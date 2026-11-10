using System;
using System.Linq;
using System.Text.RegularExpressions;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using OpenTelemetry.Exporter;
using OpenTelemetry.Metrics;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Exporters;

namespace StaffOps.OtelHelper.Metrics
{
    /// <summary>
    /// Metrics in two phases (design D-NET3): instrumentations and the Prometheus listener
    /// reader are registered when <c>AddOtelHelper</c> runs — the reader always, so whether
    /// it listens follows the resolved options; meters, views and the OTLP reader are added
    /// when the provider is built.
    /// </summary>
    internal static class MetricsSetup
    {
        /// <summary>Placeholder port of an inactive listener: it is never bound (no prefix).</summary>
        private const int InactiveListenerPort = TelemetryOptions.DefaultPrometheusMetricsPort;

        /// <summary>Eager part: service-registering calls only.</summary>
        internal static MeterProviderBuilder AddHelperInstrumentation(this MeterProviderBuilder builder)
            => builder
                .AddRuntimeInstrumentation()
                .AddAspNetCoreInstrumentation()
                .AddHttpClientInstrumentation()
                .SetExemplarFilter(ExemplarFilterType.TraceBased)
                .AddPrometheusHttpListener();

        /// <summary>
        /// The meters the helper subscribes or instruments: the service meter, the additional
        /// meters, and the ASP.NET Core, <c>System.Net.*</c>, runtime and OpenTelemetry
        /// instrumentation meters.
        /// </summary>
        internal static bool IsHelperInstrumentation(string meterName, ResolvedTelemetry resolved)
            => meterName == resolved.ServiceName
                || resolved.AdditionalMeters.Contains(meterName)
                || meterName.StartsWith("Microsoft.AspNetCore", StringComparison.Ordinal)
                || meterName.StartsWith("System.Net.", StringComparison.Ordinal)
                || meterName == "System.Runtime"
                || meterName.StartsWith("OpenTelemetry.Instrumentation.", StringComparison.Ordinal);

        /// <summary>Name of the OTLP exporter and reader options of the histogram-aggregation path.</summary>
        internal const string HistogramPathName = "StaffOps.OtelHelper.OtlpMetrics";

        /// <summary>Marks a collection where the histogram-aggregation path owns the OTLP metric reader.</summary>
        internal sealed class HistogramAggregationPath
        {
        }

        internal const string ExplicitBucketHistogram = "explicit_bucket_histogram";
        internal const string ExponentialBucketHistogram = "base2_exponential_bucket_histogram";

        /// <summary>
        /// The <c>OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION</c> value visible when
        /// <c>AddOtelHelper</c> runs: the process environment, else an <c>IConfiguration</c>
        /// registered as an instance before the call. A host builder registers its configuration
        /// as a factory, so an <c>appsettings.json</c>-only value is not seen here (it is warned at
        /// provider build, <see cref="WarnHistogramAggregation"/>).
        /// </summary>
        internal static string? HistogramAggregationAtRegistration(IServiceCollection services)
        {
            var variable = TelemetryResolver.HistogramAggregationVariable;
            var fromEnv = System.Environment.GetEnvironmentVariable(variable);
            if (!string.IsNullOrWhiteSpace(fromEnv))
            {
                return fromEnv;
            }

            return services.LastOrDefault(d => d.ServiceType == typeof(Microsoft.Extensions.Configuration.IConfiguration) && !d.IsKeyedService)
                ?.ImplementationInstance is Microsoft.Extensions.Configuration.IConfiguration configuration
                && !string.IsNullOrWhiteSpace(configuration[variable])
                ? configuration[variable]
                : null;
        }

        /// <summary>
        /// Whether the histogram-aggregation path is needed: only for
        /// <c>base2_exponential_bucket_histogram</c> (case-insensitive). <c>explicit_bucket_histogram</c>
        /// is the default and needs no path; any other value is warned at provider build.
        /// </summary>
        internal static bool HistogramAggregationRequested(IServiceCollection services)
            => string.Equals(HistogramAggregationAtRegistration(services), ExponentialBucketHistogram, StringComparison.OrdinalIgnoreCase);

        /// <summary>The SDK's internal marker of <c>UseOtlpExporter()</c> (what its <c>EnsureNoUseOtlpExporterRegistrations</c> checks).</summary>
        internal static readonly Type? UseOtlpExporterRegistrationType =
            typeof(OtlpExporterOptions).Assembly.GetType("OpenTelemetry.Exporter.UseOtlpExporterRegistration");

