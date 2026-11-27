package otelhelper

import (
	"net"
	"net/url"
	"regexp"
	"strconv"
	"strings"

	"golang.org/x/net/idna"
)

// unparseableURL is shown instead of an endpoint the helper cannot parse
// safely, in every language and every diagnostic (D1 ruling, 2026-10-29).
const unparseableURL = "<unparseable URL>"

// hierURL matches "<scheme>://", the only URL form an endpoint may take (D1
// ruling "Endpoint parsing follows RFC 3986, not WHATWG", 2026-10-29).
var hierURL = regexp.MustCompile(`^([A-Za-z][A-Za-z0-9+.-]*)://`)

const maxPort = 65535

// rfcEndpoint is an endpoint split by RFC 3986: the authority ends only at
// '/', '?' or '#'; userinfo is cut at its last '@'. Nothing is decoded or
// normalized except the scheme (lowercased) and the port (numeric).
type rfcEndpoint struct {
	scheme   string
	userinfo bool
	// host as written, IPv6 in brackets; may be "" (checked by checkHost).
	host string
	// port in decimal without leading zeros (1–65535), "" when none is written.
	port string
	// path as written (escapes verbatim), "" when none.
	path string
	// hasQuery / hasFragment: a '?' / '#' is present, even with nothing after it.
	hasQuery, hasFragment bool
	query                 string
}

// rfcSplit splits text ("scheme://…"); ok is false when the authority is
// malformed (unbalanced IPv6 brackets, a second ':', a port that is not
// 1–65535 — port 0 included).
func rfcSplit(text string) (sp rfcEndpoint, ok bool) {
	m := hierURL.FindStringSubmatch(text)
	if m == nil {
		return sp, false
	}
	sp.scheme = strings.ToLower(m[1])
	rest := text[len(m[0]):]
	end := strings.IndexAny(rest, "/?#")
	if end < 0 {
		end = len(rest)
	}
	authority, after := rest[:end], rest[end:]
	if i := strings.IndexByte(after, '#'); i >= 0 {
		sp.hasFragment = true
		after = after[:i]
	}
	if i := strings.IndexByte(after, '?'); i >= 0 {
		sp.hasQuery, sp.query = true, after[i+1:]
		after = after[:i]
	}
	sp.path = after
	hostPort := authority
	if i := strings.LastIndexByte(authority, '@'); i >= 0 {
		sp.userinfo = true
		hostPort = authority[i+1:]
	}
	var portText string
	if strings.HasPrefix(hostPort, "[") {
		closing := strings.IndexByte(hostPort, ']')
		if closing < 0 {
			return sp, false
		}
		sp.host = hostPort[:closing+1]
		if tail := hostPort[closing+1:]; tail != "" {
			if tail[0] != ':' {
				return sp, false
			}
			portText = tail[1:]
		}
	} else {
		sp.host, portText, _ = strings.Cut(hostPort, ":")
	}
	// An empty port ("h:") is the scheme default, as RFC 3986 allows.
	if portText != "" {
		port, valid := parsePort(portText)
		if !valid {
			return sp, false
		}
		sp.port = port
	}
	return sp, true
}

// parsePort compares a port numerically (D1 ruling: "04318" = 4318) and
// accepts only 1–65535.
func parsePort(text string) (string, bool) {
	for i := 0; i < len(text); i++ {
		if text[i] < '0' || text[i] > '9' {
			return "", false
		}
	}
	digits := strings.TrimLeft(text, "0")
	if digits == "" || len(digits) > 5 {
		return "", false
	}
	n, _ := strconv.Atoi(digits) // ≤ 5 ASCII digits: cannot fail
	if n > maxPort {
		return "", false
	}
	return digits, true
}

// idnaProfile is UTS #46 ToASCII as WHATWG URL runs it for a domain
// (non-transitional, no STD3 rules, no hyphen checks; Bidi and ContextJ on),
// so a host the Node helper accepts converts the same way here.
var idnaProfile = idna.New(idna.MapForLookup(), idna.BidiRule(), idna.Transitional(false),
	idna.StrictDomainName(false), idna.CheckHyphens(false))

// isRegNameByte reports an RFC 3986 reg-name byte (unreserved / sub-delims);
// pct-encoded bytes are rejected by the host-characters ruling.
func isRegNameByte(c byte) bool {
	switch {
	case 'a' <= c && c <= 'z', 'A' <= c && c <= 'Z', '0' <= c && c <= '9':
		return true
	}
	return strings.IndexByte("-._~!$&'()*+,;=", c) >= 0
}

// hexNumber is a WHATWG IPv4 number in hex form ("0x", "0x7f").
var hexNumber = regexp.MustCompile(`^0[xX][0-9A-Fa-f]*$`)

// canonicalIPv4 is a dotted quad, each octet 0–255 written without leading zeros.
var canonicalIPv4 = regexp.MustCompile(`^(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}$`)

// endsInNumber reports a host whose last label (a trailing empty label
// ignored) is decimal digits or a hex number — WHATWG URL "ends in a number",
// which it would read as an IPv4 address (127.1, 0x7f.0.0.1, 2130706433).
func endsInNumber(host string) bool {
	labels := strings.Split(host, ".")
	if len(labels) > 1 && labels[len(labels)-1] == "" {
		labels = labels[:len(labels)-1]
	}
	last := labels[len(labels)-1]
	if last == "" {
		return false
	}
	if hexNumber.MatchString(last) {
		return true
	}
	for i := 0; i < len(last); i++ {
		if last[i] < '0' || last[i] > '9' {
			return false
		}
	}
	return true
}

