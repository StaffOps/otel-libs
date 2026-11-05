# .NET OTel helper (Phase C) — requirements

**Status**: Reviewed r3 (2026-11-04) — r1 reviewed in C1 (1 BLOCKING, 21 HIGH,
25 MEDIUM, 17 LOW), r2 re-reviewed in C1b (0 BLOCKING, 3 HIGH, 14 MEDIUM,
16 LOW across `dev`, `observability`, `spec-refuter`), r3 confirmed in C1c
(0 BLOCKING/HIGH/MEDIUM; its LOWs applied) — C1 closed 2026-11-04
(per-finding lists in [tasks.md](tasks.md) "r2 changes", "r3 changes",
"C1c changes").
| **Owner**: repo owner (rollout order ADR-004; "C0 last", 2026-10-28;
decisions O1–O11 of 2026-11-04, design "Owner decisions"; open owner action:
reserve the nuget.org prefix, O4) | **Relates to**:
[config-contract-v0.3](../config-contract-v0.3/requirements.md) (R1–R10 apply
unchanged), [ADR-001](../decisions/ADR-001-defer-otlp-env-to-sdk.md),
[ADR-004](../decisions/ADR-004-phased-rollout-go-python-first.md),
[ADR-006](../decisions/ADR-006-debug-span-attribute.md),
[ADR-009](../decisions/ADR-009-dotnet-retarget-net10-only.md),
[ADR-011](../decisions/ADR-011-owner-prefixed-package-names.md),
[ADR-013](../decisions/ADR-013-dotnet-helper-owns-env-resolution.md),
[nodejs-otel-helper](../nodejs-otel-helper/requirements.md) (the model for a
language catching up to the contract) · [design](design.md) · [tasks](tasks.md)

## Context

Go and Python implement the `0.3.0` contract (Phase A, released as
`0.3.0-rc.2`); Node.js implements it from day one (Phase B, `0.3.0-rc.1`
published 2026-11-04). .NET still ships the `0.2.x` behavior
(`dotnet/OtelHelper/OtelHelper.csproj:11` `<Version>0.2.0</Version>`), and the
gap is visible in the shared fixture: `parity/cases.json` holds **324 cases**
(counted 2026-11-04 at `c966d58`), every `dotnet` status is
`xfail:phase-c` (323) or `skip:phase-c` (1, `http.health_paths_override`).
No .NET parity runner exists, so none of those statuses has ever been
executed: `xfail:phase-c` records "not checked", not "known to fail".

