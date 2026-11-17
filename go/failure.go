package otelhelper

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"runtime"
	"runtime/debug"
	"strings"
	"time"

	"go.opentelemetry.io/otel/codes"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"
	"go.opentelemetry.io/otel/trace"
	grpccodes "google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Application span failure status (R11, ADR-016, design D15). A failed span
// gets status ERROR with an empty description and error.type; the message and
// the stack go to one ERROR log record in that span's context (ADR-014); a
// successful span stays Unset; the helper never sets Ok.

// Attribute keys of the failure log record (semconv exceptions-logs).
const (
	attrExceptionType       = "exception.type"
	attrExceptionMessage    = "exception.message"
	attrExceptionStacktrace = "exception.stacktrace"
)

// helperScopeName is the instrumentation scope of the helper's tracer and of
// its OTel log bridge, and the logger field of a stdout failure record.
const helperScopeName = "otel-helper"

// maxFailureChain bounds every walk of an error chain or tree (the A29
// budget), so a cyclic or very deep chain cannot hang the caller.
const maxFailureChain = 256

// WithSpan runs fn in a new span named name and applies the failure rule
// (R11, design D15) when fn returns:
//
//   - a nil error leaves the span status Unset (never Ok);
//   - a non-nil error marks the span — status ERROR without description and
//     error.type (see RecordFailure) — emits one ERROR log record in the
//     span's context, and is returned unchanged;
//   - an error whose every leaf is context.Canceled or a gRPC status with
//     codes.Canceled is a cancellation: the span stays Unset and nothing is
//     recorded. context.DeadlineExceeded is a failure;
//   - a panic is recovered, applied the same way (an error value uses the
//     error rules; any other value gets error.type %T and the message
//     fmt.Sprint(value)), and re-panicked with the same value. A panic with
//     http.ErrAbortHandler, panicked or returned, is a cancellation;
//   - runtime.Goexit ends the span Unset.
//
// Every WithSpan a failure crosses marks its own span and emits its own
// record (one record per helper span that failed, design D15 "Nesting"): use
// WithSpan only at the layers that should report.
//
// The span is started with tracer.Start(ctx, name, opts...); a nil tracer is
// GetTracer() (scope "otel-helper", global provider). Pass
// trace.WithNewRoot() for a root span (StartRootSpan stays a plain starter).
// fn receives the span's context.
func WithSpan(ctx context.Context, tracer trace.Tracer, name string, fn func(context.Context) error, opts ...trace.SpanStartOption) (err error) {
	if tracer == nil {
		tracer = GetTracer()
	}
	ctx, span := tracer.Start(ctx, name, opts...)
	returned, ended := false, false
	// The span is ended from this closure, never by a deferred span.End: the
	// SDK's End, run directly by defer, recovers a panic in flight and adds an
	// exception event with the message and the stack, which ADR-014 rules out.
	defer func() {
		// Ends the span on every path: a panic, Goexit, or failSpan itself
		// panicking. span.End is called by this closure, not deferred directly,
		// so its recover() sees nothing.
		defer func() {
			if !ended {
				ended = true
				span.End()
			}
		}()
		if returned {
			return
		}
		// recover() is nil on runtime.Goexit (a nil panic value is a
		// *runtime.PanicNilError since Go 1.21): the span ends Unset.
		if r := recover(); r != nil {
			failSpan(ctx, span, r)
			panic(r)
		}
	}()
	err = fn(ctx)
	returned = true
	if err != nil {
		failSpan(ctx, span, err)
	}
	ended = true
	span.End()
	return err
}

// RecordFailure marks the span of ctx (trace.SpanFromContext) as failed with
// err — status ERROR without description and error.type — and always emits
// one ERROR log record in ctx: the span's trace_id and span_id, whether or
// not the span is recording. The record goes through the helper's slog
// handler (the OTel bridge while a logs exporter is installed, the stdout
// JSON fallback otherwise) with err's text as body and exception.type,
// exception.message and exception.stacktrace (debug.Stack() at this call,
// without the helper's leading frames: the first frame is the caller; cut
// in the middle above 12 KiB).
//
// Use it for failures the application handles instead of returning them
// (for example a rejected order answered normally). It marks whatever it is
// given: the cancellation rule of WithSpan does not apply. A nil err does
// nothing. Returning the same err from an enclosing WithSpan records it again
// for that span.
//
// error.type is the first non-empty ErrorType() string method along err's
// Unwrap chain (the upstream convention), else semconv.ErrorType of
// go.opentelemetry.io/otel/semconv/v1.43.0, else %T when that panics (a
// typed-nil receiver).
func RecordFailure(ctx context.Context, err error) {
	if err == nil {
		return
	}
	applyFailure(ctx, trace.SpanFromContext(ctx), describeError(err))
}

