package otelhelper

// Design D16 / R12 (tasks A40, A46): error.type on Go HTTP server spans and
// metrics, and on gRPC client and server spans.

import (
	"bufio"
	"context"
	"errors"
	"mime/multipart"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelcodes "go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/types/known/emptypb"
)

// httpErrorTracing installs a TracerProvider with the helper's health
// sampler (parent-based, so an inbound -00 traceparent is not sampled), an
// in-memory metric reader and the W3C propagator.
func httpErrorTracing(t *testing.T) (*tracetest.InMemoryExporter, *sdkmetric.ManualReader) {
	t.Helper()
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithSampler(newHealthSampler(sdktrace.ParentBased(sdktrace.AlwaysSample()), defaultHealthPaths)),
		sdktrace.WithSpanProcessor(sdktrace.NewSimpleSpanProcessor(exp)))
	prevTP, prevProp := otel.GetTracerProvider(), otel.GetTextMapPropagator()
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.TraceContext{})
	t.Cleanup(func() {
		otel.SetTracerProvider(prevTP)
		otel.SetTextMapPropagator(prevProp)
		_ = tp.Shutdown(context.Background())
	})
	return exp, inMemoryMetrics(t)
}

// httpDurationPoints are the http.server.request.duration attribute sets.
func httpDurationPoints(t *testing.T, reader *sdkmetric.ManualReader) []attribute.Set {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	var sets []attribute.Set
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if h, ok := m.Data.(metricdata.Histogram[float64]); ok && m.Name == "http.server.request.duration" {
				for _, dp := range h.DataPoints {
					sets = append(sets, dp.Attributes)
				}
			}
		}
	}
	return sets
}

// onlyPoint returns the single duration point's error.type and status code.
func onlyPoint(t *testing.T, reader *sdkmetric.ManualReader) (errorType string, hasErrorType bool, statusCode int64) {
	t.Helper()
	points := httpDurationPoints(t, reader)
	if len(points) != 1 {
		t.Fatalf("duration points = %d, want 1", len(points))
	}
	v, ok := points[0].Value("error.type")
	sc, _ := points[0].Value("http.response.status_code")
	return v.AsString(), ok, sc.AsInt64()
}

func serverSpans(exp *tracetest.InMemoryExporter) []tracetest.SpanStub {
	var out []tracetest.SpanStub
	for _, s := range exp.GetSpans() {
		if s.SpanKind == trace.SpanKindServer {
			out = append(out, s)
		}
	}
	return out
}

func TestHTTPHandlerErrorTypeByStatus(t *testing.T) {
	tests := []struct {
		name    string
		handler func(w http.ResponseWriter)
		code    int64
		want    string // "" = no error.type
	}{
		{"500", func(w http.ResponseWriter) { w.WriteHeader(500) }, 500, "500"},
		{"503", func(w http.ResponseWriter) { w.WriteHeader(503) }, 503, "503"},
		{"600", func(w http.ResponseWriter) { w.WriteHeader(600) }, 600, "600"},
		{"999", func(w http.ResponseWriter) { w.WriteHeader(999) }, 999, "999"},
		{"200", func(w http.ResponseWriter) { w.WriteHeader(200) }, 200, ""},
		{"404", func(w http.ResponseWriter) { w.WriteHeader(404) }, 404, ""},
		{"499", func(w http.ResponseWriter) { w.WriteHeader(499) }, 499, ""},
		{"nothing written", func(http.ResponseWriter) {}, 200, ""},
		{"implicit 200 on Write", func(w http.ResponseWriter) {
			_, _ = w.Write([]byte("ok"))
			w.WriteHeader(500) // superfluous: the response is already 200
		}, 200, ""},
		{"implicit 200 on Flush", func(w http.ResponseWriter) {
			w.(http.Flusher).Flush()
			w.WriteHeader(500)
		}, 200, ""},
		{"first WriteHeader wins", func(w http.ResponseWriter) {
			w.WriteHeader(502)
			w.WriteHeader(200)
		}, 502, "502"},
		{"1xx ignored", func(w http.ResponseWriter) {
			w.WriteHeader(103)
			w.WriteHeader(500)
		}, 500, "500"},
		{"101 counts", func(w http.ResponseWriter) {
			w.WriteHeader(101)
			w.WriteHeader(500)
		}, 101, ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			exp, reader := httpErrorTracing(t)
			h := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { tt.handler(w) }), "t")
			h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/orders", nil))
			spans := serverSpans(exp)
			if len(spans) != 1 {
				t.Fatalf("server spans = %d, want 1", len(spans))
			}
			got, ok := spanAttr(spans[0], "error.type")
			if tt.want == "" && ok || tt.want != "" && got != tt.want {
				t.Errorf("span error.type = %q (%v), want %q", got, ok, tt.want)
			}
			if sc, _ := spanAttr(spans[0], "http.response.status_code"); sc != itoa64(tt.code) {
				t.Errorf("span http.response.status_code = %s, want %d", sc, tt.code)
			}
			mv, mok, msc := onlyPoint(t, reader)
			if tt.want == "" && mok || tt.want != "" && mv != tt.want {
				t.Errorf("metric error.type = %q (%v), want %q", mv, mok, tt.want)
			}
			if msc != tt.code {
				t.Errorf("metric http.response.status_code = %d, want %d", msc, tt.code)
			}
		})
	}
}

