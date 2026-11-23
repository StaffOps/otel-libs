# Profiles

A continuous profile shows where a process spends CPU, allocates memory,
waits on locks or throws exceptions, function by function. Use it when a
trace shows that a span is slow but not why: no child span explains the
time, so the cost is inside the code.

**Audience:** engineers investigating CPU, memory or lock contention.
**Backend:** Grafana Pyroscope.

## Support by language

| Language | Profiles |
|----------|----------|
| .NET | Opt-in profiling package, which drives Pyroscope's native CLR profiler |
| Go, Python, Node.js | None. The helpers have no profiling support |

!!! info "Not on the demo"
    The k8s01 demo has no Pyroscope data source, so this page has no
    screenshots. It describes the .NET package as documented and measured in
    its README.

## .NET: how it works

- Register the package: `services.AddOtelHelperProfiling()`.
- The runtime loads the native profiler from environment variables it reads
  when the process starts (`CORECLR_ENABLE_PROFILING=1`, `CORECLR_PROFILER`,
  `CORECLR_PROFILER_PATH`, on Linux `LD_PRELOAD`, then
  `PYROSCOPE_PROFILING_ENABLED=1` and `PYROSCOPE_SERVER_ADDRESS`). The
  helper cannot set them, so it checks them when the host starts.
- When they are all set, the helper turns on CPU, allocation, contention and
  exception profiling. It also adds a span processor that writes
  `pyroscope.profile.id` (the span id) on root spans: the link from a trace to
  its profile.
- When one is missing, the helper writes one warning on standard error,
  naming the variables and never their values. Traces, metrics and logs are
  unaffected.
- Profiles go from the native profiler **straight to Pyroscope**, not
  through the Collector. This is the second exception to "everything through
  the Collector"
  ([ADR-015](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-015-dotnet-profiles-push-to-pyroscope.md)):
  there is no OTLP path for these profiles yet.
- Measured on `linux/amd64` only. Arm64, musl and the .NET 10 runtime are
  not verified.

The full setup (container variables, image, checks, status) is in the
[.NET README](../../dotnet/README.md#profiling-pyroscope) and the
[.NET HOW-TO](../../dotnet/HOW-TO.md#profiling-pyroscope).

## Using profiles in diagnosis

1. Find the slow root span in Tempo ([Latency and saturation](../diagnosis/latency-saturation.md)).
2. If no child span explains the time, open the profile of that service for
   the same time range. In Grafana, the Tempo data source can link a span to
   Pyroscope through `pyroscope.profile.id` when the traces-to-profiles
   setting is configured.
3. Read the flame graph: a wide frame is where the time went.

## Why profiles are useful

- **Below the span**: they explain time that no span accounts for.
- **Always on**: sampling profilers cost little, so the profile of an
  incident is already there.
