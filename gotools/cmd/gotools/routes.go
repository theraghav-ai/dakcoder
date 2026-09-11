package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/routes"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// cmdRoutes takes the route inventory, or compares the current one against a
// saved one.
//
// Two modes because it is used at two moments of a migration and they are the
// same question asked from opposite ends: `--save` writes what the service
// declares *now*, and is run before the conversion starts; `--against` reads
// that file back and reports what the service no longer declares, and is run
// when the last phase closes.
//
// The comparison exits `exitFindings` when something is missing, so the gate
// can treat a lost route the way it treats a failing build -- which is the
// point of building this rather than asking the model to eyeball two lists.
func cmdRoutes(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("routes", flag.ContinueOnError)
	fs.SetOutput(stderr)
	root := fs.String("root", ".", "workspace root")
	save := fs.String("save", "", "write the inventory as JSON to this path")
	against := fs.String("against", "", "compare the current routes against a saved inventory")
	format := fs.String("format", "text", "text|json")
	if err := fs.Parse(args); err != nil {
		return exitError
	}

	ws, err := workspace.Load(*root)
	if err != nil {
		fmt.Fprintf(stderr, "gotools routes: %v\n", err)
		return exitError
	}
	inv := routes.Take(ws)

	if *save != "" {
		if err := writeInventory(*save, inv); err != nil {
			fmt.Fprintf(stderr, "gotools routes: %v\n", err)
			return exitError
		}
	}

	if *against != "" {
		return compareRoutes(*against, inv, *format, stdout, stderr)
	}

	if *format == "json" {
		return encode(stdout, stderr, inv)
	}
	writeInventoryText(stdout, inv, *save)
	return exitOK
}

func compareRoutes(path string, now *routes.Inventory, format string, stdout, stderr io.Writer) int {
	raw, err := os.ReadFile(path)
	if err != nil {
		fmt.Fprintf(stderr, "gotools routes: read %s: %v\n", path, err)
		fmt.Fprintln(stderr, "run `gotools routes --save <path>` before the migration starts")
		return exitError
	}
	var before routes.Inventory
	if err := json.Unmarshal(raw, &before); err != nil {
		fmt.Fprintf(stderr, "gotools routes: %s is not a route inventory: %v\n", path, err)
		return exitError
	}
	missing := routes.Compare(&before, now)

	if format == "json" {
		out := map[string]any{
			"before":  len(before.Routes),
			"after":   len(now.Routes),
			"missing": missing,
			"ok":      len(missing) == 0,
		}
		if code := encode(stdout, stderr, out); code != exitOK {
			return code
		}
		if len(missing) > 0 {
			return exitFindings
		}
		return exitOK
	}

	fmt.Fprintf(stdout, "routes before: %d\nroutes now:    %d\n\n", len(before.Routes), len(now.Routes))
	if len(missing) == 0 {
		fmt.Fprintln(stdout, "OK — every route in the saved inventory is still registered.")
		if len(now.Unresolved) > 0 {
			fmt.Fprintf(stdout, "\n%d registration(s) could not be read as a literal path and were "+
				"not compared:\n", len(now.Unresolved))
			for _, u := range now.Unresolved {
				fmt.Fprintf(stdout, "  %s\n", u)
			}
		}
		return exitOK
	}

	fmt.Fprintf(stdout, "%d route(s) are no longer registered:\n\n", len(missing))
	for _, r := range missing {
		fmt.Fprintf(stdout, "  %-6s %-50s was %s:%d", r.Method, r.Path, r.File, r.Line)
		if r.Handler != "" {
			fmt.Fprintf(stdout, " (%s)", r.Handler)
		}
		fmt.Fprintln(stdout)
	}
	fmt.Fprintln(stdout, "\nEach one is an endpoint a client can still call and will now get a 404 from.")
	fmt.Fprintln(stdout, "Add it to the converted handler's Routes(), with the same method and path.")
	return exitFindings
}

func writeInventory(path string, inv *routes.Inventory) error {
	if dir := filepath.Dir(path); dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return fmt.Errorf("create %s: %w", dir, err)
		}
	}
	body, err := json.MarshalIndent(inv, "", " ")
	if err != nil {
		return fmt.Errorf("encode inventory: %w", err)
	}
	// Written through a temporary file for the reason every other small file
	// here is: a half-written inventory read back after the migration would
	// compare against a truncated list and pass.
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(body, '\n'), 0o644); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	return os.Rename(tmp, path)
}

func writeInventoryText(w io.Writer, inv *routes.Inventory, saved string) {
	gin, template := inv.Counts()
	fmt.Fprintf(w, "%d route(s): %d legacy (gin), %d template\n", len(inv.Routes), gin, template)
	if saved != "" {
		fmt.Fprintf(w, "saved to %s\n", saved)
	}
	fmt.Fprintln(w)
	for _, r := range inv.Routes {
		fmt.Fprintf(w, "  %-6s %-50s %s:%d", r.Method, r.Path, r.File, r.Line)
		if r.Handler != "" {
			fmt.Fprintf(w, " → %s", r.Handler)
		}
		if r.Style == "template" && r.Name == "" {
			fmt.Fprint(w, "  [no .Name(); it will be missing from swagger]")
		}
		fmt.Fprintln(w)
	}
	if len(inv.Unresolved) > 0 {
		fmt.Fprintf(w, "\n%d registration(s) could not be read as a literal path:\n", len(inv.Unresolved))
		for _, u := range inv.Unresolved {
			fmt.Fprintf(w, "  %s\n", u)
		}
		fmt.Fprintln(w, "\nThese are not in the inventory, so they will not be checked after the "+
			"migration. Register them with a literal path, or check them by hand.")
	}
}

func encode(stdout, stderr io.Writer, v any) int {
	enc := json.NewEncoder(stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		fmt.Fprintf(stderr, "gotools routes: encode result: %v\n", err)
		return exitError
	}
	return exitOK
}
