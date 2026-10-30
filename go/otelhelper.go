package otelhelper

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/log/global"
	nooplog "go.opentelemetry.io/otel/log/noop"
	"go.opentelemetry.io/otel/metric"
	noopmetric "go.opentelemetry.io/otel/metric/noop"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"
	"go.opentelemetry.io/otel/trace"
	nooptrace "go.opentelemetry.io/otel/trace/noop"
)

// Shutdown flushes and shuts down all telemetry providers: logs, metrics
// (listener, then provider) and traces concurrently, under ctx, errors joined.
// It is idempotent: later or concurrent calls return the result of the first
// one. After it returns, Setup builds a fresh pipeline; a Setup called while
// it is in flight waits for it. Tracers, meters and instrumented handlers
// obtained before the new Setup stay bound to the old, shut-down providers
// (the global delegates forward once — design D13): obtain them after Setup.
type Shutdown func(ctx context.Context) error

var (
	mu         sync.Mutex
	setupDone  bool
	shutdownFn Shutdown
	// installed is the Options the last successful Setup resolved and acted
	// on (parity-test seam: adapters read decisions from here instead of
	// re-running the resolver, which would repeat its warnings).
	installed *Options
	// current is the installed pipeline; its Shutdown resets the state above
	// only while it is still current.
	current *pipeline
)

// installedOptions returns the options of the last successful Setup, or nil.
func installedOptions() *Options {
	mu.Lock()
	defer mu.Unlock()
	return installed
}

// noopShutdown is returned when Setup fails so callers always get a safe function.
func noopShutdown(_ context.Context) error { return nil }

// cleanupTimeout bounds the release of a partially built pipeline.
const cleanupTimeout = 5 * time.Second

// releaseOnFailure shuts down one component of a pipeline that failed to
// build. The caller's context may already be cancelled (that can be why the
// build failed), so it runs detached with its own deadline; errors only reach
// the diagnostics channel, since the build error is the one to return.
func releaseOnFailure(ctx context.Context, what string, shutdown func(context.Context) error) {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), cleanupTimeout)
	defer cancel()
	if err := shutdown(ctx); err != nil {
		diagLogger.Warn("otelhelper: releasing "+what+" after a failed Setup", "error", err)
	}
}

// pipeline is one built set of providers. Each group (logs; metrics:
// listener then provider; traces) runs its steps in order; the groups run
// concurrently on Shutdown.
type pipeline struct {
	once   sync.Once
	err    error
	groups [][]func(context.Context) error
	// closing is set (under mu) when Shutdown of the current pipeline starts;
	// done is closed when it has finished and the state is reset. Setup waits
	// on done instead of returning a dying pipeline.
	closing bool
	done    chan struct{}
}

func newPipeline() *pipeline { return &pipeline{done: make(chan struct{})} }

func (p *pipeline) shutdown(ctx context.Context) error {
	p.once.Do(func() {
		// Stop advertising this pipeline before blocking on flushes: new
		// loggers fall back to stdout and MetricsHandler answers 503. mu is
		// not held across the provider Shutdown calls, which may block. A
		// pipeline that is no longer current (a stale Shutdown kept by the
		// caller after a newer Setup) touches no shared state.
		mu.Lock()
		owner := current == p
		if owner {
			p.closing = true
			activeLogBridge.Store(nil) // loggers go back to stdout, not to a closing provider
			setPromRegistry(nil)
		}
		mu.Unlock()

		errs := make([]error, len(p.groups))
		var wg sync.WaitGroup
		for i, group := range p.groups {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for _, step := range group {
					errs[i] = errors.Join(errs[i], step(ctx))
				}
			}()
		}
		wg.Wait()
		p.err = errors.Join(errs...)

		mu.Lock()
		if owner {
			setupDone, shutdownFn, installed, current = false, nil, nil, nil
		}
		mu.Unlock()
		close(p.done)
	})
	return p.err
}

