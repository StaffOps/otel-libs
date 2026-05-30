using System.Diagnostics;
using System.Diagnostics.Metrics;
using OtelHelper;

/// <summary>
/// Runs every minute — calls all SampleApi endpoints and records results as metrics.
/// Each endpoint call starts a fresh trace (no parent propagation) so traces are distinct per endpoint type.
/// </summary>
public class ApiHealthWorker : BackgroundService
{
    private static readonly ActivitySource _activity = new("sample-process.api-health");
    private static readonly Meter _meter = new(
        Environment.GetEnvironmentVariable("SERVICE_NAME") ?? "sample-process");

    private static readonly Counter<long> _checksTotal = _meter.CreateCounter<long>(
        "api_health.checks_total", "checks", "Total health checks executed");
    private static readonly Counter<long> _checksFailedTotal = _meter.CreateCounter<long>(
        "api_health.checks_failed_total", "checks", "Total failed health checks");
    private static readonly Histogram<double> _checkDuration = _meter.CreateHistogram<double>(
        "api_health.check_duration_seconds", "s", "Duration of each health check");

    private readonly IHttpClientFactory _httpFactory;
    private readonly ILogger<ApiHealthWorker> _logger;
    private readonly TimeSpan _interval = TimeSpan.FromMinutes(1);

    private static readonly (string path, string spanName)[] Endpoints = new[]
    {
        ("/", "process.api-check"),
        ("/order/1", "process.api-order"),
        ("/order/1/cancel", "process.api-cancel"),
        ("/health/ready", "process.api-health-ready"),
        ("/batch", "process.api-batch"),
        ("/error", "process.api-simulate-error")
    };

    public ApiHealthWorker(IHttpClientFactory httpFactory, ILogger<ApiHealthWorker> logger)
    {
        _httpFactory = httpFactory;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        await Task.Delay(5000, ct);

        while (!ct.IsCancellationRequested)
        {
            _logger.LogInformation("Starting API health check cycle");

            var client = _httpFactory.CreateClient("sample-api");

            foreach (var (endpoint, spanName) in Endpoints)
            {
                // Each check is a separate root trace
                using var check = _activity.StartRootActivity(spanName);
                check?.SetTag("endpoint", endpoint);

                var sw = Stopwatch.StartNew();
                try
                {
                    var response = await client.GetAsync(endpoint, ct);
                    var status = (int)response.StatusCode;
                    check?.SetTag("http.status_code", status);

                    _checksTotal.Add(1, new KeyValuePair<string, object?>("endpoint", endpoint));

                    if (status >= 500 && endpoint != "/error")
                    {
                        _checksFailedTotal.Add(1, new KeyValuePair<string, object?>("endpoint", endpoint));
                        _logger.LogWarning("Health check failed: {Endpoint} returned {Status}", endpoint, status);
                    }
                    else
                    {
                        _logger.LogInformation("Health check OK: {Endpoint} returned {Status}", endpoint, status);
                    }
                }
                catch (Exception ex)
                {
                    _checksTotal.Add(1, new KeyValuePair<string, object?>("endpoint", endpoint));
                    _checksFailedTotal.Add(1, new KeyValuePair<string, object?>("endpoint", endpoint));
                    check?.SetStatus(ActivityStatusCode.Error, ex.Message);
                    _logger.LogError(ex, "Health check error: {Endpoint}", endpoint);
                }
                finally
                {
                    _checkDuration.Record(sw.Elapsed.TotalSeconds,
                        new KeyValuePair<string, object?>("endpoint", endpoint));
                }
            }

            _logger.LogInformation("API health check cycle completed, next in {Interval}", _interval);
            await Task.Delay(_interval, ct);
        }
    }
}
