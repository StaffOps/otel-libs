# ADR-004: Roll out contract changes Go + Python first, then Node.js, then .NET

**Status**: Accepted | **Date**: 2026-10-27 | **Deciders**: repo owner

> **Amended 2026-10-28 by the owner:** within Phase C the `net10.0` retarget
> (ADR-009, task C0) runs last, after the rest of the .NET catch-up.

## Context

The former steering rule required every env-var/attribute change to land in
all three languages in the same release. The 0.3.0 contract touches ~20
findings per language. .NET is not the owner's current priority, and a
Node.js lib is planned. Holding Go/Python fixes (several HIGH: silent loss of
telemetry, per-signal endpoints ignored, wrong environment label) until .NET
catches up would keep known bugs in production.

## Decision

Phase A: Go + Python land together. Phase B: a new Node.js lib built on the
contract from day one. Phase C: .NET catch-up plus its own backlog. Lagging
languages carry `xfail:<phase>` / `skip:<phase>` entries in
`parity/cases.json`, so the gap is visible in CI.

## Rationale

1. Ships HIGH-severity fixes to the languages in use now.
2. The parity fixture turns "temporarily divergent" into a tracked, failing-if-
   forgotten list instead of a silent drift.
3. Node.js starts on the final contract, avoiding a second migration.

## Consequences

### Positive
- Faster delivery for Go/Python; Node.js built once.
### Negative
- .NET consumers stay on 0.2.x behavior (and its bugs) until Phase C; docs must
  state per-language behavior during the gap.
### Neutral
- Version numbers may diverge per language (Go/Python 0.3.x, .NET 0.2.x).

## When this decision would be wrong

- A .NET consumer hits one of the HIGH .NET findings in production
  (N1 ActivitySource takeover, N2 tracestate loss) or .NET 8 EOL
  (2026-11-10) forces a release.

**Reversal action**: pull that specific item forward as a .NET-only patch
release; the rest of Phase C keeps its place.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| All three in one release (former rule) | Delays HIGH fixes for the languages in use |
| Per-language independent fixes, no shared fixture | Reintroduces the drift this round found |

## References
- `specs/config-contract-v0.3/tasks.md` (Phase C backlog N1–N12)
