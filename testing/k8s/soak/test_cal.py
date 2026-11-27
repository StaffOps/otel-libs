"""Tests for the K-cal driver (cal.py, ``soak.py cal``): the plan and its
estimate, the dry run, the first-drive and drain derivations, the repeat
records in ``soakcount.calibrate``'s shape, phases A and B on a simulated
cluster, and the cal mode through the real trap on the fake kubectl. No
cluster."""

from __future__ import annotations

import io
import json
import math
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cal  # noqa: E402
import guard  # noqa: E402
import k6script  # noqa: E402
import render  # noqa: E402
import soak  # noqa: E402
import soakcount  # noqa: E402
from test_loop import FAKE, RUN, FakeCluster  # noqa: E402

NOW = 1_800_000_000.0
# Fixed spans per request per app and scenario (what the simulated demo emits).
K = {"api": {"ok": 3, "business_error": 2, "db_error": 2, "unhandled": 2},
     "backend": {"ok": 1, "business_error": 1, "db_error": 1, "unhandled": 1},
     "worker": {"ok": 2, "business_error": 0, "db_error": 0, "unhandled": 0}}


def pod(lang: str, started: float, app: str = "worker") -> dict:
    return {"metadata": {"name": f"demo-{lang}-{app}-abc12-xyz12"},
            "status": {"containerStatuses": [{"name": app, "restartCount": 0,
                                              "state": {"running": {"startedAt": soakcount.sp.iso(started)}}}]}}


def series(metric: dict, pts) -> dict:
    return {"metric": {"cluster": "k8s01", **metric}, "values": [[t, str(v)] for t, v in pts]}


