package kb

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func writeDoc(t *testing.T, dir, name, body string) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

// TestHeadingsInsideCodeFencesAreNotSections is the one that matters on the
// real corpus.
//
// skill.md is full of shell examples whose comments start with `#`:
// "# Initialize new module", "# Tidy up dependencies", and forty more. Counting
// those as headings puts phantom sections in the map, and a citation could then
// "resolve" to one of them — which is worse than not resolving at all, because
// nothing would report it.
func TestHeadingsInsideCodeFencesAreNotSections(t *testing.T) {
	dir := t.TempDir()
	p := writeDoc(t, dir, "skill.md", "# Real Heading\n\n"+
		"```bash\n# Initialize new module\ngo mod init x\n# Tidy up\ngo mod tidy\n```\n\n"+
		"## Another Real Heading\n\n"+
		"~~~go\n// not a heading\n~~~\n\n"+
		"### Third\n")

	doc, err := LoadDoc(p)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	want := []string{"Real Heading", "Another Real Heading", "Third"}
	if got := doc.Titles(); !slices.Equal(got, want) {
		t.Errorf("headings = %v, want %v", got, want)
	}
	if _, ok := doc.Section("Initialize new module"); ok {
		t.Error("a shell comment inside a fence was treated as a section")
	}
}

func TestSectionExtentStopsAtTheNextSiblingOrParent(t *testing.T) {
	dir := t.TempDir()
	p := writeDoc(t, dir, "skill.md", strings.Join([]string{
		"# Top",      // 1
		"top body",   // 2
		"## A",       // 3
		"a body",     // 4
		"### A.1",    // 5
		"a one body", // 6
		"## B",       // 7
		"b body",     // 8
	}, "\n")+"\n")

	doc, err := LoadDoc(p)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	a, ok := doc.Section("A")
	if !ok {
		t.Fatal("section A not found")
	}
	if a.Line != 3 || a.EndLine != 6 {
		t.Errorf("A spans %d-%d, want 3-6 (up to but not including ## B)", a.Line, a.EndLine)
	}
	if !strings.Contains(a.Body, "a one body") {
		t.Error("A's body should include its nested subsection")
	}
	if strings.Contains(a.Body, "b body") {
		t.Error("A's body leaked into sibling B")
	}
}

// TestHashIsLineEndingIndependent: the reference template is CRLF, so a pin
// made on Windows has to match a checkout on Linux or the drift check fires on
// every CI run and gets switched off within a week.
func TestHashIsLineEndingIndependent(t *testing.T) {
	dir := t.TempDir()
	lf := writeDoc(t, dir, "lf.md", "# A\n\nbody\n\n## B\n")
	crlf := writeDoc(t, dir, "crlf.md", "# A\r\n\r\nbody\r\n\r\n## B\r\n")

	a, err := LoadDoc(lf)
	if err != nil {
		t.Fatal(err)
	}
	b, err := LoadDoc(crlf)
	if err != nil {
		t.Fatal(err)
	}
	if a.Hash != b.Hash {
		t.Errorf("CRLF and LF copies hash differently:\n  %s\n  %s", a.Hash, b.Hash)
	}
	if !strings.HasPrefix(a.Hash, "sha256:") {
		t.Errorf("hash should name its algorithm: %s", a.Hash)
	}
}

func TestParseCitation(t *testing.T) {
	tests := []struct {
		in   string
		want []Ref
	}{
		{
			"skill.md §Repository Pattern",
			[]Ref{{"skill.md", "Repository Pattern"}},
		},
		{
			"skill.md §Domain Model Pattern, §Naming Conventions",
			[]Ref{{"skill.md", "Domain Model Pattern"}, {"skill.md", "Naming Conventions"}},
		},
		{
			"SOP.md §Validation; skill.md §Request DTO Pattern",
			[]Ref{{"SOP.md", "Validation"}, {"skill.md", "Request DTO Pattern"}},
		},
		{
			// A bare "§x" clause continues the previous clause's document.
			"SOP.md §Define the Routes; §main.go",
			[]Ref{{"SOP.md", "Define the Routes"}, {"SOP.md", "main.go"}},
		},
		{
			// A file with no § names the whole file, not a heading in it.
			"core/port/response.go; skill.md §Response DTO Pattern",
			[]Ref{{"core/port/response.go", ""}, {"skill.md", "Response DTO Pattern"}},
		},
		{
			"go.mod; plan.md §4 dependency allow-list",
			[]Ref{{"go.mod", ""}, {"plan.md", "4 dependency allow-list"}},
		},
		{
			"new-template layout",
			nil,
		},
	}
	for _, tc := range tests {
		got := ParseCitation(tc.in)
		if len(got) != len(tc.want) {
			t.Errorf("ParseCitation(%q) = %v, want %v", tc.in, got, tc.want)
			continue
		}
		for i := range got {
			if got[i] != tc.want[i] {
				t.Errorf("ParseCitation(%q)[%d] = %v, want %v", tc.in, i, got[i], tc.want[i])
			}
		}
	}
}

