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
package otelaws

import (
	"context"
	"errors"
	"reflect"
	"regexp"
	"strings"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/smithy-go"
	"github.com/aws/smithy-go/middleware"
	awsotel "go.opentelemetry.io/contrib/instrumentation/github.com/aws/aws-sdk-go-v2/otelaws"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// errorTypeKey is semconv error.type.
const errorTypeKey attribute.Key = "error.type"

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
	awsotel.AppendMiddlewares(&cfg.APIOptions, awsotel.WithTracerProvider(errorSanitizingProvider{otel.GetTracerProvider()}))
	cfg.APIOptions = append(cfg.APIOptions, errorCodeMiddleware)
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
type errorSanitizingProvider struct{ trace.TracerProvider }

func (p errorSanitizingProvider) Tracer(name string, opts ...trace.TracerOption) trace.Tracer {
	return errorSanitizingTracer{p.TracerProvider.Tracer(name, opts...)}
}

type errorSanitizingTracer struct{ trace.Tracer }

func (t errorSanitizingTracer) Start(ctx context.Context, name string, opts ...trace.SpanStartOption) (context.Context, trace.Span) {
	holder := new(string)
	ctx, span := t.Tracer.Start(context.WithValue(ctx, codeHolderKey{}, holder), name, opts...)
	// otelaws uses the returned span for its attributes and the error
	// (initializeMiddlewareAfter); the context keeps the SDK span, which its
	// deserializeMiddleware reads and children see as their parent.
	return ctx, &errorSanitizingSpan{Span: span, code: holder}
}

// errorSanitizingSpan keeps an Error code without its description, owns
// error.type (otelaws's Go type name is replaced by the AWS error code) and
// drops droppedKeys.
type errorSanitizingSpan struct {
	trace.Span
	code *string
}

func (s *errorSanitizingSpan) SetAttributes(kv ...attribute.KeyValue) {
	kept := make([]attribute.KeyValue, 0, len(kv))
	for _, a := range kv {
		if a.Key != errorTypeKey && !droppedKeys[a.Key] {
			kept = append(kept, a)
		}
	}
	s.Span.SetAttributes(kept...)
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
