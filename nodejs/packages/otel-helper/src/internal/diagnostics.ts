/**
 * Helper diagnostics sink (spec JS3): one line per record on `process.stderr`,
 * formatted `otelhelper: <message> key=value…`. Never `console.*` (apps patch
 * it) and never `process.emitWarning` (wrong channel). Task B3 extends this
 * sink (deprecations, the default DiagLogger).
 *
 * A custom sink lives on the shared state, so a second copy of the same
 * version reports through the sink the first copy installed.
 */
import { readSlot, stateVersion, type DiagnosticSink, type HelperState } from './key';
import { VERSION } from '../version';

/** Value types a diagnostic attribute may carry. Never pass secrets. */
export type DiagnosticValue = string | number | boolean;

// Control characters, whitespace, quotes, `=` and backslashes would let a value
// forge or split a record, so such values are JSON-quoted (which escapes them).
// eslint-disable-next-line no-control-regex -- matching control characters is the point
const NEEDS_QUOTES = /[\x00-\x1f\x7f\s"=\\]/;
const KEY = /^[A-Za-z0-9_.]+$/;

const defaultSink: DiagnosticSink = (line) => {
  process.stderr.write(`${line}\n`);
};

function formatValue(value: DiagnosticValue): string {
  const text = String(value);
  return text === '' || NEEDS_QUOTES.test(text) ? JSON.stringify(text) : text;
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

/** Writes one diagnostic record to the current sink. A failing sink never throws into the caller. */
export function report(message: string, attrs?: Readonly<Record<string, DiagnosticValue>>): void {
  const line = formatDiagnostic(message, attrs);
  const sink = currentSink();
  try {
    sink(line);
  } catch {
    // A broken custom sink must not break the app; fall back to stderr.
    if (sink !== defaultSink) defaultSink(line);
  }
}
