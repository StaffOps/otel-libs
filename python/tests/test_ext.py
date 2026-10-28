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
