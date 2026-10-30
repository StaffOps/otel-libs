"""Task A18: contract rulings from the Node B4b review (design D1 rows dated
2026-10-29): TLS files, export deadline, code export timeout bound, header
bytes, value-length limits.
"""

import logging
import os

import pytest

from staffops_otel_helper import _exporters
from staffops_otel_helper._tls import TlsFiles, resolve_tls, tls_variables
from staffops_otel_helper.config import IGNORED_TIMEOUT_VARS, TelemetryOptions, _parse_headers


def _opts(**kwargs):
    opts = TelemetryOptions(service_name="s", **kwargs)
    opts.resolve_from_env()
    opts.validate()
    return opts


def _helper_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name.startswith("staffops_otel_helper") and r.levelno >= logging.WARNING]


class TestHeaderBytes:
    def test_non_ascii_and_control_values_are_skipped(self):
        headers, bad, literal = _parse_headers("x-a=%C3%A9,x-b=ok,x-c=é,x-d=%7F,x-bin=abc,x-u=%FF")
        assert headers == {"x-b": "ok", "x-bin": "abc", "x-u": "%FF"}
        assert bad == [1, 3, 4]
        assert literal == [6]

    def test_bin_keys_sent_as_bytes_on_grpc(self):
        assert _exporters._grpc_headers({"x-bin": "abc", "x-a": "1"}) == {"x-bin": b"abc", "x-a": "1"}


