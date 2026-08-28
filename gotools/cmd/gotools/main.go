// Command gotools is the Go-native analysis sidecar for the dakcoder
// backend coding agent.
//
// It runs two ways from one binary:
//
//	gotools lint       — a CLI linter, for developers and CI
//	gotools mcp        — an MCP server over stdio, for the agent
//
// The scaffolders (resource-scaffold, project-scaffold, fx-wire) are available
// both ways for the same reason: a developer should be able to run by hand
// exactly what the agent runs, and compare the results.
//
// Both call the same analysis entry point, so the agent and CI can never
// disagree about what a violation is.
//
// Exit codes follow the linter convention: 0 clean, 1 violations found,
// 2 the tool itself failed. CI can therefore distinguish "your code is wrong"
// from "the linter is broken", which a single non-zero code cannot.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/mcpserver"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/rules"
)

// Version is stamped at build time:
//
//	go build -ldflags "-X main.Version=$(git describe --tags --always)"
var Version = "dev"

const (
	exitOK       = 0
	exitFindings = 1
	exitError    = 2
)

func main() { os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr)) }

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		usage(stderr)
		return exitError
	}
	switch args[0] {
	case "lint":
		return cmdLint(args[1:], stdout, stderr, false)
	case "legacy-audit":
		return cmdLint(args[1:], stdout, stderr, true)
	case "resource-scaffold":
		return cmdResourceScaffold(args[1:], stdin, stdout, stderr)
	case "project-scaffold":
		return cmdProjectScaffold(args[1:], stdin, stdout, stderr)
	case "fx-wire":
		return cmdFxWire(args[1:], stdout, stderr)
	case "repo-map":
		return cmdRepoMap(args[1:], stdout, stderr)
	case "db-roundtrip-audit":
		return cmdDBRoundTripAudit(args[1:], stdout, stderr)
	case "validation-audit":
		return cmdValidationAudit(args[1:], stdout, stderr)
	case "temporal-audit":
		return cmdTemporalAudit(args[1:], stdout, stderr)
	case "lib-version-check":
		return cmdLibVersionCheck(args[1:], stdout, stderr)
	case "doc-check":
		return cmdDocCheck(args[1:], stdout, stderr)
	case "tool-catalog":
		return cmdToolCatalog(args[1:], stdout, stderr)
	case "knowledge":
		return cmdKnowledge(args[1:], stdout, stderr)
	case "mcp":
		return cmdMCP(args[1:], stderr)
	case "rules":
		return cmdRules(args[1:], stdout, stderr)
	case "version", "--version", "-v":
		fmt.Fprintln(stdout, Version)
		return exitOK
	case "help", "--help", "-h":
		usage(stdout)
		return exitOK
	default:
		fmt.Fprintf(stderr, "gotools: unknown command %q\n\n", args[0])
		usage(stderr)
		return exitError
	}
}

func usage(w io.Writer) {
	fmt.Fprint(w, `gotools — n-api-template analysis for the dakcoder agent

USAGE
  gotools lint               [flags]   check template compliance
  gotools legacy-audit       [flags]   detect pre-template (pao-generation) patterns
  gotools resource-scaffold  [flags]   write a CRUD resource from a spec
  gotools project-scaffold   [flags]   create a new service from a spec
  gotools fx-wire            [flags]   register a constructor in bootstrapper.go
  gotools repo-map           [flags]   module, package tree, exported symbols, FX graph
  gotools db-roundtrip-audit [flags]   per-method database round trips, N+1s first
  gotools validation-audit   [flags]   request fields and what their validate tags miss
  gotools temporal-audit     [flags]   work on the request path that may belong off it
  gotools lib-version-check  [flags]   CEPT library drift (reports only, never updates)
  gotools doc-check          [flags]   verify rule citations against skill.md / SOP.md
  gotools tool-catalog       [flags]   write contract C1 (the published tool schemas)
  gotools knowledge          [flags]   build the agent's knowledge base from skill.md / SOP.md
  gotools mcp                [flags]   serve the tools over MCP on stdio
  gotools rules              [flags]   list the rule set
  gotools version

LINT FLAGS
  --root DIR        workspace root (default ".")
  --paths a,b       scope blocking to these files; findings elsewhere are
                    reported but do not fail the run
  --only id1,id2    run only these rules
  --format text|json
  --quiet           suppress the summary line

SCAFFOLD FLAGS
  --root DIR        workspace root (default ".")
  --spec FILE       JSON spec, or - to read stdin
  --dry-run         print what would be written without writing it
  --format text|json

AUDIT FLAGS (db-roundtrip-audit, validation-audit, temporal-audit, lib-version-check)
  --root DIR        workspace root (default ".")
  --format text|json
  --all             audits only: include entries whose verdict is already ok
  --offline         lib-version-check only: skip the registry, report supersession
  --timeout D       lib-version-check only: per-module lookup timeout (default 30s)

  These are reports. They always exit 0; none of them gates a build.

REPO-MAP FLAGS
  --root DIR        workspace root (default ".")
  --package DIR     narrow to one package directory, in full detail
  --max-tokens N    size cap; 0 for the default (4000), -1 for none
  --format text|json

FX-WIRE FLAGS
  --root DIR        workspace root (default ".")
  --kind KIND       repo | handler
  --ctor NAME       constructor name, e.g. NewPensionHandler
  --dry-run         print the patched file without writing it
  --format text|json

DOC-CHECK FLAGS
  --docs DIR        directory holding skill.md and SOP.md (default "../new-template")
  --manifest PATH   pinned manifest (default "docs/doc-manifest.json")
  --update          rewrite the manifest, then review the diff
  --format text|json

TOOL-CATALOG FLAGS
  --out DIR         output directory (default "docs")
  --check           verify the committed catalogue is current instead of writing

KNOWLEDGE FLAGS
  --docs DIR        directory holding skill.md and SOP.md (default "../new-template")
  --out DIR         knowledge-base root (default "../packages/knowledge")
  --check           verify the committed knowledge base is current

EXIT CODES
  0  clean          1  violations found          2  gotools itself failed
`)
}

