package mcpserver

import (
	"context"
	"fmt"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/fxwire"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/scaffold"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/spec"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// FileSummary is one file in a scaffold result.
//
// Content is present only on a dry run. A written scaffold returns the manifest
// alone, which is a few hundred tokens rather than several thousand: the caller
// already has the files on disk, and re-sending them into the model's context
// would be the single largest tool result in the whole catalogue for no benefit
// (Part A §6.2).
type FileSummary struct {
	Path    string `json:"path"`
	Action  string `json:"action" jsonschema:"create or modify"`
	Bytes   int    `json:"bytes"`
	Content string `json:"content,omitempty" jsonschema:"the file's full content; present only on a dry run"`
}

// ScaffoldOutput is the result of a scaffold.
type ScaffoldOutput struct {
	OK      bool          `json:"ok"`
	Written bool          `json:"written" jsonschema:"false on a dry run, when nothing was written"`
	Files   []FileSummary `json:"files"`
	Notes   []string      `json:"notes,omitempty" jsonschema:"steps the scaffolder deliberately left for a human; relay these"`
	Module  string        `json:"module,omitempty"`
}

// ResourceScaffoldInput is the argument shape for resource_scaffold.
type ResourceScaffoldInput struct {
	Spec   spec.Resource `json:"spec" jsonschema:"the resource to scaffold"`
	Root   string        `json:"root,omitempty" jsonschema:"workspace root; omit to use the server's default"`
	DryRun bool          `json:"dry_run,omitempty" jsonschema:"true to return the files without writing them"`
}

// ProjectScaffoldInput is the argument shape for project_scaffold.
type ProjectScaffoldInput struct {
	Project  scaffold.Project `json:"project" jsonschema:"the service to create"`
	Resource spec.Resource    `json:"resource" jsonschema:"one working resource to seed the service with"`
	Root     string           `json:"root,omitempty" jsonschema:"target directory; omit to use the server's default"`
	DryRun   bool             `json:"dry_run,omitempty" jsonschema:"true to return the files without writing them"`
}

// FxWireInput is the argument shape for fx_wire.
type FxWireInput struct {
	Kind   string `json:"kind" jsonschema:"repo for a repository constructor, handler for a handler constructor"`
	Ctor   string `json:"ctor" jsonschema:"the constructor's bare name, e.g. NewPensionHandler"`
	Root   string `json:"root,omitempty" jsonschema:"workspace root; omit to use the server's default"`
	DryRun bool   `json:"dry_run,omitempty" jsonschema:"true to return the patched file without writing it"`
}

// FxWireOutput reports what was registered.
type FxWireOutput struct {
	OK                bool     `json:"ok"`
	Path              string   `json:"path"`
	Changed           bool     `json:"changed"`
	Written           bool     `json:"written"`
	Added             []string `json:"added,omitempty"`
	AlreadyRegistered []string `json:"already_registered,omitempty" jsonschema:"constructors that were already wired; this is success, not a failure"`
	Content           string   `json:"content,omitempty" jsonschema:"the patched file; present only on a dry run"`
}

// addScaffoldTools registers the write-side tools.
func addScaffoldTools(s *mcp.Server, defaultRoot string) {
	mcp.AddTool(s, &mcp.Tool{
		Name: "resource_scaffold",
		Description: "Write a whole CRUD resource — domain, DDL, repository, DTOs, handler, FX " +
			"registration — from a field spec. Use this instead of writing the files yourself.",
	}, resourceScaffoldHandler(defaultRoot))

	mcp.AddTool(s, &mcp.Tool{
		Name: "project_scaffold",
		Description: "Create a new n-api-template service in an empty directory, seeded with one " +
			"working resource. Greenfield only; to add to an existing service use resource_scaffold.",
	}, projectScaffoldHandler(defaultRoot))

	mcp.AddTool(s, &mcp.Tool{
		Name: "fx_wire",
		Description: "Register a repository or handler in bootstrap/bootstrapper.go with the " +
			"correct annotation. Never hand-edit it: an unannotated handler serves no routes.",
	}, fxWireHandler(defaultRoot))
}

func resourceScaffoldHandler(defaultRoot string) mcp.ToolHandlerFor[ResourceScaffoldInput, ScaffoldOutput] {
	return func(_ context.Context, _ *mcp.CallToolRequest, in ResourceScaffoldInput) (*mcp.CallToolResult, ScaffoldOutput, error) {
		root, err := rootFor(defaultRoot, in.Root)
		if err != nil {
			return nil, ScaffoldOutput{}, err
		}
		res, err := scaffold.Resource(root, in.Spec, scaffold.ResourceOptions{})
		if err != nil {
			return nil, ScaffoldOutput{}, err
		}
		if !in.DryRun {
			if err := scaffold.Apply(root, res); err != nil {
				return nil, ScaffoldOutput{}, err
			}
		}
		return nil, toScaffoldOutput(res, !in.DryRun), nil
	}
}

func projectScaffoldHandler(defaultRoot string) mcp.ToolHandlerFor[ProjectScaffoldInput, ScaffoldOutput] {
	return func(_ context.Context, _ *mcp.CallToolRequest, in ProjectScaffoldInput) (*mcp.CallToolResult, ScaffoldOutput, error) {
		root, err := rootFor(defaultRoot, in.Root)
		if err != nil {
			return nil, ScaffoldOutput{}, err
		}
		res, err := scaffold.NewProject(root, in.Project, in.Resource)
		if err != nil {
			return nil, ScaffoldOutput{}, err
		}
		if !in.DryRun {
			if err := scaffold.Apply(root, res); err != nil {
				return nil, ScaffoldOutput{}, err
			}
		}
		return nil, toScaffoldOutput(res, !in.DryRun), nil
	}
}

func fxWireHandler(defaultRoot string) mcp.ToolHandlerFor[FxWireInput, FxWireOutput] {
	return func(_ context.Context, _ *mcp.CallToolRequest, in FxWireInput) (*mcp.CallToolResult, FxWireOutput, error) {
		root, err := rootFor(defaultRoot, in.Root)
		if err != nil {
			return nil, FxWireOutput{}, err
		}
		module, err := workspace.ModulePath(root)
		if err != nil {
			return nil, FxWireOutput{}, err
		}
		reg := fxwire.Registration{Kind: fxwire.Kind(strings.ToLower(strings.TrimSpace(in.Kind))), Ctor: in.Ctor}

		var res *fxwire.Result
		if in.DryRun {
			res, err = fxwire.Plan(root, module, reg)
		} else {
			res, err = fxwire.Apply(root, module, reg)
		}
		if err != nil {
			return nil, FxWireOutput{}, err
		}

		out := FxWireOutput{
			OK: true, Path: res.Path, Changed: res.Changed,
			Written:           res.Changed && !in.DryRun,
			Added:             res.Added,
			AlreadyRegistered: res.AlreadyRegistered,
		}
		if in.DryRun {
			out.Content = res.Content
		}
		return nil, out, nil
	}
}

func toScaffoldOutput(res *scaffold.Result, written bool) ScaffoldOutput {
	out := ScaffoldOutput{OK: true, Written: written, Notes: res.Notes, Module: res.Module}
	for _, f := range res.Files {
		fs := FileSummary{Path: f.Path, Action: string(f.Action), Bytes: f.Bytes}
		if !written {
			fs.Content = f.Content
		}
		out.Files = append(out.Files, fs)
	}
	return out
}

// rootFor resolves a caller-supplied root against the served workspace.
func rootFor(defaultRoot, requested string) (string, error) {
	if strings.TrimSpace(requested) == "" {
		return defaultRoot, nil
	}
	resolved, err := resolveRoot(defaultRoot, requested)
	if err != nil {
		return "", fmt.Errorf("%w", err)
	}
	return resolved, nil
}
