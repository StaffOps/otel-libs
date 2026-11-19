"""DB exception detail goes to a correlated log record (task A36, ADR-014).

Contract design D1 row "DB error text in exception events", amendment of
2026-11-18: a failed SQL span keeps no message or stack trace in any attribute
or event (a recorded exception keeps an ``exception`` event with
``exception.type`` only), and one ERROR record on the D15 channel
(``staffops_otel_helper.failure``), in the span's context, carries the
message, ``exception.type``, ``exception.message`` and ``exception.stacktrace``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
from opentelemetry.trace import Status, StatusCode

from staffops_otel_helper import _db_status, _failure

FAILURE_LOGGER = "staffops_otel_helper.failure"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self.records: list[tuple[logging.LogRecord, trace.SpanContext]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record, trace.get_current_span().get_span_context()))


@pytest.fixture
def records():
    capture = _Capture()
    lg = logging.getLogger(FAILURE_LOGGER)
    level = lg.level
    lg.addHandler(capture)
    yield capture
    lg.removeHandler(capture)
    lg.setLevel(level)


def _tracer(sampler=None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=sampler) if sampler else TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("a36"), exporter


def _span_text(span) -> list[str]:
    out = [span.name, span.status.description or ""]
    out += [f"{k}={v}" for k, v in (span.attributes or {}).items()]
    for e in span.events:
        out.append(e.name)
        out += [f"{k}={v}" for k, v in (e.attributes or {}).items()]
    return out


class DuplicateEntry(Exception):
    pass


def test_statement_failure_record_in_the_span_context(records):
    """contrib sqlalchemy's path: set_status(ERROR, str(exc)) inside the except, no recorded exception."""
    tracer, exporter = _tracer()
    span = _db_status._drop_error_description(tracer.start_span("INSERT", attributes={"db.system.name": "mysql"}))
    exc = DuplicateEntry("Duplicate entry 'bound-secret-a36' for key 't.v'")
    try:
        raise exc
    except DuplicateEntry:
        span.set_status(Status(StatusCode.ERROR, str(exc)))
    span.end()
    (finished,) = exporter.get_finished_spans()
    assert finished.status.status_code is StatusCode.ERROR and finished.status.description is None
    assert list(finished.events) == [], "contrib records no exception on statement spans: none is added"
    assert not any("bound-secret-a36" in v or "Traceback" in v for v in _span_text(finished))
    (record, ctx), = records.records
    assert record.levelno == logging.ERROR and record.exc_info is None
    assert record.getMessage() == str(exc)
    assert getattr(record, "exception.type") == f"{__name__}.DuplicateEntry"
    assert getattr(record, "exception.message") == str(exc)
    stack = getattr(record, "exception.stacktrace")
    assert stack.startswith("Traceback") and "test_statement_failure_record_in_the_span_context" in stack
    assert (ctx.trace_id, ctx.span_id) == (finished.context.trace_id, finished.context.span_id)


def test_recorded_exception_keeps_a_type_only_event(records):
    """The ``connect`` span's path: use_span records the exception, then sets the status."""
    tracer, exporter = _tracer()
    span = _db_status._drop_error_description(tracer.start_span("connect"))
    exc = DuplicateEntry("unable to open database: /nonexistent/bound-secret-a36.db")
    with pytest.raises(DuplicateEntry):
        with trace.use_span(span, end_on_exit=True):
            raise exc
    (finished,) = exporter.get_finished_spans()
    assert [(e.name, dict(e.attributes)) for e in finished.events] == [
        ("exception", {"exception.type": f"{__name__}.DuplicateEntry"})]
    assert finished.status.description is None
    assert not any("bound-secret-a36" in v or "Traceback" in v for v in _span_text(finished))
    (record, ctx), = records.records
    assert record.getMessage() == str(exc) and getattr(record, "exception.type") == f"{__name__}.DuplicateEntry"
    assert ctx.span_id == finished.context.span_id


