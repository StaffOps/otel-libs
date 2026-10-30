/**
 * OTLP endpoint, protocol and TLS-setting resolution (task B4a; design D-JS3
 * ownership table; contract R1, R2, R8, design D1, D1.a–D1.c and the D1
 * edge-case rulings). Pure resolution: exporters are built from the result in
 * task B4b. Endpoints are split by RFC 3986 and the host is checked by the
 * contract's one host rule (D1 rulings, 2026-10-29), the same rule as Go and
 * Python. WHATWG is only used for IDNA (`domainToASCII`, non-ASCII hosts),
 * behind a guard label so its IPv4 parsing never runs. The rows follow the contract D1
 * table; the Go helper (`go/endpoint.go`) is the closest reference, with the
 * differences the D1 rows dated 2026-10-29 record (task A17).
 */
import { isIPv6 } from 'node:net';
import { domainToASCII } from 'node:url';
import { reportOnce, truncateValue, type DiagnosticValue } from './diagnostics';
import { envBool, envValue, hasControl, trimWS, type Warn } from './env';

/** The three OTLP signals. */
export type Signal = 'traces' | 'metrics' | 'logs';
export const SIGNALS: readonly Signal[] = ['traces', 'metrics', 'logs'];

/** OTLP protocols the helper builds exporters for. */
export type OtlpProtocol = 'grpc' | 'http/protobuf';
export const PROTOCOL_GRPC: OtlpProtocol = 'grpc';
export const PROTOCOL_HTTP: OtlpProtocol = 'http/protobuf';
const PROTOCOLS: readonly string[] = [PROTOCOL_GRPC, PROTOCOL_HTTP];

export const ENV_ENDPOINT = 'OTEL_EXPORTER_OTLP_ENDPOINT';
export const ENV_PROTOCOL = 'OTEL_EXPORTER_OTLP_PROTOCOL';
export const ENV_INSECURE = 'OTEL_EXPORTER_OTLP_INSECURE';

const DEFAULT_GRPC_PORT = '4317';
const DEFAULT_HTTP_PORT = '4318';

/** Shown instead of an endpoint the helper cannot parse safely (D1 ruling). */
export const UNPARSEABLE_URL = '<unparseable URL>';

/** `OTEL_EXPORTER_OTLP[_<SIGNAL>]_<suffix>`; `signal` undefined = the generic variable. */
export function otlpEnv(signal: Signal | undefined, suffix: string): string {
  return signal === undefined ? `OTEL_EXPORTER_OTLP_${suffix}` : `OTEL_EXPORTER_OTLP_${signal.toUpperCase()}_${suffix}`;
}

/** Code-option names of the per-signal endpoints. */
export const SIGNAL_ENDPOINT_OPTION: Readonly<Record<Signal, 'tracesEndpoint' | 'metricsEndpoint' | 'logsEndpoint'>> = {
  traces: 'tracesEndpoint',
  metrics: 'metricsEndpoint',
  logs: 'logsEndpoint',
};

/** The OTLP-related code options the resolver reads (already type-checked). */
export interface OtlpCodeOptions {
  readonly otelEndpoint?: string | undefined;
  readonly tracesEndpoint?: string | undefined;
  readonly metricsEndpoint?: string | undefined;
  readonly logsEndpoint?: string | undefined;
  readonly otlpProtocol?: string | undefined;
  readonly insecure?: boolean | undefined;
}

/** How one signal is exported over OTLP (frozen; part of `Resolved.otlp`). */
export interface OtlpEndpoint {
  /** Exporter protocol: code > `_<SIGNAL>_PROTOCOL` > `OTEL_EXPORTER_OTLP_PROTOCOL` > port-4318 inference > `grpc`. */
  readonly protocol: OtlpProtocol;
  /**
   * The resolved URL, userinfo, query and fragment removed. `http/protobuf`:
   * the request URL (a generic endpoint gets `/v1/<signal>`, a per-signal one
   * keeps its path, escapes verbatim). `grpc`: `scheme://host:port`, no path,
   * for diagnostics only — B4b passes {@link grpcTarget} and explicit channel
   * credentials (from {@link insecure}) to the gRPC exporters, never this URL:
   * the exporter would derive its target from `URL.host`, which drops a
   * scheme-default port (`http://h:80` → grpc-js dials 443).
   */
  readonly url: string;
  /** gRPC dial target `host:port` (IPv6 in brackets, port always explicit); `undefined` for `http/protobuf`. */
  readonly grpcTarget: string | undefined;
  /** Plaintext (`http://`): the scheme decides, `INSECURE` only completes a schemeless endpoint (D1.c). */
  readonly insecure: boolean;
  /** Code option or environment variable the endpoint came from. */
  readonly source: string;
  /** TLS file variables that are set but ignored because the endpoint is plaintext (names only). */
  readonly ignoredTlsVariables: readonly string[];
}

