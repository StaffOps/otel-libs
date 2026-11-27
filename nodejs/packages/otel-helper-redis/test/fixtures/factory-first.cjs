'use strict';
// redis.test.cjs fixture (task A41): redisInstrumentation() created before
// anything else — before `@staffops/otel-helper`, express, `@grpc/grpc-js`,
// `node:http` and redis are required — then setup() with OTLP over gRPC (an
// unreachable endpoint). One express request whose handler runs a Redis SET
// (REDIS_FAKE_URL) and a gRPC call; prints the finished spans and the
// helper's reports. Before A41 the factory's require hook cached the
// helper's node:http, express and grpc-js unpatched. Since A41 the factory
// call builds the core instrumentations first (covering express and grpc-js)
// and, by requiring the core's setup module lazily, loads node:http before
// any hook exists.
for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_') && k !== 'OTEL_SERVICE_NAME' && k !== 'OTEL_RESOURCE_ATTRIBUTES' && k !== 'OTEL_HELPER_METRICS_PORT') delete process.env[k];
process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'http://127.0.0.1:1';
process.env.OTEL_EXPORTER_OTLP_PROTOCOL = 'grpc';
process.env.OTEL_METRICS_EXPORTER = 'none';
process.env.OTEL_LOGS_EXPORTER = 'none';
const { redisInstrumentation } = require('@staffops/otel-helper-redis');
const instrumentation = redisInstrumentation({ redis: true, ioredis: false });

// The window: everything below is loaded after the factory built its instrumentation.
const reports = [];
require('@staffops/otel-helper/testing').setDiagnosticSink((line, meta) => {
  if (meta.origin === 'helper') reports.push(line);
});
const helper = require('@staffops/otel-helper');
const express = require('express');
const grpc = require('@grpc/grpc-js');
const http = require('node:http');
const redis = require('redis');
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');

const exporter = new InMemorySpanExporter();
helper.setup({ instrumentations: [instrumentation], spanProcessors: [new SimpleSpanProcessor({ exporter })] });

const identity = (b) => b;
const method = { path: '/a41.v1.Orders/Place', requestStream: false, responseStream: false, requestSerialize: identity, requestDeserialize: identity, responseSerialize: identity, responseDeserialize: identity };

(async () => {
  const grpcServer = new grpc.Server();
  grpcServer.addService({ Place: method }, { Place: (_call, cb) => cb(null, Buffer.alloc(0)) });
  const grpcPort = await new Promise((resolve, reject) => grpcServer.bindAsync('127.0.0.1:0', grpc.ServerCredentials.createInsecure(), (err, p) => (err ? reject(err) : resolve(p))));
  const client = new grpc.Client(`127.0.0.1:${grpcPort}`, grpc.credentials.createInsecure());
  const c = redis.createClient({ url: process.env.REDIS_FAKE_URL });
  c.on('error', () => undefined);
  await c.connect();
  const app = express();
  app.get('/orders', (_req, res) => {
    c.set('k', 'v')
      .then(() => new Promise((resolve, reject) => client.makeUnaryRequest(method.path, identity, identity, Buffer.alloc(0), (err) => (err ? reject(err) : resolve()))))
      .then(() => res.send('ok'), () => res.status(500).end());
  });
  const server = await new Promise((resolve) => {
    const s = app.listen(0, '127.0.0.1', () => resolve(s));
  });
  await new Promise((resolve, reject) => {
    http.get(`http://127.0.0.1:${server.address().port}/orders`, (res) => {
      res.resume();
      res.on('end', resolve);
    }).on('error', reject);
  });
  await c.quit();
  client.close();
  grpcServer.forceShutdown();
  await new Promise((resolve) => server.close(resolve));
  const spans = exporter.getFinishedSpans().map((s) => ({ name: s.name, kind: s.kind, scope: s.instrumentationScope.name, traceId: s.spanContext().traceId }));
  console.log(JSON.stringify({ spans, reports }));
  process.exit(0);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
