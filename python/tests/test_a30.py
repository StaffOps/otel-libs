"""A30 (2026-11-05): instrumentation parity backlog, Python side.

- gRPC span names (contract D1 "gRPC span names"): contrib grpc's
  ``/pkg.Svc/Method`` becomes the semconv ``pkg.Svc/Method``
  (``GrpcSpanNameProcessor``), as Go otelgrpc and the Node.js helper.
- Redis ``db.operation.name`` on command spans (D1 "Redis ``db.operation.name``").
- AWS attribute values (D1 "AWS service attribute values"): ``messaging.system``
  ``aws_sqs``, DynamoDB ``db.system.name=aws.dynamodb`` next to the legacy
  ``db.system`` unless the stable database mode is chosen.

URL query token names (A30) are covered in ``test_a28_url_redaction.py``.
"""

import json
import os
import subprocess
import sys

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from staffops_otel_helper import TelemetryOptions, setup_telemetry
from staffops_otel_helper.processors import GrpcSpanNameProcessor

# Fixtures and scripts of the A26/A27 suites (pytest puts tests/ on sys.path).
from test_a26_redis import _SCRIPT as REDIS_SCRIPT  # noqa: E402
from test_a26_redis import resp_fake  # noqa: E402,F401
from test_a27_aws import _SCRIPT as AWS_SCRIPT  # noqa: E402
from test_a27_aws import aws_fake  # noqa: E402,F401


