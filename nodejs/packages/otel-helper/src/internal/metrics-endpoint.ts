/**
 * The Prometheus `/metrics` endpoint (task B7; spec JS7; design D-JS8, ADR-007;
 * contract design D1 rows on the Prometheus fallback and "Mounted /metrics
 * handler status"). The reader is the `@opentelemetry/exporter-prometheus`
 * `PrometheusExporter` with its own server never started; the text is written
 * by its `PrometheusSerializer` after the metric names were translated with
 * suffix semantics (unit word, `_total`, `_ratio` added only when the name
 * does not already end with them: contract D1 row "Suffix semantics", as
 * Python; Go's exporter works on name tokens and differs when a unit word or
 * `total`/`ratio` appears inside the name) and the labels were merged (D1 row
 * "Colliding label names"). The
 * helper's listener is a plain `node:http` server on `OTEL_HELPER_METRICS_PORT`
 * (9464, `0` = none), host omitted (`::` dual-stack when IPv6 exists, else
 * `0.0.0.0`), serving only `/metrics`, `unref()`'d.
 */
import * as http from 'node:http';
import { diag } from '@opentelemetry/api';
import { PrometheusExporter, PrometheusSerializer } from '@opentelemetry/exporter-prometheus';
import type { ExportResult } from '@opentelemetry/core';
import { DataPointType, type MetricData, type PushMetricExporter, type ResourceMetrics } from '@opentelemetry/sdk-metrics';
import { report, truncateValue } from './diagnostics';
import { envValue, trimWS, typeName, type Warn } from './env';

export const ENV_METRICS_PORT = 'OTEL_HELPER_METRICS_PORT';
export const ENV_DISABLED_METRICS = 'OTEL_HELPER_DISABLED_METRICS';
export const DEFAULT_METRICS_PORT = 9464;
const MAX_PORT = 65_535;

/** Prometheus text exposition format 0.0.4, as the Python helper serves it. */
export const CONTENT_TYPE = 'text/plain; version=0.0.4; charset=utf-8';
const UNAVAILABLE_BODY = 'otelhelper: prometheus metrics reader not configured\n';
// What the serializer writes when there is no metric (a comment; Go and Python write nothing).
const NO_METRICS = '# no registered metrics';

/**
 * Validates the code option `prometheusMetricsPort` (R8: fail fast): an
 * integer in [0, 65535], `0` = no listener.
 */
export function checkCodeMetricsPort(value: unknown): number | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0 || value > MAX_PORT) {
    const shown = typeof value === 'number' ? String(value) : typeName(value);
    throw new TypeError(`setup(): prometheusMetricsPort must be an integer in [0, ${String(MAX_PORT)}], got ${shown}`);
  }
  return value;
}

// Contract design D1 row "Metrics port grammar" (A21): ASCII digits, an optional "+".
const PORT = /^\+?[0-9]+$/;

/**
 * `prometheusMetricsPort` > `OTEL_HELPER_METRICS_PORT` > 9464. An env value
 * that is not an integer in [0, 65535] warns and the default applies (Go
 * `config.go`, Python `config.py`: "invalid port", using 9464).
 */
export function resolveMetricsPort(code: number | undefined, warn: Warn): number {
  if (code !== undefined) return code;
  const raw = envValue(ENV_METRICS_PORT);
  if (raw === undefined) return DEFAULT_METRICS_PORT;
  const port = Number(raw);
  if (!PORT.test(raw) || port < 0 || port > MAX_PORT) {
    warn('invalid port', { variable: ENV_METRICS_PORT, value: truncateValue(raw), using: String(DEFAULT_METRICS_PORT) });
    return DEFAULT_METRICS_PORT;
  }
  return port;
}

/**
 * Disabled-metric patterns from code (`disabledMetrics`, `[]` = none, wins) or
 * `OTEL_HELPER_DISABLED_METRICS` (comma-separated): every item trimmed with the
 * contract set, blank items ignored (contract design D1 row "Disabled-metric
 * patterns", A21).
 */
export function resolveDisabledMetrics(code: readonly string[] | undefined): readonly string[] {
  const items = code ?? (envValue(ENV_DISABLED_METRICS) ?? '').split(',');
  return Object.freeze(items.map((p) => trimWS(p)).filter((p) => p !== ''));
}

