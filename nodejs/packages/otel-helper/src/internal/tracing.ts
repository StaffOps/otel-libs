/**
 * Sampling, propagation and the debug marker (task B5; contract R6, R7, R8,
 * R9; contract design D1 rows "Sampler" and "Propagators", D3, D5, ADR-002,
 * ADR-006; Node design D-JS3, D-JS7). Same rules as the Go helper
 * (`go/tracing.go` buildSampler, `go/health.go`, `go/config.go`
 * parsePropagators, `go/processors.go`) and the Python helper
 * (`tracing.py`, `_propagators.py`, `processors.py`).
 *
 * The helper parses `OTEL_TRACES_SAMPLER` / `_ARG`, the deprecated
 * `OTEL_HELPER_SAMPLE_RATIO` and `OTEL_PROPAGATORS` itself (ADR-001 owned
 * inputs): the sampler must be an object the helper holds, so the health
 * sampler can wrap it. Env values warn and fall back; code values fail fast
 * (R8). Sampler and propagator names are never secrets, so they are echoed
 * (truncated).
 */
import { SpanKind, SpanStatusCode, isSpanContextValid, trace, type Attributes, type Context, type Link, type TextMapPropagator } from '@opentelemetry/api';
import { CompositePropagator, W3CBaggagePropagator, W3CTraceContextPropagator } from '@opentelemetry/core';
import {
  AlwaysOffSampler,
  AlwaysOnSampler,
  ParentBasedSampler,
  SamplingDecision,
  TraceIdRatioBasedSampler,
  type Sampler,
  type SamplingResult,
  type Span,
  type SpanProcessor,
} from '@opentelemetry/sdk-trace';
import { truncateValue } from './diagnostics';
import { asciiLower, envValue, PLAIN_DECIMAL, trimWS, typeName, type Warn } from './env';

export const ENV_TRACES_SAMPLER = 'OTEL_TRACES_SAMPLER';
export const ENV_TRACES_SAMPLER_ARG = 'OTEL_TRACES_SAMPLER_ARG';
export const ENV_SAMPLE_RATIO = 'OTEL_HELPER_SAMPLE_RATIO';
export const ENV_PROPAGATORS = 'OTEL_PROPAGATORS';

/** The six sampler names of the OTel SDK environment spec the helper builds. */
export type SamplerName =
  | 'always_on'
  | 'always_off'
  | 'traceidratio'
  | 'parentbased_always_on'
  | 'parentbased_always_off'
  | 'parentbased_traceidratio';

/** The resolved sampler: its name and, for the ratio samplers, the ratio. */
export interface SamplerConfig {
  readonly name: SamplerName;
  /** In [0, 1] for `traceidratio` / `parentbased_traceidratio`; `undefined` otherwise. */
  readonly ratio: number | undefined;
}

/** Propagators the helper links (contract D1 row "Propagators"). */
export type PropagatorName = 'tracecontext' | 'baggage';

export const DEFAULT_SAMPLER: SamplerConfig = Object.freeze({ name: 'parentbased_always_on', ratio: undefined });
const SAMPLER_NAMES: readonly SamplerName[] = [
  'always_on',
  'always_off',
  'traceidratio',
  'parentbased_always_on',
  'parentbased_always_off',
  'parentbased_traceidratio',
];
// Listed by the spec but needing contrib samplers the helper doesn't ship.
const UNSUPPORTED_SAMPLERS: ReadonlySet<string> = new Set(['jaeger_remote', 'parentbased_jaeger_remote', 'xray']);
// Spec propagators the helper does not link (warned as "not supported", not "unknown").
const UNSUPPORTED_PROPAGATORS: ReadonlySet<string> = new Set(['b3', 'b3multi', 'jaeger', 'xray', 'ottrace']);
const DEFAULT_PROPAGATORS: readonly PropagatorName[] = Object.freeze(['tracecontext', 'baggage']);

/** HTTP paths whose server spans are dropped unless `healthPaths` replaces the list (R7, contract D5). */
export const DEFAULT_HEALTH_PATHS: readonly string[] = Object.freeze([
  '/health',
  '/healthz',
  '/ready',
  '/readyz',
  '/live',
  '/livez',
  '/ping',
  '/metrics',
]);

