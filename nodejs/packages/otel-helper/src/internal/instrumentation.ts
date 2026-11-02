/**
 * The core instrumentations (task B6; contract R7, R10, D5; Node design
 * D-JS1, D-JS7): `@opentelemetry/instrumentation-http` (node:http/https
 * servers and clients), `-undici` (global `fetch` and undici), `-express`
 * (route and request-handler spans, middleware layers ignored) and `-grpc`
 * (`@grpc/grpc-js` servers and clients).
 *
 * Health checks (contract D5): server spans are dropped by the helper's
 * `HealthCheckSampler`, never by an instrumentation filter — an
 * `ignoreIncomingRequestHook` would drop the request from
 * `http.server.request.duration` too (Go `middleware.go`, Python
 * `instrumentation.py`). The instrumentations only have to start their spans
 * with what the sampler reads: `url.path` at start (instrumentation-http) and
 * the span name `grpc.<service>/<method>` (instrumentation-grpc), both asserted
 * by tests. Client calls are filtered like Go `NewHTTPTransport` and the Python
 * `excluded_urls`: a request to a health path gets no span and no metric. gRPC
 * client health calls are dropped by the sampler (CLIENT spans of
 * `grpc.health.v1.Health`); instrumentation-grpc records no RPC metrics, so
 * nothing else is counted (D-JS14, as Python).
 *
 * Excluded besides: the helper's own `/metrics` listener (neither traced nor
 * counted: Go's listener has no otelhttp, Python's is an uninstrumented
 * wsgiref; design D-JS8) and the helper's OTLP/HTTP export requests. The SDK
 * exports under a suppressed-tracing context, so they get no span, but
 * instrumentation-http 0.300 records `http.client.request.duration` even then:
 * without this filter every export would count as a client request (Go's
 * exporters use an uninstrumented client; Python's instrumentation skips the
 * metric too under suppression). gRPC OTLP exports need nothing: grpc-js uses
 * http2 (not instrumented by instrumentation-http) and instrumentation-grpc
 * records no metrics.
 *
 * Lifecycle: the instrumentations are built once per process and kept on the
 * shared runtime; every `setup()` re-binds them to its providers and its
 * exclusions and enables them; `shutdown()` disables them (Python
 * uninstruments at shutdown). They are not built under `OTEL_SDK_DISABLED=true`
 * (Python: no instrumentation, contract D1 row "OTEL_SDK_DISABLED").
 */
import type * as http from 'node:http';
import { syncBuiltinESMExports } from 'node:module';
import * as path from 'node:path';
import type { MeterProvider, TracerProvider } from '@opentelemetry/api';
import type { Instrumentation } from '@opentelemetry/instrumentation';
import type * as Express from '@opentelemetry/instrumentation-express';
import type * as Grpc from '@opentelemetry/instrumentation-grpc';
import type * as Http from '@opentelemetry/instrumentation-http';
import type * as Undici from '@opentelemetry/instrumentation-undici';
import { reportOnce } from './diagnostics';
import { isHelperServer } from './metrics-endpoint';
import { getState } from './state';
import type { OtlpPlan } from './otlp';
import { normalizeHealthPath } from './tracing';

/** One OTLP/HTTP export URL, split for comparison with an outgoing request. */
export interface HttpTarget {
  readonly protocol: string;
  /** Lowercase, IPv6 brackets removed. */
  readonly hostname: string;
  /** Always explicit (the scheme default filled in). */
  readonly port: string;
  readonly pathname: string;
}

/** What the client hooks exclude, rebuilt by every `setup()`. */
export interface ClientExclusions {
  /** Normalized health paths (`normalizeHealthPath`). */
  readonly healthPaths: ReadonlySet<string>;
  /** The helper's OTLP/HTTP export URLs. */
  readonly otlpTargets: readonly HttpTarget[];
}

export const NO_EXCLUSIONS: ClientExclusions = Object.freeze({ healthPaths: new Set<string>(), otlpTargets: Object.freeze([]) });

function defaultPort(protocol: string): string {
  return protocol === 'https:' ? '443' : '80';
}