/** Validates the code option `disabledMetrics` (R8): an array of strings. */
export function checkCodeDisabledMetrics(value: unknown): readonly string[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || !(value as unknown[]).every((v) => typeof v === 'string')) {
    throw new TypeError(`setup(): disabledMetrics must be an array of strings, got ${typeName(value)}`);
  }
  return Object.freeze([...(value as string[])]);
}

/**
 * Whether `name` matches the glob `pattern` (contract design D1 row
 * "Disabled-metric patterns", A21): case-sensitive; `*` matches any run of
 * characters (`.` and `/` included), `?` exactly one, every other character is
 * literal (no `[...]` classes, no escapes). The SDK View wildcard cannot do it
 * (it expands only the first `*` and escapes `?`).
 */
export function globMatch(pattern: string, name: string): boolean {
  const p = Array.from(pattern); // code points
  const n = Array.from(name);
  let pi = 0;
  let ni = 0;
  let star = -1;
  let mark = 0;
  while (ni < n.length) {
    if (pi < p.length && p[pi] === '*') {
      star = pi++;
      mark = ni;
    } else if (pi < p.length && (p[pi] === '?' || p[pi] === n[ni])) {
      pi++;
      ni++;
    } else if (star >= 0) {
      pi = star + 1;
      ni = ++mark;
    } else {
      return false;
    }
  }
  while (pi < p.length && p[pi] === '*') pi++;
  return pi === p.length;
}

/** `true` for an instrument name to drop. */
export type DropPredicate = (name: string) => boolean;

/** The predicate of the resolved patterns; `undefined` without patterns. */
export function dropPredicate(patterns: readonly string[]): DropPredicate | undefined {
  if (patterns.length === 0) return undefined;
  return (name) => patterns.some((pattern) => globMatch(pattern, name));
}

/** The collection without the dropped metrics (and without scopes left empty). */
export function withoutDropped(resourceMetrics: ResourceMetrics, drop: DropPredicate | undefined): ResourceMetrics {
  if (drop === undefined) return resourceMetrics;
  return {
    resource: resourceMetrics.resource,
    scopeMetrics: resourceMetrics.scopeMetrics
      .map((sm) => ({ scope: sm.scope, metrics: sm.metrics.filter((m) => !drop(m.descriptor.name)) }))
      .filter((sm) => sm.metrics.length > 0),
  };
}

/**
 * A push exporter (OTLP, console) that drops the disabled metrics before
 * exporting; everything else (temporality and aggregation preferences, flush,
 * shutdown) is the wrapped exporter's. Chosen over SDK Views, which cannot
 * express the contract glob; the dropped instruments are still aggregated.
 */
export function droppingExporter(exporter: PushMetricExporter, drop: DropPredicate | undefined): PushMetricExporter {
  if (drop === undefined) return exporter;
  const wrapped: PushMetricExporter = {
    export: (metrics: ResourceMetrics, done: (result: ExportResult) => void) => {
      exporter.export(withoutDropped(metrics, drop), done);
    },
    forceFlush: () => exporter.forceFlush(),
    shutdown: () => exporter.shutdown(),
  };
  if (exporter.selectAggregationTemporality !== undefined) wrapped.selectAggregationTemporality = exporter.selectAggregationTemporality.bind(exporter);
  if (exporter.selectAggregation !== undefined) wrapped.selectAggregation = exporter.selectAggregation.bind(exporter);
  return wrapped;
}

/** The Prometheus reader: the exporter's own HTTP server is never started (D-JS8). */
export function createPrometheusReader(): PrometheusExporter {
  return new PrometheusExporter({ preventServerStart: true });
}

