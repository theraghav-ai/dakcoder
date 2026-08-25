package kb

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// promptBudget is §6.1's system-prompt allocation. SKILL.md is resident in
// every prompt, so it competes for exactly that.
const promptBudget = 1200

func templateDir(t *testing.T) string {
	t.Helper()
	p, err := filepath.Abs(filepath.Join("..", "..", "..", "new-template"))
	if err != nil {
		t.Skipf("resolve corpus: %v", err)
	}
	if _, err := os.Stat(filepath.Join(p, "skill.md")); err != nil {
		t.Skip("new-template corpus not present; skipping")
	}
	return p
}

func ruleSummaries() []RuleSummary {
	return []RuleSummary{
		{ID: "repo-contract", Severity: "error", Summary: "repositories use dblib.Psql", Citation: "skill.md §Repository Pattern"},
		{ID: "go-idiom", Severity: "warning", Summary: "idiomatic Go", Citation: "go.instructions.md §Naming Conventions"},
		{ID: "legacy-gin-handler", Severity: "error", Summary: "gin.Context in a handler", Legacy: true},
		{ID: "legacy-routes-file", Severity: "error", Summary: "routes.go exists", Legacy: true},
	}
}

func buildKB(t *testing.T) []File {
	t.Helper()
	files, err := Build(BuildInput{DocsDir: templateDir(t), Rules: ruleSummaries()})
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	return files
}

func fileNamed(t *testing.T, files []File, path string) File {
	t.Helper()
	for _, f := range files {
		if f.Path == path {
			return f
		}
	}
	t.Fatalf("no %s in the knowledge base", path)
	return File{}
}

func TestEveryReferenceIsBuilt(t *testing.T) {
	files := buildKB(t)
	if len(files) != len(References)+1 {
		t.Fatalf("got %d files, want %d references plus SKILL.md", len(files), len(References))
	}
	for _, ref := range References {
		f := fileNamed(t, files, "references/"+ref.Slug+".md")
		if !strings.Contains(f.Content, "# "+ref.Title) {
			t.Errorf("%s has no title heading", ref.Slug)
		}
		if !strings.Contains(f.Content, "@skill:"+ref.Slug) {
			t.Errorf("%s does not carry its own handle", ref.Slug)
		}
		if !strings.Contains(f.Content, "**Generated.**") {
			t.Errorf("%s does not say it is generated; someone will edit it", ref.Slug)
		}
		if len(strings.TrimSpace(f.Content)) < 400 {
			t.Errorf("%s is %d bytes — suspiciously empty", ref.Slug, len(f.Content))
		}
	}
}

// TestSkillIndexStaysInsideThePromptBudget is the property the whole design
// exists for. skill.md is ~30k tokens and cannot live in a prompt; the index
// that replaces it has to actually be small, and "small" is a number.
func TestSkillIndexStaysInsideThePromptBudget(t *testing.T) {
	skill := fileNamed(t, buildKB(t), "SKILL.md")
	tokens := len(skill.Content) / 4
	if tokens > promptBudget {
		t.Errorf("SKILL.md is ~%d tokens, over the %d-token system-prompt budget (§6.1). "+
			"Something has been added that belongs in a reference file.", tokens, promptBudget)
	}
	t.Logf("SKILL.md: %d chars ≈ %d tokens", len(skill.Content), tokens)
}

// TestSkillIndexListsEveryReference: an index that omits a reference makes it
// unreachable, because the agent only fetches what the table names.
func TestSkillIndexListsEveryReference(t *testing.T) {
	skill := fileNamed(t, buildKB(t), "SKILL.md")
	for _, ref := range References {
		if !strings.Contains(skill.Content, "@skill:"+ref.Slug) {
			t.Errorf("SKILL.md does not list @skill:%s", ref.Slug)
		}
		if !strings.Contains(skill.Content, ref.Purpose) {
			t.Errorf("SKILL.md does not say when to fetch %s", ref.Slug)
		}
	}
}

