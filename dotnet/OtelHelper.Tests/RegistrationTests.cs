using OtelHelper;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Xunit;

namespace OtelHelper.Tests;

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
    }

    [Fact]
    public void AddOtelHelper_Parameterless_Uses_Defaults()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper();

        var provider = services.BuildServiceProvider();
        var options = provider.GetRequiredService<IOptions<TelemetryOptions>>();

        Assert.NotNull(options.Value);
        Assert.NotEmpty(options.Value.ServiceName);
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
}
