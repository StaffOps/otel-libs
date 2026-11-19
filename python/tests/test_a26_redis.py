"""A26 (2026-11-03): Redis DB error status, error code, and pipeline exception events.

A failed Redis command span keeps the ERROR code and drops the description
(the server's error text, which can quote a command argument: ``unknown
command 'NOSUCHCMD', with args beginning with: '<arg>'``): contract design D1
"DB error status" covers every DB client (Go ``ext/otelredis``, Node
``@staffops/otel-helper-redis``). The reply's code becomes
``db.response.status_code`` and ``error.type`` (``_OTHER`` without one). A
pipeline error's exception event keeps the command names only: redis-py's
``annotate_exception`` puts the failing command and its arguments in the
message on the client (fix round 2026-11-03, dev H1). Both helper paths —
``instrument_redis()`` and the deprecated ``REDIS`` flag of
``setup_telemetry()`` — bind contrib redis to the ``_db_status`` wrapper. The
exception event keeps the server text (the A25 residual).

Server: an in-process RESP2 fake (always), and a real Redis when
``REDIS_TEST_URL`` is set (``redis://default:<password>@host:port/0``;
testing/docker-compose.yml python-tests, CI python-test).
"""

import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import textwrap
import threading

import pytest

SECRET = "secret-quoted-arg"
# Keys and values the commands send: never in a span attribute, status or exception event.
VALUES = ["secret-pipe-key", "secret-pipe-value", "secret-async-key", "secret-async-value", "secret-eval-arg"]


class _RespHandler(socketserver.StreamRequestHandler):
    """Enough RESP2 for redis-py with ``protocol=2``: CLIENT, SET, LPUSH (WRONGTYPE), EVAL, unknown commands."""

    def _read_command(self):
        head = self.rfile.readline()
        if not head:
            return None
        count = int(head[1:])
        args = []
        for _ in range(count):
            size = int(self.rfile.readline()[1:])
            args.append(self.rfile.read(size + 2)[:-2].decode())
        return args

    def handle(self):
        store = self.server.store
        while True:
            args = self._read_command()
            if args is None:
                return
            name = args[0].upper()
            if name in ("CLIENT", "SELECT"):
                reply = b"+OK\r\n"
            elif name == "SET":
                store[args[1]] = args[2]
                reply = b"+OK\r\n"
            elif name == "LPUSH" and isinstance(store.get(args[1]), str):
                reply = b"-WRONGTYPE Operation against a key holding the wrong kind of value\r\n"
            elif name == "EVAL":  # redis.error_reply(...) as a real server answers it
                m = re.search(r"error_reply\('([^']*)'\)", args[1])
                if "ARGV[1] .. ' bad'" in args[1]:
                    reply = f"-{args[3]} bad\r\n".encode()
                else:
                    reply = f"-{m.group(1) if m else 'ERR script'}\r\n".encode()
            else:
                quoted = "".join(f"'{a}' " for a in args[1:])
                reply = f"-ERR unknown command '{args[0]}', with args beginning with: {quoted}\r\n".encode()
            self.wfile.write(reply)


@pytest.fixture
def resp_fake():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _RespHandler)
    server.daemon_threads = True
    server.store = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"redis://127.0.0.1:{server.server_address[1]}/0"
    server.shutdown()
    server.server_close()


