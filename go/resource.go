package otelhelper

import (
	"errors"
	"log"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync/atomic"
	"unicode/utf8"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk"
	"go.opentelemetry.io/otel/sdk/resource"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"
)

// schemaURL is the semantic-conventions schema the helper's resource declares:
// the semconv version the helper targets, the same literal in every language
// (design D1 row "telemetry.sdk.* and the resource schema URL", 2026-10-29).
// It must equal the imported semconv package's SchemaURL (tested); bumping it
// is a contract change.
const schemaURL = "https://opentelemetry.io/schemas/1.43.0"

// parsedResourceAttributes is the result of parseResourceAttributes.
type parsedResourceAttributes struct {
	attrs map[string]string // valid key=value pairs; a later duplicate key wins
	// 1-based positions of items without "=", with an empty key or with a key
	// that is not UTF-8 (skipped).
	malformed []int
	// 1-based positions whose value is an invalid percent-encoding or not
	// UTF-8 once decoded (kept literally).
	undecodable []int
	// blank is true when some item is blank: ignored silently by the helper,
	// reported by the SDK's own parser as a missing value.
	blank bool
}

// sdkWouldEcho reports whether the SDK's own parse of the same value calls
// otel.Handle: for an item without "=" (a blank one included) or a bad escape.
func (p parsedResourceAttributes) sdkWouldEcho() bool {
	return p.blank || len(p.malformed) > 0 || len(p.undecodable) > 0
}

// parseResourceAttributes parses OTEL_RESOURCE_ATTRIBUTES (design D1 row
// "OTEL_RESOURCE_ATTRIBUTES parsing", 2026-10-29). The helper owns this parse:
// the SDK's echoes malformed items through otel.Handle (R8) and trims Unicode
// whitespace before decoding. Rules: split on ","; blank items are ignored;
// each item splits at its first "="; the key is trimmed with the contract set
// (empty or not UTF-8: the item is malformed); the value is percent-decoded
// and then trimmed. An invalid escape or a decode that is not UTF-8 keeps the
// value literal (still trimmed). Values never reach diagnostics: callers
// report positions only.
func parseResourceAttributes(raw string) parsedResourceAttributes {
	p := parsedResourceAttributes{attrs: map[string]string{}}
	for i, item := range strings.Split(raw, ",") {
		if trimWS(item) == "" {
			p.blank = true
			continue
		}
		k, v, found := strings.Cut(item, "=")
		key := trimWS(k)
		if !found || key == "" || !utf8.ValidString(key) {
			p.malformed = append(p.malformed, i+1)
			continue
		}
		decoded, err := url.PathUnescape(v)
		if err != nil || !utf8.ValidString(decoded) {
			p.undecodable = append(p.undecodable, i+1)
			// A raw env value can itself be invalid UTF-8; OTLP strings must be UTF-8.
			decoded = replaceInvalidUTF8(v)
		}
		p.attrs[key] = trimWS(decoded)
	}
	return p
}

// replaceInvalidUTF8 replaces each invalid byte with U+FFFD — one per byte,
// unlike strings.ToValidUTF8, which replaces a whole run with one — so the
// literal matches what the Python helper keeps for the same bytes.
func replaceInvalidUTF8(s string) string {
	if utf8.ValidString(s) {
		return s
	}
	var b strings.Builder
	for i := 0; i < len(s); {
		r, size := utf8.DecodeRuneInString(s[i:])
		if r == utf8.RuneError && size == 1 {
			b.WriteRune(utf8.RuneError)
		} else {
			b.WriteString(s[i : i+size])
		}
		i += size
	}
	return b.String()
}

// envResourceAttributes parses OTEL_RESOURCE_ATTRIBUTES and warns, once per
// process and by item position only, about the items it skipped or kept
// literally (design D1 identity-warning cadence row).
func envResourceAttributes() parsedResourceAttributes {
	raw := os.Getenv(EnvResourceAttributes)
	if trimWS(raw) == "" {
		return parsedResourceAttributes{attrs: map[string]string{}}
	}
	p := parseResourceAttributes(raw)
	if len(p.malformed) > 0 {
		warnOncePerProcess("resource-attributes:malformed",
			"otelhelper: resource attribute items are not key=value and were skipped",
			"variable", EnvResourceAttributes, "items", positions(p.malformed))
	}
	if len(p.undecodable) > 0 {
		warnOncePerProcess("resource-attributes:undecodable",
			"otelhelper: resource attribute values have an invalid percent-encoding and are kept literally",
			"variable", EnvResourceAttributes, "items", positions(p.undecodable))
	}
	return p
}

