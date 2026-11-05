/**
 * The helper's `DiagLogger`: OpenTelemetry's `diag`
 * is a no-op until a logger is set, so SDK errors (a failing export, a bad
 * attribute) would vanish. The helper installs one writing to the same sink
 * as its own diagnostics, with `origin: 'diag'` so the parity runner keeps
 * forwarded SDK messages out of `helper_log` (where R8 redaction is asserted).
 */
import { formatWithOptions } from 'node:util';
import { diag, DiagLogLevel, type DiagLogger } from '@opentelemetry/api';
import { maskLiterals, REDACT_SCAN_LIMIT, redactText, report, truncateValue } from './diagnostics';
import { asciiUpper, envValue, trimWS } from './env';
import type { DiagnosticLevel } from './key';
import { otlpEnv, parseHeaders, type Signal } from './otlp-settings';

export const ENV_LOG_LEVEL = 'OTEL_LOG_LEVEL';

const LEVELS: Readonly<Record<string, DiagLogLevel>> = {
  NONE: DiagLogLevel.NONE,
  ERROR: DiagLogLevel.ERROR,
  WARN: DiagLogLevel.WARN,
  INFO: DiagLogLevel.INFO,
  DEBUG: DiagLogLevel.DEBUG,
  VERBOSE: DiagLogLevel.VERBOSE,
  ALL: DiagLogLevel.ALL,
};

/**
 * `OTEL_LOG_LEVEL` (case-insensitive, trimmed): NONE, ERROR, WARN, INFO,
 * DEBUG, VERBOSE, ALL. Unset → WARN (the helper default, not the spec's INFO:
 * the SDK's INFO messages would be noise on every start). Any other value
 * warns and uses WARN.
 */
export function diagLevelFromEnv(): DiagLogLevel {
  const raw = envValue(ENV_LOG_LEVEL);
  if (raw === undefined) return DiagLogLevel.WARN;
  const level = LEVELS[asciiUpper(raw)];
  if (level !== undefined) return level;
  report('unknown log level', { variable: ENV_LOG_LEVEL, value: truncateValue(raw), using: 'WARN' });
  return DiagLogLevel.WARN;
}

/**
 * Whether `OTEL_LOG_LEVEL` asks for debug records (DEBUG, VERBOSE or ALL).
 * Never reports: {@link diagLevelFromEnv} already warns about an unknown
 * value when the DiagLogger is installed. Gates the helper's own debug
 * records, which go to the sink and not through `diag`.
 */
export function debugEnabled(): boolean {
  const raw = envValue(ENV_LOG_LEVEL);
  if (raw === undefined) return false;
  const level = LEVELS[asciiUpper(raw)];
  return level !== undefined && level >= DiagLogLevel.DEBUG;
}

// One line per message: arguments are rendered with a bounded depth, string
// length and array length (SDK messages may carry error objects or payloads),
// and the text goes in an attribute, quoted whenever it holds whitespace or
// control characters, so a newline in it cannot forge or split a record.
// Credentials are kept out in three layers: (1) the response body of an
// exporter error is dropped before rendering (a collector or proxy may echo the
// request, headers included, and an echoed header name can be anything);
// (2) the configured OTLP header values are masked wherever they appear;
// (3) the text is redacted by `redactText` (URL userinfo, tokens,
// credential-named `key=value` pairs) as in the helper's own error attributes.
const MAX_MESSAGE_STRING = 2048;
const MAX_MESSAGE_ARRAY = 20;
// Characters of a message redacted (twice what a record keeps; within redactText's scan).
const SCANNED_FOR_MESSAGE = Math.min(MAX_MESSAGE_STRING * 2, REDACT_SCAN_LIMIT);
const SIGNALS: readonly Signal[] = ['traces', 'metrics', 'logs'];

/** Placeholder for a dropped response body (its size only). */
function omittedBody(body: unknown): string {
  const size = typeof body === 'string' ? `${String(body.length)} characters` : body instanceof Uint8Array ? `${String(body.byteLength)} bytes` : 'non-string';
  return `<response body omitted: ${size}>`;
}

