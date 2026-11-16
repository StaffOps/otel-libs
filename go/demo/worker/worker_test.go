package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/staffops/otel-libs/go/demo/internal/demokit"
	"github.com/staffops/otel-libs/go/demo/internal/demotest"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelcodes "go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

type fakeStream struct {
	ackSpan trace.SpanContext
	msgs    []message
	readErr error
	ackErr  error
	acked   []string
	created int
}

func (s *fakeStream) CreateGroup(context.Context) error { s.created++; return nil }

func (s *fakeStream) Read(context.Context) ([]message, error) { return s.msgs, s.readErr }

func (s *fakeStream) Ack(ctx context.Context, id string) error {
	s.acked = append(s.acked, id)
	s.ackSpan = trace.SpanContextFromContext(ctx)
	return s.ackErr
}

func newWorker(t *testing.T, apiURL string) (*worker, *tracetest.SpanRecorder, *demotest.Logs, *fakeStream) {
	t.Helper()
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	tracer, rec := demotest.Tracer()
	logs := &demotest.Logs{}
	st := &fakeStream{}
	return &worker{logger: logs.Logger(), tracer: tracer, http: http.DefaultClient, apiURL: apiURL, stream: st, redisHost: "demo-redis", redisPort: 6379}, rec, logs, st
}

func TestDriveMapsAnswers(t *testing.T) {
	var got demokit.OrderRequest
	answers := map[string]int{"ok": 201, "business_error": 422, "db_error": 409, "unhandled": 500}
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		w.WriteHeader(answers[got.Scenario])
	}))
	defer api.Close()

	for seq, want := range map[uint64]struct {
		scenario, errType string
	}{1: {"ok", ""}, 3: {"business_error", "422"}, 6: {"db_error", "409"}, 9: {"unhandled", "500"}} {
		w, rec, logs, _ := newWorker(t, api.URL)
		w.drive(context.Background(), seq)
		s := rec.Ended()[0]
		if s.Name() != "demo.drive" || s.Parent().IsValid() || got.Scenario != want.scenario {
			t.Errorf("seq %d: span %s parent=%v body=%+v", seq, s.Name(), s.Parent(), got)
		}
		attrs := map[string]string{}
		for _, kv := range s.Attributes() {
			attrs[string(kv.Key)] = kv.Value.Emit()
		}
		if attrs["demo.scenario"] != want.scenario {
			t.Errorf("seq %d: demo.scenario = %q", seq, attrs["demo.scenario"])
		}
		if want.errType == "" {
			if s.Status().Code == otelcodes.Error || attrs["demo.outcome"] != "ok" || len(logs.Records()) != 0 {
				t.Errorf("seq %d: ok must not fail: %v %v", seq, s.Status(), attrs)
			}
			continue
		}
		if s.Status().Code != otelcodes.Error || s.Status().Description != "" || attrs["error.type"] != want.errType || attrs["demo.outcome"] != "error" || len(s.Events()) != 0 {
			t.Errorf("seq %d: status=%v attrs=%v events=%d", seq, s.Status(), attrs, len(s.Events()))
		}
		r := logs.Records()
		if len(r) != 1 || r[0].Message != "order request failed" || r[0].Attrs["http.response.status_code"] != want.errType {
			t.Errorf("seq %d: logs = %+v", seq, r)
		}
	}
}

func TestDriveTransportError(t *testing.T) {
	w, rec, logs, _ := newWorker(t, "http://127.0.0.1:1")
	w.http = &http.Client{Timeout: time.Second}
	w.drive(context.Background(), 1)
	s := rec.Ended()[0]
	et := errorTypeOf(s.Attributes())
	if s.Status().Code != otelcodes.Error || len(logs.Records()) != 1 || et != "syscall.Errno" {
		t.Errorf("status=%v error.type=%q logs=%v", s.Status(), et, logs.Records())
	}

	// An invalid URL fails before any request: still ERROR, error.type the root
	// error's type name (connection refused above is syscall.Errno).
	w.apiURL = "http://bad host"
	w.drive(context.Background(), 2)
	if n := len(rec.Ended()); n != 2 {
		t.Fatalf("spans = %d, want 2", n)
	}
	s2 := rec.Ended()[1]
	if et := errorTypeOf(s2.Attributes()); s2.Status().Code != otelcodes.Error || et != "url.InvalidHostError" {
		t.Errorf("second drive: status=%v error.type=%q", s2.Status(), et)
	}
	if len(logs.Records()) != 2 {
		t.Errorf("logs = %d, want 2", len(logs.Records()))
	}
}

