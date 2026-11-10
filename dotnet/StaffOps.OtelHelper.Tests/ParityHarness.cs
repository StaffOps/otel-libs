// Parity harness for .NET (design D-NET12): fixture schema and well-formedness
// rules, placeholders, child execution and the matcher. Follows
// nodejs/test/parity/harness.cjs; format and semantics: parity/README.md.
// The tests live in ParityTests.cs.

using System.Diagnostics;
using System.Net;
using System.Reflection;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace StaffOps.OtelHelper.Tests;

/// <summary>Outcome of one case in its child process.</summary>
internal sealed record ParityResult(JsonObject? Snapshot, string? Crash = null, string? Needs = null)
{
    public static ParityResult Crashed(string why) => new(null, Crash: why);
    public static ParityResult NeedsReceiver(string what) => new(null, Needs: what);
}

/// <summary>Result of matching a snapshot against a case.</summary>
internal enum ParityVerdict
{
    Pass,
    ExpectedFailure,
    NeedsReceiver,
    Failed,
}

internal static class ParityHarness
{
    private static readonly string[] Languages = { "go", "python", "dotnet", "nodejs" };

    private static readonly HashSet<string> CommonExpect = new(StringComparer.Ordinal)
    {
        "error", "warning", "warning_count", "log", "diagnostics", "helper_log", "console",
    };

    private static readonly HashSet<string> CaseKeys = new(StringComparer.Ordinal)
    {
        "id", "kind", "requirement", "note", "env", "code", "input", "expect", "status",
    };

    private static readonly string[] RequiredKeys = { "id", "kind", "env", "code", "expect", "status" };

    private static readonly Dictionary<string, string[]> Schema = new(StringComparer.Ordinal)
    {
        ["resolve"] = new[] { "resource", "metric_exporters", "metric_export_interval_ms", "log_level", "prometheus_metrics_port", "prometheus_metrics_host", "http_opt_in_metrics" },
        ["export"] = new[] { "received" },
        ["sampler"] = new[] { "sampled", "attributes" },
        ["propagation"] = new[] { "headers" },
        ["http_server"] = new[] { "server_spans", "requests_counted", "handler_child_spans", "scrape" },
        ["grpc_server"] = new[] { "server_spans", "rpcs_counted", "call_ok", "server_span_status", "server_span_name", "client_span_name" },
        ["logging"] = Array.Empty<string>(),
        ["metrics_scrape"] = new[] { "scrape", "scrape_status" },
        ["metrics_listener"] = new[] { "listener_statuses" },
        ["fork"] = new[] { "instance_ids", "instance_ids_distinct" },
    };

    private static readonly Dictionary<string, string[]> InputSchema = new(StringComparer.Ordinal)
    {
        ["sampler"] = new[] { "scenario" },
        ["http_server"] = new[] { "requests", "traceparent", "child_span" },
        ["grpc_server"] = new[] { "method" },
        ["metrics_listener"] = new[] { "requests" },
    };

    private static readonly HashSet<string> Signals = new(StringComparer.Ordinal) { "traces", "metrics", "logs" };

    private static readonly HashSet<string> RecordFields = new(StringComparer.Ordinal)
    {
        "proto", "tls", "port", "path", "raw_path", "encoding", "headers", "timeout_ms", "absent",
    };

    private static readonly HashSet<string> MatcherKeys = new(StringComparer.Ordinal)
    {
        "prefix", "regex", "absent", "not_contains", "all", "lte", "gte",
    };

    private static readonly HashSet<string> TextOps = new(StringComparer.Ordinal) { "prefix", "regex", "not_contains" };
    private static readonly Regex StatusRe = new("^(pass|xfail:[A-Za-z0-9.-]+|skip:[A-Za-z0-9._-]+)$");
    private static readonly Regex OverrideRe = new(@"^python@sdk>=[\d.]+$");
    private static readonly Regex TaskSkipRe = new(@"^skip:(C\d+[a-z]?)-[A-Za-z0-9._-]+$");

    /// <summary>D-NET15 differences, not waiting for a task (exact statuses).</summary>
    private static readonly HashSet<string> PermanentSkips = new(StringComparer.Ordinal)
    {
        "skip:no-prefork-model",
        "skip:no-detector-variable",
        "skip:no-upstream-rpc-metrics",
        "skip:no-upstream-dotnet-attribute-limit",
        "skip:no-upstream-dotnet-grpc-tls-files",
        "skip:no-upstream-dotnet-http-body-size",
    };

    /// <summary>The one documented family: Prometheus exporter differences (D-NET15 "other metrics.scrape_*").</summary>
    private static readonly Regex PromSkipRe = new(@"^skip:no-upstream-dotnet-prom-[a-z0-9-]+$");

    public const string MissingFixtureMessage = "parity/cases.json not found; mount the repo root or set PARITY_CASES";

