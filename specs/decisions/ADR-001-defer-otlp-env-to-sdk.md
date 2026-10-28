# ADR-001: Defer OTLP exporter env handling to the OTel SDK

**Status**: Accepted | **Date**: 2026-10-27 | **Deciders**: repo owner, validated by `observability` + `dev` agents

> **Corrected 2026-10-27 by adversarial review.** The first version deferred
> *every* env value to the SDK and let a test adapter re-derive URLs. That
> broke schemeless endpoints, sent portless gRPC to 443, and contradicted
> ADR-008. Exceptions (d) and wire-level assertions were added.
>
> **Corrected 2026-10-28 after A3 validation.** In practice the helper owns far
> more than four shims: SDK behavior diverged from the spec or between Go and
> Python on each of these inputs, so the helper resolves them itself — each is
> covered by parity fixture cases (design.md "D1 edge-case rulings"):
> schemeless endpoints; portless gRPC; INSECURE vs scheme conflicts; TLS-file
> drop on plaintext endpoints; userinfo/query stripping; invalid or rejected
> per-signal endpoint fallback; unsupported schemes; header parsing; timeout
> parsing (ms, range); compression parsing. The decision still holds for
> everything else (absolute, well-formed URLs, certificates, protocol defaults):
> those stay with the SDK. Rule going forward: **adding a helper-owned input
> requires amending this ADR and adding fixture cases in the same change.**
>
> **Second correction, 2026-10-28 (A3 round 2).** In Go, leaving an env
> endpoint to the SDK proved unsafe: the SDK re-reads the raw variable and three
> independent HIGH defects (rejected per-signal fallback ignored, per-signal
> schemeless + INSECURE regression, trailing newline losing all logs) came from
> the helper and SDK disagreeing about the same value. The Go helper now passes
> the resolved URL explicitly for every configured signal; the SDK still owns
> certificates, protocol defaults and everything not listed above. Python
> already takes ownership whenever a shim fires and is unaffected.

## Context

All three libs parse `OTEL_EXPORTER_OTLP_ENDPOINT` themselves, rebuild it as
`scheme://host:port`, and pass the result to the exporter as an explicit
option. Explicit options override the SDK's own env handling, which already
implements the OTLP exporter spec correctly. Reproduced consequences
(2026-10-27, scratch tests, `main` @ `3f1dc35`):

- `https://otlp.vendor.io` + `http/protobuf` → exporter posts to
  `https://otlp.vendor.io:4317/v1/traces` (Go `config.go:87-88`, Python
  `config.py:74`, .NET `TelemetryOptionsPostConfigure.cs:140-145`).
- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` alone → no trace exporter created, no
  error (Go and Python: `PROCS []`; Go receiver `B hits=[]`).
- Go: generic + per-signal set → spans hit host A with path `/custom/traces`.
- Go: `OTEL_EXPORTER_OTLP_CERTIFICATE` ignored →
  `tls: failed to verify certificate: x509: certificate signed by unknown authority`
  because the lib passes `WithTLSClientConfig(&tls.Config{})`.
- Python/.NET: path prefix dropped on the wire (`/otlp/v1/traces` → `/v1/traces`).
- IPv6: `[::1]:4317` → `::1:4317` (`too many colons`).

## Decision

> *Rewritten 2026-10-28 (A3 round 3) to match the two corrections above; the
> original text deferred every env value to the SDK and owned four shims.*

The SDK owns what it implements per spec **and identically across our
languages**: TLS certificates and client keys, protocol-specific defaults,
retry, and everything not listed below.

The helper owns, resolves and passes **explicitly** (each input covered by
parity fixture cases, design.md "D1 edge-case rulings"):

- **Endpoint** for every configured signal (Go: always via `WithEndpointURL`;
  Python: whenever a shim fires) — trimmed; schemeless rewrite; portless gRPC →
  4317; per-signal → generic fallback; unsupported schemes; userinfo/query
  stripped; `/v1/<signal>` appended to generic URLs for HTTP; diagnostics show
  `<unparseable URL>` instead of raw values.
- **TLS on plaintext endpoints**: INSECURE vs scheme conflicts (scheme wins),
  TLS-file drop for `http://`.
- **Headers** (parsing rules), **timeout** (milliseconds, range), **compression**
  (gzip default, gzip/none).
- **"OTLP configured"** per signal.
- **Sampler** (`OTEL_TRACES_SAMPLER`/`_ARG`, added 2026-10-28 in A4): the six spec
  names parsed by the helper with public SDK classes; unknown or invalid values
  warn and fall back. Reasons: Python SDK 1.42 raises `ValueError` on an
  out-of-range `_ARG`; the SDK's warning doesn't name the variable; the health
  sampler (D5) needs a sampler object to wrap.

Rule: adding a helper-owned input requires amending this ADR and adding parity
fixture cases in the same change.

## Rationale

1. Every reproduced bug above disappears with one change instead of being
   patched three times in three parsers.
2. SDKs are tested against the spec by upstream; our parsers are not.
3. It unlocks spec features we never implemented (per-signal protocol,
   certificates, mTLS) — headers per signal ended up helper-owned and
   parity-tested (see the 2026-10-28 correction).

## Consequences

### Positive
- Spec-conformant endpoint, port, path, TLS behavior in every language.
### Negative
- Behavior change: `https://host` without port under `http/protobuf` now goes
  to 443 (was 4317); path prefixes are now honored. Documented as breaking in 0.3.0.
- URL/TLS outcomes are asserted on the wire by `export` fixture cases against
  a fake receiver — adapters never re-derive them.
- Go and Python SDKs disagree when `INSECURE` conflicts with the scheme; the
  helper normalizes that signal (scheme wins) and warns (design.md D1.c).
### Neutral
- Port-4318 → `http/protobuf` inference is kept (helper still picks the
  exporter package).

## When this decision would be wrong

- An SDK version is found that does not honor per-signal endpoints or
  certificates per spec (check before each major bump).
- A consumer needs a transformation the spec doesn't allow (none known).

**Reversal action**: wrap the specific SDK gap with a minimal shim covered by
a `parity/cases.json` case, not a full parser.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Fix our parsers (keep path, scheme-default port, per-signal support) | Re-implements the spec three times; still misses certificates/headers per signal |
| Use contrib `autoexport` (Go) / SDK auto-configuration everywhere | Loses the helper's protocol inference and metrics contract; Python/Go APIs differ too much for parity |

## References
- OTLP exporter configuration: <https://opentelemetry.io/docs/specs/otel/protocol/exporter/>
- `specs/config-contract-v0.3/requirements.md` F1–F3 (R1, R2, G8)
