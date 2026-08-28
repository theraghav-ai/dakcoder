package mcpserver

import (
	"context"
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/repomap"
)

// connect starts the server over an in-memory transport and returns a connected
// client session.
//
// In-process rather than by spawning the binary: the point is to exercise the
// registration in NewServer, and a subprocess would additionally test the build
// — which is what the CLI tests are for.
func connect(t *testing.T, root string) *mcp.ClientSession {
	t.Helper()
	ctx := context.Background()

	server, err := NewServer(root, "test")
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	serverTransport, clientTransport := mcp.NewInMemoryTransports()

	serverSession, err := server.Connect(ctx, serverTransport, nil)
	if err != nil {
		t.Fatalf("server connect: %v", err)
	}
	t.Cleanup(func() { _ = serverSession.Close() })

	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"}, nil)
	session, err := client.Connect(ctx, clientTransport, nil)
	if err != nil {
		t.Fatalf("client connect: %v", err)
	}
	t.Cleanup(func() { _ = session.Close() })
	return session
}

func listTools(t *testing.T, s *mcp.ClientSession) []*mcp.Tool {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	res, err := s.ListTools(ctx, &mcp.ListToolsParams{})
	if err != nil {
		t.Fatalf("tools/list: %v", err)
	}
	return res.Tools
}

func call(t *testing.T, s *mcp.ClientSession, name string, args any) *mcp.CallToolResult {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	res, err := s.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatalf("tools/call %s: %v", name, err)
	}
	return res
}

func structured[T any](t *testing.T, res *mcp.CallToolResult) T {
	t.Helper()
	var out T
	if res.IsError {
		t.Fatalf("tool returned an error: %s", resultText(res))
	}
	b, err := json.Marshal(res.StructuredContent)
	if err != nil {
		t.Fatalf("marshal structured content: %v", err)
	}
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatalf("unmarshal structured content: %v\n%s", err, b)
	}
	return out
}

func resultText(res *mcp.CallToolResult) string {
	var b strings.Builder
	for _, c := range res.Content {
		if tc, ok := c.(*mcp.TextContent); ok {
			b.WriteString(tc.Text)
		}
	}
	return b.String()
}

// TestToolSchemasHonourContractC1 enforces the shared contract from plan.md §7:
// at most six parameters, a hand-written schema, and a description of at most
// 200 characters written as an instruction to the model.
//
// The description cap is not cosmetic. Schemas are re-sent on every turn, and
// the frontend agent's measured 3,020 tokens of tool schemas — one tool's
// description alone accounting for 562 of them — is a direct, permanent charge
// against the context budget (Part A §5, finding S5).
func TestToolSchemasHonourContractC1(t *testing.T) {
	session := connect(t, t.TempDir())
	tools := listTools(t, session)

	if len(tools) == 0 {
		t.Fatal("no tools registered")
	}
	for _, tool := range tools {
		if tool.Description == "" {
			t.Errorf("%s has no description", tool.Name)
		}
		if n := len(tool.Description); n > 200 {
			t.Errorf("%s description is %d characters, over the 200-character cap (C1)", tool.Name, n)
		}
		props := schemaProperties(t, tool)
		if props == nil {
			t.Errorf("%s has no input schema", tool.Name)
			continue
		}
		if n := len(props); n > 6 {
			t.Errorf("%s takes %d parameters (%v), over the 6-parameter cap (C1)",
				tool.Name, n, sortedNames(props))
		}
	}
}

// schemaProperties reads the top-level properties out of a tool's input schema,
// which the SDK carries as an untyped value on the wire.
func schemaProperties(t *testing.T, tool *mcp.Tool) map[string]any {
	t.Helper()
	if tool.InputSchema == nil {
		return nil
	}
	b, err := json.Marshal(tool.InputSchema)
	if err != nil {
		t.Fatalf("marshal %s schema: %v", tool.Name, err)
	}
	var schema struct {
		Properties map[string]any `json:"properties"`
	}
	if err := json.Unmarshal(b, &schema); err != nil {
		t.Fatalf("unmarshal %s schema: %v", tool.Name, err)
	}
	return schema.Properties
}

