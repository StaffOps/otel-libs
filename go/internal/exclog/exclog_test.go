package exclog

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

type emitted struct {
	ctx   context.Context
	err   error
	attrs []attribute.KeyValue
}

// stubEmit replaces Emit for one test and records its calls.
func stubEmit(t *testing.T, typ string) *[]emitted {
	t.Helper()
	var calls []emitted
	prev := Emit
	Emit = func(ctx context.Context, err error, attrs []attribute.KeyValue) string {
		calls = append(calls, emitted{ctx, err, attrs})
		return typ
	}
	t.Cleanup(func() { Emit = prev })
	return &calls
}

func provider(t *testing.T, sampler sdktrace.Sampler, opts ...Option) (*tracetest.InMemoryExporter, trace.Tracer) {
	t.Helper()
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp), sdktrace.WithSampler(sampler))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	return exp, TracerProvider(tp, opts...).Tracer("test")
}

func TestRecordErrorTypeOnlyEventAndOneEmit(t *testing.T) {
	calls := stubEmit(t, "my.Type")
	exp, tr := provider(t, sdktrace.AlwaysSample())
	ctx, span := tr.Start(context.Background(), "op")
	at := time.Unix(1700000000, 0)
	err := errors.New("duplicate 'secret'")
	span.RecordError(err, trace.WithTimestamp(at), trace.WithStackTrace(true), trace.WithAttributes())
	span.RecordError(nil) // nothing
	span.End()

	if len(*calls) != 1 || (*calls)[0].err != err {
		t.Fatalf("Emit calls = %v, want one with err", *calls)
	}
	if got := trace.SpanFromContext((*calls)[0].ctx).SpanContext(); !got.Equal(span.SpanContext()) || !got.Equal(trace.SpanFromContext(ctx).SpanContext()) {
		t.Errorf("Emit context span = %v, want %v", got, span.SpanContext())
	}
	s := exp.GetSpans()[0]
	if len(s.Events) != 1 {
		t.Fatalf("events = %v", s.Events)
	}
	e := s.Events[0]
	if e.Name != "exception" || !e.Time.Equal(at) || len(e.Attributes) != 1 || e.Attributes[0].Key != "exception.type" || e.Attributes[0].Value.AsString() != "my.Type" {
		t.Errorf("event = %+v, want exception with only exception.type=my.Type at %v", e, at)
	}
}

func TestRecordErrorWithoutHookUsesTheGoType(t *testing.T) {
	prev := Emit
	Emit = nil
	t.Cleanup(func() { Emit = prev })
	exp, tr := provider(t, sdktrace.AlwaysSample())
	_, span := tr.Start(context.Background(), "op")
	span.RecordError(errors.New("x"))
	span.End()
	if v := exp.GetSpans()[0].Events[0].Attributes[0].Value.AsString(); v != "*errors.errorString" {
		t.Errorf("exception.type = %q", v)
	}
}

func TestRecordErrorEmptyTypeFallsBackAndUnsampledStillEmits(t *testing.T) {
	calls := stubEmit(t, "")
	exp, tr := provider(t, sdktrace.NeverSample())
	_, span := tr.Start(context.Background(), "op")
	if span.IsRecording() {
		t.Fatal("want a non-recording span")
	}
	span.RecordError(errors.New("x"))
	span.End()
	if len(*calls) != 1 {
		t.Errorf("Emit calls = %d, want 1 (unsampled spans keep their ids)", len(*calls))
	}
	if len(exp.GetSpans()) != 0 {
		t.Error("an unsampled span was exported")
	}
	// The empty type path with a recording span.
	exp2, tr2 := provider(t, sdktrace.AlwaysSample())
	_, s2 := tr2.Start(context.Background(), "op2")
	s2.RecordError(errors.New("y"))
	s2.End()
	if v := exp2.GetSpans()[0].Events[0].Attributes[0].Value.AsString(); v != "*errors.errorString" {
		t.Errorf("exception.type = %q, want %%T for an empty hook type", v)
	}
}

type uncomparable struct{ parts []string }

func (u uncomparable) Error() string { return fmt.Sprint(u.parts) }

func TestRecordAttributesAndSameErrorSkipped(t *testing.T) {
	calls := stubEmit(t, "T")
	sys := attribute.String("db.system.name", "postgresql")
	exp, tr := provider(t, sdktrace.AlwaysSample(), nil, WithRecordAttributes(sys),
		WithErrorAttributes(func(err error) []attribute.KeyValue {
			if err.Error() == "panic" {
				panic("getter")
			}
			return []attribute.KeyValue{attribute.String("error.type", "23505")}
		}))
	_, span := tr.Start(context.Background(), "op")
	base := errors.New("dup")
	span.RecordError(base)
	span.RecordError(base)                            // the same value: skipped (pgx Rows.Err then Close)
	span.RecordError(fmt.Errorf("wrapped: %w", base)) // Is(new, base) but not Is(base, new): a new error
	span.RecordError(errors.New("panic"))             // getter panics: static attributes only
	u := uncomparable{[]string{"a"}}
	span.RecordError(u)
	span.RecordError(u) // == panics on an uncomparable value: counted as different
	span.End()
	if len(*calls) != 5 {
		t.Fatalf("Emit calls = %d, want 5", len(*calls))
	}
	if got := (*calls)[0].attrs; len(got) != 2 || got[0] != sys || got[1].Value.AsString() != "23505" {
		t.Errorf("record attributes = %v", got)
	}
	if got := (*calls)[2].attrs; len(got) != 1 || got[0] != sys {
		t.Errorf("a panicking getter: attributes = %v, want the static ones", got)
	}
	if n := len(exp.GetSpans()[0].Events); n != 5 {
		t.Errorf("events = %d, want 5", n)
	}
}