func itoa64(n int64) string { return strings.TrimSpace(attribute.Int64Value(n).Emit()) }

// An inbound unsampled traceparent (-00) and a health path whose span the
// sampler drops still give the metric error.type: metrics never depend on
// sampling.
func TestHTTPHandlerErrorTypeWithoutSampledSpan(t *testing.T) {
	tests := []struct{ name, path, traceparent string }{
		{"traceparent -00", "/orders", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"},
		{"health path", "/health", ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			exp, reader := httpErrorTracing(t)
			h := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusServiceUnavailable)
			}), "t")
			req := httptest.NewRequest(http.MethodGet, tt.path, nil)
			if tt.traceparent != "" {
				req.Header.Set("traceparent", tt.traceparent)
			}
			h.ServeHTTP(httptest.NewRecorder(), req)
			if n := len(exp.GetSpans()); n != 0 {
				t.Errorf("spans = %d, want 0 (not sampled)", n)
			}
			if v, _, sc := onlyPoint(t, reader); v != "503" || sc != 503 {
				t.Errorf("metric error.type = %q status %d, want 503", v, sc)
			}
		})
	}
}

// RecordFailure in the handler + 500: the application's value stays on the
// span and goes to the metric.
func TestHTTPHandlerKeepsRecordFailureValue(t *testing.T) {
	exp, reader := httpErrorTracing(t)
	h := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		RecordFailure(r.Context(), typedError{"order_rejected"})
		w.WriteHeader(http.StatusInternalServerError)
	}), "t")
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/orders", nil))
	spans := serverSpans(exp)
	if len(spans) != 1 {
		t.Fatalf("server spans = %d", len(spans))
	}
	if v, _ := spanAttr(spans[0], "error.type"); v != "order_rejected" {
		t.Errorf("span error.type = %q, want order_rejected", v)
	}
	if v, _, _ := onlyPoint(t, reader); v != "order_rejected" {
		t.Errorf("metric error.type = %q, want order_rejected", v)
	}
}

// RecordFailure on an unsampled server span (inbound -00, or a health path
// whose span the sampler drops) still gives the metric the application's
// value: the metric never depends on sampling (fix round 1).
func TestHTTPHandlerRecordFailureUnsampled(t *testing.T) {
	for _, tt := range []struct{ name, path, traceparent string }{
		{"traceparent -00", "/orders", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"},
		{"health path", "/health", ""},
	} {
		t.Run(tt.name, func(t *testing.T) {
			exp, reader := httpErrorTracing(t)
			h := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				RecordFailure(r.Context(), typedError{"order_rejected"})
				w.WriteHeader(http.StatusInternalServerError)
			}), "t")
			req := httptest.NewRequest(http.MethodPost, tt.path, nil)
			if tt.traceparent != "" {
				req.Header.Set("traceparent", tt.traceparent)
			}
			h.ServeHTTP(httptest.NewRecorder(), req)
			if n := len(exp.GetSpans()); n != 0 {
				t.Fatalf("spans = %d, want 0", n)
			}
			if v, _, _ := onlyPoint(t, reader); v != "order_rejected" {
				t.Errorf("metric error.type = %q, want order_rejected", v)
			}
		})
	}
}

