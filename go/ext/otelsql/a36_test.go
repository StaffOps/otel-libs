package otelsql

// ADR-014, contract task A36 (design D1 row "DB error text in exception
// events", amendment 2026-11-18): a failed operation's exception event keeps
// only exception.type, and one ERROR log record in the span's context carries
// the message and a stack trace (stdout fallback here: no logs exporter).

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// rawKey holds the undecoded line in a captured record (for key order).
const rawKey = "\x00raw"

// captureStdout redirects os.Stdout until the returned function is called; it
// returns the JSON lines written meanwhile, decoded (the raw line under
// rawKey).
func captureStdout(t *testing.T) func() []map[string]any {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	prev := os.Stdout
	os.Stdout = w
	done := make(chan []string)
	go func() {
		var lines []string
		sc := bufio.NewScanner(r)
		sc.Buffer(make([]byte, 0, 64<<10), 1<<20)
		for sc.Scan() {
			lines = append(lines, sc.Text())
		}
		_, _ = io.Copy(io.Discard, r)
		done <- lines
	}()
	restored := false
	restore := func() {
		if !restored {
			restored = true
			os.Stdout = prev
			_ = w.Close()
		}
	}
	t.Cleanup(restore)
	return func() []map[string]any {
		restore()
		var out []map[string]any
		for _, l := range <-done {
			var m map[string]any
			if err := json.Unmarshal([]byte(l), &m); err != nil {
				t.Fatalf("stdout line is not JSON: %q", l)
			}
			m[rawKey] = l
			out = append(out, m)
		}
		return out
	}
}

// spanText is every attribute value, event name, event attribute and the
// status description of s, as text.
func spanText(s tracetest.SpanStub) []string {
	out := []string{s.Name, s.Status.Description}
	for _, a := range s.Attributes {
		out = append(out, a.Value.Emit())
	}
	for _, e := range s.Events {
		out = append(out, e.Name)
		for _, a := range e.Attributes {
			out = append(out, string(a.Key)+"="+a.Value.Emit())
		}
	}
	return out
}

func failedSpans(exp *tracetest.InMemoryExporter) []tracetest.SpanStub {
	var out []tracetest.SpanStub
	for _, s := range exp.GetSpans() {
		if s.Status.Code == codes.Error {
			out = append(out, s)
		}
	}
	return out
}

// assertTypeOnlyEvent checks the span's events: exactly one "exception" with
// only exception.type == typ.
func assertTypeOnlyEvent(t *testing.T, s tracetest.SpanStub, typ string) {
	t.Helper()
	if len(s.Events) != 1 || s.Events[0].Name != "exception" {
		t.Fatalf("%s events = %+v, want one exception event", s.Name, s.Events)
	}
	attrs := s.Events[0].Attributes
	if len(attrs) != 1 || attrs[0].Key != "exception.type" || attrs[0].Value.AsString() != typ {
		t.Errorf("%s exception event attributes = %v, want only exception.type=%q", s.Name, attrs, typ)
	}
}

func TestFailedStatementDetailGoesToACorrelatedRecord(t *testing.T) {
	exp, _ := installProviders(t)
	db := openFake(t)
	read := captureStdout(t)
	// The fake driver answers "mysql" with: Error 1062: Duplicate entry 'secret-value'.
	if _, err := db.ExecContext(context.Background(), "mysql"); err == nil {
		t.Fatal("want an error")
	}
	lines := read()

	s := execSpan(t, exp)
	if s.Status.Code != codes.Error || s.Status.Description != "" {
		t.Errorf("status = %+v, want ERROR without description", s.Status)
	}
	assertTypeOnlyEvent(t, s, "*otelsql.MySQLError")
	for _, span := range exp.GetSpans() {
		for _, v := range spanText(span) {
			if strings.Contains(v, "secret-value") || strings.Contains(v, "Duplicate entry") || strings.Contains(v, "goroutine ") {
				t.Errorf("span %s carries the error detail: %q", span.Name, v)
			}
		}
	}

	if len(lines) != 1 {
		t.Fatalf("want one stdout record, got %d: %v", len(lines), lines)
	}
	r := lines[0]
	if r["level"] != "ERROR" || r["logger"] != "otel-helper" {
		t.Errorf("level/logger = %v/%v, want ERROR/otel-helper", r["level"], r["logger"])
	}
	const msg = "Error 1062: Duplicate entry 'secret-value'"
	if r["msg"] != msg || r["exception.message"] != msg {
		t.Errorf("msg/exception.message = %q/%q, want %q", r["msg"], r["exception.message"], msg)
	}
	if r["exception.type"] != "*otelsql.MySQLError" {
		t.Errorf("exception.type = %v, want the event's type", r["exception.type"])
	}
	// The record names its database and code (A36 review), so DB records can be filtered.
	if r["db.system.name"] != "other_sql" || r["db.response.status_code"] != "1062" || r["error.type"] != "1062" {
		t.Errorf("db attributes = %v/%v/%v", r["db.system.name"], r["db.response.status_code"], r["error.type"])
	}
	if r["trace_id"] != s.SpanContext.TraceID().String() || r["span_id"] != s.SpanContext.SpanID().String() {
		t.Errorf("ids = %v/%v, want the span's %s/%s", r["trace_id"], r["span_id"], s.SpanContext.TraceID(), s.SpanContext.SpanID())
	}
	stack, _ := r["exception.stacktrace"].(string)
	if !strings.HasPrefix(stack, "goroutine ") {
		t.Fatalf("exception.stacktrace = %q, want a goroutine stack", stack)
	}
	// The helper's own frames (debug.Stack, the core hook, the exclog wrapper)
	// are trimmed: the first frame is XSAM's, and the test is in the stack.
	frames := strings.Split(stack, "\n")
	if len(frames) < 2 || !strings.HasPrefix(frames[1], "github.com/XSAM/otelsql.") {
		t.Errorf("first frame = %q, want github.com/XSAM/otelsql", frames[1])
	}
	for _, bad := range []string{"runtime/debug.Stack", "internal/exclog.", "emitInstrumentationException"} {
		if strings.Contains(stack, bad) {
			t.Errorf("stack keeps the helper frame %q", bad)
		}
	}
	if !strings.Contains(stack, "TestFailedStatementDetailGoesToACorrelatedRecord") {
		t.Error("stack misses the caller")
	}
	// Key order on stdout: trace ids right after logger (A23 amendment).
	raw := r[rawKey].(string)
	if !strings.HasPrefix(raw, `{"time":`) || !strings.Contains(raw, `"logger":"otel-helper","trace_id":"`+s.SpanContext.TraceID().String()+`","span_id":"`) {
		t.Errorf("stdout line order: %s", raw)
	}
}

