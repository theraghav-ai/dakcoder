/**
 * Two surfaces that both exist to make a mandatory confirmation *reviewable*:
 * the scaffold wizard, and the migration plan viewer.
 *
 * **The wizard's output is the scaffolder's input, not a prompt.** The sidecar
 * (`gotools/internal/spec`) consumes a closed-shape `Resource` and renders code
 * from `text/template`; the model's only job is to produce that struct. Asking
 * for it in a QuickPick removes the model from the one step where a guess costs
 * a compile — the spike produced `decimal.Decimal` and `PpoNumber` for a
 * pension resource, and both are mechanically preventable here. Reviewing a
 * field table also beats re-reading prose to check whether `Amount` ended up
 * `float64` or `string`.
 *
 * **Inference here is a preview, never the authority.** `gotools/internal/naming`
 * owns Pascal/Snake/Kebab/Plural and `spec.Normalise` owns validation; both run
 * again server-side and their answer wins. There is no loopback route that
 * normalises a spec (`/v1/tasks` takes `{task, mode, acceptance}` and nothing
 * else), so a live preview has to infer locally — and the way to keep a
 * duplicated rule honest is to *show* its result in an editable field, so a
 * divergence appears as a value the developer can correct rather than as a
 * silent disagreement two layers down.
 *
 * **The wizard cannot hand a struct across the wire.** `POST /v1/tasks` has no
 * field for a resource spec. The spec therefore travels as a fenced JSON block
 * inside `task`, which the Scaffolder mode reads. That is a gap in the wire
 * contract, not a style choice; see `ScaffoldRequest`.
 *
 * **`plan.md` has no wire type at all.** It is a file the runtime writes for
 * itself (Part A §12.2), so nothing in `protocol.ts` describes it. The parser
 * below is deliberately tolerant: it recognises a table or a task list, keeps
 * every other byte of the document untouched, and writes back only the lines it
 * understood. An unrecognised column, status or classification is carried
 * through verbatim rather than dropped — the same additive-only reflex C2 asks
 * for on the event stream, applied to a file.
 *
 * **Reordering has a keyboard path.** Drag-and-drop is the discoverable way to
 * reorder the plan and it is not an accessible one, so *Move Up* / *Move Down*
 * do the same job from the keyboard and are the primary implementation — the
 * drop handler calls into them.
 */

import { randomBytes } from 'node:crypto';
import * as vscode from 'vscode';

// ── the resource spec ───────────────────────────────────────────────────────

/**
 * The JSON shape of `spec.Resource`, field for field.
 *
 * Not imported from `./protocol`: this is not on the event stream or any REST
 * body — it is the sidecar's tool argument, and `protocol.ts` is specifically
 * the wire contract between the extension and the runtime. Keeping it here
 * keeps that file honest about its scope.
 *
 * `Filter.type` and `Filter.db` are `json:"-"` on the Go side — normalisation
 * copies them from the field being filtered, so a caller that sets them can
 * only disagree with the field. They are absent here for the same reason.
 */
export interface ResourceField {
  go: string;
  json: string;
  db: string;
  type: string;
  validate: string;
  sql: string;
}

export interface ResourceFilter {
  go: string;
  form: string;
}

export interface ResourceSpec {
  name: string;
  plural: string;
  table: string;
  route_base: string;
  fields: ResourceField[];
  operations: string[];
  list_filters: ResourceFilter[];
  paginate: boolean;
}

/** One entry in the review pane's file list. */
export interface PlannedFile {
  path: string;
  action: 'create' | 'modify';
  note: string;
}

export interface ScaffoldRequest {
  spec: ResourceSpec;
  files: PlannedFile[];
  /**
   * The task text, with the spec fenced inside it.
   *
   * The runtime has no route that accepts a structured spec, so this is the
   * only channel. It is assembled here rather than by the caller so the JSON is
   * emitted exactly once, from the same object the review pane displayed.
   */
  task: string;
  acceptance: string[];
}

/**
 * The closed type set, mirroring `spec.goTypes`.
 *
 * Closed on purpose: an open set is what lets `decimal.Decimal` through, and a
 * repository that imports a module the project does not have turns one mistake
 * into a failure report from every verification stage at once.
 */
const GO_TYPES: readonly { readonly name: string; readonly sql: string }[] = [
  { name: 'string', sql: 'varchar(255) NOT NULL' },
  { name: 'int', sql: 'int4 NOT NULL' },
  { name: 'int64', sql: 'int8 NOT NULL' },
  { name: 'float64', sql: 'numeric(12, 2) NOT NULL' },
  { name: 'bool', sql: 'bool NOT NULL DEFAULT false' },
  { name: 'time.Time', sql: 'timestamp NOT NULL' },
];

/** A `bool` field's zero value is indistinguishable from "not supplied". */
const ALWAYS_SET = new Set(['bool']);

/** Spellings `spec.Normalise` silently corrects. Corrected here too, and said out loud. */
const TYPE_ALIASES: Readonly<Record<string, string>> = {
  integer: 'int', int32: 'int', uint: 'int64', uint64: 'int64',
  float: 'float64', float32: 'float64', double: 'float64',
  number: 'float64', decimal: 'float64',
  boolean: 'bool', text: 'string', varchar: 'string', str: 'string',
  date: 'time.Time', datetime: 'time.Time', timestamp: 'time.Time', time: 'time.Time',
};

/**
 * Types the template forbids, each naming its substitute.
 *
 * "Not allowed" without an alternative costs a turn — or, here, a trip to the
 * documentation — so the refusal always says what to use instead.
 */
const REJECTED_TYPES: Readonly<Record<string, string>> = {
  'decimal.Decimal': 'float64 (or string when exact decimal arithmetic is required)',
  shopspring: 'float64',
  'uuid.UUID': 'string',
  'json.RawMessage': 'string',
  'map[string]any': 'string (store JSON as text, or model it as a related resource)',
  any: 'an explicit type',
  'interface{}': 'an explicit type',
  '[]byte': 'string (file payloads use *multipart.FileHeader — see SOP.md §Handler with file upload)',
  'null.String': 'string',
  'sql.NullString': 'string',
  'pgtype.Timestamp': 'time.Time',
};

const OPERATIONS = ['create', 'list', 'get', 'update', 'delete'] as const;

/** Added by the scaffolder to every domain model. */
const RESERVED_FIELDS = new Set(['ID', 'CreatedAt', 'UpdatedAt']);

/**
 * Import aliases and locals the generated files use. A resource whose camel
 * form lands on one of these shadows an identifier inside its own methods,
 * which compiles in some shapes and fails confusingly in others.
 */
const RESERVED_IDENTS = new Set([
  'resp', 'repo', 'port', 'log', 'domain', 'dblib', 'config', 'sq', 'pgx',
  'handler', 'response', 'bootstrap', 'time', 'context', 'serverHandler',
  'serverRoute', 'fx', 'ctx', 'sctx', 'req', 'err', 'base', 'data', 'md',
  'ins', 'del', 'svc', 'commandTag', 'skip', 'limit', 'id', 'res',
]);

const GO_KEYWORDS = new Set([
  'break', 'case', 'chan', 'const', 'continue', 'default', 'defer', 'else',
  'fallthrough', 'for', 'func', 'go', 'goto', 'if', 'import', 'interface',
  'map', 'package', 'range', 'return', 'select', 'struct', 'switch', 'type', 'var',
]);