// failure is what marking needs to know about a failure value.
type failure struct {
	typ     string
	message string
}

// failSpan applies the rule to the span of a WithSpan call for a returned
// error or a recovered panic value; a cancellation leaves the span alone.
func failSpan(ctx context.Context, span trace.Span, value any) {
	var f failure
	if err, ok := value.(error); ok {
		if guardedEqual(err, http.ErrAbortHandler) || isCancellation(err) {
			return
		}
		f = describeError(err)
	} else {
		f = failure{typ: fmt.Sprintf("%T", value), message: fmt.Sprint(value)}
	}
	applyFailure(ctx, span, f)
}

// applyFailure marks span and emits the record.
func applyFailure(ctx context.Context, span trace.Span, f failure) {
	span.SetStatus(codes.Error, "")
	span.SetAttributes(semconv.ErrorTypeKey.String(f.typ))
	emitFailureRecord(ctx, f)
}

// emitFailureRecord hands the record to the helper's per-record slog handler
// (design D15 "Channel"), so it follows the level and exporter rules of every
// helper log record. On stdout the line carries logger "otel-helper" (the
// bridge's scope). The stack is taken only when the record is enabled; for a
// recovered panic this still runs on the panicking stack, so it includes the
// panic site.
func emitFailureRecord(ctx context.Context, f failure) {
	h := newSwitchHandler(&installedLogLevel, newStdoutJSONHandler(&installedLogLevel))
	// The held-back scope logger is written on stdout only, never exported.
	h.scopeLogger, h.hasScopeLogger = slog.StringValue(helperScopeName), true
	if !h.Enabled(ctx, slog.LevelError) {
		return
	}
	r := slog.NewRecord(time.Now(), slog.LevelError, f.message, 0)
	r.AddAttrs(
		slog.String(attrExceptionType, f.typ),
		slog.String(attrExceptionMessage, f.message),
		slog.String(attrExceptionStacktrace, failureStack()),
	)
	if err := h.Handle(ctx, r); err != nil {
		warnOncePerProcess("failure-record-write", "otelhelper: a failure log record could not be written", "error", errorText(err))
	}
}

// helperFramePrefix is how debug.Stack names a function of this package
// ("github.com/staffops/otel-libs/go."), taken from the running code so that
// a module rename, a major-version path or a fork keeps matching.
var helperFramePrefix = packagePrefix()

func packagePrefix() string {
	pc := make([]uintptr, 1)
	runtime.Callers(1, pc)
	f, _ := runtime.CallersFrames(pc).Next()
	return strings.TrimSuffix(f.Function, "packagePrefix")
}

// helperFrames are the functions between the marking point and the
// application: debug.Stack itself and the helper's marking path.
var helperFrames = map[string]bool{
	"runtime/debug.Stack":                   true,
	helperFramePrefix + "failureStack":      true,
	helperFramePrefix + "emitFailureRecord": true,
	helperFramePrefix + "applyFailure":      true,
	helperFramePrefix + "failSpan":          true,
	helperFramePrefix + "RecordFailure":     true,
	helperFramePrefix + "WithSpan":          true,
}

// maxFailureStackBytes caps exception.stacktrace. debug.Stack already keeps
// at most 100 frames (the runtime prints the innermost 50, a
// "...N frames elided..." line, then the outermost 50), but long file paths
// and argument lists can still make it tens of KiB. A backend can reject the
// whole record above its limit (Loki's structured metadata is 64 KiB per
// line), and on the stdout fallback a line over containerd's 16 KiB default
// is split, so the JSON escaping (\n, \t) must also fit: 12 KiB of text.
const maxFailureStackBytes = 12 << 10

// failureStack is the record's exception.stacktrace: debug.Stack() trimmed
// by trimHelperFrames.
func failureStack() string {
	return trimHelperFrames(string(debug.Stack()))
}

