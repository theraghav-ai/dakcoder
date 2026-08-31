---
slug: legacy-patterns
handle: "@skill:legacy-patterns"
fetch_when: "auditing or migrating a pre-template (api-* generation) service"
generated_from: legacy-rules
---

# Legacy patterns and their replacements

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

The concrete differences between the `api-*` generation and the current `n-api-*` template, each with what to do about it.

Most real IT 2.0 Go code looks like the former. Run `legacy_audit` to find these in a specific service rather than reading for them.

Generated from the legacy rule set, so this table and the audit cannot disagree.

| Rule | Blocks | Detects |
|---|---|---|
| `legacy-committed-artifacts` |  | build, log or coverage output is committed |
| `legacy-gin-handler` | yes | gin appears in handler or repo code |
| `legacy-go-work` |  | go.work is committed; template services are single-module |
| `legacy-handmade-health` |  | hand-written health handler instead of server.healthcheck config |
| `legacy-lib-generation` | yes | service uses the api-* library generation instead of n-api-* |
| `legacy-manual-validation` | yes | hand-written validator service instead of generated govalid validators |
| `legacy-response-helper` | yes | handleSuccess()-style helper instead of port.StatusCodeAndMessage envelopes |
| `legacy-routes-file` | yes | a routes/ package exists; routes belong in each handler's Routes() |
| `legacy-swaggo` |  | swaggo generates the API docs instead of the framework |

Run `legacy_audit` against a service to see which of these it trips, with a file and line for each.
