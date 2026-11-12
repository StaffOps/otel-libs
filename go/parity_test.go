package otelhelper

// Parity fixture runner (ADR-008, specs/config-contract-v0.3/design.md D2).
//
// Every case in ../parity/cases.json runs as a subtest named after its id.
// Status "pass" must pass; "xfail:<task>" must FAIL on a behavior mismatch
// (an unexpected pass fails the build, so the task that fixes the behavior
// flips the status); "skip:<reason>" is skipped. Fixture or adapter errors
// (unknown kind, expect key outside the snapshot schema, unsupported code key)
// fail hard whatever the status. Format: parity/README.md.
//
// Each case runs in a child process (this test binary re-executed): OTel and
// SDK state is set-once per process — e.g. the OTel globals.
//
// The adapter reads what Setup installed: the resource of a span from the
// installed TracerProvider, the helper's MetricsHandler, its public logger
// (NewLogger — Go apps create their own logger with it), and what reached the
// fake receiver. Exporter protocol/URL/TLS are asserted only on the wire.
// metric_exporters, export interval and log level come from installedOptions,
// the decisions the last successful Setup recorded and acted on (re-running
// the resolver would repeat its warnings and could disagree with Setup).

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"log/slog"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/go-logr/logr/funcr"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/baggage"
	otellog "go.opentelemetry.io/otel/log"
	logglobal "go.opentelemetry.io/otel/log/global"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/exemplar"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	grpcstatus "google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/types/known/emptypb"
)

type parityCase struct {
	ID     string            `json:"id"`
	Kind   string            `json:"kind"`
	Env    map[string]string `json:"env"`
	Code   map[string]any    `json:"code"`
	Input  map[string]any    `json:"input"`
	Expect map[string]any    `json:"expect"`
	Status map[string]string `json:"status"`

	raw map[string]any // the case as decoded, for key-level validation
}

const (
	envChildCase  = "PARITY_CHILD_CASE"
	envChildOut   = "PARITY_CHILD_OUT"
	envGoCoverDir = "PARITY_GOCOVERDIR" // children write coverage here (merged by the caller)
	envRequire    = "PARITY_REQUIRE_FAKEOTLP"
	childTimeout  = 90 * time.Second
)

// ── diagnostics capture (child) ───────────────────────────────────────────

type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

var activeCapture atomic.Pointer[syncBuffer]

func captureWrite(s string) {
	if b := activeCapture.Load(); b != nil {
		_, _ = b.Write([]byte(s + "\n"))
		return
	}
	fmt.Fprintln(os.Stderr, s)
}

// helperSourceDir is the helper package directory: slog/log records whose
// call site is a non-test file here are helper diagnostics (helper_log).
var helperSourceDir = func() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Dir(file) + string(filepath.Separator)
}()

// captureHandler writes every record to all and, when it was emitted from the
// helper package itself, also to helper.
type captureHandler struct{ all, helper slog.Handler }

func (h captureHandler) Enabled(context.Context, slog.Level) bool { return true }

func (h captureHandler) Handle(ctx context.Context, r slog.Record) error {
	_ = h.all.Handle(ctx, r)
	if r.PC != 0 {
		f, _ := runtime.CallersFrames([]uintptr{r.PC}).Next()
		if strings.HasPrefix(f.File, helperSourceDir) && !strings.HasSuffix(f.File, "_test.go") {
			_ = h.helper.Handle(ctx, r)
		}
	}
	return nil
}

func (h captureHandler) WithAttrs(a []slog.Attr) slog.Handler {
	return captureHandler{h.all.WithAttrs(a), h.helper.WithAttrs(a)}
}

func (h captureHandler) WithGroup(n string) slog.Handler {
	return captureHandler{h.all.WithGroup(n), h.helper.WithGroup(n)}
}

// startCapture routes slog/log and otel diagnostics into buffers. Child only:
// the otel error handler can't be restored, and the child exits after one case.
// Helper diagnostics are records the helper package emits through log or slog;
// otel error-handler / internal-logger output counts as SDK output.
func startCapture() (all, helper *syncBuffer) {
	all, helper = &syncBuffer{}, &syncBuffer{}
	activeCapture.Store(all)
	otel.SetErrorHandler(otel.ErrorHandlerFunc(func(err error) { captureWrite("otel error: " + err.Error()) }))
	otel.SetLogger(funcr.New(func(prefix, args string) { captureWrite("otel log: " + prefix + " " + args) }, funcr.Options{Verbosity: 1}))
	log.SetFlags(log.Lshortfile) // makes the log→slog bridge record the caller PC
	opts := &slog.HandlerOptions{Level: slog.LevelDebug}
	capture := slog.New(captureHandler{slog.NewTextHandler(all, opts), slog.NewTextHandler(helper, opts)})
	slog.SetDefault(capture)
	diagLogger = capture // the helper's own diagnostics channel (design D6)
	return all, helper
}

// ── receiver ──────────────────────────────────────────────────────────────

// fakeReceiver is the fakeotlp service; nil when FAKEOTLP_HOST is unset.
type fakeReceiver struct{ host, hostTLS, hostIPv6, admin, ca string }

func receiverFromEnv() *fakeReceiver {
	h := os.Getenv("FAKEOTLP_HOST")
	if h == "" {
		return nil
	}
	return &fakeReceiver{host: h, hostTLS: os.Getenv("FAKEOTLP_HOST_TLS"), hostIPv6: os.Getenv("FAKEOTLP_HOST_IPV6"),
		admin: os.Getenv("FAKEOTLP_ADMIN"), ca: os.Getenv("FAKEOTLP_CA")}
}

