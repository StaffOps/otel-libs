#!/bin/sh
# Install test (spec JS1, D-JS2): pack the built core (and the opt-in -sql,
# -redis and -aws packages, tasks B10a/B10b/B10c: their contrib instrumentations must share
# the core's @opentelemetry/instrumentation and core), install them in a fresh
# consumer next to the NEWEST @opentelemetry/api its peer range admits and the
# app's own @grpc/grpc-js, check the tree is valid, that the packages which
# must be shared have exactly one copy (`npm ls` exits 0 with duplicates, so
# paths are counted), and that both `require` and `import` load the core.
# The -pino and -winston packages (B10d fix round, 2026-11-03) are installed
# next to their peer floors, pino 10.0.0 and winston 3.10.0, and must write a
# record through the library at that floor.
# Needs the npm registry; NODE_TESTS_OFFLINE=1 skips it. Run from nodejs/
# after `npm ci && npm run build`:
#   sh ci/api-range-install.sh
set -eu
if [ "${NODE_TESTS_OFFLINE:-}" = 1 ]; then
  echo "api-range install test: SKIPPED (NODE_TESTS_OFFLINE=1)"
  exit 0
fi
ws="$(pwd)"
range="$(node -p "require('./packages/otel-helper/package.json').peerDependencies['@opentelemetry/api']")"
newest="$(npm view "@opentelemetry/api@$range" version --json | node -e '
  let s = ""; process.stdin.on("data", (d) => (s += d)).on("end", () => {
    const v = JSON.parse(s); console.log(Array.isArray(v) ? v[v.length - 1] : v);
  });')"
echo "api peer range: $range -> newest admitted: $newest"

dir="$(mktemp -d)"
trap 'rm -rf "$dir"' EXIT
npm pack ./packages/otel-helper ./packages/otel-helper-sql ./packages/otel-helper-redis ./packages/otel-helper-aws ./packages/otel-helper-pino ./packages/otel-helper-winston \
  --pack-destination "$dir" --loglevel=error >/dev/null
cd "$dir"
printf '{ "name": "api-range-consumer", "private": true }\n' > package.json
npm install --ignore-scripts --no-audit --no-fund --loglevel=error ./staffops-otel-helper-[0-9]*.tgz ./staffops-otel-helper-sql-*.tgz \
  ./staffops-otel-helper-redis-*.tgz ./staffops-otel-helper-aws-*.tgz ./staffops-otel-helper-pino-*.tgz ./staffops-otel-helper-winston-*.tgz \
  "@opentelemetry/api@$newest" "@grpc/grpc-js@^1.14.5" "pg@^8" "redis@^6" "ioredis@^6" "@aws-sdk/client-s3@^3" "@aws-sdk/client-sqs@^3" "pino@10.0.0" "winston@3.10.0"
npm ls --all >/dev/null
# The api package does not export ./package.json; read the installed file.
installed="$(node -p "JSON.parse(require('fs').readFileSync('node_modules/@opentelemetry/api/package.json', 'utf8')).version")"
echo "installed api: $installed"
[ "$installed" = "$newest" ] || { echo "FAIL: installed api $installed, expected $newest"; exit 1; }
if [ "${PLANT_DUPLICATE:-}" = 1 ]; then  # self-test of the copy count (review aid)
  mkdir -p node_modules/@opentelemetry/core/node_modules/import-in-the-middle
  cp node_modules/import-in-the-middle/package.json node_modules/@opentelemetry/core/node_modules/import-in-the-middle/
fi
status=0
for p in @opentelemetry/api @opentelemetry/core @opentelemetry/instrumentation \
         @opentelemetry/otlp-grpc-exporter-base @grpc/grpc-js import-in-the-middle; do
  n="$(find . -path "*/node_modules/$p/package.json" | wc -l | tr -d ' ')"
  echo "copies of $p: $n"
  [ "$n" = 1 ] || status=1
done
[ "$status" = 0 ] || { echo "FAIL: a package that must be shared is not installed exactly once"; exit 1; }
node -e "const h = require('@staffops/otel-helper'); if (!h.VERSION) process.exit(1)"
node --input-type=module -e "import { VERSION } from '@staffops/otel-helper'; if (!VERSION) process.exit(1)"
# The installed -sql really instruments the installed pg (L-g, 2026-11-03).
OTEL_HELPER_METRICS_PORT=0 node -e "
  require.resolve('pg');
  const { setup, shutdown } = require('@staffops/otel-helper');
  const i = require('@staffops/otel-helper-sql').sqlInstrumentation({ pg: true });
  setup({ serviceName: 'api-range', instrumentations: [i] });
  const pg = require('pg');
  if (pg.Client.prototype.query.__wrapped !== true) { console.error('FAIL: pg not instrumented'); process.exit(1); }
  shutdown().then(() => console.log('-sql instruments pg: OK'));
