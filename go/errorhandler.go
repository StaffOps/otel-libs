package otelhelper

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"unicode/utf8"

	"go.opentelemetry.io/otel"
	"google.golang.org/grpc/status"
)

// Design D1 row "SDK error text: response bodies and header values" (A29):
// text the SDK hands to the global error handler never carries an OTLP/HTTP
// response body or a configured OTLP header value.

// bodyPrefix starts the error the OTLP/HTTP exporters wrap around a non-2xx
// response body (otlptracehttp / otlpmetrichttp v1.47.0 and otlploghttp
// v0.23.0 client.go: fmt.Errorf("body: %s", respStr)).
const bodyPrefix = "body: "

// minMaskedValue is the shortest configured header value that is masked
// (Node MIN_MASKED_SECRET): masking "1" everywhere would wreck the text and
// protect nothing.
const minMaskedValue = 4

const redactedValue = "<redacted>"

// The sanitized chain (A29 fix round, code-review M2 / dev M2): the error a
// handler receives is a rebuilt chain whose every level has the sanitized
// text of the original level, so walking Unwrap never reaches a response body
// or a configured header value. errors.Is keeps working (each level's Is
// delegates to the original) and status.FromError returns the code and the
// sanitized text, without the status details. errors.As fails for every
// typed error of the original chain — the SDK's, and standard-library ones
// such as *url.Error too: only the gRPC status and errors.Is are preserved.

// sanitizedError is one level of a sanitized chain.
type sanitizedError struct {
	msg  string
	orig error
	next error
}

func (e *sanitizedError) Error() string { return e.msg }
func (e *sanitizedError) Unwrap() error { return e.next }

// Is reports whether the original level's chain matches target.
func (e *sanitizedError) Is(target error) bool { return errors.Is(e.orig, target) }

// sanitizedJoin is a level of a sanitized chain with several causes
// (errors.Join, fmt.Errorf with several %w).
type sanitizedJoin struct {
	msg   string
	orig  error
	nexts []error
}

func (e *sanitizedJoin) Error() string   { return e.msg }
func (e *sanitizedJoin) Unwrap() []error { return e.nexts }

// Is reports whether the original level's chain matches target.
func (e *sanitizedJoin) Is(target error) bool { return errors.Is(e.orig, target) }

// sanitizedStatusError is the top of a sanitized chain whose original carries
// a gRPC status: GRPCStatus has the original code and the sanitized text
// (status.FromError on the original also uses the whole error text) and no
// details (they are server data).
type sanitizedStatusError struct {
	error
	st *status.Status
}

func (e *sanitizedStatusError) Unwrap() error { return e.error }

// GRPCStatus implements the interface status.FromError looks for.
func (e *sanitizedStatusError) GRPCStatus() *status.Status { return e.st }

func isSanitized(err error) bool {
	switch err.(type) { //nolint:errorlint // only these exact values are already sanitized
	case *sanitizedError, *sanitizedJoin, *sanitizedStatusError:
		return true
	}
	return false
}

// sanitizeSDKError returns err as a sanitized chain — response bodies
// dropped, the configured OTLP header values masked at every level — or err
// itself when no level had to change and it carries no gRPC status details.
func sanitizeSDKError(err error) error {
	if err == nil || isSanitized(err) {
		return err
	}
	w := &chainWalk{m: currentMask(), seen: map[error]walked{}, budget: maxChainLevels}
	san, changed := w.level(err)
	st := w.st // found by the bounded walk: errors.As would recurse forever on a cyclic chain
	// Status details are server data: an error carrying them is always
	// wrapped, so they never reach an application handler (A29 final round).
	if !changed && (st == nil || len(st.Proto().GetDetails()) == 0) {
		return err
	}
	if st != nil {
		return &sanitizedStatusError{error: san, st: status.New(st.Code(), san.Error())}
	}
	return san
}

// maxChainLevels bounds the levels one sanitization rebuilds.
const maxChainLevels = 256

type walked struct {
	err     error
	changed bool
}

// chainWalk rebuilds a chain once per distinct error (A29 final round, dev
// R2-L5): pointer errors already seen are reused, so a chain that shares or
// references itself is walked linearly and always ends; a level budget
// bounds the rest.
type chainWalk struct {
	m      *masker
	seen   map[error]walked
	budget int
	st     *status.Status // the first gRPC status of the chain, pre-order (as errors.As)
}

