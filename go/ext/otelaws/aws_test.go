package otelaws

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	ddbtypes "github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
	sqstypes "github.com/aws/aws-sdk-go-v2/service/sqs/types"
	"github.com/aws/smithy-go"
	"github.com/aws/smithy-go/middleware"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
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

// awsFake answers S3 (REST-XML), DynamoDB and SQS (JSON 1.0) calls with the
// errors of the A27 rule: NoSuchKey, AccessDenied, a HeadObject 404 without a
// body, a ValidationException whose message quotes the request item (as
// DynamoDB can), and an SQS QueueDoesNotExist with its query error header.
func awsFake(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Amzn-Requestid", "req-1")
		w.Header().Set("X-Amz-Request-Id", "req-1")
		target := r.Header.Get("X-Amz-Target")
		switch {
		case strings.HasPrefix(target, "DynamoDB_20120810."):
			body, _ := io.ReadAll(r.Body)
			w.Header().Set("Content-Type", "application/x-amz-json-1.0")
			w.WriteHeader(http.StatusBadRequest)
			msg, _ := json.Marshal("One or more parameter values were invalid: " + string(body))
			fmt.Fprintf(w, `{"__type":"com.amazonaws.dynamodb.v20120810#ValidationException","message":%s}`, msg)
			return
		case strings.HasPrefix(target, "AmazonSQS."):
			w.Header().Set("Content-Type", "application/x-amz-json-1.0")
			w.Header().Set("X-Amzn-Query-Error", "AWS.SimpleQueueService.NonExistentQueue;Sender")
			w.WriteHeader(http.StatusBadRequest)
			fmt.Fprint(w, `{"__type":"com.amazonaws.sqs#QueueDoesNotExist","message":"The specified queue does not exist."}`)
			return
		case r.Method == http.MethodHead:
			// Body-less errors: 403 for the denied bucket, 503 for key "unavailable", else 404.
			status := http.StatusNotFound
			if strings.HasPrefix(r.URL.Path, "/denied/") {
				status = http.StatusForbidden
			} else if strings.HasSuffix(r.URL.Path, "/unavailable") {
				status = http.StatusServiceUnavailable
			}
			w.WriteHeader(status)
			return
		}
		w.Header().Set("Content-Type", "application/xml")
		code, status, message := "NoSuchKey", http.StatusNotFound, "The specified key does not exist."
		if strings.HasPrefix(r.URL.Path, "/denied/") {
			code, status, message = "AccessDenied", http.StatusForbidden, "Access Denied"
		}
		w.WriteHeader(status)
		fmt.Fprintf(w, `<?xml version="1.0" encoding="UTF-8"?><Error><Code>%s</Code><Message>%s</Message><RequestId>req-1</RequestId></Error>`, code, message)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func fakeConfig(srv *httptest.Server) aws.Config {
	return aws.Config{
		Region:           "us-east-1",
		Credentials:      credentials.NewStaticCredentialsProvider("AKIAEXAMPLEFAKEKEY01", "fake-secret", ""),
		BaseEndpoint:     aws.String(srv.URL),
		RetryMaxAttempts: 1,
	}
}

// TestErrorStatusDropsDescription (A27): a failed call keeps Error without
// description, error.type is the AWS error code (the modeled name when the
// code is not PascalCase), a quoted request value never reaches the status or
// the attributes, and the S3 key is never recorded.
func TestErrorStatusDropsDescription(t *testing.T) {
	const secret = "secret-quoted-item-value"
	exp := installTracer(t)
	cfg := fakeConfig(awsFake(t))
	Instrument(&cfg)
	ctx := context.Background()
	s3c := s3.NewFromConfig(cfg, func(o *s3.Options) { o.UsePathStyle = true })
	if _, err := s3c.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String("bucket-1"), Key: aws.String("secret-object-key")}); err == nil {
		t.Fatal("NoSuchKey: no error")
	}
	if _, err := s3c.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String("denied"), Key: aws.String("k")}); err == nil {
		t.Fatal("AccessDenied: no error")
	}
	for _, in := range []*s3.HeadObjectInput{
		{Bucket: aws.String("bucket-1"), Key: aws.String("k")},
		{Bucket: aws.String("denied"), Key: aws.String("k")},
		{Bucket: aws.String("bucket-1"), Key: aws.String("unavailable")},
	} {
		if _, err := s3c.HeadObject(ctx, in); err == nil {
			t.Fatalf("HeadObject %s/%s: no error", *in.Bucket, *in.Key)
		}
	}
	ddb := dynamodb.NewFromConfig(cfg)
	item := map[string]ddbtypes.AttributeValue{"pk": &ddbtypes.AttributeValueMemberS{Value: secret}}
	if _, err := ddb.PutItem(ctx, &dynamodb.PutItemInput{TableName: aws.String("Quoting"), Item: item}); err == nil {
		t.Fatal("ValidationException: no error")
	} else if !strings.Contains(err.Error(), secret) {
		t.Fatalf("the fake should quote the value in the error text: %v", err)
	}
	q := sqs.NewFromConfig(cfg)
	if _, err := q.SendMessage(ctx, &sqs.SendMessageInput{QueueUrl: aws.String("https://sqs.us-east-1.amazonaws.com/123456789012/missing"), MessageBody: aws.String("m")}); err == nil {
		t.Fatal("QueueDoesNotExist: no error")
	}
	spans := exp.GetSpans()
	want := []string{"NoSuchKey", "AccessDenied", "NotFound", "Forbidden", "ServiceUnavailable", "ValidationException", "QueueDoesNotExist"}
	if len(spans) != len(want) {
		t.Fatalf("got %d spans, want %d", len(spans), len(want))
	}
	for i, s := range spans {
		if s.Status.Code != codes.Error || s.Status.Description != "" {
			t.Errorf("%s: status %v %q, want Error without description", s.Name, s.Status.Code, s.Status.Description)
		}
		set := attribute.NewSet(s.Attributes...)
		if v, _ := set.Value(errorTypeKey); v.AsString() != want[i] {
			t.Errorf("%s: error.type %q, want %q", s.Name, v.AsString(), want[i])
		}
		for _, kv := range s.Attributes {
			if strings.Contains(kv.Value.Emit(), secret) || strings.Contains(kv.Value.Emit(), "secret-object-key") || kv.Key == "aws.s3.key" {
				t.Errorf("%s: %s=%s records request data", s.Name, kv.Key, kv.Value.Emit())
			}
		}
		// The deserialize middleware still writes to the SDK span in the context.
		if v, _ := set.Value("aws.request_id"); v.AsString() != "req-1" {
			t.Errorf("%s: aws.request_id %q, want req-1", s.Name, v.AsString())
		}
		if !set.HasValue("http.response.status_code") {
			t.Errorf("%s: no http.response.status_code", s.Name)
		}
	}
}

