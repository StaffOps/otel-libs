/**
 * Stable, helper-owned forwarding providers and propagator (design D-JS10,
 * decided 2026-10-29). The helper registers one of
 * each as the API global once per process and swaps its delegate on every
 * `setup()` / `shutdown()`: tracers, meters, instruments and loggers obtained
 * before a re-setup keep working, bound to the new SDK providers. With the
 * API's own globals they would stay bound to the old, shut-down provider (the
 * API's proxy tracer keeps the first delegate it resolved, and the metrics API
 * has no proxy at all).
 *
 * Without a delegate (before the first setup, after `shutdown()` settled)
 * every synchronous call goes to a no-op. Observable and batch callbacks stay
 * registered with the last SDK instruments until a new delegate arrives, so a
 * provider's final collection (during its `shutdown()`) still calls them. The
 * propagator keeps its delegate after `shutdown()`: Go and Python leave their
 * propagator installed too.
 */
import {
  context as contextApi,
  createNoopLogger,
  createNoopMeter,
  INVALID_SPAN_CONTEXT,
  isSpanContextValid,
  trace,
  type Attributes,
  type BatchObservableCallback,
  type BatchObservableResult,
  type Context,
  type Counter,
  type Gauge,
  type Histogram,
  type Logger,
  type LoggerOptions,
  type LoggerProvider,
  type LogRecord,
  type Meter,
  type MeterOptions,
  type MeterProvider,
  type MetricOptions,
  type Observable,
  type ObservableCallback,
  type ObservableCounter,
  type ObservableGauge,
  type ObservableUpDownCounter,
  type Span,
  type SpanOptions,
  type TextMapGetter,
  type TextMapPropagator,
  type TextMapSetter,
  type Tracer,
  type TracerOptions,
  type TracerProvider,
  type UpDownCounter,
} from '@opentelemetry/api';

const NOOP_METER: Meter = createNoopMeter();
const NOOP_LOGGER: Logger = createNoopLogger();

/** No sentinel collides with a real delegate (`undefined` is a valid "from"). */
const UNSET: unique symbol = Symbol('unset');

/** One value derived from a source, rebuilt when the source changes (by identity). */
class Derived<S, V> {
  private from: S | typeof UNSET = UNSET;
  private value: V | undefined;

  get(source: S, make: (source: S) => V): V {
    if (this.from !== source) {
      this.value = make(source);
      this.from = source;
    }
    return this.value as V;
  }
}

/** Cache key of a tracer, meter or logger: name, version and schema URL (the first other options win). */
function scopeKey(name: string, version: string | undefined, schemaUrl: string | undefined): string {
  return JSON.stringify([name, version ?? '', schemaUrl ?? '']);
}

/** A provider's `forceFlush()`, when it has one; resolves at once otherwise. */
function flushOf(delegate: unknown): Promise<void> {
  const flush = (delegate as { forceFlush?: unknown } | undefined)?.forceFlush;
  if (typeof flush !== 'function') return Promise.resolve();
  // Inside then(): a synchronous throw becomes a rejection, never a throw.
  return Promise.resolve()
    .then(() => Reflect.apply(flush, delegate, []) as unknown)
    .then(() => undefined);
}

// ── traces ────────────────────────────────────────────────────────────────

/**
 * The tracer used without a delegate, as the API's `NoopTracer`: a
 * non-recording span that keeps a valid parent span context (none for
 * `root: true`), and `startActiveSpan` runs the callback with it active.
 */
export const NOOP_TRACER: Tracer = Object.freeze({
  startSpan(_name: string, options?: SpanOptions, context: Context = contextApi.active()): Span {
    if (options?.root === true) return trace.wrapSpanContext(INVALID_SPAN_CONTEXT);
    const parent = trace.getSpanContext(context);
    return trace.wrapSpanContext(parent !== undefined && isSpanContextValid(parent) ? parent : INVALID_SPAN_CONTEXT);
  },
  startActiveSpan(name: string, ...rest: unknown[]): unknown {
    const fn = rest[rest.length - 1];
    if (typeof fn !== 'function') return undefined;
    const options = rest.length >= 2 ? (rest[0] as SpanOptions | undefined) : undefined;
    const parent = rest.length >= 3 ? (rest[1] as Context | undefined) ?? contextApi.active() : contextApi.active();
    const span = NOOP_TRACER.startSpan(name, options, parent);
    return contextApi.with(trace.setSpan(parent, span), fn as (span: Span) => unknown, undefined, span);
  },
});