    private static readonly Lazy<string?> CasesPathLazy = new(ResolveCasesPath);
    private static readonly Lazy<IReadOnlyList<JsonObject>> AllCasesLazy = new(() =>
        CasesPathLazy.Value is { } path ? LoadCases(path) : Array.Empty<JsonObject>());

    /// <summary>The fixture path, or null when none is found (unit-only members never need it).</summary>
    public static string? CasesPath => CasesPathLazy.Value;

    /// <summary>The fixture cases; empty when the fixture is missing (one test then fails, see ParityTests).</summary>
    public static IReadOnlyList<JsonObject> AllCases => AllCasesLazy.Value;

    private static string? ResolveCasesPath()
    {
        var overridePath = Environment.GetEnvironmentVariable("PARITY_CASES");
        if (!string.IsNullOrEmpty(overridePath))
        {
            return File.Exists(overridePath) ? overridePath : null;
        }

        var directory = Path.GetDirectoryName(typeof(ParityHarness).Assembly.Location);
        while (!string.IsNullOrEmpty(directory))
        {
            var candidate = Path.Combine(directory, "parity", "cases.json");
            if (File.Exists(candidate))
            {
                return candidate;
            }

            directory = Path.GetDirectoryName(directory);
        }

        return null;
    }

    public static string TasksPath =>
        Environment.GetEnvironmentVariable("PARITY_TASKS")
        ?? Path.GetFullPath(Path.Combine(Path.GetDirectoryName(CasesPath ?? throw new InvalidOperationException(MissingFixtureMessage))!,
            "..", "specs", "dotnet-otel-helper", "tasks.md"));

    private static IReadOnlyList<JsonObject> LoadCases(string file)
    {
        var root = JsonNode.Parse(File.ReadAllText(file))!.AsObject();
        return root["cases"]!.AsArray().Select(c => c!.AsObject()).ToList();
    }

    /// <summary>The dotnet status (no per-version override exists for .NET).</summary>
    public static string StatusOf(JsonObject c) => (string?)c["status"]?["dotnet"] ?? "";

    public static string IdOf(JsonObject c) => (string?)c["id"] ?? "<no id>";

    /// <summary>Whether a dotnet skip status is acceptable (open task or D-NET15 difference).</summary>
    public static bool IsPermanentSkip(string status) =>
        PermanentSkips.Contains(status) || PromSkipRe.IsMatch(status);

    public static string? TaskOfSkip(string status)
    {
        var m = TaskSkipRe.Match(status);
        return m.Success ? m.Groups[1].Value : null;
    }

    // ── fixture validation ──────────────────────────────────────────────

    public static List<string> ValidateCase(JsonObject c)
    {
        var errs = RequiredKeys.Where(k => !c.ContainsKey(k)).Order(StringComparer.Ordinal)
            .Select(k => $"missing key \"{k}\"").ToList();
        errs.AddRange(c.Select(kv => kv.Key).Where(k => !CaseKeys.Contains(k)).Order(StringComparer.Ordinal)
            .Select(k => $"unknown top-level key \"{k}\""));

        var kind = (string?)c["kind"] ?? "";
        if (!Schema.TryGetValue(kind, out var allowed))
        {
            errs.Add($"unknown kind \"{kind}\"");
            return errs;
        }

        if (c["expect"] is not JsonObject expect)
        {
            errs.Add("expect must be an object");
            return errs;
        }

        foreach (var (key, _) in expect)
        {
            if (!allowed.Contains(key) && !CommonExpect.Contains(key))
            {
                errs.Add($"expect key \"{key}\" is not in the {kind} snapshot schema");
            }
        }

        ValidateInput(c, kind, errs);

        if (expect["warning_count"] is { } wc && !(wc is JsonObject wo && wo.All(kv => IsNumber(kv.Value))))
        {
            errs.Add("warning_count must map substrings to numbers");
        }

        if (expect.ContainsKey("warning") && !IsWarningShape(expect["warning"]))
        {
            errs.Add("warning must be a string or list of strings");
        }

        if (expect.ContainsKey("received"))
        {
            ValidateReceived(expect["received"], errs);
        }

        errs.AddRange(CheckMatchers("expect", expect));
        ValidateStatus(c, errs);
        WalkRegexes(expect, re =>
        {
            try
            {
                _ = new Regex(re);
            }
            catch (ArgumentException ex)
            {
                errs.Add($"regex \"{re}\" does not compile: {ex.Message}");
            }
        });
        return errs;
    }

