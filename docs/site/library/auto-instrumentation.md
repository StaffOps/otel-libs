# Auto-instrumentation

What each helper instruments without application code, and the metrics that
come out of it. Use it to know which series exist before you write a query
or a dashboard.

**Audience:** application engineers and dashboard authors.
**Prerequisites:** a service set up with its language's helper
([Overview](../index.md#quick-start)).

## Instrumentations installed

| Language | Core (always) | Opt-in packages | Upstream versions on this branch |
|----------|---------------|-----------------|----------------------------------|
| Go | HTTP server and client (`NewHTTPHandler`, `NewHTTPTransport`), gRPC client and server (stats handlers), Go runtime metrics, `slog` bridge | `ext/otelsql` (XSAM otelsql), `ext/otelredis` (redisotel), `ext/otelaws` | otel SDK 1.47.0; contrib otelhttp, otelgrpc and runtime v0.72.0; XSAM otelsql v0.44.0; redisotel v9.23.0; otelaws v0.72.0 |
| Python | FastAPI / ASGI, requests, httpx, gRPC client and server (sync and aio), system and process metrics, `logging` | extras `[sql]` (SQLAlchemy), `[redis]`, `[aws]` (botocore) | SDK `>=1.42,<1.46`; contrib `>=0.63b0,<0.67b0` (the demo lock pins SDK 1.45.2 and contrib 0.66b2) |
| Node.js | `node:http` / `https`, express, `fetch` (undici), `@grpc/grpc-js`; `createLogger()` | `-sql` (pg, mysql2), `-redis` (redis, ioredis), `-aws`, `-pino`, `-winston` | SDK 3.0 / experimental 0.300; express ~0.72, undici ~0.34, pg ~0.77, mysql2 ~0.70, redis and ioredis ~0.72, aws-sdk ~0.79 |
| .NET | ASP.NET Core, HttpClient, gRPC client, .NET runtime, `ILogger` | `.Sql` (SqlClient, Npgsql, MySqlConnector), `.Redis`, `.AWS`, `.Profiling` | OpenTelemetry 1.20.1; SqlClient 1.20.2; AWS 1.20.3; GrpcNetClient, StackExchangeRedis and the Prometheus listener 1.20.1-beta.1; Npgsql 10.0.3 |

Versions are read from `go/go.mod` and `go/ext/*/go.mod`, `python/pyproject.toml`,
`nodejs/packages/*/package.json` and the .NET project files.

## Metrics emitted automatically

Prometheus names, as stored by a Prometheus-compatible backend (Prometheus on
the demo, VictoriaMetrics in production). The OTel name is the same with dots
(`http_server_request_duration_seconds` = `http.server.request.duration`,
unit seconds). Observed on the k8s01 demo (worker → api → gRPC backend,
PostgreSQL, Redis) on 2026-11-23 by an instant query per service.

| Language | HTTP | gRPC | Database and pools | Runtime and process | Other |
|----------|------|------|--------------------|---------------------|-------|
| Go | `http_server_request_duration_seconds`, `http_client_request_duration_seconds` | `rpc_server_call_duration_seconds`, `rpc_client_call_duration_seconds` | `db_client_operation_duration_seconds`, `db_sql_connection_*` (`ext/otelsql`); Redis pool `db_client_connections_*` with `db_system="redis"` (`ext/otelredis`, redisotel; on the api and worker, the Redis users) | `go_memory_used_bytes`, `go_memory_allocated_bytes_total`, `go_memory_allocations_total`, `go_memory_gc_goal_bytes`, `go_goroutine_count`, `go_processor_limit`, `go_config_gogc_percent`; `go_memory_limit` only with `GOMEMLIMIT` | — |
| Python | `http_server_request_duration_seconds`, `http_client_request_duration_seconds` | none | `db_client_connections_usage` (labels `pool_name`, `state` = `idle` / `used`; SQLAlchemy, `[sql]`); no `db_client_operation_duration_seconds` | `system_cpu_*`, `system_memory_*`, `system_swap_*`, `system_disk_*`, `system_network_*`, `system_thread_count`, `process_cpu_*`, `process_memory_*`, `process_context_switches_total`, `process_open_file_descriptor_count`, `process_thread_count`, `process_disk_io_bytes_total`, `cpython_gc_*` and `process_runtime_cpython_*` (both naming generations are present; source to be measured (phase 2)) | — |
| Node.js | `http_server_request_duration_seconds`, `http_client_request_duration_seconds` | none | `db_client_operation_duration_seconds`, `db_client_connection_count`, `db_client_connection_max`, `db_client_connection_pending_requests` (`-sql`, pg) | none | no exemplars |
| .NET | `http_server_request_duration_seconds` (gRPC server calls included, status 200), `http_client_request_duration_seconds` (gRPC client calls as `POST`) | none | `db_client_operation_duration_seconds`, `db_client_operation_failed_total`, `db_client_connection_*`, `db_client_operation_npgsql_*`, `db_client_connection_npgsql_create_time_seconds` (`.Sql`, Npgsql) | `dotnet_gc_*`, `dotnet_jit_*`, `dotnet_thread_pool_*`, `dotnet_monitor_lock_contentions_total`, `dotnet_timer_count`, `dotnet_assembly_count`, `dotnet_exceptions_total`, `dotnet_process_*` (names on `net8.0` vs `net10.0`: to be measured (phase 2)) | `kestrel_active_connections`, `kestrel_connection_duration_seconds`, `kestrel_queued_connections`, `kestrel_queued_requests`, `aspnetcore_routing_match_attempts_total`, `aspnetcore_memory_pool_*`, `dns_lookup_duration_seconds` |

!!! warning "Python `pool_name` carries the connection URL"
    The SQLAlchemy pool metric labels each series with `pool_name`, which
    holds the engine's connection URL (host, port, database and user name;
    check how your engine renders the password before exposing it). Each
    distinct URL is a new series, and the label
    exposes the database address to anyone who can read metrics. Drop or
    rewrite the label at the Collector if that matters, or exclude the
    instrument with `OTEL_HELPER_DISABLED_METRICS`.

Every service also gets, from the Collector gateway and independent of the
SDK and of tail sampling (spans the SDK drops are not counted; there are
none with the default `parentbased_always_on`): `traces_span_metrics_calls_total`,
`traces_span_metrics_duration_seconds` (span metrics) and
`traces_service_graph_request_*` (service graph). See
[Metrics](../observe/signals/metrics.md).

### Still to be measured (phase 2)

These rows wait for the cluster after the soak; do not rely on them yet:

1. Exemplar presence on the gRPC and database histograms per language.
   On the HTTP histograms it is verified on the demo: Go, Python and .NET
   carry exemplars, Node.js none.
2. .NET runtime metric names on `net8.0` and `net10.0`.
3. Python: which instrumentation emits each CPython generation.
4. The metrics added by `OTEL_HELPER_HTTP_OPT_IN_METRICS=true`, per language.
5. AWS and MySQL opt-in metrics (the demo uses PostgreSQL and Redis only).
6. The Prometheus names of the custom-signals examples.

## Controlling the volume

- `OTEL_HELPER_HTTP_OPT_IN_METRICS=true` also records the HTTP metrics
  semantic conventions make opt-in: `http.server.active_requests` and the
  request and response body sizes, where upstream has them. Go has no
  active-requests metric upstream; Node.js records none of them; .NET
  records `http.server.active_requests` and its client connection metrics,
  without body sizes. By default only `http.server.request.duration` and
  `http.client.request.duration` are recorded. In code: Go
  `WithHTTPOptInMetrics`, Python `http_opt_in_metrics`, Node.js
  `httpOptInMetrics`, .NET `TelemetryOptions.HttpOptInMetrics` (code wins).
- `OTEL_HELPER_DISABLED_METRICS` drops instruments by name pattern.
- `OTEL_METRICS_EXPORTER=none` turns metrics off; `prometheus` serves
  `/metrics` in addition to, or instead of, OTLP.

Details per language: Go [README](../go/README.md#what-is-configured-automatically),
Python [README](../python/README.md#what-is-configured-automatically),
Node.js [README](../nodejs/README.md#what-is-configured-automatically),
.NET [README](../dotnet/README.md#what-is-instrumented-automatically).

## Health checks in metrics

Health requests produce no server span, but they are counted in
`http.server.request.duration` (and Go `rpc.server.call.duration`). Exclude
them in SLI queries, for example
`http_route!~"/health|/healthz|/ready|/readyz|/live|/livez|/ping|/metrics"`.
