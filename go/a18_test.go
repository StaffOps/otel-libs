package otelhelper

// Tests for the A18 contract rulings (design D1 rows dated 2026-10-29): TLS
// files, export deadline, code timeout bound, header bytes, value-length
// limits.

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestIgnoredTimeoutVariables_WarnAndKeepOTLPDeadline(t *testing.T) {
	logs := captureDiag(t)
	t.Setenv("OTEL_BSP_EXPORT_TIMEOUT", "1")
	t.Setenv("OTEL_METRIC_EXPORT_TIMEOUT", "1")
	o := newOptions(WithServiceName("svc"), WithEndpoint("http://c:4317"))
	out := logs.String()
	for _, v := range []string{"OTEL_BSP_EXPORT_TIMEOUT", "OTEL_METRIC_EXPORT_TIMEOUT"} {
		if strings.Count(out, "variable="+v) != 1 || !strings.Contains(out, "not honored") {
			t.Errorf("want one 'not honored' warning for %s, log:\n%s", v, out)
		}
	}
	if strings.Contains(out, "OTEL_BLRP_EXPORT_TIMEOUT") {
		t.Errorf("unset variable warned:\n%s", out)
	}
	// The resolved OTLP timeout stays the deadline.
	if d := o.export(signalTraces).timeout; d != defaultOtlpTimeout {
		t.Errorf("timeout = %v, want 10s", d)
	}
}

func TestWithExportTimeout_Above24hFailsSetup(t *testing.T) {
	for _, ms := range []int{86_400_001, -1} {
		o := newOptions(WithServiceName("svc"), WithExportTimeout(ms))
		if err := o.validate(); err == nil || !strings.Contains(err.Error(), "86400000") {
			t.Errorf("WithExportTimeout(%d): err = %v, want the [1, 86400000] bound", ms, err)
		}
	}
	o := newOptions(WithServiceName("svc"), WithExportTimeout(86_400_000))
	if err := o.validate(); err != nil {
		t.Errorf("24 h must be accepted: %v", err)
	}
	if d := o.resolveTimeout(signalLogs); d != 24*time.Hour {
		t.Errorf("timeout = %v", d)
	}
}

func TestParseHeaders_NonPrintableASCIIValueSkipped(t *testing.T) {
	h, skipped, _ := parseHeaders("x-a=%C3%A9,x-b=ok,x-c=\u00e9,x-d=%7F,x-bin=abc")
	if h["x-b"] != "ok" || h["x-bin"] != "abc" || len(h) != 2 {
		t.Errorf("headers = %v", h)
	}
	if fmtInts(skipped) != fmtInts([]int{1, 3, 4}) {
		t.Errorf("skipped = %v, want [1 3 4]", skipped)
	}
}

func writeFile(t *testing.T, dir, name, content string) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestErrnoName_IsPathFree(t *testing.T) {
	for err, want := range map[error]string{fs.ErrNotExist: "ENOENT", fs.ErrPermission: "EACCES", errors.New("x"): "read error"} {
		if got := errnoName(err); got != want {
			t.Errorf("errnoName(%v) = %q, want %q", err, got, want)
		}
	}
}

func TestCheckTLSFiles_FallbackAndServerAuth(t *testing.T) {
	dir := t.TempDir()
	missing := filepath.Join(dir, "missing.pem")
	garbage := writeFile(t, dir, "garbage.pem", "s3cret-not-pem")
	ca := os.Getenv("FAKEOTLP_CA")
	clear := func() {
		for _, s := range []string{"CERTIFICATE", "CLIENT_KEY", "CLIENT_CERTIFICATE"} {
			t.Setenv(otlpEnv("", s), "")
			t.Setenv(otlpEnv(signalTraces, s), "")
		}
	}
	type tc struct {
		name string
		env  map[string]string
		want []string // substrings, in order of appearance
		none bool
	}
	cases := []tc{
		{"unreadable CA uses the system roots", map[string]string{"OTEL_EXPORTER_OTLP_CERTIFICATE": missing},
			[]string{"TLS file cannot be read", "variable=OTEL_EXPORTER_OTLP_CERTIFICATE value=ENOENT", `using="the system roots"`}, false},
		{"non-PEM per-signal CA falls back to generic", map[string]string{"OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE": garbage, "OTEL_EXPORTER_OTLP_CERTIFICATE": missing},
			[]string{"variable=OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE", `using="the generic variable"`, "variable=OTEL_EXPORTER_OTLP_CERTIFICATE"}, false},
		{"incomplete pair uses server auth", map[string]string{"OTEL_EXPORTER_OTLP_CLIENT_KEY": garbage},
			[]string{"must both be set and readable", "variable=OTEL_EXPORTER_OTLP_CLIENT_KEY", `using="server authentication"`}, false},
		{"rejected pair uses server auth", map[string]string{"OTEL_EXPORTER_OTLP_CLIENT_KEY": garbage, "OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE": garbage},
			[]string{"rejected by TLS", "variable=OTEL_EXPORTER_OTLP_CLIENT_KEY,OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE"}, false},
		{"no TLS variable leaves the SDK default", map[string]string{}, nil, true},
	}
	if ca != "" {
		cases = append(cases, tc{"good CA is used", map[string]string{"OTEL_EXPORTER_OTLP_CERTIFICATE": ca}, nil, true})
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			clear()
			for k, v := range c.env {
				t.Setenv(k, v)
			}
			logs := captureDiag(t)
			o := &Options{}
			cfg := o.checkTLSFiles(signalTraces)
			if (cfg == nil) != (len(c.env) == 0) {
				t.Errorf("config = %v, want nil only when no TLS variable is set", cfg)
			}
			if cfg != nil && len(cfg.Certificates) != 0 {
				t.Errorf("an invalid pair must not be used")
			}
			if cfg != nil && (cfg.RootCAs != nil) != (c.name == "good CA is used") {
				t.Errorf("RootCAs set = %v", cfg.RootCAs != nil)
			}
			out := logs.String()
			if c.none && out != "" {
				t.Errorf("unexpected warnings:\n%s", out)
			}
			rest := out
			for _, w := range c.want {
				i := strings.Index(rest, w)
				if i < 0 {
					t.Fatalf("missing %q in:\n%s", w, out)
				}
				rest = rest[i+len(w):]
			}
			if strings.Contains(out, "s3cret") || strings.Contains(out, dir) {
				t.Errorf("file content or path printed:\n%s", out)
			}
		})
	}
}

