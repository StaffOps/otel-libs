package otelhelper

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// TestRedactQuery (A28): only the RedactedQueryParams values are replaced,
// matched exactly (case-sensitive); every other byte stays as written.
func TestRedactQuery(t *testing.T) {
	for raw, want := range map[string]string{
		"":                                 "",
		"a=1&b=2":                          "a=1&b=2",
		"X-Amz-Signature=abc&x=1":          "X-Amz-Signature=REDACTED&x=1",
		"x=%20&sig=s&flag":                 "x=%20&sig=REDACTED&flag",
		"X%2DAmz%2DCredential=AKIA%2Fday":  "X%2DAmz%2DCredential=REDACTED",
		"x-amz-signature=abc":              "x-amz-signature=abc",
		"AWSAccessKeyId&Signature=":        "AWSAccessKeyId&Signature=REDACTED",
		"sig=a;Signature=b":                "sig=REDACTED",
		"x=1;sig=a":                        "x=1;sig=a",
		"%zz=1&X-Goog-Signature=g":         "%zz=1&X-Goog-Signature=REDACTED",
		"X-Amz-Security-Token=t%2Bk&a=b=c": "X-Amz-Security-Token=REDACTED&a=b=c",
		// A30: three more names.
		"access_token=tok&a=1":                       "access_token=REDACTED&a=1",
		"X-Goog-Credential=sa%40p.iam%2F20261105&x=": "X-Goog-Credential=REDACTED&x=",
		"GoogleAccessId=sa@p.iam&Signature=s":        "GoogleAccessId=REDACTED&Signature=REDACTED",
		"Access_Token=tok":                           "Access_Token=tok",
	} {
		if got := redactQuery(raw); got != want {
			t.Errorf("redactQuery(%q) = %q, want %q", raw, got, want)
		}
	}
	for u, want := range map[string]string{
		"https://h/p":                     "https://h/p",
		"https://h/p?sig=s#frag":          "https://h/p?sig=REDACTED#frag",
		"https://h/p?a=1#sig=s":           "https://h/p?a=1#sig=s",
		"https://h/p?X-Amz-Signature=abc": "https://h/p?X-Amz-Signature=REDACTED",
	} {
		if got := redactURLQuery(u); got != want {
			t.Errorf("redactURLQuery(%q) = %q, want %q", u, got, want)
		}
	}
	names := RedactedQueryParams()
	names[0] = "changed"
	if RedactedQueryParams()[0] != "sig" || len(names) != 10 {
		t.Error("RedactedQueryParams returns a copy of the ten names")
	}
	plain := []attribute.KeyValue{attribute.String("a", "b"), urlFullKey.String("https://h/?a=1")}
	if got := redactAttrs(plain); &got[0] != &plain[0] {
		t.Error("nothing to redact: the same slice")
	}
}

// TestNewHTTPTransportRedactsPresignedURL (A28): a presigned SigV4 URL the
// app fetches keeps no credential, signature or session token in url.full.
func TestNewHTTPTransportRedactsPresignedURL(t *testing.T) {
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sdktrace.NewSimpleSpanProcessor(exp)))
	defer tp.Shutdown(context.Background())
	prev := otel.GetTracerProvider()
	client := &http.Client{Transport: NewHTTPTransport(nil)} // built before the provider is set
	otel.SetTracerProvider(tp)
	defer otel.SetTracerProvider(prev)
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer backend.Close()
	const secret = "AKIAEXAMPLEFAKEKEY01"
	url := backend.URL + "/bucket/key?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=" + secret + "%2F20261103%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Security-Token=FakeToken&X-Amz-Signature=" + strings.Repeat("a", 64) + "&keep=1"
	resp, err := client.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1", len(spans))
	}
	set := attribute.NewSet(spans[0].Attributes...)
	full, _ := set.Value(urlFullKey)
	got := full.AsString()
	// Every attribute and the status, not only url.full.
	everything := spans[0].Status.Description
	for _, kv := range spans[0].Attributes {
		everything += " " + string(kv.Key) + "=" + kv.Value.Emit()
	}
	for _, leaked := range []string{secret, "FakeToken", strings.Repeat("a", 64)} {
		if strings.Contains(everything, leaked) {
			t.Errorf("span leaks %q: %s", leaked, everything)
		}
	}
	if !strings.Contains(got, "X-Amz-Algorithm=AWS4-HMAC-SHA256") || !strings.HasSuffix(got, "X-Amz-Signature=REDACTED&keep=1") {
		t.Errorf("url.full %q: other parameters must stay as written", got)
	}
}

// TestRedactingTracerUsesTheParentProvider: as otelhttp, the client span is
// started from the request context's span TracerProvider when there is one.
func TestRedactingTracerUsesTheParentProvider(t *testing.T) {
	global := tracetest.NewInMemoryExporter()
	gtp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sdktrace.NewSimpleSpanProcessor(global)))
	defer gtp.Shutdown(context.Background())
	parentExp := tracetest.NewInMemoryExporter()
	ptp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sdktrace.NewSimpleSpanProcessor(parentExp)))
	defer ptp.Shutdown(context.Background())
	prev := otel.GetTracerProvider()
	otel.SetTracerProvider(gtp)
	defer otel.SetTracerProvider(prev)
	tracer := redactingProvider{}.Tracer("t")
	ctx, parent := ptp.Tracer("p").Start(context.Background(), "parent")
	_, child := tracer.Start(ctx, "child")
	child.End()
	parent.End()
	_, root := tracer.Start(context.Background(), "root")
	root.End()
	if n := len(parentExp.GetSpans()); n != 2 {
		t.Errorf("parent provider got %d spans, want 2 (parent + child)", n)
	}
	if g := global.GetSpans(); len(g) != 1 || g[0].Name != "root" {
		t.Errorf("global provider got %v, want the root span only", g)
	}
}
