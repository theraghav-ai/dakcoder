package bootstrap

import (

	//validation "gitlab.cept.gov.in/it-2.0-common/api-validation"
	"context"
	"crypto/tls"
	v1 "gotemplate/gen/proto/v1/paocreationconnect"
	handler "gotemplate/handler"
	repo "gotemplate/repo/postgres"

	instrument "gotemplate/temporal_instrument"

	"time"

	log "gitlab.cept.gov.in/it-2.0-common/api-log"

	config "gitlab.cept.gov.in/it-2.0-common/api-config"
	g "gitlab.cept.gov.in/it-2.0-common/grpc-server"
	contracts "gitlab.cept.gov.in/it-2.0-common/temporal-contracts"
	tclient "go.temporal.io/sdk/client"
	worker "go.temporal.io/sdk/worker"
	"go.uber.org/fx"
)

// NewValidatorService add it as part of fx invoke
var Fxvalidator = fx.Module(
	"validator",
	fx.Invoke(handler.NewValidatorService),
)

var FxRepo = fx.Module(
	"Repomodule",
	fx.Provide(
		repo.NewPaogenRepository,
		repo.NewTransferEntryRepository,
		repo.NewPublicAcctRepository,
		repo.NewObjectionRepository,
		repo.NewObjectionFileRepository,
		repo.NewObjectionGrpcRepository,
		repo.NewPublicAcctGrpcRepository,
		repo.NewTransferEntryGrpcRepository,
		repo.NewTemporalRepository,
		// repo.NewECMSObjectionFileRepository,
	),
)

var FxHandler = fx.Module(
	"Handlermodule",
	fx.Provide(
		handler.NewPaogenHandler,
		handler.NewTransferEntryHandler,
		handler.NewPublicAcctHandler,
		handler.NewObjectionHandler,
		handler.NewObjectionFileHandler,
		handler.NewObjectionGrpcHandler,
		handler.NewPublicAcctGrpcHandler,
		handler.NewTransferEntryGrpcHandler,
		// handler.NewECMSObjectionFileHandler,
	),
)

func AddHandlers(registry *g.HandlerRegistry, createPaohandler *handler.ObjectionGrpcHandler, remunerationhandler *handler.PublicAcctGrpcHandler, transferentryhandler *handler.TransferEntryGrpcHandler) {

	registry.AddHandlers([]g.HandlerDefinition{
		{
			Constructor: g.Wrap(v1.NewPaoServiceHandler),
			Server:      createPaohandler,
		},
		{
			Constructor: g.Wrap(v1.NewRemunerationServiceHandler), // Add the new Remuneration service handler
			Server:      remunerationhandler,
		},
		{
			Constructor: g.Wrap(v1.NewTransferEntryServiceHandler), // Add the new Remuneration service handler
			Server:      transferentryhandler,
		},
	})
}

////////////////////////////////////////Temporal Code///////////////////////////////////////////////

func temporalclient(ctx context.Context, c *config.Config) (temporalclient tclient.Client, err error) {
	TemporalHost := c.GetString("temporal.host")
	TemporalPort := c.GetString("temporal.port")
	hostPort := TemporalHost + ":" + TemporalPort
	log.Info(ctx, "Connecting to Temporal Host Port: %s:%s", TemporalHost, TemporalPort)

	logger := instrument.TemporalLoggerAdapter(ctx) //implementation of our api-logger interface
	options := tclient.Options{
		HostPort:  hostPort,
		Logger:    logger,
		Namespace: contracts.PAONameSpace,
	}
	CertificatePath := c.GetString("temporal.certpath")

	if CertificatePath != "" {
		cert, _ := tls.LoadX509KeyPair(CertificatePath, "")

		options.ConnectionOptions = tclient.ConnectionOptions{
			TLS: &tls.Config{
				Certificates: []tls.Certificate{cert},
			},
		}
	}
	temporalClient, err := tclient.Dial(options)
	if err != nil {
		return nil, err
	}
	return temporalClient, nil

}

func ProvideTemporalWorker(config *config.Config, c tclient.Client) worker.Worker {

	// Set up Temporal Worker
	w := worker.New(c, contracts.PAOTaskQueue, worker.Options{
		DeadlockDetectionTimeout: 10 * time.Second,
	})
	w.RegisterWorkflow(repo.TransferentryRepoInstance.TransferentryverificationWorkflow)
	w.RegisterActivity(repo.TransferentryRepoInstance.SubVerifiedTePostingTemporalRepoNew)
	return w
}

func temporallifecycle(lc fx.Lifecycle, temporalclient tclient.Client) {
	lc.Append(fx.Hook{
		OnStart: func(context.Context) error {
			return nil
		},
		OnStop: func(ctx context.Context) error {
			temporalclient.Close()
			return nil
		},
	})

}

func RunWorker(lc fx.Lifecycle, w worker.Worker) {
	lc.Append(fx.Hook{
		OnStart: func(context.Context) error {
			go func() {
				err := w.Run(worker.InterruptCh())
				if err != nil {
					log.Fatal(nil, "Unable to start Worker", err)
				}
			}()
			return nil
		},
		OnStop: func(ctx context.Context) error {
			w.Stop()
			return nil
		},
	})
}

var Fxtemporal = fx.Module(
	"temporal",
	fx.Provide(
		temporalclient,
		ProvideTemporalWorker,
	),
	fx.Invoke(temporallifecycle, RunWorker),
	// Temporal Client Initialization

)

////////////////////////////////////////Temporal Code///////////////////////////////////////////////
