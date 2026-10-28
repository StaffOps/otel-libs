package otelhelper

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	sdklog "go.opentelemetry.io/otel/sdk/log"
)

// Design D6 / G3: the helper's slog handlers decide per record between the
// OTel bridge (a logs exporter installed by Setup) and stdout JSON.

// recordingLogExporter collects exported log bodies and attribute keys.
type recordingLogExporter struct {
	mu     sync.Mutex
	bodies []string
	attrs  []string
}

func (e *recordingLogExporter) Export(_ context.Context, records []sdklog.Record) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	for _, r := range records {
		e.bodies = append(e.bodies, r.Body().AsString())
		r.WalkAttributes(func(kv attribute.KeyValue) bool {
			e.attrs = append(e.attrs, flattenAttr("", kv)...)
			return true
		})
	}
	return nil
}

// flattenAttr returns the attribute's key and, for a map (an slog group), the
// dotted paths of every nested key: group g{a=1, h{b=2}} → g, g.a, g.h, g.h.b.
func flattenAttr(prefix string, kv attribute.KeyValue) []string {
	key := prefix + string(kv.Key)
	out := []string{key}
	if kv.Value.Type() == attribute.MAP {
		for _, inner := range kv.Value.AsMap() {
			out = append(out, flattenAttr(key+".", inner)...)
		}
	}
	return out
}
func (e *recordingLogExporter) Shutdown(context.Context) error   { return nil }
func (e *recordingLogExporter) ForceFlush(context.Context) error { return nil }

func (e *recordingLogExporter) has(body string) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	for _, b := range e.bodies {
		if b == body {
			return true
		}
	}
	return false
}

func (e *recordingLogExporter) hasAttr(key string) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	for _, k := range e.attrs {
		if k == key {
			return true
		}
	}
	return false
}

// logsTest isolates a logging test: only the logs signal is exported, and the
// OTLP log exporter is the returned recorder (each Setup gets a fresh one).
func logsTest(t *testing.T) func() *recordingLogExporter {
	t.Helper()
	lifecycleTest(t)
	t.Setenv(EnvTracesExporter, "none")
	t.Setenv(EnvMetricsExporter, "none")
	var (
		mu   sync.Mutex
		last *recordingLogExporter
	)
	otlpLogExporter = func(context.Context, *signalExport) (sdklog.Exporter, error) {
		mu.Lock()
		defer mu.Unlock()
		last = &recordingLogExporter{}
		return last, nil
	}
	return func() *recordingLogExporter {
		mu.Lock()
		defer mu.Unlock()
		return last
	}
}

// captureStdout redirects os.Stdout until the returned function is called; it
// restores os.Stdout and returns what was written. Handlers read os.Stdout at
// construction, so build them after calling this.
func captureStdout(t *testing.T) func() string {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	orig := os.Stdout
	os.Stdout = w
	var buf bytes.Buffer
	done := make(chan struct{})
	go func() { _, _ = io.Copy(&buf, r); close(done) }()
	var once sync.Once
	stop := func() string {
		once.Do(func() {
			os.Stdout = orig
			_ = w.Close()
			<-done
		})
		return buf.String()
	}
	t.Cleanup(func() { stop() })
	return stop
}

func setupLogs(t *testing.T, opts ...Option) Shutdown {
	t.Helper()
	shutdown, err := Setup(context.Background(), append([]Option{WithServiceName("logs-test")}, opts...)...)
	if err != nil {
		t.Fatalf("Setup: %v", err)
	}
	return shutdown
}

// G3: before A5 NewSlogHandler bridged to a processor-less provider with no
// endpoint (Enabled(Info)=false), so every record was lost.
func TestNewSlogHandler_NoEndpoint_WritesStdout(t *testing.T) {
	logsTest(t)
	stop := captureStdout(t)
	shutdown := setupLogs(t)
	defer shutdown(context.Background())
	l := slog.New(NewSlogHandler())
	l.Info("slog-handler-no-endpoint", "k", "v")
	l.Debug("slog-handler-debug-dropped")
	out := stop()
	if !strings.Contains(out, `"msg":"slog-handler-no-endpoint"`) || !strings.Contains(out, `"k":"v"`) {
		t.Errorf("record not written to stdout as JSON: %q", out)
	}
	if strings.Contains(out, "slog-handler-debug-dropped") {
		t.Errorf("stdout fallback of NewSlogHandler should keep slog's Info minimum: %q", out)
	}
}

// Before Setup there is nothing to export to: stdout.
func TestNewSlogHandler_BeforeSetup_WritesStdout(t *testing.T) {
	logsTest(t)
	stop := captureStdout(t)
	slog.New(NewSlogHandler()).Warn("slog-handler-before-setup")
	if out := stop(); !strings.Contains(out, "slog-handler-before-setup") {
		t.Errorf("record before Setup lost: %q", out)
	}
}

