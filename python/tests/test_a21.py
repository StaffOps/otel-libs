"""Contract task A21 (design D1 rows of 2026-11-02 "Disabled-metric patterns", "Unit 1",
"Metrics port grammar", "Metrics listener"). The parity fixture covers the scrape and the
listener end to end (metrics.disabled_metrics_*, metrics.scrape_gauge_unit_one_ratio,
metrics.port_env_*, metrics.listener_*)."""

import errno
import http.client
import socket

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource

from staffops_otel_helper import metrics as helper_metrics
from staffops_otel_helper.config import TelemetryOptions
from staffops_otel_helper.metrics import _build_drop_views, _ratio_units, _view_pattern, configure_metrics


def _resolved(**kwargs) -> TelemetryOptions:
    opts = TelemetryOptions(**kwargs)
    opts.resolve_from_env()
    return opts


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("pattern, name, dropped", [
    ("parity.*", "parity.requests", True),
    ("par*load", "parity.payload", True),
    ("*", "a.b/c", True),
    ("parity.r?quests", "parity.requests", True),
    ("parity.payloa??", "parity.payload", False),
    ("parity.[pr]*", "parity.payload", False),
    ("PARITY.*", "parity.requests", False),
    ("a*b*c", "axxbyyc", True),
])
def test_disabled_metric_glob(pattern, name, dropped):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], views=_build_drop_views([pattern]))
    provider.get_meter("parity").create_counter(name).add(1)
    data = reader.get_metrics_data()
    names = [m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics] if data else []
    assert (name not in names) is dropped
    provider.shutdown()


def test_view_pattern_escapes_brackets_and_skips_upper_case():
    assert _view_pattern("a[b]*") == "a[[]b]*"
    assert _view_pattern("Upper.*") is None


def test_disabled_metrics_items_trimmed_blanks_dropped(monkeypatch):
    monkeypatch.setenv("OTEL_HELPER_DISABLED_METRICS", " a.* ,, \t")
    assert _resolved().resolved.disabled_metrics == ["a.*"]
    assert _resolved(disabled_metrics=[" b ", ""]).resolved.disabled_metrics == ["b"]
    assert _resolved(disabled_metrics=[]).resolved.disabled_metrics == []


def test_ratio_units_only_gauges_in_unit_one():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("parity")
    meter.create_gauge("g1", unit="1").set(0.5)
    meter.create_gauge("g2", unit="{x}1").set(0.5)
    meter.create_gauge("g3", unit="By").set(1)
    meter.create_up_down_counter("u1", unit="1").add(1)
    meter.create_counter("c1", unit="1").add(1)
    units = {m.name: m.unit for rm in _ratio_units(reader.get_metrics_data()).resource_metrics
             for sm in rm.scope_metrics for m in sm.metrics}
    assert units == {"g1": "ratio", "g2": "ratio", "g3": "By", "u1": "1", "c1": "1"}
    assert _ratio_units(None) is None
    provider.shutdown()


@pytest.mark.parametrize("raw, port, warned", [
    ("9100", 9100, False), ("+9100", 9100, False), ("+0", 0, False), ("0" * 40 + "9100", 9100, False),
    ("9_100", 9464, True), ("-0", 9464, True), ("65536", 9464, True), ("1" * 40, 9464, True),
    ("٩١٠٠", 9464, True), ("++1", 9464, True), ("0x10", 9464, True),
])
def test_metrics_port_grammar(monkeypatch, caplog, raw, port, warned):
    monkeypatch.setenv("OTEL_HELPER_METRICS_PORT", raw)
    assert _resolved().resolved.prometheus_metrics_port == port
    assert ("is not a port" in caplog.text) is warned


def _request(port: int, method: str, path: str, host: str = "127.0.0.1") -> tuple[int, bytes, str | None]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        return resp.status, resp.read(), resp.getheader("Allow")
    finally:
        conn.close()


def test_listener_get_head_metrics_only():
    port = _free_port()
    provider = configure_metrics(Resource.create({}), _resolved(
        service_name="a21", metric_exporters=["prometheus"], prometheus_metrics_port=port))
    try:
        provider.get_meter("parity").create_gauge("parity.utilization", unit="1").set(0.5)
        status, body, _ = _request(port, "GET", "/metrics?x=1")
        assert status == 200 and b"parity_utilization_ratio" in body
        assert _request(port, "HEAD", "/metrics")[:2] == (200, b"")
        status, _, allow = _request(port, "POST", "/metrics")
        assert (status, allow) == (405, "GET, HEAD")
        assert _request(port, "GET", "/other")[0] == 404
        assert _request(port, "HEAD", "/other")[:2] == (404, b"")
    finally:
        provider.shutdown()


