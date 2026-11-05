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
// redisotel sets, unless OTEL_SEMCONV_STABILITY_OPT_IN holds database without
// database/dup), db.operation.name on command spans (go-redis's lower-case
// command name, e.g. "set", "cluster info"), server.address/server.port, and never the command values
// (no db.statement) or the connection string (db.connection_string is
// removed). A failed command keeps status Error with an empty description
// (contract D1 "DB error status", task A26): redisotel writes the server's
// error text there, and Redis errors can quote arguments ("ERR unknown
// command 'X', with args beginning with: '<arg>'"). The exception event
// (span.RecordError) keeps that text: the accepted A25 residual. A known
// reply code (WRONGTYPE, ERR, NOAUTH, ...) becomes db.response.status_code
// and error.type (_OTHER otherwise). The pool metrics keep the legacy
// db.system in every mode (redisotel adds it to their attributes).
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
	"os"
	"regexp"
	"strings"

	"github.com/redis/go-redis/extra/redisotel/v9"
	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// connectionStringKey is the legacy db.connection_string attribute redisotel
// always sets; the helper strips it so no connection details leave the span.
const connectionStringKey attribute.Key = "db.connection_string"

// legacySystemKey is the legacy db.system attribute redisotel always sets
// (config.go newConfig); dropped from spans in the stable database mode (A26).
const legacySystemKey attribute.Key = "db.system"

// dbSystemName is semconv v1.43.0 db.system.name for Redis.
var dbSystemName = attribute.String("db.system.name", "redis")

// envSemconvOptIn and the rule below are ext/otelsql's (a separate module, so
// copied): legacy keys unless the value holds database without database/dup,
// tokens trimmed with the contract set and ASCII-folded (A25).
const envSemconvOptIn = "OTEL_SEMCONV_STABILITY_OPT_IN"

const trimChars = " \t\n\r\v\f"

func asciiLower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if 'A' <= c && c <= 'Z' {
			b[i] = c + ('a' - 'A')
		}
	}
	return string(b)
}

func legacyKeysEnabled() bool {
	var stable, dup bool
	for _, v := range strings.Split(os.Getenv(envSemconvOptIn), ",") {
		switch asciiLower(strings.Trim(v, trimChars)) {
		case "database":
			stable = true
		case "database/dup":
			dup = true
		}
	}
	return dup || !stable
}

// errorPrefix is a Redis error reply's leading token; it becomes
// db.response.status_code and error.type only when it is a known reply code
// (A26 final round, 2026-11-03: a Lua error_reply's first word is
// arbitrary — data or a high-cardinality value — so it is _OTHER).
var errorPrefix = regexp.MustCompile(`^([A-Z][A-Z0-9_]{0,31}) `)

// replyCodes is the allowlist shared with Python and Node.js (contract design
// D1 "DB error status"): the error codes Redis 7.4 sends (src/*.c, e.g.
// server.c:1875 NOSCRIPT, cluster.c:1186 CROSSSLOT, cluster.c:1205 MOVED/ASK)
// plus redis-py's BaseParser.EXCEPTION_CLASSES keys.
var replyCodes = map[string]bool{
	"ASK": true, "BUSY": true, "BUSYGROUP": true, "BUSYKEY": true, "CLUSTERDOWN": true, "CROSSSLOT": true,
	"DENIED": true, "ERR": true, "EXECABORT": true, "INPROG": true, "INVALIDOBJ": true, "IOERR": true,
	"LOADING": true, "MASTERDOWN": true, "MISCONF": true, "MOVED": true, "NOAUTH": true, "NOGOODSLAVE": true,
	"NOGROUP": true, "NOMASTERLINK": true, "NOPERM": true, "NOPROTO": true, "NOQUORUM": true, "NOREPLICAS": true,
	"NOSCRIPT": true, "NOTBUSY": true, "OOM": true, "READONLY": true, "TRYAGAIN": true, "UNBLOCKED": true,
	"UNKILLABLE": true, "WRONGPASS": true, "WRONGTYPE": true,
}

