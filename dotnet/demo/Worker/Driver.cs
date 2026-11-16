using System.Diagnostics;
using System.Net.Http.Json;
using StaffOps.OtelHelper;

namespace StaffOps.OtelLibs.Demo.Worker;

/// <summary>
/// The load driver (DE-D7): one <c>POST /orders</c> every <c>DEMO_INTERVAL_SECONDS</c> (5 s),
/// cycling the fixed sequence of ten scenarios, each in a new root span <c>demo.drive</c>.
/// </summary>
public sealed class Driver(HttpClient api, ILogger<Driver> logger, TimeSpan interval) : BackgroundService
{
    /// <summary>Sends the request numbered <paramref name="seq"/> in its own trace.</summary>
    public async Task DriveAsync(long seq, CancellationToken cancellationToken)
    {
        var order = ScenarioCycle.Plan(seq);
        using var drive = DemoTelemetry.Source.StartRootActivity("demo.drive");
        drive?.SetTag("demo.scenario", order.Scenario);
        try
        {
            using var response = await api.PostAsJsonAsync("orders", OrderRequest.From(order), cancellationToken);
            if (response.IsSuccessStatusCode)
            {
                drive?.SetTag("demo.outcome", "ok");
                return;
            }

            // DE-D8: a non-2xx answer is a propagated failure, not an exception in the worker:
            // status ERROR, error.type = the status code, no exception event, one WARN record.
            var status = (int)response.StatusCode;
            drive?.SetStatus(ActivityStatusCode.Error);
            drive?.SetTag("error.type", status.ToString(System.Globalization.CultureInfo.InvariantCulture));
            drive?.SetTag("demo.outcome", "error");
            DemoLog.Write(logger, LogLevel.Warning, "order request failed", null,
                ("http.response.status_code", status), ("demo.scenario", order.Scenario));
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException && !cancellationToken.IsCancellationRequested)
        {
            // Transport failure (api not reachable): the type on the span, the detail in the log.
            drive?.SetStatus(ActivityStatusCode.Error);
            drive?.SetTag("error.type", Failure.ErrorType(ex));
            drive?.SetTag("demo.outcome", "error");
            DemoLog.Write(logger, LogLevel.Warning, "order request failed", ex, ("demo.scenario", order.Scenario));
        }
    }

    /// <inheritdoc />
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await Task.Yield();
        using var timer = new PeriodicTimer(interval);
        long seq = 0;
        do
        {
            await DriveAsync(++seq, stoppingToken);
        }
        while (await timer.WaitForNextTickAsync(stoppingToken));
    }
}
