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
// with WithAttrs keep their key (slog renders them), except under a reserved
// key (A23, below).
type switchHandler struct {
	min     slog.Leveler // minimum level on every path (NewLogger: fixed; NewSlogHandler: installedLogLevel); nil = the destination decides
	stdout  slog.Handler // JSON handler with ops already applied
	ops     []handlerOp
	derived *atomic.Pointer[derivedBridge]
	// excInScope: an "exception" key already exists in the scope record
	// attributes land in (WithAttrs since the last WithGroup) — no rename then.
	excInScope bool
	// scopeLogger: the last top-level "logger" attribute added with WithAttrs,
	// held back from stdout so that a record's own "logger" can replace it (A23).
	scopeLogger    slog.Value
	hasScopeLogger bool
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
	return h.stdout.Handle(ctx, h.stdoutRecord(r))
}

// reservedStdoutKeys are the stdout line's own fields. A top-level attribute
// with one of these keys never adds a second field (contract design D1 row
// "Stdout fallback fields", A23; slog's JSON handler would write a duplicate
// key): time, level and msg are dropped; the last logger becomes the line's
// logger field (Python and Node write the logger name there); an error under
// any of them is renamed to exception like any other error (or dropped when
// the record already has one).
var reservedStdoutKeys = map[string]bool{slog.TimeKey: true, slog.LevelKey: true, slog.MessageKey: true, loggerKey: true}

const loggerKey = "logger"

// inGroup reports whether a WithGroup is open: attributes then nest inside the
// group object and cannot collide with the record's own fields.
func (h *switchHandler) inGroup() bool {
	for _, op := range h.ops {
		if op.group != "" {
			return true
		}
	}
	return false
}

// inlineGroup reports whether a is a group with an empty key (slog inlines its
// members), resolving a LogValuer first.
func inlineGroup(a slog.Attr) (slog.Value, bool) {
	if a.Key != "" {
		return slog.Value{}, false
	}
	v := a.Value.Resolve()
	return v, v.Kind() == slog.KindGroup
}

// hasException reports whether attrs hold an "exception" key at their level,
// members of inline groups included (they land at the same level).
func hasException(attrs []slog.Attr) bool {
	for _, a := range attrs {
		if a.Key == "exception" {
			return true
		}
		if v, ok := inlineGroup(a); ok && hasException(v.Group()) {
			return true
		}
	}
	return false
}

// errorOf returns the error a value holds, resolving it once.
func errorOf(v slog.Value) (error, bool) {
	v = v.Resolve()
	if v.Kind() != slog.KindAny {
		return nil, false
	}
	err, ok := v.Any().(error)
	return err, ok
}

// filterReserved applies the reserved-key rule to attributes that land at the
// top level (WithAttrs before any WithGroup, members of inline groups): time,
// level and msg are dropped, a non-error logger is taken out and returned (the
// last one). An error under a reserved key is renamed to exception while
// *rename is true (then set to false), else dropped; rename nil = drop. out is
// attrs itself, and changed false, when nothing was removed; it is allocated
// only on the first change.
func filterReserved(attrs []slog.Attr, rename *bool) (out []slog.Attr, changed bool, logger slog.Value, hasLogger bool) {
	for i, a := range attrs {
		keep, drop, modified := a, false, false
		if v, ok := inlineGroup(a); ok {
			inner, innerChanged, lg, innerHas := filterReserved(v.Group(), rename)
			if innerHas {
				logger, hasLogger = lg, true
			}
			if innerChanged {
				modified = true
				if len(inner) == 0 {
					drop = true
				} else {
					keep = slog.Attr{Value: slog.GroupValue(inner...)}
				}
			}
		} else if reservedStdoutKeys[a.Key] {
			modified = true
			err, isErr := errorOf(a.Value)
			switch {
			case isErr && rename != nil && *rename:
				keep, *rename = slog.String("exception", errorText(err)), false
			case isErr:
				drop = true // an exception already exists
			case a.Key == loggerKey:
				logger, hasLogger, drop = a.Value, true, true
			default:
				drop = true
			}
		}
		if modified && out == nil {
			out = append(make([]slog.Attr, 0, len(attrs)), attrs[:i]...)
		}
		if out != nil && !drop {
			out = append(out, keep)
		}
	}
	if out == nil {
		return attrs, false, logger, hasLogger
	}
	return out, true, logger, hasLogger
}

