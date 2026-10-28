package otelhelper

import (
	"context"
	"fmt"
	"os"
	"strings"

	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/encoding"
)

// otlpTraceExporter builds the OTLP span exporter; a variable so lifecycle
// tests can inject a build failure or observe Shutdown.
var (
	otlpTraceExporter   = newOTLPTraceExporter
	consoleSpanExporter = newConsoleSpanExporter
)

// configureTracing builds the TracerProvider without installing it globally:
// Setup installs every signal only after all of them were built (G1).
func configureTracing(ctx context.Context, res *resource.Resource, opts *Options) (*sdktrace.TracerProvider, error) {
	tpOpts := []sdktrace.TracerProviderOption{
		sdktrace.WithResource(res),
		// Health-check server spans are dropped by the sampler (design D5).
		sdktrace.WithSampler(newHealthSampler(buildSampler(opts), opts.healthPaths())),
	}

	// Processors are built here, not with WithBatcher: a batch processor
	// starts its export goroutine on creation, so a later failure must shut
	// down the ones already built (which also shuts down their exporters).
	var processors []sdktrace.SpanProcessor
	for _, name := range opts.signalExporters(signalTraces) {
		var exporter sdktrace.SpanExporter
		var bspOpts []sdktrace.BatchSpanProcessorOption
		var err error
		switch name {
		case "otlp":
			e := opts.export(signalTraces)
			if !e.configured {
				continue // no endpoint for traces: spans stay in-process
			}
			exporter, err = otlpTraceExporter(ctx, e)
			if e.timeout > 0 {
				// The processor's own 30 s export timeout would otherwise cap
				// the resolved one (design D1 "Effective export deadline").
				bspOpts = append(bspOpts, sdktrace.WithExportTimeout(e.timeout))
			}
		case "console":
			exporter, err = consoleSpanExporter()
		}
		if err != nil {
			for i := len(processors) - 1; i >= 0; i-- {
				releaseOnFailure(ctx, "span processor", processors[i].Shutdown)
			}
			return nil, fmt.Errorf("trace exporter: %w", err)
		}
		p := sdktrace.NewBatchSpanProcessor(exporter, bspOpts...)
		processors = append(processors, p)
		tpOpts = append(tpOpts, sdktrace.WithSpanProcessor(p))
	}
	// With no exporter, spans still work in-process for context propagation;
	// they are simply not exported.

	if opts.DebugLevel {
		tpOpts = append(tpOpts, sdktrace.WithSpanProcessor(&debugProcessor{}))
	}

	return sdktrace.NewTracerProvider(tpOpts...), nil
}

// buildSampler is the helper's sampler (R6, design D3, ADR-002) — the single
// place it is built, so the health wrapper (D5) can wrap it.
//
// Precedence: a ratio set in code (any value, 1.0 included — D1 A13 ruling),
// or from the deprecated OTEL_HELPER_SAMPLE_RATIO alias (resolved only while
// OTEL_TRACES_SAMPLER is unset) → ParentBased(TraceIDRatioBased(ratio)); else OTEL_TRACES_SAMPLER /
// _ARG; else the spec default parentbased_always_on.
//
// The helper parses the env itself (ADR-001 owned inputs): the SDK reports an
// unknown name through otel.Handle without naming the variable, and the
// sampler must be an object the helper holds.
func buildSampler(opts *Options) sdktrace.Sampler {
	if opts.sampleRatioSet || opts.sampleRatioFromEnv {
		return sdktrace.ParentBased(sdktrace.TraceIDRatioBased(opts.SampleRatio))
	}
	raw := trimWS(os.Getenv(EnvTracesSampler))
	if raw == "" {
		return sdktrace.ParentBased(sdktrace.AlwaysSample())
	}
	switch name := strings.ToLower(raw); name {
	case "always_on":
		return sdktrace.AlwaysSample()
	case "always_off":
		return sdktrace.NeverSample()
	case "parentbased_always_on":
		return sdktrace.ParentBased(sdktrace.AlwaysSample())
	case "parentbased_always_off":
		return sdktrace.ParentBased(sdktrace.NeverSample())
	case "traceidratio":
		return sdktrace.TraceIDRatioBased(samplerRatioArg(name))
	case "parentbased_traceidratio":
		return sdktrace.ParentBased(sdktrace.TraceIDRatioBased(samplerRatioArg(name)))
	case "jaeger_remote", "parentbased_jaeger_remote", "xray":
		// Listed by the spec but need contrib samplers the helper doesn't ship.
		warnEnv("sampler is unsupported by this helper", EnvTracesSampler, raw, "parentbased_always_on")
		return sdktrace.ParentBased(sdktrace.AlwaysSample())
	default:
		warnEnv("unknown sampler (valid: always_on, always_off, traceidratio, parentbased_always_on, parentbased_always_off, parentbased_traceidratio)",
			EnvTracesSampler, raw, "parentbased_always_on")
		return sdktrace.ParentBased(sdktrace.AlwaysSample())
	}
}