// An OTLP exporter error (`OTLPExporterError`: `message`, `code`, `data` = the
// HTTP response body), or any Error carrying a `data` field.
function isErrorWithBody(value: unknown): value is Error & { data: unknown; code?: unknown } {
  return value instanceof Error && 'data' in value && (value as { data?: unknown }).data != null;
}

// An Error with a body, rendered without the body (name, message, code).
function errorWithoutBody(error: Error & { data: unknown; code?: unknown }): string {
  const code = typeof error.code === 'string' || typeof error.code === 'number' ? ` (code ${String(error.code)})` : '';
  return `${error.name}: ${error.message}${code} ${omittedBody(error.data)}`;
}

/**
 * One diag argument made safe to render: a string is passed through
 * {@link messageWithoutResponseBody}, an Error with a response body loses
 * the body, a byte buffer (a raw response) is replaced by its size, and a
 * plain object or array holding such a value directly gets a shallow copy with
 * it replaced. Anything else is returned as is.
 */
export function withoutResponseBodies(arg: unknown): unknown {
  if (typeof arg === 'string') return messageWithoutResponseBody(arg);
  if (isErrorWithBody(arg)) return errorWithoutBody(arg);
  if (arg instanceof Uint8Array) return omittedBody(arg);
  if (Array.isArray(arg)) return arg.some((v) => isErrorWithBody(v) || v instanceof Uint8Array) ? arg.map(shallow) : arg;
  if (arg !== null && typeof arg === 'object' && Object.getPrototypeOf(arg) === Object.prototype) {
    const entries = Object.entries(arg);
    if (!entries.some(([, v]) => isErrorWithBody(v) || v instanceof Uint8Array)) return arg;
    return Object.fromEntries(entries.map(([k, v]) => [k, shallow(v)]));
  }
  return arg;
}

function shallow(value: unknown): unknown {
  if (isErrorWithBody(value)) return errorWithoutBody(value);
  if (value instanceof Uint8Array) return omittedBody(value);
  return value;
}

/**
 * A diag message string made safe: the SDK's global error handler logs an
 * exporter error as `JSON.stringify` of its flattened properties (`stack`,
 * `message`, `code`, `name`, `data`); when the message is such a JSON object
 * with a `data` field, the field is replaced by its size.
 */
