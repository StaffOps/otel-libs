package otelredis

import (
	"context"
	"net"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func installProviders(t *testing.T) (*tracetest.InMemoryExporter, *sdkmetric.ManualReader) {
	t.Helper()
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	prevTP, prevMP := otel.GetTracerProvider(), otel.GetMeterProvider()
	otel.SetTracerProvider(tp)
	otel.SetMeterProvider(mp)
	t.Cleanup(func() {
		otel.SetTracerProvider(prevTP)
		otel.SetMeterProvider(prevMP)
		_ = tp.Shutdown(context.Background())
		_ = mp.Shutdown(context.Background())
	})
	return exp, reader
}

func TestInstrumentEmitsSpansAndPoolMetrics(t *testing.T) {
	exp, reader := installProviders(t)
	srv := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: srv.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	if err := Instrument(rdb); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if err := rdb.Set(ctx, "k", "secret-value", 0).Err(); err != nil {
		t.Fatal(err)
	}
	if got, err := rdb.Get(ctx, "k").Result(); err != nil || got != "secret-value" {
		t.Fatalf("GET = %q, %v", got, err)
	}

	spans := exp.GetSpans()
	var names []string
	for _, sp := range spans {
		names = append(names, sp.Name)
		set := attribute.NewSet(sp.Attributes...)
		if v, _ := set.Value("db.system.name"); v.AsString() != "redis" {
			t.Errorf("span %q db.system.name = %q, want redis", sp.Name, v.AsString())
		}
		for _, k := range []attribute.Key{"db.statement", "db.query.text", "db.connection_string"} {
			if v, ok := set.Value(k); ok {
				t.Errorf("span %q carries %s=%q; command values and connection strings must not be recorded", sp.Name, k, v.Emit())
			}
		}
		for _, kv := range sp.Attributes {
			if strings.Contains(kv.Value.Emit(), "secret-value") {
				t.Errorf("span %q attribute %s leaks the command value", sp.Name, kv.Key)
			}
		}
	}
	if !contains(names, "set") || !contains(names, "get") {
		t.Errorf("spans = %v, want set and get", names)
	}

	var rm metricdata.ResourceMetrics
	if err := reader.Collect(ctx, &rm); err != nil {
		t.Fatal(err)
	}
	var metrics []string
	var waits metricdata.Aggregation
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			metrics = append(metrics, m.Name)
			if m.Name == "db.client.connections.waits" {
				waits = m.Data
			}
		}
	}
	// Failed before A10: only tracing was instrumented, no pool metrics.
	if !contains(metrics, "db.client.connections.usage") {
		t.Errorf("metrics = %v, want db.client.connections.usage", metrics)
	}
	// WithSemConvCompliantMetrics: cumulative pool stats are monotonic counters.
	if sum, ok := waits.(metricdata.Sum[int64]); !ok || !sum.IsMonotonic {
		t.Errorf("db.client.connections.waits = %T %+v, want a monotonic Sum", waits, waits)
	}
}

func TestInstrumentClusterClient(t *testing.T) {
	installProviders(t)
	srv := miniredis.RunT(t)
	rdb := redis.NewClusterClient(&redis.ClusterOptions{Addrs: []string{srv.Addr()}})
	t.Cleanup(func() { _ = rdb.Close() })
	if err := Instrument(rdb); err != nil {
		t.Fatalf("Instrument(ClusterClient) = %v", err)
	}
}

// An unsupported UniversalClient implementation is reported, not ignored.
type fakeClient struct{ redis.UniversalClient }

func TestInstrumentUnsupportedClientErrors(t *testing.T) {
	installProviders(t)
	if err := Instrument(fakeClient{}); err == nil {
		t.Fatal("want an error for an unsupported client type")
	}
}

func contains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

