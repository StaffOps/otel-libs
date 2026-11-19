package otelhelper

import (
	"context"
	"log/slog"
	"strings"

	"go.opentelemetry.io/otel/attribute"

	"github.com/staffops/otel-libs/go/internal/exclog"
)

// Instrumentation exception records (ADR-014, contract task A36): the ext
// modules wrap their instrumentation's TracerProvider with internal/exclog,
// whose spans call this hook from RecordError. The record goes through the
// same channel as the failure records of WithSpan and RecordFailure.
func init() { exclog.Emit = emitInstrumentationException }

// emitInstrumentationException emits one ERROR record for err in ctx (the
// instrumentation span's context) without touching the span, and returns its
// exception.type (errorTypeOf, the D15 rule), which the span's type-only
// exception event also gets. attrs (db.system.name, the D16
// db.response.status_code and error.type) are added to the record, so DB
// records can be told apart from application failure records. A cancellation
// (isCancellation, the WithSpan rule) writes no record. The stack is
// debug.Stack() at the RecordError call without the helper's leading frames,
// so its first frame is the instrumentation's (XSAM otelsql recordSpanError).
func emitInstrumentationException(ctx context.Context, err error, attrs []attribute.KeyValue) string {
	f := describeError(err)
	if isCancellation(err) {
		return f.typ
	}
	extra := make([]slog.Attr, 0, len(attrs))
	for _, kv := range attrs {
		if kv.Valid() {
			extra = append(extra, slog.String(string(kv.Key), kv.Value.Emit()))
		}
	}
	emitFailureRecord(ctx, f, extra...)
	return f.typ
}

// exclogFramePrefix is how debug.Stack names a function of internal/exclog
// ("github.com/staffops/otel-libs/go/internal/exclog."), derived from the
// running package path as helperFramePrefix is.
var exclogFramePrefix = strings.TrimSuffix(helperFramePrefix, ".") + "/internal/exclog."
