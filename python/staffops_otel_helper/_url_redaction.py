"""URL query redaction on every span the helper's TracerProvider starts (contract task A28, 2026-11-03)."""

import logging
import re
import traceback
from typing import Any
from urllib.parse import unquote

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import Span, SpanKind, Status


#: Query parameters whose values are written as ``REDACTED`` (contract design D1
#: "URL query redaction", A28): request signatures and the credentials and
#: session tokens that travel with them — AWS SigV4 (``X-Amz-Signature``,
#: ``X-Amz-Credential``, ``X-Amz-Security-Token``) and SigV2
#: (``AWSAccessKeyId``, ``Signature``), Google Cloud Storage
#: (``X-Goog-Signature``), Azure SAS (``sig``): a superset of the semconv /
#: contrib util-http default (``AWSAccessKeyId``, ``Signature``, ``sig``,
#: ``X-Goog-Signature``). The same list in Go and Node.js; names match exactly
#: (case-sensitive), after percent-decoding. Since A30 (2026-11-05) also the
#: OAuth 2.0 bearer token in a query (``access_token``, RFC 6750 section 2.3)
#: and the Google Cloud Storage signed-URL credential (``X-Goog-Credential``,
#: V4; ``GoogleAccessId``, V2).
REDACTED_QUERY_PARAMS = ("sig", "Signature", "AWSAccessKeyId", "X-Goog-Signature",
                         "X-Amz-Signature", "X-Amz-Credential", "X-Amz-Security-Token",
                         "access_token", "X-Goog-Credential", "GoogleAccessId")
_REDACTED_SET = frozenset(REDACTED_QUERY_PARAMS)
#: ``url.full`` keeps a ``#fragment``; ASGI writes ``http.url``/``http.target``
#: with the whole query unquoted, so everything after the first ``?`` is query.
_URL_KEYS = ("url.full",)
_WHOLE_QUERY_KEYS = ("http.url", "http.target")
_ATTRIBUTE_KEYS = frozenset({"url.full", "http.url", "http.target", "url.query"})
#: A listed parameter inside free text (a status description, an exception
#: message or stacktrace): ``?name=value`` / ``&name=value``.
_TEXT_PARAM = re.compile(r"(?<=[?&])([^=&\s'\"#)]+)=[^&\s'\"#)]*")
_log = logging.getLogger("staffops_otel_helper._url_redaction")


def redact_query(query: str) -> str:
    """``query`` (no ``?``) with the listed values replaced; every other byte kept as written.

    Names are percent-decoded before the exact (case-sensitive) match; ``;``
    is not a separator, and a lower-case name is not a listed one (both kept
    by design, D1 "URL query redaction").
    """
    if not query:
        return query
    parts = query.split("&")
    changed = False
    for i, part in enumerate(parts):
        key, eq, _ = part.partition("=")
        if eq and unquote(key) in _REDACTED_SET:  # a name without "=" has no value: kept
            parts[i] = f"{key}=REDACTED"
            changed = True
    return "&".join(parts) if changed else query


def _redact_url(url: str, keep_fragment: bool = True) -> str:
    before, sep, rest = url.partition("?")
    if not sep:
        return url
    if not keep_fragment:
        return f"{before}?{redact_query(rest)}"
    query, hash_sep, fragment = rest.partition("#")
    return f"{before}?{redact_query(query)}{hash_sep}{fragment}"


def redact_attribute(key: str, value: Any) -> Any:
    """The value recorded for ``key`` (one of the URL attributes), else ``value`` unchanged."""
    if not isinstance(value, str):
        return value
    if key == "url.query":
        return redact_query(value)
    if key in _URL_KEYS:
        return _redact_url(value)
    if key in _WHOLE_QUERY_KEYS:
        return _redact_url(value, keep_fragment=False)
    return value


def redact_text(text: str) -> str:
    """``text`` with every ``?name=value`` / ``&name=value`` of a listed name redacted."""
    if not isinstance(text, str) or ("?" not in text and "&" not in text):
        return text

    def replace(match: re.Match) -> str:
        name = match.group(1)
        # "&amp;name=" (an HTML-escaped "&"): the name is matched without "amp;".
        bare = name[4:] if name.startswith("amp;") else name
        return f"{name}=REDACTED" if unquote(bare) in _REDACTED_SET else match.group(0)

    return _TEXT_PARAM.sub(replace, text)


def _redact_attributes(attributes: Any) -> Any:
    if not attributes:
        return attributes
    out = dict(attributes)
    for key, value in out.items():
        if key in _ATTRIBUTE_KEYS:
            out[key] = redact_attribute(key, value)
        elif isinstance(value, str):
            out[key] = redact_text(value)
    return out


