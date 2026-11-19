'use strict';
// In-process fake AWS endpoint (task B10c): one HTTP server answering the
// S3 (REST-XML, path style), DynamoDB and SQS (JSON 1.0), SNS (Query) and
// Lambda (REST-JSON) calls the tests make, in each protocol's wire format, so
// the real AWS SDK v3 clients parse the answers. Every request is recorded
// (method, url, headers, body) so the tests can prove what the client sent and
// the spans did not record. Credentials are fake (never validated).
//
// Behavior by name: S3 key `missing` → 404 NoSuchKey, bucket `denied` → 403
// AccessDenied, bucket `badsig` → 403 SignatureDoesNotMatch (echoing the
// access key id and signature, as S3 does); DynamoDB table `Missing` →
// ResourceNotFoundException, `Throttled` → ThrottlingException on every
// attempt, `ThrottledOnce` → on the first; `Quoting` → a ValidationException
// whose message quotes the item value; SQS queue `missing` → QueueDoesNotExist;
// SNS topic `missing` → NotFound, phone number `+15555550000` → InvalidParameter
// quoting the number (as SNS does); Lambda function `boom` → 500
// ServiceException, `unhandled` → 200 with `X-Amz-Function-Error: Unhandled`
// (the function threw; the payload is its error).
const crypto = require('node:crypto');
const http = require('node:http');

const xml = (body) => `<?xml version="1.0" encoding="UTF-8"?>${body}`;
const md5 = (s) => crypto.createHash('md5').update(s, 'utf8').digest('hex');

