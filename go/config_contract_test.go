package otelhelper

// Unit tests for the 0.3.0 config contract (specs/config-contract-v0.3: R1,
// R2, R4, R5, R8, G7, G8, G10, D1, D4). The pure resolution functions are
// tested here; what reaches the wire is asserted by parity/cases.json
// `export` cases. Each "regression" test names the fixture case that was
// xfail:A3 (failing) before the fix.

import (
	"bytes"
	"context"
	"encoding/pem"
	"errors"
	"fmt"
	"log"
	"log/slog"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	logglobal "go.opentelemetry.io/otel/log/global"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/emptypb"
)

// captureDiag redirects the helper diagnostics channel for one test.
func captureDiag(t *testing.T) *syncBuffer {
	t.Helper()
	resetDeprecationsForTest(t) // once-per-process warnings must fire in this test
	buf := &syncBuffer{}
	prev := diagLogger
	// No timestamp: tests assert that a value never appears in the output, and
	// a time's digits could contain a short value by chance.
	diagLogger = slog.New(slog.NewTextHandler(buf, &slog.HandlerOptions{
		Level: slog.LevelDebug,
		ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
			if len(groups) == 0 && a.Key == slog.TimeKey {
				return slog.Attr{}
			}
			return a
		},
	}))
	t.Cleanup(func() { diagLogger = prev })
	return buf
}

// ── resolveEndpoint (D1.b–c shims, ADR-001 (a)) ─────────────────────────────

func TestResolveEndpoint(t *testing.T) {
	tests := []struct {
		name     string
		in       endpointInput
		wantURL  string // resolved URL passed to the SDK
		wantWarn string // substring of a warning message; "" = no warning
		wantErr  string
	}{
		// Schemeless host:port (R1): https unless INSECURE=true; default port per protocol.
		{"schemeless grpc → https", endpointInput{raw: "10.0.0.5:14317", signal: "traces", protocol: ProtocolGRPC}, "https://10.0.0.5:14317", "", ""},
		{"schemeless grpc insecure → http", endpointInput{raw: "collector:4317", signal: "traces", protocol: ProtocolGRPC, insecure: true, insecureSet: true}, "http://collector:4317", "", ""},
		{"schemeless grpc no port → 4317", endpointInput{raw: "collector", signal: "metrics", protocol: ProtocolGRPC}, "https://collector:4317", "", ""},
		{"schemeless http no port → 4318 + path + deprecation", endpointInput{raw: "collector", signal: "logs", protocol: ProtocolHTTP, insecure: true, insecureSet: true}, "http://collector:4318/v1/logs", "deprecated", ""},
		{"schemeless http per-signal path verbatim", endpointInput{raw: "collector:4318/custom", signal: "logs", protocol: ProtocolHTTP, perSignal: true}, "https://collector:4318/custom", "deprecated", ""},
		{"schemeless IPv6", endpointInput{raw: "[::1]:24318", signal: "traces", protocol: ProtocolGRPC}, "https://[::1]:24318", "", ""},
		{"schemeless IPv6 no port", endpointInput{raw: "[2001:db8::1]", signal: "traces", protocol: ProtocolGRPC}, "https://[2001:db8::1]:4317", "", ""},
		// Absolute env URLs are the SDK's (ADR-001).
		// Absolute env URLs are passed explicitly too (Go endpoint ownership).
		{"env https grpc with port", endpointInput{raw: "https://collector:4317", signal: "traces", protocol: ProtocolGRPC}, "https://collector:4317", "", ""},
		{"env http/protobuf no port → scheme default (443/80)", endpointInput{raw: "https://otlp.vendor.io", signal: "traces", protocol: ProtocolHTTP}, "https://otlp.vendor.io/v1/traces", "", ""},
		{"env IPv6 URL unchanged", endpointInput{raw: "http://[::1]:24318", signal: "traces", protocol: ProtocolHTTP}, "http://[::1]:24318/v1/traces", "", ""},
		{"env per-signal no path → root", endpointInput{raw: "http://c:4318", signal: "traces", protocol: ProtocolHTTP, perSignal: true}, "http://c:4318", "", ""},
		{"opaque scheme unix:/path → unsupported", endpointInput{raw: "unix:/tmp/otlp.sock", signal: "traces", protocol: ProtocolGRPC}, "", "", "unsupported endpoint scheme"},
		{"env unix scheme → not configured", endpointInput{raw: "unix:///tmp/otlp.sock", signal: "traces", protocol: ProtocolGRPC}, "", "", "unsupported endpoint scheme"},
		{"env URL with userinfo/query → helper owns it", endpointInput{raw: "https://u:pw@c:4318/otlp?token=q", signal: "traces", protocol: ProtocolHTTP}, "https://c:4318/otlp/v1/traces", "not supported", ""},
		// D1.b: portless gRPC URL → 4317 + "explicit port".
		{"portless grpc https", endpointInput{raw: "https://collector", signal: "traces", protocol: ProtocolGRPC}, "https://collector:4317", "explicit port", ""},
		{"portless grpc http IPv6", endpointInput{raw: "http://[::1]", signal: "traces", protocol: ProtocolGRPC}, "http://[::1]:4317", "explicit port", ""},
		// D1.c: INSECURE contradicting the scheme → scheme wins, explicit URL, warning.
		{"https + INSECURE=true", endpointInput{raw: "https://c:4317", signal: "traces", protocol: ProtocolGRPC, insecure: true, insecureSet: true, insecureVar: EnvInsecure}, "https://c:4317", "contradicts", ""},
		{"http + INSECURE=false", endpointInput{raw: "http://c:4318", signal: "metrics", protocol: ProtocolHTTP, insecureSet: true, insecureVar: EnvInsecure}, "http://c:4318/v1/metrics", "contradicts", ""},
		{"http + INSECURE=true consistent", endpointInput{raw: "http://c:4318", signal: "metrics", protocol: ProtocolHTTP, insecure: true, insecureSet: true}, "http://c:4318/v1/metrics", "", ""},
		// Code URLs (ADR-001 (a)): generic gets /v1/<signal> for HTTP, per-signal verbatim.
		{"code generic http", endpointInput{raw: "http://c:4318", signal: "traces", protocol: ProtocolHTTP, fromCode: true}, "http://c:4318/v1/traces", "", ""},
		{"code generic http prefix", endpointInput{raw: "http://c:4318/otlp/", signal: "logs", protocol: ProtocolHTTP, fromCode: true}, "http://c:4318/otlp/v1/logs", "", ""},
		{"code per-signal verbatim", endpointInput{raw: "http://c:4318/custom/traces", signal: "traces", protocol: ProtocolHTTP, fromCode: true, perSignal: true}, "http://c:4318/custom/traces", "", ""},
		{"code grpc drops path", endpointInput{raw: "https://c:4317/ignored", signal: "traces", protocol: ProtocolGRPC, fromCode: true}, "https://c:4317", "", ""},
		{"code + WithInsecure conflict", endpointInput{raw: "https://c:4317", variable: "WithEndpoint", signal: "traces", protocol: ProtocolGRPC, fromCode: true, insecure: true, insecureSet: true, insecureFromCode: true}, "", "", "contradicts"},
		{"code unsupported scheme", endpointInput{raw: "ftp://c:21", variable: "WithEndpoint", signal: "traces", protocol: ProtocolGRPC, fromCode: true}, "", "", "unsupported endpoint scheme"},
		{"code invalid", endpointInput{raw: "http://[::1", variable: "WithEndpoint", signal: "traces", protocol: ProtocolGRPC, fromCode: true}, "", "", "invalid endpoint"},
		// Env value from code-only insecure: always explicit so env INSECURE can't override.
		{"env URL + code WithInsecure consistent → explicit", endpointInput{raw: "http://c:4317", signal: "traces", protocol: ProtocolGRPC, insecure: true, insecureSet: true, insecureFromCode: true}, "http://c:4317", "", ""},
		// R8: userinfo/query never reach the SDK (gRPC) nor diagnostics.
		{"userinfo + query stripped", endpointInput{raw: "http://u:pw@c?token=q", signal: "traces", protocol: ProtocolGRPC}, "http://c:4317", "explicit port", ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			res, err := resolveEndpoint(tt.in)
			if tt.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
					t.Fatalf("err = %v, want %q", err, tt.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if res.explicitURL != tt.wantURL {
				t.Errorf("explicitURL = %q, want %q", res.explicitURL, tt.wantURL)
			}
			var msgs []string
			for _, w := range res.warnings {
				msgs = append(msgs, w.msg+" "+w.value+" "+w.using)
			}
			joined := strings.Join(msgs, "|")
			switch {
			case tt.wantWarn == "" && len(msgs) > 0:
				t.Errorf("unexpected warnings: %s", joined)
			case tt.wantWarn != "" && !strings.Contains(joined, tt.wantWarn):
				t.Errorf("warnings %q, want one containing %q", joined, tt.wantWarn)
			}
			if strings.Contains(joined, "pw") || strings.Contains(joined, "token") {
				t.Errorf("warning leaks userinfo/query: %s", joined)
			}
		})
	}
}

func TestResolveEndpointInvalidEnvNotConfigured(t *testing.T) {
	for _, raw := range []string{"http://[::1", "http://:4317", ":::"} {
		res, err := resolveEndpoint(endpointInput{raw: raw, variable: EnvCollectorEndpoint, signal: "traces", protocol: ProtocolGRPC})
		if !errors.Is(err, errEndpointInvalid) {
			t.Errorf("%q: err = %v, want errEndpointInvalid", raw, err)
		}
		if len(res.warnings) != 1 || !strings.Contains(res.warnings[0].msg, "invalid endpoint") {
			t.Errorf("%q: warnings = %+v", raw, res.warnings)
		}
	}
}