    private static void ValidateInput(JsonObject c, string kind, List<string> errs)
    {
        var input = c["input"] as JsonObject ?? new JsonObject();
        foreach (var (key, val) in input)
        {
            if (!InputSchema.TryGetValue(kind, out var fields) || !fields.Contains(key))
            {
                errs.Add($"input key \"{key}\" is not valid for kind {kind}");
            }
            else if (key == "requests" && !(val is JsonArray a && a.All(x => x is JsonValue v && v.TryGetValue<string>(out _))))
            {
                errs.Add("input.requests must be a list of strings");
            }
            else if (key is "method" or "scenario" or "traceparent" && !(val is JsonValue s && s.TryGetValue<string>(out _)))
            {
                errs.Add($"input.{key} must be a string");
            }
            else if (key == "child_span" && !(val is JsonValue b && b.TryGetValue<bool>(out _)))
            {
                errs.Add("input.child_span must be a boolean");
            }
        }

        if (kind == "metrics_listener")
        {
            var requests = input["requests"] as JsonArray;
            var ok = requests is { Count: > 0 } && requests.All(x =>
                x is JsonValue v && v.TryGetValue<string>(out var t) && Regex.IsMatch(t, @"^[A-Z]+ /\S*$"));
            if (!ok)
            {
                errs.Add("metrics_listener needs a non-empty input.requests of \"METHOD /path\" strings");
            }
        }
    }

    private static bool IsWarningShape(JsonNode? warning) => warning switch
    {
        JsonValue v => v.TryGetValue<string>(out _),
        JsonArray a => a.All(x => x is JsonValue v2 && v2.TryGetValue<string>(out _)),
        _ => false,
    };

    private static void ValidateReceived(JsonNode? received, List<string> errs)
    {
        if (received is not JsonObject recv)
        {
            errs.Add("expect.received must be an object");
            return;
        }

        foreach (var (signal, want) in recv)
        {
            if (!Signals.Contains(signal))
            {
                errs.Add($"received signal \"{signal}\" unknown");
            }

            if (want is not JsonObject wantObj)
            {
                errs.Add($"received.{signal} must be an object");
                continue;
            }

            foreach (var (field, _) in wantObj)
            {
                if (!RecordFields.Contains(field))
                {
                    errs.Add($"received.{signal} field \"{field}\" unknown");
                }
            }
        }
    }

    private static void ValidateStatus(JsonObject c, List<string> errs)
    {
        var status = c["status"] as JsonObject ?? new JsonObject();
        foreach (var lang in Languages)
        {
            if (!StatusRe.IsMatch((string?)status[lang] ?? ""))
            {
                errs.Add($"status.{lang} invalid");
            }
        }

        foreach (var (key, val) in status)
        {
            if (!Languages.Contains(key)
                && !(OverrideRe.IsMatch(key) && val is JsonValue v && v.TryGetValue<string>(out var text) && StatusRe.IsMatch(text)))
            {
                errs.Add($"status override \"{key}\": {val} invalid");
            }
        }
    }

    /// <summary>Objects that contain a matcher op may contain only matcher ops.</summary>
    private static List<string> CheckMatchers(string path, JsonNode? value)
    {
        var errs = new List<string>();
        switch (value)
        {
            case JsonObject o when o.Any(kv => MatcherKeys.Contains(kv.Key)):
                foreach (var (key, _) in o)
                {
                    if (!MatcherKeys.Contains(key))
                    {
                        errs.Add($"{path}: \"{key}\" is not a matcher op");
                    }
                }

                if (o.ContainsKey("all"))
                {
                    if (o["all"] is not JsonArray all)
                    {
                        errs.Add($"{path}.all must be a list");
                    }
                    else
                    {
                        for (var i = 0; i < all.Count; i++)
                        {
                            errs.AddRange(CheckMatchers($"{path}.all[{i}]", all[i]));
                        }
                    }
                }

                foreach (var op in new[] { "lte", "gte" })
                {
                    if (o.ContainsKey(op) && !IsNumber(o[op]))
                    {
                        errs.Add($"{path}.{op} must be a number");
                    }
                }

                break;
            case JsonObject o:
                foreach (var (key, x) in o)
                {
                    errs.AddRange(CheckMatchers($"{path}.{key}", x));
                }

                break;
            case JsonArray a:
                for (var i = 0; i < a.Count; i++)
                {
                    errs.AddRange(CheckMatchers($"{path}[{i}]", a[i]));
                }

                break;
        }

        return errs;
    }

    private static void WalkRegexes(JsonNode? value, Action<string> onRegex)
    {
        switch (value)
        {
            case JsonObject o:
                foreach (var (key, x) in o)
                {
                    if (key == "regex" && x is JsonValue v && v.TryGetValue<string>(out var re))
                    {
                        onRegex(re);
                    }
                    else
                    {
                        WalkRegexes(x, onRegex);
                    }
                }

                break;
            case JsonArray a:
                foreach (var x in a)
                {
                    WalkRegexes(x, onRegex);
                }

                break;
        }
    }

    private static bool IsNumber(JsonNode? node) =>
        node is JsonValue v && v.GetValueKind() == JsonValueKind.Number;