func sortedNames(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// TestToolNamesAreStable: tool names are API. They appear in prompts, in
// playbook filenames, and in the Python tool router, so a rename is a breaking
// change that should be a deliberate edit to this list.
func TestToolNamesAreStable(t *testing.T) {
	session := connect(t, t.TempDir())
	got := map[string]bool{}
	for _, tool := range listTools(t, session) {
		got[tool.Name] = true
	}
	want := []string{
		"rules_lint", "legacy_audit", "list_rules",
		"resource_scaffold", "project_scaffold", "fx_wire", "repo_map",
		// The four audits, added deliberately. They reproduce the sheets the
		// manual review of 41 services was assembled by hand.
		"db_roundtrip_audit", "validation_audit", "temporal_audit", "lib_version_check",
	}
	for _, name := range want {
		if !got[name] {
			t.Errorf("tool %s is not registered", name)
		}
		delete(got, name)
	}
	for name := range got {
		t.Errorf("tool %s is registered but not in the expected set; add it here deliberately", name)
	}
}

func TestRulesLintRoundTrip(t *testing.T) {
	root := templateCopy(t)
	session := connect(t, root)

	out := structured[LintOutput](t, call(t, session, "rules_lint", map[string]any{}))
	if !out.OK {
		t.Errorf("the reference template should lint clean over MCP; got %d violation(s): %v", out.Count, out.Violations)
	}
	if out.FilesScanned == 0 {
		t.Error("no files scanned")
	}
}

func TestListRulesRoundTrip(t *testing.T) {
	session := connect(t, t.TempDir())

	compliance := structured[RulesOutput](t, call(t, session, "list_rules", map[string]any{}))
	legacy := structured[RulesOutput](t, call(t, session, "list_rules", map[string]any{"legacy": true}))

	if len(compliance.Rules) == 0 || len(legacy.Rules) == 0 {
		t.Fatalf("compliance=%d legacy=%d; both sets should be non-empty", len(compliance.Rules), len(legacy.Rules))
	}
	for _, r := range compliance.Rules {
		if strings.HasPrefix(r.ID, "legacy-") {
			t.Errorf("legacy rule %s leaked into the compliance listing", r.ID)
		}
		if r.Citation == "" {
			t.Errorf("rule %s has no citation; the agent cannot explain it", r.ID)
		}
	}
}

// TestResourceScaffoldDryRunWritesNothing: the approval gate previews with a
// dry run, and a preview that mutates the working tree is not a preview.
func TestResourceScaffoldDryRunWritesNothing(t *testing.T) {
	root := templateCopy(t)
	session := connect(t, root)
	before := snapshot(t, root)

	out := structured[ScaffoldOutput](t, call(t, session, "resource_scaffold", map[string]any{
		"dry_run": true,
		"spec":    pensionArgs(),
	}))
	if out.Written {
		t.Error("a dry run reported that it wrote files")
	}
	if len(out.Files) != 7 {
		t.Errorf("got %d files, want 7", len(out.Files))
	}
	for _, f := range out.Files {
		if f.Content == "" {
			t.Errorf("%s has no content; a dry run has to return it or the caller cannot preview", f.Path)
		}
	}
	if len(out.Notes) == 0 {
		t.Error("the result should carry the follow-up steps (govalid, DDL)")
	}
	if diff := snapshotDiff(before, snapshot(t, root)); diff != "" {
		t.Errorf("the dry run modified the working tree: %s", diff)
	}
}

// TestResourceScaffoldWritesAndOmitsContent: a real run writes the files and
// returns the manifest only. Echoing seven files back into the model's context
// would be the largest tool result in the catalogue for no benefit — the files
// are on disk (Part A §6.2).
func TestResourceScaffoldWritesAndOmitsContent(t *testing.T) {
	root := templateCopy(t)
	session := connect(t, root)

	out := structured[ScaffoldOutput](t, call(t, session, "resource_scaffold", map[string]any{
		"spec": pensionArgs(),
	}))
	if !out.Written {
		t.Fatal("the scaffold reported that it wrote nothing")
	}
	for _, f := range out.Files {
		if f.Content != "" {
			t.Errorf("%s content was echoed back after being written", f.Path)
		}
		if f.Bytes == 0 {
			t.Errorf("%s reports zero bytes", f.Path)
		}
		if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(f.Path))); err != nil {
			t.Errorf("%s was reported but not written: %v", f.Path, err)
		}
	}
}

