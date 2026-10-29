/**
 * Helper diagnostics sink (spec JS3): one line per record on `process.stderr`,
 * formatted `otelhelper: <message> key=value…`. Never `console.*` (apps patch
 * it) and never `process.emitWarning` (wrong channel). Deprecations and the
 * SDK messages the helper's DiagLogger forwards (origin `diag`) use it too.
 *
 * A custom sink lives on the shared state, so a second copy of the same
 * version reports through the sink the first copy installed.
 */
import { readSlot, stateVersion, type DiagnosticMeta, type DiagnosticSink, type HelperState } from './key';
import { VERSION } from '../version';

/** Value types a diagnostic attribute may carry. Never pass secrets. */
export type DiagnosticValue = string | number | boolean;

// Control characters (C0, DEL, C1), whitespace, line/paragraph separators,
// quotes, `=` and backslashes would let a value forge or split a record, so
// such values are JSON-quoted. JSON.stringify leaves C1 and U+2028/U+2029
// as-is, so they are escaped afterwards.
// eslint-disable-next-line no-control-regex -- matching control characters is the point
const NEEDS_QUOTES = /[\x00-\x1f\x7f-\x9f\s"=\\\u2028\u2029]/;
const UNESCAPED_BY_JSON = /[\x7f-\x9f\u2028\u2029]/g;

/** Longest echoed value in a diagnostic before it is cut (with a trailing `…`). */
export const MAX_ECHOED_VALUE = 64;

/** `value` cut to {@link MAX_ECHOED_VALUE} characters plus `…` when longer. */
export function truncateValue(value: string): string {
  return value.length > MAX_ECHOED_VALUE ? `${value.slice(0, MAX_ECHOED_VALUE)}…` : value;
}

const KEY = /^[A-Za-z0-9_.]+$/;

const defaultSink: DiagnosticSink = (line) => {
  process.stderr.write(`${line}\n`);
};

function formatValue(value: DiagnosticValue): string {
  const text = String(value);
  if (text !== '' && !NEEDS_QUOTES.test(text)) return text;
  return JSON.stringify(text).replace(UNESCAPED_BY_JSON, (ch) => `\\u${ch.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

/**
 * Formats a record the way {@link report} writes it. Attribute keys are code
 * constants; a key outside `[A-Za-z0-9_.]` is a programming error (TypeError).
 */
export function formatDiagnostic(message: string, attrs?: Readonly<Record<string, DiagnosticValue>>): string {
  let line = `otelhelper: ${message}`;
  if (attrs !== undefined) {
    for (const [key, value] of Object.entries(attrs)) {
      if (!KEY.test(key)) throw new TypeError(`invalid diagnostic attribute key: ${JSON.stringify(key)}`);
      line += ` ${key}=${formatValue(value)}`;
    }
  }
  return line;
}

// The sink installed on the shared state of this version, else stderr. Never throws.
function currentSink(): DiagnosticSink {
  const slot = readSlot();
  if (slot.ok && stateVersion(slot.value) === VERSION) {
    try {
      const sink = (slot.value as HelperState).sink;
      if (typeof sink === 'function') return sink;
    } catch {
      // Unreadable sink: fall through to stderr.
    }
  }
  return defaultSink;
}

// Fallback for "once" keys while the shared state is unavailable (inactive copy).
const localWarned = new Set<string>();

function warnedSet(): Set<string> {
  const slot = readSlot();
  if (slot.ok && stateVersion(slot.value) === VERSION) {
    try {
      const warned = (slot.value as HelperState).warned;
      if (warned instanceof Set) return warned;
    } catch {
      // Unreadable: fall through to the module-local set.
    }
  }
  return localWarned;
}

/** Metadata of a helper record when the caller passes none: the helper's own warning. */
const HELPER_WARN: DiagnosticMeta = Object.freeze({ origin: 'helper', level: 'warn' });

/**
 * Writes one diagnostic record to the current sink. `meta` defaults to a
 * helper warning; the DiagLogger (B3) passes `origin: 'diag'` and the SDK
 * message's level. A failing sink never throws into the caller.
 */
export function report(
  message: string,
  attrs?: Readonly<Record<string, DiagnosticValue>>,
  meta: DiagnosticMeta = HELPER_WARN,
): void {
  const line = formatDiagnostic(message, attrs);
  const sink = currentSink();
  try {
    sink(line, meta);
  } catch {
    // A broken custom sink must not break the app; fall back to stderr.
    if (sink !== defaultSink) defaultSink(line, meta);
  }
}

/**
 * {@link report} at most once per process for `key` (design D8: deprecations
 * are warned once, however many times setup runs). The key set lives on the
 * shared state, so every copy of this version shares it. Returns whether it
 * wrote the record.
 */
export function reportOnce(
  key: string,
  message: string,
  attrs?: Readonly<Record<string, DiagnosticValue>>,
  meta: DiagnosticMeta = HELPER_WARN,
): boolean {
  const warned = warnedSet();
  if (warned.has(key)) return false;
  warned.add(key);
  report(message, attrs, meta);
  return true;
}

/** Forgets every "once" key (tests only, through `/testing` `reset()`). */
export function clearReportedOnce(): void {
  warnedSet().clear();
  localWarned.clear();
}
