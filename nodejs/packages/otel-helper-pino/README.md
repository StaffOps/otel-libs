# @staffops/otel-helper-pino

A pino destination for [`@staffops/otel-helper`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md): each record
goes through the OpenTelemetry Logs API (correlated with the active span)
while the helper's logs exporter is active, otherwise as one stdout JSON line
of the helper's shape (`time`, `level`, `msg`, `logger`, the fields,
`exception`) — never both.

> **Release candidate `0.3.0-rc.1`**, published to npmjs (dist-tag `next`, with
> provenance) and to GitHub Packages. Install an exact version, the same as
> the core's (the peer dependency pins it):
> `npm install @staffops/otel-helper@0.3.0-rc.1 @staffops/otel-helper-pino@0.3.0-rc.1 pino`.
> Never a bare `npm install @staffops/otel-helper-pino`: until the first
> stable release, `latest` is a deprecated `0.0.0-bootstrap.0` placeholder.

```js
const pino = require('pino');
const { createPinoDestination } = require('@staffops/otel-helper-pino');

const log = pino({ name: 'checkout' }, createPinoDestination()); // or pino(createPinoDestination())
log.info({ order: 'o1' }, 'order received');
```

- Pass the destination explicitly; do not put it in a `pino.multistream`
  next to `process.stdout` (each record would appear twice on stdout).
- `pino.transport({ target })` is not supported (a worker thread has no
  helper setup).
- `trace` records are always dropped; the helper level
  (`createPinoDestination({ minimumLogLevel })`, default from `setup()`)
  applies on top of pino's.
- `logger.flush(cb)` flushes the OTLP records: call it before
  `process.exit()`.

Exports (the 0.x API contract, [API stability](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md#api-stability-0x)):
`createPinoDestination(options?)`, `VERSION`; types `PinoDestinationOptions`, `PinoDestination`. The default export is the `pino.transport({ target })` entry point, which always throws (unsupported): it is not part of the API contract.

Details and caveats: [HOW-TO §5](https://github.com/StaffOps/otel-libs/blob/main/nodejs/HOW-TO.md#pino). Peer dependencies:
`@staffops/otel-helper` (same version), `pino` `^10.0.0`. Node.js 22.15.0
or newer. License: Apache-2.0.