What the .NET code does today, measured against every contract row, is the
gap inventory in [design.md](design.md#gap-inventory-reference); C1
reproduced its current-behavior claims in Docker. Headline findings:

- The helper emits **no diagnostics at all**: no warning, deprecation or INFO
  line exists in `dotnet/OtelHelper/**` (the only log call is the profiling
  prerequisite warning, `OtelHelper.Profiling/ProfilingPrerequisiteWarning.cs:23`).
  Invalid env values either throw `OptionsValidationException` **inside the
  `AddOtelHelper()` call** (its bootstrap `BuildServiceProvider()`,
  `TelemetryExtensions.cs:70-73`, validator `TelemetryOptionsValidator.cs:31-63`
  — measured, C1 dev P1) or are dropped silently
  (`TelemetryOptionsPostConfigure.cs:48-49, 76-78`). A malformed
  `OTEL_EXPORTER_OTLP_HEADERS` makes the SDK throw while the providers are
  built (measured on 1.15.3, C1 dev H4).
- Every exporter gets an endpoint the helper rebuilt as `scheme://host:port`
  (path dropped, `:4317` forced even for `https://` HTTP endpoints, userinfo
  and query dropped without a warning — `TelemetryOptionsPostConfigure.cs:137-164`,
  measured 2026-11-04), a fixed 10 s timeout (`TelemetryOptions.cs:58`,
  `TracerSetup.cs:65`) and no compression (the pinned exporter 1.15.3
  predates gzip support, added in 1.16.0).
- The resource is built on top of the SDK's env detector and then overwritten:
  `deployment.environment.name` is always set (default `LOCAL`) and a fresh
  `service.instance.id` replaces one given in `OTEL_RESOURCE_ATTRIBUTES`
  (`TelemetryExtensions.cs:76-87`; measured, C1 dev P8).
- `AddOtelHelperRedis()` (the overload that resolves the multiplexer from DI)
  throws `NotSupportedException` when the provider is built
  (`OtelHelper.Redis/OtelRedisExtensions.cs:17-21`; measured, C1 dev B1).
- Phase C backlog N1–N13 (`config-contract-v0.3/tasks.md`, "Phase C backlog —
  .NET") is still open; N6 became task C0, the last one.
- .NET 8 reaches end of support on 2026-11-10; the owner chose (O1,
  2026-11-04) a `0.2.1` patch adding `net10.0` (task C-patch) ahead of Phase C.

## Scope

Packages: core and the opt-in packages `AWS`, `Redis`, `Sql` (SqlClient,
Npgsql, MySqlConnector — O5), `Profiling`, renamed `StaffOps.OtelHelper*`
(ADR-011, N13); a parity runner and driver; unit tests (≥ 90% line gate, as
today); e2e smoke rows; examples (api → backend gRPC → process, custom
signals); docs incl. a backend-visible migration guide; CI and release wiring;
the `0.2.1` patch; a `dotnet/v0.3.0-rc.1` release; then the `net10.0`-only
retarget (C0) and `0.3.0`.

### Out of scope
- New instrumentations beyond the current set plus Npgsql and MySqlConnector
  (O5).
- Entity Framework Core instrumentation.
- .NET Framework / `netstandard2.0` targets.
- Native-profiler bootstrap (`CORECLR_*`, `LD_PRELOAD`): deployment-owned; the
  helper only detects and warns (DN9).
- Upstream SDK fixes (issues may be filed; gaps are recorded as
  `skip:no-upstream-*`, never deleted cases).

## Cross-language requirements

**R1–R10 apply as written**, including every dated D1 row of
`config-contract-v0.3/design.md` (edge-case rulings through 2026-11-03, tasks
A16–A28). A fixture case passes for .NET, or carries `xfail:C<n>` while the
task C<n> that flips it is open, or `skip:C<n>-<reason>` while the case needs
an API that task C<n> adds (parity/README: an unmappable `code` key is an
adapter error, which never satisfies an `xfail`), or a permanent
`skip:<reason>` recorded in design D-NET15; no case is deleted. .NET
differences that cannot be closed go to the root README "Known
emitted-telemetry gaps".

The Go-only requirements map to .NET as follows: G1/G2 (lifecycle) → DN4;
G3 (stdout when no logs exporter) → DN8; G7 (no `os.Setenv`) → DN2;
G8 (certificates) and G10 (timeout/compression/headers resolved by the helper)
→ DN2. G4–G6 and G9 have .NET counterparts in DN1, DN7, DN9 and DN3.

## .NET-only requirements (DN)

### DN0 — `0.2.1` patch: `net10.0` before .NET 8 end of support
(Owner decision O1, 2026-11-04; ADR-009 amendment of the same date; branch
plan in tasks.md C-patch.)
- The `0.2.x` libraries SHALL target `net8.0;net10.0` with **no behavior
  change on the same runtime**: same OpenTelemetry versions (1.15.x), same
  code, same defaults; on a given runtime the `net8.0` and `net10.0` assets
  SHALL emit the same telemetry. Evidence: the helper has no target-framework
  branches — no `#if`, `Environment.Version` or `RuntimeInformation` anywhere
  in `dotnet/` (C1b observability, C1c spec-refuter) — and no CI leg runs the
  `net8.0` asset on runtime 10 (roll-forward removed), so the claim rests on
  that source check, not on a run.
- `0.2.1` SHALL be built from a maintenance branch `release/dotnet-0.2` cut
  from tag `v0.2.0`, never from the 0.3 branch, so DN1 and the 0.3 CI stay
  unchanged; only its CHANGELOG entry (and the README ".NET 10 Note" text)
  are forward-ported.
- The release notes and the README ".NET 10 Note" SHALL list what changes when
  an app moves to the .NET 9+/10 **runtime** (true for any 0.2.x version,
  roll-forward included): runtime metrics from the built-in `System.Runtime`
  meter (`dotnet.*` instead of `process.runtime.dotnet.*`); HttpClient
  `url.full` query `?*` with userinfo removed; additional ASP.NET Core 10
  meters. A test SHALL assert the runtime-metric names by runtime: names
  starting `process.runtime.dotnet.` when `Environment.Version.Major < 9`,
  names starting `dotnet.` otherwise — so a leg that silently ran on another
  runtime fails (measured C1c: 16 `process.runtime.dotnet.*` names on runtime 8,
  18 `dotnet.*` names on runtime 10).
- The release notes SHALL state that the Profiling package on .NET 10 is not
  validated (native profiler not exercised) and give the workaround for the
  `AddOtelHelperRedis()` DI crash (pass the multiplexer explicitly); the fix
  itself lands in C14b.
- CI on the branch SHALL install the 10.0 SDK on every leg (SDK 8 cannot build
  the dual target, `NETSDK1045`), run each leg's own TFM with `-f`, print
  `dotnet --list-runtimes`, write coverage per TFM with the existing 90% gate,
  and not set `DOTNET_ROLL_FORWARD`.
- `dotnet/v0.2.1` SHALL be published by a `dotnet/v*` release job with the
  tag/`<Version>` check, backported from the 0.3 branch to the maintenance
  branch; the branch workflow's top-level permissions SHALL be
  `contents: read`, with `packages: write` on the publish job only.
- The `0.2.1` packages SHALL carry `LICENSE` and
  `<PackageLicenseExpression>Apache-2.0</PackageLicenseExpression>` (owner
  decision O11, 2026-11-04; the `v0.2.0` tree has none).

### DN1 — Runtime and dependency set
- Until C0 every **library and test** project SHALL target `net8.0` (plus
  `net10.0` on the `0.2.x` line, DN0); after C0 they SHALL target `net10.0`
  only and CI SHALL run `10.0.x` only (ADR-009). Examples may target a newer
  framework than the libraries (`example/dotnet-api/dotnet-api.csproj:3` is
  `net10.0` today, and its Dockerfiles use `sdk:10.0`).
- `OpenTelemetry*` core packages SHALL be on one generation (`1.20.x`, owner
  decision O6), contrib packages on the matching minor; every
  `PackageReference` is a minimum version (NuGet semantics), never a
  bracketed exact pin in a library project.
- WHEN a contrib instrumentation package exists only as a prerelease
  (`StackExchangeRedis`, `GrpcNetClient`) THEN the helper SHALL depend on the
  newest prerelease of the matching minor and the README SHALL say so.
- On the 0.3 line the test project SHALL reference `Microsoft.Extensions.*`
  at the major its target framework ships (8.x for `net8.0`, 10.x for
  `net10.0`), so restore never reports `NU1605`. (The `0.2.1` test project
  keeps today's 9.0.0 on `net8.0` and adds 10.0.0 on `net10.0` — DN0.)

### DN2 — Helper-owned resolution, explicit values (ADR-013)
- The helper SHALL resolve every D1 row it owns from **process environment
  variables** (and code), never from `IConfiguration`, and SHALL construct
  its OTLP exporters itself, assigning every exporter option explicitly
  (endpoint per signal, protocol, timeout, compression, `Headers` empty) and
  supplying its own `HttpClientFactory` (headers, TLS, no redirects, response
  size and timeout as the SDK default client) — the "neutralizing" table of
  ADR-013.
- WHEN `OTEL_EXPORTER_OTLP_<SIGNAL>_*` is set THEN that signal SHALL use it,
  although upstream `AddOtlpExporter` ignores per-signal variables.
- The helper SHALL NOT call `Environment.SetEnvironmentVariable`.
- WHEN an `OTEL_*` key exists only in `IConfiguration` (for example
  `appsettings.json`) for a helper-owned row THEN it SHALL have no effect on
  that row, and the HOW-TO SHALL say so (behavior change from 0.2.x).
- Upstream switches that are not D1 rows (for example the ASP.NET Core gRPC
  flag, O7) SHALL be read from wherever the SDK or instrumentation reads them
  (the app `IConfiguration` for that flag); "never from `IConfiguration`"
  applies to helper-owned D1 rows only.
- The rows that stay with the SDK SHALL be listed in the HOW-TO with the
  source the SDK reads (`IConfiguration`): `OTEL_SDK_DISABLED` (the helper
  reads the same key from `IConfiguration` so its INFO line and resolution
  agree with the SDK), attribute limits, retry.
- `OTEL_EXPORTER_OTLP_CERTIFICATE`, `_CLIENT_CERTIFICATE` and `_CLIENT_KEY`
  (generic and per signal) SHALL be read and validated by the helper and
  applied through its `HttpClientFactory` handler; WHERE the wire check of C8
  shows the handler does not apply to gRPC, the affected cases SHALL get a
  D-NET15 skip, not a silent pass.
- A malformed or duplicate header item in env SHALL never fail provider
  construction (today it does).

### DN3 — Diagnostics are loud, never leak
- Helper configuration diagnostics (warnings, deprecations, INFO lines) SHALL
  go through **one** channel: one line on standard error prefixed
  `otelhelper: ` (design D-NET5), written by one DI-registered sink with an
  injectable writer (static fallback before the host exists), which the
  parity driver hooks.
- The SDK's own warnings (`OpenTelemetry-*` `EventSource`s at Warning and
  above) SHALL be forwarded by an in-process `EventListener` to the same sink
  with origin `sdk` (not part of `helper_log`), redacted; a code option turns
  the forwarding off (principle 8: "failures must be loud").
- WHEN an env var holds an unknown or unparseable value THEN the helper SHALL
  warn naming the variable and the value used, and SHALL NOT fail the app
  (principle 8).
- WHEN the same invalid value is set in code THEN host start SHALL fail
  (`ValidateOnStart`) with an `OptionsValidationException` naming the option.
- No diagnostic SHALL contain the value of a `*_HEADERS` or `*_CLIENT_KEY`
  variable, URL userinfo or a query string (R8) — including the validator's
  message for an invalid code endpoint (today it echoes the raw value,
  `TelemetryOptionsValidator.cs:19`); echoed values are truncated (D1 row
  "Echoed values are truncated") and CR, LF, other C0/C1 controls, DEL,
  U+2028 and U+2029 are escaped so a value cannot forge a line.
- WHEN `OTEL_HELPER_EXTRA_INSTRUMENTATION` is set THEN one warning SHALL say it
  has no effect in .NET (`TelemetryOptions.cs:188` has no production caller;
  only tests call it).

### DN4 — Options API, lifecycle and migration
- **Set in code wins, whatever the value** (D1 row "Code option equal to the
  helper default"): every option that has an env counterpart SHALL be nullable,
  `null` meaning "not set in code"; no sentinel comparison with a default
  value (today `"my-service"`, `LOCAL`, `"SQL"`, `AlwaysOnSampler`).
- A value bound from `IConfiguration` into `TelemetryOptions`
  (`services.Configure<TelemetryOptions>(config.GetSection(…))`) counts as
  code and beats `OTEL_*` env; this SHALL be tested and stated in the
  migration guide (the usual .NET order is env over `appsettings.json`).
- WHEN `services.Configure<TelemetryOptions>(…)` is registered after
  `AddOtelHelper()` THEN the providers SHALL use it (N4), including whether
  the Prometheus listener binds (design D-NET3: the reader is always
  registered; "no listener" clears its prefixes).
- WHEN `AddOtelHelper()` is called twice THEN the second call SHALL change
  nothing (today's guard, `TelemetryExtensions.cs:31-33`) and SHALL warn once.
- WHEN the host stops THEN every provider SHALL flush and shut down within the
  export deadline; WHEN options validation fails THEN no listener SHALL stay
  open and no exporter thread SHALL be left running (G1 analog).
- The migration guide in `CHANGELOG.md` SHALL have two parts: **API changes**
  (each with the replacement) and **telemetry changes** — a table of
  attribute/metric/span, value before, value after, and the dashboards or
  alerts affected (design "Migration guide inputs"), with a separate section
  for what changes again at C0.

### DN5 — Dependency-injection hygiene
- The helper SHALL NOT register `ActivitySource` or `Meter` as unkeyed DI
  singletons (N1): ASP.NET Core resolves `ActivitySource` from DI after a
  `TryAddSingleton` (`GenericWebHostBuilder.cs:76`, release/8.0 and 10.0), so a
  later registration replaces the `Microsoft.AspNetCore` source (reproduced,
  C1 dev P5: server activity `svc-a|Microsoft.AspNetCore.Hosting.HttpRequestIn`).
- The replacement SHALL be keyed services (`[FromKeyedServices(OtelHelperKeys.Instrumentation)]`
  for `ActivitySource` and `Meter`, .NET 8 keyed DI) documented in the
  migration guide; a test SHALL show what unkeyed injection resolves to
  after 0.3 — measured C1b: in web hosts it **silently** resolves to the
  `Microsoft.AspNetCore` source (custom spans exported under that scope), in
  workers it is null; unkeyed `Meter` is null — and the migration guide SHALL
  list this first in its API part. (A helper warning when the keyed instance
  is never resolved was considered and not adopted: there is no reliable
  moment to decide "never".)
- WHEN an app creates a `Meter` named `<ServiceName>` or `<ServiceName>.*`, or
  one listed in a new `AdditionalMeters` option, THEN its instruments SHALL be
  exported (N5; today only `ServiceName`, `MetricsSetup.cs:20`).

### DN6 — Debug marker and root activities
- R9 via ADR-006: the helper SHALL set the span attribute `debug=true` on
  spans whose parent is absent or remote — tested as
  `ParentSpanId == default || HasRemoteParent`, because `Activity.Parent` is
  also null for in-process children started from an explicit context (the
  Redis instrumentation does this) — and SHALL NOT touch `tracestate`
  (N2: `DebugTraceStateProcessor.cs:17-21`; measured `debug=true,debug=true`,
  C1 dev P6).
- WHEN `StartRootActivity` returns THEN `Activity.Current` SHALL be the new
  root (or the previous value when no activity was created), and WHEN the
  root is disposed THEN the previous `Activity.Current` SHALL be restored (N3;
  measured null after dispose, C1 dev P7).

### DN7 — Instrumentation, health and HTTP/gRPC parity
- R7 and D5 semantics SHALL hold for ASP.NET Core HTTP and gRPC server
  requests: exact, case-insensitive, percent-decoded match, trailing slash and
  query ignored; default list `/health /healthz /ready /readyz /live /livez
  /ping /metrics`; overridable in code (`HealthPaths`); children of a dropped
  request dropped; the request still counted in `http.server.request.duration`.
- WHEN a gRPC call targets `grpc.health.v1.Health/Check` or `/Watch` THEN no
  span SHALL be recorded; every other gRPC server call SHALL produce one SERVER
  span with RPC conventions and the A22 status rule (upstream since
  AspNetCore 1.15.1) **when the app sets**
  `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION=true` in its
  configuration (owner decision O7 (b), 2026-11-04). WHEN gRPC endpoints are
  mapped and the key is not true THEN the helper SHALL warn once (design
  D-NET8); the README SHALL say that without the key gRPC server spans are
  HTTP-shaped (`POST /pkg.Svc/Method`, status from HTTP 200).
- Each gRPC client call SHALL produce **one** CLIENT span (as Go), not a
  GrpcNetClient span plus an HttpClient span (today both are added,
  `TracerSetup.cs:26-32`).
- HTTP spans and metrics SHALL use the stable HTTP semantic conventions
  (R10) — the .NET instrumentations emit only those.
- `grpc.health_*_counted_in_metrics` SHALL be `skip:no-upstream-rpc-metrics`
  (no RPC metric in the .NET 1.20.x packages, C1 dev string check); the README
  SHALL say gRPC calls are counted in `http.server.request.duration` with HTTP
  status 200 even when the RPC fails.

### DN8 — Logging
- D6 applies: OTLP only when a logs exporter is active; with no endpoint,
  `OTEL_LOGS_EXPORTER=none` or `console`, records SHALL reach stdout — the
  helper's JSON-line fallback (one object per line, UTC `Z` time, reserved
  keys dropped, A23; `level` one of the four words Go, Python and Node use:
  `Trace`/`Debug` → `DEBUG`, `Information` → `INFO`, `Warning` → `WARN`,
  `Error`/`Critical` → `ERROR`; OTLP keeps the SDK severity text) for no exporter
  / `none`, the SDK console exporter for `console`.
- R4 levels: `LOCAL` → Debug, unset/unknown → Information, `PRD` → Warning,
  `DEV`/`HML` → Information; code `MinimumLogLevel` wins.
- WHEN the level is above Debug THEN the helper SHALL NOT raise the minimum
  level of the `Microsoft.*` and `System.Net.Http` categories on providers it
  does not own (N9: `LoggingSetup.cs:51-55` hides framework warnings on every
  provider).

### DN9 — Opt-in packages
- One helper-owned sanitizing processor, registered by core **before** every
  export processor, SHALL apply the A25–A28 rules; opt-in packages add rules to
  it, never their own processors after the exporter.
- `Sql` SHALL instrument SqlClient, Npgsql and MySqlConnector (O5,
  2026-11-04), with tests against the compose `sql-postgres` and `sql-mysql`
  services; failed DB spans keep ERROR without a description (A25; SqlClient
  does this upstream since 1.20.0).
- `AWS` SHALL keep ERROR without a description, set `error.type` to the AWS
  error code or `_OTHER` (upstream sets neither: `SetStatus(Error,
  exception.Message)`, no `error.type`), and SHALL NOT record
  `aws.s3.key`, `aws.s3.copy_source`, `aws.s3.delete`,
  `aws.dynamodb.item_collection_metrics`; an SNS phone-number destination is
  `phone_number:**` (A27); tests assert each absence.
- `Redis`: failed commands are **not** marked ERROR upstream (the span is
  built after the fact from the profiler and never gets a status); this is a
  recorded gap, not a requirement. R10 database dup is a documented gap
  (owner decision O8 (c), 2026-11-04): Redis spans carry the legacy keys
  unless the deployment sets `OTEL_SEMCONV_STABILITY_OPT_IN=database/dup`
  (the instrumentation reads the process env only); the HOW-TO SHALL say so
  and the root README SHALL have the gap row.
- URL query redaction (O3, 2026-11-04): upstream redaction stays; WHEN an
  app disables it (`OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_DISABLE_URL_QUERY_REDACTION`,
  `OTEL_DOTNET_EXPERIMENTAL_HTTPCLIENT_DISABLE_URL_QUERY_REDACTION`,
  `DOTNET_SYSTEM_NET_HTTP_DISABLEURIREDACTION` or the
  `System.Net.Http.DisableUriRedaction` switch) THEN the sanitizing processor
  SHALL still write the values of the A28 names as `REDACTED`.
- `Profiling` SHALL start the profiler when the host starts, not at
  registration, and its prerequisite check SHALL include `CORECLR_PROFILER`
  and `CORECLR_PROFILER_PATH` (or `LD_PRELOAD` where the provider needs it)
  (N11).
- Each opt-in package SHALL have tests that exercise a real span, not only
  registration (today `SubpackageExtensionsTests.cs`, 4 tests).

### DN10 — Packaging and names
- Package IDs SHALL be `StaffOps.OtelHelper`, `StaffOps.OtelHelper.{AWS,Redis,Sql,Profiling}`,
  root namespace `StaffOps.OtelHelper` (ADR-011, N13); environment variable
  names do not change; no `OtelHelper` 0.3 compatibility package (O2,
  2026-11-04).
- The repository SHALL have a `dotnet/Directory.Build.props` (shared package
  metadata, SourceLink, deterministic build, `TreatWarningsAsErrors` without a
  blanket `CS1591` exemption); tests and examples that use the `[Obsolete]`
  member suppress CS0618 locally (N12).
- WHEN a `dotnet/v*` tag is pushed THEN CI SHALL pack and publish exactly the
  five `StaffOps.*` packages at the tag version (existing tag/`<Version>`
  check, `ci.yml:656-668`).
- The `0.2.x` packages (`OtelHelper*`) SHALL stay installable.

### DN11 — Parity runner and release gate
- The .NET test suite SHALL run every case of `parity/cases.json`, each in its
  own process, with the runner rules of `parity/README.md` (env reset,
  placeholders, matchers incl. string-only text matchers, strict xfail,
  `PARITY_REQUIRE_FAKEOTLP`), against the shared `fakeotlp` receiver; the
  fixture path is found from a repository-root marker, not a relative path
  from the build output.
- The first runner task SHALL record what the code does: a case that passes
  becomes `pass`; one that fails becomes `xfail:C<n>`; one whose `code` keys
  have no 0.2 API, or a value the 0.2 type cannot hold, becomes
  `skip:C5-no-api` (13 cases, design D-NET12); an
  upstream gap becomes `skip:<reason>` from D-NET15.
- The suite SHALL fail on a `skip:C<n>-<reason>` that names no open task in
  `specs/dotnet-otel-helper/tasks.md` (the Node rule; `tools/spec_lint.py`
  checks `xfail:` only, design D-NET12).
- WHEN `PARITY_RELEASE=1` THEN the suite SHALL fail while any `dotnet` status is
  `xfail:*`, `skip:phase-c` or `skip:C<n>-…`; CI SHALL set it on `dotnet/v*`
  tags.
- Child-process coverage SHALL count toward the 90% gate (measured feasible
  with coverlet.msbuild 6.0.2 when the child exits normally, C1 dev).

### DN12 — Runtime retarget (C0, last)
- ADR-009: `net10.0` only for libraries, tests and examples; CI `10.0.x` only;
  `DOTNET_ROLL_FORWARD` removed; README "Supported Targets" updated; breaking
  for `net8.0` consumers, who stay on `0.2.x`.
- The runtime metrics, HttpClient span and query-redaction differences
  follow the **.NET 9+ runtime**, not the target framework, so C0 SHALL
  record them with a before/after diff of the same app on the **.NET 8
  runtime** (last `net8.0` build) and on the **.NET 10 runtime** (C0 build),
  re-verify the HttpClient health filter on the .NET 10 runtime, and put the
  result in the migration guide's C0 section, addressed to consumers still
  on .NET 8.
- C0 SHALL run after C19 (owner order 2026-10-28, confirmed with O1 on
  2026-11-04).

## Acceptance
- [ ] `0.2.1` published with `net8.0;net10.0` (C-patch).
- [ ] Every fixture case `pass` for .NET or `skip:<reason>` from D-NET15; no
      `xfail:*`, `skip:phase-c` or `skip:C<n>-…` left (`PARITY_RELEASE=1` green).
- [ ] Line coverage ≥ 90% per the existing CI gate, parity children included.
- [ ] e2e smoke rows for .NET green against a real Collector (grpc and
      http/protobuf, path prefix, per-signal endpoint, custom CA), and a
      Prometheus scrape from another container.
- [ ] Examples and docs complete: `dotnet/README.md`, `HOW-TO.md`, `TESTS.md`,
      `example/EXAMPLES.md` (custom span, metric, log with the Go/Python/Node
      names), root README env table without a separate .NET column,
      `AGENTS.md`, `CONSUMING.md`, MkDocs, both parts of the migration guide.
- [ ] `observability` + `dev` refutation and `code-review` per task and once on
      the whole package (C19), zero open BLOCKING/HIGH; `CHANGELOG.md` entries.
- [ ] `dotnet/v0.3.0-rc.1` published by CI; then C0 and the `net10.0` release.