func TestSanitizingTracerKeepsStartOptions(t *testing.T) {
	exp, _ := installProviders(t)
	tr := sanitizingProvider{TracerProvider: otel.GetTracerProvider()}.Tracer("t")
	ts := time.Unix(1700000000, 0)
	_, sp := tr.Start(context.Background(), "op",
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(attribute.String("db.connection_string", "redis://h:1"), attribute.String("keep", "1")),
		trace.WithTimestamp(ts),
		trace.WithNewRoot(),
	)
	sp.End()
	got := exp.GetSpans()[0]
	set := attribute.NewSet(got.Attributes...)
	if _, ok := set.Value("db.connection_string"); ok {
		t.Error("db.connection_string not removed")
	}
	if v, _ := set.Value("keep"); v.AsString() != "1" {
		t.Error("other attributes must be kept")
	}
	if got.SpanKind != trace.SpanKindClient || !got.StartTime.Equal(ts) || got.Parent.IsValid() {
		t.Errorf("kind/timestamp/new-root not preserved: %v %v %v", got.SpanKind, got.StartTime, got.Parent)
	}
}

// A26 (2026-11-03): a failed command keeps status Error with an empty
// description and carries the reply's code as db.response.status_code and
// error.type; the server text (which quotes arguments) never reaches the
// status or an attribute. Against miniredis, and against a real Redis when
// OTELREDIS_TEST_ADDR (host:port) and OTELREDIS_TEST_PASSWORD are set
// (testing/docker-compose.yml go-tests, CI go-test).
type target struct{ name, addr, password string }

func targets(t *testing.T) []target {
	t.Helper()
	out := []target{{"miniredis", miniredis.RunT(t).Addr(), ""}}
	if addr := os.Getenv("OTELREDIS_TEST_ADDR"); addr != "" {
		out = append(out, target{"redis", addr, os.Getenv("OTELREDIS_TEST_PASSWORD")})
	}
	return out
}

const secret = "secret-quoted-arg"

func assertNoSecret(t *testing.T, spans tracetest.SpanStubs) {
	t.Helper()
	for _, sp := range spans {
		if strings.Contains(sp.Status.Description, secret) || strings.Contains(sp.Name, secret) {
			t.Errorf("span %q leaks the argument in its name or status %q", sp.Name, sp.Status.Description)
		}
		for _, kv := range sp.Attributes {
			if strings.Contains(kv.Value.Emit(), secret) {
				t.Errorf("span %q attribute %s leaks the argument", sp.Name, kv.Key)
			}
		}
	}
}

func assertNoValue(t *testing.T, spans tracetest.SpanStubs, value string) {
	t.Helper()
	for _, sp := range spans {
		for _, kv := range sp.Attributes {
			if strings.Contains(kv.Value.Emit(), value) {
				t.Errorf("span %q attribute %s carries %q", sp.Name, kv.Key, value)
			}
		}
	}
}

func spanByName(t *testing.T, spans tracetest.SpanStubs, name string) tracetest.SpanStub {
	t.Helper()
	for _, sp := range spans {
		if sp.Name == name {
			return sp
		}
	}
	t.Fatalf("no span %q in %d spans", name, len(spans))
	return tracetest.SpanStub{}
}

func attr(sp tracetest.SpanStub, key string) (string, bool) {
	set := attribute.NewSet(sp.Attributes...)
	v, ok := set.Value(attribute.Key(key))
	return v.Emit(), ok
}

