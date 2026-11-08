package otelhelper

import (
	"context"
	"net/url"
	"strings"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/embedded"
)

// redactedQueryParams are the query parameters whose values the helper
// replaces with "REDACTED" in url.full (contract design D1 "URL query
// redaction", task A28): request signatures and the credentials and session
// tokens that travel with them — AWS SigV4 (X-Amz-Signature, X-Amz-Credential,
// X-Amz-Security-Token) and SigV2 (AWSAccessKeyId, Signature), Google Cloud
// Storage (X-Goog-Signature) and Azure SAS (sig). The same list in Python and
// Node.js (instrumentation-http's default), a superset of the semconv
// default (AWSAccessKeyId, Signature, sig, X-Goog-Signature); names match
// exactly (case-sensitive) after percent-decoding, so a lower-case name is
// kept, and ";" is not a separator. Since A30 (2026-11-05) also the OAuth 2.0
// bearer token in a query (access_token, RFC 6750 section 2.3) and the
// Google Cloud Storage signed-URL credential (X-Goog-Credential, V4;
// GoogleAccessId, V2), in every language.
var redactedQueryParams = []string{
	"sig", "Signature", "AWSAccessKeyId", "X-Goog-Signature",
	"X-Amz-Signature", "X-Amz-Credential", "X-Amz-Security-Token",
	"access_token", "X-Goog-Credential", "GoogleAccessId",
}

const urlFullKey attribute.Key = "url.full"

// RedactedQueryParams returns a copy of the query parameter names whose
// values NewHTTPTransport writes as REDACTED in url.full (A28).
func RedactedQueryParams() []string { return append([]string(nil), redactedQueryParams...) }

func isRedactedParam(name string) bool {
	for _, p := range redactedQueryParams {
		if p == name {
			return true
		}
	}
	return false
}

// redactQuery replaces the value of each redactedQueryParams parameter in a
// raw query, keeping every other byte as written (no re-encoding, no
// reordering); a listed name without "=" has no value and is kept as is.
func redactQuery(raw string) string {
	if raw == "" {
		return raw
	}
	parts := strings.Split(raw, "&")
	changed := false
	for i, part := range parts {
		key, _, hasValue := strings.Cut(part, "=")
		name, err := url.QueryUnescape(key)
		if err != nil {
			name = key
		}
		if hasValue && isRedactedParam(name) {
			parts[i] = key + "=REDACTED"
			changed = true
		}
	}
	if !changed {
		return raw
	}
	return strings.Join(parts, "&")
}

// redactURLQuery is u with the redactedQueryParams values redacted in its query.
func redactURLQuery(u string) string {
	before, query, ok := strings.Cut(u, "?")
	if !ok {
		return u
	}
	query, fragment, hasFragment := strings.Cut(query, "#")
	out := before + "?" + redactQuery(query)
	if hasFragment {
		out += "#" + fragment
	}
	return out
}

// redactingProvider hands otelhttp tracers whose spans redact url.full. It
// picks the provider at each span start as otelhttp does without
// WithTracerProvider (transport.go RoundTrip): the parent span's
// TracerProvider when the request context has a valid span, else the global
// one — so a transport built before Setup reports to the helper's provider.
type redactingProvider struct{ embedded.TracerProvider }

func (redactingProvider) Tracer(name string, opts ...trace.TracerOption) trace.Tracer {
	return redactingTracer{name: name, opts: opts}
}

type redactingTracer struct {
	embedded.Tracer
	name string
	opts []trace.TracerOption
}

func (t redactingTracer) Start(ctx context.Context, name string, opts ...trace.SpanStartOption) (context.Context, trace.Span) {
	provider := otel.GetTracerProvider()
	if parent := trace.SpanFromContext(ctx); parent.SpanContext().IsValid() {
		provider = parent.TracerProvider()
	}
	ctx, span := provider.Tracer(t.name, t.opts...).Start(ctx, name, opts...)
	// otelhttp sets url.full with span.SetAttributes after Start
	// (transport.go RoundTrip: RequestTraceAttrs); the context keeps the SDK span.
	return ctx, redactingSpan{span}
}

// redactAttrs is attrs with url.full redacted (attrs itself when nothing changes).
func redactAttrs(attrs []attribute.KeyValue) []attribute.KeyValue {
	var out []attribute.KeyValue
	for i, kv := range attrs {
		if kv.Key != urlFullKey {
			continue
		}
		if r := redactURLQuery(kv.Value.AsString()); r != kv.Value.AsString() {
			if out == nil {
				out = append([]attribute.KeyValue(nil), attrs...)
			}
			out[i] = urlFullKey.String(r)
		}
	}
	if out == nil {
		return attrs
	}
	return out
}

type redactingSpan struct{ trace.Span }

func (s redactingSpan) SetAttributes(kv ...attribute.KeyValue) {
	s.Span.SetAttributes(redactAttrs(kv)...)
}