// The character classes are `spec.go`'s, not approximations of it. `tagRe` and
// `sqlTypeRe` are the injection guards: every value here is interpolated into
// generated Go or into a .sql file a developer runs as a database superuser, so
// a backtick in a validate tag or a semicolon in a column type is an injection,
// not a typo. Server-side validation is the real gate; this one exists so the
// refusal arrives while the developer is still looking at the field.
const IDENT_RE = /^[A-Za-z][A-Za-z0-9]*$/;
const TAG_RE = /^[A-Za-z0-9_,=|.:+ '\-]*$/;
const SNAKE_RE = /^[a-z][a-z0-9_]*$/;
const SQL_TYPE_RE = /^[A-Za-z0-9_ ,()'.\-]+$/;
const ROUTE_RE = /^\/[a-z0-9]+(?:[-/][a-z0-9]+)*$/;

// ── naming, mirroring gotools/internal/naming ───────────────────────────────

/**
 * The IT 2.0 half of this list is why it is duplicated rather than skipped: the
 * spike emitted `PpoNumber` for a PPO number, which appears in every DOP
 * service. A preview that shows `ppo_number` only after a round trip is a
 * preview that arrives too late to be worth having.
 */
const INITIALISMS = new Set([
  'ACL', 'API', 'ASCII', 'CPU', 'CSS', 'DNS', 'EOF', 'GUID', 'HTML', 'HTTP',
  'HTTPS', 'ID', 'IP', 'JSON', 'LHS', 'QPS', 'RAM', 'RHS', 'RPC', 'SLA',
  'SMTP', 'SQL', 'SSH', 'TCP', 'TLS', 'TTL', 'UDP', 'UI', 'UID', 'UUID',
  'URI', 'URL', 'UTF8', 'VM', 'XML', 'XMPP', 'XSRF', 'XSS',
  'PPO', 'HOA', 'DOP', 'OTP', 'PIN', 'SMS', 'PAN', 'IFSC', 'GST', 'KYC',
  'MICR', 'NEFT', 'RTGS', 'UPI', 'DOB', 'CSI', 'PLI', 'RPLI', 'GDS', 'NPS',
]);

const IRREGULAR_PLURALS: Readonly<Record<string, string>> = {
  person: 'people', child: 'children', man: 'men', woman: 'women',
  datum: 'data', index: 'indexes', status: 'statuses',
  foot: 'feet', tooth: 'teeth', mouse: 'mice', criterion: 'criteria',
};

const UNCOUNTABLE = new Set(['data', 'info', 'equipment', 'staff', 'news', 'series', 'species']);

const isUpper = (c: string): boolean => c >= 'A' && c <= 'Z';
const isLower = (c: string): boolean => c >= 'a' && c <= 'z';
const isDigit = (c: string): boolean => c >= '0' && c <= '9';

/**
 * Split an identifier into words, accepting Pascal, camel, snake, kebab and any
 * mixture. A run of capitals is one word up to the last capital that starts a
 * lower-cased word, which is what keeps `PPONumber` as PPO|Number.
 */
function words(source: string): string[] {
  const out: string[] = [];
  let current = '';
  const flush = (): void => {
    if (current) out.push(current);
    current = '';
  };
  for (let i = 0; i < source.length; i++) {
    const c = source[i];
    if (c === '_' || c === '-' || c === ' ' || c === '.' || c === '/') {
      flush();
      continue;
    }
    if (isDigit(c)) {
      current += c;
      continue;
    }
    if (isUpper(c)) {
      const prev = i > 0 ? source[i - 1] : '';
      const next = i + 1 < source.length ? source[i + 1] : '';
      if (isLower(prev) || isDigit(prev) || (isUpper(prev) && isLower(next))) flush();
      current += c;
      continue;
    }
    current += c;
  }
  flush();
  return splitInitialismRuns(out);
}

/**
 * Break an all-capitals word that is a concatenation of known initialisms.
 * Without it `Snake("HTTPURL")` is `httpurl`, and a db tag that no longer maps
 * back to its field name is a silent scan failure rather than a compile error.
 * Only a *complete* decomposition counts: guessing is worse than leaving a
 * plausible name alone.
 */
function splitInitialismRuns(input: string[]): string[] {
  const out: string[] = [];
  for (const word of input) {
    if (word.length <= 3 || word !== word.toUpperCase() || !/^[A-Za-z]+$/.test(word) || word.length > 32) {
      out.push(word);
      continue;
    }
    const parts = decompose(word);
    if (parts && parts.length >= 2) out.push(...parts);
    else out.push(word);
  }
  return out;
}

function decompose(word: string): string[] | undefined {
  if (word === '') return [];
  // Longest head first, then backtrack, so UIDAPI is UID+API rather than
  // stalling on UI.
  for (let n = word.length; n >= 2; n--) {
    const head = word.slice(0, n);
    if (!INITIALISMS.has(head)) continue;
    const rest = decompose(word.slice(n));
    if (rest) return [head, ...rest];
  }
  return undefined;
}

function capitalise(word: string): string {
  if (!word) return '';
  if (INITIALISMS.has(word.toUpperCase())) return word.toUpperCase();
  return word[0].toUpperCase() + word.slice(1).toLowerCase();
}

function pascal(source: string): string {
  return words(source).map(capitalise).join('');
}

function camel(source: string): string {
  const parts = words(source);
  if (!parts.length) return '';
  const head = parts[0].toLowerCase();
  return head + parts.slice(1).map(capitalise).join('');
}

function snake(source: string): string {
  return words(source).map((w) => w.toLowerCase()).join('_');
}

function kebab(source: string): string {
  return snake(source).replace(/_/g, '-');
}

function pluralise(source: string): string {
  const parts = words(source);
  if (!parts.length) return '';
  const last = parts[parts.length - 1];
  parts[parts.length - 1] = restoreCase(last, pluraliseWord(last.toLowerCase()));
  if (source.includes('_')) return parts.join('_');
  if (source.includes('-')) return parts.join('-');
  return parts.join('');
}

function pluraliseWord(word: string): string {
  if (!word || UNCOUNTABLE.has(word)) return word;
  const irregular = IRREGULAR_PLURALS[word];
  if (irregular) return irregular;
  if (/(?:s|x|z|ch|sh)$/.test(word)) return `${word}es`;
  if (word.length > 1 && word.endsWith('y') && !'aeiou'.includes(word[word.length - 2])) {
    return `${word.slice(0, -1)}ies`;
  }
  return `${word}s`;
}

function restoreCase(original: string, inflected: string): string {
  if (!original) return inflected;
  if (original.length > 1 && original === original.toUpperCase()) return inflected.toUpperCase();
  if (isUpper(original[0])) return inflected[0].toUpperCase() + inflected.slice(1);
  return inflected;
}

// ── multi-step plumbing ─────────────────────────────────────────────────────

type StepOutcome<T> = { kind: 'value'; value: T } | { kind: 'back' } | { kind: 'cancel' };

const back = <T>(): StepOutcome<T> => ({ kind: 'back' });
const cancel = <T>(): StepOutcome<T> => ({ kind: 'cancel' });

interface StepChrome {
  title: string;
  step: number;
  totalSteps: number;
  /** False on the first step, where Back would mean "cancel" and should not exist. */
  canGoBack: boolean;
}

type Validation = string | vscode.InputBoxValidationMessage | undefined;

/**
 * `ignoreFocusOut` is on throughout the wizard: half-way through describing six
 * fields, a click on the editor to check a column name must not throw the whole
 * spec away.
 */
function inputStep(
  chrome: StepChrome,
  options: {
    prompt: string;
    value?: string;
    placeholder?: string;
    validate?: (value: string) => Validation;
  },
): Promise<StepOutcome<string>> {
  return new Promise((resolve) => {
    const box = vscode.window.createInputBox();
    box.title = chrome.title;
    box.step = chrome.step;
    box.totalSteps = chrome.totalSteps;
    box.prompt = options.prompt;
    box.value = options.value ?? '';
    box.placeholder = options.placeholder ?? '';
    box.ignoreFocusOut = true;
    box.buttons = chrome.canGoBack ? [vscode.QuickInputButtons.Back] : [];

    let settled = false;
    const finish = (outcome: StepOutcome<string>): void => {
      if (settled) return;
      settled = true;
      resolve(outcome);
      box.hide();
    };

    box.onDidTriggerButton((button) => {
      if (button === vscode.QuickInputButtons.Back) finish(back());
    });
    box.onDidChangeValue((value) => {
      box.validationMessage = options.validate?.(value.trim());
    });
    box.onDidAccept(() => {
      const value = box.value.trim();
      const message = options.validate?.(value);
      // Only Error blocks. Warning and Info are how an accepted-but-corrected
      // value (`decimal` becoming `float64`) is announced without refusing it.
      if (blocking(message)) {
        box.validationMessage = message;
        return;
      }
      finish({ kind: 'value', value });
    });
    box.onDidHide(() => {
      finish(cancel());
      box.dispose();
    });
    box.show();
  });
}

function blocking(message: Validation): boolean {
  if (message === undefined) return false;
  if (typeof message === 'string') return message.length > 0;
  return message.severity === vscode.InputBoxValidationSeverity.Error;
}

interface Choice<T> extends vscode.QuickPickItem {
  value: T;
}

function pickStep<T>(
  chrome: StepChrome,
  options: {
    placeholder: string;
    items: (Choice<T> | vscode.QuickPickItem)[];
    canSelectMany?: boolean;
    /** Compared by `value`, so callers do not have to hold on to item identity. */
    selected?: readonly T[];
  },
): Promise<StepOutcome<T[]>> {
  return new Promise((resolve) => {
    const picker = vscode.window.createQuickPick<Choice<T> | vscode.QuickPickItem>();
    picker.title = chrome.title;
    picker.step = chrome.step;
    picker.totalSteps = chrome.totalSteps;
    picker.placeholder = options.placeholder;
    picker.ignoreFocusOut = true;
    picker.canSelectMany = options.canSelectMany ?? false;
    picker.items = options.items;
    picker.buttons = chrome.canGoBack ? [vscode.QuickInputButtons.Back] : [];
    if (options.selected) {
      const wanted = new Set<unknown>(options.selected);
      picker.selectedItems = options.items.filter(
        (item) => 'value' in item && wanted.has((item as Choice<T>).value),
      );
    }

    let settled = false;
    const finish = (outcome: StepOutcome<T[]>): void => {
      if (settled) return;
      settled = true;
      resolve(outcome);
      picker.hide();
    };

    picker.onDidTriggerButton((button) => {
      if (button === vscode.QuickInputButtons.Back) finish(back());
    });
    picker.onDidAccept(() => {
      const chosen = picker.selectedItems.filter((item): item is Choice<T> => 'value' in item);
      finish({ kind: 'value', value: chosen.map((item) => item.value) });
    });
    picker.onDidHide(() => {
      finish(cancel());
      picker.dispose();
    });
    picker.show();
  });
}

// ── the scaffold wizard ─────────────────────────────────────────────────────

/** name · plural · table · route · fields · operations · filters. Review is separate. */
const TOTAL_STEPS = 7;

interface Draft {
  name: string;
  plural: string;
  table: string;
  routeBase: string;
  fields: ResourceField[];
  operations: string[];
  filters: ResourceFilter[];
  paginate: boolean;
  /** Set once the developer edits an inferred value, so re-inference stops overwriting it. */
  pinned: Set<'plural' | 'table' | 'routeBase'>;
}

function emptyDraft(): Draft {
  return {
    name: '',
    plural: '',
    table: '',
    routeBase: '',
    fields: [],
    operations: [...OPERATIONS],
    filters: [],
    paginate: false,
    pinned: new Set(),
  };
}

/** What the fields picker can come back with. `noop` re-opens it unchanged. */
type FieldAction =
  | { kind: 'add' }
  | { kind: 'done' }
  | { kind: 'field'; index: number }
  | { kind: 'noop' };

function draftToSpec(draft: Draft): ResourceSpec {
  return {
    name: draft.name,
    plural: draft.plural,
    table: draft.table,
    route_base: draft.routeBase,
    fields: draft.fields,
    operations: draft.operations,
    list_filters: draft.filters,
    paginate: draft.paginate,
  };
}

export class ScaffoldWizard {
  constructor(private readonly deps: WizardDeps) {}

  async run(): Promise<void> {
    const draft = emptyDraft();
    let index = 0;
    let direction = 1;

    // Indexed rather than recursive: Back is `index--`, a skipped step is
    // `index += direction`, and both read as what they are.
    while (index <= TOTAL_STEPS) {
      if (index === 6 && !draft.operations.includes('list')) {
        // Filters and pagination only exist on a list route.
        index += direction;
        if (index < 0) return;
        continue;
      }

      const outcome = await this.step(index, draft);
      if (outcome.kind === 'cancel') return;
      if (outcome.kind === 'back') {
        direction = -1;
        index -= 1;
        if (index < 0) return;
        continue;
      }
      direction = 1;
      index += 1;
    }
  }

  private async step(index: number, draft: Draft): Promise<StepOutcome<void>> {
    switch (index) {
      case 0:
        return this.nameStep(draft);
      case 1:
        return this.pluralStep(draft);
      case 2:
        return this.tableStep(draft);
      case 3:
        return this.routeStep(draft);
      case 4:
        return this.fieldsStep(draft);
      case 5:
        return this.operationsStep(draft);
      case 6:
        return this.filtersStep(draft);
      default:
        return this.reviewStep(draft);
    }
  }

  private chrome(step: number): StepChrome {
    return {
      title: vscode.l10n.t('Scaffold a resource'),
      step: step + 1,
      totalSteps: TOTAL_STEPS,
      canGoBack: step > 0,
    };
  }

  private async nameStep(draft: Draft): Promise<StepOutcome<void>> {
    const outcome = await inputStep(this.chrome(0), {
      prompt: vscode.l10n.t('The singular resource name, in PascalCase.'),
      value: draft.name,
      placeholder: vscode.l10n.t('Pension'),
      validate: (value) => validateName(value),
    });
    if (outcome.kind !== 'value') return outcome;

    const name = pascal(outcome.value);
    // Re-infer only what the developer has not touched. Renaming Pension to
    // PensionClaim should follow through to the table and the route; it must not
    // undo a plural they corrected by hand two steps ago.
    if (name !== draft.name) {
      draft.name = name;
      if (!draft.pinned.has('plural')) draft.plural = pluralise(name);
      if (!draft.pinned.has('table')) draft.table = snake(draft.plural);
      if (!draft.pinned.has('routeBase')) draft.routeBase = `/${kebab(draft.plural)}`;
    }
    return { kind: 'value', value: undefined };
  }

  private async pluralStep(draft: Draft): Promise<StepOutcome<void>> {
    const inferred = pluralise(draft.name);
    const outcome = await inputStep(this.chrome(1), {
      prompt: vscode.l10n.t(
        'The plural, used for the table, the route and the list types. Inferred from the name — correct it if the inflection is wrong.',
      ),
      value: draft.plural || inferred,
      validate: (value) => validatePlural(value),
    });
    if (outcome.kind !== 'value') return outcome;

    const plural = pascal(outcome.value);
    if (plural !== inferred) draft.pinned.add('plural');
    if (plural !== draft.plural) {
      draft.plural = plural;
      if (!draft.pinned.has('table')) draft.table = snake(plural);
      if (!draft.pinned.has('routeBase')) draft.routeBase = `/${kebab(plural)}`;
    }
    return { kind: 'value', value: undefined };
  }

  private async tableStep(draft: Draft): Promise<StepOutcome<void>> {
    const inferred = snake(draft.plural);
    const outcome = await inputStep(this.chrome(2), {
      prompt: vscode.l10n.t('The Postgres table. Lower snake case.'),
      value: draft.table || inferred,
      validate: (value) => validateTable(value, inferred),
    });
    if (outcome.kind !== 'value') return outcome;
    draft.table = outcome.value.toLowerCase();
    if (draft.table !== inferred) draft.pinned.add('table');
    return { kind: 'value', value: undefined };
  }

  private async routeStep(draft: Draft): Promise<StepOutcome<void>> {
    const inferred = `/${kebab(draft.plural)}`;
    const outcome = await inputStep(this.chrome(3), {
      prompt: vscode.l10n.t('The route base, under the /v1 prefix.'),
      value: draft.routeBase || inferred,
      validate: (value) => validateRoute(value, inferred),
    });
    if (outcome.kind !== 'value') return outcome;
    draft.routeBase = `/${outcome.value.toLowerCase().replace(/^\/+|\/+$/g, '')}`;
    if (draft.routeBase !== inferred) draft.pinned.add('routeBase');
    return { kind: 'value', value: undefined };
  }

  /**
   * The repeatable step, and the one the whole wizard exists for.
   *
   * The picker *is* the live table: every field is a row showing its Go type,
   * json tag, validate tag and column type at once, which is the view that
   * makes "did Amount end up float64" a glance rather than a search.
   */
  private async fieldsStep(draft: Draft): Promise<StepOutcome<void>> {
    for (;;) {
      const action = await this.fieldsPicker(draft);
      if (action.kind !== 'value') return action;

      const chosen = action.value;
      if (chosen.kind === 'done') {
        if (!draft.fields.length) {
          // `spec.Normalise` rejects an empty field list, so accepting one here
          // would only move the refusal to a place with less context.
          void vscode.window.showWarningMessage(
            vscode.l10n.t(
              'A resource needs at least one field of its own. ID, CreatedAt and UpdatedAt are added by the scaffolder.',
            ),
          );
          continue;
        }
        return { kind: 'value', value: undefined };
      }
      if (chosen.kind === 'add') {
        const field = await this.editField(draft, undefined);
        if (field) draft.fields.push(field);
        continue;
      }
      if (chosen.kind === 'field') await this.fieldActions(draft, chosen.index);
    }
  }

  private fieldsPicker(draft: Draft): Promise<StepOutcome<FieldAction>> {
    type Action = FieldAction;
    const items: (Choice<Action> | vscode.QuickPickItem)[] = [];

    if (draft.fields.length) {
      items.push({ label: vscode.l10n.t('Fields'), kind: vscode.QuickPickItemKind.Separator });
      draft.fields.forEach((field, index) => {
        items.push({
          // The glyph is decoration; the type is spelled out in `description`,
          // so nothing here depends on an icon rendering.
          label: `$(symbol-field) ${field.go}`,
          description: field.type,
          detail: vscode.l10n.t(
            'json {0} · db {1} · validate {2} · {3}',
            field.json,
            field.db,
            field.validate,
            field.sql,
          ),
          value: { kind: 'field', index } as Action,
        });
      });
    }
    items.push({ label: '', kind: vscode.QuickPickItemKind.Separator });
    items.push({
      label: vscode.l10n.t('$(add) Add a field'),
      value: { kind: 'add' } as Action,
    });
    items.push({
      label: vscode.l10n.t('$(check) Done with fields'),
      description:
        draft.fields.length === 1
          ? vscode.l10n.t('1 field')
          : vscode.l10n.t('{0} fields', draft.fields.length),
      value: { kind: 'done' } as Action,
    });

    return pickStep<Action>(this.chrome(4), {
      placeholder: vscode.l10n.t('Select a field to edit it, or add another.'),
      items,
    }).then((outcome) => {
      if (outcome.kind !== 'value') return outcome;
      // Accepting with nothing highlighted — a filter that matches no row —
      // is not a decision, so the picker simply comes back rather than
      // stepping the wizard somewhere the developer did not ask to go.
      const first: Action = outcome.value[0] ?? { kind: 'noop' };
      return { kind: 'value' as const, value: first };
    });
  }

  /**
   * Edit / move / remove, reached by accepting a field row.
   *
   * Item buttons would be the compact idiom, but they have no reliable keyboard
   * path, and reordering fields changes the column order in the DDL — that is
   * not a mouse-only capability.
   */
  private async fieldActions(draft: Draft, index: number): Promise<void> {
    const field = draft.fields[index];
    if (!field) return;
    type Action = 'edit' | 'db' | 'up' | 'down' | 'remove';
    const items: Choice<Action>[] = [
      { label: vscode.l10n.t('$(edit) Edit {0}', field.go), value: 'edit' },
      {
        label: vscode.l10n.t('$(database) Override the db column'),
        description: field.db,
        value: 'db',
      },
    ];
    if (index > 0) items.push({ label: vscode.l10n.t('$(arrow-up) Move up'), value: 'up' });
    if (index < draft.fields.length - 1) {
      items.push({ label: vscode.l10n.t('$(arrow-down) Move down'), value: 'down' });
    }
    items.push({ label: vscode.l10n.t('$(trash) Remove {0}', field.go), value: 'remove' });

    const outcome = await pickStep<Action>(
      { ...this.chrome(4), canGoBack: true },
      { placeholder: vscode.l10n.t('What should happen to {0}?', field.go), items },
    );
    if (outcome.kind !== 'value') return;

    switch (outcome.value[0]) {
      case 'edit': {
        const edited = await this.editField(draft, field);
        if (edited) draft.fields[index] = edited;
        return;
      }
      case 'db': {
        const result = await inputStep(
          { ...this.chrome(4), canGoBack: true },
          {
            prompt: vscode.l10n.t('The Postgres column for {0}.', field.go),
            value: field.db,
            validate: (value) => validateSnake(value, snake(field.go)),
          },
        );
        if (result.kind === 'value') field.db = result.value.toLowerCase();
        return;
      }
      case 'up':
        [draft.fields[index - 1], draft.fields[index]] = [draft.fields[index], draft.fields[index - 1]];
        return;
      case 'down':
        [draft.fields[index], draft.fields[index + 1]] = [draft.fields[index + 1], draft.fields[index]];
        return;
      case 'remove':
        draft.fields.splice(index, 1);
        // A filter on a field that no longer exists cannot become a WHERE
        // clause; dropping it here beats a spec error after the review pane.
        draft.filters = draft.filters.filter((filter) => filter.go !== field.go);
        return;
      default:
        return;
    }
  }

  /** name → Go type → json tag → validate tag → SQL type, with Back throughout. */
  private async editField(
    draft: Draft,
    existing: ResourceField | undefined,
  ): Promise<ResourceField | undefined> {
    const field: ResourceField = existing
      ? { ...existing }
      : { go: '', json: '', db: '', type: '', validate: 'required', sql: '' };
    const title = existing
      ? vscode.l10n.t('Edit field {0}', existing.go)
      : vscode.l10n.t('Add a field');
    const chrome = (step: number): StepChrome => ({
      title,
      step,
      totalSteps: 5,
      canGoBack: step > 1,
    });

    let index = 0;
    while (index < 5) {
      let outcome: StepOutcome<unknown>;
      switch (index) {
        case 0: {
          const result = await inputStep(chrome(1), {
            prompt: vscode.l10n.t('The Go field name, in PascalCase.'),
            value: field.go,
            placeholder: vscode.l10n.t('PPONumber'),
            validate: (value) => validateFieldName(value, draft.fields, existing),
          });
          if (result.kind === 'value') {
            const go = pascal(result.value);
            // Tags follow the name only while they are still the inferred ones.
            const jsonWasInferred = !field.json || field.json === snake(field.go);
            const dbWasInferred = !field.db || field.db === snake(field.go);
            field.go = go;
            if (jsonWasInferred) field.json = snake(go);
            if (dbWasInferred) field.db = snake(go);
          }
          outcome = result;
          break;
        }
        case 1: {
          const result = await this.typeStep(chrome(2), field.type);
          if (result.kind === 'value') {
            const previousDefault = defaultSql(field.type);
            // Keep a hand-written column type; replace one we suggested.
            if (!field.sql || field.sql === previousDefault) field.sql = defaultSql(result.value);
            field.type = result.value;
          }
          outcome = result;
          break;
        }
        case 2: {
          const result = await inputStep(chrome(3), {
            prompt: vscode.l10n.t('The json tag, in lower snake case.'),
            value: field.json || snake(field.go),
            validate: (value) => validateSnake(value, snake(field.go)),
          });
          if (result.kind === 'value') field.json = result.value;
          outcome = result;
          break;
        }
        case 3: {
          const result = await inputStep(chrome(4), {
            prompt: vscode.l10n.t(
              'The go-playground validate tag. Empty means required — the scaffolder fills it in.',
            ),
            value: field.validate,
            placeholder: vscode.l10n.t('required, or oneof=active suspended closed'),
            validate: (value) => validateTag(value),
          });
          if (result.kind === 'value') field.validate = result.value || 'required';
          outcome = result;
          break;
        }
        default: {
          const fallback = defaultSql(field.type);
          const result = await inputStep(chrome(5), {
            prompt: vscode.l10n.t('The Postgres column type.'),
            value: field.sql || fallback,
            validate: (value) => validateSqlType(value, fallback),
          });
          if (result.kind === 'value') field.sql = result.value || fallback;
          outcome = result;
          break;
        }
      }

      if (outcome.kind === 'cancel') return undefined;
      if (outcome.kind === 'back') {
        index -= 1;
        if (index < 0) return undefined;
        continue;
      }
      index += 1;
    }
    return field;
  }

  /**
   * The six permitted types, plus an escape hatch that refuses by name.
   *
   * The escape hatch is the point: someone will type `decimal.Decimal`, and the
   * useful answer is "use float64, or string for exact decimal arithmetic" at
   * the moment they type it — not a spec error after seven files did not get
   * written.
   */
  private async typeStep(chrome: StepChrome, current: string): Promise<StepOutcome<string>> {
    const items: Choice<string>[] = GO_TYPES.map((type) => ({
      label: type.name,
      description: type.sql,
      detail: ALWAYS_SET.has(type.name)
        ? vscode.l10n.t(
            'A false bool cannot be told apart from an absent one, so a partial update always writes it, and it cannot be a list filter.',
          )
        : undefined,
      value: type.name,
    }));
    items.push({ label: vscode.l10n.t('$(edit) Another type…'), value: '' });

    const outcome = await pickStep<string>(chrome, {
      placeholder: vscode.l10n.t('The Go type. This set is closed — the template compiles against it.'),
      items,
      selected: current ? [current] : undefined,
    });
    if (outcome.kind !== 'value') return outcome;

    const picked = outcome.value[0];
    if (picked === undefined) return back();
    if (picked !== '') return { kind: 'value', value: picked };

    const typed = await inputStep(chrome, {
      prompt: vscode.l10n.t('A type name. It is resolved against the template’s allow-list.'),
      value: current,
      validate: (value) => validateGoType(value),
    });
    if (typed.kind !== 'value') return typed;
    return { kind: 'value', value: resolveGoType(typed.value) ?? typed.value };
  }

  private async operationsStep(draft: Draft): Promise<StepOutcome<void>> {
    const items: Choice<string>[] = OPERATIONS.map((op) => ({
      label: op,
      description: routeLabel(op, draft.routeBase),
      value: op,
    }));
    const outcome = await pickStep<string>(this.chrome(5), {
      placeholder: vscode.l10n.t('Which operations? Selecting none means all five.'),
      items,
      canSelectMany: true,
      selected: draft.operations,
    });
    if (outcome.kind !== 'value') return outcome;

    // Empty means all five, exactly as `spec.normaliseOperations` reads it.
    // Disagreeing with the server about the default would be a difference
    // nobody could see until the files appeared.
    const chosen = outcome.value.length ? outcome.value : [...OPERATIONS];
    draft.operations = OPERATIONS.filter((op) => chosen.includes(op));
    if (!draft.operations.includes('list')) {
      draft.filters = [];
      draft.paginate = false;
    }
    return { kind: 'value', value: undefined };
  }

  private async filtersStep(draft: Draft): Promise<StepOutcome<void>> {
    type Selection = { kind: 'paginate' } | { kind: 'filter'; field: ResourceField };
    const items: (Choice<Selection> | vscode.QuickPickItem)[] = [
      {
        label: vscode.l10n.t('Accept skip and limit'),
        description: vscode.l10n.t('pagination'),
        detail: vscode.l10n.t('Binds port.MetadataRequest on the list route.'),
        value: { kind: 'paginate' } as Selection,
      },
      { label: vscode.l10n.t('Filters'), kind: vscode.QuickPickItemKind.Separator },
    ];

    const eligible = draft.fields.filter((field) => !ALWAYS_SET.has(field.type));
    for (const field of eligible) {
      items.push({
        label: field.go,
        description: vscode.l10n.t('?{0}=', field.json),
        value: { kind: 'filter', field } as Selection,
      });
    }
    const excluded = draft.fields.filter((field) => ALWAYS_SET.has(field.type));
    if (excluded.length) {
      items.push({
        label: vscode.l10n.t('Not available as filters'),
        kind: vscode.QuickPickItemKind.Separator,
      });
      for (const field of excluded) {
        // Listed rather than hidden: "where did Active go?" is a question worth
        // answering in place, and the reason is not guessable.
        items.push({
          label: field.go,
          description: field.type,
          detail: vscode.l10n.t(
            'A {0} filter cannot tell false from absent, so it would silently constrain every request.',
            field.type,
          ),
        });
      }
    }

    // `pickStep` matches the previous selection by object identity, so the
    // pre-selected values have to be drawn out of `items` rather than rebuilt —
    // an equal-looking `{kind:'paginate'}` is a different object and would
    // silently come back unticked after a Back.
    const previously = items
      .filter((item): item is Choice<Selection> => 'value' in item)
      .map((item) => item.value)
      .filter((value) =>
        value.kind === 'paginate'
          ? draft.paginate
          : draft.filters.some((filter) => filter.go === value.field.go),
      );

    const outcome = await pickStep<Selection>(this.chrome(6), {
      placeholder: vscode.l10n.t('Optional query-string filters on the list route.'),
      items,
      canSelectMany: true,
      selected: previously,
    });
    if (outcome.kind !== 'value') return outcome;

    draft.paginate = outcome.value.some((value) => value.kind === 'paginate');
    draft.filters = outcome.value
      .filter((value): value is { kind: 'filter'; field: ResourceField } => value.kind === 'filter')
      .map((value) => ({ go: value.field.go, form: value.field.json }));
    return { kind: 'value', value: undefined };
  }

  private async reviewStep(draft: Draft): Promise<StepOutcome<void>> {
    const spec = draftToSpec(draft);
    const files = await plannedFiles(spec, this.deps.workspaceRoot());
    const decision = await showReview(this.deps, spec, files);
    if (decision === 'back') return back();
    if (decision === 'cancel') return cancel();

    const request: ScaffoldRequest = {
      spec,
      files,
      task: taskText(spec),
      acceptance: acceptanceCriteria(spec),
    };
    try {
      await this.deps.scaffold(request);
    } catch (err) {
      this.deps.log.error(`scaffold ${spec.name} could not be started: ${message(err)}`);
      void vscode.window.showErrorMessage(
        vscode.l10n.t('The scaffold could not be started: {0}', message(err)),
      );
    }
    return { kind: 'value', value: undefined };
  }
}

// ── spec validation, mirroring spec.Normalise ───────────────────────────────

function validateName(raw: string): Validation {
  const value = pascal(raw);
  if (!value) return vscode.l10n.t('A singular PascalCase name, for example Pension.');
  if (!IDENT_RE.test(value)) {
    return vscode.l10n.t('{0} is not a Go identifier. Use letters and digits, starting with a letter.', value);
  }
  if (GO_KEYWORDS.has(value.toLowerCase())) {
    return vscode.l10n.t('{0} collides with a Go keyword. Choose a different noun.', value);
  }
  if (RESERVED_IDENTS.has(camel(value))) {
    return vscode.l10n.t(
      '{0} becomes the local variable {1}, which shadows an identifier the generated code uses. Try {0}Record.',
      value,
      camel(value),
    );
  }
  return preview(vscode.l10n.t('Recorded as {0}.', value), value !== raw.trim());
}

function validatePlural(raw: string): Validation {
  const value = pascal(raw);
  if (!value) return vscode.l10n.t('A plural is required — it names the table, the route and the list types.');
  if (!IDENT_RE.test(value)) return vscode.l10n.t('{0} is not a Go identifier.', value);
  return preview(vscode.l10n.t('Recorded as {0}.', value), value !== raw.trim());
}

function validateTable(raw: string, inferred: string): Validation {
  const value = raw.toLowerCase();
  if (!value) return vscode.l10n.t('A table name is required.');
  if (!SNAKE_RE.test(value)) {
    return vscode.l10n.t('{0} is not lower snake case. Try {1}.', raw, inferred);
  }
  return undefined;
}

function validateRoute(raw: string, inferred: string): Validation {
  const value = `/${raw.toLowerCase().replace(/^\/+|\/+$/g, '')}`;
  if (value === '/') return vscode.l10n.t('A route base is required, for example {0}.', inferred);
  if (!ROUTE_RE.test(value)) {
    return vscode.l10n.t('{0} is not a valid path. Try {1}.', value, inferred);
  }
  return undefined;
}

function validateFieldName(
  raw: string,
  fields: readonly ResourceField[],
  editing: ResourceField | undefined,
): Validation {
  const value = pascal(raw);
  if (!value) return vscode.l10n.t('A PascalCase field name.');
  if (!IDENT_RE.test(value)) {
    return vscode.l10n.t('{0} is not a Go identifier. Use letters and digits, starting with a letter.', value);
  }
  if (RESERVED_FIELDS.has(value)) {
    return vscode.l10n.t(
      '{0} is added by the scaffolder — every domain model gets ID, CreatedAt and UpdatedAt.',
      value,
    );
  }
  if (fields.some((field) => field.go === value && field !== editing)) {
    return vscode.l10n.t('{0} is already declared on this resource.', value);
  }
  return preview(
    vscode.l10n.t('Recorded as {0}, json {1}.', value, snake(value)),
    value !== raw.trim(),
  );
}

function validateSnake(raw: string, inferred: string): Validation {
  if (!raw) return vscode.l10n.t('Required. The inferred value is {0}.', inferred);
  if (!SNAKE_RE.test(raw)) return vscode.l10n.t('{0} is not lower snake case. Try {1}.', raw, inferred);
  return undefined;
}

function validateTag(raw: string): Validation {
  if (!TAG_RE.test(raw)) {
    // Not pedantry: a quote or a backtick here closes the struct-tag literal and
    // injects source into a file that is about to be compiled.
    return vscode.l10n.t(
      'A validate tag cannot contain quotes, backticks or backslashes. Use plain validator syntax, such as required or oneof=active closed.',
    );
  }
  return undefined;
}

function validateSqlType(raw: string, fallback: string): Validation {
  if (!raw) return preview(vscode.l10n.t('Empty uses {0}.', fallback), true);
  if (!SQL_TYPE_RE.test(raw)) {
    // This string is written straight into a .sql file the developer runs with
    // database privileges, so a statement separator is an injection into DDL.
    return vscode.l10n.t(
      'That is not a plain column definition. Use something like {0} — semicolons and quotes are not allowed here.',
      fallback,
    );
  }
  if (raw.includes('--')) {
    return vscode.l10n.t('A column type cannot contain a SQL comment. Use something like {0}.', fallback);
  }
  return undefined;
}

function validateGoType(raw: string): Validation {
  const value = raw.trim();
  if (!value) return vscode.l10n.t('A type name is required.');
  const substitute = REJECTED_TYPES[value];
  if (substitute) {
    return vscode.l10n.t('{0} is not available in this template. Use {1}.', value, substitute);
  }
  const resolved = resolveGoType(value);
  if (!resolved) {
    return vscode.l10n.t('{0} is not on the allow-list. Use one of {1}.', value, permittedTypes());
  }
  if (resolved !== value) {
    // Accepted and corrected, not refused: `spec.Normalise` maps the same
    // aliases, and bouncing a perfectly clear `datetime` back would be pedantry.
    return { message: vscode.l10n.t('Recorded as {0}.', resolved), severity: vscode.InputBoxValidationSeverity.Warning };
  }
  return undefined;
}

function resolveGoType(raw: string): string | undefined {
  const value = raw.trim();
  if (GO_TYPES.some((type) => type.name === value)) return value;
  return TYPE_ALIASES[value.toLowerCase()];
}

function defaultSql(type: string): string {
  return GO_TYPES.find((candidate) => candidate.name === type)?.sql ?? 'varchar(255) NOT NULL';
}

function permittedTypes(): string {
  return GO_TYPES.map((type) => type.name).join(', ');
}

/** A non-blocking note, shown only when the value was actually changed. */
function preview(text: string, changed: boolean): Validation {
  return changed
    ? { message: text, severity: vscode.InputBoxValidationSeverity.Info }
    : undefined;
}

function routeLabel(op: string, base: string): string {
  switch (op) {
    case 'create':
      return `POST /v1${base}`;
    case 'list':
      return `GET /v1${base}`;
    case 'get':
      return `GET /v1${base}/:id`;
    case 'update':
      return `PUT /v1${base}/:id`;
    case 'delete':
      return `DELETE /v1${base}/:id`;
    default:
      // Additive by reflex: an operation this build does not know about is a row
      // without a route label, not an exception.
      return '';
  }
}

// ── what the scaffolder will write ──────────────────────────────────────────

/**
 * The exact output of `scaffold.Resource`: five new files, `handler/request.go`
 * appended to or created, and `bootstrap/bootstrapper.go` patched via `fx_wire`.
 *
 * `request.go`'s action is decided by a stat rather than assumed, because
 * "create" and "modify" mean different things to a reviewer — one of them is a
 * file with their own DTOs in it.
 */
async function plannedFiles(spec: ResourceSpec, root: vscode.Uri | undefined): Promise<PlannedFile[]> {
  const stem = snake(spec.name);
  const tableStem = snake(spec.plural);
  const files: PlannedFile[] = [
    {
      path: `core/domain/${stem}.go`,
      action: 'create',
      note: vscode.l10n.t('The domain struct, with json and db tags plus ID, CreatedAt and UpdatedAt.'),
    },
    {
      path: `db/${tableStem}.sql`,
      action: 'create',
      note: vscode.l10n.t(
        'The DDL. Nothing applies it for you — the agent never runs DDL, because it is the one action git cannot undo.',
      ),
    },
    {
      path: `repo/postgres/${stem}.go`,
      action: 'create',
      note: vscode.l10n.t('The repository on dblib.Psql, with query timeouts and pgx row scanning.'),
    },
    {
      path: `handler/response/${stem}.go`,
      action: 'create',
      note: vscode.l10n.t('The response DTO, its converters and the operation envelopes.'),
    },
    {
      path: `handler/${stem}.go`,
      action: 'create',
      note: vscode.l10n.t('The handler, its constructor and Routes() with .Name(...) on every route.'),
    },
  ];

  const requestExists = root ? await exists(vscode.Uri.joinPath(root, 'handler', 'request.go')) : true;
  files.push({
    path: 'handler/request.go',
    action: requestExists ? 'modify' : 'create',
    note: requestExists
      ? vscode.l10n.t(
          'The request DTOs are appended. Every resource’s DTOs share this file — govalid only generates validators for what is in it.',
        )
      : vscode.l10n.t('Created, because this service does not have one yet.'),
  });
  files.push({
    path: 'bootstrap/bootstrapper.go',
    action: 'modify',
    note: vscode.l10n.t(
      'FX registration, via fx_wire. Left unchanged if it already registers both constructors.',
    ),
  });
  return files;
}

async function exists(uri: vscode.Uri): Promise<boolean> {
  try {
    await vscode.workspace.fs.stat(uri);
    return true;
  } catch {
    return false;
  }
}

/**
 * The §10.1 acceptance criteria, narrowed to the operations actually selected.
 *
 * They are listed because `POST /v1/tasks` accepts `acceptance`, and a criterion
 * naming a route that was never requested would fail a run for the wrong reason.
 */
function acceptanceCriteria(spec: ResourceSpec): string[] {
  const criteria = [
    'go build ./... clean',
    'go vet ./... clean',
    'rules_lint: layer-sql-boundary, handler-signature, repo-contract, fx-registration all pass',
    'FxRepo + FxHandler updated with correct annotations',
    'govalid validators regenerated',
  ];
  if (spec.operations.includes('create')) {
    criteria.push(`POST /v1${spec.route_base} visible in /docs/v3Doc.json`);
  } else if (spec.operations.includes('list')) {
    criteria.push(`GET /v1${spec.route_base} visible in /docs/v3Doc.json`);
  }
  return criteria;
}

function taskText(spec: ResourceSpec): string {
  const summary = vscode.l10n.t(
    'Scaffold the resource {0} from this spec. It is complete — do not infer or change any part of it.',
    spec.name,
  );
  return `${summary}\n\n\`\`\`json\n${JSON.stringify(spec, undefined, 2)}\n\`\`\`\n`;
}

// ── the review pane ─────────────────────────────────────────────────────────

type ReviewDecision = 'confirm' | 'back' | 'cancel';

/**
 * A `WebviewPanel` rather than a modal: seven file paths and a field table do
 * not fit in a dialog, and the decision this asks for is exactly the one that
 * deserves scrolling back over.
 *
 * It takes focus, unlike everything else in this file. The developer asked for
 * it by finishing the wizard, and its confirm button has to be reachable.
 */
function showReview(
  deps: WizardDeps,
  spec: ResourceSpec,
  files: readonly PlannedFile[],
): Promise<ReviewDecision> {
  return new Promise((resolve) => {
    const panel = vscode.window.createWebviewPanel(
      'dakcoder.scaffoldReview',
      vscode.l10n.t('Scaffold {0}', spec.name),
      vscode.ViewColumn.Active,
      // No local resources at all: the document is self-contained, so the
      // narrowest possible root is the correct one.
      { enableScripts: true, localResourceRoots: [], retainContextWhenHidden: false },
    );

    let settled = false;
    const finish = (decision: ReviewDecision): void => {
      if (settled) return;
      settled = true;
      resolve(decision);
      panel.dispose();
    };

    panel.webview.onDidReceiveMessage((raw: unknown) => {
      const type = (raw as { type?: unknown } | null)?.type;
      if (type === 'confirm') finish('confirm');
      else if (type === 'back') finish('back');
      else if (type === 'copy') {
        void vscode.env.clipboard.writeText(JSON.stringify(spec, undefined, 2));
        void vscode.window.setStatusBarMessage(vscode.l10n.t('Spec copied.'), 3000);
      }
      // Any other message is ignored. The seam is additive here too.
    });
    panel.onDidDispose(() => finish('cancel'));
    panel.webview.html = reviewHtml(panel.webview, spec, files);
    deps.log.info(`scaffold review open for ${spec.name} (${files.length} files)`);
  });
}

function reviewHtml(
  webview: vscode.Webview,
  spec: ResourceSpec,
  files: readonly PlannedFile[],
): string {
  const nonce = randomBytes(16).toString('base64');
  const lang = vscode.env.language || 'en';
  const filterOf = (field: ResourceField): string => {
    const filter = spec.list_filters.find((candidate) => candidate.go === field.go);
    return filter ? `?${filter.form}=` : '—';
  };

  const fieldRows = spec.fields
    .map(
      (field) => `<tr>
      <th scope="row"><code>${esc(field.go)}</code></th>
      <td><code>${esc(field.type)}</code></td>
      <td><code>${esc(field.json)}</code></td>
      <td><code>${esc(field.db)}</code></td>
      <td><code>${esc(field.validate)}</code></td>
      <td><code>${esc(field.sql)}</code></td>
      <td>${esc(filterOf(field))}</td>
    </tr>`,
    )
    .join('\n');

  const fileRows = files
    .map((file) => {
      // The word, not only the colour: this list is read in screenshots and in
      // high-contrast themes, and "create" versus "modify" is the whole point.
      const label = file.action === 'create' ? vscode.l10n.t('create') : vscode.l10n.t('modify');
      const glyph = file.action === 'create' ? '+' : '~';
      return `<tr>
      <td class="action ${file.action}"><span aria-hidden="true">${glyph}</span> ${esc(label)}</td>
      <th scope="row"><code>${esc(file.path)}</code></th>
      <td class="note">${esc(file.note)}</td>
    </tr>`;
    })
    .join('\n');

  const routes = spec.operations
    .map((op) => `<li><code>${esc(routeLabel(op, spec.route_base) || op)}</code></li>`)
    .join('\n');

  const fieldCount =
    spec.fields.length === 1
      ? vscode.l10n.t('1 field')
      : vscode.l10n.t('{0} fields', spec.fields.length);
  const fileCount =
    files.length === 1
      ? vscode.l10n.t('1 file')
      : vscode.l10n.t('{0} files', files.length);

  return `<!DOCTYPE html>
<html lang="${esc(lang)}">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}'; img-src ${webview.cspSource};">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(vscode.l10n.t('Scaffold {0}', spec.name))}</title>
<style nonce="${nonce}">
  /* Every colour is a theme variable. A brand hex here would be unreadable in
     one theme out of the three the pilot machines actually run. */
  body {
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-foreground);
    background: var(--vscode-editor-background);
    padding: 1rem 1.25rem 6rem;
    line-height: 1.5;
  }
  h1 { font-size: 1.4em; margin: 0 0 .25rem; }
  h2 { font-size: 1.05em; margin: 1.75rem 0 .5rem; }
  p.lede { color: var(--vscode-descriptionForeground); margin: 0 0 1rem; }
  dl.meta { display: grid; grid-template-columns: max-content 1fr; gap: .25rem 1rem; margin: 0; }
  dt { color: var(--vscode-descriptionForeground); }
  dd { margin: 0; }
  table { border-collapse: collapse; width: 100%; margin-top: .5rem; }
  caption { text-align: left; color: var(--vscode-descriptionForeground); padding-bottom: .35rem; }
  th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid var(--vscode-panel-border); vertical-align: top; }
  thead th { color: var(--vscode-descriptionForeground); font-weight: 600; white-space: nowrap; }
  tbody th { font-weight: 600; }
  code { font-family: var(--vscode-editor-font-family); font-size: .95em; }
  td.action { white-space: nowrap; font-variant: small-caps; }
  td.action.create { color: var(--vscode-gitDecoration-addedResourceForeground); }
  td.action.modify { color: var(--vscode-gitDecoration-modifiedResourceForeground); }
  td.note { color: var(--vscode-descriptionForeground); }
  ul.routes { margin: .5rem 0; padding-left: 1.25rem; }
  .bar {
    position: fixed; left: 0; right: 0; bottom: 0;
    display: flex; gap: .5rem; align-items: center;
    padding: .75rem 1.25rem;
    background: var(--vscode-editor-background);
    border-top: 1px solid var(--vscode-panel-border);
  }
  .bar .spacer { flex: 1; }
  button {
    font-family: inherit; font-size: inherit;
    padding: .35rem 1rem; border: 1px solid var(--vscode-button-border, transparent);
    border-radius: 2px; cursor: pointer;
    color: var(--vscode-button-secondaryForeground);
    background: var(--vscode-button-secondaryBackground);
  }
  button.primary {
    color: var(--vscode-button-foreground);
    background: var(--vscode-button-background);
  }
  button:hover { background: var(--vscode-button-secondaryHoverBackground); }
  button.primary:hover { background: var(--vscode-button-hoverBackground); }
  button:focus-visible { outline: 1px solid var(--vscode-focusBorder); outline-offset: 2px; }
  .warn { color: var(--vscode-descriptionForeground); }
</style>
</head>
<body>
  <h1>${esc(vscode.l10n.t('Scaffold {0}', spec.name))}</h1>
  <p class="lede">${esc(
    vscode.l10n.t(
      'Nothing is written until you confirm. {0}, {1}.',
      fieldCount,
      fileCount,
    ),
  )}</p>

  <dl class="meta">
    <dt>${esc(vscode.l10n.t('Resource'))}</dt><dd><code>${esc(spec.name)}</code> / <code>${esc(spec.plural)}</code></dd>
    <dt>${esc(vscode.l10n.t('Table'))}</dt><dd><code>${esc(spec.table)}</code></dd>
    <dt>${esc(vscode.l10n.t('Route base'))}</dt><dd><code>/v1${esc(spec.route_base)}</code></dd>
    <dt>${esc(vscode.l10n.t('Pagination'))}</dt><dd>${esc(
      spec.paginate ? vscode.l10n.t('skip and limit accepted') : vscode.l10n.t('not enabled'),
    )}</dd>
  </dl>

  <h2>${esc(vscode.l10n.t('Routes'))}</h2>
  <ul class="routes">${routes}</ul>

  <h2>${esc(vscode.l10n.t('Fields'))}</h2>
  <table>
    <caption>${esc(
      vscode.l10n.t('ID, CreatedAt and UpdatedAt are added by the scaffolder and are not listed here.'),
    )}</caption>
    <thead><tr>
      <th scope="col">${esc(vscode.l10n.t('Go'))}</th>
      <th scope="col">${esc(vscode.l10n.t('Type'))}</th>
      <th scope="col">${esc(vscode.l10n.t('json'))}</th>
      <th scope="col">${esc(vscode.l10n.t('db'))}</th>
      <th scope="col">${esc(vscode.l10n.t('validate'))}</th>
      <th scope="col">${esc(vscode.l10n.t('SQL'))}</th>
      <th scope="col">${esc(vscode.l10n.t('Filter'))}</th>
    </tr></thead>
    <tbody>${fieldRows}</tbody>
  </table>

  <h2>${esc(vscode.l10n.t('Files'))}</h2>
  <table>
    <thead><tr>
      <th scope="col">${esc(vscode.l10n.t('Action'))}</th>
      <th scope="col">${esc(vscode.l10n.t('Path'))}</th>
      <th scope="col">${esc(vscode.l10n.t('Notes'))}</th>
    </tr></thead>
    <tbody>${fileRows}</tbody>
  </table>
  <p class="warn">${esc(
    vscode.l10n.t(
      'Each write is still presented for approval as one changeset. The DDL is written but never applied.',
    ),
  )}</p>

  <div class="bar">
    <button type="button" id="back">${esc(vscode.l10n.t('Back to the wizard'))}</button>
    <button type="button" id="copy">${esc(vscode.l10n.t('Copy the spec'))}</button>
    <span class="spacer"></span>
    <button type="button" id="confirm" class="primary">${esc(vscode.l10n.t('Scaffold'))}</button>
  </div>

<script nonce="${nonce}">
  const vscodeApi = acquireVsCodeApi();
  for (const id of ['back', 'copy', 'confirm']) {
    document.getElementById(id).addEventListener('click', () => vscodeApi.postMessage({ type: id }));
  }
  document.getElementById('confirm').focus();
</script>
</body>
</html>`;
}

function esc(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── the migration plan ──────────────────────────────────────────────────────

export const MIGRATION_PLAN_PATH = '.dakcoder/migration/plan.md';

/** The four classifications Part A §12.2 writes. Anything else is carried through. */
export type Classification = 'MIGRATE' | 'SKIP' | 'ALREADY_MIGRATED' | 'TEST';

const CLASSIFICATIONS: readonly Classification[] = ['MIGRATE', 'SKIP', 'ALREADY_MIGRATED', 'TEST'];

export interface MigrationUnit {
  /** Workspace-relative path to the handler or repo. */
  path: string;
  /** handler / repo / test / whatever the plan says. Never interpreted. */
  kind: string;
  classification: string;
  rules: string[];
  status: string;
  /** A sha or a URL, once the unit is migrated. Empty until then. */
  commit: string;
  /** Where this unit came from, so a rewrite touches only the lines it parsed. */
  line: number;
  form: 'table' | 'task';
  /**
   * The classification a unit had before its checkbox was cleared.
   *
   * Unchecking means SKIP, but re-checking must not turn a TEST unit into a
   * MIGRATE one — the tick restores what was there, which is the only reading
   * that survives a mis-click.
   */
  previous?: string;
}

/** Column keys the table parser recognises, and the header spellings that map to each. */
const COLUMN_KEYS: Readonly<Record<string, readonly string[]>> = {
  path: ['unit', 'path', 'file', 'handler', 'target'],
  kind: ['kind', 'type', 'layer'],
  classification: ['classification', 'class', 'decision', 'action'],
  rules: ['rules', 'violations', 'violated', 'violated rules', 'findings'],
  status: ['status', 'state', 'progress'],
  commit: ['commit', 'sha', 'link', 'commit link'],
};

export interface ParsedPlan {
  lines: string[];
  units: MigrationUnit[];
  /** Header cell index per key, for the table form. Absent for a task list. */
  columns?: Record<string, number>;
  eol: '\n' | '\r\n';
}

/**
 * Parse `plan.md` without claiming to understand it.
 *
 * Two forms are recognised — a GFM table and a task list — because the plan is
 * described as both "machine-parseable" and "a checklist" and the runtime has
 * not pinned one yet. Everything outside the recognised lines is preserved
 * byte for byte, so prose, headings and a `## Notes` section written by hand
 * survive a reorder. A row this build cannot read is left alone rather than
 * dropped: dropping it would silently delete a unit from the migration.
 */
export function parseMigrationPlan(text: string): ParsedPlan {
  const eol = text.includes('\r\n') ? '\r\n' : '\n';
  const lines = text.split(/\r?\n/);
  const units: MigrationUnit[] = [];
  let columns: Record<string, number> | undefined;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (!columns && isTableRow(line) && i + 1 < lines.length && isDelimiterRow(lines[i + 1])) {
      const mapped = mapColumns(splitRow(line));
      if (mapped) {
        columns = mapped;
        i += 1;
        continue;
      }
    }
    if (columns && isTableRow(line) && !isDelimiterRow(line)) {
      const unit = unitFromRow(splitRow(line), columns, i);
      if (unit) units.push(unit);
      continue;
    }
    const task = unitFromTask(line, i);
    if (task) units.push(task);
  }

  return { lines, units, columns, eol };
}

const isTableRow = (line: string): boolean => /^\s*\|.*\|\s*$/.test(line);
const isDelimiterRow = (line: string): boolean => /^\s*\|[\s:|-]+\|\s*$/.test(line);

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\||\|$/g, '')
    .split('|')
    .map((cell) => cell.trim());
}

