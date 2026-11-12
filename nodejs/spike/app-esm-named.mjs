// ESM app, named imports from node:http.
import { createServer, get } from 'node:http';
import { trace } from '@opentelemetry/api';
import scenario from './http-scenario.cjs';
import stub from './setup-stub.cjs';

await scenario.run(createServer, get, trace.getTracer('spike-app'));
console.log(JSON.stringify({ spans: stub.summarize(globalThis.__spike.memory) }));
