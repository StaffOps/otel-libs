# ADR-006: Debug marker is the existing span attribute `debug=true`, not tracestate

**Status**: Accepted | **Date**: 2026-10-27 | **Deciders**: repo owner, reconciled by `observability` agent

> **Corrected 2026-10-27 by the owner.** The first version of this ADR chose a
> new namespaced attribute `staffops.debug`. The owner asked to keep the
> default instead. OTel defines no standard debug attribute, so "default" here
> means the attribute the libs already emit (`debug=true`), which the
> Collector's tail-sampling policy already matches.

## Context

Debug mode marks root spans so the Collector's tail sampler keeps them. Go and
Python set attribute `debug=true`; .NET additionally prepends `debug=true` to
tracestate (`dotnet/OtelHelper/Tracing/DebugTraceStateProcessor.cs:17`). That
processor treats remote-parent spans as roots (`Activity.Parent == null`),
producing `debug=true,debug=true`; W3C forbids duplicate keys and .NET's
`TraceContextPropagator` then discards the **whole** tracestate downstream,
including other vendors' entries (reproduced).

## Decision

Drop tracestate. Keep span attribute `debug=true` on the service's **entry
spans** — spans whose parent is absent **or remote** — set only from local
config (`OTEL_HELPER_DEBUG_LEVEL=true`). Never derive it from inbound
tracestate or baggage. ("Root" was ambiguous in the first version; the .NET
bug in Context is exactly that ambiguity.)

## Rationale

1. Tail sampling's `string_attribute` policy matches any span buffered for the
   trace, so one marked span keeps the whole trace — propagation is
   unnecessary **provided** the gateway routes by trace ID, `decision_wait`
   covers the trace, and a sampled-decision cache catches late spans. These are
   unverified deployment facts, listed as prerequisites (design.md D11.3).
2. Trusting inbound values lets any caller force 100% retention (cost DoS).
3. Keeping the existing attribute needs no Collector change and no consumer
   migration.

## Consequences

### Positive
- No tracestate corruption; same behavior in every language; no Collector transition.
### Negative
- Any service running with `OTEL_HELPER_DEBUG_LEVEL=true` in PRD forces
  retention of every trace it touches (callers included) and DEBUG logs; the
  Collector policy must `and` the attribute with an environment allow-list and
  a rate limit (design.md D11.2). Debug logs and the span marker are coupled
  (one knob) — documented.
- `debug` is not namespaced (semconv recommends prefixing custom attributes);
  another library setting `debug` could trigger the policy.
### Neutral
- .NET changes in Phase C.

## When this decision would be wrong

- Spans with an unrelated `debug` attribute start being retained by the policy.

**Reversal action**: emit both `debug` and a namespaced key, move the Collector
policy to the namespaced key, then drop `debug` after one release.

A downstream system outside the tail sampler needing the flag in-band would
also reopen this; then use a vendor-scoped tracestate key set once at the true
root, de-duplicated, behind an opt-in.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| New namespaced `staffops.debug` | Owner preferred the existing default; needs a Collector transition |
| Fix the .NET processor and port tracestate to Go/Python via a Sampler | Solves a problem we don't have and keeps the trust issue |

## References
- W3C Trace Context, tracestate
- Collector `tailsamplingprocessor` `string_attribute` policy
