# ADR-010: Go module path matches the repository name (`github.com/staffops/otel-libs/go`)

**Status**: Accepted | **Date**: 2026-10-28 | **Deciders**: repo owner

## Context

The repository is `github.com/StaffOps/otel-libs`, but the Go module path,
NuGet `RepositoryUrl`, docs and release commands still used the former name
`staffops-otel-libs`. `go get` of the old path only works through GitHub's
rename redirect, and a module whose path names a repository that no longer
exists breaks the day that name is reused or the redirect is dropped. Found
during A13 validation (code-review M2).

## Decision

From `0.3.0` the Go modules are `github.com/staffops/otel-libs/go` and
`github.com/staffops/otel-libs/go/ext/{otelaws,otelredis,otelsql}`. Every
reference to the old name in code, CI, package metadata and current docs
moves to `otel-libs`. Historical records (released CHANGELOG sections,
`specs/go-otel-helper/`) keep the old name as written at the time.

## Rationale

1. A module path must resolve without relying on a redirect.
2. `0.3.0` is already breaking for Go; one migration step instead of two.
3. Lower-case `staffops` keeps the path as it was (no `!staff!ops` proxy
   escaping) and GitHub owners are case-insensitive.

## Consequences

### Positive
- `go get`, `pkg.go.dev` and the module proxy resolve the real repository.
### Negative
- Every Go consumer rewrites its imports when moving to 0.3 (migration step).
- Releases `go/v0.1.0`…`go/v0.2.1` keep declaring the old path; pinning them
  keeps working through the redirect only.
### Neutral
- Python and .NET package names are unchanged.

## When this decision would be wrong

- The repository is renamed again. **Reversal action**: a new major path, or a
  vanity import path the org controls.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Keep the old path, rely on the redirect | Fragile, and invisible until it breaks |
| Vanity import path (custom domain) | Needs a domain and hosting the org hasn't set up |
