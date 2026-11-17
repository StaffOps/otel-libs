package main

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"sync"
	"time"

	otelhelper "github.com/staffops/otel-libs/go"
	"github.com/staffops/otel-libs/go/demo/internal/demokit"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	grpccodes "google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	demov1 "github.com/staffops/otel-libs/go/demo/gen/otellibs/demo/v1"
)

// maxBody bounds the request body the api decodes.
const maxBody = 64 << 10

// placeOrderTimeout is the deadline of the backend call.
const placeOrderTimeout = 5 * time.Second

// cache, queue and orderClient are the api's dependencies (interfaces so the
// handler is testable without Redis or a backend).
type cache interface {
	Get(ctx context.Context, key string) (string, bool, error)
	Set(ctx context.Context, key, value string, ttl time.Duration) error
}

type queue interface {
	Add(ctx context.Context, stream string, values map[string]any) (string, error)
}

type orderClient interface {
	PlaceOrder(ctx context.Context, in *demov1.PlaceOrderRequest, opts ...grpc.CallOption) (*demov1.PlaceOrderResponse, error)
}

type api struct {
	logger    *slog.Logger
	tracer    trace.Tracer
	cache     cache
	queue     queue
	orders    orderClient
	run       string
	redisHost string
	redisPort int
	ready     *readiness
}

// readinessTTL is how long a readiness result is reused.
const readinessTTL = time.Second

// readiness caches the result of check for readinessTTL, so probes do not
// hit Redis and the backend on every request.
type readiness struct {
	mu      sync.Mutex
	check   func(ctx context.Context) bool
	now     func() time.Time
	checked time.Time
	ok      bool
}

func newReadiness(check func(ctx context.Context) bool) *readiness {
	return &readiness{check: check, now: time.Now}
}

// Ready reports the cached result, re-checking once it is older than
// readinessTTL.
func (r *readiness) Ready(ctx context.Context) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.checked.IsZero() && r.now().Sub(r.checked) < readinessTTL {
		return r.ok
	}
	r.ok = r.check(ctx)
	r.checked = r.now()
	return r.ok
}

// routes wires the handlers. Each route is wrapped by the helper's HTTP
// handler (server span "POST /orders" from the mux pattern) around the
// recovery middleware, so an escaped panic becomes a 500 the server span
// sees.
func (a *api) routes() http.Handler {
	mux := http.NewServeMux()
	mux.Handle("POST /orders", otelhelper.NewHTTPHandler(recoverer(http.HandlerFunc(a.placeOrder)), "POST /orders"))
	mux.Handle("GET /health", otelhelper.NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if a.ready != nil && !a.ready.Ready(r.Context()) {
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "unavailable"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"status": "healthy"})
	}), "GET /health"))
	return mux
}

// recoverer is the api's top-level handler for the unhandled scenario: it
// maps an escaped panic to 500 and logs nothing (otelhelper.WithSpan already
// marked the owning span demo.order.accept and wrote its record, DE-D8).
// http.ErrAbortHandler keeps its net/http meaning.
func recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if v := recover(); v != nil {
				if v == http.ErrAbortHandler {
					panic(v)
				}
				writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
			}
		}()
		next.ServeHTTP(w, r)
	})
}

// placeOrder runs in the demo span demo.order.accept through
// otelhelper.WithSpan (contract design D15): the unhandled scenario's panic is
// recovered there, marks the span (status ERROR without description,
// error.type DemoUnhandledError, no exception event), writes the one ERROR
// record with the message and the stack (taken at the recover, so it shows
// the panic site) in the span's context, ends the span and is re-panicked for
// recoverer (DE-D8). WithSpan ends the span in a way the SDK's End cannot
// recover the panic, so the span gets no SDK exception event with the message
// (the DE-D7 r3.4 rule).
func (a *api) placeOrder(w http.ResponseWriter, r *http.Request) {
	_ = otelhelper.WithSpan(r.Context(), a.tracer, "demo.order.accept", func(ctx context.Context) error {
		a.accept(ctx, w, r)
		return nil
	})
}

