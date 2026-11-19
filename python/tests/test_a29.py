"""Contract task A29 (design D1 rows of 2026-11-05 "SDK error text: response bodies and header
values", "Path on a gRPC endpoint", "Prometheus exporter variables"). The parity fixture covers
the wire side (export.http_error_body_header_values_not_logged,
export.grpc_error_details_header_values_masked, export.grpc_*path*, metrics.port_prometheus_*,
metrics.listener_prometheus_host_loopback)."""

from __future__ import annotations

import errno
import http.client
import logging
import socket
import sys

import pytest
from opentelemetry.sdk.resources import Resource

from staffops_otel_helper import _sdk_log_mask as mask
from staffops_otel_helper import metrics as helper_metrics
from staffops_otel_helper.config import TelemetryOptions, _parse_port, _parse_prometheus_host
from staffops_otel_helper.metrics import configure_metrics

GRPC_LOGGER = "opentelemetry.exporter.otlp.proto.grpc.exporter"
HTTP_LOGGER = "opentelemetry.exporter.otlp.proto.http.trace_exporter"


def _resolved(**kwargs) -> TelemetryOptions:
    opts = TelemetryOptions(**kwargs)
    opts.resolve_from_env()
    return opts


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── SDK error text: header values masked ─────────────────────────────────────────────


def test_configured_header_values(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "api-key=s3cr3t%20value, x-short=1,bad")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_HEADERS", "x-logs=logs-t0ken")
    values = mask.configured_header_values()
    assert {"s3cr3t value", "s3cr3t%20value", "1", "logs-t0ken"} <= set(values)
    assert mask.configured_header_values() is values  # memoized on the raw values
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "k=other-secret")
    assert "other-secret" in mask.configured_header_values()
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_HEADERS", "   ")
    assert mask.configured_header_values() == ()


@pytest.mark.parametrize("text", [
    "raw s3cr3t%20value here",
    "decoded s3cr3t value here",
    'json {"q": "a\\"b\\\\c-1"}',
    'twice {\\"q\\": \\"a\\\\\\"b\\\\\\\\c-1\\"}',
    "repr {'q': 'a\"b\\\\c-1'}",
    "unicode café-token",
    "form Bearer+s3cr3t+value",
    "token only s3cr3t here",
])
def test_mask_header_values_every_form(text):
    values = ("s3cr3t value", "s3cr3t%20value", 'a"b\\c-1', "café-token", "1", "on")
    out = mask.mask_header_values(text, values)
    assert mask.REDACTED in out
    for secret in ("s3cr3t", 'a"b', 'a\\"b', "café"):
        assert secret not in out, out


def test_mask_skips_short_values():
    assert mask.mask_header_values("code 1 on", ("1", "on", "abc")) == "code 1 on"
    assert mask.mask_header_values("abc", ("abc def1",)) == "abc"  # a part under 4 characters


def test_mask_forms_parts_html_form_and_overlap():
    values = ("Bearer tok-s3cr3t-1234", "team", "team-secret-token", "a&b<c>-amp-value")
    for text in ("invalid token tok-s3cr3t-1234", "form Bearer+tok-s3cr3t-1234", "tenant team-secret-token",
                 'html "a\\u0026b\\u003cc\\u003e-amp-value"', "raw a&b<c>-amp-value"):
        out = mask.mask_header_values(text, values)
        assert not any(s in out for s in ("s3cr3t", "secret-token", "amp-value")), out
    assert mask.mask_header_values("tenant team-secret-token", values) == "tenant <redacted>"
    # One pass: "<redacted>" is never scanned again.
    assert mask.mask_header_values("x dact y", ("dact",)) == "x <redacted> y"
    forms = mask.mask_forms(("Bearer tok-s3cr3t-1234",))
    assert forms == sorted(forms, key=lambda f: (-len(f), f)) and "tok-s3cr3t-1234" in forms
    assert "Bearer+tok-s3cr3t-1234" in forms and "Bearer" not in forms  # an auth-scheme name is not a part
    assert mask.mask_header_values("scheme Bearer, token tok-s3cr3t-1234", values) == "scheme Bearer, token <redacted>"
    # Overlapping values never leave a tail (A29 final round, dev R2-L1).
    assert mask.mask_header_values("x OVERLAP-AAAA-BBBB-CCCC-PLACEHOLDER2 y",
                                   ("OVERLAP-AAAA-BBBB", "BBBB-CCCC-PLACEHOLDER2")) == "x <redacted> y"


