using System.Diagnostics;
using System.Text.RegularExpressions;

namespace StaffOps.OtelHelper.Tracing
{
    /// <summary>
    /// The AWS rules of the sanitizing processor (task C14c; contract A27, design D-NET14, DN9). They
    /// apply to the spans of the AWS SDK for .NET v4 sources (<c>AWSSDK.&lt;Service&gt;</c>, created
    /// by <c>AWSSDK.Core</c> through <c>OpenTelemetry.Instrumentation.AWS</c> 1.20.3), whoever
    /// subscribed them (the AWS package or the application). Measured 2026-11-11 against a local fake
    /// endpoint (S3, DynamoDB, SNS, SQS) and DynamoDB Local:
    /// <list type="number">
    /// <item><b>Status</b>: a failed call is <c>ERROR</c> with the exception message as description
    /// (upstream <c>AWSTraceSpan.RecordException</c>); the description is removed.</item>
    /// <item><b><c>error.type</c></b> (upstream writes none): the AWS error code (<c>aws.error_code</c>)
    /// when it has the code shape <c>^[A-Z][A-Za-z0-9]{1,63}$</c>; else the modeled exception's name
    /// without <c>Exception</c> (<c>Amazon.SQS.Model.QueueDoesNotExistException</c> →
    /// <c>QueueDoesNotExist</c>, where SQS's code is the dotted
    /// <c>AWS.SimpleQueueService.NonExistentQueue</c>); else, for an error from outside the SDK (a
    /// transport error), its class name (<c>HttpRequestException</c>); else <c>_OTHER</c>. A body-less
    /// S3 <c>HEAD</c> error already has the code <c>NotFound</c>, <c>Forbidden</c>,
    /// <c>ServiceUnavailable</c> (the SDK derives it from the status).</item>
    /// <item><b>Payload attributes</b>: <c>aws.s3.key</c> (written from the request's <c>Key</c>),
    /// <c>aws.s3.copy_source</c>, <c>aws.s3.delete</c> and <c>aws.dynamodb.item_collection_metrics</c>
    /// are removed (the last three are not written at 1.20.3; removed if an enrichment adds them).</item>
    /// <item><b>Exception text</b>: <c>AWSSDK.Core</c> also writes <c>exception.message</c> and
    /// <c>exception.stacktrace</c> as span attributes; both are removed (<c>exception.type</c> kept).
    /// The <c>exception</c> event keeps only <c>exception.type</c> and the detail goes to a correlated
    /// log record (ADR-014, <see cref="DbExceptionEvents"/>): the SDK copies the service's message
    /// verbatim, and DynamoDB quotes expression literals in it
    /// (<c>Invalid UpdateExpression: Syntax error; token: "98765", near: "= 98765"</c>).</item>
    /// </list>
    /// SNS: a publish to a phone number records no destination at 1.20.3 (measured), so no phone
    /// number reaches a span attribute; an SNS error quoting the number reaches only the log record.
    /// </summary>
    internal static class AwsSpanRules
    {
        /// <summary>The source-name prefix of the AWS SDK for .NET v4 spans (the instrumentation subscribes <c>AWSSDK.*</c>).</summary>
        internal const string SourcePrefix = "AWSSDK.";

        internal const string ErrorTypeKey = "error.type";
        internal const string ErrorCodeKey = "aws.error_code";
        internal const string Other = "_OTHER";

        /// <summary>Payload attributes never recorded (contract A27 "AWS payload attributes").</summary>
        internal static readonly string[] DroppedKeys =
        {
            "aws.s3.key", "aws.s3.copy_source", "aws.s3.delete", "aws.dynamodb.item_collection_metrics",
            DbExceptionEvents.MessageKey, DbExceptionEvents.StackTraceKey,
        };

        private static readonly Regex CodeShape = new("^[A-Z][A-Za-z0-9]{1,63}$", RegexOptions.CultureInvariant, TimeSpan.FromMilliseconds(100));

        /// <summary>Whether <paramref name="sourceName"/> is an AWS SDK source these rules cover.</summary>
        internal static bool IsAwsSource(string sourceName) => sourceName.StartsWith(SourcePrefix, StringComparison.Ordinal);

        /// <summary>Applies the rules to a span of an AWS SDK source; other spans are left alone. Idempotent.</summary>
        internal static void Apply(Activity activity)
        {
            if (!IsAwsSource(activity.Source.Name))
            {
                return;
            }

            if (activity.Status == ActivityStatusCode.Error && !string.IsNullOrEmpty(activity.StatusDescription))
            {
                activity.SetStatus(ActivityStatusCode.Error);
            }

            foreach (var key in DroppedKeys)
            {
                if (activity.GetTagItem(key) is not null)
                {
                    activity.SetTag(key, null);
                }
            }

            if (activity.GetTagItem("otel.status_description") is not null)
            {
                activity.SetTag("otel.status_description", null);
            }

            if (activity.Status == ActivityStatusCode.Error)
            {
                var errorType = ErrorType(activity.GetTagItem(ErrorCodeKey) as string, ExceptionType(activity));
                if (!Equals(activity.GetTagItem(ErrorTypeKey), errorType))
                {
                    activity.SetTag(ErrorTypeKey, errorType);
                }
            }
        }

