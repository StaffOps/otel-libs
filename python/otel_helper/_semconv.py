"""Stable HTTP semantic conventions by default (design D12, R10).

Contrib instrumentations read ``OTEL_SEMCONV_STABILITY_OPT_IN`` once, at the
first ``instrument()`` call, and default to the old HTTP generation
(``http.server.duration`` in ms). Go can only emit the stable one, so the helper
defaults the variable to ``http`` before any instrumentor runs. A value set by
the user always wins (including ``http/dup``).
"""

from __future__ import annotations

import logging
import os

from otel_helper.config import TRIM_CHARS

_log = logging.getLogger("otel_helper.semconv")

ENV_SEMCONV_OPT_IN = "OTEL_SEMCONV_STABILITY_OPT_IN"
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


def ensure_semconv_default() -> None:
    """Default ``OTEL_SEMCONV_STABILITY_OPT_IN`` to ``http``; warn once if HTTP stays on the old generation."""
    global _warned
    os.environ.setdefault(ENV_SEMCONV_OPT_IN, "http")
    value = os.environ[ENV_SEMCONV_OPT_IN]
    tokens = {t.strip(TRIM_CHARS).lower() for t in value.split(",")}
    if not tokens & _STABLE_HTTP_TOKENS:
        reason = f"{ENV_SEMCONV_OPT_IN}={value!r} does not include 'http'"
    elif _late_initialized_old_http():
        reason = "an instrumentation was initialized before otel_helper set the default"
    else:
        return
    if not _warned:
        _warned = True
        _log.warning(
            "[OtelHelper] HTTP telemetry uses the old semantic conventions (%s): metric names and "
            "attributes will not match the other helper libraries. Set %s=http (or http/dup) before "
            "any instrumentation runs.", reason, ENV_SEMCONV_OPT_IN,
        )
