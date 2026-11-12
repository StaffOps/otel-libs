# ADR-005: Go floor 1.26 (otel v1.47 / contrib v0.72) and gRPC stats-handler API

**Status**: Accepted | **Date**: 2026-10-27 | **Deciders**: repo owner, validated by `dev` agent

> **Corrected 2026-10-27 by adversarial review.** The removal version was first
> stated as v0.63; it is v0.61.0.
>
> **Corrected 2026-10-28 during A2.** The first version put a health filter on
> the stats handlers; `otelgrpc.WithFilter` also skips `rpc.server.call.duration`
> (parity case `grpc.health_counted_in_metrics` failed: `rpcs_counted: want 1,
> got 0`), contradicting R7/D5. It also omitted two bump effects: otelhttp
> v0.72 names server spans `<METHOD> <route>` and no longer uses the
> `NewHTTPHandler` `operation` argument for the name; and the v1.47 OTLP/HTTP
> exporters reject the helper's bracket-less IPv6 host (`::1:24318`), fixed with
> `net.JoinHostPort` in A2.

## Context

`go/go.mod` pins `go 1.22`, otel v1.31.0, contrib v0.56.0, otlplog v0.7.0
(October 2024). Measured from proxy.golang.org `.mod` files: otel v1.36–1.38 →
go 1.23, v1.39–1.41 → 1.24, v1.42–1.46 → 1.25, v1.47 → 1.26. Go 1.27 is
released, so upstream supports only 1.26 and 1.27. otelgrpc removed
`Unary/StreamServer/ClientInterceptor` from v0.61.0 on (verified: `^func UnaryServerInterceptor` present in v0.60.0, absent in v0.61.0–v0.63.0); `go/middleware.go:44-61`
exports wrappers over them, so **any** bump past v0.60 breaks the build
(`undefined: otelgrpc.UnaryServerInterceptor`, 4 errors, all in middleware.go).
v1.47 is the release where `otel/log` and `sdk/log` become stable.

## Decision

Target otel/sdk v1.47.0, otel/log + sdk/log v1.47.0, otlplog exporters
v0.23.0, exporters/prometheus v0.69.0, contrib v0.72.0, otelslog v0.21.0,
`go 1.26`; CI on 1.26.x and 1.27.x. Remove the four `*Interceptor()` functions
and add `GRPCServerOption()`, `GRPCDialOption()`, `GRPCServerHandler()`,
`GRPCClientHandler()` (stats handlers). **No `otelgrpc.WithFilter` health
filter**: it drops metrics as well as spans, which R7/D5 forbid; health spans
are dropped by the A7-go sampler instead.

## Rationale

1. The interceptor removal is forced by any useful bump; doing it once with the
   stable logs API avoids a second break.
2. 1.26 is the oldest upstream-supported Go.
3. Prototype: `go vet` + `go test` pass at 92.6% coverage; examples tidy clean.

## Consequences

### Positive
- Current, supported dependencies; stable logs API; `http.route` span names.
### Negative
- Span names change from the fixed `operation` string to `<METHOD> <route>`
  (`GET` when no pattern) — dashboards/queries keyed on old names break.
- Breaking API: callers of `UnaryServerInterceptor()` etc. must switch to
  `grpc.NewServer(otelhelper.GRPCServerOption())`.
- Consumers on Go < 1.26 must upgrade their toolchain.
### Neutral
- `ext/*` modules bump separately (same PR series).

## When this decision would be wrong

- A key consumer cannot move to Go 1.26 before 0.3.0.

**Reversal action**: cut 0.3.0 on otel v1.46 / contrib v0.71 (go 1.25) — the
interceptor removal still applies; only the stable-logs benefit is lost.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Deprecated pass-through interceptors | Would compile but silently drop all gRPC telemetry |
| Stay on v1.31 | Blocks CVE fixes and every finding that needs newer APIs |
| `GRPCServerHandler(...otelgrpc.Option)` variadic options | Would let callers re-add `otelgrpc.WithFilter` (drops RPC metrics, R7) and ties the public API to contrib v0.x option types that already churned; adding helper-owned options later is source-compatible (A2 code-review) |

## References
- `specs/config-contract-v0.3/requirements.md` G4, G5
