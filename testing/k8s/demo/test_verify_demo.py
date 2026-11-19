"""Tests for ``verify.py demo`` (specs/demo-environment DE5, DE-D10, DE-D11).

Standard library unittest; no cluster access. Tempo traces are recorded from the
DE3 local smokes (fixtures/tempo-<lang>-<scenario>.json: the Collector file
exporter's OTLP JSON re-encoded as Tempo's /api/traces answer, base64 ids) and, for
Go, Python and Node.js, read from Tempo on k8s01 for run 20261116t1812 (long
strings cut at 240); Loki and Prometheus answers are built inline in their API
shapes. Edited 2026-11-17 (A45, DE-D8 note): the type-only ``exception`` event of
each owning demo span (``demo.order.process`` / ``demo.order.accept``, scope
``otel-libs-demo``) was removed from the .NET fixtures, as the demo apps no longer
write it; every other span, DB spans included, is as recorded. Go re-recorded
2026-11-18 from the cluster (images ``ea25d10``, Go D16 and A45, traces after
05:30 UTC), Python and Node.js the same day (images ``2eb28e6``, D16 and A45,
traces after 07:25 UTC); none of the three edited.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import verify  # noqa: E402

FIXTURES = HERE / "fixtures"
# Run ids of the recorded smokes (the run tokens in the fixtures carry them).
RUNS = {"go": "20261116t1812", "node": "20261116t1812", "dotnet": "de3dsmoke", "python": "20261116t1812"}
# Fake clock. The Go, Python and Node.js fixtures (recorded 2026-11-18) start after it, from about
# 1_794_981_477 s: the checks do not filter span start times by the window today,
# so the fixtures are used as-is. A check that starts filtering them needs NOW moved.
NOW = 1_794_900_000
W = 900
WEND = NOW - 120
WSTART = WEND - W


def fixture(lang: str, scen: str) -> dict:
    return json.loads((FIXTURES / f"tempo-{lang}-{scen}.json").read_text())


def trace_hex(data: dict) -> str:
    return verify.parse_trace(data)[0].trace_id


def spans_of(lang: str, scen: str) -> list[verify.Span]:
    return verify.parse_trace(fixture(lang, scen))


class FakeBackends:
    """Answers the Backends methods from in-memory data; unknown queries return empty."""

    def __init__(self) -> None:
        self.traces: dict[str, dict] = {}
        self.missing_once: set[str] = set()  # trace ids that 404 on the first fetch
        self.search: list[tuple[tuple[str, ...], list[dict]]] = []
        self.loki_lines_fn = lambda q, s, e, limit: []
        self.loki_entries_fn = None  # (q, start, end, limit) -> [(ts, labels, line)]
        self.metrics: list[dict] | None = None  # TraceQL metrics series; None = query error
        self.loki_vec: dict[str, list[dict]] = {}
        self.prom_vec: list[tuple[str, list[dict]]] = []
        self.ranges: dict[str, list[dict]] = {}
        self.series: list[dict] = []
        self.exemplars: list[dict] = []
        self.flags = {"enable-feature": "exemplar-storage"}
        self.runtime = {"startTime": "2026-11-16T14:14:31.462528298Z"}
        self.calls: list[tuple[str, str]] = []
        self.fail: str = ""

    def add_trace(self, data: dict) -> str:
        tid = trace_hex(data)
        self.traces[tid] = data
        return tid

    def _maybe_fail(self, what: str) -> None:
        if self.fail and self.fail in what:
            raise verify.QueryError(f"{what}: boom")

    def tempo_trace(self, trace_id: str) -> dict | None:
        tid = verify.norm_trace(trace_id)
        self.calls.append(("trace", tid))
        self._maybe_fail("trace")
        if tid in self.missing_once:
            self.missing_once.discard(tid)
            return None
        return copy.deepcopy(self.traces.get(tid))

    def tempo_search(self, traceql: str, start: int, end: int, limit: int = 20) -> list[dict]:
        self.calls.append(("search", traceql))
        self._maybe_fail("search")
        for needles, result in self.search:
            if all(n in traceql for n in needles):
                return result(start, end, limit) if callable(result) else result[:limit]
        return []

    def loki_lines(self, logql: str, start: int, end: int, limit: int = 50):
        self.calls.append(("loki", logql))
        self._maybe_fail("loki")
        return self.loki_lines_fn(logql, start, end, limit)

    def loki_entries(self, logql: str, start: int, end: int, limit: int = 50):
        self.calls.append(("loki", logql))
        self._maybe_fail("loki")
        if self.loki_entries_fn is not None:
            return self.loki_entries_fn(logql, start, end, limit)
        return [(f"{start}-{i}", labels, line)
                for i, (labels, line) in enumerate(self.loki_lines_fn(logql, start, end, limit))]

    def tempo_metrics(self, traceql: str, start: int, end: int, step: int) -> list[dict]:
        self.calls.append(("metrics", traceql))
        if self.metrics is None:
            raise verify.QueryError("metrics: not enabled")
        return self.metrics

    def loki_query(self, logql: str, at: int) -> list[dict]:
        self._maybe_fail("lokiq")
        for needle, vec in self.loki_vec.items():
            if needle in logql:
                return vec
        return []

    def prom_query(self, promql: str, at: int) -> list[dict]:
        self.calls.append(("prom", promql))
        self._maybe_fail("prom")
        for needle, vec in self.prom_vec:
            if needle in promql:
                return vec
        return []

    def prom_query_range(self, promql: str, start: int, end: int, step: int) -> list[dict]:
        self._maybe_fail("range")
        for needle, result in self.ranges.items():
            if promql.startswith(needle):
                return result
        return []

    def prom_series(self, match: str, start: int, end: int) -> list[dict]:
        self._maybe_fail("series")
        return self.series

    def prom_exemplars(self, promql: str, start: int, end: int) -> list[dict]:
        self.calls.append(("exemplars", promql))
        self._maybe_fail("exemplars")
        return self.exemplars

    def prom_status(self, which: str) -> dict:
        self._maybe_fail("status")
        return self.flags if which == "flags" else self.runtime


def make_run(lang: str = "go", **kw) -> verify.DemoRun:
    run = verify.DemoRun(run=kw.pop("run", RUNS.get(lang, "r1")), langs=[lang],
                         namespace="dev-otel-libs", window_start=WSTART, window_end=WEND, now=NOW)
    for key, value in kw.items():
        setattr(run, key, value)
    return run


def instances(lang: str) -> dict:
    return {(lang, app): {(f"dev-otel-libs/demo-{lang}-{app}", f"inst-{lang}-{app}")}
            for app in verify.DEMO_APPS}


def no_sleep(_seconds: float) -> None:
    pass


def cache(b: FakeBackends, sleeps: list | None = None) -> verify.TraceCache:
    return verify.TraceCache(b, sleep=(sleeps.append if sleeps is not None else no_sleep))


def report(out: io.StringIO | None = None) -> verify.DemoReport:
    return verify.DemoReport(verify.load_expected(), out=out or io.StringIO())


class IdEncodingTest(unittest.TestCase):
    """design "Id encodings": every source normalizes to 32 / 16 lower-case hex."""

    def test_base64_hex_and_stripped(self) -> None:
        raw = bytes.fromhex("03af7c3f7f4949d40770b8d30fc2affa")
        self.assertEqual(verify.norm_trace(base64.b64encode(raw).decode()), raw.hex())
        self.assertEqual(verify.norm_trace("3af7c3f7f4949d40770b8d30fc2affa"), raw.hex())  # Tempo search
        self.assertEqual(verify.norm_trace("03AF7C3F7F4949D40770B8D30FC2AFFA"), raw.hex())
        self.assertEqual(verify.norm_span(base64.b64encode(bytes.fromhex("00c0ffee00c0ffee")).decode()),
                         "00c0ffee00c0ffee")
        self.assertEqual(verify.norm_span("c0ffee"), "0000000000c0ffee")
        self.assertEqual(verify.norm_trace(""), "")
        self.assertEqual(verify.norm_trace("not*base64"), "")  # invalid: counted as a miss
        self.assertEqual(verify.norm_span(base64.b64encode(b"x" * 9).decode()), "")  # too long

    def test_flags_and_severity(self) -> None:
        self.assertTrue(verify.sampled({"flags": "3"}))   # Python records carry 3
        self.assertTrue(verify.sampled({"flags": "1"}))
        self.assertFalse(verify.sampled({"flags": "0"}))
        self.assertTrue(verify.sampled({}))
        self.assertTrue(verify.sampled({"flags": "junk"}))
        self.assertTrue(verify.sampled({"flags": "0x0"}))  # base 10 only: not a number, counted sampled
        self.assertEqual(verify.severity_number({"severity_number": "17"}), 17)
        self.assertEqual(verify.severity_number({"severity_number": "x"}), 0)
        self.assertEqual(verify.severity_number({}), 0)

    def test_parse_trace(self) -> None:
        spans = spans_of("go", "ok")
        self.assertTrue(all(len(s.trace_id) == 32 and len(s.span_id) == 16 for s in spans))
        kinds = {s.kind for s in spans}
        self.assertEqual(kinds, {"internal", "client", "server", "producer", "consumer"})
        prod = next(s for s in spans if s.kind == "producer")
        cons = next(s for s in spans if s.kind == "consumer")
        self.assertEqual(cons.parent_id, prod.span_id)
        self.assertEqual(cons.links, [(prod.trace_id, prod.span_id)])
        self.assertEqual(next(s for s in spans if s.name == "demo.drive").parent_id, "")
        err = next(s for s in spans_of("go", "business_error") if s.name == "demo.order.process")
        self.assertEqual((err.status, err.attrs["error.type"]), ("error", "OrderRejectedError"))
        # resourceSpans / numeric enums (OTLP JSON) parse the same way.
        alt = {"resourceSpans": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "x"}}]},
            "scopeSpans": [{"scope": {"name": "s"}, "spans": [{
                "traceId": "ab", "spanId": "cd", "name": "n", "kind": 3,
                "status": {"code": 2}, "attributes": [{"key": "b", "value": {"boolValue": True}},
                                                     {"key": "l", "value": {"arrayValue": {}}}]}]}]}]}
        s = verify.parse_trace(alt)[0]
        self.assertEqual((s.kind, s.status, s.attrs["b"], s.trace_id[-2:]), ("client", "error", "true", "ab"))
        self.assertIn("arrayValue", s.attrs["l"])


class ExpectationTest(unittest.TestCase):
    """DR9: strict xfail and n/a; expected.json is complete and well-formed."""

    def test_judge_matrix(self) -> None:
        j = verify.judge
        self.assertEqual(j("pass", "pass"), "PASS")
        self.assertEqual(j("pass", "fail"), "FAIL")
        self.assertEqual(j("pass", "nodata"), "FAIL")
        self.assertEqual(j("xfail:A36", "fail"), "XFAIL")
        self.assertEqual(j("xfail:A36", "pass"), "XPASS")
        self.assertEqual(j("xfail:A36", "nodata"), "FAIL")  # no data is never an expected failure
        self.assertEqual(j("n/a:x", "nodata"), "NA")
        self.assertEqual(j("n/a:x", "pass"), "UNEXPECTED")
        self.assertEqual(j("n/a:x", "fail"), "UNEXPECTED")
        for e in ("pass", "xfail:A1", "n/a:x"):
            self.assertEqual(j(e, "error"), "FAIL")
            self.assertEqual(j(e, "blocked"), "BLOCKED")
            self.assertEqual(j(e, "unexpected"), "UNEXPECTED")

    def test_shipped_expected_json(self) -> None:
        e = verify.load_expected()
        self.assertEqual(set(e), set(verify.LANGS))
        # Go D16 (images ea25d10, run 20261116t1812): 500 server span error.type, DB edge, 23505.
        self.assertEqual(e["go"]["d.http"]["expect"], "pass")
        self.assertEqual(e["go"]["e.edge.db"]["expect"], "pass")
        # A36 closed (k8s01 run with images 84372c0, 2026-11-19): the four strict xfail:A36
        # cells gave XPASS, so they are plain expected passes with no reasons left.
        for lang, cell in (("go", "d.db"), ("go", "g.sentinel.db"), ("python", "d.db"), ("node", "d.db")):
            self.assertEqual(e[lang][cell], {"expect": "pass", "ref": "", "also": [], "reason": [],
                                             "requires_series": False}, (lang, cell))
        self.assertEqual(e["python"]["g.sentinel.db"]["expect"], "pass")
        self.assertEqual(e["node"]["g.sentinel.db"]["expect"], "pass")
        self.assertEqual(e["dotnet"]["d.grpc"]["expect"], "pass")
        self.assertEqual(e["dotnet"]["e.trace"]["expect"], "pass")
        for cell in verify.EXEMPLAR_CELLS:
            want = "n/a:upstream-no-rpc-metrics" if cell == "b.rpc_server" else "n/a:upstream-no-sdk-exemplars"
            self.assertEqual(e["node"][cell]["expect"], want)  # DE6: Node.js emits no RPC metrics
        self.assertNotIn("A44", json.dumps(json.loads(verify.EXPECTED_JSON.read_text())["expected"]))
        self.assertNotIn("A36", json.dumps(json.loads(verify.EXPECTED_JSON.read_text())["expected"]))
        tasks = {v["expect"] for cells in e.values() for v in cells.values() if v["expect"].startswith("xfail:")}
        self.assertEqual(tasks, {"xfail:A37"})
        self.assertEqual(e["dotnet"]["d.http"]["also"], ["A42"])
        out = io.StringIO()
        report(out).cell("dotnet", "d.http", verify.Obs("fail", "x"))
        self.assertIn("[expected xfail:A37+A42]", out.getvalue())

    def test_bad_files(self) -> None:
        good = json.loads(verify.EXPECTED_JSON.read_text())
        cases = []
        missing = copy.deepcopy(good)
        del missing["expected"]["go"]["a"]
        cases.append((missing, "missing cells"))
        bad = copy.deepcopy(good)
        bad["expected"]["go"]["a"] = "xfail"
        cases.append((bad, "bad expectation"))
        extra = copy.deepcopy(good)
        extra["expected"]["rust"] = {}
        cases.append((extra, "unknown languages"))
        cells = copy.deepcopy(good)
        cells["cells"] = ["a"]
        cases.append((cells, "'cells'"))
        cases.append(({}, "missing object"))
        also = copy.deepcopy(good)
        also["expected"]["go"]["a"] = {"expect": "pass", "also_xfail": ["A42"]}
        cases.append((also, "also_xfail"))
        also2 = copy.deepcopy(good)
        also2["expected"]["go"]["d.db"] = {"expect": "xfail:A36", "also_xfail": "A42"}
        cases.append((also2, "also_xfail"))
        nolang = copy.deepcopy(good)
        del nolang["expected"]["node"]
        cases.append((nolang, "no cells for language"))
        with tempfile.TemporaryDirectory() as tmp:
            for data, msg in cases:
                path = Path(tmp) / "e.json"
                path.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, msg):
                    verify.load_expected(path)

    def test_report_lines_matrix_and_exit(self) -> None:
        out = io.StringIO()
        rep = report(out)
        rep.cell("go", "a", verify.Obs("pass", "ok"))
        self.assertFalse(rep.failed())
        rep.cell("python", "d.grpc", verify.Obs("fail", "x", problems=[
            "grpc_error: server span carries ['event exception.exception.message', "
            "'event exception.exception.stacktrace']"]))
        self.assertFalse(rep.failed())  # XFAIL does not fail the run
        rep.cell("go", "d.http", verify.Obs("pass", "fixed"))
        self.assertFalse(rep.failed())  # Go D16: d.http is an expected pass
        rep.cell("python", "g.sentinel.app", verify.Obs("pass", "fixed"))
        self.assertTrue(rep.failed())  # XPASS does
        rep.matrix(["go", "python"])
        text = out.getvalue()
        self.assertIn("XFAIL       python d.grpc: x [expected xfail:A37] (ref: contrib", text)
        self.assertIn("XPASS       python g.sentinel.app", text)
        self.assertIn("verify demo: PASS 2, XFAIL 1, XPASS 1; FAIL", text)
        # Go D16 sets db.response.status_code: the A44 problem no longer explains a Go d.db XFAIL.
        rep3 = report()
        for lang in ("go", "python", "node"):  # D16 in all three since 2eb28e6
            self.assertEqual(rep3.cell(lang, "d.db", verify.Obs("fail", "x", problems=[
                "db_error: 'q' db.response.status_code None"])), "FAIL")
        rep2 = report(out2 := io.StringIO())
        rep2.blocked("flags")
        self.assertTrue(rep2.failed())
        rep2.cell("go", "a", verify.Obs("pass", "ok"))
        rep2.matrix(["go"])
        # A pre-flight BLOCKED is not a cell, but the tally names it (it is why the run fails).
        self.assertIn("verify demo: PASS 1; pre-flight BLOCKED 1; FAIL", out2.getvalue())


class TraceCacheTest(unittest.TestCase):
    def test_retry_once_after_60s(self) -> None:
        b = FakeBackends()
        tid = b.add_trace(fixture("go", "ok"))
        sid = spans_of("go", "ok")[0].span_id
        b.missing_once.add(tid)  # late root span: absent on the first read
        sleeps: list = []
        ok, misses, retried = cache(b, sleeps).resolve([(tid, sid)])
        self.assertEqual((ok, misses, retried, sleeps), (1, [], 1, [60]))

    def test_permanent_miss_and_wrong_span(self) -> None:
        b = FakeBackends()
        tid = b.add_trace(fixture("go", "ok"))
        sleeps: list = []
        c = cache(b, sleeps)
        ok, misses, retried = c.resolve([(tid, "ffffffffffffffff"), ("ab" * 16, ""), (tid, "")])
        self.assertEqual((ok, len(misses), retried, sleeps), (1, 2, 2, [60]))
        self.assertEqual(c.resolve([(tid, "")])[2], 0)  # nothing to retry: no sleep
        self.assertEqual(sleeps, [60])


def loki_record(trace: str, span: str, svc: str, **extra: str) -> tuple[dict, str]:
    labels = {"trace_id": trace, "span_id": span, "service_name": svc, "flags": "1",
              "service_instance_id": "i", **extra}
    return labels, "line"


class CheckATest(unittest.TestCase):
    """DR5: OTLP records resolve in Tempo; span_id is a span of the trace; the minimum
    counts records on (or below) demo spans only (r3.5, review M6)."""

    def setUp(self) -> None:
        self.b = FakeBackends()
        self.tid = self.b.add_trace(fixture("go", "ok"))
        spans = spans_of("go", "ok")
        self.proc = next(s for s in spans if s.name == "demo.order.process")
        self.cons = next(s for s in spans if s.kind == "consumer")
        self.accept = next(s for s in spans if s.name == "demo.order.accept")
        self.server = next(s for s in spans if s.kind == "server" and s.service.endswith("api"))
        self.db = next(s for s in spans if s.is_postgres())
        self.b.loki_vec["count_over_time"] = [
            {"metric": {"service_name": f"demo-go-{app}"}, "value": [WEND, "100"]} for app in verify.DEMO_APPS]

    def lines(self, *records):
        self.b.loki_lines_fn = lambda q, s, e, limit: list(records)

    def good(self):
        return (loki_record(self.tid, self.proc.span_id, "demo-go-backend"),
                loki_record(self.tid.lstrip("0"), self.cons.span_id, "demo-go-worker"),
                loki_record(self.tid, self.accept.span_id, "demo-go-api"))

    def test_pass(self) -> None:
        self.lines(*self.good(), loki_record("cd" * 16, "1" * 16, "demo-go-api", flags="0"))
        obs = verify.check_a(self.b, make_run(), "go", cache(self.b))
        self.assertEqual(obs.state, "pass", obs.detail)
        self.assertIn("(10 unsampled skipped)", obs.detail)  # the fake answers every slice
        self.assertIn("3 on or below a demo span", obs.detail)

    def test_db_span_below_a_demo_span_counts(self) -> None:
        # e.g. the .NET helper's correlated record on the Npgsql span under demo.order.process.
        self.lines(loki_record(self.tid, self.db.span_id, "demo-go-backend"), *self.good()[1:])
        self.assertEqual(verify.check_a(self.b, make_run(), "go", cache(self.b)).state, "pass")

    def test_framework_records_do_not_count(self) -> None:
        self.lines(loki_record(self.tid, self.server.span_id, "demo-go-api"), *self.good()[:2])
        obs = verify.check_a(self.b, make_run(), "go", cache(self.b))
        self.assertEqual(obs.state, "fail")
        self.assertEqual(obs.problems, ["api: no sampled record on a demo span"])
        self.assertIn("2 on or below a demo span (≈ 200", obs.detail)

    def test_minimum_on_demo_share(self) -> None:
        many = [loki_record(self.tid, self.server.span_id, "demo-go-api")] * 0
        framework = [loki_record(f"{i:032x}", "0" * 15 + "1", "demo-go-api") for i in range(1, 4)]
        for i in range(1, 4):
            self.b.add_trace({"batches": [{"resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "demo-go-api"}}]},
                "scopeSpans": [{"scope": {"name": "fw"}, "spans": [
                    {"traceId": f"{i:032x}", "spanId": "0" * 15 + "1", "kind": "SPAN_KIND_SERVER"}]}]}]})
        self.lines(*self.good(), *framework, *many)
        obs = verify.check_a(self.b, make_run(), "go", cache(self.b))
        self.assertEqual(obs.state, "fail")
        self.assertIn("records on demo spans ≈ 150 (total 300 x share 0.50) < minimum 180", obs.detail)

    def test_unresolved_after_retry_fails(self) -> None:
        self.lines(loki_record(self.tid, "0123456789abcdef", "demo-go-backend"), *self.good())
        sleeps: list = []
        obs = verify.check_a(self.b, make_run(), "go", cache(self.b, sleeps))
        self.assertEqual(obs.state, "fail")
        self.assertIn("do not resolve", obs.detail)
        self.assertEqual(sleeps, [60])

    def test_minimums_and_nodata(self) -> None:
        self.lines(*self.good())
        self.b.loki_vec["count_over_time"] = [{"metric": {"service_name": "demo-go-api"}, "value": [WEND, "5"]}]
        obs = verify.check_a(self.b, make_run(), "go", cache(self.b))
        self.assertEqual(obs.state, "fail")
        self.assertIn("minimum 180", obs.detail)
        self.b.loki_vec.clear()
        self.assertEqual(verify.check_a(self.b, make_run(), "go", cache(self.b)).state, "nodata")

    def test_record_without_span_id_or_invalid_trace(self) -> None:
        self.lines(*self.good(), loki_record("ef" * 16, "", "demo-go-api"),
                   loki_record("zz*", "1" * 16, "demo-go-api"))
        obs = verify.check_a(self.b, make_run(), "go", cache(self.b))
        self.assertIn("1 sampled records carry a trace_id but no span_id", obs.problems)
        self.assertIn("10 sampled records carry an invalid trace_id", obs.problems)  # one per slice

    def test_query_errors(self) -> None:
        self.b.fail = "lokiq"
        self.assertEqual(verify.check_a(self.b, make_run(), "go", cache(self.b)).state, "error")
        self.b.fail = "loki"
        self.assertEqual(verify.check_a(self.b, make_run(), "go", cache(self.b)).state, "error")
        self.b.fail = ""
        self.lines()
        self.assertIn("no sampled record to resolve", verify.check_a(self.b, make_run(), "go", cache(self.b)).detail)


def exemplar_series(instance: str, *pairs: tuple[str, str]) -> dict:
    return {"seriesLabels": {"instance": instance},
            "exemplars": [{"labels": {"trace_id": t, "span_id": s}, "value": "0.01", "timestamp": WEND}
                          for t, s in pairs]}


class CheckBTest(unittest.TestCase):
    """DR6 / DE-D10: _bucket exemplars scoped by target_info, trace and span resolve."""

    def setUp(self) -> None:
        self.b = FakeBackends()
        self.tid = self.b.add_trace(fixture("go", "ok"))
        self.proc = next(s for s in spans_of("go", "ok") if s.name == "demo.order.process")
        self.run = make_run(instances=instances("go"), exemplar_storage=True)

    def test_pass_and_selector(self) -> None:
        self.b.exemplars = [exemplar_series("inst-go-backend", (self.tid, self.proc.span_id))]
        obs = verify.check_b(self.b, self.run, "go", "b.demo_order", cache(self.b))
        self.assertEqual(obs.state, "pass", obs.detail)
        q = next(c[1] for c in self.b.calls if c[0] == "exemplars")
        self.assertTrue(q.startswith("demo_order_duration_seconds_bucket{"))
        self.assertIn('job="dev-otel-libs/demo-go-backend"', q)
        self.assertIn('instance=~"inst\\\\-go\\\\-backend"', q)

    def test_dotnet_rpc_override(self) -> None:
        run = make_run("dotnet", instances=instances("dotnet"), exemplar_storage=True)
        verify.check_b(self.b, run, "dotnet", "b.rpc_server", cache(self.b))
        q = next(c[1] for c in self.b.calls if c[0] == "exemplars")
        self.assertTrue(q.startswith("http_server_request_duration_seconds_bucket{"))
        self.assertIn('http_route="/demo.v1.OrderService/PlaceOrder"', q)

    def test_span_not_in_trace_fails(self) -> None:
        self.b.exemplars = [exemplar_series("inst-go-backend", (self.tid, "00000000deadbeef"))]
        obs = verify.check_b(self.b, self.run, "go", "b.demo_order", cache(self.b))
        self.assertEqual(obs.state, "fail")
        self.assertIn("0/1 (trace, span) resolve", obs.detail)

    def test_other_instance_ignored_nodata(self) -> None:
        self.b.exemplars = [exemplar_series("someone-else", (self.tid, self.proc.span_id))]
        obs = verify.check_b(self.b, self.run, "go", "b.demo_order", cache(self.b))
        self.assertEqual(obs.state, "nodata")
        self.assertEqual(verify.judge("n/a:upstream-no-sdk-exemplars", obs.state), "NA")

    def test_node_na_is_strict(self) -> None:
        run = make_run("node", instances=instances("node"), exemplar_storage=True)
        self.b.exemplars = [exemplar_series("inst-node-api", (self.tid, self.proc.span_id))]
        obs = verify.check_b(self.b, run, "node", "b.http_server", cache(self.b))
        rep = report()
        self.assertEqual(rep.cell("node", "b.http_server", obs), "UNEXPECTED")

    def test_blocked_and_nodata(self) -> None:
        run = make_run(exemplar_storage=False, instances=instances("go"))
        self.assertEqual(verify.check_b(self.b, run, "go", "b.http_server", cache(self.b)).state, "blocked")
        run = make_run(flags_error="x", instances=instances("go"))
        self.assertEqual(verify.check_b(self.b, run, "go", "b.http_server", cache(self.b)).state, "blocked")
        run = make_run(exemplar_storage=True)
        self.assertEqual(verify.check_b(self.b, run, "go", "b.http_server", cache(self.b)).state, "nodata")
        self.b.fail = "exemplars"
        self.assertEqual(verify.check_b(self.b, self.run, "go", "b.http_server", cache(self.b)).state, "error")


def range_series(scen: str, outcome: str, values: list[tuple[int, float]], **labels: str) -> dict:
    return {"metric": {"demo_scenario": scen, "demo_outcome": outcome, **labels},
            "values": [[t, str(v)] for t, v in values]}


def metric_series(name: str, scen: str, outcome: str, **labels: str) -> dict:
    out = {"__name__": name, "demo_scenario": scen, "demo_outcome": outcome, **labels}
    if outcome == "error":
        out.setdefault("error_type", verify.PINNED_TYPES.get(scen, "OrderRejectedError"))
    return out


class CheckCTest(unittest.TestCase):
    """DR7: names, labels, bounds, structure and increase per backend scenario."""

    def setUp(self) -> None:
        self.b = FakeBackends()
        grow = [(WSTART, 1.0), (WSTART + 300, 5.0), (WEND, 9.0)]
        for metric in ("demo_orders_processed_total", "demo_order_duration_seconds_count"):
            self.b.ranges[metric] = [range_series("ok", "ok", grow),
                                     range_series("business_error", "error", grow),
                                     range_series("db_error", "error", grow)]
        les = [str(b) for b in verify.DURATION_BOUNDS] + ["+Inf"]
        self.b.series = (
            [metric_series("demo_orders_processed_total", s, o) for s, o in
             (("ok", "ok"), ("business_error", "error"), ("db_error", "error"))]
            + [metric_series("demo_order_duration_seconds_bucket", "ok", "ok", le=le) for le in les]
            + [metric_series("demo_order_duration_seconds_sum", "ok", "ok"),
               metric_series("demo_order_duration_seconds_count", "ok", "ok")])
        self.run = make_run(instances=instances("go"))

    def test_pass(self) -> None:
        obs = verify.check_c(self.b, self.run, "go")
        self.assertEqual(obs.state, "pass", obs.detail)
        self.assertIn("ok=8", obs.detail)

    def test_series_increase(self) -> None:
        self.assertEqual(verify.series_increase([[WSTART, "3"], [WSTART + 30, "5"]], WSTART), 2)
        self.assertEqual(verify.series_increase([[WSTART, "3"], [WSTART + 30, "1"]], WSTART), 1)  # reset
        self.assertEqual(verify.series_increase([[WSTART + 60, "4"], [WSTART + 90, "6"]], WSTART), 6)  # born
        self.assertEqual(verify.series_increase([], WSTART), 0)

    def test_metric_error_type_equals_the_span_value(self) -> None:
        """DE-D9: error_type on business_error / db_error series is the pinned literal."""
        self.b.series[2]["error_type"] = "OrderRejectedError"  # db_error series with the wrong type
        obs = verify.check_c(self.b, self.run, "go")
        self.assertEqual(obs.state, "fail")
        self.assertIn("demo_orders_processed_total{demo_scenario=db_error} error_type 'OrderRejectedError' "
                      "(want OrderExistsError)", obs.detail)
        self.b.series[2]["error_type"] = "*errors.errorString"
        self.b.series.append(metric_series("demo_order_duration_seconds_count", "business_error", "error",
                                           error_type="democommon.OrderRejectedError"))
        obs = verify.check_c(self.b, self.run, "go")
        self.assertEqual(len(obs.problems), 2, obs.problems)
        self.assertIn("demo_order_duration_seconds_count{demo_scenario=business_error} error_type "
                      "'democommon.OrderRejectedError'", obs.detail)
        # An ok series carries no error_type and is not compared; the right values pass.
        self.b.series[2]["error_type"] = "OrderExistsError"
        self.b.series[-1]["error_type"] = "OrderRejectedError"
        self.assertEqual(verify.check_c(self.b, self.run, "go").state, "pass")

    def test_unhandled_series_is_unexpected(self) -> None:
        self.b.ranges["demo_orders_processed_total"].append(range_series("unhandled", "error", [(WEND, 1)]))
        obs = verify.check_c(self.b, self.run, "go")
        self.assertEqual(obs.state, "unexpected")
        self.assertEqual(verify.judge("pass", obs.state), "UNEXPECTED")

    def test_missing_scenario_and_structure(self) -> None:
        self.b.ranges["demo_orders_processed_total"] = self.b.ranges["demo_orders_processed_total"][:1]
        self.b.series = [s for s in self.b.series if s.get("le") not in ("+Inf", "2.5")]
        self.b.series.append(metric_series("demo_orders_processed_bucket", "ok", "ok"))
        self.b.series.append({"__name__": "demo_order_duration_seconds_sum", "demo_scenario": "ok"})
        obs = verify.check_c(self.b, self.run, "go")
        self.assertEqual(obs.state, "fail")
        for text in ("demo_scenario=business_error}} increase 0".replace("}}", "}"),
                     "want exactly demo_orders_processed_total", 'no le="+Inf"', "!= DE-D9",
                     "without demo_scenario/demo_outcome"):
            self.assertIn(text, obs.detail)

    def test_error_type_only_on_errors(self) -> None:
        self.b.series[0]["error_type"] = "X"  # ok series with error_type
        obs = verify.check_c(self.b, self.run, "go")
        self.assertIn("error_type present", obs.detail)
        self.b.series[0].pop("error_type")
        self.b.series[1].pop("error_type")
        self.assertIn("error_type absent", verify.check_c(self.b, self.run, "go").detail)
        self.b.series[1]["error_type"] = "X"
        self.b.series.append(metric_series("demo_order_duration_seconds_bucket", "ok", "ok", le="abc"))
        self.assertIn("non-numeric le", verify.check_c(self.b, self.run, "go").detail)

    def test_nodata_and_errors(self) -> None:
        self.assertEqual(verify.check_c(self.b, make_run(), "go").state, "nodata")
        empty = FakeBackends()
        self.assertEqual(verify.check_c(empty, self.run, "go").state, "nodata")
        self.b.fail = "range"
        self.assertEqual(verify.check_c(self.b, self.run, "go").state, "error")


def record_lines(lang: str, owner: bool = True, extra: tuple = ()):
    """Loki fake for check d: per fixture trace, the owning span's one ERROR record (A45)
    and the helper record of each failed PostgreSQL span; ``extra`` adds (trace id, labels).
    Answers trace-wide queries and span-scoped ones (``| span_id="..."``)."""
    recs = []
    for sc in verify.ERROR_SCENARIOS:
        app, name = verify.OWNER[sc]
        for sp in spans_of(lang, sc):
            base = {"service_name": sp.service, "span_id": sp.span_id, "severity_number": "17",
                    "exception_stacktrace": "at x"}
            if owner and sp.scope == verify.DEMO_SCOPE and sp.name == name and sp.service == verify.svc(lang, app):
                recs.append((sp.trace_id, {**base, "exception_type": verify.PINNED_TYPES[sc]}))
            if sp.is_postgres() and sp.status == "error":
                recs.append((sp.trace_id, {**base, "exception_type": "PostgresException"}))
    recs.extend(extra)

    def lines(q, s, e, limit):
        tid = q.split('trace_id="', 1)[1].split('"', 1)[0] if 'trace_id="' in q else None
        sid = q.rsplit('span_id="', 1)[1].split('"', 1)[0] if 'span_id="' in q else None
        return [(dict(labels), "msg") for t, labels in recs
                if (tid is None or verify.norm_trace(t) == tid)
                and (sid is None or verify.norm_span(labels["span_id"]) == sid)]
    return lines


class CheckDTest(unittest.TestCase):
    """DR3 / DE-D11 d: owning span, gRPC, HTTP and DB span sets on the recorded smokes."""

    def setup_lang(self, lang: str, records: bool = True) -> FakeBackends:
        b = FakeBackends()
        for scen in verify.ERROR_SCENARIOS:
            tid = b.add_trace(fixture(lang, scen))
            b.search.append(((f'span.demo.scenario="{scen}"',), [{"traceID": tid.lstrip("0")}]))
        b.search.append((("span.error.type !~",), [{"spanSet": {"matched": 2}}]))
        if records:
            b.loki_lines_fn = record_lines(lang)
        return b

    def cells(self, lang: str, b: FakeBackends | None = None, out: io.StringIO | None = None) -> dict:
        b = b or self.setup_lang(lang)
        rep = report(out)
        return verify.check_d(b, make_run(lang), lang, cache(b), rep)

    def test_go(self) -> None:
        out = io.StringIO()
        c = self.cells("go", out=out)
        self.assertEqual(c["d.demo"].state, "pass", c["d.demo"].detail)
        self.assertEqual(c["d.grpc"].state, "pass", c["d.grpc"].detail)
        self.assertEqual(c["d.http"].state, "pass", c["d.http"].detail)  # D16: api SERVER error.type 500
        # The fixture is the pre-A36 recording: XSAM still records exception.message there.
        self.assertEqual(c["d.db"].state, "fail")
        self.assertIn("run token", c["d.db"].detail)
        self.assertIn("exception events [['exception.message', 'exception.type']]", c["d.db"].detail)
        self.assertNotIn("db.response.status_code", c["d.db"].detail)  # D16: 23505 on the span
        self.assertEqual(len(c["d.db"].problems), 2, c["d.db"].problems)
        self.assertIn("2 worker -> api HTTP CLIENT span(s) with a connection-error", out.getvalue())
        e = verify.load_expected()["go"]
        # A36 closed (2026-11-19): telemetry shaped like the pre-A36 fixture now fails the run.
        self.assertEqual([verify.judge(e[k]["expect"], c[k].state) for k in c],
                         ["PASS", "PASS", "PASS", "FAIL"])

    def test_python(self) -> None:
        c = self.cells("python")
        self.assertEqual(c["d.demo"].state, "pass", c["d.demo"].detail)
        self.assertEqual(c["d.grpc"].state, "fail")  # aio server records AbortError (A37)
        self.assertIn("server span carries", c["d.grpc"].detail)
        self.assertNotIn("client description", c["d.grpc"].detail)  # 'order rejected' is allowed
        self.assertEqual(c["d.http"].state, "fail")  # FastAPI exception event (A37)
        self.assertIn("exception.stacktrace", c["d.http"].detail)
        # No exception event is fine since 2026-11-19 (observability ruling, A36 review): with
        # the synthetic Loki records of this fixture the cell passes; without a record it fails
        # on the record only. The k8s01 run with the A36 images (84372c0) passed it.
        self.assertEqual(c["d.db"].state, "pass", c["d.db"].detail)
        bare = self.cells("python", self.setup_lang("python", records=False))
        self.assertEqual(bare["d.db"].problems, [p for p in bare["d.db"].problems if "no correlated ERROR record" in p])
        self.assertTrue(bare["d.db"].problems)
        e = verify.load_expected()["python"]
        self.assertEqual([verify.judge(e[k]["expect"], c[k].state) for k in c],
                         ["PASS", "XFAIL", "XFAIL", "PASS"])
        self.assertEqual(verify.judge(e["d.db"]["expect"], bare["d.db"].state), "FAIL")

    def test_node(self) -> None:
        c = self.cells("node")
        self.assertEqual(c["d.demo"].state, "pass", c["d.demo"].detail)
        self.assertEqual(c["d.grpc"].state, "fail")
        self.assertIn("grpc.error_message", c["d.grpc"].detail)
        self.assertEqual(c["d.http"].state, "fail")
        self.assertIn("request handler", c["d.http"].detail)  # express framework INTERNAL span
        # Pre-A36 fixture: the pg event keeps exception.message, which the verifier flags.
        self.assertEqual(c["d.db"].state, "fail")
        self.assertIn("exception events [['exception.message']]", c["d.db"].detail)
        self.assertNotIn("db.response.status_code", c["d.db"].detail)  # D16: 23505 on the span
        self.assertEqual(len(c["d.db"].problems), 1, c["d.db"].problems)
        e = verify.load_expected()["node"]
        self.assertEqual([verify.judge(e[k]["expect"], c[k].state) for k in c],
                         ["PASS", "XFAIL", "XFAIL", "FAIL"])  # A36 closed: a regression fails

    def test_dotnet(self) -> None:
        c = self.cells("dotnet")
        self.assertEqual(c["d.demo"].state, "pass", c["d.demo"].detail)
        self.assertEqual(c["d.grpc"].state, "pass", c["d.grpc"].detail)
        self.assertEqual(c["d.http"].state, "fail")
        self.assertIn("exception.stacktrace", c["d.http"].detail)
        self.assertEqual(c["d.db"].state, "pass", c["d.db"].detail)

    def test_missing_records_and_traces(self) -> None:
        c = self.cells("go", self.setup_lang("go", records=False))
        self.assertIn("0 ERROR records on or below demo spans, 0 at the owning span", c["d.demo"].detail)
        b = FakeBackends()
        c = self.cells("go", b)
        self.assertTrue(all(o.state == "nodata" for o in c.values()))
        b = self.setup_lang("go")
        b.fail = "search"
        self.assertTrue(all(o.state == "error" for o in self.cells("go", b).values()))

    def test_owning_span_rules(self) -> None:
        spans = spans_of("go", "business_error")
        owner = next(s for s in spans if s.name == "demo.order.process")
        owner.attrs["error.type"] = "Wrong"
        owner.events.append(("exception", {"exception.type": "X", "exception.message": "m"}))
        owner.status = "unset"
        owner.message = "amount over limit"
        b = FakeBackends()
        b.loki_lines_fn = record_lines("go")
        problems = verify._d_demo(b, make_run(), "go", "business_error", spans)
        self.assertEqual(len(problems), 4, problems)
        self.assertIn("has a status description (want empty)", problems[1])
        self.assertIn("(want none)", problems[3])
        spans = [s for s in spans if s.name != "demo.order.process"]
        self.assertIn("missing", verify._d_demo(b, make_run(), "go", "business_error", spans)[0])

    def test_all_fixture_traces_pass_d_demo(self) -> None:
        """Every language and error scenario: one record at the owning span, propagating spans clean."""
        for lang in ("go", "python", "node", "dotnet"):
            b = FakeBackends()
            b.loki_lines_fn = record_lines(lang)
            for scen in verify.ERROR_SCENARIOS:
                self.assertEqual(verify._d_demo(b, make_run(lang), lang, scen, spans_of(lang, scen)), [],
                                 f"{lang} {scen}")

    def test_owning_span_wants_no_exception_event(self) -> None:
        """A45 (DE-D8 note 2026-11-17): even a type-only event on the owning span fails."""
        for lang in ("go", "python", "node", "dotnet"):
            b = FakeBackends()
            b.loki_lines_fn = record_lines(lang)
            spans = spans_of(lang, "business_error")
            owner = next(s for s in spans if s.name == "demo.order.process")
            owner.events.append(("exception", {"exception.type": "OrderRejectedError"}))
            problems = verify._d_demo(b, make_run(lang), lang, "business_error", spans)
            self.assertEqual(problems, ["business_error: demo.order.process exception events "
                                        "[['exception.type']] (want none)"], lang)

    def test_record_type_is_pinned_in_every_language(self) -> None:
        """The record's exception_type equals the pinned literal, .NET included (DE-D8 measured the short name)."""
        for lang in ("go", "python", "node", "dotnet"):
            spans = spans_of(lang, "business_error")
            owner = next(s for s in spans if s.name == "demo.order.process")
            b = FakeBackends()
            b.loki_lines_fn = lambda *a, o=owner: [({"span_id": o.span_id, "severity_number": "17",
                                                      "exception_type": "Other", "exception_stacktrace": "s"}, "x")]
            problems = verify._d_demo(b, make_run(lang), lang, "business_error", spans)
            self.assertEqual(problems, [f"business_error: {lang} record exception_type 'Other'"], lang)
            b.loki_lines_fn = lambda *a, o=owner: [({"span_id": o.span_id, "severity_number": "17"}, "x")]
            self.assertEqual(verify._d_demo(b, make_run(lang), lang, "business_error", spans),
                             ["business_error: ERROR record without exception_stacktrace/exception_type"])

    def test_trace_record_budget(self) -> None:
        """Exactly one ERROR record per failure on or below demo spans, and it is on the owning span."""
        spans = spans_of("go", "db_error")
        tid = spans[0].trace_id
        accept = next(s for s in spans if s.name == "demo.order.accept")
        drive = next(s for s in spans if s.name == "demo.drive")
        server = next(s for s in spans if s.kind == "server" and "PlaceOrder" in s.name)
        client = next(s for s in spans if s.kind == "client" and "PlaceOrder" in s.name)
        # Not counted: the DB span's record (d.db's, in record_lines), an INFO/WARN record, and a
        # framework record on a span outside every demo span (the gRPC SERVER span, A37 debt).
        warn = (tid, {"service_name": drive.service, "span_id": drive.span_id, "severity_number": "13"})
        framework = (tid, {"service_name": server.service, "span_id": server.span_id, "severity_number": "17"})
        b = FakeBackends()
        b.loki_lines_fn = record_lines("go", extra=(warn, framework))
        self.assertEqual(verify._d_demo(b, make_run(), "go", "db_error", spans), [])
        # A second ERROR record below a demo span (the api's gRPC CLIENT span under accept) counts.
        extra = (tid, {"service_name": client.service, "span_id": client.span_id, "severity_number": "17"})
        b.loki_lines_fn = record_lines("go", extra=(extra,))
        (problem,) = verify._d_demo(b, make_run(), "go", "db_error", spans)
        self.assertIn("2 ERROR records on or below demo spans, 1 at the owning span", problem)
        self.assertIn(f"others at ['{client.service}/{client.span_id}']", problem)
        # The only record sits on a propagating span instead of the owning span.
        moved = (tid, {"service_name": accept.service, "span_id": accept.span_id, "severity_number": "17",
                       "exception_type": "OrderExistsError", "exception_stacktrace": "s"})
        b.loki_lines_fn = record_lines("go", owner=False, extra=(moved,))
        problems = verify._d_demo(b, make_run(), "go", "db_error", spans)
        self.assertIn("1 ERROR records on or below demo spans, 0 at the owning span", problems[0])
        self.assertIn("1 ERROR records at the propagating span demo.order.accept (want 0)", problems[1])
        # A record without span_id is counted and named.
        nospan = (tid, {"service_name": accept.service, "severity_number": "17"})
        b.loki_lines_fn = record_lines("go", extra=(nospan,))
        self.assertIn("no span_id", verify._d_demo(b, make_run(), "go", "db_error", spans)[0])

    def test_propagating_span_shapes(self) -> None:
        """api demo.order.accept on 422/409 and worker demo.drive: ERROR, no description,
        error.type = the HTTP status code, no exception event (DE-D8, design item 3)."""
        for scen in ("business_error", "db_error"):
            spans = spans_of("node", scen)
            accept = next(s for s in spans if s.name == "demo.order.accept")
            drive = next(s for s in spans if s.name == "demo.drive")
            accept.attrs.pop("error.type")
            accept.message = "x"
            accept.events.append(("exception", {"exception.type": "X"}))
            drive.attrs["error.type"] = "Error"
            b = FakeBackends()
            b.loki_lines_fn = record_lines("node")
            problems = verify._d_demo(b, make_run("node"), "node", scen, spans)
            code = verify.HTTP_CODES[scen]
            self.assertEqual(problems, [
                f"{scen}: demo.drive error.type 'Error' (want {code})",
                f"{scen}: demo.order.accept status error description set (want error, empty)",
                f"{scen}: demo.order.accept error.type None (want {code})",
                f"{scen}: demo.order.accept has an exception event (want none)"], scen)
        # unhandled: demo.order.accept is the owner; only demo.drive is checked as propagating.
        spans = spans_of("node", "unhandled")
        drive = next(s for s in spans if s.name == "demo.drive")
        drive.status = "unset"
        b = FakeBackends()
        b.loki_lines_fn = record_lines("node")
        self.assertEqual(verify._d_demo(b, make_run("node"), "node", "unhandled", spans),
                         ["unhandled: demo.drive status unset description empty (want error, empty)"])
        spans = [s for s in spans_of("node", "unhandled") if s.name != "demo.drive"]
        self.assertEqual(verify._d_demo(b, make_run("node"), "node", "unhandled", spans),
                         ["unhandled: propagating span demo-node-worker demo.drive missing"])

    def test_grpc_and_http_rules(self) -> None:
        spans = spans_of("go", "business_error")
        client = next(s for s in spans if s.kind == "client" and "PlaceOrder" in s.name)
        client.message = "detail rej-x"
        client.status = "unset"
        server = next(s for s in spans if s.kind == "server" and "PlaceOrder" in s.name)
        server.status = "error"
        problems = verify._d_grpc("go", "business_error", spans)
        self.assertEqual(len(problems), 3, problems)
        self.assertEqual(len(verify._d_grpc("go", "business_error", [])), 2)
        api_server = next(s for s in spans if s.kind == "server" and s.service.endswith("api"))
        api_server.status = "error"
        worker = next(s for s in spans if s.kind == "client" and "http.request.method" in s.attrs)
        worker.attrs["error.type"] = "500"
        problems = verify._d_http("go", "business_error", spans)
        self.assertEqual(len(problems), 2, problems)
        self.assertEqual(verify._d_http("go", "business_error", []), ["business_error: api SERVER span missing"])
        no_worker = [s for s in spans if s is not worker]
        self.assertIn("worker HTTP CLIENT span missing", " ".join(verify._d_http("go", "db_error", no_worker)))
        unh = spans_of("go", "unhandled")
        next(s for s in unh if s.kind == "server").status = "unset"
        self.assertIn("api SERVER status unset", verify._d_http("go", "unhandled", unh)[0])
        unh = spans_of("go", "unhandled")  # fresh copy: the recorded span has error.type 500 (D16)
        server = next(s for s in unh if s.kind == "server" and s.service.endswith("api"))
        self.assertEqual(verify._d_http("go", "unhandled", unh), [])
        server.attrs.pop("error.type")
        self.assertIn("unhandled: api SERVER error.type None (want 500)", verify._d_http("go", "unhandled", unh))

    def test_db_rules(self) -> None:
        b = FakeBackends()
        self.assertEqual(verify._d_db(b, make_run(), "go", []),
                         ["db_error: no PostgreSQL CLIENT span with status error"])
        spans = spans_of("dotnet", "db_error")
        db = next(s for s in spans if s.is_postgres() and s.status == "error")
        db.message = "x"
        problems = verify._d_db(b, make_run("dotnet"), "dotnet", spans)
        self.assertTrue(any("status description" in p for p in problems))
        self.assertTrue(any("no correlated ERROR record" in p for p in problems))
        # Exception events (ruling 2026-11-19): none or one type-only event passes; two, or one
        # with any other attribute, fail.
        db.message = ""
        for events, bad in (([], False), ([("exception", {"exception.type": "T"})], False),
                            ([("exception", {"exception.type": "T"})] * 2, True),
                            ([("exception", {"exception.type": "T", "exception.message": "m"})], True)):
            db.events = events
            got = [p for p in verify._d_db(b, make_run("dotnet"), "dotnet", spans) if "exception events" in p]
            self.assertEqual(bool(got), bad, (events, got))

    def test_span_sets(self) -> None:
        sets = {}
        for scen in verify.ERROR_SCENARIOS:
            for s in spans_of("node", scen):
                sets.setdefault(verify.span_set_d(s, "node"), set()).add(s.name)
        self.assertIn("demo.order.process", sets["d.demo"])
        self.assertIn("demo.v1.OrderService/PlaceOrder", sets["d.grpc"])
        self.assertIn("request handler - /orders", sets["d.http"])
        self.assertIn("POST", sets["d.http"])
        self.assertIn("pg.query:INSERT demo_node", sets["d.db"])
        self.assertIn("XADD", sets.get(None, set()) | {"XADD"})


