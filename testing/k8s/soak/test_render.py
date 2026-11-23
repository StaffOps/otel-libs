"""Tests for the soak-run object renderers (render.py) and the k6 script
generator (k6script.py), including a recorded run of the pinned k6.

``fixtures/k6-2.3.0-run.log`` is the stderr + stdout of the pinned image
(``docker.io/grafana/k6:2.3.0@sha256:2dd4756e…``, 2026-11-23) running the
script of ``FIXTURE_PLAN`` (targets refused on 127.0.0.1:1, so every request
is status 0 / ``noanswer``; re-recorded 2026-11-23 after the review M11
cross-VU reservoir bound):
``k6 run --log-format=raw /scripts/run.js``. It proves on that version that
the per-cell sub-metrics come back in ``handleSummary`` as written and that the
bounded reservoir lines are printed. Regenerate it only for a new k6 pin.
"""

from __future__ import annotations

import json
import sys
import unittest
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import k6script  # noqa: E402
import render  # noqa: E402

RUN = "r1"
IMG = "harbor.staffops.space/development/otel-libs-fakeotlp:abc@sha256:" + "a" * 64
FIXTURE = HERE / "fixtures" / "k6-2.3.0-run.log"
FIXTURE_PLAN = k6script.K6Plan(langs=("go", "python"), rate=4, duration_s=3, timeout_s=1, p99_s=0.5,
                               reservoir=2, url="http://127.0.0.1:1/{lang}/orders")
ALLOWED_LABELS = {render.RUN_LABEL, render.ROLE_LABEL}


def all_objects() -> list[dict]:
    objs = render.every_kind(RUN, IMG, "// script", 100)
    objs += [render.soak_otlp(RUN, v) for v in render.OTLP_VARIANTS]
    objs += [render.fallback_service(RUN, lang) for lang in render.LANGS]
    return objs


def label_sets(obj: dict) -> list[dict]:
    out = [obj["metadata"]["labels"]]
    tmpl = (obj.get("spec") or {}).get("template")
    if tmpl:
        out.append(tmpl["metadata"]["labels"])
    return out


class Labels(unittest.TestCase):
    def test_every_object_carries_only_the_soak_labels(self):
        for obj in all_objects():
            for labels in label_sets(obj):
                with self.subTest(kind=obj["kind"], name=obj["metadata"]["name"]):
                    self.assertEqual(set(labels), ALLOWED_LABELS)
                    self.assertEqual(labels[render.RUN_LABEL], RUN)
                    self.assertNotIn("otel-libs/run", labels)
                    self.assertNotIn("app.kubernetes.io/component", labels)
                    self.assertEqual(obj["metadata"]["namespace"], render.NAMESPACE)

    def test_every_kind_has_one_object_of_each_renderer(self):
        objs = render.every_kind(RUN, IMG, "// s", 100)
        self.assertEqual(Counter(o["kind"] for o in objs),
                         Counter({"Service": 4, "Pod": 2, "Job": 2, "ConfigMap": 1}))
        names = [o["metadata"]["name"] for o in objs]
        self.assertEqual(len(names), len(set(zip(names, (o["kind"] for o in objs)))))

    def test_run_id_rules(self):
        for bad in ("", "UPPER", "-x", "x-", "a" * 64, "a_b", "a.b", "r1\n"):
            with self.assertRaises(render.RenderError, msg=bad):
                render.labels(bad, "k6")
        self.assertEqual(render.check_run("20261123t1200"), "20261123t1200")
        self.assertEqual(render.check_run("k1-" + "a" * 60), "k1-" + "a" * 60)  # the K1 rule (63)

    def test_as_list(self):
        self.assertEqual(render.as_list([{"a": 1}])["kind"], "List")


