namespace StaffOps.OtelLibs.Demo.Tests;

public class DemoSettingsTests
{
    private static Func<string, string?> Env(params (string Key, string Value)[] vars)
    {
        var map = vars.ToDictionary(v => v.Key, v => v.Value);
        return name => map.GetValueOrDefault(name);
    }

    [Fact]
    public void Get_TrimsAndFallsBackOnBlank()
    {
        Assert.Equal("x", DemoSettings.Get("A", "d", Env(("A", " x "))));
        Assert.Equal("d", DemoSettings.Get("A", "d", Env(("A", "  "))));
        Assert.Equal("d", DemoSettings.Get("A", "d", Env()));
    }

    [Theory]
    [InlineData("2.5", 2.5)]
    [InlineData("0", 5)]
    [InlineData("-1", 5)]
    [InlineData("abc", 5)]
    [InlineData("", 5)]
    public void Seconds_AcceptsPositiveNumbersOnly(string raw, double expected) =>
        Assert.Equal(TimeSpan.FromSeconds(expected), DemoSettings.Seconds("S", 5, Env(("S", raw))));

    [Theory]
    [InlineData("9090", 9090)]
    [InlineData("0", 8080)]
    [InlineData("70000", 8080)]
    [InlineData("x", 8080)]
    public void Port_AcceptsValidPortsOnly(string raw, int expected) =>
        Assert.Equal(expected, DemoSettings.Port("P", 8080, Env(("P", raw))));

    [Fact]
    public void ReadSecretFile_TrimsTheTrailingNewline()
    {
        var path = Path.GetTempFileName();
        try
        {
            File.WriteAllText(path, "s3cr3t-value\n");
            Assert.Equal("s3cr3t-value", DemoSettings.ReadSecretFile("PW_FILE", Env(("PW_FILE", path))));
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void ReadSecretFile_FailsWithoutRevealingAnything()
    {
        var unset = Assert.Throws<InvalidOperationException>(() => DemoSettings.ReadSecretFile("PW_FILE", Env()));
        Assert.Equal("PW_FILE is not set", unset.Message);

        var missing = Assert.Throws<InvalidOperationException>(() =>
            DemoSettings.ReadSecretFile("PW_FILE", Env(("PW_FILE", "/nonexistent/secret-path"))));
        Assert.DoesNotContain("secret-path", missing.Message);

        var path = Path.GetTempFileName();
        try
        {
            File.WriteAllText(path, "\n");
            var empty = Assert.Throws<InvalidOperationException>(() => DemoSettings.ReadSecretFile("PW_FILE", Env(("PW_FILE", path))));
            Assert.Equal("PW_FILE: the file is empty", empty.Message);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Theory]
    [InlineData("demo-redis:6380", "demo-redis", 6380)]
    [InlineData("demo-redis", "demo-redis", 6379)]
    [InlineData("demo-redis:bad", "demo-redis", 6379)]
    [InlineData("[::1]:7000", "::1", 7000)]
    [InlineData("[::1]", "::1", 6379)]
    [InlineData("::1", "::1", 6379)]
    [InlineData(" 10.0.0.1:1 ", "10.0.0.1", 1)]
    public void HostPort_SplitsHostAndPort(string address, string host, int port) =>
        Assert.Equal((host, port), DemoSettings.HostPort(address, 6379));

    [Theory]
    [InlineData(null, "local")]
    [InlineData("", "local")]
    [InlineData("deployment.environment.name=dev", "local")]
    [InlineData("deployment.environment.name=dev,otel_libs.run=20261116t1812", "20261116t1812")]
    [InlineData("otel_libs.run = r%2D1 ,x=y", "r-1")]
    [InlineData("otel_libs.run=", "local")]
    [InlineData("otel_libs.run=%zz,otel_libs.run=second", "%zz")]
    [InlineData("broken,otel_libs.run=ok1", "ok1")]
    public void RunId_ReadsOtelLibsRunFromResourceAttributes(string? attributes, string expected) =>
        Assert.Equal(expected, DemoSettings.RunId(attributes));

    [Fact]
    public void ApiBase_EndsWithOneSlashAndRejectsOtherSchemes()
    {
        Assert.Equal("http://api:8080/", DemoSettings.ApiBase("http://api:8080").ToString());
        Assert.Equal("http://api:8080/", DemoSettings.ApiBase("http://api:8080//").ToString());
        Assert.Equal(new Uri("http://api:8080/orders"), new Uri(DemoSettings.ApiBase("http://api:8080"), "orders"));
        Assert.Throws<InvalidOperationException>(() => DemoSettings.ApiBase("ftp://api"));
        Assert.Throws<InvalidOperationException>(() => DemoSettings.ApiBase("api:8080/x y"));
    }

    [Fact]
    public void BackendUri_AddsPlaintextSchemeWhenMissing()
    {
        Assert.Equal(new Uri("http://backend:50051"), DemoSettings.BackendUri("backend:50051"));
        Assert.Equal(new Uri("https://backend:443"), DemoSettings.BackendUri("https://backend:443"));
        Assert.Throws<InvalidOperationException>(() => DemoSettings.BackendUri("ftp://backend"));
    }

    [Theory]
    [InlineData("true", true)]
    [InlineData("TRUE", true)]
    [InlineData("false", false)]
    [InlineData("1", false)]
    public void SuppressFrameworkExceptions_OnlyForTrue(string raw, bool expected) =>
        Assert.Equal(expected, DemoSettings.SuppressFrameworkExceptions(Env(("DEMO_SUPPRESS_FRAMEWORK_EXCEPTIONS", raw))));

    [Fact]
    public void Keys_ArePerLanguage()
    {
        Assert.Equal("demo:dotnet:orders", DemoSettings.StreamKey);
        Assert.Equal("demo:dotnet:quote:alice", DemoSettings.QuoteKey("alice"));
        Assert.Equal("demo-workers", DemoSettings.ConsumerGroup);
    }

    [Fact]
    public void RedisOptions_CarryEndpointAndDoNotAbortOnConnectFail()
    {
        var options = DemoRedis.Options("demo-redis:6380", "pw");
        Assert.False(options.AbortOnConnectFail);
        Assert.Equal("pw", options.Password);
        var endpoint = Assert.IsType<System.Net.DnsEndPoint>(Assert.Single(options.EndPoints));
        Assert.Equal(("demo-redis", 6380), (endpoint.Host, endpoint.Port));
        Assert.DoesNotContain("pw", options.ToString(includePassword: false));
        Assert.Equal(("demo-redis", 6379), DemoRedis.ServerOf("demo-redis"));
    }
}
