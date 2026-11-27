'use strict';
// sdk.test.cjs fixture (task B10c): one call of every kind the tests cover,
// through the real AWS SDK v3 clients against the in-process fake
// (aws-fake.cjs) — S3, DynamoDB, SQS, SNS and Lambda, successes, service
// errors and throttling — carrying values the telemetry must never record
// (object, item, message and payload bodies, a phone number). `spansOf`
// keeps the spans the app records: the fake's own SERVER spans (it runs in
// the same process) are left out.
const { SpanKind } = require('@opentelemetry/api');
const { clientConfig } = require('./aws-fake.cjs');

/** Values sent or returned in bodies: none may appear in the telemetry. */
const SECRETS = Object.freeze([
  'secret-upload-body',
  'secret-object-body',
  'secret-partition-key',
  'secret-item-value',
  'secret-message-body',
  'secret-sns-message',
  'secret-lambda-payload',
  'secret-lambda-result',
  'secret-lambda-error',
  '+15555550123',
]);

/** Values an AWS error message quotes: only in the exception event (the A25 residual), never elsewhere. */
const RESIDUAL = Object.freeze(['+15555550000', 'quoted-in-error']);

/** Runs the calls; resolves `{ errors }`: the error name of each call that failed, by label. */
async function runScenario(fake) {
  const s3 = require('@aws-sdk/client-s3');
  const ddb = require('@aws-sdk/client-dynamodb');
  const sqs = require('@aws-sdk/client-sqs');
  const sns = require('@aws-sdk/client-sns');
  const lambda = require('@aws-sdk/client-lambda');
  const config = clientConfig(fake);
  const errors = {};
  const call = async (label, promise) => {
    try {
      await promise;
    } catch (err) {
      errors[label] = err.name;
    }
  };
  const S3 = new s3.S3Client({ ...config, forcePathStyle: true });
  await call('s3.put', S3.send(new s3.PutObjectCommand({ Bucket: 'bucket-1', Key: 'object-1', Body: 'secret-upload-body' })));
  await call('s3.get', S3.send(new s3.GetObjectCommand({ Bucket: 'bucket-1', Key: 'object-1' })).then((r) => r.Body.transformToString()));
  await call('s3.missing', S3.send(new s3.GetObjectCommand({ Bucket: 'bucket-1', Key: 'missing' })));
  await call('s3.denied', S3.send(new s3.GetObjectCommand({ Bucket: 'denied', Key: 'object-1' })));
  await call('s3.badsig', S3.send(new s3.GetObjectCommand({ Bucket: 'badsig', Key: 'object-1' })));
  const D = new ddb.DynamoDBClient(config);
  await call('ddb.put', D.send(new ddb.PutItemCommand({ TableName: 'Orders', Item: { pk: { S: 'secret-partition-key' }, v: { S: 'secret-item-value' } }, ReturnItemCollectionMetrics: 'SIZE', ReturnConsumedCapacity: 'TOTAL' })));
  await call('ddb.get', D.send(new ddb.GetItemCommand({ TableName: 'Orders', Key: { pk: { S: 'secret-partition-key' } }, ConsistentRead: true })));
  await call('ddb.missing', D.send(new ddb.GetItemCommand({ TableName: 'Missing', Key: { pk: { S: 'k' } } })));
  await call('ddb.throttled', D.send(new ddb.GetItemCommand({ TableName: 'Throttled', Key: { pk: { S: 'k' } } })));
  await call('ddb.throttledOnce', D.send(new ddb.GetItemCommand({ TableName: 'ThrottledOnce', Key: { pk: { S: 'k' } } })));
  await call('ddb.quoting', D.send(new ddb.PutItemCommand({ TableName: 'Quoting', Item: { pk: { S: 'quoted-in-error' } } })));
  const Q = new sqs.SQSClient(config);
  const queueUrl = `${fake.url}/123456789012/orders`;
  await call('sqs.send', Q.send(new sqs.SendMessageCommand({ QueueUrl: queueUrl, MessageBody: 'secret-message-body' })));
  await call('sqs.receive', Q.send(new sqs.ReceiveMessageCommand({ QueueUrl: queueUrl, MaxNumberOfMessages: 1 })));
  await call('sqs.missing', Q.send(new sqs.SendMessageCommand({ QueueUrl: `${fake.url}/123456789012/missing`, MessageBody: 'secret-message-body' })));
  const N = new sns.SNSClient(config);
  await call('sns.publish', N.send(new sns.PublishCommand({ TopicArn: 'arn:aws:sns:us-east-1:123456789012:alerts', Message: 'secret-sns-message' })));
  await call('sns.phone', N.send(new sns.PublishCommand({ PhoneNumber: '+15555550123', Message: 'secret-sns-message' })));
  await call('sns.invalid', N.send(new sns.PublishCommand({ PhoneNumber: '+15555550000', Message: 'secret-sns-message' })));
  await call('sns.missing', N.send(new sns.PublishCommand({ TopicArn: 'arn:aws:sns:us-east-1:123456789012:missing', Message: 'secret-sns-message' })));
  const L = new lambda.LambdaClient(config);
  await call('lambda.invoke', L.send(new lambda.InvokeCommand({ FunctionName: 'resize', Payload: Buffer.from('{"v":"secret-lambda-payload"}') })));
  await call('lambda.unhandled', L.send(new lambda.InvokeCommand({ FunctionName: 'unhandled', Payload: Buffer.from('{}') })).then((r) => Buffer.from(r.Payload).toString()));
  await call('lambda.error', L.send(new lambda.InvokeCommand({ FunctionName: 'boom', Payload: Buffer.from('{}') })));
  return { errors };
}

/** The spans the app records (the fake's SERVER spans left out), oldest first. */
function spansOf(exporter) {
  return exporter.getFinishedSpans().filter((s) => !(s.kind === SpanKind.SERVER && s.instrumentationScope.name === '@opentelemetry/instrumentation-http'));
}

/** A JSON-friendly view of a span. */
function view(span) {
  return {
    name: span.name,
    kind: span.kind,
    scope: span.instrumentationScope.name,
    status: span.status,
    attributes: span.attributes,
    events: span.events.map((e) => ({ name: e.name, attributes: e.attributes })),
    links: span.links.map((l) => ({ traceId: l.context.traceId, attributes: l.attributes })),
    traceId: span.spanContext().traceId,
  };
}

module.exports = { RESIDUAL, SECRETS, runScenario, spansOf, view };
