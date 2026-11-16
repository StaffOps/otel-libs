package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/staffops/otel-libs/go/demo/internal/demokit"
	"github.com/staffops/otel-libs/go/demo/internal/demotest"
	"go.opentelemetry.io/otel/attribute"
	otelcodes "go.opentelemetry.io/otel/codes"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	demov1 "github.com/staffops/otel-libs/go/demo/gen/otellibs/demo/v1"
)

// fakeStore stands in for PostgreSQL: Insert of a dup- id fails as the
// schema's trigger does.
type fakeStore struct {
	rows      map[string]orderRow
	insertErr error
	upsertErr error
	calls     []string
}

func (f *fakeStore) Upsert(_ context.Context, o orderRow) error {
	f.calls = append(f.calls, "upsert "+o.ID)
	if f.upsertErr != nil {
		return f.upsertErr
	}
	o.UpdatedAt = time.Date(2026, 11, 16, 12, 0, 0, 0, time.UTC)
	f.rows[o.ID] = o
	return nil
}

func (f *fakeStore) Insert(_ context.Context, o orderRow) error {
	f.calls = append(f.calls, "insert "+o.ID)
	if f.insertErr != nil {
		return f.insertErr
	}
	f.rows[o.ID] = o
	return nil
}

func (f *fakeStore) Get(_ context.Context, id string) (orderRow, error) {
	f.calls = append(f.calls, "get "+id)
	r, ok := f.rows[id]
	if !ok {
		return orderRow{}, sql.ErrNoRows
	}
	return r, nil
}

type fixture struct {
	svc    *orderService
	store  *fakeStore
	spans  *tracetest.SpanRecorder
	logs   *demotest.Logs
	reader *sdkmetric.ManualReader
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	tracer, rec := demotest.Tracer()
	reader := sdkmetric.NewManualReader()
	ins, err := demokit.NewInstruments(sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader)).Meter(demokit.TracerName))
	if err != nil {
		t.Fatal(err)
	}
	logs := &demotest.Logs{}
	st := &fakeStore{
		rows:      map[string]orderRow{},
		insertErr: &pgconn.PgError{Code: "23505", Message: "duplicate order dup-run7"},
	}
	return &fixture{
		svc:    &orderService{store: st, tracer: tracer, logger: logs.Logger(), ins: ins, run: "run7"},
		store:  st,
		spans:  rec,
		logs:   logs,
		reader: reader,
	}
}

// points returns "scenario/outcome/error.type" → count of demo.orders.processed.
func (f *fixture) points(t *testing.T) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := f.reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	out := map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "demo.orders.processed" {
				continue
			}
			for _, dp := range m.Data.(metricdata.Sum[int64]).DataPoints {
				s, _ := dp.Attributes.Value("demo.scenario")
				o, _ := dp.Attributes.Value("demo.outcome")
				e, _ := dp.Attributes.Value(attribute.Key("error.type"))
				out[fmt.Sprintf("%s/%s/%s", s.AsString(), o.AsString(), e.AsString())] += dp.Value
			}
		}
	}
	return out
}

func TestPlaceOrderOK(t *testing.T) {
	f := newFixture(t)
	resp, err := f.svc.PlaceOrder(context.Background(), &demov1.PlaceOrderRequest{OrderId: "ok-1", Customer: "c", Amount: 11, Scenario: "ok"})
	if err != nil || resp.GetOrderId() != "ok-1" || resp.GetStatus() != "processed" {
		t.Fatalf("PlaceOrder = %v, %v", resp, err)
	}
	if strings.Join(f.store.calls, ",") != "upsert ok-1,get ok-1" {
		t.Errorf("store calls = %v", f.store.calls)
	}
	s := f.spans.Ended()[0]
	if s.Name() != "demo.order.process" || s.Status().Code == otelcodes.Error || len(s.Events()) != 0 {
		t.Errorf("span = %s %v events=%d", s.Name(), s.Status(), len(s.Events()))
	}
	logs := f.logs.Records()
	if len(logs) != 1 || logs[0].Message != "order processed" || logs[0].Attrs["order.id"] != "ok-1" || logs[0].Attrs["demo.scenario"] != "ok" {
		t.Errorf("logs = %+v", logs)
	}
	if logs[0].SpanContext.SpanID() != s.SpanContext().SpanID() {
		t.Error("INFO record must be inside demo.order.process")
	}
	if got := f.points(t); got["ok/ok/"] != 1 || len(got) != 1 {
		t.Errorf("metric points = %v", got)
	}
}

