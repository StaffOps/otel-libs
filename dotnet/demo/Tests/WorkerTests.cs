using System.Diagnostics;
using System.Net;
using Microsoft.Extensions.Logging;
using StackExchange.Redis;
using StaffOps.OtelLibs.Demo.Worker;

namespace StaffOps.OtelLibs.Demo.Tests;

public class WorkerTests
{
    private static readonly Consumer.Settings ConsumerSettings = new("pod-1", TimeSpan.FromSeconds(2), "demo-redis", 6379);

    [Fact]
    public void Fields_KeepTheFirstValueOfARepeatedName()
    {
        var entry = new StreamEntry("1-0", [new NameValueEntry("a", "1"), new NameValueEntry("a", "2"), new NameValueEntry("b", "3")]);
        Assert.Equal(new Dictionary<string, string> { ["a"] = "1", ["b"] = "3" }, Consumer.Fields(entry));
    }

    [Fact]
    public void StartProcessSpan_UsesTheProducerAsParentAndLinkOutsideOtherSpans()
    {
        using var spans = new SpanCollector();
        var producer = new ActivityContext(ActivityTraceId.CreateRandom(), ActivitySpanId.CreateRandom(), ActivityTraceFlags.Recorded);
        var fields = MessageCarrier.Inject(producer, default);
        var consumer = new Consumer(null!, new RecordingLogger<Consumer>(), ConsumerSettings);

        using var unrelated = DemoTelemetry.Source.StartActivity("unrelated");
        using (var span = consumer.StartProcessSpan("1-0", fields))
        {
            Assert.NotNull(span);
            Assert.Equal(ActivityKind.Consumer, span.Kind);
            Assert.Equal("process demo:dotnet:orders", span.OperationName);
            Assert.Equal(producer.TraceId, span.TraceId);
            Assert.Equal(producer.SpanId, span.ParentSpanId);
            var link = Assert.Single(span.Links);
            Assert.Equal((producer.TraceId, producer.SpanId), (link.Context.TraceId, link.Context.SpanId));
            Assert.Equal("1-0", span.GetTagItem("messaging.message.id"));
            Assert.Equal("demo-workers", span.GetTagItem("messaging.consumer.group.name"));
            Assert.Equal("demo:dotnet:orders", span.GetTagItem("messaging.destination.name"));
            Assert.Equal(6379, span.GetTagItem("server.port"));
        }
    }

    [Fact]
    public void StartProcessSpan_WithoutContextStartsARootWithoutLink()
    {
        using var spans = new SpanCollector();
        var consumer = new Consumer(null!, new RecordingLogger<Consumer>(), ConsumerSettings);
        using var span = consumer.StartProcessSpan("2-0", new Dictionary<string, string>());
        Assert.NotNull(span);
        Assert.Equal(default, span.ParentSpanId);
        Assert.Empty(span.Links);
    }

    [Fact]
    public void ConsumerTags_AreTheMessagingAttributeSet()
    {
        var tags = Consumer.ConsumerTags("3-0", "h", 1).ToDictionary(t => t.Key, t => t.Value);
        Assert.Equal("redis", tags["messaging.system"]);
        Assert.Equal("process", tags["messaging.operation.name"]);
        Assert.Equal("process", tags["messaging.operation.type"]);
        Assert.Equal(8, tags.Count);
    }

    [Theory]
    [InlineData(1, HttpStatusCode.Created, null)]
    [InlineData(3, HttpStatusCode.UnprocessableEntity, "422")]
    [InlineData(6, HttpStatusCode.Conflict, "409")]
    [InlineData(9, HttpStatusCode.InternalServerError, "500")]
    public async Task DriveAsync_MarksNon2xxAnswersOnTheRootSpan(long seq, HttpStatusCode status, string? errorType)
    {
        using var spans = new SpanCollector();
        var logger = new RecordingLogger<Driver>();
        var handler = new StubHandler(_ => new HttpResponseMessage(status));
        var driver = new Driver(new HttpClient(handler) { BaseAddress = new Uri("http://api:8080/") }, logger, TimeSpan.FromSeconds(5));

        using (DemoTelemetry.Source.StartActivity("outer"))
        {
            await driver.DriveAsync(seq, CancellationToken.None);
        }

        var drive = spans.Single("demo.drive");
        Assert.Equal(default, drive.ParentSpanId);
        Assert.Equal(ScenarioCycle.At(seq), drive.GetTagItem("demo.scenario"));
        Assert.Equal(new Uri("http://api:8080/orders"), handler.Requests.Single().RequestUri);
        Assert.Contains("\"scenario\":\"" + ScenarioCycle.At(seq) + "\"", handler.Bodies.Single());
        Assert.Empty(drive.Events);
        if (errorType is null)
        {
            Assert.Equal(ActivityStatusCode.Unset, drive.Status);
            Assert.Equal("ok", drive.GetTagItem("demo.outcome"));
            Assert.Empty(logger.Entries);
        }
        else
        {
            Assert.Equal(ActivityStatusCode.Error, drive.Status);
            Assert.Equal(errorType, drive.GetTagItem("error.type"));
            Assert.Equal("error", drive.GetTagItem("demo.outcome"));
            var log = Assert.Single(logger.Entries);
            Assert.Equal((LogLevel.Warning, "order request failed"), (log.Level, log.Message));
            Assert.Equal((int)status, log.Attributes["http.response.status_code"]);
            Assert.Equal(drive.SpanId.ToHexString(), log.SpanId);
        }
    }

    [Fact]
    public async Task DriveAsync_TransportFailureLogsTheDetailNotTheSpan()
    {
        using var spans = new SpanCollector();
        var logger = new RecordingLogger<Driver>();
        var handler = new StubHandler(_ => throw new HttpRequestException("connection refused (api:8080)"));
        var driver = new Driver(new HttpClient(handler) { BaseAddress = new Uri("http://api:8080/") }, logger, TimeSpan.FromSeconds(5));

        await driver.DriveAsync(1, CancellationToken.None);

        var drive = spans.Single("demo.drive");
        Assert.Equal(ActivityStatusCode.Error, drive.Status);
        Assert.Null(drive.StatusDescription);
        Assert.Equal("HttpRequestException", drive.GetTagItem("error.type"));
        Assert.Empty(drive.Events);
        Assert.IsType<HttpRequestException>(Assert.Single(logger.Entries).Exception);
    }

    [Fact]
    public async Task ExecuteAsync_SendsTheFirstRequestImmediatelyAndStopsOnCancel()
    {
        using var spans = new SpanCollector();
        var handler = new StubHandler(_ => new HttpResponseMessage(HttpStatusCode.Created));
        var driver = new Driver(new HttpClient(handler) { BaseAddress = new Uri("http://api:8080/") }, new RecordingLogger<Driver>(), TimeSpan.FromMilliseconds(20));

        await driver.StartAsync(CancellationToken.None);
        for (var i = 0; i < 200 && handler.Requests.Count < 2; i++)
        {
            await Task.Delay(10);
        }

        await driver.StopAsync(CancellationToken.None);
        Assert.True(handler.Requests.Count >= 2);
    }

    private sealed class StubHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) : HttpMessageHandler
    {
        public List<HttpRequestMessage> Requests { get; } = [];
        public List<string> Bodies { get; } = [];

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            lock (Requests)
            {
                Requests.Add(request);
            }

            Bodies.Add(request.Content is null ? "" : await request.Content.ReadAsStringAsync(cancellationToken));
            return respond(request);
        }
    }
}
