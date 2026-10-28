"""OTLP exporter construction from a resolved :class:`~otel_helper.config.SignalExport`.

Only the arguments the helper owns are passed (``SignalExport.kwargs``); the SDK
resolves everything else from the standard env vars (ADR-001).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from collections.abc import Iterator

from otel_helper.config import PROTOCOL_HTTP, SignalExport

_HTTP = {
    "traces": ("opentelemetry.exporter.otlp.proto.http.trace_exporter", "OTLPSpanExporter"),
    "metrics": ("opentelemetry.exporter.otlp.proto.http.metric_exporter", "OTLPMetricExporter"),
    "logs": ("opentelemetry.exporter.otlp.proto.http._log_exporter", "OTLPLogExporter"),
}
_GRPC = {
    "traces": ("opentelemetry.exporter.otlp.proto.grpc.trace_exporter", "OTLPSpanExporter"),
    "metrics": ("opentelemetry.exporter.otlp.proto.grpc.metric_exporter", "OTLPMetricExporter"),
    "logs": ("opentelemetry.exporter.otlp.proto.grpc._log_exporter", "OTLPLogExporter"),
}


def _compression(protocol: str, name: str):  # noqa: ANN202 - enum type differs per protocol
    if protocol == PROTOCOL_HTTP:
        from opentelemetry.exporter.otlp.proto.http import Compression
    else:
        from grpc import Compression
    return {"gzip": Compression.Gzip, "none": Compression.NoCompression}[name]


@contextmanager
def _masked(names: tuple[str, ...]) -> Iterator[None]:
    """Hide env vars from the SDK constructor for the duration of the call.

    The gRPC exporters parse ``OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT`` with
    ``float()`` unconditionally, so an invalid value (already warned, and
    replaced by an explicit ``timeout=``) would crash setup (R8).
    """
    saved = {n: os.environ.pop(n) for n in names if n in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def build_otlp_exporter(plan: SignalExport):  # noqa: ANN201 - one of six SDK exporter types
    """Instantiate the OTLP exporter for one signal."""
    import importlib

    module_name, class_name = (_HTTP if plan.protocol == PROTOCOL_HTTP else _GRPC)[plan.signal]
    cls = getattr(importlib.import_module(module_name), class_name)
    kwargs = dict(plan.kwargs)
    kwargs["compression"] = _compression(plan.protocol, kwargs["compression"])
    with _masked(plan.masked_env):
        return cls(**kwargs)
