package mcpserver

import (
	"context"
	"fmt"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/callmap"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// The migration's three tools: what a handler file holds and how it splits
// into steps, whether a step is done, and who calls a function. See
// internal/callmap for why they exist -- a general code graph records none of
// the handler-to-repository calls a conversion is made of.

// HandlerMapInput is the argument shape for handler_map.
type HandlerMapInput struct {
	Root      string `json:"root,omitempty" jsonschema:"workspace root; omit to use the server's default"`
	Path      string `json:"path" jsonschema:"the handler file, e.g. handler/paogen.go"`
	StepLines int    `json:"step_lines,omitempty" jsonschema:"lines per step; omit for 800"`
}

// UnitCheckInput is the argument shape for unit_check.
type UnitCheckInput struct {
	Root    string   `json:"root,omitempty" jsonschema:"workspace root; omit to use the server's default"`
	Path    string   `json:"path" jsonschema:"the file the step converted"`
	Methods []string `json:"methods,omitempty" jsonschema:"the methods the step converted; omit for every handler method in the file"`
}

// ImpactInput is the argument shape for impact.
type ImpactInput struct {
	Root   string `json:"root,omitempty" jsonschema:"workspace root; omit to use the server's default"`
	Symbol string `json:"symbol" jsonschema:"Type.Method, a bare name, or file.go::Name"`
}

func addCallMapTools(s *mcp.Server, defaultRoot string) {
	mcp.AddTool(s, &mcp.Tool{
		Name: "handler_map",
		Description: "One handler file split into conversion steps: every method with its lines, " +
			"route and the repository methods it calls. Plan a migration's handler steps from this.",
	}, handlerMapHandler(defaultRoot))

	mcp.AddTool(s, &mcp.Tool{
		Name: "unit_check",
		Description: "Whether a conversion step is done: the file parses and the named methods and " +
			"the repository methods they call have the template shape. Works while the build is red.",
	}, unitCheckHandler(defaultRoot))

	mcp.AddTool(s, &mcp.Tool{
		Name: "impact",
		Description: "Who calls a function, two levels up, with the routes that reach it. Call " +
			"before changing a repository method's signature.",
	}, impactHandler(defaultRoot))
}

func loadMap(defaultRoot, requested string) (*workspace.Workspace, *callmap.Map, error) {
	root, err := rootFor(defaultRoot, requested)
	if err != nil {
		return nil, nil, err
	}
	ws, err := workspace.Load(root)
	if err != nil {
		return nil, nil, fmt.Errorf("load workspace: %w", err)
	}
	return ws, callmap.Build(ws), nil
}

func handlerMapHandler(defaultRoot string) mcp.ToolHandlerFor[HandlerMapInput, callmap.HandlerMapResult] {
	return func(_ context.Context, _ *mcp.CallToolRequest, in HandlerMapInput) (*mcp.CallToolResult, callmap.HandlerMapResult, error) {
		if strings.TrimSpace(in.Path) == "" {
			return nil, callmap.HandlerMapResult{}, fmt.Errorf("path is required: the handler file to map")
		}
		ws, m, err := loadMap(defaultRoot, in.Root)
		if err != nil {
			return nil, callmap.HandlerMapResult{}, err
		}
		res, err := m.HandlerMap(ws, in.Path, in.StepLines)
		if err != nil {
			return nil, callmap.HandlerMapResult{}, err
		}
		return nil, *res, nil
	}
}

func unitCheckHandler(defaultRoot string) mcp.ToolHandlerFor[UnitCheckInput, callmap.CheckResult] {
	return func(_ context.Context, _ *mcp.CallToolRequest, in UnitCheckInput) (*mcp.CallToolResult, callmap.CheckResult, error) {
		if strings.TrimSpace(in.Path) == "" {
			return nil, callmap.CheckResult{}, fmt.Errorf("path is required: the file the step converted")
		}
		ws, m, err := loadMap(defaultRoot, in.Root)
		if err != nil {
			return nil, callmap.CheckResult{}, err
		}
		res, err := m.Check(ws, in.Path, in.Methods)
		if err != nil {
			return nil, callmap.CheckResult{}, err
		}
		return nil, *res, nil
	}
}

func impactHandler(defaultRoot string) mcp.ToolHandlerFor[ImpactInput, callmap.ImpactResult] {
	return func(_ context.Context, _ *mcp.CallToolRequest, in ImpactInput) (*mcp.CallToolResult, callmap.ImpactResult, error) {
		if strings.TrimSpace(in.Symbol) == "" {
			return nil, callmap.ImpactResult{}, fmt.Errorf("symbol is required, e.g. PaogenRepository.GetDDOsRepo")
		}
		ws, m, err := loadMap(defaultRoot, in.Root)
		if err != nil {
			return nil, callmap.ImpactResult{}, err
		}
		return nil, *m.Impact(ws, in.Symbol), nil
	}
}
