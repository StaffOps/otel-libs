'use strict';
// Control: the CommonJS preload without the hooks entry.
globalThis.__spike = require('./setup-stub.cjs').setupStub();
