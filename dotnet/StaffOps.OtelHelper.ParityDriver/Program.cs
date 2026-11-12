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
// without an option); the case fails whatever its status.

using System.Diagnostics.CodeAnalysis;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using StaffOps.OtelHelper.Diagnostics;

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

        // The helper's sink (design D-NET5): `diagnostics` = WARN and above of both origins
        // (the helper and the SDK events it forwards), `helper_log` = every helper record.
        snapshot["diagnostics"] = DriverDiagnostics.Diagnostics();
        snapshot["helper_log"] = DriverDiagnostics.HelperLog();

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
/// The helper diagnostics sink the driver registers before AddOtelHelper: it writes
/// each record to stderr as an application's sink would (so `console` sees it) and
/// keeps a copy for `diagnostics` and `helper_log`.
/// </summary>
internal static class DriverDiagnostics
{
    private static readonly List<DiagnosticRecord> Records = new();

    public static OtelHelperDiagnostics Sink { get; } = new(() => Console.Error)
    {
        Observer = record =>
        {
            lock (Records)
            {
                Records.Add(record);
            }
        },
    };

    public static string Diagnostics() => Join(r => r.Level != DiagnosticLevel.Info);

    public static string HelperLog() => Join(r => r.Origin == DiagnosticOrigin.Helper);

    private static string Join(Func<DiagnosticRecord, bool> keep)
    {
        lock (Records)
        {
            return string.Join("\n", Records.Where(keep).Select(r => r.Line));
        }
    }
}
