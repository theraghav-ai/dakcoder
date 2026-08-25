package rules

import (
	"strings"
	"testing"
)

// The secret value used throughout these tests. It must never appear in a
// violation message, which is what TestCredentialValuesAreNeverEchoed asserts.
const testSecret = "s3cr3t-Cept@123-value"

func configWorkspace(t *testing.T, files map[string]string) string {
	t.Helper()
	if _, ok := files["handler/user.go"]; !ok {
		files["handler/user.go"] = goodHandler
	}
	return mkws(t, files)
}

func TestSecretsInConfigDetectsCommittedCredentials(t *testing.T) {
	root := configWorkspace(t, map[string]string{
		"configs/config.yaml": `
db:
  username: postgres
  password: ` + testSecret + `
minio:
  accessKey: "AKIAIOSFODNN7EXAMPLE"
  secretKey: ""
cache:
  redispassword:
AADHAAR_CLIENT_SECRET: ` + testSecret + `
server:
  addr: ":8080"
`,
	})
	res, err := Analyze(root, RunOptions{Only: []string{"secrets-in-config"}})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	all := append(append([]Violation{}, res.Violations...), res.Warnings...)

	found := map[string]bool{}
	for _, v := range all {
		found[v.Message] = true
	}
	for _, want := range []string{"db.password", "minio.accessKey", "AADHAAR_CLIENT_SECRET"} {
		hit := false
		for _, v := range all {
			if strings.Contains(v.Message, want) {
				hit = true
			}
		}
		if !hit {
			t.Errorf("%s was not reported", want)
		}
	}
	// A blank credential key is the desired state, not a finding, and neither is
	// a non-credential key that happens to have a value.
	for _, v := range all {
		if strings.Contains(v.Message, "secretKey") {
			t.Errorf("an empty credential key was reported: %s", v.Message)
		}
		if strings.Contains(v.Message, "redispassword") {
			t.Errorf("a valueless credential key was reported: %s", v.Message)
		}
		if strings.Contains(v.Message, "username") || strings.Contains(v.Message, "addr") {
			t.Errorf("a non-credential key was reported: %s", v.Message)
		}
	}
}

// TestCredentialValuesAreNeverEchoed is the security property of the rule.
//
// plan.md §17 requires that committed credentials are never echoed into a
// prompt, a log, a trace or a diff. A violation message goes into all four at
// once, so the value has to be unreachable — which is why ConfigKey keeps it
// unexported and exposes only HasRealValue.
func TestCredentialValuesAreNeverEchoed(t *testing.T) {
	root := configWorkspace(t, map[string]string{
		"configs/config.yaml": "db:\n  password: " + testSecret + "\nminio:\n  secretKey: \"" + testSecret + "\"\n",
	})
	res, err := Analyze(root, RunOptions{Only: []string{"secrets-in-config"}})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	all := append(append([]Violation{}, res.Violations...), res.Warnings...)
	if len(all) != 2 {
		t.Fatalf("got %d findings, want 2", len(all))
	}
	for _, v := range all {
		for _, field := range []string{v.Message, v.Fix, v.Citation, v.String()} {
			if strings.Contains(field, testSecret) {
				t.Errorf("a credential value reached the violation: %s", field)
			}
		}
	}
}

// TestSecretSeverityFollowsWhoWroteIt: a credential the agent just added has to
// block; one that was already committed is somebody else's rotation problem and
// must not fail every lint until they get to it.
func TestSecretSeverityFollowsWhoWroteIt(t *testing.T) {
	files := map[string]string{
		"configs/config.yaml":     "db:\n  password: " + testSecret + "\n",
		"configs/config.dev.yaml": "db:\n  password: " + testSecret + "\n",
	}
	root := mkws(t, files)

	unscoped, err := Analyze(root, RunOptions{Only: []string{"secrets-in-config"}})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	if !unscoped.OK {
		t.Errorf("an unscoped run should not block on pre-existing credentials; got %d", unscoped.Count)
	}
	if len(unscoped.Warnings) != 2 {
		t.Errorf("got %d warnings, want 2", len(unscoped.Warnings))
	}

	scoped, err := Analyze(root, RunOptions{
		Only:  []string{"secrets-in-config"},
		Scope: []string{"configs/config.dev.yaml"},
	})
	if err != nil {
		t.Fatalf("analyze scoped: %v", err)
	}
	if scoped.Count != 1 {
		t.Fatalf("got %d blocking violations, want 1 (the touched file)", scoped.Count)
	}
	if scoped.Violations[0].Path != "configs/config.dev.yaml" {
		t.Errorf("blocking violation is on %s", scoped.Violations[0].Path)
	}
	if len(scoped.Warnings) != 1 {
		t.Errorf("the untouched file should stay advisory; got %d warnings", len(scoped.Warnings))
	}
}

// TestPlaceholdersAreNotSecrets: a config with its credentials stripped is the
// state we are asking people to reach, so it must not be flagged on arrival.
func TestPlaceholdersAreNotSecrets(t *testing.T) {
	root := configWorkspace(t, map[string]string{
		"configs/config.yaml": `
db:
  password:
  passwd: ""
minio:
  secretKey: ${MINIO_SECRET_KEY}
  accessKey: <your-access-key>
auth:
  token: CHANGEME
  apikey: TODO
`,
	})
	res, err := Analyze(root, RunOptions{Only: []string{"secrets-in-config"}})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	all := append(append([]Violation{}, res.Violations...), res.Warnings...)
	if len(all) != 0 {
		for _, v := range all {
			t.Errorf("placeholder reported as a secret: %s", v)
		}
	}
}