func TestRedactURL(t *testing.T) {
	tests := map[string]string{
		"http://u:secret@c:4317/p?token=x#frag": "http://c:4317/p",
		"https://c":                             "https://c",
		"u:secret@c:4317?token=x":               "c:4317",
		"c:4317/otlp#f":                         "c:4317/otlp",
		"http://[::1":                           "<unparseable URL>",
	}
	for in, want := range tests {
		if got := redactURL(in); got != want {
			t.Errorf("redactURL(%q) = %q, want %q", in, got, want)
		}
	}
}

// ── per-signal resolution (R2) ─────────────────────────────────────────────

func TestResolveExport_PerSignalConfigured(t *testing.T) {
	// Regression: export.per_signal_endpoint_only (xfail:A3 — no exporter at all).
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://c:4318/v1/traces")
	o := newOptions(WithServiceName("svc"))
	if !o.export(signalTraces).configured {
		t.Error("traces not configured with only OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
	}
	if o.export(signalMetrics).configured || o.export(signalLogs).configured {
		t.Error("metrics/logs must not be configured by a traces-only endpoint")
	}
	if p := o.export(signalTraces).protocol; p != ProtocolHTTP {
		t.Errorf("protocol = %q, want http/protobuf inferred from the per-signal port 4318", p)
	}
	if u := o.export(signalTraces).endpointURL; u != "http://c:4318/v1/traces" {
		t.Errorf("endpointURL = %q, want the per-signal URL verbatim", u)
	}
}

func TestResolveExport_CodePerSignalBeatsEverything(t *testing.T) {
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://env:4318/v1/traces")
	o := newOptions(WithServiceName("svc"), WithEndpoint("http://generic:4318"),
		WithTracesEndpoint("http://code:4318/t"), WithMetricsEndpoint("http://code:4318/m"), WithLogsEndpoint("http://code:4318/l"))
	for sig, want := range map[string]string{signalTraces: "http://code:4318/t", signalMetrics: "http://code:4318/m", signalLogs: "http://code:4318/l"} {
		if got := o.export(sig).endpointURL; got != want {
			t.Errorf("%s endpointURL = %q, want %q", sig, got, want)
		}
	}
}

func TestResolveExport_Compression(t *testing.T) {
	// Regression: export.compression_default_gzip (Go HTTP metrics/logs sent uncompressed).
	o := newOptions(WithServiceName("svc"), WithEndpoint("http://c:4318"))
	for _, s := range allSignals {
		if c := o.export(s).compression; c != "gzip" {
			t.Errorf("%s default compression = %q, want gzip (D1.d)", s, c)
		}
	}
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "none")
	o = newOptions(WithServiceName("svc"), WithEndpoint("http://c:4318"))
	if c := o.export(signalTraces).compression; c != CompressionNone {
		t.Errorf("per-signal env none: compression = %q", c)
	}
	if c := o.export(signalLogs).compression; c != "gzip" {
		t.Errorf("logs compression = %q, want gzip default", c)
	}
	o = newOptions(WithServiceName("svc"), WithEndpoint("http://c:4318"), WithCompression("NONE"))
	if c := o.export(signalTraces).compression; c != "none" {
		t.Errorf("code compression = %q, want none", c)
	}
}

func TestResolveExport_TimeoutOnlyFromCode(t *testing.T) {
	// Regression G10 / export.timeout_env_grpc: a 10 s default used to be
	// passed in code, overriding OTEL_EXPORTER_OTLP_TIMEOUT.
	o := newOptions(WithServiceName("svc"), WithEndpoint("http://c:4317"))
	for _, s := range allSignals {
		if d := o.export(s).timeout; d != 0 {
			t.Errorf("%s timeout = %v, want 0 (not set in code)", s, d)
		}
	}
	o = newOptions(WithServiceName("svc"), WithEndpoint("http://c:4317"), WithExportTimeout(1500))
	for _, s := range allSignals {
		if d := o.export(s).timeout; d != 1500*time.Millisecond {
			t.Errorf("%s timeout = %v, want 1.5s", s, d)
		}
	}
	if err := newOptions(WithExportTimeout(0)).validate(); err == nil || !strings.Contains(err.Error(), "WithExportTimeout") {
		t.Errorf("WithExportTimeout(0): validate() = %v, want error naming the option", err)
	}
}

func TestResolveExport_CertificateWithPlaintextWarns(t *testing.T) {
	logs := captureDiag(t)
	t.Setenv(EnvCollectorEndpoint, "http://c:4317")
	t.Setenv("OTEL_EXPORTER_OTLP_CERTIFICATE", "/x/ca.pem")
	t.Setenv("OTEL_EXPORTER_OTLP_CLIENT_KEY", "/x/parity-secret-key.pem")
	newOptions(WithServiceName("svc"))
	out := logs.String()
	if !strings.Contains(out, "OTEL_EXPORTER_OTLP_CERTIFICATE") || !strings.Contains(out, "OTEL_EXPORTER_OTLP_CLIENT_KEY") {
		t.Errorf("warning does not name the TLS variables: %q", out)
	}
	if strings.Contains(out, "parity-secret-key") || strings.Contains(out, "/x/ca.pem") {
		t.Errorf("warning prints TLS file paths: %q", out)
	}
	if n := strings.Count(out, "TLS files are set"); n != 1 {
		t.Errorf("warned %d times, want once for the three signals", n)
	}
}

func TestResolveExport_InsecurePrecedence(t *testing.T) {
	logs := captureDiag(t)
	t.Setenv(EnvCollectorEndpoint, "https://c:4317")
	t.Setenv(EnvInsecure, "false")
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_INSECURE", "true")
	o := newOptions(WithServiceName("svc"))
	if u := o.export(signalTraces).endpointURL; u != "https://c:4317" {
		t.Errorf("traces endpointURL = %q, want explicit https (per-signal INSECURE conflicts)", u)
	}
	if u := o.export(signalMetrics).endpointURL; u != "https://c:4317" {
		t.Errorf("metrics endpointURL = %q, want SDK (generic INSECURE=false agrees with https)", u)
	}
	if !strings.Contains(logs.String(), "OTEL_EXPORTER_OTLP_TRACES_INSECURE") {
		t.Errorf("no warning naming the per-signal variable: %q", logs.String())
	}
}

// ── protocol (D1 table) ────────────────────────────────────────────────────

func TestSignalProtocol(t *testing.T) {
	logs := captureDiag(t)
	o := &Options{}
	if p := o.signalProtocol(signalTraces, "http://c:4318"); p != ProtocolHTTP {
		t.Errorf("4318 inference = %q", p)
	}
	if p := o.signalProtocol(signalTraces, "c:4317"); p != ProtocolGRPC {
		t.Errorf("default = %q", p)
	}
	t.Setenv(EnvOtlpProtocol, "http/protobuf")
	t.Setenv("OTEL_EXPORTER_OTLP_METRICS_PROTOCOL", "grpc")
	if p := o.signalProtocol(signalTraces, "c:4317"); p != ProtocolHTTP {
		t.Errorf("generic env = %q", p)
	}
	if p := o.signalProtocol(signalMetrics, "c:4318"); p != ProtocolGRPC {
		t.Errorf("per-signal env = %q", p)
	}
	t.Setenv("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", "bogus")
	t.Setenv(EnvOtlpProtocol, "http/json")
	if p := o.signalProtocol(signalLogs, "c:4317"); p != ProtocolGRPC {
		t.Errorf("unknown env values must fall back, got %q", p)
	}
	if !strings.Contains(logs.String(), "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL") || !strings.Contains(logs.String(), "http/json") {
		t.Errorf("missing warnings: %q", logs.String())
	}
	o.OtlpProtocol = " HTTP/PROTOBUF "
	if p := o.signalProtocol(signalLogs, "c:4317"); p != ProtocolHTTP {
		t.Errorf("code = %q", p)
	}
	if err := newOptions(WithOtlpProtocol("http/json")).validate(); err == nil || !strings.Contains(err.Error(), "WithOtlpProtocol") {
		t.Errorf("code http/json: validate() = %v, want error naming the option", err)
	}
}

func TestEndpointPort(t *testing.T) {
	for in, want := range map[string]string{"c:4318": "4318", "http://c:4318/x": "4318", "https://c": "", "[::1]:4317": "4317", "http://[::1": ""} {
		if got := endpointPort(in); got != want {
			t.Errorf("endpointPort(%q) = %q, want %q", in, got, want)
		}
	}
}

// ── identity (R4, R5, D4, ADR-003; D1 rows of 2026-10-29) ─────────────────

// resourceMap returns the attributes buildResource emits for o.
func resourceMap(o *Options) map[string]string {
	m := map[string]string{}
	for _, kv := range buildResource(o).Attributes() {
		m[string(kv.Key)] = kv.Value.Emit()
	}
	return m
}

var unknownServiceRe = regexp.MustCompile(`^unknown_service:.+$`)

