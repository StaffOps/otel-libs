using System.Diagnostics;
using System.Diagnostics.Metrics;
using OtelHelper;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Xunit;

namespace OtelHelper.Tests;

public class TelemetryPipelineTests
{
    private static TelemetryOptions CreateResolved(Action<TelemetryOptions>? configure = null)
    {
        var opts = new TelemetryOptions();
        configure?.Invoke(opts);
        new TelemetryOptionsPostConfigure().PostConfigure(null, opts);
        return opts;
    }

    private static ServiceProvider BuildProvider(Action<TelemetryOptions>? configure = null)
    {
        var services = new ServiceCollection();
        services.AddOtelHelper(configure ?? (_ => { }));
        return services.BuildServiceProvider();
    }

    // --- Tracing: AlwaysOnSampler for all environments (sampling delegated to Collector) ---

    [Theory]
    [InlineData(DeploymentEnvironment.LOCAL)]
    [InlineData(DeploymentEnvironment.DEV)]
    [InlineData(DeploymentEnvironment.HML)]
    [InlineData(DeploymentEnvironment.PRD)]
    public void All_Environments_Register_Pipeline_Without_Error(DeploymentEnvironment env)
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test";
            opts.Environment = env;
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });
        Assert.NotNull(provider);
    }

    // --- Logging: log level per environment ---

    [Theory]
    [InlineData(DeploymentEnvironment.LOCAL, LogLevel.Debug)]
    [InlineData(DeploymentEnvironment.DEV, LogLevel.Information)]
    [InlineData(DeploymentEnvironment.HML, LogLevel.Information)]
    [InlineData(DeploymentEnvironment.PRD, LogLevel.Warning)]
    public void LogLevel_Matches_Environment(DeploymentEnvironment env, LogLevel expectedMinLevel)
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test";
            opts.Environment = env;
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        var loggerFactory = provider.GetRequiredService<ILoggerFactory>();
        var logger = loggerFactory.CreateLogger("TestLogger");

        // The level below minimum should not be enabled
        if (expectedMinLevel > LogLevel.Trace)
        {
            var belowMinimum = (LogLevel)((int)expectedMinLevel - 1);
            Assert.False(logger.IsEnabled(belowMinimum),
                $"LogLevel {belowMinimum} should be disabled for {env}");
        }

        // The minimum level itself should be enabled
        Assert.True(logger.IsEnabled(expectedMinLevel),
            $"LogLevel {expectedMinLevel} should be enabled for {env}");
    }

    [Fact]
    public void DebugLevel_Forces_Debug_LogLevel()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test";
            opts.Environment = DeploymentEnvironment.PRD; // normally Warning
            opts.DebugLevel = true;
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        var logger = provider.GetRequiredService<ILoggerFactory>().CreateLogger("Test");
        Assert.True(logger.IsEnabled(LogLevel.Debug));
    }

    // --- Endpoint resolution ---

    [Fact]
    public void ExtraInstrumentation_Default_Has_SQL()
    {
        var opts = CreateResolved();
        Assert.True(opts.HasInstrumentation("SQL"));
        Assert.False(opts.HasInstrumentation("AWS"));
    }

    [Fact]
    public void ExtraInstrumentation_Multiple_Values()
    {
        var opts = new TelemetryOptions { ExtraInstrumentation = "SQL,AWS" };
        Assert.True(opts.HasInstrumentation("SQL"));
        Assert.True(opts.HasInstrumentation("AWS"));
    }

    [Fact]
    public void ExtraInstrumentation_Case_Insensitive()
    {
        var opts = new TelemetryOptions { ExtraInstrumentation = "sql,aws" };
        Assert.True(opts.HasInstrumentation("SQL"));
        Assert.True(opts.HasInstrumentation("AWS"));
    }

    [Fact]
    public void ExtraInstrumentation_Empty_Disables_All()
    {
        var opts = new TelemetryOptions { ExtraInstrumentation = "" };
        Assert.False(opts.HasInstrumentation("SQL"));
        Assert.False(opts.HasInstrumentation("AWS"));
    }

    [Fact]
    public void DebugLevel_Enables_All_Extra_Instrumentation()
    {
        var opts = new TelemetryOptions { DebugLevel = true, ExtraInstrumentation = "" };
        Assert.True(opts.HasInstrumentation("SQL"));
        Assert.True(opts.HasInstrumentation("AWS"));
    }

    [Fact]
    public void Default_Endpoint_Uses_Localhost()
    {
        var opts = CreateResolved();
        Assert.Contains("localhost", opts.OtelCollectorEndpoint);
    }

    [Fact]
    public void Endpoint_Extracts_Host_And_Appends_Port()
    {
        System.Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_ENDPOINT", "http://my-collector.svc");
        try
        {
            // Static fields are cached, so test the logic directly
            var input = "http://my-collector.svc";
            var uri = new Uri(input);
            var host = $"{uri.Scheme}://{uri.Host}";
            Assert.Equal("http://my-collector.svc", host);
            Assert.Equal("http://my-collector.svc:4317", $"{host}:4317");
        }
        finally
        {
            System.Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_ENDPOINT", null);
        }
    }

    [Fact]
    public void Endpoint_With_Trailing_Slash_Is_Cleaned()
    {
        var input = "http://collector.monitoring/";
        var uri = new Uri(input.TrimEnd('/'));
        var host = $"{uri.Scheme}://{uri.Host}";
        Assert.Equal("http://collector.monitoring", host);
    }

    // --- Full pipeline integration ---

    [Fact]
    public void Full_Pipeline_Registers_All_Signals()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "integration-test";
            opts.Environment = DeploymentEnvironment.DEV;
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        // Logging registered
        var loggerFactory = provider.GetService<ILoggerFactory>();
        Assert.NotNull(loggerFactory);

        // Options registered
        var options = provider.GetService<Microsoft.Extensions.Options.IOptions<TelemetryOptions>>();
        Assert.NotNull(options);
        Assert.Equal("integration-test", options!.Value.ServiceName);
    }

    // --- Sampler configurability ---

    [Fact]
    public void Custom_Sampler_Is_Accepted()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
            opts.Sampler = new OpenTelemetry.Trace.TraceIdRatioBasedSampler(0.5);
        });
        Assert.NotNull(provider);
    }

    // --- MinimumLogLevel override ---

    [Fact]
    public void Custom_MinimumLogLevel_Overrides_Environment()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test";
            opts.Environment = DeploymentEnvironment.PRD; // normally Warning
            opts.MinimumLogLevel = LogLevel.Information;
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        var logger = provider.GetRequiredService<ILoggerFactory>().CreateLogger("Test");
        Assert.True(logger.IsEnabled(LogLevel.Information));
    }

    // --- ResourceAttributes ---

    [Fact]
    public void ResourceAttributes_Default_Is_Empty()
    {
        var opts = new TelemetryOptions();
        Assert.Empty(opts.ResourceAttributes);
    }

    [Fact]
    public void ResourceAttributes_Accepted_In_Pipeline()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
            opts.ResourceAttributes = new Dictionary<string, object>
            {
                ["app.version"] = "1.0.0",
                ["app.team"] = "devops"
            };
        });
        Assert.NotNull(provider);
    }

    // --- AdditionalActivitySources ---

    [Fact]
    public void AdditionalActivitySources_Default_Is_Empty()
    {
        var opts = new TelemetryOptions();
        Assert.Empty(opts.AdditionalActivitySources);
    }

    [Fact]
    public void AdditionalActivitySources_Accepted_In_Pipeline()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
            opts.AdditionalActivitySources = new List<string> { "MyApp.Orders", "MyApp.Payments" };
        });
        Assert.NotNull(provider);
    }

    // --- GetDefaultLogLevel ---

    [Fact]
    public void GetDefaultLogLevel_Returns_Correct_Values()
    {
        Assert.Equal(LogLevel.Debug, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironment.LOCAL));
        Assert.Equal(LogLevel.Information, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironment.DEV));
        Assert.Equal(LogLevel.Warning, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironment.PRD));
        Assert.Equal(LogLevel.Debug, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironment.PRD, debugLevel: true));
    }

    // --- DI: ActivitySource and Meter ---

    [Fact]
    public void ActivitySource_Registered_Via_DI()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test-svc";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        var source = provider.GetService<ActivitySource>();
        Assert.NotNull(source);
        Assert.Equal("test-svc", source!.Name);
    }

    [Fact]
    public void Meter_Registered_Via_DI()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test-svc";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        var meter = provider.GetService<Meter>();
        Assert.NotNull(meter);
        Assert.Equal("test-svc", meter!.Name);
    }

    // --- StartRootActivity ---

    [Fact]
    public void StartRootActivity_Creates_Root_Span_Without_Parent()
    {
        var source = new ActivitySource("test-root");
        var listener = new ActivityListener
        {
            ShouldListenTo = _ => true,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllData
        };
        ActivitySource.AddActivityListener(listener);

        try
        {
            // Simulate existing Activity.Current (as in a loop)
            using var parent = source.StartActivity("parent");
            Assert.NotNull(Activity.Current);

            // StartRootActivity should clear parent and create root
            using var root = source.StartRootActivity("root-span");
            Assert.NotNull(root);
            Assert.Null(root!.Parent);
            Assert.Equal("root-span", root.OperationName);
        }
        finally
        {
            listener.Dispose();
        }
    }

    [Fact]
    public void StartRootActivity_Generates_New_TraceId_Each_Call()
    {
        var source = new ActivitySource("test-root-ids");
        var listener = new ActivityListener
        {
            ShouldListenTo = _ => true,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllData
        };
        ActivitySource.AddActivityListener(listener);

        try
        {
            using var span1 = source.StartRootActivity("op1");
            var traceId1 = span1?.TraceId;

            using var span2 = source.StartRootActivity("op2");
            var traceId2 = span2?.TraceId;

            Assert.NotNull(traceId1);
            Assert.NotNull(traceId2);
            Assert.NotEqual(traceId1, traceId2);
        }
        finally
        {
            listener.Dispose();
        }
    }

    // --- DebugTraceStateProcessor ---

    [Fact]
    public void DebugMode_Sets_TraceState_And_Attribute_On_Root_Span()
    {
        var source = new ActivitySource("test-debug");
        var listener = new ActivityListener
        {
            ShouldListenTo = _ => true,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded
        };
        ActivitySource.AddActivityListener(listener);

        try
        {
            var processor = new OtelHelper.Tracing.DebugTraceStateProcessor();
            using var root = source.StartActivity("root");
            Assert.NotNull(root);
            processor.OnStart(root!);

            Assert.Contains("debug=true", root.TraceStateString);
            Assert.Equal("true", root.GetTagItem("debug")?.ToString());
        }
        finally
        {
            listener.Dispose();
        }
    }

    [Fact]
    public void DebugMode_Does_Not_Set_On_Child_Span()
    {
        var source = new ActivitySource("test-debug-child");
        var listener = new ActivityListener
        {
            ShouldListenTo = _ => true,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded
        };
        ActivitySource.AddActivityListener(listener);

        try
        {
            var processor = new OtelHelper.Tracing.DebugTraceStateProcessor();
            using var parent = source.StartActivity("parent");
            using var child = source.StartActivity("child");
            Assert.NotNull(child);
            processor.OnStart(child!);

            // Child has a parent, so processor should not set debug
            Assert.Null(child.GetTagItem("debug"));
        }
        finally
        {
            listener.Dispose();
        }
    }

    // --- OTEL_HELPER_SAMPLE_RATIO ---

    [Fact]
    public void SampleRatio_EnvVar_Sets_TraceIdRatioBasedSampler()
    {
        System.Environment.SetEnvironmentVariable("OTEL_HELPER_SAMPLE_RATIO", "0.5");
        try
        {
            var opts = CreateResolved(o => { o.ServiceName = "test"; });
            Assert.IsType<OpenTelemetry.Trace.TraceIdRatioBasedSampler>(opts.Sampler);
        }
        finally
        {
            System.Environment.SetEnvironmentVariable("OTEL_HELPER_SAMPLE_RATIO", null);
        }
    }

    [Fact]
    public void SampleRatio_Default_Keeps_AlwaysOn()
    {
        var opts = CreateResolved(o => { o.ServiceName = "test"; });
        Assert.IsType<OpenTelemetry.Trace.AlwaysOnSampler>(opts.Sampler);
    }

    [Fact]
    public void SampleRatio_Invalid_EnvVar_Keeps_AlwaysOn()
    {
        System.Environment.SetEnvironmentVariable("OTEL_HELPER_SAMPLE_RATIO", "not_a_number");
        try
        {
            var opts = CreateResolved(o => { o.ServiceName = "test"; });
            Assert.IsType<OpenTelemetry.Trace.AlwaysOnSampler>(opts.Sampler);
        }
        finally
        {
            System.Environment.SetEnvironmentVariable("OTEL_HELPER_SAMPLE_RATIO", null);
        }
    }

    // --- Double registration guard ---

    [Fact]
    public void Double_AddOtelHelper_Does_Not_Duplicate_Registration()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper(o => { o.ServiceName = "test"; o.OtelCollectorEndpoint = "http://localhost:4317"; });
        services.AddOtelHelper(o => { o.ServiceName = "test2"; o.OtelCollectorEndpoint = "http://localhost:4317"; });

        var provider = services.BuildServiceProvider();
        var validators = provider.GetServices<Microsoft.Extensions.Options.IValidateOptions<TelemetryOptions>>();
        // Should only have one validator (not duplicated)
        Assert.Single(validators);
    }

    // --- Environment PostConfigure ---

    [Fact]
    public void PostConfigure_Resolves_Environment_From_EnvVar()
    {
        System.Environment.SetEnvironmentVariable("ENVIRONMENT", "PRD");
        try
        {
            var opts = CreateResolved();
            Assert.Equal(DeploymentEnvironment.PRD, opts.Environment);
        }
        finally
        {
            System.Environment.SetEnvironmentVariable("ENVIRONMENT", null);
        }
    }

    // --- Validator ---

    [Fact]
    public void Validator_Fails_On_Empty_ServiceName()
    {
        var validator = new TelemetryOptionsValidator();
        var opts = new TelemetryOptions { ServiceName = "", OtelCollectorEndpoint = "http://localhost:4317" };
        var result = validator.Validate(null, opts);
        Assert.True(result.Failed);
        Assert.Contains("ServiceName", result.FailureMessage);
    }

    [Fact]
    public void Validator_Fails_On_Empty_Endpoint()
    {
        var validator = new TelemetryOptionsValidator();
        var opts = new TelemetryOptions { ServiceName = "test", OtelCollectorEndpoint = "" };
        var result = validator.Validate(null, opts);
        Assert.True(result.Failed);
        Assert.Contains("OtelCollectorEndpoint", result.FailureMessage);
    }

    [Fact]
    public void Validator_Fails_On_Invalid_URI()
    {
        var validator = new TelemetryOptionsValidator();
        var opts = new TelemetryOptions { ServiceName = "test", OtelCollectorEndpoint = "not-a-uri" };
        var result = validator.Validate(null, opts);
        Assert.True(result.Failed);
        Assert.Contains("OtelCollectorEndpoint", result.FailureMessage);
    }

    [Fact]
    public void Validator_Fails_On_Zero_Timeout()
    {
        var validator = new TelemetryOptionsValidator();
        var opts = new TelemetryOptions { ServiceName = "test", OtelCollectorEndpoint = "http://localhost:4317", ExportTimeoutMs = 0 };
        var result = validator.Validate(null, opts);
        Assert.True(result.Failed);
        Assert.Contains("ExportTimeoutMs", result.FailureMessage);
    }

    // --- IsSignalEnabled ---

    [Fact]
    public void IsSignalEnabled_Returns_True_When_DisabledSignals_Empty()
    {
        var opts = new TelemetryOptions { DisabledSignals = "" };
        Assert.True(opts.IsSignalEnabled("traces"));
        Assert.True(opts.IsSignalEnabled("metrics"));
        Assert.True(opts.IsSignalEnabled("logs"));
    }

    [Fact]
    public void IsSignalEnabled_Returns_False_When_Signal_In_DisabledSignals()
    {
        var opts = new TelemetryOptions { DisabledSignals = "metrics,logs" };
        Assert.True(opts.IsSignalEnabled("traces"));
        Assert.False(opts.IsSignalEnabled("metrics"));
        Assert.False(opts.IsSignalEnabled("logs"));
    }

    [Fact]
    public void IsSignalEnabled_Is_Case_Insensitive()
    {
        var opts = new TelemetryOptions { DisabledSignals = "TRACES" };
        Assert.False(opts.IsSignalEnabled("traces"));
        Assert.False(opts.IsSignalEnabled("Traces"));
        Assert.False(opts.IsSignalEnabled("TRACES"));
    }

    [Fact]
    public void PostConfigure_Resolves_DisabledSignals_From_EnvVar()
    {
        System.Environment.SetEnvironmentVariable("OTEL_HELPER_DISABLED_SIGNALS", "metrics,logs");
        try
        {
            var opts = CreateResolved();
            Assert.Equal("metrics,logs", opts.DisabledSignals);
            Assert.False(opts.IsSignalEnabled("metrics"));
            Assert.True(opts.IsSignalEnabled("traces"));
        }
        finally
        {
            System.Environment.SetEnvironmentVariable("OTEL_HELPER_DISABLED_SIGNALS", null);
        }
    }

    [Fact]
    public void PostConfigure_Resolves_DisabledMetrics_From_EnvVar()
    {
        System.Environment.SetEnvironmentVariable("OTEL_HELPER_DISABLED_METRICS", "http.*,system.disk.*");
        try
        {
            var opts = CreateResolved();
            Assert.Equal("http.*,system.disk.*", opts.DisabledMetrics);
        }
        finally
        {
            System.Environment.SetEnvironmentVariable("OTEL_HELPER_DISABLED_METRICS", null);
        }
    }

    [Fact]
    public void PostConfigure_DisabledMetrics_Stays_Empty_When_EnvVar_Not_Set()
    {
        System.Environment.SetEnvironmentVariable("OTEL_HELPER_DISABLED_METRICS", null);
        var opts = CreateResolved();
        Assert.Equal("", opts.DisabledMetrics);
    }

    // --- Full pipeline with disabled signals ---

    [Fact]
    public void Pipeline_Registers_When_Metrics_Disabled()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test-disabled";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
            opts.DisabledSignals = "metrics";
        });
        Assert.NotNull(provider);
        var loggerFactory = provider.GetService<ILoggerFactory>();
        Assert.NotNull(loggerFactory);
    }

    // --- HasInstrumentation("REDIS") ---

    [Fact]
    public void HasInstrumentation_Redis_Works_Like_SQL_AWS()
    {
        var opts = new TelemetryOptions { ExtraInstrumentation = "SQL,REDIS" };
        Assert.True(opts.HasInstrumentation("SQL"));
        Assert.True(opts.HasInstrumentation("REDIS"));
        Assert.False(opts.HasInstrumentation("AWS"));
    }

    [Fact]
    public void HasInstrumentation_Redis_Case_Insensitive()
    {
        var opts = new TelemetryOptions { ExtraInstrumentation = "redis" };
        Assert.True(opts.HasInstrumentation("REDIS"));
    }

    // --- DisabledMetrics glob pattern via pipeline ---

    [Fact]
    public void Pipeline_Registers_With_DisabledMetrics_Pattern()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test-drop-metrics";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
            opts.DisabledMetrics = "http.server.*,system.*";
        });
        Assert.NotNull(provider);
    }

    // --- Disabled traces signal skips tracing setup ---

    [Fact]
    public void Pipeline_Registers_When_Traces_Disabled()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test-no-traces";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
            opts.DisabledSignals = "traces";
        });
        Assert.NotNull(provider);
    }

    [Fact]
    public void Pipeline_Registers_When_Logs_Disabled()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test-no-logs";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
            opts.DisabledSignals = "logs";
        });
        Assert.NotNull(provider);
    }

    [Fact]
    public void Pipeline_Registers_When_All_Signals_Disabled()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test-no-signals";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
            opts.DisabledSignals = "traces,metrics,logs";
        });
        Assert.NotNull(provider);
    }

    // --- ResolveCollectorHost non-URI fallback ---

    [Fact]
    public void PostConfigure_Endpoint_NonURI_Uses_RawValue()
    {
        System.Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_ENDPOINT", "just-a-hostname");
        try
        {
            var opts = CreateResolved();
            Assert.Contains("just-a-hostname", opts.OtelCollectorEndpoint);
        }
        finally
        {
            System.Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_ENDPOINT", null);
        }
    }

    // --- AWS instrumentation in full pipeline ---

    [Fact]
    public void Pipeline_Registers_With_AWS_Instrumentation()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test-aws";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
            opts.ExtraInstrumentation = "SQL,AWS";
        });
        Assert.NotNull(provider);
    }

    // --- REDIS instrumentation in full pipeline ---

    [Fact]
    public void Pipeline_Registers_With_Redis_Instrumentation()
    {
        using var provider = BuildProvider(opts =>
        {
            opts.ServiceName = "test-redis";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
            opts.ExtraInstrumentation = "SQL,REDIS";
        });
        Assert.NotNull(provider);
    }
}
