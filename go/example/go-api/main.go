package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	otelhelper "github.com/staffops/staffops-otel-libs/go"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
)

var (
	detectionCounter atomic.Int64
	backendURL       = envOr("BACKEND_URL", "http://localhost:50051")
	httpClient       = &http.Client{Transport: otelhelper.NewHTTPTransport(nil)}
)

func main() {
	// run returns instead of calling log.Fatal, so its deferred telemetry
	// flush always runs; os.Exit happens only after it.
	if err := run(); err != nil {
		log.Printf("%s: %v", "go-api", err)
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

	startedAt := time.Now()
	tracer := otelhelper.GetTracer("go-api")
	meter := otelhelper.GetMeter("go-api")

	requestCount, _ := meter.Int64Counter("api.requests.total", metric.WithDescription("Total API requests"))
	requestDuration, _ := meter.Float64Histogram("api.request.duration_ms", metric.WithDescription("Request duration in ms"))

	mux := http.NewServeMux()

	mux.Handle("POST /detect", otelhelper.NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		ctx, span := tracer.Start(r.Context(), "detect.submit")
		defer span.End()

		requestCount.Add(ctx, 1, metric.WithAttributes(attribute.String("endpoint", "detect")))

		// Call backend
		body, _ := json.Marshal(map[string]any{"source": "api", "ts": time.Now().Unix()})
		req, _ := http.NewRequestWithContext(ctx, http.MethodPost, backendURL+"/detect", bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")

		resp, err := httpClient.Do(req)
		if err != nil {
			span.RecordError(err)
			span.SetStatus(codes.Error, "backend call failed")
			http.Error(w, "backend error", http.StatusBadGateway)
			return
		}
		defer resp.Body.Close()

		id := fmt.Sprintf("det-%d", detectionCounter.Add(1))
		span.SetAttributes(attribute.String("detection.id", id))

		requestDuration.Record(ctx, float64(time.Since(start).Milliseconds()))
		json.NewEncoder(w).Encode(map[string]string{"id": id, "status": "submitted"})
	}), "POST /detect"))

	mux.Handle("GET /status/{id}", otelhelper.NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("id")
		ctx, span := tracer.Start(r.Context(), "detect.status")
		defer span.End()
		span.SetAttributes(attribute.String("detection.id", id))

		requestCount.Add(ctx, 1, metric.WithAttributes(attribute.String("endpoint", "status")))

		// Call backend with the span's ctx, so the client span is its child.
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, backendURL+"/status/"+id, nil)
		resp, err := httpClient.Do(req)
		if err != nil {
			span.RecordError(err)
			span.SetStatus(codes.Error, "backend call failed")
			http.Error(w, "backend error", http.StatusBadGateway)
			return
		}
		defer resp.Body.Close()

		var result map[string]any
		json.NewDecoder(resp.Body).Decode(&result)
		json.NewEncoder(w).Encode(result)
	}), "GET /status"))

	mux.Handle("GET /metrics/summary", otelhelper.NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{
			"total_detections": detectionCounter.Load(),
			"uptime_seconds":   time.Since(startedAt).Seconds(),
		})
	}), "GET /metrics/summary"))

	// Wrapped so probes count in http.server.request.duration (failing probes
	// stay visible); the helper sampler drops their spans.
	mux.Handle("GET /health", otelhelper.NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"healthy"}`))
	}), "GET /health"))

	srv := &http.Server{Addr: ":8080", Handler: mux}
	srvErr := make(chan error, 1)
	go func() { srvErr <- srv.ListenAndServe() }()
	log.Println("go-api listening on :8080")

	var runErr error
	select {
	case <-ctx.Done():
	case err := <-srvErr:
		runErr = err // e.g. port in use: still drain and flush below
	}

	// Drain in-flight requests (bounded), then let the deferred Shutdown flush
	// telemetry: in that order, spans of drained requests are exported too.
	// Drain 4 s + flush 5 s stays inside docker stop's default 10 s grace period.
	drainCtx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	if err := srv.Shutdown(drainCtx); err != nil {
		log.Printf("http drain: %v", err)
	}
	return runErr
}

func envOr(key, def string) string {
	if v := os.Getenv(key); strings.TrimSpace(v) != "" {
		return v
	}
	return def
}