def test_captured_values_stay_masked_after_the_env_changes(monkeypatch):
    """What the exporters were built with stays masked (A29 final round, dev R2-M1)."""
    from staffops_otel_helper import setup_telemetry

    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer%20tok-captured-s3cr3t")
    setup_telemetry(TelemetryOptions(service_name="a29"))
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_HEADERS", "x-new=new-v4lue")
    values = mask.configured_header_values()
    assert "Bearer tok-captured-s3cr3t" in values and "new-v4lue" in values
    record = logging.LogRecord(GRPC_LOGGER, logging.ERROR, __file__, 1, "details: %s",
                               ("invalid token tok-captured-s3cr3t new-v4lue",), None)
    mask.HeaderValueMask().filter(record)
    assert record.getMessage() == "details: invalid token <redacted> <redacted>"


def test_filter_masks_message_and_args(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=s3cr3t-value")
    record = logging.LogRecord(GRPC_LOGGER, logging.ERROR, __file__, 1,
                               "Failed to export %s to %s, error code: %s, error details: %s",
                               ("traces", "collector:4317", "StatusCode.INVALID_ARGUMENT",
                                'echo {"x-api-key": "s3cr3t-value"}'), None)
    assert mask.HeaderValueMask().filter(record) is True
    assert "s3cr3t" not in record.getMessage()
    assert '"x-api-key": "<redacted>"' in record.getMessage()


def test_filter_leaves_records_alone_without_headers():
    record = logging.LogRecord(HTTP_LOGGER, logging.ERROR, __file__, 1, "code: %s, reason: %s",
                               (400, "Bad Request"), None)
    assert mask.HeaderValueMask().filter(record) is True
    assert record.args == (400, "Bad Request")


def test_filter_masks_exception_text_and_drops_exc_info(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=s3cr3t-value")
    try:
        raise RuntimeError("details: s3cr3t-value")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(GRPC_LOGGER, logging.ERROR, __file__, 1, "failed", None, exc_info)
    record.stack_info = "Stack: s3cr3t-value"
    mask.HeaderValueMask().filter(record)
    assert record.exc_info is None and record.exc_text is None
    text = logging.Formatter().format(record)
    assert "s3cr3t" not in text and "RuntimeError: details: <redacted>" in text
    assert record.stack_info == "Stack: <redacted>"


def test_filter_keeps_an_exception_without_secrets(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=s3cr3t-value")
    try:
        raise RuntimeError("nothing to hide")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(GRPC_LOGGER, logging.ERROR, __file__, 1, "failed", None, exc_info)
    mask.HeaderValueMask().filter(record)
    assert record.exc_info is exc_info


def test_filter_never_raises(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=s3cr3t-value")
    record = logging.LogRecord(GRPC_LOGGER, logging.ERROR, __file__, 1, "%d", ("not a number",), None)
    assert mask.HeaderValueMask().filter(record) is True
    assert record.getMessage() == mask.UNRENDERABLE


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def test_setup_installs_the_mask_on_the_exporter_loggers(monkeypatch):
    from staffops_otel_helper import setup_telemetry, shutdown_telemetry

    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=s3cr3t-value")
    setup_telemetry(TelemetryOptions(service_name="a29"))
    shutdown_telemetry()
    setup_telemetry(TelemetryOptions(service_name="a29"))  # a second real install attempt
    mask.install_sdk_log_mask()  # and a direct one: still one filter per logger
    for name in mask.EXPORTER_LOGGERS:
        assert sum(isinstance(f, mask.HeaderValueMask) for f in logging.getLogger(name).filters) == 1
    handler = _ListHandler()
    logger = logging.getLogger(GRPC_LOGGER)
    logger.addHandler(handler)
    try:
        logger.error("error details: %s", "echo s3cr3t-value")
    finally:
        logger.removeHandler(handler)
    assert handler.lines == ["error details: echo <redacted>"]


# ── Path on a gRPC endpoint ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("env, warned", [
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4317/secret-path"}, ["OTEL_EXPORTER_OTLP_ENDPOINT"]),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "collector:4317/secret-path"}, ["OTEL_EXPORTER_OTLP_ENDPOINT"]),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4317",
      "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://collector:4317/v1/logs"}, ["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"]),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4317/"}, []),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318/secret-path"}, []),  # http/protobuf by port
])
def test_grpc_endpoint_path_warns_once_naming_the_variable(monkeypatch, caplog, env, warned):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
        opts = _resolved(service_name="a29")
    lines = [r.getMessage() for r in caplog.records if "endpoint path is ignored by the gRPC exporter" in r.getMessage()]
    assert [line.split(":")[0].removeprefix("[OtelHelper] ") for line in lines] == warned
    assert "secret-path" not in caplog.text and "/v1/logs" not in caplog.text
    for signal in ("traces", "metrics", "logs"):
        plan = opts.otlp_export(signal)
        if plan is not None and plan.protocol == "grpc":
            assert "secret-path" not in str(plan.kwargs.get("endpoint", ""))


