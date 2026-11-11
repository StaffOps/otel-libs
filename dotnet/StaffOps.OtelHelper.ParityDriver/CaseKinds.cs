using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Grpc.Core;
using Grpc.Net.Client;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Server.Kestrel.Core;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using OpenTelemetry;
using OpenTelemetry.Context.Propagation;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper.ParityDriver;

/// <summary>The fixture kinds (parity/README.md, "Kinds"), one async method each.</summary>
internal static class CaseKinds
{
    private const int ShutdownDeadlineMs = 15_000;
    private const int ExportShutdownDeadlineMs = 75_000;
    private const string TraceId = "0af7651916cd43dd8448eb211c80319c";
    private const string SpanId = "b7ad6b7169203331";
    private const string ParityScope = "parity";
    private static readonly IReadOnlyDictionary<string, string> NoHeaders = new Dictionary<string, string>();

    public static async Task<JsonObject> RunAsync(JsonObject c)
    {
        var kind = (string?)c["kind"] ?? "";
        try
        {
            return kind switch
            {
                "resolve" => await ResolveAsync(c),
                "export" => await ExportAsync(c),
                "sampler" => await SamplerAsync(c),
                "propagation" => await PropagationAsync(c),
                "http_server" => await HttpServerAsync(c),
                "grpc_server" => await GrpcServerAsync(c),
                "logging" => await LoggingAsync(c),
                "metrics_scrape" => await MetricsScrapeAsync(c),
                "metrics_listener" => await MetricsListenerAsync(c),
                "fork" => new JsonObject { ["adapter_error"] = "fork is skipped on .NET (no prefork model)" },
                _ => new JsonObject { ["adapter_error"] = $"unknown kind \"{kind}\"" },
            };
        }
        catch (AdapterException ex)
        {
            return new JsonObject { ["adapter_error"] = ex.Message };
        }
    }

    // ── setup ───────────────────────────────────────────────────────────

    /// <summary>A host with the helper set up; disposed at the end of the case.</summary>
    private sealed class Run
    {
        public required IHost Host { get; init; }
        public required ResolvedTelemetry Resolved { get; init; }
        public required SpanCapture Spans { get; init; }
    }

    /// <summary>Records every finished span for the kinds that assert them.</summary>
    private sealed class SpanCapture : BaseProcessor<Activity>
    {
        private readonly List<Activity> _ended = new();

        public override void OnEnd(Activity data)
        {
            lock (_ended)
            {
                _ended.Add(data);
            }
        }

        public IReadOnlyList<Activity> Ended()
        {
            lock (_ended)
            {
                return _ended.ToArray();
            }
        }
    }

    private enum HostModel
    {
        Generic,
        Http1,
        Http2Grpc,
    }

    /// <summary>Builds a host with AddOtelHelper(mapped code options); a setup failure lands in `error`.</summary>
    private static async Task<Run?> SetupAsync(JsonObject c, JsonObject snap, HostModel model = HostModel.Generic,
        Action<WebApplication>? configureApp = null)
    {
        var map = CodeMapper.Map(c["code"]?.AsObject());
        var capture = new SpanCapture();
        IHost? host = null;
        try
        {
            IHostApplicationBuilder builder;
            WebApplicationBuilder? web = null;
            if (model == HostModel.Generic)
            {
                builder = Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder();
            }
            else
            {
                web = WebApplication.CreateBuilder();
                builder = web;
                web.WebHost.ConfigureKestrel(k => k.Listen(IPAddress.Loopback, 0,
                    o => o.Protocols = model == HostModel.Http2Grpc ? HttpProtocols.Http2 : HttpProtocols.Http1));
            }

            // Only what the helper installs: no default console or debug providers.
            builder.Logging.ClearProviders();

            // The helper's diagnostics sink, with the driver's capture (design D-NET5):
            // registered first, so AddOtelHelper keeps it (TryAdd) and also uses it for
            // registration-time records.
            builder.Services.AddSingleton<IOtelHelperDiagnostics>(DriverDiagnostics.Sink);
            builder.Services.AddOtelHelper(options =>
            {
                map(options);
                // The parity scope: the helper API for an app source and meter of another name.
                options.AdditionalActivitySources.Add(ParityScope);
                options.AdditionalMeters.Add(ParityScope);
            });
            builder.Services.ConfigureOpenTelemetryTracerProvider(b => b.AddProcessor(capture));

            if (web is not null && model == HostModel.Http2Grpc)
            {
                web.Services.AddGrpc();
                web.Services.AddSingleton<Grpc.AspNetCore.Server.Model.IServiceMethodProvider<GrpcHealthService>, GrpcHealthMethods>();
                web.Services.AddSingleton<Grpc.AspNetCore.Server.Model.IServiceMethodProvider<GrpcEchoService>, GrpcEchoMethods>();
            }

            if (web is not null)
            {
                var app = web.Build();
                configureApp?.Invoke(app);
                host = app;
            }
            else
            {
                host = ((HostApplicationBuilder)builder).Build();
            }

            await host.StartAsync();
            var resolved = host.Services.GetRequiredService<ResolvedTelemetry>();
            return new Run { Host = host, Resolved = resolved, Spans = capture };
        }
        catch (Exception ex) when (ex is not AdapterException)
        {
            snap["error"] = DescribeSetupFailure(ex);
            host?.Dispose();
            return null;
        }
    }

