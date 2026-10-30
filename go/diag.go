package otelhelper

import (
	"log/slog"
	"os"
	"sync"
)

// diagLogger is the single channel for the helper's own diagnostics
// (configuration warnings, deprecations, resolution notes) — design D6.
//
// It writes to stderr on purpose: slog.Default() may have been pointed at the
// OTel bridge by the application, and a config warning about the exporter must
// not depend on that exporter working. otel.Handle stays reserved for errors
// raised by the SDK pipeline at runtime (see diag_channel_test.go for the
// allow-list). It is a package variable so tests can hook it.
var diagLogger = slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

// maxEchoedValue is the number of characters of an env value a warning
// echoes (design D1 row "Echoed values are truncated", A19; Node
// MAX_ECHOED_VALUE).
const maxEchoedValue = 64

// truncateValue cuts value to maxEchoedValue code points plus "…" when longer.
func truncateValue(value string) string {
	n := 0
	for i := range value {
		if n == maxEchoedValue {
			return value[:i] + "…"
		}
		n++
	}
	return value
}

// warnEnv reports an environment value the helper ignored or corrected.
// value must already be safe to print (see redactURL); secrets never reach
// here. It is truncated to maxEchoedValue code points.
func warnEnv(msg, variable, value, using string) {
	diagLogger.Warn("otelhelper: "+msg, "variable", variable, "value", truncateValue(value), "using", using)
}

// deprecationsWarned records which deprecation warnings this process already
// emitted: each is logged once per process (design D8), however many times
// Setup runs. Tests reset it with resetDeprecationsForTest.
var deprecationsWarned sync.Map

// warnDeprecated logs, once per process, that variable is deprecated and names
// its replacement. Only the variable name is printed, never its value.
func warnDeprecated(variable, replacement string) {
	if _, seen := deprecationsWarned.LoadOrStore("deprecated:"+variable, true); seen {
		return
	}
	diagLogger.Warn("otelhelper: "+variable+" is deprecated and will be removed in 1.0; use "+replacement,
		"variable", variable, "replacement", replacement)
}

// warnEnvOncePerProcess is warnEnv deduplicated per process by key (used for
// deprecations that also carry a value, such as a schemeless endpoint).
func warnEnvOncePerProcess(key, msg, variable, value, using string) {
	if _, seen := deprecationsWarned.LoadOrStore("env:"+key, true); seen {
		return
	}
	warnEnv(msg, variable, value, using)
}

// warnOncePerProcess is diagLogger.Warn deduplicated per process by key (the
// identity and OTEL_RESOURCE_ATTRIBUTES warnings, design D1 cadence row).
// args must already be safe to print.
func warnOncePerProcess(key, msg string, args ...any) {
	if _, seen := deprecationsWarned.LoadOrStore("once:"+key, true); seen {
		return
	}
	diagLogger.Warn(msg, args...)
}

// warnUnsupported logs msg once per process for a set variable that is not
// supported, naming the variable only (never its value).
func warnUnsupported(variable, msg string) {
	if _, seen := deprecationsWarned.LoadOrStore("unused:"+variable, true); seen {
		return
	}
	diagLogger.Warn(msg, "variable", variable)
}

// warnUnused logs, once per process, that variable is set but has no effect.
func warnUnused(variable, why string) {
	if _, seen := deprecationsWarned.LoadOrStore("unused:"+variable, true); seen {
		return
	}
	diagLogger.Warn("otelhelper: "+variable+" has no effect in Go and is ignored; "+why,
		"variable", variable)
}