// A failing WithSpan child inside the handler does not change the server's
// error.type: the metric gets "500", sampled or not, as the span does.
func TestHTTPHandlerChildFailureKeepsStatusCode(t *testing.T) {
	for _, tp := range []string{"", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"} {
		t.Run("traceparent="+tp, func(t *testing.T) {
			exp, reader := httpErrorTracing(t)
			h := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_ = WithSpan(r.Context(), otel.Tracer("t"), "child", func(context.Context) error {
					return typedError{"child_failed"}
				})
				w.WriteHeader(http.StatusInternalServerError)
			}), "t")
			req := httptest.NewRequest(http.MethodGet, "/orders", nil)
			if tp != "" {
				req.Header.Set("traceparent", tp)
			}
			h.ServeHTTP(httptest.NewRecorder(), req)
			if v, _, _ := onlyPoint(t, reader); v != "500" {
				t.Errorf("metric error.type = %q, want 500", v)
			}
			if tp == "" {
				if v, _ := spanAttr(serverSpans(exp)[0], "error.type"); v != "500" {
					t.Errorf("server span error.type = %q, want 500", v)
				}
			}
		})
	}
}

// The route a mux inside the handler sets reaches otelhttp's span name and
// metric through the helper's request copy.
func TestHTTPHandlerMuxInsideKeepsRoute(t *testing.T) {
	exp, reader := httpErrorTracing(t)
	mux := http.NewServeMux()
	mux.HandleFunc("GET /orders/{id}", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(500) })
	NewHTTPHandler(mux, "").ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/orders/7", nil))
	if n := serverSpans(exp)[0].Name; n != "GET /orders/{id}" {
		t.Errorf("span name = %q", n)
	}
	p := httpDurationPoints(t, reader)[0]
	if v, _ := p.Value("http.route"); v.AsString() != "/orders/{id}" {
		t.Errorf("metric http.route = %q", v.AsString())
	}
}

// Traces off (no-op TracerProvider) with an inbound sampled traceparent: the
// context's span is the caller's remote context, so neither RecordFailure
// nor a failing child may decide the metric; both give "500" (fix round 2).
func TestHTTPHandlerTracesOffInboundParent(t *testing.T) {
	for _, tt := range []struct {
		name string
		fail func(r *http.Request)
	}{
		{"RecordFailure", func(r *http.Request) { RecordFailure(r.Context(), typedError{"order_rejected"}) }},
		{"WithSpan child", func(r *http.Request) {
			_ = WithSpan(r.Context(), otel.Tracer("t"), "child", func(context.Context) error { return typedError{"child_failed"} })
		}},
	} {
		t.Run(tt.name, func(t *testing.T) {
			prevTP, prevProp := otel.GetTracerProvider(), otel.GetTextMapPropagator()
			otel.SetTracerProvider(noop.NewTracerProvider())
			otel.SetTextMapPropagator(propagation.TraceContext{})
			t.Cleanup(func() {
				otel.SetTracerProvider(prevTP)
				otel.SetTextMapPropagator(prevProp)
			})
			reader := inMemoryMetrics(t)
			h := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				tt.fail(r)
				w.WriteHeader(http.StatusInternalServerError)
			}), "t")
			req := httptest.NewRequest(http.MethodGet, "/orders", nil)
			req.Header.Set("traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
			h.ServeHTTP(httptest.NewRecorder(), req)
			if v, _, _ := onlyPoint(t, reader); v != "500" {
				t.Errorf("metric error.type = %q, want 500", v)
			}
		})
	}
}

// A multipart form parsed by the handler on the helper's request copy is
// handed back, so net/http and otelhttp can remove its temporary files.
func TestHTTPHandlerMultipartFormCopiedBack(t *testing.T) {
	_, _ = httpErrorTracing(t)
	var body strings.Builder
	mw := multipart.NewWriter(&body)
	fw, _ := mw.CreateFormFile("f", "a.txt")
	_, _ = fw.Write([]byte("data"))
	_ = mw.Close()
	req := httptest.NewRequest(http.MethodPost, "/upload", strings.NewReader(body.String()))
	req.Header.Set("Content-Type", mw.FormDataContentType())
	var seen *multipart.Form
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseMultipartForm(1 << 20); err != nil {
			t.Error(err)
		}
		w.WriteHeader(http.StatusInternalServerError)
	})
	// Observe the request otelhttp hands to the helper's wrapper.
	outer := httpErrorTypeHandler{next: inner}
	tracer := otel.Tracer("t")
	ctx, span := tracer.Start(req.Context(), "server")
	defer span.End()
	r := req.WithContext(ctx)
	outer.ServeHTTP(httptest.NewRecorder(), r)
	seen = r.MultipartForm
	if seen == nil || len(seen.File["f"]) != 1 {
		t.Fatalf("MultipartForm not copied back: %+v", seen)
	}
	_ = seen.RemoveAll()
}