func TestRealSQLiteFailureKeepsOnlyTheType(t *testing.T) {
	exp, _ := installProviders(t)
	db, closeDB, err := Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer closeDB()
	read := captureStdout(t)
	// SQLite: "no such table: dbsecret_a36" quotes the text of the statement.
	_, qerr := db.ExecContext(context.Background(), "DELETE FROM dbsecret_a36 WHERE v = ?", "bound-a36")
	lines := read()
	if qerr == nil || !strings.Contains(qerr.Error(), "dbsecret_a36") {
		t.Fatalf("want a no-such-table error, got %v", qerr)
	}
	failed := failedSpans(exp)
	if len(failed) == 0 {
		t.Fatal("no failed span")
	}
	for _, s := range failed {
		for _, e := range s.Events {
			for _, a := range e.Attributes {
				if a.Key != "exception.type" {
					t.Errorf("%s event %s carries %s", s.Name, e.Name, a.Key)
				}
			}
		}
		for _, v := range spanText(s) {
			if strings.Contains(v, "no such table") || strings.Contains(v, "bound-a36") {
				t.Errorf("span %s carries %q", s.Name, v)
			}
		}
	}
	if len(lines) != len(failed) {
		t.Fatalf("records = %d, failed spans = %d (one per recorded error): %v", len(lines), len(failed), lines)
	}
	ids := map[string]bool{}
	for _, s := range failed {
		ids[s.SpanContext.SpanID().String()] = true
	}
	for _, r := range lines {
		if !strings.Contains(r["msg"].(string), "no such table: dbsecret_a36") || r["exception.stacktrace"] == "" {
			t.Errorf("record = %v", r)
		}
		if !ids[r["span_id"].(string)] {
			t.Errorf("record span_id %v is not a failed span's", r["span_id"])
		}
	}
}

func TestUnsampledFailureStillWritesTheRecord(t *testing.T) {
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp), sdktrace.WithSampler(sdktrace.NeverSample()))
	prev := otel.GetTracerProvider()
	otel.SetTracerProvider(tp)
	t.Cleanup(func() { otel.SetTracerProvider(prev); _ = tp.Shutdown(context.Background()) })
	db := openFake(t)
	read := captureStdout(t)
	if _, err := db.ExecContext(context.Background(), "mysql"); err == nil {
		t.Fatal("want an error")
	}
	lines := read()
	if len(exp.GetSpans()) != 0 {
		t.Fatalf("spans exported with NeverSample: %d", len(exp.GetSpans()))
	}
	if len(lines) != 1 || lines[0]["exception.type"] != "*otelsql.MySQLError" {
		t.Fatalf("records = %v, want one (principle 8: an unsampled span still has its ids)", lines)
	}
	if lines[0]["db.response.status_code"] != "1062" || lines[0]["db.system.name"] != "other_sql" {
		t.Errorf("unsampled record attributes = %v (XSAM sets none on the span; the helper computes them)", lines[0])
	}
	if id, _ := lines[0]["trace_id"].(string); len(id) != 32 {
		t.Errorf("trace_id = %v", lines[0]["trace_id"])
	}
}

func TestCanceledStatementWritesNoRecord(t *testing.T) {
	exp, _ := installProviders(t)
	db := openFake(t)
	read := captureStdout(t)
	// The fake driver answers "canceled" with exec: context canceled (wrapped).
	_, err := db.ExecContext(context.Background(), "canceled")
	lines := read()
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want a cancellation", err)
	}
	if len(failedSpans(exp)) == 0 {
		t.Fatal("no failed span: the cancellation never reached XSAM")
	}
	if len(lines) != 0 {
		t.Errorf("a cancellation wrote %d records: %v", len(lines), lines)
	}
	for _, s := range failedSpans(exp) {
		for _, e := range s.Events {
			if len(e.Attributes) != 1 || e.Attributes[0].Key != "exception.type" {
				t.Errorf("%s event = %v", s.Name, e.Attributes)
			}
		}
	}
}
