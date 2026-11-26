"""Tests for K-shut + K3 + K5 (shut.py and ``soak.py shut``): the plan, the pod
watch, the derivations, the deletion tick, the inputs (checked against
``soakcount.action_shut``) and the mode on the fakes. No cluster."""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "demo"))

import guard  # noqa: E402
import k6script  # noqa: E402
import render  # noqa: E402
import shut  # noqa: E402
import soak  # noqa: E402
import soakcount  # noqa: E402
from guard import sp  # noqa: E402
from test_loop import RUN, Base, Clock, RealClock, hooked  # noqa: E402

T0 = 1_795_000_000.0


def iso(t: float) -> str:
    return sp.iso(t)


def pod(name: str, *, phase: str = "Running", started: float | None = T0 - 900, finished: float | None = None,
        exit_code: int | None = None, deleting: float | None = None, grace: int = 15, last: dict | None = None,
        env: bool = False) -> dict:
    app = name.split("-")[2]
    st = {"running": {"startedAt": iso(started)}} if finished is None else \
        {"terminated": {"startedAt": iso(started), "finishedAt": iso(finished), "exitCode": exit_code,
                        "reason": "Completed" if exit_code == 0 else "Error"}}
    cs = {"name": app, "restartCount": 0, "state": st}
    if last:
        cs["lastState"] = {"terminated": last}
    meta = {"name": name, "namespace": sp.APP_NS, "uid": f"uid-{name}"}
    if deleting is not None:
        meta["deletionTimestamp"] = iso(deleting)
    spec = {"terminationGracePeriodSeconds": grace}
    if env:
        spec["containers"] = [{"name": app, "env": [{"name": "OTEL_EXPORTER_OTLP_HEADERS",
                                                     "value": "x-soak-canary=SECRET-CANARY"}]}]
    return {"metadata": meta, "spec": spec, "status": {"phase": phase, "containerStatuses": [cs]}}


def demo_set(langs, suffix: str, apps=("worker", "api", "backend"), **kw) -> list[dict]:
    return [pod(f"demo-{lang}-{app}-{suffix}-x", **kw) for lang in langs for app in apps]


def copy_ser(api: str, iid: str, values) -> dict:
    return {"metric": {"cluster": "k8s01", "service_name": api, "service_instance_id": iid, "pod": "soak-agent-0"},
            "values": [[t, str(v)] for t, v in values]}


class FakeOps:
    def __init__(self, langs=("go", "python"), clock=None):
        self.langs = langs
        self.clock = clock or Clock(T0)
        self.pod_list = demo_set(langs, "old1")
        self.started = None
        self.series: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.fail_delete: set[str] = set()
        self.fail_copy = False
        self.fail_k6 = False
        self.k6_calls = 0
        self.created = None
        self.k6_text = None
        self.fail_created = False
        self.fail_k6_text = False

    def now(self):
        return self.clock()

    def pods(self):
        return self.pod_list

    def deployments(self):
        return [{"metadata": {"name": f"demo-{lang}-api", "labels": {"app.kubernetes.io/name": f"demo-{lang}-api"}},
                 "spec": {"template": {"spec": {"terminationGracePeriodSeconds": 15}}}} for lang in self.langs] + \
            [{"metadata": {"name": "x"}, "spec": {}}]

    def promoted(self):
        return {"list": ["service.name", "k8s.pod.name"], "source": "fake"}

    def k6_started(self, job):
        self.k6_calls += 1
        if self.fail_k6:
            self.fail_k6 = False
            raise guard.ReadError("api unreachable")
        return self.started

    def k6_job_created(self, job):
        if self.fail_created:
            raise guard.ReadError("api unreachable")
        return self.created

    def k6_log_timestamps(self, job):
        if self.fail_k6_text:
            raise guard.ReadError("api unreachable")
        return self.k6_text

    def follow_command(self, pod_name, container, since):
        return ["follow", pod_name, container, since]

    def read_logs(self, pod_name, container, since, timestamps=False):
        if timestamps:   # the snapshot before the delete
            if pod_name.startswith("demo-python"):
                raise guard.ReadError("api unreachable")
            return 0, sp.iso(T0 + 1) + " t ok\n"
        return (0, "a\nb\nc\n") if pod_name.startswith("demo-go") else (1, "NotFound")

    def copy_series(self, t1, t2):
        if self.fail_copy:
            raise guard.ReadError("prometheus unreachable")
        return self.series

    def delete(self, lang, name):
        if lang in self.fail_delete:
            raise soak.SoakError("kubectl delete: forbidden")
        self.deleted.append((lang, name))


class FakeWatch:
    def __init__(self, term=None, pods=None):
        self.term = term or {}
        self.pods = pods or {}

    def snapshot(self):
        return self.pods

    def terminated(self, pod_name, container):
        return self.term.get(pod_name)

    def record(self):
        return {"events": 3, "restarts": 0, "gaps": [], "errors": [], "pods": 2}


def plan(**kw) -> shut.ShutPlan:
    base = dict(langs=("go", "python"), rate=4, pre_s=120, post_s=180, delta_s=30, worker_interval_s=10800)
    return shut.ShutPlan(**{**base, **kw})


class Plan(unittest.TestCase):
    def test_defaults_and_k6_phases(self):
        p = shut.ShutPlan(langs=("go",), rate=4, worker_interval_s=10800)
        p.validate()
        k = p.k6()
        self.assertEqual([(ph.name, ph.start_s, ph.duration_s) for ph in k.phases], [("pre", 0, 300), ("post", 300, 300)])
        self.assertEqual(k.deadline_s, 300 + 600 + 15 + 60)
        self.assertIn("phase:post", ",".join(k6script.threshold_key(c, "expected") for c in k.cells()))
        self.assertEqual(p.as_json()["langs"], ["go"])

    def test_refusals(self):
        for kw, why in ((dict(pre_s=30), "at least 60"), (dict(delta_s=10), "graceful stop"),
                        (dict(delta_s=150), "at most --post"), (dict(worker_interval_s=0), "worker-interval"),
                        (dict(post_s=1800, delta_s=60), "expire"), (dict(rate=0), "rate")):
            with self.subTest(kw=kw), self.assertRaises(k6script.PlanError) as ctx:
                plan(**kw).validate()
            self.assertIn(why, str(ctx.exception))


class Pods(unittest.TestCase):
    def test_summary_keeps_states_never_env(self):
        p = pod("demo-go-api-1a-x", finished=T0, exit_code=0, deleting=T0 - 5, env=True,
                last={"startedAt": iso(T0 - 99), "finishedAt": iso(T0 - 50), "exitCode": 1})
        s = shut.pod_summary(p)
        self.assertEqual((s["service"], s["app"], s["lang"], s["grace_s"]), ("demo-go-api", "api", "go", 15))
        c = s["containers"]["api"]
        self.assertEqual((c["finished_at"], c["exit_code"], c["started_at"]), (T0, 0, T0 - 900))
        self.assertEqual(c["last_terminated"]["finished_at"], T0 - 50)
        self.assertNotIn("SECRET-CANARY", json.dumps(s))
        self.assertFalse(shut.live(s))
        self.assertIsNone(shut.pod_summary({"metadata": {"name": "soak-k6-shut-x"}}))

    def test_demo_pods_and_grace(self):
        pods = demo_set(("go", "node"), "a1") + [pod("demo-go-api-b2-y", deleting=T0), pod("demo-go-api-c3-z",
                                                                                          phase="Pending")]
        got = shut.demo_pods(pods, ("go",))
        self.assertEqual([p["pod"] for p in got], ["demo-go-api-a1-x", "demo-go-backend-a1-x", "demo-go-worker-a1-x"])
        self.assertEqual(shut.deployment_grace(FakeOps(("go",)).deployments()), {"demo-go-api": 15.0, "x": None})


class FakeProc:
    def __init__(self, chunks, block=True):
        self.chunks, self.block = chunks, block
        self.gone = threading.Event()
        self.stdout = self._lines()

    def _lines(self):
        yield from self.chunks
        if self.block:
            self.gone.wait(5)

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.gone.set()


def event(kind, obj) -> str:
    return json.dumps({"type": kind, "object": obj}, indent=2) + "\n"


