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
//
// They also carry db.namespace (the database name; legacy db.name with the
// legacy keys), resolved by WithDBNamespace, the DSN or PGDATABASE (see
// Open), and a failed operation's span carries db.response.status_code and
// error.type: the PostgreSQL SQLSTATE or the MySQL server error number, or
// error.type=_OTHER without one (design D16).
//
// A failed operation's exception event keeps only exception.type; the
// error's text and a stack trace go to one ERROR log record in the span's
// context (same trace_id and span_id) through the helper's log channel: OTLP
// while a logs exporter is installed, the stdout JSON fallback otherwise
// (ADR-014, contract task A36). Logs can therefore carry the database's
// error text, which may quote a value from the statement or the data. The
// record also carries db.system.name and, when known, db.response.status_code
// and error.type; a cancellation (context.Canceled) gets no record.
//
// Without otelhelper.Setup (or after Shutdown) these records still go to the
// stdout JSON fallback at the default level (INFO, so ERROR passes): an
// application that uses this package without the helper's Setup sees one
// stdout line per failed operation.
//
// Dependencies: this module requires the helper's core module
// (github.com/staffops/otel-libs/go) for that log channel, so importing it
// also brings the core's dependency graph (the OTLP exporters, gRPC, the
// Prometheus exporter) into the build. An application of this package
// already uses the core in practice; the binary only links what is used.
package otelsql

import (
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/XSAM/otelsql"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	semconvlegacy "go.opentelemetry.io/otel/semconv/v1.26.0"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"

	// The core links the exception-record hook of internal/exclog (ADR-014).
	_ "github.com/staffops/otel-libs/go"
	"github.com/staffops/otel-libs/go/internal/exclog"
)

// envSemconvOptIn is the standard semantic-convention migration switch.
const envSemconvOptIn = "OTEL_SEMCONV_STABILITY_OPT_IN"

// Option configures Open.
type Option func(*config)

type config struct {
	namespace string
}

// WithDBNamespace sets db.namespace (and legacy db.name) on every span and
// measurement of the pool: the database name. It wins over the name parsed
// from the DSN and over PGDATABASE; an empty name is ignored. Pass it when
// the DSN carries no database name the helper can read — a key returned by
// pgx stdlib.RegisterConnConfig, a driver the helper does not parse — or for
// SQLite, which otherwise gets no db.namespace.
func WithDBNamespace(name string) Option {
	return func(c *config) {
		if name != "" {
			c.namespace = name
		}
	}
}

// Open wraps sql.Open with OpenTelemetry instrumentation (traces, metrics and
// db.sql.* pool metrics). The driver name is mapped to a semantic-convention
// database system (see systemAttributes).
//
// db.namespace is resolved in this order (design D16): WithDBNamespace; the
// database name parsed from dataSourceName (PostgreSQL URL or keyword form,
// go-sql-driver/mysql, SQL Server); for the drivers mapped to postgresql
// (pgx, pgx/v5, postgres, postgresql) the PGDATABASE environment variable;
// else none, and one line on stderr naming the driver (never the DSN).
// SQLite (sqlite, sqlite3) gets none from the DSN (a file path is not a
// namespace) and no line.
//
// A failed operation's span gets db.response.status_code and error.type set
// to the PostgreSQL SQLSTATE or the MySQL server error number, else
// error.type=_OTHER; the duration metric gets db.response.status_code (its
// error.type stays the XSAM value, the Go error type name). driver.ErrSkip
// is not a failure: the span stays Unset and the metric has no error.type.
//
// That span's exception event carries only exception.type (the D15 rule:
// the first ErrorType() method in the chain, else semconv.ErrorType); the
// error's text and a stack trace (taken where XSAM records the error, so it
// starts in github.com/XSAM/otelsql) go to one ERROR log record per recorded
// error, in the span's context, also when the span is not sampled. The
// record follows the helper's log level and exporter rules (NewSlogHandler).
//
// closeDB unregisters the pool-metrics callback and closes the pool; call it
// instead of db.Close, or every open/close cycle leaks a callback that keeps
// reporting a closed pool. If the pool metrics can't be registered the
// database is closed and the error returned: telemetry failures are never
// silently dropped.
func Open(driverName, dataSourceName string, opts ...Option) (db *sql.DB, closeDB func() error, err error) {
	var cfg config
	for _, o := range opts {
		if o != nil {
			o(&cfg)
		}
	}
	legacy := legacyKeysEnabled()
	kv := systemAttributes(driverName, legacy)
	if ns := resolveNamespace(driverName, dataSourceName, cfg.namespace); ns != "" {
		kv = append(kv, semconv.DBNamespace(ns))
		if legacy {
			kv = append(kv, attribute.String("db.name", ns))
		}
	}
	attrs := otelsql.WithAttributes(kv...)
	db, err = otelsql.Open(driverName, dataSourceName, attrs,
		// RecordError → a type-only exception event + one correlated ERROR
		// record (ADR-014, A36). Wraps the global provider as XSAM would read
		// it, so a pool opened before Setup still follows the helper's.
		otelsql.WithTracerProvider(exclog.TracerProvider(otel.GetTracerProvider(),
			// The record names its database and error code (A36 review), sampled or not.
			exclog.WithRecordAttributes(semconv.DBSystemNameKey.String(systemName(driverName))),
			exclog.WithErrorAttributes(spanErrorAttributes))),
		otelsql.WithSpanOptions(otelsql.SpanOptions{DisableErrSkip: true}),
		otelsql.WithDisableSkipErrMeasurement(true),
		otelsql.WithSpanErrorAttributesGetter(spanErrorAttributes),
		otelsql.WithInstrumentErrorAttributesGetter(instrumentErrorAttributes),
	)
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

// spanErrorAttributes is the XSAM span error getter. XSAM calls it for every
// non-nil error while the span records, before SetStatus. XSAM itself writes
// neither attribute and the helper installs no attributes getter, so on a
// statement span they are absent when it runs (design D16 "only if absent").
// The sql.rows span is the exception: XSAM can call the getter twice for it,
// from Next (rows.go:173) and from Close (rows.go:149, XSAM v0.44.0). The
// second error's attributes overwrite the same keys, so a code-less second
// error leaves the first db.response.status_code next to error.type=_OTHER.
func spanErrorAttributes(err error) []attribute.KeyValue {
	if errors.Is(err, driver.ErrSkip) {
		return nil // not a failure; XSAM calls the getter regardless of DisableErrSkip
	}
	if code, ok := responseStatusCode(err); ok {
		return []attribute.KeyValue{semconv.DBResponseStatusCode(code), semconv.ErrorTypeKey.String(code)}
	}
	return []attribute.KeyValue{semconv.ErrorTypeOther}
}

// instrumentErrorAttributes is the XSAM metric error getter: the accepted
// code as db.response.status_code. XSAM appends its own error.type (the Go
// type name) after this output, so the helper adds none.
func instrumentErrorAttributes(err error) []attribute.KeyValue {
	if errors.Is(err, driver.ErrSkip) {
		return nil
	}
	if code, ok := responseStatusCode(err); ok {
		return []attribute.KeyValue{semconv.DBResponseStatusCode(code)}
	}
	return nil
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

// systemName is the db.system.name value systemAttributes uses for driverName.
func systemName(driverName string) string {
	return systemAttributes(driverName, false)[0].Value.AsString()
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
