/**
 * Signal selection (task B4c; spec JS2; design D-JS3 rows
 * `OTEL_{TRACES,METRICS,LOGS}_EXPORTER`, `OTEL_SDK_DISABLED`,
 * `OTEL_METRIC_EXPORT_INTERVAL`; contract design D1 rows "Signal on/off" and
 * "Metrics exporters / interval / exemplars"): which exporters each signal
 * uses, whether the SDK is disabled and the metric export interval. Same
 * rules as the Go helper (`go/config.go` normalizeExporters,
 * resolveMetricExporters) and the Python helper (`config.py`
 * `_parse_exporter_list`, `_resolve_signal_switches`); env values warn and
 * fall back, code values fail fast (R8). Exporter values are never secrets,
 * so they are echoed (truncated).
 */
import { truncateValue } from './diagnostics';
import { envBool, envValue, trimWS, type Warn } from './env';
import type { OtlpPlan } from './otlp';
import type { Signal } from './otlp-settings';

export const ENV_SDK_DISABLED = 'OTEL_SDK_DISABLED';
export const ENV_TRACES_EXPORTER = 'OTEL_TRACES_EXPORTER';
export const ENV_METRICS_EXPORTER = 'OTEL_METRICS_EXPORTER';
export const ENV_LOGS_EXPORTER = 'OTEL_LOGS_EXPORTER';
export const ENV_METRIC_EXPORT_INTERVAL = 'OTEL_METRIC_EXPORT_INTERVAL';
export const ENV_DISABLED_SIGNALS = 'OTEL_HELPER_DISABLED_SIGNALS';

/** Contract default (the SDK default is 60 000). */
export const DEFAULT_EXPORT_INTERVAL_MS = 30_000;
/**
 * Largest metric export interval (contract design D1 row "Metric export
 * interval", A19): 2^31-1 ms, the largest delay `setInterval` honors (a
 * larger one fires after 1 ms). Env values above it are clamped, code values
 * outside [1, it] throw, in every language.
 */
export const MAX_EXPORT_INTERVAL_MS = 2 ** 31 - 1;

/** Exporters of traces and logs. */
export type SpanLogExporterName = 'otlp' | 'console';
/** Exporters of metrics (`prometheus`: the `/metrics` endpoint, task B7). */
export type MetricExporterName = 'otlp' | 'prometheus' | 'console';

const SIGNALS: readonly Signal[] = ['traces', 'metrics', 'logs'];

const EXPORTER_ENV: Readonly<Record<Signal, string>> = {
  traces: ENV_TRACES_EXPORTER,
  metrics: ENV_METRICS_EXPORTER,
  logs: ENV_LOGS_EXPORTER,
};

const ALLOWED: Readonly<Record<Signal, readonly string[]>> = {
  traces: ['otlp', 'console'],
  metrics: ['otlp', 'prometheus', 'console'],
  logs: ['otlp', 'console'],
};

/** Code options this module reads (type-checked and normalized by {@link checkCodeSignals}). */
export interface SignalCodeOptions {
  readonly metricExporters?: readonly string[] | undefined;
  readonly exportIntervalMs?: number | undefined;
  readonly disabledSignals?: readonly string[] | undefined;
}

/** What {@link resolveSignals} returns (frozen lists). */
export interface SignalSelection {
  readonly sdkDisabled: boolean;
  readonly tracesExporters: readonly SpanLogExporterName[];
  readonly metricExporters: readonly MetricExporterName[];
  readonly logsExporters: readonly SpanLogExporterName[];
  readonly metricExportIntervalMs: number;
  /** Where the interval came from: `exportIntervalMs`, the env variable, or `default`. */
  readonly intervalSource: string;
}

/**
 * Parses an `OTEL_<SIGNAL>_EXPORTER` value: comma-separated, trimmed,
 * case-insensitive, duplicates dropped. `logging` is the deprecated spelling
 * of `console` (warned); an unknown value warns and is dropped; `none` with
 * other values warns and wins. Returns `undefined` when unset, blank or no
 * valid value is left (the default applies), `[]` for `none`.
 */
export function parseExporterEnv(signal: Signal, warn: Warn): string[] | undefined {
  const variable = EXPORTER_ENV[signal];
  const raw = envValue(variable);
  if (raw === undefined) return undefined;
  const allowed = ALLOWED[signal];
  const out: string[] = [];
  let none = false;
  for (const part of raw.split(',')) {
    let value = trimWS(part).toLowerCase();
    if (value === '') continue;
    if (value === 'none') {
      none = true;
      continue;
    }
    if (value === 'logging') {
      warn('deprecated exporter value "logging"; use "console"', { variable, value, using: 'console' });
      value = 'console';
    } else if (!allowed.includes(value)) {
      warn('unknown exporter; ignored', { variable, value: truncateValue(value), using: allowed.join(', ') });
      continue;
    }
    if (!out.includes(value)) out.push(value);
  }
  if (none) {
    if (out.length > 0) {
      warn('"none" combined with other exporters; the signal is disabled', { variable, value: truncateValue(raw), using: 'none' });
    }
    return [];
  }
  return out.length === 0 ? undefined : out;
}