function startAwsFake() {
  const requests = [];
  const queues = new Map();
  const attempts = new Map();
  let n = 0;
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const body = Buffer.concat(chunks).toString('utf8');
      requests.push({ method: req.method, url: req.url, headers: req.headers, body });
      const id = `req-${++n}`;
      const send = (status, headers, payload = '') => {
        res.writeHead(status, { 'x-amzn-requestid': id, 'x-amz-request-id': id, 'content-length': Buffer.byteLength(payload), ...headers });
        res.end(payload);
      };
      const json = (status, value, headers = {}) => send(status, { 'content-type': 'application/x-amz-json-1.0', ...headers }, JSON.stringify(value));
      const target = req.headers['x-amz-target'] ?? '';
      if (target.startsWith('DynamoDB_20120810.')) return dynamodb(target.slice(18), JSON.parse(body || '{}'), json);
      if (target.startsWith('AmazonSQS.')) return sqs(target.slice(10), JSON.parse(body || '{}'), json);
      if (req.url.startsWith('/2015-03-31/functions/')) return lambda(req.url, body, send);
      if (req.method === 'POST' && req.url === '/' && /Action=Publish/.test(body)) return sns(new URLSearchParams(body), id, send);
      return s3(req, body, id, send);
    });
  });

  function s3(req, body, id, send) {
    const [, bucket = '', ...rest] = decodeURI(req.url.split('?', 1)[0]).split('/');
    const key = rest.join('/');
    const error = (status, code, message, extra = '') =>
      send(status, { 'content-type': 'application/xml', 'x-amz-id-2': `host-${id}` }, xml(`<Error><Code>${code}</Code><Message>${message}</Message>${extra}<RequestId>${id}</RequestId><HostId>host-${id}</HostId></Error>`));
    if (req.method === 'HEAD') {
      // Body-less errors, as S3 HeadObject: 403 denied, 503 key "unavailable", 404 key "missing".
      const status = bucket === 'denied' ? 403 : key === 'unavailable' ? 503 : key === 'missing' ? 404 : 200;
      return send(status, { 'x-amz-id-2': `host-${id}` });
    }
    if (bucket === 'denied') return error(403, 'AccessDenied', 'Access Denied');
    if (bucket === 'badsig') {
      const auth = req.headers.authorization ?? '';
      const keyId = /Credential=([^/]+)/.exec(auth)?.[1] ?? '';
      const signature = /Signature=([0-9a-f]+)/.exec(auth)?.[1] ?? '';
      return error(403, 'SignatureDoesNotMatch', 'The request signature we calculated does not match the signature you provided. Check your key and signing method.',
        `<AWSAccessKeyId>${keyId}</AWSAccessKeyId><StringToSign>AWS4-HMAC-SHA256</StringToSign><SignatureProvided>${signature}</SignatureProvided>`);
    }
    if (key === 'missing') return error(404, 'NoSuchKey', 'The specified key does not exist.', `<Key>${key}</Key>`);
    if (req.method === 'PUT') return send(200, { etag: '"etag-1"', 'x-amz-id-2': `host-${id}` });
    if (req.method === 'GET') return send(200, { 'content-type': 'text/plain', etag: '"etag-1"', 'x-amz-id-2': `host-${id}` }, 'secret-object-body');
    return send(200, {});
  }

  function dynamodb(operation, input, json) {
    const table = input.TableName ?? '';
    const fail = (type, message) => json(400, { __type: `com.amazonaws.dynamodb.v20120810#${type}`, message });
    if (table === 'Missing') return fail('ResourceNotFoundException', 'Requested resource not found');
    const seen = (attempts.get(table) ?? 0) + 1;
    attempts.set(table, seen);
    if (table === 'Throttled' || (table === 'ThrottledOnce' && seen === 1)) return fail('ThrottlingException', 'Rate of requests exceeds the allowed throughput.');
    if (table === 'Quoting') return fail('ValidationException', `One or more parameter values were invalid: ${JSON.stringify(input.Item ?? {})}`);
    if (operation === 'GetItem') return json(200, { Item: { pk: { S: 'k1' }, secret: { S: 'secret-item-value' } } });
    if (operation === 'PutItem') {
      return json(200, {
        ConsumedCapacity: { TableName: table, CapacityUnits: 1 },
        ItemCollectionMetrics: { ItemCollectionKey: { pk: { S: 'secret-partition-key' } }, SizeEstimateRangeGB: [0, 1] },
      });
    }
    return json(200, {});
  }

  function sqs(operation, input, json) {
    const name = String(input.QueueUrl ?? '').split('/').pop();
    if (name === 'missing') {
      return json(400, { __type: 'com.amazonaws.sqs#QueueDoesNotExist', message: 'The specified queue does not exist.' }, { 'x-amzn-query-error': 'AWS.SimpleQueueService.NonExistentQueue;Sender' });
    }
    const queue = queues.get(name) ?? [];
    queues.set(name, queue);
    if (operation === 'SendMessage') {
      const message = { MessageId: `msg-${queue.length + 1}`, ReceiptHandle: `rh-${queue.length + 1}`, Body: input.MessageBody, MD5OfBody: md5(input.MessageBody), MessageAttributes: input.MessageAttributes };
      queue.push(message);
      return json(200, { MessageId: message.MessageId, MD5OfMessageBody: message.MD5OfBody });
    }
    if (operation === 'ReceiveMessage') return json(200, { Messages: queue.splice(0) });
    return json(200, {});
  }

  function sns(params, id, send) {
    const topic = params.get('TopicArn') ?? '';
    const phone = params.get('PhoneNumber') ?? '';
    if (phone === '+15555550000') {
      return send(400, { 'content-type': 'text/xml' }, xml(`<ErrorResponse><Error><Type>Sender</Type><Code>InvalidParameter</Code><Message>Invalid parameter: PhoneNumber Reason: ${phone} is not valid to publish to</Message></Error><RequestId>${id}</RequestId></ErrorResponse>`));
    }
    if (topic.endsWith(':missing')) {
      return send(404, { 'content-type': 'text/xml' }, xml(`<ErrorResponse><Error><Type>Sender</Type><Code>NotFound</Code><Message>Topic does not exist</Message></Error><RequestId>${id}</RequestId></ErrorResponse>`));
    }
    return send(200, { 'content-type': 'text/xml' },
      xml(`<PublishResponse xmlns="http://sns.amazonaws.com/doc/2010-03-31/"><PublishResult><MessageId>sns-${id}</MessageId></PublishResult><ResponseMetadata><RequestId>${id}</RequestId></ResponseMetadata></PublishResponse>`));
  }

  function lambda(url, body, send) {
    const name = decodeURIComponent(url.split('/')[3] ?? '');
    if (name === 'unhandled') return send(200, { 'content-type': 'application/json', 'x-amz-function-error': 'Unhandled', 'x-amz-executed-version': '$LATEST' }, JSON.stringify({ errorType: 'Error', errorMessage: 'secret-lambda-error' }));
    if (name === 'boom') return send(500, { 'content-type': 'application/json', 'x-amzn-errortype': 'ServiceException' }, JSON.stringify({ Type: 'Service', message: 'Lambda was unable to run the function.' }));
    return send(200, { 'content-type': 'application/json', 'x-amz-executed-version': '$LATEST' }, JSON.stringify({ echoed: 'secret-lambda-result', got: body.length }));
  }

  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      resolve({
        url: `http://127.0.0.1:${port}`,
        requests,
        close: () => new Promise((done) => server.close(() => done())),
      });
    });
  });
}

/** Fake credentials only (never real): the sweep looks for each of these in the telemetry. */
const CREDENTIALS = Object.freeze({ // copied per client: the SDK writes `$source` on the object it gets
  accessKeyId: 'AKIAEXAMPLEFAKEKEY01',
  secretAccessKey: 'fakeSecretAccessKeyExampleOnly0000000000',
  sessionToken: 'FakeSessionTokenExampleOnly/abc+def==',
});

/** Client configuration for the fake (S3 adds `forcePathStyle: true`); two attempts at most. */
function clientConfig(fake, extra = {}) {
  return { region: 'us-east-1', endpoint: fake.url, credentials: { ...CREDENTIALS }, maxAttempts: 2, ...extra };
}

module.exports = { startAwsFake, CREDENTIALS, clientConfig };
