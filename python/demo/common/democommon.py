"""Shared pieces of the Python demo apps (specs/demo-environment).

Settings come from environment variables only (DR0, DE-D13 r3.3); passwords
are read once from the files named by ``DEMO_PG_PASSWORD_FILE`` and
``DEMO_REDIS_PASSWORD_FILE`` and are never logged. Owning demo spans use the
helper's ``staffops_otel_helper.traced`` (contract design D15, A45), which applies
the ADR-014 failure pattern of DE-D8; the demo errors pin their ``error.type``
through ``__otel_error_type__``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import unquote

# Language segment of the stream, key prefix, database and service names (DE-D2, DE-D4).
LANG = "python"
# Instrumentation scope of every demo span and instrument (DE-D8: check d selects by it).
SCOPE = "otel-libs-demo"
STREAM = f"demo:{LANG}:orders"
CONSUMER_GROUP = "demo-workers"
QUOTE_KEY_PREFIX = f"demo:{LANG}:quote:"
QUOTE_TTL_SECONDS = 30
STREAM_MAXLEN = 1000

# Scenario names (design "Scenario table").
OK = "ok"
BUSINESS_ERROR = "business_error"
DB_ERROR = "db_error"
UNHANDLED = "unhandled"
SCENARIOS = (OK, BUSINESS_ERROR, DB_ERROR, UNHANDLED)

# DE-D7: positions 1..10 of the fixed cycle.
CYCLE = (OK, OK, BUSINESS_ERROR, OK, OK, DB_ERROR, OK, OK, UNHANDLED, OK)

# The worker's customers: three values so the api's quote cache both hits and misses.
CUSTOMERS = ("cust-a", "cust-b", "cust-c")

# Amount limit of the backend's business rule; business_error orders exceed it.
AMOUNT_LIMIT = 1000.0
REJECTED_AMOUNT = 5000.0

# Custom metrics (DE-D9).
METRIC_PROCESSED = "demo.orders.processed"
METRIC_DURATION = "demo.order.duration"
DURATION_BOUNDARIES = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5)

# gRPC status messages stay constant; the run tokens are in the exception messages only (DE-D8).
GRPC_MSG_REJECTED = "order rejected"
GRPC_MSG_EXISTS = "order exists"
# Any other backend failure: INTERNAL with this constant message (DE-D7 r3.5).
GRPC_MSG_FAILED = "order failed"
GRPC_MSG_INVALID = "invalid scenario"
# Scenarios the backend accepts ("unhandled" fails in the api before the call).
BACKEND_SCENARIOS = (OK, BUSINESS_ERROR, DB_ERROR)


class OrderRejectedError(Exception):
    """Business rule violation in the backend (business_error)."""

    # The pinned literal (DE-D8), read by the helper instead of ``democommon.OrderRejectedError``.
    __otel_error_type__ = "OrderRejectedError"


class OrderExistsError(Exception):
    """The database refused the order as a duplicate (db_error)."""

    __otel_error_type__ = "OrderExistsError"


class DemoUnhandledError(Exception):
    """Failure the api does not handle; the framework maps it to 500 (unhandled)."""

    __otel_error_type__ = "DemoUnhandledError"


def run_id(environ: dict[str, str] | None = None) -> str:
    """Return the run id: ``otel_libs.run`` from ``OTEL_RESOURCE_ATTRIBUTES``, else ``local``.

    The run id is the test-only resource attribute of DR0; the apps read it to
    build the run tokens (``rej-<run>``, ``dup-<run>``, ``unh-<run>``) and never
    set it on the resource themselves (the SDK does, from the same variable).
    """
    env = os.environ if environ is None else environ
    for item in env.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        key, sep, value = item.partition("=")
        if sep and unquote(key.strip()) == "otel_libs.run":
            value = unquote(value.strip())
            if value:
                return value
    return "local"


def read_secret_file(path: str | None, name: str) -> str:
    """Read a password from the file at ``path``, stripping one trailing newline.

    ``name`` is the environment variable that named the file; error messages
    carry it, never the content.
    """
    if not path:
        raise RuntimeError(f"{name} is not set")
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read()
    except OSError as e:
        raise RuntimeError(f"cannot read the file named by {name}: {type(e).__name__}") from None
    value = value.rstrip("\r\n")
    if not value:
        raise RuntimeError(f"the file named by {name} is empty")
    return value


def split_host_port(addr: str, default_port: int) -> tuple[str, int]:
    """Split ``host[:port]`` (``[v6]:port`` accepted) into host and port."""
    addr = addr.strip()
    if addr.startswith("["):
        host, _, rest = addr[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif addr.count(":") == 1:
        host, _, port = addr.partition(":")
    else:
        host, port = addr, ""
    if not host:
        raise ValueError("empty host")
    return host, int(port) if port else default_port


_log = logging.getLogger("demo.config")
_warned: set[str] = set()


def _env_number(name: str, default, parse, environ: dict[str, str] | None):
    """Parse ``name``; unset → ``default``; invalid or not > 0 → ``default`` with one warning per name."""
    env = os.environ if environ is None else environ
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = parse(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    if name not in _warned:
        _warned.add(name)
        # The variable's name only; its value may be anything the operator typed.
        _log.warning("invalid value for %s, using the default %s", name, default)
    return default


def env_int(name: str, default: int, environ: dict[str, str] | None = None) -> int:
    return _env_number(name, default, int, environ)


def env_float(name: str, default: float, environ: dict[str, str] | None = None) -> float:
    return _env_number(name, default, float, environ)


@dataclass(frozen=True)
class OrderRequest:
    """The worker's HTTP body (DE-D7) and the gRPC request fields."""

    order_id: str
    customer: str
    amount: float
    scenario: str

    def to_json(self) -> dict:
        return {"order_id": self.order_id, "customer": self.customer,
                "amount": self.amount, "scenario": self.scenario}


