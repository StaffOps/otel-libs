package otelhelper

// Contract task A29 (design D1 rows of 2026-11-05 "SDK error text: response
// bodies and header values", "Path on a gRPC endpoint", "Prometheus exporter
// variables"); the parity fixture covers the wire side
// (export.http_error_body_header_values_not_logged,
// export.grpc_error_details_header_values_masked, export.grpc_*path*,
// metrics.port_prometheus_*, metrics.listener_prometheus_host_loopback).

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/wrapperspb"
)

// clearOTelEnvA29 unsets every OTEL_* variable for one test.
func clearOTelEnvA29(t *testing.T) {
	t.Helper()
	for _, kv := range os.Environ() {
		if k, _, _ := strings.Cut(kv, "="); strings.HasPrefix(k, "OTEL_") || k == "SERVICE_NAME" || k == "ENVIRONMENT" {
			t.Setenv(k, "")
			_ = os.Unsetenv(k)
		}
	}
	t.Setenv("OTEL_SERVICE_NAME", "a29")
	// Values an earlier Setup captured would be masked too: start empty.
	resetCapturedHeaders()
	t.Cleanup(resetCapturedHeaders)
}

func resetCapturedHeaders() {
	c := &headerValuesCache
	c.mu.Lock()
	defer c.mu.Unlock()
	c.captured, c.stale = nil, true
}

// exporterBodyError builds the error chain the OTLP/HTTP exporters return
// for a non-retryable status (otlptracehttp v1.47.0 client.go:258-269).
func exporterBodyError(body string) (wrapped, bodyErr error) {
	bodyErr = fmt.Errorf("body: %s", body)
	err := fmt.Errorf("failed to send to %s: %s (%w)", "http://collector:4318/v1/traces", "400 Bad Request", bodyErr)
	return fmt.Errorf("traces export: %w", err), bodyErr
}

// retryLikeError mimics the exporters' retryableError (Unwrap() error).
type retryLikeError struct{ err error }

func (e retryLikeError) Error() string { return "retry-able request failure: " + e.err.Error() }
func (e retryLikeError) Unwrap() error { return e.err }

func TestA29SanitizeDropsResponseBody(t *testing.T) {
	clearOTelEnvA29(t)
	body := `{"request_headers":{"x-api-key":"s3cr3t-value"}}`
	err, bodyErr := exporterBodyError(body)
	got := sanitizeSDKError(err)
	want := "traces export: failed to send to http://collector:4318/v1/traces: 400 Bad Request (body: <response body omitted: " +
		fmt.Sprint(len(body)) + " bytes>)"
	if got.Error() != want {
		t.Fatalf("sanitized = %q\nwant        %q", got.Error(), want)
	}
	if !errors.Is(got, bodyErr) {
		t.Fatal("errors.Is must still find the original body error")
	}
	assertChainClean(t, got, "s3cr3t", "request_headers")

	// Retryable form and errors.Join (Unwrap() []error).
	retry := retryLikeError{fmt.Errorf("body: %s", "echo s3cr3t-value")}
	joined := errors.Join(errors.New("metrics: ok part"), retry)
	if s := sanitizeSDKError(joined).Error(); strings.Contains(s, "s3cr3t") || !strings.Contains(s, "retry-able request failure: body: <response body omitted: 17 bytes>") || !strings.Contains(s, "ok part") {
		t.Fatalf("joined = %q", s)
	}

	// A wrapper that flattened the chain (%v): everything from "body: " is cut.
	flat := fmt.Errorf("export failed: %v", err)
	if s := sanitizeSDKError(flat).Error(); strings.Contains(s, "s3cr3t") || !strings.HasSuffix(s, "bytes>") || !strings.HasPrefix(s, "export failed: traces export:") {
		t.Fatalf("flattened = %q", s)
	}
}

