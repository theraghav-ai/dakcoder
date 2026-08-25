# Part B — The VS Code Extension

> Companion to **[plan.md](plan.md)** (shared context, locked decisions D1–D7, contracts C1–C5) and **[plan-backend-agent.md](plan-backend-agent.md)** (Part A).
> Read plan.md §1–§8 first. Contracts C1–C5 are the only interface to Part A; this document does not restate them.

---

## 1. Scope

Part B owns everything the developer touches: activation, the local runtime lifecycle, the Go toolchain preflight, sign-in, the chat surface, the diff/approval surface, the Go-specific editor integrations, session management, settings, packaging and distribution.

It owns **no agent logic**. It does not trim context, does not decide budgets, does not interpret rules. Under contract C5 the server is authoritative on context and the extension only *displays* what the server reports. This boundary is worth defending: `postgen`'s one soft spot is a `contextMaxMessages` setting exposed in the client for a decision that belongs on the server.

---

## 2. What we inherit, audited

`postgen`'s extension is 3,557 lines of TypeScript across five files, shipped as `postgen-0.4.0.vsix` with no runtime dependencies (esbuild-bundled, `@types` and `vsce` only). It is a good base. Honest audit:

| Component | LOC | Verdict |
|---|---|---|
| `extension.ts` | 1,374 | **Fork.** Command registration, task orchestration, output channel, status bar, doctor, mode switch. Needs decomposition — 1,374 lines in one file is where the frontend agent's own `check-lines` rule would have complained. |
| `chatView.ts` | 1,340 | **Fork.** Webview with markdown rendering, streamed-delta folding into one bubble, `@file` typeahead, slash commands, elapsed-time indicator, in-composer Stop, message steering, diff colourising, "Show more" clamping, `workspaceState` persistence, `aria-live` log region. Genuinely good; the HTML/CSS/JS is inline in a template string, which will not survive the growth this plan implies. |
| `gateway.ts` | 260 | **Fork nearly as-is.** Clean SSE parser, per-endpoint typed client, consumer errors swallowed so a disposed webview cannot abort the stream (a real bug, correctly fixed). |
| `runtime.ts` | 457 | **Fork with a major fix.** Python discovery, managed venv, wheel-hash-keyed install, loopback spawn, health wait, cross-window runtime adoption via a lock file. The adoption trick is clever and solves real SQLite contention. The `pip install` path is the problem — §4. |
| `sessionsView.ts` | 126 | **Fork as-is.** TreeView of past runs with open/resume/delete. |

Known gaps to close: no real auth (§6), no quota surface (§7), no token/context visibility (§10), no Go toolchain awareness (§5), first-run network dependency (§4), prewarm disabled (§4.4), a client-side context setting that should not exist (§13).

---

## 3. Product surface

### 3.1 Commands

| Command | Notes |
|---|---|
| `dakcoder: Sign In` / `Sign Out` | Real GitLab OAuth (§6), not a token prompt |
| `dakcoder: New Task` | Quick-pick over recent tasks + free text |
| `dakcoder: Scaffold Resource…` | Multi-step wizard producing the §10.1 spec (Part A §10.1) |
| `dakcoder: Scaffold New Service…` | Greenfield wizard |
| `dakcoder: Audit Template Compliance` | `rules_lint` across the workspace → Problems panel |
| `dakcoder: Audit Legacy Patterns` | `legacy_audit` → Problems panel + migration plan (§11.4) |
| `dakcoder: Migrate to n-api-template…` | Opens/creates `.dakcoder/migration/plan.md`, runs unit by unit |
| `dakcoder: Debug Last Failure` | Feeds the last failing command output into Debugger mode |
| `dakcoder: Explain This Rule` | On a `rules_lint` diagnostic — fetches the `skill.md` citation |
| `dakcoder: Compact Context` | Manual compaction (`/compact` equivalent) |
| `dakcoder: Show Context Inspector` | §10.2 |
| `dakcoder: Stop Current Task` | Abort |
| `dakcoder: Revert Changes From Last Task` | Restore touched paths to git HEAD |
| `dakcoder: Resume Session…` | Quick-pick over resumable sessions |
| `dakcoder: Doctor` | Go toolchain + runtime + connectivity + auth diagnostics (§5) |
| `dakcoder: Switch Mode (Local / Server)` | |
| `dakcoder: Restart Local Runtime` | |
| `dakcoder: Show Output` / `Clear Chat` | |

### 3.2 Views

