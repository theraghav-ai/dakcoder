package rules

import (
	"go/ast"
	"sort"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// credentialKeys are the config key names that hold a secret.
//
// Matched on the key, not on the value. Entropy scoring is deliberately not
// used: it fires on request ids, hashes and base64 fixtures, and a security
// rule that cries wolf is one people switch off. Every credential actually
// committed to the reference template — a MinIO access/secret pair, an Aadhaar
// client secret, a database password and a Redis password — is caught by the
// key name alone, so the cheap check is also the complete one here.
var credentialKeys = []string{
	"password", "passwd", "pwd",
	"secret", "secretkey", "clientsecret",
	"token", "accesstoken", "refreshtoken", "bearertoken",
	"apikey", "accesskey", "privatekey", "credential", "credentials",
	"passphrase", "salt", "signingkey", "encryptionkey",
}

// isCredentialKey reports whether the last segment of a dotted path names a
// secret. Underscores and hyphens are stripped so client_secret, clientSecret
// and CLIENT-SECRET all match one entry.
func isCredentialKey(path string) bool {
	last := path
	if i := strings.LastIndex(path, "."); i >= 0 {
		last = path[i+1:]
	}
	norm := strings.ToLower(strings.NewReplacer("_", "", "-", "", " ", "").Replace(last))
	for _, k := range credentialKeys {
		if norm == k || strings.HasSuffix(norm, k) {
			return true
		}
	}
	return false
}

// SecretsInConfig refuses newly added credentials in configs/ and reports
// pre-existing ones as an advisory.
//
// Severity depends on who put the value there, which is a distinction the rule
// can make because the caller tells it which files it just changed:
//
//   - the agent edited this config in this run → error, and the write is refused
//   - the value was already committed          → warning, once, never echoed
//
// The reference template ships live-looking credentials in both config.yaml and
// config.prod.yaml. Blocking on those would make the linter unusable on the very
// template it enforces, and the fix — rotating them — is not the agent's to
// make (plan.md §9 Q7). Reporting them is still worth doing: a developer who has
// never opened those files should learn they are there.
//
// The value is never included in the message. That is the whole reason
// ConfigKey keeps it unexported: plan.md §17 requires committed credentials are
// never echoed into a prompt, a log, a trace or a diff, and a rule that quoted
// the offending value would put it into all four at once.
var SecretsInConfig = Rule{
	ID:       "secrets-in-config",
	Severity: SeverityError,
	Summary:  "no literal credentials in configs/*.yaml",
	Citation: "plan.md §6, §17; new-template/configs/*.yaml",
	Check: func(p *Pass) {
		for _, cf := range p.WS.Configs() {
			if cf.ParseErr != nil {
				continue
			}
			for _, key := range sortedKeys(cf.Keys) {
				entry := cf.Keys[key]
				if !isCredentialKey(entry.Path) || !entry.HasRealValue() {
					continue
				}
				f := p.AtPath(cf.Rel, entry.Line).
					Fix("blank it and supply the value from your deployment's secret store at run time")
				if p.Touched(cf.Rel) {
					f.Report("%s sets a literal credential; it must not be committed", entry.Path)
					continue
				}
				f.Severity(SeverityWarning).
					Report("%s carries a committed credential (pre-existing — rotate it and blank the file; the agent will not echo the value)", entry.Path)
			}
		}
	},
}

// configGetters are the *config.Config lookups whose first argument is a key
// path. They all resolve through viper, which folds case.
var configGetters = map[string]bool{
	"GetString": true, "GetInt": true, "GetInt32": true, "GetInt64": true,
	"GetUint": true, "GetUint32": true, "GetUint64": true,
	"GetBool": true, "GetFloat64": true, "GetDuration": true, "GetTime": true,
	"GetStringSlice": true, "GetIntSlice": true, "GetStringMap": true,
	"GetStringMapString": true, "GetSizeInBytes": true, "Get": true,
}

// ConfigKeyExists checks that every key the code reads is actually declared.
//
// A missing key does not fail: viper returns the zero value, so a repository
// asking for an absent `db.QueryTimeoutLow` gets a 0s deadline and every query
// it wraps fails immediately with `context deadline exceeded`. The error names
// the context, not the config, so the trail back to a typo or a casing mismatch
// is a long one. That failure is §13.2's "config key returns the zero value",
// and it is entirely mechanical to catch.
//
// A key missing from *some* environments is the more interesting case, and the
// one this rule exists for: it is how a service works in dev and dies in
// production. Those are reported separately, and as an error, because a key
// present in the base config and absent from config.prod.yaml is a defect
// nobody discovers until the worst possible moment.
var ConfigKeyExists = Rule{
	ID:       "config-key-exists",
	Severity: SeverityError,
	Summary:  "every cfg.Get*(\"key\") is declared in configs/*.yaml",
	Citation: "skill.md §Configuration Files; plan.md §13.2",
	Check: func(p *Pass) {
		configs := p.WS.Configs()
		if len(configs) == 0 {
			return // not a template service, or configs/ is absent
		}

		for _, f := range p.WS.Files {
			if f.Layer == workspace.LayerTest {
				continue
			}
			ast.Inspect(f.AST, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok || len(call.Args) == 0 {
					return true
				}
				sel, ok := call.Fun.(*ast.SelectorExpr)
				if !ok || !configGetters[sel.Sel.Name] {
					return true
				}
				key, ok := stringLit(call.Args[0])
				if !ok || key == "" {
					// A computed key cannot be checked, and guessing at one
					// would produce a violation the developer cannot act on.
					return true
				}

				var missing []string
				for _, cf := range configs {
					if cf.ParseErr != nil {
						continue
					}
					if !cf.Has(key) {
						missing = append(missing, cf.Rel)
					}
				}
				switch {
				case len(missing) == 0:
				case len(missing) == len(configs):
					p.At(f, call.Args[0]).
						Fix("add %q to configs/config.yaml, or correct the path (casing is not significant, but the segments are)", key).
						Report("config key %q is not declared in any config file; the lookup returns the zero value", key)
				default:
					p.At(f, call.Args[0]).
						Fix("add %q to %s", key, strings.Join(missing, ", ")).
						Report("config key %q is missing from %d of %d config files (%s); the lookup returns the zero value in those environments",
							key, len(missing), len(configs), strings.Join(missing, ", "))
				}
				return true
			})
		}
	},
}

