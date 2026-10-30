package otelhelper

// Contract rulings of task A18 (design D1 rows dated 2026-10-29, from the
// Node B4b review): TLS files, export deadline, value-length limits.

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io/fs"
	"os"
	"strconv"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// Export-timeout variables of the SDK batch processors and periodic reader
// that the helper does not honor: the resolved OTLP timeout is the single
// export deadline of a signal (D1 ruling "Export deadline").
var ignoredTimeoutVariables = []string{"OTEL_BSP_EXPORT_TIMEOUT", "OTEL_BLRP_EXPORT_TIMEOUT", "OTEL_METRIC_EXPORT_TIMEOUT"}

// maxExportTimeoutMs bounds a code export timeout (24 h, as the env clamp).
const maxExportTimeoutMs = 86_400_000

const msgTimeoutNotHonored = "not honored: the OTLP export timeout is the export deadline of every signal; set OTEL_EXPORTER_OTLP_TIMEOUT or OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT"

// warnIgnoredTimeouts warns about every ignored export-timeout variable that is set.
func (o *Options) warnIgnoredTimeouts() {
	for _, v := range ignoredTimeoutVariables {
		if trimWS(os.Getenv(v)) != "" {
			o.warnOnce(endpointWarning{msg: msgTimeoutNotHonored, variable: v})
		}
	}
}

// errnoName is a portable, path-free name for a file read error (R8: the
// error text holds the path).
func errnoName(err error) string {
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return "ENOENT"
	case errors.Is(err, fs.ErrPermission):
		return "EACCES"
	default:
		return "read error"
	}
}

// tlsFile returns the first readable (and, for the CA, parseable) file among
// the per-signal and generic variables of suffix, warning — by variable name
// only, never the path — for every file skipped. "" when none.
func (o *Options) tlsFile(signal, suffix string, isCA bool) (variable string, content []byte) {
	var candidates []string
	for _, key := range []string{otlpEnv(signal, suffix), otlpEnv("", suffix)} {
		if trimWS(os.Getenv(key)) != "" {
			candidates = append(candidates, key)
		}
	}
	for i, key := range candidates {
		next := "the generic variable"
		if i == len(candidates)-1 {
			next = "none"
			if isCA {
				next = "the system roots"
			}
		}
		b, err := os.ReadFile(trimWS(os.Getenv(key)))
		if err != nil {
			o.warnOnce(endpointWarning{msg: "TLS file cannot be read; ignored", variable: key, value: errnoName(err), using: next})
			continue
		}
		if isCA && !x509.NewCertPool().AppendCertsFromPEM(b) {
			o.warnOnce(endpointWarning{msg: "TLS CA file holds no PEM certificate; ignored", variable: key, value: "<file content not shown>", using: next})
			continue
		}
		return key, b
	}
	return "", nil
}

// checkTLSFiles resolves the TLS files of a TLS endpoint (D1 ruling "TLS
// files"): the CA and the client key/certificate, each per-signal then
// generic, a file that cannot be read or parsed warned and skipped. The key
// and the certificate are one pair; when only one resolves or crypto/tls
// rejects them, the helper warns and the signal uses server authentication
// with the resolved CA. It returns the tls.Config the exporter gets
// explicitly (nil when no TLS file variable is set: the SDK default, system
// roots), so the Go exporters' own env handling — per-prefix pairing, and
// the log exporter dropping the whole TLS config on one bad file — no longer
// decides. The SDK still reads the files at New() and prints their paths in
// its own errors (documented Go limitation, D1; G7 forbids masking).
func (o *Options) checkTLSFiles(signal string) *tls.Config {
	set := false
	for _, s := range []string{"CERTIFICATE", "CLIENT_KEY", "CLIENT_CERTIFICATE"} {
		for _, key := range []string{otlpEnv(signal, s), otlpEnv("", s)} {
			if trimWS(os.Getenv(key)) != "" {
				set = true
			}
		}
	}
	if !set {
		return nil
	}
	cfg := &tls.Config{MinVersion: tls.VersionTLS12}
	if _, ca := o.tlsFile(signal, "CERTIFICATE", true); ca != nil {
		pool := x509.NewCertPool()
		pool.AppendCertsFromPEM(ca)
		cfg.RootCAs = pool
	}
	keyVar, key := o.tlsFile(signal, "CLIENT_KEY", false)
	certVar, cert := o.tlsFile(signal, "CLIENT_CERTIFICATE", false)
	pair := keyVar
	if certVar != "" {
		if pair != "" {
			pair += ","
		}
		pair += certVar
	}
	switch {
	case keyVar == "" && certVar == "":
	case keyVar == "" || certVar == "":
		o.warnOnce(endpointWarning{msg: "client key and client certificate must both be set and readable; the client certificate is not used",
			variable: pair, value: "<file paths not shown>", using: "server authentication"})
	default:
		crt, err := tls.X509KeyPair(cert, key)
		if err != nil {
			o.warnOnce(endpointWarning{msg: "client key and certificate rejected by TLS (bad PEM, mismatch or encrypted key); the client certificate is not used",
				variable: pair, value: "<file paths not shown>", using: "server authentication"})
		} else {
			cfg.Certificates = []tls.Certificate{crt}
		}
	}
	return cfg
}

