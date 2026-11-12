// Parity fixture runner for .NET (design D-NET12; ADR-008; format and
// semantics: parity/README.md). Each case in parity/cases.json runs in its own
// process (StaffOps.OtelHelper.ParityDriver); status `pass` must pass,
// `xfail:C<n>` must fail on a behavior mismatch (strict), `skip:<reason>` is
// never spawned. Fixture and adapter errors fail whatever the status.
//
//   dotnet test --filter "FullyQualifiedName~ParityTests"
//   PARITY_ONLY=export.grpc_plaintext,svc.code_beats_env dotnet test --filter ...
//
// Environment: FAKEOTLP_* (testing/README.md), PARITY_CASES (fixture path),
// PARITY_TASKS (tasks.md path), PARITY_REQUIRE_FAKEOTLP (a receiver-dependent
// case fails instead of being reported as not run), PARITY_RELEASE (release
// gate), PARITY_ONLY (comma-separated ids to run), PARITY_RESULTS (write every
// outcome as JSON to this path). Requirement: DN11.

using System.Text.Json.Nodes;
using Xunit;
using Xunit.Abstractions;

namespace StaffOps.OtelHelper.Tests;

public class ParityTests
{
    private readonly ITestOutputHelper _output;

    public ParityTests(ITestOutputHelper output)
    {
        _output = output;
    }

    private const string FixtureMissingRow = "<parity fixture missing>";

    public static IEnumerable<object[]> CaseIds() =>
        ParityHarness.CasesPath is null
            ? new[] { new object[] { FixtureMissingRow } }
            : ParityRun.RunnableCases.Select(c => new object[] { ParityHarness.IdOf(c) });

    [Fact]
    public void Fixture_is_found()
    {
        Assert.True(ParityHarness.CasesPath is not null, ParityHarness.MissingFixtureMessage);
    }

    /// <summary>Skipped cases are never spawned; this lists them with their reasons.</summary>
    [Fact]
    public void Skipped_cases_are_listed_with_their_reasons()
    {
        foreach (var c in ParityRun.SelectedCases.Where(c => ParityHarness.StatusOf(c).StartsWith("skip:", StringComparison.Ordinal)))
        {
            _output.WriteLine($"skipped {ParityHarness.IdOf(c)}: {ParityHarness.StatusOf(c)}");
        }
    }

    /// <summary>DN11: every non-skipped fixture case, in its own process, under its status.</summary>
    [Theory]
    [MemberData(nameof(CaseIds))]
    public void Case(string id)
    {
        Assert.False(id == FixtureMissingRow, ParityHarness.MissingFixtureMessage);
        var testCase = ParityHarness.AllCases.Single(c => ParityHarness.IdOf(c) == id);
        var (verdict, reason) = ParityHarness.Evaluate(testCase, ParityRun.Result(id));
        switch (verdict)
        {
            case ParityVerdict.NeedsReceiver:
                _output.WriteLine($"not run: {reason}");
                break;
            case ParityVerdict.ExpectedFailure:
                _output.WriteLine($"expected failure ({ParityHarness.StatusOf(testCase)}): {reason}");
                break;
            case ParityVerdict.Failed:
                Assert.Fail(reason);
                break;
        }
    }

    [Fact]
    public void Fixture_is_well_formed_ids_unique_every_case_valid()
    {
        var ids = ParityHarness.AllCases.Select(ParityHarness.IdOf).ToList();
        Assert.Equal(ids.Count, ids.Distinct(StringComparer.Ordinal).Count());
        var problems = ParityHarness.AllCases
            .Select(c => (Id: ParityHarness.IdOf(c), Errors: ParityHarness.ValidateCase(c)))
            .Where(p => p.Errors.Count > 0)
            .Select(p => $"{p.Id}: {string.Join("; ", p.Errors)}")
            .ToList();
        Assert.True(problems.Count == 0, string.Join("\n", problems));
    }

    [Fact]
    public void Every_dotnet_skip_names_an_open_task_or_a_recorded_difference()
    {
        if (ParityHarness.CasesPath is null)
        {
            return; // Fixture_is_found reports the missing fixture once.
        }

        var openTasks = OpenTasks(out var doneTasks);
        var bad = new List<string>();
        foreach (var c in ParityHarness.AllCases)
        {
            var status = ParityHarness.StatusOf(c);
            if (!status.StartsWith("skip:", StringComparison.Ordinal))
            {
                continue;
            }

            var task = ParityHarness.TaskOfSkip(status);
            if (task is null)
            {
                if (!ParityHarness.IsPermanentSkip(status))
                {
                    bad.Add($"{ParityHarness.IdOf(c)}: {status} names no task and no D-NET15 difference");
                }
            }
            else if (doneTasks.Contains(task))
            {
                bad.Add($"{ParityHarness.IdOf(c)}: {status} names task {task}, which is done");
            }
            else if (!openTasks.Contains(task))
            {
                bad.Add($"{ParityHarness.IdOf(c)}: {status} names task {task}, which is not in tasks.md");
            }
        }

        Assert.True(bad.Count == 0, string.Join("\n", bad));
    }

