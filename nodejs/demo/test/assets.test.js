'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const { test } = require('node:test');
const grpc = require('@grpc/grpc-js');
const protoLoader = require('@grpc/proto-loader');

const { DEFAULT_SCHEMA_FILE, assetDir, loadOrderService, readSchema, schemaFile } = require('../lib/assets');

// The tests run from a checkout (nodejs/demo), so the shared files are in ../../demo.
const LIB = path.join(__dirname, '..', 'lib');

test('assetDir prefers the image layout, then the checkout', () => {
  const image = path.join(LIB, '..', 'proto');
  assert.equal(assetDir('proto', LIB, (dir) => dir === image), image);
  assert.equal(assetDir('proto', LIB), path.join(LIB, '..', '..', '..', 'demo', 'proto'));
  assert.throws(() => assetDir('proto', LIB, () => false), /demo\/proto not found/);
});

test('loadOrderService loads demo.v1.OrderService from the shared proto', () => {
  const OrderService = loadOrderService(grpc, protoLoader);
  assert.deepEqual(Object.keys(OrderService.service).sort(), ['GetOrder', 'PlaceOrder']);
  assert.equal(OrderService.service.PlaceOrder.path, '/demo.v1.OrderService/PlaceOrder');
});

test('schemaFile: DEMO_SCHEMA_FILE, else the image path', () => {
  assert.equal(schemaFile({}), DEFAULT_SCHEMA_FILE);
  assert.equal(DEFAULT_SCHEMA_FILE, '/etc/otel-libs-demo/schema.sql');
  assert.equal(schemaFile({ DEMO_SCHEMA_FILE: ' /x.sql ' }), '/x.sql');
  assert.equal(typeof schemaFile(), 'string');
});

test('readSchema returns the shared schema with the advisory lock first', () => {
  const schema = readSchema(path.join(__dirname, '..', '..', '..', 'demo', 'sql', 'schema.sql'));
  assert.match(schema, /SELECT pg_advisory_xact_lock\(4242\);/);
  assert.match(schema, /CREATE OR REPLACE TRIGGER orders_dup/);
});