func TestErrorStatusDropsDescription(t *testing.T) {
	for _, tc := range targets(t) {
		t.Run(tc.name, func(t *testing.T) {
			exp, _ := installProviders(t)
			rdb := redis.NewClient(&redis.Options{Addr: tc.addr, Password: tc.password})
			t.Cleanup(func() { _ = rdb.Close() })
			if err := Instrument(rdb); err != nil {
				t.Fatal(err)
			}
			ctx := context.Background()
			if err := rdb.Set(ctx, "a26-go-key", "v", 0).Err(); err != nil {
				t.Fatal(err)
			}
			if err := rdb.LPush(ctx, "a26-go-key", "x").Err(); err == nil || !strings.Contains(err.Error(), "WRONGTYPE") {
				t.Fatalf("LPUSH err = %v, want WRONGTYPE", err)
			}
			if err := rdb.Do(ctx, "NOSUCHCMD", secret).Err(); err == nil {
				t.Fatal("want an unknown-command error")
			}
			// A Lua error quotes the script (here the secret) in the reply.
			if err := rdb.Eval(ctx, "return redis.error_reply('ERR "+secret+"')", nil).Err(); err == nil {
				t.Fatal("want a script error")
			}
			// A Lua reply whose first word is data: never an attribute (A26 final round, R1).
			if err := rdb.EvalRO(ctx, "return redis.error_reply(ARGV[1] .. ' bad')", nil, "SECRET_TOKEN42").Err(); err == nil {
				t.Fatal("want a script error")
			}
			_, err := rdb.Pipelined(ctx, func(p redis.Pipeliner) error {
				p.Set(ctx, "a26-go-pipe", secret, 0)
				p.LPush(ctx, "a26-go-key", secret)
				return nil
			})
			if err == nil {
				t.Fatal("want a pipeline error (WRONGTYPE)")
			}
			spans := exp.GetSpans()
			assertNoSecret(t, spans)
			for _, tt := range []struct{ span, code string }{{"lpush", "WRONGTYPE"}, {"nosuchcmd", "ERR"}, {"eval", "ERR"}} {
				sp := spanByName(t, spans, tt.span)
				if sp.Status.Code != codes.Error || sp.Status.Description != "" {
					t.Errorf("%s status = %+v, want Error with no description", tt.span, sp.Status)
				}
				if v, _ := attr(sp, "db.response.status_code"); v != tt.code {
					t.Errorf("%s db.response.status_code = %q, want %q", tt.span, v, tt.code)
				}
				if v, _ := attr(sp, "error.type"); v != tt.code {
					t.Errorf("%s error.type = %q, want %q", tt.span, v, tt.code)
				}
				if v, _ := attr(sp, "db.operation.name"); v != tt.span {
					t.Errorf("%s db.operation.name = %q, want the command as provided", tt.span, v)
				}
			}
			pipe := spanByName(t, spans, "redis.pipeline set lpush")
			if pipe.Status.Code != codes.Error || pipe.Status.Description != "" {
				t.Errorf("pipeline status = %+v, want Error with no description", pipe.Status)
			}
			if _, ok := attr(pipe, "db.operation.name"); ok {
				t.Error("pipeline spans carry no db.operation.name")
			}
			custom := spanByName(t, spans, "eval_ro")
			if v, _ := attr(custom, "error.type"); v != "_OTHER" {
				t.Errorf("eval_ro error.type = %q, want _OTHER (not an allowlisted reply code)", v)
			}
			if _, ok := attr(custom, "db.response.status_code"); ok {
				t.Error("eval_ro: no db.response.status_code for a custom first word")
			}
			assertNoValue(t, spans, "SECRET_TOKEN42")
			if sp := spanByName(t, spans, "set"); sp.Status.Code != codes.Unset {
				t.Errorf("set status = %+v, want Unset (passed through)", sp.Status)
			}
		})
	}
}

// A dial to a closed port: the error text has no reply code, so error.type is _OTHER.
func TestDialErrorStatus(t *testing.T) {
	exp, _ := installProviders(t)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()
	rdb := redis.NewClient(&redis.Options{Addr: addr, MaxRetries: -1})
	t.Cleanup(func() { _ = rdb.Close() })
	if err := Instrument(rdb); err != nil {
		t.Fatal(err)
	}
	if err := rdb.Get(context.Background(), "k").Err(); err == nil {
		t.Fatal("want a dial error")
	}
	dial := spanByName(t, exp.GetSpans(), "redis.dial")
	if dial.Status.Code != codes.Error || dial.Status.Description != "" {
		t.Errorf("dial status = %+v, want Error with no description", dial.Status)
	}
	if v, _ := attr(dial, "error.type"); v != "_OTHER" {
		t.Errorf("dial error.type = %q, want _OTHER", v)
	}
	if _, ok := attr(dial, "db.response.status_code"); ok {
		t.Error("a dial error has no db.response.status_code")
	}
}

