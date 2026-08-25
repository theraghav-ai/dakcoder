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
	),
)
