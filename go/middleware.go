package otelhelper

import (
	"net/http"

	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/stats"
)

// NewHTTPHandler wraps an http.Handler with OTel tracing and HTTP server
// metrics.
//
// Every request is measured, health checks included, so failing probes stay
// visible in http.server.request.duration. Health-check spans are dropped by
// the sampler Setup installs (DefaultHealthPaths, see WithHealthPaths), not
// here: an otelhttp filter would drop the metrics too (design D5).
//
// Server spans are named "<METHOD> <route>" from the http.ServeMux pattern
// (for example "GET /orders/{id}"), or just "<METHOD>" when no pattern
// matched. Since 0.3.0 operation is ignored; it is kept for source
// compatibility.
//
// A response with a 5xx status, or a code 600–999, gets error.type = the
// status code ("500") on the server span, when it is recording and has none,
// and on http.server.request.duration, sampled or not (design D16, A40;
// otelhttp sets error.type only on client spans). On the metric, an
// error.type the application set on the span (RecordFailure) wins, then one
// it added to the otelhttp Labeler, then the status code. The status is read
// as otelhttp reads it: the first WriteHeader (1xx other than 101 ignored),
// an implicit 200 on the first Write or Flush, 200 when the handler writes
// nothing; a ReadFrom (io.Copy) is not seen. A panicking handler gets no
// error.type. The writer the handler receives keeps the optional interfaces
// of the server's (http.Flusher, http.Hijacker, io.ReaderFrom, http.Pusher).
func NewHTTPHandler(handler http.Handler, operation string) http.Handler {
	return otelhttp.NewHandler(httpErrorTypeHandler{next: handler}, operation)
}

// NewHTTPTransport wraps an http.RoundTripper with OTel tracing for outgoing requests.
// Requests to a health path (DefaultHealthPaths or WithHealthPaths, matched
// like the server side: exact, case-insensitive, trailing slash and query
// ignored) get no client span and no client metric (otelhttp filter).
// The values of the RedactedQueryParams() (request signatures, credentials,
// session tokens: a presigned S3 URL) are written as REDACTED in url.full
// (contract D1 "URL query redaction", A28). Server spans carry no url.query
// (otelhttp v0.72 records url.path only). With only a remote parent in the
// request context, otelhttp records no client span: start a local span after
// extracting the incoming context.
func NewHTTPTransport(base http.RoundTripper) http.RoundTripper {
	if base == nil {
		base = http.DefaultTransport
	}
	return otelhttp.NewTransport(base,
		otelhttp.WithFilter(func(r *http.Request) bool {
			return !isClientHealthPath(r.URL.Path)
		}),
		// url.full with the RedactedQueryParams values redacted (A28).
		otelhttp.WithTracerProvider(redactingProvider{}),
	)
}

// GRPCServerHandler returns a gRPC stats.Handler that traces and meters server
// RPCs (spans + rpc.server.call.duration).
//
// No otelgrpc.WithFilter is installed on purpose: that filter drops metrics as
// well as spans, so health RPCs (grpc.health.v1.Health/*) would disappear from
// rpc.server.call.duration and failing probes would go unnoticed. Health RPCs
// are counted like any other RPC; their server spans are dropped by the
// sampler Setup installs (design D5).
//
// A SERVER span the semantic conventions mark ERROR (UNKNOWN,
// DEADLINE_EXCEEDED, UNIMPLEMENTED, INTERNAL, UNAVAILABLE, DATA_LOSS) gets
// error.type = the canonical code name, when it has none (design D16, A46);
// otelgrpc sets it on the metric only.
func GRPCServerHandler() stats.Handler {
	return grpcErrorTypeHandler{Handler: otelgrpc.NewServerHandler(), server: true}
}

// GRPCClientHandler returns a gRPC stats.Handler that traces and meters client
// RPCs (spans + rpc.client.call.duration).
//
// Unlike the server side, health calls are filtered: client calls to grpc.health.v1.Health/* (Check, Watch, List) get no
// span and no metric (design D6 "A5 validation rulings" — the same rule as
// NewHTTPTransport): a client has no sampler-level way to drop only the span.
//
// A CLIENT span that ends with any non-OK status gets error.type = the
// canonical code name (FAILED_PRECONDITION), or _OTHER for a code above 16,
// when it has none (design D16, A46). grpc-go reports an error without a
// code as UNKNOWN.
func GRPCClientHandler() stats.Handler {
	return grpcErrorTypeHandler{Handler: otelgrpc.NewClientHandler(otelgrpc.WithFilter(func(info *stats.RPCTagInfo) bool {
		return !isGRPCHealthMethod(info.FullMethodName)
	}))}
}

// GRPCServerOption returns the grpc.NewServer option that installs
// GRPCServerHandler:
//
//	srv := grpc.NewServer(otelhelper.GRPCServerOption())
func GRPCServerOption() grpc.ServerOption { return grpc.StatsHandler(GRPCServerHandler()) }

// GRPCDialOption returns the grpc.NewClient option that installs
// GRPCClientHandler:
//
//	conn, err := grpc.NewClient(target, otelhelper.GRPCDialOption())
func GRPCDialOption() grpc.DialOption { return grpc.WithStatsHandler(GRPCClientHandler()) }
