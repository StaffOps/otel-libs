// Package otelaws provides OpenTelemetry instrumentation for AWS SDK v2.
//
// Usage:
//
//	cfg, err := config.LoadDefaultConfig(ctx)
//	if err != nil { ... }
//	otelaws.Instrument(&cfg)
//	// All subsequent AWS SDK calls will produce trace spans.
//
// A failed call keeps status Error with an empty description (contract D1
// "AWS error status", task A27): contrib otelaws writes err.Error() there,
// and an AWS error message can quote request values. error.type is the AWS
// error code when it matches ^[A-Z][A-Za-z0-9]{1,63}$ (NoSuchKey,
// AccessDenied, ValidationException); a code that does not (a dotted query
// code such as AWS.SimpleQueueService.NonExistentQueue) gives the modeled
// error's type name (QueueDoesNotExist); else _OTHER — a transport error
// without an AWS code is _OTHER too. The same values in Python ext.aws and
// Node @staffops/otel-helper-aws. Not recorded (D1 "AWS payload
// attributes"): the S3 object key, copy source and delete request
// (aws.s3.key, aws.s3.copy_source, aws.s3.delete); otelaws records no request
// or response body, no DynamoDB item collection metrics and no SNS phone
// number. otelaws records no exception event.
//
// Attribute values (contract D1 "AWS service attribute values", task A30):
// server.address/server.port of the request URL on every call (otelaws
// writes the SQS queue URL as server.address; dropped), messaging.system
// "aws.sns" for SNS (otelaws writes "aws_sns"; SQS is "aws_sqs" already) and,
// on DynamoDB spans, the legacy db.system=dynamodb next to
// db.system.name=aws.dynamodb unless OTEL_SEMCONV_STABILITY_OPT_IN holds
// database without database/dup; messaging.operation.name on SNS and SQS spans
// is the AWS operation (Publish, SendMessage; otelaws writes publish_input on
// SNS and nothing on SQS).
//
// RPC keys (contract D1 "AWS RPC keys", ruled 2026-11-06): the legacy
// rpc.system=aws-api, rpc.service=<Service>, rpc.method=<Operation> by
// default, as Python and Node.js; OTEL_SEMCONV_STABILITY_OPT_IN=rpc gives
// otelaws's current rpc.system.name=aws-api, rpc.method=<Service>/<Operation>;
// rpc/dup writes both, rpc.method holding the current value. Before this rule
// Go wrote only the current keys. Span names are otelaws's
// <Service>.<Operation>, the rule every language follows; kinds are CLIENT
// except SQS/SNS send (PRODUCER) and receive (CONSUMER), 2026-11-08.
package otelaws

import (
	"context"
	"errors"
	"os"
	"reflect"
	"regexp"
	"strconv"
	"strings"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/smithy-go"
	"github.com/aws/smithy-go/middleware"
	smithyhttp "github.com/aws/smithy-go/transport/http"
	awsotel "go.opentelemetry.io/contrib/instrumentation/github.com/aws/aws-sdk-go-v2/otelaws"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// errorTypeKey is semconv error.type.
const errorTypeKey attribute.Key = "error.type"

// Keys and values the helper maps (contract D1 "AWS service attribute
// values", ruled 2026-11-05, task A30), semconv v1.43.0.
const (
	serverAddressKey   attribute.Key = "server.address"
	serverPortKey      attribute.Key = "server.port"
	messagingSystemKey attribute.Key = "messaging.system"
	dbSystemNameKey    attribute.Key = "db.system.name"
	legacyDBSystemKey  attribute.Key = "db.system"
	messagingOpKey     attribute.Key = "messaging.operation.name"
	rpcSystemNameKey   attribute.Key = "rpc.system.name"
	rpcSystemKey       attribute.Key = "rpc.system"
	rpcServiceKey      attribute.Key = "rpc.service"
	rpcMethodKey       attribute.Key = "rpc.method"
)

// messagingOperations maps otelaws's SNS messaging.operation.name values
// (snsattributes.go:26,34) to the system-specific operation name, the AWS
// operation (semconv messaging/sns.md: messaging.operation.name is "the
// system-specific name of the messaging operation"; contract D1 "AWS service
// attribute values", ruled 2026-11-06, A30). Python and Node write the same.
var messagingOperations = map[string]string{"publish_input": "Publish", "publish_batch_input": "PublishBatch"}

// messagingSpanKinds is the span kind of the SQS and SNS send and receive
// calls, by otelaws's span name (contract D1 "AWS service attribute values",
// owner amendment 2026-11-08, A30): PRODUCER on send/publish, CONSUMER on
// receive, as Python and Node.js; every other AWS call stays CLIENT (otelaws
// aws.go:69). The wrapper's Start sets it: otelaws has no option for the kind.
var messagingSpanKinds = map[string]trace.SpanKind{
	"SQS.SendMessage":      trace.SpanKindProducer,
	"SQS.SendMessageBatch": trace.SpanKindProducer,
	"SQS.ReceiveMessage":   trace.SpanKindConsumer,
	"SNS.Publish":          trace.SpanKindProducer,
	"SNS.PublishBatch":     trace.SpanKindProducer,
}

// messagingSystems maps otelaws's messaging.system values to the semconv
// registry values (registry/attributes/messaging.md: aws.sns, aws_sqs):
// otelaws v0.72 writes "aws_sns" (snsattributes.go:18).
var messagingSystems = map[string]string{"aws_sns": "aws.sns"}

// legacyDBSystems is the legacy db.system value of a db.system.name, written
// with it unless OTEL_SEMCONV_STABILITY_OPT_IN holds database without
// database/dup (contract D1 "Database semconv opt-in", as ext/otelsql and
// ext/otelredis): semconv v1.43 db.system.name aws.dynamodb, db.system dynamodb.
var legacyDBSystems = map[string]string{"aws.dynamodb": "dynamodb"}

// envSemconvOptIn and legacyKeysEnabled are ext/otelsql's (a separate module,
// so copied): legacy keys unless the value holds database without
// database/dup, tokens trimmed with the contract set and ASCII-folded (A25).
const envSemconvOptIn = "OTEL_SEMCONV_STABILITY_OPT_IN"

const trimChars = " \t\n\r\v\f"

func asciiLower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if 'A' <= c && c <= 'Z' {
			b[i] = c + ('a' - 'A')
		}
	}
	return string(b)
}

