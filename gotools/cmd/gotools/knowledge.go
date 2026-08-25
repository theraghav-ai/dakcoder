package main

import (
	"bytes"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/kb"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/rules"
)

// cmdKnowledge writes the progressive-disclosure knowledge base (Part A §14.2),
// or verifies the committed copy is current.
func cmdKnowledge(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("knowledge", flag.ContinueOnError)
	fs.SetOutput(stderr)
	docs := fs.String("docs", "../new-template", "directory holding skill.md and SOP.md")
	out := fs.String("out", "../packages/knowledge", "knowledge-base root")
	check := fs.Bool("check", false, "verify the committed knowledge base is current instead of writing it")
	if err := fs.Parse(args); err != nil {
		return exitError
	}

	var summaries []kb.RuleSummary
	for _, r := range rules.Default().All() {
		summaries = append(summaries, kb.RuleSummary{
			ID:       r.ID,
			Severity: string(r.Severity),
			Summary:  r.Summary,
			Citation: r.Citation,
			Legacy:   r.Legacy,
		})
	}

	files, err := kb.Build(kb.BuildInput{DocsDir: *docs, Rules: summaries})
	if err != nil {
		fmt.Fprintf(stderr, "gotools knowledge: %v\n", err)
		return exitError
	}

	if *check {
		var stale []string
		for _, f := range files {
			body, rerr := os.ReadFile(filepath.Join(*out, filepath.FromSlash(f.Path)))
			if rerr != nil || !bytes.Equal(normaliseEOL(body), normaliseEOL([]byte(f.Content))) {
				stale = append(stale, f.Path)
			}
		}
		if len(stale) > 0 {
			sort.Strings(stale)
			fmt.Fprintf(stderr, "gotools knowledge: %d file(s) are stale or missing:\n", len(stale))
			for _, p := range stale {
				fmt.Fprintf(stderr, "  %s\n", p)
			}
			fmt.Fprintln(stderr, "run `make knowledge` and commit the result")
			return exitFindings
		}
		fmt.Fprintf(stdout, "OK — the knowledge base is current (%d files)\n", len(files))
		return exitOK
	}

	for _, f := range files {
		path := filepath.Join(*out, filepath.FromSlash(f.Path))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			fmt.Fprintf(stderr, "gotools knowledge: %v\n", err)
			return exitError
		}
		if err := os.WriteFile(path, []byte(f.Content), 0o644); err != nil {
			fmt.Fprintf(stderr, "gotools knowledge: write %s: %v\n", f.Path, err)
			return exitError
		}
	}
	fmt.Fprintf(stdout, "wrote %d file(s) to %s\n", len(files), filepath.ToSlash(*out))
	for _, f := range files {
		fmt.Fprintf(stdout, "  %-42s %6d bytes\n", f.Path, len(f.Content))
	}
	return exitOK
}
