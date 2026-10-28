# ADR-002: Default sampler is `parentbased_always_on` in every language

**Status**: Accepted | **Date**: 2026-10-27 | **Deciders**: repo owner, reconciled by `observability` + `dev` agents

## Context

Go uses `ParentBased(AlwaysOn|TraceIdRatio)` (`go/tracing.go:29-35`). Python
(`python/otel_helper/tracing.py:29`) and .NET (`dotnet/OtelHelper/TracerSetup.cs:43`)
use bare `AlwaysOn`/`TraceIdRatioBased`. Reproduced: under a remote parent
with `sampled=0`, Go sampled 0/1000 children, Python 1000/1000, .NET 1000/1000;
with ratio 0.5, Python 490/1000 and .NET 507/1000 (parent ignored).

Two reviewers disagreed: one proposed ignoring remote unsampled parents
(`remoteParentNotSampled=AlwaysOn`) so an upstream head sampler cannot cut
traces before tail sampling.

## Decision

`ParentBased(root=AlwaysOn)` by default in all languages. `OTEL_TRACES_SAMPLER`
/`_ARG` still override. `OTEL_HELPER_SAMPLE_RATIO` is deprecated in favor of
`OTEL_TRACES_SAMPLER=parentbased_traceidratio`.

## Rationale

1. It is the spec default (`parentbased_always_on`); Go is already compliant —
   the outliers are Python and .NET.
2. Istio Ambient ztunnel is L4 and never creates or edits `traceparent`; only
   waypoints/ingress (Envoy) can head-sample. The correct fix for those is
   mesh tracing at 100% or disabled — not ignoring the parent.
3. Ignoring `sampled=0` by default produces orphan spans whose parent was
   dropped (Tempo "root span not yet received") and re-emits `sampled=1`,
   making downstream Envoys trace more.

## Consequences

### Positive
- Identical sampling across languages; W3C semantics respected.
### Negative
- Python/.NET services stop recording children of unsampled upstream calls
  (breaking, small). If an Envoy head-samples below 100%, those traces are lost.
### Neutral
- Ratio sampling below 1.0 still compounds with tail sampling — documented as
  discouraged.

## When this decision would be wrong

- A head sampler outside our control (third-party caller, managed ingress)
  sends `sampled=0` for traffic we must trace.

**Reversal action**: add an opt-in option (`WithIgnoreRemoteUnsampledParent` /
equivalent) mapping to `ParentBased(remoteParentNotSampled=AlwaysOn)` — not a
default change.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| `ParentBased(remoteParentNotSampled=AlwaysOn)` default | Orphan spans, breaks W3C semantics for external callers, inflates downstream Envoy volume |
| Keep bare AlwaysOn in Python/.NET | Parity break; ratio<1 then makes independent per-service decisions |

## References
- SDK env vars, `OTEL_TRACES_SAMPLER`: <https://opentelemetry.io/docs/specs/otel/configuration/sdk-environment-variables/>
- Istio Telemetry API `randomSamplingPercentage`
