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

	disabled map[string]bool
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
		TimestampLayout: spec.TimestampLayout,
	}
	c.index()
	return c
}

func (c *Config) index() {
	c.disabled = make(map[string]bool, len(c.Disable))
	for _, id := range c.Disable {
		c.disabled[id] = true
	}
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
	merged.Disable = over.Disable
	merged.SeverityOverride = over.SeverityOverride
	merged.index()
	return merged, nil
}
