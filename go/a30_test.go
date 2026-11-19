package otelhelper

// A30 (owner ruling Q1 (b), 2026-11-06; contract D1 "HTTP opt-in metrics"):
// only the two HTTP durations by default; the body sizes behind
// OTEL_HELPER_HTTP_OPT_IN_METRICS / WithHTTPOptInMetrics (otelhttp v0.72 has
// no active-requests instrument).

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/instrumentation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
)

func TestHTTPOptInMetrics(t *testing.T) {
	for _, c := range []struct {
		name string
		env  string
		opts []Option
		on   bool
	}{
		{"default", "", nil, false},
		{"env true", "TRUE", nil, true},
		{"env invalid", "yes", nil, false},
		{"env false", "false", nil, false},
		{"code wins over env", "true", []Option{WithHTTPOptInMetrics(false)}, false},
		{"code on", "", []Option{WithHTTPOptInMetrics(true)}, true},
	} {
		t.Run(c.name, func(t *testing.T) {
			lifecycleTest(t)
			if c.env != "" {
				t.Setenv(EnvHTTPOptInMetrics, c.env)
			}
			opts := append([]Option{WithServiceName("a30"), WithMetricExporters("prometheus"), WithoutMetricsListener()}, c.opts...)
			s, err := Setup(context.Background(), opts...)
			if err != nil {
				t.Fatal(err)
			}
			defer func() {
				ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
				defer cancel()
				_ = s(ctx)
			}()
			if got := installedOptions().HTTPOptInMetrics; got != c.on {
				t.Errorf("HTTPOptInMetrics = %v, want %v", got, c.on)
			}
			h := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok")) }), "a30")
			h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/orders", strings.NewReader("body")))
			_, body := scrape(t)
			if !strings.Contains(body, "http_server_request_duration_seconds") {
				t.Fatalf("no duration histogram in %q", truncate(body))
			}
			for _, name := range []string{"http_server_request_body_size", "http_server_response_body_size"} {
				if strings.Contains(body, name) != c.on {
					t.Errorf("%s present = %v, want %v", name, !c.on, c.on)
				}
			}
		})
	}
}

func TestHTTPOptInMetricsView(t *testing.T) {
	for name, drop := range map[string]bool{
		"http.server.active_requests": true, "http.server.request.body.size": true, "http.client.response.body.size": true,
		"http.server.request.size": true, "http.server.request.duration": false, "http.client.request.duration": false, "orders.total": false,
	} {
		s, ok := httpOptInMetricsView(sdkmetric.Instrument{Name: name, Scope: instrumentation.Scope{Name: contribScopePrefix + "net/http/otelhttp"}})
		if ok != drop {
			t.Errorf("%s: matched %v, want %v", name, ok, drop)
		}
		if _, isDrop := s.Aggregation.(sdkmetric.AggregationDrop); ok && !isDrop {
			t.Errorf("%s: aggregation %T, want drop", name, s.Aggregation)
		}
		// An application's own instrument of that name is kept (2026-11-08).
		if _, ok := httpOptInMetricsView(sdkmetric.Instrument{Name: name, Scope: instrumentation.Scope{Name: "my-app"}}); ok {
			t.Errorf("%s of an application scope dropped", name)
		}
	}
}

// A custom Option writing the field directly is not overwritten (review L1, 2026-11-08).
func TestHTTPOptInMetricsDirectOption(t *testing.T) {
	lifecycleTest(t)
	s, err := Setup(context.Background(), WithServiceName("a30"), WithMetricExporters("none"), func(o *Options) { o.HTTPOptInMetrics = true })
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s(context.Background()) }()
	if !installedOptions().HTTPOptInMetrics {
		t.Error("a direct Options.HTTPOptInMetrics=true was overwritten")
	}
}
