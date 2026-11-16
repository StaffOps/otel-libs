namespace StaffOps.OtelLibs.Demo;

// The class names are the pinned error.type literals of DR3 / DE-D8, the same in every language
// (a deliberate deviation from the semconv fully qualified name): .NET writes the short type name.
// The messages carry the run tokens rej-/dup-/unh-<run>; they belong in logs only (ADR-014).

/// <summary>The backend's business rule rejected the order (business_error).</summary>
public sealed class OrderRejectedError(string message) : Exception(message);

/// <summary>The order already exists: the database raised SQLSTATE 23505 (db_error).</summary>
public sealed class OrderExistsError(string message, Exception inner) : Exception(message, inner);

/// <summary>An exception nobody handles: the api maps it to HTTP 500 (unhandled).</summary>
public sealed class DemoUnhandledError(string message) : Exception(message);