// Value-length limit variables (D1 ruling "Value-length limits"): 0 is not a
// valid limit. It warns and the next level applies (per-signal → generic →
// no limit).
const (
	envAttrValueLength    = "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT"
	envSpanValueLength    = "OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT"
	envLogRecValueLength  = "OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT"
	msgZeroValueLength    = "a value-length limit of 0 is invalid; the next level or no limit is used"
	noValueLengthLimit    = -1
	valueLengthLimitUnset = -2
)

// envValueLength parses a value-length variable: valueLengthLimitUnset when
// unset or not an integer (the SDK reports those), the value otherwise; a 0
// warns once and reads as unset.
func (o *Options) envValueLength(variable string) int {
	raw := trimWS(os.Getenv(variable))
	if raw == "" {
		return valueLengthLimitUnset
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		return valueLengthLimitUnset
	}
	if n == 0 {
		o.warnOnce(endpointWarning{msg: msgZeroValueLength, variable: variable, value: "0", using: "next level, else no limit"})
		return valueLengthLimitUnset
	}
	return n
}

// spanLimits returns the SDK's env-derived span limits with a 0 value-length
// limit replaced by the next level (generic, else no limit); ok is false when
// no 0 was involved (the SDK's own limits apply unchanged).
func (o *Options) spanLimits() (limits sdktrace.SpanLimits, ok bool) {
	span, generic := o.envValueLength(envSpanValueLength), o.envValueLength(envAttrValueLength)
	limits = sdktrace.NewSpanLimits()
	if limits.AttributeValueLengthLimit != 0 {
		return limits, false
	}
	switch {
	case span != valueLengthLimitUnset:
		limits.AttributeValueLengthLimit = span
	case generic != valueLengthLimitUnset:
		limits.AttributeValueLengthLimit = generic
	default:
		limits.AttributeValueLengthLimit = noValueLengthLimit
	}
	return limits, true
}

// logValueLength returns the log record value-length limit to pass when
// OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT is 0 (any spelling the SDK reads
// as 0, e.g. "00"; the Go log SDK reads only that variable, not the generic
// one); ok is false otherwise.
func (o *Options) logValueLength() (limit int, ok bool) {
	if n, err := strconv.Atoi(trimWS(os.Getenv(envLogRecValueLength))); err != nil || n != 0 {
		return 0, false
	}
	o.envValueLength(envLogRecValueLength) // warns
	if g := o.envValueLength(envAttrValueLength); g != valueLengthLimitUnset {
		return g, true
	}
	return noValueLengthLimit, true
}

// batchSizeOverride implements the D1 ruling "Batch size above queue size"
// (A18): when <prefix>_MAX_EXPORT_BATCH_SIZE is larger than
// <prefix>_MAX_QUEUE_SIZE (defaults 512 / 2048), warn once naming both
// variables and use the queue size. The Go SDK clamped silently (the span
// processor to min(512, queue), the log processor to the queue size).
// Unparseable values are left to the SDK.
func (o *Options) batchSizeOverride(prefix string) (int, bool) {
	read := func(variable string, dflt int) (int, bool) {
		raw := trimWS(os.Getenv(variable))
		if raw == "" {
			return dflt, true
		}
		n, err := strconv.Atoi(raw)
		return n, err == nil
	}
	queueVar, batchVar := prefix+"_MAX_QUEUE_SIZE", prefix+"_MAX_EXPORT_BATCH_SIZE"
	queue, okQ := read(queueVar, 2048)
	batch, okB := read(batchVar, 512)
	if !okQ || !okB || queue <= 0 || batch <= queue {
		return 0, false
	}
	o.warnOnce(endpointWarning{msg: "the export batch size is larger than the queue size; using the queue size",
		variable: batchVar + "," + queueVar, value: strconv.Itoa(batch), using: strconv.Itoa(queue)})
	return queue, true
}
