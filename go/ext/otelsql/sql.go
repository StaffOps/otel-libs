// Package otelsql provides OpenTelemetry instrumentation for database/sql.
//
// Usage:
//
//	db, err := otelsql.Open("postgres", dsn)
//	if err != nil { ... }
//	defer db.Close()
//	// All subsequent SQL operations will produce trace spans and metrics.
package otelsql

import (
	"database/sql"

	"github.com/XSAM/otelsql"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
)

// Open wraps sql.Open with OpenTelemetry instrumentation (traces + metrics).
// The driverName is used as the db.system semantic convention attribute.
func Open(driverName, dataSourceName string) (*sql.DB, error) {
	db, err := otelsql.Open(driverName, dataSourceName,
		otelsql.WithAttributes(semconv.DBSystemKey.String(driverName)),
	)
	if err != nil {
		return nil, err
	}

	// Register db.sql.* connection pool metrics.
	if regErr := otelsql.RegisterDBStatsMetrics(db,
		otelsql.WithAttributes(semconv.DBSystemKey.String(driverName)),
	); regErr != nil {
		// Non-fatal: tracing still works without pool metrics.
		_ = regErr
	}

	return db, nil
}