func TestA29SanitizeKeepsUnrelatedErrors(t *testing.T) {
	clearOTelEnvA29(t)
	if sanitizeSDKError(nil) != nil {
		t.Fatal("nil must stay nil")
	}
	plain := errors.New("rpc error: code = Unavailable desc = connection refused")
	if got := sanitizeSDKError(plain); got != plain {
		t.Fatalf("unchanged error must be returned as is, got %v", got)
	}
	// "body: " with nothing after it is not a response body (the exporters write "(empty)").
	empty := errors.New("body: ")
	if got := sanitizeSDKError(empty); got.Error() != "body: <response body omitted: 0 bytes>" {
		t.Fatalf("empty body = %q", got)
	}
	// The SDK's "(empty)" body is 0 bytes, not 7 (obs L9).
	if got := sanitizeSDKError(fmt.Errorf("x (%w)", errors.New("body: (empty)"))); got.Error() != "x (body: <response body omitted: 0 bytes>)" {
		t.Fatalf("(empty) body = %q", got)
	}
}

func TestA29MaskHeaderValues(t *testing.T) {
	clearOTelEnvA29(t)
	t.Setenv(otlpEnv("", "HEADERS"), `api-key=s3cr3t%20value, x-short=1,flag=on,q=a"b\c-1`)
	t.Setenv(otlpEnv(signalLogs, "HEADERS"), "x-logs-token=logs-t0ken")
	values := configuredHeaderValues()
	for _, in := range []string{
		"raw s3cr3t%20value here",
		"decoded s3cr3t value here",
		`json {"q":"a\"b\\c-1"}`,
		`twice {\"q\":\"a\\\"b\\\\c-1\"}`,
		"per-signal logs-t0ken",
		`quoted "a\"b\\c-1"`,
	} {
		out := maskHeaderValues(in, values)
		for _, secret := range []string{"s3cr3t", "logs-t0ken", `a"b`, `a\"b`, `a\\\"b`} {
			if strings.Contains(out, secret) {
				t.Errorf("mask(%q) = %q still contains %q", in, out, secret)
			}
		}
		if !strings.Contains(out, redactedValue) {
			t.Errorf("mask(%q) = %q: nothing masked", in, out)
		}
	}
	// Values under 4 characters are never masked.
	if out := maskHeaderValues("code 1 on", values); out != "code 1 on" {
		t.Errorf("short values masked: %q", out)
	}
	// Memoized on the raw values, re-read when they change.
	t.Setenv(otlpEnv("", "HEADERS"), "k=other-secret")
	if out := maskHeaderValues("other-secret s3cr3t value", configuredHeaderValues()); out != redactedValue+" s3cr3t value" {
		t.Errorf("after env change: %q", out)
	}
	t.Setenv(otlpEnv("", "HEADERS"), "")
	t.Setenv(otlpEnv(signalLogs, "HEADERS"), "")
	if v := configuredHeaderValues(); len(v) != 0 {
		t.Errorf("no headers configured, got %d values", len(v))
	}
}

func TestA29SanitizeMasksGRPCStatusText(t *testing.T) {
	clearOTelEnvA29(t)
	t.Setenv(otlpEnv("", "HEADERS"), "x-api-key=s3cr3t-value")
	err := errors.New(`traces export: rpc error: code = InvalidArgument desc = echo: request headers {"x-api-key":"s3cr3t-value"}`)
	got := sanitizeSDKError(err).Error()
	if strings.Contains(got, "s3cr3t") || !strings.Contains(got, `"x-api-key":"<redacted>"`) || !strings.Contains(got, "InvalidArgument") {
		t.Fatalf("got %q", got)
	}
}

// recordingErrHandler is goroutine-safe: SDK export goroutines call it.
type recordingErrHandler struct {
	mu   sync.Mutex
	errs []error
}

func (h *recordingErrHandler) Handle(err error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.errs = append(h.errs, err)
}

func (h *recordingErrHandler) got() []error {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]error(nil), h.errs...)
}

