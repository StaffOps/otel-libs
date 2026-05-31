package otelhelper

import (
	"context"
	"fmt"
	"os"

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

	mp := sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(res),
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exporter)),
	)
	otel.SetMeterProvider(mp)

	// Start runtime metrics (goroutines, GC, memory). Non-fatal if it fails.
	if err := runtime.Start(runtime.WithMeterProvider(mp)); err != nil {
		otel.Handle(err)
	}

	return mp, nil
}
