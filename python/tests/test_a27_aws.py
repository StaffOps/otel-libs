"""A27 (2026-11-03): AWS error status, error.type and payload rules for ``ext.aws``.

The A25 DB error rule extended to AWS clients (contract design D1 "AWS error
status"): a failed botocore span keeps the ERROR code and drops the
description (the AWS error text can quote request values); ``error.type`` is
the AWS error code (``^[A-Z][A-Za-z0-9]{1,63}$``) or ``_OTHER``;
``aws.dynamodb.item_collection_metrics`` (the partition-key value) is never
recorded; an SNS phone number stays redacted (contrib's own rule). Both helper
paths — ``instrument_aws()`` and the deprecated ``AWS`` flag of
``setup_telemetry()`` — bind contrib botocore to the ``_aws_status`` wrapper.
Go ``ext/otelaws`` and Node ``@staffops/otel-helper-aws`` follow the same rule.

Server: an in-process fake AWS endpoint (S3 REST-XML, DynamoDB JSON 1.0, SNS
Query); fake credentials only.
"""

import json
import os
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

SECRET = "secret-quoted-item-value"
PARTITION_KEY = "secret-partition-key"
PHONE = "+15555550123"


class _AwsHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send(self, status, body, content_type):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("x-amzn-RequestId", "req-1")
        self.send_header("x-amz-request-id", "req-1")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _s3_error(self, status, code, message):
        self._send(status, f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code><Message>{message}</Message>'
                           f'<RequestId>req-1</RequestId></Error>', "application/xml")

    def do_HEAD(self):  # noqa: N802 - http.server API: body-less errors, as S3 HeadObject
        path = self.path.split("?", 1)[0]
        self.send_response(403 if path.startswith("/denied/") else 503 if path.endswith("/unavailable") else 404)
        self.send_header("x-amz-request-id", "req-1")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802 - http.server API
        bucket = self.path.split("?", 1)[0].split("/")[1]
        if bucket == "denied":
            return self._s3_error(403, "AccessDenied", "Access Denied")
        return self._s3_error(404, "NoSuchKey", "The specified key does not exist.")

    def do_POST(self):  # noqa: N802 - http.server API
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        target = self.headers.get("X-Amz-Target") or ""
        if target.startswith("AmazonSQS.") or "Action=SendMessage" in body:
            # SQS QueueDoesNotExist: JSON 1.0 with the query-compatible header (and, for an old
            # botocore on the Query protocol, the query error).
            if target:
                self.send_response(400)
                data = json.dumps({"__type": "com.amazonaws.sqs#QueueDoesNotExist",
                                   "message": "The specified queue does not exist."}).encode()
                self.send_header("Content-Type", "application/x-amz-json-1.0")
                self.send_header("x-amzn-query-error", "AWS.SimpleQueueService.NonExistentQueue;Sender")
                self.send_header("x-amzn-RequestId", "req-1")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return None
            return self._send(400, '<ErrorResponse><Error><Type>Sender</Type><Code>AWS.SimpleQueueService.NonExistentQueue'
                                   '</Code><Message>The specified queue does not exist.</Message></Error>'
                                   '<RequestId>req-1</RequestId></ErrorResponse>', "text/xml")
        if target.startswith("DynamoDB_20120810."):
            item = json.loads(body or "{}")
            if item.get("TableName") == "Quoting":
                return self._send(400, json.dumps({
                    "__type": "com.amazonaws.dynamodb.v20120810#ValidationException",
                    "message": f"One or more parameter values were invalid: {json.dumps(item.get('Item'))}"}),
                    "application/x-amz-json-1.0")
            return self._send(200, json.dumps({
                "ConsumedCapacity": {"TableName": item.get("TableName"), "CapacityUnits": 1},
                "ItemCollectionMetrics": {"ItemCollectionKey": {"pk": {"S": PARTITION_KEY}}, "SizeEstimateRangeGB": [0, 1]},
            }), "application/x-amz-json-1.0")
        # SNS Publish (Query protocol)
        return self._send(200, '<PublishResponse xmlns="http://sns.amazonaws.com/doc/2010-03-31/"><PublishResult>'
                               '<MessageId>m-1</MessageId></PublishResult><ResponseMetadata><RequestId>req-1</RequestId>'
                               '</ResponseMetadata></PublishResponse>', "text/xml")


@pytest.fixture()
def aws_fake():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AwsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