// rpcMode is the RPC convention generation of AWS spans (contract D1 "AWS RPC
// keys", ruled 2026-11-06, task A30): the legacy keys by default, the
// current ones with OTEL_SEMCONV_STABILITY_OPT_IN=rpc, both with rpc/dup
// (rpc/dup wins over rpc, as semconv rpc-spans.md says).
type rpcMode int

const (
	rpcLegacy rpcMode = iota // default: rpc.system, rpc.service, rpc.method=<Operation>
	rpcStable                // "rpc": rpc.system.name, rpc.method=<Service>/<Operation>
	rpcDup                   // "rpc/dup": both; rpc.method holds the current value
)

func rpcModeFromEnv() rpcMode {
	mode := rpcLegacy
	for _, v := range strings.Split(os.Getenv(envSemconvOptIn), ",") {
		switch asciiLower(strings.Trim(v, trimChars)) {
		case "rpc/dup":
			return rpcDup
		case "rpc":
			mode = rpcStable
		}
	}
	return mode
}

func legacyKeysEnabled() bool {
	var stable, dup bool
	for _, v := range strings.Split(os.Getenv(envSemconvOptIn), ",") {
		switch asciiLower(strings.Trim(v, trimChars)) {
		case "database":
			stable = true
		case "database/dup":
			dup = true
		}
	}
	return dup || !stable
}

// droppedKeys are never recorded: an S3 object key, a copy source
// (bucket/key) and a DeleteObjects request (every key) are data.
var droppedKeys = map[attribute.Key]bool{"aws.s3.key": true, "aws.s3.copy_source": true, "aws.s3.delete": true}

// awsErrorCode is the shape of an AWS error code (a bounded set of PascalCase
// identifiers); the same pattern in Python and Node.js.
var awsErrorCode = regexp.MustCompile(`^[A-Z][A-Za-z0-9]{1,63}$`)

// codeHolderKey is the private context key of the *string the span's error
// code is written to (by errorCodeMiddleware, read by SetStatus).
type codeHolderKey struct{}

// Instrument adds OpenTelemetry tracing middleware to an AWS SDK v2 config.
// Call this after loading the AWS config and before creating service clients,
// once per config: a second call adds a second set of middleware (two spans
// per call).
func Instrument(cfg *aws.Config) {
	awsotel.AppendMiddlewares(&cfg.APIOptions, awsotel.WithTracerProvider(errorSanitizingProvider{TracerProvider: otel.GetTracerProvider(), legacyKeys: legacyKeysEnabled(), rpc: rpcModeFromEnv()}))
	cfg.APIOptions = append(cfg.APIOptions, errorCodeMiddleware, serverAddressMiddleware)
}