/** The helper's global TracerProvider: forwards to the active setup's SDK provider. */
export class ForwardingTracerProvider implements TracerProvider {
  private delegate: TracerProvider | undefined;
  private readonly tracers = new Map<string, ForwardingTracer>();

  getDelegate(): TracerProvider | undefined {
    return this.delegate;
  }

  setDelegate(next: TracerProvider | undefined): void {
    this.delegate = next;
  }

  /** One tracer per (name, version, schema URL), as the SDK caches them. */
  getTracer(name: string, version?: string, options?: TracerOptions): Tracer {
    const key = scopeKey(name, version, options?.schemaUrl);
    let tracer = this.tracers.get(key);
    if (tracer === undefined) {
      tracer = new ForwardingTracer(this, name, version, options);
      this.tracers.set(key, tracer);
    }
    return tracer;
  }

  /** Flushes the current delegate; resolves at once without one. */
  forceFlush(): Promise<void> {
    return flushOf(this.delegate);
  }
}

class ForwardingTracer implements Tracer {
  private readonly tracer = new Derived<TracerProvider | undefined, Tracer>();

  constructor(
    private readonly owner: ForwardingTracerProvider,
    private readonly name: string,
    private readonly version: string | undefined,
    private readonly options: TracerOptions | undefined,
  ) {}

  private current(): Tracer {
    return this.tracer.get(this.owner.getDelegate(), (p) => (p === undefined ? NOOP_TRACER : p.getTracer(this.name, this.version, this.options)));
  }

  startSpan(name: string, options?: SpanOptions, context?: Context): Span {
    return this.current().startSpan(name, options, context);
  }

  startActiveSpan<F extends (span: Span) => unknown>(name: string, fn: F): ReturnType<F>;
  startActiveSpan<F extends (span: Span) => unknown>(name: string, options: SpanOptions, fn: F): ReturnType<F>;
  startActiveSpan<F extends (span: Span) => unknown>(name: string, options: SpanOptions, context: Context, fn: F): ReturnType<F>;
  startActiveSpan(...args: unknown[]): unknown {
    // Called as a method on the delegate, with the caller's arguments as given.
    return (this.current() as unknown as { startActiveSpan(...a: unknown[]): unknown }).startActiveSpan(...args);
  }
}

// ── metrics ───────────────────────────────────────────────────────────────

/** The helper's global MeterProvider: forwards to the active setup's SDK provider. */
export class ForwardingMeterProvider implements MeterProvider {
  private delegate: MeterProvider | undefined;
  private readonly meters = new Map<string, ForwardingMeter>();
  /** Meters with observable callbacks or batch callbacks: moved to every new delegate. */
  private readonly observing = new Set<ForwardingMeter>();

  getDelegate(): MeterProvider | undefined {
    return this.delegate;
  }

  /**
   * Swaps the delegate. A defined delegate gets every observable and batch
   * callback (an SDK provider only calls the callbacks of its own
   * instruments); without one they stay where they are, so the last
   * provider's final collection still calls them.
   */
  setDelegate(next: MeterProvider | undefined): void {
    this.delegate = next;
    if (next === undefined) return;
    for (const meter of this.observing) meter.rebind();
  }

  /** One meter per (name, version, schema URL), as the SDK caches them. */
  getMeter(name: string, version?: string, options?: MeterOptions): Meter {
    const key = scopeKey(name, version, options?.schemaUrl);
    let meter = this.meters.get(key);
    if (meter === undefined) {
      meter = new ForwardingMeter(this, name, version, options);
      this.meters.set(key, meter);
    }
    return meter;
  }

  /** Flushes the current delegate; resolves at once without one. */
  forceFlush(): Promise<void> {
    return flushOf(this.delegate);
  }

