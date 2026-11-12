# ADR-015: .NET continuous profiles push directly to Pyroscope (second exception to "everything via Collector")

**Status**: Accepted | **Date**: 2026-11-12 | **Deciders**: repo owner (decision during the .NET C15 review)

## Context

AGENTS.md principle 2 says the SDK exports OTLP to the Collector and never
directly to a backend, with one recorded exception: the Prometheus `/metrics`
fallback ([ADR-007](ADR-007-keep-prometheus-in-core.md)).

The opt-in package `StaffOps.OtelHelper.Profiling` starts the Pyroscope .NET
native profiler (CLR profiler loaded through `CORECLR_*`). That profiler does
not produce OTLP: it pushes pprof data to `PYROSCOPE_SERVER_ADDRESS` itself.
The helper only checks the prerequisites, starts it with the host, and tags
root spans with `pyroscope.profile.id` for trace-to-profiles links. This was
already the 0.2.x behavior; the C15 review found it recorded only in the
README and HOW-TO, not as an exception to the principle.

## Decision

1. Continuous profiles from the .NET Profiling package go **directly to
   Pyroscope** (`PYROSCOPE_SERVER_ADDRESS`), not through the Collector. This is
   the second, explicit exception to principle 2, recorded in AGENTS.md.
2. The exception is limited to profiles from that opt-in package. Traces,
   metrics and logs keep going through the Collector.
3. The docs state it where the package is configured, with the egress
   consequence (the app needs network access to Pyroscope and its auth token),
   and mention that a profile-proxying agent (e.g. Grafana Alloy
   `pyroscope.receive_http`) can sit in the path where egress rules require a
   single exit. That option is not verified by this repo.

## Rationale

1. No OTLP path exists today: the native profiler speaks the Pyroscope push
   API only, and the OpenTelemetry Profiles signal is not yet supported end to
   end by the profiler and the Collector distribution in use.
2. Keeping the package (rather than dropping .NET profiling) preserves the
   trace-to-profiles workflow that 0.2.x users already have.
3. Recording the exception keeps principle 2 honest: a reader of AGENTS.md
   sees every path that bypasses the Collector.

## Consequences

### Positive
- The principle and the code agree; the bypass is visible and bounded.
- No change for 0.2.x users of the package.

### Negative
- Profiles skip the Collector's processing (attribute enrichment, filtering,
  routing, retry). The app needs direct egress to Pyroscope and holds the
  Pyroscope credentials itself.
- Collector-level controls (tail sampling, redaction processors) do not apply
  to profiles.

### Neutral
- Go, Python and Node.js ship no profiling package today; parity is not
  affected.

## When this decision would be wrong

- The profiler (or a supported .NET alternative) can export the OpenTelemetry
  Profiles signal over OTLP, and the Collector distribution in use accepts and
  forwards it: then route profiles through the Collector and retire this
  exception.
- A deployment forbids direct egress from apps to backends: then the proxying
  agent path becomes mandatory and should be documented as the default.