        /// <summary>Whether <c>UseOtlpExporter()</c> is registered in <paramref name="services"/> (matched by type name).</summary>
        internal static bool UseOtlpExporterRegistered(IServiceCollection services)
            => services.Any(d => d.ServiceType.FullName == "OpenTelemetry.Exporter.UseOtlpExporterRegistration");

        /// <summary>Whether <c>UseOtlpExporter()</c> is registered in the built provider.</summary>
        internal static bool UseOtlpExporterRegistered(IServiceProvider sp)
            => UseOtlpExporterRegistrationType is { } type && sp.GetService(type) is not null;

        /// <summary>
        /// Provider-build warnings for the histogram variable and <c>UseOtlpExporter()</c>: the
        /// doubled export of <c>UseOtlpExporter()</c> next to the helper; an unsupported value
        /// (compared as written, case-insensitive: the SDK ignores a padded value); and
        /// <c>base2_exponential_bucket_histogram</c> in the SDK's source (<c>IConfiguration</c>)
        /// that <c>AddOtelHelper</c> did not see — skipped when <c>UseOtlpExporter()</c> is
        /// registered, whose own reader reads <c>IConfiguration</c>.
        /// </summary>
        internal static void WarnHistogramAggregation(IServiceProvider sp, ResolvedTelemetry resolved, Microsoft.Extensions.Configuration.IConfiguration? configuration, IOtelHelperDiagnostics diagnostics)
        {
            var useOtlp = UseOtlpExporterRegistered(sp);

            // Doubled only when the helper itself exports over OTLP for at least one signal.
            if (useOtlp && ResolvedTelemetry.Signals.Any(signal => resolved.OtlpTarget(signal) is not null))
            {
                diagnostics.ReportOnce("use-otlp-exporter", DiagnosticLevel.Warn,
                    "UseOtlpExporter() with AddOtelHelper exports every signal twice; drop one");
            }

            var variable = TelemetryResolver.HistogramAggregationVariable;
            var raw = configuration?[variable] ?? System.Environment.GetEnvironmentVariable(variable);
            if (string.IsNullOrWhiteSpace(raw) || string.Equals(raw, ExplicitBucketHistogram, StringComparison.OrdinalIgnoreCase))
            {
                return;
            }

            if (!string.Equals(raw, ExponentialBucketHistogram, StringComparison.OrdinalIgnoreCase))
            {
                diagnostics.ReportOnce($"invalid:{variable}:{raw}", DiagnosticLevel.Warn,
                    $"invalid {variable}: unsupported aggregation (values are compared as written, without trimming), using explicit_bucket_histogram; valid values: explicit_bucket_histogram, base2_exponential_bucket_histogram",
                    ("variable", variable), ("value", raw));
                return;
            }

            if (useOtlp || sp.GetService<HistogramAggregationPath>() is not null)
            {
                return;
            }

            var atRegistration = System.Environment.GetEnvironmentVariable(variable);
            if (string.IsNullOrWhiteSpace(atRegistration))
            {
                diagnostics.ReportOnce("late:" + variable, DiagnosticLevel.Warn,
                    $"{variable} is set only in IConfiguration, which is registered as a factory that AddOtelHelper cannot read, so it is not applied; set it in the process environment",
                    ("variable", variable));
            }
            else if (!string.Equals(atRegistration, raw, StringComparison.Ordinal))
            {
                diagnostics.ReportOnce("late:" + variable, DiagnosticLevel.Warn,
                    $"{variable} differs between IConfiguration and the process environment; AddOtelHelper used the process environment value, so base2_exponential_bucket_histogram is not applied",
                    ("variable", variable), ("used", atRegistration));
            }
        }

        /// <summary>Whether the histogram path's reader really exports: metrics on and an OTLP metrics target.</summary>
        private static bool HistogramPathActive(ResolvedTelemetry resolved)
            => resolved.IsSignalEnabled("metrics") && resolved.ResolvedMetricExporters().Contains("otlp") && resolved.OtlpTarget("metrics") is not null;