class CheckETest(unittest.TestCase):
    """DR8: the expected `ok` trace and the service-graph edge rules."""

    def test_ok_traces_match(self) -> None:
        for lang in ("go", "python", "node", "dotnet"):
            self.assertEqual(verify.match_ok_trace(spans_of(lang, "ok"), lang), [], lang)

    def test_consumer_link_and_parent_required(self) -> None:
        spans = spans_of("go", "ok")
        cons = next(s for s in spans if s.kind == "consumer")
        cons.links = []
        self.assertEqual(verify.match_ok_trace(spans, "go"),
                         ["12 CONSUMER (parent = link = PRODUCER)", "13 Redis XACK"])
        spans = spans_of("go", "ok")
        spans = [s for s in spans if s.name != "demo.drive"]
        self.assertEqual(len(verify.match_ok_trace(spans, "go")), 13)
        spans = [s for s in spans_of("go", "ok") if s.kind != "producer"]
        self.assertIn("10 PRODUCER send", verify.match_ok_trace(spans, "go"))

    def test_check_e_trace(self) -> None:
        b = FakeBackends()
        tid = b.add_trace(fixture("node", "ok"))
        start = str((WEND - 60) * 10**9)
        b.search.append((('name="demo.drive"',), [{"traceID": tid, "startTimeUnixNano": start}]))
        obs = verify.check_e_trace(b, make_run("node"), "node", cache(b))
        self.assertEqual(obs.state, "pass", obs.detail)
        b.traces[tid] = {"batches": []}
        self.assertEqual(verify.check_e_trace(b, make_run("node"), "node", cache(b)).state, "fail")
        b.search[0] = (('name="demo.drive"',), [{"traceID": tid, "startTimeUnixNano": str(NOW * 10**9)}])
        self.assertEqual(verify.check_e_trace(b, make_run("node"), "node", cache(b)).state, "nodata")
        b.fail = "search"
        self.assertEqual(verify.check_e_trace(b, make_run("node"), "node", cache(b)).state, "error")

    def edges(self, *rows: tuple[str, str, str, str, float]) -> FakeBackends:
        b = FakeBackends()
        vec = []
        for client, server, ctype, failed, value in rows:
            metric = {"client": client, "server": server, "failed": failed}
            if ctype:
                metric["connection_type"] = ctype
            vec.append({"metric": metric, "value": [WEND, str(value)]})
        b.prom_vec.append(("traces_service_graph_request_total", vec))
        b.search.append((("link:spanID != nil",), [{"traceID": "a"}] * 100))
        return b

    def test_edges(self) -> None:
        good = [("demo-node-worker", "demo-node-api", "", "false", 126), ("demo-node-worker", "demo-node-api", "", "true", 54),
                ("demo-node-api", "demo-node-backend", "", "false", 126),
                ("demo-node-api", "demo-node-worker", "messaging_system", "false", 120),
                ("demo-node-backend", "demo_node", "database", "false", 300),
                ("demo-node-api", "redis", "virtual_node", "false", 10)]
        out = io.StringIO()
        res = verify.check_e_edges(self.edges(*good), make_run("node"), "node", report(out))
        self.assertEqual(res["e.edges"].state, "pass", res["e.edges"].detail)
        self.assertIn("failed share 0.30", res["e.edges"].detail)
        self.assertEqual(res["e.edge.db"].state, "pass")
        self.assertIn("redis[virtual_node]=10.0", out.getvalue())
        bad = [("demo-go-api", "demo-go-worker", "messaging_system", "false", 50),
               ("demo-go-backend", "postgresql", "virtual_node", "false", 300)]
        res = verify.check_e_edges(self.edges(*bad), make_run("go"), "go", report())
        self.assertEqual(res["e.edges"].state, "fail")
        self.assertIn("worker -> api edge (no connection_type) has no requests", res["e.edges"].detail)
        self.assertIn("50.0 < 0.8 x 126.0", res["e.edges"].detail)
        self.assertEqual(res["e.edge.db"].state, "fail")
        self.assertIn("postgresql[virtual_node]", res["e.edge.db"].detail)
        res = verify.check_e_edges(self.edges(), make_run("go"), "go", report())
        self.assertEqual(res["e.edge.db"].state, "nodata")

    def test_edge_errors(self) -> None:
        b = self.edges()
        b.fail = "prom"
        self.assertTrue(all(o.state == "error" for o in verify.check_e_edges(b, make_run(), "go", report()).values()))
        b = self.edges(("demo-go-worker", "demo-go-api", "", "false", 1))
        b.search = []
        res = verify.check_e_edges(b, make_run(), "go", report())
        self.assertIn("no CONSUMER span with a link", res["e.edges"].detail)

    def test_dropped_spans_info(self) -> None:
        b = FakeBackends()
        b.prom_vec.append(("servicegraph_dropped", [{"metric": {}, "value": [WEND, "3"]}]))
        out = io.StringIO()
        verify.servicegraph_dropped(b, make_run(), report(out))
        self.assertIn("dropped spans in the window 3", out.getvalue())
        verify.servicegraph_dropped(FakeBackends(), make_run(), report(out))
        self.assertIn("dropped spans in the window unmeasured", out.getvalue())
        b.fail = "prom"
        verify.servicegraph_dropped(b, make_run(), report(out))
        self.assertIn("unreadable", out.getvalue())


