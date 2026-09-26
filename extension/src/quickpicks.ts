/**
 * The chat view's two title-bar pickers: every slash command, and every
 * setting.
 *
 * Both exist because the panel's only way to discover either was already
 * knowing it: slash commands appear when "/" is typed at the start of the
 * composer, and settings live in the Settings editor under a search nobody
 * runs. A picker lists all of them with what they do, in the place the work
 * happens.
 *
 * The settings list is read from the extension's own manifest, so a setting
 * added to `package.json` appears here without an edit to this file.
 */

import * as vscode from 'vscode';

import type { SlashSpec } from './chat';

// ── slash commands ──────────────────────────────────────────────────────────

/**
 * Pick a slash command, then put it in the composer ready for its argument.
 *
 * Composed rather than run: most commands take an argument (`/graph X`,
 * `/explain X`, `/migrate path`) and several mean something different without
 * one, so the developer finishes the line and sends it the usual way.
 */
export async function pickSlashCommand(
  commands: readonly SlashSpec[],
  compose: (text: string) => void,
): Promise<void> {
  const picked = await vscode.window.showQuickPick(
    commands.map((spec) => ({ label: `/${spec.name}`, detail: spec.hint, name: spec.name })),
    {
      title: vscode.l10n.t('dakcoder: Slash commands'),
      placeHolder: vscode.l10n.t('Choose a command; it goes into the chat composer ready to send'),
      matchOnDetail: true,
    },
  );
  if (!picked) return;
  await vscode.commands.executeCommand('dakcoder.chat.focus');
  compose(`/${picked.name} `);
}

// ── settings ────────────────────────────────────────────────────────────────

interface SettingSchema {
  type?: string | string[];
  default?: unknown;
  enum?: string[];
  enumDescriptions?: string[];
  minimum?: number;
  maximum?: number;
  description?: string;
  markdownDescription?: string;
  scope?: string;
}

interface SettingItem extends vscode.QuickPickItem {
  key?: string;
  schema?: SettingSchema;
  openAll?: boolean;
}

/** The part of `markdownDescription` a QuickPick can show: no markup, one paragraph. */
function plain(schema: SettingSchema): string {
  const text = schema.description ?? schema.markdownDescription ?? '';
  return text
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .split('\n')[0]!
    .trim();
}

function shown(value: unknown): string {
  if (value === undefined || value === '') return vscode.l10n.t('(not set)');
  if (typeof value === 'boolean') return value ? vscode.l10n.t('on') : vscode.l10n.t('off');
  if (Array.isArray(value)) return value.join(', ') || vscode.l10n.t('(empty)');
  return String(value);
}

/** Where a change lands: the workspace if it already overrides the value there. */
function targetFor(key: string, schema: SettingSchema): vscode.ConfigurationTarget {
  if (schema.scope === 'machine' || schema.scope === 'application') {
    return vscode.ConfigurationTarget.Global;
  }
  const inspected = vscode.workspace.getConfiguration().inspect(key);
  return inspected?.workspaceValue !== undefined
    ? vscode.ConfigurationTarget.Workspace
    : vscode.ConfigurationTarget.Global;
}

/**
 * List every dakcoder setting with its current value, and change one.
 *
 * Booleans flip on selection; enums offer their values; numbers and strings
 * get an input box bounded by the manifest's own minimum and maximum. Anything
 * with a richer shape -- `approvalPolicy` takes a string *or* a list -- opens
 * in the Settings editor, which is the one place that edits it correctly.
 *
 * A setting whose description says it "takes effect when the runtime
 * restarts" offers the restart, because a change that silently does nothing
 * until some later restart reads as a change that did not work.
 */
export async function quickSettings(extension: vscode.Extension<unknown>): Promise<void> {
  const properties: Record<string, SettingSchema> =
    (extension.packageJSON?.contributes?.configuration?.properties as Record<string, SettingSchema>) ?? {};

  for (;;) {
    const config = vscode.workspace.getConfiguration();
    const items: SettingItem[] = Object.entries(properties)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, schema]) => ({
        label: key.replace(/^dakcoder\./, ''),
        description: shown(config.get(key)),
        detail: plain(schema),
        key,
        schema,
      }));
    items.push(
      { label: '', kind: vscode.QuickPickItemKind.Separator },
      { label: vscode.l10n.t('$(gear) Open all dakcoder settings'), openAll: true },
    );

    const picked = await vscode.window.showQuickPick(items, {
      title: vscode.l10n.t('dakcoder: Settings'),
      placeHolder: vscode.l10n.t('Choose a setting to change it'),
      matchOnDetail: true,
    });
    if (!picked) return;
    if (picked.openAll) {
      await vscode.commands.executeCommand('workbench.action.openSettings', `@ext:${extension.id}`);
      return;
    }
    if (!picked.key || !picked.schema) return;

    const changed = await edit(picked.key, picked.schema);
    if (changed && /runtime restarts/i.test(picked.schema.markdownDescription ?? picked.schema.description ?? '')) {
      const restart = vscode.l10n.t('Restart runtime');
      void vscode.window
        .showInformationMessage(
          vscode.l10n.t('{0} takes effect when the runtime restarts.', picked.label),
          restart,
        )
        .then((choice) => {
          if (choice === restart) void vscode.commands.executeCommand('dakcoder.restartRuntime');
        });
    }
    // Back to the list, so several settings can be changed in one visit.
  }
}

async function edit(key: string, schema: SettingSchema): Promise<boolean> {
  const config = vscode.workspace.getConfiguration();
  const current = config.get(key);
  const target = targetFor(key, schema);
  const type = schema.type;

  if (type === 'boolean') {
    await config.update(key, !current, target);
    return true;
  }

  if (schema.enum) {
    const picked = await vscode.window.showQuickPick(
      schema.enum.map((value, i) => ({
        label: value,
        description: value === current ? vscode.l10n.t('current') : undefined,
        detail: schema.enumDescriptions?.[i],
      })),
      { title: key },
    );
    if (!picked) return false;
    await config.update(key, picked.label, target);
    return true;
  }

  if (type === 'number' || type === 'integer') {
    const answer = await vscode.window.showInputBox({
      title: key,
      prompt: plain(schema),
      value: current === undefined ? '' : String(current),
      validateInput: (text) => {
        const n = Number(text);
        if (text.trim() === '' || !Number.isFinite(n)) return vscode.l10n.t('Enter a number.');
        if (schema.minimum !== undefined && n < schema.minimum) {
          return vscode.l10n.t('At least {0}.', String(schema.minimum));
        }
        if (schema.maximum !== undefined && n > schema.maximum) {
          return vscode.l10n.t('At most {0}.', String(schema.maximum));
        }
        return undefined;
      },
    });
    if (answer === undefined) return false;
    await config.update(key, Number(answer), target);
    return true;
  }

  if (type === 'string') {
    const answer = await vscode.window.showInputBox({
      title: key,
      prompt: plain(schema),
      value: typeof current === 'string' ? current : '',
    });
    if (answer === undefined) return false;
    await config.update(key, answer, target);
    return true;
  }

  await vscode.commands.executeCommand('workbench.action.openSettings', key);
  return false;
}
