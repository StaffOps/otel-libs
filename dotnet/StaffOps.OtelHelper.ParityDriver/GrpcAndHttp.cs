using System.Net.Sockets;
using System.Text;
using Grpc.AspNetCore.Server.Model;
using Grpc.Core;

namespace StaffOps.OtelHelper.ParityDriver;

/// <summary>Raw-byte gRPC method descriptors, so the driver needs no protoc step.</summary>
public static class GrpcMethods
{
    private static readonly Marshaller<byte[]> Raw = Marshallers.Create(static b => b, static b => b);

    public static readonly Method<byte[], byte[]> HealthCheck = new(MethodType.Unary, "grpc.health.v1.Health", "Check", Raw, Raw);
    public static readonly Method<byte[], byte[]> HealthWatch = new(MethodType.ServerStreaming, "grpc.health.v1.Health", "Watch", Raw, Raw);
    public static readonly Method<byte[], byte[]> EchoPing = new(MethodType.Unary, "parity.v1.Echo", "Ping", Raw, Raw);
    public static readonly Method<byte[], byte[]> EchoNotFound = new(MethodType.Unary, "parity.v1.Echo", "NotFound", Raw, Raw);

    public static readonly byte[] Serving = { 0x08, 0x01 }; // HealthCheckResponse{status: SERVING}

    public static async Task InvokeAsync(CallInvoker invoker, string target)
    {
        switch (target)
        {
            case "/grpc.health.v1.Health/Check":
                await invoker.AsyncUnaryCall(HealthCheck, null, default, Array.Empty<byte>()).ResponseAsync;
                break;
            case "/grpc.health.v1.Health/Watch":
                using (var call = invoker.AsyncServerStreamingCall(HealthWatch, null, default, Array.Empty<byte>()))
                {
                    while (await call.ResponseStream.MoveNext(CancellationToken.None))
                    {
                    }
                }

                break;
            case "/parity.v1.Echo/Ping":
                await invoker.AsyncUnaryCall(EchoPing, null, default, Array.Empty<byte>()).ResponseAsync;
                break;
            case "/parity.v1.Echo/NotFound":
                await invoker.AsyncUnaryCall(EchoNotFound, null, default, Array.Empty<byte>()).ResponseAsync;
                break;
            default:
                throw new AdapterException($"unknown gRPC method \"{target}\"");
        }
    }
}

/// <summary>grpc.health.v1.Health with Check and Watch (one SERVING update).</summary>
public sealed class GrpcHealthService
{
}

/// <summary>parity.v1.Echo: Ping answers OK, NotFound answers NOT_FOUND (fixture A22).</summary>
public sealed class GrpcEchoService
{
}

/// <summary>Registers the raw-byte methods directly, without protoc or binder reflection.</summary>
internal sealed class GrpcHealthMethods : IServiceMethodProvider<GrpcHealthService>
{
    public void OnServiceMethodDiscovery(ServiceMethodProviderContext<GrpcHealthService> context)
    {
        context.AddUnaryMethod(GrpcMethods.HealthCheck, Array.Empty<object>(),
            (_, _, _) => Task.FromResult(GrpcMethods.Serving));
        context.AddServerStreamingMethod(GrpcMethods.HealthWatch, Array.Empty<object>(),
            (_, _, stream, _) => stream.WriteAsync(GrpcMethods.Serving));
    }
}

internal sealed class GrpcEchoMethods : IServiceMethodProvider<GrpcEchoService>
{
    public void OnServiceMethodDiscovery(ServiceMethodProviderContext<GrpcEchoService> context)
    {
        context.AddUnaryMethod(GrpcMethods.EchoPing, Array.Empty<object>(),
            (_, _, _) => Task.FromResult(GrpcMethods.Serving));
        context.AddUnaryMethod(GrpcMethods.EchoNotFound, Array.Empty<object>(),
            (_, _, _) => throw new RpcException(new Status(StatusCode.NotFound, "not found")));
    }
}

/// <summary>
/// One HTTP/1.1 request over a plain socket, not through HttpClient: the helper
/// instruments HttpClient and would turn every request into a traced client call.
/// </summary>
internal static class RawHttp
{
    private const int TimeoutMs = 10_000;

    public static async Task<(int Status, string Body)> RequestAsync(string method, string host, int port, string target,
        IReadOnlyDictionary<string, string> headers)
    {
        using var timeout = new CancellationTokenSource(TimeoutMs);
        using var client = new TcpClient();
        await client.ConnectAsync(host, port, timeout.Token);

        var request = new StringBuilder($"{method} {target} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n");
        foreach (var (name, value) in headers)
        {
            request.Append($"{name}: {value}\r\n");
        }

        request.Append("\r\n");
        var stream = client.GetStream();
        await stream.WriteAsync(Encoding.ASCII.GetBytes(request.ToString()), timeout.Token);

        using var buffer = new MemoryStream();
        await stream.CopyToAsync(buffer, timeout.Token);
        return Parse(buffer.ToArray());
    }

    private static (int Status, string Body) Parse(byte[] raw)
    {
        var headerEnd = IndexOf(raw, "\r\n\r\n"u8, 0);
        var head = Encoding.ASCII.GetString(raw, 0, headerEnd < 0 ? raw.Length : headerEnd);
        var body = headerEnd < 0 ? Array.Empty<byte>() : raw[(headerEnd + 4)..];
        var statusLine = head.Split("\r\n", 2)[0].Split(' ', 3);
        var status = statusLine.Length > 1 && int.TryParse(statusLine[1], out var code) ? code : 0;
        if (head.Contains("transfer-encoding: chunked", StringComparison.OrdinalIgnoreCase))
        {
            body = DecodeChunked(body);
        }

        return (status, Encoding.UTF8.GetString(body));
    }

    /// <summary>Chunked transfer coding on bytes (chunk sizes count bytes); chunk extensions are dropped.</summary>
    private static byte[] DecodeChunked(byte[] body)
    {
        using var decoded = new MemoryStream();
        var position = 0;
        while (position < body.Length)
        {
            var lineEnd = IndexOf(body, "\r\n"u8, position);
            if (lineEnd < 0)
            {
                break;
            }

            var sizeText = Encoding.ASCII.GetString(body, position, lineEnd - position).Split(';', 2)[0].Trim();
            var length = int.Parse(sizeText, System.Globalization.NumberStyles.HexNumber, System.Globalization.CultureInfo.InvariantCulture);
            if (length == 0)
            {
                break;
            }

            var start = lineEnd + 2;
            decoded.Write(body, start, Math.Min(length, body.Length - start));
            position = start + length + 2;
        }

        return decoded.ToArray();
    }

    private static int IndexOf(byte[] data, ReadOnlySpan<byte> pattern, int from)
    {
        var index = data.AsSpan(from).IndexOf(pattern);
        return index < 0 ? -1 : index + from;
    }
}
