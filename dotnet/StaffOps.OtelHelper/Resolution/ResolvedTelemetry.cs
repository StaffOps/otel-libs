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

        /// <summary>Generic endpoint after resolution; empty when none is configured.</summary>
        public required string OtelCollectorEndpoint { get; init; }

        /// <summary>Code per-signal endpoints (used as written), by signal name.</summary>
        public required IReadOnlyDictionary<string, string> SignalEndpoints { get; init; }

        /// <summary>Protocol set in code or a valid <c>OTEL_EXPORTER_OTLP_PROTOCOL</c>; null = inferred per endpoint.</summary>
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
        public string ResolvedOtlpProtocol() => ProtocolFor(OtelCollectorEndpoint);

        /// <summary>Protocol for an endpoint: explicit, else port 4318 → http/protobuf, else grpc.</summary>
        internal string ProtocolFor(string endpoint)
        {
            if (OtlpProtocol is not null)
            {
                return OtlpProtocol;
            }

            return endpoint.Length > 0
                && Uri.TryCreate(endpoint, UriKind.Absolute, out var uri)
                && uri.Port == TelemetryOptions.DefaultOtlpHttpPort
                ? TelemetryOptions.ProtocolHttpProtobuf
                : TelemetryOptions.ProtocolGrpc;
        }

        /// <summary>
        /// The OTLP target of one signal: a code per-signal endpoint as written, else the generic
        /// endpoint (HTTP/protobuf appends <c>/v1/&lt;signal&gt;</c>); null when none is configured.
        /// </summary>
        internal (string Endpoint, string Protocol)? OtlpTarget(string signal)
        {
            if (SignalEndpoints.TryGetValue(signal, out var perSignal))
            {
                return (perSignal, ProtocolFor(perSignal));
            }

            if (OtelCollectorEndpoint.Length == 0)
            {
                return null;
            }

            var protocol = ResolvedOtlpProtocol();
            var endpoint = protocol == TelemetryOptions.ProtocolHttpProtobuf
                ? OtelCollectorEndpoint.TrimEnd('/') + "/v1/" + signal
                : OtelCollectorEndpoint;
            return (endpoint, protocol);
        }
    }
}
