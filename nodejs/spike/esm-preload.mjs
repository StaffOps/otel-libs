// Custom ESM preload as an app would write it (JS5): hooks first, then setup.
import '@staffops/otel-helper/hooks';
import stub from './setup-stub.cjs';

globalThis.__spike = stub.setupStub();
