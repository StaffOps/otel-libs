// ESM app, default import of node:http.
import http from 'node:http';
import { trace } from '@opentelemetry/api';
import scenario from './http-scenario.cjs';
import stub from './setup-stub.cjs';

await scenario.run(http.createServer, http.get, trace.getTracer('spike-app'));
console.log(JSON.stringify({ spans: stub.summarize(globalThis.__spike.memory) }));
