using System.Collections.Concurrent;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Microsoft.Extensions.Primitives;
using OpenTelemetry.Resources;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// What the providers' resources learned about the application's own resource configuration,
    /// one per container.
    /// <list type="bullet">
    /// <item>A <c>deployment.environment.name</c> that an earlier application <c>ConfigureResource</c>
    /// set is used only when the helper resolves none (below every helper source): it is emitted,
    /// and the default log level follows it. The resource is built after the host's first logger,
    /// so the level is re-applied through this change token
    /// (<c>IOptionsMonitor&lt;LoggerFilterOptions&gt;</c>, which <c>LoggerFactory</c> follows).</item>
    /// <item>What the helper set on each signal's resource, so the host-start check can compare it
    /// with the built provider (<see cref="CheckBuiltResources"/>).</item>
    /// </list>
    /// </summary>
    internal sealed class AppResourceState : IOptionsChangeTokenSource<LoggerFilterOptions>, IDisposable
    {
        private readonly ConcurrentDictionary<string, (IReadOnlyDictionary<string, object> Attributes, string? SchemaUrl)> _expected = new(StringComparer.Ordinal);
        private string? _environment;
        private CancellationTokenSource _changed = new();

        public AppResourceState(IServiceProvider services)
        {
            // A host runs the helper's hosted service, which checks the built resources at start.
            // Asked without constructing it: the host lifetime needs a logger, whose filters read
            // this state (resolving it here deadlocks host startup).
            HasHost = services.GetService(typeof(Microsoft.Extensions.DependencyInjection.IServiceProviderIsService)) is Microsoft.Extensions.DependencyInjection.IServiceProviderIsService isService
                && isService.IsService(typeof(Microsoft.Extensions.Hosting.IHostApplicationLifetime));
        }

        public string? Name => Options.DefaultName;

        /// <summary>True in a host: the start-time check writes the resource diagnostics.</summary>
        internal bool HasHost { get; }

        /// <summary>The environment the application's resource set, when the helper resolved none.</summary>
        public string? AppEnvironment => Volatile.Read(ref _environment);

        public IChangeToken GetChangeToken() => new CancellationChangeToken(Volatile.Read(ref _changed).Token);

        /// <summary>Records the application's environment (the first one wins) and refreshes the logger filters.</summary>
        internal void SetAppEnvironment(string environment)
        {
            if (Interlocked.CompareExchange(ref _environment, environment, null) is not null)
            {
                return;
            }

            var previous = Interlocked.Exchange(ref _changed, new CancellationTokenSource());
            previous.Cancel();
            previous.Dispose();
        }

        /// <summary>Records what the helper set on <paramref name="signal"/>'s resource (null schema URL: the factory path).</summary>
        internal void Expect(string signal, IReadOnlyDictionary<string, object> attributes, string? expectedSchemaUrl)
            => _expected[signal] = (attributes, expectedSchemaUrl);

        /// <summary>
        /// Compares each built provider's resource with what the helper set for its signal and
        /// warns once naming the signals and the changed keys: a <c>ConfigureResource</c> or
        /// <c>SetResourceBuilder</c> registered after <c>AddOtelHelper()</c> runs after the helper
        /// (SDK 1.20.1 has no later hook). Called at host start, after the providers are built.
        /// </summary>
        internal void CheckBuiltResources(IReadOnlyDictionary<string, Resource?> built, ResolvedTelemetry resolved, Diagnostics.IOtelHelperDiagnostics diagnostics)
        {
            if (resolved.SdkDisabled)
            {
                // No resource is exported; only the identity diagnostic the resource would carry.
                if (resolved.Environment is null)
                {
                    ResourceSetup.WarnEnvironmentNotSet(diagnostics);
                }

                return;
            }

            CheckEnvironment(built, resolved, diagnostics);
            var signals = new List<string>();
            var keys = new List<string>();
            foreach (var signal in ResolvedTelemetry.Signals)
            {
                if (!built.TryGetValue(signal, out var resource) || resource is null || !resource.Attributes.Any() || !_expected.TryGetValue(signal, out var expected))
                {
                    continue;
                }

                var changed = ResourceSetup.Changed(expected.Attributes, expected.SchemaUrl, resource);
                if (changed.Count > 0)
                {
                    signals.Add(signal);
                    keys.AddRange(changed.Where(k => !keys.Contains(k)));
                }
            }

            if (signals.Count > 0)
            {
                diagnostics.ReportOnce("resource:later-change", Diagnostics.DiagnosticLevel.Warn,
                    "resource configuration registered after AddOtelHelper() (ConfigureResource or SetResourceBuilder) changed the helper's resource; register it before AddOtelHelper(), or set these values in TelemetryOptions",
                    ("signals", string.Join(",", signals)), ("changed", string.Join(",", keys)));
            }
        }

        /// <summary>
        /// The absent-environment warning, seen across the three built resources (ADR-003): "not
        /// set" only when none carries <c>deployment.environment.name</c>, one line naming the
        /// signals that lack it when only some do (an earlier <c>ConfigureResource</c> on some
        /// providers), nothing when all do.
        /// </summary>
        private static void CheckEnvironment(IReadOnlyDictionary<string, Resource?> built, ResolvedTelemetry resolved, Diagnostics.IOtelHelperDiagnostics diagnostics)
        {
            if (resolved.Environment is not null)
            {
                return;
            }

            var lacking = new List<string>();
            var carrying = 0;
            foreach (var signal in ResolvedTelemetry.Signals)
            {
                if (!built.TryGetValue(signal, out var resource) || resource is null || !resource.Attributes.Any())
                {
                    continue;
                }

                var has = resource.Attributes.Any(kv => kv.Key == TelemetryResolver.EnvironmentKey && ContractText.NonBlank(kv.Value as string) is not null);
                if (has)
                {
                    carrying++;
                }
                else
                {
                    lacking.Add(signal);
                }
            }

            if (carrying == 0)
            {
                ResourceSetup.WarnEnvironmentNotSet(diagnostics);
            }
            else if (lacking.Count > 0)
            {
                diagnostics.ReportOnce("identity:environment-differs", Diagnostics.DiagnosticLevel.Warn,
                    $"{TelemetryResolver.EnvironmentKey} differs between signals: only an earlier ConfigureResource set it, not for every signal; set it in {TelemetryResolver.ResourceAttributesEnvVar} or TelemetryOptions",
                    ("missing", string.Join(",", lacking)));
            }
        }

        public void Dispose() => Volatile.Read(ref _changed).Dispose();
    }
}
