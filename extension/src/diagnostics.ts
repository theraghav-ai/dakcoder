/**
 * The Problems panel, the lightbulb, and the free half of the product.
 *
 * **`gotools` is a local binary with a published JSON contract.** `gotools lint
 * --format json` and `gotools legacy-audit --format json` emit
 * `{rule, severity, path, line, col, message, fix, citation}` — the tags on
 * `rules.Violation` in `gotools/internal/rules/rule.go`, which that file's own
 * comment declares to be API. So template-compliance and legacy findings become
 * real, navigable `Diagnostic`s sub-second, offline, signed out, for zero model
 * tokens and zero quota. Nothing in this module asks the agent a question it can
 * answer itself, and nothing here regexes prose out of a `tool_result`: the
 * structured output already exists, and parsing English that a model wrote is
 * how a feature starts working and then quietly stops.
 *
 * **`gopls` findings are not republished.** The `golang.go` extension already
 * owns them, and two extensions publishing the same squiggle produces duplicate
 * rows the developer cannot dismiss. They are *read* — `goDiagnostics()` — when
 * the agent needs them, and that is the only relationship this module has with
 * them.
 *
 * **Re-running a gate stage locally is free.** When a gate blocks, the stream
 * carries the failing stage's output, which tells the developer *which* stage
 * failed. Running the same command here as a real `Task` with a problem matcher
 * tells them *why*, in the Problems panel, with F8 walking the errors — and it
 * costs no tokens, works offline, and gives navigation the transcript cannot.
 * The two are complementary: the agent says which stage, the editor says why.
 *
 * **The prompts sent to the agent are deliberately not localised.** Every
 * user-visible string goes through `vscode.l10n.t`; the task text a lightbulb
 * builds is an instruction to a model, not UI, and translating it would change
 * what the agent is asked to do. The distinction is marked at each site.
 */

import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { chmodSync, statSync } from 'node:fs';
import * as vscode from 'vscode';

import type { GateEvent, Intent } from './protocol';

// ── the gotools JSON contract ───────────────────────────────────────────────

/**
 * Mirrors `rules.Violation` (gotools/internal/rules/rule.go).
 *
 * Not in `protocol.ts` on purpose: that file is the *runtime's* wire contract,
 * and this is a local binary's stdout. Putting them together would imply the
 * two version as one thing, and they do not — `gotools` ships in `bin/` and the
 * runtime ships as a wheel.
 *
 * `severity` is typed as `string` rather than a union for the same additive
 * reason the event union has a default arm: a severity this build has not heard
 * of is a row that still renders, not a parse failure.
 */
export interface LintViolation {
  rule: string;
  severity: string;
  /** Workspace-relative, forward slashes — `normalisePath` in the Go source. */
  path: string;
  line: number;
  /** `omitempty`: absent, or 0, means the rule reported a line and no column. */
  col?: number;
  message: string;
  /** One-line imperative remedy. Absent on rules that have no mechanical fix. */
  fix?: string;
  /** e.g. `skill.md §Repository Pattern; SOP.md §Handler Pattern`. */
  citation?: string;
}

/**
 * Mirrors `rules.Result`.
 *
 * Every slice is optional here because Go marshals a nil slice as `null`, not
 * `[]` — `violations` has no `omitempty` and still arrives as `null` on a clean
 * run. Treating that as an empty array is the difference between "clean" and a
 * `TypeError` on the happy path.
 */
export interface LintResult {
  ok: boolean;
  count: number;
  violations?: LintViolation[] | null;
  /** Findings outside `--paths`. Reported for visibility; never blocking. */
  out_of_scope?: LintViolation[] | null;
  out_of_scope_count: number;
  warnings?: LintViolation[] | null;
  files_scanned: number;
  rules_run: number;
  duration_ms: number;
}

/** Mirrors `rules.Rule` as `gotools rules --format json` emits it. */
export interface RuleDoc {
  id: string;
  severity: string;
  summary: string;
  citation?: string;
  legacy: boolean;
}

export type AuditKind = 'rules_lint' | 'legacy_audit';

// ── constants ───────────────────────────────────────────────────────────────

/**
 * Two sources, two collections. The Problems panel groups and filters by
 * source, and a developer auditing legacy code wants to hide those findings
 * without losing the compliance ones — which one collection cannot express.
 * The source string is also how the code-action provider tells a `rules_lint`
 * finding from a `legacy_audit` one without a second lookup table.
 */
export const RULES_SOURCE = 'dakcoder';
export const LEGACY_SOURCE = 'dakcoder legacy';

const TASK_TYPE = 'dakcoder';
const TASK_SOURCE = 'dakcoder';

/** Contributed in this module's `package.json` fragment; see `contributes`. */
const GO_MATCHER = '$dakcoder-go';

/** Virtual documents for "Explain this rule" when the cited doc is not on disk. */
const RULE_SCHEME = 'dakcoder-rule';

/**
 * `gotools` reports a *start* position and no end. A zero-width range draws no
 * squiggle at all, so the end column is deliberately past any real line: the
 * editor clamps a Position to the end of its line when it renders, so this
 * underlines exactly to end-of-line and never further. Only used when the file
 * is not open — when it is, the real line is available and gets used instead.
 */
const CLAMPED_EOL = 4096;

/** A lint of a large repository is seconds, not minutes. Past this it is stuck. */
const LINT_TIMEOUT_MS = 120_000;

/** Enough for `gotools rules` and `fx-wire`, which do no analysis. */
const QUICK_TIMEOUT_MS = 20_000;

/** Save-triggered lint coalesces; a formatter that saves twice must cost one run. */
const SAVE_DEBOUNCE_MS = 750;

/** SQL that has leaked into a handler. Keywords only — no attempt to parse. */
const SQL_HINT =
  /\b(SELECT\s+[\s\S]*?\bFROM\b|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|LEFT\s+JOIN|INNER\s+JOIN)\b/i;
/** The Squirrel builder and the raw drivers, which are SQL without the keywords. */
const SQL_API_HINT = /\b(squirrel|sq\.(Select|Insert|Update|Delete)|db\.(Query|Exec|QueryRow))\b/;

/** `validate:"..."` on a struct field, which is what `govalid` reads. */
const VALIDATE_TAG = /`[^`]*\bvalidate:"/;

/**
 * The template's own constructor convention. `gotools` matches on the *returned
 * type*, which needs a parse; this matches on the name, which needs a regex.
 * That is fine because this is only a shortcut to the lightbulb — `fx-registration`
 * remains the authority, and it publishes a diagnostic that gets the same action.
 */
