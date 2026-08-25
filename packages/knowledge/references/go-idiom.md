---
slug: go-idiom
handle: "@skill:go-idiom"
fetch_when: "general Go style questions — sits under the template rules, not over them"
generated_from: idiom-rules
---

# Idiomatic Go, the checkable subset

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

The part of the house Go style that is machine-checked. It sits deliberately *under* the template rules: a service that is idiomatic but off-template is a much bigger problem than one that is on-template and slightly unidiomatic.

All of it is advisory except a mismatched package declaration, which does not compile.

Generated from the rule set. `golangci-lint` covers far more at the verification gate; this is what is cheap enough to check after every edit.

## `go-idiom`

idiomatic Go: any over interface{}, lower-case unpunctuated error strings, one package per directory

Source: go.instructions.md §Naming Conventions, §Error Handling, §Type Definitions

## What it checks

- `any` rather than the written-out `interface{}` (Go 1.18+).
- Error strings lower-case and unpunctuated — they get wrapped, and `open config: Could not read file.` is what a capitalised, punctuated inner message turns into.
- `fmt.Errorf` wrapping an error with `%w` rather than `%v`. In this template the consequence is concrete: `pgx.ErrNoRows` has to survive the trip up to the framework to become a 404, and `%v` severs it.
- One package per directory. **This one blocks** — it does not compile, and the compiler's version of the message names a directory rather than the file that introduced the mismatch.
- Package names lower-case and single-word.

## What it deliberately does not check

Anything needing type information — unchecked errors, missing `defer` close, nil interface versus nil pointer. Those are `golangci-lint`'s at the verification gate, where there is time for them.
