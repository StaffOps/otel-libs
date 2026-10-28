package otelhelper

import (
	"strings"
	"sync/atomic"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"
	"go.opentelemetry.io/otel/trace"
)

var defaultHealthPaths = []string{"/health", "/healthz", "/ready", "/readyz", "/live", "/livez", "/ping", "/metrics"}

// DefaultHealthPaths returns (a copy of) the HTTP paths whose server spans the
// helper drops (R7, design D5) unless WithHealthPaths replaces the list.
func DefaultHealthPaths() []string { return append([]string(nil), defaultHealthPaths...) }

// installedHealthPaths is the normalized list of the last successful Setup,
// read per request by NewHTTPTransport; nil = the defaults.
var (
	installedHealthPaths atomic.Pointer[map[string]bool]
	defaultHealthPathSet = healthPathSet(defaultHealthPaths)
)

func healthPathSet(paths []string) map[string]bool {
	set := make(map[string]bool, len(paths))
	for _, p := range paths {
		set[normalizeHealthPath(p)] = true
	}
	return set
}

// isClientHealthPath reports whether an outgoing request targets a health
// path (same list and normalization as the server sampler — design D6 "A5
// validation rulings").
func isClientHealthPath(path string) bool {
	set := defaultHealthPathSet
	if m := installedHealthPaths.Load(); m != nil {
		set = *m
	}
	return set[normalizeHealthPath(path)]
}

// isGRPCHealthMethod reports whether a full gRPC method name ("/svc/Method"
// or "svc/Method", as otelgrpc names server spans and fills rpc.method) belongs
// to the grpc.health.v1.Health service — the whole service: Check, Watch, List
// (design D6 "A5 validation rulings").
func isGRPCHealthMethod(name string) bool {
	return strings.HasPrefix(strings.TrimPrefix(name, "/"), "grpc.health.v1.Health/")
}

// semconv v1.43 has no http.target key; older HTTP instrumentations set it.
const attrHTTPTarget = "http.target"

// healthSampler drops health-check SERVER spans and defers everything else to
// the helper sampler (design D5). Dropping in the sampler, not with the
// otelhttp/otelgrpc WithFilter options, keeps health requests in the RED
// metrics: the instrumentation still runs, only the span is not recorded.
// Children of a dropped span follow it through ParentBased (the default);
// a sampler that ignores the parent (always_on, traceidratio) samples them
// as it would any child of an unsampled parent.
type healthSampler struct {
	inner sdktrace.Sampler
	paths map[string]bool // normalized: lowercase, no trailing slash
}

func newHealthSampler(inner sdktrace.Sampler, paths []string) sdktrace.Sampler {
	return healthSampler{inner: inner, paths: healthPathSet(paths)}
}

// normalizeHealthPath strips the query, lowercases and drops trailing slashes
// ("/" stays "/"), so "/Health/?x=1" matches "/health".
func normalizeHealthPath(p string) string {
	if i := strings.IndexAny(p, "?#"); i >= 0 {
		p = p[:i]
	}
	p = strings.ToLower(p)
	for len(p) > 1 && strings.HasSuffix(p, "/") {
		p = p[:len(p)-1]
	}
	return p
}

func (s healthSampler) ShouldSample(p sdktrace.SamplingParameters) sdktrace.SamplingResult {
	parent := trace.SpanContextFromContext(p.ParentContext)
	// Only the entry span of a request: no parent, or a remote one (a probe
	// carrying a sampled traceparent is still a probe).
	if p.Kind == trace.SpanKindServer && (!parent.IsValid() || parent.IsRemote()) && s.isHealth(p) {
		return sdktrace.SamplingResult{Decision: sdktrace.Drop, Tracestate: parent.TraceState()}
	}
	return s.inner.ShouldSample(p)
}

func (s healthSampler) isHealth(p sdktrace.SamplingParameters) bool {
	if isGRPCHealthMethod(p.Name) {
		return true
	}
	var target string
	for _, kv := range p.Attributes {
		switch kv.Key {
		case semconv.URLPathKey:
			return s.paths[normalizeHealthPath(kv.Value.AsString())]
		case semconv.RPCMethodKey:
			if isGRPCHealthMethod(kv.Value.AsString()) {
				return true
			}
		case attrHTTPTarget:
			target = kv.Value.AsString()
		}
	}
	return target != "" && s.paths[normalizeHealthPath(target)]
}

func (s healthSampler) Description() string {
	return "HealthCheckExcluding{" + s.inner.Description() + "}"
}