// TestInvalidSpecIsAToolErrorNotAProtocolError: the agent has to see the
// message and correct its next call. A protocol error would fail the session.
func TestInvalidSpecIsAToolErrorNotAProtocolError(t *testing.T) {
	root := templateCopy(t)
	session := connect(t, root)

	res := call(t, session, "resource_scaffold", map[string]any{
		"dry_run": true,
		"spec": map[string]any{
			"name":   "Pension",
			"fields": []any{map[string]any{"go": "Amount", "type": "decimal.Decimal"}},
		},
	})
	if !res.IsError {
		t.Fatal("an invalid spec should come back as a tool error")
	}
	text := resultText(res)
	if !strings.Contains(text, "decimal.Decimal") {
		t.Errorf("the error should name the offending type, got: %s", text)
	}
	if !strings.Contains(text, "float64") {
		t.Errorf("the error should name the substitute so one turn fixes it, got: %s", text)
	}
}

func TestFxWireRoundTrip(t *testing.T) {
	root := templateCopy(t)
	session := connect(t, root)

	out := structured[FxWireOutput](t, call(t, session, "fx_wire", map[string]any{
		"kind": "handler", "ctor": "NewPensionHandler", "dry_run": true,
	}))
	if !out.Changed || len(out.Added) != 1 {
		t.Fatalf("dry run: changed=%v added=%v", out.Changed, out.Added)
	}
	if out.Written {
		t.Error("a dry run reported that it wrote the file")
	}
	if !strings.Contains(out.Content, "fx.ResultTags(serverHandler.ServerControllersGroupTag)") {
		t.Error("the handler was not registered with its group tag")
	}

	out = structured[FxWireOutput](t, call(t, session, "fx_wire", map[string]any{
		"kind": "handler", "ctor": "NewPensionHandler",
	}))
	if !out.Written {
		t.Error("the real call did not write")
	}

	// And again: a retried tool call must not duplicate the provider.
	out = structured[FxWireOutput](t, call(t, session, "fx_wire", map[string]any{
		"kind": "handler", "ctor": "NewPensionHandler",
	}))
	if out.Changed || len(out.AlreadyRegistered) != 1 {
		t.Errorf("a repeat call changed=%v already=%v; it should be a no-op", out.Changed, out.AlreadyRegistered)
	}
}

// TestRootArgumentCannotEscapeTheWorkspace: the root argument is ultimately
// derived from model output, so a traversal must be refused rather than served
// (plan.md §17, workspace containment).
func TestRootArgumentCannotEscapeTheWorkspace(t *testing.T) {
	root := templateCopy(t)
	session := connect(t, root)

	for _, escape := range []string{"..", "../..", filepath.Join("..", "elsewhere"), string(filepath.Separator) + "etc"} {
		res := call(t, session, "rules_lint", map[string]any{"root": escape})
		if !res.IsError {
			t.Errorf("root %q was accepted; it must be refused", escape)
		}
	}
}

func TestProjectScaffoldRoundTrip(t *testing.T) {
	root := t.TempDir()
	session := connect(t, root)

	out := structured[ScaffoldOutput](t, call(t, session, "project_scaffold", map[string]any{
		"dry_run":  true,
		"project":  map[string]any{"module": "gitlab.cept.gov.in/it-2.0/pension-api"},
		"resource": pensionArgs(),
	}))
	if out.Written {
		t.Error("a dry run reported that it wrote files")
	}
	paths := map[string]bool{}
	for _, f := range out.Files {
		paths[f.Path] = true
	}
	for _, want := range []string{
		"go.mod", "main.go", "bootstrap/bootstrapper.go",
		"core/port/response.go", "configs/config.yaml", "configs/config.prod.yaml",
		"README.md", ".gitignore",
	} {
		if !paths[want] {
			t.Errorf("project scaffold produced no %s", want)
		}
	}
}

