package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"text/tabwriter"
	"time"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/libversion"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/rules"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// The four commands here are reports, not gates. Every one of them exits 0 with
// findings, because none of them is asserting a contract — they reproduce the
// three sheets the manual review was assembled by hand plus the library-version
// column, so that the next review round is a diff against a baseline rather
// than 41 people reading code.
//
// That is why they are separate commands rather than more rules: a rule that
// never blocks is a warning nobody reads, and these are worth reading in full.

// auditFlags are the options every audit shares.
type auditFlags struct {
	root   string
	format string
}

func bindAuditFlags(fs *flag.FlagSet) *auditFlags {
	a := &auditFlags{}
	fs.StringVar(&a.root, "root", ".", "workspace root")
	fs.StringVar(&a.format, "format", "text", "text|json")
	return a
}

// loadForAudit parses the workspace once, the way the linter does.
func loadForAudit(a *auditFlags, stderr io.Writer, name string) (*workspace.Workspace, bool) {
	if a.format != "text" && a.format != "json" {
		fmt.Fprintf(stderr, "gotools %s: --format must be text or json, got %q\n", name, a.format)
		return nil, false
	}
	ws, err := workspace.Load(a.root)
	if err != nil {
		fmt.Fprintf(stderr, "gotools %s: %v\n", name, err)
		return nil, false
	}
	return ws, true
}

func writeJSON(w io.Writer, stderr io.Writer, name string, v any) int {
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		fmt.Fprintf(stderr, "gotools %s: encode result: %v\n", name, err)
		return exitError
	}
	return exitOK
}

// cmdDBRoundTripAudit reproduces the Db/batch column — 748 of the review's
// findings, its largest single category.
func cmdDBRoundTripAudit(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("db-roundtrip-audit", flag.ContinueOnError)
	fs.SetOutput(stderr)
	a := bindAuditFlags(fs)
	all := fs.Bool("all", false, "include methods whose verdict is already ok")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	ws, ok := loadForAudit(a, stderr, "db-roundtrip-audit")
	if !ok {
		return exitError
	}

	reports := rules.RoundTripAudit(ws)
	if !*all {
		kept := reports[:0]
		for _, r := range reports {
			if r.Verdict != "ok" {
				kept = append(kept, r)
			}
		}
		reports = kept
	}

	if a.format == "json" {
		return writeJSON(stdout, stderr, "db-roundtrip-audit", reports)
	}
	if len(reports) == 0 {
		fmt.Fprintln(stdout, "no repository method makes more than one database call")
		return exitOK
	}
	tw := tabwriter.NewWriter(stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "METHOD\tSTMTS\tIN-LOOP\tBATCHED\tTXN\tVERDICT")
	for _, r := range reports {
		fmt.Fprintf(tw, "%s\t%d\t%s\t%s\t%s\t%s\n",
			r.Method, r.Statements, yesNo(r.InLoop), yesNo(r.Batched), yesNo(r.Transaction), r.Verdict)
	}
	tw.Flush()
	fmt.Fprintf(stdout, "\n%d method(s) worth a look, ordered by cost\n", len(reports))
	return exitOK
}

func yesNo(b bool) string {
	if b {
		return "yes"
	}
	return "-"
}

