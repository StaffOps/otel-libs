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
func NewHTTPHandler(handler http.Handler, operation string) http.Handler {
	return otelhttp.NewHandler(handler, operation)
}

// NewHTTPTransport wraps an http.RoundTripper with OTel tracing for outgoing requests.
// Requests to a health path (DefaultHealthPaths or WithHealthPaths, matched
// like the server side: exact, case-insensitive, trailing slash and query
// ignored) get no client span and no client metric (otelhttp filter).
func NewHTTPTransport(base http.RoundTripper) http.RoundTripper {
	if base == nil {
		base = http.DefaultTransport
	}
	return otelhttp.NewTransport(base,
		otelhttp.WithFilter(func(r *http.Request) bool {
			return !isClientHealthPath(r.URL.Path)
		}),
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
func GRPCServerHandler() stats.Handler {
	return otelgrpc.NewServerHandler()
}

// GRPCClientHandler returns a gRPC stats.Handler that traces and meters client
// RPCs (spans + rpc.client.call.duration).
//
// Unlike the server side, health calls are filtered: client calls to grpc.health.v1.Health/* (Check, Watch, List) get no
// span and no metric (design D6 "A5 validation rulings" — the same rule as
// NewHTTPTransport): a client has no sampler-level way to drop only the span.
func GRPCClientHandler() stats.Handler {
	return otelgrpc.NewClientHandler(otelgrpc.WithFilter(func(info *stats.RPCTagInfo) bool {
		return !isGRPCHealthMethod(info.FullMethodName)
	}))
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