def _provider():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(GrpcSpanNameProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_grpc_span_name_processor_renames_only_contrib_names():
    provider, exporter = _provider()
    tracer = provider.get_tracer("t")
    grpc_attrs = {"rpc.system": "grpc", "rpc.service": "parity.v1.Echo", "rpc.method": "Ping"}
    tracer.start_span("/parity.v1.Echo/Ping", kind=SpanKind.SERVER, attributes=grpc_attrs).end()
    tracer.start_span("/parity.v1.Echo/Ping", kind=SpanKind.CLIENT, attributes=grpc_attrs).end()
    # Not contrib's shape: kept.
    tracer.start_span("/parity.v1.Echo/Other", attributes=grpc_attrs).end()
    tracer.start_span("/parity.v1.Echo/Ping", attributes={**grpc_attrs, "rpc.system": "connect_rpc"}).end()
    tracer.start_span("/parity.v1.Echo/Ping").end()
    tracer.start_span("/x/y", attributes={"rpc.system": "grpc", "rpc.service": 1, "rpc.method": "y"}).end()
    assert [s.name for s in exporter.get_finished_spans()] == [
        "parity.v1.Echo/Ping", "parity.v1.Echo/Ping", "/parity.v1.Echo/Other", "/parity.v1.Echo/Ping",
        "/parity.v1.Echo/Ping", "/x/y"]


def test_grpc_span_name_processor_never_raises():
    class Broken:
        @property
        def attributes(self):
            raise RuntimeError("boom")

    p = GrpcSpanNameProcessor()
    p.on_start(Broken())
    p.on_end(None)
    p.shutdown()
    assert p.force_flush() is True


def test_grpc_spans_through_the_helper_have_semconv_names():
    from test_health_a7 import TestSyncGrpc

    spans = TestSyncGrpc()._exercise("/parity.v1.Echo/Ping")
    assert sorted((s.kind.name, s.name) for s in spans) == [
        ("CLIENT", "parity.v1.Echo/Ping"), ("SERVER", "parity.v1.Echo/Ping")]
    for s in spans:  # attributes unchanged (the legacy rpc.* keys)
        assert s.attributes["rpc.service"] == "parity.v1.Echo" and s.attributes["rpc.method"] == "Ping"


def test_setup_installs_the_grpc_span_name_processor():
    setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    trace.get_tracer("t").start_span(
        "/a.B/C", attributes={"rpc.system": "grpc", "rpc.service": "a.B", "rpc.method": "C"}).end()
    assert [s.name for s in exporter.get_finished_spans()] == ["a.B/C"]


def test_redis_operation_name_rule():
    from staffops_otel_helper._db_status import redis_operation_name

    assert redis_operation_name("SET") == "SET"
    assert redis_operation_name("CLUSTER INFO") == "CLUSTER INFO"
    assert redis_operation_name("redis.search") == "FT.SEARCH"
    assert redis_operation_name("redis.create_index") == "FT.CREATE"
    for none in ("", "0", "12", "redis", None):
        assert redis_operation_name(none) is None


def test_redis_wrapper_sets_operation_name_at_end():
    from staffops_otel_helper._db_status import _ErrorDescriptionDroppingTracerProvider

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = _ErrorDescriptionDroppingTracerProvider(provider, redis=True).get_tracer("t")
    with tracer.start_as_current_span("SET", kind=SpanKind.CLIENT):
        pass
    with tracer.start_as_current_span("SET GET", kind=SpanKind.CLIENT) as span:
        span.set_attribute("db.redis.pipeline_length", 2)
    with tracer.start_as_current_span("0", kind=SpanKind.CLIENT):
        pass
    with tracer.start_as_current_span("GET", kind=SpanKind.CLIENT) as span:
        span.set_attribute("db.operation.name", "own")
    tracer.start_span("DEL").end()
    sql = _ErrorDescriptionDroppingTracerProvider(provider).get_tracer("t")
    with sql.start_as_current_span("SELECT"):
        pass
    ops = {s.name: s.attributes.get("db.operation.name") for s in exporter.get_finished_spans()}
    assert ops == {"SET": "SET", "SET GET": None, "0": None, "GET": "own", "DEL": "DEL", "SELECT": None}


def _run_script(script, url, extra_env=None, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "AWS_"))}
    env["OTEL_HELPER_METRICS_PORT"] = "0"
    env.update(extra_env or {})
    out = subprocess.run([sys.executable, "-c", script, "ext", url, *args], env=env, capture_output=True,
                         text=True, check=True, timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_redis_spans_carry_the_operation_name(resp_fake):  # noqa: F811
    pytest.importorskip("redis")
    pytest.importorskip("opentelemetry.instrumentation.redis")
    spans = _run_script(REDIS_SCRIPT, resp_fake, None, "secret")
    commands = [s for s in spans if "db.redis.pipeline_length" not in s["attributes"]]
    pipelines = [s for s in spans if "db.redis.pipeline_length" in s["attributes"]]
    assert commands and pipelines
    for s in commands:
        assert s["attributes"].get("db.operation.name") == s["name"], s
    for s in pipelines:
        assert "db.operation.name" not in s["attributes"], s


@pytest.mark.parametrize("opt_in,legacy", [(None, True), ("http,database/dup", True), ("http,database", False)])
def test_aws_attribute_values(aws_fake, opt_in, legacy):  # noqa: F811
    pytest.importorskip("botocore")
    pytest.importorskip("opentelemetry.instrumentation.botocore")
    env = {} if opt_in is None else {"OTEL_SEMCONV_STABILITY_OPT_IN": opt_in}
    spans = _run_script(AWS_SCRIPT, aws_fake, env, "secret")
    ddb = [s for s in spans if s["name"].startswith("DynamoDB.")]
    assert ddb
    for s in ddb:
        assert s["attributes"].get("db.system.name") == "aws.dynamodb", s
        assert s["attributes"].get("db.system") == ("dynamodb" if legacy else None), s
    systems = {s["attributes"].get("messaging.system") for s in spans if "messaging.system" in s["attributes"]}
    assert systems == {"aws.sns", "aws_sqs"}, systems


def test_map_aws_attributes(monkeypatch):
    from staffops_otel_helper._aws_status import map_aws_attributes

    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    assert map_aws_attributes({"messaging.system": "aws.sqs", "error.type": "x",
                               "aws.dynamodb.item_collection_metrics": "k"}) == {"messaging.system": "aws_sqs"}
    assert map_aws_attributes({"db.system": "dynamodb"}) == {"db.system.name": "aws.dynamodb", "db.system": "dynamodb"}
    assert map_aws_attributes({"db.system": "dynamodb", "db.system.name": "own"}) == {
        "db.system.name": "own", "db.system": "dynamodb"}
    assert map_aws_attributes({"messaging.system": "aws.sns", "db.system": "other"}) == {
        "messaging.system": "aws.sns", "db.system": "other"}
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "DATABASE")
    assert map_aws_attributes({"db.system": "dynamodb"}) == {"db.system.name": "aws.dynamodb"}


def test_legacy_db_keys_enabled():
    from staffops_otel_helper._semconv import legacy_db_keys_enabled

    for value, want in [("", True), ("http", True), ("database", False), (" DATABASE ", False),
                        ("database,database/dup", True), ("http,database/dup", True)]:
        assert legacy_db_keys_enabled(value) is want, value


# --- Owner rulings of 2026-11-06 (Q2 RPC keys, Q3 span kind/name, Q6 port) ---

def _aws_spans(mode, **start):
    from staffops_otel_helper._aws_status import _AwsTracerProvider

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = _AwsTracerProvider(provider, mode).get_tracer("botocore")
    with tracer.start_as_current_span(**start):
        pass
    return exporter.get_finished_spans()[0]


_SNS = {"rpc.system": "aws-api", "rpc.service": "SNS", "rpc.method": "Publish", "messaging.system": "aws.sns"}


