"""Tests for ``verify.py soak`` (specs/soak-simulation K-count, r6.2 with the K0e
corrections; soakcount.py).

Standard library unittest; no cluster access. ``fixtures/soak-count-series.json``
carries the label sets read on k8s01 on 2026-11-23 (values made up); the K8a RSS
shape (steps over the first 12 h, then flat) follows the pre-soak evidence of
design S-D5 (2026-11-23 recalibration).
"""

from __future__ import annotations

import argparse
import io
import json
import re
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
import soakplatform as sp  # noqa: E402
import verify  # noqa: E402

FIX = json.loads((HERE / "fixtures" / "soak-count-series.json").read_text())
T0 = 1_795_000_000.0
H = 3600.0


def shift(series: list[dict], dt: float) -> list[dict]:
    return [{**s, "values": [[float(t) + dt, v] for t, v in s["values"]]} for s in series]


def ser(metric: dict, values) -> dict:
    return {"metric": {"cluster": "k8s01", **metric}, "values": [[t, str(v)] for t, v in values]}


def inst(metric: dict, value: float, at: float = T0) -> dict:
    return {"metric": {"cluster": "k8s01", **metric}, "value": [at, str(value)]}


class FakeReader:
    """Range selectors answered from ``raw`` (metric name -> series), bounded to the
    range; instant and range queries answered by the first matching pattern."""

    context = "k8s01"

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.raw: dict[str, list[dict]] = {}
        self.instant: list[tuple[str, list[dict]]] = []
        self.ranges: list[tuple[str, list[dict]]] = []

    def prom(self, q: str, at: float) -> list[dict]:
        self.queries.append(q)
        m = re.fullmatch(r"([a-z_]+)\{.*\}\[(\d+)s\]", q)
        if m:
            width = float(m.group(2))
            return [{**s, "values": [v for v in s["values"] if at - width <= float(v[0]) <= at]}
                    for s in self.raw.get(m.group(1), [])]
        for pattern, ans in self.instant:
            if re.search(pattern, q):
                return ans
        return []

    def prom_range(self, q: str, start: float, end: float, step: int) -> list[dict]:
        self.queries.append(q)
        for pattern, ans in self.ranges:
            if re.search(pattern, q):
                return ans
        return []


# ── series rules ──


class SeriesRules(unittest.TestCase):
    GW = dict(expiry=sc.GATEWAY_EXPIRY_S, scrape=sc.GATEWAY_SCRAPE_S)   # 5 m, 30 s
    CP = dict(expiry=sc.COPY_EXPIRY_S, scrape=sc.COPY_SCRAPE_S)         # 30 m, 15 s

    def cs(self, values, t1, t2, src):
        return sc.count_series(values, t1, t2, **src)

    def test_continuous_series_sums_positive_differences(self):
        self.assertEqual(self.cs([[0, "10"], [30, "15"], [60, "21"]], 0, 60, self.GW), (11.0, 11.0, []))

    def test_first_sample_inside_the_window_adds_its_value(self):
        lo, hi, ev = self.cs([[30, "4"], [60, "8"]], 0, 60, self.GW)
        self.assertEqual((lo, hi), (8.0, 8.0))
        self.assertIn("first sample", ev[0])

    def test_lower_value_is_a_reset(self):
        self.assertEqual(self.cs([[0, "10"], [30, "15"], [60, "3"]], 0, 60, self.GW)[:2], (8.0, 8.0))

    def test_short_gap_adds_v_minus_last(self):
        self.assertEqual(self.cs([[0, "10"], [240, "16"]], 0, 300, self.GW)[:2], (6.0, 6.0))

    def test_short_gap_lower_value_adds_v(self):
        lo, hi, ev = self.cs([[0, "10"], [240, "4"]], 0, 300, self.GW)
        self.assertEqual((lo, hi), (4.0, 4.0))
        self.assertIn("reset", ev[0])

    def test_gap_at_least_expiry_is_a_reset_adding_v(self):
        lo, hi, ev = self.cs([[0, "10"], [300, "16"]], 0, 400, self.GW)
        self.assertEqual((lo, hi), (16.0, 16.0))
        self.assertIn("expired", ev[0])

    def test_same_gap_differs_by_source_expiry(self):
        vals = [[0, "10"], [600, "16"]]  # 10 min gap
        self.assertEqual(self.cs(vals, 0, 900, self.GW)[:2], (16.0, 16.0))  # >= 5 m: expired
        self.assertEqual(self.cs(vals, 0, 900, self.CP)[:2], (6.0, 6.0))    # < 30 m: v - last

    def test_reappearance_across_t1(self):
        lo, hi, ev = self.cs([[-400, "10"], [30, "12"], [60, "14"]], 0, 60, self.GW)
        self.assertEqual((lo, hi), (14.0, 14.0))
        self.assertIn("expired", ev[0])
        self.assertEqual(self.cs([[-400, "10"], [30, "12"]], 0, 60, self.CP)[:2], (2.0, 2.0))

    def test_no_sample_inside(self):
        self.assertEqual(self.cs([[-30, "5"]], 0, 60, self.GW), (0.0, 0.0, []))

    @staticmethod
    def flat(changed_at: float, until: float, step: float, value: str, before: str) -> list:
        """A value that changed at ``changed_at`` and was scraped flat until ``until``."""
        return [[changed_at - step, before]] + [[t, value] for t in range(int(changed_at), int(until) + 1, int(step))]

    def check_source(self, src):
        e, k = src["expiry"], src["scrape"]
        flat = self.flat(-e + 2 * k, 0, k, "40", "39")       # unchanged since -e + 2k, last sample at 0
        # (1) a gap of more than 2 scrapes after the expiry: expired, adds v
        lo, hi, ev = self.cs(flat + [[3 * k, "41"]], 0, 4 * k, src)
        self.assertEqual((lo, hi), (41.0, 41.0))
        self.assertIn("expired", ev[0])
        # (2) a 1-scrape and a 2-scrape gap after the expiry, v >= last: ambiguous
        for gap in (k, 2 * k):
            lo, hi, ev = self.cs(flat + [[gap, "41"]], 0, 4 * k, src)
            self.assertEqual((lo, hi), (1.0, 41.0), gap)
            self.assertIn("ambiguous: expiry or update", ev[0])
        # the change clock restarts: the following flat samples are not ambiguous again
        lo, hi, ev = self.cs(flat + [[k, "41"], [2 * k, "41"], [3 * k, "43"]], 0, 4 * k, src)
        self.assertEqual((lo, hi), (3.0, 43.0))
        self.assertEqual(sum("ambiguous" in x for x in ev), 1)
        # (3) the same gaps with a recent change: an update, v - last
        recent = self.flat(-3 * k, 0, k, "40", "39")
        for gap in (k, 2 * k):
            self.assertEqual(self.cs(recent + [[gap, "41"]], 0, 4 * k, src)[:2], (1.0, 1.0))
        # (4) v < last after the expiry: a reset, v
        self.assertEqual(self.cs(flat + [[k, "3"]], 0, 4 * k, src)[:2], (3.0, 3.0))

    def test_idle_flat_to_expiry_adds_nothing(self):
        # 5 -> 10 at t=0, then scraped flat until the copy expiry: count 5, not ambiguous.
        k, e = self.CP["scrape"], self.CP["expiry"]
        vals = [[-k, "5"]] + [[t, "10"] for t in range(0, int(e) + 1, int(k))]
        lo, hi, ev = self.cs(vals, -1, e + 60, self.CP)
        self.assertEqual((lo, hi), (5.0, 5.0))
        self.assertFalse(any("ambiguous" in x for x in ev))

    def test_long_gap_return_after_flat_to_expiry(self):
        # Flat until the expiry, back 150 s later with 12 then 14: expired, 10 + ... = 19.
        k, e = self.CP["scrape"], self.CP["expiry"]
        vals = [[-k, "5"]] + [[t, "10"] for t in range(0, int(e) + 1, int(k))]
        vals += [[e + 150, "12"], [e + 165, "14"]]
        self.assertEqual(self.cs(vals, -1, e + 200, self.CP)[:2], (19.0, 19.0))
        # back 45 s later with a lower value 3: a reset, 5 + 3 = ... plus nothing else
        vals2 = [[-k, "5"]] + [[t, "10"] for t in range(0, int(e) + 1, int(k))] + [[e + 45, "3"]]
        self.assertEqual(self.cs(vals2, -1, e + 60, self.CP)[:2], (8.0, 8.0))

    def test_flat_value_after_a_scrape_gap_adds_nothing(self):
        k, e = self.GW["scrape"], self.GW["expiry"]
        vals = [[-k, "5"]] + [[t, "10"] for t in range(0, int(e) + 1, int(k))] + [[e + 4 * k, "10"]]
        self.assertEqual(self.cs(vals, -1, e + 5 * k, self.GW)[:2], (5.0, 5.0))

    def test_ambiguous_band_copy(self):
        self.check_source(self.CP)

    def test_ambiguous_band_gateway(self):
        self.check_source(self.GW)

    def test_measured_scrape(self):
        series = [{"values": [[t, "1"] for t in range(0, 600, 15)]}]
        self.assertEqual(sc.measured_scrape(series, 30), 15)
        self.assertEqual(sc.measured_scrape([{"values": [[0, "1"], [15, "1"]]}], 30), 30.0)   # too few

    def test_counted_carries_the_upper_value(self):
        c = sc.Counted(3, 7)
        self.assertEqual((float(c), c.hi, sc.upper(c), sc.upper(2.0)), (3.0, 7.0, 7.0, 2.0))
        self.assertEqual(sc.Counted(4).hi, 4.0)

    def test_born_vs_history_through_the_read_range(self):
        # A series last seen 20 min before t1: inside the copy's read (30 m + margin) it is a
        # baseline (v - last); outside the gateway's read (5 m + margin) it is born / reset (v).
        r = FakeReader()
        t1, t2 = T0, T0 + 600
        old = [[t1 - 1200, "100"], [t1 + 60, "104"]]
        r.raw[sc.FIXED_METRIC] = [ser({"service_name": "demo-go-api", "service_instance_id": "a"}, old)]
        r.raw[sc.SPAN_METRIC] = [ser({"service_name": "demo-go-api", "span_kind": "SPAN_KIND_SERVER",
                                      "span_name": "POST /orders"}, old)]
        _, copy = sc.raw_read(r, sc.FIXED_METRIC, {"job": sp.COPY_JOB}, t1, t2, sc.COPY_EXPIRY_S)
        _, gw = sc.raw_read(r, sc.SPAN_METRIC, {}, t1, t2, sc.GATEWAY_EXPIRY_S)
        self.assertEqual(sc.copy_counts(copy, t1, t2)["demo-go-api"].count, 4.0)
        self.assertEqual(sc.gateway_families(gw, t1, t2)["demo-go-api"]["server:POST /orders"], 104.0)


class ClusterScope(unittest.TestCase):
    def test_vps_series_never_enters_a_read(self):
        r = FakeReader()
        r.raw[sc.FIXED_METRIC] = shift(FIX["copy"], T0)
        r.raw[sc.SPAN_METRIC] = shift(FIX["gateway"], T0)
        q, copy = sc.raw_read(r, sc.FIXED_METRIC, {"job": sp.COPY_JOB}, T0, T0 + 90, sc.COPY_EXPIRY_S)
        self.assertIn('cluster="k8s01"', q)
        self.assertTrue(all(s["metric"]["cluster"] == "k8s01" for s in copy))
        counts = sc.copy_counts(copy, T0, T0 + 90)
        self.assertEqual(counts["demo-go-api"].count, 90.0)   # the vps +99999 is not in it
        _, gw = sc.raw_read(r, sc.SPAN_METRIC, {}, T0, T0 + 90, sc.GATEWAY_EXPIRY_S)
        fams = sc.gateway_families(gw, T0, T0 + 90)
        self.assertEqual(fams["demo-go-api"]["server:POST /orders"], 25.0)  # 20 + born 5; vps excluded


