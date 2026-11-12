using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net;
using System.Text;
using Amazon.DynamoDBv2;
using Amazon.DynamoDBv2.Model;
using Amazon.Runtime;
using Amazon.S3;
using Amazon.S3.Model;
using Amazon.SimpleNotificationService;
using Amazon.SimpleNotificationService.Model;
using Amazon.SQS;
using Amazon.SQS.Model;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Logs;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.AWS;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Logging;
using StaffOps.OtelHelper.Tracing;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C14c (contract A27, design D-NET14, DN9; ADR-014): the AWS rules of the sanitizing
/// processor and <c>AddOtelHelperAws()</c>. The integration tests drive the real AWS SDK for .NET v4
/// clients (S3, DynamoDB, SNS, SQS) through <c>AddOtelHelper</c> + <c>AddOtelHelperAws</c> against a
/// local fake endpoint (no AWS account, no network): each response is the service's wire shape,
/// and DynamoDB's error text is the one DynamoDB Local 2.6.1 returned (measured 2026-11-11). In the
/// <c>EnvVarTests</c> collection: the AWS SDK's tracer provider and pipeline customizer are
/// process-wide.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public sealed class AwsTests
{
    private const string AccessKey = "AKIAC14CPLACEHOLDER";
    private const string SecretKey = "c14c-secret-key-placeholder";
    private const string SessionToken = "c14c-session-token-placeholder";
    private const string ObjectKey = "customers/4111-1111-c14ckey.json";
    private const string PhoneNumber = "+15555550142";

    /// <summary>Text no span field may hold after the rules ran (the log record may).</summary>
    private static readonly string[] Secrets = { AccessKey, SecretKey, SessionToken, ObjectKey, "4111-1111", PhoneNumber, "98765", "c14cpartition", "c14citem" };

    private sealed class Capture : BaseProcessor<Activity>
    {
        public ConcurrentQueue<Activity> Ended { get; } = new();

        public override void OnEnd(Activity data) => Ended.Enqueue(data);
    }

    private sealed class LogCapture : BaseProcessor<OpenTelemetry.Logs.LogRecord>
    {
        public ConcurrentQueue<(string? Category, ActivityTraceId TraceId, ActivitySpanId SpanId, string? Body, List<KeyValuePair<string, object?>> Attributes)> Records { get; } = new();

        public override void OnEnd(OpenTelemetry.Logs.LogRecord data)
            => Records.Enqueue((data.CategoryName, data.TraceId, data.SpanId, data.FormattedMessage ?? data.Body, data.Attributes?.ToList() ?? new()));
    }

    /// <summary>A local HTTP endpoint answering every request with <see cref="Next"/>.</summary>
    private sealed class FakeAws : IDisposable
    {
        private HttpListener _listener;

        public FakeAws()
        {
            Port = TestPorts.FreeTcpPort();
            // A port free a moment ago can be taken before the bind: up to three ports.
            for (var attempt = 1; ; attempt++)
            {
                _listener = new HttpListener();
                _listener.Prefixes.Add($"http://127.0.0.1:{Port}/");
                try
                {
                    _listener.Start();
                    break;
                }
                catch (HttpListenerException) when (attempt < 3)
                {
                    _listener.Close();
                    Port = TestPorts.FreeTcpPort();
                }
            }

            _ = Task.Run(LoopAsync);
        }

        public int Port { get; private set; }

        public string Url => $"http://127.0.0.1:{Port}";

        public (int Status, string Body, Dictionary<string, string> Headers) Next { get; set; } = (200, string.Empty, new());

        public ConcurrentQueue<string> Requests { get; } = new();

        private async Task LoopAsync()
        {
            while (_listener.IsListening)
            {
                HttpListenerContext context;
                try
                {
                    context = await _listener.GetContextAsync();
                }
                catch (Exception)
                {
                    return;
                }

                var next = Next;
                Requests.Enqueue($"{context.Request.HttpMethod} {context.Request.RawUrl}");
                context.Response.StatusCode = next.Status;
                foreach (var (key, value) in next.Headers)
                {
                    context.Response.Headers[key] = value;
                }

                if (context.Request.HttpMethod != "HEAD")
                {
                    var bytes = Encoding.UTF8.GetBytes(next.Body);
                    context.Response.ContentLength64 = bytes.Length;
                    await context.Response.OutputStream.WriteAsync(bytes);
                }

                context.Response.Close();
            }
        }

        public void Dispose() => _listener.Close();
    }

    private sealed class Rig : IDisposable
    {
        public required ServiceProvider Provider { get; init; }

        public required Capture Spans { get; init; }

        public required LogCapture Logs { get; init; }

        public required FakeAws Fake { get; init; }

        public AWSCredentials Credentials { get; } = new SessionAWSCredentials(AccessKey, SecretKey, SessionToken);

        /// <summary>The AWS SDK spans of this rig's calls (the clients are created after the provider, which registers the SDK customizer).</summary>
        public List<Activity> AwsSpans() => Spans.Ended.Where(a => AwsSpanRules.IsAwsSource(a.Source.Name)).ToList();

        public List<(string? Category, ActivityTraceId TraceId, ActivitySpanId SpanId, string? Body, List<KeyValuePair<string, object?>> Attributes)> AwsRecords()
            => Logs.Records.Where(r => r.Category == DbExceptionLog.AwsCategory).ToList();

        public void Dispose()
        {
            Provider.Dispose();
            Fake.Dispose();
        }
    }

    private static Rig Start()
    {
        var spans = new Capture();
        var logs = new LogCapture();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "c14c-aws";
            o.OtelCollectorEndpoint = "http://localhost:4317";
            o.PrometheusMetricsPort = 0;
        });
        services.AddOtelHelperAws();
        services.ConfigureOpenTelemetryTracerProvider(b => b.AddProcessor(spans));
        services.ConfigureOpenTelemetryLoggerProvider(b => b.AddProcessor(logs));
        var provider = services.BuildServiceProvider();
        provider.GetRequiredService<TracerProvider>();
        provider.GetRequiredService<ILoggerFactory>();
        return new Rig { Provider = provider, Spans = spans, Logs = logs, Fake = new FakeAws() };
    }

    private static AmazonS3Client S3(Rig rig, string? url = null)
        => new(rig.Credentials, new AmazonS3Config { ServiceURL = url ?? rig.Fake.Url, ForcePathStyle = true, MaxErrorRetry = 0, AuthenticationRegion = "us-east-1" });

    private static AmazonDynamoDBClient DynamoDb(Rig rig)
        => new(rig.Credentials, new AmazonDynamoDBConfig { ServiceURL = rig.Fake.Url, MaxErrorRetry = 0, AuthenticationRegion = "us-east-1" });

    private static AmazonSimpleNotificationServiceClient Sns(Rig rig)
        => new(rig.Credentials, new AmazonSimpleNotificationServiceConfig { ServiceURL = rig.Fake.Url, MaxErrorRetry = 0, AuthenticationRegion = "us-east-1" });

    private static AmazonSQSClient Sqs(Rig rig)
        => new(rig.Credentials, new AmazonSQSConfig { ServiceURL = rig.Fake.Url, MaxErrorRetry = 0, AuthenticationRegion = "us-east-1" });

    private static Dictionary<string, string> Xml => new() { ["Content-Type"] = "application/xml", ["x-amz-request-id"] = "C14CREQ" };

    private static Dictionary<string, string> Json => new() { ["Content-Type"] = "application/x-amz-json-1.0" };

    /// <summary>Runs <paramref name="call"/>, returns its exception (or null) and the one AWS span it produced.</summary>
    private static async Task<(Exception? Error, Activity Span)> CallAsync(Rig rig, Func<Task> call)
    {
        var before = rig.AwsSpans().Count;
        Exception? error = null;
        try
        {
            await call();
        }
        catch (Exception e)
        {
            error = e;
        }

        var watch = Stopwatch.StartNew();
        while (rig.AwsSpans().Count == before && watch.ElapsedMilliseconds < 2000)
        {
            await Task.Delay(10);
        }

        var spans = rig.AwsSpans().Skip(before).ToList();
        return (error, Assert.Single(spans));
    }

    private static string Render(object? value) => value is System.Collections.IEnumerable list and not string
        ? string.Join(",", list.Cast<object?>())
        : value?.ToString() ?? string.Empty;

    /// <summary>Every text field of a span: name, status description, attributes, events.</summary>
    private static string Text(Activity span) => string.Join("\n",
        new[] { span.DisplayName, span.StatusDescription ?? string.Empty }
            .Concat(span.TagObjects.Select(t => t.Key + "=" + Render(t.Value)))
            .Concat(span.Events.SelectMany(e => new[] { e.Name }.Concat(e.Tags.Select(t => t.Key + "=" + Render(t.Value))))));

    private static void AssertNoSecret(Activity span)
    {
        var text = Text(span);
        foreach (var secret in Secrets)
        {
            Assert.DoesNotContain(secret, text, StringComparison.Ordinal);
        }
    }

    /// <summary>A failed span after the rules: ERROR without description, the given error.type, type-only event, no exception text attributes.</summary>
    private static void AssertFailed(Activity span, string errorType)
    {
        Assert.Equal(ActivityStatusCode.Error, span.Status);
        Assert.Null(span.StatusDescription);
        Assert.Equal(errorType, span.GetTagItem("error.type"));
        Assert.Null(span.GetTagItem("exception.message"));
        Assert.Null(span.GetTagItem("exception.stacktrace"));
        var exception = Assert.Single(span.Events, e => e.Name == "exception");
        Assert.NotNull(exception.Tags.FirstOrDefault(t => t.Key == "exception.type").Value);
        Assert.DoesNotContain(exception.Tags, t => t.Key is "exception.message" or "exception.stacktrace");
        AssertNoSecret(span);
    }

    /// <summary>The one ADR-014 record of <paramref name="span"/>: its ids, the message as body and as attribute, the stack trace.</summary>
    private static void AssertRecord(Rig rig, Activity span, string message)
    {
        var record = Assert.Single(rig.AwsRecords(), r => r.SpanId == span.SpanId);
        Assert.Equal(span.TraceId, record.TraceId);
        Assert.Equal(message, record.Body);
        Assert.Equal(message, record.Attributes.FirstOrDefault(a => a.Key == "exception.message").Value);
        Assert.NotNull(record.Attributes.FirstOrDefault(a => a.Key == "exception.stacktrace").Value);
        Assert.NotNull(record.Attributes.FirstOrDefault(a => a.Key == "exception.type").Value);
        // The correlation processor moved the span's ids into the record's trace context.
        Assert.DoesNotContain(record.Attributes, a => a.Key is "trace_id" or "span_id" or "otelhelper.event_time");
        var text = string.Join("\n", record.Attributes.Select(a => a.Key + "=" + a.Value));
        foreach (var credential in new[] { AccessKey, SecretKey, SessionToken })
        {
            Assert.DoesNotContain(credential, text, StringComparison.Ordinal);
        }
    }

    // ── error.type (A27) ───────────────────────────────────────────────────

    [Theory]
    [InlineData("NoSuchKey", "Amazon.S3.AmazonS3Exception", "NoSuchKey")]
    [InlineData("ConditionalCheckFailedException", "Amazon.DynamoDBv2.Model.ConditionalCheckFailedException", "ConditionalCheckFailedException")]
    [InlineData("AWS.SimpleQueueService.NonExistentQueue", "Amazon.SQS.Model.QueueDoesNotExistException", "QueueDoesNotExist")]
    [InlineData(null, "Amazon.SQS.Model.QueueDoesNotExistException", "QueueDoesNotExist")]
    [InlineData("lower", "Amazon.SQS.Model.QueueDoesNotExistException", "QueueDoesNotExist")]
    [InlineData(null, "Amazon.S3.AmazonS3Exception", "_OTHER")]
    [InlineData(null, "Amazon.Runtime.AmazonUnmarshallingException", "_OTHER")]
    [InlineData(null, "Amazon.SQS.Model.QueueDoesNotExist", "_OTHER")]
    [InlineData(null, "Amazon.SQS.Model.Exception", "_OTHER")]
    [InlineData(null, "System.Net.Http.HttpRequestException", "HttpRequestException")]
    [InlineData(null, "System.Threading.Tasks.TaskCanceledException", "TaskCanceledException")]
    [InlineData(null, "Plain", "Plain")]
    [InlineData(null, "My.Lib.weird_name", "_OTHER")]
    [InlineData(null, "My.Lib.", "_OTHER")]
    [InlineData(null, null, "_OTHER")]
    [InlineData("A", null, "_OTHER")]
    [InlineData("Ab", null, "Ab")]
    [InlineData("A123456789012345678901234567890123456789012345678901234567890123", null, "A123456789012345678901234567890123456789012345678901234567890123")]
    [InlineData("A1234567890123456789012345678901234567890123456789012345678901234", null, "_OTHER")]
    public void Error_Type_Is_The_Aws_Code_Else_The_Modeled_Name_Else_The_Transport_Class(string? code, string? exceptionType, string expected)
        => Assert.Equal(expected, AwsSpanRules.ErrorType(code, exceptionType));

    [Theory]
    [InlineData("AWSSDK.S3", true)]
    [InlineData("AWSSDK.DynamoDB", true)]
    [InlineData("AWSSDK", false)]
    [InlineData("AWSSDKX.S3", false)]
    [InlineData("awssdk.s3", false)]
    [InlineData("Amazon.AWS", false)]
    public void Aws_Sources_Are_The_Sdk_Sources(string source, bool aws)
        => Assert.Equal(aws, AwsSpanRules.IsAwsSource(source));

    private static ActivityListener RecordAll(ActivitySource source)
    {
        var listener = new ActivityListener
        {
            ShouldListenTo = s => s == source,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded,
        };
        ActivitySource.AddActivityListener(listener);
        return listener;
    }

    /// <summary>A failed span of <paramref name="sourceName"/> shaped as AWSSDK.Core 4.0.3.3 writes one (measured), through a processor.</summary>
    private static (Activity Span, List<(LogLevel Level, IReadOnlyList<KeyValuePair<string, object?>> State, string Message)> Records) FailedAwsSpan(string sourceName, params (string Key, object? Value)[] extra)
    {
        using var source = new ActivitySource(sourceName);
        using var listener = RecordAll(source);
        var logger = new ListLogger();
        var activity = source.StartActivity("S3.GetObject", ActivityKind.Client)!;
        activity.SetTag("aws.s3.bucket", "orders");
        activity.SetTag("aws.s3.key", ObjectKey);
        activity.SetTag("aws.s3.copy_source", "orders/" + ObjectKey);
        activity.SetTag("aws.s3.delete", "Objects=[{Key=" + ObjectKey + "}]");
        activity.SetTag("aws.dynamodb.item_collection_metrics", "{\"c14cpartition\":1}");
        foreach (var (key, value) in extra)
        {
            activity.SetTag(key, value);
        }

        try
        {
            throw new InvalidOperationException("Invalid argument for key " + ObjectKey);
        }
        catch (InvalidOperationException e)
        {
            activity.SetTag("exception.type", "Amazon.S3.AmazonS3Exception");
            activity.SetTag("exception.message", e.Message);
            activity.SetTag("exception.stacktrace", e.StackTrace);
            activity.SetTag("aws.error_code", "InvalidArgument");
            activity.AddException(e);
            activity.SetStatus(ActivityStatusCode.Error, e.Message);
        }

        activity.Stop();
        var processor = new SanitizingProcessor(new TestDiagnostics().Sink) { AwsExceptionLogger = () => logger };
        processor.OnEnd(activity);
        processor.Sanitize(new Batch<Activity>(new[] { activity }, 1));
        return (activity, logger.Records);
    }

    [Fact]
    public void Failed_Aws_Span_Loses_Description_Payload_Keys_And_Exception_Text()
    {
        var (span, records) = FailedAwsSpan("AWSSDK.S3");

        Assert.Equal(ActivityStatusCode.Error, span.Status);
        Assert.Null(span.StatusDescription);
        Assert.Equal("InvalidArgument", span.GetTagItem("error.type"));
        Assert.Equal("orders", span.GetTagItem("aws.s3.bucket"));
        Assert.Equal("InvalidArgument", span.GetTagItem("aws.error_code"));
        Assert.Equal("Amazon.S3.AmazonS3Exception", span.GetTagItem("exception.type"));
        foreach (var key in new[] { "aws.s3.key", "aws.s3.copy_source", "aws.s3.delete", "aws.dynamodb.item_collection_metrics", "exception.message", "exception.stacktrace" })
        {
            Assert.Null(span.GetTagItem(key));
        }

        AssertNoSecret(span);
        var record = Assert.Single(records);
        Assert.Equal(LogLevel.Error, record.Level);
        Assert.Equal("Invalid argument for key " + ObjectKey, record.Message);
        Assert.Equal(span.SpanId.ToHexString(), records[0].State.First(p => p.Key == "span_id").Value);
    }

    [Fact]
    public void An_Error_Type_Set_Elsewhere_Is_Replaced_By_The_Contract_Value()
    {
        var (span, _) = FailedAwsSpan("AWSSDK.S3", ("error.type", "System.InvalidOperationException"));

        Assert.Equal("InvalidArgument", span.GetTagItem("error.type"));
    }

    [Fact]
    public void Error_Type_Falls_Back_To_The_Exception_Event_When_The_Span_Has_No_Type_Attribute()
    {
        using var source = new ActivitySource("AWSSDK.SQS");
        using var listener = RecordAll(source);
        var activity = source.StartActivity("SQS.SendMessage", ActivityKind.Client)!;
        activity.AddEvent(new ActivityEvent("exception", tags: new ActivityTagsCollection { { "exception.type", "Amazon.SQS.Model.QueueDoesNotExistException" } }));
        activity.SetStatus(ActivityStatusCode.Error);
        activity.Stop();

        new SanitizingProcessor(new TestDiagnostics().Sink).OnEnd(activity);

        Assert.Equal("QueueDoesNotExist", activity.GetTagItem("error.type"));
    }

    [Fact]
    public void Error_Type_Ignores_Other_Events()
    {
        using var source = new ActivitySource("AWSSDK.SQS");
        using var listener = RecordAll(source);
        var activity = source.StartActivity("SQS.SendMessage", ActivityKind.Client)!;
        activity.AddEvent(new ActivityEvent("retry", tags: new ActivityTagsCollection { { "exception.type", "Amazon.SQS.Model.QueueDoesNotExistException" } }));
        activity.SetStatus(ActivityStatusCode.Error);
        activity.Stop();

        new SanitizingProcessor(new TestDiagnostics().Sink).OnEnd(activity);

        Assert.Equal("_OTHER", activity.GetTagItem("error.type"));
    }

    [Fact]
    public void Successful_Aws_Span_Gets_No_Error_Type_But_Loses_The_Object_Key()
    {
        using var source = new ActivitySource("AWSSDK.S3");
        using var listener = RecordAll(source);
        var activity = source.StartActivity("S3.PutObject", ActivityKind.Client)!;
        activity.SetTag("aws.s3.key", ObjectKey);
        activity.SetTag("otel.status_description", "x");
        activity.Stop();

        new SanitizingProcessor(new TestDiagnostics().Sink).OnEnd(activity);

        Assert.Null(activity.GetTagItem("error.type"));
        Assert.Null(activity.GetTagItem("aws.s3.key"));
        Assert.Null(activity.GetTagItem("otel.status_description"));
        Assert.Equal(ActivityStatusCode.Unset, activity.Status);
    }

    [Fact]
    public void Other_Sources_Keep_Their_Aws_Named_Attributes()
    {
        using var source = new ActivitySource("c14c-app");
        using var listener = RecordAll(source);
        var logger = new ListLogger();
        var activity = source.StartActivity("upload")!;
        activity.SetTag("aws.s3.key", ObjectKey);
        activity.SetTag("exception.message", "kept");
        activity.SetStatus(ActivityStatusCode.Error, "kept");
        activity.Stop();

        new SanitizingProcessor(new TestDiagnostics().Sink) { AwsExceptionLogger = () => logger }.OnEnd(activity);

        Assert.Equal(ObjectKey, activity.GetTagItem("aws.s3.key"));
        Assert.Equal("kept", activity.GetTagItem("exception.message"));
        Assert.Equal("kept", activity.StatusDescription);
        Assert.Null(activity.GetTagItem("error.type"));
        Assert.Empty(logger.Records);
    }

    [Fact]
    public void Aws_Record_Without_Message_Or_Type_Has_The_Aws_Fallback_Body()
    {
        using var source = new ActivitySource("AWSSDK.S3");
        using var listener = RecordAll(source);
        var logger = new ListLogger();
        var activity = source.StartActivity("S3.GetObject")!;
        activity.AddEvent(new ActivityEvent("exception", tags: new ActivityTagsCollection { { "exception.stacktrace", "at Amazon.Runtime.Internal..." } }));
        activity.SetStatus(ActivityStatusCode.Error);
        activity.Stop();

        new SanitizingProcessor(new TestDiagnostics().Sink) { AwsExceptionLogger = () => logger }.OnEnd(activity);

        Assert.Equal(DbExceptionLog.AwsFallbackBody, Assert.Single(logger.Records).Message);
        Assert.DoesNotContain(activity.Events.Single().Tags, t => t.Key == "exception.stacktrace");
    }

    [Fact]
    public void A_Failing_Rule_Still_Removes_The_Aws_Payload_And_Url_Attributes()
    {
        using var source = new ActivitySource("AWSSDK.S3");
        using var listener = RecordAll(source);
        var diagnostics = new TestDiagnostics();
        var activity = source.StartActivity("S3.GetObject")!;
        activity.SetTag("aws.s3.key", ObjectKey);
        activity.SetTag("exception.message", ObjectKey);
        activity.SetTag("url.full", "https://example.com/o?X-Amz-Signature=c14csig");
        activity.SetStatus(ActivityStatusCode.Error, ObjectKey);
        activity.Stop();

        new SanitizingProcessor(diagnostics.Sink) { Rules = _ => throw new InvalidOperationException() }.OnEnd(activity);

        AssertNoSecret(activity);
        Assert.Null(activity.GetTagItem("url.full"));
        Assert.Equal("otelhelper: WARN span sanitizer failed; the span's status description, query text, connection, URL and AWS payload attributes were removed source=AWSSDK.S3 error=InvalidOperationException", Assert.Single(diagnostics.Lines));
    }

    [Fact]
    public void Aws_Record_Category_Is_Correlated_On_Otlp_And_Stdout()
    {
        Assert.True(DbExceptionLog.IsRecordCategory(DbExceptionLog.AwsCategory));
        Assert.True(DbExceptionLog.IsRecordCategory(DbExceptionLog.Category));
        Assert.False(DbExceptionLog.IsRecordCategory("StaffOps.OtelHelper.Other"));
        var attributes = new List<KeyValuePair<string, object?>> { new("trace_id", "0123456789abcdef0123456789abcdef"), new("span_id", "0123456789abcdef") };
        Assert.True(DbExceptionLog.TryReadContext(DbExceptionLog.AwsCategory, attributes, out var traceId, out _, out _));
        Assert.Equal("0123456789abcdef0123456789abcdef", traceId.ToHexString());
        var time = DateTimeOffset.UnixEpoch;
        Assert.Equal(("0123456789abcdef0123456789abcdef", "0123456789abcdef"), JsonLine.SpanContext(DbExceptionLog.AwsCategory, attributes, ref time));
    }

    private sealed class ListLogger : ILogger
    {
        public List<(LogLevel Level, IReadOnlyList<KeyValuePair<string, object?>> State, string Message)> Records { get; } = new();

        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;

        public bool IsEnabled(LogLevel logLevel) => true;

        public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter)
            => Records.Add((logLevel, (IReadOnlyList<KeyValuePair<string, object?>>)state!, formatter(state, exception)));
    }

    // ── real SDK clients against the fake endpoint ─────────────────────────

    [Fact]
    public async Task S3_Put_Is_One_Client_Span_Without_The_Object_Key_Or_Http_Child()
    {
        using var rig = Start();
        using var s3 = S3(rig);
        rig.Fake.Next = (200, string.Empty, new() { ["ETag"] = "\"e1\"" });

        var (error, span) = await CallAsync(rig, () => s3.PutObjectAsync(new PutObjectRequest { BucketName = "orders", Key = ObjectKey, ContentBody = "payload" }));

        Assert.Null(error);
        Assert.Equal("S3.PutObject", span.DisplayName);
        Assert.Equal(ActivityKind.Client, span.Kind);
        Assert.Equal(ActivityStatusCode.Unset, span.Status);
        Assert.Equal("orders", span.GetTagItem("aws.s3.bucket"));
        Assert.Equal("aws-api", span.GetTagItem("rpc.system"));
        Assert.Equal("S3", span.GetTagItem("rpc.service"));
        Assert.Equal("PutObject", span.GetTagItem("rpc.method"));
        Assert.Null(span.GetTagItem("aws.s3.key"));
        Assert.Null(span.GetTagItem("error.type"));
        AssertNoSecret(span);
        Assert.Contains(rig.Fake.Requests, r => r.Contains(ObjectKey, StringComparison.Ordinal));
        // SuppressDownstreamInstrumentation: no HttpClient span (its url.full path is the key), no SDK-internal span.
        Assert.DoesNotContain(rig.Spans.Ended, a => Equals(a.GetTagItem("server.port"), rig.Fake.Port));
        Assert.DoesNotContain(rig.Spans.Ended, a => a.DisplayName is "HttpRequest" or "CredentialsRetrieval");
        Assert.Empty(rig.AwsRecords());
    }

    [Fact]
    public async Task S3_NoSuchKey_Is_Error_With_The_Code_And_The_Detail_In_A_Correlated_Log()
    {
        using var rig = Start();
        using var s3 = S3(rig);
        rig.Fake.Next = (404, $"<?xml version=\"1.0\"?><Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>{ObjectKey}</Key><RequestId>C14CREQ</RequestId></Error>", Xml);

        var (error, span) = await CallAsync(rig, () => s3.GetObjectAsync("orders", ObjectKey));

        Assert.Equal("NoSuchKey", Assert.IsAssignableFrom<AmazonServiceException>(error).ErrorCode);
        AssertFailed(span, "NoSuchKey");
        Assert.Equal("Amazon.S3.AmazonS3Exception", span.GetTagItem("exception.type"));
        Assert.Equal("NoSuchKey", span.GetTagItem("aws.error_code"));
        AssertRecord(rig, span, "The specified key does not exist.");
    }

    [Fact]
    public async Task S3_Error_Message_Quoting_The_Key_Reaches_Only_The_Log()
    {
        using var rig = Start();
        using var s3 = S3(rig);
        rig.Fake.Next = (400, $"<?xml version=\"1.0\"?><Error><Code>InvalidArgument</Code><Message>Invalid argument for key {ObjectKey}</Message></Error>", Xml);

        var (_, span) = await CallAsync(rig, () => s3.PutObjectAsync(new PutObjectRequest { BucketName = "orders", Key = ObjectKey, ContentBody = "payload" }));

        AssertFailed(span, "InvalidArgument");
        AssertRecord(rig, span, "Invalid argument for key " + ObjectKey);
    }

    [Theory]
    [InlineData(404, "NotFound")]
    [InlineData(403, "Forbidden")]
    [InlineData(503, "ServiceUnavailable")]
    public async Task S3_Head_Without_A_Body_Gives_The_Status_Name(int status, string errorType)
    {
        using var rig = Start();
        using var s3 = S3(rig);
        rig.Fake.Next = (status, string.Empty, new());

        var (_, span) = await CallAsync(rig, () => s3.GetObjectMetadataAsync("orders", ObjectKey));

        Assert.Equal("S3.GetObjectMetadata", span.DisplayName);
        AssertFailed(span, errorType);
    }

    [Fact]
    public async Task S3_Copy_And_Delete_Record_No_Copy_Source_Or_Delete_Request()
    {
        using var rig = Start();
        using var s3 = S3(rig);
        rig.Fake.Next = (200, "<?xml version=\"1.0\"?><CopyObjectResult><ETag>\"e\"</ETag><LastModified>2026-11-11T00:00:00.000Z</LastModified></CopyObjectResult>", Xml);
        var (_, copy) = await CallAsync(rig, () => s3.CopyObjectAsync("orders", ObjectKey, "orders", ObjectKey + ".bak"));
        rig.Fake.Next = (200, $"<?xml version=\"1.0\"?><DeleteResult><Deleted><Key>{ObjectKey}</Key></Deleted></DeleteResult>", Xml);
        var (_, delete) = await CallAsync(rig, () => s3.DeleteObjectsAsync(new DeleteObjectsRequest { BucketName = "orders", Objects = new List<KeyVersion> { new() { Key = ObjectKey } } }));

        foreach (var span in new[] { copy, delete })
        {
            Assert.Null(span.GetTagItem("aws.s3.copy_source"));
            Assert.Null(span.GetTagItem("aws.s3.delete"));
            Assert.Null(span.GetTagItem("aws.s3.key"));
            AssertNoSecret(span);
        }

        Assert.Equal("S3.CopyObject", copy.DisplayName);
        Assert.Equal("S3.DeleteObjects", delete.DisplayName);
    }

    [Fact]
    public async Task DynamoDb_Put_Records_No_Item_Collection_Metrics()
    {
        using var rig = Start();
        using var ddb = DynamoDb(rig);
        rig.Fake.Next = (200, "{\"ItemCollectionMetrics\":{\"ItemCollectionKey\":{\"pk\":{\"S\":\"c14cpartition\"}},\"SizeEstimateRangeGB\":[0,1]}}", Json);

        var (error, span) = await CallAsync(rig, () => ddb.PutItemAsync(new PutItemRequest
        {
            TableName = "orders",
            ReturnItemCollectionMetrics = ReturnItemCollectionMetrics.SIZE,
            Item = new() { ["pk"] = new AttributeValue { S = "c14cpartition" } },
        }));

        Assert.Null(error);
        Assert.Equal("DynamoDB.PutItem", span.DisplayName);
        Assert.Null(span.GetTagItem("aws.dynamodb.item_collection_metrics"));
        Assert.Equal("dynamodb", span.GetTagItem("db.system"));
        Assert.Equal(new[] { "orders" }, Assert.IsType<string[]>(span.GetTagItem("aws.dynamodb.table_names")));
        AssertNoSecret(span);
    }

    [Fact]
    public async Task DynamoDb_Expression_Literal_In_The_Error_Reaches_Only_The_Log()
    {
        // The text DynamoDB Local 2.6.1 returned for UpdateExpression "SET card = 98765" (measured).
        const string message = "Invalid UpdateExpression: Syntax error; token: \"98765\", near: \"= 98765\"";
        using var rig = Start();
        using var ddb = DynamoDb(rig);
        rig.Fake.Next = (400, "{\"__type\":\"com.amazon.coral.validate#ValidationException\",\"message\":\"Invalid UpdateExpression: Syntax error; token: \\\"98765\\\", near: \\\"= 98765\\\"\"}", Json);

        var (_, span) = await CallAsync(rig, () => ddb.UpdateItemAsync(new UpdateItemRequest
        {
            TableName = "orders",
            Key = new() { ["pk"] = new AttributeValue { S = "c14citem" } },
            UpdateExpression = "SET card = 98765",
        }));

        AssertFailed(span, "ValidationException");
        AssertRecord(rig, span, message);
    }

    [Fact]
    public async Task DynamoDb_Conditional_Check_With_The_Old_Item_Keeps_The_Item_Out_Of_The_Span()
    {
        using var rig = Start();
        using var ddb = DynamoDb(rig);
        rig.Fake.Next = (400, "{\"__type\":\"com.amazonaws.dynamodb.v20120810#ConditionalCheckFailedException\",\"message\":\"The conditional request failed\",\"Item\":{\"pk\":{\"S\":\"c14citem\"},\"card\":{\"S\":\"4111-1111\"}}}", Json);

        var (error, span) = await CallAsync(rig, () => ddb.PutItemAsync(new PutItemRequest
        {
            TableName = "orders",
            ConditionExpression = "attribute_not_exists(pk)",
            ReturnValuesOnConditionCheckFailure = ReturnValuesOnConditionCheckFailure.ALL_OLD,
            Item = new() { ["pk"] = new AttributeValue { S = "c14citem" } },
        }));

        Assert.IsType<ConditionalCheckFailedException>(error);
        AssertFailed(span, "ConditionalCheckFailedException");
        AssertRecord(rig, span, "The conditional request failed");
        Assert.DoesNotContain(rig.AwsRecords(), r => string.Join("\n", r.Attributes.Select(a => a.Value)).Contains("4111-1111", StringComparison.Ordinal));
    }

    [Fact]
    public async Task Sns_Publish_To_A_Phone_Number_Records_No_Number()
    {
        using var rig = Start();
        using var sns = Sns(rig);
        rig.Fake.Next = (200, "<PublishResponse xmlns=\"http://sns.amazonaws.com/doc/2010-03-31/\"><PublishResult><MessageId>m-1</MessageId></PublishResult><ResponseMetadata><RequestId>r1</RequestId></ResponseMetadata></PublishResponse>", new() { ["Content-Type"] = "text/xml" });
        var (_, ok) = await CallAsync(rig, () => sns.PublishAsync(new PublishRequest { PhoneNumber = PhoneNumber, Message = "code 1234" }));
        rig.Fake.Next = (400, $"<ErrorResponse xmlns=\"http://sns.amazonaws.com/doc/2010-03-31/\"><Error><Type>Sender</Type><Code>InvalidParameter</Code><Message>Invalid parameter: PhoneNumber Reason: {PhoneNumber} is not valid to publish to</Message></Error><RequestId>r2</RequestId></ErrorResponse>", new() { ["Content-Type"] = "text/xml" });
        var (_, failed) = await CallAsync(rig, () => sns.PublishAsync(new PublishRequest { PhoneNumber = PhoneNumber, Message = "code 1234" }));

        Assert.Equal("SNS.Publish", ok.DisplayName);
        AssertNoSecret(ok);
        AssertFailed(failed, "InvalidParameter");
        AssertRecord(rig, failed, $"Invalid parameter: PhoneNumber Reason: {PhoneNumber} is not valid to publish to");
    }

    [Fact]
    public async Task Sqs_Query_Compatible_Code_Gives_The_Modeled_Name()
    {
        using var rig = Start();
        using var sqs = Sqs(rig);
        var headers = Json;
        headers["x-amzn-query-error"] = "AWS.SimpleQueueService.NonExistentQueue;Sender";
        rig.Fake.Next = (400, "{\"__type\":\"com.amazonaws.sqs#QueueDoesNotExist\",\"message\":\"The specified queue does not exist.\"}", headers);

        var (error, span) = await CallAsync(rig, () => sqs.SendMessageAsync(rig.Fake.Url + "/000000000000/missing", "body"));

        Assert.Equal("AWS.SimpleQueueService.NonExistentQueue", Assert.IsType<QueueDoesNotExistException>(error).ErrorCode);
        Assert.Equal("SQS.SendMessage", span.DisplayName);
        Assert.Equal(ActivityKind.Client, span.Kind);
        AssertFailed(span, "QueueDoesNotExist");
        Assert.Equal("AWS.SimpleQueueService.NonExistentQueue", span.GetTagItem("aws.error_code"));
    }

    [Fact]
    public async Task Transport_Error_Gives_The_Exception_Class()
    {
        using var rig = Start();
        using var s3 = S3(rig, $"http://127.0.0.1:{TestPorts.FreeTcpPort()}");

        var (error, span) = await CallAsync(rig, () => s3.GetObjectAsync("orders", ObjectKey));

        Assert.IsType<HttpRequestException>(error);
        AssertFailed(span, "HttpRequestException");
        Assert.Single(rig.AwsRecords(), r => r.SpanId == span.SpanId);
    }

    [Fact]
    public async Task Unparseable_Response_Gives_Other()
    {
        using var rig = Start();
        using var ddb = DynamoDb(rig);
        rig.Fake.Next = (200, "not json {c14citem}", Json);

        var (error, span) = await CallAsync(rig, () => ddb.GetItemAsync(new GetItemRequest { TableName = "orders", Key = new() { ["pk"] = new AttributeValue { S = "c14citem" } } }));

        Assert.IsType<AmazonUnmarshallingException>(error);
        AssertFailed(span, "_OTHER");
    }

    // ── URL query redaction (A28, O3) ──────────────────────────────────────

    [Theory]
    [InlineData("sig=abc", "sig=REDACTED")]
    [InlineData("Signature=abc", "Signature=REDACTED")]
    [InlineData("AWSAccessKeyId=AKIA", "AWSAccessKeyId=REDACTED")]
    [InlineData("X-Goog-Signature=abc", "X-Goog-Signature=REDACTED")]
    [InlineData("X-Amz-Signature=abc", "X-Amz-Signature=REDACTED")]
    [InlineData("X-Amz-Credential=AKIA%2F20261111%2Fus-east-1", "X-Amz-Credential=REDACTED")]
    [InlineData("X-Amz-Security-Token=tok", "X-Amz-Security-Token=REDACTED")]
    [InlineData("access_token=tok", "access_token=REDACTED")]
    [InlineData("X-Goog-Credential=sa", "X-Goog-Credential=REDACTED")]
    [InlineData("GoogleAccessId=sa", "GoogleAccessId=REDACTED")]
    [InlineData("X-Amz-Date=20261111T000000Z&X-Amz-Signature=abc&token=keep", "X-Amz-Date=20261111T000000Z&X-Amz-Signature=REDACTED&token=keep")]
    [InlineData("X-Amz-%53ignature=abc", "X-Amz-%53ignature=REDACTED")]
    [InlineData("x-amz-signature=low", "x-amz-signature=low")]
    [InlineData("sig", "sig")]
    [InlineData("sig=", "sig=REDACTED")]
    [InlineData("sig=Redacted", "sig=Redacted")]
    [InlineData("sig=REDACTED", "sig=REDACTED")]
    [InlineData("a=1;sig=2", "a=1;sig=2")]
    [InlineData("%zz=1&sig=a=b", "%zz=1&sig=REDACTED")]
    [InlineData("token=t&api_key=k&key=v", "token=t&api_key=k&key=v")]
    [InlineData("", "")]
    public void Query_Values_Of_The_Contract_Names_Are_Redacted(string query, string expected)
        => Assert.Equal(expected, UrlQueryRedaction.RedactQuery(query));

    [Theory]
    [InlineData("https://h/o?X-Amz-Signature=abc#frag?sig=x", true, "https://h/o?X-Amz-Signature=REDACTED#frag?sig=x")]
    [InlineData("/o?sig=a#b", false, "/o?sig=REDACTED")]
    [InlineData("https://h/o", true, "https://h/o")]
    [InlineData("https://h/o?", true, "https://h/o?")]
    [InlineData("https://h/o?*", true, "https://h/o?*")]
    [InlineData("https://h/o?a=b?sig=c", true, "https://h/o?a=b?sig=c")]
    public void Urls_Keep_Everything_But_The_Listed_Values(string url, bool fragment, string expected)
        => Assert.Equal(expected, UrlQueryRedaction.RedactUrl(url, fragment));

    [Theory]
    [InlineData("?sig=a&b=c", "?sig=REDACTED&b=c")]
    [InlineData("sig=a&b=c", "sig=REDACTED&b=c")]
    [InlineData("?b=c", "?b=c")]
    public void Url_Query_Value_Keeps_Its_Leading_Question_Mark(string value, string expected)
        => Assert.Equal(expected, UrlQueryRedaction.RedactQueryValue(value));

    [Fact]
    public void Url_Rule_Applies_To_Every_Source_And_Every_Url_Key()
    {
        using var source = new ActivitySource("c14c-url");
        using var listener = RecordAll(source);
        var activity = source.StartActivity("GET")!;
        activity.SetTag("url.full", "https://example.com/o?X-Amz-Credential=c14ccred&X-Amz-Signature=c14csig#f");
        activity.SetTag("http.url", "https://example.com/o?sig=c14csig");
        activity.SetTag("http.target", "/o?Signature=c14csig#x");
        activity.SetTag("url.query", "?access_token=c14ctok&page=2");
        activity.SetTag("url.path", "/o?sig=kept-not-a-query-key");
        activity.Stop();

        new SanitizingProcessor(new TestDiagnostics().Sink).OnEnd(activity);

        Assert.Equal("https://example.com/o?X-Amz-Credential=REDACTED&X-Amz-Signature=REDACTED#f", activity.GetTagItem("url.full"));
        Assert.Equal("https://example.com/o?sig=REDACTED", activity.GetTagItem("http.url"));
        Assert.Equal("/o?Signature=REDACTED", activity.GetTagItem("http.target"));
        Assert.Equal("?access_token=REDACTED&page=2", activity.GetTagItem("url.query"));
        Assert.Equal("/o?sig=kept-not-a-query-key", activity.GetTagItem("url.path"));
    }

    [Fact]
    public void Url_Rule_Redacts_The_Text_Of_Non_String_Values()
    {
        using var source = new ActivitySource("c14c-url");
        using var listener = RecordAll(source);
        var activity = source.StartActivity("GET")!;
        var clean = new Uri("https://example.com/o?page=2");
        activity.SetTag("url.full", new Uri("https://example.com/o?sig=x"));
        activity.SetTag("http.url", clean);
        activity.SetTag("http.target", 42);
        activity.SetTag("url.query", new StringBuilder("?access_token=c14ctok"));
        activity.Stop();

        new SanitizingProcessor(new TestDiagnostics().Sink).OnEnd(activity);

        Assert.Equal("https://example.com/o?sig=REDACTED", activity.GetTagItem("url.full"));
        Assert.Same(clean, activity.GetTagItem("http.url"));
        Assert.Equal(42, activity.GetTagItem("http.target"));
        Assert.Equal("?access_token=REDACTED", activity.GetTagItem("url.query"));
    }

    [Theory]
    [InlineData(null, null)]
    [InlineData(true, null)]
    [InlineData(7L, null)]
    [InlineData(1.5, null)]
    [InlineData("x", "x")]
    public void Url_Text_Of_Primitives_Is_None(object? value, string? expected)
        => Assert.Equal(expected, UrlQueryRedaction.TextOf(value));

    // ── HTTP child spans of AWS calls (fix round 1) ───────────────────────

    [Theory]
    [InlineData("http://127.0.0.1:9000/orders/customers/k.json", true, "http://127.0.0.1:9000/")]
    [InlineData("https://orders.s3.amazonaws.com/k.json?X-Amz-Signature=a#f", true, "https://orders.s3.amazonaws.com/?X-Amz-Signature=a#f")]
    [InlineData("https://h", true, "https://h/")]
    [InlineData("https://h?x=1", true, "https://h/?x=1")]
    [InlineData("https://h/", true, "https://h/")]
    [InlineData("/orders/k.json?x=1", false, "/?x=1")]
    [InlineData("/orders/k.json", false, "/")]
    [InlineData("https://[::1]:9000/b/k", true, "https://[::1]:9000/")]
    [InlineData("https://u:p@h/b", true, "https://u:p@h/")]
    public void Child_Url_Loses_Its_Path(string url, bool absolute, string expected)
        => Assert.Equal(expected, AwsSpanRules.WithoutPath(url, absolute));

    /// <summary>A child span of <paramref name="childSource"/> under a parent of <paramref name="parentSource"/>, both ended, through a processor.</summary>
    private static Activity ChildOf(string parentSource, string childSource, params (string Key, object Value)[] tags)
    {
        using var parentActivitySource = new ActivitySource(parentSource);
        using var childActivitySource = new ActivitySource(childSource);
        using var parentListener = RecordAll(parentActivitySource);
        using var childListener = RecordAll(childActivitySource);
        var parent = parentActivitySource.StartActivity("HttpRequest")!;
        var child = childActivitySource.StartActivity("PUT", ActivityKind.Client)!;
        foreach (var (key, value) in tags)
        {
            child.SetTag(key, value);
        }

        child.Stop();
        // One pass (the processor's OnEnd): the exporter's second pass must not be what fixes it.
        new SanitizingProcessor(new TestDiagnostics().Sink).OnEnd(child);
        parent.Stop();
        return child;
    }

    [Fact]
    public void Http_Child_Of_An_Aws_Span_Loses_The_Path_In_Every_Url_Key()
    {
        var child = ChildOf("AWSSDK.S3", "System.Net.Http",
            ("url.full", new Uri($"http://127.0.0.1:9000/orders/{ObjectKey}?X-Amz-Signature=c14csig&page=2")),
            ("http.url", $"http://127.0.0.1:9000/orders/{ObjectKey}"),
            ("url.path", $"/orders/{ObjectKey}"),
            ("http.target", $"/orders/{ObjectKey}?sig=c14csig"));

        Assert.Equal("http://127.0.0.1:9000/?X-Amz-Signature=REDACTED&page=2", child.GetTagItem("url.full"));
        Assert.Equal("http://127.0.0.1:9000/", child.GetTagItem("http.url"));
        Assert.Equal("/", child.GetTagItem("url.path"));
        Assert.Equal("/?sig=REDACTED", child.GetTagItem("http.target"));
        AssertNoSecret(child);
        var again = new SanitizingProcessor(new TestDiagnostics().Sink).Sanitize(new Batch<Activity>(new[] { child }, 1));
        foreach (var span in again)
        {
            Assert.Equal("http://127.0.0.1:9000/?X-Amz-Signature=REDACTED&page=2", span.GetTagItem("url.full"));
        }
    }

    [Theory]
    [InlineData("https://h/b/k", true)]
    [InlineData("http://127.0.0.1:9000", true)]
    [InlineData("/orders/k.json?r=https://x/y", false)]
    [InlineData("orders?r=https://x/y", false)]
    [InlineData("://h/b", false)]
    [InlineData("/b", false)]
    public void Only_A_Scheme_Before_The_Path_Makes_A_Url_Absolute(string url, bool absolute)
        => Assert.Equal(absolute, AwsSpanRules.IsAbsolute(url));

    [Fact]
    public void Relative_Child_Url_With_A_Url_In_Its_Query_Loses_Its_Path()
    {
        var child = ChildOf("AWSSDK.S3", "System.Net.Http", ("url.full", $"/orders/{ObjectKey}?r=https://x/y"));

        Assert.Equal("/?r=https://x/y", child.GetTagItem("url.full"));
    }

    [Fact]
    public void Http_Grandchild_Under_A_Handler_Span_Loses_The_Path()
    {
        using var aws = new ActivitySource("AWSSDK.S3");
        using var handler = new ActivitySource("c14c-handler");
        using var http = new ActivitySource("System.Net.Http");
        using var l1 = RecordAll(aws);
        using var l2 = RecordAll(handler);
        using var l3 = RecordAll(http);
        var root = aws.StartActivity("HttpRequest")!;
        var middle = handler.StartActivity("retry")!;
        var grandchild = http.StartActivity("PUT", ActivityKind.Client)!;
        grandchild.SetTag("url.full", $"http://127.0.0.1:9000/orders/{ObjectKey}");
        grandchild.Stop();
        new SanitizingProcessor(new TestDiagnostics().Sink).OnEnd(grandchild);
        middle.Stop();
        root.Stop();

        Assert.Equal("http://127.0.0.1:9000/", grandchild.GetTagItem("url.full"));
        AssertNoSecret(grandchild);
    }

    [Fact]
    public void Ancestors_Beyond_The_Depth_Bound_Are_Not_Walked()
    {
        using var aws = new ActivitySource("AWSSDK.S3");
        using var handler = new ActivitySource("c14c-handler");
        using var l1 = RecordAll(aws);
        using var l2 = RecordAll(handler);
        var chain = new List<Activity> { aws.StartActivity("HttpRequest")! };
        for (var i = 0; i < AwsSpanRules.MaxAncestorDepth + 1; i++)
        {
            chain.Add(handler.StartActivity("h" + i)!);
        }

        var last = chain[^1];
        Assert.False(AwsSpanRules.HasAwsAncestor(last));
        Assert.True(AwsSpanRules.HasAwsAncestor(chain[^2]));
        for (var i = chain.Count - 1; i >= 0; i--)
        {
            chain[i].Stop();
        }
    }

    [Fact]
    public void Http_Child_Of_Another_Span_And_The_Aws_Span_Itself_Keep_Their_Path()
    {
        var other = ChildOf("c14c-app", "System.Net.Http", ("url.full", "http://127.0.0.1:9000/orders/k.json"));
        var aws = ChildOf("AWSSDK.S3", "AWSSDK.S3", ("url.full", "http://127.0.0.1:9000/orders/k.json"));

        Assert.Equal("http://127.0.0.1:9000/orders/k.json", other.GetTagItem("url.full"));
        Assert.Equal("http://127.0.0.1:9000/orders/k.json", aws.GetTagItem("url.full"));
    }

    [Fact]
    public async Task Real_HttpClient_Span_Under_An_Aws_Span_Records_No_Object_Key()
    {
        // The residual scenarios (a client built before the provider, the application's own
        // AddAWSInstrumentation registered first) put the SDK's HttpClient call under the SDK's
        // HttpRequest span; the same shape, built here with the real HttpClient instrumentation.
        using var rig = Start();
        rig.Fake.Next = (200, "ok", new());
        using var source = new ActivitySource("AWSSDK.S3");
        using (source.StartActivity("HttpRequest"))
        {
            using var http = new HttpClient();
            (await http.PutAsync($"{rig.Fake.Url}/orders/{ObjectKey}?x-id=PutObject", new StringContent("p"))).EnsureSuccessStatusCode();
        }

        var watch = Stopwatch.StartNew();
        Activity? child = null;
        while (child is null && watch.ElapsedMilliseconds < 5000)
        {
            await Task.Delay(10);
            child = rig.Spans.Ended.FirstOrDefault(a => a.Source.Name == "System.Net.Http" && Equals(a.GetTagItem("server.port"), rig.Fake.Port));
        }

        Assert.NotNull(child);
        var full = (string)child!.GetTagItem("url.full")!;
        Assert.StartsWith(rig.Fake.Url + "/?", full);
        AssertNoSecret(child);
        Assert.Contains(rig.Fake.Requests, r => r.Contains(ObjectKey, StringComparison.Ordinal));
    }

    private const string PresignedQuery = "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIAC14CPLACEHOLDER%2F20261111%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20261111T120000Z&X-Amz-Expires=300&X-Amz-Security-Token=c14c-session-token-placeholder&X-Amz-SignedHeaders=host&X-Amz-Signature=c14csignature&token=keep&x-amz-signature=low";

    private const string RedactedQuery = "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=REDACTED&X-Amz-Date=20261111T120000Z&X-Amz-Expires=300&X-Amz-Security-Token=REDACTED&X-Amz-SignedHeaders=host&X-Amz-Signature=REDACTED&token=keep&x-amz-signature=low";

    /// <summary>Server and client spans of one HttpClient GET of a presigned-shaped URL to a local ASP.NET Core app.</summary>
    private static async Task<(Activity Server, Activity Client)> PresignedRoundTripAsync()
    {
        var capture = new Capture();
        var builder = Microsoft.AspNetCore.Builder.WebApplication.CreateBuilder();
        builder.Logging.ClearProviders();
        Microsoft.AspNetCore.Hosting.WebHostBuilderKestrelExtensions.ConfigureKestrel(builder.WebHost, k => k.Listen(IPAddress.Loopback, 0));
        builder.Services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        builder.Services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "c14c-url";
            o.PrometheusMetricsPort = 0;
        });
        builder.Services.ConfigureOpenTelemetryTracerProvider(b => b.AddProcessor(capture));
        var app = builder.Build();
        Microsoft.AspNetCore.Builder.RunExtensions.Run(app, context => Microsoft.AspNetCore.Http.HttpResponseWritingExtensions.WriteAsync(context.Response, "ok"));
        await app.StartAsync();
        try
        {
            var port = new Uri(app.Urls.First()).Port;
            using var http = new HttpClient();
            (await http.GetAsync($"http://127.0.0.1:{port}/orders/c14c.json?{PresignedQuery}")).EnsureSuccessStatusCode();
            var watch = Stopwatch.StartNew();
            Activity? server = null, client = null;
            while ((server is null || client is null) && watch.ElapsedMilliseconds < 5000)
            {
                await Task.Delay(10);
                server = capture.Ended.FirstOrDefault(a => a.Kind == ActivityKind.Server && Equals(a.GetTagItem("server.port"), port));
                client = capture.Ended.FirstOrDefault(a => a.Kind == ActivityKind.Client && Equals(a.GetTagItem("server.port"), port));
            }

            return (Assert.IsType<Activity>(server), Assert.IsType<Activity>(client));
        }
        finally
        {
            await app.StopAsync();
            await app.DisposeAsync();
        }
    }

    [Fact]
    public async Task Upstream_Redaction_Text_Is_Kept_On_Every_Runtime()
    {
        var (server, client) = await PresignedRoundTripAsync();

        // Server url.query (contrib ASP.NET Core): every value "Redacted", on every runtime (O3).
        Assert.Equal("?" + string.Join('&', PresignedQuery.Split('&').Select(p => p.Split('=')[0] + "=Redacted")), server.GetTagItem("url.query"));
        var full = (string)client.GetTagItem("url.full")!;
        if (Environment.Version.Major >= 9)
        {
            // .NET 9+ runtime: the query is "*" (names dropped).
            Assert.EndsWith("/orders/c14c.json?*", full);
        }
        else
        {
            // .NET 8 runtime: the contrib HttpClient instrumentation writes every value as "Redacted".
            Assert.EndsWith("?" + string.Join('&', PresignedQuery.Split('&').Select(p => p.Split('=')[0] + "=Redacted")), full);
        }
    }

    [Fact]
    public async Task Disabled_Upstream_Redaction_Still_Redacts_The_Contract_Names()
    {
        Environment.SetEnvironmentVariable("OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_DISABLE_URL_QUERY_REDACTION", "true");
        Environment.SetEnvironmentVariable("OTEL_DOTNET_EXPERIMENTAL_HTTPCLIENT_DISABLE_URL_QUERY_REDACTION", "true");
        try
        {
            var (server, client) = await PresignedRoundTripAsync();

            Assert.Equal("?" + RedactedQuery, server.GetTagItem("url.query"));
            var full = (string)client.GetTagItem("url.full")!;
            if (Environment.Version.Major >= 9)
            {
                // The contrib switch has no effect on the .NET 9+ runtime: the runtime redacts (its own
                // switch is read once per process, measured in the probe, see HOW-TO §14 "AWS").
                Assert.EndsWith("/orders/c14c.json?*", full);
            }
            else
            {
                Assert.EndsWith("/orders/c14c.json?" + RedactedQuery, full);
            }

            foreach (var span in new[] { server, client })
            {
                var text = Text(span);
                Assert.DoesNotContain("c14csignature", text, StringComparison.Ordinal);
                Assert.DoesNotContain(SessionToken, text, StringComparison.Ordinal);
                Assert.DoesNotContain(AccessKey, text, StringComparison.Ordinal);
            }
        }
        finally
        {
            Environment.SetEnvironmentVariable("OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_DISABLE_URL_QUERY_REDACTION", null);
            Environment.SetEnvironmentVariable("OTEL_DOTNET_EXPERIMENTAL_HTTPCLIENT_DISABLE_URL_QUERY_REDACTION", null);
        }
    }

    [Fact]
    public void AddOtelHelperAws_Refuses_Null()
        => Assert.Throws<ArgumentNullException>(() => OtelAwsExtensions.AddOtelHelperAws(null!));
}
