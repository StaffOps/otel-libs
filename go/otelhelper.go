package otelhelper

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"
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

	tp, err := configureTracing(ctx, res, options)
	if err != nil {
		return noopShutdown, fmt.Errorf("otelhelper: %w", err)
	}

	mp, err := configureMetrics(ctx, res, options)
	if err != nil {
		tp.Shutdown(ctx) // cleanup already-created provider
		return noopShutdown, fmt.Errorf("otelhelper: %w", err)
	}

	lp, err := configureLogging(ctx, res, options)
	if err != nil {
		tp.Shutdown(ctx) // cleanup
		mp.Shutdown(ctx) // cleanup
		return noopShutdown, fmt.Errorf("otelhelper: %w", err)
	}

	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	shutdownFn = func(ctx context.Context) error {
		return errors.Join(
			tp.Shutdown(ctx),
			mp.Shutdown(ctx),
			lp.Shutdown(ctx),
		)
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
	attrs := []attribute.KeyValue{
		semconv.ServiceName(opts.ServiceName),
		attribute.String("deployment.environment", string(opts.Environment)),
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