func TestA29HandlerWrapsAnApplicationHandler(t *testing.T) {
	clearOTelEnvA29(t)
	app := &recordingErrHandler{}
	withErrorHandler(t, app)
	restore := installSDKErrorHandler()
	if _, ok := otel.GetErrorHandler().(*sdkErrorHandler); !ok {
		t.Fatal("helper handler not installed")
	}
	err, _ := exporterBodyError("s3cr3t-value")
	otel.Handle(err)
	otel.Handle(nil) // ignored
	if got := app.got(); len(got) != 1 || strings.Contains(got[0].Error(), "s3cr3t") || !errors.Is(got[0], err) {
		t.Fatalf("application handler got %v", got)
	}
	assertChainClean(t, app.got()[0], "s3cr3t")
	restore()
	restore() // idempotent
	if otel.GetErrorHandler() != otel.ErrorHandler(app) {
		t.Fatal("the application handler was not put back")
	}
}

func TestA29RestoreKeepsAHandlerSetAfterSetup(t *testing.T) {
	clearOTelEnvA29(t)
	app, late := &recordingErrHandler{}, &recordingErrHandler{}
	withErrorHandler(t, app)
	restore := installSDKErrorHandler()
	otel.SetErrorHandler(late) // last writer wins: raw errors from now on
	restore()
	if otel.GetErrorHandler() != otel.ErrorHandler(late) {
		t.Fatal("a handler installed after Setup was overwritten")
	}
}

func TestA29HandlerWithoutApplicationHandlerWritesDiagLogger(t *testing.T) {
	clearOTelEnvA29(t)
	buf := captureDiag(t)
	withErrorHandler(t, otelDefaultHandlerForTest()) // the SDK default delegator: no app handler
	restore := installSDKErrorHandler()
	defer restore()
	h, ok := otel.GetErrorHandler().(*sdkErrorHandler)
	if !ok || h.next != nil {
		t.Fatalf("with the SDK default the helper handler must not forward (next=%v)", h)
	}
	err, _ := exporterBodyError("s3cr3t-value")
	h.Handle(err)
	out := buf.String()
	if !strings.Contains(out, "level=ERROR") || !strings.Contains(out, "otelhelper: OpenTelemetry SDK error") ||
		!strings.Contains(out, "response body omitted") || strings.Contains(out, "s3cr3t") {
		t.Fatalf("diag output = %q", out)
	}
}

// TestA29SetupSanitizesExportErrors runs an OTLP/HTTP export against a server
// echoing the request headers in a 400 body: neither the handler output nor
// the Shutdown error carries them, and Shutdown puts the app handler back.
func TestA29SetupSanitizesExportErrors(t *testing.T) {
	clearOTelEnvA29(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.WriteHeader(http.StatusBadRequest)
		_, _ = fmt.Fprintf(w, `{"echo":{"x-api-key":%q}}`, r.Header.Get("X-Api-Key"))
	}))
	defer srv.Close()
	t.Setenv(otlpEnv("", "ENDPOINT"), srv.URL)
	t.Setenv(EnvOtlpProtocol, "http/protobuf")
	t.Setenv(otlpEnv("", "HEADERS"), "x-api-key=s3cr3t-value")
	t.Setenv(EnvMetricsExporter, "none")
	t.Setenv(EnvLogsExporter, "none")
	app := &recordingErrHandler{}
	withErrorHandler(t, app)

	shutdown, err := Setup(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := otel.GetErrorHandler().(*sdkErrorHandler); !ok {
		t.Fatal("Setup did not install the helper handler")
	}
	_, span := GetTracer("a29").Start(context.Background(), "s")
	span.End()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	shutErr := shutdown(ctx)
	var texts []string
	for _, e := range app.got() {
		texts = append(texts, e.Error())
	}
	if shutErr != nil {
		texts = append(texts, shutErr.Error())
	}
	all := strings.Join(texts, "\n")
	if !strings.Contains(all, "400") {
		t.Fatalf("the export failure must stay visible: %q", all)
	}
	if strings.Contains(all, "s3cr3t") {
		t.Fatalf("header value leaked: %q", all)
	}
	if otel.GetErrorHandler() != otel.ErrorHandler(app) {
		t.Fatal("Shutdown did not put the application handler back")
	}
}

