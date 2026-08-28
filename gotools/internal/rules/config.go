package rules

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/spec"
)

// ConfigFile is the optional per-repository config, resolved relative to the
// workspace root.
const ConfigFile = ".dakcoder/gotools.yaml"

// Config carries everything a rule needs that is deployment- rather than
// code-dependent.
//
// The allow-list in particular MUST be data, not a Go constant. It changes when
// the IT 2.0 common libraries release, and a rule set that requires a binary
// rebuild to accept a new approved dependency will be worked around rather than
// updated.
type Config struct {
	// AllowedDeps are direct dependency path prefixes that need no approval.
	AllowedDeps []string `yaml:"allowed_deps"`

	// AllowedTestDeps are additionally permitted in _test.go files. Test code
	// legitimately uses testify, testcontainers and golang-migrate; treating
	// those as violations trains people to ignore the linter.
	AllowedTestDeps []string `yaml:"allowed_test_deps"`

	// MaxFileLines caps a single .go file. 0 disables the check.
	MaxFileLines int `yaml:"max_file_lines"`

	// Disable lists rule IDs to switch off entirely.
	Disable []string `yaml:"disable"`

	// SeverityOverride maps a rule ID to "error" or "warning".
	SeverityOverride map[string]Severity `yaml:"severity"`

	// RequiredDomainFields are the fields every domain struct must carry.
	RequiredDomainFields []string `yaml:"required_domain_fields"`

	// TimestampLayout is the string layout response DTOs must format times with.
	TimestampLayout string `yaml:"timestamp_layout"`

	// RoundTripNotice is the number of separate database calls in one
	// repository method at which repo-multi-roundtrip mentions that a batch is
	// possible. RoundTripRecommend is where it recommends one.
	//
	// Two thresholds rather than one because the review's own answer was two
	// thresholds: flag at 2, recommend at 3, mandate never. A batch is not
	// always available — the second query may depend on the first query's
	// result — so both tiers are advisory and neither blocks.
	RoundTripNotice    int `yaml:"round_trip_notice"`
	RoundTripRecommend int `yaml:"round_trip_recommend"`

	// SensitiveFields are field and variable names that must never reach a log
	// line. Matched as whole words against the identifier, never as substrings:
	// `pan` as a substring matches CompanyName, Discrepancy and
	// NoOfPanchayatSanchaarSevaKendras, and a rule that is wrong nineteen times
	// out of twenty gets switched off.
	SensitiveFields []string `yaml:"sensitive_fields"`

	disabled  map[string]bool
	sensitive map[string]bool
}

// DefaultConfig returns the built-in defaults, derived from the reference
// template's go.mod and from skill.md / SOP.md.
func DefaultConfig() Config {
	c := Config{
		AllowedDeps: []string{
			"github.com/Masterminds/squirrel",
			"github.com/jackc/pgx/v5",
			"gitlab.cept.gov.in/it-2.0-common/",
			"go.uber.org/fx",
		},
		AllowedTestDeps: []string{
			"github.com/stretchr/testify",
			"github.com/testcontainers/testcontainers-go",
			"github.com/golang-migrate/migrate",
			"github.com/magiconair/properties",
			"go.uber.org/mock",
			"github.com/golang/mock",
		},
		MaxFileLines:         600,
		RequiredDomainFields: []string{"ID", "CreatedAt", "UpdatedAt"},
		// Taken from the spec package rather than repeated, so the layout the
		// rule checks is by construction the layout the scaffolder writes.
		TimestampLayout:    spec.TimestampLayout,
		RoundTripNotice:    2,
		RoundTripRecommend: 3,
		SensitiveFields:    DefaultSensitiveFields,
	}
	c.index()
	return c
}

// DefaultSensitiveFields is the starting list of identifiers that must not be
// logged.
//
// Aadhaar appears four ways because production request structs spell it four
// ways — AadhaarNumber, Aadhaar_Number, AadharNumber and ReceiverAdharNo all
// exist. A list carrying one spelling silently misses the other three, which is
// the failure mode that matters most here.
var DefaultSensitiveFields = []string{
	"password", "passwd", "pwd",
	"token", "accesstoken", "refreshtoken",
	"authorization", "auth", "secret", "secretkey", "credential", "credentials",
	"otp",
	"aadhaar", "aadhar", "adhar", "aadhaarnumber", "aadharnumber",
	"pan", "pannumber",
	"mobile", "mobileno", "mobilenumber", "phone", "phoneno", "phonenumber",
	"email", "emailid",
	"accountnumber", "accountno", "ifsc", "upiid",
	"card", "cardnumber", "cvv", "pin",
	"dob", "dateofbirth",
}

func (c *Config) index() {
	c.disabled = make(map[string]bool, len(c.Disable))
	for _, id := range c.Disable {
		c.disabled[id] = true
	}
	c.sensitive = make(map[string]bool, len(c.SensitiveFields))
	for _, f := range c.SensitiveFields {
		c.sensitive[normaliseIdent(f)] = true
	}
}

// IsSensitive reports whether an identifier names something that must not be
// logged.
//
// Matching is on *words*, not substrings, and that distinction is the rule. The
// identifier is split on underscores and camel-case boundaries, then each word
// and each adjacent pair is looked up. So:
//
//	ReceiverAdharNo   -> receiver | adhar | no      -> matches "adhar"
//	AccountNo         -> account  | no              -> matches the pair "accountno"
//	CompanyName       -> company  | name            -> no match
//	NoOfPanchayat…    -> no | of | panchayat | …    -> no match for "pan"
//
// Measured against the 8,229 distinct field names in the review sheets, a plain
// substring match on "pan" hits 20 of them and 19 are innocent. A rule that is
// wrong nineteen times out of twenty is a rule somebody disables.
func (c Config) IsSensitive(ident string) bool {
	if ident == "" {
		return false
	}
	if c.sensitive[normaliseIdent(ident)] {
		return true
	}
	words := identWords(ident)
	for i, w := range words {
		if c.sensitive[w] {
			return true
		}
		if i+1 < len(words) && c.sensitive[w+words[i+1]] {
			return true
		}
	}
	return false
}

