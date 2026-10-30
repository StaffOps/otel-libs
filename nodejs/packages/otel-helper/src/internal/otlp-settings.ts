/**
 * Per-signal OTLP export settings the helper owns (task B4b; design D-JS3
 * ownership table; contract design D1 rows "Header parsing details",
 * "Timeout range / syntax", "Env compression", "Fallback order for every
 * per-signal value"): headers, timeout and compression. Same rules as the Go
 * helper (`go/endpoint.go` resolveHeaders / resolveTimeout /
 * resolveCompression) and the A18 rulings (contract design D1 rows dated
 * 2026-10-29: export deadline, code timeout bound, header bytes). Header
 * values are secrets: diagnostics name the variable and the item position
 * only (R8).
 */
import { truncateValue } from './diagnostics';
import { trimWS, type Warn } from './env';

/** The three OTLP signals. */
export type Signal = 'traces' | 'metrics' | 'logs';

/** `OTEL_EXPORTER_OTLP[_<SIGNAL>]_<suffix>`; `signal` undefined = the generic variable. */
export function otlpEnv(signal: Signal | undefined, suffix: string): string {
  return signal === undefined ? `OTEL_EXPORTER_OTLP_${suffix}` : `OTEL_EXPORTER_OTLP_${signal.toUpperCase()}_${suffix}`;
}

/** OTLP compression values (case-insensitive in env and code). */
export type OtlpCompression = 'gzip' | 'none';
export const COMPRESSIONS: readonly OtlpCompression[] = ['gzip', 'none'];
export const DEFAULT_COMPRESSION: OtlpCompression = 'gzip';

/** Default OTLP export timeout (OTLP exporter spec), milliseconds. */
export const DEFAULT_TIMEOUT_MS = 10_000;
export const MIN_TIMEOUT_MS = 1;
/** 24 h: the clamp of the contract, and below `setTimeout`'s 2^31-1 ms limit. */
export const MAX_TIMEOUT_MS = 86_400_000;

// Plain decimal milliseconds: no sign, exponent, hex, underscore or bare dot (D1 ruling).
const DECIMAL_MS = /^[0-9]+(\.[0-9]+)?$/;
// The intersection of HTTP tokens and gRPC metadata keys (D1 ruling).
const HEADER_KEY = /^[A-Za-z0-9._-]+$/;
const BAD_ESCAPE = /%(?![0-9A-Fa-f]{2})/;
// A decoded header value must be printable ASCII (D1 ruling "Header bytes", A18).
const PRINTABLE_ASCII = /^[\x20-\x7e]*$/;

/** Result of {@link parseHeaders}. */
export interface ParsedHeaders {
  /** Valid pairs, keys lowercased, last one wins (null prototype). */
  readonly headers: Readonly<Record<string, string>>;
  /** 1-based position in the env value of the item each key came from (null prototype). */
  readonly positions: Readonly<Record<string, number>>;
  /** 1-based positions of skipped items (no `=`, invalid key, decoded value not printable ASCII). */
  readonly skipped: readonly number[];
  /** 1-based positions of values kept literally (invalid escape or not UTF-8 once decoded). */
  readonly literal: readonly number[];
}

/** Strict percent-decoding of a whole value; `undefined` on a bad escape or a non-UTF-8 result. */
function decodeValue(value: string): string | undefined {
  if (BAD_ESCAPE.test(value)) return undefined;
  try {
    return decodeURIComponent(value);
  } catch {
    return undefined; // URIError: the decoded bytes are not UTF-8
  }
}

/**
 * Parses a W3C-baggage-style `key=value,key2=value2` list (D1 ruling "Header
 * parsing details"): keys must match `^[A-Za-z0-9._-]+$` and are not
 * percent-decoded; values are, as a whole (an invalid escape or a non-UTF-8
 * decode keeps the whole value literal); values are trimmed after decoding; a
 * decoded value that is not printable ASCII (0x20–0x7E: a control character or
 * a non-ASCII character such as a decoded `%C3%A9`) is skipped (A18).
 */
export function parseHeaders(raw: string): ParsedHeaders {
  const headers = Object.create(null) as Record<string, string>;
  const positions = Object.create(null) as Record<string, number>;
  const skipped: number[] = [];
  const literal: number[] = [];
  raw.split(',').forEach((item, i) => {
    const pos = i + 1;
    const eq = item.indexOf('=');
    const key = trimWS(eq < 0 ? item : item.slice(0, eq));
    if (eq < 0 || !HEADER_KEY.test(key)) {
      skipped.push(pos);
      return;
    }
    const rawValue = item.slice(eq + 1);
    const decoded = decodeValue(rawValue);
    const value = trimWS(decoded ?? rawValue);
    if (!PRINTABLE_ASCII.test(value)) {
      skipped.push(pos);
      return;
    }
    if (decoded === undefined) literal.push(pos);
    headers[key.toLowerCase()] = value;
    positions[key.toLowerCase()] = pos;
  });
  return { headers, positions, skipped, literal };
}

/** Headers of one signal and the variable they came from (for diagnostics). */
export interface ResolvedHeaders {
  readonly variable: string;
  /** Lowercased keys, null prototype; empty when every item was malformed. */
  readonly values: Readonly<Record<string, string>>;
  /** Position of each key's item in the env value, for diagnostics (`item #N`). */
  readonly positions: Readonly<Record<string, number>>;
}

/**
 * Headers of one signal: `OTEL_EXPORTER_OTLP_<SIGNAL>_HEADERS` replaces
 * `OTEL_EXPORTER_OTLP_HEADERS` (as in Go and Python); a per-signal variable
 * with no valid item falls back to the generic one. `undefined` when neither
 * is set; empty `values` when every set item was malformed.
 */
