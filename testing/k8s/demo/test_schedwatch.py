"""Tests for schedwatch.py (standard library unittest; no cluster access)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schedwatch  # noqa: E402

SINCE = datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc)
NS = "dev-otel-libs"


def event(ns: str = "platform", name: str = "p1", msg: str = "0/1 nodes are available: 1 Too many pods.",
          last: str | None = "2026-11-16T18:05:00Z", kind: str = "Pod", reason: str = "FailedScheduling",
          **extra) -> dict:
    ev = {"reason": reason, "message": msg,
          "involvedObject": {"kind": kind, "namespace": ns, "name": name},
          "metadata": {"namespace": ns, "creationTimestamp": "2026-11-16T17:00:00Z"}}
    if last is not None:
        ev["lastTimestamp"] = last
    ev.update(extra)
    return ev


def pending(*keys: str, phase: str = "Pending") -> dict:
    items = []
    for key in keys:
        ns, name = key.split("/")
        items.append({"metadata": {"namespace": ns, "name": name}, "status": {"phase": phase}})
    return {"items": items}


class ParseTime(unittest.TestCase):
    def test_zulu_and_fraction_and_offset(self):
        self.assertEqual(schedwatch.parse_time("2026-11-16T18:00:00Z"), SINCE)
        self.assertEqual(schedwatch.parse_time("2026-11-16T18:00:00.123456Z").microsecond, 123456)
        self.assertEqual(schedwatch.parse_time("2026-11-16T15:00:00-03:00"), SINCE)

    def test_naive_is_utc_and_bad_is_none(self):
        self.assertEqual(schedwatch.parse_time("2026-11-16T18:00:00"), SINCE)
        self.assertIsNone(schedwatch.parse_time("yesterday"))
        self.assertIsNone(schedwatch.parse_time(None))
        self.assertIsNone(schedwatch.parse_time(""))


class EventTime(unittest.TestCase):
    def test_newest_of_all_fields(self):
        ev = {"lastTimestamp": None, "eventTime": "2026-11-16T18:01:00.000000Z",
              "series": {"lastObservedTime": "2026-11-16T18:09:00.000000Z"},
              "metadata": {"creationTimestamp": "2026-11-16T17:00:00Z"}}
        self.assertEqual(schedwatch.event_time(ev), datetime(2026, 11, 16, 18, 9, tzinfo=timezone.utc))

    def test_none_when_no_time(self):
        self.assertIsNone(schedwatch.event_time({}))


class Hits(unittest.TestCase):
    def test_hit_for_each_phrase(self):
        for phrase in schedwatch.PHRASES:
            with self.subTest(phrase=phrase):
                got = schedwatch.hits({"items": [event(msg=f"0/1 nodes: 1 {phrase}.")]},
                                      pending("platform/p1"), NS, SINCE)
                self.assertEqual(got, [("platform/p1", phrase)])

    def test_scope_filters(self):
        cases = {
            "own namespace": event(ns=NS),
            "older than the run": event(last="2026-11-16T17:59:59Z"),
            "other reason": event(reason="Scheduled"),
            "not a pod": event(kind="Node"),
            "other message": event(msg="0/1 nodes: node(s) had untolerated taint"),
            "no time": event(last=None, metadata={"namespace": "platform"}),
        }
        for label, ev in cases.items():
            with self.subTest(label=label):
                self.assertEqual(schedwatch.hits({"items": [ev]}, pending("platform/p1", f"{NS}/p1"), NS, SINCE), [])

    def test_pod_no_longer_pending(self):
        self.assertEqual(schedwatch.hits({"items": [event()]}, pending(), NS, SINCE), [])
        self.assertEqual(schedwatch.hits({"items": [event()]}, pending("platform/p1", phase="Running"), NS, SINCE), [])

    def test_events_k8s_io_shape_and_dedup(self):
        ev = {"reason": "FailedScheduling", "note": "Insufficient memory",
              "regarding": {"kind": "Pod", "namespace": "a", "name": "x"},
              "eventTime": "2026-11-16T18:30:00Z"}
        got = schedwatch.hits({"items": [ev, event(ns="a", name="x")]}, pending("a/x"), NS, SINCE)
        self.assertEqual(got, [("a/x", "Insufficient memory")])

    def test_namespace_from_metadata(self):
        ev = event()
        del ev["involvedObject"]["namespace"]
        self.assertEqual(schedwatch.hits({"items": [ev]}, pending("platform/p1"), NS, SINCE),
                         [("platform/p1", "Too many pods")])


class Main(unittest.TestCase):
    def run_main(self, outputs, argv=None):
        results = [mock.Mock(stdout=json.dumps(o)) for o in outputs]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(schedwatch.subprocess, "run", side_effect=results) as run, \
                redirect_stdout(out), redirect_stderr(err):
            rc = schedwatch.main(argv or ["--context", "k8s01", "--namespace", NS,
                                          "--since", "2026-11-16T18:00:00Z"])
        return rc, out.getvalue(), err.getvalue(), run

    def test_no_hit(self):
        rc, out, _, run = self.run_main([{"items": []}, {"items": []}])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(run.call_args_list[0].args[0][:4], ["kubectl", "--context", "k8s01", "get"])

    def test_hit_prints_name_not_message(self):
        secretish = "Too many pods. token=" + "a" * 48
        rc, out, _, _ = self.run_main([{"items": [event(msg=secretish)]}, pending("platform/p1")])
        self.assertEqual(rc, schedwatch.EXIT_HIT)
        self.assertEqual(rc, 10)
        self.assertIn("platform/p1", out)
        self.assertNotIn("token=", out)

    def test_bad_since(self):
        rc, _, err, _ = self.run_main([], ["--context", "k", "--namespace", NS, "--since", "soon"])
        self.assertEqual(rc, 2)
        self.assertIn("bad --since", err)

    def test_kubectl_failure(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(schedwatch.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(1, "kubectl")), \
                redirect_stdout(out), redirect_stderr(err):
            rc = schedwatch.main(["--context", "k", "--namespace", NS, "--since", "2026-11-16T18:00:00Z"])
        self.assertEqual(rc, 2)
        self.assertIn("CalledProcessError", err.getvalue())

    def test_timeout_missing_kubectl_and_any_error_are_blind(self):
        for exc in (subprocess.TimeoutExpired("kubectl", 60), FileNotFoundError("kubectl"),
                    OSError("x"), RuntimeError("x")):
            with self.subTest(exc=type(exc).__name__):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(schedwatch.subprocess, "run", side_effect=exc), \
                        redirect_stdout(out), redirect_stderr(err):
                    rc = schedwatch.main(["--context", "k", "--namespace", NS,
                                          "--since", "2026-11-16T18:00:00Z"])
                self.assertEqual(rc, 2)
                self.assertEqual(err.getvalue().strip(), f"schedwatch: {type(exc).__name__}")

    def test_kubectl_has_a_timeout(self):
        _, _, _, run = self.run_main([{"items": []}, {"items": []}])
        self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_bad_json(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(schedwatch.subprocess, "run", return_value=mock.Mock(stdout="{")), \
                redirect_stdout(out), redirect_stderr(err):
            rc = schedwatch.main(["--context", "k", "--namespace", NS, "--since", "2026-11-16T18:00:00Z"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
