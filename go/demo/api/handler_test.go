package main

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/staffops/otel-libs/go/demo/internal/demotest"
	"go.opentelemetry.io/otel"
	otelcodes "go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	demov1 "github.com/staffops/otel-libs/go/demo/gen/otellibs/demo/v1"
)

type fakeCache struct {
	mu   sync.Mutex
	data map[string]string
	err  error
	ops  []string
}

func (c *fakeCache) Get(_ context.Context, key string) (string, bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.ops = append(c.ops, "GET "+key)
	if c.err != nil {
		return "", false, c.err
	}
	v, ok := c.data[key]
	return v, ok, nil
}

func (c *fakeCache) Set(_ context.Context, key, value string, _ time.Duration) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.ops = append(c.ops, "SET "+key)
	c.data[key] = value
	return nil
}

type fakeQueue struct {
	values []map[string]any
	err    error
}

func (q *fakeQueue) Add(_ context.Context, _ string, values map[string]any) (string, error) {
	if q.err != nil {
		return "", q.err
	}
	q.values = append(q.values, values)
	return "1700000000000-0", nil
}

type fakeOrders struct {
	err  error
	reqs []*demov1.PlaceOrderRequest
}

func (o *fakeOrders) PlaceOrder(_ context.Context, in *demov1.PlaceOrderRequest, _ ...grpc.CallOption) (*demov1.PlaceOrderResponse, error) {
	o.reqs = append(o.reqs, in)
	if o.err != nil {
		return nil, o.err
	}
	return &demov1.PlaceOrderResponse{OrderId: in.GetOrderId(), Status: "processed"}, nil
}

type apiFixture struct {
	a      *api
	cache  *fakeCache
	queue  *fakeQueue
	orders *fakeOrders
	logs   *demotest.Logs
	spans  interface {
		Ended() []sdktrace.ReadOnlySpan
	}
}

func newAPI(t *testing.T) *apiFixture {
	t.Helper()
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	tracer, rec := demotest.Tracer()
	logs := &demotest.Logs{}
	f := &apiFixture{cache: &fakeCache{data: map[string]string{}}, queue: &fakeQueue{}, orders: &fakeOrders{}, logs: logs, spans: rec}
	f.a = &api{logger: logs.Logger(), tracer: tracer, cache: f.cache, queue: f.queue, orders: f.orders, run: "run5", redisHost: "demo-redis", redisPort: 6379}
	return f
}

func (f *apiFixture) post(body string) *httptest.ResponseRecorder {
	rr := httptest.NewRecorder()
	f.a.routes().ServeHTTP(rr, httptest.NewRequest(http.MethodPost, "/orders", strings.NewReader(body)))
	return rr
}

func (f *apiFixture) span(t *testing.T, name string) sdktrace.ReadOnlySpan {
	t.Helper()
	for _, s := range f.spans.Ended() {
		if s.Name() == name {
			return s
		}
	}
	t.Fatalf("no span %q", name)
	return nil
}

func TestPlaceOrderOKPublishesWithContext(t *testing.T) {
	f := newAPI(t)
	rr := f.post(`{"order_id":"ok-1","customer":"customer-a","amount":11,"scenario":"ok"}`)
	if rr.Code != http.StatusCreated {
		t.Fatalf("code = %d body=%s", rr.Code, rr.Body)
	}
	if strings.Join(f.cache.ops, ",") != "GET demo:go:quote:customer-a,SET demo:go:quote:customer-a" {
		t.Errorf("cache ops = %v", f.cache.ops)
	}
	f.post(`{"order_id":"ok-2","customer":"customer-a","amount":12,"scenario":"ok"}`)
	if len(f.cache.ops) != 3 {
		t.Errorf("second order must hit the cache: %v", f.cache.ops)
	}

	accept := f.span(t, "demo.order.accept")
	producer := f.span(t, "send demo:go:orders")
	if producer.SpanKind() != trace.SpanKindProducer || producer.Parent().SpanID() != accept.SpanContext().SpanID() {
		t.Errorf("producer kind=%v parent=%v", producer.SpanKind(), producer.Parent().SpanID())
	}
	attrs := map[string]string{}
	for _, kv := range producer.Attributes() {
		attrs[string(kv.Key)] = kv.Value.Emit()
	}
	for k, v := range map[string]string{
		"messaging.system": "redis", "messaging.operation.name": "send", "messaging.operation.type": "send",
		"messaging.destination.name": "demo:go:orders", "messaging.message.id": "1700000000000-0",
		"server.address": "demo-redis", "server.port": "6379",
	} {
		if attrs[k] != v {
			t.Errorf("producer %s = %q, want %q", k, attrs[k], v)
		}
	}
	tp, _ := f.queue.values[0]["traceparent"].(string)
	if !strings.Contains(tp, producer.SpanContext().SpanID().String()) || !strings.Contains(tp, producer.SpanContext().TraceID().String()) {
		t.Errorf("traceparent %q must name the producer span", tp)
	}
	if f.queue.values[0]["order_id"] != "ok-1" {
		t.Errorf("message = %v", f.queue.values[0])
	}
}

