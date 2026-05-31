// Package otelhelper provides a single-call OpenTelemetry setup for Go services.
//
// Usage:
//
//	shutdown, err := otelhelper.Setup(ctx)
//	if err != nil { log.Fatal(err) }
//	defer shutdown(ctx)
package otelhelper