_SCRIPT = textwrap.dedent("""
    import asyncio, json, logging, sys
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    mode, url, secret = sys.argv[1], sys.argv[2], sys.argv[3]
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    if mode == "foreign":
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        RedisInstrumentor().instrument()
    from staffops_otel_helper import setup_telemetry, TelemetryOptions
    setup_telemetry(TelemetryOptions(service_name="svc", extra_instrumentation="REDIS" if mode == "setup" else ""))
    ex = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(ex))
    if mode in ("ext", "foreign"):
        from staffops_otel_helper.ext.redis import instrument_redis
        assert instrument_redis()
        assert instrument_redis(), "a second call is a no-op that reports the instrumentation active"
    import redis
    import redis.asyncio
    r = redis.Redis.from_url(url, protocol=2)
    r.set("a26-py-key", "v")
    calls = [
        lambda: r.lpush("a26-py-key", "x"),
        lambda: r.execute_command("NOSUCHCMD", secret),
        lambda: r.eval("return redis.error_reply('MYCODE secret-eval-arg')", 0),
        # A Lua reply whose first word is data (R1): never an attribute.
        lambda: r.eval("return redis.error_reply(ARGV[1] .. ' bad')", 0, "SECRET_TOKEN42"),
    ]
    for call in calls:
        try:
            call()
        except redis.ResponseError:
            pass
    p = r.pipeline(transaction=False)
    p.set("secret-pipe-key", "secret-pipe-value")
    p.lpush("a26-py-key", "secret-pipe-value")
    try:
        p.execute()
    except redis.ResponseError:
        pass
    async def pipeline():
        a = redis.asyncio.Redis.from_url(url, protocol=2)
        ap = a.pipeline(transaction=False)
        ap.set("secret-async-key", "secret-async-value")
        ap.lpush("a26-py-key", "secret-async-value")
        try:
            await ap.execute()
        except redis.ResponseError:
            pass
        await (getattr(a, "aclose", None) or a.close)()  # aclose: redis >= 5.0.1
    asyncio.run(pipeline())
    out = []
    for s in ex.get_finished_spans():
        out.append({"name": s.name, "code": s.status.status_code.name, "description": s.status.description,
                    "attributes": {k: str(v) for k, v in s.attributes.items()},
                    "events": [dict(e.attributes) for e in s.events]})
    print(json.dumps(out))
""")


def _run(mode, url):
    pytest.importorskip("redis")
    pytest.importorskip("opentelemetry.instrumentation.redis")
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env["OTEL_HELPER_METRICS_PORT"] = "0"
    out = subprocess.run([sys.executable, "-c", _SCRIPT, mode, url, SECRET], env=env, capture_output=True,
                         text=True, check=True, timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1]), out.stderr


def _assert_failed_spans_redacted(spans):
    import redis.exceptions

    # redis-py 8.x keeps an unmapped code's text and sets status_code for mapped
    # ones; older versions strip ERR without a trace, so a plain ResponseError is ERR.
    modern = hasattr(redis.exceptions.ResponseError("x"), "status_code")
    evals = [s for s in spans if s["name"] == "EVAL"]
    single = {s["name"]: s for s in spans if s["name"] in ("SET", "LPUSH", "NOSUCHCMD")}
    single["EVAL"] = evals[0]
    assert single["SET"]["code"] == "UNSET", spans
    assert "error.type" not in single["SET"]["attributes"]
    for name in ("LPUSH", "NOSUCHCMD", "EVAL"):
        span = single[name]
        assert span["code"] == "ERROR", span
        assert not span["description"], span  # was "ResponseError: <server text>"
        assert all(SECRET not in v for v in span["attributes"].values()), span
    # Known reply codes only (R1/R2): WRONGTYPE stays in redis-py's text; ERR is stripped
    # (status_code on 8.x, a plain ResponseError before).
    assert single["LPUSH"]["attributes"]["db.response.status_code"] == "WRONGTYPE"
    assert single["LPUSH"]["attributes"]["error.type"] == "WRONGTYPE"
    assert single["NOSUCHCMD"]["attributes"]["error.type"] == "ERR"
    assert single["NOSUCHCMD"]["attributes"]["db.response.status_code"] == "ERR"
    # Lua error_reply first words (MYCODE, SECRET_TOKEN42) are not reply codes.
    assert len(evals) == 2, evals
    for span in evals:
        assert span["attributes"]["error.type"] == ("_OTHER" if modern else "ERR"), span
        assert "SECRET_TOKEN42" not in json.dumps(span["attributes"])
    # Accepted residual (contract D1 "DB error text in exception events"): the event keeps the server text.
    assert SECRET in single["NOSUCHCMD"]["events"][0]["exception.message"]

    # Pipelines, sync and asyncio: one span named by its commands; the event has no key or value.
    pipes = [s for s in spans if s["name"] == "SET LPUSH"]
    assert len(pipes) == 2, [s["name"] for s in spans]
    for span in pipes:
        assert span["code"] == "ERROR" and not span["description"], span
        assert span["attributes"]["error.type"] == "WRONGTYPE"
        event = span["events"][0]
        # redis.asyncio writes the reply as a tuple repr: ('WRONGTYPE ...',)
        assert event["exception.message"].startswith("Command # 2 (LPUSH ? ?) of pipeline caused error: "), event
        assert "WRONGTYPE Operation against a key holding the wrong kind of value" in event["exception.message"]
        assert event["exception.stacktrace"].endswith(f"redis.exceptions.ResponseError: {event['exception.message']}\n")
    for span in spans:
        text = json.dumps([span["attributes"], span["description"], span["name"]])
        events = json.dumps(span["events"])
        for value in VALUES:
            assert value not in text, (value, span["name"])
            if value != "secret-eval-arg":  # the Lua reply quotes it: server text (residual)
                assert value not in events, (value, span["name"], events)


