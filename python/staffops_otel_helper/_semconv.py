"""Stable HTTP and dual database semantic conventions by default (design D12, R10).

Contrib instrumentations read ``OTEL_SEMCONV_STABILITY_OPT_IN`` once, at the
first ``instrument()`` call, and default to the old HTTP generation
(``http.server.duration`` in ms) and the legacy database keys (``db.system``).
Go can only emit stable HTTP and its ext/otelsql emits both ``db.system.name``
and ``db.system``, so the helper defaults the variable to ``http,database/dup``
before any instrumentor runs (verified on contrib sqlalchemy 0.63b0 and 0.66b1:
both keys). A value set by the user wins for HTTP; for the database keys it is
normalized (A25, ``normalize_semconv_opt_in``): ASCII-folded, and ``database/dup``
appended when it names no database mode, so only ``database`` without ``/dup``
drops the legacy ``db.system`` (Go and Node rule).
"""

from __future__ import annotations

import logging
import os

from staffops_otel_helper.config import TRIM_CHARS, ascii_lower

_log = logging.getLogger("staffops_otel_helper.semconv")

ENV_SEMCONV_OPT_IN = "OTEL_SEMCONV_STABILITY_OPT_IN"
DEFAULT_SEMCONV_OPT_IN = "http,database/dup"
_STABLE_HTTP_TOKENS = frozenset({"http", "http/dup"})
_warned = False


def _late_initialized_old_http() -> bool:
    """True when contrib already fixed the HTTP mode to the old generation.

    Reads a private contrib attribute: no public API exists. Any failure means
    "can't tell" (no warning), never an error.
    """
    try:
        from opentelemetry.instrumentation._semconv import (
            _OpenTelemetrySemanticConventionStability as stability,
            _OpenTelemetryStabilitySignalType as signal_type,
            _StabilityMode as mode,
        )

        if not stability._initialized:
            return False
        return stability._OTEL_SEMCONV_STABILITY_SIGNAL_MAPPING.get(signal_type.HTTP) == mode.DEFAULT
    except Exception:  # noqa: BLE001 - private API; absent or changed means no warning
        return False


_DATABASE_TOKENS = frozenset({"database", "database/dup"})


def normalize_semconv_opt_in(value: str | None) -> str:
    """The value contrib should read (contract design D1 row "Database semconv opt-in", A25).

    Unset, empty or whitespace-only (the contract trim set) → ``http,database/dup``
    (empty = unset, as in Node and the contract). A set value is ASCII-folded —
    every token, so ``HTTP`` / ``HTTP/DUP`` now select stable / dup HTTP too
    (contrib compares tokens case-sensitively; Go and Node fold) — and one
    without a ``database`` or ``database/dup`` token gets ``database/dup``
    appended, so the legacy ``db.system`` is kept unless ``database`` is chosen
    without ``/dup``, as Go ``ext/otelsql`` and Node ``-sql`` do. In dup mode
    contrib emits the full new attribute set and the full legacy set. Before
    A25 a value such as ``http`` switched contrib to the legacy database keys only.
    """
    if value is None or not value.strip(TRIM_CHARS):
        return DEFAULT_SEMCONV_OPT_IN
    folded = ascii_lower(value)
    tokens = {t.strip(TRIM_CHARS) for t in folded.split(",")}
    if tokens & _DATABASE_TOKENS:
        return folded
    return f"{folded},database/dup"


def ensure_semconv_default() -> None:
    """Normalize ``OTEL_SEMCONV_STABILITY_OPT_IN`` (default ``http,database/dup``); warn once if HTTP stays old.

    Writes the normalized value back to ``os.environ``: contrib reads it from
    there, and child processes started afterwards inherit the rewritten value.
    """
    global _warned
    os.environ[ENV_SEMCONV_OPT_IN] = normalize_semconv_opt_in(os.environ.get(ENV_SEMCONV_OPT_IN))
    value = os.environ[ENV_SEMCONV_OPT_IN]
    tokens = {t.strip(TRIM_CHARS) for t in value.split(",")}
    if not tokens & _STABLE_HTTP_TOKENS:
        reason = f"{ENV_SEMCONV_OPT_IN}={value!r} does not include 'http'"
    elif _late_initialized_old_http():
        reason = "an instrumentation was initialized before staffops_otel_helper set the default"
    else:
        return
    if not _warned:
        _warned = True
        _log.warning(
            "[OtelHelper] HTTP telemetry uses the old semantic conventions (%s): metric names and "
            "attributes will not match the other helper libraries. Set %s=http (or http/dup) before "
            "any instrumentation runs.", reason, ENV_SEMCONV_OPT_IN,
        )