// level rebuilds err's chain with sanitized texts (both Unwrap forms) and
// reports whether any text changed.
func (w *chainWalk) level(err error) (error, bool) {
	ptr := reflect.TypeOf(err).Kind() == reflect.Pointer
	if ptr {
		if r, ok := w.seen[err]; ok {
			return r.err, r.changed
		}
	}
	w.budget--
	if gs, ok := err.(interface{ GRPCStatus() *status.Status }); ok && w.st == nil { //nolint:errorlint // the walk visits every level
		w.st = gs.GRPCStatus()
	}
	text := err.Error()
	newText := maskWith(w.m, dropResponseBodies(err, text))
	changed := newText != text
	leaf := &sanitizedError{msg: newText, orig: err}
	if ptr {
		w.seen[err] = walked{leaf, changed} // provisional: a cycle back to err ends here
	}
	out := error(leaf)
	if w.budget > 0 {
		switch u := err.(type) { //nolint:errorlint // walking the chain by hand, both Unwrap forms
		case interface{ Unwrap() []error }:
			kids := make([]error, 0, len(u.Unwrap()))
			for _, k := range u.Unwrap() {
				if k == nil {
					continue
				}
				sk, c := w.level(k)
				changed = changed || c
				kids = append(kids, sk)
			}
			out = &sanitizedJoin{msg: newText, orig: err, nexts: kids}
		case interface{ Unwrap() error }:
			if next := u.Unwrap(); next != nil {
				sn, c := w.level(next)
				changed = changed || c
				leaf.next = sn
			}
		}
	}
	if ptr {
		w.seen[err] = walked{out, changed}
	}
	return out, changed
}

// dropResponseBodies replaces, in text (err's rendering), the text of every
// "body: …" leaf error of err's chain by its size. Then any "body: " left
// that is not already an omitted body (a wrapper flattened the chain into
// text) is replaced up to the end of the text.
func dropResponseBodies(err error, text string) string {
	var bodies []string
	collectBodies(err, &bodies)
	for _, b := range bodies {
		text = strings.ReplaceAll(text, b, omittedBody(b[len(bodyPrefix):]))
	}
	for i := 0; ; {
		j := strings.Index(text[i:], bodyPrefix)
		if j < 0 {
			return text
		}
		at := i + j + len(bodyPrefix)
		if !strings.HasPrefix(text[at:], omittedPrefix) {
			return text[:at-len(bodyPrefix)] + omittedBody(text[at:])
		}
		i = at
	}
}

const omittedPrefix = "<response body omitted: "

// omittedBody is the placeholder for a body: its size in bytes. The
// exporters write "(empty)" for an empty body: 0 bytes.
func omittedBody(body string) string {
	n := len(body)
	if body == "(empty)" {
		n = 0
	}
	return bodyPrefix + omittedPrefix + strconv.Itoa(n) + " bytes>"
}

// collectBodies appends the text of the leaf errors of err's chain that start
// with "body: " (both Unwrap forms; each pointer error visited once, at most
// maxChainLevels errors).
func collectBodies(err error, out *[]string) {
	seen := map[error]bool{}
	budget := maxChainLevels
	var walk func(e error)
	walk = func(e error) {
		if e == nil || budget <= 0 {
			return
		}
		if reflect.TypeOf(e).Kind() == reflect.Pointer {
			if seen[e] {
				return
			}
			seen[e] = true
		}
		budget--
		switch u := e.(type) { //nolint:errorlint // walking the chain by hand, both Unwrap forms
		case interface{ Unwrap() []error }:
			for _, k := range u.Unwrap() {
				walk(k)
			}
			return
		case interface{ Unwrap() error }:
			if next := u.Unwrap(); next != nil {
				walk(next)
				return
			}
		}
		if s := e.Error(); strings.HasPrefix(s, bodyPrefix) && len(s) > len(bodyPrefix) && !strings.HasPrefix(s[len(bodyPrefix):], omittedPrefix) {
			*out = append(*out, s)
		}
	}
	walk(err)
}

