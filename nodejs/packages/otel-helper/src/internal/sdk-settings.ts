/**
 * SDK settings the OpenTelemetry JS SDK 3.0 no longer reads from the
 * environment (spec JS2; design D-JS3 row "`OTEL_BSP_*`, `OTEL_BLRP_*`,
 * limits"): batch span / log record processor settings and attribute, span
 * and log record limits, with the OpenTelemetry specification defaults (the
 * Go and Python SDKs read the same variables themselves). Invalid values warn
 * and use the default; they are never secrets, so the value is echoed
 * (truncated).
 *
 * Contract rulings (design D1 rows dated 2026-10-29, task A18):
 * `OTEL_BSP_EXPORT_TIMEOUT`, `OTEL_BLRP_EXPORT_TIMEOUT` and
 * `OTEL_METRIC_EXPORT_TIMEOUT` are not honored (the resolved OTLP timeout is
 * the single export deadline) and warn when set; a value-length limit of 0 is
 * invalid (warn, next level, else no limit).
 */
import { truncateValue } from './diagnostics';
import { trimWS, type Warn } from './env';
import { MAX_TIMEOUT_MS } from './otlp-settings';

/** Batch processor settings (milliseconds and counts). */
export interface BatchSettings {
  readonly scheduledDelayMillis: number;
  readonly maxQueueSize: number;
  readonly maxExportBatchSize: number;
}

/** Span limits (`TracerProvider` `spanLimits`). */
export interface SpanLimitSettings {
  readonly attributeValueLengthLimit: number;
  readonly attributeCountLimit: number;
  readonly eventCountLimit: number;
  readonly linkCountLimit: number;
  readonly attributePerEventCountLimit: number;
  readonly attributePerLinkCountLimit: number;
}

/** Log record limits (`LoggerProvider` `logRecordLimits`). */
export interface LogRecordLimitSettings {
  readonly attributeValueLengthLimit: number;
  readonly attributeCountLimit: number;
}

/** Everything this module resolves. */
export interface SdkSettings {
  readonly bsp: BatchSettings;
  readonly blrp: BatchSettings;
  readonly spanLimits: SpanLimitSettings;
  readonly logRecordLimits: LogRecordLimitSettings;
}

/** Export-timeout variables of the SDK processors and reader that the helper does not honor (A18). */
export const IGNORED_TIMEOUT_VARIABLES: readonly string[] = ['OTEL_BSP_EXPORT_TIMEOUT', 'OTEL_BLRP_EXPORT_TIMEOUT', 'OTEL_METRIC_EXPORT_TIMEOUT'];

const INTEGER = /^[0-9]+$/;

interface IntRule {
  /** Smallest accepted value (0 or 1). */
  readonly min: number;
  /** Values above it are clamped with a warning (durations: 24 h, the Node timer limit). */
  readonly clampAbove?: number;
}

const DURATION: IntRule = { min: 0, clampAbove: MAX_TIMEOUT_MS };
const POSITIVE: IntRule = { min: 1 };
const NON_NEGATIVE: IntRule = { min: 0 };

/**
 * A non-negative decimal integer variable; `undefined` when unset or blank.
 * Garbage or a value below `rule.min` warns and reads as `undefined` (the
 * caller's next level); a value above `rule.clampAbove` is clamped with a warning.
 */
function envInt(variable: string, rule: IntRule, warn: Warn): number | undefined {
  const raw = trimWS(process.env[variable] ?? '');
  if (raw === '') return undefined;
  const n = Number(raw);
  if (!INTEGER.test(raw) || !Number.isSafeInteger(n) || n < rule.min) {
    const grammar = rule.min === 0 ? 'non-negative integer' : 'positive integer';
    warn(`invalid value (${grammar}); the next level or the default is used`, { variable, value: truncateValue(raw), using: 'default' });
    return undefined;
  }
  if (rule.clampAbove !== undefined && n > rule.clampAbove) {
    warn('value above 24 h; clamped', { variable, value: truncateValue(raw), using: String(rule.clampAbove) });
    return rule.clampAbove;
  }
  return n;
}

