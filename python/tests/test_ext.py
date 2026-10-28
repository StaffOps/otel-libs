"""Tests for opt-in instrumentation extensions (otel_helper.ext).

Both paths are simulated through sys.modules (the test env may or may not have
the libraries — SQLAlchemy is a test dependency since A10): ``None`` makes the
import fail; a fake module stands in for an installed instrumentor.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from otel_helper.ext.aws import instrument_aws
from otel_helper.ext.redis import instrument_redis
from otel_helper.ext.sql import instrument_sql


_REDIS_MOD = "opentelemetry.instrumentation.redis"


def _fake_instrumentor_module(monkeypatch, module_name: str, class_name: str) -> MagicMock:
    """Register a fake opentelemetry.instrumentation.<lib> module exposing a mock instrumentor."""
    instrumentor_cls = MagicMock(name=class_name)
    module = ModuleType(module_name)
    setattr(module, class_name, instrumentor_cls)
    monkeypatch.setitem(sys.modules, module_name, module)
    return instrumentor_cls


class TestExtraNotInstalled:
    def test_aws_raises_actionable_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.botocore", None)  # import fails
        with pytest.raises(ImportError, match=r"pip install otel-helper\[aws\]"):
            instrument_aws()

    def test_redis_raises_actionable_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.redis", None)  # import fails
        with pytest.raises(ImportError, match=r"pip install otel-helper\[redis\]"):
            instrument_redis()

    def test_sql_raises_actionable_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.sqlalchemy", None)  # import fails
        with pytest.raises(ImportError, match=r"pip install otel-helper\[sql\]"):
            instrument_sql()


class TestLibraryNotInstalled:
    """The extra is installed but the instrumented library is not (A13)."""

    def test_sql_names_the_missing_library_not_the_extra(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "opentelemetry.instrumentation.sqlalchemy":
                raise ModuleNotFoundError("No module named 'sqlalchemy'", name="sqlalchemy")
            return real_import(name, *args, **kwargs)

        monkeypatch.delitem(sys.modules, "opentelemetry.instrumentation.sqlalchemy", raising=False)
        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ImportError) as exc:
            instrument_sql()
        assert "'sqlalchemy' is not" in str(exc.value)
        assert "pip install sqlalchemy" in str(exc.value)

    @pytest.mark.parametrize("name", ["opentelemetry", "opentelemetry.instrumentation",
                                      "opentelemetry.instrumentation.redis"])
    def test_hint_points_to_extra_when_instrumentor_missing(self, name):
        from otel_helper._support import missing_dependency_hint

        msg = missing_dependency_hint(ModuleNotFoundError("x", name=name), "redis", "Redis", library="redis",
                                      module=_REDIS_MOD)
        assert msg == "Redis instrumentation not installed. Run: pip install otel-helper[redis]"

    def test_hint_uses_top_level_package(self):
        from otel_helper._support import missing_dependency_hint

        msg = missing_dependency_hint(ModuleNotFoundError("x", name="botocore.session"), "aws", "AWS",
                                      library="botocore", module="opentelemetry.instrumentation.botocore")
        assert "'botocore' is not" in msg and "otel-helper[aws] does not pull it in" in msg


class TestImportFailureNotMisreported:
    """D14 A13 ruling: only a ModuleNotFoundError for the expected module names a cause."""

    def test_version_skew_is_not_reported_as_missing_library(self, monkeypatch):
        import builtins

        real_import = builtins.__import__
        skew = ImportError("cannot import name 'X' from 'sqlalchemy'", name="sqlalchemy")

        def fake_import(name, *args, **kwargs):
            if name == "opentelemetry.instrumentation.sqlalchemy":
                raise skew
            return real_import(name, *args, **kwargs)

        monkeypatch.delitem(sys.modules, "opentelemetry.instrumentation.sqlalchemy", raising=False)
        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ImportError) as exc:
            instrument_sql()
        msg = str(exc.value)
        assert msg == "failed to import the SQL instrumentation: cannot import name 'X' from 'sqlalchemy'"
        assert "not installed" not in msg and "is not." not in msg
        assert exc.value.__cause__ is skew

    @pytest.mark.parametrize("extra,label,library", [("sql", "SQL", "sqlalchemy"), ("redis", "Redis", "redis"),
                                                     ("aws", "AWS", "botocore")])
    def test_missing_transitive_dependency_is_generic(self, extra, label, library):
        from otel_helper._support import missing_dependency_hint

        err = ModuleNotFoundError("No module named 'wrapt'", name="wrapt")
        msg = missing_dependency_hint(err, extra, label, library=library,
                                      module=f"opentelemetry.instrumentation.{extra}")
        assert msg == f"failed to import the {label} instrumentation: No module named 'wrapt'"

    @pytest.mark.parametrize("name", [None, "", "opentelemetry.semconv._incubating.attributes",
                                      "opentelemetry.instrumentation.redis.util",
                                      "opentelemetry.instrumentation.sqlalchemy"])
    def test_other_otel_modules_and_no_name_are_generic(self, name):
        """Version skew inside the OTel namespace is not "extra not installed" (D14, review M2)."""
        from otel_helper._support import missing_dependency_hint

        err = ModuleNotFoundError("No module named 'x'", name=name)
        msg = missing_dependency_hint(err, "redis", "Redis", library="redis", module=_REDIS_MOD)
        assert msg == "failed to import the Redis instrumentation: No module named 'x'"

    def test_plain_import_error_for_instrumentor_is_generic(self):
        from otel_helper._support import missing_dependency_hint

        err = ImportError("cannot import name 'RedisInstrumentor'", name="opentelemetry.instrumentation.redis")
        msg = missing_dependency_hint(err, "redis", "Redis", library="redis", module=_REDIS_MOD)
        assert msg.startswith("failed to import the Redis instrumentation: ")

    def test_other_library_module_not_found_is_generic(self):
        from otel_helper._support import missing_dependency_hint

        msg = missing_dependency_hint(ModuleNotFoundError("x", name="redis"), "sql", "SQL", library="sqlalchemy",
                                      module="opentelemetry.instrumentation.sqlalchemy")
        assert msg == "failed to import the SQL instrumentation: x"

    @pytest.mark.parametrize("entry,module,library", [
        (instrument_aws, "opentelemetry.instrumentation.botocore", "botocore"),
        (instrument_redis, "opentelemetry.instrumentation.redis", "redis"),
        (instrument_sql, "opentelemetry.instrumentation.sqlalchemy", "sqlalchemy"),
    ])
    def test_entry_points_pass_their_library(self, monkeypatch, entry, module, library):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == module:
                raise ModuleNotFoundError(f"No module named '{library}'", name=library)
            return real_import(name, *args, **kwargs)

        monkeypatch.delitem(sys.modules, module, raising=False)
        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ImportError, match=f"library '{library}' is not"):
            entry()


class TestExtraInstalled:
    def test_aws_instruments(self, monkeypatch):
        cls = _fake_instrumentor_module(
            monkeypatch, "opentelemetry.instrumentation.botocore", "BotocoreInstrumentor")
        instrument_aws()
        cls.return_value.instrument.assert_called_once_with()

    def test_redis_instruments(self, monkeypatch):
        cls = _fake_instrumentor_module(
            monkeypatch, "opentelemetry.instrumentation.redis", "RedisInstrumentor")
        instrument_redis()
        cls.return_value.instrument.assert_called_once_with()

    def test_sql_instruments_globally(self, monkeypatch):
        cls = _fake_instrumentor_module(
            monkeypatch, "opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor")
        instrument_sql()
        cls.return_value.instrument.assert_called_once_with()

    def test_sql_instruments_specific_engine(self, monkeypatch):
        cls = _fake_instrumentor_module(
            monkeypatch, "opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor")
        engine = object()
        instrument_sql(engine=engine)
        cls.return_value.instrument.assert_called_once_with(engine=engine)
