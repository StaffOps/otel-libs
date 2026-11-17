package demokit

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/staffops/otel-libs/go/demo/internal/demotest"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/trace"
)

func TestScenarioAtFollowsTheCycle(t *testing.T) {
	want := map[uint64]string{
		1: ScenarioOK, 2: ScenarioOK, 3: ScenarioBusinessError, 4: ScenarioOK, 5: ScenarioOK,
		6: ScenarioDBError, 7: ScenarioOK, 8: ScenarioOK, 9: ScenarioUnhandled, 10: ScenarioOK,
		11: ScenarioOK, 13: ScenarioBusinessError, 16: ScenarioDBError, 19: ScenarioUnhandled, 0: ScenarioOK,
	}
	for seq, s := range want {
		if got := ScenarioAt(seq); got != s {
			t.Errorf("ScenarioAt(%d) = %q, want %q", seq, got, s)
		}
	}
	// One of each error and seven ok per 10 (DE-D7).
	counts := map[string]int{}
	for seq := uint64(1); seq <= 50; seq++ {
		counts[ScenarioAt(seq)]++
	}
	if counts[ScenarioOK] != 35 || counts[ScenarioBusinessError] != 5 || counts[ScenarioDBError] != 5 || counts[ScenarioUnhandled] != 5 {
		t.Errorf("counts over 50 = %v", counts)
	}
}

func TestValidScenario(t *testing.T) {
	for _, s := range []string{ScenarioOK, ScenarioBusinessError, ScenarioDBError, ScenarioUnhandled} {
		if !ValidScenario(s) {
			t.Errorf("ValidScenario(%q) = false", s)
		}
	}
	for _, s := range []string{"", "OK", "other"} {
		if ValidScenario(s) {
			t.Errorf("ValidScenario(%q) = true", s)
		}
	}
}

func TestOrderFor(t *testing.T) {
	ok := OrderFor(1)
	if ok.Scenario != ScenarioOK || ok.OrderID != "ok-1" || ok.Amount <= 0 || ok.Amount > MaxOrderAmount {
		t.Errorf("OrderFor(1) = %+v", ok)
	}
	if got := OrderFor(501).OrderID; got != "ok-1" {
		t.Errorf("ok ids must wrap at 500: OrderFor(501).OrderID = %q", got)
	}
	rej := OrderFor(3)
	if rej.Scenario != ScenarioBusinessError || rej.Amount <= MaxOrderAmount {
		t.Errorf("business_error must exceed the limit: %+v", rej)
	}
	for _, seq := range []uint64{6, 9} {
		o := OrderFor(seq)
		if o.Amount > MaxOrderAmount || !strings.HasPrefix(o.OrderID, "ord-") {
			t.Errorf("OrderFor(%d) = %+v", seq, o)
		}
	}
	// Customers cycle over three values; no id carries a run token prefix.
	seen := map[string]bool{}
	for seq := uint64(1); seq <= 30; seq++ {
		o := OrderFor(seq)
		seen[o.Customer] = true
		for _, p := range []string{"dup-", "rej-", "unh-"} {
			if strings.HasPrefix(o.OrderID, p) {
				t.Errorf("OrderFor(%d).OrderID = %q carries a run token prefix", seq, o.OrderID)
			}
		}
	}
	if len(seen) != 3 || !seen["cust-a"] || !seen["cust-b"] || !seen["cust-c"] {
		t.Errorf("customers = %v, want 3", seen)
	}
}

func TestNames(t *testing.T) {
	if StreamKey != "demo:go:orders" || QuoteKey("c") != "demo:go:quote:c" || DuplicateID("r1") != "dup-r1" {
		t.Errorf("names: %q %q %q", StreamKey, QuoteKey("c"), DuplicateID("r1"))
	}
}

func TestErrorTypeAndMessages(t *testing.T) {
	driver := errors.New("ERROR: duplicate order dup-r1 (SQLSTATE 23505)")
	cases := []struct {
		err      error
		typ, msg string
	}{
		{&OrderRejectedError{Run: "r1"}, TypeOrderRejected, "amount over limit rej-r1"},
		{&OrderExistsError{ID: "dup-r1", Err: driver}, TypeOrderExists, "order dup-r1 exists"},
		{&DemoUnhandledError{Run: "r1"}, TypeDemoUnhandled, "boom unh-r1"},
		{fmt.Errorf("wrapped: %w", &OrderRejectedError{Run: "r1"}), TypeOrderRejected, "wrapped: amount over limit rej-r1"},
		{errors.New("other"), "*errors.errorString", "other"},
	}
	for _, c := range cases {
		if got := ErrorType(c.err); got != c.typ {
			t.Errorf("ErrorType(%v) = %q, want %q", c.err, got, c.typ)
		}
		if c.err.Error() != c.msg {
			t.Errorf("message = %q, want %q", c.err.Error(), c.msg)
		}
	}
	if ErrorType(nil) != "" {
		t.Error("ErrorType(nil) must be empty")
	}
	if !errors.Is(&OrderExistsError{ID: "x", Err: driver}, driver) {
		t.Error("OrderExistsError must unwrap to the driver error")
	}
}