    private static bool IsString(JsonNode? node) =>
        node is JsonValue v && v.GetValueKind() == JsonValueKind.String;

    private static string Repr(JsonNode? value, bool present) =>
        !present ? "<absent>" : value?.ToJsonString() ?? "null";

    private static double AsDouble(JsonNode? node) => node!.GetValue<double>();

    private static bool DeepEquals(JsonNode? a, JsonNode? b)
    {
        if (a is null || b is null)
        {
            return a is null && b is null;
        }

        if (IsNumber(a) && IsNumber(b))
        {
            return AsDouble(a).Equals(AsDouble(b));
        }

        // Numbers compare as floats at any depth (1 equals 1.0), so no raw-text DeepEquals.
        return (a, b) switch
        {
            (JsonObject oa, JsonObject ob) => oa.Count == ob.Count
                && oa.All(kv => ob.ContainsKey(kv.Key) && DeepEquals(kv.Value, ob[kv.Key])),
            (JsonArray xa, JsonArray xb) => xa.Count == xb.Count && xa.Zip(xb).All(p => DeepEquals(p.First, p.Second)),
            (JsonValue va, JsonValue vb) => va.GetValueKind() == vb.GetValueKind() && JsonNode.DeepEquals(va, vb),
            _ => false,
        };
    }

    // ── matching (mirrors harness.cjs) ──────────────────────────────────

    public static List<string> MatchValue(string path, JsonNode? want, JsonNode? got, bool present)
    {
        if (want is not JsonObject wantObj)
        {
            if (!present)
            {
                return new List<string> { $"{path}: want {Repr(want, true)}, got <absent>" };
            }

            return DeepEquals(want, got) ? new List<string>() : new List<string> { $"{path}: want {Repr(want, true)}, got {Repr(got, true)}" };
        }

        if (!wantObj.Any(kv => MatcherKeys.Contains(kv.Key)))
        {
            var gotObj = got as JsonObject ?? new JsonObject();
            return wantObj.OrderBy(kv => kv.Key, StringComparer.Ordinal)
                .SelectMany(kv => MatchValue($"{path}.{kv.Key}", kv.Value, gotObj[kv.Key], gotObj.ContainsKey(kv.Key))).ToList();
        }

        var mismatches = new List<string>();
        var text = IsString(got) ? got!.GetValue<string>() : "";
        foreach (var (op, arg) in wantObj.OrderBy(kv => kv.Key, StringComparer.Ordinal))
        {
            if (TextOps.Contains(op) && present && !IsString(got))
            {
                mismatches.Add($"{path}: {op} applies to strings, got {Repr(got, present)}");
            }
            else if (op == "absent")
            {
                var wantAbsent = arg!.GetValue<bool>();
                if (wantAbsent && present)
                {
                    mismatches.Add($"{path}: want absent, got {Repr(got, present)}");
                }

                if (!wantAbsent && !present)
                {
                    mismatches.Add($"{path}: want present");
                }
            }
            else if (op == "prefix")
            {
                var prefix = arg!.GetValue<string>();
                if (!present || !text.StartsWith(prefix, StringComparison.Ordinal))
                {
                    mismatches.Add($"{path}: want prefix {Repr(arg, true)}, got {Repr(got, present)}");
                }
            }
            else if (op == "regex")
            {
                var pattern = arg!.GetValue<string>();
                Regex regex;
                try
                {
                    regex = new Regex(pattern);
                }
                catch (ArgumentException ex)
                {
                    mismatches.Add($"{path}: bad regex {Repr(arg, true)}: {ex.Message}");
                    continue;
                }

                if (!present || !regex.IsMatch(text))
                {
                    mismatches.Add($"{path}: want /{pattern}/, got {Truncate(text, 300)}");
                }
            }
            else if (op == "not_contains")
            {
                var needle = arg!.GetValue<string>();
                if (present && text.Contains(needle, StringComparison.Ordinal))
                {
                    mismatches.Add($"{path}: must not contain {Repr(arg, true)}");
                }
            }
            else if (op is "lte" or "gte")
            {
                var bound = AsDouble(arg);
                if (!present || !IsNumber(got) || (op == "lte" && AsDouble(got) > bound) || (op == "gte" && AsDouble(got) < bound))
                {
                    mismatches.Add($"{path}: want {op} {bound}, got {Repr(got, present)}");
                }
            }
            else if (op == "all")
            {
                foreach (var sub in arg!.AsArray())
                {
                    mismatches.AddRange(MatchValue(path, sub, got, present));
                }
            }
        }

        return mismatches;
    }

    private static string Truncate(string text, int length) => text.Length <= length ? text : text[..length];

