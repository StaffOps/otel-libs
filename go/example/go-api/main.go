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
	"time"

	otelhelper "github.com/karlipegomes/staffops-otel-libs/go"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
)

var (
	detectionCounter atomic.Int64
	backendURL       = envOr("BACKEND_URL", "http://localhost:50051")
	httpClient       = &http.Client{Transport: otelhelper.NewHTTPTransport(nil)}
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	shutdown, err := otelhelper.Setup(ctx)
	if err != nil {
		log.Fatalf("otel setup: %v", err)
	}
	defer shutdown(ctx)

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
		_, span := tracer.Start(r.Context(), "detect.status")
		defer span.End()
		span.SetAttributes(attribute.String("detection.id", id))

		requestCount.Add(r.Context(), 1, metric.WithAttributes(attribute.String("endpoint", "status")))

		// Call backend
		req, _ := http.NewRequestWithContext(r.Context(), http.MethodGet, backendURL+"/status/"+id, nil)
		resp, err := httpClient.Do(req)
		if err != nil {
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
			"uptime_seconds":   time.Since(time.Now()).Seconds(),
		})
	}), "GET /metrics/summary"))

	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"healthy"}`))
	})

	srv := &http.Server{Addr: ":8080", Handler: mux}
	go func() {
		<-ctx.Done()
		srv.Shutdown(context.Background())
	}()

	log.Println("go-api listening on :8080")
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		log.Fatal(err)
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); strings.TrimSpace(v) != "" {
		return v
	}
	return def
}
