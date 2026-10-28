package otelhelper

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// debugProcessor marks the service's entry spans with debug=true so the
// Collector's tail sampler (string_attribute policy on "debug") keeps the
// trace (ADR-006). An entry span is one with no parent or a remote parent;
// spans with a local parent are left alone.
//
// It is installed only when debug mode is on in local config
// (OTEL_HELPER_DEBUG_LEVEL=true or WithDebug): the marker is never derived
// from inbound tracestate or baggage — a caller must not be able to force
// retention — and the processor never writes tracestate.
type debugProcessor struct{}

func (d *debugProcessor) OnStart(_ context.Context, s sdktrace.ReadWriteSpan) {
	if p := s.Parent(); !p.IsValid() || p.IsRemote() {
		s.SetAttributes(attribute.String("debug", "true"))
	}
}

func (d *debugProcessor) OnEnd(_ sdktrace.ReadOnlySpan)      {}
func (d *debugProcessor) Shutdown(_ context.Context) error   { return nil }
func (d *debugProcessor) ForceFlush(_ context.Context) error { return nil }