// TestContextKeepsSDKSpan: the span in the call's context is the SDK span
// (children are its children), and another Initialize middleware that starts
// its own span between otelaws and the helper's does not take the error code.
func TestContextKeepsSDKSpan(t *testing.T) {
	exp := installTracer(t)
	cfg := fakeConfig(awsFake(t))
	Instrument(&cfg)
	// A user middleware after the helper's: it starts a child span around the rest of the call.
	cfg.APIOptions = append(cfg.APIOptions, func(stack *middleware.Stack) error {
		return stack.Initialize.Add(middleware.InitializeMiddlewareFunc("UserSpan", func(
			ctx context.Context, in middleware.InitializeInput, next middleware.InitializeHandler) (
			middleware.InitializeOutput, middleware.Metadata, error,
		) {
			if _, ok := trace.SpanFromContext(ctx).(*errorSanitizingSpan); ok {
				t.Error("the context must carry the SDK span, not the wrapper")
			}
			ctx, span := otel.Tracer("user").Start(ctx, "user-child")
			defer span.End()
			return next.HandleInitialize(ctx, in)
		}), middleware.After)
	})
	s3c := s3.NewFromConfig(cfg, func(o *s3.Options) { o.UsePathStyle = true })
	if _, err := s3c.GetObject(context.Background(), &s3.GetObjectInput{Bucket: aws.String("bucket-1"), Key: aws.String("k")}); err == nil {
		t.Fatal("NoSuchKey: no error")
	}
	spans := exp.GetSpans()
	if len(spans) != 2 {
		t.Fatalf("got %d spans, want 2 (user child + SDK span)", len(spans))
	}
	child, sdk := spans[0], spans[1]
	if child.Name != "user-child" || child.Parent.SpanID() != sdk.SpanContext.SpanID() {
		t.Errorf("user-child parent %v, want the SDK span %v", child.Parent.SpanID(), sdk.SpanContext.SpanID())
	}
	if sdk.ChildSpanCount != 1 {
		t.Errorf("SDK span ChildSpanCount %d, want 1", sdk.ChildSpanCount)
	}
	sdkSet := attribute.NewSet(sdk.Attributes...)
	if v, _ := sdkSet.Value(errorTypeKey); v.AsString() != "NoSuchKey" {
		t.Errorf("error.type %q, want NoSuchKey", v.AsString())
	}
}

