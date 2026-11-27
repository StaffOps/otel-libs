using StackExchange.Redis;

namespace StaffOps.OtelLibs.Demo;

/// <summary>The Redis client settings of the api and the worker (DE-D4, DE-D13).</summary>
public static class DemoRedis
{
    /// <summary>
    /// Client options for <paramref name="address"/> (<c>host[:port]</c>) and the password read from
    /// its file. The app keeps running while Redis is down (<c>AbortOnConnectFail = false</c>); the
    /// options object holds the password, so it is never logged.
    /// </summary>
    public static ConfigurationOptions Options(string address, string password)
    {
        var (host, port) = DemoSettings.HostPort(address, 6379);
        var options = new ConfigurationOptions
        {
            Password = password,
            AbortOnConnectFail = false,
        };
        options.EndPoints.Add(host, port);
        return options;
    }

    /// <summary>Connects with the settings of the environment (<c>DEMO_REDIS_ADDR</c>, <c>DEMO_REDIS_PASSWORD_FILE</c>).</summary>
    [System.Diagnostics.CodeAnalysis.ExcludeFromCodeCoverage(Justification = "Opens a connection: exercised by the local smoke and the cluster run (DE6).")]
    public static IConnectionMultiplexer ConnectFromEnvironment()
    {
        var options = Options(DemoSettings.Get("DEMO_REDIS_ADDR", "demo-redis:6379"),
            DemoSettings.ReadSecretFile("DEMO_REDIS_PASSWORD_FILE"));
        return ConnectionMultiplexer.Connect(options);
    }

    /// <summary>The <c>server.address</c> / <c>server.port</c> of the messaging spans (DE-D6).</summary>
    public static (string Host, int Port) ServerOf(string address) => DemoSettings.HostPort(address, 6379);
}
