package mcpserver

import (
	"context"
	"strconv"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/libversion"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/rules"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// The four audits, exposed to the agent.
//
// They are separate tools rather than more rules because they answer a
// different question. `rules_lint` says "this edit is wrong"; these say "here is
// the shape of the problem across the service", which is what a human reviewer
// produced by hand for 41 services and what the agent needs when it is asked to
// improve something rather than to add something.
//
// Every description says when to call it. The agent pays for each call out of
// its context budget, so a tool that does not say when it is worth the spend
// gets called either always or never.

// AuditInput is the shared input: which workspace to look at.
type AuditInput struct {
	Root string `json:"root,omitempty" jsonschema:"workspace root; omit to use the server's default"`
}

// RoundTripOutput profiles the service's database round trips.
type RoundTripOutput struct {
	Methods []rules.RoundTripReport `json:"methods"`
	Summary string                  `json:"summary"`
}

// ValidationOutput lists request fields with nothing bounding them.
type ValidationOutput struct {
	Fields  []rules.ValidationReport `json:"fields"`
	Summary string                   `json:"summary"`
}

// TemporalOutput lists work done on the request path.
type TemporalOutput struct {
	Candidates []rules.TemporalCandidate `json:"candidates"`
	Summary    string                    `json:"summary"`
	Note       string                    `json:"note"`
}

// LibVersionOutput reports CEPT library drift.
type LibVersionOutput struct {
	Result  *libversion.Result `json:"result"`
	Summary string             `json:"summary"`
	Note    string             `json:"note"`
}

func addAuditTools(s *mcp.Server, defaultRoot string) {
	mcp.AddTool(s, &mcp.Tool{
		Name: "db_roundtrip_audit",
		Description: "Per repository method: database calls, whether any is in a loop, batched, " +
			"in a transaction, plus a verdict. Worst first. Call before optimising by eye.",
	}, roundTripHandler(defaultRoot))

	mcp.AddTool(s, &mcp.Tool{
		Name: "validation_audit",
		Description: "Every request field, its validate tag, and what the tag leaves unbounded. " +
			"Call when writing or reviewing request DTOs: `required` alone means only 'not " +
			"empty', so a 10MB string passes.",
	}, validationHandler(defaultRoot))

	mcp.AddTool(s, &mcp.Tool{
		Name: "temporal_audit",
		Description: "Inline work that may belong off the request path: uploads, SMS, email, " +
			"reports, outbound calls. Candidates only, no recommendation. Call when asked about " +
			"async or Temporal.",
	}, temporalHandler(defaultRoot))

	mcp.AddTool(s, &mcp.Tool{
		Name: "lib_version_check",
		Description: "CEPT library drift: which are behind, which are superseded by the n-api-* " +
			"generation. Reports only — never edit go.mod on it; tell the user. Call when asked " +
			"about versions or migration.",
	}, libVersionHandler(defaultRoot))
}

// auditWorkspace resolves the root and parses it, the way every audit needs.
func auditWorkspace(defaultRoot, in string) (*workspace.Workspace, error) {
	root := defaultRoot
	if strings.TrimSpace(in) != "" {
		resolved, err := resolveRoot(defaultRoot, in)
		if err != nil {
			return nil, err
		}
		root = resolved
	}
	return workspace.Load(root)
}

func roundTripHandler(defaultRoot string) mcp.ToolHandlerFor[AuditInput, RoundTripOutput] {
	return func(_ context.Context, _ *mcp.CallToolRequest, in AuditInput) (*mcp.CallToolResult, RoundTripOutput, error) {
		ws, err := auditWorkspace(defaultRoot, in.Root)
		if err != nil {
			return nil, RoundTripOutput{}, err
		}
		methods := rules.RoundTripAudit(ws)
		var attention int
		for _, m := range methods {
			if m.Verdict != "ok" {
				attention++
			}
		}
		return nil, RoundTripOutput{
			Methods: methods,
			Summary: plural(len(methods), "repository method") + " touch the database, " +
				plural(attention, "worth a look") + "; the list is ordered by cost",
		}, nil
	}
}

func validationHandler(defaultRoot string) mcp.ToolHandlerFor[AuditInput, ValidationOutput] {
	return func(_ context.Context, _ *mcp.CallToolRequest, in AuditInput) (*mcp.CallToolResult, ValidationOutput, error) {
		ws, err := auditWorkspace(defaultRoot, in.Root)
		if err != nil {
			return nil, ValidationOutput{}, err
		}
		fields := rules.ValidationAudit(ws)
		var unbounded int
		for _, f := range fields {
			if f.Missing != "" {
				unbounded++
			}
		}
		return nil, ValidationOutput{
			Fields: fields,
			Summary: plural(len(fields), "request field") + ", " +
				plural(unbounded, "with nothing bounding it"),
		}, nil
	}
}

func temporalHandler(defaultRoot string) mcp.ToolHandlerFor[AuditInput, TemporalOutput] {
	return func(_ context.Context, _ *mcp.CallToolRequest, in AuditInput) (*mcp.CallToolResult, TemporalOutput, error) {
		ws, err := auditWorkspace(defaultRoot, in.Root)
		if err != nil {
			return nil, TemporalOutput{}, err
		}
		candidates := rules.TemporalAudit(ws)
		return nil, TemporalOutput{
			Candidates: candidates,
			Summary:    plural(len(candidates), "candidate") + " doing work inline",
			Note: "Candidates only. Do not move any of this without asking: the template has " +
				"no Temporal wiring, and where the work belongs is a decision about what " +
				"should happen when it fails halfway through.",
		}, nil
	}
}

func libVersionHandler(defaultRoot string) mcp.ToolHandlerFor[AuditInput, LibVersionOutput] {
	return func(ctx context.Context, _ *mcp.CallToolRequest, in AuditInput) (*mcp.CallToolResult, LibVersionOutput, error) {
		ws, err := auditWorkspace(defaultRoot, in.Root)
		if err != nil {
			return nil, LibVersionOutput{}, err
		}
		res := libversion.Check(ctx, ws, libversion.GoListLister{Dir: ws.Root})
		return nil, LibVersionOutput{
			Result:  res,
			Summary: res.Summary(),
			Note: "Report only. Do not edit go.mod — tell the user what is available and let " +
				"them decide. A library bump mid-review turns a review into a regression hunt.",
		}, nil
	}
}

// plural renders "3 things" / "1 thing" / "no things".
func plural(n int, noun string) string {
	switch n {
	case 0:
		return "no " + noun + "s"
	case 1:
		return "1 " + noun
	default:
		return strconv.Itoa(n) + " " + noun + "s"
	}
}
