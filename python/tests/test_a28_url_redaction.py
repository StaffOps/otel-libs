"""A28 (2026-11-03): URL query redaction parity (contract design D1 "URL query redaction").

The values of the shared parameter list (a superset of contrib util-http's
default) are written as ``REDACTED`` in ``url.full``, ``http.url``,
``http.target`` and ``url.query`` on every span the helper's TracerProvider
starts; Go and Node.js redact the same list.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest


def test_url_query_redaction_processor():
    """A28: the shared parameter list is redacted in url.full, http.url, http.target and url.query at span start."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from staffops_otel_helper._url_redaction import REDACTED_QUERY_PARAMS, UrlQueryRedactionProcessor, redact_query

    assert REDACTED_QUERY_PARAMS == ("sig", "Signature", "AWSAccessKeyId", "X-Goog-Signature",
                                     "X-Amz-Signature", "X-Amz-Credential", "X-Amz-Security-Token",
                                     "access_token", "X-Goog-Credential", "GoogleAccessId")
    for raw, want in [
        ("", ""),
        ("a=1&b=2", "a=1&b=2"),
        ("X-Amz-Signature=abc&x=1", "X-Amz-Signature=REDACTED&x=1"),
        ("x=%20&sig=s&flag", "x=%20&sig=REDACTED&flag"),
        ("X%2DAmz%2DCredential=AKIA%2Fday", "X%2DAmz%2DCredential=REDACTED"),
        ("x-amz-signature=abc", "x-amz-signature=abc"),
        ("AWSAccessKeyId&Signature=", "AWSAccessKeyId&Signature=REDACTED"),  # no "=": no value, kept
        ("x=1;sig=a", "x=1;sig=a"),  # ";" is not a separator (kept by design)
        ("sig=a;Signature=b", "sig=REDACTED"),
        # A30 (2026-11-05): three more names, matched exactly.
        ("access_token=tok&a=1", "access_token=REDACTED&a=1"),
        ("X-Goog-Credential=sa%40p.iam%2F20261105&x=", "X-Goog-Credential=REDACTED&x="),
        ("GoogleAccessId=sa@p.iam&Signature=s", "GoogleAccessId=REDACTED&Signature=REDACTED"),
        ("Access_Token=tok", "Access_Token=tok"),
    ]:
        assert redact_query(raw) == want, raw
    ex = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(UrlQueryRedactionProcessor())
    tp.add_span_processor(SimpleSpanProcessor(ex))
    tracer = tp.get_tracer("t")
    presigned = ("https://b.s3.amazonaws.com/k?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIAEXAMPLEFAKEKEY01%2F2026"
                 "&X-Amz-Security-Token=FakeToken&X-Amz-Signature=" + "a" * 64 + "&keep=1#frag")
    tracer.start_span("client", attributes={"url.full": presigned, "http.url": presigned}).end()
    tracer.start_span("server", attributes={"url.query": "sig=s&a=1", "http.target": "/p?sig=s"}).end()
    tracer.start_span("plain", attributes={"url.full": "https://h/p?a=1", "url.query": "a=1", "n": 1}).end()
    spans = {s.name: dict(s.attributes) for s in ex.get_finished_spans()}
    for key in ("url.full", "http.url"):
        assert spans["client"][key] == ("https://b.s3.amazonaws.com/k?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=REDACTED"
                                        "&X-Amz-Security-Token=REDACTED&X-Amz-Signature=REDACTED&keep=1#frag")
    assert spans["server"] == {"url.query": "sig=REDACTED&a=1", "http.target": "/p?sig=REDACTED"}
    assert spans["plain"] == {"url.full": "https://h/p?a=1", "url.query": "a=1", "n": 1}


def test_setup_installs_the_url_redaction_processor():
    from opentelemetry.sdk.resources import Resource

    from staffops_otel_helper.config import TelemetryOptions
    from staffops_otel_helper._url_redaction import UrlQueryRedactionProcessor
    from staffops_otel_helper.tracing import build_tracer_provider

    provider = build_tracer_provider(Resource.create({}), TelemetryOptions(service_name="svc"))
    try:
        processors = provider._active_span_processor._span_processors  # noqa: SLF001 - the SDK's processor list
        assert isinstance(processors[0], UrlQueryRedactionProcessor)
    finally:
        provider.shutdown()