const FX_CTOR = /^\s*func\s+(New\w*(?:Handler|Repository))\s*\(/;

// ── what the module needs from the assembler ────────────────────────────────

export interface DiagnosticsDeps {
  /**
   * Start an agent task. Wired to the same `submit()` the chat uses, so a run
   * started from a lightbulb appears in the panel like any other — a task the
   * developer cannot see in the transcript is a task they cannot steer or stop.
   */
  startTask(task: string, options?: { intent?: Intent; acceptance?: string[] }): Promise<void>;
  /**
   * Folder `[0]`, matching what `extension.ts` spawns the runtime against. A
   * different folder would publish findings about a workspace the agent cannot
   * see, which is worse than publishing none.
   */
  workspaceRoot(): vscode.Uri | undefined;
  /** The `.vsix` root. `bin/gotools-<platform>` lives under it (plan §4.5). */
  extensionUri: vscode.Uri;
  log: vscode.LogOutputChannel;
}

// ── registration ────────────────────────────────────────────────────────────

/**
 * Wire the diagnostics surface. Returns the instance so activation can call
 * `audit()` after a run finishes and `offerGateRerun()` on a blocked gate.
 *
 * Nothing here spawns anything: the provider registrations and the collections
 * are cheap, and `gotools` is not located until something asks for a finding.
 */
export function register(
  context: vscode.ExtensionContext,
  deps: DiagnosticsDeps,
): GoDiagnostics {
  const service = new GoDiagnostics(deps);
  context.subscriptions.push(service);

  const command = (id: string, run: (...args: never[]) => unknown) =>
    context.subscriptions.push(
      vscode.commands.registerCommand(id, run as (...args: unknown[]) => unknown),
    );

  command('dakcoder.auditTemplate', () => service.audit('rules_lint', { reveal: true }));
  command('dakcoder.auditLegacy', () => service.audit('legacy_audit', { reveal: true }));
  command('dakcoder.clearDiagnostics', () => service.clear());
  command('dakcoder.explainRule', (arg?: unknown) => service.explainRule(arg));
  command('dakcoder.fixDiagnostic', (arg?: unknown) => service.fixDiagnostic(arg));
  command('dakcoder.debugDiagnostic', (arg?: unknown) => service.debugDiagnostic(arg));
  command('dakcoder.migrateHandler', (arg?: unknown) => service.migrateHandler(arg));
  command('dakcoder.wireIntoFx', (arg?: unknown) => service.wireIntoFx(arg));
  command('dakcoder.regenerateValidators', () => service.regenerateValidators());
  command('dakcoder.extractToRepository', (arg?: unknown) => service.extractToRepository(arg));
  command('dakcoder.rerunGateStage', (arg?: unknown) => service.rerunGateStage(arg));

  return service;
}

// ── the service ─────────────────────────────────────────────────────────────

export class GoDiagnostics implements vscode.Disposable {
  private readonly rules = vscode.languages.createDiagnosticCollection('dakcoder');
  private readonly legacy = vscode.languages.createDiagnosticCollection('dakcoder-legacy');
  private readonly subscriptions: vscode.Disposable[] = [];

  /**
   * The violation behind each published diagnostic, keyed by position and rule.
   *
   * Not a `WeakMap` on the `Diagnostic` object: a diagnostic handed back through
   * `CodeActionContext` has crossed a marshalling boundary and is not guaranteed
   * to be the same instance, so identity is not a key that survives.
   */
  private readonly findings = new Map<string, LintViolation>();

  private tool?: Promise<string>;
  private docIndex?: Promise<Map<string, ReferenceDoc>>;
  private bootstrap?: Promise<string>;
  private ruleTable?: Promise<Map<string, RuleDoc>>;
  private migrateCommand?: Promise<boolean>;
  private saveTimer?: ReturnType<typeof setTimeout>;
  private running = false;

  constructor(private readonly deps: DiagnosticsDeps) {
    this.subscriptions.push(
      this.rules,
      this.legacy,
      vscode.languages.registerCodeActionsProvider(
        { language: 'go', scheme: 'file' },
        new DakcoderCodeActions(this),
        { providedCodeActionKinds: DakcoderCodeActions.kinds },
      ),
      vscode.workspace.registerTextDocumentContentProvider(RULE_SCHEME, {
        provideTextDocumentContent: (uri) => this.ruleMarkdown(uri),
      }),
      vscode.tasks.registerTaskProvider(TASK_TYPE, {
        provideTasks: () => this.provideStageTasks(),
        resolveTask: (task) => this.resolveStageTask(task),
      }),
      // A deleted file keeps its squiggles forever otherwise, and a Problems row
      // that opens nothing is the kind of small lie that costs trust in all of them.
      vscode.workspace.onDidDeleteFiles((event) => {
        for (const uri of event.files) {
          this.forget(RULES_SOURCE, this.rules, uri);
          this.forget(LEGACY_SOURCE, this.legacy, uri);
        }
      }),
      vscode.workspace.onDidSaveTextDocument((document) => this.onSave(document)),
    );

    // Cheap, and the FX lightbulb is only correct while this is current.
    const watcher = vscode.workspace.createFileSystemWatcher('**/bootstrap/bootstrapper.go');
    const invalidate = () => {
      this.bootstrap = undefined;
    };
    watcher.onDidChange(invalidate, undefined, this.subscriptions);
    watcher.onDidCreate(invalidate, undefined, this.subscriptions);
    watcher.onDidDelete(invalidate, undefined, this.subscriptions);
    this.subscriptions.push(watcher);
  }

  // ── auditing ──────────────────────────────────────────────────────────────

  /**
   * Run one of the two audits and publish the result.
   *
   * `paths` scopes *blocking*, not scanning: `gotools` still analyses the whole
   * workspace and returns everything else under `out_of_scope`. That is why a
   * scoped run merges only the named files here — publishing its out-of-scope
   * findings would silently turn a one-file save into a whole-repository audit.
   */
  async audit(
    kind: AuditKind,
    options: { paths?: string[]; reveal?: boolean; quiet?: boolean } = {},
  ): Promise<LintResult | undefined> {
    const root = this.deps.workspaceRoot();
    if (!root) {
      if (!options.quiet) {
        void vscode.window.showInformationMessage(
          vscode.l10n.t('Open a Go service folder before auditing it.'),
        );
      }
      return undefined;
    }
    if (this.running) return undefined; // one gotools at a time; they read the same tree

    const title =
      kind === 'legacy_audit'
        ? vscode.l10n.t('Auditing legacy patterns…')
        : vscode.l10n.t('Auditing template compliance…');

    this.running = true;
    try {
      const result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title, cancellable: true },
        (_progress, token) => this.runLint(root, kind, options.paths, token),
      );
      if (!result) return undefined;
      await this.publish(root, kind, result, options.paths);
      if (options.reveal) await this.report(kind, result);
      return result;
    } finally {
      this.running = false;
    }
  }

  private async runLint(
    root: vscode.Uri,
    kind: AuditKind,
    paths: string[] | undefined,
    token: vscode.CancellationToken,
  ): Promise<LintResult | undefined> {
    const tool = await this.gotools();
    if (!tool) return undefined;

    const args = [
      kind === 'legacy_audit' ? 'legacy-audit' : 'lint',
      '--root',
      root.fsPath,
      '--format',
      'json',
    ];
    if (paths?.length) args.push('--paths', paths.join(','));

    const done = await capture(tool, args, {
      cwd: root.fsPath,
      timeoutMs: LINT_TIMEOUT_MS,
      token,
    });
    if (done.cancelled) return undefined;

    // The linter convention `gotools` documents: 0 clean, 1 findings, 2 the tool
    // itself failed. Treating 1 as an error would report every violation as a
    // crash; treating 2 as findings would report a crash as a clean run.
    if (done.code === 2 || done.timedOut) {
      const detail = done.timedOut
        ? vscode.l10n.t('gotools did not finish within {0} seconds.', LINT_TIMEOUT_MS / 1000)
        : firstLine(done.stderr) || vscode.l10n.t('gotools exited with {0}.', done.code);
      this.deps.log.error(`gotools ${args[0]} failed: ${done.stderr.trim() || done.code}`);
      void vscode.window.showErrorMessage(detail);
      return undefined;
    }
    try {
      return JSON.parse(done.stdout) as LintResult;
    } catch {
      // Not a parse bug to surface as a stack trace: the overwhelmingly likely
      // cause is a gotools older than this build, and the version skew is the
      // thing worth saying.
      this.deps.log.error(`gotools ${args[0]} produced output this build cannot read`);
      void vscode.window.showErrorMessage(
        vscode.l10n.t('gotools produced output this extension cannot read. Run "dakcoder: Doctor".'),
      );
      return undefined;
    }
  }

  /** Turn a result into diagnostics, grouped per file so each `set` is one call. */
  private async publish(
    root: vscode.Uri,
    kind: AuditKind,
    result: LintResult,
    paths?: string[],
  ): Promise<void> {
    const collection = kind === 'legacy_audit' ? this.legacy : this.rules;
    const source = kind === 'legacy_audit' ? LEGACY_SOURCE : RULES_SOURCE;
    const scoped = paths?.length ? new Set(paths.map(normalisePath)) : undefined;

    const all = [
      ...(result.violations ?? []),
      ...(result.out_of_scope ?? []),
      ...(result.warnings ?? []),
    ].filter((v) => !scoped || scoped.has(normalisePath(v.path)));

    const docs = await this.documents();
    const grouped = new Map<string, { uri: vscode.Uri; items: vscode.Diagnostic[] }>();

    for (const violation of all) {
      const uri = vscode.Uri.joinPath(root, ...normalisePath(violation.path).split('/'));
      const key = uri.toString();
      const bucket = grouped.get(key) ?? { uri, items: [] };
      bucket.items.push(this.toDiagnostic(uri, violation, source, docs));
      grouped.set(key, bucket);
    }

    if (scoped) {
      // A scoped run is an opinion about the named files and nothing else. It
      // must not touch the other collection either: re-linting one file on save
      // is not a statement about the legacy audit the developer ran an hour ago.
      for (const relative of scoped) {
        const uri = vscode.Uri.joinPath(root, ...relative.split('/'));
        this.forget(source, collection, uri);
        collection.set(uri, grouped.get(uri.toString())?.items ?? []);
      }
      return;
    }

    this.forget(source, collection);
    for (const { uri, items } of grouped.values()) collection.set(uri, items);
  }

  private toDiagnostic(
    uri: vscode.Uri,
    violation: LintViolation,
    source: string,
    docs: Map<string, ReferenceDoc>,
  ): vscode.Diagnostic {
    const range = rangeFor(uri, violation.line, violation.col);
    const message = violation.fix
      ? `${violation.message}\n${vscode.l10n.t('Fix: {0}', violation.fix)}`
      : violation.message;

    const diagnostic = new vscode.Diagnostic(range, message, severityOf(violation.severity));
    diagnostic.source = source;

    // `Diagnostic.code` with a `target` is the extension-API spelling of LSP's
    // `codeDescription`: the rule id renders as a link straight to its authority.
    const citation = resolveCitation(violation.citation, docs);
    diagnostic.code = citation
      ? { value: violation.rule, target: citation.link }
      : violation.rule;

    if (citation) {
      // A child row in the Problems panel that opens the cited section. The
      // difference between a linter that looks opinionated and one a developer
      // can check is entirely whether they can reach the sentence it enforces.
      diagnostic.relatedInformation = [
        new vscode.DiagnosticRelatedInformation(citation.location, citation.label),
      ];
    }

    this.findings.set(findingKey(source, uri, range.start.line, violation.rule), violation);
    return diagnostic;
  }

  private async report(kind: AuditKind, result: LintResult): Promise<void> {
    const blocking = (result.violations ?? []).length;
    const warnings = (result.warnings ?? []).length;
    const total = blocking + warnings + result.out_of_scope_count;

    if (total === 0) {
      void vscode.window.showInformationMessage(
        kind === 'legacy_audit'
          ? vscode.l10n.t('No legacy patterns found in {0} files.', result.files_scanned)
          : vscode.l10n.t('Template compliance is clean across {0} files.', result.files_scanned),
      );
      return;
    }

    // `vscode.l10n` has no ICU plurals, so the singular is a separate string.
    // "Found 1 findings" reads as a bug in the tool that found it.
    const summary =
      total === 1
        ? vscode.l10n.t('1 finding.')
        : vscode.l10n.t('{0} findings.', total);
    const show = vscode.l10n.t('Show Problems');
    const picked = await vscode.window.showInformationMessage(summary, show);
    if (picked === show) {
      // Focus goes to the panel only because the developer asked for it with a
      // click. Nothing in this module takes focus off the editor on its own.
      await vscode.commands.executeCommand('workbench.actions.view.problems');
    }
  }

  /** Re-lint one file on save, scoped, when the setting is on. */
  private onSave(document: vscode.TextDocument): void {
    if (document.languageId !== 'go' || document.uri.scheme !== 'file') return;
    if (!vscode.workspace.getConfiguration('dakcoder').get<boolean>('lintOnSave')) return;
    const root = this.deps.workspaceRoot();
    if (!root) return;
    const relative = relativeTo(root, document.uri);
    if (!relative) return;

    // Debounced because a format-on-save chain can fire this twice for one
    // keystroke, and the second run would publish the same findings again.
    if (this.saveTimer) clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => {
      this.saveTimer = undefined;
      void this.audit('rules_lint', { paths: [relative], quiet: true });
    }, SAVE_DEBOUNCE_MS);
  }

  clear(): void {
    this.rules.clear();
    this.legacy.clear();
    this.findings.clear();
  }

  /**
   * Drop one collection's diagnostics and the violations behind them.
   *
   * Scoped to a single source because the two audits are independent: clearing
   * the compliance findings for a file must leave its legacy findings — and the
   * violations behind them — exactly where they were, or a legacy row keeps its
   * squiggle and quietly loses its lightbulb.
   */
  private forget(source: string, collection: vscode.DiagnosticCollection, uri?: vscode.Uri): void {
    if (uri) collection.delete(uri);
    else collection.clear();
    const prefix = uri ? `${source}\u0000${uri.toString()}\u0000` : `${source}\u0000`;
    for (const key of [...this.findings.keys()]) {
      if (key.startsWith(prefix)) this.findings.delete(key);
    }
  }

  /** What the code-action provider needs: the structured finding behind a row. */
  violationFor(uri: vscode.Uri, diagnostic: vscode.Diagnostic): LintViolation | undefined {
    const code = typeof diagnostic.code === 'object' ? diagnostic.code.value : diagnostic.code;
    if (code === undefined || !diagnostic.source) return undefined;
    return this.findings.get(
      findingKey(diagnostic.source, uri, diagnostic.range.start.line, String(code)),
    );
  }

  // ── the seven code actions ────────────────────────────────────────────────

  /**
   * *Fix with dakcoder* — one rule, one file, one range, in Debugger mode.
   *
   * Scoped deliberately: an unscoped "fix the lint" is how a run that should
   * touch one handler ends up reformatting the repository. The acceptance
   * criterion is the same command the gate runs, so the agent's own verifier
   * checks exactly what the developer clicked.
   */
  async fixDiagnostic(arg?: unknown): Promise<void> {
    const target = this.resolveTarget(arg);
    if (!target) {
      void vscode.window.showInformationMessage(
        vscode.l10n.t('Put the cursor on a dakcoder finding, then try again.'),
      );
      return;
    }
    const { violation, relative } = target;
    const where = violation.line > 0 ? `${relative}:${violation.line}` : relative;

    // Not localised: this is the instruction to the model, not UI. See the file
    // header — a translated prompt changes what the agent is asked to do.
    const task = [
      `Fix the ${violation.rule} violation at ${where}.`,
      '',
      `    ${violation.message}`,
      violation.fix ? `    fix: ${violation.fix}` : '',
      violation.citation ? `    see: ${violation.citation}` : '',
      '',
      `Change only what ${violation.rule} requires in ${relative}. Do not reformat`,
      'the rest of the file and do not fix unrelated findings.',
    ]
      .filter(Boolean)
      .join('\n');

    await this.deps.startTask(task, {
      intent: 'agent',
      acceptance: [`gotools lint --only ${violation.rule} --paths ${relative} reports no violations`],
    });
  }

  /**
   * *Explain this rule* — the cited section, in a peek.
   *
   * Peeked rather than opened, when the document is on disk: opening a second
   * editor loses the code the developer was reading, which is the thing the
   * explanation is about. When the document is not on disk — a service checkout
   * has no `skill.md` — the rule's own summary and citation open beside it.
   * Neither path invents an explanation.
   */
  async explainRule(arg?: unknown): Promise<void> {
    const ruleId = await this.resolveRuleId(arg);
    if (!ruleId) return;

    const table = await this.ruleDocs();
    const rule = table.get(ruleId);
    const docs = await this.documents();
    const citation = resolveCitation(rule?.citation, docs);

    const editor = vscode.window.activeTextEditor;
    if (citation && editor) {
      // `editor.action.peekLocations` peeks inside the *active* editor, so the
      // developer keeps their place in the Go file they are reading.
      await vscode.commands.executeCommand(
        'editor.action.peekLocations',
        editor.document.uri,
        editor.selection.active,
        [citation.location],
        'peek',
      );
      return;
    }

    const uri = vscode.Uri.from({ scheme: RULE_SCHEME, path: `/${ruleId}.md` });
    const document = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(document, {
      preview: true,
      viewColumn: vscode.ViewColumn.Beside,
      // Focus stays where the developer put it.
      preserveFocus: true,
    });
  }

  /** *Migrate this handler* — one unit of the migration flow (plan §11.4). */
  async migrateHandler(arg?: unknown): Promise<void> {
    const target = this.resolveTarget(arg);
    if (!target) return;
    const { relative, violation } = target;

    // The migration plan viewer owns the flow when it is present. Checked rather
    // than assumed, because this module must keep working in a build where that
    // view has not shipped yet.
    if (await this.hasMigrateCommand()) {
      await vscode.commands.executeCommand('dakcoder.migrate', {
        path: relative,
        rule: violation.rule,
      });
      return;
    }

    // Not localised: an instruction to the model. See the file header.
    await this.deps.startTask(
      [
        `Migrate ${relative} to the n-api-template contract, one unit only.`,
        '',
        `    ${violation.rule}: ${violation.message}`,
        violation.fix ? `    fix: ${violation.fix}` : '',
        '',
        `Do not touch files other than ${relative} and the repository or bootstrap`,
        'files the migration of this one unit requires.',
      ]
        .filter(Boolean)
        .join('\n'),
      {
        intent: 'agent',
        acceptance: [`gotools legacy-audit --paths ${relative} reports no violations`],
      },
    );
  }

  /**
   * *Debug with dakcoder* — a compile error, exactly as the compiler wrote it.
   *
   * Read from `vscode.languages.getDiagnostics`, which is where `golang.go`
   * already publishes them. The compiler's own sentence goes to the agent
   * verbatim: paraphrasing an error message is how the agent ends up debugging
   * a different problem from the one on screen.
   */
  async debugDiagnostic(arg?: unknown): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    const uri = isUriArg(arg) ? arg : editor?.document.uri;
    if (!uri) return;
    const root = this.deps.workspaceRoot();
    const lines = goDiagnostics(root, uri).filter((d) => d.severity === 'error');
    if (!lines.length) {
      void vscode.window.showInformationMessage(
        vscode.l10n.t('There are no Go errors in this file to debug.'),
      );
      return;
    }

    // Not localised: an instruction to the model. See the file header.
    const rendered = lines.slice(0, 20).map((d) => `    ${formatGoDiagnostic(d)}`);
    await this.deps.startTask(
      [
        `Fix the Go compile errors in ${lines[0].path}.`,
        '',
        ...rendered,
        '',
        'These are gopls / compiler errors from the editor, quoted verbatim.',
        'Fix the cause, not the symptom, and do not silence an error by deleting the code that reports it.',
      ].join('\n'),
      { intent: 'agent', acceptance: ['go build ./... succeeds'] },
    );
  }

  /**
   * *Wire into FX* — `gotools fx-wire`, run locally.
   *
   * Plan §11.2 names the `fx_wire` *tool*, but the loopback API exposes no
   * single-tool invocation (§7 lists `/v1/tasks` and nothing finer), so calling
   * it through the agent would spend a model turn on a deterministic edit. The
   * same binary runs here for free, and the developer sees the patch in the
   * terminal and their SCM view rather than in a transcript.
   */
  async wireIntoFx(arg?: unknown): Promise<void> {
    const root = this.deps.workspaceRoot();
    if (!root) return;
    const target = await this.resolveConstructor(arg, root);
    if (!target) return;

    const tool = await this.gotools();
    if (!tool) return;

    await this.runShellTask(
      `fx-wire ${target.ctor}`,
      tool,
      ['fx-wire', '--root', root.fsPath, '--kind', target.kind, '--ctor', target.ctor],
      root,
    );
    // Re-audit whatever the exit code was: on success to clear the finding, on
    // failure because a half-applied patch is exactly when the Problems panel
    // most needs to be current.
    await this.audit('rules_lint', { quiet: true });
  }

  /**
   * *Regenerate validators* — `govalid ./request.go`, from `handler/`.
   *
   * The working directory is load-bearing and the reason this is not a bare
   * `govalid ./...`: the generated validators must land in the `handler`
   * package, and running from the repository root produces them in the wrong
   * package, which compiles far enough to be confusing (Part A, `govalid_gen`).
   */
  async regenerateValidators(): Promise<void> {
    const root = this.deps.workspaceRoot();
    if (!root) return;
    const handler = vscode.Uri.joinPath(root, 'handler');
    if (!(await exists(vscode.Uri.joinPath(handler, 'request.go')))) {
      void vscode.window.showWarningMessage(
        vscode.l10n.t('handler/request.go does not exist, so there are no validators to generate.'),
      );
      return;
    }
    await this.runShellTask('govalid', 'govalid', ['./request.go'], handler);
  }

  /** *Extract to repository* — a scoped `layer-sql-boundary` fix. */
  async extractToRepository(arg?: unknown): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    const root = this.deps.workspaceRoot();
    if (!editor || !root) return;
    const relative = relativeTo(root, editor.document.uri);
    if (!relative) return;

    const selection = isRangeArg(arg) ? arg : editor.selection;
    const text = editor.document.getText(selection);
    const from = selection.start.line + 1;
    const to = selection.end.line + 1;

    // Not localised: an instruction to the model. See the file header.
    await this.deps.startTask(
      [
        `Extract the SQL in ${relative} lines ${from}–${to} into the repository layer.`,
        '',
        '```go',
        text.trim(),
        '```',
        '',
        'Add a method on the matching repo/postgres repository, expose it through',
        'its core/port interface, and call it from the handler. The handler must',
        'not import database/sql, pgx or squirrel when you are done.',
      ].join('\n'),
      {
        intent: 'agent',
        acceptance: [
          `gotools lint --only layer-sql-boundary --paths ${relative} reports no violations`,
          'go build ./... succeeds',
        ],
      },
    );
  }

  // ── gate re-run ───────────────────────────────────────────────────────────

  /**
   * Offer to re-run the stage a gate blocked on, locally.
   *
   * Called on a `gate` event; ignores everything that is not a blocked gate. The
   * stage name arrives from the server as a string, and an unrecognised one is a
   * stage this build has not heard of — additive-only means that is a re-run we
   * decline to offer, never an error we show.
   */
  async offerGateRerun(gate: GateEvent): Promise<void> {
    if (gate.ok || !gate.blocked_by) return;
    if (!stageCommand(gate.blocked_by)) return;

    const run = vscode.l10n.t('Run {0} locally', gate.blocked_by);
    const picked = await vscode.window.showWarningMessage(
      vscode.l10n.t('The gate blocked on {0}.', gate.blocked_by),
      run,
    );
    if (picked === run) await this.rerunGateStage(gate.blocked_by);
  }

  /**
   * Run one gate stage as a real Task, so its errors land in Problems and F8
   * walks them. Stages with no honest local equivalent say so rather than
   * running something similar-looking.
   */
  async rerunGateStage(arg?: unknown): Promise<void> {
    const root = this.deps.workspaceRoot();
    if (!root) return;
    const stage = typeof arg === 'string' ? arg : await this.pickStage();
    if (!stage) return;

    // `rules_lint` re-runs as an audit rather than a task: the JSON path gives
    // navigable diagnostics with rule ids and citations, which a problem matcher
    // scraping the text output cannot.
    if (normaliseStage(stage) === 'rules_lint') {
      await this.audit('rules_lint', { reveal: true });
      return;
    }

    const spec = stageCommand(stage);
    if (!spec) {
      void vscode.window.showInformationMessage(noLocalRerunReason(stage));
      return;
    }
    const command = spec.tool === 'go' ? goBinary() : spec.tool;
    const cwd = spec.cwd ? vscode.Uri.joinPath(root, spec.cwd) : root;
    await this.runShellTask(stage, command, spec.args, cwd, matcherFor(spec));
  }

  private async pickStage(): Promise<string | undefined> {
    const items = [...RERUNNABLE.keys(), 'rules_lint'].sort().map((stage) => ({ label: stage }));
    const picked = await vscode.window.showQuickPick(items, {
      placeHolder: vscode.l10n.t('Which gate stage should run locally?'),
    });
    return picked?.label;
  }

  private provideStageTasks(): vscode.Task[] {
    const root = this.deps.workspaceRoot();
    if (!root) return [];
    const folder = vscode.workspace.getWorkspaceFolder(root);
    if (!folder) return [];
    const tasks: vscode.Task[] = [];
    for (const [stage, spec] of RERUNNABLE) {
      tasks.push(
        buildTask(
          folder,
          stage,
          spec.tool === 'go' ? goBinary() : spec.tool,
          spec.args,
          spec.cwd ? vscode.Uri.joinPath(root, spec.cwd) : root,
          matcherFor(spec),
        ),
      );
    }
    return tasks;
  }

  private resolveStageTask(task: vscode.Task): vscode.Task | undefined {
    const stage = (task.definition as { stage?: unknown }).stage;
    if (typeof stage !== 'string') return undefined;
    const spec = stageCommand(stage);
    const root = this.deps.workspaceRoot();
    if (!spec || !root) return undefined;
    const folder = vscode.workspace.getWorkspaceFolder(root);
    if (!folder) return undefined;
    return buildTask(
      folder,
      stage,
      spec.tool === 'go' ? goBinary() : spec.tool,
      spec.args,
      spec.cwd ? vscode.Uri.joinPath(root, spec.cwd) : root,
      matcherFor(spec),
    );
  }

  /** Execute a task and resolve with its exit code, or undefined if it never ran. */
  private async runShellTask(
    name: string,
    command: string,
    args: string[],
    cwd: vscode.Uri,
    matcher: string | undefined = GO_MATCHER,
  ): Promise<number | undefined> {
    const folder = vscode.workspace.getWorkspaceFolder(cwd) ?? vscode.workspace.workspaceFolders?.[0];
    if (!folder) return undefined;
    const task = buildTask(folder, name, command, args, cwd, matcher);
    const execution = await vscode.tasks.executeTask(task);
    return new Promise((resolve) => {
      const listener = vscode.tasks.onDidEndTaskProcess((event) => {
        if (event.execution !== execution) return;
        listener.dispose();
        resolve(event.exitCode);
      });
    });
  }

  // ── locating things ───────────────────────────────────────────────────────

  /**
   * The `gotools` binary: the setting, then the bundled per-platform build,
   * then the bare name for a developer with one on PATH.
   *
   * The bundled binary is checksum-verified against `bin/gotools.sha256` and
   * refused on mismatch (plan §4.5). A binary the developer chose — a setting or
   * one on their PATH — is not checked against that manifest, because it is not
   * the one the manifest describes and failing it would be a false alarm.
   */
  private gotools(): Promise<string | undefined> {
    this.tool ??= this.locateGotools();
    return this.tool.then((path) => {
      if (path) return path;
      void this.reportMissingTool();
      return undefined;
    });
  }

  private locateGotools(): Promise<string> {
    return resolveGotools(this.deps.extensionUri, this.deps.log);
  }

  private async reportMissingTool(): Promise<void> {
    const doctor = vscode.l10n.t('Run Doctor');
    const picked = await vscode.window.showErrorMessage(
      vscode.l10n.t('The gotools analyser could not be started. Reinstall the extension, or set "dakcoder.gotoolsPath".'),
      doctor,
    );
    if (picked === doctor) await vscode.commands.executeCommand('dakcoder.doctor');
  }

  /** `gotools rules --format json`: the rule table, for "Explain this rule". */
  private ruleDocs(): Promise<Map<string, RuleDoc>> {
    this.ruleTable ??= (async () => {
      const table = new Map<string, RuleDoc>();
      const tool = await this.gotools().catch(() => undefined);
      if (!tool) return table;
      const done = await capture(tool, ['rules', '--format', 'json'], {
        timeoutMs: QUICK_TIMEOUT_MS,
      });
      if (done.code !== 0) return table;
      try {
        for (const rule of JSON.parse(done.stdout) as RuleDoc[]) table.set(rule.id, rule);
      } catch {
        this.deps.log.warn('gotools rules produced output this build cannot read');
      }
      return table;
    })();
    return this.ruleTable;
  }

  /**
   * `skill.md` and `SOP.md`, wherever they are, indexed by heading.
   *
   * Searched in the workspace first — a template checkout has them — then in the
   * extension's own copy. When neither exists there is no citation link, and the
   * rule id renders as plain text: a link that opens nothing is worse than no
   * link, because the developer spends the click finding that out.
   */
  private documents(): Promise<Map<string, ReferenceDoc>> {
    this.docIndex ??= (async () => {
      const found = new Map<string, ReferenceDoc>();
      const candidates: vscode.Uri[] = [];
      try {
        candidates.push(
          ...(await vscode.workspace.findFiles('**/{skill,SKILL,SOP,sop}.md', '**/node_modules/**', 8)),
        );
      } catch {
        /* no workspace, or a search the host declined */
      }
      for (const name of ['skill.md', 'SOP.md']) {
        candidates.push(vscode.Uri.joinPath(this.deps.extensionUri, 'media', 'docs', name));
      }
      for (const uri of candidates) {
        const name = uri.path.split('/').pop()?.toLowerCase();
        if (!name || found.has(name)) continue;
        try {
          const text = new TextDecoder().decode(await vscode.workspace.fs.readFile(uri));
          found.set(name, { uri, headings: headingsOf(text) });
        } catch {
          /* listed but unreadable; the next candidate may not be */
        }
      }
      return found;
    })();
    return this.docIndex;
  }

  /** `bootstrap/bootstrapper.go` as text. Absent is empty, not an error. */
  private bootstrapText(): Promise<string> {
    this.bootstrap ??= (async () => {
      const root = this.deps.workspaceRoot();
      if (!root) return '';
      try {
        const uri = vscode.Uri.joinPath(root, 'bootstrap', 'bootstrapper.go');
        return new TextDecoder().decode(await vscode.workspace.fs.readFile(uri));
      } catch {
        // Not a template service, or not scaffolded yet. Either way the FX
        // lightbulb has nothing to say, and saying nothing is correct.
        return '';
      }
    })();
    return this.bootstrap;
  }

  private hasMigrateCommand(): Promise<boolean> {
    // `getCommands` returns a Thenable, not a Promise; resolving it here keeps
    // the cached field a real Promise so a second caller can `catch` on it.
    this.migrateCommand ??= Promise.resolve(vscode.commands.getCommands(true)).then((all) =>
      all.includes('dakcoder.migrate'),
    );
    return this.migrateCommand;
  }

  /**
   * Whether a constructor declared on this line is missing from the composition
   * root. Used by the lightbulb; `fx-registration` is still the authority.
   */
  async unwiredConstructor(
    document: vscode.TextDocument,
    line: number,
  ): Promise<{ ctor: string; kind: 'handler' | 'repo' } | undefined> {
    const root = this.deps.workspaceRoot();
    if (!root) return undefined;
    const relative = relativeTo(root, document.uri);
    const kind = layerKind(relative);
    if (!kind) return undefined;

    const match = document.lineAt(line).text.match(FX_CTOR);
    if (!match) return undefined;
    const ctor = match[1];
    const text = await this.bootstrapText();
    if (!text || text.includes(ctor)) return undefined;
    return { ctor, kind };
  }

  // ── argument plumbing ─────────────────────────────────────────────────────

  /**
   * The finding a command should act on: the one passed in, or the one under
   * the cursor. Both paths exist because the same command is reached from a
   * lightbulb (which passes it) and from `/fix` (which does not).
   */
  private resolveTarget(
    arg: unknown,
  ): { violation: LintViolation; relative: string; uri: vscode.Uri } | undefined {
    const root = this.deps.workspaceRoot();
    if (!root) return undefined;

    if (isTargetArg(arg)) {
      const relative = relativeTo(root, arg.uri);
      return relative ? { violation: arg.violation, relative, uri: arg.uri } : undefined;
    }

    const editor = vscode.window.activeTextEditor;
    if (!editor) return undefined;
    const uri = editor.document.uri;
    const relative = relativeTo(root, uri);
    if (!relative) return undefined;
    const line = editor.selection.active.line;
    for (const collection of [this.rules, this.legacy]) {
      for (const diagnostic of collection.get(uri) ?? []) {
        if (diagnostic.range.start.line !== line) continue;
        const violation = this.violationFor(uri, diagnostic);
        if (violation) return { violation, relative, uri };
      }
    }
    return undefined;
  }

  private async resolveRuleId(arg: unknown): Promise<string | undefined> {
    if (typeof arg === 'string' && arg.trim()) return arg.trim();
    if (isTargetArg(arg)) return arg.violation.rule;
    const target = this.resolveTarget(undefined);
    if (target) return target.violation.rule;

    const table = await this.ruleDocs();
    if (!table.size) return undefined;
    const picked = await vscode.window.showQuickPick(
      [...table.values()].map((rule) => ({
        label: rule.id,
        description: rule.severity,
        detail: rule.summary,
      })),
      { placeHolder: vscode.l10n.t('Which rule?'), matchOnDetail: true },
    );
    return picked?.label;
  }

  /**
   * Which constructor to wire. From the lightbulb it is known; from `/wire` it
   * is a name or nothing, and nothing means asking `fx-registration` which
   * constructors are unregistered — free, and a real answer rather than a guess.
   */
  private async resolveConstructor(
    arg: unknown,
    root: vscode.Uri,
  ): Promise<{ ctor: string; kind: 'handler' | 'repo' } | undefined> {
    if (isWireArg(arg)) return { ctor: arg.ctor, kind: arg.kind };
    if (typeof arg === 'string' && arg.trim()) {
      const ctor = arg.trim();
      return { ctor, kind: /Repository$/.test(ctor) ? 'repo' : 'handler' };
    }

    const editor = vscode.window.activeTextEditor;
    if (editor) {
      const here = await this.unwiredConstructor(editor.document, editor.selection.active.line);
      if (here) return here;
    }

    const result = await this.runUnwiredScan(root);
    if (!result?.length) {
      void vscode.window.showInformationMessage(
        vscode.l10n.t('Every handler and repository is already registered in bootstrap/.'),
      );
      return undefined;
    }
    const picked = await vscode.window.showQuickPick(
      result.map((c) => ({ label: c.ctor, description: c.kind, detail: c.path })),
      { placeHolder: vscode.l10n.t('Which constructor should be wired into FX?') },
    );
    if (!picked) return undefined;
    return { ctor: picked.label, kind: picked.description === 'repo' ? 'repo' : 'handler' };
  }

  /**
   * The unregistered constructors, from `fx-registration` itself.
   *
   * The constructor's *name* is read out of the Go source at the position the
   * violation points at — never out of the violation's message. The message is
   * prose written for a human and a wording change would silently break this;
   * the position is the structured part of the contract.
   */
  private async runUnwiredScan(
    root: vscode.Uri,
  ): Promise<{ ctor: string; kind: 'handler' | 'repo'; path: string }[] | undefined> {
    const tool = await this.gotools();
    if (!tool) return undefined;
    const done = await capture(
      tool,
      ['lint', '--root', root.fsPath, '--only', 'fx-registration', '--format', 'json'],
      { cwd: root.fsPath, timeoutMs: LINT_TIMEOUT_MS },
    );
    if (done.code === 2) return undefined;
    let result: LintResult;
    try {
      result = JSON.parse(done.stdout) as LintResult;
    } catch {
      return undefined;
    }

    const out: { ctor: string; kind: 'handler' | 'repo'; path: string }[] = [];
    for (const violation of [...(result.violations ?? []), ...(result.out_of_scope ?? [])]) {
      const kind = layerKind(normalisePath(violation.path));
      // A finding reported against bootstrapper.go is the "registered, but with
      // plain fx.Provide" case. `fx-wire` adds a registration and cannot repair
      // one, so it is not offered — the fix there is an edit, not a generator.
      if (!kind || violation.line <= 0) continue;
      const uri = vscode.Uri.joinPath(root, ...normalisePath(violation.path).split('/'));
      const ctor = await constructorAt(uri, violation.line);
      if (ctor) out.push({ ctor, kind, path: normalisePath(violation.path) });
    }
    return out;
  }

  // ── the virtual "explain" document ────────────────────────────────────────

  private async ruleMarkdown(uri: vscode.Uri): Promise<string> {
    const id = uri.path.replace(/^\//, '').replace(/\.md$/, '');
    const rule = (await this.ruleDocs()).get(id);
    if (!rule) {
      return vscode.l10n.t('# {0}\n\nThis build of gotools does not define a rule with that id.', id);
    }
    const lines = [
      `# ${rule.id}`,
      '',
      rule.summary,
      '',
      vscode.l10n.t('- Severity: {0}', rule.severity),
      vscode.l10n.t('- Applies to: {0}', rule.legacy ? 'legacy_audit' : 'rules_lint'),
    ];
    if (rule.citation) lines.push(vscode.l10n.t('- Source: {0}', rule.citation));
    lines.push(
      '',
      // Said plainly rather than papered over. The alternative — paraphrasing
      // the section from memory — would be inventing the authority the rule cites.
      vscode.l10n.t(
        'The cited document is not in this workspace, so only the rule definition is shown here.',
      ),
    );
    return lines.join('\n');
  }

  dispose(): void {
    if (this.saveTimer) clearTimeout(this.saveTimer);
    for (const item of this.subscriptions) {
      try {
        item.dispose();
      } catch {
        /* shutdown must not throw */
      }
    }
    this.subscriptions.length = 0;
    this.findings.clear();
  }
}

