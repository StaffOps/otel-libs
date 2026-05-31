package otelhelper

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func newTestProvider(t *testing.T, processors ...sdktrace.SpanProcessor) (*tracetest.InMemoryExporter, trace.Tracer) {
	t.Helper()
	exp := tracetest.NewInMemoryExporter()
	opts := []sdktrace.TracerProviderOption{
		sdktrace.WithSpanProcessor(sdktrace.NewSimpleSpanProcessor(exp)),
	}
	for _, p := range processors {
		opts = append(opts, sdktrace.WithSpanProcessor(p))
	}
	tp := sdktrace.NewTracerProvider(opts...)
	t.Cleanup(func() { tp.Shutdown(context.Background()) })
	return exp, tp.Tracer("test")
}

func TestStartRootSpanCreatesNewTrace(t *testing.T) {
	exp, tracer := newTestProvider(t)

	// Create a parent span
	ctx, parent := tracer.Start(context.Background(), "parent")
	parentTraceID := parent.SpanContext().TraceID()

	// StartRootSpan should create a new independent trace
	_, child := StartRootSpan(ctx, tracer, "root-child")
	child.End()
	parent.End()

	spans := exp.GetSpans()
	if len(spans) != 2 {
		t.Fatalf("Expected 2 spans, got %d", len(spans))
	}

	// Find the root-child span
	var rootChild tracetest.SpanStub
	for _, s := range spans {
		if s.Name == "root-child" {
			rootChild = s
		}
	}

	if rootChild.SpanContext.TraceID() == parentTraceID {
		t.Error("StartRootSpan should create a new trace ID, got same as parent")
	}
	if rootChild.Parent.IsValid() {
		t.Error("StartRootSpan should have no valid parent")
	}
}

func TestDebugProcessorSetsAttributeOnRoot(t *testing.T) {
	exp, tracer := newTestProvider(t, &debugProcessor{})

	_, span := tracer.Start(context.Background(), "root-span")
	span.End()

	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("Expected 1 span, got %d", len(spans))
	}

	found := false
	for _, attr := range spans[0].Attributes {
		if attr.Key == "debug" && attr.Value == attribute.StringValue("true") {
			found = true
		}
	}
	if !found {
		t.Error("Root span should have debug=true attribute")
	}
}

func TestDebugProcessorSkipsChildSpans(t *testing.T) {
	exp, tracer := newTestProvider(t, &debugProcessor{})

	ctx, parent := tracer.Start(context.Background(), "parent")
	_, child := tracer.Start(ctx, "child")
	child.End()
	parent.End()

	spans := exp.GetSpans()
	for _, s := range spans {
		if s.Name == "child" {
			for _, attr := range s.Attributes {
				if attr.Key == "debug" {
					t.Error("Child span should NOT have debug attribute")
				}
			}
		}
	}
}
