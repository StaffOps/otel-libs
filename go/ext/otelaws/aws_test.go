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
	// The legacy RPC keys by default (contract D1 "AWS RPC keys", A30):
	// otelaws's rpc.system.name / rpc.method=<Service>/<Operation> rewritten.
	for k, want := range map[attribute.Key]string{
		"rpc.system":  "aws-api",
		"rpc.service": "SQS",
		"rpc.method":  "ListQueues",
		"aws.region":  "us-east-1",
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
	tracer := errorSanitizingProvider{TracerProvider: otel.GetTracerProvider(), legacyKeys: true, rpc: rpcStable}.Tracer("t")
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

// TestA30AttributeValues (contract D1 "AWS service attribute values", A30):
// server.address/server.port from the request URL on every call (SQS's
// queue URL is not a server address), DynamoDB db.system.name with the
// legacy db.system unless the stable database mode is chosen.
func TestA30AttributeValues(t *testing.T) {
	for _, tt := range []struct {
		optIn  string
		legacy bool
	}{{"", true}, {"database/dup", true}, {"database", false}} {
		t.Run("optin="+tt.optIn, func(t *testing.T) {
			t.Setenv(envSemconvOptIn, tt.optIn)
			exp := installTracer(t)
			srv := awsFake(t)
			cfg := fakeConfig(srv)
			Instrument(&cfg)
			ctx := context.Background()
			item := map[string]ddbtypes.AttributeValue{"pk": &ddbtypes.AttributeValueMemberS{Value: "v"}}
			_, _ = dynamodb.NewFromConfig(cfg).PutItem(ctx, &dynamodb.PutItemInput{TableName: aws.String("T"), Item: item})
			_, _ = sqs.NewFromConfig(cfg).SendMessage(ctx, &sqs.SendMessageInput{QueueUrl: aws.String("https://sqs.us-east-1.amazonaws.com/123456789012/q"), MessageBody: aws.String("m")})
			spans := exp.GetSpans()
			if len(spans) != 2 {
				t.Fatalf("got %d spans, want 2", len(spans))
			}
			host, port := hostPort(t, srv.URL)
			for _, s := range spans {
				set := attribute.NewSet(s.Attributes...)
				if v, _ := set.Value(serverAddressKey); v.AsString() != host {
					t.Errorf("%s: server.address %q, want %q", s.Name, v.AsString(), host)
				}
				if v, _ := set.Value(serverPortKey); v.AsInt64() != int64(port) {
					t.Errorf("%s: server.port %d, want %d", s.Name, v.AsInt64(), port)
				}
			}
			ddb := attribute.NewSet(spans[0].Attributes...)
			if v, _ := ddb.Value(dbSystemNameKey); v.AsString() != "aws.dynamodb" {
				t.Errorf("db.system.name %q, want aws.dynamodb", v.AsString())
			}
			if v, ok := ddb.Value(legacyDBSystemKey); ok != tt.legacy || (ok && v.AsString() != "dynamodb") {
				t.Errorf("db.system = %q (present %v), want present %v with dynamodb", v.AsString(), ok, tt.legacy)
			}
			q := attribute.NewSet(spans[1].Attributes...)
			if v, _ := q.Value(messagingSystemKey); v.AsString() != "aws_sqs" {
				t.Errorf("messaging.system %q, want aws_sqs", v.AsString())
			}
		})
	}
}

func hostPort(t *testing.T, raw string) (string, int) {
	t.Helper()
	rest := strings.TrimPrefix(raw, "http://")
	i := strings.LastIndexByte(rest, ':')
	var p int
	if _, err := fmt.Sscanf(rest[i+1:], "%d", &p); err != nil {
		t.Fatal(err)
	}
	return rest[:i], p
}

// SNS messaging.system is mapped to the semconv value; other values pass.
func TestMapAttributes(t *testing.T) {
	in := []attribute.KeyValue{
		messagingSystemKey.String("aws_sns"),
		serverAddressKey.String("https://sqs.us-east-1.amazonaws.com/1/q"),
		dbSystemNameKey.String("aws.dynamodb"),
		attribute.String("aws.s3.key", "k"),
		attribute.String("keep", "v"),
	}
	got := attribute.NewSet(mapAttributes(in, true, "")...)
	if v, _ := got.Value(messagingSystemKey); v.AsString() != "aws.sns" {
		t.Errorf("messaging.system %q, want aws.sns", v.AsString())
	}
	if got.HasValue(serverAddressKey) || got.HasValue("aws.s3.key") {
		t.Errorf("dropped keys recorded: %v", got.ToSlice())
	}
	if v, _ := got.Value(legacyDBSystemKey); v.AsString() != "dynamodb" {
		t.Errorf("db.system %q, want dynamodb", v.AsString())
	}
	if !got.HasValue("keep") {
		t.Error("other attributes must pass")
	}
	sqsSet := attribute.NewSet(mapAttributes([]attribute.KeyValue{messagingSystemKey.String("aws_sqs")}, false, "")...)
	if v, _ := sqsSet.Value(messagingSystemKey); v.AsString() != "aws_sqs" {
		t.Errorf("aws_sqs changed to %q", v.AsString())
	}
	stable := attribute.NewSet(mapAttributes([]attribute.KeyValue{dbSystemNameKey.String("aws.dynamodb")}, false, "")...)
	if stable.HasValue(legacyDBSystemKey) {
		t.Error("stable mode: no legacy db.system")
	}
}

func TestServerAttributes(t *testing.T) {
	for _, tt := range []struct {
		scheme, host, port string
		want               []attribute.KeyValue
	}{
		{"https", "s3.amazonaws.com", "", []attribute.KeyValue{serverAddressKey.String("s3.amazonaws.com"), serverPortKey.Int(443)}},
		{"HTTP", "localhost", "", []attribute.KeyValue{serverAddressKey.String("localhost"), serverPortKey.Int(80)}},
		{"http", "127.0.0.1", "4566", []attribute.KeyValue{serverAddressKey.String("127.0.0.1"), serverPortKey.Int(4566)}},
		{"ftp", "h", "", []attribute.KeyValue{serverAddressKey.String("h")}},
		{"http", "h", "99999", []attribute.KeyValue{serverAddressKey.String("h")}},
		{"https", "", "443", nil},
	} {
		got := serverAttributes(tt.scheme, tt.host, tt.port)
		if fmt.Sprint(got) != fmt.Sprint(tt.want) {
			t.Errorf("serverAttributes(%q, %q, %q) = %v, want %v", tt.scheme, tt.host, tt.port, got, tt.want)
		}
	}
}

// TestRPCModes (contract D1 "AWS RPC keys", A30): legacy by default, the
// current keys with rpc, both with rpc/dup (rpc.method keeps the current value).
func TestRPCModes(t *testing.T) {
	for _, tt := range []struct {
		optIn  string
		mode   rpcMode
		want   map[attribute.Key]string
		absent []attribute.Key
	}{
		{"", rpcLegacy, map[attribute.Key]string{"rpc.system": "aws-api", "rpc.service": "SQS", "rpc.method": "ListQueues"}, []attribute.Key{"rpc.system.name"}},
		{"http,database", rpcLegacy, map[attribute.Key]string{"rpc.system": "aws-api", "rpc.method": "ListQueues"}, []attribute.Key{"rpc.system.name"}},
		{" RPC ", rpcStable, map[attribute.Key]string{"rpc.system.name": "aws-api", "rpc.method": "SQS/ListQueues"}, []attribute.Key{"rpc.system", "rpc.service"}},
		{"rpc,rpc/dup", rpcDup, map[attribute.Key]string{"rpc.system.name": "aws-api", "rpc.system": "aws-api", "rpc.service": "SQS", "rpc.method": "SQS/ListQueues"}, nil},
		{"rpc/old", rpcLegacy, map[attribute.Key]string{"rpc.method": "ListQueues"}, nil},
	} {
		t.Run("optin="+tt.optIn, func(t *testing.T) {
			t.Setenv(envSemconvOptIn, tt.optIn)
			if got := rpcModeFromEnv(); got != tt.mode {
				t.Fatalf("rpcModeFromEnv() = %v, want %v", got, tt.mode)
			}
			exp := installTracer(t)
			cfg := newConfig(&stubTransport{})
			Instrument(&cfg)
			if _, err := sqs.NewFromConfig(cfg).ListQueues(context.Background(), &sqs.ListQueuesInput{}); err != nil {
				t.Fatal(err)
			}
			s := exp.GetSpans()[0]
			if s.Name != "SQS.ListQueues" || s.SpanKind != trace.SpanKindClient {
				t.Errorf("span %q kind %v, want SQS.ListQueues CLIENT", s.Name, s.SpanKind)
			}
			set := attribute.NewSet(s.Attributes...)
			for k, want := range tt.want {
				if v, _ := set.Value(k); v.AsString() != want {
					t.Errorf("%s = %q, want %q", k, v.AsString(), want)
				}
			}
			for _, k := range tt.absent {
				if set.HasValue(k) {
					t.Errorf("%s present", k)
				}
			}
		})
	}
	// A method value without a service keeps its shape.
	got := attribute.NewSet(rpcAttributes([]attribute.KeyValue{rpcMethodKey.String("Op")}, rpcLegacy)...)
	if v, _ := got.Value(rpcMethodKey); v.AsString() != "Op" || got.HasValue(rpcServiceKey) {
		t.Errorf("rpcAttributes(Op) = %v", got.ToSlice())
	}
}

// SNS messaging.operation.name is the AWS operation (A30, 2026-11-06).
func TestSNSOperationName(t *testing.T) {
	for in, want := range map[string]string{"publish_input": "Publish", "publish_batch_input": "PublishBatch", "other": "other"} {
		got := attribute.NewSet(mapAttributes([]attribute.KeyValue{messagingOpKey.String(in)}, false, "")...)
		if v, _ := got.Value(messagingOpKey); v.AsString() != want {
			t.Errorf("%s -> %q, want %q", in, v.AsString(), want)
		}
	}
}

// Owner amendment 2026-11-08 (contract D1 "AWS service attribute values"):
// SQS/SNS send PRODUCER and receive CONSUMER, keeping <Service>.<Operation>;
// every other call CLIENT. messaging.operation.name is the AWS operation.
func TestMessagingSpanKinds(t *testing.T) {
	for name, want := range map[string]trace.SpanKind{
		"SQS.SendMessage": trace.SpanKindProducer, "SQS.SendMessageBatch": trace.SpanKindProducer, "SQS.ReceiveMessage": trace.SpanKindConsumer,
		"SNS.Publish": trace.SpanKindProducer, "SNS.PublishBatch": trace.SpanKindProducer,
	} {
		if got := messagingSpanKinds[name]; got != want {
			t.Errorf("%s kind %v, want %v", name, got, want)
		}
	}
	for _, name := range []string{"SQS.ListQueues", "SQS.DeleteMessage", "SNS.CreateTopic", "DynamoDB.PutItem"} {
		if _, ok := messagingSpanKinds[name]; ok {
			t.Errorf("%s must stay CLIENT", name)
		}
	}
	exp := installTracer(t)
	cfg := fakeConfig(awsFake(t))
	Instrument(&cfg)
	ctx := context.Background()
	q := sqs.NewFromConfig(cfg)
	url := aws.String("https://sqs.us-east-1.amazonaws.com/123456789012/q")
	_, _ = q.SendMessage(ctx, &sqs.SendMessageInput{QueueUrl: url, MessageBody: aws.String("m")})
	_, _ = q.ReceiveMessage(ctx, &sqs.ReceiveMessageInput{QueueUrl: url})
	_, _ = q.DeleteMessage(ctx, &sqs.DeleteMessageInput{QueueUrl: url, ReceiptHandle: aws.String("r")})
	spans := exp.GetSpans()
	if len(spans) != 3 {
		t.Fatalf("got %d spans, want 3", len(spans))
	}
	for i, want := range []struct {
		name string
		kind trace.SpanKind
		op   string
	}{{"SQS.SendMessage", trace.SpanKindProducer, "SendMessage"}, {"SQS.ReceiveMessage", trace.SpanKindConsumer, "ReceiveMessage"}, {"SQS.DeleteMessage", trace.SpanKindClient, "DeleteMessage"}} {
		s := spans[i]
		if s.Name != want.name || s.SpanKind != want.kind {
			t.Errorf("span %d: %s %v, want %s %v", i, s.Name, s.SpanKind, want.name, want.kind)
		}
		set := attribute.NewSet(s.Attributes...)
		if v, _ := set.Value(messagingOpKey); v.AsString() != want.op {
			t.Errorf("%s: messaging.operation.name %q, want %q", s.Name, v.AsString(), want.op)
		}
	}
	if got := operationOf("noservice"); got != "" {
		t.Errorf("operationOf = %q", got)
	}
	// An otelaws value already mapped is not doubled.
	got := mapAttributes([]attribute.KeyValue{messagingSystemKey.String("aws_sns"), messagingOpKey.String("publish_input")}, false, "Publish")
	if len(got) != 2 {
		t.Errorf("mapAttributes = %v", got)
	}
}
