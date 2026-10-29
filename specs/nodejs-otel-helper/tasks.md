# Node.js OTel helper — tasks

**Status**: Draft r3 (2026-10-29). Spec: [requirements](requirements.md) ·
[design](design.md) · [research](research.md). Each task: implement + tests in
Docker, flip its parity cases (`skip:B<n>-…` → `pass`, or a D-JS14 status),
refutation (`observability` + `dev`), `code-review`, CHANGELOG, commit.

| Task | Depends on | Covers | Status |
|---|---|---|---|
| B0 Adversarial review r1 | — | format rule | ✅ 2026-10-29 (r2 written) |
| B0b Re-review r2 (observability 1 HIGH, dev 2 HIGH) | B0 | format rule | ✅ 2026-10-29 (r3 written) |
| B0c Re-review r3 (dev: none; observability: 1 HIGH, fixed in r3.1) | B0b | format rule | ✅ 2026-10-29 |
| B0d Confirm r3.1 (observability) | B0c | format rule | ✅ 2026-10-29 (no BLOCKING/HIGH) |
| B1a Workspaces, tsc CJS + ESM wrapper, eslint, publint/attw, node:test, c8 gate, `.gitignore` (node_modules, coverage, *.tgz), dual-load shared-state test | B0d | JS1, JS5, D-JS10, D-JS11 | ✅ 2026-10-29 (dev refutation + code-review approved after fixes) |
| B1b Compose `fakeotlp-node`/`node-tests`, CI matrix 22.15.0/22/24/26, `verify.sh` step, api-range install test | B1a | JS1, D-JS12 | ✅ 2026-10-29 (dev refutation + code-review approved after fixes) |
| B1c Spike gate: hooks on the four Node versions, duplicate-SDK check, context manager wiring, explicit gRPC credentials (no lazy env TLS read) | B1a | D-JS1, D-JS11 | ✅ 2026-10-29 (dev refutation + code-review approved after fixes) |
| B2 Parity runner + driver (all kinds wired), per-case `skip:B<n>` statuses, TLS receiver by DNS name for Node 26 (D-JS14 F5), `trace_matrix.py` JS support, `parity/README.md` Node notes | B1b, B1c | D-JS12 | ☐ |
| B3 Resolver: identity, environment, resource, deprecations, diagnostics sink + DiagLogger | B2 | R4, R5, JS3, JS4, D-JS3–5 | ☐ |
| B4a Endpoints, protocol, TLS warnings, IPv6/zone ids, gRPC :80 (new fixture case `export.grpc_http_explicit_port_80`, run by the Go and Python suites in the same change) | B3 | R1, R2, D-JS3 | ☐ |
| B4b Exporter construction (order: instrumentation before credentials/exporters; 3.0 processors take `{exporter}` options — spike F7), env masking, headers, timeout + effective deadline, compression, BSP/BLRP/limits, gRPC credential errors (new fixture cases: unreadable cert, key without cert, bad PEM — run by Go/Python too) | B4a | R2, R8, JS2 | ☐ |
| B4c Exporter lists, console, `OTEL_SDK_DISABLED`, `OTEL_CONFIG_FILE`, interval | B4b | R8, JS2 | ☐ |
| B5 Sampler (ParentBased, grammar, health wrapper outside it), propagators, debug marker (`debug="true"` on no/remote parent) | B3 | R6, R7, R9 | ☐ |
| B7 `/metrics`: helper listener on `::`, mounted handler status table, unit-suffix adapter (+ fixture assertions on the HTTP histogram name and a counter with a unit, run by the Go and Python suites in the same change), Prometheus fallback | B4c | D-JS8, ADR-007 | ☐ |
| B6 HTTP/undici/express/gRPC instrumentation, health (HTTP + gRPC by name), client exclusions incl. OTLP endpoints, `http_server`/`grpc_server` cases | B5, B7 | R7, R10, D-JS7 | ☐ |
| B8 Logging: helper logger, stdout fallback, console exporter, R4 levels | B4c | JS6, R4 | ☐ |
| B9 Lifecycle: sync setup, rollback (G1), foreign globals, shutdown deadline, re-setup re-binding, `/testing` reset, register entry | B6–B8 | JS5, JS7, D-JS10 | ☐ |
| B9b `cluster` fork kind + multi-worker docs | B9 | JS9, D-JS13 | ☐ |
| B10a `-sql` (pg + mysql2, postgres/mysql services, pool metrics decision) + semconv dup | B9 | JS8, D-JS9 | ☐ |
| B10b `-redis` (redis + ioredis, RESP fake) | B9 | JS8 | ☐ |
| B10c `-aws` (AWS SDK v3 against a local endpoint) | B9 | JS8 | ☐ |
| B10d `-pino` destination, `-winston` transport | B8 | JS6, D-JS6 | ☐ |
| B11a e2e `node-smoke` (+ 13 expectation rows, markers, CI job name) | B10a–d | acceptance | ☐ |
| B11b Examples api → backend (gRPC, notification.proto, proto-loader, health) → process; Dockerfile + Dockerfile.demo (npm pack tarballs) | B11a | acceptance | ☐ |
| B11c custom-signals (same names/shape as Go/Python) + EXAMPLES.md | B11b | acceptance | ☐ |
| B12 Docs: nodejs README/HOW-TO/TESTS; MkDocs nav + `docs/site/nodejs/` symlinks + `docs.yml` paths + `site_description`; root README (matrix, quick start, env table heading, opt-in, gaps, installing); AGENTS.md; CONSUMING (npmjs + GitHub Packages `.npmrc`); release + parity-case skills; `verify.sh` steps; dashboards: none needed (they read .NET example metrics) | B11c | acceptance | ☐ |
| B13 Release: CI `node-test` job, `nodejs/v*` tag trigger, `build-lib`/publish `needs`, npm pack, example images, `publish-nodejs-release` (id-token, npmjs + GitHub Packages); exporter-env re-check checklist per SDK bump | B12 + owner claimed `staffops` npm org | JS10 | ☐ |
| B14 Final validation (refutation + code-review on the whole Node package), Node `0.3.0` rc | B13 | acceptance | ☐ |

**Owner actions**: claim the `staffops` org on npmjs before B13 (ADR-011).