@pytest.mark.parametrize("mode", ["ext", "setup"])
def test_failed_commands_carry_no_description(mode, resp_fake):
    spans, stderr = _run(mode, resp_fake)
    _assert_failed_spans_redacted(spans)
    assert "already instrumented without the helper" not in stderr


def test_instrumented_elsewhere_is_kept_and_warned(resp_fake):
    spans, stderr = _run("foreign", resp_fake)
    assert stderr.count("Redis is already instrumented without the helper's tracer") == 1, stderr
    assert any(s["description"] for s in spans if s["code"] == "ERROR"), "the foreign instrumentation keeps contrib's text"


@pytest.mark.skipif(not os.environ.get("REDIS_TEST_URL"), reason="REDIS_TEST_URL unset (a real Redis)")
@pytest.mark.parametrize("mode", ["ext", "setup"])
def test_failed_commands_carry_no_description_on_a_real_redis(mode):
    url = os.environ["REDIS_TEST_URL"]
    host, port = url.split("@")[-1].split("/")[0].rsplit(":", 1)
    socket.create_connection((host, int(port)), timeout=5).close()
    spans, _ = _run(mode, url)
    _assert_failed_spans_redacted(spans)


def test_sanitize_pipeline_message():
    from staffops_otel_helper._db_status import sanitize_pipeline_message

    assert sanitize_pipeline_message("Command # 3 (hset k f v) of pipeline caused error: WRONGTYPE x") == (
        "Command # 3 (HSET ? ? ?) of pipeline caused error: WRONGTYPE x")
    assert sanitize_pipeline_message("Command # 1 (PING) of pipeline caused error: boom") == (
        "Command # 1 (PING) of pipeline caused error: boom")
    assert sanitize_pipeline_message("WRONGTYPE plain") == "WRONGTYPE plain"
    from staffops_otel_helper._db_status import _server_text

    assert _server_text("Command # 2 (LPUSH k v) of pipeline caused error: ('WRONGTYPE x',)") == "WRONGTYPE x"
    assert _server_text("Command # 2 (LPUSH k v) of pipeline caused error: (not a tuple") == "(not a tuple"
    assert _server_text("Command # 2 (LPUSH k v) of pipeline caused error: (1,)") == "(1,)"


def test_shared_wrapper_is_the_sql_one():
    from staffops_otel_helper import _db_status
    from staffops_otel_helper.ext import sql as sql_ext

    assert sql_ext.wrapped_instrument_kwargs is _db_status.wrapped_instrument_kwargs
    assert sql_ext._drop_error_description is _db_status._drop_error_description
    marked = type("I", (), {})()
    assert not _db_status.is_wrapped(marked)
    _db_status.mark_wrapped(marked)
    assert _db_status.is_wrapped(marked)
    _db_status.clear_wrapped(marked)
    assert not _db_status.is_wrapped(marked)
    _db_status.clear_wrapped(marked)  # idempotent


def test_status_without_record_exception_falls_back_to_the_description():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import Status, StatusCode

    from staffops_otel_helper._db_status import _drop_error_description

    tp = TracerProvider()
    span = _drop_error_description(tp.get_tracer("t").start_span("x"), redis=True)
    span.set_status(Status(StatusCode.ERROR, "ResponseError: WRONGTYPE Operation against a key"))
    assert span.attributes["error.type"] == "WRONGTYPE"
    assert span.status.description is None
    custom = _drop_error_description(tp.get_tracer("t").start_span("w"), redis=True)
    custom.set_status(Status(StatusCode.ERROR, "ResponseError: SECRET_TOKEN42 bad"))
    assert custom.attributes["error.type"] == "_OTHER"
    other = _drop_error_description(tp.get_tracer("t").start_span("y"), redis=True)
    other.set_status(StatusCode.ERROR, "no code here")
    assert other.attributes["error.type"] == "_OTHER"
    ok = _drop_error_description(tp.get_tracer("t").start_span("z"), redis=True)
    ok.set_status(StatusCode.OK)
    assert "error.type" not in ok.attributes


