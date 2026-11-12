// Package otelsql provides OpenTelemetry instrumentation for database/sql.
//
// Usage:
//
//	db, closeDB, err := otelsql.Open("pgx", dsn)
//	if err != nil { ... }
//	defer closeDB() // not db.Close(): also unregisters the pool metrics
//	// All subsequent SQL operations produce spans and metrics, including the
//	// db.sql.* connection pool metrics.
//
// Spans and measurements carry db.system.name (semantic conventions v1.43.0)
// and, until 1.0, the legacy db.system key (v1.26.0 values) so existing
// dashboards keep working during the migration. OTEL_SEMCONV_STABILITY_OPT_IN
// containing "database" (and not "database/dup") emits only db.system.name.
package otelsql

import (
	"database/sql"
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/XSAM/otelsql"
	"go.opentelemetry.io/otel/attribute"
	semconvlegacy "go.opentelemetry.io/otel/semconv/v1.26.0"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"
)

// envSemconvOptIn is the standard semantic-convention migration switch.
const envSemconvOptIn = "OTEL_SEMCONV_STABILITY_OPT_IN"

// Open wraps sql.Open with OpenTelemetry instrumentation (traces, metrics and
// db.sql.* pool metrics). The driver name is mapped to a semantic-convention
// database system (see systemAttributes).
//
// closeDB unregisters the pool-metrics callback and closes the pool; call it
// instead of db.Close, or every open/close cycle leaks a callback that keeps
// reporting a closed pool. If the pool metrics can't be registered the
// database is closed and the error returned: telemetry failures are never
// silently dropped.
func Open(driverName, dataSourceName string) (db *sql.DB, closeDB func() error, err error) {
	attrs := otelsql.WithAttributes(systemAttributes(driverName, legacyKeysEnabled())...)
	db, err = otelsql.Open(driverName, dataSourceName, attrs)
	if err != nil {
		return nil, nil, err
	}
	reg, err := otelsql.RegisterDBStatsMetrics(db, attrs)
	if err != nil {
		_ = db.Close()
		return nil, nil, fmt.Errorf("otelsql: register db.sql pool metrics for driver %q: %w", driverName, err)
	}
	return db, func() error { return errors.Join(reg.Unregister(), db.Close()) }, nil
}

// trimChars is the contract trim set (design D1 "Trimmed character set"): not
// Unicode whitespace, which strings.TrimSpace would also remove (U+00A0).
const trimChars = " \t\n\r\v\f"

// asciiLower lowercases A-Z only (design D1 "ASCII case folding"):
// strings.ToLower maps non-ASCII letters too (U+0130 to i).
func asciiLower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if 'A' <= c && c <= 'Z' {
			b[i] = c + ('a' - 'A')
		}
	}
	return string(b)
}

// legacyKeysEnabled reports whether the legacy db.system key is emitted:
// yes unless OTEL_SEMCONV_STABILITY_OPT_IN contains "database" without
// "database/dup" (the semantic-convention migration contract). Tokens are
// trimmed with the contract set and ASCII-folded (A25, as Python and Node).
func legacyKeysEnabled() bool {
	var stable, dup bool
	for _, v := range strings.Split(os.Getenv(envSemconvOptIn), ",") {
		switch asciiLower(strings.Trim(v, trimChars)) {
		case "database":
			stable = true
		case "database/dup":
			dup = true
		}
	}
	return dup || !stable
}

// systemAttributes maps a database/sql driver name to db.system.name and, when
// legacy is true, the legacy db.system attribute. Unknown drivers report
// other_sql.
func systemAttributes(driverName string, legacy bool) []attribute.KeyValue {
	var name, old attribute.KeyValue
	switch strings.ToLower(driverName) {
	case "pgx", "pgx/v5", "postgres", "postgresql":
		name, old = semconv.DBSystemNamePostgreSQL, semconvlegacy.DBSystemPostgreSQL
	case "sqlite", "sqlite3":
		name, old = semconv.DBSystemNameSQLite, semconvlegacy.DBSystemSqlite
	case "mysql":
		name, old = semconv.DBSystemNameMySQL, semconvlegacy.DBSystemMySQL
	case "sqlserver", "mssql":
		// Legacy db.system used "mssql" for SQL Server (semconv ≤ v1.26).
		name, old = semconv.DBSystemNameMicrosoftSQLServer, semconvlegacy.DBSystemMSSQL
	default:
		name, old = semconv.DBSystemNameOtherSQL, semconvlegacy.DBSystemOtherSQL
	}
	if legacy {
		return []attribute.KeyValue{name, old}
	}
	return []attribute.KeyValue{name}
}