class Watch(unittest.TestCase):
    def test_feed_parses_multiline_documents_and_keeps_the_first_termination(self):
        w = shut.PodWatch([], spawn=None, clock=lambda: T0)
        text = event("ADDED", pod("demo-go-api-1a-x")) + event("MODIFIED", pod("demo-go-api-1a-x", finished=T0,
                                                                                exit_code=0, deleting=T0 - 3))
        rest = ""
        for ch in [text[i:i + 37] for i in range(0, len(text), 37)]:
            rest = w.feed(rest + ch)
        self.assertEqual(rest, "")
        w.handle({"type": "DELETED", "object": pod("demo-go-api-1a-x", finished=T0 + 9, exit_code=1)})
        w.handle({"type": "ADDED", "object": {"metadata": {"name": "other"}}})
        w.handle("not a dict")
        self.assertEqual(w.terminated("demo-go-api-1a-x", "api")["finished_at"], T0)
        self.assertIsNone(w.terminated("demo-go-api-1a-x", "backend"))
        self.assertIsNone(w.terminated("nope", "api"))
        self.assertEqual(w.record()["events"], 3)
        rec = json.loads(w.lines())
        self.assertEqual(rec["deleted_seen"], T0)

    def test_thread_restarts_and_stops(self):
        procs = [FakeProc([event("ADDED", pod("demo-go-api-1a-x"))], block=False),
                 FakeProc([event("MODIFIED", pod("demo-go-api-1a-x", finished=T0, exit_code=0))], block=True)]
        w = shut.PodWatch(["kubectl"], spawn=lambda cmd: procs.pop(0), restart_pause_s=0.01)
        w.start()
        for _ in range(200):
            if w.terminated("demo-go-api-1a-x", "api"):
                break
            time.sleep(0.01)
        w.stop()
        self.assertEqual(w.terminated("demo-go-api-1a-x", "api")["exit_code"], 0)
        self.assertEqual(w.record()["restarts"], 1)

    def test_restart_limit_and_spawn_error(self):
        w = shut.PodWatch(["k"], spawn=lambda cmd: FakeProc([], block=False), restart_pause_s=0.0, max_restarts=2)
        w.start()
        w._thread.join(5)
        self.assertEqual(w.record()["restarts"], 2)
        self.assertIn("not restarted", w.record()["errors"][0])
        w.stop()

        def boom(cmd):
            raise OSError("no kubectl")
        w2 = shut.PodWatch(["k"], spawn=boom).start()
        w2._thread.join(5)
        w2.stop()
        self.assertIn("watch start: OSError", w2.record()["errors"][0])
        shut.PodWatch(["k"], spawn=boom).stop()   # never started

    def test_real_child_through_spawn_watch(self):
        script = ("import json,sys,time\n"
                  f"sys.stdout.write({event('ADDED', pod('demo-go-api-1a-x', finished=T0, exit_code=0))!r})\n"
                  "sys.stdout.flush(); time.sleep(30)\n")
        w = shut.PodWatch([sys.executable, "-I", "-c", script], spawn=soak.spawn_watch).start()
        for _ in range(500):
            if w.terminated("demo-go-api-1a-x", "api"):
                break
            time.sleep(0.01)
        w.stop()
        self.assertEqual(w.terminated("demo-go-api-1a-x", "api")["finished_at"], T0)
        self.assertEqual(w.record()["restarts"], 0)

    def test_watch_command(self):
        k = soak.Kube("k8s01", kubectl=["kc"])
        self.assertEqual(k.watch_command(), ["kc", "--context", "k8s01", "-n", "dev-otel-libs", "get", "pods", "-l",
                                             soak.DEMO_SELECTOR, "--watch", "--output-watch-events", "-o", "json"])


class Derivations(unittest.TestCase):
    def test_old_instance(self):
        api = "demo-go-api"
        t1, t2 = T0, T0 + 300
        grew = copy_ser(api, "aaa", [(t1 - 30, 5), (t1 + 15, 9), (t2, 20)])
        idle = copy_ser(api, "bbb", [(t1 - 30, 7), (t1 + 15, 7)])
        self.assertEqual(shut.old_instance([grew, idle, copy_ser("demo-go-backend", "c", [(t1, 1), (t2, 9)])],
                                           api, t1, t2)[0], "aaa")
        self.assertIsNone(shut.old_instance([idle], api, t1, t2)[0])
        self.assertIn("2 service_instance_id", shut.old_instance([grew, copy_ser(api, "ccc", [(t1 - 9, 1), (t2, 3)])],
                                                                 api, t1, t2)[1])
        self.assertIn("unset", shut.old_instance([copy_ser(api, "unset", [(t1 - 9, 1), (t2, 3)])], api, t1, t2)[1])
        self.assertIn("no copy series", shut.old_instance([], api, t1, t2)[1])

    def test_post_bound(self):
        self.assertEqual(shut.post_issued_bound(4, None, T0, 9)[0], None)
        self.assertEqual(shut.post_issued_bound(4, T0 + 300, T0 + 200, 9)[0], 0)
        n, why = shut.post_issued_bound(4, T0 + 300, T0 + 340, None)
        self.assertEqual(n, 4 * 41 + 1)
        self.assertIn("rate 4/s", why)
        self.assertEqual(shut.post_issued_bound(4, T0 + 300, T0 + 340, 50)[0], 50)

    def test_k6_line_times_and_increments(self):
        text = ("2026-11-18T11:06:40.123456789Z " + k6script.RESERVOIR_PREFIX + '{"phase": "pre"}\n'
                "2026-11-18T11:06:41Z noise\n"
                "2026-11-18T11:06:42Z " + k6script.RESERVOIR_PREFIX + "{bad\n"
                "no timestamp\n"
                "2026-11-18T11:08:40.5Z " + k6script.RESERVOIR_PREFIX + '{"phase": "post"}\n'
                "2026-11-18T11:08:41Z " + k6script.RESERVOIR_PREFIX + '{"phase": "post"}\n')
        t = shut.phase_times(text)
        self.assertAlmostEqual(t["pre"], T0 + 0.123456, places=5)
        self.assertEqual(t["post"], T0 + 120.5)
        self.assertIsNone(shut.line_time("2026-13-99T99:99:99Z x"))
        self.assertEqual(shut.increments([[T0 - 5, "1"], [T0 + 5, "3"], [T0 + 9, "1"]], T0), [(T0 + 5, 2), (T0 + 9, 1)])
        self.assertEqual(shut.increments([[T0 + 5, "2"], [T0 + 6, "2"]], T0), [(T0 + 5, 2)])
        self.assertEqual(shut.increments([], T0), [])

    def test_post_totals_and_commands(self):
        cells = {"lang:go,route:orders,scen:ok,class:none,phase:post": {"expected": 3, "unexpected": 1, "noanswer": 2},
                 "lang:go,route:orders,scen:ok,class:none,phase:pre": {"expected": 9, "unexpected": 0, "noanswer": 0}}
        self.assertEqual(shut.post_totals(cells), {"go": 6})
        self.assertEqual(shut.post_totals(None), {})
        self.assertEqual(shut.commands("r1", False), [])
        self.assertIn("soak identity --run r1 --input testing/k8s/soak/runs/r1/identity-input.json",
                      shut.commands("r1", True)[1])