// UCUM units with a Prometheus word (OpenTelemetry specification,
// "Prometheus and OpenMetrics compatibility", metric metadata). The Go and
// Python exporters apply the same table.
const UNIT_WORDS: Readonly<Record<string, string>> = {
  d: 'days',
  h: 'hours',
  min: 'minutes',
  s: 'seconds',
  ms: 'milliseconds',
  us: 'microseconds',
  ns: 'nanoseconds',
  By: 'bytes',
  KiBy: 'kibibytes',
  MiBy: 'mebibytes',
  GiBy: 'gibibytes',
  TiBy: 'tibibytes',
  KBy: 'kilobytes',
  MBy: 'megabytes',
  GBy: 'gigabytes',
  TBy: 'terabytes',
  m: 'meters',
  V: 'volts',
  A: 'amperes',
  J: 'joules',
  W: 'watts',
  g: 'grams',
  Cel: 'celsius',
  Hz: 'hertz',
  '%': 'percent',
};

// The denominator of a "per" unit (`By/s` → `bytes_per_second`), singular.
const PER_WORDS: Readonly<Record<string, string>> = {
  s: 'second',
  m: 'minute',
  h: 'hour',
  d: 'day',
  w: 'week',
  mo: 'month',
  y: 'year',
};

function sanitizeUnit(unit: string): string {
  return unit.replace(/[^a-zA-Z0-9]+/g, '_').replace(/^_+|_+$/g, '');
}

function unitWord(unit: string): string {
  return UNIT_WORDS[unit] ?? sanitizeUnit(unit);
}

/**
 * The name suffix of a unit: `{…}` annotations dropped, known units spelled
 * out, `a/b` → `a_per_b`, `1` → `ratio` on Gauges only — not on non-monotonic
 * sums (contract D1 row "Unit 1"; Go compares the raw unit, so `{x}1` gets no
 * `_ratio` there), anything else sanitized.
 */
export function unitSuffix(unit: string, gauge: boolean): string {
  const u = unit.replace(/\{[^}]*\}/g, '');
  if (u === '1') return gauge ? 'ratio' : '';
  const word = UNIT_WORDS[u];
  if (word !== undefined) return word;
  const slash = u.indexOf('/');
  if (slash >= 0) {
    const bottomRaw = u.slice(slash + 1);
    const bottom = PER_WORDS[bottomRaw] ?? sanitizeUnit(bottomRaw);
    if (bottom !== '') {
      const topRaw = u.slice(0, slash);
      const top = topRaw === '1' ? '' : unitWord(topRaw);
      return top === '' ? `per_${bottom}` : `${top}_per_${bottom}`;
    }
  }
  return sanitizeUnit(u);
}

/**
 * The Prometheus name of a metric: invalid characters → `_` (runs collapsed),
 * the unit suffix unless the name already ends with it, `_total` on monotonic
 * sums (a trailing `_total` moves after the unit: `busy_total` in `s` →
 * `busy_seconds_total`). Suffix semantics (contract D1 row "Suffix
 * semantics"), as Python: a unit word or `total` inside the name is kept
 * (`probe.bytes-sent` in `By` → `probe_bytes_sent_bytes_total`; Go's
 * token-based namer writes `probe_bytes_sent_total`).
 */
export function prometheusName(metric: Pick<MetricData, 'descriptor' | 'dataPointType'> & { readonly isMonotonic?: boolean }): string {
  let name = metric.descriptor.name.replace(/[^a-zA-Z0-9_]/g, '_').replace(/_{2,}/g, '_');
  const counter = metric.dataPointType === DataPointType.SUM && metric.isMonotonic === true;
  // Only a Gauge gets `_ratio` for `1` (Go writes none on an UpDownCounter either).
  const gauge = metric.dataPointType === DataPointType.GAUGE;
  if (counter && name.endsWith('_total')) name = name.slice(0, -'_total'.length);
  const suffix = unitSuffix(metric.descriptor.unit, gauge);
  if (suffix !== '' && !name.endsWith(`_${suffix}`)) name = `${name}_${suffix}`;
  return counter ? `${name}_total` : name;
}

// Scope labels are written by the helper (see mergedLabels), not the serializer.
const serializer = new PrometheusSerializer(undefined, false, undefined, false, true);

/** The serializer's label-name sanitization (invalid characters → `_`, runs collapsed). */
function labelName(key: string): string {
  return key.replace(/[^a-zA-Z0-9_]/g, '_').replace(/_{2,}/g, '_');
}