func TestUntracedRecordsNothing(t *testing.T) {
	tracer, rec := demotest.Tracer() // default sampler: ParentBased(AlwaysSample)
	ctx := Untraced(context.Background())
	if sc := trace.SpanContextFromContext(ctx); !sc.IsValid() || sc.IsSampled() {
		t.Fatalf("Untraced parent = %+v, want valid and unsampled", sc)
	}
	_, span := tracer.Start(ctx, "XREADGROUP")
	if span.IsRecording() {
		t.Error("a span under Untraced must not record")
	}
	span.End()
	if n := len(rec.Ended()); n != 0 {
		t.Errorf("ended spans = %d, want 0", n)
	}
	if a, b := trace.SpanContextFromContext(Untraced(context.Background())), trace.SpanContextFromContext(ctx); a.TraceID() == b.TraceID() {
		t.Error("each Untraced context must get its own ids")
	}
}

func TestTypeNameUsesTheRootError(t *testing.T) {
	op := &net.OpError{Op: "dial", Net: "tcp", Err: errors.New("connection refused")}
	transport := &url.Error{Op: "Post", URL: "http://api/orders", Err: op}
	if got := TypeName(transport); got != "*errors.errorString" {
		t.Errorf("TypeName(url.Error→OpError→errorString) = %q", got)
	}
	if got := TypeName(&url.Error{Op: "Post", URL: "x", Err: &net.DNSError{Err: "no such host"}}); got != "*net.DNSError" {
		t.Errorf("TypeName = %q, want *net.DNSError", got)
	}
	if got := ErrorType(fmt.Errorf("x: %w", &net.DNSError{})); got != "*net.DNSError" {
		t.Errorf("ErrorType fallback = %q", got)
	}
}

func TestRecordFailureFollowsADR014(t *testing.T) {
	tracer, rec := demotest.Tracer()
	logs := &demotest.Logs{}
	ctx, span := tracer.Start(context.Background(), "demo.order.process")
	got := RecordFailure(ctx, span, logs.Logger(), &OrderRejectedError{Run: "r9"})
	span.End()
	if got != TypeOrderRejected {
		t.Fatalf("RecordFailure returned %q", got)
	}

	s := rec.Ended()[0]
	if s.Status().Code != codes.Error || s.Status().Description != "" {
		t.Errorf("status = %+v, want ERROR without description", s.Status())
	}
	if v := attr(s.Attributes(), "error.type"); v != TypeOrderRejected {
		t.Errorf("error.type = %q", v)
	}
	if len(s.Events()) != 1 || s.Events()[0].Name != "exception" {
		t.Fatalf("events = %+v, want one exception event", s.Events())
	}
	ev := s.Events()[0].Attributes
	if len(ev) != 1 || ev[0].Key != "exception.type" || ev[0].Value.AsString() != TypeOrderRejected {
		t.Errorf("exception event attributes = %v, want only exception.type", ev)
	}
	for _, kv := range s.Attributes() {
		if strings.Contains(kv.Value.Emit(), "rej-r9") {
			t.Errorf("span attribute %s leaks the run token", kv.Key)
		}
	}

	r := logs.Records()
	if len(r) != 1 {
		t.Fatalf("records = %d, want 1", len(r))
	}
	l := r[0]
	if l.Level != slog.LevelError || l.Message != "amount over limit rej-r9" {
		t.Errorf("record = %v %q", l.Level, l.Message)
	}
	if l.SpanContext.SpanID() != s.SpanContext().SpanID() || l.SpanContext.TraceID() != s.SpanContext().TraceID() {
		t.Error("the record must be written while the owning span is current")
	}
	if l.Attrs["exception.type"] != TypeOrderRejected || l.Attrs["exception.message"] != "amount over limit rej-r9" {
		t.Errorf("record attrs = %v", l.Attrs)
	}
	if !strings.Contains(l.Attrs["exception.stacktrace"], "goroutine") || !strings.Contains(l.Attrs["exception.stacktrace"], "RecordFailure") {
		t.Errorf("stacktrace = %q, want debug.Stack() output", l.Attrs["exception.stacktrace"])
	}
}