class Sim:
    """Cumulative copy and gateway counters fed by simulated k6 runs."""

    def __init__(self, langs, t0: float, drain: dict[str, float] | None = None):
        self.langs = tuple(langs)
        self.copy = {s: [(t0 - 100, 0.0)] for lang in langs for s in cal.lang_services(lang)}
        self.gw = {}
        for lang in langs:
            for key in ((f"demo-{lang}-api", "SPAN_KIND_SERVER", "POST /orders"),
                        (f"demo-{lang}-api", "SPAN_KIND_PRODUCER", f"send demo:{lang}:orders"),
                        (f"demo-{lang}-worker", "SPAN_KIND_CONSUMER", f"process demo:{lang}:orders")):
                self.gw[key] = [(t0 - 100, 0.0)]
        self.drain = drain or {}
        self.backlog = {lang: 0.0 for lang in langs}
        self.extra: dict[str, float] = {}   # svc -> extra spans added on the next run (non-integral k)

    def _add(self, store, key, t, inc):
        store[key].append((t, store[key][-1][1] + inc))

    def flat(self, t: float) -> None:
        for store in (self.copy, self.gw):
            for key, pts in store.items():
                pts.append((t, pts[-1][1]))

    def traffic(self, plan: k6script.K6Plan, start: float, end: float) -> dict:
        mixl = k6script._expand(plan.mix, k6script.EXPECTED_STATUS)
        clsl = k6script._expand(plan.classes, k6script.CLASSES)
        metrics = {}
        for lang in plan.langs:
            per = {}
            for i in range(plan.rate * plan.duration_s):
                cell = (mixl[i % len(mixl)], clsl[(i // len(mixl)) % len(clsl)])
                per[cell] = per.get(cell, 0) + 1
            ok = sum(n for (s, _), n in per.items() if s == "ok")
            self.backlog[lang] += ok
            cap = self.drain.get(lang)
            done = self.backlog[lang] if cap is None else min(self.backlog[lang], math.floor(cap * (end - start)))
            self.backlog[lang] -= done
            for app in ("api", "backend"):
                svc = f"demo-{lang}-{app}"
                self._add(self.copy, svc, end, sum(K[app][s] * n for (s, _), n in per.items())
                          + self.extra.pop(svc, 0))
            self._add(self.copy, f"demo-{lang}-worker", end, 2 * done + self.extra.pop(f"demo-{lang}-worker", 0))
            self._add(self.gw, (f"demo-{lang}-api", "SPAN_KIND_SERVER", "POST /orders"), end, sum(per.values()))
            self._add(self.gw, (f"demo-{lang}-api", "SPAN_KIND_PRODUCER", f"send demo:{lang}:orders"), end, ok)
            self._add(self.gw, (f"demo-{lang}-worker", "SPAN_KIND_CONSUMER", f"process demo:{lang}:orders"),
                      end, done)
            for (scen, cls), n in per.items():
                c = {"lang": lang, "route": "orders", "scen": scen, "class": cls}
                metrics[k6script.threshold_key(c, "expected")] = {"values": {"count": n}}
                metrics[k6script.threshold_key(c, "noanswer")] = {"values": {"count": 0}}
        for key, pts in {**self.copy, **self.gw}.items():
            if pts[-1][0] < end:
                pts.append((end, pts[-1][1]))
        return {"metrics": metrics}

    def drain_more(self, start: float, end: float) -> None:
        for lang in self.langs:
            cap = self.drain.get(lang)
            done = self.backlog[lang] if cap is None else min(self.backlog[lang], math.floor(cap * (end - start)))
            self.backlog[lang] -= done
            self._add(self.copy, f"demo-{lang}-worker", end, 2 * done)
            self._add(self.gw, (f"demo-{lang}-worker", "SPAN_KIND_CONSUMER", f"process demo:{lang}:orders"),
                      end, done)

    def copy_series(self, t2: float) -> list[dict]:
        return [series({"service_name": s, "service_instance_id": "i1", "pod": f"{s}-p"},
                       [p for p in pts if p[0] <= t2]) for s, pts in self.copy.items()]

    def gateway_series(self, t2: float) -> list[dict]:
        return [series({"service_name": s, "span_kind": k, "span_name": n}, [p for p in pts if p[0] <= t2])
                for (s, k, n), pts in self.gw.items()]


class FakeOps:
    def __init__(self, sim: Sim, t: float, *, invalid=(), drops=None, pods=None, gate_bad=(), fail_reads=False):
        self.sim, self.t = sim, t
        self.invalid, self.drops, self.gate_bad = set(invalid), drops or {}, set(gate_bad)
        self.pod_list = pods or []
        self.fail_reads = fail_reads
        self.windows: list[str] = []

    def now(self):
        return self.t

    def k6(self, window, plan):
        self.windows.append(window)
        start = self.t
        self.t += max(1, math.ceil((30 + plan.duration_s) / 60)) * 60
        if window in self.invalid:
            return soak.WindowResult(window, True, False, "platform: guard (x)", start=start, end=self.t), None
        summary = self.sim.traffic(plan, start, self.t)
        if window in self.drops:
            summary["metrics"]["dropped_iterations"] = {"values": {"count": self.drops[window]}}
        return soak.WindowResult(window, True, True, start=start, end=self.t), summary

    def quiet(self, window, seconds):
        self.windows.append(window)
        start = self.t
        self.t += max(1, math.ceil(seconds / 60)) * 60
        self.sim.drain_more(start, self.t)
        self.sim.flat(self.t)
        valid = window not in self.invalid
        return soak.WindowResult(window, True, valid, "" if valid else "platform: mesh (y)", start=start, end=self.t)

    def copy_series(self, t1, t2):
        if self.fail_reads:
            raise soakcount.verify.QueryError("prometheus unreachable")
        return self.sim.copy_series(t2)

    def gateway_series(self, t1, t2):
        return self.sim.gateway_series(t2)

    def copy_gate(self, t1, t2):
        bad = any(t1 <= b <= t2 for b in self.gate_bad)
        return {"clean": not bad, "reasons": ["copy:queue t2: size 3"] if bad else []}

    def pods(self):
        return self.pod_list


def small_plan(**kw) -> cal.CalPlan:
    base = dict(langs=("go", "python"), n=4, rate=2, steps=(2, 5, 10), step_s=60, quiesce_s=60,
                mix={"ok": 1, "unhandled": 1}, classes={"none": 1})
    base.update(kw)
    return cal.CalPlan(**base)


class Plan(unittest.TestCase):
    def test_default_plan_is_the_k8b_plan(self):
        p = cal.CalPlan(langs=tuple(render.LANGS))
        p.validate()
        self.assertEqual(len(p.cells()), 12)
        self.assertEqual({(s, c) for _, s, c in p.cells()},
                         {(s, c) for s in k6script.DEFAULT_MIX for c in k6script.CLASSES})
        est = p.estimate()
        # 120 + 12 x 2 x (60 + 120); 4 x (360 + 120)
        self.assertEqual((est["phase_a_s"], est["phase_b_s"], est["k6_runs"]), (4440, 1920, 28))
        self.assertEqual(est["total_s"], [600 + 4440 + 1920, 1260 + 4440 + 1920 + 900])
        self.assertEqual(len(p.window_names()), 28)
        for name in p.window_names():
            render.k6_name(name)  # every window name is a valid k6 Job name

    def test_cell_and_step_plans(self):
        p = small_plan()
        cp = p.cell_plan("ok", "s01")
        self.assertEqual((cp.mix, cp.classes, cp.duration_s, cp.reservoir), ({"ok": 1}, {"s01": 1}, 2, 0))
        self.assertEqual(len(cp.cells()), 2)  # one cell per language
        sp_ = p.step_plan(10)
        self.assertEqual((sp_.rate, sp_.duration_s, sp_.mix), (10, 60, p.mix))

    def test_validation(self):
        for kw, msg in (({"n": 5}, "multiple of the rate"), ({"n": 2}, "at least 2 s"),
                        ({"steps": (5,)}, "two rising"), ({"steps": (5, 2)}, "two rising"),
                        ({"steps": (2, 2)}, "two rising"), ({"step_s": 30}, "guard interval"),
                        ({"quiesce_s": 0}, "--quiesce"), ({"worker_interval_s": 600}, "worker drive"),
                        ({"mix": {"ok": 0}}, "no cell"), ({"timeout_s": 20}, "1-15 s")):
            with self.subTest(kw=kw), self.assertRaises(cal.CalError if "1-15" not in msg else k6script.PlanError) as cm:
                small_plan(**kw).validate()
            self.assertIn(msg, str(cm.exception))

    def test_worst_case_and_memory(self):
        p = cal.CalPlan(langs=tuple(render.LANGS))
        est = p.estimate()
        # cell window to its deadline (300 + 15 + 15 + 60 -> 420), step (300 + 300 + 15 + 60 -> 720)
        self.assertEqual(est["worst_s"], 1260 + 120 + 24 * (420 + 120) + 4 * (720 + 120) + 900)
        self.assertLess(est["worst_s"] + 120, p.worker_interval_s)
        need = p.step_plan(20).memory_limit_mi * 2**20
        self.assertIsNone(p.memory_problem(need + 2 * 2**30, 2 * 2**30))
        self.assertIn("exceeds MemAvailable", p.memory_problem(need + 2**30, 2 * 2**30))

    def test_describe(self):
        text = "\n".join(cal.CalPlan(langs=("go",)).describe("c1"))
        for want in ("no kubectl call", "12 cells x 2 repeats", "N = 30", "steps 2, 5, 10, 20",
                     "DEMO_INTERVAL_SECONDS=21600", "Service/soak-otlp", "28 runs", "total 1.9-2.4 h",
                     "kcal-input.json"):
            self.assertIn(want, text)


class Derivations(unittest.TestCase):
    def test_first_increase(self):
        s = [series({"service_name": "demo-python-api"}, [(NOW - 10, 4), (NOW + 15, 4), (NOW + 30, 6)]),
             series({"service_name": "demo-python-api"}, [(NOW + 20, 2)]),                 # born after: a step
             series({"service_name": "demo-node-api"}, [(NOW - 10, 9), (NOW + 40, 3)]),    # reset with spans
             series({"service_name": "demo-go-api"}, [(NOW - 10, 9), (NOW + 40, 9)]),      # flat
             {"metric": {"cluster": "vps", "service_name": "demo-go-api"}, "values": [[NOW + 5, "1"]]}]
        self.assertEqual(cal.first_increase(s, NOW), {"demo-python-api": NOW + 20, "demo-node-api": NOW + 40})

    def test_first_drives(self):
        s = [series({"service_name": "demo-python-worker"}, [(NOW + 50, 2)]),
             series({"service_name": "demo-python-api"}, [(NOW + 45, 3)])]
        pods = [pod("go", NOW + 5), pod("python", NOW + 10), pod("node", NOW + 12)]
        w = cal.first_drives(s, pods, NOW, ("go", "python", "node", "dotnet"))
        self.assertIsNone(w["go"]["first_drive"])
        self.assertIn("unmeasured: Go drives first at start + DEMO_INTERVAL_SECONDS", w["go"]["source"])
        self.assertEqual((w["python"]["first_drive"], w["python"]["first_drive_delay_s"]), (NOW + 45, 35.0))
        self.assertIn("upper bound", w["python"]["source"])
        self.assertIsNone(w["node"]["first_drive"])
        self.assertIn("unmeasured: no step", w["node"]["source"])
        self.assertEqual(w["dotnet"]["note"], "no Running worker pod")

    def test_drive_conflicts(self):
        w = {"go": {"startedAt": NOW}, "python": {"startedAt": NOW, "first_drive": NOW + 30},
             "node": {"startedAt": NOW, "first_drive": None}, "dotnet": {"startedAt": None}}
        self.assertEqual(cal.drive_conflicts(w, 21600, NOW + 3600, 120), ["dotnet: no worker start recorded"])
        bad = cal.drive_conflicts(w, 3600, NOW + 3600, 120)
        self.assertEqual(sorted(b.split(":")[0] for b in bad), ["dotnet", "go", "node", "python"])

    def test_repeat_records(self):
        cc = {"demo-go-api": soakcount.CopyCount("demo-go-api", count=90.0, count_hi=90.0),
              "demo-go-backend": soakcount.CopyCount("demo-go-backend", count=30.0, count_hi=31.0, unset=True)}
        fams = {"demo-go-api": {"server:POST /orders": soakcount.Counted(30, 31)}}
        table = {cal.cell_id("go", "ok", "none"): {"expected": 30, "unexpected": 0, "noanswer": 0}}
        (rec, problems), (rec2, _) = cal.repeat_records(table, cc, fams, ("go", "python"), "ok", "none", {"w": 1})
        self.assertEqual(rec["cell"], "lang:go,route:orders,scen:ok,class:none")
        self.assertEqual(rec["copy"], {"demo-go-worker": 0.0, "demo-go-api": 90.0, "demo-go-backend": 30.0})
        self.assertEqual((rec["absent"], rec["unset"], rec["w"]), (["demo-go-worker"], ["demo-go-backend"], 1))
        self.assertEqual(rec["gateway_ambiguous"], {"demo-go-api": ["server:POST /orders"]})
        self.assertEqual(rec["gateway"], {"demo-go-api": {}})   # the ambiguous family is left out (M3)
        self.assertEqual(problems, ["demo-go-backend: ambiguous copy count 30..31"])  # a sibling counted: absent = 0
        self.assertEqual(rec2["counts"], {"expected": 0, "unexpected": 0, "noanswer": 0})
        self.assertEqual(_, ["demo-python-worker, demo-python-api, demo-python-backend: "
                             "absent copy series, no sibling success"])   # M2: no service of python counted
        # The record is calibrate()'s input shape.
        out = soakcount.calibrate([rec, {**rec, "unset": []}])
        self.assertIn("demo-go-api", out["services"])

    def test_ambiguous_family_is_not_calibrated(self):
        """M3: a family with lower != upper is excluded from calibrate's input, so
        it gets no k (calibrate would judge the lower value)."""
        cc = {s: soakcount.CopyCount(s, count=30.0, count_hi=30.0) for s in cal.lang_services("go")}
        cid = cal.cell_id("go", "ok", "none")
        table = {cid: {"expected": 30, "unexpected": 0, "noanswer": 0}}
        recs = []
        for hi in (30, 31):
            fams = {"demo-go-api": {"server:POST /orders": soakcount.Counted(30, 30),
                                    "producer:send demo:go:orders": soakcount.Counted(30, hi)}}
            recs.append(cal.repeat_records(table, cc, fams, ("go",), "ok", "none", {})[0][0])
        out = soakcount.calibrate(recs)
        self.assertEqual(out["families"]["demo-go-api"], {"server:POST /orders": {cid: 1}})
        self.assertIn("producer:send demo:go:orders", out["excluded_families"]["demo-go-api"])
        # ambiguous in both repeats: absent from every repeat's gateway, still excluded and named
        recs = []
        for _ in range(2):
            fams = {"demo-go-api": {"server:POST /orders": soakcount.Counted(30, 30),
                                    "producer:send demo:go:orders": soakcount.Counted(30, 31)}}
            recs.append(cal.repeat_records(table, cc, fams, ("go",), "ok", "none", {})[0][0])
        out = soakcount.calibrate(recs)
        self.assertEqual(out["families"]["demo-go-api"], {"server:POST /orders": {cid: 1}})
        self.assertEqual(out["excluded_families"]["demo-go-api"], ["producer:send demo:go:orders"])

    def test_dropped_loaded_and_drain(self):
        self.assertEqual(cal.dropped_iterations({"metrics": {}}), 0)
        self.assertEqual(cal.dropped_iterations({"metrics": {"dropped_iterations": {"values": {"count": 7}}}}), 7)
        st = [{"rate": r, "clean": c} for r, c in ((2, True), (5, True), (10, True), (20, False))]
        self.assertEqual(cal.loaded_rate(st), 5)
        self.assertEqual(cal.loaded_rate([{**s, "clean": True} for s in st]), 10)
        self.assertIsNone(cal.loaded_rate(st[:1]))
        self.assertIsNone(cal.loaded_rate([{"rate": 2, "clean": False}, {"rate": 5, "clean": True}]))
        steps = [{"duration_s": 100, "drain": {"go": {"send": 100, "drain_msg_per_s": None},
                                               "python": {"send": 100, "drain_msg_per_s": 4.0}}},
                 {"duration_s": 100, "drain": {"go": {"send": 300, "drain_msg_per_s": None},
                                               "python": {"send": 300, "drain_msg_per_s": 3.5}}},
                 {"duration_s": 100, "drain": {"error": "x"}}]
        d = cal.drain_summary(steps, ("go", "python", "node"))
        self.assertEqual(d["python"], {"state": "measured", "msg_per_s": 3.5,
                                       "basis": "consumed / step span while a backlog was left at the boundary"})
        self.assertEqual((d["go"]["state"], d["go"]["at_least_msg_per_s"]), ("unmeasured", 3.0))
        self.assertEqual(d["node"]["reason"], "no drain read")

    def test_calibrate_names_a_planned_cell_without_repeats(self):
        cid = cal.cell_id("go", "ok", "none")
        out = soakcount.calibrate([], planned=[cid])
        self.assertFalse(out["services"]["demo-go-api"]["qualified"])
        self.assertEqual(out["services"]["demo-go-api"]["cause"], f"{cid}: 0 repeat(s), need 2")

    def test_write_json_atomic_and_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sub" / "x.json"
            cal.write_json(p, {"a": 1})
            self.assertEqual(json.loads(p.read_text()), {"a": 1})
            with self.assertRaises(cal.CalError):
                cal.write_json(p, {"k": "https://user:pw@example.com/"})
            self.assertEqual(json.loads(p.read_text()), {"a": 1})  # the old file stays
            self.assertEqual(sorted(x.name for x in p.parent.iterdir()), ["x.json"])  # no temp left


class Driver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.lines: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def drive(self, plan=None, **ops_kw):
        plan = plan or small_plan()
        sim = Sim(plan.langs, NOW, drain=ops_kw.pop("drain", None))
        seed = ops_kw.pop("seed", True)
        extra = ops_kw.pop("extra", {})
        if seed:  # python's first drive after the hook
            sim.copy["demo-python-api"].append((NOW + 40, 3.0))
        ops = FakeOps(sim, NOW + 1200, pods=[pod("go", NOW + 10), pod("python", NOW + 12)], **ops_kw)
        sim.extra.update(extra)
        c = cal.Calibrator(ops, plan, self.d, log=self.lines.append)
        return c, ops, sim

    def out(self, name):
        return json.loads((self.d / name).read_text())

    def test_full_run(self):
        c, ops, _ = self.drive(drain={"python": 1.0})
        self.assertEqual(c.run(NOW), 2, self.lines)   # rate cap FAIL (code review M4)
        out, inp = self.out("soak-cal.json"), self.out("kcal-input.json")
        self.assertEqual(len(inp["repeats"]), 2 * 2 * 2)   # 2 cells x 2 repeats x 2 langs
        self.assertEqual(inp["invalid"], [])
        self.assertEqual(out["services"]["demo-go-api"],
                         {"qualified": True, "cause": None,
                          "k": {"lang:go,route:orders,scen:ok,class:none": 3,
                                "lang:go,route:orders,scen:unhandled,class:none": 2}})
        self.assertEqual(out["services"]["demo-go-worker"]["k"]["lang:go,route:orders,scen:ok,class:none"], 2)
        self.assertEqual(out["families"]["demo-go-api"]["server:POST /orders"],
                         {"lang:go,route:orders,scen:ok,class:none": 1,
                          "lang:go,route:orders,scen:unhandled,class:none": 1})
        # verify.py soak cal on the written input reproduces the calibration.
        again = soakcount.calibrate(inp["repeats"], mix=inp["mix"], rate=out["driver"]["loaded_rate"],
                                    drain=inp.get("drain"), invalid=inp["invalid"], planned=inp["planned"])
        self.assertEqual({k: out[k] for k in again}, again)
        drv = out["driver"]
        self.assertEqual([s["rate"] for s in drv["steps"]], [2, 5, 10])
        self.assertTrue(all(s["clean"] for s in drv["steps"]))
        self.assertEqual(drv["loaded_rate"], 5)
        self.assertEqual(drv["workers"]["python"]["first_drive"], NOW + 40)
        self.assertIsNone(drv["workers"]["go"]["first_drive"])
        self.assertEqual(drv["drain"]["python"]["state"], "measured")
        self.assertEqual(drv["drain"]["go"]["state"], "unmeasured")
        self.assertEqual(inp["drain"], {"python": drv["drain"]["python"]["msg_per_s"]})
        self.assertEqual(out["rate_cap"]["python"]["verdict"], "FAIL")
        # floor(1.0 msg/s / ok share 0.5) = 2 req/s; min with the loaded rate 5.
        self.assertEqual((drv["k8b_rate"], drv["k8b_rate_per_lang"]), (2, {"python": 2}))
        self.assertEqual(ops.windows[:3], ["cal-q0", "cal-a00-r1", "cal-a00-q1"])
        self.assertTrue(any("loaded rate 5" in x for x in self.lines))
        self.assertTrue(any(x.startswith("cal: FAIL rate cap for python") and "run K8b at 2 req/s" in x
                            for x in self.lines))
        self.assertEqual([s["noanswer"] for s in drv["steps"]], [0, 0, 0])
        self.assertTrue(drv["k6_peak_rss"].startswith("unmeasured"))

    def test_full_run_without_saturation_passes(self):
        c, _, _ = self.drive()
        self.assertEqual(c.run(NOW), 0, self.lines)
        out = self.out("soak-cal.json")
        self.assertNotIn("rate_cap", out)
        self.assertEqual({v["state"] for v in out["driver"]["drain"].values()}, {"unmeasured"})
        self.assertEqual(out["driver"]["k8b_rate"], 5)

    def test_late_consumer_span_is_not_a_drain_rate(self):
        """Code review M1/L5: process short by a few messages at t2 (a late span or
        an ambiguity) is below max(10, 5 %): no drain rate, no rate cap."""
        self.assertFalse(cal.saturated(100, 95))
        self.assertFalse(cal.saturated(1000, 951))
        self.assertTrue(cal.saturated(1000, 940))
        self.assertTrue(cal.saturated(30, 19))
        c, ops, sim = self.drive()
        orig = sim.traffic

        def late(plan, start, end):
            out = orig(plan, start, end)
            if plan.duration_s == 60 and plan.rate == 10:  # three consumer spans arrive after t2
                key = ("demo-python-worker", "SPAN_KIND_CONSUMER", "process demo:python:orders")
                t, v = sim.gw[key][-1]
                sim.gw[key][-1] = (t, v - 3)
            return out
        sim.traffic = late
        self.assertEqual(c.run(NOW), 0, self.lines)
        out = self.out("soak-cal.json")
        step = out["driver"]["steps"][-1]["drain"]["python"]
        self.assertEqual((step["send"] - step["process"], step["saturated"], step["drain_msg_per_s"]),
                         (3.0, False, None))
        self.assertEqual(out["driver"]["drain"]["python"]["state"], "unmeasured")
        self.assertNotIn("rate_cap", out)

    def test_k8b_rate(self):
        mix = {"ok": 7, "business_error": 1, "db_error": 1, "unhandled": 1}
        self.assertEqual(cal.k8b_rate(10, {"go": 5.0, "node": 2.0}, mix), (2, {"go": 7, "node": 2}))
        self.assertEqual(cal.k8b_rate(5, {}, mix), (5, {}))
        self.assertEqual(cal.k8b_rate(None, {"go": 5.0}, mix), (None, {"go": 7}))

    def test_non_integral_cell_is_excluded_by_calibrate(self):
        c, _, _ = self.drive(extra={"demo-go-backend": 1})   # one extra span in the first repeat
        self.assertEqual(c.run(NOW), 0)
        svc = self.out("soak-cal.json")["services"]["demo-go-backend"]
        self.assertFalse(svc["qualified"])
        self.assertIn("not integral", svc["cause"])

    def test_invalid_repeat_aborts_and_is_named(self):
        c, ops, _ = self.drive(invalid={"cal-a01-r1"})
        self.assertEqual(c.run(NOW), 1)
        inp, out = self.out("kcal-input.json"), self.out("soak-cal.json")
        bad = [r for r in inp["invalid"] if r["window"] == "cal-a01-r1"]
        self.assertEqual(len(bad), 2)
        self.assertEqual(bad[0]["reasons"], ["platform: guard (x)"])
        self.assertIn("cal-a01-r1: platform: guard (x)", out["driver"]["phase_a"]["aborted"])
        self.assertEqual(out["driver"]["steps"], [])
        self.assertNotIn("cal-a01-q1", ops.windows)   # no quiesce after an invalid k6 window
        self.assertFalse(out["services"]["demo-go-api"]["qualified"])
        self.assertIn("lang:go,route:orders,scen:unhandled,class:none: invalid repeat(s): platform: guard (x)",
                      out["services"]["demo-go-api"]["cause"])
        self.assertEqual(out["services"]["demo-go-api"]["k"], {"lang:go,route:orders,scen:ok,class:none": 3})
        # verify.py soak cal on the written input names the same cause.
        again = soakcount.calibrate(inp["repeats"], mix=inp["mix"], invalid=inp["invalid"], planned=inp["planned"])
        self.assertEqual(again["services"], out["services"])

    def test_invalid_quiesce_and_unclean_gate(self):
        for kw, why in (({"invalid": {"cal-q0"}}, "quiesce before the first repeat"),
                        ({"invalid": {"cal-a00-q2"}}, "quiesce: platform: mesh (y)")):
            with self.subTest(kw=kw):
                c, _, _ = self.drive(**kw)
                self.assertEqual(c.run(NOW), 1)
                self.assertIn(why, json.dumps(self.out("soak-cal.json")["driver"]))
        c, _, _ = self.drive(gate_bad={NOW + 1200 + 60 * 4})
        self.assertEqual(c.run(NOW), 0)
        inp = self.out("kcal-input.json")
        self.assertTrue(inp["invalid"])
        self.assertTrue(all("copy gate: copy:queue t2: size 3" in r["reasons"] for r in inp["invalid"]))

    def test_count_read_failure_invalidates_the_cell(self):
        c, ops, _ = self.drive()
        orig = ops.copy_series
        calls = []

        def flaky(t1, t2):
            calls.append(t1)
            if len(calls) == 2:
                raise soakcount.verify.QueryError("prometheus unreachable")
            return orig(t1, t2)
        ops.copy_series = flaky
        c.run(NOW)
        inp = self.out("kcal-input.json")
        self.assertTrue(any("count read failed: prometheus unreachable" in " ".join(r["reasons"])
                            for r in inp["invalid"]))

    def test_dropped_iterations_end_the_steps(self):
        c, _, _ = self.drive(drops={"cal-b1-5": 3})
        self.assertEqual(c.run(NOW), 1)   # only one clean step: no loaded rate
        drv = self.out("soak-cal.json")["driver"]
        self.assertEqual([(s["rate"], s["clean"], s["dropped_iterations"]) for s in drv["steps"]],
                         [(2, True, 0), (5, False, 3)])
        self.assertIsNone(drv["loaded_rate"])
        c, _, _ = self.drive(invalid={"cal-b2-10"})
        self.assertEqual(c.run(NOW), 0)
        drv = self.out("soak-cal.json")["driver"]
        self.assertEqual((drv["loaded_rate"], drv["steps"][-1]["invalid"]), (2, "platform: guard (x)"))

    def test_drain_wait_after_a_backlog(self):
        plan = small_plan(drain_wait_s=600)
        c, ops, sim = self.drive(plan, drain={"python": 0.05})
        self.assertEqual(c.run(NOW), 2)   # the measured drain fails the rate cap
        dw = self.out("soak-cal.json")["driver"]["drain_wait"]
        self.assertGreater(dw["waited_s"], 0)
        self.assertEqual(len(dw["reads"]), int(dw["waited_s"] // 60))   # code review L3
        self.assertIn("python", dw["reads"][0]["read"])
        self.assertEqual(dw["drained"], sim.backlog["python"] == 0)
        self.assertTrue(any(w.startswith("cal-d") for w in ops.windows))
        c, ops, _ = self.drive(small_plan(drain_wait_s=600), drain={"python": 0.05}, invalid={"cal-d0"})
        c.run(NOW)
        self.assertEqual(self.out("soak-cal.json")["driver"]["drain_wait"]["stopped"], "platform: mesh (y)")

    def test_worker_drive_inside_aborts_before_any_k6(self):
        c, ops, _ = self.drive(small_plan(worker_interval_s=4000))
        ops.pod_list = [pod("go", NOW - 3000), pod("python", NOW - 3000)]
        self.assertEqual(c.run(NOW), 1)
        self.assertEqual(ops.windows, [])
        self.assertIn("worker drive inside the calibration", self.out("soak-cal.json")["driver"]["aborted"])

    def test_drive_check_before_each_window(self):
        """L1: a run that is late (the clock jumped) stops before the window whose
        worst case would reach a worker's next drive."""
        c, ops, _ = self.drive(small_plan(worker_interval_s=8000))
        orig = ops.quiet

        def jump(window, seconds):
            res = orig(window, seconds)
            if window == "cal-a00-q1":
                ops.t += 7000
                res.end = ops.t
            return res
        ops.quiet = jump
        self.assertEqual(c.run(NOW), 1)
        self.assertIn("worker drive inside the next window",
                      self.out("soak-cal.json")["driver"]["phase_a"]["aborted"])
        self.assertNotIn("cal-a00-r2", ops.windows)

    def test_gap_between_windows_invalidates(self):
        c, ops, _ = self.drive()
        orig = ops.k6

        def late(window, plan):
            if window == "cal-a00-r2":
                ops.t += 600
            return orig(window, plan)
        ops.k6 = late
        self.assertEqual(c.run(NOW), 1)
        self.assertIn("laptop: 600s between windows", self.out("soak-cal.json")["driver"]["phase_a"]["aborted"])

    def test_missing_summary_invalidates(self):
        c, ops, _ = self.drive()
        orig = ops.k6
        ops.k6 = lambda w, p: (orig(w, p)[0], None)
        self.assertEqual(c.run(NOW), 1)
        self.assertIn("k6: no summary", self.out("soak-cal.json")["driver"]["phase_a"]["aborted"])


class ClusterReads(unittest.TestCase):
    def test_calops_reads_use_k_counts_readers(self):
        class R:
            def __init__(self):
                self.queries = []

            def prom(self, q, at):
                self.queries.append((q, at))
                return [series({"service_name": "demo-go-api"}, [(at, 1)]),
                        {"metric": {"cluster": "vps", "service_name": "demo-go-api"}, "values": []}]

            def kget(self, args):
                self.queries.append(tuple(args))
                return {"items": [pod("go", NOW)]}
        r = R()
        ops = soak.CalOps(RUN, None, None, r, Path("."))
        self.assertEqual(len(ops.copy_series(NOW, NOW + 600)), 1)   # k8s01 only
        self.assertEqual(len(ops.gateway_series(NOW, NOW + 600)), 1)
        self.assertEqual(len(ops.pods()), 1)
        q_copy, q_gw = r.queries[0][0], r.queries[1][0]
        self.assertIn("otel_libs_soak_fixed_spans_total", q_copy)
        self.assertIn('job="dev-otel-libs/soak-agent"', q_copy)
        self.assertIn("traces_span_metrics_calls_total", q_gw)
        # (t2 - t1) + expiry + BORN_GRACE + one scrape, as K-count's count action reads.
        self.assertTrue(q_copy.endswith(f"[{600 + soakcount.COPY_EXPIRY_S + soakcount.sp.BORN_GRACE + soakcount.sp.SCRAPE_S}s]"))
        self.assertEqual(r.queries[2], ("pods", "-n", "dev-otel-libs"))
        orig = soakcount._copy_gate
        soakcount._copy_gate = lambda rd, t1, t2: ({"copy:queue t1": (False, "size 2")}, [])
        try:
            self.assertEqual(ops.copy_gate(NOW, NOW + 1), {"clean": False, "reasons": ["copy:queue t1: size 2"]})
        finally:
            soakcount._copy_gate = orig


class MemoryCheck(unittest.TestCase):
    def test_k6_memory_check(self):
        plan = cal.CalPlan(langs=tuple(render.LANGS))
        cfg = guard.GuardConfig()

        class R:
            def __init__(self, v):
                self.v = v

            def prom(self, q, at):
                if self.v is None:
                    raise guard.ReadError("api unreachable")
                assert "node_memory_MemAvailable_bytes" in q
                return [] if self.v == "none" else [{"metric": {"cluster": "k8s01"}, "value": [at, str(self.v)]}]
        self.assertIsNone(soak.k6_memory_check(R(20 * 2**30), cfg, plan, NOW))
        self.assertIn("exceeds MemAvailable", soak.k6_memory_check(R(4 * 2**30), cfg, plan, NOW))
        self.assertIn("unreadable", soak.k6_memory_check(R(None), cfg, plan, NOW))
        self.assertIn("no MemAvailable", soak.k6_memory_check(R("none"), cfg, plan, NOW))


class CliAndCluster(unittest.TestCase):
    """The cal mode through soak.main: the dry run touches nothing; the full mode
    runs inside run's pre-flight, hook and trap on the fake kubectl."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.saved_env = os.environ.get("SOAK_RUNS_DIR")
        os.environ["SOAK_RUNS_DIR"] = str(self.dir / "runs")

    def tearDown(self):
        if self.saved_env is None:
            os.environ.pop("SOAK_RUNS_DIR", None)
        else:
            os.environ["SOAK_RUNS_DIR"] = self.saved_env
        self.tmp.cleanup()

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = soak.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def script(self, name, text):
        p = self.dir / name
        p.write_text(text)
        p.chmod(0o755)
        return p

    def test_dry_run_touches_nothing(self):
        kubectl = self.script("kubectl", "#!/bin/sh\necho called >> \"$0.calls\"\nexit 1\n")
        rc, out, _ = self.main("cal", "--run", RUN, "--dry-run", "--kubectl", str(kubectl), "--steps", "2,4")
        self.assertEqual(rc, 0)
        self.assertIn("steps 2, 4 req/s", out)
        self.assertFalse((self.dir / "runs").exists())
        self.assertFalse(Path(str(kubectl) + ".calls").exists())

    def test_bad_plan_is_one_line(self):
        rc, _, err = self.main("cal", "--run", RUN, "--dry-run", "--n", "5")
        self.assertEqual((rc, err.strip().startswith("soak: --n 5")), (1, True))
        with self.assertRaises(SystemExit):
            self.main("cal", "--run", RUN, "--steps", "a,b")

    def test_cal_mode_on_the_fake_cluster(self):
        """Pre-flight, gate, hook (worker interval 21600), the k6 windows through
        the loop, each Job deleted after its logs, the outputs written, and the
        trap leaves exactly the seeded objects."""
        kubectl = self.script("kubectl", f"#!/bin/sh\nexec {sys.executable} -I {HERE / 'fakekube.py'} \"$@\"\n")
        demo = self.script("demo.sh", f"#!/bin/sh\nexec {sys.executable} -I {HERE / 'fakekube.py'} demo-sh \"$@\"\n")
        t0 = time.time()
        clock = {"t": t0}
        langs = ("go",)
        sim = Sim(langs, t0)
        plan = cal.CalPlan(langs=langs, n=4, rate=2, steps=(2, 5), step_s=60, quiesce_s=60,
                           mix={"ok": 1}, classes={"none": 1})
        saved = (soak.DEMO_SH, soak.start_caffeinate, guard.entry_read, guard.wait_demo_ready,
                 guard.wait_copy_counts, guard.copy_totals, guard.window_read, soak.Loop, soak.CalOps,
                 soak.Kube.apply, cal.CAL_MIX, cal.CAL_CLASSES, soak.KubeReader)
        ok = guard.Item("x", "ok")
        soak.DEMO_SH, soak.start_caffeinate = demo, lambda: None
        guard.entry_read = lambda r, cfg, at, **kw: guard.GuardRead(at, [ok])
        guard.wait_demo_ready = guard.wait_copy_counts = lambda *a, **kw: ok
        guard.copy_totals = lambda r, at: {}
        guard.window_read = lambda *a, **kw: guard.GuardRead(clock["t"], [ok])
        cal.CAL_MIX, cal.CAL_CLASSES = dict(plan.mix), dict(plan.classes)
        saved_mem = soak.k6_memory_check
        soak.k6_memory_check = lambda *a: None

        class FakeReader:
            def __init__(self, kube):
                self.kube = kube

        class TestLoop(soak.Loop):
            def __init__(self, *a, **kw):
                def sleep(s):
                    clock["t"] += s
                super().__init__(*a, **{**kw, "clock": lambda: clock["t"], "sleep": sleep, "fd_reader": None})

        class TestOps(soak.CalOps):
            def copy_series(self, t1, t2):
                return sim.copy_series(t2)

            def gateway_series(self, t1, t2):
                return sim.gateway_series(t2)

            def copy_gate(self, t1, t2):
                return {"clean": True, "reasons": []}

            def pods(self):
                return [pod("go", t0)]

        plans = {"cal-a00-r1": plan.cell_plan("ok", "none"), "cal-a00-r2": plan.cell_plan("ok", "none"),
                 "cal-b0-2": plan.step_plan(2), "cal-b1-5": plan.step_plan(5)}
        try:
            with FakeCluster(self.dir) as c:
                orig_apply = saved[9]

                def apply_and_finish(self_, run, objs):
                    res = orig_apply(self_, run, objs)
                    for o in objs:
                        if o["kind"] == "Job":
                            name = o["metadata"]["name"]
                            window = name[len("soak-k6-"):]
                            start = clock["t"]
                            clock["t"] += 60
                            summary = sim.traffic(plans[window], start, clock["t"])
                            text = f"{k6script.SUMMARY_BEGIN}\n{json.dumps(summary)}\n{k6script.SUMMARY_END}\n"

                            def done(st, name=name, text=text):
                                st.setdefault("logs", {})[f"job/{name}"] = text
                                for x in st["objects"]:
                                    if x["kind"] == "Job" and x["metadata"]["name"] == name:
                                        x["status"] = {"succeeded": 1}
                            c.edit(done)
                    return res
                soak.Kube.apply = apply_and_finish
                soak.Loop, soak.CalOps, soak.KubeReader = TestLoop, TestOps, FakeReader
                rc, out, err = self.main("cal", "--run", RUN, "--kubectl", str(kubectl), "--tag", "abc1234",
                                         "--langs", "go", "--n", "4", "--steps", "2,5", "--step-duration", "60",
                                         "--quiesce", "60")
                self.assertEqual(c.keys(), c.seeded)   # the trap removed every soak-run object
                deletes = [a for a in c.verbs() if a[:1] != ["demo-sh"] and "delete" in a]
                job_deletes = [a for a in deletes if "--field-selector" in a]
                self.assertEqual(len(job_deletes), 4)   # each k6 Job after its logs
                demo_calls = c.demo_calls()
        finally:
            (soak.DEMO_SH, soak.start_caffeinate, guard.entry_read, guard.wait_demo_ready,
             guard.wait_copy_counts, guard.copy_totals, guard.window_read, soak.Loop, soak.CalOps,
             soak.Kube.apply, cal.CAL_MIX, cal.CAL_CLASSES, soak.KubeReader) = saved
            soak.k6_memory_check = saved_mem
        self.assertEqual(rc, 0, out + err)
        d = self.dir / "runs" / RUN
        self.assertIn("*/worker DEMO_INTERVAL_SECONDS=21600", (d / "soak.env").read_text())
        self.assertEqual(demo_calls[-1]["env"]["DEMO_RESTORE"], "1")
        out_json = json.loads((d / "soak-cal.json").read_text())
        self.assertEqual(out_json["services"]["demo-go-api"]["k"], {"lang:go,route:orders,scen:ok,class:none": 3})
        self.assertTrue((d / "raw" / "soak-k6-cal-a00-r1.log").exists())
        self.assertEqual(out_json["driver"]["loaded_rate"], 2)   # both steps clean: the highest minus one
        self.assertEqual(json.loads((d / "state.json").read_text())["window"], "cal")


if __name__ == "__main__":
    unittest.main()