func TestA29GRPCEndpointPathWarns(t *testing.T) {
	for _, tc := range []struct {
		name, raw, protocol string
		fromCode, warn      bool
	}{
		{"grpc path", "http://collector:4317/v1/traces", ProtocolGRPC, false, true},
		{"grpc schemeless path", "collector:4317/prefix", ProtocolGRPC, false, true},
		{"grpc code path", "http://collector:4317/p", ProtocolGRPC, true, true},
		{"grpc root", "http://collector:4317/", ProtocolGRPC, false, false},
		{"grpc none", "http://collector:4317", ProtocolGRPC, false, false},
		{"http path", "http://collector:4318/prefix", ProtocolHTTP, false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			res, err := resolveEndpoint(endpointInput{raw: tc.raw, variable: "VAR", signal: signalTraces, protocol: tc.protocol, fromCode: tc.fromCode})
			if err != nil {
				t.Fatal(err)
			}
			found := false
			for _, w := range res.warnings {
				if w.msg == msgGRPCPathIgnored {
					found = true
					if w.variable != "VAR" || w.value != "" || w.using != "" {
						t.Errorf("warning must name the variable only: %+v", w)
					}
				}
			}
			if found != tc.warn {
				t.Errorf("warned = %v, want %v (%+v)", found, tc.warn, res.warnings)
			}
			if tc.protocol == ProtocolGRPC && strings.Contains(res.explicitURL, "/p") {
				t.Errorf("path kept in the gRPC target: %q", res.explicitURL)
			}
		})
	}
}

func TestA29GRPCEndpointPathWarnsOncePerVariable(t *testing.T) {
	clearOTelEnvA29(t)
	buf := captureDiag(t)
	t.Setenv(otlpEnv("", "ENDPOINT"), "http://collector:4317/secret-path")
	t.Setenv(otlpEnv(signalLogs, "ENDPOINT"), "http://collector:4317/v1/logs")
	o := newOptions()
	for _, s := range allSignals {
		o.export(s)
	}
	out := buf.String()
	if n := strings.Count(out, msgGRPCPathIgnored); n != 2 {
		t.Fatalf("want 2 warnings (generic once, logs once), got %d: %q", n, out)
	}
	if strings.Contains(out, "secret-path") || strings.Contains(out, "/v1/logs") {
		t.Fatalf("path printed: %q", out)
	}
	for _, v := range []string{otlpEnv("", "ENDPOINT"), otlpEnv(signalLogs, "ENDPOINT")} {
		if !strings.Contains(out, "variable="+v) {
			t.Errorf("variable %s not named: %q", v, out)
		}
	}
}

func TestA29PrometheusPortPrecedence(t *testing.T) {
	for _, tc := range []struct {
		name, prom, helper string
		code               *int
		want               int
		warn               string
	}{
		{"default", "", "", nil, 9464, ""},
		{"helper only", "", "9100", nil, 9100, ""},
		{"standard beats helper", "9300", "9100", nil, 9300, ""},
		{"standard zero", "0", "9100", nil, 0, ""},
		{"standard grammar", "+0009300", "", nil, 9300, ""},
		{"standard invalid falls to helper", "93_00", "9100", nil, 9100, EnvPrometheusPort},
		{"standard invalid falls to default", "70000", "", nil, 9464, EnvPrometheusPort},
		{"both invalid", "x", "y", nil, 9464, EnvMetricsPort},
		{"blank standard is unset", "  ", "9100", nil, 9100, ""},
		{"code wins", "9300", "9100", intPtr(9200), 9200, ""},
		{"code zero wins", "9300", "", intPtr(0), 0, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			clearOTelEnvA29(t)
			buf := captureDiag(t)
			t.Setenv(EnvPrometheusPort, tc.prom)
			t.Setenv(EnvMetricsPort, tc.helper)
			var opts []Option
			if tc.code != nil {
				opts = append(opts, WithPrometheusMetricsPort(*tc.code))
			}
			o := newOptions(opts...)
			if o.PrometheusMetricsPort != tc.want {
				t.Fatalf("port = %d, want %d", o.PrometheusMetricsPort, tc.want)
			}
			out := buf.String()
			if tc.warn != "" && !strings.Contains(out, "variable="+tc.warn) {
				t.Errorf("no warning naming %s: %q", tc.warn, out)
			}
			if tc.warn == "" && strings.Contains(out, "invalid port") {
				t.Errorf("unexpected warning: %q", out)
			}
		})
	}
}

