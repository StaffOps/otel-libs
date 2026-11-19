using Microsoft.Extensions.Hosting;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper.Profiling
{
    /// <summary>
    /// The prerequisites of one container, read once: the host start and the tracer provider
    /// build (which may come first) see the same answer.
    /// </summary>
    internal sealed class ProfilingState
    {
        private readonly Lazy<ProfilingPrerequisites> _prerequisites;

        public ProfilingState()
            : this(ProfilingPrerequisites.FromEnvironment)
        {
        }

        internal ProfilingState(Func<ProfilingPrerequisites> read)
        {
            _prerequisites = new Lazy<ProfilingPrerequisites>(read);
        }

        public ProfilingPrerequisites Prerequisites => _prerequisites.Value;
    }

    /// <summary>
    /// Starts the profiler when the host starts (task C15, N11), not at registration. When a
    /// prerequisite is not met it writes one <c>otelhelper: WARN</c> line through the helper's
    /// diagnostics sink (design D-NET5) and does not start the profiler; a start that throws
    /// becomes one <c>otelhelper: ERROR</c> line and the host keeps starting.
    /// </summary>
    internal sealed class ProfilingStartup : IHostedService
    {
        internal const string PrerequisitesOnceKey = "profiling-prerequisites";
        internal const string StartFailedOnceKey = "profiling-start-failed";

        private readonly ProfilingState _state;
        private readonly IProfilingProvider _provider;
        private readonly IOtelHelperDiagnostics _diagnostics;

        public ProfilingStartup(ProfilingState state, IProfilingProvider provider, IServiceProvider services)
        {
            _state = state;
            _provider = provider;
            // Without AddOtelHelper() there is no container sink: the registration-time one.
            _diagnostics = services.GetService(typeof(IOtelHelperDiagnostics)) as IOtelHelperDiagnostics ?? OtelHelperDiagnostics.Fallback;
        }

        public Task StartAsync(CancellationToken cancellationToken)
        {
            var prerequisites = _state.Prerequisites;
            if (!prerequisites.IsSatisfied)
            {
                _diagnostics.ReportOnce(PrerequisitesOnceKey, DiagnosticLevel.Warn,
                    $"profiling prerequisites not met ({string.Join(", ", prerequisites.Unmet)}): the helper does not start the Pyroscope profiler and spans get no pyroscope.profile.id; " +
                    "set these variables in the container environment (Helm values or Dockerfile), the runtime reads them when the process starts");
                return Task.CompletedTask;
            }

            try
            {
                _provider.Start();
            }
            catch (Exception ex)
            {
                // Pyroscope catches only DllNotFoundException; a native library of another version
                // throws EntryPointNotFoundException. The type only: the message may hold paths.
                _diagnostics.ReportOnce(StartFailedOnceKey, DiagnosticLevel.Error,
                    "the Pyroscope profiler could not be started: its profile types stay off and the application keeps running",
                    ("error.type", ex.GetType().FullName ?? ex.GetType().Name));
            }

            return Task.CompletedTask;
        }

        public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;
    }
}