export function messageWithoutResponseBody(message: string): string {
  if (!message.startsWith('{') || !message.includes('"data"')) return message;
  let parsed: unknown;
  try {
    parsed = JSON.parse(message);
  } catch {
    return message;
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return message;
  const record = parsed as Record<string, unknown>;
  if (record.data == null || (typeof record.stack !== 'string' && typeof record.name !== 'string')) return message;
  return JSON.stringify({ ...record, data: omittedBody(record.data) });
}

const HEADER_VARIABLES: readonly string[] = [undefined, ...SIGNALS].map((signal) => otlpEnv(signal, 'HEADERS'));
let headerCache:
  | { readonly raws: readonly (string | undefined)[]; readonly captured: ReadonlySet<string>; readonly values: ReadonlySet<string> }
  | undefined;
// The values the exporters were built with (captureHeaderValues, at setup).
let captured: ReadonlySet<string> = new Set();

function valuesOf(raws: readonly (string | undefined)[]): Set<string> {
  const values = new Set<string>();
  for (const raw of raws) {
    if (raw === undefined) continue;
    for (const value of Object.values(parseHeaders(raw).headers)) values.add(value);
    for (const item of raw.split(',')) {
      const eq = item.indexOf('=');
      if (eq >= 0) values.add(trimWS(item.slice(eq + 1))); // the contract trim set (code-review L12)
    }
  }
  return values;
}

/**
 * Records the header values setup builds the exporters with: they stay
 * masked after the application changes or deletes the variables, because
 * the exporters keep sending them (A29 final round, dev R2-M1).
 */
export function captureHeaderValues(): void {
  captured = valuesOf(HEADER_VARIABLES.map((variable) => process.env[variable]));
}

/** Forgets the captured values (`testing.reset()`). */
export function resetCapturedHeaderValues(): void {
  captured = new Set();
}

/**
 * The union of the values captured at setup and the configured OTLP header
 * values now (`OTEL_EXPORTER_OTLP_HEADERS` and the per-signal variables), raw
 * and decoded: they are masked literally in forwarded text. Never logged.
 * Rebuilt only when the raw variables or the captured set changed.
 */
export function configuredHeaderValues(): ReadonlySet<string> {
  const raws = HEADER_VARIABLES.map((variable) => process.env[variable]);
  if (headerCache?.captured === captured && raws.every((raw, i) => raw === headerCache?.raws[i])) return headerCache.values;
  const values = new Set<string>([...captured, ...valuesOf(raws)]);
  values.delete('');
  headerCache = { raws, captured, values };
  return values;
}

/** The `msg` of a forwarded record whose arguments could not be rendered (a throwing getter, `inspect.custom` or `toString`, a revoked Proxy). */
export const UNRENDERABLE_MESSAGE = '<diag message could not be rendered>';

/**
 * The safe `msg` text of one SDK diag call: response bodies dropped, rendered,
 * configured header values masked, redacted, then cut at 2048 characters (the
 * cut comes last, so a quoted value is never cut open before redaction).
 */
export function renderDiagMessage(message: unknown, args: readonly unknown[]): string {
  const full = formatWithOptions(
    { depth: 2, breakLength: Infinity, maxStringLength: MAX_MESSAGE_STRING, maxArrayLength: MAX_MESSAGE_ARRAY },
    withoutResponseBodies(message),
    ...args.map(withoutResponseBodies),
  );
  const masked = maskLiterals(full, configuredHeaderValues());
  // Only text that is scanned is ever written: twice the output length is
  // redacted (a quoted value cut open by this slice is redacted to its end),
  // and the record keeps the first 2048 characters of that.
  const scanned = masked.slice(0, SCANNED_FOR_MESSAGE);
  const redacted = redactText(scanned);
  if (redacted.length <= MAX_MESSAGE_STRING && scanned.length === masked.length) return redacted;
  // Top-level string arguments are inserted verbatim by util.format: cut the whole text too.
  // Omitted: the redacted characters past the cut plus the characters never scanned.
  const omitted = Math.max(0, redacted.length - MAX_MESSAGE_STRING) + (masked.length - scanned.length);
  return `${redacted.slice(0, MAX_MESSAGE_STRING)}… (${String(omitted)} more characters)`;
}

function forward(level: DiagnosticLevel): (message: string, ...args: unknown[]) => void {
  return (message, ...args) => {
    // Never throws into the SDK, and always writes exactly one record.
    let msg: string;
    try {
      msg = renderDiagMessage(message, args);
    } catch {
      msg = UNRENDERABLE_MESSAGE;
    }
    report('opentelemetry diag', { level, msg }, { origin: 'diag', level });
  };
}

/** The logger instance the helper installs (one per module instance). */
export const HELPER_DIAG_LOGGER: DiagLogger = Object.freeze({
  error: forward('error'),
  warn: forward('warn'),
  info: forward('info'),
  debug: forward('debug'),
  verbose: forward('debug'),
});

/**
 * Installs {@link HELPER_DIAG_LOGGER} at the `OTEL_LOG_LEVEL` level.
 * `reinstall` suppresses the API's "logger overridden" warning, written when
 * the helper replaces the logger it installed itself (setup after shutdown or
 * reset). When another logger was set earlier, the API warns on both
 * loggers (documented, design D-JS5).
 */
export function installDiagLogger(reinstall: boolean): void {
  diag.setLogger(HELPER_DIAG_LOGGER, { logLevel: diagLevelFromEnv(), suppressOverrideMessage: reinstall });
}
