/**
 * `@staffops/otel-helper-aws` — AWS SDK for JavaScript v3 instrumentation for
 * `@staffops/otel-helper` (opt-in, spec JS8, design D-JS9). Go
 * `ext/otelaws`, Python `staffops_otel_helper.ext.aws`.
 *
 * ```js
 * const { setup } = require('@staffops/otel-helper');
 * const { awsInstrumentation } = require('@staffops/otel-helper-aws');
 * setup({ instrumentations: [awsInstrumentation()] });
 * const { S3Client } = require('@aws-sdk/client-s3'); // after setup(): every send() produces a span
 * ```
 *
 * Wraps the official contrib instrumentation
 * `@opentelemetry/instrumentation-aws-sdk` (every `@aws-sdk/client-*` v3
 * client). One CLIENT (SQS send: PRODUCER, receive: CONSUMER; SNS Publish:
 * PRODUCER) span per `send()`, retries included, named `<Service>.<Operation>`
 * or the messaging/Lambda name (`<queue> send`, `<function> Invoke`). Spans
 * carry `rpc.system=aws-api`, `rpc.service`, `rpc.method`, `cloud.region`
 * and `aws.region`, `aws.request_id` (and the contrib's `aws.request.id`),
 * `http.response.status_code`, plus the service attributes of S3 (bucket),
 * DynamoDB (`db.system.name=dynamodb` and, until 1.0, the legacy `db.system`;
 * table names, operation), SQS/SNS (messaging system, destination, message
 * id; trace context injected in message attributes), Lambda (`faas.*`). The
 * HTTP requests the SDK makes are not traced again (as Go and Python).
 *
 * Never recorded: credentials (access key, secret, session token, signature),
 * request and response bodies (object contents, item values, message bodies,
 * Lambda payloads; DynamoDB `ItemCollectionMetrics` keys), an SNS phone
 * number in attributes or the span name (`phone_number:**`, as Python), and
 * the error text in the span status (contract D1 row "DB error status", extended to AWS by design
 * D-JS14): `error.type` is the AWS error code (`NoSuchKey`,
 * `ThrottlingException`) or `_OTHER`. The semconv `exception` event keeps the
 * AWS error message, as for SQL and Redis (a recorded residual): an SNS
 * `InvalidParameter` error quotes the phone number there. A streamed
 * response body (S3 `GetObject`) emits its events in the SDK's suppressed
 * HTTP context: to trace a call made in its listeners, save
 * `context.active()` before reading and wrap it in `context.with(saved, …)`.
 *
 * One per process: every result shares the same contrib instrumentation, its
 * enable state and bindings (`internal/aws-instrumentation.ts`). Pass one
 * `awsInstrumentation()` to each `setup()`; do not also pass a raw
 * `AwsInstrumentation` (`setup()` rejects it: it would patch twice and record
 * what this package drops).
 */
import type { Instrumentation } from '@opentelemetry/instrumentation';
import { legacyKeysMode } from '@staffops/otel-helper/internal/db';
import { registry, type Library } from './internal/aws-instrumentation';

export { VERSION } from './internal/aws-instrumentation';

/**
 * Options of {@link awsInstrumentation}: none yet (the contrib configuration
 * is fixed by this package). Any key is rejected, so a future option cannot
 * be silently ignored by an older version.
 */
export type AwsInstrumentationOptions = Readonly<Record<string, never>>;

/** What {@link awsInstrumentation} returns: one `Instrumentation` covering the AWS SDK. */
export interface AwsInstrumentation extends Instrumentation {
  /** The libraries this instance instruments (`aws-sdk`). */
  readonly libraries: readonly Library[];
  /** The contrib instrumentation names it covers (`setup()` rejects another instrumentation for them). */
  readonly coveredInstrumentations: readonly string[];
}

function typeName(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function checkOptions(options: unknown): void {
  if (options === undefined) return;
  const proto: unknown = typeof options === 'object' && options !== null && !Array.isArray(options) ? Object.getPrototypeOf(options) : undefined;
  if (proto !== Object.prototype && proto !== null) throw new TypeError(`awsInstrumentation(): options must be an object, got ${typeName(options)}`);
  const o = options as Record<string, unknown>;
  const extra = Object.keys(o).filter((k) => o[k] !== undefined);
  if (extra.length > 0) throw new TypeError(`awsInstrumentation(): options has unknown key(s) ${extra.sort().join(', ')}`);
}

/**
 * AWS SDK v3 instrumentation for `setup({ instrumentations: [awsInstrumentation()] })`.
 * Call it — and `setup()` — before the app loads an `@aws-sdk/client-*`
 * package (a client loaded or built earlier is still instrumented on the
 * first enable). The first call of this, `sqlInstrumentation()` or
 * `redisInstrumentation()` reads `OTEL_SEMCONV_STABILITY_OPT_IN` for the
 * whole process (the DynamoDB `db.system` key).
 *
 * @throws TypeError on invalid options; Error (D14 wording) when the contrib
 *   instrumentation does not load. An SDK that does not resolve from the app
 *   is not an error (a `diag` debug line on the first enable).
 */
export function awsInstrumentation(options?: AwsInstrumentationOptions): AwsInstrumentation {
  checkOptions(options);
  legacyKeysMode();
  return registry.instrument({});
}
