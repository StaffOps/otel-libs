using System.Text;

namespace StaffOps.OtelHelper.Tracing
{
    /// <summary>The SQL lexical rules <see cref="DbQuerySanitizer"/> follows.</summary>
    internal enum SqlDialect
    {
        /// <summary>
        /// PostgreSQL: <c>"…"</c> is an identifier, <c>$tag$…$tag$</c> a string, <c>$1</c> a
        /// parameter; a backslash escapes only inside <c>E'…'</c>; block comments nest.
        /// </summary>
        PostgreSql,

        /// <summary>
        /// MySQL / MariaDB: <c>"…"</c> is a string (the default SQL mode), <c>`…`</c> an identifier,
        /// a backslash escapes inside every quoted string, <c>#</c> starts a line comment.
        /// </summary>
        MySql,
    }

    /// <summary>
    /// Replaces the literals of a SQL statement with <c>?</c> and drops its comments (task C14a,
    /// contract A25/R10: no value reaches a span attribute). Semantic conventions: <c>db.query.text</c>
    /// is collected by default only when it is sanitized. String literals (with their prefixes:
    /// PostgreSQL <c>E</c>, <c>B</c>, <c>X</c>, <c>N</c>, <c>U&amp;</c>; MySQL <c>X</c>, <c>B</c>,
    /// <c>N</c> and charset introducers such as <c>_utf8mb4</c>), dollar-quoted strings, MySQL
    /// double-quoted strings and numbers (decimal, exponent, <c>0x</c>/<c>0b</c>/<c>0o</c>,
    /// underscores) become <c>?</c>; identifiers, keywords, operators and parameters
    /// (<c>$1</c>, <c>@p0</c>, <c>?</c>, <c>:name</c>) are kept. Fails closed: whatever follows an
    /// unterminated literal or comment is dropped, the literal itself becomes <c>?</c>.
    /// Written from the lexical rules of the two dialects, not from another sanitizer.
    /// </summary>
    internal static class DbQuerySanitizer
    {
        internal const string Placeholder = "?";

        /// <summary>The sanitized form of <paramref name="sql"/>.</summary>
        internal static string Sanitize(string sql, SqlDialect dialect)
        {
            var output = new StringBuilder(sql.Length);
            var i = 0;
            while (i < sql.Length)
            {
                var c = sql[i];
                var next = i + 1 < sql.Length ? sql[i + 1] : '\0';

                if (c == '-' && next == '-')
                {
                    i = SkipLineComment(sql, i);
                    AppendSeparator(output);
                }
                else if (c == '#' && dialect == SqlDialect.MySql)
                {
                    i = SkipLineComment(sql, i);
                    AppendSeparator(output);
                }
                else if (c == '/' && next == '*')
                {
                    if (!TrySkipBlockComment(sql, i, nested: dialect == SqlDialect.PostgreSql, out i))
                    {
                        break;
                    }

                    AppendSeparator(output);
                }
                else if (c == '\'')
                {
                    output.Append(Placeholder);
                    var start = i;
                    if (!TrySkipQuoted(sql, i, '\'', backslashEscapes: dialect == SqlDialect.MySql, out i))
                    {
                        break;
                    }

                    // A PostgreSQL standard string with a backslash ends at its next quote, unless the
                    // server runs with standard_conforming_strings off (then \' is an escape). Either
                    // reading can leave a value as bare text, so the rest is dropped.
                    if (dialect == SqlDialect.PostgreSql && sql.AsSpan(start, i - start).Contains('\\'))
                    {
                        break;
                    }
                }
                else if (c == '"')
                {
                    if (dialect == SqlDialect.MySql)
                    {
                        output.Append(Placeholder);
                        if (!TrySkipQuoted(sql, i, '"', backslashEscapes: true, out i))
                        {
                            break;
                        }
                    }
                    else
                    {
                        // A quoted identifier: kept as written. Unterminated: dropped (it may be a value
                        // the statement was cut in).
                        var start = i;
                        if (!TrySkipQuoted(sql, i, '"', backslashEscapes: false, out i))
                        {
                            break;
                        }

                        output.Append(sql, start, i - start);
                    }
                }
                else if (c == '`' && dialect == SqlDialect.MySql)
                {
                    var start = i;
                    if (!TrySkipQuoted(sql, i, '`', backslashEscapes: false, out i))
                    {
                        break;
                    }

                    output.Append(sql, start, i - start);
                }
                else if (c == '$' && dialect == SqlDialect.PostgreSql)
                {
                    if (char.IsAsciiDigit(next))
                    {
                        // A positional parameter ($1): kept.
                        var start = i++;
                        while (i < sql.Length && char.IsAsciiDigit(sql[i]))
                        {
                            i++;
                        }

                        output.Append(sql, start, i - start);
                    }
                    else if (TryDollarTag(sql, i, out var tag))
                    {
                        output.Append(Placeholder);
                        var close = sql.IndexOf(tag, i + tag.Length, StringComparison.Ordinal);
                        if (close < 0)
                        {
                            break;
                        }

                        i = close + tag.Length;
                    }
                    else
                    {
                        output.Append(c);
                        i++;
                    }
                }
                else if (char.IsAsciiDigit(c) || (c == '.' && char.IsAsciiDigit(next)))
                {
                    output.Append(Placeholder);
                    i = SkipNumber(sql, i);
                }
                else if (IsIdentifierStart(c))
                {
                    var start = i;
                    while (i < sql.Length && IsIdentifierPart(sql[i]))
                    {
                        i++;
                    }

                    var word = sql.AsSpan(start, i - start);
                    if (i < sql.Length && sql[i] == '\'' && IsStringPrefix(word, dialect))
                    {
                        output.Append(Placeholder);
                        var escapes = dialect == SqlDialect.MySql || word.Equals("E", StringComparison.OrdinalIgnoreCase);
                        var literalStart = i;
                        if (!TrySkipQuoted(sql, i, '\'', escapes, out i))
                        {
                            break;
                        }

                        if (!escapes && dialect == SqlDialect.PostgreSql && sql.AsSpan(literalStart, i - literalStart).Contains('\\'))
                        {
                            break;
                        }
                    }
                    else if (dialect == SqlDialect.PostgreSql && word.Equals("U", StringComparison.OrdinalIgnoreCase)
                        && i + 1 < sql.Length && sql[i] == '&' && sql[i + 1] == '\'')
                    {
                        output.Append(Placeholder);
                        if (!TrySkipQuoted(sql, i + 1, '\'', backslashEscapes: false, out i))
                        {
                            break;
                        }
                    }
                    else
                    {
                        output.Append(word);
                    }
                }
                else
                {
                    output.Append(c);
                    i++;
                }
            }

            return output.ToString();
        }

