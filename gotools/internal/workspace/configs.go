package workspace

import (
	"os"
	"path/filepath"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// ConfigDir is where the template keeps its per-environment configuration.
const ConfigDir = "configs"

// ConfigFile is one parsed configs/*.yaml.
//
// Configuration is part of the workspace, not a separate concern, and three
// rules need it: whether a key a repository reads actually exists, whether
// swagger generation is switched on, and whether a credential has been
// committed. Loading it here keeps the "parse once, hand it to every rule"
// property that the Go files already have.
type ConfigFile struct {
	Rel string // workspace-relative, forward slashes
	Env string // "" for the base config.yaml; otherwise "dev", "prod", …

	// Keys maps a lower-cased dotted path to the key that declares it.
	//
	// Lower-cased because viper — which is what *config.Config wraps — folds
	// key case internally, so `db.QueryTimeoutLow` and `db.querytimeoutlow`
	// address the same value. A case-sensitive comparison here would report a
	// key as missing when the application finds it perfectly well.
	Keys map[string]ConfigKey

	ParseErr error
}

// ConfigKey is one key in a config file.
type ConfigKey struct {
	// Path is the dotted path with its original casing, for messages.
	Path string
	Line int
	Col  int

	// Scalar reports whether the key holds a scalar rather than a map or list.
	Scalar bool

	// Empty reports whether a scalar key has no value at all — `password:` with
	// nothing after it. This is what a config with its credentials stripped
	// looks like, and it is deliberately distinguished from a key that is
	// present with a value.
	Empty bool

	// value is the scalar as written.
	//
	// Unexported on purpose. Rules need to know whether a credential key has a
	// value; they must never be able to put that value into a violation
	// message, a log line, or a prompt. plan.md §17 requires that committed
	// credentials are "never echoed into a prompt, a log, a trace or a diff",
	// and the cheapest way to hold that line is to make it impossible to reach.
	value string
}

// Looks like a placeholder rather than a real value.
var placeholderValues = map[string]bool{
	"": true, "~": true, "null": true, "<nil>": true,
	"changeme": true, "change_me": true, "todo": true, "tbd": true,
	"xxx": true, "placeholder": true, "example": true, "none": true,
	"your_password_here": true, "secret": true,
}

// HasRealValue reports whether a scalar key carries something that looks like a
// live value rather than a blank or a placeholder.
//
// Returns only a boolean. The value itself is not reachable from outside this
// package, which is the point.
func (k ConfigKey) HasRealValue() bool {
	if !k.Scalar || k.Empty {
		return false
	}
	v := strings.TrimSpace(k.value)
	if placeholderValues[strings.ToLower(v)] {
		return false
	}
	// Environment interpolation and angle-bracket placeholders are explicitly
	// "fill this in", not a committed secret.
	if strings.HasPrefix(v, "${") || (strings.HasPrefix(v, "<") && strings.HasSuffix(v, ">")) {
		return false
	}
	return true
}

// Configs returns the parsed config files in stable path order.
func (w *Workspace) Configs() []*ConfigFile { return w.configs }

// BaseConfig returns configs/config.yaml, the file the application reads when
// no environment is selected. Nil when it does not exist.
func (w *Workspace) BaseConfig() *ConfigFile {
	for _, c := range w.configs {
		if c.Env == "" {
			return c
		}
	}
	return nil
}

// loadConfigs parses every configs/*.yaml.
//
// A file that does not parse is recorded rather than fatal: the agent can
// legitimately leave a config mid-edit, and refusing to lint the Go code
// because a YAML file is briefly broken would be exactly the wrong trade.
func (w *Workspace) loadConfigs() {
	dir := filepath.Join(w.Root, ConfigDir)
	entries, err := os.ReadDir(dir)
	if err != nil {
		return // no configs/ directory; rules that need one will no-op
	}

	var names []string
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		n := e.Name()
		if strings.HasSuffix(n, ".yaml") || strings.HasSuffix(n, ".yml") {
			names = append(names, n)
		}
	}
	sort.Strings(names)

	for _, name := range names {
		body, rerr := os.ReadFile(filepath.Join(dir, name))
		if rerr != nil {
			continue
		}
		w.readCount++

		cf := &ConfigFile{
			Rel:  ConfigDir + "/" + name,
			Env:  envOf(name),
			Keys: map[string]ConfigKey{},
		}
		var doc yaml.Node
		if perr := yaml.Unmarshal(body, &doc); perr != nil {
			cf.ParseErr = perr
			w.configs = append(w.configs, cf)
			continue
		}
		if len(doc.Content) > 0 {
			flatten(doc.Content[0], "", cf.Keys)
		}
		w.configs = append(w.configs, cf)
	}
}

// envOf extracts the environment from a config file name:
// config.yaml -> "", config.prod.yaml -> "prod".
func envOf(name string) string {
	base := strings.TrimSuffix(strings.TrimSuffix(name, ".yaml"), ".yml")
	if base == "config" {
		return ""
	}
	return strings.TrimPrefix(base, "config.")
}

// flatten walks a YAML mapping into dotted key paths.
//
// Sequences are recorded as a key and not descended into: no rule asks about
// the third element of a CORS origin list, and indexing into one would make the
// key space unbounded for no benefit.
func flatten(node *yaml.Node, prefix string, out map[string]ConfigKey) {
	if node == nil || node.Kind != yaml.MappingNode {
		return
	}
	for i := 0; i+1 < len(node.Content); i += 2 {
		keyNode, valNode := node.Content[i], node.Content[i+1]
		if keyNode.Kind != yaml.ScalarNode {
			continue
		}
		path := keyNode.Value
		if prefix != "" {
			path = prefix + "." + keyNode.Value
		}

		entry := ConfigKey{
			Path: path,
			Line: keyNode.Line,
			Col:  keyNode.Column,
		}
		switch valNode.Kind {
		case yaml.ScalarNode:
			entry.Scalar = true
			entry.value = valNode.Value
			// A key with nothing after it parses as an empty scalar with the
			// null tag; a key with "" is an explicitly empty string. Both mean
			// "no value supplied".
			entry.Empty = valNode.Value == "" || valNode.Tag == "!!null"
		case yaml.MappingNode:
			flatten(valNode, path, out)
		}
		out[strings.ToLower(path)] = entry
	}
}

// Key looks up a dotted path, case-insensitively.
func (c *ConfigFile) Key(path string) (ConfigKey, bool) {
	k, ok := c.Keys[strings.ToLower(path)]
	return k, ok
}

// Has reports whether a dotted path is present.
func (c *ConfigFile) Has(path string) bool {
	_, ok := c.Keys[strings.ToLower(path)]
	return ok
}
