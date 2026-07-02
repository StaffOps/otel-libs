// go-backend is a net/http server on :50051 simulating a backend service.
// Distributed tracing works via HTTP context propagation (W3C TraceContext).
//
// For real gRPC usage, you would use:
//
//	srv := grpc.NewServer(
//	    grpc.UnaryInterceptor(otelhelper.UnaryServerInterceptor()),
//	)
//
// And on the client side:
//
//	conn, _ := grpc.Dial(addr,
//	    grpc.WithUnaryInterceptor(otelhelper.UnaryClientInterceptor()),
//	)
package main

import (
	"context"
	"encoding/json"
	"log"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"time"

	otelhelper "github.com/staffops/staffops-otel-libs/go"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	shutdown, err := otelhelper.Setup(ctx)
	if err != nil {
		log.Fatalf("otel setup: %v", err)
	}
	defer shutdown(ctx)

	tracer := otelhelper.GetTracer("go-backend")
	meter := otelhelper.GetMeter("go-backend")

	processingDuration, _ := meter.Float64Histogram("backend.processing.duration_ms", metric.WithDescription("Detection processing duration"))

	mux := http.NewServeMux()

	mux.Handle("POST /detect", otelhelper.NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctx, span := tracer.Start(r.Context(), "backend.detect")
		defer span.End()

		// Simulate anomaly detection with random latency 50-500ms
		latency := time.Duration(50+rand.Intn(450)) * time.Millisecond
		time.Sleep(latency)

		anomalyDetected := rand.Float64() > 0.7
		span.SetAttributes(
			attribute.Bool("anomaly.detected", anomalyDetected),
			attribute.Int64("processing.latency_ms", latency.Milliseconds()),
		)
		processingDuration.Record(ctx, float64(latency.Milliseconds()))

		json.NewEncoder(w).Encode(map[string]any{
			"anomaly_detected": anomalyDetected,
			"confidence":       rand.Float64(),
			"latency_ms":       latency.Milliseconds(),
		})
	}), "POST /detect"))

	mux.Handle("GET /status/{id}", otelhelper.NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("id")
		_, span := tracer.Start(r.Context(), "backend.get_status")
		defer span.End()
		span.SetAttributes(attribute.String("detection.id", id))

		statuses := []string{"pending", "processing", "completed", "failed"}
		json.NewEncoder(w).Encode(map[string]string{
			"id":     id,
			"status": statuses[rand.Intn(len(statuses))],
		})
	}), "GET /status"))

	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"status":"healthy"}`))
	})

	srv := &http.Server{Addr: ":50051", Handler: mux}
	go func() {
		<-ctx.Done()
		srv.Shutdown(context.Background())
	}()

	log.Println("go-backend listening on :50051")
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
