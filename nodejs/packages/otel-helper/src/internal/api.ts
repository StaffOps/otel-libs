/**
 * Tracer/meter accessors, `withSpan()` and `startRootSpan()`: the Node counterparts of Go
 * `GetTracer`/`GetMeter`/`StartRootSpan` and Python
 * `get_tracer`/`get_meter`/`start_root_span` (design "Layout and public API").
 */
import { context, metrics, SpanKind, trace } from '@opentelemetry/api';
import type { Attributes, Link, Meter, Span, Tracer } from '@opentelemetry/api';
import { isCancellation, markFailure } from './failure';

/** Default instrumentation scope, as Go and Python (`otel-helper`). */
export const DEFAULT_SCOPE = 'otel-helper';

/**
 * A Tracer from the global TracerProvider. `name` is the instrumentation scope
 * (your module or library); empty or omitted → `otel-helper`. The service name
 * comes from the resource, not from here.
 */
export function getTracer(name?: string, version?: string): Tracer {
  return trace.getTracer(name === undefined || name === '' ? DEFAULT_SCOPE : name, version);
}

/** A Meter from the global MeterProvider; same scope rule as {@link getTracer}. */
export function getMeter(name?: string, version?: string): Meter {
  return metrics.getMeter(name === undefined || name === '' ? DEFAULT_SCOPE : name, version);
}

/** Options of {@link startRootSpan}. */
export interface RootSpanOptions {
  /** Tracer to start the span with; default `getTracer()` (scope `otel-helper`). */
  readonly tracer?: Tracer | undefined;
  /** Attributes set at span start, so the sampler sees them (Go `WithAttributes`, Python `attributes=`). */
  readonly attributes?: Attributes | undefined;
  /** Span kind; default INTERNAL (as Go and Python). */
  readonly kind?: SpanKind | undefined;
  /** Links set at span start. */
  readonly links?: Link[] | undefined;
}

/** Options of {@link withSpan}. */
export interface WithSpanOptions extends RootSpanOptions {
  /**
   * `true` starts a new trace (a root span, as {@link startRootSpan});
   * default `false`: a child of the active span.
   */
  readonly root?: boolean | undefined;
}

/**
 * What {@link withSpan} and {@link startRootSpan} return: `Promise<U>` for a
 * thenable `fn` result (`PromiseLike<U>`, an async function), the result
 * itself otherwise.
 */
export type SpanResult<T> = T extends PromiseLike<infer U> ? Promise<U> : T;

/** The return type of {@link startRootSpan} (the same as {@link SpanResult}). */
export type RootSpanResult<T> = SpanResult<T>;

function endSafely(span: Span): void {
  try {
    span.end();
  } catch {
    // Ending must never replace the application's own error.
  }
}

/** A failure leaves the span: the rule (cancellation → Unset, no record), then end. Never throws. */
function failAndEnd(span: Span, err: unknown): void {
  if (!isCancellation(err)) markFailure(err, span);
  endSafely(span);
}

function run<T>(caller: string, name: string, fn: (span: Span) => T, options: WithSpanOptions, forceRoot: boolean): SpanResult<T> {
  if (typeof name !== 'string') throw new TypeError(`${caller}: name must be a string, got ${typeof name}`);
  if (typeof fn !== 'function') throw new TypeError(`${caller}: fn must be a function, got ${typeof fn}`);
  const opts: unknown = options;
  if (typeof opts !== 'object' || opts === null) throw new TypeError(`${caller}: options must be an object`);
  const rootOption: unknown = options.root;
  if (rootOption !== undefined && typeof rootOption !== 'boolean') throw new TypeError(`${caller}: root must be a boolean, got ${typeof rootOption}`);
  const root = forceRoot || rootOption === true;
  const tracer = options.tracer ?? getTracer();
  const spanOptions = {
    ...(root ? { root: true } : {}),
    kind: options.kind ?? SpanKind.INTERNAL,
    ...(options.attributes === undefined ? {} : { attributes: options.attributes }),
    ...(options.links === undefined ? {} : { links: options.links }),
  };
  return tracer.startActiveSpan(name, spanOptions, context.active(), (span: Span): SpanResult<T> => {
    try {
      const result = fn(span);
      const then: unknown = typeof result === 'object' || typeof result === 'function'
        ? (result as { then?: unknown } | null)?.then
        : undefined;
      if (typeof then !== 'function') {
        endSafely(span);
        return result as SpanResult<T>;
      }
      return Promise.resolve(result).then(
        (value: unknown) => {
          endSafely(span);
          return value;
        },
        (err: unknown) => {
          failAndEnd(span, err);
          throw err;
        },
      ) as SpanResult<T>;
    } catch (err) {
      failAndEnd(span, err);
      throw err;
    }
  });
}