@pytest.mark.parametrize("mode,want,absent", [
    ("legacy", {"rpc.system": "aws-api", "rpc.service": "SNS", "rpc.method": "Publish"}, ["rpc.system.name"]),
    ("rpc", {"rpc.system.name": "aws-api", "rpc.method": "SNS/Publish"}, ["rpc.system", "rpc.service"]),
    ("rpc/dup", {"rpc.system.name": "aws-api", "rpc.system": "aws-api", "rpc.service": "SNS",
                 "rpc.method": "SNS/Publish"}, []),
])
def test_aws_call_span_kind_name_and_rpc_keys(mode, want, absent):
    span = _aws_spans(mode, name="alerts send", kind=SpanKind.CLIENT, attributes=_SNS)
    assert span.name == "SNS.Publish" and span.kind == SpanKind.PRODUCER  # owner amendment 2026-11-08
    assert span.attributes["messaging.operation.name"] == "Publish"
    for k, v in want.items():
        assert span.attributes[k] == v, k
    for k in absent:
        assert k not in span.attributes, k


def test_aws_call_span_leaves_other_spans():
    from staffops_otel_helper._aws_status import aws_call_span

    gen_ai = {"rpc.system": "aws-api", "rpc.service": "Bedrock Runtime", "rpc.method": "Converse",
              "gen_ai.operation.name": "chat"}
    name, kind, attrs = aws_call_span("chat m", SpanKind.CLIENT, gen_ai, "legacy")
    assert name == "chat m" and attrs["rpc.method"] == "Converse"
    assert aws_call_span("x", SpanKind.INTERNAL, {"rpc.system": "grpc"}, "rpc")[:2] == ("x", SpanKind.INTERNAL)
    assert aws_call_span("x", None, {"rpc.system": "aws-api", "rpc.service": 1}, "rpc")[0] == "x"


def test_rpc_mode():
    from staffops_otel_helper._aws_status import rpc_mode

    assert rpc_mode("") == "legacy"
    assert rpc_mode("http,database/dup") == "legacy"
    assert rpc_mode(" RPC ") == "rpc"
    assert rpc_mode("rpc,rpc/dup") == "rpc/dup"
    assert rpc_mode("rpc/old") == "legacy"


def test_server_attributes_port_follows_the_scheme():
    from staffops_otel_helper._aws_status import server_attributes

    assert server_attributes("http://localstack") == {"server.address": "localstack", "server.port": 80}
    assert server_attributes("HTTP://h") == {"server.address": "h", "server.port": 80}
    assert server_attributes("https://s3.amazonaws.com") == {"server.address": "s3.amazonaws.com", "server.port": 443}
    assert server_attributes("http://h:4566") == {"server.address": "h", "server.port": 4566}
    assert server_attributes("http://h:99999") == {"server.address": "h"}  # no server.port, as Go
    assert server_attributes("not a url") == {}


def test_contrib_server_attributes_is_patched():
    pytest.importorskip("opentelemetry.instrumentation.botocore")
    import opentelemetry.instrumentation.botocore as contrib

    from staffops_otel_helper._aws_status import aws_instrument_kwargs, server_attributes

    aws_instrument_kwargs()
    assert contrib.get_server_attributes is server_attributes
    aws_instrument_kwargs()  # idempotent
    assert contrib.get_server_attributes is server_attributes


@pytest.mark.parametrize("opt_in,method", [(None, "Publish"), ("rpc", "SNS/Publish"), ("rpc/dup", "SNS/Publish")])
def test_aws_rpc_modes_through_botocore(aws_fake, opt_in, method):  # noqa: F811
    pytest.importorskip("botocore")
    pytest.importorskip("opentelemetry.instrumentation.botocore")
    env = {} if opt_in is None else {"OTEL_SEMCONV_STABILITY_OPT_IN": opt_in}
    spans = _run_script(AWS_SCRIPT, aws_fake, env, "secret")
    publish = [s for s in spans if s["name"] == "SNS.Publish"]
    assert publish, [s["name"] for s in spans]
    assert not [s for s in spans if s["name"].endswith(" send")]
    for s in publish:
        assert s["attributes"]["rpc.method"] == method
        assert s["attributes"]["messaging.operation.name"] == "Publish"
        assert ("rpc.service" in s["attributes"]) is (opt_in != "rpc")


# --- Owner ruling Q1 (b), 2026-11-06: HTTP opt-in metrics behind one switch ---

