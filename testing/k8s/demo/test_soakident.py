"""Tests for ``verify.py soak identity`` (K3) and ``soak labels`` (K5)
(testing/k8s/soakident.py) and the ``soak shut`` input handling of
soakcount.action_shut, on fake reads. No cluster.

Label sets follow the live series of ``job="dev-otel-libs/demo-python-api"``
read on 2026-11-26 (app series with ``instance`` and ``k8s_pod_name``;
``target_info`` with ``service_instance_id``, ``k8s_pod_uid``,
``otel_libs_run``, ``telemetry_sdk_*``; gateway span-metrics series without
``instance``)."""

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
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "soak"))
import soakcount as sc  # noqa: E402
import soakident as si  # noqa: E402
import soakplatform as sp  # noqa: E402
import verify  # noqa: E402

T0 = 1_795_000_000.0
SVC = "demo-go-api"
OLD_POD, NEW_POD, SHUT_POD = "demo-go-api-old1-x", "demo-go-api-new2-x", "demo-go-api-new3-x"
OLD, NEW, NEWER = "11111111-aaaa", "22222222-bbbb", "33333333-cccc"
PROMOTED = ["cluster", "service.name", "service.namespace", "service.version", "deployment.environment.name",
            "k8s.cluster.name", "k8s.namespace.name", "k8s.pod.name", "k8s.container.name", "k8s.deployment.name",
            "k8s.statefulset.name", "k8s.daemonset.name", "k8s.node.name"]


def resource(iid: str, pod: str, svc: str = SVC) -> dict:
    return {"cluster": "k8s01", "job": f"dev-otel-libs/{svc}", "instance": iid, "service_name": svc,
            "service_namespace": "dev-otel-libs", "service_version": "7080d19", "deployment_environment_name": "dev",
            "k8s_cluster_name": "k8s01", "k8s_namespace_name": "dev-otel-libs", "k8s_pod_name": pod,
            "k8s_container_name": "api", "k8s_deployment_name": svc, "k8s_node_name": "k8s01"}


def app_set(iid: str, pod: str, svc: str = SVC) -> list[dict]:
    r = resource(iid, pod, svc)
    ti = {**r, "__name__": "target_info", "service_instance_id": iid, "k8s_pod_uid": "u-" + pod,
          "otel_libs_run": "r", "telemetry_sdk_language": "go", "telemetry_sdk_name": "opentelemetry",
          "telemetry_sdk_version": "1.0"}
    return [ti, {**r, "__name__": "http_server_request_duration_seconds_count", "http_route": "/orders"},
            {**r, "__name__": "process_cpu_time_seconds_total"}]


def gateway_set(svc: str = SVC) -> list[dict]:
    return [{"cluster": "k8s01", "job": f"dev-otel-libs/{svc}", "__name__": "traces_span_metrics_calls_total",
             "span_name": "POST /orders"},
            {"cluster": "k8s01", "job": f"dev-otel-libs/{svc}", "__name__": "target_info", "k8s_pod_ip": "10.0.0.1"},
            {"cluster": "vps", "job": f"dev-otel-libs/{svc}", "__name__": "x", "instance": "other"}]


class Src:
    """Fake reads: series by a substring of the match, the last sample per
    instance, Loki groups per service (or an error)."""

    def __init__(self):
        self.app = {SVC: app_set(OLD, OLD_POD) + app_set(NEW, NEW_POD) + app_set(NEWER, SHUT_POD) + gateway_set()}
        self.copy = {SVC: [{"cluster": "k8s01", "service_name": SVC, "service_instance_id": i, "exported_instance": i}
                           for i in (NEW, NEWER)]}
        self.last = {OLD: T0 + 12, NEW: T0 + 400}
        self.loki: dict | Exception = {SVC: {NEW: 30, NEWER: 9, "": 4}}
        self.queries: list[str] = []

    def series(self, match, start, end):
        self.queries.append(match)
        if "otel_libs_soak_fixed_spans_total" in match:
            return self.copy.get(re.search(r'service_name="([a-z-]+)"', match).group(1), [])
        return self.app.get(re.search(r'job="dev-otel-libs/([a-z-]+)"', match).group(1), [])

    def prom(self, q, at):
        self.queries.append(q)
        iid = re.search(r'instance="([^"]+)"', q).group(1)
        return [{"metric": {}, "value": [at, str(self.last[iid])]}] if iid in self.last else []

    def loki_query(self, q, at):
        if isinstance(self.loki, Exception):
            raise self.loki
        svc = re.search(r'service_name="([^"]+)"', q).group(1)
        return [{"metric": {"service_instance_id": k} if k else {}, "value": [at, str(n)]}
                for k, n in self.loki.get(svc, {}).items()]


