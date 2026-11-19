package otelhelper

import (
	"context"
	"crypto/tls"
	"errors"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	otellog "go.opentelemetry.io/otel/log"
	logglobal "go.opentelemetry.io/otel/log/global"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// ── A31: OTLP export redirects are refused (design D1 "OTLP export redirects") ──

const redirectToken = "a31-redirect-token"

// redirectPair starts a target server counting every request and an OTLP
// endpoint answering status with a Location on the target that carries a token.
func redirectPair(t *testing.T, status int) (endpoint string, hits *atomic.Int64) {
	t.Helper()
	hits = &atomic.Int64{}
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		hits.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(target.Close)
	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		location := target.URL + "/redirected" + r.URL.Path + "?token=" + redirectToken
		w.Header().Set("Location", location)
		w.WriteHeader(status)
		// Redirectors commonly repeat the Location in the body.
		_, _ = w.Write([]byte("Redirecting to " + location))
	}))
	t.Cleanup(redirector.Close)
	return redirector.URL, hits
}

func redirectExport(endpoint, signal string) *signalExport {
	return &signalExport{
		configured:  true,
		protocol:    ProtocolHTTP,
		endpointURL: endpoint + "/v1/" + signal,
		compression: CompressionNone,
		timeout:     5 * time.Second,
		headers:     map[string]string{"x-a31-header": "a31-header-value"},
	}
}

// exportOnce sends one non-empty batch of signal through a fresh exporter.
func exportOnce(t *testing.T, signal string, e *signalExport) error {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	switch signal {
	case signalTraces:
		exp, err := newOTLPTraceExporter(ctx, e)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = exp.Shutdown(ctx) }()
		return exp.ExportSpans(ctx, tracetest.SpanStubs{{Name: "a31"}}.Snapshots())
	case signalMetrics:
		exp, err := newOTLPMetricExporter(ctx, e)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = exp.Shutdown(ctx) }()
		rm := &metricdata.ResourceMetrics{ScopeMetrics: []metricdata.ScopeMetrics{{Metrics: []metricdata.Metrics{{
			Name: "a31", Data: metricdata.Gauge[int64]{DataPoints: []metricdata.DataPoint[int64]{{Value: 1}}},
		}}}}}
		return exp.Export(ctx, rm)
	default:
		exp, err := newOTLPLogExporter(ctx, e)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = exp.Shutdown(ctx) }()
		var rec sdklog.Record
		rec.SetBody(attribute.StringValue("a31"))
		return exp.Export(ctx, []sdklog.Record{rec})
	}
}

func TestOTLPHTTPRedirectRefused(t *testing.T) {
	for _, status := range []int{http.StatusTemporaryRedirect, http.StatusPermanentRedirect} {
		for _, signal := range allSignals {
			t.Run(signal+"/"+strconv.Itoa(status), func(t *testing.T) {
				logs := captureDiag(t)
				endpoint, hits := redirectPair(t, status)
				// Two exports: the export fails each time, the ERROR is written once.
				for i := range 2 {
					err := exportOnce(t, signal, redirectExport(endpoint, signal))
					if err == nil {
						t.Fatalf("export %d: a %d answer must fail the export", i, status)
					}
					// The body repeats the Location: the reporter empties it.
					for _, secret := range []string{redirectToken, "/redirected", "Redirecting to"} {
						if strings.Contains(err.Error(), secret) {
							t.Errorf("export error carries %q: %v", secret, err)
						}
					}
				}
				if n := hits.Load(); n != 0 {
					t.Errorf("the redirect was followed: %d request(s) reached the Location target", n)
				}
				out := logs.String()
				line := regexp.MustCompile(`level=ERROR [^\n]*redirects are not followed[^\n]*signal=` + signal + `[^\n]*status=` + strconv.Itoa(status))
				if n := len(line.FindAllString(out, -1)); n != 1 {
					t.Errorf("want exactly one redirect ERROR for %s %d, got %d:\n%s", signal, status, n, out)
				}
				if n := strings.Count(out, "redirects are not followed"); n != 1 {
					t.Errorf("want one redirect line in total, got %d:\n%s", n, out)
				}
				for _, secret := range []string{redirectToken, "/redirected", "a31-header-value"} {
					if strings.Contains(out, secret) {
						t.Errorf("diagnostics carry %q:\n%s", secret, out)
					}
				}
			})
		}
	}
}

// One line per signal and per status: a second status for the same signal is
// a second line, the same status is not.
func TestOTLPHTTPRedirectOncePerSignalAndStatus(t *testing.T) {
	logs := captureDiag(t)
	for _, status := range []int{http.StatusTemporaryRedirect, http.StatusTemporaryRedirect, http.StatusFound} {
		endpoint, _ := redirectPair(t, status)
		_ = exportOnce(t, signalTraces, redirectExport(endpoint, signalTraces))
	}
	out := logs.String()
	if n := strings.Count(out, "redirects are not followed"); n != 2 {
		t.Errorf("want 2 lines (307 and 302), got %d:\n%s", n, out)
	}
	if !strings.Contains(out, "signal=traces status=302") || !strings.Contains(out, "signal=traces status=307") {
		t.Errorf("missing signal/status fields:\n%s", out)
	}
}