/**
 * Runs `fn(span)` in a span and applies the failure rule of application
 * spans (contract R11, design D15) — the Node counterpart of Go `WithSpan`
 * and Python `traced`.
 *
 * - The span is a child of the active span (`root: true` starts a new
 *   trace, as {@link startRootSpan}); it is active inside `fn`. Kind
 *   INTERNAL unless `kind` is given; `attributes` and `links` are set at
 *   start, so the sampler sees them.
 * - Sync `fn`: the span ends when `fn` returns. A thenable result: the call
 *   returns a real `Promise` that settles once with it, and the span ends then.
 * - Success leaves the status Unset; the helper never sets Ok.
 * - A throw, a rejection or a throwing `then` sets status ERROR **without a
 *   description** and `error.type`, adds **no** `exception` event, and writes
 *   one ERROR log record (event name `exception`, body = the message,
 *   `exception.type` / `exception.message` / `exception.stacktrace`) in the
 *   span's context; then the same value is rethrown. A failure that crosses
 *   several helper spans marks each of them, and each writes its own record
 *   in its own span context (no deduplication).
 * - A cancellation (`name === 'AbortError'`, a grpc-js `CANCELLED`
 *   `ServiceError`) leaves the span Unset with no record; a `TimeoutError`,
 *   and an `AbortError` whose `cause` is one (Node core APIs given
 *   `AbortSignal.timeout()`), is a failure with `error.type` `TimeoutError`.
 * - `error.type`: a non-empty string under `Symbol.for('otel.error_type')`
 *   wins; else the error's `name` when it is not `"Error"`, else its
 *   `constructor.name`, else `"Error"`.
 * - A promise that never settles leaves the span open (never exported).
 *
 * @throws TypeError when `name` is not a string, `fn` not a function,
 * `options` not an object or `root` defined and not a boolean.
 */
export function withSpan<T>(name: string, fn: (span: Span) => T, options: WithSpanOptions = {}): SpanResult<T> {
  return run('withSpan', name, fn, options, false);
}

/**
 * Runs `fn(span)` in a new **root** span — a new trace, whatever span is
 * active (Go `StartRootSpan` with `WithNewRoot`, Python `start_root_span`).
 * Use it in workers where each iteration is an independent trace. The same
 * as `withSpan(name, fn, { ...options, root: true })`: the same settlement
 * and failure rule (status ERROR without a description, `error.type`, no
 * `exception` event, one correlated ERROR record; cancellation Unset).
 *
 * - Other context values (baggage) are kept, as in Go and Python, and the
 *   sampler sees no parent (a remote or unsampled one included), so the
 *   root is sampled on its own.
 * - Work `fn` starts without awaiting it runs under the span after it
 *   ended: its child spans keep the trace, but the root no longer measures them.
 *
 * @throws TypeError when `name` is not a string, `fn` not a function,
 * `options` not an object or `root` defined and not a boolean.
 */
export function startRootSpan<T>(name: string, fn: (span: Span) => T, options: RootSpanOptions = {}): RootSpanResult<T> {
  return run('startRootSpan', name, fn, options, true);
}
