using System;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using OpenTelemetry.Exporter;
using OpenTelemetry.Logs;

namespace OtelHelper.Logging
{
    internal static class LoggingSetup
    {
        internal static IServiceCollection ConfigureLogging(
            this IServiceCollection services,
            TelemetryOptions options)
        {
            services.AddLogging(builder =>
            {
                builder.AddOpenTelemetry(logging =>
                {
                    logging.IncludeFormattedMessage = true;
                    logging.IncludeScopes = true;
                    logging.ParseStateValues = true;

                    // OTLP exporter only when collector endpoint is configured.
                    // Without endpoint, logs go to console/stdout only (standard K8s pattern via Fluent Bit).
                    if (!string.IsNullOrWhiteSpace(options.OtelCollectorEndpoint))
                    {
                        var protocol = options.ResolvedOtlpProtocol();
                        logging.AddOtlpExporter(otlp =>
                        {
                            if (protocol == TelemetryOptions.ProtocolHttpProtobuf)
                            {
                                otlp.Protocol = OtlpExportProtocol.HttpProtobuf;
                                otlp.Endpoint = new Uri(options.OtelCollectorEndpoint.TrimEnd('/') + "/v1/logs");
                            }
                            else
                            {
                                otlp.Protocol = OtlpExportProtocol.Grpc;
                                otlp.Endpoint = new Uri(options.OtelCollectorEndpoint);
                            }
                            otlp.TimeoutMilliseconds = options.ExportTimeoutMs;
                        });
                    }
                });

                var level = options.MinimumLogLevel
                    ?? TelemetryOptions.GetDefaultLogLevel(options.Environment, options.DebugLevel);

                builder.SetMinimumLevel(level);

                // Reduce framework noise — only errors from Microsoft/System in non-debug
                if (level > LogLevel.Debug)
                {
                    builder.AddFilter("Microsoft", LogLevel.Error);
                    builder.AddFilter("System.Net.Http", LogLevel.Error);
                }
            });

            return services;
        }
    }
}
