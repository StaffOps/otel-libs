# Node.js helper — research input (reference)

Collected 2026-10-28 by an `observability` agent (read-only); verified and
extended by the B0 reviews (2026-10-29). Sources: opentelemetry-js commit
`12a5817` (tags `v3.0.0`, `experimental/v0.300.0`, `api/v1.10.0`),
opentelemetry-js-contrib `main@5f038c56`, npm registry 2026-10-28. Paths:
`exp/…` = `experimental/packages/…`, `pkg/…` = `packages/…`, `contrib/…` =
`packages/…` in contrib. "verify" = not confirmed; settled in the task named.

## Versions

| Package | Version | Stable | engines |
|---|---|---|---|
| `@opentelemetry/api` (logs API folded in) | 1.10.0 | yes | >=8 |
| `core`, `resources`, `sdk-trace`, `sdk-metrics`, `sdk-logs`, `context-async-hooks` | 3.0.0 | yes | >=22.15.0 |
| `sdk-node`, `configuration`, OTLP exporters, `exporter-prometheus`, `instrumentation`, `instrumentation-http`, `instrumentation-grpc` | 0.300.0 | no | >=22.15.0 |
| `semantic-conventions` | 1.44.0 | yes | >=14 |
| contrib `instrumentation-{express 0.72, undici 0.34, pg 0.77, redis 0.72, ioredis 0.72, aws-sdk 0.79, pino 0.70, winston 0.69}` | — | no | >=22.15.0 |
| `@fastify/otel` 0.21.4 | on SDK 2.x | — | — |

Internal pins are exact (`exporter-trace-otlp-grpc` → `otlp-exporter-base
"0.300.0"`, `sdk-trace "3.0.0"`); peers `@opentelemetry/api >=1.10.0 <1.11.0`.
Node lines 2026-10-28: 22 maintenance (EOL 2027-04-30), 24 maintenance (EOL
2028-04-30), 26 active LTS (EOL 2029-04-30); upstream CI 22.15.0/22/24/26.

## Facts the design relies on

| Fact | Evidence |
|---|---|
| Stable trace SDK reads no env; sampler/BSP/limits env gone | `doc/3.x/migration-guide.md:370-405`, `:383-399` |
| `sdk-trace` registers no context manager or propagator | migration guide `:418-441` |
| `NodeSDK`: no endpoint → `otlp` to localhost; protocol default `http/protobuf`; `none,otlp` → `otlp`; interval 60 000; `OTEL_SDK_DISABLED` skips propagators; Prometheus server in constructor | `exp/opentelemetry-sdk-node/src/sdk.ts:103-106,183-239,218-228`; `utils.ts:131-162,313-361` |
| `diag` no-op until set; `setLogger` warns old and new logger unless `suppressOverrideMessage` | `api/src/api/diag.ts:60-95` |
| HTTP exporter parses env URL at construction and warns the **raw** value | `exp/otlp-exporter-base/src/platform/node/configuration/otlp-node-http-env-configuration.ts:46-85` |
| Schemeless HTTP env endpoint → silent fallback to localhost | same file `:60-90` |
| Headers via baggage parser (splits `;`, decodes keys, drops silently); env merged beneath code headers | `pkg/opentelemetry-core/src/baggage/utils.ts:49-144`; `otlp-http-configuration.ts:20-47`; `otlp-grpc-configuration.ts:63-104` |
| gRPC target = `new URL().host` (drops default port: `http://h:80` → `h` → grpc-js 443) | `otlp-grpc-configuration.ts:42-61,105`; grpc-js 1.14.6 `resolver-dns.js:39` |
| gRPC TLS files read lazily at first export and on client rebuild | `grpc-exporter-transport.ts:119-192`; `otlp-grpc-env-configuration.ts:227-245` |
| TLS: scheme wins over INSECURE (gRPC); HTTP never reads INSECURE | `otlp-grpc-env-configuration.ts:227-246`; `otlp-node-http-configuration.ts:42-54` |
| Timeout ms, `Number()` grammar, no clamp; compression case-sensitive, default none | `shared-env-configuration.ts:9-62` |
| BSP/BLRP `exportTimeoutMillis` default 30 000; metric reader throws if timeout > interval, default interval 60 000 | `sdk-trace/src/export/BatchSpanProcessorBase.ts:48`; `sdk-logs/src/export/BatchLogRecordProcessorBase.ts:156`; `sdk-metrics/src/export/PeriodicExportingMetricReader.ts:78,133-148` |
| `url.path` set at HTTP server span start; duration recorded for non-recording spans; `ignoreIncomingRequestHook` drops metrics too; HTTP stable-semconv only | `exp/opentelemetry-instrumentation-http/src/utils.ts:811-813`; `http.ts:528-596,878-928`; `experimental/CHANGELOG.md:154` |
| Outgoing HTTP has no suppression check → OTLP/HTTP exports record client metrics | `http.ts:676-700`; `core/src/internal/exporter.ts:21-27` |
| gRPC: span name `grpc.<svc>/<method>`; `ignoreGrpcMethods` matches the method segment only; no meter | `instrumentation-grpc/src/instrumentation.ts:228-246,311-318`; `serverUtils.ts:199-238` |
| Prometheus exporter: host `localhost`, `port || 9464`, server from constructor, errors only to `diag`; serializer adds `_total` but no unit suffix | `exp/opentelemetry-exporter-prometheus/src/PrometheusExporter.ts:21-23,68-69,85-166`; `PrometheusSerializer.ts:104-133,514-520` |
| Resource env detector: raw (untrimmed) service name; a bad `OTEL_RESOURCE_ATTRIBUTES` item discards all, logged at `diag.debug`; `processDetector` adds `process.command_args` | `pkg/opentelemetry-core/src/platform/node/environment.ts:41-47`; `pkg/opentelemetry-resources/src/detectors/EnvDetector.ts:33-132`; `ProcessDetector.ts:30-49` |
| `serviceInstanceIdDetector`: per-module `randomUUID` | `ServiceInstanceIdDetector.ts:14-33` |
| No proxy meter provider (meters before setup stay no-op); `set*GlobalProvider` refuses duplicates | `api/src/api/metrics.ts:42-50` |
| Contrib DB instrumentations emit only `db.system.name` (pg at span start); ignore `OTEL_SEMCONV_STABILITY_OPT_IN` | `contrib/instrumentation-pg/src/utils.ts:137-164`; pg CHANGELOG 0.73.0 |
| instrumentation-pino replaces the stream with a multistream (always tees) | `contrib/instrumentation-pino/src/instrumentation.ts:71-111` |
| Exemplar filter not exported/wired | `sdk-metrics/src/index.ts`; `sdk-node/src/create-from-config.ts:1224` |
| import-in-the-middle sync hooks: 22.22.3+, 24.11.1+, 25.1+, 26 | iitm 3.5.3 `supports-sync-hooks.mjs` |
| npm: `@staffops/*` and `staffops` scope unregistered (404 / "Scope not found") | registry queries 2026-10-28 |
