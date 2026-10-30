/**
 * OTLP exporter construction (task B4b; spec JS2; design D-JS1, D-JS3;
 * contract design D1 rows dated 2026-10-29, task A18). The exporters get every
 * value explicitly — URL or gRPC target, headers, timeout, compression and the
 * TLS material (gRPC channel credentials, or HTTPS agent options) — and are
 * built while every `OTEL_EXPORTER_OTLP[_<SIGNAL>]_*` variable the helper owns
 * is hidden from `process.env` ({@link withMaskedEnv}): a value the helper
 * rejected never reaches the wire, and the SDK never echoes a raw value or a
 * file path (R8). The helper reads the TLS files itself ({@link tlsMaterial}),
 * so TLS never depends on the environment after construction.
 *
 * The gRPC exporter packages are loaded only when a gRPC signal is
 * configured: they load `@grpc/grpc-js`, which `require('@staffops/otel-helper')`
 * must not do (an app's grpc-js is still patched by the instrumentation's
 * require hooks when it is loaded later, spike B1c F6).
 */
import { X509Certificate } from 'node:crypto';
import * as fs from 'node:fs';
import { isIP } from 'node:net';
import * as path from 'node:path';
import { createSecureContext } from 'node:tls';
import type * as GrpcBase from '@opentelemetry/otlp-grpc-exporter-base';
import type * as GrpcLogs from '@opentelemetry/exporter-logs-otlp-grpc';
import type * as GrpcMetrics from '@opentelemetry/exporter-metrics-otlp-grpc';
import type * as GrpcTraces from '@opentelemetry/exporter-trace-otlp-grpc';
import { OTLPLogExporter as HttpLogExporter } from '@opentelemetry/exporter-logs-otlp-proto';
import { OTLPMetricExporter as HttpMetricExporter } from '@opentelemetry/exporter-metrics-otlp-proto';
import { OTLPTraceExporter as HttpTraceExporter } from '@opentelemetry/exporter-trace-otlp-proto';
import type { LogRecordExporter } from '@opentelemetry/sdk-logs';
import type { PushMetricExporter } from '@opentelemetry/sdk-metrics';
import type { SpanExporter } from '@opentelemetry/sdk-trace';
import { truncateValue } from './diagnostics';
import { trimWS, type Warn } from './env';
import type { OtlpEndpoint } from './otlp';
import { otlpEnv, type OtlpCompression, type ResolvedHeaders, type Signal } from './otlp-settings';

const SIGNALS: readonly Signal[] = ['traces', 'metrics', 'logs'];

type ChannelCredentials = ReturnType<typeof GrpcBase.createInsecureCredentials>;
type Metadata = ReturnType<typeof GrpcBase.createEmptyMetadata>;

/** The suffixes of the helper-owned OTLP variables (JS2, A18): hidden during construction. */
export const MASKED_SUFFIXES: readonly string[] = [
  'ENDPOINT',
  'HEADERS',
  'TIMEOUT',
  'COMPRESSION',
  'PROTOCOL',
  'INSECURE',
  'CERTIFICATE',
  'CLIENT_KEY',
  'CLIENT_CERTIFICATE',
];

/** Every variable {@link withMaskedEnv} hides: the generic and the per-signal ones. */
export function maskedVariables(): string[] {
  const names: string[] = [];
  for (const suffix of MASKED_SUFFIXES) {
    names.push(otlpEnv(undefined, suffix));
    for (const signal of SIGNALS) names.push(otlpEnv(signal, suffix));
  }
  return names;
}

/**
 * Runs `build` with every helper-owned OTLP variable removed from
 * `process.env` and restores them afterwards, also when `build` throws.
 * Synchronous on purpose (design D-JS3): no other code of this thread runs
 * while they are hidden. A worker started with `SHARE_ENV` shares
 * `process.env` and would see the mask meanwhile (D-JS14).
 */
export function withMaskedEnv<T>(build: () => T): T {
  const saved = new Map<string, string>();
  for (const name of maskedVariables()) {
    const value = process.env[name];
    if (value !== undefined) {
      saved.set(name, value);
      Reflect.deleteProperty(process.env, name);
    }
  }
  try {
    return build();
  } finally {
    for (const [name, value] of saved) process.env[name] = value;
  }
}

