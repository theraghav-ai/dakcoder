package rules

import (
	"go/ast"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// dblibExec are the dblib entry points that execute a built query.
var dblibExec = map[string]bool{
	"dblib.Insert": true, "dblib.SelectOne": true, "dblib.SelectRows": true,
	"dblib.Update": true, "dblib.Delete": true, "dblib.Exec": true,
}

// RepoContract enforces the three properties every repository method must have:
// queries built through dblib.Psql, a context deadline from config, and the
// dependencies injected rather than reached for.
//
// The dblib.Psql requirement matters more than it looks. dblib.Psql is a
// Squirrel builder pre-configured with dollar placeholders; a hand-rolled
// sq.Insert(...) defaults to `?` placeholders, which Postgres rejects at
// runtime rather than compile time. The v1 plan asserted the opposite rule
// (`add .PlaceholderFormat(sq.Dollar)`) — the reference template does not do
// that anywhere, and copying it would produce technically-working but
// off-template code.
var RepoContract = Rule{
	ID:       "repo-contract",
	Severity: SeverityError,
	Summary:  "repositories use dblib.Psql, wrap queries in a config-driven timeout, and inject *dblib.DB + *config.Config",
	Citation: "skill.md §Repository Pattern",
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerRepo) {
			// Constructor must inject both dependencies.
			funcsIn(f, func(fd *ast.FuncDecl) {
				if _, isMethod := receiverType(fd); isMethod {
					return
				}
				if !strings.HasPrefix(fd.Name.Name, "New") {
					return
				}
				rs := results(fd)
				if len(rs) == 0 || !strings.HasSuffix(rs[0], "Repository") {
					return
				}
				ps := params(fd)
				hasDB, hasCfg := false, false
				for _, t := range ps {
					switch {
					case strings.HasSuffix(t, "dblib.DB"):
						hasDB = true
					case strings.HasSuffix(t, "config.Config"):
						hasCfg = true
					}
				}
				if !hasDB || !hasCfg {
					p.At(f, fd).
						Fix("func New%s(db *dblib.DB, cfg *config.Config) *%s", strings.TrimPrefix(fd.Name.Name, "New"), strings.TrimPrefix(fd.Name.Name, "New")).
						Report("%s must inject *dblib.DB and *config.Config (has db=%v cfg=%v)", fd.Name.Name, hasDB, hasCfg)
				}
			})

			// Raw Squirrel builders bypass the dollar-placeholder configuration.
			ast.Inspect(f.AST, func(n ast.Node) bool {
				sel, ok := n.(*ast.SelectorExpr)
				if !ok {
					return true
				}
				id, ok := sel.X.(*ast.Ident)
				if !ok || id.Name != "sq" {
					return true
				}
				switch sel.Sel.Name {
				case "Insert", "Select", "Update", "Delete", "StatementBuilder":
					p.At(f, sel).
						Fix("use dblib.Psql.%s — it is pre-configured with $N placeholders", sel.Sel.Name).
						Report("query built with raw sq.%s; Squirrel defaults to ? placeholders, which Postgres rejects", sel.Sel.Name)
				}
				return true
			})

			// Every method that executes a query must first take a deadline.
			funcsIn(f, func(fd *ast.FuncDecl) {
				if fd.Body == nil {
					return
				}
				if _, isMethod := receiverType(fd); !isMethod {
					return
				}
				executes := false
				ast.Inspect(fd.Body, func(n ast.Node) bool {
					if c, ok := n.(*ast.CallExpr); ok && dblibExec[callName(c)] {
						executes = true
						return false
					}
					return true
				})
				if !executes {
					return
				}
				if !anyCall(fd.Body, "context.WithTimeout") {
					p.At(f, fd).
						Fix(`ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutLow")); defer cancel()`).
						Report("%s executes a query without context.WithTimeout; a slow query would hang the request", fd.Name.Name)
					return
				}
				// The deadline must come from config, not a literal.
				if !callsMatching(fd.Body, "GetDuration") {
					p.At(f, fd).
						Fix(`take the deadline from r.cfg.GetDuration("db.QueryTimeoutLow"|"...Med")`).
						Report("%s hard-codes its query timeout; it must come from config", fd.Name.Name)
				}
			})
		}
	},
}

