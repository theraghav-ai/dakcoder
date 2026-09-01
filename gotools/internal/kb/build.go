package kb

import (
	"fmt"
	"path/filepath"
	"sort"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// File is one generated knowledge-base file.
type File struct {
	Path    string // relative to the knowledge-base root
	Content string
}

// RuleSummary is the minimum a generated reference needs about a rule.
//
// Passed in rather than imported, because internal/rules already imports this
// package's sibling for the timestamp constant and a cycle here would be a
// tangle for no gain.
type RuleSummary struct {
	ID       string
	Severity string
	Summary  string
	Citation string
	Legacy   bool
}

// BuildInput is everything the generator draws on.
type BuildInput struct {
	// DocsDir holds skill.md and SOP.md — the reference template.
	DocsDir string
	// Rules is the full rule set, compliance and legacy.
	Rules []RuleSummary
}

// Build assembles the knowledge base.
//
// Every byte is derived: from a document section, from the reference template's
// configuration, or from the rule set. The only hand-written text is the intros
// and corrections in References, and those live in Go beside the rules they
// describe rather than in a markdown file nobody re-reads.
func Build(in BuildInput) ([]File, error) {
	docs := map[string]*Doc{}
	load := func(name string) (*Doc, error) {
		if d, ok := docs[name]; ok {
			return d, nil
		}
		d, err := LoadDoc(filepath.Join(in.DocsDir, name))
		if err != nil {
			return nil, err
		}
		docs[name] = d
		return d, nil
	}

	var ws *workspace.Workspace
	out := make([]File, 0, len(References)+1)

	for _, ref := range References {
		var body string
		var err error

		switch ref.Generator {
		case "":
			if len(ref.Sources) == 0 && ref.Body != "" {
				body = strings.TrimSpace(ref.Body) + "\n"
				break
			}
			body, err = renderFromSources(ref, load)
		case "config-keys":
			if ws == nil {
				if ws, err = workspace.Load(in.DocsDir); err != nil {
					return nil, fmt.Errorf("load %s: %w", in.DocsDir, err)
				}
			}
			body = renderConfigKeys(ws)
		case "legacy-rules":
			body = renderRules(in.Rules, true)
		case "idiom-rules":
			body = renderIdiom(in.Rules)
		default:
			return nil, fmt.Errorf("reference %s names unknown generator %q", ref.Slug, ref.Generator)
		}
		if err != nil {
			return nil, fmt.Errorf("reference %s: %w", ref.Slug, err)
		}
		out = append(out, File{
			Path:    "references/" + ref.Slug + ".md",
			Content: renderReference(ref, body),
		})
	}

	out = append(out, File{Path: "SKILL.md", Content: renderIndex(in.Rules)})
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out, nil
}

// renderFromSources extracts the declared document sections verbatim.
func renderFromSources(ref Reference, load func(string) (*Doc, error)) (string, error) {
	var b strings.Builder
	for _, src := range ref.Sources {
		doc, err := load(src.Doc)
		if err != nil {
			return "", err
		}
		sec, ok := doc.Section(src.Section)
		if !ok {
			return "", fmt.Errorf("%s §%s does not resolve", src.Doc, src.Section)
		}
		fmt.Fprintf(&b, "## %s\n\n*From `%s` §%s (lines %d–%d).*\n\n",
			sec.Title, src.Doc, sec.Title, sec.Line, sec.EndLine)
		b.WriteString(strings.TrimSpace(demote(sec.Body)))
		b.WriteString("\n\n")
	}
	return b.String(), nil
}

// demote pushes extracted headings one level down so they nest under the
// reference's own `##` heading instead of competing with it.
//
// Fence-aware, because skill.md's shell examples are full of `# comment` lines
// and demoting those would corrupt the code.
func demote(body string) string {
	lines := strings.Split(body, "\n")
	inFence := false
	for i, line := range lines {
		if fenceRe.MatchString(line) {
			inFence = !inFence
			continue
		}
		if inFence {
			continue
		}
		if m := headingRe.FindStringSubmatch(line); m != nil && len(m[1]) < 6 {
			lines[i] = "#" + line
		}
	}
	return strings.Join(lines, "\n")
}

// renderReference wraps a body in the reference's front matter, intro and
// corrections.
func renderReference(ref Reference, body string) string {
	var b strings.Builder

	b.WriteString("---\n")
	fmt.Fprintf(&b, "slug: %s\n", ref.Slug)
	fmt.Fprintf(&b, "handle: \"@skill:%s\"\n", ref.Slug)
	fmt.Fprintf(&b, "fetch_when: %s\n", quoteYAML(ref.Purpose))
	if len(ref.Sources) > 0 {
		b.WriteString("sources:\n")
		for _, s := range ref.Sources {
			fmt.Fprintf(&b, "  - %s\n", quoteYAML(s.String()))
		}
	}
	if ref.Generator != "" {
		fmt.Fprintf(&b, "generated_from: %s\n", ref.Generator)
	}
	b.WriteString("---\n\n")

	fmt.Fprintf(&b, "# %s\n\n", ref.Title)
	b.WriteString(generatedNotice)
	b.WriteString("\n")

	if ref.Intro != "" {
		b.WriteString(ref.Intro)
		b.WriteString("\n\n")
	}
	if len(ref.Corrections) > 0 {
		// First, deliberately. A reader who meets the correction after the text
		// it corrects has already absorbed the wrong version.
		b.WriteString("## Corrections to the source\n\n")
		b.WriteString("The document below is reproduced as written. These parts of it are wrong:\n\n")
		for _, c := range ref.Corrections {
			fmt.Fprintf(&b, "- %s\n", c)
		}
		b.WriteString("\n")
	}
	b.WriteString(strings.TrimSpace(body))
	b.WriteString("\n")
	return b.String()
}

const generatedNotice = "> **Generated.** Do not edit — run `make knowledge` and commit the result.\n" +
	"> Assembled from the reference template, so it cannot drift from what the linter enforces.\n"

// renderIndex writes SKILL.md: the always-loaded contract, and the table that
// says what to fetch and when.
func renderIndex(rules []RuleSummary) string {
	var b strings.Builder

	b.WriteString("---\nname: n-api-template\nhandle: \"@skill\"\n---\n\n")
	b.WriteString("# The n-api-template contract\n\n")
	b.WriteString(generatedNotice)
	b.WriteString("\nThis file is always in context. Everything else is fetched on demand — " +
		"`skill.md` is 2,339 lines and 95% of it is irrelevant to any given turn.\n\n")

	b.WriteString("## The contract, in short\n\n")
	b.WriteString(contractSummary)

	b.WriteString("\n## What to fetch, and when\n\n")
	b.WriteString("| Handle | Fetch when |\n|---|---|\n")
	for _, ref := range References {
		fmt.Fprintf(&b, "| `@skill:%s` | %s |\n", ref.Slug, ref.Purpose)
	}

	// Deliberately not the rule table.
	//
	// This file is resident in every prompt, and §6.1 budgets the system prompt
	// at 1,200 tokens. Twenty-one rule rows cost roughly 475 of them to tell the
	// agent things it will be told again — with a fix and a citation attached —
	// the moment a rule actually fires. `list_rules` is one call away.
	compliance, legacy := 0, 0
	for _, r := range rules {
		if r.Legacy {
			legacy++
		} else {
			compliance++
		}
	}
	fmt.Fprintf(&b, "\n## Verification\n\n"+
		"`rules_lint` runs after every edit batch and checks %d rules. Every violation "+
		"carries a one-line fix and a citation, so it can be acted on without fetching "+
		"anything — call `list_rules` only if you need the whole set.\n\n"+
		"Pass `paths` with the files you changed. Findings elsewhere are reported but never "+
		"block, which is what stops a stray legacy violation turning into unrequested work.\n\n"+
		"%d legacy-pattern rules run only under `legacy_audit` — see "+
		"`@skill:legacy-patterns`. They never fire during ordinary edits: a pre-template "+
		"service trips roughly 1,700 compliance findings, which would bury the two you "+
		"actually caused.\n", compliance, legacy)
	return b.String()
}

// contractSummary is the invariant part — what is true of every service,
// stated in the shortest form that is still actionable.
const contractSummary = "```\n" +
	"core/domain/     plain Go models: json + db tags, ID, CreatedAt, UpdatedAt\n" +
	"core/port/       shared request and response envelopes\n" +
	"repo/postgres/   the ONLY place SQL, squirrel or pgx may appear\n" +
	"handler/         handlers, routes, and every request DTO in request.go\n" +
	"handler/response/  wire types and their New*Response converters\n" +
	"bootstrap/       the Uber-FX composition root\n" +
	"db/              DDL, applied by hand — never by the agent\n" +
	"configs/         one file per environment\n" +
	"```\n\n" +
	"- Handlers take `(sctx *serverRoute.Context, req T) (*resp.R, error)` — never a\n" +
	"  `*gin.Context`, never a manual `ShouldBind`. Input-less routes take `_ struct{}`.\n" +
	"- Handlers embed `*serverHandler.Base` and declare their own `Routes()`. Every route\n" +
	"  carries `.Name(...)` or it is missing from the generated OpenAPI document.\n" +
	"- Repositories build queries with `dblib.Psql`, take a deadline from\n" +
	"  `cfg.GetDuration(\"db.QueryTimeout…\")`, and map rows with\n" +
	"  `pgx.RowToStructByName[domain.X]`. A zero-row write returns `pgx.ErrNoRows`.\n" +
	"- Responses embed `port.StatusCodeAndMessage` with `json:\",inline\"` and take their\n" +
	"  status from the predefined `port.*Success` constants.\n" +
	"- Request structs live in `handler/request/request.go` (package `request`), because `govalid`\n" +
	"  reads only that file. Run it, or every non-GET route answers 422.\n" +
	"- Repositories go into `FxRepo` as plain providers; handlers go into `FxHandler`\n" +
	"  wrapped in `fx.Annotate` with `fx.As` and `fx.ResultTags`. Use `fx_wire`.\n" +
	"- Never add a credential to `configs/*.yaml`. Never echo one that is already there.\n"

// ── generated references ────────────────────────────────────────────────────

// renderConfigKeys tabulates every key across the environment files.
func renderConfigKeys(ws *workspace.Workspace) string {
	configs := ws.Configs()
	if len(configs) == 0 {
		return "*No `configs/` directory was found in the reference template.*\n"
	}

	envs := make([]string, 0, len(configs))
	byEnv := map[string]*workspace.ConfigFile{}
	for _, c := range configs {
		label := c.Env
		if label == "" {
			label = "base"
		}
		envs = append(envs, label)
		byEnv[label] = c
	}

	keys := map[string]string{} // lower path -> original path
	for _, c := range configs {
		for lower, k := range c.Keys {
			if k.Scalar {
				keys[lower] = k.Path
			}
		}
	}
	ordered := make([]string, 0, len(keys))
	for lower := range keys {
		ordered = append(ordered, lower)
	}
	sort.Strings(ordered)

	var b strings.Builder
	fmt.Fprintf(&b, "## Keys\n\n%d scalar keys across %d environment file(s). "+
		"A ✓ means the environment declares the key; a blank means a lookup there returns "+
		"the zero value.\n\n", len(ordered), len(configs))

	b.WriteString("| Key |")
	for _, e := range envs {
		fmt.Fprintf(&b, " %s |", e)
	}
	b.WriteString("\n|---|")
	for range envs {
		b.WriteString("---|")
	}
	b.WriteString("\n")

	for _, lower := range ordered {
		fmt.Fprintf(&b, "| `%s` |", keys[lower])
		for _, e := range envs {
			mark := " |"
			if byEnv[e].Has(lower) {
				mark = " ✓ |"
			}
			b.WriteString(mark)
		}
		b.WriteString("\n")
	}

	b.WriteString("\n## Gaps\n\n")
	var gaps []string
	for _, lower := range ordered {
		var absent []string
		for _, e := range envs {
			if !byEnv[e].Has(lower) {
				absent = append(absent, e)
			}
		}
		if len(absent) > 0 && len(absent) < len(envs) {
			gaps = append(gaps, fmt.Sprintf("- `%s` is missing from %s", keys[lower], strings.Join(absent, ", ")))
		}
	}
	if len(gaps) == 0 {
		b.WriteString("Every key is declared in every environment file.\n")
	} else {
		b.WriteString("Keys declared in some environments and not others. Reading one of these " +
			"in an environment that does not declare it returns the zero value, silently:\n\n")
		b.WriteString(strings.Join(gaps, "\n"))
		b.WriteString("\n")
	}
	return b.String()
}

// renderRules tabulates a rule set.
func renderRules(rules []RuleSummary, legacy bool) string {
	var b strings.Builder
	b.WriteString("| Rule | Blocks | Detects |\n|---|---|---|\n")
	n := 0
	for _, r := range rules {
		if r.Legacy != legacy {
			continue
		}
		n++
		blocks := ""
		if r.Severity == "error" {
			blocks = "yes"
		}
		fmt.Fprintf(&b, "| `%s` | %s | %s |\n", r.ID, blocks, r.Summary)
	}
	if n == 0 {
		return "*No rules in this set.*\n"
	}
	b.WriteString("\nRun `legacy_audit` against a service to see which of these it trips, with " +
		"a file and line for each.\n")
	return b.String()
}

// renderIdiom describes the one advisory rule and what it covers.
func renderIdiom(rules []RuleSummary) string {
	var b strings.Builder
	for _, r := range rules {
		if r.ID != "go-idiom" {
			continue
		}
		fmt.Fprintf(&b, "## `%s`\n\n%s\n\nSource: %s\n\n", r.ID, r.Summary, r.Citation)
	}
	b.WriteString("## What it checks\n\n")
	b.WriteString("- `any` rather than the written-out `interface{}` (Go 1.18+).\n")
	b.WriteString("- Error strings lower-case and unpunctuated — they get wrapped, and " +
		"`open config: Could not read file.` is what a capitalised, punctuated inner message " +
		"turns into.\n")
	b.WriteString("- `fmt.Errorf` wrapping an error with `%w` rather than `%v`. In this " +
		"template the consequence is concrete: `pgx.ErrNoRows` has to survive the trip up to " +
		"the framework to become a 404, and `%v` severs it.\n")
	b.WriteString("- One package per directory. **This one blocks** — it does not compile, " +
		"and the compiler's version of the message names a directory rather than the file " +
		"that introduced the mismatch.\n")
	b.WriteString("- Package names lower-case and single-word.\n\n")
	b.WriteString("## What it deliberately does not check\n\n")
	b.WriteString("Anything needing type information — unchecked errors, missing `defer` " +
		"close, nil interface versus nil pointer. Those are `golangci-lint`'s at the " +
		"verification gate, where there is time for them.\n")
	return b.String()
}

func quoteYAML(s string) string {
	return `"` + strings.ReplaceAll(s, `"`, `\"`) + `"`
}

// Equal reports whether two file sets have identical paths and content, which
// is what the freshness check compares.
func Equal(a, b []File) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i].Path != b[i].Path || normaliseEOL(a[i].Content) != normaliseEOL(b[i].Content) {
			return false
		}
	}
	return true
}

func normaliseEOL(s string) string {
	return strings.ReplaceAll(s, "\r\n", "\n")
}
