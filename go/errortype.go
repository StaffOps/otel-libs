package otelhelper

// Protocol error.type on server/client spans the instrumentations leave
// without one (design D16, R12; tasks A40 and A46). Every write yields to a
// value already on the span: the helper reads the span through
// sdktrace.ReadOnlySpan and writes nothing on a span that is not an SDK span.

import (
	"context"
	"net/http"
	"strconv"
	"sync"

	"github.com/felixge/httpsnoop"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"
	"go.opentelemetry.io/otel/trace"
	grpccodes "google.golang.org/grpc/codes"
	"google.golang.org/grpc/stats"
	"google.golang.org/grpc/status"
)

// spanErrorType is the error.type already on span: found only for a
// recording SDK span that carries one. sdk reports whether span is a
// recording SDK span (the only kind the helper writes to).
func spanErrorType(span trace.Span) (value string, found, sdk bool) {
	if !span.IsRecording() {
		return "", false, false
	}
	ro, ok := span.(sdktrace.ReadOnlySpan)
	if !ok {
		return "", false, false
	}
	for _, kv := range ro.Attributes() {
		if kv.Key == semconv.ErrorTypeKey {
			return kv.Value.Emit(), true, true
		}
	}
	return "", false, true
}

// setErrorTypeIfAbsent writes error.type=value on a recording SDK span that
// has none.
func setErrorTypeIfAbsent(span trace.Span, value string) {
	if _, found, sdk := spanErrorType(span); sdk && !found {
		span.SetAttributes(semconv.ErrorTypeKey.String(value))
	}
}

// httpErrorTypeHandler runs inside otelhttp's handler (so the request
// context carries otelhttp's span and Labeler) and, after the application
// handler returns, sets error.type for a 5xx or 600–999 response.
type httpErrorTypeHandler struct{ next http.Handler }

func (h httpErrorTypeHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// The holder needs the server span's own identity. With traces off (a
	// no-op TracerProvider) the context's span is the caller's remote
	// context, shared with every span the handler starts: no holder then,
	// and the metric gets the status code.
	inner, holder := r, (*httpFailureHolder)(nil)
	if sc := trace.SpanFromContext(r.Context()).SpanContext(); sc.IsValid() && !sc.IsRemote() {
		holder = &httpFailureHolder{server: sc}
		inner = r.WithContext(context.WithValue(r.Context(), httpFailureKey{}, holder))
	}
	defer func() {
		// otelhttp reads the route (span name, http.route) and the multipart
		// form (temp-file cleanup) from its own request copy: hand back what
		// the application set on ours, also while a panic unwinds.
		if inner.Pattern != "" {
			r.Pattern = inner.Pattern
		}
		if inner.MultipartForm != nil {
			r.MultipartForm = inner.MultipartForm
		}
	}()
	st := &responseStatus{code: http.StatusOK}
	ww := httpsnoop.Wrap(w, httpsnoop.Hooks{
		WriteHeader: func(next httpsnoop.WriteHeaderFunc) httpsnoop.WriteHeaderFunc {
			return func(code int) {
				st.writeHeader(code)
				next(code)
			}
		},
		Write: func(next httpsnoop.WriteFunc) httpsnoop.WriteFunc {
			return func(b []byte) (int, error) {
				st.implicitOK()
				return next(b)
			}
		},
		Flush: func(next httpsnoop.FlushFunc) httpsnoop.FlushFunc {
			return func() {
				st.implicitOK()
				next()
			}
		},
	})
	h.next.ServeHTTP(ww, inner)
	if code := st.get(); isHTTPServerError(code) {
		noted := ""
		if holder != nil {
			noted = holder.get()
		}
		annotateHTTPServerError(r.Context(), code, noted)
	}
}

// httpFailureHolder carries, per request, the error.type RecordFailure (or a
// failing WithSpan) gave the server span itself, whether or not that span is
// sampled: the metric must not depend on sampling (design D16).
type httpFailureHolder struct {
	server trace.SpanContext
	mu     sync.Mutex
	typ    string
}

type httpFailureKey struct{}

func (h *httpFailureHolder) get() string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.typ
}

// noteServerFailure records typ for the HTTP metric when span is the server
// span of the request in ctx (same SpanContext, which a non-recording span
// still has). A failing child span (a WithSpan inside the handler) is not
// the server span and records nothing: the sampled path reads only the
// server span's attribute, and both paths must agree.
func noteServerFailure(ctx context.Context, span trace.Span, typ string) {
	h, ok := ctx.Value(httpFailureKey{}).(*httpFailureHolder)
	if !ok {
		return
	}
	sc := span.SpanContext()
	if !sc.IsValid() || !sc.Equal(h.server) {
		return
	}
	h.mu.Lock()
	h.typ = typ
	h.mu.Unlock()
}

// responseStatus mirrors otelhttp's RespWriterWrapper status tracking
// (internal/request/resp_writer_wrapper.go, v0.72.0): the first WriteHeader
// wins, 1xx other than 101 is ignored, Write and Flush imply 200, the
// default is 200. Matching it exactly keeps error.type and
// http.response.status_code in agreement.
type responseStatus struct {
	mu          sync.Mutex
	code        int
	wroteHeader bool
}

