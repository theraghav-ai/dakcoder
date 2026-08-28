package catalog

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"
)

func build(t *testing.T) *Catalog {
	t.Helper()
	c, err := Build("test")
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	return c
}

// TestCatalogueConformsToC1 is the check that matters most, because the
// catalogue is the artefact other teams build against: a non-conforming schema
// that reaches this file has already been consumed by the time a test would
// catch it elsewhere.
func TestCatalogueConformsToC1(t *testing.T) {
	c := build(t)
	if len(c.Tools) == 0 {
		t.Fatal("no tools in the catalogue")
	}
	for _, v := range c.Conformance() {
		t.Errorf("%s: %s", v.Tool, v.Detail)
	}
}

func TestCatalogueCoversEveryTool(t *testing.T) {
	c := build(t)
	got := make([]string, 0, len(c.Tools))
	for _, tool := range c.Tools {
		got = append(got, tool.Name)
	}
	// Alphabetical, as the catalogue is built. The four *_audit / *_check tools
	// are read-only reports added for the code-review findings; every one of
	// them must stay out of the mutating set asserted below.
	want := []string{
		"db_roundtrip_audit", "fx_wire", "legacy_audit", "lib_version_check",
		"list_rules", "project_scaffold", "repo_map", "resource_scaffold",
		"rules_lint", "temporal_audit", "validation_audit",
	}
	if !slices.Equal(got, want) {
		t.Errorf("tools = %v, want %v", got, want)
	}
}

// TestMutatingToolsAreDeclaredNotGuessed: "does this write" is what the
// approval gate keys on (Part A §7.2). A write-side tool missing from the set
// would be auto-approved, so the set is declared — and this asserts it names
// only tools that exist, which is the way it would rot.
func TestMutatingToolsAreDeclaredNotGuessed(t *testing.T) {
	c := build(t)
	present := map[string]bool{}
	for _, tool := range c.Tools {
		present[tool.Name] = true
	}
	for _, name := range MutatingToolNames() {
		if !present[name] {
			t.Errorf("%s is declared as mutating but is not a registered tool", name)
		}
	}

	// Every write-side tool must offer a dry run, or the approval gate has
	// nothing to show a diff from.
	for _, tool := range c.Tools {
		if !tool.Mutates {
			continue
		}
		hasDryRun := false
		for _, p := range tool.Params {
			if p.Name == "dry_run" {
				hasDryRun = true
			}
		}
		if !hasDryRun {
			t.Errorf("%s mutates but takes no dry_run; the approval gate cannot preview it", tool.Name)
		}
	}
}

func TestSchemasAreValidJSONAndDescribeTheirParameters(t *testing.T) {
	c := build(t)
	for _, tool := range c.Tools {
		var schema map[string]any
		if err := json.Unmarshal(tool.InputSchema, &schema); err != nil {
			t.Errorf("%s input schema is not valid JSON: %v", tool.Name, err)
			continue
		}
		if schema["type"] != "object" {
			t.Errorf("%s input schema type = %v, want object", tool.Name, schema["type"])
		}
		for _, p := range tool.Params {
			if p.Type == "" {
				t.Errorf("%s parameter %s has no type", tool.Name, p.Name)
			}
			// A parameter the model cannot interpret costs a turn. Nested spec
			// objects carry their description on the referenced type instead.
			if p.Description == "" && p.Type != "object" {
				t.Errorf("%s parameter %s has no description", tool.Name, p.Name)
			}
		}
	}
}

// TestRenderingIsDeterministic: the catalogue is committed and checked for
// freshness, so an unstable rendering means a spurious diff on every run and a
// check that gets ignored.
func TestRenderingIsDeterministic(t *testing.T) {
	first, err := Build("test")
	if err != nil {
		t.Fatal(err)
	}
	firstJSON, err := first.JSON()
	if err != nil {
		t.Fatal(err)
	}
	firstMD := first.Markdown()

	for i := range 3 {
		next, err := Build("test")
		if err != nil {
			t.Fatal(err)
		}
		nextJSON, err := next.JSON()
		if err != nil {
			t.Fatal(err)
		}
		if string(nextJSON) != string(firstJSON) {
			t.Fatalf("run %d produced different JSON", i)
		}
		if string(next.Markdown()) != string(firstMD) {
			t.Fatalf("run %d produced different markdown", i)
		}
	}
}

func TestMarkdownDocumentsEveryTool(t *testing.T) {
	c := build(t)
	md := string(c.Markdown())

	if !strings.Contains(md, "Generated.") {
		t.Error("the document should say it is generated, or someone will edit it")
	}
	for _, tool := range c.Tools {
		if !strings.Contains(md, "## "+tool.Name) {
			t.Errorf("%s has no section", tool.Name)
		}
		if !strings.Contains(md, tool.Description) {
			t.Errorf("%s description is missing", tool.Name)
		}
		for _, p := range tool.Params {
			if !strings.Contains(md, "`"+p.Name+"`") {
				t.Errorf("%s parameter %s is not documented", tool.Name, p.Name)
			}
		}
	}
}

// TestConformanceCatchesABreach proves the check can fail; a conformance test
// that only ever sees conforming input asserts nothing.
func TestConformanceCatchesABreach(t *testing.T) {
	bad := &Catalog{Tools: []Tool{
		{Name: "no_description", InputSchema: json.RawMessage(`{"type":"object"}`)},
		{Name: "too_long", Description: strings.Repeat("x", MaxDescriptionChar+1),
			InputSchema: json.RawMessage(`{"type":"object"}`)},
		{Name: "too_many", Description: "fine", InputSchema: json.RawMessage(`{"type":"object"}`),
			Params: make([]Param, MaxParams+1)},
		{Name: "no_schema", Description: "fine"},
	}}
	got := bad.Conformance()
	if len(got) != 4 {
		t.Fatalf("got %d violations, want 4: %v", len(got), got)
	}
	byTool := map[string]string{}
	for _, v := range got {
		byTool[v.Tool] = v.Detail
	}
	for _, name := range []string{"no_description", "too_long", "too_many", "no_schema"} {
		if byTool[name] == "" {
			t.Errorf("%s was not reported", name)
		}
	}
}

// TestParamsOfHandlesNullableTypes: the SDK emits `"type": ["string","null"]`
// for a nullable field, which is valid JSON Schema and is not a string.
func TestParamsOfHandlesNullableTypes(t *testing.T) {
	schema := json.RawMessage(`{
		"type": "object",
		"properties": {
			"plain":    {"type": "string",           "description": "a"},
			"nullable": {"type": ["array", "null"],  "description": "b"},
			"nested":   {"$ref": "#/$defs/Thing"}
		},
		"required": ["plain"]
	}`)
	params, err := paramsOf(schema)
	if err != nil {
		t.Fatalf("paramsOf: %v", err)
	}
	byName := map[string]Param{}
	for _, p := range params {
		byName[p.Name] = p
	}
	if got := byName["plain"].Type; got != "string" {
		t.Errorf("plain type = %q", got)
	}
	if got := byName["nullable"].Type; got != "array or null" {
		t.Errorf("nullable type = %q, want \"array or null\"", got)
	}
	if got := byName["nested"].Type; got != "object" {
		t.Errorf("a $ref parameter should render as object, got %q", got)
	}
	// Required first, so a reader meets the mandatory arguments before the rest.
	if params[0].Name != "plain" || !params[0].Required {
		t.Errorf("required parameters should sort first, got %v", params)
	}
}