// accept is the work of placeOrder inside demo.order.accept. A backend
// failure only propagates here: it is marked by hand (ERROR without
// description, error.type = the answered HTTP status, no record) and
// answered, so WithSpan sees no error.
func (a *api) accept(ctx context.Context, w http.ResponseWriter, r *http.Request) {
	span := trace.SpanFromContext(ctx)
	var req demokit.OrderRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, maxBody)).Decode(&req); err != nil || !demokit.ValidScenario(req.Scenario) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid order"})
		return
	}
	span.SetAttributes(demokit.Scenario(req.Scenario), attribute.String("order.id", req.OrderID))

	if req.Scenario == demokit.ScenarioUnhandled {
		panic(&demokit.DemoUnhandledError{Run: a.run})
	}
	// Every scenario that reaches the cache reads it (DE-D7 r3.5).
	a.quote(ctx, req.Customer)

	callCtx, cancel := context.WithTimeout(ctx, placeOrderTimeout)
	defer cancel()
	resp, err := a.orders.PlaceOrder(callCtx, &demov1.PlaceOrderRequest{
		OrderId:  req.OrderID,
		Customer: req.Customer,
		Amount:   req.Amount,
		Scenario: req.Scenario,
	})
	if err != nil {
		// A parent span that only propagates the failure: ERROR without
		// description, error.type = the HTTP status it answers (as the worker's
		// demo.drive), no event, no record; only the owning span (the backend's
		// demo.order.process) names the failure (DE-D8 note of 2026-11-17).
		httpStatus, msg := httpStatusFor(status.Code(err))
		span.SetStatus(codes.Error, "")
		span.SetAttributes(attribute.String("error.type", strconv.Itoa(httpStatus)))
		writeJSON(w, httpStatus, map[string]string{"error": msg})
		return
	}
	a.publish(ctx, resp.GetOrderId(), req.Scenario)
	writeJSON(w, http.StatusCreated, map[string]string{"order_id": resp.GetOrderId(), "status": resp.GetStatus()})
}

// quote reads the customer's cached quote and stores one on a miss. The cache
// is optional: an error is logged (WARN) and the order goes on.
func (a *api) quote(ctx context.Context, customer string) {
	key := demokit.QuoteKey(customer)
	if _, found, err := a.cache.Get(ctx, key); err != nil {
		a.logger.WarnContext(ctx, "quote cache unavailable", "error", err.Error())
		return
	} else if found {
		return
	}
	value := strconv.FormatFloat(float64(len(customer))*1.25, 'f', 2, 64)
	if err := a.cache.Set(ctx, key, value, demokit.QuoteTTL); err != nil {
		a.logger.WarnContext(ctx, "quote cache unavailable", "error", err.Error())
	}
}

// publish appends the order event inside a PRODUCER span (DE-D6), with the
// current context injected by the global propagator into the message fields.
// A failed publish marks the producer span and is logged; the order itself is
// stored, so the answer stays 201.
func (a *api) publish(ctx context.Context, orderID, scenario string) {
	ctx, span := a.tracer.Start(ctx, "send "+demokit.StreamKey,
		trace.WithSpanKind(trace.SpanKindProducer),
		trace.WithAttributes(
			attribute.String("messaging.system", "redis"),
			attribute.String("messaging.operation.name", "send"),
			attribute.String("messaging.operation.type", "send"),
			attribute.String("messaging.destination.name", demokit.StreamKey),
			attribute.String("server.address", a.redisHost),
			attribute.Int("server.port", a.redisPort),
		))
	defer span.End()

	values := map[string]any{"order_id": orderID, "scenario": scenario}
	carrier := propagation.MapCarrier{}
	otel.GetTextMapPropagator().Inject(ctx, carrier)
	for k, v := range carrier {
		values[k] = v
	}
	id, err := a.queue.Add(ctx, demokit.StreamKey, values)
	if err != nil {
		span.SetStatus(codes.Error, "")
		span.SetAttributes(attribute.String("error.type", demokit.TypeName(err)))
		a.logger.WarnContext(ctx, "order event not published", "error", err.Error())
		return
	}
	span.SetAttributes(attribute.String("messaging.message.id", id))
}

// httpStatusFor maps the backend's gRPC code to the api's answer (design
// "Scenario table"): FAILED_PRECONDITION 422, ALREADY_EXISTS 409, anything
// else 502.
func httpStatusFor(code grpccodes.Code) (int, string) {
	switch code {
	case grpccodes.OK:
		return http.StatusCreated, "created"
	case grpccodes.FailedPrecondition:
		return http.StatusUnprocessableEntity, "order rejected"
	case grpccodes.AlreadyExists:
		return http.StatusConflict, "order exists"
	}
	return http.StatusBadGateway, "backend error"
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
