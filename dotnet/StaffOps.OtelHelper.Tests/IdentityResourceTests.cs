using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using OpenTelemetry;
using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Diagnostics;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C6, identity and resource (design D-NET7; contract R4, R5, D4, A16, A24): service name
/// and environment precedence, the R4 log levels, the helper's own <c>OTEL_RESOURCE_ATTRIBUTES</c>
/// parse, the rebuilt resource (earlier application configuration kept), the schema URL and the
/// <c>telemetry.sdk.*</c> rules.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public class IdentityResourceTests : IDisposable
{
    private static readonly string[] TouchedEnvVars =
    {
        "OTEL_SERVICE_NAME", "SERVICE_NAME", "ENVIRONMENT", "OTEL_RESOURCE_ATTRIBUTES", "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_HELPER_DEBUG_LEVEL", "OTEL_SDK_DISABLED",
    };

    public IdentityResourceTests() => ClearEnv();

    public void Dispose() => ClearEnv();

    private static void ClearEnv()
    {
        foreach (var v in TouchedEnvVars)
        {
            Environment.SetEnvironmentVariable(v, null);
        }
    }

    private static void Env(string name, string? value) => Environment.SetEnvironmentVariable(name, value);

    private static ResolvedTelemetry Resolve(Action<TelemetryOptions>? configure, out TestDiagnostics diagnostics)
    {
        diagnostics = new TestDiagnostics();
        return diagnostics.Resolve(configure);
    }

    private static ResolvedTelemetry Resolve(Action<TelemetryOptions>? configure = null) => Resolve(configure, out _);

    private static (ServiceProvider Provider, TestDiagnostics Diagnostics) Build(
        Action<TelemetryOptions> configure, Action<IServiceCollection>? before = null)
    {
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        before?.Invoke(services);
        services.AddOtelHelper(configure);
        return (services.BuildServiceProvider(), diagnostics);
    }

    private static Dictionary<string, object> TraceResource(ServiceProvider provider, out Resource resource)
    {
        resource = provider.GetRequiredService<TracerProvider>().GetResource();
        return resource.Attributes.ToDictionary(kv => kv.Key, kv => kv.Value);
    }

    private static Dictionary<string, object> TraceResource(ServiceProvider provider) => TraceResource(provider, out _);

    private static int Count(TestDiagnostics diagnostics, string fragment)
        => diagnostics.Lines.Count(l => l.Contains(fragment, StringComparison.Ordinal));

    // ── OTEL_RESOURCE_ATTRIBUTES parser (A16) ──────────────────────────────

    [Fact]
    public void Parser_Blank_Items_Are_Ignored_Without_A_Position()
    {
        var parsed = ResourceAttributesParser.Parse("a=1,, \t,b=2,");
        Assert.Equal(new Dictionary<string, string> { ["a"] = "1", ["b"] = "2" }, parsed.Attributes);
        Assert.Empty(parsed.Malformed);
        Assert.Empty(parsed.Undecodable);
    }

    [Fact]
    public void Parser_Skips_Items_Without_Equals_Or_With_An_Empty_Key_By_Position()
    {
        var parsed = ResourceAttributesParser.Parse("a=1,broken,=orphan, \t=x,b=2");
        Assert.Equal(new Dictionary<string, string> { ["a"] = "1", ["b"] = "2" }, parsed.Attributes);
        Assert.Equal(new[] { 2, 3, 4 }, parsed.Malformed);
    }

    [Theory]
    [InlineData("k=%20payments%09", "payments")]
    [InlineData("k=a+b", "a+b")]
    [InlineData("k=a=b=c", "a=b=c")]
    [InlineData("k=café%20bar", "café bar")]
    [InlineData("k=%C3%A9", "é")]
    [InlineData("k=%e2%82%ac", "€")]
    [InlineData("k= v ", " v ")]
    [InlineData(" k \t=  v ", "v")]
    public void Parser_Decodes_Strictly_Then_Trims_With_The_Contract_Set(string raw, string expected)
    {
        var parsed = ResourceAttributesParser.Parse(raw);
        Assert.Equal(expected, parsed.Attributes["k"]);
        Assert.Empty(parsed.Undecodable);
    }

    [Theory]
    [InlineData("k=%zz", "%zz")]
    [InlineData("k=%C3", "%C3")]
    [InlineData("k=%", "%")]
    [InlineData("k=ab%2", "ab%2")]
    [InlineData("k= %FF ", "%FF")]
    [InlineData("k=%C3%28", "%C3%28")]
    public void Parser_Keeps_An_Undecodable_Value_Literal_By_Position(string raw, string expected)
    {
        var parsed = ResourceAttributesParser.Parse("a=1," + raw);
        Assert.Equal(expected, parsed.Attributes["k"]);
        Assert.Equal(new[] { 2 }, parsed.Undecodable);
    }

    [Fact]
    public void Parser_Unpaired_Surrogates_Make_A_Key_Malformed_And_A_Value_Replaced()
    {
        var parsed = ResourceAttributesParser.Parse("bad\ud800key=v,k=x\udc00y,ok=1,k2=%41\ud800");
        Assert.Equal(new[] { 1 }, parsed.Malformed);
        Assert.Equal(new[] { 2, 4 }, parsed.Undecodable);
        Assert.Equal("x�y", parsed.Attributes["k"]);
        Assert.Equal("%41�", parsed.Attributes["k2"]);
        Assert.Equal("1", parsed.Attributes["ok"]);
        Assert.Equal("😀", ResourceAttributesParser.Parse("e=😀").Attributes["e"]);
    }

    [Fact]
    public void Parser_Keeps_A_Unicode_Space_In_The_Key_And_A_Later_Duplicate_Wins()
    {
        var parsed = ResourceAttributesParser.Parse(" team.name=a,x=1,x=2");
        Assert.True(parsed.Attributes.ContainsKey(" team.name"));
        Assert.False(parsed.Attributes.ContainsKey("team.name"));
        Assert.Equal("2", parsed.Attributes["x"]);
    }

    [Fact]
    public void Env_Resource_Attribute_Problems_Warn_Once_By_Position_Never_The_Value()
    {
        Env("OTEL_RESOURCE_ATTRIBUTES", "token-s3cr3t,api.key=%zzs3cr3t,team.name=payments");
        var resolved = Resolve(null, out var diagnostics);

        Assert.Equal("payments", resolved.EnvResourceAttributes["team.name"]);
        Assert.Equal("%zzs3cr3t", resolved.EnvResourceAttributes["api.key"]);
        Assert.DoesNotContain("s3cr3t", diagnostics.Text);
        var malformed = Assert.Single(diagnostics.Lines, l => l.Contains("not key=value", StringComparison.Ordinal));
        Assert.Contains("variable=OTEL_RESOURCE_ATTRIBUTES", malformed);
        Assert.Contains("items=1", malformed);
        var undecodable = Assert.Single(diagnostics.Lines, l => l.Contains("kept literally", StringComparison.Ordinal));
        Assert.Contains("items=2", undecodable);
    }

    [Fact]
    public void Blank_Env_Resource_Attributes_Warn_Nothing()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        Env("ENVIRONMENT", "DEV");
        Env("OTEL_RESOURCE_ATTRIBUTES", " \t");
        var resolved = Resolve(null, out var diagnostics);
        Assert.Empty(resolved.EnvResourceAttributes);
        Assert.Equal("", diagnostics.Text);
    }

    // ── service.name precedence (R5) ───────────────────────────────────────

    [Fact]
    public void ServiceName_Otel_Variable_Beats_The_Deprecated_One()
    {
        Env("OTEL_SERVICE_NAME", "svc-std");
        Env("SERVICE_NAME", "svc-legacy");
        Assert.Equal("svc-std", Resolve().ServiceName);
    }

    [Fact]
    public void ServiceName_Resource_Attributes_Beat_The_Deprecated_Variable_And_Lose_To_The_Otel_One()
    {
        Env("OTEL_RESOURCE_ATTRIBUTES", "service.name=svc-ra");
        Env("SERVICE_NAME", "svc-legacy");
        Assert.Equal("svc-ra", Resolve().ServiceName);

        Env("OTEL_SERVICE_NAME", " \t ");
        Assert.Equal("svc-ra", Resolve().ServiceName);

        Env("OTEL_SERVICE_NAME", " svc-std ");
        Assert.Equal("svc-std", Resolve().ServiceName);
    }

    [Fact]
    public void ServiceName_Code_Option_Beats_Code_Attribute_Which_Beats_Env()
    {
        Env("OTEL_SERVICE_NAME", "svc-std");
        Assert.Equal("svc-attr", Resolve(o => o.ResourceAttributes["service.name"] = " svc-attr ").ServiceName);
        Assert.Equal("svc-option", Resolve(o =>
        {
            o.ServiceName = "svc-option";
            o.ResourceAttributes["service.name"] = "svc-attr";
        }).ServiceName);
        Assert.Equal("svc-std", Resolve(o => o.ResourceAttributes["service.name"] = " ").ServiceName);
    }

    [Fact]
    public void ServiceName_Blank_Resource_Attribute_Falls_Back_To_The_Deprecated_Variable()
    {
        Env("OTEL_RESOURCE_ATTRIBUTES", "service.name= ,deployment.environment.name=DEV");
        Env("SERVICE_NAME", "svc-legacy");
        Assert.Equal("svc-legacy", Resolve().ServiceName);
    }

    [Fact]
    public void ServiceName_Unset_Is_The_Sdk_Default_With_One_Warning()
    {
        var resolved = Resolve(null, out var diagnostics);

        Assert.False(resolved.ServiceNameConfigured);
        Assert.Matches("^unknown_service(:.+)?$", resolved.ServiceName);
        Assert.Equal(TelemetryResolver.UnknownServiceName(), resolved.ServiceName);
        Assert.Equal(1, Count(diagnostics, "OTEL_SERVICE_NAME"));
        Assert.Contains("no service name configured", diagnostics.Text);
    }

    [Fact]
    public void ServiceName_From_A_Code_Attribute_Only_Warns_Nothing()
    {
        Env("ENVIRONMENT", "DEV");
        var resolved = Resolve(o => o.ResourceAttributes["service.name"] = "svc-code-attribute", out var diagnostics);
        Assert.True(resolved.ServiceNameConfigured);
        Assert.Equal("svc-code-attribute", resolved.ServiceName);
        Assert.Equal(0, Count(diagnostics, "OTEL_SERVICE_NAME"));
    }

    // ── deployment.environment.name and the log level (R4, ADR-003) ─────────

    [Fact]
    public void Environment_Unset_Is_Absent_Info_And_Warned_Once()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        var (provider, diagnostics) = Build(_ => { });
        using (provider)
        {
            var resolved = provider.GetRequiredService<ResolvedTelemetry>();
            Assert.Null(resolved.Environment);
            Assert.Equal(LogLevel.Information, resolved.MinimumLogLevel);
            var attributes = TraceResource(provider);
            provider.GetRequiredService<MeterProvider>();
            Assert.False(attributes.ContainsKey("deployment.environment.name"));
            Assert.Equal(1, Count(diagnostics, "deployment.environment.name"));
        }
    }

    [Theory]
    [InlineData("staging", "staging")]
    [InlineData("prd", "PRD")]
    [InlineData(" Hml ", "HML")]
    public void Environment_Alias_Canonicalizes_Known_Names_And_Keeps_Others(string alias, string expected)
    {
        Env("ENVIRONMENT", alias);
        Assert.Equal(expected, Resolve().Environment);
    }

    [Fact]
    public void Environment_Resource_Attributes_Beat_The_Alias_And_Are_Verbatim()
    {
        Env("ENVIRONMENT", "DEV");
        Env("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment.name=prd");
        var resolved = Resolve();
        Assert.Equal("prd", resolved.Environment);
        Assert.Equal(LogLevel.Warning, resolved.MinimumLogLevel);
    }

    [Fact]
    public void Environment_Code_Option_Beats_Code_Attribute_Which_Beats_Env()
    {
        Env("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment.name=HML");
        Assert.Equal("DEV", Resolve(o => o.ResourceAttributes["deployment.environment.name"] = "DEV").Environment);
        Assert.Equal("HML", Resolve(o => o.ResourceAttributes["deployment.environment.name"] = "").Environment);
        var resolved = Resolve(o =>
        {
            o.Environment = " PRD ";
            o.ResourceAttributes["deployment.environment.name"] = "DEV";
        });
        Assert.Equal("PRD", resolved.Environment);
        Assert.Equal(LogLevel.Warning, resolved.MinimumLogLevel);
    }

    [Fact]
    public void Environment_Blank_In_Resource_Attributes_Is_Absent_And_Warned()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        Env("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment.name= ");
        var (provider, diagnostics) = Build(_ => { });
        using (provider)
        {
            Assert.False(TraceResource(provider).ContainsKey("deployment.environment.name"));
            Assert.Equal(LogLevel.Information, provider.GetRequiredService<ResolvedTelemetry>().MinimumLogLevel);
            Assert.Equal(1, Count(diagnostics, "deployment.environment.name"));
        }
    }

    [Fact]
    public void LogLevel_Code_Environment_Matches_Case_Insensitively()
    {
        Assert.Equal(LogLevel.Warning, Resolve(o => o.Environment = "prd").MinimumLogLevel);
        Assert.Equal(LogLevel.Debug, Resolve(o => o.Environment = "local").MinimumLogLevel);
        Assert.Equal(LogLevel.Information, Resolve(o => o.Environment = "staging").MinimumLogLevel);
    }

    // ── service.instance.id (D4) ───────────────────────────────────────────

    [Fact]
    public void InstanceId_Code_Beats_Env_Which_Beats_The_Process_Id()
    {
        Env("OTEL_RESOURCE_ATTRIBUTES", "service.instance.id=inst-env");
        Assert.Equal("inst-code", Resolve(o => o.ResourceAttributes["service.instance.id"] = "inst-code").ServiceInstanceId);
        Assert.Equal("inst-env", Resolve(o => o.ResourceAttributes["service.instance.id"] = " ").ServiceInstanceId);
        Env("OTEL_RESOURCE_ATTRIBUTES", "service.instance.id=\t");
        var generated = Resolve().ServiceInstanceId;
        Assert.Matches("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", generated);
        Assert.Equal(ResourceSetup.ProcessInstanceId, generated);
        Assert.Equal(generated, Resolve().ServiceInstanceId);
    }

    [Fact]
    public void InstanceId_Is_The_One_The_Sdk_AddService_Generates_In_This_Process()
    {
        var sdkId = ResourceBuilder.CreateEmpty().AddService("x").Build().Attributes
            .Single(kv => kv.Key == "service.instance.id").Value;
        Assert.Equal(sdkId, ResourceSetup.ProcessInstanceId);
    }

    // ── the rebuilt resource (D-NET7, A16, A24) ──────────────────────────────

    [Fact]
    public void Resource_Carries_The_Resolved_Identity_On_Every_Provider_With_The_Contract_Schema_Url()
    {
        Env("OTEL_RESOURCE_ATTRIBUTES", "team.name=payments,service.instance.id=inst-1");
        var (provider, _) = Build(o =>
        {
            o.ServiceName = "svc";
            o.Environment = "PRD";
        });
        using (provider)
        {
            var attributes = TraceResource(provider, out var resource);
            Assert.Equal(ResourceSetup.SchemaUrl, resource.SchemaUrl);
            Assert.Equal("https://opentelemetry.io/schemas/1.43.0", resource.SchemaUrl);
            Assert.Equal("svc", attributes["service.name"]);
            Assert.Equal("PRD", attributes["deployment.environment.name"]);
            Assert.Equal("inst-1", attributes["service.instance.id"]);
            Assert.Equal("payments", attributes["team.name"]);

            var meterResource = provider.GetRequiredService<MeterProvider>().GetResource();
            Assert.Equal(ResourceSetup.SchemaUrl, meterResource.SchemaUrl);
            Assert.Equal(attributes, meterResource.Attributes.ToDictionary(kv => kv.Key, kv => kv.Value));
        }
    }

    [Fact]
    public void Resource_Code_Attributes_Beat_Env_Per_Key()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        Env("OTEL_RESOURCE_ATTRIBUTES", "team.name=env,region=eu");
        var (provider, _) = Build(o => o.ResourceAttributes["team.name"] = "code");
        using (provider)
        {
            var attributes = TraceResource(provider);
            Assert.Equal("code", attributes["team.name"]);
            Assert.Equal("eu", attributes["region"]);
        }
    }

    [Fact]
    public void Resource_Drops_The_Sdk_Env_Detector_Parse()
    {
        // The SDK detector trims Unicode spaces, decodes '+' as a space and keeps items the
        // helper rejects; none of its output may survive the rebuild.
        Env("OTEL_SERVICE_NAME", "svc");
        Env("OTEL_RESOURCE_ATTRIBUTES", " team.name=payments,plus=a+b,=orphan");
        var (provider, _) = Build(_ => { });
        using (provider)
        {
            var attributes = TraceResource(provider);
            Assert.Equal("payments", attributes[" team.name"]);
            Assert.False(attributes.ContainsKey("team.name"));
            Assert.Equal("a+b", attributes["plus"]);
            Assert.False(attributes.ContainsKey(""));
        }
    }

    [Fact]
    public void Resource_Ignores_Otel_Service_Name_Set_Only_In_IConfiguration()
    {
        // A guard only: the SDK's OtelServiceNameEnvVarDetector reads IConfiguration first, and
        // the helper clears it. ADR-013: the helper reads OTEL_SERVICE_NAME from the process environment; the SDK's
        // detector (which reads IConfiguration) is cleared.
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?> { ["OTEL_SERVICE_NAME"] = "from-config" })
            .Build();
        var (provider, _) = Build(_ => { }, s => s.AddSingleton<IConfiguration>(configuration));
        using (provider)
        {
            Assert.Equal(TelemetryResolver.UnknownServiceName(), TraceResource(provider)["service.name"]);
        }
    }

    [Fact]
    public void Resource_Blank_Env_Service_Name_Is_The_Explicit_Sdk_Default()
    {
        // The SDK detector would emit service.name="" from the blank item; the helper's rebuild
        // sets unknown_service:<process> explicitly (the SDK default lives in the cleared list).
        Env("OTEL_RESOURCE_ATTRIBUTES", "service.name=,deployment.environment.name=DEV");
        var (provider, _) = Build(_ => { });
        using (provider)
        {
            Assert.Equal(TelemetryResolver.UnknownServiceName(), TraceResource(provider)["service.name"]);
        }
    }

    [Fact]
    public void Resource_Ignores_Otel_Resource_Attributes_Set_Only_In_IConfiguration()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?> { ["OTEL_RESOURCE_ATTRIBUTES"] = "team.name=from-config" })
            .Build();
        var (provider, _) = Build(_ => { }, s => s.AddSingleton<IConfiguration>(configuration));
        using (provider)
        {
            Assert.False(TraceResource(provider).ContainsKey("team.name"));
        }
    }

    [Fact]
    public void Resource_Telemetry_Sdk_Comes_From_The_Sdk_Env_Silently_Code_Warned_Once_Per_Key()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        Env("ENVIRONMENT", "DEV");
        Env("OTEL_RESOURCE_ATTRIBUTES", "telemetry.sdk.name=custom,telemetry.sdk.language=custom,telemetry.distro.name=kept");
        var (provider, diagnostics) = Build(o =>
        {
            o.ResourceAttributes["telemetry.sdk.version"] = "custom";
            o.ResourceAttributes["telemetry.sdk.language"] = "custom";
        });
        using (provider)
        {
            var attributes = TraceResource(provider);
            provider.GetRequiredService<MeterProvider>();
            Assert.Equal("opentelemetry", attributes["telemetry.sdk.name"]);
            Assert.Equal("dotnet", attributes["telemetry.sdk.language"]);
            Assert.Matches("^[0-9]+\\.[0-9]+\\.[0-9]+", (string)attributes["telemetry.sdk.version"]);
            Assert.Equal("kept", attributes["telemetry.distro.name"]);
            Assert.Equal(1, Count(diagnostics, "telemetry.sdk.version"));
            Assert.Equal(1, Count(diagnostics, "telemetry.sdk.language"));
            Assert.Equal(0, Count(diagnostics, "telemetry.sdk.name"));
            Assert.DoesNotContain("custom", diagnostics.Text);
        }
    }

    [Fact]
    public void Resource_Keeps_Earlier_App_Attributes_Between_Env_And_Code()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        Env("OTEL_RESOURCE_ATTRIBUTES", "team.name=env,region=env");
        var (provider, diagnostics) = Build(
            o => o.ResourceAttributes["region"] = "code",
            s => s.ConfigureOpenTelemetryTracerProvider(b => b.ConfigureResource(r => r.AddAttributes(new[]
            {
                new KeyValuePair<string, object>("team.name", "app"),
                new KeyValuePair<string, object>("region", "app"),
                new KeyValuePair<string, object>("app.only", 7),
            }))));
        using (provider)
        {
            var attributes = TraceResource(provider, out var resource);
            Assert.Equal("app", attributes["team.name"]);
            Assert.Equal("code", attributes["region"]);
            Assert.Equal(7L, attributes["app.only"]);
            Assert.Equal(ResourceSetup.SchemaUrl, resource.SchemaUrl);
            Assert.Equal(0, Count(diagnostics, "ConfigureResource"));
        }
    }

    [Fact]
    public void Resource_App_AddService_Name_Is_Replaced_With_One_Warning_Its_Instance_Id_Agrees()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        var (provider, diagnostics) = Build(_ => { },
            s => s.ConfigureOpenTelemetryTracerProvider(b => b.ConfigureResource(r => r.AddService("app-name", serviceVersion: "1.2.3"))));
        using (provider)
        {
            var attributes = TraceResource(provider);
            Assert.Equal("svc", attributes["service.name"]);
            Assert.Equal("1.2.3", attributes["service.version"]);
            Assert.Equal(ResourceSetup.ProcessInstanceId, attributes["service.instance.id"]);
            Assert.Equal(1, Count(diagnostics, "service.name set by an earlier ConfigureResource"));
            Assert.DoesNotContain("app-name", diagnostics.Text);
        }
    }

    [Fact]
    public void Resource_App_Environment_Stays_When_None_Is_Resolved()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        var (provider, _) = Build(_ => { },
            s => s.ConfigureOpenTelemetryTracerProvider(b => b.ConfigureResource(r => r.AddAttributes(new[]
            {
                new KeyValuePair<string, object>("deployment.environment.name", "app-env"),
            }))));
        using (provider)
        {
            Assert.Equal("app-env", TraceResource(provider)["deployment.environment.name"]);
        }
    }

    [Fact]
    public void Resource_An_Earlier_Factory_Detector_Is_Kept_Without_Clearing_And_Warned_Once()
    {
        Env("OTEL_RESOURCE_ATTRIBUTES", "team.name=env");
        var (provider, diagnostics) = Build(o =>
            {
                o.ServiceName = "svc";
                o.Environment = "DEV";
            },
            s => s.ConfigureOpenTelemetryTracerProvider(b => b.ConfigureResource(r =>
                r.AddDetector(_ => new FixedDetector("detector.attr", "from-detector")))));
        using (provider)
        {
            var attributes = TraceResource(provider);
            provider.GetRequiredService<MeterProvider>();
            Assert.Equal("from-detector", attributes["detector.attr"]);
            Assert.Equal("svc", attributes["service.name"]);
            Assert.Equal("DEV", attributes["deployment.environment.name"]);
            Assert.Equal("env", attributes["team.name"]);
            Assert.Equal("dotnet", attributes["telemetry.sdk.language"]);
            Assert.Equal(1, Count(diagnostics, "resource detector factory"));
        }
    }

    [Fact]
    public void Resource_App_Attributes_Equal_To_The_Sdk_Defaults_Are_Not_Kept()
    {
        var snapshot = ResourceBuilder.CreateDefault()
            .AddAttributes(new[]
            {
                new KeyValuePair<string, object>("arr", new[] { "a", "b" }),
                new KeyValuePair<string, object>("telemetry.sdk.name", "opentelemetry"),
            })
            .Build();
        var kept = ResourceSetup.AppAttributes(snapshot);
        Assert.Equal(new[] { "a", "b" }, kept["arr"]);
        Assert.False(kept.ContainsKey("telemetry.sdk.name"));
        Assert.False(kept.ContainsKey("service.name"));
    }

    // ── fix round 1: later and earlier application resource configuration ──

    private static Action<IServiceCollection> LaterAppResource()
        => s => s.AddOpenTelemetry().ConfigureResource(r => r.AddService("app-name").AddAttributes(new[]
        {
            new KeyValuePair<string, object>("telemetry.sdk.name", "app"),
        }));

    /// <summary>Runs the helper's hosted service as host start would (it builds the providers and checks their resources).</summary>
    private static void StartHelper(IServiceProvider provider)
    {
        foreach (var hosted in provider.GetServices<Microsoft.Extensions.Hosting.IHostedService>().Where(h => h.GetType().Name == "ResolveAtStartService"))
        {
            hosted.StartAsync(CancellationToken.None).GetAwaiter().GetResult();
        }
    }

    private static string LaterChangeLine(TestDiagnostics diagnostics)
        => Assert.Single(diagnostics.Lines, l => l.Contains("registered after AddOtelHelper() (ConfigureResource or SetResourceBuilder) changed", StringComparison.Ordinal));

    [Fact]
    public void Later_ConfigureResource_Is_Reported_At_Start_For_Every_Signal_With_The_Schema_Conflict_As_Helper_Text()
    {
        Env("OTEL_SERVICE_NAME", "svc-env");
        Env("ENVIRONMENT", "DEV");
        Env("OTEL_RESOURCE_ATTRIBUTES", "service.instance.id=inst-env");
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddOtelHelper(_ => { });
        LaterAppResource()(services);
        using var provider = services.BuildServiceProvider();
        StartHelper(provider);

        var resources = new[]
        {
            provider.GetRequiredService<TracerProvider>().GetResource(),
            provider.GetRequiredService<MeterProvider>().GetResource(),
            provider.GetRequiredService<OpenTelemetry.Logs.LoggerProvider>().GetResource(),
        };
        foreach (var resource in resources)
        {
            // What the SDK 1.20.1 builds: the later resources win, and the two schema URLs
            // (helper 1.43.0, AddService 1.44.0) leave none. No SDK hook runs after them.
            var attributes = resource.Attributes.ToDictionary(kv => kv.Key, kv => kv.Value);
            Assert.Equal("app-name", attributes["service.name"]);
            Assert.Equal("app", attributes["telemetry.sdk.name"]);
            Assert.NotEqual("inst-env", attributes["service.instance.id"]);
            Assert.Null(resource.SchemaUrl);
        }

        var line = LaterChangeLine(diagnostics);
        Assert.Contains("signals=traces,metrics,logs", line);
        Assert.Contains("changed=service.name,service.instance.id,telemetry.sdk.name", line);
        Assert.DoesNotContain("schema_URL", line);
        var schema = Assert.Single(diagnostics.Lines, l => l.Contains("different schema URLs were merged", StringComparison.Ordinal));
        Assert.Contains("old=https://opentelemetry.io/schemas/1.43.0", schema);
        Assert.Contains("updating=https://opentelemetry.io/schemas/1.44.0", schema);
        Assert.Equal(0, Count(diagnostics, "conflicting Schema URLs"));
    }

    [Fact]
    public void Later_SetResourceBuilder_On_Tracing_Only_Is_Reported_Naming_Traces()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        Env("ENVIRONMENT", "DEV");
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddOtelHelper(_ => { });
        services.ConfigureOpenTelemetryTracerProvider(b => b.SetResourceBuilder(ResourceBuilder.CreateEmpty().AddService("other")));
        using var provider = services.BuildServiceProvider();
        StartHelper(provider);

        Assert.Equal("other", TraceResource(provider)["service.name"]);
        Assert.Equal("svc", provider.GetRequiredService<MeterProvider>().GetResource().Attributes.Single(kv => kv.Key == "service.name").Value);
        var line = LaterChangeLine(diagnostics);
        Assert.Contains("signals=traces ", line + " ");
        Assert.Contains("service.name", line);
        Assert.Contains("deployment.environment.name", line);
        Assert.Contains("telemetry.sdk.name", line);
    }

    [Fact]
    public void Earlier_ConfigureResource_Is_Not_Reported_As_A_Later_Change()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        var (provider, diagnostics) = Build(o => o.Environment = "DEV",
            s => s.AddOpenTelemetry().ConfigureResource(r => r.AddService("app-name")));
        using (provider)
        {
            StartHelper(provider);
            var attributes = TraceResource(provider, out var resource);
            Assert.Equal("svc", attributes["service.name"]);
            Assert.Equal(ResourceSetup.SchemaUrl, resource.SchemaUrl);
            Assert.Equal(0, Count(diagnostics, "registered after AddOtelHelper()"));
            Assert.Equal(0, Count(diagnostics, "schema URL"));
        }
    }

    [Fact]
    public void Each_Earlier_Detector_Runs_Exactly_Once_Per_Provider()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        Env("ENVIRONMENT", "DEV");
        var counting = new CountingDetector();
        var (provider, _) = Build(_ => { }, s => s.AddOpenTelemetry().ConfigureResource(r => r.AddDetector(counting)));
        using (provider)
        {
            StartHelper(provider);
            Assert.Equal("counted", TraceResource(provider)["counting.attr"]);
            Assert.Equal(3, counting.Calls);
        }
    }

    [Fact]
    public void Each_Later_Detector_Runs_Exactly_Once_Per_Provider()
    {
        // Round 1 rebuilt the resource inside Build(): a detector registered after the helper ran
        // twice per provider (6). The start-time check reads the built resources only.
        Env("OTEL_SERVICE_NAME", "svc");
        Env("ENVIRONMENT", "DEV");
        var counting = new CountingDetector();
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddOtelHelper(_ => { });
        services.AddOpenTelemetry().ConfigureResource(r => r.AddDetector(counting));
        using var provider = services.BuildServiceProvider();
        StartHelper(provider);
        Assert.Equal("counted", TraceResource(provider)["counting.attr"]);
        Assert.Equal(3, counting.Calls);
        Assert.Equal(0, Count(diagnostics, "registered after AddOtelHelper()"));
    }

    [Fact]
    public void Sdk_Disabled_Skips_The_Later_Change_Check()
    {
        Env("OTEL_SERVICE_NAME", "svc-env");
        Env("ENVIRONMENT", "DEV");
        Env("OTEL_SDK_DISABLED", "true");
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddOtelHelper(_ => { });
        LaterAppResource()(services);
        using var provider = services.BuildServiceProvider();
        StartHelper(provider);
        Assert.Equal(0, Count(diagnostics, "registered after AddOtelHelper()"));
    }

    private static (Microsoft.Extensions.Hosting.IHost Host, TestDiagnostics Diagnostics) StartHost(Action<IServiceCollection>? before = null)
    {
        var diagnostics = new TestDiagnostics();
        var builder = new Microsoft.Extensions.Hosting.HostApplicationBuilder(new Microsoft.Extensions.Hosting.HostApplicationBuilderSettings { DisableDefaults = true });
        builder.Services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        before?.Invoke(builder.Services);
        builder.Services.AddOtelHelper(_ => { });
        var host = builder.Build();
        host.StartAsync().GetAwaiter().GetResult();
        return (host, diagnostics);
    }

    [Fact]
    public async Task Host_App_Environment_On_Tracing_Only_Names_The_Lacking_Signals_Without_Not_Set()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        var (host, diagnostics) = StartHost(s => s.ConfigureOpenTelemetryTracerProvider(b => b.ConfigureResource(r => r.AddAttributes(new[]
        {
            new KeyValuePair<string, object>("deployment.environment.name", "PRD"),
        }))));
        using (host)
        {
            Assert.Equal(LogLevel.Warning, host.Services.GetRequiredService<IOptionsMonitor<LoggerFilterOptions>>().CurrentValue.MinLevel);
            Assert.Equal(0, Count(diagnostics, "is not set"));
            var line = Assert.Single(diagnostics.Lines, l => l.Contains("differs between signals", StringComparison.Ordinal));
            Assert.Contains("missing=metrics,logs", line);
            await host.StopAsync();
        }
    }

    [Fact]
    public async Task Host_Without_Any_Environment_Warns_Not_Set_Exactly_Once()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        var (host, diagnostics) = StartHost();
        using (host)
        {
            Assert.Equal(1, Count(diagnostics, "deployment.environment.name is not set"));
            Assert.Equal(0, Count(diagnostics, "differs between signals"));
            await host.StopAsync();
        }
    }

    [Fact]
    public void App_Environment_Is_Used_When_None_Is_Resolved_And_Picks_The_Log_Level()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        // A blank code attribute for the key is "not provided": it must not hide the app value.
        var (provider, diagnostics) = Build(o => o.ResourceAttributes["deployment.environment.name"] = " ",
            s => s.ConfigureOpenTelemetryTracerProvider(b => b.ConfigureResource(r => r.AddAttributes(new[]
            {
                new KeyValuePair<string, object>("deployment.environment.name", "prd"),
            }))));
        using (provider)
        {
            var filters = provider.GetRequiredService<IOptionsMonitor<LoggerFilterOptions>>();
            Assert.Equal(LogLevel.Information, filters.CurrentValue.MinLevel);
            Assert.Equal("prd", TraceResource(provider)["deployment.environment.name"]);
            Assert.Equal(LogLevel.Warning, filters.CurrentValue.MinLevel);

            // The meter's resource has no app environment (it reports it absent), but the "not set"
            // warning is not written once another signal's resource carried one.
            Assert.DoesNotContain(provider.GetRequiredService<MeterProvider>().GetResource().Attributes, kv => kv.Key == "deployment.environment.name");
            Assert.Equal(0, Count(diagnostics, "is not set"));
        }
    }

    [Fact]
    public void App_Identity_Values_Replaced_By_Resolved_Ones_Are_Warned_Once_Per_Key()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        Env("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment.name=HML,service.instance.id=inst-env");
        var (provider, diagnostics) = Build(_ => { },
            s => s.ConfigureOpenTelemetryTracerProvider(b => b.ConfigureResource(r => r
                .AddService("svc", serviceInstanceId: "inst-app")
                .AddAttributes(new[] { new KeyValuePair<string, object>("deployment.environment.name", "app-env") }))));
        using (provider)
        {
            var attributes = TraceResource(provider);
            Assert.Equal("HML", attributes["deployment.environment.name"]);
            Assert.Equal("inst-env", attributes["service.instance.id"]);
            Assert.Equal(1, Count(diagnostics, "deployment.environment.name set by an earlier ConfigureResource"));
            Assert.Equal(1, Count(diagnostics, "service.instance.id set by an earlier ConfigureResource"));
            Assert.Equal(0, Count(diagnostics, "service.name set by an earlier ConfigureResource"));
            Assert.DoesNotContain("inst-app", diagnostics.Text);
            Assert.DoesNotContain("app-env", diagnostics.Text);
        }
    }

    [Fact]
    public void App_Explicit_Instance_Id_Is_Kept_When_The_Helper_Has_Only_The_Process_Id()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        Env("ENVIRONMENT", "DEV");
        var (provider, diagnostics) = Build(_ => { },
            s => s.ConfigureOpenTelemetryTracerProvider(b => b.ConfigureResource(r => r.AddService("svc", serviceInstanceId: "inst-app"))));
        using (provider)
        {
            Assert.Equal("inst-app", TraceResource(provider)["service.instance.id"]);
            Assert.Equal(0, Count(diagnostics, "service.instance.id"));
        }
    }

    [Fact]
    public void Factory_Detector_Path_Names_An_Unresolved_Environment_Whose_Blank_Sdk_Value_Stays()
    {
        Env("OTEL_SERVICE_NAME", "svc");
        Env("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment.name=");
        var (provider, diagnostics) = Build(_ => { },
            s => s.ConfigureOpenTelemetryTracerProvider(b => b.ConfigureResource(r =>
                r.AddDetector(_ => new FixedDetector("detector.attr", "x")))));
        using (provider)
        {
            StartHelper(provider);
            var attributes = TraceResource(provider);
            Assert.Equal("", attributes["deployment.environment.name"]);
            var line = Assert.Single(diagnostics.Lines, l => l.Contains("resource detector factory", StringComparison.Ordinal));
            Assert.Contains("deployment.environment.name is not set by the helper", line);
            Assert.Equal(0, Count(diagnostics, "registered after AddOtelHelper()"));
        }
    }

    // ── code validation (A16: invalid code resource attributes fail at start) ─

    [Theory]
    [InlineData("")]
    [InlineData(" \t")]
    public void Validator_Empty_Code_Key_Fails_At_Start(string key)
    {
        var ex = Assert.Throws<OptionsValidationException>(() => Validate(o => o.ResourceAttributes[key] = "orphan"));
        Assert.Contains("TelemetryOptions.ResourceAttributes has an empty key", ex.Message);
    }

    [Fact]
    public void Validator_Unicode_Space_Key_Is_Valid()
    {
        Validate(o => o.ResourceAttributes[" "] = "v");
    }

    [Fact]
    public void Validator_Unsupported_Values_Fail_Naming_The_Key()
    {
        var ex = Assert.Throws<OptionsValidationException>(() => Validate(o =>
        {
            o.ResourceAttributes["nothing"] = null!;
            o.ResourceAttributes["when"] = DateTime.UnixEpoch;
            o.ResourceAttributes["service.name"] = 42;
            o.ResourceAttributes["fine"] = new[] { 1L, 2L };
        }));
        Assert.Contains("ResourceAttributes[nothing] must be a string, bool, number or an array of them, got null", ex.Message);
        Assert.Contains("ResourceAttributes[when]", ex.Message);
        Assert.Contains("ResourceAttributes[service.name] must be a string, got Int32", ex.Message);
        Assert.DoesNotContain("[fine]", ex.Message);
    }

    [Fact]
    public void Validator_Null_Resource_Attributes_Fail()
    {
        var ex = Assert.Throws<OptionsValidationException>(() => Validate(o => o.ResourceAttributes = null!));
        Assert.Contains("TelemetryOptions.ResourceAttributes must not be null", ex.Message);
    }

    private static void Validate(Action<TelemetryOptions> configure)
    {
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(configure);
        using var provider = services.BuildServiceProvider();
        _ = provider.GetRequiredService<IOptions<TelemetryOptions>>().Value;
    }

    private sealed class CountingDetector : IResourceDetector
    {
        private int _calls;

        public int Calls => Volatile.Read(ref _calls);

        public Resource Detect()
        {
            Interlocked.Increment(ref _calls);
            return new(new[] { new KeyValuePair<string, object>("counting.attr", "counted") });
        }
    }

    private sealed class FixedDetector : IResourceDetector
    {
        private readonly string _key;
        private readonly string _value;

        public FixedDetector(string key, string value)
        {
            _key = key;
            _value = value;
        }

        public Resource Detect() => new(new[] { new KeyValuePair<string, object>(_key, _value) });
    }
}
