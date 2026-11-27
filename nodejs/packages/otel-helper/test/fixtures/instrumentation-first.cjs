'use strict';
// Task A41 (contract tasks.md): an instrumentation built before anything
// else, then `@staffops/otel-helper`, express, `@grpc/grpc-js`, `node:http`
// and `node:https` required, then setup() with OTLP over gRPC (an
// unreachable endpoint). One express request that makes one gRPC call;
// prints the finished spans, the helper's reports, whether each module is
// patched, and whether `node:http` was already loaded when the
// instrumentation had been built (`httpLoadedFirst`).
// argv[2]: `factory` — an opt-in factory result (a `contribRegistry` of the
// internal contrib entry, as `-sql`, `-redis` and `-aws` build theirs), whose
// first build builds the core instrumentations first. That covers express
// and grpc-js loaded afterwards; `node:http` is loaded by the factory call
// itself (its lazy require of the helper's setup module), before any hook
// exists, so this mode does not put `node:http` in the window. `own` — an
// instrumentation the app builds itself, the order the helper cannot fix:
// `node:http` and `node:https` are loaded through its hook.
for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_') || k === 'SERVICE_NAME' || k === 'ENVIRONMENT') delete process.env[k];
process.env.OTEL_SERVICE_NAME = 'svc';
process.env.OTEL_HELPER_METRICS_PORT = '0';
process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'http://127.0.0.1:1';
process.env.OTEL_EXPORTER_OTLP_PROTOCOL = 'grpc';
process.env.OTEL_METRICS_EXPORTER = 'none';
process.env.OTEL_LOGS_EXPORTER = 'none';

const { InstrumentationBase, InstrumentationNodeModuleDefinition } = require('@opentelemetry/instrumentation');

/** Built (and so enabled, installing the shared require hook) by its constructor. */
class Early extends InstrumentationBase {
  constructor() {
    super('a41-early', '0.0.0', {});
  }
  init() {
    return [new InstrumentationNodeModuleDefinition('a41-not-installed', ['*'])];
  }
}

const mode = process.argv[2];
let early;
if (mode === 'factory') {
  const { contribRegistry } = require('@staffops/otel-helper/internal/contrib');
  const registry = contribRegistry({
    name: 'a41-opt-in',
    version: '0.0.0',
    libraries: ['lib'],
    contrib: { lib: 'a41-contrib' },
    packages: { lib: ['a41-not-installed'] },
    build: () => new Early(),
    patchedFiles: { lib: [] },
    tracerProvider: (_library, provider) => provider,
    meterProvider: (_library, provider) => provider,
  });
  early = registry.instrument({});
} else if (mode === 'own') {
  early = new Early();
} else {
  throw new Error(`unknown mode ${String(mode)}`);
}

const httpLoadedFirst = process.moduleLoadList.includes('NativeModule http');

// The window: everything below is loaded after the instrumentation was built.
const reports = [];
require('@staffops/otel-helper/testing').setDiagnosticSink((line, meta) => {
  if (meta.origin === 'helper') reports.push(line);
});
const helper = require('@staffops/otel-helper');
const express = require('express');
const grpc = require('@grpc/grpc-js');
const http = require('node:http');
const https = require('node:https');
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const { isWrapped } = require('@opentelemetry/instrumentation');

const exporter = new InMemorySpanExporter();
helper.setup({ instrumentations: [early], spanProcessors: [new SimpleSpanProcessor({ exporter })] });
const patched = {
  'node:http': isWrapped(http.request),
  'node:https': isWrapped(https.request),
  express: isWrapped(express.application.use),
  '@grpc/grpc-js': isWrapped(grpc.Server.prototype.register),
};

const identity = (b) => b;
const method = { path: '/a41.v1.Orders/Place', requestStream: false, responseStream: false, requestSerialize: identity, requestDeserialize: identity, responseSerialize: identity, responseDeserialize: identity };
const grpcServer = new grpc.Server();
grpcServer.addService({ Place: method }, { Place: (_call, cb) => cb(null, Buffer.alloc(0)) });
grpcServer.bindAsync('127.0.0.1:0', grpc.ServerCredentials.createInsecure(), (err, grpcPort) => {
  if (err) throw err;
  const client = new grpc.Client(`127.0.0.1:${grpcPort}`, grpc.credentials.createInsecure());
  const app = express();
  app.get('/orders', (_req, res) => {
    client.makeUnaryRequest(method.path, identity, identity, Buffer.alloc(0), (callErr) => res.status(callErr ? 500 : 200).send('ok'));
  });
  const server = app.listen(0, '127.0.0.1', () => {
    http.get(`http://127.0.0.1:${server.address().port}/orders`, (res) => {
      res.resume();
      res.on('end', () => {
        client.close();
        grpcServer.forceShutdown();
        server.close(() => {
          const spans = exporter.getFinishedSpans().map((s) => ({ name: s.name, kind: s.kind, scope: s.instrumentationScope.name, traceId: s.spanContext().traceId }));
          console.log(JSON.stringify({ spans, reports, patched, httpLoadedFirst, status: res.statusCode }));
          process.exit(0);
        });
      });
    });
  });
});
