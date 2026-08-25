// Package repomap builds the orientation map the agent reads at turn one.
//
// # Why this is not `go list` or `packages.Load`
//
// Part A §8.1 sketches repo_map as `go list -json ./...` with a packages.Load
// path for symbol detail. Both were rejected for the same reason the rules
// engine rejects go/types: repo_map is most needed exactly when the build is
// broken. The Planner runs it before anything else, often on a service the
// developer has just described as not working, and a loader that needs the
// module graph to resolve returns nothing useful there — or spends two and a
// half minutes discovering it cannot.
//
// The workspace is already parsed. Reading the map out of it costs
// milliseconds, works on code that does not compile, and needs no toolchain.
//
// # Why the budget is the point
//
// The frontend agent's repo_map emitted indented JSON with a preview of every
// file, up to 200 files: 20–30k tokens, injected at turn one and then re-sent
// on every subsequent turn for the rest of the task (Part A §5, finding S4). On
// a 25-turn task that single tool result costs more than everything else
// combined.
//
// So the output has a hard token budget and degrades within it: every package
// stays listed — the agent needs to know a package exists far more than it
// needs its symbol list — and per-package detail is dropped in reverse order of
// how much the layer matters, with an explicit marker saying what was dropped
// and how to fetch it. Breadth is preserved; depth is negotiable.
package repomap

import (
	"encoding/json"
	"go/ast"
	"go/token"
	"path"
	"sort"
	"strings"
	"time"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/rules"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// DefaultMaxTokens is the cap from Part A §6.2.
const DefaultMaxTokens = 4000

// Options configures a map.
type Options struct {
	// Package narrows the map to one package directory, with full detail. This
	// is what the elision marker tells the agent to call.
	Package string
	// MaxTokens caps the estimated size. Zero uses DefaultMaxTokens; a negative
	// value disables the cap, which is for the CLI and for tests, never for the
	// agent.
	MaxTokens int
}

// Map is the repository as the agent sees it.
type Map struct {
	Module     string `json:"module,omitempty"`
	GoVersion  string `json:"go_version,omitempty"`
	Generation string `json:"generation,omitempty" jsonschema:"n-api for the current template generation, api for the legacy one"`

	Requires []string  `json:"requires,omitempty" jsonschema:"direct dependencies as path@version"`
	Packages []Package `json:"packages"`
	FX       *FX       `json:"fx,omitempty"`

	Files      int      `json:"files"`
	Elided     *Elision `json:"elided,omitempty"`
	EstTokens  int      `json:"est_tokens"`
	DurationMS int64    `json:"duration_ms"`
}

// Package is one directory of Go source.
type Package struct {
	Dir   string `json:"dir"`
	Name  string `json:"name"`
	Layer string `json:"layer,omitempty"`
	Files int    `json:"files"`

	Types []string `json:"types,omitempty"`
	Funcs []string `json:"funcs,omitempty" jsonschema:"exported functions; methods appear as (*Type).Method"`

	// MoreTypes and MoreFuncs count what a per-package cap removed, so a
	// truncated list never reads as a complete one.
	MoreTypes int `json:"more_types,omitempty"`
	MoreFuncs int `json:"more_funcs,omitempty"`

	// Summarised marks a package whose symbols were dropped to fit the budget.
	Summarised bool `json:"summarised,omitempty"`

	// allTypes and allFuncs hold the unabridged lists so the fitter can retry a
	// larger cap without re-walking the AST.
	allTypes, allFuncs []string
}

// FX is the composition root, summarised.
//
// Read from the same scan fx-registration reports from, so the map and the
// linter cannot disagree about what is wired.
type FX struct {
	Repos    []string `json:"repos,omitempty"`
	Handlers []string `json:"handlers,omitempty"`
	// Unwired constructors will fail at startup with an Uber-FX graph error.
	Unwired []string `json:"unwired,omitempty" jsonschema:"constructors declared but absent from bootstrap/; these fail at startup"`
	// Misregistered handlers start cleanly and serve nothing at all.
	Misregistered []string `json:"misregistered,omitempty" jsonschema:"handlers registered without their group tag; they start but serve no routes"`
}

// Elision records what the budget cost.
//
// Reported rather than applied silently: a truncated map that does not say it
// was truncated reads as "this repository has no other packages", and the agent
// plans against a repository that does not exist.
type Elision struct {
	Truncated  int    `json:"truncated,omitempty" jsonschema:"packages whose symbol lists were shortened; see more_types and more_funcs"`
	Summarised int    `json:"summarised,omitempty" jsonschema:"packages whose symbol lists were dropped"`
	Dropped    int    `json:"dropped,omitempty" jsonschema:"packages omitted entirely"`
	Hint       string `json:"hint"`
}

// generationModules distinguish the two library generations. api-config exists
// in both, so it is deliberately not in this list.
var generationModules = []string{"server", "bootstrapper", "db", "log", "validation"}

// Build renders the map from a loaded workspace.
//
// It does no I/O: everything comes from the single parse the workspace already
// did, which is the S3 guard — the frontend agent's version read every file
// twice, once for a preview and once to count its lines.
func Build(ws *workspace.Workspace, opts Options) *Map {
	start := time.Now()

	m := &Map{
		Module:     ws.ModulePath,
		GoVersion:  ws.GoVersion,
		Generation: detectGeneration(ws),
		Files:      len(ws.Files),
	}
	for _, r := range ws.Requires {
		m.Requires = append(m.Requires, r.Path+"@"+r.Version)
	}
	sort.Strings(m.Requires)

	m.Packages = buildPackages(ws, opts.Package)

	if opts.Package == "" {
		if fx := buildFX(ws); fx != nil {
			m.FX = fx
		}
	}

	budget := opts.MaxTokens
	if budget == 0 {
		budget = DefaultMaxTokens
	}
	if budget > 0 {
		fit(m, budget)
	}
	m.EstTokens = estimateTokens(m)
	m.DurationMS = time.Since(start).Milliseconds()
	return m
}

// buildPackages groups files by directory and extracts exported symbols.
func buildPackages(ws *workspace.Workspace, only string) []Package {
	only = strings.TrimSuffix(strings.TrimPrefix(strings.ReplaceAll(only, "\\", "/"), "./"), "/")

	byDir := map[string]*Package{}
	for _, f := range ws.Files {
		if f.Layer == workspace.LayerTest {
			continue
		}
		dir := path.Dir(f.Rel)
		if dir == "." {
			dir = ""
		}
		if only != "" && dir != only {
			continue
		}
		p := byDir[dir]
		if p == nil {
			p = &Package{Dir: dir, Name: f.Package, Layer: f.Layer.String()}
			byDir[dir] = p
		}
		p.Files++
		collectSymbols(f, p)
	}

	out := make([]Package, 0, len(byDir))
	for _, p := range byDir {
		sort.Strings(p.Types)
		sort.Strings(p.Funcs)
		// Keep the unabridged lists so the fitter can walk a cap down and back
		// up without re-walking the AST.
		p.allTypes, p.allFuncs = p.Types, p.Funcs
		out = append(out, *p)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Dir < out[j].Dir })
	return out
}

