using System.Diagnostics;
using Grpc.Core;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Http.HttpResults;
using Microsoft.Extensions.Logging;
using StaffOps.OtelLibs.Demo.Api;

namespace StaffOps.OtelLibs.Demo.Tests;

public class ApiTests
{
    [Theory]
    [InlineData(StatusCode.FailedPrecondition, 422)]
    [InlineData(StatusCode.AlreadyExists, 409)]
    [InlineData(StatusCode.Unavailable, 502)]
    [InlineData(StatusCode.Internal, 502)]
    [InlineData(StatusCode.InvalidArgument, 502)]
    public void HttpStatusFor_MapsBusinessCodesAndEverythingElseTo502(StatusCode code, int expected) =>
        Assert.Equal(expected, OrderEndpoint.HttpStatusFor(code));

    [Fact]
    public void StreamFields_PutPropagationFirstThenTheOrder()
    {
        var fields = OrderEndpoint.StreamFields(new Dictionary<string, string> { ["traceparent"] = "00-x-y-01" }, "ok-1", Scenario.Ok);

        Assert.Equal(["traceparent", "order_id", "scenario"], fields.Select(f => f.Name.ToString()));
        Assert.Equal(("ok-1", "ok"), (fields[1].Value.ToString(), fields[2].Value.ToString()));
    }

    [Fact]
    public async Task HandleAsync_RejectsAnIncompleteBodyBeforeAnySpan()
    {
        using var spans = new SpanCollector();
        var endpoint = new OrderEndpoint(null!, null!, new RecordingLogger<OrderEndpoint>(), new OrderEndpoint.Settings("r1", "redis", 6379));

        var result = await endpoint.HandleAsync(new OrderRequest("", "bob", 1, Scenario.Ok), CancellationToken.None);
        Assert.Equal(StatusCodes.Status400BadRequest, Assert.IsAssignableFrom<IStatusCodeHttpResult>(result).StatusCode);
        Assert.Equal(StatusCodes.Status400BadRequest,
            Assert.IsAssignableFrom<IStatusCodeHttpResult>(await endpoint.HandleAsync(null, CancellationToken.None)).StatusCode);
        Assert.Equal(StatusCodes.Status400BadRequest, Assert.IsAssignableFrom<IStatusCodeHttpResult>(
            await endpoint.HandleAsync(new OrderRequest("ord-1", "bob", 1, "bogus"), CancellationToken.None)).StatusCode);
        Assert.Empty(spans.Started);
    }

    [Fact]
    public async Task HandleAsync_Unhandled_RecordsInAcceptAndRethrowsBeforeTheCache()
    {
        using var spans = new SpanCollector();
        var logger = new RecordingLogger<OrderEndpoint>();
        // No Redis and no backend: the unhandled path must not reach either.
        var endpoint = new OrderEndpoint(null!, null!, logger, new OrderEndpoint.Settings("r1", "redis", 6379));

        var thrown = await Assert.ThrowsAsync<DemoUnhandledError>(() =>
            endpoint.HandleAsync(new OrderRequest("ord-9", "carol", 19, Scenario.Unhandled), CancellationToken.None));
        Assert.Equal("boom unh-r1", thrown.Message);

        var accept = spans.Single("demo.order.accept");
        Assert.Equal(ActivityStatusCode.Error, accept.Status);
        Assert.Null(accept.StatusDescription);
        Assert.Equal("DemoUnhandledError", accept.GetTagItem("error.type"));
        Assert.Empty(accept.Events);
        var log = Assert.Single(logger.Entries);
        Assert.Equal((LogLevel.Error, "boom unh-r1"), (log.Level, log.Message));
        Assert.Empty(log.Attributes); // no demo.scenario / order.id (DE-D8 note of 2026-11-17)
        Assert.Equal(accept.SpanId.ToHexString(), log.SpanId);
    }
}