  /** Called by a meter when it gets its first callback, and when it has none left. */
  watch(meter: ForwardingMeter, watched: boolean): void {
    if (watched) this.observing.add(meter);
    else this.observing.delete(meter);
  }

  /** Number of meters holding callbacks (tests: retention). */
  watchedCount(): number {
    return this.observing.size;
  }
}

/** A batch callback as registered with one delegate meter. */
interface BatchRegistration {
  readonly meter: Meter;
  readonly callback: BatchObservableCallback;
  readonly observables: Observable[];
}

interface BatchEntry {
  readonly callback: BatchObservableCallback;
  readonly observables: readonly Observable[];
  registered: BatchRegistration | undefined;
}

class ForwardingMeter implements Meter {
  private readonly meter = new Derived<MeterProvider | undefined, Meter>();
  /** This meter's observable instruments that hold callbacks. */
  private readonly observables = new Set<ForwardingObservable>();
  private readonly batches: BatchEntry[] = [];

  constructor(
    private readonly owner: ForwardingMeterProvider,
    private readonly name: string,
    private readonly version: string | undefined,
    private readonly options: MeterOptions | undefined,
  ) {}

  /** The delegate meter now (the API's no-op meter without a delegate). */
  current(): Meter {
    return this.meter.get(this.owner.getDelegate(), (p) => (p === undefined ? NOOP_METER : p.getMeter(this.name, this.version, this.options)));
  }

  /** The delegate meter, or `undefined` without a delegate (callbacks then stay where they are). */
  target(): Meter | undefined {
    return this.owner.getDelegate() === undefined ? undefined : this.current();
  }

  createCounter<A extends Attributes = Attributes>(name: string, options?: MetricOptions): Counter<A> {
    return new ForwardingAdd<A>(this, (m) => m.createCounter<A>(name, options));
  }

  createUpDownCounter<A extends Attributes = Attributes>(name: string, options?: MetricOptions): UpDownCounter<A> {
    return new ForwardingAdd<A>(this, (m) => m.createUpDownCounter<A>(name, options));
  }

  createHistogram<A extends Attributes = Attributes>(name: string, options?: MetricOptions): Histogram<A> {
    return new ForwardingRecord<A>(this, (m) => m.createHistogram<A>(name, options));
  }

  createGauge<A extends Attributes = Attributes>(name: string, options?: MetricOptions): Gauge<A> {
    return new ForwardingRecord<A>(this, (m) => m.createGauge<A>(name, options));
  }

  createObservableGauge<A extends Attributes = Attributes>(name: string, options?: MetricOptions): ObservableGauge<A> {
    return new ForwardingObservable(this, (m) => m.createObservableGauge(name, options));
  }

  createObservableCounter<A extends Attributes = Attributes>(name: string, options?: MetricOptions): ObservableCounter<A> {
    return new ForwardingObservable(this, (m) => m.createObservableCounter(name, options));
  }

  createObservableUpDownCounter<A extends Attributes = Attributes>(name: string, options?: MetricOptions): ObservableUpDownCounter<A> {
    return new ForwardingObservable(this, (m) => m.createObservableUpDownCounter(name, options));
  }

  /** Called by an observable when it gets its first callback (`true`) and loses its last (`false`). */
  watch(observable: ForwardingObservable, watched: boolean): void {
    if (watched) this.observables.add(observable);
    else this.observables.delete(observable);
    this.updateWatched();
  }

  private updateWatched(): void {
    this.owner.watch(this, this.observables.size > 0 || this.batches.length > 0);
  }

  addBatchObservableCallback<A extends Attributes = Attributes>(callback: BatchObservableCallback<A>, observables: Observable<A>[]): void {
    const cb = callback as BatchObservableCallback;
    if (this.findBatch(cb, observables) >= 0) return; // the SDK ignores a duplicate too
    const entry: BatchEntry = { callback: cb, observables: [...(observables as Observable[])], registered: undefined };
    this.batches.push(entry);
    this.updateWatched();
    const meter = this.target();
    if (meter !== undefined) this.registerBatch(entry, meter);
  }