// ── the code action provider ────────────────────────────────────────────────

/**
 * Seven triggers, one provider.
 *
 * Every action is a command with its arguments already resolved, so nothing is
 * recomputed when the developer clicks: the lightbulb menu is built from what
 * is on screen, and clicking it must not go looking for the context again and
 * find something different.
 */
class DakcoderCodeActions implements vscode.CodeActionProvider {
  static readonly kinds = [
    vscode.CodeActionKind.QuickFix,
    vscode.CodeActionKind.Refactor,
    vscode.CodeActionKind.RefactorExtract,
  ];

  constructor(private readonly service: GoDiagnostics) {}

  async provideCodeActions(
    document: vscode.TextDocument,
    range: vscode.Range | vscode.Selection,
    context: vscode.CodeActionContext,
  ): Promise<vscode.CodeAction[]> {
    const actions: vscode.CodeAction[] = [];
    let explained = false;

    for (const diagnostic of context.diagnostics) {
      const ours = diagnostic.source === RULES_SOURCE || diagnostic.source === LEGACY_SOURCE;
      if (!ours) {
        // 4. A Go compile error. Anything the Go extension flagged as an error
        // qualifies; we do not try to distinguish gopls's error kinds, because
        // that taxonomy is theirs and it changes.
        if (diagnostic.severity === vscode.DiagnosticSeverity.Error) {
          actions.push(
            action(
              vscode.l10n.t('Debug with dakcoder'),
              vscode.CodeActionKind.QuickFix,
              'dakcoder.debugDiagnostic',
              [document.uri],
              diagnostic,
            ),
          );
        }
        continue;
      }

      const violation = this.service.violationFor(document.uri, diagnostic);
      if (!violation) continue;
      const target: TargetArg = { uri: document.uri, violation };

      if (diagnostic.source === LEGACY_SOURCE) {
        // 3. legacy_audit → one unit of the migration flow.
        actions.push(
          action(
            vscode.l10n.t('Migrate this handler'),
            vscode.CodeActionKind.QuickFix,
            'dakcoder.migrateHandler',
            [target],
            diagnostic,
          ),
        );
      } else {
        // 1. rules_lint → a scoped Debugger task.
        const fix = action(
          vscode.l10n.t('Fix with dakcoder'),
          vscode.CodeActionKind.QuickFix,
          'dakcoder.fixDiagnostic',
          [target],
          diagnostic,
        );
        // Preferred only when gotools stated a remedy: without one the agent is
        // being asked to invent the fix, which is not the default a Ctrl+. should
        // land on.
        fix.isPreferred = !!violation.fix;
        actions.push(fix);

        // 5. fx-registration is the authoritative form of "not wired into FX".
        if (violation.rule === 'fx-registration' && violation.line > 0) {
          const ctor = document.lineAt(clampLine(document, violation.line - 1)).text.match(FX_CTOR);
          if (ctor) {
            actions.push(
              action(
                vscode.l10n.t('Wire into FX'),
                vscode.CodeActionKind.QuickFix,
                'dakcoder.wireIntoFx',
                [{ ctor: ctor[1], kind: /Repository$/.test(ctor[1]) ? 'repo' : 'handler' }],
                diagnostic,
              ),
            );
          }
        }
      }

      // 2. Every dakcoder finding can explain itself. Offered once: seven copies
      // of the same entry is what a lightbulb menu looks like when nobody checked.
      if (!explained) {
        explained = true;
        actions.push(
          action(
            vscode.l10n.t('Explain this rule'),
            vscode.CodeActionKind.QuickFix,
            'dakcoder.explainRule',
            [target],
          ),
        );
      }
    }

    // 5b. The same wiring offer without having run an audit first — the
    // developer has just written the constructor and the lint has not run yet.
    const unwired = await this.service.unwiredConstructor(document, range.start.line);
    if (unwired && !actions.some((a) => a.command?.command === 'dakcoder.wireIntoFx')) {
      actions.push(
        action(
          vscode.l10n.t('Wire into FX'),
          vscode.CodeActionKind.Refactor,
          'dakcoder.wireIntoFx',
          [unwired],
        ),
      );
    }

    // 6. A validate tag in handler/request.go.
    //
    // Offered whenever the cursor is on such a tag, not only when it was just
    // edited: nothing on the wire and nothing in the editor reports "this tag
    // changed", and diffing against git here would be a second source of truth
    // that disagrees with the SCM view the developer is already looking at.
    if (/(^|\/)handler\/request\.go$/.test(document.uri.path)) {
      const line = document.lineAt(range.start.line).text;
      if (VALIDATE_TAG.test(line)) {
        actions.push(
          action(
            vscode.l10n.t('Regenerate validators'),
            vscode.CodeActionKind.QuickFix,
            'dakcoder.regenerateValidators',
            [],
          ),
        );
      }
    }

    // 7. A selection in a handler that contains SQL.
    if (!range.isEmpty && isHandlerPath(document.uri.path)) {
      const selected = document.getText(range);
      if (SQL_HINT.test(selected) || SQL_API_HINT.test(selected)) {
        actions.push(
          action(
            vscode.l10n.t('Extract to repository'),
            vscode.CodeActionKind.RefactorExtract,
            'dakcoder.extractToRepository',
            [new vscode.Range(range.start, range.end)],
          ),
        );
      }
    }

    return actions;
  }
}