// TestCorrectionsComeBeforeTheTextTheyCorrect: a reader who meets the
// correction after the wrong version has already absorbed the wrong version.
func TestCorrectionsComeBeforeTheTextTheyCorrect(t *testing.T) {
	files := buildKB(t)
	checked := 0
	for _, ref := range References {
		if len(ref.Corrections) == 0 {
			continue
		}
		checked++
		body := fileNamed(t, files, "references/"+ref.Slug+".md").Content
		corrections := strings.Index(body, "## Corrections to the source")
		if corrections < 0 {
			t.Errorf("%s declares corrections but does not render them", ref.Slug)
			continue
		}
		// The first extracted section heading comes from the source document.
		if len(ref.Sources) > 0 {
			extracted := strings.Index(body, "*From `")
			if extracted >= 0 && extracted < corrections {
				t.Errorf("%s renders the source before its corrections", ref.Slug)
			}
		}
		for _, c := range ref.Corrections {
			if !strings.Contains(body, c) {
				t.Errorf("%s is missing a declared correction", ref.Slug)
			}
		}
	}
	if checked == 0 {
		t.Fatal("no reference declares a correction; the known template defects are unrecorded")
	}
}

// TestKnownDefectsAreCarried names the specific things skill.md gets wrong.
//
// Handing the agent the document verbatim would hand it these: the spike showed
// the model follows its context closely, which is exactly why the context has
// to be right.
func TestKnownDefectsAreCarried(t *testing.T) {
	files := buildKB(t)
	want := map[string]string{
		"repository-pattern": "PlaceholderFormat",
		"request-dto":        "ToDomain",
		"response-dto":       "MetaDataResponse",
		"handler-pattern":    "layer-sql-boundary",
		"worked-example":     "do not compile",
	}
	for slug, marker := range want {
		body := fileNamed(t, files, "references/"+slug+".md").Content
		if !strings.Contains(body, marker) {
			t.Errorf("%s does not warn about %q", slug, marker)
		}
	}
}

// TestExtractedSectionsAreVerbatimAndAttributed: the value of extraction over
// paraphrase is that the reader gets the document, and can go and check.
func TestExtractedSectionsAreVerbatimAndAttributed(t *testing.T) {
	dir := templateDir(t)
	doc, err := LoadDoc(filepath.Join(dir, "skill.md"))
	if err != nil {
		t.Fatal(err)
	}
	sec, ok := doc.Section("Repository Pattern")
	if !ok {
		t.Skip("the corpus no longer has a Repository Pattern section")
	}

	body := fileNamed(t, buildKB(t), "references/repository-pattern.md").Content
	if !strings.Contains(body, "*From `skill.md` §Repository Pattern") {
		t.Error("the extract is not attributed to its source")
	}
	// A distinctive line from the middle of the section must survive intact.
	for _, line := range strings.Split(sec.Body, "\n") {
		line = strings.TrimSpace(line)
		if len(line) > 40 && !strings.HasPrefix(line, "#") && !strings.HasPrefix(line, "```") {
			if !strings.Contains(body, line) {
				t.Errorf("extracted text was altered; this line is missing:\n  %s", line)
			}
			break
		}
	}
}

// TestHeadingsAreDemotedButCodeIsNot: extracted headings nest under the
// reference's own heading, and shell comments inside fences are code.
func TestHeadingsAreDemotedButCodeIsNot(t *testing.T) {
	body := demote("## Section\n\nprose\n\n```bash\n# Initialize new module\ngo mod init x\n```\n\n### Sub\n")
	if !strings.Contains(body, "### Section") {
		t.Error("a heading was not demoted")
	}
	if !strings.Contains(body, "#### Sub") {
		t.Error("a nested heading was not demoted")
	}
	if !strings.Contains(body, "# Initialize new module") || strings.Contains(body, "## Initialize new module") {
		t.Error("a shell comment inside a fence was demoted as if it were a heading")
	}
}

