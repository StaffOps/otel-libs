"""D16 / R12 (2026-11-18): protocol span error attributes, Python side.

- A44: a failed SQLAlchemy span carries ``db.response.status_code`` and
  ``error.type`` = the PostgreSQL SQLSTATE / MySQL server error number, else
  ``error.type=_OTHER`` (``_db_status.sql_error_code`` + the end hook).
- A46: a failed gRPC span carries ``error.type`` = the canonical status code
  name, else ``_OTHER`` (``_grpc_status``).

Fake exception classes stand in for the drivers (no new dev dependency).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from concurrent import futures

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

from staffops_otel_helper import TelemetryOptions, _grpc_status, setup_telemetry
from staffops_otel_helper._db_status import _drop_error_description, sql_error_code

# --------------------------------------------------------------------------- A44: sql_error_code


class _Diag:
    def __init__(self, sqlstate):
        self.sqlstate = sqlstate


class Psycopg2Error(Exception):  # psycopg2: pgcode (None on a client error) + diag.sqlstate
    def __init__(self, pgcode, diag=None):
        super().__init__("duplicate key value violates unique constraint (bound-secret)")
        self.pgcode = pgcode
        self.diag = _Diag(diag)


class Psycopg3Error(Exception):  # psycopg 3: sqlstate property + diag.sqlstate
    def __init__(self, sqlstate, diag=None):
        super().__init__("relation does not exist")
        self._sqlstate = sqlstate
        self.diag = _Diag(diag)

    @property
    def sqlstate(self):
        return self._sqlstate


class AsyncpgError(Exception):  # asyncpg: sqlstate on the driver error
    def __init__(self, sqlstate):
        super().__init__("asyncpg")
        self.sqlstate = sqlstate


class AdaptedError(Exception):  # SQLAlchemy's adapted DBAPI error, the driver error as __cause__
    pass


class RaisingProperty(Exception):
    @property
    def pgcode(self):
        raise RuntimeError("boom")

    @property
    def args(self):  # noqa: D401 - the MySQL path reads args
        raise RuntimeError("boom")


class PyMySQLError(Exception):  # pymysql / mysqlclient: args = (number, message)
    pass


def _caused(outer, cause):
    try:
        raise outer from cause
    except BaseException as e:  # noqa: BLE001
        return e


@pytest.mark.parametrize("exc, system, want", [
    # PostgreSQL: psycopg2 pgcode.
    (Psycopg2Error("23505"), "postgresql", "23505"),
    # psycopg2 client error: pgcode None, diag.sqlstate None.
    (Psycopg2Error(None), "postgresql", None),
    (Psycopg2Error(None, diag="42P01"), "postgresql", "42P01"),
    # psycopg 3: sqlstate, then diag.
    (Psycopg3Error("42P01"), "postgresql", "42P01"),
    (Psycopg3Error(None, diag="22P02"), "postgresql", "22P02"),
    # pgcode with a bad shape, sqlstate accepted: the next getter.
    (Psycopg3Error("EPIPE", diag="23505"), "postgresql", "23505"),
    # asyncpg through SQLAlchemy: the outer error has none, its __cause__ has it.
    (_caused(AdaptedError("x"), AsyncpgError("23505")), "postgresql", "23505"),
    # The outer error wins over the cause.
    (_caused(Psycopg2Error("40001"), AsyncpgError("23505")), "postgresql", "40001"),
    # Application-defined code (RAISE ... USING ERRCODE): allowed by the pattern.
    (Psycopg2Error("P0001"), "postgresql", "P0001"),
    # Bad shapes: socket codes, no digit, wrong length, lower case, not a str.
    (Psycopg2Error("EPIPE"), "postgresql", None),
    (Psycopg2Error("ABCDE"), "postgresql", None),
    (Psycopg2Error("2350"), "postgresql", None),
    (Psycopg2Error("235051"), "postgresql", None),
    (Psycopg2Error("2350a"), "postgresql", None),
    (Psycopg2Error("23505\n"), "postgresql", None),
    (Psycopg2Error(23505), "postgresql", None),
    (RaisingProperty("x"), "postgresql", None),
    (ValueError("plain"), "postgresql", None),
    # MySQL / MariaDB: args[0] as the server error number.
    (PyMySQLError(1062, "Duplicate entry 'bound-secret'"), "mysql", "1062"),
    (PyMySQLError(1064, "syntax"), "mariadb", "1064"),
    (PyMySQLError(2003, "Can't connect"), "mysql", None),  # CR_CONN_HOST_ERROR, a client error
    (PyMySQLError(2013, "Lost connection"), "mysql", None),
    (PyMySQLError(1999, "x"), "mysql", "1999"),
    (PyMySQLError(3000, "x"), "mysql", "3000"),
    (PyMySQLError(65535, "x"), "mysql", "65535"),
    (PyMySQLError(65536, "x"), "mysql", None),
    (PyMySQLError(0, "x"), "mysql", None),
    (PyMySQLError(True, "x"), "mysql", None),
    (PyMySQLError("1062", "x"), "mysql", None),
    (PyMySQLError(), "mysql", None),
    (RaisingProperty("x"), "mysql", None),
    (_caused(AdaptedError("x"), PyMySQLError(1062, "x")), "mysql", "1062"),
    # A MySQL number on a PostgreSQL span, a SQLSTATE on a MySQL span: no code.
    (PyMySQLError(1062, "x"), "postgresql", None),
    (Psycopg2Error("23505"), "mysql", None),
    # An OSError carries an errno, never a code: skipped as the exception and as the cause (aiomysql
    # raises OperationalError(2013) from ConnectionResetError(104)).
    (_caused(PyMySQLError(2013, "x"), ConnectionResetError(104, "reset")), "mysql", None),
    (_caused(PyMySQLError(2006, "x"), OSError(32, "broken pipe")), "mysql", None),
    (_caused(AdaptedError("x"), OSError(32, "broken pipe")), "mysql", None),
    (ConnectionRefusedError(111, "x"), "mysql", None),
    (_caused(AdaptedError("x"), ConnectionResetError(104, "reset")), "postgresql", None),
    # Other systems and no exception.
    (Psycopg2Error("23505"), "sqlite", None),
    (Psycopg2Error("23505"), "mssql", None),
    (Psycopg2Error("23505"), None, None),
    (None, "postgresql", None),
])
def test_sql_error_code(exc, system, want):
    assert sql_error_code(exc, system) == want


# --------------------------------------------------------------------------- A44: the end hook


def _sql_span(attributes=None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    span = provider.get_tracer("t").start_span("INSERT", kind=SpanKind.CLIENT, attributes=attributes or {})
    return _drop_error_description(span), exporter


def _fail_like_contrib(span, exc):
    """contrib sqlalchemy's handle_error: Status(ERROR, str(exc)) inside SQLAlchemy's except, then end()."""
    try:
        raise exc
    except BaseException:  # noqa: BLE001
        span.set_status(Status(StatusCode.ERROR, str(exc)))
    span.end()


def _finished(exporter):
    (span,) = exporter.get_finished_spans()
    return span


@pytest.mark.parametrize("system_key", ["db.system.name", "db.system"])
def test_failed_postgres_span_gets_the_sqlstate(system_key):
    span, exporter = _sql_span({system_key: "postgresql"})
    _fail_like_contrib(span, Psycopg2Error("23505"))
    s = _finished(exporter)
    assert s.status.status_code is StatusCode.ERROR and s.status.description is None
    assert s.attributes["db.response.status_code"] == "23505"
    assert s.attributes["error.type"] == "23505"


def test_failed_mysql_span_gets_the_error_number_and_a_client_error_is_other():
    span, exporter = _sql_span({"db.system.name": "mysql"})
    _fail_like_contrib(span, PyMySQLError(1062, "Duplicate entry"))
    assert _finished(exporter).attributes["error.type"] == "1062"
    span, exporter = _sql_span({"db.system.name": "mysql"})
    _fail_like_contrib(span, PyMySQLError(2003, "Can't connect"))
    s = _finished(exporter)
    assert s.attributes["error.type"] == "_OTHER" and "db.response.status_code" not in s.attributes


def test_status_description_that_is_not_the_active_exception_is_not_captured():
    """The description guard: another text than str(sys.exc_info()[1]) captures nothing (_OTHER)."""
    span, exporter = _sql_span({"db.system.name": "postgresql"})
    try:
        raise Psycopg2Error("23505")
    except BaseException:  # noqa: BLE001
        span.set_status(Status(StatusCode.ERROR, "something else"))
    span.end()
    s = _finished(exporter)
    assert s.attributes["error.type"] == "_OTHER" and "db.response.status_code" not in s.attributes


def test_status_code_form_and_no_active_exception():
    span, exporter = _sql_span({"db.system.name": "postgresql"})
    span.set_status(StatusCode.ERROR, "duplicate")  # no exception being handled
    span.end()
    assert _finished(exporter).attributes["error.type"] == "_OTHER"


def test_record_exception_captures_the_connect_failure():
    span, exporter = _sql_span({"db.system.name": "postgresql"})
    exc = Psycopg2Error("28P01")
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
    span.end()
    s = _finished(exporter)
    assert s.attributes["error.type"] == "28P01" and s.attributes["db.response.status_code"] == "28P01"
    assert [e.name for e in s.events] == ["exception"]


def test_values_already_on_the_span_are_kept():
    span, exporter = _sql_span({"db.system.name": "postgresql", "error.type": "app_value"})
    _fail_like_contrib(span, Psycopg2Error("23505"))
    s = _finished(exporter)
    assert s.attributes["error.type"] == "app_value" and "db.response.status_code" not in s.attributes
    span, exporter = _sql_span({"db.system.name": "postgresql", "db.response.status_code": "40001"})
    _fail_like_contrib(span, Psycopg2Error("23505"))
    s = _finished(exporter)
    # error.type SHOULD match an upstream status code (semconv): the preset value, not the exception's.
    assert s.attributes["db.response.status_code"] == "40001" and s.attributes["error.type"] == "40001"


def test_unset_and_ok_spans_get_nothing():
    for status in (None, StatusCode.OK):
        span, exporter = _sql_span({"db.system.name": "postgresql"})
        if status is not None:
            span.set_status(status)
        span.end()
        s = _finished(exporter)
        assert "error.type" not in s.attributes and "db.response.status_code" not in s.attributes


def test_end_hook_never_raises(monkeypatch):
    from staffops_otel_helper import _db_status

    def boom(*a):
        raise RuntimeError("boom")

    monkeypatch.setattr(_db_status, "_set_sql_error_type", boom)
    span, exporter = _sql_span({"db.system.name": "postgresql"})
    _fail_like_contrib(span, Psycopg2Error("23505"))
    assert _finished(exporter).status.status_code is StatusCode.ERROR


def test_unprintable_exception_is_not_captured():
    class Unprintable(Exception):
        def __str__(self):
            raise RuntimeError("no str")

    span, exporter = _sql_span({"db.system.name": "postgresql"})
    try:
        raise Unprintable()
    except BaseException:  # noqa: BLE001
        span.set_status(Status(StatusCode.ERROR, "x"))
    span.end()
    assert _finished(exporter).attributes["error.type"] == "_OTHER"


def test_non_recording_span_is_left_alone():
    span = _drop_error_description(trace.INVALID_SPAN)
    span.set_status(Status(StatusCode.ERROR, "x"))
    span.end()  # no error


# --------------------------------------------------------------------------- A44: real sqlite


_SQLITE_SCRIPT = textwrap.dedent("""
    import json, os, sys, tempfile
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    ex = InMemorySpanExporter(); tp = TracerProvider(); tp.add_span_processor(SimpleSpanProcessor(ex))
    trace.set_tracer_provider(tp)
    from staffops_otel_helper import _db_status
    seen = []  # what the end hook read through the real SQLAlchemy path
    _code = _db_status.sql_error_code
    def recording(exc, system):
        seen.append([type(exc).__name__ if exc is not None else None, system])
        return _code(exc, system)
    _db_status.sql_error_code = recording
    from staffops_otel_helper.ext.sql import instrument_sql
    import sqlalchemy
    from sqlalchemy import event
    assert instrument_sql()
    path = os.path.join(tempfile.mkdtemp(), "d16.db")
    engine = sqlalchemy.create_engine("sqlite:///" + path)

    @event.listens_for(engine, "before_cursor_execute")
    def app_error_type(conn, cursor, statement, params, context, executemany):
        # after contrib's listener (registered at create_engine): a value set on the open span
        span = getattr(context, "_otel_span", None)
        if span is not None and "'preset'" in statement:
            span.set_attribute("error.type", "app_value")

    with engine.connect() as c:
        c.execute(sqlalchemy.text("create table t (v text unique)"))
        c.execute(sqlalchemy.text("insert into t values ('bound-secret-x')"))
        for stmt in ["insert into t values ('bound-secret-x')", "select from where 'bound-secret-z'",
                     "insert into missing values ('preset')"]:
            try:
                c.execute(sqlalchemy.text(stmt))
            except Exception:
                pass
    try:  # a failed connect: sqlite cannot open a file in a missing directory
        sqlalchemy.create_engine("sqlite:////nonexistent-dir-otel/x.db").connect()
    except Exception:
        pass
    out = []
    for s in ex.get_finished_spans():
        out.append({"name": s.name, "code": s.status.status_code.name, "description": s.status.description,
                    "statement": s.attributes.get("db.statement", s.attributes.get("db.query.text")),
                    "attributes": {k: str(v) for k, v in s.attributes.items()}})
    print(json.dumps({"path": path, "spans": out, "seen": seen}))