def test_redact_text_and_attributes():
    from staffops_otel_helper._url_redaction import redact_attribute, redact_text

    text = ("HTTPConnectionPool(host='127.0.0.1', port=1): Max retries exceeded with url: "
            "/k?X-Amz-Credential=AKIAEXAMPLEFAKEKEY01%2F2026&X-Amz-Signature=abc (Caused by ...)")
    assert redact_text(text).endswith("/k?X-Amz-Credential=REDACTED&X-Amz-Signature=REDACTED (Caused by ...)")
    assert redact_text("'https://h/p?sig=s'") == "'https://h/p?sig=REDACTED'"
    assert redact_text("?X%2DAmz%2DSignature=v#x") == "?X%2DAmz%2DSignature=REDACTED#x"
    assert redact_text("no query here") == "no query here"
    # HTML-escaped "&" (&amp;) in free text: the name is matched without its "amp;" prefix.
    assert redact_text("<a href='/k?a=1&amp;X-Amz-Signature=abc'>") == "<a href='/k?a=1&amp;X-Amz-Signature=REDACTED'>"
    # A URL percent-encoded as a whole (%3F...%3D) is kept by design (D1).
    assert redact_text("u=https%3A%2F%2Fh%2Fk%3FX-Amz-Signature%3Dabc") == "u=https%3A%2F%2Fh%2Fk%3FX-Amz-Signature%3Dabc"
    assert redact_text("?x-amz-signature=v&keep=1") == "?x-amz-signature=v&keep=1", "lower-case names are kept"
    assert redact_text(5) == 5
    # http.url / http.target: everything after the first "?" is query (ASGI unquotes it, "#" included).
    assert redact_attribute("http.target", "/p?a=#1&sig=s") == "/p?a=#1&sig=REDACTED"
    assert redact_attribute("url.full", "https://h/p?sig=s#sig=t") == "https://h/p?sig=REDACTED#sig=t"
    assert redact_attribute("url.query", "sig=s") == "sig=REDACTED"
    assert redact_attribute("other", "?sig=s") == "?sig=s"
    assert redact_attribute("url.full", 5) == 5