def test_grpc_code_endpoint_path_warns_not_fails(caplog):
    with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
        opts = _resolved(service_name="a29", otel_endpoint="http://collector:4317/code-path")
    assert caplog.text.count("endpoint path is ignored by the gRPC exporter") == 1
    assert opts.otlp_export("traces").kwargs["endpoint"] == "http://collector:4317"


# ── Prometheus exporter variables ───────────────────────────────────────────────────


@pytest.mark.parametrize("prom, helper, code, want, warned", [
    (None, None, None, 9464, None),
    (None, "9100", None, 9100, None),
    ("9300", "9100", None, 9300, None),
    ("0", "9100", None, 0, None),
    ("+0009300", None, None, 9300, None),
    ("93_00", "9100", None, 9100, "OTEL_EXPORTER_PROMETHEUS_PORT"),
    ("70000", None, None, 9464, "OTEL_EXPORTER_PROMETHEUS_PORT"),
    ("x", "y", None, 9464, "OTEL_HELPER_METRICS_PORT"),
    ("  ", "9100", None, 9100, None),
    ("9300", "9100", 9200, 9200, None),
    ("9300", None, 0, 0, None),
])
def test_prometheus_port_precedence(monkeypatch, caplog, prom, helper, code, want, warned):
    if prom is not None:
        monkeypatch.setenv("OTEL_EXPORTER_PROMETHEUS_PORT", prom)
    if helper is not None:
        monkeypatch.setenv("OTEL_HELPER_METRICS_PORT", helper)
    with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
        opts = _resolved(service_name="a29", prometheus_metrics_port=code)
    assert opts.resolved.prometheus_metrics_port == want
    if warned:
        assert f"{warned}=" in caplog.text and "is not a port" in caplog.text
    else:
        assert "is not a port" not in caplog.text


@pytest.mark.parametrize("value, want", [
    ("9100", 9100), ("+0", 0), ("0" * 40 + "1", 1), ("65536", None), ("-1", None), ("9_464", None),
    ("９４６４", None), ("1" * 40, None),
])
def test_parse_port(value, want):
    assert _parse_port(value) == want