function batch(prefix: 'OTEL_BSP' | 'OTEL_BLRP', defaultDelay: number, warn: Warn): BatchSettings {
  const maxQueueSize = envInt(`${prefix}_MAX_QUEUE_SIZE`, POSITIVE, warn) ?? 2048;
  let maxExportBatchSize = envInt(`${prefix}_MAX_EXPORT_BATCH_SIZE`, POSITIVE, warn) ?? 512;
  if (maxExportBatchSize > maxQueueSize) {
    // Contract D1 ruling "Batch size above queue size" (A18): warn naming both variables, use the queue size.
    warn('the export batch size is larger than the queue size; using the queue size', {
      variable: `${prefix}_MAX_EXPORT_BATCH_SIZE,${prefix}_MAX_QUEUE_SIZE`,
      value: String(maxExportBatchSize),
      using: String(maxQueueSize),
    });
    maxExportBatchSize = maxQueueSize;
  }
  return Object.freeze({
    scheduledDelayMillis: envInt(`${prefix}_SCHEDULE_DELAY`, DURATION, warn) ?? defaultDelay,
    maxQueueSize,
    maxExportBatchSize,
  });
}

/**
 * Reads the batch-processor and limit variables with the specification
 * defaults: BSP delay 5000 ms, BLRP delay 1000 ms, queue 2048, batch 512;
 * attribute count 128, value length unlimited; span event/link counts and
 * attributes per event/link 128. The signal-specific limit variables fall
 * back to `OTEL_ATTRIBUTE_COUNT_LIMIT` / `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`.
 * A value-length limit must be positive (0 warns: next level, else none).
 * Each ignored export-timeout variable that is set warns.
 */
export function resolveSdkSettings(warn: Warn): SdkSettings {
  for (const variable of IGNORED_TIMEOUT_VARIABLES) {
    if (trimWS(process.env[variable] ?? '') !== '') {
      warn('not honored: the OTLP export timeout is the export deadline of every signal; set OTEL_EXPORTER_OTLP_TIMEOUT or OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT', {
        variable,
      });
    }
  }
  const attrCount = envInt('OTEL_ATTRIBUTE_COUNT_LIMIT', NON_NEGATIVE, warn) ?? 128;
  const attrLength = envInt('OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT', POSITIVE, warn) ?? Infinity;
  return Object.freeze({
    bsp: batch('OTEL_BSP', 5000, warn),
    blrp: batch('OTEL_BLRP', 1000, warn),
    spanLimits: Object.freeze({
      attributeValueLengthLimit: envInt('OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT', POSITIVE, warn) ?? attrLength,
      attributeCountLimit: envInt('OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT', NON_NEGATIVE, warn) ?? attrCount,
      eventCountLimit: envInt('OTEL_SPAN_EVENT_COUNT_LIMIT', NON_NEGATIVE, warn) ?? 128,
      linkCountLimit: envInt('OTEL_SPAN_LINK_COUNT_LIMIT', NON_NEGATIVE, warn) ?? 128,
      attributePerEventCountLimit: envInt('OTEL_EVENT_ATTRIBUTE_COUNT_LIMIT', NON_NEGATIVE, warn) ?? 128,
      attributePerLinkCountLimit: envInt('OTEL_LINK_ATTRIBUTE_COUNT_LIMIT', NON_NEGATIVE, warn) ?? 128,
    }),
    logRecordLimits: Object.freeze({
      attributeValueLengthLimit: envInt('OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT', POSITIVE, warn) ?? attrLength,
      attributeCountLimit: envInt('OTEL_LOGRECORD_ATTRIBUTE_COUNT_LIMIT', NON_NEGATIVE, warn) ?? attrCount,
    }),
  });
}