def test_live_span_writes_status_and_events_are_redacted():
    """Attributes written after start, the status description and exception events (H1, H2)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import Status, StatusCode

    from staffops_otel_helper._url_redaction import UrlQueryRedactionProcessor, _wrap_span

    ex = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(UrlQueryRedactionProcessor())
    tp.add_span_processor(SimpleSpanProcessor(ex))
    from opentelemetry.trace import SpanKind

    internal = tp.get_tracer("t").start_span("internal")
    internal.set_attribute("url.query", "sig=kept")  # no URL at start, not CLIENT/SERVER: not wrapped
    internal.end()
    span = tp.get_tracer("t").start_span("s", kind=SpanKind.SERVER)
    span.set_attribute("url.query", "sig=late")
    span.set_attributes({"http.target": "/p?Signature=late", "n": 1})
    span.set_status(StatusCode.ERROR, "failed: /p?sig=status")
    try:
        raise ConnectionError("Max retries exceeded with url: /p?X-Amz-Signature=exc")
    except ConnectionError as exc:
        span.record_exception(exc, attributes={"extra": "?sig=attr"})
    span.add_event("e", {"msg": "&sig=event"})
    span.end()
    other = tp.get_tracer("t").start_span("status-object", attributes={"url.full": "https://h/p"})
    other.set_status(Status(StatusCode.ERROR, "x?sig=obj"))
    other.end()
    done = {s.name: s for s in ex.get_finished_spans()}
    s = done["s"]
    assert dict(s.attributes) == {"url.query": "sig=REDACTED", "http.target": "/p?Signature=REDACTED", "n": 1}
    assert s.status.description == "failed: /p?sig=REDACTED"
    blob = repr([e.attributes for e in s.events])
    for leaked in ("=exc", "=attr", "=event"):
        assert leaked not in blob.replace("REDACTED", ""), (leaked, blob)
    assert s.events[0].attributes["exception.message"].endswith("X-Amz-Signature=REDACTED")
    assert "X-Amz-Signature=REDACTED" in s.events[0].attributes["exception.stacktrace"]
    assert done["status-object"].status.description == "x?sig=REDACTED"
    assert done["internal"].attributes["url.query"] == "sig=kept", "only URL-carrying or HTTP spans are wrapped"

    class Slotted:
        __slots__ = ()

        def set_attribute(self, *args):
            pass

        def set_status(self, *args):
            pass

    assert _wrap_span(Slotted()) is None, "a span that cannot be patched is left as it is"
    UrlQueryRedactionProcessor().on_start(object())  # never raises


_SCRIPT = textwrap.dedent("""
    import json, os, sys, threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    mode, secret = sys.argv[1], sys.argv[2]
    if mode == "app":
        from fastapi import FastAPI
        app = FastAPI()  # built before setup: instrument_fastapi(app)
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from staffops_otel_helper import TelemetryOptions, instrument_fastapi, setup_telemetry
    setup_telemetry(TelemetryOptions(service_name="svc"))
    ex = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(ex))
    if mode == "app":
        instrument_fastapi(app)
    else:
        from fastapi import FastAPI
        app = FastAPI()  # built after setup: the patched class

    @app.get("/items")
    def items():
        return {"ok": True}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    query = (f"X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential={secret}%2F20261103%2Fus-east-1%2Fs3%2Faws4_request"
             f"&X-Amz-Security-Token=Tok{secret}&X-Amz-Signature=Sig{secret}&keep=1")
    used = []
    try:
        import requests
        requests.get(f"{base}/bucket/key?{query}", timeout=5)
        try:
            requests.get(f"http://127.0.0.1:1/bucket/key?{query}", timeout=5)
        except requests.RequestException:
            pass
        used.append("requests")
    except ImportError:
        pass
    import httpx
    httpx.get(f"{base}/bucket/key?{query}")
    used.append("httpx")
    from starlette.testclient import TestClient
    TestClient(app).get(f"/items?{query}")
    used.append("fastapi")
    server.shutdown()
    out = []
    for s in ex.get_finished_spans():
        out.append({"name": s.name, "scope": s.instrumentation_scope.name if s.instrumentation_scope else "",
                    "description": s.status.description, "attributes": {k: str(v) for k, v in s.attributes.items()},
                    "events": [{k: str(v) for k, v in e.attributes.items()} for e in s.events]})
    print(json.dumps({"used": used, "spans": out}))
""")


@pytest.mark.parametrize("mode,opt_in", [("auto", None), ("app", None), ("auto", "http/dup")])
def test_real_instrumentations_never_record_the_presigned_query(mode, opt_in):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("requests")  # the refused-port case must never be skipped silently
    secret = "AKIAEXAMPLEFAKEKEY01"
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env["OTEL_HELPER_METRICS_PORT"] = "0"
    if opt_in:
        env["OTEL_SEMCONV_STABILITY_OPT_IN"] = opt_in
    proc = subprocess.run([sys.executable, "-c", _SCRIPT, mode, secret], env=env, capture_output=True, text=True,
                          check=True, timeout=120)
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    scopes = {s["scope"] for s in result["spans"]}
    assert any("httpx" in sc for sc in scopes), scopes
    assert any("asgi" in sc or "fastapi" in sc for sc in scopes), scopes
    assert "requests" in result["used"], result["used"]
    if "requests" in result["used"]:
        assert any("requests" in sc for sc in scopes), scopes
        failed = [s for s in result["spans"] if "requests" in s["scope"] and (s["description"] or s["events"])]
        assert failed, "the refused-port call records a failure (status or exception event), swept below"
    for span in result["spans"]:
        text = json.dumps(span)
        assert secret not in text, (span["name"], text)
    keys = ("url.full", "http.url", "url.query", "http.target")
    urls = [v for s in result["spans"] for k, v in s["attributes"].items() if k in keys]
    # Other parameters stay as written (a value without a query, e.g. a dup-mode path, is skipped).
    assert urls and all("keep=1" in u for u in urls if "=" in u), [u for u in urls if "keep=1" not in u]
    assert any("X-Amz-Signature=REDACTED" in u for u in urls), urls
