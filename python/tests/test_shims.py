"""Helper-owned OTLP shims (ADR-001 exceptions, design D1.b–e) and setup-level contract items.

What the helper passes to the SDK exporters is asserted on the resolved
``SignalExport`` and on the real exporter objects; what reaches the wire is the
parity fixture's job (``export`` cases).
"""

import ast
import logging
import os
from pathlib import Path

import pytest

from staffops_otel_helper import _semconv
from staffops_otel_helper._exporters import build_otlp_exporter
from staffops_otel_helper.config import TelemetryOptions


def _plan(signal="traces", **kwargs):
    opts = TelemetryOptions(service_name="s", **kwargs)
    opts.resolve_from_env()
    opts.validate()
    return opts.otlp_export(signal)


class TestDeferToSdk:
    """ADR-001: absolute env URLs → the helper passes no endpoint/insecure/timeout/headers."""

    @pytest.mark.parametrize("endpoint", [
        "http://collector:4317", "https://collector:4317", "https://vendor.io",
        "http://collector:4318/otlp", "http://[::1]:4317",
    ])
    def test_env_url_not_passed(self, endpoint):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        plan = _plan()
        assert plan.source == "env"
        assert plan.kwargs == {"compression": "gzip"}

    def test_per_signal_env_url_not_passed(self):
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "http://collector:4317"
        assert _plan().kwargs == {"compression": "gzip"}


