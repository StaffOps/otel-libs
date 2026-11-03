/**
 * `@staffops/otel-helper/internal/db` — NOT public API (Node design D-JS9,
 * tasks B10a/B10b). The database-span rules the opt-in instrumentation
 * packages `@staffops/otel-helper-sql` and `@staffops/otel-helper-redis`
 * share, so the rules have one implementation and not a copy per package
 * (moved here from `-sql` in B10b, 2026-11-03). Those packages peer-depend on
 * the core at the same version; this entry may change in any release, so
 * applications must not import it.
 *
 * - {@link dbTracerProvider}: the TracerProvider each contrib DB
 *   instrumentation is bound to. An ERROR status keeps its code and drops the
 *   description (contract D1 row "DB error status", A25: the description is
 *   the server's error text, which can quote a value), with `error.type`
 *   `_OTHER` when the span has none; the legacy `db.system` on the span-start
 *   attributes; an optional span-name rule.
 * - {@link dbMeterProvider}: the legacy key on synchronous measurements, and a
 *   generation guard (an instrument created before the latest bind records
 *   nothing; instrumentation-pg moves its pool max between instrument
 *   generations on every re-bind).
 * - {@link legacyKeysMode}: `OTEL_SEMCONV_STABILITY_OPT_IN` database mode,
 *   read once per process by the first DB instrumentation factory call.
 *
 * Members the wrappers do not change (`forceFlush`, `getDelegate`, anything a
 * later API adds) are forwarded to the wrapped object.
 *
 * @internal
 * @packageDocumentation
 */
import { SpanStatusCode, type Attributes, type Context, type Meter, type MeterProvider, type Span, type SpanOptions, type SpanStatus, type Tracer, type TracerProvider } from '@opentelemetry/api';

/** @internal */
export const ENV_SEMCONV_OPT_IN = 'OTEL_SEMCONV_STABILITY_OPT_IN';
/** @internal */
export const ATTR_DB_SYSTEM_NAME = 'db.system.name';
/** @internal */
export const ATTR_DB_SYSTEM = 'db.system';

/**
 * `db.system.name` (semconv v1.43) → legacy `db.system` (v1.26) for the
 * systems the opt-in packages instrument. The values are identical (Go maps
 * the same pairs: `ext/otelsql` `systemAttributes`, redisotel `db.system`);
 * an unknown value is copied as it is.
 *
 * @internal
 */
export const LEGACY_DB_SYSTEM: Readonly<Record<string, string>> = Object.freeze({
  postgresql: 'postgresql',
  mysql: 'mysql',
  redis: 'redis',
});

// The contract trim set (design D1 "Trimmed character set") and ASCII-only
// case folding (D1 "ASCII case folding"), as the core resolver.
const LEADING = /^[ \t\n\r\v\f]+/;
const TRAILING = /[ \t\n\r\v\f]+$/;

function token(raw: string): string {
  return raw.replace(LEADING, '').replace(TRAILING, '').replace(/[A-Z]+/g, (m) => m.toLowerCase());
}

/**
 * Whether the legacy `db.system` key is emitted: yes unless the value holds
 * `database` without `database/dup` (Go `legacyKeysEnabled`, Python since
 * A25). Unset, empty or without database tokens: yes (the helper default is
 * `http,database/dup`).
 *
 * @internal
 */
export function legacyKeysEnabled(value: string | undefined = process.env[ENV_SEMCONV_OPT_IN]): boolean {
  if (value === undefined) return true;
  const tokens = new Set(value.split(',').map(token));
  return tokens.has('database/dup') || !tokens.has('database');
}

let legacyKeys: boolean | undefined;

/**
 * The database mode, read once per process by the first caller (the first
 * `sqlInstrumentation()` or `redisInstrumentation()`, as Go reads it at its
 * first `otelsql.Open`).
 *
 * @internal
 */
export function legacyKeysMode(): boolean {
  legacyKeys ??= legacyKeysEnabled();
  return legacyKeys;
}

/**
 * `attributes` plus the legacy `db.system` when they carry `db.system.name`
 * and no `db.system`.
 *
 * @internal
 */
export function withLegacyKey<A extends Attributes | undefined>(attributes: A): A;
export function withLegacyKey(attributes: Attributes | undefined): Attributes | undefined {
  if (attributes === undefined) return undefined;
  const name = attributes[ATTR_DB_SYSTEM_NAME];
  if (typeof name !== 'string' || attributes[ATTR_DB_SYSTEM] !== undefined) return attributes;
  return { ...attributes, [ATTR_DB_SYSTEM]: LEGACY_DB_SYSTEM[name] ?? name };
}

/** Forwards every member of `target` not in `own`; methods are bound to `target`. */
function forwarding<T extends object>(target: T, own: Partial<T>): T {
  return new Proxy(target, {
    get(t, prop) {
      if (Object.prototype.hasOwnProperty.call(own, prop)) return own[prop as keyof T];
      const value: unknown = Reflect.get(t, prop, t);
      return typeof value === 'function' ? (value as (...a: unknown[]) => unknown).bind(t) : value;
    },
  });
}

/**
 * Options of {@link dbTracerProvider}.
 *
 * @internal
 */