def ident_input(**kw) -> dict:
    inp = {"hook_at": T0, "t1": T0 + 100, "t2": T0 + 800, "export_interval_s": 10, "finish_resolution_s": 1,
           "promoted": {"list": PROMOTED, "source": "test"},
           "rolls": {"nonce": {"judged": True, "services": {SVC: {"old": [{"pod": OLD_POD, "finished_at": T0 + 5}],
                                                                   "new": [{"pod": NEW_POD}]}}},
                     "shut": {"judged": False, "services": {SVC: {"old": [{"pod": NEW_POD, "finished_at": T0 + 392}],
                                                                   "new": [{"pod": SHUT_POD}]}}}}}
    inp.update(kw)
    return inp


def nonce(data):
    return data["rolls"]["nonce"][SVC]


def states(v) -> dict[str, str]:
    return {c["check"]: c["state"] for c in v["checks"]}


class Identity(unittest.TestCase):
    def test_pass_and_report_only_shut_roll(self):
        src = Src()
        data = si.action_identity(src, ident_input(), T0 + 900)
        v = nonce(data)
        self.assertEqual(v["verdict"], "PASS", v["checks"])
        self.assertEqual((v["old_instances"], v["new_instances"]), ([OLD], [NEW]))
        s = data["rolls"]["shut"][SVC]
        self.assertEqual((s["verdict"], s["result"]), ("REPORT-ONLY", "PASS"))
        q = [x for x in src.queries if x.startswith("max(")][0]
        self.assertIn("timestamp({cluster=\"k8s01\", job=\"dev-otel-libs/demo-go-api\", instance=", q)
        self.assertIn("[1200s:5s]", q)        # 15 min + the 5 min lookback when the start is unknown
        self.assertNotIn("last_over_time", q)
        self.assertIn("finishedAt + 10 s", data["bound"])

    def test_failures(self):
        cases = {
            "late last sample": lambda s, i: s.last.__setitem__(OLD, T0 + 17),
            "new id equals old": lambda s, i: s.app.__setitem__(SVC, app_set(OLD, OLD_POD) + app_set(OLD, NEW_POD)),
            "unset id": lambda s, i: s.app.__setitem__(SVC, app_set(OLD, OLD_POD) + app_set("unset", NEW_POD)),
            "two ids for a pod": lambda s, i: s.app.__setitem__(SVC, app_set(OLD, OLD_POD) + app_set(NEW, NEW_POD)
                                                               + app_set("9", NEW_POD)),
            "copy lacks it": lambda s, i: s.copy.__setitem__(SVC, [{"cluster": "k8s01", "service_instance_id": "z",
                                                                   "exported_instance": "z"}]),
            "copy mismatch": lambda s, i: s.copy.__setitem__(SVC, [{"cluster": "k8s01", "service_instance_id": NEW,
                                                                   "exported_instance": "other"}]),
            "loki lacks it": lambda s, i: s.loki.__setitem__(SVC, {"other": 3}),
        }
        for name, fn in cases.items():
            with self.subTest(name):
                src, inp = Src(), ident_input()
                fn(src, inp)
                self.assertEqual(nonce(si.action_identity(src, inp, T0 + 900))["verdict"], "FAIL")

    def test_unmeasured_and_unqualified(self):
        src = Src()
        src.copy[SVC], src.loki[SVC] = [], {}
        v = nonce(si.action_identity(src, ident_input(), T0 + 900))
        self.assertEqual(v["verdict"], "UNMEASURED")
        self.assertEqual(states(v)[f"copy {NEW}"], "UNMEASURED")
        self.assertEqual(states(v)[f"loki {NEW}"], "UNMEASURED")
        src = Src()
        del src.last[OLD]
        self.assertEqual(nonce(si.action_identity(src, ident_input(), T0 + 900))["verdict"], "UNMEASURED")
        inp = ident_input()
        inp["rolls"]["nonce"]["services"][SVC]["old"][0]["finished_at"] = None
        self.assertEqual(nonce(si.action_identity(Src(), inp, T0 + 900))["verdict"], "UNQUALIFIED")
        inp = ident_input()
        inp["rolls"]["nonce"]["services"][SVC]["new"] = [{"pod": "demo-go-api-zz-z"}]
        inp["rolls"]["nonce"]["services"][SVC]["old"].append({"pod": "demo-go-api-gone-y", "finished_at": T0})
        v = nonce(si.action_identity(Src(), inp, T0 + 900))
        self.assertEqual(states(v)["new demo-go-api-zz-z"], "UNMEASURED")
        self.assertEqual(states(v)["old demo-go-api-gone-y"], "UNMEASURED")
        inp["rolls"]["nonce"]["services"][SVC]["new"] = []
        self.assertEqual(states(nonce(si.action_identity(Src(), inp, T0 + 900)))["new pods"], "UNMEASURED")

    def test_loki_error_and_unset_copy_are_findings(self):
        src = Src()
        src.loki = verify.QueryError("loki: 502")
        src.copy[SVC].append({"cluster": "k8s01", "service_instance_id": "unset"})
        data = si.action_identity(src, ident_input(), T0 + 900)
        self.assertEqual(states(nonce(data))[f"loki {NEW}"], "UNMEASURED")
        self.assertTrue(any("Loki unreadable" in f for f in data["findings"]))
        self.assertTrue(any("unset service_instance_id" in f for f in data["findings"]))

    def test_wide_range_final_flush_and_loki_extra(self):
        src = Src()
        src.loki[SVC]["99999999-zzzz"] = 2
        inp = ident_input()
        old = inp["rolls"]["nonce"]["services"][SVC]["old"][0]
        old.update(started_at=T0 - 3 * 3600, deletion=T0 + 2)
        data = si.action_identity(src, inp, T0 + 900)
        q = [x for x in src.queries if x.startswith("max(") and OLD in x][0]
        self.assertIn(f"[{3 * 3600 + 605 + 300}s:6s]", q)    # from the pod's start, step <= the lookback
        ro = {c["check"]: c for c in nonce(data)["report_only"]}
        self.assertTrue(ro[f"final flush {OLD}"]["seen"])
        self.assertIn("99999999-zzzz", ro["loki extra ids"]["detail"])
        self.assertEqual(nonce(data)["verdict"], "PASS")       # report-only parts never change it
        old.update(started_at=T0 - 5 * 86400, deletion=T0 + 20)
        src.queries.clear()
        data = si.action_identity(src, inp, T0 + 900)
        q = [x for x in src.queries if x.startswith("max(") and OLD in x][0]
        self.assertIn("[86400s:44s]", q)
        self.assertFalse({c["check"]: c for c in nonce(data)["report_only"]}[f"final flush {OLD}"]["seen"])
        self.assertIn("final flush", si.rows("identity", data)[0][2])

    def test_tempo_report_only_through_the_guard(self):
        import soakextras as sx

        class Guard:
            def __init__(self):
                self.calls = []

            def search(self, q, start, end, limit):
                self.calls.append(q)
                if NEWER in q:
                    raise sx.TempoStopped("tempo-0 working set 3.10 GiB >= stop line")
                return [{"traceID": "a"}] if NEW in q else []

            def stats(self):
                return {"reads": len(self.calls)}
        g = Guard()
        data = si.action_identity(Src(), ident_input(), T0 + 900, g)
        ro = {c["check"]: c for c in nonce(data)["report_only"]}
        self.assertEqual(ro[f"tempo {NEW}"]["detail"], "found")
        self.assertIn("unmeasured", {c["check"]: c for c in data["rolls"]["shut"][SVC]["report_only"]}
                      [f"tempo {NEWER}"]["detail"])
        self.assertIn('resource.service.instance.id = "22222222-bbbb"', g.calls[0])
        self.assertEqual(data["tempo"], {"reads": 2})
        self.assertEqual(nonce(data)["verdict"], "PASS")

    def test_rows(self):
        src = Src()
        src.last[OLD] = T0 + 20
        rows = si.rows("identity", si.action_identity(src, ident_input(), T0 + 900))
        self.assertEqual(rows[0][:2], (f"nonce {SVC}", "FAIL"))
        self.assertIn("last sample", rows[0][2])
        self.assertEqual(rows[1][1], "REPORT-ONLY (PASS)")