def order_for(seq: int) -> OrderRequest:
    """Build the ``seq``-th order of the fixed cycle; ``seq`` starts at 1 (DE-D7 r3.4/r3.5, DE-D4).

    Position ``(seq - 1) mod 10 + 1`` of the cycle picks the scenario. Amounts:
    ``10 + seq mod 90`` (``business_error``: 5000, over the backend's limit of
    1000). ``ok`` orders use the id ``ok-<seq mod 500>`` the backend upserts, which
    bounds the table at 500 rows per language; the error scenarios use
    ``ord-<seq mod 500>``. The run tokens are built by the app that raises the
    error, so no span attribute (``order.id``) ever holds one (DR12).
    """
    scenario = CYCLE[(seq - 1) % len(CYCLE)]
    customer = CUSTOMERS[(seq - 1) % len(CUSTOMERS)]
    amount = REJECTED_AMOUNT if scenario == BUSINESS_ERROR else 10.0 + seq % 90
    prefix = "ok" if scenario == OK else "ord"
    return OrderRequest(f"{prefix}-{seq % 500}", customer, amount, scenario)


def error_type(exc: BaseException) -> str:
    """The ``error.type`` the helper gives ``exc`` (contract design D15), for the metric attributes.

    So the metric attribute (DE-D9) equals the span attribute: a non-empty ``str``
    ``__otel_error_type__`` (the pinned literals), else ``module.qualname``, bare
    for ``builtins`` and ``__main__``.
    """
    override = getattr(exc, "__otel_error_type__", None)
    if isinstance(override, str) and override:
        return override
    cls = type(exc)
    module = cls.__module__
    return cls.__qualname__ if module in ("builtins", "__main__") else f"{module}.{cls.__qualname__}"


async def ensure_group(redis_client) -> bool:
    """Create the consumer group and the stream if absent; return True when created.

    Both the api (before its first XADD, so no event is missed) and the worker
    call it, the worker again after a ``NOGROUP`` reply (Redis restarted with
    empty data). Existence is checked first (``EXISTS``, then ``XINFO GROUPS``,
    neither of which fails), so the usual start-up makes no failing command and
    no ERROR Redis span; only a race between two processes still gets
    ``BUSYGROUP``, which is accepted (DE-D7 r3.5).
    """
    from redis.exceptions import ResponseError

    if await redis_client.exists(STREAM):
        for group in await redis_client.xinfo_groups(STREAM):
            name = group.get("name", group.get(b"name"))
            if (name.decode() if isinstance(name, bytes) else name) == CONSUMER_GROUP:
                return False
    try:
        await redis_client.xgroup_create(STREAM, CONSUMER_GROUP, id="$", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
        return False
    return True
