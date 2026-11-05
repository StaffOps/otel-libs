"""Keep the OTel SDK from echoing ``OTEL_RESOURCE_ATTRIBUTES`` items (R8, design D1 row of 2026-10-29).

The helper parses the variable itself, but the SDK still runs its own env detector
whenever ``Resource.create()`` is called — notably at import of
``opentelemetry.sdk._logs`` (a log-record class default) — and that detector logs every
item without ``=`` verbatim on the ``opentelemetry.sdk.resources`` logger. A
process-local :class:`logging.Filter` drops exactly that record while the helper
imports the SDK log module and while it sets up; the environment is never touched.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
from collections.abc import Iterator

_SDK_RESOURCES_LOGGER = "opentelemetry.sdk.resources"
_INVALID_PAIR = "Invalid key value resource attribute pair"


class _DropInvalidPair(logging.Filter):
    """Drops the SDK's invalid-pair warning (it prints the raw item); every other record passes."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (isinstance(record.msg, str) and record.msg.startswith(_INVALID_PAIR))


_FILTER = _DropInvalidPair()


@contextlib.contextmanager
def suppress_sdk_resource_echo() -> Iterator[None]:
    """Install the filter for the duration of the block (re-entrant: nested blocks keep it)."""
    installed = _FILTER not in logging.getLogger(_SDK_RESOURCES_LOGGER).filters
    if installed:
        logging.getLogger(_SDK_RESOURCES_LOGGER).addFilter(_FILTER)
    try:
        yield
    finally:
        if installed:
            logging.getLogger(_SDK_RESOURCES_LOGGER).removeFilter(_FILTER)


def import_sdk_logs_quietly() -> None:
    """Import ``opentelemetry.sdk._logs`` with the filter on (it calls ``Resource.create()`` at import)."""
    with suppress_sdk_resource_echo():
        importlib.import_module("opentelemetry.sdk._logs")
