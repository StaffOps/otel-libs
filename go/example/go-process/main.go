package main

import (
	"context"
	"log"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"sync/atomic"
	"time"

	otelhelper "github.com/staffops/staffops-otel-libs/go"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
)

var pendingAlerts atomic.Int64

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	shutdown, err := otelhelper.Setup(ctx)
	if err != nil {
		log.Fatalf("otel setup: %v", err)
	}
	defer shutdown(ctx)

	tracer := otelhelper.GetTracer("go-process")
	meter := otelhelper.GetMeter("go-process")

	scannedMetrics, _ := meter.Int64Counter("process.metrics.scanned", metric.WithDescription("Total metrics scanned"))
	alertsDispatched, _ := meter.Int64Counter("process.alerts.dispatched", metric.WithDescription("Total alerts dispatched"))
	pendingAlertsGauge, _ := meter.Int64ObservableGauge("process.alerts.pending", metric.WithDescription("Pending alerts"))
	meter.RegisterCallback(func(_ context.Context, o metric.Observer) error {
		o.ObserveInt64(pendingAlertsGauge, pendingAlerts.Load())
		return nil
	}, pendingAlertsGauge)

	// Workers
	go worker(ctx, "anomalyScanner", 30*time.Second, func(ctx context.Context) {
		ctx, span := otelhelper.StartRootSpan(ctx, tracer, "process.anomaly_scan")
		defer span.End()

		batch := rand.Intn(100) + 10
		span.SetAttributes(attribute.Int("batch.size", batch))
		time.Sleep(time.Duration(rand.Intn(200)) * time.Millisecond)
		scannedMetrics.Add(ctx, int64(batch))
		pendingAlerts.Add(int64(rand.Intn(5)))
		log.Printf("anomalyScanner: scanned %d metrics", batch)
	})

	go worker(ctx, "alertDispatcher", 60*time.Second, func(ctx context.Context) {
		ctx, span := otelhelper.StartRootSpan(ctx, tracer, "process.alert_dispatch")
		defer span.End()

		count := min(pendingAlerts.Load(), int64(rand.Intn(10)+1))
		pendingAlerts.Add(-count)
		span.SetAttributes(attribute.Int64("alerts.dispatched", count))
		time.Sleep(time.Duration(rand.Intn(100)) * time.Millisecond)
		alertsDispatched.Add(ctx, count)
		log.Printf("alertDispatcher: dispatched %d alerts", count)
	})

	go worker(ctx, "cleanupWorker", 3*time.Minute, func(ctx context.Context) {
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
	go func() {
		<-ctx.Done()
		srv.Shutdown(context.Background())
	}()

	log.Println("go-process listening on :8080 (health only)")
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		log.Fatal(err)
	}
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