func TestResolveIdentity_ServiceName(t *testing.T) {
	t.Run("code wins", func(t *testing.T) {
		t.Setenv(EnvOtelServiceName, "std")
		o := &Options{ServiceName: " code "}
		o.resolveIdentity()
		if o.ServiceName != "code" {
			t.Errorf("got %q", o.ServiceName)
		}
	})
	t.Run("OTEL_SERVICE_NAME beats OTEL_RESOURCE_ATTRIBUTES and SERVICE_NAME", func(t *testing.T) {
		// Regression: svc.otel_service_name_beats_legacy (SERVICE_NAME used to win).
		t.Setenv(EnvOtelServiceName, " std\t")
		t.Setenv(EnvResourceAttributes, "service.name=from-ra")
		t.Setenv(EnvServiceName, "legacy")
		o := &Options{}
		o.resolveIdentity()
		if o.ServiceName != "std" || resourceMap(o)["service.name"] != "std" {
			t.Errorf("ServiceName = %q resource=%v", o.ServiceName, resourceMap(o))
		}
	})
	t.Run("OTEL_RESOURCE_ATTRIBUTES beats SERVICE_NAME", func(t *testing.T) {
		t.Setenv(EnvResourceAttributes, "service.name=from-ra")
		t.Setenv(EnvServiceName, "legacy")
		o := &Options{}
		o.resolveIdentity()
		if o.ServiceName != "from-ra" {
			t.Errorf("ServiceName = %q", o.ServiceName)
		}
	})
	t.Run("SERVICE_NAME fallback", func(t *testing.T) {
		t.Setenv(EnvServiceName, "legacy")
		o := &Options{}
		if got := resourceMap(o)["service.name"]; got != "legacy" || o.ServiceName != "legacy" {
			t.Errorf("ServiceName = %q resource=%q", o.ServiceName, got)
		}
	})
	t.Run("none: SDK default + warning", func(t *testing.T) {
		// Regression: svc.unset_uses_sdk_default_and_warns (was "my-service").
		logs := captureDiag(t)
		o := &Options{}
		if got := resourceMap(o)["service.name"]; !unknownServiceRe.MatchString(got) || got != o.ServiceName {
			t.Errorf("ServiceName = %q resource=%q", o.ServiceName, got)
		}
		if !strings.Contains(logs.String(), EnvOtelServiceName) {
			t.Errorf("no warning naming %s: %q", EnvOtelServiceName, logs.String())
		}
	})
	t.Run("option beats the code attribute, in the value and the resource", func(t *testing.T) {
		o := &Options{ServiceName: "svc-option", ResourceAttributes: map[string]string{"service.name": "svc-attribute"}}
		if got := resourceMap(o)["service.name"]; got != "svc-option" || o.ServiceName != "svc-option" {
			t.Errorf("ServiceName = %q resource=%q", o.ServiceName, got)
		}
	})
	t.Run("code-only attribute resolves without the missing-name warning", func(t *testing.T) {
		logs := captureDiag(t)
		o := &Options{ResourceAttributes: map[string]string{"service.name": " svc-code "}}
		if got := resourceMap(o)["service.name"]; got != "svc-code" || o.ServiceName != "svc-code" {
			t.Errorf("ServiceName = %q resource=%q", o.ServiceName, got)
		}
		if strings.Contains(logs.String(), "no service name") {
			t.Errorf("unexpected missing-name warning: %q", logs.String())
		}
	})
	for name, tc := range map[string]struct{ code, ra string }{
		"blank code attribute": {code: " \t", ra: ""},
		"blank env attribute":  {code: "", ra: "service.name= "},
		"empty env attribute":  {code: "", ra: "service.name="},
	} {
		t.Run(name+" is not provided", func(t *testing.T) {
			logs := captureDiag(t)
			if tc.ra != "" {
				t.Setenv(EnvResourceAttributes, tc.ra)
			}
			o := &Options{}
			if tc.code != "" {
				o.ResourceAttributes = map[string]string{"service.name": tc.code}
			}
			got := resourceMap(o)["service.name"]
			if !unknownServiceRe.MatchString(got) {
				t.Errorf("service.name = %q, want the SDK default", got)
			}
			if !strings.Contains(logs.String(), "no service name") || strings.Contains(logs.String(), `using=""`) {
				t.Errorf("want the missing-name warning with the default: %q", logs.String())
			}
		})
	}
	t.Run("blank code attribute falls back to OTEL_SERVICE_NAME", func(t *testing.T) {
		t.Setenv(EnvOtelServiceName, "svc-std")
		o := &Options{ResourceAttributes: map[string]string{"service.name": " "}}
		if got := resourceMap(o)["service.name"]; got != "svc-std" {
			t.Errorf("service.name = %q, want svc-std", got)
		}
	})
}

func TestResolveIdentity_Environment(t *testing.T) {
	envKey := string(semconv.DeploymentEnvironmentNameKey)
	t.Run("unset: absent, warned once, INFO", func(t *testing.T) {
		// Regression: env.unset_absent_and_warns / log_level.unset_is_info (was LOCAL + DEBUG).
		logs := captureDiag(t)
		o := &Options{}
		res := resourceMap(o)
		if _, ok := res[envKey]; ok || o.Environment != "" {
			t.Errorf("Environment = %q resource=%v", o.Environment, res)
		}
		if n := countLines(logs.String(), "deployment.environment.name"); n != 1 {
			t.Errorf("warned %d times, want 1: %q", n, logs.String())
		}
		if lvl := DefaultLogLevel(o.Environment, false); lvl != slog.LevelInfo {
			t.Errorf("log level = %v, want INFO", lvl)
		}
	})
	t.Run("ENVIRONMENT known canonicalized", func(t *testing.T) {
		t.Setenv(EnvEnvironment, "prd")
		o := &Options{}
		if got := resourceMap(o)[envKey]; o.Environment != PRD || got != "PRD" {
			t.Errorf("Environment = %q resource=%q", o.Environment, got)
		}
	})
	t.Run("ENVIRONMENT unknown verbatim", func(t *testing.T) {
		// Regression: env.unknown_value_verbatim (was coerced to LOCAL).
		t.Setenv(EnvEnvironment, "staging")
		o := &Options{}
		if got := resourceMap(o)[envKey]; o.Environment != "staging" || got != "staging" {
			t.Errorf("Environment = %q resource=%q", o.Environment, got)
		}
		if lvl := DefaultLogLevel(o.Environment, false); lvl != slog.LevelInfo {
			t.Errorf("log level = %v, want INFO", lvl)
		}
	})
	t.Run("OTEL_RESOURCE_ATTRIBUTES beats ENVIRONMENT and is emitted verbatim", func(t *testing.T) {
		// Regression: env.resource_attributes_beat_alias / resource.env_attribute_not_overwritten.
		t.Setenv(EnvEnvironment, "DEV")
		t.Setenv(EnvResourceAttributes, envKey+"=hml")
		o := &Options{}
		if got := resourceMap(o)[envKey]; o.Environment != "hml" || got != "hml" {
			t.Errorf("Environment = %q resource=%q", o.Environment, got)
		}
	})
	t.Run("option beats the code attribute, which beats the env", func(t *testing.T) {
		t.Setenv(EnvResourceAttributes, envKey+"=HML")
		o := &Options{Environment: "PRD", ResourceAttributes: map[string]string{envKey: "DEV"}}
		if got := resourceMap(o)[envKey]; o.Environment != PRD || got != "PRD" {
			t.Errorf("Environment = %q resource=%q", o.Environment, got)
		}
		o = &Options{ResourceAttributes: map[string]string{envKey: "perf"}}
		if got := resourceMap(o)[envKey]; o.Environment != "perf" || got != "perf" {
			t.Errorf("Environment = %q resource=%q", o.Environment, got)
		}
	})
	t.Run("blank env attribute is absent and warned", func(t *testing.T) {
		logs := captureDiag(t)
		t.Setenv(EnvResourceAttributes, envKey+"= ")
		o := &Options{}
		if v, ok := resourceMap(o)[envKey]; ok {
			t.Errorf("blank environment emitted as %q", v)
		}
		if !strings.Contains(logs.String(), "deployment.environment.name is not set") {
			t.Errorf("missing ADR-003 warning: %q", logs.String())
		}
	})
	t.Run("blank code attribute falls back to the env", func(t *testing.T) {
		t.Setenv(EnvResourceAttributes, envKey+"=HML")
		o := &Options{ResourceAttributes: map[string]string{envKey: ""}}
		if got := resourceMap(o)[envKey]; got != "HML" {
			t.Errorf("environment = %q, want HML", got)
		}
	})
}

var uuidRe = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

func TestResolveIdentity_InstanceID(t *testing.T) {
	// Regression: resource.instance_id_generated (no id before A3).
	o := &Options{ServiceName: "svc"}
	id := resourceMap(o)["service.instance.id"]
	if !uuidRe.MatchString(id) {
		t.Errorf("instance id %q is not a v4 UUID", id)
	}
	if resourceMap(&Options{ServiceName: "svc"})["service.instance.id"] == id {
		t.Error("two resolutions produced the same id")
	}
	t.Setenv(EnvResourceAttributes, "service.instance.id=from-env")
	if got := resourceMap(&Options{ServiceName: "svc"})["service.instance.id"]; got != "from-env" {
		t.Errorf("env id not kept: %q", got)
	}
	o = &Options{ServiceName: "svc", ResourceAttributes: map[string]string{"service.instance.id": "code"}}
	if got := resourceMap(o)["service.instance.id"]; got != "code" {
		t.Errorf("code id not kept: %q", got)
	}
	for _, blank := range []string{"", " \t"} {
		t.Setenv(EnvResourceAttributes, "service.instance.id="+blank)
		if got := resourceMap(&Options{ServiceName: "svc"})["service.instance.id"]; !uuidRe.MatchString(got) {
			t.Errorf("blank env id %q: got %q, want a generated UUID", blank, got)
		}
		o = &Options{ServiceName: "svc", ResourceAttributes: map[string]string{"service.instance.id": blank}}
		t.Setenv(EnvResourceAttributes, "")
		if got := resourceMap(o)["service.instance.id"]; !uuidRe.MatchString(got) {
			t.Errorf("blank code id %q: got %q, want a generated UUID", blank, got)
		}
	}
}