class Families(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(sc.fixed_family("SPAN_KIND_SERVER", "POST /orders"), "server:POST /orders")
        self.assertEqual(sc.fixed_family("SPAN_KIND_CONSUMER", "process demo:go:orders"),
                         "consumer:process demo:go:orders")
        self.assertEqual(sc.fixed_family("SPAN_KIND_INTERNAL", "demo.order.accept"), "demo:demo.order.accept")
        self.assertEqual(sc.fixed_family("SPAN_KIND_CLIENT", "demo.v1.OrderService/PlaceOrder"),
                         "rpc-client:demo.v1.OrderService/PlaceOrder")
        for kind, name in (("SPAN_KIND_CLIENT", "XADD"), ("SPAN_KIND_CLIENT", "HTTP POST"),
                           ("SPAN_KIND_INTERNAL", "demo.drive"), ("SPAN_KIND_CLIENT", "sql.conn.exec")):
            self.assertIsNone(sc.fixed_family(kind, name), name)

    def test_gateway_sums_over_service_version_and_skips_non_fixed(self):
        fams = sc.gateway_families(FIX["gateway"], 0, 60)
        self.assertNotIn("XADD", json.dumps(fams))
        self.assertEqual(fams["demo-go-api"]["rpc-client:demo.v1.OrderService/PlaceOrder"], 20.0)
        self.assertNotIn("demo:demo.drive", fams.get("demo-go-worker", {}))


# ── bounds and verdicts ──

CELL_OK = "lang:go,route:orders,scen:ok,class:none"
CELL_BE = "lang:go,route:orders,scen:business_error,class:none"


def k6_counts(table: dict[str, tuple[int, int, int]]) -> dict[str, int]:
    out = {}
    for cell, (e, u, n) in table.items():
        for outcome, v in (("expected", e), ("unexpected", u), ("noanswer", n)):
            out["soak_requests{" + cell + f",outcome:{outcome}}}"] = v
    return out


class Bounds(unittest.TestCase):
    def setUp(self):
        self.cells = sc.cell_table(k6_counts({CELL_OK: (10, 0, 2), CELL_BE: (3, 1, 0),
                                              "lang:python,route:orders,scen:ok,class:none": (50, 0, 0)}))
        self.k = {CELL_OK: 5, CELL_BE: 3}

    def test_parse_key(self):
        self.assertEqual(sc.parse_key("soak_requests{lang:go,route:orders,scen:ok,class:s00,phase:pre,outcome:noanswer}"),
                         ("lang:go,route:orders,scen:ok,class:s00,phase:pre", "noanswer"))
        self.assertIsNone(sc.parse_key("http_reqs"))
        self.assertIsNone(sc.parse_key("soak_requests{lang:go}"))

    def test_interval_and_other_languages_ignored(self):
        b = sc.bounds(self.cells, self.k, "go")
        self.assertEqual((b.lo, b.hi, b.unexpected), (10 * 5 + 4 * 3, 12 * 5 + 4 * 3, 1))

    def test_verdicts(self):
        b = sc.bounds(self.cells, self.k, "go")
        self.assertEqual(sc.judge_bound(62, b), "PASS")
        self.assertEqual(sc.judge_bound(72, b), "PASS")   # no-answer requests that made spans
        self.assertEqual(sc.judge_bound(61, b), "LOSS")
        self.assertEqual(sc.judge_bound(73, b), "DUPLICATES")

    def test_missing_k_is_unqualified(self):
        b = sc.bounds(self.cells, {CELL_OK: 5}, "go")
        self.assertEqual(b.missing, [CELL_BE])
        self.assertEqual(sc.judge_bound(50, b), "UNQUALIFIED")


def gates(copy=True, shared=True):
    return {"copy": {"clean": copy, "reasons": [] if copy else ["x"]},
            "shared": {"clean": shared, "reasons": [] if shared else ["y"]}}


class CountVerdicts(unittest.TestCase):
    def setUp(self):
        self.cells = sc.cell_table(k6_counts({CELL_OK: (18, 0, 0)}))
        self.kcal = {"services": {"demo-go-api": {"qualified": True, "k": {CELL_OK: 5}},
                                  "demo-go-backend": {"qualified": True, "k": {CELL_OK: 2}},
                                  "demo-go-worker": {"qualified": False, "cause": "x not integral"}},
                     "families": {"demo-go-api": {"server:POST /orders": {CELL_OK: 1},
                                                  "producer:send demo:go:orders": {CELL_OK: 1}}}}
        self.copy = sc.copy_counts(FIX["copy"], 0, 90)
        self.fams = sc.gateway_families(FIX["gateway"], 0, 60)

    def test_copy_primary_and_unset_and_gateway(self):
        out = sc.count_verdicts(self.copy, self.fams, self.cells, self.kcal, gates(), sp.demo_services(["go"]))
        self.assertEqual(out["demo-go-api"]["verdict"], "PASS")          # 90 in [90, 90]
        self.assertEqual(out["demo-go-backend"]["verdict"], "UNQUALIFIED")
        self.assertIn("unset", out["demo-go-backend"]["finding"])
        self.assertEqual(out["demo-go-worker"]["verdict"], "UNQUALIFIED")
        self.assertIn("not integral", out["demo-go-worker"]["reason"])
        gw = out["demo-go-api"]["gateway"]
        self.assertEqual(gw["server:POST /orders"]["verdict"], "DUPLICATES")  # 25 > 18
        self.assertEqual(gw["producer:send demo:go:orders"]["verdict"], "LOSS")  # 7 < 18

    def test_dirty_copy_sentinel_unqualifies_copy_only(self):
        out = sc.count_verdicts(self.copy, self.fams, self.cells, self.kcal, gates(copy=False),
                                sp.demo_services(["go"]))
        self.assertEqual(out["demo-go-api"]["verdict"], "UNQUALIFIED")
        self.assertNotEqual(out["demo-go-api"]["gateway"]["server:POST /orders"]["verdict"], "UNQUALIFIED")

    def test_dirty_shared_sentinel_unqualifies_gateway_only(self):
        out = sc.count_verdicts(self.copy, self.fams, self.cells, self.kcal, gates(shared=False),
                                sp.demo_services(["go"]))
        self.assertEqual(out["demo-go-api"]["verdict"], "PASS")
        self.assertEqual(out["demo-go-api"]["gateway"]["server:POST /orders"]["verdict"], "UNQUALIFIED")

    def test_window_problem_and_no_answers(self):
        out = sc.count_verdicts(self.copy, self.fams, self.cells, self.kcal, gates(), ["demo-go-api"],
                                {"go": ["next drive inside"]})
        self.assertEqual(out["demo-go-api"]["verdict"], "UNQUALIFIED")
        self.assertIn("window", out["demo-go-api"]["reason"])
        self.assertEqual(out["demo-go-api"]["gateway"]["server:POST /orders"]["verdict"], "UNQUALIFIED")
        empty = sc.cell_table(k6_counts({CELL_OK: (0, 0, 0)}))
        out = sc.count_verdicts(self.copy, self.fams, empty, self.kcal, gates(), ["demo-go-api"])
        self.assertEqual(out["demo-go-api"]["verdict"], "UNQUALIFIED")   # no vacuous PASS (M3)
        self.assertIn("no answered request", out["demo-go-api"]["reason"])

    def test_unset_outside_the_window_is_a_finding_only(self):
        copy = sc.copy_counts(FIX["copy"], 60, 90)   # the unset series has no sample in (60, 90]
        self.assertFalse(copy["demo-go-backend"].unset)
        self.assertTrue(copy["demo-go-backend"].unset_outside)
        kcal = {"services": {"demo-go-backend": {"qualified": True, "k": {CELL_OK: 0}}}}
        out = sc.count_verdicts(copy, {}, self.cells, kcal, gates(), ["demo-go-backend"])
        self.assertIn("outside the window", out["demo-go-backend"]["finding"])
        self.assertEqual(out["demo-go-backend"]["verdict"], "PASS")

    def test_ambiguous_interval_in_the_bound(self):
        # The copy count is [80, 95] after an ambiguous reappearance: it overlaps [90, 90].
        c = sc.CopyCount("demo-go-api", count=80.0, count_hi=95.0, instances={"a": 80.0}, instances_hi={"a": 95.0})
        out = sc.count_verdicts({"demo-go-api": c}, {"demo-go-api": {"server:POST /orders": sc.Counted(10, 18)}},
                                self.cells, self.kcal, gates(), ["demo-go-api"])
        self.assertEqual(out["demo-go-api"]["verdict"], "PASS")
        self.assertEqual(out["demo-go-api"]["counted_hi"], 95.0)
        self.assertIn("interval", out["demo-go-api"]["ambiguous"])
        self.assertEqual(out["demo-go-api"]["gateway"]["server:POST /orders"]["verdict"], "PASS")
        self.assertEqual(out["demo-go-api"]["gateway"]["server:POST /orders"]["counted_hi"], 18.0)
        b = sc.bounds(self.cells, {CELL_OK: 5}, "go")
        self.assertEqual(sc.judge_bound(sc.Counted(80, 89), b), "LOSS")      # even the upper is below
        self.assertEqual(sc.judge_bound(sc.Counted(91, 99), b), "DUPLICATES")
        text = sc.render_report("r", {}, {"count": sc.rows_for("count", {"services": out})})
        self.assertIn("80..95 (ambiguous: expiry or update)", text)
        shut = sc.shut_verdict("a", sc.Counted(3, 9), sc.cell_table(k6_counts({PRE: (2, 0, 0), POST: (0, 0, 0)})),
                               {CELL_OK: 4}, "go", deleted_at=T0, finished_at=T0 + 1, grace_s=30, exit_code=0)
        self.assertEqual((shut["counted_hi"], shut["verdict"]), (9.0, "PASS"))   # [3, 9] overlaps [8, 8]

    def test_drain_with_ambiguous_sides(self):
        fams = {"demo-go-api": {"producer:send demo:go:orders": sc.Counted(6, 8)},
                "demo-go-worker": {"consumer:process demo:go:orders": sc.Counted(7, 7)}}
        out = sc.drain_check(fams, ["go"])["go"]
        self.assertEqual((out["verdict"], out["ambiguous"]), ("PASS", True))
        fams["demo-go-worker"]["consumer:process demo:go:orders"] = sc.Counted(9, 9)
        self.assertEqual(sc.drain_check(fams, ["go"])["go"]["verdict"], "FAIL")

    def test_per_instance_breakdown(self):
        self.assertEqual(self.copy["demo-go-api"].instances, {"c32245f7-8517-423c-accb-b8d72ed6d80e": 90.0})
        self.assertTrue(self.copy["demo-go-backend"].unset)


class Drain(unittest.TestCase):
    def test_send_equals_process(self):
        fams = sc.gateway_families(FIX["gateway"], 0, 60)
        out = sc.drain_check(fams, ["go", "python"])
        self.assertEqual((out["go"]["send"], out["go"]["process"], out["go"]["verdict"]), (7.0, 7.0, "PASS"))
        self.assertEqual(out["python"]["verdict"], "NA")
        fams["demo-go-worker"]["consumer:process demo:go:orders"] = 6.0
        self.assertEqual(sc.drain_check(fams, ["go"])["go"]["verdict"], "FAIL")

    def test_t1_go_rule(self):
        workers = {"go": {"startedAt": T0, "first_drive": None}, "python": {"first_drive": T0 + 50},
                   "node": {"first_drive": T0 + 20}}
        t1, per = sc.window_t1(workers, 120)
        self.assertEqual(per["go"], T0 + 120)          # startedAt + quiesce, not a drive
        self.assertEqual(t1, T0 + 170)
        with self.assertRaises(ValueError):
            sc.window_t1({"dotnet": {"startedAt": T0}}, 120)

    def test_window_between_drives(self):
        workers = {"go": {"startedAt": T0}, "python": {"first_drive": T0 + 50}}
        self.assertEqual(sc.next_drive_ok(workers, 10800, T0 + 7200), [])
        bad = sc.next_drive_ok(workers, 10800, T0 + 10900)
        self.assertEqual([b.split(":")[0] for b in bad], ["go", "python"])
        # t2 + quiesce (review M2): a drive inside the quiesce after t2 counts
        self.assertEqual(sc.next_drive_ok({"go": {"startedAt": T0}}, 10800, T0 + 10700), [])
        self.assertEqual(len(sc.next_drive_ok({"go": {"startedAt": T0}}, 10800, T0 + 10700, quiesce=120)), 1)

    def test_no_vacuous_drain_pass(self):
        cells = sc.cell_table(k6_counts({CELL_OK: (5, 0, 0)}))
        self.assertEqual(sc.drain_check({}, ["go"], cells)["go"]["verdict"], "FAIL")   # ok traffic, no send
        self.assertEqual(sc.drain_check({}, ["go"], {})["go"]["verdict"], "NA")

    def test_window_problems_and_worker_state(self):
        pods = [{"metadata": {"name": "demo-go-worker-1a-x", "namespace": sp.APP_NS},
                 "status": {"containerStatuses": [{"name": "worker", "restartCount": 1,
                                                   "state": {"running": {"startedAt": sp.iso(T0)}}}]}},
                {"metadata": {"name": "demo-go-api-1a-x", "namespace": sp.APP_NS}, "status": {}},
                {"metadata": {"name": "demo-python-worker-1a-x", "namespace": sp.APP_NS},
                 "status": {"containerStatuses": [{"name": "worker", "restartCount": 0,
                                                   "state": {"running": {"startedAt": sp.iso(T0 - 50)}}}]}}]
        live = sc.worker_state(pods, ["go", "python"])
        self.assertEqual(live["go"], {"pod": "demo-go-worker-1a-x", "startedAt": T0, "restartCount": 1})
        workers = {"go": {"startedAt": T0, "restartCount": 0}, "python": {"first_drive": T0 + 10, "startedAt": T0 - 50}}
        p = sc.window_problems(workers, live, T0 + 200, T0 + 3600, quiesce=120, interval=10800,
                               langs=["go", "python", "node"])
        self.assertIn("restarted", p["go"][0])
        self.assertEqual(p["python"], [])
        self.assertIn("no worker input", p["node"][0])
        p = sc.window_problems(workers, live, T0 + 60, T0 + 3600, quiesce=120, interval=10800, langs=["python"])
        self.assertIn("before the t1 rule", p["python"][0])
        p = sc.window_problems({"dotnet": {}}, {}, T0, T0 + 1, quiesce=0, interval=10, langs=["dotnet"])
        self.assertIn("first demo.drive", p["dotnet"][0])
        p = sc.window_problems({"python": {"first_drive": T0}}, {}, T0 + 10, T0 + 20, quiesce=0, interval=10800,
                               langs=["python"])
        self.assertIn("no Running worker", p["python"][0])
        p = sc.window_problems({"python": {"first_drive": T0}}, {}, T0 + 10, T0 + 20, quiesce=0, interval=10800,
                               langs=["python"], when="at t2")
        self.assertEqual(p["python"], ["no Running worker pod at t2"])

    def test_parse_ts_without_an_offset_is_utc(self):
        self.assertEqual(sp.parse_ts("2026-11-26T12:00:00"), sp.parse_ts("2026-11-26T12:00:00Z"))
        self.assertEqual(sp.parse_ts("2026-11-26T12:00:00+01:00"), sp.parse_ts("2026-11-26T11:00:00Z"))
        self.assertIsNone(sp.parse_ts("not a time"))
        self.assertIsNone(sp.parse_ts(None))

    def test_recorded_workers(self):
        """Defect 2: soak.py run's workers.json (start + t2); the flat legacy
        format and a missing or failed t2 record fall back to the live pods."""
        start = {"go": {"startedAt": T0, "restartCount": 0}}
        t2 = {"go": {"pod": "p", "startedAt": T0, "restartCount": 0}}
        w, live, at_t2, src = sc.recorded_workers({"start": {"at": T0, "workers": start},
                                                   "t2": {"at": T0 + 9, "workers": t2}})
        self.assertEqual((w, live, at_t2), (start, t2, True))
        self.assertTrue(src.startswith("recorded at t2 (2026-"), src)
        self.assertEqual(sc.recorded_workers({"start": {"workers": start}, "t2": {"workers": t2}})[3],
                         "recorded at t2 (--workers)")
        w, live, at_t2, src = sc.recorded_workers(start)
        self.assertEqual((w, live, at_t2), (start, None, False))
        self.assertIn("fallback: no t2 record", src)
        w, live, at_t2, src = sc.recorded_workers({"start": {"at": T0, "workers": start},
                                                   "t2": {"at": T0, "error": "boom"}})
        self.assertEqual((w, live, at_t2), (start, None, False))
        self.assertIn("t2 record has no workers: boom", src)
        self.assertEqual(sc.recorded_workers({"start": {"at": T0, "error": "boom"}})[:3], ({}, None, False))
        self.assertEqual(sc.recorded_workers(None), ({}, None, False, "live worker pods at count time (no --workers)"))
        for bad in ([1], {"start": [1]}, "x"):
            with self.assertRaises(ValueError):
                sc.recorded_workers(bad)


# ── sentinels ──


class Sentinels(unittest.TestCase):
    def test_scoped_gates(self):
        v = [sp.CounterVerdict("copy:x", "copy", "CLEAN"), sp.CounterVerdict("agent:y", "shared", "NOISY", 3)]
        g = sc.scoped_gates(v, {"copy:queue t1": (True, ""), "shared:gateway restart": (False, "rolled")})
        self.assertTrue(g["copy"]["clean"])
        self.assertFalse(g["shared"]["clean"])
        self.assertEqual(len(g["shared"]["reasons"]), 2)
        g = sc.scoped_gates([sp.CounterVerdict("copy:x", "copy", "ABSENT")], {"copy:queue t2": (False, "size 3")})
        self.assertFalse(g["copy"]["clean"])

    def test_queue_check(self):
        r = FakeReader()
        labels = {"exporter": "otlp", "job": sp.COPY_JOB}
        r.instant = [("queue_size", [inst(labels, 0), inst({**labels, "cluster": "vps"}, 99)]),
                     ("queue_capacity", [inst(labels, 1000)])]
        self.assertTrue(sc.queue_check(r, sp.COPY_JOB, T0, zero=True)[0])
        r.instant[0] = ("queue_size", [inst(labels, 150)])
        self.assertFalse(sc.queue_check(r, sp.COPY_JOB, T0, zero=True)[0])
        self.assertFalse(sc.queue_check(r, sp.GATEWAY_JOB, T0, zero=False)[0])   # 15 % > 10 %
        r.instant[0] = ("queue_size", [inst(labels, 50)])
        self.assertTrue(sc.queue_check(r, sp.GATEWAY_JOB, T0, zero=False)[0])
        r.instant = []
        self.assertFalse(sc.queue_check(r, sp.COPY_JOB, T0, zero=True)[0])

    def test_gateway_restart(self):
        def pod(started):
            return {"metadata": {"namespace": "opentelemetry", "name": "otel-gateway-collector-7cc44695b6-7pj59"},
                    "status": {"phase": "Running", "containerStatuses": [
                        {"name": "otc-container", "restartCount": 0, "state": {"running": {"startedAt": started}}}]}}
        up = [ser({"job": sp.GATEWAY_JOB}, [(T0 + 10, 100), (T0 + 40, 130)])]
        self.assertTrue(sc.gateway_restart([pod(sp.iso(T0 - 600))], up, T0, T0 + 60)[0])
        self.assertFalse(sc.gateway_restart([pod(sp.iso(T0 + 5))], up, T0, T0 + 60)[0])
        self.assertFalse(sc.gateway_restart([], up, T0, T0 + 60)[0])
        drop = [ser({"job": sp.GATEWAY_JOB}, [(T0 + 10, 100), (T0 + 40, 3)])]
        self.assertFalse(sc.gateway_restart([pod(sp.iso(T0 - 600))], drop, T0, T0 + 60)[0])
        self.assertFalse(sc.gateway_restart([pod(sp.iso(T0 - 600))], [], T0, T0 + 60)[0])
        self.assertFalse(sc.gateway_restart([pod(sp.iso(T0 - 600))], [ser({}, [(T0 - 5, 1)])], T0, T0 + 60)[0])


# ── K-shut ──

PRE = "lang:go,route:orders,scen:ok,class:none,phase:pre"
POST = "lang:go,route:orders,scen:ok,class:none,phase:post"


class Shut(unittest.TestCase):
    def setUp(self):
        self.cells = sc.cell_table(k6_counts({PRE: (100, 0, 2), POST: (40, 0, 3)}))
        self.k = {CELL_OK: 4}
        self.exit = {"deleted_at": T0, "finished_at": T0 + 4, "grace_s": 30, "exit_code": 0}

    def test_bounds_with_no_answer_in_the_upper_bound(self):
        v = sc.shut_verdict("old", 400, self.cells, self.k, "go", **self.exit)
        self.assertEqual((v["lo"], v["hi"], v["verdict"]), (400, 400 + 160 + 20, "PASS"))
        self.assertIn("unknown", v["post_basis"])
        self.assertEqual(sc.shut_verdict("old", 399, self.cells, self.k, "go", **self.exit)["verdict"], "LOSS")
        self.assertEqual(sc.shut_verdict("old", 581, self.cells, self.k, "go", **self.exit)["verdict"], "DUPLICATES")

    def test_post_split_per_cell_and_scalar(self):
        v = sc.shut_verdict("old", 420, self.cells, self.k, "go", post_before_finish={POST: 5}, **self.exit)
        self.assertEqual(v["hi"], 400 + 20 + 20)
        self.assertIn("per-cell", v["post_basis"])
        cells = sc.cell_table(k6_counts({PRE: (100, 0, 0), POST: (40, 0, 0),
                                         "lang:go,route:orders,scen:business_error,class:none,phase:post": (5, 0, 0)}))
        kmap = {CELL_OK: 4, CELL_BE: 6}
        v = sc.shut_verdict("old", 400, cells, kmap, "go", post_before_finish=5, **self.exit)
        self.assertEqual(v["hi"], 400 + 5 * 6)                 # the largest post k, named
        self.assertIn("largest post k 6", v["post_basis"])

    def test_exit_rules(self):
        base = dict(deleted_at=T0, grace_s=30)
        self.assertEqual(sc.exit_verdict(T0, T0 + 4, 30, 0)["verdict"], "PASS")
        self.assertEqual(sc.exit_verdict(T0, T0 + 29, 30, 0)["verdict"], "FAIL")       # >= grace - 1 s
        self.assertEqual(sc.exit_verdict(T0, T0 + 3, 30, 137)["verdict"], "FAIL")      # SIGKILL
        self.assertEqual(sc.exit_verdict(T0, T0 - 1, 30, 0)["verdict"], "FAIL")
        # finishedAt is whole seconds: a prompt exit reads just before deleted_at.
        self.assertEqual(sc.exit_verdict(T0, T0 - 0.04, 30, 0)["verdict"], "PASS")
        self.assertEqual(sc.exit_verdict(T0, T0 - 0.99, 30, 0)["verdict"], "PASS")
        self.assertEqual(sc.exit_verdict(None, T0, 30, 0)["verdict"], "UNQUALIFIED")
        self.assertEqual(sc.exit_verdict(T0, T0 + 1, None, 0)["verdict"], "UNQUALIFIED")
        # the exit is folded into the verdict
        v = sc.shut_verdict("old", 400, self.cells, self.k, "go", finished_at=T0 + 40, exit_code=0, **base)
        self.assertEqual((v["count_verdict"], v["exit"]["verdict"], v["verdict"]), ("PASS", "FAIL", "FAIL"))
        v = sc.shut_verdict("old", 400, self.cells, self.k, "go")
        self.assertEqual(v["verdict"], "UNQUALIFIED")                                 # no times
        v = sc.shut_verdict("old", 400, self.cells, None, "go", **self.exit)
        self.assertEqual(v["verdict"], "REPORT-ONLY")

    def test_other_states(self):
        self.assertEqual(sc.shut_verdict("unset", 1, self.cells, self.k, "go", **self.exit)["verdict"], "UNQUALIFIED")
        self.assertEqual(sc.shut_verdict("old", 1, self.cells, {"other": 1}, "go", **self.exit)["verdict"],
                         "UNQUALIFIED")
        v = sc.shut_verdict("old", 400, self.cells, self.k, "go", copy_gate={"clean": False, "reasons": ["q"]},
                            **self.exit)
        self.assertEqual(v["verdict"], "UNQUALIFIED")
        self.assertIn("copy sentinels", v["reason"])
        none = sc.cell_table(k6_counts({PRE: (0, 0, 0), POST: (3, 0, 0)}))
        self.assertEqual(sc.shut_verdict("old", 0, none, self.k, "go", **self.exit)["verdict"], "UNQUALIFIED")


# ── K-cal ──


def rep(cell, n, copy, gateway=None, noanswer=0, unset=()):
    return {"cell": cell, "counts": {"expected": n, "unexpected": 0, "noanswer": noanswer},
            "copy": copy, "gateway": gateway or {}, "unset": list(unset)}


class Calibration(unittest.TestCase):
    def test_integral_and_stable(self):
        out = sc.calibrate([rep(CELL_OK, 100, {"demo-go-api": 500, "demo-go-backend": 200},
                                {"demo-go-api": {"server:POST /orders": 100}}),
                            rep(CELL_OK, 50, {"demo-go-api": 250, "demo-go-backend": 100},
                                {"demo-go-api": {"server:POST /orders": 50}})])
        self.assertEqual(out["services"]["demo-go-api"], {"qualified": True, "k": {CELL_OK: 5}, "cause": None})
        self.assertEqual(out["families"]["demo-go-api"]["server:POST /orders"], {CELL_OK: 1})
        # a service missing from a repeat is a cause, not k = 0 (review L3)
        self.assertIn("no copy delta for demo-go-worker", out["services"]["demo-go-worker"]["cause"])
        out = sc.calibrate([rep(CELL_OK, 10, {"demo-go-worker": 0}), rep(CELL_OK, 10, {"demo-go-worker": 0})])
        self.assertEqual(out["services"]["demo-go-worker"]["k"], {CELL_OK: 0})

    def test_non_integral_names_the_varying_family(self):
        out = sc.calibrate([rep(CELL_OK, 100, {"demo-go-api": 512},
                                {"demo-go-api": {"server:POST /orders": 100, "rpc-client:a.B/C": 112}}),
                            rep(CELL_OK, 100, {"demo-go-api": 500},
                                {"demo-go-api": {"server:POST /orders": 100, "rpc-client:a.B/C": 100}})])
        st = out["services"]["demo-go-api"]
        self.assertFalse(st["qualified"])
        self.assertIn("not integral", st["cause"])
        self.assertIn("rpc-client:a.B/C", st["cause"])
        self.assertEqual(out["excluded_families"]["demo-go-api"], ["rpc-client:a.B/C"])

    def test_non_integral_without_a_family_cause(self):
        out = sc.calibrate([rep(CELL_OK, 100, {"demo-go-api": 512}), rep(CELL_OK, 100, {"demo-go-api": 512})])
        self.assertIn("no gateway family varies", out["services"]["demo-go-api"]["cause"])

    def test_unstable_noanswer_single_unset(self):
        out = sc.calibrate([rep(CELL_OK, 100, {"demo-go-api": 500}), rep(CELL_OK, 100, {"demo-go-api": 600})])
        self.assertIn("differs across repeats", out["services"]["demo-go-api"]["cause"])
        out = sc.calibrate([rep(CELL_OK, 100, {"demo-go-api": 500}, noanswer=1), rep(CELL_OK, 100, {"demo-go-api": 500})])
        self.assertIn("no answer", out["services"]["demo-go-api"]["cause"])
        out = sc.calibrate([rep(CELL_OK, 100, {"demo-go-api": 500})])
        self.assertIn("need 2", out["services"]["demo-go-api"]["cause"])
        out = sc.calibrate([rep(CELL_OK, 100, {"demo-go-api": 500}, unset=["demo-go-api"]),
                            rep(CELL_OK, 100, {"demo-go-api": 500})])
        self.assertIn("unset", out["services"]["demo-go-api"]["cause"])
        out = sc.calibrate([rep(CELL_OK, 0, {}), rep(CELL_OK, 0, {})])
        self.assertIn("N = 0", out["services"]["demo-go-api"]["cause"])

    def test_absent_family_is_k0_in_a_measured_cell(self):
        """Defect 3 (K8b k8b-20261125): a family counted in one cell and absent
        from both repeats of another valid cell gets k = 0 there; an invalid,
        single-repeat, unanswered or unmeasured cell gets none."""
        send = "producer:send demo:go:orders"
        ok_gw = {"demo-go-api": {"server:POST /orders": 10, send: 10}, "demo-go-worker": {"consumer:x": 10}}
        be_gw = {"demo-go-api": {"server:POST /orders": 10}}
        copy = {"demo-go-api": 20, "demo-go-backend": 0, "demo-go-worker": 10}
        cells = {k: f"lang:go,route:orders,scen:{k},class:none" for k in ("db_error", "unhandled", "x1", "x2")}
        reps = [rep(CELL_OK, 10, copy, ok_gw), rep(CELL_OK, 10, copy, ok_gw),
                rep(CELL_BE, 10, copy, be_gw), rep(CELL_BE, 10, copy, be_gw),
                rep(cells["db_error"], 10, copy, be_gw),                                       # one repeat
                rep(cells["unhandled"], 10, copy, be_gw), rep(cells["unhandled"], 10, copy, be_gw, noanswer=1),
                rep(cells["x2"], 10, copy, be_gw), rep(cells["x2"], 10, copy, be_gw)]
        del reps[-1]["gateway"]                                                             # not measured
        reps.append({**rep(cells["x2"], 10, copy), "gateway": None})
        out = sc.calibrate(reps, invalid=[{"cell": cells["x1"], "reasons": ["window invalid"]}],
                           planned=[CELL_OK, CELL_BE, *cells.values()])
        api = out["families"]["demo-go-api"]
        self.assertEqual(api[send], {CELL_OK: 1, CELL_BE: 0})
        self.assertNotIn(cells["unhandled"], api[send])   # a repeat with a no-answer request: not measured
        self.assertEqual(out["families"]["demo-go-worker"], {"consumer:x": {CELL_OK: 1, CELL_BE: 0}})
        # the cross-check with k = 0: exact 0 passes, a span in the error cell is DUPLICATES
        table = sc.cell_table(k6_counts({CELL_OK: (7, 0, 0), CELL_BE: (3, 0, 0)}))
        b = sc.bounds(table, api[send], "go")
        self.assertEqual((b.lo, b.hi, b.missing), (7, 7, []))
        self.assertEqual(sc.judge_bound(7.0, b), "PASS")
        self.assertEqual(sc.judge_bound(8.0, b), "DUPLICATES")
        # review M1: empty gateway reads (the driver writes "gateway": {}) are not a measured absence
        empty = [rep(CELL_OK, 10, copy, ok_gw), rep(CELL_OK, 10, copy, ok_gw),
                 rep(CELL_BE, 10, copy, {}), rep(CELL_BE, 10, copy, {"demo-go-api": {"server:POST /orders": 0}})]
        out = sc.calibrate(empty)
        self.assertEqual(out["families"]["demo-go-api"][send], {CELL_OK: 1})
        # s00 (unsampled inbound): the series are present with a delta of 0 -> k = 0 is recorded
        s00 = "lang:go,route:orders,scen:business_error,class:s00"
        zero = {"demo-go-api": {"server:POST /orders": 0}, "demo-go-backend": {"server:a.B/C": 0}}
        out = sc.calibrate([rep(CELL_OK, 10, copy, ok_gw), rep(CELL_OK, 10, copy, ok_gw),
                            rep(s00, 10, copy, zero), rep(s00, 10, copy, zero)])
        self.assertEqual(out["families"]["demo-go-api"][send], {CELL_OK: 1, s00: 0})
        self.assertEqual(out["families"]["demo-go-api"]["server:POST /orders"], {CELL_OK: 1, s00: 0})
        # round 2: series present at 0 are evidence only in a class:s00 cell; a sampled cell needs a count > 0
        out = sc.calibrate([rep(CELL_OK, 10, copy, ok_gw), rep(CELL_OK, 10, copy, ok_gw),
                            rep(CELL_BE, 10, copy, zero), rep(CELL_BE, 10, copy, zero)])
        self.assertEqual(out["families"]["demo-go-api"][send], {CELL_OK: 1})
        self.assertEqual(out["families"]["demo-go-api"]["server:POST /orders"], {CELL_OK: 1, CELL_BE: 0})
        # the service's own series missing, a sibling of the same language present: counts (K-D12 sibling rule)
        sib = {"demo-go-backend": {"server:a.B/C": 10}}
        out = sc.calibrate([rep(CELL_OK, 10, copy, ok_gw), rep(CELL_OK, 10, copy, ok_gw),
                            rep(CELL_BE, 10, copy, sib), rep(CELL_BE, 10, copy, sib)])
        self.assertEqual(out["families"]["demo-go-api"][send], {CELL_OK: 1, CELL_BE: 0})
        self.assertEqual(out["families"]["demo-go-worker"]["consumer:x"], {CELL_OK: 1, CELL_BE: 0})
        other = [rep(CELL_OK, 10, copy, ok_gw), rep(CELL_OK, 10, copy, ok_gw),   # another language's evidence
                 rep(CELL_BE, 10, copy, {"demo-node-api": {"x": 5}}), rep(CELL_BE, 10, copy, be_gw)]
        self.assertNotIn(CELL_BE, sc.calibrate(other)["families"]["demo-go-api"][send])
        # a family ambiguous in a cell is not "absent" there, and is excluded anyway
        amb = [rep(CELL_OK, 10, copy, ok_gw), rep(CELL_OK, 10, copy, ok_gw),
               {**rep(CELL_BE, 10, copy, be_gw), "gateway_ambiguous": {"demo-go-api": [send]}},
               rep(CELL_BE, 10, copy, be_gw)]
        out = sc.calibrate(amb)
        self.assertNotIn(send, out["families"]["demo-go-api"])
        self.assertIn(send, out["excluded_families"]["demo-go-api"])
        # present in one repeat only: the existing rule (0 vs 1 differ: excluded for that cell)
        one = [rep(CELL_OK, 10, copy, ok_gw), rep(CELL_OK, 10, copy, ok_gw),
               rep(CELL_BE, 10, copy, ok_gw), rep(CELL_BE, 10, copy, be_gw)]
        out = sc.calibrate(one)
        self.assertEqual(out["families"]["demo-go-api"][send], {CELL_OK: 1})
        self.assertIn(send, out["excluded_families"]["demo-go-api"])

    def test_rate_cap(self):
        out = sc.calibrate([rep(CELL_OK, 10, {"demo-go-api": 50}), rep(CELL_OK, 10, {"demo-go-api": 50})],
                           rate=6, drain={"go": 5.0, "node": 4.0})
        self.assertEqual(out["rate_cap"]["go"]["verdict"], "PASS")      # 6 x 0.7 = 4.2 <= 5
        self.assertEqual(out["rate_cap"]["node"]["verdict"], "FAIL")    # 4.2 > 4


# ── K8a slope rules ──


def step_then_plateau(start: float, hours: float, base: float = 60e6, step_mi: float = 4, steps_until_h: float = 12,
                      plateau_mi_per_h: float = 0.08) -> list[tuple[float, float]]:
    """RSS sampled every 60 s: a 4 Mi step every 2 h up to 12 h, then flat with a
    0.08 Mi/h residual (the pre-soak evidence of S-D5, 2026-11-23)."""
    pts = []
    t = start
    while t <= start + hours * H:
        h = (t - start) / H
        steps = min(int(h // 2) + 1, int(steps_until_h // 2))
        v = base + steps * step_mi * 2**20
        if h > steps_until_h:
            v += (h - steps_until_h) * plateau_mi_per_h * 2**20
        v += ((int(t) // 60) % 7 - 3) * 1024  # small noise
        pts.append((t, v))
        t += 60
    return pts


class Slopes(unittest.TestCase):
    def test_step_then_plateau_passes_after_the_12h_warmup(self):
        pts = step_then_plateau(T0, 30)
        v = sc.judge_slope(pts, T0 + 12 * H, T0 + 30 * H)
        self.assertEqual(v["verdict"], "PASS", v)
        self.assertFalse(v["ratio_over_1_10"])

    def test_same_shape_fails_with_a_1h_warmup(self):
        pts = step_then_plateau(T0, 30)
        self.assertEqual(sc.judge_slope(pts, T0 + 1 * H, T0 + 19 * H)["verdict"], "FAIL")

    def test_linear_leak_fails(self):
        pts = [(T0 + i * 60, 100e6 + i * 60 / H * 2e6) for i in range(30 * 60)]   # 2 % per hour
        self.assertEqual(sc.judge_slope(pts, T0 + 12 * H, T0 + 30 * H)["verdict"], "FAIL")

    def test_step_in_the_second_half_fails(self):
        pts = [(T0 + i * 60, 100.0 if i < 26 * 60 else 112.0) for i in range(30 * 60)]
        v = sc.judge_slope(pts, T0 + 12 * H, T0 + 30 * H)
        self.assertEqual(v["verdict"], "FAIL")
        self.assertGreater(v["second_half_slope_per_h"], v["limit_per_h"])

    def test_nodata(self):
        self.assertEqual(sc.judge_slope([(T0, 0.0), (T0 + H, 0.0)], T0, T0 + H)["verdict"], "NODATA")

    # Owner ruling 2026-11-23: integer gauges, limit max(1 %/h x median, 2 / span_h).
    @staticmethod
    def threads(steps_at_h: list[float], base: float = 5.0) -> list[tuple[float, float]]:
        return [(T0 + i * 60, base + sum(1 for s in steps_at_h if i * 60 >= s * H)) for i in range(18 * 60 + 1)]

    def test_integer_single_step_passes(self):
        for at in (4.5, 9.0, 13.5):   # in either half, at the worst place (the middle)
            v = sc.judge_slope(self.threads([at]), T0, T0 + 18 * H)
            self.assertEqual(v["verdict"], "PASS", (at, v))
            self.assertTrue(v["integer"])
            self.assertAlmostEqual(v["limit_per_h"], 2 / 18)
            self.assertAlmostEqual(v["second_half_limit_per_h"], 2 / 9)

    def test_integer_two_steps_fail(self):
        self.assertEqual(sc.judge_slope(self.threads([6, 12]), T0, T0 + 18 * H)["verdict"], "FAIL")
        self.assertEqual(sc.judge_slope(self.threads([11, 15]), T0, T0 + 18 * H)["verdict"], "FAIL")

    def test_integer_sustained_rise_fails(self):
        for every in (6, 4, 2):
            steps = [h for h in range(every, 18, every)]
            self.assertEqual(sc.judge_slope(self.threads(steps), T0, T0 + 18 * H)["verdict"], "FAIL", every)

    def test_non_integer_span_has_no_absolute_floor(self):
        pts = [(t, v + 0.5) for t, v in self.threads([9])]
        v = sc.judge_slope(pts, T0, T0 + 18 * H)
        self.assertFalse(v["integer"])
        self.assertEqual(v["verdict"], "FAIL")   # 1 % of 5.5 = 0.055/h

    def test_queue_burst_passes_on_15min_medians(self):
        # a 7 min burst inside one 15 min bin, late in the second half
        queue = [(T0 + i * 60, 60.0 if 1020 <= i < 1027 else 0.0) for i in range(18 * 60 + 1)]
        self.assertEqual(sc.judge_slope(queue, T0, T0 + 18 * H)["verdict"], "FAIL")   # raw fit
        v = sc.judge_slope(queue, T0, T0 + 18 * H, median_bins=True)
        self.assertEqual(v["verdict"], "PASS", v)
        self.assertTrue(v["median_bins"])
        self.assertIn("threadpool_queue", sc.MEDIAN_BIN_RULES)

    def test_slope_helper(self):
        self.assertIsNone(sc.slope_per_hour([(T0, 1.0)]))
        self.assertIsNone(sc.slope_per_hour([(T0, 1.0), (T0, 2.0)]))
        self.assertAlmostEqual(sc.slope_per_hour([(0, 0.0), (3600, 5.0)]), 5.0)


def passive_reader(judged: tuple[float, float], leak_cid: str | None = None) -> FakeReader:
    r = FakeReader()
    hours = int((judged[1] - judged[0]) / H)
    for rule, metric, _m, rlangs, keys in sc.PASSIVE_METRICS:   # full coverage per hour
        want = 3600 / sc.rule_interval(rule)
        r.ranges.append((r"count_over_time\(" + re.escape(metric), [
            ser({keys[0]: f"demo-{lang}-{app}-6f7d-x1"}, [(judged[0] + (h + 1) * H, want) for h in range(hours)])
            for lang in (rlangs or verify.LANGS) for app in verify.DEMO_APPS]))
    for rule, metric, _m, rlangs, keys in (*sc.PASSIVE_METRICS, sc.CROSSCHECK):
        ans = []
        for lang in rlangs or verify.LANGS:
            for app in verify.DEMO_APPS:
                pts = [(judged[0] + i * 600, 100.0 + (i * 50 if f"{lang}-{app}" == leak_cid else 0))
                       for i in range(int((judged[1] - judged[0]) / 600) + 1)]
                ans.append(ser({keys[0]: f"demo-{lang}-{app}-6f7d-x1", keys[1]: app}, pts))
        r.ranges.append((re.escape(metric), ans))
    return r


class Passive(unittest.TestCase):
    def test_verdicts_per_container(self):
        judged = (T0 + 12 * H, T0 + 30 * H)
        r = passive_reader(judged, leak_cid="dotnet-worker")
        series, queries = sc.passive_series(r, *judged)
        self.assertTrue(all('cluster="k8s01"' in q for q in queries))
        fds = {f"{lang}-{app}": [("p1", judged[0] + i * 60, 30.0) for i in range(1080)]
               for lang in verify.LANGS for app in verify.DEMO_APPS}
        rest = {f"{lang}-{app}": 0.0 for lang in verify.LANGS for app in verify.DEMO_APPS}
        rest["go-api"] = 1.0
        out = sc.passive_verdicts(series, fds, rest, judged, verify.LANGS)
        self.assertEqual(out["dotnet-worker"]["verdict"], "FAIL")
        self.assertEqual(out["go-api"]["rules"]["restarts"]["verdict"], "FAIL")
        self.assertEqual(out["python-api"]["verdict"], "PASS")
        self.assertTrue(out["python-api"]["rules"]["python_fds"]["report_only"])
        self.assertEqual(out["node-api"]["rules"]["runtime"]["verdict"], "n/a")
        self.assertNotIn("goroutines", out["node-api"]["rules"])
        self.assertEqual(out["node-api"]["verdict"], "PASS")

    def test_fd_coverage_per_read_that_ran(self):
        # Re-review HIGH-2: reads of 70 s (period 70 s, about 51 per hour) with every
        # FD sample successful is full coverage, not 51/60.
        judged = (T0, T0 + 18 * H)
        reads = [T0 + i * 70 for i in range(int(18 * H / 70))]
        pts = [("p1", t, 20.0) for t in reads]
        sh = sc.fd_shares(pts, reads, *judged)
        self.assertTrue(sh["ok"], sh)
        self.assertEqual(set(sh["hours"]), {1.0})
        # hours with no read are left to the heartbeat rule, not judged here
        some = [t for t in reads if not 3 * H <= t - T0 < 5 * H]
        sh = sc.fd_shares([("p1", t, 20.0) for t in some], some, *judged)
        self.assertTrue(sh["ok"], sh)
        self.assertIsNone(sh["hours"][3])
        # one failed exec in eight reads of an hour: 7/8 < 95 %
        bad = [p for i, p in enumerate(pts) if i % 8]
        self.assertFalse(sc.fd_shares(bad, reads, *judged)["ok"])
        self.assertFalse(sc.fd_shares([], [], *judged)["ok"])     # no read at all
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "guard.jsonl"
            self.assertEqual(sc.fd_reads(p), [])
            p.write_text("\n".join([json.dumps({"fd": {"at": T0, "containers": {}}}), "x",
                                     json.dumps({"phase": "entry"}), json.dumps({"fd": "bad"}),
                                     json.dumps({"fd": {"at": T0 + 70, "error": "pod list"}})]) + "\n")
            self.assertEqual(sc.fd_reads(p), [T0, T0 + 70])

    def test_several_pods_rule_on_fds(self):
        judged = (T0, T0 + 18 * H)
        fds = {"go-api": [("p1", T0 + i * 600, 20.0) for i in range(54)] +
               [("p2", T0 + i * 600 + 300, 20.0) for i in range(54, 108)]}
        out = sc.passive_verdicts({}, fds, {}, judged, ["go"])
        self.assertEqual(out["go-api"]["rules"]["fds"]["verdict"], "FAIL")
        self.assertIn("several pods", out["go-api"]["rules"]["fds"]["reason"])

    def test_coverage_inside_the_judged_span(self):
        # Review H4: a 6 h hole in a runtime series makes the container INVALID.
        judged = (T0, T0 + 18 * H)
        full = [(T0 + (h + 1) * H, 360) for h in range(18)]
        holed = [(t, 0 if 6 <= i < 12 else n) for i, (t, n) in enumerate(full)]
        self.assertTrue(sc.hourly_shares(full, *judged, 10)["ok"])
        sh = sc.hourly_shares(holed, *judged, 10)
        self.assertFalse(sh["ok"])
        self.assertEqual(sh["low_hours"], [6, 7, 8, 9, 10, 11])
        # one hour at 90 %, total above 95 %: still a gap (any hour)
        one = [(t, 324 if i == 3 else n) for i, (t, n) in enumerate(full)]
        self.assertFalse(sc.hourly_shares(one, *judged, 10)["ok"])
        r = passive_reader(judged)
        cad = [ser({"pod": f"demo-go-{a}-6f7d-x1"}, [(t, 240) for t, _ in full]) for a in verify.DEMO_APPS]
        gor = [ser({"k8s_pod_name": f"demo-go-{a}-6f7d-x1"}, holed if a == "api" else full) for a in verify.DEMO_APPS]
        r.ranges = [("count_over_time\\(container", cad), ("count_over_time\\(go_goroutine", gor)] + r.ranges
        fds = {f"go-{a}": [("p1", T0 + i * 60, 20.0) for i in range(18 * 60)] for a in verify.DEMO_APPS}
        reads = [T0 + i * 60 for i in range(18 * 60)]
        cov, q = sc.passive_coverage(r, *judged, fds, ["go"], reads=reads)
        self.assertTrue(all('cluster="k8s01"' in x for x in q))
        self.assertTrue(cov["go-backend"]["goroutines"]["ok"])
        self.assertTrue(cov["go-backend"]["rss"]["ok"])
        self.assertTrue(cov["go-backend"]["fds"]["ok"])
        self.assertFalse(cov["go-api"]["goroutines"]["ok"])
        series, _ = sc.passive_series(r, *judged)
        rest = {f"go-{a}": 0.0 for a in verify.DEMO_APPS}
        out = sc.passive_verdicts(series, fds, rest, judged, ["go"], cov)
        self.assertEqual(out["go-api"]["verdict"], "INVALID")
        self.assertIn("platform: telemetry gap", out["go-api"]["reason"])
        self.assertEqual(out["go-backend"]["verdict"], "PASS")
        # FD exec failures for 6 h while the reads ran: a gap
        fds["go-worker"] = [p for p in fds["go-worker"] if not 6 * H <= p[1] - T0 < 12 * H]
        cov, _ = sc.passive_coverage(r, *judged, fds, ["go"], reads=reads)
        self.assertFalse(cov["go-worker"]["fds"]["ok"])
        self.assertEqual(cov["go-worker"]["fds"]["low_hours"], [6, 7, 8, 9, 10, 11])
        rest["go-worker"] = 1.0
        out = sc.passive_verdicts(series, fds, rest, judged, ["go"], cov)
        self.assertEqual(out["go-worker"]["verdict"], "FAIL")    # a restart is FAIL, not INVALID

    def test_coverage_partial_last_bin(self):
        # K8a review 2026-11-25 LOW-1: 10 h 30 min judged left its last 30 min out.
        self.assertEqual(sc.coverage_bins(T0, T0 + 10.5 * H), (10, 1800.0, [3600.0] * 10 + [1800.0]))
        self.assertEqual(sc.coverage_bins(T0, T0 + 10 * H + 300),
                         (10, 300.0, [3600.0] * 9 + [3900.0]))              # short tail folded in
        self.assertEqual(sc.coverage_bins(T0, T0 + 18 * H), (18, 0.0, [3600.0] * 18))
        self.assertEqual(sc.coverage_bins(T0, T0 + 1200), (0, 1200.0, [1200.0]))
        judged = (T0, T0 + 10.5 * H)
        full = [(T0 + (h + 1) * H, 360) for h in range(10)]
        sh = sc.hourly_shares(full, *judged, 10, tail=180)
        self.assertTrue(sh["ok"], sh)
        self.assertEqual(len(sh["hours"]), 11)
        self.assertEqual(sh["hours"][-1], 1.0)
        sh = sc.hourly_shares(full, *judged, 10)                    # tail missing
        self.assertFalse(sh["ok"])
        self.assertEqual(sh["low_hours"], [10])
        sh = sc.hourly_shares(full, *judged, 10, tail=90)            # tail at 50 %
        self.assertEqual(sh["low_hours"], [10])
        # a range point at the span's end is never routed into the tail bin (review LOW-2)
        sh = sc.hourly_shares(full + [(judged[1], 180)], *judged, 10, tail=180)
        self.assertEqual(sh["hours"][-1], 1.0)
        # a 5 min tail is folded into the last hour, and must be read (review LOW-1)
        short = (T0, T0 + 10 * H + 300)
        sh = sc.hourly_shares(full, *short, 10)
        self.assertEqual(len(sh["hours"]), 10)
        self.assertEqual(sh["low_hours"], [9])
        self.assertTrue(sc.hourly_shares(full, *short, 10, tail=30)["ok"])
        # passive_coverage reads the tail with an instant query at the end
        r = passive_reader((T0, T0 + 10 * H))
        tail = [inst({"pod": f"demo-go-{a}-6f7d-x1"}, 1800 / sc.CADVISOR_SCRAPE_S, judged[1])
                for a in verify.DEMO_APPS]
        r.instant.append((r"count_over_time\(container_memory_rss.*\[1800s\]", tail))
        reads = [T0 + i * 60 for i in range(int(10.5 * 60))]
        fds = {f"go-{a}": [("p1", t, 20.0) for t in reads] for a in verify.DEMO_APPS}
        cov, q = sc.passive_coverage(r, *judged, fds, ["go"], reads=reads)
        self.assertTrue(any("[1800s]" in x and "instant" in x for x in q), q)
        self.assertTrue(cov["go-api"]["rss"]["ok"], cov["go-api"]["rss"])
        self.assertFalse(cov["go-api"]["goroutines"]["ok"])          # no tail answer
        self.assertEqual(cov["go-api"]["goroutines"]["low_hours"], [10])
        self.assertTrue(cov["go-api"]["fds"]["ok"])
        self.assertEqual(len(cov["go-api"]["fds"]["hours"]), 11)
        # a span under 1 h issues no range query (start + 3600 > end; review LOW-3)
        r = passive_reader((T0, T0 + 10 * H))
        r.instant.append((r"count_over_time\(container_memory_rss.*\[1200s\]", [
            inst({"pod": f"demo-go-{a}-6f7d-x1"}, 1200 / sc.CADVISOR_SCRAPE_S, T0 + 1200) for a in verify.DEMO_APPS]))
        cov, q = sc.passive_coverage(r, T0, T0 + 1200, {}, ["go"], reads=[])
        self.assertFalse(any("range" in x for x in q), q)
        self.assertTrue(cov["go-api"]["rss"]["ok"], cov["go-api"]["rss"])
        self.assertEqual(cov["go-api"]["rss"]["hours"], [1.0])

    def test_coverage_recorded_on_pass(self):
        # K8a review 2026-11-25 MEDIUM-2: a PASS carries its coverage evidence.
        judged = (T0, T0 + 18 * H)
        r = passive_reader(judged)
        series, _ = sc.passive_series(r, *judged)
        reads = [T0 + i * 60 for i in range(18 * 60)]
        fds = {f"go-{a}": [("p1", t, 20.0) for t in reads] for a in verify.DEMO_APPS}
        cov, _ = sc.passive_coverage(r, *judged, fds, ["go"], reads=reads)
        rest = {f"go-{a}": 0.0 for a in verify.DEMO_APPS}
        out = sc.passive_verdicts(series, fds, rest, judged, ["go"], cov)
        self.assertEqual(out["go-api"]["verdict"], "PASS")
        for rule in ("rss", "working_set", "threads", "goroutines", "fds"):
            self.assertTrue(out["go-api"]["rules"][rule]["coverage"]["ok"], rule)
        self.assertNotIn("coverage", out["go-api"]["rules"]["restarts"])

    def test_nodata_and_several_pods(self):
        judged = (T0, T0 + 18 * H)
        series = {"rss": {"go-api": [("p1", "api", [(T0 + i * 600, 1.0) for i in range(50)]),
                                     ("p2", "api", [(T0 + i * 600, 1.0) for i in range(50)])]}}
        out = sc.passive_verdicts(series, {}, {}, judged, ["go"])
        self.assertEqual(out["go-api"]["rules"]["rss"]["verdict"], "FAIL")
        self.assertEqual(out["go-backend"]["verdict"], "NODATA")

    def test_restarts_rule(self):
        r = FakeReader()
        start, end = T0, T0 + 30 * H
        r.raw["kube_pod_container_status_restarts_total"] = [
            ser({"pod": "demo-go-api-1-x", "container": "api"}, [(start - 30, 2), (start + 600, 2), (end - 60, 3)]),
            ser({"pod": "demo-go-backend-1-x", "container": "backend"}, [(start - 30, 0), (end - 60, 0)]),
            # a kube-state-metrics restart: a new series of the same pod is not a restart
            ser({"pod": "demo-go-backend-1-x", "container": "backend", "instance": "10.0.0.2:8080"},
                [(start + 3600, 0), (end - 30, 0)]),
            ser({"pod": "demo-go-worker-2-y", "container": "worker"}, [(start + 600, 1), (end - 60, 1)]),
            ser({"pod": "other-pod"}, [(start, 0)]),
            ser({"pod": "demo-node-api-1-x", "cluster": "vps"}, [(start, 0), (end - 60, 9)]),
        ]
        out, q = sc.restarts(r, start, end)
        self.assertEqual(out, {"go-api": 1.0, "go-backend": 0.0, "go-worker": 2.0})   # a new pod is a restart (M5)
        self.assertIn('cluster="k8s01"', q)

    def test_coverage_gate(self):
        r = FakeReader()
        lookback = 1800
        r.instant = [("increase", [inst({"service_name": s}, 10) for s in sp.demo_services(["go", "node"])
                                   if s != "demo-node-worker"]),
                     ("count_over_time\\(go_goroutine", [
                         inst({"k8s_pod_name": "demo-go-api-1-x"}, 60), inst({"k8s_pod_name": "demo-go-backend-1-x"}, 57),
                         inst({"k8s_pod_name": "demo-go-worker-1-x"}, 30)])]
        out = sc.coverage(r, T0, lookback, ["go", "node"], interval=30)
        self.assertEqual(out["verdict"], "FAIL")
        self.assertEqual(out["span_metrics"]["demo-node-worker"], "FAIL")
        self.assertEqual(out["runtime"]["go-api:goroutines"]["verdict"], "PASS")
        self.assertEqual(out["runtime"]["go-backend:goroutines"]["verdict"], "PASS")   # 57/60 = 0.95
        self.assertEqual(out["runtime"]["go-worker:goroutines"]["verdict"], "FAIL")
        self.assertEqual(out["runtime"]["node:runtime"]["verdict"], "n/a")
        self.assertTrue(all('cluster="k8s01"' in q for q in out["queries"]))


# ── FD sweep ──


def demo_pod(name, phase="Running", deleting=False, container="api"):
    meta = {"name": name, "namespace": sp.APP_NS}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-23T00:00:00Z"
    return {"metadata": meta, "spec": {"containers": [{"name": container}]}, "status": {"phase": phase}}


class FdSweepTests(unittest.TestCase):
    def test_reads_every_running_demo_container(self):
        pods = [demo_pod("demo-go-api-1a-x"), demo_pod("demo-go-backend-1a-x", container="backend"),
                demo_pod("demo-go-worker-1a-x", deleting=True), demo_pod("demo-node-api-1a-x"),
                demo_pod("demo-python-api-1a-x", phase="Pending"), demo_pod("soak-agent-1-x")]
        calls = []

        def exec_read(pod, container):
            calls.append((pod, container))
            if pod.startswith("demo-go-backend"):
                return "ls: cannot access\n"
            return "".join(f"{i}\n" for i in range(23))

        sweep = sc.FdSweep(lambda: pods, exec_read, lambda: T0, langs=("go",))
        out = sweep.read()
        self.assertEqual(sorted(calls), [("demo-go-api-1a-x", "api"), ("demo-go-backend-1a-x", "backend")])
        self.assertEqual(out["containers"]["go-api"]["fds"], 23)
        self.assertIn("not an FD listing", out["containers"]["go-backend"]["error"])
        self.assertEqual(out["missing"], ["go-worker"])
        self.assertEqual(out["at"], T0)

    def test_parse_listing(self):
        self.assertEqual(sc.parse_fd_listing("0\n1\n2\n"), (3, None))
        self.assertEqual(sc.parse_fd_listing("0\n")[0], 1)               # one FD named "0"
        self.assertIn("implausible", sc.parse_fd_listing("")[1])           # 0 FDs
        self.assertIn("implausible", sc.parse_fd_listing("\n \n")[1])
        self.assertIn("not an FD listing", sc.parse_fd_listing("0")[1] or sc.parse_fd_listing("x\n")[1])

    def test_count_zero_is_an_error_not_zero(self):
        # "0" as a wc-style count must never pass as a count of zero FDs
        out = sc.FdSweep(lambda: [demo_pod("demo-go-api-1a-x")], lambda p, c: "", lambda: T0, langs=("go",)).read()
        self.assertNotIn("fds", out["containers"]["go-api"])
        self.assertIn("implausible", out["containers"]["go-api"]["error"])

    def test_failures_are_recorded(self):
        def boom(pod, container):
            raise RuntimeError("exec timed out")
        out = sc.FdSweep(lambda: [demo_pod("demo-go-api-1a-x")], boom, lambda: T0, langs=("go",)).read()
        self.assertIn("exec timed out", out["containers"]["go-api"]["error"])

        def nolist():
            raise RuntimeError("no pods")
        self.assertIn("pod list", sc.FdSweep(nolist, boom, lambda: T0).read()["error"])

    def test_fd_points_from_guard_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "guard.jsonl"
            self.assertEqual(sc.fd_points(p), {})
            p.write_text("\n".join([
                json.dumps({"phase": "entry"}),
                json.dumps({"fd": {"at": T0, "containers": {"go-api": {"pod": "p1", "fds": 20},
                                                            "go-backend": {"error": "x"}}}}),
                "not json",
                json.dumps({"fd": {"at": T0 + 60, "containers": {"go-api": {"pod": "p1", "fds": 21}}}})]) + "\n")
            self.assertEqual(sc.fd_points(p), {"go-api": [("p1", T0, 20.0), ("p1", T0 + 60, 21.0)]})

    def test_loop_hook_wiring(self):
        import soak  # noqa: PLC0415 — the K-loop (testing/k8s/soak)
        kube = soak.Kube("k8s01", kubectl=["false"])
        for pod, container in (("soak-agent-1-x", "otelcol"), ("demo-go-api-1a-x", "API"),
                               ("demo-go-api-1a-x", "api; rm"), ("xdemo-go-api-1a-x", "api")):
            with self.assertRaises(soak.SoakError):
                kube.exec_fd(pod, container)
        sweep = kube.fd_sweep(["go"])
        self.assertIsInstance(sweep, sc.FdSweep)
        with mock.patch.object(soak.Kube, "_ok", return_value="0\n1\n2\n") as ok:
            self.assertEqual(kube.exec_fd("demo-go-api-1a-x", "api"), "0\n1\n2\n")
        self.assertEqual(ok.call_args.args[0],
                         ["exec", "demo-go-api-1a-x", "-c", "api", "--", "ls", "-1", "/proc/1/fd"])
        # a failed ls is a non-zero exit: kubectl's error, never a count
        with self.assertRaises(soak.SoakError):
            kube.exec_fd("demo-go-api-1a-x", "api")
        out = sc.FdSweep(lambda: [demo_pod("demo-go-api-1a-x")], kube.exec_fd, lambda: T0, langs=("go",)).read()
        self.assertIn("SoakError", out["containers"]["go-api"]["error"])


# ── report ──


class Report(unittest.TestCase):
    def test_scan_refuses_secrets_canary_token(self):
        canary = "ab" * 24
        self.assertEqual(sc.scan_report("clean counts only", canary), [])
        self.assertIn("canary", sc.scan_report(f"x {canary} y", canary))
        self.assertIn("hex48", sc.scan_report(f"x {canary} y", None))
        self.assertIn("fakeotlp token", sc.scan_report("Location: /parity-redirect-c/?t=parity-redirect-token", None))
        self.assertIn("userinfo", sc.scan_report("http://u:p@h", None))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r" / "x.md"
            with self.assertRaises(sc.ScanError):
                sc.write_report(p, f"leak {canary}", canary)
            self.assertFalse(p.exists())
            sc.write_report(p, "ok\n", canary)
            self.assertEqual(p.read_text(), "ok\n")

    def test_digest_is_not_a_canary_hit(self):
        self.assertEqual(sc.scan_report("img@sha256:" + "0" * 64, None), [])

    def test_render_sections(self):
        count = {"window": "(a, b]", "services": {"demo-go-api": {"verdict": "PASS", "counted": 90.0, "lo": 90.0,
                                                                  "hi": 92.0, "gateway": {"server:x": {
                                                                      "verdict": "PASS", "counted": 3, "lo": 3, "hi": 3}}},
                                                  "demo-go-backend": {"verdict": "UNQUALIFIED", "counted": 8,
                                                                      "finding": "unset", "reason": "D4"}},
                 "drain": {"go": {"send": 7, "process": 7, "verdict": "PASS"}}, "queries": ["q1"]}
        shut = {"apis": {"demo-go-api": {"service_instance_id": "old", "verdict": "PASS", "counted": 4, "lo": 4,
                                         "hi": 5, "exit": {"verdict": "PASS", "seconds": 2.0}}}}
        cal = {"services": {"demo-go-api": {"qualified": True, "k": {CELL_OK: 5}, "cause": None},
                            "demo-go-worker": {"qualified": False, "k": {}, "cause": "x"}},
               "rate_cap": {"go": {"enqueue_rate": 4.2, "drain_rate": 5, "verdict": "PASS"}}}
        passive = {"window": "w", "containers": {"go-api": {"verdict": "PASS", "rules": {
            "rss": {"verdict": "PASS", "samples": 10, "median": 1.0, "slope_per_h": 0.0},
            "runtime": {"verdict": "n/a", "reason": "AG12"}}}}}
        gate = {"span_metrics": {"demo-go-api": "PASS"}, "runtime": {"go-api:goroutines": {"share": 1.0, "verdict": "PASS"}}}
        sections = {n: sc.rows_for(n, d) for n, d in (("count", count), ("shut", shut), ("cal", cal),
                                                     ("passive", passive), ("gate", gate))}
        text = sc.render_report("r1", {"commit": "abc", "tag": "soak/r1", "digests": ["img@sha256:" + "1" * 64]},
                                sections)
        for needle in ("# Soak run `r1`", "| demo-go-api copy | PASS | counted 90 in [90, 92] |",
                       "Finding: demo-go-backend: unset", "drain go", "rate cap go", "`q1`", "soak/r1",
                       "go-api rss", "AG12", "span metrics demo-go-api", "QUALIFIED", "EXCLUDED"):
            self.assertIn(needle, text)
        self.assertEqual(sc.scan_report(text, None), [])



def _deploy(name, image):
    app = name.rsplit("-", 1)[-1]
    return {"metadata": {"name": name}, "spec": {"selector": {"matchLabels": {"app": name}},
            "template": {"spec": {"containers": [{"name": app, "image": image}]}}}}


def _pod(name, image_id, phase="Running", app=None, cname=None):
    app = app or name.rsplit("-", 2)[0]
    cname = cname or app.rsplit("-", 1)[-1]
    return {"metadata": {"name": name, "labels": {"app": app}},
            "status": {"phase": phase, "containerStatuses": [{"name": cname, "imageID": image_id}]}}


def _live(tag="t1", langs=("go",), skip=(), pods_extra=()):
    deps, pods = [], list(pods_extra)
    for lang in langs:
        for app in verify.DEMO_APPS:
            name = f"demo-{lang}-{app}"
            if name in skip:
                continue
            deps.append(_deploy(name, f"reg.example.com/otel-libs-{name}:{tag}"))
            pods.append(_pod(f"{name}-abc-1", f"reg.example.com/otel-libs-{name}@sha256:" + "a" * 64))
    return {"deploy": {"items": deps}, "pods": {"items": pods}}


class ReportProvenance(unittest.TestCase):
    """K11 fix: the soaked commit comes from the tag (or state.json), HEAD only
    as "generated at"; digests from snapshot.json, else live only at the run's tag."""

    @unittest.skipUnless(sc._git("rev-parse", "--git-dir"), "needs a git checkout")
    def test_git_helper(self):
        self.assertRegex(sc._git("rev-parse", "HEAD") or "", r"^[0-9a-f]{40}$")
        self.assertIsNone(sc._git("rev-list", "-n1", "refs/tags/soak/no-such-run-k11", "--"))

    def test_commit_from_the_tag(self):
        with mock.patch.object(sc, "_git", return_value="1404f9c1f8b16400649b4af03c25a9e58f85b852"):
            self.assertEqual(sc.run_commit("soak/r1", {}),
                             ("1404f9c", "`git rev-parse refs/tags/soak/r1^{commit}`", []))

    def test_commit_tag_and_state_disagree(self):
        with mock.patch.object(sc, "_git", return_value="1404f9c1f8b16400649b4af03c25a9e58f85b852"):
            commit, _, notes = sc.run_commit("soak/r1", {"commit": "e5f8e4b"})
        self.assertEqual(commit, "1404f9c")
        self.assertIn("e5f8e4b", notes[0])
        with mock.patch.object(sc, "_git", return_value="1404f9c1f8b16400649b4af03c25a9e58f85b852"):
            self.assertEqual(sc.run_commit("soak/r1", {"commit": "1404f9c"})[2], [])

    def test_tag_not_found_falls_back_to_state(self):
        with mock.patch.object(sc, "_git", return_value=None) as g:
            commit, source, notes = sc.run_commit("soak/gone", {"commit": "abcdef0123", "dirty": True})
        self.assertEqual((commit, source), ("abcdef0", "recorded in state.json"))
        self.assertIn("tag `soak/gone` not found", notes)
        self.assertIn("dirty", notes[1])
        self.assertEqual(g.call_args.args, ("rev-parse", "--verify", "-q", "refs/tags/soak/gone^{commit}"))

    def test_head_label_marks_dirty_tooling(self):
        with mock.patch.object(sc, "_git", side_effect=["abc1234", " M testing/k8s/soakcount.py"]):
            self.assertEqual(sc.head_label(), "abc1234+dirty")
        with mock.patch.object(sc, "_git", side_effect=["abc1234", None]):
            self.assertEqual(sc.head_label(), "abc1234")

    def test_commit_from_state_without_tag(self):
        with mock.patch.object(sc, "_git", return_value=None) as g:
            self.assertEqual(sc.run_commit("none", {"commit": "1d683b2aaaa"}), ("1d683b2", "recorded in state.json", []))
            g.assert_not_called()
            commit, source, notes = sc.run_commit("soak/gone", {})
        self.assertIsNone(commit)
        self.assertIsNone(source)
        self.assertIn("not found", notes[0])

    def _digests(self, live, tag="t1", langs=("go",)):
        def kubectl(ctx, args):
            if isinstance(live, Exception):
                raise live
            return live[args[0]]
        with mock.patch.object(verify, "kubectl_json", side_effect=kubectl):
            return sc.live_digests("k8s01", "dev-otel-libs", tag, langs)

    def test_image_repo(self):
        self.assertEqual(sc.image_repo("reg.example.com/a/b:t1"), "reg.example.com/a/b")
        self.assertEqual(sc.image_repo("reg.example.com:5000/a/b@sha256:" + "a" * 64), "reg.example.com:5000/a/b")
        self.assertEqual(sc.image_repo("redis:7"), "docker.io/library/redis")
        self.assertEqual(sc.image_repo("library/redis:7"), "docker.io/library/redis")
        self.assertEqual(sc.image_repo("other.example.com/development/otel-libs-demo-go-api@sha256:" + "a" * 64),
                         "other.example.com/development/otel-libs-demo-go-api")

    def test_live_digests_at_the_run_tag(self):
        out, source = self._digests(_live())
        self.assertEqual(len(out), 3)
        self.assertTrue(all(sp.IMAGE_DIGEST_RE.match(d) for d in out), out)
        self.assertIn("not recorded at run time", source)
        self.assertIn("`t1`", source)

    def test_live_digests_refused_on_another_tag(self):
        out, source = self._digests(_live(tag="t2"))
        self.assertEqual(out, [])
        self.assertTrue(source.startswith("not recorded"), source)
        self.assertIn("t2", source)

    def test_live_digests_not_recorded_cases(self):
        self.assertTrue(self._digests(_live(), tag=None)[1].startswith("not recorded"))
        self.assertIn("missing", self._digests(_live(skip=("demo-go-api",)))[1])
        self.assertIn("live read failed", self._digests(verify.QueryError("boom"))[1])
        self.assertIn("live read failed", self._digests(FileNotFoundError("kubectl"))[1])
        two = _live(pods_extra=[_pod("demo-go-api-abc-2", "reg.example.com/otel-libs-demo-go-api@sha256:" + "b" * 64)])
        self.assertIn("no single running digest", self._digests(two)[1])
        pending = _live(pods_extra=[_pod("demo-go-api-abc-2", "reg.example.com/x@sha256:" + "b" * 64, "Pending")])
        self.assertEqual(len(self._digests(pending)[0]), 3)
        # another app's pod, another container name or another repository never counts
        # same last segment, another registry: not the Deployment's repository
        other_reg = _live(pods_extra=[_pod("demo-go-api-abc-6", "mirror.example.com/otel-libs-demo-go-api@sha256:" + "9" * 64)])
        self.assertEqual(len(self._digests(other_reg)[0]), 3)
        noise = _live(pods_extra=[_pod("demo-go-api-abc-3", "reg.example.com/x@sha256:" + "c" * 64, app="demo-go-apix"),
                                  _pod("demo-go-api-abc-4", "reg.example.com/otel-libs-demo-go-api@sha256:" + "d" * 64,
                                       cname="sidecar"),
                                  _pod("demo-go-api-abc-5", "reg.example.com/istio-proxy@sha256:" + "e" * 64,
                                       app="demo-go-api", cname="api")])
        out, _ = self._digests(noise)
        self.assertEqual(len(out), 3, out)
        self.assertTrue(all(d.endswith("a" * 64) for d in out), out)

    def test_render_commit_and_generated_at(self):
        text = sc.render_report("r1", {"commit": "1404f9c", "commit_source": "`git rev-list -n1 soak/r1`",
                                       "commit_notes": ["n1"], "tag": "soak/r1", "generated_at": "7252420",
                                       "generated": "2026-11-26T00:00:00Z", "digests": [],
                                       "digests_source": "not recorded (x)"}, {})
        self.assertIn("- Commit: `1404f9c` (`git rev-list -n1 soak/r1`); tag: `soak/r1`", text)
        self.assertIn("- Note: n1", text)
        self.assertIn("at HEAD `7252420`", text)
        self.assertIn("- Image digests: not recorded (x)", text)
        self.assertIn("- Commit: `not recorded`", sc.render_report("r1", {}, {}))

    def test_action_report_without_snapshot_reads_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "state.json").write_text(json.dumps({"tag": "t1", "langs": ["go"]}))
            args = parse(["soak", "report", "--run", "r1", "--tag", "soak/r1", "--report", str(d / "r.md"),
                          "--out-dir", str(d)])
            with mock.patch.object(sc, "_git", return_value="e5f8e4be967d82ec4bb17e6fd7821977fb0ab0d1"), \
                    mock.patch.object(verify, "kubectl_json", side_effect=lambda c, a: _live()[a[0]]), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(sc.action_report(args, d), 0)
            text = (d / "r.md").read_text()
        self.assertIn("- Commit: `e5f8e4b`", text)
        self.assertIn("from the live demo Deployments", text)
        self.assertIn("@sha256:" + "a" * 64, text)
        self.assertIn("re-push of the tag since the run is not excluded", text)

    def test_action_report_prefers_state_digests_and_its_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "state.json").write_text(json.dumps({"tag": "t1", "langs": ["go"], "context": "ctx-x",
                                                      "namespace": "ns-x"}))
            args = parse(["soak", "report", "--run", "r1", "--tag", "none", "--report", str(d / "r.md"),
                          "--out-dir", str(d)])
            seen = []

            def kubectl(ctx, a):
                seen.append((ctx, a[2]))
                return _live()[a[0]]
            with mock.patch.object(verify, "kubectl_json", side_effect=kubectl), redirect_stdout(io.StringIO()):
                self.assertEqual(sc.action_report(args, d), 0)
            self.assertEqual(seen[0], ("ctx-x", "ns-x"))
            (d / "state.json").write_text(json.dumps({"tag": "t1", "digests_at": "2026-11-27T00:00:00Z",
                                                      "digests": {"demo-go-api": "r/x:t1@sha256:" + "f" * 64}}))
            with mock.patch.object(verify, "kubectl_json", side_effect=AssertionError("no live read")), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(sc.action_report(args, d), 0)
            text = (d / "r.md").read_text()
        self.assertIn("recorded in state.json at the run start (2026-11-27T00:00:00Z)", text)
        self.assertIn("f" * 64, text)

    def test_extras_candidate_rows_are_labelled_heuristic(self):
        sec = sc.rows_for("extras", {"rows": [["parity api metric m", "FAIL", "have go; candidate gaps row: X"]]})
        self.assertEqual(sec["rows"][0][2], "have go; heuristic candidate gaps row: X")

    def test_scan_is_case_insensitive(self):
        canary = "ab" * 24
        self.assertIn("canary", sc.scan_report(canary.upper(), canary))
        self.assertIn("fakeotlp token", sc.scan_report("PARITY-REDIRECT-TOKEN", None))


# ── the CLI ──


def parse(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", default="k8s01")
    parser.add_argument("--langs", type=verify._langs, default=list(verify.LANGS))
    sub = parser.add_subparsers(dest="mode")
    sc.add_parser(sub)
    return parser.parse_args(argv)


def k6_log(counts: dict[str, int]) -> str:
    summary = {"metrics": {k: {"values": {"count": v}} for k, v in counts.items()}}
    return "noise\nSOAK_SUMMARY_BEGIN\n" + json.dumps(summary) + "\nSOAK_SUMMARY_END\n"


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_soak(self, argv, reader=None):
        args = parse(argv + ["--out-dir", str(self.d)])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = sc.main_soak(None, args, T0 + 7200, reader=reader or FakeReader())
        return rc, out.getvalue() + err.getvalue()

    def test_cal_count_shut_report(self):
        cal_in = self.d / "cal-in.json"
        deltas = {"demo-go-api": 50, "demo-go-backend": 20, "demo-go-worker": 10}
        cal_in.write_text(json.dumps({"repeats": [rep(CELL_OK, 10, deltas), rep(CELL_OK, 10, deltas)],
                                      "drain": {"go": 5.0}}))
        rc, out = self.run_soak(["--langs", "go", "soak", "cal", "--run", "r1", "--input", str(cal_in), "--rate", "1"])
        self.assertEqual(rc, 0, out)
        kcal = self.d / "soak-cal.json"
        self.assertTrue(kcal.exists())
        log = self.d / "k6.log"
        log.write_text(k6_log(k6_counts({CELL_OK: (18, 0, 0)})))
        r = FakeReader()
        t1, t2 = T0 - 90, T0
        r.raw[sc.FIXED_METRIC] = shift(FIX["copy"], t1)
        r.raw[sc.SPAN_METRIC] = shift(FIX["gateway"], t1)
        r.raw["otelcol_process_uptime"] = [ser({"job": sp.GATEWAY_JOB}, [(t1 + 10, 1), (t2, 100)])]
        r.instant = [("queue_size", [inst({"exporter": "e"}, 0)]), ("queue_capacity", [inst({"exporter": "e"}, 100)])]
        gw = {"metadata": {"namespace": "opentelemetry", "name": "otel-gateway-collector-7cc44695b6-7pj59"},
              "status": {"phase": "Running", "containerStatuses": [
                  {"name": "c", "restartCount": 0, "state": {"running": {"startedAt": sp.iso(t1 - 600)}}}]}}
        worker = {"metadata": {"namespace": sp.APP_NS, "name": "demo-go-worker-1a-x"},
                  "status": {"phase": "Running", "containerStatuses": [
                      {"name": "worker", "restartCount": 0, "state": {"running": {"startedAt": sp.iso(t1 - 600)}}}]}}
        dep = {"spec": {"template": {"spec": {"terminationGracePeriodSeconds": 30}}}}

        def kubectl(context, args):
            if args[0] == "deployment":
                if args[1] == "demo-go-api":
                    return dep
                raise verify.QueryError("not found")
            return {"items": [gw] if "opentelemetry" in args else [worker]}

        workers = self.d / "workers.json"
        workers.write_text(json.dumps({"go": {"startedAt": t1 - 600, "restartCount": 0}}))
        count_argv = ["--langs", "go", "soak", "count", "--run", "r1", "--t1", str(t1), "--t2", str(t2),
                      "--k6-log", str(log), "--kcal", str(kcal), "--workers", str(workers), "--quiesce", "60"]
        with mock.patch.object(sp, "read_sentinels", return_value=[[sp.CounterVerdict("c", "copy", "CLEAN")]]), \
                mock.patch.object(verify, "kubectl_json", side_effect=kubectl):
            rc, out = self.run_soak(count_argv, reader=r)
            wdata = json.loads((self.d / "soak-count.json").read_text())
            # without --workers every language's window is unusable: UNQUALIFIED, never PASS
            rc2, _ = self.run_soak(count_argv[:-4] + ["--quiesce", "60"], reader=r)
            nodata = json.loads((self.d / "soak-count.json").read_text())
            self.assertEqual(nodata["services"]["demo-go-api"]["verdict"], "UNQUALIFIED")
            self.assertEqual(rc2, 1)
            (self.d / "soak-count.json").write_text(json.dumps(wdata))
        data = json.loads((self.d / "soak-count.json").read_text())
        self.assertEqual(data["services"]["demo-go-api"]["verdict"], "PASS", out)   # 90 in [90, 90]
        self.assertEqual(data["services"]["demo-go-backend"]["verdict"], "UNQUALIFIED")
        self.assertTrue(data["gates"]["shared"]["clean"], data["gates"])
        self.assertEqual(rc, 1)   # backend and worker are not PASS
        self.assertIn("demo-go-backend: unset service_instance_id in the window", data["findings"])
        self.assertEqual(data["worker_window"], {"go": []})
        self.assertTrue(data["worker_source"].startswith("live worker pods"), data["worker_source"])
        # Defect 2: the workers recorded at the window start and at t2; the live
        # worker (replaced after t2, by the trap's restore) is not the check.
        moved = {"startedAt": t1 - 900, "restartCount": 0}
        workers.write_text(json.dumps({"start": {"at": t1, "workers": {"go": {**moved, "first_drive": None}}},
                                       "t2": {"at": t2, "workers": {"go": {"pod": "demo-go-worker-0-y", **moved}}}}))
        with mock.patch.object(sp, "read_sentinels", return_value=[[sp.CounterVerdict("c", "copy", "CLEAN")]]), \
                mock.patch.object(verify, "kubectl_json", side_effect=kubectl):
            self.run_soak(count_argv, reader=r)
            rec = json.loads((self.d / "soak-count.json").read_text())
            self.assertEqual(rec["worker_window"], {"go": []})
            self.assertTrue(rec["worker_source"].startswith("recorded at t2"), rec["worker_source"])
            workers.write_text(json.dumps({"go": moved}))   # the flat format: the live pod was replaced
            self.run_soak(count_argv, reader=r)
            rec = json.loads((self.d / "soak-count.json").read_text())
            self.assertIn("restarted or replaced", rec["worker_window"]["go"][0])
            # review M5: the t2 record itself shows a restart, or no Go worker at t2
            for t2w, why in (({"go": {"pod": "demo-go-worker-0-y", **moved, "restartCount": 1}},
                              "restarted or replaced"), ({}, "no Running worker pod at t2")):
                workers.write_text(json.dumps({"start": {"at": t1, "workers": {"go": {**moved}}},
                                               "t2": {"at": t2, "workers": t2w}}))
                self.run_soak(count_argv, reader=r)
                rec = json.loads((self.d / "soak-count.json").read_text())
                self.assertTrue(rec["worker_source"].startswith("recorded at t2"))
                self.assertIn(why, rec["worker_window"]["go"][0])
                self.assertEqual(rec["services"]["demo-go-api"]["verdict"], "UNQUALIFIED")
        # K-shut on the old instance
        shut_in = self.d / "shut.json"
        cells = {f"soak_requests{{lang:go,route:orders,scen:ok,class:none,phase:{ph},outcome:{o}}}": n
                 for ph, ns in (("pre", (18, 0, 0)), ("post", (0, 0, 0)))
                 for o, n in zip(("expected", "unexpected", "noanswer"), ns)}
        (self.d / "shut-k6.log").write_text(k6_log(cells))
        shut_in.write_text(json.dumps({"t1": t1, "t2": t2, "k6_log": str(self.d / "shut-k6.log"), "kcal": str(kcal),
                                       "apis": {"demo-go-api": {"old_instance": "c32245f7-8517-423c-accb-b8d72ed6d80e",
                                                                "deleted_at": T0, "finished_at": T0 + 3,
                                                                "exit_code": 0},
                                                "demo-go-backend": {"old_instance": "x", "deleted_at": T0,
                                                                    "finished_at": T0 + 3}}}))
        with mock.patch.object(sp, "read_sentinels", return_value=[[sp.CounterVerdict("c", "copy", "CLEAN")]]), \
                mock.patch.object(verify, "kubectl_json", side_effect=kubectl):
            rc, out = self.run_soak(["soak", "shut", "--run", "r1", "--input", str(shut_in)], reader=r)
        shut = json.loads((self.d / "soak-shut.json").read_text())
        self.assertEqual(shut["apis"]["demo-go-api"]["verdict"], "PASS", out)
        self.assertEqual(shut["apis"]["demo-go-api"]["exit"]["grace_s"], 30)         # from the Deployment
        self.assertEqual(shut["apis"]["demo-go-backend"]["exit"]["verdict"], "UNQUALIFIED")   # no grace read
        self.assertEqual(rc, 1)
        with mock.patch.object(sp, "read_sentinels", return_value=[[sp.CounterVerdict("c", "copy", "NOISY", 2)]]), \
                mock.patch.object(verify, "kubectl_json", side_effect=kubectl):
            self.run_soak(["soak", "shut", "--run", "r1", "--input", str(shut_in)], reader=r)
        self.assertEqual(json.loads((self.d / "soak-shut.json").read_text())["apis"]["demo-go-api"]["verdict"],
                         "UNQUALIFIED")
        # the report: the canary in a section is refused, a clean one is written
        report = self.d / "report.md"
        digest = "harbor.staffops.space/development/otel-libs-fakeotlp:ab24d24@sha256:" + "2" * 64
        (self.d / "snapshot.json").write_text(json.dumps({"images": [{"ref": digest}, {"n": 1}], "x": "y"}))
        rc, out = self.run_soak(["soak", "report", "--run", "r1", "--report", str(report)])
        self.assertEqual(rc, 0, out)
        text = report.read_text()
        self.assertIn(digest, text)
        self.assertIn("## count", text)
        self.assertIn("## shut", text)
        canary = "cd" * 24
        (self.d / "canary").write_text(canary + "\n")
        (self.d / "soak-gate.json").write_text(json.dumps({"span_metrics": {canary: "PASS"}, "runtime": {}}))
        rc, out = self.run_soak(["soak", "report", "--run", "r1", "--report", str(self.d / "r2.md")])
        self.assertEqual(rc, 1)
        self.assertIn("canary", out)
        self.assertFalse((self.d / "r2.md").exists())

    def test_cal_output_is_never_overwritten(self):
        """Defect 3: re-run the calibration on a recorded kcal-input.json into a
        new file; the original soak-cal.json and an existing --output stay."""
        cal_in = self.d / "kcal-input.json"
        deltas = {"demo-go-api": 50, "demo-go-backend": 20, "demo-go-worker": 10}
        gw = {"demo-go-api": {"producer:send demo:go:orders": 10}}
        be = {"demo-go-api": {"server:POST /orders": 10}}
        cal_in.write_text(json.dumps({"repeats": [rep(CELL_OK, 10, deltas, gw), rep(CELL_OK, 10, deltas, gw),
                                                  rep(CELL_BE, 10, deltas, be), rep(CELL_BE, 10, deltas, be)]}))
        (self.d / "soak-cal.json").write_text('{"original": true}')
        r2 = self.d / "soak-cal.r2.json"
        argv = ["--langs", "go", "soak", "cal", "--run", "r1", "--input", str(cal_in), "--output", str(r2)]
        rc, out = self.run_soak(argv)
        self.assertEqual(rc, 0, out)
        self.assertIn(str(r2), out)
        self.assertEqual(json.loads((self.d / "soak-cal.json").read_text()), {"original": True})
        fams = json.loads(r2.read_text())["families"]["demo-go-api"]["producer:send demo:go:orders"]
        self.assertEqual(fams, {CELL_OK: 1, CELL_BE: 0})
        self.assertEqual(r2.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(x.name for x in self.d.iterdir() if x.name.startswith(".")), [])  # no temp left
        before = r2.read_text()
        rc, out = self.run_soak(argv)
        self.assertEqual(rc, 1)
        self.assertIn("exists; not overwritten", out)
        self.assertEqual(r2.read_text(), before)

    def test_count_without_summary_is_an_error(self):
        log = self.d / "k6.log"
        log.write_text("no markers\n")
        (self.d / "k.json").write_text("{}")
        rc, out = self.run_soak(["soak", "count", "--run", "r1", "--t1", "0", "--t2", "1", "--k6-log", str(log),
                                 "--kcal", str(self.d / "k.json")])
        self.assertEqual(rc, 1)
        self.assertIn("no k6 summary", out)

    def test_required_arguments(self):
        rc, out = self.run_soak(["soak", "passive", "--run", "r1"])
        self.assertEqual(rc, 2)
        self.assertIn("--start", out)
        rc, out = self.run_soak(["soak", "count", "--run", "r1", "--t1", "1"])
        self.assertEqual(rc, 2)
        self.assertIn("--t2 --k6-log --kcal", out)

    def test_passive_and_gate(self):
        start = T0 - 30 * H
        judged = (start + 12 * H, start + 30 * H)
        r = passive_reader(judged)
        r.raw["kube_pod_container_status_restarts_total"] = [
            ser({"pod": f"demo-go-{a}-1-x"}, [(start - 30, 0), (judged[1] - 60, 0)]) for a in verify.DEMO_APPS]
        with (self.d / "guard.jsonl").open("w") as fh:
            for i in range(1080):
                fh.write(json.dumps({"fd": {"at": judged[0] + i * 60, "containers": {
                    f"go-{a}": {"fds": 20} for a in verify.DEMO_APPS}}}) + "\n")
        rc, out = self.run_soak(["--langs", "go", "soak", "passive", "--run", "r1", "--start", str(start)], reader=r)
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS", out)
        rc, out = self.run_soak(["--langs", "go", "soak", "gate", "--run", "r1"], reader=FakeReader())
        self.assertEqual(rc, 1)

    def test_verify_registers_the_mode(self):
        with mock.patch.object(sc, "main_soak", return_value=0) as m:
            self.assertEqual(verify.main(["soak", "report", "--run", "r1"]), 0)
        self.assertEqual(m.call_args.args[1].action, "report")


if __name__ == "__main__":
    unittest.main()
