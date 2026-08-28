package rules

import (
	"go/ast"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

const logCitation = "docs/CODE-REVIEW-FINDINGS.md; references/logging.md"

// logFuncs are the log entry points, by rendered call name suffix.
var logLevels = []string{"Debug", "Info", "Warn", "Error", "Fatal", "Panic"}

// isLogCall reports whether a call is a log emission, returning the level.
func isLogCall(c *ast.CallExpr) (string, bool) {
	name := callName(c)
	pkg, method, ok := strings.Cut(name, ".")
	if !ok {
		return "", false
	}
	if !strings.EqualFold(pkg, "log") && !strings.HasSuffix(strings.ToLower(pkg), "logger") {
		return "", false
	}
	for _, lvl := range logLevels {
		if method == lvl || method == lvl+"f" {
			return lvl, true
		}
	}
	return "", false
}

// RepoNoLogging keeps logging in the layer that has the request.
//
// The handler knows the route, the request id and the user; the repository
// knows none of those, so a log line written there is a message with no context
// attached to it. Worse, a repository that both logs and returns an error
// produces two entries for one failure — and the reviewers found 135 such calls
// across 7 files in a single service, 66 of them in one file.
//
// The existing error-handling rule already requires handlers to log before
// returning, so this is the other half of the same contract: log once, where
// the context is.
var RepoNoLogging = Rule{
	ID:       "repo-no-logging",
	Severity: SeverityWarning,
	Summary:  "repositories return errors rather than logging them; the handler logs once, with request context",
	Citation: logCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerRepo) {
			funcsIn(f, func(fd *ast.FuncDecl) {
				if fd.Body == nil {
					return
				}
				ast.Inspect(fd.Body, func(n ast.Node) bool {
					call, ok := n.(*ast.CallExpr)
					if !ok {
						return true
					}
					lvl, isLog := isLogCall(call)
					if !isLog {
						return true
					}
					p.At(f, call).
						Fix("return the error and let the handler log it once, where the request id is").
						Report("%s logs at %s inside the repository layer; the line carries no request context",
							fd.Name.Name, lvl)
					return false
				})
			})
		}
	},
}

// NoSensitiveLogging keeps secrets and personal data out of the logs.
//
// Ships as a warning rather than an error because the field list has no named
// owner yet (findings §10.4). Promoting it is a one-line change once someone
// owns the vocabulary, and that is the right moment: a blocking rule needs a
// list somebody is accountable for.
//
// Matching is on whole identifiers, never substrings. Tested against the 8,229
// distinct field names in the review sheets, a substring match on `pan` hits 20
// of them and 19 are innocent — CompanyName, Discrepancy,
// NoOfPanchayatSanchaarSevaKendras. A rule wrong nineteen times out of twenty
// is a rule that gets disabled.
var NoSensitiveLogging = Rule{
	ID:       "no-sensitive-logging",
	Severity: SeverityWarning,
	Summary:  "passwords, tokens, identity numbers and contact details never reach a log line",
	Citation: logCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.Files {
			if f.Layer == workspace.LayerTest {
				continue
			}
			funcsIn(f, func(fd *ast.FuncDecl) {
				if fd.Body == nil {
					return
				}
				ast.Inspect(fd.Body, func(n ast.Node) bool {
					call, ok := n.(*ast.CallExpr)
					if !ok {
						return true
					}
					if _, isLog := isLogCall(call); !isLog {
						return true
					}
					for _, arg := range call.Args {
						name, ok := sensitiveOperand(p.Cfg, arg)
						if !ok {
							continue
						}
						p.At(f, arg).
							Fix("log an identifier instead — the record id, not the value").
							Report("log line includes %s, which holds sensitive data", name)
					}
					return true
				})
			})
		}
	},
}

// sensitiveOperand reports whether an expression names something that must not
// be logged, returning the offending identifier.
//
// Handles the two shapes that actually occur: a bare variable (`password`) and
// a field selector (`req.AadhaarNumber`). A whole struct passed to a logger is
// caught by log-level-hygiene instead, which is the rule that can say something
// useful about it.
func sensitiveOperand(cfg Config, e ast.Expr) (string, bool) {
	switch t := e.(type) {
	case *ast.Ident:
		if cfg.IsSensitive(t.Name) {
			return t.Name, true
		}
	case *ast.SelectorExpr:
		if cfg.IsSensitive(t.Sel.Name) {
			return typeString(t), true
		}
	case *ast.UnaryExpr:
		return sensitiveOperand(cfg, t.X)
	}
	return "", false
}

// LogLevelHygiene keeps Info readable and keeps payloads out of it.
//
// From the review: *"Many Debug messages are being printed as Log.Info. These
// should be Debug. Avoid printing the whole response as Info."* Two costs —
// an Info stream nobody can read during an incident, and whole request or
// response bodies in the logs, which is how personal data leaks without anyone
// deciding to log it.
var LogLevelHygiene = Rule{
	ID:       "log-level-hygiene",
	Severity: SeverityWarning,
	Summary:  "whole requests and responses are logged at Debug, not Info",
	Citation: logCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.Files {
			if f.Layer == workspace.LayerTest {
				continue
			}
			funcsIn(f, func(fd *ast.FuncDecl) {
				if fd.Body == nil {
					return
				}
				ast.Inspect(fd.Body, func(n ast.Node) bool {
					call, ok := n.(*ast.CallExpr)
					if !ok {
						return true
					}
					lvl, isLog := isLogCall(call)
					if !isLog || lvl != "Info" {
						return true
					}
					for _, arg := range call.Args {
						name, isPayload := payloadOperand(arg)
						if !isPayload {
							continue
						}
						p.At(f, call).
							Fix("log at Debug, and log identifying fields rather than the whole value").
							Report("%s is logged whole at Info level; payloads belong at Debug", name)
						return false
					}
					return true
				})
			})
		}
	},
}

// payloadOperand reports whether an argument looks like a whole request or
// response value rather than a scalar.
func payloadOperand(e ast.Expr) (string, bool) {
	name := ""
	switch t := e.(type) {
	case *ast.Ident:
		name = t.Name
	case *ast.SelectorExpr:
		name = t.Sel.Name
	case *ast.UnaryExpr:
		return payloadOperand(t.X)
	default:
		return "", false
	}
	n := strings.ToLower(name)
	for _, suffix := range []string{"request", "response", "req", "resp", "payload", "body", "dto"} {
		if n == suffix || strings.HasSuffix(n, suffix) {
			return name, true
		}
	}
	return "", false
}