// ── OTEL_RESOURCE_ATTRIBUTES parsing (D1 row of 2026-10-29, R8) ──────────────

func TestParseResourceAttributes(t *testing.T) {
	tests := []struct {
		name                   string
		raw                    string
		want                   map[string]string
		malformed, undecodable []int
	}{
		{"plain", "a=1,b=2", map[string]string{"a": "1", "b": "2"}, nil, nil},
		{"blank items ignored silently", " ,a=1,,\t,", map[string]string{"a": "1"}, nil, nil},
		{"first = splits", "a=b=c", map[string]string{"a": "b=c"}, nil, nil},
		{"no = skipped", "a=1,broken,b=2", map[string]string{"a": "1", "b": "2"}, []int{2}, nil},
		{"empty key skipped", "=orphan, \t=x,a=1", map[string]string{"a": "1"}, []int{1, 2}, nil},
		{"key and value trimmed", " a \v= 1\f", map[string]string{"a": "1"}, nil, nil},
		{"decode then trim", "a=%20payments%09", map[string]string{"a": "payments"}, nil, nil},
		{"plus kept", "a=x+y", map[string]string{"a": "x+y"}, nil, nil},
		{"utf-8 decoded", "a=caf%C3%A9", map[string]string{"a": "café"}, nil, nil},
		{"bad escape literal", "a=%zz", map[string]string{"a": "%zz"}, nil, []int{1}},
		{"bad escape literal, trimmed", "a= %zz ", map[string]string{"a": "%zz"}, nil, []int{1}},
		{"truncated escape literal", "a=%4", map[string]string{"a": "%4"}, nil, []int{1}},
		{"invalid utf-8 literal", "x=1,a=%C3", map[string]string{"x": "1", "a": "%C3"}, nil, []int{2}},
		{"raw invalid utf-8 replaced", "a=\xff", map[string]string{"a": "\uFFFD"}, nil, []int{1}},
		{"one replacement per invalid byte", "a=\xff\xfe", map[string]string{"a": "\uFFFD\uFFFD"}, nil, []int{1}},
		{"valid runes kept around invalid bytes", "a=é\xffx", map[string]string{"a": "é\uFFFDx"}, nil, []int{1}},
		{"non-utf-8 key malformed", "k\xff=1,a=1", map[string]string{"a": "1"}, []int{1}, nil},
		{"unicode space not trimmed", "a=\u00a0x", map[string]string{"a": "\u00a0x"}, nil, nil},
		{"later duplicate wins", "a=1,a=2", map[string]string{"a": "2"}, nil, nil},
		{"empty value kept", "a=", map[string]string{"a": ""}, nil, nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			p := parseResourceAttributes(tt.raw)
			if !reflect.DeepEqual(p.attrs, tt.want) || !reflect.DeepEqual(p.malformed, tt.malformed) ||
				!reflect.DeepEqual(p.undecodable, tt.undecodable) {
				t.Errorf("parse(%q) = %v malformed=%v undecodable=%v; want %v %v %v",
					tt.raw, p.attrs, p.malformed, p.undecodable, tt.want, tt.malformed, tt.undecodable)
			}
		})
	}
}

func TestEnvResourceAttributes_PositionalWarningsNeverEchoValues(t *testing.T) {
	logs := captureDiag(t)
	t.Setenv(EnvResourceAttributes, "token-s3cr3t-value,api.key=%zzs3cr3t,=s3cr3t-orphan,team.name=payments,x=%C3s3cr3t")
	got := envResourceAttributes().attrs
	if got["team.name"] != "payments" || got["api.key"] != "%zzs3cr3t" {
		t.Errorf("attrs = %v", got)
	}
	out := logs.String()
	if strings.Contains(out, "s3cr3t") {
		t.Fatalf("diagnostics echo a value: %q", out)
	}
	if !strings.Contains(out, "not key=value") || !strings.Contains(out, "items=1,3") ||
		!strings.Contains(out, "invalid percent-encoding") || !strings.Contains(out, "items=2,5") ||
		strings.Count(out, EnvResourceAttributes) != 2 {
		t.Errorf("want one positional warning per kind naming %s: %q", EnvResourceAttributes, out)
	}
}

func TestEnvResourceAttributes_CleanInputDoesNotWarn(t *testing.T) {
	logs := captureDiag(t)
	t.Setenv(EnvResourceAttributes, " , team.name=%20payments%09,,")
	p := envResourceAttributes()
	if p.attrs["team.name"] != "payments" || !p.blank || !p.sdkWouldEcho() {
		t.Errorf("parse = %+v (blank items must still arm the SDK echo filter)", p)
	}
	if logs.String() != "" {
		t.Errorf("unexpected diagnostics: %q", logs.String())
	}
	t.Setenv(EnvResourceAttributes, " \t")
	if p := envResourceAttributes(); len(p.attrs) != 0 || p.sdkWouldEcho() {
		t.Errorf("blank variable parsed to %+v", p)
	}
	t.Setenv(EnvResourceAttributes, "a=1,b=2")
	if envResourceAttributes().sdkWouldEcho() {
		t.Error("clean value must not arm the echo filter")
	}
}

func TestEnvResourceAttributes_WarnedOncePerProcess(t *testing.T) {
	logs := captureDiag(t)
	t.Setenv(EnvResourceAttributes, "broken,a=%zz")
	envResourceAttributes()
	envResourceAttributes()
	if n := strings.Count(logs.String(), "not key=value"); n != 1 {
		t.Errorf("malformed warning %d times, want 1: %q", n, logs.String())
	}
	if n := strings.Count(logs.String(), "invalid percent-encoding"); n != 1 {
		t.Errorf("undecodable warning %d times, want 1: %q", n, logs.String())
	}
}

func TestResolveIdentity_WarningsOncePerProcess(t *testing.T) {
	logs := captureDiag(t)
	for range 2 {
		(&Options{}).resolveIdentity()
	}
	if n := countLines(logs.String(), "no service name configured"); n != 1 {
		t.Errorf("missing-name warning %d times, want 1", n)
	}
	if n := countLines(logs.String(), "deployment.environment.name is not set"); n != 1 {
		t.Errorf("ADR-003 warning %d times, want 1", n)
	}
}

func TestResolveIdentity_CodeTelemetrySDKWarnedOnceByKey(t *testing.T) {
	logs := captureDiag(t)
	t.Setenv(EnvResourceAttributes, "telemetry.sdk.name=from-env")
	for range 2 {
		(&Options{ServiceName: "svc", ResourceAttributes: map[string]string{"telemetry.sdk.version": "s3cr3t"}}).resolveIdentity()
	}
	out := logs.String()
	if n := countLines(out, "telemetry.sdk.version in WithResourceAttributes is ignored"); n != 1 {
		t.Errorf("code telemetry.sdk warning %d times, want 1: %q", n, out)
	}
	if strings.Contains(out, "s3cr3t") || strings.Contains(out, "telemetry.sdk.name") {
		t.Errorf("the warning must name the code key only, never a value or an env key: %q", out)
	}
}

func TestValidate_EmptyResourceAttributeKeyFails(t *testing.T) {
	for _, k := range []string{"", " \t"} {
		o := newOptions(WithServiceName("svc"), WithResourceAttributes(map[string]string{k: "x", "a": "b"}))
		if err := o.validate(); err == nil || !strings.Contains(err.Error(), "empty key") {
			t.Errorf("key %q: err = %v, want an empty-key error", k, err)
		}
	}
}

// ── SDK echo filter (D1 row "Go: SDK providers re-read OTEL_RESOURCE_ATTRIBUTES") ──

// recordingHandler is goroutine-safe: the first SetErrorHandler of the process
// makes the API's default delegator forward to it for good, so SDK goroutines
// of later tests may still reach it.
type recordingHandler struct {
	mu  sync.Mutex
	got []string
}

func (h *recordingHandler) Handle(err error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.got = append(h.got, err.Error())
}

func (h *recordingHandler) errors() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]string(nil), h.got...)
}

func withErrorHandler(t *testing.T, h otel.ErrorHandler) {
	t.Helper()
	prev := otel.GetErrorHandler()
	otel.SetErrorHandler(h)
	t.Cleanup(func() { otel.SetErrorHandler(prev) })
}

func TestQuietResourceEnvEcho_DropsOnlyTheSDKEcho(t *testing.T) {
	user := &recordingHandler{}
	withErrorHandler(t, user)
	stop := quietResourceEnvEcho(true)
	otel.Handle(fmt.Errorf("%w: missing value: [token-s3cr3t]", resource.ErrPartialResource))
	otel.Handle(url.EscapeError("%zz"))
	otel.Handle(&url.Error{Op: "Post", URL: "https://c", Err: url.EscapeError("%zz")}) // an exporter's: kept
	otel.Handle(errors.New("exporter failed"))
	stop()
	stop() // idempotent

	otel.Handle(fmt.Errorf("%w: later", resource.ErrPartialResource)) // outside the window: kept
	want := []string{`Post "https://c": invalid URL escape "%zz"`, "exporter failed", "partial resource: later"}
	if got := user.errors(); !reflect.DeepEqual(got, want) {
		t.Errorf("user handler got %q, want %q", got, want)
	}
	if otel.GetErrorHandler() != otel.ErrorHandler(user) {
		t.Error("previous handler not restored")
	}
}