function bare(hostname: string): string {
  const h = hostname.startsWith('[') && hostname.endsWith(']') ? hostname.slice(1, -1) : hostname;
  return h.toLowerCase();
}

function targetOf(url: URL): HttpTarget {
  return { protocol: url.protocol, hostname: bare(url.hostname), port: url.port || defaultPort(url.protocol), pathname: url.pathname };
}

/**
 * The OTLP/HTTP endpoints the helper exports to: every `http/protobuf`
 * signal whose exporter list has `otlp` (`exporting`). gRPC endpoints are
 * left out (see the module comment).
 */
export function otlpTargets(otlp: OtlpPlan, exporting: Readonly<Record<keyof OtlpPlan, boolean>>): HttpTarget[] {
  const out: HttpTarget[] = [];
  for (const signal of ['traces', 'metrics', 'logs'] as const) {
    const endpoint = otlp[signal];
    if (endpoint?.protocol !== 'http/protobuf' || !exporting[signal]) continue;
    try {
      out.push(targetOf(new URL(endpoint.url)));
    } catch {
      // The plan holds validated URLs; an unparseable one is never exported to either.
    }
  }
  return out;
}

/** The exclusions of one setup: its health paths (`[]` = none) and OTLP targets. */
export function clientExclusions(healthPaths: readonly string[], targets: readonly HttpTarget[]): ClientExclusions {
  return Object.freeze({ healthPaths: new Set(healthPaths.map(normalizeHealthPath)), otlpTargets: Object.freeze([...targets]) });
}

/**
 * The path of a request target: origin-form as sent, query and fragment cut;
 * an absolute-form target (a request through a proxy, `path:
 * 'http://target/healthz'`) gives its URL pathname.
 */
