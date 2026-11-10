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
        /// <summary>Health paths whose spans are not recorded when <see cref="TelemetryOptions.HealthPaths"/> is not set.</summary>
        internal static readonly string[] DefaultHealthPaths = { "/ping", "/health", "/healthz", "/ready" };

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

            var identity = ResolveIdentity(code, diagnostics);
            var environment = identity.Environment;
            var debug = code.DebugLevel ?? EnvTrue(TelemetryOptions.DebugLevelEnvVar);
            var sdkDisabled = ReadSdkDisabled(configuration);
            if (sdkDisabled)
            {
                diagnostics.ReportOnce("info:" + TelemetryOptions.SdkDisabledEnvVar, DiagnosticLevel.Info,
                    "OTEL_SDK_DISABLED=true: telemetry disabled, only propagators installed");
            }

            var hasMetricsEndpoint = targets.ContainsKey("metrics");
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
                ExportTimeoutMs = code.ExportTimeoutMs ?? TelemetryOptions.DefaultExportTimeoutMs,
                Compression = ResolveCompression(code, diagnostics),
                Headers = NonBlank(Env("OTEL_EXPORTER_OTLP_HEADERS")),
                CertificatePath = NonBlank(Env("OTEL_EXPORTER_OTLP_CERTIFICATE")),
                ClientCertificatePath = NonBlank(Env("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE")),
                ClientKeyPath = NonBlank(Env("OTEL_EXPORTER_OTLP_CLIENT_KEY")),
                Sampler = ResolveSampler(code),
                MinimumLogLevel = code.MinimumLogLevel ?? TelemetryOptions.GetDefaultLogLevel(environment, debug),
                ResourceAttributes = new Dictionary<string, object>(code.ResourceAttributes ?? new Dictionary<string, object>()),
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

        internal const string ServiceNameKey = "service.name";
        internal const string EnvironmentKey = "deployment.environment.name";
        internal const string ServiceInstanceIdKey = "service.instance.id";
        internal const string ResourceAttributesEnvVar = "OTEL_RESOURCE_ATTRIBUTES";

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
    }
}
