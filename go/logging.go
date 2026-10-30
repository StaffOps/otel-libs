package otelhelper

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"reflect"
	"sync/atomic"

	"go.opentelemetry.io/contrib/bridges/otelslog"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	"go.opentelemetry.io/otel/sdk/resource"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
)

// otlpLogExporter and consoleLogExporter build the log exporters; variables
// so tests can inject a build failure or a recorder.
var (
	otlpLogExporter    = newOTLPLogExporter
	consoleLogExporter = newConsoleLogExporter
)

// configureLogging builds the LoggerProvider without installing it globally
// (Setup does, after every signal was built — G1). hasProcessor reports
// whether an export processor is attached; Setup publishes the bridge.
func configureLogging(ctx context.Context, res *resource.Resource, opts *Options) (lp *sdklog.LoggerProvider, hasProcessor bool, err error) {
	lpOpts := []sdklog.LoggerProviderOption{
		sdklog.WithResource(res),
	}
	if limit, ok := opts.logValueLength(); ok {
		lpOpts = append(lpOpts, sdklog.WithAttributeValueLengthLimit(limit))
	}

	var processors []sdklog.Processor
	for _, name := range opts.signalExporters(signalLogs) {
		var exporter sdklog.Exporter
		var bpOpts []sdklog.BatchProcessorOption
		var err error
		switch name {
		case "otlp":
			e := opts.export(signalLogs)
			if !e.configured {
				continue
			}
			exporter, err = otlpLogExporter(ctx, e)
			// The export deadline (A18): OTEL_BLRP_EXPORT_TIMEOUT is not honored.
			bpOpts = append(bpOpts, sdklog.WithExportTimeout(e.timeout))
		case "console":
			exporter, err = consoleLogExporter()
		}
		if err != nil {
			// A batch processor runs its own goroutine from creation on.
			for i := len(processors) - 1; i >= 0; i-- {
				releaseOnFailure(ctx, "log processor", processors[i].Shutdown)
			}
			return nil, false, fmt.Errorf("log exporter: %w", err)
		}
		if n, ok := opts.batchSizeOverride("OTEL_BLRP"); ok {
			bpOpts = append(bpOpts, sdklog.WithExportMaxBatchSize(n))
		}
		p := sdklog.NewBatchProcessor(exporter, bpOpts...)
		processors = append(processors, p)
		lpOpts = append(lpOpts, sdklog.WithProcessor(p))
	}

	return sdklog.NewLoggerProvider(lpOpts...), len(processors) > 0, nil
}

// logBridge is the OTel slog bridge of one installed pipeline, bound to that
// pipeline's LoggerProvider (not the global one: the global delegate forwards
// only to the first provider ever installed, so a bridge built from it would
// keep writing to a shut-down provider after Shutdown → Setup — design D13).
type logBridge struct{ handler slog.Handler }

// activeLogBridge is non-nil while the installed pipeline has a log export
// processor (OTLP or console). Setup stores it once every signal is built;
// Shutdown clears it before flushing. The helper's slog handlers read it per
// record (design D6), so loggers built before Setup switch to the bridge and
// loggers outliving Shutdown go back to stdout instead of a dead provider.
var activeLogBridge atomic.Pointer[logBridge]

// newLogBridge returns the bridge for a pipeline, or nil when the provider
// has no processor: a processor-less provider silently drops every record.
func newLogBridge(lp *sdklog.LoggerProvider, hasProcessor bool) *logBridge {
	if lp == nil || !hasProcessor {
		return nil
	}
	return &logBridge{handler: otelslog.NewHandler("otel-helper", otelslog.WithLoggerProvider(lp))}
}

// logsBridged reports whether records currently go through the OTel bridge.
func logsBridged() bool { return activeLogBridge.Load() != nil }

// handlerOp is one WithGroup (group != "") or WithAttrs call, replayed on the
// bridge of each pipeline the handler meets.
type handlerOp struct {
	group string
	attrs []slog.Attr
}

// derivedBridge caches a pipeline bridge with a handler's ops applied.
type derivedBridge struct {
	base    *logBridge
	handler slog.Handler
}

// installedLogLevel is DefaultLogLevel of the environment the last successful
// Setup resolved (Info until then); it is the level of NewSlogHandler on both
// the bridge and the stdout path (R4, design D6 "A5" and "A13" rulings). Kept
// after Shutdown.
var installedLogLevel slog.LevelVar

// switchHandler is the helper's slog.Handler (G3, design D6): per record it
// writes through the active OTel bridge, or — with no logs exporter, before
// Setup, after Shutdown — as single-line JSON to stdout: time, level
// (DEBUG/INFO/WARN/ERROR), msg, the attributes, and exception for the first
// error-valued attribute of the record (the shape the Python helper mirrors).
// Only attributes passed with the record are renamed; error attributes added
// with WithAttrs keep their key (slog renders them).
type switchHandler struct {
	min     slog.Leveler // minimum level on every path (NewLogger: fixed; NewSlogHandler: installedLogLevel); nil = the destination decides
	stdout  slog.Handler // JSON handler with ops already applied
	ops     []handlerOp
	derived *atomic.Pointer[derivedBridge]
	// excInScope: an "exception" key already exists in the scope record
	// attributes land in (WithAttrs since the last WithGroup) — no rename then.
	excInScope bool
}

