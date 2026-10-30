package otelhelper

import (
	"crypto/tls"
	"errors"
	"fmt"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// signalExport is the helper's OTLP decision for one signal (ADR-001, second
// correction). Endpoint, compression, timeout, headers and the plaintext TLS
// override are helper-resolved and passed explicitly; certificates and the
// remaining SDK defaults stay with the SDK.
type signalExport struct {
	// configured: code endpoint ∨ generic env ∨ this signal's env endpoint.
	configured bool
	protocol   string
	// endpointURL is the fully resolved URL, always passed via WithEndpointURL:
	// the SDK must never re-read a raw env endpoint the helper judged
	// differently (rejected per-signal value, schemeless, whitespace).
	endpointURL string
	// compression to pass: CompressionGzip or CompressionNone (always set).
	compression string
	// dropTLS: plaintext (http://) endpoint while env TLS files are set. The
	// helper overrides the env TLS config so the scheme wins: the HTTP
	// exporters' New() would fail, gRPC would dial TLS (design D1.c).
	dropTLS bool
	// tlsConfig: the helper-resolved TLS files of a TLS endpoint (A18), passed
	// explicitly; nil = no TLS file variable set (SDK default, system roots).
	tlsConfig *tls.Config
	// timeout: code, else a parsed env value, else 10 s — the signal's single
	// export deadline (A18).
	timeout time.Duration
	// headers parsed from OTEL_EXPORTER_OTLP[_<SIGNAL>]_HEADERS (nil = unset).
	headers map[string]string
}

// endpointInput is everything resolveEndpoint needs, so it stays pure.
type endpointInput struct {
	raw       string // endpoint as configured
	variable  string // option or env var it came from (for diagnostics)
	signal    string
	protocol  string
	fromCode  bool
	perSignal bool
	// insecure: the effective INSECURE setting for this signal, if any.
	insecure, insecureSet, insecureFromCode bool
	insecureVar                             string
}

// endpointWarning is one configuration warning. With value and using both
// empty only the variable is printed (the value may hold credentials, e.g. an
// '@' in the path).
type endpointWarning struct{ msg, variable, value, using string }

type endpointResult struct {
	explicitURL string // resolved URL passed to the SDK
	scheme      string // effective scheme: "http" or "https"
	warnings    []endpointWarning
}

// Env endpoint problems (code values return descriptive errors instead).
// opaqueScheme matches "<scheme>:/…" without "//" (RFC 3986 scheme syntax).
var opaqueScheme = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9+.-]*:/`)

var (
	errEndpointInvalid     = errors.New("invalid endpoint")
	errEndpointUnsupported = errors.New("unsupported endpoint scheme")
)

const msgUnsupportedScheme = "unsupported endpoint scheme (use http:// or https://)"

// resolveEndpoint resolves one endpoint value (design D1.b–c, D1 edge-case
// rulings): RFC 3986 split, schemeless host:port, portless gRPC URLs,
// INSECURE/scheme conflicts, userinfo/query stripping, generic →
// "/v1/<signal>" for HTTP. The result is always passed explicitly (Go
// endpoint ownership).
func resolveEndpoint(in endpointInput) (endpointResult, error) {
	var res endpointResult
	fail := func(envErr error, msg string) (endpointResult, error) {
		// An invalid value is never rendered, not even partly (D1 ruling,
		// 2026-10-29); an unsupported scheme parsed, so its redacted form is shown.
		shown := unparseableURL
		using := "next configured endpoint, else none"
		if errors.Is(envErr, errEndpointUnsupported) {
			shown, using = redactEndpoint(in.raw, in.protocol), "no OTLP exporter for this signal"
		}
		if in.fromCode {
			return res, fmt.Errorf("%s: %s %q", in.variable, msg, shown)
		}
		res.warnings = append(res.warnings, endpointWarning{msg: msg + "; ignored", variable: in.variable, value: shown, using: using})
		return res, envErr
	}
	// Before parsing: a control character is unparseable, also in an opaque
	// value (D1 ruling); Python's urlsplit would silently strip \t\r\n.
	if strings.IndexFunc(in.raw, isControl) >= 0 {
		return fail(errEndpointInvalid, "endpoint contains a control character")
	}
	// WHATWG reads '\' as '/' and would split the authority elsewhere (D1 ruling: RFC 3986).
	if strings.Contains(in.raw, `\`) {
		return fail(errEndpointInvalid, "endpoint contains a backslash")
	}
	schemeless := !strings.Contains(in.raw, "://")
	if schemeless && opaqueScheme.MatchString(in.raw) {
		// "unix:/path", "ftp:/x": a scheme, not a host named "unix".
		return fail(errEndpointUnsupported, msgUnsupportedScheme)
	}
	if schemeless && strings.HasPrefix(in.raw, "/") {
		return fail(errEndpointInvalid, "invalid endpoint")
	}
	text := in.raw
	if schemeless {
		// Schemeless host:port: legal for gRPC per spec; https:// unless INSECURE=true (R1).
		scheme := "https"
		if in.insecureSet && in.insecure {
			scheme = "http"
		}
		text = scheme + "://" + in.raw
	}
	sp, ok := rfcSplit(text)
	if !ok {
		return fail(errEndpointInvalid, "invalid endpoint")
	}
	if sp.scheme != "http" && sp.scheme != "https" {
		return fail(errEndpointUnsupported, msgUnsupportedScheme)
	}
	host, zoneID, ok := checkHost(sp.host)
	if !ok {
		if zoneID {
			return fail(errEndpointInvalid, "IPv6 zone ids are not supported in endpoint URLs")
		}
		return fail(errEndpointInvalid, "invalid endpoint")
	}
	if in.protocol == ProtocolHTTP {
		// The Go OTLP/HTTP exporters url.Parse the endpoint and would fall back
		// to their default endpoint on an invalid escape: reject it loudly.
		decoded, err := url.PathUnescape(sp.path)
		if err != nil {
			return fail(errEndpointInvalid, "endpoint path has an invalid percent-escape")
		}
		if strings.Contains(sp.path, "%") && (&url.URL{Path: decoded}).EscapedPath() != sp.path {
			// WithEndpointURL keeps url.URL.Path, not RawPath: "/a%2Fb" is sent
			// as "/a/b" (D1 note "Go: path escapes decoded", A17). Name only.
			res.warnings = append(res.warnings, endpointWarning{
				msg: "endpoint path has percent-escapes the Go OTLP/HTTP exporter sends decoded", variable: in.variable})
		}
	}
	port := sp.port
	hostWith := func(p string) string {
		if p == "" {
			return host
		}
		return host + ":" + p
	}
	// shownURL renders the resolved endpoint for a diagnostic: the scheme only
	// when the path or query holds an '@' (D1 ruling, A17 final round: the
	// "host:port" may then be "user:password-prefix", R8).
	shownURL := func(rest string) string {
		if pathHasAt(sp) {
			return sp.scheme + "://"
		}
		return sp.scheme + "://" + rest
	}
	shown := redactEndpoint(in.raw, in.protocol)
	if schemeless {
		if port == "" {
			port = defaultPortFor(in.protocol)
		}
		if in.protocol == ProtocolHTTP {
			res.warnings = append(res.warnings, endpointWarning{msg: msgSchemelessHTTPDeprecated, variable: in.variable,
				value: shown, using: shownURL(hostWith(port) + sp.path)})
		}
	} else {
		if in.insecureSet && in.insecure != (sp.scheme == "http") {
			if in.fromCode && in.insecureFromCode {
				return res, fmt.Errorf("WithInsecure: insecure=%v contradicts the %s:// scheme of %s", in.insecure, sp.scheme, in.variable)
			}
			res.warnings = append(res.warnings, endpointWarning{
				msg:      "INSECURE setting contradicts the endpoint scheme; the scheme wins",
				variable: in.insecureVar, value: strconv.FormatBool(in.insecure), using: sp.scheme + "://"})
		}
		if in.protocol == ProtocolGRPC && port == "" {
			// gRPC has no scheme-default port: the SDK would dial 443 (D1.b).
			port = defaultOtlpGRPCPort
			res.warnings = append(res.warnings, endpointWarning{
				msg:      "gRPC endpoint has no port; using 4317 — set an explicit port (e.g. :4317, or :443 behind an ingress)",
				variable: in.variable, value: shown, using: shownURL(hostWith(port))})
		}
	}
	path := ""
	if in.protocol == ProtocolHTTP {
		// Escapes kept as written in the resolved URL (D1 ruling "Percent-encoding
		// in the endpoint path"); see the open item on the SDK's re-encoding.
		if in.perSignal {
			path = sp.path
		} else {
			path = strings.TrimRight(sp.path, "/") + "/v1/" + in.signal
		}
	}
	res.scheme = sp.scheme
	res.explicitURL = sp.scheme + "://" + hostWith(port) + path
	if sp.userinfo || sp.hasQuery || sp.hasFragment {
		// Not supported in any endpoint: the helper takes ownership of the URL
		// so neither the SDK nor its error messages ever see them (R8). An empty
		// '?' or '#' counts too (D1 ruling, 2026-10-29).
		res.warnings = append(res.warnings, endpointWarning{
			msg:      "endpoint userinfo/query is not supported and was removed; put credentials in OTEL_EXPORTER_OTLP_HEADERS",
			variable: in.variable, value: shown, using: shownURL(hostWith(port) + path)})
	}
	if pathHasAt(sp) {
		// Used as parsed; the variable only, the value may hold the credentials (D1 ruling).
		res.warnings = append(res.warnings, endpointWarning{
			msg: "endpoint path contains '@' — misplaced credentials?", variable: in.variable})
	}
	return res, nil
}

func (o *Options) codeSignalEndpoint(signal string) (value, option string) {
	switch signal {
	case signalTraces:
		return o.TracesEndpoint, "WithTracesEndpoint"
	case signalMetrics:
		return o.MetricsEndpoint, "WithMetricsEndpoint"
	default:
		return o.LogsEndpoint, "WithLogsEndpoint"
	}
}

// msgSchemelessHTTPDeprecated is the deprecation warning for a schemeless
// http/protobuf endpoint (design D8); unlike other endpoint warnings it is
// emitted once per process, not once per resolution.
const msgSchemelessHTTPDeprecated = "schemeless endpoint is deprecated for http/protobuf and will be removed in 1.0; use a URL (http:// or https://)"

// warnOnce logs a configuration warning once per resolution (the same
// generic setting would otherwise warn for each of the three signals).
func (o *Options) warnOnce(w endpointWarning) {
	key := w.msg + "\x00" + w.variable + "\x00" + w.value
	if w.msg == msgSchemelessHTTPDeprecated {
		warnEnvOncePerProcess(key, w.msg, w.variable, w.value, w.using)
		return
	}
	if o.warned == nil {
		o.warned = map[string]bool{}
	}
	if o.warned[key] {
		return
	}
	o.warned[key] = true
	if w.value == "" && w.using == "" {
		diagLogger.Warn("otelhelper: "+w.msg, "variable", w.variable)
		return
	}
	warnEnv(w.msg, w.variable, w.value, w.using)
}

// envBoolOnce is envBool with the warning deduplicated across signals.
func (o *Options) envBoolOnce(key string) (value, set bool) {
	raw := trimWS(os.Getenv(key))
	if raw == "" {
		return false, false
	}
	if v := strings.ToLower(raw); v != "true" && v != "false" {
		o.warnOnce(endpointWarning{msg: "unparseable boolean", variable: key, value: raw, using: "false"})
	}
	return strings.EqualFold(raw, "true"), true
}

// export returns (and caches) the OTLP decision for a signal.
func (o *Options) export(signal string) *signalExport {
	if e, ok := o.exports[signal]; ok {
		return e
	}
	if o.exports == nil {
		o.exports = map[string]*signalExport{}
	}
	e, err := o.resolveExport(signal)
	if err != nil {
		o.resolveErrs = append(o.resolveErrs, err)
	}
	o.exports[signal] = e
	return e
}

func (o *Options) resolveExport(signal string) (*signalExport, error) {
	e := &signalExport{}
	codeSignal, codeSignalOpt := o.codeSignalEndpoint(signal)
	sources := []endpointInput{
		{raw: trimWS(codeSignal), variable: codeSignalOpt, fromCode: true, perSignal: true},
		{raw: trimWS(o.OtelEndpoint), variable: "WithEndpoint", fromCode: true},
		{raw: trimWS(os.Getenv(otlpEnv(signal, "ENDPOINT"))), variable: otlpEnv(signal, "ENDPOINT"), perSignal: true},
		{raw: trimWS(os.Getenv(EnvCollectorEndpoint)), variable: EnvCollectorEndpoint},
	}

	// Effective INSECURE: code > per-signal env > generic env.
	var insecure, insecureSet, insecureFromCode bool
	var insecureVar string
	if o.insecureExplicit || o.Insecure {
		insecure, insecureSet, insecureFromCode, insecureVar = o.Insecure, true, true, "WithInsecure"
	} else {
		for _, key := range []string{otlpEnv(signal, "INSECURE"), EnvInsecure} {
			if v, set := o.envBoolOnce(key); set {
				insecure, insecureSet, insecureVar = v, true, key
				break
			}
		}
	}

	var res endpointResult
	var in endpointInput
	found := false
	for _, src := range sources {
		if src.raw == "" {
			continue
		}
		in = src
		in.signal = signal
		in.protocol = o.signalProtocol(signal, in.raw)
		in.insecure, in.insecureSet, in.insecureFromCode, in.insecureVar = insecure, insecureSet, insecureFromCode, insecureVar
		var err error
		res, err = resolveEndpoint(in)
		for _, w := range res.warnings {
			o.warnOnce(w)
		}
		switch {
		case err == nil:
			found = true
		case errors.Is(err, errEndpointInvalid):
			continue // invalid env value: fall back to the next level (generic)
		case errors.Is(err, errEndpointUnsupported):
			return e, nil // unsupported env scheme: signal not configured
		default:
			return e, err // code value
		}
		break
	}
	if !found {
		return e, nil
	}
	e.configured = true
	e.protocol = in.protocol
	e.endpointURL = res.explicitURL

	if res.scheme == "http" {
		var tlsVars []string
		for _, suffix := range []string{"CERTIFICATE", "CLIENT_CERTIFICATE", "CLIENT_KEY"} {
			for _, key := range []string{otlpEnv(signal, suffix), otlpEnv("", suffix)} {
				if trimWS(os.Getenv(key)) != "" {
					tlsVars = append(tlsVars, key)
				}
			}
		}
		if len(tlsVars) > 0 {
			// Only variable names are printed: the values are file paths of
			// key material (R8).
			o.warnOnce(endpointWarning{
				"TLS files are set but the endpoint is plaintext (http:// or schemeless with INSECURE=true); they are ignored for this signal — use https:// or unset them",
				strings.Join(tlsVars, ","), "<file paths not shown>", "plaintext"})
			e.dropTLS = true
		}
	} else {
		e.tlsConfig = o.checkTLSFiles(signal)
	}

	e.compression = o.resolveCompression(signal)
	e.timeout = o.resolveTimeout(signal)
	e.headers = o.resolveHeaders(signal)
	return e, nil
}

// resolveCompression: code > OTEL_EXPORTER_OTLP_<SIGNAL>_COMPRESSION >
// OTEL_EXPORTER_OTLP_COMPRESSION > gzip (D1.d). Env values are
// case-insensitive gzip/none; anything else warns and falls back to the next
// level.
func (o *Options) resolveCompression(signal string) string {
	if c := strings.ToLower(trimWS(o.Compression)); c != "" {
		return c // validated in validate()
	}
	for _, key := range []string{otlpEnv(signal, "COMPRESSION"), otlpEnv("", "COMPRESSION")} {
		raw := trimWS(os.Getenv(key))
		if raw == "" {
			continue
		}
		switch v := strings.ToLower(raw); v {
		case CompressionGzip, CompressionNone:
			return v
		default:
			o.warnOnce(endpointWarning{msg: "unsupported compression (gzip or none)", variable: key, value: raw, using: "next level, else gzip"})
		}
	}
	return CompressionGzip
}

const (
	defaultOtlpTimeout = 10 * time.Second
	minOtlpTimeout     = time.Millisecond
	maxOtlpTimeout     = 24 * time.Hour
)

// decimalMs accepts plain decimal milliseconds ("500", "250.5"), not hex,
// exponents, signs or Inf/NaN spellings.
var decimalMs = regexp.MustCompile(`^[0-9]+(\.[0-9]+)?$`)

// resolveTimeout: code > OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT >
// OTEL_EXPORTER_OTLP_TIMEOUT (decimal milliseconds) > 10 s. Unparseable or
// zero values warn and fall back to the next level; values outside
// [1 ms, 24 h] are clamped with a warning. The result is the signal's single
// export deadline, always passed to the exporter and to its batch processor
// or periodic reader (D1 ruling "Export deadline", A18).
func (o *Options) resolveTimeout(signal string) time.Duration {
	if o.ExportTimeoutMs > 0 {
		return time.Duration(o.ExportTimeoutMs) * time.Millisecond
	}
	for _, key := range []string{otlpEnv(signal, "TIMEOUT"), otlpEnv("", "TIMEOUT")} {
		raw := trimWS(os.Getenv(key))
		if raw == "" {
			continue
		}
		ms, err := strconv.ParseFloat(raw, 64)
		if !decimalMs.MatchString(raw) || err != nil || ms <= 0 {
			o.warnOnce(endpointWarning{msg: "invalid timeout (positive decimal milliseconds)", variable: key, value: raw, using: "next level, else 10000"})
			continue
		}
		// Compare in float before converting: 1e30 ms overflows Duration.
		switch {
		case ms > float64(maxOtlpTimeout/time.Millisecond):
			o.warnOnce(endpointWarning{msg: "timeout above 24 h; clamped", variable: key, value: raw, using: "86400000"})
			return maxOtlpTimeout
		case ms < 1:
			o.warnOnce(endpointWarning{msg: "timeout below 1 ms; clamped", variable: key, value: raw, using: "1"})
			return minOtlpTimeout
		}
		return time.Duration(ms * float64(time.Millisecond))
	}
	return defaultOtlpTimeout
}

// resolveHeaders parses OTEL_EXPORTER_OTLP_<SIGNAL>_HEADERS, else the
// generic variable (W3C-baggage-style key=value list). When the per-signal
// variable has no valid item the generic one is used. Warnings name the item
// position only — header values are secrets (R8). nil = no variable set.
func (o *Options) resolveHeaders(signal string) map[string]string {
	var result map[string]string
	for _, key := range []string{otlpEnv(signal, "HEADERS"), otlpEnv("", "HEADERS")} {
		raw := os.Getenv(key)
		if trimWS(raw) == "" {
			continue
		}
		headers, skipped, literal := parseHeaders(raw)
		for _, pos := range skipped {
			o.warnOnce(endpointWarning{msg: "malformed header item skipped", variable: key, value: "item #" + strconv.Itoa(pos), using: "remaining headers"})
		}
		for _, pos := range literal {
			o.warnOnce(endpointWarning{msg: "invalid percent-escape in header value; kept literally", variable: key, value: "item #" + strconv.Itoa(pos), using: "literal value"})
		}
		if len(headers) > 0 {
			return headers
		}
		if result == nil {
			result = headers // empty: overrides the SDK's own (bad) parse
		}
	}
	return result
}

// isControl reports a byte < 0x20 or 0x7F (DEL).
func isControl(r rune) bool { return r < 0x20 || r == 0x7f }

// notPrintableASCII reports a rune outside 0x20–0x7E.
func notPrintableASCII(r rune) bool { return r < 0x20 || r > 0x7e }

// headerKey is the intersection of HTTP tokens and gRPC metadata keys.
var headerKey = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)

// parseHeaders returns the valid pairs (keys lowercased, last one wins), the
// 1-based positions of skipped items (empty, no '=', invalid key, decoded
// control character) and of values kept literally (invalid percent-escape or
// a decode that is not valid UTF-8). Keys are not
// percent-decoded; values are, then trimmed.
func parseHeaders(raw string) (headers map[string]string, skipped, literal []int) {
	headers = map[string]string{}
	for i, item := range strings.Split(raw, ",") {
		k, v, ok := strings.Cut(item, "=")
		key := trimWS(k)
		if !ok || !headerKey.MatchString(key) {
			skipped = append(skipped, i+1)
			continue
		}
		val, err := url.PathUnescape(v)
		if err != nil || !utf8.ValidString(val) {
			val = v // whole value literal, no partial decoding
			literal = append(literal, i+1)
		}
		val = trimWS(val)
		if strings.IndexFunc(val, notPrintableASCII) >= 0 {
			// e.g. %0D%0A or %C3%A9: printable ASCII only (D1 ruling
			// "Header bytes", A18; grpc-go fails the whole export on a
			// non-ASCII value). Only the "skipped" warning: drop a
			// "literal" mark for the same item.
			if n := len(literal); n > 0 && literal[n-1] == i+1 {
				literal = literal[:n-1]
			}
			skipped = append(skipped, i+1)
			continue
		}
		headers[strings.ToLower(key)] = val
	}
	return headers, skipped, literal
}

// signalProtocol resolves the OTLP protocol of a signal: code >
// OTEL_EXPORTER_OTLP_<SIGNAL>_PROTOCOL > OTEL_EXPORTER_OTLP_PROTOCOL >
// port-4318 inference on that signal's endpoint (D1.a) > grpc. An invalid code
// value is rejected by validate(); invalid env values warn and are skipped.
func (o *Options) signalProtocol(signal, endpoint string) string {
	if p := strings.ToLower(trimWS(o.OtlpProtocol)); p != "" {
		if p == ProtocolHTTP {
			return ProtocolHTTP
		}
		return ProtocolGRPC
	}
	for _, key := range []string{otlpEnv(signal, "PROTOCOL"), EnvOtlpProtocol} {
		v := strings.ToLower(trimWS(os.Getenv(key)))
		switch v {
		case "":
			continue
		case ProtocolGRPC, ProtocolHTTP:
			return v
		case "http/json":
			o.warnOnce(endpointWarning{msg: "OTLP protocol http/json is not supported by the Go exporters; ignored", variable: key, value: v, using: "port inference, else grpc"})
		default:
			o.warnOnce(endpointWarning{msg: "unknown OTLP protocol; ignored", variable: key, value: v, using: "port inference, else grpc"})
		}
	}
	if endpointPort(endpoint) == defaultOtlpHTTPPort {
		if o.warned == nil {
			o.warned = map[string]bool{}
		}
		if !o.warned["infer4318"] {
			o.warned["infer4318"] = true
			diagLogger.Info("otelhelper: endpoint port 4318 and no protocol set; using http/protobuf",
				"set", EnvOtlpProtocol)
		}
		return ProtocolHTTP
	}
	return ProtocolGRPC
}
