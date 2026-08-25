package main

import (
	"bytes"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/catalog"
)

// cmdToolCatalog writes contract C1's sidecar half, or verifies it is current.
func cmdToolCatalog(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("tool-catalog", flag.ContinueOnError)
	fs.SetOutput(stderr)
	outDir := fs.String("out", "docs", "directory to write the catalogue into")
	check := fs.Bool("check", false, "verify the committed catalogue is current instead of writing it")
	if err := fs.Parse(args); err != nil {
		return exitError
	}

	cat, err := catalog.Build(Version)
	if err != nil {
		fmt.Fprintf(stderr, "gotools tool-catalog: %v\n", err)
		return exitError
	}

	// A non-conforming schema must never reach the document other teams build
	// against — by the time it is published it has already been consumed.
	if bad := cat.Conformance(); len(bad) > 0 {
		fmt.Fprintf(stderr, "gotools tool-catalog: %d contract C1 violation(s):\n", len(bad))
		for _, v := range bad {
			fmt.Fprintf(stderr, "  %s: %s\n", v.Tool, v.Detail)
		}
		return exitFindings
	}

	jsonBody, err := cat.JSON()
	if err != nil {
		fmt.Fprintf(stderr, "gotools tool-catalog: %v\n", err)
		return exitError
	}
	mdBody := cat.Markdown()

	files := map[string][]byte{
		filepath.Join(*outDir, "tool-catalog.json"): jsonBody,
		filepath.Join(*outDir, "TOOL-CATALOG.md"):   mdBody,
	}

	if *check {
		var stale []string
		for path, want := range files {
			got, rerr := os.ReadFile(path)
			if rerr != nil || !bytes.Equal(normaliseEOL(got), normaliseEOL(want)) {
				stale = append(stale, path)
			}
		}
		if len(stale) > 0 {
			fmt.Fprintf(stderr,
				"gotools tool-catalog: %d file(s) are stale or missing:\n", len(stale))
			for _, p := range stale {
				fmt.Fprintf(stderr, "  %s\n", filepath.ToSlash(p))
			}
			fmt.Fprintln(stderr, "run `make tool-catalog` and commit the result")
			return exitFindings
		}
		fmt.Fprintf(stdout, "OK — the catalogue is current (%d tools)\n", len(cat.Tools))
		return exitOK
	}

	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		fmt.Fprintf(stderr, "gotools tool-catalog: %v\n", err)
		return exitError
	}
	for path, body := range files {
		if err := os.WriteFile(path, body, 0o644); err != nil {
			fmt.Fprintf(stderr, "gotools tool-catalog: write %s: %v\n", path, err)
			return exitError
		}
		fmt.Fprintf(stdout, "wrote %s\n", filepath.ToSlash(path))
	}
	fmt.Fprintf(stdout, "%d tools, contract C1 conformant\n", len(cat.Tools))
	return exitOK
}

// normaliseEOL makes the freshness check platform-independent: git may hand
// back CRLF on Windows for a file generated with LF, and a catalogue that
// reports itself stale on every Windows CI run is a check that gets disabled.
func normaliseEOL(b []byte) []byte {
	return bytes.ReplaceAll(b, []byte("\r\n"), []byte("\n"))
}