/** Files read during one setup, by resolved path (a generic file is read once for three signals). */
export type FileCache = Map<string, Buffer | NodeJS.ErrnoException>;

/** Reads the file a variable names, relative to the working directory as the SDK does. */
function readFile(variable: string, cache: FileCache): Buffer | NodeJS.ErrnoException {
  const file = path.resolve(process.cwd(), trimWS(process.env[variable] ?? ''));
  let content = cache.get(file);
  if (content === undefined) {
    try {
      content = fs.readFileSync(file);
    } catch (err) {
      content = err as NodeJS.ErrnoException;
    }
    cache.set(file, content);
  }
  return content;
}

const PEM_CERTIFICATE = /-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/g;

/** Whether `pem` holds at least one certificate Node can parse (else the CA would trust nothing, silently). */
function hasCertificate(pem: Buffer): boolean {
  for (const block of pem.toString('latin1').match(PEM_CERTIFICATE) ?? []) {
    try {
      new X509Certificate(block);
      return true;
    } catch {
      // Try the next block.
    }
  }
  return false;
}

/** The set TLS variables of a suffix, per-signal first. */
function tlsCandidates(signal: Signal, suffix: string): string[] {
  return [otlpEnv(signal, suffix), otlpEnv(undefined, suffix)].filter((v) => trimWS(process.env[v] ?? '') !== '');
}

/**
 * The first readable (and, for the CA, parseable) file among `candidates`
 * (per-signal, then generic: D1 ruling "TLS files", A18). Each file skipped
 * warns naming the variable — never the path or the content (R8).
 */
function firstFile(candidates: readonly string[], cache: FileCache, warn: Warn, isCA: boolean): { variable: string; content: Buffer } | undefined {
  for (const variable of candidates) {
    const content = readFile(variable, cache);
    const next = variable === candidates[candidates.length - 1] ? (isCA ? 'the system roots' : 'none') : 'the generic variable';
    if (!Buffer.isBuffer(content)) {
      warn('TLS file cannot be read; ignored', { variable, value: truncateValue(content.code ?? 'read error'), using: next });
      continue;
    }
    if (isCA && !hasCertificate(content)) {
      warn('TLS CA file holds no PEM certificate; ignored', { variable, using: next });
      continue;
    }
    return { variable, content };
  }
  return undefined;
}

/** TLS material of one signal (undefined fields: system roots / no client certificate). */
export interface TlsMaterial {
  readonly ca: Buffer | undefined;
  readonly key: Buffer | undefined;
  readonly cert: Buffer | undefined;
  /** Variables of the client pair, for diagnostics. */
  readonly pairVariables: string;
  /** Variable of the CA, for diagnostics (`''` when none). */
  readonly caVariable: string;
}

/**
 * Resolves the CA and the client key/certificate pair of a TLS endpoint (D1
 * ruling "TLS files", A18): each file per-signal then generic; an unreadable
 * or unparseable file warns and falls back. The key and the certificate are
 * one pair: when only one resolves, or the TLS layer rejects them (bad PEM,
 * mismatch, encrypted key), the signal uses server authentication with the
 * resolved CA, with a warning naming the variables only. Never throws.
 */
export function tlsMaterial(signal: Signal, cache: FileCache, warn: Warn): TlsMaterial {
  const ca = firstFile(tlsCandidates(signal, 'CERTIFICATE'), cache, warn, true);
  const key = firstFile(tlsCandidates(signal, 'CLIENT_KEY'), cache, warn, false);
  const cert = firstFile(tlsCandidates(signal, 'CLIENT_CERTIFICATE'), cache, warn, false);
  const pairVariables = [key?.variable, cert?.variable].filter((v) => v !== undefined).join(',');
  const caVariable = ca?.variable ?? '';
  const serverAuth = (message: string): TlsMaterial => {
    warn(message, { variable: pairVariables, using: 'server authentication' });
    return { ca: ca?.content, key: undefined, cert: undefined, pairVariables, caVariable };
  };
  if (key === undefined && cert === undefined) return { ca: ca?.content, key: undefined, cert: undefined, pairVariables, caVariable };
  if (key === undefined || cert === undefined) {
    return serverAuth('client key and client certificate must both be set and readable; the client certificate is not used');
  }
  try {
    createSecureContext({ key: key.content, cert: cert.content });
  } catch {
    // The TLS layer's message never holds file content, but is not needed either.
    return serverAuth('client key and certificate rejected by TLS (bad PEM, mismatch or encrypted key); the client certificate is not used');
  }
  return { ca: ca?.content, key: key.content, cert: cert.content, pairVariables, caVariable };
}