// Setup configures tracing, metrics, and logging. Call once at startup;
// further calls return the installed Shutdown until it has run.
//
// Every signal is built before anything is installed: when Setup returns an
// error, what was already built is shut down in reverse order, the global
// providers, propagator and MetricsHandler are left as they were, and Setup
// can be called again.
func Setup(ctx context.Context, opts ...Option) (Shutdown, error) {
	mu.Lock()
	for current != nil && current.closing {
		done := current.done
		mu.Unlock()
		select {
		case <-done:
		case <-ctx.Done():
			return noopShutdown, fmt.Errorf("otelhelper: waiting for an in-flight Shutdown: %w", ctx.Err())
		}
		mu.Lock()
	}
	defer mu.Unlock()

	if setupDone {
		return shutdownFn, nil
	}

	options := newOptions(opts...)
	if err := options.validate(); err != nil {
		// Validation failure does NOT set setupDone — caller can retry with valid config.
		return noopShutdown, fmt.Errorf("otelhelper: %w", err)
	}

	p := newPipeline()
	if options.sdkDisabled {
		// OTEL_SDK_DISABLED=true: no-op providers for every signal; context
		// propagation keeps working. The INFO line is the same in every
		// language (design D1 row "OTEL_SDK_DISABLED INFO", A19).
		diagLogger.Info("otelhelper: OTEL_SDK_DISABLED=true: telemetry disabled, only propagators installed")
		otel.SetTracerProvider(nooptrace.NewTracerProvider())
		otel.SetMeterProvider(noopmetric.NewMeterProvider())
		global.SetLoggerProvider(nooplog.NewLoggerProvider())
		otel.SetTextMapPropagator(options.propagator)
		setPromInactive() // Prometheus deliberately off: 200, empty body
		commit(p, options, nil)
		return shutdownFn, nil
	}

	res := buildResource(options)
	// The SDK providers re-parse OTEL_RESOURCE_ATTRIBUTES (WithResource merges
	// resource.Environment()) and echo malformed items through otel.Handle:
	// filter exactly those while they are built (design D1 row of 2026-10-29).
	stopQuiet := quietResourceEnvEcho(options.envResourceEcho)
	defer stopQuiet()

	// built holds release functions in build order; fail runs them in reverse.
	var built []func(context.Context) error
	fail := func(err error) (Shutdown, error) {
		for i := len(built) - 1; i >= 0; i-- {
			releaseOnFailure(ctx, "telemetry pipeline", built[i])
		}
		return noopShutdown, fmt.Errorf("otelhelper: %w", err)
	}

	var (
		tp       *sdktrace.TracerProvider
		mp       *sdkmetric.MeterProvider
		registry *prometheus.Registry
		lp       *sdklog.LoggerProvider
		hasProc  bool
	)

	if options.IsSignalEnabled("traces") {
		var err error
		if tp, err = configureTracing(ctx, res, options); err != nil {
			return fail(err)
		}
		built = append(built, tp.Shutdown)
		p.groups = append(p.groups, []func(context.Context) error{tp.Shutdown})
	}

	if options.IsSignalEnabled("metrics") && len(options.resolvedMetricExporters()) > 0 {
		var (
			release func(context.Context) error
			err     error
		)
		if mp, release, registry, err = configureMetrics(ctx, res, options); err != nil {
			return fail(err)
		}
		built = append(built, mp.Shutdown)
		group := []func(context.Context) error{mp.Shutdown}
		if release != nil {
			// Listener first on Shutdown: stop serving scrapes, then flush.
			built = append(built, release)
			group = append([]func(context.Context) error{release}, group...)
		}
		p.groups = append(p.groups, group)
	}

	if options.IsSignalEnabled("logs") {
		var err error
		if lp, hasProc, err = configureLogging(ctx, res, options); err != nil {
			return fail(err)
		}
		built = append(built, lp.Shutdown)
		p.groups = append(p.groups, []func(context.Context) error{lp.Shutdown})
	}

	stopQuiet() // providers built: the SDK will not parse the variable again
	// Everything built: install.
	if tp != nil {
		otel.SetTracerProvider(tp)
	} else {
		otel.SetTracerProvider(nooptrace.NewTracerProvider())
	}
	if mp != nil {
		otel.SetMeterProvider(mp)
	} else {
		otel.SetMeterProvider(noopmetric.NewMeterProvider())
	}
	if lp != nil {
		global.SetLoggerProvider(lp)
	}
	otel.SetTextMapPropagator(options.propagator)
	if registry != nil {
		setPromRegistry(registry)
	} else {
		setPromInactive() // no "prometheus" metrics exporter: 200, empty body
	}

	commit(p, options, newLogBridge(lp, hasProc))
	return shutdownFn, nil
}

// commit records p as the installed pipeline and publishes its log bridge
// (nil = stdout). Callers hold mu.
func commit(p *pipeline, options *Options, bridge *logBridge) {
	options.metricIntervalMs = effectiveIntervalMs(options)
	current = p
	shutdownFn = p.shutdown
	// Level first: a record that sees the new bridge must also see its level.
	installedLogLevel.Set(DefaultLogLevel(options.Environment, options.DebugLevel))
	activeLogBridge.Store(bridge)
	paths := healthPathSet(options.healthPaths())
	installedHealthPaths.Store(&paths)
	setupDone, installed = true, options
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

// buildResource layers, lowest to highest precedence: OTEL_RESOURCE_ATTRIBUTES
// (the helper's parse), code ResourceAttributes, the resolved identity
// (service.name, deployment.environment.name — absent when unset —,
// service.instance.id) and the SDK's telemetry.sdk.*, which env and code
// cannot override. The schema URL is the semconv version the helper targets
// (design D1 rows of 2026-10-29).
func buildResource(opts *Options) *resource.Resource {
	if !opts.identityResolved {
		opts.resolveIdentity()
	}
	merged := map[string]string{}
	for _, layer := range []map[string]string{opts.envResource, opts.ResourceAttributes} {
		for k, v := range layer {
			merged[k] = v
		}
	}
	merged[string(semconv.ServiceNameKey)] = opts.ServiceName
	merged[string(semconv.ServiceInstanceIDKey)] = opts.serviceInstanceID
	if opts.Environment == "" {
		delete(merged, string(semconv.DeploymentEnvironmentNameKey))
	} else {
		merged[string(semconv.DeploymentEnvironmentNameKey)] = string(opts.Environment)
	}
	kvs := make([]attribute.KeyValue, 0, len(merged)+3)
	for k, v := range merged {
		if !isTelemetrySDKKey(k) {
			kvs = append(kvs, attribute.String(k, v))
		}
	}
	return resource.NewWithAttributes(schemaURL, append(kvs, telemetrySDKAttributes()...)...)
}
