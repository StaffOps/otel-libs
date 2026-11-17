// Environment parsing shared by the three demo apps (specs/demo-environment
// DE-D2, DE-D13). Every setting comes from the environment; passwords come
// from files (DEMO_PG_PASSWORD_FILE, DEMO_REDIS_PASSWORD_FILE) and are never
// logged or put in an error message.
'use strict';

const fs = require('node:fs');

/** Language label of this demo set: services demo-node-*, database demo_node, keys demo:node:*. */
const LANG = 'node';

/** Run id when the environment carries none (local runs). */
const DEFAULT_RUN = 'local';

/**
 * The run id: `otel_libs.run` from OTEL_RESOURCE_ATTRIBUTES (the one place the
 * demo manifests set it, DE-D2), else "local" (no separate variable, DE-D7
 * r3.4). The error messages of the scenarios carry it as run tokens (rej-, dup-, unh-<run>, DE-D8).
 */
function runId(env = process.env) {
  for (const entry of (env.OTEL_RESOURCE_ATTRIBUTES ?? '').split(',')) {
    const eq = entry.indexOf('=');
    if (eq < 0) continue;
    if (safeDecode(entry.slice(0, eq).trim()) !== 'otel_libs.run') continue;
    const value = safeDecode(entry.slice(eq + 1).trim());
    if (value !== '') return value;
  }
  return DEFAULT_RUN;
}

/** Percent-decoding as the W3C Baggage octets of OTEL_RESOURCE_ATTRIBUTES; malformed input stays as is. */
function safeDecode(text) {
  try {
    return decodeURIComponent(text);
  } catch {
    return text;
  }
}

/** The variable's trimmed value, else `fallback` (the defaults the four languages share, DE-D13). */
function envOr(env, name, fallback) {
  const value = (env[name] ?? '').trim();
  return value === '' ? fallback : value;
}

/** A required, non-empty variable; the error names the variable only. */
function requireEnv(env, name) {
  const value = (env[name] ?? '').trim();
  if (value === '') throw new Error(`${name} is not set`);
  return value;
}

/** A positive number of seconds (fractions allowed), else `fallback`; an invalid value is an error. */
function secondsEnv(env, name, fallback) {
  const raw = (env[name] ?? '').trim();
  if (raw === '') return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) throw new Error(`${name} must be a positive number of seconds`);
  return value;
}

/** A TCP port in [1, 65535], else `fallback`; an invalid value is an error. */
function portEnv(env, name, fallback) {
  const raw = (env[name] ?? '').trim();
  if (raw === '') return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 1 || value > 65535) throw new Error(`${name} must be a TCP port (1-65535)`);
  return value;
}

/** `host:port` (or `[v6]:port`) split into its parts; a missing port is `defaultPort`. */
function parseHostPort(value, defaultPort, name = 'address') {
  const text = value.trim();
  let host = text;
  let portText = '';
  if (text.startsWith('[')) {
    const close = text.indexOf(']');
    if (close < 0) throw new Error(`${name} has an unclosed IPv6 bracket`);
    host = text.slice(1, close);
    const rest = text.slice(close + 1);
    if (rest !== '') {
      if (!rest.startsWith(':')) throw new Error(`${name} must be host[:port]`);
      portText = rest.slice(1);
    }
  } else {
    const colon = text.lastIndexOf(':');
    if (colon >= 0) {
      host = text.slice(0, colon);
      portText = text.slice(colon + 1);
    }
  }
  if (host === '') throw new Error(`${name} has no host`);
  if (portText === '') return { host, port: defaultPort };
  const port = Number(portText);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error(`${name} has an invalid port`);
  return { host, port };
}

/**
 * The secret in the file named by `env[name]`, read once at start-up, without
 * its trailing line break. Errors name the variable, never the content.
 */
function readSecretFile(env, name, readFile = fs.readFileSync) {
  const file = requireEnv(env, name);
  let text;
  try {
    text = readFile(file, 'utf8');
  } catch (err) {
    // The cause names the path and the errno, never the content.
    throw new Error(`${name}: cannot read the file (${err && err.code ? err.code : 'error'})`, { cause: err });
  }
  const secret = text.replace(/\r?\n$/, '');
  if (secret === '') throw new Error(`${name}: the file is empty`);
  return secret;
}

module.exports = { DEFAULT_RUN, LANG, envOr, parseHostPort, portEnv, readSecretFile, requireEnv, runId, secondsEnv };