/**
 * Attributes whose keys collide once sanitized (`a.b`, `a_b`) become one label
 * whose values, sorted as strings, are joined with `;` (contract D1 row
 * "Colliding label names", as Go's `slices.Sort(vals)`): Prometheus rejects a scrape with a
 * repeated label name. `extra` labels (the scope) are added only when no
 * attribute already has their name.
 */
export function mergedLabels(attributes: Readonly<Record<string, unknown>>, extra: Readonly<Record<string, string>> = {}): Record<string, string> {
  const groups = new Map<string, string[]>();
  for (const key of Object.keys(attributes).sort()) {
    const raw = attributes[key];
    const value = typeof raw === 'string' ? raw : JSON.stringify(raw);
    const name = labelName(key);
    const values = groups.get(name);
    if (values === undefined) groups.set(name, [value]);
    else values.push(value);
  }
  const out: Record<string, string> = Object.create(null) as Record<string, string>;
  for (const [name, values] of groups) out[name] = values.sort().join(';');
  for (const [name, value] of Object.entries(extra)) if (!(name in out)) out[name] = value;
  return out;
}

/**
 * The exposition text of one collection: the names translated by
 * {@link prometheusName}, the unit cleared (it is in the name now, and Go
 * and Python write no `# UNIT` line), labels merged by {@link mergedLabels}
 * with the scope as `otel_scope_name`, `otel_scope_version` and
 * `otel_scope_schema_url` (empty when unset, as Go and Python write them), then
 * the upstream serializer.
 */
export function serializeMetrics(resourceMetrics: ResourceMetrics): string {
  const translated = {
    resource: { attributes: mergedLabels(resourceMetrics.resource.attributes) },
    scopeMetrics: resourceMetrics.scopeMetrics.map((sm) => {
      const scope = { otel_scope_name: sm.scope.name, otel_scope_schema_url: sm.scope.schemaUrl ?? '', otel_scope_version: sm.scope.version ?? '' };
      return {
        scope: sm.scope,
        metrics: sm.metrics.map((m) => ({
          ...m,
          descriptor: { ...m.descriptor, name: prometheusName(m), unit: '' },
          dataPoints: m.dataPoints.map((dp) => ({ ...dp, attributes: mergedLabels(dp.attributes, scope) })),
        })),
      };
    }),
  } as unknown as ResourceMetrics;
  const text = serializer.serialize(translated);
  return text.endsWith(NO_METRICS) ? text.slice(0, -NO_METRICS.length) : text;
}

/** What a scrape finds: no reader (503), Prometheus deliberately off (200, empty) or a reader. */
export type ScrapeTarget =
  | { readonly kind: 'unavailable' }
  | { readonly kind: 'inactive' }
  | { readonly kind: 'active'; readonly reader: PrometheusExporter; readonly drop?: DropPredicate | undefined };

/** The request surface the handler reads (design D-JS8 amendment). */
export interface ScrapeRequest {
  readonly method?: string | undefined;
}

/** The response surface the handler uses (design D-JS8 amendment): a subset of `http.ServerResponse`. */
export interface ScrapeResponse {
  statusCode: number;
  readonly headersSent: boolean;
  setHeader(name: string, value: string | number): unknown;
  end(chunk?: string): unknown;
}

function respond(req: ScrapeRequest, res: ScrapeResponse, status: number, body: string): void {
  if (res.headersSent) {
    res.end();
    return;
  }
  res.statusCode = status;
  res.setHeader('Content-Type', CONTENT_TYPE);
  res.setHeader('Content-Length', Buffer.byteLength(body));
  if (req.method === 'HEAD') res.end();
  else res.end(body);
}

/**
 * Answers one scrape for `target()` (design "Mounted /metrics handler
 * status"): 503 without a reader (before setup, failed, foreign meter
 * provider, after shutdown), 200 with an empty body when Prometheus is
 * deliberately off, else the collection. A collection that fails answers 503
 * when the setup went away meanwhile (shutdown), else 500.
 */
