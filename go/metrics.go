package otelhelper

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"path"
	"time"

	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.opentelemetry.io/contrib/instrumentation/runtime"
	"go.opentelemetry.io/otel"
	promexporter "go.opentelemetry.io/otel/exporters/prometheus"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
)

func configureMetrics(ctx context.Context, res *resource.Resource, opts *Options) (*sdkmetric.MeterProvider, error) {
	// TODO: Replace with sdkmetric.WithExemplarFilter(sdkmetric.TraceBasedExemplarFilter)
	// when available in a future SDK version. The programmatic API is not exported in v1.31.0.
	// This must be called before spawning goroutines (Setup is called early at startup).
	if os.Getenv("OTEL_METRICS_EXEMPLAR_FILTER") == "" {
		os.Setenv("OTEL_METRICS_EXEMPLAR_FILTER", "trace_based")
	}

	var mpOpts []sdkmetric.Option

	if opts.OtelEndpoint == "" {
		// Prometheus scrape fallback — expose /metrics on PrometheusMetricsPort.
		exporter, err := promexporter.New()
		if err != nil {
			return nil, fmt.Errorf("prometheus exporter: %w", err)
		}
		mpOpts = append(mpOpts,
			sdkmetric.WithResource(res),
			sdkmetric.WithReader(exporter),
		)
	} else {
		// OTLP push path — export metrics to collector via gRPC.
		exporter, err := otlpmetricgrpc.New(ctx,
			otlpmetricgrpc.WithEndpoint(opts.OtelEndpoint),
			otlpmetricgrpc.WithInsecure(),
			otlpmetricgrpc.WithCompressor("gzip"),
		)
		if err != nil {
			return nil, fmt.Errorf("metric exporter: %w", err)
		}
		mpOpts = append(mpOpts,
			sdkmetric.WithResource(res),
			sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exporter,
				sdkmetric.WithInterval(30*time.Second),
			)),
		)
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

	// When in Prometheus mode, start HTTP server for scraping.
	if opts.OtelEndpoint == "" {
		go func() {
			mux := http.NewServeMux()
			mux.Handle("/metrics", promhttp.Handler())
			srv := &http.Server{Addr: fmt.Sprintf(":%d", opts.PrometheusMetricsPort), Handler: mux}
			srv.ListenAndServe()
		}()
	}

	return mp, nil
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