    private static List<string> MatchReceived(JsonObject want, JsonNode? received)
    {
        var recv = received as JsonObject ?? new JsonObject();
        var mismatches = new List<string>();
        foreach (var (signal, wantNode) in want.OrderBy(kv => kv.Key, StringComparer.Ordinal))
        {
            var w = wantNode!.AsObject();
            var records = recv[signal] as JsonArray ?? new JsonArray();
            if (w.ContainsKey("absent") && w["absent"]!.GetValue<bool>())
            {
                if (records.Count > 0)
                {
                    mismatches.Add($"received.{signal}: want none, got {records.ToJsonString()}");
                }

                continue;
            }

            if (records.Count == 0)
            {
                mismatches.Add($"received.{signal}: nothing received (all: {recv.ToJsonString()})");
                continue;
            }

            for (var i = 0; i < records.Count; i++)
            {
                var rec = records[i]!.AsObject();
                foreach (var (field, wantField) in w.OrderBy(kv => kv.Key, StringComparer.Ordinal))
                {
                    mismatches.AddRange(MatchValue($"received.{signal}[{i}].{field}", wantField, rec[field], rec.ContainsKey(field)));
                }
            }
        }

        return mismatches;
    }

    /// <summary>Behavior mismatches of a snapshot against a case's expect.</summary>
    public static List<string> MatchExpect(JsonObject expect, JsonObject snap)
    {
        var mismatches = new List<string>();
        if (!expect.ContainsKey("error") && snap.ContainsKey("error"))
        {
            mismatches.Add($"unexpected error: {(string?)snap["error"]}");
        }

        foreach (var (key, want) in expect.OrderBy(kv => kv.Key, StringComparer.Ordinal))
        {
            switch (key)
            {
                case "error":
                    if (want is JsonValue errorValue && errorValue.TryGetValue<string>(out var substring))
                    {
                        var error = (string?)snap["error"];
                        if (!snap.ContainsKey("error") || error is null || !error.Contains(substring, StringComparison.Ordinal))
                        {
                            mismatches.Add($"error: want substring \"{substring}\", got {Repr(snap["error"], snap.ContainsKey("error"))}");
                        }
                    }
                    else
                    {
                        mismatches.AddRange(MatchValue("error", want, snap["error"], snap.ContainsKey("error")));
                    }

                    break;
                case "warning":
                    var log = (string?)snap["log"] ?? "";
                    foreach (var sub in WarningSubstrings(want))
                    {
                        if (!log.Contains(sub, StringComparison.Ordinal))
                        {
                            mismatches.Add($"warning: no diagnostic contains \"{sub}\" (log: {Truncate(log, 300)})");
                        }
                    }

                    break;
                case "warning_count":
                    var lines = ((string?)snap["diagnostics"] ?? "").Split('\n');
                    foreach (var (sub, n) in want!.AsObject().OrderBy(kv => kv.Key, StringComparer.Ordinal))
                    {
                        var count = lines.Count(line => line.Contains(sub, StringComparison.Ordinal));
                        if (count != AsDouble(n))
                        {
                            mismatches.Add($"warning_count[\"{sub}\"]: want {AsDouble(n)}, got {count}");
                        }
                    }

                    break;
                case "received":
                    mismatches.AddRange(MatchReceived(want!.AsObject(), snap["received"]));
                    break;
                default:
                    mismatches.AddRange(MatchValue(key, want, snap[key], snap.ContainsKey(key)));
                    break;
            }
        }

        return mismatches;
    }

    private static IEnumerable<string> WarningSubstrings(JsonNode? want) =>
        want is JsonArray a ? a.Select(x => x!.GetValue<string>()) : new[] { want!.GetValue<string>() };