// trimHelperFrames removes the helper's frames from the top of a debug.Stack
// text, so the first frame is the application's:
//   - the leading run of marking frames (debug.Stack, failureStack, ...,
//     RecordFailure, WithSpan and its closures): for a returned error the
//     first frame is the caller of WithSpan or RecordFailure;
//   - then, for a panic re-raised by inner WithSpan calls, each leading
//     "panic(...)" + WithSpan-closure pair (the inner re-panic), so every
//     layer's record starts at the original "panic(...)" frame followed by
//     the panic site.
//
// The "goroutine N [running]:" header and every later line stay as
// debug.Stack writes them (two lines per frame: the function, then a tab and
// file:line; one line for the runtime's "...N frames elided..." and the
// "created by" pair); a later helper frame (an enclosing WithSpan below the
// application's frames) is kept. A text made only of helper frames is
// returned unchanged. A text longer than maxFailureStackBytes is cut in the
// middle, as the runtime does: the innermost and the outermost frames stay,
// with one "...N frames elided by otel-helper..." line between them.
func trimHelperFrames(raw string) string {
	lines := strings.Split(raw, "\n")
	if len(lines) < 3 {
		return raw
	}
	isFrame := func(i int) bool { return i+1 < len(lines) && strings.HasPrefix(lines[i+1], "\t") }
	i := 1
	for isFrame(i) && isHelperFrame(lines[i]) {
		i += 2
	}
	for isFrame(i) && isFrame(i+2) && strings.HasPrefix(lines[i], "panic(") && isHelperClosure(lines[i+2]) {
		i += 4
	}
	if i >= len(lines) || (i == len(lines)-1 && lines[i] == "") {
		return raw
	}
	out := lines[0] + "\n" + strings.Join(lines[i:], "\n")
	if len(out) <= maxFailureStackBytes {
		return out
	}
	// Blocks: a frame (two lines) or any other single line.
	var blocks []string
	var frames []bool
	for j := i; j < len(lines); {
		if isFrame(j) {
			blocks, frames = append(blocks, lines[j]+"\n"+lines[j+1]), append(frames, true)
			j += 2
			continue
		}
		blocks, frames = append(blocks, lines[j]), append(frames, false)
		j++
	}
	budget := maxFailureStackBytes - len(lines[0]) - 64 // the header and the marker line
	tail, used := len(blocks), 0
	for tail > 0 && used+len(blocks[tail-1])+1 <= budget/2 {
		tail--
		used += len(blocks[tail]) + 1
	}
	head := 0
	for head < tail && used+len(blocks[head])+1 <= budget {
		used += len(blocks[head]) + 1
		head++
	}
	// The count includes the frames of a runtime "...N frames elided..." line
	// inside the cut.
	elided := 0
	for k, f := range frames[head:tail] {
		var n int
		switch {
		case f:
			elided++
		case strings.HasPrefix(blocks[head+k], "..."):
			if _, err := fmt.Sscanf(blocks[head+k], "...%d frames elided...", &n); err == nil {
				elided += n
			}
		}
	}
	noun := "frames"
	if elided == 1 {
		noun = "frame"
	}
	parts := append([]string{lines[0]}, blocks[:head]...)
	parts = append(parts, fmt.Sprintf("...%d %s elided by otel-helper...", elided, noun))
	parts = append(parts, blocks[tail:]...)
	return strings.Join(parts, "\n")
}

// frameName is the function name of a debug.Stack function line
// ("pkg.Func(args)" → "pkg.Func").
func frameName(line string) string {
	if j := strings.LastIndexByte(line, '('); j > 0 {
		return line[:j]
	}
	return line
}

// isHelperFrame reports whether a debug.Stack function line names a helper
// marking frame or a WithSpan closure.
func isHelperFrame(line string) bool {
	return helperFrames[frameName(line)] || isHelperClosure(line)
}

// isHelperClosure reports whether a function line names a closure of
// WithSpan, in any naming the compiler uses ("WithSpan.func1",
// "WithSpan.func1.1", "WithSpan.1").
func isHelperClosure(line string) bool {
	return strings.HasPrefix(frameName(line), helperFramePrefix+"WithSpan.")
}

// describeError returns the type and message of err.
func describeError(err error) failure {
	return failure{typ: errorTypeOf(err), message: errorText(err)}
}

