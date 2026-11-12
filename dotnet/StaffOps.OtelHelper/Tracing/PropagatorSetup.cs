using System.Diagnostics;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using OpenTelemetry;
using OpenTelemetry.Context.Propagation;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper.Tracing
{
    /// <summary>
    /// Installs the resolved propagation (design D-NET9), also under <c>OTEL_SDK_DISABLED=true</c>.
    /// Three things carry context on .NET, and all three follow <c>OTEL_PROPAGATORS</c> in both directions:
    /// <list type="bullet">
    /// <item>the OpenTelemetry default propagator (<see cref="Sdk.SetDefaultTextMapPropagator"/>), used by
    /// the instrumentations and by application code;</item>
    /// <item>the runtime's <see cref="DistributedContextPropagator.Current"/>, with which <c>HttpClient</c>
    /// writes context on its own: set to <see cref="HelperRuntimePropagator.Global"/>;</item>
    /// <item>the <see cref="DistributedContextPropagator"/> service ASP.NET Core extracts inbound context
    /// with: replaced in the container by <see cref="Register"/> (the web host adds the runtime default
    /// with <c>TryAddSingleton</c>, before or after the application's services; both orders are tested).</item>
    /// </list>
    /// With traces disabled or the SDK disabled, a trace started here carries no context and a trace
    /// continued here is passed on unchanged (Go's and Python's no-op tracer). The settings are
    /// process-wide: the last container resolved wins, and nothing is restored when it is disposed.
    /// An application that set its own <see cref="DistributedContextPropagator.Current"/> keeps it (one
    /// warning when the settings are not the default). A <c>SocketsHttpHandler</c> takes the runtime
    /// propagator when it is created.
    /// </summary>
    internal static class PropagatorSetup
    {
        private static readonly object Gate = new();

        /// <summary>Marks a container whose own <see cref="DistributedContextPropagator"/> service was kept.</summary>
        internal sealed class ApplicationServiceKept
        {
        }

        /// <summary>
        /// Eager part (from <c>AddOtelHelper</c>). (1) <see cref="DistributedContextPropagator.Current"/>
        /// becomes <see cref="HelperRuntimePropagator.Global"/> now, with the token settings of
        /// <c>OTEL_PROPAGATORS</c> (environment only), so an <c>HttpClient</c> created before the host
        /// starts follows them too; the rest (traces or SDK disabled) is refined at resolution, and the
        /// handler reads the settings at each request. (2) The container's
        /// <see cref="DistributedContextPropagator"/> service (ASP.NET Core extracts with it) is the helper's,
        /// its settings the container's resolved values. An application propagator — a
        /// <see cref="DistributedContextPropagator.Current"/> it set before, or a service it registered —
        /// is kept; the resolution warns when the settings are not the default. A <c>Current</c> the
        /// application sets after <c>AddOtelHelper</c> is kept for handlers created later (warned the
        /// same way); handlers created before keep the helper's.
        /// </summary>
        internal static void Register(IServiceCollection services)
        {
            var currentIsOurs = HelperRuntimePropagator.IsRuntimeDefault(DistributedContextPropagator.Current) || DistributedContextPropagator.Current is HelperRuntimePropagator;
            if (currentIsOurs)
            {
                var names = PropagatorResolver.Resolve(new OtelHelperDiagnostics(() => TextWriter.Null));
                lock (Gate)
                {
                    HelperRuntimePropagator.GlobalSettings = new PropagationSettings(names.Contains("tracecontext"), names.Contains("baggage"), false);
                    DistributedContextPropagator.Current = HelperRuntimePropagator.Global;
                }
            }

            var existing = services.LastOrDefault(d => d.ServiceType == typeof(DistributedContextPropagator) && !d.IsKeyedService);
            var replaceable = existing is null
                || (existing.ImplementationInstance is DistributedContextPropagator instance
                    && (HelperRuntimePropagator.IsRuntimeDefault(instance) || instance is HelperRuntimePropagator));
            if (!replaceable)
            {
                services.TryAddSingleton<ApplicationServiceKept>();
                return;
            }

            if (!currentIsOurs)
            {
                return;
            }

            services.RemoveAll<DistributedContextPropagator>();
            services.AddSingleton<DistributedContextPropagator>(sp =>
            {
                var settings = ResolvedAccess.TryGet(sp, out var resolved) ? PropagationSettings.From(resolved) : PropagationSettings.Default;
                return new HelperRuntimePropagator(() => settings);
            });
        }

        internal static void Install(ResolvedTelemetry resolved, IOtelHelperDiagnostics diagnostics, bool applicationServiceKept = false)
        {
            var settings = PropagationSettings.From(resolved);
            if (applicationServiceKept && !settings.IsDefault)
            {
                diagnostics.ReportOnce("application-propagator-service", DiagnosticLevel.Warn,
                    $"the application registered its own DistributedContextPropagator service; ASP.NET Core extracts inbound context with it, not as {PropagatorResolver.PropagatorsEnvVar} says",
                    ("variable", PropagatorResolver.PropagatorsEnvVar));
            }

            lock (Gate)
            {
                Sdk.SetDefaultTextMapPropagator(Create(resolved.Propagators, settings.PassThroughOnly));
                HelperRuntimePropagator.GlobalSettings = settings;
                var current = DistributedContextPropagator.Current;
                if (current is HelperRuntimePropagator)
                {
                    return;
                }

                if (HelperRuntimePropagator.IsRuntimeDefault(current))
                {
                    DistributedContextPropagator.Current = HelperRuntimePropagator.Global;
                    return;
                }

                if (!settings.IsDefault)
                {
                    diagnostics.ReportOnce("runtime-propagator", DiagnosticLevel.Warn,
                        $"the application replaced DistributedContextPropagator.Current; HttpClient keeps writing context with it, not as {PropagatorResolver.PropagatorsEnvVar} and the disabled signals say",
                        ("variable", PropagatorResolver.PropagatorsEnvVar));
                }
            }
        }

        /// <summary>
        /// One propagator for one name, a composite otherwise, an empty composite for none. A lone
        /// <c>tracecontext</c> is the SDK's <see cref="TraceContextPropagator"/>, which the HTTP
        /// instrumentation recognizes and leaves the header to the runtime (a composite of one gives
        /// the same headers on the wire, measured in C10; the plain type keeps that upstream path).
        /// With <paramref name="passThroughOnly"/> the trace context is the no-op tracer's
        /// (<see cref="PassThroughPropagator"/>).
        /// </summary>
        internal static TextMapPropagator Create(IReadOnlyList<string> names, bool passThroughOnly = false)
        {
            var propagators = names.Select(name => name == "tracecontext" ? (TextMapPropagator)new TraceContextPropagator() : new BaggagePropagator()).ToList();
            var propagator = propagators.Count == 1 ? propagators[0] : new CompositeTextMapPropagator(propagators);
            return passThroughOnly && names.Contains("tracecontext") ? new PassThroughPropagator(propagator) : propagator;
        }

        /// <summary>
        /// Traces or the SDK disabled: injecting the current activity's context writes what a no-op span
        /// would carry — the inbound context of a continued trace, nothing for a trace started here.
        /// Any other context is injected as given; extraction is the inner propagator's.
        /// </summary>
        internal sealed class PassThroughPropagator : TextMapPropagator
        {
            private readonly TextMapPropagator _inner;

            internal PassThroughPropagator(TextMapPropagator inner) => _inner = inner;

            public override ISet<string>? Fields => _inner.Fields;

            public override void Inject<T>(PropagationContext context, T carrier, Action<T, string, string> setter)
            {
                if (HealthMatching.IsClientHealthRequest(carrier))
                {
                    // A health request carries no trace context, also when its activity is not
                    // sampled (the HttpClient filter then does not run; C11 fix round 2).
                    context = new PropagationContext(default, context.Baggage);
                }
                else if (Activity.Current is { } current && current.Context == context.ActivityContext)
                {
                    context = new PropagationContext(PropagationSettings.PassThrough(current), context.Baggage);
                }

                _inner.Inject(context, carrier, setter);
            }

            public override PropagationContext Extract<T>(PropagationContext context, T carrier, Func<T, string, IEnumerable<string>?> getter)
                => _inner.Extract(context, carrier, getter);
        }
    }
}
