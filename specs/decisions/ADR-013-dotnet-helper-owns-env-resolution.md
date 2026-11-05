# ADR-013: The .NET helper owns env resolution and builds its exporters with explicit values

**Status**: Accepted (2026-11-04, after the reviews) — written in Phase C
spec r2, reviewed in C1b, revised in r3, confirmed in C1c (no BLOCKING, HIGH
or MEDIUM) | **Date**: 2026-11-04 | **Deciders**: repo owner — amends
[ADR-001](ADR-001-defer-otlp-env-to-sdk.md) for .NET only, as
[ADR-012](ADR-012-node-helper-owns-env-resolution.md) did for Node.js

## Context

ADR-001 defers to the SDK every OTLP env input the SDK implements per spec,
and requires an amendment (and fixture cases) whenever the helper takes
ownership of a new input. OpenTelemetry .NET 1.20.1 diverges from the 0.3
contract in ways a deferring helper cannot fix. Evidence — upstream source at
`core-1.20.1`, and measurements in Docker on `mcr.microsoft.com/dotnet/sdk:8.0`
/ `10.0` with OpenTelemetry 1.20.1 unless stated (Phase C C1/C1b `dev`
probes; outputs quoted):

- `AddOtlpExporter` reads **no** per-signal variable
  (`OpenTelemetry.Exporter.OpenTelemetryProtocol` README lines 494-498,
  532-536, 556-560: "AddOtlpExporter: Not supported").
- `OtlpExporterOptions`' constructor reads env/`IConfiguration`
  (`OtlpExporterOptions.cs:361-386` endpoint, protocol, headers, timeout,
  compression; `:489-508` TLS paths into the internal `MtlsOptions`, `:309`).
  Measured: `new OtlpExporterOptions()` with env endpoint, `OTEL_EXPORTER_OTLP_TIMEOUT=1234`
  and `OTEL_EXPORTER_OTLP_COMPRESSION=gzip` returned that endpoint,
  `TimeoutMilliseconds=1234`, `Compression=GZip`.
- `GetHeaders` unescapes the whole headers string before splitting
  (`OtlpExporterOptionsExtensions.cs:37`) and throws
  `ArgumentException: Headers provided in an invalid format.` (`:58`); a
  duplicate key throws `ArgumentException: An item with the same key has
  already been added. Key: k`. Both fail provider construction (measured on
  1.15.3 and 1.20.1).
- The only public seam for TLS and headers is `HttpClientFactory`
  (`OtlpExporterOptions.cs:282-291`), used for **both** protocols
  (`OtlpExporterOptionsExtensions.cs:135-157`). Measured (C1b): with a helper
  factory the gRPC export called it once per exporter, sent HTTP/2
  (`RequestVersionExact`, `application/grpc`) and carried
  `DefaultRequestHeaders` intact (`x-contract=[v,1]` — comma kept —
  `k-bin=[AAE=]`); `Headers=""` with a malformed env headers value did not
  throw. HTTP/protobuf goes through the same factory but calls the
  **synchronous** `HttpClient.Send` (5 of 5 exports); a handler overriding only
  `SendAsync` made every HTTP export return `Failure` without a request.
- When a factory is supplied, the SDK's own limits do not apply to that client
  ("that client is responsible for its own limits", `OtlpExporterOptions.cs:236-239`).
- Exemplars default to `AlwaysOff` in .NET (SDK
  `docs/metrics/customizing-the-sdk/README.md:505-511`), not the spec's
  `trace_based`.
- Registering instrumentations or `AddOtlpExporter` inside a deferred
  `ConfigureOpenTelemetry*Provider((sp, b) => …)` callback throws
  `NotSupportedException: Services cannot be configured after ServiceProvider
  has been created.` (measured, C1 dev B1).

## Decision

The .NET helper resolves every D1 row the Go helper owns **except
`OTEL_SDK_DISABLED` and the attribute value-length limits** (left to the SDK,
table below), **plus the exemplar filter default** (.NET-only, below), from
**code options and process environment variables**, and **constructs** its
OTLP exporters itself (`new OtlpTraceExporter/OtlpMetricExporter/OtlpLogExporter(options)`
inside the processors and readers it adds), assigning every option property
explicitly so that nothing the constructor read from env/`IConfiguration`
survives ("neutralizing SDK-read values"):