    /// <summary>Verdict of one case under its status (strict xfail, the Node rules).</summary>
    public static (ParityVerdict Verdict, string Reason) Evaluate(JsonObject c, ParityResult result) =>
        Evaluate(c, result, !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("PARITY_REQUIRE_FAKEOTLP")));

    public static (ParityVerdict Verdict, string Reason) Evaluate(JsonObject c, ParityResult result, bool requireReceiver)
    {
        var errs = ValidateCase(c);
        if (errs.Count > 0)
        {
            return (ParityVerdict.Failed, $"fixture error (fails regardless of status): {string.Join("; ", errs)}");
        }

        if (result.Crash is not null)
        {
            return (ParityVerdict.Failed, result.Crash);
        }

        if (result.Needs is not null)
        {
            if (requireReceiver)
            {
                return (ParityVerdict.Failed, $"PARITY_REQUIRE_FAKEOTLP is set but {result.Needs} is not: export cases must run");
            }

            return (ParityVerdict.NeedsReceiver, $"case needs the fake OTLP receiver ({result.Needs} unset; see testing/README.md)");
        }

        var snap = result.Snapshot!;
        if (snap.ContainsKey("adapter_error"))
        {
            return (ParityVerdict.Failed, $"adapter error (fails regardless of status): {(string?)snap["adapter_error"]}");
        }

        if (snap.ContainsKey("driver_crash"))
        {
            return (ParityVerdict.Failed, $"driver crashed: {(string?)snap["driver_crash"]}");
        }

        var mismatches = MatchExpect(c["expect"]!.AsObject(), snap);
        var status = StatusOf(c);
        if (status.StartsWith("xfail:", StringComparison.Ordinal))
        {
            if (mismatches.Count == 0)
            {
                return (ParityVerdict.Failed, $"unexpected pass: {IdOf(c)} is {status}; flip it to pass");
            }

            return (ParityVerdict.ExpectedFailure, string.Join("; ", mismatches));
        }

        if (mismatches.Count > 0)
        {
            return (ParityVerdict.Failed, $"parity case {IdOf(c)} failed:\n  {string.Join("\n  ", mismatches)}");
        }

        return (ParityVerdict.Pass, "");
    }

    // ── receiver and environment ────────────────────────────────────────

    private static readonly Dictionary<string, string> PlaceholderEnv = new(StringComparer.Ordinal)
    {
        ["{receiver_ipv6}"] = "FAKEOTLP_HOST_IPV6",
        ["{receiver_tls}"] = "FAKEOTLP_HOST_TLS",
    };

    /// <summary>Placeholder values, specific placeholders first; null without a receiver.</summary>
    public static (string Placeholder, string Value)[]? Receiver()
    {
        var host = Environment.GetEnvironmentVariable("FAKEOTLP_HOST");
        if (string.IsNullOrEmpty(host))
        {
            return null;
        }

        return new[]
        {
            ("{receiver_ipv6}", Environment.GetEnvironmentVariable("FAKEOTLP_HOST_IPV6") ?? ""),
            ("{receiver_tls}", Environment.GetEnvironmentVariable("FAKEOTLP_HOST_TLS") ?? ""),
            ("{receiver_ca}", Environment.GetEnvironmentVariable("FAKEOTLP_CA") ?? ""),
            ("{receiver}", host),
        };
    }

    private sealed class NeedsReceiverException : Exception
    {
        public NeedsReceiverException(string what) : base(what)
        {
        }
    }

    public static string Expand(string value, (string Placeholder, string Value)[]? receiver)
    {
        if (!value.Contains("{receiver", StringComparison.Ordinal))
        {
            return value;
        }

        if (receiver is null)
        {
            throw new NeedsReceiverException("FAKEOTLP_HOST");
        }

        foreach (var (placeholder, variable) in PlaceholderEnv)
        {
            if (value.Contains(placeholder, StringComparison.Ordinal)
                && receiver.First(r => r.Placeholder == placeholder).Value.Length == 0)
            {
                throw new NeedsReceiverException(variable);
            }
        }

        foreach (var (placeholder, real) in receiver)
        {
            value = value.Replace(placeholder, real, StringComparison.Ordinal);
        }

        return value;
    }

    private static JsonNode? ExpandNode(JsonNode? value, (string Placeholder, string Value)[]? receiver) => value switch
    {
        JsonValue v when v.TryGetValue<string>(out var text) => JsonValue.Create(Expand(text, receiver)),
        JsonArray a => new JsonArray(a.Select(x => ExpandNode(x, receiver)).ToArray()),
        JsonObject o => ExpandObject(o, receiver),
        null => null,
        _ => value.DeepClone(),
    };

    private static JsonObject ExpandObject(JsonObject o, (string Placeholder, string Value)[]? receiver)
    {
        var copy = new JsonObject();
        foreach (var (key, x) in o)
        {
            copy[key] = ExpandNode(x, receiver);
        }

        return copy;
    }

    private static async Task<string> DestIpsAsync((string Placeholder, string Value)[] receiver)
    {
        var ips = new SortedSet<string>(StringComparer.Ordinal);
        foreach (var key in new[] { "{receiver}", "{receiver_tls}", "{receiver_ipv6}" })
        {
            var host = receiver.First(r => r.Placeholder == key).Value.Trim('[', ']');
            if (host.Length == 0)
            {
                continue;
            }

            foreach (var address in await Dns.GetHostAddressesAsync(host))
            {
                ips.Add(address.ToString());
            }
        }

        return string.Join(",", ips);
    }

    // ── child execution ─────────────────────────────────────────────────

    /// <summary>Child kill timeout; the driver gives export cases 75 s to shut down.</summary>
    private const int ChildTimeoutMs = 120_000;

    private static string DriverPath =>
        Path.Combine(Path.GetDirectoryName(typeof(ParityHarness).Assembly.Location)!, "StaffOps.OtelHelper.ParityDriver.dll");

    /// <summary>Runs one case in a child process; never throws for case outcomes.</summary>
    public static async Task<ParityResult> RunCaseAsync(JsonObject c)
    {
        var tmp = Directory.CreateTempSubdirectory("parity-");
        try
        {
            var receiver = Receiver();
            JsonObject prepared;
            Dictionary<string, string> environment;
            try
            {
                environment = await ChildEnvironmentAsync(c, receiver);
                prepared = c.DeepClone().AsObject();
                prepared["code"] = ExpandObject(c["code"]!.AsObject(), receiver);
            }
            catch (NeedsReceiverException ex)
            {
                return ParityResult.NeedsReceiver(ex.Message);
            }

            var caseFile = Path.Combine(tmp.FullName, "case.json");
            await File.WriteAllTextAsync(caseFile, prepared.ToJsonString());
            return await SpawnDriverAsync(caseFile, environment);
        }
        catch (Exception ex)
        {
            return ParityResult.Crashed($"harness error: {ex}");
        }
        finally
        {
            tmp.Delete(recursive: true);
        }
    }

    /// <summary>
    /// Variables the reset removes from the parent environment: OTEL_*, SERVICE_NAME and
    /// ENVIRONMENT, also with the DOTNET_ / ASPNETCORE_ prefixes the .NET host maps into
    /// IConfiguration (OTel .NET reads settings from IConfiguration), and Logging__*.
    /// </summary>
    private static readonly Regex ResetVariableRe = new(
        "^((DOTNET_|ASPNETCORE_)?(OTEL_|SERVICE_NAME$|ENVIRONMENT$)|LOGGING__)", RegexOptions.IgnoreCase);

    /// <summary>The child environment: the parent's minus the reset set, the port default, then the case env.</summary>
    public static Dictionary<string, string> BaseChildEnvironment(IEnumerable<KeyValuePair<string, string>> parent,
        JsonObject caseEnv, (string Placeholder, string Value)[]? receiver)
    {
        var environment = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var (key, value) in parent)
        {
            if (!ResetVariableRe.IsMatch(key))
            {
                environment[key] = value;
            }
        }

        // Never bind :9464 in tests; cases may override.
        environment["OTEL_HELPER_METRICS_PORT"] = "0";
        foreach (var (key, value) in caseEnv)
        {
            environment[key] = Expand(value!.GetValue<string>(), receiver);
        }

        return environment;
    }

    private static async Task<Dictionary<string, string>> ChildEnvironmentAsync(JsonObject c, (string Placeholder, string Value)[]? receiver)
    {
        var parent = Environment.GetEnvironmentVariables().Cast<System.Collections.DictionaryEntry>()
            .Select(e => new KeyValuePair<string, string>((string)e.Key, (string?)e.Value ?? ""));
        var environment = BaseChildEnvironment(parent, c["env"]!.AsObject(), receiver);

        if ((string?)c["kind"] == "export")
        {
            if (receiver is null)
            {
                throw new NeedsReceiverException("FAKEOTLP_HOST");
            }

            environment["FAKEOTLP_DEST_IPS"] = await DestIpsAsync(receiver);
        }

        return environment;
    }

    private static Task<ParityResult> SpawnDriverAsync(string caseFile, Dictionary<string, string> environment)
    {
        var dotnetHost = Environment.GetEnvironmentVariable("DOTNET_HOST_PATH");
        return SpawnAsync(string.IsNullOrEmpty(dotnetHost) ? "dotnet" : dotnetHost, new[] { DriverPath, caseFile }, environment, ChildTimeoutMs);
    }

    /// <summary>Runs a child and turns its output into a result (injectable for the harness tests).</summary>
    internal static async Task<ParityResult> SpawnAsync(string file, IEnumerable<string> arguments,
        IReadOnlyDictionary<string, string> environment, int timeoutMs)
    {
        var startInfo = new ProcessStartInfo(file)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
        };
        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        startInfo.Environment.Clear();
        foreach (var (key, value) in environment)
        {
            startInfo.Environment[key] = value;
        }

        using var process = Process.Start(startInfo) ?? throw new InvalidOperationException("driver failed to start");
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        using var timeout = new CancellationTokenSource(timeoutMs);
        try
        {
            await process.WaitForExitAsync(timeout.Token);
        }
        catch (OperationCanceledException)
        {
            process.Kill(entireProcessTree: true);
            await process.WaitForExitAsync();
            return ParityResult.Crashed($"driver killed after {timeoutMs} ms:\n{Tail(await stderr)}");
        }

        var output = await stdout;
        var errorOutput = await stderr;
        if (process.ExitCode != 0)
        {
            // The driver always exits 0 (coverage); any other code is a crash, even with a parsable stdout.
            return ParityResult.Crashed($"driver exited with rc={process.ExitCode}; stderr:\n{Tail(errorOutput)}");
        }

        JsonObject snap;
        try
        {
            snap = JsonNode.Parse(output)!.AsObject();
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException)
        {
            return ParityResult.Crashed($"driver stdout is not one JSON snapshot ({ex.Message}); rc={process.ExitCode}; stdout: {Tail(output)}; stderr:\n{Tail(errorOutput)}");
        }

        var diagnostics = (string?)snap["diagnostics"] ?? "";
        snap["console"] = errorOutput;
        snap["log"] = string.Join("\n", new[] { diagnostics, errorOutput }.Where(s => s.Length > 0));
        return new ParityResult(snap);
    }

    private static string Tail(string text) => text.Length <= 3000 ? text : text[^3000..];
}

