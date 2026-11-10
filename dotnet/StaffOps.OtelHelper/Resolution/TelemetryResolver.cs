using System.Globalization;
using Microsoft.Extensions.Configuration;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// Builds <see cref="ResolvedTelemetry"/> from the code options and the process
    /// environment (ADR-013: helper-owned rows never come from <c>IConfiguration</c>;
    /// <c>OTEL_SDK_DISABLED</c> is read where the SDK reads it). Precedence: code &gt;
    /// standard <c>OTEL_*</c> &gt; <c>OTEL_HELPER_*</c>/legacy &gt; default. An environment
    /// value the helper cannot use is warned through the diagnostics sink and replaced by the
    /// default (design D-NET6); code values were validated before (they fail at start).
    /// </summary>
    internal static class TelemetryResolver
    {
        internal const string DefaultServiceName = "my-service";

        /// <summary>Health paths whose spans are not recorded when <see cref="TelemetryOptions.HealthPaths"/> is not set.</summary>
        internal static readonly string[] DefaultHealthPaths = { "/ping", "/health", "/healthz", "/ready" };

        internal static ResolvedTelemetry Resolve(TelemetryOptions code, IOtelHelperDiagnostics diagnostics, IConfiguration? configuration = null)
        {
            WarnUnsupported(code, diagnostics);

            var insecure = code.Insecure ?? EnvTrue(TelemetryOptions.InsecureEnvVar);
            var endpoint = ResolveGenericEndpoint(code, insecure, diagnostics);
            var signalEndpoints = new Dictionary<string, string>(StringComparer.Ordinal);
            AddIfSet(signalEndpoints, "traces", code.TracesEndpoint);
            AddIfSet(signalEndpoints, "metrics", code.MetricsEndpoint);
            AddIfSet(signalEndpoints, "logs", code.LogsEndpoint);

            var environment = NonBlank(code.Environment) ?? ParseEnvironment(Env(TelemetryOptions.EnvironmentEnvVar));
            var debug = code.DebugLevel ?? EnvTrue(TelemetryOptions.DebugLevelEnvVar);
            var sdkDisabled = ReadSdkDisabled(configuration);
            if (sdkDisabled)
            {
                diagnostics.ReportOnce("info:" + TelemetryOptions.SdkDisabledEnvVar, DiagnosticLevel.Info,
                    "OTEL_SDK_DISABLED=true: telemetry disabled, only propagators installed");
            }

            var hasMetricsEndpoint = endpoint.Length > 0 || signalEndpoints.ContainsKey("metrics");
            return new ResolvedTelemetry
            {
                ServiceName = NonBlank(code.ServiceName)
                    ?? NonBlank(Env(TelemetryOptions.ServiceNameEnvVar))
                    ?? NonBlank(Env(TelemetryOptions.OtelServiceNameEnvVar))
                    ?? DefaultServiceName,
                Environment = environment,
                OtelCollectorEndpoint = endpoint,
                SignalEndpoints = signalEndpoints,
                OtlpProtocol = ResolveProtocol(code, diagnostics),
                DebugLevel = debug,
                ExportTimeoutMs = code.ExportTimeoutMs ?? TelemetryOptions.DefaultExportTimeoutMs,
                Compression = ResolveCompression(code, diagnostics),
                Headers = NonBlank(Env("OTEL_EXPORTER_OTLP_HEADERS")),
                CertificatePath = NonBlank(Env("OTEL_EXPORTER_OTLP_CERTIFICATE")),
                ClientCertificatePath = NonBlank(Env("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE")),
                ClientKeyPath = NonBlank(Env("OTEL_EXPORTER_OTLP_CLIENT_KEY")),
                Sampler = ResolveSampler(code),
                MinimumLogLevel = code.MinimumLogLevel ?? TelemetryOptions.GetDefaultLogLevel(environment, debug),
                ResourceAttributes = new Dictionary<string, object>(code.ResourceAttributes),
                AdditionalActivitySources = code.AdditionalActivitySources.ToArray(),
                AdditionalMeters = code.AdditionalMeters.ToArray(),
                DisabledSignals = NonBlank(code.DisabledSignals) ?? NonBlank(Env(TelemetryOptions.DisabledSignalsEnvVar)) ?? "",
                DisabledMetrics = NonBlank(code.DisabledMetrics) ?? NonBlank(Env(TelemetryOptions.DisabledMetricsEnvVar)) ?? "",
                MetricExporters = ResolveMetricExporters(code, hasMetricsEndpoint, diagnostics),
                ExportIntervalMs = code.ExportIntervalMs ?? ResolveEnvInterval(),
                PrometheusMetricsPort = code.PrometheusMetricsPort ?? ResolveEnvPort(diagnostics),
                HealthPaths = code.HealthPaths?.ToArray() ?? DefaultHealthPaths,
                SdkDisabled = sdkDisabled,
                ForwardSdkDiagnostics = code.ForwardSdkDiagnostics,
                MetricTemporality = ResolveTemporality(configuration, diagnostics, out var temporalityConfigured),
                MetricTemporalityConfigured = temporalityConfigured,
            };
        }

        // ── identity ───────────────────────────────────────────────────────

        /// <summary><c>ENVIRONMENT</c>: a known name (case-insensitive, <c>-</c> read as <c>_</c>) in its canonical spelling, else <c>LOCAL</c>.</summary>
        private static string ParseEnvironment(string? raw)
        {
            var value = NonBlank(raw)?.Replace("-", "_");
            return DeploymentEnvironments.Known.FirstOrDefault(k => k.Equals(value, StringComparison.OrdinalIgnoreCase))
                ?? DeploymentEnvironments.LOCAL;
        }

        // ── endpoints and exporter settings ────────────────────────────────

        private static string ResolveGenericEndpoint(TelemetryOptions code, bool insecure, IOtelHelperDiagnostics diagnostics)
        {
            if (NonBlank(code.OtelCollectorEndpoint) is { } fromCode)
            {
                // An http(s) URL set in code is used as written; a schemeless host:port is
                // resolved like the environment value (N10: it is not a URL with scheme "collector").
                var trimmed = fromCode.Trim();
                return IsHttpUrl(trimmed) ? trimmed : ResolveEndpoint(trimmed.TrimEnd('/'), insecure);
            }

            var fromEnv = Env(TelemetryOptions.CollectorEndpointEnvVar);
            if (string.IsNullOrWhiteSpace(fromEnv))
            {
                return "";
            }

            if (fromEnv.Contains("://", StringComparison.Ordinal) && !IsHttpUrl(fromEnv.Trim().TrimEnd('/')))
            {
                // A URL the helper cannot use (unsupported scheme, port out of range, no host):
                // before, it was rewritten as a schemeless value ("https://http://…"). The full
                // parsing rules are task C7a; the value is rendered without credentials.
                diagnostics.ReportOnce("invalid:" + TelemetryOptions.CollectorEndpointEnvVar, DiagnosticLevel.Warn,
                    $"invalid {TelemetryOptions.CollectorEndpointEnvVar}: not a usable http:// or https:// URL; no OTLP endpoint is configured",
                    ("variable", TelemetryOptions.CollectorEndpointEnvVar), ("value", DiagnosticText.RenderUrl(fromEnv)));
                return "";
            }

            return ResolveEndpoint(fromEnv.TrimEnd('/'), insecure);
        }

        /// <summary>An absolute <c>http</c>/<c>https</c> URL with a host.</summary>
        internal static bool IsHttpUrl(string value)
            => Uri.TryCreate(value.Trim(), UriKind.Absolute, out var uri)
                && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps)
                && uri.Host.Length > 0;

        /// <summary>
        /// Resolves an endpoint string to <c>scheme://host:port</c> (the 0.2 rule, kept until task C7a):
        /// an http(s) URL keeps its scheme and gets port 4317 when it has none; a schemeless
        /// value gets <c>http://</c> when <paramref name="insecure"/>, else <c>https://</c>, and
        /// port 4317 when it has none.
        /// </summary>
        internal static string ResolveEndpoint(string rawEndpoint, bool? insecure = null)
        {
            if (Uri.TryCreate(rawEndpoint, UriKind.Absolute, out var uri)
                && (uri.Scheme == "http" || uri.Scheme == "https"))
            {
                var port = uri.IsDefaultPort ? 4317 : uri.Port;
                return $"{uri.Scheme}://{uri.Host}:{port}";
            }

            var scheme = (insecure ?? EnvTrue(TelemetryOptions.InsecureEnvVar)) ? "http" : "https";
            var hostPort = rawEndpoint;
            var resolvedPort = 4317;
            var lastColon = hostPort.LastIndexOf(':');
            if (lastColon > 0 && int.TryParse(hostPort.AsSpan(lastColon + 1), NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsedPort))
            {
                resolvedPort = parsedPort;
                hostPort = hostPort[..lastColon];
            }

            return $"{scheme}://{hostPort}:{resolvedPort}";
        }

        private static string? ResolveProtocol(TelemetryOptions code, IOtelHelperDiagnostics diagnostics)
        {
            if (NonBlank(code.OtlpProtocol) is { } fromCode)
            {
                return fromCode.Trim().ToLowerInvariant();
            }

            var fromEnv = NonBlank(Env(TelemetryOptions.OtlpProtocolEnvVar));
            if (fromEnv is null)
            {
                return null;
            }

            var normalized = fromEnv.Trim().ToLowerInvariant();
            if (TelemetryOptions.ValidOtlpProtocols.Contains(normalized))
            {
                return normalized;
            }

            WarnInvalidEnv(diagnostics, TelemetryOptions.OtlpProtocolEnvVar, fromEnv,
                "unsupported OTLP protocol, using grpc (http/protobuf on port 4318); valid values: grpc, http/protobuf");
            return null;
        }

        private static string ResolveCompression(TelemetryOptions code, IOtelHelperDiagnostics diagnostics)
        {
            if (NonBlank(code.Compression) is { } fromCode)
            {
                return fromCode.Trim().ToLowerInvariant();
            }

            var fromEnv = NonBlank(Env(TelemetryOptions.CompressionEnvVar));
            if (fromEnv is null)
            {
                return TelemetryOptions.CompressionNone;
            }

            var normalized = fromEnv.Trim().ToLowerInvariant();
            if (TelemetryOptions.ValidCompressions.Contains(normalized))
            {
                return normalized;
            }

            WarnInvalidEnv(diagnostics, TelemetryOptions.CompressionEnvVar, fromEnv,
                "unsupported compression, using none; valid values: gzip, none");
            return TelemetryOptions.CompressionNone;
        }

        // ── sampler ────────────────────────────────────────────────────────

        /// <summary>
        /// Code <see cref="TelemetryOptions.Sampler"/> &gt; code <see cref="TelemetryOptions.SampleRatio"/>
        /// (ParentBased) &gt; <c>OTEL_TRACES_SAMPLER</c> (null: the SDK applies it) &gt;
        /// <c>OTEL_HELPER_SAMPLE_RATIO</c> (the 0.2 rule until task C10) &gt; always on.
        /// </summary>
        private static Sampler? ResolveSampler(TelemetryOptions code)
        {
            if (code.Sampler is not null)
            {
                return code.Sampler;
            }

            if (code.SampleRatio is { } ratio)
            {
                return new ParentBasedSampler(new TraceIdRatioBasedSampler(ratio));
            }

            if (!string.IsNullOrWhiteSpace(Env(TelemetryOptions.TracesSamplerEnvVar)))
            {
                return null;
            }

            var ratioText = Env(TelemetryOptions.SampleRatioEnvVar);
            if (!string.IsNullOrWhiteSpace(ratioText)
                && double.TryParse(ratioText, NumberStyles.Float, CultureInfo.InvariantCulture, out var envRatio))
            {
                envRatio = Math.Clamp(envRatio, 0.0, 1.0);
                if (envRatio < 1.0)
                {
                    return new TraceIdRatioBasedSampler(envRatio);
                }
            }

            return new AlwaysOnSampler();
        }

        // ── metrics ────────────────────────────────────────────────────────

        private static IReadOnlyList<string> ResolveMetricExporters(TelemetryOptions code, bool hasEndpoint, IOtelHelperDiagnostics diagnostics)
        {
            if (NonBlank(code.MetricExporters) is { } fromCode)
            {
                // Validated at start: known names only, "none" alone, "otlp" with an endpoint.
                var list = SplitLower(fromCode);
                return list.Contains("none") ? Array.Empty<string>() : list.Distinct().ToArray();
            }

            var fromEnv = NonBlank(Env(TelemetryOptions.MetricsExporterEnvVar));
            if (fromEnv is null)
            {
                return hasEndpoint ? new[] { "otlp" } : new[] { "prometheus" };
            }

            var entries = SplitLower(fromEnv);
            var known = entries.Where(e => TelemetryOptions.ValidMetricExporters.Contains(e)).Distinct().ToList();
            if (known.Count < entries.Length)
            {
                WarnInvalidEnv(diagnostics, TelemetryOptions.MetricsExporterEnvVar, fromEnv,
                    "unknown metric exporter ignored; valid values: otlp, prometheus, none");
            }

            if (known.Contains("none"))
            {
                if (known.Count > 1)
                {
                    WarnInvalidEnv(diagnostics, TelemetryOptions.MetricsExporterEnvVar, fromEnv,
                        "'none' combined with other exporters: metrics are not exported");
                }

                return Array.Empty<string>();
            }

            if (known.Count == 0)
            {
                return hasEndpoint ? new[] { "otlp" } : new[] { "prometheus" };
            }

            if (known.Contains("otlp") && !hasEndpoint)
            {
                diagnostics.ReportOnce("otlp-without-endpoint:" + TelemetryOptions.MetricsExporterEnvVar, DiagnosticLevel.Warn,
                    "metric exporter 'otlp' has no endpoint and is ignored; set OTEL_EXPORTER_OTLP_ENDPOINT",
                    ("variable", TelemetryOptions.MetricsExporterEnvVar));
                known.Remove("otlp");
            }

            return known;
        }

        private static long ResolveEnvInterval()
        {
            var text = Env(TelemetryOptions.MetricExportIntervalEnvVar);
            return !string.IsNullOrWhiteSpace(text) && int.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out var interval) && interval > 0
                ? interval
                : TelemetryOptions.DefaultExportIntervalMs;
        }

        private static int ResolveEnvPort(IOtelHelperDiagnostics diagnostics)
        {
            var text = Env(TelemetryOptions.MetricsPortEnvVar);
            if (string.IsNullOrWhiteSpace(text) || !int.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out var port))
            {
                return TelemetryOptions.DefaultPrometheusMetricsPort;
            }

            if (port is < 0 or > 65535)
            {
                WarnInvalidEnv(diagnostics, TelemetryOptions.MetricsPortEnvVar, text, "port outside [0, 65535], using 9464");
                return TelemetryOptions.DefaultPrometheusMetricsPort;
            }

            return port;
        }

        internal const string TemporalityVariable = "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE";

        /// <summary>
        /// The OTLP metric temporality, from the source the SDK reads (the application's
        /// <c>IConfiguration</c>, else the environment): <c>cumulative</c>, <c>delta</c> or
        /// <c>lowmemory</c>, case-insensitive; anything else warns and is cumulative. 0.2.x got
        /// it through <c>AddOtlpExporter</c>; a directly constructed reader does not (ADR-013).
        /// </summary>
        private static OpenTelemetry.Metrics.MetricReaderTemporalityPreference ResolveTemporality(IConfiguration? configuration, IOtelHelperDiagnostics diagnostics, out bool configured)
        {
            var raw = NonBlank(configuration is not null ? configuration[TemporalityVariable] : Env(TemporalityVariable));
            configured = true;
            switch (raw?.Trim().ToLowerInvariant())
            {
                case "cumulative":
                    return OpenTelemetry.Metrics.MetricReaderTemporalityPreference.Cumulative;
                case "delta":
                    return OpenTelemetry.Metrics.MetricReaderTemporalityPreference.Delta;
                case "lowmemory":
                    return OpenTelemetry.Metrics.MetricReaderTemporalityPreference.LowMemory;
                case null:
                    configured = false;
                    return OpenTelemetry.Metrics.MetricReaderTemporalityPreference.Cumulative;
                default:
                    // Names only: upstream's Enum.TryParse also took a number; the helper warns.
                    configured = false;
                    WarnInvalidEnv(diagnostics, TemporalityVariable, raw, "unsupported temporality, using cumulative; valid values: cumulative, delta, lowmemory");
                    return OpenTelemetry.Metrics.MetricReaderTemporalityPreference.Cumulative;
            }
        }

        internal const string HistogramAggregationVariable = "OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION";

        // ── SDK-owned and unsupported rows ─────────────────────────────────

        /// <summary>
        /// <c>OTEL_SDK_DISABLED</c> from the same source the SDK reads (the application's
        /// <c>IConfiguration</c>, else the environment), so the INFO line and the resolution
        /// agree with what the SDK does (ADR-013).
        /// </summary>
        private static bool ReadSdkDisabled(IConfiguration? configuration)
        {
            var raw = configuration is not null ? configuration[TelemetryOptions.SdkDisabledEnvVar] : Env(TelemetryOptions.SdkDisabledEnvVar);
            return bool.TryParse(raw?.Trim(), out var disabled) && disabled;
        }

        private static void WarnUnsupported(TelemetryOptions code, IOtelHelperDiagnostics diagnostics)
        {
            foreach (var variable in new[] { TelemetryOptions.ConfigFileEnvVar, TelemetryOptions.ExperimentalConfigFileEnvVar })
            {
                if (!string.IsNullOrWhiteSpace(Env(variable)))
                {
                    // Contract D1 row "OTEL_CONFIG_FILE" (A19): the name only, never the path.
                    diagnostics.ReportOnce("unused:" + variable, DiagnosticLevel.Warn,
                        $"{variable} is not supported by the .NET helper and is ignored; configure it with environment variables or TelemetryOptions",
                        ("variable", variable));
                }
            }

#pragma warning disable CS0618 // the obsolete option is read only to say it has no effect
            if (code.ExtraInstrumentation is not null)
#pragma warning restore CS0618
            {
                diagnostics.ReportOnce("unused:TelemetryOptions.ExtraInstrumentation", DiagnosticLevel.Warn,
                    "TelemetryOptions.ExtraInstrumentation has no effect in .NET; add the opt-in packages (StaffOps.OtelHelper.Sql, .AWS, .Redis) instead");
            }
        }

        private static void WarnInvalidEnv(IOtelHelperDiagnostics diagnostics, string variable, string value, string why)
            => diagnostics.ReportOnce($"invalid:{variable}:{value}", DiagnosticLevel.Warn, $"invalid {variable}: {why}",
                ("variable", variable), ("value", value));

        // ── small helpers ──────────────────────────────────────────────────

        private static string? Env(string name) => System.Environment.GetEnvironmentVariable(name);

        private static bool EnvTrue(string name) => string.Equals(Env(name)?.Trim(), "true", StringComparison.OrdinalIgnoreCase);

        private static string? NonBlank(string? value) => string.IsNullOrWhiteSpace(value) ? null : value;

        private static string[] SplitLower(string value)
            => value.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Select(e => e.ToLowerInvariant())
                .ToArray();

        private static void AddIfSet(Dictionary<string, string> endpoints, string signal, string? value)
        {
            if (NonBlank(value) is { } set)
            {
                endpoints[signal] = set.Trim();
            }
        }
    }
}