func TestQuietResourceEnvEcho_NotNeededInstallsNothing(t *testing.T) {
	user := &recordingHandler{}
	withErrorHandler(t, user)
	quietResourceEnvEcho(false)()
	if otel.GetErrorHandler() != otel.ErrorHandler(user) {
		t.Error("handler replaced although nothing would be echoed")
	}
}

func TestQuietResourceEnvEcho_KeepsAHandlerSetDuringTheWindow(t *testing.T) {
	user, late := &recordingHandler{}, &recordingHandler{}
	withErrorHandler(t, user)
	stop := quietResourceEnvEcho(true)
	otel.SetErrorHandler(late)
	stop()
	if otel.GetErrorHandler() != otel.ErrorHandler(late) {
		t.Error("a handler installed during the window was overwritten")
	}
}

func TestResourceEnvEchoFilter_NeverLoops(t *testing.T) {
	var buf bytes.Buffer
	prevOut := log.Writer()
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(prevOut) })
	f := &resourceEnvEchoFilter{prev: nil}
	f.active.Store(true)
	f.Handle(errors.New("to log, not to a nil handler"))
	f.prev = f
	f.Handle(errors.New("to log, not back to itself"))
	f.active.Store(false)
	withErrorHandler(t, f) // inactive and global: forwarding to the global would loop
	f.Handle(errors.New("inactive global"))
	for _, msg := range []string{"nil handler", "back to itself", "inactive global"} {
		if !strings.Contains(buf.String(), msg) {
			t.Errorf("%q not logged: %q", msg, buf.String())
		}
	}
	if !forwardsBackToFilter(otelDefaultHandlerForTest()) {
		t.Error("the SDK default delegator must be treated as looping back")
	}
}

// sdkDefaultErrorHandler is the API's default delegating handler, read at
// package init, before any test installs one.
var sdkDefaultErrorHandler = otel.GetErrorHandler()

func otelDefaultHandlerForTest() otel.ErrorHandler { return sdkDefaultErrorHandler }

func TestSetup_SDKDoesNotEchoMalformedResourceItems(t *testing.T) {
	user := &recordingHandler{}
	withErrorHandler(t, user)
	t.Setenv(EnvResourceAttributes, "token-s3cr3t-value,,api.key=%zzs3cr3t,team.name=payments,deployment.environment.name=DEV")
	shutdown, err := Setup(context.Background(), WithServiceName("svc"), WithoutMetricsListener())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = shutdown(context.Background()) })
	for _, e := range user.errors() {
		if strings.Contains(e, "s3cr3t") || strings.Contains(e, "partial resource") {
			t.Errorf("SDK echo reached the application's handler: %q", e)
		}
	}
	if otel.GetErrorHandler() != otel.ErrorHandler(user) {
		t.Error("Setup did not restore the application's error handler")
	}
}

// ── telemetry.sdk.* and schema URL (D1 row of 2026-10-29) ──────────────────

func TestBuildResource_TelemetrySDKFromSDKAndSchemaURL(t *testing.T) {
	t.Setenv(EnvResourceAttributes, "telemetry.sdk.name=env,telemetry.sdk.language=env,telemetry.sdk.version=0,telemetry.distro.name=d")
	o := &Options{ServiceName: "svc", ResourceAttributes: map[string]string{"telemetry.sdk.version": "code"}}
	res := buildResource(o)
	got := resourceMap(o)
	if got["telemetry.sdk.name"] != "opentelemetry" || got["telemetry.sdk.language"] != "go" ||
		got["telemetry.sdk.version"] != sdk.Version() || got["telemetry.distro.name"] != "d" {
		t.Errorf("resource = %v", got)
	}
	if res.SchemaURL() != "https://opentelemetry.io/schemas/1.43.0" || semconv.SchemaURL != schemaURL {
		t.Errorf("schema URL = %q (semconv package %q), want the contract literal", res.SchemaURL(), semconv.SchemaURL)
	}
}

func TestBuildResource_CodeAndFallbackLayers(t *testing.T) {
	t.Setenv(EnvServiceName, "legacy")
	t.Setenv(EnvEnvironment, "staging")
	o := newOptions(WithResourceAttributes(map[string]string{"team": "platform"}))
	got := map[string]string{}
	for _, kv := range buildResource(o).Attributes() {
		got[string(kv.Key)] = kv.Value.Emit()
	}
	if got["service.name"] != "legacy" || got["deployment.environment.name"] != "staging" || got["team"] != "platform" ||
		!uuidRe.MatchString(got["service.instance.id"]) {
		t.Errorf("resource = %v", got)
	}
	if _, ok := got["deployment.environment"]; ok {
		t.Error("legacy deployment.environment key emitted")
	}
}

func TestValidate_CodeValuesFailFast(t *testing.T) {
	tests := map[string]Option{
		"WithServiceName":  WithServiceName("  "),
		"WithEnvironment":  WithEnvironment(""),
		"WithCompression":  WithCompression("zstd"),
		"WithEndpoint":     WithEndpoint("ftp://c"),
		"WithInsecure":     func(o *Options) { WithEndpoint("https://c:4317")(o); WithInsecure(true)(o) },
		"WithOtlpProtocol": WithOtlpProtocol("thrift"),
	}
	for name, opt := range tests {
		err := newOptions(opt).validate()
		if err == nil || !strings.Contains(err.Error(), name) {
			t.Errorf("%s: validate() = %v, want an error naming the option", name, err)
		}
	}
	if err := newOptions(WithEndpoint("ftp://c")).validate(); strings.Count(err.Error(), "WithEndpoint") != 1 {
		t.Errorf("generic code endpoint error repeated per signal: %v", err)
	}
	// Setup surfaces it and stays retryable.
	resetGlobals()
	if _, err := Setup(context.Background(), WithCompression("zstd")); err == nil {
		t.Error("Setup accepted an invalid code value")
	}
	resetGlobals()
}

// ── env parsing (R8) ───────────────────────────────────────────────────────

func TestParseExporterEnv(t *testing.T) {
	tests := []struct {
		value    string
		want     []string // nil = unset/default
		wantWarn string
	}{
		{"", nil, ""},
		{"otlp", []string{"otlp"}, ""},
		{"console", []string{"console"}, ""},
		{"Console , OTLP", []string{"console", "otlp"}, ""},
		{"none", []string{}, ""},
		{"logging", []string{"console"}, "deprecated"},
		{"none,otlp", []string{}, "combined"},
		{"bogus", nil, "unknown exporter"},
		{"bogus,otlp", []string{"otlp"}, "unknown exporter"},
		{"otlp,otlp", []string{"otlp"}, ""},
	}
	for _, tt := range tests {
		t.Run(tt.value, func(t *testing.T) {
			logs := captureDiag(t)
			t.Setenv(EnvTracesExporter, tt.value)
			got := parseExporterEnv(EnvTracesExporter, "otlp", "console")
			if (got == nil) != (tt.want == nil) || len(got) != len(tt.want) || strings.Join(got, ",") != strings.Join(tt.want, ",") {
				t.Errorf("got %#v, want %#v", got, tt.want)
			}
			if tt.wantWarn != "" && (!strings.Contains(logs.String(), tt.wantWarn) || !strings.Contains(logs.String(), EnvTracesExporter)) {
				t.Errorf("warnings %q, want %q naming the variable", logs.String(), tt.wantWarn)
			}
			if tt.wantWarn == "" && logs.String() != "" {
				t.Errorf("unexpected warning: %q", logs.String())
			}
		})
	}
}

func TestSignalExportersDefault(t *testing.T) {
	o := &Options{}
	if got := o.signalExporters(signalTraces); strings.Join(got, ",") != "otlp" {
		t.Errorf("traces default = %v", got)
	}
	o.logsExporters = []string{}
	if got := o.signalExporters(signalLogs); len(got) != 0 {
		t.Errorf("logs none = %v", got)
	}
}

func TestParsePropagators(t *testing.T) {
	tests := []struct {
		value    string
		fields   string
		wantWarn string
	}{
		{"", "baggage,traceparent,tracestate", ""},
		{"baggage", "baggage", ""},
		{"tracecontext", "traceparent,tracestate", ""},
		{"none", "", ""},
		{"none,baggage", "", "combined"},
		{"b3", "baggage,traceparent,tracestate", "not supported"},
		{"bogus,baggage", "baggage", "unknown propagator"},
		{"baggage,baggage", "baggage", ""},
	}
	for _, tt := range tests {
		t.Run(tt.value, func(t *testing.T) {
			logs := captureDiag(t)
			p := parsePropagators(tt.value)
			fields := p.Fields()
			sortStrings(fields)
			if got := strings.Join(fields, ","); got != tt.fields {
				t.Errorf("fields = %q, want %q", got, tt.fields)
			}
			if tt.wantWarn != "" && !strings.Contains(logs.String(), tt.wantWarn) {
				t.Errorf("warnings %q, want %q", logs.String(), tt.wantWarn)
			}
		})
	}
}

func countLines(s, sub string) int {
	n := 0
	for _, l := range strings.Split(s, "\n") {
		if strings.Contains(l, sub) {
			n++
		}
	}
	return n
}