// expand replaces placeholders; missing reports an unset optional address.
func (r *fakeReceiver) expand(s string) (out string, missing string) {
	switch {
	case strings.Contains(s, "{receiver_ipv6}") && r.hostIPv6 == "":
		return s, "FAKEOTLP_HOST_IPV6"
	case strings.Contains(s, "{receiver_tls}") && r.hostTLS == "":
		return s, "FAKEOTLP_HOST_TLS"
	}
	return strings.NewReplacer("{receiver_ipv6}", r.hostIPv6, "{receiver_tls}", r.hostTLS,
		"{receiver_ca}", r.ca, "{receiver}", r.host).Replace(s), ""
}

// destIPs: records sent to other local addresses are not this case's.
func (r *fakeReceiver) destIPs(t *testing.T) map[string]bool {
	t.Helper()
	out := map[string]bool{}
	for _, h := range []string{r.host, r.hostTLS, strings.Trim(r.hostIPv6, "[]")} {
		if h == "" {
			continue
		}
		ips, err := net.LookupIP(h)
		if err != nil {
			t.Fatalf("resolve receiver address %q: %v", h, err)
		}
		for _, ip := range ips {
			out[ip.String()] = true
		}
	}
	return out
}

var adminClient = &http.Client{Timeout: 10 * time.Second}

