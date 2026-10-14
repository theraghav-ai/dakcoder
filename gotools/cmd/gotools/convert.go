package main

import (
	"flag"
	"fmt"
	"io"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/convert"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// cmdConvert converts legacy gin handlers to the template shape by rule. A
// dry run unless --write: a developer reads the report before letting it
// touch a service.
func cmdConvert(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("convert", flag.ContinueOnError)
	fs.SetOutput(stderr)
	root := fs.String("root", ".", "workspace root")
	paths := fs.String("paths", "", "handler files to convert, comma-separated; default every handler file")
	methods := fs.String("methods", "", "only these handler methods, comma-separated")
	write := fs.Bool("write", false, "write the converted files (default: report only)")
	format := fs.String("format", "text", "text|json")
	service := fs.Bool("service", false, "convert the service wiring instead: routes.go into Routes(), bootstrap, main.go, library imports")
	dtos := fs.Bool("dtos", false, "give every converted handler's request type a Validate(): govalid where it can, the legacy validator where custom tags need it")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	ws, err := workspace.Load(*root)
	if err != nil {
		fmt.Fprintf(stderr, "gotools convert: %v\n", err)
		return exitError
	}
	if *dtos {
		res, err := convert.DTOs(ws, *write)
		if err != nil {
			fmt.Fprintf(stderr, "gotools convert: %v\n", err)
			return exitError
		}
		if *format == "json" {
			return encode(stdout, stderr, res)
		}
		fmt.Fprint(stdout, convert.RenderDTOs(res))
		return exitOK
	}
	if *service {
		res, err := convert.Service(ws, *write)
		if err != nil {
			fmt.Fprintf(stderr, "gotools convert: %v\n", err)
			return exitError
		}
		if *format == "json" {
			return encode(stdout, stderr, res)
		}
		fmt.Fprint(stdout, convert.RenderService(res))
		return exitOK
	}
	res, err := convert.Run(ws, convert.Options{Paths: split(*paths), Methods: split(*methods), Write: *write})
	if err != nil {
		fmt.Fprintf(stderr, "gotools convert: %v\n", err)
		return exitError
	}
	if *format == "json" {
		return encode(stdout, stderr, res)
	}
	fmt.Fprint(stdout, convert.Render(res))
	return exitOK
}

func split(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}
