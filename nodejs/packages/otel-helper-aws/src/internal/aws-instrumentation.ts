/**
 * The `Instrumentation` returned by `awsInstrumentation()` and the
 * per-process state it shares, on the core
 * `internal/contrib` registry as `-sql` and `-redis`.
 *
 * One per process: the contrib instrumentation is built once and shared by
 * every result, and so are its enable state and bindings. Pass one
 * `awsInstrumentation()` to each `setup()`.
 */
import { join } from 'node:path';
import type { Span } from '@opentelemetry/api';
import type { Instrumentation } from '@opentelemetry/instrumentation';
import type * as AwsSdk from '@opentelemetry/instrumentation-aws-sdk';
import { contribLoadError, contribRegistry } from '@staffops/otel-helper/internal/contrib';
import { dbTracerProvider, legacyKeysMode } from '@staffops/otel-helper/internal/db';
import { awsErrorType, errorRequestId, sanitizeAttributes } from './attributes';

export const NAME = '@staffops/otel-helper-aws';
/** Version of this package; a test keeps it equal to package.json `version`. */
export const VERSION = '0.3.0-dev.0';

/** The one library: the AWS SDK for JavaScript v3 (every `@aws-sdk/client-*`). */
export const LIBRARIES = ['aws-sdk'] as const;
export type Library = (typeof LIBRARIES)[number];

/** The contrib module, which is also its `instrumentationName`. */
export const CONTRIB: Readonly<Record<Library, string>> = Object.freeze({ 'aws-sdk': '@opentelemetry/instrumentation-aws-sdk' });

/**
 * Packages that count as the SDK being present: the ones the contrib patches
 * (`Client.prototype.send`). SDK v3 clients since 2025 take `Client` from
 * `@smithy/core` (`client` submodule), older ones from `@smithy/smithy-client`
 * (`@aws-sdk/smithy-client` before 3.300).
 */
export const PACKAGES: Readonly<Record<Library, readonly string[]>> = Object.freeze({
  'aws-sdk': Object.freeze(['@smithy/core', '@smithy/smithy-client', '@aws-sdk/smithy-client']),
});

/**
 * Files the contrib patches in place, required again on the first enable when
 * they are cached (core `internal/contrib`): the `send` patch alone
 * instruments a client built before setup() (it patches the client's
 * middleware stack on each call).
 */
const PATCHED_FILES: Readonly<Record<Library, readonly (readonly [string, string])[]>> = {
  'aws-sdk': [
    ['@smithy/core', join('dist-cjs', 'submodules', 'client', 'index.js')],
    ['@smithy/smithy-client', ''],
    ['@aws-sdk/smithy-client', ''],
  ],
};

/**
 * The contrib's `exceptionHook`, run after it set the ERROR status (its
 * description dropped by the tracer wrapper) and recorded the exception
 * event: `error.type` is the AWS error code when it has the code shape, else
 * `_OTHER`; the request id of the failed call when the contrib found none.
 */
export function onException(span: Span, _info: unknown, err: unknown): void {
  span.setAttribute('error.type', awsErrorType(err));
  const requestId = errorRequestId(err);
  const recorded = (span as { readonly attributes?: Readonly<Record<string, unknown>> }).attributes?.['aws.request_id'];
  if (requestId !== undefined && recorded === undefined) span.setAttribute('aws.request_id', requestId);
}

function build(library: Library): Instrumentation {
  let loaded: typeof AwsSdk;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports -- loaded on demand, so a failure is reported in D14 words
    loaded = require('@opentelemetry/instrumentation-aws-sdk') as typeof AwsSdk;
  } catch (err) {
    throw contribLoadError('AWS', NAME, CONTRIB[library], err);
  }
  // No `dynamoDBStatementSerializer` (no `db.query.text`: it would serialize
  // the request input), no payload parsing for SQS context, and the HTTP
  // requests the SDK makes are not traced again under the SDK span (Python
  // botocore `suppress_http_instrumentation`; Go `otelaws` is middleware on
  // an untraced HTTP client).
  return new loaded.AwsInstrumentation({
    suppressInternalInstrumentation: true,
    sqsExtractContextPropagationFromPayload: false,
    exceptionHook: onException,
  });
}

/**
 * The per-process state: the shared contrib instance, counted enables,
 * bindings, the re-require of preloaded files and the one-time reports.
 */
export const registry = contribRegistry<Library>({
  name: NAME,
  version: VERSION,
  libraries: LIBRARIES,
  contrib: CONTRIB,
  packages: PACKAGES,
  build,
  patchedFiles: PATCHED_FILES,
  // ERROR without description and `error.type` (contract A25 extended to AWS,
  // design D-JS14), the legacy `db.system` on DynamoDB spans (D-JS9), and the
  // attribute rules of `attributes.ts`.
  tracerProvider: (_library, provider) => dbTracerProvider(provider, { legacyKeys: legacyKeysMode(), sanitizeAttributes }),
  // The only contrib metrics are Bedrock's gen_ai ones, re-created on each
  // bind and held only by the contrib (older instruments are dropped): bound as is.
  meterProvider: (_library, provider) => provider,
});

/** Test seam: the contrib instrumentation of a result (never part of the API). */
export const childrenForTests = (instrumentation: unknown): readonly Instrumentation[] => registry.children(instrumentation);