func intPtr(v int) *int { return &v }

func TestA29ParsePrometheusHost(t *testing.T) {
	for _, tc := range []struct {
		in, want string
		ok       bool
	}{
		{"127.0.0.1", "127.0.0.1", true},
		{"0.0.0.0", "0.0.0.0", true},
		{"::1", "::1", true},
		{"[::1]", "::1", true},
		{"FE80::AB", "fe80::ab", true},
		{"::", "", true},
		{"[::]", "", true},
		{"localhost", "127.0.0.1", true},
		{"LocalHost", "127.0.0.1", true},
		{"::ffff:10.0.0.1", "10.0.0.1", true},
		{"::ffff:0.0.0.0", "0.0.0.0", true},
		{"[::ffff:7f00:1]", "127.0.0.1", true},
		{"*", "", false},
		{"collector.example.com", "", false},
		{"127.0.0.1:9464", "", false},
		{"http://127.0.0.1", "", false},
		{"[127.0.0.1]", "", false},
		{"fe80::1%eth0", "", false},
		{"[fe80::1%25eth0]", "", false},
		{"010.0.0.1", "", false},
		{"1.2.3", "", false},
	} {
		got, ok := parsePrometheusHost(tc.in)
		if got != tc.want || ok != tc.ok {
			t.Errorf("parsePrometheusHost(%q) = %q, %v; want %q, %v", tc.in, got, ok, tc.want, tc.ok)
		}
	}
}

func TestA29PrometheusHostResolution(t *testing.T) {
	clearOTelEnvA29(t)
	buf := captureDiag(t)
	t.Setenv(EnvPrometheusHost, " [::1] ")
	if o := newOptions(); o.prometheusHost != "::1" {
		t.Fatalf("host = %q", o.prometheusHost)
	}
	t.Setenv(EnvPrometheusHost, "*")
	if o := newOptions(); o.prometheusHost != "" {
		t.Fatalf("invalid host must fall back to every interface, got %q", o.prometheusHost)
	}
	if out := buf.String(); !strings.Contains(out, "variable="+EnvPrometheusHost) || !strings.Contains(out, "every interface") {
		t.Fatalf("no warning: %q", out)
	}
	t.Setenv(EnvPrometheusHost, "https://user:tok-s3cr3t@metrics.example.com?k=q-s3cr3t")
	_ = newOptions()
	if out := buf.String(); strings.Contains(out, "s3cr3t") || !strings.Contains(out, "https://metrics.example.com") {
		t.Fatalf("echoed value not redacted: %q", out)
	}
}

func TestA29ListenAddress(t *testing.T) {
	for _, tc := range []struct{ host, network, addr string }{
		{"", "tcp", ":9464"},
		{"127.0.0.1", "tcp4", "127.0.0.1:9464"},
		{"0.0.0.0", "tcp4", "0.0.0.0:9464"},
		{"::1", "tcp", "[::1]:9464"},
	} {
		if n, a := listenAddress(tc.host, 9464); n != tc.network || a != tc.addr {
			t.Errorf("listenAddress(%q) = %s %s; want %s %s", tc.host, n, a, tc.network, tc.addr)
		}
	}
}

