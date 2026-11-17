"""Backend: order logic in demo.order.process, custom metrics, servicer status mapping."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import grpc
import pytest
from opentelemetry.trace import StatusCode

import backend
import democommon as dc


class FakeRepo:
    def __init__(self, duplicate: bool = True):
        self.rows: dict[str, dict] = {}
        self.inserted: list[str] = []
        self.duplicate = duplicate

    def upsert(self, order_id, customer, amount, scenario):
        self.rows[order_id] = {"order_id": order_id, "customer": customer, "amount": amount,
                               "scenario": scenario, "updated_at": "2026-11-16T00:00:00+00:00"}

    def insert(self, order_id, customer, amount, scenario):
        self.inserted.append(order_id)
        if self.duplicate:
            raise backend.DuplicateOrder(f"duplicate order {order_id}")
        self.upsert(order_id, customer, amount, scenario)

    def get(self, order_id):
        return self.rows.get(order_id)


def make_logic(otel, repo=None, run="r1"):
    return backend.OrderLogic(repo or FakeRepo(), otel.tracer, otel.meter, run)


def test_ok_upserts_reads_back_logs_and_measures(otel):
    repo = FakeRepo()
    logic = make_logic(otel, repo)
    with otel.tracer.start_as_current_span("server"):
        assert logic.place("ok-3", "cust-a", 12.0, "ok") == "ok-3"
    assert "ok-3" in repo.rows
    (span,) = otel.finished("demo.order.process")
    assert span.attributes["demo.scenario"] == "ok" and span.attributes["order.id"] == "ok-3"
    assert span.status.status_code is StatusCode.UNSET and not span.events
    (rec,) = otel.records()
    assert rec.body == "order processed" and rec.span_id == span.context.span_id
    assert rec.attributes["order.id"] == "ok-3" and rec.attributes["demo.scenario"] == "ok"

    ((counter, cp),) = otel.points(dc.METRIC_PROCESSED)
    assert counter.unit == "{order}" and cp.value == 1
    assert dict(cp.attributes) == {"demo.scenario": "ok", "demo.outcome": "ok"}
    ((hist, hp),) = otel.points(dc.METRIC_DURATION)
    assert hist.unit == "s" and tuple(hp.explicit_bounds) == dc.DURATION_BOUNDARIES
    # trace_based exemplar from inside the sampled demo span (DE-D9, DE-D10).
    assert hp.exemplars and hp.exemplars[0].span_id == span.context.span_id
    assert hp.exemplars[0].trace_id == span.context.trace_id


def test_business_error_owning_span_and_log(otel):
    logic = make_logic(otel)
    with pytest.raises(dc.OrderRejectedError, match="amount over limit rej-r1"):
        logic.place("ord-2", "cust-c", 5000.0, "business_error")
    (span,) = otel.finished("demo.order.process")
    assert span.status.status_code is StatusCode.ERROR and span.status.description is None
    assert span.attributes["error.type"] == "OrderRejectedError"
    assert not span.events  # A45: no exception event (DE-D8 note 2026-11-17)
    (rec,) = otel.records()  # the helper's one record, in the owning span
    assert rec.span_id == span.context.span_id and rec.attributes["exception.stacktrace"]
    assert rec.attributes["exception.type"] == "OrderRejectedError"
    assert rec.body == rec.attributes["exception.message"] == "amount over limit rej-r1"
    assert rec.severity_number.value >= 17
    ((_, cp),) = otel.points(dc.METRIC_PROCESSED)
    assert dict(cp.attributes) == {"demo.scenario": "business_error", "demo.outcome": "error",
                                   "error.type": "OrderRejectedError"}


def test_db_error_wraps_the_duplicate(otel):
    repo = FakeRepo()
    logic = make_logic(otel, repo)
    with pytest.raises(dc.OrderExistsError, match="order dup-r1 exists") as info:
        logic.place("ord-5", "cust-c", 15.0, "db_error")
    assert repo.inserted == ["dup-r1"]
    assert isinstance(info.value.__cause__, backend.DuplicateOrder)
    (span,) = otel.finished("demo.order.process")
    assert span.attributes["error.type"] == "OrderExistsError"
    assert span.attributes["order.id"] == "ord-5"  # never the run token (DR12)
    ((_, hp),) = otel.points(dc.METRIC_DURATION)
    assert hp.attributes["error.type"] == "OrderExistsError"


def test_db_error_insert_that_succeeds_returns_the_id(otel):
    assert make_logic(otel, FakeRepo(duplicate=False)).place("ord-5", "c", 1.0, "db_error") == "dup-r1"


def test_unexpected_failure_is_recorded_like_a_scenario_error(otel):
    class Lost(FakeRepo):
        def get(self, order_id):
            return None

    with pytest.raises(RuntimeError):
        make_logic(otel, Lost()).place("ok-1", "c", 1.0, "ok")
    (span,) = otel.finished("demo.order.process")
    assert span.status.status_code is StatusCode.ERROR and span.status.description is None
    assert span.attributes["error.type"] == "RuntimeError"
    assert not span.events
    (rec,) = otel.records()
    assert rec.span_id == span.context.span_id and rec.attributes["exception.type"] == "RuntimeError"
    expected = {"demo.scenario": "ok", "demo.outcome": "error", "error.type": "RuntimeError"}
    ((_, cp),) = otel.points(dc.METRIC_PROCESSED)
    ((_, hp),) = otel.points(dc.METRIC_DURATION)
    assert dict(cp.attributes) == expected and cp.value == 1
    assert dict(hp.attributes) == expected and hp.count == 1
    assert hp.exemplars and hp.exemplars[0].span_id == span.context.span_id


class Aborted(Exception):
    pass


class FakeContext:
    def __init__(self):
        self.code = None
        self.details = None

    async def abort(self, code, details):
        self.code, self.details = code, details
        raise Aborted()


@pytest.mark.parametrize("scenario, amount, code, details", [
    ("business_error", 5000.0, grpc.StatusCode.FAILED_PRECONDITION, "order rejected"),
    ("db_error", 10.0, grpc.StatusCode.ALREADY_EXISTS, "order exists"),
])
def test_servicer_maps_errors_to_constant_status(otel, scenario, amount, code, details):
    import demo_pb2

    repo = FakeRepo()
    servicer = backend.build_servicer(make_logic(otel, repo), repo)
    ctx = FakeContext()
    req = demo_pb2.PlaceOrderRequest(order_id="ord-1", customer="c", amount=amount, scenario=scenario)
    with pytest.raises(Aborted):
        asyncio.run(servicer.PlaceOrder(req, ctx))
    assert (ctx.code, ctx.details) == (code, details)
    assert "r1" not in ctx.details  # run tokens stay out of the gRPC status (DE-D8)


def test_servicer_unexpected_failure_is_internal_with_a_constant_message(otel):
    import demo_pb2

    class Broken(FakeRepo):
        def upsert(self, *a):
            raise OSError("disk on fire")

    repo = Broken()
    servicer = backend.build_servicer(make_logic(otel, repo), repo)
    ctx = FakeContext()
    req = demo_pb2.PlaceOrderRequest(order_id="ok-1", customer="c", amount=1.0, scenario="ok")
    with pytest.raises(Aborted):
        asyncio.run(servicer.PlaceOrder(req, ctx))
    assert (ctx.code, ctx.details) == (grpc.StatusCode.INTERNAL, "order failed")
    (span,) = otel.finished("demo.order.process")
    assert span.attributes["error.type"] == "OSError"


@pytest.mark.parametrize("scenario", ["unhandled", "", "nope"])
def test_servicer_rejects_unknown_scenarios(otel, scenario):
    import demo_pb2

    repo = FakeRepo()
    servicer = backend.build_servicer(make_logic(otel, repo), repo)
    ctx = FakeContext()
    req = demo_pb2.PlaceOrderRequest(order_id="ord-1", customer="c", amount=1.0, scenario=scenario)
    with pytest.raises(Aborted):
        asyncio.run(servicer.PlaceOrder(req, ctx))
    assert (ctx.code, ctx.details) == (grpc.StatusCode.INVALID_ARGUMENT, "invalid scenario")
    assert not otel.finished("demo.order.process") and not repo.rows


def test_servicer_ok_and_get_order(otel):
    import demo_pb2

    repo = FakeRepo()
    servicer = backend.build_servicer(make_logic(otel, repo), repo)
    req = demo_pb2.PlaceOrderRequest(order_id="ok-7", customer="c", amount=1.0, scenario="ok")
    reply = asyncio.run(servicer.PlaceOrder(req, FakeContext()))
    assert (reply.order_id, reply.status) == ("ok-7", "processed")
    order = asyncio.run(servicer.GetOrder(demo_pb2.GetOrderRequest(order_id="ok-7"), FakeContext()))
    assert order.order_id == "ok-7" and order.scenario == "ok"
    ctx = FakeContext()
    with pytest.raises(Aborted):
        asyncio.run(servicer.GetOrder(demo_pb2.GetOrderRequest(order_id="nope"), ctx))
    assert ctx.code is grpc.StatusCode.NOT_FOUND


class FakeConn:
    def __init__(self, engine):
        self.engine = engine

    def exec_driver_sql(self, sql, params=None):
        self.engine.calls.append((sql, params))
        if self.engine.fail:
            raise self.engine.fail.pop(0)
        return self

    def first(self):
        return self.engine.row


class FakeEngine:
    def __init__(self, fail=None, row=None):
        self.calls = []
        self.fail = list(fail or [])
        self.row = row

    @contextmanager
    def begin(self):
        yield FakeConn(self)

    connect = begin

    def raw_connection(self):
        engine = self

        class Cursor:
            def execute(self, sql, *params):
                engine.calls.append((sql, params or None))
                if engine.fail:
                    raise engine.fail.pop(0)

            def close(self):
                engine.closed_cursors += 1

        class Raw:
            def cursor(self):
                return Cursor()

            def commit(self):
                engine.commits += 1

            def close(self):
                engine.closed += 1

        return Raw()

    commits = 0
    closed = 0
    closed_cursors = 0


def _integrity(sqlstate):
    from sqlalchemy.exc import IntegrityError

    class Orig(Exception):
        pass

    orig = Orig("duplicate order dup-r1\n")
    orig.sqlstate = sqlstate
    return IntegrityError("INSERT", {}, orig)


def test_sql_repository_binds_every_value_and_translates_23505():
    engine = FakeEngine(fail=[_integrity("23505")])
    repo = backend.SqlRepository(engine)
    with pytest.raises(backend.DuplicateOrder, match="^duplicate order dup-r1$"):
        repo.insert("dup-r1", "c", 1.0, "db_error")
    sql, params = engine.calls[0]
    assert "dup-r1" not in sql and params["id"] == "dup-r1"


def test_sql_repository_other_integrity_errors_pass_through():
    from sqlalchemy.exc import IntegrityError

    repo = backend.SqlRepository(FakeEngine(fail=[_integrity("23503")]))
    with pytest.raises(IntegrityError):
        repo.insert("x", "c", 1.0, "db_error")


def test_sql_repository_upsert_and_get():
    import datetime as dt

    when = dt.datetime(2026, 11, 16, tzinfo=dt.timezone.utc)
    engine = FakeEngine(row=("ok-1", "c", 12.5, "ok", when))
    repo = backend.SqlRepository(engine)
    repo.upsert("ok-1", "c", 12.5, "ok")
    assert repo.get("ok-1") == {"order_id": "ok-1", "customer": "c", "amount": 12.5,
                                "scenario": "ok", "updated_at": when.isoformat()}
    assert "ON CONFLICT (id) DO UPDATE" in engine.calls[0][0]
    engine.row = None
    assert repo.get("missing") is None


def test_apply_schema_sends_one_command_without_parameters_and_retries():
    engine = FakeEngine(fail=[ConnectionError("not yet")])
    backend.apply_schema(engine, "SELECT 1; SELECT 2;", attempts=3, delay=0)
    assert engine.calls == [("SELECT 1; SELECT 2;", None), ("SELECT 1; SELECT 2;", None)]
    assert (engine.commits, engine.closed, engine.closed_cursors) == (1, 2, 2)


def test_apply_schema_gives_up():
    engine = FakeEngine(fail=[ConnectionError("a"), ConnectionError("b")])
    with pytest.raises(ConnectionError):
        backend.apply_schema(engine, "SELECT 1", attempts=2, delay=0)


def test_schema_file_is_the_shared_contract():
    from conftest import REPO

    schema = (REPO / "demo" / "sql" / "schema.sql").read_text()
    assert "pg_advisory_xact_lock" in schema and "WHEN (NEW.id LIKE 'dup-%')" in schema


def test_engine_from_env_builds_the_url_in_memory(monkeypatch, tmp_path):
    pw = tmp_path / "pg-pw"
    pw.write_text("not-printed\n")
    monkeypatch.setenv("DEMO_PG_HOST", "pg.test:5433")
    monkeypatch.setenv("DEMO_PG_DB", "demo_python")
    monkeypatch.setenv("DEMO_PG_USER", "demo_python")
    monkeypatch.setenv("DEMO_PG_PASSWORD_FILE", str(pw))
    engine = backend.create_engine_from_env()  # no connection yet
    try:
        url = engine.url
        assert (url.drivername, url.host, url.port, url.database, url.username) == (
            "postgresql+psycopg", "pg.test", 5433, "demo_python", "demo_python")
        assert url.password == "not-printed"
        assert "not-printed" not in repr(url) and "not-printed" not in str(url)
    finally:
        engine.dispose()