        private static bool IsIdentifierStart(char c) => char.IsLetter(c) || c == '_';

        private static bool IsIdentifierPart(char c) => char.IsLetterOrDigit(c) || c == '_' || c == '$';

        /// <summary>A prefix that makes the following quoted text a literal of another kind.</summary>
        private static bool IsStringPrefix(ReadOnlySpan<char> word, SqlDialect dialect)
        {
            if (word.Length == 1)
            {
                var p = char.ToUpperInvariant(word[0]);
                return p is 'X' or 'B' or 'N' || (p == 'E' && dialect == SqlDialect.PostgreSql);
            }

            // A MySQL charset introducer (_utf8mb4'…', _binary'…').
            return dialect == SqlDialect.MySql && word.Length > 1 && word[0] == '_';
        }

        /// <summary>Keeps one space where a comment separated two tokens.</summary>
        private static void AppendSeparator(StringBuilder output)
        {
            if (output.Length > 0 && !char.IsWhiteSpace(output[^1]))
            {
                output.Append(' ');
            }
        }

        /// <summary>The index of the line end after a line comment (the newline is kept).</summary>
        private static int SkipLineComment(string sql, int i)
        {
            var end = sql.IndexOfAny(new[] { '\n', '\r' }, i);
            return end < 0 ? sql.Length : end;
        }

        private static bool TrySkipBlockComment(string sql, int i, bool nested, out int end)
        {
            var depth = 0;
            while (i < sql.Length)
            {
                if (sql[i] == '/' && i + 1 < sql.Length && sql[i + 1] == '*' && (nested || depth == 0))
                {
                    depth++;
                    i += 2;
                }
                else if (sql[i] == '*' && i + 1 < sql.Length && sql[i + 1] == '/')
                {
                    depth--;
                    i += 2;
                    if (depth == 0)
                    {
                        end = i;
                        return true;
                    }
                }
                else
                {
                    i++;
                }
            }

            end = sql.Length;
            return false;
        }

        /// <summary>
        /// Skips the quoted text that starts at <paramref name="i"/> (the opening quote): a doubled
        /// quote is part of the text, and so is a backslash-escaped character when
        /// <paramref name="backslashEscapes"/>. False when the text is not terminated.
        /// </summary>
        private static bool TrySkipQuoted(string sql, int i, char quote, bool backslashEscapes, out int end)
        {
            i++;
            while (i < sql.Length)
            {
                var c = sql[i];
                if (backslashEscapes && c == '\\')
                {
                    i += 2;
                }
                else if (c == quote)
                {
                    if (i + 1 < sql.Length && sql[i + 1] == quote)
                    {
                        i += 2;
                    }
                    else
                    {
                        end = i + 1;
                        return true;
                    }
                }
                else
                {
                    i++;
                }
            }

            end = sql.Length;
            return false;
        }

        /// <summary>A PostgreSQL dollar-quote opener at <paramref name="i"/>: <c>$$</c> or <c>$tag$</c>.</summary>
        private static bool TryDollarTag(string sql, int i, out string tag)
        {
            var j = i + 1;
            if (j < sql.Length && IsIdentifierStart(sql[j]))
            {
                while (j < sql.Length && (char.IsLetterOrDigit(sql[j]) || sql[j] == '_'))
                {
                    j++;
                }
            }

            if (j < sql.Length && sql[j] == '$')
            {
                tag = sql.Substring(i, j - i + 1);
                return true;
            }

            tag = string.Empty;
            return false;
        }

        /// <summary>The index after a numeric literal: decimal with fraction and exponent, or 0x/0b/0o with underscores.</summary>
        private static int SkipNumber(string sql, int i)
        {
            if (sql[i] == '0' && i + 1 < sql.Length && "xXbBoO".Contains(sql[i + 1]))
            {
                i += 2;
                while (i < sql.Length && (char.IsAsciiHexDigit(sql[i]) || sql[i] == '_'))
                {
                    i++;
                }

                return i;
            }

            while (i < sql.Length && (char.IsAsciiDigit(sql[i]) || sql[i] == '_' || sql[i] == '.'))
            {
                i++;
            }

            if (i < sql.Length && (sql[i] == 'e' || sql[i] == 'E'))
            {
                var j = i + 1;
                if (j < sql.Length && (sql[j] == '+' || sql[j] == '-'))
                {
                    j++;
                }

                if (j < sql.Length && char.IsAsciiDigit(sql[j]))
                {
                    i = j;
                    while (i < sql.Length && char.IsAsciiDigit(sql[i]))
                    {
                        i++;
                    }
                }
            }

            return i;
        }
    }
}