def _wrap_span(span: Span) -> None:
    """Patch the live span so every later URL attribute, status description and event is redacted.

    Instrumentations write URL attributes after start too (ASGI 0.66b2 sets
    ``url.query``/``http.target`` again when the response starts), and a
    failed client call puts the URL in its status and exception (urllib3
    ``MaxRetryError``). The wrappers never raise: on an internal failure the
    original call runs with the value as given.
    """
    original_attribute = span.set_attribute
    original_attributes = getattr(span, "set_attributes", None)
    original_status = span.set_status
    original_record = getattr(span, "record_exception", None)
    original_event = getattr(span, "add_event", None)

    def set_attribute(key: str, value: Any) -> Any:
        try:
            value = redact_attribute(key, value) if key in _ATTRIBUTE_KEYS else value
        except Exception:  # noqa: BLE001 - never raise into the instrumented call
            _log.debug("url attribute not redacted", exc_info=False)
        return original_attribute(key, value)

    def set_attributes(attributes: Any) -> Any:
        try:
            attributes = {k: (redact_attribute(k, v) if k in _ATTRIBUTE_KEYS else v)
                          for k, v in dict(attributes).items()}
        except Exception:  # noqa: BLE001
            _log.debug("url attributes not redacted", exc_info=False)
        return original_attributes(attributes)

    def set_status(status: Any, description: str | None = None) -> Any:
        try:
            if isinstance(status, Status) and status.description:
                status = Status(status.status_code, redact_text(status.description))
            if description:
                description = redact_text(description)
        except Exception:  # noqa: BLE001
            _log.debug("status not redacted", exc_info=False)
        return original_status(status, description)

    def record_exception(exception: BaseException, attributes: Any = None, timestamp: int | None = None,
                         escaped: bool = False) -> Any:
        extra = attributes
        try:
            stacktrace = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
            extra = {"exception.message": redact_text(str(exception)),
                     "exception.stacktrace": redact_text(stacktrace),
                     **(_redact_attributes(attributes) or {})}
        except Exception:  # noqa: BLE001
            _log.debug("exception event not redacted", exc_info=False)
        return original_record(exception, attributes=extra, timestamp=timestamp, escaped=escaped)

    def add_event(name: str, attributes: Any = None, timestamp: int | None = None) -> Any:
        try:
            attributes = _redact_attributes(attributes)
        except Exception:  # noqa: BLE001
            _log.debug("event not redacted", exc_info=False)
        return original_event(name, attributes, timestamp)

    try:
        span.set_attribute = set_attribute  # type: ignore[method-assign]
        span.set_status = set_status  # type: ignore[method-assign]
        if original_attributes is not None:
            span.set_attributes = set_attributes  # type: ignore[method-assign]
        if original_record is not None:
            span.record_exception = record_exception  # type: ignore[method-assign]
        if original_event is not None:
            span.add_event = add_event  # type: ignore[method-assign]
    except AttributeError:  # a span type with __slots__: left as it is
        pass


class UrlQueryRedactionProcessor(SpanProcessor):
    """Redacts the :data:`REDACTED_QUERY_PARAMS` values on every span the helper's provider starts (A28).

    contrib util-http redacts only ``AWSAccessKeyId``, ``Signature``, ``sig``
    and ``X-Goog-Signature`` in client ``url.full`` (``PARAMS_TO_REDACT``,
    0.63b0–0.66b1), so a presigned SigV4 URL kept its credential, signature
    and session token; the ASGI server writes ``url.query`` unredacted
    (``_set_http_target``), at start and again later. ``on_start`` rewrites
    the start attributes and patches the live span (:func:`_wrap_span`), so
    later attribute writes, the status description and exception events are
    redacted too. One rule for every span of the helper's provider; no
    instrumentation patched. A foreign global TracerProvider gets no redaction.
    """

    def on_start(self, span: Span, parent_context=None) -> None:
        try:
            attributes = getattr(span, "attributes", None) or {}
            has_url = False
            for key in _ATTRIBUTE_KEYS:
                value = attributes.get(key)
                has_url = has_url or value is not None
                redacted = redact_attribute(key, value)
                if redacted != value:
                    span.set_attribute(key, redacted)
            # Only spans that carry or may later carry a URL: HTTP client/server spans.
            if has_url or getattr(span, "kind", None) in (SpanKind.CLIENT, SpanKind.SERVER):
                _wrap_span(span)
        except Exception:  # noqa: BLE001 - a processor never raises into the span start
            _log.debug("span not redacted", exc_info=False)

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
