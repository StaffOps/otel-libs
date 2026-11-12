using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry.Trace;

namespace StaffOps.OtelHelper.AWS
{
    /// <summary>
    /// Extension methods to register AWS instrumentation as an opt-in subpackage.
    /// </summary>
    public static class OtelAwsExtensions
    {
        /// <summary>
        /// Adds AWS SDK for .NET v4 instrumentation (<c>OpenTelemetry.Instrumentation.AWS</c>) to the
        /// StaffOps.OtelHelper tracing pipeline: one CLIENT span per AWS call, named
        /// <c>&lt;Service&gt;.&lt;Operation&gt;</c>, from the <c>AWSSDK.&lt;Service&gt;</c> sources.
        /// </summary>
        /// <remarks>
        /// <para>
        /// Downstream instrumentation is suppressed inside an AWS call
        /// (<c>SuppressDownstreamInstrumentation</c>, task C14c): without it each call also gives an
        /// HttpClient span whose <c>url.full</c> carries the S3 object key in its path, and the SDK's
        /// internal <c>HttpRequest</c> / <c>CredentialsRetrieval</c> spans (measured 2026-11-11).
        /// </para>
        /// <para>
        /// The core sanitizing processor applies the contract A27 rules to these spans, also when the
        /// application subscribes the sources itself: status <c>ERROR</c> without description,
        /// <c>error.type</c> from the AWS error code, no <c>aws.s3.key</c> /
        /// <c>aws.s3.copy_source</c> / <c>aws.s3.delete</c> / <c>aws.dynamodb.item_collection_metrics</c>,
        /// no <c>exception.message</c> / <c>exception.stacktrace</c>, and the exception detail in a
        /// correlated log record of category <c>StaffOps.OtelHelper.Aws</c> (ADR-014).
        /// </para>
        /// </remarks>
        public static IServiceCollection AddOtelHelperAws(this IServiceCollection services)
        {
            ArgumentNullException.ThrowIfNull(services);
            services.ConfigureOpenTelemetryTracerProvider(builder =>
                builder.AddAWSInstrumentation(o => o.SuppressDownstreamInstrumentation = true));

            return services;
        }
    }
}