class TestExportDeadline:
    def test_default_timeout_always_explicit(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c:4317")
        plan = _opts().otlp_export("traces")
        assert plan.kwargs["timeout"] == 10.0
        assert plan.timeout_ms == 10000.0

    def test_env_and_code_timeout_become_the_deadline(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c:4317")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "1500")
        assert _opts().otlp_export("traces").timeout_ms == 1500.0
        assert _opts(export_timeout_ms=2500).otlp_export("traces").timeout_ms == 2500.0

    def test_ignored_timeout_variables_warn_once(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        for var in IGNORED_TIMEOUT_VARS:
            monkeypatch.setenv(var, "5")
        _opts()
        warnings = _helper_warnings(caplog)
        for var in IGNORED_TIMEOUT_VARS:
            assert sum(var in w and "not honored" in w for w in warnings) == 1, warnings

    @pytest.mark.parametrize("value", [86_400_001, 10**12])
    def test_code_timeout_above_24h_fails(self, value):
        with pytest.raises(ValueError, match=r"\[1, 86400000\]"):
            _opts(export_timeout_ms=value)

    def test_code_timeout_24h_accepted(self):
        _opts(export_timeout_ms=86_400_000)

    def test_processors_and_reader_get_the_deadline(self, monkeypatch):
        from staffops_otel_helper import setup_telemetry, shutdown_telemetry

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c:4317")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1234")
        captured = {}
        import staffops_otel_helper.metrics as metrics_mod
        import staffops_otel_helper.tracing as tracing_mod
        import staffops_otel_helper.logging_setup as logging_mod

        def spy(name, real):
            def make(*args, **kwargs):
                captured[name] = kwargs.get("export_timeout_millis")
                return real(*args, **kwargs)
            return make

        monkeypatch.setattr(tracing_mod, "BatchSpanProcessor", spy("bsp", tracing_mod.BatchSpanProcessor))
        monkeypatch.setattr(logging_mod, "BatchLogRecordProcessor", spy("blrp", logging_mod.BatchLogRecordProcessor))
        monkeypatch.setattr(metrics_mod, "PeriodicExportingMetricReader",
                            spy("reader", metrics_mod.PeriodicExportingMetricReader))
        setup_telemetry(TelemetryOptions(service_name="s"))
        try:
            assert captured == {"bsp": 1234.0, "blrp": 1234.0, "reader": 1234.0}
        finally:
            shutdown_telemetry()


@pytest.fixture
def tls_env(monkeypatch, tmp_path):
    for var in tls_variables("traces"):
        monkeypatch.delenv(var, raising=False)

    def write(name, content):
        p = tmp_path / name
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
        return str(p)

    return monkeypatch, write, tmp_path


def _resolve(warned):
    def warn(key, msg, *args):
        warned.append(msg % args)
    return warn


class TestTlsFiles:
    def test_unreadable_and_not_pem_ca_fall_back(self, tls_env):
        mp, write, tmp = tls_env
        warned = []
        mp.setenv("OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE", write("garbage.pem", "s3cret-not-pem"))
        mp.setenv("OTEL_EXPORTER_OTLP_CERTIFICATE", str(tmp / "missing.pem"))
        assert resolve_tls("traces", _resolve(warned)) == TlsFiles()
        assert "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE" in warned[0] and "the generic variable" in warned[0]
        assert "OTEL_EXPORTER_OTLP_CERTIFICATE" in warned[1] and "ENOENT" in warned[1] and "system roots" in warned[1]
        assert not any("s3cret" in w or str(tmp) in w for w in warned)

    def test_good_generic_ca_after_bad_per_signal(self, tls_env):
        ca = os.environ.get("FAKEOTLP_CA")
        if not ca or not os.path.exists(ca):
            pytest.skip("needs FAKEOTLP_CA")
        mp, write, _ = tls_env
        warned = []
        mp.setenv("OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE", os.devnull)
        mp.setenv("OTEL_EXPORTER_OTLP_CERTIFICATE", ca)
        files = resolve_tls("traces", _resolve(warned))
        assert files.ca_path == ca and files.ca and files.key is None
        assert len(warned) == 1 and "TRACES_CERTIFICATE" in warned[0]
        # Certificate used as the key: rejected pair, CA kept.
        mp.setenv("OTEL_EXPORTER_OTLP_CLIENT_KEY", ca)
        mp.setenv("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", ca)
        files = resolve_tls("traces", _resolve(warned))
        assert files.ca_path == ca and files.key_path is None
        assert "rejected by TLS" in warned[-1]

    def test_incomplete_pair_is_server_auth(self, tls_env):
        mp, write, tmp = tls_env
        warned = []
        mp.setenv("OTEL_EXPORTER_OTLP_CLIENT_KEY", write("k.pem", "key"))
        assert resolve_tls("traces", _resolve(warned)) == TlsFiles()
        assert "must both be set" in warned[0] and "OTEL_EXPORTER_OTLP_CLIENT_KEY" in warned[0]
        warned.clear()
        mp.setenv("OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE", str(tmp / "missing"))
        resolve_tls("traces", _resolve(warned))
        assert "cannot be read" in warned[0] and "must both be set" in warned[1]

    def test_rejected_pair(self, tls_env):
        mp, write, _ = tls_env
        warned = []
        p = write("bad.pem", "nope")
        mp.setenv("OTEL_EXPORTER_OTLP_CLIENT_KEY", p)
        mp.setenv("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", p)
        assert resolve_tls("traces", _resolve(warned)) == TlsFiles()
        assert "rejected by TLS" in warned[0]

    def test_tls_files_never_in_repr(self):
        assert "s3cret" not in repr(TlsFiles(key=b"s3cret", cert=b"c", ca=b"a"))

    def test_tls_kwargs_per_protocol(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://c:4318")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
        plan = _opts().otlp_export("traces")
        assert _exporters._tls_kwargs(plan) == {}
        from dataclasses import replace
        http = replace(plan, tls=TlsFiles(ca_path="/ca", key_path="/k", cert_path="/c"))
        assert _exporters._tls_kwargs(http) == {"certificate_file": "/ca", "client_key_file": "/k",
                                                "client_certificate_file": "/c"}
        grpc_plan = replace(plan, protocol="grpc", tls=TlsFiles())
        assert set(_exporters._tls_kwargs(grpc_plan)) == {"credentials"}
        assert _exporters._tls_kwargs(replace(plan, tls=None)) == {}

    def test_tls_variables_masked_during_construction(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://c:4317")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_CLIENT_KEY", "/nonexistent/k")
        plan = _opts().otlp_export("traces")
        assert "OTEL_EXPORTER_OTLP_CLIENT_KEY" in plan.masked_env
        exporter = _exporters.build_otlp_exporter(plan)  # no grpc abort: the incomplete pair never reaches grpc
        exporter.shutdown()
        assert os.environ["OTEL_EXPORTER_OTLP_CLIENT_KEY"] == "/nonexistent/k"


class TestValueLengthZero:
    def test_no_zero_no_override(self):
        assert _opts().value_length_limits() == {}

    def test_generic_zero_means_no_limit(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        monkeypatch.setenv("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "0")
        assert _opts().value_length_limits() == {"span": -1, "log": -1, "generic": -1}
        assert any("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT=0" in w for w in _helper_warnings(caplog))

    def test_per_signal_zero_falls_back_to_generic(self, monkeypatch):
        monkeypatch.setenv("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "0")
        monkeypatch.setenv("OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT", "0")
        monkeypatch.setenv("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "40")
        assert _opts().value_length_limits() == {"span": 40, "log": 40}

    def test_generic_zero_keeps_a_non_zero_signal_value(self, monkeypatch):
        # Code-review HIGH: the signal variable must not be overridden by the generic fallback.
        from opentelemetry import trace

        from staffops_otel_helper import setup_telemetry, shutdown_telemetry

        monkeypatch.setenv("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "0")
        monkeypatch.setenv("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "3")
        assert _opts().value_length_limits() == {"log": -1, "generic": -1}  # logs: generic 0 → no limit
        setup_telemetry(TelemetryOptions(service_name="s"))
        try:
            span = trace.get_tracer("t").start_span("s", attributes={"a": "abcdef"})
            assert span.attributes["a"] == "abc"
            span.end()
        finally:
            shutdown_telemetry()

    def test_span_limits_reach_the_provider(self, monkeypatch):
        from opentelemetry import trace

        from staffops_otel_helper import setup_telemetry, shutdown_telemetry

        monkeypatch.setenv("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "0")
        setup_telemetry(TelemetryOptions(service_name="s"))
        try:
            span = trace.get_tracer("t").start_span("s", attributes={"a": "abcdef"})
            assert span.attributes["a"] == "abcdef"
            span.end()
        finally:
            shutdown_telemetry()


class TestBatchSizeAboveQueue:
    def test_no_override_by_default(self):
        assert _opts().batch_size_override("OTEL_BSP") == {}

    @pytest.mark.parametrize("prefix", ["OTEL_BSP", "OTEL_BLRP"])
    def test_batch_above_queue_uses_queue_and_warns(self, monkeypatch, caplog, prefix):
        caplog.set_level(logging.WARNING)
        monkeypatch.setenv(f"{prefix}_MAX_QUEUE_SIZE", "10")
        monkeypatch.setenv(f"{prefix}_MAX_EXPORT_BATCH_SIZE", "50")
        opts = _opts()
        assert opts.batch_size_override(prefix) == {"max_export_batch_size": 10}
        opts.batch_size_override(prefix)
        msgs = [w for w in _helper_warnings(caplog) if f"{prefix}_MAX_QUEUE_SIZE" in w]
        assert len(msgs) == 1 and f"{prefix}_MAX_EXPORT_BATCH_SIZE" in msgs[0]

    def test_default_queue_and_garbage(self, monkeypatch):
        monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "5000")
        assert _opts().batch_size_override("OTEL_BSP") == {"max_export_batch_size": 2048}
        monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "many")
        assert _opts().batch_size_override("OTEL_BSP") == {}

    def test_setup_survives_batch_above_queue(self, monkeypatch):
        from staffops_otel_helper import setup_telemetry, shutdown_telemetry

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c:4317")
        monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp,console")
        monkeypatch.setenv("OTEL_LOGS_EXPORTER", "otlp,console")
        for prefix in ("OTEL_BSP", "OTEL_BLRP"):
            monkeypatch.setenv(f"{prefix}_MAX_QUEUE_SIZE", "10")
            monkeypatch.setenv(f"{prefix}_MAX_EXPORT_BATCH_SIZE", "50")
        setup_telemetry(TelemetryOptions(service_name="s"))
        shutdown_telemetry()
