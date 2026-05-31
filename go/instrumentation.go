package otelhelper

import "database/sql"

// InstrumentSQL is a placeholder for SQL instrumentation.
// When otelsql is available and SQL instrumentation is enabled, this wraps the DB.
func InstrumentSQL(db *sql.DB, opts *Options) *sql.DB {
	if opts == nil || !opts.HasInstrumentation("SQL") {
		return db
	}
	// TODO: integrate otelsql when adding database/sql instrumentation
	return db
}
