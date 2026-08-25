package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/fxwire"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/scaffold"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/spec"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// projectSpecFile is the shape of the file `project-scaffold --spec` reads: a
// service description plus the one resource it is seeded with.
type projectSpecFile struct {
	Project  scaffold.Project `json:"project"`
	Resource spec.Resource    `json:"resource"`
}

type scaffoldFlags struct {
	root   string
	spec   string
	dryRun bool
	format string
}

func (sf *scaffoldFlags) bind(fs *flag.FlagSet) {
	fs.StringVar(&sf.root, "root", ".", "workspace root")
	fs.StringVar(&sf.spec, "spec", "", `spec file, or "-" for stdin`)
	fs.BoolVar(&sf.dryRun, "dry-run", false, "print what would be written without writing it")
	fs.StringVar(&sf.format, "format", "text", "text|json")
}

func (sf *scaffoldFlags) validate(stderr io.Writer, name string) bool {
	if sf.format != "text" && sf.format != "json" {
		fmt.Fprintf(stderr, "gotools %s: --format must be text or json, got %q\n", name, sf.format)
		return false
	}
	if strings.TrimSpace(sf.spec) == "" {
		fmt.Fprintf(stderr, "gotools %s: --spec is required (a file path, or - for stdin)\n", name)
		return false
	}
	return true
}

// readSpec loads a JSON spec from a file or stdin.
func readSpec(path string, stdin io.Reader, into any) error {
	var (
		body []byte
		err  error
	)
	if path == "-" {
		body, err = io.ReadAll(stdin)
	} else {
		body, err = os.ReadFile(path)
	}
	if err != nil {
		return fmt.Errorf("read spec: %w", err)
	}
	dec := json.NewDecoder(strings.NewReader(string(body)))
	// Unknown fields are a spec the caller believes in and we are ignoring —
	// far better to say so than to scaffold something subtly different from
	// what was asked for.
	dec.DisallowUnknownFields()
	if err := dec.Decode(into); err != nil {
		return fmt.Errorf("parse spec: %w", err)
	}
	return nil
}

func cmdResourceScaffold(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("resource-scaffold", flag.ContinueOnError)
	fs.SetOutput(stderr)
	var sf scaffoldFlags
	sf.bind(fs)
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	if !sf.validate(stderr, "resource-scaffold") {
		return exitError
	}

	var s spec.Resource
	if err := readSpec(sf.spec, stdin, &s); err != nil {
		fmt.Fprintf(stderr, "gotools resource-scaffold: %v\n", err)
		return exitError
	}

	res, err := scaffold.Resource(sf.root, s, scaffold.ResourceOptions{})
	if err != nil {
		return reportScaffoldError(stderr, "resource-scaffold", err)
	}
	return finishScaffold(sf, res, stdout, stderr, "resource-scaffold")
}

func cmdProjectScaffold(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("project-scaffold", flag.ContinueOnError)
	fs.SetOutput(stderr)
	var sf scaffoldFlags
	sf.bind(fs)
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	if !sf.validate(stderr, "project-scaffold") {
		return exitError
	}

	var file projectSpecFile
	if err := readSpec(sf.spec, stdin, &file); err != nil {
		fmt.Fprintf(stderr, "gotools project-scaffold: %v\n", err)
		return exitError
	}

	res, err := scaffold.NewProject(sf.root, file.Project, file.Resource)
	if err != nil {
		return reportScaffoldError(stderr, "project-scaffold", err)
	}
	return finishScaffold(sf, res, stdout, stderr, "project-scaffold")
}

