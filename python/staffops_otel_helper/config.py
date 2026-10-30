"""Configuration for OtelHelper: options, env resolution and validation.

Resolution contract: ``specs/config-contract-v0.3/design.md`` D1 (incl. "D1
edge-case rulings"). Precedence is explicit code > standard ``OTEL_*`` env >
legacy / ``OTEL_HELPER_*`` env > helper default. OTLP settings the SDK already
resolves per spec (absolute ``http(s)://`` endpoints, per-signal endpoints,
certificates) are left to the SDK (ADR-001); the helper only passes explicit
exporter arguments for the inputs it owns: code values, schemeless endpoints,
portless gRPC URLs, TLS conflicts, URL userinfo/query, headers, timeouts
(milliseconds, D1.e) and compression (gzip default, D1.d).

Parsing rule (R8): unknown or unparseable **env** values log a warning and fall
back (per-signal → generic → default); invalid **code** values raise
``ValueError`` naming the option. Diagnostics go through the ``staffops_otel_helper.*``
loggers and never print ``*_HEADERS``/``*_CLIENT_KEY`` values or URL
userinfo/query strings.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, cast
import re
from urllib.parse import unquote, unquote_to_bytes

import idna

from staffops_otel_helper._diag import warn_once_per_process
from staffops_otel_helper._tls import TlsFiles, resolve_tls, tls_variables

_log = logging.getLogger("staffops_otel_helper.config")

# Exactly what both helper languages trim (D1 edge-case rulings): not Unicode
# whitespace, which str.strip() would also remove.
TRIM_CHARS = " \t\n\r\v\f"


class DeploymentEnvironment(Enum):
    LOCAL = "LOCAL"
    DEV = "DEV"
    HML = "HML"
    PRD = "PRD"


# Environment variable names
ENV_COLLECTOR_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
ENV_SERVICE_NAME = "SERVICE_NAME"
ENV_OTEL_SERVICE_NAME = "OTEL_SERVICE_NAME"
ENV_ENVIRONMENT = "ENVIRONMENT"
ENV_DEBUG_LEVEL = "OTEL_HELPER_DEBUG_LEVEL"
ENV_EXTRA_INSTRUMENTATION = "OTEL_HELPER_EXTRA_INSTRUMENTATION"
ENV_SAMPLE_RATIO = "OTEL_HELPER_SAMPLE_RATIO"
ENV_DISABLED_SIGNALS = "OTEL_HELPER_DISABLED_SIGNALS"
ENV_DISABLED_METRICS = "OTEL_HELPER_DISABLED_METRICS"
ENV_METRICS_PORT = "OTEL_HELPER_METRICS_PORT"
ENV_INSECURE = "OTEL_EXPORTER_OTLP_INSECURE"
ENV_METRICS_EXPORTER = "OTEL_METRICS_EXPORTER"
ENV_METRIC_EXPORT_INTERVAL = "OTEL_METRIC_EXPORT_INTERVAL"
ENV_TRACES_SAMPLER = "OTEL_TRACES_SAMPLER"
ENV_TRACES_SAMPLER_ARG = "OTEL_TRACES_SAMPLER_ARG"
ENV_OTLP_PROTOCOL = "OTEL_EXPORTER_OTLP_PROTOCOL"
ENV_SDK_DISABLED = "OTEL_SDK_DISABLED"
ENV_RESOURCE_ATTRIBUTES = "OTEL_RESOURCE_ATTRIBUTES"
ENV_RESOURCE_DETECTORS = "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS"

ATTR_SERVICE_NAME = "service.name"
ATTR_ENVIRONMENT = "deployment.environment.name"
ATTR_INSTANCE_ID = "service.instance.id"

SIGNALS = ("traces", "metrics", "logs")
_DEFAULT_OTLP_PORT = 4317
_DEFAULT_OTLP_HTTP_PORT = 4318
_DEFAULT_EXPORT_INTERVAL_MS = 30_000
_DEFAULT_METRICS_PORT = 9464
_DEFAULT_EXTRA_INSTRUMENTATION = "SQL"
_DEFAULT_TIMEOUT_MS = 10_000.0
_SECONDS_LIKE_TIMEOUT_MS = 100
_VALID_METRIC_EXPORTERS = ("otlp", "prometheus", "console", "none")
_VALID_SPAN_LOG_EXPORTERS = ("otlp", "console", "none")
_VALID_COMPRESSIONS = ("gzip", "none")
PROTOCOL_GRPC = "grpc"
PROTOCOL_HTTP = "http/protobuf"
_VALID_OTLP_PROTOCOLS = (PROTOCOL_GRPC, PROTOCOL_HTTP)
_MIN_TIMEOUT_MS = 1.0
_MAX_TIMEOUT_MS = 24 * 3600 * 1000.0
_DECIMAL_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")  # plain decimal ms: no sign/exponent/hex/bare dot
_HEADER_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# A "%" not followed by two hex digits (headers, OTEL_RESOURCE_ATTRIBUTES, endpoint paths).
_BAD_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_PRINTABLE_ASCII_RE = re.compile(r"^[\x20-\x7e]*$")
_MAX_CODE_TIMEOUT_MS = 86_400_000  # D1 ruling "Code export timeout bound" (A18): the env clamp range
#: Export-timeout variables the helper does not honor (D1 ruling "Export deadline", A18).
IGNORED_TIMEOUT_VARS = ("OTEL_BSP_EXPORT_TIMEOUT", "OTEL_BLRP_EXPORT_TIMEOUT", "OTEL_METRIC_EXPORT_TIMEOUT")
_VALUE_LENGTH_VARS = ("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT",
                      "OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT")
UNPARSEABLE_URL = "<unparseable URL>"
_OPAQUE_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:/")


def _has_control(text: str) -> bool:
    """Byte < 0x20 or 0x7F (D1 edge-case rulings: endpoints and header values)."""
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)


def _signal_var(suffix: str, signal: str) -> str:
    """``OTEL_EXPORTER_OTLP_<SIGNAL>_<SUFFIX>``, e.g. suffix ``ENDPOINT``."""
    return f"OTEL_EXPORTER_OTLP_{signal.upper()}_{suffix}"


def _env(name: str) -> str | None:
    """Stripped env value; empty or whitespace-only counts as unset."""
    value = os.environ.get(name)
    if value is None or not value.strip(TRIM_CHARS):
        return None
    return value.strip(TRIM_CHARS)



def _non_blank(value: object) -> str | None:
    """A string trimmed with the contract set, or None when it is not a string or is blank."""
    if not isinstance(value, str):
        return None
    return value.strip(TRIM_CHARS) or None


def _percent_decode(value: str) -> str | None:
    """Strict percent-decoding (``decodeURIComponent``); None on a bad escape or non-UTF-8 result."""
    if _BAD_ESCAPE_RE.search(value):
        return None
    try:
        return unquote_to_bytes(value).decode("utf-8")
    except UnicodeError:  # not UTF-8 once decoded, or a surrogate-escaped raw byte
        return None


def _is_utf8(value: str) -> bool:
    """False for a str carrying surrogate-escaped raw bytes (``os.environ`` decodes non-UTF-8 that way)."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@dataclass(frozen=True)
class ParsedResourceAttributes:
    """Result of :func:`parse_resource_attributes`."""

    attributes: dict[str, str]
    #: 1-based positions of items without ``=`` or with an empty key (skipped).
    malformed: tuple[int, ...] = ()
    #: 1-based positions whose value is an invalid percent-encoding or not UTF-8 (kept literally).
    undecodable: tuple[int, ...] = ()


def parse_resource_attributes(raw: str) -> ParsedResourceAttributes:
    """Parse ``OTEL_RESOURCE_ATTRIBUTES`` (design D1 row of 2026-10-29).

    The helper owns this parse: the SDK's ``OTELResourceDetector`` logs invalid items
    verbatim (R8) and trims Unicode whitespace. Split on ``,``; blank items are ignored;
    each item splits at its first ``=``; the key is trimmed with the contract set (an
    empty or non-UTF-8 key makes the item malformed); the value is percent-decoded, then
    trimmed. A bad escape or a decode that is not UTF-8
    keeps the value literal (still trimmed). Callers report positions only, never values.
    """
    attributes: dict[str, str] = {}
    malformed: list[int] = []
    undecodable: list[int] = []
    for position, item in enumerate(raw.split(","), start=1):
        if not item.strip(TRIM_CHARS):
            continue
        key, sep, value = item.partition("=")
        key = key.strip(TRIM_CHARS)
        if not sep or not key or not _is_utf8(key):
            malformed.append(position)
            continue
        decoded = _percent_decode(value)
        if decoded is None:
            undecodable.append(position)
            # A raw env byte that is not UTF-8 arrives surrogate-escaped; OTLP strings must be UTF-8.
            decoded = value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        attributes[key] = decoded.strip(TRIM_CHARS)
    return ParsedResourceAttributes(attributes, tuple(malformed), tuple(undecodable))


