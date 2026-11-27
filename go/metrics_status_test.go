package otelhelper

// Mounted /metrics handler status (design "Mounted /metrics handler status").

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func scrape(t *testing.T) (int, string) {
	t.Helper()
	rr := httptest.NewRecorder()
	MetricsHandler().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	return rr.Code, rr.Body.String()
}

func TestMetricsHandlerStatus(t *testing.T) {
	cases := []struct {
		name string
		env  map[string]string
		opts []Option
		code int
		body bool // non-empty body expected
	}{
		{"prometheus active", nil, []Option{WithMetricExporters("prometheus"), WithoutMetricsListener()}, http.StatusOK, true},
		{"otlp only", nil, []Option{WithEndpoint(deadEndpoint), WithMetricExporters("otlp")}, http.StatusOK, false},
		{"exporter none", map[string]string{EnvMetricsExporter: "none"}, nil, http.StatusOK, false},
		{"sdk disabled", map[string]string{EnvSDKDisabled: "true"}, nil, http.StatusOK, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			lifecycleTest(t)
			for k, v := range c.env {
				t.Setenv(k, v)
			}
			if code, _ := scrape(t); code != http.StatusServiceUnavailable {
				t.Errorf("before Setup: %d, want 503", code)
			}
			s, err := Setup(context.Background(), append([]Option{WithServiceName("status")}, c.opts...)...)
			if err != nil {
				t.Fatal(err)
			}
			code, body := scrape(t)
			if code != c.code || (body != "") != c.body {
				t.Errorf("after Setup: %d body=%q, want %d non-empty=%v", code, truncate(body), c.code, c.body)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
			defer cancel()
			if err := s(ctx); err != nil {
				t.Logf("shutdown: %v", err)
			}
			if code, _ := scrape(t); code != http.StatusServiceUnavailable {
				t.Errorf("after Shutdown: %d, want 503", code)
			}
		})
	}
}
