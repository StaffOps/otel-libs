// sdk.test.cjs fixture: the ES module app, loaded after esm-preload.mjs.
import { PutObjectCommand, S3Client } from '@aws-sdk/client-s3';
import { shutdown } from '@staffops/otel-helper';
import { getState } from '@staffops/otel-helper/testing';
import fakeModule from './aws-fake.cjs';

const s3 = new S3Client({ ...fakeModule.clientConfig({ url: process.env.AWS_FAKE_URL }), forcePathStyle: true });
await s3.send(new PutObjectCommand({ Bucket: 'bucket-1', Key: 'esm', Body: 'b' }));
const spans = globalThis.__fixtureExporter
  .getFinishedSpans()
  .filter((s) => s.kind !== 1)
  .map((s) => ({ name: s.name, scope: s.instrumentationScope.name, attributes: s.attributes }));
const hooks = getState()?.hooks ?? null;
await shutdown();
console.log(JSON.stringify({ hooks, spans }));
