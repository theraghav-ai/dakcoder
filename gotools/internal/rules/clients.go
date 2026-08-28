package rules

import (
	"go/ast"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// The rules in this file cover three of the reviewers' standing suggestions
// that turn out to be one defect wearing three hats: clients built per request
// (#6, #13), dependencies constructed rather than injected (#22), and contexts
// that do not descend from the request (#8).
//
// All three are visible in the legacy corpus at named lines, which is why they
// gate rather than warn.

const clientCitation = "docs/CODE-REVIEW-FINDINGS.md; references/clients-and-context.md"

// clientConstructors are calls that build a long-lived, poolable client.
//
// The value is the constructor's own name for use in the message. Keyed by the
// rendered call name, so an aliased import still matches on the selector.
var clientConstructors = map[string]string{
	"resty.New":           "a Resty client",
	"minio.New":           "a MinIO client",
	"minio.NewWithRegion": "a MinIO client",
	"kafka.NewWriter":     "a Kafka writer",
	"kafka.NewReader":     "a Kafka reader",
	"redis.NewClient":     "a Redis client",
	"mongo.Connect":       "a Mongo client",
	"grpc.NewClient":      "a gRPC client",
	"grpc.Dial":           "a gRPC client",
}

// ClientSingleton requires long-lived clients to be built once and injected.
//
// A `resty.New()` inside a handler method builds a fresh client — and a fresh
// connection pool — for every request, so nothing is ever reused, keep-alive
// never helps, and the pool limits that exist to protect the upstream service
// are silently per-request rather than per-process. The legacy corpus does this
// at three sites: handler/paogen.go:2813, handler/paogen.go:4172 and
// handler/transferentry.go:2024.
//
// Scoped to the layers that serve requests. bootstrap/ is where these are
// supposed to be built, main.go legitimately wires things up, and tests build
// whatever they need.
var ClientSingleton = Rule{
	ID:       "client-singleton",
	Severity: SeverityError,
	Summary:  "long-lived clients are constructed once in bootstrap/ and injected, not built per request",
	Citation: clientCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.Files {
			switch f.Layer {
			case workspace.LayerBootstrap, workspace.LayerMain, workspace.LayerTest:
				continue
			}
			funcsIn(f, func(fd *ast.FuncDecl) {
				if fd.Body == nil {
					return
				}
				// A constructor in this file is providing the dependency, not
				// reaching for it; that is the pattern the rule wants.
				if isProviderFunc(fd) {
					return
				}
				ast.Inspect(fd.Body, func(n ast.Node) bool {
					call, ok := n.(*ast.CallExpr)
					if !ok {
						return true
					}
					what, isClient := clientConstructors[callName(call)]
					if !isClient {
						return true
					}
					p.At(f, call).
						Fix("provide it once in bootstrap/ via fx.Provide and inject the pointer into %s", receiverOrFunc(fd)).
						Report("%s builds %s per call; the connection pool is rebuilt every request", fd.Name.Name, what)
					return false
				})
			})
		}
	},
}

// isProviderFunc reports whether a function looks like a dependency provider —
// a plain `NewX` returning something — as opposed to request-handling code.
func isProviderFunc(fd *ast.FuncDecl) bool {
	if _, isMethod := receiverType(fd); isMethod {
		return false
	}
	return strings.HasPrefix(fd.Name.Name, "New") || strings.HasPrefix(fd.Name.Name, "Provide")
}

// receiverOrFunc names the enclosing type for a message, falling back to the
// function name.
func receiverOrFunc(fd *ast.FuncDecl) string {
	if recv, ok := receiverType(fd); ok {
		return strings.TrimPrefix(recv, "*")
	}
	return fd.Name.Name
}

// detachedContexts are the context constructors that start a fresh tree.
var detachedContexts = map[string]bool{
	"context.Background": true,
	"context.TODO":       true,
}

// CtxPropagation requires request-scoped work to descend from the request.
//
// `context.Background()` in a handler or repository is not a style choice: it
// discards the client's cancellation, the request deadline and the trace id.
// When the caller hangs up, the work carries on and nothing connects the log
// lines to the request that caused them.
//
// The legacy corpus has 22 of these, and one of them carries a comment reading
// `// ← FIXED: use context.Background() for all DB ops after PFMS call` — a
// deliberate detachment, added to work around a parent that had already been
// cancelled. That is the shape this rule is meant to surface: not carelessness,
// but a workaround whose cost was invisible.
var CtxPropagation = Rule{
	ID:       "ctx-propagation",
	Severity: SeverityError,
	Summary:  "request-scoped work derives its context from the request, never from context.Background()",
	Citation: clientCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.Files {
			switch f.Layer {
			// main.go and bootstrap/ own the process lifetime; a root context
			// is exactly right there. Tests build their own.
			case workspace.LayerMain, workspace.LayerBootstrap, workspace.LayerTest:
				continue
			}
			funcsIn(f, func(fd *ast.FuncDecl) {
				if fd.Body == nil {
					return
				}
				// Only functions that were handed a context can propagate one.
				if !takesContext(fd) {
					return
				}
				ast.Inspect(fd.Body, func(n ast.Node) bool {
					call, ok := n.(*ast.CallExpr)
					if !ok || !detachedContexts[callName(call)] {
						return true
					}
					p.At(f, call).
						Fix("pass the incoming ctx down instead of starting a new context tree").
						Report("%s calls %s while holding a request context; cancellation, deadline and trace id are dropped",
							fd.Name.Name, callName(call))
					return false
				})
			})
		}
	},
}