func TestSectionResolution(t *testing.T) {
	dir := t.TempDir()
	p := writeDoc(t, dir, "SOP.md", "# SOP Template\n\n### [handler].go\n\nbody\n\n"+
		"#### Define the Routes\n\nbody\n\n### Handler\n\nbody\n\n### Handler Pattern\n\nbody\n")
	doc, err := LoadDoc(p)
	if err != nil {
		t.Fatal(err)
	}

	tests := map[string]string{
		"[handler].go":                          "[handler].go",
		"[handler].go steps 2–3":                "[handler].go", // trailing locator
		"[handler].go (step 5, no gin.Context)": "[handler].go",
		"Define the Routes":                     "Define the Routes",
		"define the routes":                     "Define the Routes", // case folded
		// Longest match wins, so an abbreviation does not land on a shorter
		// heading that happens to share a prefix.
		"Handler Pattern": "Handler Pattern",
	}
	for want, expect := range tests {
		got, ok := doc.Section(want)
		if !ok {
			t.Errorf("%q did not resolve", want)
			continue
		}
		if got.Title != expect {
			t.Errorf("%q resolved to %q, want %q", want, got.Title, expect)
		}
	}

	if _, ok := doc.Section("Removing dependency on gin framework"); ok {
		t.Error("prose that is not a heading must not resolve")
	}
}

func TestCheckReportsUnresolvedCitationsWithASuggestion(t *testing.T) {
	dir := t.TempDir()
	writeDoc(t, dir, "skill.md", "# Doc\n\n## Repository Pattern\n\nbody\n")
	writeDoc(t, dir, "SOP.md", "# SOP\n\n## Validation\n\nbody\n")

	rep, err := Check(dir, []Citation{
		{RuleID: "good", Text: "skill.md §Repository Pattern"},
		{RuleID: "bad", Text: "skill.md §Repository Patterns That Do Not Exist"},
	}, nil)
	if err != nil {
		t.Fatalf("check: %v", err)
	}
	if rep.OK {
		t.Fatal("an unresolved citation must fail the check")
	}
	if len(rep.Problems) != 1 {
		t.Fatalf("got %d problems, want 1: %v", len(rep.Problems), rep.Problems)
	}
	p := rep.Problems[0]
	if p.Kind != "unresolved" || p.RuleID != "bad" {
		t.Errorf("problem = %+v", p)
	}
	// A complaint without a candidate costs the reader a search.
	if !strings.Contains(p.Fix, "Repository Pattern") {
		t.Errorf("the fix should suggest the nearest heading: %q", p.Fix)
	}
	if rep.Resolved != 1 {
		t.Errorf("resolved = %d, want 1", rep.Resolved)
	}
}

