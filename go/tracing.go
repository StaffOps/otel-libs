package otelhelper

import (
	"context"
	"crypto/tls"
	"fmt"
	"os"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/credentials"
)

func configureTracing(ctx context.Context, res *resource.Resource, opts *Options) (*sdktrace.TracerProvider, error) {
	tpOpts := []sdktrace.TracerProviderOption{
		sdktrace.WithResource(res),
	}

	if os.Getenv(EnvTracesSampler) != "" && opts.SampleRatio >= 1.0 {
		// No explicit ratio in code: standard SDK env config wins. NewTracerProvider
		// without WithSampler reads OTEL_TRACES_SAMPLER / OTEL_TRACES_SAMPLER_ARG itself.
	} else {
		var rootSampler sdktrace.Sampler
		if opts.SampleRatio >= 1.0 {
			rootSampler = sdktrace.AlwaysSample()
		} else {
			rootSampler = sdktrace.TraceIDRatioBased(opts.SampleRatio)
		}
		tpOpts = append(tpOpts, sdktrace.WithSampler(sdktrace.ParentBased(rootSampler)))
	}

	if opts.OtelEndpoint != "" {
		// OTLP push — export spans to collector, over gRPC or HTTP/protobuf
		// depending on the resolved protocol (design: config.go's
		// resolvedOtlpProtocol — explicit > OTEL_EXPORTER_OTLP_PROTOCOL >
		// port 4318 inference > grpc default).
		var exporter *otlptrace.Exporter
		var err error
		if opts.resolvedOtlpProtocol() == ProtocolHTTP {
			httpOpts := []otlptracehttp.Option{
				otlptracehttp.WithEndpoint(opts.OtelEndpoint),
				otlptracehttp.WithTimeout(time.Duration(opts.ExportTimeoutMs) * time.Millisecond),
				otlptracehttp.WithCompression(otlptracehttp.GzipCompression),
			}
			if opts.Insecure {
				httpOpts = append(httpOpts, otlptracehttp.WithInsecure())
			} else {
				httpOpts = append(httpOpts, otlptracehttp.WithTLSClientConfig(&tls.Config{}))
			}
			exporter, err = otlptracehttp.New(ctx, httpOpts...)
		} else {
			grpcOpts := []otlptracegrpc.Option{
				otlptracegrpc.WithEndpoint(opts.OtelEndpoint),
				otlptracegrpc.WithTimeout(time.Duration(opts.ExportTimeoutMs) * time.Millisecond),
				otlptracegrpc.WithCompressor("gzip"),
			}
			if opts.Insecure {
				grpcOpts = append(grpcOpts, otlptracegrpc.WithInsecure())
			} else {
				grpcOpts = append(grpcOpts, otlptracegrpc.WithTLSCredentials(credentials.NewTLS(&tls.Config{})))
			}
			exporter, err = otlptracegrpc.New(ctx, grpcOpts...)
		}
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
