// Package kb reads the template's reference documents and keeps the rule set
// honest about them.
//
// # Why this exists
//
// Every rule cites the section of skill.md or SOP.md it enforces, and that
// citation is rendered with every violation. It is the difference between a
// tool that looks opinionated and one a developer can check. But a citation is
// just a string: nothing stops it naming a section that was renamed, or a
// section whose content now says something the rule no longer enforces.
//
// plan.md §14.4 asks for the check: pin a hash of skill.md and SOP.md, and fail
// CI when they change without a corresponding rules review. Two divergences
// between the documents and the shipped reference resource are already known,
// so the check has work to do the day it lands.
//
// This package supplies the parsing and resolution; `gotools doc-check` is the
// command that runs it.
package kb

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// Doc is a parsed reference document.
type Doc struct {
	// Name is the file name as citations write it: "skill.md", "SOP.md".
	Name string
	// Path is where it was read from.
	Path string
	// Hash is the SHA-256 of the content with line endings normalised, so the
	// pin is the same on every platform. The reference template is CRLF and a
	// checkout on Linux would otherwise never match one on Windows.
	Hash string
	// Lines is the document's length, for the drift report.
	Lines int
	// Sections are the headings, in document order.
	Sections []Section
}

// Section is one heading and the body beneath it.
type Section struct {
	Title string
	Level int
	// Line is the 1-indexed line of the heading itself.
	Line int
	// EndLine is the last line of the section's body.
	EndLine int
	// Body is the text between this heading and the next of the same or a
	// higher level.
	Body string
}

// headingRe matches an ATX heading. Setext headings are not used by either
// document and supporting them would mean tracking the previous line for no
// benefit.
var headingRe = regexp.MustCompile(`^(#{1,6})\s+(.+?)\s*$`)

// fenceRe matches a code fence. Headings inside fences are code, not structure:
// skill.md's shell examples are full of `# Initialize new module` comments,
// and counting those as sections would put forty phantom entries in the map.
var fenceRe = regexp.MustCompile("^\\s*(```|~~~)")

// LoadDoc parses a markdown document.
func LoadDoc(path string) (*Doc, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	body := strings.ReplaceAll(string(raw), "\r\n", "\n")
	sum := sha256.Sum256([]byte(body))

	doc := &Doc{
		Name: filepath.Base(path),
		Path: path,
		Hash: "sha256:" + hex.EncodeToString(sum[:]),
	}

	lines := strings.Split(body, "\n")
	doc.Lines = len(lines)

	inFence := false
	for i, line := range lines {
		if fenceRe.MatchString(line) {
			inFence = !inFence
			continue
		}
		if inFence {
			continue
		}
		m := headingRe.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		doc.Sections = append(doc.Sections, Section{
			Title: m[2],
			Level: len(m[1]),
			Line:  i + 1,
		})
	}

	// Close each section at the next heading of the same or higher level.
	for i := range doc.Sections {
		end := len(lines)
		for j := i + 1; j < len(doc.Sections); j++ {
			if doc.Sections[j].Level <= doc.Sections[i].Level {
				end = doc.Sections[j].Line - 1
				break
			}
		}
		doc.Sections[i].EndLine = end
		lo := doc.Sections[i].Line // skip the heading line itself
		if lo < end {
			doc.Sections[i].Body = strings.Join(lines[lo:end], "\n")
		}
	}
	return doc, nil
}

// Section resolves a citation's section name to a heading.
//
// Matching is exact first, then prefix in both directions, because citations
// are written for a reader rather than a parser. `SOP.md §[handler].go steps
// 2–3` names a real section and adds a locator; `skill.md §Repository` would
// name one and abbreviate it. Both should resolve; neither should be a licence
// to invent a section that is not there.
func (d *Doc) Section(title string) (Section, bool) {
	want := normaliseTitle(title)
	for _, s := range d.Sections {
		if normaliseTitle(s.Title) == want {
			return s, true
		}
	}
	// The citation carries a locator after the heading, or abbreviates it.
	var best Section
	found := false
	for _, s := range d.Sections {
		got := normaliseTitle(s.Title)
		if !prefixAtWordBoundary(want, got) && !prefixAtWordBoundary(got, want) {
			continue
		}
		// Prefer the longest heading that still matches, so "§Handler Pattern"
		// does not resolve to a shorter "§Handler" if both exist.
		if !found || len(s.Title) > len(best.Title) {
			best, found = s, true
		}
	}
	return best, found
}

