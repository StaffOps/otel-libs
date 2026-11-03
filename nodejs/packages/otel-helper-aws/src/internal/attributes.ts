/**
 * What `@staffops/otel-helper-aws` records on top of, or instead of, what
 * `@opentelemetry/instrumentation-aws-sdk` records (task B10c, 2026-11-03;
 * design D-JS14 rows of 2026-11-03).
 */
import type { Attributes } from '@opentelemetry/api';

/**
 * Never recorded: the DynamoDB `ItemCollectionMetrics` response field holds
 * `ItemCollectionKey`, the partition-key value of the written item (data,
 * not a measurement).
 */
export const DROPPED_ATTRIBUTES: ReadonlySet<string> = new Set(['aws.dynamodb.item_collection_metrics']);

/**
 * Semconv keys copied from the contrib's own (the contrib writes
 * `aws.request.id`, `aws.request.extended_id` and `cloud.region`; semconv,
 * Go `otelaws` and Python botocore write `aws.request_id`, Go also
 * `aws.region`; semconv names the S3 host id `aws.extended_request_id`).
 * Both keys are kept.
 */
export const ALIASES: Readonly<Record<string, string>> = Object.freeze({
  'aws.request.id': 'aws.request_id',
  'aws.request.extended_id': 'aws.extended_request_id',
  'cloud.region': 'aws.region',
});

/** The SNS destination of a `Publish` to a phone number, as Python botocore writes it (the number is PII). */
export const REDACTED_PHONE = 'phone_number:**';

const SNS_DESTINATION_KEYS = ['messaging.destination', 'messaging.destination.name'] as const;

/**
 * The attributes actually recorded, from those the contrib passes at span
 * start or to `setAttribute`/`setAttributes` (core `internal/db`
 * `sanitizeAttributes`). Idempotent; returns `attributes` itself when nothing
 * changes.
 *
 * - {@link DROPPED_ATTRIBUTES} are removed;
 * - each {@link ALIASES} key with a value gets its semconv twin (unless set);
 * - an SNS span whose `messaging.destination.name` is neither an ARN nor
 *   `unknown` is a `Publish` to a phone number (the contrib writes
 *   `TopicArn || TargetArn || PhoneNumber || 'unknown'`): both destination
 *   keys become {@link REDACTED_PHONE}. The rule needs `messaging.system`
 *   and the destination **in the same batch**: the contrib's SNS extension
 *   sets them together in the span-start attributes and never through a
 *   later `setAttribute` (instrumentation-aws-sdk 0.79 `services/sns`); a
 *   contrib release that moved them would bypass it (a unit case pins this).
 */
export function sanitizeAttributes(attributes: Attributes): Attributes {
  let out: Attributes | undefined = Object.keys(attributes).some((key) => DROPPED_ATTRIBUTES.has(key))
    ? Object.fromEntries(Object.entries(attributes).filter(([key]) => !DROPPED_ATTRIBUTES.has(key)))
    : undefined;
  const copy = (): Attributes => (out ??= { ...attributes });
  for (const [from, to] of Object.entries(ALIASES)) {
    const value = attributes[from];
    if (value !== undefined && attributes[to] === undefined) copy()[to] = value;
  }
  const destination = attributes['messaging.destination.name'];
  if (attributes['messaging.system'] === 'aws.sns' && typeof destination === 'string' && destination !== 'unknown' && !destination.startsWith('arn:')) {
    for (const key of SNS_DESTINATION_KEYS) if (attributes[key] !== undefined) copy()[key] = REDACTED_PHONE;
  }
  return out ?? attributes;
}

/**
 * AWS error codes are a bounded set of PascalCase identifiers (`NoSuchKey`,
 * `AccessDenied`, `ThrottlingException`); anything else — a dotted query
 * code, a lower-case or over-long value — is not taken.
 */
export const AWS_ERROR_CODE = /^[A-Z][A-Za-z0-9]{1,63}$/;

