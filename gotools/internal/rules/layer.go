package rules

import (
	"go/ast"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// dataAccessPkgs are the packages that may only appear inside repo/.
var dataAccessPkgs = []string{
	"github.com/Masterminds/squirrel",
	"github.com/jackc/pgx",
	"gitlab.cept.gov.in/it-2.0-common/n-api-db",
	"gitlab.cept.gov.in/it-2.0-common/api-db",
	"database/sql",
}

// LayerSQLBoundary keeps every form of data access inside the repository layer.
//
// This is the single most important structural rule in the template. Once SQL
// leaks into a handler, the handler cannot be tested without a database, the
// query cannot be reused, and the layering argument is lost for good.
var LayerSQLBoundary = Rule{
	ID:       "layer-sql-boundary",
	Severity: SeverityError,
	Summary:  "SQL, Squirrel and pgx may only be used inside repo/",
	Citation: "skill.md §Repository Pattern; SOP.md §[handler].go (step 5, no gin.Context)",
	Check: func(p *Pass) {
		for _, f := range p.WS.Files {
			switch f.Layer {
			case workspace.LayerRepo, workspace.LayerTest:
				continue
			}
			path, spec, ok := f.ImportsAny(dataAccessPkgs...)
			if !ok {
				continue
			}
			p.At(f, spec).
				Fix("move the query into a repo/postgres method and call it from here").
				Report("data-access package %q imported in the %s layer; SQL belongs in repo/", path, f.Layer)
		}
	},
}

// LayerDTOBoundary stops the repository layer from depending on transport
// concerns, which is what creates import cycles and makes domain models
// unreusable.
var LayerDTOBoundary = Rule{
	ID:       "layer-dto-boundary",
	Severity: SeverityError,
	Summary:  "repo/ must not import handler/; domain must not carry HTTP or DTO types",
	Citation: "skill.md §Domain Model Pattern, §Response DTO Pattern",
	Check: func(p *Pass) {
		mod := p.WS.ModulePath
		for _, f := range p.WS.Files {
			switch f.Layer {
			case workspace.LayerRepo:
				if mod == "" {
					continue
				}
				for path, spec := range importSpecs(f) {
					if strings.HasPrefix(path, mod+"/handler") {
						p.At(f, spec).
							Fix("return domain.* from the repository; convert in handler/response via New*Response").
							Report("repo imports %q; the repository layer must not know about DTOs", path)
					}
				}
			case workspace.LayerDomain:
				for _, bad := range []string{"net/http", "github.com/gin-gonic/gin", "mime/multipart"} {
					if path, spec, ok := f.ImportsAny(bad); ok {
						p.At(f, spec).
							Fix("keep transport types in handler/; the domain model stays plain Go").
							Report("domain model imports %q; domain types must not carry HTTP concerns", path)
					}
				}
			}
		}
	},
}

// importSpecs maps import path to its AST node, so a rule can report an exact
// position rather than a file-level finding.
func importSpecs(f *workspace.File) map[string]*ast.ImportSpec {
	out := make(map[string]*ast.ImportSpec, len(f.AST.Imports))
	for _, im := range f.AST.Imports {
		out[strings.Trim(im.Path.Value, `"`)] = im
	}
	return out
}