// identWords splits an identifier into lower-cased words on underscores and
// camel-case boundaries, keeping initialisms whole: `PANNumber` yields
// [pan, number] and `OTP` yields [otp].
func identWords(s string) []string {
	var out []string
	var cur strings.Builder
	flush := func() {
		if cur.Len() > 0 {
			out = append(out, strings.ToLower(cur.String()))
			cur.Reset()
		}
	}
	rs := []rune(s)
	for i, r := range rs {
		switch {
		case r == '_' || r == '-' || r == ' ':
			flush()
			continue
		case isUpper(r):
			// Start a word at lower->upper, and at the last capital of a run
			// that is followed by a lower-case letter (PANNumber -> PAN|Number).
			prevLower := i > 0 && !isUpper(rs[i-1]) && rs[i-1] != '_'
			nextLower := i+1 < len(rs) && !isUpper(rs[i+1]) && rs[i+1] != '_'
			prevUpper := i > 0 && isUpper(rs[i-1])
			if prevLower || (prevUpper && nextLower) {
				flush()
			}
		}
		cur.WriteRune(r)
	}
	flush()
	return out
}

func isUpper(r rune) bool { return r >= 'A' && r <= 'Z' }

// normaliseIdent lowercases an identifier and drops separators, so the list can
// hold one spelling per concept rather than one per naming convention.
func normaliseIdent(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'A' && r <= 'Z':
			b.WriteRune(r + ('a' - 'A'))
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			b.WriteRune(r)
		}
	}
	return b.String()
}

func (c Config) isZero() bool { return c.AllowedDeps == nil && c.MaxFileLines == 0 }

// Disabled reports whether a rule is switched off.
func (c Config) Disabled(id string) bool { return c.disabled[id] }

// SeverityFor returns the effective severity for a rule.
func (c Config) SeverityFor(r *Rule) Severity {
	if s, ok := c.SeverityOverride[r.ID]; ok && (s == SeverityError || s == SeverityWarning) {
		return s
	}
	return r.Severity
}

// HasSeverityOverride reports whether a repository has pinned this rule's
// severity. When it has, a per-finding override does not apply: an operator who
// wrote `severity: {go-idiom: warning}` means every go-idiom finding, and a
// rule quietly escalating one of them past that would make the setting
// untrustworthy.
func (c Config) HasSeverityOverride(id string) bool {
	s, ok := c.SeverityOverride[id]
	return ok && (s == SeverityError || s == SeverityWarning)
}

// DepAllowed reports whether an import path is permitted as a direct
// dependency. Standard-library paths (no dot in the first segment) are always
// allowed, as are same-module imports.
func (c Config) DepAllowed(path, modulePath string, inTest bool) bool {
	if !isExternal(path) {
		return true
	}
	if modulePath != "" && (path == modulePath || strings.HasPrefix(path, modulePath+"/")) {
		return true
	}
	for _, a := range c.AllowedDeps {
		if strings.HasPrefix(path, a) {
			return true
		}
	}
	if inTest {
		for _, a := range c.AllowedTestDeps {
			if strings.HasPrefix(path, a) {
				return true
			}
		}
	}
	return false
}

// isExternal reports whether an import path names a module rather than the
// standard library. The standard library never has a dot in its first segment.
func isExternal(path string) bool {
	first, _, _ := strings.Cut(path, "/")
	return strings.Contains(first, ".")
}

// LoadConfig reads .dakcoder/gotools.yaml from the workspace root, layering it
// over the defaults. A missing file is not an error.
func LoadConfig(root string) (Config, error) {
	base := DefaultConfig()
	b, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(ConfigFile)))
	if err != nil {
		if os.IsNotExist(err) {
			return base, nil
		}
		return base, fmt.Errorf("read %s: %w", ConfigFile, err)
	}
	var over Config
	if err := yaml.Unmarshal(b, &over); err != nil {
		return base, fmt.Errorf("parse %s: %w", ConfigFile, err)
	}
	merged := base
	// Replace-not-append: a repo that narrows its allow-list must be able to,
	// and an append-only merge makes that impossible.
	if over.AllowedDeps != nil {
		merged.AllowedDeps = over.AllowedDeps
	}
	if over.AllowedTestDeps != nil {
		merged.AllowedTestDeps = over.AllowedTestDeps
	}
	if over.MaxFileLines != 0 {
		merged.MaxFileLines = over.MaxFileLines
	}
	if over.RequiredDomainFields != nil {
		merged.RequiredDomainFields = over.RequiredDomainFields
	}
	if over.TimestampLayout != "" {
		merged.TimestampLayout = over.TimestampLayout
	}
	if over.RoundTripNotice != 0 {
		merged.RoundTripNotice = over.RoundTripNotice
	}
	if over.RoundTripRecommend != 0 {
		merged.RoundTripRecommend = over.RoundTripRecommend
	}
	if over.SensitiveFields != nil {
		merged.SensitiveFields = over.SensitiveFields
	}
	merged.Disable = over.Disable
	merged.SeverityOverride = over.SeverityOverride
	merged.index()
	return merged, nil
}
