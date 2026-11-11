using System;
using System.Collections.Generic;
using System.Linq;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
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

        /// <summary>Full type name of the SDK's logger provider, as filter rules name it.</summary>
        internal static readonly string OtelProviderName = typeof(OpenTelemetryLoggerProvider).FullName!;

        /// <summary>The SDK provider's <see cref="ProviderAliasAttribute"/> (<c>OpenTelemetry</c>).</summary>
        internal static readonly string? OtelProviderAlias = AliasOf(typeof(OpenTelemetryLoggerProvider));

        /// <summary>Full type name of the helper's stdout provider.</summary>
        internal static readonly string JsonProviderName = typeof(JsonLineLoggerProvider).FullName!;

        /// <summary>Framework categories held at Error on the helper's providers above Debug (0.2 rule, N9 scope).</summary>
        internal static readonly IReadOnlyList<string> FrameworkCategories = new[] { "Microsoft", "System.Net.Http" };

        /// <summary>
        /// Whether the logs pipeline gets an exporter (contract D6): <c>console</c>, or <c>otlp</c>
        /// with a usable target — the same test <see cref="ApplyResolved"/> applies, without its
        /// diagnostics. Otherwise the stdout fallback (<see cref="JsonLineLoggerProvider"/>) writes.
        /// </summary>
        internal static bool LogsExporterActive(ResolvedTelemetry resolved)
        {
            var exporters = resolved.ResolvedSignalExporters("logs");
            return exporters.Contains("console")
                || (exporters.Contains("otlp") && resolved.OtlpTarget("logs") is { } target && Uri.TryCreate(target.Endpoint, UriKind.Absolute, out _));
        }

        /// <summary>The helper's stdout provider, registered next to the SDK's (design D-NET10).</summary>
        internal static void RegisterStdoutFallback(IServiceCollection services)
            => services.TryAddEnumerable(ServiceDescriptor.Singleton<ILoggerProvider, JsonLineLoggerProvider>(sp => new JsonLineLoggerProvider(sp)));

        /// <summary>Type name of the framework's console provider (no package reference to it).</summary>
        internal const string ConsoleProviderName = "Microsoft.Extensions.Logging.Console.ConsoleLoggerProvider";

        /// <summary>
        /// At host start: with the stdout fallback writing and the host's console provider still
        /// registered, every record reaches stdout twice (the JSON line and the console's); one
        /// warning says how to keep one.
        /// </summary>
        internal static void WarnDuplicateConsole(IServiceProvider sp, ResolvedTelemetry resolved, IOtelHelperDiagnostics diagnostics)
        {
            if (LogsExporterActive(resolved) || !sp.GetServices<ILoggerProvider>().Any(p => p.GetType().FullName == ConsoleProviderName))
            {
                return;
            }

            diagnostics.ReportOnce("stdout-duplicate-console", DiagnosticLevel.Warn,
                "no logs exporter is active, so each record is written to standard output twice: as the helper's JSON line and by the console logging provider; call builder.Logging.ClearProviders() before AddOtelHelper(), or remove only the console provider, to keep one");
        }

        /// <summary>
        /// Deferred level and framework filters, on the helper's two providers only (N9, DN8). A
        /// post-configure step, so it sees every rule the application registered, before or after
        /// <c>AddOtelHelper</c>; the global <see cref="LoggerFilterOptions.MinLevel"/> and the
        /// application's rules stay as they are (<see cref="ApplyHelperRules"/>).
        /// </summary>
        internal static void ConfigureFilters(IServiceCollection services)
            => services.AddSingleton<IPostConfigureOptions<LoggerFilterOptions>>(sp => new PostConfigureOptions<LoggerFilterOptions>(Options.DefaultName, filters =>
            {
                // An invalid code value: the host builds (its logger needs these filters),
                // one ERROR is reported, and start fails through ValidateOnStart (D-NET6).
                if (!ResolvedAccess.TryGet(sp, out var resolved))
                {
                    return;
                }

                // An environment only the application's resource set (AppResourceState) also picks
                // the level, re-applied through the change token once a provider built the resource.
                var environmentLevel = resolved.Environment is null && sp.GetService<AppResourceState>()?.AppEnvironment is { } appEnvironment
                    ? TelemetryOptions.GetDefaultLogLevel(appEnvironment, resolved.DebugLevel)
                    : resolved.MinimumLogLevel;
                ApplyHelperRules(filters, resolved.CodeMinimumLogLevel, environmentLevel, AppSetMinimumLevel(sp, filters), LogsExporterActive(resolved));
            }));

        /// <summary>
        /// Whether the application set <see cref="LoggerFilterOptions.MinLevel"/> itself
        /// (<c>SetMinimumLevel</c>): a value other than the framework default, or a second
        /// <c>DefaultLoggerLevelConfigureOptions</c> (the type <c>SetMinimumLevel</c> registers;
        /// <c>AddLogging</c> registers the first, at Information).
        /// </summary>
        internal static bool AppSetMinimumLevel(IServiceProvider sp, LoggerFilterOptions filters)
            => filters.MinLevel != LogLevel.Information
                || sp.GetServices<IConfigureOptions<LoggerFilterOptions>>().Count(o => o.GetType().Name == "DefaultLoggerLevelConfigureOptions") > 1;

        /// <summary>
        /// Adds provider-specific rules for the SDK provider and the stdout provider. The idle one
        /// (the stdout provider while a logs exporter is active, the SDK's otherwise) gets
        /// <see cref="LogLevel.None"/>. The active one gets:
        /// <list type="number">
        /// <item>a copy of each application rule for a category that names no provider (any
        /// provider-specific rule beats every rule without a provider, so without the copies the
        /// application's <c>Logging:LogLevel:&lt;category&gt;</c> would stop applying to it once
        /// the helper adds a rule of its own);</item>
        /// <item>a default-level rule (no category) only for code <c>MinimumLogLevel</c>, which
        /// wins over everything, or for the R4 environment level when the application chose no
        /// default: no rule of its own for that provider (type name or alias), no rule without
        /// provider and category with a level (<c>Logging:LogLevel:Default</c>,
        /// <c>AddFilter(null, level)</c>), no <c>SetMinimumLevel</c> (owner decision 2026-11-11:
        /// the application's explicit level wins, as in Python and 0.2.x);</item>
        /// <item>when that default level is above Debug, <c>Microsoft</c> and
        /// <c>System.Net.Http</c> at Error, unless the application has a rule for that exact
        /// category.</item>
        /// </list>
        /// Provider names and aliases match ordinally, as Microsoft.Extensions.Logging's
        /// <c>LoggerRuleSelector</c> compares them (measured).
        /// </summary>
        internal static void ApplyHelperRules(LoggerFilterOptions filters, LogLevel? codeLevel, LogLevel environmentLevel, bool appSetMinimumLevel, bool exporterActive)
        {
            var existing = filters.Rules.ToList();
            Apply(OtelProviderName, OtelProviderAlias, exporterActive);
            Apply(JsonProviderName, JsonLineLoggerProvider.Alias, !exporterActive);

            void Apply(string provider, string? alias, bool active)
            {
                bool Own(string? category) => existing.Any(r => Targets(r, provider, alias) && string.Equals(r.CategoryName, category, StringComparison.OrdinalIgnoreCase));

                if (!active)
                {
                    filters.Rules.Add(new LoggerFilterRule(provider, null, LogLevel.None, null));
                    return;
                }

                foreach (var rule in existing)
                {
                    if (rule.ProviderName is null && rule.CategoryName is { } category && !Own(category))
                    {
                        filters.Rules.Add(new LoggerFilterRule(provider, category, rule.LogLevel, rule.Filter));
                    }
                }

                var appDefault = existing.LastOrDefault(r => r.ProviderName is null && r.CategoryName is null);
                var ownDefault = existing.LastOrDefault(r => Targets(r, provider, alias) && r.CategoryName is null);
                LogLevel? level;
                if (codeLevel is { } code)
                {
                    level = code;
                    filters.Rules.Add(new LoggerFilterRule(provider, null, code, (ownDefault ?? appDefault)?.Filter));
                }
                else if (ownDefault is not null)
                {
                    level = ownDefault.LogLevel;
                }
                else if (appDefault?.LogLevel is { } appLevel)
                {
                    level = appLevel;
                }
                else if (appSetMinimumLevel)
                {
                    level = filters.MinLevel;
                }
                else
                {
                    level = environmentLevel;
                    filters.Rules.Add(new LoggerFilterRule(provider, null, environmentLevel, appDefault?.Filter));
                }

                if (level is null || level > LogLevel.Debug)
                {
                    foreach (var category in FrameworkCategories)
                    {
                        if (!Own(category) && !existing.Any(r => r.ProviderName is null && string.Equals(r.CategoryName, category, StringComparison.OrdinalIgnoreCase)))
                        {
                            filters.Rules.Add(new LoggerFilterRule(provider, category, LogLevel.Error, null));
                        }
                    }
                }
            }
        }

        // Ordinal, as the framework's rule selector compares provider names (measured, C13 fix
        // round 1: a lower-case alias in Logging:<alias>:LogLevel is not applied to the provider).
        private static bool Targets(LoggerFilterRule rule, string provider, string? alias)
            => rule.ProviderName is { } name && (name == provider || (alias is not null && name == alias));

        /// <summary>
        /// The default level the active helper provider ends up with (a category no rule names),
        /// as the rule selector picks it: the last rule for a helper provider without category
        /// (not <see cref="LogLevel.None"/>), else the application's last rule without provider
        /// and category, else <see cref="LoggerFilterOptions.MinLevel"/>.
        /// </summary>
        internal static LogLevel? HelperLevel(LoggerFilterOptions filters)
        {
            var own = filters.Rules.LastOrDefault(r => r.CategoryName is null && r.LogLevel is not LogLevel.None
                && (Targets(r, OtelProviderName, OtelProviderAlias) || Targets(r, JsonProviderName, JsonLineLoggerProvider.Alias)));
            if (own is not null)
            {
                return own.LogLevel;
            }

            var general = filters.Rules.LastOrDefault(r => r.ProviderName is null && r.CategoryName is null);
            return general is not null ? general.LogLevel : filters.MinLevel;
        }

        private static string? AliasOf(Type provider)
            => ((ProviderAliasAttribute?)Attribute.GetCustomAttribute(provider, typeof(ProviderAliasAttribute)))?.Alias;

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

            // ADR-014 records (C14a): the span's trace context, before every exporter below.
            builder.AddProcessor(new DbExceptionLog.CorrelationProcessor());

            // Disabled, OTEL_LOGS_EXPORTER=none or OTEL_SDK_DISABLED: no exporter; the stdout
            // fallback (JsonLineLoggerProvider, design D-NET10) writes instead, and the filter
            // rules keep this provider at None (LogsExporterActive must match the test below).
            var exporters = resolved.ResolvedSignalExporters("logs");
            var diagnostics = sp.GetRequiredService<IOtelHelperDiagnostics>();
            if (exporters.Contains("otlp")
                && OtlpExporters.OptionsFor("logs", resolved, diagnostics, sp.GetRequiredService<ExporterClients>()) is { } otlp)
            {
                builder.AddProcessor(CreateOtlpProcessor(sp, resolved, otlp, diagnostics));
            }

            // OTEL_LOGS_EXPORTER=console (or the alias logging): the SDK console exporter, built
            // directly because AddConsoleExporter registers services, which this deferred step cannot.
            if (exporters.Contains("console"))
            {
                builder.AddProcessor(new SimpleLogRecordExportProcessor(new ConsoleLogRecordExporter(new ConsoleExporterOptions())));
            }
        }

        /// <summary>
        /// The OTLP batch processor: queue size and delay from the SDK's options, the export deadline
        /// of logs (A18: <c>OTEL_BLRP_EXPORT_TIMEOUT</c> is not honored) and the batch size clamped to
        /// the queue size (A18: the SDK constructor would throw).
        /// </summary>
        internal static BatchLogRecordExportProcessor CreateOtlpProcessor(IServiceProvider sp, ResolvedTelemetry resolved, OtlpExporterOptions otlp, IOtelHelperDiagnostics diagnostics)
        {
            var batch = sp.GetRequiredService<IOptionsMonitor<LogRecordExportProcessorOptions>>()
                .Get(Options.DefaultName).BatchExportProcessorOptions;
            ExportSettingsResolver.WarnIgnoredOptionTimeout(
                "LogRecordExportProcessorOptions.BatchExportProcessorOptions.ExporterTimeoutMilliseconds", "OTEL_BLRP_EXPORT_TIMEOUT",
                () => batch.ExporterTimeoutMilliseconds != new BatchExportLogRecordProcessorOptions().ExporterTimeoutMilliseconds, diagnostics);
            return new BatchLogRecordExportProcessor(
                new OtlpLogExporter(otlp),
                batch.MaxQueueSize,
                batch.ScheduledDelayMilliseconds,
                OtlpExporters.TimeoutFor("logs", resolved),
                ExportSettingsResolver.ClampBatchSize("OTEL_BLRP", "BatchExportLogRecordProcessorOptions", batch.MaxQueueSize, batch.MaxExportBatchSize, diagnostics));
        }
    }
}
