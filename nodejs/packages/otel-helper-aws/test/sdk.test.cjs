'use strict';
// @staffops/otel-helper-aws against the real AWS SDK v3 clients (S3,
// DynamoDB, SQS, SNS, Lambda) and an in-process fake endpoint
// (fixtures/aws-fake.cjs; fake credentials only) — task B10c, spec JS8,
// design D-JS9/D-JS14, contract D14/A25: the emitted attributes per service,
// service errors and throttling (one span per send(), retries included;
// ERROR without description and the AWS error code as error.type), no HTTP
// span under the SDK span, the secrets sweep (access key, secret, session
// token, signature, presigned query, bodies), a re-setup, an SDK loaded
// before setup(), an ESM custom preload and the database semconv mode.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { SpanKind, SpanStatusCode } = require('@opentelemetry/api');
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { awsInstrumentation } = require('@staffops/otel-helper-aws');
const { startAwsFake, CREDENTIALS, clientConfig } = require('./fixtures/aws-fake.cjs');
const { RESIDUAL, SECRETS, runScenario, spansOf, view } = require('./fixtures/scenario.cjs');
const scrape = require('./fixtures/scrape.cjs');

const PKG_DIR = path.resolve(__dirname, '..');
const FIXTURES = path.join(__dirname, 'fixtures');
const AWS_SCOPE = '@opentelemetry/instrumentation-aws-sdk';

// Created before any @aws-sdk client loads (as an app does).
const instrumentation = awsInstrumentation();

let saved;
let exporter;
test.beforeEach(() => {
  saved = { ...process.env };
  for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_') || k.startsWith('AWS_')) delete process.env[k];
  process.env.OTEL_SERVICE_NAME = 'svc';
  process.env.OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=DEV';
  process.env.OTEL_HELPER_METRICS_PORT = '0';
  testing.reset();
  testing.setDiagnosticSink(() => undefined);
  exporter = new InMemorySpanExporter();
});
test.afterEach(async () => {
  await helper.shutdown().catch(() => undefined);
  testing.reset();
  for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
  Object.assign(process.env, saved);
});

function setupWith(exp = exporter) {
  helper.setup({ instrumentations: [instrumentation], spanProcessors: [new SimpleSpanProcessor({ exporter: exp })] });
}

/** Runs `node <args>` asynchronously (the parent's fake must keep answering); resolves the JSON it prints last, stderr and status. */
function runNode(args, env = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, {
      cwd: PKG_DIR,
      env: { ...process.env, OTEL_SERVICE_NAME: 'svc', OTEL_RESOURCE_ATTRIBUTES: 'deployment.environment.name=DEV', OTEL_HELPER_METRICS_PORT: '0', ...env },
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => (stdout += d));
    child.stderr.on('data', (d) => (stderr += d));
    const timer = setTimeout(() => child.kill('SIGKILL'), 60_000);
    child.on('close', (status) => {
      clearTimeout(timer);
      try {
        resolve({ ...JSON.parse(stdout.trim().split('\n').pop()), stderr, status });
      } catch {
        reject(new Error(`child status=${status}\nstdout:\n${stdout}\nstderr:\n${stderr}`));
      }
    });
  });
}

const rpc = (service, method) => ({ 'rpc.system': 'aws-api', 'rpc.service': service, 'rpc.method': method, 'cloud.region': 'us-east-1', 'aws.region': 'us-east-1' });

/** `attrs` contains every entry of `want`. */
function assertHas(span, want) {
  for (const [k, v] of Object.entries(want)) assert.deepEqual(span.attributes[k], v, `${span.name} ${k} (${JSON.stringify(span.attributes)})`);
}

