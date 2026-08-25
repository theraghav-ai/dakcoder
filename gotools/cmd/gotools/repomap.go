package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/repomap"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

func cmdRepoMap(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("repo-map", flag.ContinueOnError)
	fs.SetOutput(stderr)
	root := fs.String("root", ".", "workspace root")
	pkg := fs.String("package", "", "narrow to one package directory, in full detail")
	maxTokens := fs.Int("max-tokens", 0, "size cap; 0 for the default, -1 for none")
	format := fs.String("format", "text", "text|json")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	if *format != "text" && *format != "json" {
		fmt.Fprintf(stderr, "gotools repo-map: --format must be text or json, got %q\n", *format)
		return exitError
	}

	ws, err := workspace.Load(*root)
	if err != nil {
		fmt.Fprintf(stderr, "gotools repo-map: %v\n", err)
		return exitError
	}
	m := repomap.Build(ws, repomap.Options{Package: *pkg, MaxTokens: *maxTokens})

	if *format == "json" {
		enc := json.NewEncoder(stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(m); err != nil {
			fmt.Fprintf(stderr, "gotools repo-map: encode result: %v\n", err)
			return exitError
		}
		return exitOK
	}
	writeRepoMap(stdout, m)
	return exitOK
}

// writeRepoMap renders the map for a human. The JSON form is what the agent
// consumes; this one exists so a developer can sanity-check what the agent is
// being told.
func writeRepoMap(w io.Writer, m *repomap.Map) {
	if m.Module != "" {
		fmt.Fprintf(w, "module %s", m.Module)
		if m.GoVersion != "" {
			fmt.Fprintf(w, "  (go %s)", m.GoVersion)
		}
		if m.Generation != "" {
			fmt.Fprintf(w, "  [%s generation]", m.Generation)
		}
		fmt.Fprintln(w)
	}

	if len(m.Packages) > 0 {
		fmt.Fprintf(w, "\npackages:\n")
		for _, p := range m.Packages {
			dir := p.Dir
			if dir == "" {
				dir = "."
			}
			fmt.Fprintf(w, "  %-28s %-10s %2d file(s)", dir, p.Layer, p.Files)
			if p.Summarised {
				fmt.Fprint(w, "  (symbols elided)")
			}
			fmt.Fprintln(w)
			if len(p.Types) > 0 {
				fmt.Fprintf(w, "      types: %s%s\n", strings.Join(p.Types, ", "), more(p.MoreTypes))
			}
			if len(p.Funcs) > 0 {
				fmt.Fprintf(w, "      funcs: %s%s\n", strings.Join(p.Funcs, ", "), more(p.MoreFuncs))
			}
		}
	}

	if m.FX != nil {
		fmt.Fprintf(w, "\nfx:\n")
		if len(m.FX.Repos) > 0 {
			fmt.Fprintf(w, "  repos:    %s\n", strings.Join(m.FX.Repos, ", "))
		}
		if len(m.FX.Handlers) > 0 {
			fmt.Fprintf(w, "  handlers: %s\n", strings.Join(m.FX.Handlers, ", "))
		}
		if len(m.FX.Unwired) > 0 {
			fmt.Fprintf(w, "  UNWIRED:  %s  (these fail at startup)\n", strings.Join(m.FX.Unwired, ", "))
		}
		if len(m.FX.Misregistered) > 0 {
			fmt.Fprintf(w, "  MISREGISTERED: %s  (these start but serve no routes)\n",
				strings.Join(m.FX.Misregistered, ", "))
		}
	}

	if len(m.Requires) > 0 {
		fmt.Fprintf(w, "\ndirect dependencies: %d\n", len(m.Requires))
	}
	if m.Elided != nil {
		fmt.Fprintf(w, "\n%s\n", m.Elided.Hint)
	}
	fmt.Fprintf(w, "\n%d file(s), ~%d tokens, %dms\n", m.Files, m.EstTokens, m.DurationMS)
}

// more renders the "and N more" suffix on a truncated symbol list, so a capped
// list is never mistaken for a complete one.
func more(n int) string {
	if n == 0 {
		return ""
	}
	return fmt.Sprintf("  (+%d more)", n)
}
