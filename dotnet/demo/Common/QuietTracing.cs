using System.Diagnostics;
using OpenTelemetry;

namespace StaffOps.OtelLibs.Demo;

/// <summary>
/// Runs housekeeping calls without telemetry (DE-D11 r3.6): the worker's empty queue polls, its
/// start-up wait for the api, the api's readiness probe of the backend.
/// </summary>
public static class QuietTracing
{
    /// <summary>
    /// Runs <paramref name="work"/> with the SDK's instrumentation suppressed and, as the current
    /// activity, an unrecorded W3C parent of no source. The second part is what silences Redis: its
    /// spans are built later, on a drain thread, from the profiling session of the activity current
    /// at the command, where the suppression flag is no longer set; under an unrecorded parent the
    /// default parent-based sampler drops them. The previous current activity is restored after.
    /// </summary>
    public static async Task<T> RunAsync<T>(Func<Task<T>> work)
    {
        ArgumentNullException.ThrowIfNull(work);
        var previous = Activity.Current;
        var quiet = new Activity("otel-libs-demo.quiet");
        quiet.SetIdFormat(ActivityIdFormat.W3C);
        quiet.ActivityTraceFlags = ActivityTraceFlags.None;
        Activity.Current = null;
        quiet.Start();
        try
        {
            using (SuppressInstrumentationScope.Begin())
            {
                return await work();
            }
        }
        finally
        {
            quiet.Stop();
            Activity.Current = previous;
        }
    }

    /// <summary><see cref="RunAsync{T}(Func{Task{T}})"/> without a result.</summary>
    public static Task RunAsync(Func<Task> work)
    {
        ArgumentNullException.ThrowIfNull(work);
        return RunAsync(async () =>
        {
            await work();
            return true;
        });
    }
}
