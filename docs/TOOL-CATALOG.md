# Tool catalogue — the model-facing contract

> **Generated.** Do not edit. Run `make tool-catalog` and commit the result.
> Regenerating is how this file stays true; editing it is how it stops being.

Contract **C1** (plan.md §7) in full: every tool the model can be offered, which modes see it, and what it costs to run.

C1 limits: at most **6 parameters** per tool, description at most **200 characters**, written as an instruction to the model rather than documentation for a human. Enforced when the registry is imported, so a violating tool cannot reach a test run.

`gotools` publishes the *sidecar's* schemas separately. The two differ on purpose — `rules_lint` takes a comma-separated string here and an array there — because only this side is bound by the six-parameter limit.

## What each mode is offered

Mode filtering is a guarantee, not a hint: a tool absent from this table is absent from that mode's schema list *and* refused by the router if called anyway.

| Mode | Tools | Schema cost |
|---|---|---|
| **planner** | 13 | ~1,414 tokens |
| **scaffolder** | 11 | ~1,313 tokens |
| **coder** | 17 | ~1,992 tokens |
| **verifier** | 11 | ~1,203 tokens |
| **debugger** | 19 | ~2,203 tokens |

## The catalogue

| Tool | Modes | Mutates | Approval | Runs in | Description |
|---|---|---|---|---|---|
| `repo_map` | CDPSV |  |  | gotools | Get the module path, package tree, exported symbols and FX providers. Call this first in an unfamiliar repository. Pass package to see one directory in full. |
| `read_file` | CDPSV |  |  | agent | Read a slice of one file. Always pass start and end when you know roughly where to look; whole-file reads crowd out everything else in context. |
| `search_repo` | CDPV |  |  | agent | Search file contents by regular expression. Use this instead of grep, and prefer it over reading files to find something. |
| `search_docs` | CDPS |  |  | agent | Search the n-api-template knowledge base for the contract rule behind a pattern. Use it before inventing an approach, not after. |
| `go_symbols` | CDPV |  |  | gopls | Find a symbol's definition, references or package API through gopls. Use this rather than searching for a name textually. _(not yet available: gopls is not yet wired (Part A section 8.3). Use search_repo, or go_build for type errors.)_ |
| `go_diagnostics` | CDV |  |  | gopls | Type-check the workspace incrementally and report errors. This is the fast inner-loop signal; run it after every edit batch. _(not yet available: gopls is not yet wired (Part A section 8.3). Use go_build, which is authoritative but takes about four seconds.)_ |
| `rules_lint` | CDPV |  |  | gotools | Check Go against the n-api-template contract: layer boundaries, handler signatures, repository idiom, FX registration. Pass paths to scope it. |
| `legacy_audit` | PV |  |  | gotools | Detect pre-template patterns in an existing service: routes.go, gin, hand-rolled SQL builders, manual validation. Run before migrating. |
| `db_roundtrip_audit` | PV |  |  | gotools | Profile every repository method: database calls, any inside a loop, batched, in a transaction, with a verdict. Worst first. Use before optimising by eye. |
| `validation_audit` | CPV |  |  | gotools | List every request field, its validate tag, and what the tag leaves unbounded. `required` alone means only 'not empty', so a 10MB string passes. |
| `temporal_audit` | P |  |  | gotools | List inline work that may belong off the request path: uploads, SMS, email, reports, outbound calls. Candidates only — it makes no recommendation. |
| `lib_version_check` | P |  |  | gotools | Report CEPT library drift: which are behind, which are superseded by n-api-*. Reports only — never edit go.mod on it, tell the user. |
| `playbook` | CDV |  |  | agent | Get the known-good fix procedure for a failure class or rule id. Consult this before attempting a fix you have not made before. |
| `write_file` | CDS | ✓ | if protected | agent | Create a new file. Refuses to overwrite an existing one — use patch_file for that. Write complete, compiling Go, not a sketch. |
| `patch_file` | CD | ✓ | if protected | agent | Replace an exact unique string in a file. Include enough surrounding lines to make old unique; the call fails rather than guessing. |
| `delete_file` | CD | ✓ | **always** | agent | Delete a file. Always needs the developer's approval; say why in reason. |
| `gofmt` | gate | ✓ |  | agent | Format Go files and fix their imports. Runs automatically after edits; call it directly only to clean up a file you did not just touch. |
| `resource_scaffold` | S | ✓ | **always** | gotools | Write a whole CRUD resource — domain, DDL, repository, DTOs, handler — from a field spec. Produce the spec; the templates produce the code. |
| `project_scaffold` | S | ✓ | **always** | gotools | Create a new n-api-template service in an empty directory, with configs, bootstrap and one working resource. Credential fields are left empty. |
| `fx_wire` | CDS | ✓ |  | gotools | Register a repository or handler in bootstrap/bootstrapper.go with the right annotations. Run this after adding either, or FX fails at startup. |
| `govalid_gen` | CDS | ✓ |  | agent | Regenerate handler/request_*_validator.go from the request structs. Run it whenever a validate tag changes; never hand-edit the generated files. |
| `go_build` | CDV |  |  | agent | Build every package. This is the authoritative signal: nothing is done until it is clean, whatever the other tools say. |
| `go_vet` | D |  |  | agent | Run go vet over the workspace. Gate only — it takes about thirty seconds, so never run it in the edit loop. |
| `go_test` | D |  |  | agent | Run tests. Pass pattern to scope to one package when output is large. |
| `golangci_lint` | gate |  |  | agent | Run golangci-lint if the repository configures it. Advisory: its findings never block, so do not spend turns on them. |
| `govulncheck` | gate |  |  | agent | Scan dependencies for known vulnerabilities. Run it on a new service and after any dependency change, not routinely. |
| `swagger_check` | gate |  |  | agent | Check that routes are named and swagger generation is enabled, so endpoints reach /docs/v3Doc.json. This checks; it does not generate. |
| `go_mod` | CD | ✓ | if protected | agent | Run tidy, or add a dependency. Tidy is free and must be a no-op at the gate; adding a direct dependency needs approval. |
| `git_status` | CDPSV |  |  | agent | List changed, staged and untracked files. Cheap; use it to confirm what you changed. |
| `git_diff` | CDPSV |  |  | agent | Show the diff of the working tree, or of one path. Read this before claiming a change is done. |
| `git_blame` | DP |  |  | agent | Show who last changed each line of a file, and when. Use it to date a legacy pattern. |
| `git_ops` | CS | ✓ | if protected | agent | Stage, commit, or switch to the session branch. Never pushes and never rewrites history, so nothing here can lose committed work. |
| `run_terminal` | D |  | if protected | agent | Run one allow-listed binary with explicit arguments. There is no shell, so no pipes, globs or redirection. Prefer a purpose-built tool. |

