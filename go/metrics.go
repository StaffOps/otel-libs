package otelhelper

import (
	"context"
	"fmt"
	"os"
	"path"

	"go.opentelemetry.io/contrib/instrumentation/runtime"
	"go.opentelemetry.io/otel"
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

	exporter, err := otlpmetricgrpc.New(ctx,
		otlpmetricgrpc.WithEndpoint(opts.OtelEndpoint),
		otlpmetricgrpc.WithInsecure(),
		otlpmetricgrpc.WithCompressor("gzip"),
	)
	if err != nil {
		return nil, fmt.Errorf("metric exporter: %w", err)
	}

	mpOpts := []sdkmetric.Option{
		sdkmetric.WithResource(res),
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exporter)),
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
