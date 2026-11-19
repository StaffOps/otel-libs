package main

import (
	"context"
	"database/sql"
	"errors"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	otelhelper "github.com/staffops/otel-libs/go"
	"github.com/staffops/otel-libs/go/demo/internal/demokit"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	demov1 "github.com/staffops/otel-libs/go/demo/gen/otellibs/demo/v1"
)

// Statements: every value is a bind parameter (DR1), so db.query.text never
// holds an order id, customer or amount.
const (
	upsertSQL = `INSERT INTO orders (id, customer, amount, scenario, updated_at)
VALUES ($1, $2, $3, $4, now())
ON CONFLICT (id) DO UPDATE SET customer = EXCLUDED.customer, amount = EXCLUDED.amount,
  scenario = EXCLUDED.scenario, updated_at = EXCLUDED.updated_at`
	insertSQL = `INSERT INTO orders (id, customer, amount, scenario, updated_at)
VALUES ($1, $2, $3, $4, now())`
	selectSQL = `SELECT id, customer, amount, scenario, updated_at FROM orders WHERE id = $1`
)

// sqlStateUniqueViolation is the SQLSTATE the schema's trigger raises.
const sqlStateUniqueViolation = "23505"

// orderRow is one row of the orders table.
type orderRow struct {
	ID, Customer, Scenario string
	Amount                 float64
	UpdatedAt              time.Time
}

// store is the backend's database access (an interface so the service logic
// is testable without PostgreSQL).
type store interface {
	Upsert(ctx context.Context, o orderRow) error
	Insert(ctx context.Context, o orderRow) error
	Get(ctx context.Context, id string) (orderRow, error)
}

// sqlStore runs the statements on the instrumented pool.
type sqlStore struct{ db *sql.DB }

func (s *sqlStore) Upsert(ctx context.Context, o orderRow) error {
	_, err := s.db.ExecContext(ctx, upsertSQL, o.ID, o.Customer, o.Amount, o.Scenario)
	return err
}

func (s *sqlStore) Insert(ctx context.Context, o orderRow) error {
	_, err := s.db.ExecContext(ctx, insertSQL, o.ID, o.Customer, o.Amount, o.Scenario)
	return err
}

func (s *sqlStore) Get(ctx context.Context, id string) (orderRow, error) {
	var r orderRow
	err := s.db.QueryRowContext(ctx, selectSQL, id).Scan(&r.ID, &r.Customer, &r.Amount, &r.Scenario, &r.UpdatedAt)
	return r, err
}

// orderService implements demo.v1.OrderService.
type orderService struct {
	demov1.UnimplementedOrderServiceServer
	store  store
	tracer trace.Tracer
	logger *slog.Logger
	ins    *demokit.Instruments
	run    string
}

// PlaceOrder handles one order in the demo span demo.order.process, the owning
// span of the backend's failures (DE-D8). otelhelper.WithSpan applies the A45
// rule to it (contract design D15): a returned error marks it (status ERROR
// without description, error.type from the error's ErrorType() method, no
// exception event) and writes the one ERROR record with the message and the
// stack in its context. The custom metrics are recorded inside the span (so
// they get trace_based exemplars); the gRPC status, with a constant message,
// is built after the span ended.
func (s *orderService) PlaceOrder(ctx context.Context, req *demov1.PlaceOrderRequest) (*demov1.PlaceOrderResponse, error) {
	if !demokit.ValidScenario(req.GetScenario()) {
		return nil, status.Error(codes.InvalidArgument, "unknown scenario")
	}
	var id string
	err := otelhelper.WithSpan(ctx, s.tracer, "demo.order.process", func(ctx context.Context) error {
		start := time.Now()
		var err error
		id, err = s.process(ctx, req)
		if err == nil {
			s.logger.InfoContext(ctx, "order processed", "order.id", id, "demo.scenario", req.GetScenario())
		}
		attrs := metric.WithAttributes(demokit.MetricAttributes(req.GetScenario(), err)...)
		s.ins.Processed.Add(ctx, 1, attrs)
		s.ins.Duration.Record(ctx, time.Since(start).Seconds(), attrs)
		return err
	}, trace.WithAttributes(
		demokit.Scenario(req.GetScenario()),
		attribute.String("order.id", req.GetOrderId()),
	))
	if err != nil {
		code, msg := grpcStatus(err)
		return nil, status.Error(code, msg)
	}
	return &demov1.PlaceOrderResponse{OrderId: id, Status: "processed"}, nil
}

// process is the business logic: the amount rule first (business_error),
// then the trigger-raised duplicate for db_error, else upsert and read back.
func (s *orderService) process(ctx context.Context, req *demov1.PlaceOrderRequest) (string, error) {
	if req.GetAmount() > demokit.MaxOrderAmount {
		return "", &demokit.OrderRejectedError{Run: s.run}
	}
	row := orderRow{ID: req.GetOrderId(), Customer: req.GetCustomer(), Amount: req.GetAmount(), Scenario: req.GetScenario()}
	if req.GetScenario() == demokit.ScenarioDBError {
		row.ID = demokit.DuplicateID(s.run)
		err := s.store.Insert(ctx, row)
		if err == nil {
			return "", errors.New("the dup- trigger did not fire: check demo/sql/schema.sql")
		}
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == sqlStateUniqueViolation {
			return "", &demokit.OrderExistsError{ID: row.ID, Err: err}
		}
		return "", err
	}
	if err := s.store.Upsert(ctx, row); err != nil {
		return "", err
	}
	got, err := s.store.Get(ctx, row.ID)
	if err != nil {
		return "", err
	}
	return got.ID, nil
}

// GetOrder reads one stored order; NOT_FOUND when absent.
func (s *orderService) GetOrder(ctx context.Context, req *demov1.GetOrderRequest) (*demov1.Order, error) {
	r, err := s.store.Get(ctx, req.GetOrderId())
	switch {
	case errors.Is(err, sql.ErrNoRows):
		return nil, status.Error(codes.NotFound, "order not found")
	case err != nil:
		return nil, status.Error(codes.Internal, "internal error")
	}
	return &demov1.Order{
		OrderId:   r.ID,
		Customer:  r.Customer,
		Amount:    r.Amount,
		Scenario:  r.Scenario,
		UpdatedAt: r.UpdatedAt.UTC().Format(time.RFC3339),
	}, nil
}

// grpcStatus maps a failure to its gRPC code and constant message (design
// "Scenario table"); the message never carries the run token.
func grpcStatus(err error) (codes.Code, string) {
	var (
		rejected *demokit.OrderRejectedError
		exists   *demokit.OrderExistsError
	)
	switch {
	case errors.As(err, &rejected):
		return codes.FailedPrecondition, "order rejected"
	case errors.As(err, &exists):
		return codes.AlreadyExists, "order exists"
	}
	return codes.Internal, "order failed"
}