    private static string DescribeSetupFailure(Exception ex)
    {
        while (ex is AggregateException { InnerException: { } inner })
        {
            ex = inner;
        }

        var text = $"{ex.GetType().Name}: {ex.Message}";
        // Inner causes too: an adapter bug must be visible, not read as helper behavior.
        for (var inner = ex.InnerException; inner is not null; inner = inner.InnerException)
        {
            text += $" <- {inner.GetType().Name}: {inner.Message}";
        }

        return text;
    }

    /// <summary>Shuts the host down under one deadline; a straggler is abandoned.</summary>
    private static async Task ShutdownAsync(Run? run, int deadlineMs = ShutdownDeadlineMs)
    {
        if (run is null)
        {
            return;
        }

        var shutdown = Task.Run(async () =>
        {
            try
            {
                await run.Host.StopAsync();
            }
            finally
            {
                run.Host.Dispose();
            }
        });
        await Task.WhenAny(shutdown, Task.Delay(deadlineMs));
    }

    // ── resolve ─────────────────────────────────────────────────────────

    private static async Task<JsonObject> ResolveAsync(JsonObject c)
    {
        var snap = new JsonObject();
        var run = await SetupAsync(c, snap);
        if (run is not null)
        {
            snap["resource"] = ResourceOf(run.Host.Services.GetRequiredService<TracerProvider>());
            var r = run.Resolved;
            // Empty when metrics are disabled or OTEL_SDK_DISABLED is true (read where the SDK reads it).
            snap["metric_exporters"] = ToJsonArray(r.ResolvedMetricExporters());
            // The resolved interval is what the helper hands the periodic reader (MetricsSetup);
            // the SDK has no public getter for a reader's interval, so the resolved value is read.
            snap["metric_export_interval_ms"] = r.ExportIntervalMs;
            // The level the helper installed. Since C13 it is a rule on the helper's own providers
            // (N9), not LoggerFilterOptions.MinLevel: the lowest level a logger enables, read through
            // the factory (the driver cleared the host's providers, so only the helper's remain).
            snap["log_level"] = LogLevelName(InstalledLogLevel(run.Host.Services.GetRequiredService<ILoggerFactory>()));
            snap["prometheus_metrics_port"] = r.PrometheusMetricsPort;
            // The filter the built MeterProvider applies (SDK-internal member, read by reflection),
            // not the resolved value: the helper sets it in a deferred step (task C9).
            if (InstalledExemplarFilter(run.Host.Services.GetRequiredService<OpenTelemetry.Metrics.MeterProvider>()) is { } exemplarFilter)
            {
                snap["exemplar_filter"] = exemplarFilter;
            }
            snap["http_opt_in_metrics"] = r.HttpOptInMetrics;
            // The resolved bind address of the standalone listener (A29, task C12): "" = every
            // interface, which .NET binds as every IPv4 interface (the "+" prefix).
            snap["prometheus_metrics_host"] = r.PrometheusMetricsHost;
        }

        await ShutdownAsync(run);
        return snap;
    }

