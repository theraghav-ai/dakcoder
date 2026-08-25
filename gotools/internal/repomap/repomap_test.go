package repomap

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

func load(t *testing.T, root string) *workspace.Workspace {
	t.Helper()
	ws, err := workspace.Load(root)
	if err != nil {
		t.Fatalf("load %s: %v", root, err)
	}
	return ws
}

func mkws(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	if _, ok := files["go.mod"]; !ok {
		files["go.mod"] = "module pisapi\n\ngo 1.25.0\n"
	}
	for rel, body := range files {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func corpus(t *testing.T, rel string) string {
	t.Helper()
	p, err := filepath.Abs(filepath.Join("..", "..", "..", rel))
	if err != nil {
		t.Skipf("resolve %s: %v", rel, err)
	}
	if _, err := os.Stat(p); err != nil {
		t.Skipf("corpus %s not present; skipping", rel)
	}
	return p
}

func pkg(m *Map, dir string) *Package {
	for i := range m.Packages {
		if m.Packages[i].Dir == dir {
			return &m.Packages[i]
		}
	}
	return nil
}

func TestMapsTheReferenceTemplate(t *testing.T) {
	m := Build(load(t, corpus(t, "new-template")), Options{})

	if m.Module != "pisapi" {
		t.Errorf("module = %q, want pisapi", m.Module)
	}
	if m.GoVersion == "" {
		t.Error("go version was not read")
	}
	for _, dir := range []string{"", "bootstrap", "core/domain", "core/port", "handler", "handler/response", "repo/postgres"} {
		if pkg(m, dir) == nil {
			t.Errorf("package %q is missing from the map", dir)
		}
	}

	handler := pkg(m, "handler")
	if !slices.Contains(handler.Types, "UserHandler") {
		t.Errorf("handler types = %v", handler.Types)
	}
	if !slices.Contains(handler.Funcs, "(*UserHandler).CreateUser") {
		t.Errorf("methods should carry their receiver: %v", handler.Funcs)
	}
	if !slices.Contains(handler.Funcs, "NewUserHandler") {
		t.Errorf("plain functions should appear unqualified: %v", handler.Funcs)
	}
	// Unexported declarations are navigation noise the agent cannot import.
	repo := pkg(m, "repo/postgres")
	for _, f := range repo.Funcs {
		if strings.HasPrefix(f, "new") || strings.Contains(f, ".get") {
			t.Errorf("unexported symbol leaked into the map: %s", f)
		}
	}
}

// TestGenerationComesFromImportsNotGoMod is a regression.
//
// The reference template's go.mod lists `api-db v1.0.32` as a direct require
// while every repository imports `n-api-db` — an untidied go.mod. Reading the
// require block labelled the reference template itself as legacy, which is the
// one answer that would send a migration run at a service that needs none.
func TestGenerationComesFromImportsNotGoMod(t *testing.T) {
	if got := Build(load(t, corpus(t, "new-template")), Options{}).Generation; got != "n-api" {
		t.Errorf("reference template generation = %q, want n-api", got)
	}
	if got := Build(load(t, corpus(t, "pao-back-end-development")), Options{}).Generation; got != "api" {
		t.Errorf("legacy corpus generation = %q, want api", got)
	}

	mixed := mkws(t, map[string]string{
		"handler/a.go":       "package handler\n\nimport _ \"gitlab.cept.gov.in/it-2.0-common/n-api-server/handler\"\n",
		"repo/postgres/b.go": "package repo\n\nimport _ \"gitlab.cept.gov.in/it-2.0-common/api-db\"\n",
	})
	if got := Build(load(t, mixed), Options{}).Generation; got != "mixed" {
		t.Errorf("a half-migrated service should report mixed, got %q", got)
	}

	// api-config is shared by both generations and must not be a signal.
	neither := mkws(t, map[string]string{
		"handler/a.go": "package handler\n\nimport _ \"gitlab.cept.gov.in/it-2.0-common/api-config\"\n",
	})
	if got := Build(load(t, neither), Options{}).Generation; got != "" {
		t.Errorf("api-config alone should not decide a generation, got %q", got)
	}
}

// TestFXMatchesTheLinter is the property that keeps the Planner and the
// Verifier from disagreeing. Both read the same scan.
func TestFXMatchesTheLinter(t *testing.T) {
	root := mkws(t, map[string]string{
		"handler/user.go": `package handler

type UserHandler struct{}

func NewUserHandler() *UserHandler { return nil }
`,
		"handler/pension.go": `package handler

type PensionHandler struct{}

func NewPensionHandler() *PensionHandler { return nil }
`,
		"repo/postgres/user.go": `package repo

type UserRepository struct{}

func NewUserRepository() *UserRepository { return nil }
`,
		"bootstrap/bootstrapper.go": `package bootstrap

import (
	handler "pisapi/handler"
	repo "pisapi/repo/postgres"

	serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
	"go.uber.org/fx"
)

var FxRepo = fx.Module("Repomodule", fx.Provide(repo.NewUserRepository))

var FxHandler = fx.Module(
	"Handlermodule",
	fx.Provide(
		fx.Annotate(
			handler.NewUserHandler,
			fx.As(new(serverHandler.Handler)),
			fx.ResultTags(serverHandler.ServerControllersGroupTag),
		),
	),
)
`,
	})
	m := Build(load(t, root), Options{})
	if m.FX == nil {
		t.Fatal("no FX section")
	}
	if !slices.Contains(m.FX.Unwired, "NewPensionHandler") {
		t.Errorf("an unregistered handler must be surfaced: %+v", m.FX)
	}
	if slices.Contains(m.FX.Unwired, "NewUserHandler") || slices.Contains(m.FX.Unwired, "NewUserRepository") {
		t.Errorf("correctly wired constructors reported as unwired: %+v", m.FX)
	}
}

// TestMisregisteredHandlersAreSurfaced: a handler on a bare fx.Provide compiles,
// starts, and serves nothing. Nothing reports it at runtime, so the map has to.
func TestMisregisteredHandlersAreSurfaced(t *testing.T) {
	root := mkws(t, map[string]string{
		"handler/user.go": "package handler\n\ntype UserHandler struct{}\n\nfunc NewUserHandler() *UserHandler { return nil }\n",
		"bootstrap/bootstrapper.go": `package bootstrap

import (
	handler "pisapi/handler"

	"go.uber.org/fx"
)

var FxHandler = fx.Module("Handlermodule", fx.Provide(handler.NewUserHandler))
`,
	})
	m := Build(load(t, root), Options{})
	if m.FX == nil || !slices.Contains(m.FX.Misregistered, "NewUserHandler") {
		t.Errorf("a plain-registered handler must be surfaced: %+v", m.FX)
	}
	if slices.Contains(m.FX.Unwired, "NewUserHandler") {
		t.Error("it is registered — just wrongly; calling it unwired sends the agent at the wrong fix")
	}
}

// TestBudgetIsHonouredAndBreadthSurvives is finding S4 as a regression.
//
// The frontend agent's repo_map cost 20–30k tokens, injected at turn one and
// re-sent every turn after. Whatever else the budget costs, every package has
// to stay listed: the agent needs to know a package exists far more than it
// needs the ninetieth method of one it is not editing.
func TestBudgetIsHonouredAndBreadthSurvives(t *testing.T) {
	root := mkws(t, syntheticRepo(30, 60))
	full := Build(load(t, root), Options{MaxTokens: -1})

	// 30 packages need roughly 25 tokens each just to be named, so these are
	// the budgets at which breadth is still achievable. Below that, dropping
	// packages is the honest answer — TestImpossibleBudgetDropsAndSaysSo covers
	// it.
	for _, budget := range []int{4000, 2000, 1200} {
		t.Run(fmt.Sprintf("budget-%d", budget), func(t *testing.T) {
			m := Build(load(t, root), Options{MaxTokens: budget})
			if m.EstTokens > budget {
				t.Errorf("est_tokens = %d, over the %d budget", m.EstTokens, budget)
			}
			if len(m.Packages) != len(full.Packages) {
				t.Errorf("%d of %d packages survived; breadth should be the last thing given up",
					len(m.Packages), len(full.Packages))
			}
			if m.Elided == nil {
				t.Error("the map was reduced but carries no marker; a silent cap reads as a complete answer")
			} else if m.Elided.Hint == "" {
				t.Error("the elision has no hint telling the agent how to get the detail")
			}
		})
	}
}

// TestImpossibleBudgetDropsAndSaysSo: below the size of the bare package list,
// something has to give. Dropping packages is the last resort, and the one case
// where breadth is broken — so it has to be counted and reported rather than
// applied quietly.
func TestImpossibleBudgetDropsAndSaysSo(t *testing.T) {
	root := mkws(t, syntheticRepo(30, 60))
	full := Build(load(t, root), Options{MaxTokens: -1})

	m := Build(load(t, root), Options{MaxTokens: 300})
	if m.EstTokens > 300 {
		t.Errorf("est_tokens = %d, over the 300 budget", m.EstTokens)
	}
	if len(m.Packages) >= len(full.Packages) {
		t.Fatal("a 300-token budget cannot hold 30 packages; expected some to be dropped")
	}
	if m.Elided == nil || m.Elided.Dropped == 0 {
		t.Fatalf("packages were dropped without being counted: %+v", m.Elided)
	}
	if got := len(full.Packages) - len(m.Packages); got != m.Elided.Dropped {
		t.Errorf("elided.dropped = %d but %d packages are missing", m.Elided.Dropped, got)
	}
	if !strings.Contains(m.Elided.Hint, "omitted entirely") {
		t.Errorf("the hint must say packages were omitted: %q", m.Elided.Hint)
	}
}

// TestTruncatedListsCarryTheirCount: a shortened list that does not say it was
// shortened is worse than no list, because the agent concludes the symbol it is
// looking for does not exist.
func TestTruncatedListsCarryTheirCount(t *testing.T) {
	root := mkws(t, syntheticRepo(4, 200))
	m := Build(load(t, root), Options{MaxTokens: 1500})

	var truncated int
	for _, p := range m.Packages {
		if p.MoreTypes > 0 || p.MoreFuncs > 0 {
			truncated++
			if len(p.Types)+len(p.Funcs) == 0 {
				t.Errorf("%s reports more_* but shows nothing", p.Dir)
			}
		}
		if p.Summarised && (p.MoreTypes > 0 || p.MoreFuncs > 0) {
			t.Errorf("%s is both summarised and truncated; the counts are stale", p.Dir)
		}
	}
	if truncated == 0 {
		t.Fatal("nothing was truncated at a 1500-token budget over 800 symbols")
	}
	if m.Elided == nil || m.Elided.Truncated != truncated {
		t.Errorf("elided.truncated = %v, want %d", m.Elided, truncated)
	}
}

// TestPackageNarrowingReturnsFullDetail: this is what the elision hint tells the
// agent to call, so it has to actually return what was elided.
func TestPackageNarrowingReturnsFullDetail(t *testing.T) {
	root := mkws(t, syntheticRepo(20, 80))
	capped := Build(load(t, root), Options{MaxTokens: 2000})
	target := capped.Packages[0].Dir

	narrowed := Build(load(t, root), Options{Package: target, MaxTokens: -1})
	if len(narrowed.Packages) != 1 {
		t.Fatalf("narrowing returned %d packages, want 1", len(narrowed.Packages))
	}
	if narrowed.Packages[0].Dir != target {
		t.Fatalf("narrowed to %q, want %q", narrowed.Packages[0].Dir, target)
	}
	if narrowed.Packages[0].MoreTypes != 0 || narrowed.Packages[0].MoreFuncs != 0 {
		t.Error("an uncapped narrowed map should carry the complete symbol list")
	}
	if len(narrowed.Packages[0].Types) <= len(pkg(capped, target).Types) {
		t.Error("narrowing did not recover any elided detail")
	}
}

func TestMapIsDeterministic(t *testing.T) {
	ws := load(t, corpus(t, "new-template"))
	first, err := json.Marshal(Build(ws, Options{}))
	if err != nil {
		t.Fatal(err)
	}
	for range 5 {
		next, err := json.Marshal(Build(ws, Options{}))
		if err != nil {
			t.Fatal(err)
		}
		// duration_ms legitimately varies; everything else must not.
		if stripDuration(string(next)) != stripDuration(string(first)) {
			t.Fatal("the map is not deterministic; map iteration order is the usual cause")
		}
	}
}

// TestMapsCodeThatDoesNotCompile is the reason this is not built on go/packages.
//
// The Planner runs repo_map first, often on a service the developer has just
// described as broken. A loader that needs the module graph returns nothing
// useful there.
func TestMapsCodeThatDoesNotCompile(t *testing.T) {
	root := mkws(t, map[string]string{
		"handler/good.go": "package handler\n\ntype GoodHandler struct{}\n\nfunc NewGoodHandler() *GoodHandler { return nil }\n",
		"handler/broken.go": `package handler

type BrokenHandler struct{}

func (h *BrokenHandler) Serve() error {
	return undefinedFunction(missingArgument
}
`,
	})
	m := Build(load(t, root), Options{})
	h := pkg(m, "handler")
	if h == nil {
		t.Fatal("the handler package vanished because one file does not parse")
	}
	if !slices.Contains(h.Types, "GoodHandler") {
		t.Errorf("the parseable file's symbols were lost: %v", h.Types)
	}
}

// ── the S2/S3 performance guards (plan §20.5) ───────────────────────────────

// TestPrunedTreeIsFastAndReadsEachFileOnce is findings S2 and S3 as a CI gate.
//
// S2: the frontend agent's walk yielded and stat'd every path before excluding
// it — measured at 1.6s to keep 200 files out of 16,680, and worse on Windows
// where every stat goes through the antivirus filter driver. A real vendor/ is
// 100k files.
//
// S3: its repo_map then read every kept file twice, once for a 120-character
// preview and once to count newlines.
//
// The vendor tree here is smaller than the 100,000 files §20.5 names, because
// creating those on Windows costs minutes on every run and a slow test is a
// skipped test. It is large enough that a non-pruning walk cannot pass: at the
// measured rate an unpruned 20,000-file tree is ~2s on its own.
func TestPrunedTreeIsFastAndReadsEachFileOnce(t *testing.T) {
	if testing.Short() {
		t.Skip("-short: skipping the tree-walk benchmark")
	}
	const (
		realFiles   = 200
		vendorFiles = 20000
		limit       = 2 * time.Second
	)

	root := mkws(t, syntheticRepo(20, 10))
	writeJunk(t, filepath.Join(root, "vendor"), vendorFiles)
	writeJunk(t, filepath.Join(root, "node_modules"), 1000)

	start := time.Now()
	ws, err := workspace.Load(root)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	m := Build(ws, Options{})
	elapsed := time.Since(start)

	if elapsed > limit {
		t.Errorf("load + map took %v over %d vendored files, above the %v budget — "+
			"the walk is not pruning", elapsed, vendorFiles, limit)
	}
	if m.Files > realFiles {
		t.Errorf("the map contains %d files; vendored code leaked in", m.Files)
	}

	// S3: exactly one read per file considered, and building the map adds none.
	readsAfterLoad := ws.ReadCount()
	_ = Build(ws, Options{})
	if ws.ReadCount() != readsAfterLoad {
		t.Errorf("building the map performed %d extra file reads; it must consume the parse, not repeat it",
			ws.ReadCount()-readsAfterLoad)
	}
	if readsAfterLoad > m.Files+len(ws.Configs())+8 {
		t.Errorf("read %d files to map %d; each file should be read exactly once",
			readsAfterLoad, m.Files)
	}
	t.Logf("%d vendored files pruned; %d real files mapped in %v (%d reads, ~%d tokens)",
		vendorFiles, m.Files, elapsed, readsAfterLoad, m.EstTokens)
}

// TestRealCorpusIsFastEnoughForTurnOne holds the §5.3 latency target. The
// frontend agent's equivalent took 15–40s on a vendored repository.
func TestRealCorpusIsFastEnoughForTurnOne(t *testing.T) {
	root := corpus(t, "pao-back-end-development")
	const limit = 1500 * time.Millisecond

	start := time.Now()
	m := Build(load(t, root), Options{})
	elapsed := time.Since(start)

	if elapsed > limit {
		t.Errorf("repo_map on the legacy corpus took %v, over the %v target", elapsed, limit)
	}
	if m.EstTokens > DefaultMaxTokens {
		t.Errorf("est_tokens = %d, over the default budget", m.EstTokens)
	}
	t.Logf("%d files mapped in %v, ~%d tokens", m.Files, elapsed, m.EstTokens)
}

// ── fixtures ────────────────────────────────────────────────────────────────

// syntheticRepo builds a repository with the given number of packages, each
// declaring symbols types and the same number of methods.
func syntheticRepo(packages, symbols int) map[string]string {
	files := map[string]string{}
	layers := []string{"handler", "repo/postgres", "core/domain", "handler/response", "internal/misc"}
	for i := range packages {
		dir := fmt.Sprintf("%s/pkg%02d", layers[i%len(layers)], i)
		name := fmt.Sprintf("pkg%02d", i)
		var b strings.Builder
		fmt.Fprintf(&b, "package %s\n\n", name)
		for j := range symbols {
			fmt.Fprintf(&b, "type Type%03d struct{}\n\n", j)
			fmt.Fprintf(&b, "func (t *Type%03d) Method%03d() {}\n\n", j, j)
		}
		files[dir+"/gen.go"] = b.String()
	}
	return files
}

// writeJunk fills a directory with files the walk must never descend into.
func writeJunk(t *testing.T, dir string, n int) {
	t.Helper()
	// Nested, because a flat directory is the easy case and real vendor trees
	// are deep.
	const perDir = 200
	body := []byte("package junk\n\nfunc Junk() {}\n")
	for i := range n {
		sub := filepath.Join(dir, fmt.Sprintf("mod%03d", i/perDir), fmt.Sprintf("sub%02d", (i/20)%10))
		if i%perDir == 0 {
			if err := os.MkdirAll(sub, 0o755); err != nil {
				t.Fatal(err)
			}
		}
		if err := os.MkdirAll(sub, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(sub, fmt.Sprintf("f%05d.go", i)), body, 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func stripDuration(s string) string {
	for {
		i := strings.Index(s, `"duration_ms":`)
		if i < 0 {
			return s
		}
		j := i + len(`"duration_ms":`)
		for j < len(s) && s[j] != ',' && s[j] != '}' {
			j++
		}
		s = s[:i] + s[j:]
	}
}