test('JS8 every service: one span per send(), the contrib semconv plus aws.region/aws.request_id, ERROR without description, no HTTP span under it', async () => {
  const fake = await startAwsFake();
  try {
    setupWith();
    const { errors } = await runScenario(fake);
    assert.deepEqual(errors, {
      's3.missing': 'NoSuchKey',
      's3.denied': 'AccessDenied',
      's3.badsig': 'SignatureDoesNotMatch',
      'ddb.missing': 'ResourceNotFoundException',
      'ddb.throttled': 'ThrottlingException',
      'ddb.quoting': 'ValidationException',
      'sqs.missing': 'QueueDoesNotExist',
      'sns.invalid': 'InvalidParameterException',
      'sns.missing': 'NotFoundException',
      'lambda.error': 'ServiceException',
    });
    const spans = spansOf(exporter).map(view);
    // The HTTP requests the SDK made are not traced under its spans (suppressInternalInstrumentation; Go and Python too).
    assert.deepEqual([...new Set(spans.map((s) => s.scope))], [AWS_SCOPE], JSON.stringify(spans.map((s) => [s.scope, s.name])));
    assert.equal(spans.length, 21, 'one span per send(), retries included');
    const byOrder = (name) => spans.filter((s) => s.name === name);
    const [put, get, missing, denied, badsig] = [...byOrder('S3.PutObject'), ...byOrder('S3.GetObject')];
    for (const s of [put, get, missing, denied, badsig]) assert.equal(s.kind, SpanKind.CLIENT);
    assertHas(put, { ...rpc('S3', 'PutObject'), 'aws.s3.bucket': 'bucket-1', 'http.response.status_code': 200 });
    assert.equal(put.attributes['aws.request_id'], put.attributes['aws.request.id']);
    assert.match(put.attributes['aws.request_id'], /^req-\d+$/);
    assert.equal(put.attributes['aws.extended_request_id'], put.attributes['aws.request.extended_id'], 'the S3 host id, semconv key too');
    assert.match(put.attributes['aws.extended_request_id'], /^host-req-\d+$/);
    assert.deepEqual(put.status, { code: SpanStatusCode.UNSET });
    assertHas(get, rpc('S3', 'GetObject'));
    for (const [span, code, status] of [[missing, 'NoSuchKey', 404], [denied, 'AccessDenied', 403], [badsig, 'SignatureDoesNotMatch', 403]]) {
      assert.deepEqual(span.status, { code: SpanStatusCode.ERROR }, `${code}: no description (A25)`);
      assertHas(span, { 'error.type': code, 'http.response.status_code': status });
      assert.match(span.attributes['aws.request_id'], /^req-\d+$/);
      assert.equal(span.events.filter((e) => e.name === 'exception').length, 1, 'the semconv exception event is kept');
    }
    const ddb = byOrder('DynamoDB.PutItem').concat(byOrder('DynamoDB.GetItem'));
    const [ddbPut, ddbQuoting, ddbGet, ddbMissing, ddbThrottled, ddbOnce] = ddb;
    assertHas(ddbPut, { ...rpc('DynamoDB', 'PutItem'), 'db.system.name': 'aws.dynamodb', 'db.system': 'dynamodb', 'db.namespace': 'Orders', 'db.operation.name': 'PutItem', 'aws.dynamodb.table_names': ['Orders'] });
    assert.ok(ddbPut.attributes['aws.dynamodb.consumed_capacity'], 'capacity is a measurement: kept');
    assert.equal(ddbPut.attributes['aws.dynamodb.item_collection_metrics'], undefined, 'holds the partition-key value: dropped');
    assert.equal(ddbPut.attributes['db.query.text'], undefined, 'no statement (the request input)');
    assertHas(ddbGet, { 'aws.dynamodb.consistent_read': true, 'aws.dynamodb.table_names': ['Orders'] });
    assertHas(ddbMissing, { 'error.type': 'ResourceNotFoundException', 'aws.dynamodb.table_names': ['Missing'] });
    assertHas(ddbThrottled, { 'error.type': 'ThrottlingException', 'aws.dynamodb.table_names': ['Throttled'] });
    assert.deepEqual(ddbThrottled.status, { code: SpanStatusCode.ERROR });
    assertHas(ddbOnce, { 'aws.dynamodb.table_names': ['ThrottledOnce'], 'http.response.status_code': 200 });
    assert.deepEqual(ddbOnce.status, { code: SpanStatusCode.UNSET }, 'a throttled attempt retried successfully is not an error');
    assert.equal(ddbOnce.attributes['error.type'], undefined);
    const tables = (name) => fake.requests.filter((r) => r.body.includes(`"TableName":"${name}"`)).length;
    assert.equal(tables('Throttled'), 2, 'maxAttempts 2: two requests, one span');
    assert.equal(tables('ThrottledOnce'), 2);
    // A25 residual (as SQL and Redis): an error text that quotes a request value stays in the exception event only.
    assert.deepEqual(ddbQuoting.status, { code: SpanStatusCode.ERROR });
    assert.equal(ddbQuoting.attributes['error.type'], 'ValidationException');
    assert.ok(ddbQuoting.events[0].attributes['exception.message'].includes('quoted-in-error'));
    assert.ok(!JSON.stringify({ ...ddbQuoting, events: [] }).includes('quoted-in-error'), 'never in the status or the attributes');
    // A30: every AWS call span is named `<Service>.<Operation>`; SQS/SNS keep PRODUCER on send/publish and CONSUMER on receive (owner amendment 2026-11-08).
    const by = (name, key, value) => byOrder(name).filter((s) => s.attributes[key] === value);
    const [send] = by('SQS.SendMessage', 'messaging.destination.name', 'orders');
    const [sqsMissing] = by('SQS.SendMessage', 'messaging.destination.name', 'missing');
    const [receive] = byOrder('SQS.ReceiveMessage');
    assert.equal(send.kind, SpanKind.PRODUCER);
    assertHas(send, { ...rpc('SQS', 'SendMessage'), 'messaging.system': 'aws_sqs', 'messaging.destination.name': 'orders', 'messaging.message.id': 'msg-1', 'url.full': `${fake.url}/123456789012/orders` });
    assert.equal(receive.kind, SpanKind.CONSUMER);
    assertHas(receive, { 'messaging.operation.type': 'receive', 'messaging.batch.message_count': 1 });
    assert.deepEqual(receive.links, [{ traceId: send.traceId, attributes: { 'messaging.message.id': 'msg-1' } }], 'the producer context travels in the message attributes');
    assertHas(sqsMissing, { 'error.type': 'QueueDoesNotExist' });
    const [publish] = by('SNS.Publish', 'messaging.destination', 'alerts');
    const [phone, invalidPhone] = by('SNS.Publish', 'messaging.destination', 'phone_number:**');
    const [snsMissing] = by('SNS.Publish', 'messaging.destination', 'missing');
    assert.equal(publish.kind, SpanKind.PRODUCER);
    assertHas(publish, { ...rpc('SNS', 'Publish'), 'messaging.operation.name': 'Publish', 'messaging.system': 'aws.sns', 'messaging.destination.name': 'arn:aws:sns:us-east-1:123456789012:alerts', 'aws.sns.topic.arn': 'arn:aws:sns:us-east-1:123456789012:alerts' });
    assertHas(phone, { 'messaging.destination': 'phone_number:**', 'messaging.destination.name': 'phone_number:**' });
    assertHas(snsMissing, { 'error.type': 'NotFoundException' });
    // M2: SNS quotes an invalid number in its error message: attributes, name and status never carry it; the exception event does (the A25 residual).
    assertHas(invalidPhone, { 'error.type': 'InvalidParameterException', 'messaging.destination.name': 'phone_number:**' });
    assert.ok(invalidPhone.events[0].attributes['exception.message'].includes('+15555550000'));
    assert.ok(!JSON.stringify({ ...invalidPhone, events: [] }).includes('+15555550000'));
    const [invoke] = by('Lambda.Invoke', 'faas.invoked_name', 'resize');
    const [lambdaError] = by('Lambda.Invoke', 'faas.invoked_name', 'boom');
    // L5: a function that threw (200 + X-Amz-Function-Error: Unhandled) is a successful AWS call: not an
    // ERROR span (as Python botocore), and the error payload is never recorded.
    const [unhandled] = by('Lambda.Invoke', 'faas.invoked_name', 'unhandled');
    assert.deepEqual(unhandled.status, { code: SpanStatusCode.UNSET });
    assertHas(unhandled, { 'http.response.status_code': 200, 'faas.invoked_name': 'unhandled' });
    assert.equal(unhandled.attributes['error.type'], undefined);
    assertHas(invoke, { ...rpc('Lambda', 'Invoke'), 'faas.invoked_name': 'resize', 'faas.invoked_provider': 'aws', 'faas.execution': invoke.attributes['aws.request_id'] });
    assertHas(lambdaError, { 'error.type': 'ServiceException', 'http.response.status_code': 500 });
    for (const s of spans) if (s.status.code === SpanStatusCode.ERROR) assert.equal(s.status.message, undefined, `${s.name}: no status description`);
  } finally {
    await fake.close();
  }
});