Modes: **P**lanner · **C**oder · **S**caffolder · **V**erifier · **D**ebugger. `gate` means the verification gate runs it on a fixed schedule and the model never chooses it (Part A §9.3).

## Parameters

### `repo_map`

Get the module path, package tree, exported symbols and FX providers. Call this first in an unfamiliar repository. Pass package to see one directory in full.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `package` | string |  | Directory to expand in full, e.g. 'handler'. Omit for the whole tree. |
| `max_tokens` | integer |  | Budget for the map. Defaults to 4000. |

### `read_file`

Read a slice of one file. Always pass start and end when you know roughly where to look; whole-file reads crowd out everything else in context.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes | Workspace-relative path, e.g. 'handler/user.go'. |
| `start` | integer |  | First line, 1-based. |
| `end` | integer |  | Last line, inclusive. |

### `search_repo`

Search file contents by regular expression. Use this instead of grep, and prefer it over reading files to find something.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `pattern` | string | yes | Regular expression, e.g. 'func .*Handler.*Routes'. |
| `glob` | string |  | Restrict to matching paths, e.g. 'handler/**/*.go'. |
| `max` | integer |  | Maximum matches to return. Defaults to 40. |

### `search_docs`

Search the n-api-template knowledge base for the contract rule behind a pattern. Use it before inventing an approach, not after.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | What you need to know, e.g. 'repository timeout constants'. |

### `go_symbols`

Find a symbol's definition, references or package API through gopls. Use this rather than searching for a name textually.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Symbol or package, e.g. 'serverHandler.Handler'. |
| `kind` | string (search \| references \| package_api) |  | One of: search, references, package_api. |

### `go_diagnostics`

Type-check the workspace incrementally and report errors. This is the fast inner-loop signal; run it after every edit batch.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string |  | Narrow to one file. Omit for the whole workspace. |

### `rules_lint`

Check Go against the n-api-template contract: layer boundaries, handler signatures, repository idiom, FX registration. Pass paths to scope it.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `paths` | string |  | Comma-separated paths to lint. Omit for the whole workspace. |
| `only` | string |  | Comma-separated rule ids, e.g. 'layer-sql-boundary,handler-signature'. |

### `legacy_audit`

Detect pre-template patterns in an existing service: routes.go, gin, hand-rolled SQL builders, manual validation. Run before migrating.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `paths` | string |  | Comma-separated paths to audit. Omit for the whole workspace. |

### `db_roundtrip_audit`

Profile every repository method: database calls, any inside a loop, batched, in a transaction, with a verdict. Worst first. Use before optimising by eye.

_No parameters._

### `validation_audit`

List every request field, its validate tag, and what the tag leaves unbounded. `required` alone means only 'not empty', so a 10MB string passes.

_No parameters._

### `temporal_audit`

List inline work that may belong off the request path: uploads, SMS, email, reports, outbound calls. Candidates only — it makes no recommendation.

_No parameters._

