using System.Diagnostics;
using System.Diagnostics.Metrics;
using StaffOps.OtelHelper;
using StaffOps.OtelHelper.Diagnostics;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

[ResetGlobalPropagation]
public class RegistrationTests
{
    [Fact]
    public void AddOtelHelper_Registers_Options()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper(opts =>
        {
            opts.ServiceName = "test-svc";
        });

        var provider = services.BuildServiceProvider();
        var options = provider.GetRequiredService<IOptions<TelemetryOptions>>();

        Assert.Equal("test-svc", options.Value.ServiceName);
        Assert.Equal("test-svc", provider.GetRequiredService<ResolvedTelemetry>().ServiceName);
    }

    [Fact]
    public void AddOtelHelper_Parameterless_Uses_Defaults()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper();

        var provider = services.BuildServiceProvider();
        // Options hold only code values; the resolved state holds the defaults.
        Assert.Null(provider.GetRequiredService<IOptions<TelemetryOptions>>().Value.ServiceName);
        Assert.NotEmpty(provider.GetRequiredService<ResolvedTelemetry>().ServiceName);
    }

    [Fact]
    public void AddOtelHelper_Registers_Validator()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper();

        var provider = services.BuildServiceProvider();
        var validators = provider.GetServices<IValidateOptions<TelemetryOptions>>();

        Assert.Contains(validators, v => v is TelemetryOptionsValidator);
    }

    [Fact]
    public void Second_Call_Changes_Nothing_And_Warns_Once()
    {
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddOtelHelper(o => o.ServiceName = "first");
        var count = services.Count;

        services.AddOtelHelper(o => o.ServiceName = "second");
        services.AddOtelHelper(o => o.ServiceName = "third");

        // One marker for the warning, nothing else.
        Assert.Equal(count + 1, services.Count);
        Assert.Single(diagnostics.Lines, l => l.Contains("AddOtelHelper was called more than once"));
        Assert.StartsWith("otelhelper: WARN ", diagnostics.Lines[0]);
        using var provider = services.BuildServiceProvider();
        Assert.Equal("first", provider.GetRequiredService<ResolvedTelemetry>().ServiceName);
    }

    [Fact]
    public void Second_Call_Without_A_Registered_Sink_Uses_The_Fallback()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper();
        var count = services.Count;
        services.AddOtelHelper();
        Assert.Equal(count + 1, services.Count);
    }

    /// <summary>DN5: keyed ActivitySource and Meter; unkeyed injection resolves to null in a worker.</summary>
    [Fact]
    public void Instrumentation_Is_Keyed_Not_Unkeyed()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper(o => o.ServiceName = "keyed-svc");
        using var provider = services.BuildServiceProvider();

        Assert.Equal("keyed-svc", provider.GetRequiredKeyedService<ActivitySource>(OtelHelperKeys.Instrumentation).Name);
        Assert.Equal("keyed-svc", provider.GetRequiredKeyedService<Meter>(OtelHelperKeys.Instrumentation).Name);
        // DN5: the helper registers no unkeyed ActivitySource or Meter (in a worker both are null).
        Assert.Null(provider.GetService<ActivitySource>());
        Assert.Null(provider.GetService<Meter>());
    }

    [Fact]
    public void Late_Configure_Is_Honored()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper(o => o.ServiceName = "early");
        services.Configure<TelemetryOptions>(o => o.ServiceName = "late");
        using var provider = services.BuildServiceProvider();
        Assert.Equal("late", provider.GetRequiredService<ResolvedTelemetry>().ServiceName);
    }
}
