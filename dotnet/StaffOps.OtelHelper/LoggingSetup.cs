using System;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Logs;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Exporters;

namespace StaffOps.OtelHelper.Logging
{
    /// <summary>
    /// Logging in two phases (design D-NET3): the OpenTelemetry logger provider is always
    /// registered; the level, filters, resource and OTLP processor follow
    /// <see cref="ResolvedTelemetry"/> when the logging pipeline is built.
    /// </summary>
    internal static class LoggingSetup
    {
        /// <summary>Formatting of the OpenTelemetry logger provider (constant values).</summary>
        internal static void ConfigureLoggerOptions(OpenTelemetryLoggerOptions logging)
        {
            logging.IncludeFormattedMessage = true;
            logging.IncludeScopes = true;
            logging.ParseStateValues = true;
        }

        /// <summary>Deferred level and framework filters (the 0.2 rule; task C13 revisits the filters, N9).</summary>
        internal static void ConfigureFilters(IServiceCollection services)
            => services.AddSingleton<IConfigureOptions<LoggerFilterOptions>>(sp => new ConfigureOptions<LoggerFilterOptions>(filters =>
            {
                // An invalid code value: the host builds (its logger needs these filters),
                // one ERROR is reported, and start fails through ValidateOnStart (D-NET6).
                if (!ResolvedAccess.TryGet(sp, out var resolved))
                {
                    return;
                }

                // An environment only the application's resource set (AppResourceState) also picks
                // the level, re-applied through the change token once a provider built the resource.
                var level = resolved.CodeMinimumLogLevel
                    ?? (resolved.Environment is null && sp.GetService<AppResourceState>()?.AppEnvironment is { } appEnvironment
                        ? TelemetryOptions.GetDefaultLogLevel(appEnvironment, resolved.DebugLevel)
                        : resolved.MinimumLogLevel);
                filters.MinLevel = level;
                if (level > LogLevel.Debug)
                {
                    filters.Rules.Add(new LoggerFilterRule(null, "Microsoft", LogLevel.Error, null));
                    filters.Rules.Add(new LoggerFilterRule(null, "System.Net.Http", LogLevel.Error, null));
                }
            }));

        /// <summary>Deferred part, at logger provider build.</summary>
        internal static void ApplyResolved(IServiceProvider sp, LoggerProviderBuilder builder)
        {
            // Built by the host's first logger, before start: with an invalid code value this
            // provider gets nothing, and host start fails through ValidateOnStart (D-NET6).
            if (!ResolvedAccess.TryGet(sp, out var resolved))
            {
                return;
            }

            builder.ConfigureResource(r => ResourceSetup.Apply(r, resolved, sp, "logs"));
            if (!resolved.IsSignalEnabled("logs"))
            {
                return;
            }

            var diagnostics = sp.GetRequiredService<IOtelHelperDiagnostics>();
            if (OtlpExporters.OptionsFor("logs", resolved, diagnostics, sp.GetRequiredService<ExporterClients>()) is { } otlp)
            {
                var batch = sp.GetRequiredService<IOptionsMonitor<LogRecordExportProcessorOptions>>()
                    .Get(Options.DefaultName).BatchExportProcessorOptions;
                builder.AddProcessor(new BatchLogRecordExportProcessor(
                    new OtlpLogExporter(otlp),
                    batch.MaxQueueSize,
                    batch.ScheduledDelayMilliseconds,
                    batch.ExporterTimeoutMilliseconds,
                    batch.MaxExportBatchSize));
            }
        }
    }
}
