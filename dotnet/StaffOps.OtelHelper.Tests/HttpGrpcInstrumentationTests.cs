using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Net;
using System.Net.Sockets;
using System.Text;
using Grpc.AspNetCore.Server.Model;
using Grpc.Core;
using Grpc.Net.Client;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Routing;
using Microsoft.AspNetCore.Server.Kestrel.Core;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Metrics;
using StaffOps.OtelHelper.Tracing;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C11 (contract R7, R10, D5, A22, A30; .NET DN5, DN7; design D-NET8, N1, N5): contract
/// health normalization and the default list on ASP.NET Core and HttpClient, <c>HealthPaths</c>,
/// the gRPC health service dropped on the server and the client, one CLIENT span per gRPC call,
/// the O7 (b) warning, keyed DI, <c>&lt;ServiceName&gt;.*</c> meters and the HTTP opt-in metrics
/// switch. Requirement DN7: health semantics and one gRPC client span; DN5: keyed instrumentation.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public sealed class HttpGrpcInstrumentationTests : IDisposable
{
    private static readonly string[] Variables =
    {
        TelemetryOptions.HttpOptInMetricsEnvVar, GrpcServerTracingCheck.GrpcInstrumentationKey, "OTEL_TRACES_EXPORTER",
        "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_HELPER_DISABLED_SIGNALS", "OTEL_METRICS_EXPORTER", "OTEL_SDK_DISABLED",
        "OTEL_PROPAGATORS",
    };

    public HttpGrpcInstrumentationTests() => Clear();

    public void Dispose() => Clear();

    private static void Clear()
    {
        foreach (var variable in Variables)
        {
            Environment.SetEnvironmentVariable(variable, null);
        }
    }

    private static void Set(string variable, string? value) => Environment.SetEnvironmentVariable(variable, value);

    // ── normalization (contract D5, D1 rows "Percent-encoded health paths", "ASCII case folding") ──

    [Theory]
    [InlineData("/", "/")]
    [InlineData("///", "/")]
    [InlineData("/A/B/?q#f", "/a/b")]
    [InlineData("/x#y?z", "/x")]
    [InlineData("/%68ealth", "/health")]
    [InlineData("/HEALTH%2f", "/health")]
    [InlineData("/health%3Fx=1", "/health")]
    [InlineData("/health%23frag", "/health")]
    [InlineData("/%zz/HEALTH", "/%zz/health")]
    [InlineData("/health%2", "/health%2")]
    [InlineData("/%FF/x", "/%ff/x")]
    [InlineData("/caf%C3%A9", "/café")]
    [InlineData("/Kube", "/Kube")]
    [InlineData("/KÉ", "/kÉ")]
    [InlineData("", "")]
    public void Normalize_Follows_The_Contract(string raw, string expected)
        => Assert.Equal(expected, HealthMatching.Normalize(raw));

    [Theory]
    [InlineData("/grpc.health.v1.Health/Check", true)]
    [InlineData("/grpc.health.v1.Health/Watch", true)]
    [InlineData("/grpc.health.v1.Health/List", true)]
    [InlineData("grpc.health.v1.Health/Check", true)]
    [InlineData("/grpc.health.v1.Health/Check?x", true)]
    [InlineData("/health.v1.Health/Check", false)]
    [InlineData("/grpc.health.v1.HealthX/Check", false)]
    [InlineData("/GRPC.health.v1.Health/Check", false)]
    [InlineData("/parity.v1.Echo/Ping", false)]
    public void The_Grpc_Health_Service_Is_Matched_Whole_And_Case_Sensitive(string path, bool expected)
        => Assert.Equal(expected, HealthMatching.IsGrpcHealthMethod(path));

    [Theory]
    [InlineData("/health", true)]
    [InlineData("/Healthz/", true)]
    [InlineData("/ready?probe=1", true)]
    [InlineData("/readyz", true)]
    [InlineData("/live", true)]
    [InlineData("/livez", true)]
    [InlineData("/ping", true)]
    [InlineData("/metrics", true)]
    [InlineData("/users/ping", false)]
    [InlineData("/healthcare", false)]
    [InlineData("/health/live", false)]
    public void The_Default_List_Is_The_Contract_List(string path, bool expected)
        => Assert.Equal(expected, HealthMatching.IsHealth(path, HealthMatching.PathSet(TestDiagnostics.ResolveQuiet(TestDiagnostics.Identified).HealthPaths)));

    [Fact]
    public void Default_List_Matches_Go()
        => Assert.Equal(new[] { "/health", "/healthz", "/ready", "/readyz", "/live", "/livez", "/ping", "/metrics" },
            TestDiagnostics.ResolveQuiet(TestDiagnostics.Identified).HealthPaths);

    [Theory]
    [InlineData("status")]
    [InlineData("/status?x")]
    [InlineData("/status#x")]
    [InlineData("/a,/b")]
    [InlineData("/stätus")]
    [InlineData("")]
    public void Invalid_Code_Health_Path_Fails_Start(string path)
    {
        var options = new TelemetryOptions { HealthPaths = new List<string> { "/ok", path } };
        var result = new TelemetryOptionsValidator().Validate(null, options);
        Assert.True(result.Failed);
        Assert.Contains("TelemetryOptions.HealthPaths item #2", result.FailureMessage);
        Assert.Contains("must be ASCII, start with \"/\" and have no query, fragment or comma", result.FailureMessage);
    }

    [Fact]
    public void Null_Code_Health_Path_Fails_Start()
    {
        var options = new TelemetryOptions { HealthPaths = new List<string> { null! } };
        var result = new TelemetryOptionsValidator().Validate(null, options);
        Assert.True(result.Failed);
        Assert.Contains("TelemetryOptions.HealthPaths item #1 (null)", result.FailureMessage);
    }

    [Fact]
    public void Valid_Code_Health_Paths_Pass()
        => Assert.False(new TelemetryOptionsValidator().Validate(null, new TelemetryOptions { HealthPaths = new List<string> { "/Status/", "/", "/a/b" } }).Failed);

    // ── HTTP opt-in metrics switch (A30) ──────────────────────────────────

    [Theory]
    [InlineData(null, false, false)]
    [InlineData("", false, false)]
    [InlineData("TRUE", true, false)]
    [InlineData(" true\t", true, false)]
    [InlineData("false", false, false)]
    [InlineData("yes", false, true)]
    [InlineData("1", false, true)]
    public void Http_Opt_In_Metrics_From_The_Environment(string? value, bool expected, bool warns)
    {
        Set(TelemetryOptions.HttpOptInMetricsEnvVar, value);
        var diagnostics = new TestDiagnostics();
        Assert.Equal(expected, diagnostics.Resolve(TestDiagnostics.Identified).HttpOptInMetrics);
        var warning = $"otelhelper: WARN invalid OTEL_HELPER_HTTP_OPT_IN_METRICS: not a boolean (true or false), using false variable=OTEL_HELPER_HTTP_OPT_IN_METRICS value={value}";
        Assert.Equal(warns, diagnostics.Lines.Contains(warning));
        Assert.Equal(warns ? 1 : 0, diagnostics.Lines.Count(l => l.Contains("OTEL_HELPER_HTTP_OPT_IN_METRICS", StringComparison.Ordinal)));
    }

    [Theory]
    [InlineData("true", false)]
    [InlineData("yes", true)]
    [InlineData(null, true)]
    public void Http_Opt_In_Metrics_Code_Wins_And_The_Variable_Is_Not_Read(string? value, bool code)
    {
        Set(TelemetryOptions.HttpOptInMetricsEnvVar, value);
        var diagnostics = new TestDiagnostics();
        Assert.Equal(code, diagnostics.Resolve(o =>
        {
            TestDiagnostics.Identified(o);
            o.HttpOptInMetrics = code;
        }).HttpOptInMetrics);
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("OTEL_HELPER_HTTP_OPT_IN_METRICS", StringComparison.Ordinal));
    }

    [Theory]
    [InlineData("Microsoft.AspNetCore.Hosting", "http.server.active_requests", true)]
    [InlineData("System.Net.Http", "http.client.active_requests", true)]
    [InlineData("System.Net.Http", "http.client.open_connections", true)]
    [InlineData("System.Net.Http", "http.client.connection.duration", true)]
    [InlineData("System.Net.Http", "http.client.request.time_in_queue", true)]
    [InlineData("System.Net.Http", "http.client.request.duration", false)]
    [InlineData("Microsoft.AspNetCore.Hosting", "http.server.request.duration", false)]
    [InlineData("my-service", "http.server.active_requests", false)]
    [InlineData("Microsoft.AspNetCore.Server.Kestrel", "kestrel.active_connections", false)]
    public void The_Opt_In_View_Is_Scoped_To_The_Framework_Http_Meters(string meter, string instrument, bool dropped)
        => Assert.Equal(dropped, MetricsSetup.IsHttpOptInInstrument(meter, instrument));

    // ── a real web host ─────────────────────────────────────────────────

    private sealed class Capture : BaseProcessor<Activity>
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

    private sealed class Web : IAsyncDisposable
    {
        public required WebApplication App { get; init; }

        public required Capture Spans { get; init; }

        public required List<Metric> Metrics { get; init; }

        public required TestDiagnostics Diagnostics { get; init; }

        public int Port => new Uri(App.Urls.First()).Port;

        /// <summary>SERVER spans of this host (other hosts in the process report to the same instrumentation).</summary>
        public List<Activity> ServerSpans() => Spans.Ended()
            .Where(a => a.Kind == ActivityKind.Server && Equals(a.GetTagItem("server.port"), Port))
            .ToList();

        /// <summary>CLIENT spans to this host.</summary>
        public List<Activity> ClientSpans() => Spans.Ended()
            .Where(a => a.Kind == ActivityKind.Client && Equals(a.GetTagItem("server.port"), Port))
            .ToList();

        public List<string> MetricNames()
        {
            App.Services.GetRequiredService<MeterProvider>().ForceFlush();
            lock (Metrics)
            {
                return Metrics.Select(m => m.Name).Distinct().ToList();
            }
        }

        public async ValueTask DisposeAsync()
        {
            await App.StopAsync();
            await App.DisposeAsync();
        }
    }

    private static async Task<Web> StartAsync(
        Action<TelemetryOptions>? configure = null,
        bool grpc = false,
        Action<WebApplicationBuilder>? builderHook = null,
        Action<WebApplication>? map = null,
        Action<WebApplicationBuilder>? preHook = null)
    {
        var capture = new Capture();
        var metrics = new List<Metric>();
        var diagnostics = new TestDiagnostics();
        var builder = WebApplication.CreateBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.ConfigureKestrel(k => k.Listen(IPAddress.Loopback, 0, o => o.Protocols = grpc ? HttpProtocols.Http2 : HttpProtocols.Http1));
        builder.Services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        preHook?.Invoke(builder);
        builder.Services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "c11-web";
            o.PrometheusMetricsPort = 0;
            configure?.Invoke(o);
        });
        builder.Services.ConfigureOpenTelemetryTracerProvider(b => b.AddProcessor(capture));
        builder.Services.ConfigureOpenTelemetryMeterProvider(b => b.AddInMemoryExporter(metrics));
        if (grpc)
        {
            builder.Services.AddGrpc();
            builder.Services.AddSingleton<IServiceMethodProvider<TestGrpcService>, TestGrpcMethods>();
        }

        builderHook?.Invoke(builder);
        var app = builder.Build();
        if (grpc)
        {
            app.MapGrpcService<TestGrpcService>();
        }

        if (map is not null)
        {
            map(app);
        }
        else if (!grpc)
        {
            app.Run(context => context.Response.WriteAsync("ok"));
        }

        await app.StartAsync();
        return new Web { App = app, Spans = capture, Metrics = metrics, Diagnostics = diagnostics };
    }

    /// <summary>One HTTP/1.1 GET with the target sent as written (HttpClient would canonicalize it and add a client span).</summary>
    private static async Task<int> RawGetAsync(int port, string target)
    {
        using var client = new TcpClient();
        await client.ConnectAsync(IPAddress.Loopback, port);
        var stream = client.GetStream();
        await stream.WriteAsync(Encoding.ASCII.GetBytes($"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"));
        using var reader = new StreamReader(stream, Encoding.ASCII);
        var status = (await reader.ReadLineAsync())!.Split(' ')[1];
        await reader.ReadToEndAsync();
        return int.Parse(status, System.Globalization.CultureInfo.InvariantCulture);
    }

    /// <summary>Waits for the server spans of a host to end (a span ends after the response is sent).</summary>
    private static async Task<List<Activity>> SettledServerSpans(Web web, int expected)
    {
        var watch = Stopwatch.StartNew();
        while (web.ServerSpans().Count < expected && watch.ElapsedMilliseconds < 2000)
        {
            await Task.Delay(10);
        }

        await Task.Delay(50);
        return web.ServerSpans();
    }

    [Theory]
    [InlineData("/Health/?probe=1")]
    [InlineData("/healthz?x=1")]
    [InlineData("/%68ealth")]
    [InlineData("/HEALTH%2f")]
    [InlineData("/readyz")]
    [InlineData("/live/")]
    [InlineData("/livez")]
    [InlineData("/metrics")]
    [InlineData("/ping#f")]
    public async Task Server_Health_Requests_Get_No_Span_And_Are_Counted(string target)
    {
        await using var web = await StartAsync();
        Assert.Equal(200, await RawGetAsync(web.Port, target));
        Assert.Equal(200, await RawGetAsync(web.Port, "/orders"));
        var spans = await SettledServerSpans(web, 1);
        Assert.Equal(new[] { "/orders" }, spans.Select(a => (string?)a.GetTagItem("url.path")).ToArray());
        var duration = web.MetricNames();
        Assert.Contains("http.server.request.duration", duration);
        Assert.Equal(2, ServerRequestCount(web));
    }

    [Theory]
    [InlineData("/users/ping")]
    [InlineData("/healthcare")]
    [InlineData("/health/live")]
    [InlineData("/%zzhealth")]
    public async Task Server_Paths_That_Only_Contain_A_Health_Path_Are_Traced(string target)
    {
        await using var web = await StartAsync();
        await RawGetAsync(web.Port, target);
        Assert.Single(await SettledServerSpans(web, 1));
    }

    [Fact]
    public async Task Code_Health_Paths_Replace_The_Default_List()
    {
        await using var web = await StartAsync(o => o.HealthPaths = new List<string> { "/Status/" });
        await RawGetAsync(web.Port, "/STATUS?x=1");
        await RawGetAsync(web.Port, "/health");
        var spans = await SettledServerSpans(web, 1);
        Assert.Equal(new[] { "/health" }, spans.Select(a => (string?)a.GetTagItem("url.path")).ToArray());
    }

    [Theory]
    [InlineData(true, 1)]
    [InlineData(false, 0)]
    public async Task An_Application_Filter_Registered_After_AddOtelHelper_Replaces_The_Health_Filter(bool after, int spans)
    {
        // HOW-TO §9 (fix round 1): options registered after AddOtelHelper win; before, the helper's win.
        void Mine(OpenTelemetry.Instrumentation.AspNetCore.AspNetCoreTraceInstrumentationOptions o) => o.Filter = _ => true;
        await using var web = await StartAsync(builderHook: after ? b => b.Services.Configure<OpenTelemetry.Instrumentation.AspNetCore.AspNetCoreTraceInstrumentationOptions>(Mine) : null,
            preHook: after ? null : b => b.Services.Configure<OpenTelemetry.Instrumentation.AspNetCore.AspNetCoreTraceInstrumentationOptions>(Mine));
        await RawGetAsync(web.Port, "/health");
        Assert.Equal(spans, (await SettledServerSpans(web, spans)).Count);
    }

    [Fact]
    public async Task An_Empty_Health_List_Traces_Every_Path()
    {
        await using var web = await StartAsync(o => o.HealthPaths = new List<string>());
        await RawGetAsync(web.Port, "/health");
        await RawGetAsync(web.Port, "/ping");
        Assert.Equal(2, (await SettledServerSpans(web, 2)).Count);
    }

    [Fact]
    public async Task Children_Of_A_Health_Request_Are_Dropped_And_Of_Others_Kept()
    {
        var source = new ActivitySource("c11-web");
        await using var web = await StartAsync(map: app => app.Run(async context =>
        {
            using (source.StartActivity("handler-child"))
            {
            }

            await context.Response.WriteAsync("ok");
        }));
        await RawGetAsync(web.Port, "/health/");
        await RawGetAsync(web.Port, "/orders");
        var server = Assert.Single(await SettledServerSpans(web, 1));
        var children = web.Spans.Ended().Where(a => a.DisplayName == "handler-child").ToList();
        Assert.Equal(server.SpanId, Assert.Single(children).ParentSpanId);
    }

    private static long ServerRequestCount(Web web)
    {
        lock (web.Metrics)
        {
            var latest = web.Metrics.Where(m => m.Name == "http.server.request.duration").ToList();
            long count = 0;
            foreach (var metric in latest.TakeLast(1))
            {
                foreach (ref readonly var point in metric.GetMetricPoints())
                {
                    count += point.GetHistogramCount();
                }
            }

            return count;
        }
    }

    // ── HttpClient health exclusion ──────────────────────────────────────

    [Fact]
    public async Task HttpClient_Health_Requests_Get_No_Client_Span()
    {
        await using var web = await StartAsync();
        using var client = new HttpClient();
        foreach (var path in new[] { "/Health/", "/livez?x=1", "/%68ealth", "/metrics", "/grpc.health.v1.Health/Check" })
        {
            await client.GetAsync($"http://127.0.0.1:{web.Port}{path}");
        }

        Assert.Empty(web.ClientSpans());
        await client.GetAsync($"http://127.0.0.1:{web.Port}/users/ping");
        Assert.Equal(new[] { "/users/ping" }, web.ClientSpans().Select(a => new Uri((string)a.GetTagItem("url.full")!).AbsolutePath).ToArray());
    }

    [Theory]
    // absolute-form target; traced
    [InlineData("/%2568ealth", true)]
    [InlineData("/health", false)]
    [InlineData("/Health/?x=1", false)]
    public async Task Absolute_Form_Targets_Are_Decoded_Once(string path, bool traced)
    {
        // Fix round 1 (LOW 1): PathBase + Path is already decoded; decoding it again would turn
        // /%2568ealth (the path /%68ealth) into /health.
        await using var web = await StartAsync();
        Assert.Equal(200, await RawGetAsync(web.Port, $"http://127.0.0.1:{web.Port}{path}"));
        Assert.Equal(traced ? 1 : 0, (await SettledServerSpans(web, traced ? 1 : 0)).Count);
    }

    /// <summary>One request's headers, as a downstream receives them (lower-case names, raw values).</summary>
    private static async Task<List<(string Name, string Value)>> HeadersReceivedAsync(HttpClient client, string path, bool appHeaders = false)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        try
        {
            var port = ((IPEndPoint)listener.LocalEndpoint).Port;
            var accept = Task.Run(async () =>
            {
                using var socket = await listener.AcceptTcpClientAsync();
                var stream = socket.GetStream();
                var text = new StringBuilder();
                var buffer = new byte[8192];
                while (!text.ToString().Contains("\r\n\r\n", StringComparison.Ordinal))
                {
                    var read = await stream.ReadAsync(buffer);
                    if (read == 0)
                    {
                        break;
                    }

                    text.Append(Encoding.ASCII.GetString(buffer, 0, read));
                }

                await stream.WriteAsync(Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"));
                return text.ToString();
            });
            using var request = new HttpRequestMessage(HttpMethod.Get, $"http://127.0.0.1:{port}{path}");
            if (appHeaders)
            {
                // Set by the application itself: never removed (fix round 2).
                request.Headers.TryAddWithoutValidation("baggage", "app.key=app-value");
                request.Headers.TryAddWithoutValidation("tracestate", "app=1");
            }

            await client.SendAsync(request);
            var received = await accept.WaitAsync(TimeSpan.FromSeconds(10));
            return received.Split("\r\n").Skip(1).Where(l => l.Contains(':'))
                .Select(l => (l[..l.IndexOf(':')].Trim().ToLowerInvariant(), l[(l.IndexOf(':') + 1)..].Trim())).ToList();
        }
        finally
        {
            listener.Stop();
        }
    }

    private const string InboundTraceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";

    [Theory]
    // path, in a server request, OTEL_PROPAGATORS, app-set headers, traces disabled, trace context sent
    [InlineData("/healthz", true, null, false, false, false)]
    [InlineData("/Health/?x=1", true, null, false, false, false)]
    [InlineData("/grpc.health.v1.Health/Check", true, null, false, false, false)]
    [InlineData("/healthz", false, null, false, false, false)]
    [InlineData("/healthz", true, "tracecontext", false, false, false)]
    [InlineData("/healthz", false, "tracecontext", false, false, false)]
    [InlineData("/healthz", true, null, true, false, false)]
    [InlineData("/healthz", false, null, true, false, false)]
    [InlineData("/healthz", true, null, false, true, false)]
    [InlineData("/orders", true, null, false, false, true)]
    [InlineData("/orders", true, "tracecontext", false, false, true)]
    [InlineData("/orders", true, null, false, true, true)]
    public async Task HttpClient_Health_Requests_Send_No_Context(string path, bool insideRequest, string? propagators, bool appHeaders, bool tracesDisabled, bool sent)
    {
        // Fix round 1 (LOW, dev): a filtered health call sent the runtime's traceparent ...-01 naming a
        // span that is never exported; as Go's NewHTTPTransport, no trace context is sent now. A lone
        // tracecontext: the instrumentation does not inject, the runtime does (HelperRuntimePropagator).
        // Fix round 2: what the application set (baggage, tracestate) is kept; with traces disabled
        // (pass-through, the HttpClient filter does not run) the health call carries nothing either.
        Set("OTEL_PROPAGATORS", propagators);
        Set("OTEL_HELPER_DISABLED_SIGNALS", tracesDisabled ? "traces" : null);
        await using var web = await StartAsync(map: app => app.Run(async context =>
        {
            using var client = new HttpClient();
            var headers = await HeadersReceivedAsync(client, context.Request.Query["path"]!, appHeaders);
            await context.Response.WriteAsync(string.Join("\n", headers.Select(h => h.Name + "=" + h.Value)));
        }));
        List<(string Name, string Value)> received;
        if (insideRequest)
        {
            using var caller = new HttpClient(new SocketsHttpHandler { ActivityHeadersPropagator = null });
            using var request = new HttpRequestMessage(HttpMethod.Get, $"http://127.0.0.1:{web.Port}/work?path={Uri.EscapeDataString(path)}");
            if (tracesDisabled)
            {
                request.Headers.TryAddWithoutValidation("traceparent", InboundTraceparent);
            }

            var body = await (await caller.SendAsync(request)).Content.ReadAsStringAsync();
            received = body.Split('\n', StringSplitOptions.RemoveEmptyEntries)
                .Select(l => (l[..l.IndexOf('=')], l[(l.IndexOf('=') + 1)..])).ToList();
        }
        else
        {
            using var client = new HttpClient();
            received = await HeadersReceivedAsync(client, path, appHeaders);
        }

        var names = received.Select(h => h.Name).ToList();
        Assert.Equal(sent, names.Contains("traceparent"));
        Assert.DoesNotContain("request-id", names);
        if (tracesDisabled && sent)
        {
            Assert.Equal(InboundTraceparent, received.Single(h => h.Name == "traceparent").Value);
        }

        if (appHeaders)
        {
            Assert.Contains(("baggage", "app.key=app-value"), received);
            Assert.Contains(("tracestate", "app=1"), received);
        }
        else if (!sent)
        {
            Assert.DoesNotContain("tracestate", names);
            Assert.DoesNotContain("baggage", names);
        }
    }

    [Fact]
    public async Task HttpClient_Follows_Code_Health_Paths()
    {
        await using var web = await StartAsync(o => o.HealthPaths = new List<string> { "/status" });
        using var client = new HttpClient();
        await client.GetAsync($"http://127.0.0.1:{web.Port}/STATUS/");
        await client.GetAsync($"http://127.0.0.1:{web.Port}/health");
        Assert.Single(web.ClientSpans());
    }

    // ── gRPC ─────────────────────────────────────────────────────────────

    private static async Task<bool> CallAsync(Web web, Method<byte[], byte[]> method)
    {
        using var channel = GrpcChannel.ForAddress($"http://127.0.0.1:{web.Port}");
        var invoker = channel.CreateCallInvoker();
        try
        {
            if (method.Type == MethodType.ServerStreaming)
            {
                using var call = invoker.AsyncServerStreamingCall(method, null, default, Array.Empty<byte>());
                while (await call.ResponseStream.MoveNext(CancellationToken.None))
                {
                }
            }
            else
            {
                await invoker.AsyncUnaryCall(method, null, default, Array.Empty<byte>()).ResponseAsync;
            }

            return true;
        }
        catch (RpcException)
        {
            return false;
        }
    }

    [Fact]
    public async Task One_Client_Span_Per_Grpc_Call_And_The_Server_Continues_It()
    {
        Set(GrpcServerTracingCheck.GrpcInstrumentationKey, "true");
        await using var web = await StartAsync(grpc: true);
        Assert.True(await CallAsync(web, TestGrpcMethods.EchoPing));
        var server = Assert.Single(await SettledServerSpans(web, 1));

        // Every CLIENT span of the call, whatever its tags: only the gRPC one.
        var clients = web.Spans.Ended().Where(a => a.Kind == ActivityKind.Client && a.TraceId == server.TraceId).ToList();
        var client = Assert.Single(clients);
        Assert.Equal("OpenTelemetry.Instrumentation.GrpcNetClient", client.Source.Name);
        Assert.Equal("parity.v1.Echo/Ping", client.DisplayName);
        Assert.Equal(client.SpanId, server.ParentSpanId);
        Assert.Equal("parity.v1.Echo/Ping", server.DisplayName);
        Assert.Equal("Microsoft.AspNetCore", server.Source.Name);
        Assert.Equal("grpc", server.GetTagItem("rpc.system.name"));
        Assert.Equal(ActivityStatusCode.Unset, server.Status);
    }

    [Fact]
    public async Task Grpc_Not_Found_Is_Unset_On_The_Server()
    {
        Set(GrpcServerTracingCheck.GrpcInstrumentationKey, "true");
        await using var web = await StartAsync(grpc: true);
        Assert.False(await CallAsync(web, TestGrpcMethods.EchoNotFound));
        var server = Assert.Single(await SettledServerSpans(web, 1));
        Assert.Equal("parity.v1.Echo/NotFound", server.DisplayName);
        Assert.Equal(ActivityStatusCode.Unset, server.Status);
    }

    [Theory]
    [InlineData("Check")]
    [InlineData("Watch")]
    public async Task Grpc_Health_Calls_Get_No_Server_Or_Client_Span(string method)
    {
        Set(GrpcServerTracingCheck.GrpcInstrumentationKey, "true");
        await using var web = await StartAsync(grpc: true);
        Assert.True(await CallAsync(web, method == "Check" ? TestGrpcMethods.HealthCheck : TestGrpcMethods.HealthWatch));
        Assert.True(await CallAsync(web, TestGrpcMethods.EchoPing));
        var server = Assert.Single(await SettledServerSpans(web, 1));
        Assert.Equal("parity.v1.Echo/Ping", server.DisplayName);
        Assert.DoesNotContain(web.Spans.Ended(), a => a.DisplayName.Contains("Health", StringComparison.Ordinal) || (a.GetTagItem("url.full") as string)?.Contains("Health", StringComparison.Ordinal) == true);
    }

    /// <summary>
    /// A call that carries no context at all: the client instrumentations are suppressed and the
    /// handler writes no headers, so the server decides as a root (an instrumented client's filtered
    /// span would send an unsampled parent, which ParentBased drops on its own).
    /// </summary>
    private static async Task<bool> CallWithoutContextAsync(Web web, Method<byte[], byte[]> method)
    {
        using var scope = SuppressInstrumentationScope.Begin();
        using var channel = GrpcChannel.ForAddress($"http://127.0.0.1:{web.Port}", new GrpcChannelOptions
        {
            HttpHandler = new SocketsHttpHandler { ActivityHeadersPropagator = null },
        });
        try
        {
            await channel.CreateCallInvoker().AsyncUnaryCall(method, null, default, Array.Empty<byte>()).ResponseAsync;
            return true;
        }
        catch (RpcException)
        {
            return false;
        }
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task The_Server_Filter_Drops_Grpc_Health_Calls_Of_An_Uninstrumented_Client(bool key)
    {
        Set(GrpcServerTracingCheck.GrpcInstrumentationKey, key ? "true" : null);
        await using var web = await StartAsync(grpc: true);
        Assert.True(await CallWithoutContextAsync(web, TestGrpcMethods.HealthCheck));
        Assert.True(await CallWithoutContextAsync(web, TestGrpcMethods.EchoPing));
        var server = Assert.Single(await SettledServerSpans(web, 1));
        Assert.Equal(default, server.ParentSpanId);
        Assert.Contains("parity.v1.Echo/Ping", server.DisplayName, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Grpc_Health_Service_Is_Dropped_Whatever_The_Health_List()
    {
        Set(GrpcServerTracingCheck.GrpcInstrumentationKey, "true");
        await using var web = await StartAsync(o => o.HealthPaths = new List<string>(), grpc: true);
        Assert.True(await CallWithoutContextAsync(web, TestGrpcMethods.HealthCheck));
        await Task.Delay(100);
        Assert.Empty(web.ServerSpans());
    }

    // ── O7 (b): the one-time warning ─────────────────────────────────────

    private static string[] GrpcWarnings(Web web)
        => web.Diagnostics.Lines
            .Where(l => l.Contains(GrpcServerTracingCheck.GrpcInstrumentationKey, StringComparison.Ordinal))
            // An invalid value also gets the instrumentation's own (forwarded) parse warning.
            .Where(l => !l.StartsWith("otelhelper: WARN sdk ", StringComparison.Ordinal))
            .ToArray();

    [Theory]
    // env value, IConfiguration value, warns
    [InlineData(null, null, true)]
    [InlineData("false", null, true)]
    [InlineData("yes", null, true)]
    [InlineData("1", null, true)]
    [InlineData("true", null, false)]
    [InlineData(" True ", null, false)]
    [InlineData(null, "true", false)]
    [InlineData("true", "false", true)]
    public async Task Mapped_Grpc_Endpoints_Without_The_Key_Warn_Once(string? env, string? configured, bool warns)
    {
        Set(GrpcServerTracingCheck.GrpcInstrumentationKey, env);
        await using var web = await StartAsync(grpc: true, builderHook: b =>
        {
            if (configured is not null)
            {
                b.Configuration[GrpcServerTracingCheck.GrpcInstrumentationKey] = configured;
            }
        });
        await CallAsync(web, TestGrpcMethods.EchoPing);
        await CallAsync(web, TestGrpcMethods.EchoPing);
        Assert.Equal(warns
            ? new[] { "otelhelper: WARN " + GrpcServerTracingCheck.WarningText + " variable=" + GrpcServerTracingCheck.GrpcInstrumentationKey }
            : Array.Empty<string>(), GrpcWarnings(web));
    }

    [Fact]
    public async Task Without_The_Key_Grpc_Server_Spans_Are_Http_Shaped()
    {
        await using var web = await StartAsync(grpc: true);
        Assert.False(await CallAsync(web, TestGrpcMethods.EchoNotFound));
        var server = Assert.Single(await SettledServerSpans(web, 1));
        Assert.Equal("POST", server.DisplayName.Split(' ')[0]);
        Assert.Equal(200, server.GetTagItem("http.response.status_code"));
        Assert.Equal(ActivityStatusCode.Unset, server.Status);
    }

    [Fact]
    public async Task No_Grpc_Endpoint_No_Warning()
    {
        await using var web = await StartAsync();
        await RawGetAsync(web.Port, "/orders");
        Assert.Empty(GrpcWarnings(web));
    }

    [Theory]
    [InlineData("OTEL_TRACES_EXPORTER", "none")]
    [InlineData("OTEL_HELPER_DISABLED_SIGNALS", "traces")]
    [InlineData("OTEL_SDK_DISABLED", "true")]
    public async Task No_Warning_When_Traces_Are_Off(string variable, string value)
    {
        Set(variable, value);
        await using var web = await StartAsync(grpc: true);
        await CallAsync(web, TestGrpcMethods.EchoPing);
        Assert.Empty(GrpcWarnings(web));
    }

    [Fact]
    public async Task The_Grpc_Metadata_Type_Name_Is_The_Pinned_One()
    {
        // [TO VERIFY C11] of design D-NET8, at the pinned Grpc.AspNetCore.Server.
        await using var web = await StartAsync(grpc: true);
        var types = web.App.Services.GetRequiredService<EndpointDataSource>().Endpoints
            .SelectMany(e => e.Metadata).Select(m => m.GetType().FullName).ToList();
        Assert.Contains(GrpcServerTracingCheck.GrpcMethodMetadataType, types);
        Assert.True(GrpcServerTracingCheck.HasGrpcEndpoints(web.App.Services.GetRequiredService<EndpointDataSource>()));
        Assert.False(GrpcServerTracingCheck.HasGrpcEndpoints(null));
        Assert.False(GrpcServerTracingCheck.KeyIsTrue(null));
    }

    // ── keyed DI (N1, DN5) ───────────────────────────────────────────────

    [Fact]
    public async Task In_A_Web_Host_Unkeyed_ActivitySource_Is_AspNetCore_And_Server_Spans_Keep_Its_Scope()
    {
        await using var web = await StartAsync();
        var services = web.App.Services;
        // DN5 (C1b dev M2): unkeyed injection silently resolves to ASP.NET Core's own source.
        Assert.Equal("Microsoft.AspNetCore", services.GetRequiredService<ActivitySource>().Name);
        Assert.Null(services.GetService<Meter>());
        Assert.Equal("c11-web", services.GetRequiredKeyedService<ActivitySource>(OtelHelperKeys.Instrumentation).Name);
        Assert.Equal("c11-web", services.GetRequiredKeyedService<Meter>(OtelHelperKeys.Instrumentation).Name);
        await RawGetAsync(web.Port, "/orders");
        Assert.Equal("Microsoft.AspNetCore", Assert.Single(await SettledServerSpans(web, 1)).Source.Name);
    }

    [Fact]
    public void In_A_Worker_Unkeyed_Instrumentation_Is_Null()
    {
        var builder = Host.CreateApplicationBuilder();
        builder.Services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        builder.Services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.PrometheusMetricsPort = 0;
        });
        using var host = builder.Build();
        Assert.Null(host.Services.GetService<ActivitySource>());
        Assert.Null(host.Services.GetService<Meter>());
        Assert.NotNull(host.Services.GetRequiredKeyedService<ActivitySource>(OtelHelperKeys.Instrumentation));
    }

    // ── meters (N5) and the opt-in view ──────────────────────────────────

    [Fact]
    public void Service_Meter_Children_And_Additional_Meters_Are_Exported()
    {
        var exported = new List<Metric>();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "c11-meters";
            o.PrometheusMetricsPort = 0;
            o.AdditionalMeters.Add("other.lib");
        });
        services.ConfigureOpenTelemetryMeterProvider(b => b.AddInMemoryExporter(exported));
        using var provider = services.BuildServiceProvider();
        var meterProvider = provider.GetRequiredService<MeterProvider>();
        var names = new[] { "c11-meters", "c11-meters.orders", "c11-meters.a.b", "c11-metersx", "c11-meters.", "other.lib", "other.lib.child", "unrelated" };
        var meters = names.Select(n => new Meter(n)).ToList();
        try
        {
            foreach (var meter in meters)
            {
                meter.CreateCounter<long>("c11.count").Add(1);
            }

            meterProvider.ForceFlush();
            var seen = exported.Where(m => m.Name == "c11.count").Select(m => m.MeterName).Distinct().OrderBy(n => n, StringComparer.Ordinal).ToArray();
            // "c11-meters." too: the SDK's wildcard ".*" matches an empty rest.
            Assert.Equal(new[] { "c11-meters", "c11-meters.", "c11-meters.a.b", "c11-meters.orders", "other.lib" }, seen);
        }
        finally
        {
            meters.ForEach(m => m.Dispose());
        }
    }

    [Fact]
    public void Idle_Metrics_Drop_The_Service_Meter_Children_Too()
    {
        var resolved = TestDiagnostics.ResolveQuiet(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "svc";
        });
        Assert.True(MetricsSetup.IsHelperInstrumentation("svc.orders", resolved));
        Assert.True(MetricsSetup.IsHelperInstrumentation("svc", resolved));
        Assert.False(MetricsSetup.IsHelperInstrumentation("svcx", resolved));
        Assert.True(MetricsSetup.IsHelperInstrumentation("svc.", resolved));
    }

    [Theory]
    [InlineData("svc.orders", true)]
    [InlineData("svc", true)]
    [InlineData("svcx", false)]
    [InlineData("other", false)]
    public void Quiet_Meter_Notices_Cover_The_Service_Meter_Children(string meter, bool quiet)
        => Assert.Equal(quiet, SdkTextSanitizer.IsQuietMeterNotice("MetricInstrumentIgnored", new object?[] { "x", meter, "Instrument belongs to a Meter not subscribed by the provider." }, new[] { "svc", "svc.*" }));

    [Theory]
    [InlineData("MetricInstrumentIgnored", "Private.InternalDiagnostics.System.Net.Quic.MsQuic", "Instrument belongs to a Meter not subscribed by the provider.", true)]
    [InlineData("MetricInstrumentIgnored", "Private.InternalDiagnostics.Other", "Instrument belongs to a Meter not subscribed by the provider.", true)]
    [InlineData("MetricInstrumentIgnored", "Private.InternalDiagnostics.System.Net.Quic.MsQuic", "Instrument name is invalid.", false)]
    [InlineData("MetricInstrumentIgnored", "System.Net.Quic", "Instrument belongs to a Meter not subscribed by the provider.", false)]
    [InlineData("MetricInstrumentIgnored", "Private.InternalDiagnosticsX", "Instrument belongs to a Meter not subscribed by the provider.", false)]
    [InlineData("OtherEvent", "Private.InternalDiagnostics.System.Net.Quic.MsQuic", "Instrument belongs to a Meter not subscribed by the provider.", false)]
    public void Runtime_Private_Meter_Not_Subscribed_Notices_Are_Noise(string eventName, string meter, string reason, bool noise)
    {
        // C14c fix round 1: the .NET 10 runtime creates msquic.* instruments on
        // Private.InternalDiagnostics.System.Net.Quic.MsQuic at start (32 SDK warnings, measured).
        Assert.Equal(noise, SdkTextSanitizer.IsPrivateRuntimeMeterNotice(eventName, new object?[] { "msquic.connection.active", meter, reason }));
        Assert.False(SdkTextSanitizer.IsPrivateRuntimeMeterNotice(eventName, new object?[] { "x", meter }));
    }

    private static readonly string[] OptInSeries =
    {
        "http.server.active_requests", "http.client.active_requests", "http.client.open_connections", "http.client.request.time_in_queue",
    };

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task Http_Opt_In_Metrics_Switch_Controls_The_Framework_Series(bool on)
    {
        await using var web = await StartAsync(o => o.HttpOptInMetrics = on);
        using (var client = new HttpClient())
        {
            await client.GetAsync($"http://127.0.0.1:{web.Port}/orders");
        }

        var names = web.MetricNames();
        Assert.Contains("http.server.request.duration", names);
        Assert.Contains("http.client.request.duration", names);
        foreach (var series in OptInSeries)
        {
            Assert.Equal(on, names.Contains(series));
        }

        Assert.Equal(on, web.Diagnostics.Lines.Contains(
            "otelhelper: INFO HTTP opt-in metrics on: ASP.NET Core and System.Net.Http record the active requests and the client connection metrics, and have no body-size instruments"));
    }

    [Fact]
    public async Task An_Application_Instrument_With_An_Opt_In_Name_Is_Kept()
    {
        await using var web = await StartAsync(o => o.HttpOptInMetrics = false);
        web.App.Services.GetRequiredKeyedService<Meter>(OtelHelperKeys.Instrumentation).CreateUpDownCounter<long>("http.server.active_requests").Add(1);
        await RawGetAsync(web.Port, "/orders");
        web.MetricNames();
        lock (web.Metrics)
        {
            Assert.Equal(new[] { "c11-web" }, web.Metrics.Where(m => m.Name == "http.server.active_requests").Select(m => m.MeterName).Distinct().ToArray());
        }
    }

    [Fact]
    public async Task Disabled_Metrics_Still_Drop_A_Switched_On_Series()
    {
        await using var web = await StartAsync(o =>
        {
            o.HttpOptInMetrics = true;
            o.DisabledMetrics = "http.server.active_*";
        });
        await RawGetAsync(web.Port, "/orders");
        var names = web.MetricNames();
        Assert.DoesNotContain("http.server.active_requests", names);
        Assert.Contains("http.server.request.duration", names);
    }

    // ── test gRPC service (raw-byte methods, no protoc) ──────────────────

    public sealed class TestGrpcService
    {
    }

    private sealed class TestGrpcMethods : IServiceMethodProvider<TestGrpcService>
    {
        private static readonly Marshaller<byte[]> Raw = Marshallers.Create(static b => b, static b => b);
        private static readonly byte[] Serving = { 0x08, 0x01 };

        public static readonly Method<byte[], byte[]> HealthCheck = new(MethodType.Unary, "grpc.health.v1.Health", "Check", Raw, Raw);
        public static readonly Method<byte[], byte[]> HealthWatch = new(MethodType.ServerStreaming, "grpc.health.v1.Health", "Watch", Raw, Raw);
        public static readonly Method<byte[], byte[]> EchoPing = new(MethodType.Unary, "parity.v1.Echo", "Ping", Raw, Raw);
        public static readonly Method<byte[], byte[]> EchoNotFound = new(MethodType.Unary, "parity.v1.Echo", "NotFound", Raw, Raw);

        public void OnServiceMethodDiscovery(ServiceMethodProviderContext<TestGrpcService> context)
        {
            context.AddUnaryMethod(HealthCheck, Array.Empty<object>(), (_, _, _) => Task.FromResult(Serving));
            context.AddServerStreamingMethod(HealthWatch, Array.Empty<object>(), (_, _, stream, _) => stream.WriteAsync(Serving));
            context.AddUnaryMethod(EchoPing, Array.Empty<object>(), (_, _, _) => Task.FromResult(Serving));
            context.AddUnaryMethod(EchoNotFound, Array.Empty<object>(),
                (_, _, _) => throw new RpcException(new Grpc.Core.Status(Grpc.Core.StatusCode.NotFound, "not found")));
        }
    }
}
