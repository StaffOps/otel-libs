// Package otelhelper provides a single-call OpenTelemetry setup for Go services.
//
// Usage:
//
//	shutdown, err := otelhelper.Setup(ctx)
//	if err != nil { log.Fatal(err) }
//	defer func() {
//		// Use a fresh context: ctx may already be cancelled at exit, and
//		// shutdown needs time to flush buffered telemetry.
//		sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
//		defer cancel()
//		_ = shutdown(sctx)
//	}()
//
// # Opt-in instrumentation (ext/)
//
// Opt-in instrumentations live in separate sub-modules under ext/.
// Import only what you need — each sub-module has its own go.mod so
// it does not pull transitive dependencies into your main module.
//
// Available extensions:
//
//	github.com/staffops/staffops-otel-libs/go/ext/otelaws   — AWS SDK v2 tracing
//	github.com/staffops/staffops-otel-libs/go/ext/otelredis — go-redis v9 tracing
//	github.com/staffops/staffops-otel-libs/go/ext/otelsql   — database/sql tracing + metrics
//
// Example (AWS):
//
//	import "github.com/staffops/staffops-otel-libs/go/ext/otelaws"
//
//	cfg, _ := config.LoadDefaultConfig(ctx)
//	otelaws.Instrument(&cfg)
//
// Example (Redis):
//
//	import "github.com/staffops/staffops-otel-libs/go/ext/otelredis"
//
//	rdb := redis.NewClient(&redis.Options{Addr: "localhost:6379"})
//	if err := otelredis.Instrument(rdb); err != nil { ... }
//
// Example (SQL):
//
//	import "github.com/staffops/staffops-otel-libs/go/ext/otelsql"
//
//	db, closeDB, err := otelsql.Open("postgres", dsn)
//	if err != nil { ... }
//	defer closeDB() // closes db and unregisters its connection-pool metrics
package otelhelper
