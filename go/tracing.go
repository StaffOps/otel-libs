package otelhelper

import (
	"context"
	"crypto/tls"
	"fmt"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/credentials"
)

func configureTracing(ctx context.Context, res *resource.Resource, opts *Options) (*sdktrace.TracerProvider, error) {
	var rootSampler sdktrace.Sampler
	if opts.SampleRatio >= 1.0 {
		rootSampler = sdktrace.AlwaysSample()
	} else {
		rootSampler = sdktrace.TraceIDRatioBased(opts.SampleRatio)
	}

	tpOpts := []sdktrace.TracerProviderOption{
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sdktrace.ParentBased(rootSampler)),
	}

	if opts.OtelEndpoint != "" {
		// OTLP push — export spans to collector.
		traceOpts := []otlptracegrpc.Option{
			otlptracegrpc.WithEndpoint(opts.OtelEndpoint),
			otlptracegrpc.WithTimeout(time.Duration(opts.ExportTimeoutMs) * time.Millisecond),
			otlptracegrpc.WithCompressor("gzip"),
		}
		if opts.Insecure {
			traceOpts = append(traceOpts, otlptracegrpc.WithInsecure())
		} else {
			traceOpts = append(traceOpts, otlptracegrpc.WithTLSCredentials(credentials.NewTLS(&tls.Config{})))
		}
		exporter, err := otlptracegrpc.New(ctx, traceOpts...)
		if err != nil {
			return nil, fmt.Errorf("trace exporter: %w", err)
		}
		tpOpts = append(tpOpts, sdktrace.WithBatcher(exporter))
	}
	// When OtelEndpoint is empty, no exporter is added. Spans still work
	// in-process for context propagation; they are simply not exported.

	if opts.DebugLevel {
		tpOpts = append(tpOpts, sdktrace.WithSpanProcessor(&debugProcessor{}))
	}

	tp := sdktrace.NewTracerProvider(tpOpts...)
	otel.SetTracerProvider(tp)
	return tp, nil
}

// StartRootSpan starts a new span detached from any parent (new trace).
// Use in workers where each iteration should be an independent trace.
func StartRootSpan(ctx context.Context, tracer trace.Tracer, name string, opts ...trace.SpanStartOption) (context.Context, trace.Span) {
	opts = append(opts, trace.WithNewRoot())
	return tracer.Start(ctx, name, opts...)
}