    /// <summary>
    /// The exemplar filter of the built provider, from the SDK's internal member of type
    /// <c>ExemplarFilterType?</c> (<c>MeterProviderSdk</c>, 1.20.x); null under a no-op provider.
    /// A missing member is an adapter error (an SDK bump renamed it).
    /// </summary>
    private static string? InstalledExemplarFilter(OpenTelemetry.Metrics.MeterProvider provider)
    {
        const System.Reflection.BindingFlags flags = System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic;
        var type = provider.GetType();
        if (type.Name != "MeterProviderSdk")
        {
            return null;
        }

        // The general filter only: the SDK also keeps a histogram-specific one (null unless set).
        // Exactly one member must match, else the driver cannot tell which one applies.
        static bool IsGeneral(string name) => name is "ExemplarFilter" or "<ExemplarFilter>k__BackingField";
        var matches = type.GetFields(flags)
            .Where(f => f.FieldType == typeof(OpenTelemetry.Metrics.ExemplarFilterType?) && IsGeneral(f.Name))
            .Select(f => (Name: f.Name, Read: (Func<object?>)(() => f.GetValue(provider))))
            .Concat(type.GetProperties(flags)
                .Where(p => p.PropertyType == typeof(OpenTelemetry.Metrics.ExemplarFilterType?) && p.GetIndexParameters().Length == 0 && IsGeneral(p.Name))
                .Select(p => (Name: p.Name, Read: (Func<object?>)(() => p.GetValue(provider)))))
            .ToList();
        // An auto-property shows as the property and its backing field: one member.
        if (matches.Count == 2 && matches.Any(m => m.Name == "ExemplarFilter") && matches.Any(m => m.Name == "<ExemplarFilter>k__BackingField"))
        {
            matches.RemoveAll(m => m.Name != "ExemplarFilter");
        }

        if (matches.Count != 1)
        {
            var members = string.Join(", ", type.GetMembers(flags).Where(m => m.Name.Contains("Exemplar", StringComparison.Ordinal)).Select(m => m.Name));
            throw new AdapterException($"MeterProviderSdk has {matches.Count} ExemplarFilterType? members named ExemplarFilter, expected 1 (SDK bump?); exemplar members: {members}");
        }

        var value = matches[0].Read();

        return value switch
        {
            OpenTelemetry.Metrics.ExemplarFilterType.AlwaysOn => "always_on",
            OpenTelemetry.Metrics.ExemplarFilterType.AlwaysOff => "always_off",
            OpenTelemetry.Metrics.ExemplarFilterType.TraceBased => "trace_based",
            null => "unset",
            _ => "unknown",
        };
    }

    private static LogLevel InstalledLogLevel(ILoggerFactory factory)
    {
        var logger = factory.CreateLogger(ParityScope);
        for (var level = LogLevel.Trace; level < LogLevel.None; level++)
        {
            if (logger.IsEnabled(level))
            {
                return level;
            }
        }

        return LogLevel.None;
    }

    private static string LogLevelName(LogLevel level) => level switch
    {
        LogLevel.Trace => "TRACE",
        LogLevel.Debug => "DEBUG",
        LogLevel.Information => "INFO",
        LogLevel.Warning => "WARN",
        LogLevel.Error => "ERROR",
        LogLevel.Critical => "CRITICAL",
        _ => level.ToString().ToUpperInvariant(),
    };

    private static JsonObject ResourceOf(TracerProvider provider)
    {
        var attributes = new JsonObject();
        foreach (var kv in provider.GetResource().Attributes)
        {
            attributes[kv.Key] = AttrString(kv.Value);
        }

        return attributes;
    }

    private static string AttrString(object? value) => value switch
    {
        null => "",
        bool b => b ? "true" : "false",
        IFormattable f => f.ToString(null, CultureInfo.InvariantCulture),
        _ => value.ToString() ?? "",
    };

