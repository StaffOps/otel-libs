package demokit

import (
	"context"
	"fmt"
	"log"
	"log/slog"
	"time"

	otelhelper "github.com/staffops/otel-libs/go"
)

// Setup runs the helper's Setup with no code options (every setting comes
// from the environment, DR0) and returns the helper's trace-correlated slog
// logger and a flush function that runs under its own deadline (the caller's
// context may already be cancelled by SIGTERM).
func Setup(ctx context.Context) (*slog.Logger, func(), error) {
	shutdown, err := otelhelper.Setup(ctx)
	if err != nil {
		return nil, nil, fmt.Errorf("otel setup: %w", err)
	}
	logger := slog.New(otelhelper.NewSlogHandler())
	flush := func() {
		flushCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := shutdown(flushCtx); err != nil {
			// The OTel log bridge is shut down by now: report on stderr.
			log.Printf("otel shutdown: %v", err)
		}
	}
	return logger, flush, nil
}