class Driver(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name) / "runs" / "r1"
        (self.d / "raw").mkdir(parents=True)
        self.kcal = self.d.parent / "kcal" / "soak-cal.json"
        self.kcal.parent.mkdir()
        self.kcal.write_text("{}")
        self.lines: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def shutter(self, ops, watch=None, **kw):
        return shut.Shutter(ops, plan(**kw), self.d, kcal=self.kcal, watch=watch, log=self.lines.append)

    def hooked(self, ops, sh):
        sh.before_hook()
        ops.pod_list = demo_set(ops.langs, "new2")
        sh.after_hook(T0 - 600)

    def test_after_hook_refuses_two_api_pods(self):
        ops = FakeOps(("go",))
        sh = self.shutter(ops, langs=("go",))
        sh.before_hook()
        ops.pod_list = demo_set(("go",), "a") + [pod("demo-go-api-b-y")]
        with self.assertRaises(shut.ShutError) as ctx:
            sh.after_hook(T0)
        self.assertIn("go: 2 api pods", str(ctx.exception))

    def test_tick_polls_k6_then_deletes_at_the_planned_time(self):
        ops = FakeOps()
        sh = self.shutter(ops)
        self.hooked(ops, sh)
        self.assertIsNone(sh.tick(T0))                       # no window yet
        sh.start_window(T0, (T0 - 1, T0))
        ops.created = T0 + 4.5                               # cluster 5 s ahead
        self.assertEqual(sh.tick(T0 + 1), T0 + 1 + shut.K6_POLL_S)
        ops.fail_k6 = True
        self.assertEqual(sh.tick(T0 + 2), T0 + 2 + shut.K6_POLL_S)
        self.assertIn("unreadable", self.lines[-1])
        ops.started = T0 + 15                                 # cluster clock
        self.assertEqual(sh.tick(T0 + 12), T0 + 10 + 120 + 30)  # laptop clock: started - skew + pre + delta
        self.assertEqual((sh.skew, sh.skew_error_s, sh.problems), (5.0, 1.5, []))
        ops.series = [copy_ser("demo-go-api", "old-go", [(T0 - 9, 1), (T0 + 100, 40)]),
                      copy_ser("demo-python-api", "old-py", [(T0 - 9, 1), (T0 + 100, 40)])]
        ops.k6_text = (sp.iso(T0 + 15.2) + " " + k6script.RESERVOIR_PREFIX + '{"phase": "pre"}\n'
                       + sp.iso(T0 + 136) + " " + k6script.RESERVOIR_PREFIX + '{"phase": "post"}\n')
        ops.clock.t = T0 + 165
        self.assertIsNone(sh.tick(T0 + 165))
        self.assertIsNone(sh.tick(T0 + 170))                 # done
        self.assertEqual(ops.deleted, [("go", "demo-go-api-new2-x"), ("python", "demo-python-api-new2-x")])
        rec = sh.deletions["go"]
        self.assertEqual((rec["old_instance"], rec["deleted_at"], rec["delete_delay_s"], rec["grace_s"],
                          rec["grace_deployment_s"]), ("old-go", T0 + 165, 5.0, 15, 15.0))
        self.assertEqual(sh.post_start_lo, T0 + 135)        # max(startedAt + pre, post line - timeout, ...)
        self.assertEqual(sh.phase["deleted_after_post_s"], 35.0)
        self.assertEqual(sh.problems, [])

    def test_skew_and_phase_problems(self):
        cases = [
            (dict(created=T0 + 30), "clock skew +30.0"),
            (dict(fail_created=True), "clock skew unmeasured"),
            (dict(k6_text=None), "no phase=post line"),
            (dict(fail_k6_text=True), "no phase=post line"),
            (dict(k6_text=sp.iso(T0 + 400) + " " + k6script.RESERVOIR_PREFIX + '{"phase": "post"}\n'),
             "before the post phase"),
        ]
        for attrs, needle in cases:
            with self.subTest(needle):
                ops = FakeOps(("go",))
                ops.created = T0
                ops.__dict__.update(attrs)
                sh = self.shutter(ops, langs=("go",))
                self.hooked(ops, sh)
                sh.start_window(T0, (T0, T0))
                ops.started = T0
                sh.tick(T0)
                ops.clock.t = T0 + 160
                sh.tick(T0 + 160)
                self.assertTrue(any(needle in p for p in sh.problems), sh.problems)

    def test_late_delete_reasons(self):
        ops = FakeOps(("go",))
        ops.created, ops.started = T0, T0
        ops.k6_text = sp.iso(T0 + 121) + " " + k6script.RESERVOIR_PREFIX + '{"phase": "post"}\n'
        sh = self.shutter(ops, langs=("go",))
        self.hooked(ops, sh)
        sh.start_window(T0, (T0, T0))
        sh.tick(T0)
        ops.clock.t = T0 + 150 + 61 + 40          # 101 s after the planned time, 131 s after the post start
        sh.tick(ops.clock.t)
        self.assertTrue(any("late (> one loop interval)" in p for p in sh.problems), sh.problems)
        self.assertTrue(any("later than post - 60 s" in p for p in sh.problems), sh.problems)

    def test_copy_read_and_delete_failures(self):
        ops = FakeOps()
        sh = self.shutter(ops)
        self.hooked(ops, sh)
        sh.start_window(T0)
        ops.started = T0
        ops.fail_copy = True
        ops.fail_delete = {"python"}
        sh.tick(T0 + 200)
        self.assertIsNone(sh.deletions["go"]["old_instance"])
        self.assertIn("copy read failed", sh.deletions["go"]["old_instance_source"])
        self.assertIn("forbidden", sh.deletions["python"]["error"])
        self.assertIn("NOT deleted", self.lines[-1])
        inp = sh.shut_input(T0, T0 + 600, None, {}, ["api pod delete failed: python"])
        self.assertEqual((inp["valid"], inp["reasons"]), (False, ["api pod delete failed: python"]))
        self.assertIn("old service_instance_id unknown", inp["apis"]["demo-go-api"]["unqualified"])
        self.assertIn("forbidden", inp["apis"]["demo-python-api"]["unqualified"])
        self.assertEqual(inp["apis"]["demo-go-api"]["old_instance"], "")
        ident = sh.identity_input(T0, T0 + 600, {})
        self.assertEqual(list(ident["rolls"]["shut"]["services"]), ["demo-go-api"])

    def test_inputs(self):
        ops = FakeOps()
        old_api = "demo-go-api-new2-x"
        watch = FakeWatch({old_api: {"finished_at": T0 + 170, "exit_code": 143, "reason": "Error"},
                           "demo-go-api-old1-x": {"finished_at": T0 - 610, "exit_code": 0}})
        sh = self.shutter(ops, watch=watch)
        self.hooked(ops, sh)
        sh.start_window(T0)
        ops.started = T0
        ops.series = [copy_ser("demo-go-api", "old-go", [(T0 - 9, 1), (T0 + 100, 40)]),
                      copy_ser("demo-python-api", "old-py", [(T0 - 9, 1), (T0 + 100, 40)])]
        ops.clock.t = T0 + 150
        sh.tick(T0 + 150)
        ops.pod_list = demo_set(ops.langs, "new2", apps=("worker", "backend")) + \
            [pod("demo-go-api-new3-x", started=T0 + 160), pod("demo-python-api-new3-x", started=T0 + 160)]
        new_api = sh.new_api_pods()
        self.assertEqual([p["pod"] for p in new_api["go"]], ["demo-go-api-new3-x"])
        cells = {"lang:go,route:orders,scen:ok,class:none,phase:post": {"expected": 700, "unexpected": 0,
                                                                         "noanswer": 0}}
        inp = sh.shut_input(T0, T0 + 600, cells, new_api)
        go = inp["apis"]["demo-go-api"]
        self.assertEqual((go["old_instance"], go["finished_at"], go["exit_code"]), ("old-go", T0 + 170, 143))
        self.assertEqual(go["post_before_finish"], 4 * (170 + 1 - 120) + 1)
        self.assertEqual(go["deleted_at"], T0 + 150)            # skew unmeasured: no conversion
        self.assertIn("clock skew unmeasured", inp["clock"]["note"] + " ".join(sh.problems))
        self.assertIn("exit code 143", go["findings"][0])
        self.assertNotIn("unqualified", go)
        py = inp["apis"]["demo-python-api"]
        self.assertIsNone(py["post_before_finish"])
        self.assertIn("finishedAt not seen", py["findings"][0])
        self.assertEqual(inp["kcal"], "../kcal/soak-cal.json")
        self.assertEqual(inp["k6_log"], "raw/soak-k6-shut.log")
        ident = sh.identity_input(T0, T0 + 600, new_api)
        nonce = ident["rolls"]["nonce"]["services"]["demo-go-api"]
        self.assertEqual(nonce["old"][0], {"pod": "demo-go-api-old1-x", "uid": "uid-demo-go-api-old1-x",
                                           "started_at": T0 - 900, "finished_at": T0 - 610, "exit_code": 0,
                                           "deletion": None, "deletion_source": None})
        self.assertIn("not a pure restart", ident["rolls"]["nonce"]["what"])
        self.assertEqual([p["pod"] for p in nonce["new"]], ["demo-go-api-new2-x"])
        s = ident["rolls"]["shut"]
        self.assertEqual((s["at"], s["judged"]), (T0 + 150, False))
        self.assertEqual(s["services"]["demo-go-api"]["new"][0]["pod"], "demo-go-api-new3-x")
        self.assertEqual(ident["promoted"]["source"], "fake")
        self.assertEqual(ident["watch"]["events"], 3)

    def test_window_ended_before_the_deletion(self):
        ops = FakeOps(("go",))
        sh = self.shutter(ops, langs=("go",))
        self.hooked(ops, sh)
        inp = sh.shut_input(T0, T0 + 60, None, {})
        self.assertIn("window ended before the deletion", inp["apis"]["demo-go-api"]["unqualified"])
        self.assertIsNone(sh.identity_input(T0, T0 + 60, {})["rolls"]["shut"]["at"])

    def test_shut_input_is_what_action_shut_reads(self):
        """Schema check: the written input judged by soakcount.action_shut."""
        import test_soakcount as tsc
        ops = FakeOps(("python",))
        old_api = "demo-python-api-new2-x"
        sh = self.shutter(ops, langs=("python",), watch=FakeWatch({old_api: {"finished_at": T0 + 160, "exit_code": 0}}))
        self.hooked(ops, sh)
        t1 = T0
        sh.start_window(t1, (t1 - 2, t1))
        ops.created = t1 - 1 + 2                   # cluster 2 s ahead
        ops.started = t1
        ops.series = [copy_ser("demo-python-api", "old-py", [(t1 - 9, 100), (t1 + 100, 120)])]
        ops.clock.t = t1 + 150
        sh.tick(t1 + 150)
        ok = "lang:python,route:orders,scen:ok,class:none"
        counts = {f"soak_requests{{{ok},phase:pre,outcome:expected}}": 30,
                  f"soak_requests{{{ok},phase:post,outcome:expected}}": 30,
                  f"soak_requests{{{ok},phase:post,outcome:noanswer}}": 2}
        (self.d / "raw" / "soak-k6-shut.log").write_text(tsc.k6_log(counts))
        self.kcal.write_text(json.dumps({"services": {"demo-python-api": {"qualified": True, "k": {ok: 4}}}}))
        cells = soakcount.cell_table(counts)
        inp = sh.shut_input(t1, t1 + 400, cells, {})
        path = self.d / "shut-input.json"
        path.write_text(json.dumps(inp))
        r = tsc.FakeReader()
        # 30 pre x 4 = 120 counted; finishedAt 40 s after the post start at 4/s: 4 x 41 + 1 issued, capped at 32.
        r.raw[soakcount.FIXED_METRIC] = [{**copy_ser("demo-python-api", "old-py",
                                                    [(t1 - 9, 0), (t1 + 60, 60), (t1 + 165, 120)])}]
        with mock.patch.object(sp, "read_sentinels", return_value=[[sp.CounterVerdict("c", "copy", "CLEAN")]]), \
                mock.patch.object(soakcount, "queue_check", return_value=(True, "0")), \
                mock.patch.object(soakcount, "api_grace", side_effect=AssertionError("grace is recorded")):
            out = soakcount.action_shut(r, SimpleNamespace(input=path))
        v = out["apis"]["demo-python-api"]
        self.assertEqual(v["verdict"], "PASS", v)
        self.assertEqual((v["lo"], v["hi"]), (120.0, 120.0 + 32 * 4 + 2 * 4))
        self.assertEqual((v["exit"]["verdict"], v["exit"]["seconds"]), ("PASS", 8.0))   # 160 - (150 + 2)
        self.assertEqual(v["exit"]["grace_s"], 15.0)
        self.assertEqual(v["grace_source"], "recorded (pod spec)")
        self.assertIn("32 post answers", v["post_basis"])


