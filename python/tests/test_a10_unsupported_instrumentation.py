"""A10 / P18 (Python): an instrumentor that refuses the installed library version is warned.

Re-validated 2026-10-28 on contrib 0.63b0 and 0.66b1: with SQLAlchemy 2.1.6,
``SQLAlchemyInstrumentor`` declares ``sqlalchemy >= 1.0.0, < 2.1.0`` and
``instrument()`` returns without instrumenting (upstream logs one terse ERROR on
``opentelemetry.instrumentation.instrumentor``); 2.0.55 instruments. Redis
(``redis >= 2.6``) and botocore (``botocore~=1.0``) accepted redis 8.1.0 and
botocore 1.43.120 on both legs.

The installed SQLAlchemy is not part of the test environment, so the version
matrix uses real ``BaseInstrumentor`` subclasses whose declared range includes or
excludes the installed ``pytest`` version: the refusal is upstream's own check,
not a mock.
"""

import logging
import sys
from importlib.metadata import version
from types import ModuleType

import pytest
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

from otel_helper import instrumentation
from otel_helper._support import unsupported_dependency, warn_if_unsupported
from otel_helper.config import TelemetryOptions

PYTEST = version("pytest")
UNSUPPORTED = f"pytest < {PYTEST}"   # installed version just outside the range
SUPPORTED = f"pytest >= {PYTEST}"
MISSING = "otel-helper-no-such-dist >= 1"


def _instrumentor_class(name: str, deps: list[str]) -> type:
    def _instrument(self, **kwargs):
        self.calls = getattr(self, "calls", 0) + 1

    return type(name, (BaseInstrumentor,), {
        "instrumentation_dependencies": lambda self: deps,
        "_instrument": _instrument,
        "_uninstrument": lambda self, **kwargs: None,
    })


def _helper_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name.startswith("otel_helper") and r.levelno == logging.WARNING]


@pytest.fixture
def fresh(request):
    """A unique instrumentor class per test (BaseInstrumentor is a per-class singleton)."""
    created = []

    def make(deps, name="SQLAlchemyInstrumentor"):
        cls = _instrumentor_class(name, deps)
        created.append(cls)
        return cls

    yield make
    for cls in created:
        inst = cls()
        if inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()


class TestVersionMatrix:
    @pytest.mark.parametrize("deps, refused, warned", [
        ([UNSUPPORTED], True, True),           # SQLAlchemy 2.1.x vs contrib < 2.1.0
        ([SUPPORTED], False, False),           # SQLAlchemy 2.0.x
        ([MISSING], True, False),              # library not installed: skipped silently
        ([MISSING, UNSUPPORTED], True, True),  # a missing dep must not hide an out-of-range one
    ], ids=["out_of_range", "in_range", "missing", "missing_then_out_of_range"])
    def test_refusal_is_warned_only_when_installed_out_of_range(self, caplog, fresh, deps, refused, warned):
        inst = fresh(deps)()
        with caplog.at_level(logging.WARNING):
            inst.instrument()
            result = warn_if_unsupported(inst)
        assert inst.is_instrumented_by_opentelemetry is (not refused)
        assert result is warned
        msgs = _helper_warnings(caplog)
        if warned:
            assert len(msgs) == 1
            msg = msgs[0]
            assert "SQLAlchemyInstrumentor" in msg and "NOT instrumented" in msg
            assert f"pytest {PYTEST}" in msg and UNSUPPORTED in msg
            assert "opentelemetry-instrumentation " + version("opentelemetry-instrumentation") in msg
        else:
            assert msgs == []

    def test_warned_once_per_process(self, caplog, fresh):
        inst = fresh([UNSUPPORTED])()
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                inst.instrument()
                warn_if_unsupported(inst)
        assert len(_helper_warnings(caplog)) == 1

    def test_unreadable_dependencies_never_break(self):
        class Broken:
            is_instrumented_by_opentelemetry = False

            def instrumentation_dependencies(self):
                raise RuntimeError("boom")

        assert unsupported_dependency(Broken()) is None
        assert warn_if_unsupported(Broken()) is False


def _fake_module(monkeypatch, module_name: str, cls: type) -> None:
    module = ModuleType(module_name)
    setattr(module, cls.__name__, cls)
    monkeypatch.setitem(sys.modules, module_name, module)