class Objects(unittest.TestCase):
    def test_soak_otlp_variants(self):
        copy = render.soak_otlp(RUN, "copy")["spec"]
        self.assertEqual(copy, {"type": "ExternalName", "externalName": render.COPY_HOST})
        refused = render.soak_otlp(RUN, "refused")["spec"]
        self.assertEqual(refused["type"], "ClusterIP")
        self.assertNotIn("selector", refused)
        self.assertEqual([p["port"] for p in refused["ports"]], [4317, 4318])
        for mode in ("hung", "redirect"):
            spec = render.soak_otlp(RUN, mode)["spec"]
            self.assertEqual(spec["externalName"], f"soak-fakeotlp-{mode}.dev-otel-libs.svc.cluster.local")
        with self.assertRaises(render.RenderError):
            render.soak_otlp(RUN, "blackhole")
        roles = {render.soak_otlp(RUN, v)["metadata"]["labels"][render.ROLE_LABEL] for v in render.OTLP_VARIANTS}
        self.assertEqual(len(roles), 4)

    def test_sink_pods(self):
        hung = render.sink_pod(RUN, "hung", IMG, hang_max=10)
        env = {e["name"]: e["value"] for e in hung["spec"]["containers"][0]["env"]}
        self.assertEqual(env["HANG"], "true")
        self.assertEqual(env["HANG_MAX"], "10")
        self.assertEqual(env["PLAIN"], ":4317,:4318")
        self.assertTrue(env["TLS"].startswith("127."))
        redirect = render.sink_pod(RUN, "redirect", IMG)
        self.assertNotIn("HANG", {e["name"] for e in redirect["spec"]["containers"][0]["env"]})
        c = hung["spec"]["containers"][0]
        self.assertTrue(c["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(c["volumeMounts"][0]["mountPath"], "/out")
        svc = render.sink_service(RUN, "hung")
        self.assertEqual(svc["spec"]["selector"], hung["metadata"]["labels"])
        with self.assertRaises(render.RenderError):
            render.sink_pod(RUN, "hung", "fakeotlp:latest")
        with self.assertRaises(render.RenderError):
            render.sink_name("other")

    def test_k6_job(self):
        job = render.k6_job(RUN, "loaded", 1234)
        spec = job["spec"]
        self.assertEqual(spec["activeDeadlineSeconds"], 1234)
        self.assertEqual(spec["backoffLimit"], 0)
        self.assertNotIn("ttlSecondsAfterFinished", spec)
        c = spec["template"]["spec"]["containers"][0]
        self.assertEqual(c["image"], render.K6_IMAGE)
        self.assertIn("@sha256:", c["image"])
        self.assertIn("--log-format=raw", c["args"])
        self.assertEqual(spec["template"]["spec"]["volumes"][0]["configMap"]["name"], "soak-k6-loaded")
        cm = render.k6_configmap(RUN, "loaded", "x")
        self.assertEqual(cm["data"], {"script.js": "x"})
        with self.assertRaises(render.RenderError):
            render.k6_job(RUN, "loaded", 0)
        with self.assertRaises(render.RenderError):
            render.k6_name("Bad_Window")

    def test_control_job(self):
        job = render.control_job(RUN)
        containers = {c["name"]: c["args"] for c in job["spec"]["template"]["spec"]["containers"]}
        self.assertEqual(set(containers), {"rpc-system", "rpc-system-name", "no-instance"})
        self.assertIn('rpc.system="grpc"', containers["rpc-system"])
        self.assertIn('rpc.system.name="grpc"', containers["rpc-system-name"])
        for name, args in containers.items():
            self.assertEqual(args[args.index("--child-spans") + 1], "0")
            self.assertEqual(args[args.index("--traces") + 1], "1")
            self.assertIn("soak-otlp:4317", args)
            has_instance = any(a.startswith("service.instance.id=") for a in args)
            self.assertEqual(has_instance, name != "no-instance", name)
        self.assertEqual(render.control_instances(RUN)["no-instance"], "unset")
        self.assertIn("@sha256:", job["spec"]["template"]["spec"]["containers"][0]["image"])

    def test_fallback_service(self):
        svc = render.fallback_service(RUN, "node")
        self.assertEqual(svc["spec"]["selector"], {"app.kubernetes.io/name": "demo-node-api"})
        self.assertEqual(svc["spec"]["ports"][0]["port"], 9464)
        with self.assertRaises(render.RenderError):
            render.fallback_service(RUN, "java")


class K6Plan(unittest.TestCase):
    def test_cells_and_thresholds(self):
        plan = k6script.K6Plan(langs=render.LANGS, rate=10, duration_s=7200)
        cells = plan.cells()
        self.assertEqual(len(cells), 4 * 1 * 4 * 3)  # lang x route x scenario x class
        th = k6script.thresholds(plan)
        self.assertEqual(len(th), len(cells) * 3)
        self.assertIn("soak_requests{lang:go,route:orders,scen:ok,class:none,outcome:expected}", th)
        self.assertIn("soak_requests{lang:dotnet,route:orders,scen:unhandled,class:s00,outcome:noanswer}", th)
        self.assertEqual(set(map(tuple, th.values())), {("count>=0",)})

    def test_zero_shares_drop_cells(self):
        plan = k6script.K6Plan(langs=("go",), rate=1, duration_s=1, mix={"ok": 1, "db_error": 0},
                               classes={"none": 1})
        self.assertEqual(plan.cells(), [{"lang": "go", "route": "orders", "scen": "ok", "class": "none"}])

    def test_max_vus_and_deadline_math(self):
        plan = k6script.K6Plan(langs=("go",), rate=20, duration_s=7200, timeout_s=15, p99_s=0.75)
        self.assertEqual(plan.max_vus, 315)  # ceil(20 x 15.75)
        self.assertGreaterEqual(plan.max_vus, plan.rate * (plan.timeout_s + plan.p99_s))
        self.assertEqual(plan.pre_allocated_vus, 15)
        self.assertEqual(plan.graceful_stop_s, 20)
        self.assertEqual(plan.deadline_s, 300 + 7200 + 20 + 60)
        self.assertEqual(plan.concurrent_max_vus, 315)
        self.assertEqual(plan.memory_limit_mi, 256 + 4 * 315)  # from the VU count (review H6)
        four = k6script.K6Plan(langs=render.LANGS, rate=20, duration_s=60, timeout_s=15, p99_s=0.75)
        self.assertEqual(four.memory_limit_mi, 256 + 4 * 4 * 315)
        self.assertEqual(four.reservoir_lines_max, 50 * 2 * 48)  # bounded across VUs (review M11)
        sc, _ = k6script.scenarios(plan)
        self.assertEqual(sc["go"]["maxVUs"], 315)
        self.assertEqual(sc["go"]["executor"], "constant-arrival-rate")
        self.assertEqual(sc["go"]["gracefulStop"], "20s")

    def test_phases(self):
        plan = k6script.K6Plan(langs=("go", "node"), rate=2, duration_s=0,
                               phases=(k6script.Phase("pre", 0, 600), k6script.Phase("post", 600, 300)))
        self.assertEqual(plan.total_duration_s, 900)
        self.assertEqual(plan.deadline_s, 300 + 900 + 15 + 60)
        sc, meta = k6script.scenarios(plan)
        self.assertEqual(set(sc), {"pre_go", "pre_node", "post_go", "post_node"})
        self.assertEqual(sc["post_go"]["startTime"], "600s")
        self.assertEqual(sc["post_go"]["tags"], {"lang": "go", "phase": "post"})
        self.assertEqual(meta["pre_node"]["url"], "http://demo-node-api:8080/orders")
        th = k6script.thresholds(plan)
        self.assertEqual(len(th), 2 * 2 * 4 * 3 * 3)
        self.assertIn("soak_requests{lang:go,route:orders,scen:ok,class:s01,phase:post,outcome:unexpected}", th)

    def test_validation(self):
        base = dict(langs=("go",), rate=1, duration_s=10)
        bad = [dict(langs=()), dict(langs=("java",)), dict(langs=("go", "go")), dict(rate=0),
               dict(timeout_s=16), dict(timeout_s=0), dict(p99_s=0), dict(reservoir=-1), dict(reservoir_vus=0),
               dict(mix={"ok": 0}), dict(mix={"other": 1}), dict(classes={"none": -1}),
               dict(duration_s=0), dict(url="http://x/orders"),
               dict(phases=(k6script.Phase("pre", 0, 1),)),
               dict(phases=(k6script.Phase("pre", 0, 1), k6script.Phase("post", -1, 1)))]
        for override in bad:
            with self.subTest(override=override), self.assertRaises(k6script.PlanError):
                k6script.K6Plan(**{**base, **override}).validate()

    def test_generated_script(self):
        plan = k6script.K6Plan(langs=("go", "python"), rate=3, duration_s=60)
        script = k6script.generate(plan)
        self.assertNotIn("__CFG__", script)
        self.assertNotIn("__OPTIONS__", script)
        options = json.loads(script.split("export const options = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual(options["thresholds"], k6script.thresholds(plan))
        self.assertEqual(set(options["scenarios"]), {"go", "python"})
        cfg = json.loads(script.split("const CFG = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual(cfg["mix"].count("ok"), 7)
        self.assertEqual(len(cfg["mix"]), 10)
        self.assertEqual(cfg["classes"], ["none", "s01", "s00"])
        self.assertEqual(cfg["expected"], {"ok": 201, "business_error": 422, "db_error": 409, "unhandled": 500})
        self.assertEqual(cfg["reservoir"], 50)
        self.assertEqual(cfg["reservoirVus"], 2)
        self.assertIn("exec.vu.idInTest <= CFG.reservoirVus", script)
        self.assertIn("handleSummary", script)
        self.assertIn("'00-' + traceId + '-' + parentId", script)


class PinnedK6Run(unittest.TestCase):
    """The recorded run of the pinned k6 (module docstring)."""

    @classmethod
    def setUpClass(cls):
        cls.summary, cls.records = k6script.parse_logs(FIXTURE.read_text())

    def test_every_cell_outcome_sub_metric_is_in_the_summary(self):
        counts = k6script.cell_counts(self.summary)
        self.assertEqual(set(counts), set(k6script.thresholds(FIXTURE_PLAN)))
        # About 4 req/s x 3 s per language (k6 starts 12 or 13), all refused -> noanswer.
        for lang in FIXTURE_PLAN.langs:
            total = sum(v for k, v in counts.items() if f"lang:{lang}," in k)
            noanswer = sum(v for k, v in counts.items() if f"lang:{lang}," in k and "outcome:noanswer" in k)
            self.assertIn(total, (12, 13))
            self.assertEqual(noanswer, total)
            ok_none = counts[f"soak_requests{{lang:{lang},route:orders,scen:ok,class:none,outcome:noanswer}}"]
            self.assertEqual(ok_none, 7)  # iterations 0-9 are class none, 7 of them ok
        self.assertNotIn("dropped_iterations", self.summary["metrics"])  # absent = 0

    def test_reservoir_is_bounded_per_cell_per_vu(self):
        self.assertTrue(self.records)
        per = Counter((r["lang"], r["scen"], r["class"], r["phase"], r["vu"]) for r in self.records)
        self.assertLessEqual(max(per.values()), FIXTURE_PLAN.reservoir)
        # Review M11: only VUs 1..reservoir_vus log, so a cell has at most reservoir x reservoir_vus lines.
        self.assertLessEqual(max(r["vu"] for r in self.records), FIXTURE_PLAN.reservoir_vus)
        per_cell = Counter((r["lang"], r["scen"], r["class"], r["phase"]) for r in self.records)
        self.assertLessEqual(max(per_cell.values()), FIXTURE_PLAN.reservoir * FIXTURE_PLAN.reservoir_vus)
        traced = [r for r in self.records if r["class"] != "none"]
        for r in traced:
            self.assertRegex(r["trace_id"], "^[0-9a-f]{32}$")
            self.assertRegex(r["parent_id"], "^[0-9a-f]{16}$")
        for r in self.records:
            if r["class"] == "none":
                self.assertIsNone(r["trace_id"])

    def test_parse_logs_edge_cases(self):
        text = "\n".join(["SOAKRES {bad", "SOAK_SUMMARY_BEGIN", "{not json", "SOAK_SUMMARY_END",
                          'SOAKRES {"lang":"go"}'])
        summary, records = k6script.parse_logs(text)
        self.assertIsNone(summary)
        self.assertEqual(records, [{"lang": "go"}])
        self.assertEqual(k6script.cell_counts({"metrics": {"http_reqs": {}}}), {})


if __name__ == "__main__":
    unittest.main()