# Deprecated environment variables (R8/D8): warned once per process, removed in 1.0.
# Values: the replacement named in the warning. Values are never printed.
_DEPRECATED_ENV = {
    ENV_SERVICE_NAME: "OTEL_SERVICE_NAME",
    ENV_ENVIRONMENT: "OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<value>",
    ENV_SAMPLE_RATIO: "OTEL_TRACES_SAMPLER=parentbased_traceidratio with OTEL_TRACES_SAMPLER_ARG=<ratio>",
    ENV_DISABLED_SIGNALS: "OTEL_TRACES_EXPORTER / OTEL_METRICS_EXPORTER / OTEL_LOGS_EXPORTER=none",
    ENV_EXTRA_INSTRUMENTATION: ("staffops_otel_helper.ext.sql.instrument_sql() / .redis.instrument_redis() / "
                                ".aws.instrument_aws(), called before setup_telemetry()"),
}


def _warn_deprecated_env() -> None:
    """One warning per deprecated variable that is set, naming its replacement.

    Warned whether or not the variable takes effect (a standard variable or a
    code value may win). Whitespace-only counts as unset, except for
    ``OTEL_HELPER_EXTRA_INSTRUMENTATION``, whose empty value still turns the
    default ``SQL`` off.
    """
    for var, replacement in _DEPRECATED_ENV.items():
        is_set = os.environ.get(var) is not None if var == ENV_EXTRA_INSTRUMENTATION else _env(var) is not None
        if is_set:
            warn_once_per_process(_log, f"deprecated:{var}",
                                  "[OtelHelper] %s is deprecated and will be removed in 1.0; use %s.",
                                  var, replacement)


# "<scheme>://<rest>": the only URL form an endpoint may take (D1 ruling: RFC 3986).
_HIER_URL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://(.*)$", re.S)
_DIGITS_RE = re.compile(r"[0-9]+")
# RFC 3986 reg-name characters (unreserved / sub-delims); pct-encoded is rejected (host-characters ruling).
_REG_NAME_RE = re.compile(r"^[A-Za-z0-9\-._~!$&'()*+,;=]+$")
_MAX_PORT = 65535
_HEX_NUMBER_RE = re.compile(r"0[xX][0-9A-Fa-f]*")
_OCTET = r"(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])"
_CANONICAL_IPV4_RE = re.compile(rf"{_OCTET}(\.{_OCTET}){{3}}")
# Shown for a schemeless endpoint whose path or query holds an "@" (D1 ruling, A17 final round), in every language.
SCHEMELESS_AT_RENDERING = "<schemeless endpoint>"


@dataclass(frozen=True)
class _RfcEndpoint:
    """An endpoint split by RFC 3986 (D1 ruling, 2026-10-29); nothing decoded.

    The authority ends only at ``/``, ``?`` or ``#``; userinfo is cut at its last
    ``@``. ``host`` is as written (IPv6 in brackets, may be empty); ``port`` is
    numeric (leading zeros compare numerically); ``path`` keeps escapes verbatim;
    ``query``/``fragment`` are None when absent and "" when empty.
    """

    scheme: str
    userinfo: bool
    host: str
    port: int | None
    port_text: str  # as written ("" when none)
    path: str
    query: str | None
    fragment: str | None


def _rfc_split(text: str) -> _RfcEndpoint | None:
    """Split ``scheme://…``; None when the authority is malformed (brackets, a second ``:``, port not 1-65535)."""
    m = _HIER_URL_RE.match(text)
    if m is None:
        return None
    rest = m.group(2)
    end = min((i for i in (rest.find(c) for c in "/?#") if i >= 0), default=len(rest))
    authority, after = rest[:end], rest[end:]
    after, sep, fragment_text = after.partition("#")
    fragment = fragment_text if sep else None
    after, sep, query_text = after.partition("?")
    query = query_text if sep else None
    at = authority.rfind("@")
    host_port = authority[at + 1:]
    port_text: str | None = None
    if host_port.startswith("["):
        close = host_port.find("]")
        if close < 0:
            return None
        host, tail = host_port[:close + 1], host_port[close + 1:]
        if tail:
            if not tail.startswith(":"):
                return None
            port_text = tail[1:]
    else:
        host, sep, port_text = host_port.partition(":")
    port = None
    # An empty port ("h:") is the scheme default, as RFC 3986 allows.
    if port_text:
        if not _DIGITS_RE.fullmatch(port_text):
            return None
        # Compared numerically (leading zeros ignored, as in Go); never int() a long
        # string (Python refuses more than 4300 digits).
        digits = port_text.lstrip("0")
        if not digits or len(digits) > 5:
            return None
        port = int(digits)
        if port > _MAX_PORT:
            return None
    return _RfcEndpoint(m.group(1).lower(), at >= 0, host, port, port_text or "", after, query, fragment)


def _ends_in_number(host: str) -> bool:
    """WHATWG "ends in a number": the last label (a trailing empty one ignored) is digits or hex (0x...)."""
    labels = host.split(".")
    if len(labels) > 1 and labels[-1] == "":
        labels.pop()
    last = labels[-1]
    return bool(last) and (bool(_DIGITS_RE.fullmatch(last)) or bool(_HEX_NUMBER_RE.fullmatch(last)))


def _check_host(host: str) -> tuple[str | None, bool]:
    """Validated host of an http(s) endpoint, as dialed and rendered, and whether it held a zone id.

    D1 rulings "Host characters", IDN hosts and "One host rule" (2026-10-29):
    an empty host, a space, a ``%`` (percent-encoded byte or unescaped IPv6 zone
    id) or a host that fails IDNA conversion is invalid (None); a non-ASCII host
    is punycoded (UTS #46 non-transitional). The ASCII result must be an IPv6
    literal or an RFC 3986 reg-name, and a host that ends in a number must be a
    canonical dotted-quad IPv4 address. Hosts are lowercased.
    """
    if not host or " " in host:
        return None, False
    if "%" in host:
        return None, host.startswith("[")
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            return None, False
        return host.lower(), False
    if not host.isascii():
        try:
            # UTS #46 non-transitional mapping, then IDNA 2008 validation: stricter than
            # WHATWG/Go for some labels (D1 row "IDN hosts: Python residual divergence").
            host = idna.encode(host, uts46=True, transitional=False).decode("ascii")
        except (idna.IDNAError, UnicodeError):
            return None, False
        # Also checked explicitly (Go and Node do the same): no empty label, <= 253 characters.
        trimmed = host.removesuffix(".")
        if not trimmed or len(trimmed) > 253 or "" in trimmed.split("."):
            return None, False
    if not _REG_NAME_RE.match(host):
        return None, False
    # One trailing "." is allowed on a dotted quad ("10.0.0.1."), as for a domain.
    if _ends_in_number(host) and not _CANONICAL_IPV4_RE.fullmatch(host.removesuffix(".")):
        return None, False
    return host.lower(), False


def _has_at(text: str) -> bool:
    """An ``@``, raw or percent-encoded (``%40``)."""
    return "@" in text or "%40" in text.lower()


def _path_has_at(sp: _RfcEndpoint) -> bool:
    """An ``@`` (or ``%40``) in the path or query: misplaced credentials (D1 ruling: warned, used as parsed)."""
    return _has_at(sp.path) or _has_at(sp.query or "")


def _endpoint_port(raw: str) -> int | None:
    """Port of an endpoint, schemeless or URL (None if none or unparseable), for the 4318 inference."""
    if _has_control(raw) or "\\" in raw:
        return None
    sp = _rfc_split(raw if "://" in raw else f"scheme://{raw}")
    return None if sp is None else sp.port


def redact_url(raw: str, protocol: str | None = None) -> str:
    """Endpoint text safe for diagnostics: scheme, host, port and path only.

    No userinfo, query or fragment; split exactly as the resolver splits it
    (RFC 3986). When the path or query holds an ``@`` (or ``%40``) only
    ``<scheme>://`` is shown (``<schemeless endpoint>`` for a schemeless value);
    an opaque value renders as its scheme only. Anything the resolver would
    reject for ``protocol`` (with http/protobuf, an invalid path escape too)
    renders as ``<unparseable URL>`` (R8, D1 rulings).
    """
    s = raw.strip(TRIM_CHARS)
    if not s or _has_control(s) or "\\" in s:
        return UNPARSEABLE_URL
    schemeless = "://" not in s
    if schemeless and _OPAQUE_SCHEME_RE.match(s):
        # "unix:/path": no authority, and the rest may hold credentials (D1 ruling, A17).
        return f"{s.split(':', 1)[0].lower()}:"
    if schemeless and s.startswith("/"):
        return UNPARSEABLE_URL
    sp = _rfc_split(f"https://{s}" if schemeless else s)
    if sp is None:
        return UNPARSEABLE_URL
    host: str | None = sp.host
    if sp.scheme in ("http", "https"):
        host, _ = _check_host(sp.host)
        if host is None:
            return UNPARSEABLE_URL
    if protocol == PROTOCOL_HTTP and _BAD_ESCAPE_RE.search(sp.path):
        return UNPARSEABLE_URL
    if _path_has_at(sp):
        return SCHEMELESS_AT_RENDERING if schemeless else f"{sp.scheme}://"
    host_port = host if sp.port is None else f"{host}:{sp.port}"
    return f"{host_port}{sp.path}" if schemeless else f"{sp.scheme}://{host_port}{sp.path}"


