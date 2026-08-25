package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"path/filepath"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/kb"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/rules"
)

// cmdDocCheck verifies the rule set against the documents it cites.
//
// Not part of `lint`: this checks gotools against the template's documentation,
// not the developer's code against gotools. It belongs in CI, next to the
// corpus baselines, and it fails for exactly one reason — somebody changed
// skill.md or SOP.md and nobody looked at the rules that cite them.
func cmdDocCheck(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("doc-check", flag.ContinueOnError)
	fs.SetOutput(stderr)
	docs := fs.String("docs", "../new-template", "directory holding skill.md and SOP.md")
	manifest := fs.String("manifest", kb.ManifestFile, "pinned manifest path")
	update := fs.Bool("update", false, "rewrite the manifest, then review the diff")
	format := fs.String("format", "text", "text|json")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	if *format != "text" && *format != "json" {
		fmt.Fprintf(stderr, "gotools doc-check: --format must be text or json, got %q\n", *format)
		return exitError
	}

	var citations []kb.Citation
	for _, r := range rules.Default().All() {
		if r.Citation != "" {
			citations = append(citations, kb.Citation{RuleID: r.ID, Text: r.Citation})
		}
	}

	pinned, err := kb.LoadManifest(*manifest)
	if err != nil {
		fmt.Fprintf(stderr, "gotools doc-check: %v\n", err)
		return exitError
	}
	if *update {
		pinned = nil // do not report drift against a manifest we are about to replace
	}

	rep, err := kb.Check(*docs, citations, pinned)
	if err != nil {
		fmt.Fprintf(stderr, "gotools doc-check: %v\n", err)
		return exitError
	}

	if *update {
		if err := kb.SaveManifest(*manifest, rep.Manifest); err != nil {
			fmt.Fprintf(stderr, "gotools doc-check: %v\n", err)
			return exitError
		}
		fmt.Fprintf(stdout, "wrote %s — review the diff before committing\n", *manifest)
	}

	if *format == "json" {
		enc := json.NewEncoder(stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(rep); err != nil {
			fmt.Fprintf(stderr, "gotools doc-check: encode result: %v\n", err)
			return exitError
		}
	} else {
		writeDocReport(stdout, rep, *docs)
	}

	if rep.OK {
		return exitOK
	}
	return exitFindings
}

func writeDocReport(w io.Writer, rep *kb.Report, docsDir string) {
	if rep.OK {
		fmt.Fprintf(w, "OK — %d citation(s) resolved against %s\n",
			rep.Resolved, filepath.ToSlash(docsDir))
		return
	}
	fmt.Fprintf(w, "\n%d problem(s):\n\n", len(rep.Problems))
	for _, p := range rep.Problems {
		fmt.Fprintf(w, "  %s\n", p)
	}
	fmt.Fprintf(w, "\n%d citation(s) resolved across %v\n", rep.Resolved, rep.Checked)
}