// A 2xx answer writes nothing and reaches the exporter as a success.
func TestOTLPHTTPNoRedirectNoLine(t *testing.T) {
	logs := captureDiag(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) }))
	defer srv.Close()
	if err := exportOnce(t, signalLogs, redirectExport(srv.URL, signalLogs)); err != nil {
		t.Fatalf("export: %v", err)
	}
	if strings.Contains(logs.String(), "redirect") {
		t.Errorf("unexpected line:\n%s", logs.String())
	}
}

// The client reproduces the SDK default client: proxy from the environment,
// the helper TLS configuration (a copy), the export timeout (10 s when unset).
func TestNewOTLPHTTPClientMatchesSDKDefaults(t *testing.T) {
	cfg := &tls.Config{MinVersion: tls.VersionTLS12, ServerName: "collector.example.com"}
	c := newOTLPHTTPClient(signalTraces, &signalExport{tlsConfig: cfg, timeout: 3 * time.Second})
	if c.Timeout != 3*time.Second {
		t.Errorf("timeout = %v", c.Timeout)
	}
	tr := c.Transport.(*redirectReporter).base.(*http.Transport)
	if tr.TLSClientConfig == nil || tr.TLSClientConfig == cfg || tr.TLSClientConfig.ServerName != "collector.example.com" {
		t.Errorf("TLS config not a copy of the helper's: %+v", tr.TLSClientConfig)
	}
	if tr.Proxy == nil || !tr.ForceAttemptHTTP2 || tr.MaxIdleConns != 100 || tr.TLSHandshakeTimeout != 10*time.Second ||
		tr.IdleConnTimeout != 90*time.Second || tr.ExpectContinueTimeout != 1*time.Second {
		t.Errorf("transport differs from the SDK default: %+v", tr)
	}
	if err := c.CheckRedirect(nil, nil); !errors.Is(err, http.ErrUseLastResponse) {
		t.Errorf("CheckRedirect = %v", err)
	}
	d := newOTLPHTTPClient(signalLogs, &signalExport{})
	if d.Timeout != defaultOTLPTimeout {
		t.Errorf("default timeout = %v", d.Timeout)
	}
	if d.Transport.(*redirectReporter).base.(*http.Transport).TLSClientConfig != nil {
		t.Error("no helper TLS config: the transport must keep the system defaults")
	}
}

// closeIdleCounter records CloseIdleConnections calls.
type closeIdleCounter struct {
	http.RoundTripper
	closed atomic.Int64
}

func (c *closeIdleCounter) CloseIdleConnections() { c.closed.Add(1) }

// The reporter forwards CloseIdleConnections, so a caller of
// http.Client.CloseIdleConnections still reaches the transport.
func TestRedirectReporterForwardsCloseIdleConnections(t *testing.T) {
	base := &closeIdleCounter{RoundTripper: http.DefaultTransport}
	c := &http.Client{Transport: &redirectReporter{base: base, signal: signalTraces}}
	c.CloseIdleConnections()
	if n := base.closed.Load(); n != 1 {
		t.Fatalf("CloseIdleConnections reached the base %d time(s), want 1", n)
	}
	// A base without the method is not an error.
	(&http.Client{Transport: &redirectReporter{base: roundTripFunc(nil), signal: signalLogs}}).CloseIdleConnections()
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// Through Setup: every signal answered with a 307 whose Location and body
// carry a token. Each signal gets its ERROR; no diagnostic line, handled SDK
// error or Shutdown error carries the Location, the body or a header value.
func TestOTLPHTTPRedirectSetupLeaksNothing(t *testing.T) {
	resetGlobals() // an earlier test may leave a pipeline installed (Setup is then a no-op)
	clearOTelEnvA29(t)
	logs := captureDiag(t)
	endpoint, hits := redirectPair(t, http.StatusTemporaryRedirect)
	t.Setenv(otlpEnv("", "ENDPOINT"), endpoint)
	t.Setenv(EnvOtlpProtocol, "http/protobuf")
	t.Setenv(otlpEnv("", "HEADERS"), "x-a31-header=a31-header-value")
	app := &recordingErrHandler{}
	withErrorHandler(t, app)

	shutdown, err := Setup(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	_, span := GetTracer("a31").Start(context.Background(), "a31")
	span.End()
	counter, err := GetMeter("a31").Int64Counter("a31")
	if err != nil {
		t.Fatal(err)
	}
	counter.Add(context.Background(), 1)
	var rec otellog.Record
	rec.SetBody(attribute.StringValue("a31"))
	logglobal.GetLoggerProvider().Logger("a31").Emit(context.Background(), rec)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	shutErr := shutdown(ctx)

	if n := hits.Load(); n != 0 {
		t.Errorf("the redirect was followed: %d request(s) reached the Location target", n)
	}
	out := logs.String()
	for _, signal := range allSignals {
		if !strings.Contains(out, "signal="+signal+" status=307") {
			t.Errorf("no redirect ERROR for %s:\n%s", signal, out)
		}
	}
	texts := []string{out}
	for _, e := range app.got() {
		texts = append(texts, e.Error())
	}
	if shutErr != nil {
		texts = append(texts, shutErr.Error())
	}
	all := strings.Join(texts, "\n")
	for _, secret := range []string{redirectToken, "/redirected", "Redirecting to", "a31-header-value"} {
		if strings.Contains(all, secret) {
			t.Errorf("output carries %q:\n%s", secret, all)
		}
	}
}
