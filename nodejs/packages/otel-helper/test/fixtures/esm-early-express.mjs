// Fixture for test/instrumentation.test.cjs (B6 re-probe M1): express is
// imported through ESM before setup(); prints whether it is patched and how
// many "loaded before setup()" reports the helper wrote.
import express from 'express';

for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_') && k !== 'OTEL_SERVICE_NAME' && k !== 'OTEL_HELPER_METRICS_PORT') delete process.env[k];
const testing = await import('@staffops/otel-helper/testing');
const lines = [];
testing.setDiagnosticSink((line) => lines.push(line));
const helper = await import('@staffops/otel-helper');
helper.setup();
console.log(JSON.stringify({ patched: express.application.use.__wrapped === true, reports: lines.filter((l) => l.includes('was loaded before setup()')).length }));
process.exit(0);
