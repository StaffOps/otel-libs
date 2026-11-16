using Npgsql;

namespace StaffOps.OtelLibs.Demo.Backend;

/// <summary>One stored order.</summary>
public sealed record StoredOrder(string Id, string Customer, decimal Amount, string Scenario, DateTimeOffset UpdatedAt);

/// <summary>The orders table of this language's database (demo/sql/schema.sql).</summary>
public interface IOrderStore
{
    /// <summary>Inserts or updates <paramref name="order"/>'s row (the ok path, DE-D4).</summary>
    Task UpsertAsync(StoredOrder order, CancellationToken cancellationToken);

    /// <summary>Inserts a new row; the schema's trigger raises SQLSTATE 23505 for <c>dup-%</c> ids.</summary>
    Task InsertAsync(StoredOrder order, CancellationToken cancellationToken);

    /// <summary>The row with <paramref name="id"/>, or null.</summary>
    Task<StoredOrder?> GetAsync(string id, CancellationToken cancellationToken);
}

/// <summary>
/// <see cref="IOrderStore"/> on PostgreSQL through Npgsql. Every value is a bind parameter (DR1):
/// no literal reaches the statement text, so <c>db.query.text</c> holds none.
/// </summary>
[System.Diagnostics.CodeAnalysis.ExcludeFromCodeCoverage(Justification = "Needs PostgreSQL: exercised by the local smoke and the cluster run (DE6), not by unit tests.")]
public sealed class PostgresOrderStore(NpgsqlDataSource dataSource) : IOrderStore
{
    private const string Upsert =
        "INSERT INTO orders (id, customer, amount, scenario, updated_at) VALUES ($1, $2, $3, $4, $5) " +
        "ON CONFLICT (id) DO UPDATE SET customer = EXCLUDED.customer, amount = EXCLUDED.amount, " +
        "scenario = EXCLUDED.scenario, updated_at = EXCLUDED.updated_at";

    private const string Insert =
        "INSERT INTO orders (id, customer, amount, scenario, updated_at) VALUES ($1, $2, $3, $4, $5)";

    private const string Select =
        "SELECT id, customer, amount, scenario, updated_at FROM orders WHERE id = $1";

    /// <inheritdoc />
    public Task UpsertAsync(StoredOrder order, CancellationToken cancellationToken) => WriteAsync(Upsert, order, cancellationToken);

    /// <inheritdoc />
    public Task InsertAsync(StoredOrder order, CancellationToken cancellationToken) => WriteAsync(Insert, order, cancellationToken);

    /// <inheritdoc />
    public async Task<StoredOrder?> GetAsync(string id, CancellationToken cancellationToken)
    {
        await using var command = dataSource.CreateCommand(Select);
        command.Parameters.Add(new NpgsqlParameter { Value = id });
        await using var reader = await command.ExecuteReaderAsync(cancellationToken);
        if (!await reader.ReadAsync(cancellationToken))
        {
            return null;
        }

        return new StoredOrder(reader.GetString(0), reader.GetString(1), reader.GetDecimal(2), reader.GetString(3),
            reader.GetFieldValue<DateTimeOffset>(4));
    }

    private async Task WriteAsync(string sql, StoredOrder order, CancellationToken cancellationToken)
    {
        await using var command = dataSource.CreateCommand(sql);
        command.Parameters.Add(new NpgsqlParameter { Value = order.Id });
        command.Parameters.Add(new NpgsqlParameter { Value = order.Customer });
        command.Parameters.Add(new NpgsqlParameter { Value = order.Amount });
        command.Parameters.Add(new NpgsqlParameter { Value = order.Scenario });
        command.Parameters.Add(new NpgsqlParameter { Value = order.UpdatedAt });
        await command.ExecuteNonQueryAsync(cancellationToken);
    }
}
