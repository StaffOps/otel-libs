// Command backend is the Go demo backend (specs/demo-environment DE-D2,
// DE-D4, DE-D8, DE-D9): the gRPC server of demo.v1.OrderService, client of
// the shared PostgreSQL through ext/otelsql (pgx). Each PlaceOrder runs in the
// demo span demo.order.process, which records the custom metrics and, on
// failure, follows ADR-014 (error.type on the span, detail in a correlated
// ERROR log record).
//
// Environment (besides the OTEL_* variables): DEMO_PG_HOST (host or
// host:port, port 5432 by default), DEMO_PG_DB, DEMO_PG_USER,
// DEMO_PG_PASSWORD_FILE (a mounted file, DE-D13), DEMO_GRPC_PORT (50051),
// DEMO_SCHEMA_FILE (demo/sql/schema.sql in the image).
package main

import (
	"context"
	"database/sql"
	"fmt"
	"log"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/stdlib"
	otelhelper "github.com/staffops/otel-libs/go"
	"github.com/staffops/otel-libs/go/demo/internal/demokit"
	"github.com/staffops/otel-libs/go/ext/otelsql"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"

	demov1 "github.com/staffops/otel-libs/go/demo/gen/otellibs/demo/v1"
)

// defaultSchemaFile is where the Dockerfile copies demo/sql/schema.sql.
const defaultSchemaFile = "/etc/otel-libs-demo/schema.sql"

func main() {
	if err := run(); err != nil {
		log.Printf("demo-go-backend: %v", err)
		os.Exit(1)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	logger, flush, err := demokit.Setup(ctx)
	if err != nil {
		return err
	}
	defer flush()

	db, closeDB, err := openDB()
	if err != nil {
		return err
	}
	defer func() { _ = closeDB() }()

	schema, err := os.ReadFile(demokit.EnvOr("DEMO_SCHEMA_FILE", defaultSchemaFile))
	if err != nil {
		return fmt.Errorf("read schema: %w", err)
	}
	if err := applySchema(ctx, logger, db, string(schema)); err != nil {
		return err
	}

	ins, err := demokit.NewInstruments(otelhelper.GetMeter(demokit.TracerName))
	if err != nil {
		return fmt.Errorf("instruments: %w", err)
	}
	svc := &orderService{
		store:  &sqlStore{db: db},
		tracer: otelhelper.GetTracer(demokit.TracerName),
		logger: logger,
		ins:    ins,
		run:    demokit.RunID(),
	}

	port := demokit.EnvOr("DEMO_GRPC_PORT", "50051")
	lis, err := net.Listen("tcp", ":"+port)
	if err != nil {
		return fmt.Errorf("listen: %w", err)
	}
	srv := grpc.NewServer(otelhelper.GRPCServerOption())
	demov1.RegisterOrderServiceServer(srv, svc)
	hs := health.NewServer()
	healthpb.RegisterHealthServer(srv, hs)
	hs.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)

	srvErr := make(chan error, 1)
	go func() { srvErr <- srv.Serve(lis) }()
	logger.Info("demo backend listening", "server.port", port)

	var runErr error
	select {
	case <-ctx.Done():
	case runErr = <-srvErr:
	}
	hs.Shutdown()
	stopped := make(chan struct{})
	go func() { srv.GracefulStop(); close(stopped) }()
	select {
	case <-stopped:
	case <-time.After(4 * time.Second):
		srv.Stop()
	}
	return runErr
}

// openDB opens the pool through ext/otelsql. The password is read from its
// mounted file and goes only into a pgx.ConnConfig registered with the pgx
// stdlib driver, so no DSN string holding it ever exists (DE-D13).
func openDB() (*sql.DB, func() error, error) {
	password, err := demokit.ReadSecretFile("DEMO_PG_PASSWORD_FILE")
	if err != nil {
		return nil, nil, err
	}
	cfg, err := pgx.ParseConfig("sslmode=disable connect_timeout=5")
	if err != nil {
		return nil, nil, fmt.Errorf("pgx config: %w", err)
	}
	host, port := demokit.HostPort(demokit.EnvOr("DEMO_PG_HOST", "demo-postgres"), 5432)
	cfg.Host = host
	cfg.Port = uint16(port)
	cfg.Database = demokit.EnvOr("DEMO_PG_DB", "demo_"+demokit.Lang)
	cfg.User = demokit.EnvOr("DEMO_PG_USER", "demo_"+demokit.Lang)
	cfg.Password = password
	// A RegisterConnConfig key is not a DSN: name the database (db.namespace, D16).
	db, closeDB, err := otelsql.Open("pgx", stdlib.RegisterConnConfig(cfg), otelsql.WithDBNamespace(cfg.Database))
	if err != nil {
		return nil, nil, fmt.Errorf("open database: %w", err)
	}
	db.SetMaxOpenConns(4)
	db.SetConnMaxIdleTime(5 * time.Minute)
	return db, closeDB, nil
}

// applySchema sends schema.sql as one command without bind parameters (pgx
// then uses the simple query protocol: one implicit transaction, the advisory
// lock serializes concurrent backends, DE-D4 r3.3). PostgreSQL may still be
// starting, so it retries for up to a minute.
func applySchema(ctx context.Context, logger *slog.Logger, db *sql.DB, schema string) error {
	var err error
	for attempt := 1; attempt <= 30; attempt++ {
		if _, err = db.ExecContext(ctx, schema); err == nil {
			logger.Info("demo schema applied", "attempt", attempt)
			return nil
		}
		// pgx connect errors name host, user and database, never the password.
		logger.Warn("demo schema not applied yet", "attempt", attempt, "error", err.Error())
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
	return fmt.Errorf("apply schema: %w", err)
}