func newSwitchHandler(min slog.Leveler, stdout slog.Handler) *switchHandler {
	return &switchHandler{min: min, stdout: stdout, derived: new(atomic.Pointer[derivedBridge])}
}

// newStdoutJSONHandler is the fallback destination: the application's own log
// output, not helper diagnostics. os.Stdout is read at construction.
func newStdoutJSONHandler(level slog.Leveler) slog.Handler {
	return slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level, ReplaceAttr: stdoutTime})
}

// stdoutTimeFormat is RFC 3339 with millisecond precision; times are UTC, so
// the zone is always "Z" (same as the Python helper).
const stdoutTimeFormat = "2006-01-02T15:04:05.000Z07:00"

// stdoutTime renders the record time in UTC (slog's default keeps local time).
func stdoutTime(groups []string, a slog.Attr) slog.Attr {
	if len(groups) == 0 && a.Key == slog.TimeKey && a.Value.Kind() == slog.KindTime {
		return slog.String(slog.TimeKey, a.Value.Time().UTC().Format(stdoutTimeFormat))
	}
	return a
}

// bridge returns the active bridge with this handler's ops applied, or nil.
func (h *switchHandler) bridge() slog.Handler {
	b := activeLogBridge.Load()
	if b == nil {
		if h.derived.Load() != nil {
			h.derived.Store(nil) // don't keep a shut-down provider reachable
		}
		return nil
	}
	if d := h.derived.Load(); d != nil && d.base == b {
		return d.handler
	}
	bh := b.handler
	for _, op := range h.ops {
		if op.group != "" {
			bh = bh.WithGroup(op.group)
		} else {
			bh = bh.WithAttrs(op.attrs)
		}
	}
	h.derived.Store(&derivedBridge{base: b, handler: bh})
	return bh
}

func (h *switchHandler) Enabled(ctx context.Context, level slog.Level) bool {
	if h.min != nil && level < h.min.Level() {
		return false
	}
	if b := h.bridge(); b != nil {
		return b.Enabled(ctx, level)
	}
	return h.stdout.Enabled(ctx, level)
}

func (h *switchHandler) Handle(ctx context.Context, r slog.Record) error {
	// Handle can be called without Enabled (handler wrappers do), and the
	// destination may have changed since Enabled: re-check the level here.
	if h.min != nil && r.Level < h.min.Level() {
		return nil
	}
	if b := h.bridge(); b != nil {
		return b.Handle(ctx, r)
	}
	if !h.stdout.Enabled(ctx, r.Level) {
		return nil
	}
	if h.excInScope {
		return h.stdout.Handle(ctx, r)
	}
	return h.stdout.Handle(ctx, withException(r))
}

func isError(v slog.Value) bool {
	v = v.Resolve()
	if v.Kind() != slog.KindAny {
		return false
	}
	_, ok := v.Any().(error)
	return ok
}

// withException renames the record's first error-valued attribute to
// "exception" (its Error() text) for the stdout JSON shape.
// A record that already carries an "exception" attribute is left unchanged.
func withException(r slog.Record) slog.Record {
	found := -1
	i := 0
	r.Attrs(func(a slog.Attr) bool {
		if a.Key == "exception" {
			found = -1
			return false
		}
		if found < 0 && isError(a.Value) {
			found = i
		}
		i++
		return true
	})
	if found < 0 {
		return r
	}
	out := slog.NewRecord(r.Time, r.Level, r.Message, r.PC)
	i = 0
	r.Attrs(func(a slog.Attr) bool {
		if i == found {
			a = slog.String("exception", errorText(a.Value.Resolve().Any().(error)))
		}
		out.AddAttrs(a)
		i++
		return true
	})
	return out
}

// errorText is err.Error() rendered the way slog renders an error value: a
// typed-nil pointer gives "<nil>" and a panicking Error() gives "!PANIC: …",
// so a bad error value never crashes the caller.
func errorText(err error) (s string) {
	defer func() {
		if r := recover(); r != nil {
			if v := reflect.ValueOf(err); v.Kind() == reflect.Pointer && v.IsNil() {
				s = "<nil>"
				return
			}
			s = fmt.Sprintf("!PANIC: %v", r)
		}
	}()
	return err.Error()
}

func (h *switchHandler) with(op handlerOp, stdout slog.Handler) *switchHandler {
	ops := make([]handlerOp, len(h.ops), len(h.ops)+1)
	copy(ops, h.ops)
	exc := h.excInScope
	if op.group != "" {
		exc = false
	}
	for _, a := range op.attrs {
		exc = exc || a.Key == "exception"
	}
	return &switchHandler{min: h.min, stdout: stdout, ops: append(ops, op), derived: new(atomic.Pointer[derivedBridge]), excInScope: exc}
}

