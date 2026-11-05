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
"""

from __future__ import annotations

import re
import sys
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from staffops_otel_helper._db_status import _ErrorDescriptionDroppingTracer, _ErrorDescriptionDroppingTracerProvider

#: AWS error codes are a bounded set of PascalCase identifiers (Go and Node use the same pattern).
AWS_ERROR_CODE = re.compile(r"^[A-Z][A-Za-z0-9]{1,63}$")

#: Attributes never recorded on AWS spans.
DROPPED_ATTRIBUTES = frozenset({"aws.dynamodb.item_collection_metrics"})


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
        if key not in DROPPED_ATTRIBUTES and key != "error.type":
            original_attribute(key, value)

    def set_attributes(attributes: Any) -> None:
        original_attributes({k: v for k, v in dict(attributes).items()
                             if k not in DROPPED_ATTRIBUTES and k != "error.type"})

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


class _AwsTracer(_ErrorDescriptionDroppingTracer):
    """Tracer for contrib botocore: its spans follow :func:`_sanitize_aws_span`."""

    def start_span(self, *args: Any, **kwargs: Any) -> Span:
        attributes = kwargs.get("attributes")
        if attributes:
            kwargs["attributes"] = {k: v for k, v in dict(attributes).items() if k not in DROPPED_ATTRIBUTES}
        return _sanitize_aws_span(self._tracer.start_span(*args, **kwargs))


class _AwsTracerProvider(_ErrorDescriptionDroppingTracerProvider):
    def get_tracer(self, *args: Any, **kwargs: Any) -> trace.Tracer:
        return _AwsTracer(self._provider.get_tracer(*args, **kwargs))


def aws_instrument_kwargs() -> dict:
    """``instrument()`` kwargs every helper path uses for contrib botocore (A27).

    Shared by ``instrument_aws()`` and the deprecated ``AWS`` flag of
    ``setup_telemetry()`` (``instrumentation.configure_instrumentations``).
    """
    return {"tracer_provider": _AwsTracerProvider(trace.get_tracer_provider())}
