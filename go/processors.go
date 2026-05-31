package otelhelper

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// debugProcessor sets debug=true attribute on root spans for Collector tail-sampling.
//
// Cross-language note: The .NET library also sets tracestate "debug=true" for Collector
// trace_state tail-sampling policy. The Go SDK ReadWriteSpan interface does NOT expose
// SetTraceState(), so we cannot set tracestate from a SpanProcessor. Python has the same
// limitation. The Collector MUST use string_attribute policy on "debug" key for
// cross-language debug trace force-sampling.
type debugProcessor struct{}

func (d *debugProcessor) OnStart(_ context.Context, s sdktrace.ReadWriteSpan) {
	if !s.Parent().IsValid() {
		s.SetAttributes(attribute.String("debug", "true"))
	}
}

func (d *debugProcessor) OnEnd(_ sdktrace.ReadOnlySpan)     {}
func (d *debugProcessor) Shutdown(_ context.Context) error   { return nil }
func (d *debugProcessor) ForceFlush(_ context.Context) error { return nil }