// serverAddressMiddleware writes server.address and server.port of the
// request the SDK sends (Finalize, after endpoint resolution) on the call's
// span, as Python botocore does (contract D1 "AWS service attribute values",
// task A30): the host, and the URL's port or the scheme's default (443 for
// https, 80 for http). otelaws v0.72 writes server.address only on SQS spans,
// set to the queue URL (sqsattributes.go); that value is dropped by
// errorSanitizingSpan.
func serverAddressMiddleware(stack *middleware.Stack) error {
	return stack.Finalize.Add(middleware.FinalizeMiddlewareFunc("OtelHelperAWSServerAddress", func(
		ctx context.Context, in middleware.FinalizeInput, next middleware.FinalizeHandler) (
		middleware.FinalizeOutput, middleware.Metadata, error,
	) {
		if req, ok := in.Request.(*smithyhttp.Request); ok && req.URL != nil {
			if span := trace.SpanFromContext(ctx); span.IsRecording() {
				span.SetAttributes(serverAttributes(req.URL.Scheme, req.URL.Hostname(), req.URL.Port())...)
			}
		}
		return next.HandleFinalize(ctx, in)
	}), middleware.After)
}

// serverAttributes are server.address and server.port for a request URL; none without a host.
func serverAttributes(scheme, host, port string) []attribute.KeyValue {
	if host == "" {
		return nil
	}
	attrs := []attribute.KeyValue{serverAddressKey.String(host)}
	if p, err := strconv.Atoi(port); err == nil && p > 0 && p <= 65535 {
		attrs = append(attrs, serverPortKey.Int(p))
	} else if port == "" {
		switch asciiLower(scheme) {
		case "https":
			attrs = append(attrs, serverPortKey.Int(443))
		case "http":
			attrs = append(attrs, serverPortKey.Int(80))
		}
	}
	return attrs
}

// errorCodeMiddleware runs inside otelaws's span (Initialize, after its
// middleware) and writes the AWS error code of a failed call to the span's
// holder.
func errorCodeMiddleware(stack *middleware.Stack) error {
	return stack.Initialize.Add(middleware.InitializeMiddlewareFunc("OtelHelperAWSErrorCode", func(
		ctx context.Context, in middleware.InitializeInput, next middleware.InitializeHandler) (
		middleware.InitializeOutput, middleware.Metadata, error,
	) {
		out, metadata, err := next.HandleInitialize(ctx, in)
		if holder, ok := ctx.Value(codeHolderKey{}).(*string); ok && err != nil {
			*holder = errorCode(err)
		}
		return out, metadata, err
	}), middleware.After)
}

// errorCode is the error.type of err: smithy.APIError.ErrorCode() when it has
// the code shape, else the modeled error's type name (*types.QueueDoesNotExist
// → QueueDoesNotExist) when that has it, else _OTHER.
func errorCode(err error) string {
	var apiErr smithy.APIError
	if !errors.As(err, &apiErr) {
		return "_OTHER"
	}
	if code := apiErr.ErrorCode(); awsErrorCode.MatchString(code) {
		return code
	}
	t := reflect.TypeOf(apiErr)
	for t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	// Generic (unmodeled) errors have no service type name to offer.
	if name := t.Name(); awsErrorCode.MatchString(name) && !strings.HasPrefix(t.PkgPath(), "github.com/aws/smithy-go") {
		return name
	}
	return "_OTHER"
}

// errorSanitizingProvider hands otelaws tracers whose spans drop the
// description of an Error status. Contrib otelaws v0.72 starts the span with
// its tracer in initializeMiddlewareAfter and, on error, calls
// span.SetAttributes(semconv.ErrorType(err)) (the Go error type) and
// span.SetStatus(codes.Error, err.Error()).
type errorSanitizingProvider struct {
	trace.TracerProvider
	legacyKeys bool
	rpc        rpcMode
}

func (p errorSanitizingProvider) Tracer(name string, opts ...trace.TracerOption) trace.Tracer {
	return errorSanitizingTracer{Tracer: p.TracerProvider.Tracer(name, opts...), legacyKeys: p.legacyKeys, rpc: p.rpc}
}

type errorSanitizingTracer struct {
	trace.Tracer
	legacyKeys bool
	rpc        rpcMode
}

