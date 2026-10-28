package otelhelper

// Lifecycle contract (spec config-contract-v0.3 G1, G2): a failed Setup
// releases everything it built and leaves the globals untouched; Shutdown is
// idempotent and lets a later Setup build a fresh pipeline.

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	goruntime "runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	logglobal "go.opentelemetry.io/otel/log/global"
	nooplog "go.opentelemetry.io/otel/log/noop"
	noopmetric "go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/propagation"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	nooptrace "go.opentelemetry.io/otel/trace/noop"
)

// shutdownLog records the order in which spies were shut down.
type shutdownLog struct {
	mu    sync.Mutex
	order []string
}

func (l *shutdownLog) add(name string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.order = append(l.order, name)
}

func (l *shutdownLog) get() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]string(nil), l.order...)
}

type spySpanExporter struct {
	name       string
	log        *shutdownLog
	onShutdown func()
}

func (s *spySpanExporter) ExportSpans(context.Context, []sdktrace.ReadOnlySpan) error { return nil }
func (s *spySpanExporter) Shutdown(context.Context) error {
	s.log.add(s.name)
	if s.onShutdown != nil {
		s.onShutdown()
	}
	return nil
}

type spyLogExporter struct {
	name string
	log  *shutdownLog
	// block, when set, runs inside Shutdown before it returns.
	block func(ctx context.Context) error
}

func (s *spyLogExporter) Export(context.Context, []sdklog.Record) error { return nil }
func (s *spyLogExporter) ForceFlush(context.Context) error              { return nil }
func (s *spyLogExporter) Shutdown(ctx context.Context) error {
	s.log.add(s.name)
	if s.block != nil {
		return s.block(ctx)
	}
	return nil
}

var errInjected = errors.New("injected build failure")

// lifecycleTest resets the helper state, installs no-op sentinel globals and
// restores the exporter seams afterwards.
func lifecycleTest(t *testing.T) {
	t.Helper()
	resetGlobals()
	otel.SetTracerProvider(nooptrace.NewTracerProvider())
	otel.SetMeterProvider(noopmetric.NewMeterProvider())
	logglobal.SetLoggerProvider(nooplog.NewLoggerProvider())
	otel.SetTextMapPropagator(propagation.Baggage{}) // Setup would install tracecontext,baggage
	origOTLP, origConsole, origLog := otlpTraceExporter, consoleSpanExporter, otlpLogExporter
	t.Cleanup(func() {
		otlpTraceExporter, consoleSpanExporter, otlpLogExporter = origOTLP, origConsole, origLog
		if s := currentShutdown(); s != nil {
			_ = s(context.Background())
		}
		resetGlobals()
		otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	})
}

func currentShutdown() Shutdown {
	mu.Lock()
	defer mu.Unlock()
	return shutdownFn
}

// spyTraces makes every OTLP trace exporter a spy named "traces".
func spyTraces(log *shutdownLog) {
	otlpTraceExporter = func(context.Context, *signalExport) (sdktrace.SpanExporter, error) {
		return &spySpanExporter{name: "traces", log: log}, nil
	}
}

// assertUntouched checks that a failed Setup left the sentinel globals, the
// propagator and MetricsHandler as they were.
func assertUntouched(t *testing.T) {
	t.Helper()
	if tp, ok := otel.GetTracerProvider().(*sdktrace.TracerProvider); ok {
		_, span := tp.Tracer("t").Start(context.Background(), "probe")
		t.Errorf("global TracerProvider is an SDK provider after failed Setup (span.IsRecording=%v)", span.IsRecording())
	}
	if _, ok := otel.GetMeterProvider().(*sdkmetric.MeterProvider); ok {
		t.Error("global MeterProvider is an SDK provider after failed Setup")
	}
	if _, ok := logglobal.GetLoggerProvider().(*sdklog.LoggerProvider); ok {
		t.Error("global LoggerProvider is an SDK provider after failed Setup")
	}
	if f := strings.Join(otel.GetTextMapPropagator().Fields(), ","); f != "baggage" {
		t.Errorf("propagator fields = %s after failed Setup, want the previous [baggage]", f)
	}
	rr := httptest.NewRecorder()
	MetricsHandler().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rr.Code != http.StatusServiceUnavailable {
		t.Errorf("MetricsHandler after failed Setup: %d, want 503", rr.Code)
	}
	mu.Lock()
	bridged, done := logsBridged(), setupDone
	mu.Unlock()
	if bridged || done {
		t.Errorf("after failed Setup: log bridge active=%v setupDone=%v, want false/false", bridged, done)
	}
}

