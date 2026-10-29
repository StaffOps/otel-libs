/**
 * Environment reading shared by the resolver (contract design D1 edge-case
 * rulings): one trim set in every language and one boolean grammar.
 */
import { report, truncateValue, type DiagnosticValue } from './diagnostics';

/**
 * The exact set the helper trims, identical in Go, Python and Node (design D1
 * "Trimmed character set"): not Unicode whitespace.
 */
export const TRIM_CHARS = ' \t\n\r\v\f';

const LEADING = /^[ \t\n\r\v\f]+/;
const TRAILING = /[ \t\n\r\v\f]+$/;

/** Trims {@link TRIM_CHARS} from both ends. */
export function trimWS(value: string): string {
  return value.replace(LEADING, '').replace(TRAILING, '');
}

/** Trimmed value of `name`; unset, empty and whitespace-only all read as `undefined`. */
export function envValue(name: string): string | undefined {
  const raw = process.env[name];
  if (raw === undefined) return undefined;
  const value = trimWS(raw);
  return value === '' ? undefined : value;
}

/** Reports one configuration warning (the resolver passes a deduplicating one). */
export type Warn = (message: string, attrs: Readonly<Record<string, DiagnosticValue>>) => void;

const reportWarning: Warn = (message, attrs) => {
  report(message, attrs);
};

/**
 * Boolean env var: case-insensitive `true`/`false`. Any other non-blank value
 * warns (through `warn`) and reads as `false` — the spec's reading, never
 * "ignored". Unset or blank returns `undefined`. The echoed value is cut by
 * {@link truncateValue}.
 */
export function envBool(name: string, warn: Warn = reportWarning): boolean | undefined {
  const value = envValue(name);
  if (value === undefined) return undefined;
  const lower = value.toLowerCase();
  if (lower === 'true') return true;
  if (lower === 'false') return false;
  warn('unparseable boolean', { variable: name, value: truncateValue(value), using: 'false' });
  return false;
}

/** Whether `value` holds a control character: a code unit < 0x20 or 0x7F (design D1 rulings). */
export function hasControl(value: string): boolean {
  for (let i = 0; i < value.length; i++) {
    const c = value.charCodeAt(i);
    if (c < 0x20 || c === 0x7f) return true;
  }
  return false;
}