class Labels(unittest.TestCase):
    def test_pass_counts_before_and_after(self):
        data = si.action_labels(Src(), ident_input())
        v = data["services"][SVC]
        self.assertEqual(v["verdict"], "PASS", v)
        self.assertEqual(v["rolls"]["nonce"], {"before": {OLD: 2}, "after": {NEW: 2}})
        self.assertEqual(v["other_series"], 2)            # gateway series of the job, not judged (vps dropped)
        i = v["instances"][NEW]
        self.assertIn("k8s_pod_name", i["promoted_present"])
        self.assertIn("k8s_pod_uid", i["resource_only"])
        self.assertIn("k8s_daemonset_name", i["promoted_absent"])
        self.assertEqual(i["by_metric"], {"http_server_request_duration_seconds_count": 1,
                                          "process_cpu_time_seconds_total": 1})
        self.assertIn("new series", data["note"])
        rows = si.rows("labels", data)
        self.assertEqual(rows[0][:2], (SVC, "PASS"))
        self.assertIn("nonce: before 2 series (1 inst), after 2 (1 inst)", rows[0][2])

    def test_failures(self):
        def mutate(fn):
            src = Src()
            sets = app_set(OLD, OLD_POD) + app_set(NEW, NEW_POD)
            fn(sets)
            src.app[SVC] = sets
            return si.action_labels(src, ident_input())["services"][SVC]
        cases = {
            "promoted missing": (lambda s: s[1].pop("k8s_node_name"), "missing"),
            "promoted differs": (lambda s: s[1].__setitem__("service_version", "x"), "differs"),
            "resource leaked": (lambda s: s[1].__setitem__("telemetry_sdk_name", "o"),
                                "not promoted) on 1 series: http_server_request_duration_seconds_count"),
            "job wrong": (lambda s: [x.__setitem__("job", "other/demo-go-api") for x in s[:3]], "job"),
            "job not ns/name": (lambda s: s[0].__setitem__("service_namespace", "x"), "service_namespace"),
            "instance mismatch": (lambda s: s[0].__setitem__("service_instance_id", "zz"), "target_info service"),
            "series job": (lambda s: s[2].__setitem__("job", "dev-otel-libs/x"), "process_cpu"),
        }
        for name, (fn, needle) in cases.items():
            with self.subTest(name):
                v = mutate(fn)
                self.assertEqual(v["verdict"], "FAIL")
                self.assertTrue(any(needle in p for i in v["instances"].values() for p in i["problems"]),
                                v["instances"])

    def test_unmeasured(self):
        v = si.action_labels(Src(), ident_input(promoted={"list": None}))["services"][SVC]
        self.assertEqual(v["verdict"], "UNMEASURED")
        src = Src()
        src.app[SVC] = [s for s in app_set(NEW, NEW_POD) if s["__name__"] != "target_info"]
        self.assertIn("no target_info", si.action_labels(src, ident_input())["services"][SVC]["instances"][NEW]["reason"])
        src.app[SVC] = [s for s in app_set(NEW, NEW_POD) if s["__name__"] == "target_info"]
        i = si.action_labels(src, ident_input())["services"][SVC]["instances"][NEW]
        self.assertEqual((i["verdict"], i["reason"]), ("UNMEASURED", "target_info only, no data series"))
        src.app[SVC] = []
        self.assertEqual(si.action_labels(src, ident_input())["services"][SVC]["verdict"], "UNMEASURED")

    def test_helpers(self):
        self.assertEqual(si.label_name("k8s.pod.name"), "k8s_pod_name")
        self.assertEqual(si.worst([]), "PASS")
        self.assertEqual(si.worst(["PASS", "UNMEASURED", "UNQUALIFIED"]), "UNQUALIFIED")
        r = SimpleNamespace(prom_series=lambda m, a, b: ["s"], prom=lambda q, at: ["p"],
                            b=SimpleNamespace(loki_query=lambda q, at: ["l"]))
        s = si.Sources(r)
        self.assertEqual((s.series("m", 1, 2), s.prom("q", 1), s.loki_query("q", 1)), (["s"], ["p"], ["l"]))


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

    def tearDown(self):
        self.tmp.cleanup()

    def run_soak(self, argv, src, tempo=False):
        args = parse(argv + ["--out-dir", str(self.d)] + ([] if tempo else ["--no-identity-tempo"]))
        out, err = io.StringIO(), io.StringIO()
        reader = SimpleNamespace(context="k8s01", prom_series=src.series, prom=src.prom,
                                 b=SimpleNamespace(loki_query=src.loki_query))
        with redirect_stdout(out), redirect_stderr(err):
            rc = sc.main_soak(None, args, T0 + 900, reader=reader)
        return rc, out.getvalue() + err.getvalue()

    def test_identity_labels_and_report(self):
        inp = self.d / "identity-input.json"
        inp.write_text(json.dumps(ident_input()))
        rc, out = self.run_soak(["soak", "identity", "--run", "r1", "--input", str(inp)], Src())
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.d / "soak-identity.json").exists())
        self.assertIn(f"PASS         nonce {SVC}", out)
        rc, out = self.run_soak(["soak", "labels", "--run", "r1", "--input", str(inp)], Src())
        self.assertEqual(rc, 0, out)
        src = Src()
        src.last[OLD] = T0 + 30
        rc, out = self.run_soak(["soak", "identity", "--run", "r1", "--input", str(inp),
                                 "--output", str(self.d / "id2.json")], src)
        self.assertEqual(rc, 1)
        rc, out = self.run_soak(["soak", "identity", "--run", "r1"], Src())
        self.assertEqual(rc, 2)
        self.assertIn("required --input", out)
        import soakextras as sx

        class G(sx.TempoGuard):
            def search(self, q, start, end, limit):
                return []
        with mock.patch.object(sx, "TempoGuard", G):
            rc, out = self.run_soak(["soak", "identity", "--run", "r1", "--input", str(inp),
                                     "--output", str(self.d / "id3.json")], Src(), tempo=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("tempo", json.loads((self.d / "id3.json").read_text()))
        report = self.d / "report.md"
        rc, out = self.run_soak(["soak", "report", "--run", "r1", "--report", str(report)], Src())
        self.assertEqual(rc, 0, out)
        text = report.read_text()
        self.assertIn("## identity", text)
        self.assertIn("## labels", text)

    def test_nothing_judged_is_not_a_pass(self):
        inp = self.d / "identity-input.json"
        empty = ident_input()
        empty["rolls"] = {"shut": empty["rolls"]["shut"]}
        inp.write_text(json.dumps(empty))
        rc, _ = self.run_soak(["soak", "identity", "--run", "r1", "--input", str(inp)], Src())
        self.assertEqual(rc, 1)
        empty["rolls"] = {}
        inp.write_text(json.dumps(empty))
        rc, _ = self.run_soak(["soak", "labels", "--run", "r1", "--input", str(inp)], Src())
        self.assertEqual(rc, 1)


class ShutInput(unittest.TestCase):
    """soakcount.action_shut with soak.py shut's input: unqualified apis, relative paths, findings."""

    def test_unqualified_and_relative_paths(self):
        import test_soakcount as tsc
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "raw").mkdir()
            ok = "lang:node,route:orders,scen:ok,class:none"
            (d / "raw" / "k6.log").write_text(tsc.k6_log({f"soak_requests{{{ok},phase:pre,outcome:expected}}": 10}))
            (d / "cal.json").write_text(json.dumps({"services": {"demo-node-api": {"qualified": True, "k": {ok: 2}}}}))
            inp = {"t1": T0, "t2": T0 + 300, "k6_log": "raw/k6.log", "kcal": "cal.json",
                   "apis": {"demo-node-api": {"old_instance": "n1", "deleted_at": T0 + 100, "finished_at": T0 + 103,
                                              "exit_code": 0, "findings": ["exit code 143"]},
                            "demo-go-api": {"old_instance": "", "unqualified": "old service_instance_id unknown",
                                            "deleted_at": T0 + 100, "finished_at": None, "grace_s": 15}}}
            path = d / "shut-input.json"
            path.write_text(json.dumps(inp))
            r = tsc.FakeReader()
            r.raw[sc.FIXED_METRIC] = [tsc.ser({"service_name": "demo-node-api", "service_instance_id": "n1"},
                                              [(T0 - 5, 0), (T0 + 90, 20)])]
            with mock.patch.object(sp, "read_sentinels", return_value=[[sp.CounterVerdict("c", "copy", "CLEAN")]]), \
                    mock.patch.object(sc, "queue_check", return_value=(True, "0")), \
                    mock.patch.object(sc, "api_grace", return_value=30.0):
                out = sc.action_shut(r, SimpleNamespace(input=path))
        node, go = out["apis"]["demo-node-api"], out["apis"]["demo-go-api"]
        self.assertEqual(node["verdict"], "PASS", node)
        self.assertEqual(node["grace_source"], "Deployment, read now")
        self.assertEqual(node["findings"], ["exit code 143"])
        self.assertEqual((go["verdict"], go["reason"]), ("UNQUALIFIED", "old service_instance_id unknown"))
        self.assertEqual(go["exit"]["verdict"], "UNQUALIFIED")
        self.assertEqual(sc.input_path(Path("/a/b/in.json"), "/abs/x"), Path("/abs/x"))

    def test_an_exemption_makes_a_report_only_count_unqualified(self):
        import test_soakcount as tsc
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            ok = "lang:go,route:orders,scen:ok,class:none"
            (d / "k6.log").write_text(tsc.k6_log({f"soak_requests{{{ok},phase:pre,outcome:expected}}": 10}))
            (d / "cal.json").write_text(json.dumps({"services": {"demo-go-api": {"qualified": False, "k": {}},
                                                                 "demo-node-api": {"qualified": True,
                                                                                   "k": {ok.replace("go", "node"): 2}}}}))
            exempt = [{"window": "deletion to finishedAt + one scrape", "response_flags": "DC", "step": 1}]
            base = {"deleted_at": T0 + 100, "finished_at": T0 + 103, "exit_code": 0, "grace_s": 15}
            inp = {"t1": T0, "t2": T0 + 300, "k6_log": "k6.log", "kcal": "cal.json",
                   "apis": {"demo-go-api": {"old_instance": "g1", **base, "edge_exemptions": exempt},
                            "demo-node-api": {"old_instance": "n1", **base,
                                              "edge_exemptions": [{"window": "replacement pod's first 30 s"}]}}}
            path = d / "shut-input.json"
            path.write_text(json.dumps(inp))
            with mock.patch.object(sp, "read_sentinels", return_value=[[sp.CounterVerdict("c", "copy", "CLEAN")]]), \
                    mock.patch.object(sc, "queue_check", return_value=(True, "0")):
                out = sc.action_shut(tsc.FakeReader(), SimpleNamespace(input=path))
        go = out["apis"]["demo-go-api"]
        self.assertEqual((go["count_verdict"], go["verdict"]), ("REPORT-ONLY", "UNQUALIFIED"))
        self.assertIn("final flush unverifiable", go["reason"])
        self.assertNotIn("edge_exemptions", out["apis"]["demo-node-api"])   # replacement-window only

    def test_an_invalid_window_is_not_judged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shut-input.json"
            path.write_text(json.dumps({"valid": False, "reasons": ["platform: guard"], "t1": T0, "t2": T0}))
            with self.assertRaises(sc.InvalidInput) as ctx:
                sc.action_shut(None, SimpleNamespace(input=path))
            self.assertIn("platform: guard", str(ctx.exception))
            args = parse(["soak", "shut", "--run", "r1", "--input", str(path), "--out-dir", tmp])
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                rc = sc.main_soak(None, args, T0, reader=SimpleNamespace(context="k8s01"))
        self.assertEqual(rc, 2)                                  # as extras refuses an invalid count window
        self.assertIn("REFUSED", err.getvalue())