// samplerRatioArg reads OTEL_TRACES_SAMPLER_ARG for a ratio sampler: unset or
// blank → 1.0 silently; not a number in [0, 1] (NaN included) → warning, 1.0.
func samplerRatioArg(sampler string) float64 {
	raw := trimWS(os.Getenv(EnvTracesSamplerArg))
	if raw == "" {
		return 1.0
	}
	ratio, ok := parseRatio(raw)
	if !ok {
		warnEnv("sampler argument is not a number in [0, 1] for "+sampler, EnvTracesSamplerArg, raw, "1.0")
		return 1.0
	}
	return ratio
}

// StartRootSpan starts a new span detached from any parent (new trace).
// Use in workers where each iteration should be an independent trace.
func StartRootSpan(ctx context.Context, tracer trace.Tracer, name string, opts ...trace.SpanStartOption) (context.Context, trace.Span) {
	opts = append(opts, trace.WithNewRoot())
	return tracer.Start(ctx, name, opts...)
}

// newOTLPTraceExporter builds the OTLP span exporter for the resolved
// protocol. Endpoint, compression, timeout, headers and the plaintext TLS
// override are helper-resolved and passed explicitly (ADR-001 second
// correction); certificates and protocol defaults stay with the SDK (G8).
func newOTLPTraceExporter(ctx context.Context, e *signalExport) (sdktrace.SpanExporter, error) {
	if e.protocol == ProtocolHTTP {
		var o []otlptracehttp.Option
		if e.endpointURL != "" {
			o = append(o, otlptracehttp.WithEndpointURL(e.endpointURL))
		}
		if e.timeout > 0 {
			o = append(o, otlptracehttp.WithTimeout(e.timeout))
		}
		if e.headers != nil {
			o = append(o, otlptracehttp.WithHeaders(e.headers))
		}
		if e.dropTLS {
			// nil clears the TLS config the SDK loaded from env (options are
			// applied after env); otherwise New() fails on an http:// endpoint.
			o = append(o, otlptracehttp.WithTLSClientConfig(nil))
		}
		if e.compression == CompressionNone {
			o = append(o, otlptracehttp.WithCompression(otlptracehttp.NoCompression))
		} else {
			o = append(o, otlptracehttp.WithCompression(otlptracehttp.GzipCompression))
		}
		return otlptracehttp.New(ctx, o...)
	}
	var o []otlptracegrpc.Option
	if e.endpointURL != "" {
		o = append(o, otlptracegrpc.WithEndpointURL(e.endpointURL))
	}
	if e.timeout > 0 {
		o = append(o, otlptracegrpc.WithTimeout(e.timeout))
	}
	if e.headers != nil {
		o = append(o, otlptracegrpc.WithHeaders(e.headers))
	}
	if e.dropTLS {
		// The SDK prefers env TLS credentials over Insecure: force plaintext.
		o = append(o, otlptracegrpc.WithTLSCredentials(insecure.NewCredentials()))
	}
	// The trace/metric gRPC exporters accept only "gzip" ("none" is reported
	// via otel.Handle as invalid) and default to none: CompressionNone means
	// no compressor option.
	if e.compression == CompressionNone {
		// "none" is not an accepted compressor name here; force identity so
		// an env gzip can't win over WithCompression("none").
		o = append(o, otlptracegrpc.WithDialOption(grpc.WithDefaultCallOptions(grpc.UseCompressor(encoding.Identity))))
	} else {
		o = append(o, otlptracegrpc.WithCompressor(CompressionGzip))
	}
	return otlptracegrpc.New(ctx, o...)
}
