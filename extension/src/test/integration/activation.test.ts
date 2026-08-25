/**
 * Integration tests, inside a real VS Code.
 *
 * These cover what unit tests structurally cannot: whether the extension host
 * actually loads the bundle, whether every contributed command resolves against
 * the *running* command registry rather than a regex over source, and whether
 * activation stays inside its budget on a real host.
 *
 * **Nothing here signs in, spawns a runtime, or touches the network.** Those
 * paths are gated on a GitLab account and a Python venv, neither of which exists
 * in CI — and a test that quietly skips its own subject is worse than no test,
 * because the green tick claims coverage that is not there. What is asserted is
 * asserted for real; what cannot be is named in the plan, not faked here.
 */

import { strict as assert } from 'node:assert';
import * as vscode from 'vscode';

import { test } from './harness';

const EXTENSION_ID = 'dop.dakcoder-go';

test('the extension is present and its manifest is well formed', () => {
  const extension = vscode.extensions.getExtension(EXTENSION_ID);
  assert.ok(extension, `${EXTENSION_ID} was not found in the host`);

  const manifest = extension.packageJSON as {
    main: string;
    activationEvents: string[];
    contributes: Record<string, unknown>;
    capabilities?: { untrustedWorkspaces?: { supported?: boolean } };
  };

  assert.equal(manifest.main, './dist/extension.js', 'the bundle is what ships');
  assert.deepEqual(
    manifest.activationEvents,
    ['onStartupFinished'],
    'anything eagerer costs every other extension in the window',
  );
  assert.equal(
    manifest.capabilities?.untrustedWorkspaces?.supported,
    false,
    'the agent runs the Go toolchain and writes files; Restricted Mode must not offer it',
  );
});

test('activation completes inside its budget', async () => {
  const extension = vscode.extensions.getExtension(EXTENSION_ID)!;
  const started = Date.now();
  await extension.activate();
  const elapsed = Date.now() - started;

  assert.equal(extension.isActive, true);
  // Generous against the 50 ms target: a cold CI runner loading a 190 KB bundle
  // is not the thing this guards. It guards a regression that adds a network
  // call or a subprocess to activation, which costs seconds, not milliseconds.
  assert.ok(elapsed < 2000, `activation took ${elapsed} ms`);
});

test('every contributed command resolves in the real registry', async () => {
  // The static checker greps for `registerCommand`. This asks the host, which
  // is the only authority — a command registered inside a branch that never
  // runs passes the grep and fails here.
  const extension = vscode.extensions.getExtension(EXTENSION_ID)!;
  await extension.activate();

  const declared = (
    extension.packageJSON.contributes.commands as { command: string }[]
  ).map((c) => c.command);
  const registered = new Set(await vscode.commands.getCommands(true));

  const missing = declared.filter((c) => !registered.has(c));
  assert.deepEqual(missing, [], `declared but not registered: ${missing.join(', ')}`);
  assert.ok(declared.length >= 50, `expected the full command surface, saw ${declared.length}`);
});

test('the three sidebar views are contributed to one container', () => {
  const extension = vscode.extensions.getExtension(EXTENSION_ID)!;
  const contributes = extension.packageJSON.contributes as {
    viewsContainers: { activitybar: { id: string }[] };
    views: Record<string, { id: string; type?: string }[]>;
  };

  const container = contributes.viewsContainers.activitybar[0];
  assert.equal(container.id, 'dakcoder');

  const views = contributes.views[container.id];
  const byId = new Map(views.map((v) => [v.id, v]));
  assert.equal(byId.get('dakcoder.chat')?.type, 'webview');
  for (const id of ['dakcoder.sessions', 'dakcoder.quota', 'dakcoder.context']) {
    assert.ok(byId.has(id), `${id} is missing from the sidebar`);
  }
});

test('the settings that must not exist, do not', async () => {
  // A client-side model URL or key would be an unmetered bypass around quota and
  // audit, which is the whole reason model traffic is proxied. A message in a
  // plan does not prevent one from being added; this does.
  const config = vscode.workspace.getConfiguration('dakcoder');
  for (const forbidden of ['modelApiKey', 'modelBaseUrl', 'contextMaxMessages']) {
    assert.equal(
      config.inspect(forbidden)?.defaultValue,
      undefined,
      `dakcoder.${forbidden} must never be a setting`,
    );
  }
});

test('the approval timeout defaults to waiting indefinitely', () => {
  // A slow review must never auto-reject. The default is the whole control:
  // anything else silently converts "I was reading it" into "I refused".
  const config = vscode.workspace.getConfiguration('dakcoder');
  assert.equal(config.get<number>('approvalTimeoutSeconds'), 0);
});

test('nothing under configs/** can be auto-approved by default', () => {
  const config = vscode.workspace.getConfiguration('dakcoder');
  assert.equal(config.get<boolean>('autoApproveTrivialPatches'), false);
  assert.equal(config.get<string>('requireApproval'), 'write_side');
});

test('the proposed-diff scheme has a content provider', async () => {
  // The approval surface hands `vscode.diff` a virtual document. If the scheme
  // is unregistered the diff opens empty, which reads as "no changes" — the
  // most dangerous possible misreading of an approval.
  const extension = vscode.extensions.getExtension(EXTENSION_ID)!;
  await extension.activate();

  const uri = vscode.Uri.parse('dakcoder-proposed:/probe/handler/pension.go?id=none');
  // Opening is expected to fail (there is no such approval); what must not
  // happen is "cannot open, no provider registered for scheme".
  try {
    await vscode.workspace.openTextDocument(uri);
  } catch (err) {
    const message = String(err);
    assert.ok(
      !/no .*provider.*registered|cannot open.*scheme/i.test(message),
      `the scheme is unregistered: ${message}`,
    );
  }
});

test('the walkthrough has all five steps, each with a completion event', () => {
  const extension = vscode.extensions.getExtension(EXTENSION_ID)!;
  const walkthroughs = extension.packageJSON.contributes.walkthroughs as {
    id: string;
    steps: { id: string; completionEvents?: string[] }[];
  }[];

  const setup = walkthroughs.find((w) => w.id === 'dakcoder.setup');
  assert.ok(setup, 'the onboarding walkthrough is missing');
  assert.equal(setup.steps.length, 5);
  for (const step of setup.steps) {
    assert.ok(
      step.completionEvents && step.completionEvents.length > 0,
      // Without one the step never ticks, so the walkthrough reads as broken
      // to exactly the audience it exists for.
      `walkthrough step "${step.id}" has no completion event`,
    );
  }
});
