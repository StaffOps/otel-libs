package otelsql

// Design D16 / R12 (tasks A38, A44): db.namespace resolution and the SQL
// error attributes.

import (
	"bytes"
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func TestNamespaceFromDSN(t *testing.T) {
	tests := []struct {
		name, driver, dsn, want string
	}{
		{"pg url", "pgx", "postgres://u:p@db.example.com:5432/orders?sslmode=disable", "orders"},
		{"pg url postgresql scheme", "postgres", "postgresql://db.example.com/orders", "orders"},
		{"pg url uppercase scheme", "pgx", "POSTGRES://db.example.com/orders", "orders"},
		{"pg url dbname query wins", "pgx/v5", "postgres://u@h/path?dbname=fromquery", "fromquery"},
		{"pg url escaped path", "pgx", "postgres://h/my%20db", "my db"},
		{"pg url bad escape", "pgx", "postgres://h/my%zzdb", ""},
		{"pg url password with slash and at", "pgx", "postgres://u:p/a@ss@h:5432/orders", ""}, // pgx's url.Parse fails too
		{"pg url encoded password", "pgx", "postgres://u:p%2Fa%40ss@h:5432/orders", "orders"},
		{"pg url multi host", "pgx", "postgres://h1:5432,h2:5432/orders?target_session_attrs=any", "orders"},
		{"pg url no database", "pgx", "postgres://u:p@h:5432", ""},
		{"pg url fragment", "pgx", "postgres://h/orders#frag", "orders"},
		{"pg url bad query", "pgx", "postgres://h/orders?%zz", "orders"},
		{"pg keyword", "pgx", "host=h user=u password=secret dbname=orders sslmode=disable", "orders"},
		{"pg keyword spaces around =", "pgx", "host = h dbname = orders", "orders"},
		{"pg keyword quoted", "pgx", "host=h dbname='my db' password='p w'", "my db"},
		{"pg keyword quoted escapes", "pgx", `dbname='it\'s' host=h`, "it's"},
		{"pg keyword unquoted escape", "pgx", `dbname=a\ b host=h`, "a b"},
		{"pg keyword last wins", "pgx", "dbname=a dbname=b", "b"},
		{"pg keyword unterminated quote", "pgx", "dbname='orders", ""},
		{"pg keyword garbage", "pgx", "this is not a dsn", ""},
		{"pg keyword base64 password", "pgx", "host=hst password=abc123== dbname=orders", "orders"},
		{"pg keyword missing key", "pgx", "=orders", ""},
		{"pg keyword none", "pgx", "host=h user=u", ""},
		{"pg empty", "pgx", "", ""},
		{"mysql", "mysql", "user:pass@tcp(db.example.com:3306)/orders?parseTime=true", "orders"},
		{"mysql slash in password", "mysql", "user:pa/ss@tcp(h:3306)/orders", "orders"},
		{"mysql escaped", "mysql", "u@tcp(h)/my%20db", "my db"},
		{"mysql bad escape", "mysql", "u@tcp(h)/my%zz", ""},
		{"mysql no slash", "mysql", "garbage", ""},
		{"mysql minimal", "mysql", "/orders", "orders"},
		{"mysql minimal with params", "mysql", "/orders?parseTime=true", "orders"},
		{"mysql empty name", "mysql", "u@tcp(h)/?x=1", ""},
		{"sqlserver url", "sqlserver", "sqlserver://sa:secret@h:1433/instance?database=orders&encrypt=true", "orders"},
		{"sqlserver url no query", "sqlserver", "sqlserver://sa:secret@h/instance", ""},
		{"sqlserver url bad query", "sqlserver", "sqlserver://h?%zz", ""},
		{"sqlserver ado", "mssql", "server=h;user id=sa;password=secret;database=orders", "orders"},
		{"sqlserver initial catalog", "sqlserver", "Server=h;Initial Catalog=orders;", "orders"},
		{"sqlserver odbc braces", "sqlserver", "odbc:server=h;database={my db}", "my db"},
		{"sqlserver none", "sqlserver", "server=h;user id=sa", ""},
		{"sqlite path", "sqlite", "file:/var/data/app.db", ""},
		{"other driver", "oracle", "oracle://u:p@h/orders", ""},
		// pgx aliases (fix round 1).
		{"pg keyword database alias", "pgx", "host=h database=orders", "orders"},
		{"pg keyword dbname wins over database", "pgx", "database=a dbname=b", "b"},
		{"pg url database alias", "pgx", "postgres://h/path?database=fromquery", "fromquery"},
		{"pg url empty dbname query", "pgx", "postgres://hst/db?sslmode=require&dbname=", ""},
		{"pg url empty database query", "pgx", "postgres://hst/db?database=", ""},
		{"pg url socket host", "pgx", "postgres://%2Fvar%2Frun%2Fpostgresql/db", "db"},
		{"pg url socket host lowercase", "postgresql", "postgresql://%2fvar%2frun/db?sslmode=disable", "db"},
		{"pg url socket host with user", "pgx", "postgres://u@%2Fvar%2Frun/db", "db"},
		{"pg url bad host not retried", "pgx", "postgres://h%zz/db", ""},
		{"pg url socket host no path", "pgx", "postgres://%2Fvar%2Frun", ""},
		{"sqlserver ado quoted", "mssql", `server=h;password="a;b";database=orders`, "orders"},
		{"sqlserver ado braces escape", "mssql", "server=h;password={a}}b;c};database=orders", "orders"},
		{"sqlserver ado text after brace", "mssql", "password={a}x;database=orders", ""},
		{"sqlserver ado key without value", "mssql", "server;database=orders", ""},
		{"sqlserver url fragment", "sqlserver", "sqlserver://h/i?database=orders#x", "orders"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got, _ := namespaceFromDSN(familyOf(tt.driver), tt.dsn); got != tt.want {
				t.Errorf("namespaceFromDSN(%q) = %q, want %q", tt.dsn, got, tt.want)
			}
		})
	}
}

