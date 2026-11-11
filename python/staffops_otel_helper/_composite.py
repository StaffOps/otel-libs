"""The helper's global propagator: an upstream ``CompositePropagator`` whose ``extract`` never returns ``None``.

Imported only by :func:`staffops_otel_helper._propagators.build_propagator`, after
``opentelemetry.propagate`` has resolved ``OTEL_PROPAGATORS`` (see ``_propagators``).

Upstream ``CompositePropagator.extract`` returns whatever its last member returned,
and with no members (``OTEL_PROPAGATORS=none``) that is the ``context`` argument:
``None`` when the caller passed none. The ASGI and WSGI instrumentations attach
the result, so every request then failed in the SDK with ``AttributeError:
'NoneType' object has no attribute 'get'``; gRPC servers and consumers that pass
it as a span parent continued the current context instead. All now get a fresh
``Context`` (task A34).
"""

from __future__ import annotations

from opentelemetry.context.context import Context
from opentelemetry.propagators import textmap
from opentelemetry.propagators.composite import CompositePropagator


class HelperCompositePropagator(CompositePropagator):
    """``CompositePropagator`` that always returns a ``Context`` from ``extract``.

    The passed context is returned when the members leave it unchanged; with no
    context passed, a fresh empty ``Context`` (what the W3C TraceContext propagator,
    first in the default list, also starts from). ``inject`` and ``fields`` are the
    upstream ones: with no members nothing is injected.
    """

    def extract(
        self,
        carrier: textmap.CarrierT,
        context: Context | None = None,
        getter: textmap.Getter[textmap.CarrierT] = textmap.default_getter,
    ) -> Context:
        extracted = super().extract(carrier, context, getter=getter)
        return extracted if extracted is not None else Context()
