"""Identity and resource rulings of 2026-10-29 (task A16, design D1 rows).

Dedicated option beats the code attribute; blank identity values are not
provided; a code-only ``service.name`` resolves silently; the helper parses
``OTEL_RESOURCE_ATTRIBUTES`` itself and reports problems by item position only;
``telemetry.sdk.*`` come from the SDK; the schema URL is the contract literal.
"""

from __future__ import annotations

import logging
import os
import re

import pytest

from staffops_otel_helper.config import TelemetryOptions, parse_resource_attributes
from staffops_otel_helper import setup as setup_mod
from staffops_otel_helper.setup import SCHEMA_URL, _build_resource

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
UNKNOWN_RE = re.compile(r"^unknown_service(:.+)?$")


def _resource(**kwargs):
    opts = TelemetryOptions(**kwargs)
    opts.resolve_from_env()
    return opts, dict(_build_resource(opts).attributes)


@pytest.fixture
def helper_log(caplog):
    caplog.set_level(logging.DEBUG, logger="staffops_otel_helper")
    return caplog


class TestParse:
    @pytest.mark.parametrize("raw, attrs, malformed, undecodable", [
        ("a=1,b=2", {"a": "1", "b": "2"}, (), ()),
        (" ,a=1,,\t,", {"a": "1"}, (), ()),
        ("a=b=c", {"a": "b=c"}, (), ()),
        ("a=1,broken,b=2", {"a": "1", "b": "2"}, (2,), ()),
        ("=orphan, \t=x,a=1", {"a": "1"}, (1, 2), ()),
        (" a \v= 1\f", {"a": "1"}, (), ()),
        ("a=%20payments%09", {"a": "payments"}, (), ()),
        ("a=x+y", {"a": "x+y"}, (), ()),
        ("a=caf%C3%A9", {"a": "café"}, (), ()),
        ("a=%zz", {"a": "%zz"}, (), (1,)),
        ("a= %zz ", {"a": "%zz"}, (), (1,)),
        ("a=%4", {"a": "%4"}, (), (1,)),
        ("x=1,a=%C3", {"x": "1", "a": "%C3"}, (), (2,)),
        ("a=\udcff", {"a": "�"}, (), (1,)),  # raw non-UTF-8 byte, surrogate-escaped by os.environ
        ("a= x", {"a": " x"}, (), ()),  # Unicode space is not in the contract trim set
        ("a=1,a=2", {"a": "2"}, (), ()),
        ("k\udcff=1,a=1", {"a": "1"}, (1,), ()),  # a key that is not UTF-8 is malformed
        ("a=\udcff\udcfe", {"a": "\ufffd\ufffd"}, (), (1,)),  # one replacement per invalid byte
        ("a=", {"a": ""}, (), ()),
    ])
    def test_rules(self, raw, attrs, malformed, undecodable):
        parsed = parse_resource_attributes(raw)
        assert (parsed.attributes, parsed.malformed, parsed.undecodable) == (attrs, malformed, undecodable)


class TestEnvParseDiagnostics:
    def test_positional_warnings_never_echo_values(self, helper_log, capsys):
        os.environ["OTEL_SERVICE_NAME"] = "svc"
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = (
            "token-s3cr3t-value,api.key=%zzs3cr3t,=s3cr3t-orphan,team.name=payments,x=%C3s3cr3t")
        _, attrs = _resource()
        assert attrs["team.name"] == "payments" and attrs["api.key"] == "%zzs3cr3t"
        assert "token-s3cr3t-value" not in attrs
        out = capsys.readouterr()
        assert "s3cr3t" not in helper_log.text + out.out + out.err
        assert "items 1,3 are not key=value" in helper_log.text
        assert "items 2,5 have an invalid percent-encoding" in helper_log.text
        assert helper_log.text.count("OTEL_RESOURCE_ATTRIBUTES:") == 2

    def test_sdk_detector_never_runs(self, monkeypatch):
        from opentelemetry.sdk import resources

        def boom(self):  # pragma: no cover - must not be called
            raise AssertionError("OTELResourceDetector ran")

        monkeypatch.setattr(resources.OTELResourceDetector, "detect", boom)
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "team.name=payments"
        _, attrs = _resource(service_name="svc")
        assert attrs["team.name"] == "payments"

    def test_clean_input_is_silent(self, helper_log):
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = " , team.name=%20payments%09,deployment.environment.name=DEV,,"
        _, attrs = _resource(service_name="svc")
        assert attrs["team.name"] == "payments"
        assert "OTEL_RESOURCE_ATTRIBUTES" not in helper_log.text

    def test_warned_once_per_process(self, helper_log):
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "broken"
        for _ in range(2):
            opts = TelemetryOptions(service_name="svc")
            opts.resolve_from_env()
        assert helper_log.text.count("not key=value") == 1

    def test_identity_warnings_once_per_process(self, helper_log):
        for _ in range(2):
            _resource()
        assert helper_log.text.count("No service name configured") == 1
        assert helper_log.text.count("deployment.environment.name is not set") == 1