/** The host of a `host:port` gRPC target, brackets removed. */
function targetHost(target: string): string {
  const host = target.slice(0, target.lastIndexOf(':'));
  return host.startsWith('[') ? host.slice(1, -1) : host;
}

// Loaded on first use only (see the module comment).
/* eslint-disable @typescript-eslint/no-require-imports */
const grpcBase = (): typeof GrpcBase => require('@opentelemetry/otlp-grpc-exporter-base') as typeof GrpcBase;
const grpcTraces = (): typeof GrpcTraces => require('@opentelemetry/exporter-trace-otlp-grpc') as typeof GrpcTraces;
const grpcMetrics = (): typeof GrpcMetrics => require('@opentelemetry/exporter-metrics-otlp-grpc') as typeof GrpcMetrics;
const grpcLogs = (): typeof GrpcLogs => require('@opentelemetry/exporter-logs-otlp-grpc') as typeof GrpcLogs;
/* eslint-enable @typescript-eslint/no-require-imports */

/**
 * Explicit channel credentials of a gRPC signal (design D-JS3 row "gRPC
 * channel credentials"). Plaintext: insecure credentials. TLS: the
 * {@link tlsMaterial} resolved before masking; if gRPC still rejects the pair, retry with the CA only,
 * then the system roots. An IP-literal TLS target is warned (D-JS14: Node
 * refuses an IP as TLS server name, so the handshake fails on Node 26).
 */
export function grpcCredentials(ep: OtlpEndpoint, tls: TlsMaterial, warn: Warn): ChannelCredentials {
  const base = grpcBase();
  if (ep.insecure) return base.createInsecureCredentials();
  if (ep.grpcTarget !== undefined && isIP(targetHost(ep.grpcTarget)) !== 0) {
    warn('gRPC over TLS to an IP-literal endpoint: Node.js does not send an IP address as TLS server name (the handshake fails on Node 26); use a DNS name', {
      variable: ep.source,
    });
  }
  if (tls.key !== undefined && tls.cert !== undefined) {
    try {
      return base.createSslCredentials(tls.ca, tls.key, tls.cert);
    } catch {
      warn('gRPC rejected the TLS client pair; the client certificate is not used', { variable: tls.pairVariables, using: 'server authentication' });
    }
  }
  try {
    return base.createSslCredentials(tls.ca);
  } catch {
    warn('gRPC rejected the TLS CA; using the system roots', { variable: tls.caVariable });
    return base.createSslCredentials();
  }
}

/**
 * gRPC metadata from the parsed headers (printable ASCII only, A18). `-bin`
 * keys get the value's bytes (grpc-js requires a Buffer there; Go and Python
 * send the same bytes). A value grpc-js still rejects is skipped with a
 * warning naming the variable and the item position only: grpc-js puts the
 * value in its error message (R8).
 */
function grpcMetadata(headers: ResolvedHeaders | undefined, warn: Warn): Metadata {
  const metadata = grpcBase().createEmptyMetadata();
  if (headers === undefined) return metadata;
  for (const key of Object.keys(headers.values)) {
    const value = headers.values[key] ?? '';
    try {
      metadata.set(key, key.endsWith('-bin') ? Buffer.from(value, 'latin1') : value);
    } catch {
      warn('header not allowed in gRPC metadata; skipped', {
        variable: headers.variable,
        value: `item #${String(headers.positions[key] ?? 0)}`,
        using: 'remaining headers',
      });
    }
  }
  return metadata;
}