  removeBatchObservableCallback<A extends Attributes = Attributes>(callback: BatchObservableCallback<A>, observables: Observable<A>[]): void {
    const at = this.findBatch(callback, observables);
    if (at < 0) return;
    const [entry] = this.batches.splice(at, 1);
    const registered = entry?.registered;
    if (registered !== undefined) registered.meter.removeBatchObservableCallback(registered.callback, registered.observables);
    this.updateWatched();
  }

  /** Registers every observable callback and batch callback with the current delegate meter. */
  rebind(): void {
    const meter = this.target();
    if (meter === undefined) return;
    for (const observable of this.observables) observable.on(meter);
    for (const entry of this.batches) this.registerBatch(entry, meter);
  }

  private findBatch(callback: BatchObservableCallback, observables: readonly Observable[]): number {
    return this.batches.findIndex(
      (e) => e.callback === callback && e.observables.length === observables.length && observables.every((o) => e.observables.includes(o)),
    );
  }

  /** `o` as `meter` knows it: this meter's own observables are re-homed, any other passes through unchanged. */
  private own(o: Observable, meter: Meter): Observable {
    return o instanceof ForwardingObservable && o.isOf(this) ? o.on(meter) : o;
  }

  private registerBatch(entry: BatchEntry, meter: Meter): void {
    const previous = entry.registered;
    if (previous?.meter === meter) return;
    // Off the previous delegate first: a delegate seen again never calls it twice.
    previous?.meter.removeBatchObservableCallback(previous.callback, previous.observables);
    const callback: BatchObservableCallback = (result: BatchObservableResult) =>
      entry.callback({ observe: (o, value, attributes) => { result.observe(this.own(o, meter), value, attributes); } });
    const observables = entry.observables.map((o) => this.own(o, meter));
    meter.addBatchObservableCallback(callback, observables);
    entry.registered = { meter, callback, observables };
  }
}

/** Counter and UpDownCounter: each `add` goes to the current delegate meter's instrument. */
class ForwardingAdd<A extends Attributes> implements Counter<A>, UpDownCounter<A> {
  private readonly instrument = new Derived<Meter, Counter<A> | UpDownCounter<A>>();

  constructor(
    private readonly meter: ForwardingMeter,
    private readonly make: (meter: Meter) => Counter<A> | UpDownCounter<A>,
  ) {}

  add(value: number, attributes?: A, context?: Context): void {
    this.instrument.get(this.meter.current(), this.make).add(value, attributes, context);
  }
}

/** Histogram and Gauge: each `record` goes to the current delegate meter's instrument. */
class ForwardingRecord<A extends Attributes> implements Histogram<A>, Gauge<A> {
  private readonly instrument = new Derived<Meter, Histogram<A> | Gauge<A>>();

  constructor(
    private readonly meter: ForwardingMeter,
    private readonly make: (meter: Meter) => Histogram<A> | Gauge<A>,
  ) {}

  record(value: number, attributes?: A, context?: Context): void {
    this.instrument.get(this.meter.current(), this.make).record(value, attributes, context);
  }
}

/**
 * An observable instrument whose callbacks follow the delegate. It is created
 * on a delegate meter only when it needs to be (a callback, a batch), and the
 * meter tracks it only while it holds callbacks (retention).
 */
class ForwardingObservable implements Observable {
  /** The instrument on the delegate meter the callbacks are registered with. */
  private bound: { readonly meter: Meter; readonly instrument: Observable } | undefined;
  private readonly callbacks = new Set<ObservableCallback>();

  constructor(
    private readonly meter: ForwardingMeter,
    private readonly make: (meter: Meter) => Observable,
  ) {}

  /** Whether `meter` created this observable. */
  isOf(meter: ForwardingMeter): boolean {
    return this.meter === meter;
  }

  /**
   * This instrument on `meter`, created there with every callback on first
   * use; the callbacks leave the previous delegate's instrument, so a
   * delegate seen again never calls them twice.
   */
  on(meter: Meter): Observable {
    const bound = this.bound;
    if (bound?.meter === meter) return bound.instrument;
    const instrument = this.make(meter);
    for (const cb of this.callbacks) {
      bound?.instrument.removeCallback(cb);
      instrument.addCallback(cb);
    }
    this.bound = { meter, instrument };
    return instrument;
  }