""")


def test_sqlite_failures_end_to_end():
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("opentelemetry.instrumentation.sqlalchemy")
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    out = subprocess.run([sys.executable, "-c", _SQLITE_SCRIPT], env=env, capture_output=True, text=True,
                         check=True)
    result = json.loads(out.stdout.strip().splitlines()[-1])
    spans = result["spans"]
    failed = [s for s in spans if s["code"] == "ERROR"]
    assert len(failed) == 4, spans  # duplicate, syntax error, preset, connect
    for s in failed:
        assert s["description"] is None, s
        assert "db.response.status_code" not in s["attributes"], s  # SQLite: no accepted code
    by_statement = {s["statement"]: s for s in failed if s["name"] != "connect"}
    assert by_statement["insert into t values ('bound-secret-x')"]["attributes"]["error.type"] == "_OTHER"
    assert by_statement["select from where 'bound-secret-z'"]["attributes"]["error.type"] == "_OTHER"
    assert by_statement["insert into missing values ('preset')"]["attributes"]["error.type"] == "app_value"
    connect = [s for s in failed if s["name"] == "connect"]
    assert len(connect) == 1 and connect[0]["attributes"]["error.type"] == "_OTHER"
    ok = [s for s in spans if s["code"] != "ERROR"]
    assert ok and all("error.type" not in s["attributes"] for s in ok)
    # Documented residual (D16 Rules): contrib's file-path db.namespace / db.name and span name are kept.
    statement = by_statement["insert into t values ('bound-secret-x')"]
    names = {statement["attributes"].get("db.namespace"), statement["attributes"].get("db.name")}
    assert result["path"] in names, statement
    assert result["path"] in statement["name"], statement
    # The exception was captured on every failed span (statements through handle_error, connect through
    # record_exception), with the system from the span; the preset span returns before reading one.
    assert sorted(result["seen"]) == [["IntegrityError", "sqlite"], ["OperationalError", "sqlite"],
                                      ["OperationalError", "sqlite"]], result["seen"]


# --------------------------------------------------------------------------- A46: gRPC name table


@pytest.mark.parametrize("code, want", [
    (1, "CANCELLED"), (2, "UNKNOWN"), (5, "NOT_FOUND"), (6, "ALREADY_EXISTS"), (9, "FAILED_PRECONDITION"),
    (13, "INTERNAL"), (16, "UNAUTHENTICATED"),
    (0, "_OTHER"), (17, "_OTHER"), (-1, "_OTHER"), (None, "_OTHER"), (True, "_OTHER"), ("9", "_OTHER"),
])
def test_grpc_error_type(code, want):
    assert _grpc_status.grpc_error_type(code) == want


def test_grpc_name_table_covers_the_16_error_codes():
    assert sorted(_grpc_status._names()) == list(range(1, 17))


def _grpc_span(attributes, status=None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = _grpc_status._GrpcErrorTypeTracerProvider(provider).get_tracer("t")
    with tracer.start_as_current_span("/a.B/C", kind=SpanKind.CLIENT, attributes=attributes,
                                      end_on_exit=True) as span:
        if status is not None:
            span.set_status(status)
    (finished,) = exporter.get_finished_spans()
    return finished


@pytest.mark.parametrize("attributes, want", [
    ({"rpc.grpc.status_code": 9}, "FAILED_PRECONDITION"),
    ({"rpc.grpc.status_code": 0}, "_OTHER"),   # contrib starts every span with 0: a non-RpcError failure
    ({}, "_OTHER"),
    ({"rpc.grpc.status_code": 42}, "_OTHER"),
    ({"rpc.grpc.status_code": 9, "error.type": "app_value"}, "app_value"),
])
def test_grpc_end_hook_on_error_spans(attributes, want):
    assert _grpc_span(attributes, Status(StatusCode.ERROR)).attributes["error.type"] == want


def test_grpc_end_hook_leaves_unset_and_ok_spans():
    assert "error.type" not in _grpc_span({"rpc.grpc.status_code": 5}).attributes
    assert "error.type" not in _grpc_span({"rpc.grpc.status_code": 0}, Status(StatusCode.OK)).attributes


def test_grpc_end_hook_never_raises(monkeypatch):
    def boom(span):
        raise RuntimeError("boom")

    monkeypatch.setattr(_grpc_status, "_set_error_type", boom)
    assert _grpc_span({"rpc.grpc.status_code": 9}, Status(StatusCode.ERROR)).status.status_code is StatusCode.ERROR


def test_grpc_wrapper_leaves_slotted_and_non_recording_spans():
    class Slotted:
        __slots__ = ()

        def end(self):
            pass

    s = Slotted()
    assert _grpc_status._set_error_type_at_end(s) is s
    span = _grpc_status._set_error_type_at_end(trace.INVALID_SPAN)
    span.end()


def test_grpc_instrument_kwargs_wrap_the_global_provider():
    kwargs = _grpc_status.grpc_instrument_kwargs()
    assert isinstance(kwargs["tracer_provider"], _grpc_status._GrpcErrorTypeTracerProvider)


# --------------------------------------------------------------------------- A46: real grpcio


_SERVICE = "parity.v1.D16"
_REQUEST_CODES = {b"ok": None, b"fp": "FAILED_PRECONDITION", b"ae": "ALREADY_EXISTS", b"in": "INTERNAL",
                  b"nf": "NOT_FOUND", b"preset": "INTERNAL"}


def _code(grpc, request):
    name = _REQUEST_CODES.get(bytes(request))
    return None if name is None else getattr(grpc.StatusCode, name)


def _handlers(grpc):
    def unary(request, context):
        if request == b"raise":
            raise ValueError("handler bug")
        if request == b"preset":
            trace.get_current_span().set_attribute("error.type", "app_value")
        code = _code(grpc, request)
        if code is not None:
            context.abort(code, "details with bound-secret")
        return b"ok"

    def stream(request, context):
        yield b"first"
        code = _code(grpc, request)
        if code is not None:
            context.abort(code, "stream details")
        yield b"second"

    def bidi(request_iterator, context):
        for request in request_iterator:
            code = _code(grpc, request)
            if code is not None:
                context.abort(code, "bidi details")
            yield request

    def collect(request_iterator, context):
        for request in request_iterator:
            code = _code(grpc, request)
            if code is not None:
                context.abort(code, "collect details")
        return b"ok"

    return grpc.method_handlers_generic_handler(_SERVICE, {
        "Collect": grpc.stream_unary_rpc_method_handler(collect),
        "Unary": grpc.unary_unary_rpc_method_handler(unary),
        "Stream": grpc.unary_stream_rpc_method_handler(stream),
        "Bidi": grpc.stream_stream_rpc_method_handler(bidi),
    })


def _wait_spans(exporter, count, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        spans = exporter.get_finished_spans()
        if len(spans) >= count:
            return spans
        time.sleep(0.02)
    return exporter.get_finished_spans()


def _exporter():
    setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def _pair(spans):
    """{kind: span} of one call (the exporter is cleared between calls)."""
    by_kind = {}
    for s in spans:
        by_kind.setdefault(s.kind.name, []).append(s)
    return {k: v[0] for k, v in by_kind.items() if len(v) == 1}


def _error_types(spans):
    pair = _pair(spans)
    return (pair["CLIENT"].attributes.get("error.type"), pair["SERVER"].attributes.get("error.type"))


class TestSyncGrpcErrorType:
    @pytest.fixture()
    def env(self):
        grpc = pytest.importorskip("grpc")
        exporter = _exporter()
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        server.add_generic_rpc_handlers((_handlers(grpc),))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        yield grpc, channel, exporter
        channel.close()
        server.stop(0.5)

    def _unary(self, env, request):
        grpc, channel, exporter = env
        exporter.clear()
        with pytest.raises(grpc.RpcError) if request != b"ok" else _nullcontext():
            channel.unary_unary(f"/{_SERVICE}/Unary")(request, timeout=5)
        return _wait_spans(exporter, 2)

    @pytest.mark.parametrize("request_, client, server", [
        (b"fp", "FAILED_PRECONDITION", None),   # not a server error code: the server span stays Unset
        (b"ae", "ALREADY_EXISTS", None),
        (b"nf", "NOT_FOUND", None),
        (b"in", "INTERNAL", "INTERNAL"),
        (b"ok", None, None),
        (b"preset", "INTERNAL", "app_value"),   # a value the application set is kept
    ])
    def test_unary(self, env, request_, client, server):
        spans = self._unary(env, request_)
        assert _error_types(spans) == (client, server), [(s.kind, s.attributes) for s in spans]
        pair = _pair(spans)
        if server is None:
            assert "error.type" not in pair["SERVER"].attributes
        if request_ in (b"nf", b"fp", b"ae"):
            assert pair["SERVER"].status.status_code is StatusCode.UNSET

    def test_server_handler_that_raises_is_the_documented_unknown_gap(self, env):
        """D16 "Not done here": the peer gets UNKNOWN, the SERVER span stays Unset (code 0)."""
        spans = self._unary(env, b"raise")
        pair = _pair(spans)
        assert pair["CLIENT"].attributes["error.type"] == "UNKNOWN"
        assert pair["SERVER"].status.status_code is StatusCode.UNSET
        assert "error.type" not in pair["SERVER"].attributes

    def test_future(self, env):
        grpc, channel, exporter = env
        exporter.clear()
        call = channel.unary_unary(f"/{_SERVICE}/Unary").future(b"fp", timeout=5)
        assert call.exception() is not None and call.code() is grpc.StatusCode.FAILED_PRECONDITION
        spans = _wait_spans(exporter, 2)
        assert _error_types(spans) == ("FAILED_PRECONDITION", None)

    def test_unary_stream(self, env):
        grpc, channel, exporter = env
        exporter.clear()
        with pytest.raises(grpc.RpcError):
            list(channel.unary_stream(f"/{_SERVICE}/Stream")(b"in", timeout=5))
        assert _error_types(_wait_spans(exporter, 2)) == ("INTERNAL", "INTERNAL")
        exporter.clear()
        with pytest.raises(grpc.RpcError):
            list(channel.unary_stream(f"/{_SERVICE}/Stream")(b"ae", timeout=5))
        assert _error_types(_wait_spans(exporter, 2)) == ("ALREADY_EXISTS", None)

    def test_bidi(self, env):
        grpc, channel, exporter = env
        exporter.clear()
        with pytest.raises(grpc.RpcError):
            list(channel.stream_stream(f"/{_SERVICE}/Bidi")(iter([b"ok", b"fp"]), timeout=5))
        spans = _wait_spans(exporter, 2)
        assert _pair(spans)["CLIENT"].attributes["error.type"] == "FAILED_PRECONDITION", spans

    def test_with_call(self, env):
        grpc, channel, exporter = env
        exporter.clear()
        with pytest.raises(grpc.RpcError):
            channel.unary_unary(f"/{_SERVICE}/Unary").with_call(b"ae", timeout=5)
        assert _error_types(_wait_spans(exporter, 2)) == ("ALREADY_EXISTS", None)

    def test_client_stream(self, env):
        grpc, channel, exporter = env
        exporter.clear()
        with pytest.raises(grpc.RpcError):
            channel.stream_unary(f"/{_SERVICE}/Collect")(iter([b"ok", b"in"]), timeout=5)
        assert _error_types(_wait_spans(exporter, 2)) == ("INTERNAL", "INTERNAL")


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_aio_unary_client_and_server():
    grpc = pytest.importorskip("grpc")
    exporter = _exporter()
    results = {}

    async def run():
        server = grpc.aio.server()

        async def unary(request, context):
            code = _code(grpc, request)
            if code is not None:
                await context.abort(code, "aio details")
            return b"ok"

        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(_SERVICE, {
            "Unary": grpc.unary_unary_rpc_method_handler(unary)}),))
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                for request in (b"fp", b"in", b"ok"):
                    exporter.clear()
                    try:
                        await channel.unary_unary(f"/{_SERVICE}/Unary")(request, timeout=5)
                    except grpc.aio.AioRpcError:
                        pass
                    for _ in range(250):  # the client span ends in a done callback on this loop
                        if len(exporter.get_finished_spans()) >= 2:
                            break
                        await asyncio.sleep(0.02)
                    results[request] = _error_types(exporter.get_finished_spans())
        finally:
            await server.stop(0.5)

    asyncio.run(run())
    assert results == {b"fp": ("FAILED_PRECONDITION", None), b"in": ("INTERNAL", "INTERNAL"), b"ok": (None, None)}


def test_bind_sync_client_unknown_layout_warns_once(monkeypatch, caplog):
    from staffops_otel_helper import _diag

    monkeypatch.setattr(_diag, "_emitted", set())

    class Other:  # no wrapper_fn / _filter: another contrib layout
        pass

    with caplog.at_level("WARNING", logger="staffops_otel_helper"):
        assert _grpc_status.bind_sync_client(Other(), TracerProvider()) is False
        assert _grpc_status.bind_sync_client(Other(), TracerProvider()) is False
    assert caplog.text.count("gRPC sync client spans carry no error.type") == 1


def test_bind_and_unbind_sync_client():
    pytest.importorskip("grpc")
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient

    instrumentor = GrpcInstrumentorClient()
    assert _grpc_status.bind_sync_client(instrumentor, TracerProvider()) is True
    assert "wrapper_fn" in vars(instrumentor)
    _grpc_status.unbind_sync_client(instrumentor)
    assert "wrapper_fn" not in vars(instrumentor)
    _grpc_status.unbind_sync_client(instrumentor)  # idempotent


def test_sync_client_seam_is_what_bind_sync_client_replaces():
    """Fails on a contrib change: the bug worked around (tracer_provider read from the channel call) and the
    public pieces the replacement uses. Add the contrib release to the release skill's bump checklist."""
    pytest.importorskip("grpc")
    import inspect

    from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient

    source = inspect.getsource(GrpcInstrumentorClient.wrapper_fn)
    for fragment in ('kwargs.get("tracer_provider")', "client_interceptor(", "self._filter"):
        assert fragment in source, fragment