export function resolveHeaders(signal: Signal, warn: Warn): ResolvedHeaders | undefined {
  let result: ResolvedHeaders | undefined;
  for (const variable of [otlpEnv(signal, 'HEADERS'), otlpEnv(undefined, 'HEADERS')]) {
    const raw = process.env[variable];
    if (raw === undefined || trimWS(raw) === '') continue;
    const parsed = parseHeaders(raw);
    for (const pos of parsed.skipped) {
      warn('malformed header item skipped', { variable, value: `item #${String(pos)}`, using: 'remaining headers' });
    }
    for (const pos of parsed.literal) {
      warn('invalid percent-escape in header value; kept literally', { variable, value: `item #${String(pos)}`, using: 'literal value' });
    }
    const resolved = Object.freeze({ variable, values: Object.freeze(parsed.headers), positions: Object.freeze(parsed.positions) });
    if (Object.keys(parsed.headers).length > 0) return resolved;
    result ??= resolved;
  }
  return result;
}

/** A resolved export timeout and where it came from. */
export interface ResolvedTimeout {
  readonly ms: number;
  /** The code option (`exportTimeoutMs`), the env variable, or `default`. */
  readonly source: string;
}

/**
 * Export timeout of one signal in milliseconds — its single export deadline
 * (D1 ruling "Export deadline", A18): code > `_<SIGNAL>_TIMEOUT` >
 * `OTEL_EXPORTER_OTLP_TIMEOUT` > 10 000. Env values are plain decimal
 * milliseconds; anything else (or `0`) warns and falls back to the next level;
 * values outside [1 ms, 24 h] are clamped with a warning.
 */
export function resolveTimeout(signal: Signal, codeMs: number | undefined, warn: Warn): ResolvedTimeout {
  if (codeMs !== undefined) return { ms: codeMs, source: 'exportTimeoutMs' };
  for (const variable of [otlpEnv(signal, 'TIMEOUT'), otlpEnv(undefined, 'TIMEOUT')]) {
    const raw = trimWS(process.env[variable] ?? '');
    if (raw === '') continue;
    const ms = Number(raw);
    if (!DECIMAL_MS.test(raw) || !Number.isFinite(ms) || ms <= 0) {
      warn('invalid timeout (positive decimal milliseconds)', { variable, value: truncateValue(raw), using: 'next level, else 10000' });
      continue;
    }
    if (ms > MAX_TIMEOUT_MS) {
      warn('timeout above 24 h; clamped', { variable, value: truncateValue(raw), using: String(MAX_TIMEOUT_MS) });
      return { ms: MAX_TIMEOUT_MS, source: variable };
    }
    if (ms < MIN_TIMEOUT_MS) {
      warn('timeout below 1 ms; clamped', { variable, value: truncateValue(raw), using: String(MIN_TIMEOUT_MS) });
      return { ms: MIN_TIMEOUT_MS, source: variable };
    }
    return { ms, source: variable };
  }
  return { ms: DEFAULT_TIMEOUT_MS, source: 'default' };
}

/** Normalized code compression; `undefined` when not set (an empty string is not set, D1 ruling). */
export function codeCompression(value: string | undefined): string | undefined {
  if (value === undefined) return undefined;
  const c = trimWS(value).toLowerCase();
  return c === '' ? undefined : c;
}

/**
 * Compression of one signal: code > `_<SIGNAL>_COMPRESSION` >
 * `OTEL_EXPORTER_OTLP_COMPRESSION` > `gzip` (D1.d). Env values are
 * case-insensitive `gzip` / `none`; anything else warns and falls back to the
 * next level.
 */
export function resolveCompression(signal: Signal, code: string | undefined, warn: Warn): OtlpCompression {
  const fromCode = codeCompression(code);
  if (fromCode !== undefined) return fromCode as OtlpCompression; // validated by checkCodeSettings
  for (const variable of [otlpEnv(signal, 'COMPRESSION'), otlpEnv(undefined, 'COMPRESSION')]) {
    const raw = trimWS(process.env[variable] ?? '');
    if (raw === '') continue;
    const value = raw.toLowerCase();
    if ((COMPRESSIONS as readonly string[]).includes(value)) return value as OtlpCompression;
    warn('unsupported compression (gzip or none)', { variable, value: truncateValue(raw), using: 'next level, else gzip' });
  }
  return DEFAULT_COMPRESSION;
}

/** Code export settings (already type-checked). */
export interface SettingsCodeOptions {
  readonly exportTimeoutMs?: number | undefined;
  readonly compression?: string | undefined;
}

/**
 * Fails fast on code export settings (R8): `exportTimeoutMs` must be a
 * finite number of milliseconds in [1, 86 400 000] (D1 ruling "Code export
 * timeout bound", A18 — the env clamp range; decimals allowed, as in the env
 * grammar), `compression` `gzip` or `none` (case-insensitive; an empty string
 * is not set).
 */
export function checkCodeSettings(code: SettingsCodeOptions): void {
  const t = code.exportTimeoutMs;
  if (t !== undefined && (!Number.isFinite(t) || t < MIN_TIMEOUT_MS || t > MAX_TIMEOUT_MS)) {
    throw new TypeError(`setup(): exportTimeoutMs must be a number of milliseconds in [1, ${String(MAX_TIMEOUT_MS)}], got ${String(t)}`);
  }
  const c = codeCompression(code.compression);
  if (c !== undefined && !(COMPRESSIONS as readonly string[]).includes(c)) {
    throw new TypeError(
      `setup(): compression has unknown value ${JSON.stringify(truncateValue(code.compression ?? ''))}; valid values: ${COMPRESSIONS.join(', ')}`,
    );
  }
}