class LoopTick(Base):
    def test_tick_runs_between_reads(self):
        clock = Clock(T0)
        calls = []

        def tick(now):
            calls.append(now)
            return T0 + 90 if now < T0 + 90 else None
        loop = soak.Loop(RUN, self.kube, None, guard.GuardConfig(), self.d, clock=clock, sleep=clock.sleep,
                         interval=60, log=self.lines.append)
        ok = guard.GuardRead(T0, [guard.Item("x", "ok")])
        with mock.patch.object(guard, "window_read", return_value=ok):
            res = loop.window("w", 150, gate=False, tick=tick)
        self.assertTrue(res.valid)
        self.assertIn(T0 + 90, calls)
        self.assertEqual(res.reads, 4)

    def test_wait_without_tick_and_a_past_wake(self):
        clock = Clock(T0)
        loop = soak.Loop(RUN, self.kube, None, guard.GuardConfig(), self.d, clock=clock, sleep=clock.sleep)
        loop._wait(T0 + 10, None)
        self.assertEqual(clock.t, T0 + 10)
        seen = []
        loop._wait(T0 + 12, lambda now: seen.append(now) or now - 5)   # a past wake: min sleep, no busy loop
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(clock.t, T0 + 12)


class KubeWrites(Base):
    def test_delete_api_pod_by_label_and_name(self):
        with self.cluster() as c:
            c.edit(lambda st: st["objects"].append({"kind": "Pod", "metadata": {
                "name": "demo-go-api-1a-x", "namespace": sp.APP_NS,
                "labels": {"app.kubernetes.io/component": "demo", "app.kubernetes.io/name": "demo-go-api"}}}))
            self.kube.delete_api_pod("go", "demo-go-api-1a-x")
            self.assertNotIn(("Pod", sp.APP_NS, "demo-go-api-1a-x"), c.keys())
            argv = c.verbs()[-1]
            self.assertIn("--field-selector", argv)
            self.assertIn("metadata.name=demo-go-api-1a-x", argv)
            self.assertIn("--wait=false", argv)
            with self.assertRaises(soak.SoakError) as ctx:   # nothing matched: not "deleted"
                self.kube.delete_api_pod("go", "demo-go-api-1a-x")
            self.assertIn("not deleted", str(ctx.exception))
        for lang, name in (("go", "demo-go-backend-1a-x"), ("go", "demo-python-api-1a-x"), ("ruby", "x")):
            with self.assertRaises(soak.SoakError):
                self.kube.delete_api_pod(lang, name)
        self.assertEqual(self.kube.follow_command("demo-go-api-1a-x", "api", "T")[-4:],
                         ["api", "--timestamps", "-f", "--since-time=T"])
        with self.assertRaises(soak.SoakError):
            self.kube.follow_command("x", "api", "T")


class Ops(Base):
    def ops(self, reader):
        return soak.ShutOps(RUN, self.kube, None, reader, self.d)

    def test_reads(self):
        r = SimpleNamespace()
        answers = {}

        def kget(args):
            key = args[0]
            if key not in answers:
                raise guard.ReadError(f"{key} unreadable")
            return answers[key]
        r.kget = kget
        ops = self.ops(r)
        answers["deployments"] = {"items": [{"metadata": {"name": "a"}}]}
        self.assertEqual(ops.deployments(), [{"metadata": {"name": "a"}}])
        answers["pods"] = {"items": [{"status": {"containerStatuses": [{"state": {"waiting": {}}}]}},
                                     {"status": {"containerStatuses": [{"state": {"running": {
                                         "startedAt": iso(T0)}}}]}}]}
        self.assertEqual(ops.k6_started("soak-k6-shut"), T0)
        answers["pods"] = {"items": []}
        self.assertIsNone(ops.k6_started("soak-k6-shut"))
        answers["jobs"] = {"metadata": {"creationTimestamp": iso(T0)}}
        self.assertEqual(ops.k6_job_created("soak-k6-shut"), T0)
        answers["prometheus"] = {"spec": {"otlp": {"promoteResourceAttributes": ["service.name"],
                                                   "translationStrategy": "U"}}}
        self.assertEqual(ops.promoted()["source"], "prometheus/kps spec.otlp (live)")
        # fallback: the newest passing K1 snapshot
        snap = self.d.parent / "k1" / "snapshot.json"
        snap.parent.mkdir()
        snap.write_text(json.dumps({"verdict": "PASS", "prometheus": {"promoteResourceAttributes": ["cluster"]}}))
        bad = self.d.parent / "k0" / "snapshot.json"
        bad.parent.mkdir()
        bad.write_text("{not json")
        answers["prometheus"] = {"spec": {}}
        got = ops.promoted()
        self.assertEqual(got["list"], ["cluster"])
        self.assertIn("K1 snapshot k1", got["source"])
        del answers["prometheus"]
        snap.write_text(json.dumps({"verdict": "FAIL", "prometheus": {"promoteResourceAttributes": ["cluster"]}}))
        got = ops.promoted()
        self.assertIsNone(got["list"])
        self.assertIn("unreadable", got["source"])

    def test_delete_and_log(self):
        ops = self.ops(SimpleNamespace())
        with self.cluster() as c:
            c.logs({"job/soak-k6-shut": "SOAK_SUMMARY_BEGIN\n{}\nSOAK_SUMMARY_END\n"})
            self.assertIn("SOAK_SUMMARY_BEGIN", ops.save_k6_log("soak-k6-shut"))
            self.assertIsNone(ops.save_k6_log("soak-k6-other"))
            self.assertIn("SOAK_SUMMARY_BEGIN", ops.k6_log_timestamps("soak-k6-shut"))
            self.assertIsNone(ops.k6_log_timestamps("soak-k6-other"))
            self.assertIn("--timestamps", c.verbs()[-1])
            c.logs({"pod/demo-go-api-1a-x:api": "l1\n"})
            self.assertEqual(ops.read_logs("demo-go-api-1a-x", "api", "T"), (0, "l1\n"))
            self.assertIn("-f", ops.follow_command("demo-go-api-1a-x", "api", "T"))
            c.edit(lambda st: st["objects"].append({"kind": "Pod", "metadata": {
                "name": "demo-go-api-1a-x", "namespace": sp.APP_NS,
                "labels": {"app.kubernetes.io/component": "demo", "app.kubernetes.io/name": "demo-go-api"}}}))
            ops.delete("go", "demo-go-api-1a-x")
        self.assertTrue((self.d / "raw" / "soak-k6-shut.log").exists())
        self.assertTrue((self.d / "raw" / "soak-k6-other.log.err").exists())


def edge_series(source: str, values, flags: str = "DC", dest: str = "soak-agent") -> dict:
    return {"metric": {"cluster": "k8s01", "source_workload": source, "destination_workload": dest,
                       "response_flags": flags}, "values": [[t, str(v)] for t, v in values]}


