// Parity fixture driver: runs ONE case of parity/cases.json in a fresh .NET
// process (design D-NET12; format: parity/README.md).
//
//   dotnet StaffOps.OtelHelper.ParityDriver.dll <case.json>
//
// The runner (ParityTests) applies the case env and expands the {receiver}
// placeholders before spawning it. OTel .NET state is process-wide, so every
// case gets its own process. Stdout carries exactly one JSON snapshot: text the
// process writes to Console goes to stderr, and the parent reads stderr as the
// case's `console`. The driver always exits 0: coverlet writes the child's hits
// on process exit, and an abnormal exit would lose them, so failures are data in
// the snapshot (`error`, `adapter_error`, `driver_crash`). Matching happens in
// the parent.
//
// `adapter_error` is a fixture/adapter bug (unknown kind, scenario or code key
// with no 0.2 API); the case fails whatever its status.

using System.Diagnostics.CodeAnalysis;
using System.Diagnostics.Tracing;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

[assembly: ExcludeFromCodeCoverage]

namespace StaffOps.OtelHelper.ParityDriver;

internal static class Program
{
    /// <summary>Above the longest shutdown budget, below the runner's 120 s kill.</summary>
    private const int OverallDeadlineMs = 110_000;

    public static int Main(string[] args)
    {
        if (args.Length != 1)
        {
            Console.Error.WriteLine("usage: StaffOps.OtelHelper.ParityDriver <case.json> (spawned by ParityTests)");
            return 2;
        }

        // Keep the real stdout for the snapshot; Console output becomes `console` via stderr.
        var realStdout = Console.OpenStandardOutput();
        Console.SetOut(new StderrWriter());
        using var sdkEvents = new SdkEventListener();

        JsonObject snapshot;
        try
        {
            var testCase = JsonNode.Parse(File.ReadAllText(args[0]))!.AsObject();
            var work = Task.Run(() => CaseKinds.RunAsync(testCase));
            if (work.Wait(OverallDeadlineMs))
            {
                snapshot = work.Result;
            }
            else
            {
                snapshot = new JsonObject { ["adapter_error"] = $"driver deadline of {OverallDeadlineMs} ms exceeded" };
            }
        }
        catch (Exception ex)
        {
            snapshot = new JsonObject { ["driver_crash"] = ex.ToString() };
        }

        // The 0.2 helper has no diagnostics channel (task C5): nothing of its own to report.
        // SDK EventSource events go to `sdk_events`, which no matcher reads, until C5
        // forwards them through the helper's sink.
        snapshot["diagnostics"] = "";
        snapshot["helper_log"] = "";
        snapshot["sdk_events"] = sdkEvents.Joined();

        var bytes = Encoding.UTF8.GetBytes(snapshot.ToJsonString());
        realStdout.Write(bytes, 0, bytes.Length);
        realStdout.Flush();
        Console.Out.Flush();
        Console.Error.Flush();

        // Normal exit (coverlet flushes on ProcessExit), also with exporter threads
        // still retrying a wrong port.
        Environment.Exit(0);
        return 0;
    }
}

/// <summary>Sends everything written to Console.Out to the real stderr.</summary>
internal sealed class StderrWriter : TextWriter
{
    public override Encoding Encoding => Encoding.UTF8;
    public override void Write(char value) => Console.Error.Write(value);
    public override void Write(string? value) => Console.Error.Write(value);
    public override void Write(ReadOnlySpan<char> buffer) => Console.Error.Write(buffer);
    public override void WriteLine(string? value) => Console.Error.WriteLine(value);
}

/// <summary>
/// Captures OpenTelemetry SDK events at Warning and above into `sdk_events` (not
/// matched). They are not printed: the process does not print them either, so
/// `console` and `log` hold only what the process really writes. Task C5 forwards
/// them through the helper's diagnostics sink.
/// </summary>
internal sealed class SdkEventListener : EventListener
{
    private readonly List<string> _lines = new();
    private static readonly object Gate = new();

    protected override void OnEventSourceCreated(EventSource eventSource)
    {
        if (eventSource.Name.StartsWith("OpenTelemetry", StringComparison.Ordinal))
        {
            EnableEvents(eventSource, EventLevel.Warning);
        }
    }

    protected override void OnEventWritten(EventWrittenEventArgs eventData)
    {
        var payload = eventData.Payload?.ToArray() ?? Array.Empty<object?>();
        string line;
        try
        {
            line = eventData.Message is null
                ? string.Join(" ", payload)
                : string.Format(eventData.Message, payload);
        }
        catch (FormatException)
        {
            line = $"{eventData.Message} {string.Join(" ", payload)}";
        }

        line = line.ReplaceLineEndings(" ");
        lock (Gate)
        {
            _lines.Add(line);
        }
    }

    public string Joined()
    {
        lock (Gate)
        {
            return string.Join("\n", _lines);
        }
    }
}
