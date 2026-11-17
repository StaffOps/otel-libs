'use strict';
// Task A39 (contract tasks.md): an opt-in instrumentation built before
// setup() — as `setup({ instrumentations: [redisInstrumentation()] })` does —
// installs the shared require-in-the-middle hook first. This child runs one
// express request that makes one gRPC call and prints the finished spans, so
// the parent can assert the core http, express and gRPC instrumentations still
// record. argv[2]: the OTLP protocol of the (unreachable) endpoint, `grpc` or
// `http/protobuf`. argv[3] (optional): `fault` — a first setup() fails at the
// `built` step (after the exporters loaded grpc-js), then setup() again;
// `resetup` — setup(), shutdown(), setup().
for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_') || k === 'SERVICE_NAME' || k === 'ENVIRONMENT') delete process.env[k];
process.env.OTEL_SERVICE_NAME = 'svc';
process.env.OTEL_HELPER_METRICS_PORT = '0';
process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'http://127.0.0.1:1';
process.env.OTEL_EXPORTER_OTLP_PROTOCOL = process.argv[2] ?? 'grpc';
process.env.OTEL_METRICS_EXPORTER = 'none';
process.env.OTEL_LOGS_EXPORTER = 'none';

const { InstrumentationBase, InstrumentationNodeModuleDefinition } = require('@opentelemetry/instrumentation');
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const reports = [];
require('@staffops/otel-helper/testing').setDiagnosticSink((line, meta) => { if (meta.origin === 'helper') reports.push(line); });
const helper = require('@staffops/otel-helper');

/** Stands in for an opt-in factory's instrumentation: built (and so enabled) by its constructor, before setup(). */
class OptIn extends InstrumentationBase {
  constructor() {
    super('a39-opt-in', '0.0.0', {});
  }
  init() {
    return [new InstrumentationNodeModuleDefinition('a39-not-installed', ['*'])];
  }
}

const optIn = new OptIn();
// A fresh exporter per setup(): shutdown() shuts the previous one down.
let exporter;
const options = () => {
  exporter = new InMemorySpanExporter();
  return { instrumentations: [optIn], spanProcessors: [new SimpleSpanProcessor({ exporter })] };
};
const mode = process.argv[3];

async function start() {
  if (mode === 'fault') {
    const { setupFault } = require('../../dist/internal/setup.js');
    setupFault.at = (step) => {
      if (step === 'built') throw new Error('injected');
    };
    try {
      helper.setup(options());
    } catch {
      // Expected: the injected failure rolls the first setup back.
    }
    setupFault.at = undefined;
  } else if (mode === 'resetup') {
    helper.setup(options());
    await helper.shutdown(5_000);
  }
  helper.setup(options());
}

start().then(run, (err) => {
  console.error(err);
  process.exit(1);
});

function run() {
  const http = require('node:http');
  const express = require('express');
  const grpc = require('@grpc/grpc-js');

  const identity = (b) => b;
  const method = { path: '/a39.v1.Orders/Place', requestStream: false, responseStream: false, requestSerialize: identity, requestDeserialize: identity, responseSerialize: identity, responseDeserialize: identity };
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
            const spans = exporter.getFinishedSpans().map((s) => ({ name: s.name, kind: s.kind, scope: s.instrumentationScope.name }));
            console.log(JSON.stringify({ spans, reports }));
            process.exit(0);
          });
        });
      });
    });
  });
}