test('A27: body-less HeadObject errors give the status table name (NotFound, Forbidden, ServiceUnavailable), as Go and Python', async () => {
  const fake = await startAwsFake();
  const s3 = require('@aws-sdk/client-s3');
  try {
    setupWith();
    const client = new s3.S3Client({ ...clientConfig(fake, { maxAttempts: 1 }), forcePathStyle: true });
    const names = [];
    for (const [Bucket, Key] of [['bucket-1', 'missing'], ['denied', 'k'], ['bucket-1', 'unavailable']]) {
      await client.send(new s3.HeadObjectCommand({ Bucket, Key })).catch((err) => names.push(err.name));
    }
    const types = spansOf(exporter).filter((s) => s.name === 'S3.HeadObject').map((s) => [s.status.code, s.attributes['error.type']]);
    assert.deepEqual(types, [[SpanStatusCode.ERROR, 'NotFound'], [SpanStatusCode.ERROR, 'Forbidden'], [SpanStatusCode.ERROR, 'ServiceUnavailable']], `SDK error names: ${names}`);
  } finally {
    await fake.close();
  }
});

test('JS8 secrets sweep: no credential, signature, presigned query value or body in any span the app records', async () => {
  const fake = await startAwsFake();
  try {
    const diagnostics = [];
    testing.setDiagnosticSink((line) => diagnostics.push(line));
    process.env.OTEL_LOG_LEVEL = 'DEBUG';
    setupWith();
    await runScenario(fake);
    // A presigned-style URL fetched by the app: the undici span (core) redacts the signature parameters.
    const signature = 'a'.repeat(64);
    const presigned = `${fake.url}/bucket-1/object-1?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=${CREDENTIALS.accessKeyId}%2F20261103%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20261103T000000Z&X-Amz-Expires=60&X-Amz-SignedHeaders=host&X-Amz-Security-Token=${encodeURIComponent(CREDENTIALS.sessionToken)}&X-Amz-Signature=${signature}`;
    await (await fetch(presigned)).text();
    const spans = spansOf(exporter).map(view);
    const fetched = spans.find((s) => s.scope === '@opentelemetry/instrumentation-undici');
    assert.ok(fetched, 'the app fetch outside an SDK call is still traced');
    assert.match(fetched.attributes['url.full'], /X-Amz-Signature=REDACTED/);
    // What the clients really sent: the credentials and bodies reached the fake.
    const signatures = fake.requests.map((r) => /Signature=([0-9a-f]+)/.exec(r.headers.authorization ?? '')?.[1]).filter(Boolean);
    assert.ok(signatures.length >= 19, `the SDK signed its requests (${signatures.length})`);
    assert.ok(fake.requests.every((r) => r.headers['x-amz-security-token'] === CREDENTIALS.sessionToken || r.url.includes('X-Amz-Signature')), 'the session token was sent');
    assert.ok(fake.requests.some((r) => (r.headers.authorization ?? '').includes(CREDENTIALS.accessKeyId)));
    for (const body of ['secret-upload-body', 'secret-partition-key', 'secret-message-body', 'secret-sns-message', 'secret-lambda-payload']) {
      assert.ok(fake.requests.some((r) => r.body.includes(body)), `the client sent ${body}`);
    }
    // M3: the diagnostics and the metrics the helper serves, too.
    const metrics = await scrape(helper);
    assert.match(metrics, /http_server_request_duration_seconds/, 'the scrape works (the fake server is instrumented)');
    const telemetry = JSON.stringify(spans) + diagnostics.join('\n') + metrics;
    const withoutExceptionEvents = JSON.stringify(spans.map((s) => ({ ...s, events: s.events.filter((e) => e.name !== 'exception') }))) + diagnostics.join('\n') + metrics;
    for (const value of RESIDUAL) {
      assert.ok(!withoutExceptionEvents.includes(value), `only in an exception event: ${value}`);
      assert.ok(spans.some((s) => s.events.some((e) => String(e.attributes['exception.message']).includes(value))), `the A25 residual: ${value}`);
    }
    const forbidden = [
      CREDENTIALS.accessKeyId,
      CREDENTIALS.secretAccessKey,
      CREDENTIALS.sessionToken,
      encodeURIComponent(CREDENTIALS.sessionToken),
      signature,
      ...new Set(signatures),
      ...SECRETS,
    ];
    for (const value of forbidden) assert.ok(!telemetry.includes(value), `never recorded: ${value.slice(0, 12)}…`);
  } finally {
    await fake.close();
  }
});

