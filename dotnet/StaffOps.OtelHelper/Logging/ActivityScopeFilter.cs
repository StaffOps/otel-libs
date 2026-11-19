using System;
using System.Diagnostics;
using System.Collections.Generic;
using System.Linq;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using OpenTelemetry.Logs;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper.Logging
{
    /// <summary>
    /// The scope provider the SDK's logger provider reads (design D-NET10 (g), task C17 fix round 1):
    /// the logger factory's own, without the activity-tracking scope. That scope
    /// (<c>ActivityTrackingOptions</c>, set by the generic host to <c>SpanId | TraceId | ParentId</c>)
    /// repeats the record's own trace context as attributes; Go, Python and Node.js emit no such
    /// attributes. Every other scope, the application's included, is passed on unchanged.
    /// </summary>
    internal sealed class ActivityScopeFilter : IExternalScopeProvider
    {
        /// <summary>The <see cref="Activity"/> custom property under which the logger factory keeps the scope it built.</summary>
        internal const string ActivityLogScopeProperty = "__ActivityLogScope__";

        /// <summary>The runtime's (internal) type of that scope.</summary>
        internal const string ActivityLogScopeTypeName = "Microsoft.Extensions.Logging.LoggerFactoryScopeProvider+ActivityLogScope";

        private readonly IExternalScopeProvider _inner;

        internal ActivityScopeFilter(IExternalScopeProvider inner) => _inner = inner;

        /// <inheritdoc />
        public IDisposable Push(object? state) => _inner.Push(state);

        /// <inheritdoc />
        public void ForEachScope<TState>(Action<object?, TState> callback, TState state)
            => _inner.ForEachScope(
                static (scope, s) =>
                {
                    if (!IsActivityTrackingScope(scope))
                    {
                        s.callback(scope, s.state);
                    }
                },
                (callback, state));

        /// <summary>
        /// True only for the object the logger factory created for activity tracking: the instance
        /// it stored on the current activity, or an instance of its type. Decided by origin, never by
        /// key: an application scope named <c>SpanId</c> is kept. When a runtime changes both, the
        /// scope passes (the attributes come back), it is never an application value that is dropped.
        /// </summary>
        internal static bool IsActivityTrackingScope(object? scope)
            => scope is not null
                && (ReferenceEquals(scope, Activity.Current?.GetCustomProperty(ActivityLogScopeProperty))
                    || scope.GetType().FullName == ActivityLogScopeTypeName);
    }

    /// <summary>
    /// Installs <see cref="ActivityScopeFilter"/> on the SDK's logger provider. A logger provider
    /// registered after the SDK's: the logger factory hands every provider its scope provider in
    /// registration order, so when this one receives it the SDK's provider already has it and is
    /// given the filtered one instead. Writes nothing (its loggers are the null logger).
    /// </summary>
    internal sealed class ActivityScopeFilterInstaller : ILoggerProvider, ISupportExternalScope
    {
        private readonly IServiceProvider _services;
        private readonly Func<IServiceProvider, IEnumerable<ILoggerProvider>> _targets;

        internal ActivityScopeFilterInstaller(IServiceProvider services)
            : this(services, sp => sp.GetServices<ILoggerProvider>().OfType<OpenTelemetryLoggerProvider>())
        {
        }

        /// <summary>For tests: <paramref name="targets"/> picks the providers to install the filter on.</summary>
        internal ActivityScopeFilterInstaller(IServiceProvider services, Func<IServiceProvider, IEnumerable<ILoggerProvider>> targets)
        {
            _services = services;
            _targets = targets;
        }

        /// <inheritdoc />
        public void SetScopeProvider(IExternalScopeProvider scopeProvider)
        {
            var filtered = new ActivityScopeFilter(scopeProvider);
            var targets = _targets(_services).ToList();
            var installed = 0;
            foreach (var provider in targets)
            {
                if (provider is ISupportExternalScope scopeAware)
                {
                    scopeAware.SetScopeProvider(filtered);
                    installed++;
                }
            }

            // An SDK provider that takes no scope provider (not the case in OpenTelemetry 1.20):
            // the filter cannot apply, so say what will be exported.
            if (targets.Count > 0 && installed == 0)
            {
                var diagnostics = _services.GetService<IOtelHelperDiagnostics>() ?? OtelHelperDiagnostics.Fallback;
                diagnostics.ReportOnce("activity-scope-filter", DiagnosticLevel.Warn,
                    "the OpenTelemetry logger provider accepts no scope provider: the activity-tracking scope (SpanId, TraceId, ParentId) will be exported as log attributes");
            }
        }

        /// <inheritdoc />
        public ILogger CreateLogger(string categoryName) => NullLogger.Instance;

        /// <inheritdoc />
        public void Dispose()
        {
        }
    }
}
