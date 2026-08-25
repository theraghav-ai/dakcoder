package scaffold

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/fxwire"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/gopatch"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/spec"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// Import paths the generated code depends on. Written here rather than in the
// templates so the dependency surface of the whole scaffolder is one readable
// list — which is also the list dep-allowlist is checked against.
const (
	pkgSquirrel      = "github.com/Masterminds/squirrel"
	pkgPgx           = "github.com/jackc/pgx/v5"
	pkgConfig        = "gitlab.cept.gov.in/it-2.0-common/api-config"
	pkgDBLib         = "gitlab.cept.gov.in/it-2.0-common/n-api-db"
	pkgLog           = "gitlab.cept.gov.in/it-2.0-common/n-api-log"
	pkgServerHandler = "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
	pkgServerRoute   = "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"
	pkgBootstrapper  = "gitlab.cept.gov.in/it-2.0-common/n-api-bootstrapper"
	pkgFx            = "go.uber.org/fx"
)

// Workspace-relative paths the resource scaffolder touches.
const (
	requestFile = "handler/request.go"
)

// ResourceOptions configures a resource scaffold.
type ResourceOptions struct {
	// Module overrides the module path. Empty reads it from go.mod, which is
	// what the tool does in practice; the override exists for tests and for
	// scaffolding into a tree that is not yet a module.
	Module string

	// SkipFXWire suppresses the bootstrap/bootstrapper.go edit.
	//
	// Only the greenfield path sets it: project_scaffold emits a bootstrapper
	// already wired to its seed resource, and there is nothing on disk to patch
	// because nothing has been written yet.
	SkipFXWire bool
}

// Resource renders a complete CRUD resource against an existing service.
//
// It produces six new files and two modifications:
//
//	create  core/domain/<name>.go
//	create  db/<plural>.sql
//	create  repo/postgres/<name>.go
//	create  handler/response/<name>.go
//	create  handler/<name>.go
//	modify  handler/request.go              (DTOs appended)
//	modify  bootstrap/bootstrapper.go       (FX registration, via fxwire)
//
// The spec is normalised and validated first; nothing is rendered from an
// invalid spec, so a bad field type is one clear error rather than six files
// that do not compile.
func Resource(root string, s spec.Resource, opts ResourceOptions) (*Result, error) {
	normalised, err := s.Normalise()
	if err != nil {
		return nil, err
	}

	module := opts.Module
	if module == "" {
		module, err = workspace.ModulePath(root)
		if err != nil {
			return nil, err
		}
	}

	res := &Result{Module: module}
	d := data{R: normalised, Module: module, Layout: spec.TimestampLayout}

	// core/domain/<name>.go
	d.Imports = renderImports(module, []imp{{Path: "time"}})
	content, err := renderGo("domain.go.tmpl", d)
	if err != nil {
		return nil, err
	}
	res.add("core/domain/"+normalised.FileStem()+".go", ActionCreate, content)

	// db/<plural>.sql
	sqlContent, err := render("table.sql.tmpl", d)
	if err != nil {
		return nil, err
	}
	res.add("db/"+normalised.TableFileStem()+".sql", ActionCreate, sqlContent)

	// repo/postgres/<name>.go
	repoImports := []imp{
		{Path: "context"},
		{Path: module + "/core/domain"},
		{Alias: "config", Path: pkgConfig},
		{Alias: "dblib", Path: pkgDBLib},
	}
	if normalised.NeedsTimeInRepo() {
		repoImports = append(repoImports, imp{Path: "time"})
	}
	if normalised.NeedsSquirrel() {
		repoImports = append(repoImports, imp{Alias: "sq", Path: pkgSquirrel})
	}
	if normalised.NeedsPgx() {
		repoImports = append(repoImports, imp{Path: pkgPgx})
	}
	d.Imports = renderImports(module, repoImports)
	content, err = renderGo("repository.go.tmpl", d)
	if err != nil {
		return nil, err
	}
	res.add("repo/postgres/"+normalised.FileStem()+".go", ActionCreate, content)

	// handler/response/<name>.go
	d.Imports = renderImports(module, []imp{
		{Path: module + "/core/domain"},
		{Path: module + "/core/port"},
	})
	content, err = renderGo("response.go.tmpl", d)
	if err != nil {
		return nil, err
	}
	res.add("handler/response/"+normalised.FileStem()+".go", ActionCreate, content)

	// handler/<name>.go
	handlerImports := []imp{
		{Path: module + "/core/port"},
		{Alias: "resp", Path: module + "/handler/response"},
		{Alias: "repo", Path: module + "/repo/postgres"},
		{Alias: "log", Path: pkgLog},
		{Alias: "serverHandler", Path: pkgServerHandler},
		{Alias: "serverRoute", Path: pkgServerRoute},
	}
	if normalised.NeedsTimeInHandler() {
		handlerImports = append(handlerImports, imp{Path: "time"})
	}
	d.Imports = renderImports(module, handlerImports)
	content, err = renderGo("handler.go.tmpl", d)
	if err != nil {
		return nil, err
	}
	res.add("handler/"+normalised.FileStem()+".go", ActionCreate, content)

	// handler/request.go — appended to, or created.
	requestContent, err := patchRequestFile(root, module, normalised, d)
	if err != nil {
		return nil, err
	}
	action := ActionModify
	if _, statErr := os.Stat(filepath.Join(root, filepath.FromSlash(requestFile))); os.IsNotExist(statErr) {
		action = ActionCreate
	}
	res.add(requestFile, action, requestContent)

	// bootstrap/bootstrapper.go — FX registration.
	if !opts.SkipFXWire {
		wire, werr := fxwire.Plan(root,
			module,
			fxwire.Registration{Kind: fxwire.KindRepo, Ctor: "New" + normalised.RepoType()},
			fxwire.Registration{Kind: fxwire.KindHandler, Ctor: "New" + normalised.HandlerType()},
		)
		if werr != nil {
			return nil, fmt.Errorf("fx wiring: %w", werr)
		}
		switch {
		case wire.Changed:
			res.add(wire.Path, ActionModify, wire.Content)
		case len(wire.AlreadyRegistered) > 0:
			res.note("%s already registers %s — left unchanged",
				wire.Path, strings.Join(wire.AlreadyRegistered, " and "))
		}
	}

	addResourceNotes(res, normalised)
	return res, nil
}