/**
 * Body-less errors (S3 `HeadObject`): status code → the modeled name, Go's
 * `http.StatusText` with the spaces removed (Go 1.26) — the same fixed table
 * in Python (A27 fix round, 2026-11-03).
 */
export const STATUS_CODES: Readonly<Record<string, string>> = Object.freeze({
   400: 'BadRequest', 401: 'Unauthorized', 402: 'PaymentRequired', 403: 'Forbidden', 404: 'NotFound', 405: 'MethodNotAllowed', 406: 'NotAcceptable',
   407: 'ProxyAuthenticationRequired', 408: 'RequestTimeout', 409: 'Conflict', 410: 'Gone', 411: 'LengthRequired', 412: 'PreconditionFailed',
   413: 'RequestEntityTooLarge', 414: 'RequestURITooLong', 415: 'UnsupportedMediaType', 416: 'RequestedRangeNotSatisfiable', 417: 'ExpectationFailed',
   421: 'MisdirectedRequest', 422: 'UnprocessableEntity', 423: 'Locked', 424: 'FailedDependency', 425: 'TooEarly', 426: 'UpgradeRequired',
   428: 'PreconditionRequired', 429: 'TooManyRequests', 431: 'RequestHeaderFieldsTooLarge', 451: 'UnavailableForLegalReasons',
   500: 'InternalServerError', 501: 'NotImplemented', 502: 'BadGateway', 503: 'ServiceUnavailable', 504: 'GatewayTimeout',
   505: 'HTTPVersionNotSupported', 506: 'VariantAlsoNegotiates', 507: 'InsufficientStorage', 508: 'LoopDetected', 510: 'NotExtended',
   511: 'NetworkAuthenticationRequired',
});

/**
 * The `error.type` of a failed call: the SDK error's `name` (an AWS service
 * exception's name is its error code), else its `Code`, when it matches
 * {@link AWS_ERROR_CODE}; a generic `Error` (a transport failure) gives its
 * Node `code` (`ECONNREFUSED`) when that matches, else `Error`; a body-less
 * service error (`name` `Unknown`, or an all-digit name) gives the
 * {@link STATUS_CODES} name of its HTTP status; otherwise `_OTHER` (semconv
 * fallback).
 */
export function awsErrorType(err: unknown): string {
  if (typeof err === 'object' && err !== null) {
    // A body-less service error (S3 HEAD 403/503): the SDK names it `Unknown`;
    // its HTTP status, through the shared table, is the modeled name.
    if ((err as { name?: unknown }).name === 'Unknown') {
      const status: unknown = (err as { $metadata?: { httpStatusCode?: unknown } }).$metadata?.httpStatusCode;
      const named = typeof status === 'number' ? STATUS_CODES[String(status)] : undefined;
      if (named !== undefined) return named;
    }
    // A generic `Error` (a transport failure) is named by its Node `code` first (ECONNREFUSED).
    const keys = (err as { name?: unknown }).name === 'Error' ? (['code', 'name'] as const) : (['name', 'Code'] as const);
    for (const key of keys) {
      const value: unknown = (err as Record<string, unknown>)[key];
      if (typeof value !== 'string') continue;
      // A body-less error is named by its status code (`403`): the table's name.
      const named = /^[0-9]+$/.test(value) ? STATUS_CODES[value] : value;
      if (named !== undefined && AWS_ERROR_CODE.test(named)) return named;
    }
  }
  return '_OTHER';
}

/** The request id of a failed call (`$metadata.requestId`, which the contrib does not read on errors), if any. */
export function errorRequestId(err: unknown): string | undefined {
  if (typeof err !== 'object' || err === null) return undefined;
  const metadata: unknown = (err as { $metadata?: unknown }).$metadata;
  const id: unknown = typeof metadata === 'object' && metadata !== null ? (metadata as { requestId?: unknown }).requestId : undefined;
  return typeof id === 'string' && id !== '' ? id : undefined;
}