func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j] < s[j-1]; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}

func TestEnvBool(t *testing.T) {
	logs := captureDiag(t)
	for v, want := range map[string][2]bool{"": {false, false}, "TRUE": {true, true}, "false": {false, true}, "maybe": {false, true}} {
		t.Setenv(EnvSDKDisabled, v)
		got, set := envBool(EnvSDKDisabled)
		if got != want[0] || set != want[1] {
			t.Errorf("%q: (%v,%v), want %v", v, got, set, want)
		}
	}
	if !strings.Contains(logs.String(), EnvSDKDisabled) || !strings.Contains(logs.String(), "maybe") {
		t.Errorf("no warning for an unparseable boolean: %q", logs.String())
	}
}

func TestEnvParseWarnings(t *testing.T) {
	logs := captureDiag(t)
	t.Setenv(EnvSampleRatio, "half")
	t.Setenv(EnvMetricsPort, "http")
	o := newOptions(WithServiceName("svc"))
	if o.SampleRatio != 1.0 || o.PrometheusMetricsPort != 9464 {
		t.Errorf("defaults not kept: ratio=%v port=%d", o.SampleRatio, o.PrometheusMetricsPort)
	}
	for _, v := range []string{EnvSampleRatio, EnvMetricsPort} {
		if !strings.Contains(logs.String(), v) {
			t.Errorf("no warning naming %s", v)
		}
	}
}

func TestDefaultLogLevelCaseInsensitive(t *testing.T) {
	for env, want := range map[DeploymentEnvironment]slog.Level{"local": slog.LevelDebug, "Prd": slog.LevelWarn, "": slog.LevelInfo, "qa": slog.LevelInfo} {
		if got := DefaultLogLevel(env, false); got != want {
			t.Errorf("DefaultLogLevel(%q) = %v, want %v", env, got, want)
		}
	}
}

// ── Setup-level regressions ─────────────────────────────────────────────────

// G7: the helper must not call os.Setenv (it used to force
// OTEL_METRICS_EXEMPLAR_FILTER=trace_based, which is the SDK default anyway).
func TestSetupDoesNotSetEnv(t *testing.T) {
	resetGlobals()
	t.Setenv("OTEL_METRICS_EXEMPLAR_FILTER", "")
	os.Unsetenv("OTEL_METRICS_EXEMPLAR_FILTER")
	shutdown, err := Setup(context.Background(), WithServiceName("g7"), WithoutMetricsListener())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = shutdown(context.Background()); resetGlobals() }()
	if v, ok := os.LookupEnv("OTEL_METRICS_EXEMPLAR_FILTER"); ok {
		t.Errorf("Setup set OTEL_METRICS_EXEMPLAR_FILTER=%q (G7)", v)
	}
}

func TestSetupSDKDisabled(t *testing.T) {
	// Regression: export.sdk_disabled (Go ignored OTEL_SDK_DISABLED).
	resetGlobals()
	t.Setenv(EnvSDKDisabled, "true")
	shutdown, err := Setup(context.Background(), WithServiceName("off"), WithEndpoint("http://127.0.0.1:9"))
	if err != nil {
		t.Fatal(err)
	}
	defer resetGlobals()
	if installedOptions() == nil {
		t.Error("installed options not recorded")
	}
	if err := shutdown(context.Background()); err != nil {
		t.Errorf("shutdown: %v", err)
	}
	if _, ok := otel.GetTracerProvider().(*sdktrace.TracerProvider); ok {
		t.Error("an SDK TracerProvider was installed with OTEL_SDK_DISABLED=true")
	}
	if _, ok := logglobal.GetLoggerProvider().(*sdklog.LoggerProvider); ok {
		t.Error("an SDK LoggerProvider was installed")
	}
}

func TestSetupPropagatorsFromEnv(t *testing.T) {
	// Regression: propagation.env_honored (always tracecontext,baggage before).
	resetGlobals()
	t.Setenv(EnvPropagators, "baggage")
	shutdown, err := Setup(context.Background(), WithServiceName("prop"), WithoutMetricsListener())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = shutdown(context.Background()); resetGlobals() }()
	fields := otel.GetTextMapPropagator().Fields()
	if strings.Join(fields, ",") != "baggage" {
		t.Errorf("propagator fields = %v, want [baggage]", fields)
	}
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
}

func TestSetupConsoleExporters(t *testing.T) {
	resetGlobals()
	t.Setenv(EnvTracesExporter, "console")
	t.Setenv(EnvLogsExporter, "console")
	t.Setenv(EnvMetricsExporter, "console")
	shutdown, err := Setup(context.Background(), WithServiceName("console"))
	if err != nil {
		t.Fatal(err)
	}
	defer resetGlobals()
	o := installedOptions()
	if !containsStr(o.resolvedMetricExporters(), "console") || o.metricIntervalMs != defaultExportIntervalMs {
		t.Errorf("metric exporters %v interval %d", o.resolvedMetricExporters(), o.metricIntervalMs)
	}
	if !logsBridged() {
		t.Error("console logs exporter did not count as a processor")
	}
	_ = shutdown(context.Background())
}

// G8: OTEL_EXPORTER_OTLP_CERTIFICATE must reach the TLS handshake. Before
// A3 the helper passed an empty tls.Config (x509: certificate signed by
// unknown authority; parity export.http_tls_custom_ca was xfail:A3).
func TestG8_EnvCertificateUsed(t *testing.T) {
	got := make(chan string, 4)
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got <- r.URL.Path
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	caFile := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(caFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw}), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("OTEL_EXPORTER_OTLP_CERTIFICATE", caFile)
	t.Setenv(EnvCollectorEndpoint, srv.URL) // https://127.0.0.1:<port>
	t.Setenv(EnvOtlpProtocol, ProtocolHTTP)
	o := newOptions(WithServiceName("g8"))
	exp, err := newOTLPTraceExporter(context.Background(), o.export(signalTraces))
	if err != nil {
		t.Fatal(err)
	}
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	_, span := tp.Tracer("g8").Start(context.Background(), "s")
	span.End()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = tp.Shutdown(ctx)
	select {
	case p := <-got:
		if p != "/v1/traces" {
			t.Errorf("path = %q", p)
		}
	default:
		t.Fatal("no request reached the TLS server with the env CA (G8)")
	}
}

// Every builder branch constructs an exporter (no network at New).
func TestOTLPExporterBuilders(t *testing.T) {
	ctx := context.Background()
	for _, proto := range []string{ProtocolGRPC, ProtocolHTTP} {
		for _, e := range []*signalExport{
			{configured: true, protocol: proto},
			{configured: true, protocol: proto, endpointURL: "http://127.0.0.1:9/v1/x", compression: "gzip", timeout: time.Second},
			{configured: true, protocol: proto, endpointURL: "https://127.0.0.1:9", compression: "none", dropTLS: true},
		} {
			te, err := newOTLPTraceExporter(ctx, e)
			if err != nil {
				t.Fatalf("trace %s %+v: %v", proto, e, err)
			}
			me, err := newOTLPMetricExporter(ctx, e)
			if err != nil {
				t.Fatalf("metric %s %+v: %v", proto, e, err)
			}
			le, err := newOTLPLogExporter(ctx, e)
			if err != nil {
				t.Fatalf("log %s %+v: %v", proto, e, err)
			}
			sctx, cancel := context.WithTimeout(ctx, time.Second)
			_ = te.Shutdown(sctx)
			_ = me.Shutdown(sctx)
			_ = le.Shutdown(sctx)
			cancel()
		}
	}
}

// D1.c corollary / R8: CERTIFICATE + plaintext http:// under http/protobuf
// made the Go SDK's otlpmetrichttp.New fail ("insecure HTTP endpoint cannot
// use TLS client configuration"), i.e. env-only config failed Setup.
func TestCertificateWithPlaintextHTTPDoesNotFail(t *testing.T) {
	caFile := filepath.Join(t.TempDir(), "ca.pem")
	srv := httptest.NewTLSServer(http.NotFoundHandler())
	defer srv.Close()
	if err := os.WriteFile(caFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw}), 0o600); err != nil {
		t.Fatal(err)
	}
	logs := captureDiag(t)
	t.Setenv(EnvCollectorEndpoint, "http://127.0.0.1:9")
	t.Setenv(EnvOtlpProtocol, ProtocolHTTP)
	t.Setenv("OTEL_EXPORTER_OTLP_CERTIFICATE", caFile)
	o := newOptions(WithServiceName("svc"))
	for _, s := range allSignals {
		if !o.export(s).dropTLS {
			t.Errorf("%s: dropTLS not set", s)
		}
	}
	ctx := context.Background()
	if _, err := newOTLPMetricExporter(ctx, o.export(signalMetrics)); err != nil {
		t.Errorf("metric exporter: %v", err)
	}
	if _, err := newOTLPLogExporter(ctx, o.export(signalLogs)); err != nil {
		t.Errorf("log exporter: %v", err)
	}
	if _, err := newOTLPTraceExporter(ctx, o.export(signalTraces)); err != nil {
		t.Errorf("trace exporter: %v", err)
	}
	if !strings.Contains(logs.String(), "OTEL_EXPORTER_OTLP_CERTIFICATE") {
		t.Errorf("no warning: %q", logs.String())
	}
	// gRPC too: the SDK would dial TLS with the env CA although the scheme
	// is http:// (A3 review H1; parity export.certificate_with_plaintext_grpc).
	t.Setenv(EnvOtlpProtocol, ProtocolGRPC)
	og := newOptions(WithServiceName("svc"))
	for _, s := range allSignals {
		if !og.export(s).dropTLS {
			t.Errorf("gRPC %s: dropTLS not set", s)
		}
	}
	t.Setenv(EnvCollectorEndpoint, "https://127.0.0.1:9")
	if newOptions(WithServiceName("svc")).export(signalTraces).dropTLS {
		t.Error("dropTLS set for an https:// endpoint")
	}
}

