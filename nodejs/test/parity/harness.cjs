'use strict';
// Parity harness for Node.js (design D-JS12): fixture schema and
// well-formedness rules, placeholders, child execution and the matcher.
// Port of python/tests/test_parity.py, mirroring go/parity_match_test.go;
// format and semantics: parity/README.md. The tests live in runner.test.cjs.

const { spawn } = require('node:child_process');
const dns = require('node:dns');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { isDeepStrictEqual } = require('node:util');

const DRIVER = path.join(__dirname, 'driver.cjs');
const CASES_PATH = process.env.PARITY_CASES || path.resolve(__dirname, '..', '..', '..', 'parity', 'cases.json');
const CHILD_TIMEOUT_MS = 120_000; // export cases may wait up to 75 s for a long-timeout export
const DRAIN_MS = 2_000; // after the child exits, wait at most this long for its pipes to close
const LANGS = ['go', 'python', 'dotnet', 'nodejs'];
// D-JS14 statuses that are not waiting for a task (a recorded Node difference).
const PERMANENT_SKIPS = new Set(['skip:no-upstream-rpc-metrics', 'skip:no-upstream-resource-proto-key']);

// ── schema (mirrors python/tests/test_parity.py and go/parity_match_test.go) ──
const COMMON_EXPECT = new Set(['error', 'warning', 'warning_count', 'log', 'diagnostics', 'helper_log', 'console']);
const CASE_KEYS = new Set(['id', 'kind', 'requirement', 'note', 'env', 'code', 'input', 'expect', 'status']);
const REQUIRED_KEYS = ['id', 'kind', 'env', 'code', 'expect', 'status'];
const OVERRIDE_RE = /^python@sdk>=[\d.]+$/;
const SCHEMA = {
  resolve: ['resource', 'metric_exporters', 'metric_export_interval_ms', 'log_level'],
  export: ['received'],
  sampler: ['sampled', 'attributes'],
  propagation: ['headers'],
  http_server: ['server_spans', 'requests_counted', 'handler_child_spans'],
  grpc_server: ['server_spans', 'rpcs_counted', 'call_ok'],
  logging: [],
  metrics_scrape: ['scrape', 'scrape_status'],
  fork: ['instance_ids', 'instance_ids_distinct'],
};
const INPUT_SCHEMA = {
  sampler: ['scenario'],
  http_server: ['requests', 'traceparent', 'child_span'],
  grpc_server: ['method'],
};
const SIGNALS = new Set(['traces', 'metrics', 'logs']);
const RECORD_FIELDS = new Set(['proto', 'tls', 'port', 'path', 'raw_path', 'encoding', 'headers', 'timeout_ms', 'absent']);
const MATCHER_KEYS = new Set(['prefix', 'regex', 'absent', 'not_contains', 'all', 'lte', 'gte']);
const STATUS_RE = /^(pass|xfail:[A-Za-z0-9.-]+|skip:[A-Za-z0-9._-]+)$/;

const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const isNumber = (v) => typeof v === 'number';
const has = (obj, k) => Object.prototype.hasOwnProperty.call(obj, k);

function loadCases(file = CASES_PATH) {
  return JSON.parse(fs.readFileSync(file, 'utf8')).cases;
}

/** The nodejs status (no per-version override exists for Node). */
function nodeStatus(c) {
  return String(c.status?.nodejs ?? '');
}

/** Objects that contain a matcher op may contain only matcher ops. */
function checkMatchers(p, v) {
  const errs = [];
  if (isObject(v)) {
    if (Object.keys(v).some((k) => MATCHER_KEYS.has(k))) {
      for (const k of Object.keys(v)) if (!MATCHER_KEYS.has(k)) errs.push(`${p}: ${JSON.stringify(k)} is not a matcher op`);
      if (has(v, 'all')) {
        if (!Array.isArray(v.all)) errs.push(`${p}.all must be a list`);
        else v.all.forEach((sub, i) => errs.push(...checkMatchers(`${p}.all[${i}]`, sub)));
      }
      for (const op of ['lte', 'gte']) if (has(v, op) && !isNumber(v[op])) errs.push(`${p}.${op} must be a number`);
      return errs;
    }
    for (const [k, x] of Object.entries(v)) errs.push(...checkMatchers(`${p}.${k}`, x));
  } else if (Array.isArray(v)) {
    v.forEach((x, i) => errs.push(...checkMatchers(`${p}[${i}]`, x)));
  }
  return errs;
}