function mapColumns(header: string[]): Record<string, number> | undefined {
  const columns: Record<string, number> = {};
  header.forEach((cell, index) => {
    const key = cell.toLowerCase().replace(/[*_`]/g, '').trim();
    for (const [name, spellings] of Object.entries(COLUMN_KEYS)) {
      if (columns[name] === undefined && spellings.includes(key)) columns[name] = index;
    }
  });
  // A table without a path column is some other table in the same document —
  // a summary, a legend — and must not be mistaken for the plan.
  return columns.path === undefined ? undefined : columns;
}

function unitFromRow(
  cells: string[],
  columns: Record<string, number>,
  line: number,
): MigrationUnit | undefined {
  const cell = (key: string): string => {
    const index = columns[key];
    return index === undefined ? '' : (cells[index] ?? '');
  };
  const path = stripMarkup(cell('path'));
  if (!path) return undefined;
  return {
    path,
    kind: stripMarkup(cell('kind')),
    classification: normaliseClassification(cell('classification')),
    rules: splitRules(cell('rules')),
    status: stripMarkup(cell('status')).toLowerCase() || 'pending',
    commit: stripMarkup(cell('commit')),
    line,
    form: 'table',
  };
}

const TASK_RE = /^\s*[-*]\s+\[( |x|X)\]\s+(.*)$/;

/**
 * `- [ ] handler/pension.go — MIGRATE — rules: a, b — status: pending`
 *
 * Segments are matched by their label rather than by position, so a plan that
 * omits the commit or adds a segment this build has never seen still parses.
 */
function unitFromTask(line: string, index: number): MigrationUnit | undefined {
  const match = TASK_RE.exec(line);
  if (!match) return undefined;
  const segments = match[2].split(/\s+[—–]\s+|\s+--\s+/).map((part) => part.trim());
  const path = stripMarkup(segments[0] ?? '');
  if (!path) return undefined;

  const unit: MigrationUnit = {
    path,
    kind: '',
    classification: match[1] === ' ' ? 'SKIP' : 'MIGRATE',
    rules: [],
    status: 'pending',
    commit: '',
    line: index,
    form: 'task',
  };
  for (const segment of segments.slice(1)) {
    const labelled = /^(\w+)\s*:\s*(.*)$/.exec(segment);
    if (!labelled) {
      const upper = segment.toUpperCase().replace(/[`*]/g, '');
      if ((CLASSIFICATIONS as readonly string[]).includes(upper)) unit.classification = upper;
      continue;
    }
    const [, label, value] = labelled;
    switch (label.toLowerCase()) {
      case 'rules':
      case 'violations':
        unit.rules = splitRules(value);
        break;
      case 'status':
        unit.status = stripMarkup(value).toLowerCase();
        break;
      case 'commit':
        unit.commit = stripMarkup(value);
        break;
      case 'kind':
      case 'type':
        unit.kind = stripMarkup(value);
        break;
      default:
        // An unknown segment is not an error. It is also not preserved on
        // rewrite, which is why the task form is the fallback and the table is
        // the form the runtime should write.
        break;
    }
  }
  return unit;
}

