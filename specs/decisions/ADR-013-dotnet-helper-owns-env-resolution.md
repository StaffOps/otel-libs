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

> **Added 2026-11-10 (task C5 fix round, code-review H1).** Row
> `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`: read by the **helper**
> from the same source the SDK reads (`IConfiguration`, else the environment),
> as for `OTEL_SDK_DISABLED`: `cumulative`, `delta`, `lowmemory`
> (case-insensitive); anything else warns naming the variable and the default
> (`cumulative`). Reason, measured on 1.20.1: the SDK's `MetricReaderOptions`
> read the variable only when `AddOtlpExporter` registers them, so the reader
> the helper constructs got `Cumulative` whatever the variable said (0.2.x got
> the value through `AddOtlpExporter((e, r) => …)`). `OTEL_METRIC_EXPORT_TIMEOUT`
> is still read by the SDK's `MetricReaderOptions` (measured) and stays there.
> The SDK constructor's parse warnings for the rows the helper resolves and
> warns about (`OTEL_EXPORTER_OTLP_ENDPOINT`, `_PROTOCOL`, `_COMPRESSION`) are
> not forwarded; those for the other OTLP variables are (design D-NET5).

> **Added 2026-11-10 (task C5 fix round 2).** (1) The temporality is applied
> through the default `MetricReaderOptions` (an `IConfigureOptions` registered
> by `AddOtelHelper`, as upstream registers one in `AddOtlpExporter`), so an
> application's later `Configure<MetricReaderOptions>` wins; it accepts the
> names only (`cumulative`, `delta`, `lowmemory`), where upstream's
> `Enum.TryParse` also took a number: a number warns. (2)
> `OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION` is **not applied**:
> the SDK applies it only on the reader `AddOtlpExporter` builds (the reader
> option is internal), and calling `AddOtlpExporter` is a service-registering
> call, so it must run when `AddOtelHelper` runs (D-NET3); whether OTLP is
> active is known only at provider build, so the reader would exist for every
> app — exporting to a default endpoint or, made inert, aggregating every
> series a second time (measured 1.9× heap for the idle-reader case). The
> helper warns once naming the variable (migration guide, telemetry row 8).
> (3) `OTEL_METRIC_EXPORT_INTERVAL` is resolved by the helper from the process
> environment, while `OTEL_METRIC_EXPORT_TIMEOUT` is still read by the SDK's
> `MetricReaderOptions` from `IConfiguration`: an `IConfiguration`-only interval
> has no effect, an `IConfiguration`-only timeout does (documented; task C9
> decides one source). (4) `IConfiguration`-only `OTEL_DOTNET_EXPERIMENTAL_OTLP_*`
> keys (retry, disk storage) are no longer read for the helper's exporters
> (found by the C5 fix-round review; not measured in this round), and
> an `IConfiguration`-only invalid `OTEL_EXPORTER_OTLP_PROTOCOL`,
> `_COMPRESSION` or `_ENDPOINT` is ignored without a warning (the helper reads
> the process environment; the SDK constructor's warnings for those three are
> not forwarded). Nothing of (4) is restored by this round.

> **Added 2026-11-10 (task C5 fix round 3, owner decision: option (c)).**
> *(Superseded in its details by the round-4 and round-5 notes below.)*
> Item (2) of the note above is superseded: when
> `OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION` is set as
> `AddOtelHelper` runs (process environment, or an `IConfiguration` instance
> already registered), the helper registers the SDK's named `AddOtlpExporter`
> metric path eagerly, so the SDK applies the aggregation, and sets every
> value late: the named `OtlpExporterOptions` get the helper's endpoint,
> protocol, timeout, compression, empty `Headers` and `HttpClientFactory`; the
> named `MetricReaderOptions` get the resolved interval and temporality before
> any application `Configure`, which still wins. With OTLP metrics inactive at
> provider build, that reader gets a client that answers locally (no network)
> and the helper warns once. Measured: `base2_exponential_bucket_histogram`
> sends Metric field 10 (exponential histogram), unset sends field 9.
> Without the variable nothing changes: no extra reader.

