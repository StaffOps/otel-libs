package otelhelper

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
//	otelredis.Instrument(rdb)
//
// Example (SQL):
//
//	import "github.com/staffops/staffops-otel-libs/go/ext/otelsql"
//
//	db, err := otelsql.Open("postgres", dsn)