class TestCodeEndpoints:
    def test_generic_http_gets_signal_path(self):
        for signal in ("traces", "metrics", "logs"):
            plan = _plan(signal, otel_endpoint="https://vendor.io/otlp/", otlp_protocol="http/protobuf")
            assert plan.kwargs["endpoint"] == f"https://vendor.io/otlp/v1/{signal}"

    def test_per_signal_verbatim(self):
        plan = _plan(traces_endpoint="http://c:4318/custom/traces", otlp_protocol="http/protobuf")
        assert plan.kwargs["endpoint"] == "http://c:4318/custom/traces"

    def test_per_signal_beats_generic(self):
        plan = _plan(otel_endpoint="http://a:4318", traces_endpoint="http://b:4318/t", otlp_protocol="http/protobuf")
        assert plan.kwargs["endpoint"] == "http://b:4318/t"
        assert _plan("metrics", otel_endpoint="http://a:4318").kwargs["endpoint"] == "http://a:4318/v1/metrics"

    def test_code_beats_env(self):
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "http://env:4317"
        plan = _plan(otel_endpoint="http://code:4317")
        assert plan.source == "code"
        assert plan.kwargs["endpoint"] == "http://code:4317"

    def test_grpc_passes_insecure_from_scheme(self):
        assert _plan(otel_endpoint="https://c:4317").kwargs["insecure"] is False
        assert _plan(otel_endpoint="http://c:4317").kwargs["insecure"] is True

    def test_ipv6_zone_unquoted_for_grpc_only(self):
        assert _plan(otel_endpoint="http://[fe80::1%25lo]:4317").kwargs["endpoint"] == "http://[fe80::1%lo]:4317"
        plan = _plan(otel_endpoint="http://[fe80::1%25lo]:4318", otlp_protocol="http/protobuf")
        assert plan.kwargs["endpoint"] == "http://[fe80::1%25lo]:4318/v1/traces"

    @pytest.mark.parametrize("bad", ["http://c\tx:4317", "http://c:43\t17", "http://c:4318/v1\n/x", "http://c\x7f:4317"])
    def test_control_char_inside_endpoint_falls_back(self, bad, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://good:4317"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = bad
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan()
        assert plan.kwargs["endpoint"] == "http://good:4317"
        assert "<unparseable URL>" in caplog.text

    def test_http_zone_id_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            _plan(otel_endpoint="http://[fe80::1%25lo]:4318", otlp_protocol="http/protobuf")
        assert "zone" in caplog.text

    def test_env_whitespace_trimmed_and_owned(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = " http://c:4318\n"
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        assert _plan("logs").kwargs["endpoint"] == "http://c:4318/v1/logs"

    def test_ipv6_brackets_kept(self):
        assert _plan(otel_endpoint="http://[::1]:4317").kwargs["endpoint"] == "http://[::1]:4317"
        plan = _plan(otel_endpoint="http://[::1]:4318", otlp_protocol="http/protobuf")
        assert plan.kwargs["endpoint"] == "http://[::1]:4318/v1/traces"


class TestHeaders:
    """D1 edge-case rulings: headers parsed by the helper, malformed items skipped (position only)."""

    def test_unset_not_passed(self):
        assert "headers" not in _plan(otel_endpoint="http://c:4317").kwargs

    def test_parsed_percent_decoded_and_lowercased(self):
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "Authorization=Bearer%20tok,x-a = 1 , x-b=a=b"
        assert _plan(otel_endpoint="http://c:4317").kwargs["headers"] == {
            "authorization": "Bearer tok", "x-a": "1", "x-b": "a=b"}

    def test_malformed_items_skipped_without_values(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "a=secret-1,broken-secret,,=x"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan(otel_endpoint="http://c:4317")
        assert plan.kwargs["headers"] == {"a": "secret-1"}
        assert "position(s) 2, 3, 4" in caplog.text
        assert "secret" not in caplog.text

    def test_per_signal_replaces_generic(self):
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "a=1"
        os.environ["OTEL_EXPORTER_OTLP_LOGS_HEADERS"] = "b=2"
        assert _plan("logs", otel_endpoint="http://c:4317").kwargs["headers"] == {"b": "2"}
        assert _plan("traces", otel_endpoint="http://c:4317").kwargs["headers"] == {"a": "1"}

    def test_key_charset_not_decoded_and_last_wins(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "x%2Da=1,bad key=2,X-B=1,x-b=2,ok_k.1=v"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            headers = _plan(otel_endpoint="http://c:4317").kwargs["headers"]
        assert headers == {"x-b": "2", "ok_k.1": "v"}
        assert "position(s) 1, 2" in caplog.text

    def test_invalid_escape_keeps_whole_value_literal(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "a=50%,b=%zz-secret,c=a%20b%zz,d=%FF,e=ok%20v"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            headers = _plan(otel_endpoint="http://c:4317").kwargs["headers"]
        assert headers == {"a": "50%", "b": "%zz-secret", "c": "a%20b%zz", "d": "%FF", "e": "ok v"}
        assert "position(s) 1, 2, 3, 4" in caplog.text and "secret" not in caplog.text

    def test_decoded_control_char_skipped(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "a=x%0D%0Ay,b=1,c=t%7F"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            headers = _plan(otel_endpoint="http://c:4317").kwargs["headers"]
        assert headers == {"b": "1"}
        assert "position(s) 1, 3" in caplog.text

    def test_per_signal_without_valid_item_falls_back_to_generic(self):
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "a=1"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] = "broken"
        assert _plan(otel_endpoint="http://c:4317").kwargs["headers"] == {"a": "1"}

    def test_headers_env_masked_during_construction(self):
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "a=1"
        plan = _plan(otel_endpoint="http://c:4317")
        assert "OTEL_EXPORTER_OTLP_HEADERS" in plan.masked_env

    @pytest.mark.parametrize("protocol,endpoint", [("grpc", "http://c:4317"), ("http/protobuf", "http://c:4318")])
    def test_all_malformed_never_reaches_sdk_logs(self, protocol, endpoint, caplog):
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "Authorization Bearer s3cr3t"
        with caplog.at_level(logging.DEBUG):  # every logger, SDK included
            plan = _plan(otel_endpoint=endpoint, otlp_protocol=protocol)
            assert plan.kwargs["headers"] == {}
            build_otlp_exporter(plan).shutdown()
        assert "s3cr3t" not in caplog.text
        assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization Bearer s3cr3t"  # restored

    @pytest.mark.parametrize("protocol,endpoint", [("grpc", "http://c:4317"), ("http/protobuf", "http://c:4318")])
    def test_builds(self, protocol, endpoint):
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "a=1,broken"
        build_otlp_exporter(_plan(otel_endpoint=endpoint, otlp_protocol=protocol)).shutdown()


class TestSchemeless:
    """R1: host:port → https:// (http:// only if INSECURE=true), port defaulted."""

    def test_grpc_tls_by_default_no_warning(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "collector:14317"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan()
        assert plan.kwargs["endpoint"] == "https://collector:14317"
        assert plan.kwargs["insecure"] is False
        assert "deprecat" not in caplog.text

    def test_grpc_insecure_env(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "collector"
        os.environ["OTEL_EXPORTER_OTLP_INSECURE"] = "true"
        plan = _plan()
        assert plan.kwargs["endpoint"] == "http://collector:4317"
        assert plan.kwargs["insecure"] is True

    def test_code_insecure_option(self):
        assert _plan(otel_endpoint="collector:4317", insecure=True).kwargs["endpoint"] == "http://collector:4317"

    def test_http_default_port_and_deprecation(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "collector"
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan("metrics")
        assert plan.kwargs["endpoint"] == "https://collector:4318/v1/metrics"
        assert "deprecated" in caplog.text and "OTEL_EXPORTER_OTLP_ENDPOINT" in caplog.text

    def test_http_per_signal_keeps_path(self):
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "collector:4318/v1/traces"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_INSECURE"] = "true"
        assert _plan().kwargs["endpoint"] == "http://collector:4318/v1/traces"


class TestPortlessGrpc:
    """D1.b: gRPC URL without a port → 4317 + warning recommending an explicit port."""

    def test_gets_4317_and_warns_once(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://collector"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            opts = TelemetryOptions(service_name="s")
            opts.resolve_from_env()
        assert opts.otlp_export("traces").kwargs["endpoint"] == "https://collector:4317"
        assert caplog.text.count("explicit port") == 1

    def test_http_portless_left_to_sdk(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://vendor.io"
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        assert "endpoint" not in _plan().kwargs


class TestTlsConflict:
    """D1.c: the scheme wins; explicit endpoint + insecure for that signal only; warning."""

    def test_https_with_insecure_true(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://c:4317"
        os.environ["OTEL_EXPORTER_OTLP_INSECURE"] = "true"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan()
        assert plan.kwargs["insecure"] is False
        assert "OTEL_EXPORTER_OTLP_INSECURE" in caplog.text

    def test_http_with_insecure_false(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://c:4317"
        os.environ["OTEL_EXPORTER_OTLP_INSECURE"] = "false"
        assert _plan().kwargs["insecure"] is True

    def test_per_signal_conflict_only_overrides_that_signal(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://c:4317"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_INSECURE"] = "true"
        assert _plan("traces").kwargs["insecure"] is False
        assert "endpoint" not in _plan("metrics").kwargs

    def test_consistent_settings_left_to_sdk(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://c:4317"
        os.environ["OTEL_EXPORTER_OTLP_INSECURE"] = "true"
        assert "endpoint" not in _plan().kwargs

    def test_certificate_with_plaintext_warns_without_paths(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://c:4317"
        os.environ["OTEL_EXPORTER_OTLP_CERTIFICATE"] = "/x/ca.pem"
        os.environ["OTEL_EXPORTER_OTLP_CLIENT_KEY"] = "/x/secret-key.pem"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            _plan()
        assert "OTEL_EXPORTER_OTLP_CERTIFICATE" in caplog.text
        assert "OTEL_EXPORTER_OTLP_CLIENT_KEY" in caplog.text
        assert "secret-key" not in caplog.text and "/x/ca.pem" not in caplog.text

    def test_grpc_plaintext_with_cert_passes_insecure(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://c:4317"
        os.environ["OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE"] = "/x/cert.pem"
        assert _plan().kwargs["insecure"] is True

    def test_generic_conflict_warns_once_for_all_signals(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://c:4317"
        os.environ["OTEL_EXPORTER_OTLP_INSECURE"] = "true"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            opts = TelemetryOptions(service_name="s")
            opts.resolve_from_env()
        assert caplog.text.count("contradicts") == 1

    def test_invalid_insecure_is_false(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://c:4317"
        os.environ["OTEL_EXPORTER_OTLP_INSECURE"] = "yes"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan()
        # "yes" -> false contradicts http://: the scheme wins, plaintext.
        assert plan.kwargs["insecure"] is True
        assert "treating it as false" in caplog.text


class TestUserinfo:
    def test_grpc_strips_userinfo_and_query_and_redacts(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://u:pw-secret@c:4317?token=q-secret"
        with caplog.at_level(logging.DEBUG, logger="staffops_otel_helper"):
            plan = _plan()
        assert plan.kwargs["endpoint"] == "http://c:4317"
        assert "pw-secret" not in caplog.text and "q-secret" not in caplog.text

    def test_http_userinfo_stripped(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://u:pw-secret@c:4318/otlp"
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            assert _plan().kwargs["endpoint"] == "https://c:4318/otlp/v1/traces"
        assert "pw-secret" not in caplog.text and "userinfo" in caplog.text

    def test_http_query_no_longer_breaks_the_signal_path(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://h:4318/otlp?x=1"
        assert _plan("logs").kwargs["endpoint"] == "http://h:4318/otlp/v1/logs"

    def test_code_url_userinfo_stripped(self):
        plan = _plan(traces_endpoint="http://u:p@h:4318/t?k=v", otlp_protocol="http/protobuf")
        assert plan.kwargs["endpoint"] == "http://h:4318/t"


class TestTimeout:
    """D1.e: env *_TIMEOUT is milliseconds; passed as seconds."""

    def test_unset_not_passed(self):
        assert "timeout" not in _plan(otel_endpoint="http://c:4317").kwargs

    def test_generic_ms_to_seconds(self):
        os.environ["OTEL_EXPORTER_OTLP_TIMEOUT"] = "2500"
        assert _plan(otel_endpoint="http://c:4317").kwargs["timeout"] == 2.5

    def test_per_signal_beats_generic(self):
        os.environ["OTEL_EXPORTER_OTLP_TIMEOUT"] = "2500"
        os.environ["OTEL_EXPORTER_OTLP_LOGS_TIMEOUT"] = "500"
        assert _plan("logs", otel_endpoint="http://c:4317").kwargs["timeout"] == 0.5
        assert _plan("traces", otel_endpoint="http://c:4317").kwargs["timeout"] == 2.5

    def test_code_beats_env(self):
        os.environ["OTEL_EXPORTER_OTLP_TIMEOUT"] = "2500"
        assert _plan(otel_endpoint="http://c:4317", export_timeout_ms=750).kwargs["timeout"] == 0.75

    def test_seconds_like_value_warns(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_TIMEOUT"] = "10"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan(otel_endpoint="http://c:4317")
        assert plan.kwargs["timeout"] == 0.01
        assert "looks like seconds" in caplog.text

    @pytest.mark.parametrize("raw", ["soon", "0", "-1", "inf"])
    def test_invalid_warns_uses_default_and_masks(self, raw, caplog):
        os.environ["OTEL_EXPORTER_OTLP_TRACES_TIMEOUT"] = raw
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan(otel_endpoint="http://c:4317")
        assert plan.kwargs["timeout"] == 10.0
        assert plan.masked_env == ("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",)
        assert "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT" in caplog.text

    def test_invalid_per_signal_value_does_not_crash_grpc_exporter(self):
        os.environ["OTEL_EXPORTER_OTLP_TRACES_TIMEOUT"] = "soon"
        exporter = build_otlp_exporter(_plan(otel_endpoint="http://c:4317"))
        try:
            assert os.environ["OTEL_EXPORTER_OTLP_TRACES_TIMEOUT"] == "soon"  # restored
        finally:
            exporter.shutdown()

    def test_http_exporter_gets_seconds(self):
        from unittest.mock import patch

        os.environ["OTEL_EXPORTER_OTLP_TIMEOUT"] = "1500"
        with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter") as cls:
            build_otlp_exporter(_plan(otel_endpoint="http://c:4318", otlp_protocol="http/protobuf"))
        assert cls.call_args.kwargs["timeout"] == 1.5

    def test_invalid_per_signal_falls_back_to_generic(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_TIMEOUT"] = "2500"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_TIMEOUT"] = "soon"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan(otel_endpoint="http://c:4317")
        assert plan.kwargs["timeout"] == 2.5
        assert plan.masked_env == ("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",)

    @pytest.mark.parametrize("raw,seconds", [("0.5", 0.001), ("100000000", 86400.0), (" 250 ", 0.25)])
    def test_clamped_to_range_with_warning(self, raw, seconds, caplog):
        os.environ["OTEL_EXPORTER_OTLP_TIMEOUT"] = raw
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            assert _plan(otel_endpoint="http://c:4317").kwargs["timeout"] == seconds
        if raw.strip() != "250":
            assert "[1 ms, 24 h]" in caplog.text

    @pytest.mark.parametrize("raw", ["0x1p10", "1_000", "nan", "1e3", ".5", "+5", "5.", "1e-9", "inf"])
    def test_non_decimal_rejected(self, raw, caplog):
        os.environ["OTEL_EXPORTER_OTLP_TIMEOUT"] = raw
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            assert _plan(otel_endpoint="http://c:4317").kwargs["timeout"] == 10.0
        assert "OTEL_EXPORTER_OTLP_TIMEOUT" in caplog.text

    def test_code_timeout_with_invalid_per_signal_env_masks_it(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_TRACES_TIMEOUT"] = "soon"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan(otel_endpoint="http://c:4317", export_timeout_ms=750)
        assert plan.kwargs["timeout"] == 0.75
        assert plan.masked_env == ("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",)
        assert "code value used" in caplog.text
        build_otlp_exporter(plan).shutdown()  # must not raise


class TestCompression:
    """D1.d: code > env > gzip; always passed (the gRPC SDK raises on env 'none')."""

    def test_default_gzip(self):
        assert _plan(otel_endpoint="http://c:4317").kwargs["compression"] == "gzip"

    def test_env_none_and_per_signal(self):
        os.environ["OTEL_EXPORTER_OTLP_COMPRESSION"] = "none"
        os.environ["OTEL_EXPORTER_OTLP_LOGS_COMPRESSION"] = "gzip"
        assert _plan("traces", otel_endpoint="http://c:4317").kwargs["compression"] == "none"
        assert _plan("logs", otel_endpoint="http://c:4317").kwargs["compression"] == "gzip"

    def test_code_beats_env(self):
        os.environ["OTEL_EXPORTER_OTLP_COMPRESSION"] = "gzip"
        assert _plan(otel_endpoint="http://c:4317", compression="none").kwargs["compression"] == "none"

    def test_invalid_per_signal_falls_back_to_generic(self):
        os.environ["OTEL_EXPORTER_OTLP_COMPRESSION"] = "none"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_COMPRESSION"] = "deflate"
        assert _plan(otel_endpoint="http://c:4317").kwargs["compression"] == "none"

    def test_case_insensitive(self):
        os.environ["OTEL_EXPORTER_OTLP_COMPRESSION"] = "NONE"
        assert _plan(otel_endpoint="http://c:4317").kwargs["compression"] == "none"

    def test_invalid_env_warns_and_uses_gzip(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_COMPRESSION"] = "zstd"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            assert _plan(otel_endpoint="http://c:4317").kwargs["compression"] == "gzip"
        assert "OTEL_EXPORTER_OTLP_COMPRESSION" in caplog.text

    @pytest.mark.parametrize("protocol,endpoint", [("grpc", "http://c:4317"), ("http/protobuf", "http://c:4318")])
    @pytest.mark.parametrize("name", ["gzip", "none", "GZIP"])
    def test_env_none_builds_for_both_protocols(self, protocol, endpoint, name):
        os.environ["OTEL_EXPORTER_OTLP_COMPRESSION"] = name
        exporter = build_otlp_exporter(_plan(otel_endpoint=endpoint, otlp_protocol=protocol))
        exporter.shutdown()


class TestSemconvDefault:
    """R10 / D12."""

    @pytest.fixture(autouse=True)
    def _reset_flag(self, monkeypatch):
        monkeypatch.setattr(_semconv, "_warned", False)

    def test_defaults_to_http(self, monkeypatch):
        monkeypatch.setattr(_semconv, "_late_initialized_old_http", lambda: False)
        _semconv.ensure_semconv_default()
        assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http,database/dup"

    @pytest.mark.parametrize("value", ["http/dup", "database,http"])
    def test_user_value_with_http_wins_silently(self, value, monkeypatch, caplog):
        monkeypatch.setattr(_semconv, "_late_initialized_old_http", lambda: False)
        os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = value
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            _semconv.ensure_semconv_default()
        assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == value
        assert caplog.text == ""

    def test_user_value_without_http_warns_once(self, monkeypatch, caplog):
        monkeypatch.setattr(_semconv, "_late_initialized_old_http", lambda: False)
        os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "database"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            _semconv.ensure_semconv_default()
            _semconv.ensure_semconv_default()
        assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "database"
        assert caplog.text.count("old semantic conventions") == 1

    def test_late_initialization_warns(self, monkeypatch, caplog):
        monkeypatch.setattr(_semconv, "_late_initialized_old_http", lambda: True)
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            _semconv.ensure_semconv_default()
        assert "initialized before staffops_otel_helper" in caplog.text

    def test_late_detection_reads_contrib_state(self, monkeypatch):
        from opentelemetry.instrumentation import _semconv as contrib

        stability = contrib._OpenTelemetrySemanticConventionStability
        http = contrib._OpenTelemetryStabilitySignalType.HTTP
        monkeypatch.setattr(stability, "_initialized", True)
        monkeypatch.setattr(stability, "_OTEL_SEMCONV_STABILITY_SIGNAL_MAPPING",
                            {http: contrib._StabilityMode.DEFAULT})
        assert _semconv._late_initialized_old_http() is True
        monkeypatch.setattr(stability, "_OTEL_SEMCONV_STABILITY_SIGNAL_MAPPING",
                            {http: contrib._StabilityMode.HTTP})
        assert _semconv._late_initialized_old_http() is False
        monkeypatch.setattr(stability, "_initialized", False)
        assert _semconv._late_initialized_old_http() is False

    def test_late_detection_tolerates_missing_private_api(self, monkeypatch):
        from opentelemetry.instrumentation import _semconv as contrib

        monkeypatch.delattr(contrib, "_OpenTelemetrySemanticConventionStability")
        assert _semconv._late_initialized_old_http() is False

    @pytest.mark.parametrize("entry", ["aws", "redis", "sql"])
    def test_ext_entry_points_set_default_first(self, entry, monkeypatch):
        import importlib
        import sys

        calls = []
        module = importlib.import_module(f"staffops_otel_helper.ext.{entry}")
        monkeypatch.setattr(module, "ensure_semconv_default", lambda: calls.append(entry))
        # Make the instrumentor import fail: the default must already be set by then.
        monkeypatch.setitem(sys.modules, {"aws": "opentelemetry.instrumentation.botocore",
                                          "redis": "opentelemetry.instrumentation.redis",
                                          "sql": "opentelemetry.instrumentation.sqlalchemy"}[entry], None)
        with pytest.raises(ImportError):
            getattr(module, f"instrument_{entry}")()
        assert calls == [entry]


class TestSetupContract:
    def test_setup_sets_semconv_default(self):
        from staffops_otel_helper import setup_telemetry

        setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
        assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http,database/dup"

    def test_setup_installs_helper_propagator(self, monkeypatch):
        import staffops_otel_helper.setup as setup_mod

        calls = []
        monkeypatch.setattr(setup_mod, "set_global_textmap", lambda p: calls.append(p))
        os.environ["OTEL_PROPAGATORS"] = "baggage"
        setup_mod.setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
        assert [type(p).__name__ for p in calls[0]._propagators] == ["W3CBaggagePropagator"]


class TestSdkDisabled:
    def test_short_circuits(self, monkeypatch):
        import staffops_otel_helper.setup as setup_mod

        called = []
        for fn in ("build_tracer_provider", "build_metrics", "build_logging", "configure_instrumentations"):
            monkeypatch.setattr(setup_mod, fn, lambda *a, _n=fn: called.append(_n))
        propagators = []
        monkeypatch.setattr(setup_mod, "set_global_textmap", lambda p: propagators.append(p))
        os.environ["OTEL_SDK_DISABLED"] = "true"
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://c:4317"
        opts = setup_mod.setup_telemetry(TelemetryOptions(service_name="s"))
        assert opts.sdk_disabled and called == [] and len(propagators) == 1


class TestPropagators:
    """D1 propagators row: tracecontext, baggage, none; anything else warned and skipped."""

    def _names(self):
        from staffops_otel_helper._propagators import build_propagator

        return [type(p).__name__ for p in build_propagator()._propagators]

    def test_default_w3c(self):
        assert self._names() == ["TraceContextTextMapPropagator", "W3CBaggagePropagator"]

    def test_env_order_and_case(self):
        os.environ["OTEL_PROPAGATORS"] = " Baggage ,tracecontext,baggage"
        assert self._names() == ["W3CBaggagePropagator", "TraceContextTextMapPropagator"]

    def test_none_disables(self):
        os.environ["OTEL_PROPAGATORS"] = "tracecontext,none"
        assert self._names() == []

    def test_unsupported_warned_and_skipped(self, caplog):
        os.environ["OTEL_PROPAGATORS"] = "b3,tracecontext,xray"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            assert self._names() == ["TraceContextTextMapPropagator"]
        assert "OTEL_PROPAGATORS" in caplog.text and "'b3'" in caplog.text and "'xray'" in caplog.text

    def test_only_unsupported_keeps_the_default(self):
        os.environ["OTEL_PROPAGATORS"] = "jaeger"
        assert self._names() == ["TraceContextTextMapPropagator", "W3CBaggagePropagator"]

    def test_import_safe_noop_when_already_imported(self):
        from staffops_otel_helper._propagators import import_propagate_safely

        os.environ["OTEL_PROPAGATORS"] = "b3"
        import_propagate_safely()  # opentelemetry.propagate is already loaded: must not touch it
        assert os.environ["OTEL_PROPAGATORS"] == "b3"

    @pytest.mark.parametrize("value,expected", [
        ("b3,tracecontext", "['TraceContextTextMapPropagator']"),
        ("b3", "['TraceContextTextMapPropagator', 'W3CBaggagePropagator']"),
        ("baggage", "['W3CBaggagePropagator']"),
        ("TraceContext", "['TraceContextTextMapPropagator']"),
        ("tracecontext,", "['TraceContextTextMapPropagator']"),
        ("tracecontext,,baggage", "['TraceContextTextMapPropagator', 'W3CBaggagePropagator']"),
        ("", "['TraceContextTextMapPropagator', 'W3CBaggagePropagator']"),
        ("none", "[]"),
    ])
    def test_importing_the_helper_never_raises(self, value, expected):
        import subprocess
        import sys

        code = ("import os, staffops_otel_helper\n"
                "from opentelemetry.propagate import get_global_textmap\n"
                "print(os.environ['OTEL_PROPAGATORS'], [type(p).__name__ for p in get_global_textmap()._propagators])")
        env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
        env["OTEL_PROPAGATORS"] = value
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
        assert out.stdout.strip() == f"{value} {expected}".strip()


class TestInstanceId:
    """D4: code > env > SDK detector (>= 1.45) > helper UUID; env/code id in a forked worker warns."""

    def _resource(self, **kwargs):
        from staffops_otel_helper.setup import _build_resource

        opts = TelemetryOptions(service_name="s", **kwargs)
        opts.resolve_from_env()
        return _build_resource(opts)

    def test_generated_when_absent(self):
        a = self._resource().attributes["service.instance.id"]
        assert len(a) == 36

    def test_env_kept(self):
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "service.instance.id=inst-1"
        assert self._resource().attributes["service.instance.id"] == "inst-1"

    def test_code_kept(self):
        attrs = self._resource(resource_attributes={"service.instance.id": "code-1"}).attributes
        assert attrs["service.instance.id"] == "code-1"

    def test_env_resource_keys_never_overwritten(self):
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "deployment.environment.name=HML,service.name=ra"
        os.environ["ENVIRONMENT"] = "DEV"
        os.environ["SERVICE_NAME"] = "legacy"
        from staffops_otel_helper.setup import _build_resource

        opts = TelemetryOptions()  # no service name in code
        opts.resolve_from_env()
        attrs = _build_resource(opts).attributes
        assert attrs["deployment.environment.name"] == "HML"
        assert attrs["service.name"] == "ra"

    def test_forked_worker_with_explicit_id_warns(self, monkeypatch, caplog):
        import staffops_otel_helper.setup as setup_mod

        monkeypatch.setattr(setup_mod, "_forked_after_import", True)
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "service.instance.id=inst-1"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            attrs = self._resource().attributes
        assert attrs["service.instance.id"] == "inst-1"
        assert "service.instance.id" in caplog.text

    def test_forked_worker_without_explicit_id_silent(self, monkeypatch, caplog):
        import staffops_otel_helper.setup as setup_mod

        monkeypatch.setattr(setup_mod, "_forked_after_import", True)
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            self._resource()
        assert "service.instance.id" not in caplog.text

    def test_fork_after_setup_with_helper_id_warns(self, monkeypatch, caplog):
        import staffops_otel_helper.setup as setup_mod

        monkeypatch.setattr(setup_mod, "_initialized", True)
        monkeypatch.setattr(setup_mod, "_helper_instance_id", True)
        monkeypatch.setattr(setup_mod, "_fork_warning_pending", False)
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            setup_mod._mark_forked()
            assert caplog.text == ""  # the hook never logs (deadlock risk)
            from staffops_otel_helper import get_tracer

            get_tracer("x")
            get_tracer("x")
        assert caplog.text.count("post_fork") == 1
        assert "only in each worker" in caplog.text

    def test_fork_after_setup_with_sdk_id_silent(self, monkeypatch, caplog):
        import staffops_otel_helper.setup as setup_mod

        monkeypatch.setattr(setup_mod, "_initialized", True)
        monkeypatch.setattr(setup_mod, "_helper_instance_id", False)
        monkeypatch.setattr(setup_mod, "_fork_warning_pending", False)
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            setup_mod._mark_forked()
            setup_mod.emit_fork_warning()
        assert caplog.text == ""

    def test_fork_hook_marks_child(self, monkeypatch):
        import staffops_otel_helper.setup as setup_mod

        monkeypatch.setattr(setup_mod, "_forked_after_import", False)
        setup_mod._mark_forked()
        assert setup_mod._forked_after_import is True


class TestDiagnosticsChannel:
    """CLAUDE.md "Helper diagnostics": only the staffops_otel_helper* loggers, never warnings/print/std streams."""

    _PKG = Path(__file__).resolve().parents[1] / "staffops_otel_helper"
    _LEVELS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
    _FORBIDDEN_FROM = {("warnings", "warn"), ("sys", "stderr"), ("sys", "stdout"),
                       ("sys", "__stderr__"), ("sys", "__stdout__"), ("os", "write"), ("builtins", "print"),
                       ("logging", "lastResort")}
    _STD = ("stdout", "stderr", "__stdout__", "__stderr__")
    _DYNAMIC_MODULES = {"warnings", "sys", "traceback", "builtins"}

    def _trees(self):
        for path in sorted(self._PKG.rglob("*.py")):
            yield path.relative_to(self._PKG), ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    @staticmethod
    def _dotted(node) -> str:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))

    def _offenders(self, rel, tree):
        out = []
        aliases = {m: {m} for m in ("warnings", "logging", "sys", "os", "traceback", "builtins")}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in aliases and a.asname:
                        aliases[a.name].add(a.asname)
            if isinstance(node, ast.ImportFrom) and node.module in ("warnings", "logging", "sys", "os",
                                                                   "builtins", "traceback"):
                for a in node.names:
                    if (node.module, a.name) in self._FORBIDDEN_FROM or (
                            node.module == "logging" and a.name in self._LEVELS | {"root"}) or (
                            node.module == "traceback" and a.name.startswith("print_")):
                        out.append(f"{rel}:{node.lineno} from {node.module} import {a.name}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                name = self._dotted(node)
                head, _, rest = name.partition(".")
                if head in aliases["sys"] and rest in self._STD:
                    out.append(f"{rel}:{node.lineno} {name}")
                if head in aliases["logging"] and rest in ("lastResort", "root"):
                    out.append(f"{rel}:{node.lineno} {name}")
            if not isinstance(node, ast.Call):
                continue
            name = self._dotted(node.func)
            first = node.args[0] if node.args else None
            if name in ("__import__", "importlib.import_module") and isinstance(first, ast.Constant) \
                    and first.value in self._DYNAMIC_MODULES:
                out.append(f"{rel}:{node.lineno} {name}({first.value!r})")
            if name == "getattr" and isinstance(first, ast.Name) and first.id in (
                    aliases["logging"] | aliases["sys"] | aliases["warnings"]):
                out.append(f"{rel}:{node.lineno} getattr({first.id}, ...)")
            if name == "open" and isinstance(first, ast.Constant) and first.value in (1, 2):
                out.append(f"{rel}:{node.lineno} open({first.value})")
            if name.partition(".")[0] in aliases["traceback"] and name.partition(".")[2].startswith("print_"):
                out.append(f"{rel}:{node.lineno} {name}()")
            if name.partition(".")[0] in aliases["builtins"] and name.partition(".")[2] == "print":
                out.append(f"{rel}:{node.lineno} builtins.print()")
            if name.endswith("basicConfig") or name == "faulthandler.enable":
                out.append(f"{rel}:{node.lineno} {name}()")
            if name.endswith("StreamHandler") and not node.args and not node.keywords:
                out.append(f"{rel}:{node.lineno} StreamHandler() (stderr)")
            head, _, rest = name.partition(".")
            if name == "print":
                out.append(f"{rel}:{node.lineno} print()")
            elif head in aliases["warnings"] and rest == "warn":
                out.append(f"{rel}:{node.lineno} warnings.warn()")
            elif head in aliases["logging"] and (rest in self._LEVELS or rest.startswith("root.")):
                out.append(f"{rel}:{node.lineno} {name}() (root logger)")
            elif head in aliases["sys"] and rest.split(".")[0] in ("stdout", "stderr", "__stdout__", "__stderr__"):
                out.append(f"{rel}:{node.lineno} {name}()")
            elif head in aliases["os"] and rest == "write":
                out.append(f"{rel}:{node.lineno} os.write()")
            elif isinstance(node.func, ast.Attribute) and node.func.attr in self._LEVELS:
                base = node.func.value
                if isinstance(base, ast.Call) and self._dotted(base.func).endswith("getLogger"):
                    out.append(f"{rel}:{node.lineno} getLogger(...).{node.func.attr}()")
                if isinstance(base, ast.Attribute) and base.attr in ("parent", "root"):
                    out.append(f"{rel}:{node.lineno} .{base.attr}.{node.func.attr}()")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if "faulthandler" in mods:
                    out.append(f"{rel}:{node.lineno} import faulthandler")
            if isinstance(node, ast.Subscript) and self._dotted(node.value).partition(".")[2] == "modules" \
                    and self._dotted(node.value).partition(".")[0] in aliases["sys"]:
                out.append(f"{rel}:{node.lineno} sys.modules[...]")  # membership tests stay allowed
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/dev/std"):
                out.append(f"{rel}:{node.lineno} {node.value!r}")
            if isinstance(node, ast.Attribute):
                name = self._dotted(node)
                head, _, rest = name.partition(".")
                if head in aliases["warnings"] and rest == "warn":
                    out.append(f"{rel}:{node.lineno} warnings.warn (reference)")
                if node.attr == "root" and head not in aliases["logging"]:
                    out.append(f"{rel}:{node.lineno} .root")
        return out

    # The console fallback for APPLICATION logs (design D6, P10) must write to stdout
    # and replace logging.lastResort; it routes helper/SDK diagnostics back to the
    # stdlib lastResort (tests/test_logging_a5.py). The only exempt module.
    _EXEMPT = {Path("_console.py")}

    def test_no_forbidden_channels(self):
        offenders = [o for rel, tree in self._trees() if rel not in self._EXEMPT
                     for o in self._offenders(rel, tree)]
        assert offenders == []

    def test_exempt_module_is_only_the_console_fallback(self):
        trees = dict(self._trees())
        assert self._EXEMPT <= set(trees)
        assert self._offenders(Path("_console.py"), trees[Path("_console.py")])  # really exempt, not idle

    @pytest.mark.parametrize("snippet", [
        "import warnings as w\nw.warn('x')",
        "from warnings import warn",
        "from logging import warning",
        "import logging\nlogging.root.warning('x')",
        "import logging\nlogging.getLogger().warning('x')",
        "from sys import stderr",
        "import sys\nsys.__stderr__.write('x')",
        "import os\nos.write(2, b'x')",
        "import logging\nlogging.getLogger('staffops_otel_helper').parent.warning('x')",
        "print('x')",
        "from os import write",
        "import traceback\ntraceback.print_exc()",
        "from traceback import print_stack",
        "import builtins\nbuiltins.print('x')",
        "import logging\ngetattr(logging, 'warning')('x')",
        "import importlib\nimportlib.import_module('warnings')",
        "__import__('sys')",
        "open(2, 'w')",
        "import sys\nerr = sys.stderr",
        "import logging\nh = logging.lastResort",
        "import logging\nr = logging.root\nr.warning('x')",
        "import logging\nlogging.basicConfig()",
        "import logging\nh = logging.StreamHandler()",
        "import faulthandler",
        "f = open('/dev/stderr', 'w')",
        "import sys\nm = sys.modules['warnings']",
        "import warnings\nw = warnings.warn",
        "x = lg.root",
    ])
    def test_scan_catches(self, snippet):
        assert self._offenders("snippet.py", ast.parse(snippet))

    def _logger_offenders(self, rel, tree):
        bad = []
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and self._dotted(node.func).endswith("getLogger")):
                continue
            arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "name"), None)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("staffops_otel_helper"):
                continue
            if isinstance(arg, ast.Name) and arg.id == "__name__":
                continue
            parent = parents.get(node)
            # Root/third-party loggers: only level/handler tuning (and reading the level
            # back to restore it on shutdown), only in logging_setup.py.
            tuning = (str(rel) in ("logging_setup.py", "_console.py") and isinstance(parent, ast.Attribute)
                      and parent.attr in ("setLevel", "level", "addHandler", "removeHandler", "handlers"))
            if not tuning:
                bad.append(f"{rel}:{node.lineno} getLogger({ast.unparse(arg) if arg else ''})")
        return bad

    def test_loggers_are_in_the_otel_helper_hierarchy(self):
        """Named loggers are staffops_otel_helper.*; root/third-party loggers only tuned (setLevel/addHandler) in logging_setup."""
        bad = [b for rel, tree in self._trees() for b in self._logger_offenders(rel, tree)]
        assert bad == []

    @pytest.mark.parametrize("rel,snippet", [
        ("config.py", "import logging\nx = logging.getLogger()"),
        ("config.py", "import logging\nx = logging.getLogger(name='app')"),
        ("logging_setup.py", "import logging\nlogging.getLogger('app').info('x')"),
    ])
    def test_logger_scan_catches(self, rel, snippet):
        assert self._logger_offenders(rel, ast.parse(snippet))

    def test_package_name_is_otel_helper(self):
        # getLogger(__name__) only lands in the staffops_otel_helper hierarchy for this package name.
        import staffops_otel_helper.metrics as m

        assert m._logger.name.startswith("staffops_otel_helper")


class TestFinalRound:
    """A3 final round: opaque schemes, trim set, sdk >= 1.45 fork assumption."""

    @pytest.mark.parametrize("raw", ["unix:/tmp/x", "ftp:/x", "collector:/v1/traces"])
    def test_opaque_scheme_env_not_configured(self, raw, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = raw
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            opts = TelemetryOptions(service_name="s")
            opts.resolve_from_env()
        assert not opts.is_otlp_configured("traces")
        assert "unsupported scheme" in caplog.text

    def test_opaque_scheme_per_signal_not_overridden_by_generic(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://good:4317"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "unix:/tmp/x"
        opts = TelemetryOptions(service_name="s")
        opts.resolve_from_env()
        assert not opts.is_otlp_configured("traces") and opts.is_otlp_configured("metrics")

    def test_opaque_scheme_code_raises(self):
        with pytest.raises(ValueError, match="otel_endpoint"):
            _plan(otel_endpoint="unix:/tmp/x")

    def test_schemeless_host_port_still_supported(self):
        assert _plan(otel_endpoint="collector:4317").kwargs["endpoint"] == "https://collector:4317"

    def test_trim_set_is_ascii_only(self):
        from staffops_otel_helper.config import TRIM_CHARS, _env

        assert TRIM_CHARS == " \t\n\r\v\f"
        os.environ["OTEL_HELPER_METRICS_PORT"] = "\v 9000 \f"
        assert _env("OTEL_HELPER_METRICS_PORT") == "9000"
        os.environ["OTEL_HELPER_METRICS_PORT"] = "\x1c9000"
        assert _env("OTEL_HELPER_METRICS_PORT") == "\x1c9000"  # str.strip() would drop \x1c

    def test_unicode_space_in_endpoint_not_trimmed(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://c:4317 "
        opts = TelemetryOptions(service_name="s")
        opts.resolve_from_env()
        plan = opts.otlp_export("traces")
        assert plan is None or plan.kwargs.get("endpoint") != "http://c:4317"

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
    def test_sdk_145_regenerates_instance_id_after_fork(self):
        """Pins the D4 assumption: on sdk >= 1.45 a worker forked after setup gets its own id."""
        import json
        import subprocess
        import sys
        from importlib.metadata import version

        if tuple(int(p) for p in version("opentelemetry-sdk").split(".")[:2]) < (1, 45):
            pytest.skip("sdk < 1.45 has no fork-aware instance id detector")
        code = (
            "import os, json\n"
            "from staffops_otel_helper import setup_telemetry\n"
            "from opentelemetry import trace\n"
            "setup_telemetry()\n"
            "def iid():\n"
            "    s = trace.get_tracer('t').start_span('s'); v = s.resource.attributes['service.instance.id']; s.end(); return v\n"
            "r, w = os.pipe()\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    os.write(w, iid().encode()); os._exit(0)\n"
            "os.waitpid(pid, 0); os.close(w)\n"
            "print(json.dumps({'parent': iid(), 'child': os.read(r, 100).decode()}), flush=True)\n"
            "os._exit(0)\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
        env.update(OTEL_SERVICE_NAME="s", OTEL_HELPER_METRICS_PORT="0")
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
        ids = json.loads(out.stdout.strip().splitlines()[-1])
        assert ids["child"] and ids["child"] != ids["parent"]