// stdoutRecord prepares r for the stdout line (A23): the first top-level
// error-valued attribute becomes exception (its Error() text) unless an
// exception key exists in the record, its inline groups or the scope; at the
// top level, the reserved-key rule (time, level, msg dropped; an error under a
// reserved key renamed to exception, or dropped when one exists; the last
// non-error logger — the record's, else the scope's — written once, before the
// record's own attributes). r itself is returned when nothing changes, so the
// common case allocates nothing. Empty-key attributes are resolved in each
// pass that inspects them (not cached: a LogValuer is cheap to resolve again
// and caching would need a per-record buffer).
func (h *switchHandler) stdoutRecord(r slog.Record) slog.Record {
	top := !h.inGroup()
	firstErr, hasExcKey, needTop := -1, false, false
	logger, hasLogger := h.scopeLogger, top && h.hasScopeLogger
	i := 0
	r.Attrs(func(a slog.Attr) bool {
		switch {
		case a.Key == "exception":
			hasExcKey = true
		case a.Key == "":
			if v, ok := inlineGroup(a); ok && hasException(v.Group()) {
				hasExcKey = true
			}
			needTop = needTop || top
		case top && reservedStdoutKeys[a.Key]:
			needTop = true
		}
		if firstErr < 0 && isError(a.Value) {
			firstErr = i
		}
		i++
		return true
	})
	if hasExcKey || h.excInScope {
		firstErr = -1
	}
	if firstErr < 0 && !needTop && !hasLogger {
		return r
	}
	// An error under a reserved key inside an inline group is renamed only
	// when nothing else becomes the exception.
	rename := firstErr < 0 && !hasExcKey && !h.excInScope
	if top {
		// The logger field is written before the record's own attributes, so
		// the last logger — inline groups included — is found before the copy.
		i = 0
		r.Attrs(func(a slog.Attr) bool {
			switch {
			case i == firstErr:
			case a.Key == loggerKey:
				if !isError(a.Value) {
					logger, hasLogger = a.Value, true
				}
			case a.Key == "":
				if _, _, lg, ok := filterReserved([]slog.Attr{a}, nil); ok {
					logger, hasLogger = lg, true
				}
			}
			i++
			return true
		})
	}
	out := slog.NewRecord(r.Time, r.Level, r.Message, r.PC)
	if hasLogger {
		out.AddAttrs(slog.Attr{Key: loggerKey, Value: logger})
	}
	i = 0
	r.Attrs(func(a slog.Attr) bool {
		idx := i
		i++
		switch {
		case idx == firstErr:
			if err, ok := errorOf(a.Value); ok {
				out.AddAttrs(slog.String("exception", errorText(err)))
			}
		case !top:
			out.AddAttrs(a)
		case reservedStdoutKeys[a.Key]:
			// dropped; a logger was written above
		case a.Key == "":
			kept, _, _, _ := filterReserved([]slog.Attr{a}, &rename)
			out.AddAttrs(kept...)
		default:
			out.AddAttrs(a)
		}
		return true
	})
	return out
}

func isError(v slog.Value) bool {
	v = v.Resolve()
	if v.Kind() != slog.KindAny {
		return false
	}
	_, ok := v.Any().(error)
	return ok
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

// with returns a handler with op appended (replayed on the bridge) and stdout
// as the stdout destination; stdoutAttrs are the attributes stdout received.
func (h *switchHandler) with(op handlerOp, stdout slog.Handler, stdoutAttrs []slog.Attr) *switchHandler {
	ops := make([]handlerOp, len(h.ops), len(h.ops)+1)
	copy(ops, h.ops)
	exc := h.excInScope
	if op.group != "" {
		exc = false
	}
	exc = exc || hasException(stdoutAttrs)
	n := &switchHandler{min: h.min, stdout: stdout, ops: append(ops, op), derived: new(atomic.Pointer[derivedBridge]), excInScope: exc}
	if op.group == "" {
		n.scopeLogger, n.hasScopeLogger = h.scopeLogger, h.hasScopeLogger
	}
	return n
}

func (h *switchHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	if len(attrs) == 0 {
		return h
	}
	if h.inGroup() {
		return h.with(handlerOp{attrs: attrs}, h.stdout.WithAttrs(attrs), attrs)
	}
	// Top level: an error under a reserved key becomes exception when the
	// scope has none yet (A23, G4); the rest of the reserved rule as for records.
	rename := !h.excInScope && !hasException(attrs)
	stdoutAttrs, _, logger, hasLogger := filterReserved(attrs, &rename)
	n := h.with(handlerOp{attrs: attrs}, h.stdout.WithAttrs(stdoutAttrs), stdoutAttrs)
	if hasLogger {
		n.scopeLogger, n.hasScopeLogger = logger, true
	}
	return n
}

func (h *switchHandler) WithGroup(name string) slog.Handler {
	if name == "" {
		return h
	}
	stdout := h.stdout
	if h.hasScopeLogger && !h.inGroup() {
		// The held-back scope logger is written before the group opens: inside
		// the group, a record's logger nests and cannot replace it.
		stdout = stdout.WithAttrs([]slog.Attr{{Key: loggerKey, Value: h.scopeLogger}})
	}
	return h.with(handlerOp{group: name}, stdout.WithGroup(name), nil)
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