/** Per-signal OTLP plan; `undefined` = no OTLP exporter for that signal. */
export type OtlpPlan = Readonly<Record<Signal, OtlpEndpoint | undefined>>;

// "<scheme>:/…" without "//" (RFC 3986 scheme syntax): a scheme, not a host (D1 ruling).
const OPAQUE_SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:\//;
// "<scheme>://<rest>": the only URL form an endpoint may take (D1 ruling: RFC 3986).
const HIER_URL = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/(.*)$/s;
const DIGITS = /^[0-9]+$/;
const MAX_PORT = 65535;

/**
 * An endpoint split by RFC 3986 (D1 ruling "Endpoint parsing follows RFC 3986,
 * not WHATWG", 2026-10-29): the authority ends only at `/`, `?` or `#`;
 * userinfo is cut at its last `@`. Nothing is decoded or normalized here.
 */
interface RfcEndpoint {
  /** Lowercased scheme. */
  readonly scheme: string;
  /** Whether the authority holds userinfo (an `@`). */
  readonly userinfo: boolean;
  /** Host as written, IPv6 in brackets; may be '' (checked by the caller). */
  readonly host: string;
  /** Port in decimal without leading zeros (1–65535), '' when none is written. */
  readonly port: string;
  /** Path as written (escapes verbatim), '' when none. */
  readonly path: string;
  /** Query / fragment as written, without `?` / `#`; `undefined` when absent. */
  readonly query: string | undefined;
  readonly fragment: string | undefined;
}

/**
 * Splits `text` (`scheme://…`) by RFC 3986; `undefined` when the authority is
 * malformed (unbalanced IPv6 brackets, a second `:`, a port that is not 1–65535).
 */
function rfcSplit(text: string): RfcEndpoint | undefined {
  const m = HIER_URL.exec(text);
  if (m === null) return undefined;
  const rest = m[2] ?? '';
  let end = rest.length;
  for (const ch of ['/', '?', '#']) {
    const i = rest.indexOf(ch);
    if (i >= 0 && i < end) end = i;
  }
  const authority = rest.slice(0, end);
  let after = rest.slice(end);
  let fragment: string | undefined;
  const hash = after.indexOf('#');
  if (hash >= 0) {
    fragment = after.slice(hash + 1);
    after = after.slice(0, hash);
  }
  let query: string | undefined;
  const q = after.indexOf('?');
  if (q >= 0) {
    query = after.slice(q + 1);
    after = after.slice(0, q);
  }
  const at = authority.lastIndexOf('@');
  const hostPort = at >= 0 ? authority.slice(at + 1) : authority;
  let host: string;
  let portText: string | undefined;
  if (hostPort.startsWith('[')) {
    const close = hostPort.indexOf(']');
    if (close < 0) return undefined;
    host = hostPort.slice(0, close + 1);
    const tail = hostPort.slice(close + 1);
    if (tail !== '') {
      if (!tail.startsWith(':')) return undefined;
      portText = tail.slice(1);
    }
  } else {
    const colon = hostPort.indexOf(':');
    host = colon >= 0 ? hostPort.slice(0, colon) : hostPort;
    if (colon >= 0) portText = hostPort.slice(colon + 1);
  }
  let port = '';
  // An empty port (`h:`) is the scheme default, as RFC 3986 allows.
  if (portText !== undefined && portText !== '') {
    if (!DIGITS.test(portText)) return undefined;
    // Leading zeros compare numerically (D1 ruling, 2026-10-29); a long digit
    // string is Infinity, i.e. out of range.
    const n = Number(portText);
    if (n < 1 || n > MAX_PORT) return undefined;
    port = String(n);
  }
  return { scheme: (m[1] ?? '').toLowerCase(), userinfo: at >= 0, host, port, path: after, query, fragment };
}