export function serveMetrics(target: () => ScrapeTarget, req: ScrapeRequest, res: ScrapeResponse): void {
  const current = target();
  if (current.kind === 'unavailable') {
    respond(req, res, 503, UNAVAILABLE_BODY);
    return;
  }
  if (current.kind === 'inactive') {
    respond(req, res, 200, '');
    return;
  }
  // Serializing inside the chain: a throw there answers too, never an unhandled rejection.
  current.reader.collect().then(
    ({ resourceMetrics, errors }) => {
      if (errors.length > 0) diag.error('otelhelper: metric collection errors', ...errors);
      return serializeMetrics(withoutDropped(resourceMetrics, current.drop));
    },
  ).then(
    (body) => {
      respond(req, res, 200, body);
    },
    (err: unknown) => {
      const gone = target().kind !== 'active';
      const why = truncateValue(err instanceof Error ? err.message : String(err));
      respond(req, res, gone ? 503 : 500, gone ? UNAVAILABLE_BODY : `otelhelper: metric collection failed: ${why}\n`);
    },
  );
}

/**
 * The helper's own servers. Task B6's `ignoreIncomingRequestHook` checks it so
 * a scrape is neither traced nor counted in the HTTP server metrics (Go's
 * listener has no otelhttp, Python's is an uninstrumented wsgiref).
 */
const helperServers = new WeakSet<object>();

/** Whether `server` is the helper's `/metrics` listener. */
export function isHelperServer(server: unknown): boolean {
  return typeof server === 'object' && server !== null && helperServers.has(server);
}

function pathOf(url: string | undefined): string {
  if (url === undefined) return '';
  if (url.startsWith('/')) return url.split('?', 1)[0] ?? '';
  try {
    return new URL(url).pathname; // absolute-form request target
  } catch {
    return '';
  }
}

function plain(req: http.IncomingMessage, res: http.ServerResponse, status: number, body: string): void {
  res.statusCode = status;
  res.setHeader('Content-Type', 'text/plain; charset=utf-8');
  res.setHeader('Content-Length', Buffer.byteLength(body));
  if (req.method === 'HEAD') res.end();
  else res.end(body);
}

/**
 * Starts the helper's listener on `port` (> 0), host omitted. `setup()` is
 * synchronous, so a bind failure (port in use, no permission) arrives later:
 * it is reported as an error on the diagnostics sink and the mounted handler
 * keeps working (a recorded Node difference: Go fails Setup). The server is
 * `unref()`'d: it never keeps the process alive.
 */
export function startListener(port: number, handler: http.RequestListener): http.Server {
  // GET and HEAD /metrics only: other paths 404, other methods 405, with Go's
  // text bodies (contract design D1 row "Metrics listener", A21).
  // connectionsCheckingInterval: headersTimeout/requestTimeout are enforced
  // only at that period (30 s by default).
  const server = http.createServer({ headersTimeout: 5_000, requestTimeout: 10_000, connectionsCheckingInterval: 1_000 }, (req, res) => {
    if (pathOf(req.url) !== '/metrics') plain(req, res, 404, '404 page not found\n');
    else if (req.method !== 'GET' && req.method !== 'HEAD') {
      res.setHeader('Allow', 'GET, HEAD');
      plain(req, res, 405, 'Method Not Allowed\n');
    } else handler(req, res);
  });
  // An open client connection (a stalled or keep-alive scraper) must not keep the process alive either.
  server.on('connection', (socket) => {
    socket.unref();
  });
  helperServers.add(server);
  // Attached before listen: a bind error must never be an unhandled 'error' event.
  server.on('error', (err: NodeJS.ErrnoException) => {
    report(
      `the /metrics listener could not bind port ${String(port)}; metrics are served only by a mounted metricsHandler() (if another process owns the port, mount metricsHandler() and set ${ENV_METRICS_PORT}=0)`,
      { variable: ENV_METRICS_PORT, port, error: truncateValue(err.code ?? err.message) },
      { origin: 'helper', level: 'error' },
    );
  });
  server.listen(port);
  server.unref();
  return server;
}

/** Stops the listener and its open connections; resolves once it is closed (never rejects). */
export function closeListener(server: http.Server | undefined): Promise<void> {
  if (server === undefined) return Promise.resolve();
  return new Promise((resolve) => {
    // ERR_SERVER_NOT_RUNNING (the bind failed) is not a shutdown failure.
    server.close(() => {
      resolve();
    });
    server.closeAllConnections();
  });
}
