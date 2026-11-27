"""Task A17: endpoint/TLS rulings of the D1 rows dated 2026-10-29 (RFC 3986 split,
ports, host characters, IDN hosts, constant rendering, '@' in the path, INSECURE
without an endpoint, empty code protocol/compression).
"""

import logging
import os

import pytest

from staffops_otel_helper.config import (
    UNPARSEABLE_URL,
    TelemetryOptions,
    _check_host,
    _endpoint_port,
    _rfc_split,
    redact_url,
)


def _plan(signal="traces", **kwargs):
    opts = TelemetryOptions(service_name="s", **kwargs)
    opts.resolve_from_env()
    opts.validate()
    return opts.otlp_export(signal)


class TestRfcSplit:
    @pytest.mark.parametrize("text,expect", [
        ("http://h:4317", ("http", False, "h", 4317, "", None, None)),
        ("HTTPS://u:p@w@h:04318/a%2Fb?q=1#f", ("https", True, "h", 4318, "/a%2Fb", "q=1", "f")),
        ("http://h:4317/?", ("http", False, "h", 4317, "/", "", None)),
        ("http://h#", ("http", False, "h", None, "", None, "")),
        ("http://h:", ("http", False, "h", None, "", None, None)),
        ("http://[::1]:24318/x", ("http", False, "[::1]", 24318, "/x", None, None)),
        ("http://[::1]", ("http", False, "[::1]", None, "", None, None)),
    ])
    def test_split(self, text, expect):
        sp = _rfc_split(text)
        assert (sp.scheme, sp.userinfo, sp.host, sp.port, sp.path, sp.query, sp.fragment) == expect

    @pytest.mark.parametrize("text", ["http://[::1", "http://[::1]x", "http://h:0", "http://h:65536",
                                      "http://h:1:2", "http://h:abc", "http://h:\u0661", "h:4317", "1http://h"])
    def test_malformed(self, text):
        assert _rfc_split(text) is None


class TestCheckHost:
    @pytest.mark.parametrize("host,expect", [
        ("Collector", ("collector", False)),
        ("[2001:DB8::1]", ("[2001:db8::1]", False)),
        ("b\u00fccher.de", ("xn--bcher-kva.de", False)),
        ("\ufffd.example", (None, False)),
        ("", (None, False)),
        ("coll ector", (None, False)),
        ("%63ollector", (None, False)),
        ("[fe80::1%eth0]", (None, True)),
        ("[1.2.3.4]", (None, False)),
        ("a<b", (None, False)),
        ("1.2.3.4", ("1.2.3.4", False)),
        ("host.1a", ("host.1a", False)),
        ("127.1", (None, False)),
        ("0x7f.0.0.1", (None, False)),
        ("h.0x", (None, False)),
        ("2130706433", (None, False)),
        ("1.2.3.256", (None, False)),
        ("1.2.3.4..", ("1.2.3.4..", False)),  # not "ends in a number" (WHATWG): a reg-name
        ("01.2.3.4", (None, False)),
        ("1.2.3.4.", ("1.2.3.4.", False)),
        ("10.0.0.1.", ("10.0.0.1.", False)),
        ("1.2.3.4..", ("1.2.3.4..", False)),  # not "ends in a number" (WHATWG): a reg-name
        ("\uff11\uff12\uff17.\uff11", (None, False)),  # fullwidth 127.1
        ("\uff12\uff11\uff13\uff10\uff17\uff10\uff16\uff14\uff13\uff13", (None, False)),  # fullwidth 2130706433
        ("b\u00fccher.de.", ("xn--bcher-kva.de.", False)),
        ("b\u00fc..de", (None, False)),
        (".".join(c * 62 for c in "abcd") + ".b\u00fc", (None, False)),  # > 253 after punycode
        ("a{b", (None, False)),
        ('"h"', (None, False)),
        ("h{", (None, False)),
        ("stra\u00dfe.de", ("xn--strae-oqa.de", False)),
        ("a\u200db.example", (None, False)),
    ])
    def test_check(self, host, expect):
        assert _check_host(host) == expect