type lintFlags struct {
	root   string
	paths  string
	only   string
	format string
	quiet  bool
}

func (lf *lintFlags) bind(fs *flag.FlagSet) {
	fs.StringVar(&lf.root, "root", ".", "workspace root")
	fs.StringVar(&lf.paths, "paths", "", "comma-separated paths to scope blocking to")
	fs.StringVar(&lf.only, "only", "", "comma-separated rule IDs")
	fs.StringVar(&lf.format, "format", "text", "text|json")
	fs.BoolVar(&lf.quiet, "quiet", false, "suppress the summary line")
}

func cmdLint(args []string, stdout, stderr io.Writer, legacy bool) int {
	name := "lint"
	if legacy {
		name = "legacy-audit"
	}
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(stderr)
	var lf lintFlags
	lf.bind(fs)
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	if lf.format != "text" && lf.format != "json" {
		fmt.Fprintf(stderr, "gotools: --format must be text or json, got %q\n", lf.format)
		return exitError
	}

	res, err := rules.Analyze(lf.root, rules.RunOptions{
		Only:   splitList(lf.only),
		Scope:  splitList(lf.paths),
		Legacy: legacy,
	})
	if err != nil {
		var unknown *rules.UnknownRuleError
		if errors.As(err, &unknown) {
			fmt.Fprintf(stderr, "gotools: %v\nrun `gotools rules` to list valid IDs\n", err)
			return exitError
		}
		fmt.Fprintf(stderr, "gotools: %v\n", err)
		return exitError
	}

	if lf.format == "json" {
		enc := json.NewEncoder(stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(res); err != nil {
			fmt.Fprintf(stderr, "gotools: encode result: %v\n", err)
			return exitError
		}
	} else {
		writeText(stdout, res, lf.quiet)
	}

	if res.OK {
		return exitOK
	}
	return exitFindings
}

func writeText(w io.Writer, res *rules.Result, quiet bool) {
	if res.OK && len(res.Warnings) == 0 && res.OutOfScopeCount == 0 {
		if !quiet {
			fmt.Fprintf(w, "OK — 0 violations (%d files, %d rules, %dms)\n",
				res.FilesScanned, res.RulesRun, res.DurationMS)
		}
		return
	}

	if len(res.Violations) > 0 {
		fmt.Fprintf(w, "\n%d violation(s):\n\n", len(res.Violations))
		for _, v := range res.Violations {
			fmt.Fprintf(w, "  %s\n", v)
		}
	}
	if len(res.Warnings) > 0 {
		fmt.Fprintf(w, "\n%d warning(s):\n\n", len(res.Warnings))
		for _, v := range res.Warnings {
			fmt.Fprintf(w, "  %s\n", v)
		}
	}
	if res.OutOfScopeCount > 0 {
		// Deliberately summarised, not listed: these are pre-existing findings
		// in files the caller did not touch. Printing them in full is how an
		// agent gets distracted into "fixing" unrelated legacy code.
		fmt.Fprintf(w, "\n%d pre-existing violation(s) in files outside the requested scope (not blocking).\n",
			res.OutOfScopeCount)
	}
	if !quiet {
		byRule := map[string]int{}
		for _, v := range append(append([]rules.Violation{}, res.Violations...), res.Warnings...) {
			byRule[v.Rule]++
		}
		if len(byRule) > 0 {
			fmt.Fprintf(w, "\nby rule:\n")
			for _, r := range rules.Default().All() {
				if n := byRule[r.ID]; n > 0 {
					fmt.Fprintf(w, "  %-28s %d\n", r.ID, n)
				}
			}
		}
		fmt.Fprintf(w, "\n%d file(s), %d rule(s), %dms\n", res.FilesScanned, res.RulesRun, res.DurationMS)
	}
}

func cmdRules(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("rules", flag.ContinueOnError)
	fs.SetOutput(stderr)
	format := fs.String("format", "text", "text|json")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	all := rules.Default().All()
	if *format == "json" {
		enc := json.NewEncoder(stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(all); err != nil {
			fmt.Fprintf(stderr, "gotools: %v\n", err)
			return exitError
		}
		return exitOK
	}
	for _, r := range all {
		kind := "compliance"
		if r.Legacy {
			kind = "legacy"
		}
		fmt.Fprintf(stdout, "%-28s %-8s %-11s %s\n", r.ID, r.Severity, kind, r.Summary)
		if r.Citation != "" {
			fmt.Fprintf(stdout, "%-28s          see: %s\n", "", r.Citation)
		}
	}
	return exitOK
}

func cmdMCP(args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("mcp", flag.ContinueOnError)
	fs.SetOutput(stderr)
	root := fs.String("root", ".", "default workspace root")
	if err := fs.Parse(args); err != nil {
		return exitError
	}

	// Honour interrupt so a supervising agent can shut the sidecar down
	// cleanly rather than leaving an orphan process holding a workspace.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	if err := mcpserver.Serve(ctx, *root, Version); err != nil && !errors.Is(err, context.Canceled) {
		fmt.Fprintf(stderr, "gotools mcp: %v\n", err)
		return exitError
	}
	return exitOK
}

func splitList(s string) []string {
	if strings.TrimSpace(s) == "" {
		return nil
	}
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}