def test_error_without_an_exception_writes_the_description_only(records):
    tracer, exporter = _tracer()
    span = _db_status._drop_error_description(tracer.start_span("q"))
    span.set_status(StatusCode.ERROR, "server said 'bound-secret-a36'")  # outside any except: nothing captured
    span.end()
    (finished,) = exporter.get_finished_spans()
    assert finished.status.description is None
    (record, ctx), = records.records
    assert record.getMessage() == "server said 'bound-secret-a36'"
    assert getattr(record, "exception.message") == "server said 'bound-secret-a36'"
    assert not hasattr(record, "exception.type") and not hasattr(record, "exception.stacktrace")
    assert ctx.span_id == finished.context.span_id


def test_error_without_text_or_exception_writes_nothing_and_success_writes_nothing(records):
    tracer, _ = _tracer()
    span = _db_status._drop_error_description(tracer.start_span("q"))
    span.set_status(Status(StatusCode.ERROR))
    span.end()
    ok = _db_status._drop_error_description(tracer.start_span("ok"))
    ok.set_status(Status(StatusCode.OK))
    ok.end()
    assert records.records == []


def test_cancellation_writes_no_record_and_a_late_arm_writes_once(records):
    tracer, _ = _tracer()
    span = _db_status._drop_error_description(tracer.start_span("connect"))
    span.record_exception(KeyboardInterrupt())  # a cancellation: type-only event, no record
    span.set_status(StatusCode.ERROR, "interrupted")
    span.end()
    assert records.records == []
    ok = _db_status._drop_error_description(tracer.start_span("ok"))
    ok.end()  # ended without an active exception (a successful statement)
    getattr(ok, _db_status._ARM)(DuplicateEntry("later fetch"), "sqlite")  # N1: not this span's failure
    assert records.records == []
    late = _db_status._drop_error_description(tracer.start_span("q"))
    arm = getattr(late, _db_status._ARM)
    try:
        raise DuplicateEntry("after end")
    except DuplicateEntry as failure:
        late.end()  # contrib ended it inside the handler (an unsampled span: no set_status)
        try:
            raise RuntimeError("newer")  # not the end-time exception nor in its __context__ chain
        except RuntimeError as newer:
            arm(newer, "sqlite")
        arm(failure, "sqlite")  # the end-time exception: written
        arm(failure, "sqlite")  # one record per span
    arm("not an exception")
    assert [r.getMessage() for r, _ in records.records] == ["after end"]
    chained = _db_status._drop_error_description(tracer.start_span("c"))
    try:
        try:
            raise DuplicateEntry("driver error")
        except DuplicateEntry as e:
            inner = e
            raise RuntimeError("wrapper") from None
    except RuntimeError:
        chained.end()  # active: the wrapper, whose __context__ is the driver error
    getattr(chained, _db_status._ARM)(inner, "sqlite")
    assert [r.getMessage() for r, _ in records.records] == ["after end", "driver error"]
    record = records.records[0][0]
    assert getattr(record, "db.system.name") == "sqlite" and getattr(record, "error.type") == "_OTHER"


def test_unsampled_record_attributes_from_the_dialect_and_the_code():
    tracer, _ = _tracer(ALWAYS_OFF)
    span = tracer.start_span("q")

    class PgError(Exception):
        pgcode = "23505"

    assert _db_status._record_db_attributes(span, PgError("x"), "postgresql") == {
        "db.system.name": "postgresql", "db.response.status_code": "23505", "error.type": "23505"}
    assert _db_status._record_db_attributes(span, None, None) == {"error.type": "_OTHER"}
    rec = _tracer()[0].start_span("r", attributes={"db.system": "mssql", "error.type": "_OTHER"})
    assert _db_status._record_db_attributes(rec, None, None) == {"error.type": "_OTHER", "db.system.name": "microsoft.sql_server"}