// RecordFailure wins on the metric over a Labeler value too.
func TestHTTPHandlerSpanValueWinsOverLabeler(t *testing.T) {
	_, reader := httpErrorTracing(t)
	h := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		l, _ := otelhttp.LabelerFromContext(r.Context())
		l.Add(attribute.String("error.type", "app_value"))
		RecordFailure(r.Context(), typedError{"from_span"})
		w.WriteHeader(http.StatusInternalServerError)
	}), "t")
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/orders", nil))
	if v, _, _ := onlyPoint(t, reader); v != "from_span" {
		t.Errorf("metric error.type = %q, want from_span", v)
	}
}

// A Labeler error.type set by the application wins over the status code: the
// metric gets app_value and exactly one error.type; the span gets the code.
func TestHTTPHandlerKeepsLabelerValue(t *testing.T) {
	exp, reader := httpErrorTracing(t)
	h := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		l, _ := otelhttp.LabelerFromContext(r.Context())
		l.Add(attribute.String("error.type", "app_value"))
		w.WriteHeader(http.StatusInternalServerError)
	}), "t")
	var captured *otelhttp.Labeler
	outer := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctx := otelhttp.ContextWithLabeler(r.Context(), &otelhttp.Labeler{})
		captured, _ = otelhttp.LabelerFromContext(ctx)
		h.ServeHTTP(w, r.WithContext(ctx))
	})
	outer.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/orders", nil))
	if v, _, _ := onlyPoint(t, reader); v != "app_value" {
		t.Errorf("metric error.type = %q, want app_value", v)
	}
	n := 0
	for _, kv := range captured.Get() {
		if kv.Key == "error.type" {
			n++
		}
	}
	if n != 1 {
		t.Errorf("Labeler error.type entries = %d, want 1 (the helper adds none)", n)
	}
	if v, _ := spanAttr(serverSpans(exp)[0], "error.type"); v != "500" {
		t.Errorf("span error.type = %q, want 500", v)
	}
}

type typedError struct{ typ string }

func (e typedError) Error() string     { return "rejected" }
func (e typedError) ErrorType() string { return e.typ }

// hijackRecorder is a ResponseRecorder (Flusher) that also implements
// Hijacker.
type hijackRecorder struct{ *httptest.ResponseRecorder }

func (hijackRecorder) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	return nil, nil, errors.New("hijacked")
}

func TestHTTPHandlerPreservesFlusherAndHijacker(t *testing.T) {
	_, _ = httpErrorTracing(t)
	var flusher, hijacker bool
	var hijackErr error
	h := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, flusher = w.(http.Flusher)
		var hj http.Hijacker
		hj, hijacker = w.(http.Hijacker)
		if hijacker {
			_, _, hijackErr = hj.Hijack()
		}
	}), "t")
	h.ServeHTTP(hijackRecorder{httptest.NewRecorder()}, httptest.NewRequest(http.MethodGet, "/x", nil))
	if !flusher || !hijacker {
		t.Errorf("flusher=%v hijacker=%v, want both", flusher, hijacker)
	}
	if hijackErr == nil || hijackErr.Error() != "hijacked" {
		t.Errorf("Hijack did not reach the server's writer: %v", hijackErr)
	}
	// A writer without Hijacker does not gain one.
	h = NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, hijacker = w.(http.Hijacker)
	}), "t")
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/x", nil))
	if hijacker {
		t.Error("the wrapper added http.Hijacker")
	}
}

// A panicking handler gets nothing (otelhttp re-panics; unchanged).
func TestHTTPHandlerPanicGetsNoErrorType(t *testing.T) {
	exp, _ := httpErrorTracing(t)
	h := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(500)
		panic("boom")
	}), "t")
	func() {
		defer func() { _ = recover() }()
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/x", nil))
	}()
	for _, s := range serverSpans(exp) {
		if _, ok := spanAttr(s, "error.type"); ok {
			t.Error("panicking handler span has error.type")
		}
	}
}

// Outside otelhttp (no Labeler, no span) the wrapper only passes through.
func TestHTTPErrorTypeHandlerOutsideOtelhttp(t *testing.T) {
	rec := httptest.NewRecorder()
	httpErrorTypeHandler{next: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(500)
	})}.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/x", nil))
	if rec.Code != 500 {
		t.Errorf("code = %d", rec.Code)
	}
}