// prefixAtWordBoundary reports whether short is a prefix of long *and* the
// remainder starts a new word.
//
// The word boundary is the whole point. Without it "§Repository Patterns That
// Do Not Exist" resolves happily to "§Repository Pattern", because one is a
// character-prefix of the other — and a citation that resolves to the wrong
// section is worse than one that does not resolve, since nothing reports it.
// With it, only a genuine locator ("§[handler].go steps 2–3") or a genuine
// abbreviation ("§Repository" for "§Repository Pattern") matches.
func prefixAtWordBoundary(long, short string) bool {
	if short == "" || len(long) < len(short) || !strings.HasPrefix(long, short) {
		return false
	}
	return len(long) == len(short) || long[len(short)] == ' '
}

// Titles lists every heading, in document order.
func (d *Doc) Titles() []string {
	out := make([]string, 0, len(d.Sections))
	for _, s := range d.Sections {
		out = append(out, s.Title)
	}
	return out
}

// normaliseTitle folds the differences that are noise for matching: case, the
// en-dash versus hyphen that creeps in through editors, and surrounding
// punctuation.
func normaliseTitle(s string) string {
	s = strings.ToLower(strings.TrimSpace(s))
	s = strings.ReplaceAll(s, "–", "-")
	s = strings.ReplaceAll(s, "—", "-")
	s = strings.Trim(s, " .,:;`\"'")
	return strings.Join(strings.Fields(s), " ")
}

// ── citations ───────────────────────────────────────────────────────────────

// Ref is one document section named by a citation.
type Ref struct {
	Doc     string // "skill.md", "SOP.md", …
	Section string // without the § sign
}

func (r Ref) String() string {
	if r.Section == "" {
		return r.Doc
	}
	return r.Doc + " §" + r.Section
}

// docRe matches a document name in a citation.
var docRe = regexp.MustCompile(`([A-Za-z0-9_.\-/]+\.(?:md|go|yaml|yml|mod))`)

// ParseCitation splits a rule citation into the document sections it names.
//
// Citations are prose, deliberately — they are read by developers far more often
// than by this parser. The grammar they happen to follow is:
//
//	<doc> §<section>[, §<section>…][; <doc> §<section>…]
//
// A clause with no § names a whole file (`go.mod`, `core/port/response.go`) and
// yields a Ref with an empty Section, which resolution treats as "the file must
// exist" rather than "this heading must exist".
func ParseCitation(citation string) []Ref {
	var refs []Ref
	for _, clause := range strings.Split(citation, ";") {
		clause = strings.TrimSpace(clause)
		if clause == "" {
			continue
		}

		// The document name is whatever precedes the first §. Searching the
		// whole clause instead would find the filename inside a section name:
		// `SOP.md §Define the Routes; §main.go` has a second clause naming a
		// *section* called main.go, not a document called main.go.
		head, rest, hasSection := strings.Cut(clause, "§")
		doc := docRe.FindString(head)
		if doc == "" {
			// A bare "§Section" continues the previous clause's document.
			if len(refs) == 0 {
				continue
			}
			doc = refs[len(refs)-1].Doc
		}
		if !hasSection {
			refs = append(refs, Ref{Doc: doc})
			continue
		}
		for _, part := range strings.Split("§"+rest, "§")[1:] {
			// A section runs to the next comma that separates sections, which
			// is the only separator citations use inside a clause.
			sec := strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(part), ","))
			if i := strings.Index(sec, ", §"); i >= 0 {
				sec = sec[:i]
			}
			sec = strings.TrimSpace(strings.Trim(sec, ","))
			if sec != "" {
				refs = append(refs, Ref{Doc: doc, Section: sec})
			}
		}
	}
	return refs
}