def test_listener_never_raises_and_ignores_foreign_spans():
    class Ctx:
        execution_context = None
        original_exception = ValueError("x")

    _db_status._sql_handle_error(Ctx())  # no span: nothing

    class Broken:
        @property
        def execution_context(self):
            raise RuntimeError("broken")

    _db_status._sql_handle_error(Broken())  # swallowed


def test_stack_is_cut_in_the_middle_above_12_kib():
    text = "\n".join(f"  File \"app.py\", line {i}, in f{i}" for i in range(2000))
    cut = _failure._cut_middle(text)
    assert len(cut.encode()) <= 12 << 10
    assert cut.startswith("  File \"app.py\", line 0,") and cut.endswith("in f1999")
    assert "lines elided by otel-helper..." in cut
    assert _failure._cut_middle("short") == "short"


def test_db_stacktrace_falls_back_to_the_plain_traceback(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("no stack")

    monkeypatch.setattr(_failure.traceback, "TracebackException", broken)
    try:
        raise DuplicateEntry("x")
    except DuplicateEntry as e:
        exc = e
    monkeypatch.undo()
    assert _failure.db_stacktrace(exc).startswith("Traceback")


def test_one_record_per_span_even_when_the_status_is_set_twice(records):
    tracer, _ = _tracer()
    span = _db_status._drop_error_description(tracer.start_span("q"))
    try:
        raise DuplicateEntry("first")
    except DuplicateEntry as e:
        span.set_status(Status(StatusCode.ERROR, str(e)))
        span.set_status(Status(StatusCode.ERROR, "second"))
    span.end()
    assert [r.getMessage() for r, _ in records.records] == ["first"]


def test_internal_errors_never_reach_the_instrumented_call(monkeypatch, records, caplog):
    tracer, exporter = _tracer()
    span = _db_status._drop_error_description(tracer.start_span("q"))

    def broken(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(_db_status, "_emit_sql_record", broken)
    span.set_status(StatusCode.ERROR, "x")
    span.end()  # no raise
    assert len(exporter.get_finished_spans()) == 1
    # A failing logger: one diagnostics line, never a raise.
    monkeypatch.undo()
    lg = logging.getLogger(FAILURE_LOGGER)

    class Raising(logging.Handler):
        def emit(self, record):
            raise RuntimeError("handler")

        def handleError(self, record):  # logging would print; re-raise to hit the helper's guard
            raise RuntimeError("handler")

    handler = Raising()
    lg.addHandler(handler)
    try:
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper.tracing"):
            _failure.emit_exception_record(trace.INVALID_SPAN, "m", {})
    finally:
        lg.removeHandler(handler)
    assert "Could not write an exception log record (RuntimeError)" in caplog.text


def test_record_exception_event_failure_is_swallowed(records):
    tracer, _ = _tracer()

    def bad_event(*a, **k):
        raise RuntimeError("no events")

    raw = tracer.start_span("connect")
    raw.add_event = bad_event  # type: ignore[method-assign]  (the wrapper reads it at wrap time)
    wrapped = _db_status._drop_error_description(raw)
    wrapped.record_exception(DuplicateEntry("x"))  # no raise
    wrapped.set_status(StatusCode.ERROR, "x")
    wrapped.end()
    assert len(records.records) == 1 and records.records[0][0].getMessage() == "x"


_PRELUDE = """
import os
if os.environ.get("PARITY_COVERAGE_DATA"):
    import coverage
    _cov = coverage.Coverage(data_file=os.environ["PARITY_COVERAGE_DATA"], data_suffix=True,
                             source=["staffops_otel_helper"], config_file=False)
    _cov.start()
    import atexit
    atexit.register(lambda: (_cov.stop(), _cov.save()))
"""

_SCRIPT = """
import json, sys
import sqlalchemy
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from staffops_otel_helper import setup_telemetry, shutdown_telemetry
setup_telemetry()
ex = InMemorySpanExporter()
trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(ex))
from staffops_otel_helper.ext.sql import instrument_sql
assert instrument_sql()
def app_caller_e2e():
    with sqlalchemy.create_engine("sqlite://").connect() as c:
        try:  # SQLite quotes the bound value: unable to open database: /nonexistent-a36/bound-secret-a36.db
            c.execute(sqlalchemy.text("ATTACH DATABASE :p AS other"), {"p": "/nonexistent-a36/bound-secret-a36.db"})
        except Exception as e:
            print("ERR", str(e).splitlines()[0], flush=True)
    try:  # the connect span: a recorded exception (sqlite cannot open a file in a missing directory)
        sqlalchemy.create_engine("sqlite:////nonexistent-a36/connect-secret-a36.db").connect()
    except Exception:
        pass
app_caller_e2e()
out = []
for s in ex.get_finished_spans():
    out.append({"name": s.name, "code": s.status.status_code.name, "description": s.status.description,
                "trace_id": format(s.context.trace_id, "032x"), "span_id": format(s.context.span_id, "016x"),
                "attributes": {k: str(v) for k, v in s.attributes.items()},
                "events": [[e.name, {k: str(v) for k, v in e.attributes.items()}] for e in s.events]})
print("SPANS " + json.dumps(out), flush=True)
shutdown_telemetry()
"""


def test_sqlalchemy_sqlite_end_to_end_stdout_fallback():
    """instrument_sql() + setup_telemetry() without an endpoint: the record is a correlated stdout JSON line."""
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("opentelemetry.instrumentation.sqlalchemy")
    env = {k: v for k, v in os.environ.items() if not (k.startswith("OTEL_") or k in ("SERVICE_NAME", "ENVIRONMENT"))}
    env.update({"OTEL_SERVICE_NAME": "a36", "OTEL_HELPER_METRICS_PORT": "0", "OTEL_METRICS_EXPORTER": "none",
                "OTEL_TRACES_EXPORTER": "none", "ENVIRONMENT": "DEV"})
    proc = subprocess.run([sys.executable, "-c", _PRELUDE + textwrap.dedent(_SCRIPT)], env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "ERR (sqlite3.OperationalError) unable to open database: /nonexistent-a36/bound-secret-a36.db" in proc.stdout
    spans = json.loads(next(x for x in proc.stdout.splitlines() if x.startswith("SPANS "))[6:])
    failed = [s for s in spans if s["code"] == "ERROR"]
    assert sorted(s["name"] for s in failed) == ["ATTACH", "connect"], spans
    for s in failed:
        assert s["description"] is None
        text = [str(v) for v in s["attributes"].values()] + [json.dumps(e) for e in s["events"]]
        for v in text:
            assert "unable to open database" not in v and "Traceback" not in v and "bound-secret-a36" not in v, v
    connect = next(s for s in failed if s["name"] == "connect")
    # SQLAlchemy's wrapper on the connect span (use_span sees it); the DBAPI error on the statement span.
    types = {"connect": "sqlalchemy.exc.OperationalError", "ATTACH": "sqlite3.OperationalError"}
    assert connect["events"] == [["exception", {"exception.type": types["connect"]}]]
    attach = next(s for s in failed if s["name"] == "ATTACH")
    assert attach["events"] == []
    lines = [json.loads(x) for x in proc.stdout.splitlines() if x.startswith("{") and FAILURE_LOGGER in x]
    assert len(lines) == 2, proc.stdout
    by_span = {line["span_id"]: line for line in lines}
    for s in failed:
        line = by_span[s["span_id"]]
        assert line["trace_id"] == s["trace_id"] and line["level"] == "ERROR"
        assert line["exception.type"] == types[s["name"]], line
        assert line["msg"] == line["exception.message"] and "unable to open database" in line["msg"]
        assert line["exception.stacktrace"].startswith("Traceback")
        assert "in app_caller_e2e" in line["exception.stacktrace"], "the calling application frame is in the stack"
        assert "/opentelemetry/" not in line["exception.stacktrace"]
        assert line["db.system.name"] == "sqlite" and line["error.type"] == "_OTHER"
        # Key order (A23 amendment): trace ids right after logger.
        assert list(line)[:6] == ["time", "level", "msg", "logger", "trace_id", "span_id"]
    assert "bound-secret-a36" in by_span[attach["span_id"]]["msg"], "the detail is in the log, by design (ADR-014)"


_UNSAMPLED = """
import json, logging, sys
import sqlalchemy
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ParentBased, ALWAYS_ON
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
mode = sys.argv[1]
tp = TracerProvider(sampler=ALWAYS_OFF if mode == "always_off" else ParentBased(ALWAYS_ON))
trace.set_tracer_provider(tp)
out = []
class Capture(logging.Handler):
    def emit(self, r):
        c = trace.get_current_span().get_span_context()
        out.append({"msg": r.getMessage(), "trace_id": format(c.trace_id, "032x"), "span_id": format(c.span_id, "016x"),
                    "sampled": c.trace_flags.sampled, **{k: getattr(r, k, None) for k in
                    ("exception.type", "exception.stacktrace", "db.system.name", "error.type")}})
logging.getLogger("staffops_otel_helper.failure").addHandler(Capture())
from staffops_otel_helper.ext.sql import instrument_sql
assert instrument_sql()
parent = trace.set_span_in_context(NonRecordingSpan(SpanContext(
    0x4bf92f3577b34da6a3ce929d0e0e4736, 0x00f067aa0ba902b7, is_remote=True, trace_flags=TraceFlags(0))))
def app_caller_a36():
    with sqlalchemy.create_engine("sqlite://").connect() as c:
        try:
            c.execute(sqlalchemy.text("ATTACH DATABASE :p AS other"), {"p": "/nonexistent-a36/unsampled.db"})
        except Exception:
            pass
    try:
        sqlalchemy.create_engine("sqlite:////nonexistent-a36/connect.db").connect()
    except Exception:
        pass
from opentelemetry import context
token = context.attach(parent) if mode == "parent_unsampled" else None
app_caller_a36()
print("RECORDS " + json.dumps(out), flush=True)
"""


@pytest.mark.parametrize("mode", ["always_off", "parent_unsampled", "sampled"])
def test_sqlalchemy_unsampled_spans_still_write_their_records(mode):
    """Contrib reports nothing on an unsampled span: the helper's listener and connect wrapper do (A36 review)."""
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("opentelemetry.instrumentation.sqlalchemy")
    env = {k: v for k, v in os.environ.items() if not (k.startswith("OTEL_") or k in ("SERVICE_NAME", "ENVIRONMENT"))}
    proc = subprocess.run([sys.executable, "-c", _PRELUDE + textwrap.dedent(_UNSAMPLED),
                           "sampled" if mode == "sampled" else mode],
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    records = json.loads(next(x for x in proc.stdout.splitlines() if x.startswith("RECORDS "))[8:])
    assert len(records) == 2, records  # the ATTACH statement and the connect span, once each
    msgs = sorted(r["msg"].split(":")[0] for r in records)
    assert any("unable to open database" in r["msg"] for r in records), records
    for r in records:
        assert r["exception.type"] in ("sqlite3.OperationalError", "sqlalchemy.exc.OperationalError"), r
        assert r["span_id"] != "0" * 16 and len(r["trace_id"]) == 32
        assert "app_caller_a36" in r["exception.stacktrace"], "the application frame is in the stack"
        assert "staffops_otel_helper" not in r["exception.stacktrace"] and "/opentelemetry/" not in r["exception.stacktrace"]
        assert r["error.type"] == "_OTHER"
    statement = next(r for r in records if r["exception.type"] == "sqlite3.OperationalError")
    connect = next(r for r in records if r is not statement)
    assert statement["db.system.name"] == "sqlite"
    assert connect["db.system.name"] == "sqlite", "the dialect name reaches the connect record (N4)"
    # N2: the callers of the handling frame, in order, without SQLAlchemy's error handler between them.
    stack = statement["exception.stacktrace"]
    app, ctx, single = (stack.index(x) for x in ("in app_caller_a36", "in _execute_context", "in _exec_single_context"))
    assert app < ctx < single, stack
    assert "_handle_dbapi_exception" not in stack[ctx:single]
    if mode == "parent_unsampled":
        assert {r["trace_id"] for r in records} == {"4bf92f3577b34da6a3ce929d0e0e4736"}
    if mode != "sampled":
        assert not any(r["sampled"] for r in records)
    assert msgs


_FETCH = """
import json, logging, sys
import sqlalchemy
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON
tp = TracerProvider(sampler=ALWAYS_ON if sys.argv[1] == "sampled" else ALWAYS_OFF)
ex = InMemorySpanExporter(); tp.add_span_processor(SimpleSpanProcessor(ex))
trace.set_tracer_provider(tp)
out = []
class Capture(logging.Handler):
    def emit(self, r):
        out.append(r.getMessage())
logging.getLogger("staffops_otel_helper.failure").addHandler(Capture())
from staffops_otel_helper.ext.sql import instrument_sql
assert instrument_sql()
# The statement succeeds (its first row); the second row fails while it is fetched: integer overflow.
SQL = "SELECT CASE WHEN v = 2 THEN abs(-9223372036854775807 - 1) ELSE v END FROM (SELECT 1 AS v UNION ALL SELECT 2)"
with sqlalchemy.create_engine("sqlite://").connect() as c:
    result = c.execution_options(stream_results=True, yield_per=1).execute(sqlalchemy.text(SQL))
    try:
        result.fetchall()
    except Exception as e:
        print("FETCH-ERR", type(e).__name__, flush=True)
spans = [{"name": s.name, "code": s.status.status_code.name} for s in ex.get_finished_spans()]
print("OUT " + json.dumps({"records": out, "spans": spans}), flush=True)
"""


@pytest.mark.parametrize("mode", ["sampled", "unsampled"])
def test_fetch_failure_after_a_successful_statement_writes_no_record(mode):
    """A36 review N1: the listener must not arm a statement span that already ended successfully."""
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("opentelemetry.instrumentation.sqlalchemy")
    env = {k: v for k, v in os.environ.items() if not (k.startswith("OTEL_") or k in ("SERVICE_NAME", "ENVIRONMENT"))}
    proc = subprocess.run([sys.executable, "-c", _PRELUDE + textwrap.dedent(_FETCH), mode],
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "FETCH-ERR OperationalError" in proc.stdout, proc.stdout
    out = json.loads(next(x for x in proc.stdout.splitlines() if x.startswith("OUT "))[4:])
    assert out["records"] == [], out
    if mode == "sampled":
        assert [s["code"] for s in out["spans"] if s["name"] != "connect"] == ["UNSET"], out


def test_semconv_system_names():
    assert [_db_status.semconv_system(n) for n in ("mssql", "oracle", "postgresql", "mysql", "mariadb", "sqlite", "x")] == [
        "microsoft.sql_server", "oracle.db", "postgresql", "mysql", "mariadb", "sqlite", "x"]
    tracer, _ = _tracer()
    rec = tracer.start_span("r", attributes={"db.system": "mssql"})
    assert _db_status._record_db_attributes(rec, None, None)["db.system.name"] == "microsoft.sql_server"
    bare = tracer.start_span("b", attributes={"k": "v"})
    assert _db_status._record_db_attributes(bare, None, "oracle") == {"db.system.name": "oracle.db"}


def test_listener_install_is_idempotent_and_survives_uninstrument():
    pytest.importorskip("sqlalchemy")
    _db_status.install_sql_error_listener()
    _db_status.install_sql_error_listener()
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    assert event.contains(Engine, "handle_error", _db_status._sql_handle_error)