func finishScaffold(sf scaffoldFlags, res *scaffold.Result, stdout, stderr io.Writer, name string) int {
	if !sf.dryRun {
		if err := scaffold.Apply(sf.root, res); err != nil {
			return reportScaffoldError(stderr, name, err)
		}
	}

	if sf.format == "json" {
		payload := struct {
			*scaffold.Result
			Written bool `json:"written"`
		}{res, !sf.dryRun}
		enc := json.NewEncoder(stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(payload); err != nil {
			fmt.Fprintf(stderr, "gotools %s: encode result: %v\n", name, err)
			return exitError
		}
		return exitOK
	}

	verb := "wrote"
	if sf.dryRun {
		verb = "would write"
	}
	fmt.Fprintf(stdout, "%s %d file(s):\n\n", verb, len(res.Files))
	for _, f := range res.Files {
		fmt.Fprintf(stdout, "  %-6s %-38s %6d bytes\n", f.Action, f.Path, f.Bytes)
	}
	if len(res.Notes) > 0 {
		fmt.Fprintf(stdout, "\nnext:\n")
		for _, n := range res.Notes {
			fmt.Fprintf(stdout, "  · %s\n", n)
		}
	}
	return exitOK
}

// reportScaffoldError prints an invalid spec as a list the caller can work
// through, rather than as one long line.
func reportScaffoldError(stderr io.Writer, name string, err error) int {
	var bad *spec.InvalidSpecError
	if errors.As(err, &bad) {
		fmt.Fprintf(stderr, "gotools %s: the spec has %d problem(s):\n\n", name, len(bad.Issues))
		for _, issue := range bad.Issues {
			fmt.Fprintf(stderr, "  %s\n      %s\n", issue.Path, issue.Message)
			if issue.Fix != "" {
				fmt.Fprintf(stderr, "      fix: %s\n", issue.Fix)
			}
		}
		return exitError
	}
	fmt.Fprintf(stderr, "gotools %s: %v\n", name, err)
	return exitError
}

func cmdFxWire(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("fx-wire", flag.ContinueOnError)
	fs.SetOutput(stderr)
	root := fs.String("root", ".", "workspace root")
	kind := fs.String("kind", "", "repo|handler")
	ctor := fs.String("ctor", "", "constructor name, e.g. NewPensionHandler")
	dryRun := fs.Bool("dry-run", false, "print the patched file without writing it")
	format := fs.String("format", "text", "text|json")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	if *format != "text" && *format != "json" {
		fmt.Fprintf(stderr, "gotools fx-wire: --format must be text or json, got %q\n", *format)
		return exitError
	}
	if *kind == "" || *ctor == "" {
		fmt.Fprintln(stderr, "gotools fx-wire: --kind and --ctor are required")
		return exitError
	}

	module, err := workspace.ModulePath(*root)
	if err != nil {
		fmt.Fprintf(stderr, "gotools fx-wire: %v\n", err)
		return exitError
	}

	reg := fxwire.Registration{
		Kind: fxwire.Kind(strings.ToLower(strings.TrimSpace(*kind))),
		Ctor: *ctor,
	}

	var res *fxwire.Result
	if *dryRun {
		res, err = fxwire.Plan(*root, module, reg)
	} else {
		res, err = fxwire.Apply(*root, module, reg)
	}
	if err != nil {
		fmt.Fprintf(stderr, "gotools fx-wire: %v\n", err)
		return exitError
	}

	if *format == "json" {
		enc := json.NewEncoder(stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(res); err != nil {
			fmt.Fprintf(stderr, "gotools fx-wire: encode result: %v\n", err)
			return exitError
		}
		return exitOK
	}

	switch {
	case len(res.AlreadyRegistered) > 0:
		fmt.Fprintf(stdout, "%s already registers %s — nothing to do\n",
			res.Path, strings.Join(res.AlreadyRegistered, ", "))
	case *dryRun:
		fmt.Fprintf(stdout, "would register %s in %s:\n\n%s",
			strings.Join(res.Added, ", "), res.Path, res.Content)
	default:
		fmt.Fprintf(stdout, "registered %s in %s\n", strings.Join(res.Added, ", "), res.Path)
	}
	return exitOK
}
