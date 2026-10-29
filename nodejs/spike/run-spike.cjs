'use strict';
// B1c spike runner: runs every check in child processes on the current Node
// and prints one line per check plus a JSON summary. Gates fail the exit code;
// controls are observations. Needs the fake OTLP receiver (FAKEOTLP_*), as in
// `docker compose -f testing/docker-compose.yml run --rm node-tests` with this
// script as the command (see specs/nodejs-otel-helper/spike-B1c.md).
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const results = [];
const record = (id, kind, ok, detail) => {
  results.push({ id, kind, ok, detail });
  console.log(`${kind === 'gate' ? (ok ? 'PASS' : 'FAIL') : 'INFO'} ${id}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
};

function run(cmd, args, { env, cwd = __dirname } = {}) {
  const res = spawnSync(cmd, args, { cwd, env: { ...process.env, ...env }, encoding: 'utf8', timeout: 180_000 });
  if (res.error) throw res.error;
  return res;
}
const node = (args, env) => run(process.execPath, args, { env });
function json(res) {
  const line = res.stdout.trim().split('\n').pop();
  try {
    return JSON.parse(line);
  } catch {
    return { parseError: true, status: res.status, stdout: res.stdout.slice(-2000), stderr: res.stderr.slice(-2000) };
  }
}

// Checks 1, 2, 4: server + client span, child parented by the server span,
// server parented by the client span (W3C propagation), one trace.
function httpVerdict(out) {
  const spans = out.spans ?? [];
  const server = spans.find((s) => s.kind === 'SERVER');
  const client = spans.find((s) => s.kind === 'CLIENT');
  const child = spans.find((s) => s.name === 'child-in-handler');
  return {
    spans: spans.map((s) => `${s.kind}:${s.name}`),
    serverSpan: Boolean(server),
    clientSpan: Boolean(client),
    childParentIsServer: Boolean(child && server && child.parentSpanId === server.spanId),
    serverParentIsClient: Boolean(server && client && server.parentSpanId === client.spanId),
    oneTrace: new Set(spans.map((s) => s.traceId)).size === 1,
  };
}
const httpOk = (v) => v.serverSpan && v.clientSpan && v.childParentIsServer && v.serverParentIsClient && v.oneTrace;

function httpCheck(id, kind, args, env) {
  const res = node(args, env);
  const v = httpVerdict(json(res));
  if (res.stderr.trim()) v.stderr = res.stderr.trim().split('\n').slice(0, 3);
  // A gate also fails on any stderr output (warnings are findings).
  record(id, kind, kind === 'gate' ? httpOk(v) && res.status === 0 && !res.stderr.trim() : true, v);
  return v;
}

console.log(`node ${process.versions.node} (${process.platform}/${process.arch}); iitm sync hooks: ${
  require('import-in-the-middle/supports-sync-hooks.mjs').supportsSyncHooks()}`);

// 1. ESM app with a custom preload.
httpCheck('1a esm default import (hooks)', 'gate', ['--import', './esm-preload.mjs', 'app-esm-default.mjs']);
httpCheck('1b esm named import (hooks)', 'gate', ['--import', './esm-preload.mjs', 'app-esm-named.mjs']);
httpCheck('1c control: esm default import, no hooks', 'control', ['--import', './esm-preload-nohooks.mjs', 'app-esm-default.mjs']);
httpCheck('1d control: esm named import, no hooks', 'control', ['--import', './esm-preload-nohooks.mjs', 'app-esm-named.mjs']);
// Record which loader /hooks chose.
{
  const res = node(['--import', '@staffops/otel-helper/hooks', '-e',
    "console.log(JSON.stringify({hooks: globalThis[Symbol.for('staffops.otel-helper.state.v1')].hooks}))"]);
  record('1e hooks mode', 'control', true, json(res));
}

// 2. CommonJS app with --require.
httpCheck('2a cjs default require (hooks)', 'gate', ['--require', './cjs-preload.cjs', 'app-cjs.cjs']);
httpCheck('2b cjs destructured require (hooks)', 'gate', ['--require', './cjs-preload.cjs', 'app-cjs.cjs'], { SPIKE_STYLE: 'named' });
httpCheck('2c control: cjs, no hooks', 'control', ['--require', './cjs-preload-nohooks.cjs', 'app-cjs.cjs'], { SPIKE_STYLE: 'named' });

// 3. One copy of each SDK package in the installed tree (workspace + a fresh consumer install).
const SINGLE = ['@opentelemetry/api', '@opentelemetry/core', '@opentelemetry/sdk-trace', '@opentelemetry/sdk-metrics',
  '@opentelemetry/sdk-logs', '@opentelemetry/resources', '@opentelemetry/instrumentation',
  '@opentelemetry/otlp-exporter-base', '@opentelemetry/otlp-grpc-exporter-base', '@opentelemetry/otlp-transformer',
  'import-in-the-middle', 'require-in-the-middle', '@grpc/grpc-js'];
function copies(cwd) {
  const out = {};
  for (const pkg of SINGLE) {
    const res = run('npm', ['ls', pkg, '--all', '--parseable'], { cwd });
    const paths = [...new Set(res.stdout.split('\n').filter((l) => l.endsWith(`/node_modules/${pkg}`)))];
    out[pkg] = paths.map((p) => {
      const { version } = JSON.parse(fs.readFileSync(path.join(p, 'package.json'), 'utf8'));
      return `${version} ${path.relative(cwd, p)}`;
    });
  }
  return out;
}
const tree = (c) => Object.values(c).every((v) => v.length === 1);
{
  const ws = copies(path.resolve(__dirname, '..'));
  record('3a single copies (workspace tree)', 'gate', tree(ws), ws);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'b1c-consumer-'));
  const packed = run('npm', ['pack', path.resolve(__dirname, '../packages/otel-helper'), '--pack-destination', dir, '--json']);
  const tgz = JSON.parse(packed.stdout)[0].filename;
  fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({
    name: 'b1c-consumer', private: true,
    dependencies: { '@staffops/otel-helper': `file:./${tgz}`, '@grpc/grpc-js': '^1.14.7' },
  }));
  const inst = run('npm', ['install', '--ignore-scripts', '--no-audit', '--no-fund', '--loglevel=error'], { cwd: dir });
  if (inst.status !== 0) {
    record('3b single copies (fresh consumer install)', 'gate', false, inst.stderr.slice(-1500));
  } else {
    const c = copies(dir);
    record('3b single copies (fresh consumer install)', 'gate', tree(c), c);
  }
}

// 5. Explicit gRPC credentials after registerInstrumentations.
async function receiver(method) {
  const admin = process.env.FAKEOTLP_ADMIN;
  if (!admin) throw new Error('FAKEOTLP_ADMIN is not set: run inside the node-tests compose service');
  const res = await fetch(`${admin}/requests`, { method });
  return method === 'GET' ? res.json() : null;
}
async function grpcCheck(id, mode, kind, expect, env = {}) {
  await receiver('DELETE');
  const res = node(['grpc-credentials.cjs'], { SPIKE_GRPC_MODE: mode, ...env });
  const out = json(res);
  const reqs = ((await receiver('GET')) ?? []).filter((r) => r.headers?.['x-parity-spike'] === mode)
    .map((r) => ({ proto: r.proto, tls: r.tls, port: r.port, path: r.path }));
  const v = {
    exportCode: out.exportCode, exportError: out.exportError,
    grpcServerSpan: (out.spans ?? []).some((s) => s.kind === 'SERVER' && s.name.includes('spike.Spike/Ping')),
    grpcLoadedAtRegister: out.grpcLoadedAtRegister, sameGrpcCopy: out.sameGrpcCopy, grpcPath: out.grpcPath,
    received: reqs,
  };
  if (out.parseError) v.child = out;
  if (res.stderr.trim()) v.stderr = res.stderr.trim().split('\n').slice(0, 3);
  // DEP0123 comes from grpc-js setting the TLS ServerName to the receiver's IP
  // literal (127.0.0.2): a property of the test target, not of the helper. Any
  // other stderr output still fails the gate.
  const unexpected = res.stderr.split('\n').filter((l) => l && !/\[DEP0123\]|--trace-deprecation/.test(l));
  record(`5 ${id}`, kind, kind === 'gate' ? expect(v) && res.status === 0 && unexpected.length === 0 : true, v);
}

(async () => {
  // The receiver's DNS name (cert SAN `fakeotlp`); spike-B1c.md maps it to 127.0.0.2 in /etc/hosts.
  const dns = { SPIKE_TLS_HOST: 'fakeotlp' };
  const tlsOk = (v) => v.exportCode === 0 && v.grpcServerSpan && v.grpcLoadedAtRegister === false
    && v.sameGrpcCopy && v.received.length > 0 && v.received.every((r) => r.tls && r.port === '14317');
  await grpcCheck('explicit-tls (DNS name)', 'explicit-tls', 'gate', tlsOk, dns);
  await grpcCheck('explicit-insecure', 'explicit-insecure', 'gate', (v) => v.exportCode === 0 && v.grpcServerSpan
    && v.grpcLoadedAtRegister === false && v.received.length > 0 && v.received.every((r) => !r.tls && r.port === '4317'));
  await grpcCheck('control: explicit-tls (IP literal)', 'explicit-tls', 'control');
  await grpcCheck('control: exporters-first', 'exporters-first', 'control', undefined, dns);
  await grpcCheck('control: lazy-env', 'lazy-env', 'control', undefined, dns);
  await grpcCheck('control: lazy-env-bogus', 'lazy-env-bogus', 'control', undefined, dns);

  const failed = results.filter((r) => r.kind === 'gate' && !r.ok);
  console.log(`SUMMARY node=${process.versions.node} gates=${results.filter((r) => r.kind === 'gate').length} failed=${failed.length}`);
  process.exit(failed.length ? 1 : 0);
})().catch((err) => {
  console.error(err);
  process.exit(2);
});
