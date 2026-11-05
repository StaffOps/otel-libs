using OpenTelemetry;
using OpenTelemetry.Metrics;
using OtelHelper.Metrics;
using Xunit;

namespace OtelHelper.Tests;

/// <summary>
/// The runtime metric names follow the runtime, not the target framework:
/// on .NET 8 OpenTelemetry.Instrumentation.Runtime emits
/// <c>process.runtime.dotnet.*</c>; on .NET 9+ it forwards to the built-in
/// <c>System.Runtime</c> meter, which emits <c>dotnet.*</c> (DN0, D-NET0).
/// Asserting both branches makes a test leg that silently ran on another
/// runtime fail.
/// </summary>
[Collection("EnvVarTests")]
public class RuntimeMetricNamesTests
{
    private const string LegacyPrefix = "process.runtime.dotnet.";
    private const string RuntimeMeterPrefix = "dotnet.";

    [Fact]
    public void RuntimeMetricNames_FollowTheRuntimeVersion()
    {
        var opts = new TelemetryOptions
        {
            ServiceName = "runtime-metrics-svc",
            MetricExporters = "none",
        };
        new TelemetryOptionsPostConfigure().PostConfigure(null, opts);

        var exported = new List<Metric>();
        using (var provider = Sdk.CreateMeterProviderBuilder()
                   .ConfigureMetrics(opts)
                   .AddInMemoryExporter(exported)
                   .Build())
        {
            // Allocate and collect so GC-based instruments have data points.
            GC.KeepAlive(new byte[1024 * 1024]);
            GC.Collect();
            provider.ForceFlush();
        }

        var names = exported.Select(m => m.Name).Distinct().ToList();
        var legacy = names.Where(n => n.StartsWith(LegacyPrefix, StringComparison.Ordinal)).ToList();
        var builtIn = names.Where(n => n.StartsWith(RuntimeMeterPrefix, StringComparison.Ordinal)).ToList();

        if (Environment.Version.Major < 9)
        {
            Assert.NotEmpty(legacy);
            Assert.Empty(builtIn);
        }
        else
        {
            Assert.NotEmpty(builtIn);
            Assert.Empty(legacy);
        }
    }
}