function pathOnly(path: string | null | undefined): string {
  const p = path === undefined || path === null || path === '' ? '/' : path;
  if (!p.startsWith('/')) {
    try {
      return new URL(p).pathname;
    } catch {
      // Not a URL: matched as written.
    }
  }
  const cut = p.search(/[?#]/);
  return cut >= 0 ? p.slice(0, cut) : p;
}

/** Whether a request (protocol, host, port, path as sent) goes to one of `targets`. */
function matchesTarget(
  targets: readonly HttpTarget[],
  protocol: string | null | undefined,
  hostname: string,
  port: string | number | null | undefined,
  path: string,
): boolean {
  const host = bare(hostname);
  const pathname = pathOnly(path);
  return targets.some((t) => {
    if (protocol !== undefined && protocol !== null && protocol !== t.protocol) return false;
    const p = port === undefined || port === null || port === '' ? defaultPort(protocol ?? t.protocol) : String(port);
    return t.hostname === host && t.port === p && t.pathname === pathname;
  });
}

/**
 * instrumentation-http `ignoreOutgoingRequestHook`: a request to a health
 * path (D5 normalization: exact, case-insensitive, trailing slash, query and
 * fragment ignored) or to one of the helper's OTLP/HTTP endpoints. Never
 * throws (the instrumentation would report the error through `diag`).
 */
export function ignoreOutgoing(options: http.RequestOptions, exclusions: ClientExclusions): boolean {
  try {
    const path = options.path ?? '/';
    if (exclusions.healthPaths.has(normalizeHealthPath(pathOnly(path)))) return true;
    if (exclusions.otlpTargets.length === 0) return false;
    // `host` may carry no port; `hostname` wins as in node:http.
    const hostname = options.hostname ?? options.host ?? 'localhost';
    return matchesTarget(exclusions.otlpTargets, options.protocol, hostname, options.port, path);
  } catch {
    return false;
  }
}

/** The part of an undici request the hook reads. */
export interface UndiciRequestLike {
  readonly origin: string;
  readonly path: string;
}

/** instrumentation-undici `ignoreRequestHook` (`fetch`, undici): same rules as {@link ignoreOutgoing}. */
export function ignoreUndici(request: UndiciRequestLike, exclusions: ClientExclusions): boolean {
  try {
    if (exclusions.healthPaths.has(normalizeHealthPath(pathOnly(request.path)))) return true;
    if (exclusions.otlpTargets.length === 0) return false;
    const origin = new URL(request.origin);
    return matchesTarget(exclusions.otlpTargets, origin.protocol, origin.hostname, origin.port, request.path);
  } catch {
    return false;
  }
}

/** Query parameters whose values instrumentation-http redacts (`internal-types.cjs` DEFAULT_QUERY_STRINGS_TO_REDACT, 0.300). */
export const REDACTED_QUERY_PARAMS: readonly string[] = Object.freeze([
  'sig',
  'Signature',
  'AWSAccessKeyId',
  'X-Goog-Signature',
  'X-Amz-Signature',
  'X-Amz-Credential',
  'X-Amz-Security-Token',
]);
const REDACTED = 'REDACTED';

/** A query string (no leading `?`) with the {@link REDACTED_QUERY_PARAMS} values replaced, as instrumentation-http does. */
/**
 * Returned unchanged unless one of the names is present: re-serializing
 * through `URLSearchParams` would rewrite it (`flag` → `flag=`, `%20` → `+`).
 */
function redactQuery(search: string): string {
  const params = new URLSearchParams(search);
  if (!REDACTED_QUERY_PARAMS.some((name) => params.has(name))) return search;
  for (const name of REDACTED_QUERY_PARAMS) if (params.has(name)) params.set(name, REDACTED);
  return params.toString();
}

/**
 * instrumentation-undici `startSpanHook`: the URL attributes it would write
 * unredacted (`url.full` with userinfo and every query value, `url.query`
 * with its leading `?`), rewritten as instrumentation-http writes them for
 * `node:http` clients — userinfo `REDACTED:REDACTED`, the values of
 * {@link REDACTED_QUERY_PARAMS} `REDACTED` (names matched case-sensitively,
 * as upstream), and no `url.query` (A22, 2026-11-02; dropped in the B6
 * re-probe: instrumentation-http writes it on server spans only). The query
 * in `url.full` is kept as written unless a name is redacted (`node:http`
 * re-serializes it): intentional, nothing is rewritten that need not be.
 * The hook's attributes override the instrumentation's. Never throws (an
 * empty result keeps upstream's).
 */
export function undiciUrlAttributes(request: UndiciRequestLike): Record<string, string | undefined> {
  try {
    const url = new URL(request.path, request.origin);
    const query = url.search === '' ? undefined : redactQuery(url.search.slice(1));
    const auth = url.username !== '' || url.password !== '' ? `${REDACTED}:${REDACTED}@` : '';
    const full = `${url.protocol}//${auth}${url.host}${url.pathname}${query === undefined ? '' : `?${query}`}`;
    // No url.query on client spans: instrumentation-http sets it only on
    // incoming requests (utils.cjs:387-390) and Go writes none.
    return { 'url.full': full, 'url.query': undefined };
  } catch {
    return {};
  }
}

/** The part of an incoming request the hook reads. */
export interface IncomingLike {
  /** A `net.Socket` accepted by a server carries that server as `socket.server`. */
  readonly socket?: object | null | undefined;
}

/**
 * instrumentation-http `ignoreIncomingRequestHook`: only requests to the
 * helper's own `/metrics` listener (no span, no metric). Health requests are
 * NOT ignored here: they stay counted and the sampler drops their spans.
 */
export function ignoreIncoming(request: IncomingLike): boolean {
  try {
    const socket = request.socket as { readonly server?: unknown } | null | undefined;
    return isHelperServer(socket?.server);
  } catch {
    return false;
  }
}

/** The instrumentations of one process, re-bound by every setup. */
export interface HelperInstrumentations {
  readonly instrumentations: readonly Instrumentation[];
  /** Current client exclusions (read by the hooks on every request). */
  exclusions: ClientExclusions;
  /** Whether the first enable already re-required the packages loaded before it. */
  preloadChecked: boolean;
}

/** Third-party packages the core instrumentations patch through require-in-the-middle. */
export const INSTRUMENTED_PACKAGES: readonly string[] = Object.freeze(['express', '@grpc/grpc-js']);

// Loaded on first setup only: `require('@staffops/otel-helper')` stays light.
/* eslint-disable @typescript-eslint/no-require-imports */
const httpModule = (): typeof Http => require('@opentelemetry/instrumentation-http') as typeof Http;
const undiciModule = (): typeof Undici => require('@opentelemetry/instrumentation-undici') as typeof Undici;
const expressModule = (): typeof Express => require('@opentelemetry/instrumentation-express') as typeof Express;
const grpcModule = (): typeof Grpc => require('@opentelemetry/instrumentation-grpc') as typeof Grpc;
/* eslint-enable @typescript-eslint/no-require-imports */

/**
 * Builds the four core instrumentations with hooks reading
 * `holder.exclusions`, then disables them until {@link enableInstrumentations}.
 * Not `enabled: false` in their config: instrumentation-undici checks
 * `config.enabled` on every request and would ignore them all.
 */
export function createInstrumentations(): HelperInstrumentations {
  const holder: { instrumentations: Instrumentation[]; exclusions: ClientExclusions; preloadChecked: boolean } = {
    instrumentations: [],
    exclusions: NO_EXCLUSIONS,
    preloadChecked: false,
  };
  const { HttpInstrumentation } = httpModule();
  const { UndiciInstrumentation } = undiciModule();
  const { ExpressInstrumentation, ExpressLayerType } = expressModule();
  const { GrpcInstrumentation } = grpcModule();
  const factories: (() => Instrumentation)[] = [
    () =>
      new HttpInstrumentation({
        ignoreIncomingRequestHook: (req) => ignoreIncoming(req),
        ignoreOutgoingRequestHook: (options) => ignoreOutgoing(options, holder.exclusions),
      }),
    () =>
      new UndiciInstrumentation({
        ignoreRequestHook: (req) => ignoreUndici(req, holder.exclusions),
        // Overrides url.full and drops url.query (an attribute set to undefined is dropped by the SDK).
        startSpanHook: (req) => undiciUrlAttributes(req),
      }),
    // Middleware and router layers would add one span per `app.use()` / router
    // to every request (router spans have no duration of their own); the
    // request-handler spans stay.
    () => new ExpressInstrumentation({ ignoreLayersType: [ExpressLayerType.MIDDLEWARE, ExpressLayerType.ROUTER] }),
    () => new GrpcInstrumentation(),
  ];
  try {
    for (const make of factories) {
      // Disabled right away: InstrumentationBase's constructor calls enable()
      // when `config.enabled` is not false (the default), patching nothing yet
      // but registering the require/import hooks. Not `enabled: false`:
      // instrumentation-undici reads `config.enabled` on every request.
      // An upstream assumption (@opentelemetry/instrumentation 0.300,
      // `platform/node/instrumentation.cjs:24-30`): if a release stops
      // enabling in the constructor, `disable()` here is a no-op and
      // `enableInstrumentations` still enables — re-check on SDK bumps (B13).
      const instrumentation = make();
      holder.instrumentations.push(instrumentation);
      instrumentation.disable();
    }
  } catch (err) {
    // The require/import hooks the built ones registered stay registered
    // (upstream has no way to remove them) but are inert: disabled, nothing
    // patches; the next setup builds a new set (setup.ts instrumentationsOf).
    disableInstrumentations(holder);
    throw err;
  }
  return holder;
}

/**
 * The main entries of every copy of `name` with a file in `require.cache`,
 * found by their `/node_modules/<name>/` directory. A copy loaded only
 * through a deep path (`require('express/lib/express.js')`) counts too: its
 * main entry is returned when it resolves, so requiring it loads the package
 * through the hook (the deep module is the same cached object).
 */
function cachedEntries(name: string): string[] {
  const marker = `${path.sep}node_modules${path.sep}${name.split('/').join(path.sep)}${path.sep}`;
  const dirs = new Set<string>();
  for (const file of Object.keys(require.cache)) {
    const at = file.lastIndexOf(marker);
    if (at >= 0) dirs.add(file.slice(0, at + marker.length - 1));
  }
  const entries: string[] = [];
  for (const dir of dirs) {
    try {
      entries.push(require.resolve(dir));
    } catch {
      // A directory without a resolvable main entry was never required as the package.
    }
  }
  return entries;
}

/**
 * Whether a cached entry was loaded by the ESM loader (an `import` of a CJS
 * package): Node creates that `Module` without a parent, while a CJS
 * `require()` records the requiring module (checked on Node 24, B6 re-probe
 * 2026-11-02).
 */
function loadedThroughEsm(entry: string): boolean {
  const cached = require.cache[entry] as { readonly parent?: unknown } | undefined;
  return cached !== undefined && cached.parent === undefined;
}

/** The {@link INSTRUMENTED_PACKAGES} in `require.cache` now (taken when `setup()` starts). */
export function loadedPackages(): ReadonlySet<string> {
  return new Set(INSTRUMENTED_PACKAGES.filter((name) => cachedEntries(name).length > 0));
}

/**
 * Sets the exclusions and enables every instrumentation: the modules already
 * known to it are patched, later loads by the require/import hooks.
 *
 * Packages loaded before the first enable are unknown to require-in-the-middle
 * (it patches on `require`), so the first enable requires each instrumented
 * package's cached main entry again by path — that patches its prototypes in
 * place, so the app's copy is instrumented — and reports once per package
 * that was loaded before `setup()` started (`loadedBeforeSetup`; the gRPC
 * exporters load grpc-js inside `setup()`):
 * objects the app built before `setup()` (an express app, a gRPC client or
 * server) may keep unpatched references. `node:http` and `node:https` are
 * required again on every enable for the same reason, and
 * `module.syncBuiltinESMExports()` makes ESM named imports
 * (`import { request } from 'node:http'`) see the patched functions.
 */
export function enableInstrumentations(holder: HelperInstrumentations, exclusions: ClientExclusions, loadedBeforeSetup: ReadonlySet<string>): void {
  holder.exclusions = exclusions;
  for (const instrumentation of holder.instrumentations) instrumentation.enable();
  if (!holder.preloadChecked) {
    holder.preloadChecked = true;
    const mode = getState()?.hooks;
    const esmHooks = mode === 'sync' || mode === 'async';
    for (const name of INSTRUMENTED_PACKAGES) {
      const entries = cachedEntries(name);
      if (entries.length === 0) continue;
      // eslint-disable-next-line @typescript-eslint/no-require-imports -- re-require so the require hook patches it
      for (const entry of entries) require(entry);
      // The gRPC exporters load grpc-js inside setup(): re-required, not reported.
      if (!loadedBeforeSetup.has(name)) continue;
      // Imported through ESM with the loader hooks installed: import-in-the-middle
      // already handed it to the instrumentation; only CJS cache hits are reported.
      if (esmHooks && entries.every(loadedThroughEsm)) continue;
      reportOnce(
        `preloaded:${name}`,
        // Not "use /register" until B9: register does not call setup() yet.
        `${name} was loaded before setup(); apps, routers and clients created before setup() may lack spans or have an incomplete http.route — call setup() in a preload (e.g. \`--import ./otel.mjs\` that imports the hooks entry and calls setup()) before the app loads`,
        { module: name },
      );
    }
  }
  /* eslint-disable @typescript-eslint/no-require-imports */
  require('node:http');
  require('node:https');
  /* eslint-enable @typescript-eslint/no-require-imports */
  syncBuiltinESMExports();
}

/**
 * Binds every instrumentation to these providers (after they are built and
 * registered). Explicit providers, not the API proxies: the API has no proxy
 * meter, and a proxy tracer keeps the first delegate it resolved, so a
 * re-setup could not re-bind it (task B9).
 */
export function bindProviders(holder: HelperInstrumentations, tracerProvider: TracerProvider, meterProvider: MeterProvider): void {
  for (const instrumentation of holder.instrumentations) {
    instrumentation.setTracerProvider(tracerProvider);
    instrumentation.setMeterProvider(meterProvider);
  }
}

/**
 * Disables every instrumentation (unpatches the modules, ESM named imports of
 * the builtins included); `enable()` on the next setup patches them again.
 */
export function disableInstrumentations(holder: HelperInstrumentations | undefined): void {
  if (holder === undefined) return;
  for (const instrumentation of holder.instrumentations) {
    try {
      instrumentation.disable();
    } catch {
      // An unpatch failure leaves a patched module bound to a shut-down provider: harmless.
    }
  }
  syncBuiltinESMExports();
}
