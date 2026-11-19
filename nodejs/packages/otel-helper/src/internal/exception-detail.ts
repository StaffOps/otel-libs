/**
 * Exception detail of HTTP, gRPC and framework spans (ADR-014 decision 4,
 * contract task A37, design D15 amendment of 2026-11-19, coordinator rulings
 * R1 and R2).
 *
 * `bindProviders` hands each core instrumentation a forwarding
 * TracerProvider whose spans — recording or not: the instrumentations call
 * the span API without checking — keep status ERROR without a description
 * and an `exception` event with `exception.type` only (at most one per
 * exception object per span); `grpc.error_message` is never written. Two
 * roles:
 *
 * - server — instrumentation-http and instrumentation-grpc SERVER spans,
 *   every instrumentation-express span: at `end()`, one ERROR record on the
 *   helper's log channel (logger `otel-helper`, design D15 "Channel") in the
 *   span's context, **only for a failed span** (R1): HTTP and express — the
 *   span was set ERROR, and for express the error carries no 4xx
 *   `status` / `statusCode`; gRPC — the status code is one of the six
 *   server-error codes, or the handler failed without a code (the client
 *   sees `UNKNOWN`). The first recorded exception's message, type and stack
 *   (else the dropped description, or instrumentation-grpc's
 *   `grpc.error_message` / `grpc.error_name`, without a stack), a
 *   deterministic `error.type` (R2, review N6: HTTP the 5xx status when
 *   known, else the span's `error.type`, else the exception's type; gRPC the
 *   D16 code name, `UNKNOWN` without a code) and the
 *   span's `http.*` / `rpc.*` attributes; none for a cancellation;
 * - client — instrumentation-http and instrumentation-grpc CLIENT spans,
 *   instrumentation-undici: no record (the server explains the failure).
 *
 * @internal
 */

import { context as apiContext, SpanKind, SpanStatusCode, trace, type Attributes, type Span, type SpanOptions, type SpanStatus, type Tracer, type TracerProvider } from '@opentelemetry/api';
import { reportOnce } from './diagnostics';
import { errorTypeOf, exceptionRecord, FAILURE_LOGGER, isCancellation } from './failure';
import { emitFailureRecord } from './logging';
import { grpcErrorType } from './tracing';

/** How an instrumentation's spans get the server role: by SERVER kind, always (a framework), or never. */
type ServerRole = 'kind' | 'always' | 'never';
/** The server rule a span follows (undefined: the client rule). */
type ServerKind = 'http' | 'express' | 'grpc';

const ROLES: Readonly<Record<string, readonly [ServerRole, ServerKind]>> = {
  '@opentelemetry/instrumentation-http': ['kind', 'http'],
  '@opentelemetry/instrumentation-grpc': ['kind', 'grpc'],
  '@opentelemetry/instrumentation-express': ['always', 'express'],
  '@opentelemetry/instrumentation-undici': ['never', 'http'],
};

/** Span and start attributes copied to a server record (never URL or query keys). */
export const RECORD_KEYS: readonly string[] = [
  'error.type',
  'http.request.method',
  'http.route',
  'http.response.status_code',
  'http.method',
  'http.status_code',
  'rpc.system',
  'rpc.system.name',
  'rpc.service',
  'rpc.method',
  'rpc.grpc.status_code',
  'rpc.response.status_code',
];
const RECORDED = new Set(RECORD_KEYS);
/** The gRPC codes the semantic conventions count as server errors (D16): a server record only for these. */
export const GRPC_SERVER_ERROR_CODES: ReadonlySet<number> = new Set([2, 4, 12, 13, 14, 15]);
/** instrumentation-grpc's message attribute: the peer's or the handler's error text, never written. */
const GRPC_ERROR_MESSAGE = 'grpc.error_message';
const GRPC_ERROR_NAME = 'grpc.error_name';
const GRPC_CODE = 'rpc.grpc.status_code';
const HTTP_STATUS_KEYS = ['http.response.status_code', 'http.status_code'];
/** Distinct exception values remembered per span (the dedupe of the type-only event). */
const MAX_SEEN = 16;

/** The server role of an instrumentation, or undefined when it is not covered. */
export function serverRole(instrumentationName: string): ServerRole | undefined {
  return Object.prototype.hasOwnProperty.call(ROLES, instrumentationName) ? ROLES[instrumentationName]?.[0] : undefined;
}

