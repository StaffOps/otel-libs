namespace StaffOps.OtelLibs.Demo;

/// <summary>
/// Settings read from the environment (DR0: every setting from env vars; DE-D13 r3.3 names).
/// Passwords come from files (<c>DEMO_PG_PASSWORD_FILE</c>, <c>DEMO_REDIS_PASSWORD_FILE</c>), read
/// once at start-up and never logged.
/// </summary>
public static class DemoSettings
{
    /// <summary>The language of this demo set: stream and key prefix <c>demo:dotnet:</c>.</summary>
    public const string Lang = "dotnet";

    /// <summary>The Redis stream of this language (DE-D5).</summary>
    public const string StreamKey = "demo:" + Lang + ":orders";

    /// <summary>The consumer group of every language's worker (DE-D5).</summary>
    public const string ConsumerGroup = "demo-workers";

    /// <summary>The cache key of a customer's quote (DE-D4).</summary>
    public static string QuoteKey(string customer) => $"demo:{Lang}:quote:{customer}";

    /// <summary>The value of <paramref name="name"/>, or <paramref name="fallback"/> when unset or blank.</summary>
    public static string Get(string name, string fallback, Func<string, string?>? env = null)
    {
        var value = (env ?? Environment.GetEnvironmentVariable)(name);
        return string.IsNullOrWhiteSpace(value) ? fallback : value.Trim();
    }

    /// <summary>A positive number of seconds from <paramref name="name"/>; <paramref name="fallback"/> when unset or invalid.</summary>
    public static TimeSpan Seconds(string name, double fallback, Func<string, string?>? env = null)
    {
        var raw = Get(name, "", env);
        var seconds = double.TryParse(raw, System.Globalization.NumberStyles.Float,
            System.Globalization.CultureInfo.InvariantCulture, out var parsed) && parsed > 0 ? parsed : fallback;
        return TimeSpan.FromSeconds(seconds);
    }

    /// <summary>A TCP port from <paramref name="name"/>; <paramref name="fallback"/> when unset or invalid.</summary>
    public static int Port(string name, int fallback, Func<string, string?>? env = null)
    {
        var raw = Get(name, "", env);
        return int.TryParse(raw, out var port) && port is > 0 and <= 65535 ? port : fallback;
    }

    /// <summary>
    /// Reads a password from the file named by the variable <paramref name="variable"/> (trailing
    /// newline removed). Throws when the variable is unset or the file is missing or empty; the
    /// message names the variable only, never the content.
    /// </summary>
    public static string ReadSecretFile(string variable, Func<string, string?>? env = null)
    {
        var path = Get(variable, "", env);
        if (path.Length == 0)
        {
            throw new InvalidOperationException($"{variable} is not set");
        }

        if (!File.Exists(path))
        {
            throw new InvalidOperationException($"{variable}: the file does not exist");
        }

        var secret = File.ReadAllText(path).TrimEnd('\r', '\n');
        if (secret.Length == 0)
        {
            throw new InvalidOperationException($"{variable}: the file is empty");
        }

        return secret;
    }

    /// <summary>
    /// Splits <c>host[:port]</c> (an IPv6 literal in brackets, <c>[::1]:6379</c>, is accepted).
    /// A missing or invalid port gives <paramref name="defaultPort"/>.
    /// </summary>
    public static (string Host, int Port) HostPort(string address, int defaultPort)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(address);
        var value = address.Trim();
        if (value.StartsWith('['))
        {
            var close = value.IndexOf(']');
            if (close > 0)
            {
                var host = value[1..close];
                var rest = value[(close + 1)..];
                return (host, rest.StartsWith(':') && int.TryParse(rest[1..], out var p6) && p6 is > 0 and <= 65535 ? p6 : defaultPort);
            }
        }

        var colon = value.LastIndexOf(':');
        if (colon > 0 && value.IndexOf(':') == colon)
        {
            return (value[..colon], int.TryParse(value[(colon + 1)..], out var p) && p is > 0 and <= 65535 ? p : defaultPort);
        }

        return (value, defaultPort);
    }

    /// <summary>
    /// The run id: <c>otel_libs.run</c> from <c>OTEL_RESOURCE_ATTRIBUTES</c> (the demo's only
    /// source, DE-D2), percent-decoded; <c>local</c> when absent. The run tokens of the exception
    /// messages (<c>rej-</c>, <c>dup-</c>, <c>unh-&lt;run&gt;</c>, DE-D8) are built from it.
    /// </summary>
    public static string RunId(string? resourceAttributes)
    {
        if (!string.IsNullOrEmpty(resourceAttributes))
        {
            foreach (var pair in resourceAttributes.Split(','))
            {
                var eq = pair.IndexOf('=');
                if (eq <= 0 || pair[..eq].Trim() != "otel_libs.run")
                {
                    continue;
                }

                string value;
                try
                {
                    value = Uri.UnescapeDataString(pair[(eq + 1)..].Trim());
                }
                catch (UriFormatException)
                {
                    continue;
                }

                if (value.Length > 0)
                {
                    return value;
                }
            }
        }

        return "local";
    }

    /// <summary>
    /// The api base URL of <c>DEMO_API_URL</c> with one trailing slash, so <c>orders</c> resolves to
    /// <c>&lt;base&gt;/orders</c>. Only absolute <c>http</c> / <c>https</c> URLs are accepted.
    /// </summary>
    public static Uri ApiBase(string url)
    {
        if (!Uri.TryCreate(url.Trim().TrimEnd('/') + "/", UriKind.Absolute, out var uri)
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
        {
            throw new InvalidOperationException("DEMO_API_URL must be an absolute http(s) URL");
        }

        return uri;
    }

    /// <summary>
    /// The backend address of <c>DEMO_BACKEND_ADDR</c> (<c>host:port</c>, or a full
    /// <c>http://</c> URL) as the gRPC channel URI; plaintext HTTP/2 when no scheme is given.
    /// </summary>
    public static Uri BackendUri(string address)
    {
        var value = address.Trim();
        if (!value.Contains("://", StringComparison.Ordinal))
        {
            value = "http://" + value;
        }

        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri)
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
        {
            throw new InvalidOperationException("DEMO_BACKEND_ADDR must be host:port or an http(s) URL");
        }

        return uri;
    }

    /// <summary>The run id of this process (<see cref="RunId(string?)"/> on the process environment).</summary>
    public static string CurrentRunId() => RunId(Environment.GetEnvironmentVariable("OTEL_RESOURCE_ATTRIBUTES"));

    /// <summary>
    /// True when <c>DEMO_SUPPRESS_FRAMEWORK_EXCEPTIONS</c> is <c>true</c> (DE-D8 reversal action):
    /// the ASP.NET Core and HttpClient instrumentations stop recording exception events.
    /// </summary>
    public static bool SuppressFrameworkExceptions(Func<string, string?>? env = null) =>
        string.Equals(Get("DEMO_SUPPRESS_FRAMEWORK_EXCEPTIONS", "false", env), "true", StringComparison.OrdinalIgnoreCase);
}
