package kb

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// ManifestFile is where the pinned document state lives, relative to the
// gotools module root.
const ManifestFile = "docs/doc-manifest.json"

// Manifest pins the reference documents the rule set cites.
//
// Checked in and updated deliberately. The point is not that the hash is
// interesting — it is that changing skill.md or SOP.md cannot happen without
// someone touching this file, and touching this file is the moment to ask which
// rules the change affects.
type Manifest struct {
	// Comment is written into the file so anyone who opens it knows why it
	// exists without going looking.
	Comment string `json:"_comment"`
	// Docs maps a document name to its pinned state.
	Docs map[string]DocPin `json:"docs"`
}

// DocPin is one document's pinned state.
type DocPin struct {
	Hash     string `json:"hash"`
	Lines    int    `json:"lines"`
	Sections int    `json:"sections"`
	// CitedSections are the headings the rule set depends on, so a diff on this
	// file shows which rules a document change might invalidate.
	CitedSections []string `json:"cited_sections,omitempty"`
}

const manifestComment = "Pinned state of the template reference documents. " +
	"Rules cite sections of these; changing a document without reviewing the rules that " +
	"cite it is how the rule set drifts from the template it enforces (plan.md §14.4). " +
	"Regenerate deliberately with `gotools doc-check --update` and review what moved."

// Citation is a rule's citation, ready to be checked.
type Citation struct {
	RuleID string
	Text   string
}

// Problem is one thing wrong with the documentation coupling.
type Problem struct {
	Kind   string `json:"kind"` // "unresolved" | "missing-doc" | "drift" | "unpinned"
	RuleID string `json:"rule_id,omitempty"`
	Doc    string `json:"doc,omitempty"`
	Detail string `json:"detail"`
	Fix    string `json:"fix,omitempty"`
}

func (p Problem) String() string {
	head := p.Kind
	if p.RuleID != "" {
		head += " [" + p.RuleID + "]"
	}
	s := head + ": " + p.Detail
	if p.Fix != "" {
		s += "\n      fix: " + p.Fix
	}
	return s
}

// Report is the outcome of a check.
type Report struct {
	OK       bool      `json:"ok"`
	Problems []Problem `json:"problems,omitempty"`
	// Checked lists the documents that were read.
	Checked []string `json:"checked"`
	// Resolved counts citations that named a section and found it.
	Resolved int `json:"resolved"`
	// Manifest is the current state, whether or not it was written.
	Manifest Manifest `json:"-"`
}

// Check resolves every citation against the documents and compares the result
// with the pinned manifest.
//
// docsDir is the directory holding skill.md and SOP.md — the reference template.
// Documents a citation names but that are not present are reported once rather
// than per rule: an absent corpus is one problem, not thirty.
func Check(docsDir string, citations []Citation, pinned *Manifest) (*Report, error) {
	rep := &Report{Manifest: Manifest{Comment: manifestComment, Docs: map[string]DocPin{}}}

	// Only documents this package pins. A citation naming go.mod or a config
	// file is a pointer for a human, not a section reference to resolve.
	pinnable := map[string]bool{"skill.md": true, "SOP.md": true}

	docs := map[string]*Doc{}
	cited := map[string]map[string]bool{}
	missing := map[string]bool{}

	for _, c := range citations {
		for _, ref := range ParseCitation(c.Text) {
			if !pinnable[ref.Doc] {
				continue
			}
			if _, loaded := docs[ref.Doc]; !loaded && !missing[ref.Doc] {
				d, err := LoadDoc(filepath.Join(docsDir, ref.Doc))
				if err != nil {
					missing[ref.Doc] = true
					rep.Problems = append(rep.Problems, Problem{
						Kind: "missing-doc", Doc: ref.Doc,
						Detail: fmt.Sprintf("%s is cited by the rule set but was not found in %s", ref.Doc, docsDir),
						Fix:    "point --docs at the reference template, or check the corpus is present",
					})
					continue
				}
				docs[ref.Doc] = d
				cited[ref.Doc] = map[string]bool{}
			}
			doc := docs[ref.Doc]
			if doc == nil || ref.Section == "" {
				continue
			}
			sec, ok := doc.Section(ref.Section)
			if !ok {
				rep.Problems = append(rep.Problems, Problem{
					Kind: "unresolved", RuleID: c.RuleID, Doc: ref.Doc,
					Detail: fmt.Sprintf("citation names %s §%s, which is not a heading in that document", ref.Doc, ref.Section),
					Fix:    "cite a real heading — " + nearest(doc, ref.Section),
				})
				continue
			}
			rep.Resolved++
			cited[ref.Doc][sec.Title] = true
		}
	}

	for name, doc := range docs {
		pin := DocPin{Hash: doc.Hash, Lines: doc.Lines, Sections: len(doc.Sections)}
		for title := range cited[name] {
			pin.CitedSections = append(pin.CitedSections, title)
		}
		sort.Strings(pin.CitedSections)
		rep.Manifest.Docs[name] = pin
		rep.Checked = append(rep.Checked, name)
	}
	sort.Strings(rep.Checked)

	if pinned != nil {
		compare(rep, pinned)
	}
	rep.OK = len(rep.Problems) == 0
	return rep, nil
}

