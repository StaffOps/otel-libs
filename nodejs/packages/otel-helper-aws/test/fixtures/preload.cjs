'use strict';
// sdk.test.cjs fixture: the SQS client loaded — and a client built — before
// the instrumentation exists (FIXTURE_NO_PRELOAD=1: after setup();
// FIXTURE_DYNAMODB=1: a DynamoDB GetItem too, for the semconv mode test).
// Calls go to AWS_FAKE_URL with fake credentials.
const { clientConfig } = require('./aws-fake.cjs');

const fake = { url: process.env.AWS_FAKE_URL };
const preload = process.env.FIXTURE_NO_PRELOAD !== '1';
const early = preload ? require('@aws-sdk/client-sqs') : undefined;
const earlyClient = early === undefined ? undefined : new early.SQSClient(clientConfig(fake));
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const { awsInstrumentation } = require('@staffops/otel-helper-aws');

const exporter = new InMemorySpanExporter();
helper.setup({ instrumentations: [awsInstrumentation()], spanProcessors: [new SimpleSpanProcessor({ exporter })] });
const sqs = early ?? require('@aws-sdk/client-sqs');
const client = earlyClient ?? new sqs.SQSClient(clientConfig(fake));

(async () => {
  await client.send(new sqs.SendMessageCommand({ QueueUrl: `${fake.url}/123456789012/early`, MessageBody: 'm' }));
  if (process.env.FIXTURE_DYNAMODB === '1') {
    const ddb = require('@aws-sdk/client-dynamodb');
    await new ddb.DynamoDBClient(clientConfig(fake)).send(new ddb.GetItemCommand({ TableName: 'Orders', Key: { pk: { S: 'k' } } }));
  }
  const spans = exporter.getFinishedSpans().filter((s) => s.kind !== 1).map((s) => ({ name: s.name, scope: s.instrumentationScope.name, attributes: s.attributes }));
  await helper.shutdown();
  console.log(JSON.stringify({ preloaded: preload, spans }));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
