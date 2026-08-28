package rules

import (
	"go/ast"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// The rules in this file come from the manual review of 41 production services
// (docs/CODE-REVIEW-FINDINGS.md). Database round trips were the single largest
// category in that review — 748 findings across 33 of the 41 services — and
// nothing in the original rule set looked at them.
//
// The endorsed batch shape, decided by the template owner (findings §10.1), is
// the drain loop:
//
//	batch := &pgx.Batch{}
//	// ... dblib.QueueExecRow(batch, builder) per statement ...
//	results := r.db.SendBatch(ctx, batch)
//	if results != nil {
//	    defer results.Close()
//	    for i := 0; i < batch.Len(); i++ {
//	        if _, err := results.Exec(); err != nil { return err }
//	    }
//	}
//
// It is the form that reports *which* statement failed rather than merely that
// something did, and it is what 8 of the 15 existing batch sites in the legacy
// corpus already do. `dblib.TimedBatch` exists in both libraries and is
// deliberately not adopted; if that changes, the fix strings here are the only
// thing to update.

// batchFix is the one-line remedy shared by every rule that wants a batch. Kept
// in one place so the rules cannot drift from each other or from the knowledge
// base.
const batchFix = "queue with dblib.QueueExecRow(batch, b), then send once: " +
	"results := r.db.SendBatch(ctx, batch); defer results.Close(); drain with results.Exec()"

const dbPerfCitation = "docs/CODE-REVIEW-FINDINGS.md; references/db-performance.md"

// isDBExec reports whether a call executes a statement against the database.
func isDBExec(c *ast.CallExpr) bool { return dblibExec[callName(c)] }

// usesBatch reports whether a function body builds or sends a pgx batch.
func usesBatch(n ast.Node) bool {
	found := false
	ast.Inspect(n, func(x ast.Node) bool {
		if found {
			return false
		}
		switch t := x.(type) {
		case *ast.CompositeLit:
			if typeString(t.Type) == "pgx.Batch" {
				found = true
			}
		case *ast.CallExpr:
			name := callName(t)
			if methodNamed(name, "SendBatch") || strings.HasPrefix(name, "dblib.Queue") ||
				strings.HasPrefix(name, "dblib.TimedQueue") || name == "dblib.NewTimedBatch" {
				found = true
			}
		}
		return !found
	})
	return found
}

// usesTransaction reports whether a function opens a transaction.
func usesTransaction(n ast.Node) bool {
	found := false
	ast.Inspect(n, func(x ast.Node) bool {
		if found {
			return false
		}
		if c, ok := x.(*ast.CallExpr); ok {
			if methodNamed(callName(c), "Begin", "BeginTx") || callName(c) == "dblib.Tx" {
				found = true
			}
		}
		return !found
	})
	return found
}

// repoMethods yields each repository method with a body, skipping constructors.
func repoMethods(p *Pass, fn func(f *workspace.File, fd *ast.FuncDecl)) {
	for _, f := range p.WS.FilesIn(workspace.LayerRepo) {
		funcsIn(f, func(fd *ast.FuncDecl) {
			if fd.Body == nil {
				return
			}
			fn(f, fd)
		})
	}
}

// RepoBatchInLoop catches the N+1 query: a database call inside a loop body, so
// one request turns into one round trip per element.
//
// This is the highest-consequence member of the batching family and the only
// one that gates. The others are about efficiency; this one is about a request
// whose cost is unbounded in the size of its input, which is a production
// incident rather than a slow endpoint.
var RepoBatchInLoop = Rule{
	ID:       "repo-batch-in-loop",
	Severity: SeverityError,
	Summary:  "no database call inside a loop; queue the statements into a pgx.Batch and send once",
	Citation: dbPerfCitation,
	Check: func(p *Pass) {
		repoMethods(p, func(f *workspace.File, fd *ast.FuncDecl) {
			seen := map[*ast.CallExpr]bool{}
			for _, c := range callsInLoops(fd.Body, isDBExec) {
				if seen[c] {
					continue
				}
				seen[c] = true
				p.At(f, c).
					Fix(batchFix).
					Report("%s executes %s inside a loop; this is one database round trip per iteration",
						fd.Name.Name, callName(c))
			}
		})
	},
}

// RepoMultiRoundTrip counts separate database calls in one repository method.
//
// Two thresholds, neither blocking, per findings §10.2. The reviewers asked for
// a nudge at two and a recommendation at three, and were explicit that batching
// is never mandatory — which matters, because a batch is not always available.
// When the second query needs the first query's result there is nothing to
// batch, and the rule cannot tell the difference. So it reports an observation
// and leaves the judgement where it belongs.
//
// Methods that already batch, or that open a transaction, are skipped: the
// first has taken the advice and the second is repo-transaction-scope's business.
var RepoMultiRoundTrip = Rule{
	ID:       "repo-multi-roundtrip",
	Severity: SeverityWarning,
	Summary:  "repository methods making several database calls are candidates for a batch",
	Citation: dbPerfCitation,
	Check: func(p *Pass) {
		notice, recommend := p.Cfg.RoundTripNotice, p.Cfg.RoundTripRecommend
		if notice <= 0 {
			notice = 2
		}
		if recommend <= 0 {
			recommend = 3
		}
		repoMethods(p, func(f *workspace.File, fd *ast.FuncDecl) {
			if usesBatch(fd.Body) || usesTransaction(fd.Body) {
				return
			}
			n := countCalls(fd.Body, isDBExec)
			switch {
			case n >= recommend:
				p.At(f, fd).
					Fix(batchFix).
					Report("%s makes %d separate database calls; batch them where feasible", fd.Name.Name, n)
			case n >= notice:
				p.At(f, fd).
					Fix("if neither query depends on the other's result, a single batch would do").
					Report("%s makes %d separate database calls; a batch may be possible", fd.Name.Name, n)
			}
		})
	},
}

// RepoTransactionScope reports transactions that are not buying atomicity.
//
// Batch and transaction are not substitutes, and the review's shorthand ("use
// batch instead of transaction", 100 times across 15 services) is easy to
// over-apply. A batch is a round-trip optimisation; a transaction is an
// atomicity primitive; where both are wanted, a batch queued inside a
// transaction gives both. So this rule only fires where a transaction is
// actually present, and it never tells anyone to drop one that spans several
// statements — only to consider whether those statements must truly commit
// together.
var RepoTransactionScope = Rule{
	ID:       "repo-transaction-scope",
	Severity: SeverityWarning,
	Summary:  "transactions wrap statements that must commit together; a single statement is already atomic",
	Citation: dbPerfCitation,
	Check: func(p *Pass) {
		repoMethods(p, func(f *workspace.File, fd *ast.FuncDecl) {
			if !usesTransaction(fd.Body) {
				return
			}
			n := countCalls(fd.Body, isDBExec)
			if n <= 1 {
				p.At(f, fd).
					Fix("drop the transaction — a single statement is already atomic in Postgres").
					Report("%s opens a transaction for %d statement(s); the transaction buys nothing", fd.Name.Name, n)
				return
			}
			if !usesBatch(fd.Body) {
				p.At(f, fd).
					Fix("if these need not roll back together, a batch is cheaper; if they do, queue the batch inside the transaction").
					Report("%s runs %d statements in a transaction without batching them; %d round trips remain",
						fd.Name.Name, n, n)
			}
		})
	},
}

// rawRowMethods are the pgx entry points that hand back rows to scan by hand.
var rawRowMethods = []string{"Query", "QueryRow"}

// RepoRawRows bans hand-rolled row scanning.
//
// This closes the hole repo-rowmapper leaves open. That rule requires a by-name
// mapper on dblib calls that collect rows, and its reasoning about positional
// binding is right — but it only fires on calls that are already dblib.*. The
// pattern the reviewers flagged 55 times across 30 services never reaches dblib
// at all:
//
//	rows, _ := r.db.Query(ctx, sql, args...)
//	for rows.Next() { rows.Scan(&a, &b) }
//	r.db.QueryRow(ctx, sql, args...).Scan(&x)
//
// Both forms bind by position, which is the data-corruption failure db tags
// exist to prevent: add a column to the SELECT and every field after it shifts,
// with no error raised.
var RepoRawRows = Rule{
	ID:       "repo-raw-rows",
	Severity: SeverityError,
	Summary:  "rows are collected through dblib with a by-name mapper, never scanned by hand",
	Citation: "skill.md §Repository Pattern; " + dbPerfCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerRepo) {
			reported := map[int]bool{} // one finding per line
			report := func(n ast.Node, format string, args ...any) {
				line, _ := f.Position(n.Pos())
				if reported[line] {
					return
				}
				reported[line] = true
				p.At(f, n).
					Fix("use dblib.SelectRows(ctx, db, q, pgx.RowToStructByName[domain.X]) — db tags drive the mapping").
					Report(format, args...)
			}
			ast.Inspect(f.AST, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				name := callName(call)
				switch {
				case methodNamed(name, "Next", "Scan") && strings.Contains(strings.ToLower(name), "row"):
					report(call, "%s scans rows by hand; column order silently drives the mapping", name)
				case methodNamed(name, rawRowMethods...) && !strings.HasPrefix(name, "dblib."):
					// r.db.Query / r.db.QueryRow — the raw pgx path.
					if isDBReceiver(name) {
						report(call, "%s bypasses dblib; the result must then be scanned by hand", name)
					}
				}
				return true
			})
		}
	},
}

