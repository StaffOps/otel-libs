# ADR-011: Owner-prefixed package names in every language

**Status**: Accepted | **Date**: 2026-10-29 | **Deciders**: repo owner

## Context

The libraries are published from a public repository (owner decision,
2026-10-28). Two of the four distribution names are generic and unowned on
their public registries: Python `otel-helper` (404 on PyPI) and .NET
`OtelHelper*` (404 on nuget.org). Anyone can register them, so a consumer who
installs by name — or a resolver that falls back to the public registry —
gets someone else's package (dependency confusion). We worked around it with
documentation (`CONSUMING.md`, `nuget.config` source mapping, no bare name in
`requirements.txt`, an ImportError hint that no longer says `pip install
otel-helper`). Go is already safe (`github.com/staffops/otel-libs/go`) and the
planned Node package is scoped (`@staffops/otel-helper`). The owner asked for
one naming pattern across languages.

## Decision

Every package name carries the owner, `staffops`, in the registry's idiom:

| Language | Distribution / module | Code name | When |
|---|---|---|---|
| Go | `github.com/staffops/otel-libs/go` (+ `ext/*`) | package `otelhelper` (unchanged) | already |
| Python | `staffops-otel-helper` (extras `aws`, `redis`, `sql`) | `import staffops_otel_helper`; helper loggers `staffops_otel_helper.*` | `0.3.0` |
| Node.js | `@staffops/otel-helper`, `@staffops/otel-helper-{aws,redis,sql,pino,winston}` | same | first Node release (Phase B) |
| .NET | `StaffOps.OtelHelper`, `StaffOps.OtelHelper.{AWS,Redis,Sql,Profiling}` | namespace `StaffOps.OtelHelper` | Phase C |

Python keeps a thin compatibility module `otel_helper` for 0.3.x: importing
it re-exports `staffops_otel_helper` and emits one `DeprecationWarning`; it is
removed in 1.0. Environment variables (`OTEL_HELPER_*`) are part of the
configuration contract and do **not** change.

## Rationale

1. A name that carries the owner cannot be published by a third party on
   registries with namespaces (npm scope, Go module path) and is cheap to
   claim on those without (PyPI project, nuget.org ID prefix reservation).
2. 0.3.0 is already a breaking release still at rc — one migration step
   instead of two.
3. One visible pattern ("staffops + otel-helper") across languages.

## Consequences

### Positive
- Dependency-confusion risk becomes a registration task, not a doc warning.
### Negative
- Python consumers change the install name and the import (the shim keeps
  the old import working during 0.3.x); log filters on `otel_helper.*` move
  to `staffops_otel_helper.*` (the shim does not alias the logger names).
- The owner must claim `staffops-otel-helper` on PyPI (if publishing there),
  the `staffops` org on npmjs and the `StaffOps.` ID prefix on nuget.org.
### Neutral
- Go is unchanged; .NET follows in Phase C (backlog item).

## When this decision would be wrong
- The libraries move to a neutral, multi-owner home (e.g. donated).

**Reversal action**: publish under the new owner's prefix with the old names
as deprecated re-export packages for one minor.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Keep generic names + docs | Leaves a standing confusion risk; owner wants one pattern |
| Prefix only the distribution name (keep `import otel_helper`) | Install name ≠ import name; owner chose the full prefix |