function action(
  title: string,
  kind: vscode.CodeActionKind,
  command: string,
  args: unknown[],
  diagnostic?: vscode.Diagnostic,
): vscode.CodeAction {
  const created = new vscode.CodeAction(title, kind);
  created.command = { command, title, arguments: args };
  if (diagnostic) created.diagnostics = [diagnostic];
  return created;
}

// ── gate stages ─────────────────────────────────────────────────────────────

interface StageSpec {
  /** `go` is resolved through `dakcoder.goPath`; everything else is a bare name. */
  tool: string;
  args: string[];
  /** Workspace-relative, when the working directory is load-bearing. */
  cwd?: string;
  matcher?: string;
}

/**
 * The gate stages that have an honest local equivalent, running the same
 * command the agent's tool runs.
 *
 * `go test -fullpath` is not decoration: without it Go prints test file names
 * relative to the *package* directory, and a problem matcher resolving those
 * against the workspace root opens the wrong file — or none — for every test
 * outside the root package.
 */
const RERUNNABLE = new Map<string, StageSpec>([
  ['go_build', { tool: 'go', args: ['build', './...'] }],
  ['go_vet', { tool: 'go', args: ['vet', './...'] }],
  ['go_test', { tool: 'go', args: ['test', '-fullpath', './...'] }],
  ['go_mod tidy', { tool: 'go', args: ['mod', 'tidy'] }],
  ['govalid_gen', { tool: 'govalid', args: ['./request.go'], cwd: 'handler' }],
  ['golangci_lint', { tool: 'golangci-lint', args: ['run'] }],
  // No matcher: govulncheck reports module vulnerabilities, not file positions,
  // and a matcher that finds nothing is indistinguishable from a clean run.
  ['govulncheck', { tool: 'govulncheck', args: ['./...'], matcher: '' }],
]);

