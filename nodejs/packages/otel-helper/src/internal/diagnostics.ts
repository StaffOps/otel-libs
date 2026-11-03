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

/**
 * `value` cut to {@link MAX_ECHOED_VALUE} code points plus `…` when longer
 * (by code point, so a surrogate pair is never split).
 */
export function truncateValue(value: string): string {
  if (value.length <= MAX_ECHOED_VALUE) return value;
  const points = Array.from(value);
  return points.length > MAX_ECHOED_VALUE ? `${points.slice(0, MAX_ECHOED_VALUE).join('')}…` : value;
}

// Bounded scans only: the input is cut first (the output keeps 64 code points),
// and every name part is length-bounded, so no input backtracks quadratically.
const REDACT_SCAN_LIMIT = 4096;
// URLs in free text: the scheme and authority; userinfo, query and fragment are cut below.
const URL_IN_TEXT = /(?<![A-Za-z0-9])([a-z][a-z0-9+.-]{0,31}:\/\/)([^\s"'<>]*)/gi;
// Schemeless `user:password@host`.
const SCHEMELESS_USERINFO = /(?<![^\s"'(=,;])[^\s/@:"']{1,256}:[^\s/@]{1,256}@/g;
// `name: value` / `name=value` pairs whose name looks like a credential (optionally quoted).
const SECRET_PAIR =
  /(?<![A-Za-z0-9])("?)([\w-]{0,64}(?:key|token|secret|passw(?:or)?d|auth|cookie|team)[\w-]{0,64})\1(\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;]+)/gi;
const BEARER = /(?<![A-Za-z0-9])(bearer)(\s*[:=]?\s*)[A-Za-z0-9._~+/=-]+/gi;
// `basic` only before a base64 token of 8+ characters: prose ("basic auth failed") stays.
const BASIC = /(?<![A-Za-z0-9])(basic)(\s+)[A-Za-z0-9+/]{8,}={0,2}(?![A-Za-z0-9+/=])/gi;

/** `rest` of a URL after `scheme://` without userinfo, query and fragment (a path holding `@` is dropped). */
function redactAuthority(rest: string): string {
  // `dns:///user:pw@host`: an empty authority, the host in the first path segment.
  const lead = rest.startsWith('/') ? '/' : '';
  const body = rest.slice(lead.length);
  const at = body.search(/[/?#]/);
  const authority = at < 0 ? body : body.slice(0, at);
  const tail = at < 0 ? '' : body.slice(at);
  const host = authority.includes('@') ? authority.slice(authority.lastIndexOf('@') + 1) : authority;
  const path = tail.split(/[?#]/, 1)[0] ?? '';
  return `${lead}${host}${path.includes('@') ? '' : path}`;
}

/**
 * An error message made safe to echo (B9 fix round): URL userinfo, query and
 * fragment removed (`dns:///…` keeps its host), schemeless `user:pw@`
 * removed, values of credential-like `name: value` pairs and bearer/basic
 * tokens replaced by `<redacted>`, then cut ({@link truncateValue}).
 */
export function redactErrorText(text: string): string {
  const scanned = text.length > REDACT_SCAN_LIMIT ? text.slice(0, REDACT_SCAN_LIMIT) : text;
  const urls = scanned.replace(URL_IN_TEXT, (_m, scheme: string, rest: string) => `${scheme}${redactAuthority(rest)}`);
  const users = urls.replace(SCHEMELESS_USERINFO, '');
  // Tokens first: `Authorization: Bearer x` then leaves no token to the pair rule.
  const tokens = users
    .replace(BEARER, (_m, kind: string, sep: string) => `${kind}${sep === '' ? ' ' : sep}<redacted>`)
    .replace(BASIC, (_m, kind: string, sep: string) => `${kind}${sep}<redacted>`);
  return truncateValue(tokens.replace(SECRET_PAIR, (_m, quote: string, name: string, sep: string) => `${quote}${name}${quote}${sep}<redacted>`));
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
