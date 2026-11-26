"""Tests for the run loop (soak.py): the trap, cleanup, the pre-flight, the
guarded label restore, the demo restore, the window states (guard breach, blind
read, wall-clock gap, k6 failures) and the dry run, against the fake kubectl
(fakekube.py) and K1's fake Prometheus reader. No cluster."""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import guard  # noqa: E402
import k6script  # noqa: E402
import render  # noqa: E402
import soak  # noqa: E402
import soakcount  # noqa: E402
from soak import Loop  # noqa: E402
from guard import sp  # noqa: E402
from test_guard import NOW, Blind, healthy, soak_reader, tp  # noqa: E402

RUN = "r1"
IMG = "harbor.staffops.space/development/otel-libs-fakeotlp:abc@sha256:" + "b" * 64
FAKE = [sys.executable, "-I", str(HERE / "fakekube.py")]
FAKE_DEMO = [*FAKE, "demo-sh"]


class FakeCluster:
    """A temp dir holding the fake kubectl's state and call log."""

    def __init__(self, tmp: Path, *, label: str | None = "ambient", extra: list[dict] | None = None,
                 tag: str = "abc1234"):
        self.state_path, self.log_path = tmp / "state.json", tmp / "calls.jsonl"
        state = soak._seed_state(RUN, tag)
        ns = state["objects"][0]
        if label is None:
            ns["metadata"]["labels"] = {}
        else:
            ns["metadata"]["labels"][soak.MESH_LABEL] = label
        state["objects"] += extra or []
        self.state_path.write_text(json.dumps(state))
        self.log_path.write_text("")
        self.env = {"FAKEKUBE_STATE": str(self.state_path), "FAKEKUBE_LOG": str(self.log_path)}
        self.seeded = self.keys()

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def edit(self, fn) -> None:
        st = self.state()
        fn(st)
        self.state_path.write_text(json.dumps(st))

    def logs(self, logs: dict) -> None:
        self.edit(lambda st: st.__setitem__("logs", logs))

    def keys(self) -> set[tuple[str, str | None, str]]:
        return {(o["kind"], o["metadata"].get("namespace"), o["metadata"]["name"]) for o in self.state()["objects"]}

    def calls(self) -> list[dict]:
        return [json.loads(x) for x in self.log_path.read_text().splitlines()]

    def verbs(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls()]

    def demo_calls(self) -> list[dict]:
        return [c for c in self.calls() if c["argv"][0] == "demo-sh"]


def other_run_service() -> dict:
    obj = render.soak_otlp("r2", "copy")
    obj["metadata"]["name"] = "soak-otlp-r2"
    return obj


