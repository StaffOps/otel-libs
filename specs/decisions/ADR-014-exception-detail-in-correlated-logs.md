# ADR-014: Exception detail goes to a correlated log; spans carry only the exception type

**Status**: Accepted | **Date**: 2026-11-11 | **Deciders**: repo owner (decision during the .NET C14a review)

## Context

When an instrumented call fails, OpenTelemetry instrumentations record an
`exception` span event with `exception.type`, `exception.message` and
`exception.stacktrace`. For database calls the message is the server's error
text, and that text can carry literal values from the statement or from the
data. Measured on the .NET C14a probe (Npgsql 10, MySqlConnector 2.6):

- `Duplicate entry 'mydupsecret' for key 'c14a_dup.PRIMARY'`
- `22P02: invalid input syntax for type integer: "pgcastsecret"` (repeated
  under `MessageText:` in the stack trace)

The helpers already strip the span status description on failed DB spans and
sanitize the query text (contract A25), but the event kept the same text. A25
recorded that as an accepted residual in every language.

The owner ruled: traces are for identifying and counting failures (span
status, `error.type`, span metrics); the detail of an exception belongs in a
log, linked to the trace by trace/log correlation (same `trace_id` and
`span_id`). If other exceptions raise the same problem, the same path applies.

## Decision

1. **On the span:** a failed call keeps status ERROR (without description),
   `error.type`, and the domain status attributes (e.g.
   `db.response.status_code`). The `exception` event, if kept, carries only
   `exception.type`: no `exception.message`, no `exception.stacktrace`.
2. **In the logs:** the helper emits one log record for the exception, in the
   span's context so that it carries the span's `trace_id` and `span_id`. The
   record has severity ERROR, the exception message as the body, and the
   attributes `exception.type`, `exception.message` and
   `exception.stacktrace`. It goes through the language's normal log pipeline
   (OTLP when a logs exporter is active, else the stdout fallback, D6) and is
   subject to the helper's log-level rules.
3. **Scope now:** DB client spans in all four languages: Go `ext/otelsql`,
   Python `[sql]`, Node.js `-sql`, .NET `StaffOps.OtelHelper.Sql` and the core
   DB span rules. Contract A25 is amended accordingly; .NET implements it in
   C14a, and Go, Python and Node.js in a contract task scheduled before
   their next release candidate.
4. **Rule for everything else:** any other instrumentation whose exception
   text can carry data (request or message payloads, user input, values)
   follows the same pattern when it is changed or reviewed: type on the span,
   detail in a correlated log. Existing HTTP/gRPC/messaging exception events
   are reviewed against this rule as a separate task; this ADR does not change
   them by itself.

## Rationale

1. Traces are sampled, retained and indexed for analysis (Tempo, span
   metrics); they should identify and count failures, not store payload-like
   text. The type plus the status code is enough to group and alert.
2. Logs are the signal built to carry free text, and the place where
   retention, access control and redaction for application text are already
   handled.
3. Correlation keeps the debugging path: from a failed span, the log with the
   same `trace_id`/`span_id` gives the full message and stack trace.
4. One rule in every language keeps parity (the contract's core principle) and
   avoids a per-instrumentation judgement call.

## Consequences

### Positive
- No literal values or row data in span events of failed DB calls.
- Failures stay countable from traces (`error.type`, status, span metrics) and
  fully explained in logs, linked by trace id.
- A single, documented rule for future instrumentations.

### Negative
- Viewing a span alone no longer shows the error message; the reader must
  follow the trace-to-logs link (Grafana Tempo → Loki).
- The exception text now lands in the log backend: logs must be treated as
  possibly containing data values (retention, access control). This is the
  owner's intended trade-off, not a sanitization of the text.
- With no logs exporter and no stdout fallback reader, the detail is only on
  stdout; with logs disabled (`OTEL_LOGS_EXPORTER=none` with no fallback, or
  an app log level above ERROR), the detail is not kept anywhere.
- Some SDKs offer no API to remove an event from a finished span (.NET
  `Activity`); the implementation may need driver options, exporter-side
  rewriting or a Collector rule, decided per language.
- *Added 2026-11-11 (.NET C14a round 3, exception to decision 1):* .NET
  MySqlConnector 2.6 adds its `exception` event after the span has stopped,
  so only the helper's own trace exporters can rewrite it in time. A trace
  exporter or processor the application adds itself still receives the full
  `exception.message` and `exception.stacktrace` for MySqlConnector, and no
  correlated record is written on that path. Recommended: export through the
  helper (`AddOtelHelper` exporters), or delete the two attributes at the
  Collector (`transform` processor, `spanevent` context, on spans with
  `db.system` or `db.system.name`; snippet in `dotnet/HOW-TO.md` §14, not
  tested).

### Neutral
- Applications that need the old behavior can record their own events; the
  helper documents how.

## When this decision would be wrong

- A backend or workflow that cannot correlate traces and logs (no shared
  trace id) is in use, so the detail becomes unreachable from a failed span.
- Exception messages of a given instrumentation are proven to never carry
  data (e.g. fixed error codes only); then keeping the message on the span is
  harmless and this ADR's cost has no benefit for that case.