function walkRegexes(v, fn) {
  if (isObject(v)) {
    for (const [k, x] of Object.entries(v)) {
      if (k === 'regex' && typeof x === 'string') fn(x);
      else walkRegexes(x, fn);
    }
  } else if (Array.isArray(v)) {
    for (const x of v) walkRegexes(x, fn);
  }
}

/** Fixture-format errors (not behavior mismatches); type-checks every asserted shape. */
function validateCase(c) {
  const errs = REQUIRED_KEYS.filter((k) => !has(c, k)).sort().map((k) => `missing key ${JSON.stringify(k)}`);
  errs.push(...Object.keys(c).filter((k) => !CASE_KEYS.has(k)).sort().map((k) => `unknown top-level key ${JSON.stringify(k)}`));
  const allowed = SCHEMA[c.kind];
  if (allowed === undefined || !has(SCHEMA, c.kind)) return [...errs, `unknown kind ${JSON.stringify(c.kind)}`];
  const expect = c.expect;
  if (!isObject(expect)) return [...errs, 'expect must be an object'];
  for (const key of Object.keys(expect)) {
    if (!allowed.includes(key) && !COMMON_EXPECT.has(key)) errs.push(`expect key ${JSON.stringify(key)} is not in the ${c.kind} snapshot schema`);
  }
  for (const [key, val] of Object.entries(c.input ?? {})) {
    if (!(INPUT_SCHEMA[c.kind] ?? []).includes(key)) errs.push(`input key ${JSON.stringify(key)} is not valid for kind ${c.kind}`);
    else if (key === 'requests' && !(Array.isArray(val) && val.every((x) => typeof x === 'string'))) errs.push('input.requests must be a list of strings');
    else if (['method', 'scenario', 'traceparent'].includes(key) && typeof val !== 'string') errs.push(`input.${key} must be a string`);
    else if (key === 'child_span' && typeof val !== 'boolean') errs.push('input.child_span must be a boolean');
  }
  if (has(expect, 'warning_count') && !(isObject(expect.warning_count) && Object.values(expect.warning_count).every(isNumber))) {
    errs.push('warning_count must map substrings to numbers');
  }
  if (has(expect, 'warning')) {
    const w = expect.warning;
    if (!(typeof w === 'string' || (Array.isArray(w) && w.every((x) => typeof x === 'string')))) errs.push('warning must be a string or list of strings');
  }
  if (has(expect, 'received')) {
    const rec = expect.received;
    if (!isObject(rec)) {
      errs.push('expect.received must be an object');
    } else {
      for (const [sig, want] of Object.entries(rec)) {
        if (!SIGNALS.has(sig)) errs.push(`received signal ${JSON.stringify(sig)} unknown`);
        if (!isObject(want)) {
          errs.push(`received.${sig} must be an object`);
          continue;
        }
        for (const f of Object.keys(want)) if (!RECORD_FIELDS.has(f)) errs.push(`received.${sig} field ${JSON.stringify(f)} unknown`);
      }
    }
  }
  errs.push(...checkMatchers('expect', expect));
  const status = isObject(c.status) ? c.status : {};
  for (const lang of LANGS) if (!STATUS_RE.test(String(status[lang] ?? ''))) errs.push(`status.${lang} invalid`);
  for (const [key, val] of Object.entries(status)) {
    if (!LANGS.includes(key) && !(OVERRIDE_RE.test(key) && STATUS_RE.test(String(val)))) {
      errs.push(`status override ${JSON.stringify(key)}: ${JSON.stringify(val)} invalid`);
    }
  }
  walkRegexes(expect, (re) => {
    try {
      new RegExp(re, 'u');
    } catch (err) {
      errs.push(`regex ${JSON.stringify(re)} does not compile: ${err.message}`);
    }
  });
  return errs;
}

