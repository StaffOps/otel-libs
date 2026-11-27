# Contributing

This page is the entry point for working on the helpers themselves: the
rules every change follows, where the plans live, and how changes are
tested.

**Audience:** contributors and reviewers.

## Start here

| Document | What it holds |
|----------|---------------|
| [AGENTS.md](https://github.com/StaffOps/otel-libs/blob/main/AGENTS.md) | The canonical guide: principles, environment-variable contract, cross-language parity, packaging, the change loop, tooling. Read it before any change |
| [Handoff](../handoff.md) | Current state of the 0.3 work, open tasks, and how to resume |
| [Specs and decisions](https://github.com/StaffOps/otel-libs/blob/main/specs/README.md) | Requirements, designs, task tables and decision records (ADR-001 to ADR-016) |
| [Changelog and migration](../CHANGELOG.md) | What changed, per language, and how to upgrade from 0.2 |

## The change loop

Every change that is more than a trivial fix goes through these steps:

1. A spec or decision record in `specs/` comes first.
2. Implement the change with tests, in Docker.
3. Independent validation: an observability review and a development
   review that try to refute it.
4. Code review of the diff.
5. A `CHANGELOG.md` entry and the documentation in the same change.

The full rules are in [AGENTS.md](https://github.com/StaffOps/otel-libs/blob/main/AGENTS.md#development-workflow).

## Tests

| Where | What |
|-------|------|
| Per language | Unit and parity tests with a 90 % coverage gate: [Go](../go/TESTS.md), [Python](../python/TESTS.md), [Node.js](../nodejs/TESTS.md), [.NET](../dotnet/TESTS.md) |
| [`parity/`](https://github.com/StaffOps/otel-libs/blob/main/parity/README.md) | The shared contract fixture every language runs |
| [`testing/`](https://github.com/StaffOps/otel-libs/blob/main/testing/README.md) | End-to-end smoke against a real Collector; `testing/verify.sh`, the CI risk matrix locally |
| [`testing/k8s/`](https://github.com/StaffOps/otel-libs/blob/main/testing/k8s/README.md) | Images, smoke jobs, examples and the demo on the k8s01 test cluster |
| [`testing/k8s/soak/`](https://github.com/StaffOps/otel-libs/blob/main/testing/k8s/soak/README.md) | The soak run loop |

## The demo

The four-language demo (worker → api → gRPC backend, PostgreSQL, Redis)
feeds the [Observability](../observe/overview.md) pages and the
[dashboards](../dashboards/overview.md). Its shared contract is in the
[demo README](https://github.com/StaffOps/otel-libs/blob/main/demo/README.md);
the plan is in
[`specs/demo-environment/`](https://github.com/StaffOps/otel-libs/tree/main/specs/demo-environment).
