"""Process-wide helper diagnostics that must be emitted at most once per process.

``TelemetryOptions`` de-duplicates its own warnings per instance; deprecations
(R8/D8) and unsupported-instrumentation warnings (P18) are "once per process",
whatever the number of options objects or ``setup_telemetry()`` calls.
"""

from __future__ import annotations

import logging
import threading

_lock = threading.Lock()
_emitted: set[str] = set()


def warn_once_per_process(logger: logging.Logger, key: str, msg: str, *args: object) -> None:
    """Log ``msg`` at WARNING on ``logger`` the first time ``key`` is seen in this process."""
    with _lock:
        if key in _emitted:
            return
        _emitted.add(key)
    logger.warning(msg, *args)


def _reset() -> None:
    """Forget what was emitted. For tests only."""
    with _lock:
        _emitted.clear()