type Scalar = string | number | boolean;

function scalar(value: unknown): Scalar | undefined {
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' ? value : undefined;
}

function int(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isInteger(value) ? value : undefined;
}

/** The integer `status` / `statusCode` an error carries (express / http-errors), else undefined. */
function errorStatus(error: unknown): number | undefined {
  if (typeof error !== 'object' || error === null) return undefined;
  try {
    const holder = error as { status?: unknown; statusCode?: unknown };
    return int(holder.status) ?? int(holder.statusCode);
  } catch {
    return undefined;
  }
}

function internalFailure(what: string, err: unknown): void {
  let type = 'unknown';
  try {
    type = err instanceof Error ? err.name : typeof err;
  } catch {
    // a hostile value: its type stays unknown
  }
  reportOnce(`a37:${what}`, `could not ${what} on an HTTP or gRPC span`, { reason: type });
}

/**
 * Patches `span` in place with the A37 rule; `server` selects the record
 * rule (undefined: the client rule). Never throws (a frozen span is left as
 * it is).
 */
export function applyExceptionDetail(span: Span, server: ServerKind | undefined, startAttributes?: Attributes): Span {
  const known: Record<string, Scalar> = {};
  const keep = (key: string, value: unknown): void => {
    if (!RECORDED.has(key)) return;
    const v = scalar(value);
    if (v !== undefined) known[key] = v;
  };
  // rpc.grpc.status_code is never taken from the start attributes: a placeholder there, not the call's code.
  for (const [k, v] of Object.entries(startAttributes ?? {})) if (k !== GRPC_CODE) keep(k, v);
  const seen: unknown[] = [];
  let failure: { readonly error: unknown } | undefined;
  let dropped: string | undefined;
  let grpcMessage: string | undefined;
  let grpcName: string | undefined;
  let errored = false;
  let cancelled = false;
  let written = false;
  try {
    const setStatus = span.setStatus.bind(span);
    const setAttribute = span.setAttribute.bind(span);
    const setAttributes = span.setAttributes.bind(span);
    const addEvent = span.addEvent.bind(span);
    const end = span.end.bind(span);

    const httpStatus = (): number | undefined => {
      for (const key of HTTP_STATUS_KEYS) {
        const status = int(known[key]);
        if (status !== undefined) return status;
      }
      return undefined;
    };
    const failed = (): boolean => {
      if (server === 'grpc') {
        const code = int(known[GRPC_CODE]);
        if (code !== undefined && code !== 0) return GRPC_SERVER_ERROR_CODES.has(code);
        return failure !== undefined || grpcMessage !== undefined || errored;
      }
      if (!errored) return false;
      const status = server === 'express' && failure !== undefined ? errorStatus(failure.error) : undefined;
      return !(status !== undefined && status >= 400 && status < 500);
    };
    const recordErrorType = (): string => {
      if (server === 'grpc') {
        const code = int(known[GRPC_CODE]);
        return code !== undefined && code !== 0 ? grpcErrorType(code) : 'UNKNOWN';
      }
      // Review N6: the 5xx status when known, else the span's error.type, else the exception's type.
      const status = httpStatus() ?? (failure === undefined ? undefined : errorStatus(failure.error));
      if (status !== undefined && status >= 500) return String(status);
      const spanType = known['error.type'];
      if (typeof spanType === 'string' && spanType !== '') return spanType;
      return failure === undefined ? '_OTHER' : errorTypeOf(failure.error);
    };

    const write = (): void => {
      if (written || server === undefined || cancelled || !failed()) return;
      written = true;
      try {
        let body: string;
        let attributes: Record<string, Scalar>;
        if (failure !== undefined) {
          ({ body, attributes } = exceptionRecord(failure.error, errorTypeOf(failure.error)));
        } else {
          const message = dropped ?? grpcMessage;
          if (message === undefined || message === '') return;
          body = message;
          attributes = { 'exception.type': grpcName ?? 'Error', 'exception.message': message };
        }
        emitFailureRecord(FAILURE_LOGGER, body, { ...attributes, ...known, 'error.type': recordErrorType() }, trace.setSpan(apiContext.active(), span));
      } catch (err) {
        internalFailure('write an exception record', err);
      }
    };

    span.setStatus = (status: SpanStatus): Span => {
      if (status.code !== SpanStatusCode.ERROR) {
        setStatus(status);
        return span;
      }
      errored = true;
      if (dropped === undefined && typeof status.message === 'string' && status.message !== '') dropped = status.message;
      setStatus({ code: SpanStatusCode.ERROR });
      return span;
    };
    span.recordException = (exception: Parameters<Span['recordException']>[0], time?: Parameters<Span['recordException']>[1]): void => {
      try {
        if (seen.includes(exception)) return;
        if (seen.length < MAX_SEEN) seen.push(exception);
        if (isCancellation(exception)) cancelled = true;
        else failure ??= { error: exception };
        addEvent('exception', { 'exception.type': errorTypeOf(exception) }, time);
      } catch (err) {
        internalFailure('record an exception event', err);
      }
    };
    span.setAttribute = (key: string, value: Parameters<Span['setAttribute']>[1]): Span => {
      if (key === GRPC_ERROR_MESSAGE) {
        const text = scalar(value);
        if (text !== undefined) grpcMessage = String(text);
        return span;
      }
      if (key === GRPC_ERROR_NAME && scalar(value) !== undefined) grpcName = String(value);
      keep(key, value);
      setAttribute(key, value);
      return span;
    };
    span.setAttributes = (attributes: Attributes): Span => {
      const out: Attributes = {};
      for (const [key, value] of Object.entries(attributes)) {
        if (key === GRPC_ERROR_MESSAGE) {
          const text = scalar(value);
          if (text !== undefined) grpcMessage = String(text);
          continue;
        }
        if (key === GRPC_ERROR_NAME && scalar(value) !== undefined) grpcName = String(value);
        keep(key, value);
        out[key] = value;
      }
      setAttributes(out);
      return span;
    };
    span.end = (endTime?: Parameters<Span['end']>[0]): void => {
      write();
      seen.length = 0;
      failure = undefined;
      end(endTime);
    };
  } catch {
    // A frozen span: left as it is.
  }
  return span;
}