// errorTypeOf is the error.type value of err (design D15 table): the first
// non-empty ErrorType() along the Unwrap chain, else semconv.ErrorType
// (strips single-%w wrappers), called under recover (fallback %T). semconv
// walks the tree with errors.As, which never ends on a cyclic chain, so a
// tree the bounded walk cannot finish gets %T.
func errorTypeOf(err error) (s string) {
	for _, l := range chainLinks(err) {
		if t := overrideType(l); t != "" {
			return t
		}
	}
	if _, complete := walkTree(err); !complete {
		return fmt.Sprintf("%T", err)
	}
	defer func() {
		if recover() != nil {
			s = fmt.Sprintf("%T", err)
		}
	}()
	return semconv.ErrorType(err).Value.AsString()
}

// overrideType is err's own ErrorType() value, "" when it has none or panics.
func overrideType(err error) (s string) {
	defer func() {
		if recover() != nil {
			s = ""
		}
	}()
	if et, ok := err.(interface{ ErrorType() string }); ok {
		return et.ErrorType()
	}
	return ""
}

// guardedEqual is a == b, false when the comparison panics (an uncomparable
// dynamic value, such as a struct whose interface field holds a slice).
func guardedEqual(a, b any) (eq bool) {
	defer func() {
		if recover() != nil {
			eq = false
		}
	}()
	return a == b
}

// unwrapOne calls err's Unwrap() error; ok is false when it panics.
func unwrapOne(err error) (next error, ok bool) {
	defer func() {
		if recover() != nil {
			next, ok = nil, false
		}
	}()
	if u, has := err.(interface{ Unwrap() error }); has {
		return u.Unwrap(), true
	}
	return nil, true
}

// unwrapMany calls err's Unwrap() []error; ok is false when it panics.
func unwrapMany(err error) (next []error, has, ok bool) {
	defer func() {
		if recover() != nil {
			next, has, ok = nil, false, false
		}
	}()
	if u, is := err.(interface{ Unwrap() []error }); is {
		return u.Unwrap(), true, true
	}
	return nil, false, true
}

// containsError reports whether seen holds e by identity (guarded ==).
func containsError(seen []error, e error) bool {
	for _, s := range seen {
		if guardedEqual(s, e) {
			return true
		}
	}
	return false
}

// chainLinks is err and the errors its Unwrap() error chain reaches, tracked
// by identity and bounded at maxFailureChain; a panicking Unwrap ends it.
func chainLinks(err error) []error {
	links := make([]error, 0, 4)
	for cur := err; cur != nil && len(links) < maxFailureChain; {
		if containsError(links, cur) {
			break
		}
		links = append(links, cur)
		next, ok := unwrapOne(cur)
		if !ok {
			break
		}
		cur = next
	}
	return links
}

// walkTree returns the leaves of err's tree (Unwrap() error and
// Unwrap() []error links). complete is false when the walk met a cycle, a
// panicking Unwrap or the maxFailureChain node bound.
func walkTree(err error) (leaves []error, complete bool) {
	var seen []error
	complete = true
	var visit func(e error)
	visit = func(e error) {
		if !complete || e == nil {
			return
		}
		if containsError(seen, e) || len(seen) >= maxFailureChain {
			complete = false
			return
		}
		seen = append(seen, e)
		many, has, ok := unwrapMany(e)
		if !ok {
			complete = false
			return
		}
		if has {
			n := 0
			for _, c := range many {
				if c != nil {
					n++
					visit(c)
				}
			}
			if n == 0 {
				leaves = append(leaves, e)
			}
			return
		}
		next, ok := unwrapOne(e)
		if !ok {
			complete = false
			return
		}
		if next == nil {
			leaves = append(leaves, e)
			return
		}
		visit(next)
	}
	visit(err)
	return leaves, complete
}

// isCancellation reports whether every leaf of err's tree is
// context.Canceled or a gRPC status with codes.Canceled (design D15). An
// unfinished walk is a failure.
func isCancellation(err error) bool {
	leaves, complete := walkTree(err)
	if !complete || len(leaves) == 0 {
		return false
	}
	for _, l := range leaves {
		if !guardedEqual(l, context.Canceled) && !grpcCanceled(l) {
			return false
		}
	}
	return true
}

// grpcCanceled reports whether leaf carries the gRPC CANCELLED status; a
// panic (a typed-nil status error) means no.
func grpcCanceled(leaf error) (yes bool) {
	defer func() {
		if recover() != nil {
			yes = false
		}
	}()
	return status.Code(leaf) == grpccodes.Canceled
}