    private static JsonArray ToJsonArray(IEnumerable<string> items)
    {
        var array = new JsonArray();
        foreach (var item in items)
        {
            array.Add(item);
        }

        return array;
    }

    // ── export ──────────────────────────────────────────────────────────

    private static async Task<JsonObject> ExportAsync(JsonObject c)
    {
        var snap = new JsonObject();
        var admin = Environment.GetEnvironmentVariable("FAKEOTLP_ADMIN")?.TrimEnd('/')
            ?? throw new AdapterException("FAKEOTLP_ADMIN is not set (the runner sets it for export cases)");

        // Admin calls happen before setup and after shutdown only: the helper instruments HttpClient.
        await AdminAsync(admin, HttpMethod.Delete);
        var run = await SetupAsync(c, snap);
        if (run is not null)
        {
            using var source = new ActivitySource(ParityScope);
            using (source.StartActivity("parity-span"))
            {
            }

            using var meter = new Meter(ParityScope);
            meter.CreateCounter<long>("parity.requests").Add(1);

            var logger = run.Host.Services.GetRequiredService<ILoggerFactory>().CreateLogger(ParityScope);
            logger.LogWarning("parity-log");
        }

        await ShutdownAsync(run, ExportShutdownDeadlineMs);
        snap["received"] = await ReceivedAsync(admin);
        return snap;
    }

    private static async Task<JsonObject> ReceivedAsync(string admin)
    {
        var body = await AdminAsync(admin, HttpMethod.Get);
        var dest = new HashSet<string>((Environment.GetEnvironmentVariable("FAKEOTLP_DEST_IPS") ?? "")
            .Split(',', StringSplitOptions.RemoveEmptyEntries));
        var received = new JsonObject
        {
            ["traces"] = new JsonArray(),
            ["metrics"] = new JsonArray(),
            ["logs"] = new JsonArray(),
            ["other"] = new JsonArray(),
        };
        foreach (var node in JsonNode.Parse(body)?.AsArray() ?? new JsonArray())
        {
            var rec = node!.AsObject();
            if (!dest.Contains((string?)rec["addr"] ?? ""))
            {
                continue;
            }

            var proto = (string?)rec["proto"] ?? "";
            var path = (string?)rec["path"] ?? "";
            var record = new JsonObject
            {
                ["proto"] = proto,
                ["tls"] = rec["tls"]?.GetValue<bool>() ?? false,
                ["port"] = int.Parse((rec["port"]?.ToString()) ?? "0", CultureInfo.InvariantCulture),
                ["path"] = path,
                ["raw_path"] = (string?)rec["raw_path"] ?? "",
                ["encoding"] = (string?)rec["encoding"] ?? "",
                ["headers"] = rec["headers"]?.DeepClone() ?? new JsonObject(),
            };
            var ms = GrpcTimeoutMs((string?)rec["timeout"]);
            if (ms is not null)
            {
                record["timeout_ms"] = ms.Value;
            }

            received[SignalOf(proto, path)]!.AsArray().Add(record);
        }

        return received;
    }

