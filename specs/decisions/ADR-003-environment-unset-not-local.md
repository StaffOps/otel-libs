# ADR-003: Missing environment / service name never fall back to `LOCAL` / `my-service`

**Status**: Accepted | **Date**: 2026-10-27 | **Deciders**: repo owner, reconciled by `observability` + `dev` agents

## Context

`parseEnvironment` maps unset **and** unknown values to `LOCAL` in all three
libs (Go `go/config.go:53-66`, Python `config.py:44-49`, .NET
`TelemetryOptionsPostConfigure.cs:24-32`). `LOCAL` also switches the log level
to DEBUG and lands in the Collector's 100% LOCAL tail-sampling tier. A PRD pod
that forgets `ENVIRONMENT` therefore ships DEBUG logs to Loki, is labelled
`deployment.environment.name=LOCAL`, and is sampled at 100%. Values like
`production` or `BTC` are coerced to `LOCAL`. The helper values also overwrite
`OTEL_RESOURCE_ATTRIBUTES` (helper attrs are merged last). Likewise an unset
service name becomes `my-service`, merging unrelated services in Tempo/VM.

## Decision

- `deployment.environment.name`: code > `OTEL_RESOURCE_ATTRIBUTES` >
  `ENVIRONMENT` > **unset** (warn once). Unknown values are emitted verbatim;
  only `LOCAL/DEV/HML/PRD` switch helper behavior; unset/unknown behave like a
  non-LOCAL environment (log level INFO).
- `service.name`: code > `OTEL_SERVICE_NAME` > `OTEL_RESOURCE_ATTRIBUTES` >
  `SERVICE_NAME` (deprecated) > `unknown_service:<executable>` + warning (spec
  default). Never a hard error — a telemetry lib must not crash the app.

## Rationale

1. Fail-safe: the cost of a missing variable becomes "unlabelled, INFO" instead
   of "PRD data at DEBUG and 100% sampling under the wrong label".
2. Semconv defines `deployment.environment.name` as free-form; the spec
   default service name is `unknown_service:<process>`.
3. Standard variables winning is already principle 6 in `AGENTS.md`.

## Consequences

### Positive
- Correct identity and environment in Tempo/VM/Loki; no accidental DEBUG in prod.
### Negative
- Breaking: local development without `ENVIRONMENT` no longer gets DEBUG;
  dashboards filtering `=LOCAL` lose unset services.
- Requires a Collector tail-sampling branch for spans without the attribute
  (D11 in design.md) **before** rollout.
### Neutral
- `ENVIRONMENT` and `SERVICE_NAME` keep working as deprecated aliases until 1.0.

## When this decision would be wrong

- The Collector cannot route spans lacking the attribute (no default branch).

**Reversal action**: block the 0.3.0 rollout until the Collector change lands;
do not reintroduce a `LOCAL` default.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Fail setup when `ENVIRONMENT` is missing | A telemetry lib must not take the app down |
| Keep `LOCAL` default | The incident mode described in Context |
| Default to `PRD` | Mislabels dev workloads as production |

## References
- Semconv `deployment.environment.name`, `service.name`
- Resource SDK spec (`unknown_service`)
