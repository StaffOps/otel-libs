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

from staffops_otel_helper.config import TRIM_CHARS, _truncate, ascii_lower

_log = logging.getLogger("staffops_otel_helper.propagators")
_DEFAULT = ["tracecontext", "baggage"]

ENV_PROPAGATORS = "OTEL_PROPAGATORS"
SUPPORTED = ("tracecontext", "baggage", "none")
# Spec values the helper does not link (warned as "not supported", not "unknown").
_UNSUPPORTED_SPEC = frozenset(["b3", "b3multi", "jaeger", "xray", "ottrace"])


def parse(raw: str) -> tuple[list[str], list[str], bool]:
    """(linked tokens in order, skipped tokens in order, ``none`` seen).

    Every token is scanned (design D1 row "Propagator diagnostics", A20): tokens
    are trimmed and ASCII-lowercased; empty tokens and duplicates are dropped.
    """
    keep: list[str] = []
    skipped: list[str] = []
    none = False
    seen: set[str] = set()
    for token in (ascii_lower(t.strip(TRIM_CHARS)) for t in raw.split(",")):
        if not token or token in seen:
            continue
        seen.add(token)
        if token == "none":
            none = True
        elif token in SUPPORTED:
            keep.append(token)
        else:
            skipped.append(token)
    return keep, skipped, none


def _effective(keep: list[str], none: bool) -> list[str]:
    """The propagator names the helper installs: ``[]`` for ``none``, the W3C default when nothing is left."""
    if none:
        return []
    return keep or list(_DEFAULT)


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
    keep, _, none = parse(raw)
    canonical = ",".join(_effective(keep, none)) or "none"
    if raw == canonical:
        return
    os.environ[ENV_PROPAGATORS] = canonical
    try:
        import opentelemetry.propagate  # noqa: F401 - resolves the filtered value once
    finally:
        os.environ[ENV_PROPAGATORS] = raw


def build_propagator():  # noqa: ANN201 - CompositePropagator
    """The global propagator: ``OTEL_PROPAGATORS`` (supported entries) > W3C TraceContext + Baggage.

    One warning per skipped token ("not supported" for spec values the helper does
    not link, else "unknown"), and one when ``none`` comes with other values; each
    names the effective list as ``using`` (design D1 row "Propagator diagnostics", A20).
    """
    from opentelemetry.baggage.propagation import W3CBaggagePropagator
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    raw = os.environ.get(ENV_PROPAGATORS, "")
    tokens = list(_DEFAULT)
    if raw.strip(TRIM_CHARS):
        keep, skipped, none = parse(raw)
        tokens = _effective(keep, none)
        using = ", ".join(tokens) or "none"
        for token in skipped:
            kind = ("propagator not supported by the Python helper" if token in _UNSUPPORTED_SPEC
                    else "unknown propagator")
            _log.warning("[OtelHelper] %s, ignored: variable=%s value=%r using=%s.",
                         kind, ENV_PROPAGATORS, _truncate(token), using)
        if none and (keep or skipped):
            _log.warning('[OtelHelper] "none" combined with other propagators; propagation disabled: '
                         "variable=%s value=%r using=none.", ENV_PROPAGATORS, _truncate(raw))
    factories = {"tracecontext": TraceContextTextMapPropagator, "baggage": W3CBaggagePropagator}
    return CompositePropagator([factories[t]() for t in tokens])
