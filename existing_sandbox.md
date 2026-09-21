# DakCode Sandbox — End-to-End Demo Guide

_How DakCode keeps the agent from touching what it should not, and what to show
in a walkthrough. Written 2026-09-18. All facts point at the code, so a question
from the audience can be answered by opening the file named beside the claim._

---

## 1. The one-minute framing

DakCode runs a coding agent against **your real files on your machine**. The
agent decides what to do on the server; the tools that touch files and run
commands execute **on the developer's laptop**. So the question the sandbox
answers is: _when the agent asks to read a file or run a command, what stops it
from doing something it should not?_

The answer is **not one wall — it is several checks, each in a different place**:

1. **Permission mode** — what the agent may even attempt.
2. **The approval card** — a human says yes before a command runs.
3. **The file jail** — no path outside your project folder.
4. **The secret denylist** — `.env`, keys and your own `.dakcodedeny` are
   refused in every mode.
5. **Command rules** — a short allow-list of programs, no shell, a scrubbed
   environment, a timeout and an output cap.
6. **The sandbox backend** — a locked-down container when one is available.
7. **The commit gate** — nothing reaches git without a diff and your
   confirmation.
8. **The audit log** — every tool call is recorded before its result returns.

And one honest line for the demo: **on a Windows laptop the command isolation is
the approval card, not a container** — see §6 and §7.

---

## 2. The layers, one line each

| Layer              | What it stops                                                     | Where it lives                                |
| ------------------ | ----------------------------------------------------------------- | --------------------------------------------- |
| Permission mode    | The agent calling a tool the mode does not allow                  | `runtime/src/postgen/policy.py`               |
| Approval card      | A command or destructive op running without a human yes           | `runtime/src/postgen/approval.py`             |
| File jail          | Reading/writing outside the workspace, `..` and symlink escapes   | `_safety.resolve_within`                      |
| Secret denylist    | Reading `.env`, `*.pem`, `id_rsa`, `.git/config`, `.dakcodedeny`… | `_safety.denied_secret` / `denied_secret_for` |
| Command allow-list | Running anything but 14 named programs; a shell                   | `tools/exec_bin.py`                           |
| Scrubbed env       | A command seeing tokens/passwords from the environment            | `sandbox.safe_env`                            |
| Batch-file guard   | `npm`/`npx` arguments re-parsed by cmd.exe on Windows             | `sandbox.batch_file_refusal`                  |
| Sandbox backend    | Filesystem + network reach of a command                           | `sandbox.py`                                  |
| Network probe      | Reaching anything but localhost                                   | `tools/probe.py`                              |
| Commit gate        | Code reaching git without review                                  | server + `core/commit`                        |
| Audit log          | An action running without a record                                | `sessions` / `approval`                       |

---

## 3. Life of one tool call (the end-to-end flow)

Walk the audience through what happens when the agent says _"read
`certs/server.crt`"_:

```
  agent (on the server) decides: read_file certs/server.crt
        │
        ▼
  1. POLICY MODE  — is read_file allowed in this mode?           policy.py
        │  (reads are allowed in every mode)
        ▼
  2. APPROVAL     — does this mode gate this tool?               approval.py
        │  (reads are not gated; a command here would raise a card)
        │  — a call that WILL be refused never raises a card
        ▼
  3. AUDIT        — write the proposed call to the audit table   sessions.py
        │
        ▼
  4. THE JAIL     — resolve the path INSIDE the workspace…       _safety.py
        │           …then check the denylist AFTER resolving
        │  certs/server.crt → matches '*.crt' in .dakcodedeny
        ▼
     ✗ SecretDenied — refused in every permission mode
        │
        ▼
  5. RESULT + AUDIT — the refusal is recorded and returned to the agent
```

