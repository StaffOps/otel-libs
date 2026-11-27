"""democommon: environment parsing, the scenario cycle and the ADR-014 failure pattern."""

from __future__ import annotations

import json
import logging
from collections import Counter

import pytest
import staffops_otel_helper
from opentelemetry.trace import StatusCode

import democommon as dc


@pytest.mark.parametrize("value, expected", [
    ("deployment.environment.name=dev,otel_libs.run=20261116t1812", "20261116t1812"),
    ("otel_libs.run=a%2Cb , x=y", "a,b"),
    (" otel_libs.run = r1 ", "r1"),
    ("otel_libs.run=", "local"),
    ("deployment.environment.name=dev", "local"),
    ("", "local"),
    ("broken,otel_libs.run=r2", "r2"),
])
def test_run_id(value, expected):
    assert dc.run_id({"OTEL_RESOURCE_ATTRIBUTES": value}) == expected


def test_run_id_unset():
    assert dc.run_id({}) == "local"


def test_read_secret_file_strips_one_newline(tmp_path):
    f = tmp_path / "pw"
    f.write_text("s3cr3t\n")
    assert dc.read_secret_file(str(f), "DEMO_X_FILE") == "s3cr3t"


def test_read_secret_file_errors_name_the_variable_not_the_content(tmp_path):
    with pytest.raises(RuntimeError, match="DEMO_X_FILE is not set"):
        dc.read_secret_file(None, "DEMO_X_FILE")
    with pytest.raises(RuntimeError, match="DEMO_X_FILE: FileNotFoundError"):
        dc.read_secret_file(str(tmp_path / "missing"), "DEMO_X_FILE")
    empty = tmp_path / "empty"
    empty.write_text("\n")
    with pytest.raises(RuntimeError, match="is empty"):
        dc.read_secret_file(str(empty), "DEMO_X_FILE")


@pytest.mark.parametrize("addr, expected", [
    ("demo-redis:6379", ("demo-redis", 6379)),
    ("demo-redis", ("demo-redis", 1)),
    ("[::1]:7000", ("::1", 7000)),
    ("[::1]", ("::1", 1)),
    ("::1", ("::1", 1)),
])
def test_split_host_port(addr, expected):
    assert dc.split_host_port(addr, 1) == expected


def test_split_host_port_rejects_empty_host():
    with pytest.raises(ValueError):
        dc.split_host_port(":80", 1)


def test_env_numbers():
    assert dc.env_int("X", 7, {}) == 7
    assert dc.env_int("X", 7, {"X": " 9 "}) == 9
    assert dc.env_float("X", 2.0, {"X": ""}) == 2.0
    assert dc.env_float("X", 2.0, {"X": "0.5"}) == 0.5


def test_env_numbers_fall_back_with_one_warning(caplog, monkeypatch):
    monkeypatch.setattr(dc, "_warned", set())
    with caplog.at_level(logging.WARNING, logger="demo.config"):
        assert dc.env_float("DEMO_POLL_SECONDS", 2.0, {"DEMO_POLL_SECONDS": "two"}) == 2.0
        assert dc.env_float("DEMO_POLL_SECONDS", 2.0, {"DEMO_POLL_SECONDS": "-1"}) == 2.0
        assert dc.env_int("DEMO_HTTP_PORT", 8080, {"DEMO_HTTP_PORT": "0"}) == 8080
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs == ["invalid value for DEMO_POLL_SECONDS, using the default 2.0",
                    "invalid value for DEMO_HTTP_PORT, using the default 8080"]
    assert not any("two" in m for m in msgs)


def test_cycle_matches_de_d7():
    # seq starts at 1 (r3.5); positions 1..10: ok at 1,2,4,5,7,8,10; business_error 3; db_error 6; unhandled 9.
    scenarios = [dc.order_for(i).scenario for i in range(1, 11)]
    assert scenarios == ["ok", "ok", "business_error", "ok", "ok", "db_error", "ok", "ok", "unhandled", "ok"]
    counts = Counter(dc.order_for(i).scenario for i in range(1, 181))  # one 15-minute window
    assert counts == {"ok": 126, "business_error": 18, "db_error": 18, "unhandled": 18}


def test_orders_ids_amounts_and_customers():
    first = dc.order_for(1)
    assert first.to_json() == {"order_id": "ok-1", "customer": "cust-a", "amount": 11.0, "scenario": "ok"}
    assert [dc.order_for(i).customer for i in (1, 2, 3, 4)] == ["cust-a", "cust-b", "cust-c", "cust-a"]
    rejected = dc.order_for(3)
    assert rejected.order_id == "ord-3" and rejected.amount == 5000.0 > dc.AMOUNT_LIMIT
    db = dc.order_for(506)
    assert db.scenario == "db_error" and db.order_id == "ord-6" and db.amount == 10.0 + 506 % 90
    assert dc.order_for(1001).order_id == "ok-1"  # seq mod 500 (DE-D7 r3.4)
    others = [dc.order_for(i) for i in range(1, 500) if dc.order_for(i).scenario != "business_error"]
    assert all(10 <= o.amount < 100 for o in others)  # 10 + seq mod 90
    # No id the worker sends is a run token (DR12): tokens are built where the error is raised.
    assert not any(dc.order_for(i).order_id.startswith(("rej-", "dup-", "unh-")) for i in range(1, 1000))


@pytest.mark.parametrize("exc", [dc.OrderRejectedError("amount over limit rej-r1"),
                                 dc.OrderExistsError("order dup-r1 exists"),
                                 dc.DemoUnhandledError("boom unh-r1")])
def test_traced_uses_the_pinned_literals(otel, exc):
    """The demo's wiring of the A45 API (D15): the helper reads __otel_error_type__."""
    literal = type(exc).__name__
    assert dc.error_type(exc) == literal
    with pytest.raises(type(exc)):
        with staffops_otel_helper.traced("demo.order.process", tracer=otel.tracer):
            raise exc
    (s,) = otel.finished("demo.order.process")
    assert s.status.status_code is StatusCode.ERROR
    assert s.status.description is None
    assert s.attributes["error.type"] == literal
    assert not s.events
    assert not any("-r1" in str(v) for v in s.attributes.values())
    (rec,) = otel.records()
    assert rec.body == str(exc)
    assert rec.severity_number.value >= 17
    assert rec.trace_id == s.context.trace_id and rec.span_id == s.context.span_id
    assert rec.attributes["exception.type"] == literal
    assert rec.attributes["exception.message"] == str(exc)
    assert "Traceback" in rec.attributes["exception.stacktrace"]


@pytest.mark.parametrize("exc", [RuntimeError("x"), TimeoutError(), KeyError("k"),
                                 dc.OrderRejectedError("m"), dc.OrderExistsError("m"),
                                 dc.DemoUnhandledError("m"), json.JSONDecodeError("m", "d", 0)])
def test_error_type_equals_the_helper(exc):
    """The metric value (dc.error_type) is the helper's span value for the same exception."""
    from staffops_otel_helper import _failure

    assert dc.error_type(exc) == _failure.error_type(exc)


def test_error_type_mirrors_the_helper_default():
    assert dc.error_type(RuntimeError("x")) == "RuntimeError"

    class Local(Exception):
        __otel_error_type__ = ""

    assert dc.error_type(Local()) == f"{__name__}.test_error_type_mirrors_the_helper_default.<locals>.Local"
    assert dc.error_type(dc.OrderRejectedError("x")) == "OrderRejectedError"
    assert dc.error_type(TimeoutError()) == "TimeoutError"

