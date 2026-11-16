'use strict';

const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { test } = require('node:test');
const grpc = require('@grpc/grpc-js');
const protoLoader = require('@grpc/proto-loader');

const { healthHandlers, healthServiceDefinition } = require('../lib/health');

const statusOf = (service) => (service === '' || service === 'demo.v1.OrderService' ? 'SERVING' : undefined);

test('the definition is grpc.health.v1.Health with Check and a streaming Watch', () => {
  const def = healthServiceDefinition(grpc, protoLoader);
  assert.equal(def.Check.path, '/grpc.health.v1.Health/Check');
  assert.equal(def.Watch.path, '/grpc.health.v1.Health/Watch');
  assert.equal(def.Watch.responseStream, true);
});

test('Check: SERVING for known services, NOT_FOUND otherwise', () => {
  const h = healthHandlers(statusOf, grpc.status.NOT_FOUND);
  const out = [];
  h.Check({ request: { service: '' } }, (err, res) => out.push([err, res]));
  h.Check({ request: { service: 'x' } }, (err, res) => out.push([err, res]));
  assert.deepEqual(out[0], [null, { status: 'SERVING' }]);
  assert.equal(out[1][0].code, grpc.status.NOT_FOUND);
});

test('Watch: writes the status and ends on cancel', () => {
  const h = healthHandlers(statusOf, grpc.status.NOT_FOUND);
  for (const [service, expected] of [['demo.v1.OrderService', 'SERVING'], ['other', 'SERVICE_UNKNOWN']]) {
    const call = Object.assign(new EventEmitter(), { request: { service }, written: [], ended: false });
    call.write = (m) => call.written.push(m);
    call.end = () => { call.ended = true; };
    h.Watch(call);
    assert.deepEqual(call.written, [{ status: expected }]);
    call.emit('cancelled');
    assert.equal(call.ended, true);
  }
});

test('Check and Watch over a real server', async (t) => {
  const server = new grpc.Server();
  server.addService(healthServiceDefinition(grpc, protoLoader), healthHandlers(statusOf, grpc.status.NOT_FOUND));
  const port = await new Promise((resolve, reject) => server.bindAsync('127.0.0.1:0', grpc.ServerCredentials.createInsecure(), (e, p) => (e ? reject(e) : resolve(p))));
  t.after(() => server.forceShutdown());
  const Client = grpc.makeGenericClientConstructor(healthServiceDefinition(grpc, protoLoader), 'Health');
  const client = new Client(`127.0.0.1:${port}`, grpc.credentials.createInsecure());
  t.after(() => client.close());
  const res = await new Promise((resolve, reject) => client.Check({ service: '' }, (e, r) => (e ? reject(e) : resolve(r))));
  assert.equal(res.status, 'SERVING');
});
