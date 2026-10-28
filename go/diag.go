package otelhelper

import (
	"log/slog"
	"net/url"
	"os"
	"strings"
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

// warnEnv reports an environment value the helper ignored or corrected.
// value must already be safe to print (see redactURL); secrets never reach here.
func warnEnv(msg, variable, value, using string) {
	diagLogger.Warn("otelhelper: "+msg, "variable", variable, "value", value, "using", using)
}

// redactURL renders an endpoint without userinfo, query or fragment, so helper
// diagnostics never print credentials or tokens embedded in a URL (R8).
func redactURL(raw string) string {
	s := trimWS(raw)
	if strings.IndexFunc(s, isControl) >= 0 {
		return "<unparseable URL>" // never echo a value we can't parse safely
	}
	if !strings.Contains(s, "://") {
		// Schemeless "host:port[/path]": userinfo/query are not expected, but cut
		// anything after '?' / '#' and before '@' defensively.
		if i := strings.LastIndex(s, "@"); i >= 0 {
			s = s[i+1:]
		}
		if i := strings.IndexAny(s, "?#"); i >= 0 {
			s = s[:i]
		}
		return s
	}
	u, err := url.Parse(s)
	if err != nil {
		return "<unparseable URL>"
	}
	u.User = nil
	u.RawQuery = ""
	u.ForceQuery = false
	u.Fragment = ""
	u.RawFragment = ""
	return u.String()
}
