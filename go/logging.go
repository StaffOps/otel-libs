package otelhelper

import (
	"context"
	"crypto/tls"
	"fmt"
	"log/slog"
	"os"

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
	// logsHaveProcessor gates NewLogger's handler choice below: called from
	// Setup(), which already holds mu, so this plain assignment is safe.
	logsHaveProcessor = opts.OtelEndpoint != ""

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

// NewLogger returns a configured *slog.Logger with environment-appropriate level.
//
// When Setup() configured an OTLP endpoint, this bridges slog through the
// OTel Logs API (NewSlogHandler) so records are trace-correlated and exported
// via OTLP. Without an endpoint, configureLogging builds a LoggerProvider
// with NO processor attached (there is nothing to export to) — bridging
// through it in that case would silently discard every record, since a
// processor-less provider drops everything handed to it. So instead this
// falls back to a plain JSON handler on stdout, matching the flat
// msg/level/time shape the OTel bridge's absence would otherwise lose.
func NewLogger(env DeploymentEnvironment, debug bool) *slog.Logger {
	level := DefaultLogLevel(env, debug)

	mu.Lock()
	bridged := logsHaveProcessor
	mu.Unlock()

	var handler slog.Handler
	if bridged {
		handler = NewSlogHandler()
	} else {
		handler = slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level})
		// slog.NewJSONHandler already enforces Level itself; the outer
		// levelFilterHandler below is redundant here but kept for a uniform
		// return type/behavior between both branches.
	}
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
