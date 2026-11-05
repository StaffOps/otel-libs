using System.Reflection;
using System.Runtime.Versioning;
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
/// The runtime-major vs TFM-major assertion is what catches a test leg that
/// rolled forward to another runtime; the name assertions then check the
/// names that runtime produces.
/// </summary>
[Collection("EnvVarTests")]
public class RuntimeMetricNamesTests : IDisposable
{
    private const string LegacyPrefix = "process.runtime.dotnet.";
    private const string RuntimeMeterPrefix = "dotnet.";

    // A set OTEL_HELPER_DISABLED_METRICS could drop the instruments under test.
    private readonly string? _savedDisabledMetrics =
        Environment.GetEnvironmentVariable(TelemetryOptions.DisabledMetricsEnvVar);

    public RuntimeMetricNamesTests() =>
        Environment.SetEnvironmentVariable(TelemetryOptions.DisabledMetricsEnvVar, null);

    public void Dispose() =>
        Environment.SetEnvironmentVariable(TelemetryOptions.DisabledMetricsEnvVar, _savedDisabledMetrics);

    [Fact]
    public void RuntimeMetricNames_FollowTheRuntimeVersion()
    {
        // The leg must run on the runtime of the TFM it was compiled for; a
        // net8.0 assembly rolled forward to runtime 10 fails here.
        var attribute = typeof(RuntimeMetricNamesTests).Assembly
            .GetCustomAttribute<TargetFrameworkAttribute>();
        Assert.NotNull(attribute);
        var tfmMajor = new FrameworkName(attribute!.FrameworkName).Version.Major;
        Assert.Equal(tfmMajor, Environment.Version.Major);

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