    private static async Task<string> AdminAsync(string admin, HttpMethod method)
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        using var response = await http.SendAsync(new HttpRequestMessage(method, $"{admin}/requests"));
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadAsStringAsync();
    }

    private static string SignalOf(string proto, string path)
    {
        var lower = path.ToLowerInvariant();
        (string Token, string Signal)[] tokens = proto == "grpc"
            ? new[] { (".trace.", "traces"), (".metrics.", "metrics"), (".logs.", "logs") }
            : new[] { ("trace", "traces"), ("metric", "metrics"), ("log", "logs") };
        return tokens.FirstOrDefault(t => lower.Contains(t.Token)).Signal ?? "other";
    }

    private static double? GrpcTimeoutMs(string? value)
    {
        var m = Regex.Match(value ?? "", "^([0-9]+)([HMSmun])$");
        if (!m.Success)
        {
            return null;
        }

        var amount = double.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture);
        return m.Groups[2].Value switch
        {
            "H" => amount * 3_600_000,
            "M" => amount * 60_000,
            "S" => amount * 1_000,
            "m" => amount,
            "u" => amount / 1_000,
            _ => amount / 1_000_000,
        };
    }

    // ── sampler / propagation ───────────────────────────────────────────

    private static ActivityContext RemoteParent(bool sampled, bool debugTracestate = false) => new(
        ActivityTraceId.CreateFromString(TraceId),
        ActivitySpanId.CreateFromString(SpanId),
        sampled ? ActivityTraceFlags.Recorded : ActivityTraceFlags.None,
        debugTracestate ? "debug=true" : null,
        isRemote: true);

    private static async Task<JsonObject> SamplerAsync(JsonObject c)
    {
        var snap = new JsonObject();
        var run = await SetupAsync(c, snap);
        using var source = new ActivitySource(ParityScope);
        var scenario = (string?)c["input"]?["scenario"];
        Activity? target;
        switch (scenario)
        {
            case "root":
                target = source.StartActivity("target", ActivityKind.Internal, default(ActivityContext));
                break;
            case "remote_sampled":
                target = source.StartActivity("target", ActivityKind.Internal, RemoteParent(true));
                break;
            case "remote_unsampled":
                target = source.StartActivity("target", ActivityKind.Internal, RemoteParent(false));
                break;
            case "remote_sampled_debug_tracestate":
                target = source.StartActivity("target", ActivityKind.Internal, RemoteParent(true, debugTracestate: true));
                break;
            case "remote_sampled_debug_baggage":
                Baggage.SetBaggage("debug", "true");
                target = source.StartActivity("target", ActivityKind.Internal, RemoteParent(true));
                break;
            case "local_child_of_root":
            case "local_child_of_remote_unsampled":
                var parent = scenario == "local_child_of_root"
                    ? source.StartActivity("parent", ActivityKind.Internal, default(ActivityContext))
                    : source.StartActivity("parent", ActivityKind.Internal, RemoteParent(false));
                if (parent is null)
                {
                    // The fixture needs a local parent; a dropped parent would make "target" a root.
                    throw new AdapterException($"scenario {scenario}: the parent span was not created (sampler returned Drop without propagation)");
                }

                Activity.Current = parent;
                target = source.StartActivity("target");
                parent?.Stop();
                break;
            default:
                throw new AdapterException($"unknown scenario \"{scenario}\"");
        }

        snap["sampled"] = target?.Recorded ?? false;
        var attributes = new JsonObject();
        foreach (var tag in target?.TagObjects ?? Array.Empty<KeyValuePair<string, object?>>())
        {
            attributes[tag.Key] = AttrString(tag.Value);
        }

        snap["attributes"] = attributes;
        target?.Stop();
        await ShutdownAsync(run);
        return snap;
    }

    private static async Task<JsonObject> PropagationAsync(JsonObject c)
    {
        var snap = new JsonObject();
        var run = await SetupAsync(c, snap);
        using var source = new ActivitySource(ParityScope);
        Baggage.SetBaggage("parity", "1");
        var carrier = new Dictionary<string, string>();
        using (var span = source.StartActivity("parity-span"))
        {
            var context = span?.Context ?? default;
            Propagators.DefaultTextMapPropagator.Inject(new PropagationContext(context, Baggage.Current), carrier,
                static (map, key, value) => map[key] = value);
        }

        snap["headers"] = ToJsonArray(carrier.Keys.Select(k => k.ToLowerInvariant()).OrderBy(k => k, StringComparer.Ordinal));
        await ShutdownAsync(run);
        return snap;
    }

    // ── http_server / grpc_server ───────────────────────────────────────

    private static async Task<JsonObject> HttpServerAsync(JsonObject c)
    {
        var snap = new JsonObject();
        var childSpan = c["input"]?["child_span"]?.GetValue<bool>() ?? false;
        var requests = StringList(c["input"]?["requests"]);
        var traceparent = (string?)c["input"]?["traceparent"];

        PrepareListenerPort(c);
        var handlerSource = new ActivitySource(ParityScope);
        var run = await SetupAsync(c, snap, HostModel.Http1, app => app.Run(async context =>
        {
            if (childSpan)
            {
                using (handlerSource.StartActivity("handler-child"))
                {
                }
            }

            await context.Response.WriteAsync("ok");
        }));
        if (run is null)
        {
            return snap;
        }

        var appPort = BoundPort(run.Host);
        var headers = traceparent is null ? new Dictionary<string, string>() : new Dictionary<string, string> { ["traceparent"] = traceparent };
        var statuses = new JsonArray();
        var serverError = false;
        foreach (var target in requests)
        {
            var (status, _) = await RawHttp.RequestAsync("GET", "127.0.0.1", appPort, target, headers);
            statuses.Add(status);
            serverError |= status >= 500;
        }

        snap["response_statuses"] = statuses;
        var servers = ServerSpans(run.Spans);
        snap["server_spans"] = servers.Count;
        // A SERVER span exists and every SERVER span is in the inbound traceparent's trace.
        var inboundTrace = traceparent?.Split('-') is { Length: 4 } parts ? parts[1] : null;
        snap["server_continues_inbound"] = inboundTrace is not null && servers.Count > 0 && servers.All(a => a.TraceId.ToHexString() == inboundTrace);
        snap["handler_child_spans"] = run.Spans.Ended().Count(a => a.DisplayName == "handler-child");
        var (_, text) = await ScrapeAsync(ListenerPort(run));
        snap["scrape"] = text;
        snap["requests_counted"] = CountIn(text, new Regex("^http_server(_request)?_duration[a-z_]*_count[{ ]"));
        await ShutdownAsync(run);
        if (serverError && c["input"]?["allow_server_error"]?.GetValue<bool>() != true)
        {
            throw new AdapterException("a request answered 5xx (an instrumentation or helper failure, not an expectation); set input.allow_server_error to accept it");
        }

        return snap;
    }

    private static List<Activity> ServerSpans(SpanCapture spans) => spans.Ended().Where(a => a.Kind == ActivityKind.Server).ToList();

    private static async Task<JsonObject> GrpcServerAsync(JsonObject c)
    {
        // O7 (b): the app sets the key; the driver is the app here (design D-NET8, D-NET12).
        Environment.SetEnvironmentVariable("OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION", "true");
        var snap = new JsonObject();
        PrepareListenerPort(c);
        var target = (string?)c["input"]?["method"] ?? "";
        var run = await SetupAsync(c, snap, HostModel.Http2Grpc, app =>
        {
            app.MapGrpcService<GrpcHealthService>();
            app.MapGrpcService<GrpcEchoService>();
        });
        if (run is null)
        {
            return snap;
        }

        var port = BoundPort(run.Host);
        try
        {
            // A health call comes from an uninstrumented client (kubelet, grpc_health_probe): no client
            // span and no traceparent, so the server filter alone must drop it (C11 fix round 1; Go's
            // filtered otelgrpc client injects nothing either). Other calls use the instrumented client,
            // whose CLIENT span the snapshot reports.
            var health = target.StartsWith("/grpc.health.v1.Health/", StringComparison.Ordinal);
            using var suppressed = health ? OpenTelemetry.SuppressInstrumentationScope.Begin() : null;
            using var channel = health
                ? GrpcChannel.ForAddress($"http://127.0.0.1:{port}", new GrpcChannelOptions
                {
                    HttpHandler = new SocketsHttpHandler { ActivityHeadersPropagator = null },
                })
                : GrpcChannel.ForAddress($"http://127.0.0.1:{port}");
            await GrpcMethods.InvokeAsync(channel.CreateCallInvoker(), target);
            snap["call_ok"] = true;
        }
        catch (RpcException)
        {
            snap["call_ok"] = false;
        }

        // The server span ends after the response reaches the client.
        var deadline = Stopwatch.StartNew();
        while (ServerSpans(run.Spans).Count == 0 && deadline.ElapsedMilliseconds < 500)
        {
            await Task.Delay(10);
        }

        var spans = run.Spans.Ended();
        var server = ServerSpans(run.Spans);
        snap["server_spans"] = server.Count;
        snap["server_span_status"] = server.Count == 0 ? "none" : StatusName(server[0]);
        snap["server_span_name"] = server.Count == 0 ? "none" : server[0].DisplayName;
        var client = spans.Where(a => a.Kind == ActivityKind.Client).ToList();
        snap["client_span_name"] = client.Count == 0 ? "none" : client[0].DisplayName;
        // Not a fixture field: every CLIENT span in end order, for task C11 (one CLIENT span per call).
        snap["client_span_names"] = ToJsonArray(client.Select(a => $"{a.Source.Name}:{a.DisplayName}"));
        var (_, text) = await ScrapeAsync(ListenerPort(run));
        snap["rpcs_counted"] = CountIn(text, new Regex("^rpc_server(_call)?_duration[a-z_]*_count[{ ]"));
        await ShutdownAsync(run);
        return snap;
    }

    private static string StatusName(Activity activity) => activity.Status switch
    {
        ActivityStatusCode.Error => "ERROR",
        ActivityStatusCode.Ok => "OK",
        _ => "UNSET",
    };

    private static int BoundPort(IHost host)
    {
        var addresses = host.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses;
        return new Uri(addresses.First()).Port;
    }

    // ── logging ─────────────────────────────────────────────────────────

    private static async Task<JsonObject> LoggingAsync(JsonObject c)
    {
        var snap = new JsonObject();
        var run = await SetupAsync(c, snap);
        if (run is not null)
        {
            var logger = run.Host.Services.GetRequiredService<ILoggerFactory>().CreateLogger(ParityScope);
            // input.attributes: the record's name/value pairs, as the state of the record (not a
            // message template, so the message stays the same text in every language).
            var attributes = (c["input"]?["attributes"] as JsonObject ?? new JsonObject())
                .OrderBy(kv => kv.Key, StringComparer.Ordinal)
                .Select(kv => new KeyValuePair<string, object?>(kv.Key, (string?)kv.Value))
                .ToList();
            logger.Log(LogLevel.Warning, default, attributes, null, (_, _) => "parity-console-record");
        }

        await ShutdownAsync(run);
        return snap;
    }

    // ── metrics ─────────────────────────────────────────────────────────

    private static async Task<JsonObject> MetricsScrapeAsync(JsonObject c)
    {
        var snap = new JsonObject();
        PrepareListenerPort(c);
        var run = await SetupAsync(c, snap);
        if (run is null)
        {
            return snap;
        }

        var port = ListenerPort(run);
        using var meter = new Meter(ParityScope);
        meter.CreateCounter<long>("parity.requests").Add(1);
        // A counter with a unit: the scrape name carries the unit word.
        meter.CreateCounter<long>("parity.payload", unit: "By").Add(5);
        // Unit 1: a Gauge gets _ratio, an UpDownCounter does not (contract D1 row "Unit 1", A21).
        meter.CreateObservableGauge("parity.utilization", () => 0.5, unit: "1");
        meter.CreateUpDownCounter<long>("parity.inflight", unit: "1").Add(1);
        // Label-name collision, suffix semantics and a mixed-case name (A21).
        meter.CreateCounter<long>("parity.collide").Add(1, new KeyValuePair<string, object?>("a.b", "x"), new KeyValuePair<string, object?>("a_b", "y"));
        meter.CreateCounter<long>("parity.collide_sorted").Add(1, new KeyValuePair<string, object?>("a.b", "y"), new KeyValuePair<string, object?>("a_b", "x"));
        meter.CreateCounter<long>("probe.bytes-sent", unit: "By").Add(1);
        meter.CreateCounter<long>("req.total.count").Add(1);
        meter.CreateObservableGauge("ratio.load", () => 1.0, unit: "1");
        meter.CreateCounter<long>("Parity.Mixed").Add(1);

        var (status, text) = await ScrapeAsync(port);
        snap["scrape"] = text;
        snap["scrape_status"] = status;
        await ShutdownAsync(run);
        return snap;
    }

    private static async Task<JsonObject> MetricsListenerAsync(JsonObject c)
    {
        var snap = new JsonObject();
        PrepareListenerPort(c);
        var run = await SetupAsync(c, snap);
        if (run is null)
        {
            return snap;
        }

        var port = ListenerPort(run);
        if (!await WaitBoundAsync(port))
        {
            await ShutdownAsync(run);
            return new JsonObject { ["adapter_error"] = $"listener never bound on port {port}" };
        }

        // input.connect_host: "127.0.0.1" (default) or "localhost" (every address the resolver returns).
        var host = (string?)c["input"]?["connect_host"] ?? "127.0.0.1";
        var statuses = new JsonArray();
        foreach (var request in StringList(c["input"]?["requests"]))
        {
            var parts = request.Split(' ', 2);
            var (status, _) = await RawHttp.RequestAsync(parts[0], host, port, parts[1], NoHeaders);
            statuses.Add(status);
        }

        snap["listener_statuses"] = statuses;
        await ShutdownAsync(run);
        return snap;
    }

    private static List<string> StringList(JsonNode? node) =>
        node?.AsArray().Select(n => (string)n!).ToList() ?? new List<string>();

    /// <summary>
    /// The helper's own listener serves the scrape (0.2 has no mounted handler, task C12).
    /// A free port is set only when the case sets no port itself (env or code).
    /// </summary>
    private static void PrepareListenerPort(JsonObject c)
    {
        var env = c["env"] as JsonObject ?? new JsonObject();
        var code = c["code"] as JsonObject ?? new JsonObject();
        if (!env.ContainsKey("OTEL_HELPER_METRICS_PORT") && !env.ContainsKey("OTEL_EXPORTER_PROMETHEUS_PORT")
            && !code.ContainsKey("prometheus_metrics_port"))
        {
            Environment.SetEnvironmentVariable("OTEL_HELPER_METRICS_PORT", FreePort().ToString(CultureInfo.InvariantCulture));
        }
    }

    /// <summary>The port the helper resolved; 0 (no listener) cannot be scraped.</summary>
    private static int ListenerPort(Run run) => run.Resolved.PrometheusMetricsPort > 0
        ? run.Resolved.PrometheusMetricsPort
        : throw new AdapterException("the case sets the listener port to 0: there is no /metrics to scrape (no mounted handler, D-NET11)");

    private static int FreePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static async Task<bool> WaitBoundAsync(int port)
    {
        for (var i = 0; i < 100; i++)
        {
            try
            {
                using var client = new TcpClient();
                await client.ConnectAsync(IPAddress.Loopback, port);
                return true;
            }
            catch (SocketException)
            {
                await Task.Delay(20);
            }
        }

        return false;
    }

    /// <summary>
    /// GET /metrics from the helper's listener (no mounted handler in 0.2, task C12).
    /// Status 0 and empty text when nothing listens, i.e. Prometheus is inactive.
    /// </summary>
    private static async Task<(int Status, string Text)> ScrapeAsync(int port)
    {
        if (!await WaitBoundAsync(port))
        {
            return (0, "");
        }

        return await RawHttp.RequestAsync("GET", "127.0.0.1", port, "/metrics", NoHeaders);
    }

    /// <summary>Sums the `_count` samples of the series matching <paramref name="nameRegex"/>.</summary>
    private static double CountIn(string text, Regex nameRegex)
    {
        double total = 0;
        foreach (var line in text.Split('\n'))
        {
            if (line.StartsWith('#') || !nameRegex.IsMatch(line))
            {
                continue;
            }

            // "<name>{labels} <value> [timestamp]": the value is the first token after the labels.
            var afterLabels = line.Contains('}') ? line[(line.LastIndexOf('}') + 1)..] : line[(line.IndexOf(' ') + 1)..];
            var tokens = afterLabels.Trim().Split(' ', StringSplitOptions.RemoveEmptyEntries);
            if (tokens.Length > 0 && double.TryParse(tokens[0], NumberStyles.Float, CultureInfo.InvariantCulture, out var value))
            {
                total += value;
            }
        }

        return total;
    }
}

/// <summary>A fixture or adapter bug: fails the case whatever its status.</summary>
internal sealed class AdapterException : Exception
{
    public AdapterException(string message) : base(message)
    {
    }
}
