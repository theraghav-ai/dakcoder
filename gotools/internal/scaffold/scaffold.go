// Package scaffold turns a validated spec into files.
//
// # The division of labour
//
// The LLM chooses the spec; text/template writes the code. That is the whole
// design, and everything good about this package follows from it: output is
// deterministic, so it can be pinned byte-for-byte against a golden snapshot
// (§20.2); it cannot invent a dependency, because the templates name every
// import; and template drift is caught by a failing snapshot test in CI rather
// than by a reviewer noticing that last week's resource looks different from
// this week's.
//
// # Every generated file goes through gofmt before it is returned
//
// Not as a formatting nicety — as a syntax check. format.Source fails on
// unparseable Go, so a template with a missing brace fails here, with a line
// number, instead of reaching a developer as an error in a file they did not
// write and cannot easily attribute.
//
// # Nothing is written without being asked
//
// Scaffolding returns a Result describing every file and its action. Writing is
// a separate call. The agent's approval gate needs the contents before the
// write (Part B §9 presents a scaffold as one reviewable changeset), and a tool
// that returns what it would do is also a tool that can be dry-run.
package scaffold

import (
	"bytes"
	"embed"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"text/template"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/gopatch"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/spec"
)

//go:embed templates
var templateFS embed.FS

// Action is what will happen to a file.
type Action string

const (
	// ActionCreate writes a new file. It refuses to overwrite.
	ActionCreate Action = "create"
	// ActionModify rewrites an existing file that the scaffolder edited in
	// place — request.go and bootstrapper.go.
	ActionModify Action = "modify"
)

// File is one file the scaffolder produces.
type File struct {
	Path    string `json:"path" jsonschema:"workspace-relative path"`
	Action  Action `json:"action" jsonschema:"create or modify"`
	Content string `json:"content" jsonschema:"the complete file content"`
	Bytes   int    `json:"bytes"`
}

// Result is a scaffold ready to be written.
type Result struct {
	Files []File `json:"files"`
	// Notes carry decisions and follow-up actions the developer has to know
	// about — the DDL that still needs applying, the validators that still need
	// generating. A scaffolder that silently leaves required steps undone is
	// worse than one that does less.
	Notes []string `json:"notes,omitempty"`
	// Module is the Go module path the imports were written against.
	Module string `json:"module,omitempty"`
}

// Paths lists the files in the result, for a compact summary.
func (r *Result) Paths() []string {
	out := make([]string, 0, len(r.Files))
	for _, f := range r.Files {
		out = append(out, f.Path)
	}
	return out
}

func (r *Result) add(path string, action Action, content string) {
	r.Files = append(r.Files, File{
		Path: path, Action: action, Content: content, Bytes: len(content),
	})
}

func (r *Result) note(format string, args ...any) {
	r.Notes = append(r.Notes, fmt.Sprintf(format, args...))
}

// ── writing ─────────────────────────────────────────────────────────────────

// ExistsError reports files a create would have overwritten.
//
// Refusing rather than clobbering is the same discipline write_file follows
// (Part A §7.2): an agent that overwrites is an agent whose mistakes are not
// recoverable from git, because the previous content was never committed.
type ExistsError struct{ Paths []string }

// Error names the files that would have been overwritten.
func (e *ExistsError) Error() string {
	return fmt.Sprintf("refusing to overwrite existing file(s): %s\n"+
		"the resource may already be scaffolded — check the paths, or remove them first",
		strings.Join(e.Paths, ", "))
}

// Apply writes a result to disk. Files marked ActionCreate must not already
// exist; files marked ActionModify are expected to.
//
// New files are written with the line ending the destination repository already
// uses. Templates render LF, because a golden snapshot of an LF file is one that
// git and every diff tool agree about; but every .go file in n-api-template is
// CRLF, and a scaffolder that drops LF files into it leaves a repository with
// two conventions. Modified files already preserve their own endings through
// gopatch, so without this the same scaffold call produced CRLF for the two
// files it edits and LF for the five it creates — a split nobody chose.
//
// The consequence is not cosmetic. A repository with mixed endings and no
// .gitattributes (n-api-template has none) converts silently the moment anyone
// sets core.autocrlf or adds `* text=auto`, and every LF file then shows as
// entirely rewritten by a commit that touched none of them.
func Apply(root string, res *Result) error {
	var clashes []string
	for _, f := range res.Files {
		if f.Action != ActionCreate {
			continue
		}
		if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(f.Path))); err == nil {
			clashes = append(clashes, f.Path)
		}
	}
	if len(clashes) > 0 {
		sort.Strings(clashes)
		return &ExistsError{Paths: clashes}
	}

	eol := RepoEOL(root)
	for _, f := range res.Files {
		abs := filepath.Join(root, filepath.FromSlash(f.Path))
		if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
			return fmt.Errorf("create %s: %w", filepath.Dir(f.Path), err)
		}
		content := []byte(f.Content)
		if f.Action == ActionCreate {
			content = gopatch.ApplyEOL(gopatch.ToLF(content), eol)
		}
		if err := os.WriteFile(abs, content, 0o644); err != nil {
			return fmt.Errorf("write %s: %w", f.Path, err)
		}
	}
	return nil
}