- **Sidebar** — Chat (webview), Sessions (tree), **Quota** (tree, §7.2)
- **Status bar** — two items: agent state (`idle` / `planning` / `coding · 4/8` / `verifying` with a Stop affordance) and quota (`2h 41m · 3/12 · 38%`)
- **Problems panel** — `rules_lint`, `legacy_audit` and `gopls` findings as real `vscode.Diagnostic`s so they are navigable, filterable, and quick-fixable
- **Output channel** — full tool-call log, verbose-gated

### 3.3 Activation

`onStartupFinished`, as today. Nothing expensive at activation: **do not start the runtime, do not run `go version`, do not touch the network.** The runtime starts on the first task or on an explicit `Doctor`. Activation must stay under ~50 ms of extension-host time.

---

## 4. The local runtime — and its one serious problem

### 4.1 What exists

In local mode the extension finds Python 3.11+ (configured path → the `ms-python.python` extension's selected interpreter → `py`/`python3`/`python`), creates a venv under `globalStorage`, installs the bundled wheel keyed on the wheel's **content hash** (so a rebuilt wheel at the same version still forces a reinstall — a good detail), generates a random loopback token, spawns the gateway, and waits for `/v1/health` up to 60 s. Other VS Code windows adopt the running runtime through a lock file rather than starting a second one.

### 4.2 The problem (S15)

`ensureVenv` runs two `pip install`s, and the second one — `pip install <wheel>[server]` — **resolves dependencies from the network**. Behind the India Post corporate proxy this is minutes at best and the documented failure mode at worst; the code itself has a bespoke error message telling the user to configure pip or an internal index. That message is the tell. A first-run experience that can fail on the network is a first-run experience that will fail for someone on day one of the pilot, and first impressions of an internal tool do not get a second chance.

### 4.3 The fix, in two stages

**Phase 0 — vendor the wheels.** Bundle every transitive dependency as a wheel inside the `.vsix` and install offline:

```
extension/
├── runtime/
│   ├── dakcoder_agent-<ver>-py3-none-any.whl     # the agent ONLY — see Part A §19.1
│   ├── dakcoder_shared-<ver>-py3-none-any.whl
│   └── wheels/                  # full transitive closure, built per platform tag
│       ├── fastapi-*.whl  uvicorn-*.whl  pydantic*-*.whl  sqlalchemy-*.whl
│       └── httpx-*.whl  openai-*.whl  ...
└── bin/                         # gotools is a Go binary, not a wheel
    ├── gotools-win32-x64.exe    #   checksum-pinned, verified at launch (§4.5)
    └── gotools-<platform>
```

**`dakcoder-gateway` is deliberately absent.** The wheel we ship carries the agent and nothing else — no OAuth exchange, no quota enforcement, no ledger, and no code path that reads a model credential. `postgen` ships its entire gateway (`server.py`, `quota.py`, all of it) inside its bundled wheel; we do not, and Part A §19.1 explains why that matters more for us than it did for it. `redis` drops out of the closure too, since quota lives server-side.

```ts
await execCapture(venvPy, [
  "-m", "pip", "install",
  "--no-index", "--find-links", wheelsDir,   // ← zero network
  "--disable-pip-version-check",
  "--force-reinstall", wheelPath,
]);
```

Cost: roughly 25–40 MB added to the `.vsix`, built by CI with `pip download` for each target platform tag. Benefit: the network is removed from the critical path entirely, and the proxy failure mode disappears. This is unambiguously the right trade for an internal tool.

**Phase 2 — remove Python from the client.** Two options, both worth costing:

- Freeze `dakcoderd` with PyInstaller into a single per-platform binary. No interpreter discovery, no venv, no pip, ~50 MB per platform. Ship only the host platform's binary and download others on demand from the internal GitLab package registry.
- Or move the local executor into Go. `gotools` is already a Go binary in the `.vsix` (§4.5); extending it to be the local tool executor while Python stays server-side only would collapse the client to two binaries and no runtime bootstrap at all. Larger change, better end state. Evaluate after Phase 1 measures how much of the loop is actually Python.

### 4.4 Prewarm (S16)

The extension currently spawns with `--no-prewarm`. Turn it on: a 4-token probe in a background thread at runtime start costs nothing the developer can perceive, and it moves cold-start out of the first request. `Doctor` surfaces the prewarm result and its latency, as `postgen`'s `/v1/health` already does.

### 4.5 Sidecar binaries

`gotools` and the `gopls` launch policy are Part B's packaging problem.

- **`gotools`** ships as a checksum-pinned static binary per platform (`win32-x64`, `darwin-arm64`, `darwin-x64`, `linux-x64`) under `extension/bin/`. Verify the checksum before launch; refuse to run on mismatch.
- **`gopls`** is *not* bundled. Prefer, in order: the `golang.go` extension's managed `gopls`, then `$GOBIN`/`$GOPATH/bin/gopls`, then `PATH`. If none is found, `Doctor` offers `go install golang.org/x/tools/gopls@latest` as a one-click action and the agent runs in slow mode meanwhile (Part A §8.3). Bundling a `gopls` that disagrees with the user's toolchain version would cause worse problems than not having one.

### 4.6 The spawn environment holds no model credential

The local runtime authenticates to the gateway as the developer and never holds a model key (Part A §4.7). The extension is the process that constructs its environment, so enforcing that is Part B's job:

```ts
const env: NodeJS.ProcessEnv = {
  ...process.env,
  DAKCODER_MODE: "local",
  DAKCODER_GATEWAY_URL: gatewayUrl,      // model calls go to <gateway>/v1/llm  (Part A §15.4)
  DAKCODER_GATEWAY_TOKEN: loopbackToken, // authenticates the extension to its own runtime
  DAKCODER_SESSION_DSN: dsn,
  GOPLS_PATH: goplsPath,
  GOTOOLS_PATH: gotoolsPath,
};

// A model credential must never reach the child, even if one is sitting in the
// developer's shell. Same discipline the frontend agent already applies to its
// server-only cache and workspace-root variables.
delete env.DAKCODER_MODEL_API_KEY;
delete env.DAKCODER_MODEL_BASE_URL;
delete env.OPENAI_API_KEY;
delete env.LITELLM_API_KEY;
```

Two supporting checks, because an invariant nobody verifies is a comment:

- **CI greps the built `.vsix` and the bundled wheel** for `sk-[A-Za-z0-9]{16,}` and fails the build on a hit. Cheap, and it is the leak that would matter most.
- **`Doctor` reports the runtime's credential posture** — it should read *"model access: via gateway (no local key)"*. If it ever says anything else, that is a defect, and surfacing it in the report is how we would find out.

### 4.7 Runtime lifecycle

Keep cross-window adoption. Tighten it: validate the adopted runtime's **version** as well as its health, so a window running an older `.vsix` cannot adopt a newer runtime or vice versa. Add an idle timeout (default 30 min, configurable, 0 = never) so a forgotten window does not hold a `gopls` process and several hundred MB overnight.

---

## 5. Doctor: the Go toolchain preflight

Under **D2 (local-first)** the developer's own machine compiles the code. That makes the toolchain the single largest adoption risk in the programme — and the single thing a good `Doctor` can eliminate. This is the most important new client-side feature in this plan.

`Doctor` runs before the first task of a session (silently, cached for the day) and on demand, and writes a report to the output channel.

| Check | Pass | Remedy offered |
|---|---|---|
| `go version` | ≥ 1.25.0 (template is `go 1.25.0`) | Link to the internal Go distribution; block with a clear reason |
| `go env GOPATH GOBIN GOMODCACHE` | set and writable | Explain and offer to set |
| **`go env GOPRIVATE`** | contains `gitlab.cept.gov.in` | **One-click**: `go env -w GOPRIVATE=gitlab.cept.gov.in/*` |
| **`go env GONOSUMDB GONOSUMCHECK GONOSUMVERIFY` / `GOFLAGS`** | private modules excluded from sum verification | One-click `go env -w` |
| **Git credential for `gitlab.cept.gov.in`** | a probe fetch of `it-2.0-common/n-api-server` succeeds | Explain PAT-vs-SSH; offer to open the GitLab token page; show the exact `git config url.…insteadOf` or `.netrc` line |
| `gopls` | present, version recorded | Offer `go install …/gopls@latest` |
| `govalid` | present at the version `SOP.md` names | Offer `go install gitlab.cept.gov.in/it-2.0-common/n-api-validation/cmd/govalid@latest` |
| `golangci-lint` | optional | Note that lint is advisory without it |
| `govulncheck` | optional | Offer install |
| `buf` | only when a `buf.yaml` is present | Offer install |
| Module identity | `go.mod` parses; module path recorded; `go 1.x` recorded | — |
| Template generation | which library generation (`n-api-*` vs `api-*`) the module uses | If `api-*`: offer `Audit Legacy Patterns` |
| `dakcoderd` | healthy; version matches the extension | Restart action |
| `gotools` | launches; checksum verified; version recorded | Reinstall action |
| Model endpoint | reachable; prewarm latency | Show the latency; name the proxy variables if it fails |
| Auth | signed in; token expiry; roles | Sign-in action |
| Quota | current window snapshot | — |
| **Proxy sanity** | warns if `HTTP_PROXY`/`HTTPS_PROXY` are set without `NO_PROXY=127.0.0.1` | One-click fix in the spawn env |

Two design points: **every failure offers a remedy**, not just a diagnosis; and the report is copy-pasteable in full, because the first thing a developer does when blocked is paste it into a support chat.

---

## 6. Authentication (D5, contract C3)

### 6.1 Implement a real `AuthenticationProvider`

Register through `vscode.authentication.registerAuthenticationProvider` rather than rolling a bespoke sign-in. The account then appears in VS Code's Accounts menu, sessions are managed by the platform, `onDidChangeSessions` fires correctly across windows, and another IT 2.0 extension could reuse the same session later.

```ts
class DakcoderAuthProvider implements vscode.AuthenticationProvider {
  onDidChangeSessions: vscode.Event<vscode.AuthenticationProviderAuthenticationSessionsChangeEvent>;
  getSessions(scopes?: readonly string[]): Thenable<vscode.AuthenticationSession[]>;
  createSession(scopes: readonly string[]): Thenable<vscode.AuthenticationSession>;
  removeSession(sessionId: string): Thenable<void>;
}
```

### 6.2 The flow

1. Generate `code_verifier` (32 random bytes, base64url) and `state` (16 bytes). Hold the verifier in memory for the duration of the flow and mirror it into SecretStorage keyed by `state`, so a window reload mid-flow does not orphan the callback.
2. `vscode.env.openExternal` to `https://gitlab.cept.gov.in/oauth/authorize` with `client_id`, `redirect_uri`, `response_type=code`, `code_challenge`, `code_challenge_method=S256`, `state`, `scope=openid profile email read_api`.
3. Receive the callback on a registered `vscode.window.registerUriHandler` at `vscode://dop.dakcoder-go/auth/callback`.
4. **Validate `state` before doing anything else.** Mismatch → discard silently and log.
5. `POST /v1/auth/exchange {code, code_verifier, state}` to the gateway. The gateway — never the extension — talks to GitLab's token endpoint and mints the `dakcoder` JWT (Part A §15.2).
6. Store the refresh token in `context.secrets` (OS keychain-backed: Windows Credential Manager, macOS Keychain, Linux Secret Service). Keep the 15-minute access token in memory only.
7. Refresh proactively at 80% of lifetime and reactively on a 401, with a single-flight guard so ten concurrent requests trigger one refresh.

**No client secret ships in the extension.** Extension code is inspectable; a secret in it is an announcement, not a control. Register the GitLab application as a public PKCE client.

### 6.3 Loopback fallback

`vscode://` URI handlers are unreliable under Remote-SSH, dev containers, and Codespaces — the browser opens on a different machine from the extension host. Register a loopback redirect (`http://127.0.0.1:<ephemeral>/callback`) as a second URI on the GitLab application and select it automatically when `vscode.env.remoteName` is set or `vscode.env.uiKind` indicates a web UI. Bind to `127.0.0.1` only, single-use, 5-minute timeout, then close the listener.

### 6.4 Sign-out and revocation

`Sign Out` deletes the refresh token, clears the in-memory access token, fires `onDidChangeSessions`, and calls `POST /v1/auth/revoke`. A blocked GitLab account fails its next refresh within 15 minutes (Part A §15.2) — the extension must handle that path as a clean re-prompt, not an error dump.

---

## 7. Quota UX (D6, contract C4)

Quota is only useful if it is *visible before* it bites. Rolling windows are chosen partly because they can be shown.

### 7.1 Status bar

```
$(clock) 2h 41m · 3/12 · 38%
```

Window time remaining · sessions used this week · weekly token budget consumed. Hover gives the full breakdown; click opens the Quota view. Colour thresholds: default, `warningForeground` at 80%, `errorForeground` at 95%.

### 7.2 Quota view

A small tree: current window (opened at, expires in, tokens used / cap, runs used / cap), rolling week (sessions N/12, tokens used / cap), rolling hour, and role/tier. Refreshed from `GET /v1/quota` on task start, task end, and every 60 s while a task runs — never on a timer while idle.

### 7.3 Pre-flight

Before starting a run, compare the remaining window budget against an estimate for the task class (from the server's rolling median for that class). If it will not fit, say so *before* burning half of it:

> This window has ~180k tokens left. A resource scaffold typically uses ~250k. Start anyway, wait 42 min for the window to refill, or open a new session (4 of 12 remaining this week)?

### 7.4 Hitting the limit, and going offline

On `429`, show the server's one-sentence reason plus `Retry-After` as a human duration, and offer: *Wait and retry automatically* · *Resume later* (the session is persisted and resumable) · *Show quota*. Never dump the JSON body into the chat.

**Offline is a distinct state, not an error.** Because model traffic is proxied through the gateway (Part A §15.4), losing the gateway means the agent cannot run at all — by design, since that is what keeps quota and audit unbypassable. The extension must present this honestly rather than as a stack trace: a status-bar state (`dakcoder: offline`), a one-line explanation in the chat composer ("the agent needs the IT 2.0 gateway to reach the model — retrying"), automatic reconnect with backoff, and the composer disabled rather than accepting input that will fail. `Doctor` distinguishes *gateway unreachable* from *not signed in* from *quota exhausted*, because the remedies are completely different and a single "connection failed" message sends people down the wrong one.

### 7.5 Say what "local-first" actually means

The onboarding walkthrough (§15) and the README must state the boundary in one sentence, because "local" will otherwise be read as "nothing leaves my machine" and that is not true: **your repository stays on your machine — files, build output, git history, the toolchain. The prompt the agent builds does go to the India Post model endpoint, over internal infrastructure only.** Getting ahead of that is cheap; being caught out by it during a security review is not.

### 7.6 Token meter

Contract C5 gives a `usage` event per turn. Show a compact live meter in the chat header:

```
18.2k / 32k context · 1.4k reasoning · cache 84%
```

- **Context** is the input budget (Part A §6.1).
- **Reasoning** appears only when the current mode has thinking on (Planner, Debugger — Part A §4.4). Making it visible is how a developer learns that "plan this carefully" is not free, and it is the fastest way to spot a mode whose reasoning budget is misconfigured.
- **Cache** renders only when the server reports `cached_tokens`; it is currently absent from the endpoint (plan.md §9 Q1), so the client must treat it as optional and simply omit the segment rather than showing `cache 0%`, which would read as a failure rather than an unknown.

---

## 8. Chat surface

Fork `chatView.ts` and keep what works: streamed deltas folded into a single bubble, markdown with copy-able code blocks, `@file` typeahead over workspace files, slash commands, the elapsed-time working indicator with the current tool name, the in-composer Stop, message steering (a message typed during a run queues as a follow-up turn), "Show more" clamping on tall blocks, `workspaceState` persistence across reloads, and the `aria-live="polite"` log region.

### 8.1 Refactor the webview out of a template string

1,340 lines with inline HTML/CSS/JS in a template literal is already at its limit and this plan adds a context inspector, a quota panel, a token meter and a scaffold wizard. Move to `media/chat/{index.html,chat.css,chat.js}` loaded via `asWebviewUri`, with a strict CSP and a nonce. Keep zero runtime dependencies and esbuild bundling; do not introduce a UI framework for this.

### 8.2 Go-flavoured slash commands

`/scaffold` (resource wizard) · `/service` (greenfield) · `/audit` (template compliance) · `/legacy` (legacy audit) · `/migrate` · `/debug` · `/explain` · `/fix` (act on the selected diagnostic) · `/test` · `/wire` (FX registration) · `/compact` · `/rule <id>`.

### 8.3 Go-aware `@` mentions

Extend `@file` with symbol and package mentions backed by `gopls`:

- `@handler/user.go` — file (today)
- `@#UserHandler` — symbol, via `go_search`
- `@pkg:repo/postgres` — package API, via `go_package_api`
- `@build` — the last `go build` output
- `@diag` — current Problems-panel diagnostics for the active file

Symbol and package mentions send *references*, not content — the just-in-time retrieval principle (Part A §6.6). A `@pkg:` mention costs tens of tokens where attaching the package's files would cost thousands.

### 8.4 Plan rendering

Contract C2's `plan` event is structured (goal, files in scope, steps with `Accepts:`). Render it as a live checklist with per-step status (pending → running → passed → failed), each step's file paths clickable. During a long run this checklist is the developer's only real answer to "is it making progress?", and it is what makes a 90-second task feel short.

---

## 9. Diff and approval

Keep `postgen`'s approval card — Accept / Reject / **Show Diff** / **Edit args** — and its policy set (`none` / `all` / `write_side` (default) / `destructive` / an explicit tool list), plus `autoApproveTrivialPatches` with its strict heuristics. The `edit` decision is the standout: correcting the agent's arguments beats rejecting and re-prompting, and it keeps the developer in the loop without costing a turn.

Go-specific additions:

- **Native diff, not a webview blob.** Render `patch_file` and `write_file` previews through `vscode.diff` against a virtual document so the developer gets real syntax highlighting, inline navigation, and folding on Go source.
- **Multi-file review for scaffolds.** `resource_scaffold` writes seven files in one call. Present them as one reviewable changeset in a `SourceControlResourceGroup`-style list with a single Accept-all and per-file toggles — approving seven cards in sequence is the wrong ceremony for one logical action.
- **Protected-path badges.** When a diff touches `go.mod`, `bootstrap/**`, `configs/**`, `db/**`, or `*_validator.go`, label it and say why (Part A §7.2 note ¹). `*_validator.go` specifically should say "generated — the agent should run `govalid_gen` instead", because a hand-edit there is a rule violation the developer can catch faster than the linter can.
- **Never auto-approve** anything under `configs/**`, regardless of size. Those files contain credentials (plan.md §6).

---

## 10. Context controls in the UI

The server owns context (contract C5). The extension makes it *legible*, which is what turns an opaque slowdown into something a developer can act on.

### 10.1 Live meter

§7.6. Header line: `18.2k / 32k context · 1.4k reasoning · cache 84%`.

### 10.2 Context Inspector

A read-only view, opened on demand, showing what the server currently holds — sourced entirely from `usage` and `gate` events, never reconstructed client-side:

```
Context — session a1b2c3 · 18,240 / 32,768 tokens (56%)
  system + tools                    2,380   cached ✓
  task + plan                         610   pinned
  recap (turns 1–14, compacted)     1,740
  working set                      13,510
    handler/pension.go:1-142        1,980   ← freshest read
    repo/postgres/pension.go:1-118  1,640
    go build output                 2,210   3 errors preserved
    rules_lint (4 violations)         890
    … 6 more
  Compactions this session: 1 (at turn 14)
```

This is the diagnostic that lets a developer see *why* a session got slow — usually one enormous file read or a `repo_map` on a vendored repo — and act on it.

### 10.3 Manual compaction

`/compact` and `dakcoder: Compact Context` trigger a server-side compaction, with the resulting recap shown collapsed in the transcript so the developer can verify nothing important was lost. A visible recap is also the fastest way to spot a bad summary before it misleads the next ten turns.

### 10.4 No client-side context knobs

Retire `postgen.contextMaxMessages`. A message *count* is the wrong unit — 40 messages can be 5k tokens or 200k — and the decision belongs to the server's budget manager, which knows the token counts. If a per-user override is ever justified it should be a server-side policy keyed to the user's role, not a client setting.

---

## 11. Go-specific editor surfaces

This is where the extension earns its place over a chat window in a browser.

### 11.1 Diagnostics

Publish `rules_lint` and `legacy_audit` findings as real `vscode.Diagnostic`s in a `DiagnosticCollection`, with the rule id as `code` and a `codeDescription` URI pointing at the `skill.md` citation. They become navigable, filterable, and quick-fixable, and they show up in the editor gutter where the developer already looks. `gopls` diagnostics come from the `golang.go` extension already — do not duplicate them; read them via `vscode.languages.getDiagnostics` when the agent needs them.

### 11.2 Code actions

| Trigger | Action |
|---|---|
| `rules_lint` diagnostic | *Fix with dakcoder* — sends the rule id, path and range as a scoped Debugger task |
| any `rules_lint` diagnostic | *Explain this rule* — fetches the `skill.md` section into a peek view |
| `legacy_audit` diagnostic | *Migrate this handler* — starts one unit of the migration flow (§11.4) |
| Go compile error | *Debug with dakcoder* — feeds the exact error into Debugger mode |
| A new `*Handler` or `*Repository` type not in `bootstrap/bootstrapper.go` | *Wire into FX* — calls `fx_wire` |
| A `validate` tag edited in `handler/request.go` | *Regenerate validators* — calls `govalid_gen` |
| Selection in a handler containing SQL | *Extract to repository* — a scoped `layer-sql-boundary` fix |

### 11.3 Scaffold wizard

A multi-step quick-pick producing the resource spec (Part A §10.1) without the developer typing JSON: resource name → plural (inferred, editable) → table name (inferred) → route base (inferred) → fields (name / Go type / json / validate / SQL type, repeatable, with a table-preview) → operations (multi-select) → list filters. Then a review pane showing the spec and the seven files that will be written, and one confirm.

Two reasons this is worth building rather than leaving to chat: the spec is exactly the structured input the deterministic scaffolder wants, so the LLM never has to guess it; and reviewing a field table is far faster than re-reading a prose instruction to check whether `Amount` ended up `float64` or `string`.

### 11.4 Migration plan viewer

The migration mode (Part A §12.2) keeps its state in `.dakcoder/migration/plan.md`. Render it as a checklist view: each handler/repo with its classification, its violated rules, its status, and its commit link once migrated. Support reordering and reclassifying before the run starts — Part A §12.2 step 2 makes that confirmation mandatory, and this is its UI.

### 11.5 Swagger preview

After a route change, `swagger_check` can boot the service and read `/docs/v3Doc.json`. Offer *Preview API docs* — a webview rendering the OpenAPI document, with the newly added routes highlighted. It closes the loop on the one acceptance criterion a developer cannot verify by reading the diff.

### 11.6 Test and build integration

Surface `go_test` failures through the standard `vscode.tests` API so they appear in the Test Explorer with real navigation, and route `go build` output through a `ProblemMatcher`-compatible task so errors land in the Problems panel rather than only in chat.

---

## 12. Sessions, resume, revert

Keep all of it — this is `postgen`'s strongest reliability story and it is language-agnostic.

- **Sessions tree** with status, task summary, timestamps; open, resume, delete. Add a filter by status and by workspace.
- **Resume** for `escalated` / `failed` / `max_turns` / `aborted`; **conversation follow-up** for finished sessions (server replays the transcript, the new message becomes the next turn).
- **Revert** — restore every path a session touched to git HEAD, deleting files with no baseline. Keep the guard that refuses to revert a running session. Add a confirmation listing the exact paths, because "revert my last task" is easy to fire by accident.
- **Abort** — sets the cancel signal, honoured mid-stream and before each tool call in a batch. Keep both checks; they exist because of real "stopped but kept moving" bug reports.

---

## 13. Settings

| Setting | Default | Notes |
|---|---|---|
| `dakcoder.mode` | `local` | `local` \| `server` |
| `dakcoder.serverGatewayUrl` | `https://aiops.cept.gov.in/coder/backend` | Used when `mode = server` |
| `dakcoder.gatewayUrl` | *(empty)* | Advanced override, both modes |
| ~~`dakcoder.modelBaseUrl`~~ | — | **Not a client setting.** Model traffic is proxied through the gateway (Part A §15.4), so the extension never learns the model endpoint and never holds the shared API key. A client-side model URL or key field would be an unmetered bypass around quota, so neither exists. |
| ~~`dakcoder.modelApiKey`~~ | — | **Never.** See above. If someone asks for it, the answer is Part A §15.4. |
| `dakcoder.model` | *(empty)* | Display-only override, and only for a role the server already permits. Empty = the server's configured coder model. The server is authoritative and rejects anything outside its role set. |
| `dakcoder.localPort` | `8765` | Auto-increments on conflict |
| `dakcoder.pythonPath` | *(empty)* | Auto-detect; irrelevant once §4.3 Phase 2 lands |
| `dakcoder.goPath` | *(empty)* | Override the `go` binary |
| `dakcoder.goplsPath` | *(empty)* | Override; empty = discovery order in §4.5 |
| `dakcoder.requireApproval` | `write_side` | `none` \| `all` \| `write_side` \| `destructive` \| `string[]` |
| `dakcoder.approvalTimeoutSeconds` | `0` | 0 = wait indefinitely. Keep this default — a slow review must never auto-reject |
| `dakcoder.autoApproveTrivialPatches` | `false` | Never applies under `configs/**` |
| `dakcoder.defaultMode` | `multi` | |
| `dakcoder.requestTimeoutSeconds` | `600` | |
| `dakcoder.runtimeIdleTimeoutMinutes` | `30` | 0 = never |
| `dakcoder.showTokenMeter` | `true` | |
| `dakcoder.verbose` | `false` | |
| `dakcoder.telemetry` | `true` | Honours `vscode.env.isTelemetryEnabled` regardless |
| ~~`contextMaxMessages`~~ | — | **Retired** — §10.4 |

Fifteen settings was already near the limit of what anyone reads. Every addition here is load-bearing; resist more.

---

## 14. Extension performance budget

The extension host is shared with every other extension in the window. Treat these as CI-asserted budgets, not aspirations.

| Budget | Target | How |
|---|---|---|
| Activation time | ≤50 ms | `onStartupFinished`; no network, no runtime start, no `go version` at activation (§3.3) |
| Bundle size | ≤400 KB JS | esbuild, no runtime deps, tree-shaken |
| `.vsix` size | ≤80 MB | vendored wheels (§4.3) + `gotools` binaries dominate; ship the host platform's binary and fetch others on demand |
| Idle CPU | ~0% | No polling. Quota refresh only on task boundaries and while a task runs (§7.2) |
| Webview message rate | ≤25/s | Server coalesces deltas (Part A §5, S11); the client additionally batches DOM writes on `requestAnimationFrame` |
| Memory (extension host) | ≤60 MB | Cap transcript retention (500 messages, then archive to `workspaceState`) |
| SSE reconnect | ≤2 s | Reconnect with `Last-Event-ID` and replay from `GET /v1/sessions/{id}/events?since_id=` |

That last row is a real gap in the current client: the SSE parser handles a clean stream well but has no resumption path. A dropped connection today loses the live view of a run that is still executing server-side. Since the server already persists every event and exposes `since_id`, resumption is cheap to add and removes a whole class of "did it die?" confusion.

---

## 15. Packaging and distribution

- **`.vsix` only**, published to the internal GitLab package registry — no public marketplace. The frontend agent already works this way.
- **CI** (mirroring `postgen`'s `smoke.yml`): `tsc --noEmit`, esbuild bundle, `vsce package`, extension integration tests headless, `gotools` cross-compile with checksum manifest, `pip download` of the wheel closure per platform tag.
- **Version pinning across the seam.** The `.vsix` records the `dakcoderd` API version it expects. On connect, mismatch → a clear, actionable message ("this extension needs gateway ≥2.1; `/coder/backend` reports 2.0 — update the extension" with a download link). Silent version skew across a client/server boundary is the failure that costs the most support time.
- **Update path**: `Doctor` checks the registry for a newer `.vsix` (weekly, opt-out) and offers a download link. No auto-update — an internal tool should not install itself without being asked.
- **Onboarding walkthrough** via `contributes.walkthroughs`: sign in → run Doctor → open a Go repo → scaffold a resource → review the diff. Five steps, each with a completion event, so a new pilot user reaches a successful first task without reading documentation.

---

## 16. Accessibility and localisation

Keep `postgen`'s `aria-live="polite"` transcript region and its WCAG audit script (`npm run a11y` + `docs/wcag-audit.md`), and extend both to the new surfaces (quota view, context inspector, scaffold wizard, migration viewer).

Specific requirements: full keyboard reach for every approval action (Accept/Reject/Diff/Edit) with no mouse dependency; focus management that does not steal focus from the editor when a stream starts; `prefers-reduced-motion` honoured by the working indicator; all colour signalling paired with text or an icon (never colour alone); and contrast taken from VS Code theme variables rather than hard-coded, so it holds in high-contrast themes.

Localisation: use `vscode.l10n` and `package.nls.json` from the start. English only at launch, but retrofitting l10n into 3,500 lines of string-literal UI is expensive and Hindi support is a plausible future ask.

---

## 17. Testing

| Layer | Coverage |
|---|---|
| **Unit** (mocha, no VS Code host) | SSE parser (including split frames, malformed frames, `Last-Event-ID` resumption), PKCE verifier/challenge generation, state validation, token refresh single-flight, quota formatting, spec-wizard → JSON, diagnostic mapping |
| **Integration** (`@vscode/test-electron`) | Activation under 50 ms; command registration; runtime spawn against a stub gateway; auth round-trip against a stub IdP; approval accept/reject/edit; revert; diagnostics published and cleared; code actions offered on the right diagnostics |
| **Contract** | Golden fixtures for every C2 event type, asserting the client renders each and **ignores unknown types** — the additive-only guarantee has to be tested or it will be broken |
| **Doctor matrix** | Windows / macOS / Linux × (Go present/absent, `gopls` present/absent, `GOPRIVATE` set/unset, proxy set/unset). This is where local-first either works or does not. |
| **Manual, per release** | Corporate-proxy sign-in; Remote-SSH loopback fallback; two windows sharing one runtime; a 40-turn task with a mid-run reconnect; quota exhaustion mid-task |

---

## 18. Delivery

| Phase | Deliverable | Exit criteria |
|---|---|---|
| **0** | Fork and rename; **vendored wheels (§4.3)**; prewarm on; `Doctor` v1 with the full Go toolchain matrix; `gotools` binary packaging | A new developer on the corporate network reaches a healthy runtime and a green `Doctor` **with no network install and no manual setup**, on all three platforms |
| **1** | GitLab OAuth as a real `AuthenticationProvider` + loopback fallback; quota status bar and view; token meter; streaming chat with plan checklist; native diff approval; sessions/resume/revert; SSE resumption | 5–10 pilot developers signed in with GitLab identities; a resource scaffold reviewed and accepted end to end; zero shared-token paths remain |
| **2** | Scaffold wizard; diagnostics + code actions; `Explain This Rule`; context inspector; Test Explorer and Problems integration; walkthrough | A developer completes a scaffold entirely through the wizard; a `rules_lint` violation is fixed from a lightbulb; the context inspector explains a slow session |
| **3** | Migration plan viewer; swagger preview; server-mode switch; org dashboards; l10n scaffolding; accessibility sign-off | A real `pao`-style service migrated through the viewer; WCAG audit signed off; performance budgets (§14) green in CI |
