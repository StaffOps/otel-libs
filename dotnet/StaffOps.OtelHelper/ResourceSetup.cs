using System.Collections;
using System.Collections.Generic;
using System.Linq;
using OpenTelemetry.Resources;
using Microsoft.Extensions.DependencyInjection;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// The resource of the three providers (design D-NET7). In its <c>ConfigureResource</c>
    /// callback the helper builds a snapshot of what earlier callbacks put on the builder, keeps
    /// the attributes an application added (a value that differs from what the SDK's default
    /// builder produces for the same key), clears the builder and adds one resource, layered
    /// lowest to highest (contract row "telemetry.sdk.* and the resource schema URL", A16):
    /// <c>OTEL_RESOURCE_ATTRIBUTES</c> as the helper parsed it &lt; the kept application
    /// attributes (an application's own <c>ConfigureResource</c> is code) &lt; code
    /// <see cref="TelemetryOptions.ResourceAttributes"/> &lt; the resolved identity
    /// (<c>service.name</c>, <c>deployment.environment.name</c> when resolved,
    /// <c>service.instance.id</c>) &lt; the SDK's <c>telemetry.sdk.*</c>; schema URL
    /// <see cref="SchemaUrl"/>. The SDK's own environment detectors are cleared: the helper owns
    /// <c>OTEL_SERVICE_NAME</c> and <c>OTEL_RESOURCE_ATTRIBUTES</c> (ADR-013).
    /// </summary>
    internal static class ResourceSetup
    {
        /// <summary>
        /// The semantic-conventions schema the helper's resource declares: one literal in every
        /// language (contract design D1 row "telemetry.sdk.* and the resource schema URL"). The
        /// SDK 1.20 default builder declares 1.44.0; bumping this is a contract change.
        /// </summary>
        internal const string SchemaUrl = "https://opentelemetry.io/schemas/1.43.0";

        private static readonly IReadOnlyList<KeyValuePair<string, object>> TelemetrySdk =
            ResourceBuilder.CreateEmpty().AddTelemetrySdk().Build().Attributes.ToArray();

        /// <summary>
        /// The per-process <c>service.instance.id</c> (D4): the id the SDK's <c>AddService</c>
        /// generates once per process, so an application <c>AddService</c> and the helper agree.
        /// </summary>
        internal static readonly string ProcessInstanceId =
            ResourceBuilder.CreateEmpty().AddService("probe").Build().Attributes
                .Where(kv => kv.Key == TelemetryResolver.ServiceInstanceIdKey)
                .Select(kv => kv.Value as string)
                .FirstOrDefault(v => !string.IsNullOrEmpty(v))
            ?? System.Guid.NewGuid().ToString();

        internal static void Apply(ResourceBuilder resource, ResolvedTelemetry resolved, System.IServiceProvider services, string signal)
        {
            var diagnostics = services.GetRequiredService<IOtelHelperDiagnostics>();
            var state = services.GetService<AppResourceState>();
            Resource snapshot;
            try
            {
                snapshot = resource.Build();
            }
            catch (System.NotSupportedException)
            {
                // An earlier AddDetector(sp => …) factory detector cannot be built before the
                // provider sets the service provider: keep everything and add the helper's
                // attributes last (later wins), without clearing (D-NET7). No schema URL: the
                // kept SDK resources declare theirs (1.44.0), and a different one would be a merge conflict.
                var unresolvedEnvironment = resolved.Environment is null
                    ? "; deployment.environment.name is not set by the helper, so a value (also a blank one) from the SDK's own OTEL_RESOURCE_ATTRIBUTES parse stays"
                    : "";
                diagnostics.ReportOnce("resource:factory-detector", DiagnosticLevel.Warn,
                    "an earlier ConfigureResource added a resource detector factory (AddDetector(sp => ...)); the helper keeps the builder and adds its attributes last, so the SDK's own OTEL_RESOURCE_ATTRIBUTES and OTEL_SERVICE_NAME handling stays for keys the helper does not set" + unresolvedEnvironment);
                var fallback = Layered(resolved, kept: null, diagnostics, state);
                resource.AddAttributes(fallback);
                state?.Expect(signal, fallback, expectedSchemaUrl: null);
                return;
            }

            var layered = Layered(resolved, AppAttributes(snapshot), diagnostics, state);
            resource.Clear();
            resource.AddAttributes(layered, SchemaUrl);
            state?.Expect(signal, layered, SchemaUrl);
        }

        /// <summary>
        /// The snapshot attributes an application added: a key the SDK's default builder does not
        /// produce, or produces with another value. The baseline is built like the snapshot's
        /// default part (no service provider yet: the process environment).
        /// </summary>
        internal static Dictionary<string, object> AppAttributes(Resource snapshot)
        {
            var baseline = new Dictionary<string, object>(StringComparer.Ordinal);
            foreach (var kv in ResourceBuilder.CreateDefault().Build().Attributes)
            {
                baseline[kv.Key] = kv.Value;
            }

            var kept = new Dictionary<string, object>(StringComparer.Ordinal);
            foreach (var kv in snapshot.Attributes)
            {
                if (!baseline.TryGetValue(kv.Key, out var sdkValue) || !SameValue(sdkValue, kv.Value))
                {
                    kept[kv.Key] = kv.Value;
                }
            }

            return kept;
        }

        private static Dictionary<string, object> Layered(ResolvedTelemetry resolved, Dictionary<string, object>? kept, IOtelHelperDiagnostics diagnostics, AppResourceState? state)
        {
            var attributes = new Dictionary<string, object>(StringComparer.Ordinal);
            foreach (var kv in resolved.EnvResourceAttributes)
            {
                attributes[kv.Key] = kv.Value;
            }

            // Identity keys an earlier application ConfigureResource set (code level):
            // service.name is always the resolved one (the helper's source and meter carry it);
            // the environment and the instance id are kept when the helper has no value of its
            // own (none resolved / only the per-process id), else the resolved value wins. A
            // replaced value is warned once per key, naming the key only.
            string? appEnvironment = null;
            string? appInstanceId = null;
            if (kept is not null)
            {
                WarnReplaced(diagnostics, kept, TelemetryResolver.ServiceNameKey, resolved.ServiceName);
                appEnvironment = ContractText.NonBlank(kept.GetValueOrDefault(TelemetryResolver.EnvironmentKey) as string);
                if (appEnvironment is not null && resolved.Environment is not null)
                {
                    WarnReplaced(diagnostics, kept, TelemetryResolver.EnvironmentKey, resolved.Environment);
                    appEnvironment = null;
                }

                appInstanceId = ContractText.NonBlank(kept.GetValueOrDefault(TelemetryResolver.ServiceInstanceIdKey) as string);
                if (appInstanceId == ProcessInstanceId)
                {
                    // The id an app AddService generates: the same per-process id, nothing to keep.
                    appInstanceId = null;
                }
                else if (appInstanceId is not null && resolved.ServiceInstanceIdConfigured)
                {
                    WarnReplaced(diagnostics, kept, TelemetryResolver.ServiceInstanceIdKey, resolved.ServiceInstanceId);
                    appInstanceId = null;
                }

                foreach (var kv in kept)
                {
                    attributes[kv.Key] = kv.Value;
                }
            }

            foreach (var kv in resolved.ResourceAttributes)
            {
                attributes[kv.Key] = kv.Value;
            }

            attributes[TelemetryResolver.ServiceNameKey] = resolved.ServiceName;
            attributes[TelemetryResolver.ServiceInstanceIdKey] = appInstanceId ?? resolved.ServiceInstanceId;
            if (resolved.Environment is { } environment)
            {
                attributes[TelemetryResolver.EnvironmentKey] = environment;
            }
            else if (appEnvironment is not null)
            {
                // Used only when the helper resolves none (below every helper source): emitted (again
                // here, as a blank code attribute for the key would otherwise hide it), and the log
                // level follows it.
                attributes[TelemetryResolver.EnvironmentKey] = appEnvironment;
                state?.SetAppEnvironment(appEnvironment);
            }
            else
            {
                // Absent (ADR-003): also a blank value from the env, code or app attributes.
                attributes.Remove(TelemetryResolver.EnvironmentKey);
                // In a host the warning is written at start, once all three resources are built
                // (AppResourceState.CheckBuiltResources); hostless, here, unless an earlier
                // ConfigureResource already gave another signal's resource an environment.
                if (state is { HasHost: false, AppEnvironment: null })
                {
                    WarnEnvironmentNotSet(diagnostics);
                }
            }

            foreach (var kv in TelemetrySdk)
            {
                attributes[kv.Key] = kv.Value;
            }

            return attributes;
        }

        internal static void WarnEnvironmentNotSet(IOtelHelperDiagnostics diagnostics)
            => diagnostics.ReportOnce("identity:" + TelemetryResolver.EnvironmentKey, DiagnosticLevel.Warn,
                $"{TelemetryResolver.EnvironmentKey} is not set; set it in {TelemetryResolver.ResourceAttributesEnvVar} (e.g. {TelemetryResolver.EnvironmentKey}=PRD); log level defaults to INFO",
                ("using", "absent"));

        private static void WarnReplaced(IOtelHelperDiagnostics diagnostics, Dictionary<string, object> kept, string key, string resolvedValue)
        {
            if (kept.TryGetValue(key, out var appValue) && !SameValue(appValue, resolvedValue))
            {
                diagnostics.ReportOnce("resource:app-" + key, DiagnosticLevel.Warn,
                    $"{key} set by an earlier ConfigureResource is replaced by the helper's resolved value; set it in TelemetryOptions or the environment instead",
                    ("key", key));
            }
        }

        private static readonly string[] CheckedKeys =
        {
            TelemetryResolver.ServiceNameKey, TelemetryResolver.ServiceInstanceIdKey, TelemetryResolver.EnvironmentKey,
            "telemetry.sdk.name", "telemetry.sdk.language", "telemetry.sdk.version",
        };

        /// <summary>
        /// What changed in a built provider's resource against what the helper set for it: the
        /// identity keys, <c>telemetry.sdk.*</c> and the schema URL. A key the helper set with
        /// another value, or gone; after a rebuild (schema URL known) also a key added. On the
        /// factory path the SDK's own detectors stay, so only the keys the helper set count. A
        /// missing schema URL is not listed: the SDK's merge-conflict notice (event 59, a helper
        /// warning) explains it; a different non-empty one is.
        /// </summary>
        internal static List<string> Changed(IReadOnlyDictionary<string, object> expected, string? expectedSchemaUrl, Resource actual)
        {
            var got = new Dictionary<string, object>(StringComparer.Ordinal);
            foreach (var kv in actual.Attributes)
            {
                got[kv.Key] = kv.Value;
            }

            var changed = new List<string>();
            foreach (var key in CheckedKeys)
            {
                var hasWant = expected.TryGetValue(key, out var want);
                var hasGot = got.TryGetValue(key, out var value);
                if (hasWant ? !hasGot || !SameValue(want, value) : expectedSchemaUrl is not null && hasGot)
                {
                    changed.Add(key);
                }
            }

            if (expectedSchemaUrl is not null && actual.SchemaUrl is { } schema && schema != expectedSchemaUrl)
            {
                changed.Add("schema_URL");
            }

            return changed;
        }

        private static bool SameValue(object? a, object? b)
        {
            if (Equals(a, b))
            {
                return true;
            }

            return a is IEnumerable left && b is IEnumerable right && a is not string && b is not string
                && left.Cast<object?>().SequenceEqual(right.Cast<object?>());
        }
    }
}