// TestDriftNamesTheCitedSectionsThatVanished is what makes the check
// actionable. "skill.md changed" is a fact; "these three headings the rules
// depend on are gone" is a work-list.
func TestDriftNamesTheCitedSectionsThatVanished(t *testing.T) {
	dir := t.TempDir()
	writeDoc(t, dir, "skill.md", "# Doc\n\n## Repository Pattern\n\nbody\n\n## Handler Pattern\n\nbody\n")

	citations := []Citation{
		{RuleID: "repo", Text: "skill.md §Repository Pattern"},
		{RuleID: "handler", Text: "skill.md §Handler Pattern"},
	}
	before, err := Check(dir, citations, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !before.OK {
		t.Fatalf("baseline should be clean: %v", before.Problems)
	}
	pinned := before.Manifest

	// The document is edited: one cited heading is renamed.
	writeDoc(t, dir, "skill.md", "# Doc\n\n## Repository Pattern\n\nbody\n\n## Handlers\n\nbody\n")

	after, err := Check(dir, citations, &pinned)
	if err != nil {
		t.Fatal(err)
	}
	if after.OK {
		t.Fatal("a document change must not pass silently")
	}
	var drift, unresolved int
	for _, p := range after.Problems {
		switch p.Kind {
		case "drift":
			drift++
			if !strings.Contains(p.Detail, "Handler Pattern") {
				t.Errorf("drift should name the heading that disappeared: %q", p.Detail)
			}
			if !strings.Contains(p.Fix, "doc-check --update") {
				t.Errorf("the fix should say how to re-pin: %q", p.Fix)
			}
		case "unresolved":
			unresolved++
		}
	}
	if drift != 1 || unresolved != 1 {
		t.Errorf("got %d drift and %d unresolved problems, want 1 and 1", drift, unresolved)
	}
}

func TestUnchangedDocumentDoesNotDrift(t *testing.T) {
	dir := t.TempDir()
	writeDoc(t, dir, "skill.md", "# Doc\n\n## Repository Pattern\n\nbody\n")
	citations := []Citation{{RuleID: "repo", Text: "skill.md §Repository Pattern"}}

	first, err := Check(dir, citations, nil)
	if err != nil {
		t.Fatal(err)
	}
	pinned := first.Manifest

	second, err := Check(dir, citations, &pinned)
	if err != nil {
		t.Fatal(err)
	}
	if !second.OK {
		t.Errorf("an unchanged document must not report drift: %v", second.Problems)
	}
}

func TestUnpinnedDocumentIsReported(t *testing.T) {
	dir := t.TempDir()
	writeDoc(t, dir, "skill.md", "# Doc\n\n## A\n\nbody\n")
	writeDoc(t, dir, "SOP.md", "# SOP\n\n## B\n\nbody\n")

	// A manifest that knows about skill.md but not SOP.md.
	partial := Manifest{Docs: map[string]DocPin{}}
	first, _ := Check(dir, []Citation{{RuleID: "a", Text: "skill.md §A"}}, nil)
	partial.Docs["skill.md"] = first.Manifest.Docs["skill.md"]

	rep, err := Check(dir, []Citation{
		{RuleID: "a", Text: "skill.md §A"},
		{RuleID: "b", Text: "SOP.md §B"},
	}, &partial)
	if err != nil {
		t.Fatal(err)
	}
	if rep.OK {
		t.Fatal("a cited but unpinned document must be reported")
	}
	found := false
	for _, p := range rep.Problems {
		if p.Kind == "unpinned" && p.Doc == "SOP.md" {
			found = true
		}
	}
	if !found {
		t.Errorf("problems = %v", rep.Problems)
	}
}

// TestMissingDocumentIsReportedOnce: an absent corpus is one problem, not one
// per rule that cites it.
func TestMissingDocumentIsReportedOnce(t *testing.T) {
	rep, err := Check(t.TempDir(), []Citation{
		{RuleID: "a", Text: "skill.md §A"},
		{RuleID: "b", Text: "skill.md §B"},
		{RuleID: "c", Text: "skill.md §C"},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	var missing int
	for _, p := range rep.Problems {
		if p.Kind == "missing-doc" {
			missing++
		}
	}
	if missing != 1 {
		t.Errorf("got %d missing-doc problems for three citations, want 1", missing)
	}
}

// TestNonPinnableCitationsAreLeftAlone: a citation naming go.mod or a config
// file is a pointer for a human, not a heading to resolve.
func TestNonPinnableCitationsAreLeftAlone(t *testing.T) {
	dir := t.TempDir()
	writeDoc(t, dir, "skill.md", "# Doc\n\n## A\n\nbody\n")

	rep, err := Check(dir, []Citation{
		{RuleID: "deps", Text: "go.mod; plan.md §4 dependency allow-list"},
		{RuleID: "health", Text: "configs/config.yaml §server.healthcheck"},
		{RuleID: "layout", Text: "new-template layout"},
		{RuleID: "real", Text: "skill.md §A"},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !rep.OK {
		t.Errorf("only skill.md and SOP.md are pinned; the rest are prose: %v", rep.Problems)
	}
	if rep.Resolved != 1 {
		t.Errorf("resolved = %d, want 1", rep.Resolved)
	}
}

// TestReferenceCorpusCitationsAllResolve is the assertion that keeps the rule
// set anchored to the documents it claims to enforce.
func TestReferenceCorpusCitationsAllResolve(t *testing.T) {
	root, err := filepath.Abs(filepath.Join("..", "..", "..", "new-template"))
	if err != nil {
		t.Skipf("resolve corpus: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, "skill.md")); err != nil {
		t.Skip("new-template corpus not present; skipping")
	}

	skill, err := LoadDoc(filepath.Join(root, "skill.md"))
	if err != nil {
		t.Fatalf("load skill.md: %v", err)
	}
	// A sanity floor: if the fence handling regresses, this jumps to ~120.
	if n := len(skill.Sections); n < 30 || n > 70 {
		t.Errorf("skill.md has %d headings; expected 30-70. Fence handling may have regressed.", n)
	}
	for _, want := range []string{"Repository Pattern", "Handler Pattern", "Response DTO Pattern", "Configuration Files"} {
		if _, ok := skill.Section(want); !ok {
			t.Errorf("skill.md §%s did not resolve", want)
		}
	}
}

// TestNearestPrefersTheRenamedHeading covers the case this whole check exists
// for: a cited heading gets reworded slightly, and the suggestion has to point
// at the new spelling rather than at whatever else shares a word with it.
func TestNearestPrefersTheRenamedHeading(t *testing.T) {
	dir := t.TempDir()
	writeDoc(t, dir, "skill.md", "# Doc\n\n## Repository Patterns\n\nbody\n\n## Domain Model Pattern\n\nbody\n")

	rep, err := Check(dir, []Citation{{RuleID: "repo", Text: "skill.md §Repository Pattern"}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.Problems) != 1 {
		t.Fatalf("got %d problems, want 1", len(rep.Problems))
	}
	if !strings.Contains(rep.Problems[0].Fix, "Repository Patterns") {
		t.Errorf("suggestion should be the renamed heading, got: %q", rep.Problems[0].Fix)
	}
}