// eolSample is how many existing files RepoEOL looks at. Enough to be decided by
// the convention rather than by one stray file, few enough to cost nothing.
const eolSample = 12

// RepoEOL reports the line ending a repository already uses for Go source.
//
// LF for an empty directory, which is what project_scaffold gets: a brand-new
// repository has no convention to match, and LF is the one to start with.
func RepoEOL(root string) gopatch.EOL {
	var crlf, lf, seen int

	_ = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil || seen >= eolSample {
			return nil
		}
		if d.IsDir() {
			switch d.Name() {
			case ".git", "vendor", "node_modules", "bin", "testdata":
				return filepath.SkipDir
			}
			return nil
		}
		if filepath.Ext(path) != ".go" {
			return nil
		}
		// Only the head of the file: line endings do not change halfway down,
		// and reading whole files to count newlines would make a scaffold of a
		// large repository pay for something it already knows by line twenty.
		f, oerr := os.Open(path)
		if oerr != nil {
			return nil
		}
		var buf [4096]byte
		n, _ := f.Read(buf[:])
		_ = f.Close()

		head := buf[:n]
		c := bytes.Count(head, []byte("\r\n"))
		crlf += c
		lf += bytes.Count(head, []byte("\n")) - c
		seen++
		return nil
	})

	if crlf > lf {
		return gopatch.CRLF
	}
	return gopatch.LF
}

// ── rendering ───────────────────────────────────────────────────────────────

// data is what every template receives.
type data struct {
	R       spec.Resource
	Module  string
	Layout  string
	Imports string
	// Project fields, unused by resource templates.
	P Project
}

// funcs are the template helpers. They are deliberately few: logic that needs
// more than these belongs in spec's derived accessors, where it is testable
// without rendering a template.
var funcs = template.FuncMap{
	"sig":     spec.SignatureOf,
	"args":    spec.ArgsOf,
	"selargs": selectorArgs,
}

// selectorArgs renders parameters as field selectors on a bound request struct:
// selargs "req." → `req.PPONumber, req.Amount`.
func selectorArgs(prefix string, params []spec.Param) string {
	out := make([]string, len(params))
	for i, p := range params {
		out[i] = prefix + p.Field.Go
	}
	return strings.Join(out, ", ")
}

var tmpl = template.Must(
	template.New("scaffold").Funcs(funcs).ParseFS(templateFS, "templates/*/*.tmpl"),
)

// render executes a template into a string.
func render(name string, d data) (string, error) {
	var buf bytes.Buffer
	if err := tmpl.ExecuteTemplate(&buf, name, d); err != nil {
		return "", fmt.Errorf("render %s: %w", name, err)
	}
	return buf.String(), nil
}

// renderGo executes a template and gofmts the result, so a template that
// produces invalid Go fails here with a position rather than at the developer's
// next build.
func renderGo(name string, d data) (string, error) {
	raw, err := render(name, d)
	if err != nil {
		return "", err
	}
	formatted, err := gopatch.Format([]byte(raw))
	if err != nil {
		return "", fmt.Errorf("template %s: %w", name, err)
	}
	return string(formatted), nil
}

// ── imports ─────────────────────────────────────────────────────────────────

// imp is one import line.
type imp struct{ Alias, Path string }

// renderImports emits a gofmt-stable import block: standard library first, then
// this module's packages, then external ones, each group sorted and separated
// by a blank line.
//
// Built here rather than in the templates because the conditionals ("does this
// repository need squirrel?") are decisions, and decisions read better in Go
// than in nested template actions — and because getting the blank lines right
// through {{if}} whitespace trimming is a losing game.
func renderImports(module string, imps []imp) string {
	groups := make([][]string, 3)
	for _, im := range imps {
		if im.Path == "" {
			continue
		}
		line := "\t" + quote(im.Path)
		if im.Alias != "" {
			line = "\t" + im.Alias + " " + quote(im.Path)
		}
		g := gopatch.ClassifyImport(im.Path, module)
		groups[g] = append(groups[g], line)
	}

	var rendered []string
	for _, g := range groups {
		if len(g) == 0 {
			continue
		}
		sort.Strings(g)
		rendered = append(rendered, strings.Join(g, "\n"))
	}
	if len(rendered) == 0 {
		return ""
	}
	if total := countLines(rendered); total == 1 {
		return "import " + strings.TrimSpace(rendered[0])
	}
	return "import (\n" + strings.Join(rendered, "\n\n") + "\n)"
}

func countLines(groups []string) int {
	n := 0
	for _, g := range groups {
		n += strings.Count(g, "\n") + 1
	}
	return n
}

func quote(s string) string { return `"` + s + `"` }