// A span that is not an SDK span is never written to.
func TestErrorTypeNonSDKSpan(t *testing.T) {
	_, span := noop.NewTracerProvider().Tracer("t").Start(context.Background(), "s")
	if _, found, sdk := spanErrorType(span); found || sdk {
		t.Errorf("noop span: found=%v sdk=%v", found, sdk)
	}
	setErrorTypeIfAbsent(span, "x") // must not panic
	rec := recordingForeignSpan{span}
	if _, found, sdk := spanErrorType(rec); found || sdk {
		t.Errorf("foreign recording span: found=%v sdk=%v", found, sdk)
	}
}

type recordingForeignSpan struct{ trace.Span }

func (recordingForeignSpan) IsRecording() bool { return true }

func TestGRPCErrorTypeTable(t *testing.T) {
	tests := []struct {
		code   codes.Code
		server bool
		want   string
	}{
		{codes.OK, false, ""},
		{codes.OK, true, ""},
		{codes.Canceled, false, "CANCELLED"},
		{codes.Canceled, true, ""},
		{codes.Unknown, true, "UNKNOWN"},
		{codes.FailedPrecondition, false, "FAILED_PRECONDITION"},
		{codes.FailedPrecondition, true, ""},
		{codes.AlreadyExists, false, "ALREADY_EXISTS"},
		{codes.NotFound, true, ""},
		{codes.DeadlineExceeded, true, "DEADLINE_EXCEEDED"},
		{codes.Unimplemented, true, "UNIMPLEMENTED"},
		{codes.Internal, true, "INTERNAL"},
		{codes.Unavailable, true, "UNAVAILABLE"},
		{codes.DataLoss, true, "DATA_LOSS"},
		{codes.Unauthenticated, false, "UNAUTHENTICATED"},
		{codes.Code(17), false, "_OTHER"},
		{codes.Code(17), true, ""},
	}
	for _, tt := range tests {
		got, ok := grpcErrorType(tt.code, tt.server)
		if got != tt.want || ok != (tt.want != "") {
			t.Errorf("grpcErrorType(%d, server=%v) = %q %v, want %q", tt.code, tt.server, got, ok, tt.want)
		}
	}
	for c := codes.Code(1); c <= 16; c++ {
		if got, _ := grpcErrorType(c, false); got != strings.ToUpper(got) || got == "" || strings.Contains(got, "(") {
			t.Errorf("code %d name %q", c, got)
		}
	}
}

