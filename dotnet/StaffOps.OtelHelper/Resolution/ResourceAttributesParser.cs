using System.Text;

namespace StaffOps.OtelHelper
{
    /// <summary>Result of <see cref="ResourceAttributesParser.Parse"/>.</summary>
    internal sealed class ParsedResourceAttributes
    {
        internal static readonly ParsedResourceAttributes Empty = new(new Dictionary<string, string>(StringComparer.Ordinal), Array.Empty<int>(), Array.Empty<int>());

        internal ParsedResourceAttributes(IReadOnlyDictionary<string, string> attributes, IReadOnlyList<int> malformed, IReadOnlyList<int> undecodable)
        {
            Attributes = attributes;
            Malformed = malformed;
            Undecodable = undecodable;
        }

        /// <summary>Valid <c>key=value</c> pairs, values decoded and trimmed; a later duplicate key wins.</summary>
        internal IReadOnlyDictionary<string, string> Attributes { get; }

        /// <summary>1-based positions of items without <c>=</c>, with an empty key or with a key that is not valid UTF-16/UTF-8 (skipped).</summary>
        internal IReadOnlyList<int> Malformed { get; }

        /// <summary>1-based positions whose value has an invalid percent-encoding or is not UTF-8 once decoded (kept literally).</summary>
        internal IReadOnlyList<int> Undecodable { get; }
    }

    /// <summary>
    /// The helper's own <c>OTEL_RESOURCE_ATTRIBUTES</c> parser (contract design D1 row
    /// "OTEL_RESOURCE_ATTRIBUTES parsing", A16). The SDK's <c>OtelEnvResourceDetector</c> is not
    /// the contract: it trims Unicode whitespace, decodes <c>+</c> as a space and accepts an
    /// empty key. Rules: split on <c>,</c>; a blank item is ignored silently; each item splits
    /// at its first <c>=</c>; the key is trimmed with the contract set (empty, or holding an
    /// unpaired surrogate — what an invalid UTF-8 byte can become — the item is malformed and
    /// skipped); the value is strictly percent-decoded (<c>%XX</c> only, the bytes UTF-8), then
    /// trimmed; an invalid escape or a decode that is not UTF-8 keeps the value literal (still
    /// trimmed). Callers report positions only: values never reach diagnostics (R8).
    /// </summary>
    internal static class ResourceAttributesParser
    {
        private static readonly UTF8Encoding StrictUtf8 = new(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);

        internal static ParsedResourceAttributes Parse(string raw)
        {
            var attributes = new Dictionary<string, string>(StringComparer.Ordinal);
            var malformed = new List<int>();
            var undecodable = new List<int>();
            var items = raw.Split(',');
            for (var i = 0; i < items.Length; i++)
            {
                var item = items[i];
                if (ContractText.Trim(item).Length == 0)
                {
                    continue;
                }

                var equals = item.IndexOf('=');
                var key = equals < 0 ? "" : ContractText.Trim(item[..equals]);
                if (key.Length == 0 || HasUnpairedSurrogate(key))
                {
                    malformed.Add(i + 1);
                    continue;
                }

                var value = item[(equals + 1)..];
                if (TryPercentDecode(value) is { } decoded)
                {
                    value = decoded;
                }
                else
                {
                    undecodable.Add(i + 1);
                    // OTLP strings must be UTF-8: an unpaired surrogate becomes U+FFFD (the
                    // replacing encoder), as Go and Python replace an invalid byte.
                    value = Encoding.UTF8.GetString(Encoding.UTF8.GetBytes(value));
                }

                attributes[key] = ContractText.Trim(value);
            }

            return new ParsedResourceAttributes(attributes, malformed, undecodable);
        }

        /// <summary>
        /// Strict percent-decoding (<c>decodeURIComponent</c>, Go <c>url.PathUnescape</c>): every
        /// <c>%</c> must be followed by two hex digits, <c>+</c> stays a plus, and the decoded bytes
        /// must be UTF-8. Null when the value cannot be decoded.
        /// </summary>
        internal static string? TryPercentDecode(string value)
        {
            if (value.IndexOf('%') < 0)
            {
                return HasUnpairedSurrogate(value) ? null : value;
            }

            var bytes = new List<byte>(value.Length);
            var runStart = 0;
            for (var i = 0; i < value.Length; i++)
            {
                if (value[i] != '%')
                {
                    continue;
                }

                if (i + 2 >= value.Length || !IsHex(value[i + 1]) || !IsHex(value[i + 2]))
                {
                    return null;
                }

                if (!AppendUtf8(bytes, value, runStart, i))
                {
                    return null;
                }

                bytes.Add((byte)((HexValue(value[i + 1]) << 4) | HexValue(value[i + 2])));
                i += 2;
                runStart = i + 1;
            }

            if (!AppendUtf8(bytes, value, runStart, value.Length))
            {
                return null;
            }

            try
            {
                return StrictUtf8.GetString(bytes.ToArray());
            }
            catch (DecoderFallbackException)
            {
                return null;
            }
        }

        private static bool AppendUtf8(List<byte> bytes, string value, int start, int end)
        {
            if (end <= start)
            {
                return true;
            }

            var run = value[start..end];
            if (HasUnpairedSurrogate(run))
            {
                return false;
            }

            bytes.AddRange(StrictUtf8.GetBytes(run));
            return true;
        }

        private static bool HasUnpairedSurrogate(string value)
        {
            for (var i = 0; i < value.Length; i++)
            {
                if (char.IsHighSurrogate(value[i]))
                {
                    if (i + 1 >= value.Length || !char.IsLowSurrogate(value[i + 1]))
                    {
                        return true;
                    }

                    i++;
                }
                else if (char.IsLowSurrogate(value[i]))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool IsHex(char c) => c is >= '0' and <= '9' or >= 'a' and <= 'f' or >= 'A' and <= 'F';

        private static int HexValue(char c) => c <= '9' ? c - '0' : (c | 0x20) - 'a' + 10;
    }
}
