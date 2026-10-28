// Command go-custom-signals shows how to create your own telemetry on top of
// otelhelper.Setup: a batch root span with one child span per order
// (attributes, events, status and error recording), the four common metric
// instruments with attributes, and structured logs correlated with the active
// span.
//
// It processes one batch of three simulated orders (ord-3 fails), flushes
// everything with Shutdown and exits. The Python twin,
// python/example/python-custom-signals, emits the same names and the same trace
// shape. Point it at a Collector with OTEL_EXPORTER_OTLP_ENDPOINT; without one,
// logs go to stdout as JSON and metrics to the Prometheus /metrics listener
// (gone when the process exits).
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"log/slog"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
	"time"

	otelhelper "github.com/staffops/staffops-otel-libs/go"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"
)

// scope is the instrumentation scope name of the tracer and the meter.
const scope = "orders"

// PaymentDeclinedError is the business error of a failed charge. charge returns
// it unwrapped, so the span's exception.type names this type
// (*main.PaymentDeclinedError) instead of a wrapper such as *fmt.wrapError.
type PaymentDeclinedError struct {
	OrderID string
}

func (e *PaymentDeclinedError) Error() string { return "card declined by issuer" }

type order struct {
	id, kind string
	amount   float64
	fail     bool
}

// instruments groups the custom metrics.
type instruments struct {
	processed metric.Int64Counter       // order.processed: monotonic count of finished orders
	duration  metric.Float64Histogram   // order.duration: processing time distribution
	active    metric.Int64UpDownCounter // order.active: orders being processed now
}

// queueDepth backs the observable gauge: the SDK reads it at each collection.
var queueDepth atomic.Int64

func main() {
	if err := run(); err != nil {
		log.Printf("go-custom-signals: %v", err)
		os.Exit(1)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	shutdown, err := otelhelper.Setup(ctx)
	if err != nil {
		return fmt.Errorf("otel setup: %w", err)
	}
	defer func() {
		// Flush under a fresh deadline: ctx may already be cancelled by a signal.
		flushCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := shutdown(flushCtx); err != nil {
			log.Printf("otel shutdown: %v", err)
		}
	}()

	// The helper handler goes through the OTel bridge while a logs exporter is
	// installed, and writes JSON to stdout otherwise; both at the level Setup
	// resolved from deployment.environment.name.
	logger := slog.New(otelhelper.NewSlogHandler())

	ins, err := newInstruments()
	if err != nil {
		return err
	}

	orders := []order{
		{id: "ord-1", kind: "standard", amount: 42.5},
		{id: "ord-2", kind: "express", amount: 99.9},
		{id: "ord-3", kind: "standard", amount: 10, fail: true},
	}
	queueDepth.Store(int64(len(orders)))

	// A root span starts a new trace, whatever span ctx carries (workers,
	// consumers, cron jobs: one trace per unit of work).
	tracer := otelhelper.GetTracer(scope)
	ctx, batch := otelhelper.StartRootSpan(ctx, tracer, "orders.process-batch",
		trace.WithAttributes(attribute.Int("batch.size", len(orders))))
	fmt.Printf("trace_id=%s\n", batch.SpanContext().TraceID())

	failed := 0
	for _, o := range orders {
		if ctx.Err() != nil { // SIGTERM / Ctrl-C: stop taking new orders, still flush
			logger.WarnContext(ctx, "batch interrupted", "orders.pending", queueDepth.Load())
			break
		}
		if err := processOrder(ctx, tracer, logger, ins, o); err != nil {
			failed++
		}
		queueDepth.Add(-1)
	}

	if failed > 0 {
		// Status only: the error itself was recorded on the child span.
		batch.SetStatus(codes.Error, fmt.Sprintf("%d of %d orders failed", failed, len(orders)))
	}
	logger.InfoContext(ctx, "batch finished", "batch.size", len(orders), "orders.failed", failed)
	batch.End()
	return nil
}

func newInstruments() (*instruments, error) {
	meter := otelhelper.GetMeter(scope)

	processed, err := meter.Int64Counter("order.processed",
		metric.WithDescription("Orders processed"),
		metric.WithUnit("{order}"))
	if err != nil {
		return nil, err
	}
	// Second-based histograms need their own boundaries: the SDK defaults
	// (0, 5, 10, ... 10000) suit milliseconds.
	duration, err := meter.Float64Histogram("order.duration",
		metric.WithDescription("Time spent processing one order"),
		metric.WithUnit("s"),
		metric.WithExplicitBucketBoundaries(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1))
	if err != nil {
		return nil, err
	}
	active, err := meter.Int64UpDownCounter("order.active",
		metric.WithDescription("Orders being processed now"),
		metric.WithUnit("{order}"))
	if err != nil {
		return nil, err
	}
	depth, err := meter.Int64ObservableGauge("order.queue.depth",
		metric.WithDescription("Orders waiting in the queue"),
		metric.WithUnit("{order}"))
	if err != nil {
		return nil, err
	}
	// The callback runs at every collection (export interval, scrape, Shutdown).
	if _, err := meter.RegisterCallback(func(_ context.Context, o metric.Observer) error {
		o.ObserveInt64(depth, queueDepth.Load(), metric.WithAttributes(attribute.String("queue.name", "orders")))
		return nil
	}, depth); err != nil {
		return nil, err
	}
	return &instruments{processed: processed, duration: duration, active: active}, nil
}

// processOrder runs in a child span of the batch span.
func processOrder(ctx context.Context, tracer trace.Tracer, logger *slog.Logger, ins *instruments, o order) error {
	ctx, span := tracer.Start(ctx, "order.process",
		trace.WithAttributes(
			attribute.String("order.id", o.id), // fine on spans; never as a metric attribute
			attribute.String("order.type", o.kind),
		))
	defer span.End()

	start := time.Now()
	typeAttr := attribute.String("order.type", o.kind)
	ins.active.Add(ctx, 1, metric.WithAttributes(typeAttr))
	defer ins.active.Add(ctx, -1, metric.WithAttributes(typeAttr))

	// An event is a timestamped annotation inside the span.
	span.AddEvent("order.validated", trace.WithAttributes(attribute.Float64("order.amount", o.amount)))
	time.Sleep(20 * time.Millisecond) // simulated call to a payment provider

	attrs := []attribute.KeyValue{typeAttr}
	err := charge(o)
	if err != nil {
		// RecordError adds an "exception" event; SetStatus marks the span failed.
		span.RecordError(err)
		span.SetStatus(codes.Error, "payment declined")
		// error.type only on failure, low-cardinality: the error's type name.
		attrs = append(attrs, attribute.String("error.type", errorType(err)))
		// The *Context call carries the span: the record gets its trace_id/span_id.
		logger.ErrorContext(ctx, "order failed", "order.id", o.id, "err", err)
	} else {
		logger.InfoContext(ctx, "order processed", "order.id", o.id, "order.amount", o.amount)
	}

	ins.processed.Add(ctx, 1, metric.WithAttributes(attrs...))
	ins.duration.Record(ctx, time.Since(start).Seconds(), metric.WithAttributes(attrs...))
	return err
}

func charge(o order) error {
	if o.fail {
		return &PaymentDeclinedError{OrderID: o.id}
	}
	return nil
}

// errorType maps an error to a bounded error.type value.
func errorType(err error) string {
	var declined *PaymentDeclinedError
	if errors.As(err, &declined) {
		return "PaymentDeclinedError"
	}
	return "_OTHER"
}
