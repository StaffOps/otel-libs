"""Demo backend: gRPC ``demo.v1.OrderService`` server and PostgreSQL client.

specs/demo-environment DE-D2…DE-D9. ``PlaceOrder`` runs in the demo span
``demo.order.process``: ``ok`` upserts ``ok-<n>`` and reads it back,
``business_error`` breaks the amount rule (``OrderRejectedError``), ``db_error``
inserts ``dup-<run>`` and the schema's trigger raises SQLSTATE 23505
(``OrderExistsError``). The two custom metrics are recorded inside that span so
they carry ``trace_based`` exemplars.

Environment: ``DEMO_GRPC_PORT`` (50051), ``DEMO_PG_HOST`` (``host[:port]``),
``DEMO_PG_DB``, ``DEMO_PG_USER``, ``DEMO_PG_PASSWORD_FILE``, ``DEMO_SCHEMA_FILE``
(``/etc/otel-libs-demo/schema.sql``, baked into the image), plus the OTel variables the helper reads.
"""

# staffops_otel_helper first: it must run before any opentelemetry import so unsupported
# OTEL_PROPAGATORS values are skipped instead of raising (isort would move it).
import staffops_otel_helper  # isort: skip

import asyncio
import logging
import os
import signal
import time
from typing import Protocol

import grpc
from opentelemetry import trace
from opentelemetry.metrics import Meter
from opentelemetry.trace import Tracer

import democommon as dc

logger = logging.getLogger("demo.backend")

UPSERT_SQL = (
    "INSERT INTO orders (id, customer, amount, scenario, updated_at) "
    "VALUES (%(id)s, %(customer)s, %(amount)s, %(scenario)s, now()) "
    "ON CONFLICT (id) DO UPDATE SET customer = EXCLUDED.customer, amount = EXCLUDED.amount, "
    "scenario = EXCLUDED.scenario, updated_at = EXCLUDED.updated_at"
)
INSERT_SQL = (
    "INSERT INTO orders (id, customer, amount, scenario, updated_at) "
    "VALUES (%(id)s, %(customer)s, %(amount)s, %(scenario)s, now())"
)
# DE-D7 r3.4: the schema path every language bakes into its backend image.
SCHEMA_FILE_DEFAULT = "/etc/otel-libs-demo/schema.sql"
SELECT_SQL = "SELECT id, customer, amount, scenario, updated_at FROM orders WHERE id = %(id)s"


class DuplicateOrder(Exception):
    """Raised by a repository when the database answers SQLSTATE 23505."""


class Repository(Protocol):
    """What the order logic needs from storage (PostgreSQL in the app, a fake in tests)."""

    def upsert(self, order_id: str, customer: str, amount: float, scenario: str) -> None: ...
    def insert(self, order_id: str, customer: str, amount: float, scenario: str) -> None: ...
    def get(self, order_id: str) -> dict | None: ...


class SqlRepository:
    """PostgreSQL through a SQLAlchemy engine instrumented by ``instrument_sql``.

    Every value is a bind parameter (DR1); the statement texts above hold none.
    """

    def __init__(self, engine) -> None:
        self._engine = engine

    def upsert(self, order_id: str, customer: str, amount: float, scenario: str) -> None:
        with self._engine.begin() as conn:
            conn.exec_driver_sql(UPSERT_SQL, {"id": order_id, "customer": customer,
                                              "amount": amount, "scenario": scenario})

    def insert(self, order_id: str, customer: str, amount: float, scenario: str) -> None:
        from sqlalchemy.exc import IntegrityError

        try:
            with self._engine.begin() as conn:
                conn.exec_driver_sql(INSERT_SQL, {"id": order_id, "customer": customer,
                                                  "amount": amount, "scenario": scenario})
        except IntegrityError as e:
            if getattr(e.orig, "sqlstate", None) == "23505":
                # The driver's message is the trigger's "duplicate order dup-<run>".
                raise DuplicateOrder(str(e.orig).strip()) from e
            raise

    def get(self, order_id: str) -> dict | None:
        with self._engine.connect() as conn:
            row = conn.exec_driver_sql(SELECT_SQL, {"id": order_id}).first()
        if row is None:
            return None
        return {"order_id": row[0], "customer": row[1] or "", "amount": float(row[2] or 0),
                "scenario": row[3] or "", "updated_at": row[4].isoformat() if row[4] else ""}


