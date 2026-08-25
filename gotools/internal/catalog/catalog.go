// Package catalog renders the sidecar's half of contract C1.
//
// C1 is the tool schema: OpenAI function-calling JSON Schema, versioned, with
// at most six parameters and a description of at most 200 characters written as
// an instruction to the model. Three components bind to it — the gateway routes
// against it, the sidecar implements it, and the extension renders approvals
// from it — and plan.md §11 makes writing it down the sixth immediate step.
//
// # Why it is generated
//
// A hand-written catalogue is a fourth place the schema lives, and the first
// one to go stale. Everything here is read out of the running MCP server, so
// the document cannot describe a tool that does not exist, omit one that does,
// or disagree with the schema the model is actually sent. `--check` regenerates
// and diffs, which makes staleness a failed build rather than a support ticket.
package catalog

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/mcpserver"
)

// C1 limits, from plan.md §7.
const (
	MaxParams          = 6
	MaxDescriptionChar = 200
)

// Tool is one entry in the catalogue.
type Tool struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Mutates     bool            `json:"mutates"`
	InputSchema json.RawMessage `json:"input_schema"`
	// OutputSchema is absent for tools whose result the SDK does not describe.
	OutputSchema json.RawMessage `json:"output_schema,omitempty"`

	// Params are the top-level input properties, in schema order, for the
	// human-readable rendering.
	Params []Param `json:"-"`
}

// Param is one tool parameter.
type Param struct {
	Name        string
	Type        string
	Required    bool
	Description string
}

// Catalog is the sidecar's published tool set.
type Catalog struct {
	Component string `json:"component"`
	Version   string `json:"version"`
	Contract  string `json:"contract"`
	Tools     []Tool `json:"tools"`
}

// mutatingTools are the tools that write to the workspace.
//
// Declared here rather than inferred, because "does this tool mutate" is an
// approval-gate decision (Part A §7.2) and the gate must not depend on a guess.
// A new tool that writes and is missing from this set would be auto-approved,
// so the catalogue test asserts the set names only tools that exist.
var mutatingTools = map[string]bool{
	"resource_scaffold": true,
	"project_scaffold":  true,
	"fx_wire":           true,
}

// Build reads the catalogue out of a freshly constructed MCP server.
func Build(version string) (*Catalog, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	server, err := mcpserver.NewServer(".", version)
	if err != nil {
		return nil, fmt.Errorf("build server: %w", err)
	}
	serverT, clientT := mcp.NewInMemoryTransports()
	serverSession, err := server.Connect(ctx, serverT, nil)
	if err != nil {
		return nil, fmt.Errorf("connect server: %w", err)
	}
	defer serverSession.Close()

	client := mcp.NewClient(&mcp.Implementation{Name: "catalog", Version: version}, nil)
	session, err := client.Connect(ctx, clientT, nil)
	if err != nil {
		return nil, fmt.Errorf("connect client: %w", err)
	}
	defer session.Close()

	listed, err := session.ListTools(ctx, &mcp.ListToolsParams{})
	if err != nil {
		return nil, fmt.Errorf("list tools: %w", err)
	}

	cat := &Catalog{
		Component: "gotools",
		Version:   version,
		Contract:  "C1",
	}
	for _, t := range listed.Tools {
		in, err := marshalSchema(t.InputSchema)
		if err != nil {
			return nil, fmt.Errorf("%s input schema: %w", t.Name, err)
		}
		out, err := marshalSchema(t.OutputSchema)
		if err != nil {
			return nil, fmt.Errorf("%s output schema: %w", t.Name, err)
		}
		tool := Tool{
			Name:         t.Name,
			Description:  t.Description,
			Mutates:      mutatingTools[t.Name],
			InputSchema:  in,
			OutputSchema: out,
		}
		tool.Params, err = paramsOf(in)
		if err != nil {
			return nil, fmt.Errorf("%s parameters: %w", t.Name, err)
		}
		cat.Tools = append(cat.Tools, tool)
	}
	sort.Slice(cat.Tools, func(i, j int) bool { return cat.Tools[i].Name < cat.Tools[j].Name })
	return cat, nil
}