func TestMetricAttributes(t *testing.T) {
	ok := attribute.NewSet(MetricAttributes(ScenarioOK, nil)...)
	if v, _ := ok.Value("demo.outcome"); v.AsString() != "ok" {
		t.Errorf("ok outcome = %q", v.AsString())
	}
	if ok.HasValue("error.type") {
		t.Error("error.type only on failure")
	}
	fail := attribute.NewSet(MetricAttributes(ScenarioDBError, &OrderExistsError{ID: "dup-x"})...)
	if v, _ := fail.Value("demo.scenario"); v.AsString() != ScenarioDBError {
		t.Errorf("scenario = %q", v.AsString())
	}
	if v, _ := fail.Value("demo.outcome"); v.AsString() != "error" {
		t.Errorf("outcome = %q", v.AsString())
	}
	if v, _ := fail.Value("error.type"); v.AsString() != TypeOrderExists {
		t.Errorf("error.type = %q", v.AsString())
	}
	if Outcome(true).Value.AsString() != "error" || Outcome(false).Value.AsString() != "ok" || Scenario("ok").Key != "demo.scenario" {
		t.Error("Outcome/Scenario attributes")
	}
}

func TestNewInstruments(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	ins, err := NewInstruments(mp.Meter(TracerName))
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	ins.Processed.Add(ctx, 1)
	ins.Duration.Record(ctx, 0.02)
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(ctx, &rm); err != nil {
		t.Fatal(err)
	}
	found := map[string]bool{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			found[m.Name] = true
			switch m.Name {
			case "demo.orders.processed":
				if m.Unit != "{order}" {
					t.Errorf("counter unit = %q", m.Unit)
				}
				if _, ok := m.Data.(metricdata.Sum[int64]); !ok {
					t.Errorf("counter data = %T, want integer sum", m.Data)
				}
			case "demo.order.duration":
				if m.Unit != "s" {
					t.Errorf("histogram unit = %q", m.Unit)
				}
				h := m.Data.(metricdata.Histogram[float64])
				if fmt.Sprint(h.DataPoints[0].Bounds) != fmt.Sprint(DurationBuckets) {
					t.Errorf("bounds = %v", h.DataPoints[0].Bounds)
				}
			}
		}
	}
	if !found["demo.orders.processed"] || !found["demo.order.duration"] {
		t.Errorf("instruments = %v", found)
	}
}

func TestRunID(t *testing.T) {
	cases := map[string]string{
		"":                                "local",
		"deployment.environment.name=dev": "local",
		"deployment.environment.name=dev,otel_libs.run=20261116t1": "20261116t1",
		" otel_libs.run = r%2D1 ,x=y":                              "r-1",
		"otel_libs.run=":                                           "local",
	}
	for in, want := range cases {
		t.Setenv("OTEL_RESOURCE_ATTRIBUTES", in)
		if got := RunID(); got != want {
			t.Errorf("RunID(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestReadSecretFile(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "pw")
	if err := os.WriteFile(p, []byte("s3cret-value\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DEMO_X_FILE", p)
	if v, err := ReadSecretFile("DEMO_X_FILE"); err != nil || v != "s3cret-value" {
		t.Errorf("ReadSecretFile = %q, %v", v, err)
	}

	t.Setenv("DEMO_X_FILE", "")
	if _, err := ReadSecretFile("DEMO_X_FILE"); err == nil {
		t.Error("unset variable must fail")
	}
	t.Setenv("DEMO_X_FILE", filepath.Join(dir, "missing"))
	if _, err := ReadSecretFile("DEMO_X_FILE"); err == nil {
		t.Error("missing file must fail")
	}
	empty := filepath.Join(dir, "empty")
	_ = os.WriteFile(empty, []byte(" \n"), 0o600)
	t.Setenv("DEMO_X_FILE", empty)
	_, err := ReadSecretFile("DEMO_X_FILE")
	if err == nil || strings.Contains(err.Error(), "s3cret") {
		t.Errorf("empty file: err = %v", err)
	}
}

func TestEnvHelpers(t *testing.T) {
	t.Setenv("DEMO_S", "")
	if EnvSeconds("DEMO_S", 5*time.Second) != 5*time.Second {
		t.Error("unset → default")
	}
	t.Setenv("DEMO_S", "0.5")
	if EnvSeconds("DEMO_S", 5*time.Second) != 500*time.Millisecond {
		t.Error("fraction")
	}
	for _, bad := range []string{"x", "0", "-1", "4000"} {
		t.Setenv("DEMO_S", bad)
		if EnvSeconds("DEMO_S", time.Second) != time.Second {
			t.Errorf("EnvSeconds(%q) must fall back", bad)
		}
	}
	t.Setenv("DEMO_E", "  v ")
	if EnvOr("DEMO_E", "d") != "v" || EnvOr("DEMO_UNSET_XYZ", "d") != "d" {
		t.Error("EnvOr")
	}
	for in, want := range map[string]string{"h:1": "h/1", "h": "h/9", "h:x": "h/9", "h:0": "h/9", "[::1]:7": "::1/7"} {
		h, p := HostPort(in, 9)
		if got := fmt.Sprintf("%s/%d", h, p); got != want {
			t.Errorf("HostPort(%q) = %s, want %s", in, got, want)
		}
	}
}

func attr(kvs []attribute.KeyValue, key string) string {
	for _, kv := range kvs {
		if string(kv.Key) == key {
			return kv.Value.Emit()
		}
	}
	return ""
}
