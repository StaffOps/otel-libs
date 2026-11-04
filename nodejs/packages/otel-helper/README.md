# @staffops/otel-helper

OpenTelemetry helper for Node.js: one call configures traces, metrics and
logs with the cross-language contract of the Go and Python helpers —
standard `OTEL_*` variables first, OTLP export to an OpenTelemetry
Collector, sampling and resource enrichment at the Collector. Instruments
`node:http`/`https`, `fetch`, express and `@grpc/grpc-js`; Prometheus
`/metrics` when no OTLP endpoint is configured; a logger that exports
through OpenTelemetry or writes JSON lines on stdout.

> **Release candidate `0.3.0-rc.1`**, published to npmjs (dist-tag `next`, with
> provenance) and to GitHub Packages. Install an exact version:
> `npm install @staffops/otel-helper@0.3.0-rc.1 @opentelemetry/api@~1.10.0`.
> Never a bare `npm install @staffops/otel-helper`: until the first stable
> release, `latest` is a deprecated `0.0.0-bootstrap.0` placeholder. Registry
> setup (`.npmrc`, GitHub Packages token):
> [CONSUMING.md](https://github.com/StaffOps/otel-libs/blob/main/CONSUMING.md#nodejs-npm-packages).

Requires Node.js 22.15.0 or newer and the peer dependency
`@opentelemetry/api` `>=1.10.0 <1.11.0`.

Two ways to start it, before the libraries it instruments are loaded:

**A. Preload**, configured from the environment (ES modules or CommonJS):

```bash
OTEL_SERVICE_NAME=checkout-api \
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
node --import @staffops/otel-helper/register app.mjs
```

**B. `setup()` in code**, first thing in a CommonJS entry (an ES module app
puts it in a preload file of its own: see the HOW-TO):

```js
const { setup } = require('@staffops/otel-helper');
setup({ serviceName: 'checkout-api' });
```

Either way, flush on `SIGTERM` (the helper installs no signal handler) and
`await shutdown()` before any `process.exit()`; a process that ends on its
own is flushed by the helper:

```js
import { shutdown } from '@staffops/otel-helper'; // CommonJS: require('@staffops/otel-helper')

process.once('SIGTERM', () => { shutdown().finally(() => process.exit(0)); });
```

Opt-in packages: `@staffops/otel-helper-sql`, `-redis`, `-aws`
(instrumentation) and `-pino`, `-winston` (log destinations).

- [Overview and environment variables](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md)
- [API stability (0.x)](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md#api-stability-0x): what a patch release keeps, and the SDK/API version coupling
- [HOW-TO](https://github.com/StaffOps/otel-libs/blob/main/nodejs/HOW-TO.md)
- [Examples](https://github.com/StaffOps/otel-libs/blob/main/nodejs/example/README.md)

License: Apache-2.0.
