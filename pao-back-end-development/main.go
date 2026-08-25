package main

import (
	"context"
	"gotemplate/bootstrap"
	"gotemplate/routes"

	bootstrapper "gitlab.cept.gov.in/it-2.0-common/api-bootstrapper"
	"go.uber.org/fx"
)

// Swagger
//
//	@title                       Address Service API
//	@version                     1.0
//	@description                 A comprehensive API for budget related activities.
//	@termsOfService              http://cept.gov.in/terms
//	@contact.name                API Support Team
//	@contact.url                 http://cept.gov.in/support
//	@contact.email               support_cept@indiapost.gov.in
//	@license.name                Apache 2.0
//	@license.url                 http://www.apache.org/licenses/LICENSE-2.0.html
//	@host                        localhost:8080
//	@BasePath                    /
//	@schemes                     http https

func main() {
	// app := fx.New(

	// 	bootstrap.Fxvalidator,
	// 	bootstrap.FxHandler,
	// 	bootstrap.FxRepo,
	// 	fx.Invoke(routes.Routes),

	// )

	// app.Run()
	app := bootstrapper.New().Options(
		// bootstrapper.Fxclient,
		bootstrap.Fxvalidator,
		fx.Invoke(routes.Routes),
		bootstrap.FxHandler,
		bootstrap.FxRepo,
		bootstrapper.FxMinIO,
		bootstrapper.FxGrpc,
		bootstrap.Fxtemporal,
		fx.Invoke(bootstrap.AddHandlers),
	)

	app.WithContext(context.Background()).Run()

}