type HostCheck = { readonly ok: true; readonly hostname: string } | { readonly ok: false; readonly zoneId: boolean };

// RFC 3986 reg-name (unreserved / sub-delims); pct-encoded is rejected by the host-characters ruling.
const REG_NAME = /^[A-Za-z0-9\-._~!$&'()*+,;=]+$/;
const HEX_NUMBER = /^0[xX][0-9A-Fa-f]*$/;
const OCTET = '(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])';
const CANONICAL_IPV4 = new RegExp(`^${OCTET}(\\.${OCTET}){3}$`);
// eslint-disable-next-line no-control-regex
const NON_ASCII = /[^\x00-\x7f]/;

/** WHATWG "ends in a number": the last label (a trailing empty one ignored) is digits or hex (0x…). */
function endsInNumber(host: string): boolean {
  const labels = host.split('.');
  if (labels.length > 1 && labels[labels.length - 1] === '') labels.pop();
  const last = labels[labels.length - 1] ?? '';
  return last !== '' && (DIGITS.test(last) || HEX_NUMBER.test(last));
}

/**
 * UTS #46 ToASCII of a non-ASCII host via WHATWG `domainToASCII`, which also
 * runs WHATWG IPv4 parsing on its result: a guard label (`.x`) keeps that from
 * turning fullwidth `１２７.１` into `127.0.0.1` (A17 final round), so the one
 * host rule sees `127.1` as Go and Python do. `undefined` on an IDNA failure,
 * an empty label or more than 253 characters (what Python's idna enforces).
 */
function toASCII(host: string): string | undefined {
  const dot = host.endsWith('.') || host.endsWith('\u3002') || host.endsWith('\uff0e') || host.endsWith('\uff61');
  const bare = dot ? host.slice(0, -1) : host;
  const guarded = domainToASCII(`${bare}.x`);
  if (!guarded.endsWith('.x')) return undefined;
  const ascii = guarded.slice(0, -2);
  if (ascii === '' || ascii.length > 253 || ascii.split('.').includes('')) return undefined;
  return dot ? `${ascii}.` : ascii;
}

/**
 * Validates the host of an http(s) endpoint and returns the spelling that is
 * dialed and rendered (D1 rulings "Host characters", IDN hosts and "One host
 * rule", 2026-10-29): an empty host, a space, a `%` (percent-encoded byte or
 * zone id) or a host that fails IDNA is invalid; a non-ASCII host is punycoded
 * (WHATWG `domainToASCII`, UTS #46 non-transitional). The ASCII result must be
 * an IPv6 literal or an RFC 3986 reg-name, and a host that ends in a number a
 * canonical dotted quad. Lowercased.
 */
function checkHost(sp: RfcEndpoint): HostCheck {
  let host = sp.host;
  if (host === '' || host.includes(' ')) return { ok: false, zoneId: false };
  if (host.includes('%')) return { ok: false, zoneId: host.startsWith('[') };
  if (host.startsWith('[')) {
    const inner = host.slice(1, -1);
    return isIPv6(inner) ? { ok: true, hostname: host.toLowerCase() } : { ok: false, zoneId: false };
  }
  if (NON_ASCII.test(host)) {
    const ascii = toASCII(host);
    if (ascii === undefined) return { ok: false, zoneId: false };
    host = ascii;
  }
  // One trailing '.' is allowed on a dotted quad ("10.0.0.1."), as for a domain.
  if (!REG_NAME.test(host) || (endsInNumber(host) && !CANONICAL_IPV4.test(host.replace(/\.$/, '')))) return { ok: false, zoneId: false };
  return { ok: true, hostname: host.toLowerCase() };
}

/** An '@', raw or percent-encoded (`%40`). */
function hasAt(text: string): boolean {
  return text.includes('@') || text.toLowerCase().includes('%40');
}

/** An '@' (or `%40`) in the path or query: misplaced credentials (D1 ruling: warned, used as parsed). */
function pathHasAt(sp: RfcEndpoint): boolean {
  return hasAt(sp.path) || hasAt(sp.query ?? '');
}


// An invalid escape in an http/protobuf path (D1 ruling, A17: invalid in every language).
const BAD_PATH_ESCAPE = /%(?![0-9A-Fa-f]{2})/;

/** Shown for a schemeless endpoint whose path or query holds an '@' (D1 ruling, A17 final round), in every language. */
export const SCHEMELESS_AT_RENDERING = '<schemeless endpoint>';

/**
 * Endpoint text safe for diagnostics: scheme, host, port and path only (no
 * userinfo, query or fragment; no path either when it holds an `@`), split
 * exactly as the resolver splits it; an opaque value (`unix:/…`) renders as its
 * scheme only; {@link UNPARSEABLE_URL} for anything the split or the host check
 * rejects for `protocol` (D1 rulings; with http/protobuf an invalid path
 * escape too). When the path or query holds an `@` (or `%40`) only
 * `<scheme>://` is shown ({@link SCHEMELESS_AT_RENDERING} for a schemeless
 * value): the host and port may then be a user name and a password prefix.
 */
export function redactURL(raw: string, protocol?: OtlpProtocol): string {
  const s = trimWS(raw);
  if (s === '' || hasControl(s) || s.includes('\\')) return UNPARSEABLE_URL;
  const schemeless = !s.includes('://');
  if (schemeless && OPAQUE_SCHEME.test(s)) {
    // "unix:/path": no authority, and the rest may hold credentials (D1 ruling, A17).
    return `${(s.split(':', 1)[0] ?? '').toLowerCase()}:`;
  }
  if (schemeless && s.startsWith('/')) return UNPARSEABLE_URL;
  const text = schemeless ? `https://${s}` : s;
  const sp = rfcSplit(text);
  if (sp === undefined) return UNPARSEABLE_URL;
  let host = sp.host;
  if (sp.scheme === 'http' || sp.scheme === 'https') {
    const checked = checkHost(sp);
    if (!checked.ok) return UNPARSEABLE_URL;
    host = checked.hostname;
  }
  if (protocol === PROTOCOL_HTTP && BAD_PATH_ESCAPE.test(sp.path)) return UNPARSEABLE_URL;
  if (pathHasAt(sp)) return schemeless ? SCHEMELESS_AT_RENDERING : `${sp.scheme}://`;
  const hostPort = sp.port === '' ? host : `${host}:${sp.port}`;
  return schemeless ? `${hostPort}${sp.path}` : `${sp.scheme}://${hostPort}${sp.path}`;
}

/** Effective INSECURE setting of one signal. */
interface InsecureSetting {
  readonly value: boolean;
  /** Option or variable it came from. */
  readonly variable: string;
  readonly fromCode: boolean;
}

interface Source {
  readonly raw: string;
  readonly variable: string;
  readonly fromCode: boolean;
  readonly perSignal: boolean;
}

type Parsed =
  | { readonly kind: 'ok'; readonly scheme: 'http' | 'https'; readonly url: string; readonly grpcTarget: string | undefined }
  | { readonly kind: 'invalid' }
  | { readonly kind: 'unsupported' };

/**
 * Diagnostics callbacks of one resolution. `warn` writes a record once per
 * message, variable and value (Go `warnOnce`), however many signals share the
 * setting. An inactive copy (design D-JS10) passes no-ops: only the
 * once-per-process deprecation is still written.
 */
export interface Reporter {
  readonly warn: Warn;
  /** An INFO record, written once per resolution for `key`. */
  readonly info: (key: string, message: string, attrs: Readonly<Record<string, DiagnosticValue>>) => void;
}

const MSG_UNSUPPORTED = 'unsupported endpoint scheme (use http:// or https://)';
const MSG_SCHEMELESS_HTTP =
  'schemeless endpoint is deprecated for http/protobuf and will be removed in 1.0; use a URL (http:// or https://)';

/**
 * Resolves one endpoint value (D1.b–c and the D1 edge-case rulings). A code
 * value that is unusable throws a `TypeError` naming the option; an env value
 * warns and returns `invalid` (fall back to the next level) or `unsupported`
 * (the signal is not configured).
 */
function resolveEndpoint(
  src: Source,
  signal: Signal,
  protocol: OtlpProtocol,
  insecure: InsecureSetting | undefined,
  rep: Reporter,
): Parsed {
  const fail = (kind: 'invalid' | 'unsupported', message: string): Parsed => {
    // An invalid value is never rendered, not even partly (D1 ruling, 2026-10-29); an
    // unsupported scheme parsed, so its redacted form is shown.
    const failShown = kind === 'invalid' ? UNPARSEABLE_URL : redactURL(src.raw, protocol);
    if (src.fromCode) throw new TypeError(`setup(): ${src.variable} ${message}: ${JSON.stringify(failShown)}`);
    rep.warn(`${message}; ignored`, {
      variable: src.variable,
      value: failShown,
      using: kind === 'unsupported' ? 'no OTLP exporter for this signal' : 'next configured endpoint, else none',
    });
    return { kind };
  };
  const raw = src.raw;
  // Before parsing: a control character is unparseable, also in an opaque value (D1 ruling).
  if (hasControl(raw)) return fail('invalid', 'endpoint contains a control character');
  // WHATWG reads '\' as '/' and would split the authority elsewhere (D1 ruling: RFC 3986).
  if (raw.includes('\\')) return fail('invalid', 'endpoint contains a backslash');
  const schemeless = !raw.includes('://');
  if (schemeless && OPAQUE_SCHEME.test(raw)) return fail('unsupported', MSG_UNSUPPORTED);
  if (schemeless && raw.startsWith('/')) return fail('invalid', 'invalid endpoint');
  // Schemeless host:port (legal for gRPC): https://, http:// only with INSECURE=true (R1).
  const text = schemeless ? `${insecure?.value === true ? 'http' : 'https'}://${raw}` : raw;
  const sp = rfcSplit(text);
  if (sp === undefined) return fail('invalid', 'invalid endpoint');
  const scheme = sp.scheme;
  if (scheme !== 'http' && scheme !== 'https') return fail('unsupported', MSG_UNSUPPORTED);
  const checked = checkHost(sp);
  if (!checked.ok) {
    return fail('invalid', checked.zoneId ? 'IPv6 zone ids are not supported in endpoint URLs' : 'invalid endpoint');
  }
  // The Go and Python exporters cannot send it as written (D1 ruling, A17).
  if (protocol === PROTOCOL_HTTP && BAD_PATH_ESCAPE.test(sp.path)) return fail('invalid', 'endpoint path has an invalid percent-escape');
  let port = sp.port;
  const hostWith = (p: string): string => (p === '' ? checked.hostname : `${checked.hostname}:${p}`);
  // The scheme only when the path or query holds an '@' (R8, A17 final round).
  const shownURL = (rest: string): string => (pathHasAt(sp) ? `${scheme}://` : `${scheme}://${rest}`);
  const shown = redactURL(raw, protocol);
  if (schemeless) {
    if (port === '') port = protocol === PROTOCOL_HTTP ? DEFAULT_HTTP_PORT : DEFAULT_GRPC_PORT;
    if (protocol === PROTOCOL_HTTP) {
      // Deprecation (D8): once per process, also from an inactive copy.
      reportOnce(`deprecated:schemeless:${src.variable}`, MSG_SCHEMELESS_HTTP, {
        variable: src.variable,
        value: shown,
        using: shownURL(`${hostWith(port)}${sp.path}`),
      });
    }
  } else {
    if (insecure !== undefined && insecure.value !== (scheme === 'http')) {
      if (src.fromCode && insecure.fromCode) {
        throw new TypeError(`setup(): insecure ${String(insecure.value)} contradicts the ${scheme}:// scheme of ${src.variable}`);
      }
      rep.warn('INSECURE setting contradicts the endpoint scheme; the scheme wins', {
        variable: insecure.variable,
        value: String(insecure.value),
        using: `${scheme}://`,
      });
    }
    if (protocol === PROTOCOL_GRPC && port === '') {
      // gRPC has no scheme-default port: the exporter would dial 443 (D1.b).
      port = DEFAULT_GRPC_PORT;
      rep.warn('gRPC endpoint has no port; using 4317 — set an explicit port (e.g. :4317, or :443 behind an ingress)', {
        variable: src.variable,
        value: shown,
        using: shownURL(hostWith(port)),
      });
    }
  }
  const host = hostWith(port);
  let path = '';
  if (protocol === PROTOCOL_HTTP) {
    // Escapes kept verbatim, never decoded (D1 ruling "Percent-encoding in the endpoint path").
    path = src.perSignal ? sp.path : `${sp.path.replace(/\/+$/, '')}/v1/${signal}`;
  }
  const resolvedUrl = `${scheme}://${host}${path}`;
  if (sp.userinfo || sp.query !== undefined || sp.fragment !== undefined) {
    // Never passed on (R8): neither the exporter nor its error messages see them.
    // An empty '?' or '#' counts too (D1 ruling, 2026-10-29).
    rep.warn('endpoint userinfo/query is not supported and was removed; put credentials in OTEL_EXPORTER_OTLP_HEADERS', {
      variable: src.variable,
      value: shown,
      using: shownURL(`${host}${path}`),
    });
  }
  if (pathHasAt(sp)) {
    // Used as parsed; the variable only, the value may hold the credentials (D1 ruling).
    rep.warn("endpoint path contains '@' — misplaced credentials?", { variable: src.variable });
  }
  return { kind: 'ok', scheme, url: resolvedUrl, grpcTarget: protocol === PROTOCOL_GRPC ? host : undefined };
}

/** Port of an endpoint value, schemeless or URL ('' if none or unparseable), for the 4318 inference. */
function endpointPort(raw: string): string {
  if (hasControl(raw) || raw.includes('\\')) return '';
  return rfcSplit(raw.includes('://') ? raw : `scheme://${raw}`)?.port ?? '';
}

interface ProtocolChoice {
  readonly protocol: OtlpProtocol;
  /** Chosen by the port-4318 inference (D1.a). */
  readonly inferred: boolean;
}

/** Normalized code protocol, `undefined` when not set (an empty string is not set, D1 ruling). */
function codeProtocol(code: OtlpCodeOptions): OtlpProtocol | undefined {
  if (code.otlpProtocol === undefined) return undefined;
  const p = trimWS(code.otlpProtocol).toLowerCase();
  return p === '' ? undefined : (p as OtlpProtocol);
}

/**
 * Protocol of one signal for an endpoint value: code >
 * `OTEL_EXPORTER_OTLP_<SIGNAL>_PROTOCOL` > `OTEL_EXPORTER_OTLP_PROTOCOL` >
 * port-4318 inference on that endpoint (D1.a) > `grpc`. Invalid env values
 * warn and are skipped; `http/json` is not supported by the helper.
 */
function protocolFor(signal: Signal, raw: string, code: OtlpCodeOptions, rep: Reporter): ProtocolChoice {
  const fromCode = codeProtocol(code);
  if (fromCode !== undefined) return { protocol: fromCode, inferred: false };
  for (const variable of [otlpEnv(signal, 'PROTOCOL'), ENV_PROTOCOL]) {
    const value = envValue(variable)?.toLowerCase();
    if (value === undefined) continue;
    if (PROTOCOLS.includes(value)) return { protocol: value as OtlpProtocol, inferred: false };
    const message =
      value === 'http/json' ? 'OTLP protocol http/json is not supported by the Node.js helper; ignored' : 'unknown OTLP protocol; ignored';
    rep.warn(message, { variable, value: truncateValue(value), using: 'port inference, else grpc' });
  }
  if (endpointPort(raw) === DEFAULT_HTTP_PORT) return { protocol: PROTOCOL_HTTP, inferred: true };
  return { protocol: PROTOCOL_GRPC, inferred: false };
}

/** Effective INSECURE: code > `OTEL_EXPORTER_OTLP_<SIGNAL>_INSECURE` > `OTEL_EXPORTER_OTLP_INSECURE`. */
function insecureFor(signal: Signal, code: OtlpCodeOptions, rep: Reporter): InsecureSetting | undefined {
  if (code.insecure !== undefined) return { value: code.insecure, variable: 'insecure', fromCode: true };
  for (const variable of [otlpEnv(signal, 'INSECURE'), ENV_INSECURE]) {
    const value = envBool(variable, rep.warn);
    if (value !== undefined) return { value, variable, fromCode: false };
  }
  return undefined;
}

function nonEmpty(value: string | undefined): string {
  return value === undefined ? '' : trimWS(value);
}

/** Endpoint sources of a signal in precedence order (code per-signal > code generic > env per-signal > env generic). */
function sourcesFor(signal: Signal, code: OtlpCodeOptions): Source[] {
  const option = SIGNAL_ENDPOINT_OPTION[signal];
  return [
    { raw: nonEmpty(code[option]), variable: option, fromCode: true, perSignal: true },
    { raw: nonEmpty(code.otelEndpoint), variable: 'otelEndpoint', fromCode: true, perSignal: false },
    { raw: nonEmpty(process.env[otlpEnv(signal, 'ENDPOINT')]), variable: otlpEnv(signal, 'ENDPOINT'), fromCode: false, perSignal: true },
    { raw: nonEmpty(process.env[ENV_ENDPOINT]), variable: ENV_ENDPOINT, fromCode: false, perSignal: false },
  ];
}

const TLS_SUFFIXES = ['CERTIFICATE', 'CLIENT_CERTIFICATE', 'CLIENT_KEY'] as const;

function resolveSignal(signal: Signal, code: OtlpCodeOptions, rep: Reporter): OtlpEndpoint | undefined {
  const insecure = insecureFor(signal, code, rep);
  for (const src of sourcesFor(signal, code)) {
    if (src.raw === '') continue;
    const choice = protocolFor(signal, src.raw, code, rep);
    const parsed = resolveEndpoint(src, signal, choice.protocol, insecure, rep);
    if (parsed.kind === 'invalid') continue; // env value: fall back to the next level
    if (parsed.kind === 'unsupported') return undefined; // env value: an explicit choice, not overridden
    if (choice.inferred) {
      rep.info('infer4318', 'endpoint port 4318 and no protocol set; using http/protobuf', { set: ENV_PROTOCOL });
    }
    const ignoredTlsVariables: string[] = [];
    if (parsed.scheme === 'http') {
      for (const suffix of TLS_SUFFIXES) {
        for (const variable of [otlpEnv(signal, suffix), otlpEnv(undefined, suffix)]) {
          if (envValue(variable) !== undefined) ignoredTlsVariables.push(variable);
        }
      }
      if (ignoredTlsVariables.length > 0) {
        // Variable names only: the values are paths of key material (R8).
        rep.warn(
          'TLS files are set but the endpoint is plaintext (http:// or schemeless with INSECURE=true); they are ignored for this signal — use https:// or unset them',
          { variable: ignoredTlsVariables.join(','), value: '<file paths not shown>', using: 'plaintext' },
        );
      }
    }
    return Object.freeze({
      protocol: choice.protocol,
      url: parsed.url,
      grpcTarget: parsed.grpcTarget,
      insecure: parsed.scheme === 'http',
      source: src.variable,
      ignoredTlsVariables: Object.freeze(ignoredTlsVariables),
    });
  }
  return undefined;
}

/**
 * Resolves the OTLP endpoint of every signal (R1, R2, D1). "OTLP configured"
 * is per signal: code endpoint ∨ generic env ∨ that signal's env endpoint.
 * Throws a `TypeError` for an unusable code value.
 */
export function resolveOtlp(code: OtlpCodeOptions, rep: Reporter): OtlpPlan {
  const plan: Record<Signal, OtlpEndpoint | undefined> = { traces: undefined, metrics: undefined, logs: undefined };
  for (const signal of SIGNALS) plan[signal] = resolveSignal(signal, code, rep);
  return Object.freeze(plan);
}

const SILENT: Reporter = Object.freeze({
  warn: () => undefined,
  info: () => undefined,
});

/**
 * Fails fast on code OTLP values (R8): an unknown `otlpProtocol`, and for each
 * signal the code endpoint it would use (per-signal, else `otelEndpoint`) when
 * it is unusable or contradicts the code `insecure`. Pure: no diagnostics.
 */
export function checkCodeOtlp(code: OtlpCodeOptions): void {
  const p = codeProtocol(code);
  if (p !== undefined && !PROTOCOLS.includes(p)) {
    throw new TypeError(
      `setup(): otlpProtocol has unknown OTLP protocol ${JSON.stringify(truncateValue(code.otlpProtocol ?? ''))}; valid values: ${PROTOCOLS.join(', ')}`,
    );
  }
  const insecure: InsecureSetting | undefined =
    code.insecure === undefined ? undefined : { value: code.insecure, variable: 'insecure', fromCode: true };
  for (const signal of SIGNALS) {
    const src = sourcesFor(signal, code).find((s) => s.fromCode && s.raw !== '');
    // The protocol decides whether an invalid path escape is fatal (http/protobuf only).
    if (src !== undefined) resolveEndpoint(src, signal, protocolFor(signal, src.raw, code, SILENT).protocol, insecure, SILENT);
  }
}