@dataclass(frozen=True)
class _Endpoint:
    """A usable endpoint value: ``scheme`` None when schemeless; ``host`` normalized (brackets kept)."""

    scheme: str | None
    host: str
    port: int | None
    path: str
    userinfo: bool
    query: str | None
    fragment: str | None
    # The host or port is spelled differently from the raw value (IDN, leading zeros):
    # the helper passes the URL explicitly so the SDK never reads the raw spelling.
    rewritten: bool
    at_in_path: bool


def _parse_environment(value: str) -> DeploymentEnvironment | str:
    """Known values (case-insensitive) map to the enum; anything else is kept verbatim (ADR-003)."""
    stripped = value.strip(TRIM_CHARS)
    try:
        return DeploymentEnvironment(stripped.upper())
    except ValueError:
        return stripped


def get_default_log_level(
    environment: DeploymentEnvironment | str | None, debug_level: bool = False
) -> int:
    """Default log level: LOCAL DEBUG, DEV/HML INFO, PRD WARNING, unset/unknown INFO (R4)."""
    if debug_level:
        return logging.DEBUG
    if isinstance(environment, str):
        environment = _parse_environment(environment)
    if not isinstance(environment, DeploymentEnvironment):
        return logging.INFO
    return {
        DeploymentEnvironment.LOCAL: logging.DEBUG,
        DeploymentEnvironment.DEV: logging.INFO,
        DeploymentEnvironment.HML: logging.INFO,
        DeploymentEnvironment.PRD: logging.WARNING,
    }[environment]


def _decode_header_value(value: str) -> tuple[str, bool]:
    """Percent-decode a header value; (value, ok). Any bad escape or non-UTF-8 → whole value literal."""
    if _BAD_ESCAPE_RE.search(value):
        return value, False
    try:
        return unquote(value, errors="strict"), True
    except UnicodeDecodeError:
        return value, False


def _parse_headers(raw: str) -> tuple[dict[str, str], list[int], list[int]]:
    """W3C-baggage-style ``k=v,k2=v2`` (D1 edge-case rulings, header details).

    Keys must match ``^[A-Za-z0-9._-]+$`` (not percent-decoded), compared
    case-insensitively, last one wins; values are percent-decoded — an invalid
    escape or non-UTF-8 result keeps the whole value literal; a value with a
    control character is skipped. Returns (headers, skipped positions,
    positions kept literal), 1-based.
    """
    headers: dict[str, str] = {}
    bad: list[int] = []
    bad_escape: list[int] = []
    for pos, item in enumerate(raw.split(","), start=1):
        key, sep, value = item.partition("=")
        key = key.strip(TRIM_CHARS)
        if not sep or not _HEADER_KEY_RE.match(key):
            bad.append(pos)
            continue
        decoded, ok = _decode_header_value(value)
        decoded = decoded.strip(TRIM_CHARS)
        if not _PRINTABLE_ASCII_RE.match(decoded):  # control or non-ASCII (D1 "Header bytes", A18)
            bad.append(pos)
            continue
        if not ok:
            bad_escape.append(pos)
        headers[key.lower()] = decoded
    return headers, bad, bad_escape


@dataclass(frozen=True)
class SignalExport:
    """How one signal's OTLP exporter is built.

    ``kwargs`` holds only the exporter arguments the helper owns: ``endpoint``
    (and ``insecure`` for gRPC) when the helper decided the URL, ``headers``
    when an ``*_HEADERS`` env var is set, ``timeout`` in seconds when code or a
    ``*_TIMEOUT`` env var set it, and ``compression`` (always: code > env >
    ``gzip``). Everything else is resolved by the SDK. ``masked_env`` lists env
    vars hidden while the exporter is constructed because the SDK would crash
    parsing their (already warned) invalid value.
    """

    signal: str
    protocol: str
    source: Literal["code", "env"]
    kwargs: dict[str, object] = field(default_factory=dict)
    masked_env: tuple[str, ...] = ()
    #: The signal's single export deadline (D1 ruling "Export deadline", A18):
    #: passed to the exporter and to its batch processor or periodic reader.
    timeout_ms: float = _DEFAULT_TIMEOUT_MS
    #: Validated TLS files of a TLS endpoint (None: plaintext); the TLS variables are masked.
    tls: TlsFiles | None = None


@dataclass(frozen=True)
class ResolvedOptions:
    """Effective values of the options code may leave unset (``TelemetryOptions.resolved``).

    Code value if set (whatever it is), else the environment, else the helper
    default (design D1, A13 ruling). Read-only: change the ``TelemetryOptions``
    field instead.
    """

    debug_level: bool
    extra_instrumentation: str
    sample_ratio: float
    # True when sample_ratio came from code or OTEL_HELPER_SAMPLE_RATIO: the
    # sampler is then ParentBased(TraceIdRatioBased(sample_ratio)).
    sample_ratio_set: bool
    disabled_signals: list[str]
    disabled_metrics: list[str]
    prometheus_metrics_port: int
    export_interval_ms: int
    # None = not set anywhere: inferred (otlp with a metrics endpoint, else prometheus).
    metric_exporters: list[str] | None


# Fields whose public value is only ever the code value; resolution stores the
# effective value in TelemetryOptions._effective (never back into the field), so
# dataclasses.replace() and a second setup_telemetry() with the same object
# re-read the environment for everything code left unset (A13 review H1/M1).
_CODE_ONLY_DEFAULTS: dict[str, object] = {
    "debug_level": False,
    "extra_instrumentation": _DEFAULT_EXTRA_INSTRUMENTATION,
    "sample_ratio": 1.0,
    "disabled_signals": [],
    "disabled_metrics": [],
    "prometheus_metrics_port": _DEFAULT_METRICS_PORT,
    "export_interval_ms": _DEFAULT_EXPORT_INTERVAL_MS,
    "metric_exporters": None,
}