func TestValueLengthLimit_ZeroUsesNextLevel(t *testing.T) {
	for _, k := range []string{envAttrValueLength, envSpanValueLength, envLogRecValueLength} {
		t.Setenv(k, "")
	}
	o := &Options{}
	if _, ok := o.spanLimits(); ok {
		t.Error("no 0 set: SDK limits must apply unchanged")
	}
	if _, ok := o.logValueLength(); ok {
		t.Error("no 0 set: SDK log limit must apply unchanged")
	}
	logs := captureDiag(t)
	t.Setenv(envAttrValueLength, "0")
	l, ok := o.spanLimits()
	if !ok || l.AttributeValueLengthLimit != noValueLengthLimit {
		t.Errorf("generic 0: limit = %d ok=%v, want no limit", l.AttributeValueLengthLimit, ok)
	}
	t.Setenv(envSpanValueLength, "0")
	t.Setenv(envAttrValueLength, "50")
	if l, ok = o.spanLimits(); !ok || l.AttributeValueLengthLimit != 50 {
		t.Errorf("span 0, generic 50: limit = %d ok=%v", l.AttributeValueLengthLimit, ok)
	}
	t.Setenv(envSpanValueLength, "0")
	t.Setenv(envAttrValueLength, "")
	if l, ok = o.spanLimits(); !ok || l.AttributeValueLengthLimit != noValueLengthLimit {
		t.Errorf("span 0 alone: limit = %d", l.AttributeValueLengthLimit)
	}
	t.Setenv(envLogRecValueLength, "0")
	if n, ok := o.logValueLength(); !ok || n != noValueLengthLimit {
		t.Errorf("log 0: %d %v", n, ok)
	}
	t.Setenv(envAttrValueLength, "7")
	if n, ok := o.logValueLength(); !ok || n != 7 {
		t.Errorf("log 0, generic 7: %d %v", n, ok)
	}
	t.Setenv(envAttrValueLength, "abc")
	if n := o.envValueLength(envAttrValueLength); n != valueLengthLimitUnset {
		t.Errorf("garbage = %d", n)
	}
	out := logs.String()
	for _, v := range []string{envAttrValueLength, envSpanValueLength, envLogRecValueLength} {
		if strings.Count(out, "variable="+v+" ") != 1 {
			t.Errorf("want exactly one warning for %s:\n%s", v, out)
		}
	}
}

func TestLogValueLength_ZeroSpelledDoubleZeroIsInvalid(t *testing.T) {
	logs := captureDiag(t)
	t.Setenv(envAttrValueLength, "")
	t.Setenv(envLogRecValueLength, "00")
	o := &Options{}
	if n, ok := o.logValueLength(); !ok || n != noValueLengthLimit {
		t.Errorf(`"00" = %d ok=%v, want no limit`, n, ok)
	}
	if !strings.Contains(logs.String(), "variable="+envLogRecValueLength) {
		t.Errorf("no warning:\n%s", logs.String())
	}
	t.Setenv(envLogRecValueLength, "abc")
	if _, ok := o.logValueLength(); ok {
		t.Error("garbage must be left to the SDK")
	}
}

func TestBatchSizeOverride_AboveQueueUsesQueue(t *testing.T) {
	for _, prefix := range []string{"OTEL_BSP", "OTEL_BLRP"} {
		t.Run(prefix, func(t *testing.T) {
			logs := captureDiag(t)
			o := &Options{}
			t.Setenv(prefix+"_MAX_QUEUE_SIZE", "")
			t.Setenv(prefix+"_MAX_EXPORT_BATCH_SIZE", "")
			if _, ok := o.batchSizeOverride(prefix); ok {
				t.Error("defaults: no override")
			}
			t.Setenv(prefix+"_MAX_QUEUE_SIZE", "10")
			t.Setenv(prefix+"_MAX_EXPORT_BATCH_SIZE", "50")
			if n, ok := o.batchSizeOverride(prefix); !ok || n != 10 {
				t.Errorf("override = %d %v, want 10", n, ok)
			}
			o.batchSizeOverride(prefix)
			if c := strings.Count(logs.String(), prefix+"_MAX_EXPORT_BATCH_SIZE,"+prefix+"_MAX_QUEUE_SIZE"); c != 1 {
				t.Errorf("want one warning naming both variables, got %d:\n%s", c, logs.String())
			}
			t.Setenv(prefix+"_MAX_QUEUE_SIZE", "")
			t.Setenv(prefix+"_MAX_EXPORT_BATCH_SIZE", "5000")
			if n, ok := o.batchSizeOverride(prefix); !ok || n != 2048 {
				t.Errorf("default queue: %d %v", n, ok)
			}
			t.Setenv(prefix+"_MAX_QUEUE_SIZE", "many")
			if _, ok := o.batchSizeOverride(prefix); ok {
				t.Error("garbage must be left to the SDK")
			}
		})
	}
}