| Property | Always set to |
|---|---|
| `Endpoint`, `Protocol`, `TimeoutMilliseconds`, `Compression` | the resolved value (`Compression = GZip` by default — 1.20.1 sends no `Content-Encoding` otherwise, measured) |
| `Headers` | empty string — headers travel on the helper's `HttpClient` |
| `HttpClientFactory` | a helper factory returning **one long-lived client per exporter instance** (the SDK calls it once, measured) on a `SocketsHttpHandler` (or a `DelegatingHandler` that forwards **both** `Send` and `SendAsync`): TLS (custom CA validation, client certificate), contract headers in `DefaultRequestHeaders` (`-bin` values base64), `AllowAutoRedirect = false`, `MaxResponseContentBufferSize` = `MaxResponseSizeBytes`, `Timeout` = the resolved export timeout (as the default client, `OtlpExporterOptions.cs:107-139`) |
| batch / reader options | the resolved values (A18 deadline, batch-size clamp, interval) |

Rows that stay with the SDK, and where it reads them:

| Row | Owner | Source the SDK reads |
|---|---|---|
| `OTEL_SDK_DISABLED` | SDK; the helper reads the **same** key from `IConfiguration` to emit its INFO line and resolve consistently | `IConfiguration` |
| Attribute/event/link limits (`OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`, …) | SDK (applied by the exporter at serialization; limit 0 sends an empty value, measured) — the contract's "0 is invalid" rule cannot be enforced; parity skip candidate | env / `IConfiguration` |
| Retry, transports | SDK | — |
| `OTEL_EXPORTER_PROMETHEUS_HOST` / `_PORT` | owner decision O10 (a), 2026-11-04: ignored — the helper sets `Port` and replaces the listener prefix, so `HOST` has no effect — with one warning naming the variable (value truncated, D1); the contract question goes to Go + Python | env / `IConfiguration` |

**Exception to ADR-001's fixture rule — the exemplar filter.** The helper
parses `OTEL_METRICS_EXEMPLAR_FILTER` (`always_on`, `always_off`,
`trace_based`; default `trace_based`) and always calls `SetExemplarFilter`,
because the .NET SDK default (`AlwaysOff`) differs from the spec. No fixture
kind can observe the filter, so the row is covered by the C9 unit test
`ExemplarFilter_DefaultsToTraceBased_AndHonorsEnv`, not a parity case; Go and
Python leave the filter to their SDKs, whose default is the spec's
(`parity/README.md` note, C9).

## Rationale

1. R2 and the D1 rulings cannot be met by deferring (per-signal variables
   unread; headers and TLS parsed differently or not settable).
2. Go already owns these rows (ADR-001 second correction) and Node does
   (ADR-012); one implementation shape across languages.
3. Every owned row except the exemplar filter is already a parity case.

## Consequences

### Positive
- Contract behavior on the wire, proven by the shared fixture.
- A malformed `OTEL_EXPORTER_OTLP_HEADERS` no longer fails the app; header
  values may contain `,` (measured on gRPC).
### Negative
- The helper's `HttpClient` replaces the SDK default client and bypasses the
  named `IHttpClientFactory` clients (`OtlpTraceExporter`, `OtlpMetricExporter`,
  `OtlpLogExporter`) that apps can customize today — a migration-guide row.
- `OTEL_*` keys only in `appsettings.json` stop affecting helper-owned rows
  (C5 unit test with env plus in-memory `IConfiguration`).
- Each SDK bump must be re-checked for new constructor reads, the default
  client's hardening and the `Send`/`SendAsync` use (task C3 checklist).
### Neutral
- TLS through the helper handler on gRPC is a C8 wire test (the factory is
  measured to reach gRPC; the certificate path itself is not yet measured).

## When this decision would be wrong
- A future SDK honors per-signal variables on `AddOtlpExporter`, exposes TLS
  options publicly and reads only the environment.

**Reversal action**: move that row back to the SDK, drop the helper code for
it, flip its parity cases in the same change (ADR-001 rule).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Defer to the SDK (ADR-001 as written) | R2 fails by construction; malformed headers crash; `IConfiguration` sources |
| `UseOtlpExporter` (reads per-signal env) | One protocol and endpoint per call, cannot be combined with `AddOtlpExporter`, still reads `IConfiguration` (README lines 233-237) |
| Set `Headers` to an env-shaped string and let the SDK parse | Unescape-before-split loses `,` and `%`; duplicate keys throw |