func (s *responseStatus) writeHeader(code int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.wroteHeader || (code >= 100 && code <= 199 && code != http.StatusSwitchingProtocols) {
		return
	}
	s.wroteHeader = true
	s.code = code
}

func (s *responseStatus) implicitOK() { s.writeHeader(http.StatusOK) }

func (s *responseStatus) get() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.code
}

// isHTTPServerError is a 5xx or a code 600–999 (net/http panics on
// WriteHeader outside 100–999).
func isHTTPServerError(code int) bool { return code >= 500 && code <= 999 }

// annotateHTTPServerError writes error.type=code on the server span when it
// is recording and has none, and gives the duration metric, through the
// otelhttp Labeler, in this order: the value RecordFailure gave the server
// span (noted, sampled or not) or an error.type already on the recording
// span; else nothing when the application already put one in the Labeler
// (Labeler.Add only appends and the last value of a key wins); else the
// status code. The metric never depends on sampling.
func annotateHTTPServerError(ctx context.Context, code int, noted string) {
	value := strconv.Itoa(code)
	span := trace.SpanFromContext(ctx)
	existing, found, sdk := spanErrorType(span)
	if sdk && !found {
		span.SetAttributes(semconv.ErrorTypeKey.String(value))
	}
	if noted != "" {
		existing, found = noted, true
	}
	labeler, ok := otelhttp.LabelerFromContext(ctx)
	if !ok {
		return // not inside otelhttp: no metric to label
	}
	switch {
	case found:
		labeler.Add(semconv.ErrorTypeKey.String(existing))
	case labelerHas(labeler, semconv.ErrorTypeKey):
	default:
		labeler.Add(semconv.ErrorTypeKey.String(value))
	}
}

func labelerHas(l *otelhttp.Labeler, key attribute.Key) bool {
	for _, kv := range l.Get() {
		if kv.Key == key {
			return true
		}
	}
	return false
}

// grpcErrorTypeHandler wraps an otelgrpc stats.Handler and, on *stats.End
// with an error and before delegating (otelgrpc ends the span in its own
// HandleRPC), writes the canonical status code name on the RPC's span.
type grpcErrorTypeHandler struct {
	stats.Handler
	server bool
}

// rpcSpanKey holds the span otelgrpc started for this RPC. A filtered
// client call (health) starts none: without the key the handler would write
// on the caller's span.
type rpcSpanKey struct{}

func (h grpcErrorTypeHandler) TagRPC(ctx context.Context, info *stats.RPCTagInfo) context.Context {
	before := trace.SpanFromContext(ctx).SpanContext()
	ctx = h.Handler.TagRPC(ctx, info)
	var rpcSpan trace.Span
	if span := trace.SpanFromContext(ctx); span.SpanContext().IsValid() && !span.SpanContext().Equal(before) {
		rpcSpan = span
	}
	// Always shadow the key: a call that starts no span (a filtered client
	// health call inside a server handler) must not inherit the enclosing
	// RPC's span from the context.
	return context.WithValue(ctx, rpcSpanKey{}, rpcSpan)
}

func (h grpcErrorTypeHandler) HandleRPC(ctx context.Context, rs stats.RPCStats) {
	if end, ok := rs.(*stats.End); ok && end.Error != nil {
		if span, ok := ctx.Value(rpcSpanKey{}).(trace.Span); ok && span != nil {
			if value, ok := grpcErrorType(status.Code(end.Error), h.server); ok {
				setErrorTypeIfAbsent(span, value)
			}
		}
	}
	h.Handler.HandleRPC(ctx, rs)
}

// grpcErrorType is the error.type of a failed RPC (design D16): a client
// span fails on every non-OK code, a server span only on the six server
// codes otelgrpc marks ERROR (the semconv server rule). A code above 16 is
// _OTHER (client only: otelgrpc leaves such a server span Unset).
func grpcErrorType(code grpccodes.Code, server bool) (string, bool) {
	if code == grpccodes.OK {
		return "", false
	}
	if server {
		switch code {
		case grpccodes.Unknown, grpccodes.DeadlineExceeded, grpccodes.Unimplemented,
			grpccodes.Internal, grpccodes.Unavailable, grpccodes.DataLoss:
		default:
			return "", false
		}
	}
	if int(code) < len(grpcCodeNames) {
		return grpcCodeNames[code], true
	}
	return semconv.ErrorTypeOther.Value.AsString(), true
}

// grpcCodeNames are the canonical status code names (the gRPC spec and
// semconv rpc.response.status_code), indexed by code. codes.Code.String()
// gives Go names (FailedPrecondition), so the helper keeps its own table.
var grpcCodeNames = [...]string{
	"OK", "CANCELLED", "UNKNOWN", "INVALID_ARGUMENT", "DEADLINE_EXCEEDED",
	"NOT_FOUND", "ALREADY_EXISTS", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED",
	"FAILED_PRECONDITION", "ABORTED", "OUT_OF_RANGE", "UNIMPLEMENTED",
	"INTERNAL", "UNAVAILABLE", "DATA_LOSS", "UNAUTHENTICATED",
}