// Precedence: code generic endpoint beats env per-signal (design D1 table).
func TestCodeGenericBeatsEnvPerSignal(t *testing.T) {
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://env:80/custom")
	o := newOptions(WithServiceName("svc"), WithEndpoint("http://code:4318"), WithOtlpProtocol(ProtocolHTTP))
	if got := o.export(signalTraces).endpointURL; got != "http://code:4318/v1/traces" {
		t.Errorf("endpointURL = %q", got)
	}
}

func TestResolveCompressionEnv(t *testing.T) {
	logs := captureDiag(t)
	o := &Options{}
	t.Setenv(otlpEnv("", "COMPRESSION"), " GZIP ")
	if c := o.resolveCompression(signalTraces); c != CompressionGzip {
		t.Errorf("GZIP = %q", c)
	}
	t.Setenv(otlpEnv(signalLogs, "COMPRESSION"), "None")
	if c := o.resolveCompression(signalLogs); c != CompressionNone {
		t.Errorf("per-signal None = %q", c)
	}
	for _, v := range []string{"zstd", "br", "deflate"} {
		t.Setenv(otlpEnv("", "COMPRESSION"), v)
		if c := o.resolveCompression(signalMetrics); c != CompressionGzip {
			t.Errorf("%s = %q, want gzip", v, c)
		}
		if !strings.Contains(logs.String(), v) {
			t.Errorf("no warning for %s", v)
		}
	}
}

func TestResolveTimeoutEnv(t *testing.T) {
	logs := captureDiag(t)
	o := &Options{}
	if d := o.resolveTimeout(signalTraces); d != 0 {
		t.Errorf("unset = %v, want 0 (SDK default)", d)
	}
	t.Setenv(otlpEnv("", "TIMEOUT"), "500")
	t.Setenv(otlpEnv(signalTraces, "TIMEOUT"), "abc")
	if d := o.resolveTimeout(signalTraces); d != 500*time.Millisecond {
		t.Errorf("garbage per-signal → generic: %v", d)
	}
	t.Setenv(otlpEnv("", "TIMEOUT"), "0")
	t.Setenv(otlpEnv(signalTraces, "TIMEOUT"), "-3")
	if d := o.resolveTimeout(signalTraces); d != defaultOtlpTimeout {
		t.Errorf("all invalid → 10 s: %v", d)
	}
	t.Setenv(otlpEnv(signalTraces, "TIMEOUT"), "250.5")
	if d := o.resolveTimeout(signalTraces); d != 250500*time.Microsecond {
		t.Errorf("fractional ms: %v", d)
	}
	o.ExportTimeoutMs = 42
	if d := o.resolveTimeout(signalTraces); d != 42*time.Millisecond {
		t.Errorf("code wins: %v", d)
	}
	for _, v := range []string{"abc", "-3", "OTEL_EXPORTER_OTLP_TIMEOUT"} {
		if !strings.Contains(logs.String(), v) {
			t.Errorf("no warning mentioning %q: %q", v, logs.String())
		}
	}
}

func TestParseHeaders(t *testing.T) {
	h, bad, literal := parseHeaders("x-parity-a=1, broken,,Authorization=Bearer%20s3cret,b@d=x,c=a%zzb, d = two ,x-parity%2Dk=v,X-Dup=2,x-dup=3")
	want := map[string]string{"x-parity-a": "1", "authorization": "Bearer s3cret", "c": "a%zzb", "d": "two", "x-dup": "3"}
	if strings.TrimSpace(fmtInts(literal)) != "6" {
		t.Errorf("literal positions = %v, want [6]", literal)
	}
	if len(h) != len(want) {
		t.Fatalf("headers = %v", h)
	}
	for k, v := range want {
		if h[k] != v {
			t.Errorf("%s = %q, want %q", k, h[k], v)
		}
	}
	if strings.Join(strings.Fields(fmtInts(bad)), ",") != "2,3,5,8" {
		t.Errorf("bad positions = %v", bad)
	}
}

func fmtInts(v []int) string {
	var b strings.Builder
	for _, i := range v {
		b.WriteString(strconv.Itoa(i) + " ")
	}
	return b.String()
}

func TestResolveHeadersRedacted(t *testing.T) {
	logs := captureDiag(t)
	t.Setenv(otlpEnv("", "HEADERS"), "x-parity-a=1,broken-secret-value")
	t.Setenv(otlpEnv(signalLogs, "HEADERS"), "x-logs=2")
	o := &Options{}
	if h := o.resolveHeaders(signalTraces); h["x-parity-a"] != "1" || len(h) != 1 {
		t.Errorf("traces headers = %v", h)
	}
	if h := o.resolveHeaders(signalLogs); h["x-logs"] != "2" || len(h) != 1 {
		t.Errorf("per-signal headers replace generic: %v", h)
	}
	if o.resolveHeaders(signalMetrics) == nil {
		t.Error("metrics headers nil with generic set")
	}
	out := logs.String()
	if !strings.Contains(out, "item #2") || strings.Contains(out, "secret") || strings.Contains(out, "x-parity-a=1") {
		t.Errorf("warning must name the position only: %q", out)
	}
	if n := countLines(out, "malformed header"); n != 1 {
		t.Errorf("warned %d times, want once", n)
	}
	os.Unsetenv(otlpEnv("", "HEADERS"))
	os.Unsetenv(otlpEnv(signalLogs, "HEADERS"))
	if (&Options{}).resolveHeaders(signalTraces) != nil {
		t.Error("headers non-nil with no variable set")
	}
}

func TestResolveExport_EnvFallbacks(t *testing.T) {
	logs := captureDiag(t)
	// Invalid per-signal endpoint with a valid generic one → generic.
	t.Setenv(EnvCollectorEndpoint, "http://generic:4317")
	t.Setenv(otlpEnv(signalTraces, "ENDPOINT"), "http:///v1/traces") // parseable, empty host
	// Unsupported scheme → not configured (no fallback).
	t.Setenv(otlpEnv(signalLogs, "ENDPOINT"), "unix:///tmp/otlp.sock")
	o := newOptions(WithServiceName("svc"))
	if e := o.export(signalTraces); !e.configured || e.endpointURL != "http://generic:4317" {
		t.Errorf("traces = %+v, want the generic URL passed explicitly (round-2 H2)", e)
	}
	if o.export(signalLogs).configured {
		t.Error("logs configured with an unsupported scheme")
	}
	if !o.export(signalMetrics).configured {
		t.Error("metrics not configured")
	}
	for _, v := range []string{otlpEnv(signalTraces, "ENDPOINT"), otlpEnv(signalLogs, "ENDPOINT")} {
		if !strings.Contains(logs.String(), v) {
			t.Errorf("no warning naming %s", v)
		}
	}
}

func TestValidate_CodeRanges(t *testing.T) {
	for name, opt := range map[string]Option{
		"WithSampleRatio(NaN)":         WithSampleRatio(math.NaN()),
		"WithSampleRatio(1.5)":         WithSampleRatio(1.5),
		"WithSampleRatio(-0.1)":        WithSampleRatio(-0.1),
		"WithPrometheusMetricsPort":    WithPrometheusMetricsPort(70000),
		"WithPrometheusMetricsPort(-)": WithPrometheusMetricsPort(-1),
		"WithDisabledSignals":          WithDisabledSignals([]string{"traces", "profiles"}),
	} {
		err := newOptions(opt).validate()
		opt := strings.SplitN(name, "(", 2)[0]
		if err == nil || !strings.Contains(err.Error(), opt) {
			t.Errorf("%s: validate() = %v, want error naming %s", name, err, opt)
		}
	}
	if err := newOptions(WithSampleRatio(0), WithPrometheusMetricsPort(0), WithDisabledSignals([]string{"Metrics"})).validate(); err != nil {
		t.Errorf("valid boundary values rejected: %v", err)
	}
	logs := captureDiag(t)
	t.Setenv(EnvDisabledSignals, "traces,profiles")
	if o := newOptions(); len(o.DisabledSignals) != 1 || !strings.Contains(logs.String(), "profiles") {
		t.Errorf("env: %v %q", o.DisabledSignals, logs.String())
	}
}

func TestResolveExport_EndpointWhitespaceAndSchemelessPerSignal(t *testing.T) {
	// Round-2 HIGH: a trailing newline made the log SDK drop every record; the
	// trimmed URL is now passed explicitly.
	t.Setenv(EnvCollectorEndpoint, "  http://c:4318/otlp\n")
	o := newOptions(WithServiceName("svc"))
	for _, s := range allSignals {
		if got := o.export(s).endpointURL; got != "http://c:4318/otlp/v1/"+s {
			t.Errorf("%s endpointURL = %q", s, got)
		}
	}
	// Round-2 HIGH: schemeless per-signal + per-signal INSECURE=false beats
	// generic INSECURE=true → https (the SDK can't parse the raw value).
	t.Setenv(EnvCollectorEndpoint, "")
	t.Setenv(otlpEnv(signalMetrics, "ENDPOINT"), "c:14317")
	t.Setenv(EnvInsecure, "true")
	t.Setenv(otlpEnv(signalMetrics, "INSECURE"), "false")
	if got := newOptions(WithServiceName("svc")).export(signalMetrics).endpointURL; got != "https://c:14317" {
		t.Errorf("metrics endpointURL = %q, want https://c:14317", got)
	}
}