class EdgeExemption(Driver):
    def deleted(self, finished=T0 + 108, replacement=T0 + 112):
        ops = FakeOps(("go", "python"))
        pods = {"demo-go-api-new3-x": {"service": "demo-go-api", "containers": {"api": {"started_at": replacement}}},
                "demo-go-api-new2-x": {"service": "demo-go-api", "containers": {"api": {"started_at": T0 - 600}}},
                "demo-python-api-r-y": {"service": "demo-python-api", "containers": {"api": {"started_at": None}}}}
        term = {"demo-go-api-new2-x": {"finished_at": finished, "exit_code": 0}} if finished else {}
        sh = self.shutter(ops, watch=FakeWatch(term, pods))
        self.hooked(ops, sh)
        sh.skew = 0.0
        sh.deletions = {"go": {"deleted_at": T0 + 100}, "python": {"deleted_at": None}}
        return sh

    def read(self):
        return guard.GuardRead(T0 + 200, [guard.Item(shut.EDGE_NAME, "breach", "NOISY: increase 2"),
                                          guard.Item("guard (b) edge soak-agent->otel-agent-collector", "breach", "x")])

    def run_filter(self, sh, series, verdict="NOISY", increase=None, at=T0 + 200, lookback=120, fail=False):
        """The filter with the guard's own edge verdict faked (soakplatform.read_edges)."""
        start = at - lookback
        if increase is None:
            increase = sum(st for x in series for _, st in shut.increments(x["values"], start))
        v = sp.CounterVerdict("edge demo->soak-agent", "guard", verdict, increase)
        self.edges_start = None

        def read_edges(r, now, starts):
            if fail:
                raise guard.ReadError("down")
            self.edges_start = starts[0]
            return [[v, sp.CounterVerdict("edge soak-agent->otel-agent-collector", "guard", "NOISY", 1)]], []
        with mock.patch.object(sp, "read_edges", read_edges):
            return sh.exempt.filter(self.read(), SimpleNamespace(prom=lambda q, t: series), at, lookback)

    def test_exempt_inside_the_deletion_and_replacement_windows(self):
        sh = self.deleted()
        series = [edge_series("demo-go-api", [(T0 + 60, 0), (T0 + 130, 1)], flags="DC"),
                  edge_series("demo-go-api", [(T0 + 60, 0), (T0 + 150, 1)], flags="CONNECT")]
        read = self.run_filter(sh, series)
        self.assertEqual(read.items[0].state, "finding", read.items[0].detail)
        self.assertEqual(read.items[1].state, "breach")      # the other edge stays strict
        self.assertEqual(self.edges_start, T0 + 80)          # the guard's own range
        self.assertEqual([r["window"] for r in sh.exempt.records],
                         ["deletion to finishedAt + one scrape", "replacement pod's first 30 s + one scrape"])
        self.assertEqual(sh.exempt.flags_seen, {"DC", "CONNECT"})
        self.assertEqual(len(sh.exempt.for_api("demo-go-api")), 2)
        again = self.run_filter(sh, series, at=T0 + 260)     # overlapping read: not recounted
        self.assertEqual((again.items[0].state, sh.exempt.used), ("finding", 2))

    def test_only_a_noisy_verdict_with_the_exact_increase_is_downgraded(self):
        sh = self.deleted()
        series = [edge_series("demo-go-api", [(T0 + 60, 0), (T0 + 130, 1)])]
        self.run_filter(sh, series)                          # seen is now set
        for verdict, inc in (("UNKNOWN", 0), ("ABSENT", 0), ("NOISY", 3)):
            with self.subTest(verdict=verdict, inc=inc):
                read = self.run_filter(sh, series, verdict=verdict, increase=inc)
                self.assertEqual(read.items[0].state, "breach", read.items[0].detail)
        read = self.run_filter(sh, [], verdict="NOISY", increase=1)   # guard counted, nothing re-read
        self.assertEqual(read.items[0].state, "breach")

    def test_an_old_unexamined_increment_stays_a_breach(self):
        sh = self.deleted()
        # an increment 150 s before the read, never examined; the guard counted it (wider range)
        series = [edge_series("demo-go-api", [(T0 + 40, 0), (T0 + 50, 1), (T0 + 130, 2)])]
        read = self.run_filter(sh, series, increase=2)       # the re-read range sees only 1
        self.assertEqual(read.items[0].state, "breach")
        self.assertEqual(sh.exempt.records, [])              # nothing committed

    def test_flags_windows_sources_and_the_cap(self):
        sh = self.deleted()
        for series in ([edge_series("demo-go-api", [(T0 + 60, 0), (T0 + 190, 1)])],                 # no window
                       [edge_series("demo-python-api", [(T0 + 60, 0), (T0 + 105, 1)])],             # not deleted
                       [edge_series("demo-go-worker", [(T0 + 60, 0), (T0 + 105, 1)])],              # not the api
                       [edge_series("demo-go-api", [(T0 + 60, 0), (T0 + 105, 1)], flags="CONNECT")],  # connect, deletion
                       [edge_series("demo-go-api", [(T0 + 60, 0), (T0 + 150, 1)], flags="DC")]):     # DC, replacement
            read = self.run_filter(sh, series, lookback=150)
            self.assertEqual(read.items[0].state, "breach")
            self.assertIn("not downgraded", read.items[0].detail)
        sh = self.deleted(finished=None)                      # finishedAt unknown: deleted_at + grace
        read = self.run_filter(sh, [edge_series("demo-go-api", [(T0 + 60, 0), (T0 + 105, 5)])], lookback=150)
        self.assertEqual(read.items[0].state, "breach")       # 5 > the cap of 3
        read = self.run_filter(sh, [], fail=True)
        self.assertIn("not read", read.items[0].detail)
        clean = guard.GuardRead(T0, [guard.Item(shut.EDGE_NAME, "ok", "")])
        self.assertIs(sh.exempt.filter(clean, None, T0, 120), clean)

    def test_partly_exempt_stays_a_breach(self):
        sh = self.deleted()
        series = [edge_series("demo-go-api", [(T0 + 60, 0), (T0 + 105, 1), (T0 + 190, 2)])]
        read = self.run_filter(sh, series, lookback=150)
        self.assertEqual(read.items[0].state, "breach")
        self.assertIn("outside 1", read.items[0].detail)
        self.assertEqual(sh.exempt.records, [])

    def test_replacement_starts(self):
        sh = self.deleted()
        self.assertEqual(sh.replacement_starts("go"), [T0 + 112])
        self.assertEqual(sh.replacement_starts("python"), [])
        sh.watch = None
        self.assertEqual(sh.replacement_starts("go"), [])

    def test_window_lookback_is_the_guards(self):
        self.assertEqual(guard.window_lookback(guard.GuardConfig(interval_s=60)), 120)


class Follow(Driver):
    def test_bounded_follow_and_cross_check(self):
        f = shut.LogFollow(["k"], lambda cmd: FakeProc(["l1\n", "l2\n"], block=True), 0.05,
                           lambda: (0, "l1\nl2\nl3\n")).start()
        self.assertTrue(f.join(5))
        self.assertTrue(f.bounded)
        self.assertEqual((len(f.lines), f.cross), (2, {"lines": 3}))

    def test_errors(self):
        def boom(cmd):
            raise OSError("no kubectl")
        f = shut.LogFollow(["k"], boom, 1, lambda: (0, "")).start()
        f.join(5)
        self.assertIn("OSError", f.error)

        class Fail(FakeProc):
            def wait(self, timeout=None):
                return 1
        f = shut.LogFollow(["k"], lambda cmd: Fail([], block=False), 5, lambda: (1, "gone")).start()
        f.join(5)
        self.assertEqual((f.error, f.cross), ("kubectl logs -f exit 1", {"error": "pod gone (not read)"}))

        def bad_read():
            raise RuntimeError("x")
        f = shut.LogFollow(["k"], lambda cmd: FakeProc([], block=False), 5, bad_read).start()
        f.join(5)
        self.assertEqual(f.cross, {"error": "RuntimeError"})
        self.assertFalse(shut.LogFollow(["k"], None, 1, bad_read).join(0))

    def test_saved_through_the_marker_path(self):
        ops = FakeOps(("go", "python"))
        sh = shut.Shutter(ops, plan(), self.d, kcal=self.kcal, log=self.lines.append, log_spawn=lambda cmd: FakeProc(
            ["t SECRET-canary line\n", "t ok\n"] if "demo-go-api-new2-x" in cmd else ["t ok\n"], block=False))
        self.hooked(ops, sh)
        sh.start_window(T0, (T0, T0))
        ops.started, ops.created = T0, T0
        ops.clock.t = T0 + 160
        sh.tick(T0)
        sh.tick(T0 + 160)
        sh.follows["node"] = shut.LogFollow(["k"], lambda cmd: FakeProc([]), 1, lambda: (0, ""))
        sh.api["node"] = shut.pod_summary(pod("demo-node-api-a-b"))
        written = {}
        rec = sh.save_deleted_logs("secret-CANARY", lambda p, t: written.__setitem__(p.name, t))
        go = rec["demo-go-api-new2-x/api"]
        self.assertEqual((go["marker"], go["canary_lines"], go["lines"]), (True, 1, 3))   # snapshot 1 + follow 2
        self.assertNotIn("SECRET", written["demo-go-api-new2-x.api.deleted.log"])
        py = rec["demo-python-api-new2-x/api"]
        self.assertEqual((py["marker"], py["cross_check"]), (False, {"error": "pod gone (not read)"}))
        self.assertIn("still running", rec["demo-node-api-a-b/api"]["error"])
        self.assertNotIn("finding", rec["demo-go-api-new2-x/api"])   # the cross-check's 3 lines = the saved 3


