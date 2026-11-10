using System.Text;

namespace StaffOps.OtelHelper.Diagnostics
{
    /// <summary>Severity of a diagnostic record.</summary>
    internal enum DiagnosticLevel
    {
        Info,
        Warn,
        Error,
    }

    /// <summary>Who produced a record: the helper itself, or the OpenTelemetry SDK (forwarded).</summary>
    internal enum DiagnosticOrigin
    {
        Helper,
        Sdk,
    }

    /// <summary>One formatted record, as written.</summary>
    internal readonly record struct DiagnosticRecord(DiagnosticLevel Level, DiagnosticOrigin Origin, string Line);

    /// <summary>
    /// The helper's one diagnostics channel (design D-NET5): one line per record on
    /// standard error, <c>otelhelper: &lt;LEVEL&gt; &lt;message&gt; key=value…</c>.
    /// Registered in DI by <c>AddOtelHelper</c> (one instance per container, so "once"
    /// is per sink); paths that run before a container exists use <see cref="OtelHelperDiagnostics.Fallback"/>.
    /// </summary>
    internal interface IOtelHelperDiagnostics
    {
        /// <summary>Writes one record. Attribute values are echoed through <see cref="DiagnosticText.Echo"/>; never pass secrets.</summary>
        void Report(DiagnosticLevel level, string message, params (string Key, string Value)[] attributes);

        /// <summary><see cref="Report"/> at most once per sink for <paramref name="onceKey"/>; returns whether it wrote.</summary>
        bool ReportOnce(string onceKey, DiagnosticLevel level, string message, params (string Key, string Value)[] attributes);

        /// <summary>
        /// Writes a forwarded SDK record: the text is redacted, cut at the scan limit and escaped;
        /// with <paramref name="onceKey"/>, at most once per sink.
        /// </summary>
        void ReportSdk(DiagnosticLevel level, string source, string text, string? onceKey = null);
    }

    /// <summary>Default <see cref="IOtelHelperDiagnostics"/>: a writer (standard error by default) plus per-sink "once" keys.</summary>
    internal sealed class OtelHelperDiagnostics : IOtelHelperDiagnostics
    {
        internal const string Prefix = "otelhelper: ";

        private readonly Func<TextWriter> _writer;
        private readonly HashSet<string> _once = new(StringComparer.Ordinal);
        private readonly object _gate = new();

        /// <summary>A sink writing to <paramref name="writer"/>, resolved at each write (default: the current <see cref="Console.Error"/>).</summary>
        internal OtelHelperDiagnostics(Func<TextWriter>? writer = null)
        {
            _writer = writer ?? (() => Console.Error);
        }

        /// <summary>The sink for diagnostics produced before a service provider exists (registration time).</summary>
        internal static OtelHelperDiagnostics Fallback { get; set; } = new();

        /// <summary>Called with every record after it is written (parity driver, tests). Never throws into the caller.</summary>
        internal Action<DiagnosticRecord>? Observer { get; set; }

        public void Report(DiagnosticLevel level, string message, params (string Key, string Value)[] attributes)
            => Write(new DiagnosticRecord(level, DiagnosticOrigin.Helper, Format(level, message, attributes)));

        public bool ReportOnce(string onceKey, DiagnosticLevel level, string message, params (string Key, string Value)[] attributes)
        {
            lock (_gate)
            {
                if (!_once.Add(onceKey))
                {
                    return false;
                }
            }

            Report(level, message, attributes);
            return true;
        }

        public void ReportSdk(DiagnosticLevel level, string source, string text, string? onceKey = null)
        {
            if (onceKey is not null)
            {
                lock (_gate)
                {
                    if (!_once.Add(onceKey))
                    {
                        return;
                    }
                }
            }

            var body = DiagnosticText.EscapeInline(DiagnosticText.RedactText(text));
            var line = $"{Prefix}{LevelWord(level)} sdk {DiagnosticText.EscapeInline(source)}: {body}";
            Write(new DiagnosticRecord(level, DiagnosticOrigin.Sdk, line));
        }

        /// <summary>Formats a helper record. Keys are code constants (<c>[A-Za-z0-9_.]</c>); anything else is a programming error.</summary>
        internal static string Format(DiagnosticLevel level, string message, (string Key, string Value)[] attributes)
        {
            var builder = new StringBuilder(Prefix).Append(LevelWord(level)).Append(' ').Append(DiagnosticText.EscapeInline(message));
            foreach (var (key, value) in attributes)
            {
                if (key.Length == 0 || !key.All(c => char.IsAsciiLetterOrDigit(c) || c == '_' || c == '.'))
                {
                    throw new ArgumentException($"invalid diagnostic attribute key \"{key}\"", nameof(attributes));
                }

                builder.Append(' ').Append(key).Append('=').Append(DiagnosticText.Echo(value));
            }

            return builder.ToString();
        }

        private static string LevelWord(DiagnosticLevel level) => level switch
        {
            DiagnosticLevel.Info => "INFO",
            DiagnosticLevel.Warn => "WARN",
            _ => "ERROR",
        };

        private void Write(DiagnosticRecord record)
        {
            lock (_gate)
            {
                try
                {
                    var writer = _writer();
                    writer.WriteLine(record.Line);
                    writer.Flush();
                }
                catch (Exception ex) when (ex is IOException or ObjectDisposedException)
                {
                    // A closed standard error must not break the application.
                }
            }

            try
            {
                Observer?.Invoke(record);
            }
            catch (Exception)
            {
                // An observer is a test hook; it never breaks the caller.
            }
        }
    }
}
