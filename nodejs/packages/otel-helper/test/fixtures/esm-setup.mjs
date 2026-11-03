// Fixture for test/hooks.test.cjs (B9, spec JS5): an ES module entry point
// calling setup(), then shutdown() → setup() again (B9 fix round); prints the
// "without the module hooks" warnings it got (once per process).
const testing = await import('@staffops/otel-helper/testing');
const lines = [];
testing.setDiagnosticSink((line) => lines.push(line));
const helper = await import('@staffops/otel-helper');
helper.setup();
helper.setup({ serviceName: 'again' });
await helper.shutdown();
const resolved = helper.setup();
console.log(JSON.stringify({ warnings: lines.filter((l) => l.includes('without the module hooks')), resetup: resolved.serviceName }));
process.exit(0);