// isDBReceiver reports whether a rendered call name looks like it was made on a
// database handle rather than on some unrelated value that happens to have a
// Query method.
//
// Name-based, and deliberately so: the alternative is type checking, which
// would make the whole rule set depend on the workspace compiling. A repository
// that does not compile is exactly when the agent most needs the linter.
func isDBReceiver(name string) bool {
	// Drop the method, then take the last segment of what is left: for
	// `r.db.QueryRow` the receiver is `db`, not `r`.
	i := strings.LastIndex(name, ".")
	if i < 0 {
		return false
	}
	recv := name[:i]
	if j := strings.LastIndex(recv, "."); j >= 0 {
		recv = recv[j+1:]
	}
	switch strings.ToLower(recv) {
	case "db", "conn", "pool", "dbpool", "tx", "database":
		return true
	}
	return false
}

// RepoSelectStar requires an explicit column list.
//
// `SELECT *` couples the row mapper to whatever the table happens to contain,
// so a migration that adds a column changes what every existing query returns.
// dblib.GenerateColumnsFromStruct exists precisely so the column list and the
// domain struct cannot disagree.
var RepoSelectStar = Rule{
	ID:       "repo-select-star",
	Severity: SeverityError,
	Summary:  "queries name their columns; no SELECT * and no COUNT(*) over a wildcard",
	Citation: dbPerfCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerRepo) {
			ast.Inspect(f.AST, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok || !methodNamed(callName(call), "Select") {
					return true
				}
				for _, arg := range call.Args {
					bl, isLit := arg.(*ast.BasicLit)
					if !isLit {
						continue
					}
					v := strings.TrimSpace(litValue(bl))
					if v == "*" || strings.EqualFold(v, "count(*)") {
						p.At(f, bl).
							Fix("list the columns, or use dblib.GenerateColumnsFromStruct(domain.X{})").
							Report("query selects %q; a new column would silently change every result", v)
					}
				}
				return true
			})
			// Raw SQL strings anywhere in the repo layer.
			for _, bl := range stringLitsIn(f.AST) {
				v := litValue(bl)
				if len(v) < 8 {
					continue
				}
				up := strings.ToUpper(v)
				if strings.Contains(up, "SELECT *") || strings.Contains(up, "SELECT\t*") {
					p.At(f, bl).
						Fix("name the columns explicitly").
						Report("raw SQL selects every column with SELECT *")
				}
			}
		}
	},
}

