using System.Globalization;
using Microsoft.Extensions.Configuration;
using OpenTelemetry.Metrics;
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
        /// <summary>Health paths whose spans are not recorded when <see cref="TelemetryOptions.HealthPaths"/> is not set (contract D5).</summary>
        internal static readonly string[] DefaultHealthPaths = Tracing.HealthMatching.DefaultPaths;

        internal static ResolvedTelemetry Resolve(TelemetryOptions code, IOtelHelperDiagnostics diagnostics, IConfiguration? configuration = null)
        {
            WarnUnsupported(code, diagnostics);

            var protocol = ResolveProtocol(code, diagnostics);
            var targets = new Dictionary<string, OtlpSignalTarget>(StringComparer.Ordinal);
            foreach (var signal in ResolvedTelemetry.Signals)
            {
                if (ResolveSignal(signal, code, diagnostics) is { } target)
                {
                    targets[signal] = target;
                }
            }

            var generic = ResolveGenericEndpoint(code, protocol);

            // Exporter settings of every signal with a target (task C8, A18), then the A18 rows that
            // do not depend on a target.
            var exportSettings = new Dictionary<string, SignalExportSettings>(StringComparer.Ordinal);
            foreach (var (signal, target) in targets)
            {
                exportSettings[signal] = ExportSettingsResolver.Resolve(signal, target, code, diagnostics);
            }

            ExportSettingsResolver.WarnPlaintextTlsFiles(targets.Select(t => (t.Key, t.Value)), diagnostics);
            ExportSettingsResolver.WarnIgnoredTimeouts(diagnostics);
            ExportSettingsResolver.WarnZeroValueLength(diagnostics);

            var identity = ResolveIdentity(code, diagnostics);
            var environment = identity.Environment;
            var debug = code.DebugLevel ?? EnvTrue(TelemetryOptions.DebugLevelEnvVar);
            var sdkDisabled = ReadSdkDisabled(configuration, diagnostics);
            if (sdkDisabled)
            {
                diagnostics.ReportOnce("info:" + TelemetryOptions.SdkDisabledEnvVar, DiagnosticLevel.Info,
                    "OTEL_SDK_DISABLED=true: telemetry disabled, only propagators installed");
            }

            var sampler = SamplerResolver.Resolve(code, diagnostics, configuration);
            var propagators = PropagatorResolver.Resolve(diagnostics, configuration);

            var hasMetricsEndpoint = targets.ContainsKey("metrics");
            var tracesExporters = ParseExporterEnv(TracesExporterEnvVar, TraceLogExporterNames, diagnostics);
            var logsExporters = ParseExporterEnv(LogsExporterEnvVar, TraceLogExporterNames, diagnostics);
            var metricExporters = ResolveMetricExporters(code, hasMetricsEndpoint, diagnostics, out var metricsChosen);
            var chosen = new Dictionary<string, bool>(StringComparer.Ordinal)
            {
                ["traces"] = tracesExporters is not null,
                ["metrics"] = metricsChosen,
                ["logs"] = logsExporters is not null,
            };
            var portSource = PrometheusPortEnvVar;
            return new ResolvedTelemetry
            {
                ServiceName = identity.ServiceName,
                ServiceNameConfigured = identity.ServiceNameConfigured,
                Environment = environment,
                ServiceInstanceId = identity.ServiceInstanceId,
                ServiceInstanceIdConfigured = identity.ServiceInstanceIdConfigured,
                CodeMinimumLogLevel = code.MinimumLogLevel,
                EnvResourceAttributes = identity.EnvResourceAttributes,
                OtelCollectorEndpoint = generic.Endpoint,
                GenericOtlpProtocol = generic.Protocol,
                SignalTargets = targets,
                OtlpProtocol = protocol,
                DebugLevel = debug,
                ExportTimeoutMs = ExportSettingsResolver.ResolveTimeout("", code, null),
                Compression = ExportSettingsResolver.ResolveCompression("", code, null),
                ExportSettings = exportSettings,
                HeaderTexts = HeaderVariables.Select(Env).Where(v => !string.IsNullOrEmpty(v)).Select(v => v!).ToArray(),
                Sampler = sampler.Sampler,
                SamplerName = sampler.Name,
                Propagators = propagators,
                MinimumLogLevel = code.MinimumLogLevel ?? TelemetryOptions.GetDefaultLogLevel(environment, debug),
                ResourceAttributes = new Dictionary<string, object>(code.ResourceAttributes ?? new Dictionary<string, object>()),
                AdditionalActivitySources = code.AdditionalActivitySources.ToArray(),
                AdditionalMeters = code.AdditionalMeters.ToArray(),
                DisabledSignals = ResolveDisabledSignals(code, chosen, diagnostics),
                DisabledMetrics = NonBlank(code.DisabledMetrics) ?? NonBlank(Env(TelemetryOptions.DisabledMetricsEnvVar)) ?? "",
                MetricExporters = metricExporters,
                TracesExporters = tracesExporters ?? DefaultTraceLogExporters,
                LogsExporters = logsExporters ?? DefaultTraceLogExporters,
                ExportIntervalMs = code.ExportIntervalMs ?? ResolveEnvInterval(diagnostics, configuration),
                ExemplarFilter = ResolveExemplarFilter(diagnostics),
                // Code wins (the variables are then not read, as in Go); the variables are parsed and
                // warned at every setup, also when the listener does not run (A29).
                PrometheusMetricsPort = code.PrometheusMetricsPort ?? ResolveEnvPort(diagnostics, out portSource),
                PrometheusPortSource = code.PrometheusMetricsPort is null ? portSource : "TelemetryOptions.PrometheusMetricsPort",
                PrometheusMetricsHost = ResolvePrometheusHost(diagnostics, out var ipv6Unspecified),
                PrometheusHostIPv6Unspecified = ipv6Unspecified,
                HealthPaths = code.HealthPaths?.ToArray() ?? DefaultHealthPaths,
                HttpOptInMetrics = code.HttpOptInMetrics ?? ResolveEnvHttpOptInMetrics(diagnostics),
                SdkDisabled = sdkDisabled,
                ForwardSdkDiagnostics = code.ForwardSdkDiagnostics,
                MetricTemporality = ResolveTemporality(configuration, diagnostics, out var temporalityConfigured),
                MetricTemporalityConfigured = temporalityConfigured,
            };
        }

        // ── identity ───────────────────────────────────────────────────────

        internal const string ServiceNameKey = "service.name";
        internal const string EnvironmentKey = "deployment.environment.name";
        internal const string ServiceInstanceIdKey = "service.instance.id";
        internal const string ResourceAttributesEnvVar = "OTEL_RESOURCE_ATTRIBUTES";

        /// <summary>The generic and per-signal OTLP header variables (their raw values are masked in forwarded SDK text).</summary>
        internal static readonly string[] HeaderVariables =
        {
            "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_METRICS_HEADERS", "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
        };

        /// <summary><c>telemetry.sdk.*</c> keys: they always come from the SDK (contract row "telemetry.sdk.* and the resource schema URL", A16).</summary>
        internal static readonly string[] TelemetrySdkKeys = { "telemetry.sdk.name", "telemetry.sdk.language", "telemetry.sdk.version" };

        internal readonly record struct Identity(
            string ServiceName,
            bool ServiceNameConfigured,
            string? Environment,
            string ServiceInstanceId,
            bool ServiceInstanceIdConfigured,
            IReadOnlyDictionary<string, string> EnvResourceAttributes);

        /// <summary>
        /// <c>service.name</c>, <c>deployment.environment.name</c> and <c>service.instance.id</c>
        /// (contract design D1 identity rows, D4, ADR-003). A blank value (empty or contract trim set
        /// only) is not provided at any level, so the next level applies; the dedicated code option
        /// beats the same key in code <see cref="TelemetryOptions.ResourceAttributes"/>.
        /// </summary>
        private static Identity ResolveIdentity(TelemetryOptions code, IOtelHelperDiagnostics diagnostics)
        {
            var envResource = ParseEnvResourceAttributes(diagnostics);
            var codeAttributes = code.ResourceAttributes ?? new Dictionary<string, object>();

            // service.name: code option > code attribute > OTEL_SERVICE_NAME > OTEL_RESOURCE_ATTRIBUTES
            // > SERVICE_NAME (deprecated) > the SDK default unknown_service:<process> + warning.
            var serviceName = ContractText.NonBlank(code.ServiceName)
                ?? CodeString(codeAttributes, ServiceNameKey)
                ?? ContractText.NonBlank(Env(TelemetryOptions.OtelServiceNameEnvVar))
                ?? ContractText.NonBlank(envResource.GetValueOrDefault(ServiceNameKey))
                ?? ContractText.NonBlank(Env(TelemetryOptions.ServiceNameEnvVar));
            var serviceNameConfigured = serviceName is not null;
            if (serviceName is null)
            {
                serviceName = UnknownServiceName();
                diagnostics.ReportOnce("identity:" + ServiceNameKey, DiagnosticLevel.Warn,
                    "no service name configured; set " + TelemetryOptions.OtelServiceNameEnvVar,
                    ("using", serviceName));
            }

            // deployment.environment.name: code option > code attribute > OTEL_RESOURCE_ATTRIBUTES >
            // ENVIRONMENT (deprecated alias, a known name canonicalized) > absent + warning. Emitted verbatim.
            var environment = ContractText.NonBlank(code.Environment)
                ?? CodeString(codeAttributes, EnvironmentKey)
                ?? ContractText.NonBlank(envResource.GetValueOrDefault(EnvironmentKey));
            if (environment is null && ContractText.NonBlank(Env(TelemetryOptions.EnvironmentEnvVar)) is { } alias)
            {
                environment = KnownEnvironment(alias) ?? alias;
            }

            // Unset: the warning is written when the resource is built (ResourceSetup), because an
            // earlier application ConfigureResource may still provide the value (code level).

            // service.instance.id: code > OTEL_RESOURCE_ATTRIBUTES > one id per process (D4).
            var configuredId = CodeString(codeAttributes, ServiceInstanceIdKey)
                ?? ContractText.NonBlank(envResource.GetValueOrDefault(ServiceInstanceIdKey));
            var instanceId = configuredId ?? ResourceSetup.ProcessInstanceId;

            // telemetry.sdk.* set in code are ignored (the SDK sets them): warned once per key,
            // naming the key only. Env values for them are ignored silently.
            foreach (var key in TelemetrySdkKeys.Where(codeAttributes.ContainsKey))
            {
                diagnostics.ReportOnce("code-telemetry-sdk:" + key, DiagnosticLevel.Warn,
                    $"TelemetryOptions.ResourceAttributes[\"{key}\"] is ignored; the OpenTelemetry SDK sets {key}",
                    ("key", key));
            }

            return new Identity(serviceName, serviceNameConfigured, environment, instanceId, configuredId is not null, envResource);
        }

        /// <summary><c>OTEL_RESOURCE_ATTRIBUTES</c> as the helper parses it (A16); problems warned once, by item position only.</summary>
        private static IReadOnlyDictionary<string, string> ParseEnvResourceAttributes(IOtelHelperDiagnostics diagnostics)
        {
            var raw = Env(ResourceAttributesEnvVar);
            if (raw is null || ContractText.Trim(raw).Length == 0)
            {
                return ParsedResourceAttributes.Empty.Attributes;
            }

            var parsed = ResourceAttributesParser.Parse(raw);
            if (parsed.Malformed.Count > 0)
            {
                diagnostics.ReportOnce("resource-attributes:malformed", DiagnosticLevel.Warn,
                    "resource attribute items are not key=value and were skipped",
                    ("variable", ResourceAttributesEnvVar), ("items", string.Join(",", parsed.Malformed)));
            }

            if (parsed.Undecodable.Count > 0)
            {
                diagnostics.ReportOnce("resource-attributes:undecodable", DiagnosticLevel.Warn,
                    "resource attribute values have an invalid percent-encoding and are kept literally",
                    ("variable", ResourceAttributesEnvVar), ("items", string.Join(",", parsed.Undecodable)));
            }

            return parsed.Attributes;
        }

        /// <summary>A code attribute for an identity key: its string value trimmed, null when blank (the validator rejects non-strings).</summary>
        private static string? CodeString(IDictionary<string, object> attributes, string key)
            => attributes.TryGetValue(key, out var value) ? ContractText.NonBlank(value as string) : null;

        /// <summary>A known environment name (contract trim set, ASCII case-insensitive) in its canonical spelling, else null.</summary>
        internal static string? KnownEnvironment(string? value)
        {
            if (ContractText.NonBlank(value) is not { } trimmed)
            {
                return null;
            }

            var upper = ContractText.AsciiUpper(trimmed);
            return DeploymentEnvironments.Known.FirstOrDefault(k => string.Equals(k, upper, StringComparison.Ordinal));
        }

        /// <summary>The SDK's default service name, <c>unknown_service:&lt;process name&gt;</c> (<c>ResourceBuilder.PrepareDefaultResource</c>).</summary>
        internal static string UnknownServiceName()
        {
            string? processName = null;
            try
            {
                using var process = System.Diagnostics.Process.GetCurrentProcess();
                processName = process.ProcessName;
            }
            catch (Exception ex) when (ex is PlatformNotSupportedException or InvalidOperationException or NotSupportedException)
            {
                // As the SDK: no process name, no suffix.
            }

            return processName is { Length: > 0 } ? "unknown_service:" + processName : "unknown_service";
        }

        // ── endpoints and exporter settings ────────────────────────────────

        /// <summary>The per-signal OTLP environment variable, e.g. <c>OTEL_EXPORTER_OTLP_TRACES_ENDPOINT</c>.</summary>
        internal static string SignalVariable(string signal, string suffix)
            => "OTEL_EXPORTER_OTLP_" + ContractText.AsciiUpper(signal) + "_" + suffix;

        /// <summary>The code option of a signal's endpoint, as named in diagnostics.</summary>
        private static string CodeSignalOption(string signal) => signal switch
        {
            "traces" => "TelemetryOptions." + nameof(TelemetryOptions.TracesEndpoint),
            "metrics" => "TelemetryOptions." + nameof(TelemetryOptions.MetricsEndpoint),
            _ => "TelemetryOptions." + nameof(TelemetryOptions.LogsEndpoint),
        };

        private static string? CodeSignalEndpoint(TelemetryOptions code, string signal) => signal switch
        {
            "traces" => code.TracesEndpoint,
            "metrics" => code.MetricsEndpoint,
            _ => code.LogsEndpoint,
        };

        private const string CodeGenericOption = "TelemetryOptions." + nameof(TelemetryOptions.OtelCollectorEndpoint);
        private const string CodeInsecureOption = "TelemetryOptions." + nameof(TelemetryOptions.Insecure);

        /// <summary>
        /// The OTLP target of one signal (contract D1 rows "Endpoint", "Fallback order for every
        /// per-signal value", design D1.b–c): candidates in order code per-signal, code generic,
        /// <c>OTEL_EXPORTER_OTLP_&lt;SIGNAL&gt;_ENDPOINT</c>, <c>OTEL_EXPORTER_OTLP_ENDPOINT</c>;
        /// an unparseable environment value warns and falls back to the next one, an unsupported
        /// scheme warns and leaves the signal unconfigured. Null when no candidate is usable.
        /// </summary>
        private static OtlpSignalTarget? ResolveSignal(string signal, TelemetryOptions code, IOtelHelperDiagnostics diagnostics)
        {
            // INSECURE first: an unparseable value warns even when no endpoint is configured (D1 ruling, principle 8).
            var insecure = ResolveInsecure(signal, code, diagnostics);
            var candidates = new (string? Raw, string Variable, bool FromCode, bool PerSignal)[]
            {
                (CodeSignalEndpoint(code, signal), CodeSignalOption(signal), true, true),
                (code.OtelCollectorEndpoint, CodeGenericOption, true, false),
                (Env(SignalVariable(signal, "ENDPOINT")), SignalVariable(signal, "ENDPOINT"), false, true),
                (Env(TelemetryOptions.CollectorEndpointEnvVar), TelemetryOptions.CollectorEndpointEnvVar, false, false),
            };

            foreach (var (value, variable, fromCode, perSignal) in candidates)
            {
                if (ContractText.NonBlank(value) is not { } raw)
                {
                    continue;
                }

                var input = new EndpointInput
                {
                    Raw = raw,
                    Variable = variable,
                    Signal = signal,
                    Protocol = SignalProtocol(signal, code, raw, null),
                    FromCode = fromCode,
                    PerSignal = perSignal,
                    Insecure = insecure.Value,
                    InsecureFromCode = insecure.FromCode,
                    InsecureVariable = insecure.Variable,
                };
                var outcome = EndpointResolver.Resolve(input);
                foreach (var warning in outcome.Warnings)
                {
                    diagnostics.ReportOnce(warning.OnceKey, DiagnosticLevel.Warn, warning.Message, warning.Attributes);
                }

                switch (outcome.Failure)
                {
                    case EndpointFailure.Unsupported:
                        return null;
                    case EndpointFailure.Invalid:
                        // An invalid code value fails at start (validator); an environment value falls back.
                        if (fromCode)
                        {
                            return null;
                        }

                        continue;
                }

                // Now with diagnostics: the per-signal protocol warnings and the 4318 INFO, for the chosen value only.
                return new OtlpSignalTarget(outcome.Url!, SignalProtocol(signal, code, raw, diagnostics), variable, perSignal);
            }

            return null;
        }

        /// <summary>
        /// The effective INSECURE setting of a signal: code &gt; <c>OTEL_EXPORTER_OTLP_&lt;SIGNAL&gt;_INSECURE</c>
        /// &gt; <c>OTEL_EXPORTER_OTLP_INSECURE</c>. A set variable is a boolean: <c>true</c>
        /// (ASCII case-insensitive, contract trim set) is true, anything else false, and a value that
        /// is neither <c>true</c> nor <c>false</c> warns (only the variable that is consulted:
        /// a variable shadowed by code or by the per-signal one stays silent).
        /// </summary>
        internal static (bool? Value, bool FromCode, string Variable) ResolveInsecure(string signal, TelemetryOptions code, IOtelHelperDiagnostics? diagnostics)
        {
            if (code.Insecure is { } fromCode)
            {
                return (fromCode, true, CodeInsecureOption);
            }

            var variables = signal.Length == 0
                ? new[] { TelemetryOptions.InsecureEnvVar }
                : new[] { SignalVariable(signal, "INSECURE"), TelemetryOptions.InsecureEnvVar };
            foreach (var variable in variables)
            {
                if (ContractText.NonBlank(Env(variable)) is not { } raw)
                {
                    continue;
                }

                var lower = ContractText.AsciiLower(raw);
                if (lower is not ("true" or "false") && diagnostics is not null)
                {
                    WarnInvalidEnv(diagnostics, variable, raw, "not a boolean (true or false), using false");
                }

                return (lower == "true", false, variable);
            }

            return (null, false, "");
        }

        /// <summary>
        /// The OTLP protocol of a signal for one endpoint value (contract D1 row "Protocol", D1.a):
        /// code &gt; <c>OTEL_EXPORTER_OTLP_&lt;SIGNAL&gt;_PROTOCOL</c> &gt; <c>OTEL_EXPORTER_OTLP_PROTOCOL</c>
        /// &gt; port 4318 (compared numerically) → <c>http/protobuf</c>, logged once at INFO &gt;
        /// <c>grpc</c>. An unknown or <c>http/json</c> environment value warns and the next level
        /// applies. With <paramref name="diagnostics"/> null nothing is reported.
        /// </summary>
        internal static string SignalProtocol(string signal, TelemetryOptions code, string endpoint, IOtelHelperDiagnostics? diagnostics)
        {
            if (ContractText.NonBlank(code.OtlpProtocol) is { } fromCode)
            {
                return ContractText.AsciiLower(fromCode) == TelemetryOptions.ProtocolHttpProtobuf
                    ? TelemetryOptions.ProtocolHttpProtobuf
                    : TelemetryOptions.ProtocolGrpc;
            }

            var variables = signal.Length == 0
                ? new[] { TelemetryOptions.OtlpProtocolEnvVar }
                : new[] { SignalVariable(signal, "PROTOCOL"), TelemetryOptions.OtlpProtocolEnvVar };
            foreach (var variable in variables)
            {
                if (ContractText.NonBlank(Env(variable)) is not { } raw)
                {
                    continue;
                }

                var value = ContractText.AsciiLower(raw);
                if (TelemetryOptions.ValidOtlpProtocols.Contains(value))
                {
                    return value;
                }

                if (diagnostics is not null)
                {
                    WarnInvalidEnv(diagnostics, variable, raw, value == "http/json"
                        ? "http/json is not supported by the .NET OTLP exporter; ignored (port inference, else grpc)"
                        : "unsupported OTLP protocol, ignored (port inference, else grpc); valid values: grpc, http/protobuf");
                }
            }

            if (EndpointGrammar.EndpointPort(endpoint) == EndpointResolver.DefaultHttpPort)
            {
                diagnostics?.ReportOnce("info:infer4318", DiagnosticLevel.Info,
                    "endpoint port 4318 and no protocol set; using http/protobuf", ("set", TelemetryOptions.OtlpProtocolEnvVar));
                return TelemetryOptions.ProtocolHttpProtobuf;
            }

            return TelemetryOptions.ProtocolGrpc;
        }

        /// <summary>
        /// The generic endpoint after resolution, for reporting (<see cref="ResolvedTelemetry.OtelCollectorEndpoint"/>):
        /// code, else <c>OTEL_EXPORTER_OTLP_ENDPOINT</c>, resolved as <c>scheme://host:port</c>
        /// (gRPC) or <c>scheme://host[:port][path]</c> (HTTP, before <c>/v1/&lt;signal&gt;</c>);
        /// empty when neither is usable. Warnings come from the per-signal resolution.
        /// </summary>
        private static (string Endpoint, string Protocol) ResolveGenericEndpoint(TelemetryOptions code, string? protocol)
        {
            var insecure = ResolveInsecure("", code, null);
            foreach (var (value, variable, fromCode) in new[]
            {
                (code.OtelCollectorEndpoint, CodeGenericOption, true),
                (Env(TelemetryOptions.CollectorEndpointEnvVar), TelemetryOptions.CollectorEndpointEnvVar, false),
            })
            {
                if (ContractText.NonBlank(value) is not { } raw)
                {
                    continue;
                }

                var resolvedProtocol = protocol ?? SignalProtocol("", code, raw, null);
                var outcome = EndpointResolver.Resolve(new EndpointInput
                {
                    Raw = raw,
                    Variable = variable,
                    Signal = "",
                    Protocol = resolvedProtocol,
                    FromCode = fromCode,
                    PerSignal = true,
                    Insecure = insecure.Value,
                    InsecureFromCode = insecure.FromCode,
                    InsecureVariable = insecure.Variable,
                });
                if (outcome.Url is { } url)
                {
                    return (url, resolvedProtocol);
                }

                if (outcome.Failure == EndpointFailure.Unsupported || fromCode)
                {
                    break;
                }
            }

            return ("", protocol ?? TelemetryOptions.ProtocolGrpc);
        }

        /// <summary>
        /// The fail-fast errors of the code endpoints (validator, design D-NET6): each signal's
        /// code value (its own option, else <see cref="TelemetryOptions.OtelCollectorEndpoint"/>)
        /// resolved with that signal's protocol and INSECURE setting. Each message names the option
        /// and renders the value only through <see cref="EndpointGrammar.Render"/>.
        /// </summary>
        internal static IReadOnlyList<string> CodeEndpointErrors(TelemetryOptions code)
        {
            var errors = new List<string>();
            foreach (var signal in ResolvedTelemetry.Signals)
            {
                var (value, variable, perSignal) = ContractText.NonBlank(CodeSignalEndpoint(code, signal)) is { } own
                    ? (own, CodeSignalOption(signal), true)
                    : (ContractText.NonBlank(code.OtelCollectorEndpoint), CodeGenericOption, false);
                if (value is null)
                {
                    continue;
                }

                var insecure = ResolveInsecure(signal, code, null);
                var outcome = EndpointResolver.Resolve(new EndpointInput
                {
                    Raw = value,
                    Variable = variable,
                    Signal = signal,
                    Protocol = SignalProtocol(signal, code, value, null),
                    FromCode = true,
                    PerSignal = perSignal,
                    Insecure = insecure.Value,
                    InsecureFromCode = insecure.FromCode,
                    InsecureVariable = insecure.Variable,
                });
                if (outcome.Error is { } error && !errors.Contains(error))
                {
                    errors.Add(error);
                }
            }

            return errors;
        }

        /// <summary>The protocol set in code, else a valid <c>OTEL_EXPORTER_OTLP_PROTOCOL</c> (an invalid one warns); null = per signal.</summary>
        private static string? ResolveProtocol(TelemetryOptions code, IOtelHelperDiagnostics diagnostics)
        {
            if (NonBlank(code.OtlpProtocol) is { } fromCode)
            {
                return ContractText.AsciiLower(ContractText.Trim(fromCode));
            }

            var fromEnv = ContractText.NonBlank(Env(TelemetryOptions.OtlpProtocolEnvVar));
            if (fromEnv is null)
            {
                return null;
            }

            var normalized = ContractText.AsciiLower(fromEnv);
            if (TelemetryOptions.ValidOtlpProtocols.Contains(normalized))
            {
                return normalized;
            }

            WarnInvalidEnv(diagnostics, TelemetryOptions.OtlpProtocolEnvVar, fromEnv, normalized == "http/json"
                ? "http/json is not supported by the .NET OTLP exporter; ignored (port inference, else grpc)"
                : "unsupported OTLP protocol, ignored (port inference, else grpc); valid values: grpc, http/protobuf");
            return null;
        }

        // ── metrics ────────────────────────────────────────────────────────

        internal const string TracesExporterEnvVar = "OTEL_TRACES_EXPORTER";
        internal const string LogsExporterEnvVar = "OTEL_LOGS_EXPORTER";
        internal const string ExemplarFilterEnvVar = "OTEL_METRICS_EXEMPLAR_FILTER";

        /// <summary>Exporter names of <c>OTEL_TRACES_EXPORTER</c> and <c>OTEL_LOGS_EXPORTER</c> besides <c>none</c>.</summary>
        internal static readonly string[] TraceLogExporterNames = { "otlp", "console" };

        /// <summary>Exporter names of <c>OTEL_METRICS_EXPORTER</c> besides <c>none</c>.</summary>
        internal static readonly string[] MetricExporterNames = { "otlp", "prometheus", "console" };

        /// <summary>The traces and logs exporters when their variable is unset: <c>otlp</c> (active only with an endpoint).</summary>
        private static readonly string[] DefaultTraceLogExporters = { "otlp" };

        /// <summary>
        /// A comma-separated list as the contract reads one: entries trimmed with the contract
        /// trim set, ASCII-lowercased, empty entries dropped (duplicates kept).
        /// </summary>
        internal static string[] SplitList(string value)
            => value.Split(',')
                .Select(e => ContractText.AsciiLower(ContractText.Trim(e)))
                .Where(e => e.Length > 0)
                .ToArray();

        /// <summary>
        /// <c>OTEL_{TRACES,METRICS,LOGS}_EXPORTER</c> (contract R8, rows "Exporter alias logging" and
        /// "Signal on/off", A19). Null: unset, blank, only separators or only unknown entries (the
        /// default applies, and the variable does not count as set for the deprecated
        /// <c>OTEL_HELPER_DISABLED_SIGNALS</c>). Empty: <c>none</c> — combined with other entries it
        /// warns and still wins. <c>logging</c> is a deprecated alias of <c>console</c> and warns; an
        /// unknown entry warns and is dropped; entries are de-duplicated in order.
        /// </summary>
        internal static IReadOnlyList<string>? ParseExporterEnv(string variable, IReadOnlyCollection<string> names, IOtelHelperDiagnostics diagnostics)
        {
            if (ContractText.NonBlank(Env(variable)) is not { } raw)
            {
                return null;
            }

            var list = new List<string>();
            var none = false;
            var unknown = false;
            foreach (var entry in SplitList(raw))
            {
                var name = entry;
                if (name == "none")
                {
                    none = true;
                    continue;
                }

                if (name == "logging")
                {
                    diagnostics.ReportOnce($"alias:{variable}", DiagnosticLevel.Warn,
                        $"deprecated exporter value 'logging' in {variable}; use 'console'",
                        ("variable", variable), ("value", "logging"), ("using", "console"));
                    name = "console";
                }
                else if (!names.Contains(name))
                {
                    unknown = true;
                    continue;
                }

                if (!list.Contains(name))
                {
                    list.Add(name);
                }
            }

            if (unknown)
            {
                WarnInvalidEnv(diagnostics, variable, raw, $"unknown exporter ignored; valid values: {string.Join(", ", names)}, none");
            }

            if (none)
            {
                if (list.Count > 0)
                {
                    // Its own once-key: the unknown-entry warning above may echo the same value.
                    diagnostics.ReportOnce($"none-combined:{variable}:{raw}", DiagnosticLevel.Warn,
                        $"invalid {variable}: 'none' combined with other exporters: the signal is not exported",
                        ("variable", variable), ("value", raw));
                }

                return Array.Empty<string>();
            }

            return list.Count == 0 ? null : list;
        }

        /// <summary>
        /// The metric exporters: code (validated at start: known names, <c>none</c> alone, <c>otlp</c>
        /// with an endpoint) &gt; <c>OTEL_METRICS_EXPORTER</c> &gt; <c>otlp</c> with a metrics endpoint,
        /// else <c>prometheus</c>. An environment <c>otlp</c> without a metrics endpoint warns and is
        /// dropped; when nothing is left the default applies (R2). <paramref name="chosen"/>: whether
        /// code or a variable with a valid entry chose them (it then wins over
        /// <c>OTEL_HELPER_DISABLED_SIGNALS</c>), also when <c>otlp</c> was dropped.
        /// </summary>
        private static IReadOnlyList<string> ResolveMetricExporters(TelemetryOptions code, bool hasEndpoint, IOtelHelperDiagnostics diagnostics, out bool chosen)
        {
            var inferred = hasEndpoint ? new[] { "otlp" } : new[] { "prometheus" };
            // Set in code wins whatever its value (contract: Go and Node check "set", Python
            // "is not None"): an empty or blank list means no exporter.
            if (code.MetricExporters is { } fromCode)
            {
                chosen = true;
                var list = SplitList(fromCode);
                if (list.Length == 0)
                {
                    // Contract behavior (Go, Python, Node: silent); said once here because a blank
                    // bound placeholder ("MetricExporters": "") counted as unset before 0.3.
                    diagnostics.ReportOnce("info:empty-metric-exporters", DiagnosticLevel.Info,
                        "TelemetryOptions.MetricExporters is empty: no metric exporter");
                }

                return list.Contains("none") ? Array.Empty<string>() : list.Distinct().ToArray();
            }

            var fromEnv = ParseExporterEnv(TelemetryOptions.MetricsExporterEnvVar, MetricExporterNames, diagnostics);
            chosen = fromEnv is not null;
            if (fromEnv is null)
            {
                return inferred;
            }

            if (fromEnv.Contains("otlp") && !hasEndpoint)
            {
                diagnostics.ReportOnce("otlp-without-endpoint:" + TelemetryOptions.MetricsExporterEnvVar, DiagnosticLevel.Warn,
                    "metric exporter 'otlp' has no endpoint and is ignored; set OTEL_EXPORTER_OTLP_ENDPOINT or OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
                    ("variable", TelemetryOptions.MetricsExporterEnvVar));
                var kept = fromEnv.Where(e => e != "otlp").ToArray();
                return kept.Length == 0 ? inferred : kept;
            }

            return fromEnv;
        }

        /// <summary>
        /// The disabled signals, as a comma-separated list (contract row "Deprecated
        /// <c>OTEL_HELPER_DISABLED_SIGNALS</c>", A19): code (trimmed, lowercased, de-duplicated;
        /// unknown entries fail at start) wins; else each entry of the deprecated variable, unless
        /// that signal's exporters were chosen (<paramref name="chosen"/>: its
        /// <c>OTEL_&lt;SIGNAL&gt;_EXPORTER</c> holds a valid entry, or for metrics the code option).
        /// An unknown entry warns once, echoing the entry.
        /// </summary>
        private static string ResolveDisabledSignals(TelemetryOptions code, IReadOnlyDictionary<string, bool> chosen, IOtelHelperDiagnostics diagnostics)
        {
            // Set in code wins whatever its value: an empty or blank string disables nothing and
            // still beats the deprecated variable (Go disabledSignalsSet, Python "is not None").
            if (code.DisabledSignals is { } fromCode)
            {
                return string.Join(",", SplitList(fromCode).Distinct());
            }

            if (ContractText.NonBlank(Env(TelemetryOptions.DisabledSignalsEnvVar)) is not { } raw)
            {
                return "";
            }

            var disabled = new List<string>();
            foreach (var entry in SplitList(raw).Distinct())
            {
                if (!ResolvedTelemetry.Signals.Contains(entry))
                {
                    diagnostics.ReportOnce($"invalid:{TelemetryOptions.DisabledSignalsEnvVar}:{entry}", DiagnosticLevel.Warn,
                        $"invalid {TelemetryOptions.DisabledSignalsEnvVar}: unknown signal ignored; valid values: {string.Join(", ", ResolvedTelemetry.Signals)}",
                        ("variable", TelemetryOptions.DisabledSignalsEnvVar), ("value", entry));
                }
                else if (!chosen[entry])
                {
                    disabled.Add(entry);
                }
            }

            return string.Join(",", disabled);
        }

        internal const long MaxExportIntervalMs = int.MaxValue;

        /// <summary>
        /// <c>OTEL_METRIC_EXPORT_INTERVAL</c> from the process environment (contract row "Metric
        /// export interval", A19): trimmed with the contract set, then <c>^\+?[0-9]+$</c> (ASCII
        /// digits only) of any length and above 0, else a warning and 30 000; above
        /// 2 147 483 647 clamped with a warning.
        /// </summary>
        internal static long ResolveEnvInterval(IOtelHelperDiagnostics diagnostics, IConfiguration? configuration = null)
        {
            const string variable = TelemetryOptions.MetricExportIntervalEnvVar;
            if (ContractText.NonBlank(Env(variable)) is not { } text)
            {
                // Principle 8: a value only in IConfiguration (appsettings) is not read (ADR-013),
                // and the SDK's own parse line for it is dropped as helper-owned, so say so once.
                if (configuration is not null && ContractText.NonBlank(configuration[variable]) is not null)
                {
                    diagnostics.ReportOnce("iconfiguration-only:" + variable, DiagnosticLevel.Warn,
                        $"{variable} is set only in IConfiguration and is not read; set it in the process environment or TelemetryOptions.ExportIntervalMs, using 30000",
                        ("variable", variable));
                }

                return TelemetryOptions.DefaultExportIntervalMs;
            }

            var digits = text.StartsWith('+') ? text[1..] : text;
            var significant = digits.TrimStart('0');
            if (digits.Length == 0 || !digits.All(c => c is >= '0' and <= '9') || significant.Length == 0)
            {
                WarnInvalidEnv(diagnostics, variable, text, "not a positive integer number of milliseconds, using 30000");
                return TelemetryOptions.DefaultExportIntervalMs;
            }

            // More than 10 significant digits is above 2 147 483 647 (10 digits); 10 fit in a long.
            if (significant.Length > 10 || long.Parse(significant, NumberStyles.None, CultureInfo.InvariantCulture) > MaxExportIntervalMs)
            {
                WarnInvalidEnv(diagnostics, variable, text, "above 2147483647 ms, clamped to 2147483647");
                return MaxExportIntervalMs;
            }

            return long.Parse(significant, NumberStyles.None, CultureInfo.InvariantCulture);
        }

        /// <summary>
        /// <c>OTEL_METRICS_EXEMPLAR_FILTER</c> from the process environment (ADR-013 exception to
        /// ADR-001): <c>always_on</c>, <c>always_off</c> or <c>trace_based</c> (contract trim set,
        /// ASCII case-insensitive); unset is <c>trace_based</c>, the spec default; anything else warns
        /// and is <c>trace_based</c>. The helper always sets the filter, because the .NET SDK's own
        /// default is <c>AlwaysOff</c>.
        /// </summary>
        internal static ExemplarFilterType ResolveExemplarFilter(IOtelHelperDiagnostics diagnostics)
        {
            if (ContractText.NonBlank(Env(ExemplarFilterEnvVar)) is not { } raw)
            {
                return ExemplarFilterType.TraceBased;
            }

            switch (ContractText.AsciiLower(raw))
            {
                case "always_on":
                    return ExemplarFilterType.AlwaysOn;
                case "always_off":
                    return ExemplarFilterType.AlwaysOff;
                case "trace_based":
                    return ExemplarFilterType.TraceBased;
                default:
                    WarnInvalidEnv(diagnostics, ExemplarFilterEnvVar, raw, "unsupported exemplar filter, using trace_based; valid values: always_on, always_off, trace_based");
                    return ExemplarFilterType.TraceBased;
            }
        }

        /// <summary>Standard variable of the Prometheus listener port (contract D1 row "Prometheus exporter variables", A29).</summary>
        internal const string PrometheusPortEnvVar = "OTEL_EXPORTER_PROMETHEUS_PORT";

        /// <summary>Standard variable of the Prometheus listener bind address (A29).</summary>
        internal const string PrometheusHostEnvVar = "OTEL_EXPORTER_PROMETHEUS_HOST";

        /// <summary>The A29 warning, the same text in every language: an invalid restriction widens exposure.</summary>
        internal const string InvalidPrometheusHostMessage =
            "invalid host, not applied: the listener binds every interface, wider than a restriction (use an IP address or localhost)";

        /// <summary>
        /// The listener port when code sets none: <c>OTEL_EXPORTER_PROMETHEUS_PORT</c> &gt;
        /// <c>OTEL_HELPER_METRICS_PORT</c> &gt; 9464 (A29). Both use the A21 grammar
        /// (<see cref="TryParseMetricsPort"/>); an empty or blank value is unset; an invalid value
        /// warns once naming the variable (the echoed value redacted) and the next level applies.
        /// </summary>
        private static int ResolveEnvPort(IOtelHelperDiagnostics diagnostics, out string source)
        {
            // What set the port, for the bind-failure line (contract A29: the default names the standard variable).
            source = PrometheusPortEnvVar;
            foreach (var (variable, next) in new[]
            {
                (PrometheusPortEnvVar, TelemetryOptions.MetricsPortEnvVar + ", else 9464"),
                (TelemetryOptions.MetricsPortEnvVar, "9464"),
            })
            {
                if (ContractText.NonBlank(Env(variable)) is not { } text)
                {
                    continue;
                }

                if (TryParseMetricsPort(text, out var port))
                {
                    source = variable;
                    return port;
                }

                WarnInvalidEnv(diagnostics, variable, text,
                    $"invalid port (ASCII digits with an optional +, in [0, 65535]; 0 = no listener), using {next}");
            }

            return TelemetryOptions.DefaultPrometheusMetricsPort;
        }

        /// <summary>
        /// The contract port grammar (D1 row "Metrics port grammar", A21): <c>^\+?[0-9]+$</c> on the
        /// trimmed value, ASCII digits only, in [0, 65535]; leading zeros allowed whatever the length
        /// (only the significant digits are converted, so a long digit string is never parsed).
        /// </summary>
        internal static bool TryParseMetricsPort(string text, out int port)
        {
            port = 0;
            var digits = text.StartsWith('+') ? text[1..] : text;
            if (digits.Length == 0 || !digits.All(char.IsAsciiDigit))
            {
                return false;
            }

            var significant = digits.TrimStart('0');
            if (significant.Length > 5)
            {
                return false;
            }

            port = significant.Length == 0 ? 0 : int.Parse(significant, NumberStyles.None, CultureInfo.InvariantCulture);
            return port <= 65535;
        }

        /// <summary>
        /// <c>OTEL_EXPORTER_PROMETHEUS_HOST</c> (A29): <c>""</c> = every interface (unset, blank,
        /// <c>::</c> or <c>[::]</c>), else the bind address as the contract snapshot spells it. Invalid
        /// values warn once and bind every interface (fails open, owner decision).
        /// </summary>
        private static string ResolvePrometheusHost(IOtelHelperDiagnostics diagnostics, out bool ipv6Unspecified)
        {
            ipv6Unspecified = false;
            if (ContractText.NonBlank(Env(PrometheusHostEnvVar)) is not { } text)
            {
                return "";
            }

            if (TryParsePrometheusHost(text, out var host))
            {
                // "::" or "[::]": every interface by the contract; .NET binds IPv4 only (warned at bind).
                ipv6Unspecified = host.Length == 0;
                return host;
            }

            WarnInvalidEnv(diagnostics, PrometheusHostEnvVar, text, InvalidPrometheusHostMessage);
            return "";
        }

        /// <summary>
        /// The contract host grammar (A29): <c>localhost</c> (ASCII case-insensitive) → <c>127.0.0.1</c>;
        /// an IPv4 dotted quad without leading zeros; an IPv6 literal with or without brackets and
        /// without a zone id (an IPv4-mapped one → its IPv4 address; the all-zero one → <c>""</c>,
        /// every interface; any other one ASCII-lowercased, brackets dropped, as written). Host names
        /// are not resolved. Mirrors Go's <c>parsePrometheusHost</c>.
        /// </summary>
        internal static bool TryParsePrometheusHost(string text, out string host)
        {
            host = "";
            if (ContractText.AsciiLower(text) == "localhost")
            {
                host = "127.0.0.1";
                return true;
            }

            var bracketed = text.Length >= 2 && text[0] == '[' && text[^1] == ']';
            var literal = bracketed ? text[1..^1] : text;
            if (!literal.Contains(':'))
            {
                // IPv4: brackets are for IPv6 only.
                if (bracketed || !IsDottedQuad(literal))
                {
                    return false;
                }

                host = literal;
                return true;
            }

            // IPv6: hex digits, ':' and the dots of an embedded IPv4 tail only (no zone, no prefix length).
            if (!literal.All(c => char.IsAsciiHexDigit(c) || c is ':' or '.')
                || !System.Net.IPAddress.TryParse(literal, out var address)
                || address.AddressFamily != System.Net.Sockets.AddressFamily.InterNetworkV6
                || (literal.Contains('.') && !IsDottedQuad(literal[(literal.LastIndexOf(':') + 1)..])))
            {
                return false;
            }

            if (address.IsIPv4MappedToIPv6)
            {
                host = address.MapToIPv4().ToString();
                return true;
            }

            host = address.Equals(System.Net.IPAddress.IPv6Any) ? "" : ContractText.AsciiLower(literal);
            return true;
        }

        /// <summary>Four decimal octets in [0, 255], no leading zeros (<c>0</c> itself is fine).</summary>
        private static bool IsDottedQuad(string text)
        {
            var parts = text.Split('.');
            return parts.Length == 4 && parts.All(p =>
                p.Length is >= 1 and <= 3 && p.All(char.IsAsciiDigit) && (p.Length == 1 || p[0] != '0')
                && int.Parse(p, NumberStyles.None, CultureInfo.InvariantCulture) <= 255);
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
        /// <c>IConfiguration</c>, else the environment), parsed as the SDK parses it
        /// (<c>bool.TryParse</c>: <c>true</c>/<c>false</c>, case-insensitive, whitespace trimmed),
        /// so the INFO line and the resolution agree with what the SDK does (ADR-013). Any other
        /// non-blank value warns naming the variable and means <c>false</c> (contract row "Boolean
        /// env value", R8); the SDK, too, leaves telemetry on.
        /// </summary>
        private static bool ReadSdkDisabled(IConfiguration? configuration, IOtelHelperDiagnostics diagnostics)
        {
            var raw = configuration is not null ? configuration[TelemetryOptions.SdkDisabledEnvVar] : Env(TelemetryOptions.SdkDisabledEnvVar);
            if (string.IsNullOrWhiteSpace(raw))
            {
                return false;
            }

            if (!bool.TryParse(raw, out var disabled))
            {
                WarnInvalidEnv(diagnostics, TelemetryOptions.SdkDisabledEnvVar, raw, "not a boolean (true or false), using false");
                return false;
            }

            return disabled;
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

        /// <summary>
        /// <c>OTEL_HELPER_HTTP_OPT_IN_METRICS</c> (contract D1 row "HTTP opt-in metrics", A30; grammar
        /// of row "Boolean env value"): <c>true</c> (ASCII case-insensitive, contract trim set) is true;
        /// unset or blank is false; anything other than <c>true</c>/<c>false</c> warns and is false.
        /// Read only when the code option is not set.
        /// </summary>
        private static bool ResolveEnvHttpOptInMetrics(IOtelHelperDiagnostics diagnostics)
        {
            if (ContractText.NonBlank(Env(TelemetryOptions.HttpOptInMetricsEnvVar)) is not { } raw)
            {
                return false;
            }

            var lower = ContractText.AsciiLower(raw);
            if (lower is not ("true" or "false"))
            {
                WarnInvalidEnv(diagnostics, TelemetryOptions.HttpOptInMetricsEnvVar, raw, "not a boolean (true or false), using false");
            }

            return lower == "true";
        }

        private static void WarnInvalidEnv(IOtelHelperDiagnostics diagnostics, string variable, string value, string why)
            => diagnostics.ReportOnce($"invalid:{variable}:{value}", DiagnosticLevel.Warn, $"invalid {variable}: {why}",
                ("variable", variable), ("value", value));

        // ── small helpers ──────────────────────────────────────────────────

        private static string? Env(string name) => System.Environment.GetEnvironmentVariable(name);

        private static bool EnvTrue(string name) => string.Equals(Env(name)?.Trim(), "true", StringComparison.OrdinalIgnoreCase);

        private static string? NonBlank(string? value) => string.IsNullOrWhiteSpace(value) ? null : value;
    }
}
