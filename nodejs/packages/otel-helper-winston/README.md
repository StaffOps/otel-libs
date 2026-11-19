# @staffops/otel-helper-winston

A winston transport for [`@staffops/otel-helper`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md): each record
goes through the OpenTelemetry Logs API (correlated with the span active at
the logging call, also when winston buffers it) while the helper's logs
exporter is active, otherwise as one stdout JSON line of the helper's shape
(`time`, `level`, `msg`, `logger`, the fields, `exception`) — never
both.

> **Release candidate `0.3.0-rc.1`**, published to npmjs (dist-tag `next`, with
> provenance) and to GitHub Packages. Install an exact version, the same as
> the core's (the peer dependency pins it):
> `npm install @staffops/otel-helper@0.3.0-rc.1 @staffops/otel-helper-winston@0.3.0-rc.1 winston`.
> Never a bare `npm install @staffops/otel-helper-winston`: until the first
> stable release, `latest` is a deprecated `0.0.0-bootstrap.0` placeholder.

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

Exports (the 0.x API contract, [API stability](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md#api-stability-0x)):
`OtelHelperTransport` (class), `VERSION`; type `OtelHelperTransportOptions`.

Details and caveats: [HOW-TO §5](https://github.com/StaffOps/otel-libs/blob/main/nodejs/HOW-TO.md#winston). Peer dependencies:
`@staffops/otel-helper` (same version), `winston` `^3.10.0`. Node.js
22.15.0 or newer. License: Apache-2.0.
