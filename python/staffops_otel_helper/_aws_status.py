"""AWS SDK (botocore) span rules for ``ext.aws`` (contract task A27, 2026-11-03).

The DB error rule of A25/A26 extended to AWS clients (contract design D1 "AWS
error status"): a failed botocore span keeps the ERROR status code and drops
its description (contrib botocore 0.63b0–0.66b1 sets ``span.set_status(
StatusCode.ERROR, str(error))``, ``__init__.py`` ``_patched_api_call``; an AWS
error message can quote request values, e.g. a DynamoDB
``ValidationException``). ``error.type`` is the AWS error code
(``ClientError.response["Error"]["Code"]``: ``NoSuchKey``, ``AccessDenied``)
when it matches :data:`AWS_ERROR_CODE`; else the query-compatible
``Error.QueryErrorCode`` (SQS ``QueueDoesNotExist`` when ``Code`` is
``AWS.SimpleQueueService.NonExistentQueue``), else the modeled exception
class botocore raises (``client.exceptions.QueueDoesNotExist``; the Query
protocol of older botocore has no ``QueryErrorCode``); an all-digit ``Code`` (a
body-less error: S3 ``HeadObject`` 404) gives the HTTP reason phrase without
spaces (:data:`STATUS_CODES`, Go's ``http.StatusText`` values: ``NotFound``,
``Forbidden``, ``ServiceUnavailable``); else ``_OTHER``. An error without an AWS
error at all (a transport error: ``EndpointConnectionError``) gives its class
name when that matches. Every ``error.type`` write goes through
:func:`aws_error_type` (contrib's Bedrock extension writes the class name; that
write is dropped). Not covered: aiobotocore (``AioBotocoreInstrumentor``), which
the helper does not instrument. The
same rule in Go ``ext/otelaws`` and Node ``@staffops/otel-helper-aws``.

``aws.dynamodb.item_collection_metrics`` is never recorded: it holds
``ItemCollectionKey``, the partition-key value of the written item (contrib
``extensions/dynamodb.py`` ``_RES_ITEM_COL_METRICS``). An SNS phone number is
already redacted by contrib (``extensions/sns.py``: ``phone_number:**``).

The exception event keeps the AWS error message (semconv; the A25 residual).

Attribute values (A30, contract D1 "AWS service attribute values"):
``messaging.system`` ``aws_sqs`` (contrib writes ``aws.sqs``; SNS is
``aws.sns`` already) and, on DynamoDB spans, ``db.system.name=aws.dynamodb``
next to contrib's legacy ``db.system=dynamodb`` (the legacy key dropped in the
stable database mode); ``server.port`` 80 for a portless ``http://`` endpoint.
Span names, kinds and RPC keys (ruled 2026-11-06, kinds amended 2026-11-08):
every AWS call span is ``<Service>.<Operation>``, CLIENT except SQS/SNS send
(PRODUCER) and receive (CONSUMER); the RPC keys follow
``OTEL_SEMCONV_STABILITY_OPT_IN`` (:func:`aws_call_span`).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any
from urllib.parse import urlparse

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from staffops_otel_helper._db_status import _ErrorDescriptionDroppingTracer, _ErrorDescriptionDroppingTracerProvider
from staffops_otel_helper._diag import warn_once_per_process
from staffops_otel_helper._semconv import legacy_db_keys_enabled
from staffops_otel_helper.config import TRIM_CHARS, ascii_lower

#: AWS error codes are a bounded set of PascalCase identifiers (Go and Node use the same pattern).
AWS_ERROR_CODE = re.compile(r"^[A-Z][A-Za-z0-9]{1,63}$")

_log = logging.getLogger("staffops_otel_helper._aws_status")

#: Attributes never recorded on AWS spans.
DROPPED_ATTRIBUTES = frozenset({"aws.dynamodb.item_collection_metrics"})

#: contrib ``messaging.system`` values → the semconv v1.43 registry values
#: (``registry/attributes/messaging.md``: ``aws.sns``, ``aws_sqs``); contrib
#: 0.63b0–0.66b2 writes ``aws.sqs`` (``extensions/sqs.py``). A30.
MESSAGING_SYSTEMS = {"aws.sqs": "aws_sqs"}

#: Legacy ``db.system`` (contrib ``extensions/dynamodb.py``) → semconv v1.43
#: ``db.system.name`` (``registry/attributes/db.md``: ``aws.dynamodb``). A30.
DB_SYSTEM_NAMES = {"dynamodb": "aws.dynamodb"}


def map_aws_attributes(attributes: Any) -> dict:
    """The attributes recorded of a contrib botocore batch (A27 drops, A30 value mapping).

    Drops :data:`DROPPED_ATTRIBUTES` and ``error.type`` (owned by the status
    rule), maps ``messaging.system`` (:data:`MESSAGING_SYSTEMS`) and adds
    ``db.system.name`` next to a legacy ``db.system`` (:data:`DB_SYSTEM_NAMES`),
    dropping the legacy key when ``OTEL_SEMCONV_STABILITY_OPT_IN`` holds
    ``database`` without ``database/dup`` (contract D1 "Database semconv
    opt-in"; the same rule in Go ``ext/otelaws`` and Node ``-aws``).
    """
    out = {}
    for key, value in dict(attributes).items():
        if key in DROPPED_ATTRIBUTES or key == "error.type":
            continue
        if key == "messaging.system" and isinstance(value, str):
            value = MESSAGING_SYSTEMS.get(value, value)
        elif key == "db.system" and isinstance(value, str) and value in DB_SYSTEM_NAMES:
            out.setdefault("db.system.name", DB_SYSTEM_NAMES[value])
            if not legacy_db_keys_enabled():
                continue
        out[key] = value
    return out


#: Body-less errors (S3 HEAD): status code → the modeled name, Go's ``http.StatusText``
#: with the spaces removed (Go 1.26), the same fixed table in Node.js (A27 fix round).
STATUS_CODES = {
    400: "BadRequest", 401: "Unauthorized", 402: "PaymentRequired", 403: "Forbidden", 404: "NotFound",
    405: "MethodNotAllowed", 406: "NotAcceptable", 407: "ProxyAuthenticationRequired", 408: "RequestTimeout",
    409: "Conflict", 410: "Gone", 411: "LengthRequired", 412: "PreconditionFailed",
    413: "RequestEntityTooLarge", 414: "RequestURITooLong", 415: "UnsupportedMediaType",
    416: "RequestedRangeNotSatisfiable", 417: "ExpectationFailed", 421: "MisdirectedRequest",
    422: "UnprocessableEntity", 423: "Locked", 424: "FailedDependency", 425: "TooEarly",
    426: "UpgradeRequired", 428: "PreconditionRequired", 429: "TooManyRequests",
    431: "RequestHeaderFieldsTooLarge", 451: "UnavailableForLegalReasons", 500: "InternalServerError",
    501: "NotImplemented", 502: "BadGateway", 503: "ServiceUnavailable", 504: "GatewayTimeout",
    505: "HTTPVersionNotSupported", 506: "VariantAlsoNegotiates", 507: "InsufficientStorage",
    508: "LoopDetected", 510: "NotExtended", 511: "NetworkAuthenticationRequired",
}


def aws_error_type(exception: BaseException | None) -> str:
    """The ``error.type`` of a failed AWS call; see the module docstring."""
    if exception is None:
        return "_OTHER"
    response = getattr(exception, "response", None)
    error = response.get("Error") if isinstance(response, dict) else None
    if not isinstance(error, dict):
        # No AWS error code (a transport error): its class name when that matches.
        name = type(exception).__name__
        return name if AWS_ERROR_CODE.match(name) else "_OTHER"
    code = error.get("Code")
    # The modeled exception class botocore raises (client.exceptions.QueueDoesNotExist,
    # also on the Query protocol of older botocore, where there is no QueryErrorCode).
    modeled = type(exception).__name__ if type(exception).__name__ != "ClientError" else None
    candidates = [code, error.get("QueryErrorCode"), modeled]
    if isinstance(code, str) and code.isascii() and code.isdigit():
        # A body-less error (S3 HeadObject 404): the modeled name is the reason phrase.
        candidates.append(STATUS_CODES.get(int(code)))
    for candidate in candidates:
        if isinstance(candidate, str) and AWS_ERROR_CODE.match(candidate):
            return candidate
    return "_OTHER"


def _sanitize_aws_span(span: Span) -> Span:
    """Patch ``span``: ERROR without description, ``error.type``, dropped attributes."""
    original_status = span.set_status
    original_record = getattr(span, "record_exception", None)
    original_attribute = span.set_attribute
    original_attributes = getattr(span, "set_attributes", None)
    errored = [False]  # the call failed (contrib set ERROR): record_exception names error.type

    def write_error_type(value: str) -> None:
        # The SDK's set_attribute delegates to set_attributes, which the wrapper
        # below filters: write through the original set_attributes when there is one.
        if original_attributes is not None:
            original_attributes({"error.type": value})
        else:
            original_attribute("error.type", value)

    def set_status(status: Status | StatusCode, description: str | None = None) -> None:
        code = status.status_code if isinstance(status, Status) else status
        if code is not StatusCode.ERROR:
            original_status(status, description)
            return
        # contrib calls set_status inside its except block: the exception being
        # handled names error.type; record_exception refines it when it comes later.
        errored[0] = True
        write_error_type(aws_error_type(sys.exc_info()[1]))
        original_status(Status(StatusCode.ERROR))

    def record_exception(exception: BaseException, attributes: Any = None, timestamp: int | None = None,
                         escaped: bool = False) -> None:
        if errored[0]:
            write_error_type(aws_error_type(exception))
        original_record(exception, attributes=attributes, timestamp=timestamp, escaped=escaped)

    # error.type is written only by this wrapper, through aws_error_type()
    # (contrib's Bedrock extension writes the exception class name).
    def set_attribute(key: str, value: Any) -> None:
        mapped = map_aws_attributes({key: value})
        if original_attributes is not None:
            if mapped:
                original_attributes(mapped)
        else:
            for k, v in mapped.items():
                original_attribute(k, v)

    def set_attributes(attributes: Any) -> None:
        original_attributes(map_aws_attributes(attributes))

    try:
        span.set_status = set_status  # type: ignore[method-assign]
        span.set_attribute = set_attribute  # type: ignore[method-assign]
        if original_record is not None:
            span.record_exception = record_exception  # type: ignore[method-assign]
        if original_attributes is not None:
            span.set_attributes = set_attributes  # type: ignore[method-assign]
    except AttributeError:  # a span type with __slots__: left as it is
        pass
    return span


#: RPC convention generations (contract D1 "AWS RPC keys", ruled 2026-11-06, A30).
RPC_LEGACY, RPC_STABLE, RPC_DUP = "legacy", "rpc", "rpc/dup"

#: ``messaging.system`` values (contrib's and the mapped ones) of SNS and SQS spans (A30).
_MESSAGING_SYSTEMS = frozenset({"aws.sns", "aws.sqs", "aws_sqs"})

#: Span kind of the SQS/SNS send and receive calls (contract D1 "AWS service attribute values",
#: owner amendment 2026-11-08); every other AWS call is CLIENT.
_MESSAGING_KINDS = {
    ("SQS", "SendMessage"): trace.SpanKind.PRODUCER,
    ("SQS", "SendMessageBatch"): trace.SpanKind.PRODUCER,
    ("SQS", "ReceiveMessage"): trace.SpanKind.CONSUMER,
    ("SNS", "Publish"): trace.SpanKind.PRODUCER,
    ("SNS", "PublishBatch"): trace.SpanKind.PRODUCER,
}


def rpc_mode(value: str | None = None) -> str:
    """The RPC generation ``OTEL_SEMCONV_STABILITY_OPT_IN`` selects for AWS spans (A30).

    ``rpc/dup`` wins over ``rpc`` (semconv ``rpc-spans.md``); tokens trimmed
    with the contract set and ASCII-folded, as the database tokens. Default:
    the legacy keys contrib writes.
    """
    raw = os.environ.get("OTEL_SEMCONV_STABILITY_OPT_IN", "") if value is None else value
    tokens = {ascii_lower(t.strip(TRIM_CHARS)) for t in raw.split(",")}
    if RPC_DUP in tokens:
        return RPC_DUP
    return RPC_STABLE if RPC_STABLE in tokens else RPC_LEGACY


def aws_call_span(name: Any, kind: Any, attributes: Any, mode: str) -> tuple[Any, Any, Any]:
    """Name, kind and attributes of a contrib botocore span at start (A30, contract D1).

    An AWS SDK call span (``rpc.system=aws-api`` with ``rpc.service`` and
    ``rpc.method``) is named ``<Service>.<Operation>`` (semconv ``aws-sdk.md``
    ``span.aws.client``; owner ruling Q3 (b)): contrib names an SNS publish
    ``<topic> send`` (``extensions/sns.py``). Its kind is PRODUCER on SQS/SNS
    send and publish, CONSUMER on SQS receive, CLIENT otherwise (owner
    amendment 2026-11-08). Bedrock spans (``gen_ai.*`` attributes) keep their
    GenAI name and kind. SNS and SQS spans get ``messaging.operation.name`` =
    the AWS operation (Go maps otelaws's ``publish_input`` to the same value). Then
    the RPC keys follow ``mode``: ``legacy`` as contrib writes them; ``rpc``
    ``rpc.system.name=aws-api`` and ``rpc.method=<Service>/<Operation>``
    without ``rpc.system`` / ``rpc.service``; ``rpc/dup`` both, ``rpc.method``
    holding the current value (one key cannot hold both; ``rpc.service`` keeps
    the service), as Go and Node.js.
    """
    attrs = dict(attributes or {})
    service, method = attrs.get("rpc.service"), attrs.get("rpc.method")
    if attrs.get("rpc.system") != "aws-api" or not isinstance(service, str) or not isinstance(method, str):
        return name, kind, attributes
    if not any(k.startswith("gen_ai.") for k in attrs):
        name, kind = f"{service}.{method}", _MESSAGING_KINDS.get((service, method), trace.SpanKind.CLIENT)
    if attrs.get("messaging.system") in _MESSAGING_SYSTEMS:
        attrs.setdefault("messaging.operation.name", method)
    if mode in (RPC_STABLE, RPC_DUP):
        attrs["rpc.system.name"] = "aws-api"
        attrs["rpc.method"] = f"{service}/{method}"
        if mode == RPC_STABLE:
            attrs.pop("rpc.system", None)
            attrs.pop("rpc.service", None)
    return name, kind, attrs


class _AwsTracer(_ErrorDescriptionDroppingTracer):
    """Tracer for contrib botocore: its spans follow :func:`aws_call_span` and :func:`_sanitize_aws_span`."""

    def __init__(self, tracer: trace.Tracer, mode: str = RPC_LEGACY) -> None:
        super().__init__(tracer)
        self._mode = mode

    def start_span(self, *args: Any, **kwargs: Any) -> Span:
        attributes = kwargs.get("attributes")
        if attributes:
            try:
                name = kwargs.get("name", args[0] if args else None)
                name, kind, attributes = aws_call_span(name, kwargs.get("kind"), attributes, self._mode)
                if args:
                    args = (name, *args[1:])
                else:
                    kwargs["name"] = name
                if kind is not None:
                    kwargs["kind"] = kind
            except Exception:  # noqa: BLE001 - never raise into the instrumented call
                pass
            # error.type at start is kept as before (contrib sets none there).
            start_error = {k: v for k, v in dict(attributes).items() if k == "error.type"}
            kwargs["attributes"] = {**map_aws_attributes(attributes), **start_error}
        return _sanitize_aws_span(self._tracer.start_span(*args, **kwargs))


class _AwsTracerProvider(_ErrorDescriptionDroppingTracerProvider):
    def __init__(self, provider: trace.TracerProvider, mode: str = RPC_LEGACY) -> None:
        super().__init__(provider)
        self._mode = mode

    def get_tracer(self, *args: Any, **kwargs: Any) -> trace.Tracer:
        return _AwsTracer(self._provider.get_tracer(*args, **kwargs), self._mode)


def server_attributes(endpoint_url: str) -> dict:
    """``server.address`` / ``server.port`` of a botocore endpoint URL (A30, ruling Q6).

    contrib's ``utils.get_server_attributes`` writes ``parsed.port or 443``, so
    a portless ``http://`` endpoint (LocalStack, a proxy) got 443; the port
    follows the scheme here, as in Go and URL semantics: the URL's port, else
    443 for ``https``, 80 for ``http`` (any other scheme: 443, contrib's value).
    An out-of-range port (``ValueError`` from ``urlparse``) records no
    ``server.port``, as Go (D1, review L4, 2026-11-08).
    """
    parsed = urlparse(endpoint_url)
    attributes: dict = {}
    if parsed.hostname:
        attributes["server.address"] = parsed.hostname
        try:
            port = parsed.port
        except ValueError:  # an out-of-range port: no server.port, as Go
            return attributes
        attributes["server.port"] = port or (80 if ascii_lower(parsed.scheme or "") == "http" else 443)
    return attributes


_PATCHED = "_staffops_server_attributes"


def _patch_server_attributes() -> None:
    """Point contrib's module-level ``get_server_attributes`` at :func:`server_attributes` (once).

    contrib botocore 0.63b0–0.66b2 calls it by its module global name
    (``__init__.py:263/423`` in 0.63b0, ``:264/423`` in 0.66b2); a release
    without it leaves contrib's own rule, warned once (``tests/test_a30.py``
    asserts the port, so such a release fails the suite).
    """
    try:
        import opentelemetry.instrumentation.botocore as contrib
    except ImportError:
        return
    if not callable(getattr(contrib, "get_server_attributes", None)):
        # Failures are loud (AGENTS principle 8; review L5, 2026-11-08).
        warn_once_per_process(_log, "aws-server-attributes", "[OtelHelper] contrib botocore has no get_server_attributes; "
                              "a portless http:// AWS endpoint keeps contrib's server.port.")
        return
    if not getattr(contrib, _PATCHED, False):
        contrib.get_server_attributes = server_attributes
        setattr(contrib, _PATCHED, True)


def aws_instrument_kwargs() -> dict:
    """``instrument()`` kwargs every helper path uses for contrib botocore (A27, A30).

    Shared by ``instrument_aws()`` and the deprecated ``AWS`` flag of
    ``setup_telemetry()`` (``instrumentation.configure_instrumentations``).
    The RPC mode is read here, once per instrumentation (as Go's ``Instrument``).
    """
    _patch_server_attributes()
    return {"tracer_provider": _AwsTracerProvider(trace.get_tracer_provider(), rpc_mode())}