// collectSymbols appends a file's exported declarations to its package.
//
// Methods carry their receiver, because "UserHandler has a CreateUser method"
// and "there is a free function called CreateUser" are different facts and the
// agent navigates by them.
func collectSymbols(f *workspace.File, p *Package) {
	for _, d := range f.AST.Decls {
		switch t := d.(type) {
		case *ast.FuncDecl:
			if !t.Name.IsExported() {
				continue
			}
			if t.Recv == nil || len(t.Recv.List) == 0 {
				p.Funcs = append(p.Funcs, t.Name.Name)
				continue
			}
			p.Funcs = append(p.Funcs, "("+receiverName(t.Recv.List[0].Type)+")."+t.Name.Name)
		case *ast.GenDecl:
			if t.Tok != token.TYPE {
				continue // consts and vars are rarely what navigation needs
			}
			for _, spec := range t.Specs {
				ts, ok := spec.(*ast.TypeSpec)
				if ok && ts.Name.IsExported() {
					p.Types = append(p.Types, ts.Name.Name)
				}
			}
		}
	}
}

// receiverName renders a method receiver as it is written: *UserHandler.
func receiverName(e ast.Expr) string {
	switch t := e.(type) {
	case *ast.StarExpr:
		return "*" + receiverName(t.X)
	case *ast.Ident:
		return t.Name
	case *ast.IndexExpr: // generic receiver
		return receiverName(t.X)
	case *ast.IndexListExpr:
		return receiverName(t.X)
	}
	return "?"
}

