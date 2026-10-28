// Package otelredis provides OpenTelemetry instrumentation for go-redis v9.
//
// Usage:
//
//	rdb := redis.NewClient(&redis.Options{Addr: "localhost:6379"})
//	if err := otelredis.Instrument(rdb); err != nil { ... }
//	// All subsequent Redis commands produce spans, and the client's
//	// connection pool reports db.client.connections.* metrics.
//
// Spans carry db.system.name=redis (plus the legacy db.system=redis that
// redisotel sets), server.address/server.port, and never the command values
// (no db.statement) or the connection string (db.connection_string is
// removed).
//
// Pool metrics (redisotel v9.23, semantic-convention-compliant instrument
// types), with the names the helper's Prometheus /metrics endpoint serves:
//
//	db.client.connections.usage           db_client_connections_usage
//	db.client.connections.max             db_client_connections_max
//	db.client.connections.idle.max        db_client_connections_idle_max
//	db.client.connections.idle.min        db_client_connections_idle_min
//	db.client.connections.waits           db_client_connections_waits_total
//	db.client.connections.waits_duration  db_client_connections_waits_duration_nanoseconds
//	db.client.connections.timeouts        db_client_connections_timeouts_total
//	db.client.connections.hits            db_client_connections_hits_total
//	db.client.connections.misses          db_client_connections_misses_total
//	db.client.connections.create_time     db_client_connections_create_time_milliseconds_{bucket,sum,count}
//	db.client.connections.use_time        db_client_connections_use_time_milliseconds_{bucket,sum,count}
package otelredis

import (
	"context"
	"fmt"

	"github.com/redis/go-redis/extra/redisotel/v9"
	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

// connectionStringKey is the legacy db.connection_string attribute redisotel
// always sets; the helper strips it so no connection details leave the span.
const connectionStringKey attribute.Key = "db.connection_string"

// dbSystemName is semconv v1.43.0 db.system.name for Redis.
var dbSystemName = attribute.String("db.system.name", "redis")

// Instrument adds OpenTelemetry tracing hooks and connection pool metrics to a
// go-redis v9 client (redis.Client, redis.ClusterClient or redis.Ring). Both
// use the global tracer and meter providers, so call it after the helper's
// Setup, and only once per client (each call adds another hook).
//
// Tracing is installed first: when the returned error comes from the metrics
// step, the client is already traced.
func Instrument(client redis.UniversalClient) error {
	if err := redisotel.InstrumentTracing(client,
		redisotel.WithTracerProvider(sanitizingProvider{otel.GetTracerProvider()}),
		redisotel.WithDBStatement(false),
		redisotel.WithAttributes(dbSystemName),
	); err != nil {
		return err
	}
	if err := redisotel.InstrumentMetrics(client, redisotel.WithSemConvCompliantMetrics(true)); err != nil {
		return fmt.Errorf("otelredis: pool metrics (tracing is installed): %w", err)
	}
	return nil
}

// sanitizingProvider hands redisotel tracers whose spans start without
// db.connection_string; everything else is delegated unchanged.
type sanitizingProvider struct{ trace.TracerProvider }

func (p sanitizingProvider) Tracer(name string, opts ...trace.TracerOption) trace.Tracer {
	return sanitizingTracer{p.TracerProvider.Tracer(name, opts...)}
}

type sanitizingTracer struct{ trace.Tracer }

func (t sanitizingTracer) Start(ctx context.Context, name string, opts ...trace.SpanStartOption) (context.Context, trace.Span) {
	cfg := trace.NewSpanStartConfig(opts...)
	attrs := make([]attribute.KeyValue, 0, len(cfg.Attributes()))
	for _, kv := range cfg.Attributes() {
		if kv.Key != connectionStringKey {
			attrs = append(attrs, kv)
		}
	}
	out := []trace.SpanStartOption{
		trace.WithAttributes(attrs...),
		trace.WithSpanKind(cfg.SpanKind()),
		trace.WithLinks(cfg.Links()...),
	}
	if ts := cfg.Timestamp(); !ts.IsZero() {
		out = append(out, trace.WithTimestamp(ts))
	}
	if cfg.NewRoot() {
		out = append(out, trace.WithNewRoot())
	}
	return t.Tracer.Start(ctx, name, out...)
}
