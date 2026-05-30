using System.Diagnostics;
using System.Diagnostics.Metrics;
using OtelHelper;

/// <summary>
/// Simulates a scheduled job with timeout detection.
/// Demonstrates: StartRootActivity, span ERROR on timeout, observable gauge for circuit state.
/// </summary>
public class ScheduledJobWorker : BackgroundService
{
    private static readonly ActivitySource _activity = new("sample-process.scheduled-job");
    private static readonly Meter _meter = new(
        Environment.GetEnvironmentVariable("SERVICE_NAME") ?? "sample-process");

    private static readonly Counter<long> _jobsTotal = _meter.CreateCounter<long>(
        "scheduled_job.runs_total", "jobs", "Total job runs");
    private static readonly Counter<long> _jobsTimedOut = _meter.CreateCounter<long>(
        "scheduled_job.timeouts_total", "jobs", "Total job timeouts");
    private static readonly Histogram<double> _jobDuration = _meter.CreateHistogram<double>(
        "scheduled_job.duration_seconds", "s", "Duration of each job run");

    // Circuit breaker state: 0=closed, 1=open, 2=half-open
    private static int _circuitState = 0;
    private static int _consecutiveFailures = 0;
    private static DateTime _circuitOpenedAt = DateTime.MinValue;

    private readonly IHttpClientFactory _httpFactory;
    private readonly ILogger<ScheduledJobWorker> _logger;
    private readonly TimeSpan _interval = TimeSpan.FromMinutes(3);
    private readonly TimeSpan _jobTimeout = TimeSpan.FromSeconds(5);

    public ScheduledJobWorker(IHttpClientFactory httpFactory, ILogger<ScheduledJobWorker> logger)
    {
        _httpFactory = httpFactory;
        _logger = logger;
        _meter.CreateObservableGauge("circuit_breaker.state", () => _circuitState,
            description: "Circuit breaker state: 0=closed, 1=open, 2=half-open");
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        await Task.Delay(20000, ct);

        while (!ct.IsCancellationRequested)
        {
            using var span = _activity.StartRootActivity("process.scheduled-job");
            span?.SetTag("circuit_breaker.state", _circuitState switch { 0 => "closed", 1 => "open", _ => "half-open" });

            var sw = Stopwatch.StartNew();

            try
            {
                if (_circuitState == 1) // open
                {
                    // Check if enough time passed to try again
                    if (DateTime.UtcNow - _circuitOpenedAt > TimeSpan.FromSeconds(30))
                    {
                        _circuitState = 2; // half-open
                        _logger.LogInformation("Circuit breaker: half-open, attempting recovery");
                    }
                    else
                    {
                        span?.SetTag("circuit_breaker.action", "rejected");
                        _logger.LogWarning("Circuit breaker OPEN — skipping job");
                        _jobsTotal.Add(1, new KeyValuePair<string, object?>("result", "circuit-open"));
                        await Task.Delay(_interval, ct);
                        continue;
                    }
                }

                // Execute job with timeout
                using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
                cts.CancelAfter(_jobTimeout);

                await ExecuteJob(span, cts.Token);

                // Success — reset circuit
                _consecutiveFailures = 0;
                _circuitState = 0;
                _jobsTotal.Add(1, new KeyValuePair<string, object?>("result", "success"));
                _logger.LogInformation("Scheduled job completed in {Duration}ms", sw.ElapsedMilliseconds);
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested)
            {
                span?.SetStatus(ActivityStatusCode.Error, "Job timed out");
                span?.SetTag("timeout", true);
                _jobsTimedOut.Add(1);
                _jobsTotal.Add(1, new KeyValuePair<string, object?>("result", "timeout"));
                _logger.LogError("Scheduled job TIMED OUT after {Timeout}s", _jobTimeout.TotalSeconds);
                HandleFailure();
            }
            catch (Exception ex) when (!ct.IsCancellationRequested)
            {
                span?.SetStatus(ActivityStatusCode.Error, ex.Message);
                _jobsTotal.Add(1, new KeyValuePair<string, object?>("result", "error"));
                _logger.LogError(ex, "Scheduled job failed");
                HandleFailure();
            }
            finally
            {
                _jobDuration.Record(sw.Elapsed.TotalSeconds);
            }

            await Task.Delay(_interval, ct);
        }
    }

    private async Task ExecuteJob(Activity? parentSpan, CancellationToken ct)
    {
        // Step 1: fetch data (sometimes slow — triggers timeout)
        using (var fetchSpan = _activity.StartActivity("process.job-fetch-data"))
        {
            var delay = Random.Shared.Next(100, 7000); // sometimes exceeds 5s timeout
            fetchSpan?.SetTag("estimated_duration_ms", delay);
            await Task.Delay(delay, ct);
        }

        // Step 2: transform
        using (var transformSpan = _activity.StartActivity("process.job-transform"))
        {
            await Task.Delay(Random.Shared.Next(50, 200), ct);
        }

        // Step 3: persist
        using (var persistSpan = _activity.StartActivity("process.job-persist"))
        {
            persistSpan?.SetTag("db.system", "postgresql");
            await Task.Delay(Random.Shared.Next(20, 100), ct);
        }
    }

    private void HandleFailure()
    {
        _consecutiveFailures++;
        if (_consecutiveFailures >= 3 && _circuitState != 1)
        {
            _circuitState = 1; // open
            _circuitOpenedAt = DateTime.UtcNow;
            _logger.LogWarning("Circuit breaker OPENED after {Failures} consecutive failures", _consecutiveFailures);
        }
    }
}
