namespace StaffOps.OtelLibs.Demo.Tests;

public class ScenarioCycleTests
{
    [Theory]
    [InlineData(1, Scenario.Ok)]
    [InlineData(2, Scenario.Ok)]
    [InlineData(3, Scenario.BusinessError)]
    [InlineData(4, Scenario.Ok)]
    [InlineData(5, Scenario.Ok)]
    [InlineData(6, Scenario.DbError)]
    [InlineData(7, Scenario.Ok)]
    [InlineData(8, Scenario.Ok)]
    [InlineData(9, Scenario.Unhandled)]
    [InlineData(10, Scenario.Ok)]
    [InlineData(13, Scenario.BusinessError)]
    [InlineData(1006, Scenario.DbError)]
    [InlineData(99, Scenario.Unhandled)]
    public void At_FollowsTheFixedCycleOfTen(long seq, string expected) => Assert.Equal(expected, ScenarioCycle.At(seq));

    [Fact]
    public void At_EveryWindowOfTenHasSevenOkAndOneOfEachError()
    {
        var counts = Enumerable.Range(41, 10).Select(i => ScenarioCycle.At(i)).GroupBy(s => s).ToDictionary(g => g.Key, g => g.Count());
        Assert.Equal(7, counts[Scenario.Ok]);
        Assert.Equal(1, counts[Scenario.BusinessError]);
        Assert.Equal(1, counts[Scenario.DbError]);
        Assert.Equal(1, counts[Scenario.Unhandled]);
    }

    [Theory]
    [InlineData(0)]
    [InlineData(-5)]
    public void At_RejectsNonPositiveSequence(long seq) => Assert.Throws<ArgumentOutOfRangeException>(() => ScenarioCycle.At(seq));

    [Fact]
    public void Plan_OkIdsAreBoundedAndOnlyBusinessErrorExceedsTheLimit()
    {
        for (long seq = 1; seq <= 1200; seq++)
        {
            var order = ScenarioCycle.Plan(seq);
            Assert.Equal(seq, order.Seq);
            Assert.Equal(ScenarioCycle.At(seq), order.Scenario);
            Assert.Equal(order.Scenario == Scenario.BusinessError, order.Amount > ScenarioCycle.AmountLimit);
            var prefix = order.Scenario == Scenario.Ok ? "ok-" : "ord-";
            Assert.StartsWith(prefix, order.OrderId);
            Assert.InRange(int.Parse(order.OrderId[prefix.Length..]), 0, ScenarioCycle.OkIdSpace - 1);
        }
    }

    [Fact]
    public void Plan_CyclesThreeCustomersAndNeverUsesRunTokenPrefixes()
    {
        var customers = Enumerable.Range(1, 30).Select(i => ScenarioCycle.Plan(i).Customer).Distinct().ToList();
        Assert.Equal(3, customers.Count);
        Assert.All(Enumerable.Range(1, 30).Select(i => ScenarioCycle.Plan(i).OrderId),
            id => Assert.DoesNotMatch("^(dup|rej|unh)-", id));
    }

    [Fact]
    public void OrderRequest_FromCopiesThePlan()
    {
        var plan = ScenarioCycle.Plan(3);
        var body = OrderRequest.From(plan);
        Assert.Equal((plan.OrderId, plan.Customer, plan.Amount, plan.Scenario), (body.OrderId, body.Customer, body.Amount, body.Scenario));
        var json = System.Text.Json.JsonSerializer.Serialize(body);
        Assert.Contains("\"order_id\":", json);
        Assert.Contains("\"scenario\":\"business_error\"", json);
    }
}