/// <summary>Runs the cases once for the whole class (non-export first; export cases one by one).</summary>
internal static class ParityRun
{
    private static readonly Lazy<IReadOnlyDictionary<string, ParityResult>> AllResults = new(RunAll, LazyThreadSafetyMode.ExecutionAndPublication);

    private static readonly string? Only = Environment.GetEnvironmentVariable("PARITY_ONLY");

    public static IReadOnlyList<JsonObject> SelectedCases => ParityHarness.AllCases
        .Where(c => string.IsNullOrEmpty(Only) || Only.Split(',', StringSplitOptions.TrimEntries).Contains(ParityHarness.IdOf(c)))
        .ToList();

    public static IReadOnlyList<JsonObject> RunnableCases => SelectedCases
        .Where(c => !ParityHarness.StatusOf(c).StartsWith("skip:", StringComparison.Ordinal))
        .ToList();

    public static ParityResult Result(string id) => AllResults.Value[id];

    /// <summary>Whether a case binds a fixed listener port (never run those concurrently).</summary>
    internal static bool BindsFixedPort(JsonObject c)
    {
        var env = c["env"] as JsonObject ?? new JsonObject();
        var code = c["code"] as JsonObject ?? new JsonObject();
        var helperPort = env.TryGetPropertyValue("OTEL_HELPER_METRICS_PORT", out var h) ? (string?)h : null;
        var standardPort = env.TryGetPropertyValue("OTEL_EXPORTER_PROMETHEUS_PORT", out var p) ? (string?)p : null;
        var codePort = code.TryGetPropertyValue("prometheus_metrics_port", out var q) && q is JsonValue v && v.TryGetValue<int>(out var n) ? n : 0;
        return (!string.IsNullOrEmpty(helperPort) && helperPort != "0") || !string.IsNullOrEmpty(standardPort) || codePort > 0;
    }