export interface DbTracerOptions {
  /** Copy `db.system.name` into the legacy `db.system` at span start. */
  readonly legacyKeys: boolean;
  /** The span name from the requested one and the span-start attributes (default: unchanged). */
  readonly spanName?: ((requested: string, attributes: Attributes | undefined) => string) | undefined;
  /**
   * The response status code in a dropped ERROR description (`-redis`: the
   * reply's leading code, `WRONGTYPE`): set as `db.response.status_code` and
   * `error.type` (A26, 2026-11-03). Unset (`-sql`) or none found: `error.type`
   * `_OTHER`.
   */
  readonly responseStatusCode?: ((description: string) => string | undefined) | undefined;
}

/**
 * `span.setStatus` keeps an ERROR code and drops its description; when the
 * span has no `error.type` it gets `_OTHER` (semconv: the fallback value when
 * no specific type is known), so the failure stays classifiable (2026-11-03, N1).
 */
function withoutErrorDescription(span: Span, statusCode: DbTracerOptions['responseStatusCode']): Span {
  const original = span.setStatus.bind(span);
  try {
    span.setStatus = (status: SpanStatus): Span => {
      if (status.code !== SpanStatusCode.ERROR) {
        original(status);
        return span;
      }
      original({ code: SpanStatusCode.ERROR });
      const attributes = (span as { readonly attributes?: Attributes }).attributes;
      if (attributes?.['error.type'] === undefined) {
        const code = statusCode?.(status.message ?? '');
        if (code !== undefined) span.setAttribute('db.response.status_code', code);
        span.setAttribute('error.type', code ?? '_OTHER');
      }
      return span;
    };
  } catch {
    // A frozen span: left as it is.
  }
  return span;
}

function spanOptions(options: SpanOptions | undefined, legacyKeys: boolean): SpanOptions | undefined {
  if (!legacyKeys || options?.attributes === undefined) return options;
  const attributes = withLegacyKey(options.attributes);
  return attributes === options.attributes ? options : { ...options, attributes };
}

function dbTracer(tracer: Tracer, opts: DbTracerOptions): Tracer {
  const name = (requested: string, options: SpanOptions | undefined): string => (opts.spanName === undefined ? requested : opts.spanName(requested, options?.attributes));
  const startSpan = (spanName: string, options?: SpanOptions, context?: Context): Span =>
    withoutErrorDescription(tracer.startSpan(name(spanName, options), spanOptions(options, opts.legacyKeys), context), opts.responseStatusCode);
  const startActiveSpan = (spanName: string, ...rest: unknown[]): unknown => {
    // (name, fn) | (name, options, fn) | (name, options, context, fn).
    const fn = rest.pop() as (span: Span) => unknown;
    const options = rest[0] as SpanOptions | undefined;
    const next: unknown[] = rest.length > 0 ? [spanOptions(options, opts.legacyKeys), ...rest.slice(1)] : [];
    const start = tracer as unknown as { startActiveSpan(...args: unknown[]): unknown };
    return start.startActiveSpan(name(spanName, options), ...next, (span: Span) => fn(withoutErrorDescription(span, opts.responseStatusCode)));
  };
  return forwarding(tracer, { startSpan, startActiveSpan });
}

/**
 * A TracerProvider whose tracers apply {@link DbTracerOptions} and drop ERROR descriptions.
 *
 * @internal
 */
export function dbTracerProvider(provider: TracerProvider, opts: DbTracerOptions): TracerProvider {
  const getTracer = (...args: Parameters<TracerProvider['getTracer']>): Tracer => dbTracer(provider.getTracer(...args), opts);
  return forwarding(provider, { getTracer });
}

/**
 * Options of {@link dbMeterProvider}.
 *
 * @internal
 */
export interface DbMeterOptions {
  /** Copy `db.system.name` into the legacy `db.system` on every synchronous measurement. */
  readonly legacyKeys: boolean;
  /** False once a newer bind replaced this one: its instruments then record nothing. */
  readonly current: () => boolean;
}

type Recorder = (value: number, attributes?: Attributes, context?: Context) => void;

function guarded(record: Recorder, opts: DbMeterOptions): Recorder {
  return (value, attributes, context) => {
    if (opts.current()) record(value, opts.legacyKeys ? withLegacyKey(attributes) : attributes, context);
  };
}

function dbMeter(meter: Meter, opts: DbMeterOptions): Meter {
  const sync = <I extends object>(instrument: I, method: 'add' | 'record'): I => {
    const original = (instrument as Record<string, Recorder | undefined>)[method];
    if (original === undefined) return instrument;
    return forwarding(instrument, { [method]: guarded(original.bind(instrument), opts) } as Partial<I>);
  };
  // Observable instruments report through callbacks the instrumentation
  // registers itself: passed through (none of the wrapped contribs uses them).
  return forwarding(meter, {
    createCounter: (n, o) => sync(meter.createCounter(n, o), 'add'),
    createUpDownCounter: (n, o) => sync(meter.createUpDownCounter(n, o), 'add'),
    createHistogram: (n, o) => sync(meter.createHistogram(n, o), 'record'),
    createGauge: (n, o) => sync(meter.createGauge(n, o), 'record'),
  });
}

/**
 * A MeterProvider whose meters apply {@link DbMeterOptions}.
 *
 * @internal
 */
export function dbMeterProvider(provider: MeterProvider, opts: DbMeterOptions): MeterProvider {
  const getMeter = (...args: Parameters<MeterProvider['getMeter']>): Meter => dbMeter(provider.getMeter(...args), opts);
  return forwarding(provider, { getMeter });
}
