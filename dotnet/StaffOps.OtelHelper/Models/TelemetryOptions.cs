using Microsoft.Extensions.Logging;
using OpenTelemetry.Trace;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// Code options of StaffOps.OtelHelper. Every option that has an environment
    /// counterpart is nullable: <c>null</c> means "not set in code", and a value set
    /// in code wins over the environment whatever it is (also when it equals the
    /// helper default). A blank string counts as not set, except for <see cref="ServiceName"/>
    /// and <see cref="Environment"/>, where it fails host start (as in Go and Python), and for
    /// <see cref="DisabledSignals"/> and <see cref="MetricExporters"/>, where it is a set empty list. The values the helper
    /// actually uses are resolved internally from these options and the process
    /// environment; this type holds only what the application set.
    /// A section bound from <c>IConfiguration</c> into these options counts as code.
    /// </summary>
    public class TelemetryOptions
    {
        /// <summary>Environment variable name: OTLP collector endpoint for every signal. Unset: metrics fall back to the Prometheus <c>/metrics</c> endpoint and logs to stdout.</summary>
        public const string CollectorEndpointEnvVar = "OTEL_EXPORTER_OTLP_ENDPOINT";
        /// <summary>Environment variable name: Deprecated service name variable, read after <see cref="OtelServiceNameEnvVar"/> and <c>service.name</c> in <c>OTEL_RESOURCE_ATTRIBUTES</c>.</summary>
        public const string ServiceNameEnvVar = "SERVICE_NAME";
        /// <summary>Environment variable name: Standard OpenTelemetry service name variable; wins over <c>OTEL_RESOURCE_ATTRIBUTES</c> and <see cref="ServiceNameEnvVar"/>.</summary>
        public const string OtelServiceNameEnvVar = "OTEL_SERVICE_NAME";
        /// <summary>Environment variable name: Deprecated alias of <c>deployment.environment.name</c> in <c>OTEL_RESOURCE_ATTRIBUTES</c>, which wins; see <see cref="DeploymentEnvironments"/>.</summary>
        public const string EnvironmentEnvVar = "ENVIRONMENT";
        /// <summary>Environment variable name: Debug mode switch (<c>true</c>/<c>false</c>); see <see cref="DebugLevel"/>.</summary>
        public const string DebugLevelEnvVar = "OTEL_HELPER_DEBUG_LEVEL";
        /// <summary>Environment variable name: Extra instrumentations; it has no effect in .NET.</summary>
        public const string ExtraInstrumentationEnvVar = "OTEL_HELPER_EXTRA_INSTRUMENTATION";
        /// <summary>Environment variable name: Deprecated trace sample ratio, a plain decimal in [0, 1] giving <c>parentbased_traceidratio</c>; read only when no sampler or ratio is set in code and <see cref="TracesSamplerEnvVar"/> is unset; another value is warned and ignored.</summary>
        public const string SampleRatioEnvVar = "OTEL_HELPER_SAMPLE_RATIO";
        /// <summary>Environment variable name: Comma-separated signals to disable (<c>traces</c>, <c>metrics</c>, <c>logs</c>); see <see cref="DisabledSignals"/>.</summary>
        public const string DisabledSignalsEnvVar = "OTEL_HELPER_DISABLED_SIGNALS";
        /// <summary>Environment variable name: Comma-separated instrument names or wildcard patterns to drop; see <see cref="DisabledMetrics"/>.</summary>
        public const string DisabledMetricsEnvVar = "OTEL_HELPER_DISABLED_METRICS";
        /// <summary>Environment variable name: Standard OpenTelemetry variable; <c>true</c> makes a schemeless endpoint use plaintext instead of TLS.</summary>
        public const string InsecureEnvVar = "OTEL_EXPORTER_OTLP_INSECURE";
        /// <summary>Environment variable name: Port of the standalone Prometheus <c>/metrics</c> listener; see <see cref="PrometheusMetricsPort"/>.</summary>
        public const string MetricsPortEnvVar = "OTEL_HELPER_METRICS_PORT";
        /// <summary>Environment variable name: Standard OpenTelemetry metric exporter list (<c>otlp</c>, <c>prometheus</c>, <c>console</c>, <c>none</c>; <c>logging</c> is a warned alias of <c>console</c>); see <see cref="MetricExporters"/>.</summary>
        public const string MetricsExporterEnvVar = "OTEL_METRICS_EXPORTER";
        /// <summary>Environment variable name: Standard OpenTelemetry metric export interval in milliseconds; see <see cref="ExportIntervalMs"/>.</summary>
        public const string MetricExportIntervalEnvVar = "OTEL_METRIC_EXPORT_INTERVAL";
        /// <summary>Environment variable name: Standard OpenTelemetry sampler (the six spec names, ASCII case-insensitive), parsed by the helper when no sampler or ratio is set in code; unset: <c>parentbased_always_on</c>; another value is warned and the default applies.</summary>
        public const string TracesSamplerEnvVar = "OTEL_TRACES_SAMPLER";
        /// <summary>Environment variable name: Standard OpenTelemetry ratio of <c>traceidratio</c> and <c>parentbased_traceidratio</c>, a plain decimal in [0, 1]; unset: 1.0; another value is warned and 1.0 applies.</summary>
        public const string TracesSamplerArgEnvVar = "OTEL_TRACES_SAMPLER_ARG";
        /// <summary>Environment variable name: Standard OpenTelemetry propagator list: <c>tracecontext</c>, <c>baggage</c>, <c>none</c>; other values are warned and skipped; unset: <c>tracecontext,baggage</c>. Installed also under <c>OTEL_SDK_DISABLED=true</c>.</summary>
        public const string PropagatorsEnvVar = "OTEL_PROPAGATORS";
        /// <summary>Environment variable name: Standard OpenTelemetry OTLP protocol (<c>grpc</c> or <c>http/protobuf</c>); see <see cref="OtlpProtocol"/>.</summary>
        public const string OtlpProtocolEnvVar = "OTEL_EXPORTER_OTLP_PROTOCOL";
        /// <summary>Environment variable name: Standard OpenTelemetry OTLP compression (<c>gzip</c> or <c>none</c>); see <see cref="Compression"/>.</summary>
        public const string CompressionEnvVar = "OTEL_EXPORTER_OTLP_COMPRESSION";
        /// <summary>Environment variable name: Standard OpenTelemetry switch that disables the SDK, read from <c>IConfiguration</c> as the SDK reads it; a value other than <c>true</c>/<c>false</c> warns and means <c>false</c>.</summary>
        public const string SdkDisabledEnvVar = "OTEL_SDK_DISABLED";
        /// <summary>Environment variable name: Declarative configuration file; not supported by the helper (warned, ignored).</summary>
        public const string ConfigFileEnvVar = "OTEL_CONFIG_FILE";
        /// <summary>Environment variable name: Deprecated name of <see cref="ConfigFileEnvVar"/>; not supported (warned, ignored).</summary>
        public const string ExperimentalConfigFileEnvVar = "OTEL_EXPERIMENTAL_CONFIG_FILE";
        /// <summary>Environment variable name: HTTP opt-in metrics switch (<c>true</c>/<c>false</c>, ASCII case-insensitive; another value warns and means <c>false</c>); see <see cref="HttpOptInMetrics"/>.</summary>
        public const string HttpOptInMetricsEnvVar = "OTEL_HELPER_HTTP_OPT_IN_METRICS";

        internal const int DefaultExportIntervalMs = 30_000;
        internal const int DefaultExportTimeoutMs = 10_000;
        internal const int MaxExportTimeoutMs = 86_400_000;
        internal const int DefaultPrometheusMetricsPort = 9464;
        internal const int DefaultOtlpHttpPort = 4318;
        internal const string ProtocolGrpc = "grpc";
        internal const string ProtocolHttpProtobuf = "http/protobuf";
        internal const string CompressionGzip = "gzip";
        internal const string CompressionNone = "none";
        internal static readonly string[] ValidMetricExporters = { "otlp", "prometheus", "console", "none" };
        internal static readonly string[] ValidOtlpProtocols = { ProtocolGrpc, ProtocolHttpProtobuf };
        internal static readonly string[] ValidCompressions = { CompressionGzip, CompressionNone };

        /// <summary>
        /// Service name, emitted as <c>service.name</c>; wins over <c>service.name</c> in
        /// <see cref="ResourceAttributes"/>. Not set: <c>service.name</c> in
        /// <see cref="ResourceAttributes"/>, then <c>OTEL_SERVICE_NAME</c>, then <c>service.name</c> in
        /// <c>OTEL_RESOURCE_ATTRIBUTES</c>, then the deprecated <c>SERVICE_NAME</c>, then the SDK
        /// default <c>unknown_service:&lt;process name&gt;</c> with a warning. An empty or
        /// whitespace-only value fails host start; leave it <c>null</c> for "not set".
        /// </summary>
        public string? ServiceName { get; set; }

        /// <summary>
        /// Deployment environment, emitted as <c>deployment.environment.name</c> as written (trimmed);
        /// wins over the same key in <see cref="ResourceAttributes"/>. Not set: that key in
        /// <see cref="ResourceAttributes"/>, then in <c>OTEL_RESOURCE_ATTRIBUTES</c>, then the deprecated
        /// <c>ENVIRONMENT</c> variable (a known name in its canonical spelling, any other value as
        /// written); none of them: the attribute is absent, with a warning, and the log level is
        /// Information. An empty or whitespace-only value fails host start; leave it
        /// <c>null</c> for "not set". See <see cref="DeploymentEnvironments"/>.
        /// </summary>
        public string? Environment { get; set; }

        /// <summary>
        /// OTLP collector endpoint for every signal. Not set: <c>OTEL_EXPORTER_OTLP_&lt;SIGNAL&gt;_ENDPOINT</c>,
        /// else <c>OTEL_EXPORTER_OTLP_ENDPOINT</c>; when none is set, metrics fall back to the
        /// Prometheus <c>/metrics</c> endpoint and logs to stdout. Parsed by RFC 3986 (contract D1):
        /// an <c>http</c>/<c>https</c> URL keeps its path for HTTP/protobuf (which appends
        /// <c>/v1/&lt;signal&gt;</c>) and gRPC dials <c>host:port</c> (4317, warned, when the URL
        /// has none); a schemeless <c>host[:port]</c> gets <c>https://</c> (<c>http://</c> with
        /// <see cref="Insecure"/>) and the protocol's port. Userinfo, query and fragment are
        /// removed with a warning; any other scheme or an unparseable value fails at start.
        /// </summary>
        public string? OtelCollectorEndpoint { get; set; }

        /// <summary>OTLP endpoint for traces only, used as written (no signal path appended; schemeless allowed). Wins over <see cref="OtelCollectorEndpoint"/> and <c>OTEL_EXPORTER_OTLP_TRACES_ENDPOINT</c>.</summary>
        public string? TracesEndpoint { get; set; }

        /// <summary>OTLP endpoint for metrics only, used as written (no signal path appended; schemeless allowed). Wins over <see cref="OtelCollectorEndpoint"/> and <c>OTEL_EXPORTER_OTLP_METRICS_ENDPOINT</c>.</summary>
        public string? MetricsEndpoint { get; set; }

        /// <summary>OTLP endpoint for logs only, used as written (no signal path appended; schemeless allowed). Wins over <see cref="OtelCollectorEndpoint"/> and <c>OTEL_EXPORTER_OTLP_LOGS_ENDPOINT</c>.</summary>
        public string? LogsEndpoint { get; set; }

        /// <summary>
        /// Debug mode: log level Debug and the span attribute <c>debug=true</c> on entry spans (no
        /// parent, or a remote one); the tracestate is not touched.
        /// Not set: <c>OTEL_HELPER_DEBUG_LEVEL</c>.
        /// </summary>
        public bool? DebugLevel { get; set; }

        /// <summary>No effect in .NET: instrumentations are added by the opt-in packages.</summary>
        [Obsolete("ExtraInstrumentation has no effect: add the opt-in packages (StaffOps.OtelHelper.Sql, .AWS, .Redis) instead.")]
        public string? ExtraInstrumentation { get; set; }

        /// <summary>
        /// OTLP export timeout in milliseconds, in [1, 86 400 000]: the export deadline of every signal
        /// (exporter, HTTP client, batch processors and periodic reader). Not set:
        /// <c>OTEL_EXPORTER_OTLP_&lt;SIGNAL&gt;_TIMEOUT</c>, else <c>OTEL_EXPORTER_OTLP_TIMEOUT</c>, else 10 000.
        /// </summary>
        public int? ExportTimeoutMs { get; set; }

        /// <summary>
        /// Trace sampler; wins over <see cref="SampleRatio"/> and the environment. Not set:
        /// <see cref="SampleRatio"/>, else <c>OTEL_TRACES_SAMPLER</c>/<c>_ARG</c> (parsed by the
        /// helper from the process environment; a value only in <c>IConfiguration</c> is warned and
        /// not read), else <c>OTEL_HELPER_SAMPLE_RATIO</c>, else <c>parentbased_always_on</c>.
        /// Tail sampling in the Collector is preferred.
        /// </summary>
        public Sampler? Sampler { get; set; }

        /// <summary>
        /// Trace sample ratio in [0, 1]: <c>ParentBased(TraceIdRatioBased(ratio))</c>. Ignored when
        /// <see cref="Sampler"/> is set.
        /// </summary>
        public double? SampleRatio { get; set; }

        /// <summary>
        /// Minimum log level. When null, resolved from the environment and debug mode.
        /// </summary>
        public LogLevel? MinimumLogLevel { get; set; }

        /// <summary>
        /// Additional resource attributes added to all signals (traces, metrics, logs); a key here
        /// wins over the same key in <c>OTEL_RESOURCE_ATTRIBUTES</c>. A key must not be empty or
        /// whitespace, and a value must be a string, bool, number or array of them
        /// (<c>service.name</c>, <c>deployment.environment.name</c> and <c>service.instance.id</c>:
        /// strings only, a blank one counts as not set); otherwise host start fails.
        /// <c>telemetry.sdk.*</c> keys are ignored with a warning (the SDK sets them).
        /// Note: k8s.*, cloud.* are enriched by the Collector — avoid duplicating.
        /// </summary>
        public Dictionary<string, object> ResourceAttributes { get; set; } = new();

        /// <summary>
        /// Additional ActivitySource names to subscribe to, beyond the service name.
        /// </summary>
        public List<string> AdditionalActivitySources { get; set; } = new();

        /// <summary>
        /// Additional Meter names to export, beyond the service name.
        /// </summary>
        public List<string> AdditionalMeters { get; set; } = new();

        /// <summary>
        /// Comma-separated signals to disable: <c>traces</c>, <c>metrics</c>, <c>logs</c>
        /// (case-insensitive, trimmed, de-duplicated); any other entry fails host start. Set (not
        /// null), it wins over the environment whatever its value: an empty string disables nothing. Not set: the deprecated <c>OTEL_HELPER_DISABLED_SIGNALS</c>, which does
        /// not disable a signal whose <c>OTEL_&lt;SIGNAL&gt;_EXPORTER</c> holds a valid entry
        /// (<c>none</c> included) nor metrics when <see cref="MetricExporters"/> is set; its unknown
        /// entries warn once each.
        /// </summary>
        public string? DisabledSignals { get; set; }

        /// <summary>
        /// Comma-separated metric name patterns to drop: case-sensitive globs on the instrument
        /// name, <c>*</c> any run of characters, <c>?</c> one character, everything else literal.
        /// Not set: <c>OTEL_HELPER_DISABLED_METRICS</c>.
        /// </summary>
        public string? DisabledMetrics { get; set; }

        /// <summary>
        /// Comma-separated metric exporters: <c>otlp</c>, <c>prometheus</c>, <c>console</c> (the
        /// OpenTelemetry console exporter, on standard output) or <c>none</c> alone; any other
        /// entry, <c>logging</c> included, fails host start. Set (not null), it wins whatever its
        /// value: an empty string means no metric exporter. Not set: <c>OTEL_METRICS_EXPORTER</c>
        /// (there <c>logging</c> is a warned alias of <c>console</c>, an unknown entry warns, and
        /// <c>otlp</c> without an endpoint warns and is dropped), else <c>otlp</c> with an endpoint
        /// and <c>prometheus</c> without one.
        /// </summary>
        public string? MetricExporters { get; set; }

        /// <summary>
        /// Metric export interval (ms) of the OTLP and console readers, in [1, 2 147 483 647];
        /// outside it host start fails. Not set: <c>OTEL_METRIC_EXPORT_INTERVAL</c> from the
        /// process environment (digits with an optional <c>+</c>, any length; zero or anything
        /// else warns and uses 30 000; above 2 147 483 647 it is clamped with a warning), else 30 000.
        /// </summary>
        public long? ExportIntervalMs { get; set; }

        /// <summary>
        /// Port of the standalone Prometheus <c>/metrics</c> listener, in [0, 65535]; 0 means no
        /// listener. Not set: <c>OTEL_EXPORTER_PROMETHEUS_PORT</c>, else <c>OTEL_HELPER_METRICS_PORT</c>
        /// (both <c>^\+?[0-9]+$</c>; an invalid value warns and the next one applies), else 9464.
        /// The bind address comes from <c>OTEL_EXPORTER_PROMETHEUS_HOST</c> (no code option).
        /// </summary>
        public int? PrometheusMetricsPort { get; set; }

        /// <summary>
        /// OTLP wire protocol for every signal: <c>grpc</c> or <c>http/protobuf</c>. Not set:
        /// <c>OTEL_EXPORTER_OTLP_&lt;SIGNAL&gt;_PROTOCOL</c>, else <c>OTEL_EXPORTER_OTLP_PROTOCOL</c>,
        /// else port 4318 on the endpoint the signal uses means <c>http/protobuf</c>, else <c>grpc</c>.
        /// </summary>
        public string? OtlpProtocol { get; set; }

        /// <summary>
        /// Plaintext for a schemeless endpoint (<c>http://</c> instead of <c>https://</c>).
        /// Not set: <c>OTEL_EXPORTER_OTLP_&lt;SIGNAL&gt;_INSECURE</c>, else <c>OTEL_EXPORTER_OTLP_INSECURE</c>.
        /// An explicit scheme wins; set here, a contradiction with a code URL fails at start.
        /// </summary>
        public bool? Insecure { get; set; }

        /// <summary>
        /// OTLP compression of every signal: <c>gzip</c> or <c>none</c>. Not set:
        /// <c>OTEL_EXPORTER_OTLP_&lt;SIGNAL&gt;_COMPRESSION</c>, else <c>OTEL_EXPORTER_OTLP_COMPRESSION</c>, else <c>gzip</c>.
        /// </summary>
        public string? Compression { get; set; }

        /// <summary>
        /// HTTP paths whose server and client spans are not recorded, replacing the default list
        /// <c>/health /healthz /ready /readyz /live /livez /ping /metrics</c>; an empty list traces
        /// every HTTP path. Each path must be ASCII, start with <c>/</c> and have no <c>?</c>,
        /// <c>#</c> or <c>,</c>; otherwise host start fails. A request matches when its path, cut
        /// at the query, percent-decoded, ASCII-lowercased and without trailing slashes, equals a
        /// listed path normalized the same way (<c>/Health/?x=1</c> and <c>/%68ealth</c> are
        /// <c>/health</c>; <c>/users/ping</c> is not <c>/ping</c>). Health requests are still counted
        /// in <c>http.server.request.duration</c>. The <c>grpc.health.v1.Health</c> service is
        /// always dropped, whatever this list holds.
        /// </summary>
        public List<string>? HealthPaths { get; set; }

        /// <summary>
        /// Record the HTTP metrics semantic conventions make opt-in: <c>http.server.active_requests</c>,
        /// <c>http.client.active_requests</c>, <c>http.client.open_connections</c>,
        /// <c>http.client.connection.duration</c> and the .NET-only <c>http.client.request.time_in_queue</c>
        /// (ASP.NET Core and <c>System.Net.Http</c> record no body sizes). <c>false</c>: only
        /// <c>http.server.request.duration</c> and <c>http.client.request.duration</c>. Not set:
        /// <c>OTEL_HELPER_HTTP_OPT_IN_METRICS</c>, else <c>false</c>. An application meter's own
        /// instrument of one of these names is kept; <see cref="DisabledMetrics"/> still drops one.
        /// </summary>
        public bool? HttpOptInMetrics { get; set; }

        /// <summary>
        /// Forward OpenTelemetry SDK warnings and errors (its <c>EventSource</c> events) to the
        /// helper's standard-error diagnostics, redacted. Default <c>true</c>.
        /// </summary>
        public bool ForwardSdkDiagnostics { get; set; } = true;

        /// <summary>
        /// Default minimum log level for an environment name, matched ASCII case-insensitively
        /// after trimming (contract R4, as Go and Python): <c>LOCAL</c> → Debug, <c>DEV</c> and
        /// <c>HML</c> → Information, <c>PRD</c> → Warning, unset or any other value → Information.
        /// Debug mode → Debug.
        /// </summary>
        public static LogLevel GetDefaultLogLevel(string? environment, bool debug = false)
        {
            if (debug) return LogLevel.Debug;

            return TelemetryResolver.KnownEnvironment(environment) switch
            {
                DeploymentEnvironments.LOCAL => LogLevel.Debug,
                DeploymentEnvironments.DEV => LogLevel.Information,
                DeploymentEnvironments.HML => LogLevel.Information,
                DeploymentEnvironments.PRD => LogLevel.Warning,
                _ => LogLevel.Information,
            };
        }
    }
}