        /// <summary>
        /// Option (c), owner decision 2026-11-10: the SDK applies the default histogram aggregation
        /// only on the reader <c>AddOtlpExporter</c> builds (its reader option is internal), and
        /// <c>AddOtlpExporter</c> registers services, so it runs now, when
        /// <c>base2_exponential_bucket_histogram</c> is requested. Every value is set late: the named
        /// exporter options get the helper's endpoint, protocol, timeout, compression and client
        /// (or, with OTLP inactive at provider build, a client that never touches the network,
        /// warned once); the named reader options start from the default-named
        /// <c>MetricReaderOptions</c> (the app's own <c>Configure</c>, the helper's temporality, the
        /// SDK's timeout) and get the resolved interval, as the direct reader does; an application
        /// <c>Configure</c> of the named options still wins. An inactive reader only collects
        /// (interval <c>int.MaxValue</c>); its cost is an idle reader (D-NET3 note).
        /// </summary>
        internal static void AddHistogramAggregationPath(MeterProviderBuilder builder, IServiceCollection services)
        {
            services.AddSingleton<HistogramAggregationPath>();

            // Registered before AddOtlpExporter, so it runs first at provider build: a
            // UseOtlpExporter() added after AddOtelHelper makes the SDK throw on the next step.
            services.ConfigureOpenTelemetryMeterProvider((sp, _) =>
            {
                if (UseOtlpExporterRegistered(sp))
                {
                    sp.GetService<IOtelHelperDiagnostics>()?.ReportOnce("use-otlp-exporter-histogram", DiagnosticLevel.Error,
                        $"{TelemetryResolver.HistogramAggregationVariable} made AddOtelHelper register the SDK's AddOtlpExporter reader, and UseOtlpExporter() was called after AddOtelHelper: the SDK refuses both and fails the MeterProvider build; drop UseOtlpExporter() or call it before AddOtelHelper",
                        ("variable", TelemetryResolver.HistogramAggregationVariable));
                }
            });
            builder.AddOtlpExporter(HistogramPathName, (_, _) => { });
            services.AddOptions<OtlpExporterOptions>(HistogramPathName)
                .Configure<IServiceProvider>(ConfigureHistogramPathExporter);
            services.AddOptions<MetricReaderOptions>(HistogramPathName)
                .Configure<IServiceProvider>((opts, sp) =>
                {
                    var defaults = sp.GetRequiredService<IOptionsMonitor<MetricReaderOptions>>().Get(Options.DefaultName);
                    opts.TemporalityPreference = defaults.TemporalityPreference;
                    opts.PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds = defaults.PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds;
                    opts.PeriodicExportingMetricReaderOptions.ExportIntervalMilliseconds = defaults.PeriodicExportingMetricReaderOptions.ExportIntervalMilliseconds;
                    if (!ResolvedAccess.TryGet(sp, out var resolved))
                    {
                        return;
                    }

                    opts.PeriodicExportingMetricReaderOptions.ExportIntervalMilliseconds = HistogramPathActive(resolved)
                        ? (int)Math.Min(resolved.ExportIntervalMs, int.MaxValue)
                        : int.MaxValue;
                    WarnIgnoredReaderTimeout(defaults, sp.GetService<IOtelHelperDiagnostics>());
                })
                // Last, so an application Configure of the named options cannot replace the deadline (A18).
                .PostConfigure<IServiceProvider>((opts, sp) =>
                {
                    if (ResolvedAccess.TryGet(sp, out var resolved))
                    {
                        // The Configure above copied the default-named value: anything else came from an
                        // application Configure of the named options, which the deadline replaces (warned).
                        var defaults = sp.GetRequiredService<IOptionsMonitor<MetricReaderOptions>>().Get(Options.DefaultName);
                        if (opts.PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds != defaults.PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds)
                        {
                            ExportSettingsResolver.WarnIgnoredOptionTimeout(
                                $"MetricReaderOptions(\"{HistogramPathName}\").PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds", "",
                                () => true, sp.GetService<IOtelHelperDiagnostics>());
                        }

                        opts.PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds = OtlpExporters.TimeoutFor("metrics", resolved);
                    }
                });
        }