// A logger created before Setup switches to the bridge once Setup installs a
// logs exporter (before A5 it stayed on stdout forever).
func TestLogger_CreatedBeforeSetup_UsesBridge(t *testing.T) {
	exporter := logsTest(t)
	stop := captureStdout(t)
	logger := NewLogger(DEV, false)
	handler := slog.New(NewSlogHandler().WithAttrs([]slog.Attr{slog.String("pre", "1")}).WithGroup("grp"))
	shutdown := setupLogs(t, WithEndpoint("http://127.0.0.1:1"))
	logger.Warn("logger-before-setup")
	handler.Info("handler-before-setup", "inner", "x")
	if err := shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	out := stop()
	exp := exporter()
	if exp == nil || !exp.has("logger-before-setup") || !exp.has("handler-before-setup") {
		t.Fatalf("records not exported through the bridge (stdout %q)", out)
	}
	if !exp.hasAttr("pre") || !exp.hasAttr("grp") {
		t.Errorf("WithAttrs/WithGroup made before Setup not applied to the bridge: %v", exp.attrs)
	}
	if strings.Contains(out, "logger-before-setup") || strings.Contains(out, "handler-before-setup") {
		t.Errorf("logs exporter active: records must not also go to stdout (D6), got %q", out)
	}
}

// After Shutdown records go back to stdout, never to the closed provider.
func TestLogger_AfterShutdown_WritesStdout(t *testing.T) {
	logsTest(t)
	stop := captureStdout(t)
	shutdown := setupLogs(t, WithEndpoint("http://127.0.0.1:1"))
	logger := NewLogger(DEV, false)
	handler := slog.New(NewSlogHandler())
	if err := shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	logger.Warn("logger-after-shutdown")
	handler.Warn("handler-after-shutdown")
	out := stop()
	for _, msg := range []string{"logger-after-shutdown", "handler-after-shutdown"} {
		if !strings.Contains(out, `"msg":"`+msg+`"`) {
			t.Errorf("%s not on stdout after Shutdown: %q", msg, out)
		}
	}
}

// A logger that outlives Shutdown follows the next Setup's pipeline.
func TestLogger_ResetupAfterShutdown_UsesNewPipeline(t *testing.T) {
	exporter := logsTest(t)
	shutdown := setupLogs(t, WithEndpoint("http://127.0.0.1:1"))
	logger := NewLogger(DEV, false)
	_ = shutdown(context.Background())
	shutdown = setupLogs(t, WithEndpoint("http://127.0.0.1:1"))
	second := exporter()
	logger.Warn("logger-second-pipeline")
	_ = shutdown(context.Background())
	if !second.has("logger-second-pipeline") {
		t.Error("record of a logger built during the first pipeline did not reach the second one")
	}
}

// NewLogger keeps its environment level on both destinations.
func TestNewLogger_LevelOnBothDestinations(t *testing.T) {
	exporter := logsTest(t)
	stop := captureStdout(t)
	logger := NewLogger(PRD, false) // Warn
	logger.Info("prd-info-stdout")
	shutdown := setupLogs(t, WithEndpoint("http://127.0.0.1:1"))
	logger.Info("prd-info-bridge")
	logger.Warn("prd-warn-bridge")
	_ = shutdown(context.Background())
	out := stop()
	if strings.Contains(out, "prd-info-stdout") {
		t.Errorf("Info written to stdout at PRD level: %q", out)
	}
	exp := exporter()
	if exp.has("prd-info-bridge") || !exp.has("prd-warn-bridge") {
		t.Errorf("bridge level: got %v, want only prd-warn-bridge", exp.bodies)
	}
}

// OTEL_LOGS_EXPORTER=otlp,console: both the OTLP and the console exporter
// receive the record (D6). The console exporter is replaced by a recorder:
// stdoutlog binds the process stdout before a test can redirect it.
func TestLogger_OTLPAndConsole_Both(t *testing.T) {
	exporter := logsTest(t)
	console := &recordingLogExporter{}
	consoleLogExporter = func() (sdklog.Exporter, error) { return console, nil }
	t.Cleanup(func() { consoleLogExporter = newConsoleLogExporter })
	t.Setenv(EnvLogsExporter, "otlp,console")
	stop := captureStdout(t)
	shutdown := setupLogs(t, WithEndpoint("http://127.0.0.1:1"))
	NewLogger(DEV, false).Warn("otlp-and-console")
	_ = shutdown(context.Background())
	out := stop()
	if !exporter().has("otlp-and-console") {
		t.Error("record not exported over OTLP")
	}
	if !console.has("otlp-and-console") {
		t.Error("record not handed to the console exporter")
	}
	if strings.Contains(out, `"msg":"otlp-and-console"`) {
		t.Errorf("record also written as fallback JSON: %q", out)
	}
}

