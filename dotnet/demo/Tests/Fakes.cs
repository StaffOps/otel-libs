using System.Reflection;
using Grpc.Core;
using StackExchange.Redis;

namespace StaffOps.OtelLibs.Demo.Tests;

/// <summary>An interface implementation whose every call goes to one handler (method name + arguments).</summary>
public class Fake<T> : DispatchProxy where T : class
{
    private Func<MethodInfo, object?[], object?> _handler = (m, _) => throw new NotSupportedException(m.Name);

    public static T Create(Func<MethodInfo, object?[], object?> handler)
    {
        var proxy = Create<T, Fake<T>>();
        ((Fake<T>)(object)proxy)._handler = handler;
        return proxy;
    }

    protected override object? Invoke(MethodInfo? targetMethod, object?[]? args) => _handler(targetMethod!, args ?? []);
}

/// <summary>An in-memory Redis with the commands the api and the worker use.</summary>
internal sealed class FakeRedis
{
    private long _nextId;

    public Dictionary<string, string> Strings { get; } = [];
    public List<(string Key, NameValueEntry[] Fields, RedisValue Id)> Added { get; } = [];
    public Queue<StreamEntry> Pending { get; } = new();
    public List<string> Acked { get; } = [];
    public List<string> Commands { get; } = [];
    public int GroupCreates { get; private set; }
    public Exception? GroupError { get; set; }
    public Exception? ReadError { get; set; }
    public Exception? AckError { get; set; }
    public Exception? StringError { get; set; }
    public Exception? AddError { get; set; }

    public IConnectionMultiplexer Multiplexer => Fake<IConnectionMultiplexer>.Create((m, _) =>
        m.Name == nameof(IConnectionMultiplexer.GetDatabase) ? Database : throw new NotSupportedException(m.Name));

    public IDatabase Database => Fake<IDatabase>.Create(Handle);

    private object? Handle(MethodInfo method, object?[] args)
    {
        Commands.Add(method.Name);
        switch (method.Name)
        {
            case nameof(IDatabase.StringGetAsync):
                if (StringError is not null)
                {
                    return Task.FromException<RedisValue>(StringError);
                }

                return Task.FromResult(Strings.TryGetValue(args[0]!.ToString()!, out var v) ? (RedisValue)v : RedisValue.Null);
            case nameof(IDatabase.StringSetAsync):
                Strings[args[0]!.ToString()!] = args[1]!.ToString()!;
                return Task.FromResult(true);
            case nameof(IDatabase.StreamAddAsync):
                if (AddError is not null)
                {
                    return Task.FromException<RedisValue>(AddError);
                }

                RedisValue id = $"{++_nextId}-0";
                Added.Add((args[0]!.ToString()!, (NameValueEntry[])args[1]!, id));
                return Task.FromResult(id);
            case nameof(IDatabase.StreamCreateConsumerGroupAsync):
                GroupCreates++;
                return GroupError is null ? Task.FromResult(true) : Task.FromException<bool>(GroupError);
            case nameof(IDatabase.StreamReadGroupAsync):
                if (ReadError is not null)
                {
                    return Task.FromException<StreamEntry[]>(ReadError);
                }

                var batch = new List<StreamEntry>();
                while (Pending.Count > 0)
                {
                    batch.Add(Pending.Dequeue());
                }

                return Task.FromResult(batch.ToArray());
            case nameof(IDatabase.StreamAcknowledgeAsync):
                if (AckError is not null)
                {
                    return Task.FromException<long>(AckError);
                }

                Acked.Add(args[2]!.ToString()!);
                return Task.FromResult(1L);
            default:
                throw new NotSupportedException(method.Name);
        }
    }
}

/// <summary>A gRPC call invoker answering unary calls with a function (response or RpcException).</summary>
internal sealed class FakeInvoker(Func<object, object> respond) : CallInvoker
{
    public List<object> Requests { get; } = [];

    public override AsyncUnaryCall<TResponse> AsyncUnaryCall<TRequest, TResponse>(Method<TRequest, TResponse> method, string? host, CallOptions options, TRequest request)
    {
        Requests.Add(request);
        Task<TResponse> response;
        try
        {
            response = Task.FromResult((TResponse)respond(request));
        }
        catch (Exception ex)
        {
            response = Task.FromException<TResponse>(ex);
        }

        return new AsyncUnaryCall<TResponse>(response, Task.FromResult(new Metadata()), () => Status.DefaultSuccess, () => [], () => { });
    }

    public override TResponse BlockingUnaryCall<TRequest, TResponse>(Method<TRequest, TResponse> method, string? host, CallOptions options, TRequest request) =>
        throw new NotSupportedException();

    public override AsyncServerStreamingCall<TResponse> AsyncServerStreamingCall<TRequest, TResponse>(Method<TRequest, TResponse> method, string? host, CallOptions options, TRequest request) =>
        throw new NotSupportedException();

    public override AsyncClientStreamingCall<TRequest, TResponse> AsyncClientStreamingCall<TRequest, TResponse>(Method<TRequest, TResponse> method, string? host, CallOptions options) =>
        throw new NotSupportedException();

    public override AsyncDuplexStreamingCall<TRequest, TResponse> AsyncDuplexStreamingCall<TRequest, TResponse>(Method<TRequest, TResponse> method, string? host, CallOptions options) =>
        throw new NotSupportedException();
}

/// <summary>A minimal server call context (cancellation only).</summary>
internal sealed class FakeServerCallContext(CancellationToken cancellationToken = default) : ServerCallContext
{
    protected override string MethodCore => "/demo.v1.OrderService/GetOrder";
    protected override string HostCore => "localhost";
    protected override string PeerCore => "ipv4:127.0.0.1:1";
    protected override DateTime DeadlineCore => DateTime.MaxValue;
    protected override Metadata RequestHeadersCore => [];
    protected override CancellationToken CancellationTokenCore => cancellationToken;
    protected override Metadata ResponseTrailersCore => [];
    protected override Status StatusCore { get; set; }
    protected override WriteOptions? WriteOptionsCore { get; set; }
    protected override AuthContext AuthContextCore => new(null, []);

    protected override ContextPropagationToken CreatePropagationTokenCore(ContextPropagationOptions? options) => throw new NotSupportedException();

    protected override Task WriteResponseHeadersAsyncCore(Metadata responseHeaders) => Task.CompletedTask;
}
