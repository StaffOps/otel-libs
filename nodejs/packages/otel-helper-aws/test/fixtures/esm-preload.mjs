// sdk.test.cjs fixture (design D-JS11): a custom ESM preload, as an app writes
// it instead of `--import @staffops/otel-helper/register` when it passes
// options in code. FIXTURE_NO_HOOKS=1 leaves the hooks out (the control).
import { InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace';

if (process.env.FIXTURE_NO_HOOKS !== '1') await import('@staffops/otel-helper/hooks');
const { setup } = await import('@staffops/otel-helper');
const { awsInstrumentation } = await import('@staffops/otel-helper-aws');

const exporter = new InMemorySpanExporter();
globalThis.__fixtureExporter = exporter;
setup({ instrumentations: [awsInstrumentation()], spanProcessors: [new SimpleSpanProcessor({ exporter })] });
