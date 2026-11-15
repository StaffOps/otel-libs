"""Python smoke program: one span, one metric and one log record, then exit.

Same markers as the Go, Node and .NET smoke programs (testing/e2e/): span
"e2e-smoke-span", counter "e2e.smoke.runs", WARN record "e2e-smoke-log",
written inside the span so the log record carries its trace_id/span_id.
Configuration comes only from the standard OTel environment variables.

Exit code 1, like the other smoke programs, when the telemetry did not leave:
  * shutdown_telemetry raised (a provider shutdown failed or timed out), or
  * an OTLP exporter logged an ERROR record. The Python SDK reports a refused
    or unreachable export only through its logger: shutdown_telemetry() and the
    providers' force_flush() still succeed (measured with SDK 1.42-1.45 against
    a closed port, grpc and http/protobuf), so the exporter loggers are the
    only signal. Transient-error retries log at WARNING and do not count.

This detection is verified by hand, not in CI: run
testing/k8s/images/check-python-refused.sh (local Docker; asserts exit 1 against
a closed port for both protocols). It depends on the SDK's exporter logger
names: if upstream renames them, a refused export exits 0 again.
"""

# staffops_otel_helper first: it must run before any opentelemetry import so
# unsupported OTEL_PROPAGATORS values are skipped instead of raising.
import staffops_otel_helper  # isort: skip  # noqa: F401

import logging
import sys

from staffops_otel_helper import get_meter, get_tracer, setup_telemetry, shutdown_telemetry

logger = logging.getLogger("e2e-python-smoke")

# Parent of the grpc and http OTLP exporter loggers
# (opentelemetry.exporter.otlp.proto.{grpc,http}...).
EXPORTER_LOGGER = "opentelemetry.exporter.otlp"


class _ErrorCounter(logging.Handler):
    """Counts ERROR-or-worse records; the SDK's own handlers still print them."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


def main() -> int:
    export_errors = _ErrorCounter()
    logging.getLogger(EXPORTER_LOGGER).addHandler(export_errors)

    setup_telemetry()
    tracer = get_tracer("e2e-python-smoke")
    runs = get_meter("e2e-python-smoke").create_counter("e2e.smoke.runs")
    with tracer.start_as_current_span("e2e-smoke-span"):
        runs.add(1)
        logger.warning("e2e-smoke-log")
    try:
        shutdown_telemetry(timeout_ms=10_000)
    except RuntimeError as exc:
        print(f"python-smoke: shutdown incomplete: {exc}", file=sys.stderr)
        return 1
    if export_errors.count:
        print(f"python-smoke: {export_errors.count} export error(s) logged by the OTLP exporters",
              file=sys.stderr)
        return 1
    print("python-smoke: emitted span, metric and log", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