/**
 * Why the remaining stages are not offered. Each of these would otherwise run
 * something that looks like the stage and is not it.
 */
function noLocalRerunReason(stage: string): string {
  switch (normaliseStage(stage)) {
    case 'gofmt':
      // Part A §9.3: every file in the reference template fails `gofmt -l`
      // because it is CRLF, so an unscoped local gofmt reports every file.
      return vscode.l10n.t(
        'gofmt runs scoped to the files the agent touched. Running it across the workspace would report every file, because the template uses CRLF.',
      );
    case 'go_diagnostics':
      return vscode.l10n.t(
        'gopls diagnostics are already in the Problems panel, published by the Go extension.',
      );
    case 'swagger_check':
      return vscode.l10n.t(
        'swagger_check boots the service against a database and a free port, so it has no local re-run here.',
      );
    default:
      // Additive-only: a stage name this build has not heard of is one we
      // decline to re-run, not an error.
      return vscode.l10n.t('{0} has no local equivalent to re-run.', stage);
  }
}

/** `go_build (after generate)` is the same command as `go_build`. */
function normaliseStage(stage: string): string {
  return stage.replace(/\s*\(.*\)\s*$/, '').trim();
}

function stageCommand(stage: string): StageSpec | undefined {
  return RERUNNABLE.get(normaliseStage(stage));
}