// patchRequestFile appends the resource's DTOs to handler/request.go, creating
// the file if the service does not have one yet.
//
// Appending rather than rewriting is deliberate: request.go accumulates every
// resource's DTOs, so it is the one generated file a developer's own edits
// share space with. SOP.md §Validation requires all request structs to live
// here — govalid is run as `govalid ./request.go`, and a struct anywhere else
// silently never gets a validator, which means input reaches the handler
// unvalidated with nothing to show for it.
func patchRequestFile(root, module string, r spec.Resource, d data) (string, error) {
	decls, err := render("request.go.tmpl", d)
	if err != nil {
		return "", err
	}
	decls = strings.TrimSpace(decls)

	abs := filepath.Join(root, filepath.FromSlash(requestFile))
	existing, readErr := os.ReadFile(abs)
	if readErr != nil {
		if !os.IsNotExist(readErr) {
			return "", fmt.Errorf("read %s: %w", requestFile, readErr)
		}
		// New file.
		var imports []imp
		if r.Has("list") {
			imports = append(imports, imp{Path: module + "/core/port"})
		}
		if r.NeedsTimeInRequest() {
			imports = append(imports, imp{Path: "time"})
		}
		header := "package handler\n"
		if block := renderImports(module, imports); block != "" {
			header += "\n" + block + "\n"
		}
		out, ferr := gopatch.Format([]byte(header + "\n" + decls + "\n"))
		if ferr != nil {
			return "", fmt.Errorf("template request.go.tmpl: %w", ferr)
		}
		return string(out), nil
	}

	// Re-scaffolding must not append a second copy of every type.
	for _, name := range declaredTypeNames(r) {
		dup, derr := gopatch.HasDecl(existing, name)
		if derr != nil {
			return "", fmt.Errorf("%s: %w", requestFile, derr)
		}
		if dup {
			return "", fmt.Errorf(
				"%s already declares %s; the resource looks scaffolded already — remove the existing DTOs, or scaffold under a different name",
				requestFile, name)
		}
	}

	src := existing
	if r.Has("list") {
		src, _, err = gopatch.EnsureImport(src, "", module+"/core/port", module)
		if err != nil {
			return "", fmt.Errorf("%s: %w", requestFile, err)
		}
	}
	if r.NeedsTimeInRequest() {
		src, _, err = gopatch.EnsureImport(src, "", "time", module)
		if err != nil {
			return "", fmt.Errorf("%s: %w", requestFile, err)
		}
	}
	out, err := gopatch.AppendDecls(src, decls)
	if err != nil {
		return "", fmt.Errorf("%s: %w", requestFile, err)
	}
	return string(out), nil
}

// declaredTypeNames lists the types the resource contributes to request.go.
func declaredTypeNames(r spec.Resource) []string {
	var out []string
	if r.Has("create") {
		out = append(out, r.CreateReq())
	}
	if r.Has("update") {
		out = append(out, r.UpdateReq())
	}
	if r.NeedsIDUri() {
		out = append(out, r.IDUri())
	}
	if r.Has("list") {
		out = append(out, r.ListParams())
	}
	return out
}

// addResourceNotes records the steps the scaffolder deliberately does not take.
func addResourceNotes(res *Result, r spec.Resource) {
	res.note("run `govalid ./request.go` from handler/ — the framework returns 422 for any non-GET route whose request DTO has no generated Validate()")
	res.note("apply db/%s.sql to your database; the agent never runs DDL, because it is the one action git cannot undo", r.TableFileStem())
	res.note("verify with: go build ./... && gotools lint")
	if r.Has("create") || r.Has("update") {
		res.note("create and update use dblib.InsertReturning/UpdateReturning with a RETURNING clause, so the response carries the stored row rather than an echo of the request — this differs from the reference user resource, which returns id 0 on create")
	}
}