/** Everything one signal's exporter is built from. */
export interface ExporterInput {
  readonly signal: Signal;
  readonly endpoint: OtlpEndpoint;
  readonly headers: ResolvedHeaders | undefined;
  /** TLS material, resolved before the environment is masked (`undefined`: plaintext). */
  readonly tls: TlsMaterial | undefined;
  readonly warn: Warn;
}

const NO_TLS: TlsMaterial = Object.freeze({ ca: undefined, key: undefined, cert: undefined, pairVariables: '', caVariable: '' });

type GrpcConfig = NonNullable<ConstructorParameters<typeof GrpcTraces.OTLPTraceExporter>[0]>;
type HttpConfig = NonNullable<ConstructorParameters<typeof HttpTraceExporter>[0]>;

/**
 * The exporters type `compression` as an enum (`CompressionAlgorithm` of
 * `@opentelemetry/otlp-exporter-base`, not a direct dependency) whose values
 * are exactly `'gzip'` and `'none'`.
 */
// eslint-disable-next-line @typescript-eslint/no-unnecessary-type-parameters -- T is the exporter's enum type, chosen by the caller
function compressionOption<T>(compression: OtlpCompression): T {
  return compression as unknown as T;
}

function grpcConfig(input: ExporterInput): GrpcConfig {
  const { signal, endpoint } = input;
  if (endpoint.grpcTarget === undefined) throw new Error(`internal: gRPC endpoint of ${signal} has no target`);
  return {
    // Schemeless host:port: the exporter keeps it as the dial target (a URL
    // would lose a scheme-default port, design D-JS3).
    url: endpoint.grpcTarget,
    credentials: grpcCredentials(endpoint, input.tls ?? NO_TLS, input.warn),
    metadata: grpcMetadata(input.headers, input.warn),
    compression: compressionOption<NonNullable<GrpcConfig['compression']>>(endpoint.compression),
    timeoutMillis: endpoint.timeoutMs,
  };
}

function httpConfig(input: ExporterInput): HttpConfig {
  const { endpoint } = input;
  // Null prototype: a `__proto__` header key stays an ordinary key.
  const headers = Object.create(null) as Record<string, string>;
  for (const key of Object.keys(input.headers?.values ?? {})) headers[key] = input.headers?.values[key] ?? '';
  const config: HttpConfig = {
    url: endpoint.url,
    headers,
    compression: compressionOption<NonNullable<HttpConfig['compression']>>(endpoint.compression),
    timeoutMillis: endpoint.timeoutMs,
  };
  if (!endpoint.insecure) {
    // The helper's TLS material replaces the exporter's env reading (masked anyway).
    const tls = input.tls ?? NO_TLS;
    config.httpAgentOptions = { keepAlive: true, ca: tls.ca, key: tls.key, cert: tls.cert };
  }
  return config;
}

const isGrpc = (input: ExporterInput): boolean => input.endpoint.protocol === 'grpc';

/** The OTLP span exporter of a configured traces endpoint. */
export function spanExporter(input: ExporterInput): SpanExporter {
  return isGrpc(input) ? new (grpcTraces().OTLPTraceExporter)(grpcConfig(input)) : new HttpTraceExporter(httpConfig(input));
}

/**
 * The OTLP metric exporter of a configured metrics endpoint. Temporality is
 * not passed, so the exporter's own resolution applies (cumulative unless
 * `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` says otherwise; that
 * variable is not helper-owned and stays visible).
 */
export function metricExporter(input: ExporterInput): PushMetricExporter {
  return isGrpc(input) ? new (grpcMetrics().OTLPMetricExporter)(grpcConfig(input)) : new HttpMetricExporter(httpConfig(input));
}

/** The OTLP log record exporter of a configured logs endpoint. */
export function logExporter(input: ExporterInput): LogRecordExporter {
  return isGrpc(input) ? new (grpcLogs().OTLPLogExporter)(grpcConfig(input)) : new HttpLogExporter(httpConfig(input));
}
