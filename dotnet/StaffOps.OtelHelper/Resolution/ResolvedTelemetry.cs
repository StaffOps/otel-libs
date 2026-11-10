using Microsoft.Extensions.Logging;
using OpenTelemetry.Trace;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// The values the helper acts on, resolved once per service provider from the code
    /// options (<c>IOptions&lt;TelemetryOptions&gt;</c>) and the process environment
    /// (design D-NET3, D-NET4). Every deferred configuration step reads this singleton,
    /// so a <c>Configure&lt;TelemetryOptions&gt;</c> registered after <c>AddOtelHelper</c>
    /// still applies. Internal: the parity driver and the tests read it through
    /// <c>InternalsVisibleTo</c>.
    /// </summary>
    internal sealed class ResolvedTelemetry
    {
        internal static readonly string[] Signals = { "traces", "metrics", "logs" };

        /// <summary>The resolved <c>service.name</c>, or <c>unknown_service:&lt;process&gt;</c> when none is configured.</summary>
        public required string ServiceName { get; init; }

        /// <summary>False when no level provided a service name (the SDK default is used, warned).</summary>
        public bool ServiceNameConfigured { get; init; } = true;

        /// <summary>The resolved <c>deployment.environment.name</c> as emitted; null = absent (ADR-003).</summary>
        public required string? Environment { get; init; }

        /// <summary>The <c>service.instance.id</c>: code, else <c>OTEL_RESOURCE_ATTRIBUTES</c>, else one id per process (D4).</summary>
        public string ServiceInstanceId { get; init; } = ResourceSetup.ProcessInstanceId;

        /// <summary>True when the instance id came from code or <c>OTEL_RESOURCE_ATTRIBUTES</c>, false when it is the per-process id.</summary>
        public bool ServiceInstanceIdConfigured { get; init; }

        /// <summary><see cref="TelemetryOptions.MinimumLogLevel"/> as set in code; null = derived from the environment.</summary>
        public LogLevel? CodeMinimumLogLevel { get; init; }

        /// <summary><c>OTEL_RESOURCE_ATTRIBUTES</c> as the helper parsed it (A16).</summary>
        public IReadOnlyDictionary<string, string> EnvResourceAttributes { get; init; } = ParsedResourceAttributes.Empty.Attributes;

        /// <summary>
        /// The generic endpoint (code, else <c>OTEL_EXPORTER_OTLP_ENDPOINT</c>) after resolution, before
        /// any <c>/v1/&lt;signal&gt;</c>; empty when none is usable. Reporting only: exporters use <see cref="SignalTargets"/>.
        /// </summary>
        public required string OtelCollectorEndpoint { get; init; }

        /// <summary>The protocol of <see cref="OtelCollectorEndpoint"/> (explicit, else port-4318 inference, else grpc).</summary>
        public string GenericOtlpProtocol { get; init; } = TelemetryOptions.ProtocolGrpc;

        /// <summary>The OTLP target of each configured signal (contract D1); a missing signal has no OTLP exporter.</summary>
        public required IReadOnlyDictionary<string, OtlpSignalTarget> SignalTargets { get; init; }

        /// <summary>Protocol set in code or a valid <c>OTEL_EXPORTER_OTLP_PROTOCOL</c>; null = per signal.</summary>
        public string? OtlpProtocol { get; init; }

        public required bool DebugLevel { get; init; }

        public required int ExportTimeoutMs { get; init; }

        /// <summary><c>gzip</c> or <c>none</c>.</summary>
        public required string Compression { get; init; }

        /// <summary>The generic <c>OTEL_EXPORTER_OTLP_HEADERS</c> value (process environment); never logged.</summary>
        public string? Headers { get; init; }

        public string? CertificatePath { get; init; }

        public string? ClientCertificatePath { get; init; }

        public string? ClientKeyPath { get; init; }

        /// <summary>The sampler the helper sets; null leaves the SDK's <c>OTEL_TRACES_SAMPLER</c> handling in place.</summary>
        public Sampler? Sampler { get; init; }

        public required LogLevel MinimumLogLevel { get; init; }

        public required IReadOnlyDictionary<string, object> ResourceAttributes { get; init; }

        public required IReadOnlyList<string> AdditionalActivitySources { get; init; }

        public required IReadOnlyList<string> AdditionalMeters { get; init; }

        public required string DisabledSignals { get; init; }

        public required string DisabledMetrics { get; init; }

        /// <summary>Active metric exporters (<c>otlp</c>, <c>prometheus</c>); empty = no exporter.</summary>
        public required IReadOnlyList<string> MetricExporters { get; init; }

        public required long ExportIntervalMs { get; init; }

        public required int PrometheusMetricsPort { get; init; }

        public required IReadOnlyList<string> HealthPaths { get; init; }

        /// <summary><c>OTEL_SDK_DISABLED</c> as the SDK reads it (<c>IConfiguration</c>).</summary>
        public required bool SdkDisabled { get; init; }

        public required bool ForwardSdkDiagnostics { get; init; }

        /// <summary><c>OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE</c>, read from <c>IConfiguration</c> as the SDK reads it.</summary>
        public OpenTelemetry.Metrics.MetricReaderTemporalityPreference MetricTemporality { get; init; }

        /// <summary>Whether the temporality variable held a valid value (only then is it applied, so a code <c>Configure&lt;MetricReaderOptions&gt;</c> wins).</summary>
        public bool MetricTemporalityConfigured { get; init; }

        /// <summary>True unless the signal is listed in the disabled signals.</summary>
        public bool IsSignalEnabled(string signal)
            => !DisabledSignals.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Any(s => s.Equals(signal, StringComparison.OrdinalIgnoreCase));

        /// <summary>Metric exporters in effect: none when metrics are disabled or the SDK is.</summary>
        public IReadOnlyList<string> ResolvedMetricExporters()
            => IsSignalEnabled("metrics") && !SdkDisabled ? MetricExporters : Array.Empty<string>();

        /// <summary>Whether the Prometheus listener binds: the exporter is active and the port is above 0.</summary>
        public bool PrometheusListenerActive
            => PrometheusMetricsPort > 0 && ResolvedMetricExporters().Contains("prometheus");

        /// <summary>Protocol of the generic endpoint: explicit, else port 4318 → http/protobuf, else grpc.</summary>
        public string ResolvedOtlpProtocol() => GenericOtlpProtocol;

        /// <summary>The OTLP target of one signal (URL and protocol); null when the signal has none.</summary>
        internal (string Endpoint, string Protocol)? OtlpTarget(string signal)
            => SignalTargets.TryGetValue(signal, out var target) ? (target.Endpoint, target.Protocol) : null;
    }
}

namespace StaffOps.OtelHelper
{
    /// <summary>The resolved OTLP target of one signal.</summary>
    /// <param name="Endpoint">The URL passed to the exporter: <c>scheme://host:port</c> for gRPC; for HTTP/protobuf the full URL (a generic value gets <c>/v1/&lt;signal&gt;</c>).</param>
    /// <param name="Protocol"><c>grpc</c> or <c>http/protobuf</c>.</param>
    /// <param name="Variable">The option or environment variable the endpoint came from.</param>
    /// <param name="PerSignal">Whether it came from a per-signal option or variable.</param>
    internal sealed record OtlpSignalTarget(string Endpoint, string Protocol, string Variable, bool PerSignal);
}