/**
 * Forwards every member of `target` not in `own`; methods are bound to
 * `target`, except `constructor` (callers read `tracer.constructor.name`).
 */
export function forwardingProxy<T extends object>(target: T, own: Partial<T>): T {
  return new Proxy(target, {
    get(t, prop) {
      if (Object.prototype.hasOwnProperty.call(own, prop)) return own[prop as keyof T];
      const value: unknown = Reflect.get(t, prop, t);
      return typeof value === 'function' && prop !== 'constructor' ? (value as (...a: unknown[]) => unknown).bind(t) : value;
    },
  });
}

function detailTracer(tracer: Tracer, role: ServerRole, kind: ServerKind): Tracer {
  const serverOf = (options: SpanOptions | undefined): ServerKind | undefined => (role === 'always' || (role === 'kind' && options?.kind === SpanKind.SERVER) ? kind : undefined);
  const startSpan = (name: string, options?: SpanOptions, ctx?: Parameters<Tracer['startSpan']>[2]): Span => applyExceptionDetail(tracer.startSpan(name, options, ctx), serverOf(options), options?.attributes);
  const startActiveSpan = (name: string, ...rest: unknown[]): unknown => {
    // (name, fn) | (name, options, fn) | (name, options, context, fn).
    const fn = rest.pop() as (span: Span) => unknown;
    const options = rest[0] as SpanOptions | undefined;
    const start = tracer as unknown as { startActiveSpan(...args: unknown[]): unknown };
    return start.startActiveSpan(name, ...rest, (span: Span) => fn(applyExceptionDetail(span, serverOf(options), options?.attributes)));
  };
  return forwardingProxy(tracer, { startSpan, startActiveSpan });
}

/**
 * `provider` for one core instrumentation: the tracers of a covered
 * instrumentation (http, undici, express, grpc) apply the A37 rule; any
 * other name gets `provider` itself.
 */
export function exceptionDetailTracerProvider(provider: TracerProvider, instrumentationName: string): TracerProvider {
  const entry = Object.prototype.hasOwnProperty.call(ROLES, instrumentationName) ? ROLES[instrumentationName] : undefined;
  if (entry === undefined) return provider;
  const [role, kind] = entry;
  const getTracer = (...args: Parameters<TracerProvider['getTracer']>): Tracer => detailTracer(provider.getTracer(...args), role, kind);
  return forwardingProxy(provider, { getTracer });
}
