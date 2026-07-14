package otelhelper

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	noopmetric "go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"
	nooptrace "go.opentelemetry.io/otel/trace/noop"
)

// Shutdown flushes and shuts down all telemetry providers.
type Shutdown func(ctx context.Context) error

var (
	mu         sync.Mutex
	setupDone  bool
	shutdownFn Shutdown
	setupErr   error
)

// noopShutdown is returned when Setup fails so callers always get a safe function.
func noopShutdown(_ context.Context) error { return nil }

// Setup configures tracing, metrics, and logging. Call once at startup.
// Returns a Shutdown function for deferred cleanup.
func Setup(ctx context.Context, opts ...Option) (Shutdown, error) {
	mu.Lock()
	defer mu.Unlock()

	if setupDone {
		return shutdownFn, nil
	}

	options := newOptions(opts...)
	if err := options.validate(); err != nil {
		// Validation failure does NOT set setupDone — caller can retry with valid config.
		return noopShutdown, fmt.Errorf("otelhelper: %w", err)
	}

	res := buildResource(options)

	var shutdowns []func(context.Context) error

	if options.IsSignalEnabled("traces") {
		tp, err := configureTracing(ctx, res, options)
		if err != nil {
			return noopShutdown, fmt.Errorf("otelhelper: %w", err)
		}
		shutdowns = append(shutdowns, tp.Shutdown)
	} else {
		otel.SetTracerProvider(nooptrace.NewTracerProvider())
	}

	if options.IsSignalEnabled("metrics") && len(options.resolvedMetricExporters()) > 0 {
		mp, srvShutdown, err := configureMetrics(ctx, res, options)
		if err != nil {
			noopShutdown(ctx) // best-effort cleanup
			return noopShutdown, fmt.Errorf("otelhelper: %w", err)
		}
		if srvShutdown != nil {
			shutdowns = append(shutdowns, srvShutdown)
		}
		shutdowns = append(shutdowns, mp.Shutdown)
	} else {
		otel.SetMeterProvider(noopmetric.NewMeterProvider())
	}

	if options.IsSignalEnabled("logs") {
		lp, err := configureLogging(ctx, res, options)
		if err != nil {
			for _, fn := range shutdowns {
				fn(ctx)
			}
			return noopShutdown, fmt.Errorf("otelhelper: %w", err)
		}
		shutdowns = append(shutdowns, lp.Shutdown)
	}

	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	shutdownFn = func(ctx context.Context) error {
		var errs []error
		for _, fn := range shutdowns {
			errs = append(errs, fn(ctx))
		}
		return errors.Join(errs...)
	}
	setupDone = true
	setupErr = nil
	return shutdownFn, nil
}

// GetTracer returns a Tracer from the global provider.
func GetTracer(name ...string) trace.Tracer {
	n := "otel-helper"
	if len(name) > 0 && name[0] != "" {
		n = name[0]
	}
	return otel.Tracer(n)
}

// GetMeter returns a Meter from the global provider.
func GetMeter(name ...string) metric.Meter {
	n := "otel-helper"
	if len(name) > 0 && name[0] != "" {
		n = name[0]
	}
	return otel.Meter(n)
}

func buildResource(opts *Options) *resource.Resource {
	// "deployment.environment.name" is the semconv >= v1.27 key. The otel module
	// version pinned in go.mod (v1.31.0) only bundles semconv up to v1.26.0, which
	// has no typed constant for it — using the literal string keeps this in sync
	// with Python/.NET without forcing an unrelated SDK-wide version bump.
	attrs := []attribute.KeyValue{
		semconv.ServiceName(opts.ServiceName),
		attribute.String("deployment.environment.name", string(opts.Environment)),
	}
	for k, v := range opts.ResourceAttributes {
		attrs = append(attrs, attribute.String(k, v))
	}
	res, _ := resource.Merge(
		resource.Default(),
		resource.NewWithAttributes(semconv.SchemaURL, attrs...),
	)
	return res
}