test('M1 (documented gap): a GetObject Body listener runs in the suppressed SDK HTTP context; a saved context restores tracing', async () => {
  const api = require('@opentelemetry/api');
  const fake = await startAwsFake();
  const s3 = require('@aws-sdk/client-s3');
  try {
    setupWith();
    const client = new s3.S3Client({ ...clientConfig(fake), forcePathStyle: true });
    const read = async (path, wrap) => {
      const res = await client.send(new s3.GetObjectCommand({ Bucket: 'bucket-1', Key: 'object-1' }));
      const saved = api.context.active();
      await new Promise((resolve, reject) => {
        res.Body.on('data', () => undefined);
        const call = () => fetch(`${fake.url}/${path}`).then((r) => r.text()).then(resolve, reject);
        res.Body.on('end', wrap ? () => api.context.with(saved, call) : call);
      });
    };
    await read('in-listener', false);
    await read('with-saved-context', true);
    const fetched = spansOf(exporter).filter((s) => s.instrumentationScope.name === '@opentelemetry/instrumentation-undici').map((s) => s.attributes['url.full']);
    // instrumentation-http binds the response (the Body) to the request's context, which
    // suppressInternalInstrumentation suppressed: not re-bindable (context-async-hooks keeps the first bind).
    assert.ok(!fetched.some((u) => u.endsWith('/in-listener')), `untraced in the listener: ${fetched}`);
    assert.ok(fetched.some((u) => u.endsWith('/with-saved-context')), `traced with the saved context: ${fetched}`);
  } finally {
    await fake.close();
  }
});

