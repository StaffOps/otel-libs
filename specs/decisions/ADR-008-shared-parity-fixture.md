# ADR-008: Enforce cross-language parity with a shared JSON fixture

**Status**: Accepted | **Date**: 2026-10-27 | **Deciders**: repo owner, designed by `dev` agent

## Context

Parity was a documentation rule only. The 2026-10-27 parity matrix found
divergences none of the per-language suites could catch: sampler
(ParentBased in Go only), health exclusion (same 4 paths → 3 behaviors),
port precedence (.NET env beats code), `OTEL_METRIC_EXPORT_INTERVAL=0` (Python
raises, Go/.NET ignore), dead `EXTRA_INSTRUMENTATION` in Go/.NET.

## Decision

A language-neutral fixture `parity/cases.json` (input env/code → expected
normalized output) is executed by every language's test suite in CI. Each
case carries a per-language `status` (`pass` | `xfail:<reason>` |
`skip:<reason>`); an `xfail` that passes fails the build (strict).

## Rationale

1. A divergence surfaces as one failing case id in exactly the language that
   diverges.
2. JSON needs no new dependency in Go, Python, .NET or Node.js.
3. Strict xfail makes phased rollout (ADR-004) self-tracking.

## Consequences

### Positive
- Parity becomes a CI property, not a review checklist.
### Negative
- Each language maintains a small adapter mapping its resolved options to a
  normalized snapshot, and a key map from snake_case `code` options to its API.
- Python cases run in subprocesses (OTel globals are set-once) — slower suite.
### Neutral
- Docker test commands must mount the repo root to reach `parity/`.

## When this decision would be wrong

- Adapters grow into a second implementation of resolution logic (the
  adapter must only *read* resolved state).

**Reversal action**: narrow the fixture to the `resolve` kind and test the
wire-level kinds per language.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| YAML fixture | Needs a parser dependency in Go/.NET |
| One shared conformance binary (e.g. receiver + driver per language) | Heavier; still needs per-language drivers |

## References
- `specs/config-contract-v0.3/design.md` (fixture format)