func adminDo(t *testing.T, method, admin string) []byte {
	t.Helper()
	req, err := http.NewRequest(method, strings.TrimRight(admin, "/")+"/requests", nil)
	if err != nil {
		t.Fatalf("fake receiver admin request: %v", err)
	}
	resp, err := adminClient.Do(req)
	if err != nil {
		t.Fatalf("fake receiver admin %s: %v", method, err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("fake receiver admin %s: HTTP %d: %s", method, resp.StatusCode, b)
	}
	return b
}

// noReceiver skips locally, fails when the receiver is required (CI, compose).
func noReceiver(t *testing.T, what string) {
	t.Helper()
	if os.Getenv(envRequire) != "" {
		t.Fatalf("%s is set but %s is not: export cases must run", envRequire, what)
	}
	t.Skipf("case needs the fake OTLP receiver (%s unset; see testing/README.md)", what)
}

// ── parent ────────────────────────────────────────────────────────────────

func loadParityCases(t *testing.T) []parityCase {
	t.Helper()
	path := os.Getenv("PARITY_CASES")
	if path == "" {
		path = filepath.Join("..", "parity", "cases.json")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var f struct {
		Cases []json.RawMessage `json:"cases"`
	}
	if err := json.Unmarshal(raw, &f); err != nil {
		t.Fatalf("parse fixture: %v", err)
	}
	cases := make([]parityCase, 0, len(f.Cases))
	for i, rc := range f.Cases {
		var c parityCase
		// A shape error (e.g. env not an object) becomes a per-case fixture error.
		if err := json.Unmarshal(rc, &c); err != nil {
			c = parityCase{ID: fmt.Sprintf("malformed-case-%d", i), Kind: "<malformed>"}
		}
		_ = json.Unmarshal(rc, &c.raw)
		cases = append(cases, c)
	}
	return cases
}

func TestParity(t *testing.T) {
	recv := receiverFromEnv()
	cases := loadParityCases(t)
	if id := os.Getenv(envChildCase); id != "" {
		runChild(t, cases, id, recv)
		return
	}
	for _, c := range cases {
		t.Run(c.ID, func(t *testing.T) {
			if errs := validateCase(c); len(errs) > 0 {
				t.Fatalf("fixture error (fails regardless of status): %s", strings.Join(errs, "; "))
			}
			status := c.Status["go"]
			if strings.HasPrefix(status, "skip:") {
				t.Skip(status)
			}
			if c.Kind != "export" {
				t.Parallel() // export cases share the receiver's request log
			}
			mismatches := runInChild(t, c, recv)
			switch {
			case status == "pass" && len(mismatches) > 0:
				t.Errorf("parity case %s failed:\n  %s", c.ID, strings.Join(mismatches, "\n  "))
			case status != "pass" && len(mismatches) == 0:
				t.Errorf("XPASS (strict): %s passes but is marked %q — flip its go status to \"pass\"", c.ID, status)
			case status != "pass":
				t.Skipf("XFAIL %s (expected): %s", status, strings.Join(mismatches, "; "))
			}
		})
	}
}

// childEnv: parent environment minus every OTel/helper variable, plus the case's.
func childEnv(t *testing.T, c parityCase, recv *fakeReceiver) []string {
	var env []string
	for _, kv := range os.Environ() {
		k, _, _ := strings.Cut(kv, "=")
		if strings.HasPrefix(k, "OTEL_") || k == EnvServiceName || k == EnvEnvironment || strings.HasPrefix(k, "PARITY_CHILD_") {
			continue
		}
		env = append(env, kv)
	}
	env = append(env, EnvMetricsPort+"=0") // never bind :9464 in tests; cases may override
	keys := make([]string, 0, len(c.Env))
	for k := range c.Env {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		v := c.Env[k]
		if strings.Contains(v, "{receiver") {
			if recv == nil {
				noReceiver(t, "FAKEOTLP_HOST")
			}
			var missing string
			if v, missing = recv.expand(v); missing != "" {
				noReceiver(t, missing)
			}
		}
		env = append(env, k+"="+v)
	}
	if c.Kind == "export" && recv == nil {
		noReceiver(t, "FAKEOTLP_HOST")
	}
	return env
}

// runInChild executes one case in a child process and matches its snapshot.
// Adapter errors are fatal here, so they can never satisfy an xfail.
func runInChild(t *testing.T, c parityCase, recv *fakeReceiver) []string {
	env := childEnv(t, c, recv)
	if raw, _ := json.Marshal(c.Code); recv != nil {
		if _, missing := recv.expand(string(raw)); missing != "" {
			noReceiver(t, missing)
		}
	} else if strings.Contains(string(raw), "{receiver") {
		noReceiver(t, "FAKEOTLP_HOST")
	}
	out := filepath.Join(t.TempDir(), "snapshot.json")
	args := []string{"-test.run=^TestParity$", "-test.count=1"}
	if dir := os.Getenv(envGoCoverDir); dir != "" {
		args = append(args, "-test.gocoverdir="+dir)
	}
	ctx, cancel := context.WithTimeout(context.Background(), childTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], args...)
	cmd.Env = append(env, envChildCase+"="+c.ID, envChildOut+"="+out)
	output, err := cmd.CombinedOutput()
	if c.Kind == "export" && recv != nil {
		// Clear anything that reached the receiver after the child read it
		// (late flush), so the next export case starts from an empty log.
		adminDo(t, http.MethodDelete, recv.admin)
	}
	raw, readErr := os.ReadFile(out)
	if readErr != nil {
		t.Fatalf("child for %s produced no snapshot (err=%v):\n%s", c.ID, err, output)
	}
	var snap map[string]any
	if err := json.Unmarshal(raw, &snap); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	if ae, ok := snap["adapter_error"]; ok {
		t.Fatalf("adapter error (fails regardless of status): %v", ae)
	}
	// The child's own stdout/stderr are part of what the helper printed.
	console := string(output) + fmt.Sprint(snap["console_capture"])
	delete(snap, "console_capture")
	snap["console"] = console
	snap["log"] = fmt.Sprint(snap["diagnostics"]) + "\n" + console
	return matchExpect(c.Expect, snap)
}

// ── child ─────────────────────────────────────────────────────────────────

func runChild(t *testing.T, cases []parityCase, id string, recv *fakeReceiver) {
	var c *parityCase
	for i := range cases {
		if cases[i].ID == id {
			c = &cases[i]
		}
	}
	if c == nil {
		t.Fatalf("unknown case %q", id)
	}
	snap := runCaseKind(t, *c, recv)
	raw, err := json.Marshal(snap)
	if err != nil {
		t.Fatalf("encode snapshot: %v", err)
	}
	if err := os.WriteFile(os.Getenv(envChildOut), raw, 0o600); err != nil {
		t.Fatalf("write snapshot: %v", err)
	}
}

func runCaseKind(t *testing.T, c parityCase, recv *fakeReceiver) map[string]any {
	opts, unsupported := codeOptions(c.Code, recv)
	if len(unsupported) > 0 {
		return map[string]any{"adapter_error": "unsupported code key(s) " + strings.Join(unsupported, ", ")}
	}
	diag, helperDiag := startCapture()
	var snap map[string]any
	switch c.Kind {
	case "resolve":
		snap = runResolve(opts)
	case "export":
		snap = runExport(t, opts, recv)
	case "sampler":
		snap = runSampler(opts, c.Input)
	case "propagation":
		snap = runPropagation(opts)
	case "http_server":
		snap = runHTTPServer(t, opts, c.Input)
	case "grpc_server":
		snap = runGRPCServer(t, opts, c.Input)
	case "logging":
		snap = runLogging(t, opts, c.Input)
	case "metrics_scrape":
		snap = runMetricsScrape(opts)
	case "metrics_listener":
		snap = runMetricsListener(t, opts, c.Input)
	default:
		return map[string]any{"adapter_error": "unknown kind " + c.Kind}
	}
	if s, ok := snap["adapter_error"]; ok {
		return map[string]any{"adapter_error": s}
	}
	snap["diagnostics"] = diag.String()
	snap["helper_log"] = helperDiag.String()
	return snap
}

// codeOptions maps the fixture's snake_case code keys to functional options.
// An unknown key or a value of the wrong type (e.g. a non-integer number for
// an integer option) is reported as unsupported: an adapter error, never a
// panic and never a silent conversion.
func codeOptions(code map[string]any, recv *fakeReceiver) ([]Option, []string) {
	var opts []Option
	var unsupported []string
	for _, k := range sortedKeys(code) {
		v := code[k]
		bad := func() { unsupported = append(unsupported, fmt.Sprintf("%s (value %T)", k, v)) }
		str := func() (string, bool) {
			s, ok := v.(string)
			if ok && recv != nil {
				s, _ = recv.expand(s)
			}
			return s, ok
		}
		integer := func() (int, bool) {
			f, ok := v.(float64)
			if !ok || f != math.Trunc(f) || math.Abs(f) > 1<<31 {
				return 0, false
			}
			return int(f), true
		}
		strOpt := map[string]func(string) Option{
			"service_name":     WithServiceName,
			"environment":      func(s string) Option { return WithEnvironment(DeploymentEnvironment(s)) },
			"otel_endpoint":    WithEndpoint,
			"traces_endpoint":  WithTracesEndpoint,
			"metrics_endpoint": WithMetricsEndpoint,
			"logs_endpoint":    WithLogsEndpoint,
			"compression":      WithCompression,
			"otlp_protocol":    WithOtlpProtocol,
		}
		intOpt := map[string]func(int) Option{
			"export_timeout_ms":  WithExportTimeout,
			"export_interval_ms": WithExportInterval,
			// A29 fix round (obs L7): code beats OTEL_EXPORTER_PROMETHEUS_PORT.
			"prometheus_metrics_port": WithPrometheusMetricsPort,
		}
		if f, ok := strOpt[k]; ok {
			if s, ok := str(); ok {
				opts = append(opts, f(s))
			} else {
				bad()
			}
			continue
		}
		if f, ok := intOpt[k]; ok {
			if n, ok := integer(); ok {
				opts = append(opts, f(n))
			} else {
				bad()
			}
			continue
		}
		switch k {
		case "insecure":
			if b, ok := v.(bool); ok {
				opts = append(opts, WithInsecure(b))
			} else {
				bad()
			}
		case "http_opt_in_metrics":
			if b, ok := v.(bool); ok {
				opts = append(opts, WithHTTPOptInMetrics(b))
			} else {
				bad()
			}
		case "debug":
			if b, ok := v.(bool); !ok {
				bad()
			} else if b {
				opts = append(opts, WithDebug())
			}
		case "sample_ratio":
			if f, ok := v.(float64); ok {
				opts = append(opts, WithSampleRatio(f))
			} else {
				bad()
			}
		case "metric_exporters":
			items, ok := v.([]any)
			var list []string
			for _, e := range items {
				s, isStr := e.(string)
				ok = ok && isStr
				list = append(list, s)
			}
			if ok {
				opts = append(opts, WithMetricExporters(list...))
			} else {
				bad()
			}
		case "disabled_signals":
			items, ok := v.([]any)
			list := []string{}
			for _, e := range items {
				s, isStr := e.(string)
				ok = ok && isStr
				list = append(list, s)
			}
			if ok {
				opts = append(opts, WithDisabledSignals(list))
			} else {
				bad()
			}
		case "health_paths":
			items, ok := v.([]any)
			list := []string{}
			for _, e := range items {
				s, isStr := e.(string)
				ok = ok && isStr
				list = append(list, s)
			}
			if ok {
				opts = append(opts, WithHealthPaths(list...))
			} else {
				bad()
			}
		case "resource_attributes":
			m, ok := v.(map[string]any)
			attrs := map[string]string{}
			for ak, av := range m {
				s, isStr := av.(string)
				ok = ok && isStr
				attrs[ak] = s
			}
			if ok {
				opts = append(opts, WithResourceAttributes(attrs))
			} else {
				bad()
			}
		default:
			unsupported = append(unsupported, k)
		}
	}
	return opts, unsupported
}

func setupForCase(opts []Option) (Shutdown, map[string]any) {
	snap := map[string]any{}
	shutdown, err := Setup(context.Background(), opts...)
	if err != nil {
		snap["error"] = err.Error()
	}
	return shutdown, snap
}

func shutdownQuick(shutdown Shutdown) { shutdownWithin(shutdown, 5*time.Second) }

// exportShutdownDeadline bounds the flush of export cases: long enough that a
// loaded runner still delivers every signal, and above 60 s so
// export.timeout_env_above_batch_default_grpc can see a 60 s export timeout
// on the wire (grpc-timeout = min(exporter timeout, this deadline)). Reachable
// cases finish in milliseconds; only unreachable signals wait, bounded by the
// exporter timeout (10 s default).
const exportShutdownDeadline = 75 * time.Second

func shutdownWithin(shutdown Shutdown, d time.Duration) {
	ctx, cancel := context.WithTimeout(context.Background(), d)
	defer cancel()
	_ = shutdown(ctx)
}

// installedResource reads the resource from a span of the installed provider.
func installedResource() map[string]any {
	_, span := otel.Tracer("parity").Start(context.Background(), "resource-probe")
	defer span.End()
	res := map[string]any{}
	if ro, ok := span.(sdktrace.ReadOnlySpan); ok {
		for _, kv := range ro.Resource().Attributes() {
			res[string(kv.Key)] = kv.Value.Emit()
		}
	}
	return res
}

func runResolve(opts []Option) map[string]any {
	shutdown, snap := setupForCase(opts)
	defer shutdownQuick(shutdown)
	if snap["error"] != nil {
		return snap
	}
	snap["resource"] = installedResource()
	o := installedOptions()
	if o == nil {
		return map[string]any{"adapter_error": "Setup succeeded but recorded no installed options"}
	}
	// The effective list (parity/README.md): [] when metrics or the SDK are disabled.
	exporters := []any{}
	if !o.sdkDisabled && o.IsSignalEnabled("metrics") {
		for _, e := range o.resolvedMetricExporters() {
			exporters = append(exporters, e)
		}
	}
	snap["metric_exporters"] = exporters
	snap["metric_export_interval_ms"] = float64(o.metricIntervalMs)
	snap["log_level"] = DefaultLogLevel(o.Environment, o.DebugLevel).String()
	snap["prometheus_metrics_port"] = float64(o.PrometheusMetricsPort)
	snap["prometheus_metrics_host"] = o.prometheusHost // "" = every interface (A29)
	snap["http_opt_in_metrics"] = o.HTTPOptInMetrics   // A30
	if f := installedExemplarFilter(); f != "" {
		snap["exemplar_filter"] = f // .NET C9 fix round / A33
	}
	return snap
}

// installedExemplarFilter names the exemplar filter of the installed SDK
// MeterProvider ("" when none is installed). The helper leaves
// OTEL_METRICS_EXEMPLAR_FILTER to the SDK (G7), which keeps the filter in an
// unexported pipeline field: read by reflection, compared by code pointer.
func installedExemplarFilter() string {
	mp, ok := otel.GetMeterProvider().(*sdkmetric.MeterProvider)
	if !ok {
		return ""
	}
	pipes := reflect.ValueOf(mp).Elem().FieldByName("pipes")
	if !pipes.IsValid() || pipes.Len() == 0 {
		return ""
	}
	f := pipes.Index(0).Elem().FieldByName("exemplarFilter")
	if !f.IsValid() || f.IsNil() {
		return ""
	}
	known := map[string]exemplar.Filter{
		"always_on":   exemplar.AlwaysOnFilter,
		"always_off":  exemplar.AlwaysOffFilter,
		"trace_based": exemplar.TraceBasedFilter,
	}
	for name, filter := range known {
		if f.Pointer() == reflect.ValueOf(filter).Pointer() {
			return name
		}
	}
	return "unknown"
}

type wireRecord struct {
	Addr     string            `json:"addr"`
	Proto    string            `json:"proto"`
	TLS      bool              `json:"tls"`
	Port     string            `json:"port"`
	Path     string            `json:"path"`
	RawPath  string            `json:"raw_path"`
	Encoding string            `json:"encoding"`
	Timeout  string            `json:"timeout"`
	Headers  map[string]string `json:"headers"`
}

// grpcTimeoutMs converts a grpc-timeout header ("<digits><H|M|S|m|u|n>") to ms.
func grpcTimeoutMs(v string) (float64, bool) {
	if len(v) < 2 {
		return 0, false
	}
	n, err := strconv.ParseFloat(v[:len(v)-1], 64)
	if err != nil {
		return 0, false
	}
	unit := map[byte]float64{'H': 3.6e6, 'M': 6e4, 'S': 1e3, 'm': 1, 'u': 1e-3, 'n': 1e-6}[v[len(v)-1]]
	if unit == 0 {
		return 0, false
	}
	return n * unit, true
}

func runExport(t *testing.T, opts []Option, recv *fakeReceiver) map[string]any {
	t.Helper()
	dest := recv.destIPs(t)
	adminDo(t, http.MethodDelete, recv.admin)
	shutdown, snap := setupForCase(opts)
	ctx := context.Background()
	if snap["error"] == nil {
		_, span := GetTracer("parity").Start(ctx, "parity-span")
		span.End()
		counter, _ := GetMeter("parity").Int64Counter("parity.requests")
		counter.Add(ctx, 1)
		var rec otellog.Record
		rec.SetBody(attribute.StringValue("parity-log"))
		rec.SetSeverity(otellog.SeverityWarn)
		logglobal.GetLoggerProvider().Logger("parity").Emit(ctx, rec)
	}
	shutdownWithin(shutdown, exportShutdownDeadline)
	var recs []wireRecord
	if err := json.Unmarshal(adminDo(t, http.MethodGet, recv.admin), &recs); err != nil {
		t.Fatalf("decode receiver records: %v", err)
	}
	received := map[string]any{"traces": []any{}, "metrics": []any{}, "logs": []any{}, "other": []any{}}
	for _, r := range recs {
		if !dest[r.Addr] {
			continue
		}
		port, _ := strconv.Atoi(r.Port)
		hdrs := map[string]any{}
		for k, v := range r.Headers {
			hdrs[k] = v
		}
		m := map[string]any{"proto": r.Proto, "tls": r.TLS, "port": float64(port), "path": r.Path, "raw_path": r.RawPath, "encoding": r.Encoding, "headers": hdrs}
		if ms, ok := grpcTimeoutMs(r.Timeout); ok {
			m["timeout_ms"] = ms
		}
		s := signalOf(r.Proto, r.Path)
		received[s] = append(received[s].([]any), m)
	}
	snap["received"] = received
	return snap
}

// signalOf classifies a recorded request: gRPC by service name, HTTP by path.
func signalOf(proto, path string) string {
	p := strings.ToLower(path)
	tokens := [][2]string{{"trace", "traces"}, {"metric", "metrics"}, {"log", "logs"}}
	if proto == "grpc" {
		tokens = [][2]string{{".trace.", "traces"}, {".metrics.", "metrics"}, {".logs.", "logs"}}
	}
	for _, tk := range tokens {
		if strings.Contains(p, tk[0]) {
			return tk[1]
		}
	}
	return "other"
}

var (
	parityTraceID, _ = trace.TraceIDFromHex("0af7651916cd43dd8448eb211c80319c")
	paritySpanID, _  = trace.SpanIDFromHex("b7ad6b7169203331")
)

func remoteParent(sampled, debugTraceState bool) context.Context {
	cfg := trace.SpanContextConfig{TraceID: parityTraceID, SpanID: paritySpanID, Remote: true}
	if sampled {
		cfg.TraceFlags = trace.FlagsSampled
	}
	if debugTraceState {
		cfg.TraceState, _ = trace.ParseTraceState("debug=true")
	}
	return trace.ContextWithRemoteSpanContext(context.Background(), trace.NewSpanContext(cfg))
}

func runSampler(opts []Option, input map[string]any) map[string]any {
	shutdown, snap := setupForCase(opts)
	defer shutdownQuick(shutdown)
	tr := GetTracer("parity")
	var span trace.Span
	switch input["scenario"] {
	case "root":
		_, span = tr.Start(context.Background(), "target")
	case "remote_sampled":
		_, span = tr.Start(remoteParent(true, false), "target")
	case "remote_unsampled":
		_, span = tr.Start(remoteParent(false, false), "target")
	case "remote_sampled_debug_tracestate":
		_, span = tr.Start(remoteParent(true, true), "target")
	case "remote_sampled_debug_baggage":
		m, _ := baggage.NewMember("debug", "true")
		bag, _ := baggage.New(m)
		_, span = tr.Start(baggage.ContextWithBaggage(remoteParent(true, false), bag), "target")
	case "local_child_of_remote_unsampled", "local_child_of_root":
		parentCtx := context.Background()
		if input["scenario"] == "local_child_of_remote_unsampled" {
			parentCtx = remoteParent(false, false)
		}
		ctx, parent := tr.Start(parentCtx, "parent")
		defer parent.End()
		_, span = tr.Start(ctx, "target")
	default:
		return map[string]any{"adapter_error": fmt.Sprintf("unknown scenario %v", input["scenario"])}
	}
	defer span.End()
	snap["sampled"] = span.SpanContext().IsSampled()
	attrs := map[string]any{}
	if ro, ok := span.(sdktrace.ReadOnlySpan); ok {
		for _, kv := range ro.Attributes() {
			attrs[string(kv.Key)] = kv.Value.Emit()
		}
	}
	snap["attributes"] = attrs
	return snap
}

func runPropagation(opts []Option) map[string]any {
	shutdown, snap := setupForCase(opts)
	defer shutdownQuick(shutdown)
	m, _ := baggage.NewMember("parity", "1")
	bag, _ := baggage.New(m)
	ctx, span := GetTracer("parity").Start(baggage.ContextWithBaggage(context.Background(), bag), "parity-span")
	defer span.End()
	carrier := propagation.MapCarrier{}
	otel.GetTextMapPropagator().Inject(ctx, carrier)
	keys := carrier.Keys()
	sort.Strings(keys)
	list := []any{}
	for _, k := range keys {
		list = append(list, strings.ToLower(k))
	}
	snap["headers"] = list
	return snap
}

// recordSpans attaches an in-memory exporter to the helper-installed provider.
func recordSpans(t *testing.T) *tracetest.InMemoryExporter {
	t.Helper()
	tp, ok := otel.GetTracerProvider().(*sdktrace.TracerProvider)
	if !ok {
		t.Fatalf("global tracer provider is %T, want *sdktrace.TracerProvider", otel.GetTracerProvider())
	}
	exp := tracetest.NewInMemoryExporter()
	tp.RegisterSpanProcessor(sdktrace.NewSimpleSpanProcessor(exp))
	return exp
}

func countSpans(exp *tracetest.InMemoryExporter, match func(tracetest.SpanStub) bool) float64 {
	n := 0
	for _, s := range exp.GetSpans() {
		if match(s) {
			n++
		}
	}
	return float64(n)
}

func isServer(s tracetest.SpanStub) bool { return s.SpanKind == trace.SpanKindServer }

// serverContinuesInbound reports whether there is a SERVER span and every
// SERVER span is in the trace of the inbound traceparent (fixture field
// server_continues_inbound; false without an inbound traceparent).
func serverContinuesInbound(exp *tracetest.InMemoryExporter, traceparent string) bool {
	parts := strings.Split(traceparent, "-")
	if len(parts) != 4 {
		return false
	}
	n := 0
	for _, s := range exp.GetSpans() {
		if !isServer(s) {
			continue
		}
		if s.SpanContext.TraceID().String() != parts[1] {
			return false
		}
		n++
	}
	return n > 0
}

// serverSpanStatus is the status code of the first SERVER span ("UNSET",
// "ERROR", "OK"), or "none" without one (fixture field server_span_status).
func serverSpanStatus(exp *tracetest.InMemoryExporter) string {
	for _, s := range exp.GetSpans() {
		if isServer(s) {
			return strings.ToUpper(s.Status.Code.String())
		}
	}
	return "none"
}

// serverSpanName is the name of the first SERVER span, or "none" without one
// (fixture field server_span_name; added 2026-11-05, A30).
func serverSpanName(exp *tracetest.InMemoryExporter) string {
	for _, s := range exp.GetSpans() {
		if isServer(s) {
			return s.Name
		}
	}
	return "none"
}

// clientSpanName is the name of the first CLIENT span, or "none" without one
// (fixture field client_span_name; added 2026-11-08, A30).
func clientSpanName(exp *tracetest.InMemoryExporter) string {
	for _, s := range exp.GetSpans() {
		if s.SpanKind == trace.SpanKindClient {
			return s.Name
		}
	}
	return "none"
}

func scrapeText() string {
	rr := httptest.NewRecorder()
	MetricsHandler().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	return rr.Body.String()
}

// scrapeCount sums every sample whose line matches re in the helper's /metrics.
func scrapeCount(re *regexp.Regexp) float64 {
	var total float64
	sc := bufio.NewScanner(strings.NewReader(scrapeText()))
	for sc.Scan() {
		line := sc.Text()
		if strings.HasPrefix(line, "#") || !re.MatchString(line) {
			continue
		}
		fields := strings.Fields(line)
		if v, err := strconv.ParseFloat(fields[len(fields)-1], 64); err == nil {
			total += v
		}
	}
	return total
}

var (
	httpServerCountRe = regexp.MustCompile(`^http_server(_request)?_duration[a-z_]*_count[{ ]`)
	rpcServerCountRe  = regexp.MustCompile(`^rpc_server(_call)?_duration[a-z_]*_count[{ ]`)
)

func runHTTPServer(t *testing.T, opts []Option, input map[string]any) map[string]any {
	shutdown, snap := setupForCase(opts)
	defer shutdownQuick(shutdown)
	if snap["error"] != nil {
		return snap
	}
	exp := recordSpans(t)
	childSpan, _ := input["child_span"].(bool)
	h := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if childSpan {
			_, s := GetTracer("parity").Start(r.Context(), "handler-child")
			s.End()
		}
		_, _ = w.Write([]byte("ok"))
	}), "parity")
	tp, _ := input["traceparent"].(string)
	statuses := []any{}
	serverError := false
	for _, p := range input["requests"].([]any) {
		req := httptest.NewRequest(http.MethodGet, p.(string), nil)
		if tp != "" {
			req.Header.Set("traceparent", tp)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		statuses = append(statuses, float64(rec.Code))
		serverError = serverError || rec.Code >= 500
	}
	snap["response_statuses"] = statuses
	if allow, _ := input["allow_server_error"].(bool); serverError && !allow {
		return map[string]any{"adapter_error": "a request answered 5xx (an instrumentation or helper failure, not an expectation); set input.allow_server_error to accept it"}
	}
	snap["server_spans"] = countSpans(exp, isServer)
	snap["server_continues_inbound"] = serverContinuesInbound(exp, tp)
	snap["handler_child_spans"] = countSpans(exp, func(s tracetest.SpanStub) bool { return s.Name == "handler-child" })
	snap["scrape"] = scrapeText()
	snap["requests_counted"] = scrapeCount(httpServerCountRe)
	return snap
}