> **Added 2026-11-10 (task C5 fix round 4).** The path of option (c) is
> registered only for `base2_exponential_bucket_histogram` (case-insensitive);
> `explicit_bucket_histogram` is the default and needs none; any other value
> warns by name. Where the variable is read: the process environment, or an
> `IConfiguration` registered as an instance before `AddOtelHelper`. A host
> builder registers its configuration as a factory (`HostBuilder`
> `PopulateServiceCollection`, `services.AddSingleton(_ => appConfiguration)`),
> so an `appsettings.json`-only value cannot be seen when `AddOtelHelper` runs:
> it is warned once at provider build, not applied. The named reader options
> start from the default-named `MetricReaderOptions` (an app's default
> `Configure`, the helper's temporality, the SDK's export timeout) and get the
> resolved interval; an inactive reader only collects (interval
> `int.MaxValue`). `UseOtlpExporter()` together with the variable throws
> `NotSupportedException` at provider build (upstream refuses the
> combination): a migration-guide line.

> **Added 2026-11-10 (task C5 fix round 5).** `UseOtlpExporter()` next to
> `AddOtelHelper()` exports every signal twice (measured by the review: 4
> `/v1/metrics` requests for 2 flushes); the helper detects the SDK's internal
> `OpenTelemetry.Exporter.UseOtlpExporterRegistration` by type name (the
> registration `EnsureNoUseOtlpExporterRegistrations` checks) and warns once at
> provider build. With the histogram variable: registered before
> `AddOtelHelper`, the helper registers no histogram reader (warned;
> `UseOtlpExporter`'s reader applies the variable from `IConfiguration`, so the
> host-configuration warning is skipped); registered after, the SDK's
> `NotSupportedException` at provider build cannot be avoided, and a callback
> registered just before the helper's `AddOtlpExporter` writes one ERROR first.
> The variable is compared as written (case-insensitive, untrimmed), as the SDK
> reads it: a padded value warns and registers nothing. The host-configuration
> warning names the process-environment value `AddOtelHelper` used when it
> differs from `IConfiguration`. *(Round 6, 2026-11-10:* that happens only when a source outranking the
> environment — the command line — holds `base2_exponential_bucket_histogram`;
> in the default host the environment outranks `appsettings.json`. With
> `UseOtlpExporter()` first and the variable in env, two warnings print — no
> second reader, and the doubling — and the doubling warning prints only when
> the helper itself has an OTLP target.*)

> **Added 2026-11-10 (task C8).** The exporter rows are the helper's now: headers, timeout,
> compression and TLS files per signal; the resolved timeout is passed to the exporter, the
> client, the batch processors and the periodic reader, so `OTEL_METRIC_EXPORT_TIMEOUT` (and
> `OTEL_BSP_EXPORT_TIMEOUT`, `OTEL_BLRP_EXPORT_TIMEOUT`) are no longer honored — they warn
> (contract row "Export deadline"); this supersedes "stays there" in the C5 notes above. The
> client is a `SocketsHttpHandler` wrapped by a handler that forwards `Send` and `SendAsync`,
> adds `grpc-timeout` on gRPC (the .NET exporter sends none) and reports a `3xx` as an ERROR
> per signal and status. Compression defaults to `GZip`, as decided above.

> **Added 2026-11-10 (task C9).** Item (3) of the C5 fix-round-2 note is decided:
> `OTEL_METRIC_EXPORT_INTERVAL` stays a process-environment row, parsed by the
> contract grammar, and since C8 the export timeout is the helper's as well, so
> neither reads `IConfiguration` (an `appsettings.json`-only value has no effect;
> test `Interval_Only_In_IConfiguration_Is_Not_Read`). `OTEL_TRACES_EXPORTER`,
> `OTEL_LOGS_EXPORTER` and `OTEL_METRICS_EXEMPLAR_FILTER` are process-environment
> rows too. `OTEL_SDK_DISABLED` keeps the `IConfiguration` source of the table
> above and gains the warning for a value other than `true`/`false`. The
> exemplar unit test named below exists and runs on the built provider.

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
