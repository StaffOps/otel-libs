package otelhelper

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"path"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.opentelemetry.io/contrib/instrumentation/runtime"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	promexporter "go.opentelemetry.io/otel/exporters/prometheus"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/encoding"
)

// promRegistry is the dedicated registry backing the Prometheus reader.
// Deliberately NOT the client_golang global registry, so application metrics
// registered there don't leak into the helper's /metrics and vice versa.
var (
	promRegistryMu sync.Mutex
	promRegistry   *prometheus.Registry
	// promInactive: the installed pipeline deliberately has no Prometheus
	// reader (OTEL_SDK_DISABLED, or OTEL_METRICS_EXPORTER without
	// "prometheus").
	promInactive bool
)

// MetricsHandler returns an http.Handler serving the Prometheus scrape
// endpoint. Mount it on the application's own mux:
//
//	mux.Handle("/metrics", otelhelper.MetricsHandler())
//
// Combine with WithoutMetricsListener() (or OTEL_HELPER_METRICS_PORT=0) to
// suppress the standalone listener.
//
// Status (design "Mounted /metrics handler status"): 503 before Setup, after
// a failed Setup and after Shutdown (transient: the scraper retries); 200 with
// an empty body when the installed pipeline deliberately has no Prometheus
// reader, so a configuration choice doesn't look like a down target.
func MetricsHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		promRegistryMu.Lock()
		reg, inactive := promRegistry, promInactive
		promRegistryMu.Unlock()
		if reg == nil && inactive {
			w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
			w.WriteHeader(http.StatusOK)
			return
		}
		if reg == nil {
			http.Error(w, "otelhelper: prometheus metrics reader not configured", http.StatusServiceUnavailable)
			return
		}
		promhttp.HandlerFor(reg, promhttp.HandlerOpts{}).ServeHTTP(w, r)
	})
}

// setPromRegistry publishes the active registry; nil means "not configured"
// (503).
func setPromRegistry(reg *prometheus.Registry) {
	promRegistryMu.Lock()
	defer promRegistryMu.Unlock()
	promRegistry, promInactive = reg, false
}

// setPromInactive records that Prometheus is deliberately off (200, empty).
func setPromInactive() {
	promRegistryMu.Lock()
	defer promRegistryMu.Unlock()
	promRegistry, promInactive = nil, true
}

// effectiveIntervalMs is the export interval the periodic readers use.
func effectiveIntervalMs(o *Options) int {
	if o.ExportIntervalMs > 0 {
		return o.ExportIntervalMs
	}
	return defaultExportIntervalMs
}