func runGRPCServer(t *testing.T, opts []Option, input map[string]any) map[string]any {
	shutdown, snap := setupForCase(opts)
	defer shutdownQuick(shutdown)
	if snap["error"] != nil {
		return snap
	}
	exp := recordSpans(t)
	lis := bufconn.Listen(1 << 20)
	srv := grpc.NewServer(
		GRPCServerOption(),
		grpc.UnknownServiceHandler(func(_ any, s grpc.ServerStream) error { // "/parity.v1.Echo/Ping", ".../NotFound"
			var m emptypb.Empty
			if err := s.RecvMsg(&m); err != nil {
				return err
			}
			if name, _ := grpc.MethodFromServerStream(s); strings.HasSuffix(name, "/NotFound") {
				return grpcstatus.Error(codes.NotFound, "not found")
			}
			return s.SendMsg(&emptypb.Empty{})
		}),
	)
	healthpb.RegisterHealthServer(srv, health.NewServer())
	go func() { _ = srv.Serve(lis) }()
	conn, err := grpc.NewClient("passthrough:///parity",
		GRPCDialOption(), // client span name in the snapshot (A30, 2026-11-08)
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return lis.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return map[string]any{"adapter_error": "grpc client: " + err.Error()}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	method := input["method"].(string)
	switch method {
	case "/grpc.health.v1.Health/Check":
		_, err = healthpb.NewHealthClient(conn).Check(ctx, &healthpb.HealthCheckRequest{})
	case "/grpc.health.v1.Health/Watch":
		wctx, wcancel := context.WithCancel(ctx)
		var stream healthpb.Health_WatchClient
		if stream, err = healthpb.NewHealthClient(conn).Watch(wctx, &healthpb.HealthCheckRequest{}); err == nil {
			_, err = stream.Recv()
		}
		wcancel()
	default:
		err = conn.Invoke(ctx, method, &emptypb.Empty{}, &emptypb.Empty{})
	}
	snap["call_ok"] = err == nil
	_ = conn.Close()
	srv.GracefulStop() // server spans end when handlers return (after cancel for Watch)
	snap["server_spans"] = countSpans(exp, isServer)
	snap["server_span_status"] = serverSpanStatus(exp)
	snap["server_span_name"] = serverSpanName(exp)
	snap["client_span_name"] = clientSpanName(exp)
	snap["rpcs_counted"] = scrapeCount(rpcServerCountRe)
	return snap
}

// runLogging emits one record through the helper's public logger (Go apps
// build their logger with NewLogger) and reports the process console.
// The fixed span context of the logging kind's in_span input (parity/README.md).
const (
	paritySpanTraceID = "4bf92f3577b34da6a3ce929d0e0e4736"
	paritySpanSpanID  = "00f067aa0ba902b7"
)

func runLogging(t *testing.T, opts []Option, input map[string]any) map[string]any {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		return map[string]any{"adapter_error": err.Error()}
	}
	origOut, origErr := os.Stdout, os.Stderr
	os.Stdout, os.Stderr = w, w
	var console bytes.Buffer
	done := make(chan struct{})
	go func() { _, _ = io.Copy(&console, r); close(done) }()

	shutdown, snap := setupForCase(opts)
	env, debug := DeploymentEnvironment(""), false
	if o := installedOptions(); o != nil {
		env, debug = o.Environment, o.DebugLevel
	}
	// input.attributes: the record's attributes, in key order.
	attrs, _ := input["attributes"].(map[string]any)
	keys := make([]string, 0, len(attrs))
	for k := range attrs {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	args := make([]any, 0, 2*len(keys))
	for _, k := range keys {
		args = append(args, k, attrs[k])
	}
	// input.in_span: the record is written inside a span (contract A23 amendment 2026-11-11).
	if inSpan, _ := input["in_span"].(bool); inSpan {
		// A valid, non-recording span context with fixed ids, whatever tracer is installed.
		tid, _ := trace.TraceIDFromHex(paritySpanTraceID)
		sid, _ := trace.SpanIDFromHex(paritySpanSpanID)
		ctx := trace.ContextWithSpanContext(context.Background(), trace.NewSpanContext(trace.SpanContextConfig{TraceID: tid, SpanID: sid, TraceFlags: trace.FlagsSampled}))
		NewLogger(env, debug).WarnContext(ctx, "parity-console-record", args...)
	} else {
		NewLogger(env, debug).Warn("parity-console-record", args...)
	}
	shutdownQuick(shutdown)
	os.Stdout, os.Stderr = origOut, origErr
	_ = w.Close()
	<-done
	snap["console_capture"] = console.String()
	return snap
}

// runMetricsListener starts the helper's own /metrics listener on a free port
// (OTEL_HELPER_METRICS_PORT set by the driver) and records the status of each
// "METHOD /path" request.
func runMetricsListener(t *testing.T, opts []Option, input map[string]any) map[string]any {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return map[string]any{"adapter_error": "free port: " + err.Error()}
	}
	port := ln.Addr().(*net.TCPAddr).Port
	_ = ln.Close()
	t.Setenv(EnvMetricsPort, strconv.Itoa(port))
	shutdown, snap := setupForCase(opts)
	defer shutdownQuick(shutdown)
	if snap["error"] != nil {
		return snap
	}
	client := &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{DisableKeepAlives: true}}
	// input.connect_host: "127.0.0.1" (default) or "localhost" (every address the resolver returns).
	host := "127.0.0.1"
	if h, ok := input["connect_host"].(string); ok {
		host = h
	}
	statuses := []any{}
	for _, r := range input["requests"].([]any) {
		method, target, _ := strings.Cut(r.(string), " ")
		req, err := http.NewRequest(method, fmt.Sprintf("http://%s:%d%s", host, port, target), nil)
		if err != nil {
			return map[string]any{"adapter_error": "request: " + err.Error()}
		}
		resp, err := client.Do(req)
		if err != nil {
			return map[string]any{"adapter_error": "request: " + err.Error()}
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		_ = resp.Body.Close()
		statuses = append(statuses, float64(resp.StatusCode))
	}
	snap["listener_statuses"] = statuses
	return snap
}

