// Command api is the Go demo api (specs/demo-environment DE-D2, DE-D5,
// DE-D6, DE-D8): HTTP server of POST /orders, Redis quote cache client
// (ext/otelredis), gRPC client of demo.v1.OrderService and producer of the
// order event on the Redis stream demo:go:orders, with the W3C context
// injected into the message fields.
//
// Environment (besides the OTEL_* variables): DEMO_HTTP_PORT (8080),
// DEMO_BACKEND_ADDR (demo-go-backend:50051), DEMO_REDIS_ADDR
// (demo-redis:6379), DEMO_REDIS_PASSWORD_FILE (a mounted file, DE-D13).
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/redis/go-redis/v9/maintnotifications"
	otelhelper "github.com/staffops/otel-libs/go"
	"github.com/staffops/otel-libs/go/demo/internal/demokit"
	"github.com/staffops/otel-libs/go/ext/otelredis"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"

	demov1 "github.com/staffops/otel-libs/go/demo/gen/otellibs/demo/v1"
)

func main() {
	if err := run(); err != nil {
		log.Printf("demo-go-api: %v", err)
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

	password, err := demokit.ReadSecretFile("DEMO_REDIS_PASSWORD_FILE")
	if err != nil {
		return err
	}
	redisAddr := demokit.EnvOr("DEMO_REDIS_ADDR", "demo-redis:6379")
	// Maintenance notifications off: go-redis v9.23 otherwise sends CLIENT
	// MAINT_NOTIFICATIONS ON per new connection, which Redis 7.4 rejects with
	// ERR, leaving an ERROR span per connection (DE3a smoke, 2026-11-16).
	rdb := redis.NewClient(&redis.Options{
		Addr:                     redisAddr,
		Password:                 password,
		MaintNotificationsConfig: &maintnotifications.Config{Mode: maintnotifications.ModeDisabled},
	})
	defer func() { _ = rdb.Close() }()
	if err := otelredis.Instrument(rdb); err != nil {
		return fmt.Errorf("instrument redis: %w", err)
	}

	// Plaintext inside the cluster: the mesh (ztunnel) encrypts pod traffic.
	conn, err := grpc.NewClient(demokit.EnvOr("DEMO_BACKEND_ADDR", "demo-"+demokit.Lang+"-backend:50051"),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		otelhelper.GRPCDialOption())
	if err != nil {
		return fmt.Errorf("grpc client: %w", err)
	}
	defer func() { _ = conn.Close() }()

	host, port := demokit.HostPort(redisAddr, 6379)
	a := &api{
		logger:    logger,
		tracer:    otelhelper.GetTracer(demokit.TracerName),
		cache:     redisCache{rdb},
		queue:     redisQueue{rdb},
		orders:    demov1.NewOrderServiceClient(conn),
		run:       demokit.RunID(),
		redisHost: host,
		redisPort: port,
		ready:     newReadiness(readyCheck(rdb, healthpb.NewHealthClient(conn))),
	}

	srv := &http.Server{
		Addr:              ":" + demokit.EnvOr("DEMO_HTTP_PORT", "8080"),
		Handler:           a.routes(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	srvErr := make(chan error, 1)
	go func() { srvErr <- srv.ListenAndServe() }()
	logger.Info("demo api listening", "server.address", srv.Addr)

	var runErr error
	select {
	case <-ctx.Done():
	case runErr = <-srvErr:
		if errors.Is(runErr, http.ErrServerClosed) {
			runErr = nil
		}
	}
	drainCtx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	_ = srv.Shutdown(drainCtx)
	return runErr
}

// readyCheck reports whether Redis answers PING and the backend's
// grpc.health.v1 answers SERVING, each within one second (DE-D11 r3.6). It
// runs untraced; the gRPC client handler also drops health RPCs.
func readyCheck(rdb *redis.Client, health healthpb.HealthClient) func(ctx context.Context) bool {
	return func(ctx context.Context) bool {
		ctx, cancel := context.WithTimeout(demokit.Untraced(ctx), time.Second)
		defer cancel()
		if rdb.Ping(ctx).Err() != nil {
			return false
		}
		resp, err := health.Check(ctx, &healthpb.HealthCheckRequest{})
		return err == nil && resp.GetStatus() == healthpb.HealthCheckResponse_SERVING
	}
}

// redisCache is the quote cache on the instrumented client.
type redisCache struct{ rdb *redis.Client }

// Get returns the cached quote; found is false on a miss.
func (c redisCache) Get(ctx context.Context, key string) (string, bool, error) {
	v, err := c.rdb.Get(ctx, key).Result()
	if errors.Is(err, redis.Nil) {
		return "", false, nil
	}
	return v, err == nil, err
}

func (c redisCache) Set(ctx context.Context, key, value string, ttl time.Duration) error {
	return c.rdb.Set(ctx, key, value, ttl).Err()
}

// redisQueue appends order events to the stream.
type redisQueue struct{ rdb *redis.Client }

func (q redisQueue) Add(ctx context.Context, stream string, values map[string]any) (string, error) {
	return q.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: stream,
		MaxLen: demokit.StreamMaxLen,
		Approx: true,
		Values: values,
	}).Result()
}