// ── receiver / environment ──────────────────────────────────────────────

class NeedsReceiver extends Error {}

const PLACEHOLDER_ENV = { '{receiver_ipv6}': 'FAKEOTLP_HOST_IPV6', '{receiver_tls}': 'FAKEOTLP_HOST_TLS' };

function receiver(env = process.env) {
  if (!env.FAKEOTLP_HOST) return null;
  // Specific placeholders before "{receiver}" (insertion order is the replacement order).
  return {
    '{receiver_ipv6}': env.FAKEOTLP_HOST_IPV6 ?? '',
    '{receiver_tls}': env.FAKEOTLP_HOST_TLS ?? '',
    '{receiver_ca}': env.FAKEOTLP_CA ?? '',
    '{receiver}': env.FAKEOTLP_HOST,
  };
}

function expand(value, recv) {
  if (typeof value === 'string') {
    if (!value.includes('{receiver')) return value;
    if (recv === null) throw new NeedsReceiver('FAKEOTLP_HOST');
    for (const [placeholder, variable] of Object.entries(PLACEHOLDER_ENV)) {
      if (value.includes(placeholder) && !recv[placeholder]) throw new NeedsReceiver(variable);
    }
    for (const [placeholder, real] of Object.entries(recv)) value = value.split(placeholder).join(real);
    return value;
  }
  if (Array.isArray(value)) return value.map((v) => expand(v, recv));
  if (isObject(value)) return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, expand(v, recv)]));
  return value;
}

/** Receiver addresses; records sent to other local addresses are ignored. */
async function destIps(recv) {
  const ips = new Set();
  for (const key of ['{receiver}', '{receiver_tls}', '{receiver_ipv6}']) {
    const host = recv[key].replace(/^\[|\]$/g, '');
    if (!host) continue;
    for (const { address } of await dns.promises.lookup(host, { all: true })) ips.add(address);
  }
  return [...ips].sort().join(',');
}

async function childEnv(c, recv) {
  const env = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (!(k.startsWith('OTEL_') || k === 'SERVICE_NAME' || k === 'ENVIRONMENT')) env[k] = v;
  }
  env.OTEL_HELPER_METRICS_PORT = '0'; // never bind :9464 in tests; cases may override
  Object.assign(env, expand(c.env ?? {}, recv));
  if (c.kind === 'export') {
    if (recv === null) throw new NeedsReceiver('FAKEOTLP_HOST');
    env.FAKEOTLP_DEST_IPS = await destIps(recv);
  }
  return env;
}

// ── execution ───────────────────────────────────────────────────────────

function spawnDriver(caseFile, outFile, env) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [DRIVER, caseFile, outFile], { env, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8').on('data', (d) => {
      stdout += d;
    });
    child.stderr.setEncoding('utf8').on('data', (d) => {
      stderr += d;
    });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill('SIGKILL');
    }, CHILD_TIMEOUT_MS);
    child.on('error', (err) => {
      clearTimeout(timer);
      resolve({ code: null, stdout, stderr: `${stderr}\nspawn failed: ${err.message}`, timedOut });
    });
    // Resolve on 'exit', then give the pipes a short drain: a grandchild (a
    // cluster worker, a leaked exporter process) holding them open must not
    // delay the result until 'close'.
    let done = false;
    const finish = (code, signal) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve({ code: code ?? signal, stdout, stderr, timedOut });
    };
    child.on('exit', (code, signal) => {
      const drain = setTimeout(() => finish(code, signal), DRAIN_MS);
      child.on('close', () => {
        clearTimeout(drain);
        finish(code, signal);
      });
    });
  });
}

/**
 * Runs one case in a child; returns the snapshot or {_needs}/{_crash}. A
 * harness error (unexpected exception) is reported as `_crash`, not thrown.
 */
