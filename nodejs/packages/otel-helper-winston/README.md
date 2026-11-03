# @staffops/otel-helper-winston

A winston transport for [`@staffops/otel-helper`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md): each record
goes through the OpenTelemetry Logs API (correlated with the span active at
the logging call, also when winston buffers it) while the helper's logs
exporter is active, otherwise as one stdout JSON line of the helper's shape
(`time`, `level`, `msg`, `logger`, the fields, `exception`) — never
both.

> **Not released yet** (`0.3.0-dev.0`); install it from the same release as
> the core (the peer dependency pins the same version).

```js
const winston = require('winston');
const { OtelHelperTransport } = require('@staffops/otel-helper-winston');

const log = winston.createLogger({
  format: winston.format.errors({ stack: true }), // on the logger, not the transport
  transports: [new OtelHelperTransport({ name: 'checkout' })],
});
log.info('order received', { order: 'o2' });
```

- Add the transport explicitly; the helper never rewrites your transports.
- Keep `format.errors()` on the logger: a transport-level format loses
  `logger.error(err)`'s message and stack.
- Records written before the transport was attached arrive uncorrelated.
- `logger.end()` flushes the OTLP records: call it before `process.exit()`.

Details and caveats: [HOW-TO §5](https://github.com/StaffOps/otel-libs/blob/main/nodejs/HOW-TO.md#winston). Peer dependencies:
`@staffops/otel-helper` (same version), `winston` `^3.10.0`. Node.js
22.15.0 or newer. License: Apache-2.0.
