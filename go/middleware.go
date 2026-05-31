package otelhelper

import (
	"net/http"

	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"google.golang.org/grpc"
)

var healthPaths = map[string]struct{}{
	"/ping": {}, "/health": {}, "/healthz": {}, "/ready": {},
}

// NewHTTPHandler wraps an http.Handler with OTel tracing, filtering health paths.
func NewHTTPHandler(handler http.Handler, operation string) http.Handler {
	return otelhttp.NewHandler(handler, operation,
		otelhttp.WithFilter(func(r *http.Request) bool {
			_, skip := healthPaths[r.URL.Path]
			return !skip
		}),
	)
}

// NewHTTPTransport wraps an http.RoundTripper with OTel tracing for outgoing requests.
// Health paths are filtered from client spans.
func NewHTTPTransport(base http.RoundTripper) http.RoundTripper {
	if base == nil {
		base = http.DefaultTransport
	}
	return otelhttp.NewTransport(base,
		otelhttp.WithFilter(func(r *http.Request) bool {
			_, skip := healthPaths[r.URL.Path]
			return !skip
		}),
	)
}

func grpcHealthFilter(info *otelgrpc.InterceptorInfo) bool {
	return info.Method != "/grpc.health.v1.Health/Check"
}

// UnaryServerInterceptor returns a gRPC unary server interceptor with health filtering.
func UnaryServerInterceptor() grpc.UnaryServerInterceptor {
	return otelgrpc.UnaryServerInterceptor(otelgrpc.WithInterceptorFilter(grpcHealthFilter))
}

// StreamServerInterceptor returns a gRPC stream server interceptor with health filtering.
func StreamServerInterceptor() grpc.StreamServerInterceptor {
	return otelgrpc.StreamServerInterceptor(otelgrpc.WithInterceptorFilter(grpcHealthFilter))
}

// UnaryClientInterceptor returns a gRPC unary client interceptor with health filtering.
func UnaryClientInterceptor() grpc.UnaryClientInterceptor {
	return otelgrpc.UnaryClientInterceptor(otelgrpc.WithInterceptorFilter(grpcHealthFilter))
}

// StreamClientInterceptor returns a gRPC stream client interceptor with health filtering.
func StreamClientInterceptor() grpc.StreamClientInterceptor {
	return otelgrpc.StreamClientInterceptor(otelgrpc.WithInterceptorFilter(grpcHealthFilter))
}