class TestRedactUrl:
    @pytest.mark.parametrize("raw,expect", [
        ("http://:4317", UNPARSEABLE_URL),
        ("collector:abc", UNPARSEABLE_URL),
        ("http://h:70000", UNPARSEABLE_URL),
        ("http://h:0", UNPARSEABLE_URL),
        ("http://user:pw\\x@h:4317", UNPARSEABLE_URL),
        ("//h:4317", UNPARSEABLE_URL),
        ("", UNPARSEABLE_URL),
        ("http://h:04318/x", "http://h:4318/x"),
        ("http://b\u00fccher.de", "http://xn--bcher-kva.de"),
        ("http://user/parity-secret@collector", "http://"),
        ("http://admin:1234/rest@collector?x", "http://"),
        ("user:123/s@c", "<schemeless endpoint>"),
        ("ftp://user/s@x", "ftp://"),
        ("http://h:4318/%zz", "http://h:4318/%zz"),  # no protocol: no escape check
        ("http://h:4317/p?token=a@b", "http://"),
        ("unix:/a@b/c?q", "unix:"),
        ("Collector:/v1/traces", "collector:"),
        ("http://h:4317/a%40b", "http://"),
        ("x:@", UNPARSEABLE_URL),
        ("ftp://u:p@h:21/x", "ftp://h:21/x"),
        ("[::1]:4317/otlp", "[::1]:4317/otlp"),
    ])
    def test_redact(self, raw, expect):
        assert redact_url(raw) == expect

    def test_protocol_escape_check(self):
        assert redact_url("http://h:4318/%zz", "http/protobuf") == UNPARSEABLE_URL
        assert redact_url("http://h:4317/%zz", "grpc") == "http://h:4317/%zz"

    def test_long_ports(self):
        assert _rfc_split("http://h:" + "0" * 5000 + "4318").port == 4318
        assert _rfc_split("http://h:" + "9" * 5000) is None
        assert redact_url("http://h:" + "9" * 5000) == UNPARSEABLE_URL
        assert _endpoint_port("h:" + "0" * 5000 + "4318") == 4318
        assert _endpoint_port("h:" + "9" * 5000) is None

    def test_endpoint_port(self):
        assert _endpoint_port("h:04318") == 4318
        assert _endpoint_port("http://h:4318\x01") is None
        assert _endpoint_port("h:4318\\x") is None
        assert _endpoint_port("http://h:0") is None


