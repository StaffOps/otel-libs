package otelsql

import (
	"log/slog"
	"os"
)

// diagLogger is this module's channel for its own diagnostics (the core
// helper's pattern, design D6): stderr, never slog.Default(), which the
// application may have pointed at the OTel bridge. A package variable so
// tests can hook it.
var diagLogger = slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
