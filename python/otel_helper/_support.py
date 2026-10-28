"""Make contrib instrumentors that refuse the installed library version loud (P18).

``BaseInstrumentor.instrument()`` turns into a no-op when a library the
instrumentor declares (``instrumentation_dependencies()``) is installed in a
version outside the supported range — e.g. SQLAlchemy 2.1 with
``opentelemetry-instrumentation-sqlalchemy`` 0.63b0–0.66b1 (``< 2.1.0``). The
upstream record is a terse ERROR on ``opentelemetry.instrumentation.instrumentor``
(0.63b0: ``DependencyConflict: requested ... but found ...``) and the call
returns normally, so the app runs without those spans. The helper adds one
warning on its own channel naming the instrumentor, the installed and the
supported versions, and what to do.

A library that is not installed at all is not reported: optional libraries are
skipped silently by design.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version

from opentelemetry.instrumentation.dependencies import get_dependency_conflicts

from otel_helper._diag import warn_once_per_process

_log = logging.getLogger("otel_helper.instrumentation")
_CONTRIB_DIST = "opentelemetry-instrumentation"


def _installed(dist: str) -> str | None:
    try:
        return version(dist)
    except PackageNotFoundError:
        return None


def unsupported_dependency(instrumentor) -> tuple[str, str] | None:
    """First declared dependency that is installed outside the supported range.

    Returns ``(requirement, found)`` as upstream reports them (e.g.
    ``("sqlalchemy >= 1.0.0, < 2.1.0", "sqlalchemy 2.1.6")``), or None when every
    declared dependency is either missing or in range (or cannot be read).
    Each dependency is checked on its own, so a missing one cannot hide an
    out-of-range one.
    """
    try:
        for dep in instrumentor.instrumentation_dependencies():
            conflict = get_dependency_conflicts([dep])
            if conflict is not None and conflict.found is not None:
                return str(conflict.required), str(conflict.found)
    except Exception:  # noqa: BLE001 - advisory check, never break setup
        return None
    return None


def warn_if_unsupported(instrumentor) -> bool:
    """Warn (once per process and instrumentor) when ``instrument()`` was refused.

    Call after ``instrument()``. Returns True when a warning applies (the
    instrumentor is inactive because of an unsupported library version).
    """
    if getattr(instrumentor, "is_instrumented_by_opentelemetry", False):
        return False
    found = unsupported_dependency(instrumentor)
    if found is None:
        return False
    requirement, installed = found
    name = type(instrumentor).__name__
    warn_once_per_process(
        _log, f"unsupported:{name}:{installed}",
        "[OtelHelper] %s (%s %s) does not support the installed %s (supported: %s): it is NOT "
        "instrumented, so that library records no telemetry. Install a supported version, or "
        "upgrade otel-helper once a contrib release supports it.",
        name, _CONTRIB_DIST, _installed(_CONTRIB_DIST) or "unknown", installed, requirement,
    )
    return True


def missing_dependency_hint(err: ImportError, extra: str, label: str, library: str, module: str) -> str:
    """Message for an ``ext.*`` entry point whose import of ``module`` failed.

    A cause is named only when it is certain (design D14, A13 ruling):

    - ``ModuleNotFoundError`` for ``module`` (the contrib instrumentor, e.g.
      ``opentelemetry.instrumentation.sqlalchemy``) or one of its parent
      packages: the extra is not installed.
    - ``ModuleNotFoundError`` for ``library`` (the instrumented library, e.g.
      ``sqlalchemy``) or one of its submodules: the extra installs only the
      instrumentor, so the user must install the library itself.
    - anything else — a plain ``ImportError`` (version skew), a missing
      transitive dependency (``wrapt``), another ``opentelemetry.*`` module
      (``opentelemetry.semconv._incubating…``), no module name: the import
      error, verbatim.
    """
    if isinstance(err, ModuleNotFoundError) and err.name:
        parts = module.split(".")
        if err.name in {".".join(parts[:i]) for i in range(1, len(parts) + 1)}:
            return (
                f"{label} instrumentation not installed. "
                f"Run: pip install otel-helper[{extra}]"
            )
        if err.name.split(".")[0] == library:
            return (
                f"{label} instrumentation is installed but the instrumented library "
                f"'{library}' is not. Install it (e.g. pip install {library}); "
                f"otel-helper[{extra}] does not pull it in."
            )
    return f"failed to import the {label} instrumentation: {err}"