// OTEL_LOGS_EXPORTER=none with an endpoint: stdout only.
func TestLogger_LogsExporterNone_WritesStdout(t *testing.T) {
	exporter := logsTest(t)
	t.Setenv(EnvLogsExporter, "none")
	stop := captureStdout(t)
	shutdown := setupLogs(t, WithEndpoint("http://127.0.0.1:1"))
	NewLogger(DEV, false).Warn("logs-exporter-none")
	_ = shutdown(context.Background())
	if out := stop(); !strings.Contains(out, "logs-exporter-none") {
		t.Errorf("record not on stdout: %q", out)
	}
	if exporter() != nil {
		t.Error("an OTLP log exporter was built with OTEL_LOGS_EXPORTER=none")
	}
}

// Records emitted while Setup and Shutdown run concurrently (race detector).
func TestLogger_ConcurrentWithLifecycle(t *testing.T) {
	logsTest(t)
	stop := captureStdout(t)
	logger := NewLogger(DEV, false).With("worker", 1)
	var wg sync.WaitGroup
	done := make(chan struct{})
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-done:
					return
				default:
					logger.WithGroup("g").Info("concurrent", "i", 1)
				}
			}
		}()
	}
	for i := 0; i < 5; i++ {
		s := setupLogs(t, WithEndpoint("http://127.0.0.1:1"))
		_ = s(context.Background())
	}
	close(done)
	wg.Wait()
	stop()
}

func TestSwitchHandler_EmptyWithIsIdentity(t *testing.T) {
	h := NewSlogHandler()
	if h.WithAttrs(nil) != h || h.WithGroup("") != h {
		t.Error("WithAttrs(nil) / WithGroup(\"\") should return the same handler")
	}
}

// Nested groups reach the bridge with their structure:
// WithGroup(a) → WithAttrs(x) → WithGroup(b) → record attr y = a{x, b{y}}.
func TestLogger_NestedGroupsOnBridge(t *testing.T) {
	exporter := logsTest(t)
	h := NewSlogHandler().WithGroup("a").WithAttrs([]slog.Attr{slog.Int("x", 1)}).WithGroup("b")
	shutdown := setupLogs(t, WithEndpoint("http://127.0.0.1:1"))
	slog.New(h).Info("nested", "y", 2)
	_ = shutdown(context.Background())
	exp := exporter()
	for _, path := range []string{"a", "a.x", "a.b", "a.b.y"} {
		if !exp.hasAttr(path) {
			t.Errorf("missing %s in exported attributes %v", path, exp.attrs)
		}
	}
	for _, wrong := range []string{"x", "y", "b", "a.y"} {
		if exp.hasAttr(wrong) {
			t.Errorf("attribute %s at the wrong level: %v", wrong, exp.attrs)
		}
	}
}

// Stdout shape (D6 A5 rulings): one JSON line with time, level, msg, attrs,
// and exception for the first error-valued attribute.
func TestStdoutShape_ExceptionAndLevels(t *testing.T) {
	logsTest(t)
	stop := captureStdout(t)
	l := NewLogger(LOCAL, false)
	l.Error("failed", "err", errors.New("boom\nline2"), "order", "o-1", "err2", errors.New("second"))
	l.Debug("dbg")
	out := stop()
	lines := strings.Split(strings.TrimSpace(out), "\n")
	if len(lines) != 2 {
		t.Fatalf("want 2 single-line records, got %q", out)
	}
	var rec map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &rec); err != nil {
		t.Fatalf("not JSON: %v (%q)", err, lines[0])
	}
	for k, want := range map[string]any{"level": "ERROR", "msg": "failed", "exception": "boom\nline2", "order": "o-1", "err2": "second"} {
		if rec[k] != want {
			t.Errorf("%s = %v, want %v (record %v)", k, rec[k], want, rec)
		}
	}
	if _, ok := rec["time"]; !ok {
		t.Error("time missing")
	}
	if _, ok := rec["err"]; ok {
		t.Error("the error attribute should be emitted as exception, not err")
	}
	if !strings.Contains(lines[1], `"level":"DEBUG"`) {
		t.Errorf("debug line: %q", lines[1])
	}
}