def test_listener_dual_stack_or_ipv4_fallback(monkeypatch):
    port = _free_port()
    server, thread = helper_metrics._start_listener(port)
    try:
        assert server.server_address[1] == port
        assert _request(port, "GET", "/other")[0] == 404  # IPv4 reaches it either way
    finally:
        helper_metrics.stop_listener((server, thread))
    # Without IPv6 support the listener binds IPv4 only.
    monkeypatch.setattr(socket, "has_ipv6", False)
    server, thread = helper_metrics._start_listener(port)
    try:
        assert server.address_family == socket.AF_INET
    finally:
        helper_metrics.stop_listener((server, thread))


def test_listener_ipv6_bind_failure_falls_back_but_port_in_use_raises(monkeypatch):
    class Unavailable(helper_metrics._DualStackServer):
        def server_bind(self):
            raise OSError(errno.EADDRNOTAVAIL, "no IPv6")

    class Busy(helper_metrics._DualStackServer):
        def server_bind(self):
            raise OSError(errno.EADDRINUSE, "in use")

    monkeypatch.setattr(socket, "has_ipv6", True)
    monkeypatch.setattr(helper_metrics, "_DualStackServer", Unavailable)
    server, thread = helper_metrics._start_listener(_free_port())
    try:
        assert server.address_family == socket.AF_INET
    finally:
        helper_metrics.stop_listener((server, thread))
    monkeypatch.setattr(helper_metrics, "_DualStackServer", Busy)
    with pytest.raises(OSError):
        helper_metrics._start_listener(_free_port())


# ── B7/A21 fix round ─────────────────────────────────────────────────────────

def _raw(port: int, data: bytes, timeout: float = 5.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(data)
        chunks = []
        while True:
            try:
                chunk = s.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def _listener(port):
    return configure_metrics(Resource.create({}), _resolved(
        service_name="a21", metric_exporters=["prometheus"], prometheus_metrics_port=port))


def test_listener_every_method_goes_through_serve():
    port = _free_port()
    provider = _listener(port)
    try:
        for method in ("TRACE", "PROPFIND", "DELETE"):
            status, body, allow = _request(port, method, "/metrics")
            assert (status, body, allow) == (405, b"Method Not Allowed\n", "GET, HEAD"), method
            assert _request(port, method, "/other")[:2] == (404, b"404 page not found\n"), method
        # BaseHTTPRequestHandler collapses leading slashes: 200 (Go: 307, Node: 404; D1 row "Metrics listener").
        assert _request(port, "GET", "//metrics")[0] == 200
        # Absolute-form request target.
        assert _raw(port, b"GET http://x/metrics HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").startswith(b"HTTP/1.0 200")
        assert _raw(port, b"GET http://x/other HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").startswith(b"HTTP/1.0 404")
    finally:
        provider.shutdown()


def test_listener_closes_a_half_sent_request_and_leaves_no_thread(monkeypatch):
    import threading
    import time

    assert helper_metrics._ListenerHandler.timeout == 10
    monkeypatch.setattr(helper_metrics._ListenerHandler, "timeout", 1)
    port = _free_port()
    server, thread = helper_metrics._start_listener(port)
    try:
        started = time.monotonic()
        assert _raw(port, b"GET /metrics HTTP/1.1\r\nHost: x\r\n", timeout=5) == b""  # closed, nothing sent
        assert time.monotonic() - started < 4
    finally:
        helper_metrics.stop_listener((server, thread))
    time.sleep(1.5)
    assert not [t for t in threading.enumerate() if "process_request_thread" in t.name]


def test_listener_collection_failure_is_500_and_logged(monkeypatch, caplog):
    import prometheus_client

    def boom(registry):
        raise RuntimeError("collector boom")

    monkeypatch.setattr(prometheus_client, "generate_latest", boom)
    port = _free_port()
    server, thread = helper_metrics._start_listener(port)
    try:
        with caplog.at_level("WARNING", logger="staffops_otel_helper"):
            assert _request(port, "GET", "/metrics")[:2] == (500, b"metric collection failed\n")
        assert "/metrics collection failed: collector boom" in caplog.text
    finally:
        helper_metrics.stop_listener((server, thread))


def test_listener_handle_error_logs_without_traceback(caplog, capsys):
    server = helper_metrics._IPv4Server(("127.0.0.1", 0), helper_metrics._ListenerHandler)
    try:
        with caplog.at_level("WARNING", logger="staffops_otel_helper"):
            try:
                raise ValueError("handler machinery boom")
            except ValueError:
                server.handle_error(None, ("127.0.0.1", 1))
        assert "listener request failed: handler machinery boom" in caplog.text
        assert "Traceback" not in capsys.readouterr().err
    finally:
        server.server_close()


def test_foreign_meter_provider_starts_no_listener():
    import subprocess
    import sys

    port = _free_port()
    script = f"""
import socket
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
metrics.set_meter_provider(MeterProvider())
from staffops_otel_helper import setup_telemetry
from staffops_otel_helper.config import TelemetryOptions
setup_telemetry(TelemetryOptions(service_name="a21", metric_exporters=["prometheus"], prometheus_metrics_port={port}))
s = socket.socket()
print("refused" if s.connect_ex(("127.0.0.1", {port})) != 0 else "listening")
"""
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "refused", out.stdout + out.stderr


def test_colliding_label_names_merged_in_original_key_order():
    from staffops_otel_helper.metrics import _merged

    assert _merged({"a_b": "x", "a.b": "y", "n": 3}) == {"a_b": "x;y", "n": 3}  # values sorted, as Go
    assert _merged({"a.b": 5, "a_b": True, "a-b": ["a", "b"]}) == {"a_b": '5;["a","b"];true'}
    assert _merged({"a.b": (1, 2), "a_b": 1.5}) == {"a_b": "1.5;[1,2]"}
    plain = {"k": "v"}
    assert _merged(plain) is plain and _merged({}) == {}
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], resource=Resource.create({"x.y": "2", "x_y": "1"}))
    provider.get_meter("parity").create_counter("c").add(1, {"a.b": "y", "a_b": "x"})
    data = _ratio_units(reader.get_metrics_data())
    rm = data.resource_metrics[0]
    assert rm.resource.attributes["x_y"] == "1;2"
    assert dict(rm.scope_metrics[0].metrics[0].data.data_points[0].attributes) == {"a_b": "x;y"}
    provider.shutdown()


