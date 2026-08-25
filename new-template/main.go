package main

import (
	"context"
	"pisapi/bootstrap"

	bootstrapper "gitlab.cept.gov.in/it-2.0-common/n-api-bootstrapper"
)

// Swagger
//
//	@title			Personal Information System API
//	@version		1.0
//	@description	A comprehensive API for Personal Information System.
//	@termsOfService	http://cept.gov.in/terms
//	@contact.name	API Support Team
//	@contact.url	http://cept.gov.in/support
//	@contact.email	support_cept@indiapost.gov.in
//	@license.name	Apache 2.0
//	@license.url	http://www.apache.org/licenses/LICENSE-2.0.html
//	@host			localhost:8080
//	@BasePath		/v1
//	@schemes		http https
func main() {
	app := bootstrapper.New().Options(
		// bootstrap.Fxvalidator,
		bootstrap.FxHandler,
		bootstrap.FxRepo,
	)
	app.WithContext(context.Background()).Run()
}