// cmdValidationAudit reproduces the "Validations to be added" sheet, which is
// roughly 40% of every row in the review.
func cmdValidationAudit(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("validation-audit", flag.ContinueOnError)
	fs.SetOutput(stderr)
	a := bindAuditFlags(fs)
	all := fs.Bool("all", false, "include fields that are already fully constrained")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	ws, ok := loadForAudit(a, stderr, "validation-audit")
	if !ok {
		return exitError
	}

	reports := rules.ValidationAudit(ws)
	if !*all {
		kept := reports[:0]
		for _, r := range reports {
			if r.Missing != "" {
				kept = append(kept, r)
			}
		}
		reports = kept
	}

	if a.format == "json" {
		return writeJSON(stdout, stderr, "validation-audit", reports)
	}
	if len(reports) == 0 {
		fmt.Fprintln(stdout, "every request field is bound")
		return exitOK
	}
	tw := tabwriter.NewWriter(stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "STRUCT\tFIELD\tTYPE\tVALIDATE\tMISSING")
	for _, r := range reports {
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\t%s\n", r.Struct, r.Field, r.Type, orDash(r.Tag), orDash(r.Missing))
	}
	tw.Flush()
	fmt.Fprintf(stdout, "\n%d field(s) with nothing bounding them\n", len(reports))
	return exitOK
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

// cmdTemporalAudit lists work done on the request path that the review
// repeatedly said should move off it.
//
// Deliberately advice-free. See rules.TemporalAudit and findings §10.3.
func cmdTemporalAudit(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("temporal-audit", flag.ContinueOnError)
	fs.SetOutput(stderr)
	a := bindAuditFlags(fs)
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	ws, ok := loadForAudit(a, stderr, "temporal-audit")
	if !ok {
		return exitError
	}

	candidates := rules.TemporalAudit(ws)
	if a.format == "json" {
		return writeJSON(stdout, stderr, "temporal-audit", candidates)
	}
	if len(candidates) == 0 {
		fmt.Fprintln(stdout, "no off-request-path candidates found")
		return exitOK
	}
	tw := tabwriter.NewWriter(stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "LOCATION\tFUNC\tKIND\tCALL")
	for _, c := range candidates {
		fmt.Fprintf(tw, "%s:%d\t%s\t%s\t%s\n", c.Path, c.Line, c.Func, c.Kind, c.Call)
	}
	tw.Flush()
	fmt.Fprintf(stdout, "\n%d candidate(s) for review. This report makes no recommendation:\n"+
		"where this work belongs is a decision about failure semantics, and the\n"+
		"template has no Temporal wiring yet.\n", len(candidates))
	return exitOK
}

// cmdLibVersionCheck reports drift against the published CEPT libraries.
//
// Reports only. It never edits go.mod and it always exits 0 — being behind on a
// shared library is information, not a build failure, and the template owner
// asked for it that way.
func cmdLibVersionCheck(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("lib-version-check", flag.ContinueOnError)
	fs.SetOutput(stderr)
	a := bindAuditFlags(fs)
	timeout := fs.Duration("timeout", 30*time.Second, "per-module registry lookup timeout")
	offline := fs.Bool("offline", false, "skip the registry; report only what is superseded")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	ws, ok := loadForAudit(a, stderr, "lib-version-check")
	if !ok {
		return exitError
	}

	var lister libversion.Lister = libversion.GoListLister{Dir: a.root, Timeout: *timeout}
	if *offline {
		lister = offlineLister{}
	}
	res := libversion.Check(context.Background(), ws, lister)

	if a.format == "json" {
		return writeJSON(stdout, stderr, "lib-version-check", res)
	}
	if len(res.Reports) == 0 {
		fmt.Fprintln(stdout, "no CEPT common libraries in go.mod")
		return exitOK
	}
	tw := tabwriter.NewWriter(stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "MODULE\tCURRENT\tLATEST\tSTATUS")
	for _, r := range res.Reports {
		status := string(r.Status)
		switch {
		case r.SupersededBy != "":
			status = "SUPERSEDED -> " + shortModule(r.SupersededBy)
			if r.Behind > 0 {
				status += fmt.Sprintf(" (also %d behind)", r.Behind)
			}
		case r.Behind > 0:
			status = fmt.Sprintf("behind (%d)", r.Behind)
		}
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\n", shortModule(r.Module), r.Current, orDash(r.Latest), status)
	}
	tw.Flush()
	fmt.Fprintf(stdout, "\n%s\n", res.Summary())
	if res.Error != "" {
		fmt.Fprintf(stdout, "registry not fully reachable: %s\n", res.Error)
	}
	fmt.Fprintln(stdout, "reported only — nothing was changed, and being behind does not fail a build")
	return exitOK
}

// shortModule trims the shared namespace so the table fits a terminal.
func shortModule(path string) string {
	if len(path) > len(libversion.ModulePrefix) && path[:len(libversion.ModulePrefix)] == libversion.ModulePrefix {
		return path[len(libversion.ModulePrefix):]
	}
	return path
}

// offlineLister answers nothing, so --offline still reports the superseded
// column, which is static knowledge and needs no network.
type offlineLister struct{}

func (offlineLister) Versions(context.Context, string) ([]string, error) { return nil, nil }