// ── helpers ─────────────────────────────────────────────────────────────────

func pensionArgs() map[string]any {
	return map[string]any{
		"name": "Pension",
		"fields": []any{
			map[string]any{"go": "PpoNumber", "type": "string", "validate": "required"},
			map[string]any{"go": "Amount", "type": "float64"},
			map[string]any{"go": "Status", "type": "string", "validate": "oneof=active closed"},
		},
		"list_filters": []any{map[string]any{"go": "Status"}},
	}
}

// templateCopy copies the reference template into a temp dir, skipping when the
// corpus is absent.
func templateCopy(t *testing.T) string {
	t.Helper()
	src, err := filepath.Abs(filepath.Join("..", "..", "..", "new-template"))
	if err != nil {
		t.Skipf("resolve corpus: %v", err)
	}
	if _, err := os.Stat(src); err != nil {
		t.Skip("new-template corpus not present; skipping")
	}
	dst := t.TempDir()
	err = filepath.WalkDir(src, func(p string, d fs.DirEntry, werr error) error {
		if werr != nil {
			return werr
		}
		rel, rerr := filepath.Rel(src, p)
		if rerr != nil {
			return rerr
		}
		if d.IsDir() {
			if d.Name() == ".git" || d.Name() == ".claude" {
				return fs.SkipDir
			}
			return os.MkdirAll(filepath.Join(dst, rel), 0o755)
		}
		b, rerr := os.ReadFile(p)
		if rerr != nil {
			return rerr
		}
		return os.WriteFile(filepath.Join(dst, rel), b, 0o644)
	})
	if err != nil {
		t.Fatalf("copy corpus: %v", err)
	}
	return dst
}

// snapshot records every file's size and modification time, so a test can
// prove a dry run left the tree alone.
func snapshot(t *testing.T, root string) map[string]string {
	t.Helper()
	out := map[string]string{}
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		info, ierr := d.Info()
		if ierr != nil {
			return ierr
		}
		rel, _ := filepath.Rel(root, p)
		out[filepath.ToSlash(rel)] = info.ModTime().Format(time.RFC3339Nano) + ":" + itoa(int(info.Size()))
		return nil
	})
	if err != nil {
		t.Fatalf("snapshot: %v", err)
	}
	return out
}

func snapshotDiff(before, after map[string]string) string {
	var problems []string
	for path, sig := range after {
		if b, ok := before[path]; !ok {
			problems = append(problems, "created "+path)
		} else if b != sig {
			problems = append(problems, "modified "+path)
		}
	}
	for path := range before {
		if _, ok := after[path]; !ok {
			problems = append(problems, "deleted "+path)
		}
	}
	return strings.Join(problems, ", ")
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}

func TestRepoMapRoundTrip(t *testing.T) {
	root := templateCopy(t)
	session := connect(t, root)

	out := structured[repomap.Map](t, call(t, session, "repo_map", map[string]any{}))
	if out.Module != "pisapi" {
		t.Errorf("module = %q, want pisapi", out.Module)
	}
	if out.Generation != "n-api" {
		t.Errorf("generation = %q, want n-api", out.Generation)
	}
	if len(out.Packages) == 0 {
		t.Fatal("no packages in the map")
	}
	if out.FX == nil || len(out.FX.Handlers) == 0 {
		t.Errorf("the composition root should be summarised: %+v", out.FX)
	}

	// The whole point of the tool: it must be cheap enough to send at turn one
	// and keep sending. The frontend agent's equivalent was 20-30k tokens.
	if out.EstTokens > repomap.DefaultMaxTokens {
		t.Errorf("est_tokens = %d, over the %d default budget", out.EstTokens, repomap.DefaultMaxTokens)
	}

	narrowed := structured[repomap.Map](t, call(t, session, "repo_map", map[string]any{
		"package": "repo/postgres",
	}))
	if len(narrowed.Packages) != 1 || narrowed.Packages[0].Dir != "repo/postgres" {
		t.Errorf("narrowing returned %d package(s)", len(narrowed.Packages))
	}
}
