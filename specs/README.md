# Specs

Planning artefacts for `staffops-otel-libs`. Conventions and principles live in
[`../AGENTS.md`](../AGENTS.md); what shipped lives in
[`../CHANGELOG.md`](../CHANGELOG.md).

## Reading order

1. [`config-contract-v0.3/`](config-contract-v0.3/) — **active**. Cross-language
   config contract, parity harness, Go/Python fixes (Phase A), Node.js (B),
   .NET (C). [requirements](config-contract-v0.3/requirements.md) ·
   [design](config-contract-v0.3/design.md) · [tasks](config-contract-v0.3/tasks.md)
2. [`decisions/`](decisions/) — decision records (index below).
3. Shipped (historical, older user-story format; superseded wherever
   `config-contract-v0.3` differs — e.g. health paths D5, gRPC API ADR-005):
   [`metrics-exporter-contract/`](metrics-exporter-contract/) (0.1.0),
   [`go-otel-helper/`](go-otel-helper/) (0.1.0).

## Decision records

| ADR | Decision | Status |
|-----|----------|--------|
| [001](decisions/ADR-001-defer-otlp-env-to-sdk.md) | Defer OTLP exporter env handling to the SDK | Accepted |
| [002](decisions/ADR-002-parentbased-sampler.md) | Default sampler `parentbased_always_on` everywhere | Accepted |
| [003](decisions/ADR-003-environment-unset-not-local.md) | Missing environment/service name never fall back to `LOCAL`/`my-service` | Accepted |
| [004](decisions/ADR-004-phased-rollout-go-python-first.md) | Roll out Go + Python first, then Node.js, then .NET | Accepted |
| [005](decisions/ADR-005-go-1-26-floor-grpc-stats-handlers.md) | Go floor 1.26 and gRPC stats-handler API | Accepted |
| [006](decisions/ADR-006-debug-span-attribute.md) | Debug marker is the existing span attribute `debug=true`, not tracestate | Accepted |
| [007](decisions/ADR-007-keep-prometheus-in-core.md) | Keep Prometheus `/metrics` in core | Accepted |
| [008](decisions/ADR-008-shared-parity-fixture.md) | Enforce parity with a shared JSON fixture | Accepted |
| [009](decisions/ADR-009-dotnet-retarget-net10-only.md) | Retarget .NET to `net10.0` only before .NET 8 EOL | Accepted |

## Format

- Feature specs: `<feature>/requirements.md` (WHEN … THEN … SHALL, acceptance
  checklist, out of scope), `design.md` (each decision: choice, justification,
  trade-offs, when it would be wrong, **reversal action**, alternatives),
  `tasks.md` (status table, dependencies, promotion triggers for deferred work).
  Reference sections (tables, fixture formats, checklists) inside `design.md`
  are exempt from the decision format; decisions inside them are not.
- Decision records: `decisions/ADR-NNN-<imperative-title>.md` — context with
  checkable evidence (file:line, quoted errors, measured values), decision,
  rationale, consequences, when wrong, alternatives.
- No invented numbers: write `unmeasured` when a figure wasn't measured.
- Every spec set gets an independent adversarial review before commit. An
  owner decision recorded without a completed review says so explicitly
  (e.g. ADR-007).
- Corrections are recorded in the document (dated note saying what was wrong),
  never applied silently.
