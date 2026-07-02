using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace OtelHelper.Profiling
{
    /// <summary>
    /// Logs a one-time warning at startup when the native Pyroscope profiler
    /// prerequisites are missing, so the operator knows profiling will be a no-op.
    /// </summary>
    internal sealed class ProfilingPrerequisiteWarning : IHostedService
    {
        private readonly ILogger _logger;
        private readonly string _missing;

        public ProfilingPrerequisiteWarning(ILoggerFactory loggerFactory, string missing)
        {
            _logger = loggerFactory.CreateLogger("OtelHelper.Profiling");
            _missing = missing;
        }

        public Task StartAsync(CancellationToken cancellationToken)
        {
            _logger.LogWarning(
                "OtelHelper.Profiling: native Pyroscope profiler prerequisites missing ({Missing}). " +
                "Profiling will produce no data. Set these as process environment variables in the " +
                "deployment (Helm/Dockerfile). See the OtelHelper.Profiling README.",
                _missing);
            return Task.CompletedTask;
        }

        public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;
    }
}
