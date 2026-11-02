package otelhelper

// Contract task A21 (design D1 rows of 2026-11-02 "Disabled-metric patterns",
// "Metrics port grammar", "Metrics listener"); the parity fixture covers the
// wire side (metrics.disabled_metrics_*, metrics.port_env_*, metrics.listener_*).

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"
)

func TestA21GlobMatch(t *testing.T) {
	tests := []struct {
		pattern, name string
		want          bool
	}{
		{"parity.*", "parity.requests", true},
		{"par*load", "parity.payload", true},
		{"*", "a.b/c", true},
		{"a/*", "a/b/c", true},
		{"parity.r?quests", "parity.requests", true},
		{"parity.payloa??", "parity.payload", false},
		{"parity.[pr]*", "parity.payload", false},
		{"parity.[pr]*", "parity.[pr]x", true},
		{"PARITY.*", "parity.requests", false},
		{"a*b*c", "axxbyyc", true},
		{"a*b*c", "axxbyy", false},
		{"**", "", true},
		{"?", "", false},
		{`a\*`, `a\bc`, true},
		{"", "", true},
		{"", "x", false},
		{"é?", "éx", true},
	}
	for _, tt := range tests {
		t.Run(tt.pattern+"~"+tt.name, func(t *testing.T) {
			if got := globMatch(tt.pattern, tt.name); got != tt.want {
				t.Fatalf("globMatch(%q, %q) = %v, want %v", tt.pattern, tt.name, got, tt.want)
			}
		})
	}
}

func TestA21TrimPatterns(t *testing.T) {
	got := trimPatterns([]string{" a.* ", "", "\t", "b"})
	if strings.Join(got, ",") != "a.*,b" {
		t.Fatalf("trimPatterns = %q", got)
	}
}

func TestA21ParseMetricsPort(t *testing.T) {
	tests := []struct {
		in   string
		want int
		ok   bool
	}{
		{"9100", 9100, true},
		{"+9100", 9100, true},
		{"0", 0, true},
		{"+0", 0, true},
		{"65535", 65535, true},
		{strings.Repeat("0", 40) + "9100", 9100, true},
		{"65536", 0, false},
		{"-0", 0, false},
		{"-1", 0, false},
		{"9_464", 0, false},
		{"++1", 0, false},
		{"+", 0, false},
		{"0x10", 0, false},
		{"９４６４", 0, false},
		{strings.Repeat("1", 40), 0, false},
	}
	for _, tt := range tests {
		got, ok := parseMetricsPort(tt.in)
		if ok != tt.ok || (ok && got != tt.want) {
			t.Errorf("parseMetricsPort(%q) = %d, %v; want %d, %v", tt.in, got, ok, tt.want, tt.ok)
		}
	}
}

func TestA21ListenerMethodsAndPaths(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	_ = ln.Close()
	for _, kv := range os.Environ() {
		if k, _, _ := strings.Cut(kv, "="); strings.HasPrefix(k, "OTEL_") {
			t.Setenv(k, "")
			_ = os.Unsetenv(k)
		}
	}
	t.Setenv("OTEL_SERVICE_NAME", "a21")
	t.Setenv(EnvMetricsPort, fmt.Sprint(port))
	shutdown, err := Setup(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = shutdown(context.Background()) }()
	client := &http.Client{Timeout: 5 * time.Second}
	for _, tc := range []struct {
		method, path string
		want         int
		allow        string
	}{
		{http.MethodGet, "/metrics", 200, ""},
		{http.MethodHead, "/metrics", 200, ""},
		{http.MethodPost, "/metrics", 405, "GET, HEAD"},
		{"PROPFIND", "/metrics", 405, "GET, HEAD"},
		{http.MethodGet, "/other", 404, ""},
		{http.MethodGet, "/metrics/", 404, ""},
	} {
		req, err := http.NewRequest(tc.method, fmt.Sprintf("http://127.0.0.1:%d%s", port, tc.path), nil)
		if err != nil {
			t.Fatal(err)
		}
		resp, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != tc.want {
			t.Errorf("%s %s = %d, want %d", tc.method, tc.path, resp.StatusCode, tc.want)
		}
		if got := resp.Header.Get("Allow"); got != tc.allow {
			t.Errorf("%s %s Allow = %q, want %q", tc.method, tc.path, got, tc.allow)
		}
	}
}