@dataclass
class TelemetryOptions:
    """Configuration options for OtelHelper. Env vars fill defaults; explicit values take priority.

    ``None`` / empty means "not set in code"; any other value set here wins over
    the environment, also when it equals the helper default (``sample_ratio=1.0``,
    ``prometheus_metrics_port=9464``, ``disabled_metrics=[]``, ``debug_level=False``
    — design D1 edge-case rulings). Resolution never writes these fields back:
    read the effective values from ``resolved`` (code, else env, else default).
    Endpoints set here are taken as
    given: a generic ``otel_endpoint`` gets ``/v1/<signal>`` appended for
    ``http/protobuf``; ``traces_endpoint``/``metrics_endpoint``/``logs_endpoint``
    are used verbatim. After ``setup_telemetry()``, ``service_name`` and
    ``environment`` hold what was resolved: ``None`` when nothing provides them,
    and ``environment`` is a plain ``str`` for values outside LOCAL/DEV/HML/PRD.
    """

    service_name: str | None = None
    environment: DeploymentEnvironment | str | None = None
    otel_endpoint: str = ""
    traces_endpoint: str = ""
    metrics_endpoint: str = ""
    logs_endpoint: str = ""
    # Debug mode. None = OTEL_HELPER_DEBUG_LEVEL, else False.
    debug_level: bool | None = None
    # Deprecated. None = OTEL_HELPER_EXTRA_INSTRUMENTATION, else "SQL".
    extra_instrumentation: str | None = None
    # Export timeout (ms). None = OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT (ms), else the SDK default (10 s).
    export_timeout_ms: int | None = None
    # OTLP compression: "gzip" or "none". None = env, else "gzip" (design D1.d).
    compression: str | None = None
    # Head-sampling ratio for root spans: ParentBased(TraceIdRatioBased(ratio)), winning over
    # OTEL_TRACES_SAMPLER whatever the value (1.0 included). None = env (the deprecated
    # OTEL_HELPER_SAMPLE_RATIO only while OTEL_TRACES_SAMPLER is unset); the effective value
    # is resolved.sample_ratio (this field keeps the code value).
    sample_ratio: float | None = None
    # Signals to disable. None = OTEL_HELPER_DISABLED_SIGNALS (deprecated); [] = none, over the env.
    disabled_signals: list[str] | None = None
    # Instrument-name patterns to drop. None = OTEL_HELPER_DISABLED_METRICS; [] = none, over the env.
    disabled_metrics: list[str] | None = None
    # Metric exporters: "otlp", "prometheus", "console", "none". None = resolve from
    # OTEL_METRICS_EXPORTER, falling back to legacy inference (otlp when a metrics
    # endpoint is configured, prometheus otherwise). Explicit value wins over env.
    metric_exporters: list[str] | None = None
    # Metric export interval (ms). None = OTEL_METRIC_EXPORT_INTERVAL, else 30000.
    export_interval_ms: int | None = None
    # Standalone /metrics port (0 = no listener). None = OTEL_HELPER_METRICS_PORT, else 9464.
    prometheus_metrics_port: int | None = None
    minimum_log_level: int | None = None
    resource_attributes: dict[str, object] = field(default_factory=dict)
    # Plaintext for a schemeless endpoint set in code. Contradicting a code URL's scheme raises.
    insecure: bool | None = None
    # OTLP wire protocol: "grpc" or "http/protobuf". None = resolve per signal from
    # OTEL_EXPORTER_OTLP_<SIGNAL>_PROTOCOL / OTEL_EXPORTER_OTLP_PROTOCOL, falling back
    # to port inference (4318 -> http/protobuf, else grpc). "http/json" is not supported.
    otlp_protocol: str | None = None
    # HTTP paths whose server spans are dropped by the sampler (R7, design D5): exact,
    # case-insensitive, trailing slash and query ignored. None = DEFAULT_HEALTH_PATHS;
    # [] = no path is dropped. gRPC grpc.health.v1.Health/* is always excluded.
    health_paths: list[str] | None = None

    # Resolution results (filled by resolve_from_env; not constructor arguments).
    _otlp: dict[str, SignalExport] = field(default_factory=dict, init=False, repr=False)
    _span_log_exporters: dict[str, list[str]] = field(default_factory=dict, init=False, repr=False)
    _resource_overrides: dict[str, object] = field(default_factory=dict, init=False, repr=False)
    _env_resource: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _warned: set[str] = field(default_factory=set, init=False, repr=False)
    _resolved: bool = field(default=False, init=False, repr=False)
    _sdk_disabled: bool = field(default=False, init=False, repr=False)
    _setup_disabled: set[str] = field(default_factory=set, init=False, repr=False)
    # Effective values of the _CODE_ONLY_DEFAULTS fields (+ "sample_ratio_set"), rebuilt
    # by every resolve_from_env(); empty before the first one.
    _effective: dict[str, object] = field(default_factory=dict, init=False, repr=False)

    # ── public queries ────────────────────────────────────────────────────

    def _value(self, name: str):
        """Effective value: resolved, else (before resolution) code value, else default."""
        if name in self._effective:
            return self._effective[name]
        code = getattr(self, name)
        return _CODE_ONLY_DEFAULTS[name] if code is None else code

    @property
    def resolved(self) -> ResolvedOptions:
        """The effective option values (code > env > default) — see ``ResolvedOptions``."""
        values = {name: self._value(name) for name in _CODE_ONLY_DEFAULTS}
        values["debug_level"] = bool(values["debug_level"])
        for name in ("disabled_signals", "disabled_metrics", "metric_exporters"):
            if values[name] is not None:
                values[name] = list(values[name])
        sample_set = self._effective.get("sample_ratio_set", self.sample_ratio is not None)
        return ResolvedOptions(sample_ratio_set=bool(sample_set), **values)

    def has_instrumentation(self, name: str) -> bool:
        if self._value("debug_level"):
            return True
        extra = self._value("extra_instrumentation")
        return name.upper() in [x.strip(TRIM_CHARS).upper() for x in extra.split(",") if x.strip(TRIM_CHARS)]

    def is_signal_enabled(self, signal: str) -> bool:
        """Check if a signal (traces, metrics, logs) is enabled.

        False also for a signal setup turned off itself (design D13: traces and
        metrics after setup → shutdown → setup in one process).
        """
        signal = signal.lower()
        return signal not in self._setup_disabled and signal not in [s.lower() for s in self._value("disabled_signals")]

    @property
    def sdk_disabled(self) -> bool:
        """``OTEL_SDK_DISABLED=true``: setup installs propagators only."""
        return self._sdk_disabled

    def otlp_export(self, signal: str) -> SignalExport | None:
        """The resolved OTLP exporter plan for a signal; None = OTLP not configured for it."""
        return self._otlp.get(signal)

    def is_otlp_configured(self, signal: str) -> bool:
        """Per signal (D1): code endpoint, generic env endpoint, or that signal's env endpoint."""
        return signal in self._otlp

    def resolved_exporters(self, signal: str) -> list[str]:
        """Active exporters for ``traces``/``logs`` (from ``OTEL_<SIGNAL>_EXPORTER``, default ``otlp``)."""
        if signal == "metrics":
            return self.resolved_metric_exporters()
        exporters = self._span_log_exporters.get(signal) or ["otlp"]
        return [] if exporters == ["none"] else list(exporters)

    def resolved_metric_exporters(self) -> list[str]:
        """Active metric exporters after resolution. "none" resolves to an empty list."""
        exporters = self._value("metric_exporters")
        if exporters is None:
            return ["otlp"] if self.is_otlp_configured("metrics") else ["prometheus"]
        if exporters == ["none"]:
            return []
        return list(exporters)

    def resolved_otlp_protocol(self, signal: str = "traces") -> str:
        """OTLP wire protocol for a signal: code > per-signal env > generic env > port 4318 > grpc."""
        plan = self._otlp.get(signal)
        if plan is not None:
            return plan.protocol
        candidates = self._endpoint_candidates(signal) if self._resolved else [(self.otel_endpoint, False, "code", "")]
        raw = candidates[0][0] if candidates else ""
        return self._protocol_for(signal, raw or "", warn=False)

    # ── diagnostics ───────────────────────────────────────────────────────

    def _warn_once(self, key: str, msg: str, *args: object) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        _log.warning(msg, *args)

    def _info_once(self, key: str, msg: str, *args: object) -> None:
        if key not in self._warned:
            self._warned.add(key)
            _log.info(msg, *args)

    def _env_bool(self, name: str) -> bool | None:
        """Spec boolean: case-insensitive ``true`` → True; anything else → False (warned unless ``false``)."""
        raw = _env(name)
        if raw is None:
            return None
        low = raw.lower()
        if low not in ("true", "false"):
            self._warn_once(f"bool:{name}", "[OtelHelper] %s=%r is not a boolean (true/false); "
                            "treating it as false.", name, raw)
        return low == "true"

    # ── code validation (fail fast, before anything uses the values) ──────

    def _validate_code(self) -> None:
        def fail(option: str, why: str) -> None:
            raise ValueError(f"[OtelHelper] {option} {why}.")

        def check_str(option: str, value: object, allow_none: bool = True) -> None:
            if value is None and allow_none:
                return
            if not isinstance(value, str):
                fail(option, f"must be a string, got {type(value).__name__}")

        if not isinstance(self.resource_attributes, dict):
            fail("resource_attributes", f"must be a dict, got {type(self.resource_attributes).__name__}")
        for key, value in self.resource_attributes.items():
            if not isinstance(key, str):
                fail("resource_attributes", f"keys must be strings, got {type(key).__name__}")
            if not key.strip(TRIM_CHARS):
                fail("resource_attributes", "has an empty key")
            if key in (ATTR_SERVICE_NAME, ATTR_ENVIRONMENT, ATTR_INSTANCE_ID) and not isinstance(value, str):
                fail(f"resource_attributes[{key!r}]", f"must be a string, got {type(value).__name__}")
        check_str("service_name", self.service_name)
        if isinstance(self.service_name, str) and not self.service_name.strip(TRIM_CHARS):
            fail("service_name", "must not be empty")
        if self.environment is not None and not isinstance(self.environment, DeploymentEnvironment):
            if not isinstance(self.environment, str) or not self.environment.strip(TRIM_CHARS):
                fail("environment", "must be a DeploymentEnvironment or a non-empty string")
        for option in ("otel_endpoint", "traces_endpoint", "metrics_endpoint", "logs_endpoint"):
            check_str(option, getattr(self, option), allow_none=False)
        # An empty string is not set (A13 ruling; D1 row of 2026-10-29): env, then default apply.
        check_str("otlp_protocol", self.otlp_protocol)
        if _non_blank(self.otlp_protocol) is not None \
                and self.otlp_protocol.strip(TRIM_CHARS).lower() not in _VALID_OTLP_PROTOCOLS:
            fail("otlp_protocol", f"has unknown OTLP protocol '{self.otlp_protocol}' "
                 f"(Unknown OTLP protocol; valid values: {', '.join(_VALID_OTLP_PROTOCOLS)})")
        check_str("compression", self.compression)
        if _non_blank(self.compression) is not None \
                and self.compression.strip(TRIM_CHARS).lower() not in _VALID_COMPRESSIONS:
            fail("compression", f"'{self.compression}' is not supported (valid: {', '.join(_VALID_COMPRESSIONS)})")
        if self.insecure is not None and not isinstance(self.insecure, bool):
            fail("insecure", f"must be a bool, got {type(self.insecure).__name__}")
        for option in ("export_timeout_ms", "export_interval_ms"):
            value = getattr(self, option)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                fail(option, f"must be an integer (milliseconds), got {type(value).__name__}")
            if value <= 0:
                fail(option, "must be greater than 0")
            if option == "export_timeout_ms" and value > _MAX_CODE_TIMEOUT_MS:
                fail(option, f"must be in [1, {_MAX_CODE_TIMEOUT_MS}] ms, got {value}")
        if self.debug_level is not None:
            # bool or int (truthiness, as before 0.3); a string such as "false" would read as true.
            if not isinstance(self.debug_level, int):
                fail("debug_level", f"must be a bool, got {type(self.debug_level).__name__}")
            self.debug_level = bool(self.debug_level)
        check_str("extra_instrumentation", self.extra_instrumentation)
        ratio = self.sample_ratio
        if ratio is not None and (isinstance(ratio, bool) or not isinstance(ratio, (int, float))
                                  or math.isnan(ratio) or not 0.0 <= ratio <= 1.0):
            fail("sample_ratio", f"must be a number in [0, 1], got {ratio!r}")
        port = self.prometheus_metrics_port
        if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535):
            fail("prometheus_metrics_port", f"must be an integer in [0, 65535], got {port!r}")
        if self.disabled_signals is not None:
            if not isinstance(self.disabled_signals, (list, tuple)) or not all(
                    isinstance(s, str) for s in self.disabled_signals):
                fail("disabled_signals", "must be a list of strings")
            unknown = [s for s in self.disabled_signals if s.strip(TRIM_CHARS).lower() not in SIGNALS]
            if unknown:
                fail("disabled_signals", f"has unknown signal(s) {unknown} (valid: {', '.join(SIGNALS)})")
            self.disabled_signals = [s.strip(TRIM_CHARS).lower() for s in self.disabled_signals]
        if self.disabled_metrics is not None:
            if not isinstance(self.disabled_metrics, (list, tuple)) or not all(
                    isinstance(m, str) for m in self.disabled_metrics):
                fail("disabled_metrics", "must be a list of strings")
            self.disabled_metrics = list(self.disabled_metrics)
        if self.health_paths is not None:
            if not isinstance(self.health_paths, (list, tuple)) or not all(
                    isinstance(p, str) for p in self.health_paths):
                fail("health_paths", "must be a list of strings")
            for path in self.health_paths:
                if not path.startswith("/") or any(c in path for c in "?#,") or not path.isascii():
                    fail("health_paths", f'path {path!r} must be ASCII, start with "/" and have no query, '
                         "fragment or comma")
        if self.metric_exporters is not None:
            if not isinstance(self.metric_exporters, (list, tuple)) or not all(
                    isinstance(e, str) for e in self.metric_exporters):
                fail("metric_exporters", "must be a list or tuple of strings")
            self.metric_exporters = list(self.metric_exporters)

    # ── resolution ────────────────────────────────────────────────────────

    def resolve_from_env(self) -> None:
        """Apply environment variable defaults (PostConfigure equivalent).

        Raises ValueError for invalid values set in code; env problems only warn.
        """
        self._validate_code()
        # Every resolution starts from the code values only (A13 review M1): a second
        # setup_telemetry() with the same object re-reads the environment.
        self._effective = {}
        self._otlp = {}
        self._span_log_exporters = {}
        self._resource_overrides = {}
        _warn_deprecated_env()
        for var in IGNORED_TIMEOUT_VARS:
            if _env(var) is not None:
                self._warn_once(f"ignored-timeout:{var}", "[OtelHelper] %s is not honored: the OTLP export timeout is "
                                "the export deadline of every signal; set OTEL_EXPORTER_OTLP_TIMEOUT or "
                                "OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT.", var)
        self._env_resource = self._parse_env_resource()
        self._resolve_service_name()
        self._resolve_environment()
        self._resource_overrides[ATTR_INSTANCE_ID] = (
            _non_blank(self.resource_attributes.get(ATTR_INSTANCE_ID))
            or _non_blank(self._env_resource.get(ATTR_INSTANCE_ID)))

        # A value set in code wins whatever it is (D1 A13 ruling): only None reads the env.
        eff = self._effective
        eff["debug_level"] = self.debug_level if self.debug_level is not None \
            else bool(self._env_bool(ENV_DEBUG_LEVEL))

        if self.extra_instrumentation is not None:
            eff["extra_instrumentation"] = self.extra_instrumentation
        else:
            env_extra = os.getenv(ENV_EXTRA_INSTRUMENTATION)
            eff["extra_instrumentation"] = _DEFAULT_EXTRA_INSTRUMENTATION if env_extra is None else env_extra

        # Standard OTel var wins over the deprecated proprietary one: when
        # OTEL_TRACES_SAMPLER is set, it configures the sampler (tracing.build_sampler)
        # and OTEL_HELPER_SAMPLE_RATIO is ignored.
        eff["sample_ratio"], eff["sample_ratio_set"] = 1.0, False
        if self.sample_ratio is not None:
            eff["sample_ratio"], eff["sample_ratio_set"] = float(self.sample_ratio), True
        elif _env(ENV_TRACES_SAMPLER) is None:
            self._resolve_sample_ratio()

        self._sdk_disabled = bool(self._env_bool(ENV_SDK_DISABLED))
        self._resolve_signal_switches()

        for signal in SIGNALS:
            plan = self._resolve_signal(signal)
            if plan is not None:
                self._otlp[signal] = plan
            elif "otlp" in self._span_log_exporters.get(signal, []):
                self._warn_once(f"otlp-no-endpoint:{signal}", "[OtelHelper] OTEL_%s_EXPORTER includes 'otlp' "
                                "but no %s endpoint is configured (%s or %s); no OTLP exporter for it.",
                                signal.upper(), signal, ENV_COLLECTOR_ENDPOINT, _signal_var("ENDPOINT", signal))

        self._resolve_metric_exporters()
        self._resolve_export_interval()

        if self.disabled_metrics is not None:
            eff["disabled_metrics"] = list(self.disabled_metrics)
        else:
            env_metrics = os.getenv(ENV_DISABLED_METRICS) or ""
            eff["disabled_metrics"] = [s.strip(TRIM_CHARS) for s in env_metrics.split(",") if s.strip(TRIM_CHARS)]

        if self.prometheus_metrics_port is not None:
            eff["prometheus_metrics_port"] = self.prometheus_metrics_port
        else:
            eff["prometheus_metrics_port"] = _DEFAULT_METRICS_PORT
            env_port = _env(ENV_METRICS_PORT)
            if env_port is not None:
                try:
                    port = int(env_port)
                    if not 0 <= port <= 65535:
                        raise ValueError
                    eff["prometheus_metrics_port"] = port
                except ValueError:
                    self._warn_once(ENV_METRICS_PORT, "[OtelHelper] %s=%r is not a port (0-65535); using 9464.",
                                    ENV_METRICS_PORT, env_port)
        self._resolved = True

    def _resolve_sample_ratio(self) -> None:
        raw = _env(ENV_SAMPLE_RATIO)
        if raw is None:
            return
        if not _DECIMAL_RE.match(raw):  # plain decimal only (design D3): no sign/exponent/_ /NaN
            self._warn_once(ENV_SAMPLE_RATIO, "[OtelHelper] %s=%r is not a number; using 1.0.",
                            ENV_SAMPLE_RATIO, raw)
            return
        ratio = float(raw)
        clamped = max(0.0, min(1.0, ratio))
        if clamped != ratio:
            self._warn_once(f"{ENV_SAMPLE_RATIO}:range", "[OtelHelper] %s=%r is outside [0, 1]; using %s.",
                            ENV_SAMPLE_RATIO, raw, clamped)
        self._effective["sample_ratio"], self._effective["sample_ratio_set"] = clamped, True

    def _parse_env_resource(self) -> dict[str, str]:
        """``OTEL_RESOURCE_ATTRIBUTES`` as the helper parses it; problems warned once per process, by position only."""
        raw = os.environ.get(ENV_RESOURCE_ATTRIBUTES)
        if raw is None or not raw.strip(TRIM_CHARS):
            return {}
        parsed = parse_resource_attributes(raw)
        if parsed.malformed:
            warn_once_per_process(_log, "resource_attributes:malformed", "[OtelHelper] %s: items %s are not key=value "
                            "and were skipped.", ENV_RESOURCE_ATTRIBUTES, ",".join(map(str, parsed.malformed)))
        if parsed.undecodable:
            warn_once_per_process(_log, "resource_attributes:undecodable", "[OtelHelper] %s: the values of items %s have an "
                            "invalid percent-encoding and are kept literally.", ENV_RESOURCE_ATTRIBUTES,
                            ",".join(map(str, parsed.undecodable)))
        return parsed.attributes

    def _resolve_service_name(self) -> None:
        """code option > code attribute > OTEL_SERVICE_NAME > OTEL_RESOURCE_ATTRIBUTES > SERVICE_NAME >
        SDK default (D1). A blank value at any level is not provided."""
        if self.service_name is not None:
            self.service_name = self.service_name.strip(TRIM_CHARS)
            self._resource_overrides[ATTR_SERVICE_NAME] = self.service_name
            return
        name = (_non_blank(self.resource_attributes.get(ATTR_SERVICE_NAME)) or _env(ENV_OTEL_SERVICE_NAME)
                or _non_blank(self._env_resource.get(ATTR_SERVICE_NAME)) or _env(ENV_SERVICE_NAME))
        if name is not None:
            self.service_name = name
        else:
            warn_once_per_process(
                _log, "identity:service.name",
                "[OtelHelper] No service name configured: set %s. The SDK default "
                "(unknown_service) will be used.", ENV_OTEL_SERVICE_NAME,
            )
        # None: _build_resource() applies the SDK default.
        self._resource_overrides[ATTR_SERVICE_NAME] = name

    def _resolve_environment(self) -> None:
        """code option > code attribute > OTEL_RESOURCE_ATTRIBUTES > ENVIRONMENT (deprecated alias) >
        unset (ADR-003). A blank value at any level is not provided; emitted verbatim."""
        if self.environment is not None:
            if isinstance(self.environment, DeploymentEnvironment):
                value: str = self.environment.value
            else:
                value = self.environment.strip(TRIM_CHARS)
                self.environment = _parse_environment(value)
            self._resource_overrides[ATTR_ENVIRONMENT] = value
            return
        attr = (_non_blank(self.resource_attributes.get(ATTR_ENVIRONMENT))
                or _non_blank(self._env_resource.get(ATTR_ENVIRONMENT)))
        legacy = _env(ENV_ENVIRONMENT)
        emitted: str | None = None
        if attr is not None:
            # Behavior follows the value; the resource carries it unchanged.
            self.environment, emitted = _parse_environment(attr), attr
        elif legacy is not None:
            parsed = _parse_environment(legacy)
            self.environment = parsed
            emitted = parsed.value if isinstance(parsed, DeploymentEnvironment) else parsed
        else:
            warn_once_per_process(
                _log, "identity:environment",
                "[OtelHelper] %s is not set (OTEL_RESOURCE_ATTRIBUTES, or the legacy %s alias): the "
                "attribute is omitted and the log level defaults to INFO.", ATTR_ENVIRONMENT, ENV_ENVIRONMENT,
            )
        # None: the attribute is absent from the resource.
        self._resource_overrides[ATTR_ENVIRONMENT] = emitted

    def _parse_exporter_list(self, var: str, raw: str, valid: tuple[str, ...]) -> list[str] | None:
        """Parse ``OTEL_<SIGNAL>_EXPORTER``; returns None when no valid entry is left."""
        out: list[str] = []
        for token in (t.strip(TRIM_CHARS).lower() for t in raw.split(",")):
            if not token:
                continue
            if token == "logging" and "console" in valid:
                self._warn_once(f"{var}:logging", "[OtelHelper] %s value 'logging' is deprecated; "
                                "treating it as 'console'.", var)
                token = "console"
            elif token not in valid:
                self._warn_once(f"{var}:{token}", "[OtelHelper] %s: unknown exporter %r ignored. "
                                "Valid values: %s.", var, token, ", ".join(valid))
                continue
            if token not in out:
                out.append(token)
        if "none" in out and len(out) > 1:
            self._warn_once(f"{var}:none", "[OtelHelper] %s combines 'none' with other exporters; "
                            "treating it as 'none'.", var)
            return ["none"]
        return out or None

    def _resolve_signal_switches(self) -> None:
        """OTEL_{TRACES,LOGS}_EXPORTER; standard vars win over OTEL_HELPER_DISABLED_SIGNALS."""
        for signal in ("traces", "logs"):
            var = f"OTEL_{signal.upper()}_EXPORTER"
            raw = _env(var)
            if raw is not None:
                parsed = self._parse_exporter_list(var, raw, _VALID_SPAN_LOG_EXPORTERS)
                if parsed is not None:
                    self._span_log_exporters[signal] = parsed
        if self.disabled_signals is not None:
            self._effective["disabled_signals"] = list(self.disabled_signals)
        else:
            self._effective["disabled_signals"] = []
            env_disabled = os.getenv(ENV_DISABLED_SIGNALS)
            if env_disabled:
                standard = {s for s in SIGNALS if _env(f"OTEL_{s.upper()}_EXPORTER") is not None}
                self._effective["disabled_signals"] = [
                    s.strip(TRIM_CHARS).lower() for s in env_disabled.split(",")
                    if s.strip(TRIM_CHARS) and s.strip(TRIM_CHARS).lower() not in standard
                ]

    def _resolve_metric_exporters(self) -> None:
        if self.metric_exporters is not None:
            self.metric_exporters = [e.strip(TRIM_CHARS).lower() for e in self.metric_exporters if e.strip(TRIM_CHARS)]
            self._effective["metric_exporters"] = list(self.metric_exporters)
            return
        self._effective["metric_exporters"] = None
        raw = _env(ENV_METRICS_EXPORTER)
        if raw is None:
            return
        parsed = self._parse_exporter_list(ENV_METRICS_EXPORTER, raw, _VALID_METRIC_EXPORTERS)
        if parsed and "otlp" in parsed and not self.is_otlp_configured("metrics"):
            self._warn_once(
                f"{ENV_METRICS_EXPORTER}:otlp",
                "[OtelHelper] %s includes 'otlp' but no metrics endpoint is configured (%s or %s); "
                "ignoring 'otlp'.", ENV_METRICS_EXPORTER, ENV_COLLECTOR_ENDPOINT, _signal_var("ENDPOINT", "metrics"),
            )
            parsed = [e for e in parsed if e != "otlp"] or None
        self._effective["metric_exporters"] = parsed

    def _resolve_export_interval(self) -> None:
        if self.export_interval_ms is not None:
            self._effective["export_interval_ms"] = self.export_interval_ms
            return
        self._effective["export_interval_ms"] = _DEFAULT_EXPORT_INTERVAL_MS
        raw = _env(ENV_METRIC_EXPORT_INTERVAL)
        if raw is not None:
            try:
                value = int(raw)
                if value <= 0:
                    raise ValueError
                self._effective["export_interval_ms"] = value
            except ValueError:
                self._warn_once(ENV_METRIC_EXPORT_INTERVAL, "[OtelHelper] %s=%r is not a positive integer "
                                "(milliseconds); using %d.", ENV_METRIC_EXPORT_INTERVAL, raw,
                                _DEFAULT_EXPORT_INTERVAL_MS)

    # ── OTLP per-signal resolution ────────────────────────────────────────

    def _endpoint_candidates(self, signal: str) -> list[tuple[str, bool, Literal["code", "env"], str]]:
        """(raw endpoint, is per-signal, source, name) in precedence order.

        Code values come alone (they either work or raise); env values list the
        per-signal one first so an invalid one falls back to the generic one.
        """
        code_signal = getattr(self, f"{signal}_endpoint").strip(TRIM_CHARS)
        if code_signal:
            return [(code_signal, True, "code", f"{signal}_endpoint")]
        if self.otel_endpoint.strip(TRIM_CHARS):
            return [(self.otel_endpoint.strip(TRIM_CHARS), False, "code", "otel_endpoint")]
        out: list[tuple[str, bool, Literal["code", "env"], str]] = []
        var = _signal_var("ENDPOINT", signal)
        if (env_signal := _env(var)) is not None:
            out.append((env_signal, True, "env", var))
        if (env_generic := _env(ENV_COLLECTOR_ENDPOINT)) is not None:
            out.append((env_generic, False, "env", ENV_COLLECTOR_ENDPOINT))
        return out

    def _protocol_for(self, signal: str, raw: str, warn: bool = True) -> str:
        if (code := _non_blank(self.otlp_protocol)) is not None:
            return code.lower()
        for var in (_signal_var("PROTOCOL", signal), ENV_OTLP_PROTOCOL):
            value = _env(var)
            if value is None:
                continue
            if value.lower() in _VALID_OTLP_PROTOCOLS:
                return value.lower()
            if warn:
                self._warn_once(f"proto:{var}", "[OtelHelper] %s=%r is not a supported OTLP protocol "
                                "(valid: %s); ignoring it.", var, value, ", ".join(_VALID_OTLP_PROTOCOLS))
        if _endpoint_port(raw) == _DEFAULT_OTLP_HTTP_PORT:
            if warn:
                self._info_once("proto:4318", "[OtelHelper] OTLP protocol inferred as http/protobuf "
                                "from port 4318; set %s to override.", ENV_OTLP_PROTOCOL)
            return PROTOCOL_HTTP
        return PROTOCOL_GRPC

    def _parse_endpoint(self, raw: str, source: str, name: str, protocol: str) -> _Endpoint | str:
        """Parse and check an endpoint (RFC 3986 split, D1 edge-case rulings).

        Code errors raise. Env errors warn and return ``"invalid"`` (unparseable:
        fall back to the next level) or ``"unsupported"`` (a non-http(s) scheme:
        the signal is not configured). An invalid value is never rendered, not
        even partly: always ``<unparseable URL>`` (D1 ruling, 2026-10-29).
        """
        def fail(kind: str, why: str) -> str:
            shown = UNPARSEABLE_URL if kind == "invalid" else redact_url(raw, protocol)
            if source == "code":
                raise ValueError(f"[OtelHelper] {name} '{shown}' is not a valid endpoint: {why}.")
            self._warn_once(f"endpoint:{name}", "[OtelHelper] %s=%s is not a valid endpoint (%s); %s.",
                            name, shown, why, "no OTLP exporter for it" if kind == "unsupported" else "ignoring it")
            return kind

        # Before parsing: a control character is unparseable, also in an opaque value
        # (D1 ruling); urlsplit-style parsers would silently strip \t\r\n.
        if _has_control(raw):
            return fail("invalid", "it contains a control character")
        # WHATWG reads "\\" as "/" and would split the authority elsewhere (D1 ruling: RFC 3986).
        if "\\" in raw:
            return fail("invalid", "it contains a backslash")
        schemeless = "://" not in raw
        if schemeless and _OPAQUE_SCHEME_RE.match(raw):
            # "unix:/tmp/x", "collector:/v1/traces": a scheme without "//", not host[:port].
            return fail("unsupported", f"unsupported scheme {raw.split(':', 1)[0].lower()}:")
        if schemeless and raw.startswith("/"):
            return fail("invalid", "expected http(s)://host[:port] or host:port")
        sp = _rfc_split(f"https://{raw}" if schemeless else raw)
        if sp is None:
            return fail("invalid", "unparseable")
        if sp.scheme not in ("http", "https"):
            return fail("unsupported", f"unsupported scheme {sp.scheme}://")
        host, zone_id = _check_host(sp.host)
        if host is None:
            return fail("invalid", "IPv6 zone ids are not supported in endpoint URLs" if zone_id
                        else "expected http(s)://host[:port] or host:port")
        if protocol == PROTOCOL_HTTP and _BAD_ESCAPE_RE.search(sp.path):
            # requests would re-quote every escape of the path once one is invalid (D1 ruling, A17).
            return fail("invalid", "the path has an invalid percent-escape")
        rewritten = host != sp.host.lower() or (sp.port is not None and sp.port_text != str(sp.port))
        return _Endpoint(scheme=None if schemeless else sp.scheme, host=host, port=sp.port, path=sp.path,
                         userinfo=sp.userinfo, query=sp.query, fragment=sp.fragment,
                         rewritten=rewritten, at_in_path=_path_has_at(sp))

    def _resolve_signal(self, signal: str) -> SignalExport | None:
        # Read INSECURE first: an unparseable value warns even when no endpoint is
        # configured (D1 ruling, 2026-10-29; principle 8).
        insecure, insecure_name = self._insecure_setting(signal)
        chosen = None
        fell_back = False
        for raw, per_signal, source, name in self._endpoint_candidates(signal):
            parsed = self._parse_endpoint(raw, source, name, self._protocol_for(signal, raw, warn=False))
            if parsed == "unsupported":
                return None
            if parsed == "invalid":
                fell_back = True
                continue
            chosen = (raw, per_signal, source, name, parsed)
            break
        if chosen is None:
            return None
        raw, per_signal, source, name, parsed_ep = chosen
        ep = cast(_Endpoint, parsed_ep)  # "invalid" / "unsupported" were handled in the loop
        protocol = self._protocol_for(signal, raw)
        shown = redact_url(raw, protocol)
        port = ep.port
        scheme = ep.scheme

        # After skipping an invalid per-signal value the SDK would still read it: own the URL.
        explicit = source == "code" or fell_back or ep.rewritten or (source == "env" and os.environ.get(name) != raw)
        if scheme is None:
            scheme = "http" if insecure else "https"
            port = port or (_DEFAULT_OTLP_HTTP_PORT if protocol == PROTOCOL_HTTP else _DEFAULT_OTLP_PORT)
            explicit = True
            if protocol == PROTOCOL_HTTP:
                # Deprecation (D8): once per process, same wording as Go.
                warn_once_per_process(_log, f"deprecated:schemeless:{name}", "[OtelHelper] %s=%s: schemeless "
                                      "endpoint is deprecated for http/protobuf and will be removed in 1.0; "
                                      "use a URL (http:// or https://).", name, shown)
        else:
            if port is None and protocol == PROTOCOL_GRPC:
                self._warn_once(f"port:{name}", "[OtelHelper] %s=%s has no port; gRPC has no scheme "
                                "default port, using %d. Set an explicit port.",
                                name, shown, _DEFAULT_OTLP_PORT)
                port = _DEFAULT_OTLP_PORT
                explicit = True
            if insecure is not None and insecure != (scheme == "http"):
                if insecure_name == "insecure" and source == "code":
                    raise ValueError(f"[OtelHelper] insecure={insecure} contradicts the {scheme} scheme of "
                                     f"{name} '{shown}'.")
                self._warn_once(f"tls:{insecure_name}:{shown}", "[OtelHelper] %s=%s contradicts the "
                                "%s scheme of %s; the scheme wins (%s).", insecure_name,
                                str(insecure).lower(), scheme, shown,
                                "plaintext" if scheme == "http" else "TLS")
                explicit = True

        if ep.userinfo or ep.query is not None or ep.fragment is not None:
            # An empty "?" or "#" counts too (D1 ruling, 2026-10-29).
            self._warn_once(f"userinfo:{name}", "[OtelHelper] %s: URL userinfo, query and fragment are not "
                            "supported and were removed (%s); put credentials in OTEL_EXPORTER_OTLP_HEADERS.",
                            name, shown)
            explicit = True

        if ep.at_in_path:
            # Used as parsed; the name only, the value may hold the credentials (D1 ruling).
            self._warn_once(f"at:{name}", "[OtelHelper] %s: endpoint path contains '@' — misplaced "
                            "credentials?", name)

        if scheme == "http":
            tls_vars = [v for suffix in ("CERTIFICATE", "CLIENT_CERTIFICATE", "CLIENT_KEY")
                        for v in (_signal_var(suffix, signal), f"OTEL_EXPORTER_OTLP_{suffix}")
                        if _env(v) is not None]
            if tls_vars:
                self._warn_once(f"cert:{','.join(tls_vars)}:{shown}", "[OtelHelper] %s set but the "
                                "endpoint %s is plaintext (http); TLS files are not used.",
                                ", ".join(tls_vars), shown)
                if protocol == PROTOCOL_GRPC:
                    explicit = True  # pass insecure=True so the SDK doesn't load the files

        # The helper owns the TLS files (D1 ruling "TLS files", A18): resolved and
        # validated here, passed explicitly, and hidden from the SDK.
        tls = resolve_tls(signal, self._warn_once) if scheme == "https" else None

        kwargs: dict[str, object] = {}
        if explicit:
            netloc = f"{ep.host}:{port}" if port else ep.host
            if protocol == PROTOCOL_HTTP:
                # Escapes kept verbatim, never decoded (D1 ruling "Percent-encoding in the endpoint path").
                path = ep.path if per_signal else f"{ep.path.rstrip('/')}/v1/{signal}"
                kwargs["endpoint"] = f"{scheme}://{netloc}{path}"
            else:
                kwargs["endpoint"] = f"{scheme}://{netloc}"
                kwargs["insecure"] = scheme == "http"

        masked: list[str] = [v for v in tls_variables(signal) if v in os.environ]
        headers = self._headers_for(signal)
        if headers is not None:
            kwargs["headers"] = headers
            # The helper owns header parsing: the SDK must not re-parse (and log) the raw values,
            # which it would do for an empty dict and, on sdk >= 1.45 HTTP, merges anyway.
            masked += [v for v in (_signal_var("HEADERS", signal), "OTEL_EXPORTER_OTLP_HEADERS")
                       if v in os.environ]
        timeout_s = self._timeout_for(signal, masked)
        if timeout_s is None:
            timeout_s = _DEFAULT_TIMEOUT_MS / 1000  # the export deadline is always explicit (A18)
        kwargs["timeout"] = timeout_s
        kwargs["compression"] = self._compression_for(signal)
        return SignalExport(signal=signal, protocol=protocol, source=source, kwargs=kwargs,
                            masked_env=tuple(masked), timeout_ms=timeout_s * 1000, tls=tls)

    def _insecure_setting(self, signal: str) -> tuple[bool | None, str]:
        if self.insecure is not None:
            return self.insecure, "insecure"
        for var in (_signal_var("INSECURE", signal), ENV_INSECURE):
            value = self._env_bool(var)
            if value is not None:
                return value, var
        return None, ""

    def _headers_for(self, signal: str) -> dict[str, str] | None:
        """Per-signal headers env replaces the generic one (as in both SDKs).

        Malformed items are skipped (warned by position, never values); a
        per-signal var with no valid item falls back to the generic one.
        """
        found = False
        for var in (_signal_var("HEADERS", signal), "OTEL_EXPORTER_OTLP_HEADERS"):
            raw = os.environ.get(var)
            if raw is None or not raw.strip(TRIM_CHARS):
                continue
            found = True
            headers, bad, bad_escape = _parse_headers(raw)
            if bad:
                self._warn_once(f"headers:{var}", "[OtelHelper] %s: malformed item(s) at position(s) %s "
                                "skipped (expected key=value, key [A-Za-z0-9._-], no control characters).",
                                var, ", ".join(map(str, bad)))
            if bad_escape:
                self._warn_once(f"headers-escape:{var}", "[OtelHelper] %s: invalid percent-encoding in the "
                                "value at position(s) %s; value kept literally.", var, ", ".join(map(str, bad_escape)))
            if headers:
                return headers
        return {} if found else None

    def _timeout_for(self, signal: str, masked: list[str]) -> float | None:
        """Seconds for the exporter: code > per-signal env > generic env (milliseconds, D1.e).

        An invalid per-signal value is always masked: the gRPC SDK ``float()``s it
        unconditionally, even when ``timeout=`` is passed.
        """
        per_signal = _signal_var("TIMEOUT", signal)
        result: float | None = None
        for var in (per_signal, "OTEL_EXPORTER_OTLP_TIMEOUT"):
            raw = _env(var)
            if raw is None:
                continue
            try:
                if not _DECIMAL_RE.match(raw):
                    raise ValueError
                ms = float(raw)
                if not math.isfinite(ms) or ms <= 0:
                    raise ValueError
            except ValueError:
                if var == per_signal:
                    masked.append(var)
                if self.export_timeout_ms is not None:
                    self._warn_once(f"timeout:{var}", "[OtelHelper] %s=%r is not a positive number of "
                                    "milliseconds; ignored, code value used.", var, raw)
                    continue
                fallback = "OTEL_EXPORTER_OTLP_TIMEOUT" if var == per_signal else f"{int(_DEFAULT_TIMEOUT_MS)} ms"
                self._warn_once(f"timeout:{var}", "[OtelHelper] %s=%r is not a positive number of "
                                "milliseconds; falling back to %s.", var, raw, fallback)
                continue
            if self.export_timeout_ms is None:
                clamped = min(max(ms, _MIN_TIMEOUT_MS), _MAX_TIMEOUT_MS)
                if clamped != ms:
                    self._warn_once(f"timeout-range:{var}", "[OtelHelper] %s=%s is outside [1 ms, 24 h]; "
                                    "using %s ms.", var, raw, f"{clamped:g}")
                    ms = clamped
                if ms < _SECONDS_LIKE_TIMEOUT_MS:
                    self._warn_once(f"timeout-unit:{var}", "[OtelHelper] %s=%s is read as milliseconds "
                                    "(OTLP spec); a value below %d looks like seconds.", var, raw,
                                    _SECONDS_LIKE_TIMEOUT_MS)
                result = ms / 1000
                break
        if self.export_timeout_ms is not None:
            return self.export_timeout_ms / 1000
        if result is None and any(_env(v) is not None for v in (per_signal, "OTEL_EXPORTER_OTLP_TIMEOUT")):
            return _DEFAULT_TIMEOUT_MS / 1000  # all set values were invalid: keep the SDK from parsing them
        return result

    def _compression_for(self, signal: str) -> str:
        if (code := _non_blank(self.compression)) is not None:
            return code.lower()
        for var in (_signal_var("COMPRESSION", signal), "OTEL_EXPORTER_OTLP_COMPRESSION"):
            raw = _env(var)
            if raw is None:
                continue
            if raw.lower() in _VALID_COMPRESSIONS:
                return raw.lower()
            self._warn_once(f"compression:{var}", "[OtelHelper] %s=%r is not a supported compression "
                            "(valid: %s); ignoring it.", var, raw, ", ".join(_VALID_COMPRESSIONS))
        return "gzip"

    def batch_size_override(self, prefix: str) -> dict[str, int]:
        """``max_export_batch_size`` to pass when it exceeds the queue size (D1 ruling, A18).

        ``<prefix>_MAX_EXPORT_BATCH_SIZE`` above ``<prefix>_MAX_QUEUE_SIZE``
        (defaults 512 / 2048) warns once naming both variables and the queue
        size is used — the SDK would raise ``ValueError`` and fail setup.
        Unparseable values are left to the SDK.
        """
        def read(var: str, default: int) -> int | None:
            raw = _env(var)
            if raw is None:
                return default
            try:
                return int(raw)
            except ValueError:
                return None

        queue_var, batch_var = f"{prefix}_MAX_QUEUE_SIZE", f"{prefix}_MAX_EXPORT_BATCH_SIZE"
        queue, batch = read(queue_var, 2048), read(batch_var, 512)
        if queue is None or batch is None or queue <= 0 or batch <= queue:
            return {}
        self._warn_once(f"batch-above-queue:{prefix}", "[OtelHelper] %s=%d is larger than %s=%d; using the queue "
                        "size as the export batch size.", batch_var, batch, queue_var, queue)
        return {"max_export_batch_size": queue}

    def value_length_limits(self) -> dict[str, int | None]:
        """Value-length limits to pass explicitly because an env value is 0 (D1 ruling, A18).

        0 is not a valid limit: it warns once and the next level applies
        (per-signal → generic → no limit). Returns ``{}`` when no 0 is set (the
        SDK reads the variables itself); otherwise the keys ``span`` and/or
        ``log`` with the limit to use (-1 = ``SpanLimits.UNSET``, no limit).
        """
        values: dict[str, int | None] = {}
        zero: set[str] = set()
        for var in _VALUE_LENGTH_VARS:
            raw = _env(var)
            n: int | None = None
            if raw is not None:
                try:
                    n = int(raw)
                except ValueError:
                    n = None  # the SDK reports it
            if n == 0:
                self._warn_once(f"value-length-zero:{var}", "[OtelHelper] %s=0: a value-length limit of 0 is invalid; "
                                "the next level or no limit is used.", var)
                zero.add(var)
                n = None
            values[var] = n
        generic, span, log = (values[v] for v in _VALUE_LENGTH_VARS)
        out: dict[str, int | None] = {}
        fallback = generic if generic is not None else -1
        if _VALUE_LENGTH_VARS[1] in zero or (_VALUE_LENGTH_VARS[0] in zero and span is None):
            out["span"] = span if span is not None else fallback
        if _VALUE_LENGTH_VARS[2] in zero or (_VALUE_LENGTH_VARS[0] in zero and log is None):
            out["log"] = log if log is not None else fallback
        if _VALUE_LENGTH_VARS[0] in zero:
            out["generic"] = -1
        return out

    # ── validation ────────────────────────────────────────────────────────

    def validate(self) -> None:
        """Validate code-set options at startup. Raises ValueError on invalid config.

        This is equivalent to .NET's IValidateOptions — the application MUST NOT start with invalid config.
        Call setup_telemetry() in main() to ensure failures are visible.
        """
        self._validate_code()
        if self.metric_exporters is not None:
            unknown = [e for e in self.metric_exporters if e not in _VALID_METRIC_EXPORTERS]
            if unknown:
                raise ValueError(
                    f"[OtelHelper] Unknown metric exporter(s) {unknown} in metric_exporters. "
                    f"Valid values: {', '.join(_VALID_METRIC_EXPORTERS)}."
                )
            if "none" in self.metric_exporters and len(self.metric_exporters) > 1:
                raise ValueError("[OtelHelper] 'none' cannot be combined with other metric exporters "
                                 "in metric_exporters.")
            if "otlp" in self.metric_exporters and not self.is_otlp_configured("metrics"):
                raise ValueError(
                    "[OtelHelper] Metric exporter 'otlp' requires an endpoint. "
                    f"Set {ENV_COLLECTOR_ENDPOINT} or remove 'otlp' from metric_exporters."
                )
