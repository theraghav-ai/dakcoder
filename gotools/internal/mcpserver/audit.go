package mcpserver

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

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
//
// Only the methods that need attention. The CLI lists everything behind
// `--all`, and that is the right default there — a person scrolls. An agent
// does not: this tool returned all 84 methods of one legacy service, 76 of them
// with the verdict "ok", which is 15KB of JSON to say eight things. The counts
// carry what the omitted rows would have said.
type RoundTripOutput struct {
	Methods      []rules.RoundTripReport `json:"methods"`
	TotalScanned int                     `json:"total_scanned"`
	Healthy      int                     `json:"healthy"`
	Summary      string                  `json:"summary"`
}

// ValidationOutput lists request fields with nothing bounding them.
//
// Deficient fields only, for the same reason. A service with 400 well-bounded
// fields and 3 bad ones should send 3 rows and a number, not 403.
type ValidationOutput struct {
	Fields       []rules.ValidationReport `json:"fields"`
	TotalScanned int                      `json:"total_scanned"`
	Bounded      int                      `json:"bounded"`
	Summary      string                   `json:"summary"`
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
		all := rules.RoundTripAudit(ws)
		needsWork := make([]rules.RoundTripReport, 0, len(all))
		for _, m := range all {
			if m.Verdict != "ok" {
				needsWork = append(needsWork, m)
			}
		}
		return nil, RoundTripOutput{
			Methods:      needsWork,
			TotalScanned: len(all),
			Healthy:      len(all) - len(needsWork),
			Summary: fmt.Sprintf(
				"%d of %s need attention, worst first; the other %d are fine",
				len(needsWork), plural(len(all), "repository method"), len(all)-len(needsWork),
			),
		}, nil
	}
}

func validationHandler(defaultRoot string) mcp.ToolHandlerFor[AuditInput, ValidationOutput] {
	return func(_ context.Context, _ *mcp.CallToolRequest, in AuditInput) (*mcp.CallToolResult, ValidationOutput, error) {
		ws, err := auditWorkspace(defaultRoot, in.Root)
		if err != nil {
			return nil, ValidationOutput{}, err
		}
		all := rules.ValidationAudit(ws)
		unbounded := make([]rules.ValidationReport, 0, len(all))
		for _, f := range all {
			if f.Missing != "" {
				unbounded = append(unbounded, f)
			}
		}
		return nil, ValidationOutput{
			Fields:       unbounded,
			TotalScanned: len(all),
			Bounded:      len(all) - len(unbounded),
			Summary: fmt.Sprintf(
				"%d of %s have nothing bounding them",
				len(unbounded), plural(len(all), "request field"),
			),
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
		// One short budget for the whole report, not per module.
		//
		// The lookup shells out to `go list -m -versions`, which reaches the
		// GitLab over the network. On a machine that cannot see it, each module
		// burns its own timeout in turn — six modules at thirty seconds is three
		// minutes of an agent turn spent on a tool whose answer is "reports
		// only". Five seconds total is enough when the registry is reachable and
		// short enough not to matter when it is not: the supersession half of
		// the report needs no network and is still produced.
		ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()

		res := libversion.Check(ctx, ws, libversion.GoListLister{Dir: ws.Root, Timeout: 5 * time.Second})
		note := "Report only. Do not edit go.mod — tell the user what is available and let " +
			"them decide. A library bump mid-review turns a review into a regression hunt."
		if !res.Reachable {
			note = "The package registry was not reachable, so only supersession is reported " +
				"— the 'behind by N' half is missing. " + note
		}
		return nil, LibVersionOutput{Result: res, Summary: res.Summary(), Note: note}, nil
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
