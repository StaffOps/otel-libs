// Node smoke program: emits one span, one metric and one log record through
// the helper and exits; testing/e2e/smoke.sh checks the Collector received all
// three. Same markers as the Go smoke program (testing/e2e/go-smoke/main.go):
// span "e2e-smoke-span", counter "e2e.smoke.runs", WARN record "e2e-smoke-log".
// The Node examples (task B11b) do not exist yet, so this program stands in
// for them the way go-smoke stands in for the Go examples.
'use strict';

const { context, metrics, trace } = require('@opentelemetry/api');
const { createLogger, setup, shutdown } = require('@staffops/otel-helper');

async function main() {
  setup();

  const span = trace.getTracer('e2e-node-smoke').startSpan('e2e-smoke-span');
  const ctx = trace.setSpan(context.active(), span);
  context.with(ctx, () => {
    metrics.getMeter('e2e-node-smoke').createCounter('e2e.smoke.runs').add(1);
    createLogger({ name: 'e2e-node-smoke' }).warn('e2e-smoke-log');
  });
  span.end();

  await shutdown(10_000);
  console.error('node-smoke: emitted span, metric and log');
}

main().catch((err) => {
  console.error('node-smoke:', err);
  process.exitCode = 1;
});
