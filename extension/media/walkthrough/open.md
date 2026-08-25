## Open a Go service

dakcoder knows the **n-api-template** contract: the handler/repository split, FX
wiring in `bootstrap/`, generated validators, and the rules in `skill.md`.

Open a repository built on that template. If the module still uses the older
`api-*` libraries, run **Audit Legacy Patterns** — dakcoder will tell you what
differs and can migrate it a handler at a time.
