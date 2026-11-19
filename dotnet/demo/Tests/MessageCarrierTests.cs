using System.Diagnostics;
using OpenTelemetry;
using OpenTelemetry.Context.Propagation;

namespace StaffOps.OtelLibs.Demo.Tests;

public class MessageCarrierTests
{
    private static readonly TextMapPropagator W3C = new CompositeTextMapPropagator([new TraceContextPropagator(), new BaggagePropagator()]);

    [Fact]
    public void InjectThenExtract_KeepsTheContextAndBaggage()
    {
        var context = new ActivityContext(ActivityTraceId.CreateRandom(), ActivitySpanId.CreateRandom(), ActivityTraceFlags.Recorded, "k=v");
        var baggage = Baggage.Create(new Dictionary<string, string> { ["tenant"] = "a" });

        var fields = MessageCarrier.Inject(context, baggage, W3C);
        Assert.Equal($"00-{context.TraceId}-{context.SpanId}-01", fields["traceparent"]);
        Assert.Equal("k=v", fields["tracestate"]);
        Assert.Equal("tenant=a", fields["baggage"]);

        var extracted = MessageCarrier.Extract(fields, W3C);
        Assert.Equal(context.TraceId, extracted.ActivityContext.TraceId);
        Assert.Equal(context.SpanId, extracted.ActivityContext.SpanId);
        Assert.True(extracted.ActivityContext.IsRemote);
        Assert.Equal("a", extracted.Baggage.GetBaggage("tenant"));
    }

    [Fact]
    public void Extract_WithoutFieldsGivesAnInvalidContext()
    {
        var extracted = MessageCarrier.Extract(new Dictionary<string, string> { ["order_id"] = "ok-1" }, W3C);
        Assert.False(extracted.ActivityContext.IsValid());
        Assert.Throws<ArgumentNullException>(() => MessageCarrier.Extract(null!, W3C));
    }

    [Fact]
    public void DefaultPropagator_IsUsedWhenNoneIsGiven()
    {
        using var _ = new SpanCollector(); // sets the W3C default propagator
        var context = new ActivityContext(ActivityTraceId.CreateRandom(), ActivitySpanId.CreateRandom(), ActivityTraceFlags.Recorded);
        var fields = MessageCarrier.Inject(context, default);
        Assert.Equal(context.SpanId, MessageCarrier.Extract(fields).ActivityContext.SpanId);
    }
}