// TestConfigKeysReferenceIsGeneratedFromTheRealConfigs: a hand-written key
// table is one that omits the key you are looking for.
func TestConfigKeysReferenceIsGeneratedFromTheRealConfigs(t *testing.T) {
	body := fileNamed(t, buildKB(t), "references/config-keys.md").Content
	for _, key := range []string{"db.QueryTimeoutLow", "db.QueryTimeoutMed", "server.addr"} {
		if !strings.Contains(body, "`"+key+"`") {
			t.Errorf("the key table is missing %s", key)
		}
	}
	if !strings.Contains(body, "## Gaps") {
		t.Error("the reference should call out keys missing from some environments")
	}
	// The gap that matters: swagger generation is declared in the base config
	// and in none of the environment files.
	if !strings.Contains(body, "swagger.generation.mode") {
		t.Error("the swagger.generation.mode gap is not surfaced")
	}
}

func TestGeneratedRuleReferencesFollowTheRuleSet(t *testing.T) {
	files := buildKB(t)

	legacy := fileNamed(t, files, "references/legacy-patterns.md").Content
	for _, id := range []string{"legacy-gin-handler", "legacy-routes-file"} {
		if !strings.Contains(legacy, "`"+id+"`") {
			t.Errorf("legacy-patterns does not list %s", id)
		}
	}
	if strings.Contains(legacy, "`repo-contract`") {
		t.Error("a compliance rule leaked into the legacy reference")
	}

	idiom := fileNamed(t, files, "references/go-idiom.md").Content
	if !strings.Contains(idiom, "`go-idiom`") {
		t.Error("go-idiom reference does not name its rule")
	}
	if !strings.Contains(idiom, "golangci-lint") {
		t.Error("go-idiom should say what it deliberately leaves to the gate")
	}
}

// TestBuildIsDeterministic: the knowledge base is committed and freshness
// checked, so an unstable build means a diff on every run.
func TestBuildIsDeterministic(t *testing.T) {
	first := buildKB(t)
	for range 3 {
		next, err := Build(BuildInput{DocsDir: templateDir(t), Rules: ruleSummaries()})
		if err != nil {
			t.Fatal(err)
		}
		if !Equal(first, next) {
			t.Fatal("the knowledge base is not deterministic; map iteration order is the usual cause")
		}
	}
}

// TestUnresolvableSourceFailsLoudly: a reference naming a section that was
// renamed must not silently produce an empty file.
func TestUnresolvableSourceFailsLoudly(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "skill.md"), []byte("# Doc\n\n## Present\n\nbody\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := renderFromSources(Reference{
		Slug:    "x",
		Sources: []Ref{{Doc: "skill.md", Section: "Absent"}},
	}, func(name string) (*Doc, error) { return LoadDoc(filepath.Join(dir, name)) })
	if err == nil {
		t.Fatal("an unresolvable source must be an error, not an empty section")
	}
	if !strings.Contains(err.Error(), "Absent") {
		t.Errorf("the error should name the section: %v", err)
	}
}

func TestReferenceBySlug(t *testing.T) {
	if _, ok := ReferenceBySlug("repository-pattern"); !ok {
		t.Error("repository-pattern should resolve")
	}
	if _, ok := ReferenceBySlug("no-such-reference"); ok {
		t.Error("an unknown slug should not resolve")
	}
}

// TestEveryReferenceDeclaresAPurpose: the fetch table is the only thing the
// agent sees before deciding to spend a call, so a blank entry is a reference
// nobody fetches.
func TestEveryReferenceDeclaresAPurpose(t *testing.T) {
	seen := map[string]bool{}
	for _, ref := range References {
		if ref.Slug == "" || ref.Title == "" || ref.Purpose == "" || ref.Intro == "" {
			t.Errorf("reference %q is missing a slug, title, purpose or intro", ref.Slug)
		}
		if seen[ref.Slug] {
			t.Errorf("duplicate slug %q", ref.Slug)
		}
		seen[ref.Slug] = true
		if len(ref.Sources) == 0 && ref.Generator == "" {
			t.Errorf("%s has neither sources nor a generator; it would be empty", ref.Slug)
		}
	}
}
