# @staffops/otel-helper-sql

SQL instrumentation for [`@staffops/otel-helper`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md): `pg`
(node-postgres, with `pg-pool`) and `mysql2`, through the official
`@opentelemetry/instrumentation-pg` and `-mysql2`.

> **Not released yet** (`0.3.0-dev.0`); install it from the same release as
> the core (the peer dependency pins the same version).

```js
const { setup } = require('@staffops/otel-helper');
const { sqlInstrumentation } = require('@staffops/otel-helper-sql');

setup({ instrumentations: [sqlInstrumentation()] }); // or sqlInstrumentation({ pg: true, mysql2: false })
const pg = require('pg'); // after setup()
```

- Spans carry `db.system.name` and, until 1.0, the legacy `db.system`
  (`OTEL_SEMCONV_STABILITY_OPT_IN=database` drops the legacy key; `db.system.name` is always written), `db.query.text` as
  written — never the bound values — and `server.address`, `server.port`,
  `db.namespace`. No connection string or credentials.
- A failed query keeps status ERROR without a description and
  `error.type=_OTHER`. The exception event: pg a sanitized message (error
  type and SQLSTATE), mysql2 none. The SQLSTATE is on pg's
  `db.client.operation.duration` (`error.type`, callback form only).
- pg also records `db.client.operation.duration` and pool metrics; mysql2
  records none.
- One `sqlInstrumentation()` per process and per `setup()`; create it
  before loading the libraries. `pg` and `mysql2` stay your app's
  dependencies. This package installs `@types/pg` and `@types/pg-pool`
  (runtime dependencies of `@opentelemetry/instrumentation-pg`).

Details and caveats: [HOW-TO §9](https://github.com/StaffOps/otel-libs/blob/main/nodejs/HOW-TO.md#sql-staffopsotel-helper-sql).
Peer dependencies: `@staffops/otel-helper` (same version),
`@opentelemetry/api` `>=1.10.0 <1.11.0`. Node.js 22.15.0 or newer.
License: Apache-2.0.