// rpcAttributes rewrites otelaws's span-start RPC keys (rpc.system.name,
// rpc.method=<Service>/<Operation>, attributes.go:35,46) for mode: legacy
// gives rpc.system=aws-api, rpc.service=<Service>, rpc.method=<Operation>;
// dup keeps the current keys and adds rpc.system and rpc.service (rpc.method
// keeps the current, fully-qualified value: one key cannot hold both, and
// rpc.service still carries the service, as otelgrpc's dup mode).
func rpcAttributes(attrs []attribute.KeyValue, mode rpcMode) []attribute.KeyValue {
	if mode == rpcStable {
		return attrs
	}
	out := make([]attribute.KeyValue, 0, len(attrs)+2)
	for _, a := range attrs {
		switch a.Key {
		case rpcSystemNameKey:
			if mode == rpcDup {
				out = append(out, a)
			}
			out = append(out, rpcSystemKey.String(a.Value.AsString()))
			continue
		case rpcMethodKey:
			service, method, ok := strings.Cut(a.Value.AsString(), "/")
			if ok {
				out = append(out, rpcServiceKey.String(service))
				if mode == rpcLegacy {
					out = append(out, rpcMethodKey.String(method))
					continue
				}
			}
		}
		out = append(out, a)
	}
	return out
}

func (t errorSanitizingTracer) Start(ctx context.Context, name string, opts ...trace.SpanStartOption) (context.Context, trace.Span) {
	holder := new(string)
	cfg := trace.NewSpanStartConfig(opts...)
	out := []trace.SpanStartOption{
		trace.WithAttributes(rpcAttributes(cfg.Attributes(), t.rpc)...),
		trace.WithSpanKind(cfg.SpanKind()),
		trace.WithLinks(cfg.Links()...),
	}
	if ts := cfg.Timestamp(); !ts.IsZero() {
		out = append(out, trace.WithTimestamp(ts))
	}
	if cfg.NewRoot() {
		out = append(out, trace.WithNewRoot())
	}
	if kind, ok := messagingSpanKinds[name]; ok {
		out = append(out, trace.WithSpanKind(kind))
	}
	ctx, span := t.Tracer.Start(context.WithValue(ctx, codeHolderKey{}, holder), name, out...)
	// otelaws uses the returned span for its attributes and the error
	// (initializeMiddlewareAfter); the context keeps the SDK span, which its
	// deserializeMiddleware reads and children see as their parent.
	return ctx, &errorSanitizingSpan{Span: span, code: holder, legacyKeys: t.legacyKeys, operation: operationOf(name)}
}

// errorSanitizingSpan keeps an Error code without its description, owns
// error.type (otelaws's Go type name is replaced by the AWS error code) and
// drops droppedKeys.
type errorSanitizingSpan struct {
	trace.Span
	code       *string
	legacyKeys bool
	operation  string // <Operation> of the span name <Service>.<Operation>
}

func (s *errorSanitizingSpan) SetAttributes(kv ...attribute.KeyValue) {
	s.Span.SetAttributes(mapAttributes(kv, s.legacyKeys, s.operation)...)
}

// mapAttributes is what the span records of an otelaws attribute batch:
// without error.type (owned by SetStatus), droppedKeys and otelaws's SQS
// server.address (the queue URL; serverAddressMiddleware writes the host),
// with messaging.system mapped to the semconv value and, in legacy mode, the
// legacy db.system next to db.system.name (A30).
func mapAttributes(kv []attribute.KeyValue, legacyKeys bool, operation string) []attribute.KeyValue {
	kept := make([]attribute.KeyValue, 0, len(kv)+2)
	var messaging, operationNamed bool
	for _, a := range kv {
		messaging = messaging || a.Key == messagingSystemKey
		operationNamed = operationNamed || a.Key == messagingOpKey
		switch {
		case a.Key == errorTypeKey || droppedKeys[a.Key] || a.Key == serverAddressKey:
			continue
		case a.Key == messagingSystemKey:
			if v, ok := messagingSystems[a.Value.AsString()]; ok {
				a = messagingSystemKey.String(v)
			}
		case a.Key == messagingOpKey:
			if v, ok := messagingOperations[a.Value.AsString()]; ok {
				a = messagingOpKey.String(v)
			}
		case a.Key == dbSystemNameKey && legacyKeys:
			if v, ok := legacyDBSystems[a.Value.AsString()]; ok {
				kept = append(kept, legacyDBSystemKey.String(v))
			}
		}
		kept = append(kept, a)
	}
	if messaging && !operationNamed && operation != "" {
		kept = append(kept, messagingOpKey.String(operation))
	}
	return kept
}

// operationOf is the <Operation> of an otelaws span name <Service>.<Operation>, or "".
func operationOf(spanName string) string {
	if i := strings.LastIndexByte(spanName, '.'); i >= 0 {
		return spanName[i+1:]
	}
	return ""
}

func (s *errorSanitizingSpan) SetStatus(code codes.Code, description string) {
	if code != codes.Error {
		s.Span.SetStatus(code, description)
		return
	}
	errType := *s.code
	if errType == "" {
		errType = "_OTHER"
	}
	s.Span.SetAttributes(errorTypeKey.String(errType))
	s.Span.SetStatus(codes.Error, "")
}