/**
 * The Go matcher unless a stage opted out with an empty string.
 *
 * Written out rather than left to a default parameter because two of the three
 * call sites pass the field straight through, and a missing matcher is invisible
 * until someone notices the build errors never reached the Problems panel.
 */
function matcherFor(spec: StageSpec): string {
  return spec.matcher ?? GO_MATCHER;
}

function buildTask(
  folder: vscode.WorkspaceFolder,
  stage: string,
  command: string,
  args: string[],
  cwd: vscode.Uri,
  matcher: string | undefined,
): vscode.Task {
  const task = new vscode.Task(
    { type: TASK_TYPE, stage },
    folder,
    stage,
    TASK_SOURCE,
    new vscode.ShellExecution(quote(command), args.map(quote), { cwd: cwd.fsPath }),
    matcher ? [matcher] : [],
  );
  task.presentationOptions = {
    // `Silent` reveals the terminal only when the matcher does not account for
    // the failure. Focus is never taken from the editor — a build that steals
    // the cursor mid-keystroke is a build people stop running.
    reveal: vscode.TaskRevealKind.Silent,
    focus: false,
    echo: true,
    panel: vscode.TaskPanelKind.Shared,
    clear: true,
  };
  task.group = vscode.TaskGroup.Build;
  return task;
}