func TestA29ListenerBindsTheConfiguredHost(t *testing.T) {
	clearOTelEnvA29(t)
	port := freePort(t)
	t.Setenv(EnvPrometheusPort, fmt.Sprint(port))
	t.Setenv(EnvMetricsPort, "1") // the standard variable wins
	t.Setenv(EnvPrometheusHost, "127.0.0.1")
	shutdown, err := Setup(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = shutdown(context.Background()) }()
	resp, err := (&http.Client{Timeout: 5 * time.Second}).Get(fmt.Sprintf("http://127.0.0.1:%d/metrics", port))
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
	// Bound on loopback only: another local address of the host is refused.
	if addr := nonLoopbackIPv4(t); addr != "" {
		if c, err := net.DialTimeout("tcp", net.JoinHostPort(addr, fmt.Sprint(port)), time.Second); err == nil {
			_ = c.Close()
			t.Fatalf("listener reachable on %s", addr)
		}
	}
}

func nonLoopbackIPv4(t *testing.T) string {
	t.Helper()
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return ""
	}
	for _, a := range addrs {
		if ipn, ok := a.(*net.IPNet); ok && ipn.IP.To4() != nil && !ipn.IP.IsLoopback() {
			return ipn.IP.String()
		}
	}
	return ""
}

func TestA29ListenerOnAnAddressNotOwnedFailsSetup(t *testing.T) {
	clearOTelEnvA29(t)
	t.Setenv(EnvPrometheusHost, "192.0.2.1") // TEST-NET-1: never a local address
	t.Setenv(EnvPrometheusPort, fmt.Sprint(freePort(t)))
	shutdown, err := Setup(context.Background())
	if err == nil {
		_ = shutdown(context.Background())
		t.Fatal("Setup must fail when the listener cannot bind")
	}
	if !strings.Contains(err.Error(), "prometheus /metrics listener on 192.0.2.1:") {
		t.Fatalf("err = %v", err)
	}
}

// assertChainClean walks every level of err's chain (both Unwrap forms) and
// fails when one renders a secret.
func assertChainClean(t *testing.T, err error, secrets ...string) {
	t.Helper()
	var walk func(e error, depth int)
	walk = func(e error, depth int) {
		if e == nil || depth > 40 {
			return
		}
		for _, s := range secrets {
			if strings.Contains(e.Error(), s) {
				t.Fatalf("level %d of the chain renders %q: %q", depth, s, e.Error())
			}
		}
		switch u := e.(type) { //nolint:errorlint // walking by hand
		case interface{ Unwrap() []error }:
			for _, k := range u.Unwrap() {
				walk(k, depth+1)
			}
		case interface{ Unwrap() error }:
			walk(u.Unwrap(), depth+1)
		}
	}
	walk(err, 0)
}

// Fix round (code-review M2, dev M2): the chain a handler can walk is
// sanitized at every level; errors.Is and status.FromError keep working.
func TestA29SanitizedChainHidesBodiesAndValues(t *testing.T) {
	clearOTelEnvA29(t)
	t.Setenv(otlpEnv("", "HEADERS"), "authorization=Bearer%20tok-s3cr3t-value")
	body := fmt.Errorf("body: %s", `{"echo":"Bearer tok-s3cr3t-value"}`)
	retry := retryLikeError{body}
	joined := errors.Join(fmt.Errorf("metrics: %w", retry), errors.New("logs: invalid token tok-s3cr3t-value"))
	got := sanitizeSDKError(joined)
	assertChainClean(t, got, "s3cr3t")
	if !errors.Is(got, body) {
		t.Fatal("errors.Is must reach the original body error")
	}
	if sanitizeSDKError(got) != got {
		t.Fatal("sanitizing twice must return the same value")
	}

	st := status.New(codes.InvalidArgument, "invalid token tok-s3cr3t-value")
	grpcErr := fmt.Errorf("traces export: %w", st.Err())
	san := sanitizeSDKError(grpcErr)
	assertChainClean(t, san, "s3cr3t")
	fs, ok := status.FromError(san)
	if !ok || fs.Code() != codes.InvalidArgument || strings.Contains(fs.Message(), "s3cr3t") ||
		!strings.Contains(fs.Message(), "invalid token <redacted>") || len(fs.Details()) != 0 {
		t.Fatalf("status.FromError = %v, %v (%q)", fs.Code(), ok, fs.Message())
	}
}