class SnapshotMerge(Driver):
    def test_merge_logs(self):
        a, b, c = sp.iso(T0) + " a\n", sp.iso(T0 + 1) + " b\n", sp.iso(T0 + 2) + " c\n"
        b2 = sp.iso(T0 + 1) + " b2\n"
        self.assertEqual(shut.merge_logs([a, b], [a, b, b2, c, "no ts\n"]), [a, b, b2, c, "no ts\n"])
        self.assertEqual(shut.merge_logs(["x\n"], ["y\n"]), ["x\n", "y\n"])
        self.assertEqual(shut.merge_logs([], [a]), [a])

    def test_snapshot_kept_when_the_follow_fails_and_both_failing_is_unmeasured(self):
        ops = FakeOps(("go", "python"))

        def spawn(cmd):
            raise OSError("no kubectl")
        sh = shut.Shutter(ops, plan(), self.d, kcal=self.kcal, log=self.lines.append, log_spawn=spawn)
        self.hooked(ops, sh)
        sh.start_window(T0, (T0, T0))
        ops.started, ops.created = T0, T0
        ops.clock.t = T0 + 160
        sh.tick(T0)
        sh.tick(T0 + 160)
        written = {}
        rec = sh.save_deleted_logs("", lambda p, t: written.__setitem__(p.name, t))
        go = rec["demo-go-api-new2-x/api"]
        self.assertEqual((go["lines"], go["snapshot_lines"], go["follow_lines"]), (1, 1, 0))
        self.assertIn("snapshot before the delete only", go["finding"])
        self.assertIsNone(go["canary_lines"])                 # no canary file: not counted
        self.assertIn("snapshot", rec["demo-python-api-new2-x/api"]["error"])
        self.assertTrue(sh.snapshots["go"]["follow_first_line_before_delete"])   # ended: not waited 2 s

    def test_follow_waited_for_before_the_delete(self):
        ops = FakeOps(("go",))
        order = []
        real_delete = ops.delete

        def delete(lang, name):
            order.append(("delete", sh.follows[lang].running.is_set()))
            real_delete(lang, name)
        ops.delete = delete
        sh = shut.Shutter(ops, plan(langs=("go",)), self.d, kcal=self.kcal, log=self.lines.append,
                          log_spawn=lambda cmd: FakeProc([sp.iso(T0 + 5) + " late\n"], block=False))
        self.hooked(ops, sh)
        sh.start_window(T0, (T0, T0))
        ops.started, ops.created = T0, T0
        ops.clock.t = T0 + 160
        sh.tick(T0)
        sh.tick(T0 + 160)
        self.assertEqual(order, [("delete", True)])
        rec = sh.save_deleted_logs("c", lambda p, t: None)["demo-go-api-new2-x/api"]
        self.assertEqual((rec["lines"], rec["follow_first_line_before_delete"]), (2, True))


class Clocks(Driver):
    def test_identity_deletion_on_the_cluster_clock(self):
        ops = FakeOps(("go",))
        sh = self.shutter(ops, langs=("go",))
        self.hooked(ops, sh)
        sh.skew = 4.0
        sh.deletions = {"go": {"deleted_at": T0 + 100}}
        ident = sh.identity_input(T0, T0 + 600, {})
        old = ident["rolls"]["shut"]["services"]["demo-go-api"]["old"][0]
        self.assertEqual((old["deletion"], ident["rolls"]["shut"]["at"]), (T0 + 104, T0 + 104))
        self.assertIn("skew", old["deletion_source"])
        sh.watch = FakeWatch(pods={"demo-go-api-new2-x": {"deletion_requested": T0 + 103},
                                   "demo-go-api-old1-x": {"deletion_requested": T0 - 620}})
        ident = sh.identity_input(T0, T0 + 600, {})
        old = ident["rolls"]["shut"]["services"]["demo-go-api"]["old"][0]
        self.assertEqual(old["deletion"], T0 + 104)          # K-shut's own request wins over the watch
        nonce = ident["rolls"]["nonce"]["services"]["demo-go-api"]["old"][0]
        self.assertEqual((nonce["deletion"], nonce["deletion_source"]),
                         (T0 - 620, "watch: first deletionTimestamp - its grace"))
        sh.skew = None
        old = sh.identity_input(T0, T0 + 600, {})["rolls"]["shut"]["services"]["demo-go-api"]["old"][0]
        self.assertEqual(old["deletion"], T0 + 103)          # no skew: the watch's request time

    def test_watch_keeps_the_delete_request_time(self):
        w = shut.PodWatch([], spawn=None, clock=lambda: T0)
        first = pod("demo-go-api-1a-x", deleting=T0 + 15)
        first["metadata"]["deletionGracePeriodSeconds"] = 15
        last = pod("demo-go-api-1a-x", deleting=T0 + 4, finished=T0 + 2, exit_code=0)
        last["metadata"]["deletionGracePeriodSeconds"] = 0     # the kubelet's final grace-0 delete
        w.handle({"type": "MODIFIED", "object": first})
        w.handle({"type": "DELETED", "object": last})
        self.assertEqual(w.snapshot()["demo-go-api-1a-x"]["deletion_requested"], T0)


# kubectl logs --timestamps of the k6 Job as the node writes it (k8s01, 2026-11-26:
# the CRI timestamp with the node's -03:00 offset), lines from
# runs/kshut-20261126/raw/soak-k6-shut.stop.log.
K6_TS_EXCERPT = (
    "2026-11-26T16:49:33.912000123-03:00 \n"
    "2026-11-26T16:49:33.912000456-03:00          /\\      Grafana   /‾‾/  \n"
    "2026-11-26T16:49:34.101000000-03:00      execution: local\n"
    '2026-11-26T16:49:35.230477120-03:00 SOAKRES {"lang":"go","route":"orders","scen":"ok","class":"none",'
    '"phase":"pre","trace_id":null,"parent_id":null,"status":201,"vu":3}\n'
    '2026-11-26T16:49:35.231000001-03:00 SOAKRES {"lang":"python","route":"orders","scen":"ok","class":"none",'
    '"phase":"pre","trace_id":null,"parent_id":null,"status":201,"vu":9}\n'
    '2026-11-26T16:54:35.240112233-03:00 SOAKRES {"lang":"dotnet","route":"orders","scen":"ok","class":"none",'
    '"phase":"post","trace_id":null,"parent_id":null,"status":201,"vu":201}\n')


class RealLogFormat(unittest.TestCase):
    def test_node_offset_timestamps(self):
        """Run kshut-20261126: a Z-only pattern matched no line of the real log."""
        t = shut.phase_times(K6_TS_EXCERPT)
        self.assertAlmostEqual(t["pre"], sp.parse_ts("2026-11-26T19:49:35.230477Z"), places=5)
        self.assertAlmostEqual(t["post"], sp.parse_ts("2026-11-26T19:54:35.240112Z"), places=5)
        self.assertEqual(shut.line_time("2026-11-26T19:54:35Z x"), sp.parse_ts("2026-11-26T19:54:35Z"))
        self.assertAlmostEqual(shut.line_time("2026-11-26T22:54:35.5+03:00 x"), sp.parse_ts("2026-11-26T19:54:35Z") + 0.5)


def zt_hit(src="soak-k6-shut-vktn4", dst="demo-go-api-new2-x", t="2026-11-18T11:08:31.455517Z", src_ns="dev-otel-libs"):
    return json.dumps({"level": "error", "time": t, "scope": "access", "message": "connection complete",
                       "src.workload": src, "src.namespace": src_ns, "dst.addr": "10.244.73.138:15008",
                       "dst.workload": dst, "dst.namespace": "dev-otel-libs", "direction": "inbound",
                       "error": "connection failed: Connection refused (os error 111)"})