func TestPlaceOrderErrorAnswers(t *testing.T) {
	cases := []struct {
		name, body string
		err        error
		code       int
	}{
		{"business_error", `{"order_id":"ord-3","amount":5000,"scenario":"business_error"}`, status.Error(codes.FailedPrecondition, "order rejected"), 422},
		{"db_error", `{"order_id":"ord-6","amount":20,"scenario":"db_error"}`, status.Error(codes.AlreadyExists, "order exists"), 409},
		{"backend down", `{"order_id":"ord-1","amount":20,"scenario":"ok"}`, status.Error(codes.Unavailable, "x"), 502},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := newAPI(t)
			f.orders.err = c.err
			if rr := f.post(c.body); rr.Code != c.code {
				t.Fatalf("code = %d, want %d", rr.Code, c.code)
			}
			accept := f.span(t, "demo.order.accept")
			if accept.Status().Code != otelcodes.Error || len(accept.Events()) != 0 {
				t.Errorf("accept status=%v events=%d", accept.Status(), len(accept.Events()))
			}
			if accept.Status().Description != "" {
				t.Errorf("accept description = %q", accept.Status().Description)
			}
			for _, kv := range accept.Attributes() {
				if kv.Key == "error.type" {
					t.Errorf("accept must not carry error.type (DE-D7 r3.4), got %q", kv.Value.AsString())
				}
			}
			if len(f.logs.Records()) != 0 || len(f.queue.values) != 0 {
				t.Errorf("no log and no publish on a propagated failure: %v %v", f.logs.Records(), f.queue.values)
			}
		})
	}
}

func TestPlaceOrderUnhandledRecordsInsideTheOwningSpan(t *testing.T) {
	f := newAPI(t)
	rr := f.post(`{"order_id":"ord-9","amount":20,"scenario":"unhandled"}`)
	if rr.Code != http.StatusInternalServerError {
		t.Fatalf("code = %d", rr.Code)
	}
	if len(f.orders.reqs) != 0 || len(f.cache.ops) != 0 {
		t.Error("unhandled must fail before the cache and the backend call")
	}
	accept := f.span(t, "demo.order.accept")
	if accept.Status().Code != otelcodes.Error || accept.Status().Description != "" || len(accept.Events()) != 1 {
		t.Errorf("accept = %v events=%v", accept.Status(), accept.Events())
	}
	logs := f.logs.Records()
	if len(logs) != 1 {
		t.Fatalf("logs = %+v, want one record (recoverer logs nothing)", logs)
	}
	l := logs[0]
	if l.Message != "boom unh-run5" || l.Attrs["exception.type"] != "DemoUnhandledError" || l.SpanContext.SpanID() != accept.SpanContext().SpanID() {
		t.Errorf("record = %+v", l)
	}
	if !strings.Contains(l.Attrs["exception.stacktrace"], "placeOrder") {
		t.Error("the stack must be the panicking goroutine's (deferred recover)")
	}
}

func TestPlaceOrderRejectsInvalidInput(t *testing.T) {
	f := newAPI(t)
	for _, body := range []string{`{`, `{"scenario":"bogus"}`} {
		if rr := f.post(body); rr.Code != http.StatusBadRequest {
			t.Errorf("%s: code = %d", body, rr.Code)
		}
	}
	rr := httptest.NewRecorder()
	f.a.routes().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/health", nil))
	if rr.Code != http.StatusOK {
		t.Errorf("health = %d", rr.Code)
	}
}

func TestCacheAndQueueFailuresAreNotFatal(t *testing.T) {
	f := newAPI(t)
	f.cache.err = errors.New("down")
	f.queue.err = errors.New("down")
	if rr := f.post(`{"order_id":"ok-1","customer":"c","amount":11,"scenario":"ok"}`); rr.Code != http.StatusCreated {
		t.Fatalf("code = %d", rr.Code)
	}
	producer := f.span(t, "send demo:go:orders")
	if producer.Status().Code != otelcodes.Error {
		t.Errorf("producer status = %v", producer.Status())
	}
	if n := len(f.logs.Records()); n != 2 {
		t.Errorf("warn records = %d, want 2", n)
	}
}

func TestHealthReflectsCachedReadiness(t *testing.T) {
	f := newAPI(t)
	calls, ok := 0, false
	now := time.Date(2026, 11, 16, 12, 0, 0, 0, time.UTC)
	f.a.ready = newReadiness(func(context.Context) bool { calls++; return ok })
	f.a.ready.now = func() time.Time { return now }
	get := func() int {
		rr := httptest.NewRecorder()
		f.a.routes().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/health", nil))
		return rr.Code
	}
	if c := get(); c != http.StatusServiceUnavailable || calls != 1 {
		t.Fatalf("not ready: code=%d calls=%d", c, calls)
	}
	ok = true
	now = now.Add(500 * time.Millisecond)
	if c := get(); c != http.StatusServiceUnavailable || calls != 1 {
		t.Errorf("within the TTL the cached result is used: code=%d calls=%d", c, calls)
	}
	now = now.Add(time.Second)
	if c := get(); c != http.StatusOK || calls != 2 {
		t.Errorf("after the TTL it re-checks: code=%d calls=%d", c, calls)
	}
}

func TestRecovererKeepsErrAbortHandler(t *testing.T) {
	h := recoverer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { panic(http.ErrAbortHandler) }))
	defer func() {
		if v := recover(); v != http.ErrAbortHandler {
			t.Errorf("recovered %v, want http.ErrAbortHandler", v)
		}
	}()
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/", nil))
}

func TestHTTPStatusFor(t *testing.T) {
	cases := map[codes.Code]int{
		codes.OK: 201, codes.FailedPrecondition: 422, codes.AlreadyExists: 409,
		codes.Internal: 502, codes.Unavailable: 502, codes.DeadlineExceeded: 502,
	}
	for c, want := range cases {
		if got, _ := httpStatusFor(c); got != want {
			t.Errorf("httpStatusFor(%v) = %d, want %d", c, got, want)
		}
	}
}
