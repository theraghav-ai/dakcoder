package workspace

import (
	"os"
	"path/filepath"
	"testing"
)

func mkconfigs(t *testing.T, files map[string]string) *Workspace {
	t.Helper()
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "go.mod"), []byte("module pisapi\n\ngo 1.25.0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for rel, body := range files {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	ws, err := Load(root)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	return ws
}

func TestConfigFlatteningAndEnvNames(t *testing.T) {
	ws := mkconfigs(t, map[string]string{
		"configs/config.yaml": `appname: "svc"
db:
  QueryTimeoutLow: 2s
  nested:
    deep: value
server:
  cors:
    alloworigins:
      - "http://localhost:3000"
`,
		"configs/config.prod.yaml": "appname: \"svc\"\n",
		"configs/config.yml":       "appname: \"svc\"\n",
	})

	if len(ws.Configs()) != 3 {
		t.Fatalf("loaded %d config files, want 3", len(ws.Configs()))
	}

	base := ws.BaseConfig()
	if base == nil {
		t.Fatal("no base config found")
	}
	if base.Rel != "configs/config.yaml" {
		t.Errorf("base config is %s", base.Rel)
	}

	for _, want := range []string{"appname", "db.QueryTimeoutLow", "db.nested.deep", "server.cors.alloworigins"} {
		if !base.Has(want) {
			t.Errorf("key %q was not flattened out", want)
		}
	}
	// Sequences are recorded as a key and not descended into: no rule asks about
	// the third CORS origin, and indexing would make the key space unbounded.
	if base.Has("server.cors.alloworigins.0") {
		t.Error("sequence elements should not become keys")
	}

	envs := map[string]bool{}
	for _, c := range ws.Configs() {
		envs[c.Env] = true
	}
	if !envs[""] || !envs["prod"] {
		t.Errorf("environments = %v, want the base and prod", envs)
	}
}

// TestConfigKeysAreCaseInsensitive: viper folds key case, so a case-sensitive
// lookup would report keys as missing that the application resolves fine.
func TestConfigKeysAreCaseInsensitive(t *testing.T) {
	ws := mkconfigs(t, map[string]string{
		"configs/config.yaml": "db:\n  QueryTimeoutLow: 2s\n",
	})
	base := ws.BaseConfig()
	for _, spelling := range []string{"db.QueryTimeoutLow", "db.querytimeoutlow", "DB.QUERYTIMEOUTLOW"} {
		if !base.Has(spelling) {
			t.Errorf("%q was not found", spelling)
		}
	}
	// The original casing survives for messages.
	k, _ := base.Key("db.querytimeoutlow")
	if k.Path != "db.QueryTimeoutLow" {
		t.Errorf("Path = %q; messages should quote the key as it is written", k.Path)
	}
}

// TestConfigKeysCarryTheirLine: secrets-in-config points at the offending line,
// and a finding without a position is one a developer has to go hunting for.
func TestConfigKeysCarryTheirLine(t *testing.T) {
	ws := mkconfigs(t, map[string]string{
		"configs/config.yaml": "appname: svc\n\ndb:\n  username: postgres\n  password: hunter2\n",
	})
	k, ok := ws.BaseConfig().Key("db.password")
	if !ok {
		t.Fatal("db.password not found")
	}
	if k.Line != 5 {
		t.Errorf("line = %d, want 5", k.Line)
	}
	if k.Col == 0 {
		t.Error("no column recorded")
	}
}