// swaggerModeKey gates whether the framework emits the OpenAPI document.
const swaggerModeKey = "swagger.generation.mode"

// SwaggerVisible checks that generated API documentation is switched on.
//
// Route names are checked by routes-in-handler; this rule covers the other half,
// which is a config key rather than code. With `swagger.generation.mode` unset
// the framework generates nothing, so /docs/v3Doc.json is empty and every route
// is missing from it while serving perfectly — a failure with no error attached,
// discovered by whoever tries to generate a client SDK.
//
// The base config gates. Environment files are reported as a warning only: the
// reference template declares the key in config.yaml and in none of the six
// environment files, and whether that is deliberate is a question for the
// template owner rather than something to fail a build over.
var SwaggerVisible = Rule{
	ID:       "swagger-visible",
	Severity: SeverityError,
	Summary:  "swagger.generation.mode is set, so routes reach /docs/v3Doc.json",
	Citation: "SOP.md §Running the application and Swagger Docs; configs/config.yaml",
	Check: func(p *Pass) {
		configs := p.WS.Configs()
		if len(configs) == 0 {
			return
		}
		base := p.WS.BaseConfig()
		if base == nil {
			p.AtPath(workspace.ConfigDir+"/config.yaml", 1).
				Fix("add configs/config.yaml declaring swagger.generation.mode: \"build\"").
				Report("no base configs/config.yaml; the framework has no default configuration to read")
			return
		}
		if key, ok := base.Key(swaggerModeKey); !ok || !key.HasRealValue() {
			p.AtPath(base.Rel, keyLine(base, "swagger", 1)).
				Fix(`add swagger: {generation: {mode: "build"}} — the framework generates the document, there is no CLI`).
				Report("%s is not set; no OpenAPI document is generated and every route is missing from /docs/v3Doc.json", swaggerModeKey)
		}

		var missing []string
		for _, cf := range configs {
			if cf.Env == "" || cf.ParseErr != nil {
				continue
			}
			if key, ok := cf.Key(swaggerModeKey); !ok || !key.HasRealValue() {
				missing = append(missing, cf.Env)
			}
		}
		if len(missing) > 0 {
			p.AtPath(base.Rel, 1).
				Severity(SeverityWarning).
				Fix("declare swagger.generation.mode in each environment file, or confirm with the template owner that documentation is base-config only").
				Report("%s is declared in the base config but not in %s; those environments generate no OpenAPI document",
					swaggerModeKey, strings.Join(missing, ", "))
		}
	},
}

// keyLine returns a config key's line, or a fallback when it is absent.
func keyLine(cf *workspace.ConfigFile, path string, fallback int) int {
	if k, ok := cf.Key(path); ok {
		return k.Line
	}
	return fallback
}

// stringLit renders a basic string literal, reporting false for anything else.
func stringLit(e ast.Expr) (string, bool) {
	lit, ok := e.(*ast.BasicLit)
	if !ok || lit.Kind.String() != "STRING" {
		return "", false
	}
	v := lit.Value
	if len(v) >= 2 && (v[0] == '"' || v[0] == '`') {
		return v[1 : len(v)-1], true
	}
	return "", false
}

func sortedKeys(m map[string]workspace.ConfigKey) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
