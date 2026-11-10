using Microsoft.Extensions.Configuration;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// A diagnostics sink of its own per test (design D-NET5: never Console.SetError, which
/// races under parallel xUnit), plus resolution helpers.
/// </summary>
internal sealed class TestDiagnostics
{
    private readonly StringWriter _writer = new();

    public TestDiagnostics()
    {
        Sink = new OtelHelperDiagnostics(() => _writer);
    }

    public OtelHelperDiagnostics Sink { get; }

    /// <summary>Everything the sink wrote.</summary>
    public string Text => _writer.ToString();

    /// <summary>The written lines.</summary>
    public string[] Lines => Text.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

    /// <summary>Resolves code options against the current process environment.</summary>
    public ResolvedTelemetry Resolve(Action<TelemetryOptions>? configure = null, IConfiguration? configuration = null)
    {
        var options = new TelemetryOptions();
        configure?.Invoke(options);
        return TelemetryResolver.Resolve(options, Sink, configuration);
    }

    /// <summary>Resolves with a throwaway sink.</summary>
    public static ResolvedTelemetry ResolveQuiet(Action<TelemetryOptions>? configure = null)
        => new TestDiagnostics().Resolve(configure);
}

/// <summary>Builds the helper on a plain service collection, the providers already built.</summary>
internal static class TestPipelines
{
    public static Microsoft.Extensions.DependencyInjection.ServiceProvider Build(Action<TelemetryOptions> configure, TestDiagnostics? diagnostics = null)
    {
        var services = new Microsoft.Extensions.DependencyInjection.ServiceCollection();
        Microsoft.Extensions.DependencyInjection.ServiceCollectionServiceExtensions.AddSingleton<IOtelHelperDiagnostics>(services, (diagnostics ?? new TestDiagnostics()).Sink);
        services.AddOtelHelper(configure);
        var provider = Microsoft.Extensions.DependencyInjection.ServiceCollectionContainerBuilderExtensions.BuildServiceProvider(services);
        Microsoft.Extensions.DependencyInjection.ServiceProviderServiceExtensions.GetRequiredService<OpenTelemetry.Trace.TracerProvider>(provider);
        Microsoft.Extensions.DependencyInjection.ServiceProviderServiceExtensions.GetRequiredService<OpenTelemetry.Metrics.MeterProvider>(provider);
        return provider;
    }
}