// A26 (obs M1): the stable database mode drops the legacy db.system from spans.
func TestSemconvStableModeDropsLegacySystem(t *testing.T) {
	for _, tt := range []struct {
		value  string
		legacy bool
	}{{"", true}, {"http", true}, {"database", false}, {" DATABASE ", false}, {"database,database/dup", true}, {"database ", true}} {
		t.Run(tt.value, func(t *testing.T) {
			t.Setenv(envSemconvOptIn, tt.value)
			if got := legacyKeysEnabled(); got != tt.legacy {
				t.Fatalf("legacyKeysEnabled(%q) = %v, want %v", tt.value, got, tt.legacy)
			}
			exp, _ := installProviders(t)
			rdb := redis.NewClient(&redis.Options{Addr: miniredis.RunT(t).Addr()})
			t.Cleanup(func() { _ = rdb.Close() })
			if err := Instrument(rdb); err != nil {
				t.Fatal(err)
			}
			_ = rdb.Get(context.Background(), "k")
			sp := spanByName(t, exp.GetSpans(), "get")
			if _, ok := attr(sp, "db.system"); ok != tt.legacy {
				t.Errorf("db.system present = %v, want %v", ok, tt.legacy)
			}
			if v, _ := attr(sp, "db.system.name"); v != "redis" {
				t.Errorf("db.system.name = %q", v)
			}
		})
	}
}

func TestReplyCodeAllowlist(t *testing.T) {
	for _, tt := range []struct{ in, want string }{
		{"WRONGTYPE Operation against a key", "WRONGTYPE"},
		{"ERR unknown command", "ERR"},
		{"NOSCRIPT No matching script", "NOSCRIPT"},
		{"MOVED 3999 127.0.0.1:6381", "MOVED"},
		{"SECRET_TOKEN42 bad", ""},
		{"MYCODE custom", ""},
		{"dial tcp: connection refused", ""},
		{"NOTREADY x", ""},
	} {
		if got := replyCode(tt.in); got != tt.want {
			t.Errorf("replyCode(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

func TestEmptyOperationNameSkipped(t *testing.T) {
	exp, _ := installProviders(t)
	_, sp := sanitizingProvider{TracerProvider: otel.GetTracerProvider()}.Tracer("t").Start(context.Background(), "")
	sp.End()
	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("spans = %d", len(spans))
	}
	if _, ok := attr(spans[0], "db.operation.name"); ok {
		t.Error("an empty name gives no db.operation.name")
	}
}

func TestStatusSanitizingSpanPassesOtherStatuses(t *testing.T) {
	exp, _ := installProviders(t)
	tr := sanitizingProvider{TracerProvider: otel.GetTracerProvider(), legacyKeys: true}.Tracer("t")
	for _, code := range []codes.Code{codes.Ok, codes.Unset} {
		ctx, sp := tr.Start(context.Background(), "op")
		if !trace.SpanFromContext(ctx).SpanContext().Equal(sp.SpanContext()) {
			t.Error("the context must carry the same span (the SDK one)")
		}
		if _, ok := sp.(statusSanitizingSpan); !ok {
			t.Error("the handle must be the wrapper")
		}
		sp.SetStatus(code, "fine")
		sp.End()
	}
	spans := exp.GetSpans()
	if len(spans) != 2 {
		t.Fatalf("spans = %d, want 2", len(spans))
	}
	if spans[0].Status.Code != codes.Ok || spans[1].Status.Code != codes.Unset {
		t.Errorf("statuses = %+v %+v, want Ok then Unset", spans[0].Status, spans[1].Status)
	}
	if _, ok := attr(spans[0], "error.type"); ok {
		t.Error("no error.type on a non-error status")
	}
}