// maskForms is every masked form of the configured values (design D1 row
// "SDK error text", A29 fix round), longest first and deduplicated: for each
// value and each of its whitespace-separated parts (the credential of
// "Bearer <token>") of at least minMaskedValue characters — the text, JSON-
// escaped once and twice, JSON-escaped with HTML escaping (\u0026, \u003c,
// \u003e) and that once more JSON-escaped, form-encoded (url.QueryEscape: space as "+") and, Go's own quote
// form, strconv.Quote.
func maskForms(values []string) []string {
	seen := map[string]bool{}
	var forms []string
	add := func(f string) {
		if f != "" && !seen[f] {
			seen[f] = true
			forms = append(forms, f)
		}
	}
	for _, v := range values {
		parts := strings.Fields(v)
		if len(parts) > 1 && isAuthScheme(parts[0]) {
			parts = parts[1:] // "Bearer" alone is not a secret (A29 final round)
		}
		bases := append([]string{v}, parts...)
		for _, b := range bases {
			if utf8.RuneCountInString(b) < minMaskedValue {
				continue
			}
			once := jsonEscape(b, false)
			add(b)
			add(once)
			add(jsonEscape(once, false))
			html := jsonEscape(b, true)
			add(html)
			add(jsonEscape(html, false)) // and JSON-escaped again (a JSON-rendered error, Node's diag)
			add(url.QueryEscape(b))
			add(quoted(b))
		}
	}
	sort.SliceStable(forms, func(i, j int) bool {
		if len(forms[i]) != len(forms[j]) {
			return len(forms[i]) > len(forms[j])
		}
		return forms[i] < forms[j]
	})
	return forms
}

// isAuthScheme reports whether a first part is an HTTP authentication scheme
// name (ASCII case-insensitive): it is not masked on its own.
func isAuthScheme(part string) bool {
	switch asciiLower(part) {
	case "bearer", "basic", "digest", "negotiate":
		return true
	}
	return false
}

// masker holds the forms of a set of values, longest first.
type masker struct{ forms []string }

func newMasker(values []string) *masker {
	forms := maskForms(values)
	if len(forms) == 0 {
		return nil
	}
	return &masker{forms: forms}
}

// maskWith marks every occurrence of every form (overlapping ones included)
// and replaces each run of marked bytes by one "<redacted>" (A29 final round,
// dev R2-L1): two configured values that overlap in the text never leave a
// tail of one of them, and "<redacted>" is never re-scanned. Adjacent masked
// values collapse into one "<redacted>".
func maskWith(m *masker, text string) string {
	if m == nil || text == "" {
		return text
	}
	var marked []bool
	for _, f := range m.forms {
		for i := 0; i < len(text); {
			j := strings.Index(text[i:], f)
			if j < 0 {
				break
			}
			if marked == nil {
				marked = make([]bool, len(text))
			}
			start := i + j
			for k := start; k < start+len(f); k++ {
				marked[k] = true
			}
			i = start + 1
		}
	}
	if marked == nil {
		return text
	}
	var b strings.Builder
	for i := 0; i < len(text); {
		if marked[i] {
			b.WriteString(redactedValue)
			for i < len(text) && marked[i] {
				i++
			}
			continue
		}
		b.WriteByte(text[i])
		i++
	}
	return b.String()
}

// maskHeaderValues masks every form of values in text (see maskForms).
func maskHeaderValues(text string, values []string) string {
	return maskWith(newMasker(values), text)
}

// jsonEscape is s as JSON escapes it inside a string literal (no quotes).
func jsonEscape(s string, escapeHTML bool) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(escapeHTML)
	_ = enc.Encode(s) // a string always encodes
	out := strings.TrimSuffix(buf.String(), "\n")
	return out[1 : len(out)-1]
}

func quoted(s string) string {
	q := strconv.Quote(s)
	return q[1 : len(q)-1]
}

var headerVariables = []string{
	otlpEnv("", "HEADERS"),
	otlpEnv(signalTraces, "HEADERS"),
	otlpEnv(signalMetrics, "HEADERS"),
	otlpEnv(signalLogs, "HEADERS"),
}

var headerValuesCache struct {
	mu       sync.Mutex
	raws     []string
	captured []string // the values the exporters were built with (captureHeaderValues)
	stale    bool     // captured changed: rebuild
	values   []string
	mask     *masker
}