// Fix round (dev H1, code-review H1/M3, obs M3/L5): token parts, form and
// HTML-escaped JSON forms, overlapping values masked whole.
func TestA29MaskFormsAndParts(t *testing.T) {
	values := []string{"Bearer tok-s3cr3t-1234", "team", "team-secret-token", "a&b<c>-amp-value"}
	for _, in := range []string{
		"invalid token tok-s3cr3t-1234",
		"form Bearer+tok-s3cr3t-1234",
		"tenant team-secret-token",
		`html "a\u0026b\u003cc\u003e-amp-value"`,
		"raw a&b<c>-amp-value",
	} {
		out := maskHeaderValues(in, values)
		for _, secret := range []string{"s3cr3t", "secret-token", "amp-value"} {
			if strings.Contains(out, secret) {
				t.Errorf("mask(%q) = %q leaks %q", in, out, secret)
			}
		}
	}
	if out := maskHeaderValues("tenant team-secret-token", values); out != "tenant <redacted>" {
		t.Errorf("overlapping values: %q", out)
	}
	// "<redacted>" is never re-scanned: a value inside it does not cut it open.
	if out := maskHeaderValues("x dact y", []string{"dact"}); out != "x <redacted> y" {
		t.Errorf("re-scan: %q", out)
	}
	// Parts under 4 characters are not masked.
	if out := maskHeaderValues("abc", []string{"abc def1"}); out != "abc" {
		t.Errorf("short part masked: %q", out)
	}
	// A leading auth-scheme name is not a part of its own (A29 final round).
	if out := maskHeaderValues("scheme Bearer, token tok-s3cr3t-1234", values); out != "scheme Bearer, token <redacted>" {
		t.Errorf("auth scheme: %q", out)
	}
	// Overlapping values never leave a tail (dev R2-L1).
	if out := maskHeaderValues("x OVERLAP-AAAA-BBBB-CCCC-PLACEHOLDER2 y", []string{"OVERLAP-AAAA-BBBB", "BBBB-CCCC-PLACEHOLDER2"}); out != "x <redacted> y" {
		t.Errorf("overlap: %q", out)
	}
}

func TestA29FlattenedSecondBodyDropped(t *testing.T) {
	clearOTelEnvA29(t)
	leaf := fmt.Errorf("a (%w)", errors.New("body: first-body"))
	flat := fmt.Errorf("b: %v", fmt.Errorf("x (%w)", errors.New("body: second-body")))
	got := sanitizeSDKError(errors.Join(leaf, flat)).Error()
	if strings.Contains(got, "first-body") || strings.Contains(got, "second-body") {
		t.Fatalf("got %q", got)
	}
}

func TestA29FailedSetupLeavesTheHandler(t *testing.T) {
	clearOTelEnvA29(t)
	app := &recordingErrHandler{}
	withErrorHandler(t, app)
	t.Setenv(EnvPrometheusHost, "192.0.2.1")
	t.Setenv(EnvPrometheusPort, fmt.Sprint(freePort(t)))
	if shutdown, err := Setup(context.Background()); err == nil {
		_ = shutdown(context.Background())
		t.Fatal("Setup must fail")
	}
	if otel.GetErrorHandler() != otel.ErrorHandler(app) {
		t.Fatalf("a failed Setup replaced the handler: %T", otel.GetErrorHandler())
	}
}