class HygieneTest(unittest.TestCase):
    """DR12: anchored token, paging without a silent cap, span sets, Loki line + metadata."""

    SECRET = "ab" * 24  # 48 hex characters, the shape of a generated password

    def test_patterns(self) -> None:
        self.assertEqual(verify.scan_secret(f"x-{self.SECRET}-y"), ["hex48"])
        self.assertEqual(verify.scan_secret(self.SECRET), ["hex48"])
        self.assertEqual(verify.scan_secret(self.SECRET + "a"), [])  # 49 hex: not anchored
        self.assertEqual(verify.scan_secret("PassWord=x"), ["password="])
        self.assertEqual(verify.scan_secret("postgres://u:p@h/db"), ["userinfo"])
        self.assertEqual(verify.scan_secret("http://h/db"), [])

    def test_scan_intervals_window_plus_startup(self) -> None:
        # DE-D11 M7 (owner 2026-11-18): run window + STARTUP_SCAN after each pod start.
        old = WSTART - 14 * 3600
        self.assertEqual(verify.scan_intervals([old, old + 100], WSTART, WEND),
                         [(old, old + 100 + verify.STARTUP_SCAN), (WSTART, WEND)])
        # A start inside the window or touching it merges; one after window_end is dropped.
        self.assertEqual(verify.scan_intervals([WSTART - 200, WEND + 5], WSTART, WEND),
                         [(WSTART - 200, WEND)])
        self.assertEqual(verify.scan_intervals([], WSTART, WEND), [(WSTART, WEND)])
        touch = WSTART - verify.STARTUP_SCAN
        self.assertEqual(verify.scan_intervals([touch], WSTART, WEND), [(touch, WEND)])
        self.assertEqual(verify.scan_intervals([touch - 1], WSTART, WEND),
                         [(touch - 1, WSTART - 1), (WSTART, WEND)])
        self.assertEqual(verify.scan_intervals([WEND], WSTART, WEND), [(WSTART, WEND)])

    def test_g_scan_bounded_to_intervals(self) -> None:
        # A pod 14 h old: no Tempo search or Loki query spans the hours between its
        # first minutes and the run window.
        old = WSTART - 14 * 3600
        b = FakeBackends()
        spans: list[tuple[int, int]] = []

        def rec(s, e, limit):
            spans.append((s, e))
            return []
        b.search.append((("resource.otel_libs.run",), rec))
        verify.check_g(b, make_run(pods={"go": [("demo-go-api-1-x", old)]}, latest_starts=[old]),
                       "go", cache(b), {})
        # The three server-side token searches keep the full range (one search each, up
        # to 1000 traces fetched per search); every listing search is bounded.
        self.assertEqual(spans.count((old, WEND)), 3)
        self.assertEqual(sorted(set(spans) - {(old, WEND)}), [(old, old + verify.STARTUP_SCAN), (WSTART, WEND)])
        loki: list[tuple[int, int]] = []

        def lines(q, s, e, limit):
            loki.append((s, e))
            return []
        b.loki_lines_fn = lines
        verify.loki_hygiene(b, make_run(pods={"go": [("demo-go-api-1-x", old)]}))
        self.assertEqual(sorted(loki), [(old, old + verify.STARTUP_SCAN), (WSTART, WEND)])
        # A later container start of the pod adds its own interval.
        spans.clear()
        verify.check_g(b, make_run(pods={"go": [("demo-go-api-1-x", old)]}, latest_starts=[old + 3600]),
                       "go", cache(b), {})
        self.assertEqual(sorted(set(spans) - {(old, WEND)}),
                         [(old, old + verify.STARTUP_SCAN), (old + 3600, old + 3600 + verify.STARTUP_SCAN),
                          (WSTART, WEND)])

    def test_g_startup_roots_are_scanned(self) -> None:
        # The old connection-time guarantee: a root found only in a pod's first minutes
        # is fetched and scanned.
        old = WSTART - 14 * 3600
        b = FakeBackends()
        data = fixture("go", "ok")
        data["batches"][0]["scopeSpans"][0]["spans"][0]["attributes"].append(
            {"key": "db.connection_string", "value": {"stringValue": "password=x"}})
        tid = b.add_trace(data)
        b.search.append((("resource.otel_libs.run",),
                         lambda s, e, limit: [{"traceID": tid}] if (s, e) == (old, old + verify.STARTUP_SCAN) else []))
        c = verify.check_g(b, make_run(pods={"go": [("demo-go-api-1-x", old)]}), "go", cache(b), {})
        self.assertEqual(c["g.secret"].state, "fail")
        self.assertIn("password=", c["g.secret"].detail)

    def test_tempo_page_splits_and_caps(self) -> None:
        b = FakeBackends()

        def dense(start, end, limit):
            n = min(limit, (end - start) // 12)
            return [{"traceID": f"{start + i:032x}"} for i in range(n)]
        b.search.append(((), dense))
        traces, capped = verify.tempo_page(b, "{}", 0, 400, limit=10, min_slice=100)
        self.assertFalse(capped)
        self.assertEqual(len(traces), 32)  # 4 slices of 100 s, 8 traces each
        _, capped = verify.tempo_page(b, "{}", 0, 400, limit=10, min_slice=200)
        self.assertTrue(capped)

    def test_loki_page(self) -> None:
        b = FakeBackends()
        b.loki_lines_fn = lambda q, s, e, limit: [({}, "l")] * min(limit, max(0, (e - s) // 12))
        lines, capped = verify.loki_page(b, "{}", 0, 400, limit=10, min_slice=100)
        self.assertFalse(capped)
        self.assertEqual(len(lines), 32)
        _, capped = verify.loki_page(b, "{}", 0, 400, limit=10, min_slice=200)
        self.assertTrue(capped)

    def run_g(self, lang: str, scen: str, loki: dict | None = None, polls: int = 0) -> dict:
        b = FakeBackends()
        tid = b.add_trace(fixture(lang, scen))
        listed = [{"traceID": tid, "rootTraceName": "demo.drive"}]
        listed += [{"traceID": f"{i + 1:032x}", "rootTraceName": "XREADGROUP"} for i in range(polls)]
        b.search.append((("connect",), []))
        b.search.append((("statusMessage",), [{"traceID": tid}]))
        b.search.append((("event.exception.message",), []))
        b.search.append((("span.db.query.text",), []))
        b.search.append(((), listed))
        run = make_run(lang, pods={lang: [(f"demo-{lang}-api-1", WSTART - 600)]})
        self.b = b
        return verify.check_g(b, run, lang, cache(b), loki or {})

    def test_go_db_error_sentinel_db(self) -> None:
        c = self.run_g("go", "db_error")
        self.assertEqual(c["g.secret"].state, "pass", c["g.secret"].detail)
        self.assertEqual(c["g.sentinel.db"].state, "fail")
        db = next(s for s in spans_of("go", "db_error") if s.is_postgres() and s.status == "error")
        self.assertIn(f"run token in client span {db.span_id} event exception.exception.message",
                      c["g.sentinel.db"].detail)
        self.assertNotIn("sql.conn.exec", c["g.sentinel.db"].detail)  # never the span name
        self.assertNotIn(f"dup-{RUNS['go']}", c["g.sentinel.db"].detail)  # labels only, never values
        self.assertEqual(c["g.sentinel.app"].state, "pass", c["g.sentinel.app"].detail)

    def test_node_unhandled_sentinel_app(self) -> None:
        c = self.run_g("node", "unhandled")
        self.assertEqual(c["g.sentinel.app"].state, "fail")
        self.assertEqual(len(c["g.sentinel.app"].problems), 3)  # one problem per hit
        self.assertNotIn("request handler", c["g.sentinel.app"].detail)
        self.assertEqual(c["g.sentinel.db"].state, "nodata")  # no DB span in that trace

    def test_poll_roots_sampled(self) -> None:
        self.run_g("dotnet", "ok", polls=120)
        fetched = [c for c in self.b.calls if c[0] == "trace"]
        self.assertEqual(len(fetched), 1 + 50)  # the drive trace + 50 of 120 poll roots

    def test_loki_hits_and_caps(self) -> None:
        c = self.run_g("dotnet", "ok", loki={"dotnet": ["hex48 in Loki field x of pod p"],
                                             "*": ["hex48 in a Loki line of pod demo-redis-0"]})
        self.assertEqual(c["g.secret"].state, "fail")
        self.assertIn("Loki hits 2", c["g.secret"].detail)
        secret = spans_of("dotnet", "ok")
        b = FakeBackends()
        data = fixture("dotnet", "ok")
        data["batches"][0]["scopeSpans"][0]["spans"][0]["attributes"].append(
            {"key": "db.connection_string", "value": {"stringValue": f"Password={self.SECRET}"}})
        tid = b.add_trace(data)
        b.search.append(((), [{"traceID": tid}]))
        svc = secret[0].service
        lang = svc.split("-")[1]
        c = verify.check_g(b, make_run(lang, pods={lang: [("p", WSTART)]}), lang, cache(b), {})
        self.assertEqual(c["g.secret"].state, "fail")
        self.assertIn("hex48", c["g.secret"].detail)
        self.assertIn("password=", c["g.secret"].detail)
        self.assertNotIn(self.SECRET, c["g.secret"].detail)

    def test_capped_unresolved_nodata_errors(self) -> None:
        b = FakeBackends()
        b.search.append(((), lambda s, e, limit: [{"traceID": f"{i:032x}"} for i in range(limit)]))
        c = verify.check_g(b, make_run(pods={"go": [("p", WEND - 30)]}), "go", cache(b), {})
        self.assertIn("still hit", c["g.secret"].detail)
        self.assertIn("not fetchable", c["g.secret"].detail)
        self.assertEqual(verify.check_g(b, make_run(), "go", cache(b), {})["g.secret"].state, "nodata")
        self.assertEqual(verify.check_g(b, make_run(pods_error="x"), "go", cache(b), {})["g.secret"].state, "error")
        b.fail = "search"
        self.assertEqual(verify.check_g(b, make_run(pods={"go": [("p", WSTART)]}), "go", cache(b), {})
                         ["g.sentinel.db"].state, "error")

    def test_loki_hygiene(self) -> None:
        b = FakeBackends()
        seen: list = []

        def lines(q, s, e, limit):
            seen.append(q)
            return [({"k8s_pod_name": "demo-go-api-7d9-abc", "exception_message": f"x {self.SECRET}"}, "ok"),
                    ({"k8s_pod_name": "demo-postgres-0"}, f"conn postgres://u:{self.SECRET}@h"),
                    ({"k8s_pod_name": "demo-node-worker-1-x"}, "clean")]
        b.loki_lines_fn = lines
        run = make_run(pods={"go": [("demo-go-api-7d9-abc", WSTART)], "node": [("demo-node-worker-1-x", WSTART)]})
        hits, err = verify.loki_hygiene(b, run)
        self.assertEqual(err, "")
        self.assertEqual(hits["go"], ["hex48 in Loki field exception_message of pod demo-go-api-7d9-abc"])
        self.assertEqual(sorted(hits["*"]), ["hex48 in a Loki line of pod demo-postgres-0",
                                             "userinfo in a Loki line of pod demo-postgres-0"])
        self.assertNotIn("node", hits)
        self.assertIn('k8s_pod_name=~"demo-go-api-7d9-abc|demo-node-worker-1-x|demo-postgres-.*|demo-redis-.*"',
                      seen[0])
        self.assertEqual(verify.loki_hygiene(b, make_run()), ({}, ""))
        b.fail = "loki"
        self.assertEqual(verify.loki_hygiene(b, run)[0], {})
        b.fail = ""
        b.loki_lines_fn = lambda q, s, e, limit: [({"k8s_pod_name": "x"}, "")] * limit
        run.window_end = run.window_start + 30
        self.assertIn("still hit", verify.loki_hygiene(b, run)[0]["*"][0])


def report_text(b: FakeBackends, pod: dict) -> str:
    out = io.StringIO()
    verify.demo_preflight(b, "k8s01", make_run(window_start=990), report(out),
                          kubectl=lambda c, a: {"items": [pod]})
    return out.getvalue()


def pods_json(*items: tuple[str, str]) -> dict:
    return {"items": [{"metadata": {"name": n}, "status": {"startTime": t}} for n, t in items]}


class PreflightAndDriverTest(unittest.TestCase):
    def test_preflight(self) -> None:
        b = FakeBackends()
        b.prom_vec.append(("exemplars_in_storage", [{"metric": {}, "value": [NOW, "100000"]}]))
        b.prom_vec.append(("target_info", [
            {"metric": {"service_name": "demo-go-api", "job": "dev-otel-libs/demo-go-api", "instance": "i1"}},
            {"metric": {"service_name": "other", "job": "x", "instance": "i2"}}]))
        b.runtime = {"startTime": "2026-11-16T14:14:31.4Z"}
        out = io.StringIO()
        rep = report(out)
        run = make_run(window_start=1000)
        calls = []

        def kubectl(ctx, args):
            calls.append((ctx, args))
            return pods_json(("demo-go-api-5f-abc", "1970-01-01T00:00:10Z"), ("unrelated", ""))
        verify.demo_preflight(b, "k8s01", run, rep, kubectl=kubectl)
        self.assertTrue(run.exemplar_storage)
        self.assertEqual(run.instances, {("go", "api"): {("dev-otel-libs/demo-go-api", "i1")}})
        self.assertEqual(run.pods["go"][0][0], "demo-go-api-5f-abc")
        self.assertEqual(calls, [("k8s01", ["pods", "-n", "dev-otel-libs", "-l", f"otel-libs/run={RUNS['go']}"])])
        text = out.getvalue()
        self.assertIn("inside the window", text)
        self.assertIn("(full: old exemplars are evicted)", text)
        self.assertFalse(rep.failed())

    def test_startup_race_blocks(self) -> None:
        # DE-D11 r3.6: the window must start >= 60 s after the youngest app pod start;
        # a container restart (running.startedAt) counts as a start.
        pod = {"metadata": {"name": "demo-go-api-5f-abc"},
               "status": {"startTime": "1970-01-01T00:00:10Z", "containerStatuses": [
                   {"state": {"running": {"startedAt": "1970-01-01T00:16:00Z"}}}]}}
        self.assertEqual(verify.pod_start(pod), 960)
        self.assertIsNone(verify.pod_start({"status": {}}))
        for start, blocked in ((1000, True), (1020, False)):
            out = io.StringIO()
            rep = report(out)
            verify.demo_preflight(FakeBackends(), "k8s01", make_run(window_start=start), rep,
                                  kubectl=lambda c, a: {"items": [pod]})
            self.assertEqual(rep.failed(), blocked, out.getvalue())
        out = io.StringIO()
        rep = report(out)
        verify.demo_preflight(FakeBackends(), "k8s01", make_run(window_start=0), rep,
                              kubectl=lambda c, a: pods_json(("demo-go-api-5f-abc", "")))
        self.assertFalse(rep.failed())  # unknown start time: no startup check
        self.assertIn("startup race", report_text(FakeBackends(), pod))

    def test_preflight_blocked_and_errors(self) -> None:
        b = FakeBackends()
        b.flags = {"enable-feature": "native-histograms"}
        rep = report()
        run = make_run()
        verify.demo_preflight(b, "k8s01", run, rep, kubectl=lambda c, a: {"items": []})
        self.assertFalse(run.exemplar_storage)
        self.assertTrue(rep.failed())
        b.fail = "prom"  # target_info + gauge
        b.flags = {"enable-feature": "exemplar-storage"}
        run = make_run()

        def broken(ctx, args):
            raise verify.QueryError("no access")
        out = io.StringIO()
        verify.demo_preflight(b, "k8s01", run, report(out), kubectl=broken)
        self.assertEqual((run.pods_error, bool(run.target_error)), ("no access", True))
        b.fail = "status"
        run = make_run()
        rep = report()
        verify.demo_preflight(b, "k8s01", run, rep, kubectl=lambda c, a: {"items": []})
        self.assertTrue(run.flags_error)
        self.assertTrue(rep.failed())

    def test_timestamps(self) -> None:
        self.assertEqual(verify._ts("1970-01-01T00:00:10Z"), 10)
        self.assertEqual(verify._ts("1970-01-01T00:00:10.5Z"), 10)
        self.assertEqual(verify._ts("1970-01-01T00:00:10.123456789Z"), 10)
        self.assertIsNone(verify._ts(""))
        self.assertIsNone(verify._ts("yesterday"))

    def test_verify_demo_end_to_end_on_empty_backends(self) -> None:
        b = FakeBackends()
        out = io.StringIO()
        rep = report(out)
        run = make_run(langs=["go", "node"])
        run.langs = ["go", "node"]
        verify.verify_demo(b, "k8s01", run, rep, cache=cache(b), kubectl=lambda c, a: {"items": []})
        rep.matrix(run.langs)
        self.assertEqual(len(rep.cells), 2 * len(verify.DEMO_CELLS))
        # requires_series: a Node.js b cell without its metric series is FAIL, not NA.
        self.assertEqual(rep.cells[("node", "b.http_server")], "FAIL")
        self.assertEqual(rep.cells[("go", "a")], "FAIL")              # minimums: empty is FAIL
        self.assertEqual(rep.cells[("go", "d.db")], "FAIL")           # xfail without data is FAIL
        self.assertTrue(rep.failed())
        dry = report()
        verify.verify_demo(b, "k8s01", make_run(), dry, kubectl=lambda c, a: {"items": []}, dry_run=True)
        self.assertEqual(dry.cells, {})
        b.fail = "loki"
        rep = report()
        verify.verify_demo(b, "k8s01", make_run(pods={"go": [("demo-go-api-x", WSTART)]}), rep,
                           cache=cache(b), kubectl=lambda c, a: pods_json(("demo-go-api-x", "")))
        self.assertEqual(rep.cells[("go", "g.secret")], "FAIL")

    def test_cli_usage_errors(self) -> None:
        err = io.StringIO()
        orig = sys.stderr
        sys.stderr = err
        try:
            self.assertEqual(verify.main(["demo", "--run", "x", "--settle", "60"]), 2)
            self.assertEqual(verify.main(["demo", "--run", "x", "--window", "0"]), 2)
            self.assertEqual(verify.main(["demo", "--run", "x", "--expected", "/nonexistent.json"]), 2)
        finally:
            sys.stderr = orig
        self.assertIn("at least 120s", err.getvalue())


class ClientTest(unittest.TestCase):
    """The Prometheus query_range / exemplar / status clients, Loki instant query and the
    read-only kubectl get -o json wrapper (shared with soak K1, K-D3)."""

    def proc(self, stdout: str, code: int = 0, stderr: str = "") -> mock.Mock:
        return mock.Mock(returncode=code, stdout=stdout, stderr=stderr)

    def backends(self) -> verify.Backends:
        return verify.Backends("k8s01", "monitoring", "tempo:3200", "loki:3100", "kps-prometheus:9090")

    def test_prometheus_and_loki_clients(self) -> None:
        b = self.backends()
        ok = json.dumps({"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {}}]}})
        with mock.patch.object(verify.subprocess, "run", return_value=self.proc(ok)) as run:
            self.assertEqual(b.prom_query_range("up", 1, 2, 30), [{"metric": {}}])
            argv = run.call_args.args[0]
            self.assertEqual(argv[:5], ["kubectl", "--context", "k8s01", "get", "--raw"])
            self.assertIn("/services/kps-prometheus:9090/proxy/api/v1/query_range?query=up&start=1&end=2&step=30",
                          argv[5])
            self.assertEqual(b.loki_query("sum(x)", 3), [{"metric": {}}])
            self.assertIn("time=3000000000", run.call_args.args[0][5])
        ex = json.dumps({"status": "success", "data": [{"seriesLabels": {}}]})
        with mock.patch.object(verify.subprocess, "run", return_value=self.proc(ex)) as run:
            self.assertEqual(b.prom_exemplars("m_bucket", 1, 2), [{"seriesLabels": {}}])
            self.assertIn("/api/v1/query_exemplars?", run.call_args.args[0][5])
        st = json.dumps({"status": "success", "data": {"enable-feature": "exemplar-storage"}})
        with mock.patch.object(verify.subprocess, "run", return_value=self.proc(st)) as run:
            self.assertEqual(b.prom_status("flags")["enable-feature"], "exemplar-storage")
            self.assertTrue(run.call_args.args[0][5].endswith("/api/v1/status/flags"))
        bad = json.dumps({"status": "error", "error": "parse"})
        with mock.patch.object(verify.subprocess, "run", return_value=self.proc(bad)):
            with self.assertRaisesRegex(verify.QueryError, "parse"):
                b.prom_query_range("(", 1, 2, 30)

    def test_transport_errors_are_retried(self) -> None:
        b = self.backends()
        ok = json.dumps({"status": "success", "data": {"result": []}})
        reset = self.proc("", 1, "Unable to connect to the server: read tcp 10.0.0.1:1->10.0.0.2:6443: "
                                 "read: connection reset by peer")
        tls = self.proc("", 1, "Unable to connect to the server: net/http: TLS handshake timeout")
        sleeps: list = []
        with mock.patch.object(verify.subprocess, "run", side_effect=[reset, tls, self.proc(ok)]) as run, \
                mock.patch.object(verify, "_sleep", sleeps.append):
            self.assertEqual(b.prom_query("up", 1), [])
            self.assertEqual(run.call_count, 3)
        self.assertEqual(sleeps, list(verify.TRANSPORT_BACKOFF))
        # Bounded: three transport failures in a row are a QueryError, not a fourth try.
        dial = self.proc("", 1, "Unable to connect to the server: dial tcp 10.0.0.2:6443: i/o timeout")
        sleeps.clear()
        with mock.patch.object(verify.subprocess, "run", side_effect=[dial, dial, dial, self.proc(ok)]) as run, \
                mock.patch.object(verify, "_sleep", sleeps.append):
            with self.assertRaisesRegex(verify.QueryError, "i/o timeout"):
                b.tempo_trace("ab")
            self.assertEqual(run.call_count, 3)
        self.assertEqual(len(sleeps), 2)
        # A kubectl that hangs past its own timeout is a transport failure too.
        hang = verify.subprocess.TimeoutExpired(["kubectl"], 60)
        with mock.patch.object(verify.subprocess, "run", side_effect=[hang, self.proc(ok)]) as run, \
                mock.patch.object(verify, "_sleep", lambda _s: None):
            self.assertEqual(b.prom_query("up", 1), [])
            self.assertEqual(run.call_count, 2)

    def test_answers_are_not_retried(self) -> None:
        b = self.backends()
        answers = (
            "Error from server (NotFound): the server could not find the requested resource",
            "Error from server (ServiceUnavailable): the server is currently unable to handle the request",
            "Error from server (InternalError): an error on the server (\"query timed out\") has prevented "
            "the request from succeeding",
            "Error from server (Forbidden): services \"tempo:3200\" is forbidden",
            # A 5xx whose body quotes the backend's own dial error is still an answer.
            "Error from server (InternalError): an error on the server (\"dial tcp 10.1.2.3:9095: "
            "i/o timeout\") has prevented the request from succeeding",
        )
        for stderr in answers:
            with mock.patch.object(verify.subprocess, "run", return_value=self.proc("", 1, stderr)) as run, \
                    mock.patch.object(verify, "_sleep", self.fail):
                with self.assertRaises(verify.QueryError):
                    b.prom_query("up", 1)
                self.assertEqual(run.call_count, 1, stderr)
        with mock.patch.object(verify.subprocess, "run", return_value=self.proc(
                "", 1, "Error from server (NotFound): trace not found")) as run, \
                mock.patch.object(verify, "_sleep", self.fail):
            self.assertIsNone(b.tempo_trace("ab"))  # a 404 answer: no retry, the trace is absent
            self.assertEqual(run.call_count, 1)
        with mock.patch.object(verify.subprocess, "run", return_value=self.proc("<html>")) as run, \
                mock.patch.object(verify, "_sleep", self.fail):
            with self.assertRaisesRegex(verify.QueryError, "not JSON"):
                b.prom_query("up", 1)
            self.assertEqual(run.call_count, 1)

    def test_kubectl_json(self) -> None:
        with mock.patch.object(verify.subprocess, "run", return_value=self.proc('{"items": []}')) as run:
            self.assertEqual(verify.kubectl_json("k8s01", ["pods", "-n", "ns"]), {"items": []})
            self.assertEqual(run.call_args.args[0],
                             ["kubectl", "--context", "k8s01", "get", "pods", "-n", "ns", "-o", "json"])
        with mock.patch.object(verify.subprocess, "run", return_value=self.proc("", 1, "forbidden")):
            with self.assertRaisesRegex(verify.QueryError, "forbidden"):
                verify.kubectl_json("k8s01", ["pods"])
        with mock.patch.object(verify.subprocess, "run", return_value=self.proc("nope")):
            with self.assertRaisesRegex(verify.QueryError, "not JSON"):
                verify.kubectl_json("k8s01", ["pods"])

    def test_descends_stops_on_gaps_and_cycles(self) -> None:
        def sp(sid: str, parent: str) -> verify.Span:
            return verify.Span("t", sid, parent, "n", "internal", "s", "", "unset", "", {}, [], [])
        a, b_, c = sp("a", ""), sp("b", "a"), sp("c", "x")
        by = {s.span_id: s for s in (a, b_, c)}
        self.assertTrue(verify.descends(b_, a, by))
        self.assertFalse(verify.descends(c, a, by))  # parent not in the trace
        loop1, loop2 = sp("l1", "l2"), sp("l2", "l1")
        self.assertFalse(verify.descends(loop1, a, {"l1": loop1, "l2": loop2}))

    def test_main_demo_dry_run_and_full(self) -> None:
        fake = FakeBackends()
        out = io.StringIO()
        with mock.patch.object(verify, "Backends", return_value=fake), \
                mock.patch.object(verify, "kubectl_json", return_value={"items": []}), \
                mock.patch.object(verify.time, "time", return_value=NOW), \
                mock.patch.object(sys, "stdout", out):
            self.assertEqual(verify.main(["--langs", "go", "demo", "--run", "r1", "--dry-run"]), 0)
            fake.flags = {}
            self.assertEqual(verify.main(["--langs", "go", "demo", "--run", "r1", "--dry-run"]), 1)
            fake.flags = {"enable-feature": "exemplar-storage"}
            with mock.patch.object(verify.TraceCache, "__init__",
                                   lambda self, b, sleep=None, retry_delay=0: (
                                       setattr(self, "b", b), setattr(self, "sleep", no_sleep),
                                       setattr(self, "retry_delay", 0), setattr(self, "_traces", {}))[0]):
                self.assertEqual(verify.main(["--langs", "go", "demo", "--run", "r1"]), 1)
        text = out.getvalue()
        self.assertIn("verify demo --dry-run: OK", text)
        self.assertIn("verify demo --dry-run: BLOCKED", text)
        self.assertIn(f"window [{WSTART}, {WEND}] (900s, settle 120s)", text)
        self.assertIn("cell", text)


class ReviewFixesTest(unittest.TestCase):
    """DE5 code review M1-M8 and LOWs (DE-D11 r3.7)."""

    def test_m1_exemplar_without_span_id_fails(self) -> None:
        b = FakeBackends()
        tid = b.add_trace(fixture("go", "ok"))
        run = make_run(instances=instances("go"), exemplar_storage=True)
        b.exemplars = [exemplar_series("inst-go-backend", (tid, ""))]
        obs = verify.check_b(b, run, "go", "b.demo_order", cache(b))
        self.assertEqual(obs.state, "fail")
        self.assertIn("1 exemplars without span_id", obs.problems)
        b.exemplars = [exemplar_series("inst-go-backend", ("zz*", "1" * 16))]
        self.assertIn("1 exemplars with an invalid trace_id",
                      verify.check_b(b, run, "go", "b.demo_order", cache(b)).problems)

    # Cells whose fixtures (recorded 2026-11-18, before A36) still show the old DB exception
    # detail; A36 closed on 2026-11-19, so these judge FAIL: a regression is caught.
    PRE_A36 = {("go", "d.db"), ("node", "d.db"), ("go", "g.sentinel.db")}

    def test_m2_every_fixture_xfail_is_explained_by_its_reasons(self) -> None:
        expected = verify.load_expected()
        for lang in ("go", "python", "node", "dotnet"):
            b = CheckDTest().setup_lang(lang)
            rep = report()
            for cell, obs in verify.check_d(b, make_run(lang), lang, cache(b), rep).items():
                want = expected[lang][cell]["expect"]
                verdict = "XFAIL" if want.startswith("xfail:") else "PASS"
                if (lang, cell) in self.PRE_A36:
                    verdict = "FAIL"  # pre-A36 recording against the closed A36 expectation
                self.assertEqual(rep.cell(lang, cell, obs), verdict, (lang, cell, obs.problems))
            for scen in verify.ERROR_SCENARIOS:
                for cell, obs in HygieneTest().run_g(lang, scen).items():
                    if obs.state == "fail":
                        verdict = "FAIL" if (lang, cell) in self.PRE_A36 else "XFAIL"
                        self.assertEqual(rep.cell(lang, cell, obs), verdict, (lang, scen, cell, obs.problems))

    def test_m2_unexplained_problem_fails_the_xfail_cell(self) -> None:
        out = io.StringIO()
        rep = report(out)
        explained = ("grpc_error: server span carries ['event exception.exception.message', "
                     "'event exception.exception.stacktrace']")
        obs = verify.Obs("fail", "d", problems=[explained, "grpc_error: 'q' has a status description "
                                                           "(contract A25: empty)"])
        self.assertEqual(rep.cell("python", "d.grpc", verify.Obs("fail", "d", problems=[explained])), "XFAIL")
        self.assertEqual(rep.cell("python", "d.grpc", obs), "FAIL")
        self.assertIn("problems outside the xfail reasons", out.getvalue())
        self.assertEqual(rep.cell("python", "d.grpc", verify.Obs("fail", "only the detail")), "FAIL")
        self.assertEqual(verify.unmatched(verify.Obs("fail", "abc"), ["b"]), [])

    def test_m2_loader_rules(self) -> None:
        good = json.loads(verify.EXPECTED_JSON.read_text())
        bad = []
        x = copy.deepcopy(good)
        x["expected"]["go"]["d.db"] = {"expect": "xfail:A36"}
        bad.append((x, "needs 'reason'"))
        x = copy.deepcopy(good)
        x["expected"]["go"]["a"] = {"expect": "pass", "reason": ["x"]}
        bad.append((x, "only for xfail"))
        x = copy.deepcopy(good)
        x["expected"]["go"]["a"] = {"expect": "pass", "requires_series": True}
        bad.append((x, "only for n/a"))
        with tempfile.TemporaryDirectory() as tmp:
            for data, msg in bad:
                path = Path(tmp) / "e.json"
                path.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, msg):
                    verify.load_expected(path)

    def test_m3_loki_page_has_no_gap_at_mid(self) -> None:
        b = FakeBackends()
        entries = [(t * 10**9, f"l{t}") for t in range(0, 400, 10)] + [(int(200.5 * 10**9), "gap")]

        def fn(q, start, end, limit):
            hit = [(str(ts), {}, line) for ts, line in entries if start * 10**9 <= ts <= end * 10**9]
            return hit[:limit]
        b.loki_entries_fn = fn
        lines, capped = verify.loki_page(b, "{}", 0, 400, limit=25, min_slice=100)
        self.assertFalse(capped)
        got = [line for _, line in lines]
        self.assertIn("gap", got)              # mid + 0.5 s: in the old (mid, mid + 1) gap
        self.assertEqual(len(got), len(set(got)))  # the line at mid is de-duplicated
        self.assertEqual(len(got), 41)

    def test_m4_hits_never_print_names_or_values(self) -> None:
        secret = "AB" * 24  # 48 upper-case hex: matched too (LOW)
        data = fixture("go", "ok")
        span = data["batches"][0]["scopeSpans"][0]["spans"][0]
        span["name"] = f"op {secret}"
        b = FakeBackends()
        tid = b.add_trace(data)
        b.search.append(((), [{"traceID": tid}]))
        c = verify.check_g(b, make_run(pods={"go": [("p", WSTART)]}), "go", cache(b), {})
        self.assertEqual(c["g.secret"].state, "fail")
        self.assertRegex(c["g.secret"].detail, rf"hex48 in \w+ span {verify.norm_span(span['spanId'])} name")
        self.assertNotIn(secret, c["g.secret"].detail)
        self.assertNotIn("op ", c["g.secret"].detail)
        # d token hits: kind, span id and labels only.
        c = CheckDTest().cells("node")
        self.assertNotIn("request handler - /orders' in", c["d.http"].detail)
        self.assertIn("run token on internal span", c["d.http"].detail)

    def test_low_resource_attributes_are_scanned(self) -> None:
        data = fixture("go", "ok")
        data["batches"][0]["resource"]["attributes"].append(
            {"key": "process.command_args", "value": {"stringValue": "--password=x"}})
        b = FakeBackends()
        tid = b.add_trace(data)
        b.search.append(((), [{"traceID": tid}]))
        c = verify.check_g(b, make_run(pods={"go": [("p", WSTART)]}), "go", cache(b), {})
        self.assertIn("resource process.command_args", c["g.secret"].detail)
        self.assertNotIn("--password=x", c["g.secret"].detail)

    def test_m5_target_info_errors_and_requires_series(self) -> None:
        b = FakeBackends()
        run = make_run(exemplar_storage=True, target_error="boom", instances=instances("go"))
        self.assertEqual(verify.check_b(b, run, "go", "b.http_server", cache(b)).state, "error")
        self.assertEqual(verify.check_c(b, run, "go").state, "error")
        run = make_run("node", exemplar_storage=True, instances=instances("node"))
        b.series = [{"__name__": "http_server_request_duration_seconds_bucket"}]
        obs = verify.check_b(b, run, "node", "b.http_server", cache(b))
        self.assertEqual((obs.state, obs.series), ("nodata", True))
        self.assertEqual(report().cell("node", "b.http_server", obs), "NA")
        b.series = []
        obs = verify.check_b(b, run, "node", "b.http_server", cache(b))
        self.assertEqual(report().cell("node", "b.http_server", obs), "FAIL")
        # Python b.rpc_server: n/a because the metric is not emitted, so no series is fine.
        run = make_run("python", exemplar_storage=True, instances=instances("python"))
        obs = verify.check_b(b, run, "python", "b.rpc_server", cache(b))
        self.assertEqual(report().cell("python", "b.rpc_server", obs), "NA")
        b.fail = "series"
        self.assertEqual(verify.check_b(b, run, "python", "b.rpc_server", cache(b)).state, "error")

    def test_m7_grace_uses_latest_start_g_uses_earliest(self) -> None:
        pod = {"metadata": {"name": "demo-go-api-5f-abc"},
               "status": {"startTime": "1970-01-01T00:00:10Z", "containerStatuses": [
                   {"state": {"running": {"startedAt": "1970-01-01T00:16:00Z"}}}]}}
        run = make_run(window_start=990)
        rep = report()
        verify.demo_preflight(FakeBackends(), "k8s01", run, rep, kubectl=lambda c, a: {"items": [pod]})
        self.assertTrue(rep.failed())               # 990 < 960 + 60
        self.assertEqual(run.pods["go"], [("demo-go-api-5f-abc", 10)])  # g scan starts at startTime
        self.assertEqual(run.latest_starts, [960])

    def test_m8_paging_crosscheck(self) -> None:
        b = FakeBackends()
        a = (WEND - 60) // 60 * 60
        listed = [{"traceID": f"{i:032x}", "startTimeUnixNano": str((a + i) * 10**9)} for i in range(1, 4)]
        b.metrics = [{"samples": [{"timestampMs": str(a * 1000), "value": 3},
                                  {"timestampMs": str((a + 60) * 1000), "value": 3}]}]
        detail, problem = verify.paging_crosscheck(b, "x", listed, WSTART, WEND)
        self.assertEqual(problem, "")
        self.assertIn("listed 3, count_over_time 3", detail)
        b.metrics = [{"samples": [{"timestampMs": str(a * 1000), "value": 5}]}]
        _, problem = verify.paging_crosscheck(b, "x", listed, WSTART, WEND)
        self.assertIn("lists 3 root traces", problem)
        b.metrics = None
        detail, problem = verify.paging_crosscheck(b, "x", listed, WSTART, WEND)
        self.assertEqual(problem, "")
        self.assertIn("unmeasured", detail)
        self.assertIn("skipped", verify.paging_crosscheck(b, "x", listed, WEND - 10, WEND)[0])
        # In check_g the mismatch is a problem of every g cell.
        b = FakeBackends()
        tid = b.add_trace(fixture("go", "ok"))
        b.search.append(((), [{"traceID": tid, "startTimeUnixNano": str(a * 10**9)}]))
        b.metrics = [{"samples": [{"timestampMs": str(a * 1000), "value": 9}]}]
        c = verify.check_g(b, make_run(pods={"go": [("p", WSTART)]}), "go", cache(b), {})
        self.assertTrue(all("count_over_time counts 9" in o.detail for o in c.values()))

    def test_low_edges_report_ratio_and_scope(self) -> None:
        b = CheckETest().edges(("demo-go-api", "demo-go-worker", "messaging_system", "false", 120))
        res = verify.check_e_edges(b, make_run(), "go", report())
        self.assertIn("messaging edge / linked = 1.20", res["e.edges"].detail)
        self.assertIn("edges scoped by service name, not by run", res["e.edges"].detail)


if __name__ == "__main__":
    unittest.main()