func TestPlaceOrderErrors(t *testing.T) {
	cases := []struct {
		name    string
		req     *demov1.PlaceOrderRequest
		code    codes.Code
		msg     string
		errType string
		logMsg  string
		calls   string
		point   string
	}{
		{
			name: "business_error", req: &demov1.PlaceOrderRequest{OrderId: "ord-3", Amount: 5000, Scenario: "business_error"},
			code: codes.FailedPrecondition, msg: "order rejected", errType: "OrderRejectedError",
			logMsg: "amount over limit rej-run7", calls: "", point: "business_error/error/OrderRejectedError",
		},
		{
			name: "db_error", req: &demov1.PlaceOrderRequest{OrderId: "ord-6", Amount: 20, Scenario: "db_error"},
			code: codes.AlreadyExists, msg: "order exists", errType: "OrderExistsError",
			logMsg: "order dup-run7 exists", calls: "insert dup-run7", point: "db_error/error/OrderExistsError",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := newFixture(t)
			_, err := f.svc.PlaceOrder(context.Background(), c.req)
			st, _ := status.FromError(err)
			if st.Code() != c.code || st.Message() != c.msg {
				t.Fatalf("status = %v %q, want %v %q", st.Code(), st.Message(), c.code, c.msg)
			}
			if got := strings.Join(f.store.calls, ","); got != c.calls {
				t.Errorf("store calls = %q, want %q", got, c.calls)
			}
			s := f.spans.Ended()[0]
			if s.Status().Code != otelcodes.Error || s.Status().Description != "" {
				t.Errorf("span status = %+v", s.Status())
			}
			var et string
			for _, kv := range s.Attributes() {
				if kv.Key == "error.type" {
					et = kv.Value.AsString()
				}
				if strings.Contains(kv.Value.Emit(), "-run7") {
					t.Errorf("span attribute %s leaks a run token", kv.Key)
				}
			}
			if et != c.errType {
				t.Errorf("error.type = %q", et)
			}
			logs := f.logs.Records()
			if len(logs) != 1 || logs[0].Message != c.logMsg || logs[0].Attrs["exception.type"] != c.errType {
				t.Errorf("logs = %+v", logs)
			}
			if got := f.points(t); got[c.point] != 1 || len(got) != 1 {
				t.Errorf("metric points = %v", got)
			}
		})
	}
}

func TestPlaceOrderUnexpectedFailures(t *testing.T) {
	f := newFixture(t)
	f.store.insertErr = errors.New("connection refused")
	_, err := f.svc.PlaceOrder(context.Background(), &demov1.PlaceOrderRequest{OrderId: "ord-6", Scenario: "db_error"})
	if st, _ := status.FromError(err); st.Code() != codes.Internal || st.Message() != "order failed" {
		t.Errorf("other insert error: %v", err)
	}

	if s := f.spans.Ended()[0]; s.Status().Code != otelcodes.Error {
		t.Errorf("unexpected failure must be recorded on demo.order.process: %v", s.Status())
	}
	if r := f.logs.Records(); len(r) != 1 || r[0].Attrs["exception.type"] != "*errors.errorString" {
		t.Errorf("unexpected failure log = %+v", r)
	}

	f = newFixture(t)
	f.store.insertErr = nil // the trigger is missing: the insert succeeds
	_, err = f.svc.PlaceOrder(context.Background(), &demov1.PlaceOrderRequest{OrderId: "ord-6", Scenario: "db_error"})
	if status.Code(err) != codes.Internal {
		t.Errorf("missing trigger: %v", err)
	}

	f = newFixture(t)
	f.store.upsertErr = errors.New("boom")
	_, err = f.svc.PlaceOrder(context.Background(), &demov1.PlaceOrderRequest{OrderId: "ok-1", Scenario: "ok"})
	if status.Code(err) != codes.Internal || f.points(t)["ok/error/*errors.errorString"] != 1 {
		t.Errorf("upsert error: %v %v", err, f.points(t))
	}

	f = newFixture(t)
	_, err = f.svc.PlaceOrder(context.Background(), &demov1.PlaceOrderRequest{OrderId: "x", Scenario: "bogus"})
	if status.Code(err) != codes.InvalidArgument || len(f.spans.Ended()) != 0 || len(f.points(t)) != 0 {
		t.Errorf("unknown scenario: %v", err)
	}
}

func TestGetOrder(t *testing.T) {
	f := newFixture(t)
	if _, err := f.svc.PlaceOrder(context.Background(), &demov1.PlaceOrderRequest{OrderId: "ok-2", Customer: "c", Amount: 12, Scenario: "ok"}); err != nil {
		t.Fatal(err)
	}
	o, err := f.svc.GetOrder(context.Background(), &demov1.GetOrderRequest{OrderId: "ok-2"})
	if err != nil || o.GetAmount() != 12 || o.GetUpdatedAt() != "2026-11-16T12:00:00Z" {
		t.Errorf("GetOrder = %v, %v", o, err)
	}
	if _, err := f.svc.GetOrder(context.Background(), &demov1.GetOrderRequest{OrderId: "nope"}); status.Code(err) != codes.NotFound {
		t.Errorf("missing order: %v", err)
	}
}

func TestGRPCStatus(t *testing.T) {
	cases := []struct {
		err  error
		code codes.Code
		msg  string
	}{
		{&demokit.OrderRejectedError{Run: "r"}, codes.FailedPrecondition, "order rejected"},
		{fmt.Errorf("x: %w", &demokit.OrderExistsError{ID: "dup-r"}), codes.AlreadyExists, "order exists"},
		{errors.New("other"), codes.Internal, "order failed"},
	}
	for _, c := range cases {
		code, msg := grpcStatus(c.err)
		if code != c.code || msg != c.msg {
			t.Errorf("grpcStatus(%v) = %v %q", c.err, code, msg)
		}
	}
}
