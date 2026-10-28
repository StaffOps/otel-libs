package otelsql

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/metric/noop"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	_ "modernc.org/sqlite" // registers driver "sqlite"
)

func TestSystemAttributes(t *testing.T) {
	tests := []struct{ driver, name, legacy string }{
		{"pgx", "postgresql", "postgresql"},
		{"pgx/v5", "postgresql", "postgresql"},
		{"postgres", "postgresql", "postgresql"},
		{"sqlite", "sqlite", "sqlite"},
		{"sqlite3", "sqlite", "sqlite"},
		{"mysql", "mysql", "mysql"},
		{"sqlserver", "microsoft.sql_server", "mssql"},
		{"MSSQL", "microsoft.sql_server", "mssql"},
		{"oracle", "other_sql", "other_sql"},
		{"", "other_sql", "other_sql"},
	}
	for _, tt := range tests {
		t.Run(tt.driver, func(t *testing.T) {
			got := attribute.NewSet(systemAttributes(tt.driver, true)...)
			if v, _ := got.Value("db.system.name"); v.AsString() != tt.name {
				t.Errorf("db.system.name = %q, want %q", v.AsString(), tt.name)
			}
			if v, _ := got.Value("db.system"); v.AsString() != tt.legacy {
				t.Errorf("db.system = %q, want %q", v.AsString(), tt.legacy)
			}
		})
	}
}

// installProviders sets in-memory global providers (the ext module uses the
// globals, like the helper's Setup) and restores the previous ones.
func installProviders(t *testing.T) (*tracetest.InMemoryExporter, *sdkmetric.ManualReader) {
	t.Helper()
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	prevTP, prevMP := otel.GetTracerProvider(), otel.GetMeterProvider()
	otel.SetTracerProvider(tp)
	otel.SetMeterProvider(mp)
	t.Cleanup(func() {
		otel.SetTracerProvider(prevTP)
		otel.SetMeterProvider(prevMP)
		_ = tp.Shutdown(context.Background())
		_ = mp.Shutdown(context.Background())
	})
	return exp, reader
}

func TestOpenEmitsSpansAndPoolMetricsWithSystemName(t *testing.T) {
	exp, reader := installProviders(t)
	db, closeDB, err := Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer closeDB()
	ctx := context.Background()
	if _, err := db.ExecContext(ctx, "CREATE TABLE t (id INTEGER)"); err != nil {
		t.Fatal(err)
	}
	var n int
	if err := db.QueryRowContext(ctx, "SELECT count(*) FROM t").Scan(&n); err != nil {
		t.Fatal(err)
	}

	spans := exp.GetSpans()
	if len(spans) == 0 {
		t.Fatal("no spans recorded")
	}
	for _, s := range spans {
		set := attribute.NewSet(s.Attributes...)
		if v, _ := set.Value("db.system.name"); v.AsString() != "sqlite" {
			t.Errorf("span %q db.system.name = %q, want sqlite", s.Name, v.AsString())
		}
		if v, _ := set.Value("db.system"); v.AsString() != "sqlite" {
			t.Errorf("span %q db.system = %q, want sqlite", s.Name, v.AsString())
		}
	}

	var rm metricdata.ResourceMetrics
	if err := reader.Collect(ctx, &rm); err != nil {
		t.Fatal(err)
	}
	pool := false
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if strings.HasPrefix(m.Name, "db.sql.connection") {
				pool = true
				if g, ok := m.Data.(metricdata.Sum[int64]); ok {
					for _, dp := range g.DataPoints {
						if v, _ := dp.Attributes.Value("db.system.name"); v.AsString() != "sqlite" {
							t.Errorf("%s db.system.name = %q, want sqlite", m.Name, v.AsString())
						}
					}
				}
			}
		}
	}
	if !pool {
		t.Error("no db.sql.connection.* pool metrics collected")
	}
}

func TestOpenUnknownDriverFails(t *testing.T) {
	if _, _, err := Open("no-such-driver", ""); err == nil {
		t.Fatal("want error for an unregistered driver")
	}
}

// failingMeterProvider returns meters whose callback registration fails.
type failingMeterProvider struct{ noop.MeterProvider }

