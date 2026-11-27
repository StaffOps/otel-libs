namespace StaffOps.OtelHelper
{
    /// <summary>
    /// Text rules shared by every language of the contract (design D1): the trim set is ASCII
    /// whitespace only (space, tab, LF, CR, VT, FF), never Unicode whitespace, and enum-like
    /// names fold A-Z only (row "ASCII case folding", A20).
    /// </summary>
    internal static class ContractText
    {
        /// <summary>The contract trim set: Go <c>wsCutset</c>, Python <c>TRIM_CHARS</c>.</summary>
        internal static readonly char[] TrimSet = { ' ', '\t', '\n', '\r', '\v', '\f' };

        /// <summary>Trims the contract set only (a leading U+00A0 stays).</summary>
        internal static string Trim(string value) => value.Trim(TrimSet);

        /// <summary>The value trimmed with the contract set, or null when it is null or blank.</summary>
        internal static string? NonBlank(string? value)
        {
            if (value is null)
            {
                return null;
            }

            var trimmed = Trim(value);
            return trimmed.Length == 0 ? null : trimmed;
        }

        /// <summary>Lower-cases A-Z only (no culture, no non-ASCII folding).</summary>
        internal static string AsciiLower(string value)
            => string.Create(value.Length, value, static (span, source) =>
            {
                for (var i = 0; i < source.Length; i++)
                {
                    var c = source[i];
                    span[i] = c is >= 'A' and <= 'Z' ? (char)(c + ('a' - 'A')) : c;
                }
            });

        /// <summary>Upper-cases A-Z only (no culture, no non-ASCII folding).</summary>
        internal static string AsciiUpper(string value)
            => string.Create(value.Length, value, static (span, source) =>
            {
                for (var i = 0; i < source.Length; i++)
                {
                    var c = source[i];
                    span[i] = c is >= 'a' and <= 'z' ? (char)(c - ('a' - 'A')) : c;
                }
            });
    }
}