// Fix round 1 (review): inputs whose earlier parse leaked a credential or a
// host into db.namespace. No fragment of the secret or of the host may reach
// the namespace; each resolves to unknown (no attribute, the driver-only
// line) or to the real database.
func TestNamespaceNeverLeaksSecrets(t *testing.T) {
	tests := []struct{ driver, dsn, want string }{
		{"pgx", "postgres://u:a/S3CRET?x@hst/db", ""},
		{"pgx", "postgres://u:a/S3CRET#x@hst/db", ""},
		{"pgx", "postgres://u:x?dbname=S3CRET@hst/db", ""},
		// Fix round 2: '@' in the query or fragment.
		{"pgx", "postgres://u@hst/db?password=p@ss/S3CRET", "db"},
		{"pgx", "postgres://hst/db?password=a@b/S3CRET&sslmode=disable", "db"},
		{"pgx", "postgres://hst/orders#frag@x/S3CRET", "orders"},
		// Round 3: socket host with a password; an empty ?dbname=.
		{"pgx", "postgres://u:S3CRET@%2Fvar%2Frun%2Fpostgresql/db", "db"},
		{"pgx", "postgres://u:a/S3CRET@%2Fvar%2Frun/db", ""},
		{"pgx", "postgres://u:S3CRET@%2Fvar/db?dbname=", ""},
		{"sqlserver", "sqlserver://hst?database=db&password=a@b?database=S3CRET", "db"},
		{"mysql", "u:a/S3CRET", ""},
		{"pgx", "host=hst password=S3CRET dbname= ", ""},
		{"pgx", "postgres://u:S3CRET@hst", ""},
		{"pgx", "postgres://u@hst/db?options=-csearch_path%3DS3CRET@x", "db"},
		{"pgx", "host=hst dbname= password=S3CRET", ""},
		{"pgx", `host=hst dbname=\ password=S3CRET`, ""},
		{"mysql", "u:a/S3CRET@tcp(hst:3306)", ""},
		{"mysql", "u:a/S3CRET@tcp(hst:3306)?x=1", ""},
		{"sqlserver", "odbc:server=hst;pwd={a;database=S3CRET}", ""},
		{"sqlserver", "password={S3CRET;database=x}", ""},
		{"sqlserver", "server=hst;password={S3CRET;database=x", ""},
		{"sqlserver", `server=hst;password="S3CRET;database=x`, ""},
		{"sqlserver", "sqlserver://u:a?database=S3CRET@hst/i", ""},
		{"sqlserver", "sqlserver://u:a?x@hst/i?database=db", ""},
	}
	for _, tt := range tests {
		t.Run(tt.dsn, func(t *testing.T) {
			t.Setenv(envPGDatabase, "")
			namespaceWarned.Clear()
			buf := captureDiag(t)
			got := resolveNamespace(tt.driver, tt.dsn, "")
			if got != tt.want {
				t.Errorf("resolveNamespace = %q, want %q", got, tt.want)
			}
			raw, _ := namespaceFromDSN(familyOf(tt.driver), tt.dsn)
			for _, out := range []string{got, buf.String()} {
				for _, frag := range []string{"S3CRET", "S3C", "CRET", "hst", "password"} {
					if strings.Contains(out, frag) {
						t.Errorf("%q leaks %q", out, frag)
					}
				}
			}
			if raw != "" && !validNamespace(raw) && got != "" {
				t.Errorf("invalid parse %q used", raw)
			}
			if tt.want == "" && strings.Count(buf.String(), "\n") > 1 {
				t.Errorf("more than one line: %q", buf.String())
			}
		})
	}
}

