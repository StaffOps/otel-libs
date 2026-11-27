'use strict';
// Stand-in for the helper's setup() (B1c spike only), in the design D-JS1
// order: instrumentations first, then credentials/exporters (which load
// @grpc/grpc-js), then providers, context manager and propagator.
const api = require('@opentelemetry/api');

/**
 * @param {{ exporters?: () => import('@opentelemetry/sdk-trace').SpanExporter[], exportersFirst?: boolean }} [opts]
 *   exporters: factory called after registerInstrumentations (or before it when exportersFirst).
 */
function setupStub(opts = {}) {
  const loadedBefore = isLoaded('@grpc/grpc-js');
  let extra = [];
  if (opts.exportersFirst && opts.exporters) extra = opts.exporters();
  const { registerInstrumentations } = require('@opentelemetry/instrumentation');
  const { HttpInstrumentation } = require('@opentelemetry/instrumentation-http');
  const { GrpcInstrumentation } = require('@opentelemetry/instrumentation-grpc');
  registerInstrumentations({ instrumentations: [new HttpInstrumentation(), new GrpcInstrumentation()] });
  const grpcLoadedAtRegister = isLoaded('@grpc/grpc-js');
  if (!opts.exportersFirst && opts.exporters) extra = opts.exporters();

  const { TracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
  const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
  const { W3CTraceContextPropagator } = require('@opentelemetry/core');
  const memory = new InMemorySpanExporter();
  const provider = new TracerProvider({ spanProcessors: [new SimpleSpanProcessor({ exporter: memory })] });
  api.context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
  api.propagation.setGlobalPropagator(new W3CTraceContextPropagator());
  api.trace.setGlobalTracerProvider(provider);
  return { provider, memory, exporters: extra, grpcLoadedBeforeSetup: loadedBefore, grpcLoadedAtRegister };
}

// True when any file of the package is already in the CommonJS cache.
function isLoaded(pkg) {
  const marker = `/node_modules/${pkg}/`;
  return Object.keys(require.cache).some((f) => f.includes(marker));
}

/** Compact view of the in-memory spans for the runner. */
function summarize(memory) {
  return memory.getFinishedSpans().map((s) => ({
    name: s.name,
    kind: api.SpanKind[s.kind],
    traceId: s.spanContext().traceId,
    spanId: s.spanContext().spanId,
    parentSpanId: s.parentSpanContext?.spanId ?? null,
    scope: s.instrumentationScope.name,
  }));
}

module.exports = { setupStub, summarize, isLoaded };