// layerPriority orders packages by how much the agent needs their detail.
//
// handler and repo first because that is where nearly every task lands; domain
// next because it is what they exchange; everything else after. A package the
// agent is unlikely to edit still appears in the map — it just appears without
// its symbol list when the budget is tight.
var layerPriority = map[string]int{
	"handler":   0,
	"repo":      1,
	"domain":    2,
	"response":  3,
	"bootstrap": 4,
	"port":      5,
	"main":      6,
	"routes":    7,
	"other":     8,
	"test":      9,
}

func priorityOf(p Package) int {
	if v, ok := layerPriority[p.Layer]; ok {
		return v
	}
	return len(layerPriority)
}

// symbolCaps are the per-package symbol limits the fitter walks down.
//
// Uniform truncation comes before selective elision because that is what
// "breadth-first, adaptive depth" means in practice. Without it, the highest
// priority package keeps every one of its symbols while every other package
// loses its list entirely — which on the legacy corpus produced a 2,400-token
// dump of one handler package and nothing at all about the other nine. The
// agent needs to know a package exists far more than it needs the ninetieth
// method of the one package it is not editing.
var symbolCaps = []int{40, 24, 12, 6, 3, 1}

// worstCaseTail stands in for the fields Build fills after fitting: the elision
// marker and the token count itself.
//
// Without it the fitter measures a map that is missing its own marker, degrades
// until that fits, and then Build adds ~50 tokens of hint and pushes the result
// back over the budget. A cap that is exceeded by the note explaining the cap is
// a special kind of useless.
//
// The placeholder is the longest hint the code can produce, so the real one is
// always the same size or smaller.
var worstCaseTail = &Elision{
	Truncated: 999, Summarised: 999, Dropped: 999,
	Hint: "symbol lists shortened in 999 package(s), symbols dropped from 999 package(s), " +
		`999 package(s) omitted entirely to fit the context budget — ` +
		`call repo_map(package="<dir>") for any package you need in full`,
}

// fit brings the map inside its token budget.
func fit(m *Map, budget int) {
	// Measure as the caller will see it, tail included.
	m.Elided, m.EstTokens = worstCaseTail, 999999
	defer func() { m.EstTokens = 0 }()

	if estimateTokens(m) <= budget {
		m.Elided = nil
		return
	}

	elision := &Elision{}

	// 1. Shrink every package's symbol list uniformly.
	for _, cap := range symbolCaps {
		for i := range m.Packages {
			applyCap(&m.Packages[i], cap)
		}
		if estimateTokens(m) <= budget {
			m.Elided = finishElision(m, elision)
			return
		}
	}

	// 2. Least important first, and within a layer the largest first, so one
	// oversized package does not cost five small ones their detail.
	order := make([]int, len(m.Packages))
	for i := range order {
		order[i] = i
	}
	sort.SliceStable(order, func(a, b int) bool {
		pa, pb := m.Packages[order[a]], m.Packages[order[b]]
		if pa.Layer != pb.Layer {
			return priorityOf(pa) > priorityOf(pb)
		}
		return len(pa.Types)+len(pa.Funcs) > len(pb.Types)+len(pb.Funcs)
	})

	for _, i := range order {
		if estimateTokens(m) <= budget {
			break
		}
		p := &m.Packages[i]
		if len(p.allTypes) == 0 && len(p.allFuncs) == 0 {
			continue
		}
		p.Types, p.Funcs = nil, nil
		p.MoreTypes, p.MoreFuncs = 0, 0
		p.Summarised = true
		elision.Summarised++
	}

	// 3. Only if the bare package list still does not fit: drop packages, least
	// important first. This breaks the breadth-first promise, so it is a last
	// resort and it is counted — a truncated map that does not say it was
	// truncated reads as "there are no other packages".
	all := m.Packages
	dropped := map[int]bool{}
	for _, i := range order {
		if estimateTokens(m) <= budget {
			break
		}
		dropped[i] = true
		elision.Dropped++
		m.Packages = keeping(all, dropped)
	}

	m.Elided = finishElision(m, elision)
}