        private static void ConfigureHistogramPathExporter(OtlpExporterOptions opts, IServiceProvider sp)
        {
            var diagnostics = sp.GetRequiredService<IOtelHelperDiagnostics>();
            var clients = sp.GetRequiredService<ExporterClients>();
            var resolvedOk = ResolvedAccess.TryGet(sp, out var resolved);
            if (resolvedOk && HistogramPathActive(resolved)
                && OtlpExporters.OptionsFor("metrics", resolved, diagnostics, clients) is { } built)
            {
                opts.Endpoint = built.Endpoint;
                opts.Protocol = built.Protocol;
                opts.TimeoutMilliseconds = built.TimeoutMilliseconds;
                opts.Compression = built.Compression;
                opts.Headers = string.Empty;
                opts.HttpClientFactory = built.HttpClientFactory;
                return;
            }

            if (resolvedOk)
            {
                // With invalid code options the ERROR of ResolvedAccess stays alone.
                diagnostics.ReportOnce("inactive:" + TelemetryResolver.HistogramAggregationVariable, DiagnosticLevel.Warn,
                    $"{TelemetryResolver.HistogramAggregationVariable} is set but OTLP metrics are not active: its OTLP reader stays idle and sends nothing",
                    ("variable", TelemetryResolver.HistogramAggregationVariable));
            }

            opts.Endpoint = new Uri("http://otlp-metrics-inactive.invalid/v1/metrics");
            opts.Protocol = OtlpExportProtocol.HttpProtobuf;
            opts.Compression = OtlpExportCompression.None;
            opts.Headers = string.Empty;
            var stub = new HttpClient(new NoNetworkHandler());
            clients.Add(stub);
            opts.HttpClientFactory = () => stub;
        }

        /// <summary>Answers every request with an empty 200 without any network access (inactive OTLP reader).</summary>
        internal sealed class NoNetworkHandler : HttpMessageHandler
        {
            /// <summary>Requests answered (tests).</summary>
            internal static int Answered;

            protected override HttpResponseMessage Send(HttpRequestMessage request, CancellationToken cancellationToken)
            {
                Interlocked.Increment(ref Answered);
                return new HttpResponseMessage(System.Net.HttpStatusCode.OK) { Content = new ByteArrayContent(Array.Empty<byte>()), RequestMessage = request };
            }

            protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
                => Task.FromResult(Send(request, cancellationToken));
        }

        /// <summary>Temporality from the environment, as upstream's <c>AddOtlpExporter</c> does; code configured later wins.</summary>
        internal static void ConfigureReaderOptions(IServiceCollection services)
            => services.AddOptions<MetricReaderOptions>()
                .Configure<IServiceProvider>((opts, sp) =>
                {
                    if (ResolvedAccess.TryGet(sp, out var resolved) && resolved.MetricTemporalityConfigured)
                    {
                        opts.TemporalityPreference = resolved.MetricTemporality;
                    }
                });

        /// <summary>Deferred values of the Prometheus listener options.</summary>
        internal static void ConfigureListenerOptions(IServiceCollection services)
            => services.AddOptions<PrometheusHttpListenerOptions>()
                .Configure<IServiceProvider>((opts, sp) =>
                {
                    var active = ResolvedAccess.TryGet(sp, out var resolved) && resolved.PrometheusListenerActive;
                    ConfigurePrometheusListener(opts, active ? resolved.PrometheusMetricsPort : InactiveListenerPort, active);
                });

        /// <summary>Deferred part, at provider build.</summary>
        internal static void ApplyResolved(IServiceProvider sp, MeterProviderBuilder builder)
        {
            if (!ResolvedAccess.TryGet(sp, out var resolved))
            {
                return;
            }

            builder.ConfigureResource(r => ResourceSetup.Apply(r, resolved, sp, "metrics"));

            // "Off" (signal disabled, or no metric exporter at all): instrumentation and the
            // non-listening reader registered, no meter, no OTLP reader, and a view that drops the
            // helper's own instrumentation meters, so the idle reader aggregates nothing of the
            // helper's (D-NET3, fix rounds 2026-11-10). Meters and readers the application adds
            // itself are not touched. `prometheus` with port 0 is an active exporter (the app may
            // mount the scraping endpoint, HOW-TO), not "off".
            var otlpActive = resolved.ResolvedMetricExporters().Contains("otlp");
            if (!resolved.IsSignalEnabled("metrics") || resolved.ResolvedMetricExporters().Count == 0)
            {
                builder.AddView(instrument => IsHelperInstrumentation(instrument.Meter.Name, resolved) ? MetricStreamConfiguration.Drop : null);
                return;
            }

            builder.AddMeter(resolved.ServiceName);
            foreach (var meter in resolved.AdditionalMeters)
            {
                builder.AddMeter(meter);
            }

            if (!string.IsNullOrEmpty(resolved.DisabledMetrics))
            {
                var patterns = resolved.DisabledMetrics
                    .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                    .Select(p => "^" + Regex.Escape(p).Replace("\\*", ".*") + "$")
                    .Select(p => new Regex(p, RegexOptions.Compiled | RegexOptions.IgnoreCase))
                    .ToList();

                builder.AddView(instrument => patterns.Any(p => p.IsMatch(instrument.Name)) ? MetricStreamConfiguration.Drop : null);
            }

            // The histogram-aggregation path (option (c)) owns the OTLP reader when it was registered.
            if (!otlpActive || sp.GetService<HistogramAggregationPath>() is not null)
            {
                return;
            }

            var diagnostics = sp.GetRequiredService<IOtelHelperDiagnostics>();
            if (OtlpExporters.OptionsFor("metrics", resolved, diagnostics, sp.GetRequiredService<ExporterClients>()) is { } otlp)
            {
                var reader = CreateOtlpReader(sp, resolved, otlp);
                builder.AddReader(reader);
            }
        }

