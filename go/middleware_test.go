package otelhelper

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func TestHTTPHandlerFiltersHealthPaths(t *testing.T) {
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sdktrace.NewSimpleSpanProcessor(exp)))
	defer tp.Shutdown(context.Background())
	otel.SetTracerProvider(tp)

	handler := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}), "test-server")

	for _, path := range []string{"/health", "/healthz", "/ready", "/ping"} {
		exp.Reset()
		req := httptest.NewRequest("GET", path, nil)
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)

		if len(exp.GetSpans()) != 0 {
			t.Errorf("Path %s should be filtered (no spans), got %d spans", path, len(exp.GetSpans()))
		}
	}
}

func TestHTTPHandlerCreatesSpanForNonHealth(t *testing.T) {
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sdktrace.NewSimpleSpanProcessor(exp)))
	defer tp.Shutdown(context.Background())
	otel.SetTracerProvider(tp)

	handler := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}), "test-server")

	req := httptest.NewRequest("GET", "/api/data", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if len(exp.GetSpans()) == 0 {
		t.Error("Non-health path should create a span")
	}
}