        /// <summary>
        /// The child-span rule (C14c fix round 1): a span of another source whose in-process parent is
        /// an AWS SDK span — the HttpClient span under an AWS call, which <c>AddOtelHelperAws()</c>
        /// suppresses, but which comes back for a client built before the tracer provider or for an
        /// application's own <c>AddAWSInstrumentation()</c> registered first — gets the path of
        /// <c>url.full</c> / <c>http.url</c> replaced with <c>/</c> (scheme, host, port, query and
        /// fragment kept; the query is redacted by the URL rule after this one), <c>url.path</c> written
        /// as <c>/</c> and <c>http.target</c> as <c>/</c> plus its query: the path is the S3 bucket and
        /// object key. The SDK's own internal spans carry no URL (measured: <c>HttpRequest</c> has the
        /// method, status and content lengths, <c>CredentialsRetrieval</c> the RPC keys).
        /// </summary>
        internal static void ApplyToChild(Activity activity)
        {
            if (IsAwsSource(activity.Source.Name) || !HasAwsAncestor(activity))
            {
                return;
            }

            foreach (var key in new[] { "url.full", "http.url", "url.path", "http.target" })
            {
                if (UrlQueryRedaction.TextOf(activity.GetTagItem(key)) is not { } value)
                {
                    continue;
                }

                var stripped = key switch
                {
                    "url.path" => "/",
                    "http.target" => WithoutPath(value, absolute: false),
                    _ => WithoutPath(value, absolute: IsAbsolute(value)),
                };
                if (stripped != value)
                {
                    activity.SetTag(key, stripped);
                }
            }
        }

        /// <summary>The ancestors walked by <see cref="HasAwsAncestor"/> (a DelegatingHandler or resilience span may sit between the AWS span and the HttpClient span).</summary>
        internal const int MaxAncestorDepth = 16;

        /// <summary>Whether an in-process ancestor of <paramref name="activity"/>, up to <see cref="MaxAncestorDepth"/> levels (round 2), is an AWS SDK span.</summary>
        internal static bool HasAwsAncestor(Activity activity)
        {
            var ancestor = activity.Parent;
            for (var depth = 0; ancestor is not null && depth < MaxAncestorDepth; depth++, ancestor = ancestor.Parent)
            {
                if (IsAwsSource(ancestor.Source.Name))
                {
                    return true;
                }
            }

            return false;
        }

        /// <summary>Whether <paramref name="url"/> is absolute: <c>://</c> before the first <c>/</c>, <c>?</c> or <c>#</c> (a query holding a URL does not count, round 2).</summary>
        internal static bool IsAbsolute(string url)
        {
            var scheme = url.IndexOf("://", StringComparison.Ordinal);
            var first = url.IndexOfAny(new[] { '/', '?', '#' });
            return scheme > 0 && (first < 0 || scheme < first);
        }

        /// <summary><paramref name="url"/> with its path replaced by <c>/</c>: the scheme and authority of an absolute URL, then <c>/</c>, then everything from the first <c>?</c> or <c>#</c>.</summary>
        internal static string WithoutPath(string url, bool absolute)
        {
            var start = 0;
            if (absolute)
            {
                var scheme = url.IndexOf("://", StringComparison.Ordinal) + 3;
                start = url.IndexOfAny(new[] { '/', '?', '#' }, scheme);
                if (start < 0)
                {
                    return url + "/";
                }
            }

            var rest = url.IndexOfAny(new[] { '?', '#' }, start);
            return string.Concat(url.AsSpan(0, start), "/", rest < 0 ? ReadOnlySpan<char>.Empty : url.AsSpan(rest));
        }

        /// <summary>
        /// The <c>error.type</c> of a failed AWS call from its AWS error code and the full name of its
        /// exception type (either may be null); see the class summary.
        /// </summary>
        internal static string ErrorType(string? code, string? exceptionType)
        {
            if (code is not null && CodeShape.IsMatch(code))
            {
                return code;
            }

            if (exceptionType is null)
            {
                return Other;
            }

            var dot = exceptionType.LastIndexOf('.');
            var name = dot < 0 ? exceptionType : exceptionType[(dot + 1)..];
            var ns = dot < 0 ? string.Empty : exceptionType[..dot];
            if (ns.StartsWith("Amazon.", StringComparison.Ordinal))
            {
                // A modeled service error (Amazon.<Service>.Model.<Name>Exception); a generic SDK error
                // (AmazonS3Exception, AmazonUnmarshallingException) names no AWS error.
                if (ns.EndsWith(".Model", StringComparison.Ordinal) && name.EndsWith("Exception", StringComparison.Ordinal))
                {
                    var modeled = name[..^"Exception".Length];
                    return CodeShape.IsMatch(modeled) ? modeled : Other;
                }

                return Other;
            }

            // An error from outside the SDK (transport, cancellation): its class name.
            return name.Length > 0 && CodeShape.IsMatch(name) ? name : Other;
        }

        /// <summary>The exception type of the span: its <c>exception.type</c> attribute, else that of its first <c>exception</c> event.</summary>
        private static string? ExceptionType(Activity activity)
        {
            if (activity.GetTagItem(DbExceptionEvents.TypeKey) is string type)
            {
                return type;
            }

            foreach (ref readonly var e in activity.EnumerateEvents())
            {
                if (e.Name == DbExceptionEvents.EventName && DbExceptionEvents.Tag(e, DbExceptionEvents.TypeKey) is { } eventType)
                {
                    return eventType;
                }
            }

            return null;
        }
    }
}