class ZtunnelExemption(Driver):
    def deleted(self):
        ops = FakeOps(("go",))
        sh = self.shutter(ops, langs=("go",), watch=FakeWatch({"demo-go-api-new2-x": {"finished_at": T0 + 112}}))
        self.hooked(ops, sh)
        sh.skew = 0.0
        sh.deletions = {"go": {"deleted_at": T0 + 110}}      # 11:08:30Z
        return sh

    def run_filter(self, sh, lines, detail=None, fail=False):
        item = guard.Item(shut.ZT_NAME, "breach", detail or f"{len(lines)} lines in 120s across 1 ztunnel pods")
        read = guard.GuardRead(T0 + 200, [item, guard.Item("guard (b) other edges", "finding", "x")])
        sh.ops.clock.t = T0 + 240.3          # the guard's read took 40 s: the filter runs after it
        self.since = None

        def logs(ns, pod_name, since_s):
            if fail:
                raise guard.ReadError("down")
            self.since = since_s
            return lines
        reader = SimpleNamespace(kget=lambda a: {"items": [{"metadata": {"name": "ztunnel-qfmq4",
                                                                       "namespace": "istio-system"}}]}, logs=logs)
        return sh.zt_exempt.filter(read, reader, T0 + 200, 120)

    def test_k6_to_a_deleted_pod_inside_the_window_is_exempt(self):
        sh = self.deleted()
        read = self.run_filter(sh, [zt_hit(), zt_hit(t="2026-11-18T11:08:40Z"), "unrelated line"],
                               detail="2 lines in 120s across 1 ztunnel pods")
        self.assertEqual(read.items[0].state, "finding", read.items[0].detail)
        self.assertEqual(self.since, 41 + 120 + shut.ZT_SLACK_S)   # reaches back to the guard's at
        self.assertEqual(len(sh.zt_exempt.records), 2)
        self.assertEqual(sorted(sh.zt_exempt.records[0]), sorted(["time", "src.namespace", "src.workload",
                                                                 "dst.namespace", "dst.workload", "read_at", "window"]))
        again = self.run_filter(sh, [zt_hit()], detail="1 lines in 120s across 1 ztunnel pods")
        self.assertEqual((again.items[0].state, len(sh.zt_exempt.records)), ("finding", 2))   # not recounted

    def test_everything_else_stays_strict(self):
        for name, lines, detail in (
                ("another source", [zt_hit(src="demo-go-worker-a-b")], None),
                ("another destination", [zt_hit(dst="demo-go-backend-a-b")], None),
                ("before the delete", [zt_hit(t="2026-11-18T11:08:20Z")], None),
                ("after finishedAt + one read", [zt_hit(t="2026-11-18T11:09:40Z")], None),
                ("k6 of another window", [zt_hit(src="soak-k6-loaded-abcde")], None),
                ("fewer lines than the guard counted", [zt_hit()], "2 lines in 120s across 1 ztunnel pods"),
                ("more lines than the guard counted", [zt_hit(), zt_hit(t="2026-11-18T11:08:41Z")],
                 "1 lines in 120s across 1 ztunnel pods"),
                ("no count in the detail", [zt_hit()], "query error")):
            with self.subTest(name):
                sh = self.deleted()
                read = self.run_filter(sh, lines, detail)
                self.assertEqual(read.items[0].state, "breach")
                self.assertEqual(sh.zt_exempt.records, [])
        sh = self.deleted()
        non_json = "2026-11-18T11:08:31Z error dev-otel-libs connection refused 10.0.0.1:15008"
        self.assertEqual(self.run_filter(sh, [zt_hit(), non_json]).items[0].state, "breach")
        sh.zt_exempt.cap = 1
        self.assertEqual(self.run_filter(sh, [zt_hit(), zt_hit(t="2026-11-18T11:08:41Z")]).items[0].state, "breach")
        self.assertIn("not read", self.run_filter(sh, [], fail=True).items[0].detail)
        clean = guard.GuardRead(T0, [guard.Item(shut.ZT_NAME, "ok", "")])
        self.assertIs(sh.zt_exempt.filter(clean, None, T0, 120), clean)

    def test_a_strict_line_at_the_edge_of_the_guards_range_stays_a_breach(self):
        """Review: a worker -> api refusal at the start of the guard's range, then
        k6 -> deleted-pod refusals; a re-read counted from the filter's own clock
        would miss the first line and downgrade the breach."""
        sh = self.deleted()
        sh.deletions = {"go": {"deleted_at": T0 + 85}}       # a window that reaches the range start too
        edge = "2026-11-18T11:08:00.5Z"                      # at - lookback + 0.5 s (at = T0 + 200)
        lines = [zt_hit(src="demo-go-worker-a-b", t=edge), zt_hit(), zt_hit(t="2026-11-18T11:08:40Z")]
        read = self.run_filter(sh, lines, detail="3 lines in 120s across 1 ztunnel pods")
        self.assertEqual(read.items[0].state, "breach")
        self.assertIn("outside 1", read.items[0].detail)
        self.assertEqual(sh.zt_exempt.records, [])
        # the same lines without the strict one, plus one older than the range: judged on the range only
        sh = self.deleted()
        older = zt_hit(src="demo-go-worker-a-b", t="2026-11-18T11:07:59Z")   # before at - lookback
        read = self.run_filter(sh, [older, zt_hit(), zt_hit(t="2026-11-18T11:08:40Z")],
                               detail="2 lines in 120s across 1 ztunnel pods")
        self.assertEqual(read.items[0].state, "finding", read.items[0].detail)

    def test_read_filter_runs_both(self):
        sh = self.deleted()
        read = guard.GuardRead(T0, [guard.Item("x", "ok", "")])
        self.assertIs(sh.read_filter(read, None, T0, 120), read)


class K6LogFallback(Base):
    def test_a_failed_read_keeps_the_stop_time_save(self):
        (self.d / "raw" / "soak-k6-shut.stop.log").write_text("     execution: local\nSOAKRES {}\n")
        err = "unable to retrieve container logs for containerd://abc"
        with self.cluster() as c:
            c.logs({"job/soak-k6-shut": err})
            text, source = soak.save_k6_log(self.kube, self.d, "soak-k6-shut")
            self.assertIn("SOAKRES", text)
            self.assertIn("stop-time save (soak-k6-shut.stop.log)", source)
            self.assertIn("SOAKRES", (self.d / "raw" / "soak-k6-shut.log").read_text())
            self.assertIn("unable to retrieve", (self.d / "raw" / "soak-k6-shut.log.err").read_text())
            (self.d / "raw" / "soak-k6-shut.stop.log").unlink()
            text, source = soak.save_k6_log(self.kube, self.d, "soak-k6-shut")   # the copied .log stays
            self.assertIn("earlier save", source)
            (self.d / "raw" / "soak-k6-shut.log").unlink()
            text, source = soak.save_k6_log(self.kube, self.d, "soak-k6-shut")
            self.assertIsNone(text)
            self.assertIn("no usable k6 log", source)
            c.logs({"job/soak-k6-shut": "     execution: local\nSOAKRES {}\n"})
            self.assertEqual(soak.save_k6_log(self.kube, self.d, "soak-k6-shut")[1], "kubectl logs job (now)")

    def test_the_trap_records_which_log_it_used(self):
        (self.d / "raw" / "soak-k6-shut.stop.log").write_text("     execution: local\nSOAKRES {}\n")
        job = render.k6_job(RUN, "shut", 999)
        with self.cluster(extra=[job]) as c:
            c.logs({"job/soak-k6-shut": "unable to retrieve container logs for containerd://abc"})
            res = self.trap(state=hooked(reservoir=True))
        rec = json.loads((self.d / "trap.jsonl").read_text().splitlines()[-1])
        self.assertIn("stop-time save", rec["k6_log_source"]["soak-k6-shut"])
        self.assertFalse([e for e in res.errors if "soak-k6-shut" in e])
        self.assertFalse(soak.k6_log_usable(""))


class WatchRace(unittest.TestCase):
    def test_stop_during_spawn_ends_the_child(self):
        w = shut.PodWatch(["k"], spawn=None)
        procs = []

        def spawn(cmd):
            w._stop.set()            # stop() lands while the child is being spawned
            procs.append(FakeProc([event("ADDED", pod("demo-go-api-1a-x"))]))
            return procs[-1]
        w.spawn = spawn
        w.start()
        w._thread.join(5)
        self.assertTrue(procs[0].gone.is_set())
        self.assertEqual(w.record()["events"], 0)
        w.stop()


def k6_log(langs, pre=(30, 0, 0), post=(30, 0, 2)) -> str:
    counts = {}
    for lang in langs:
        for ph, ns in (("pre", pre), ("post", post)):
            for o, n in zip(("expected", "unexpected", "noanswer"), ns):
                counts[f"soak_requests{{lang:{lang},route:orders,scen:ok,class:none,phase:{ph},outcome:{o}}}"] = n
    summary = {"metrics": {k: {"values": {"count": v}} for k, v in counts.items()}}
    return (k6script.RESERVOIR_PREFIX + "{}\n" + k6script.SUMMARY_BEGIN + "\n" + json.dumps(summary) + "\n"
            + k6script.SUMMARY_END + "\n")