// takesContext reports whether a function receives something request-scoped it
// could have propagated.
//
// `*gin.Context` counts. It is the legacy shape the template has moved away
// from, but it carries the request just the same — and a legacy service is
// exactly where this rule earns its keep, because that is where
// `context.WithTimeout(context.Background(), …)` gets written to work around a
// parent that was already cancelled.
func takesContext(fd *ast.FuncDecl) bool {
	for _, t := range params(fd) {
		switch t {
		case "context.Context", "*context.Context", serverCtxType, "*gin.Context":
			return true
		}
	}
	return false
}

// detachedTimeouts returns every context.WithTimeout/WithDeadline/WithCancel
// call whose parent is a fresh root rather than an inherited context.
//
// Shared with repo-contract, which needs the same question answered about the
// deadline it already requires.
func detachedTimeouts(n ast.Node) []*ast.CallExpr {
	var out []*ast.CallExpr
	ast.Inspect(n, func(x ast.Node) bool {
		call, ok := x.(*ast.CallExpr)
		if !ok {
			return true
		}
		switch callName(call) {
		case "context.WithTimeout", "context.WithDeadline", "context.WithCancel":
		default:
			return true
		}
		if len(call.Args) == 0 {
			return true
		}
		parent, ok := call.Args[0].(*ast.CallExpr)
		if ok && detachedContexts[callName(parent)] {
			out = append(out, call)
		}
		return true
	})
	return out
}

// timeoutSetters are the ways a client is given a deadline.
var timeoutSetters = []string{"SetTimeout", "WithTimeout", "SetDeadline", "Timeout"}

// ExternalCallTimeout requires outbound calls to be bounded.
//
// repo-contract already requires a deadline on every database call. Nothing
// required one on the HTTP calls that leave the service, and an outbound
// request with no timeout is the classic way one slow upstream turns into an
// exhausted worker pool here.
//
// Advisory, because the deadline may legitimately live on the injected client
// rather than at the call site, and this rule sees one function at a time.
var ExternalCallTimeout = Rule{
	ID:       "external-call-timeout",
	Severity: SeverityWarning,
	Summary:  "outbound calls carry a timeout, a deadline, or a context that has one",
	Citation: clientCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.Files {
			if f.Layer == workspace.LayerTest {
				continue
			}
			funcsIn(f, func(fd *ast.FuncDecl) {
				if fd.Body == nil {
					return
				}
				bounded := false
				ast.Inspect(fd.Body, func(n ast.Node) bool {
					if c, ok := n.(*ast.CallExpr); ok {
						name := callName(c)
						if methodNamed(name, timeoutSetters...) ||
							name == "context.WithTimeout" || name == "context.WithDeadline" {
							bounded = true
							return false
						}
					}
					return true
				})
				if bounded {
					return
				}
				ast.Inspect(fd.Body, func(n ast.Node) bool {
					call, ok := n.(*ast.CallExpr)
					if !ok {
						return true
					}
					name := callName(call)
					if !isOutboundCall(name) {
						return true
					}
					p.At(f, call).
						Fix("set the timeout on the injected client, or pass a context that already carries a deadline").
						Report("%s makes an outbound call (%s) with no timeout in sight; a slow upstream would block indefinitely",
							fd.Name.Name, name)
					return false
				})
			})
		}
	},
}

// outboundVerbs are the request-issuing methods of the HTTP clients in use.
var outboundVerbs = []string{"Get", "Post", "Put", "Patch", "Delete", "Head", "Do", "Send"}

// isOutboundCall reports whether a rendered call name issues a network request.
//
// Restricted to receivers that look like an HTTP client, because `Get` and
// `Do` are far too common as bare method names to match on their own.
func isOutboundCall(name string) bool {
	if strings.HasPrefix(name, "http.") {
		return methodNamed(name, outboundVerbs...)
	}
	recv, _, ok := strings.Cut(name, ".")
	if !ok {
		return false
	}
	if i := strings.LastIndex(recv, "."); i >= 0 {
		recv = recv[i+1:]
	}
	switch strings.ToLower(recv) {
	case "client", "httpclient", "restclient", "resty", "r", "req", "request":
		return methodNamed(name, outboundVerbs...)
	}
	return false
}