async function runCase(c, opts = {}) {
  try {
    return await runCaseUnchecked(c, opts);
  } catch (err) {
    return { _crash: `harness error: ${err?.stack ?? String(err)}` };
  }
}

async function runCaseUnchecked(c, { env = process.env } = {}) {
  const recv = receiver(env);
  let childEnvironment;
  let prepared;
  try {
    childEnvironment = await childEnv(c, recv);
    prepared = { ...c, code: expand(c.code ?? {}, recv) };
  } catch (err) {
    if (err instanceof NeedsReceiver) return { _needs: err.message };
    throw err;
  }
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'parity-'));
  try {
    const caseFile = path.join(tmp, 'case.json');
    const outFile = path.join(tmp, 'out.json');
    fs.writeFileSync(caseFile, JSON.stringify(prepared));
    const proc = await spawnDriver(caseFile, outFile, childEnvironment);
    if (!fs.existsSync(outFile)) {
      const why = proc.timedOut ? `timed out after ${CHILD_TIMEOUT_MS / 1000} s` : `rc=${proc.code}`;
      return { _crash: `driver crashed (${why}):\n${proc.stderr.slice(-3000)}` };
    }
    const snap = JSON.parse(fs.readFileSync(outFile, 'utf8'));
    const consoleText = proc.stdout + proc.stderr;
    snap.console = consoleText;
    snap.log = [snap.diagnostics ?? '', consoleText].filter(Boolean).join('\n');
    return snap;
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

// ── matching (mirrors go/parity_match_test.go and python/tests/test_parity.py) ──

const repr = (v) => (v === undefined ? '<absent>' : JSON.stringify(v));
// Text matchers apply to strings only: a present non-string value is a
// mismatch (no language-specific stringification; parity/README.md).
const TEXT_OPS = new Set(['prefix', 'regex', 'not_contains']);

function matchValue(p, want, got, present) {
  if (!isObject(want)) {
    if (!present) return [`${p}: want ${repr(want)}, got <absent>`];
    return isDeepStrictEqual(want, got) ? [] : [`${p}: want ${repr(want)}, got ${repr(got)}`];
  }
  if (!Object.keys(want).some((k) => MATCHER_KEYS.has(k))) {
    const gd = isObject(got) ? got : {};
    return Object.keys(want).sort().flatMap((k) => matchValue(`${p}.${k}`, want[k], gd[k], has(gd, k)));
  }
  const out = [];
  const s = typeof got === 'string' ? got : '';
  for (const op of Object.keys(want).sort()) {
    const arg = want[op];
    if (TEXT_OPS.has(op) && present && typeof got !== 'string') {
      out.push(`${p}: ${op} applies to strings, got ${repr(got)}`);
    } else if (op === 'absent') {
      if (arg === true && present) out.push(`${p}: want absent, got ${repr(got)}`);
      if (arg === false && !present) out.push(`${p}: want present`);
    } else if (op === 'prefix') {
      if (!present || !s.startsWith(arg)) out.push(`${p}: want prefix ${repr(arg)}, got ${repr(got)}`);
    } else if (op === 'regex') {
      let re;
      try {
        re = new RegExp(arg, 'u');
      } catch (err) {
        out.push(`${p}: bad regex ${repr(arg)}: ${err.message}`);
        continue;
      }
      if (!present || !re.test(s)) out.push(`${p}: want /${arg}/, got ${repr(s.slice(0, 300))}`);
    } else if (op === 'not_contains') {
      if (present && s.includes(arg)) out.push(`${p}: must not contain ${repr(arg)}`);
    } else if (op === 'lte' || op === 'gte') {
      if (!present || !isNumber(got) || (op === 'lte' && got > arg) || (op === 'gte' && got < arg)) {
        out.push(`${p}: want ${op} ${arg}, got ${repr(got)}`);
      }
    } else if (op === 'all') {
      for (const sub of arg) out.push(...matchValue(p, sub, got, present));
    }
  }
  return out;
}

function matchReceived(want, received) {
  const recv = isObject(received) ? received : {};
  const out = [];
  for (const sig of Object.keys(want).sort()) {
    const w = want[sig];
    const recs = Array.isArray(recv[sig]) ? recv[sig] : [];
    if (w.absent === true) {
      if (recs.length) out.push(`received.${sig}: want none, got ${JSON.stringify(recs)}`);
      continue;
    }
    if (!recs.length) {
      out.push(`received.${sig}: nothing received (all: ${JSON.stringify(recv)})`);
      continue;
    }
    recs.forEach((rec, i) => {
      for (const field of Object.keys(w).sort()) out.push(...matchValue(`received.${sig}[${i}].${field}`, w[field], rec[field], has(rec, field)));
    });
  }
  return out;
}

/** Behavior mismatches of a snapshot against a case's `expect`. */
function matchExpect(expect, snap) {
  const out = [];
  if (!has(expect, 'error') && has(snap, 'error')) out.push(`unexpected error: ${snap.error}`);
  for (const key of Object.keys(expect).sort()) {
    const want = expect[key];
    if (key === 'error') {
      if (typeof want === 'string') {
        if (!has(snap, 'error') || !String(snap.error).includes(want)) out.push(`error: want substring ${repr(want)}, got ${repr(snap.error)}`);
      } else {
        out.push(...matchValue('error', want, snap.error, has(snap, 'error')));
      }
    } else if (key === 'warning') {
      const log = snap.log ?? '';
      for (const sub of typeof want === 'string' ? [want] : want) {
        if (!log.includes(sub)) out.push(`warning: no diagnostic contains ${repr(sub)} (log: ${repr(log.slice(0, 300))})`);
      }
    } else if (key === 'warning_count') {
      const lines = (snap.diagnostics ?? '').split('\n');
      for (const sub of Object.keys(want).sort()) {
        const got = lines.filter((line) => line.includes(sub)).length;
        if (got !== want[sub]) out.push(`warning_count[${repr(sub)}]: want ${want[sub]}, got ${got}`);
      }
    } else if (key === 'received') {
      out.push(...matchReceived(want, snap.received));
    } else {
      out.push(...matchValue(key, want, snap[key], has(snap, key)));
    }
  }
  return out;
}

/**
 * Verdict of one case under its status: {ok, reason}. Fixture and adapter
 * problems fail whatever the status; only a behavior mismatch satisfies an
 * xfail; an xfail that passes fails (strict).
 */
function verdict(c, snap, { requireReceiver = Boolean(process.env.PARITY_REQUIRE_FAKEOTLP) } = {}) {
  const errs = validateCase(c);
  if (errs.length) return { ok: false, reason: `fixture error (fails regardless of status): ${errs.join('; ')}` };
  if (has(snap, '_crash')) return { ok: false, reason: snap._crash };
  if (has(snap, '_needs')) {
    if (requireReceiver) return { ok: false, reason: `PARITY_REQUIRE_FAKEOTLP is set but ${snap._needs} is not: export cases must run` };
    return { ok: true, skip: `case needs the fake OTLP receiver (${snap._needs} unset; see testing/README.md)` };
  }
  if (has(snap, 'adapter_error')) return { ok: false, reason: `adapter error (fails regardless of status): ${snap.adapter_error}` };
  if (has(snap, 'adapter_pending')) return { ok: false, reason: `adapter pending (fails unless the case is skipped): ${snap.adapter_pending}` };
  const mismatches = matchExpect(c.expect, snap);
  const status = nodeStatus(c);
  if (status.startsWith('xfail:')) {
    if (!mismatches.length) return { ok: false, reason: `unexpected pass: ${c.id} is ${status}; flip it to pass` };
    return { ok: true, xfail: mismatches.join('; ') };
  }
  if (mismatches.length) return { ok: false, reason: [`parity case ${c.id} failed:`, ...mismatches].join('\n  ') };
  return { ok: true };
}

module.exports = {
  CASES_PATH, CHILD_TIMEOUT_MS, PERMANENT_SKIPS, SCHEMA, NeedsReceiver,
  loadCases, nodeStatus, validateCase, receiver, expand, destIps, childEnv, runCase, matchExpect, verdict,
};