func TestResolveTimeoutSyntaxAndClamp(t *testing.T) {
	logs := captureDiag(t)
	o := &Options{}
	for raw, want := range map[string]time.Duration{
		"0x1p10":       defaultOtlpTimeout, // hex rejected → default
		".5":           defaultOtlpTimeout,
		"5.":           defaultOtlpTimeout,
		"1e3":          defaultOtlpTimeout,
		"1_000":        defaultOtlpTimeout,
		"inf":          defaultOtlpTimeout,
		"nan":          defaultOtlpTimeout,
		"1e30":         defaultOtlpTimeout, // exponent rejected (no overflow)
		"+5":           defaultOtlpTimeout,
		"0.5":          time.Millisecond,
		"999999999999": 24 * time.Hour,
		"1500":         1500 * time.Millisecond,
	} {
		t.Setenv(otlpEnv("", "TIMEOUT"), raw)
		if got := o.resolveTimeout(signalTraces); got != want {
			t.Errorf("%q → %v, want %v", raw, got, want)
		}
	}
	for _, w := range []string{"0x1p10", "clamped"} {
		if !strings.Contains(logs.String(), w) {
			t.Errorf("no warning mentioning %q", w)
		}
	}
}

func TestResolveCompressionPerSignalFallsBack(t *testing.T) {
	captureDiag(t)
	t.Setenv(otlpEnv(signalTraces, "COMPRESSION"), "deflate")
	t.Setenv(otlpEnv("", "COMPRESSION"), "none")
	if c := (&Options{}).resolveCompression(signalTraces); c != CompressionNone {
		t.Errorf("deflate + generic none = %q, want none", c)
	}
}

func TestResolveHeadersPerSignalFallsBack(t *testing.T) {
	captureDiag(t)
	t.Setenv(otlpEnv(signalTraces, "HEADERS"), "broken")
	t.Setenv(otlpEnv("", "HEADERS"), "x-g=1")
	if h := (&Options{}).resolveHeaders(signalTraces); h["x-g"] != "1" {
		t.Errorf("per-signal with no valid item must fall back to generic: %v", h)
	}
	os.Unsetenv(otlpEnv("", "HEADERS"))
	if h := (&Options{}).resolveHeaders(signalTraces); h == nil || len(h) != 0 {
		t.Errorf("no valid item anywhere: want an empty (non-nil) map, got %#v", h)
	}
}

func TestParseHeadersLiteralAndControl(t *testing.T) {
	h, skipped, literal := parseHeaders("a=a%20b%zz,b=%FF,c=x%0D%0Ay,d=ok%20v,e=x\ty")
	if h["a"] != "a%20b%zz" || h["b"] != "%FF" || h["d"] != "ok v" {
		t.Errorf("headers = %q (want whole-literal a and b, decoded d)", h)
	}
	if _, ok := h["c"]; ok {
		t.Error("decoded CR/LF value kept")
	}
	if _, ok := h["e"]; ok {
		t.Error("raw control character value kept")
	}
	if fmtInts(literal) != "1 2 " || fmtInts(skipped) != "3 5 " {
		t.Errorf("literal=%v skipped=%v", literal, skipped)
	}
}

func TestResolveEndpointControlCharAndUnsupportedText(t *testing.T) {
	res, err := resolveEndpoint(endpointInput{raw: "http://c:43\t17", variable: EnvCollectorEndpoint, signal: "traces", protocol: ProtocolGRPC})
	if !errors.Is(err, errEndpointInvalid) || len(res.warnings) != 1 || res.warnings[0].value != "<unparseable URL>" {
		t.Errorf("control char: err=%v warnings=%+v", err, res.warnings)
	}
	res, err = resolveEndpoint(endpointInput{raw: "unix:///s", variable: EnvCollectorEndpoint, signal: "traces", protocol: ProtocolGRPC})
	if !errors.Is(err, errEndpointUnsupported) || res.warnings[0].using != "no OTLP exporter for this signal" {
		t.Errorf("unsupported: err=%v warnings=%+v", err, res.warnings)
	}
}

// P2-c: the periodic reader's own 30 s timeout must not cap a resolved 60 s
// export timeout (design D1 "Effective export deadline"). A gRPC server
// records the deadline the metric export arrives with.
func TestPeriodicReaderUsesResolvedTimeout(t *testing.T) {
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	got := make(chan time.Duration, 8)
	srv := grpc.NewServer(grpc.UnknownServiceHandler(func(_ any, s grpc.ServerStream) error {
		if dl, ok := s.Context().Deadline(); ok {
			select {
			case got <- time.Until(dl):
			default:
			}
		}
		var m emptypb.Empty
		_ = s.RecvMsg(&m)
		return s.SendMsg(&emptypb.Empty{})
	}))
	go func() { _ = srv.Serve(lis) }()
	defer srv.Stop()

	o := newOptions(WithServiceName("svc"), WithEndpoint("http://"+lis.Addr().String()),
		WithExportTimeout(60_000), WithExportInterval(50), WithMetricExporters("otlp"))
	mp, _, _, err := configureMetrics(context.Background(), resource.Default(), o)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = mp.Shutdown(ctx)
	}()
	c, _ := mp.Meter("p").Int64Counter("c")
	c.Add(context.Background(), 1)
	select {
	case d := <-got:
		if d < 45*time.Second {
			t.Errorf("export deadline %v, want ≈60 s (the reader's 30 s default capped it)", d)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("no metric export received")
	}
}

func TestOpaqueSchemeAndTrimSet(t *testing.T) {
	for _, raw := range []string{"collector:/v1/traces", "unix:/tmp/x", "ftp:/x"} {
		if _, err := resolveEndpoint(endpointInput{raw: raw, variable: EnvCollectorEndpoint, signal: "traces", protocol: ProtocolGRPC}); !errors.Is(err, errEndpointUnsupported) {
			t.Errorf("%q: err = %v, want unsupported scheme", raw, err)
		}
	}
	if got := trimWS("\v\f \t\nx\r"); got != "x" {
		t.Errorf("trimWS = %q", got)
	}
	if got := trimWS(" x\x1c"); got != " x\x1c" {
		t.Errorf("trimWS must not trim Unicode/\\x1c whitespace: %q", got)
	}
	if got := redactURL("collector:4317\n/x"); got != "<unparseable URL>" {
		t.Errorf("redactURL with control char = %q", got)
	}
	_, skipped, literal := parseHeaders("a=%zz\x01b")
	if len(literal) != 0 || len(skipped) != 1 {
		t.Errorf("literal+control: literal=%v skipped=%v, want only skipped", literal, skipped)
	}
}

// D1 A13 ruling: a code value equal to the helper default still wins over the
// environment ("was it set" is tracked by a flag, never by value).
func TestCodeDefaultValuesBeatEnv(t *testing.T) {
	t.Setenv(EnvMetricsPort, "9999")
	t.Setenv(EnvDisabledMetrics, "http.*")
	o := newOptions(WithServiceName("svc"), WithPrometheusMetricsPort(9464), WithDisabledMetrics([]string{}))
	if o.PrometheusMetricsPort != 9464 {
		t.Errorf("PrometheusMetricsPort = %d, want the code 9464 over %s", o.PrometheusMetricsPort, EnvMetricsPort)
	}
	if len(o.DisabledMetrics) != 0 {
		t.Errorf("DisabledMetrics = %v, want the code empty list over %s", o.DisabledMetrics, EnvDisabledMetrics)
	}
	// nil = "not set" (env), like Python's None; only a non-nil empty slice overrides.
	if o = newOptions(WithServiceName("svc"), WithDisabledMetrics(nil)); len(o.DisabledMetrics) != 1 {
		t.Errorf("WithDisabledMetrics(nil) = %v, want %s applied", o.DisabledMetrics, EnvDisabledMetrics)
	}
	t.Setenv(EnvDisabledSignals, "traces")
	if o = newOptions(WithServiceName("svc"), WithDisabledSignals([]string{})); !o.IsSignalEnabled("traces") {
		t.Errorf("WithDisabledSignals([]string{}) did not override %s", EnvDisabledSignals)
	}
	if o = newOptions(WithServiceName("svc"), WithDisabledSignals(nil)); o.IsSignalEnabled("traces") {
		t.Errorf("WithDisabledSignals(nil) = %v, want %s applied", o.DisabledSignals, EnvDisabledSignals)
	}
	t.Setenv(EnvDisabledSignals, "")
	if o = newOptions(WithServiceName("svc"), WithoutMetricsListener()); o.PrometheusMetricsPort != 0 {
		t.Errorf("WithoutMetricsListener: port = %d, want 0 over %s", o.PrometheusMetricsPort, EnvMetricsPort)
	}
	// Without code the env still applies.
	o = newOptions(WithServiceName("svc"))
	if o.PrometheusMetricsPort != 9999 || len(o.DisabledMetrics) != 1 || o.DisabledMetrics[0] != "http.*" {
		t.Errorf("env not applied without code: port=%d disabled=%v", o.PrometheusMetricsPort, o.DisabledMetrics)
	}
}
