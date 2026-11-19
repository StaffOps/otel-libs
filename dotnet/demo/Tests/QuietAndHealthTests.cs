using System.Diagnostics;
using Grpc.Core;
using Grpc.Health.V1;
using OpenTelemetry;
using StaffOps.OtelLibs.Demo.Api;

namespace StaffOps.OtelLibs.Demo.Tests;

public class QuietTracingTests
{
    [Fact]
    public async Task RunAsync_UsesAnUnrecordedParentAndSuppressionThenRestores()
    {
        using var spans = new SpanCollector();
        using var outer = DemoTelemetry.Source.StartActivity("outer");
        var seen = await QuietTracing.RunAsync(() =>
        {
            var current = Activity.Current!;
            return Task.FromResult((current.OperationName, current.Recorded, current.IdFormat, current.Parent, Sdk.SuppressInstrumentation));
        });

        Assert.Equal(("otel-libs-demo.quiet", false, ActivityIdFormat.W3C, (Activity?)null, true), seen);
        Assert.Same(outer, Activity.Current);
        Assert.False(Sdk.SuppressInstrumentation);
        Assert.Single(spans.Started); // only "outer": the quiet activity has no source
    }

    [Fact]
    public async Task RunAsync_RestoresAfterAFailure()
    {
        Activity.Current = null;
        await Assert.ThrowsAsync<InvalidOperationException>(() => QuietTracing.RunAsync(() => throw new InvalidOperationException()));
        Assert.Null(Activity.Current);
        var ran = false;
        await QuietTracing.RunAsync(() => { ran = true; return Task.CompletedTask; });
        Assert.True(ran);
        await Assert.ThrowsAsync<ArgumentNullException>(() => QuietTracing.RunAsync((Func<Task<int>>)null!));
        await Assert.ThrowsAsync<ArgumentNullException>(() => QuietTracing.RunAsync((Func<Task>)null!));
    }
}

public class ApiHealthTests
{
    private sealed class Clock : TimeProvider
    {
        public DateTimeOffset Now { get; set; } = new(2026, 11, 16, 20, 0, 0, TimeSpan.Zero);
        public override DateTimeOffset GetUtcNow() => Now;
    }

    private static HealthCheckResponse Status(HealthCheckResponse.Types.ServingStatus status) => new() { Status = status };

    [Fact]
    public async Task Ready_WhenRedisIsConnectedAndTheBackendServes_CachedForOneSecond()
    {
        var redis = new FakeRedis();
        var invoker = new FakeInvoker(_ => Status(HealthCheckResponse.Types.ServingStatus.Serving));
        var clock = new Clock();
        var health = new ApiHealth(redis.Multiplexer, new Health.HealthClient(invoker), clock);

        Assert.True(await health.IsReadyAsync(CancellationToken.None));
        Assert.True(await health.IsReadyAsync(CancellationToken.None));
        Assert.Single(invoker.Requests);

        clock.Now += ApiHealth.CacheFor;
        redis.Connected = false;
        Assert.False(await health.IsReadyAsync(CancellationToken.None));
        Assert.Single(invoker.Requests); // Redis down: the backend is not asked
    }

    [Theory]
    [InlineData(HealthCheckResponse.Types.ServingStatus.NotServing)]
    [InlineData(HealthCheckResponse.Types.ServingStatus.Unknown)]
    public async Task NotReady_WhenTheBackendDoesNotServe(HealthCheckResponse.Types.ServingStatus status)
    {
        var health = new ApiHealth(new FakeRedis().Multiplexer, new Health.HealthClient(new FakeInvoker(_ => Status(status))));
        Assert.False(await health.IsReadyAsync(CancellationToken.None));
    }

    [Fact]
    public async Task NotReady_WhenTheBackendCallFails()
    {
        var health = new ApiHealth(new FakeRedis().Multiplexer,
            new Health.HealthClient(new FakeInvoker(_ => throw new RpcException(new Grpc.Core.Status(StatusCode.Unavailable, "down")))));
        Assert.False(await health.IsReadyAsync(CancellationToken.None));
    }
}