// assertRecording checks that the installed pipeline produces recording spans.
func assertRecording(t *testing.T) {
	t.Helper()
	_, span := GetTracer("lifecycle").Start(context.Background(), "probe")
	defer span.End()
	if !span.IsRecording() {
		t.Errorf("span.IsRecording=false; global TracerProvider is %T", otel.GetTracerProvider())
	}
}

// countGoroutines counts live goroutines whose stack mentions fn.
func countGoroutines(fn string) int {
	buf := make([]byte, 1<<20)
	buf = buf[:goruntime.Stack(buf, true)]
	n := 0
	for _, g := range strings.Split(string(buf), "\n\n") {
		if strings.Contains(g, fn) {
			n++
		}
	}
	return n
}

func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

func assertPortFree(t *testing.T, port int) {
	t.Helper()
	ln, err := net.Listen("tcp", fmt.Sprintf(":%d", port))
	if err != nil {
		t.Errorf("port %d still bound after failed Setup/Shutdown: %v", port, err)
		return
	}
	ln.Close()
}

const deadEndpoint = "http://127.0.0.1:9"

func TestSetupFailure_TracesStage_ReleasesAndRetries(t *testing.T) {
	lifecycleTest(t)
	t.Setenv(EnvTracesExporter, "console,otlp")
	var log shutdownLog
	consoleSpanExporter = func() (sdktrace.SpanExporter, error) {
		return &spySpanExporter{name: "console", log: &log}, nil
	}
	otlpTraceExporter = func(context.Context, *signalExport) (sdktrace.SpanExporter, error) {
		return nil, errInjected
	}

	before := countGoroutines("processQueue")
	const attempts = 5
	for range attempts {
		s, err := Setup(context.Background(), WithServiceName("g1"), WithEndpoint(deadEndpoint), WithMetricExporters("prometheus"), WithoutMetricsListener())
		if !errors.Is(err, errInjected) {
			t.Fatalf("Setup error = %v, want the injected trace exporter failure", err)
		}
		if err := s(context.Background()); err != nil {
			t.Errorf("Shutdown returned by a failed Setup: %v", err)
		}
	}
	if after := countGoroutines("processQueue"); after != before {
		t.Errorf("batch span processor goroutines: %d before, %d after %d failed Setups (leaked)", before, after, attempts)
	}
	if got := log.get(); len(got) != attempts || got[0] != "console" {
		t.Errorf("exporters shut down = %v, want [console] once per attempt (built before the failing one)", got)
	}
	assertUntouched(t)

	consoleSpanExporter = newConsoleSpanExporter
	spyTraces(&log)
	if _, err := Setup(context.Background(), WithServiceName("g1"), WithEndpoint(deadEndpoint), WithMetricExporters("prometheus"), WithoutMetricsListener()); err != nil {
		t.Fatalf("retry Setup: %v", err)
	}
	assertRecording(t)
}

func TestSetupFailure_MetricsListener_ReleasesAndRetries(t *testing.T) {
	lifecycleTest(t)
	var log shutdownLog
	spyTraces(&log)
	busy, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatal(err)
	}
	port := busy.Addr().(*net.TCPAddr).Port
	opts := []Option{WithServiceName("g1"), WithEndpoint(deadEndpoint),
		WithMetricExporters("prometheus"), WithPrometheusMetricsPort(port)}

	_, err = Setup(context.Background(), opts...)
	if err == nil || !strings.Contains(err.Error(), "listener") {
		t.Fatalf("Setup error = %v, want the busy-port listener error", err)
	}
	if got := log.get(); len(got) != 1 || got[0] != "traces" {
		t.Errorf("shut down after failure = %v, want [traces] (the provider built before metrics)", got)
	}
	assertUntouched(t)

	busy.Close()
	s, err := Setup(context.Background(), opts...)
	if err != nil {
		t.Fatalf("retry Setup on the freed port: %v", err)
	}
	assertRecording(t)
	resp, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d/metrics", port))
	if err != nil {
		t.Fatalf("GET /metrics after retry: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("GET /metrics after retry = %d, want 200", resp.StatusCode)
	}
	if err := s(context.Background()); err != nil {
		t.Errorf("Shutdown: %v", err)
	}
	assertPortFree(t, port)
}

func TestSetupFailure_LogsStage_ReleasesAndRetries(t *testing.T) {
	lifecycleTest(t)
	var log shutdownLog
	spyTraces(&log)
	otlpLogExporter = func(context.Context, *signalExport) (sdklog.Exporter, error) {
		return nil, errInjected
	}
	port := freePort(t)
	opts := []Option{WithServiceName("g1"), WithEndpoint(deadEndpoint),
		WithMetricExporters("prometheus"), WithPrometheusMetricsPort(port)}

	_, err := Setup(context.Background(), opts...)
	if !errors.Is(err, errInjected) {
		t.Fatalf("Setup error = %v, want the injected log exporter failure", err)
	}
	if got := log.get(); len(got) != 1 || got[0] != "traces" {
		t.Errorf("shut down after failure = %v, want [traces]", got)
	}
	assertPortFree(t, port) // the /metrics listener built before logs is released
	assertUntouched(t)

	otlpLogExporter = func(context.Context, *signalExport) (sdklog.Exporter, error) {
		return &spyLogExporter{name: "logs", log: &log}, nil
	}
	if _, err := Setup(context.Background(), opts...); err != nil {
		t.Fatalf("retry Setup on the same port: %v", err)
	}
	assertRecording(t)
}

