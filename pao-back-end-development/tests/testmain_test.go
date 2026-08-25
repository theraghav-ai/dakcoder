package tests

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"gotemplate/bootstrap"
	"gotemplate/routes"

	"github.com/golang-migrate/migrate/v4"
	_ "github.com/golang-migrate/migrate/v4/database/postgres"
	_ "github.com/golang-migrate/migrate/v4/source/file"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/wait"
	bootstrapper "gitlab.cept.gov.in/it-2.0-common/api-bootstrapper"
	config "gitlab.cept.gov.in/it-2.0-common/api-config"
	db "gitlab.cept.gov.in/it-2.0-common/api-db"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
	router "gitlab.cept.gov.in/it-2.0-common/api-server"
	"go.uber.org/fx"
	"go.uber.org/fx/fxtest"
)

var Router *router.Router

var Fxconfig = fx.Module(
	"configmodule",
	fx.Provide(
		config.NewDefaultConfigFactory,
		newFxConfig,
	),
)

type FxConfigParam struct {
	fx.In
	Factory config.ConfigFactory
}

func newFxConfig(p FxConfigParam) (*config.Config, error) {
	return p.Factory.Create(
		config.WithFileName("config"),
		//config.WithAppEnv(os.Getenv("APP_ENV")),
		config.WithFilePaths(
			".",
			"../configs",
			//os.Getenv("APP_CONFIG_PATH"),
		),
	)
}

var FxDB = fx.Module(
	"DBModule",
	fx.Provide(
		SetUpDB,
	),
	// fx.Invoke(dblifecycle),
)

func SetUpDB(c *config.Config) (*db.DB, testcontainers.Container) {
	ctx := context.Background()
	var db1 *pgxpool.Pool
	var err error
	db1, Container, err = setupdockerdb(ctx, c)
	if err != nil {
		log.Fatal(ctx, "failed to setup db--->>> %s", err)
	}
	db := db.DB{Pool: db1}
	log.Info(ctx, "Successfully connected to the database %s", c.GetString("db.database"))
	return &db, Container
}

func setupdockerdb(ctx context.Context, c *config.Config) (*pgxpool.Pool, testcontainers.Container, error) {
	var env = map[string]string{
		"POSTGRES_PASSWORD": c.GetString("db.password"),
		"POSTGRES_USER":     c.GetString("db.username"),
		"POSTGRES_DB":       c.GetString("db.database"),
	}
	var port = "5432/tcp"

	req := testcontainers.GenericContainerRequest{
		ContainerRequest: testcontainers.ContainerRequest{
			Image:        "postgres:14-alpine",
			ExposedPorts: []string{port},
			Env:          env,
			ShmSize:      134217728,
			WaitingFor:   wait.ForLog("database system is ready to accept connections"),
		},
		Started: true,
	}
	container, err := testcontainers.GenericContainer(ctx, req)
	if err != nil {
		return nil, container, fmt.Errorf("failed to start container: %v", err)
	}

	p, err := container.MappedPort(ctx, "5432")
	if err != nil {
		return nil, container, fmt.Errorf("failed to get container external port: %v", err)
	}

	dbAddr := fmt.Sprintf("localhost:%s", p.Port())

	dsn := fmt.Sprintf("user=%s password=%s host=%s port=%s dbname=%s search_path=%s sslmode=disable",
		c.GetString("db.username"),
		c.GetString("db.password"),
		c.GetString("db.host"),
		p.Port(),
		c.GetString("db.database"),
		c.GetString("db.schema"))

	config, err := pgxpool.ParseConfig(dsn)
	if err != nil {

		return nil, container, err
	}
	config.MaxConns = int32(c.GetInt("db.maxconns"))
	config.MinConns = int32(c.GetInt("db.minconns"))
	config.MaxConnLifetime = time.Duration(c.GetInt("db.maxconnlifetime")) * time.Minute
	config.MaxConnIdleTime = time.Duration(c.GetInt("db.maxconnidletime")) * time.Minute
	retries := 0
	var db *pgxpool.Pool
	for retries < 10 {
		db, _ = pgxpool.New(ctx, config.ConnString())

		err := db.Ping(ctx)
		if err == nil {
			log.Info(ctx, "Ping Successful....")
			break
		} else {

			db.Close()
		}

		retries++
		log.Info(ctx, "Ping attempt failed. Retrying... (Attempt %d/%d)\n", retries, 10)
		time.Sleep(1 * time.Second)
	}

	err = migrateDb(dbAddr, c)
	if err != nil {
		log.Fatal(ctx, "failed to perform db migration--->>> %s", err)
	}

	return db, container, nil
}

func migrateDb(dbAddr string, c *config.Config) error {
	_, path, _, ok := runtime.Caller(0)
	if !ok {
		return fmt.Errorf("failed to get path")
	}
	//pathToMigrationFiles := filepath.Dir(path) + "/migration"
	pathToMigrationFiles := filepath.Join(filepath.Dir(path), "migration")

	databaseURL := fmt.Sprintf("postgres://%s:%s@%s/%s?sslmode=disable", c.GetString("db.username"), c.GetString("db.password"), dbAddr, c.GetString("db.database"))

	m, err := migrate.New(fmt.Sprintf("file:%s", pathToMigrationFiles), databaseURL)
	if err != nil {
		return err
	}
	defer m.Close()

	err = m.Up()
	if err != nil && !errors.Is(err, migrate.ErrNoChange) {
		return err
	}

	return nil
}

func teardownTestData() {
	_ = Container.Terminate(context.Background())
	App.RequireStop()
}

var Container testcontainers.Container
var App *fxtest.App

func BootstrapTestApp(tb testing.TB, options ...fx.Option) *router.Router {
	tb.Helper()
	//var httpServer *gin.Engine

	//tb.Setenv("APP_ENV", "test")

	App = fxtest.New(
		tb,
		//fx.Options(options...),
		//fx.Populate(&httpServer),
		Fxconfig,
		// bootstrapper.Fxlog,
		FxDB,
		fx.Populate(&Router),
		//bootstrap.Fxclient,
		bootstrap.Fxvalidator,
		// bootstrapper.Fxrouter,
		bootstrap.FxHandler,
		bootstrap.FxRepo,
		bootstrapper.FxMinIO,
		fx.Invoke(routes.Routes),
	)
	App.RequireStart()

	return Router
}

func TestMain(m *testing.M) {
	t := &testing.T{}
	Router = BootstrapTestApp(t)
	m.Run()
	teardownTestData()
}