// MutatingToolNames lists the declared write-side tools, for tests.
func MutatingToolNames() []string {
	out := make([]string, 0, len(mutatingTools))
	for name := range mutatingTools {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

func marshalSchema(s any) (json.RawMessage, error) {
	if s == nil {
		return nil, nil
	}
	b, err := json.Marshal(s)
	if err != nil {
		return nil, err
	}
	if string(b) == "null" {
		return nil, nil
	}
	// Re-encode through a map so the key order is stable: encoding/json sorts
	// map keys, and an unstable catalogue produces a diff on every run.
	var generic any
	if err := json.Unmarshal(b, &generic); err != nil {
		return nil, err
	}
	return json.Marshal(generic)
}

// paramsOf extracts the top-level input properties.
func paramsOf(schema json.RawMessage) ([]Param, error) {
	if len(schema) == 0 {
		return nil, nil
	}
	var s struct {
		Properties map[string]struct {
			// JSON Schema permits both `"type": "string"` and
			// `"type": ["string", "null"]`, and the SDK emits the second form
			// for nullable fields — so this cannot be a plain string.
			Type        json.RawMessage `json:"type"`
			Description string          `json:"description"`
			Ref         string          `json:"$ref"`
		} `json:"properties"`
		Required []string `json:"required"`
	}
	if err := json.Unmarshal(schema, &s); err != nil {
		return nil, err
	}
	required := map[string]bool{}
	for _, r := range s.Required {
		required[r] = true
	}
	out := make([]Param, 0, len(s.Properties))
	for name, p := range s.Properties {
		typ := renderType(p.Type)
		if typ == "" {
			typ = "object"
		}
		out = append(out, Param{
			Name: name, Type: typ,
			Required:    required[name],
			Description: p.Description,
		})
	}
	// Required parameters first, then alphabetical: that is the order a reader
	// needs them in, and it is stable.
	sort.Slice(out, func(i, j int) bool {
		if out[i].Required != out[j].Required {
			return out[i].Required
		}
		return out[i].Name < out[j].Name
	})
	return out, nil
}

// renderType flattens a JSON Schema type, which is either a string or a list of
// them. A nullable field arrives as ["string","null"] and reads better in the
// document as "string or null" than as raw JSON.
func renderType(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var one string
	if err := json.Unmarshal(raw, &one); err == nil {
		return one
	}
	var many []string
	if err := json.Unmarshal(raw, &many); err == nil {
		return strings.Join(many, " or ")
	}
	return strings.Trim(string(raw), `"`)
}

// ── conformance ─────────────────────────────────────────────────────────────

// Violation is a C1 breach.
type Violation struct {
	Tool   string `json:"tool"`
	Detail string `json:"detail"`
}

// Conformance checks the catalogue against C1's limits.
//
// The same limits the mcpserver test asserts, checked again here because this
// is the document other teams build against: a catalogue that publishes a
// non-conforming schema is worse than a test that fails, since the schema has
// already been consumed by then.
func (c *Catalog) Conformance() []Violation {
	var out []Violation
	for _, t := range c.Tools {
		if t.Description == "" {
			out = append(out, Violation{t.Name, "no description"})
		}
		if n := len(t.Description); n > MaxDescriptionChar {
			out = append(out, Violation{t.Name,
				fmt.Sprintf("description is %d characters, over the %d-character cap", n, MaxDescriptionChar)})
		}
		if n := len(t.Params); n > MaxParams {
			out = append(out, Violation{t.Name,
				fmt.Sprintf("takes %d parameters, over the %d-parameter cap", n, MaxParams)})
		}
		if len(t.InputSchema) == 0 {
			out = append(out, Violation{t.Name, "no input schema"})
		}
	}
	return out
}

// ── rendering ───────────────────────────────────────────────────────────────

// JSON renders the machine-readable catalogue — the artefact the gateway and
// the extension actually bind to.
func (c *Catalog) JSON() ([]byte, error) {
	b, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return nil, err
	}
	return append(b, '\n'), nil
}

// Markdown renders the human-readable catalogue.
func (c *Catalog) Markdown() []byte {
	var b bytes.Buffer

	b.WriteString("# Tool catalogue — `gotools`\n\n")
	b.WriteString("> **Generated.** Do not edit. Run `make tool-catalog` and commit the result.\n")
	b.WriteString("> Regenerating is how this file stays true; editing it is how it stops being.\n\n")
	b.WriteString("The sidecar's half of contract **C1** (plan.md §7). The gateway routes against\n")
	b.WriteString("these schemas, the extension renders approvals from them, and the model is sent\n")
	b.WriteString("them verbatim — so they are re-sent on every turn, which is why the size limits\n")
	b.WriteString("below are limits and not preferences.\n\n")
	fmt.Fprintf(&b, "C1 limits: at most **%d parameters** per tool, description at most **%d characters**, "+
		"written as an instruction to the model rather than documentation for a human.\n\n", MaxParams, MaxDescriptionChar)

	b.WriteString("| Tool | Mutates | Params | Description |\n|---|---|---|---|\n")
	for _, t := range c.Tools {
		mut := ""
		if t.Mutates {
			mut = "✓"
		}
		fmt.Fprintf(&b, "| [`%s`](#%s) | %s | %d | %s |\n",
			t.Name, t.Name, mut, len(t.Params), t.Description)
	}

	b.WriteString("\nA tool marked **Mutates** writes to the workspace and passes through the\n")
	b.WriteString("approval gate (Part A §7.2). Each of them takes `dry_run`, which returns the\n")
	b.WriteString("full result without touching the working tree — that is what the gate uses to\n")
	b.WriteString("show a diff before anything is written.\n")

	for _, t := range c.Tools {
		fmt.Fprintf(&b, "\n---\n\n## %s\n\n%s\n\n", t.Name, t.Description)
		if t.Mutates {
			b.WriteString("**Mutates the workspace.** Approval-gated.\n\n")
		}
		if len(t.Params) == 0 {
			b.WriteString("Takes no parameters.\n")
		} else {
			b.WriteString("| Parameter | Type | Required | Description |\n|---|---|---|---|\n")
			for _, p := range t.Params {
				req := ""
				if p.Required {
					req = "yes"
				}
				fmt.Fprintf(&b, "| `%s` | %s | %s | %s |\n",
					p.Name, p.Type, req, strings.ReplaceAll(p.Description, "|", "\\|"))
			}
		}
		b.WriteString("\n<details><summary>Input schema</summary>\n\n```json\n")
		b.Write(indentJSON(t.InputSchema))
		b.WriteString("\n```\n\n</details>\n")
		if len(t.OutputSchema) > 0 {
			b.WriteString("\n<details><summary>Output schema</summary>\n\n```json\n")
			b.Write(indentJSON(t.OutputSchema))
			b.WriteString("\n```\n\n</details>\n")
		}
	}
	return b.Bytes()
}

func indentJSON(raw json.RawMessage) []byte {
	var out bytes.Buffer
	if err := json.Indent(&out, raw, "", "  "); err != nil {
		return raw
	}
	return out.Bytes()
}