    [Fact]
    public void Release_gate_no_dotnet_case_still_xfail_or_skipped_pending_a_task()
    {
        if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("PARITY_RELEASE")))
        {
            _output.WriteLine("set PARITY_RELEASE=1 for the release gate");
            return;
        }

        // DN11: no xfail, no skip:phase-c and no skip:C<n>-… left at a release.
        var pending = ParityHarness.AllCases
            .Where(c => ParityHarness.StatusOf(c) is var s
                && (s.StartsWith("xfail:", StringComparison.Ordinal) || s == "skip:phase-c" || ParityHarness.TaskOfSkip(s) is not null))
            .Select(c => $"{ParityHarness.IdOf(c)}: {ParityHarness.StatusOf(c)}")
            .ToList();
        Assert.True(pending.Count == 0, string.Join("\n", pending));
    }

    /// <summary>Task ids from the tasks.md table: `| C4 ...` rows; a row with ✅ is done.</summary>
    private static HashSet<string> OpenTasks(out HashSet<string> done)
    {
        var open = new HashSet<string>(StringComparer.Ordinal);
        done = new HashSet<string>(StringComparer.Ordinal);
        foreach (var line in File.ReadAllLines(ParityHarness.TasksPath))
        {
            var m = System.Text.RegularExpressions.Regex.Match(line, @"^\| (C\d+[a-z]?)[ -]");
            if (!m.Success)
            {
                continue;
            }

            var finishedRow = line.TrimEnd().TrimEnd('|').TrimEnd().EndsWith("✅", StringComparison.Ordinal)
                || line.Contains("| ✅", StringComparison.Ordinal);
            (finishedRow ? done : open).Add(m.Groups[1].Value);
        }

        Assert.True(open.Count + done.Count > 0, $"no task rows found in {ParityHarness.TasksPath}");
        return open;
    }
}

/// <summary>The matcher and schema are test infrastructure; they are tested too (Node harness tests).</summary>
public class ParityHarnessTests
{
    private static JsonObject Obj(string json) => JsonNode.Parse(json)!.AsObject();

    [Theory]
    [InlineData("""{"a": {"prefix": "unknown_service"}, "n": 3, "warning": "OTEL_FOO"}""", true)]
    [InlineData("""{"b": {"absent": true}}""", true)]
    [InlineData("""{"a": {"absent": false}, "n": {"absent": false}}""", true)]
    [InlineData("""{"zz": {"absent": false}}""", false)]
    [InlineData("""{"a": {"all": [{"regex": "node$"}, {"not_contains": "go"}]}}""", true)]
    [InlineData("""{"warning_count": {"OTEL_FOO": 2}}""", true)]
    [InlineData("""{"warning_count": {"OTEL_FOO": 1}}""", false)]
    [InlineData("""{"a": {"not_contains": "node"}}""", false)]
    [InlineData("""{"a": {"regex": "("}}""", false)]
    [InlineData("""{"n": 4}""", false)]
    [InlineData("""{"n": 3}""", true)]
    [InlineData("""{"warning": ["OTEL_FOO", "OTEL_BAR"]}""", false)]
    [InlineData("""{"n": {"prefix": "3"}}""", false)]
    [InlineData("""{"n": {"not_contains": "x"}}""", false)]
    [InlineData("""{"zz": {"not_contains": "x"}}""", true)]
    [InlineData("""{"t": {"lte": 500, "gte": 1}}""", true)]
    [InlineData("""{"t": {"lte": 100}}""", false)]
    [InlineData("""{"r": {"service.name": "x"}}""", true)]
    [InlineData("""{"l": ["a", "b"]}""", true)]
    [InlineData("""{"l": ["b", "a"]}""", false)]
    [InlineData("""{"m": [1.0, {"a": 1.0}]}""", true)]
    [InlineData("""{"m": [1, {"a": 2}]}""", false)]
    public void MatchExpect_operators(string expect, bool matches)
    {
        var snap = Obj("""{"a": "unknown_service:node", "n": 3, "t": 499, "r": {"service.name": "x", "other": "y"}, "m": [1, {"a": 1}], "l": ["a", "b"], "log": "x OTEL_FOO y", "diagnostics": "OTEL_FOO\nOTEL_FOO"}""");
        var mismatches = ParityHarness.MatchExpect(Obj(expect), snap);
        Assert.Equal(matches, mismatches.Count == 0);
    }