// NewSlogHandler's stdout path uses the environment level Setup resolved.
func TestNewSlogHandler_StdoutUsesEnvironmentLevel(t *testing.T) {
	logsTest(t)
	stop := captureStdout(t)
	l := slog.New(NewSlogHandler())
	shutdown := setupLogs(t, WithEnvironment(PRD)) // no endpoint: stdout
	l.Info("prd-info-hidden")
	l.Warn("prd-warn-shown")
	_ = shutdown(context.Background())
	out := stop()
	if strings.Contains(out, "prd-info-hidden") || !strings.Contains(out, "prd-warn-shown") {
		t.Errorf("PRD level not applied to NewSlogHandler stdout: %q", out)
	}
}

// Handle enforces NewLogger's level on the bridge even without Enabled, and
// a handler drops its cached bridge once the pipeline is gone.
func TestSwitchHandler_HandleLevelAndCacheRelease(t *testing.T) {
	exporter := logsTest(t)
	h := NewLogger(PRD, false).Handler().(*switchHandler)
	shutdown := setupLogs(t, WithEndpoint("http://127.0.0.1:1"))
	ctx := context.Background()
	_ = h.Handle(ctx, slog.NewRecord(time.Now(), slog.LevelInfo, "direct-info", 0))
	_ = h.Handle(ctx, slog.NewRecord(time.Now(), slog.LevelWarn, "direct-warn", 0))
	if h.derived.Load() == nil {
		t.Fatal("bridge not cached")
	}
	_ = shutdown(ctx)
	if exp := exporter(); exp.has("direct-info") || !exp.has("direct-warn") {
		t.Errorf("bridge got %v, want only direct-warn", exp.bodies)
	}
	stop := captureStdout(t)
	_ = h.Handle(ctx, slog.NewRecord(time.Now(), slog.LevelWarn, "after", 0))
	stop()
	if h.derived.Load() != nil {
		t.Error("cached bridge of a shut-down pipeline kept alive")
	}
}

type ptrErr struct{ msg string }

func (e *ptrErr) Error() string { return e.msg } // panics on a nil receiver

type panicErr struct{}

func (panicErr) Error() string { panic("bad error") }

// Stdout fallback renders error values like slog and never crashes (D6 A5
// rulings): typed-nil → "<nil>", panicking Error() → "!PANIC: …"; an existing
// "exception" attribute is not duplicated; time is RFC 3339 ms in UTC ("Z").
func TestStdoutErrorRenderingAndTime(t *testing.T) {
	logsTest(t)
	stop := captureStdout(t)
	l := NewLogger(DEV, false)
	var nilErr *ptrErr
	ctx := context.Background()
	l.ErrorContext(ctx, "typed-nil", "err", error(nilErr))
	l.Error("panicking", "err", panicErr{})
	l.Error("has-exception", "exception", "given", "err", errors.New("kept"))
	l.With("exception", "scoped").Error("scoped-exception", "err", errors.New("kept2"))
	l.With("err0", errors.New("attrs-error")).Error("withattrs-error")
	out := stop()
	lines := strings.Split(strings.TrimSpace(out), "\n")
	if len(lines) != 5 {
		t.Fatalf("want 5 lines, got %q", out)
	}
	recs := make([]map[string]any, len(lines))
	for i, line := range lines {
		if err := json.Unmarshal([]byte(line), &recs[i]); err != nil {
			t.Fatalf("line %d not JSON: %v (%q)", i, err, line)
		}
		if strings.Count(line, `"exception"`) > 1 {
			t.Errorf("duplicate exception key: %q", line)
		}
	}
	checks := []struct {
		i    int
		k    string
		want any
	}{
		{0, "exception", "<nil>"},
		{1, "exception", "!PANIC: bad error"},
		{2, "exception", "given"}, {2, "err", "kept"},
		{3, "exception", "scoped"}, {3, "err", "kept2"},
		{4, "err0", "attrs-error"},
	}
	for _, c := range checks {
		if recs[c.i][c.k] != c.want {
			t.Errorf("line %d %s = %v, want %v", c.i, c.k, recs[c.i][c.k], c.want)
		}
	}
	ts, _ := recs[0]["time"].(string)
	if _, err := time.Parse(time.RFC3339, ts); err != nil || !strings.HasSuffix(ts, "Z") || len(ts) != len("2006-01-02T15:04:05.000Z") {
		t.Errorf("time %q, want RFC 3339 with milliseconds in UTC (Z)", ts)
	}
}

func TestErrorText(t *testing.T) {
	var nilErr *ptrErr
	for want, err := range map[string]error{"<nil>": nilErr, "!PANIC: bad error": panicErr{}, "x": errors.New("x")} {
		if got := errorText(err); got != want {
			t.Errorf("errorText = %q, want %q", got, want)
		}
	}
}
