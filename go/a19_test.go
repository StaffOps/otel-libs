package otelhelper

// Tests for the A19 contract rulings (design D1 rows dated 2026-10-29):
// OTEL_HELPER_DISABLED_SIGNALS vs the standard variables, the "logging"
// alias in code, the metric export interval grammar and bound,
// OTEL_CONFIG_FILE.

import (
	"strconv"
	"strings"
	"testing"
)

func TestDisabledSignals_StandardVariableWins(t *testing.T) {
	logs := captureDiag(t)
	t.Setenv(EnvDisabledSignals, "traces,metrics,logs,profiles,profiles,PROFILES")
	t.Setenv(EnvTracesExporter, "otlp")
	t.Setenv(EnvLogsExporter, " ")
	o := newOptions(WithServiceName("svc"))
	if !o.IsSignalEnabled("traces") {
		t.Error("traces disabled although OTEL_TRACES_EXPORTER is set")
	}
	if o.IsSignalEnabled("metrics") || o.IsSignalEnabled("logs") {
		t.Errorf("DisabledSignals = %v, want metrics and logs (a blank standard variable is unset)", o.DisabledSignals)
	}
	if strings.Count(logs.String(), "value=profiles") != 1 {
		t.Errorf("want one warning for the unknown entry, log:\n%s", logs.String())
	}
	// Code wins over the variable.
	if o := newOptions(WithServiceName("svc"), WithDisabledSignals([]string{})); len(o.DisabledSignals) != 0 {
		t.Errorf("WithDisabledSignals([]) = %v", o.DisabledSignals)
	}
}

func TestDisabledSignals_StandardVariableSetOnlyWithAValidEntry(t *testing.T) {
	captureDiag(t)
	t.Setenv(EnvDisabledSignals, "traces,metrics,logs")
	t.Setenv(EnvTracesExporter, "bogus")
	t.Setenv(EnvLogsExporter, " , ,")
	t.Setenv(EnvMetricsExporter, "none")
	o := newOptions(WithServiceName("svc"))
	if o.IsSignalEnabled("traces") || o.IsSignalEnabled("logs") {
		t.Errorf("only-unknown / only-separator standard values must count as unset: %v", o.DisabledSignals)
	}
	if !o.IsSignalEnabled("metrics") {
		t.Error(`OTEL_METRICS_EXPORTER=none is set: it wins (and disables metrics itself)`)
	}
}

func TestDisabledSignals_CodeMetricExportersWin(t *testing.T) {
	t.Setenv(EnvDisabledSignals, "metrics")
	if o := newOptions(WithServiceName("svc"), WithMetricExporters("console")); !o.IsSignalEnabled("metrics") {
		t.Errorf("WithMetricExporters must win over %s: %v", EnvDisabledSignals, o.DisabledSignals)
	}
}

func TestWithDisabledSignals_TrimmedLoweredDeduplicated(t *testing.T) {
	o := newOptions(WithServiceName("svc"), WithDisabledSignals([]string{" Traces ", "traces", "LOGS"}))
	if err := o.validate(); err != nil {
		t.Fatal(err)
	}
	if strings.Join(o.DisabledSignals, ",") != "traces,logs" || o.IsSignalEnabled("traces") {
		t.Errorf("DisabledSignals = %v", o.DisabledSignals)
	}
	if err := newOptions(WithServiceName("svc"), WithDisabledSignals([]string{" "})).validate(); err == nil {
		t.Error("a blank signal must fail validate")
	}
}

func TestTruncateValue(t *testing.T) {
	long := strings.Repeat("é", maxEchoedValue+5)
	if got := truncateValue(long); got != strings.Repeat("é", maxEchoedValue)+"…" {
		t.Errorf("truncateValue = %q", got)
	}
	if got := truncateValue("short"); got != "short" {
		t.Errorf("truncateValue(short) = %q", got)
	}
}

func TestParseExportInterval_Grammar(t *testing.T) {
	for _, tc := range []struct {
		in   string
		want int
		warn string
	}{
		{"5000", 5000, ""},
		{"+5000", 5000, ""},
		{"007", 7, ""},
		{"2147483647", 2147483647, ""},
		{"2147483648", maxExportIntervalMs, "clamped"},
		{"99999999999999999999999", maxExportIntervalMs, "clamped"},
		{"0", defaultExportIntervalMs, "invalid"},
		{"+", defaultExportIntervalMs, "invalid"},
		{"-5", defaultExportIntervalMs, "invalid"},
		{"5_000", defaultExportIntervalMs, "invalid"},
		{"5000.0", defaultExportIntervalMs, "invalid"},
		{"٥٠٠٠", defaultExportIntervalMs, "invalid"},
	} {
		logs := captureDiag(t)
		if got := parseExportInterval(tc.in); got != tc.want {
			t.Errorf("parseExportInterval(%q) = %d, want %d", tc.in, got, tc.want)
		}
		out := logs.String()
		if (tc.warn == "") != (out == "") || (tc.warn != "" && !strings.Contains(out, tc.warn)) {
			t.Errorf("parseExportInterval(%q): want warning %q, log:\n%s", tc.in, tc.warn, out)
		}
	}
}

func TestWithExportInterval_Bound(t *testing.T) {
	cases := []int{0, -1}
	if strconv.IntSize == 64 {
		v := int64(maxExportIntervalMs) // built at run time: a constant max+1 would not compile on 32-bit
		cases = append(cases, int(v+1))
	}
	for _, ms := range cases {
		o := newOptions(WithServiceName("svc"), WithExportInterval(ms))
		if err := o.validate(); err == nil || !strings.Contains(err.Error(), "2147483647") {
			t.Errorf("WithExportInterval(%d): validate() = %v", ms, err)
		}
	}
	if err := newOptions(WithServiceName("svc"), WithExportInterval(maxExportIntervalMs)).validate(); err != nil {
		t.Errorf("WithExportInterval(max): %v", err)
	}
}

func TestConfigFile_WarnsOncePerProcessNamingTheVariable(t *testing.T) {
	logs := captureDiag(t)
	t.Setenv(EnvConfigFile, "/etc/secret-path/otel.yaml")
	t.Setenv(EnvExperimentalConfigFile, "/etc/secret-path/otel.yaml")
	newOptions(WithServiceName("svc"))
	newOptions(WithServiceName("svc"))
	out := logs.String()
	for _, v := range []string{EnvConfigFile, EnvExperimentalConfigFile} {
		if strings.Count(out, "msg=\"otelhelper: "+v+" is not supported") != 1 {
			t.Errorf("want one warning for %s, log:\n%s", v, out)
		}
	}
	if strings.Contains(out, "secret-path") {
		t.Errorf("path printed:\n%s", out)
	}
}
