# @staffops/otel-helper-redis

Redis instrumentation for [`@staffops/otel-helper`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md):
node-redis (`redis`, `@redis/client`) and `ioredis`, through the official
`@opentelemetry/instrumentation-redis` and `-ioredis`.

> **Not released yet** (`0.3.0-dev.0`); install it from the same release as
> the core (the peer dependency pins the same version).

```js
const { setup } = require('@staffops/otel-helper');
const { redisInstrumentation } = require('@staffops/otel-helper-redis');

setup({ instrumentations: [redisInstrumentation()] }); // or redisInstrumentation({ ioredis: true, redis: false })
const { createClient } = require('redis'); // after setup()
```

- Spans are named by the command, uppercased (`SET`; connection spans
  `connect`), with `db.system.name=redis` (and, until 1.0, the legacy
  `db.system`), `db.operation.name`, `server.address`/`server.port` and
  `db.query.text` as `SET ? ?`: no keys, values or passwords.
  `db.operation.name` is the command as the client provides it (node-redis
  `SET`, ioredis `set`; not normalized, as semconv recommends).
- One span per command, an ioredis command issued before `ready` included
  (its span covers the wait for the connection).
- A failed command keeps status ERROR without a description; `error.type`
  is the Redis reply code (`WRONGTYPE`, `ERR`), else `_OTHER`. The exception
  event keeps the server's error text, which can quote arguments.
- No Redis metrics. ioredis traces every connect and reconnect handshake as
  root spans; an ES module app needs the loader hooks for ioredis
  (`node --import @staffops/otel-helper/register`).
- One `redisInstrumentation()` per process and per `setup()`; create it
  before loading the clients, which stay your app's dependencies.

Exports (the 0.x API contract, [API stability](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md#api-stability-0x)):
`redisInstrumentation(options?)`, `VERSION`; types `RedisInstrumentationOptions`, `RedisInstrumentation`.

Details, caveats and a Collector filter for the ioredis handshake roots:
[HOW-TO §9](https://github.com/StaffOps/otel-libs/blob/main/nodejs/HOW-TO.md#redis-staffopsotel-helper-redis). Peer
dependencies: `@staffops/otel-helper` (same version), `@opentelemetry/api`
`>=1.10.0 <1.11.0`. Node.js 22.15.0 or newer. License: Apache-2.0.