func runMetricsScrape(opts []Option) map[string]any {
	shutdown, snap := setupForCase(opts)
	defer shutdownQuick(shutdown)
	if snap["error"] != nil {
		return snap
	}
	counter, _ := GetMeter("parity").Int64Counter("parity.requests")
	counter.Add(context.Background(), 1)
	// A counter with a unit: the scrape name carries the unit word.
	payload, _ := GetMeter("parity").Int64Counter("parity.payload", metric.WithUnit("By"))
	payload.Add(context.Background(), 5)
	// Unit 1: a Gauge gets _ratio, an UpDownCounter does not (design D1 row "Unit 1", A21).
	util, _ := GetMeter("parity").Float64Gauge("parity.utilization", metric.WithUnit("1"))
	util.Record(context.Background(), 0.5)
	inflight, _ := GetMeter("parity").Int64UpDownCounter("parity.inflight", metric.WithUnit("1"))
	inflight.Add(context.Background(), 1)
	// B7/A21 fix round: colliding label names, suffix semantics, a mixed-case name.
	collide, _ := GetMeter("parity").Int64Counter("parity.collide")
	collide.Add(context.Background(), 1, metric.WithAttributes(attribute.String("a.b", "x"), attribute.String("a_b", "y")))
	collideSorted, _ := GetMeter("parity").Int64Counter("parity.collide_sorted")
	collideSorted.Add(context.Background(), 1, metric.WithAttributes(attribute.String("a.b", "y"), attribute.String("a_b", "x")))
	sent, _ := GetMeter("parity").Int64Counter("probe.bytes-sent", metric.WithUnit("By"))
	sent.Add(context.Background(), 1)
	totalCount, _ := GetMeter("parity").Int64Counter("req.total.count")
	totalCount.Add(context.Background(), 1)
	load, _ := GetMeter("parity").Float64Gauge("ratio.load", metric.WithUnit("1"))
	load.Record(context.Background(), 1)
	mixed, _ := GetMeter("parity").Int64Counter("Parity.Mixed")
	mixed.Add(context.Background(), 1)
	rr := httptest.NewRecorder()
	MetricsHandler().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	snap["scrape"] = rr.Body.String()
	snap["scrape_status"] = rr.Code
	return snap
}