func (h *switchHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	if len(attrs) == 0 {
		return h
	}
	return h.with(handlerOp{attrs: attrs}, h.stdout.WithAttrs(attrs))
}

func (h *switchHandler) WithGroup(name string) slog.Handler {
	if name == "" {
		return h
	}
	return h.with(handlerOp{group: name}, h.stdout.WithGroup(name))
}

// NewSlogHandler returns the helper's slog.Handler. While Setup has a logs
// exporter installed (OTLP and/or OTEL_LOGS_EXPORTER=console) records go
// through the OTel Logs API bridge — trace-correlated (trace_id/span_id from
// the record's context). Otherwise (before Setup, after Shutdown, no endpoint,
// OTEL_LOGS_EXPORTER=none) records are written to stdout as JSON. Both paths
// drop records below the level of the environment Setup resolved
// (DefaultLogLevel: LOCAL=Debug, DEV/HML/unset=Info, PRD=Warn; Info before the
// first Setup) — R4, design D6. Destination and level are read per record, so
// a handler created before Setup switches once Setup installs the exporter and
// uses the level Setup resolved. Trace correlation needs the context-taking
// slog calls (InfoContext, ...).
func NewSlogHandler() slog.Handler {
	return newSwitchHandler(&installedLogLevel, newStdoutJSONHandler(&installedLogLevel))
}

// DefaultLogLevel returns the appropriate slog.Level for a given environment
// (case-insensitive): LOCAL=Debug, DEV/HML=Info, PRD=Warning; unset or any
// other value = Info (ADR-003).
func DefaultLogLevel(env DeploymentEnvironment, debug bool) slog.Level {
	if debug {
		return slog.LevelDebug
	}
	known, _ := knownEnvironment(string(env))
	switch known {
	case LOCAL:
		return slog.LevelDebug
	case DEV, HML:
		return slog.LevelInfo
	case PRD:
		return slog.LevelWarn
	default:
		return slog.LevelInfo
	}
}

// NewLogger returns a *slog.Logger with the environment's level
// (DefaultLogLevel) over the same per-record handler as NewSlogHandler: the
// OTel bridge while Setup has a logs exporter installed, stdout JSON
// (msg/level/time) otherwise. Records are never handed to a provider without
// a processor, which would silently drop them.
func NewLogger(env DeploymentEnvironment, debug bool) *slog.Logger {
	level := DefaultLogLevel(env, debug)
	return slog.New(newSwitchHandler(level, newStdoutJSONHandler(level)))
}

// newOTLPLogExporter mirrors newOTLPTraceExporter for logs.
func newOTLPLogExporter(ctx context.Context, e *signalExport) (sdklog.Exporter, error) {
	if e.protocol == ProtocolHTTP {
		var o []otlploghttp.Option
		if e.endpointURL != "" {
			o = append(o, otlploghttp.WithEndpointURL(e.endpointURL))
		}
		if e.timeout > 0 {
			o = append(o, otlploghttp.WithTimeout(e.timeout))
		}
		if e.headers != nil {
			o = append(o, otlploghttp.WithHeaders(e.headers))
		}
		if e.tlsConfig != nil {
			o = append(o, otlploghttp.WithTLSClientConfig(e.tlsConfig))
		}
		if e.dropTLS {
			// nil clears the TLS config the SDK loaded from env (options are
			// applied after env); otherwise New() fails on an http:// endpoint.
			o = append(o, otlploghttp.WithTLSClientConfig(nil))
		}
		if e.compression == CompressionNone {
			o = append(o, otlploghttp.WithCompression(otlploghttp.NoCompression))
		} else {
			o = append(o, otlploghttp.WithCompression(otlploghttp.GzipCompression))
		}
		return otlploghttp.New(ctx, o...)
	}
	var o []otlploggrpc.Option
	if e.endpointURL != "" {
		o = append(o, otlploggrpc.WithEndpointURL(e.endpointURL))
	}
	if e.timeout > 0 {
		o = append(o, otlploggrpc.WithTimeout(e.timeout))
	}
	if e.headers != nil {
		o = append(o, otlploggrpc.WithHeaders(e.headers))
	}
	if e.tlsConfig != nil {
		o = append(o, otlploggrpc.WithTLSCredentials(credentials.NewTLS(e.tlsConfig)))
	}
	if e.dropTLS {
		// The SDK prefers env TLS credentials over Insecure: force plaintext.
		o = append(o, otlploggrpc.WithTLSCredentials(insecure.NewCredentials()))
	}
	o = append(o, otlploggrpc.WithCompressor(e.compression)) // accepts gzip and none
	return otlploggrpc.New(ctx, o...)
}