func TestShutdown_IdempotentAndSignalsConcurrent(t *testing.T) {
	lifecycleTest(t)
	var log shutdownLog
	tracesDown := make(chan struct{})
	var once sync.Once
	otlpTraceExporter = func(context.Context, *signalExport) (sdktrace.SpanExporter, error) {
		return &spySpanExporter{name: "traces", log: &log, onShutdown: func() { once.Do(func() { close(tracesDown) }) }}, nil
	}
	// The logs exporter only finishes once traces have been shut down: a
	// sequential logs → traces Shutdown would wait here until the timeout.
	otlpLogExporter = func(context.Context, *signalExport) (sdklog.Exporter, error) {
		return &spyLogExporter{name: "logs", log: &log, block: func(context.Context) error {
			select {
			case <-tracesDown:
				return nil
			case <-time.After(2 * time.Second):
				return errors.New("traces were not shut down while logs were flushing (sequential Shutdown)")
			}
		}}, nil
	}
	s, err := Setup(context.Background(), WithServiceName("g2"), WithEndpoint(deadEndpoint),
		WithMetricExporters("prometheus"), WithoutMetricsListener())
	if err != nil {
		t.Fatal(err)
	}
	first := s(context.Background())
	second := s(context.Background())
	if first != nil || second != first {
		t.Errorf("shutdown#1 err=%v shutdown#2 err=%v, want nil and the same result", first, second)
	}
	got := log.get()
	if len(got) != 2 || !containsStr(got, "logs") || !containsStr(got, "traces") {
		t.Errorf("shut down = %v, want logs and traces once each", got)
	}
	rr := httptest.NewRecorder()
	MetricsHandler().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rr.Code != http.StatusServiceUnavailable {
		t.Errorf("MetricsHandler after Shutdown: %d, want 503", rr.Code)
	}
	if logsBridged() {
		t.Error("log bridge still active after Shutdown: loggers would write to a closed provider")
	}
}

func TestShutdown_ErrorsJoinedAndRepeated(t *testing.T) {
	lifecycleTest(t)
	var log shutdownLog
	spyTraces(&log)
	boom := errors.New("logs flush failed")
	otlpLogExporter = func(context.Context, *signalExport) (sdklog.Exporter, error) {
		return &spyLogExporter{name: "logs", log: &log, block: func(context.Context) error { return boom }}, nil
	}
	s, err := Setup(context.Background(), WithServiceName("g2"), WithEndpoint(deadEndpoint),
		WithMetricExporters("prometheus"), WithoutMetricsListener())
	if err != nil {
		t.Fatal(err)
	}
	first := s(context.Background())
	if !errors.Is(first, boom) {
		t.Fatalf("Shutdown err = %v, want it to wrap the logs error", first)
	}
	if second := s(context.Background()); second != first {
		t.Errorf("second Shutdown err = %v, want the first result %v", second, first)
	}
}

func TestSetup_WaitsForInFlightShutdown(t *testing.T) {
	lifecycleTest(t)
	var log shutdownLog
	spyTraces(&log)
	entered, release := make(chan struct{}), make(chan struct{})
	otlpLogExporter = func(context.Context, *signalExport) (sdklog.Exporter, error) {
		return &spyLogExporter{name: "logs", log: &log, block: func(context.Context) error {
			close(entered)
			<-release
			return nil
		}}, nil
	}
	opts := []Option{WithServiceName("g2"), WithEndpoint(deadEndpoint),
		WithMetricExporters("prometheus"), WithoutMetricsListener()}
	s1, err := Setup(context.Background(), opts...)
	if err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	old := current
	mu.Unlock()

	shutdownDone := make(chan error, 1)
	go func() { shutdownDone <- s1(context.Background()) }()
	<-entered // Shutdown is flushing logs

	otlpLogExporter = newOTLPLogExporter
	setupDone := make(chan error, 1)
	go func() {
		_, err := Setup(context.Background(), opts...)
		setupDone <- err
	}()
	select {
	case err := <-setupDone:
		t.Fatalf("Setup returned (err=%v) while Shutdown was in flight; want it to wait", err)
	case <-time.After(100 * time.Millisecond):
	}
	close(release)
	if err := <-shutdownDone; err != nil {
		t.Errorf("Shutdown: %v", err)
	}
	if err := <-setupDone; err != nil {
		t.Fatalf("Setup after the in-flight Shutdown: %v", err)
	}
	mu.Lock()
	fresh := current
	mu.Unlock()
	if fresh == nil || fresh == old {
		t.Error("Setup returned the dying pipeline instead of building a fresh one")
	}
	assertRecording(t)
}