// replyCode is the known reply code leading description, or "".
func replyCode(description string) string {
	if m := errorPrefix.FindStringSubmatch(description); m != nil && replyCodes[m[1]] {
		return m[1]
	}
	return ""
}

// Instrument adds OpenTelemetry tracing hooks and connection pool metrics to a
// go-redis v9 client (redis.Client, redis.ClusterClient or redis.Ring). Both
// use the global tracer and meter providers, so call it after the helper's
// Setup, and only once per client (each call adds another hook).
// OTEL_SEMCONV_STABILITY_OPT_IN is read at each call, as otelsql.Open.
//
// Tracing is installed first: when the returned error comes from the metrics
// step, the client is already traced.
func Instrument(client redis.UniversalClient) error {
	if err := redisotel.InstrumentTracing(client,
		redisotel.WithTracerProvider(sanitizingProvider{TracerProvider: otel.GetTracerProvider(), legacyKeys: legacyKeysEnabled()}),
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
// db.connection_string (and without db.system in the stable database mode),
// with db.operation.name on command spans, and drop the description of an
// Error status (redisotel v9.23 tracing.go:187-190 recordError:
// span.SetStatus(codes.Error, err.Error())); everything else is delegated
// unchanged.
type sanitizingProvider struct {
	trace.TracerProvider
	legacyKeys bool
}

func (p sanitizingProvider) Tracer(name string, opts ...trace.TracerOption) trace.Tracer {
	return sanitizingTracer{Tracer: p.TracerProvider.Tracer(name, opts...), legacyKeys: p.legacyKeys}
}

type sanitizingTracer struct {
	trace.Tracer
	legacyKeys bool
}

func (t sanitizingTracer) Start(ctx context.Context, name string, opts ...trace.SpanStartOption) (context.Context, trace.Span) {
	cfg := trace.NewSpanStartConfig(opts...)
	attrs := make([]attribute.KeyValue, 0, len(cfg.Attributes())+1)
	for _, kv := range cfg.Attributes() {
		if kv.Key == connectionStringKey || (!t.legacyKeys && kv.Key == legacySystemKey) {
			continue
		}
		attrs = append(attrs, kv)
	}
	// Command spans are named cmd.FullName() (tracing.go:134): go-redis
	// lowercases the command and adds the subcommand only for cluster and
	// command ("set", "cluster info"; CLIENT SETINFO is "client"). semconv
	// makes db.operation.name Required when known. Pipeline and dial spans
	// ("redis.pipeline ...", "redis.dial") have none; an empty name is skipped.
	if name != "" && !strings.HasPrefix(name, "redis.") {
		attrs = append(attrs, attribute.String("db.operation.name", name))
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
	ctx, span := t.Tracer.Start(ctx, name, out...)
	// redisotel uses only the returned span (End at tracing.go:96, 135, 177;
	// recordError at 100, 138, 180, which calls RecordError and SetStatus at
	// 189-190), so the wrapper is the handle and the context keeps the
	// original SDK span: children started from ctx see the SDK span as their
	// parent, as without the helper.
	return ctx, statusSanitizingSpan{span}
}

// statusSanitizingSpan keeps an Error code and drops its description (A26);
// a known reply code becomes db.response.status_code and error.type
// (_OTHER otherwise: a dial error, a Lua error_reply's own first word).
type statusSanitizingSpan struct{ trace.Span }

func (s statusSanitizingSpan) SetStatus(code codes.Code, description string) {
	if code != codes.Error {
		s.Span.SetStatus(code, description)
		return
	}
	if code := replyCode(description); code != "" {
		s.Span.SetAttributes(attribute.String("db.response.status_code", code), attribute.String("error.type", code))
	} else {
		s.Span.SetAttributes(attribute.String("error.type", "_OTHER"))
	}
	s.Span.SetStatus(codes.Error, "")
}