// configureMetrics builds the MeterProvider with one reader per resolved
// exporter ("otlp" push and/or "prometheus" scrape — design D2). Nothing is
// installed globally: Setup installs the provider and publishes the returned
// registry (non-nil only with a "prometheus" reader) to MetricsHandler after
// every signal was built (G1). The returned release (possibly nil) stops the
// standalone /metrics listener and frees its port synchronously.
func configureMetrics(ctx context.Context, res *resource.Resource, opts *Options) (*sdkmetric.MeterProvider, func(context.Context) error, *prometheus.Registry, error) {
	// Exemplars: the SDK default is trace_based and it honors
	// OTEL_METRICS_EXEMPLAR_FILTER itself — the helper sets nothing (G7).
	mpOpts := []sdkmetric.Option{sdkmetric.WithResource(res)}
	var (
		release  func(context.Context) error
		registry *prometheus.Registry
		readers  []sdkmetric.Reader
	)
	// fail releases the readers built so far: a periodic reader starts its
	// export goroutine on creation, before any provider owns it.
	fail := func(err error) (*sdkmetric.MeterProvider, func(context.Context) error, *prometheus.Registry, error) {
		for i := len(readers) - 1; i >= 0; i-- {
			releaseOnFailure(ctx, "metric reader", readers[i].Shutdown)
		}
		return nil, nil, nil, err
	}

	interval := effectiveIntervalMs(opts)
	periodic := func(exp sdkmetric.Exporter, timeout time.Duration) {
		ro := []sdkmetric.PeriodicReaderOption{sdkmetric.WithInterval(time.Duration(interval) * time.Millisecond)}
		if timeout > 0 {
			// The reader's own 30 s timeout would otherwise cap the resolved
			// export timeout (design D1 "Effective export deadline").
			ro = append(ro, sdkmetric.WithTimeout(timeout))
		}
		readers = append(readers, sdkmetric.NewPeriodicReader(exp, ro...))
	}

	if opts.hasMetricExporter("otlp") {
		exporter, err := newOTLPMetricExporter(ctx, opts.export(signalMetrics))
		if err != nil {
			return fail(fmt.Errorf("metric exporter: %w", err))
		}
		periodic(exporter, opts.export(signalMetrics).timeout)
	}

	if opts.hasMetricExporter("console") {
		exporter, err := newConsoleMetricExporter()
		if err != nil {
			return fail(fmt.Errorf("console metric exporter: %w", err))
		}
		periodic(exporter, 0)
	}

	if opts.hasMetricExporter("prometheus") {
		reg := prometheus.NewRegistry()
		exporter, err := promexporter.New(promexporter.WithRegisterer(reg))
		if err != nil {
			return fail(fmt.Errorf("prometheus exporter: %w", err))
		}
		readers = append(readers, exporter)
		registry = reg

		if opts.PrometheusMetricsPort > 0 {
			// Synchronous Listen: a busy port fails Setup immediately instead of
			// dying silently in a goroutine (the P5 "silent telemetry loss" bug).
			ln, err := net.Listen("tcp", fmt.Sprintf(":%d", opts.PrometheusMetricsPort))
			if err != nil {
				return fail(fmt.Errorf(
					"prometheus /metrics listener on port %d: %w (if another process owns the port, mount otelhelper.MetricsHandler() and use WithoutMetricsListener())",
					opts.PrometheusMetricsPort, err))
			}
			mux := http.NewServeMux()
			mux.Handle("/metrics", promhttp.HandlerFor(reg, promhttp.HandlerOpts{}))
			srv := &http.Server{
				Handler:           mux,
				ReadHeaderTimeout: 5 * time.Second,
			}
			go func() {
				if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
					otel.Handle(fmt.Errorf("otelhelper: prometheus /metrics server: %w", err))
				}
			}()
			release = func(ctx context.Context) error {
				err := srv.Shutdown(ctx)
				// Shutdown racing a not-yet-started Serve would leave the port
				// bound until that goroutine runs; close it here so a retry of
				// Setup can bind it at once. A second Close is harmless.
				_ = ln.Close()
				return err
			}
		}
	}

	for _, r := range readers {
		mpOpts = append(mpOpts, sdkmetric.WithReader(r))
	}
	if len(opts.DisabledMetrics) > 0 {
		mpOpts = append(mpOpts, sdkmetric.WithView(disabledMetricsView(opts.DisabledMetrics)))
	}

	mp := sdkmetric.NewMeterProvider(mpOpts...)

	// Start runtime metrics (goroutines, GC, memory). Non-fatal if it fails.
	if err := runtime.Start(runtime.WithMeterProvider(mp)); err != nil {
		diagLogger.Warn("otelhelper: Go runtime metrics not started", "error", err)
	}

	return mp, release, registry, nil
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

// newOTLPMetricExporter mirrors newOTLPTraceExporter for metrics.
func newOTLPMetricExporter(ctx context.Context, e *signalExport) (sdkmetric.Exporter, error) {
	if e.protocol == ProtocolHTTP {
		var o []otlpmetrichttp.Option
		if e.endpointURL != "" {
			o = append(o, otlpmetrichttp.WithEndpointURL(e.endpointURL))
		}
		if e.timeout > 0 {
			o = append(o, otlpmetrichttp.WithTimeout(e.timeout))
		}
		if e.headers != nil {
			o = append(o, otlpmetrichttp.WithHeaders(e.headers))
		}
		if e.dropTLS {
			// nil clears the TLS config the SDK loaded from env (options are
			// applied after env); otherwise New() fails on an http:// endpoint.
			o = append(o, otlpmetrichttp.WithTLSClientConfig(nil))
		}
		if e.compression == CompressionNone {
			o = append(o, otlpmetrichttp.WithCompression(otlpmetrichttp.NoCompression))
		} else {
			o = append(o, otlpmetrichttp.WithCompression(otlpmetrichttp.GzipCompression))
		}
		return otlpmetrichttp.New(ctx, o...)
	}
	var o []otlpmetricgrpc.Option
	if e.endpointURL != "" {
		o = append(o, otlpmetricgrpc.WithEndpointURL(e.endpointURL))
	}
	if e.timeout > 0 {
		o = append(o, otlpmetricgrpc.WithTimeout(e.timeout))
	}
	if e.headers != nil {
		o = append(o, otlpmetricgrpc.WithHeaders(e.headers))
	}
	if e.dropTLS {
		// The SDK prefers env TLS credentials over Insecure: force plaintext.
		o = append(o, otlpmetricgrpc.WithTLSCredentials(insecure.NewCredentials()))
	}
	// The trace/metric gRPC exporters accept only "gzip" ("none" is reported
	// via otel.Handle as invalid) and default to none: CompressionNone means
	// no compressor option.
	if e.compression == CompressionNone {
		// "none" is not an accepted compressor name here; force identity so
		// an env gzip can't win over WithCompression("none").
		o = append(o, otlpmetricgrpc.WithDialOption(grpc.WithDefaultCallOptions(grpc.UseCompressor(encoding.Identity))))
	} else {
		o = append(o, otlpmetricgrpc.WithCompressor(CompressionGzip))
	}
	return otlpmetricgrpc.New(ctx, o...)
}