/** A plain decimal in [0, 1] (contract D3); `undefined` otherwise. */
export function parseRatio(raw: string): number | undefined {
  if (!PLAIN_DECIMAL.test(raw)) return undefined;
  const ratio = Number(raw);
  return Number.isFinite(ratio) && ratio <= 1 ? ratio : undefined;
}

/** Code options this module reads (type-checked by {@link checkCodeTracing}). */
export interface TracingCodeOptions {
  readonly sampleRatio?: number | undefined;
  readonly healthPaths?: readonly string[] | undefined;
}

/**
 * Validates the code options of this module (R8: fail fast, naming the
 * option) and returns the normalized values. `sampleRatio`: a number in
 * [0, 1] (NaN rejected). `healthPaths`: an array of ASCII paths starting with
 * `/`, without query, fragment or comma (the Go and Python rule); `[]` traces
 * every path.
 */
export function checkCodeTracing(code: Readonly<Record<string, unknown>>): Partial<Record<keyof TracingCodeOptions, unknown>> {
  const out: Partial<Record<keyof TracingCodeOptions, unknown>> = {};
  const ratio = code.sampleRatio;
  if (ratio !== undefined) {
    if (typeof ratio !== 'number') throw new TypeError(`setup(): sampleRatio must be a number in [0, 1], got ${typeName(ratio)}`);
    if (Number.isNaN(ratio) || ratio < 0 || ratio > 1) throw new TypeError(`setup(): sampleRatio must be a number in [0, 1], got ${String(ratio)}`);
    out.sampleRatio = ratio;
  }
  if (code.healthPaths !== undefined) {
    const paths = code.healthPaths;
    if (!Array.isArray(paths)) throw new TypeError(`setup(): healthPaths must be an array of strings, got ${typeName(paths)}`);
    const list = [...(paths as unknown[])];
    list.forEach((p, i) => {
      if (typeof p !== 'string') throw new TypeError(`setup(): healthPaths item #${String(i + 1)} must be a string, got ${typeName(p)}`);
      // eslint-disable-next-line no-control-regex
      if (!p.startsWith('/') || /[?#,]/.test(p) || !/^[\x00-\x7f]*$/.test(p)) {
        throw new TypeError(
          `setup(): healthPaths item #${String(i + 1)} (${JSON.stringify(p)}) must be ASCII, start with "/" and have no query, fragment or comma`,
        );
      }
    });
    out.healthPaths = Object.freeze(list);
  }
  return out;
}

/**
 * The sampler (R6, contract D3, ADR-002). Precedence: a code `sampleRatio`
 * (any value, 1.0 included — D1 A13 ruling) → `parentbased_traceidratio`;
 * else `OTEL_TRACES_SAMPLER` / `_ARG`; else, while `OTEL_TRACES_SAMPLER` is
 * unset, the deprecated `OTEL_HELPER_SAMPLE_RATIO` → `parentbased_traceidratio`;
 * else `parentbased_always_on`. Names are trimmed and case-insensitive; an
 * unknown or unsupported name warns and uses the default; an invalid `_ARG`
 * warns and uses 1.0; an invalid alias warns and is ignored.
 */
export function resolveSampler(code: TracingCodeOptions, warn: Warn): SamplerConfig {
  if (code.sampleRatio !== undefined) return Object.freeze({ name: 'parentbased_traceidratio', ratio: code.sampleRatio });
  const raw = envValue(ENV_TRACES_SAMPLER);
  if (raw === undefined) {
    const alias = envValue(ENV_SAMPLE_RATIO);
    if (alias === undefined) return DEFAULT_SAMPLER;
    const ratio = parseRatio(alias);
    if (ratio === undefined) {
      warn('sample ratio is not a number in [0, 1]', { variable: ENV_SAMPLE_RATIO, value: truncateValue(alias), using: 'parentbased_always_on' });
      return DEFAULT_SAMPLER;
    }
    return Object.freeze({ name: 'parentbased_traceidratio', ratio });
  }
  const name = asciiLower(raw);
  if (UNSUPPORTED_SAMPLERS.has(name)) {
    warn('sampler is unsupported by this helper', { variable: ENV_TRACES_SAMPLER, value: truncateValue(raw), using: 'parentbased_always_on' });
    return DEFAULT_SAMPLER;
  }
  const known = SAMPLER_NAMES.find((n) => n === name);
  if (known === undefined) {
    warn(`unknown sampler (valid: ${SAMPLER_NAMES.join(', ')})`, {
      variable: ENV_TRACES_SAMPLER,
      value: truncateValue(raw),
      using: 'parentbased_always_on',
    });
    return DEFAULT_SAMPLER;
  }
  if (known !== 'traceidratio' && known !== 'parentbased_traceidratio') return Object.freeze({ name: known, ratio: undefined });
  return Object.freeze({ name: known, ratio: samplerRatioArg(known, warn) });
}

/** `OTEL_TRACES_SAMPLER_ARG` of a ratio sampler: unset or blank → 1.0; invalid → warning, 1.0. */
function samplerRatioArg(sampler: string, warn: Warn): number {
  const raw = envValue(ENV_TRACES_SAMPLER_ARG);
  if (raw === undefined) return 1;
  const ratio = parseRatio(raw);
  if (ratio === undefined) {
    warn(`sampler argument is not a number in [0, 1] for ${sampler}`, { variable: ENV_TRACES_SAMPLER_ARG, value: truncateValue(raw), using: '1.0' });
    return 1;
  }
  return ratio;
}

/** The SDK sampler of a {@link SamplerConfig} (before the health wrapper). */
export function buildSampler(config: SamplerConfig): Sampler {
  const ratio = (): Sampler => new TraceIdRatioBasedSampler(config.ratio ?? 1);
  switch (config.name) {
    case 'always_on':
      return new AlwaysOnSampler();
    case 'always_off':
      return new AlwaysOffSampler();
    case 'traceidratio':
      return ratio();
    case 'parentbased_always_off':
      return new ParentBasedSampler({ root: new AlwaysOffSampler() });
    case 'parentbased_traceidratio':
      return new ParentBasedSampler({ root: ratio() });
    case 'parentbased_always_on':
      return new ParentBasedSampler({ root: new AlwaysOnSampler() });
  }
}

/**
 * `OTEL_PROPAGATORS` (R8, contract D1 rows "Propagators", "casing / empty
 * tokens" and "Propagator diagnostics", A20): comma-separated, trimmed, ASCII
 * case-insensitive, empty tokens and duplicates dropped; every token is
 * scanned. `tracecontext` and `baggage` are linked; `none` disables
 * propagation; spec values the helper does not link warn as "not supported",
 * anything else as "unknown", one warning per token; when nothing supported
 * is left the W3C default applies; `none` with other values warns. Every
 * warning names the effective list as `using`. Returns `[]` for `none`.
 */
export function resolvePropagators(warn: Warn): readonly PropagatorName[] {
  const raw = envValue(ENV_PROPAGATORS);
  if (raw === undefined) return DEFAULT_PROPAGATORS;
  const linked: PropagatorName[] = [];
  const skipped: string[] = [];
  let none = false;
  const seen = new Set<string>();
  for (const part of raw.split(',')) {
    const value = asciiLower(trimWS(part));
    if (value === '' || seen.has(value)) continue;
    seen.add(value);
    if (value === 'tracecontext' || value === 'baggage') linked.push(value);
    else if (value === 'none') none = true;
    else skipped.push(value);
  }
  const effective: readonly PropagatorName[] = none ? Object.freeze([]) : linked.length === 0 ? DEFAULT_PROPAGATORS : Object.freeze(linked);
  const using = effective.length === 0 ? 'none' : effective.join(', ');
  for (const value of skipped) {
    const message = UNSUPPORTED_PROPAGATORS.has(value) ? 'propagator not supported by the Node.js helper, ignored' : 'unknown propagator, ignored';
    warn(message, { variable: ENV_PROPAGATORS, value: truncateValue(value), using });
  }
  if (none && (linked.length > 0 || skipped.length > 0)) {
    warn('"none" combined with other propagators; propagation disabled', { variable: ENV_PROPAGATORS, value: truncateValue(raw), using: 'none' });
  }
  return effective;
}

/** The global propagator of the resolved names (`[]`: one that injects and extracts nothing). */
export function buildPropagator(names: readonly PropagatorName[]): TextMapPropagator {
  return new CompositePropagator({
    propagators: names.map((n) => (n === 'tracecontext' ? new W3CTraceContextPropagator() : new W3CBaggagePropagator())),
  });
}

function cutQuery(path: string): string {
  const cut = path.search(/[?#]/);
  return cut >= 0 ? path.slice(0, cut) : path;
}

/**
 * Strips the query and fragment, percent-decodes, lowercases and drops
 * trailing slashes (`/` stays `/`). Decoding follows Go, whose sampler sees
 * the decoded `req.URL.Path` (contract design D1 row "Percent-encoded health
 * paths", A22, 2026-11-02): `/%68ealth` and `/health%2F` are health paths. A
 * malformed escape keeps the raw path (Go's server rejects such a request).
 * The decoded path is cut again, as Go's normalization of a decoded `?` does.
 */
export function normalizeHealthPath(path: string): string {
  let p = cutQuery(path);
  if (p.includes('%')) {
    try {
      p = cutQuery(decodeURIComponent(p));
    } catch {
      // Malformed escape: matched as written.
    }
  }
  // ASCII only (contract design D1 row "ASCII case folding", A20).
  p = asciiLower(p);
  while (p.length > 1 && p.endsWith('/')) p = p.slice(0, -1);
  return p;
}

const GRPC_HEALTH_SERVICE = 'grpc.health.v1.Health/';
// instrumentation-grpc names spans `grpc.<service>/<method>` (design D-JS7).
const GRPC_HEALTH_SPAN = `grpc.${GRPC_HEALTH_SERVICE}`;

/**
 * Whether a span name is the instrumentation's name for a
 * `grpc.health.v1.Health` method (the whole service: Check, Watch, List):
 * `grpc.grpc.health.v1.Health/<method>` only. A user service in package
 * `health.v1` (span `grpc.health.v1.Health/<method>`) is not health.
 */
export function isGrpcHealthSpanName(name: string): boolean {
  return name.startsWith(GRPC_HEALTH_SPAN);
}

const ATTR_URL_PATH = 'url.path';
// instrumentation-http 0.300 never sets it; kept as the Go/Python fallback (old HTTP semconv).
const ATTR_HTTP_TARGET = 'http.target';

/**
 * Drops health-check spans and defers everything else to `inner` (contract
 * D5, design D-JS7), wrapping the whole resolved sampler — outside
 * ParentBased, as Go's `healthSampler`. It decides from what the span carries
 * at start: SERVER spans with no parent or a remote one (a probe carrying a
 * sampled `traceparent` is still a probe) whose `url.path` (else
 * `http.target`) is a health path — exact, case-insensitive, trailing slash
 * and query ignored — or whose name is the instrumentation's span name of a
 * `grpc.health.v1.Health` method (`grpc.grpc.health.v1.Health/<method>`;
 * instrumentation-grpc sets `rpc.*` only after start, so the name is all the
 * sampler sees); and CLIENT spans of that gRPC service with any parent. A drop keeps the parent's tracestate. Children of a dropped
 * span follow it through ParentBased (the default); a sampler that ignores
 * the parent (`always_on`, `traceidratio`) samples them again. The
 * instrumentation still runs, so the requests stay counted in metrics.
 */
export class HealthCheckSampler implements Sampler {
  private readonly paths: ReadonlySet<string>;

  constructor(
    private readonly inner: Sampler,
    paths: readonly string[] = DEFAULT_HEALTH_PATHS,
  ) {
    this.paths = new Set(paths.map(normalizeHealthPath));
  }

  /** Whether `path` (raw, as received) is one of this sampler's health paths. */
  isHealthPath(path: string): boolean {
    return this.paths.has(normalizeHealthPath(path));
  }

  shouldSample(ctx: Context, traceId: string, spanName: string, spanKind: SpanKind, attributes: Attributes, links: Link[]): SamplingResult {
    const parent = trace.getSpanContext(ctx);
    const valid = parent !== undefined && isSpanContextValid(parent);
    const entry = !valid || parent.isRemote === true;
    const drop =
      (spanKind === SpanKind.SERVER && entry && this.isHealth(spanName, attributes)) ||
      (spanKind === SpanKind.CLIENT && isGrpcHealthSpanName(spanName));
    if (drop) {
      // The parent's tracestate is kept (contract D5); without one the SDK keeps none either.
      return valid && parent.traceState !== undefined
        ? { decision: SamplingDecision.NOT_RECORD, traceState: parent.traceState }
        : { decision: SamplingDecision.NOT_RECORD };
    }
    return this.inner.shouldSample(ctx, traceId, spanName, spanKind, attributes, links);
  }

  private isHealth(name: string, attributes: Attributes): boolean {
    if (isGrpcHealthSpanName(name)) return true;
    const path = attributes[ATTR_URL_PATH] ?? attributes[ATTR_HTTP_TARGET];
    return typeof path === 'string' && this.isHealthPath(path);
  }

  toString(): string {
    return `HealthCheckExcluding{${this.inner.toString()}}`;
  }
}

/**
 * Marks the service's entry spans (no parent, or a remote one) with
 * `debug="true"` (R9, ADR-006) so the Collector's tail sampler keeps the
 * trace. Installed only when debug mode is on in local config (`debug` /
 * `OTEL_HELPER_DEBUG_LEVEL`): the marker is never derived from inbound
 * tracestate or baggage, and the processor never writes tracestate.
 */
export class DebugMarkerProcessor implements SpanProcessor {
  onStart(span: Span): void {
    const parent = span.parentSpanContext;
    if (parent === undefined || parent.isRemote === true) span.setAttribute('debug', 'true');
  }

  onEnd(): void {
    // Nothing to do at end: the attribute is set at start.
  }

  forceFlush(): Promise<void> {
    return Promise.resolve();
  }

  shutdown(): Promise<void> {
    return Promise.resolve();
  }
}

/**
 * gRPC status codes that make a SERVER span an error (UNKNOWN 2,
 * DEADLINE_EXCEEDED 4, UNIMPLEMENTED 12, INTERNAL 13, UNAVAILABLE 14,
 * DATA_LOSS 15): the RPC semconv server rule, as Go otelgrpc
 * (`serverStatus`) and Python (`_utilities.py`) apply it.
 */
const GRPC_SERVER_ERROR_CODES: ReadonlySet<number> = new Set([2, 4, 12, 13, 14, 15]);

/** The writable status of an SDK span in `onEnding` (`setStatus` refuses UNSET). */
interface MutableStatus {
  status: { code: SpanStatusCode; message?: string };
}

/**
 * Normalizes gRPC SERVER span status before the span ends (contract task
 * A22, 2026-11-02): instrumentation-grpc 0.300 marks every non-OK status
 * ERROR, Go and Python only the six codes of {@link GRPC_SERVER_ERROR_CODES}.
 * A SERVER span with `rpc.system=grpc` and another `rpc.grpc.status_code`
 * (NOT_FOUND, INVALID_ARGUMENT, …: the client's fault) ends with status
 * UNSET. Runs in `onEnding`, so every later processor and exporter (the
 * app's own `spanProcessors` too) sees the same status; an exporter wrapper
 * would leave those processors with the upstream ERROR. The span's
 * attributes (`rpc.grpc.status_code`, `grpc.error_*`) are kept.
 */
export class GrpcServerStatusProcessor implements SpanProcessor {
  onStart(): void {
    // Decided at end, from the status code the instrumentation set.
  }

  onEnding(span: Span): void {
    // Runs inside span.end() on the app's RPC path: never throws.
    try {
      if (span.kind !== SpanKind.SERVER || span.status.code !== SpanStatusCode.ERROR) return;
      const attrs = span.attributes;
      const code = attrs['rpc.grpc.status_code'];
      if (attrs['rpc.system'] !== 'grpc' || typeof code !== 'number' || GRPC_SERVER_ERROR_CODES.has(code)) return;
      // Also clears an app's own setStatus(ERROR) on such a span (contract D1 row "gRPC server span status").
      (span as unknown as MutableStatus).status = { code: SpanStatusCode.UNSET };
    } catch {
      // A frozen or exotic span keeps its status.
    }
  }

  onEnd(): void {
    // Nothing to do: the status is fixed in onEnding.
  }

  forceFlush(): Promise<void> {
    return Promise.resolve();
  }

  shutdown(): Promise<void> {
    return Promise.resolve();
  }
}
