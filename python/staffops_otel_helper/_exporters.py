"""OTLP exporter construction from a resolved :class:`~staffops_otel_helper.config.SignalExport`.

Only the arguments the helper owns are passed (``SignalExport.kwargs``); the SDK
resolves everything else from the standard env vars (ADR-001).
"""

from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from collections.abc import Iterator

from staffops_otel_helper.config import PROTOCOL_HTTP, SignalExport

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
    replaced by an explicit ``timeout=``) would crash setup (R8). The TLS file
    variables are hidden too: the helper passes the validated files itself (A18).
    """
    saved = {n: os.environ.pop(n) for n in names if n in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def _tls_kwargs(plan: SignalExport) -> dict[str, object]:
    """TLS arguments from the helper-validated files (D1 ruling "TLS files", A18).

    gRPC: explicit channel credentials, so the SDK never reads the (masked) env
    files and grpc never sees an incomplete client pair. HTTP: the file paths
    (``certificate_file`` falls back to the system roots when no CA is set).
    """
    tls = plan.tls
    if tls is None:
        return {}
    if plan.protocol == PROTOCOL_HTTP:
        out: dict[str, object] = {}
        if tls.ca_path is not None:
            out["certificate_file"] = tls.ca_path
        if tls.key_path is not None and tls.cert_path is not None:
            out["client_key_file"] = tls.key_path
            out["client_certificate_file"] = tls.cert_path
        return out
    import grpc

    return {"credentials": grpc.ssl_channel_credentials(
        root_certificates=tls.ca, private_key=tls.key, certificate_chain=tls.cert)}


def _grpc_headers(headers: dict[str, str]) -> dict[str, object]:
    """``-bin`` metadata keys need bytes in grpc: send the (ASCII) value's bytes, as Go does (A18)."""
    return {k: (v.encode("ascii") if k.endswith("-bin") else v) for k, v in headers.items()}


def build_otlp_exporter(plan: SignalExport):  # noqa: ANN201 - one of six SDK exporter types
    """Instantiate the OTLP exporter for one signal."""
    module_name, class_name = (_HTTP if plan.protocol == PROTOCOL_HTTP else _GRPC)[plan.signal]
    cls = getattr(importlib.import_module(module_name), class_name)
    kwargs = dict(plan.kwargs)
    kwargs["compression"] = _compression(plan.protocol, kwargs["compression"])
    if plan.protocol != PROTOCOL_HTTP and isinstance(kwargs.get("headers"), dict):
        kwargs["headers"] = _grpc_headers(kwargs["headers"])  # type: ignore[arg-type]
    kwargs.update(_tls_kwargs(plan))
    with _masked(plan.masked_env):
        return cls(**kwargs)
