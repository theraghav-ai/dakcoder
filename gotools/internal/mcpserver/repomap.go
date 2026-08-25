package mcpserver

import (
	"context"
	"fmt"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/repomap"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// RepoMapInput is the argument shape for repo_map.
type RepoMapInput struct {
	Root      string `json:"root,omitempty" jsonschema:"workspace root; omit to use the server's default"`
	Package   string `json:"package,omitempty" jsonschema:"a package directory such as repo/postgres, for that package in full detail"`
	MaxTokens int    `json:"max_tokens,omitempty" jsonschema:"size cap; omit for the default of 4000"`
}

// addRepoMapTool registers the orientation tool.
func addRepoMapTool(s *mcp.Server, defaultRoot string) {
	mcp.AddTool(s, &mcp.Tool{
		Name: "repo_map",
		Description: "Module path, library generation, package tree with exported symbols, and the " +
			"FX composition root. Call once to orient; pass `package` for one package in full.",
	}, repoMapHandler(defaultRoot))
}

func repoMapHandler(defaultRoot string) mcp.ToolHandlerFor[RepoMapInput, repomap.Map] {
	return func(_ context.Context, _ *mcp.CallToolRequest, in RepoMapInput) (*mcp.CallToolResult, repomap.Map, error) {
		root, err := rootFor(defaultRoot, in.Root)
		if err != nil {
			return nil, repomap.Map{}, err
		}
		ws, err := workspace.Load(root)
		if err != nil {
			return nil, repomap.Map{}, fmt.Errorf("load workspace: %w", err)
		}
		m := repomap.Build(ws, repomap.Options{Package: in.Package, MaxTokens: in.MaxTokens})
		return nil, *m, nil
	}
}
