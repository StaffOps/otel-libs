"""Test setup of the Python demo apps (specs/demo-environment DE3b).

- puts the app directories on ``sys.path`` (the images copy them flat to /app);
- generates the gRPC stubs from demo/proto into a temporary directory, as the
  Dockerfiles do at build time (DE-D3);
- gives each test in-memory OpenTelemetry providers: a tracer, a meter (with
  the SDK's default ``trace_based`` exemplar filter) and a logger whose
  handler sits on the demo loggers and on the helper's failure logger
  (``staffops_otel_helper.failure``, the records of ``traced``, D15), so a test can
  check span ↔ log ↔ exemplar ids.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[1]
REPO = DEMO.parents[1]
for sub in ("common", "worker", "api", "backend"):
    sys.path.insert(0, str(DEMO / sub))


def _generate_stubs() -> None:
    from grpc_tools import protoc

    proto_dir = REPO / "demo" / "proto" / "otellibs" / "demo" / "v1"
    out = tempfile.mkdtemp(prefix="demo-stubs-")
    rc = protoc.main(["protoc", f"-I{proto_dir}", f"--python_out={out}",
                      f"--grpc_python_out={out}", str(proto_dir / "demo.proto")])
    if rc != 0:
        raise RuntimeError(f"protoc failed with {rc}")
    sys.path.insert(0, out)


_generate_stubs()

# The helper must be imported before any opentelemetry module (as the apps do).
import staffops_otel_helper  # noqa: E402,F401  isort: skip

from opentelemetry.instrumentation.logging.handler import LoggingHandler  # noqa: E402
from opentelemetry.sdk._logs import LoggerProvider  # noqa: E402
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor  # noqa: E402
from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

try:  # renamed across SDK releases
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter as _InMemoryLogs  # noqa: E402
except ImportError:  # pragma: no cover - older SDK
    from opentelemetry.sdk._logs.export import InMemoryLogExporter as _InMemoryLogs  # noqa: E402

DEMO_LOGGERS = ("demo.backend", "demo.api", "demo.worker", "staffops_otel_helper.failure")


@dataclass
class Otel:
    tracer: object
    meter: object
    spans: InMemorySpanExporter
    logs: object
    metrics: InMemoryMetricReader
    _data: object = None

    def finished(self, name: str | None = None):
        spans = self.spans.get_finished_spans()
        return [s for s in spans if name is None or s.name == name]

    def records(self):
        return [getattr(d, "log_record", d) for d in self.logs.get_finished_logs()]

    def points(self, metric_name: str):
        # One collection per test: the exemplar reservoirs reset at every collect.
        if self._data is None:
            self._data = self.metrics.get_metrics_data()
        data = self._data
        out = []
        for rm in data.resource_metrics if data else []:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    if m.name == metric_name:
                        out.extend((m, p) for p in m.data.data_points)
        return out


@pytest.fixture
def otel():
    spans = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(spans))
    reader = InMemoryMetricReader()
    mp = MeterProvider(metric_readers=[reader])
    logs = _InMemoryLogs()
    lp = LoggerProvider()
    lp.add_log_record_processor(SimpleLogRecordProcessor(logs))
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=lp)
    loggers = [logging.getLogger(n) for n in DEMO_LOGGERS]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    try:
        yield Otel(tp.get_tracer("otel-libs-demo"), mp.get_meter("otel-libs-demo"), spans, logs, reader)
    finally:
        for lg in loggers:
            lg.removeHandler(handler)
        tp.shutdown()
        mp.shutdown()
        lp.shutdown()