"
node --input-type=module -e "import { sqlInstrumentation } from '@staffops/otel-helper-sql'; if (typeof sqlInstrumentation !== 'function') process.exit(1)"
# The installed -redis really instruments the installed redis and ioredis (B10b):
# node-redis is patched in @redis/client (client/index.js), ioredis in its entry.
OTEL_HELPER_METRICS_PORT=0 node -e "
  const { setup, shutdown } = require('@staffops/otel-helper');
  const i = require('@staffops/otel-helper-redis').redisInstrumentation({ redis: true, ioredis: true });
  setup({ serviceName: 'api-range', instrumentations: [i] });
  require('redis');
  const client = require(require.resolve('@redis/client/dist/lib/client/index.js', { paths: [require.resolve('redis')] })).default;
  const IORedis = require('ioredis');
  if (client.prototype.sendCommand.__wrapped !== true) { console.error('FAIL: redis not instrumented'); process.exit(1); }
  if (IORedis.prototype.sendCommand.__wrapped !== true) { console.error('FAIL: ioredis not instrumented'); process.exit(1); }
  shutdown().then(() => console.log('-redis instruments redis and ioredis: OK'));
"
node --input-type=module -e "import { redisInstrumentation } from '@staffops/otel-helper-redis'; if (typeof redisInstrumentation !== 'function') process.exit(1)"
# The installed -aws really instruments the newest AWS SDK v3 clients (B10c): one
# SDK span per send() against a local endpoint (fake credentials), and no HTTP
# span under it.
OTEL_HELPER_METRICS_PORT=0 node -e "
  const http = require('node:http');
  const md5 = (s) => require('node:crypto').createHash('md5').update(s).digest('hex');
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d)).on('end', () => {
      const sqs = String(req.headers['x-amz-target'] || '').startsWith('AmazonSQS.');
      res.writeHead(200, { 'content-type': sqs ? 'application/x-amz-json-1.0' : 'application/xml', 'x-amzn-requestid': 'r', 'x-amz-request-id': 'r', etag: '\"e\"' });
      res.end(sqs ? JSON.stringify({ MessageId: 'm', MD5OfMessageBody: md5(JSON.parse(body).MessageBody) }) : '');
    });
  });
  const ended = [];
  const { setup, shutdown } = require('@staffops/otel-helper');
  setup({ serviceName: 'api-range', instrumentations: [require('@staffops/otel-helper-aws').awsInstrumentation()],
    spanProcessors: [{ onStart() {}, onEnd: (s) => ended.push(s.instrumentationScope.name + ' ' + s.kind + ' ' + s.name), forceFlush: async () => {}, shutdown: async () => {} }] });
  const { SQSClient, SendMessageCommand } = require('@aws-sdk/client-sqs');
  const { S3Client, PutObjectCommand } = require('@aws-sdk/client-s3');
  server.listen(0, '127.0.0.1', async () => {
    const endpoint = 'http://127.0.0.1:' + server.address().port;
    const config = () => ({ region: 'us-east-1', endpoint, credentials: { accessKeyId: 'AKIAEXAMPLE', secretAccessKey: 'fake-secret-example' } });
    await new SQSClient(config()).send(new SendMessageCommand({ QueueUrl: endpoint + '/123456789012/q', MessageBody: 'b' }));
    await new S3Client({ ...config(), forcePathStyle: true }).send(new PutObjectCommand({ Bucket: 'b', Key: 'k', Body: 'x' }));
    server.close();
    const sdk = ended.filter((e) => !e.startsWith('@opentelemetry/instrumentation-http 1 '));
    const want = ['@opentelemetry/instrumentation-aws-sdk 3 q send', '@opentelemetry/instrumentation-aws-sdk 2 S3.PutObject'];
    if (JSON.stringify(sdk) !== JSON.stringify(want)) { console.error('FAIL: -aws spans', JSON.stringify(ended)); process.exit(1); }
    await shutdown();
    console.log('-aws instruments @aws-sdk/client-sqs and client-s3: OK');
  });
"
node --input-type=module -e "import { awsInstrumentation } from '@staffops/otel-helper-aws'; if (typeof awsInstrumentation !== 'function') process.exit(1)"
# The log bridges at their peer floors write one stdout line each (no setup: the stdout fallback).
lines="$(node -e "
  const v = (p) => require(p + '/package.json').version;
  if (v('pino') !== '10.0.0' || v('winston') !== '3.10.0') { console.error('FAIL: floors not installed', v('pino'), v('winston')); process.exit(1); }
  require('pino')(require('@staffops/otel-helper-pino').createPinoDestination()).info('pino-floor');
  const winston = require('winston');
  const { OtelHelperTransport } = require('@staffops/otel-helper-winston');
  winston.createLogger({ transports: [new OtelHelperTransport()] }).info('winston-floor');
")"
echo "$lines" | grep -q '"msg":"pino-floor","logger":"pino"' || { echo "FAIL: -pino at pino 10.0.0: $lines"; exit 1; }
echo "$lines" | grep -q '"msg":"winston-floor","logger":"winston"' || { echo "FAIL: -winston at winston 3.10.0: $lines"; exit 1; }
echo "-pino at pino 10.0.0, -winston at winston 3.10.0: OK"
node --input-type=module -e "import { createPinoDestination } from '@staffops/otel-helper-pino'; import { OtelHelperTransport } from '@staffops/otel-helper-winston'; if (typeof createPinoDestination !== 'function' || typeof OtelHelperTransport !== 'function') process.exit(1)"
echo "api-range install test: OK (workspace $ws)"
