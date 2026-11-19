using StaffOps.OtelHelper.Tracing;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C14a: the literal sanitizer of Npgsql and MySqlConnector query text (contract A25/R10,
/// design D-NET14). One case per lexical rule; each expected text fails when its rule is removed
/// or changed (mutation-checked, dotnet/TESTS.md).
/// </summary>
public class DbQuerySanitizerTests
{
    private static string Pg(string sql) => DbQuerySanitizer.Sanitize(sql, SqlDialect.PostgreSql);

    private static string My(string sql) => DbQuerySanitizer.Sanitize(sql, SqlDialect.MySql);

    // ── strings ────────────────────────────────────────────────────────────

    [Theory]
    [InlineData("SELECT * FROM t WHERE a = 'secret'", "SELECT * FROM t WHERE a = ?")]
    [InlineData("SELECT 'it''s' , 'x'", "SELECT ? , ?")]
    [InlineData("SELECT ''", "SELECT ?")]
    public void String_Literals_Become_Placeholders_In_Both_Dialects(string sql, string expected)
    {
        Assert.Equal(expected, Pg(sql));
        Assert.Equal(expected, My(sql));
    }

    [Theory]
    [InlineData(@"WHERE a = 'C:\' AND b = 'secret'", "WHERE a = ?")]
    [InlineData(@"WHERE a = 'it\'s secret' AND b = 1", "WHERE a = ?")]
    [InlineData(@"WHERE a = N'it\'s secret' AND b = 1", "WHERE a = ?")]
    [InlineData(@"WHERE a = 'C:\\' AND b = 1", "WHERE a = ?")]
    public void PostgreSql_Standard_String_With_A_Backslash_Drops_The_Rest(string sql, string expected)
    {
        // standard_conforming_strings on: 'C:\' ends at its second quote; off: \' is an escape. Either
        // reading of the other leaves a value as bare text, so nothing after the literal is kept.
        Assert.Equal(expected, Pg(sql));
    }

    [Fact]
    public void PostgreSql_Standard_String_Without_A_Backslash_Keeps_The_Rest()
        => Assert.Equal("WHERE a = ? AND b = ?", Pg("WHERE a = 'x' AND b = 2"));

    [Theory]
    [InlineData(@"WHERE a = E'x\'secret' AND b = 1", "WHERE a = ? AND b = ?")]
    [InlineData(@"WHERE a = e'x\'secret'", "WHERE a = ?")]
    public void PostgreSql_Escape_String_Honors_Backslash(string sql, string expected)
        => Assert.Equal(expected, Pg(sql));

    [Fact]
    public void MySql_Backslash_Escapes_In_Every_Quoted_String()
        => Assert.Equal("WHERE a = ? AND b = ?", My(@"WHERE a = 'x\' secret' AND b = ""y\"" z"""));

    [Fact]
    public void MySql_Double_Quoted_Text_Is_A_String()
        => Assert.Equal("WHERE name = ?", My("WHERE name = \"secret\""));

    [Fact]
    public void PostgreSql_Double_Quoted_Text_Is_An_Identifier_And_Kept()
        => Assert.Equal("SELECT \"Na\"\"me\" FROM \"T\" WHERE a = ?", Pg("SELECT \"Na\"\"me\" FROM \"T\" WHERE a = 'x'"));

    [Fact]
    public void MySql_Backtick_Identifier_Is_Kept_Even_With_A_Quote_Inside()
        => Assert.Equal("SELECT `it's` FROM `t``x` WHERE a = ?", My("SELECT `it's` FROM `t``x` WHERE a = 'x'"));

    [Fact]
    public void Backtick_Is_Ordinary_Text_In_PostgreSql()
        => Assert.Equal("SELECT `a` , ?", Pg("SELECT `a` , 'x'"));

    [Theory]
    [InlineData("SELECT X'1F', B'0101', N'secret', U&'d\\0061t'", "SELECT ?, ?, ?, ?")]
    [InlineData("SELECT x'1f', b'01', n'secret', u&'x'", "SELECT ?, ?, ?, ?")]
    public void PostgreSql_Prefixed_Strings_Become_Placeholders(string sql, string expected)
        => Assert.Equal(expected, Pg(sql));

    [Fact]
    public void PostgreSql_Unicode_Identifier_Is_Kept()
        => Assert.Equal("SELECT U&\"d\\0061t\"", Pg("SELECT U&\"d\\0061t\""));

    [Theory]
    [InlineData("SELECT X'1F', b'01', N'secret', _utf8mb4'secret', _binary'x'", "SELECT ?, ?, ?, ?, ?")]
    [InlineData("SELECT _utf8mb4 'secret'", "SELECT _utf8mb4 ?")]
    public void MySql_Prefixed_Strings_And_Introducers_Become_Placeholders(string sql, string expected)
        => Assert.Equal(expected, My(sql));

    [Fact]
    public void Identifier_Adjacent_To_A_Quote_Is_Not_A_Prefix_Unless_Listed()
        => Assert.Equal("SELECT abc?", Pg("SELECT abc'x'"));

    [Fact]
    public void E_Prefix_Is_PostgreSql_Only()
        => Assert.Equal("SELECT E?", My("SELECT E'x'"));

    [Fact]
    public void Underscore_Introducer_Is_MySql_Only()
        => Assert.Equal("SELECT _utf8?", Pg("SELECT _utf8'x'"));

    // ── dollar quoting and parameters ──────────────────────────────────────

    [Theory]
    [InlineData("SELECT $$secret$$", "SELECT ?")]
    [InlineData("SELECT $fn$ a $ b $$ c $fn$, 1", "SELECT ?, ?")]
    [InlineData("SELECT $a_1$x$a_1$", "SELECT ?")]
    public void PostgreSql_Dollar_Quoted_Strings_Become_Placeholders(string sql, string expected)
        => Assert.Equal(expected, Pg(sql));

    [Theory]
    [InlineData("WHERE a = $1 AND b = $23")]
    [InlineData("SELECT a$b, c$ FROM t$1")]
    [InlineData("SELECT x$tag$y FROM t")]
    [InlineData("SELECT $ + x")]
    public void PostgreSql_Parameters_And_Dollar_In_Identifiers_Are_Kept(string sql)
        => Assert.Equal(sql, Pg(sql));

    [Fact]
    public void Dollar_Is_Not_A_Quote_In_MySql()
        => Assert.Equal("SELECT $$a$$, $?", My("SELECT $$a$$, $1"));

    [Theory]
    [InlineData("WHERE a = @p0 AND b = ? AND c = :name AND d IS NULL AND e = TRUE")]
    [InlineData("SELECT t1.col2 FROM schema1.t1")]
    public void Parameters_Keywords_And_Identifiers_With_Digits_Are_Kept(string sql)
    {
        Assert.Equal(sql, Pg(sql));
        Assert.Equal(sql, My(sql));
    }

    // ── numbers ────────────────────────────────────────────────────────────

    [Theory]
    [InlineData("SELECT 42", "SELECT ?")]
    [InlineData("SELECT 3.14, .5, 1e10, 2.5E-3, 7e", "SELECT ?, ?, ?, ?, ?e")]
    [InlineData("SELECT 0x1F, 0XfF, 0b101, 0o17, 1_000_000", "SELECT ?, ?, ?, ?, ?")]
    [InlineData("SELECT -7 IN (1,2)", "SELECT -? IN (?,?)")]
    [InlineData("SELECT 1e+5", "SELECT ?")]
    public void Numbers_Become_Placeholders(string sql, string expected)
    {
        Assert.Equal(expected, Pg(sql));
        Assert.Equal(expected, My(sql));
    }

    // ── comments ───────────────────────────────────────────────────────────

    [Theory]
    [InlineData("SELECT a -- user=secret\nFROM t", "SELECT a \nFROM t")]
    [InlineData("SELECT a--secret\r\nFROM t", "SELECT a \r\nFROM t")]
    [InlineData("SELECT a /* secret */ FROM t", "SELECT a  FROM t")]
    [InlineData("SELECT a/*secret*/FROM t", "SELECT a FROM t")]
    [InlineData("SELECT a -- secret", "SELECT a ")]
    public void Comments_Are_Dropped_In_Both_Dialects(string sql, string expected)
    {
        Assert.Equal(expected, Pg(sql));
        Assert.Equal(expected, My(sql));
    }

    [Fact]
    public void PostgreSql_Block_Comments_Nest()
        => Assert.Equal("SELECT a FROM t", Pg("SELECT a/* x /* y */ 'secret' */FROM t"));

    [Fact]
    public void MySql_Block_Comments_Do_Not_Nest()
        => Assert.Equal("SELECT a  ? */FROM t", My("SELECT a/* x /* y */ 'secret' */FROM t"));

    [Fact]
    public void Hash_Starts_A_Comment_In_MySql_Only()
    {
        Assert.Equal("SELECT a \nFROM t", My("SELECT a # secret\nFROM t"));
        Assert.Equal("SELECT a # b", Pg("SELECT a # b"));
    }

    // ── fail closed ────────────────────────────────────────────────────────

    [Theory]
    [InlineData("WHERE a = 'secret AND b = 1", "WHERE a = ?")]
    [InlineData("WHERE a = E'sec\\'ret", "WHERE a = ?")]
    [InlineData("WHERE a = U&'secret", "WHERE a = ?")]
    [InlineData("WHERE a = $q$secret", "WHERE a = ?")]
    [InlineData("SELECT a /* secret", "SELECT a ")]
    [InlineData("SELECT \"secret", "SELECT ")]
    public void PostgreSql_Unterminated_Text_Drops_The_Rest(string sql, string expected)
        => Assert.Equal(expected, Pg(sql));

    [Theory]
    [InlineData("WHERE a = \"secret", "WHERE a = ?")]
    [InlineData("WHERE a = _utf8mb4'secret", "WHERE a = ?")]
    [InlineData("SELECT `secret", "SELECT ")]
    [InlineData("WHERE a = 'x\\", "WHERE a = ?")]
    public void MySql_Unterminated_Text_Drops_The_Rest(string sql, string expected)
        => Assert.Equal(expected, My(sql));

    [Fact]
    public void Empty_And_Literal_Free_Text_Is_Unchanged()
    {
        Assert.Equal(string.Empty, Pg(string.Empty));
        Assert.Equal("CALL refresh_orders()", My("CALL refresh_orders()"));
    }

    [Fact]
    public void Non_Ascii_Identifiers_Are_Kept()
        => Assert.Equal("SELECT preço FROM tabela WHERE café = ?", Pg("SELECT preço FROM tabela WHERE café = 'x'"));
}
