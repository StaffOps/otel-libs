# ADR-012: The Node.js helper composes the SDK itself and owns env resolution

**Status**: Accepted | **Date**: 2026-10-29 | **Deciders**: repo owner (Phase B
go-ahead), research by `observability` agent — amends
[ADR-001](ADR-001-defer-otlp-env-to-sdk.md) for Node.js only

## Context

ADR-001 defers to the OTel SDK every OTLP env input the SDK handles per spec.
OpenTelemetry JS 3.0 (2026-10-20) removed environment reading from the stable
trace SDK (`doc/3.x/migration-guide.md:370-405`: `sdk-trace` is
`sdk-trace-base` "with any reading of environment variables removed"). Env is
now read only by the exporters (0.300.x, at construction) and by
`@opentelemetry/sdk-node` 0.300.0 — experimental — whose behavior contradicts
the 0.3 contract in several rows (evidence: research report 2026-10-28):

- with no endpoint every signal defaults to `otlp` → silent export to
  `localhost:4317/4318` (`sdk-node/src/sdk.ts:103-106`; R2 forbids it);
- protocol default `http/protobuf` (contract D1.a: `grpc`), `http/json`
  accepted (contract: warn + ignore);
- `OTEL_TRACES_EXPORTER=none,otlp` → `otlp` (contract R8: treated as `none`);
- metric interval default 60 000 ms (contract 30 000);
- `OTEL_SDK_DISABLED` also skips propagators (contract: kept);
- the Prometheus listener starts in the constructor;
- `diag` is a no-op until set, so every SDK warning is silent (principle 8);
- the default `processDetector` adds `process.command_args` (secret leak).

## Decision

The Node.js helper does **not** use `NodeSDK`. It composes the stable
providers (`sdk-trace`, `sdk-metrics`, `sdk-logs`), the exporters and
`registerInstrumentations` itself and resolves **every D1 row** (design D1 +
edge-case rulings), passing fully resolved values to every exporter it
constructs. The only inputs left to the SDK are the certificate / mTLS file
variables (`OTEL_EXPORTER_OTLP[_<SIGNAL>]_{CERTIFICATE,CLIENT_KEY,CLIENT_CERTIFICATE}`)
of the **HTTP** exporters, read per spec. For gRPC the helper always passes
explicit channel credentials (it reads those files itself), because the gRPC
exporter otherwise builds credentials lazily from env at first export and on
reconnect — after the construction-time masking has ended. Every helper-owned `OTEL_EXPORTER_OTLP[_<SIGNAL>]_*` variable (endpoint,
headers, timeout, compression, protocol, insecure) is hidden from
`process.env` during exporter construction — so rejected values never reach
the wire and the exporter never warns raw values — the Python pattern
(design D1.e family).

## Rationale

1. There is no stable SDK component that honors the contract's env rows; the
   one that reads env is experimental and diverges.
2. Go already resolves almost every row itself (ADR-001 second correction);
   owning them in Node keeps the three languages on one implementation shape.
3. Every owned row is already a parity case, so the cost is code, not new
   contract surface.

## Consequences

### Positive
- Node runs the same 187 parity cases with one resolution model (each passes
  or carries a recorded status — design D-JS14).
### Negative
- More helper code; every exporter release must be re-checked for new env
  reads (the exporter-env checklist in task B13).
### Neutral
- `OTEL_CONFIG_FILE` (declarative config, `@opentelemetry/configuration`) is
  not honored; the helper warns when it is set.

## When this decision would be wrong
- `NodeSDK` (or a stable successor) reaches 1.0 and matches the contract.

**Reversal action**: replace the helper's composition with that component,
keep the helper-owned rows where it still diverges (ADR-001 rule), and prove
it with the unchanged parity fixture.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Wrap `NodeSDK` and patch its divergences | Env read and side effects at construction; patches on an experimental API |
| Defer to exporter env reads (ADR-001 as-is) | Silent localhost export, wrong defaults, silent diagnostics |
