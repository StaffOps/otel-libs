"""``OTEL_PROPAGATORS`` handling (design D1 propagators row, decided 2026-10-28).

Supported in every helper language: ``tracecontext``, ``baggage``, ``none``.
Any other value (``b3``, ``jaeger``, ``xray``, ``ottrace``, …) is skipped with
a warning; when nothing supported is left, the W3C default applies: the helper ships no extra propagator dependencies, and env values
must never crash the app (R8).

The OpenTelemetry API resolves ``OTEL_PROPAGATORS`` when ``opentelemetry.propagate``
is first imported and raises ``ValueError`` for an entry point that isn't
installed. :func:`import_propagate_safely` therefore runs before the package
imports anything from OpenTelemetry; :func:`build_propagator` then installs the
composite the helper resolved.
"""

from __future__ import annotations

import logging
import os
import sys

from otel_helper.config import TRIM_CHARS

_log = logging.getLogger("otel_helper.propagators")
_DEFAULT = ["tracecontext", "baggage"]

ENV_PROPAGATORS = "OTEL_PROPAGATORS"
SUPPORTED = ("tracecontext", "baggage", "none")


def parse(raw: str) -> tuple[list[str], list[str]]:
    """(supported tokens in order, unsupported tokens); ``none`` clears the list, as in the API."""
    keep: list[str] = []
    skipped: list[str] = []
    for token in (t.strip(TRIM_CHARS).lower() for t in raw.split(",")):
        if not token:
            continue
        if token == "none":
            return ["none"], skipped
        if token not in SUPPORTED:
            skipped.append(token)
        elif token not in keep:
            keep.append(token)
    return keep, skipped


def import_propagate_safely() -> None:
    """Import ``opentelemetry.propagate`` with the env value in canonical form.

    The API raises on unknown entries, on casing it doesn't recognize
    (``TraceContext``) and on empty tokens (``tracecontext,,baggage``, ``""``), so
    the raw value is replaced by the helper's canonical form whenever they differ.

    The env var is restored right after the import. Does nothing when the module
    was already imported (by then the API already accepted or rejected the value).
    """
    raw = os.environ.get(ENV_PROPAGATORS)
    if raw is None or "opentelemetry.propagate" in sys.modules:
        return
    keep, _ = parse(raw)
    canonical = ",".join(keep or _DEFAULT)
    if raw == canonical:
        return
    os.environ[ENV_PROPAGATORS] = canonical
    try:
        import opentelemetry.propagate  # noqa: F401 - resolves the filtered value once
    finally:
        os.environ[ENV_PROPAGATORS] = raw


def build_propagator():  # noqa: ANN201 - CompositePropagator
    """The global propagator: ``OTEL_PROPAGATORS`` (supported entries) > W3C TraceContext + Baggage."""
    from opentelemetry.baggage.propagation import W3CBaggagePropagator
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    raw = os.environ.get(ENV_PROPAGATORS, "")
    tokens = _DEFAULT
    if raw.strip(TRIM_CHARS):
        tokens, skipped = parse(raw)
        tokens = tokens or _DEFAULT  # only unsupported entries: keep the default
        if skipped:
            _log.warning(
                "[OtelHelper] %s: unsupported propagator(s) %s skipped (supported: %s).",
                ENV_PROPAGATORS, ", ".join(repr(s) for s in skipped), ", ".join(SUPPORTED),
            )
    factories = {"tracecontext": TraceContextTextMapPropagator, "baggage": W3CBaggagePropagator}
    return CompositePropagator([factories[t]() for t in tokens if t in factories])
