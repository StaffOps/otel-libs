"""Masks the configured OTLP header values in the SDK exporters' log records.

Design D1 row "SDK error text: response bodies and header values" (A29). The
OTLP exporters never log a response body, but they do log server-controlled
text: the HTTP reason phrase (``opentelemetry-exporter-otlp-proto-http``
1.42.0 ``trace_exporter/__init__.py:205-220``; 1.45.2 through
``opentelemetry-exporter-otlp-common`` 0.66b2 ``http.py:202-218``) and, from
1.45, the gRPC status message (``exporter.py:467-484`` ``error.details()``).
A collector or proxy echoing the request there would print the header values.
A filter on those loggers masks the values of ``OTEL_EXPORTER_OTLP_HEADERS``
and ``OTEL_EXPORTER_OTLP_<SIGNAL>_HEADERS`` and of their whitespace-separated
parts (``mask_forms``: raw and decoded, JSON-escaped once and twice, HTML-escaped
JSON, form-encoded, ``repr``; under 4 characters skipped) in the message and the
exception text. It reads the environment at each record and never raises.

Scope (A29 fix round, dev L3/L4): a logger filter applies only to records logged
on that logger, so a child logger of these names, or another SDK logger (e.g.
``opentelemetry.sdk._shared_internal``), is not masked — the SDK logs no server
text there today. A traceback that needed masking is folded into the message and
``exc_info`` cleared, so handlers that read ``exc_info`` (the OTel
``LoggingHandler`` ``exception.*``, error trackers) see no exception for that record.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from urllib.parse import quote_plus

from staffops_otel_helper.config import TRIM_CHARS, _parse_headers

#: The SDK OTLP exporter loggers (``logging.getLogger(__name__)`` of each module, 1.42-1.45).
EXPORTER_LOGGERS: tuple[str, ...] = (
    "opentelemetry.exporter.otlp.proto.grpc.exporter",
    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
    "opentelemetry.exporter.otlp.proto.grpc.metric_exporter",
    "opentelemetry.exporter.otlp.proto.grpc._log_exporter",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.exporter.otlp.proto.http.metric_exporter",
    "opentelemetry.exporter.otlp.proto.http._log_exporter",
    "opentelemetry.exporter.otlp.proto.http._common",
    "opentelemetry.exporter.otlp.common.http",
)

_HEADER_VARIABLES = tuple(
    f"OTEL_EXPORTER_OTLP_{p}HEADERS" for p in ("", "TRACES_", "METRICS_", "LOGS_")
)

#: Shortest configured value that is masked (Go ``minMaskedValue``, Node ``MIN_MASKED_SECRET``).
MIN_MASKED_VALUE = 4
REDACTED = "<redacted>"
UNRENDERABLE = "<OTLP exporter log record could not be rendered>"

_cache_lock = threading.Lock()
_cache: tuple[tuple[str | None, ...], tuple[str, ...], tuple[str, ...]] | None = None
# The values the exporters were built with (capture_header_values, at setup).
_captured: tuple[str, ...] = ()


def _values_of(raws: tuple[str | None, ...]) -> tuple[str, ...]:
    values: dict[str, None] = {}
    for raw in raws:
        if raw is None or not raw.strip(TRIM_CHARS):
            continue
        for value in _parse_headers(raw)[0].values():
            values.setdefault(value, None)
        for item in raw.split(","):
            _, sep, value = item.partition("=")
            if sep:
                values.setdefault(value.strip(TRIM_CHARS), None)
    return tuple(v for v in values if v)


def _raws() -> tuple[str | None, ...]:
    return tuple(os.environ.get(v) for v in _HEADER_VARIABLES)


def capture_header_values() -> None:
    """Records the header values setup builds the exporters with: they stay masked after
    the application changes or deletes the variables, because the exporters keep sending
    them (A29 final round, dev R2-M1)."""
    global _captured
    values = _values_of(_raws())
    with _cache_lock:
        _captured = values


def _reset_captured() -> None:
    """Forget the captured values. For tests only."""
    global _captured
    with _cache_lock:
        _captured = ()


def configured_header_values() -> tuple[str, ...]:
    """The union of the values captured at setup and the configured header values now,
    raw (after the first ``=``, trimmed) and decoded; rebuilt when either changes. Never logged."""
    global _cache
    raws = _raws()
    with _cache_lock:
        captured = _captured
        if _cache is not None and _cache[0] == raws and _cache[1] is captured:
            return _cache[2]
    result = tuple(dict.fromkeys((*captured, *_values_of(raws))))
    with _cache_lock:
        _cache = (raws, captured, result)
    return result


def _json(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)[1:-1]


def _forms(value: str) -> list[str]:
    """The masked forms of one value (design D1 row "SDK error text", A29 fix round)."""
    once = _json(value)
    # Go's encoding/json default (HTML escaping), what a Go collector or proxy writes.
    html = once.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return [
        value,
        once,
        _json(once),
        html,
        _json(html),  # and JSON-escaped again (a JSON-rendered error)
        quote_plus(value, safe=""),  # form-encoded: space as "+", as Go url.QueryEscape
        value.replace("\\", "\\\\").replace("'", "\\'"),  # repr() of a str
    ]


_AUTH_SCHEMES = frozenset({"bearer", "basic", "digest", "negotiate"})


def mask_forms(values: tuple[str, ...] | list[str]) -> list[str]:
    """Every masked form of every value and of each of its whitespace-separated parts
    (the credential of ``Bearer <token>``) of at least 4 characters; deduplicated,
    longest first."""
    forms: set[str] = set()
    for value in values:
        parts = value.split()
        if len(parts) > 1 and parts[0].lower() in _AUTH_SCHEMES:
            parts = parts[1:]  # "Bearer" alone is not a secret (A29 final round)
        for base in (value, *parts):
            if len(base) >= MIN_MASKED_VALUE:
                forms.update(f for f in _forms(base) if f)
    return sorted(forms, key=lambda f: (-len(f), f))


def _mask_spans(text: str, forms: list[str]) -> str:
    """Marks every occurrence of every form (overlapping ones included) and replaces each
    run of marked characters by one ``<redacted>`` (A29 final round, dev R2-L1): overlapping
    values never leave a tail, and ``<redacted>`` is never scanned again."""
    marked: bytearray | None = None
    for form in forms:
        start = text.find(form)
        while start >= 0:
            if marked is None:
                marked = bytearray(len(text))
            marked[start:start + len(form)] = b"\x01" * len(form)
            start = text.find(form, start + 1)
    if marked is None:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if marked[i]:
            out.append(REDACTED)
            while i < n and marked[i]:
                i += 1
        else:
            j = marked.find(1, i)
            j = n if j < 0 else j
            out.append(text[i:j])
            i = j
    return "".join(out)


def mask_header_values(text: str, values: tuple[str, ...] | list[str]) -> str:
    """``text`` with every form of each value replaced by ``<redacted>`` (see ``_mask_spans``)."""
    return _mask_spans(text, mask_forms(values))


def _mask(text: str, values: tuple[str, ...]) -> str:
    return _mask_spans(text, _forms_for(values))


_forms_cache: tuple[tuple[str, ...], list[str]] | None = None


def _forms_for(values: tuple[str, ...]) -> list[str]:
    """The forms of ``values``, memoized on the tuple configured_header_values() returns."""
    global _forms_cache
    cached = _forms_cache
    if cached is not None and cached[0] is values:
        return cached[1]
    forms = mask_forms(values)
    _forms_cache = (values, forms)
    return forms


class HeaderValueMask(logging.Filter):
    """Masks the configured header values in a record; never drops it, never raises."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            values = configured_header_values()
            if not values:
                return True
            message = record.getMessage()
            masked = _mask(message, values)
            if record.exc_info and record.exc_info[0] is not None:
                text = record.exc_text or logging.Formatter().formatException(record.exc_info)
                masked_exc = _mask(text, values)
                if masked_exc != text:
                    # The traceback moves into the message: a handler reading exc_info
                    # (the OTel LoggingHandler sets exception.* from it) must not see it.
                    masked = f"{masked}\n{masked_exc}"
                    record.exc_info, record.exc_text = None, None
            if record.stack_info:
                record.stack_info = _mask(record.stack_info, values)
            if masked != message:
                record.msg, record.args = masked, None
        except Exception:  # noqa: BLE001 - a log filter must never raise
            record.msg, record.args, record.exc_info, record.exc_text = UNRENDERABLE, None, None, None
        return True


_installed = False
_install_lock = threading.Lock()
_FILTER = HeaderValueMask()


def install_sdk_log_mask() -> None:
    """Adds the filter to every SDK OTLP exporter logger, once per process."""
    global _installed
    with _install_lock:
        if _installed:
            return
        for name in EXPORTER_LOGGERS:
            logging.getLogger(name).addFilter(_FILTER)
        _installed = True