_SCRIPT = textwrap.dedent("""
    import json, sys
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    mode, url = sys.argv[1], sys.argv[2]
    if mode == "foreign":
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
        BotocoreInstrumentor().instrument()
    from staffops_otel_helper import setup_telemetry, TelemetryOptions
    setup_telemetry(TelemetryOptions(service_name="svc", extra_instrumentation="AWS" if mode == "setup" else ""))
    ex = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(ex))
    if mode in ("ext", "foreign"):
        from staffops_otel_helper.ext.aws import instrument_aws
        assert instrument_aws()
        assert instrument_aws(), "a second call is a no-op that reports the instrumentation active"
    import botocore.session
    from botocore.config import Config
    session = botocore.session.get_session()
    def client(service):
        return session.create_client(service, region_name="us-east-1", endpoint_url=url,
                                     aws_access_key_id="AKIAEXAMPLEFAKEKEY01", aws_secret_access_key="fake-secret-example",
                                     config=Config(retries={"total_max_attempts": 1}, s3={"addressing_style": "path"}))
    s3, ddb, sns, sqs = client("s3"), client("dynamodb"), client("sns"), client("sqs")
    calls = [
        lambda: s3.get_object(Bucket="bucket-1", Key="missing"),
        lambda: s3.get_object(Bucket="denied", Key="k"),
        lambda: ddb.put_item(TableName="Quoting", Item={"pk": {"S": sys.argv[3]}}),
        lambda: ddb.put_item(TableName="Orders", Item={"pk": {"S": "k"}}, ReturnItemCollectionMetrics="SIZE"),
        lambda: sns.publish(PhoneNumber="+15555550123", Message="m"),
        lambda: s3.head_object(Bucket="bucket-1", Key="k"),
        lambda: s3.head_object(Bucket="denied", Key="k"),
        lambda: s3.head_object(Bucket="bucket-1", Key="unavailable"),
        lambda: sqs.send_message(QueueUrl=url + "/123456789012/missing", MessageBody="m"),
    ]
    for call in calls:
        try:
            call()
        except Exception:
            pass
    out = []
    for s in ex.get_finished_spans():
        out.append({"name": s.name, "code": s.status.status_code.name, "description": s.status.description,
                    "attributes": {k: str(v) for k, v in s.attributes.items()},
                    "events": [dict(e.attributes) for e in s.events]})
    print(json.dumps(out))
""")


def _run(mode, url):
    pytest.importorskip("botocore")
    pytest.importorskip("opentelemetry.instrumentation.botocore")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "AWS_"))}
    env["OTEL_HELPER_METRICS_PORT"] = "0"
    out = subprocess.run([sys.executable, "-c", _SCRIPT, mode, url, SECRET], env=env, capture_output=True,
                         text=True, check=True, timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1]), out.stderr


@pytest.mark.parametrize("mode", ["ext", "setup"])
def test_failed_aws_calls_carry_no_description_and_the_error_code(mode, aws_fake):
    spans, stderr = _run(mode, aws_fake)
    by_name = {}
    for span in spans:
        by_name.setdefault(span["name"], []).append(span)
    missing, denied = by_name["S3.GetObject"]
    # error.type parity (A27 fix round): the modeled names, as Go and Node.
    heads = by_name["S3.HeadObject"]
    assert [h["attributes"].get("error.type") for h in heads] == ["NotFound", "Forbidden", "ServiceUnavailable"], heads
    assert all(h["code"] == "ERROR" for h in heads), heads
    [queue] = [s for s in spans if s["attributes"].get("rpc.service") == "SQS"]
    assert queue["code"] == "ERROR" and queue["attributes"]["error.type"] == "QueueDoesNotExist", queue
    quoting, orders = by_name["DynamoDB.PutItem"]
    for span, code in ((missing, "NoSuchKey"), (denied, "AccessDenied"), (quoting, "ValidationException")):
        assert span["code"] == "ERROR", span
        assert not span["description"], span  # was "<ClientError text>"
        assert span["attributes"]["error.type"] == code, span
        assert SECRET not in json.dumps([span["attributes"], span["description"]]), span
    # The accepted residual (contract D1 "DB error text in exception events"): the event keeps the AWS text.
    assert SECRET in quoting["events"][0]["exception.message"]
    assert orders["code"] == "UNSET" and "error.type" not in orders["attributes"], orders
    assert "aws.dynamodb.item_collection_metrics" not in orders["attributes"], orders
    assert "aws.dynamodb.consumed_capacity" in orders["attributes"], "a measurement: kept"
    [publish] = [s for s in spans if s["attributes"].get("rpc.service") == "SNS"]
    assert publish["attributes"]["messaging.destination.name"] == "phone_number:**", publish
    for span in spans:
        assert PARTITION_KEY not in json.dumps(span), span["name"]
        assert PHONE not in json.dumps(span), span["name"]
        assert "AKIAEXAMPLEFAKEKEY01" not in json.dumps(span), span["name"]
    assert "already instrumented without the helper" not in stderr


