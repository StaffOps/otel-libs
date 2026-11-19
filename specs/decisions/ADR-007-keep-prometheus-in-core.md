# ADR-007: Keep the Prometheus `/metrics` exporter in core

**Status**: Accepted | **Date**: 2026-10-27 | **Deciders**: repo owner

## Context

Proposal (2026-10-27): move the Prometheus exporter out of core into an opt-in
accessory package in every language (Go `ext/otelprometheus`, Python
`otel-helper[prometheus]`, .NET `OtelHelper.Prometheus`). Motivations found:
Python `opentelemetry-exporter-prometheus` is a beta that pins
`opentelemetry-sdk~=1.Y.0` (0.66b1 → `~=1.45.1`), dictating the SDK version
for every consumer; the .NET exporter never left beta (core pins
`1.9.0-beta.2`); Go's `exporters/prometheus` is v0.x. Costs: breaking the
"no endpoint → `/metrics`" fallback (principle 2), a new extension point in
each core, splitting the metrics-contract test suites.

## Decision

Not pursued. Prometheus stays in core with the existing fallback and
`OTEL_METRICS_EXPORTER` contract. The validation agents were stopped before
reporting; this is an owner decision on cost/benefit.

## Rationale

1. Avoids a silent breaking change for consumers scraping `/metrics`.
2. The lockstep problem is contained by bounding SDK and contrib as a matched
   pair (design.md D9) instead.

## Consequences

### Positive
- No migration for scrape-based consumers.
### Negative
- Core keeps a beta dependency in Python and .NET.
### Neutral
- .NET exporter bump (N7) still planned in Phase C, with a `/metrics` output diff first.

## When this decision would be wrong

- The Python lockstep pin blocks an SDK upgrade we need, or the .NET beta
  blocks a stable NuGet release (NU5104 on pack).

**Reversal action**: reopen the accessory-package proposal with the
validation round (observability + dev) that was not completed.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Opt-in accessory package in all languages | Effort and breaking change judged not worth it now |

## References
- `specs/metrics-exporter-contract/design.md` D1 (original "no subpackage" decision)