def hooked(**extra) -> dict:
    return {"tag": "abc1234", "langs": ["go", "python"], "replicas": {"go": 1, "python": 1},
            "hook_applied": True, **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.d = soak.run_dir(RUN, self.dir / "runs")
        self.kube = soak.Kube("k8s01", kubectl=FAKE)
        self.lines: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def cluster(self, **kw) -> FakeCluster:
        return FakeCluster(self.dir, **kw)

    def trap(self, state: dict | None = None, **kw):
        kw.setdefault("demo_sh", FAKE_DEMO)
        return soak.trap(self.kube, RUN, self.d, state=hooked() if state is None else state,
                         log=self.lines.append, **kw)

    def script(self, name: str, text: str) -> Path:
        p = self.dir / name
        p.write_text(text)
        p.chmod(0o755)
        return p

    def fake_kubectl(self) -> Path:
        return self.script("kubectl", f"#!/bin/sh\nexec {sys.executable} -I {HERE / 'fakekube.py'} \"$@\"\n")


class Trap(Base):
    def test_deletes_exactly_the_run_objects(self):
        with self.cluster(extra=[other_run_service()]) as c:
            objs = render.every_kind(RUN, IMG, "// s", 100) + [render.fallback_service(RUN, "dotnet")]
            self.kube.apply(RUN, objs)
            c.logs({"job/soak-k6-dry": "k6 output\n"})
            res = self.trap()
            created = sorted(f"{o['kind']}/{o['metadata']['name']}" for o in objs)
            self.assertEqual(res.deleted, created)
            self.assertEqual(res.left, [])
            self.assertEqual(c.keys(), c.seeded)  # demo, copy, namespace, run r2, other namespace stay
            self.assertIn(("Service", "dev-otel-libs", "soak-otlp-r2"), c.keys())
            self.assertIn(("Service", "monitoring", "soak-otlp"), c.keys())  # namespace scoping
            deletes = [a for a in c.verbs() if "delete" in a]
            self.assertEqual(len(deletes), 1)
            self.assertIn(f"{render.RUN_LABEL}={RUN}", deletes[0])
            self.assertIn("dev-otel-libs", deletes[0])
            self.assertTrue(res.ok, res.errors)

    def test_logs_saved_before_the_delete_and_measured(self):
        with self.cluster() as c:
            self.kube.apply(RUN, [render.k6_configmap(RUN, "loaded", "s"), render.k6_job(RUN, "loaded", 9)])
            c.logs({"job/soak-k6-loaded": "SOAKRES {}\nSOAK_SUMMARY_BEGIN\n{}\n"})
            res = self.trap(hooked(reservoir=True))
            verbs = [a[a.index("-n") + 2] for a in c.verbs() if "-n" in a and a[0] != "demo-sh"]
            self.assertLess(verbs.index("logs"), verbs.index("delete"))
            self.assertEqual((self.d / "raw" / "soak-k6-loaded.log").read_text(),
                             "SOAKRES {}\nSOAK_SUMMARY_BEGIN\n{}\n")
            self.assertEqual(res.log_bytes, {"soak-k6-loaded": len("SOAKRES {}\nSOAK_SUMMARY_BEGIN\n{}\n")})
            self.assertTrue(res.ok, res.errors)
            logs_call = [a for a in c.verbs() if "logs" in a][0]
            self.assertNotIn("--timestamps", logs_call)  # review H1

    def test_demo_container_logs_saved_before_the_restore(self):
        """Step 1b: every demo container's log, since the hook, before the restore;
        canary lines counted, never written to trap.jsonl."""
        token = "ab" * 24
        (self.d / "canary").write_text(token + "\n")
        pod = {"kind": "Pod", "apiVersion": "v1",
               "metadata": {"name": "demo-go-api-abc12-def34", "namespace": "dev-otel-libs"},
               "spec": {"containers": [{"name": "api"}]}}
        other = {"kind": "Pod", "apiVersion": "v1", "metadata": {"name": "demo-postgres-0", "namespace": "dev-otel-libs"},
                 "spec": {"containers": [{"name": "postgres"}]}}
        with self.cluster(extra=[pod, other]) as c:
            c.logs({"pod/demo-go-api-abc12-def34:api": f"line one\nexport failed x-soak-canary={token}\n"})
            res = self.trap(hooked(hook_at=1_795_000_000.0))
            calls = [a for a in c.verbs() if a and "logs" in a and "pod/demo-go-api-abc12-def34" in a]
            verbs = [a[a.index("-n") + 2] for a in c.verbs() if "-n" in a and a[0] != "demo-sh"]
        self.assertEqual(len(calls), 1)
        self.assertIn("--container=api", calls[0])
        self.assertIn("--since-time=2026-11-18T11:06:40Z", calls[0])
        self.assertLess(verbs.index("logs"), verbs.index("delete"))
        rec = res.demo_logs["demo-go-api-abc12-def34/api"]
        self.assertEqual((rec["lines"], rec["canary_lines"]), (2, 1))
        self.assertEqual(rec["secret_patterns"], ["hex48"])
        self.assertTrue(rec["marker"])   # a canary line: the marker is saved, never the text
        path = self.d / "raw" / "demo-go-api-abc12-def34.api.log"
        saved = path.read_text()
        self.assertNotIn(token, saved)
        self.assertEqual(json.loads(saved), {"not_written": "canary", "canary_lines": 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(token, (self.d / "trap.jsonl").read_text())
        self.assertNotIn("demo-postgres-0/postgres", res.demo_logs)

    def test_demo_logs_skipped_without_hook_and_errors_recorded(self):
        pod = {"kind": "Pod", "apiVersion": "v1",
               "metadata": {"name": "demo-go-api-abc12-def34", "namespace": "dev-otel-libs"},
               "spec": {"containers": [{"name": "api"}]}}
        token = "cd" * 24
        (self.d / "canary").write_text(token + "\n")
        with self.cluster(extra=[pod]) as c:
            c.edit(lambda st: st.__setitem__("fail", {"logs": f"error: auth header x-soak-canary={token.upper()}"}))
            res = self.trap(hooked())
        self.assertIn("<canary>", res.demo_logs["demo-go-api-abc12-def34/api"]["error"])
        self.assertTrue(any("logs pod/demo-go-api" in e for e in res.errors))
        self.assertFalse(any(token in e.lower() for e in res.errors))
        with self.cluster(extra=[pod]) as c:
            c.logs({"pod/demo-go-api-abc12-def34:api": "plain line\n"})
            res = self.trap(hooked())
        rec = res.demo_logs["demo-go-api-abc12-def34/api"]
        self.assertEqual((rec["canary_lines"], rec["marker"]), (0, False))
        self.assertEqual((self.d / "raw" / "demo-go-api-abc12-def34.api.log").read_text(), "plain line\n")
        with self.cluster(extra=[pod]) as c:
            res = self.trap({"tag": "abc1234"})
        self.assertEqual(res.demo_logs, {})

    def test_reservoir_missing_from_the_log_is_an_error(self):
        with self.cluster() as c:
            self.kube.apply(RUN, [render.k6_job(RUN, "loaded", 9)])
            c.logs({"job/soak-k6-loaded": "SOAK_SUMMARY_BEGIN\n{}\nSOAK_SUMMARY_END\n"})
            res = self.trap(hooked(reservoir=True))
        self.assertTrue(any("no SOAKRES line" in e for e in res.errors), res.errors)

    def test_failed_logs_never_overwrite_a_saved_log(self):
        """Review M12: an error goes to <job>.log.err; the saved log stays."""
        with self.cluster() as c:
            self.kube.apply(RUN, [render.k6_job(RUN, "loaded", 9)])
            (self.d / "raw" / "soak-k6-loaded.log").write_text("kept\n")
            res = self.trap()
            self.assertTrue(any("logs job/soak-k6-loaded" in e for e in res.errors))
            self.assertEqual((self.d / "raw" / "soak-k6-loaded.log").read_text(), "kept\n")
            self.assertIn("not found", (self.d / "raw" / "soak-k6-loaded.log.err").read_text())
            self.assertEqual(res.deleted, ["Job/soak-k6-loaded"])
            self.assertTrue(res.demo_restored)
            self.assertEqual(c.keys(), c.seeded)

    def test_restore_uses_the_recorded_state(self):
        os.environ["DEMO_ENV_FILE"] = "/tmp/should-not-pass"
        os.environ["RUN"] = "other"
        try:
            with self.cluster() as c:
                res = self.trap(hooked(tag="f00ba12", langs=["node", "go"], replicas={"node": 1, "go": 2}))
                demo = c.demo_calls()
        finally:
            os.environ.pop("DEMO_ENV_FILE")
            os.environ.pop("RUN")
        self.assertTrue(res.demo_restored, res.errors)
        self.assertEqual([d["argv"] for d in demo], [["demo-sh", "apply", "--all"],
                                                     ["demo-sh", "scale", "--lang", "go", "2"]])
        self.assertEqual(demo[0]["env"]["TAG"], "f00ba12")
        self.assertEqual(demo[0]["env"]["LANGS"], "node go")
        self.assertEqual(demo[0]["env"]["DEMO_RESTORE"], "1")
        self.assertNotIn("DEMO_RESTORE", demo[1]["env"])  # a scale-up runs demo.sh's pre-flight
        for call in demo:
            self.assertNotIn("DEMO_ENV_FILE", call["env"])
            self.assertNotIn("RUN", call["env"])

    def test_a_language_recorded_at_zero_is_not_reapplied(self):
        """Re-review M2: no apply for it (a scale-up); scaled back to 0 instead."""
        with self.cluster() as c:
            res = self.trap(hooked(langs=["go", "node"], replicas={"go": 1, "node": 0}))
            demo = c.demo_calls()
        self.assertTrue(res.demo_restored, res.errors)
        self.assertEqual([d["argv"] for d in demo], [["demo-sh", "apply", "--all"],
                                                     ["demo-sh", "scale", "--lang", "node", "0"]])
        self.assertEqual(demo[0]["env"]["LANGS"], "go")
        self.assertEqual(demo[1]["env"]["DEMO_RESTORE"], "1")
        with self.cluster() as c:
            self.trap(hooked(langs=["node"], replicas={"node": 0}))
            self.assertEqual([d["argv"][1] for d in c.demo_calls()], ["scale"])

    def test_no_restore_without_the_hook(self):
        with self.cluster() as c:
            res = self.trap({"tag": "abc1234", "hook_applied": False})
            self.assertEqual(c.demo_calls(), [])
        self.assertFalse(res.demo_restored)
        self.assertTrue(res.ok)
        self.assertIn("trap: the demo was not changed by this run (no hook): no restore", self.lines)

    def test_no_tag_is_an_error(self):
        with self.cluster() as c:
            res = self.trap(hooked(tag=None))
            self.assertEqual(c.demo_calls(), [])
        self.assertIn("cleanup --run r1 --tag", res.errors[0])

    def test_demo_failure_is_reported(self):
        demo = self.script("demo-fail.sh", "#!/bin/sh\necho nope >&2\nexit 3\n")
        with self.cluster():
            res = self.trap(demo_sh=[str(demo)])
        self.assertFalse(res.demo_restored)
        self.assertIn("exit 3", res.errors[0])

    def test_kubectl_down_every_step_reports(self):
        with self.cluster() as c:
            c.edit(lambda st: st.__setitem__("fail", {"get": "Unable to connect to the server", "delete": "x"}))
            res = self.trap()
        self.assertEqual(len([e for e in res.errors if e.startswith(("k6 logs", "delete", "namespace"))]), 3)
        self.assertTrue(res.demo_restored)  # step 4 still runs
        self.assertTrue((self.d / "trap.jsonl").exists())

    def test_oserror_in_step_one_is_recorded(self):
        """Review H4: any exception, not only SoakError."""
        with self.cluster() as c:
            self.kube.apply(RUN, [render.k6_job(RUN, "loaded", 9)])
            c.logs({"job/soak-k6-loaded": "x\n"})
            shutil.rmtree(self.d / "raw")
            res = self.trap()
            self.assertTrue(any(e.startswith("k6 logs: FileNotFoundError") for e in res.errors), res.errors)
            self.assertEqual(res.deleted, ["Job/soak-k6-loaded"])
            self.assertTrue(res.demo_restored)
        record = json.loads((self.d / "trap.jsonl").read_text().splitlines()[-1])
        self.assertEqual(record["deleted"], ["Job/soak-k6-loaded"])

    def test_timeout_from_demo_apply_is_recorded(self):
        orig = soak.demo_apply

        def slow(*a, **kw):
            raise subprocess.TimeoutExpired("demo.sh", 1800)
        soak.demo_apply = slow
        try:
            with self.cluster():
                res = self.trap()
        finally:
            soak.demo_apply = orig
        self.assertTrue(any(e.startswith("demo restore: TimeoutExpired") for e in res.errors), res.errors)
        self.assertTrue((self.d / "trap.jsonl").exists())

    def test_trap_jsonl_written_when_logging_fails(self):
        def boom(_line):
            raise RuntimeError("log sink")
        with self.cluster():
            with self.assertRaises(RuntimeError):
                soak.trap(self.kube, RUN, self.d, state=hooked(), demo_sh=FAKE_DEMO, log=boom)
        self.assertTrue((self.d / "trap.jsonl").exists())


class LabelNoOp(Base):
    """K0e L3: the guarded namespace-label restore."""

    def label_calls(self, c):
        return [a for a in c.verbs() if "label" in a]

    def test_ambient_means_no_write(self):
        with self.cluster(label="ambient") as c:
            res = self.trap()
            self.assertFalse(res.label_written)
            self.assertEqual(self.label_calls(c), [])
            self.assertIn("trap: namespace label already ambient (no write)", self.lines)

    def test_other_value_means_one_overwrite(self):
        for value in ("none", None):
            with self.subTest(value=value), self.cluster(label=value) as c:
                res = self.trap()
                calls = self.label_calls(c)
                self.assertTrue(res.label_written)
                self.assertEqual(len(calls), 1)
                self.assertIn("--overwrite", calls[0])
                self.assertIn(f"{soak.MESH_LABEL}=ambient", calls[0])
                ns = [o for o in c.state()["objects"] if o["kind"] == "Namespace"][0]
                self.assertEqual(ns["metadata"]["labels"][soak.MESH_LABEL], "ambient")


class Preflight(Base):
    def test_clean_cluster_may_start(self):
        with self.cluster():
            self.assertEqual(soak.preflight(self.kube), [])

    def test_leftovers_of_any_run_refuse(self):
        with self.cluster(extra=[other_run_service()]):
            problems = soak.preflight(self.kube)
        self.assertEqual(len(problems), 1)
        self.assertIn("Service/soak-otlp-r2", problems[0])
        self.assertIn("cleanup", problems[0])

    def test_a_soak_labelled_object_elsewhere_is_not_a_leftover(self):
        with self.cluster():  # the seed holds monitoring/soak-otlp labelled with the run
            self.assertEqual(soak.preflight(self.kube), [])

    def test_unlabelled_object_with_a_soak_name_refuses(self):
        """Review M1."""
        foreign = render.soak_otlp(RUN, "copy")
        foreign["metadata"]["labels"] = {"app": "x"}
        with self.cluster(extra=[foreign]):
            problems = soak.preflight(self.kube)
        self.assertEqual(len(problems), 1)
        self.assertIn("soak name but no", problems[0])
        self.assertIn("Service/soak-otlp", problems[0])

    def test_non_ambient_label_refuses(self):
        with self.cluster(label="none"):
            problems = soak.preflight(self.kube)
        self.assertIn("not 'ambient'", problems[0])

    def test_apply_refuses_what_is_not_this_runs_soak_object(self):
        with self.cluster():
            cases = []
            plain = {"kind": "Service", "metadata": {"name": "x", "namespace": "dev-otel-libs", "labels": {}}}
            elsewhere = render.soak_otlp(RUN, "copy")
            elsewhere["metadata"]["namespace"] = "monitoring"
            other = render.soak_otlp("r2", "copy")
            role = render.soak_otlp(RUN, "copy")
            role["kind"] = "RoleBinding"
            cases += [plain, elsewhere, other, role]
            for obj in cases:
                with self.subTest(obj=obj["metadata"]), self.assertRaises(soak.SoakError):
                    self.kube.apply(RUN, [obj])

    def test_demo_record_and_tag_check(self):
        """Review H5: the running tag and replicas, refused on a mismatch."""
        with self.cluster(tag="abc1234") as c:
            record, problems = soak.demo_preflight(self.kube, ["go", "python"], "abc1234")
            self.assertEqual(problems, [])
            self.assertEqual(record, {"tag": "abc1234", "replicas": {"go": 1, "python": 1}})
            _, problems = soak.demo_preflight(self.kube, ["go"], "f00d000")
            self.assertIn("runs image tag abc1234, not f00d000", problems[0])

            def scale(st):
                for o in st["objects"]:
                    if o["kind"] != "Deployment":
                        continue
                    if o["metadata"]["name"] == "demo-go-api":
                        o["spec"]["replicas"] = 2
                    if o["metadata"]["name"] == "demo-node-api":
                        o["spec"]["template"]["spec"]["containers"][0]["image"] = "r/otel-libs-demo-node-api:zzz"
            c.edit(scale)
            _, problems = soak.demo_preflight(self.kube, ["go", "node"], "abc1234")
            self.assertTrue(any("replicas differ" in p for p in problems), problems)
            self.assertTrue(any("several image tags" in p for p in problems), problems)
            c.edit(lambda st: st.__setitem__("objects", [o for o in st["objects"]
                                                          if o["metadata"]["name"] != "demo-python-worker"]))
            _, problems = soak.demo_preflight(self.kube, ["python"], "abc1234")
            self.assertIn("2 of 3 Deployments", problems[0])

    def test_image_tag(self):
        self.assertEqual(soak.image_tag("h:5000/p/otel-libs-demo-go-api:abc@sha256:" + "0" * 64), "abc")
        self.assertEqual(soak.image_tag("h:5000/p/x"), "")


class Clock:
    def __init__(self, t: float = NOW, jumps: dict[int, float] | None = None, on_sleep=None):
        self.t, self.sleeps, self.jumps, self.on_sleep = t, 0, jumps or {}, on_sleep

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps += 1
        self.t += s + self.jumps.get(self.sleeps, 0)
        if self.on_sleep:
            self.on_sleep(self.sleeps)


class RealClock:
    """The real clock plus every fake sleep (the loop never sleeps for real)."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset

    def sleep(self, s: float) -> None:
        self.offset += max(0.0, s)


class Switch(tp.FakeReader):
    """A healthy reader that can go blind."""

    def __init__(self):
        super().__init__()
        self.__dict__.update(soak_reader(healthy()).__dict__)
        self.blind = False

    def prom(self, q, at):
        if self.blind:
            raise sp.verify.QueryError("prometheus unreachable")
        return super().prom(q, at)

    def kget(self, args):
        if self.blind:
            raise sp.verify.QueryError("api unreachable")
        return super().kget(args)


def noisy(r, metric, job, at):
    r.raw[metric] = [tp.series(tp.k8s(__name__=metric, job=job), [(at - 1900, 0), (at - 10, 3), (at + 10_000, 3)])]


def k6_pod(state: dict, waiting: str | None = None, exit_code: int | None = None) -> dict:
    cs = {"name": "k6", "state": ({"terminated": {"exitCode": exit_code}} if exit_code is not None else
                                  {"waiting": {"reason": waiting}} if waiting else {"running": {}})}
    return {"kind": "Pod", "apiVersion": "v1",
            "metadata": {"name": "soak-k6-loaded-abcde", "namespace": "dev-otel-libs",
                         "labels": {"job-name": "soak-k6-loaded", **render.labels(RUN, "k6")}},
            "status": {"containerStatuses": [cs], **state}}


class Window(Base):
    def loop(self, reader, clock, **kw):
        return soak.Loop(RUN, self.kube, reader, guard.GuardConfig(), self.d, clock=clock, sleep=clock.sleep,
                         interval=60, log=self.lines.append, **kw)

    def start_k6(self, pull_s: int = 300, deadline_s: int = 999):
        def start():
            self.kube.apply(RUN, [render.k6_configmap(RUN, "loaded", "s"), render.k6_job(RUN, "loaded", deadline_s)])
            return soak.Load("soak-k6-loaded", pull_s, deadline_s)
        return start

    def job(self, c):
        return [o for o in c.state()["objects"] if o["kind"] == "Job"][0]

    def test_clean_passive_window_runs_its_duration(self):
        reader, clock = Switch(), Clock()
        with self.cluster():
            fds = []
            res = self.loop(reader, clock, fd_reader=lambda: fds.append(1) or {"demo-go-api": 12}).window(
                "passive", 300)
        self.assertTrue(res.started and res.valid, res.reason)
        self.assertEqual(res.reads, 6)
        self.assertEqual(len(fds), 6)  # the FD read at every guard read
        guard_lines = [json.loads(x) for x in (self.d / "guard.jsonl").read_text().splitlines()]
        self.assertEqual(guard_lines[0]["phase"], "entry")
        self.assertEqual(guard_lines[1]["fd"], {"demo-go-api": 12})
        self.assertTrue((self.d / "heartbeat").exists())
        self.assertIsNone(self.kube.deadline)  # the read deadline is cleared after each read

    def test_fixed_cadence_with_slow_reads(self):
        # K-count re-review HIGH-2: the read's time is taken off the sleep. Reads
        # of 70 s (FD sweep included) sleep 0 and the next read starts at once;
        # reads of 10 s sleep 50 s, so a read starts every 60 s.
        for read_s, want_sleep in ((70, 0.0), (10, 50.0)):
            reader, clock = Switch(), Clock()
            sleeps, starts = [], []
            real_sleep = clock.sleep

            def sleep(s, real_sleep=real_sleep, sleeps=sleeps):
                sleeps.append(s)
                real_sleep(s)

            def fd(clock=clock, starts=starts, read_s=read_s):
                starts.append(clock.t)
                clock.t += read_s
                return {"at": clock.t, "containers": {}}

            with self.cluster():
                loop = soak.Loop(RUN, self.kube, reader, guard.GuardConfig(), self.d, clock=clock, sleep=sleep,
                                 interval=60, log=self.lines.append, fd_reader=fd)
                res = loop.window("passive", 600)
            self.assertTrue(res.valid, res.reason)       # no wall-clock gap: 70 s < 3 x 60 s
            self.assertTrue(sleeps and all(abs(x - want_sleep) < 1e-6 for x in sleeps), (read_s, sleeps))
            period = [b - a for a, b in zip(starts, starts[1:])]
            self.assertTrue(all(abs(p - max(60, read_s)) < 1e-6 for p in period), (read_s, period))

    def test_breach_stops_k6_and_invalidates(self):
        reader = Switch()
        clock = Clock(on_sleep=lambda n: reader.log_lines.append(
            "2026 ERROR connection refused 10.0.0.1:15008 ns dev-otel-libs"))
        with self.cluster() as c:
            c.logs({"job/soak-k6-loaded": "partial\n"})
            res = self.loop(reader, clock).window("loaded", 3600, loaded=True, start_load=self.start_k6())
            self.assertTrue(res.started)
            self.assertFalse(res.valid)
            self.assertTrue(res.reason.startswith("platform: mesh (guard (c) ztunnel refused"), res.reason)
            self.assertIn("suspended", res.reason)
            self.assertTrue(self.job(c)["spec"]["suspend"])
            self.assertEqual((self.d / "raw" / "soak-k6-loaded.stop.log").read_text(), "partial\n")
        self.assertEqual(res.reads, 2)  # the read after the first sleep breaches

    def test_shared_rise_breaches_loaded_but_not_passive(self):
        for loaded, valid in ((True, False), (False, True)):
            with self.subTest(loaded=loaded):
                reader = Switch()
                clock = Clock()
                clock.on_sleep = lambda n, r=reader, c=clock: noisy(
                    r, "otelcol_receiver_refused_spans", sp.GATEWAY_JOB, c.t) if n == 1 else None
                with self.cluster():
                    res = self.loop(reader, clock).window("w", 180, loaded=loaded)
                self.assertEqual(res.valid, valid, res.reason)
                if valid:
                    self.assertTrue(any(f.startswith("sentinels shared") for f in res.findings))
                else:
                    self.assertIn("sentinels shared", res.reason)

    def test_blind_read_stops_the_load(self):
        reader = Switch()
        clock = Clock(on_sleep=lambda n: setattr(reader, "blind", True))
        with self.cluster() as c:
            c.logs({"job/soak-k6-loaded": "x\n"})
            res = self.loop(reader, clock).window("loaded", 3600, loaded=True, start_load=self.start_k6())
            self.assertFalse(res.valid)
            self.assertTrue(res.reason.startswith("guard: blind read"), res.reason)
            self.assertIn("deleted", res.reason)
            self.assertNotIn(("Job", "dev-otel-libs", "soak-k6-loaded"), c.keys())
            self.assertIn(("ConfigMap", "dev-otel-libs", "soak-k6-loaded"), c.keys())

    def test_blind_stop_retries_until_the_deadline(self):
        """Review M4."""
        clock = Clock()
        with self.cluster() as c:
            loop = self.loop(Switch(), clock)
            c.edit(lambda st: st.__setitem__("fail", {"delete": "Unable to connect to the server"}))
            msg = loop.stop_load(soak.Load("soak-k6-x", 300, 200), "delete", started=clock())
            self.assertIn("activeDeadlineSeconds bounds the load", msg)
            self.assertGreaterEqual(clock.t - NOW, 200)
            self.assertEqual(len([a for a in c.verbs() if "delete" in a]), clock.sleeps + 1)
            # A later success ends the retries.
            calls = [0]

            def flaky(n):
                calls[0] = n
                if n == 2:
                    c.edit(lambda st: st.__setitem__("fail", {}))
            clock2 = Clock(on_sleep=flaky)
            loop2 = self.loop(Switch(), clock2)
            c.edit(lambda st: st.__setitem__("fail", {"delete": "down"}))
            self.assertIn("deleted", loop2.stop_load(soak.Load("soak-k6-x", 300, 10_000), "delete", clock2()))
            self.assertEqual(clock2.sleeps, 2)

    def test_wall_clock_gap_invalidates(self):
        reader, clock = Switch(), Clock(jumps={2: 400})  # laptop asleep after the 2nd read
        with self.cluster() as c:
            c.logs({"job/soak-k6-loaded": "x\n"})
            c.edit(lambda st: st["objects"].append(k6_pod({})))
            res = self.loop(reader, clock).window("loaded", 3600, loaded=True, start_load=self.start_k6())
            self.assertTrue(self.job(c)["spec"]["suspend"])
        self.assertFalse(res.valid)
        self.assertIn("laptop: wall-clock gap 460s", res.reason)
        self.assertEqual(res.reads, 2)

    def test_gap_detection_unit(self):
        clock = Clock()
        with self.cluster():
            loop = self.loop(Switch(), clock)
            self.assertIsNone(loop.heartbeat())
            clock.t += 180
            self.assertIsNone(loop.heartbeat())  # exactly 3 x interval is allowed
            clock.t += 181
            self.assertIn("181s", loop.heartbeat())
            clock.t -= 10
            self.assertIn("gap -10s", loop.heartbeat())  # clock went backwards

    def test_entry_gate_blocks_and_points_to_the_platform(self):
        reader, clock = Switch(), Clock()
        noisy(reader, "otelcol_exporter_send_failed_spans", sp.AGENT_JOB, NOW)
        started = []
        with self.cluster():
            res = self.loop(reader, clock).window("loaded", 60, loaded=True,
                                                  start_load=lambda: started.append(1))
        self.assertFalse(res.started)
        self.assertIn("ask the platform session", res.reason)
        self.assertEqual(started, [])

    def test_entry_gate_blind(self):
        with self.cluster():
            res = self.loop(Blind(), Clock()).window("w", 60)
        self.assertIn("entry gate: blind read", res.reason)

    def test_copy_counts_gate(self):
        with self.cluster():
            res = self.loop(Switch(), Clock()).window("w", 60, services=["demo-go-api", "demo-go-worker"])
        self.assertFalse(res.started)
        self.assertIn("demo-go-worker", res.reason)
        with self.cluster():
            res = self.loop(Switch(), Clock()).window("w", 60, services=["demo-go-api"])
        self.assertTrue(res.valid, res.reason)
        with self.cluster():  # no increase since the hook's totals (review M7)
            res = self.loop(Switch(), Clock()).window("w", 60, services=["demo-go-api"],
                                                      since={"demo-go-api": 5.0})
        self.assertIn("no increase since the hook", res.reason)

    def test_k6_success_ends_the_window(self):
        with self.cluster() as c:
            start = self.start_k6()

            def done():
                load = start()
                c.edit(lambda st: [o.__setitem__("status", {"succeeded": 1}) for o in st["objects"]
                                   if o["kind"] == "Job"])
                return load
            res = self.loop(Switch(), Clock()).window("loaded", 7200, loaded=True, start_load=done)
        self.assertTrue(res.valid, res.reason)
        self.assertEqual(res.reads, 1)

    def test_k6_failed_job_is_invalid(self):
        """Review H6."""
        with self.cluster() as c:
            start = self.start_k6()

            def failed():
                load = start()
                c.edit(lambda st: [o.__setitem__("status", {"failed": 1, "conditions": [
                    {"type": "Failed", "reason": "DeadlineExceeded", "message": "too long"}]})
                    for o in st["objects"] if o["kind"] == "Job"])
                return load
            res = self.loop(Switch(), Clock()).window("loaded", 7200, loaded=True, start_load=failed)
        self.assertFalse(res.valid)
        self.assertEqual(res.reason, "k6: Job failed (DeadlineExceeded: too long)")

    def test_k6_image_pull_backoff_after_the_pull_allowance(self):
        with self.cluster() as c:
            c.edit(lambda st: st["objects"].append(k6_pod({}, waiting="ImagePullBackOff")))
            res = self.loop(Switch(), Clock()).window("loaded", 7200, loaded=True,
                                                      start_load=self.start_k6(pull_s=120))
        self.assertFalse(res.valid)
        self.assertIn("k6: pod not running 120s after the start (ImagePullBackOff)", res.reason)
        self.assertEqual(res.reads, 3)  # reads at 0 and 60 s are inside the allowance

    def test_k6_exited_0_before_the_job_succeeded(self):
        """kcal-20261125b: the k6 container terminated with exit 0 while the Job had
        no ``succeeded`` yet; that is not a "pod not running" failure."""
        with self.cluster() as c:
            c.edit(lambda st: st["objects"].append(k6_pod({}, exit_code=0)))
            res = self.loop(Switch(), Clock()).window("loaded", 600, loaded=True,
                                                      start_load=self.start_k6(pull_s=120))
        self.assertTrue(res.valid, res.reason)
        with self.cluster() as c:
            c.edit(lambda st: st["objects"].append(k6_pod({}, exit_code=1)))
            res = self.loop(Switch(), Clock()).window("loaded", 600, loaded=True,
                                                      start_load=self.start_k6(pull_s=120))
        self.assertIn("pod not running", res.reason)

    def test_k6_start_failure_is_recorded(self):
        """Review L6."""
        def boom():
            raise soak.SoakError("apply refused")
        with self.cluster():
            res = self.loop(Switch(), Clock()).window("loaded", 60, loaded=True, start_load=boom)
        self.assertFalse(res.started)
        self.assertIn("k6 start failed: SoakError: apply refused", res.reason)
        record = json.loads((self.d / "windows.jsonl").read_text().splitlines()[-1])
        self.assertEqual(record["reason"], res.reason)

    def test_stop_without_job_and_failed_suspend(self):
        with self.cluster() as c:
            loop = self.loop(Switch(), Clock())
            self.assertEqual(loop.stop_load(None, "suspend"), "no load to stop")
            c.edit(lambda st: st.__setitem__("fail", {"patch": "forbidden"}))
            self.assertIn("activeDeadlineSeconds bounds the load",
                          loop.stop_load(soak.Load("soak-k6-x", 1, 1), "suspend"))
            self.assertTrue((self.d / "raw" / "soak-k6-x.stop.log.err").exists())
            self.assertIsNone(loop.load_state(soak.Load("missing", 1, 1), NOW))


class CtlReader:
    """The copy's control series per read (two queries per read: spans, fixed);
    the last entry repeats."""

    def __init__(self, reads: list[tuple[float, dict[str, float]]], blind_at: int | None = None):
        self.reads, self.calls, self.blind_at = reads, 0, blind_at

    def prom(self, q, at):
        i = min(self.calls // 2, len(self.reads) - 1)
        self.calls += 1
        if self.blind_at is not None and i == self.blind_at:
            raise sp.verify.QueryError("prometheus unreachable")
        spans, fixed = self.reads[i]
        assert 'service_name="soak-control"' in q and 'job="dev-otel-libs/soak-agent"' in q, q
        if q.startswith("sum by (cluster) (otel_libs_soak_spans_total{"):
            return [tp.instant(tp.k8s(), spans)] if spans else []
        assert q.startswith("sum by (cluster, service_instance_id) (otel_libs_soak_fixed_spans_total{"), q
        return [tp.instant(tp.k8s(service_instance_id=k), v) for k, v in fixed.items()]


EXPECTED_FIXED = {f"{RUN}-ctl-a": 2, f"{RUN}-ctl-b": 2, f"{RUN}-ctl-n": 1, "unset": 2}
CTL_SPANS = 8


class Control(Base):
    def run_control(self, c: FakeCluster, reader, job_status: dict | None = None):
        clock = Clock()
        orig = soak.Kube.apply

        def apply(self_, run, objs):
            out = orig(self_, run, objs)
            if job_status is not None and any(o["kind"] == "Job" for o in objs):
                c.edit(lambda st: [o.__setitem__("status", job_status) for o in st["objects"] if o["kind"] == "Job"])
            return out
        soak.Kube.apply = apply
        try:
            return soak.positive_control(self.kube, reader, RUN, self.d, clock=clock, sleep=clock.sleep,
                                         log=self.lines.append), clock
        finally:
            soak.Kube.apply = orig

    def test_exact_counts_pass(self):
        with self.cluster() as c:
            c.logs({"job/soak-control": "traces generated\n"})
            rec, _ = self.run_control(c, CtlReader([(0, {}), (CTL_SPANS, EXPECTED_FIXED)]), {"succeeded": 1})
            job = [o for o in c.state()["objects"] if o["kind"] == "Job"][0]
        self.assertEqual(rec["verdict"], "PASS", rec)
        self.assertEqual(rec["delta"], {"spans": 8.0, "fixed": {k: float(v) for k, v in EXPECTED_FIXED.items()}})
        self.assertEqual(job["metadata"]["labels"][soak.RUN_LABEL], RUN)
        self.assertEqual(json.loads((self.d / "control.json").read_text())["verdict"], "PASS")
        self.assertEqual(json.loads((self.d / "windows.jsonl").read_text().splitlines()[-1])["window"], "control")
        self.assertEqual((self.d / "raw" / "soak-control.log").read_text(), "traces generated\n")
        self.assertIn("control: PASS", self.lines[-1])

    def test_existing_series_count_as_a_delta(self):
        before = (7, {"old": 2})
        after = (15, {"unset": 2, "old": 2, f"{RUN}-ctl-a": 2, f"{RUN}-ctl-b": 2, f"{RUN}-ctl-n": 1})
        with self.cluster() as c:
            rec, _ = self.run_control(c, CtlReader([before, after]), {"succeeded": 1})
        self.assertEqual(rec["verdict"], "PASS", rec)

    def test_a_late_extra_span_fails_on_the_settle_read(self):
        with self.cluster() as c:
            rec, _ = self.run_control(c, CtlReader([(0, {}), (CTL_SPANS, EXPECTED_FIXED), (CTL_SPANS + 1, EXPECTED_FIXED)]),
                                      {"succeeded": 1})
        self.assertEqual(rec["verdict"], "FAIL")
        self.assertEqual(rec["first"]["spans"], 8.0)
        self.assertIn("expected", rec["reason"])

    def test_a_missing_rpc_key_fails_after_the_count_wait(self):
        fixed = {**EXPECTED_FIXED, f"{RUN}-ctl-b": 1}  # rpc.system.name clause missing: SERVER only
        with self.cluster() as c:
            rec, clock = self.run_control(c, CtlReader([(0, {}), (CTL_SPANS, fixed)]), {"succeeded": 1})
        self.assertEqual(rec["verdict"], "FAIL")
        self.assertIn("expected", rec["reason"])
        self.assertGreaterEqual(clock.t - NOW, soak.CONTROL_COUNT_S + soak.CONTROL_SETTLE_S)

    def test_a_client_span_counted_without_an_rpc_key_fails(self):
        """The negative case: ctl-n must add its SERVER span only."""
        fixed = {**EXPECTED_FIXED, f"{RUN}-ctl-n": 2}
        with self.cluster() as c:
            rec, _ = self.run_control(c, CtlReader([(0, {}), (CTL_SPANS, fixed)]), {"succeeded": 1})
        self.assertEqual(rec["verdict"], "FAIL")
        self.assertIn("ctl-n", rec["reason"])

    def test_failed_or_unfinished_job_fails_without_counting(self):
        for status, why in (({"failed": 1, "conditions": [{"type": "Failed", "reason": "DeadlineExceeded"}]},
                             "control Job failed (DeadlineExceeded)"),
                            ({}, f"not finished after {soak.CONTROL_JOB_S}s")):
            with self.subTest(why=why), self.cluster() as c:
                reader = CtlReader([(0, {})])
                rec, _ = self.run_control(c, reader, status)
                self.assertEqual((rec["verdict"], rec["delta"]), ("FAIL", None))
                self.assertIn(why, rec["reason"])
                self.assertEqual(reader.calls, 2)  # the before read only
                self.assertTrue((self.d / "raw" / "soak-control.log.err").exists())

    def test_reset_and_blind_reads_fail(self):
        for reader, why in ((CtlReader([(5, {"old": 3}), (3, {"old": 1})]), "copy reset"),
                            (CtlReader([(5, {}), (2, {})]), "spans went down"),
                            (CtlReader([(0, {}), (CTL_SPANS, EXPECTED_FIXED)], blind_at=1), "blind read")):
            with self.subTest(why=why), self.cluster() as c:
                rec, _ = self.run_control(c, reader, {"succeeded": 1})
                self.assertEqual(rec["verdict"], "FAIL")
                self.assertIn(why, rec["reason"])

    def test_a_live_unset_series_refuses_before_the_job(self):
        """Streams without service.instance.id collide at the copy's exporter
        (measured 2026-11-23): an earlier control's must have expired first."""
        with self.cluster() as c:
            with self.assertRaisesRegex(soak.SoakError, "still live on the copy"):
                self.run_control(c, CtlReader([(2, {"unset": 2})]), {"succeeded": 1})
            self.assertFalse(any("apply" in a for a in c.verbs()))

    def test_blind_before_read_is_a_soak_error(self):
        with self.cluster() as c:
            with self.assertRaises(soak.SoakError):
                self.run_control(c, CtlReader([(0, {})], blind_at=0), {"succeeded": 1})
            self.assertFalse(any("apply" in a for a in c.verbs()))


class Signals(unittest.TestCase):
    def test_signals_run_the_exit_function(self):
        before = signal.getsignal(signal.SIGINT)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            ran = []
            with self.subTest(sig=sig), self.assertRaises(SystemExit) as cm:
                with soak.trapped(lambda: ran.append(signal.getsignal(signal.SIGINT))):
                    os.kill(os.getpid(), sig)
            self.assertEqual(cm.exception.code, 128 + sig)
            self.assertEqual(ran, [signal.SIG_IGN])  # a second Ctrl-C cannot cut the trap
        self.assertIs(signal.getsignal(signal.SIGINT), before)  # restored

    def test_normal_exit_and_exception_run_it(self):
        ran = []
        with soak.trapped(lambda: ran.append("exit")):
            pass
        with self.assertRaises(ValueError), soak.trapped(lambda: ran.append("error")):
            raise ValueError
        self.assertEqual(ran, ["exit", "error"])

    def test_children_in_their_own_session_are_killed(self):
        """Review M6: an interrupt ends the child's process group."""
        cmd = [sys.executable, "-c", "import os, sys, time; print(os.getsid(0), flush=True); time.sleep(30)"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, start_new_session=True)
        soak._children.add(proc)
        try:
            sid = int(proc.stdout.readline())
            self.assertEqual(sid, proc.pid)  # its own session
            self.assertNotEqual(sid, os.getsid(0))
            soak.kill_children()
            self.assertEqual(proc.wait(timeout=10), -signal.SIGTERM)
        finally:
            soak._children.discard(proc)
            proc.stdout.close()

    def test_run_child_timeout_kills_and_raises(self):
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            soak.run_child([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(soak._children, set())
        self.assertEqual(soak.run_child([sys.executable, "-c", "print('ok')"], timeout=10).stdout, "ok\n")

    def test_caffeinate_only_on_macos(self):
        started = []
        proc = soak.start_caffeinate("darwin", lambda _n: "/usr/bin/caffeinate",
                                     lambda argv: started.append(argv) or "proc")
        self.assertEqual(proc, "proc")
        self.assertEqual(started[0][:3], ["caffeinate", "-dims", "-w"])
        self.assertIsNone(soak.start_caffeinate("linux", lambda _n: "/x", lambda a: 1 / 0))
        self.assertIsNone(soak.start_caffeinate("darwin", lambda _n: None, lambda a: 1 / 0))


class Helpers(Base):
    def test_hook_env(self):
        text = soak.hook_env("n1", "c" * 48, worker_interval_s=10800)
        self.assertIn("*/* OTEL_EXPORTER_OTLP_ENDPOINT=http://soak-otlp:4317", text)
        self.assertIn("*/* OTEL_EXPORTER_OTLP_HEADERS=x-soak-canary=" + "c" * 48, text)
        self.assertIn("*/* DEMO_RESTART_NONCE=n1", text)
        self.assertIn("*/worker DEMO_INTERVAL_SECONDS=10800", text)
        self.assertNotIn("OTEL_SERVICE_NAME", text)
        self.assertNotIn("DEMO_INTERVAL", soak.hook_env("n", "c"))

    def test_private_file_mode_also_for_an_existing_file(self):
        """Review L2."""
        p = self.dir / "canary"
        p.write_text("old\n")
        p.chmod(0o644)
        soak.write_private(p, "secret\n")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(p.read_text(), "secret\n")

    def test_evidence_is_scanned(self):
        """Review L3: a text the secret scan flags is not written."""
        p = self.dir / "x.log"
        self.assertEqual(soak.write_evidence(p, "fine\n"), [])
        self.assertEqual(p.read_text(), "fine\n")
        hits = soak.write_evidence(p, "header=" + "ab" * 24 + "\n")
        self.assertEqual(hits, ["hex48"])
        self.assertNotIn("ab" * 24, p.read_text())
        self.assertIn("not_written", p.read_text())
        soak.append_jsonl(self.dir / "g.jsonl", {"k": "password=x"})
        self.assertNotIn("password=x", (self.dir / "g.jsonl").read_text())

    def test_state_round_trip_and_run_dir_env(self):
        os.environ["SOAK_RUNS_DIR"] = str(self.dir)
        try:
            d = soak.run_dir("r9")
        finally:
            os.environ.pop("SOAK_RUNS_DIR")
        self.assertEqual(d, self.dir / "r9")
        self.assertEqual(soak.load_state(d), {})
        soak.save_state(d, {"tag": "t"})
        self.assertEqual(soak.load_state(d), {"tag": "t"})

    def test_kube_errors_and_timeouts(self):
        k = soak.Kube("k8s01", kubectl=[sys.executable, "-c", "print('not json')"])
        with self.assertRaises(soak.SoakError):
            k.get(["pods"])
        self.assertLessEqual(soak.Kube("k8s01").timeout, 30)  # review M5
        slow = soak.Kube("k8s01", kubectl=[sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
        self.assertIn("timed out", slow._run(["get"]).stderr)
        slow.deadline = time.monotonic() - 1
        self.assertIn("deadline passed", slow._run(["get"]).stderr)

    def test_logs_parse_through_kube_logs(self):
        """Review H1: fake-kubectl output through Kube.logs parses, a summary
        line longer than a 16 KiB CRI chunk included."""
        plan = k6script.K6Plan(langs=("go",), rate=1, duration_s=1)
        summary = {"metrics": {k: {"values": {"count": 1}} for k in k6script.thresholds(plan)},
                   "pad": "x" * 40_000}
        text = ('SOAKRES {"lang":"go"}\n' + k6script.SUMMARY_BEGIN + "\n" + json.dumps(summary) + "\n"
                + k6script.SUMMARY_END + "\n")
        with self.cluster() as c:
            c.logs({"job/soak-k6-x": text})
            rc, out = self.kube.logs("job/soak-k6-x")
        parsed, records = k6script.parse_logs(out)
        self.assertEqual(rc, 0)
        self.assertEqual(len(k6script.cell_counts(parsed)), len(k6script.thresholds(plan)))
        self.assertEqual(records, [{"lang": "go"}])

    def test_demo_tag_errors(self):
        """Review L7."""
        orig = soak.DEMO_TAG
        try:
            soak.DEMO_TAG = self.dir / "missing.sh"
            with self.assertRaises(soak.SoakError):
                soak.demo_tag()
            soak.DEMO_TAG = self.script("empty.sh", "#!/bin/sh\nexit 0\n")
            with self.assertRaises(soak.SoakError):
                soak.demo_tag()
            soak.DEMO_TAG = self.script("tag.sh", "#!/bin/sh\necho abc1234\n")
            self.assertEqual(soak.demo_tag(), "abc1234")
        finally:
            soak.DEMO_TAG = orig

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_gitignore_covers_runs(self):
        root = HERE.parents[2]
        proc = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "testing/k8s/soak/runs/x"],
                              check=False)
        self.assertEqual(proc.returncode, 0, "testing/k8s/soak/runs/ must be git-ignored")


class Reader(Base):
    def test_kube_reader_goes_through_the_loops_kubectl(self):
        with self.cluster() as c:
            c.edit(lambda st: st.update({
                "raw": {"/api/v1/namespaces/monitoring/services/kps-prometheus:9090/proxy/api/v1/query":
                        {"status": "success", "data": {"result": [{"value": [0, "1"]}]}}},
                "logs": {"ztunnel-x": "line\n"}}))
            r = soak.KubeReader(self.kube)
            self.assertIsInstance(r, sp.Reader)
            self.assertEqual(r.prom("up", NOW), [{"value": [0, "1"]}])
            self.assertEqual(r.prom_range("up", NOW - 60, NOW, 15), [{"value": [0, "1"]}])
            self.assertEqual(len(r.kget(["pods", "-A"])["items"]), 0)
            self.assertEqual(r.logs("istio-system", "ztunnel-x", 60), ["line"])
            raw = [a for a in c.verbs() if "--raw" in a]
            self.assertTrue(any("/proxy/api/v1/query?" in x for a in raw for x in a))
            c.edit(lambda st: st.__setitem__("raw", {"/api": {"status": "error", "error": "bad query"}}))
            with self.assertRaises(guard.ReadError):
                r.prom("up", NOW)
            c.edit(lambda st: st.__setitem__("fail", {"get": "down", "logs": "down"}))
            for call in (lambda: r.prom("up", NOW), lambda: r.kget(["pods"]),
                         lambda: r.logs("istio-system", "ztunnel-x", 1)):
                with self.assertRaises(guard.ReadError):
                    call()

    RESET = ("Unable to connect to the server: read tcp 10.0.0.1:63251->10.0.0.2:6443: "
             "read: connection reset by peer")
    QUERY = "/api/v1/namespaces/monitoring/services/kps-prometheus:9090/proxy/api/v1/query"

    def raw_calls(self, c) -> int:
        return len([a for a in c.verbs() if "--raw" in a])

    def test_transport_reset_then_success_is_retried(self):
        """Correction 2026-11-24: one reset, then the answer: ok, the retry counted."""
        with self.cluster() as c:
            c.edit(lambda st: st.update({
                "raw": {self.QUERY: {"status": "success", "data": {"result": [{"value": [0, "1"]}]}}},
                "flaky": {"get": {"times": 1, "stderr": self.RESET}}}))
            sleeps = []
            r = soak.KubeReader(self.kube, sleep=sleeps.append)
            self.assertEqual(r.prom("up", NOW), [{"value": [0, "1"]}])
            self.assertEqual((r.retries, sleeps, self.raw_calls(c)), (1, [1], 2))
            self.assertIn("connection reset by peer", r.retry_error)
            # kubectl get and logs retry too; a 5xx from the API proxy is transport.
            c.edit(lambda st: st.__setitem__("flaky", {
                "get": {"times": 1, "stderr": "Error from server (ServiceUnavailable): the server is "
                                              "currently unable to handle the request"},
                "logs": {"times": 1, "stderr": "Unable to connect to the server: net/http: "
                                               "TLS handshake timeout"}}))
            self.assertEqual(r.kget(["pods", "-A"])["items"], [])
            c.logs({"ztunnel-x": "line\n"})
            self.assertEqual(r.logs("istio-system", "ztunnel-x", 60), ["line"])
            self.assertEqual(r.retries, 3)
            self.assertIn("TLS handshake", r.retry_error)

    def test_transport_reset_on_every_attempt_is_blind(self):
        with self.cluster() as c:
            c.edit(lambda st: st.__setitem__("fail", {"get": self.RESET}))
            sleeps = []
            r = soak.KubeReader(self.kube, sleep=sleeps.append)
            with self.assertRaises(guard.ReadError) as cm:
                r.prom("up", NOW)
            self.assertIn("connection reset by peer", str(cm.exception))
            self.assertEqual((r.retries, sleeps, self.raw_calls(c)),
                             (len(sp.READ_RETRY_BACKOFF_S), list(sp.READ_RETRY_BACKOFF_S), 3))
            read = guard.window_read(r, guard.GuardConfig(), guard.RollState(), NOW, loaded=False)
            self.assertTrue(read.blind)
            rec = read.as_json()
            self.assertGreater(rec["retries"], 0)
            self.assertIn("connection reset by peer", rec["retry_error"])

    def test_answers_are_never_retried(self):
        """An empty Prometheus result stays blind, an error status and a 4xx
        are failures: none is retried."""
        with self.cluster() as c:
            c.edit(lambda st: st.__setitem__("raw", {self.QUERY: {"status": "success", "data": {"result": []}}}))
            sleeps = []
            r = soak.KubeReader(self.kube, sleep=sleeps.append)
            self.assertEqual(r.prom("up", NOW), [])
            items = guard.floors(r, guard.GuardConfig(), NOW)
            self.assertEqual({i.state for i in items}, {"blind"})
            self.assertEqual({i.detail for i in items}, {"no series"})
            self.assertEqual((r.retries, sleeps, self.raw_calls(c)), (0, [], 4))
            c.edit(lambda st: st.__setitem__("raw", {"/api": {"status": "error", "error": "bad query"}}))
            with self.assertRaises(guard.ReadError):
                r.prom("up", NOW)
            c.edit(lambda st: st.__setitem__("fail", {"get": "Error from server (BadRequest): bad_data"}))
            with self.assertRaises(guard.ReadError):
                r.prom("up", NOW)
            self.assertEqual((r.retries, self.raw_calls(c)), (0, 6))

    def test_no_retry_past_the_read_deadline(self):
        with self.cluster() as c:
            c.edit(lambda st: st.__setitem__("fail", {"get": self.RESET}))
            sleeps = []
            r = soak.KubeReader(self.kube, sleep=sleeps.append)
            self.kube.deadline = time.monotonic() + 0.5  # shorter than the first pause
            try:
                with self.assertRaises(guard.ReadError):
                    r.prom("up", NOW)
            finally:
                self.kube.deadline = None
            self.assertEqual((r.retries, sleeps, self.raw_calls(c)), (0, [], 1))
            self.assertIsNone(r.time_left())
            # Review M1: a retry needs its pause plus READ_RETRY_MIN_CALL_S left.
            self.kube.deadline = time.monotonic() + 1 + sp.READ_RETRY_MIN_CALL_S - 0.5
            try:
                with self.assertRaises(guard.ReadError):
                    r.prom("up", NOW)
            finally:
                self.kube.deadline = None
            self.assertEqual((r.retries, self.raw_calls(c)), (0, 2))
            # Inside a bounded read a timed-out call is retried like a reset.
            self.kube.deadline = time.monotonic() + 40
            c.edit(lambda st: st.update({"fail": {}, "flaky": {"get": {"times": 1,
                                                                        "stderr": "kubectl timed out after 25s"}},
                                         "raw": {self.QUERY: {"status": "success", "data": {"result": []}}}}))
            try:
                self.assertEqual(r.prom("up", NOW), [])
            finally:
                self.kube.deadline = None
            self.assertEqual(r.retries, 1)

    def test_writes_are_never_retried(self):
        with self.cluster() as c:
            c.edit(lambda st: st.__setitem__("flaky", {
                "apply": {"times": 1, "stderr": self.RESET}, "delete": {"times": 1, "stderr": self.RESET}}))
            with self.assertRaises(soak.SoakError):
                self.kube.apply(RUN, [render.k6_job(RUN, "loaded", 9)])
            with self.assertRaises(soak.SoakError):
                self.kube.delete_run(RUN)
            verbs = [a for a in c.verbs() if any(v in a for v in ("apply", "delete"))]
            self.assertEqual(len(verbs), 2)

    def test_guard_config_uses_a_passing_recent_k1_snapshot(self):
        """Review M14."""
        runs = self.dir / "cfg"
        d = soak.run_dir("r1", runs)
        lines = []
        self.assertEqual(soak.guard_config(d, NOW, log=lines.append).floors, sp.INITIAL_FLOORS)
        self.assertIn("initial K-D13 floors", lines[-1])
        k1 = soak.run_dir("k1-x", runs)
        snap = {"task": "K1", "verdict": "FAIL", "taken_at": sp.iso(NOW - 60),
                "floors": {"idle_cpu_share": {"floor": 0.3}}}
        (k1 / "snapshot.json").write_text(json.dumps(snap))
        cfg = soak.guard_config(d, NOW, ["go"], log=lines.append)
        self.assertEqual(cfg.floors, sp.INITIAL_FLOORS)
        self.assertIn("verdict FAIL", lines[-1])
        (k1 / "snapshot.json").write_text(json.dumps({**snap, "verdict": "PASS"}))
        cfg = soak.guard_config(d, NOW, ["go"], log=lines.append)
        self.assertEqual(cfg.floors["idle_cpu_share"], 0.3)
        self.assertEqual(cfg.langs, ("go",))
        self.assertIn(f"floors from K1 snapshot {k1 / 'snapshot.json'}", lines[-1])


class Cli(Base):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = soak.main(list(argv))
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def runs_env(self):
        os.environ["SOAK_RUNS_DIR"] = str(self.dir / "runs")

    def tearDown(self):
        os.environ.pop("SOAK_RUNS_DIR", None)
        super().tearDown()

    def test_render_modes(self):
        rc, out, _ = self.main("render", "--run", RUN, "--what", "k6", "--rate", "5", "--duration", "60")
        self.assertEqual(rc, 0)
        items = json.loads(out)["items"]
        self.assertEqual([i["kind"] for i in items], ["ConfigMap", "Job"])
        self.assertEqual(items[1]["spec"]["activeDeadlineSeconds"], 300 + 60 + 15 + 60)
        limit = items[1]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"]
        self.assertEqual(limit, f"{256 + 4 * 4 * 55}Mi")  # 4 languages x ceil(5 x 11) VUs
        for what, extra in (("soak-otlp", ["--variant", "refused"]), ("control", []),
                            ("fallback", ["--langs", "python"]), ("k6-job", ["--rate", "1"]),
                            ("k6-configmap", ["--rate", "1"]),
                            ("sink-hung", ["--fakeotlp-image", IMG]), ("sink-redirect", [])):
            rc, out, err = self.main("render", "--run", RUN, "--what", what, *extra)
            self.assertEqual(rc, 0, (what, err))
        self.assertEqual(self.main("render", "--run", RUN, "--what", "nope")[0], 1)
        self.assertEqual(self.main("render", "--run", RUN, "--what", "k6")[0], 2)
        self.assertEqual(self.main("render", "--run", "Bad", "--what", "control")[0], 2)
        self.assertEqual(self.main("render", "--run", RUN, "--what", "control", "--langs", "java")[0], 2)

    def test_other_namespace_and_context_refused(self):
        for key, value in (("NAMESPACE", "monitoring"), ("KUBE_CONTEXT", "prod")):
            os.environ[key] = value
            try:
                rc, _, err = self.main("render", "--run", RUN, "--what", "control")
            finally:
                os.environ.pop(key)
            self.assertEqual(rc, 2, key)
        os.environ["KUBE_CONTEXT"] = "other"  # review M2: only with the explicit override
        try:
            self.assertEqual(self.main("render", "--run", RUN, "--what", "control", "--allow-other-context")[0], 0)
        finally:
            os.environ.pop("KUBE_CONTEXT")

    def test_dry_run_mode(self):
        self.runs_env()
        rc, out, _ = self.main("dry-run", "--run", RUN)
        self.assertEqual(rc, 0, out)
        self.assertIn("dry-run: PASS", out)
        self.assertIn("kinds ConfigMap, Job, Pod, Service", out)
        self.assertIn("Service in namespace monitoring: True", out)
        self.assertNotIn("FAKEKUBE_STATE", os.environ)

    def test_cleanup_and_preflight_modes(self):
        self.runs_env()
        kubectl = self.fake_kubectl()
        with self.cluster(extra=[render.soak_otlp(RUN, "copy")]) as c:
            rc, out, _ = self.main("preflight", "--run", RUN, "--kubectl", str(kubectl), "--tag", "abc1234")
            self.assertEqual(rc, 1)
            self.assertIn("REFUSED soak-run objects exist", out)
            soak.save_state(self.d, hooked(langs=["go"]))
            orig = soak.DEMO_SH
            soak.DEMO_SH = self.script("demo.sh", "#!/bin/sh\nexit 0\n")
            try:
                rc, out, _ = self.main("cleanup", "--run", RUN, "--kubectl", str(kubectl))
            finally:
                soak.DEMO_SH = orig
            self.assertEqual(rc, 0, out)
            self.assertEqual(c.keys(), c.seeded - {("Service", "dev-otel-libs", "soak-otlp")})
            # Clean now: the pre-flight reaches the entry gate (on the fake: no Prometheus).
            rc, out, _ = self.main("preflight", "--run", RUN, "--kubectl", str(kubectl), "--tag", "abc1234")
            self.assertEqual(rc, 1)
            self.assertIn("REFUSED entry gate", out)
            rc, out, _ = self.main("preflight", "--run", RUN, "--kubectl", str(kubectl), "--tag", "zzz")
            self.assertIn("not zzz", out)

    def test_cleanup_with_a_tag_and_no_state_restores(self):
        self.runs_env()
        kubectl = self.fake_kubectl()
        demo = self.script("demo.sh", f"#!/bin/sh\nexec {sys.executable} -I {HERE / 'fakekube.py'} demo-sh \"$@\"\n")
        orig = soak.DEMO_SH
        soak.DEMO_SH = demo
        try:
            with self.cluster() as c:
                rc, out, _ = self.main("cleanup", "--run", RUN, "--kubectl", str(kubectl), "--tag", "t5",
                                       "--langs", "node")
                calls = c.demo_calls()
        finally:
            soak.DEMO_SH = orig
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls[0]["env"]["TAG"], "t5")
        self.assertEqual(calls[0]["env"]["LANGS"], "node")

    def test_run_mode_refuses_before_any_write(self):
        self.runs_env()
        kubectl = self.fake_kubectl()
        for extra, why in (([other_run_service()], "soak-run objects exist"), ([], "entry gate")):
            with self.subTest(why=why), self.cluster(extra=extra) as c:
                rc, out, _ = self.main("run", "--run", RUN, "--kubectl", str(kubectl), "--tag", "abc1234",
                                       "--hook", "--rate", "2", "--worker-interval", "10800")
                self.assertEqual(rc, 1)
                self.assertIn(why, out)
                self.assertFalse(any(v in a for a in c.verbs() for v in ("apply", "delete", "label", "patch")))
                self.assertEqual(c.demo_calls(), [])

    def test_run_mode_with_the_hook_restores_on_failure(self):
        """The hook applied, then (g) fails after the roll: the trap removes
        soak-otlp and restores the demo with the recorded tag (review B1, H5, M9)."""
        self.runs_env()
        kubectl = self.fake_kubectl()
        demo = self.script("demo.sh", f"#!/bin/sh\nexec {sys.executable} -I {HERE / 'fakekube.py'} demo-sh \"$@\"\n")
        orig = soak.DEMO_SH, soak.start_caffeinate, guard.entry_read, guard.wait_demo_ready, guard.copy_totals
        soak.DEMO_SH, soak.start_caffeinate = demo, lambda: None
        guard.entry_read = lambda r, cfg, at, **kw: guard.GuardRead(at, [guard.Item("x", "ok")])
        guard.copy_totals = lambda r, at: {}
        guard.wait_demo_ready = lambda *a, **kw: guard.Item("guard (g) demo pods Ready after the hook", "breach",
                                                           "demo-go-api-x: Ready for less than 600s")
        try:
            with self.cluster() as c:
                rc, out, err = self.main("run", "--run", RUN, "--kubectl", str(kubectl), "--tag", "abc1234",
                                         "--hook", "--rate", "2", "--duration", "60", "--langs", "go",
                                         "--worker-interval", "10800")
                self.assertEqual(rc, 1)
                self.assertIn("Ready for less than 600s", err)
                self.assertEqual(c.keys(), c.seeded)
                calls = c.demo_calls()
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0]["env"]["DEMO_ENV_FILE"], str(self.d / "soak.env"))
                self.assertEqual(calls[0]["env"]["TAG"], "abc1234")
                self.assertNotIn("DEMO_ENV_FILE", calls[1]["env"])
                self.assertEqual(calls[1]["env"]["DEMO_RESTORE"], "1")
                applied = [json.loads(x["stdin"]) for x in c.calls()
                           if "apply" in x["argv"] and x["argv"][0] != "demo-sh"]
                self.assertEqual([i["kind"] for a in applied for i in a["items"]], ["Service"])
        finally:
            soak.DEMO_SH, soak.start_caffeinate, guard.entry_read, guard.wait_demo_ready, guard.copy_totals = orig
        canary = (self.d / "canary").read_text().strip()
        self.assertRegex(canary, "^[0-9a-f]{48}$")
        self.assertNotIn(canary, out + err)
        self.assertIn("*/worker DEMO_INTERVAL_SECONDS=10800", (self.d / "soak.env").read_text())
        state = soak.load_state(self.d)
        self.assertEqual((state["tag"], state["hook_applied"], state["replicas"]), ("abc1234", True, {"go": 1}))

    def test_run_mode_regates_after_the_hook(self):
        """Re-review HIGH-2: clean before the hook, noisy after the waits: the
        window does not start and the trap restores."""
        self.runs_env()
        kubectl = self.fake_kubectl()
        demo = self.script("demo.sh", f"#!/bin/sh\nexec {sys.executable} -I {HERE / 'fakekube.py'} demo-sh \"$@\"\n")
        orig = (soak.DEMO_SH, soak.start_caffeinate, guard.entry_read, guard.wait_demo_ready,
                guard.wait_copy_counts, guard.copy_totals)
        ok = guard.Item("x", "ok")
        reads = []

        def gate(r, cfg, at, **kw):
            reads.append(at)
            state = "ok" if len(reads) == 1 else "breach"
            return guard.GuardRead(at, [guard.Item("sentinels shared", state, "otel-gateway +3")])
        soak.DEMO_SH, soak.start_caffeinate = demo, lambda: None
        guard.entry_read, guard.copy_totals = gate, lambda r, at: {}
        guard.wait_demo_ready = guard.wait_copy_counts = lambda *a, **kw: ok
        try:
            with self.cluster() as c:
                rc, out, err = self.main("run", "--run", RUN, "--kubectl", str(kubectl), "--tag", "abc1234",
                                         "--hook", "--rate", "2", "--duration", "60", "--langs", "go",
                                         "--worker-interval", "10800")
                self.assertEqual(rc, 1)
                self.assertIn("entry gate after the hook: platform: guard (sentinels shared", err)
                self.assertEqual(len(reads), 2)
                self.assertNotIn("window loaded", out)
                applied = [json.loads(x["stdin"]) for x in c.calls()
                           if "apply" in x["argv"] and x["argv"][0] != "demo-sh"]
                self.assertEqual([i["kind"] for a in applied for i in a["items"]], ["Service"])  # no k6
                self.assertEqual(c.keys(), c.seeded)
                self.assertEqual(c.demo_calls()[-1]["env"]["DEMO_RESTORE"], "1")
        finally:
            (soak.DEMO_SH, soak.start_caffeinate, guard.entry_read, guard.wait_demo_ready,
             guard.wait_copy_counts, guard.copy_totals) = orig

    def test_run_mode_control_failure_ends_before_the_hook(self):
        """K-copy: a failed control ends the run before the hook; the trap
        removes soak-otlp and the control Job, and the demo is not touched."""
        self.runs_env()
        kubectl = self.fake_kubectl()
        demo = self.script("demo.sh", f"#!/bin/sh\nexec {sys.executable} -I {HERE / 'fakekube.py'} demo-sh \"$@\"\n")
        orig = soak.DEMO_SH, soak.start_caffeinate, guard.entry_read, soak.positive_control
        soak.DEMO_SH, soak.start_caffeinate = demo, lambda: None
        guard.entry_read = lambda r, cfg, at, **kw: guard.GuardRead(at, [guard.Item("x", "ok")])

        def failing(kube, r, run, d, **kw):
            kube.apply(run, [render.control_job(run)])
            return {"verdict": "FAIL", "reason": "counted 2, expected 3"}
        soak.positive_control = failing
        try:
            with self.cluster() as c:
                rc, out, err = self.main("run", "--run", RUN, "--kubectl", str(kubectl), "--tag", "abc1234",
                                         "--hook", "--control", "--duration", "60", "--langs", "go")
                self.assertEqual(rc, 1)
                self.assertIn("positive control: counted 2, expected 3", err)
                self.assertEqual(c.keys(), c.seeded)
                self.assertEqual(c.demo_calls(), [])
        finally:
            soak.DEMO_SH, soak.start_caffeinate, guard.entry_read, soak.positive_control = orig

    def test_copy_totals_error_is_one_line(self):
        """Re-review LOW: a QueryError from copy_totals ends the run with one line."""
        self.runs_env()
        kubectl = self.fake_kubectl()
        demo = self.script("demo.sh", f"#!/bin/sh\nexec {sys.executable} -I {HERE / 'fakekube.py'} demo-sh \"$@\"\n")
        orig = soak.DEMO_SH, soak.start_caffeinate, guard.entry_read, guard.copy_totals
        soak.DEMO_SH, soak.start_caffeinate = demo, lambda: None
        guard.entry_read = lambda r, cfg, at, **kw: guard.GuardRead(at, [guard.Item("x", "ok")])

        def boom(r, at):
            raise sp.verify.QueryError("prometheus unreachable")
        guard.copy_totals = boom
        try:
            with self.cluster():
                rc, out, err = self.main("run", "--run", RUN, "--kubectl", str(kubectl), "--tag", "abc1234",
                                         "--hook", "--langs", "go")
        finally:
            soak.DEMO_SH, soak.start_caffeinate, guard.entry_read, guard.copy_totals = orig
        self.assertEqual(rc, 1)
        self.assertEqual(err.strip(), "soak: copy totals before the hook: prometheus unreachable")

    def full_run(self, *extra, window_read=None, on_trap=None, copy_series=None, pods=None, done_at=2,
                 job_status=None, objects=(), worker_started=None, clock=None, completion_offset=-5.0):
        """A passing gate, hook and load on the fakes, on a clock that follows
        the real one plus every fake sleep (so the loop never sleeps for real
        and stays after the entry gate's real-time records). The k6 Job
        succeeds at the ``done_at``-th window read (``completionTime`` = that
        read's time), or gets ``job_status`` then; None: never. ``objects`` are
        added at the first window read.
        Returns (rc, out, err, cluster, clock)."""
        self.runs_env()
        kubectl = self.fake_kubectl()
        demo = self.script("demo.sh", f"#!/bin/sh\nexec {sys.executable} -I {HERE / 'fakekube.py'} demo-sh \"$@\"\n")
        clock = clock or RealClock()
        ok = guard.Item("x", "ok")
        reads = []
        holder = {}

        def read(r, cfg, roll, at, **kw):
            reads.append(at)
            if len(reads) == 1 and objects:   # the k6 pod, once the Job exists
                holder["c"].edit(lambda st: st["objects"].extend(objects))
            if done_at is not None and len(reads) == done_at:
                status = job_status or {"succeeded": 1, "completionTime": sp.iso(at + completion_offset)}
                holder["c"].edit(lambda st: [o.__setitem__("status", status) for o in st["objects"]
                                             if o["kind"] == "Job"])
            return (window_read or (lambda r, cfg, roll, at, **kw: guard.GuardRead(at, [ok])))(r, cfg, roll, at, **kw)
        started = time.time() - 900 if worker_started is None else worker_started
        default_pods = [{"metadata": {"name": f"demo-{lang}-worker-1a-x", "namespace": sp.APP_NS},
                         "status": {"containerStatuses": [{"name": "worker", "restartCount": 0,
                                                           "state": {"running": {"startedAt": sp.iso(started)}}}]}}
                        for lang in render.LANGS]
        patched = {
            (soak, "DEMO_SH"): demo, (soak, "start_caffeinate"): lambda: None,
            (guard, "entry_read"): lambda r, cfg, at, **kw: guard.GuardRead(at, [ok]),
            (guard, "wait_demo_ready"): lambda *a, **kw: ok, (guard, "wait_copy_counts"): lambda *a, **kw: ok,
            (guard, "copy_totals"): lambda r, at: {},
            (guard, "window_read"): read,
            (soak, "Loop"): lambda *a, **kw: Loop(*a, **{**kw, "clock": clock, "sleep": clock.sleep}),
            (soak.CalOps, "copy_series"): lambda self_, t1, t2: (copy_series or (lambda a, b: []))(t1, t2),
            (soak.CalOps, "pods"): lambda self_: (pods or (lambda: default_pods))(),
        }
        if on_trap is not None:
            real_trap = soak.trap

            def trap(*a, **kw):
                on_trap(clock)
                return real_trap(*a, **kw)
            patched[(soak, "trap")] = trap
        orig = {k: getattr(*k) for k in patched}
        for (obj, name), v in patched.items():
            setattr(obj, name, v)
        try:
            with self.cluster() as c:
                holder["c"] = c
                rc, out, err = self.main("run", "--run", RUN, "--kubectl", str(kubectl), "--tag", "abc1234",
                                         "--hook", "--rate", "2", "--duration", "60", "--langs", "go",
                                         "--worker-interval", "10800", *extra)
        finally:
            for (obj, name), v in orig.items():
                setattr(obj, name, v)
        return rc, out, err, c, clock

    def windows(self) -> list[dict]:
        return [json.loads(x) for x in (self.d / "windows.jsonl").read_text().splitlines()]

    def inputs(self) -> dict:
        return json.loads((self.d / "count-window.json").read_text())

    def test_run_mode_full_window_on_fakes(self):
        """A passing gate, hook and load: the window runs to the k6 Job's end."""
        rc, out, err, c, _ = self.full_run()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("window loaded: started, valid", out)
        self.assertEqual(c.keys(), c.seeded)  # the trap removed the k6 objects and soak-otlp

    def test_run_mode_window_follows_k6_not_the_duration(self):
        """Review HIGH-1: k6 runs pull + duration + graceful stop; the window
        lasts until the Job succeeds (at most its activeDeadlineSeconds), not
        --duration. Here the Job succeeds at the 3rd read (120 s > --duration 60 s);
        k6_end is its completionTime and t2 = the window end + the quiesce."""
        seen = {}

        def on_trap(clock):
            seen["at"] = clock()
            seen["windows"] = [w["name"] for w in self.windows()]
            seen["inputs"] = self.inputs()
        loaded = []

        def read(r, cfg, roll, at, **kw):
            loaded.append(kw.get("loaded"))
            return guard.GuardRead(at, [guard.Item("x", "ok")])
        rc, out, err, c, clock = self.full_run(window_read=read, on_trap=on_trap, done_at=3)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(seen["windows"], ["loaded", "loaded-q"])
        k6w, qw = self.windows()
        self.assertEqual((k6w["ended"], k6w["reads"]), ("load done", 3))
        self.assertGreaterEqual(k6w["end"] - k6w["start"], 120)
        self.assertTrue(qw["valid"])
        self.assertEqual(qw["reads"], 3)          # at 0, 60 and 120 s
        self.assertEqual(loaded, [True] * 6)       # the k6 window's three reads and the quiesce's three
        inp = seen["inputs"]
        self.assertTrue(inp["valid"], inp)
        self.assertEqual(inp["k6_end_source"], "loop window end (laptop clock)")
        self.assertEqual(inp["k6_end"], k6w["end"])
        self.assertGreaterEqual(inp["k6_completion_skew_s"], 5)   # completionTime = the read - 5 s, truncated
        self.assertEqual(inp["t1"], k6w["start"])
        self.assertEqual(inp["t2"], k6w["end"] + soakcount.QUIESCE_S)
        self.assertGreaterEqual(qw["end"], inp["t2"])
        self.assertLessEqual(inp["t2"], seen["at"])  # the restore comes after t2
        self.assertEqual(inp["count_args"], ["--t1", f"{k6w['start']:.3f}", "--t2", f"{inp['t2']:.3f}",
                                             "--k6-log", "raw/soak-k6-loaded.log", "--quiesce", "120",
                                             "--workers", "workers.json", "--worker-interval", "10800"])
        self.assertIn("count window loaded: t1", out)
        self.assertEqual(c.demo_calls()[-1]["env"]["DEMO_RESTORE"], "1")

    def test_run_mode_k6_not_done_at_the_deadline(self):
        """The Job never succeeds while its pod runs: the window ends at the
        Job's deadline ("duration"); no quiesce; the count inputs are invalid."""
        running = k6_pod({"phase": "Running"})
        rc, out, err, _, _ = self.full_run(done_at=None, objects=[running])
        self.assertEqual(rc, 1, out + err)
        k6w, = self.windows()
        self.assertEqual(k6w["ended"], "duration")
        plan = k6script.K6Plan(langs=("go",), rate=2, duration_s=60, timeout_s=10, p99_s=1.0)
        self.assertGreaterEqual(k6w["end"] - k6w["start"], plan.deadline_s)
        inp = self.inputs()
        self.assertFalse(inp["valid"])
        self.assertIn("k6 not done at window end (ended: duration)", inp["reasons"])
        self.assertIsNone(inp["count_args"])
        self.assertEqual((inp["quiesce_window"], inp["k6_completion_time"]), (None, None))

    def test_run_mode_failed_k6_job(self):
        rc, out, err, c, _ = self.full_run(job_status={"failed": 1, "conditions": [
            {"type": "Failed", "reason": "BackoffLimitExceeded", "message": "pod failed"}]})
        self.assertEqual(rc, 1)
        k6w, = self.windows()
        self.assertEqual(k6w["ended"], "invalid")
        inp = self.inputs()
        self.assertFalse(inp["valid"])
        self.assertIn("k6: Job failed (BackoffLimitExceeded: pod failed)", inp["reasons"][0])
        self.assertIsNone(inp["count_args"])
        self.assertEqual(c.keys(), c.seeded)

    def test_run_mode_quiesce_option(self):
        rc, _, err, c, _ = self.full_run("--quiesce", "30")
        self.assertEqual(rc, 1)
        self.assertIn("below 120s", err)
        self.assertEqual(c.demo_calls(), [])      # refused before any write
        rc, out, err, _, _ = self.full_run("--quiesce", "30", "--allow-short-quiesce")
        self.assertEqual(rc, 0, out + err)
        k6w, qw = self.windows()[-2:]
        self.assertEqual(qw["reads"], 1 + 1)   # 0 s, then 60 s >= 30 s
        inp = self.inputs()
        self.assertEqual((inp["t2"], inp["short_quiesce"]), (k6w["end"] + 30, True))
        self.assertEqual(inp["count_args"][inp["count_args"].index("--quiesce") + 1], "30")
        rc, out, err, _, _ = self.full_run("--quiesce", "0", "--allow-short-quiesce")
        self.assertEqual(rc, 0, out + err)
        inp = self.inputs()
        self.assertEqual((inp["t2"], inp["quiesce_window"], inp["quiesce_s"]), (inp["window_end"], None, 0.0))
        for bad in ("-1", "nan", "inf"):
            rc, _, err, _, _ = self.full_run("--quiesce", bad, "--allow-short-quiesce")
            self.assertEqual(rc, 1, bad)
            self.assertIn("a finite number of seconds", err)

    def test_run_mode_breach_in_the_quiesce_invalidates_the_count(self):
        reads = []

        def read(r, cfg, roll, at, **kw):
            reads.append(at)
            state = "breach" if len(reads) == 4 else "ok"
            return guard.GuardRead(at, [guard.Item("sentinels shared", state, "otel-gateway +3")])
        rc, out, err, c, _ = self.full_run(window_read=read)
        self.assertEqual(rc, 1, out + err)
        inp = self.inputs()
        self.assertFalse(inp["valid"])
        self.assertIn("sentinels shared", inp["reasons"][0])
        self.assertIsNone(inp["count_args"])
        self.assertEqual(c.keys(), c.seeded)

    def test_run_mode_no_quiesce_after_an_invalid_window(self):
        def read(r, cfg, roll, at, **kw):
            return guard.GuardRead(at, [guard.Item("sentinels shared", "breach", "otel-gateway +3")])
        rc, out, err, _, _ = self.full_run(window_read=read)
        self.assertEqual(rc, 1)
        self.assertEqual([w["name"] for w in self.windows()], ["loaded"])
        inp = self.inputs()
        self.assertFalse(inp["valid"])
        self.assertIsNone(inp["quiesce_window"])

    def test_run_mode_signal_in_the_quiesce_runs_the_trap_at_once(self):
        """An INT or TERM during the quiesce: the trap runs at once, before the
        quiesce's end; no t2 record and no count-window.json are written."""
        reads, trap_at = [], []

        def read(r, cfg, roll, at, **kw):
            reads.append(at)
            if len(reads) == 3:   # the quiesce's first read
                os.kill(os.getpid(), signal.SIGTERM)
            return guard.GuardRead(at, [guard.Item("x", "ok")])
        rc, out, err, c, clock = self.full_run(window_read=read, on_trap=lambda ck: trap_at.append(ck()))
        self.assertEqual(rc, 128 + signal.SIGTERM)
        self.assertEqual(len(trap_at), 1)
        self.assertEqual([w["name"] for w in self.windows()], ["loaded"])   # the quiesce never finished
        self.assertEqual(c.keys(), c.seeded)
        self.assertEqual(c.demo_calls()[-1]["env"]["DEMO_RESTORE"], "1")
        self.assertFalse((self.d / "count-window.json").exists())
        self.assertNotIn("t2", json.loads((self.d / "workers.json").read_text()))

    def test_run_mode_records_the_workers(self):
        """Defect 2: workers.json at the window start (first drives over the copy
        series since the hook) and at t2, before the trap replaces the pods."""
        calls = []

        def worker(name, started, restarts):
            return {"metadata": {"name": name, "namespace": sp.APP_NS},
                    "status": {"containerStatuses": [{"name": "worker", "restartCount": restarts,
                                                      "state": {"running": {"startedAt": sp.iso(started)}}}]}}

        clock = RealClock()

        def copy_series(t1, t2):
            calls.append((t1, t2))
            clock.offset += 600   # the read sees the drive; the window starts later, as after the hook's waits
            return [{"metric": {"service_name": "demo-python-api", "cluster": "k8s01"},
                     "values": [[t1 - 5, "3"], [t1 + 40, "7"]]}]
        started = float(int(time.time()) - 3600)
        pod_lists = [[worker("demo-go-worker-1a-x", started, 0), worker("demo-python-worker-1a-x", started, 0)],
                     [worker("demo-go-worker-1a-x", started, 1), worker("demo-python-worker-1a-x", started, 0)]]
        rc, out, err, _, _ = self.full_run("--langs", "go,python", copy_series=copy_series,
                                           pods=lambda: pod_lists.pop(0), clock=clock)
        self.assertEqual(rc, 0, out + err)
        w = json.loads((self.d / "workers.json").read_text())
        start, t2 = w["start"]["workers"], w["t2"]["workers"]
        self.assertEqual(start["go"]["startedAt"], started)
        self.assertIsNone(start["go"]["first_drive"])          # Go: startedAt rule, unmeasured
        self.assertEqual(start["python"]["first_drive"], calls[0][0] + 40)
        self.assertEqual(t2["go"], {"pod": "demo-go-worker-1a-x", "startedAt": started, "restartCount": 1})
        self.assertGreaterEqual(w["t2"]["at"], self.inputs()["t2"])
        self.assertLessEqual(w["start"]["at"], self.windows()[0]["start"])
        entry = [json.loads(x) for x in (self.d / "guard.jsonl").read_text().splitlines()
                 if json.loads(x).get("phase") == "entry-after-hook"]
        self.assertGreaterEqual(w["start"]["at"], entry[-1]["at"])   # after the last entry gate

    def test_run_mode_refuses_a_window_count_could_not_qualify(self):
        """Review M2: checked at minute 0, before k6 starts (the trap runs): a
        failed start record, a non-Go worker without a first drive, a Go worker
        whose next drive falls before the planned t2 + quiesce."""
        def boom(t1, t2):
            raise sp.verify.QueryError("prometheus unreachable")
        cases = (({"copy_series": boom}, "workers at the window start: QueryError: prometheus unreachable"),
                 ({"pods": lambda: []}, "go: no startedAt for t1"),
                 ({"worker_started": time.time() - 10800 + 300}, "go: next demo.drive at"))
        for kw, why in cases:
            with self.subTest(why=why):
                rc, out, err, c, _ = self.full_run(**kw)
                self.assertEqual(rc, 1)
                self.assertIn("worker window: " + why, err)
                self.assertIn("not started", err)
                self.assertNotIn("window loaded", out)
                self.assertEqual(c.keys(), c.seeded)
                self.assertEqual(c.demo_calls()[-1]["env"]["DEMO_RESTORE"], "1")
        rc, out, err, _, _ = self.full_run("--langs", "go,python", copy_series=lambda a, b: [])
        self.assertIn("python: no first demo.drive for t1", err)

    def test_run_mode_cluster_clock_ahead(self):
        """Round 2 MEDIUM-C: the cluster clock 5 s ahead of the laptop: t2 is the
        laptop's window end + the quiesce, the window stays valid, the skew is recorded."""
        rc, out, err, _, _ = self.full_run(completion_offset=5.0)
        self.assertEqual(rc, 0, out + err)
        k6w, qw = self.windows()
        inp = self.inputs()
        self.assertTrue(inp["valid"], inp)
        self.assertEqual(inp["t2"], k6w["end"] + soakcount.QUIESCE_S)
        self.assertLessEqual(inp["t2"], qw["end"])
        self.assertGreater(inp["k6_completion_time"], k6w["end"])
        # completionTime is whole seconds (sp.iso drops the fraction: up to 1 s) and is
        # stamped at the read, a few ms before the loop's window end: -6 < skew < -3
        # (the former upper bound -4 failed about one run in five, L4).
        self.assertTrue(-6 < inp["k6_completion_skew_s"] < -3, inp["k6_completion_skew_s"])

    def test_run_mode_hook_and_rate_need_the_worker_interval(self):
        """Round 2 MEDIUM-B: refused before any read or write."""
        self.runs_env()
        kubectl = self.fake_kubectl()
        with self.cluster() as c:
            rc, _, err = self.main("run", "--run", RUN, "--kubectl", str(kubectl), "--tag", "abc1234",
                                   "--hook", "--rate", "2", "--duration", "60", "--langs", "go")
            self.assertEqual(rc, 1)
            self.assertIn("--hook --rate needs --worker-interval", err)
            self.assertEqual(c.calls(), [])

    def test_run_mode_planned_t2_covers_the_last_read(self):
        """Round 2 MEDIUM-A: the loop ends at the first read after the Job's
        deadline; a Go drive just after the naive planned t2 (now + deadline +
        quiesce, + quiesce) is refused."""
        plan = k6script.K6Plan(langs=("go",), rate=2, duration_s=60, timeout_s=10, p99_s=1.0)
        naive = plan.deadline_s + 2 * soakcount.QUIESCE_S
        rc, out, err, c, _ = self.full_run(worker_started=time.time() - 10800 + naive + 30)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("worker window: go: next demo.drive at", err)
        self.assertEqual(c.demo_calls()[-1]["env"]["DEMO_RESTORE"], "1")

    def test_count_inputs_unit(self):
        import argparse
        args = argparse.Namespace(window="loaded", quiesce=120.0)
        res = soak.WindowResult("loaded", True, True, start=NOW, end=NOW + 7200, ended="load done")
        q = soak.WindowResult("loaded-q", True, True, start=NOW + 7200, end=NOW + 7300)   # ended early
        inp = soak.count_inputs(args, res, q, hooked=False, k6_completion=None, interval=10800.0)
        self.assertFalse(inp["valid"])
        self.assertIn("no --hook", inp["reasons"][0])
        self.assertIn("before t2", inp["reasons"][1])
        self.assertIsNone(inp["count_args"])
        self.assertIsNone(inp["workers"])
        # round 2 MEDIUM-C: t2 stays on the laptop clock; completionTime is information with its skew
        inp = soak.count_inputs(args, res, None, hooked=True, k6_completion=NOW + 7300, interval=10800.0)
        self.assertEqual((inp["t2"], inp["k6_completion_time"], inp["k6_completion_skew_s"]),
                         (NOW + 7200, NOW + 7300, -100.0))

    def test_run_mode_gap_before_the_quiesce_invalidates_it(self):
        """A laptop stall between the k6 window and its quiesce (> interval + slack)."""
        real = soak.Loop.window

        def window(self_, name, *a, **kw):
            if name.endswith("-q"):
                self_.clock.offset += soak.GUARD_INTERVAL_S + soak.QUIESCE_GAP_SLACK_S + 1
            return real(self_, name, *a, **kw)
        soak.Loop.window = window
        try:
            rc, out, err, _, _ = self.full_run()
        finally:
            soak.Loop.window = real
        self.assertEqual(rc, 1, out + err)
        self.assertIn("between the window and its quiesce (> 90s)", self.inputs()["reasons"][0])

    def test_worker_record_failure_is_an_error_not_workers(self):
        class Ops:
            def now(self):
                return NOW

            def pods(self):
                raise soak.SoakError("api unreachable")
        rec = soak.worker_record(Ops(), NOW - 100, ["go"], start=False)
        self.assertEqual(rec, {"at": NOW, "error": "SoakError: api unreachable"})

    def test_job_completion(self):
        class K:
            def __init__(self, job):
                self.job = job

            def get(self, args):
                if self.job is None:
                    raise soak.SoakError("gone")
                return self.job
        self.assertEqual(soak.job_completion(K({"status": {"completionTime": sp.iso(NOW)}}), "j"), NOW)
        self.assertIsNone(soak.job_completion(K({"status": {}}), "j"))
        self.assertIsNone(soak.job_completion(K(None), "j"))

if __name__ == "__main__":
    unittest.main()
