"""Local propagation test — run with: docker run --rm -v $(pwd):/app -w /app python:3.11-slim sh -c 'pip install -e .[dev] opentelemetry-instrumentation-httpx==0.46b0 opentelemetry-instrumentation-fastapi==0.46b0 httpx fastapi -q && python tests/test_propagation.py'"""

from otel_helper import setup_telemetry, TelemetryOptions
from opentelemetry import trace
from opentelemetry.propagate import inject, extract

setup_telemetry(TelemetryOptions(
    service_name="test",
    otel_endpoint="http://localhost:4317",
))

tracer = trace.get_tracer("test")

print("=== Test 1: inject traceparent inside span ===")
with tracer.start_as_current_span("parent"):
    headers = {}
    inject(headers)
    assert "traceparent" in headers, "FAIL: no traceparent"
    print(f"  {headers['traceparent']}")
    print("  PASS")

print()
print("=== Test 2: extract preserves trace_id ===")
ctx = extract(headers)
with tracer.start_as_current_span("child", context=ctx) as child:
    parent_tid = headers["traceparent"].split("-")[1]
    child_tid = format(child.get_span_context().trace_id, "032x")
    assert parent_tid == child_tid, f"FAIL: {parent_tid} != {child_tid}"
    print(f"  trace_id: {parent_tid}")
    print("  PASS")

print()
print("=== Test 3: HTTPX instrumentor active ===")
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
inst = HTTPXClientInstrumentor()
print(f"  is_instrumented: {inst.is_instrumented_by_opentelemetry}")
assert inst.is_instrumented_by_opentelemetry, "FAIL: not instrumented"
print("  PASS")

print()
print("=== Test 4: FastAPI instrumentor active ===")
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
inst2 = FastAPIInstrumentor()
print(f"  is_instrumented: {inst2.is_instrumented_by_opentelemetry}")
assert inst2.is_instrumented_by_opentelemetry, "FAIL: not instrumented"
print("  PASS")

print()
print("ALL PROPAGATION TESTS PASSED ✅")