// errorGRPCServer answers each method with the status its name gives
// ("/t.v1.S/Internal" → INTERNAL, "Ok" → OK, "Code99" → code 99, "Preset"
// → INTERNAL after the handler set error.type=app on its span) and serves
// the health service.
func errorGRPCServer(t *testing.T) *grpc.ClientConn {
	t.Helper()
	var self atomic.Pointer[grpc.ClientConn] // for "HealthInside"
	lis := bufconn.Listen(1 << 20)
	srv := grpc.NewServer(GRPCServerOption(), grpc.UnknownServiceHandler(func(_ any, s grpc.ServerStream) error {
		var m emptypb.Empty
		if err := s.RecvMsg(&m); err != nil {
			return err
		}
		name, _ := grpc.MethodFromServerStream(s)
		switch name[strings.LastIndexByte(name, '/')+1:] {
		case "Ok":
			return s.SendMsg(&emptypb.Empty{})
		case "FailedPrecondition":
			return status.Error(codes.FailedPrecondition, "stock")
		case "AlreadyExists":
			return status.Error(codes.AlreadyExists, "dup")
		case "Internal":
			return status.Error(codes.Internal, "bug")
		case "NotFound":
			return status.Error(codes.NotFound, "missing")
		case "Code99":
			return status.Error(codes.Code(99), "odd")
		case "Plain":
			return errors.New("no code")
		case "Preset":
			trace.SpanFromContext(s.Context()).SetAttributes(attribute.String("error.type", "app"))
			return status.Error(codes.Internal, "bug")
		case "Bidi":
			return status.Error(codes.Unavailable, "down")
		case "AppStatus":
			// The application's own status on the RPC span (A37: kept when the RPC ends OK).
			trace.SpanFromContext(s.Context()).SetStatus(otelcodes.Error, "app text")
			return s.SendMsg(&emptypb.Empty{})
		case "AppStatusThenInternal":
			// A37 review M2: otelgrpc's own SetStatus on the failure replaces the app's description.
			trace.SpanFromContext(s.Context()).SetStatus(otelcodes.Error, "app text")
			return status.Error(codes.Internal, "bug")
		case "HealthInside":
			// A filtered client health call that fails inside the handler,
			// then success: the server span must stay clean.
			_, err := healthpb.NewHealthClient(self.Load()).Check(s.Context(), &healthpb.HealthCheckRequest{Service: "missing"})
			if status.Code(err) != codes.NotFound {
				return status.Error(codes.Internal, "health call: "+err.Error())
			}
			return s.SendMsg(&emptypb.Empty{})
		}
		return status.Error(codes.Unimplemented, name)
	}))
	healthpb.RegisterHealthServer(srv, health.NewServer())
	go func() { _ = srv.Serve(lis) }()
	conn, err := grpc.NewClient("passthrough:///t", GRPCDialOption(),
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return lis.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	self.Store(conn)
	t.Cleanup(func() {
		_ = conn.Close()
		srv.Stop()
	})
	return conn
}

func spanOfKind(t *testing.T, exp *tracetest.InMemoryExporter, kind trace.SpanKind, name string) tracetest.SpanStub {
	t.Helper()
	var found tracetest.SpanStub
	waitFor(t, func() bool {
		for _, s := range exp.GetSpans() {
			if s.SpanKind == kind && s.Name == name {
				found = s
				return true
			}
		}
		return false
	})
	return found
}

func TestGRPCErrorTypeOnSpans(t *testing.T) {
	tests := []struct {
		method, client, server string // "" = no error.type
	}{
		{"FailedPrecondition", "FAILED_PRECONDITION", ""},
		{"AlreadyExists", "ALREADY_EXISTS", ""},
		{"Internal", "INTERNAL", "INTERNAL"},
		{"NotFound", "NOT_FOUND", ""},
		{"Ok", "", ""},
		{"Code99", "_OTHER", ""},
		{"Plain", "UNKNOWN", "UNKNOWN"},
		{"Preset", "INTERNAL", "app"},
		// Fix round 1: the nested filtered call inherited the server's span key.
		{"HealthInside", "", ""},
	}
	for _, tt := range tests {
		t.Run(tt.method, func(t *testing.T) {
			exp := inMemoryTracing(t)
			conn := errorGRPCServer(t)
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			_ = conn.Invoke(ctx, "/t.v1.S/"+tt.method, &emptypb.Empty{}, &emptypb.Empty{})
			for _, c := range []struct {
				kind trace.SpanKind
				want string
			}{{trace.SpanKindClient, tt.client}, {trace.SpanKindServer, tt.server}} {
				s := spanOfKind(t, exp, c.kind, "t.v1.S/"+tt.method)
				got, ok := spanAttr(s, "error.type")
				if c.want == "" && ok || c.want != "" && got != c.want {
					t.Errorf("%v error.type = %q (%v), want %q", c.kind, got, ok, c.want)
				}
			}
		})
	}
}

// One failing bidi call: streams take the same End path.
func TestGRPCErrorTypeBidiStream(t *testing.T) {
	exp := inMemoryTracing(t)
	conn := errorGRPCServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	stream, err := conn.NewStream(ctx, &grpc.StreamDesc{ServerStreams: true, ClientStreams: true}, "/t.v1.S/Bidi")
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.SendMsg(&emptypb.Empty{}); err != nil {
		t.Fatal(err)
	}
	_ = stream.CloseSend()
	if err := stream.RecvMsg(&emptypb.Empty{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("recv err = %v, want UNAVAILABLE", err)
	}
	for _, kind := range []trace.SpanKind{trace.SpanKindClient, trace.SpanKindServer} {
		if v, _ := spanAttr(spanOfKind(t, exp, kind, "t.v1.S/Bidi"), "error.type"); v != "UNAVAILABLE" {
			t.Errorf("%v error.type = %q, want UNAVAILABLE", kind, v)
		}
	}
}

// A filtered client health call starts no span: the caller's span must not
// receive the failed call's error.type.
func TestGRPCErrorTypeFilteredHealthCallLeavesParent(t *testing.T) {
	exp := inMemoryTracing(t)
	conn := errorGRPCServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ctx, parent := otel.Tracer("t").Start(ctx, "parent")
	_, err := healthpb.NewHealthClient(conn).Check(ctx, &healthpb.HealthCheckRequest{Service: "missing"})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("health err = %v, want NOT_FOUND", err)
	}
	parent.End()
	for _, s := range exp.GetSpans() {
		if s.Name == "parent" {
			if v, ok := spanAttr(s, "error.type"); ok {
				t.Errorf("parent span got error.type=%q from a filtered call", v)
			}
			return
		}
	}
	t.Fatal("no parent span")
}
