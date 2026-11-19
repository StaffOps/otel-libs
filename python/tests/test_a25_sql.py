"""A25 (2026-11-03): database semconv opt-in normalization and DB error status.

R2: ``OTEL_SEMCONV_STABILITY_OPT_IN`` is normalized before contrib reads it —
ASCII-folded, ``database/dup`` appended when no database mode is named — so the
legacy ``db.system`` is kept unless ``database`` is chosen without ``/dup`` (Go
``ext/otelsql`` and Node ``-sql``). R1: a failed statement's span keeps the
ERROR code and drops the status description (the database error text, which
can quote a value), as Go and Node.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Status, StatusCode

from staffops_otel_helper import _semconv
from staffops_otel_helper.ext import sql as sql_ext


@pytest.mark.parametrize("value, expected", [
    (None, "http,database/dup"),
    ("", "http,database/dup"),         # empty = unset (2026-11-03, L-e)
    (" \t", "http,database/dup"),      # whitespace-only = unset
    ("\u00a0", "\u00a0,database/dup"),  # NBSP is not in the trim set: a set value
    ("HTTP/DUP", "http/dup,database/dup"),
    ("http", "http,database/dup"),
    ("DATABASE", "database"),
    ("database", "database"),
    ("Http, Database/Dup", "http, database/dup"),
    ("http,database ", "http,database ,database/dup"),  # NBSP is not in the trim set
    ("databaſe", "databaſe,database/dup"),  # U+017F does not fold to s (ASCII only)
])
def test_normalize_semconv_opt_in(value, expected):
    assert _semconv.normalize_semconv_opt_in(value) == expected


def test_ensure_semconv_default_writes_the_normalized_value(monkeypatch):
    monkeypatch.setattr(_semconv, "_warned", False)
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "HTTP")
    _semconv.ensure_semconv_default()
    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http,database/dup"


def _span():
    return TracerProvider().get_tracer("t").start_span("s")


def test_error_status_keeps_the_code_and_drops_the_description():
    span = sql_ext._drop_error_description(_span())
    span.set_status(Status(StatusCode.ERROR, "duplicate entry 'bound-secret-x'"))
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    span = sql_ext._drop_error_description(_span())
    span.set_status(StatusCode.ERROR, "bound-secret-y")
    assert span.status.description is None


def test_non_error_status_is_unchanged():
    span = sql_ext._drop_error_description(_span())
    span.set_status(StatusCode.OK)
    assert span.status.status_code is StatusCode.OK


def test_span_without_writable_set_status_is_left_alone():
    class Slotted:
        __slots__ = ()

        def set_status(self, *a):
            pass

    s = Slotted()
    assert sql_ext._drop_error_description(s) is s


_SCRIPT = textwrap.dedent("""
    import json
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    ex = InMemorySpanExporter(); tp = TracerProvider(); tp.add_span_processor(SimpleSpanProcessor(ex))
    trace.set_tracer_provider(tp)
    from staffops_otel_helper.ext.sql import instrument_sql
    import sqlalchemy
    assert instrument_sql()
    tracer = trace.get_tracer("app")
    with sqlalchemy.create_engine("sqlite://").connect() as c:
        c.execute(sqlalchemy.text("create table t (v text unique)"))
        c.execute(sqlalchemy.text("insert into t values ('bound-secret-x')"))
        for stmt in ["insert into t values ('bound-secret-x')", "select from where 'bound-secret-z'"]:
            try:
                c.execute(sqlalchemy.text(stmt))
            except Exception:
                pass
    with tracer.start_as_current_span("app") as s:
        s.set_status(trace.Status(trace.StatusCode.ERROR, "app text kept"))
    out = []
    for s in ex.get_finished_spans():
        out.append({"name": s.name, "scope": s.instrumentation_scope.name, "code": s.status.status_code.name,
                    "description": s.status.description, "attributes": {k: str(v) for k, v in s.attributes.items()}})
    print(json.dumps(out))