test('re-setup: shutdown() stops the spans of clients built earlier; the next setup() reports them to the new providers', async () => {
  const fake = await startAwsFake();
  const sqs = require('@aws-sdk/client-sqs');
  // A processor that keeps every ended span (InMemorySpanExporter clears itself on shutdown).
  const ended = [];
  const keep = { onStart: () => undefined, onEnd: (s) => ended.push(s.name), forceFlush: () => Promise.resolve(), shutdown: () => Promise.resolve() };
  try {
    helper.setup({ instrumentations: [instrumentation], spanProcessors: [keep] });
    const client = new sqs.SQSClient(clientConfig(fake));
    const send = () => client.send(new sqs.SendMessageCommand({ QueueUrl: `${fake.url}/123456789012/resetup`, MessageBody: 'm' }));
    await send();
    assert.deepEqual(ended.filter((n) => n === 'SQS.SendMessage'), ['SQS.SendMessage']);
    await helper.shutdown();
    await send();
    assert.deepEqual(ended.filter((n) => n === 'SQS.SendMessage'), ['SQS.SendMessage'], 'nothing after shutdown()');
    const next = new InMemorySpanExporter();
    setupWith(next);
    await send();
    assert.deepEqual(spansOf(next).map((s) => [s.name, s.instrumentationScope.name]), [['SQS.SendMessage', AWS_SCOPE]], 'the client built earlier reports again');
  } finally {
    await fake.close();
  }
});

