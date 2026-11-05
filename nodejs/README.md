# OTel Helper — Node.js

OpenTelemetry helper for Node.js services. One call configures traces, metrics
and logs with the shared cross-language contract: standard `OTEL_*` variables
first, export through an OpenTelemetry Collector, sampling and resource
enrichment at the Collector. The resolution rules, defaults and warnings are
the Go and Python ones; the shared fixture `parity/cases.json` proves it.

Audience: developers instrumenting a Node.js service. The practical guide is
[HOW-TO.md](HOW-TO.md); runnable examples are in [example/](example/README.md);
how to test the library is in [TESTS.md](TESTS.md).

> **Status: release candidate `0.3.0-rc.1`**, the first npm release (tag
> `nodejs/v0.3.0-rc.1`): the six packages at the same version, published to
> npmjs (dist-tag `next`, with provenance through trusted publishing) and to
> GitHub Packages. Install an exact version, e.g.
> `npm install @staffops/otel-helper@0.3.0-rc.1`; never a bare
> `npm install @staffops/otel-helper`: until the first stable release,
> `latest` on npmjs is a deprecated `0.0.0-bootstrap.0` placeholder. See
> [Installation](#installation).

## Requirements

- Node.js **22.15.0 or newer** (`engines`); CI tests 22.15.0, 22, 24 and 26.
- OpenTelemetry JS stable packages `~3.0.0` and experimental `~0.300.0` (one
  generation, pulled by the helper), with `@opentelemetry/api`
  `>=1.10.0 <1.11.0` as a **peer dependency** — the range the 3.0 SDK
  packages declare. npm 7 and later install it automatically; list it in your
  own `package.json` when your code imports it.
- CommonJS and ES modules: one implementation serves `require` and `import`
  (the ES module entry re-exports the CommonJS one, so both share one state).

## Packages

| Package | What it adds | Peer dependencies |
|---|---|---|
| `@staffops/otel-helper` | Core: `setup()`, `shutdown()`, exporters, sampler, propagators, `/metrics`, `createLogger()`, and the `node:http`/`https`, `fetch` (undici), express and `@grpc/grpc-js` instrumentations | `@opentelemetry/api` |
| [`@staffops/otel-helper-sql`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/packages/otel-helper-sql/README.md) | `sqlInstrumentation()`: `pg` (with `pg-pool`) and `mysql2` | core (same version), `@opentelemetry/api` |
| [`@staffops/otel-helper-redis`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/packages/otel-helper-redis/README.md) | `redisInstrumentation()`: node-redis (`redis`, `@redis/client`) and `ioredis` | core (same version), `@opentelemetry/api` |
| [`@staffops/otel-helper-aws`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/packages/otel-helper-aws/README.md) | `awsInstrumentation()`: every AWS SDK for JavaScript v3 client (`@aws-sdk/client-*`) | core (same version), `@opentelemetry/api` |
| [`@staffops/otel-helper-pino`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/packages/otel-helper-pino/README.md) | `createPinoDestination()`: pino records through the helper | core (same version), `pino` `^10.0.0` |
| [`@staffops/otel-helper-winston`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/packages/otel-helper-winston/README.md) | `OtelHelperTransport`: winston records through the helper | core (same version), `winston` `^3.10.0` |

The core stays lightweight: databases, Redis, the AWS SDK and the log
libraries are opt-in packages. The instrumented libraries themselves (`pg`,
`redis`, `@aws-sdk/client-*`, `pino`, `winston`) stay your app's own
dependencies.

## Installation

From npmjs, always at an exact version; every opt-in package at the core's
version (its peer dependency pins it):

```bash
npm install @staffops/otel-helper@0.3.0-rc.1 @staffops/otel-helper-sql@0.3.0-rc.1 \
  @opentelemetry/api@~1.10.0
npm audit signatures   # registry signatures and provenance attestations
```

A release candidate is reachable only by version or by the dist-tag `next`;
until the first stable release `latest` is the deprecated bootstrap
placeholder, so a bare `npm install @staffops/otel-helper` is never right.
GitHub Packages carries the same version with the same bytes (no provenance,
and GitHub requires a `read:packages` token even for public packages). The
`.npmrc` scope mapping that keeps every `@staffops/*` request on one
registry, the token and the `actions/setup-node` settings are in
[CONSUMING.md](../CONSUMING.md#nodejs-npm-packages).

npm 11 warns
`1 package has install scripts not yet covered by allowScripts: protobufjs@… (postinstall …)`
(protobufjs comes with `@grpc/grpc-js` through `@grpc/proto-loader`, a dependency of the gRPC OTLP exporters): harmless — that postinstall only
prints a warning when a project pins protobufjs with an unexpected version
range, so leaving it unapproved, or `--ignore-scripts`, changes nothing.

### From `npm pack` tarballs (an unreleased commit)

To try a commit that is not published, install tarballs built from this
repository, as the examples' `HELPER_SOURCE=workspace` build and their CI
images (`Dockerfile.demo`) do (the examples' default build installs the
published version: [example/README.md](example/README.md#build)):

```bash
# In a checkout of this repository (Docker; no local Node needed): tarballs in nodejs/tarballs/
docker run --rm -v "$PWD/nodejs:/src:ro" -v "$PWD/nodejs/tarballs:/out" node:24-slim sh -ec '
  mkdir /work && tar -C /src --exclude=node_modules --exclude=dist --exclude=tarballs -cf - . | tar -C /work -xf -
  cd /work && npm ci --ignore-scripts && npm run build
  for p in packages/*; do (cd "$p" && npm pack --pack-destination /out); done'

# In your app: the core first, or in the same `npm install` as any opt-in package
npm install /path/to/staffops-otel-helper-0.3.0-rc.1.tgz /path/to/staffops-otel-helper-sql-0.3.0-rc.1.tgz \
  @opentelemetry/api@~1.10.0
```

Install the core tarball before, or in the same `npm install` as, any opt-in
tarball: an opt-in tarball installed alone makes npm resolve its peer
`@staffops/otel-helper` (that exact version) from the registry — the
published bytes, not your build, or an error for a version that was never
published. The tarballs land in `nodejs/tarballs/`, owned by root (the
container's user); remove them with `sudo` or from another container.

## Quick start

The helper must run before your app loads `express`, `@grpc/grpc-js` and the
other libraries it instruments. Pick one of two ways.

**A. Preload, configured from the environment:**

```bash
export OTEL_SERVICE_NAME=checkout-api
export OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=PRD
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317

node --import @staffops/otel-helper/register app.mjs   # ES modules or CommonJS
node --require @staffops/otel-helper/register app.js   # also works; see HOW-TO §13 for worker threads
```

**B. `setup()` in code**, first thing in a CommonJS entry (code values win
over the environment):

```js
const { setup } = require('@staffops/otel-helper');

setup({ serviceName: 'checkout-api', resourceAttributes: { 'app.component': 'gateway' } });

const express = require('express'); // after setup(): instrumented, full http.route
```

In an ES module app, code options need a preload file of your own (an
`import` statement is hoisted, so `setup()` in the entry runs after the
imports): see [HOW-TO §1](HOW-TO.md#1-getting-started).

Either way, the helper installs no signal handler: flush on `SIGTERM`
yourself.

```js
// ES modules
import { shutdown } from '@staffops/otel-helper';
// CommonJS: const { shutdown } = require('@staffops/otel-helper');

process.once('SIGTERM', () => { shutdown().finally(() => process.exit(0)); });
```

A short-lived job that simply returns is flushed when the process ends on its
own (the helper's `beforeExit` listener); one that calls `process.exit()`
must `await shutdown()` first ([HOW-TO §4](HOW-TO.md#short-lived-jobs-and-scripts)).

## Environment variables

Precedence everywhere: **code (`setup()` options) > standard `OTEL_*`
variable > deprecated / `OTEL_HELPER_*` variable > helper default**. An
invalid environment value warns on stderr (`otelhelper: …`) and falls back to
the next level; an invalid value passed in code throws a `TypeError` naming
the option. The helper's warnings never print header values, client keys or URL
credentials; forwarded SDK messages have exporter response bodies dropped,
the configured OTLP header values masked and credential-like text redacted
(pattern-based, [HOW-TO §14](HOW-TO.md#14-validation-and-diagnostics)). The OpenTelemetry JS SDK 3.0 reads no environment variable
itself: the helper resolves every row below and passes the result to the SDK.

### Service identity

| Variable | Default | Notes |
|---|---|---|
| `OTEL_SERVICE_NAME` | _(unset)_ | Wins over `OTEL_RESOURCE_ATTRIBUTES` and `SERVICE_NAME`. Unset everywhere → SDK `unknown_service:<argv0>` (`unknown_service:node` for the usual `node` binary) + one warning. Whitespace-only = unset |
| `OTEL_RESOURCE_ATTRIBUTES` | _(unset)_ | Any resource attribute, e.g. `deployment.environment.name=PRD`. Never overwritten by a helper default. Parsed by the helper: values percent-decoded then trimmed; an item without `=`, with an empty key or a bad `%` escape is warned by position (never by value); a blank identity value (`service.name=`) counts as unset; `telemetry.sdk.*` are ignored (the SDK sets them) |
| `SERVICE_NAME` | _(unset)_ | **Deprecated** (removed in 1.0) → `OTEL_SERVICE_NAME`. Used only when nothing else sets `service.name` |
| `ENVIRONMENT` | _(unset)_ | **Deprecated** alias → `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<value>`. Unset → attribute absent + one warning; unknown values are emitted verbatim |
| `OTEL_NODE_RESOURCE_DETECTORS` | _(unset)_ | Not used: the helper builds the resource itself (no host, process or cloud detectors). A value other than `env`, `serviceinstance`, `none` is warned once — add those attributes in the Collector (`resourcedetection`, `k8sattributes`) |

`service.instance.id`: a random UUID per process unless code or
`OTEL_RESOURCE_ATTRIBUTES` sets one; an explicit id is never rewritten (and is
warned in a `cluster` worker — see HOW-TO, [cluster and pm2](HOW-TO.md#cluster-and-pm2)).
The resolved environment sets the helper's log level: `LOCAL` debug,
`DEV`/`HML` info, `PRD` warn; unset or any other value info.

### OTLP export

| Variable | Default | Notes |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(unset)_ | Generic endpoint. For `http/protobuf`, `/v1/<signal>` is appended (a path prefix such as `/otlp` is kept); no port → 443/80. Schemeless `host:port` → `https://` (deprecated for `http/protobuf`). gRPC: a path (other than `/`) is ignored and warned once, naming the variable only — `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://c:4317/v1/traces` sends to `c:4317` (A29). |
| `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` | _(unset)_ | `TRACES`, `METRICS`, `LOGS`. Configures that signal only. `http/protobuf`: used as-is, path included (no `/v1/<signal>` appended). gRPC: only `scheme://host:port` is used, a path is ignored |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_PROTOCOL` | `grpc` | `grpc` or `http/protobuf`. Unset and endpoint port `4318` → `http/protobuf`. `http/json` is warned and ignored |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` | `false` | Only for schemeless endpoints. `true` (any case) = plaintext; any other value = false, warned unless it is `false` |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT` | `10000` | Milliseconds, plain decimals only, clamped to [1 ms, 24 h]. The single export deadline: `OTEL_BSP_EXPORT_TIMEOUT`, `OTEL_BLRP_EXPORT_TIMEOUT` and `OTEL_METRIC_EXPORT_TIMEOUT` are not honored (warned when set). The metric reader timeout is capped at the export interval (warned) |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_COMPRESSION` | `gzip` | `gzip` or `none` (any case); anything else warns and uses `gzip` |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_HEADERS` | _(unset)_ | `key=value,...`, values percent-decoded. Keys must match `[A-Za-z0-9._-]+`, are lowercased, last one wins; malformed items are skipped with a warning naming only their position. Values of 4 characters or more, and each space-separated part of 4 or more (the token of `Bearer <token>`), are masked as `<redacted>` in forwarded SDK text, and response bodies are dropped (B14; the A29 fix round aligned the forms with Go and Python and masks longest first). |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE`, `_CLIENT_CERTIFICATE`, `_CLIENT_KEY` | _(unset)_ | Read and validated by the helper (per-signal, then generic). An unreadable or non-PEM CA warns and the system roots are used; an incomplete or rejected client pair warns and the signal uses server authentication. A certificate with a plaintext endpoint is warned |

Each per-signal variable falls back to the generic one, then to the default; an
invalid per-signal value is warned and falls back the same way. A signal is
"OTLP configured" when code, the generic endpoint or its own endpoint sets
one; with only `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, traces are exported,
metrics use the Prometheus fallback and logs stay local. While the exporters
are built, the helper hides these variables from `process.env`, so a value it
rejected never reaches the SDK or the wire.

### Signals, sampling, propagation

| Variable | Default | Notes |
|---|---|---|
| `OTEL_SDK_DISABLED` | `false` | `true` installs the context manager and propagators only (one INFO line): no providers, exporters, `/metrics` listener or instrumentation. The helper's SDK `DiagLogger` is still installed |
| `OTEL_CONFIG_FILE` / `OTEL_EXPERIMENTAL_CONFIG_FILE` | _(unset)_ | Declarative configuration is **not supported**: ignored with one warning per process naming the variable (never the path) |
| `OTEL_TRACES_EXPORTER` / `OTEL_LOGS_EXPORTER` | `otlp` | `otlp`, `console`, `none` or a list (`otlp,console`). `logging` is a deprecated alias of `console` (variables only: in `metricExporters` it throws) |
| `OTEL_METRICS_EXPORTER` | _(inferred)_ | `otlp`, `prometheus`, `console`, a list, `none`. Unset: `otlp` when a metrics endpoint is configured, else `prometheus` |
| `OTEL_METRIC_EXPORT_INTERVAL` | `30000` | Milliseconds (the SDK default is 60000): ASCII digits with an optional `+` (any number of digits), > 0, else warned and 30 000; above 2 147 483 647 clamped with a warning |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | The six spec names, case-insensitive. Unknown name → warning + default; invalid `_ARG` → warning + 1.0. Across languages prefer `parentbased_traceidratio` to a bare `traceidratio` ([HOW-TO §7](HOW-TO.md#7-sampling)) |
| `OTEL_PROPAGATORS` | `tracecontext,baggage` | `tracecontext`, `baggage`, `none`. Other values are warned and skipped. A propagator registered before `setup()` is kept (warned) unless `replaceGlobals` is set |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | _(unset)_ | Not set by the helper. HTTP: the Node.js instrumentations emit only the stable conventions, so a value without `http`, or with `http/dup`, is warned once and has no HTTP effect. Database keys (opt-in packages): both `db.system.name` and the legacy `db.system` unless the value holds `database` without `database/dup`; read once per process by the first `sqlInstrumentation()`, `redisInstrumentation()` or `awsInstrumentation()` |
| `OTEL_BSP_*`, `OTEL_BLRP_*`, `OTEL_*_LIMIT` | spec defaults | Batch processor sizes and delays and the attribute, span, event, link and log record limits are read by the helper (SDK 3.0 no longer does). A value-length limit of `0` is invalid (warned); a batch size above the queue size warns and uses the queue size |
| `OTEL_LOG_LEVEL` | `WARN` | **Node only.** Level of the OpenTelemetry SDK's own messages (`NONE`, `ERROR`, `WARN`, `INFO`, `DEBUG`, `VERBOSE`, `ALL`), written by the helper's `DiagLogger` to stderr. Unset → WARN (not the spec's INFO); another value warns and uses WARN. `installDiagLogger: false` leaves `diag` alone |
| `OTEL_METRICS_EXEMPLAR_FILTER` | — | Not honored: `@opentelemetry/sdk-metrics` 3.0 exports no exemplar filter, so Node.js metric points carry no exemplars (Go and Python do) |

### Helper variables

| Variable | Default | Notes |
|---|---|---|
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug log level and `debug=true` on entry spans (no parent or a remote parent) for Collector tail-sampling policies ([ADR-006](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-006-debug-span-attribute.md)). Do not leave on in production |
| `OTEL_EXPORTER_PROMETHEUS_PORT` | _(unset)_ | Standalone `/metrics` listener port; beats `OTEL_HELPER_METRICS_PORT`, code (Node.js: `prometheusMetricsPort`) still wins. Same grammar; `0` = no listener; invalid → warned, next level (A29) |
| `OTEL_EXPORTER_PROMETHEUS_HOST` | every interface | Listener bind address: an IPv4/IPv6 literal (brackets allowed, no zone id) or `localhost` (= `127.0.0.1`); `::` = every interface. Unset binds every interface — **not** the spec default `localhost`, so other pods can scrape. Other values (host names, `*`) are warned and every interface is used; no code option (A29) |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Standalone Prometheus `/metrics` listener (dual-stack `::`, `GET`/`HEAD /metrics` only: other paths 404, other methods 405); ASCII digits with an optional `+`, in [0, 65535]; `0` disables it (mount `metricsHandler()` instead). A busy port does not fail `setup()`: it is reported. Used when `OTEL_EXPORTER_PROMETHEUS_PORT` is unset or invalid. |
| `OTEL_HELPER_DISABLED_METRICS` | _(unset)_ | Comma list of patterns, each a case-sensitive glob on the instrument name (`*` any run of characters, `?` one character, everything else literal); items trimmed, blank items ignored; matching instruments are dropped from every exporter |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | **Deprecated** → `OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `OTEL_TRACES_SAMPLER_ARG`. Used only while `OTEL_TRACES_SAMPLER` is unset |
| `OTEL_HELPER_DISABLED_SIGNALS` | _(unset)_ | **Deprecated** → `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=none`. A standard variable with a valid entry wins for its signal, and `metricExporters` wins for metrics; unknown entries warn once |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | — | **No effect in Node.js** (warned once): pass `sqlInstrumentation()`, `redisInstrumentation()` or `awsInstrumentation()` in `setup({ instrumentations })` |

Deprecated variables warn once per process, naming the replacement (never the
value), also when a standard variable or code wins.

## Endpoint and TLS rules

| Endpoint | Transport |
|---|---|
| `https://host:4317` | TLS (system CA store) |
| `http://host:4317` | Plaintext |
| `host:4317` (schemeless) | TLS; plaintext only with `OTEL_EXPORTER_OTLP_INSECURE=true` (or `setup({ insecure: true })`). Deprecated for `http/protobuf` |

- The scheme always wins over `INSECURE`; a contradiction is warned (and,
  when both come from code, throws).
- gRPC URL without a port → `:4317` with a warning; `http/protobuf` URL without
  a port → 443/80.
- URL user info, query and fragment are stripped (warned, redacted): put
  credentials in `OTEL_EXPORTER_OTLP_HEADERS`.
- `unix://`, `ftp://` and other schemes leave the signal unconfigured.
- gRPC over TLS to an **IP literal** (`https://10.0.0.5:4317`) fails on
  Node.js 26 (Node refuses an IP as the TLS server name); the helper warns on
  every Node.js version. Use a DNS name.

## What is configured automatically

| Signal | Captured |
|---|---|
| Traces | `node:http`/`https` servers and clients, `fetch` (undici), express (route-named SERVER span plus a `request handler - <route>` span per matched handler), `@grpc/grpc-js` client and server. Health probes produce no server span |
| Metrics | `http.server.request.duration` and `http.client.request.duration` (stable semconv, seconds), your instruments; every 30 s. No runtime metrics (Go and Python emit `go.*` / `system.*`, `process.*`) |
| Logs | `createLogger()` records (and the pino/winston packages) with trace/span ids, exported when a logs exporter is active; single-line JSON on stdout otherwise |

Without any OTLP endpoint: metrics on `http://<pod>:9464/metrics`, traces
in-process only (propagation still works), logs as JSON on stdout.

Health checks: server spans for `/health /healthz /ready /readyz /live /livez
/ping /metrics` (exact, case-insensitive, trailing slash and query ignored)
and every `grpc.health.v1.Health` call are not recorded, but the HTTP
requests **are counted** in `http.server.request.duration`. Override the list
with `setup({ healthPaths: [...] })` (`[]` traces every path).

## Public API

| Name | Purpose |
|---|---|
| `setup(options?) → Resolved` | Configure everything (synchronous); while a setup is active, later calls return its frozen result and ignore their options |
| `shutdown(timeoutMs = 30000) → Promise<void>` | Flush and shut down under one deadline; rejects with an `AggregateError` (a provider failed) or an `Error` (deadline) |
| `SetupOptions`, `Resolved` | Code configuration and the resolved values (types; [HOW-TO §12](HOW-TO.md#12-setupoptions-and-resolved-reference)) |
| `getTracer(name?, version?)` / `getMeter(name?, version?)` | Tracer / Meter from the global providers (default scope name `otel-helper`, shared with Go and Python) |
| `startRootSpan(name, fn, { tracer?, attributes?, kind?, links? })` | Run `fn(span)` in a new trace; ends the span, records a throw or rejection |
| `createLogger({ name, level? })` | The helper's logger (`debug`, `info`, `warn`, `error`) |
| `defaultLogLevel(environment?, debug?)` | The level an environment implies (`LOCAL` debug, `DEV`/`HML` info, `PRD` warn) |
| `metricsHandler()` | `(req, res)` handler serving `/metrics` from the app's own server (`node:http`, express) |
| `VERSION` | The helper version |
| `@staffops/otel-helper/register` | Preload entry: the module hooks, then `setup()` from the environment |
| `@staffops/otel-helper/hooks` | Preload entry: the module hooks only (then call `setup({...})` yourself) |
| `@staffops/otel-helper/testing` | Tests only: `reset()`, `setDiagnosticSink()`, `getResource()`, `getProviders()` (and `getState()`, `STATE_KEY`, for the helper's own suites) |

The `@staffops/otel-helper/internal/*` entries are for the opt-in packages
only: not a public API, no compatibility promise.

## API stability (0.x)

Before 1.0 a minor release (`0.3` → `0.4`) may break the API; a patch
release does not. What a patch keeps, and what may change in any release:

| Contract (kept within a minor release) | Not contract (may change in any release) |
|---|---|
| The root exports of `@staffops/otel-helper` (the [Public API](#public-api) table) and of each opt-in package: `-sql` `sqlInstrumentation`, `VERSION` (types `SqlInstrumentationOptions`, `SqlInstrumentation`); `-redis` `redisInstrumentation`, `VERSION` (types `RedisInstrumentationOptions`, `RedisInstrumentation`); `-aws` `awsInstrumentation`, `VERSION` (types `AwsInstrumentationOptions`, `AwsInstrumentation`); `-pino` `createPinoDestination`, `VERSION` (types `PinoDestinationOptions`, `PinoDestination`); `-winston` `OtelHelperTransport`, `VERSION` (type `OtelHelperTransportOptions`) | `@staffops/otel-helper/testing`: `reset()`, `setDiagnosticSink()`, `getResource()` and `getProviders()` are kept as far as possible; `getState()`, `STATE_KEY` and `HelperState` are for the helper's own test suites only (marked `@internal`), their shape changes without notice |
| `@staffops/otel-helper/register` and `@staffops/otel-helper/hooks` (entry names and behavior) | `@staffops/otel-helper/internal/*` (`bridge`, `contrib`, `db`): used by the opt-in packages, which peer-depend on the core at the same version |
| The fields of `SetupOptions` and `Resolved` (names, types, meaning), and the environment-variable contract | Diagnostic message wording (match on the variable name, not the text); the `-pino` default export (the `pino.transport({ target })` entry point, which only throws) |

**OpenTelemetry version coupling.** Two `SetupOptions` fields take SDK
types: `spanProcessors` (`SpanProcessor` from `@opentelemetry/sdk-trace`
`~3.0.0`) and `instrumentations` (`Instrumentation` from
`@opentelemetry/instrumentation` `~0.300.0`). Pass objects built against
the same SDK generation the helper depends on; a helper release that moves
to a new SDK generation changes these types and is a minor release. The
`@opentelemetry/api` peer range `>=1.10.0 <1.11.0` follows the SDK packages
the helper pulls; widening or moving it is also a minor release.

## Known differences from Go and Python

The configuration contract is shared; some emitted telemetry is not. The
cross-language list is in the root README,
[Known emitted-telemetry gaps](https://github.com/StaffOps/otel-libs/blob/main/README.md#known-emitted-telemetry-gaps); the
full Node.js record, with the status of each item, is
[design D-JS14](https://github.com/StaffOps/otel-libs/blob/main/specs/nodejs-otel-helper/design.md#d-js14--known-node-differences-reference-each-with-a-fixture-status).
The ones you meet first:

- gRPC spans carry the legacy `rpc.*` attributes, names `grpc.<pkg.Svc>/<Method>`,
  and no RPC metrics; a handler that throws synchronously leaves its SERVER
  span unended.
- Only two HTTP metrics; `http.route` only with express; `node:http2` is not
  instrumented.
- No runtime metrics and no exemplars.
- A TracerProvider, MeterProvider or propagator registered before `setup()` is
  kept (warned) unless `replaceGlobals: true`; `setup()` after `shutdown()`
  rebuilds all three signals and keeps earlier tracers, meters and loggers
  working (Go keeps earlier tracers on the old provider; Python cannot
  re-enable traces and metrics).

## Architecture

```text
[ Node.js app ] --OTLP gRPC :4317 or HTTP :4318--> [ OTel Collector ] --> traces / metrics / logs backends
```

- The SDK samples parent-based always-on; tail sampling happens at the Collector.
- The helper sets `service.name`, `deployment.environment.name` and
  `service.instance.id`; the Collector (`k8sattributes`) adds the rest.
- Nothing is exported directly to a backend; the only exception is the
  Prometheus `/metrics` fallback.
- The helper composes the SDK itself (no `NodeSDK`): one stable forwarding
  TracerProvider, MeterProvider and LoggerProvider per process are the API
  globals, and each `setup()` points them at its own SDK providers.