def test_instrumented_elsewhere_is_kept_and_warned(aws_fake):
    spans, stderr = _run("foreign", aws_fake)
    assert stderr.count("botocore is already instrumented without the helper's tracer") == 1, stderr
    assert any(s["description"] for s in spans if s["code"] == "ERROR"), "the foreign instrumentation keeps contrib's text"


def test_aws_error_type():
    from staffops_otel_helper._aws_status import aws_error_type

    class ClientError(Exception):
        def __init__(self, code):
            super().__init__("x")
            self.response = {"Error": {"Code": code}}

    class EndpointConnectionError(Exception):
        pass

    assert aws_error_type(ClientError("NoSuchKey")) == "NoSuchKey"
    assert aws_error_type(ClientError("AWS.SimpleQueueService.NonExistentQueue")) == "_OTHER", "a dotted query code"
    assert aws_error_type(ClientError("A" + "b" * 63)) == "A" + "b" * 63
    assert aws_error_type(ClientError("A" + "b" * 64)) == "_OTHER"
    assert aws_error_type(EndpointConnectionError()) == "EndpointConnectionError"
    assert aws_error_type(type("lower_case", (Exception,), {})()) == "_OTHER"
    bad = ClientError("x")
    bad.response = {"Error": "not a dict"}
    assert aws_error_type(bad) == "ClientError"
    bad.response = "not a dict"
    assert aws_error_type(bad) == "ClientError"
    assert aws_error_type(None) == "_OTHER"
    query = ClientError("AWS.SimpleQueueService.NonExistentQueue")
    query.response["Error"]["QueryErrorCode"] = "QueueDoesNotExist"
    assert aws_error_type(query) == "QueueDoesNotExist", "the query-compatible code"
    QueueDoesNotExist = type("QueueDoesNotExist", (ClientError,), {})
    assert aws_error_type(QueueDoesNotExist("AWS.SimpleQueueService.NonExistentQueue")) == "QueueDoesNotExist", "the modeled class"
    assert aws_error_type(ClientError("404")) == "NotFound"
    assert aws_error_type(ClientError("403")) == "Forbidden"
    assert aws_error_type(ClientError("503")) == "ServiceUnavailable"
    assert aws_error_type(ClientError("413")) == "RequestEntityTooLarge", "Go's StatusText, not Python's phrase"
    assert aws_error_type(ClientError("418")) == "_OTHER", "not in the table (I'm a teapot)"
    assert aws_error_type(ClientError("²")) == "_OTHER", "a non-ASCII digit is not a status code"
    assert aws_error_type(ClientError("599")) == "_OTHER", "no reason phrase"
    no_code = ClientError("x")
    no_code.response = {"Error": {}}
    assert aws_error_type(no_code) == "_OTHER"


def test_span_rules_without_botocore():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import Status, StatusCode

    from staffops_otel_helper._aws_status import _AwsTracerProvider, _sanitize_aws_span

    tracer = _AwsTracerProvider(TracerProvider()).get_tracer("t")
    span = tracer.start_span("s", attributes={"aws.dynamodb.item_collection_metrics": "k", "kept": 1})
    span.set_attribute("aws.dynamodb.item_collection_metrics", "k")
    span.set_attributes({"aws.dynamodb.item_collection_metrics": "k", "also": 2})
    span.record_exception(ValueError("before any error status"))
    assert "error.type" not in span.attributes, "only a failed call gets error.type"
    ok = tracer.start_span("ok")
    ok.set_status(Status(StatusCode.OK))
    assert ok.status.status_code is StatusCode.OK and "error.type" not in ok.attributes, "other statuses pass through"
    span.set_status(StatusCode.ERROR, "secret text")
    assert span.status.status_code is StatusCode.ERROR and not span.status.description
    assert span.attributes["error.type"] == "_OTHER", "no exception being handled"
    inside = tracer.start_span("inside")
    try:
        raise KeyError("k")
    except KeyError:
        inside.set_status(StatusCode.ERROR, "x")
    assert inside.attributes["error.type"] == "KeyError", "the exception being handled names it"
    span.record_exception(ValueError("x"))
    assert span.attributes["error.type"] == "ValueError"
    span.set_attribute("error.type", "ThrottlingException_from_contrib")
    assert span.attributes["error.type"] == "ValueError", "contrib's own error.type writes are dropped (Bedrock)"
    assert dict(span.attributes) == {"kept": 1, "also": 2, "error.type": "ValueError"}
    span.end()
    with tracer.start_as_current_span("ctx") as current:
        assert current.is_recording()
    plain = tracer.start_span("plain")
    assert "error.type" not in plain.attributes

    class Slotted:
        __slots__ = ()

        def set_status(self, *args):
            pass

        def set_attribute(self, *args):
            pass

    slotted = Slotted()
    assert _sanitize_aws_span(slotted) is slotted, "a span that cannot be patched is left as it is"