func TestValidNamespace(t *testing.T) {
	for _, ns := range []string{"orders", "my_db", "db-1", "Orders.v2"} {
		if !validNamespace(ns) {
			t.Errorf("%q rejected", ns)
		}
	}
	for _, ns := range []string{"u@h", "h:5432", "a,b", "a=b", "f(x)", "{x}", "my db", "a\tb"} {
		if validNamespace(strings.ReplaceAll(ns, "\\t", "\t")) {
			t.Errorf("%q accepted", ns)
		}
	}
}

// FuzzNamespacePassword: whatever the password holds, a URL, a quoted
// keyword, a go-sql-driver or a braced ADO DSN never yields a namespace
// containing it (unless it is the database name itself).
func FuzzNamespacePassword(f *testing.F) {
	for _, seed := range []string{"a/S3CRET?x", "a/S3CRET#x", "x?dbname=S3CRET", "p@ss/w0rd", "{x;database=y}", "a'b\\c"} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, secret string) {
		if len(secret) < 4 || strings.Contains("orders", secret) {
			return
		}
		quoted := strings.NewReplacer("\\", "\\\\", "'", "\\'").Replace(secret)
		braced := strings.ReplaceAll(secret, "}", "}}")
		for _, c := range []struct {
			family dbFamily
			dsn    string
		}{
			{familyPostgreSQL, "postgres://u:" + secret + "@hst/orders"},
			{familyPostgreSQL, "host=hst password='" + quoted + "' dbname=orders"},
			{familyMySQL, "u:" + secret + "@tcp(hst:3306)/orders"},
			{familySQLServer, "server=hst;password={" + braced + "};database=orders"},
		} {
			ns, _ := namespaceFromDSN(c.family, c.dsn)
			if validNamespace(ns) && ns != "orders" && strings.Contains(ns, secret) {
				t.Errorf("%q: namespace %q holds the password", c.dsn, ns)
			}
		}
	})
}

// captureDiag hooks the module's diagnostics logger for one test.
func captureDiag(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := diagLogger
	diagLogger = slog.New(slog.NewTextHandler(&buf, nil))
	t.Cleanup(func() { diagLogger = prev })
	return &buf
}

