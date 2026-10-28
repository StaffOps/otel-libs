package otelaws

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// stubTransport answers every AWS call locally with an empty JSON response.
type stubTransport struct{ requests int }

func (s *stubTransport) Do(req *http.Request) (*http.Response, error) {
	s.requests++
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"application/x-amz-json-1.0"}, "X-Amzn-Requestid": []string{"req-1"}},
		Body:       io.NopCloser(bytes.NewBufferString(`{"QueueUrls":[]}`)),
		Request:    req,
	}, nil
}

func newConfig(transport aws.HTTPClient) aws.Config {
	return aws.Config{
		Region:      "us-east-1",
		Credentials: credentials.NewStaticCredentialsProvider("AKIDEXAMPLE", "secret", ""),
		HTTPClient:  transport,
	}
}

func installTracer(t *testing.T) *tracetest.InMemoryExporter {
	t.Helper()
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	prev := otel.GetTracerProvider()
	otel.SetTracerProvider(tp)
	t.Cleanup(func() {
		otel.SetTracerProvider(prev)
		_ = tp.Shutdown(context.Background())
	})
	return exp
}

func TestInstrumentCreatesClientSpan(t *testing.T) {
	exp := installTracer(t)
	stub := &stubTransport{}
	cfg := newConfig(stub)
	Instrument(&cfg)

	if _, err := sqs.NewFromConfig(cfg).ListQueues(context.Background(), &sqs.ListQueuesInput{}); err != nil {
		t.Fatal(err)
	}
	if stub.requests != 1 {
		t.Fatalf("stub saw %d requests, want 1", stub.requests)
	}
	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1", len(spans))
	}
	s := spans[0]
	if s.SpanKind != trace.SpanKindClient {
		t.Errorf("span kind = %v, want client", s.SpanKind)
	}
	set := attribute.NewSet(s.Attributes...)
	// contrib otelaws v0.72 emits the current RPC conventions
	// (rpc.system.name, rpc.method = "<Service>/<Operation>").
	for k, want := range map[attribute.Key]string{
		"rpc.system.name": "aws-api",
		"rpc.method":      "SQS/ListQueues",
		"aws.region":      "us-east-1",
	} {
		if v, _ := set.Value(k); v.AsString() != want {
			t.Errorf("%s = %q, want %q (attrs %v)", k, v.AsString(), want, s.Attributes)
		}
	}
}

func TestUninstrumentedConfigCreatesNoSpan(t *testing.T) {
	exp := installTracer(t)
	cfg := newConfig(&stubTransport{})
	if _, err := sqs.NewFromConfig(cfg).ListQueues(context.Background(), &sqs.ListQueuesInput{}); err != nil {
		t.Fatal(err)
	}
	if n := len(exp.GetSpans()); n != 0 {
		t.Errorf("got %d spans without Instrument, want 0", n)
	}
}
