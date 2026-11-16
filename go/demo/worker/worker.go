package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"

	otelhelper "github.com/staffops/otel-libs/go"
	"github.com/staffops/otel-libs/go/demo/internal/demokit"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

// message is one stream entry.
type message struct {
	ID     string
	Values map[string]any
}

// stream is the worker's queue access (an interface so the consumer logic is
// testable without Redis).
type stream interface {
	CreateGroup(ctx context.Context) error
	Read(ctx context.Context) ([]message, error)
	Ack(ctx context.Context, id string) error
}

type worker struct {
	logger    *slog.Logger
	tracer    trace.Tracer
	http      *http.Client
	apiURL    string
	stream    stream
	redisHost string
	redisPort int
}

// drive sends the seq-th order of the cycle in a new root span demo.drive.
// A non-2xx answer sets ERROR with error.type = the status code and
// demo.outcome=error, records no exception event (none occurs here) and logs
// one WARN record (DE-D8).
func (w *worker) drive(ctx context.Context, seq uint64) {
	order := demokit.OrderFor(seq)
	ctx, span := otelhelper.StartRootSpan(ctx, w.tracer, "demo.drive",
		trace.WithAttributes(demokit.Scenario(order.Scenario)))
	defer span.End()

	body, _ := json.Marshal(order)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, w.apiURL+"/orders", bytes.NewReader(body))
	if err != nil {
		w.fail(ctx, span, demokit.TypeName(err), 0)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := w.http.Do(req)
	if err != nil {
		// error.type = the root error's type name, e.g. *net.OpError (DE-D7 r3.5).
		w.fail(ctx, span, demokit.TypeName(err), 0)
		return
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		w.fail(ctx, span, strconv.Itoa(resp.StatusCode), resp.StatusCode)
		return
	}
	span.SetAttributes(demokit.Outcome(false))
}

// fail marks demo.drive failed; statusCode 0 means no answer arrived.
func (w *worker) fail(ctx context.Context, span trace.Span, errorType string, statusCode int) {
	span.SetStatus(codes.Error, "")
	span.SetAttributes(attribute.String("error.type", errorType), demokit.Outcome(true))
	if statusCode == 0 {
		w.logger.WarnContext(ctx, "order request failed", "error.type", errorType)
		return
	}
	w.logger.WarnContext(ctx, "order request failed", "http.response.status_code", statusCode)
}

// poll reads up to ten entries (a root Redis span per poll, DE-D5) and
// processes each in its own CONSUMER span.
func (w *worker) poll(ctx context.Context) {
	msgs, err := w.stream.Read(ctx)
	if err != nil {
		w.logger.WarnContext(ctx, "stream read failed", "error", err.Error())
		if strings.HasPrefix(err.Error(), "NOGROUP") {
			_ = w.stream.CreateGroup(ctx) // the stream or group was deleted: recreate it
		}
		return
	}
	for _, m := range msgs {
		w.process(m)
	}
}

// process handles one message (DE-D6): the context extracted from the
// message fields is the CONSUMER span's parent and its one link; the span
// starts outside any other active span and the message is acknowledged inside
// it.
func (w *worker) process(m message) {
	carrier := propagation.MapCarrier{}
	for k, v := range m.Values {
		if s, ok := v.(string); ok {
			carrier[k] = s
		}
	}
	parent := otel.GetTextMapPropagator().Extract(context.Background(), carrier)
	ctx, span := w.tracer.Start(parent, "process "+demokit.StreamKey,
		trace.WithSpanKind(trace.SpanKindConsumer),
		trace.WithLinks(trace.LinkFromContext(parent)),
		trace.WithAttributes(
			attribute.String("messaging.system", "redis"),
			attribute.String("messaging.operation.name", "process"),
			attribute.String("messaging.operation.type", "process"),
			attribute.String("messaging.destination.name", demokit.StreamKey),
			attribute.String("messaging.message.id", m.ID),
			attribute.String("messaging.consumer.group.name", demokit.ConsumerGroup),
			attribute.String("server.address", w.redisHost),
			attribute.Int("server.port", w.redisPort),
		))
	defer span.End()

	orderID, _ := m.Values["order_id"].(string)
	scenario, _ := m.Values["scenario"].(string)
	w.logger.InfoContext(ctx, "order event processed", "order.id", orderID, "demo.scenario", scenario)
	if err := w.stream.Ack(ctx, m.ID); err != nil {
		span.SetStatus(codes.Error, "")
		span.SetAttributes(attribute.String("error.type", demokit.TypeName(err)))
		w.logger.WarnContext(ctx, "order event not acknowledged", "error", err.Error())
	}
}