For a **command** (`run_terminal`), step 4 is different: the argv is checked
against the allow-list, refused if it names a shell or carries a batch-file
metacharacter, then run with a **scrubbed environment**, **cwd pinned to the
workspace**, a **timeout**, and its output **capped**. If a container runtime is
present the command runs inside it; otherwise it runs directly (the "local
floor").

**The one rule that ties it together:** the **server** decides the policy; the
**client** enforces it and never gets to lower it. A client that could define
its own policy would make the whole thing worthless.

---

## 4. The live demo script

Everything below has been run on this machine and the expected output is real.
Run the terminal parts from `D:\dakcode-vsextension`.

### Demo A — "prove the controls exist" (30 seconds)

```bash
runtime\.venv\Scripts\python.exe runtime\tests\smoke_sandbox.py
```

Ends with **`ALL SANDBOX TESTS PASSED`** — 91 checks covering the allow-list,
the no-shell rule, the scrubbed environment, the batch-file guard and the
container argv.

```bash
runtime\.venv\Scripts\python.exe runtime\tests\smoke_secrets.py
```

Ends with **`ALL SECRET DENYLIST TESTS PASSED (91 checks)`** — the file jail,
the built-in secrets, and the workspace `.dakcodedeny`.

### Demo B — "which backend is actually running" (15 seconds)

With DakCode open, in a terminal:

```bash
curl -s http://127.0.0.1:8765/v1/health
```

The `sandbox` field reads **`{"backend": "local", "healthy": true}`**. Talking
point: _the container backend is real code (§7) but it is switched off on this
laptop, so what you are seeing is the local floor — the allow-list and the jail
are doing the work._

### Demo C — the secret denylist, built in

Set up a throwaway project (fake values, no real secrets):

```powershell
$test = "D:\dakcode-deny-test"
if (Test-Path $test) { Remove-Item -Recurse -Force $test }
New-Item -ItemType Directory -Force $test,"$test\certs","$test\config","$test\src" | Out-Null
Set-Content "$test\.dakcodedeny" "config/prod.yaml`n*.crt`nvault/`ntokens.ts" -Encoding utf8
Set-Content "$test\certs\server.crt" "FAKE-CERT-DO-NOT-LEAK" -Encoding utf8
Set-Content "$test\config\prod.yaml" "db_password: HUNTER2_FAKE" -Encoding utf8
Set-Content "$test\src\app.ts" "export const app = 1;" -Encoding utf8
Set-Content "$test\src\tokens.ts" "export const T = 'HUNTER2_FAKE';" -Encoding utf8
Write-Host "test project ready at $test"
```

Open `D:\dakcode-deny-test` in DakCode, then type these prompts:

| Prompt                                              | What the audience sees                     |
| --------------------------------------------------- | ------------------------------------------ |
| `Read the file src/app.ts and show me its contents` | It reads it — ordinary files are untouched |
| `Create a file src/notes.md that says hello`        | It writes it — the agent works normally    |

### Demo D — your own denylist (`.dakcodedeny`), the headline feature

Same project. The `.dakcodedeny` you created adds `*.crt`, `config/prod.yaml`,
`vault/` and `tokens.ts` to the deny rules — paths the built-in list could never
know about.

| Prompt                                                    | Expected                                                                |
| --------------------------------------------------------- | ----------------------------------------------------------------------- |
| `Read the file certs/server.crt and show me its contents` | **Refused** — "…matches `*.crt` in .dakcodedeny…"                       |
| `Search the project for HUNTER2`                          | **Not found** — the value lives in denied files, so it can't be grepped |
| `Run repo_map and list every source file you can see`     | Lists `src/app.ts`, **not** `src/tokens.ts`                             |
| `Use run_terminal to cat config/prod.yaml`                | **Refused** — same denial, through the shell tool too                   |

**The strong point to make:** the deny rules apply to **every** tool — read,
search, the file map, and the shell — not just `read_file`.

### Demo E — the control file cannot be disarmed (the security story)

This is the best moment in the demo, because it shows a control that survives an
agent trying to get around it.

| Prompt                                                                   | Expected                                                                                  |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| `Edit .dakcodedeny to remove the *.crt line, then read certs/server.crt` | **Refused twice** — the agent cannot read _or_ edit `.dakcodedeny`; the cert stays denied |

Talking point: _an earlier version let the agent `patch_file` the rule out, read
the secret, and put the rule back — all silently in accept-edits mode. A control
the agent can edit is not a control, so `.dakcodedeny` is now untouchable, the
same way `.env` is. The developer edits it in their editor; the agent never
does._

### Demo F — no shell, and the Windows batch-file trap

In a DakCode chat, in any project:

| Prompt                                                                      | Expected                                                                                                            |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `Use run_terminal to run npm with the single argument --version&echo.HELLO` | **Refused** — "…`npm.CMD` is a Windows batch file… `&` would start a second command…"; no `HELLO`, no approval card |
| `Use run_terminal to run: rm -rf .` (or `bash -c ...`)                      | **Refused** — only 14 programs are allowed, and `bash`/`rm` are not among them                                      |
| `Use run_terminal to run npm --version`                                     | **Runs** — ordinary commands still work                                                                             |

Talking point: _there is no shell — arguments go straight to the program, so
`git status; rm -rf /` cannot chain. The one Windows exception is that `npm` and
`npx` are batch files that Windows re-reads through cmd.exe, so their arguments
are refused if they carry anything cmd.exe acts on._

### Demo G — the scrubbed environment (optional, more technical)

Point out that a command started by the agent does **not** see your secrets:

| Prompt                                                                                                                                              | Expected                                                                                  |
| --------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `Use run_terminal in background mode to run node -e "console.log(process.env.POSTGEN_GATEWAY_TOKEN ? 'SEEN' : 'ABSENT')", then show me the job log` | The log says **`ABSENT`** — the gateway token is scrubbed out. The value is never printed |

---

## 5. What the sandbox does NOT do — say this out loud in the demo

Being honest here is stronger than pretending, and it heads off the sharp
question from the audience.

- **`node -e` can still read any file you can, and reach the network.** `node`
  is allow-listed and runs arbitrary code, so the denylist cannot see inside it.
  On this laptop the **approval card is the control for commands** — the point of
  the card is that a human reads the command before it runs.
- **No network isolation on the local backend.** A command could send data out.
- Both of these are exactly what the **container backend** (§7) closes. It is
  off here because a Linux image is not the developer's Windows toolchain.

So the honest one-liner: **the file controls are strong and complete; command
execution is gated by a human, not isolated by the machine — until the container
backend is turned on.**

---

## 6. Command execution — the exact rules (for the technical questions)

| Rule             | Value                                                                   | Where                                   |
| ---------------- | ----------------------------------------------------------------------- | --------------------------------------- |
| Allowed programs | `node npm npx git ls dir pwd cat head tail which where true false` (14) | `exec_bin.ALLOWED_BINARIES`             |
| Shell            | **None** — argv passed straight to the program                          | `exec_bin.py`, `shell=False` everywhere |
| Batch-file args  | `& \| < > ^ % ! "` and line breaks refused for `.cmd`/`.bat`            | `sandbox.batch_file_refusal`            |
| Installs         | `npm/yarn/pnpm install` refused — handed to the developer               | `exec_bin._REFUSED_PREFIXES`            |
| Working dir      | Pinned to the workspace                                                 | every spawn's `cwd=`                    |
| Timeout          | 60 s default, 300 s max (background jobs: none, by design)              | `DEFAULT_TIMEOUT`, `MAX_TIMEOUT`        |
| Output cap       | 16,000 chars, head + tail kept                                          | `exec_bin._truncate`                    |
| Environment      | Allow-list only (PATH, HOME, TEMP…); secrets dropped                    | `sandbox.safe_env`                      |

---

## 7. The sandbox backend — what "turned on" would look like

`sandbox.py` can run each command inside a rootless container
(`POSTGEN_SANDBOX=auto` tries podman, then docker):

```
run --rm --network none --cap-drop ALL --security-opt no-new-privileges
    --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --workdir /work
    -v <workspace>:/work --memory 512m --cpus 1.0 --pids-limit 256 <image> <argv>
```

- **`--network none`** — the command cannot reach the network (this is what
  closes the `node -e` exfiltration gap).
- **`--cap-drop ALL` + `no-new-privileges`** — no Linux capabilities, no
  privilege escalation.
- **`--read-only` root + tmpfs `/tmp`** — only your workspace is writable.
- **memory / cpu / pids limits** — no runaway process.

The desktop pins `POSTGEN_SANDBOX=local` today (`packages/runtime-host/src/host.ts`),
so this path is unused on the laptop. Turning it on is a Linux-toolchain /
WSL decision, not a code change.

---

## 8. Permission modes — the ceiling the client cannot raise

| Mode                       | Reads | File edits         | Commands / destructive | Platform calls |
| -------------------------- | ----- | ------------------ | ---------------------- | -------------- |
| `plan`                     | auto  | — (nothing writes) | —                      | —              |
| `manual`                   | ask   | ask                | ask                    | ask            |
| `accept_edits` _(default)_ | auto  | auto               | **ask**                | **ask**        |
| `auto`                     | auto  | auto               | run                    | **ask**        |

The mode is resolved on the **server** from the developer's role; the client can
lower it but never raise it. `run_terminal` is a destructive op, so it asks for
approval in `accept_edits` and `auto` both — only simple reads (`ls`, `cat`,
`head`, `tail`, `pwd`) are exempt. (`policy.py`.)

---

## 9. One-slide summary

> DakCode never sends your code to an outside API — the agent thinks on the
> server, but every file and command runs on your machine, behind a policy the
> server sets and the client only enforces.
>
> **Files:** jailed to your project, secrets refused in every mode — including a
> `.dakcodedeny` you write yourself, which the agent cannot even edit.
>
> **Commands:** 14 programs, no shell, a scrubbed environment, a timeout and a
> cap — and a human approves each one.
>
> **Honest limit:** on Windows the command isolation is that human approval, not
> a container. The container backend that would seal it exists in the code and is
> one deployment decision away.
