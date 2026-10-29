'use strict';
// Custom CommonJS preload (`node --require`): hooks first, then setup.
require('@staffops/otel-helper/hooks');
globalThis.__spike = require('./setup-stub.cjs').setupStub();