func readHeaderRaws() []string {
	raws := make([]string, len(headerVariables))
	for i, v := range headerVariables {
		raws[i] = os.Getenv(v)
	}
	return raws
}

// headerValuesOf is the raw (after the first "=", trimmed) and decoded values
// of the header variables' raw texts.
func headerValuesOf(raws []string) []string {
	seen := map[string]bool{}
	var values []string
	add := func(v string) {
		if v != "" && !seen[v] {
			seen[v] = true
			values = append(values, v)
		}
	}
	for _, raw := range raws {
		if trimWS(raw) == "" {
			continue
		}
		headers, _, _ := parseHeaders(raw)
		for _, v := range headers {
			add(v)
		}
		for _, item := range strings.Split(raw, ",") {
			if _, v, ok := strings.Cut(item, "="); ok {
				add(trimWS(v))
			}
		}
	}
	return values
}

// captureHeaderValues records the header values Setup builds the exporters
// with: they stay masked after the application changes or deletes the
// variables (A29 final round, dev R2-M1), because the exporters keep sending
// them.
func captureHeaderValues() {
	values := headerValuesOf(readHeaderRaws())
	c := &headerValuesCache
	c.mu.Lock()
	defer c.mu.Unlock()
	c.captured, c.stale = values, true
}

// configuredHeaderValues returns the union of the values captured at Setup
// and the values of OTEL_EXPORTER_OTLP_HEADERS and the per-signal variables
// now, raw and decoded; rebuilt when either changes. Never logged.
func configuredHeaderValues() []string {
	raws := readHeaderRaws()
	c := &headerValuesCache
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.stale && c.raws != nil && equalStrings(c.raws, raws) {
		return c.values
	}
	values := append([]string(nil), c.captured...)
	seen := map[string]bool{}
	for _, v := range values {
		seen[v] = true
	}
	for _, v := range headerValuesOf(raws) {
		if !seen[v] {
			seen[v] = true
			values = append(values, v)
		}
	}
	c.raws, c.stale, c.values, c.mask = raws, false, values, newMasker(values)
	return values
}

// currentMask is the masker of the configured header values (nil = none).
func currentMask() *masker {
	configuredHeaderValues()
	c := &headerValuesCache
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.mask
}

// sdkErrorHandler is the global OTel error handler while a pipeline is
// installed. It sanitizes every error, then forwards it to the application
// handler that was global when Setup ran (next), or — when that was the SDK's
// default delegator, which would route it back here — writes it on the
// helper's stderr logger.
type sdkErrorHandler struct{ next otel.ErrorHandler }

// Handle implements otel.ErrorHandler.
func (h *sdkErrorHandler) Handle(err error) {
	if err == nil {
		return
	}
	err = sanitizeSDKError(err)
	if h.next != nil {
		h.next.Handle(err)
		return
	}
	reportSDKError(err)
}

// reportSDKError writes an SDK error (already sanitized) on the helper's
// stderr logger: never log.Print, never slog.Default().
func reportSDKError(err error) {
	diagLogger.Error("otelhelper: OpenTelemetry SDK error", "error", err.Error())
}

// Install and restore are check-then-set, not atomic: an application
// otel.SetErrorHandler running concurrently between the read and the write
// can be overwritten (the accepted window of quietResourceEnvEcho). The
// first SetErrorHandler of a process makes the SDK's default delegator
// forward to that handler for good (otel internal/errorhandler
// delegateErrorHandlerOnce), so after Shutdown "puts back the default" the
// errors still reach the helper's handler — still sanitized.
//
// installSDKErrorHandler makes the helper's handler global and returns the
// function that puts back the handler it replaced — only while the helper's
// handler is still the global one (an application handler set meanwhile is
// kept). An application handler set later replaces the helper's
// (otel.SetErrorHandler is last-writer-wins) and receives raw errors.
func installSDKErrorHandler() (restore func()) {
	prev := otel.GetErrorHandler()
	h := &sdkErrorHandler{}
	if !forwardsBackToFilter(prev) {
		h.next = prev
	}
	otel.SetErrorHandler(h)
	var once sync.Once
	return func() {
		once.Do(func() {
			if cur, ok := otel.GetErrorHandler().(*sdkErrorHandler); ok && cur == h {
				otel.SetErrorHandler(prev)
			}
		})
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