### `lib_version_check`

Report CEPT library drift: which are behind, which are superseded by n-api-*. Reports only — never edit go.mod on it, tell the user.

_No parameters._

### `playbook`

Get the known-good fix procedure for a failure class or rule id. Consult this before attempting a fix you have not made before.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `rule` | string |  | Rule id or failure class, e.g. 'fx-registration' or 'pgx-no-rows'. |

### `write_file`

Create a new file. Refuses to overwrite an existing one — use patch_file for that. Write complete, compiling Go, not a sketch.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes | Workspace-relative path for the new file. |
| `content` | string | yes | Full file content. |

### `patch_file`

Replace an exact unique string in a file. Include enough surrounding lines to make old unique; the call fails rather than guessing.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes | Workspace-relative path of the file to change. |
| `old` | string | yes | Exact text to replace, unique within the file. |
| `new` | string | yes | Replacement text. |

### `delete_file`

Delete a file. Always needs the developer's approval; say why in reason.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes | Workspace-relative path to delete. |
| `reason` | string | yes | One sentence on why it should go. |

### `gofmt`

Format Go files and fix their imports. Runs automatically after edits; call it directly only to clean up a file you did not just touch.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `paths` | string |  | Comma-separated paths. Omit for files changed this session. |

### `resource_scaffold`

Write a whole CRUD resource — domain, DDL, repository, DTOs, handler — from a field spec. Produce the spec; the templates produce the code.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `spec` | string | yes | Resource as JSON: {"name","table","route_base","fields","operations"}. |

### `project_scaffold`

Create a new n-api-template service in an empty directory, with configs, bootstrap and one working resource. Credential fields are left empty.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | Service as JSON: {"module": "gitlab.cept.gov.in/it-2.0/x-api"}. |
| `resource` | string | yes | One resource to seed the service with, same shape as resource_scaffold. |

### `fx_wire`

Register a repository or handler in bootstrap/bootstrapper.go with the right annotations. Run this after adding either, or FX fails at startup.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `kind` | string (repo \| handler) | yes | Either 'repo' or 'handler'. |
| `ctor` | string | yes | The constructor's bare name, e.g. 'NewPensionHandler'. |

### `govalid_gen`

Regenerate handler/request_*_validator.go from the request structs. Run it whenever a validate tag changes; never hand-edit the generated files.

_No parameters._

### `go_build`

Build every package. This is the authoritative signal: nothing is done until it is clean, whatever the other tools say.

_No parameters._

### `go_vet`

Run go vet over the workspace. Gate only — it takes about thirty seconds, so never run it in the edit loop.

_No parameters._

### `go_test`

Run tests. Pass pattern to scope to one package when output is large.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `pattern` | string |  | Package pattern, e.g. './handler/...'. Omit for './...'. |
| `run` | string |  | Regular expression for -run, e.g. 'TestCreatePension'. |

### `golangci_lint`

Run golangci-lint if the repository configures it. Advisory: its findings never block, so do not spend turns on them.

_No parameters._

### `govulncheck`

Scan dependencies for known vulnerabilities. Run it on a new service and after any dependency change, not routinely.

_No parameters._

### `swagger_check`

Check that routes are named and swagger generation is enabled, so endpoints reach /docs/v3Doc.json. This checks; it does not generate.

_No parameters._

### `go_mod`

Run tidy, or add a dependency. Tidy is free and must be a no-op at the gate; adding a direct dependency needs approval.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `op` | string (tidy \| get \| why) | yes | One of: tidy, get, why. |
| `pkg` | string |  | Module path, for get and why. |
| `version` | string |  | Version for get, e.g. 'v1.4.0'. Omit for latest. |

### `git_status`

List changed, staged and untracked files. Cheap; use it to confirm what you changed.

_No parameters._

### `git_diff`

Show the diff of the working tree, or of one path. Read this before claiming a change is done.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string |  | Limit the diff to one path. |
| `staged` | string |  | Pass 'true' to diff the index instead of the working tree. |

### `git_blame`

Show who last changed each line of a file, and when. Use it to date a legacy pattern.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes | Workspace-relative path. |
| `start` | integer |  | First line. |
| `end` | integer |  | Last line. |

### `git_ops`

Stage, commit, or switch to the session branch. Never pushes and never rewrites history, so nothing here can lose committed work.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `op` | string (branch \| add \| commit) | yes | One of: branch, add, commit. |
| `paths` | string |  | Comma-separated paths for add. Omit to stage tracked changes. |
| `message` | string |  | Commit message, for commit. |

### `run_terminal`

Run one allow-listed binary with explicit arguments. There is no shell, so no pipes, globs or redirection. Prefer a purpose-built tool.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `argv` | string | yes | Command and arguments, JSON array, e.g. '["go","env","GOPRIVATE"]'. |
| `timeout` | integer |  | Seconds before it is killed. Defaults to 60. |
