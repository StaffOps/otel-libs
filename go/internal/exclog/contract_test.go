package exclog

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

// The cross-module contract (package documentation): ext modules of another
// release call these. A changed signature fails this file at compile time;
// within a major version names may only be added.
var (
	_ func(context.Context, error, []attribute.KeyValue) string  = Emit
	_ func(trace.TracerProvider, ...Option) trace.TracerProvider = TracerProvider
	_ func(...attribute.KeyValue) Option                         = WithRecordAttributes
	_ func(func(error) []attribute.KeyValue) Option              = WithErrorAttributes
	_ func(*config)                                              = Option(nil)
)