func TestErrorCode(t *testing.T) {
	queryCode := "AWS.SimpleQueueService.NonExistentQueue"
	for _, tc := range []struct {
		err  error
		want string
	}{
		{&smithy.GenericAPIError{Code: "NoSuchKey"}, "NoSuchKey"},
		{&smithy.GenericAPIError{Code: queryCode}, "_OTHER"},
		{&smithy.GenericAPIError{Code: "lowercase"}, "_OTHER"},
		{&sqstypes.QueueDoesNotExist{ErrorCodeOverride: &queryCode}, "QueueDoesNotExist"},
		{&smithy.OperationError{ServiceID: "SQS", OperationName: "SendMessage", Err: &sqstypes.QueueDoesNotExist{ErrorCodeOverride: &queryCode}}, "QueueDoesNotExist"},
		{errors.New("dial tcp 127.0.0.1:1: connect: connection refused"), "_OTHER"},
	} {
		if got := errorCode(tc.err); got != tc.want {
			t.Errorf("errorCode(%v) = %q, want %q", tc.err, got, tc.want)
		}
	}
}

func TestSanitizingSpanPassesOtherStatusesAndAttributes(t *testing.T) {
	exp := installTracer(t)
	tracer := errorSanitizingProvider{otel.GetTracerProvider()}.Tracer("t")
	_, span := tracer.Start(context.Background(), "ok")
	span.SetAttributes(attribute.String("kept", "v"), errorTypeKey.String("*smithy.OperationError"), attribute.String("aws.s3.copy_source", "b/k"))
	span.SetStatus(codes.Ok, "")
	span.End()
	_, failed := tracer.Start(context.Background(), "transport")
	failed.SetStatus(codes.Error, "dial tcp 127.0.0.1:1: connect: connection refused")
	failed.End()
	s := exp.GetSpans()[0]
	set := attribute.NewSet(s.Attributes...)
	if v, _ := set.Value("kept"); v.AsString() != "v" {
		t.Errorf("attributes %v: kept missing", s.Attributes)
	}
	if set.HasValue(errorTypeKey) || set.HasValue("aws.s3.copy_source") {
		t.Errorf("otelaws's Go type name and the copy source must not be recorded: %v", s.Attributes)
	}
	if s.Status.Code != codes.Ok {
		t.Errorf("status %v, want Ok", s.Status.Code)
	}
	f := exp.GetSpans()[1]
	fset := attribute.NewSet(f.Attributes...)
	if v, _ := fset.Value(errorTypeKey); v.AsString() != "_OTHER" || f.Status.Description != "" {
		t.Errorf("no AWS error code: error.type %q, description %q; want _OTHER and none", v.AsString(), f.Status.Description)
	}
}
