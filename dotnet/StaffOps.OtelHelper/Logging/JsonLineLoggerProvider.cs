using System;
using System.Buffers;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Threading;
using Microsoft.Extensions.Logging;

namespace StaffOps.OtelHelper.Logging
{
    /// <summary>
    /// The stdout fallback of design D-NET10 (contract D6, A23): while no logs exporter is active
    /// (no OTLP endpoint, <c>OTEL_LOGS_EXPORTER=none</c>, logs disabled, <c>OTEL_SDK_DISABLED</c>),
    /// every record becomes one JSON object on one line of standard output, the line Go and
    /// Python write: <c>time</c> (UTC, milliseconds, <c>Z</c>), <c>level</c>
    /// (<c>DEBUG</c>/<c>INFO</c>/<c>WARN</c>/<c>ERROR</c>), <c>msg</c>, <c>logger</c> (the
    /// category), the state values, and <c>exception</c>. Registered by <c>AddOtelHelper</c>
    /// next to the OpenTelemetry provider; the level rules of <see cref="LoggingSetup"/> apply to
    /// it, and it writes nothing while a logs exporter is active.
    /// </summary>
    [ProviderAlias(Alias)]
    internal sealed class JsonLineLoggerProvider : ILoggerProvider
    {
        /// <summary>Provider alias for <c>Logging:&lt;alias&gt;:LogLevel</c> configuration.</summary>
        internal const string Alias = "OtelHelperStdout";

        private readonly Lazy<bool> _active;

        /// <summary>Active when the resolved state has no logs exporter (decided once per container).</summary>
        public JsonLineLoggerProvider(IServiceProvider services)
            : this(() => ResolvedAccess.TryGet(services, out var resolved) && !LoggingSetup.LogsExporterActive(resolved))
        {
        }

        internal JsonLineLoggerProvider(Func<bool> active)
            => _active = new Lazy<bool>(() =>
            {
                try
                {
                    return active();
                }
                catch (Exception)
                {
                    // Resolution failed in a way ResolvedAccess does not cover: write nothing.
                    return false;
                }
            }, LazyThreadSafetyMode.ExecutionAndPublication);

        /// <summary>Where lines go: the current <see cref="Console.Out"/>, read per record (tests replace it).</summary>
        internal Func<TextWriter> Output { get; set; } = () => Console.Out;

        /// <summary>The record time (tests replace it).</summary>
        internal Func<DateTimeOffset> Clock { get; set; } = () => DateTimeOffset.UtcNow;

        /// <summary>The encoding stdout is written with: the current <see cref="Console.OutputEncoding"/>, read per record (tests replace it).</summary>
        internal Func<Encoding> OutputEncoding { get; set; } = () => Console.OutputEncoding;

        internal bool Active => _active.Value;

        public ILogger CreateLogger(string categoryName) => new JsonLineLogger(categoryName, this);

        public void Dispose()
        {
        }
    }

    /// <summary>One category of <see cref="JsonLineLoggerProvider"/>.</summary>
    internal sealed class JsonLineLogger : ILogger
    {
        private readonly string _category;
        private readonly JsonLineLoggerProvider _provider;

        internal JsonLineLogger(string category, JsonLineLoggerProvider provider)
        {
            _category = category;
            _provider = provider;
        }

        /// <summary>Scopes are not written on the line (Go and Python have none); the OTLP path keeps them.</summary>
        public IDisposable? BeginScope<TState>(TState state)
            where TState : notnull => null;

        public bool IsEnabled(LogLevel logLevel) => logLevel != LogLevel.None && _provider.Active;

