using Grpc.Core;
using Grpc.Health.V1;
using StackExchange.Redis;

namespace StaffOps.OtelLibs.Demo.Api;

/// <summary>
/// Readiness of the api for <c>GET /health</c> (DE-D11 r3.6): Redis connected and the backend's
/// <c>grpc.health.v1</c> answering SERVING within <see cref="Timeout"/>. The answer is cached for
/// <see cref="CacheFor"/>, and the backend check is untraced (<see cref="QuietTracing"/>).
/// </summary>
public sealed class ApiHealth(IConnectionMultiplexer redis, Health.HealthClient backend, TimeProvider? clock = null)
{
    /// <summary>How long one answer is reused.</summary>
    public static readonly TimeSpan CacheFor = TimeSpan.FromSeconds(1);

    /// <summary>Deadline of the backend health call.</summary>
    public static readonly TimeSpan Timeout = TimeSpan.FromSeconds(1);

    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private (bool Ready, DateTimeOffset At)? _last;

    /// <summary>True when the api can serve orders.</summary>
    public async Task<bool> IsReadyAsync(CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken);
        try
        {
            var now = _clock.GetUtcNow();
            if (_last is { } last && now - last.At < CacheFor)
            {
                return last.Ready;
            }

            var ready = redis.IsConnected && await BackendServingAsync(cancellationToken);
            _last = (ready, now);
            return ready;
        }
        finally
        {
            _gate.Release();
        }
    }

    private Task<bool> BackendServingAsync(CancellationToken cancellationToken) => QuietTracing.RunAsync(async () =>
    {
        try
        {
            var reply = await backend.CheckAsync(new HealthCheckRequest(),
                deadline: _clock.GetUtcNow().UtcDateTime + Timeout, cancellationToken: cancellationToken);
            return reply.Status == HealthCheckResponse.Types.ServingStatus.Serving;
        }
        catch (RpcException)
        {
            return false;
        }
    });
}