    private static IReadOnlyDictionary<string, ParityResult> RunAll()
    {
        var results = new System.Collections.Concurrent.ConcurrentDictionary<string, ParityResult>(StringComparer.Ordinal);
        var cases = RunnableCases;
        var parallel = cases.Where(c => (string?)c["kind"] != "export" && !BindsFixedPort(c)).ToList();
        var serial = cases.Except(parallel).OrderBy(c => (string?)c["kind"] == "export" ? 1 : 0).ToList();

        var options = new ParallelOptions { MaxDegreeOfParallelism = Math.Max(1, Math.Min(8, Environment.ProcessorCount * 2)) };
        Parallel.ForEachAsync(parallel, options, async (c, _) =>
        {
            results[ParityHarness.IdOf(c)] = await ParityHarness.RunCaseAsync(c);
        }).GetAwaiter().GetResult();

        foreach (var c in serial)
        {
            // Off the test thread's synchronization context.
            results[ParityHarness.IdOf(c)] = Task.Run(() => ParityHarness.RunCaseAsync(c)).GetAwaiter().GetResult();
        }

        var dictionary = results.ToDictionary(kv => kv.Key, kv => kv.Value, StringComparer.Ordinal);
        WriteResultsFile(dictionary);
        return dictionary;
    }

    /// <summary>PARITY_RESULTS=path writes every outcome as JSON, for recording statuses.</summary>
    private static void WriteResultsFile(IReadOnlyDictionary<string, ParityResult> results)
    {
        var path = Environment.GetEnvironmentVariable("PARITY_RESULTS");
        if (string.IsNullOrEmpty(path))
        {
            return;
        }

        var outcomes = new JsonObject();
        foreach (var c in ParityHarness.AllCases)
        {
            var id = ParityHarness.IdOf(c);
            if (!results.TryGetValue(id, out var result))
            {
                continue;
            }

            var (verdict, reason) = ParityHarness.Evaluate(c, result);
            var outcome = new JsonObject { ["verdict"] = verdict.ToString(), ["reason"] = reason };
            outcome["behavior_matches"] = result.Snapshot is not null && ParityHarness.MatchExpect(c["expect"]!.AsObject(), result.Snapshot).Count == 0;
            outcome["snapshot"] = result.Snapshot?.DeepClone();
            outcomes[id] = outcome;
        }

        File.WriteAllText(path, outcomes.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
    }
}