class TestExtEntryPoints:
    @pytest.mark.parametrize("ext, module_name, class_name", [
        ("sql", "opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor"),
        ("redis", "opentelemetry.instrumentation.redis", "RedisInstrumentor"),
        ("aws", "opentelemetry.instrumentation.botocore", "BotocoreInstrumentor"),
    ])
    @pytest.mark.parametrize("deps, expected", [([UNSUPPORTED], False), ([SUPPORTED], True)],
                             ids=["unsupported", "supported"])
    def test_returns_status_and_warns(self, monkeypatch, caplog, fresh, ext, module_name, class_name,
                                      deps, expected):
        """Failed before A10: instrument_*() returned None with no helper warning."""
        _fake_module(monkeypatch, module_name, fresh(deps, class_name))
        func = getattr(__import__(f"otel_helper.ext.{ext}", fromlist=["x"]), f"instrument_{ext}")
        with caplog.at_level(logging.WARNING):
            assert func() is expected
        assert bool(_helper_warnings(caplog)) is (not expected)

    def test_sql_engine_path(self, monkeypatch, fresh):
        cls = fresh([SUPPORTED])
        _fake_module(monkeypatch, "opentelemetry.instrumentation.sqlalchemy", cls)
        from otel_helper.ext.sql import instrument_sql

        assert instrument_sql(engine=object()) is True


class TestConfigureInstrumentations:
    @staticmethod
    def _opts(caplog) -> TelemetryOptions:
        opts = TelemetryOptions(service_name="svc", environment="DEV")
        opts.resolve_from_env()
        caplog.clear()  # only what configure_instrumentations() logs is asserted
        return opts

    def _only(self, monkeypatch, cls, flag):
        _fake_module(monkeypatch, "otel_helper_test_fake_instr", cls)
        monkeypatch.setattr(instrumentation, "_INSTRUMENTORS",
                            (("otel_helper_test_fake_instr", cls.__name__, flag),))

    def test_refused_instrumentor_warned_during_setup(self, monkeypatch, caplog, fresh):
        self._only(monkeypatch, fresh([UNSUPPORTED]), "SQL")
        opts = self._opts(caplog)
        with caplog.at_level(logging.WARNING):
            assert instrumentation.configure_instrumentations(opts) == []
        msgs = _helper_warnings(caplog)
        assert len(msgs) == 1 and "NOT instrumented" in msgs[0]

    def test_implicit_sql_default_warned(self, monkeypatch, caplog, fresh):
        """Default extra_instrumentation='SQL' turns SQLAlchemy on globally: kept (spec silent), warned."""
        self._only(monkeypatch, fresh([SUPPORTED]), "SQL")
        opts = self._opts(caplog)
        with caplog.at_level(logging.WARNING):
            done = instrumentation.configure_instrumentations(opts)
            turned_on = len(done)
            instrumentation.uninstrument(done)  # clears the list
            instrumentation.configure_instrumentations(opts)
        msgs = _helper_warnings(caplog)
        assert turned_on == 1
        assert len(msgs) == 1
        assert "implicitly" in msgs[0] and "otel_helper.ext.sql.instrument_sql()" in msgs[0]

    def test_no_implicit_warning_when_env_var_set(self, monkeypatch, caplog, fresh):
        """The variable's own deprecation warning covers it; no second message."""
        import os

        os.environ["OTEL_HELPER_EXTRA_INSTRUMENTATION"] = "SQL"
        self._only(monkeypatch, fresh([SUPPORTED]), "SQL")
        opts = self._opts(caplog)
        with caplog.at_level(logging.WARNING):
            instrumentation.configure_instrumentations(opts)
        assert _helper_warnings(caplog) == []

    def test_explicit_ext_before_setup_leaves_nothing_implicit(self, monkeypatch, caplog, fresh):
        cls = fresh([SUPPORTED])
        self._only(monkeypatch, cls, "SQL")
        cls().instrument()  # what otel_helper.ext.sql.instrument_sql() does
        opts = self._opts(caplog)
        with caplog.at_level(logging.WARNING):
            assert instrumentation.configure_instrumentations(opts) == []
        assert _helper_warnings(caplog) == []

    def test_always_on_instrumentor_not_reported_as_implicit(self, monkeypatch, caplog, fresh):
        self._only(monkeypatch, fresh([SUPPORTED], "FastAPIInstrumentor"), None)
        opts = self._opts(caplog)
        with caplog.at_level(logging.WARNING):
            done = instrumentation.configure_instrumentations(opts)
        assert len(done) == 1 and _helper_warnings(caplog) == []