// finishElision counts what the budget cost and writes the marker.
//
// Returns nil when nothing was elided, so an untruncated map carries no marker
// at all — and, conversely, a truncated one always does. A cap the output does
// not mention reads as a complete answer, which is how an agent ends up
// planning against a repository that has more in it than it was shown.
func finishElision(m *Map, e *Elision) *Elision {
	for _, p := range m.Packages {
		if p.MoreTypes > 0 || p.MoreFuncs > 0 {
			e.Truncated++
		}
	}
	if e.Truncated == 0 && e.Summarised == 0 && e.Dropped == 0 {
		return nil
	}

	var parts []string
	if e.Truncated > 0 {
		parts = append(parts, "symbol lists shortened in "+itoa(e.Truncated)+" package(s)")
	}
	if e.Summarised > 0 {
		parts = append(parts, "symbols dropped from "+itoa(e.Summarised)+" package(s)")
	}
	if e.Dropped > 0 {
		parts = append(parts, itoa(e.Dropped)+" package(s) omitted entirely")
	}
	e.Hint = strings.Join(parts, ", ") +
		` to fit the context budget — call repo_map(package="<dir>") for any package you need in full`
	return e
}

// applyCap truncates a package's symbol lists, recording how many were removed.
func applyCap(p *Package, limit int) {
	p.Types, p.MoreTypes = truncate(p.allTypes, limit)
	p.Funcs, p.MoreFuncs = truncate(p.allFuncs, limit)
}

func truncate(all []string, limit int) (kept []string, dropped int) {
	if len(all) <= limit {
		return all, 0
	}
	return all[:limit], len(all) - limit
}

// keeping returns the packages not in the dropped set, in the original order.
func keeping(all []Package, dropped map[int]bool) []Package {
	if len(dropped) == 0 {
		return all
	}
	out := make([]Package, 0, len(all)-len(dropped))
	for i, p := range all {
		if !dropped[i] {
			out = append(out, p)
		}
	}
	return out
}

// buildFX summarises the composition root.
func buildFX(ws *workspace.Workspace) *FX {
	scan := rules.ScanFX(ws)
	if !scan.Present {
		return nil
	}
	fx := &FX{
		Repos:         scan.Names(rules.FXRepo, nil),
		Handlers:      scan.Names(rules.FXHandler, nil),
		Unwired:       scan.Unwired(),
		Misregistered: scan.Misregistered(),
	}
	if len(fx.Repos) == 0 && len(fx.Handlers) == 0 {
		return nil
	}
	return fx
}

// detectGeneration reports which IT 2.0 library generation the code uses, which
// tells the Planner immediately whether this is a migration target.
//
// Read from imports rather than from go.mod's require block, because the two
// disagree and only one of them is what the compiler sees. The reference
// template's go.mod lists `api-db v1.0.32` as a direct require while every
// repository imports `n-api-db` — an untidied go.mod, and reading it would have
// labelled the reference template itself as legacy.
//
// api-config is deliberately not a signal: it is shared by both generations.
func detectGeneration(ws *workspace.Workspace) string {
	const prefix = "gitlab.cept.gov.in/it-2.0-common/"
	legacy, current := false, false
	for _, f := range ws.Files {
		for _, im := range f.AST.Imports {
			p := strings.Trim(im.Path.Value, `"`)
			if !strings.HasPrefix(p, prefix) {
				continue
			}
			// Trim any sub-package: n-api-server/handler is still n-api-server.
			name := strings.TrimPrefix(p, prefix)
			if i := strings.IndexByte(name, '/'); i >= 0 {
				name = name[:i]
			}
			for _, mod := range generationModules {
				switch name {
				case "n-api-" + mod:
					current = true
				case "api-" + mod:
					legacy = true
				}
			}
		}
	}
	switch {
	case legacy && current:
		// A partially migrated service. Worth saying so rather than picking
		// one: it is the state a migration run leaves between commits, and the
		// Planner needs to know which half it is looking at.
		return "mixed"
	case legacy:
		return "api"
	case current:
		return "n-api"
	default:
		return ""
	}
}

// estimateTokens approximates the serialised size in tokens.
//
// Four characters per token is the usual rule of thumb and it is measured
// against the compact encoding, because that is what the caller sends. Indented
// JSON is roughly a third larger for no information — which is precisely how
// the frontend agent's repo_map reached 30k tokens.
func estimateTokens(m *Map) int {
	b, err := json.Marshal(m)
	if err != nil {
		return 0
	}
	return len(b) / 4
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	if neg {
		return "-" + string(b)
	}
	return string(b)
}
