"""OTLP exporter construction from a resolved :class:`~staffops_otel_helper.config.SignalExport`.

Only the arguments the helper owns are passed (``SignalExport.kwargs``); the SDK
resolves everything else from the standard env vars (ADR-001).
"""

from __future__ import annotations

import functools
import importlib
import logging
import os
from contextlib import contextmanager
from collections.abc import Iterator

from staffops_otel_helper._diag import error_once_per_process, warn_once_per_process
from staffops_otel_helper.config import PROTOCOL_HTTP, SignalExport

_log = logging.getLogger("staffops_otel_helper.export")

#: The SDK's generic credential-provider variable (an entry point that returns a
#: ``requests.Session``); the per-signal one is built by :func:`_credential_variables`.
_CREDENTIAL_PROVIDER = "OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER"

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


def _report_redirect(signal: str, status: int) -> None:
    """One ERROR per process, signal and status (design D1 "OTLP export redirects", A31).

    The wording is shared with the other languages (contract fragment
    ``redirects are not followed``, then ``signal=`` and ``status=``). The
    ``Location`` value, which may carry credentials, is never printed.
    """
    error_once_per_process(
        _log, f"redirect:{signal}:{status}",
        "otelhelper: the OTLP endpoint for %s answered HTTP %d (a redirect); redirects are not followed, "
        "so these %s are not exported: configure the final endpoint URL signal=%s status=%d",
        signal, status, signal, signal, status)


@functools.cache
def _redirect_refused_error() -> type:
    """The exception the response hook raises on a ``3xx`` (requests is imported lazily).

    A ``requests.exceptions.RequestException`` that is not a connection error
    or a timeout, so both exporter bounds fail the export without a retry:
    1.42 catches ``RequestException`` in ``export()`` and retries only a
    ``ConnectionError``; 1.45 turns the exception into a transport error and
    retries only its connection-error types (``ConnectionError``, timeouts,
    ``SSLError``, ``ProxyError``). Its text names the signal and the status,
    never the ``Location``.
    """
    import requests

    class OTLPRedirectRefused(requests.exceptions.RequestException):
        """An OTLP/HTTP export was answered with a redirect, which the helper does not follow."""

    # The SDK self-metrics record ``error.type`` as ``type(error).__qualname__``:
    # keep it stable instead of ``_redirect_refused_error.<locals>.OTLPRedirectRefused``.
    OTLPRedirectRefused.__qualname__ = "OTLPRedirectRefused"
    OTLPRedirectRefused.__name__ = "OTLPRedirectRefused"
    return OTLPRedirectRefused


def _refuse_redirects(session, signal: str):  # noqa: ANN001, ANN202 - requests.Session, imported lazily
    """Make ``session`` never follow a ``3xx``, report it, and fail the export.

    ``get_redirect_target`` returning ``None`` stops requests before it builds
    the follow-up request (OTLP 1.42 posts with redirects allowed, and would
    replay the request to the ``Location`` host). The response hook writes the
    ERROR and raises :func:`_redirect_refused_error`: without it both exporter
    bounds count a ``3xx`` as a successful export (1.42 checks ``Response.ok``,
    1.45 ``200 <= status < 400``), and the SDK self-metrics would record the
    lost batch as exported. The response is closed first, its body unread.
    """

    def _no_redirect_target(_resp: object) -> None:
        return None

    def _on_response(resp, *_args: object, **_kwargs: object) -> None:  # noqa: ANN001 - requests.Response
        if 300 <= resp.status_code <= 399:
            status = resp.status_code
            _report_redirect(signal, status)
            resp.close()
            raise _redirect_refused_error()(
                f"OTLP export answered HTTP {status}, a redirect; not followed (signal={signal} status={status})")

    session.get_redirect_target = _no_redirect_target
    session.hooks.setdefault("response", []).append(_on_response)
    return session


def _credential_variables(signal: str) -> tuple[str, str]:
    return _CREDENTIAL_PROVIDER, f"OTEL_PYTHON_EXPORTER_OTLP_HTTP_{signal.upper()}_CREDENTIAL_PROVIDER"


def _http_session(signal: str):  # noqa: ANN202 - requests.Session, or None to let the SDK build it
    """The ``requests.Session`` passed to an OTLP/HTTP exporter (``session=``).

    A credential provider (``OTEL_PYTHON_EXPORTER_OTLP_HTTP[_<SIGNAL>]_CREDENTIAL_PROVIDER``)
    is loaded by the SDK's own loader, so the provider's session gets the same
    refusal; a loader that returns no session gets a plain refusing one, as
    the SDK would build a plain ``requests.Session``. Should that private
    loader move in a later SDK, the helper warns once, naming the variable,
    and leaves the exporter to load the provider itself (``None``): a plain
    session would drop the provider's credentials, so that one export path
    follows the SDK's redirect handling.
    """
    import requests

    variables = _credential_variables(signal)
    configured = [v for v in variables if os.environ.get(v)]
    if not configured:
        return _refuse_redirects(requests.Session(), signal)
    try:
        from opentelemetry.exporter.otlp.proto.http._common import _load_session_from_envvar
    except ImportError:
        warn_once_per_process(
            _log, f"redirect-loader:{configured[0]}",
            "otelhelper: %s is set but the SDK credential-provider loader was not found; "
            "the provider's session is left to the SDK, so its OTLP/HTTP redirects are not refused "
            "by the helper signal=%s", configured[0], signal)
        return None
    session = _load_session_from_envvar(variables[1])
    return _refuse_redirects(requests.Session() if session is None else session, signal)


def build_otlp_exporter(plan: SignalExport):  # noqa: ANN201 - one of six SDK exporter types
    """Instantiate the OTLP exporter for one signal."""
    module_name, class_name = (_HTTP if plan.protocol == PROTOCOL_HTTP else _GRPC)[plan.signal]
    cls = getattr(importlib.import_module(module_name), class_name)
    kwargs = dict(plan.kwargs)
    kwargs["compression"] = _compression(plan.protocol, kwargs["compression"])
    if plan.protocol != PROTOCOL_HTTP and isinstance(kwargs.get("headers"), dict):
        kwargs["headers"] = _grpc_headers(kwargs["headers"])  # type: ignore[arg-type]
    kwargs.update(_tls_kwargs(plan))
    if plan.protocol == PROTOCOL_HTTP:
        session = _http_session(plan.signal)
        if session is not None:
            kwargs["session"] = session
    with _masked(plan.masked_env):
        return cls(**kwargs)
