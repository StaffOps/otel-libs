using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry.Instrumentation.AspNetCore;
using OpenTelemetry.Instrumentation.Http;
using StaffOps.OtelHelper;

namespace StaffOps.OtelLibs.Demo;

/// <summary>The telemetry set-up every demo app shares.</summary>
public static class DemoHosting
{
    /// <summary>
    /// <c>AddOtelHelper()</c> with the demo scope added to the exported sources and meters; no
    /// resource attribute in code (DR0). With <c>DEMO_SUPPRESS_FRAMEWORK_EXCEPTIONS=true</c> the
    /// ASP.NET Core and HttpClient instrumentations record no exception events (DE-D8 reversal;
    /// unset by default, so the A37 gaps stay visible).
    /// </summary>
    public static IServiceCollection AddDemoTelemetry(this IServiceCollection services, Func<string, string?>? env = null)
    {
        services.AddOtelHelper(options =>
        {
            options.AdditionalActivitySources.Add(DemoTelemetry.ScopeName);
            options.AdditionalMeters.Add(DemoTelemetry.ScopeName);
        });

        if (DemoSettings.SuppressFrameworkExceptions(env))
        {
            services.Configure<AspNetCoreTraceInstrumentationOptions>(o => o.RecordException = false);
            services.Configure<HttpClientTraceInstrumentationOptions>(o => o.RecordException = false);
        }

        return services;
    }
}