class TestIdentity:
    def test_option_beats_code_attribute(self):
        opts, attrs = _resource(service_name="svc-option", environment="PRD",
                                resource_attributes={"service.name": "svc-attribute",
                                                     "deployment.environment.name": "DEV"})
        assert (opts.service_name, attrs["service.name"]) == ("svc-option", "svc-option")
        assert attrs["deployment.environment.name"] == "PRD"

    def test_code_attribute_only_resolves_silently(self, helper_log):
        opts, attrs = _resource(resource_attributes={"service.name": " svc-code "})
        assert opts.service_name == "svc-code" and attrs["service.name"] == "svc-code"
        assert "No service name" not in helper_log.text

    @pytest.mark.parametrize("env, code", [
        ("service.name=", {}), ("service.name= \t", {}), ("", {"service.name": ""}), ("", {"service.name": " "}),
    ])
    def test_blank_service_name_is_not_provided(self, env, code, helper_log):
        if env:
            os.environ["OTEL_RESOURCE_ATTRIBUTES"] = env
        opts, attrs = _resource(resource_attributes=code)
        assert opts.service_name is None and UNKNOWN_RE.match(attrs["service.name"])
        assert "No service name" in helper_log.text

    def test_blank_code_service_name_falls_back_to_env(self):
        os.environ["OTEL_SERVICE_NAME"] = "svc-std"
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "service.name=from-ra"
        _, attrs = _resource(resource_attributes={"service.name": " "})
        assert attrs["service.name"] == "svc-std"

    def test_otel_service_name_beats_resource_attributes(self):
        os.environ["OTEL_SERVICE_NAME"] = " svc-std\t"
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "service.name=from-ra"
        _, attrs = _resource()
        assert attrs["service.name"] == "svc-std"

    def test_default_service_name_has_the_interpreter(self, monkeypatch):
        monkeypatch.setattr(setup_mod.sys, "executable", "/usr/bin/python3.13")
        assert setup_mod._default_service_name() == "unknown_service:python3.13"
        monkeypatch.setattr(setup_mod.sys, "executable", "")
        assert setup_mod._default_service_name() == "unknown_service"

    def test_blank_env_environment_is_absent(self, helper_log):
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "deployment.environment.name= "
        opts, attrs = _resource(service_name="svc")
        assert "deployment.environment.name" not in attrs and opts.environment is None
        assert "deployment.environment.name is not set" in helper_log.text

    def test_blank_code_environment_falls_back(self):
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "deployment.environment.name=hml"
        opts, attrs = _resource(service_name="svc", resource_attributes={"deployment.environment.name": ""})
        assert attrs["deployment.environment.name"] == "hml"  # emitted verbatim
        assert opts.environment.value == "HML"  # behavior follows the known value

    @pytest.mark.parametrize("key", ["service.name", "deployment.environment.name", "service.instance.id"])
    def test_non_string_code_identity_fails_fast(self, key):
        with pytest.raises(ValueError, match=re.escape(f"resource_attributes[{key!r}] must be a string")):
            _resource(service_name="svc", resource_attributes={key: 5})

    @pytest.mark.parametrize("attrs, message", [
        ({"": "x"}, "has an empty key"), ({" \t": "x"}, "has an empty key"),
        ({1: "x"}, "keys must be strings"),
    ])
    def test_bad_code_keys_fail_fast(self, attrs, message):
        with pytest.raises(ValueError, match=message):
            _resource(service_name="svc", resource_attributes=attrs)

    def test_resource_attributes_must_be_a_dict(self):
        with pytest.raises(ValueError, match="must be a dict"):
            _resource(service_name="svc", resource_attributes=[("a", "b")])

    def test_non_identity_values_are_not_type_checked(self):
        _, attrs = _resource(service_name="svc", resource_attributes={"replicas": 3})
        assert attrs["replicas"] == 3

    @pytest.mark.parametrize("env, code", [
        ("service.instance.id=", {}), ("service.instance.id= ", {}), ("", {"service.instance.id": ""}),
    ])
    def test_blank_instance_id_is_generated(self, env, code):
        if env:
            os.environ["OTEL_RESOURCE_ATTRIBUTES"] = env
        _, attrs = _resource(service_name="svc", resource_attributes=code)
        assert UUID_RE.match(attrs["service.instance.id"])

    def test_instance_id_falls_back_to_helper_uuid_without_sdk_detector(self, monkeypatch):
        monkeypatch.setattr(setup_mod, "_sdk_instance_id", lambda: None)
        _, attrs = _resource(service_name="svc")
        assert UUID_RE.match(attrs["service.instance.id"]) and setup_mod._helper_instance_id is True

    def test_instance_id_uses_sdk_detector_when_available(self, monkeypatch):
        monkeypatch.setattr(setup_mod, "_sdk_instance_id", lambda: "sdk-id")
        _, attrs = _resource(service_name="svc")
        assert attrs["service.instance.id"] == "sdk-id" and setup_mod._helper_instance_id is False

    def test_sdk_instance_id_matches_the_installed_sdk(self):
        try:
            from opentelemetry.sdk.resources import ServiceInstanceIdResourceDetector  # noqa: F401
        except ImportError:
            assert setup_mod._sdk_instance_id() is None
        else:
            assert UUID_RE.match(setup_mod._sdk_instance_id())