def _scrape_after(options, env=None):
    from opentelemetry import metrics
    from starlette.testclient import TestClient

    from staffops_otel_helper import metrics_app

    for k, v in (env or {}).items():
        os.environ[k] = v
    setup_telemetry(options)
    meter = metrics.get_meter("opentelemetry.instrumentation.asgi")
    meter.create_histogram("http.server.request.duration", unit="s").record(0.1)
    meter.create_up_down_counter("http.server.active_requests", unit="{request}").add(1)
    meter.create_histogram("http.server.response.body.size", unit="By").record(10)
    meter.create_histogram("http.client.request.size", unit="By").record(10)  # old-generation name
    # An application's own instrument of an opt-in name is kept (scoped view, 2026-11-08).
    metrics.get_meter("my.app").create_up_down_counter("http.client.active_requests").add(1)
    return options.resolved.http_opt_in_metrics, TestClient(metrics_app()).get("/").text


@pytest.mark.parametrize("env,code,on", [
    (None, None, False),
    ("TRUE", None, True),
    ("false", None, False),
    ("true", False, False),
    (None, True, True),
])
def test_http_opt_in_metrics_switch(env, code, on, monkeypatch):
    monkeypatch.delenv("OTEL_HELPER_HTTP_OPT_IN_METRICS", raising=False)
    opts = TelemetryOptions(service_name="a30", metric_exporters=["prometheus"], prometheus_metrics_port=0,
                            http_opt_in_metrics=code)
    resolved, text = _scrape_after(opts, None if env is None else {"OTEL_HELPER_HTTP_OPT_IN_METRICS": env})
    assert resolved is on
    assert "http_server_request_duration_seconds" in text
    assert "http_client_active_requests" in text  # the application's own instrument
    for name in ("http_server_active_requests", "http_server_response_body_size_bytes", "http_client_request_size"):
        assert (name in text) is on, (name, text[:400])


def test_http_opt_in_metrics_invalid_env_warns(monkeypatch, caplog):
    monkeypatch.setenv("OTEL_HELPER_HTTP_OPT_IN_METRICS", "yes")
    opts = TelemetryOptions(service_name="a30", metric_exporters=["none"])
    setup_telemetry(opts)
    assert opts.resolved.http_opt_in_metrics is False
    assert "OTEL_HELPER_HTTP_OPT_IN_METRICS" in caplog.text


def test_http_opt_in_metrics_code_must_be_bool():
    with pytest.raises((TypeError, ValueError), match="http_opt_in_metrics"):
        setup_telemetry(TelemetryOptions(service_name="a30", http_opt_in_metrics="true"))  # type: ignore[arg-type]


@pytest.mark.parametrize("service,method,kind", [
    ("SQS", "SendMessage", SpanKind.PRODUCER), ("SQS", "SendMessageBatch", SpanKind.PRODUCER),
    ("SQS", "ReceiveMessage", SpanKind.CONSUMER), ("SNS", "Publish", SpanKind.PRODUCER),
    ("SNS", "PublishBatch", SpanKind.PRODUCER), ("SQS", "DeleteMessage", SpanKind.CLIENT),
    ("SNS", "CreateTopic", SpanKind.CLIENT), ("Lambda", "Invoke", SpanKind.CLIENT), ("S3", "PutObject", SpanKind.CLIENT),
])
def test_aws_messaging_kinds(service, method, kind):  # owner amendment 2026-11-08
    from staffops_otel_helper._aws_status import aws_call_span

    attrs = {"rpc.system": "aws-api", "rpc.service": service, "rpc.method": method}
    name, got, _ = aws_call_span("x send", SpanKind.CLIENT, attrs, "legacy")
    assert name == f"{service}.{method}" and got == kind


def test_sqs_messaging_operation_name():
    from staffops_otel_helper._aws_status import aws_call_span

    attrs = {"rpc.system": "aws-api", "rpc.service": "SQS", "rpc.method": "SendMessage", "messaging.system": "aws.sqs"}
    assert aws_call_span("q", None, attrs, "legacy")[2]["messaging.operation.name"] == "SendMessage"


def test_patch_warns_when_contrib_has_no_hook(monkeypatch, caplog):
    pytest.importorskip("opentelemetry.instrumentation.botocore")
    import opentelemetry.instrumentation.botocore as contrib

    from staffops_otel_helper import _aws_status

    monkeypatch.delattr(contrib, "get_server_attributes", raising=False)
    _aws_status._patch_server_attributes()
    assert "get_server_attributes" in caplog.text


def test_http_rpc_keeps_stable_http():
    # Code-review M1: `rpc` alone has no `http` token (contrib would use the old HTTP conventions); `http,rpc` keeps it.
    from staffops_otel_helper._semconv import ensure_semconv_default, normalize_semconv_opt_in

    assert normalize_semconv_opt_in("http,rpc") == "http,rpc,database/dup"
    assert "http" not in normalize_semconv_opt_in("rpc").split(",")
    os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "http,rpc/dup"
    ensure_semconv_default()
    tokens = os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"].split(",")
    assert "http" in tokens and "rpc/dup" in tokens
