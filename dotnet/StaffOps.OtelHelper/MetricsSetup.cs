using System;
using System.Collections.Generic;
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
    /// Metrics in two phases (design D-NET3): instrumentations are registered when
    /// <c>AddOtelHelper</c> runs; meters, views and the readers (OTLP, console and, only when it
    /// binds, the Prometheus listener; task C12) are added when the provider is built.
    /// </summary>
    internal static class MetricsSetup
    {
        /// <summary>Eager part: service-registering calls only.</summary>
        internal static MeterProviderBuilder AddHelperInstrumentation(this MeterProviderBuilder builder)
            => builder
                .AddRuntimeInstrumentation()
                .AddAspNetCoreInstrumentation()
                .AddHttpClientInstrumentation();

        /// <summary>
        /// The meters the helper subscribes or instruments: the service meter and its children
        /// (<c>&lt;ServiceName&gt;.*</c>, N5), the additional meters, and the ASP.NET Core,
        /// <c>System.Net.*</c>, runtime and OpenTelemetry instrumentation meters.
        /// </summary>
        internal static bool IsHelperInstrumentation(string meterName, ResolvedTelemetry resolved)
            => IsServiceMeter(meterName, resolved.ServiceName)
                || resolved.AdditionalMeters.Contains(meterName)
                || meterName.StartsWith("Microsoft.AspNetCore", StringComparison.Ordinal)
                || meterName.StartsWith("System.Net.", StringComparison.Ordinal)
                || meterName == "System.Runtime"
                || meterName.StartsWith("OpenTelemetry.Instrumentation.", StringComparison.Ordinal);

        /// <summary>
        /// The service meter <c>&lt;ServiceName&gt;</c> or one of its children <c>&lt;ServiceName&gt;.*</c>
        /// (DN5, N5), as the SDK's <c>AddMeter</c> wildcard matches them (<c>svc.</c> included).
        /// </summary>
        internal static bool IsServiceMeter(string meterName, string serviceName)
            => meterName == serviceName || meterName.StartsWith(serviceName + ".", StringComparison.Ordinal);

        /// <summary>
        /// The HTTP metrics semantic conventions v1.43 make opt-in (contract D1 row "HTTP opt-in
        /// metrics", A30): the list Go and Python drop, plus the .NET-only
        /// <c>http.client.request.time_in_queue</c>. Of these, .NET 8 records the active requests and
        /// the three connection metrics; the body sizes and the old-generation sizes have no instrument.
        /// </summary>
        internal static readonly string[] HttpOptInMetricNames =
        {
            "http.server.active_requests", "http.client.active_requests",
            "http.server.request.body.size", "http.server.response.body.size",
            "http.client.request.body.size", "http.client.response.body.size",
            "http.server.request.size", "http.server.response.size",
            "http.client.request.size", "http.client.response.size",
            "http.client.open_connections", "http.client.connection.duration",
            "http.client.request.time_in_queue",
        };

        /// <summary>
        /// The framework meters the helper subscribes for HTTP (ASP.NET Core 8 <c>AddAspNetCoreInstrumentation</c>
        /// and <c>AddHttpClientInstrumentation</c>): the opt-in view is scoped to them, so an
        /// application meter's instrument of the same name is kept (as Go and Python scope theirs).
        /// </summary>
        internal static readonly string[] HttpInstrumentationMeters = { "Microsoft.AspNetCore.Hosting", "System.Net.Http" };

        /// <summary>Whether the opt-in view drops the instrument <paramref name="instrumentName"/> of <paramref name="meterName"/>.</summary>
        internal static bool IsHttpOptInInstrument(string meterName, string instrumentName)
            => Array.IndexOf(HttpInstrumentationMeters, meterName) >= 0 && Array.IndexOf(HttpOptInMetricNames, instrumentName) >= 0;

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
        internal static bool HistogramPathActive(ResolvedTelemetry resolved)
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

        /// <summary>
        /// Deferred values of the Prometheus listener options: the resolved port and host, applied
        /// only when the helper registers its listener reader (an application's own
        /// <c>AddPrometheusHttpListener</c> next to an inactive helper listener keeps its options).
        /// </summary>
        internal static void ConfigureListenerOptions(IServiceCollection services)
            => services.AddOptions<PrometheusHttpListenerOptions>(ListenerName)
                .Configure<IServiceProvider>((opts, sp) =>
                {
                    if (ResolvedAccess.TryGet(sp, out var resolved) && resolved.PrometheusListenerActive)
                    {
                        ConfigurePrometheusListener(opts, resolved.PrometheusMetricsPort, resolved.PrometheusBindHost!);
                    }
                });

        /// <summary>
        /// Name of the helper's listener options and reader (fix round 1 of C12): an application's
        /// own <c>AddPrometheusHttpListener()</c> uses the default-named options and keeps them.
        /// </summary>
        internal const string ListenerName = "StaffOps.OtelHelper.Prometheus";

        /// <summary>Deferred part, at provider build.</summary>
        internal static void ApplyResolved(IServiceProvider sp, MeterProviderBuilder builder)
        {
            if (!ResolvedAccess.TryGet(sp, out var resolved))
            {
                return;
            }

            builder.ConfigureResource(r => ResourceSetup.Apply(r, resolved, sp, "metrics"));

            // Always set (ADR-013 exception): the .NET SDK default is AlwaysOff, the spec's trace_based.
            // A deferred builder runs ConfigureBuilder at once, so an application's later
            // SetExemplarFilter still wins.
            builder.SetExemplarFilter(resolved.ExemplarFilter);

            // The database driver meters lose their pool-name attribute (C14a fix round 1): an unnamed
            // pool is named after its connection string. One stream per matching view, so this view
            // also drops what the helper's drop views below drop (a Drop elsewhere would not stop it).
            var helperOff = !resolved.IsSignalEnabled("metrics") || resolved.ResolvedMetricExporters().Count == 0;
            var disabledPatterns = string.IsNullOrEmpty(resolved.DisabledMetrics) ? Array.Empty<Regex>() : DisabledMetricPatterns(resolved.DisabledMetrics);
            builder.AddView(instrument => DbMetricViews.View(instrument,
                i => (helperOff && IsHelperInstrumentation(i.Meter.Name, resolved)) || disabledPatterns.Any(p => p.IsMatch(i.Name))));

            // "Off" (signal disabled, or no metric exporter at all): instrumentation registered, no
            // meter, no reader, and a view that drops the helper's own instrumentation meters, so a
            // reader the application adds aggregates nothing of the helper's (D-NET3, fix rounds
            // 2026-11-10). Meters and readers the application adds itself are not touched.
            // `prometheus` with port 0 is an active exporter (the app may mount the scraping
            // endpoint with its own exporter, HOW-TO), not "off".
            var otlpActive = resolved.ResolvedMetricExporters().Contains("otlp");
            if (!resolved.IsSignalEnabled("metrics") || resolved.ResolvedMetricExporters().Count == 0)
            {
                builder.AddView(instrument => IsHelperInstrumentation(instrument.Meter.Name, resolved) ? MetricStreamConfiguration.Drop : null);
                return;
            }

            // The service meter and its children (N5: <ServiceName>.* is a wildcard of the SDK's AddMeter).
            builder.AddMeter(resolved.ServiceName, resolved.ServiceName + ".*");
            foreach (var meter in resolved.AdditionalMeters)
            {
                builder.AddMeter(meter);
            }

            // HTTP opt-in metrics (A30): off, the framework meters keep only the two durations.
            if (!resolved.HttpOptInMetrics)
            {
                builder.AddView(instrument => IsHttpOptInInstrument(instrument.Meter.Name, instrument.Name) ? MetricStreamConfiguration.Drop : null);
            }
            else
            {
                sp.GetRequiredService<IOtelHelperDiagnostics>().ReportOnce("info:http-opt-in-metrics", DiagnosticLevel.Info,
                    "HTTP opt-in metrics on: ASP.NET Core and System.Net.Http record the active requests and the client connection metrics, and have no body-size instruments");
            }

            // The standalone listener, only when it binds (C12, revising D-NET3): with no configure
            // delegate AddPrometheusHttpListener registers no service, only a reader factory, which
            // this deferred step runs at once. A pull-only reader fails every collection outside a
            // scrape, so registering it always made MeterProvider.ForceFlush and Shutdown return
            // false in an OTLP-only configuration (measured, C8 notes).
            if (resolved.PrometheusListenerWanted)
            {
                AddListener(sp, builder, resolved);
            }

            // OTEL_METRICS_EXPORTER=console (or the alias logging): the SDK console exporter on a
            // periodic reader with the resolved interval, built directly (AddConsoleExporter
            // registers services, which this deferred step cannot).
            if (resolved.ResolvedMetricExporters().Contains("console"))
            {
                builder.AddReader(Track(sp, CreateConsoleReader(sp, resolved)));
            }

            if (!string.IsNullOrEmpty(resolved.DisabledMetrics))
            {
                var patterns = DisabledMetricPatterns(resolved.DisabledMetrics);
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
                builder.AddReader(Track(sp, CreateOtlpReader(sp, resolved, otlp)));
            }
        }

        /// <summary>
        /// The standalone listener, named (<see cref="ListenerName"/>). IPv6 hosts follow the fix-round-1
        /// decision (the managed <c>HttpListener</c> refuses every IPv6 literal prefix, measured on
        /// .NET 8.0.33 and 10.0.14): the loopback binds 127.0.0.1 with one warning; any other IPv6
        /// address gets no listener and one ERROR (never widened to every interface); <c>::</c>
        /// binds every IPv4 interface with one warning. A failed bind (busy port, an address the
        /// machine does not own) writes one ERROR naming what set the port and the host, then the
        /// build fails with the upstream exception, as the contract says.
        /// </summary>
        private static void AddListener(IServiceProvider sp, MeterProviderBuilder builder, ResolvedTelemetry resolved)
        {
            var diagnostics = sp.GetRequiredService<IOtelHelperDiagnostics>();
            var variable = TelemetryResolver.PrometheusHostEnvVar;
            if (resolved.PrometheusBindHost is null)
            {
                diagnostics.ReportOnce("prometheus-ipv6-host", DiagnosticLevel.Error,
                    $"{variable} is an IPv6 address, which the .NET HttpListener cannot bind: no Prometheus listener runs (not widened to every interface); use an IPv4 address or localhost, or leave it unset (every IPv4 interface)",
                    ("variable", variable), ("value", resolved.PrometheusMetricsHost));
                return;
            }

            if (resolved.PrometheusMetricsHost.Contains(':'))
            {
                diagnostics.ReportOnce("prometheus-ipv6-loopback", DiagnosticLevel.Warn,
                    $"{variable} is the IPv6 loopback, which the .NET HttpListener cannot bind: the listener binds 127.0.0.1 (and answers requests for localhost) instead",
                    ("variable", variable), ("value", resolved.PrometheusMetricsHost));
            }
            else if (resolved.PrometheusHostIPv6Unspecified)
            {
                diagnostics.ReportOnce("prometheus-ipv6-any", DiagnosticLevel.Warn,
                    $"{variable} is the IPv6 unspecified address; .NET has no IPv6 or dual-stack bind: the listener binds every IPv4 interface",
                    ("variable", variable));
            }

            try
            {
                builder.AddPrometheusHttpListener(ListenerName, null);
            }
            // Only the listener start (measured on .NET 8.0.33 and 10.0.14: a busy port and an address
            // this machine does not own both surface as HttpListenerException, errno 98 and 99, wrapped
            // by the exporter); any other failure of the reader factory propagates without this line.
            catch (InvalidOperationException ex) when (ex.InnerException is System.Net.HttpListenerException listenerError)
            {
                var host = resolved.PrometheusBindHost.Length == 0 ? "every IPv4 interface" : resolved.PrometheusBindHost;
                var hint = resolved.PrometheusMetricsHost.Length > 0
                    ? $"the port set by {resolved.PrometheusPortSource} or the host set by {variable}"
                    : $"the port set by {resolved.PrometheusPortSource}";
                diagnostics.ReportOnce("prometheus-bind-failed", DiagnosticLevel.Error,
                    $"the Prometheus listener cannot bind (port in use, or an address this machine does not own); change {hint}; the MeterProvider build fails",
                    ("host", host), ("port", resolved.PrometheusMetricsPort.ToString(System.Globalization.CultureInfo.InvariantCulture)),
                    ("error", listenerError.Message));
                throw;
            }
        }

        /// <summary>A push reader the helper builds itself, recorded for <c>ForceFlushOtelHelper</c>.</summary>
        private static MetricReader Track(IServiceProvider sp, MetricReader reader)
        {
            sp.GetService<HelperMetricReaders>()?.Add(reader);
            return reader;
        }

        /// <summary>
        /// The push metric readers the helper built for one container (OTLP and console), which
        /// <c>ForceFlushOtelHelper</c> collects one by one. The SDK-built reader of the histogram
        /// path is not among them (the SDK gives no handle to it).
        /// </summary>
        internal sealed class HelperMetricReaders
        {
            private readonly List<MetricReader> _readers = new();

            internal void Add(MetricReader reader)
            {
                lock (_readers)
                {
                    _readers.Add(reader);
                }
            }

            internal IReadOnlyList<MetricReader> Snapshot()
            {
                lock (_readers)
                {
                    return _readers.ToArray();
                }
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

        /// <summary>
        /// The console metric reader: the resolved interval and the default <c>MetricReaderOptions</c>
        /// temporality, as upstream's <c>AddConsoleExporter</c> reads them.
        /// </summary>
        internal static PeriodicExportingMetricReader CreateConsoleReader(IServiceProvider sp, ResolvedTelemetry resolved)
        {
            var readerOptions = sp.GetRequiredService<IOptionsMonitor<MetricReaderOptions>>().Get(Options.DefaultName);
            return new PeriodicExportingMetricReader(
                new ConsoleMetricExporter(new ConsoleExporterOptions()),
                (int)Math.Min(resolved.ExportIntervalMs, int.MaxValue))
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
        /// The contract glob of <c>OTEL_HELPER_DISABLED_METRICS</c> (D1 row "Disabled-metric
        /// patterns", A21): comma-separated items trimmed with the contract set, blank items
        /// ignored; each item a <b>case-sensitive</b> match of the whole instrument name where
        /// <c>*</c> is any run of characters (<c>.</c> and <c>/</c> included), <c>?</c> exactly one
        /// character, and every other character literal (no classes, no escapes).
        /// </summary>
        internal static Regex[] DisabledMetricPatterns(string list)
            => list.Split(',')
                .Select(ContractText.Trim)
                .Where(item => item.Length > 0)
                .Select(item => new Regex(
                    "^" + Regex.Escape(item).Replace("\\*", ".*").Replace("\\?", ".") + "\\z",
                    RegexOptions.Compiled | RegexOptions.CultureInvariant | RegexOptions.Singleline))
                .ToArray();

        /// <summary>
        /// Prefixes the Prometheus listener binds, path <c>/metrics/</c> (design D-NET11, task C12).
        /// Every interface (<paramref name="host"/> <c>""</c>, or <c>0.0.0.0</c>): <c>+</c>, which the
        /// managed <c>HttpListener</c> binds to <c>IPAddress.Any</c> (every IPv4 interface; it refuses
        /// a <c>0.0.0.0</c> prefix and every IPv6 literal). An IPv4 literal: that address only. The
        /// listener routes a specific-host prefix by the request's <c>Host</c> header, so
        /// <c>127.0.0.1</c> also gets a <c>localhost</c> prefix (fix round 1): <c>localhost</c> binds
        /// what the resolver returns first (<c>::1</c> on a dual-stack host, <c>127.0.0.1</c> with
        /// IPv6 off, measured), loopback either way.
        /// </summary>
        internal static string[] PrometheusListenerPrefixes(int port, string host = "")
            => host switch
            {
                "" or "0.0.0.0" => new[] { $"http://+:{port}/metrics/" },
                "127.0.0.1" => new[] { $"http://127.0.0.1:{port}/metrics/", $"http://localhost:{port}/metrics/" },
                _ => new[] { $"http://{host}:{port}/metrics/" },
            };

        /// <summary>
        /// The listener the helper registers: its port and host, and a fresh collection on every
        /// scrape. This delegate runs after the options factory has read
        /// <c>OTEL_EXPORTER_PROMETHEUS_HOST</c>/<c>_PORT</c> itself, so the values set here (the
        /// helper's resolution of the same variables, A29) win.
        /// </summary>
        internal static void ConfigurePrometheusListener(PrometheusHttpListenerOptions opts, int port, string host = "")
        {
            // A valid host for the exporter's own prefix, which the delegate below
            // replaces; an env-provided "*" would otherwise abort the build.
            opts.Host = "localhost";
            opts.Port = port;
            // 0.2.x (1.9.0-beta.2) had no response cache; upstream now defaults to 300 ms.
            opts.ScrapeResponseCacheDurationMilliseconds = 0;
            var prefixes = PrometheusListenerPrefixes(port, host);
            opts.ConfigureHttpListener = (_, listener) =>
            {
                // Clear first: keeping the localhost prefix next to "+" fails on
                // Linux with "Address already in use" (exporter README).
                listener.Prefixes.Clear();
                foreach (var prefix in prefixes)
                {
                    listener.Prefixes.Add(prefix);
                }
            };
        }
    }
}