func TestResolveNamespacePrecedence(t *testing.T) {
	tests := []struct {
		name, driver, dsn, option, pgdatabase, want string
		line                                        bool
	}{
		{"option wins over dsn", "pgx", "dbname=fromdsn", "fromoption", "fromenv", "fromoption", false},
		{"dsn wins over PGDATABASE", "pgx", "dbname=fromdsn", "", "fromenv", "fromdsn", false},
		{"PGDATABASE for pgx", "pgx", "host=h", "", "fromenv", "fromenv", false},
		{"PGDATABASE for pgx/v5", "pgx/v5", "host=h", "", "fromenv", "fromenv", false},
		{"PGDATABASE for postgres", "postgres", "", "", "fromenv", "fromenv", false},
		{"PGDATABASE for postgresql", "postgresql", "", "", "fromenv", "fromenv", false},
		{"PGDATABASE ignored for mysql", "mysql", "garbage", "", "fromenv", "", true},
		{"nothing for pgx", "pgx", "host=h", "", "", "", true},
		{"registered conn config key", "pgx", "registeredConnConfig0", "", "", "", true},
		{"unknown driver", "oracle", "x", "", "", "", true},
		{"sqlite none and no line", "sqlite", "app.db", "", "fromenv", "", false},
		{"sqlite3 none and no line", "sqlite3", "app.db", "", "", "", false},
		{"sqlite with option", "sqlite3", "app.db", "main", "", "main", false},
		// Fix round 1: a service file decides the database; invalid names.
		{"service= skips PGDATABASE", "pgx", "service=prod", "", "fromenv", "", true},
		{"url service skips PGDATABASE", "pgx", "postgres://h?service=prod", "", "fromenv", "", true},
		{"invalid PGDATABASE", "pgx", "host=h", "", "a b", "", true},
		{"invalid dsn name", "mssql", "database={my db}", "", "", "", true},
		{"option used as given", "mssql", "", "my db", "", "my db", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv(envPGDatabase, tt.pgdatabase)
			namespaceWarned.Clear()
			buf := captureDiag(t)
			if got := resolveNamespace(tt.driver, tt.dsn, tt.option); got != tt.want {
				t.Errorf("resolveNamespace = %q, want %q", got, tt.want)
			}
			out := buf.String()
			if got := strings.Count(out, "\n"); tt.line && got != 1 || !tt.line && got != 0 {
				t.Fatalf("diagnostics lines = %d (%q), want line=%v", got, out, tt.line)
			}
			if tt.line {
				want := "otelhelper: db.namespace unknown for driver " + tt.driver + "; pass otelsql.WithDBNamespace"
				if !strings.Contains(out, want) {
					t.Errorf("line %q, want %q", out, want)
				}
				if tt.dsn != "" && strings.Contains(out, tt.dsn) {
					t.Errorf("line %q echoes the DSN", out)
				}
			}
		})
	}
}

func TestWithDBNamespaceEmptyIgnored(t *testing.T) {
	var c config
	WithDBNamespace("x")(&c)
	WithDBNamespace("")(&c)
	if c.namespace != "x" {
		t.Errorf("namespace = %q, want x (an empty name is ignored)", c.namespace)
	}
}

// pgError is a pgx-shaped error (*pgconn.PgError has a SQLState method).
type pgError struct{ code string }

func (e *pgError) Error() string    { return "ERROR: duplicate key value (SQLSTATE " + e.code + ")" }
func (e *pgError) SQLState() string { return e.code }

// MySQLError mirrors go-sql-driver's *mysql.MySQLError shape.
type MySQLError struct {
	Number   uint16
	SQLState [5]byte
	Message  string
}

func (e MySQLError) Error() string { return fmt.Sprintf("Error %d: %s", e.Number, e.Message) }

// intMySQLError has a Number but another type name.
type intMySQLError struct{ Number int }

func (e intMySQLError) Error() string { return "mysql" }

type badMySQLError struct{ Number string }

func (badMySQLError) Error() string { return "mysql" }

// panicState panics in SQLState (a typed nil receiver in a real driver).
type panicState struct{}

func (panicState) Error() string    { return "x" }
func (panicState) SQLState() string { panic("boom") }

type multiErr []error

func (m multiErr) Error() string   { return "multi" }
func (m multiErr) Unwrap() []error { return m }