// A value the resolution skipped (an invalid per-signal endpoint with a
// path) never produces the path warning (code-review M4b).
func TestA29SkippedEndpointPathNotWarned(t *testing.T) {
	clearOTelEnvA29(t)
	buf := captureDiag(t)
	t.Setenv(otlpEnv("", "ENDPOINT"), "http://collector:4317")
	t.Setenv(otlpEnv(signalTraces, "ENDPOINT"), "http://coll\tector:4317/skipped-path")
	o := newOptions()
	o.export(signalTraces)
	if out := buf.String(); strings.Contains(out, msgGRPCPathIgnored) || strings.Contains(out, "skipped-path") {
		t.Fatalf("output = %q", out)
	}
	if !strings.Contains(o.export(signalTraces).endpointURL, "collector:4317") {
		t.Fatalf("no fallback: %q", o.export(signalTraces).endpointURL)
	}
}

func TestA29RedactEchoedValue(t *testing.T) {
	for in, want := range map[string]string{
		"plain":                 "plain",
		"https://u:p@h:1/x?q#f": "https://h:1/x",
		"u:p@h":                 "h",
		"h#frag":                "h",
		"a@b@c":                 "c",
		"http://h/p@x":          "http://x",
	} {
		if got := redactEchoedValue(in); got != want {
			t.Errorf("redactEchoedValue(%q) = %q, want %q", in, got, want)
		}
	}
}

// The values the exporters were built with stay masked after the
// application deletes the variable (A29 final round, dev R2-M1).
func TestA29CapturedHeaderValuesStayMasked(t *testing.T) {
	clearOTelEnvA29(t)
	t.Setenv(otlpEnv("", "HEADERS"), "authorization=Bearer%20tok-captured-s3cr3t")
	t.Setenv(EnvMetricsExporter, "none")
	shutdown, err := Setup(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = shutdown(context.Background()) }()
	_ = os.Unsetenv(otlpEnv("", "HEADERS"))
	got := sanitizeSDKError(errors.New("rpc error: code = Unauthenticated desc = invalid token tok-captured-s3cr3t"))
	if strings.Contains(got.Error(), "s3cr3t") {
		t.Fatalf("captured value printed after the env was cleared: %q", got)
	}
}

// A chain that references itself, or shares levels, is walked once per error.
type selfJoin struct{ errs []error }

func (e *selfJoin) Error() string   { return "self" }
func (e *selfJoin) Unwrap() []error { return e.errs }

func TestA29SelfReferencingChainEnds(t *testing.T) {
	clearOTelEnvA29(t)
	j := &selfJoin{}
	j.errs = []error{j, errors.New("body: s3cr3t"), j}
	done := make(chan error, 1)
	go func() { done <- sanitizeSDKError(j) }()
	select {
	case got := <-done:
		if _, ok := got.(*sanitizedJoin); !ok {
			t.Fatalf("got %T", got)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("sanitizing a self-referencing chain did not end")
	}
}

// gRPC status details are server data: always wrapped, even when no text changed.
func TestA29StatusDetailsNeverPassThrough(t *testing.T) {
	clearOTelEnvA29(t)
	st, err := status.New(codes.InvalidArgument, "plain").WithDetails(wrapperspb.String("echo-detail"))
	if err != nil {
		t.Fatal(err)
	}
	got := sanitizeSDKError(st.Err())
	fs, ok := status.FromError(got)
	if !ok || fs.Code() != codes.InvalidArgument || len(fs.Details()) != 0 {
		t.Fatalf("details reached the handler: %v %v %v", ok, fs.Code(), fs.Details())
	}
	var ue *url.Error
	if errors.As(sanitizeSDKError(fmt.Errorf("x: %w", &url.Error{Op: "Post", URL: "u", Err: errors.New("body: s3cr3t")})), &ue) {
		t.Fatal("errors.As must not reach the original typed errors")
	}
}
