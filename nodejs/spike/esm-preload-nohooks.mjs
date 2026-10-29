// Control: the same preload without the hooks entry.
import stub from './setup-stub.cjs';

globalThis.__spike = stub.setupStub();