class Mode(Base):
    """``soak.py shut`` end to end on the fakes (the pattern of test_loop's full_run)."""

    def setUp(self):
        super().setUp()
        import os
        os.environ["SOAK_RUNS_DIR"] = str(self.dir / "runs")
        self.kcal = self.dir / "soak-cal.json"
        self.kcal.write_text("{}")

    def tearDown(self):
        import os
        os.environ.pop("SOAK_RUNS_DIR", None)
        super().tearDown()

    def main(self, *argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = soak.main(list(argv))
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def shut_run(self, *extra, done_at=4, replicas=1, delete_fails=False, watch_events=True):
        kubectl = self.fake_kubectl()
        demo = self.script("demo.sh", f"#!/bin/sh\nexec {sys.executable} -I {HERE / 'fakekube.py'} demo-sh \"$@\"\n")
        clock = RealClock()
        ok = guard.Item("x", "ok")
        holder = {"pods": demo_set(("go",), "old1", started=time.time() - 3000)}
        reads = []

        def read(r, cfg, roll, at, **kw):
            reads.append(at)
            if done_at is not None and len(reads) == done_at:
                holder["c"].edit(lambda st: [o.__setitem__("status", {"succeeded": 1,
                                                                       "completionTime": sp.iso(at - 5)})
                                             for o in st["objects"] if o["kind"] == "Job"])
            holder.setdefault("applied", at)
            return guard.GuardRead(at, [ok])

        def ready(*a, **kw):
            holder["pods"] = demo_set(("go",), "new2", started=time.time() - 900)
            return ok

        def delete(self_, lang, name):
            if delete_fails:
                raise soak.SoakError("kubectl delete: forbidden")
            holder["deleted"] = clock()
            holder["pods"] = [p for p in holder["pods"] if p["metadata"]["name"] != name] + \
                [pod("demo-go-api-new3-x", started=clock() + 2)]
            soak.Kube.delete_api_pod(self_.kube, lang, name)

        def copy_series(self_, t1, t2):
            return [copy_ser("demo-go-api", "old-go", [(t1 - 5, 10), (t2, 50)])]

        def spawn(cmd):
            evs = []
            if watch_events:
                evs = [event("DELETED", pod("demo-go-api-old1-x", finished=time.time() - 1000, exit_code=0)),
                       event("MODIFIED", pod("demo-go-api-new2-x", finished=time.time() - 1, exit_code=0,
                                             deleting=time.time() - 3))]
            return FakeProc(evs)
        patched = {
            (soak, "DEMO_SH"): demo, (soak, "start_caffeinate"): lambda: None,
            (guard, "entry_read"): lambda r, cfg, at, **kw: guard.GuardRead(at, [ok]),
            (guard, "wait_demo_ready"): ready, (guard, "wait_copy_counts"): lambda *a, **kw: ok,
            (guard, "copy_totals"): lambda r, at: {}, (guard, "window_read"): read,
            (soak, "Loop"): lambda *a, **kw: _loop(*a, clock=clock, **kw),
            (soak.CalOps, "copy_series"): copy_series, (soak.CalOps, "pods"): lambda self_: holder["pods"],
            (soak.ShutOps, "deployments"): lambda self_: [],
            (soak.ShutOps, "k6_started"): lambda self_, job: holder.setdefault("k6", clock() - 95),
            (soak.ShutOps, "k6_job_created"): lambda self_, job: holder["applied"],
            (soak.ShutOps, "k6_log_timestamps"): lambda self_, job: (
                sp.iso(holder["k6"] + 0.5) + " " + k6script.RESERVOIR_PREFIX + '{"phase": "pre"}\n'
                + sp.iso(holder["k6"] + 60.5) + " " + k6script.RESERVOIR_PREFIX + '{"phase": "post"}\n'),
            (soak, "spawn_logs"): lambda cmd: FakeProc(["2026-11-26T10:00:00Z hello\n", "2026-11-26T10:00:01Z bye\n"],
                                                     block=False),
            (soak.ShutOps, "promoted"): lambda self_: {"list": ["service.name"], "source": "fake"},
            (soak.ShutOps, "delete"): delete, (soak, "spawn_watch"): spawn,
        }
        orig = {k: getattr(*k) for k in patched}
        for (obj, name), v in patched.items():
            setattr(obj, name, v)
        try:
            with self.cluster() as c:
                holder["c"] = c
                if replicas != 1:
                    c.edit(lambda st: [o["spec"].__setitem__("replicas", replicas) for o in st["objects"]
                                       if o["kind"] == "Deployment" and o["metadata"]["name"].startswith("demo-go-")])
                c.logs({"job/soak-k6-shut": k6_log(("go",)), "pod/demo-go-api-new2-x:api": "x\n"})
                c.edit(lambda st: st["objects"].append({"kind": "Pod", "metadata": {
                    "name": "demo-go-api-new2-x", "namespace": sp.APP_NS,
                    "labels": {"app.kubernetes.io/component": "demo", "app.kubernetes.io/name": "demo-go-api"}}}))
                rc, out, err = self.main("shut", "--run", RUN, "--kubectl", str(kubectl), "--tag", "abc1234",
                                         "--langs", "go", "--rate", "2", "--worker-interval", "3600",
                                         "--kcal", str(self.kcal), "--pre", "60", "--post", "120", *extra)
        finally:
            for (obj, name), v in orig.items():
                setattr(obj, name, v)
        return rc, out, err, c, holder

    def test_full_shut_window(self):
        rc, out, err, c, holder = self.shut_run()
        self.assertEqual(rc, 0, out + err)
        d = self.dir / "runs" / RUN
        inp = json.loads((d / "shut-input.json").read_text())
        go = inp["apis"]["demo-go-api"]
        self.assertEqual((go["old_instance"], go["pod"], go["exit_code"]), ("old-go", "demo-go-api-new2-x", 0))
        self.assertIsNotNone(go["finished_at"])
        self.assertEqual(go["new_pods"], ["demo-go-api-new3-x"])
        self.assertIsInstance(go["post_before_finish"], int)
        ident = json.loads((d / "identity-input.json").read_text())
        self.assertEqual(sorted(ident["rolls"]["nonce"]["services"]), ["demo-go-api", "demo-go-backend",
                                                                       "demo-go-worker"])
        self.assertIsNotNone(ident["rolls"]["nonce"]["services"]["demo-go-api"]["old"][0]["finished_at"])
        win = json.loads((d / "shut-window.json").read_text())
        self.assertTrue(win["valid"], win["reasons"])
        self.assertEqual(len(win["commands"]), 3)
        self.assertNotIn("count_args", win)
        deletes = [a for a in c.verbs() if a[:1] != ["demo-sh"] and "delete" in a and "pods" in a]
        self.assertTrue(any("metadata.name=demo-go-api-new2-x" in a for a in deletes), deletes)
        self.assertEqual(c.keys(), c.seeded)     # the trap removed the run's objects
        self.assertNotIn("env", (d / "pods.jsonl").read_text())
        logs = win["deleted_logs"]["demo-go-api-new2-x/api"]
        # snapshot "x" (the fake's pod log) + the follow's two lines after it
        self.assertEqual((logs["lines"], logs["canary_lines"], logs["marker"]), (3, 0, False))
        self.assertEqual((logs["snapshot_lines"], logs["follow_lines"]), (1, 2))
        # the cross-check read races the fake's state file write by the delete: either answer is recorded
        self.assertIn(logs["cross_check"], ({"lines": 1}, {"error": "pod gone (not read)"}))
        saved = d / "raw" / "demo-go-api-new2-x.api.deleted.log"
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertIsNotNone(win["skew_s"])
        self.assertIn("deleted_after_post_s", win["phase"])
        self.assertIn("next: python3 -I testing/k8s/verify.py soak shut", out)

    def test_delete_failure_invalidates(self):
        rc, out, err, *_ = self.shut_run(delete_fails=True)
        self.assertEqual(rc, 1)
        win = json.loads((self.dir / "runs" / RUN / "shut-window.json").read_text())
        self.assertIn("api pod delete failed: go", win["reasons"])
        self.assertFalse(json.loads((self.dir / "runs" / RUN / "shut-input.json").read_text())["valid"])
        self.assertEqual(win["commands"], [])

    def test_k6_never_done_means_no_deletion_reason(self):
        rc, out, err, *_ = self.shut_run("--allow-short-quiesce", "--quiesce", "0", done_at=None,
                                         watch_events=False)
        self.assertEqual(rc, 1)
        win = json.loads((self.dir / "runs" / RUN / "shut-window.json").read_text())
        self.assertFalse(win["valid"])

    def test_refusals_before_any_write(self):
        rc, out, err, c, _ = self.shut_run(replicas=2)
        self.assertEqual(rc, 1)
        self.assertIn("api replicas must be 1", out)
        self.assertFalse([a for a in c.verbs() if "apply" in a])
        rc, out, err, *_ = self.shut_run("--delta", "5")
        self.assertEqual(rc, 1)
        self.assertIn("graceful stop", err)
        self.kcal.unlink()
        rc, out, err, *_ = self.shut_run()
        self.assertEqual(rc, 1)
        self.assertIn("not a file", err)


REAL_LOOP = soak.Loop


def _loop(*a, clock, **kw):
    return REAL_LOOP(*a, **{**kw, "clock": clock, "sleep": clock.sleep})


if __name__ == "__main__":
    unittest.main()