// NoStoredProcedure keeps business logic in Go.
//
// A stored procedure is invisible to the linter, to code review, to the type
// checker and to `git log`. The review flagged the practice in two services;
// the rule exists so it does not spread to a third.
var NoStoredProcedure = Rule{
	ID:       "no-stored-procedure",
	Severity: SeverityError,
	Summary:  "no stored procedure calls; business logic lives in Go",
	Citation: dbPerfCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerRepo) {
			for _, bl := range stringLitsIn(f.AST) {
				v := strings.TrimSpace(litValue(bl))
				if v == "" {
					continue
				}
				up := strings.ToUpper(v)
				if strings.HasPrefix(up, "CALL ") || strings.HasPrefix(up, "SELECT * FROM CALL") {
					p.At(f, bl).
						Fix("move the logic into Go and build the query with dblib.Psql").
						Report("stored procedure invoked from Go; its behaviour is invisible to review and to the compiler")
				}
			}
		}
	},
}

// RepoSQLNow keeps time generation in Go.
//
// Two reasons, and the reviewers gave both. A row written with NOW() takes the
// database's clock while the rest of the request uses the application's, so a
// created_at can precede a value computed moments earlier. And NOW() inside a
// batch returns the transaction start time, not the statement time, so a batch
// of inserts silently shares one timestamp.
var RepoSQLNow = Rule{
	ID:       "repo-sql-now",
	Severity: SeverityWarning,
	Summary:  "timestamps come from time.Now() in Go, not NOW() in SQL",
	Citation: dbPerfCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerRepo) {
			for _, bl := range stringLitsIn(f.AST) {
				up := strings.ToUpper(litValue(bl))
				var found string
				switch {
				case strings.Contains(up, "NOW()"):
					found = "NOW()"
				case strings.Contains(up, "CURRENT_TIMESTAMP"):
					found = "CURRENT_TIMESTAMP"
				default:
					continue
				}
				p.At(f, bl).
					Fix("take now := time.Now() once at the top of the func and pass it as a parameter").
					Report("query uses SQL %s; the database clock and the request clock will disagree", found)
			}
		}
	},
}