def test_reply_code_resolution():
    """R2 (2026-11-03): status_code, then a known leading code, then the class map, then ERR (redis-py < 8)."""
    redis = pytest.importorskip("redis")
    from redis import exceptions as e

    from staffops_otel_helper._db_status import REPLY_CODES, redis_reply_code

    modern = hasattr(e.ResponseError("x"), "status_code")
    assert redis_reply_code(e.NoPermissionError("this user has no permissions"), "this user has no permissions") == "NOPERM"
    assert redis_reply_code(e.NoScriptError("No matching script"), "No matching script") == "NOSCRIPT"
    assert redis_reply_code(e.ResponseError("WRONGTYPE Operation"), "WRONGTYPE Operation") == "WRONGTYPE"
    # ERR-stripped texts that start with an upper-case word are not codes (EXEC, DUMP).
    for text in ("EXEC without MULTI", "DUMP payload version or checksum are wrong"):
        assert redis_reply_code(e.ResponseError(text), text) == (None if modern else "ERR"), text
    assert redis_reply_code(e.ConnectionError("Error 111 connecting"), "Error 111 connecting") is None
    assert redis_reply_code(None, "MOVED 3999 127.0.0.1:6381") == "MOVED"
    assert redis_reply_code(None, "MYCODE x") is None
    if modern:
        err = e.ResponseError("unknown command", status_code="ERR")
        assert redis_reply_code(err, "unknown command") == "ERR"
        assert redis_reply_code(e.ResponseError("x", status_code="SECRET_TOKEN42"), "x") is None
    assert {"ERR", "WRONGTYPE", "NOAUTH", "MOVED", "ASK"} <= REPLY_CODES
    assert "NOTREADY" not in REPLY_CODES  # not a Redis 7.4 reply code


def test_pipeline_message_fallback_and_chains():
    """dev L1 / obs LOW-5 (2026-11-03): args[0], every chained message, any "of pipeline" text."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from staffops_otel_helper._db_status import _drop_error_description, sanitize_pipeline_message

    assert sanitize_pipeline_message("Command #2 (SET secret-k secret-v) of pipeline failed") == "Command #2 () of pipeline failed"
    ex = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(ex))
    secret = "Command # 1 (SET secret-k secret-v) of pipeline caused error: WRONGTYPE x"

    def raised(exc):
        try:
            raise exc
        except Exception as caught:  # noqa: BLE001
            return caught

    cause = raised(ValueError(secret))
    try:
        raise RuntimeError("wrapper " + secret) from cause
    except RuntimeError as chained:
        span = _drop_error_description(tp.get_tracer("t").start_span("chained"), redis=True)
        span.record_exception(chained)
        span.end()
    two = raised(ValueError(secret, "second arg"))
    span = _drop_error_description(tp.get_tracer("t").start_span("two"), redis=True)
    span.record_exception(two)
    span.end()
    for s in ex.get_finished_spans():
        event = dict(s.events[0].attributes)
        text = event["exception.message"] + event["exception.stacktrace"]
        assert "secret-k" not in text and "secret-v" not in text, (s.name, text)
    by_name = {s.name: dict(s.events[0].attributes) for s in ex.get_finished_spans()}
    assert by_name["two"]["exception.message"] == "Command # 1 (SET ? ?) of pipeline caused error: WRONGTYPE x"
    assert "The above exception was the direct cause" in by_name["chained"]["exception.stacktrace"]


def test_wrapper_never_raises_into_the_call(monkeypatch):
    """code-review N1 (2026-11-03): an internal failure records a minimal event."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from opentelemetry.trace import StatusCode

    from staffops_otel_helper import _db_status

    def boom(*_a, **_k):
        raise RuntimeError("internal")

    monkeypatch.setattr(_db_status, "sanitize_pipeline_message", boom)
    monkeypatch.setattr(_db_status, "redis_reply_code", boom)
    ex = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(ex))
    span = _db_status._drop_error_description(tp.get_tracer("t").start_span("x"), redis=True)
    span.record_exception(ValueError("Command # 1 (SET k v) of pipeline caused error: x"))
    span.set_status(StatusCode.ERROR, "y")
    span.end()
    event = dict(ex.get_finished_spans()[0].events[0].attributes)
    assert event["exception.message"] == "(the message could not be sanitized)"
    assert ex.get_finished_spans()[0].status.status_code is StatusCode.ERROR
    # literal_eval input is bounded
    assert _db_status._server_text("Command # 1 (X) of pipeline caused error: (" + "1," * 3000 + ")").startswith("(")