func TestSetup_WaitForShutdownHonorsCtx(t *testing.T) {
	lifecycleTest(t)
	var log shutdownLog
	spyTraces(&log)
	entered, release := make(chan struct{}), make(chan struct{})
	otlpLogExporter = func(context.Context, *signalExport) (sdklog.Exporter, error) {
		return &spyLogExporter{name: "logs", log: &log, block: func(context.Context) error {
			close(entered)
			<-release
			return nil
		}}, nil
	}
	opts := []Option{WithServiceName("g2"), WithEndpoint(deadEndpoint),
		WithMetricExporters("prometheus"), WithoutMetricsListener()}
	s1, err := Setup(context.Background(), opts...)
	if err != nil {
		t.Fatal(err)
	}
	shutdownDone := make(chan error, 1)
	go func() { shutdownDone <- s1(context.Background()) }()
	<-entered

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	s2, err := Setup(ctx, opts...)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("Setup err = %v, want the ctx deadline while Shutdown is in flight", err)
	}
	if s2 == nil || s2(context.Background()) != nil {
		t.Error("Setup cancelled while waiting must return a no-op Shutdown")
	}
	close(release)
	if err := <-shutdownDone; err != nil {
		t.Errorf("Shutdown: %v", err)
	}
}

func TestShutdown_ConcurrentCallsShareOneResult(t *testing.T) {
	lifecycleTest(t)
	var log shutdownLog
	spyTraces(&log)
	s, err := Setup(context.Background(), WithServiceName("g2"), WithEndpoint(deadEndpoint),
		WithMetricExporters("prometheus"), WithoutMetricsListener())
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	errs := make([]error, 8)
	for i := range errs {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			errs[i] = s(context.Background())
		}(i)
	}
	wg.Wait()
	for i, e := range errs {
		if e != errs[0] {
			t.Errorf("call %d err=%v, want %v", i, e, errs[0])
		}
	}
	if got := log.get(); len(got) != 1 {
		t.Errorf("trace exporter shut down %d times, want 1", len(got))
	}
}

func TestSetupAfterShutdown_BuildsFreshPipeline(t *testing.T) {
	lifecycleTest(t)
	var log shutdownLog
	spyTraces(&log)
	opts := []Option{WithServiceName("g2"), WithEndpoint(deadEndpoint),
		WithMetricExporters("prometheus"), WithoutMetricsListener()}

	s1, err := Setup(context.Background(), opts...)
	if err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	first := current
	mu.Unlock()
	if err := s1(context.Background()); err != nil {
		t.Fatal(err)
	}
	if installedOptions() != nil {
		t.Error("installed options still set after Shutdown")
	}

	s2, err := Setup(context.Background(), opts...)
	if err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	second := current
	mu.Unlock()
	if second == nil || second == first {
		t.Error("Setup after shutdown returned the old pipeline")
	}
	assertRecording(t)
	rr := httptest.NewRecorder()
	MetricsHandler().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rr.Code != http.StatusOK {
		t.Errorf("MetricsHandler after re-Setup: %d, want 200", rr.Code)
	}

	// A stale Shutdown from the first pipeline must not tear down the second.
	_ = s1(context.Background())
	mu.Lock()
	still := current
	mu.Unlock()
	if still != second || installedOptions() == nil {
		t.Error("calling the old Shutdown reset the new pipeline")
	}
	assertRecording(t)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := s2(ctx); err != nil {
		t.Errorf("second pipeline Shutdown: %v", err)
	}
}

func TestSetupSDKDisabled_ShutdownAllowsFreshSetup(t *testing.T) {
	lifecycleTest(t)
	t.Setenv(EnvSDKDisabled, "true")
	s, err := Setup(context.Background(), WithServiceName("off"))
	if err != nil {
		t.Fatal(err)
	}
	if err := s(context.Background()); err != nil {
		t.Fatal(err)
	}
	t.Setenv(EnvSDKDisabled, "false")
	var log shutdownLog
	spyTraces(&log)
	if _, err := Setup(context.Background(), WithServiceName("on"), WithEndpoint(deadEndpoint), WithMetricExporters("prometheus"), WithoutMetricsListener()); err != nil {
		t.Fatal(err)
	}
	assertRecording(t)
}
