// Package mcpserver exposes the gotools analysis over the Model Context
// Protocol on stdio, so the agent can call it as a tool.
//
// Tool descriptions here are written as instructions to the model, not as
// documentation for a human. That is deliberate and it is the cheapest place to
// steer behaviour: telling the model in the schema to pass `paths` costs
// nothing at runtime and saves a turn of it linting the whole repository and
// then trying to fix findings it did not cause.
package mcpserver

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/rules"
)

// LintInput is the argument shape for rules_lint and legacy_audit.
type LintInput struct {
	Root  string   `json:"root,omitempty" jsonschema:"workspace root; omit to use the server's default"`
	Paths []string `json:"paths,omitempty" jsonschema:"workspace-relative files you just changed. Pass these so the check is scoped to your edits. Omit ONLY for a full audit the user explicitly asked for"`
	Only  []string `json:"only,omitempty" jsonschema:"subset of rule ids to run; omit to run all"`
}

// LintOutput is the structured result. It mirrors rules.Result so the agent
// sees exactly what the CLI and CI see.
type LintOutput struct {
	OK              bool              `json:"ok" jsonschema:"true when there are no blocking violations in scope"`
	Count           int               `json:"count" jsonschema:"number of blocking violations"`
	Violations      []rules.Violation `json:"violations" jsonschema:"blocking violations, each with a fix and a citation"`
	OutOfScopeCount int               `json:"out_of_scope_count" jsonschema:"pre-existing violations in files you did not touch; these do not block and you should not fix them unless asked"`
	Warnings        []rules.Violation `json:"warnings,omitempty" jsonschema:"non-blocking advice"`
	FilesScanned    int               `json:"files_scanned"`
	DurationMS      int64             `json:"duration_ms"`
}

// RulesInput lists the rule set.
type RulesInput struct {
	Legacy bool `json:"legacy,omitempty" jsonschema:"true to list legacy-detection rules instead of compliance rules"`
}

// RuleInfo describes one rule.
type RuleInfo struct {
	ID       string `json:"id"`
	Severity string `json:"severity"`
	Summary  string `json:"summary"`
	Citation string `json:"citation,omitempty"`
}

// RulesOutput is the rule listing.
type RulesOutput struct {
	Rules []RuleInfo `json:"rules"`
}

// Serve runs the MCP server on stdio until the context is cancelled.
func Serve(ctx context.Context, defaultRoot, version string) error {
	s, err := NewServer(defaultRoot, version)
	if err != nil {
		return err
	}
	return s.Run(ctx, &mcp.StdioTransport{})
}

// NewServer builds the server with every tool registered.
//
// Separate from Serve so the tool set can be exercised in-process over an
// in-memory transport. Contract C1 constrains the schemas — at most six
// parameters, a description of at most 200 characters written as an instruction
// to the model — and a contract nobody checks is a comment, so there is a test
// that walks exactly this registration.
func NewServer(root, version string) (*mcp.Server, error) {
	abs, err := filepath.Abs(root)
	if err != nil {
		return nil, fmt.Errorf("resolve root: %w", err)
	}

	s := mcp.NewServer(&mcp.Implementation{
		Name:    "gotools",
		Title:   "dakcoder Go analysis",
		Version: version,
	}, nil)

	mcp.AddTool(s, &mcp.Tool{
		Name: "rules_lint",
		Description: "Check Go against the n-api-template contract: layer boundaries, handler " +
			"signature, repository contract, DTO envelopes, FX wiring. Run after each edit " +
			"batch, passing `paths` with the files you changed.",
	}, lintHandler(abs, false))

	mcp.AddTool(s, &mcp.Tool{
		Name: "legacy_audit",
		Description: "Detect pre-template (api-*) patterns in an existing service: routes.go, " +
			"gin handlers, manual validation, swaggo docs, handleSuccess helpers. " +
			"Use when planning a migration, not during ordinary edits.",
	}, lintHandler(abs, true))

	mcp.AddTool(s, &mcp.Tool{
		Name: "list_rules",
		Description: "List the rule ids, severities and citations. Call this before " +
			"explaining a violation so you quote the real rule id and its source.",
	}, rulesHandler)

	addScaffoldTools(s, abs)
	addRepoMapTool(s, abs)
	addAuditTools(s, abs)
	return s, nil
}

func lintHandler(defaultRoot string, legacy bool) mcp.ToolHandlerFor[LintInput, LintOutput] {
	return func(ctx context.Context, _ *mcp.CallToolRequest, in LintInput) (*mcp.CallToolResult, LintOutput, error) {
		root := defaultRoot
		if strings.TrimSpace(in.Root) != "" {
			resolved, err := resolveRoot(defaultRoot, in.Root)
			if err != nil {
				return nil, LintOutput{}, err
			}
			root = resolved
		}

		res, err := rules.Analyze(root, rules.RunOptions{
			Only:   in.Only,
			Scope:  in.Paths,
			Legacy: legacy,
		})
		if err != nil {
			// Returned as a tool error, not a protocol error: the agent should
			// see the message and correct its call, not have the session fail.
			return nil, LintOutput{}, fmt.Errorf("analyze: %w", err)
		}

		return nil, LintOutput{
			OK:              res.OK,
			Count:           res.Count,
			Violations:      res.Violations,
			OutOfScopeCount: res.OutOfScopeCount,
			Warnings:        res.Warnings,
			FilesScanned:    res.FilesScanned,
			DurationMS:      res.DurationMS,
		}, nil
	}
}

func rulesHandler(_ context.Context, _ *mcp.CallToolRequest, in RulesInput) (*mcp.CallToolResult, RulesOutput, error) {
	var out RulesOutput
	for _, r := range rules.Default().All() {
		if r.Legacy != in.Legacy {
			continue
		}
		out.Rules = append(out.Rules, RuleInfo{
			ID:       r.ID,
			Severity: string(r.Severity),
			Summary:  r.Summary,
			Citation: r.Citation,
		})
	}
	return nil, out, nil
}

// resolveRoot keeps a caller-supplied root inside the server's root.
//
// The agent is not a trusted caller: its `root` argument is ultimately derived
// from model output, and an absolute path or a `..` traversal would let a
// prompt-injected instruction point the analyser at anything readable on the
// machine. This is the same containment the Python side applies to every file
// path, applied here because the sidecar is a separate process and must not
// rely on its caller having checked.
func resolveRoot(base, req string) (string, error) {
	if filepath.IsAbs(req) {
		// An absolute path is allowed only if it is the base or inside it.
		clean := filepath.Clean(req)
		if clean == base || strings.HasPrefix(clean, base+string(filepath.Separator)) {
			return clean, nil
		}
		return "", fmt.Errorf("root %q is outside the served workspace", req)
	}
	joined := filepath.Clean(filepath.Join(base, req))
	if joined != base && !strings.HasPrefix(joined, base+string(filepath.Separator)) {
		return "", fmt.Errorf("root %q escapes the served workspace", req)
	}
	return joined, nil
}