// validDomainLength reports a converted IDN host (one trailing '.' ignored) of
// at most 253 characters with no empty label — what Python's idna enforces
// (D1 IDN row, A17 final round).
func validDomainLength(ascii string) bool {
	trimmed := strings.TrimSuffix(ascii, ".")
	if trimmed == "" || len(trimmed) > 253 {
		return false
	}
	for _, label := range strings.Split(trimmed, ".") {
		if label == "" {
			return false
		}
	}
	return true
}

// checkHost validates the host of an http(s) endpoint and returns the spelling
// that is dialed and rendered (D1 rulings "Host characters", IDN hosts and
// "One host rule", 2026-10-29): an empty host, a space, a '%' (percent-encoded
// byte or an unescaped IPv6 zone id) or a host that fails IDNA conversion is
// invalid; a non-ASCII host is converted to punycode. The (ASCII) result must
// be an IPv6 literal or an RFC 3986 reg-name, and a host that ends in a number
// must be a canonical dotted-quad IPv4 address. Hosts are lowercased. zoneID
// reports an IPv6 zone id.
func checkHost(host string) (normalized string, zoneID, ok bool) {
	switch {
	case host == "" || strings.ContainsRune(host, ' '):
		return "", false, false
	case strings.ContainsRune(host, '%'):
		return "", strings.HasPrefix(host, "["), false
	case strings.HasPrefix(host, "["):
		inner := host[1 : len(host)-1]
		if !strings.Contains(inner, ":") || net.ParseIP(inner) == nil {
			return "", false, false
		}
		return strings.ToLower(host), false, true
	}
	if !isASCII(host) {
		ascii, err := idnaProfile.ToASCII(host)
		if err != nil || !validDomainLength(ascii) {
			return "", false, false
		}
		host = ascii
	}
	for i := 0; i < len(host); i++ {
		if !isRegNameByte(host[i]) {
			return "", false, false
		}
	}
	// One trailing '.' is allowed on a dotted quad ("10.0.0.1."), as for a domain.
	if endsInNumber(host) && !canonicalIPv4.MatchString(strings.TrimSuffix(host, ".")) {
		return "", false, false
	}
	return strings.ToLower(host), false, true
}

// hasAt reports an '@', raw or percent-encoded (%40).
func hasAt(s string) bool {
	return strings.Contains(s, "@") || strings.Contains(strings.ToLower(s), "%40")
}

// pathHasAt reports an '@' (or %40) in the path or query (D1 ruling: warned, used as parsed).
func pathHasAt(sp rfcEndpoint) bool {
	return hasAt(sp.path) || hasAt(sp.query)
}

// schemelessAtRendering is shown for a schemeless endpoint whose path or query
// holds an '@' (D1 ruling, A17 final round), in every language.
const schemelessAtRendering = "<schemeless endpoint>"

// redactURL is redactEndpoint without a protocol (no protocol-dependent check).
func redactURL(raw string) string { return redactEndpoint(raw, "") }

// redactEndpoint renders an endpoint for diagnostics: scheme, host, port and
// path only (no userinfo, query or fragment), split exactly as resolveEndpoint
// splits it. When the path or query holds an '@' (or %40) only "<scheme>://"
// is shown (schemelessAtRendering for a schemeless value); an opaque value
// ("unix:/…") renders as its scheme only. unparseableURL for anything the
// resolver rejects for protocol (R8, D1 rulings): with ProtocolHTTP, an
// invalid path escape too.
func redactEndpoint(raw, protocol string) string {
	s := trimWS(raw)
	if s == "" || strings.IndexFunc(s, isControl) >= 0 || strings.Contains(s, `\`) {
		return unparseableURL
	}
	schemeless := !strings.Contains(s, "://")
	if schemeless && opaqueScheme.MatchString(s) {
		// "unix:/path": no authority, and the rest may hold credentials (D1 ruling, A17).
		scheme, _, _ := strings.Cut(s, ":")
		return strings.ToLower(scheme) + ":"
	}
	if schemeless && strings.HasPrefix(s, "/") {
		return unparseableURL
	}
	text := s
	if schemeless {
		text = "https://" + s
	}
	sp, ok := rfcSplit(text)
	if !ok {
		return unparseableURL
	}
	host := sp.host
	if sp.scheme == "http" || sp.scheme == "https" {
		if host, _, ok = checkHost(sp.host); !ok {
			return unparseableURL
		}
	}
	if protocol == ProtocolHTTP {
		if _, err := url.PathUnescape(sp.path); err != nil {
			return unparseableURL
		}
	}
	if pathHasAt(sp) {
		if schemeless {
			return schemelessAtRendering
		}
		return sp.scheme + "://"
	}
	if sp.port != "" {
		host += ":" + sp.port
	}
	if schemeless {
		return host + sp.path
	}
	return sp.scheme + "://" + host + sp.path
}

// endpointPort returns the port of an endpoint, schemeless or URL, as the
// resolver reads it ("" if none or unparseable) — for the port-4318 inference.
func endpointPort(raw string) string {
	s := trimWS(raw)
	if strings.IndexFunc(s, isControl) >= 0 || strings.Contains(s, `\`) {
		return ""
	}
	if !strings.Contains(s, "://") {
		s = "scheme://" + s
	}
	sp, ok := rfcSplit(s)
	if !ok {
		return ""
	}
	return sp.port
}