class TestEnvEndpoints:
    @pytest.mark.parametrize("bad", ["http://collector:70000", "http://collector:0", "http://:4317",
                                     "http:///collector:4317", "//collector:4317", "https://h:4318/x\\y",
                                     "http://user:parity-secret\\pw@collector:4317", "http://%63ollector:4317",
                                     "http://coll ector:4317", "http://[fe80::1%eth0]:4317",
                                     "http://\ufffd.example:4317", "unix:/x\x7f", "http://127.1:4317",
                                     "http://1.2.3.256:4317", "http://a{b:4317", "http://a\u200db.example:4317"])
    def test_invalid_falls_back_with_constant(self, bad, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://good:4317"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = bad
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan()
        assert plan.kwargs["endpoint"] == "http://good:4317"
        assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=<unparseable URL>" in caplog.text
        assert "parity-secret" not in caplog.text and "unsupported" not in caplog.text

    def test_idn_host_punycoded_and_owned(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://b\u00fccher.de:4318"
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        assert _plan("logs").kwargs["endpoint"] == "http://xn--bcher-kva.de:4318/v1/logs"

    def test_leading_zero_port_owned(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector:04318"
        with caplog.at_level(logging.INFO, logger="staffops_otel_helper"):
            plan = _plan()
        assert plan.protocol == "http/protobuf"
        assert plan.kwargs["endpoint"] == "http://collector:4318/v1/traces"
        assert "inferred as http/protobuf" in caplog.text

    def test_invalid_path_escape_http_only(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://good:4318"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "http://h:4318/%zz"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan()
        assert plan.kwargs["endpoint"] == "http://good:4318/v1/traces"
        assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=<unparseable URL>" in caplog.text
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "http://h:4317/%zz"  # gRPC: the path is dropped
        assert _plan().kwargs.get("endpoint") is None
        with pytest.raises(ValueError, match="invalid percent-escape"):
            _plan(traces_endpoint="http://h:4318/%zz", otlp_protocol="http/protobuf")

    def test_strasse_non_transitional(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://stra\u00dfe.de"
        assert _plan().kwargs["endpoint"] == "http://xn--strae-oqa.de:4317"

    def test_at_percent_encoded_warned(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://h:4317/secret%40c"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            _plan()
        assert "misplaced credentials" in caplog.text and "secret" not in caplog.text

    def test_path_escape_verbatim(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector:4318/a%2Fb"
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        # Without a reason to own the URL the helper passes no endpoint: the SDK reads the env value as written.
        assert "endpoint" not in _plan().kwargs
        # An INSECURE/scheme contradiction makes the helper own the URL; the escape is still kept verbatim.
        os.environ["OTEL_EXPORTER_OTLP_INSECURE"] = "false"
        assert _plan().kwargs["endpoint"] == "http://collector:4318/a%2Fb/v1/traces"

    def test_at_in_path_never_renders_host_or_port(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://admin:1234/rest@collector?x"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            _plan()
        assert "misplaced credentials" in caplog.text
        assert "admin" not in caplog.text and "1234" not in caplog.text and "rest@" not in caplog.text

    @pytest.mark.parametrize("raw", ["http://collector:4317/?", "http://collector:4317#"])
    def test_empty_query_or_fragment_removed(self, raw, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = raw
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan()
        assert plan.kwargs["endpoint"] == "http://collector:4317"
        assert "query" in caplog.text

    def test_at_in_path_warned_name_only(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "http://user:4318/parity-secret@collector"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            plan = _plan()
        assert plan is not None
        assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: endpoint path contains '@'" in caplog.text
        assert "parity-secret" not in caplog.text

    @pytest.mark.parametrize("var", ["OTEL_EXPORTER_OTLP_INSECURE", "OTEL_EXPORTER_OTLP_LOGS_INSECURE"])
    def test_insecure_garbage_warns_without_endpoint(self, var, caplog):
        os.environ[var] = "yes"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            assert _plan() is None
        assert var in caplog.text

    def test_insecure_shadowed_by_code_is_silent(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_INSECURE"] = "yes"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            _plan(insecure=True)
        assert "OTEL_EXPORTER_OTLP_INSECURE" not in caplog.text


class TestCodeEndpoints:
    @pytest.mark.parametrize("bad,why", [
        ("unix:/x\x7f", "control character"),
        ("http://collector:70000", "not a valid endpoint"),
        ("http://:4317", "not a valid endpoint"),
        ("https://h:4318/x\\y", "backslash"),
        ("http://[fe80::1%eth0]:4317", "zone ids"),
    ])
    def test_invalid_fails_with_constant(self, bad, why):
        with pytest.raises(ValueError, match=why) as err:
            _plan(otel_endpoint=bad)
        assert "otel_endpoint '<unparseable URL>'" in str(err.value)

    def test_code_unsupported_scheme_shows_redacted_value(self):
        with pytest.raises(ValueError, match="unsupported scheme ftp://") as err:
            _plan(otel_endpoint="ftp://u:secret@h:21")
        assert "secret" not in str(err.value) and "ftp://h:21" in str(err.value)

    @pytest.mark.parametrize("option", ["otlp_protocol", "compression"])
    @pytest.mark.parametrize("value", ["", " \t"])
    def test_empty_protocol_and_compression_not_set(self, option, value):
        os.environ["OTEL_EXPORTER_OTLP_COMPRESSION"] = "none"
        plan = _plan(otel_endpoint="http://collector:4318", **{option: value})
        assert plan.protocol == "http/protobuf"  # port-4318 inference still applies
        assert plan.kwargs["compression"] == "none"  # env applies