function splitRules(cell: string): string[] {
  return stripMarkup(cell)
    .split(/[,;]/)
    .map((rule) => rule.trim())
    .filter((rule) => rule.length > 0 && rule !== '-' && rule !== '—');
}

function stripMarkup(cell: string): string {
  // Link text is what a human reads; the href is noise in a tree label. The
  // href survives because `commit` keeps whichever form the cell held.
  const link = /^\[([^\]]*)\]\(([^)]*)\)$/.exec(cell.trim());
  const text = link ? (link[1] || link[2]) : cell;
  return text.replace(/[`*_]/g, '').trim();
}

/**
 * An unclassified row reads as MIGRATE.
 *
 * The alternative — defaulting to SKIP — makes a plan whose classification
 * column this build failed to recognise look empty, and an empty plan is
 * indistinguishable from a finished one. MIGRATE puts the row in front of the
 * developer instead, where the confirmation lists it by name before anything
 * runs. An unknown classification word is kept verbatim and simply is not
 * MIGRATE, so it is excluded until someone says otherwise.
 */
function normaliseClassification(cell: string): string {
  const value = stripMarkup(cell).toUpperCase().replace(/[\s-]+/g, '_');
  return value || 'MIGRATE';
}

/**
 * Write the units back into the lines they came from, in the new order.
 *
 * Only the slots the parser claimed are rewritten. Reordering is therefore a
 * permutation over those slots and nothing else in the document moves — a
 * heading that sat between two rows stays between two rows.
 */
export function renderMigrationPlan(plan: ParsedPlan, units: readonly MigrationUnit[]): string {
  const slots = plan.units.map((unit) => unit.line).sort((a, b) => a - b);
  const lines = [...plan.lines];
  units.forEach((unit, index) => {
    const slot = slots[index];
    if (slot === undefined) return;
    lines[slot] =
      unit.form === 'table' && plan.columns
        ? renderRow(unit, plan.columns, plan.lines[slot])
        : renderTask(unit);
  });
  return lines.join(plan.eol);
}

function renderRow(unit: MigrationUnit, columns: Record<string, number>, template: string): string {
  const cells = splitRow(template);
  const width = Math.max(cells.length, ...Object.values(columns).map((index) => index + 1));
  const out = new Array<string>(width).fill('');
  for (let i = 0; i < width; i++) out[i] = cells[i] ?? '';
  const set = (key: string, value: string): void => {
    const index = columns[key];
    if (index !== undefined) out[index] = value;
  };
  set('path', unit.path);
  set('kind', unit.kind);
  set('classification', unit.classification);
  set('rules', unit.rules.join(', '));
  set('status', unit.status);
  set('commit', unit.commit);
  return `| ${out.join(' | ')} |`;
}

function renderTask(unit: MigrationUnit): string {
  const box = unit.classification === 'MIGRATE' ? 'x' : ' ';
  const parts = [unit.path, unit.classification];
  if (unit.kind) parts.push(`kind: ${unit.kind}`);
  if (unit.rules.length) parts.push(`rules: ${unit.rules.join(', ')}`);
  if (unit.status) parts.push(`status: ${unit.status}`);
  if (unit.commit) parts.push(`commit: ${unit.commit}`);
  return `- [${box}] ${parts.join(' — ')}`;
}

// ── the migration tree ──────────────────────────────────────────────────────

class UnitNode extends vscode.TreeItem {
  constructor(
    readonly unit: MigrationUnit,
    readonly index: number,
  ) {
    super(unit.path, vscode.TreeItemCollapsibleState.Collapsed);
  }
}

class DetailNode extends vscode.TreeItem {
  constructor(label: string, icon: string, description?: string) {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.iconPath = new vscode.ThemeIcon(icon);
    if (description) this.description = description;
  }
}

type MigrationNode = UnitNode | DetailNode;

const MIGRATION_MIME = 'application/vnd.code.tree.dakcodermigration';

/**
 * Status glyphs. Every one is paired with the status *word* in the row's
 * description, so nothing here depends on the icon or its colour.
 */
function statusIcon(status: string): vscode.ThemeIcon {
  switch (status) {
    case 'done':
    case 'migrated':
      return new vscode.ThemeIcon('pass-filled');
    case 'running':
    case 'in_progress':
      return new vscode.ThemeIcon('sync~spin');
    case 'failed':
    case 'error':
      return new vscode.ThemeIcon('error');
    case 'skipped':
      return new vscode.ThemeIcon('circle-slash');
    case 'pending':
      return new vscode.ThemeIcon('circle-outline');
    default:
      // A status this build has never seen still gets a row and its own word.
      return new vscode.ThemeIcon('question');
  }
}

function statusWord(status: string): string {
  switch (status) {
    case 'done':
    case 'migrated':
      return vscode.l10n.t('migrated');
    case 'running':
    case 'in_progress':
      return vscode.l10n.t('running');
    case 'failed':
    case 'error':
      return vscode.l10n.t('failed');
    case 'skipped':
      return vscode.l10n.t('skipped');
    case 'pending':
      return vscode.l10n.t('pending');
    default:
      return status;
  }
}

function classificationWord(classification: string): string {
  switch (classification) {
    case 'MIGRATE':
      return vscode.l10n.t('migrate');
    case 'SKIP':
      return vscode.l10n.t('skip');
    case 'ALREADY_MIGRATED':
      return vscode.l10n.t('already migrated');
    case 'TEST':
      return vscode.l10n.t('test');
    default:
      return classification.toLowerCase();
  }
}

export class MigrationTree
  implements
    vscode.TreeDataProvider<MigrationNode>,
    vscode.TreeDragAndDropController<MigrationNode>,
    vscode.Disposable
{
  readonly dropMimeTypes = [MIGRATION_MIME];
  readonly dragMimeTypes = [MIGRATION_MIME];

  private readonly changed = new vscode.EventEmitter<MigrationNode | undefined>();
  readonly onDidChangeTreeData = this.changed.event;

  private readonly disposables: vscode.Disposable[] = [];
  private view?: vscode.TreeView<MigrationNode>;
  private watcher?: vscode.FileSystemWatcher;

  private plan?: ParsedPlan;
  private units: MigrationUnit[] = [];
  private unreadable?: string;
  /** True from the moment the run is confirmed: the plan is the run's order now. */
  private locked = false;
  /** The text we last wrote, so our own write does not read as an external edit. */
  private lastWritten?: string;

  constructor(private readonly deps: WizardDeps) {}

  attach(view: vscode.TreeView<MigrationNode>): void {
    this.view = view;
    this.disposables.push(view.onDidChangeCheckboxState((event) => this.onCheckbox(event)));
  }

  dispose(): void {
    this.changed.dispose();
    this.watcher?.dispose();
    for (const disposable of this.disposables) disposable.dispose();
  }

  private planUri(): vscode.Uri | undefined {
    const root = this.deps.workspaceRoot();
    return root ? vscode.Uri.joinPath(root, ...MIGRATION_PLAN_PATH.split('/')) : undefined;
  }

  async refresh(): Promise<void> {
    const uri = this.planUri();
    this.watch(uri);
    if (!uri) {
      this.plan = undefined;
      this.units = [];
      this.unreadable = undefined;
      this.publish();
      return;
    }
    try {
      const bytes = await vscode.workspace.fs.readFile(uri);
      const text = new TextDecoder().decode(bytes);
      const parsed = parseMigrationPlan(text);
      this.plan = parsed;
      this.units = parsed.units.map((unit) => ({ ...unit }));
      this.unreadable = parsed.units.length
        ? undefined
        : vscode.l10n.t(
            'No units were recognised in {0}. Open it to check the format — a table with a Unit column, or a task list, is what this view reads.',
            MIGRATION_PLAN_PATH,
          );
    } catch {
      // A missing plan is the normal state before the audit runs, not an error.
      this.plan = undefined;
      this.units = [];
      this.unreadable = undefined;
    }
    this.publish();
  }

  private watch(uri: vscode.Uri | undefined): void {
    if (this.watcher || !uri) return;
    const root = this.deps.workspaceRoot();
    if (!root) return;
    this.watcher = vscode.workspace.createFileSystemWatcher(
      new vscode.RelativePattern(root, MIGRATION_PLAN_PATH),
    );
    const reload = (): void => {
      void this.reloadIfForeign(uri);
    };
    this.watcher.onDidCreate(reload);
    this.watcher.onDidChange(reload);
    this.watcher.onDidDelete(reload);
  }

  /**
   * The runtime rewrites `plan.md` as each unit completes, so the view has to
   * follow it. It must not follow its *own* writes, though: reloading on those
   * would drop the ordering the developer is halfway through arranging.
   */
  private async reloadIfForeign(uri: vscode.Uri): Promise<void> {
    try {
      const text = new TextDecoder().decode(await vscode.workspace.fs.readFile(uri));
      if (text === this.lastWritten) return;
    } catch {
      /* deleted; refresh will report it */
    }
    await this.refresh();
  }

  private publish(): void {
    void vscode.commands.executeCommand(
      'setContext',
      'dakcoder.hasMigrationPlan',
      this.units.length > 0,
    );
    void vscode.commands.executeCommand('setContext', 'dakcoder.migrationLocked', this.locked);
    if (this.view) {
      const selected = this.units.filter(isSelected).length;
      this.view.message = this.unreadable;
      this.view.description = this.units.length
        ? selected === 1
          ? vscode.l10n.t('1 of {0} selected', this.units.length)
          : vscode.l10n.t('{0} of {1} selected', selected, this.units.length)
        : undefined;
    }
    this.changed.fire(undefined);
  }

  getTreeItem(node: MigrationNode): vscode.TreeItem {
    if (!(node instanceof UnitNode)) return node;
    const { unit } = node;
    node.id = `${unit.path}:${node.index}`;
    node.description = `${classificationWord(unit.classification)} · ${statusWord(unit.status)}`;
    node.iconPath = statusIcon(unit.status);
    node.contextValue = this.locked ? 'dakcoder.migration.unit.locked' : 'dakcoder.migration.unit';
    node.checkboxState = isSelected(unit)
      ? vscode.TreeItemCheckboxState.Checked
      : vscode.TreeItemCheckboxState.Unchecked;
    node.accessibilityInformation = {
      label: vscode.l10n.t(
        '{0}, position {1} of {2}, {3}, {4}',
        unit.path,
        node.index + 1,
        this.units.length,
        classificationWord(unit.classification),
        statusWord(unit.status),
      ),
    };
    node.tooltip = tooltipFor(unit);
    node.command = {
      command: 'dakcoder.migration.openUnit',
      title: vscode.l10n.t('Open'),
      arguments: [node],
    };
    return node;
  }

  getChildren(node?: MigrationNode): MigrationNode[] {
    if (!node) return this.units.map((unit, index) => new UnitNode(unit, index));
    if (!(node instanceof UnitNode)) return [];

    const children: MigrationNode[] = [];
    if (node.unit.kind) {
      children.push(new DetailNode(vscode.l10n.t('Kind'), 'symbol-class', node.unit.kind));
    }
    for (const rule of node.unit.rules) {
      children.push(new DetailNode(rule, 'law', vscode.l10n.t('violated')));
    }
    if (!node.unit.rules.length) {
      children.push(
        new DetailNode(vscode.l10n.t('No rules listed'), 'info', vscode.l10n.t('by the audit')),
      );
    }
    if (node.unit.commit) {
      const commit = new DetailNode(node.unit.commit, 'git-commit', vscode.l10n.t('commit'));
      commit.contextValue = 'dakcoder.migration.commit';
      commit.command = {
        command: 'dakcoder.migration.openCommit',
        title: vscode.l10n.t('Open the commit'),
        arguments: [node.unit.commit],
      };
      children.push(commit);
    }
    return children;
  }

  /**
   * Required for `reveal`, which is only ever called on a unit row — and unit
   * rows are roots. Detail rows are built per expansion and hold no
   * back-reference, so nothing here can walk upwards; nothing needs to.
   */
  getParent(): MigrationNode | undefined {
    return undefined;
  }

  // ── mutation ──────────────────────────────────────────────────────────────

  private async onCheckbox(
    event: vscode.TreeCheckboxChangeEvent<MigrationNode>,
  ): Promise<void> {
    if (this.refuseWhenLocked()) {
      this.changed.fire(undefined);
      return;
    }
    for (const [node, state] of event.items) {
      if (!(node instanceof UnitNode)) continue;
      const unit = this.units[node.index];
      if (!unit) continue;
      if (state === vscode.TreeItemCheckboxState.Checked) {
        unit.classification = unit.previous && unit.previous !== 'SKIP' ? unit.previous : 'MIGRATE';
      } else if (unit.classification !== 'SKIP') {
        unit.previous = unit.classification;
        unit.classification = 'SKIP';
      }
    }
    await this.save();
  }

  async reclassify(node: unknown): Promise<void> {
    if (this.refuseWhenLocked()) return;
    const unit = this.unitOf(node);
    if (!unit) return;
    const picked = await vscode.window.showQuickPick(
      CLASSIFICATIONS.map((value) => ({
        label: classificationWord(value),
        description: value,
        picked: unit.classification === value,
      })),
      {
        title: vscode.l10n.t('Classify {0}', unit.path),
        placeHolder: vscode.l10n.t('Only MIGRATE units are run.'),
      },
    );
    if (!picked) return;
    unit.previous = unit.classification;
    unit.classification = picked.description;
    await this.save();
  }

  /**
   * The keyboard path for reordering, and the implementation the drop handler
   * delegates to. Leaf handlers first and shared helpers last is the ordering
   * the audit proposes; correcting it is the point of the confirmation step.
   */
  async move(node: unknown, offset: number): Promise<void> {
    if (this.refuseWhenLocked()) return;
    const unit = this.unitOf(node);
    if (!unit) return;
    const from = this.units.indexOf(unit);
    const to = from + offset;
    if (from < 0 || to < 0 || to >= this.units.length) return;
    this.units.splice(to, 0, ...this.units.splice(from, 1));
    await this.save();
    try {
      // Focus follows the row that moved. Without it, repeated Move Up walks
      // the focus ring instead of the item, and the second press moves the
      // wrong unit — which is how a plan gets silently mis-ordered.
      await this.view?.reveal(new UnitNode(unit, to), { select: true, focus: true });
    } catch {
      /* the tree refreshed underneath us; the order is already saved */
    }
  }

  handleDrag(
    source: readonly MigrationNode[],
    transfer: vscode.DataTransfer,
    _token: vscode.CancellationToken,
  ): void {
    const paths = source.filter((node): node is UnitNode => node instanceof UnitNode).map((node) => node.unit.path);
    if (paths.length) transfer.set(MIGRATION_MIME, new vscode.DataTransferItem(paths));
  }

  async handleDrop(
    target: MigrationNode | undefined,
    transfer: vscode.DataTransfer,
    _token: vscode.CancellationToken,
  ): Promise<void> {
    if (this.refuseWhenLocked()) return;
    const item = transfer.get(MIGRATION_MIME);
    if (!item) return;
    const paths = item.value as unknown;
    if (!Array.isArray(paths)) return;

    const moving = this.units.filter((unit) => paths.includes(unit.path));
    if (!moving.length) return;
    const rest = this.units.filter((unit) => !moving.includes(unit));
    // Dropping on nothing means "to the end", which is what the empty area
    // below the last row looks like it should do.
    const anchor = target instanceof UnitNode ? rest.indexOf(this.units[target.index]) : rest.length;
    const at = anchor < 0 ? rest.length : anchor;
    rest.splice(at, 0, ...moving);
    this.units = rest;
    await this.save();
  }

  private refuseWhenLocked(): boolean {
    if (!this.locked) return false;
    void vscode.window.showWarningMessage(
      vscode.l10n.t('The migration is running. Stop it before changing the plan.'),
    );
    return true;
  }

  private unitOf(node: unknown): MigrationUnit | undefined {
    if (node instanceof UnitNode) return this.units[node.index];
    // Invoked from the command palette rather than the row: act on the
    // selection, which is what the user was looking at.
    const selected = this.view?.selection.find((item): item is UnitNode => item instanceof UnitNode);
    return selected ? this.units[selected.index] : undefined;
  }

  private async save(): Promise<void> {
    const uri = this.planUri();
    if (!uri || !this.plan) {
      this.publish();
      return;
    }
    const text = renderMigrationPlan(this.plan, this.units);
    try {
      await vscode.workspace.fs.writeFile(uri, new TextEncoder().encode(text));
      this.lastWritten = text;
      // Re-parse our own output so line numbers and slots stay true; the plan
      // file is the durable state and the in-memory copy must not drift from it.
      this.plan = parseMigrationPlan(text);
    } catch (err) {
      this.deps.log.warn(`could not write ${MIGRATION_PLAN_PATH}: ${message(err)}`);
      void vscode.window.showErrorMessage(
        vscode.l10n.t('{0} could not be saved: {1}', MIGRATION_PLAN_PATH, message(err)),
      );
    }
    this.publish();
  }

  // ── the confirmation ──────────────────────────────────────────────────────

  /**
   * Part A §12.2 step 2 makes this mandatory, and the reason is worth repeating:
   * a migration the developer did not order is a migration they will not review.
   * The modal lists the order because the order is the decision.
   */
  async confirmAndRun(): Promise<void> {
    if (this.locked) {
      void vscode.window.showInformationMessage(vscode.l10n.t('The migration is already running.'));
      return;
    }
    const selected = this.units.filter(isSelected);
    if (!selected.length) {
      void vscode.window.showWarningMessage(
        vscode.l10n.t('Nothing is selected. Tick the units to migrate, in the order to migrate them.'),
      );
      return;
    }

    const shown = selected.slice(0, 12);
    const detail = shown
      .map((unit, index) => `${index + 1}. ${unit.path}${unit.rules.length ? `  (${unit.rules.join(', ')})` : ''}`)
      .join('\n');
    const remaining = selected.length - shown.length;
    const tail =
      remaining > 0
        ? `\n${remaining === 1 ? vscode.l10n.t('and 1 more') : vscode.l10n.t('and {0} more', remaining)}`
        : '';
    const skipped = this.units.length - selected.length;
    const skippedLine =
      skipped > 0
        ? `\n\n${
            skipped === 1
              ? vscode.l10n.t('1 unit is not selected and will not be touched.')
              : vscode.l10n.t('{0} units are not selected and will not be touched.', skipped)
          }`
        : '';

    const confirm = vscode.l10n.t('Migrate');
    const answer = await vscode.window.showWarningMessage(
      selected.length === 1
        ? vscode.l10n.t('Migrate 1 unit, in this order?')
        : vscode.l10n.t('Migrate {0} units, in this order?', selected.length),
      {
        modal: true,
        detail: `${detail}${tail}${skippedLine}\n\n${vscode.l10n.t(
          'Each unit is migrated in its own commit, and each write is still presented for approval.',
        )}`,
      },
      confirm,
    );
    if (answer !== confirm) return;

    this.locked = true;
    this.publish();
    try {
      await this.deps.migrate(selected.map((unit) => ({ ...unit })));
    } catch (err) {
      this.deps.log.error(`the migration could not be started: ${message(err)}`);
      void vscode.window.showErrorMessage(
        vscode.l10n.t('The migration could not be started: {0}', message(err)),
      );
    } finally {
      this.locked = false;
      this.publish();
    }
  }

  async openUnit(node: unknown): Promise<void> {
    const unit = this.unitOf(node);
    const root = this.deps.workspaceRoot();
    if (!unit || !root) return;
    const uri = vscode.Uri.joinPath(root, ...unit.path.split('/'));
    try {
      const document = await vscode.workspace.openTextDocument(uri);
      // `preview: true` so walking the list does not fill the tab bar, and the
      // reveal that got here already moved focus deliberately.
      await vscode.window.showTextDocument(document, { preview: true });
    } catch {
      void vscode.window.showWarningMessage(
        vscode.l10n.t('{0} is named in the plan but is not in this workspace.', unit.path),
      );
    }
  }

  /**
   * A commit cell holds either a URL or a sha; the plan format does not say
   * which, and there is no configured host to turn a sha into a URL. So a URL
   * opens and a sha is copied — inventing a forge address would send the
   * developer to a page that may not exist.
   */
  async openCommit(commit: unknown): Promise<void> {
    if (typeof commit !== 'string' || !commit) return;
    if (/^https?:\/\//i.test(commit)) {
      await vscode.env.openExternal(vscode.Uri.parse(commit));
      return;
    }
    await vscode.env.clipboard.writeText(commit);
    void vscode.window.setStatusBarMessage(
      vscode.l10n.t('Commit {0} copied.', commit.slice(0, 12)),
      3000,
    );
  }

  async openPlanFile(): Promise<void> {
    const uri = this.planUri();
    if (!uri) return;
    try {
      await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri));
    } catch {
      void vscode.window.showInformationMessage(
        vscode.l10n.t(
          'There is no {0} yet. Run "dakcoder: Audit Legacy Patterns" to produce one.',
          MIGRATION_PLAN_PATH,
        ),
      );
    }
  }
}

/** Only MIGRATE units run. Every other classification is a deliberate exclusion. */
function isSelected(unit: MigrationUnit): boolean {
  return unit.classification === 'MIGRATE';
}

function tooltipFor(unit: MigrationUnit): vscode.MarkdownString {
  const lines = [
    `**${mdEscape(unit.path)}**`,
    '',
    `${vscode.l10n.t('Classification')}: ${mdEscape(classificationWord(unit.classification))}`,
    `${vscode.l10n.t('Status')}: ${mdEscape(statusWord(unit.status))}`,
  ];
  if (unit.kind) lines.push(`${vscode.l10n.t('Kind')}: ${mdEscape(unit.kind)}`);
  lines.push(
    unit.rules.length
      ? `${vscode.l10n.t('Violates')}: ${mdEscape(unit.rules.join(', '))}`
      : vscode.l10n.t('No rules listed by the audit.'),
  );
  if (unit.commit) lines.push(`${vscode.l10n.t('Commit')}: ${mdEscape(unit.commit)}`);

  const tooltip = new vscode.MarkdownString(lines.join('\n\n'));
  // Every string above came out of a file the agent wrote. Plain markdown still
  // renders links and images, so it is escaped and untrusted rather than merely
  // un-HTML'd.
  tooltip.isTrusted = false;
  tooltip.supportHtml = false;
  return tooltip;
}

function mdEscape(text: string): string {
  return text.replace(/[\\`*_{}[\]()#+\-.!|<>]/g, '\\$&');
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// ── registration ────────────────────────────────────────────────────────────

export interface WizardDeps {
  log: vscode.LogOutputChannel;
  /** The folder the runtime was spawned against. Every path here is relative to it. */
  workspaceRoot: () => vscode.Uri | undefined;
  /**
   * Start the scaffold. The caller owns the seam to `POST /v1/tasks`, which
   * takes only `{task, mode, acceptance}` — hence `request.task` carrying the
   * spec as fenced JSON. Send it in `scaffolder` mode.
   */
  scaffold: (request: ScaffoldRequest) => Promise<void>;
  /** Start the migration run, in the confirmed order. Resolves when it ends. */
  migrate: (units: MigrationUnit[]) => Promise<void>;
}

export interface Wizards extends vscode.Disposable {
  scaffold: ScaffoldWizard;
  migration: MigrationTree;
}

export function register(context: vscode.ExtensionContext, deps: WizardDeps): Wizards {
  const scaffold = new ScaffoldWizard(deps);
  const migration = new MigrationTree(deps);

  const view = vscode.window.createTreeView<MigrationNode>('dakcoder.migration', {
    treeDataProvider: migration,
    dragAndDropController: migration,
    // Manual, because the tick is a classification and not a rollup: checking a
    // parent must never be read as checking rules the audit found.
    manageCheckboxStateManually: true,
    canSelectMany: true,
    showCollapseAll: true,
  });
  migration.attach(view);
  void migration.refresh();

  const disposables: vscode.Disposable[] = [
    view,
    migration,
    vscode.commands.registerCommand('dakcoder.scaffoldResource', () => scaffold.run()),
    vscode.commands.registerCommand('dakcoder.migration.refresh', () => migration.refresh()),
    vscode.commands.registerCommand('dakcoder.migration.openPlanFile', () => migration.openPlanFile()),
    vscode.commands.registerCommand('dakcoder.migration.openUnit', (node?: unknown) =>
      migration.openUnit(node),
    ),
    vscode.commands.registerCommand('dakcoder.migration.openCommit', (commit?: unknown) =>
      migration.openCommit(commit),
    ),
    vscode.commands.registerCommand('dakcoder.migration.reclassify', (node?: unknown) =>
      migration.reclassify(node),
    ),
    vscode.commands.registerCommand('dakcoder.migration.moveUp', (node?: unknown) =>
      migration.move(node, -1),
    ),
    vscode.commands.registerCommand('dakcoder.migration.moveDown', (node?: unknown) =>
      migration.move(node, 1),
    ),
    vscode.commands.registerCommand('dakcoder.migration.run', () => migration.confirmAndRun()),
  ];
  context.subscriptions.push(...disposables);

  return {
    scaffold,
    migration,
    dispose: () => {
      for (const disposable of disposables) disposable.dispose();
    },
  };
}