def test_upper_case_pattern_warns_once_per_pattern_naming_it_truncated(caplog):
    long_pattern = "Orders." + "x" * 100
    with caplog.at_level("WARNING", logger="staffops_otel_helper"):
        assert _view_pattern("Orders.*") is None
        assert _view_pattern("Orders.*") is None
        assert _view_pattern(long_pattern) is None
    hits = [r.getMessage() for r in caplog.records if "upper-case" in r.getMessage()]
    assert len(hits) == 2
    assert "OTEL_HELPER_DISABLED_METRICS" in hits[0] and "'Orders.*'" in hits[0]
    assert long_pattern not in hits[1] and "Orders.xxx" in hits[1]


def test_ratio_units_returns_unchanged_objects_without_a_collision():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    provider.get_meter("parity").create_counter("c").add(1, {"k": "v"})
    data = reader.get_metrics_data()
    assert _ratio_units(data) is data
    provider.get_meter("parity").create_counter("d").add(1, {"a.b": "x", "a_b": "y"})
    data = reader.get_metrics_data()
    adapted = _ratio_units(data)
    assert adapted is not data
    by_name = {m.name: m for m in adapted.resource_metrics[0].scope_metrics[0].metrics}
    original = {m.name: m for m in data.resource_metrics[0].scope_metrics[0].metrics}
    assert by_name["c"] is original["c"]
    assert adapted.resource_metrics[0].resource is data.resource_metrics[0].resource
    provider.shutdown()


def test_foreign_meter_provider_with_a_busy_port_does_not_fail_setup():
    import subprocess
    import sys

    script = """
import socket
blocker = socket.socket()
blocker.bind(("0.0.0.0", 0))
blocker.listen()
port = blocker.getsockname()[1]
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
metrics.set_meter_provider(MeterProvider())
from staffops_otel_helper import setup_telemetry
from staffops_otel_helper.config import TelemetryOptions
opts = setup_telemetry(TelemetryOptions(service_name="a21", metric_exporters=["prometheus"], prometheus_metrics_port=port))
print("ok", opts.is_signal_enabled("metrics"))
"""
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "ok False"


def test_collection_failure_log_is_truncated(monkeypatch, caplog):
    import prometheus_client

    def boom(registry):
        raise RuntimeError("y" * 500)

    monkeypatch.setattr(prometheus_client, "generate_latest", boom)
    port = _free_port()
    server, thread = helper_metrics._start_listener(port)
    try:
        with caplog.at_level("WARNING", logger="staffops_otel_helper"):
            assert _request(port, "GET", "/metrics")[0] == 500
        assert "y" * 500 not in caplog.text and "yyyy" in caplog.text
    finally:
        helper_metrics.stop_listener((server, thread))
