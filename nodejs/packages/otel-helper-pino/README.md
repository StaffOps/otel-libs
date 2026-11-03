# @staffops/otel-helper-pino

A pino destination for [`@staffops/otel-helper`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md): each record
goes through the OpenTelemetry Logs API (correlated with the active span)
while the helper's logs exporter is active, otherwise as one stdout JSON line
of the helper's shape (`time`, `level`, `msg`, `logger`, the fields,
`exception`) — never both.

> **Not released yet** (`0.3.0-dev.0`); install it from the same release as
> the core (the peer dependency pins the same version).

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

Details and caveats: [HOW-TO §5](https://github.com/StaffOps/otel-libs/blob/main/nodejs/HOW-TO.md#pino). Peer dependencies:
`@staffops/otel-helper` (same version), `pino` `^10.0.0`. Node.js 22.15.0
or newer. License: Apache-2.0.
