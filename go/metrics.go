package otelhelper

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"path"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.opentelemetry.io/contrib/instrumentation/runtime"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	promexporter "go.opentelemetry.io/otel/exporters/prometheus"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	"google.golang.org/grpc/credentials"
)

// promRegistry is the dedicated registry backing the Prometheus reader.
// Deliberately NOT the client_golang global registry, so application metrics
// registered there don't leak into the helper's /metrics and vice versa.
var (
	promRegistryMu sync.Mutex
	promRegistry   *prometheus.Registry
)

// MetricsHandler returns an http.Handler serving the Prometheus scrape
// endpoint. Mount it on the application's own mux:
//
//	mux.Handle("/metrics", otelhelper.MetricsHandler())
//
// Combine with WithoutMetricsListener() (or OTEL_HELPER_METRICS_PORT=0) to
// suppress the standalone listener. Before Setup configures the Prometheus
// reader, the handler answers 503.
func MetricsHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		promRegistryMu.Lock()
		reg := promRegistry
		promRegistryMu.Unlock()
		if reg == nil {
			http.Error(w, "otelhelper: prometheus metrics reader not configured", http.StatusServiceUnavailable)
			return
		}
		promhttp.HandlerFor(reg, promhttp.HandlerOpts{}).ServeHTTP(w, r)
	})
}

func setPromRegistry(reg *prometheus.Registry) {
	promRegistryMu.Lock()
	defer promRegistryMu.Unlock()
	promRegistry = reg
}

// configureMetrics builds the MeterProvider with one reader per resolved
// exporter ("otlp" push and/or "prometheus" scrape — design D2). The returned
// shutdown (possibly nil) stops the standalone /metrics listener.
func configureMetrics(ctx context.Context, res *resource.Resource, opts *Options) (*sdkmetric.MeterProvider, func(context.Context) error, error) {
	// TODO: Replace with sdkmetric.WithExemplarFilter(sdkmetric.TraceBasedExemplarFilter)
	// when available in a future SDK version. The programmatic API is not exported in v1.31.0.
	// This must be called before spawning goroutines (Setup is called early at startup).
	if os.Getenv("OTEL_METRICS_EXEMPLAR_FILTER") == "" {
		os.Setenv("OTEL_METRICS_EXEMPLAR_FILTER", "trace_based")
	}

	mpOpts := []sdkmetric.Option{sdkmetric.WithResource(res)}
	var srvShutdown func(context.Context) error

	if opts.hasMetricExporter("otlp") {
		metricOpts := []otlpmetricgrpc.Option{
			otlpmetricgrpc.WithEndpoint(opts.OtelEndpoint),
			otlpmetricgrpc.WithCompressor("gzip"),
		}
		if opts.Insecure {
			metricOpts = append(metricOpts, otlpmetricgrpc.WithInsecure())
		} else {
			metricOpts = append(metricOpts, otlpmetricgrpc.WithTLSCredentials(credentials.NewTLS(&tls.Config{})))
		}
		exporter, err := otlpmetricgrpc.New(ctx, metricOpts...)
		if err != nil {
			return nil, nil, fmt.Errorf("metric exporter: %w", err)
		}
		interval := opts.ExportIntervalMs
		if interval <= 0 {
			interval = defaultExportIntervalMs
		}
		mpOpts = append(mpOpts, sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exporter,
			sdkmetric.WithInterval(time.Duration(interval)*time.Millisecond),
		)))
	}

	if opts.hasMetricExporter("prometheus") {
		registry := prometheus.NewRegistry()
		exporter, err := promexporter.New(promexporter.WithRegisterer(registry))
		if err != nil {
			return nil, nil, fmt.Errorf("prometheus exporter: %w", err)
		}
		setPromRegistry(registry)
		mpOpts = append(mpOpts, sdkmetric.WithReader(exporter))

		if opts.PrometheusMetricsPort > 0 {
			// Synchronous Listen: a busy port fails Setup immediately instead of
			// dying silently in a goroutine (the P5 "silent telemetry loss" bug).
			ln, err := net.Listen("tcp", fmt.Sprintf(":%d", opts.PrometheusMetricsPort))
			if err != nil {
				return nil, nil, fmt.Errorf(
					"prometheus /metrics listener on port %d: %w (if another process owns the port, mount otelhelper.MetricsHandler() and use WithoutMetricsListener())",
					opts.PrometheusMetricsPort, err)
			}
			mux := http.NewServeMux()
			mux.Handle("/metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))
			srv := &http.Server{
				Handler:           mux,
				ReadHeaderTimeout: 5 * time.Second,
			}
			go func() {
				if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
					otel.Handle(fmt.Errorf("otelhelper: prometheus /metrics server: %w", err))
				}
			}()
			srvShutdown = srv.Shutdown
		}
	}

	if len(opts.DisabledMetrics) > 0 {
		mpOpts = append(mpOpts, sdkmetric.WithView(disabledMetricsView(opts.DisabledMetrics)))
	}

	mp := sdkmetric.NewMeterProvider(mpOpts...)
	otel.SetMeterProvider(mp)

	// Start runtime metrics (goroutines, GC, memory). Non-fatal if it fails.
	if err := runtime.Start(runtime.WithMeterProvider(mp)); err != nil {
		otel.Handle(err)
	}

	return mp, srvShutdown, nil
}

// disabledMetricsView returns a View that drops metrics matching any of the given glob patterns.
func disabledMetricsView(patterns []string) sdkmetric.View {
	return func(inst sdkmetric.Instrument) (sdkmetric.Stream, bool) {
		for _, pattern := range patterns {
			if matched, _ := path.Match(pattern, inst.Name); matched {
				return sdkmetric.Stream{Aggregation: sdkmetric.AggregationDrop{}}, true
			}
		}
		return sdkmetric.Stream{}, false
	}
}
