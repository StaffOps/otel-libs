/**
 * `OTEL_RESOURCE_ATTRIBUTES` parser (spec JS4). The SDK's env detector is not
 * used: it keeps values untrimmed and drops every attribute on one bad item,
 * logging only at `diag.debug` (research.md). The helper keeps the valid
 * items and warns about the others by position, never printing a value.
 */
import { trimWS } from './env';

/** Result of {@link parseResourceAttributes}. */
export interface ParsedResourceAttributes {
  /** Valid `key=value` pairs; a later duplicate key wins. */
  readonly attributes: Readonly<Record<string, string>>;
  /** 1-based positions of items without `=` or with an empty key (skipped). */
  readonly malformed: readonly number[];
  /** 1-based positions whose value has an invalid `%` escape (kept literally). */
  readonly undecodable: readonly number[];
}

/**
 * Parses `key1=value1,key2=value2` (OTel SDK environment variable spec):
 * keys and (decoded) values are trimmed with the contract trim set; values are
 * percent-decoded — an invalid escape keeps the whole value literal, as the
 * header rule does (contract design D1). Blank items (`a=1,,b=2`, a trailing
 * comma) are ignored. The first `=` separates key and value.
 */
export function parseResourceAttributes(raw: string): ParsedResourceAttributes {
  // Null prototype: a `__proto__` key is an ordinary attribute, not a prototype write.
  const attributes = Object.create(null) as Record<string, string>;
  const malformed: number[] = [];
  const undecodable: number[] = [];
  raw.split(',').forEach((item, index) => {
    if (trimWS(item) === '') return;
    const eq = item.indexOf('=');
    const key = eq < 0 ? '' : trimWS(item.slice(0, eq));
    if (key === '') {
      malformed.push(index + 1);
      return;
    }
    const value = item.slice(eq + 1);
    let decoded = value;
    try {
      decoded = decodeURIComponent(value);
    } catch {
      undecodable.push(index + 1);
    }
    // Node decodes the environment before the helper sees it (invalid bytes are
    // already U+FFFD), so the "key or value is not UTF-8" rule of Go/Python
    // cannot trigger here: an invalid byte reaches the resource as U+FFFD.
    attributes[key] = trimWS(decoded);
  });
  return { attributes, malformed, undecodable };
}