        /// <summary>
        /// The OTLP periodic reader: the resolved interval, and the temporality and export timeout
        /// of the default <c>MetricReaderOptions</c>. The helper configures their temporality from
        /// <c>OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE</c> (the SDK does so only under
        /// <c>AddOtlpExporter</c>, measured 2026-11-10), registered at <c>AddOtelHelper</c> like
        /// upstream, so an application's later <c>Configure&lt;MetricReaderOptions&gt;</c> wins.
        /// </summary>
        internal static PeriodicExportingMetricReader CreateOtlpReader(IServiceProvider sp, ResolvedTelemetry resolved, OtlpExporterOptions otlp)
        {
            var readerOptions = sp.GetRequiredService<IOptionsMonitor<MetricReaderOptions>>().Get(Options.DefaultName);
            WarnIgnoredReaderTimeout(readerOptions, sp.GetService<IOtelHelperDiagnostics>());
            var interval = (int)Math.Min(resolved.ExportIntervalMs, int.MaxValue);
            // The export deadline of metrics (A18): OTEL_METRIC_EXPORT_TIMEOUT is not honored.
            return new PeriodicExportingMetricReader(
                new OtlpMetricExporter(otlp),
                interval,
                OtlpExporters.TimeoutFor("metrics", resolved))
            {
                TemporalityPreference = readerOptions.TemporalityPreference,
            };
        }

        /// <summary>The default-named reader options' export timeout is not used (A18 deadline): warned once naming the option.</summary>
        internal static void WarnIgnoredReaderTimeout(MetricReaderOptions defaults, IOtelHelperDiagnostics? diagnostics)
            => ExportSettingsResolver.WarnIgnoredOptionTimeout(
                "MetricReaderOptions.PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds", "OTEL_METRIC_EXPORT_TIMEOUT",
                () => defaults.PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds is not null, diagnostics);

        /// <summary>
        /// Prefix the Prometheus listener binds: every IPv4 address (the managed
        /// <c>HttpListener</c> maps <c>+</c> to <c>IPAddress.Any</c>; no IPv6), path
        /// <c>/metrics/</c> (design D-NET11). The upstream default host is
        /// <c>localhost</c>, which other containers and pods cannot reach, and a
        /// <c>*</c>, <c>+</c> or <c>0.0.0.0</c> <c>Host</c> aborts the provider build.
        /// </summary>
        internal static string PrometheusListenerPrefix(int port) => $"http://+:{port}/metrics/";

        /// <summary>
        /// Keeps the 0.2.x listener behavior on the 1.20.x exporter: bind all
        /// interfaces on the helper's port and serve a fresh collection on every
        /// scrape. This delegate runs after the options factory has read
        /// <c>OTEL_EXPORTER_PROMETHEUS_HOST</c>/<c>_PORT</c>, so the values set
        /// here win and those variables have no effect, as in 0.2.x. An inactive
        /// listener (<paramref name="active"/> false) gets no prefix: nothing listens
        /// (design D-NET3, "no listener").
        /// </summary>
        internal static void ConfigurePrometheusListener(PrometheusHttpListenerOptions opts, int port, bool active = true)
        {
            // A valid host for the exporter's own prefix, which the delegate below
            // replaces; an env-provided "*" would otherwise abort the build.
            opts.Host = "localhost";
            opts.Port = port;
            // 0.2.x (1.9.0-beta.2) had no response cache; upstream now defaults to 300 ms.
            opts.ScrapeResponseCacheDurationMilliseconds = 0;
            opts.ConfigureHttpListener = active
                ? static (o, listener) =>
                {
                    // Clear first: keeping the localhost prefix next to "+" fails on
                    // Linux with "Address already in use" (exporter README).
                    listener.Prefixes.Clear();
                    listener.Prefixes.Add(PrometheusListenerPrefix(o.Port));
                }
                : static (_, listener) => listener.Prefixes.Clear();
        }
    }
}