""")


def test_sqlalchemy_error_spans_carry_no_description():
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("opentelemetry.instrumentation.sqlalchemy")
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    out = subprocess.run([sys.executable, "-c", _SCRIPT], env=env, capture_output=True, text=True, check=True)
    spans = json.loads(out.stdout.strip().splitlines()[-1])
    db = [s for s in spans if s["scope"] == "opentelemetry.instrumentation.sqlalchemy"]
    errors = [s for s in db if s["code"] == "ERROR"]
    assert len(errors) == 2, spans
    for s in errors:
        assert s["description"] is None
    # The statement text is recorded as written (all languages); only the status is redacted.
    app = [s for s in spans if s["name"] == "app"]
    assert app[0]["description"] == "app text kept", "only the SQLAlchemy scope is affected"


_SETUP_SCRIPT = textwrap.dedent("""
    import json, logging, sys
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    mode = sys.argv[1]
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    if mode == "foreign":
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        SQLAlchemyInstrumentor().instrument()
    import sqlalchemy
    early = sqlalchemy.create_engine("sqlite://") if mode == "engine_before" else None
    from staffops_otel_helper import setup_telemetry, TelemetryOptions
    setup_telemetry(TelemetryOptions(service_name="svc"))
    ex = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(ex))
    if mode in ("setup_then_ext", "foreign"):
        from staffops_otel_helper.ext.sql import instrument_sql
        assert instrument_sql()
    if mode == "engine_before":
        from staffops_otel_helper.ext.sql import instrument_sql
        assert instrument_sql(early) is True
        assert instrument_sql(early) is True  # attached once: no doubled spans
    with (early or sqlalchemy.create_engine("sqlite://")).connect() as c:
        c.execute(sqlalchemy.text("create table t (v text unique)"))
        c.execute(sqlalchemy.text("insert into t values ('bound-secret-x')"))
        try:
            c.execute(sqlalchemy.text("insert into t values ('bound-secret-x')"))
        except Exception:
            pass
    try:  # a failed connect (L-a): sqlite cannot open a file in a missing directory
        sqlalchemy.create_engine("sqlite:////nonexistent-dir-otel/x.db").connect()
    except Exception:
        pass
    out = []
    for s in ex.get_finished_spans():
        out.append({"name": s.name, "scope": s.instrumentation_scope.name, "code": s.status.status_code.name,
                    "description": s.status.description, "events": [e.name for e in s.events]})
    print(json.dumps(out))
""")


def _run_setup(mode, errors_only=True):
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("opentelemetry.instrumentation.sqlalchemy")
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    out = subprocess.run([sys.executable, "-c", _SETUP_SCRIPT, mode], env=env, capture_output=True, text=True,
                         check=True)
    spans = [s for s in json.loads(out.stdout.strip().splitlines()[-1])
             if s["scope"] == "opentelemetry.instrumentation.sqlalchemy" and (s["code"] == "ERROR" or not errors_only)]
    return spans, out.stderr


@pytest.mark.parametrize("mode", ["default", "setup_then_ext"])
def test_setup_telemetry_sql_flag_path_drops_error_description(mode):
    """2026-11-03 fix round (P1): the deprecated SQL flag of setup_telemetry() uses the same wrapper."""
    spans, stderr = _run_setup(mode)
    by_name = {s["name"]: s for s in spans}
    assert len(spans) == 2, spans  # the duplicate insert and the failed connect
    for s in spans:
        assert s["description"] is None, s
    assert by_name["connect"]["events"] == ["exception"], "the connect span keeps its exception event"
    assert "already instrumented without the helper" not in stderr


def test_instrumented_elsewhere_is_kept_and_warned():
    """Uninstrumenting would drop the listeners of engines created so far: warned instead."""
    spans, stderr = _run_setup("foreign")
    assert "SQLAlchemy is already instrumented without the helper's tracer" in stderr
    assert any(s["description"] for s in spans), "the foreign instrumentation keeps contrib's description"


def test_engine_created_before_setup_is_traced_by_instrument_sql_engine():
    """2026-11-03 (M1): instrument_sql(engine) attaches the wrapped tracer to an engine created before setup."""
    spans, stderr = _run_setup("engine_before", errors_only=False)
    statements = [s for s in spans if s["name"] != "connect"]
    assert len(statements) == 3, spans  # create table, insert, failed insert: one span each (not doubled)
    failed = [s for s in statements if s["code"] == "ERROR"]
    assert len(failed) == 1 and failed[0]["description"] is None, failed
    assert "could not be traced" not in stderr


def test_attach_engine_unknown_contrib_layout_warns_and_returns_false(monkeypatch, caplog):
    pytest.importorskip("opentelemetry.instrumentation.sqlalchemy")
    from opentelemetry.instrumentation.sqlalchemy.engine import EngineTracer
    from staffops_otel_helper import _diag

    monkeypatch.setattr(EngineTracer, "_remove_event_listener_params", None)
    monkeypatch.setattr(_diag, "_emitted", set())
    with caplog.at_level("WARNING", logger="staffops_otel_helper"):
        assert sql_ext._attach_engine(object()) is False
    assert "could not be traced (TypeError)" in caplog.text


def test_attach_engine_already_traced_is_left_alone(monkeypatch):
    pytest.importorskip("opentelemetry.instrumentation.sqlalchemy")
    import weakref
    from opentelemetry.instrumentation.sqlalchemy.engine import EngineTracer

    class Engine:
        pass

    engine = Engine()
    monkeypatch.setattr(EngineTracer, "_remove_event_listener_params", [(weakref.ref(engine), "x", None)])
    assert sql_ext._attach_engine(engine) is True
