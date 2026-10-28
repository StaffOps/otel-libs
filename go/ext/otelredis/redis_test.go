package otelredis

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
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
	tr := sanitizingProvider{otel.GetTracerProvider()}.Tracer("t")
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
