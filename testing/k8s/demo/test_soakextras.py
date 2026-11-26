"""Tests for ``verify.py soak extras`` (specs/soak-simulation K8b, design K-D21
draft; soakextras.py).

Standard library unittest; no cluster access. The canary is a fake 48-hex token;
the label sets follow those read on k8s01 on 2026-11-26 (values made up). The
SR10 regression cases run on the real gaps tables and the real
``soak/sr10-gaps.json``.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "soak"))
import soakcount as sc  # noqa: E402
import soakextras as sx  # noqa: E402
import soakplatform as sp  # noqa: E402
import verify  # noqa: E402

TOKEN = "0123456789abcdef" * 3          # fake canary, 48 hex
T1, T2 = 1_795_000_000.0, 1_795_007_200.0
NOW = T2 + 3600
CR = {"spec": {"replicas": 1, "config": {
    "exporters": {"otlp_grpc/tempo": {}},
    "processors": {"batch": {"timeout": "5s"}},
    "connectors": {"service_graph": {"store": {"ttl": "10s"}}}}}}


def cell(lang, scen, cls):
    return f"lang:{lang},route:orders,scen:{scen},class:{cls}"


def k6_log(per: dict, records: list[dict]) -> str:
    counts = {f"soak_requests{{lang:{lang},route:orders,scen:{scen},class:{cls},outcome:expected}}": n
              for (lang, scen, cls), n in per.items()}
    summary = {"metrics": {k: {"values": {"count": v}} for k, v in counts.items()}}
    res = "".join(f"SOAKRES {json.dumps(r)}\n" for r in records)
    return "noise\n" + res + "SOAK_SUMMARY_BEGIN\n" + json.dumps(summary) + "\nSOAK_SUMMARY_END\n"


def span(sid, parent, service, name, kind=2, attrs=None, res=None, start=1):
    return {"traceId": "t", "spanId": sid, "parentSpanId": parent, "name": name, "kind": kind,
            "startTimeUnixNano": start,
            "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in (attrs or {}).items()],
            "_res": {"service.name": service, **(res or {})}}


def trace(*spans) -> dict:
    batches = []
    for s in spans:
        res = s.pop("_res")
        batches.append({"resource": {"attributes": [{"key": k, "value": {"stringValue": v}} for k, v in res.items()]},
                        "scopeSpans": [{"scope": {"name": "x"}, "spans": [s]}]})
    return {"batches": batches}


def ok_trace(lang, parent="", status="201", extra_attrs=None, gw=None):
    res = {"gateway.id": gw} if gw else None
    return trace(span("aaaa", parent, f"demo-{lang}-api", "POST /orders",
                      attrs={"http.response.status_code": status, **(extra_attrs or {})}, res=res),
                 span("bbbb", "aaaa", f"demo-{lang}-backend", "demo.v1.OrderService/PlaceOrder", res=res))


def sgser(client, server, failed, values, ctype=None):
    m = {"cluster": "k8s01", "client": client, "server": server, "failed": failed}
    if ctype:
        m["connection_type"] = ctype
    return {"metric": m, "values": [[t, str(v)] for t, v in values]}


def kcal(langs=("python",), unqualified=("go",)) -> dict:
    out = {"services": {}, "families": {}}
    for lang in langs:
        svc = f"demo-{lang}-backend"
        k = {cell(lang, s, c): (1 if s != "unhandled" and c != "s00" else 0)
             for s in ("ok", "business_error", "db_error", "unhandled") for c in ("none", "s01", "s00")}
        out["services"][svc] = {"qualified": True, "k": k}
        out["families"][svc] = {sx.BACKEND_FAMILY: k}
    for lang in unqualified:
        out["services"][f"demo-{lang}-backend"] = {"qualified": False, "cause": "x"}
    return out


class FakeSources:
    context = "k8s01"

    def __init__(self) -> None:
        self.prom_ans: list[tuple[str, list]] = [MEM_OK]
        self.series_ans: list[dict] = []
        self.md: dict = {}
        self.loki: dict[str, list] = {}          # selector substring -> entries
        self.loki_q: list[tuple[str, list]] = []
        self.traces: dict[str, dict] = {}
        self.search: list[dict] = []
        self.search_err = False
        self.metrics: list[dict] = []
        self.metrics_err = False
        self.pods: dict[str, list] = {}
        self.cr: dict | None = CR
        self.logtext: dict[str, str] = {}
        self.queries: list[str] = []

    def prom(self, q, at):
        self.queries.append(q)
        for pat, ans in self.prom_ans:
            if pat in q:
                return ans
        return []

    def series(self, match, start, end):
        self.queries.append(match)
        return self.series_ans

    def metadata(self):
        return self.md

    def loki_entries(self, q, start, end, limit):
        self.queries.append(q)
        for pat, ents in self.loki.items():
            if pat in q:
                return [e for e in ents if start <= int(e[0]) <= end][:limit]
        return []

    def loki_query(self, q, at):
        self.queries.append(q)
        for pat, ans in self.loki_q:
            if pat in q:
                return ans
        return []

    def tempo_trace(self, tid):
        self.queries.append(tid)
        v = self.traces.get(tid)
        if v == "error":
            raise verify.QueryError("tempo down")
        return json.loads(json.dumps(v)) if v is not None else None

    def tempo_search(self, q, start, end, limit):
        self.queries.append(q)
        if self.search_err:
            raise verify.QueryError("search down")
        return self.search[:limit]

    def tempo_metrics(self, q, start, end, step):
        self.queries.append(q)
        if self.metrics_err:
            raise verify.QueryError("metrics down")
        return self.metrics

    def kget(self, args):
        self.queries.append(" ".join(args))
        if args[0] == "opentelemetrycollector":
            if self.cr is None:
                raise verify.QueryError("no CR")
            return self.cr
        ns = args[2]
        v = self.pods.get(ns, [])
        if v == "error":
            raise verify.QueryError("forbidden")
        return {"items": v}

    def logs(self, ns, pod, container, since):
        self.queries.append(f"logs {ns} {pod} {container}")
        v = self.logtext.get(pod, "")
        if v == "error":
            raise verify.QueryError("logs gone")
        return v


def pod(name, started, restarts=0, container="otc-container"):
    return {"metadata": {"name": name}, "status": {"containerStatuses": [
        {"name": container, "restartCount": restarts,
         "state": {"running": {"startedAt": sp.iso(started)}} if started else {"waiting": {}}}]}}


MEM_OK = ("container_memory_working_set_bytes",
          [{"metric": {"cluster": "k8s01", "pod": "tempo-0"}, "value": [T2, str(2**30)]}])


def G(src, **kw):
    """A Tempo guard that never sleeps for real."""
    kw.setdefault("sleep", lambda s: None)
    return sx.TempoGuard(src, kw.pop("rate", 1e9), kw.pop("mem_stop", 1.5 * 2**30), **kw)


CLEAN = {"clean": True, "reasons": []}
DIRTY = {"clean": False, "reasons": ["agent: NOISY 2"]}


# ── inputs and config ──


class Inputs(unittest.TestCase):
    def ns(self, **kw):
        base = dict(t1=None, t2=None, k6_log=None, k6_end=None, kcal=Path("k.json"), count_window=None, run="r1")
        base.update(kw)
        return argparse.Namespace(**base)

    def test_flags(self):
        got = sx.resolve_inputs(self.ns(t1=1.0, t2=2.0, k6_log=Path("a.log"), k6_end=1.5))
        self.assertEqual((got["t1"], got["t2"], got["k6_log"], got["k6_end"], got["missing"]),
                         (1.0, 2.0, Path("a.log"), 1.5, []))

    def test_count_window(self):
        with tempfile.TemporaryDirectory() as d:
            runs = Path(d)
            (runs / "r1").mkdir()
            cw = runs / "r1" / "count-window.json"
            cw.write_text(json.dumps({"t1": 5, "t2": 9, "k6_end": 7, "valid": True,
                                      "k6_log": "raw/soak-k6-loaded.log"}))
            got = sx.resolve_inputs(self.ns(count_window=cw, t2=8.0), runs)
            self.assertEqual((got["t1"], got["t2"], got["k6_end"]), (5.0, 8.0, 7.0))
            self.assertEqual(got["k6_log"], runs / "r1" / "raw/soak-k6-loaded.log")
            cw.write_text(json.dumps({"t1": 5}))
            self.assertEqual(sx.resolve_inputs(self.ns(count_window=cw, kcal=None), runs)["missing"],
                             ["--t2", "--k6-log", "--kcal"])
            cw.write_text(json.dumps({"t1": 5, "t2": 9, "valid": False, "reasons": ["k6 not done"]}))
            with self.assertRaisesRegex(sx.InputError, "k6 not done"):
                sx.resolve_inputs(self.ns(count_window=cw), runs)

    def test_read_canary(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(sx.read_canary("r", Path(d)))
            (Path(d) / "r").mkdir()
            (Path(d) / "r" / "canary").write_text("\n")
            self.assertIsNone(sx.read_canary("r", Path(d)))
            (Path(d) / "r" / "canary").write_text(TOKEN + "\n")
            self.assertEqual(sx.read_canary("r", Path(d)), TOKEN)

    def test_gateway_id_needs_a_real_set_action(self):
        sets, why = sx.gateway_sets_id(CR)
        self.assertFalse(sets)
        self.assertIn("AG2", why)
        self.assertIn("replicas 1", why)
        self.assertFalse(sx.gateway_sets_id({})[0])
        mention = {"spec": {"config": {"processors": {"x": {"comment": "gateway.id is not set here"}}}}}
        self.assertFalse(sx.gateway_sets_id(mention)[0])
        act = {"spec": {"config": {"processors": {"resource": {"attributes": [
            {"key": "gateway.id", "value": "g1", "action": "upsert"}]}}}}}
        self.assertTrue(sx.gateway_sets_id(act)[0])
        delete = {"spec": {"config": {"processors": {"resource": {"attributes": [
            {"key": "gateway.id", "action": "delete"}]}}}}}
        self.assertFalse(sx.gateway_sets_id(delete)[0])
        ottl = {"spec": {"config": {"processors": {"transform": {"trace_statements": [
            {"statements": ['set(resource.attributes["gateway.id"], "g1")']}]}}}}}
        self.assertTrue(sx.gateway_sets_id(ottl)[0])

    def test_flush_after_and_durations(self):
        after, note = sx.flush_after(CR, 100.0, CR)
        self.assertEqual(after, 130.0)   # BSP 5 s + agent batch 5 s + ttl 10 s + 2 x gateway batch 5 s
        self.assertIn("store.ttl 10s", note)
        self.assertIn("BatchSpanProcessor", note)
        self.assertAlmostEqual(sx.flush_after({}, 100.0)[0],
                         100.0 + sx.SDK_BSP_DELAY_S + 3 * sx.DEFAULT_BATCH_S + sx.SG_DEFAULT_TTL_S)
        self.assertEqual(sx.collector_cr(FakeSources(), "otel-agent"), CR)
        self.assertEqual(sx.duration_s("250ms", 1), 0.25)
        self.assertEqual(sx.duration_s("soon", 7), 7)
        src = FakeSources()
        self.assertEqual(sx.gateway_cr(src), CR)
        src.cr = None
        self.assertEqual(sx.gateway_cr(src), {})
        self.assertEqual(sx.collector_cr(src, "otel-agent"), {})

    def test_shared_gates(self):
        src = FakeSources()
        src.pods["opentelemetry"] = [pod("otel-gateway-collector-abc12-def34", T1 - 100)]
        src.prom_ans = [("otelcol_process_uptime", [{"metric": {"cluster": "k8s01"}, "values": [[T1, "1"], [T2, "2"]]}]),
                        ("otelcol_exporter_queue_size", [{"metric": {"cluster": "k8s01", "exporter": "e"}, "value": [T2, "0"]}]),
                        ("otelcol_exporter_queue_capacity", [{"metric": {"cluster": "k8s01", "exporter": "e"}, "value": [T2, "100"]}])]
        verdicts = [[sp.CounterVerdict("copy-x", "copy", "NOISY", 3), sp.CounterVerdict("agent-x", "shared", "CLEAN")]]
        with mock.patch.object(sp, "read_sentinels", return_value=verdicts), \
                mock.patch.object(sc, "gateway_restart", return_value=(True, "unchanged")):
            self.assertTrue(sx.shared_gates(src, T1, T2)["clean"])   # the copy's NOISY does not gate
        with mock.patch.object(sp, "read_sentinels", return_value=[[sp.CounterVerdict("agent-x", "shared", "NOISY", 3)]]):
            g = sx.shared_gates(src, T1, T2)
        self.assertFalse(g["clean"])
        self.assertTrue(any("gateway restart" in x for x in g["reasons"]))

    def test_redact(self):
        sx._SECRET[:] = [TOKEN]
        try:
            self.assertEqual(sx.redact(f"x {TOKEN.upper()} y" + "z" * 300, 20), "x <canary> y" + "z" * 8)
        finally:
            sx._SECRET.clear()
        self.assertEqual(sx.redact("abc", None), "abc")


# ── check 1 ──


class Graph(unittest.TestCase):
    CELLS = {cell("python", s, c): {"expected": n, "unexpected": 0, "noanswer": 0}
             for (s, n) in (("ok", 700), ("business_error", 100), ("db_error", 100), ("unhandled", 100))
             for c in ("none", "s01", "s00")}

    def series(self, ok_total, failed_total, lang="python", late=0, worker=0):
        """Flushes every 60 s from T1 - 30; the load ends before T2 - 60, the next
        flush (T2 + 30) holds ``late`` more edges."""
        flushes = [T1 - 30 + 60 * i for i in range(int((T2 - T1) // 60) + 2)]
        def vals(base, total, extra):
            return [(t, base + (total if t > T1 + 60 else 0) + (extra if t > T2 else 0)) for t in flushes]
        out = [sgser(f"demo-{lang}-api", f"demo-{lang}-backend", "false", vals(100, ok_total, late)),
               sgser(f"demo-{lang}-api", f"demo-{lang}-backend", "true", vals(5, failed_total, 0)),
               sgser(f"demo-{lang}-api", f"demo-{lang}-worker", "false", vals(0, 999, 0), "messaging_system")]
        if worker:
            out.append(sgser(f"demo-{lang}-worker", f"demo-{lang}-api", "false", vals(0, 0, worker)))
        return out

    def run_graph(self, series, cells=None, k=None, gate=CLEAN, lang="python", after=T2 - 50, cap=T2 + 120):
        reads = sx.graph_reads(series, (lang,), after, cap)
        return sx.graph_verdicts(series, self.CELLS if cells is None else cells, k or kcal(), (lang,), T1, reads,
                                 gate)[lang]

    def test_exact_at_the_first_flush_after_the_load(self):
        v = self.run_graph(self.series(1400, 400, late=4))
        self.assertEqual(v["read_at"], T2 - 30)            # the flush after k6 end + TTL + 2 batch
        self.assertEqual(v["verdict"], "PASS", v)
        self.assertEqual((v["total"]["counted"], v["total"]["lo"], v["total"]["verdict"]), (1800.0, 1800.0, "PASS"))
        self.assertEqual((v["failed"]["counted"], v["failed"]["verdict"]), (400.0, "PASS"))
        self.assertAlmostEqual(v["share"]["expected"], 400 / 1800, places=5)

    def test_worker_traffic_widens_only_the_upper_end(self):
        v = self.run_graph(self.series(1400, 400, late=2, worker=3), after=T2 + 10)
        self.assertEqual(v["read_at"], T2 + 30)
        self.assertEqual((v["total"]["counted"], v["total"]["lo"], v["total"]["hi"]), (1802.0, 1800.0, 1803.0))
        self.assertEqual(v["verdict"], "PASS", v)
        v = self.run_graph(self.series(1400, 400, late=5, worker=3), after=T2 + 10)
        self.assertEqual(v["total"]["verdict"], "DUPLICATES")
        self.assertTrue(v["total"]["report_only"])
        self.assertEqual(v["verdict"], "PASS")   # the counts are report-only, only the share is judged
        v = self.run_graph(self.series(1390, 400, late=3, worker=3), after=T2 + 10)
        self.assertEqual(v["total"]["verdict"], "LOSS")   # the allowance never hides a loss

    def test_no_flush_after_uses_the_last_sample(self):
        v = self.run_graph(self.series(1400, 400), after=T2 + 200, cap=T2 + 100)
        self.assertEqual(v["verdict"], "UNMEASURED")
        self.assertIn("no connector flush", v["read_note"])

    def test_loss_leaves_the_share_unqualified(self):
        v = self.run_graph(self.series(1300, 400))
        self.assertEqual(v["total"]["verdict"], "LOSS")
        self.assertEqual(v["share"]["verdict"], "UNQUALIFIED")
        self.assertEqual(v["verdict"], "UNQUALIFIED")

    def test_share_fail_and_floor(self):
        v = self.run_graph(self.series(1000, 800))
        self.assertEqual(v["verdict"], "FAIL")
        small = {c: {**n, "expected": n["expected"] // 100} for c, n in self.CELLS.items()}
        v = self.run_graph(self.series(14, 4), cells=small)
        self.assertEqual(v["share"]["verdict"], "UNQUALIFIED")
        self.assertIn("1000", v["share"]["reason"])

    def test_gate_keeps_both_reasons(self):
        small = {c: {**n, "expected": n["expected"] // 100} for c, n in self.CELLS.items()}
        v = self.run_graph(self.series(14, 4), cells=small, gate=DIRTY)
        self.assertEqual(v["verdict"], "UNQUALIFIED")
        self.assertTrue(any("share" in r for r in v["reasons"]))
        self.assertTrue(any("shared sentinels" in r for r in v["reasons"]))

    def test_k_modes(self):
        cells = {c.replace("python", "go"): n for c, n in self.CELLS.items()}
        v = self.run_graph(self.series(1400, 400, "go"), cells=cells, lang="go")
        self.assertEqual((v["verdict"], v["measured"]), ("REPORT-ONLY", "PASS"))
        self.assertTrue(v["k_mode"].startswith("nominal"))
        self.assertTrue(any("report-only, wide band" in r for r in v["reasons"]))
        v = self.run_graph(self.series(1400, 400, "node"), lang="node")   # node absent from the file
        self.assertEqual(v["verdict"], "UNQUALIFIED")
        self.assertIn("not in the calibration file", v["k_mode"])
        k = kcal()
        k["families"]["demo-python-backend"] = {}
        self.assertIn("no server:", self.run_graph(self.series(1400, 400), k=k)["k_mode"])
        k = kcal()
        k["services"]["demo-python-backend"].pop("qualified")
        self.assertIn("no qualified flag", self.run_graph(self.series(1400, 400), k=k)["k_mode"])

    def test_missing_cell_k_and_no_traffic(self):
        k = kcal()
        fam = k["families"]["demo-python-backend"][sx.BACKEND_FAMILY]
        fam.pop(cell("python", "ok", "none"))
        v = self.run_graph(self.series(1400, 400), k=k)
        self.assertEqual(v["share"]["verdict"], "UNQUALIFIED")
        self.assertEqual(v["verdict"], "UNQUALIFIED")
        self.assertEqual(sx.judge_share(0, 0, 0.2)["verdict"], "UNQUALIFIED")
        v = self.run_graph([], cells={})
        self.assertEqual(v["verdict"], "UNMEASURED")   # no flush at all

    def test_reset_counted_and_reads(self):
        s = [sgser("demo-python-api", "demo-python-backend", "false", [(T1 - 30, 100), (T1 + 100, 150), (T1 + 200, 20)])]
        got = sx.edge_counts(sx._edge(s, "demo-python-api", "demo-python-backend"), T1, T2)
        self.assertEqual(float(got["total"]), 70.0)
        self.assertTrue(any("reset" in e for e in got["events"]))
        src = FakeSources()
        q, ser = sx.read_graph(src, T1, T2 + 120)
        self.assertIn('cluster="k8s01"', q)
        self.assertIn("(api|worker)", q)
        src.prom_ans = [("otelcol_connector_servicegraph", [
            {"metric": {"cluster": "k8s01", "__name__": "otelcol_connector_servicegraph_expired_edges"},
             "values": [[T1 - 30, "10"], [T2, "61"]]}])]
        q, out = sx.edge_store(src, T1, T2)
        self.assertEqual(out, {"otelcol_connector_servicegraph_expired_edges": 51.0})


# ── check 2 ──


class Canary(unittest.TestCase):
    def test_scan_entries_counts_never_values(self):
        ents = [({"service_name": "demo-go-api"}, "ok line"),
                ({"service_name": "demo-go-api", "x_header": f"x-soak-canary={TOKEN}"}, "hdr"),
                ({"k8s_pod_name": "p"}, f"failed {TOKEN.upper()} password=1")]
        res = sx.scan_entries(ents, TOKEN)
        self.assertEqual(res["lines"], 3)
        self.assertEqual(res["hits"], ["field x_header of demo-go-api", "line of p"])
        self.assertEqual(res["hygiene"]["password="], 1)

    def test_loki_slices(self):
        self.assertEqual(sx.loki_slices(0, 100, 0), [(0, 100)])
        self.assertEqual(len(sx.loki_slices(0, 100, 12000, limit=5000)), 5)

    def loki_src(self):
        src = FakeSources()
        src.loki_q = [('k8s_namespace_name="dev-otel-libs"}[',
                       [{"metric": {"service_name": "demo-go-api"}, "value": [T2, "3"]},
                        {"metric": {"service_name": "short"}, "value": [T2, "5"]},
                        {"metric": {"service_name": "broken"}, "value": [T2, "1"]}]),
                      ('k8s_namespace_name=""', [{"metric": {"service_name": "demo-go-api"}, "value": [T2, "1"]}])]
        same = (str(int(T1) + 1), {"service_name": "demo-go-api"}, "a")
        src.loki = {'service_name="demo-go-api", k8s_namespace_name=""': [
                        (str(int(T1) + 1), {"service_name": "demo-go-api"}, "nolabel")],
                    'service_name="demo-go-api"': [same, (str(int(T1) + 2), {"service_name": "demo-go-api"}, "a"),
                                                   (str(int(T1) + 3), {"service_name": "demo-go-api"}, TOKEN)],
                    'service_name="short"': [(str(int(T1) + 1), {"service_name": "short"}, "x")]}
        orig = src.loki_entries

        def entries(q, a, e, limit):
            if "broken" in q:
                raise verify.QueryError(f"loki 500 {TOKEN}")
            return orig(q, a, e, limit)
        src.loki_entries = entries
        return src

    def test_canary_loki(self):
        sx._SECRET[:] = [TOKEN]
        try:
            rows = sx.canary_loki(self.loki_src(), TOKEN, int(T1), int(T2))
        finally:
            sx._SECRET.clear()
        by = {r["source"]: r for r in rows}
        go = by["loki dev-otel-libs/demo-go-api"]
        self.assertEqual((go["lines"], go["status"]), (3, "scanned"))   # same line twice, two timestamps: kept
        self.assertEqual(go["hits"], ["line of demo-go-api"])
        self.assertEqual(by["loki dev-otel-libs/short"]["status"], "incomplete")
        self.assertEqual(by["loki dev-otel-libs/broken"]["status"], "unmeasured")
        self.assertNotIn(TOKEN, by["loki dev-otel-libs/broken"]["reason"])
        self.assertEqual(by["loki opentelemetry"]["status"], "unmeasured")   # no stream is unmeasured
        self.assertEqual(by["loki (no namespace)/demo-go-api"]["lines"], 1)
        self.assertEqual(sx.canary_verdict(rows), "FAIL")

    def test_canary_loki_errors_and_empty(self):
        src = FakeSources()
        src.loki_query = mock.Mock(side_effect=verify.QueryError("down"))
        rows = sx.canary_loki(src, TOKEN, int(T1), int(T2))
        self.assertTrue(all(r["status"] == "unmeasured" for r in rows))
        src = FakeSources()
        rows = sx.canary_loki(src, TOKEN, int(T1), int(T2))
        self.assertEqual(rows[-1]["reason"], "no demo record without k8s_namespace_name")

    def test_loki_records_capped(self):
        src = FakeSources()
        src.loki = {"x": [(str(int(T1)), {"a": str(i)}, "l") for i in range(10)]}
        with mock.patch.object(sx, "LOKI_LIMIT", 3), mock.patch.object(verify, "MIN_SLICE", 3600 * 10):
            _, capped = sx.loki_records(src, "x", int(T1), int(T1) + 10, 1)
        self.assertTrue(capped)

    def test_collectors(self):
        src = FakeSources()
        src.pods = {"opentelemetry": [pod("otel-agent-collector-ab12c", T1 - 10), pod("otel-gateway-collector-ab-cd", T1 + 10, 1),
                                      pod("otel-logs-collector-zz9", None), pod("unrelated", T1 - 10)],
                    sp.APP_NS: [pod("soak-agent-abc12-x", T1 - 5)]}
        src.logtext = {"otel-agent-collector-ab12c": f"l1\nexport failed {TOKEN}\n", "soak-agent-abc12-x": "error"}
        rows, stable = sx.canary_collectors(src, TOKEN, T1, NOW)
        by = {r["source"]: r for r in rows}
        a = by["container log opentelemetry/otel-agent-collector-ab12c/otc-container"]
        self.assertEqual((a["status"], a["lines"], a["hits"]), ("scanned", 2, ["1 line(s)"]))
        self.assertEqual(by["container log opentelemetry/otel-gateway-collector-ab-cd/otc-container"]["status"], "unmeasured")
        self.assertIn("not running", by["container log opentelemetry/otel-logs-collector-zz9/otc-container"]["reason"])
        self.assertEqual(by[f"container log {sp.APP_NS}/soak-agent-abc12-x/otc-container"]["reason"], "logs gone")
        self.assertFalse(stable["ok"])
        src2 = FakeSources()
        src2.pods = {"opentelemetry": "error"}
        rows, stable = sx.canary_collectors(src2, TOKEN, T1, NOW)
        self.assertTrue(any("pod list" in r.get("reason", "") for r in rows))
        self.assertTrue(any(r.get("reason") == "no such pod now" for r in rows))
        self.assertFalse(stable["ok"])

    def test_demo_logs_from_the_trap(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "raw").mkdir()
            (run / "raw" / "demo-go-api-a-b.api.log").write_text("one\ntwo\n")
            (run / "raw" / "demo-go-backend-a-b.backend.log").write_text('{"not_written": "secret scan"}\n')
            (run / "trap.jsonl").write_text("not json\n" + json.dumps({"demo_logs": {
                "demo-go-api-a-b/api": {"path": "demo-go-api-a-b.api.log", "lines": 2, "canary_lines": 0},
                "demo-go-backend-a-b/backend": {"path": "demo-go-backend-a-b.backend.log", "lines": 9,
                                                "canary_lines": 1, "marker": True},
                "demo-go-worker-a-b/worker": {"error": "gone"},
                "demo-go-x/api": {"path": "missing.log", "lines": 1, "canary_lines": 0}}}) + "\n")
            rows = sx.canary_demo(run, TOKEN, FakeSources(), T1, T2, {"ok": True, "why": []})
        by = {r["source"]: r for r in rows}
        self.assertEqual(by["demo container log demo-go-api-a-b/api (trap)"]["status"], "scanned")
        b = by["demo container log demo-go-backend-a-b/backend (trap)"]
        self.assertEqual((b["hits"], b["lines"]), (["1 line(s)"], 9))
        self.assertIn("marker", b["note"])
        self.assertEqual(by["demo container log demo-go-worker-a-b/worker (trap)"]["status"], "unmeasured")
        self.assertEqual(by["demo container log demo-go-x/api (trap)"]["status"], "unmeasured")

    def test_demo_logs_file_log_fallback(self):
        src = FakeSources()
        src.loki_q = [('service_instance_id = ""', [{"metric": {"service_name": "soak-k6-loaded"}, "value": [T2, "40"]},
                                                    {"metric": {"service_name": "k8s-events"}, "value": [T2, "3"]}])]
        with tempfile.TemporaryDirectory() as d:
            rows = sx.canary_demo(Path(d), TOKEN, src, T1, T2, {"ok": True, "why": []})
            self.assertEqual(rows[0]["status"], "measured-empty")
            self.assertEqual(sx.ROW_VERDICT["measured-empty"], "REPORT-ONLY")
            self.assertIn("no demo stdout record", rows[0]["reason"])
            (Path(d) / "state.json").write_text(json.dumps({"hook_applied": True}))
            hooked = sx.canary_demo(Path(d), TOKEN, src, T1, T2, {"ok": True, "why": []})
            self.assertEqual(hooked[0]["status"], "unmeasured")
            self.assertIn("no demo_logs record", hooked[0]["reason"])
            (Path(d) / "state.json").write_text("{bad")
            src.loki_q[0][1].append({"metric": {"service_name": "demo-go-api"}, "value": [T2, "2"]})
            self.assertEqual(sx.canary_demo(Path(d), TOKEN, src, T1, T2, {"ok": True, "why": []})[0]["status"],
                             "scanned")
            rows = sx.canary_demo(Path(d), TOKEN, src, T1, T2, {"ok": False, "why": ["otel-logs restarted"]})
            self.assertEqual(rows[0]["status"], "unmeasured")
            self.assertIn("otel-logs restarted", rows[0]["reason"])
            rows = sx.canary_demo(Path(d), TOKEN, FakeSources(), T1, T2, {"ok": True, "why": []})
            self.assertIn("no file_log record of another container", rows[0]["reason"])
            bad = FakeSources()
            bad.loki_query = mock.Mock(side_effect=verify.QueryError("down"))
            self.assertEqual(sx.canary_demo(Path(d), TOKEN, bad, T1, T2, {"ok": True, "why": []})[0]["status"],
                             "unmeasured")

    def test_files_spans_and_verdict(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "k6.log"
            p.write_text(f"a\n{TOKEN}\n")
            self.assertEqual(sx.canary_file(p, TOKEN, "k6")["hits"], ["1 line(s)"])
            self.assertEqual(sx.canary_file(Path(d) / "none", TOKEN, "k6")["status"], "unmeasured")
        spans = verify.parse_trace(ok_trace("go", extra_attrs={"note": f"h={TOKEN}"}))
        self.assertEqual(sx.canary_spans({"t": spans}, TOKEN)["hits"], ["demo-go-api attr note"])
        empty = sx.canary_spans({}, TOKEN)
        self.assertEqual(sx.canary_verdict([empty]), "UNMEASURED")
        self.assertEqual(sx.canary_verdict([{"status": "scanned", "hits": []}]), "PASS")
        self.assertEqual(sx.canary_verdict([{"status": "scanned", "hits": []},
                                            {"status": "measured-empty", "hits": []}]), "UNMEASURED")
        self.assertEqual(sx.canary_verdict([{"status": "incomplete", "hits": []}]), "UNMEASURED")


# ── check 3 ──


def rec(lang, scen, cls, tid=None, parent=None, status=201):
    return {"lang": lang, "route": "orders", "scen": scen, "class": cls, "phase": None,
            "trace_id": tid, "parent_id": parent, "status": status, "vu": 1}


NO_ID = (False, "N/A, AG2")


class Orphans(unittest.TestCase):
    def test_reservoir_bounds_and_skips_noanswer(self):
        rs = [rec("go", "ok", "s01", f"{i:032x}") for i in range(5)] + [rec("go", "ok", "s01", "f" * 32, status=0)]
        self.assertEqual(len(sx.reservoir(rs, 3)["go|ok|s01"]), 3)
        self.assertEqual(len(sx.reservoir(rs, 50)["go|ok|s01"]), 5)

    def test_judge_trace(self):
        s01 = verify.parse_trace(ok_trace("go", parent="cccc"))
        self.assertEqual(sx.judge_trace(s01, "go", "s01", "cccc", False)["orphans"], 0)
        j = sx.judge_trace(s01, "go", "s01", "dddd", False)
        self.assertEqual(j["orphans"], 1)
        self.assertIn("entry parent differs from k6's parent_id", j["problems"])
        self.assertIn("entry span has a parent (class none)", sx.judge_trace(s01, "go", "none", None, False)["problems"])
        root = verify.parse_trace(ok_trace("go"))
        self.assertEqual(sx.judge_trace(root, "go", "none", None, False)["orphans"], 0)
        self.assertIn("entry span is a root (class s01)", sx.judge_trace(root, "go", "s01", "cccc", False)["problems"])
        orphan = verify.parse_trace(trace(span("aaaa", "", "demo-go-api", "POST /orders"),
                                          span("eeee", "9999", "demo-go-backend", "x")))
        self.assertEqual(sx.judge_trace(orphan, "go", "none", None, False)["orphans"], 1)
        noentry = verify.parse_trace(trace(span("eeee", "", "demo-go-backend", "x")))
        self.assertIn("no entry span (api SERVER POST /orders)",
                      sx.judge_trace(noentry, "go", "none", None, False)["problems"])
        split = verify.parse_trace(trace(span("aaaa", "", "demo-go-api", "POST /orders", res={"gateway.id": "g1"}),
                                         span("bbbb", "aaaa", "demo-go-backend", "x", res={"gateway.id": "g2"})))
        self.assertTrue(sx.judge_trace(split, "go", "none", None, False)["gateway_split"])
        one = verify.parse_trace(ok_trace("go", gw="g1"))
        self.assertFalse(sx.judge_trace(one, "go", "none", None, True)["gateway_split"])
        self.assertTrue(sx.judge_trace(root, "go", "none", None, True)["gateway_split"])   # required, absent
        self.assertFalse(sx.judge_trace(root, "go", "none", None, False)["gateway_split"])

    def test_classify(self):
        self.assertEqual(sx.classify(verify.parse_trace(ok_trace("go")), "go"), ("none", "ok"))
        self.assertEqual(sx.classify(verify.parse_trace(ok_trace("go", "cccc", "422")), "go"), ("s01", "business_error"))
        self.assertEqual(sx.classify(verify.parse_trace(trace(span("e", "", "demo-go-backend", "x"))), "go"), (None, None))

    def test_tempo_holds_window(self):
        src = FakeSources()
        self.assertFalse(sx.tempo_holds_window(G(src), T1, T2, ["1" * 32])[0])
        src.traces["1" * 32] = ok_trace("go")
        self.assertTrue(sx.tempo_holds_window(G(src), T1, T2, ["1" * 32])[0])
        src.search = [{"traceID": "abc"}]
        self.assertTrue(sx.tempo_holds_window(G(src), T1, T2, [])[0])
        src.search_err = True
        self.assertIn("Tempo search: search down", sx.tempo_holds_window(G(src), T1, T2, [])[1])

    def test_cell_verdict(self):
        c = sx._new_cell(2)
        self.assertEqual(sx.cell_verdict("go|ok|s01", c, 2)[0], "UNQUALIFIED")
        c["fetched"] = 2
        self.assertEqual(sx.cell_verdict("go|ok|s01", c, 2), ("PASS", []))
        c["errors"] = 1
        self.assertEqual(sx.cell_verdict("go|ok|s01", c, 2)[0], "UNMEASURED")
        c["missing"] = 1
        self.assertEqual(sx.cell_verdict("go|ok|s01", c, 2)[0], "FAIL")
        s00 = sx._new_cell(1)
        s00["absent"] = 1
        self.assertEqual(sx.cell_verdict("go|ok|s00", s00, 1)[0], "PASS")

    def setup_held(self):
        src = FakeSources()
        a, c, d = "a" * 32, "c" * 32, "d" * 32
        src.traces = {a: ok_trace("python", parent="1111"), c: ok_trace("python"), "e" * 32: ok_trace("python", "7777")}
        src.search = [{"traceID": c}, {"traceID": "e" * 32}, {"traceID": "f" * 32}]
        records = [rec("python", "ok", "s01", a, "1111"), rec("python", "ok", "s00", "9" * 32, "4444"),
                   rec("python", "ok", "none")]
        return src, records, d

    def test_orphan_check_pass_with_n1(self):
        src, records, _ = self.setup_held()
        out, traces = sx.orphan_check(G(src), records, ("python", "node"), T1, T2, 1, CLEAN, NO_ID)
        p = out["python"]
        self.assertEqual(p["verdict"], "PASS", p)
        self.assertEqual((p["n"], p["fetched"], p["missing"], p["orphans"]), (1, 2, 0, 0))
        self.assertEqual(p["cells"]["python|ok|s00"]["absent"], 1)
        self.assertEqual(p["cells"]["python|ok|none"]["fetched"], 1)
        self.assertEqual(len(traces), 2)
        n = out["node"]
        self.assertEqual(n["verdict"], "UNMEASURED")
        self.assertEqual(n["finding"], "no reservoir record for this language")

    def test_below_n_missing_and_errors(self):
        src, records, d = self.setup_held()
        out, _ = sx.orphan_check(G(src), records, ("python",), T1, T2, 50, CLEAN, NO_ID)
        self.assertEqual(out["python"]["verdict"], "UNQUALIFIED")           # 1 checked < N 50
        records.append(rec("python", "db_error", "s01", "b" * 32, "2222"))   # not in Tempo
        out, _ = sx.orphan_check(G(src), records, ("python",), T1, T2, 1, CLEAN, NO_ID)
        self.assertEqual(out["python"]["verdict"], "FAIL")
        self.assertEqual(out["python"]["missing"], 1)
        records[-1] = rec("python", "db_error", "s01", d, "3333")
        src.traces[d] = "error"
        out, _ = sx.orphan_check(G(src), records, ("python",), T1, T2, 1, CLEAN, NO_ID)
        self.assertEqual(out["python"]["verdict"], "UNMEASURED")
        self.assertEqual(out["python"]["errors"], 1)

    def test_orphan_check_failures(self):
        src, records, _ = self.setup_held()
        src.traces["9" * 32] = ok_trace("python", parent="4444")
        out, _ = sx.orphan_check(G(src), records, ("python",), T1, T2, 1, CLEAN, NO_ID)
        self.assertEqual(out["python"]["verdict"], "FAIL")
        self.assertTrue(any("unsampled" in p for p in out["python"]["problems"]))
        out, _ = sx.orphan_check(G(src), records, ("python",), T1, T2, 1, DIRTY, NO_ID)
        self.assertEqual((out["python"]["verdict"], out["python"]["measured"]), ("UNQUALIFIED", "FAIL"))
        src.traces.pop("9" * 32)
        out, _ = sx.orphan_check(G(src), records, ("python",), T1, T2, 1, CLEAN, (True, "sets"))
        self.assertTrue(any("gateway.id" in p for p in out["python"]["problems"]))

    def test_orphan_check_unmeasured(self):
        src = FakeSources()
        records = [rec("python", "ok", "s01", "a" * 32, "1111")]
        out, traces = sx.orphan_check(G(src), records, ("python", "go"), T1, T2, 50, CLEAN, NO_ID)
        self.assertEqual(out["python"]["verdict"], "UNMEASURED")
        self.assertIn("Tempo holds no trace", out["python"]["reason"])
        self.assertEqual(out["go"]["finding"], "no reservoir record for this language")
        self.assertEqual(traces, {})

    def test_search_traces_dedup_and_error(self):
        src = FakeSources()
        src.search = [{"traceID": "0abc"}, {"traceID": "abc"}]
        self.assertEqual(sx.search_traces(G(src), "go", T1, T2, 5), ["abc".zfill(32)])
        src, records, _ = self.setup_held()
        orig, calls = src.tempo_search, {"n": 0}

        def search(q, a, e, limit):
            calls["n"] += 1
            if calls["n"] > 1:
                raise verify.QueryError("search down")
            return orig(q, a, e, limit)
        src.tempo_search = search
        out, _ = sx.orphan_check(G(src), records, ("python",), T1, T2, 1, CLEAN, NO_ID)
        self.assertIn("search down", out["python"]["search_error"])   # 3 failed slices stop the reads
        self.assertEqual(out["python"]["verdict"], "UNMEASURED")


class Guard(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.slept: list[float] = []

        def sleep(s):
            self.slept.append(s)
            self.now[0] += s
        self.src = FakeSources()
        self.src.traces = {"a" * 32: ok_trace("go")}
        self.g = sx.TempoGuard(self.src, 10, 1.5 * 2**30, clock=lambda: self.now[0], sleep=sleep, wall=lambda: T2)

    def test_rate_is_sequential_and_bounded(self):
        for _ in range(3):
            self.g.trace("a" * 32)
        self.assertEqual(self.slept, [6.0, 6.0])   # 10 per minute
        self.now[0] += 100
        self.g.trace("a" * 32)
        self.assertEqual(len(self.slept), 2)       # enough time passed: no wait
        self.assertEqual(self.g.stats()["reads"], 4)

    def test_memory_read_every_batch(self):
        calls = []
        orig = self.src.prom
        self.src.prom = lambda q, at: calls.append(q) or orig(q, at)
        for _ in range(11):
            self.g.trace("a" * 32)
        self.assertEqual(len([q for q in calls if "tempo-0" in q]), 2)   # before reads 1 and 11
        self.assertIn('container="tempo"', calls[0])
        self.assertIn('cluster="k8s01"', calls[0])

    def test_memory_stop_line(self):
        self.src.prom_ans = [("container_memory_working_set_bytes",
                              [{"metric": {"cluster": "k8s01"}, "value": [T2, str(1.5 * 2**30)]}])]
        with self.assertRaises(sx.TempoStopped):
            self.g.trace("a" * 32)
        self.assertIn(">= stop line", self.g.stopped)
        self.assertEqual(self.g.calls, 0)
        out = sx.fetch_all(self.g, ["a" * 32, "b" * 32])
        self.assertEqual(set(out.values()), {"stopped"})

    def test_memory_unreadable_stops(self):
        self.src.prom_ans = []
        with self.assertRaises(sx.TempoStopped):
            self.g.search("q", 1, 2, 1)
        self.assertIn("unreadable", self.g.stopped)
        g = sx.TempoGuard(self.src, 10, sleep=lambda s: None)
        self.src.prom = mock.Mock(side_effect=verify.QueryError("prom down"))
        with self.assertRaises(sx.TempoStopped):
            g.metrics("q", 1, 2, 3)
        self.assertIn("prom down", g.stopped)

    def test_errors_never_abort_and_three_in_a_row_stop(self):
        self.src.traces = {"e1": "error", "e2": "error", "e3": "error", "ok": ok_trace("go")}
        out = sx.fetch_all(self.g, ["e1", "ok", "e2", "e3", "ok"])
        self.assertEqual(out["e1"], "error")
        self.assertIsInstance(out["ok"], list)
        self.assertIsNone(self.g.stopped)
        out = sx.fetch_all(self.g, ["e1", "e2", "e3", "ok"])
        self.assertEqual([out[k] for k in ("e1", "e2", "e3", "ok")], ["error", "error", "error", "stopped"])
        self.assertIn("3 consecutive Tempo errors", self.g.stopped)
        self.assertEqual(self.g.stats()["errors"], 6)

    def test_success_resets_the_error_run(self):
        self.src.traces = {"e": "error", "ok": ok_trace("go")}
        out = sx.fetch_all(self.g, ["e", "e", "ok", "e", "e", "ok"])
        self.assertIsNone(self.g.stopped)
        self.assertIsInstance(out["ok"], list)

    def test_error_text_is_redacted(self):
        sx._SECRET[:] = [TOKEN]
        try:
            self.src.tempo_trace = mock.Mock(side_effect=verify.QueryError(f"EOF {TOKEN}"))
            with self.assertRaises(sx.TempoError) as cm:
                self.g.trace("x")
        finally:
            sx._SECRET.clear()
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_bad_rate(self):
        with self.assertRaises(sx.InputError):
            sx.TempoGuard(self.src, 0)

    def test_orphans_stop_midway_is_unmeasured(self):
        src = FakeSources()
        src.search = [{"traceID": "c" * 32}]
        src.traces = {"c" * 32: ok_trace("python"), "a" * 32: ok_trace("python", parent="1111")}
        records = [rec("python", "ok", "s01", "a" * 32, "1111"), rec("python", "db_error", "s01", "b" * 32, "1")]
        state = {"n": 0}
        orig = src.prom

        def prom(q, at):
            state["n"] += 1
            if "tempo-0" in q and state["n"] > 1:
                return [{"metric": {"cluster": "k8s01"}, "value": [T2, str(2 * 2**30)]}]
            return orig(q, at)
        src.prom = prom
        g = G(src, batch=2)
        out, _ = sx.orphan_check(g, records, ("python",), T1, T2, 1, CLEAN, NO_ID)
        self.assertEqual(out["python"]["verdict"], "UNMEASURED")
        self.assertIn("stop line", out["python"]["reason"])
        self.assertGreater(out["python"]["stopped"], 0)


class Traffic(unittest.TestCase):
    def test_metric_traffic_and_counter_of(self):
        self.assertEqual(sx.counter_of("a_sum"), "a_count")
        self.assertEqual(sx.counter_of("a_total"), "a_total")
        self.assertIsNone(sx.counter_of("a_bytes"))
        job = {"go": "dev-otel-libs/demo-go-worker", "py": "dev-otel-libs/demo-python-worker"}

        def inst(job_, v, name="traces_span_metrics_calls_total"):
            return {"metric": {"cluster": "k8s01", "__name__": name, "job": job_}, "value": [T2, str(v)]}
        src = FakeSources()
        answers = {T1: [inst(job["go"], 10), inst(job["py"], 5)],
                   T2: [inst(job["go"], 30), inst(job["py"], 5), inst("other/job", 9),
                        inst("dev-otel-libs/demo-node-worker", 4)]}
        src.prom = lambda q, at: answers[at]
        traffic, q = sx.metric_traffic(src, T1, T2)
        self.assertTrue(traffic[("worker", "go", "traces_span_metrics_calls_total")])
        self.assertFalse(traffic[("worker", "python", "traces_span_metrics_calls_total")])
        self.assertTrue(traffic[("worker", "node", "traces_span_metrics_calls_total")])   # new at t2
        self.assertIn("_count|_total", q)

    def test_label_keys_without_traffic_are_unmeasured(self):
        metrics = {"worker": {"go": {"traces_span_metrics_calls_total": {"http_request_method"}},
                              "python": {"traces_span_metrics_calls_total": set()}}}
        traffic = {("worker", "go", "traces_span_metrics_calls_total"): True,
                   ("worker", "python", "traces_span_metrics_calls_total"): False}
        items = sx.parity_items(metrics, {}, [], ("go", "python"), [], traffic=traffic)
        attr = next(i for i in items if i["kind"] == "metric attr")
        self.assertEqual(attr["status"], "UNMEASURED")
        self.assertIn("no traffic", attr["reason"])
        traffic[("worker", "python", "traces_span_metrics_calls_total")] = True
        items = sx.parity_items(metrics, {}, [], ("go", "python"), [], traffic=traffic)
        self.assertEqual(next(i for i in items if i["kind"] == "metric attr")["status"], "FAIL")
        src = FakeSources()
        src.series_ans = [{"__name__": "traces_span_metrics_calls_total", "job": f"dev-otel-libs/demo-{lg}-worker",
                           "cluster": "k8s01", **({"http_request_method": "GET"} if lg == "go" else {})}
                          for lg in ("go", "python")]
        src.metrics = [{"labels": [{"key": "resource.service.name", "value": {"stringValue": f"demo-{lg}-api"}},
                                   {"key": "name", "value": {"stringValue": "x"}},
                                   {"key": "kind", "value": {"stringValue": "server"}}]} for lg in ("go", "python")]
        out, _ = sx.parity_check(src, G(src), ("go", "python"), T1, T2, {}, CLEAN, [], [])
        self.assertEqual(out["verdict"], "UNMEASURED")   # no counter answer: no traffic proven
        self.assertIn("without traffic", out["reason"])


# ── check 4 ──


GAPS_MD = """# Doc

