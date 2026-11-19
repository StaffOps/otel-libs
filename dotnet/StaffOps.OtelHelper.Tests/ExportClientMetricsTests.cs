using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Net;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Exporters;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// C19 fix round 1 (observability MEDIUM 1; design D-NET2 note of 2026-11-12): the helper's own OTLP
/// exports are not recorded on the <c>System.Net.Http</c> meter the helper exports for the application.
/// Before the fix, every export POST was an <c>http.client.request.duration</c> point with the
/// Collector's <c>server.address</c> (measured on both protocols).
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public class ExportClientMetricsTests
{
    [Theory]
    [InlineData(TelemetryOptions.ProtocolHttpProtobuf)]
    [InlineData(TelemetryOptions.ProtocolGrpc)]
    public async Task Helper_Exports_Record_No_System_Net_Http_Point(string protocol)
    {
        using var receiver = new Receiver();
        using var recorder = new HttpClientMeasurements(receiver.Port);

        // Positive control: an application request to the same port is recorded.
        using (var http = new HttpClient())
        {
            await http.GetAsync(receiver.Url + "/control");
        }

        recorder.Listener.RecordObservableInstruments();
        Assert.NotEqual(0, recorder.Count);
        recorder.Reset();

        var diagnostics = new TestDiagnostics();
        using var provider = TestPipelines.Build(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "c19-export-meter";
            // Every signal to the receiver: the MeterProvider is real and subscribes System.Net.Http.
            o.OtelCollectorEndpoint = receiver.Url;
            o.OtlpProtocol = protocol;
            o.ExportTimeoutMs = 2000;
            o.PrometheusMetricsPort = 0;
            o.HttpOptInMetrics = true;
        }, diagnostics);
        var tracer = provider.GetRequiredService<TracerProvider>();
        using (var source = new ActivitySource("c19-export-meter"))
        using (source.StartActivity("export-me"))
        {
        }

        // The export runs (http/protobuf answered 200; gRPC against an HTTP/1.1 listener fails, which
        // the runtime records as a failed request just the same before the fix).
        tracer.ForceFlush(5000);
        if (protocol == TelemetryOptions.ProtocolHttpProtobuf)
        {
            Assert.NotEqual(0, receiver.Requests);
        }

        Assert.Equal(0, recorder.Count);

        // Positive control again, with the helper's providers built: application requests still count.
        using (var http = new HttpClient())
        {
            await http.GetAsync(receiver.Url + "/control-after");
        }

        Assert.NotEqual(0, recorder.Count);

        // The SDK's "Meter not subscribed" notices for the private meter are not forwarded (ten
        // `otelhelper: WARN sdk` lines per start before the noise rule, measured).
        provider.GetRequiredService<OpenTelemetry.Metrics.MeterProvider>();
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains(ExportClientMeterFactory.MeterName, StringComparison.Ordinal));
    }

    [Fact]
    public void The_Factory_Names_Every_Meter_Privately_And_Disposes_Them()
    {
        var factory = new ExportClientMeterFactory();
        var meter = factory.Create(new MeterOptions("System.Net.Http"));
        Assert.Equal(ExportClientMeterFactory.MeterName, meter.Name);
        factory.Dispose();
        factory.Dispose();
        var late = factory.Create(new MeterOptions("System.Net.Http"));
        Assert.Equal(ExportClientMeterFactory.MeterName, late.Name);
    }

    /// <summary>Counts System.Net.Http client measurements whose server.port is the receiver's.</summary>
    private sealed class HttpClientMeasurements : IDisposable
    {
        private readonly int _port;
        private int _count;

        public HttpClientMeasurements(int port)
        {
            _port = port;
            Listener = new MeterListener
            {
                InstrumentPublished = (instrument, listener) =>
                {
                    if (instrument.Meter.Name == "System.Net.Http" && instrument.Name.StartsWith("http.client.", StringComparison.Ordinal))
                    {
                        listener.EnableMeasurementEvents(instrument);
                    }
                },
            };
            Listener.SetMeasurementEventCallback<double>((_, _, tags, _) => Record(tags));
            Listener.SetMeasurementEventCallback<long>((_, _, tags, _) => Record(tags));
            Listener.SetMeasurementEventCallback<int>((_, _, tags, _) => Record(tags));
            Listener.Start();
        }

        public MeterListener Listener { get; }

        public int Count => Volatile.Read(ref _count);

        public void Reset() => Interlocked.Exchange(ref _count, 0);

        public void Dispose() => Listener.Dispose();

        private void Record(ReadOnlySpan<KeyValuePair<string, object?>> tags)
        {
            foreach (var tag in tags)
            {
                if (tag.Key == "server.port" && Convert.ToInt32(tag.Value, System.Globalization.CultureInfo.InvariantCulture) == _port)
                {
                    Interlocked.Increment(ref _count);
                    return;
                }
            }
        }
    }

    /// <summary>An HTTP/1.1 receiver answering 200 to everything.</summary>
    private sealed class Receiver : IDisposable
    {
        private readonly HttpListener _listener = new();
        private int _requests;

        public Receiver()
        {
            Port = TestPorts.FreeTcpPort();
            Url = $"http://127.0.0.1:{Port}";
            _listener.Prefixes.Add(Url + "/");
            _listener.Start();
            _ = Task.Run(async () =>
            {
                while (_listener.IsListening)
                {
                    try
                    {
                        var context = await _listener.GetContextAsync();
                        await context.Request.InputStream.CopyToAsync(Stream.Null);
                        Interlocked.Increment(ref _requests);
                        context.Response.StatusCode = 200;
                        context.Response.ContentLength64 = 0;
                        context.Response.Close();
                    }
                    catch (Exception ex) when (ex is HttpListenerException or ObjectDisposedException or InvalidOperationException)
                    {
                        return;
                    }
                }
            });
        }

        public int Port { get; }

        public string Url { get; }

        public int Requests => Volatile.Read(ref _requests);

        public void Dispose() => _listener.Close();
    }
}
