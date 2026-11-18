# @staffops/otel-helper-sql

SQL instrumentation for [`@staffops/otel-helper`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md): `pg`
(node-postgres, with `pg-pool`) and `mysql2`, through the official
`@opentelemetry/instrumentation-pg` and `-mysql2`.

> **Release candidate `0.3.0-rc.1`**, published to npmjs (dist-tag `next`, with
> provenance) and to GitHub Packages. Install an exact version, the same as
> the core's (the peer dependency pins it):
> `npm install @staffops/otel-helper@0.3.0-rc.1 @staffops/otel-helper-sql@0.3.0-rc.1`.
> Never a bare `npm install @staffops/otel-helper-sql`: until the first
> stable release, `latest` is a deprecated `0.0.0-bootstrap.0` placeholder.

```js
const { setup } = require('@staffops/otel-helper');
const { sqlInstrumentation } = require('@staffops/otel-helper-sql');

setup({ instrumentations: [sqlInstrumentation()] }); // or sqlInstrumentation({ pg: true, mysql2: false })
const pg = require('pg'); // after setup()
```

- Spans carry `db.system.name` and, until 1.0, the legacy `db.system` and
  `db.statement` (= `db.query.text`)
  (`OTEL_SEMCONV_STABILITY_OPT_IN=database` drops the legacy keys; `db.system.name` is always written), `db.query.text` as
  written — never the bound values — and `server.address`, `server.port`,
  `db.namespace`. No connection string or credentials.
- A failed query keeps status ERROR without a description, with
  `db.response.status_code` and `error.type` = the PostgreSQL SQLSTATE
  (`23505`) or the MySQL server error number (`1062`), pool queries included;
  without one (a refused
  connection, a client error, the mysql2 streaming form) `error.type=_OTHER`.
  Message text never reaches these attributes. The exception event: pg a
  sanitized message (error type and SQLSTATE), mysql2 none. pg's
  `db.client.operation.duration` keeps upstream's `error.type` (callback
  form only). mysql2 codes are read through a private method of
  instrumentation-mysql2; without it, `_OTHER` and one WARN line.
- pg also records `db.client.operation.duration` and pool metrics; mysql2
  records none.
- One `sqlInstrumentation()` per process and per `setup()`; create it
  before loading the libraries. `pg` and `mysql2` stay your app's
  dependencies. This package installs `@types/pg` and `@types/pg-pool`
  (runtime dependencies of `@opentelemetry/instrumentation-pg`).

Exports (the 0.x API contract, [API stability](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md#api-stability-0x)):
`sqlInstrumentation(options?)`, `VERSION`; types `SqlInstrumentationOptions`, `SqlInstrumentation`.

Details and caveats: [HOW-TO §9](https://github.com/StaffOps/otel-libs/blob/main/nodejs/HOW-TO.md#sql-staffopsotel-helper-sql).
Peer dependencies: `@staffops/otel-helper` (same version),
`@opentelemetry/api` `>=1.10.0 <1.11.0`. Node.js 22.15.0 or newer.
License: Apache-2.0.