func positions(items []int) string {
	s := make([]string, len(items))
	for i, n := range items {
		s[i] = strconv.Itoa(n)
	}
	return strings.Join(s, ",")
}

// defaultServiceName is the SDK's default service.name, unknown_service:<exe>
// (computed like sdk/resource's defaultServiceNameDetector, which is not
// exported and only reachable through resource.Default(), which reads the env).
func defaultServiceName() string {
	exe, err := os.Executable()
	if err != nil {
		return "unknown_service:go"
	}
	return "unknown_service:" + filepath.Base(exe)
}

// telemetrySDKAttributes are the SDK's telemetry.sdk.* attributes. They always
// come from the SDK: env and code values for these keys are ignored.
func telemetrySDKAttributes() []attribute.KeyValue {
	return []attribute.KeyValue{
		semconv.TelemetrySDKName("opentelemetry"),
		semconv.TelemetrySDKLanguageGo,
		semconv.TelemetrySDKVersion(sdk.Version()),
	}
}

func isTelemetrySDKKey(k string) bool {
	switch attribute.Key(k) {
	case semconv.TelemetrySDKNameKey, semconv.TelemetrySDKLanguageKey, semconv.TelemetrySDKVersionKey:
		return true
	}
	return false
}

// resourceEnvEchoFilter is the global OTel error handler while Setup builds
// the providers (design D1 row "Go: SDK providers re-read
// OTEL_RESOURCE_ATTRIBUTES"). sdktrace/sdkmetric/sdklog.WithResource merge
// resource.Environment(), whose parser hands every malformed item, verbatim,
// to otel.Handle. While active, the filter drops exactly those errors
// (resource.ErrPartialResource and a bare url.EscapeError — exporters wrap
// theirs in *url.Error, which passes) and forwards everything else to the
// handler that was installed before. Once inactive it forwards everything to
// the current global handler.
type resourceEnvEchoFilter struct {
	prev   otel.ErrorHandler
	active atomic.Bool
}

// Handle implements otel.ErrorHandler.
func (f *resourceEnvEchoFilter) Handle(err error) {
	active := f.active.Load()
	if active && isResourceEnvEcho(err) {
		return
	}
	target := f.prev
	if !active {
		target = otel.GetErrorHandler()
		if cur, ok := target.(*resourceEnvEchoFilter); ok && cur == f {
			// Between stop's active.Store(false) and SetErrorHandler(f.prev)
			// the filter is still global: forward to the handler it replaced.
			target = f.prev
		}
	}
	if forwardsBackToFilter(target) {
		// The SDK default: the first SetErrorHandler made the default
		// delegator forward to this filter, so calling it would loop.
		log.Print(err)
		return
	}
	target.Handle(err)
}

func isResourceEnvEcho(err error) bool {
	if errors.Is(err, resource.ErrPartialResource) {
		return true
	}
	_, bare := err.(url.EscapeError) //nolint:errorlint // a wrapped *url.Error comes from an exporter: keep it
	return bare
}

// forwardsBackToFilter reports whether h may route errors back into a filter:
// a filter itself, or the SDK's default delegating handler (an internal type,
// recognised by name; its delegate is the first handler ever installed).
func forwardsBackToFilter(h otel.ErrorHandler) bool {
	if h == nil {
		return true
	}
	if _, ok := h.(*resourceEnvEchoFilter); ok {
		return true
	}
	return strings.HasSuffix(reflect.TypeOf(h).String(), ".ErrDelegator")
}

// quietResourceEnvEcho installs a resourceEnvEchoFilter when needed (the
// helper's parse found something the SDK parser would echo) and returns the
// function that removes it. The previous handler is restored only if the
// filter is still the global one (an application handler set meanwhile is
// kept). Race window, documented: while the providers are being built
// (microseconds), another goroutine's resource.ErrPartialResource error is
// dropped too, and a handler it reads with otel.GetErrorHandler is the filter
// (which forwards).
func quietResourceEnvEcho(needed bool) (stop func()) {
	if !needed {
		return func() {}
	}
	f := &resourceEnvEchoFilter{prev: otel.GetErrorHandler()}
	f.active.Store(true)
	otel.SetErrorHandler(f)
	return func() {
		f.active.Store(false)
		if cur, ok := otel.GetErrorHandler().(*resourceEnvEchoFilter); ok && cur == f {
			otel.SetErrorHandler(f.prev)
		}
	}
}