@pytest.mark.parametrize("value, want", [
    ("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "0.0.0.0"), ("::1", "::1"), ("[::1]", "::1"),
    ("FE80::AB", "fe80::ab"), ("::", ""), ("[::]", ""), ("localhost", "127.0.0.1"), ("LocalHost", "127.0.0.1"),
    ("::ffff:10.0.0.1", "10.0.0.1"), ("::ffff:0.0.0.0", "0.0.0.0"), ("[::ffff:7f00:1]", "127.0.0.1"),
    ("*", None), ("collector.example.com", None), ("127.0.0.1:9464", None), ("http://127.0.0.1", None),
    ("[127.0.0.1]", None), ("fe80::1%eth0", None), ("010.0.0.1", None), ("1.2.3", None), ("١٢٧.0.0.1", None),
])
def test_parse_prometheus_host(value, want):
    assert _parse_prometheus_host(value) == want


def test_prometheus_host_resolution(monkeypatch, caplog):
    assert _resolved(service_name="a29").resolved.prometheus_metrics_host == ""
    monkeypatch.setenv("OTEL_EXPORTER_PROMETHEUS_HOST", " [::1] ")
    assert _resolved(service_name="a29").resolved.prometheus_metrics_host == "::1"
    monkeypatch.setenv("OTEL_EXPORTER_PROMETHEUS_HOST", "*")
    with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
        assert _resolved(service_name="a29").resolved.prometheus_metrics_host == ""
    assert "OTEL_EXPORTER_PROMETHEUS_HOST='*': invalid host, not applied" in caplog.text
    assert "every interface" in caplog.text
    caplog.clear()
    monkeypatch.setenv("OTEL_EXPORTER_PROMETHEUS_HOST", "https://user:tok-s3cr3t@metrics.example.com?k=q-s3cr3t")
    with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
        _resolved(service_name="a29")
    assert "s3cr3t" not in caplog.text and "'https://metrics.example.com'" in caplog.text


@pytest.mark.parametrize("value, want", [
    ("plain", "plain"), ("https://u:p@h:1/x?q#f", "https://h:1/x"), ("u:p@h", "h"), ("h#frag", "h"),
    ("a@b@c", "c"), ("http://h/p@x", "http://x"),
])
def test_redact_echoed_value(value, want):
    from staffops_otel_helper.config import redact_echoed_value

    assert redact_echoed_value(value) == want


def _get(host: str, port: int) -> int:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", "/metrics")
        return conn.getresponse().status
    finally:
        conn.close()


def test_listener_binds_the_configured_ipv4_host(monkeypatch):
    port = _free_port()
    monkeypatch.setenv("OTEL_EXPORTER_PROMETHEUS_PORT", str(port))
    monkeypatch.setenv("OTEL_HELPER_METRICS_PORT", "1")  # the standard variable wins
    monkeypatch.setenv("OTEL_EXPORTER_PROMETHEUS_HOST", "127.0.0.1")
    provider = configure_metrics(Resource.create({}), _resolved(service_name="a29", metric_exporters=["prometheus"]))
    try:
        assert _get("127.0.0.1", port) == 200
        # Negative control (obs L8): another address of this host is refused.
        other = _non_loopback_ipv4()
        if other:
            with pytest.raises(OSError):
                socket.create_connection((other, port), timeout=1).close()
    finally:
        provider.shutdown()


def _non_loopback_ipv4() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent: picks the outgoing interface
            addr = s.getsockname()[0]
    except OSError:
        return None
    return None if addr.startswith("127.") else addr


def test_start_listener_on_hosts():
    port = _free_port()
    server, thread = helper_metrics._start_listener(port, "127.0.0.1")
    try:
        assert server.address_family == socket.AF_INET and server.server_address[:2] == ("127.0.0.1", port)
    finally:
        helper_metrics.stop_listener((server, thread))
    if not socket.has_ipv6:
        return
    try:
        server, thread = helper_metrics._start_listener(port, "::1")
    except OSError as e:  # no IPv6 loopback in this environment
        assert e.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT)
        return
    try:
        assert server.address_family == socket.AF_INET6 and server.server_address[0] == "::1"
        assert _get("::1", port) == 200
    finally:
        helper_metrics.stop_listener((server, thread))


def test_listener_on_an_address_not_owned_fails_setup(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_PROMETHEUS_HOST", "192.0.2.1")  # TEST-NET-1: never local
    monkeypatch.setenv("OTEL_EXPORTER_PROMETHEUS_PORT", str(_free_port()))
    with pytest.raises(RuntimeError, match=r"listener on 192\.0\.2\.1, port \d+") as err:
        configure_metrics(Resource.create({}), _resolved(service_name="a29", metric_exporters=["prometheus"]))
    # The hint names what applied (A29 fix round, M1).
    assert "OTEL_EXPORTER_PROMETHEUS_HOST sets the address" in str(err.value)
    assert "set OTEL_EXPORTER_PROMETHEUS_PORT=0" in str(err.value)


@pytest.mark.parametrize("env, code, want", [
    ({"OTEL_HELPER_METRICS_PORT": "{port}"}, None, "set OTEL_HELPER_METRICS_PORT=0"),
    ({"OTEL_EXPORTER_PROMETHEUS_PORT": "{port}", "OTEL_HELPER_METRICS_PORT": "1"}, None,
     "set OTEL_EXPORTER_PROMETHEUS_PORT=0"),
    ({}, "{port}", "set prometheus_metrics_port=0"),
])
def test_bind_failure_hint_names_the_port_source(monkeypatch, env, code, want):
    busy = socket.socket(socket.AF_INET6 if socket.has_ipv6 else socket.AF_INET, socket.SOCK_STREAM)
    try:
        busy.bind(("::" if socket.has_ipv6 else "0.0.0.0", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        for k, v in env.items():
            monkeypatch.setenv(k, v.format(port=port))
        opts = _resolved(service_name="a29", metric_exporters=["prometheus"],
                         prometheus_metrics_port=int(code.format(port=port)) if code else None)
        with pytest.raises(RuntimeError) as err:
            configure_metrics(Resource.create({}), opts)
        assert want in str(err.value) and "OTEL_EXPORTER_PROMETHEUS_HOST" not in str(err.value)
    finally:
        busy.close()


def test_skipped_endpoint_path_not_warned(monkeypatch, caplog):
    """A value the resolution skipped never produces the path warning (code-review M4b)."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://coll\tector:4317/skipped-path")
    with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
        opts = _resolved(service_name="a29")
    assert "endpoint path is ignored" not in caplog.text and "skipped-path" not in caplog.text
    assert opts.otlp_export("traces") is not None