function typeName(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function stringList(option: string, value: unknown): string[] {
  if (!Array.isArray(value)) throw new TypeError(`setup(): ${option} must be an array of strings, got ${typeName(value)}`);
  const items = [...(value as unknown[])];
  for (const item of items) {
    if (typeof item !== 'string') throw new TypeError(`setup(): ${option} must be an array of strings, got an item of type ${typeName(item)}`);
  }
  return items as string[];
}

/**
 * Validates and normalizes the code options of this module (R8: fail fast,
 * naming the option) and returns the normalized values to store in the
 * options snapshot. `metricExporters`: `otlp`, `prometheus`, `console` or
 * `none` (trimmed, case-insensitive, blanks skipped, duplicates dropped);
 * `none` alone or an empty array disables metrics; `none` with other values
 * and unknown values (also `logging`, which only the environment accepts)
 * throw. `exportIntervalMs`: an integer in [1, 2^31-1].
 * `disabledSignals`: `traces`, `metrics`, `logs`.
 */
export function checkCodeSignals(code: Readonly<Record<string, unknown>>): Partial<Record<keyof SignalCodeOptions, unknown>> {
  const out: Partial<Record<keyof SignalCodeOptions, unknown>> = {};
  if (code.metricExporters !== undefined) {
    const list: string[] = [];
    let none = false;
    for (const item of stringList('metricExporters', code.metricExporters)) {
      const value = trimWS(item).toLowerCase();
      if (value === '') continue;
      if (value === 'none') {
        none = true;
        continue;
      }
      if (!ALLOWED.metrics.includes(value)) {
        throw new TypeError(
          `setup(): metricExporters has unknown metric exporter ${JSON.stringify(truncateValue(item))}; valid values: ${ALLOWED.metrics.join(', ')}, none`,
        );
      }
      if (!list.includes(value)) list.push(value);
    }
    if (none && list.length > 0) throw new TypeError('setup(): metricExporters: "none" cannot be combined with other metric exporters');
    out.metricExporters = Object.freeze(list);
  }
  const interval = code.exportIntervalMs;
  if (interval !== undefined) {
    if (typeof interval !== 'number' || !Number.isSafeInteger(interval) || interval < 1 || interval > MAX_EXPORT_INTERVAL_MS) {
      throw new TypeError(
        `setup(): exportIntervalMs must be an integer number of milliseconds in [1, ${String(MAX_EXPORT_INTERVAL_MS)}], got ${typeof interval === 'number' ? String(interval) : typeName(interval)}`,
      );
    }
    out.exportIntervalMs = interval;
  }
  if (code.disabledSignals !== undefined) {
    const list: Signal[] = [];
    for (const item of stringList('disabledSignals', code.disabledSignals)) {
      const value = trimWS(item).toLowerCase();
      if (!(SIGNALS as readonly string[]).includes(value)) {
        throw new TypeError(`setup(): disabledSignals has unknown signal ${JSON.stringify(truncateValue(item))}; valid values: ${SIGNALS.join(', ')}`);
      }
      if (!list.includes(value as Signal)) list.push(value as Signal);
    }
    out.disabledSignals = Object.freeze(list);
  }
  return out;
}

// Contract design D1 row "Metric export interval" (A19): ASCII digits, an optional "+".
const INTERVAL = /^\+?[0-9]+$/;

/** `exportIntervalMs` > `OTEL_METRIC_EXPORT_INTERVAL` (positive integer ms) > 30 000. */
function resolveInterval(code: SignalCodeOptions, warn: Warn): { ms: number; source: string } {
  if (code.exportIntervalMs !== undefined) return { ms: code.exportIntervalMs, source: 'exportIntervalMs' };
  const variable = ENV_METRIC_EXPORT_INTERVAL;
  const raw = envValue(variable);
  if (raw === undefined) return { ms: DEFAULT_EXPORT_INTERVAL_MS, source: 'default' };
  const ms = Number(raw);
  if (!INTERVAL.test(raw) || ms < 1) {
    warn('invalid metric export interval (positive integer milliseconds)', {
      variable,
      value: truncateValue(raw),
      using: String(DEFAULT_EXPORT_INTERVAL_MS),
    });
    return { ms: DEFAULT_EXPORT_INTERVAL_MS, source: 'default' };
  }
  if (ms > MAX_EXPORT_INTERVAL_MS) {
    warn('metric export interval above 2147483647 ms; clamped', {
      variable,
      value: truncateValue(raw),
      using: String(MAX_EXPORT_INTERVAL_MS),
    });
    return { ms: MAX_EXPORT_INTERVAL_MS, source: variable };
  }
  return { ms, source: variable };
}

/**
 * Signals disabled by the deprecated `OTEL_HELPER_DISABLED_SIGNALS` (code
 * `disabledSignals` wins, also `[]`). The standard variable of a signal wins
 * over it, and for metrics the code `metricExporters` (contract D1 row
 * "Deprecated OTEL_HELPER_DISABLED_SIGNALS"); an unknown entry warns once per
 * setup. The deprecation itself is warned by the resolver.
 */
function disabledSignals(code: SignalCodeOptions, warn: Warn): ReadonlySet<Signal> {
  if (code.disabledSignals !== undefined) return new Set(code.disabledSignals as Signal[]);
  const out = new Set<Signal>();
  const silent: Warn = () => undefined; // the lists themselves are parsed (and warned) by the caller
  // A standard variable counts as set only when it parsed to a valid entry
  // (`none` included); for metrics the code option wins too (contract D1 row
  // "Deprecated OTEL_HELPER_DISABLED_SIGNALS", A19).
  const chosen = (signal: Signal): boolean =>
    (signal === 'metrics' && code.metricExporters !== undefined) || parseExporterEnv(signal, silent) !== undefined;
  for (const part of (envValue(ENV_DISABLED_SIGNALS) ?? '').split(',')) {
    const value = trimWS(part).toLowerCase();
    if (value === '') continue;
    if (!(SIGNALS as readonly string[]).includes(value)) {
      warn('unknown signal; ignored', { variable: ENV_DISABLED_SIGNALS, value: truncateValue(value), using: SIGNALS.join(', ') });
      continue;
    }
    if (!chosen(value as Signal)) out.add(value as Signal);
  }
  return out;
}

/**
 * Resolves the exporters of every signal, `OTEL_SDK_DISABLED` and the metric
 * export interval. Traces and logs: `OTEL_<SIGNAL>_EXPORTER`, default `otlp`
 * (an `otlp` entry exports nothing while the signal has no endpoint).
 * Metrics: code `metricExporters` > `OTEL_METRICS_EXPORTER` > `otlp` when the
 * metrics endpoint is configured, else `prometheus`; an env `otlp` without an
 * endpoint warns and is dropped, a code one throws (R2). A signal disabled
 * by `OTEL_HELPER_DISABLED_SIGNALS` / `disabledSignals` gets no exporter, and
 * `OTEL_SDK_DISABLED=true` none at all (lists are the effective ones).
 */
export function resolveSignals(code: SignalCodeOptions, otlp: OtlpPlan, warn: Warn): SignalSelection {
  const sdkDisabled = envBool(ENV_SDK_DISABLED, warn) ?? false;
  const disabled = disabledSignals(code, warn);

  let metrics: string[] | undefined;
  if (code.metricExporters !== undefined) {
    if (code.metricExporters.includes('otlp') && otlp.metrics === undefined) {
      throw new TypeError(
        'setup(): metricExporters: metric exporter "otlp" requires an endpoint; set OTEL_EXPORTER_OTLP_ENDPOINT (or metricsEndpoint) or remove "otlp"',
      );
    }
    metrics = [...code.metricExporters];
  } else {
    metrics = parseExporterEnv('metrics', warn);
    if (metrics?.includes('otlp') === true && otlp.metrics === undefined) {
      warn('"otlp" requires an OTLP endpoint (OTEL_EXPORTER_OTLP_ENDPOINT or OTEL_EXPORTER_OTLP_METRICS_ENDPOINT); ignored', {
        variable: ENV_METRICS_EXPORTER,
        value: 'otlp',
        using: 'endpoint-based default',
      });
      metrics = metrics.filter((e) => e !== 'otlp');
      if (metrics.length === 0) metrics = undefined;
    }
  }
  metrics ??= [otlp.metrics !== undefined ? 'otlp' : 'prometheus'];
  const traces = parseExporterEnv('traces', warn) ?? ['otlp'];
  const logs = parseExporterEnv('logs', warn) ?? ['otlp'];
  const interval = resolveInterval(code, warn);

  // Every entry was checked against ALLOWED above.
  const effective = (signal: Signal, list: readonly string[]): readonly string[] =>
    Object.freeze(sdkDisabled || disabled.has(signal) ? [] : [...list]);
  return Object.freeze({
    sdkDisabled,
    tracesExporters: effective('traces', traces) as readonly SpanLogExporterName[],
    metricExporters: effective('metrics', metrics) as readonly MetricExporterName[],
    logsExporters: effective('logs', logs) as readonly SpanLogExporterName[],
    metricExportIntervalMs: interval.ms,
    intervalSource: interval.source,
  });
}