## Known emitted-telemetry gaps

Intro mentioning `nothing`.

| Area | Gap |
|------|-----|
| Python and Node.js gRPC | Spans carry the legacy `rpc.system` attributes, no `rpc.server.duration` |
| .NET framework metrics | `kestrel.*` series |
| Go and Python spans | `only-go` span name |

## Next

| Area | Gap |
|------|-----|
| Go | `go_outside` |
"""


def series_of(name, langs, role="api", **labels):
    return [{"__name__": name, "job": f"dev-otel-libs/demo-{lg}-{role}", "cluster": "k8s01",
             **(labels.get(lg) or {})} for lg in langs]


def metric_item(kind, item, have, role="api", **extra):
    return {"kind": kind, "role": role, "item": item, "have": have,
            "missing": [lg for lg in verify.LANGS if lg not in have], **extra}


class Parity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.md = Path(self.tmp.name) / "gaps.md"
        self.md.write_text(GAPS_MD)
        self.rows = sx.gap_rows((self.md,))

    def tearDown(self):
        self.tmp.cleanup()

    def test_gap_rows(self):
        areas = [r["area"] for r in self.rows]
        self.assertIn("Python and Node.js gRPC", areas)
        self.assertNotIn("Go", areas)          # outside the section
        grpc = next(r for r in self.rows if r["area"].startswith("Python"))
        self.assertEqual(grpc["langs"], {"python", "node"})

    def test_candidate_row_tie_both_sides(self):
        self.assertTrue(sx.token_matches("http.server.request.duration", "http_server_request_duration_seconds_count"))
        self.assertTrue(sx.token_matches("kestrel.*", "kestrel_queued_connections"))
        self.assertFalse(sx.token_matches("rpc.system", "rpc_systems"))
        # 2 vs 2: the row names python/node; the item is on the go/dotnet side or the other.
        self.assertIsNotNone(sx.candidate_row("rpc_system", ["go", "dotnet"], ["python", "node"], self.rows))
        self.assertIsNotNone(sx.candidate_row("rpc_system", ["python", "node"], ["go", "dotnet"], self.rows))
        self.assertIsNone(sx.candidate_row("rpc_system", ["python"], ["go", "dotnet"], self.rows, context="http_server"))

    def test_allowed(self):
        e = {"kind": "metric attr", "role": "*", "item": "http_server_", "match": "prefix", "attribute": "server_port",
             "langs_missing": ["python", "node", "dotnet"], "status": "accepted", "reason": "r", "gaps_ref": "g"}
        it = metric_item("metric attr", "http_server_request_duration_seconds_count server_port", ["go"],
                         metric="http_server_request_duration_seconds_count", attribute="server_port")
        self.assertIs(sx.allowed(it, [e]), e)
        self.assertIsNone(sx.allowed({**it, "attribute": "server_address"}, [e]))
        self.assertIsNone(sx.allowed({**it, "missing": ["python", "node", "dotnet", "go"]}, [e]))
        self.assertIsNone(sx.allowed(it, [{**e, "status": "proposed"}]))
        self.assertIs(sx.allowed(it, [{**e, "status": "proposed"}], accept_proposed=True)["status"], "proposed")
        self.assertIsNone(sx.allowed(it, [{**e, "match": "exact"}]))
        self.assertIsNone(sx.allowed(it, [{**e, "role": "backend"}]))
        span = metric_item("span", "only-go", ["go"], span_kind="internal")
        se = {"kind": "span", "role": "api", "item": "only-go", "span_kind": "server",
              "langs_missing": ["python", "node", "dotnet"], "status": "accepted", "reason": "r", "gaps_ref": "g"}
        self.assertIsNone(sx.allowed(span, [se]))
        self.assertEqual(sx.allowed(span, [{**se, "span_kind": None}]), se | {"span_kind": None})

    def test_load_allowlist(self):
        p = Path(self.tmp.name) / "a.json"
        p.write_text(json.dumps({"entries": [{"kind": "metric"}]}))
        with self.assertRaises(sx.InputError):
            sx.load_allowlist(p)
        p.write_text(json.dumps([]))
        self.assertEqual(sx.load_allowlist(p), [])
        real = sx.load_allowlist()
        self.assertTrue(real and all(e["status"] in ("proposed", "needs-row") for e in real))
        self.assertTrue(any(e["status"] == "needs-row" for e in real))
        self.assertFalse(any(e["role"] == "worker" and e["item"].startswith("traces_span_metrics_") for e in real))
        row = next(e for e in real if e["status"] == "needs-row")
        it = {"kind": row["kind"], "role": row["role"], "item": row["item"], "missing": row["langs_missing"],
              "have": [], **({"metric": row["item"], "attribute": row["attribute"]} if row.get("attribute") else {})}
        self.assertIsNone(sx.allowed(it, real, accept_proposed=True))   # needs-row never passes

    def test_parity_items(self):
        series = (series_of("http_server_request_duration_seconds_count", verify.LANGS,
                            python={"rpc_system": "grpc"}, node={"rpc_system": "grpc"})
                  + series_of("go_goroutine_count", ["go"]) + series_of("orders_total", ["go"])
                  + [{"__name__": "orders_total", "job": "dev-otel-libs/demo-python-api", "cluster": "vps"},
                     {"__name__": "x", "job": "other/job", "cluster": "k8s01"}])
        metrics = sx.metric_sets(series)
        tser = [{"labels": [{"key": "resource.service.name", "value": {"stringValue": f"demo-{lg}-api"}},
                            {"key": "name", "value": {"stringValue": f"send demo:{lg}:orders"}},
                            {"key": "kind", "value": {"stringValue": "producer"}}]} for lg in verify.LANGS]
        tser.append({"labels": [{"key": "resource.service.name", "value": {"stringValue": "demo-go-api"}},
                                {"key": "name", "value": {"stringValue": "only-go"}},
                                {"key": "kind", "value": {"stringValue": "SPAN_KIND_INTERNAL"}}]})
        spans = sx.span_sets(tser, {"t": verify.parse_trace(ok_trace("go"))})
        self.assertIn(("send demo:<lang>:orders", "producer"), spans["api"]["python"])
        entry = {"kind": "metric", "role": "api", "item": "orders_total", "langs_missing": ["python", "node", "dotnet"],
                 "status": "accepted", "reason": "r", "gaps_ref": "g"}
        items = sx.parity_items(metrics, spans, self.rows, verify.LANGS, [entry])
        by = {(i["kind"], i["item"]): i for i in items}
        self.assertEqual(by[("metric", "go_goroutine_count")]["status"], "documented")
        self.assertEqual(by[("metric", "orders_total")]["status"], "documented")
        attr = by[("metric attr", "http_server_request_duration_seconds_count rpc_system")]
        self.assertEqual(attr["status"], "FAIL")
        self.assertIsNone(attr["candidate"])          # the gRPC row is not about http.server
        only = by[("span", "only-go")]
        self.assertEqual((only["status"], only["span_kind"]), ("FAIL", "internal"))
        self.assertIn("Go and Python spans", only["candidate"])   # matched on the name
        self.assertNotIn(("span", "send demo:<lang>:orders"), by)

    def test_parity_check(self):
        src = FakeSources()
        src.series_ans = series_of("orders_total", ["go"]) + series_of("m", verify.LANGS)
        src.md = {"orders_total": [{"unit": "1"}, {"unit": "By"}]}
        out, q = sx.parity_check(src, G(src), verify.LANGS, T1, T2, {}, CLEAN, self.rows, [])
        self.assertEqual(out["verdict"], "FAIL")
        self.assertIn("spans unmeasured", out["reason"])
        self.assertEqual(out["units_report_only"], {"orders_total": ["1", "By"]})
        self.assertIn("demo:go:", out["normalization"])
        self.assertTrue(q[0].startswith("series "))
        src.series_ans = series_of("m", verify.LANGS)
        src.metrics_err = True
        out, _ = sx.parity_check(src, G(src), verify.LANGS, T1, T2, {}, CLEAN, self.rows, [])
        self.assertEqual(out["verdict"], "UNMEASURED")
        self.assertIn("TraceQL metrics: metrics down", out["spans_note"])
        src.metrics_err = False
        src.metrics = [{"labels": [{"key": "resource.service.name", "value": {"stringValue": f"demo-{lg}-api"}},
                                   {"key": "name", "value": {"stringValue": "POST /orders"}},
                                   {"key": "kind", "value": {"stringValue": "server"}}]} for lg in verify.LANGS]
        out, _ = sx.parity_check(src, G(src), verify.LANGS, T1, T2, {}, CLEAN, self.rows, [])
        self.assertEqual(out["verdict"], "PASS", out["items"])
        out, _ = sx.parity_check(src, G(src), verify.LANGS, T1, T2, {}, CLEAN, self.rows, [], accept_proposed=True,
                                 allowlist=Path("/elsewhere/gaps.json"))
        self.assertEqual(out["verdict"], "PREVIEW")   # never PASS in a preview
        self.assertIn("preview", out)
        self.assertEqual(out["allowlist"], "/elsewhere/gaps.json")
        out, _ = sx.parity_check(src, G(src), verify.LANGS, T1, T2, {}, DIRTY, self.rows, [])
        self.assertEqual(out["verdict"], "UNQUALIFIED")
        src.series_ans = []
        out, _ = sx.parity_check(src, G(src), verify.LANGS, T1, T2, {}, CLEAN, self.rows, [])
        self.assertEqual(out["reason"], "no demo series in the window")


class ParityRegression(unittest.TestCase):
    """The four false PASSes the round-1 code-review reproduced with the token
    heuristic, on the real gaps tables and the real allowlist: each is a FAIL,
    also when the proposed entries are previewed."""

    def judge(self, items):
        rows, entries = sx.gap_rows(), sx.load_allowlist()
        out = []
        for accept in (False, True):
            got = []
            for it in items:
                metrics = {}
                for lg in it["have_metric"]:
                    metrics.setdefault(it["role"], {}).setdefault(lg, {})[it["metric"]] = set(it["keys"].get(lg, ()))
                got += sx.parity_items(metrics, {}, rows, verify.LANGS, entries, accept)
            out.append(got)
        return out

    def assert_fail(self, items, want):
        for got in self.judge(items):
            by = {(i["kind"], i["item"]): i for i in got}
            self.assertEqual(by[want]["status"], "FAIL", by[want])

    def test_python_without_http_server_duration(self):
        self.assert_fail([{"role": "api", "metric": "http_server_request_duration_seconds_count",
                           "have_metric": ["go", "node", "dotnet"], "keys": {}}],
                         ("metric", "http_server_request_duration_seconds_count"))

    def test_go_without_db_operation_duration(self):
        self.assert_fail([{"role": "backend", "metric": "db_client_operation_duration_seconds_count",
                           "have_metric": ["python", "node", "dotnet"], "keys": {}}],
                         ("metric", "db_client_operation_duration_seconds_count"))

    def test_go_only_exception_count(self):
        self.assert_fail([{"role": "api", "metric": "exception_count_total", "have_metric": ["go"], "keys": {}}],
                         ("metric", "exception_count_total"))

    def test_error_type_on_http_server(self):
        keys = {"dotnet": ["error_type"], "go": [], "python": [], "node": []}
        self.assert_fail([{"role": "api", "metric": "http_server_request_duration_seconds_count",
                           "have_metric": list(verify.LANGS), "keys": keys}],
                         ("metric attr", "http_server_request_duration_seconds_count error_type"))


# ── check 5 and the driver ──


class Logs(unittest.TestCase):
    def test_severity_counts(self):
        src = FakeSources()
        src.loki_q = [("severity_number", [{"metric": {"service_name": "demo-go-api", "severity_number": "17"},
                                            "value": [T2, "12"]}])]
        out, q = sx.severity_counts(src, T1 + 0.4, T2, {})
        self.assertEqual(out["services"], {"demo-go-api": {"17": 12}})
        self.assertIn("no per-severity expectation", out["expected"])
        self.assertIn('service_instance_id != ""', q)
        out, _ = sx.severity_counts(src, T1, T2, {"log_severity": {"demo-go-api": {"17": 12}}})
        self.assertEqual(out["expected"], {"demo-go-api": {"17": 12}})


def parse(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", default="k8s01")
    parser.add_argument("--langs", type=verify._langs, default=list(verify.LANGS))
    sub = parser.add_subparsers(dest="mode")
    sc.add_parser(sub)
    return parser.parse_args(argv)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.runs = self.d / "runs"
        (self.runs / "r1").mkdir(parents=True)
        (self.runs / "r1" / "canary").write_text(TOKEN)
        per = {("python", s, c): n for (s, n) in (("ok", 700), ("business_error", 100), ("db_error", 100),
                                                  ("unhandled", 100)) for c in ("none", "s01", "s00")}
        self.log = self.d / "k6.log"
        self.log.write_text(k6_log(per, [rec("python", "ok", "s01", "a" * 32, "1111")]))
        self.kcal = self.d / "kcal.json"
        self.kcal.write_text(json.dumps(kcal()))

    def tearDown(self):
        self.tmp.cleanup()

    def src(self):
        src = FakeSources()
        src.traces = {"a" * 32: ok_trace("python", parent="1111")}
        src.search = [{"traceID": "c" * 32}]
        src.traces["c" * 32] = ok_trace("python")
        src.prom_ans = [MEM_OK, (sx.SG_METRIC, [sgser("demo-python-api", "demo-python-backend", "false", [(T1 - 10, 0), (T2, 1400)]),
                                        sgser("demo-python-api", "demo-python-backend", "true", [(T1 - 10, 0), (T2, 400)])])]
        src.series_ans = series_of("m", ["python"])
        src.metrics = [{"labels": [{"key": "resource.service.name", "value": {"stringValue": "demo-python-api"}},
                                   {"key": "name", "value": {"stringValue": "POST /orders"}},
                                   {"key": "kind", "value": {"stringValue": "server"}}]}]
        return src

    def run_extras(self, argv, src):
        args = parse(["--langs", "python", "soak", "extras", "--run", "r1", "--out-dir", str(self.d), *argv])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch.object(sp, "read_sentinels", return_value=[[sp.CounterVerdict("a", "shared", "CLEAN")]]), \
                mock.patch.object(sc, "gateway_restart", return_value=(True, "ok")), \
                mock.patch.object(sc, "queue_check", return_value=(True, "ok")):
            rc = sx.main_extras(None, args, Path(args.out_dir), args.langs, NOW, src=src, runs_dir=self.runs)
        return rc, out.getvalue() + err.getvalue()

    def base(self):
        return ["--t1", str(T1), "--t2", str(T2), "--k6-log", str(self.log), "--kcal", str(self.kcal),
                "--orphan-sample", "1", "--k6-end", str(T2 - 100), "--tempo-rate", "1e9"]

    def test_end_to_end(self):
        src = self.src()
        rc, out = self.run_extras(self.base(), src)
        data = json.loads((self.d / "soak-extras.json").read_text())
        self.assertEqual(data["graph"]["python"]["verdict"], "PASS", out)
        self.assertEqual(data["orphans"]["python"]["verdict"], "PASS", out)
        self.assertEqual(data["parity"]["verdict"], "PASS", out)
        self.assertEqual(data["canary"]["verdict"], "UNMEASURED")   # no Loki stream, no Collector pod in the fake
        self.assertEqual(data["k6_end_source"], "input")
        self.assertEqual(rc, 1)
        self.assertIn("graph python api->backend", out)
        self.assertNotIn(TOKEN, out + (self.d / "soak-extras.json").read_text())
        self.assertFalse(any(TOKEN.lower() in str(q).lower() for q in src.queries))   # never in a query
        self.assertTrue(any(r["source"] == "soak-extras.json (before writing)" for r in data["canary"]["sources"]))
        self.assertTrue(any(r["source"].startswith("k6 log") for r in data["canary"]["sources"]))
        self.assertTrue(sc.rows_for("extras", data)["rows"])
        self.assertIn("PASS         canary k6 log", out)

    def test_canary_hit_is_named_not_shown(self):
        src = self.src()
        src.traces["a" * 32] = ok_trace("python", parent="1111", extra_attrs={"h": TOKEN})
        rc, out = self.run_extras(self.base(), src)
        self.assertEqual(rc, 1)
        text = (self.d / "soak-extras.json").read_text()
        self.assertIn("demo-python-api attr h", text)
        self.assertNotIn(TOKEN, text + out)

    def test_missing_and_errors(self):
        rc, out = self.run_extras([], self.src())
        self.assertEqual(rc, 2)
        self.assertIn("--count-window", out)
        src = self.src()
        src.kget = mock.Mock(side_effect=verify.QueryError(f"boom {TOKEN.upper()}"))
        rc, out = self.run_extras(self.base(), src)
        self.assertEqual(rc, 1)
        self.assertIn("<canary>", out)
        self.assertNotIn(TOKEN, out.lower())
        src.kget = mock.Mock(side_effect=RuntimeError(f"surprise {TOKEN}"))
        rc, out = self.run_extras(self.base(), src)
        self.assertEqual(rc, 1)
        self.assertIn("RuntimeError", out)
        self.assertNotIn(TOKEN, out)

    def test_count_window_not_valid_exits_2(self):
        cw = self.runs / "r1" / "count-window.json"
        cw.write_text(json.dumps({"valid": False, "reasons": ["quiesce invalid"]}))
        rc, out = self.run_extras(["--count-window", str(cw), "--kcal", str(self.kcal)], self.src())
        self.assertEqual(rc, 2)
        self.assertIn("quiesce invalid", out)

    def test_no_canary_and_output_refused(self):
        (self.runs / "r1" / "canary").unlink()
        target = self.d / "x.json"
        rc, out = self.run_extras(self.base() + ["--output", str(target)], self.src())
        data = json.loads(target.read_text())
        self.assertEqual(data["canary"]["sources"][0]["reason"], "no canary file")
        rc, out = self.run_extras(self.base() + ["--output", str(target)], self.src())
        self.assertEqual(rc, 1)
        self.assertIn("not overwritten", out)

    def test_json_never_carries_canary(self):
        with mock.patch.object(sx, "orphan_check", return_value=({"python": {"verdict": "PASS", "x": TOKEN}}, {})):
            rc, out = self.run_extras(self.base(), self.src())
        self.assertEqual(rc, 1)
        self.assertIn("carries the canary: not written", out)
        self.assertNotIn(TOKEN, out)
        self.assertFalse((self.d / "soak-extras.json").exists())

    def test_preview_never_exits_0(self):
        rc, out = self.run_extras(self.base() + ["--sr10-accept-proposed"], self.src())
        data = json.loads((self.d / "soak-extras.json").read_text())
        self.assertEqual(data["parity"]["verdict"], "PREVIEW")
        self.assertEqual(rc, 1)

    def test_tempo_eof_never_aborts(self):
        src = self.src()
        src.tempo_trace = mock.Mock(side_effect=verify.QueryError("tempo:3200/api/traces/x: EOF"))
        rc, out = self.run_extras(self.base(), src)
        self.assertEqual(rc, 1)
        self.assertNotIn("ERROR", out)
        data = json.loads((self.d / "soak-extras.json").read_text())
        self.assertEqual(data["orphans"]["python"]["verdict"], "UNMEASURED")
        self.assertGreaterEqual(data["tempo"]["errors"], 1)

    def test_bad_tempo_rate_exits_2(self):
        rc, out = self.run_extras(self.base() + ["--tempo-rate", "0"], self.src())
        self.assertEqual(rc, 2)
        self.assertIn("--tempo-rate", out)

    def test_overall_ok(self):
        data = {"graph": {"go": {"verdict": "REPORT-ONLY"}}, "orphans": {"go": {"verdict": "PASS"}},
                "canary": {"verdict": "PASS"}, "parity": {"verdict": "PASS"}}
        self.assertTrue(sx.overall_ok(data))
        data["parity"]["verdict"] = "UNMEASURED"
        self.assertFalse(sx.overall_ok(data))

    def test_main_soak_dispatch(self):
        with mock.patch.object(sx, "main_extras", return_value=0) as m:
            self.assertEqual(verify.main(["soak", "extras", "--run", "r1", "--count-window", "cw.json",
                                          "--sr10-accept-proposed"]), 0)
        self.assertEqual(m.call_args.args[1].count_window, Path("cw.json"))
        self.assertTrue(m.call_args.args[1].sr10_accept_proposed)
        args = parse(["soak", "extras", "--run", "r1", "--output", str(self.kcal)])
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(sc.main_soak(None, args, NOW, reader=object()), 1)
        self.assertIn("not overwritten", err.getvalue())

    def test_sources_wraps_reader(self):
        r = mock.Mock()
        r.b = mock.Mock()
        r.b._get.return_value = {"status": "success", "data": {"m": []}}
        s = sx.Sources(r)
        self.assertEqual(s.metadata(), {"m": []})
        s.prom("q", 1)
        s.series("m", 1, 2)
        s.loki_entries("q", 1, 2, 3)
        s.loki_query("q", 1)
        s.tempo_trace("t")
        s.tempo_search("q", 1, 2, 3)
        s.tempo_metrics("q", 1, 2, 3)
        s.kget(["pods"])
        s.logs("ns", "p", "c", 10)
        r._run.assert_called_with(["logs", "-n", "ns", "p", "-c", "c", "--since=10s"])
        r.b.loki_entries.assert_called_with("q", 1, 2, limit=3)


if __name__ == "__main__":
    unittest.main()
