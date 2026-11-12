using System.Diagnostics;
using System.Reflection;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper.Tracing
{
    /// <summary>
    /// The helper's one sanitizing processor (design D-NET14, DN9): added by <c>AddOtelHelper</c>
    /// when the tracer provider is built, before the helper's export processors, so every processor
    /// registered after <c>AddOtelHelper</c> sees the sanitized span. It is added also when the
    /// helper exports no traces, for an exporter the application adds itself. The helper's own
    /// exporters run the same rules again on each batch (<see cref="Sanitize"/>): a driver can change
    /// a span after it ended — MySqlConnector 2.6 sets the status and <c>error.type</c> of a failed
    /// command after stopping its activity (<c>MySqlDataReader.Dispose</c>, then
    /// <c>CommandExecutor</c>'s catch) — which no processor sees. Rules: <see cref="DbSpanRules"/>
    /// (task C14a), <see cref="AwsSpanRules"/> and <see cref="UrlQueryRedaction"/> (task C14c); they
    /// are idempotent. A rule that throws never lets the raw values through: the span's sensitive
    /// attributes are removed and one warning names the exception type.
    /// </summary>
    internal sealed class SanitizingProcessor : BaseProcessor<Activity>
    {
        private readonly IOtelHelperDiagnostics _diagnostics;

        /// <summary>A processor for <paramref name="diagnostics"/>; the database key mode is read from the environment now.</summary>
        internal SanitizingProcessor(IOtelHelperDiagnostics diagnostics)
            : this(diagnostics, DbSpanRules.LegacyKeysEnabled(Environment.GetEnvironmentVariable(SemconvOptInVariable)))
        {
        }

        internal SanitizingProcessor(IOtelHelperDiagnostics diagnostics, bool legacyDbKeys)
        {
            _diagnostics = diagnostics;
            LegacyDbKeys = legacyDbKeys;
        }

        internal const string SemconvOptInVariable = "OTEL_SEMCONV_STABILITY_OPT_IN";

        /// <summary>Whether the legacy database keys are emitted (<see cref="DbSpanRules.LegacyKeysEnabled"/>), read once when the provider is built.</summary>
        internal bool LegacyDbKeys { get; }

        /// <summary>Test seam: replaces the rules (null = <see cref="DbSpanRules.Apply"/> with <see cref="LegacyDbKeys"/>).</summary>
        internal Action<Activity>? Rules { get; init; }

        /// <summary>The logger of the ADR-014 records (<see cref="Logging.DbExceptionLog.Category"/>), resolved on first use; null = no record.</summary>
        internal Func<ILogger?> ExceptionLogger { get; init; } = () => null;

        /// <summary>The logger of the ADR-014 records of AWS SDK spans (<see cref="Logging.DbExceptionLog.AwsCategory"/>, task C14c), resolved on first use; null = no record.</summary>
        internal Func<ILogger?> AwsExceptionLogger { get; init; } = () => null;

        /// <summary>Test seam: the private event list of <see cref="Activity"/> (null = this runtime has none).</summary>
        internal FieldInfo? EventsField { get; init; } = DbExceptionEvents.ActivityEventsField;

        public override void OnEnd(Activity data)
        {
            if (data.IsAllDataRequested)
            {
                Apply(data);
            }
        }

        /// <summary>The batch an exporter gets: the same spans, each sanitized again (read once: a batch of a batch processor drains its queue).</summary>
        internal Batch<Activity> Sanitize(in Batch<Activity> batch)
        {
            var items = new List<Activity>();
            foreach (var activity in batch)
            {
                Apply(activity);
                items.Add(activity);
            }

            return new Batch<Activity>(items.ToArray(), items.Count);
        }

        private void Apply(Activity data)
        {
            try
            {
                if (Rules is { } rules)
                {
                    rules(data);
                }
                else
                {
                    DbSpanRules.Apply(data, LegacyDbKeys);
                    AwsSpanRules.Apply(data);
                    AwsSpanRules.ApplyToChild(data);
                    UrlQueryRedaction.Apply(data);
                }

                var aws = AwsSpanRules.IsAwsSource(data.Source.Name);
                if ((aws || DbSpanRules.IsDbSource(data.Source.Name))
                    && DbExceptionEvents.Handle(data, EventsField, (span, e) => Log(aws, span, e)) == DbExceptionEvents.Outcome.Unsupported)
                {
                    _diagnostics.ReportOnce("exception-events-unsupported", DiagnosticLevel.Warn,
                        "cannot rewrite span events on this runtime (System.Diagnostics.Activity has no _events field); database and AWS exception events keep their message and stack trace (ADR-014)");
                }
            }
            catch (Exception e)
            {
                DbSpanRules.Strip(data);
                try
                {
                    // Fail closed: the exception detail leaves the span even without its log record.
                    DbExceptionEvents.Handle(data, EventsField, (_, _) => { });
                }
                catch (Exception)
                {
                    // Nothing more can be removed; the warning below is written either way.
                }

                _diagnostics.ReportOnce("sanitizer-failed", DiagnosticLevel.Warn,
                    "span sanitizer failed; the span's status description, query text, connection, URL and AWS payload attributes were removed",
                    ("source", data.Source.Name), ("error", e.GetType().Name));
            }
        }

        private void Log(bool aws, Activity span, ActivityEvent e)
        {
            if ((aws ? AwsExceptionLogger() : ExceptionLogger()) is { } logger)
            {
                Logging.DbExceptionLog.Write(logger, span, e, aws ? Logging.DbExceptionLog.AwsFallbackBody : Logging.DbExceptionLog.DbFallbackBody);
            }
        }
    }

    /// <summary>The helper's OTLP trace exporter: each batch through <see cref="SanitizingProcessor.Sanitize"/> first.</summary>
    internal sealed class SanitizingOtlpTraceExporter : OtlpTraceExporter
    {
        private readonly SanitizingProcessor _sanitizer;

        internal SanitizingOtlpTraceExporter(OtlpExporterOptions options, SanitizingProcessor sanitizer)
            : base(options)
        {
            _sanitizer = sanitizer;
        }

        public override ExportResult Export(in Batch<Activity> activityBatch) => base.Export(_sanitizer.Sanitize(activityBatch));
    }

    /// <summary>The helper's console trace exporter: each batch through <see cref="SanitizingProcessor.Sanitize"/> first.</summary>
    internal sealed class SanitizingConsoleActivityExporter : ConsoleActivityExporter
    {
        private readonly SanitizingProcessor _sanitizer;

        internal SanitizingConsoleActivityExporter(ConsoleExporterOptions options, SanitizingProcessor sanitizer)
            : base(options)
        {
            _sanitizer = sanitizer;
        }

        public override ExportResult Export(in Batch<Activity> batch) => base.Export(_sanitizer.Sanitize(batch));
    }
}