def test_reset_telemetry_unbinds_the_sync_client():
    pytest.importorskip("grpc")
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient

    from staffops_otel_helper.setup import reset_telemetry

    setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
    assert "wrapper_fn" in vars(GrpcInstrumentorClient())
    reset_telemetry()
    assert "wrapper_fn" not in vars(GrpcInstrumentorClient())


def test_refused_or_failed_instrument_unbinds_the_sync_client(monkeypatch):
    pytest.importorskip("grpc")
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient

    from staffops_otel_helper import instrumentation

    # Refused (instrument() a no-op, as for an unsupported grpcio): warned path, nothing of ours left.
    monkeypatch.setattr(GrpcInstrumentorClient, "instrument", lambda self, **kw: None)
    done: list = []
    try:
        instrumentation.configure_instrumentations(TelemetryOptions(service_name="s"), done)
        assert "wrapper_fn" not in vars(GrpcInstrumentorClient())
    finally:
        instrumentation.uninstrument(done)

    def boom(self, **kw):
        raise RuntimeError("instrument failed")

    monkeypatch.setattr(GrpcInstrumentorClient, "instrument", boom)
    done = []
    try:
        with pytest.raises(RuntimeError):
            instrumentation.configure_instrumentations(TelemetryOptions(service_name="s"), done)
        assert "wrapper_fn" not in vars(GrpcInstrumentorClient())
    finally:
        instrumentation.uninstrument(done)
