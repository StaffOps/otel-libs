package otelhelper

import (
	"context"
	"crypto/tls"
	"fmt"
	"log/slog"

	"go.opentelemetry.io/contrib/bridges/otelslog"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	"go.opentelemetry.io/otel/log/global"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	"go.opentelemetry.io/otel/sdk/resource"
	"google.golang.org/grpc/credentials"
)

func configureLogging(ctx context.Context, res *resource.Resource, opts *Options) (*sdklog.LoggerProvider, error) {
	lpOpts := []sdklog.LoggerProviderOption{
		sdklog.WithResource(res),
	}

	if opts.OtelEndpoint != "" {
		// OTLP push — export logs to collector, over gRPC or HTTP/protobuf
		// depending on the resolved protocol.
		var exporter sdklog.Exporter
		var err error
		if opts.resolvedOtlpProtocol() == ProtocolHTTP {
			httpOpts := []otlploghttp.Option{
				otlploghttp.WithEndpoint(opts.OtelEndpoint),
			}
			if opts.Insecure {
				httpOpts = append(httpOpts, otlploghttp.WithInsecure())
			} else {
				httpOpts = append(httpOpts, otlploghttp.WithTLSClientConfig(&tls.Config{}))
			}
			exporter, err = otlploghttp.New(ctx, httpOpts...)
		} else {
			grpcOpts := []otlploggrpc.Option{
				otlploggrpc.WithEndpoint(opts.OtelEndpoint),
			}
			if opts.Insecure {
				grpcOpts = append(grpcOpts, otlploggrpc.WithInsecure())
			} else {
				grpcOpts = append(grpcOpts, otlploggrpc.WithTLSCredentials(credentials.NewTLS(&tls.Config{})))
			}
			exporter, err = otlploggrpc.New(ctx, grpcOpts...)
		}
		if err != nil {
			return nil, fmt.Errorf("log exporter: %w", err)
		}
		lpOpts = append(lpOpts, sdklog.WithProcessor(sdklog.NewBatchProcessor(exporter)))
	}
	// When OtelEndpoint is empty, LoggerProvider is created without a processor.
	// Logs go to stdout/slog only (no OTLP export).

	lp := sdklog.NewLoggerProvider(lpOpts...)
	global.SetLoggerProvider(lp)
	return lp, nil
}

// NewSlogHandler returns an slog.Handler that bridges to OTel logs.
// Logs emitted within a span context automatically include trace_id and span_id.
func NewSlogHandler() slog.Handler {
	return otelslog.NewHandler("otel-helper")
}

// DefaultLogLevel returns the appropriate slog.Level for a given environment.
// LOCAL=Debug, DEV/HML=Info, PRD=Warning.
func DefaultLogLevel(env DeploymentEnvironment, debug bool) slog.Level {
	if debug {
		return slog.LevelDebug
	}
	switch env {
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

// NewLogger returns a configured *slog.Logger with OTel bridge and environment-appropriate level.
func NewLogger(env DeploymentEnvironment, debug bool) *slog.Logger {
	level := DefaultLogLevel(env, debug)
	handler := NewSlogHandler()
	return slog.New(levelFilterHandler{level: level, inner: handler})
}

// levelFilterHandler wraps an slog.Handler with a minimum level filter.
type levelFilterHandler struct {
	level slog.Level
	inner slog.Handler
}

func (h levelFilterHandler) Enabled(_ context.Context, level slog.Level) bool {
	return level >= h.level
}

func (h levelFilterHandler) Handle(ctx context.Context, r slog.Record) error {
	return h.inner.Handle(ctx, r)
}

func (h levelFilterHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	return levelFilterHandler{level: h.level, inner: h.inner.WithAttrs(attrs)}
}

func (h levelFilterHandler) WithGroup(name string) slog.Handler {
	return levelFilterHandler{level: h.level, inner: h.inner.WithGroup(name)}
}