test('an SDK loaded (and a client built) before awsInstrumentation() and setup() is still instrumented', async () => {
  const fake = await startAwsFake();
  try {
    const out = await runNode([path.join(FIXTURES, 'preload.cjs')], { AWS_FAKE_URL: fake.url });
    assert.equal(out.status, 0, out.stderr);
    assert.ok(out.preloaded, 'the SDK was in require.cache first');
    assert.deepEqual(out.spans.map((s) => [s.name, s.scope, s.attributes['rpc.method']]), [['SQS.SendMessage', AWS_SCOPE, 'SendMessage']]);
  } finally {
    await fake.close();
  }
});

test('ESM app with a custom preload (D-JS11): spans with the hooks; without them too (the SDK loads by require() inside its packages)', async () => {
  const fake = await startAwsFake();
  try {
    for (const noHooks of ['', '1']) {
      const out = await runNode(['--import', path.join(FIXTURES, 'esm-preload.mjs'), path.join(FIXTURES, 'esm-app.mjs')], { AWS_FAKE_URL: fake.url, FIXTURE_NO_HOOKS: noHooks });
      assert.equal(out.status, 0, out.stderr);
      if (noHooks === '') {
        assert.ok(out.hooks === 'sync' || out.hooks === 'async', `hooks mode ${out.hooks}`);
      } else {
        assert.equal(out.hooks, null);
        assert.match(out.stderr, /without the module hooks; packages imported after setup\(\) \(express, @grpc\/grpc-js\)/, 'the JS5 warning names no AWS package');
      }
      assert.deepEqual(out.spans.map((s) => [s.name, s.scope]), [['S3.PutObject', AWS_SCOPE]], `hooks=${noHooks === '' ? 'on' : 'off'}`);
    }
  } finally {
    await fake.close();
  }
});

test('OTEL_SEMCONV_STABILITY_OPT_IN=database: DynamoDB spans carry db.system.name only (D-JS9)', async () => {
  const fake = await startAwsFake();
  try {
    for (const [value, legacy] of [['database', undefined], ['', 'dynamodb']]) {
      const out = await runNode([path.join(FIXTURES, 'preload.cjs')], { AWS_FAKE_URL: fake.url, FIXTURE_NO_PRELOAD: '1', FIXTURE_DYNAMODB: '1', OTEL_SEMCONV_STABILITY_OPT_IN: value });
      assert.equal(out.status, 0, out.stderr);
      const [span] = out.spans.filter((s) => s.name === 'DynamoDB.GetItem');
      assert.equal(span.attributes['db.system.name'], 'aws.dynamodb', 'semconv v1.43 value (A30)');
      assert.equal(span.attributes['db.system'], legacy, JSON.stringify(value));
    }
  } finally {
    await fake.close();
  }
});

test('A30 OTEL_SEMCONV_STABILITY_OPT_IN rpc / rpc/dup: AWS RPC keys (legacy by default, as Go and Python)', async () => {
  const fake = await startAwsFake();
  try {
    for (const [value, want, absent] of [
      ['', { 'rpc.system': 'aws-api', 'rpc.service': 'SQS', 'rpc.method': 'SendMessage' }, ['rpc.system.name']],
      ['http, RPC ', { 'rpc.system.name': 'aws-api', 'rpc.method': 'SQS/SendMessage' }, ['rpc.system', 'rpc.service']],
      ['rpc,rpc/dup', { 'rpc.system.name': 'aws-api', 'rpc.system': 'aws-api', 'rpc.service': 'SQS', 'rpc.method': 'SQS/SendMessage' }, []],
    ]) {
      const out = await runNode([path.join(FIXTURES, 'preload.cjs')], { AWS_FAKE_URL: fake.url, FIXTURE_NO_PRELOAD: '1', OTEL_SEMCONV_STABILITY_OPT_IN: value });
      assert.equal(out.status, 0, out.stderr);
      const [span] = out.spans.filter((s) => s.name === 'SQS.SendMessage');
      for (const [k, v] of Object.entries(want)) assert.equal(span.attributes[k], v, `${JSON.stringify(value)} ${k}`);
      for (const k of absent) assert.equal(span.attributes[k], undefined, `${JSON.stringify(value)} ${k}`);
    }
  } finally {
    await fake.close();
  }
});