func TestConfigKeyExists(t *testing.T) {
	repoSrc := `package repo

import (
	"context"

	config "gitlab.cept.gov.in/it-2.0-common/api-config"
)

type XRepository struct{ cfg *config.Config }

func (r *XRepository) A(ctx context.Context) {
	_ = r.cfg.GetDuration("db.QueryTimeoutLow")
	_ = r.cfg.GetString("db.Missing")
	_ = r.cfg.GetString("client.baseurl")
	_ = r.cfg.GetString(someVariable)
}

var someVariable = "db.QueryTimeoutLow"
`
	root := mkws(t, map[string]string{
		"repo/postgres/x.go": repoSrc,
		"configs/config.yaml": `
db:
  QueryTimeoutLow: 2s
client:
  baseurl: "http://localhost:8080"
`,
		"configs/config.prod.yaml": `
db:
  QueryTimeoutLow: 2s
`,
	})
	res, err := Analyze(root, RunOptions{Only: []string{"config-key-exists"}})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	all := append(append([]Violation{}, res.Violations...), res.Warnings...)

	var missingEverywhere, missingSomewhere int
	for _, v := range all {
		switch {
		case strings.Contains(v.Message, `"db.Missing"`):
			missingEverywhere++
		case strings.Contains(v.Message, `"client.baseurl"`):
			missingSomewhere++
			if !strings.Contains(v.Message, "config.prod.yaml") {
				t.Errorf("the finding should name the environments that lack the key: %s", v.Message)
			}
		default:
			t.Errorf("unexpected finding: %s", v)
		}
	}
	if missingEverywhere != 1 {
		t.Errorf("a key absent from every config should be reported once; got %d", missingEverywhere)
	}
	if missingSomewhere != 1 {
		t.Errorf("a key present in the base but missing from prod is the interesting case; got %d", missingSomewhere)
	}
}

// TestConfigKeyLookupIsCaseInsensitive: viper folds key case, so a
// case-sensitive comparison would report keys as missing that the application
// resolves perfectly well.
func TestConfigKeyLookupIsCaseInsensitive(t *testing.T) {
	root := mkws(t, map[string]string{
		"repo/postgres/x.go": `package repo

import config "gitlab.cept.gov.in/it-2.0-common/api-config"

type XRepository struct{ cfg *config.Config }

func (r *XRepository) A() { _ = r.cfg.GetDuration("db.querytimeoutlow") }
`,
		"configs/config.yaml": "db:\n  QueryTimeoutLow: 2s\n",
	})
	res, err := Analyze(root, RunOptions{Only: []string{"config-key-exists"}})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	if !res.OK || len(res.Warnings) > 0 {
		t.Errorf("case should not matter: %v %v", res.Violations, res.Warnings)
	}
}

func TestConfigKeyExistsNoOpsWithoutConfigs(t *testing.T) {
	root := mkws(t, map[string]string{
		"repo/postgres/x.go": `package repo

import config "gitlab.cept.gov.in/it-2.0-common/api-config"

type XRepository struct{ cfg *config.Config }

func (r *XRepository) A() { _ = r.cfg.GetString("anything.at.all") }
`,
	})
	res, err := Analyze(root, RunOptions{Only: []string{"config-key-exists"}})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	if !res.OK {
		t.Errorf("with no configs/ there is nothing to check against; got %d violations", res.Count)
	}
}

func TestSwaggerVisible(t *testing.T) {
	t.Run("missing from the base config blocks", func(t *testing.T) {
		root := configWorkspace(t, map[string]string{
			"configs/config.yaml": "appname: x\n",
		})
		res, err := Analyze(root, RunOptions{Only: []string{"swagger-visible"}})
		if err != nil {
			t.Fatalf("analyze: %v", err)
		}
		if res.Count != 1 {
			t.Fatalf("got %d blocking violations, want 1", res.Count)
		}
		if !strings.Contains(res.Violations[0].Message, "v3Doc.json") {
			t.Errorf("the message should say what breaks: %s", res.Violations[0].Message)
		}
	})

	t.Run("missing from an environment is advisory", func(t *testing.T) {
		root := configWorkspace(t, map[string]string{
			"configs/config.yaml":      "swagger:\n  generation:\n    mode: \"build\"\n",
			"configs/config.prod.yaml": "appname: x\n",
		})
		res, err := Analyze(root, RunOptions{Only: []string{"swagger-visible"}})
		if err != nil {
			t.Fatalf("analyze: %v", err)
		}
		if !res.OK {
			t.Errorf("an environment gap should not block; got %d", res.Count)
		}
		if len(res.Warnings) != 1 || !strings.Contains(res.Warnings[0].Message, "prod") {
			t.Errorf("expected one warning naming prod, got %v", res.Warnings)
		}
	})

	t.Run("fully configured is silent", func(t *testing.T) {
		cfg := "swagger:\n  generation:\n    mode: \"build\"\n"
		root := configWorkspace(t, map[string]string{
			"configs/config.yaml":      cfg,
			"configs/config.prod.yaml": cfg,
		})
		res, err := Analyze(root, RunOptions{Only: []string{"swagger-visible"}})
		if err != nil {
			t.Fatalf("analyze: %v", err)
		}
		if !res.OK || len(res.Warnings) > 0 {
			t.Errorf("nothing to report: %v %v", res.Violations, res.Warnings)
		}
	})
}

// TestMalformedConfigDoesNotAbortTheLint: the agent can legitimately leave a
// YAML file half-written, and refusing to lint the Go code because of it would
// be exactly the wrong trade.
func TestMalformedConfigDoesNotAbortTheLint(t *testing.T) {
	root := configWorkspace(t, map[string]string{
		"configs/config.yaml": "db:\n  password: [unclosed\n   : : :\n",
	})
	res, err := Analyze(root, RunOptions{})
	if err != nil {
		t.Fatalf("a malformed config must not fail the run: %v", err)
	}
	_ = res
}
