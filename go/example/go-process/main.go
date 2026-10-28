package main

import (
	"context"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	otelhelper "github.com/staffops/staffops-otel-libs/go"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
)

var pendingAlerts atomic.Int64

func main() {
	// run returns instead of calling log.Fatal, so its deferred telemetry
	// flush always runs; os.Exit happens only after it.
	if err := run(); err != nil {
		log.Printf("%s: %v", "go-process", err)
		os.Exit(1)
	}
}

func run() error {
	// SIGTERM is what `docker stop` / Kubernetes send: without it the process
	// is killed and the deferred Shutdown never flushes buffered telemetry.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	shutdown, err := otelhelper.Setup(ctx)
	if err != nil {
		return fmt.Errorf("otel setup: %w", err)
	}
	defer func() {
		// ctx is already cancelled by the signal here: flush under a fresh deadline.
		flushCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := shutdown(flushCtx); err != nil {
			log.Printf("otel shutdown: %v", err)
		}
	}()

	tracer := otelhelper.GetTracer("go-process")
	// OTel-bridged slog logger (stdout JSON without a logs exporter): records
	// emitted inside a span carry its trace_id/span_id.
	logger := otelhelper.NewLogger(otelhelper.DeploymentEnvironment(os.Getenv("ENVIRONMENT")), false)

	var workers sync.WaitGroup
	start := func(name string, interval time.Duration, fn func(context.Context)) {
		workers.Add(1)
		go func() {
			defer workers.Done()
			worker(ctx, name, interval, fn)
		}()
	}
	meter := otelhelper.GetMeter("go-process")

	scannedMetrics, _ := meter.Int64Counter("process.metrics.scanned", metric.WithDescription("Total metrics scanned"))
	alertsDispatched, _ := meter.Int64Counter("process.alerts.dispatched", metric.WithDescription("Total alerts dispatched"))
	pendingAlertsGauge, _ := meter.Int64ObservableGauge("process.alerts.pending", metric.WithDescription("Pending alerts"))
	meter.RegisterCallback(func(_ context.Context, o metric.Observer) error {
		o.ObserveInt64(pendingAlertsGauge, pendingAlerts.Load())
		return nil
	}, pendingAlertsGauge)

	// Workers
	start("anomalyScanner", 30*time.Second, func(ctx context.Context) {
		ctx, span := otelhelper.StartRootSpan(ctx, tracer, "process.anomaly_scan")
		defer span.End()

		batch := rand.Intn(100) + 10
		span.SetAttributes(attribute.Int("batch.size", batch))
		time.Sleep(time.Duration(rand.Intn(200)) * time.Millisecond)
		scannedMetrics.Add(ctx, int64(batch))
		pendingAlerts.Add(int64(rand.Intn(5)))
		logger.InfoContext(ctx, "anomaly scan complete", "worker", "anomalyScanner", "batch.size", batch)
	})

	start("alertDispatcher", 60*time.Second, func(ctx context.Context) {
		ctx, span := otelhelper.StartRootSpan(ctx, tracer, "process.alert_dispatch")
		defer span.End()

		count := min(pendingAlerts.Load(), int64(rand.Intn(10)+1))
		pendingAlerts.Add(-count)
		span.SetAttributes(attribute.Int64("alerts.dispatched", count))
		time.Sleep(time.Duration(rand.Intn(100)) * time.Millisecond)
		alertsDispatched.Add(ctx, count)
		log.Printf("alertDispatcher: dispatched %d alerts", count)
	})

	start("cleanupWorker", 3*time.Minute, func(ctx context.Context) {
		_, span := otelhelper.StartRootSpan(ctx, tracer, "process.cleanup")
		defer span.End()

		cleaned := rand.Intn(20)
		span.SetAttributes(attribute.Int("detections.cleaned", cleaned))
		time.Sleep(time.Duration(rand.Intn(50)) * time.Millisecond)
		log.Printf("cleanupWorker: cleaned %d expired detections", cleaned)
	})

	// Health endpoint
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"status":"healthy"}`))
	})

	srv := &http.Server{Addr: ":8080", Handler: mux}
	srvErr := make(chan error, 1)
	go func() { srvErr <- srv.ListenAndServe() }()
	log.Println("go-process listening on :8080 (health only)")

	var runErr error
	select {
	case <-ctx.Done():
	case err := <-srvErr:
		runErr = err // e.g. port in use: still drain and flush below
	}

	// Drain in-flight requests (bounded), then let the deferred Shutdown flush
	// telemetry: in that order, spans of drained requests are exported too.
	drainCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(drainCtx); err != nil {
		log.Printf("http drain: %v", err)
	}
	workers.Wait() // finish in-flight iterations so their spans end before the flush
	return runErr
}

func worker(ctx context.Context, name string, interval time.Duration, fn func(context.Context)) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	log.Printf("%s: started (every %s)", name, interval)
	for {
		select {
		case <-ctx.Done():
			log.Printf("%s: stopped", name)
			return
		case <-ticker.C:
			fn(ctx)
		}
	}
}