// dblibMappedCalls are the dblib entry points whose fourth argument is a
// pgx.RowToFunc. All of them collect rows, so all of them need a mapper.
var dblibMappedCalls = map[string]bool{
	"dblib.SelectOne": true, "dblib.SelectRows": true,
	"dblib.SelectOneOK": true, "dblib.SelectRowsOK": true,
	"dblib.InsertReturning": true, "dblib.UpdateReturning": true,
	"dblib.InsertReturningrows": true,
}

// safeRowMappers are the pgx mappers that bind by name or read a scalar.
//
// RowToStructByPos is deliberately absent: it binds by column position, so
// adding a column to the SELECT silently shifts every field after it. That is a
// data-corruption bug with no error attached, and it is precisely the class of
// mistake db tags exist to prevent.
var safeRowMappers = []string{
	"RowToStructByName",
	"RowToAddrOfStructByName",
	"RowToStructByNameLax",
	"RowTo", // scalar reads, e.g. pgx.RowTo[int] for a COUNT(*)
}

// RepoRowMapper requires a by-name row mapper on every call that collects rows.
var RepoRowMapper = Rule{
	ID:       "repo-rowmapper",
	Severity: SeverityError,
	Summary:  "dblib calls that collect rows map them by name, not by position",
	Citation: "skill.md §Repository Pattern",
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerRepo) {
			ast.Inspect(f.AST, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				name := callName(call)
				if !dblibMappedCalls[name] {
					return true
				}
				// Signature is (ctx, db, query, mapper).
				if len(call.Args) < 4 {
					p.At(f, call).
						Fix("pass pgx.RowToStructByName[domain.X] as the final argument").
						Report("%s called with %d argument(s); a row mapper is required", name, len(call.Args))
					return false
				}
				mapper := typeString(call.Args[3])
				if !isSafeRowMapper(mapper) {
					p.At(f, call).
						Fix("use pgx.RowToStructByName[domain.X] so db tags drive the mapping (or pgx.RowTo[T] for a scalar)").
						Report("%s uses row mapper %q; want a by-name mapper", name, mapper)
				}
				return false
			})
		}
	},
}

// isSafeRowMapper reports whether a rendered mapper expression names one of the
// by-name or scalar pgx mappers.
func isSafeRowMapper(mapper string) bool {
	for _, safe := range safeRowMappers {
		// Suffix-matched on the selector so an aliased pgx import still passes,
		// and anchored on "." or the start so RowToStructByPos cannot match
		// RowTo by prefix.
		if mapper == safe || strings.HasSuffix(mapper, "."+safe) {
			return true
		}
		if strings.HasPrefix(mapper, safe+"[") || strings.Contains(mapper, "."+safe+"[") {
			return true
		}
	}
	return false
}

// RepoNoRows requires that a write affecting zero rows surfaces as
// pgx.ErrNoRows, which is what the handler layer translates into a 404. Without
// it, updating a non-existent record returns 200 and the caller never learns.
var RepoNoRows = Rule{
	ID:       "repo-norows",
	Severity: SeverityError,
	Summary:  "Update/Delete map RowsAffected()==0 to pgx.ErrNoRows",
	Citation: "skill.md §Repository Pattern, §Error Handling; SOP.md §[handler].go (step 8, error response order)",
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerRepo) {
			funcsIn(f, func(fd *ast.FuncDecl) {
				if fd.Body == nil {
					return
				}
				writes := false
				ast.Inspect(fd.Body, func(n ast.Node) bool {
					if c, ok := n.(*ast.CallExpr); ok {
						if name := callName(c); name == "dblib.Update" || name == "dblib.Delete" {
							writes = true
							return false
						}
					}
					return true
				})
				if !writes {
					return
				}
				if !callsMatching(fd.Body, "RowsAffected") || !strings.Contains(f.Text(fd.Body), "pgx.ErrNoRows") {
					p.At(f, fd).
						Fix("if commandTag.RowsAffected() == 0 { return pgx.ErrNoRows }").
						Report("%s does not surface a zero-row write as pgx.ErrNoRows; the caller would see success", fd.Name.Name)
				}
			})
		}
	},
}
