package otelhelper

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func TestDefaultLogLevel(t *testing.T) {
	tests := []struct {
		env   DeploymentEnvironment
		debug bool
		want  slog.Level
	}{
		{LOCAL, false, slog.LevelDebug},
		{DEV, false, slog.LevelInfo},
		{HML, false, slog.LevelInfo},
		{PRD, false, slog.LevelWarn},
		{PRD, true, slog.LevelDebug},   // debug overrides everything
		{LOCAL, true, slog.LevelDebug},  // debug + LOCAL = Debug
		{DEV, true, slog.LevelDebug},    // debug overrides DEV
	}
	for _, tt := range tests {
		t.Run(string(tt.env), func(t *testing.T) {
			got := DefaultLogLevel(tt.env, tt.debug)
			if got != tt.want {
				t.Errorf("DefaultLogLevel(%s, %v) = %v, want %v", tt.env, tt.debug, got, tt.want)
			}
		})
	}
}

func TestNewHTTPTransportFiltersHealthPaths(t *testing.T) {
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sdktrace.NewSimpleSpanProcessor(exp)))
	defer tp.Shutdown(context.Background())
	otel.SetTracerProvider(tp)

	// Create a test backend server
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}))
	defer backend.Close()

	transport := NewHTTPTransport(nil)
	client := &http.Client{Transport: transport}

	// Health paths should be filtered (no client spans)
	for _, path := range []string{"/health", "/healthz", "/ready", "/ping"} {
		exp.Reset()
		req, _ := http.NewRequest("GET", backend.URL+path, nil)
		resp, err := client.Do(req)
		if err != nil {
			t.Fatalf("request to %s failed: %v", path, err)
		}
		resp.Body.Close()

		if len(exp.GetSpans()) != 0 {
			t.Errorf("Health path %s should be filtered, got %d spans", path, len(exp.GetSpans()))
		}
	}

	// Non-health path should create a span
	exp.Reset()
	req, _ := http.NewRequest("GET", backend.URL+"/api/data", nil)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("request failed: %v", err)
	}
	resp.Body.Close()

	if len(exp.GetSpans()) == 0 {
		t.Error("Non-health path should create a client span")
	}
}

func TestGRPCHealthFilter(t *testing.T) {
	// Test that the filter function correctly identifies health check methods
	healthInfo := &otelgrpc.InterceptorInfo{Method: "/grpc.health.v1.Health/Check"}
	if grpcHealthFilter(healthInfo) {
		t.Error("grpcHealthFilter should return false for health check method")
	}

	normalInfo := &otelgrpc.InterceptorInfo{Method: "/myservice.MyService/DoWork"}
	if !grpcHealthFilter(normalInfo) {
		t.Error("grpcHealthFilter should return true for non-health methods")
	}
}

func TestShutdownErrorJoining(t *testing.T) {
	// Simulate a shutdown function that joins multiple errors
	err1 := errors.New("trace shutdown failed")
	err2 := errors.New("log shutdown failed")

	joined := errors.Join(nil, err1, nil, err2)
	if joined == nil {
		t.Fatal("Expected joined error to be non-nil")
	}
	if !errors.Is(joined, err1) {
		t.Error("Joined error should contain err1")
	}
	if !errors.Is(joined, err2) {
		t.Error("Joined error should contain err2")
	}

	// All nil = no error
	allNil := errors.Join(nil, nil, nil)
	if allNil != nil {
		t.Error("All nil errors should join to nil")
	}
}