    [Fact]
    public void MatchExpect_an_unexpected_error_fails_unless_expected()
    {
        Assert.Single(ParityHarness.MatchExpect(new JsonObject(), Obj("""{"error": "boom"}""")));
        Assert.Empty(ParityHarness.MatchExpect(Obj("""{"error": "boo"}"""), Obj("""{"error": "boom"}""")));
        Assert.NotEmpty(ParityHarness.MatchExpect(Obj("""{"error": "zzz"}"""), Obj("""{"error": "boom"}""")));
    }

    [Fact]
    public void Evaluate_strict_xfail_only_a_behavior_mismatch_satisfies_it()
    {
        var xfail = Obj("""{"id": "x", "kind": "resolve", "env": {}, "code": {}, "expect": {"log_level": "INFO"}, "status": {"go": "pass", "python": "pass", "dotnet": "xfail:C5", "nodejs": "pass"}}""");
        Assert.Equal(ParityVerdict.Failed, ParityHarness.Evaluate(xfail, new ParityResult(Obj("""{"log_level": "INFO"}"""))).Verdict);
        Assert.Equal(ParityVerdict.ExpectedFailure, ParityHarness.Evaluate(xfail, new ParityResult(Obj("""{"log_level": "WARN"}"""))).Verdict);
        Assert.Equal(ParityVerdict.Failed, ParityHarness.Evaluate(xfail, ParityResult.Crashed("boom")).Verdict);
    }

    [Theory]
    [InlineData("""{"id": "a", "kind": "nope", "env": {}, "code": {}, "expect": {}, "status": {"go": "pass", "python": "pass", "dotnet": "pass", "nodejs": "pass"}}""", "unknown kind")]
    [InlineData("""{"id": "a", "kind": "resolve", "env": {}, "code": {}, "expect": {"bogus": 1}, "status": {"go": "pass", "python": "pass", "dotnet": "pass", "nodejs": "pass"}}""", "not in the resolve snapshot schema")]
    [InlineData("""{"id": "a", "kind": "resolve", "env": {}, "code": {}, "expect": {"log_level": {"regex": ".", "contians": "x"}}, "status": {"go": "pass", "python": "pass", "dotnet": "pass", "nodejs": "pass"}}""", "is not a matcher op")]
    [InlineData("""{"id": "a", "kind": "resolve", "env": {}, "code": {}, "expect": {}, "status": {"go": "pass", "python": "pass", "dotnet": "bad status", "nodejs": "pass"}}""", "status.dotnet invalid")]
    public void ValidateCase_reports_malformed_cases(string json, string error)
    {
        Assert.Contains(ParityHarness.ValidateCase(Obj(json)), e => e.Contains(error, StringComparison.Ordinal));
    }

    [Fact]
    public void BaseChildEnvironment_resets_otel_identity_and_host_mapped_keys()
    {
        var parent = new Dictionary<string, string>
        {
            ["OTEL_SERVICE_NAME"] = "x", ["otel_traces_sampler"] = "x", ["SERVICE_NAME"] = "x", ["ENVIRONMENT"] = "x",
            ["DOTNET_OTEL_SDK_DISABLED"] = "true", ["ASPNETCORE_ENVIRONMENT"] = "x", ["ASPNETCORE_OTEL_SERVICE_NAME"] = "x",
            ["Logging__LogLevel__Default"] = "None", ["PATH"] = "/bin", ["SERVICE_NAMES"] = "kept", ["DOTNET_ROLL_FORWARD"] = "Major",
        };
        var env = ParityHarness.BaseChildEnvironment(parent, Obj("""{"OTEL_SERVICE_NAME": "case"}"""), null);
        Assert.Equal(new[] { "DOTNET_ROLL_FORWARD", "OTEL_HELPER_METRICS_PORT", "OTEL_SERVICE_NAME", "PATH", "SERVICE_NAMES" },
            env.Keys.Order(StringComparer.Ordinal).ToArray());
        Assert.Equal("case", env["OTEL_SERVICE_NAME"]);
        Assert.Equal("0", env["OTEL_HELPER_METRICS_PORT"]);
        Assert.Equal("9100", ParityHarness.BaseChildEnvironment(parent, Obj("""{"OTEL_HELPER_METRICS_PORT": "9100"}"""), null)["OTEL_HELPER_METRICS_PORT"]);
    }

