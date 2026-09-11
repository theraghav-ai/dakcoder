package mcpserver

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/routes"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// RouteInventoryInput is the argument shape for route_inventory.
//
// One tool with two modes rather than two tools, because they are the same
// question asked from opposite ends of a migration -- what does this service
// declare -- and a pair would have to keep one inventory format in step across
// two schemas the model picks between.
type RouteInventoryInput struct {
	Root    string `json:"root,omitempty" jsonschema:"workspace root; omit to use the server's default"`
	Save    string `json:"save,omitempty" jsonschema:"write the inventory to this path, for comparing against later"`
	Against string `json:"against,omitempty" jsonschema:"compare the current routes against a saved inventory and report what is missing"`
}

// RouteInventoryOutput carries both modes: the inventory always, and the
// comparison when one was asked for.
type RouteInventoryOutput struct {
	Routes     []routes.Route `json:"routes"`
	Unresolved []string       `json:"unresolved,omitempty"`
	Files      []string       `json:"files"`
	Legacy     int            `json:"legacy"`
	Template   int            `json:"template"`
	// Saved is the path written, when Save was given.
	Saved string `json:"saved,omitempty"`
	// Missing is every route the saved inventory had that this one does not.
	// Populated only when Against was given; `Compared` says which case an
	// empty list is, because "nothing is missing" and "nothing was compared"
	// must not look the same to a gate.
	Missing  []routes.Route `json:"missing,omitempty"`
	Compared bool           `json:"compared"`
	Before   int            `json:"before,omitempty"`
}

func addRouteInventoryTool(s *mcp.Server, defaultRoot string) {
	mcp.AddTool(s, &mcp.Tool{
		Name: "route_inventory",
		Description: "Every HTTP route the service registers, gin or template, prefixes " +
			"resolved. `save` records them before a migration; `against` reports which a " +
			"finished one no longer serves.",
	}, routeInventoryHandler(defaultRoot))
}

func routeInventoryHandler(defaultRoot string) mcp.ToolHandlerFor[RouteInventoryInput, RouteInventoryOutput] {
	return func(_ context.Context, _ *mcp.CallToolRequest, in RouteInventoryInput) (*mcp.CallToolResult, RouteInventoryOutput, error) {
		root, err := rootFor(defaultRoot, in.Root)
		if err != nil {
			return nil, RouteInventoryOutput{}, err
		}
		ws, err := workspace.Load(root)
		if err != nil {
			return nil, RouteInventoryOutput{}, fmt.Errorf("load workspace: %w", err)
		}
		inv := routes.Take(ws)
		legacy, template := inv.Counts()
		out := RouteInventoryOutput{
			Routes:     inv.Routes,
			Unresolved: inv.Unresolved,
			Files:      inv.Files,
			Legacy:     legacy,
			Template:   template,
		}

		if in.Save != "" {
			path := resolveUnder(root, in.Save)
			if err := saveInventory(path, inv); err != nil {
				return nil, out, err
			}
			out.Saved = in.Save
		}

		if in.Against != "" {
			before, err := loadInventory(resolveUnder(root, in.Against))
			if err != nil {
				return nil, out, err
			}
			out.Before = len(before.Routes)
			out.Missing = routes.Compare(before, inv)
			out.Compared = true
		}
		return nil, out, nil
	}
}

func resolveUnder(root, path string) string {
	if filepath.IsAbs(path) {
		return path
	}
	return filepath.Join(root, filepath.FromSlash(path))
}

func saveInventory(path string, inv *routes.Inventory) error {
	if dir := filepath.Dir(path); dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return fmt.Errorf("create %s: %w", dir, err)
		}
	}
	body, err := json.MarshalIndent(inv, "", " ")
	if err != nil {
		return fmt.Errorf("encode inventory: %w", err)
	}
	// Through a temporary file, because a truncated inventory read back after
	// the migration compares against a short list and passes.
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(body, '\n'), 0o644); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	return os.Rename(tmp, path)
}

func loadInventory(path string) (*routes.Inventory, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf(
			"read %s: %w — take the inventory with save= before the migration starts", path, err)
	}
	var inv routes.Inventory
	if err := json.Unmarshal(raw, &inv); err != nil {
		return nil, fmt.Errorf("%s is not a route inventory: %w", path, err)
	}
	return &inv, nil
}
