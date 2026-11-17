# ADR-016: Application spans get one failure rule in every language — Error on a failure, Unset on success, never Ok

**Status**: Accepted | **Date**: 2026-11-17 | **Deciders**: repo owner (decision during the demo DE9 review). Adversarial review rounds 1 and 2 done 2026-11-17 (`observability`, `spec-refuter`); corrections recorded below and in design D15.

## Context

Spans from instrumentations (HTTP, gRPC, database, Redis) already get their
status from semantic conventions in every language: the instrumentation the
helper turns on sets ERROR on a 5xx server response, a 4xx/5xx client
response, a gRPC error code or a failed statement, and leaves success Unset.

Spans the application creates itself (for example the demo's
`demo.order.process`) differ by language, measured in the code on 2026-11-17:

| Language | Plain SDK, exception or error leaves the span | Helper root-span function |
|---|---|---|
| Python | `start_as_current_span` sets ERROR and records an `exception` event with message and stack (`record_exception=True`, `set_status_on_exception=True` defaults) | `start_root_span` (`python/staffops_otel_helper/tracing.py:220`) inherits that: message and stack on the span |
| Node.js | `startActiveSpan` sets nothing | `startRootSpan` (`nodejs/packages/otel-helper/src/internal/api.ts:140`) records the exception with message and stack, and the description `"<name>: <message>"` |
| Go | nothing (errors are return values) | `StartRootSpan` (`go/tracing.go:142`) sets nothing |
| .NET | nothing | `StartRootActivity` (`dotnet/StaffOps.OtelHelper/ActivitySourceExtensions.cs:27`) sets nothing |

So the same failure gives a different span in each language, and the Python
and Node.js helpers put the exception message and stack on the span, which
[ADR-014](ADR-014-exception-detail-in-correlated-logs.md) rules out.

The span status feeds the platform's span metrics (Collector `span_metrics`
connector, Prometheus label `status_code`; `otel.status_code` behind the connector's `spanmetrics.statusCodeConvention.useOtelPrefix` gate): error rate = `STATUS_CODE_ERROR` / all
calls, with Unset counted as success. A different rule per language gives a
different error rate for the same failures.

The OpenTelemetry trace API says instrumentation libraries SHOULD NOT set Ok
and SHOULD leave the status Unset unless an error occurred; Ok is for an
application developer who wants to override an error. The common practice
(Python SDK, Java `@WithSpan`) is: an exception that leaves the span sets
ERROR.

## Decision

1. **One rule for application spans in Go, Python and Node.js** (.NET later,
   parked by the owner on 2026-11-16): when the work in a span fails — a
   thrown or rejected exception, a returned Go `error`, a Go panic — the span
   gets status ERROR **without description** and `error.type` (the
   exception's type name). When the work succeeds the status stays **Unset**.
   The helper **never sets Ok**.
2. **ADR-014 applies:** the message and the stack trace go to one ERROR log
   record emitted in the span's context (same `trace_id` and `span_id`), once
   per failure. The new functions add **no `exception` span event**
   (*corrected 2026-11-17, review round 1: semconv `exceptions-spans` is
   deprecated in favor of exception log records, and a type-only event only
   repeats `error.type`*). A **cancellation** is not a failure: the span stays
   Unset (semconv HTTP client and gRPC server rules); timeouts are failures.
   The empty description is a deliberate deviation from semconv
   recording-errors (description SHOULD be the message), under its
   sensitive-data carve-out. The record's severity is always ERROR, a
   deliberate deviation from semconv exceptions-logs (WARN for exceptions
   the caller is expected to handle): the helper cannot see whether a caller
   handles a propagated failure. A gRPC `CANCELLED` status is a cancellation
   too (helper span Unset); the instrumentation's gRPC CLIENT span still
   marks it (semconv).
3. **The helper provides it**, so applications do not write it by hand: a
   function that runs work in a span and applies the rule, and a function
   that applies the rule to a span the application already holds (for
   failures that are handled, not thrown). Python `start_root_span` and
   Node.js `startRootSpan` follow the same rule; Go `StartRootSpan` stays a
   plain span starter. `error.type` defaults are native to each language,
   and an override per error (Go `ErrorType() string`, the upstream convention;
   namespaced keys in Python `__otel_error_type__` and Node.js
   `Symbol.for('otel.error_type')`, so library fields are never read) gives one value across languages (design D15).
4. **A business outcome is not automatically an error.** Only a failure that
   reaches the helper (an exception that leaves the span, a returned error)
   marks the span. An outcome the application handles and answers normally
   stays Unset unless the application calls the second function.

## Rationale

1. Same failure, same span, in every language: span metrics, alerts and
   Tempo searches (`status = error`) mean the same thing everywhere — the
   contract's parity principle.
2. It matches the OpenTelemetry API (no Ok from libraries) and the common
   practice (exception → ERROR); the empty description is the one deliberate
   deviation (decision 2).
3. It reuses the ADR-014 rule (type on the span, detail in a correlated
   log), so nothing new about privacy has to be decided.

## Consequences

### Positive
- Error rate from span metrics is comparable across languages.
- Applications get the ADR-014 pattern for their own spans without code of
  their own (the demo's four `failure.*` files become unnecessary).

### Negative
- Behavior change for Python `start_root_span` and Node.js `startRootSpan`:
  the span no longer carries the message, the stack or the status
  description; readers follow the trace → logs link instead (CHANGELOG
  item).
- A failure is now logged by the helper too: an application that already
  logs the same exception gets two ERROR records unless it drops its own.
- .NET stays different until its task is picked up.

### Neutral
- Spans the application starts with the plain SDK API keep the SDK's own
  behavior; the rule applies through the helper functions.

## When this decision would be wrong

- The owner wants handled business errors (a rejected order answered with
  422) to count as span errors by default; then decision 4 is reversed and
  the helper needs a per-call classification hook.
- The platform stops correlating logs with traces; then the detail is
  unreachable from a failed span (same condition as ADR-014).

## Reversal action

If decision 4 is reversed, add a classification callback to the span
function. If the helper's record is unwanted, add an option to skip it (span
marking stays). Both are additive in the next minor (design D15).

## Alternatives

- **Do nothing; document the SDK behavior per language:** rejected. The same
  failure stays different per language, and the Python and Node.js helpers
  keep the ADR-014 violation.
- **A SpanProcessor that marks spans:** rejected. A processor never sees the
  exception, and status cannot change once the span ends.
- **Set Ok on success:** rejected. The trace API says libraries SHOULD NOT,
  and span metrics need no Ok (Unset counts as success).
- **Keep the SDK default (message in the description and in an event):**
  rejected by ADR-014.