func TestErrorAttributeGetters(t *testing.T) {
	var nilMySQL *MySQLError
	tests := []struct {
		name string
		err  error
		code string // "" = no db.response.status_code
		typ  string // span error.type
	}{
		{"pgx unique violation", &pgError{"23505"}, "23505", "23505"},
		{"wrapped pgx", fmt.Errorf("insert: %w", &pgError{"22P02"}), "22P02", "22P02"},
		{"application sqlstate", &pgError{"P0001"}, "P0001", "P0001"},
		{"EPIPE is not a sqlstate", &pgError{"EPIPE"}, "", "_OTHER"},
		{"lowercase rejected", &pgError{"22p02"}, "", "_OTHER"},
		{"too short", &pgError{"2350"}, "", "_OTHER"},
		{"empty sqlstate", &pgError{""}, "", "_OTHER"},
		{"panicking SQLState", panicState{}, "", "_OTHER"},
		{"mysql 1062", &MySQLError{Number: 1062, Message: "Duplicate entry"}, "1062", "1062"},
		{"mysql value struct", MySQLError{Number: 1064}, "1064", "1064"},
		{"wrapped mysql", fmt.Errorf("q: %w", &MySQLError{Number: 1146}), "1146", "1146"},
		{"mysql in a join", errors.Join(errors.New("a"), &MySQLError{Number: 1213}), "1213", "1213"},
		{"mysql in a multi", multiErr{errors.New("a"), nil, &MySQLError{Number: 1205}}, "1205", "1205"},
		{"mysql client 2003", &MySQLError{Number: 2003}, "", "_OTHER"},
		{"mysql client 2013", &MySQLError{Number: 2013}, "", "_OTHER"},
		{"mysql 2000 boundary", &MySQLError{Number: 2000}, "", "_OTHER"},
		{"mysql 2999 boundary", &MySQLError{Number: 2999}, "", "_OTHER"},
		{"mysql 1999", &MySQLError{Number: 1999}, "1999", "1999"},
		{"mysql 3000", &MySQLError{Number: 3000}, "3000", "3000"},
		{"mysql zero", &MySQLError{Number: 0}, "", "_OTHER"},
		{"mysql nil pointer", nilMySQL, "", "_OTHER"},
		{"other struct with Number", intMySQLError{Number: 1062}, "", "_OTHER"}, // type name differs
		{"mysql string number", badMySQLError{Number: "1062"}, "", "_OTHER"},
		{"plain error", errors.New("connection refused"), "", "_OTHER"},
		{"bad conn", driver.ErrBadConn, "", "_OTHER"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			span := attribute.NewSet(spanErrorAttributes(tt.err)...)
			if v, _ := span.Value("error.type"); v.AsString() != tt.typ {
				t.Errorf("span error.type = %q, want %q", v.AsString(), tt.typ)
			}
			v, ok := span.Value("db.response.status_code")
			if tt.code == "" && ok || tt.code != "" && v.AsString() != tt.code {
				t.Errorf("span db.response.status_code = %q (%v), want %q", v.AsString(), ok, tt.code)
			}
			metric := instrumentErrorAttributes(tt.err)
			if tt.code == "" && len(metric) != 0 {
				t.Errorf("metric attributes = %v, want none", metric)
			}
			if tt.code != "" && (len(metric) != 1 || metric[0].Value.AsString() != tt.code || metric[0].Key != "db.response.status_code") {
				t.Errorf("metric attributes = %v, want db.response.status_code=%s", metric, tt.code)
			}
		})
	}
	if _, ok := responseStatusCode(nil); ok {
		t.Error("nil error has a code")
	}
}

func TestErrSkipGettersReturnNothing(t *testing.T) {
	for _, err := range []error{driver.ErrSkip, fmt.Errorf("w: %w", driver.ErrSkip)} {
		if a := spanErrorAttributes(err); a != nil {
			t.Errorf("span attributes for %v = %v, want none", err, a)
		}
		if a := instrumentErrorAttributes(err); a != nil {
			t.Errorf("metric attributes for %v = %v, want none", err, a)
		}
	}
}

// fakeDriverName is a test driver (other_sql family) whose ExecContext
// answers by query text: "skip" → driver.ErrSkip (database/sql then falls
// back to Prepare + Exec, which succeeds), "pg" → a 23505 SQLSTATE error,
// "mysql" → a 1062 MySQL error, "plain" → a code-less error.
const fakeDriverName = "otelsql-test-fake"

func init() { sql.Register(fakeDriverName, fakeDriver{}) }

type fakeDriver struct{}

func (fakeDriver) Open(string) (driver.Conn, error) { return fakeConn{}, nil }

type fakeConn struct{}

func (fakeConn) Prepare(string) (driver.Stmt, error) { return fakeStmt{}, nil }
func (fakeConn) Close() error                        { return nil }
func (fakeConn) Begin() (driver.Tx, error)           { return nil, errors.New("no transactions") }
func (fakeConn) ExecContext(_ context.Context, query string, _ []driver.NamedValue) (driver.Result, error) {
	switch query {
	case "skip":
		return nil, driver.ErrSkip
	case "pg":
		return nil, &pgError{"23505"}
	case "mysql":
		return nil, &MySQLError{Number: 1062, Message: "Duplicate entry 'secret-value'"}
	case "plain":
		return nil, errors.New("connection reset")
	case "canceled":
		return nil, fmt.Errorf("exec: %w", context.Canceled)
	}
	return driver.RowsAffected(0), nil
}

type fakeStmt struct{}

