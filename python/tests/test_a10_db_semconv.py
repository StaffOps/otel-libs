"""R10 database semconv (A10 validation): ``db.system.name`` and ``db.system`` by default.

The helper defaults ``OTEL_SEMCONV_STABILITY_OPT_IN`` to ``http,database/dup``
(``_semconv.py``). The contrib stability mode is checked directly (always runs);
the emitted span keys are checked with real SQLAlchemy + sqlite when SQLAlchemy is
installed (it is not part of the default test env — no new dev dependency; the
A10 validation run installs ``sqlalchemy==2.0.x`` explicitly). Re-validated
2026-10-28 on contrib 0.63b0 and 0.66b1: unset/``http`` → ``db.system`` only;
``http,database/dup`` → both; ``http,database`` → ``db.system.name`` only.
"""

import os
import subprocess
import sys
import textwrap

import pytest

from staffops_otel_helper import _semconv

_SCRIPT = textwrap.dedent("""
    import os, sys, json
    if sys.argv[1] != "-":
        os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = sys.argv[1]
    from staffops_otel_helper._semconv import ensure_semconv_default
    ensure_semconv_default()
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    ex = InMemorySpanExporter(); tp = TracerProvider(); tp.add_span_processor(SimpleSpanProcessor(ex))
    trace.set_tracer_provider(tp)
    from staffops_otel_helper.ext.sql import instrument_sql
    import sqlalchemy
    assert instrument_sql()
    with sqlalchemy.create_engine("sqlite://").connect() as c:
        c.execute(sqlalchemy.text("select 1"))
    a = dict(ex.get_finished_spans()[-1].attributes)
    print(json.dumps({k: a[k] for k in a if k.startswith("db.system")}))
""")


def test_default_selects_database_dup_mode(monkeypatch):
    """The contrib stability parser maps the helper default to DATABASE_DUP (and HTTP)."""
    monkeypatch.setattr(_semconv, "_warned", False)
    _semconv.ensure_semconv_default()
    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http,database/dup"
    tokens = {t.strip() for t in os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"].split(",")}
    assert {"http", "database/dup"} <= tokens


@pytest.mark.parametrize("opt_in, expected", [
    ("-", {"db.system": "sqlite", "db.system.name": "sqlite"}),   # helper default
    ("http,database", {"db.system.name": "sqlite"}),               # user value wins
    ("http", {"db.system": "sqlite"}),
], ids=["default_dup", "database_new_only", "http_only_legacy"])
def test_sqlalchemy_db_system_keys(opt_in, expected):
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("opentelemetry.instrumentation.sqlalchemy")
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    out = subprocess.run([sys.executable, "-c", _SCRIPT, opt_in], env=env, capture_output=True,
                         text=True, check=True)
    import json

    assert json.loads(out.stdout.strip().splitlines()[-1]) == expected
