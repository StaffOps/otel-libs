/**
 * Tracer/meter accessors and `startRootSpan()`: the Node counterparts of Go
 * `GetTracer`/`GetMeter`/`StartRootSpan` and Python
 * `get_tracer`/`get_meter`/`start_root_span` (design "Layout and public API").
 */
import { context, metrics, SpanKind, SpanStatusCode, trace } from '@opentelemetry/api';
import type { Attributes, Exception, Link, Meter, Span, Tracer } from '@opentelemetry/api';

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

/** `String(value)` that never throws (`Object.create(null)`, a throwing `toString`). */
function safeString(value: unknown): string {
  try {
    return String(value);
  } catch {
    try {
      return Object.prototype.toString.call(value);
    } catch {
      return 'unknown error';
    }
  }
}

/**
 * Records `err` on `span` (exception event + ERROR) and ends it; never throws,
 * so the caller always rethrows the original value. Each step is guarded on
 * its own (fix round 2026-11-03): a throwing `name` or `message` getter, or a
 * failing `recordException`, still leaves the span ERROR and ended. The event
 * gets a plain `{ name, message, stack }`: the SDK prefers an error's `code`
 * for `exception.type` (a gRPC status number, an errno string), the helper
 * writes the error's name, as Go (the Go type) and Python (the class name).
 */
function failAndEnd(span: Span, err: unknown): void {
  let isError = false;
  try {
    isError = err instanceof Error;
  } catch {
    // A Proxy whose getPrototypeOf trap throws: treated as a non-Error value.
  }
  let exception: Exception;
  let description: string;
  if (isError) {
    const e = err as Error;
    let name = 'Error';
    let message = '';
    let stack: string | undefined;
    try {
      name = safeString(e.name);
    } catch {
      // keep 'Error'
    }
    try {
      message = safeString(e.message);
    } catch {
      // keep ''
    }
    try {
      stack = typeof e.stack === 'string' ? e.stack : undefined;
    } catch {
      // no stack
    }
    exception = stack === undefined ? { name, message } : { name, message, stack };
    // Python's start_as_current_span description: "<error name>: <message>".
    description = `${name}: ${message}`;
  } else {
    description = safeString(err);
    exception = description;
  }
  try {
    span.recordException(exception);
  } catch {
    // Recording must never replace the application's own error.
  }
  try {
    span.setStatus({ code: SpanStatusCode.ERROR, message: description });
  } catch {
    // as above
  }
  try {
    span.end();
  } catch {
    // as above
  }
}

/**
 * What {@link startRootSpan} returns: `Promise<U>` for a thenable `fn` result
 * (`PromiseLike<U>`, an async function), the result itself otherwise.
 */
export type RootSpanResult<T> = T extends PromiseLike<infer U> ? Promise<U> : T;

/**
 * Runs `fn(span)` in a new **root** span — a new trace, whatever span is
 * active (Go `StartRootSpan` with `WithNewRoot`, Python `start_root_span`).
 * Use it in workers where each iteration is an independent trace.
 *
 * - The span is active inside `fn`; other context values (baggage) are kept,
 *   as in Go and Python, and the sampler sees no parent (a remote or unsampled
 *   one included), so the root is sampled on its own.
 * - Sync `fn`: the span ends when `fn` returns. A thenable result: the call
 *   returns a real `Promise` that settles once with it, and the span ends then.
 * - A throw, a rejection or a throwing `then` records an exception event
 *   (`exception.type` is the error's `name`, not its `code`) and status ERROR
 *   with the description `"<error name>: <message>"` (the value's string form
 *   for a non-`Error`), then the original value is rethrown.
 * - A promise that never settles leaves the span open (never exported), as an
 *   unclosed span in Go or Python. Work `fn` starts without awaiting it runs
 *   under the span after it ended: its child spans keep the trace, but the
 *   root no longer measures them.
 *
 * @throws TypeError when `name` is not a string or `fn` not a function.
 */
export function startRootSpan<T>(name: string, fn: (span: Span) => T, options: RootSpanOptions = {}): RootSpanResult<T> {
  if (typeof name !== 'string') throw new TypeError(`startRootSpan: name must be a string, got ${typeof name}`);
  if (typeof fn !== 'function') throw new TypeError(`startRootSpan: fn must be a function, got ${typeof fn}`);
  const tracer = options.tracer ?? getTracer();
  const spanOptions = {
    root: true,
    kind: options.kind ?? SpanKind.INTERNAL,
    ...(options.attributes === undefined ? {} : { attributes: options.attributes }),
    ...(options.links === undefined ? {} : { links: options.links }),
  };
  return tracer.startActiveSpan(name, spanOptions, context.active(), (span: Span): RootSpanResult<T> => {
    try {
      const result = fn(span);
      const then: unknown = typeof result === 'object' || typeof result === 'function'
        ? (result as { then?: unknown } | null)?.then
        : undefined;
      if (typeof then !== 'function') {
        span.end();
        return result as RootSpanResult<T>;
      }
      return Promise.resolve(result).then(
        (value: unknown) => {
          span.end();
          return value;
        },
        (err: unknown) => {
          failAndEnd(span, err);
          throw err;
        },
      ) as RootSpanResult<T>;
    } catch (err) {
      failAndEnd(span, err);
      throw err;
    }
  });
}