func (fakeStmt) Close() error                               { return nil }
func (fakeStmt) NumInput() int                              { return -1 }
func (fakeStmt) Exec([]driver.Value) (driver.Result, error) { return driver.RowsAffected(1), nil }
func (fakeStmt) Query([]driver.Value) (driver.Rows, error)  { return nil, io.EOF }

func openFake(t *testing.T, opts ...Option) *sql.DB {
	t.Helper()
	db, closeDB, err := Open(fakeDriverName, "fake", opts...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = closeDB() })
	return db
}

func execSpan(t *testing.T, exp *tracetest.InMemoryExporter) tracetest.SpanStub {
	t.Helper()
	for _, s := range exp.GetSpans() {
		if s.Name == "sql.conn.exec" {
			return s
		}
	}
	t.Fatalf("no sql.conn.exec span in %d spans", len(exp.GetSpans()))
	return tracetest.SpanStub{}
}

// durationPoints returns the attribute sets of db.client.operation.duration.
func durationPoints(t *testing.T, reader *sdkmetric.ManualReader) []attribute.Set {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	var sets []attribute.Set
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if h, ok := m.Data.(metricdata.Histogram[float64]); ok && m.Name == "db.client.operation.duration" {
				for _, dp := range h.DataPoints {
					sets = append(sets, dp.Attributes)
				}
			}
		}
	}
	return sets
}

func TestOpenErrorAttributesOnSpanAndMetric(t *testing.T) {
	tests := []struct {
		query, code, typ string
	}{
		{"pg", "23505", "23505"},
		{"mysql", "1062", "1062"},
		{"plain", "", "_OTHER"},
	}
	for _, tt := range tests {
		t.Run(tt.query, func(t *testing.T) {
			exp, reader := installProviders(t)
			buf := captureDiag(t)
			db := openFake(t, WithDBNamespace("orders"))
			if buf.Len() != 0 {
				t.Errorf("diagnostics with WithDBNamespace: %q", buf.String())
			}
			if _, err := db.ExecContext(context.Background(), tt.query); err == nil {
				t.Fatal("want an error")
			}
			s := execSpan(t, exp)
			if s.Status.Code != codes.Error || s.Status.Description != "" {
				t.Errorf("status = %v %q, want Error without description", s.Status.Code, s.Status.Description)
			}
			set := attribute.NewSet(s.Attributes...)
			if v, _ := set.Value("error.type"); v.AsString() != tt.typ {
				t.Errorf("span error.type = %q, want %q", v.AsString(), tt.typ)
			}
			v, ok := set.Value("db.response.status_code")
			if tt.code == "" && ok || tt.code != "" && v.AsString() != tt.code {
				t.Errorf("span db.response.status_code = %q (%v), want %q", v.AsString(), ok, tt.code)
			}
			for _, kv := range s.Attributes {
				if strings.Contains(kv.Value.Emit(), "secret-value") {
					t.Errorf("attribute %s carries message text", kv.Key)
				}
			}
			found := false
			for _, p := range durationPoints(t, reader) {
				if _, failed := p.Value("error.type"); !failed {
					continue
				}
				found = true
				mv, mok := p.Value("db.response.status_code")
				if tt.code == "" && mok || tt.code != "" && mv.AsString() != tt.code {
					t.Errorf("metric db.response.status_code = %q (%v), want %q", mv.AsString(), mok, tt.code)
				}
				if ns, _ := p.Value("db.namespace"); ns.AsString() != "orders" {
					t.Errorf("metric db.namespace = %q, want orders", ns.AsString())
				}
			}
			if !found {
				t.Error("no failed db.client.operation.duration point")
			}
		})
	}
}