class OrderLogic:
    """The backend's handling of one order inside ``demo.order.process`` (DR3, DR4)."""

    def __init__(self, repo: Repository, tracer: Tracer, meter: Meter, run: str) -> None:
        self._repo = repo
        self._tracer = tracer
        self._run = run
        self._processed = meter.create_counter(
            dc.METRIC_PROCESSED, unit="{order}", description="Orders processed by the demo backend")
        self._duration = meter.create_histogram(
            dc.METRIC_DURATION, unit="s", description="Time the demo backend spent on one order",
            explicit_bucket_boundaries_advisory=list(dc.DURATION_BOUNDARIES))

    def place(self, order_id: str, customer: str, amount: float, scenario: str) -> str:
        """Process one order; return the stored id or raise the scenario's error.

        Synchronous on purpose: it runs in a worker thread (``asyncio.to_thread``
        copies the context, so the gRPC server span stays the parent).
        """
        # The owning span of the backend's failures (DE-D8): the helper's traced marks it when
        # an exception leaves (status ERROR without description, error.type, no exception
        # event) and writes the one ERROR record with the message and the stack (D15).
        with staffops_otel_helper.traced(
            "demo.order.process", tracer=self._tracer,
            attributes={"demo.scenario": scenario, "order.id": order_id},
        ):
            started = time.perf_counter()
            attrs = {"demo.scenario": scenario, "demo.outcome": "ok"}
            try:
                stored = self._process(order_id, customer, amount, scenario)
            except Exception as e:
                # The two scenario errors and any unexpected failure alike (DE-D8, r3.5).
                attrs = {**attrs, "demo.outcome": "error", "error.type": dc.error_type(e)}
                raise
            else:
                logger.info("order processed", extra={"order.id": stored, "demo.scenario": scenario})
                return stored
            finally:
                # Inside the span: the measurements get trace_based exemplars (DE-D9).
                self._duration.record(time.perf_counter() - started, attrs)
                self._processed.add(1, attrs)

    def _process(self, order_id: str, customer: str, amount: float, scenario: str) -> str:
        if amount > dc.AMOUNT_LIMIT:
            raise dc.OrderRejectedError(f"amount over limit rej-{self._run}")
        if scenario == dc.DB_ERROR:
            dup_id = f"dup-{self._run}"
            try:
                self._repo.insert(dup_id, customer, amount, scenario)
            except DuplicateOrder as e:
                raise dc.OrderExistsError(f"order {dup_id} exists") from e
            return dup_id
        self._repo.upsert(order_id, customer, amount, scenario)
        if self._repo.get(order_id) is None:  # read back, as DE-D4 asks
            raise RuntimeError("upserted order not found")
        return order_id


def build_servicer(logic: OrderLogic, repo: Repository):
    """Return the ``OrderService`` servicer bound to ``logic`` (stubs imported lazily)."""
    import demo_pb2
    import demo_pb2_grpc

    class OrderService(demo_pb2_grpc.OrderServiceServicer):
        async def PlaceOrder(self, request, context):
            if request.scenario not in dc.BACKEND_SCENARIOS:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, dc.GRPC_MSG_INVALID)
            try:
                stored = await asyncio.to_thread(
                    logic.place, request.order_id, request.customer, request.amount, request.scenario)
            except dc.OrderRejectedError:
                code, details = grpc.StatusCode.FAILED_PRECONDITION, dc.GRPC_MSG_REJECTED
            except dc.OrderExistsError:
                code, details = grpc.StatusCode.ALREADY_EXISTS, dc.GRPC_MSG_EXISTS
            except Exception:  # noqa: BLE001 - already recorded and logged in demo.order.process
                code, details = grpc.StatusCode.INTERNAL, dc.GRPC_MSG_FAILED
            else:
                return demo_pb2.PlaceOrderResponse(order_id=stored, status="processed")
            # Outside the demo span; aio raises AbortError here, which the contrib
            # server interceptor records on the server span (DE-D8 r3.1, xfail:A37).
            await context.abort(code, details)

        async def GetOrder(self, request, context):
            row = await asyncio.to_thread(repo.get, request.order_id)
            if row is None:
                await context.abort(grpc.StatusCode.NOT_FOUND, "order not found")
            return demo_pb2.Order(**row)

    return OrderService()


