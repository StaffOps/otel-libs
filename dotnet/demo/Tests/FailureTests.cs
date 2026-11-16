using System.Diagnostics;
using Microsoft.Extensions.Logging;

namespace StaffOps.OtelLibs.Demo.Tests;

public class FailureTests
{
    [Fact]
    public void Record_PutsTheTypeOnTheSpanAndTheDetailInALogInsideIt()
    {
        using var spans = new SpanCollector();
        var logger = new RecordingLogger<FailureTests>();
        var error = new OrderRejectedError("amount over limit rej-r1");

        using (var span = DemoTelemetry.Source.StartActivity("demo.order.process"))
        {
            Failure.Record(span, error, logger, ("order.id", "ord-3"));
        }

        var recorded = spans.Single("demo.order.process");
        Assert.Equal(ActivityStatusCode.Error, recorded.Status);
        Assert.Null(recorded.StatusDescription);
        Assert.Equal("OrderRejectedError", recorded.GetTagItem("error.type"));
        var ev = Assert.Single(recorded.Events);
        Assert.Equal("exception", ev.Name);
        var tag = Assert.Single(ev.Tags);
        Assert.Equal(("exception.type", (object?)"OrderRejectedError"), (tag.Key, tag.Value));
        Assert.DoesNotContain(recorded.Tags, t => t.Value?.Contains("rej-r1") == true);

        var log = Assert.Single(logger.Entries);
        Assert.Equal(LogLevel.Error, log.Level);
        Assert.Equal("amount over limit rej-r1", log.Message);
        Assert.Same(error, log.Exception);
        Assert.Equal("ord-3", log.Attributes["order.id"]);
        Assert.Equal(recorded.TraceId.ToHexString(), log.TraceId);
        Assert.Equal(recorded.SpanId.ToHexString(), log.SpanId);
    }

    [Fact]
    public void Record_WithoutASpanStillLogs()
    {
        var logger = new RecordingLogger<FailureTests>();
        Failure.Record(null, new DemoUnhandledError("boom unh-r1"), logger);
        Assert.Equal("boom unh-r1", Assert.Single(logger.Entries).Message);
        Assert.Throws<ArgumentNullException>(() => Failure.Record(null, null!, logger));
        Assert.Throws<ArgumentNullException>(() => Failure.Record(null, new DemoUnhandledError("x"), null!));
    }

    [Fact]
    public void Propagate_SetsErrorWithoutEventOrDescription()
    {
        using var spans = new SpanCollector();
        using (var span = DemoTelemetry.Source.StartActivity("demo.order.accept"))
        {
            Failure.Propagate(span);
        }

        var recorded = spans.Single("demo.order.accept");
        Assert.Equal(ActivityStatusCode.Error, recorded.Status);
        Assert.Null(recorded.StatusDescription);
        Assert.Empty(recorded.Events);
        Failure.Propagate(null);
    }

    [Fact]
    public void ErrorTypes_AreThePinnedLiterals()
    {
        Assert.Equal("OrderRejectedError", Failure.ErrorType(new OrderRejectedError("m")));
        Assert.Equal("OrderExistsError", Failure.ErrorType(new OrderExistsError("m", new InvalidOperationException())));
        Assert.Equal("DemoUnhandledError", Failure.ErrorType(new DemoUnhandledError("m")));
    }

    [Fact]
    public void DemoLog_WritesAFixedBodyWithNamedAttributes()
    {
        var logger = new RecordingLogger<FailureTests>();
        DemoLog.Write(logger, LogLevel.Information, "order processed", null, ("order.id", "ok-1"), ("demo.scenario", "ok"));
        var log = Assert.Single(logger.Entries);
        Assert.Equal("order processed", log.Message);
        Assert.Equal(2, log.Attributes.Count);
        Assert.Equal("ok", log.Attributes["demo.scenario"]);
        Assert.Throws<ArgumentNullException>(() => DemoLog.Write(null!, LogLevel.Information, "x", null));
    }
}