// driver.ErrSkip (go-sql-driver returns it for every statement with
// arguments when interpolateParams=false) is not a failure: the span stays
// Unset with no error attribute, the metric has no error.type.
func TestOpenErrSkipLeavesSpanUnsetAndMetricClean(t *testing.T) {
	exp, reader := installProviders(t)
	db := openFake(t, WithDBNamespace("orders"))
	if _, err := db.ExecContext(context.Background(), "skip"); err != nil {
		t.Fatalf("ErrSkip fallback failed: %v", err)
	}
	s := execSpan(t, exp)
	if s.Status.Code != codes.Unset {
		t.Errorf("status = %v, want Unset", s.Status.Code)
	}
	set := attribute.NewSet(s.Attributes...)
	for _, k := range []attribute.Key{"error.type", "db.response.status_code"} {
		if _, ok := set.Value(k); ok {
			t.Errorf("span carries %s on ErrSkip", k)
		}
	}
	if len(s.Events) != 0 {
		t.Errorf("span events = %v, want none (no exception event)", s.Events)
	}
	points := durationPoints(t, reader)
	if len(points) == 0 {
		t.Fatal("no db.client.operation.duration points")
	}
	for _, p := range points {
		for _, k := range []attribute.Key{"error.type", "db.response.status_code"} {
			if v, ok := p.Value(k); ok {
				t.Errorf("metric carries %s=%s on ErrSkip", k, v.Emit())
			}
		}
	}
}

func TestOpenNamespaceAndLegacyName(t *testing.T) {
	for _, tt := range []struct {
		optIn  string
		legacy bool
	}{{"", true}, {"database", false}} {
		t.Run("optin="+tt.optIn, func(t *testing.T) {
			t.Setenv(envSemconvOptIn, tt.optIn)
			exp, reader := installProviders(t)
			db := openFake(t, WithDBNamespace("orders"))
			if _, err := db.ExecContext(context.Background(), "ok"); err != nil {
				t.Fatal(err)
			}
			set := attribute.NewSet(execSpan(t, exp).Attributes...)
			if v, _ := set.Value("db.namespace"); v.AsString() != "orders" {
				t.Errorf("db.namespace = %q, want orders", v.AsString())
			}
			v, ok := set.Value("db.name")
			if tt.legacy && v.AsString() != "orders" || !tt.legacy && ok {
				t.Errorf("db.name = %q (%v), want legacy=%v", v.AsString(), ok, tt.legacy)
			}
			if n := poolPoints(t, reader); n == 0 {
				t.Error("no pool metrics")
			}
		})
	}
}

func TestOpenUnknownNamespaceLogsDriverOnly(t *testing.T) {
	exp, _ := installProviders(t)
	namespaceWarned.Clear()
	buf := captureDiag(t)
	db, closeDB, err := Open(fakeDriverName, "user=admin password=hunter2", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer closeDB()
	out := buf.String()
	if strings.Count(out, "\n") != 1 || !strings.Contains(out, "db.namespace unknown for driver "+fakeDriverName) {
		t.Errorf("diagnostics = %q, want one line naming the driver", out)
	}
	if strings.Contains(out, "hunter2") || strings.Contains(out, "admin") {
		t.Errorf("diagnostics echo the DSN: %q", out)
	}
	// Once per driver name per process: a second pool adds no line.
	_, closeDB2, err := Open(fakeDriverName, "other")
	if err != nil {
		t.Fatal(err)
	}
	_ = closeDB2()
	if buf.String() != out {
		t.Errorf("second Open wrote another line: %q", buf.String())
	}
	if _, err := db.ExecContext(context.Background(), "ok"); err != nil {
		t.Fatal(err)
	}
	set := attribute.NewSet(execSpan(t, exp).Attributes...)
	if _, ok := set.Value("db.namespace"); ok {
		t.Error("db.namespace set without a resolved name")
	}
}

// A real SQLite failure through Open: no SQLSTATE, so error.type=_OTHER and
// no db.response.status_code; SQLite gets no db.namespace and no line.
func TestOpenSQLiteFailure(t *testing.T) {
	exp, _ := installProviders(t)
	buf := captureDiag(t)
	db, closeDB, err := Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer closeDB()
	if buf.Len() != 0 {
		t.Errorf("SQLite diagnostics: %q", buf.String())
	}
	if _, err := db.ExecContext(context.Background(), "INSERT INTO missing VALUES (1)"); err == nil {
		t.Fatal("want an error")
	}
	s := execSpan(t, exp)
	if s.Status.Code != codes.Error {
		t.Errorf("status = %v, want Error", s.Status.Code)
	}
	set := attribute.NewSet(s.Attributes...)
	if v, _ := set.Value("error.type"); v.AsString() != "_OTHER" {
		t.Errorf("error.type = %q, want _OTHER", v.AsString())
	}
	for _, k := range []attribute.Key{"db.response.status_code", "db.namespace", "db.name"} {
		if _, ok := set.Value(k); ok {
			t.Errorf("SQLite span carries %s", k)
		}
	}
}