def create_engine_from_env():
    """SQLAlchemy engine for ``demo_<lang>``; the password stays in memory (DE-D13)."""
    from sqlalchemy import URL, create_engine

    host, port = dc.split_host_port(os.environ.get("DEMO_PG_HOST", "demo-postgres"), 5432)
    url = URL.create(
        "postgresql+psycopg",
        username=os.environ.get("DEMO_PG_USER", f"demo_{dc.LANG}"),
        password=dc.read_secret_file(os.environ.get("DEMO_PG_PASSWORD_FILE"), "DEMO_PG_PASSWORD_FILE"),
        host=host, port=port,
        database=os.environ.get("DEMO_PG_DB", f"demo_{dc.LANG}"),
    )
    # hide_parameters: bind values (the run tokens) stay out of SQLAlchemy error texts.
    return create_engine(url, pool_size=2, max_overflow=2, pool_pre_ping=True, hide_parameters=True)


def apply_schema(engine, schema: str, attempts: int = 30, delay: float = 2.0) -> None:
    """Send ``schema`` as one command without bind parameters (DE-D4 r3.3), retrying until PostgreSQL answers."""
    for attempt in range(1, attempts + 1):
        try:
            # Straight on the DBAPI connection: SQLAlchemy's exec_driver_sql always passes a
            # (possibly empty) parameter set, and psycopg then parses the schema's '%' as
            # placeholders. cursor.execute(text) without parameters uses the simple query
            # protocol: the multi-statement text, dollar-quoted body included, is one
            # command and one implicit transaction (DE-D4 r3.3).
            raw = engine.raw_connection()
            try:
                cursor = raw.cursor()
                try:
                    cursor.execute(schema)
                finally:
                    cursor.close()
                raw.commit()
            finally:
                raw.close()
            logger.info("schema applied")
            return
        except Exception as e:  # noqa: BLE001 - only the type is logged (no DSN, no text)
            if attempt == attempts:
                raise
            logger.warning("database not ready, retrying", extra={"error.type": type(e).__name__,
                                                                    "demo.attempt": attempt})
            time.sleep(delay)


async def serve() -> None:  # pragma: no cover - process wiring, run by the image smoke
    from grpc_health.v1 import health, health_pb2, health_pb2_grpc

    import demo_pb2_grpc

    engine = create_engine_from_env()
    schema_path = os.environ.get("DEMO_SCHEMA_FILE", SCHEMA_FILE_DEFAULT)
    with open(schema_path, encoding="utf-8") as fh:
        schema = fh.read()
    await asyncio.to_thread(apply_schema, engine, schema)

    repo = SqlRepository(engine)
    logic = OrderLogic(repo, staffops_otel_helper.get_tracer(dc.SCOPE),
                       staffops_otel_helper.get_meter(dc.SCOPE), dc.run_id())
    server = grpc.aio.server()
    demo_pb2_grpc.add_OrderServiceServicer_to_server(build_servicer(logic, repo), server)
    health_servicer = health.HealthServicer()
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    port = dc.env_int("DEMO_GRPC_PORT", 50051)
    server.add_insecure_port(f"[::]:{port}")

    # SIGTERM → graceful stop, so main() flushes telemetry afterwards.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await server.start()
    logger.info("backend listening", extra={"server.port": port})
    await stop.wait()
    await server.stop(grace=4)
    engine.dispose()


def main() -> None:  # pragma: no cover - process wiring, run by the image smoke
    from staffops_otel_helper.ext.sql import instrument_sql

    # Extensions before setup_telemetry(): the one 0.3 mechanism (no implicit-SQL warning).
    instrument_sql()
    staffops_otel_helper.setup_telemetry()
    try:
        asyncio.run(serve())
    finally:
        try:
            staffops_otel_helper.shutdown_telemetry(timeout_ms=5_000)
        except RuntimeError as e:
            logger.warning("telemetry shutdown incomplete: %s", e)


if __name__ == "__main__":  # pragma: no cover
    main()