class TestTelemetrySdkAndSchema:
    def test_sdk_values_win_and_schema_is_the_contract_literal(self, helper_log):
        from opentelemetry.sdk.version import __version__

        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = (
            "telemetry.sdk.name=env,telemetry.sdk.language=env,telemetry.sdk.version=0,telemetry.distro.name=d")
        opts = TelemetryOptions(service_name="svc", resource_attributes={"telemetry.sdk.version": "code"})
        opts.resolve_from_env()
        resource = _build_resource(opts)
        attrs = resource.attributes
        assert (attrs["telemetry.sdk.name"], attrs["telemetry.sdk.language"], attrs["telemetry.sdk.version"]) == (
            "opentelemetry", "python", __version__)
        assert attrs["telemetry.distro.name"] == "d"
        assert resource.schema_url == SCHEMA_URL == "https://opentelemetry.io/schemas/1.43.0"
        # Code values warn once, naming the key only; env values are silent.
        lines = [r.getMessage() for r in helper_log.records if "the OpenTelemetry SDK sets" in r.getMessage()]
        assert lines == ["[OtelHelper] resource_attributes['telemetry.sdk.version'] is ignored: "
                         "the OpenTelemetry SDK sets telemetry.sdk.version."]
        _build_resource(opts)  # once per process
        assert sum("the OpenTelemetry SDK sets" in r.getMessage() for r in helper_log.records) == 1

    def test_schema_url_matches_semconv_package_when_it_has_1_43(self):
        schemas = pytest.importorskip("opentelemetry.semconv.schemas")
        if not hasattr(schemas.Schemas, "V1_43_0"):
            pytest.skip("semconv package predates 1.43.0 (SDK 1.42-1.44): the literal is newer, by design")
        assert schemas.Schemas.V1_43_0.value == SCHEMA_URL


class TestResourceDetectorsVariable:
    @pytest.fixture(autouse=True)
    def _fresh_once(self):
        from staffops_otel_helper import _diag

        _diag._reset()

    def test_extra_detectors_warned_once(self, helper_log):
        os.environ["OTEL_EXPERIMENTAL_RESOURCE_DETECTORS"] = "otel,process"
        _resource(service_name="svc")
        _resource(service_name="svc")
        assert helper_log.text.count("OTEL_EXPERIMENTAL_RESOURCE_DETECTORS is ignored") == 1

    @pytest.mark.parametrize("value", ["otel", "otel, service_instance", " "])
    def test_builtin_names_silent(self, value, helper_log):
        os.environ["OTEL_EXPERIMENTAL_RESOURCE_DETECTORS"] = value
        _resource(service_name="svc")
        assert "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS" not in helper_log.text


class TestSdkNeverParses:
    """The SDK's env detector must not run (and echo) in a process set up by the helper."""

    def test_fresh_process_never_echoes(self):
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
        env.update(OTEL_SERVICE_NAME="svc", OTEL_HELPER_METRICS_PORT="0",
                   OTEL_RESOURCE_ATTRIBUTES="token-s3cr3t-value,api.key=%zzs3cr3t,deployment.environment.name=DEV")
        code = ("import staffops_otel_helper as h; o = h.setup_telemetry(); "
                "from opentelemetry import trace; "
                "print(dict(trace.get_tracer_provider().resource.attributes)['api.key']); h.shutdown_telemetry()")
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert "%zzs3cr3t" in out.stdout.splitlines()
        assert "token-s3cr3t-value" not in out.stdout + out.stderr
        assert "items 1 are not key=value" in out.stdout + out.stderr

    def test_filter_drops_only_the_invalid_pair_record(self, caplog):
        from staffops_otel_helper._resource_echo import suppress_sdk_resource_echo

        sdk_log = logging.getLogger("opentelemetry.sdk.resources")
        caplog.set_level(logging.WARNING, logger="opentelemetry.sdk.resources")
        with suppress_sdk_resource_echo():
            with suppress_sdk_resource_echo():  # re-entrant
                sdk_log.warning("Invalid key value resource attribute pair %s: %s", "s3cr3t", "x")
            sdk_log.warning("Failed to merge resources")
        sdk_log.warning("Invalid key value resource attribute pair %s: %s", "outside", "x")
        assert "s3cr3t" not in caplog.text
        assert "Failed to merge resources" in caplog.text and "outside" in caplog.text
        assert sdk_log.filters == []

    def test_environment_is_never_mutated(self, monkeypatch):
        from staffops_otel_helper import _resource_echo

        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "bad"
        seen = []
        monkeypatch.setattr(_resource_echo.importlib, "import_module",
                            lambda name: seen.append(os.environ.get("OTEL_RESOURCE_ATTRIBUTES")))
        _resource_echo.import_sdk_logs_quietly()
        assert seen == ["bad"]


