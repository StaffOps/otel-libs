using System.Diagnostics;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using OpenTelemetry;
using OpenTelemetry.Logs;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Logging;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C17 fix round 1 (design D-NET10 (g)): the activity-tracking scope the logger factory adds
/// (<c>SpanId</c>, <c>TraceId</c>, <c>ParentId</c>) does not reach the SDK's log records; every
/// other scope does, an application scope that uses the same keys included. The capture processor
/// reads the scopes the way the OTLP exporter does (<c>LogRecord.ForEachScope</c>).
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public sealed class ActivityScopeFilterTests
{
    private static readonly ActivitySource Source = new("test-service");

    /// <summary>Scope entries per record, as the OTLP exporter enumerates them.</summary>
    private sealed class ScopeCapture : BaseProcessor<LogRecord>
    {
        public List<(string? Body, List<KeyValuePair<string, object?>> Scopes)> Records { get; } = new();

        public override void OnEnd(LogRecord data)
        {
            var scopes = new List<KeyValuePair<string, object?>>();
            data.ForEachScope((scope, list) =>
            {
                foreach (var item in scope)
                {
                    list.Add(item);
                }
            }, scopes);
            lock (Records)
            {
                Records.Add((data.FormattedMessage ?? data.Body, scopes));
            }
        }
    }

    private static IHost BuildHost(ScopeCapture capture, Action<HostApplicationBuilder>? configure = null)
    {
        var builder = Host.CreateApplicationBuilder();
        builder.Logging.ClearProviders();
        builder.Services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        builder.Services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.OtelCollectorEndpoint = "http://localhost:4317"; o.PrometheusMetricsPort = 0; });
        builder.Services.ConfigureOpenTelemetryLoggerProvider(b => b.AddProcessor(capture));
        configure?.Invoke(builder);
        var host = builder.Build();
        // The tracer provider listens to the service's source once built (the host builds it at start).
        host.Services.GetRequiredService<OpenTelemetry.Trace.TracerProvider>();
        return host;
    }

    private static List<KeyValuePair<string, object?>> LogInsideActivity(IHost host, ScopeCapture capture, Action<ILogger> write)
    {
        var logger = host.Services.GetRequiredService<ILoggerFactory>().CreateLogger("scope-test");
        using (var activity = Source.StartActivity("scope-span"))
        {
            Assert.NotNull(activity);
            write(logger);
        }

        return Assert.Single(capture.Records).Scopes;
    }

    [Fact]
    public void Generic_Host_Activity_Tracking_Scope_Is_Not_On_The_Record()
    {
        var capture = new ScopeCapture();
        using var host = BuildHost(capture);
        var scopes = LogInsideActivity(host, capture, logger =>
        {
            using (logger.BeginScope(new Dictionary<string, object?> { ["order.id"] = "ord-1" }))
            {
                logger.LogWarning("scoped");
            }
        });

        Assert.DoesNotContain(scopes, s => s.Key is "SpanId" or "TraceId" or "ParentId");
        Assert.Contains(scopes, s => s.Key == "order.id" && Equals(s.Value, "ord-1"));
    }

    [Fact]
    public void Application_Scope_With_The_Same_Keys_Is_Kept()
    {
        var capture = new ScopeCapture();
        using var host = BuildHost(capture);
        var scopes = LogInsideActivity(host, capture, logger =>
        {
            using (logger.BeginScope(new Dictionary<string, object?> { ["SpanId"] = "app-span", ["TraceId"] = "app-trace", ["ParentId"] = "app-parent" }))
            {
                logger.LogWarning("same keys");
            }
        });

        Assert.Equal(new[] { "app-span" }, scopes.Where(s => s.Key == "SpanId").Select(s => s.Value));
        Assert.Equal(new[] { "app-trace" }, scopes.Where(s => s.Key == "TraceId").Select(s => s.Value));
        Assert.Equal(new[] { "app-parent" }, scopes.Where(s => s.Key == "ParentId").Select(s => s.Value));
    }

    [Fact]
    public void Opt_In_Activity_Tags_Scope_Is_Kept()
    {
        // Tags is an application opt-in: that scope (activity.TagObjects) is not the tracking scope.
        var capture = new ScopeCapture();
        using var host = BuildHost(capture, b => b.Logging.Configure(o => o.ActivityTrackingOptions = ActivityTrackingOptions.SpanId | ActivityTrackingOptions.Tags));
        var logger = host.Services.GetRequiredService<ILoggerFactory>().CreateLogger("scope-test");
        using (var activity = Source.StartActivity("scope-span"))
        {
            activity!.SetTag("tenant", "t1");
            logger.LogWarning("tags");
        }

        var scopes = Assert.Single(capture.Records).Scopes;
        Assert.DoesNotContain(scopes, s => s.Key == "SpanId");
        Assert.Contains(scopes, s => s.Key == "tenant" && Equals(s.Value, "t1"));
    }

    [Fact]
    public void Filter_Holds_When_The_Application_Adds_OpenTelemetry_Logging_Again()
    {
        var capture = new ScopeCapture();
        using var host = BuildHost(capture, b => b.Logging.AddOpenTelemetry());
        Assert.Single(host.Services.GetServices<ILoggerProvider>().OfType<OpenTelemetryLoggerProvider>());
        var scopes = LogInsideActivity(host, capture, logger => logger.LogWarning("again"));
        Assert.DoesNotContain(scopes, s => s.Key is "SpanId" or "TraceId" or "ParentId");
    }

    [Fact]
    public void Record_Outside_An_Activity_Keeps_Application_Scopes()
    {
        var capture = new ScopeCapture();
        using var host = BuildHost(capture);
        var logger = host.Services.GetRequiredService<ILoggerFactory>().CreateLogger("scope-test");
        using (logger.BeginScope(new Dictionary<string, object?> { ["batch.size"] = 3 }))
        {
            logger.LogWarning("no activity");
        }

        var scopes = Assert.Single(capture.Records).Scopes;
        Assert.Contains(scopes, s => s.Key == "batch.size" && Equals(s.Value, 3));
    }

    /// <summary>A logger provider that keeps the scope provider the factory hands it.</summary>
    private sealed class ScopeProviderCapture : ILoggerProvider, ISupportExternalScope
    {
        public IExternalScopeProvider? ScopeProvider { get; private set; }

        public void SetScopeProvider(IExternalScopeProvider scopeProvider) => ScopeProvider = scopeProvider;

        public ILogger CreateLogger(string categoryName) => NullLogger.Instance;

        public void Dispose()
        {
        }
    }

    /// <summary>The scope objects the factory's scope provider reports under <paramref name="activity"/>.</summary>
    private static List<object?> FactoryScopes(IExternalScopeProvider provider, Activity activity)
    {
        var previous = Activity.Current;
        Activity.Current = activity;
        try
        {
            var scopes = new List<object?>();
            provider.ForEachScope((scope, list) => list.Add(scope), scopes);
            return scopes;
        }
        finally
        {
            Activity.Current = previous;
        }
    }

    [Fact]
    public void The_Factory_Scope_Matches_The_Custom_Property_And_The_Type()
    {
        var capture = new ScopeProviderCapture();
        using var factory = LoggerFactory.Create(b => b.AddProvider(capture).Configure(o => o.ActivityTrackingOptions = ActivityTrackingOptions.SpanId));
        using var activity = new Activity("tracking").Start();
        var scope = Assert.Single(FactoryScopes(capture.ScopeProvider!, activity));

        // Both origin rules hold for the real object on this runtime.
        Assert.Same(scope, activity.GetCustomProperty(ActivityScopeFilter.ActivityLogScopeProperty));
        Assert.Equal(ActivityScopeFilter.ActivityLogScopeTypeName, scope!.GetType().FullName);
        Assert.Empty(FactoryScopesFiltered(capture.ScopeProvider!, activity));
    }

    private static List<object?> FactoryScopesFiltered(IExternalScopeProvider inner, Activity activity)
        => FactoryScopes(new ActivityScopeFilter(inner), activity);

    [Fact]
    public void Type_Rule_Alone_Drops_The_Factory_Scope()
    {
        var capture = new ScopeProviderCapture();
        using var factory = LoggerFactory.Create(b => b.AddProvider(capture).Configure(o => o.ActivityTrackingOptions = ActivityTrackingOptions.SpanId));
        using var activity = new Activity("tracking").Start();
        var scope = Assert.Single(FactoryScopes(capture.ScopeProvider!, activity));

        // Another current activity: the custom-property rule no longer matches this object.
        using var other = new Activity("other").Start();
        Assert.True(ActivityScopeFilter.IsActivityTrackingScope(scope));
    }

    [Fact]
    public void Custom_Property_Rule_Alone_Drops_Only_That_Instance()
    {
        using var activity = new Activity("tracking").Start();
        var stored = new List<KeyValuePair<string, object?>> { new("SpanId", "x") };
        activity.SetCustomProperty(ActivityScopeFilter.ActivityLogScopeProperty, stored);

        Assert.True(ActivityScopeFilter.IsActivityTrackingScope(stored));
        Assert.False(ActivityScopeFilter.IsActivityTrackingScope(new List<KeyValuePair<string, object?>> { new("SpanId", "x") }));
        Assert.False(ActivityScopeFilter.IsActivityTrackingScope(null));
    }

    [Fact]
    public void No_Current_Activity_Matches_Nothing_By_Property()
    {
        Activity.Current = null;
        Assert.False(ActivityScopeFilter.IsActivityTrackingScope(new object()));
    }

    [Fact]
    public void Filter_Delegates_Push_And_Keeps_Other_Scopes()
    {
        var capture = new ScopeProviderCapture();
        using var factory = LoggerFactory.Create(b => b.AddProvider(capture).Configure(o => o.ActivityTrackingOptions = ActivityTrackingOptions.SpanId));
        var filter = new ActivityScopeFilter(capture.ScopeProvider!);
        using var activity = new Activity("tracking").Start();
        using (filter.Push("app-scope"))
        {
            var seen = FactoryScopesFiltered(capture.ScopeProvider!, activity);
            Assert.Equal(new object?[] { "app-scope" }, seen);
        }

        Assert.Empty(FactoryScopesFiltered(capture.ScopeProvider!, activity));
    }

    [Fact]
    public void Installer_Writes_Nothing()
    {
        var installer = new ActivityScopeFilterInstaller(new ServiceCollection().BuildServiceProvider());
        Assert.Same(NullLogger.Instance, installer.CreateLogger("any"));
        installer.SetScopeProvider(new ScopeProviderCapture().ScopeProvider ?? new LoggerExternalScopeProvider());
        installer.Dispose();
    }

    /// <summary>A logger provider that takes no scope provider.</summary>
    private sealed class PlainProvider : ILoggerProvider
    {
        public ILogger CreateLogger(string categoryName) => NullLogger.Instance;

        public void Dispose()
        {
        }
    }

    private static (ActivityScopeFilterInstaller Installer, TestDiagnostics Diagnostics) Installer(params ILoggerProvider[] targets)
    {
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection().AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink).BuildServiceProvider();
        return (new ActivityScopeFilterInstaller(services, _ => targets), diagnostics);
    }

    [Fact]
    public void A_Provider_That_Takes_No_Scope_Provider_Warns_Once()
    {
        var (installer, diagnostics) = Installer(new PlainProvider());
        installer.SetScopeProvider(new LoggerExternalScopeProvider());
        installer.SetScopeProvider(new LoggerExternalScopeProvider());

        var line = Assert.Single(diagnostics.Lines);
        Assert.StartsWith("otelhelper: WARN the OpenTelemetry logger provider accepts no scope provider", line);
        Assert.Contains("SpanId, TraceId, ParentId", line);
    }

    [Fact]
    public void A_Scope_Aware_Provider_Gets_The_Filter_Without_A_Warning()
    {
        var target = new ScopeProviderCapture();
        var (installer, diagnostics) = Installer(new PlainProvider(), target);
        installer.SetScopeProvider(new LoggerExternalScopeProvider());

        Assert.IsType<ActivityScopeFilter>(target.ScopeProvider);
        Assert.Empty(diagnostics.Lines);
    }

    [Fact]
    public void No_Target_Provider_Is_Silent()
    {
        var (installer, diagnostics) = Installer();
        installer.SetScopeProvider(new LoggerExternalScopeProvider());
        Assert.Empty(diagnostics.Lines);
    }
}