class DeletedLogsCanarySource(unittest.TestCase):
    """soakextras.canary_demo: K-shut's followed logs of the deleted api pods, each its own source."""

    def test_rows(self):
        import soakextras as sx
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "raw").mkdir()
            (run / "raw" / "demo-go-api-a-b.api.deleted.log").write_text("one\n")
            (run / "trap.jsonl").write_text(json.dumps({"demo_logs": {
                "demo-go-api-c-d/api": {"path": "demo-go-api-a-b.api.deleted.log", "lines": 1, "canary_lines": 0}}})
                + "\n")
            (run / "shut-window.json").write_text(json.dumps({"deleted_logs": {
                "demo-go-api-a-b/api": {"path": "demo-go-api-a-b.api.deleted.log", "lines": 1, "canary_lines": 0},
                "demo-node-api-a-b/api": {"error": "kubectl logs -f exit 1"},
                "demo-dotnet-api-a-b/api": {"path": "x.log", "lines": 4, "canary_lines": 2, "marker": True}}}))
            rows = sx.canary_demo(run, "tok", None, T0, T0 + 1, {"ok": True, "why": []})
            by = {r["source"]: r for r in rows}
            self.assertEqual(by["deleted api pod log demo-go-api-a-b/api (shut)"]["status"], "scanned")
            self.assertEqual(by["deleted api pod log demo-node-api-a-b/api (shut)"]["status"], "unmeasured")
            dn = by["deleted api pod log demo-dotnet-api-a-b/api (shut)"]
            self.assertEqual((dn["status"], dn["hits"]), ("unmeasured", ["2 line(s)"]))
            self.assertIn("demo container log demo-go-api-c-d/api (trap)", by)
            (run / "shut-window.json").write_text(json.dumps({"deleted_logs": {}}))
            rows = sx.canary_demo(run, "tok", None, T0, T0 + 1, {"ok": True, "why": []})
            self.assertEqual(rows[-1]["status"], "unmeasured")
            (run / "shut-window.json").write_text("{bad")
            rows = sx.canary_demo(run, "tok", None, T0, T0 + 1, {"ok": True, "why": []})
            self.assertEqual(rows[-1]["source"], "deleted api pod log shut-window.json (shut)")
            (run / "shut-window.json").unlink()
            self.assertEqual(len(sx.canary_demo(run, "tok", None, T0, T0 + 1, {"ok": True, "why": []})), 1)


if __name__ == "__main__":
    unittest.main()