class TestForkRestore:
    """sdk >= 1.45: the SDK's after-fork refresh must not replace the helper's resource (design D4)."""

    def _provider(self, resource):
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider(resource=resource)
        if not hasattr(provider, "_handle_fork"):
            pytest.skip("SDK < 1.45 has no after-fork resource refresh")
        return provider

    def test_explicit_id_restored_after_sdk_refresh(self):
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "service.instance.id=explicit-1"
        opts, _ = _resource(service_name="svc")
        resource = _build_resource(opts)
        provider = self._provider(resource)
        provider._handle_fork()  # what the SDK does in the child
        assert provider.resource.attributes["service.instance.id"] != "explicit-1"  # the SDK overwrote it
        setup_mod._restore_resource([provider, None], resource, explicit_id=True)
        assert provider.resource.attributes["service.instance.id"] == "explicit-1"
        tracer = provider.get_tracer("t")
        assert tracer.resource.attributes["service.instance.id"] == "explicit-1"

    def test_generated_id_takes_the_fresh_sdk_id_and_drops_detector_keys(self):
        from opentelemetry.sdk.resources import Resource

        opts, _ = _resource(service_name="svc")
        resource = _build_resource(opts)
        provider = self._provider(resource)
        provider._update_resource(Resource({"process.pid": 1}))  # a detector attribute added after fork
        setup_mod._restore_resource([provider], resource, explicit_id=False)
        attrs = provider.resource.attributes
        assert "process.pid" not in attrs and attrs["service.instance.id"] == setup_mod._sdk_instance_id()
        assert provider.resource.schema_url == SCHEMA_URL

    def test_meter_and_logger_providers_restored(self):
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource

        resource = Resource({"service.name": "svc", "service.instance.id": "x"}, SCHEMA_URL)
        meters, logs = MeterProvider(resource=resource), LoggerProvider(resource=resource)
        if not hasattr(meters, "_handle_fork"):
            pytest.skip("SDK < 1.45 has no after-fork resource refresh")
        meters._update_resource(Resource({"service.instance.id": "sdk"}))
        logs._update_resource(Resource({"service.instance.id": "sdk"}))
        setup_mod._restore_resource([meters, logs], resource, explicit_id=True)
        assert meters._sdk_config.resource.attributes["service.instance.id"] == "x"
        assert logs._resource.attributes["service.instance.id"] == "x"
        meters.shutdown()

    def test_restore_never_raises(self):
        class Broken:
            _resource = None

            def _update_resource(self, resource):
                raise RuntimeError("boom")

        setup_mod._restore_resource([Broken(), object()], _build_resource(_resource(service_name="svc")[0]),
                                    explicit_id=True)

    def test_hook_registered_only_for_refreshing_providers(self, monkeypatch):
        registered = []

        def register(**kw):
            if "_register_fork_restore" in kw["after_in_child"].__qualname__:
                registered.append(kw["after_in_child"])

        monkeypatch.setattr(setup_mod.os, "register_at_fork", register)
        pipeline = setup_mod._Pipeline()
        resource = _build_resource(_resource(service_name="svc")[0])
        setup_mod._register_fork_restore(pipeline, resource, explicit_id=False)
        assert registered == []
        from opentelemetry.sdk.trace import TracerProvider

        pipeline.tracer_provider = TracerProvider(resource=resource)
        setup_mod._register_fork_restore(pipeline, resource, explicit_id=False)
        if not hasattr(pipeline.tracer_provider, "_handle_fork"):
            assert registered == []
            return
        assert len(registered) == 1
        registered[0]()  # restores without error
        assert pipeline.tracer_provider.resource.attributes["service.name"] == "svc"
