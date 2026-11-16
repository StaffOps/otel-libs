package demokit

import (
	"go.opentelemetry.io/otel/metric"
)

// DurationBuckets are the demo.order.duration boundaries in seconds (DE-D9).
var DurationBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5}

// Instruments are the backend's custom metrics (DE-D9).
type Instruments struct {
	// Processed is demo.orders.processed, unit {order}, integer.
	Processed metric.Int64Counter
	// Duration is demo.order.duration, unit s, DurationBuckets.
	Duration metric.Float64Histogram
}

// NewInstruments creates the custom instruments on meter.
func NewInstruments(meter metric.Meter) (*Instruments, error) {
	processed, err := meter.Int64Counter("demo.orders.processed",
		metric.WithDescription("Orders processed by the demo backend"),
		metric.WithUnit("{order}"))
	if err != nil {
		return nil, err
	}
	duration, err := meter.Float64Histogram("demo.order.duration",
		metric.WithDescription("Time the demo backend spent on one order"),
		metric.WithUnit("s"),
		metric.WithExplicitBucketBoundaries(DurationBuckets...))
	if err != nil {
		return nil, err
	}
	return &Instruments{Processed: processed, Duration: duration}, nil
}