// TestHasRealValueSeparatesSecretsFromTheStateWeWant is the distinction the
// whole secrets rule rests on: a blanked credential is the goal, not a finding.
func TestHasRealValueSeparatesSecretsFromTheStateWeWant(t *testing.T) {
	ws := mkconfigs(t, map[string]string{
		"configs/config.yaml": `real:
  a: hunter2
  b: "Cept@123"
  c: 12345
blank:
  d:
  e: ""
  f: ~
  g: null
placeholder:
  h: ${MINIO_SECRET_KEY}
  i: <your-access-key>
  j: CHANGEME
  k: TODO
  l: change_me
structural:
  m:
    n: value
`,
	})
	base := ws.BaseConfig()

	for _, key := range []string{"real.a", "real.b", "real.c"} {
		k, ok := base.Key(key)
		if !ok {
			t.Fatalf("%s missing", key)
		}
		if !k.HasRealValue() {
			t.Errorf("%s should count as a real value", key)
		}
	}
	for _, key := range []string{
		"blank.d", "blank.e", "blank.f", "blank.g",
		"placeholder.h", "placeholder.i", "placeholder.j", "placeholder.k", "placeholder.l",
	} {
		k, ok := base.Key(key)
		if !ok {
			t.Fatalf("%s missing", key)
		}
		if k.HasRealValue() {
			t.Errorf("%s is blank or a placeholder and must not count as a value", key)
		}
	}
	// A mapping is not a scalar and can never be a credential.
	if k, ok := base.Key("structural.m"); !ok || k.Scalar || k.HasRealValue() {
		t.Errorf("a nested mapping should not be a scalar with a value: %+v", k)
	}
}

// TestMalformedConfigIsRecordedNotFatal: the agent can leave a YAML file
// half-written, and refusing to load the workspace because of it would stop the
// Go rules running at exactly the moment they are wanted.
func TestMalformedConfigIsRecordedNotFatal(t *testing.T) {
	ws := mkconfigs(t, map[string]string{
		"configs/config.yaml":     "db:\n  password: [unclosed\n : : :\n",
		"configs/config.dev.yaml": "appname: svc\n",
	})
	if len(ws.Configs()) != 2 {
		t.Fatalf("loaded %d configs, want 2", len(ws.Configs()))
	}
	var broken int
	for _, c := range ws.Configs() {
		if c.ParseErr != nil {
			broken++
		}
	}
	if broken != 1 {
		t.Errorf("%d configs recorded a parse error, want 1", broken)
	}
}

func TestNoConfigsDirectoryIsNotAnError(t *testing.T) {
	ws := mkconfigs(t, map[string]string{})
	if len(ws.Configs()) != 0 {
		t.Errorf("got %d configs", len(ws.Configs()))
	}
	if ws.BaseConfig() != nil {
		t.Error("BaseConfig should be nil with no configs/ directory")
	}
}

// TestReadCountIsOnePerFile is finding S3 at the source: the frontend agent's
// repo_map read every file twice, once for a preview and once to count lines.
func TestReadCountIsOnePerFile(t *testing.T) {
	ws := mkconfigs(t, map[string]string{
		"handler/a.go":            "package handler\n",
		"handler/b.go":            "package handler\n",
		"repo/postgres/c.go":      "package repo\n",
		"configs/config.yaml":     "appname: svc\n",
		"configs/config.dev.yaml": "appname: svc\n",
	})
	// 3 Go files + 2 configs. go.mod is read separately and is not counted.
	if got, want := ws.ReadCount(), len(ws.Files)+len(ws.Configs()); got != want {
		t.Errorf("ReadCount = %d, want %d (one read per file considered)", got, want)
	}
}

// TestReferenceCorpusConfigsLoad exercises the real seven-file config set,
// which is where the nesting and comment styles actually vary.
func TestReferenceCorpusConfigsLoad(t *testing.T) {
	root, err := filepath.Abs(filepath.Join("..", "..", "..", "new-template"))
	if err != nil {
		t.Skipf("resolve corpus: %v", err)
	}
	if _, err := os.Stat(root); err != nil {
		t.Skip("new-template corpus not present; skipping")
	}
	ws, err := Load(root)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if len(ws.Configs()) != 7 {
		t.Errorf("loaded %d config files, want 7", len(ws.Configs()))
	}
	for _, c := range ws.Configs() {
		if c.ParseErr != nil {
			t.Errorf("%s failed to parse: %v", c.Rel, c.ParseErr)
		}
		if !c.Has("db.QueryTimeoutLow") {
			t.Errorf("%s does not declare db.QueryTimeoutLow", c.Rel)
		}
	}
	if !ws.BaseConfig().Has("swagger.generation.mode") {
		t.Error("the base config should declare swagger.generation.mode")
	}
}