/** A path with a space is normal on Windows; `ShellExecution` needs it quoted. */
function quote(value: string): vscode.ShellQuotedString {
  return { value, quoting: vscode.ShellQuoting.Strong };
}

function goBinary(): string {
  const configured = vscode.workspace.getConfiguration('dakcoder').get<string>('goPath')?.trim();
  return configured || (process.platform === 'win32' ? 'go.exe' : 'go');
}

// ── reading gopls's diagnostics ─────────────────────────────────────────────

export interface GoDiagnosticLine {
  /** Workspace-relative when a root is given, absolute otherwise. */
  path: string;
  line: number;
  col: number;
  severity: 'error' | 'warning' | 'info' | 'hint';
  source: string;
  message: string;
}

/**
 * The editor's own Go diagnostics, for the agent and for `@diag`.
 *
 * dakcoder's own findings are filtered out. Feeding them back would present the
 * agent with its own linter's output as if the compiler had said it, and the
 * agent would then weigh one finding twice.
 */
export function goDiagnostics(
  root: vscode.Uri | undefined,
  uri?: vscode.Uri,
): GoDiagnosticLine[] {
  const entries: [vscode.Uri, vscode.Diagnostic[]][] = uri
    ? [[uri, vscode.languages.getDiagnostics(uri)]]
    : vscode.languages.getDiagnostics();

  const out: GoDiagnosticLine[] = [];
  for (const [target, diagnostics] of entries) {
    if (!target.path.endsWith('.go')) continue;
    for (const diagnostic of diagnostics) {
      if (diagnostic.source === RULES_SOURCE || diagnostic.source === LEGACY_SOURCE) continue;
      out.push({
        path: (root && relativeTo(root, target)) || target.fsPath,
        line: diagnostic.range.start.line + 1,
        col: diagnostic.range.start.character + 1,
        severity: severityWord(diagnostic.severity),
        source: diagnostic.source ?? 'go',
        message: diagnostic.message,
      });
    }
  }
  return out;
}

/** `path:line:col: message` — the shape every Go tool already prints. */
export function formatGoDiagnostic(entry: GoDiagnosticLine): string {
  return `${entry.path}:${entry.line}:${entry.col}: ${entry.message} [${entry.source}]`;
}

function severityWord(severity: vscode.DiagnosticSeverity): GoDiagnosticLine['severity'] {
  switch (severity) {
    case vscode.DiagnosticSeverity.Error:
      return 'error';
    case vscode.DiagnosticSeverity.Warning:
      return 'warning';
    case vscode.DiagnosticSeverity.Information:
      return 'info';
    default:
      return 'hint';
  }
}

// ── citations ───────────────────────────────────────────────────────────────

interface ReferenceDoc {
  uri: vscode.Uri;
  headings: { title: string; line: number }[];
}

interface ResolvedCitation {
  location: vscode.Location;
  /** The citation as gotools wrote it — the authority, quoted, not paraphrased. */
  label: string;
  /** The same place as a link target, with a best-effort line fragment. */
  link: vscode.Uri;
}

/**
 * `skill.md §Repository Pattern; SOP.md §Handler Pattern` → a location.
 *
 * `skill.md` wins when a rule cites both, which is what plan §11.1 asks for.
 * A citation naming a section that no longer exists resolves to the top of the
 * document rather than to nothing: the document is still the authority, and a
 * renamed heading is exactly what `gotools doc-check` exists to catch.
 */
function resolveCitation(
  citation: string | undefined,
  docs: Map<string, ReferenceDoc>,
): ResolvedCitation | undefined {
  if (!citation || !docs.size) return undefined;

  const parts = [...citation.matchAll(/([A-Za-z0-9_.-]+\.md)\s*§\s*([^;]+)/g)].map((m) => ({
    doc: m[1].toLowerCase(),
    section: m[2],
  }));
  const chosen = parts.find((p) => p.doc === 'skill.md' && docs.has(p.doc))
    ?? parts.find((p) => docs.has(p.doc));
  if (!chosen) return undefined;

  const doc = docs.get(chosen.doc);
  if (!doc) return undefined;

  const wanted = normaliseHeading(chosen.section);
  const heading = doc.headings.find((h) => normaliseHeading(h.title) === wanted)
    ?? doc.headings.find((h) => normaliseHeading(h.title).startsWith(wanted));
  const line = heading?.line ?? 0;
  const position = new vscode.Position(line, 0);

  return {
    location: new vscode.Location(doc.uri, position),
    label: citation,
    // `#L<n>` is the editor's own line fragment. If an opener ignores it the
    // document still opens, which is the whole reason it is best-effort.
    link: doc.uri.with({ fragment: `L${line + 1}` }),
  };
}

/**
 * `[handler].go (step 5, no gin.Context)` → `[handler].go`.
 * Parentheses first, then the comma, so a comma inside the parenthetical does
 * not truncate the heading before it.
 */
