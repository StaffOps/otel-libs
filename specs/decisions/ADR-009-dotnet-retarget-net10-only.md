# ADR-009: Retarget .NET to `net10.0` only before .NET 8 end of support

**Status**: Accepted | **Date**: 2026-10-27 | **Deciders**: repo owner

> **Amended 2026-10-28 by the owner:** execution order is Phase B (Node.js),
> then the rest of Phase C (.NET), and C0 last — no longer "before
> 2026-11-10". Consequence: the published .NET package keeps targeting
> `net8.0` past its end of support until Phase C finishes.

## Context

Every .NET project targets only `net8.0`. .NET 8 (LTS) reaches end of support
on **2026-11-10**, 14 days after this decision. CI's `10.0.x` leg runs the net8
build on the net10 runtime via `DOTNET_ROLL_FORWARD=Major`, so net10 is never
compiled. `dotnet build OtelHelper.sln` already fails on sdk 8.0
(`NETSDK1045`) because `example/dotnet-api` targets net10.0. A dev prototype
(2026-10-27) built `net8.0;net10.0` with OpenTelemetry 1.20.1 at 180/180 tests,
after moving test references to `Microsoft.Extensions.*` 10.x (`NU1605`
otherwise). .NET is last in the rollout order (ADR-004); the owner chose to
handle only the runtime move now.

## Decision

Retarget all .NET libraries, tests and examples to `net10.0` **only**; CI runs
`10.0.x` only. No other Phase C change rides along (OpenTelemetry stays on
1.15.x; the Prometheus exporter bump N7 still needs its `/metrics` diff first).

## Rationale

1. A supported runtime is the one .NET item with a hard date.
2. Single TFM keeps the matrix small while .NET is not a priority; dual
   targeting would add a leg for a runtime that is out of support in two weeks.
3. Keeping the change minimal avoids pulling Phase C work forward.

## Consequences

### Positive
- Supported runtime; solution builds; CI compiles what it tests.
### Negative
- **Breaking for consumers still on .NET 8**: the new package won't install on
  `net8.0` apps; they stay on 0.2.x.
### Neutral
- OpenTelemetry .NET packages ship assets that load on net10; the SDK bump is
  separate.

## When this decision would be wrong

- A consumer must stay on .NET 8 past EOL (e.g. extended support contract).

**Reversal action**: multi-target `net8.0;net10.0` (already prototyped, 180/180).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| `net8.0;net10.0` | Supports an out-of-support runtime; owner chose net10 only |
| Do nothing until Phase C | Ships on an unsupported runtime after 2026-11-10 |

## References
- `specs/config-contract-v0.3/tasks.md` N6, C0