func errorTypeOf(kvs []attribute.KeyValue) string {
	for _, kv := range kvs {
		if kv.Key == "error.type" {
			return kv.Value.AsString()
		}
	}
	return ""
}

func TestProcessUsesTheProducerAsParentAndLink(t *testing.T) {
	w, rec, logs, st := newWorker(t, "")
	tracer, _ := demotest.Tracer()
	pctx, producer := tracer.Start(context.Background(), "send demo:go:orders", trace.WithSpanKind(trace.SpanKindProducer))
	carrier := propagation.MapCarrier{}
	otel.GetTextMapPropagator().Inject(pctx, carrier)
	producer.End()
	values := map[string]any{"order_id": "ok-1", "scenario": "ok"}
	for k, v := range carrier {
		values[k] = v
	}
	st.msgs = []message{{ID: "1-0", Values: values}}

	w.poll(context.Background())

	s := rec.Ended()[0]
	psc := producer.SpanContext()
	if s.Name() != "process demo:go:orders" || s.SpanKind() != trace.SpanKindConsumer {
		t.Errorf("span %s kind %v", s.Name(), s.SpanKind())
	}
	if s.Parent().SpanID() != psc.SpanID() || s.SpanContext().TraceID() != psc.TraceID() {
		t.Error("the producer context must be the parent")
	}
	if len(s.Links()) != 1 || s.Links()[0].SpanContext.SpanID() != psc.SpanID() {
		t.Errorf("links = %+v, want one link to the producer", s.Links())
	}
	attrs := map[string]string{}
	for _, kv := range s.Attributes() {
		attrs[string(kv.Key)] = kv.Value.Emit()
	}
	if attrs["messaging.operation.type"] != "process" || attrs["messaging.message.id"] != "1-0" || attrs["messaging.consumer.group.name"] != "demo-workers" || attrs["messaging.destination.name"] != "demo:go:orders" {
		t.Errorf("attrs = %v", attrs)
	}
	if len(st.acked) != 1 || st.acked[0] != "1-0" {
		t.Errorf("acked = %v", st.acked)
	}
	if st.ackSpan.SpanID() != s.SpanContext().SpanID() {
		t.Error("XACK must run inside the CONSUMER span")
	}
	r := logs.Records()
	if len(r) != 1 || r[0].Message != "order event processed" || r[0].SpanContext.SpanID() != s.SpanContext().SpanID() || r[0].Attrs["order.id"] != "ok-1" || r[0].Attrs["demo.scenario"] != "ok" {
		t.Errorf("logs = %+v", r)
	}
}

func TestPollFailures(t *testing.T) {
	w, rec, logs, st := newWorker(t, "")
	st.readErr = errors.New("NOGROUP No such key")
	w.poll(context.Background())
	if st.created != 1 || len(logs.Records()) != 1 {
		t.Errorf("NOGROUP must recreate the group: created=%d", st.created)
	}

	st.readErr = nil
	st.ackErr = errors.New("down")
	st.msgs = []message{{ID: "2-0", Values: map[string]any{}}} // no context: a root consumer span
	w.poll(context.Background())
	s := rec.Ended()[0]
	if s.Status().Code != otelcodes.Error || s.Parent().IsValid() || len(s.Links()) != 0 {
		t.Errorf("status=%v parent=%v links=%v", s.Status(), s.Parent(), s.Links())
	}
}

func TestEvery(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	var seqs []uint64
	every(ctx, time.Millisecond, func(seq uint64) {
		seqs = append(seqs, seq)
		if seq == 3 {
			cancel()
		}
	})
	if len(seqs) != 3 || seqs[0] != 1 || seqs[2] != 3 {
		t.Errorf("seqs = %v", seqs)
	}
}
