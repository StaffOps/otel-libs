// Type-level checks of startRootSpan's return type (B11b fix round and final
// round, 2026-11-03). Compiled by `npm run typecheck`, never run: a wrong
// return type fails the build.
import { startRootSpan } from '../../src/index';
import type { RootSpanResult } from '../../src/index';

type IsAny<T> = 0 extends 1 & T ? true : false;
type Equal<A, B> = IsAny<A> extends true ? IsAny<B> : IsAny<B> extends true ? false
  : [A] extends [B] ? ([B] extends [A] ? true : false) : false;
type Expect<T extends true> = T;

export const syncCall = () => startRootSpan('sync', () => 42);
export const asyncCall = () => startRootSpan('async', async () => {
  await Promise.resolve();
  return 'done';
});
declare const thenable: PromiseLike<boolean>;
export const thenableCall = () => startRootSpan('thenable', () => thenable);
export const spanCall = () => startRootSpan('span', (span) => span.spanContext().traceId);
// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const untyped: any;
// eslint-disable-next-line @typescript-eslint/no-unsafe-return
export const anyCall = () => startRootSpan('any', () => untyped);

export type Checks = [
  Expect<Equal<ReturnType<typeof syncCall>, number>>,
  Expect<Equal<ReturnType<typeof asyncCall>, Promise<string>>>,
  Expect<Equal<ReturnType<typeof thenableCall>, Promise<boolean>>>,
  Expect<Equal<ReturnType<typeof spanCall>, string>>,
  // An `any` result stays `any` (the conditional type distributes to any).
  Expect<IsAny<ReturnType<typeof anyCall>>>,
  Expect<Equal<RootSpanResult<Promise<number> | string>, Promise<number> | string>>,
  // The Equal helper itself: any is not number.
  Expect<Equal<Equal<number, string>, false>>,
];

export function rejected(): void {
  // @ts-expect-error name must be a string
  void startRootSpan(1, () => 1);
  // @ts-expect-error fn must be a function
  void startRootSpan('x', 'nope');
}

// withSpan / recordFailure (task A45, design D15): same return-type rule.
import { recordFailure, withSpan } from '../../src/index';
import type { SpanResult, WithSpanOptions } from '../../src/index';

export const withSync = () => withSpan('sync', () => 42);
export const withAsync = () => withSpan('async', async () => {
  await Promise.resolve();
  return 'done';
}, { root: true, attributes: { a: 1 } });
const options: WithSpanOptions = { root: false };
export const withOptions = () => withSpan('opts', (span) => span.isRecording(), options);

export type WithSpanChecks = [
  Expect<Equal<ReturnType<typeof withSync>, number>>,
  Expect<Equal<ReturnType<typeof withAsync>, Promise<string>>>,
  Expect<Equal<ReturnType<typeof withOptions>, boolean>>,
  Expect<Equal<SpanResult<PromiseLike<number>>, Promise<number>>>,
  Expect<Equal<ReturnType<typeof recordFailure>, void>>,
];

export function withSpanRejected(): void {
  recordFailure(new Error('x'));
  recordFailure('a primitive is accepted');
  // @ts-expect-error span must be a Span
  recordFailure(new Error('x'), 'not a span');
  // @ts-expect-error root must be a boolean
  void withSpan('x', () => 1, { root: 'yes' });
}