func (failingMeterProvider) Meter(string, ...metric.MeterOption) metric.Meter { return failingMeter{} }

type failingMeter struct{ noop.Meter }

func (failingMeter) RegisterCallback(metric.Callback, ...metric.Observable) (metric.Registration, error) {
	return nil, errors.New("boom")
}

// Failed before A10: the registration error was assigned to _ and dropped.
func TestOpenSurfacesPoolMetricsRegistrationError(t *testing.T) {
	prev := otel.GetMeterProvider()
	otel.SetMeterProvider(failingMeterProvider{})
	t.Cleanup(func() { otel.SetMeterProvider(prev) })
	db, closeDB, err := Open(stubDriverName, "")
	if err == nil || !strings.Contains(err.Error(), "boom") || !strings.Contains(err.Error(), "pool metrics") {
		t.Fatalf("err = %v, want the registration error surfaced", err)
	}
	if db != nil || closeDB != nil {
		t.Error("db and closeDB must be nil (db closed) on error")
	}
}

// stubDriverName is registered once per test binary: sql.Register panics on a
// duplicate name, and -count=N reruns the tests in the same process.
const stubDriverName = "otelsql-test-stub"

func init() { sql.Register(stubDriverName, stubDriver{}) }

type stubDriver struct{}

func (stubDriver) Open(string) (driver.Conn, error) { return nil, errors.New("not used") }

func poolPoints(t *testing.T, reader *sdkmetric.ManualReader) int {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	n := 0
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if !strings.HasPrefix(m.Name, "db.sql.connection") {
				continue
			}
			switch d := m.Data.(type) {
			case metricdata.Sum[int64]:
				n += len(d.DataPoints)
			case metricdata.Gauge[int64]:
				n += len(d.DataPoints)
			case metricdata.Sum[float64]:
				n += len(d.DataPoints)
			}
		}
	}
	return n
}

// D14: closeDB unregisters the pool-metrics callback, so a closed pool stops
// reporting (db.Close alone left the callback registered).
func TestCloseDBUnregistersPoolMetrics(t *testing.T) {
	_, reader := installProviders(t)
	db, closeDB, err := Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Ping(); err != nil {
		t.Fatal(err)
	}
	if poolPoints(t, reader) == 0 {
		t.Fatal("no db.sql.connection* points while the pool is open")
	}
	if err := closeDB(); err != nil {
		t.Fatal(err)
	}
	if n := poolPoints(t, reader); n != 0 {
		t.Errorf("%d db.sql.connection* points after closeDB, want 0", n)
	}
}

func TestSemconvStabilityOptIn(t *testing.T) {
	tests := []struct {
		env    string
		legacy bool
	}{
		{"", true},
		{"http", true},
		{"database/dup", true},
		{"database,database/dup", true},
		{"database", false},
		{" http , Database ", false},
	}
	for _, tt := range tests {
		t.Run(tt.env, func(t *testing.T) {
			t.Setenv(envSemconvOptIn, tt.env)
			if got := legacyKeysEnabled(); got != tt.legacy {
				t.Errorf("legacyKeysEnabled() = %v, want %v", got, tt.legacy)
			}
		})
	}
}

func TestOptInDatabaseEmitsOnlyNewKey(t *testing.T) {
	t.Setenv(envSemconvOptIn, "database")
	exp, _ := installProviders(t)
	db, closeDB, err := Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer closeDB()
	if _, err := db.ExecContext(context.Background(), "SELECT 1"); err != nil {
		t.Fatal(err)
	}
	spans := exp.GetSpans()
	if len(spans) == 0 {
		t.Fatal("no spans")
	}
	for _, s := range spans {
		set := attribute.NewSet(s.Attributes...)
		if v, _ := set.Value("db.system.name"); v.AsString() != "sqlite" {
			t.Errorf("span %q db.system.name = %q, want sqlite", s.Name, v.AsString())
		}
		if _, ok := set.Value("db.system"); ok {
			t.Errorf("span %q carries legacy db.system with OPT_IN=database", s.Name)
		}
	}
}
