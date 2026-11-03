/**
 * The TracerProvider and MeterProvider `@staffops/otel-helper-sql` binds each
 * contrib instrumentation to (task B10a, fix round 2026-11-03).
 *
 * Tracer (`dbTracerProvider`):
 * - ERROR status keeps its code and drops the description (contract D1 row
 *   "DB error status", A25): the description is the database's error text,
 *   which can quote a statement value. Go (XSAM otelsql) sets an empty
 *   description; Python's `ext.sql` drops it the same way. The `exception`
 *   event is semconv-defined and kept (pg writes a sanitized message there).
 * - the legacy `db.system` on the span-start attributes (design D-JS9);
 * - for mysql2, a normalized span name (`statementSpanName`).
 *
 * Meter (`dbMeterProvider`): the legacy key on synchronous measurements, and
 * a generation guard — an instrument created before the latest bind records
 * nothing. instrumentation-pg re-creates its instruments on every
 * `setMeterProvider` and moves the pool `max` by subtracting it from the old
 * instrument and adding it to the new one; through the helper's stable
 * forwarding MeterProvider both reach the new SDK provider and cancel out, so
 * `db.client.connection.max` read 0 after a re-setup. With the guard the
 * subtraction is dropped and the new provider gets the value.
 *
 * Members the wrappers do not change (`forceFlush`, `getDelegate`, anything a
 * later API adds) are forwarded to the wrapped object.
 */
import { SpanStatusCode, type Attributes, type Context, type Meter, type MeterProvider, type Span, type SpanOptions, type SpanStatus, type Tracer, type TracerProvider } from '@opentelemetry/api';
import { ATTR_DB_SYSTEM_NAME, withLegacyKey } from './semconv';

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

export const MAX_SPAN_NAME = 32;

/**
 * mysql2 span name from the statement: upstream takes the text before its
 * first space, so `\n  SELECT 1` gives an empty name, `/* c *\/ SELECT 1` the
 * comment, and `SELECT 1;` with a tab a whole statement. Leading whitespace
 * and comments (`--`, `#`, `/* *\/`) are skipped; the first word (letters,
 * digits, `_`) is uppercased and cut at {@link MAX_SPAN_NAME}; with none, the
 * `db.system.name` value (`mysql`). pg already names spans from the operation.
 */
export function statementSpanName(text: unknown, fallback: string): string {
  if (typeof text !== 'string') return fallback;
  let rest = text;
  for (;;) {
    rest = rest.replace(/^\s+/, '');
    if (rest.startsWith('--') || rest.startsWith('#')) {
      const nl = rest.indexOf('\n');
      rest = nl < 0 ? '' : rest.slice(nl + 1);
    } else if (rest.startsWith('/*')) {
      const end = rest.indexOf('*/', 2);
      rest = end < 0 ? '' : rest.slice(end + 2);
    } else {
      break;
    }
  }
  const word = /^[A-Za-z0-9_]+/.exec(rest)?.[0];
  if (word === undefined) return fallback;
  return word.slice(0, MAX_SPAN_NAME).replace(/[a-z]+/g, (m) => m.toUpperCase());
}

/** Options of {@link dbTracerProvider}. */
export interface DbTracerOptions {
  /** Copy `db.system.name` into the legacy `db.system` at span start. */
  readonly legacyKeys: boolean;
  /** Rename spans with {@link statementSpanName} (mysql2). */
  readonly normalizeSpanName: boolean;
}

/**
 * `span.setStatus` keeps an ERROR code and drops its description; when the
 * span has no `error.type` it gets `_OTHER` (semconv: the fallback value when
 * no specific type is known), so the failure stays classifiable (2026-11-03, N1).
 */
function withoutErrorDescription(span: Span): Span {
  const original = span.setStatus.bind(span);
  try {
    span.setStatus = (status: SpanStatus): Span => {
      if (status.code !== SpanStatusCode.ERROR) {
        original(status);
        return span;
      }
      original({ code: SpanStatusCode.ERROR });
      const attributes = (span as { readonly attributes?: Attributes }).attributes;
      if (attributes?.['error.type'] === undefined) span.setAttribute('error.type', '_OTHER');
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
  const name = (requested: string, options: SpanOptions | undefined): string => {
    if (!opts.normalizeSpanName) return requested;
    const attrs: Attributes | undefined = options?.attributes;
    const system = attrs?.[ATTR_DB_SYSTEM_NAME];
    return statementSpanName(attrs?.['db.query.text'], typeof system === 'string' ? system : requested);
  };
  const startSpan = (spanName: string, options?: SpanOptions, context?: Context): Span =>
    withoutErrorDescription(tracer.startSpan(name(spanName, options), spanOptions(options, opts.legacyKeys), context));
  const startActiveSpan = (spanName: string, ...rest: unknown[]): unknown => {
    // (name, fn) | (name, options, fn) | (name, options, context, fn).
    const fn = rest.pop() as (span: Span) => unknown;
    const options = rest[0] as SpanOptions | undefined;
    const next: unknown[] = rest.length > 0 ? [spanOptions(options, opts.legacyKeys), ...rest.slice(1)] : [];
    const start = tracer as unknown as { startActiveSpan(...args: unknown[]): unknown };
    return start.startActiveSpan(name(spanName, options), ...next, (span: Span) => fn(withoutErrorDescription(span)));
  };
  return forwarding(tracer, { startSpan, startActiveSpan });
}

/** A TracerProvider whose tracers apply {@link DbTracerOptions} and drop ERROR descriptions. */
export function dbTracerProvider(provider: TracerProvider, opts: DbTracerOptions): TracerProvider {
  const getTracer = (...args: Parameters<TracerProvider['getTracer']>): Tracer => dbTracer(provider.getTracer(...args), opts);
  return forwarding(provider, { getTracer });
}

/** Options of {@link dbMeterProvider}. */
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
  // registers itself: passed through (neither pg nor mysql2 uses them).
  return forwarding(meter, {
    createCounter: (n, o) => sync(meter.createCounter(n, o), 'add'),
    createUpDownCounter: (n, o) => sync(meter.createUpDownCounter(n, o), 'add'),
    createHistogram: (n, o) => sync(meter.createHistogram(n, o), 'record'),
    createGauge: (n, o) => sync(meter.createGauge(n, o), 'record'),
  });
}

/** A MeterProvider whose meters apply {@link DbMeterOptions}. */
export function dbMeterProvider(provider: MeterProvider, opts: DbMeterOptions): MeterProvider {
  const getMeter = (...args: Parameters<MeterProvider['getMeter']>): Meter => dbMeter(provider.getMeter(...args), opts);
  return forwarding(provider, { getMeter });
}