  addCallback(callback: ObservableCallback): void {
    if (this.callbacks.has(callback)) return;
    const meter = this.meter.target();
    // Without a delegate the callback is only kept: the next delegate gets it.
    const instrument = meter === undefined ? undefined : this.on(meter);
    this.callbacks.add(callback);
    instrument?.addCallback(callback);
    if (this.callbacks.size === 1) this.meter.watch(this, true);
  }

  removeCallback(callback: ObservableCallback): void {
    if (!this.callbacks.delete(callback)) return;
    this.bound?.instrument.removeCallback(callback);
    if (this.callbacks.size === 0) this.meter.watch(this, false);
  }
}

// ── logs ──────────────────────────────────────────────────────────────────

/** The helper's global LoggerProvider (OpenTelemetry Logs API callers): forwards to the active setup's SDK provider. */
export class ForwardingLoggerProvider implements LoggerProvider {
  private delegate: LoggerProvider | undefined;
  private readonly loggers = new Map<string, ForwardingLogger>();

  getDelegate(): LoggerProvider | undefined {
    return this.delegate;
  }

  setDelegate(next: LoggerProvider | undefined): void {
    this.delegate = next;
  }

  /** One logger per (name, version, schema URL), as the SDK caches them. */
  getLogger(name: string, version?: string, options?: LoggerOptions): Logger {
    const key = scopeKey(name, version, options?.schemaUrl);
    let logger = this.loggers.get(key);
    if (logger === undefined) {
      logger = new ForwardingLogger(this, name, version, options);
      this.loggers.set(key, logger);
    }
    return logger;
  }

  /** Flushes the current delegate; resolves at once without one. */
  forceFlush(): Promise<void> {
    return flushOf(this.delegate);
  }
}

class ForwardingLogger implements Logger {
  private readonly logger = new Derived<LoggerProvider | undefined, Logger>();

  constructor(
    private readonly owner: ForwardingLoggerProvider,
    private readonly name: string,
    private readonly version: string | undefined,
    private readonly options: LoggerOptions | undefined,
  ) {}

  private current(): Logger {
    return this.logger.get(this.owner.getDelegate(), (p) => (p === undefined ? NOOP_LOGGER : p.getLogger(this.name, this.version, this.options)));
  }

  emit(logRecord: LogRecord): void {
    this.current().emit(logRecord);
  }

  enabled(options?: Parameters<Logger['enabled']>[0]): boolean {
    return this.current().enabled(options);
  }
}

// ── propagation ───────────────────────────────────────────────────────────

const NOOP_PROPAGATOR: TextMapPropagator = Object.freeze({
  inject: () => undefined,
  extract: (context: Context) => context,
  fields: () => [],
});

/** The helper's global propagator: forwards to the propagator the last setup built. */
export class ForwardingPropagator implements TextMapPropagator {
  private delegate: TextMapPropagator = NOOP_PROPAGATOR;

  /** The current delegate; `undefined` = none (injects nothing, extracts nothing). */
  getDelegate(): TextMapPropagator | undefined {
    return this.delegate === NOOP_PROPAGATOR ? undefined : this.delegate;
  }

  setDelegate(next: TextMapPropagator | undefined): void {
    this.delegate = next ?? NOOP_PROPAGATOR;
  }

  inject(context: Context, carrier: unknown, setter: TextMapSetter): void {
    this.delegate.inject(context, carrier, setter);
  }

  extract(context: Context, carrier: unknown, getter: TextMapGetter): Context {
    return this.delegate.extract(context, carrier, getter);
  }

  fields(): string[] {
    return this.delegate.fields();
  }
}

/** The four forwarders of one process (kept on the shared runtime). */
export interface Forwarders {
  readonly tracerProvider: ForwardingTracerProvider;
  readonly meterProvider: ForwardingMeterProvider;
  readonly loggerProvider: ForwardingLoggerProvider;
  readonly propagator: ForwardingPropagator;
}

export function createForwarders(): Forwarders {
  return Object.freeze({
    tracerProvider: new ForwardingTracerProvider(),
    meterProvider: new ForwardingMeterProvider(),
    loggerProvider: new ForwardingLoggerProvider(),
    propagator: new ForwardingPropagator(),
  });
}