    private static Dictionary<string, string> ShellEnv() => new() { ["PATH"] = Environment.GetEnvironmentVariable("PATH") ?? "/usr/bin:/bin" };

    [Fact]
    public async Task SpawnAsync_non_json_stdout_is_a_crash()
    {
        var result = await ParityHarness.SpawnAsync("sh", new[] { "-c", "echo not-json" }, ShellEnv(), 10_000);
        Assert.Contains("not one JSON snapshot", result.Crash);
    }

    [Fact]
    public async Task SpawnAsync_nonzero_exit_is_a_crash_even_with_json_stdout()
    {
        var result = await ParityHarness.SpawnAsync("sh", new[] { "-c", "echo '{}'; exit 3" }, ShellEnv(), 10_000);
        Assert.Contains("rc=3", result.Crash);
    }

    [Fact]
    public async Task SpawnAsync_snapshot_gets_console_and_log()
    {
        var result = await ParityHarness.SpawnAsync("sh", new[] { "-c", "echo '{\"diagnostics\": \"d\"}'; echo err >&2" }, ShellEnv(), 10_000);
        Assert.Null(result.Crash);
        Assert.Equal("err\n", (string?)result.Snapshot!["console"]);
        Assert.Equal("d\nerr\n", (string?)result.Snapshot!["log"]);
    }

    [Fact]
    public async Task SpawnAsync_kills_a_child_past_its_timeout()
    {
        var result = await ParityHarness.SpawnAsync("sh", new[] { "-c", "sleep 30" }, ShellEnv(), 300);
        Assert.Contains("killed after 300 ms", result.Crash);
    }

    [Fact]
    public void Evaluate_missing_receiver_fails_only_when_required()
    {
        var c = Obj("""{"id": "x", "kind": "export", "env": {}, "code": {}, "expect": {}, "status": {"go": "pass", "python": "pass", "dotnet": "pass", "nodejs": "pass"}}""");
        Assert.Equal(ParityVerdict.Failed, ParityHarness.Evaluate(c, ParityResult.NeedsReceiver("FAKEOTLP_HOST"), requireReceiver: true).Verdict);
        Assert.Equal(ParityVerdict.NeedsReceiver, ParityHarness.Evaluate(c, ParityResult.NeedsReceiver("FAKEOTLP_HOST"), requireReceiver: false).Verdict);
    }

    [Theory]
    [InlineData("skip:no-prefork-model", true)]
    [InlineData("skip:no-upstream-dotnet-prom-mounted-handler", true)]
    [InlineData("skip:no-upstream-dotnet-http-body-size-x", false)]
    [InlineData("skip:no-whatever", false)]
    public void IsPermanentSkip_exact_set_and_prom_family(string status, bool expected)
    {
        Assert.Equal(expected, ParityHarness.IsPermanentSkip(status));
    }

    [Fact]
    public void BindsFixedPort_reads_env_and_code_ports()
    {
        Assert.True(ParityRun.BindsFixedPort(Obj("""{"env": {"OTEL_HELPER_METRICS_PORT": "9100"}, "code": {}}""")));
        Assert.True(ParityRun.BindsFixedPort(Obj("""{"env": {"OTEL_EXPORTER_PROMETHEUS_PORT": "0"}, "code": {}}""")));
        Assert.True(ParityRun.BindsFixedPort(Obj("""{"env": {}, "code": {"prometheus_metrics_port": 9200}}""")));
        Assert.False(ParityRun.BindsFixedPort(Obj("""{"env": {"OTEL_HELPER_METRICS_PORT": "0"}, "code": {}}""")));
    }

    [Fact]
    public void Expand_replaces_placeholders_and_requires_the_receiver()
    {
        (string, string)[] receiver = { ("{receiver_ipv6}", "[::1]"), ("{receiver_tls}", "127.0.0.3"), ("{receiver_ca}", "/ca.pem"), ("{receiver}", "127.0.0.2") };
        Assert.Equal("http://127.0.0.2:4317", ParityHarness.Expand("http://{receiver}:4317", receiver));
        Assert.Equal("https://127.0.0.3", ParityHarness.Expand("https://{receiver_tls}", receiver));
        Assert.ThrowsAny<Exception>(() => ParityHarness.Expand("x {receiver}", null));
    }
}
