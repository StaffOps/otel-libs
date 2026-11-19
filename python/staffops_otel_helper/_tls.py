"""TLS files of a TLS endpoint, resolved by the helper (contract design D1 ruling
"TLS files", 2026-10-29, task A18).

``OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE``, ``_CLIENT_KEY`` and
``_CLIENT_CERTIFICATE`` each resolve per-signal, then generic; a file that
cannot be read (or, for the CA, holds no PEM certificate) warns and falls back
to the next level, then the default (system roots / no client certificate).
The key and the certificate are one pair: when only one resolves, or the TLS
layer rejects them (bad PEM, mismatch, encrypted key), the signal uses server
authentication with the resolved CA. This also keeps grpc's C core from
aborting the process on an incomplete pair (``grpc_tls_certificate_distributor``
CHECK). Warnings name the variables only, never a path or file content (R8).
"""

from __future__ import annotations

import errno
import os
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field

_TRIM = " \t\n\r\v\f"

Warn = Callable[..., None]  # (key, msg, *args)


@dataclass(frozen=True)
class TlsFiles:
    """Validated TLS files of one signal (None: system roots / no client certificate)."""

    ca_path: str | None = None
    key_path: str | None = None
    cert_path: str | None = None
    # File contents for gRPC credentials; never printed (repr=False).
    ca: bytes | None = field(default=None, repr=False)
    key: bytes | None = field(default=None, repr=False)
    cert: bytes | None = field(default=None, repr=False)


def tls_variables(signal: str) -> tuple[str, ...]:
    """Every TLS file variable of a signal (masked during exporter construction)."""
    return tuple(name for suffix in ("CERTIFICATE", "CLIENT_KEY", "CLIENT_CERTIFICATE")
                 for name in (f"OTEL_EXPORTER_OTLP_{signal.upper()}_{suffix}", f"OTEL_EXPORTER_OTLP_{suffix}"))


def _errno_name(err: OSError) -> str:
    return errno.errorcode.get(err.errno or 0, "read error")


def _has_certificate(content: bytes) -> bool:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        ctx.load_verify_locations(cadata=content.decode("latin-1"))
    except (ssl.SSLError, ValueError):
        return False
    return ctx.cert_store_stats().get("x509", 0) > 0


def _first_file(signal: str, suffix: str, warn: Warn, is_ca: bool) -> tuple[str, str, bytes] | None:
    candidates = [v for v in (f"OTEL_EXPORTER_OTLP_{signal.upper()}_{suffix}", f"OTEL_EXPORTER_OTLP_{suffix}")
                  if (os.environ.get(v) or "").strip(_TRIM)]
    for i, var in enumerate(candidates):
        last = i == len(candidates) - 1
        nxt = ("the system roots" if is_ca else "none") if last else "the generic variable"
        path = os.environ[var].strip(_TRIM)
        try:
            with open(path, "rb") as f:
                content = f.read()
        except OSError as e:
            warn(f"tls-read:{var}", "[OtelHelper] %s: TLS file cannot be read (%s); ignored, using %s.",
                 var, _errno_name(e), nxt)
            continue
        if is_ca and not _has_certificate(content):
            warn(f"tls-pem:{var}", "[OtelHelper] %s: TLS CA file holds no PEM certificate; ignored, using %s.", var, nxt)
            continue
        return var, path, content
    return None


def _pair_accepted(cert_path: str, key_path: str) -> bool:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        # A password callback returning nothing: an encrypted key fails instead of prompting.
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path, password=lambda: b"")
    except (ssl.SSLError, ValueError, OSError):
        return False
    return True


def resolve_tls(signal: str, warn: Warn) -> TlsFiles:
    """Resolve and validate the TLS files of a TLS endpoint; never raises."""
    ca = _first_file(signal, "CERTIFICATE", warn, True)
    key = _first_file(signal, "CLIENT_KEY", warn, False)
    cert = _first_file(signal, "CLIENT_CERTIFICATE", warn, False)
    ca_kw = {"ca_path": ca[1], "ca": ca[2]} if ca else {}
    pair = ",".join(x[0] for x in (key, cert) if x)
    if key is None and cert is None:
        return TlsFiles(**ca_kw)
    if key is None or cert is None:
        warn(f"tls-pair:{pair}", "[OtelHelper] %s: client key and client certificate must both be set and "
             "readable; the client certificate is not used (server authentication).", pair)
        return TlsFiles(**ca_kw)
    if not _pair_accepted(cert[1], key[1]):
        warn(f"tls-pair-rejected:{pair}", "[OtelHelper] %s: client key and certificate rejected by TLS (bad PEM, "
             "mismatch or encrypted key); the client certificate is not used (server authentication).", pair)
        return TlsFiles(**ca_kw)
    return TlsFiles(key_path=key[1], cert_path=cert[1], key=key[2], cert=cert[2], **ca_kw)