// compare reports drift between the pinned manifest and what is on disk.
func compare(rep *Report, pinned *Manifest) {
	for _, name := range rep.Checked {
		now := rep.Manifest.Docs[name]
		before, ok := pinned.Docs[name]
		if !ok {
			rep.Problems = append(rep.Problems, Problem{
				Kind: "unpinned", Doc: name,
				Detail: fmt.Sprintf("%s is cited by the rule set but is not in %s", name, ManifestFile),
				Fix:    "run `gotools doc-check --update` and commit the manifest",
			})
			continue
		}
		if before.Hash == now.Hash {
			continue
		}
		// Name the sections the rules actually depend on, because those are the
		// ones worth reading the diff for.
		affected := intersect(before.CitedSections, now.CitedSections)
		detail := fmt.Sprintf("%s changed (%d → %d lines, %d → %d headings)",
			name, before.Lines, now.Lines, before.Sections, now.Sections)
		if gone := missingFrom(before.CitedSections, now.CitedSections); len(gone) > 0 {
			detail += fmt.Sprintf("; %d cited heading(s) no longer exist: %s",
				len(gone), strings.Join(gone, ", "))
		}
		rep.Problems = append(rep.Problems, Problem{
			Kind: "drift", Doc: name, Detail: detail,
			Fix: fmt.Sprintf("review the %d rule citation(s) into %s, then run `gotools doc-check --update`",
				len(affected), name),
		})
	}
}

// nearest suggests the closest heading, so an unresolved citation comes with a
// candidate rather than just a complaint.
func nearest(doc *Doc, want string) string {
	w := normaliseTitle(want)
	best, score := "", 0
	for _, s := range doc.Sections {
		if n := similarity(w, normaliseTitle(s.Title)); n > score {
			best, score = s.Title, n
		}
	}
	if best == "" {
		return "no heading in " + doc.Name + " resembles it"
	}
	return fmt.Sprintf("did you mean %s §%s?", doc.Name, best)
}

// similarity ranks a heading against a citation that failed to resolve.
//
// Shared words dominate, with the common prefix as a tie-break. The tie-break
// earns its place on the case this check exists for: when `§Repository Pattern`
// is renamed to `§Repository Patterns`, word overlap alone scores it level with
// `§Domain Model Pattern` — both share exactly one word — and the suggestion
// lands on the wrong one. The prefix separates them.
//
// Deliberately not edit distance: this ranks a handful of candidates for a
// human to read, and a wrong-but-plausible suggestion costs nothing because the
// heading list is right there in the document.
func similarity(a, b string) int {
	set := map[string]bool{}
	for _, w := range strings.Fields(a) {
		set[w] = true
	}
	shared := 0
	for _, w := range strings.Fields(b) {
		if set[w] {
			shared++
		}
	}
	if shared == 0 {
		return 0
	}
	return shared*100 + commonPrefixLen(a, b)
}

func commonPrefixLen(a, b string) int {
	n := min(len(a), len(b))
	for i := range n {
		if a[i] != b[i] {
			return i
		}
	}
	return n
}

func intersect(a, b []string) []string {
	in := map[string]bool{}
	for _, s := range a {
		in[s] = true
	}
	var out []string
	for _, s := range b {
		if in[s] {
			out = append(out, s)
		}
	}
	return out
}

func missingFrom(before, after []string) []string {
	have := map[string]bool{}
	for _, s := range after {
		have[s] = true
	}
	var out []string
	for _, s := range before {
		if !have[s] {
			out = append(out, s)
		}
	}
	sort.Strings(out)
	return out
}

// ── manifest I/O ────────────────────────────────────────────────────────────

// LoadManifest reads the pinned manifest. A missing file is not an error: the
// caller decides whether an unpinned rule set is a problem, and on the first
// run it is not.
func LoadManifest(path string) (*Manifest, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	var m Manifest
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	return &m, nil
}

// SaveManifest writes the manifest, creating its directory if needed.
func SaveManifest(path string, m Manifest) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("create %s: %w", filepath.Dir(path), err)
	}
	b, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return fmt.Errorf("encode manifest: %w", err)
	}
	return os.WriteFile(path, append(b, '\n'), 0o644)
}
