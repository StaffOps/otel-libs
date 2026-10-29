/**
 * The helper's `DiagLogger` (spec JS3, design D-JS5): OpenTelemetry's `diag`
 * is a no-op until a logger is set, so SDK errors (a failing export, a bad
 * attribute) would vanish. The helper installs one writing to the same sink
 * as its own diagnostics, with `origin: 'diag'` so the parity runner keeps
 * forwarded SDK messages out of `helper_log` (where R8 redaction is asserted).
 */
import { formatWithOptions } from 'node:util';
import { diag, DiagLogLevel, type DiagLogger } from '@opentelemetry/api';
import { report, truncateValue, type DiagnosticValue } from './diagnostics';
import { envValue } from './env';
import type { DiagnosticLevel } from './key';

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
  const level = LEVELS[raw.toUpperCase()];
  if (level !== undefined) return level;
  report('unknown log level', { variable: ENV_LOG_LEVEL, value: truncateValue(raw), using: 'WARN' });
  return DiagLogLevel.WARN;
}

// One line per message: arguments are rendered with a bounded depth, string
// length and array length (SDK messages may carry error objects or payloads),
// and the text goes in an attribute, quoted whenever it holds whitespace or
// control characters, so a newline in it cannot forge or split a record.
const MAX_MESSAGE_STRING = 2048;
const MAX_MESSAGE_ARRAY = 20;
function forward(level: DiagnosticLevel): (message: string, ...args: unknown[]) => void {
  return (message, ...args) => {
    const full = formatWithOptions({ depth: 2, breakLength: Infinity, maxStringLength: MAX_MESSAGE_STRING, maxArrayLength: MAX_MESSAGE_ARRAY }, message, ...args);
    // Top-level string arguments are inserted verbatim by util.format: cut the whole text too.
    const text = full.length > MAX_MESSAGE_STRING ? `${full.slice(0, MAX_MESSAGE_STRING)}… (${String(full.length - MAX_MESSAGE_STRING)} more characters)` : full;
    const attrs: Record<string, DiagnosticValue> = { level, msg: text };
    report('opentelemetry diag', attrs, { origin: 'diag', level });
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
