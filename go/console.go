package otelhelper

// Console exporters for OTEL_{TRACES,METRICS,LOGS}_EXPORTER=console (R8).
//
// The stdout exporter dependencies are pending owner approval: this file is
// the only place that imports them, so rejecting them is this file plus three
// go.mod lines (and "console" leaving the allowed exporter lists).

import (
	"go.opentelemetry.io/otel/exporters/stdout/stdoutlog"
	"go.opentelemetry.io/otel/exporters/stdout/stdoutmetric"
	"go.opentelemetry.io/otel/exporters/stdout/stdouttrace"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

func newConsoleSpanExporter() (sdktrace.SpanExporter, error) { return stdouttrace.New() }

func newConsoleMetricExporter() (sdkmetric.Exporter, error) { return stdoutmetric.New() }

func newConsoleLogExporter() (sdklog.Exporter, error) { return stdoutlog.New() }
