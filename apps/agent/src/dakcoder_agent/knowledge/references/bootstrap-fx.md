---
slug: bootstrap-fx
handle: "@skill:bootstrap-fx"
fetch_when: "registering a new repository or handler — read before editing bootstrapper.go"
sources:
  - "skill.md §Bootstrap Configuration"
  - "SOP.md §bootstrap/bootstrapper.go"
---

# Bootstrap and Uber-FX registration

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

The composition root registers repositories and handlers, and the two registrations are **not** interchangeable.

A repository is a plain provider. A handler must be wrapped in `fx.Annotate` with `fx.As(new(serverHandler.Handler))` and `fx.ResultTags(serverHandler.ServerControllersGroupTag)`, because the server collects handlers by group tag.

Getting this wrong has two failure modes and neither is obvious. A missing registration fails at start-up with an Uber-FX error naming a type, not a file. A handler registered with a bare `fx.Provide` compiles, starts, and silently serves none of its routes — there is no error at all.

Prefer the `fx_wire` tool over editing by hand; it produces the correct shape and is a no-op if the constructor is already registered.

Enforced by `fx-registration`.

## Bootstrap Configuration

*From `skill.md` §Bootstrap Configuration (lines 135–260).*

**Location**: `bootstrap/bootstrapper.go`

**Purpose**: Defines Uber FX dependency injection modules for automatic wiring of dependencies.

**Complete Pattern**:
```go
package bootstrap

import (
    "go.uber.org/fx"
    serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
    handler "{project}/handler"
    repo "{project}/repo/postgres"
)

// FxRepo module provides all repository implementations
var FxRepo = fx.Module(
    "Repomodule",
    fx.Provide(
        repo.New{Resource1}Repository,
        repo.New{Resource2}Repository,
        // Add more repository constructors here
        // repo.New{Resource3}Repository,
    ),
)

// FxHandler module provides all HTTP handlers
var FxHandler = fx.Module(
    "Handlermodule",
    fx.Provide(
        // Each handler must be annotated to implement serverHandler.Handler interface
        fx.Annotate(
            handler.New{Resource1}Handler,
            fx.As(new(serverHandler.Handler)),
            fx.ResultTags(serverHandler.ServerControllersGroupTag),
        ),
        fx.Annotate(
            handler.New{Resource2}Handler,
            fx.As(new(serverHandler.Handler)),
            fx.ResultTags(serverHandler.ServerControllersGroupTag),
        ),
        // Add more handler constructors here
        // fx.Annotate(
        //     handler.New{Resource3}Handler,
        //     fx.As(new(serverHandler.Handler)),
        //     fx.ResultTags(serverHandler.ServerControllersGroupTag),
        // ),
    ),
)

// Optional: Custom validator module (if using custom validators)
// var Fxvalidator = fx.Module(
//     "Validatormodule",
//     fx.Provide(
//         // Add custom validator providers here
//     ),
// )
```

**Rules**:
- Create separate FX modules for different concerns (Repo, Handler, Validator, etc.)
- Module naming convention: `Fx{ModuleName}` (e.g., FxRepo, FxHandler)
- Module string name: `"{ModuleName}module"` (e.g., "Repomodule", "Handlermodule")
- Use `fx.Provide()` to register constructors
- Handlers MUST be wrapped with `fx.Annotate()` with:
  - `fx.As(new(serverHandler.Handler))` - Converts to Handler interface
  - `fx.ResultTags(serverHandler.ServerControllersGroupTag)` - Groups handlers
- Repositories are provided directly without annotation
- Add comments to indicate where new resources should be added
- Dependencies are automatically injected based on constructor parameters
- Order of registration doesn't matter (FX resolves dependency graph)

**Dependency Injection Flow**:
1. Bootstrapper creates database connection (*dblib.DB)
2. Bootstrapper loads configuration (*config.Config)
3. FxRepo provides repositories (injecting db and config)
4. FxHandler provides handlers (injecting repositories)
5. Server automatically discovers and registers all handlers

**Example with Multiple Resources**:
```go
package bootstrap

import (
    handler "pisapi/handler"
    repo "pisapi/repo/postgres"

    serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
    "go.uber.org/fx"
)

var FxRepo = fx.Module(
    "Repomodule",
    fx.Provide(
        repo.NewUserRepository,
        repo.NewProductRepository,
        repo.NewOrderRepository,
    ),
)

var FxHandler = fx.Module(
    "Handlermodule",
    fx.Provide(
        fx.Annotate(
            handler.NewUserHandler,
            fx.As(new(serverHandler.Handler)),
            fx.ResultTags(serverHandler.ServerControllersGroupTag),
        ),
        fx.Annotate(
            handler.NewProductHandler,
            fx.As(new(serverHandler.Handler)),
            fx.ResultTags(serverHandler.ServerControllersGroupTag),
        ),
        fx.Annotate(
            handler.NewOrderHandler,
            fx.As(new(serverHandler.Handler)),
            fx.ResultTags(serverHandler.ServerControllersGroupTag),
        ),
    ),
)
```

---

## bootstrap/bootstrapper.go

*From `SOP.md` §bootstrap/bootstrapper.go (lines 201–222).*

1. add the handlers to the bootstrapper as shown.
```go


var FxHandler = fx.Module(
	"Handlermodule",
	fx.Provide(
		fx.Annotate(
			handler.NewTransferHandler,
			fx.As(new(serverHandler.Handler)),
			fx.ResultTags(serverHandler.serverControllersGroupTag),
		),
		fx.Annotate(
			handler.NewNocHandler,
			fx.As(new(serverHandler.Handler)),
			fx.ResultTags(serverHandler.serverControllersGroupTag),
		),
		
	),
)
```