        public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter)
        {
            if (!IsEnabled(logLevel))
            {
                return;
            }

            // One Write call per record: Console.Out is synchronized, so concurrent lines never interleave.
            try
            {
                string line;
                try
                {
                    var time = _provider.Clock();
                    var context = JsonLine.SpanContext(_category, state, ref time);
                    line = JsonLine.Format(time, logLevel, _category, state, exception, formatter,
                        JsonLine.RawSupplementaryFor(_provider.OutputEncoding()), context);
                }
                catch (Exception)
                {
                    // Format guards every value; this is the last resort (logging never throws).
                    line = JsonLine.Minimal(logLevel);
                }

                _provider.Output().Write(line);
            }
            catch (Exception)
            {
                // A closed or broken stdout must not fail the caller (logging never throws).
            }
        }
    }

    /// <summary>The contract line (contract design D1 rows "Stdout fallback time zone/fields", A23).</summary>
    internal static class JsonLine
    {
        /// <summary>The line's own fields: an attribute with one of these names is dropped (A23 (a)).</summary>
        internal static readonly IReadOnlyCollection<string> ReservedKeys = new[] { "time", "level", "msg", "logger" };

        /// <summary>Items written for one collection value; more become a final <c>"…"</c> (an endless sequence cannot hang the logger).</summary>
        internal const int MaxItems = 1000;

        private const string ExceptionKey = "exception";
        private const string TraceIdKey = "trace_id";
        private const string SpanIdKey = "span_id";

        /// <summary>
        /// The span context of a line (contract A23 amendment 2026-11-11): a database exception record
        /// (ADR-014) names its span and event time in its attributes; any other record emitted while a
        /// span is current (<see cref="System.Diagnostics.Activity.Current"/>) gets that span's ids.
        /// </summary>
        internal static (string TraceId, string SpanId)? SpanContext<TState>(string category, TState state, ref DateTimeOffset time)
        {
            if (DbExceptionLog.TryReadContext(category, Attributes(state), out var traceId, out var spanId, out var eventTime))
            {
                if (eventTime is { } at)
                {
                    time = new DateTimeOffset(at, TimeSpan.Zero);
                }

                return (traceId.ToHexString(), spanId.ToHexString());
            }

            return System.Diagnostics.Activity.Current is { } current && current.TraceId != default && current.SpanId != default
                ? (current.TraceId.ToHexString(), current.SpanId.ToHexString())
                : null;
        }

        private const string OriginalFormatKey = "{OriginalFormat}";

        // No HTML escaping (a log line is not HTML): non-ASCII text stays readable, as in Go and
        // Python; quotes, backslashes and control characters (so line breaks) are escaped. The
        // encoder escapes every character outside the BMP as a surrogate pair; RawSupplementary
        // writes those back as UTF-8, as Go and Python do.
        private static readonly JsonWriterOptions WriterOptions = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

        /// <summary>The <c>level</c> word of a record: the four words Go, Python and Node.js write (Critical → ERROR, as Python).</summary>
        internal static string LevelWord(LogLevel level) => level switch
        {
            LogLevel.Trace or LogLevel.Debug => "DEBUG",
            LogLevel.Information => "INFO",
            LogLevel.Warning => "WARN",
            _ => "ERROR",
        };

        /// <summary>RFC 3339 in UTC with milliseconds and <c>Z</c>, whatever the process zone.</summary>
        internal static string Time(DateTimeOffset time)
            => time.UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture);

        /// <summary>
        /// One JSON object and a line feed. A value that cannot be rendered becomes text, a lone
        /// surrogate U+FFFD. Not guarded: a <c>ToString()</c> that recurses without end (a stack
        /// overflow cannot be caught in .NET).
        /// </summary>
        internal static string Format<TState>(DateTimeOffset time, LogLevel level, string category, TState state, Exception? exception, Func<TState, Exception?, string>? formatter, bool rawSupplementary = true, (string TraceId, string SpanId)? context = null)
        {
            var buffer = new ArrayBufferWriter<byte>(256);
            using (var writer = new Utf8JsonWriter(buffer, WriterOptions))
            {
                writer.WriteStartObject();
                writer.WriteString("time", Time(time));
                writer.WriteString("level", LevelWord(level));
                writer.WriteString("msg", Clean(Message(state, exception, formatter)));
                writer.WriteString("logger", Clean(category));

                var seen = new HashSet<string>(ReservedKeys, StringComparer.Ordinal);
                if (context is { } ids)
                {
                    // A23 amendment (2026-11-11): the span context right after logger; an attribute of
                    // either name is then dropped (no duplicate key).
                    writer.WriteString(TraceIdKey, ids.TraceId);
                    writer.WriteString(SpanIdKey, ids.SpanId);
                    seen.Add(TraceIdKey);
                    seen.Add(SpanIdKey);
                }

                if (DbExceptionLog.IsRecordCategory(category))
                {
                    seen.Add(DbExceptionLog.EventTimeKey);
                }

                if (exception is not null)
                {
                    // The record's exception is the field (as Python's exc_info); an attribute of that name is dropped.
                    seen.Add(ExceptionKey);
                }

                foreach (var (rawKey, value) in Attributes(state))
                {
                    var key = Clean(rawKey);
                    // {OriginalFormat} is the template, not a value; the first of duplicate names wins.
                    if (key == OriginalFormatKey || !seen.Add(key))
                    {
                        continue;
                    }

                    writer.WritePropertyName(key);
                    WriteValue(writer, value, nested: false);
                }

                if (exception is not null)
                {
                    writer.WriteString(ExceptionKey, Clean(Text(exception)));
                }

                writer.WriteEndObject();
            }

            var json = Encoding.UTF8.GetString(buffer.WrittenSpan);
            return (rawSupplementary ? RawSupplementary(json) : json) + "\n";
        }

        /// <summary>
        /// Whether characters outside the BMP are written raw: only when stdout is UTF-8 (code page
        /// 65001). Another console encoding would turn them into <c>?</c>; the escaped form keeps them.
        /// </summary>
        internal static bool RawSupplementaryFor(Encoding? encoding) => encoding?.CodePage == 65001;

        /// <summary>The line without the record's content, when even <see cref="Format"/> failed.</summary>
        internal static string Minimal(LogLevel level)
            => "{\"time\":\"" + Time(DateTimeOffset.UtcNow) + "\",\"level\":\"" + LevelWord(level) + "\",\"msg\":\"[unrenderable record]\"}\n";

        // An escaped surrogate pair the encoder wrote; an even run of backslashes before it is text.
        private static readonly System.Text.RegularExpressions.Regex EscapedPair = new(
            @"(?<!\\)((?:\\\\)*)\\u(D[89AB][0-9A-F]{2})\\u(D[C-F][0-9A-F]{2})",
            System.Text.RegularExpressions.RegexOptions.IgnoreCase | System.Text.RegularExpressions.RegexOptions.CultureInvariant);

        /// <summary>Characters outside the BMP as UTF-8 instead of <c>\uD83D\uDE00</c> escapes (still valid JSON).</summary>
        internal static string RawSupplementary(string json)
            => json.Contains("\\uD", StringComparison.OrdinalIgnoreCase)
                ? EscapedPair.Replace(json, m => m.Groups[1].Value
                    + (char)Convert.ToInt32(m.Groups[2].Value, 16) + (char)Convert.ToInt32(m.Groups[3].Value, 16))
                : json;

        /// <summary>A lone surrogate (invalid UTF-16) replaced by U+FFFD; the writer with this encoder does the same (measured), this keeps it encoder-independent.</summary>
        internal static string Clean(string text)
        {
            for (var i = 0; i < text.Length; i++)
            {
                if (char.IsSurrogate(text[i]) && !(char.IsHighSurrogate(text[i]) && i + 1 < text.Length && char.IsLowSurrogate(text[i + 1])))
                {
                    return Replace(text);
                }

                if (char.IsHighSurrogate(text[i]))
                {
                    i++;
                }
            }

            return text;

            static string Replace(string text)
            {
                var builder = new StringBuilder(text.Length);
                for (var i = 0; i < text.Length; i++)
                {
                    if (char.IsHighSurrogate(text[i]) && i + 1 < text.Length && char.IsLowSurrogate(text[i + 1]))
                    {
                        builder.Append(text[i]).Append(text[++i]);
                    }
                    else
                    {
                        builder.Append(char.IsSurrogate(text[i]) ? '\uFFFD' : text[i]);
                    }
                }

                return builder.ToString();
            }
        }

        private static string Message<TState>(TState state, Exception? exception, Func<TState, Exception?, string>? formatter)
        {
            if (formatter is null)
            {
                return Text(state);
            }

            try
            {
                return formatter(state, exception) ?? string.Empty;
            }
            catch (Exception)
            {
                return Text(state);
            }
        }

        /// <summary>The state's name/value pairs, read up to the first failure.</summary>
        private static List<KeyValuePair<string, object?>> Attributes<TState>(TState state)
        {
            var pairs = new List<KeyValuePair<string, object?>>();
            if (state is not IEnumerable<KeyValuePair<string, object?>> values)
            {
                return pairs;
            }

            try
            {
                foreach (var pair in values)
                {
                    if (pair.Key is not null)
                    {
                        pairs.Add(pair);
                    }
                }
            }
            catch (Exception)
            {
                // A state whose enumeration throws keeps what was read.
            }

            return pairs;
        }

        private static void WriteValue(Utf8JsonWriter writer, object? value, bool nested)
        {
            switch (value)
            {
                case null:
                    writer.WriteNullValue();
                    return;
                case string s:
                    writer.WriteStringValue(Clean(s));
                    return;
                case bool b:
                    writer.WriteBooleanValue(b);
                    return;
                case byte or sbyte or short or ushort or int:
                    writer.WriteNumberValue(Convert.ToInt32(value, CultureInfo.InvariantCulture));
                    return;
                case uint u:
                    writer.WriteNumberValue(u);
                    return;
                case long l:
                    writer.WriteNumberValue(l);
                    return;
                case ulong ul:
                    writer.WriteNumberValue(ul);
                    return;
                case decimal m:
                    writer.WriteNumberValue(m);
                    return;
                case float f:
                    // Non-finite values as text: the line stays valid JSON (contract ruling, A23).
                    if (float.IsFinite(f))
                    {
                        writer.WriteNumberValue(f);
                    }
                    else
                    {
                        writer.WriteStringValue(f.ToString(CultureInfo.InvariantCulture));
                    }

                    return;
                case double d:
                    if (double.IsFinite(d))
                    {
                        writer.WriteNumberValue(d);
                    }
                    else
                    {
                        writer.WriteStringValue(d.ToString(CultureInfo.InvariantCulture));
                    }

                    return;
                case DateTime dt:
                    writer.WriteStringValue(dt.ToString("O", CultureInfo.InvariantCulture));
                    return;
                case DateTimeOffset dto:
                    writer.WriteStringValue(dto.ToString("O", CultureInfo.InvariantCulture));
                    return;
                case IEnumerable items when !nested && value is not IDictionary && Items(items) is { } list:
                    writer.WriteStartArray();
                    foreach (var item in list)
                    {
                        WriteValue(writer, item, nested: true);
                    }

                    writer.WriteEndArray();
                    return;
                default:
                    writer.WriteStringValue(Clean(Text(value)));
                    return;
            }
        }

        /// <summary>The first <see cref="MaxItems"/> items of a collection (then <c>"…"</c>), or null when enumerating it throws (it is then written as text).</summary>
        private static List<object?>? Items(IEnumerable items)
        {
            try
            {
                var list = new List<object?>();
                foreach (var item in items)
                {
                    if (list.Count == MaxItems)
                    {
                        list.Add("…");
                        break;
                    }

                    list.Add(item);
                }

                return list;
            }
            catch (Exception)
            {
                return null;
            }
        }

        /// <summary><c>ToString()</c> (invariant culture) that never throws.</summary>
        internal static string Text(object? value)
        {
            if (value is null)
            {
                return string.Empty;
            }

            try
            {
                return Convert.ToString(value, CultureInfo.InvariantCulture) ?? string.Empty;
            }
            catch (Exception)
            {
                return "[unprintable " + value.GetType().Name + "]";
            }
        }
    }
}
