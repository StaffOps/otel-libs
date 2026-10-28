// Command go-smoke emits one span, one metric and one log record through the
// helper and exits; testing/e2e/smoke.sh checks the Collector received all
// three. The go/example apps can't serve here: they log with the standard
// library (not exported) and their first span fires only after 30 s.
package main

import (
	"context"
	"log"
	"time"

	otelhelper "github.com/staffops/staffops-otel-libs/go"
)

func main() {
	ctx := context.Background()
	shutdown, err := otelhelper.Setup(ctx)
	if err != nil {
		log.Fatalf("otel setup: %v", err)
	}

	ctx, span := otelhelper.GetTracer("e2e-go-smoke").Start(ctx, "e2e-smoke-span")
	runs, err := otelhelper.GetMeter("e2e-go-smoke").Int64Counter("e2e.smoke.runs")
	if err != nil {
		log.Fatalf("counter: %v", err)
	}
	runs.Add(ctx, 1)
	otelhelper.NewLogger(otelhelper.DEV, false).WarnContext(ctx, "e2e-smoke-log")
	span.End()

	flushCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := shutdown(flushCtx); err != nil {
		log.Fatalf("shutdown: %v", err)
	}
	log.Print("go-smoke: emitted span, metric and log")
}