function normaliseHeading(title: string): string {
  return title
    .replace(/\(.*$/, '')
    .replace(/,.*$/, '')
    .replace(/[#§]/g, '')
    .trim()
    .toLowerCase();
}

/**
 * ATX headings, skipping fenced blocks — mirroring `kb.LoadDoc`. skill.md's
 * shell examples are full of `# Initialize new module` comments, and counting
 * those as headings puts dozens of phantom sections in the index.
 */
function headingsOf(text: string): { title: string; line: number }[] {
  const headings: { title: string; line: number }[] = [];
  let fenced = false;
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^\s*(```|~~~)/.test(line)) {
      fenced = !fenced;
      continue;
    }
    if (fenced) continue;
    const match = line.match(/^(#{1,6})\s+(.+?)\s*$/);
    if (match) headings.push({ title: match[2], line: i });
  }
  return headings;
}

// ── small helpers ───────────────────────────────────────────────────────────

interface TargetArg {
  uri: vscode.Uri;
  violation: LintViolation;
}

function isTargetArg(value: unknown): value is TargetArg {
  const candidate = value as TargetArg | undefined;
  return !!candidate && candidate.uri instanceof vscode.Uri && !!candidate.violation?.rule;
}

function isWireArg(value: unknown): value is { ctor: string; kind: 'handler' | 'repo' } {
  const candidate = value as { ctor?: unknown; kind?: unknown } | undefined;
  return (
    !!candidate &&
    typeof candidate.ctor === 'string' &&
    (candidate.kind === 'handler' || candidate.kind === 'repo')
  );
}

function isUriArg(value: unknown): value is vscode.Uri {
  return value instanceof vscode.Uri;
}

function isRangeArg(value: unknown): value is vscode.Range {
  return value instanceof vscode.Range;
}

function severityOf(severity: string): vscode.DiagnosticSeverity {
  switch (severity) {
    case 'error':
      return vscode.DiagnosticSeverity.Error;
    case 'warning':
      return vscode.DiagnosticSeverity.Warning;
    default:
      // Additive by habit: a severity this build has not heard of still renders
      // as a row rather than disappearing or throwing.
      return vscode.DiagnosticSeverity.Information;
  }
}

function findingKey(source: string, uri: vscode.Uri, line: number, rule: string): string {
  return `${source}\u0000${uri.toString()}\u0000${line}\u0000${rule}`;
}

function normalisePath(path: string): string {
  return path.replace(/\\/g, '/').replace(/^\.\//, '');
}

function relativeTo(root: vscode.Uri, uri: vscode.Uri): string | undefined {
  const base = root.path.endsWith('/') ? root.path : `${root.path}/`;
  // Case-insensitive on Windows, where the same file arrives as both `d:/…` and
  // `D:/…` depending on which API produced the Uri.
  const insensitive = process.platform !== 'linux';
  const left = insensitive ? uri.path.toLowerCase() : uri.path;
  const right = insensitive ? base.toLowerCase() : base;
  if (!left.startsWith(right)) return undefined;
  return uri.path.slice(base.length);
}

function layerKind(relative: string | undefined): 'handler' | 'repo' | undefined {
  if (!relative) return undefined;
  const path = normalisePath(relative);
  if (path.startsWith('repo/')) return 'repo';
  if (path.startsWith('handler/') && !path.startsWith('handler/response/')) return 'handler';
  return undefined;
}

function isHandlerPath(path: string): boolean {
  return /(^|\/)handler\/(?!response\/)[^/]+\.go$/.test(path);
}

function clampLine(document: vscode.TextDocument, line: number): number {
  return Math.max(0, Math.min(line, document.lineCount - 1));
}

/**
 * A range for a finding. `gotools` reports a start and no end, so the end comes
 * from the open document when there is one — a squiggle under the identifier —
 * and from the clamped sentinel when there is not.
 */
function rangeFor(uri: vscode.Uri, line: number, col?: number): vscode.Range {
  const zeroLine = Math.max(0, line - 1);
  const zeroCol = Math.max(0, (col ?? 0) - 1);

  const document = vscode.workspace.textDocuments.find(
    (d) => d.uri.toString() === uri.toString(),
  );
  if (document && zeroLine < document.lineCount) {
    const text = document.lineAt(zeroLine);
    if (col && col > 0) {
      const word = document.getWordRangeAtPosition(new vscode.Position(zeroLine, zeroCol));
      if (word) return word;
      return new vscode.Range(zeroLine, zeroCol, zeroLine, text.range.end.character);
    }
    return new vscode.Range(zeroLine, text.firstNonWhitespaceCharacterIndex, zeroLine, text.range.end.character);
  }
  return new vscode.Range(zeroLine, zeroCol, zeroLine, CLAMPED_EOL);
}

/** The constructor name declared at (or just after) a reported line. */
async function constructorAt(uri: vscode.Uri, line: number): Promise<string | undefined> {
  try {
    const text = new TextDecoder().decode(await vscode.workspace.fs.readFile(uri));
    const lines = text.split(/\r?\n/);
    // The rule points at the declaration; a doc comment above it can put the
    // reported position a line or two off, so a small window is searched.
    for (let i = Math.max(0, line - 3); i < Math.min(lines.length, line + 2); i += 1) {
      const match = lines[i].match(FX_CTOR);
      if (match) return match[1];
    }
  } catch {
    /* the file moved between the lint and the pick */
  }
  return undefined;
}

/**
 * The sidecar the extension will actually launch: the setting, then the
 * checksum-verified bundled build, then the bare name for a developer with one
 * on PATH — the order `dakcoder.gotoolsPath` documents.
 *
 * Free rather than a method because `runtime.ts` must hand the same answer to
 * the Python runtime, which can compose neither the filename (reproducing
 * node's `process.arch` from Python would be the per-platform mapping table
 * `scripts/build-gotools.mjs` exists to avoid, Part B §4.5) nor the trust
 * decision behind it. Sharing the *resolution* rather than the *name* is the
 * point: the runtime is the consumer that lets `gotools` write in the
 * workspace, so it is the last one that should reach the binary around the
 * manifest check.
 */
export async function resolveGotools(
  extensionUri: vscode.Uri,
  log: vscode.LogOutputChannel,
): Promise<string> {
  const configured = vscode.workspace
    .getConfiguration('dakcoder')
    .get<string>('gotoolsPath')
    ?.trim();
  if (configured) return configured;

  const name = `gotools-${process.platform}-${process.arch}${
    process.platform === 'win32' ? '.exe' : ''
  }`;
  const bundled = vscode.Uri.joinPath(extensionUri, 'bin', name);
  if (await exists(bundled)) {
    if (await checksumOk(extensionUri, log, bundled, name)) {
      ensureExecutable(bundled.fsPath, log);
      return bundled.fsPath;
    }
    // Refused, not warned past: a sidecar that does not match its manifest is
    // either a corrupted download or a substituted binary, and this one writes
    // files in the workspace.
    throw new Error(`checksum mismatch for ${name}`);
  }
  return process.platform === 'win32' ? 'gotools.exe' : 'gotools';
}

/**
 * Restore the execute bit the `.vsix` does not carry.
 *
 * A zip written on Windows stores 0666 for every entry, and VS Code extracts
 * those modes verbatim. So on linux and macOS the sidecar arrives present,
 * checksum-valid and unrunnable, and the only symptom is EACCES from a spawn.
 *
 * Best-effort: a read-only install directory is not a reason to refuse a binary
 * that may already be executable.
 */
function ensureExecutable(file: string, log: vscode.LogOutputChannel): void {
  if (process.platform === 'win32') return;
  try {
    if ((statSync(file).mode & 0o111) !== 0) return;
    chmodSync(file, 0o755);
    log.info(`marked ${file} executable; the .vsix does not carry the bit`);
  } catch (err) {
    log.warn(`could not mark ${file} executable: ${String(err)}`);
  }
}

async function checksumOk(
  extensionUri: vscode.Uri,
  log: vscode.LogOutputChannel,
  binary: vscode.Uri,
  name: string,
): Promise<boolean> {
  const manifest = vscode.Uri.joinPath(extensionUri, 'bin', 'gotools.sha256');
  let expected: string | undefined;
  try {
    const text = new TextDecoder().decode(await vscode.workspace.fs.readFile(manifest));
    for (const line of text.split(/\r?\n/)) {
      const match = line.match(/^([0-9a-f]{64})\s+\*?(.+?)\s*$/i);
      if (match && match[2].endsWith(name)) expected = match[1].toLowerCase();
    }
  } catch {
    // No manifest is a packaging defect, not a mismatch. Refusing here would
    // break every development build, which is the one place it would be seen.
    log.warn('bin/gotools.sha256 is missing; the sidecar checksum was not verified');
    return true;
  }
  if (!expected) {
    log.warn(`bin/gotools.sha256 does not list ${name}; the checksum was not verified`);
    return true;
  }
  const bytes = await vscode.workspace.fs.readFile(binary);
  const actual = createHash('sha256').update(bytes).digest('hex');
  if (actual === expected) return true;
  log.error(`${name}: expected sha256 ${expected}, got ${actual}`);
  return false;
}

async function exists(uri: vscode.Uri): Promise<boolean> {
  try {
    await vscode.workspace.fs.stat(uri);
    return true;
  } catch {
    return false;
  }
}

function firstLine(text: string): string {
  return text.trim().split(/\r?\n/)[0] ?? '';
}

interface Captured {
  code: number;
  stdout: string;
  stderr: string;
  timedOut: boolean;
  cancelled: boolean;
}

/**
 * Spawn and capture, keeping the exit code.
 *
 * `runtime.run` rejects on a non-zero exit and discards stdout with it, which is
 * exactly wrong here: `gotools lint` exits 1 *and prints the findings*, and the
 * findings are the whole point. Distinguishing 1 from 2 is the linter convention
 * the tool documents, and it needs both the code and the output.
 */
function capture(
  command: string,
  args: string[],
  options: { cwd?: string; timeoutMs: number; token?: vscode.CancellationToken },
): Promise<Captured> {
  return new Promise((resolve) => {
    let stdout = '';
    let stderr = '';
    let timedOut = false;
    let cancelled = false;

    const child = spawn(command, args, {
      cwd: options.cwd,
      windowsHide: true,
      shell: false,
    });

    const timer = setTimeout(() => {
      timedOut = true;
      child.kill();
    }, options.timeoutMs);

    const subscription = options.token?.onCancellationRequested(() => {
      cancelled = true;
      child.kill();
    });

    const finish = (code: number) => {
      clearTimeout(timer);
      subscription?.dispose();
      resolve({ code, stdout, stderr, timedOut, cancelled });
    };

    child.stdout?.on('data', (chunk: Buffer) => (stdout += chunk.toString()));
    child.stderr?.on('data', (chunk: Buffer) => (stderr += chunk.toString()));
    // ENOENT included: a missing binary is exit 2 in the tool's own vocabulary,
    // so every caller handles it through the path it already has.
    child.on('error', (err) => {
      stderr += err.message;
      finish(2);
    });
    child.on('close', (code) => finish(code ?? 2));
  });
}
