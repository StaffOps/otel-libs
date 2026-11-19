"""Tests for preflight.py (standard library unittest; no cluster access)."""

from __future__ import annotations

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preflight  # noqa: E402

Mi = 2**20


def c(cpu: str = "0", mem: str = "0", **extra: str) -> dict:
    return {"resources": {"requests": {"cpu": cpu, "memory": mem}}, **extra}


def node(name: str = "n1", cpu: str = "8", mem: str = "32Gi", pods: str = "200",
         ready: str = "True", unschedulable: bool = False) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"unschedulable": unschedulable},
        "status": {"allocatable": {"cpu": cpu, "memory": mem, "pods": pods},
                   "conditions": [{"type": "Ready", "status": ready}]},
    }


class QuantityTest(unittest.TestCase):
    def test_suffixes(self) -> None:
        cases = {
            "100m": Decimal("0.1"), "2": Decimal(2), "0.5": Decimal("0.5"),
            "128Mi": 128 * Mi, "1Gi": 2**30, "1Ki": 1024, "1Ti": 2**40,
            "1k": 1000, "1M": 10**6, "1G": 10**9, "1e3": 1000,
            "250000000n": Decimal("0.25"), "500u": Decimal("0.0005"), "123456": 123456,
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(preflight.quantity(text), Decimal(want))

    def test_rejects_garbage(self) -> None:
        for text in ("", "Mi", "1Xi", "abc", "1 Gi", "1.2.3", ".", "1..2", "-.e5"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                preflight.quantity(text)


class PodRequestsTest(unittest.TestCase):
    def test_app_containers_sum(self) -> None:
        spec = {"containers": [c("100m", "64Mi"), c("50m", "32Mi")]}
        self.assertEqual(preflight.pod_requests(spec), (Decimal("0.15"), 96 * Mi))

    def test_missing_requests_count_zero(self) -> None:
        self.assertEqual(preflight.pod_requests({"containers": [{}]}), (Decimal(0), Decimal(0)))

    def test_regular_init_container_max_wins(self) -> None:
        spec = {"containers": [c("100m", "64Mi")], "initContainers": [c("500m", "16Mi"), c("10m", "256Mi")]}
        self.assertEqual(preflight.pod_requests(spec), (Decimal("0.5"), 256 * Mi))

    def test_sidecar_adds_to_app_and_to_later_inits(self) -> None:
        spec = {
            "containers": [c("100m", "64Mi")],
            "initContainers": [c("20m", "8Mi", restartPolicy="Always"), c("100m", "64Mi")],
        }
        # app + sidecar = 120m / 72Mi; init after the sidecar = 120m / 72Mi.
        self.assertEqual(preflight.pod_requests(spec), (Decimal("0.12"), 72 * Mi))

    def test_init_container_before_a_sidecar_excludes_it(self) -> None:
        spec = {
            "containers": [c("100m", "64Mi")],
            "initContainers": [c("300m", "200Mi"), c("20m", "8Mi", restartPolicy="Always")],
        }
        # The first init runs before the sidecar starts: 300m / 200Mi beats
        # app + sidecar (120m / 72Mi), with no sidecar added to it.
        self.assertEqual(preflight.pod_requests(spec), (Decimal("0.3"), 200 * Mi))

    def test_overhead_added(self) -> None:
        spec = {"containers": [c("100m", "64Mi")], "overhead": {"cpu": "10m", "memory": "8Mi"}}
        self.assertEqual(preflight.pod_requests(spec), (Decimal("0.11"), 72 * Mi))


class FitsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.n = {"alloc_cpu": Decimal(8), "req_cpu": Decimal("7.5"),
                  "alloc_mem": Decimal(1024 * Mi), "req_mem": Decimal(256 * Mi),
                  "alloc_pods": 10, "pods": 8}
        self.reserve = (Decimal("0.25"), Decimal(512 * Mi), 1)

    def test_exact_boundary_fits(self) -> None:
        self.assertTrue(preflight.fits(self.n, Decimal("0.25"), Decimal(256 * Mi), 1, *self.reserve))

    def test_one_unit_over_fails_each_dimension(self) -> None:
        self.assertFalse(preflight.fits(self.n, Decimal("0.251"), Decimal(256 * Mi), 1, *self.reserve))
        self.assertFalse(preflight.fits(self.n, Decimal("0.25"), Decimal(256 * Mi + 1), 1, *self.reserve))
        self.assertFalse(preflight.fits(self.n, Decimal("0.25"), Decimal(256 * Mi), 2, *self.reserve))


class NodeFreeTest(unittest.TestCase):
    def test_counts_pods_on_ready_schedulable_nodes(self) -> None:
        nodes = {"items": [node("a"), node("b", ready="False"), node("c", unschedulable=True)]}
        pods = {"items": [
            {"spec": {"nodeName": "a", "containers": [c("1", "1Gi")]}},
            {"spec": {"nodeName": "a", "containers": [c("500m", "1Gi")]}},
            {"spec": {"containers": [c("4", "4Gi")]}},  # unscheduled: not counted
        ]}
        free = preflight.node_free(nodes, pods)
        self.assertEqual([n["node"] for n in free], ["a"])
        self.assertEqual(free[0]["req_cpu"], Decimal("1.5"))
        self.assertEqual(free[0]["req_mem"], 2 * 2**30)
        self.assertEqual(free[0]["pods"], 2)


class MainTest(unittest.TestCase):
    def _main(self, nodes: dict, pods: dict, *argv: str) -> tuple[int, str, str]:
        def fake(context: str, *args: str) -> dict:
            return nodes if args[0] == "nodes" else pods

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(preflight, "kubectl_json", side_effect=fake), redirect_stdout(out), redirect_stderr(err):
            rc = preflight.main(["--context", "x", *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_fits_and_does_not_fit(self) -> None:
        nodes = {"items": [node(cpu="1", mem="2Gi", pods="10")]}
        rc, out, _ = self._main(nodes, {"items": []}, "--cpu", "750m", "--memory", "1536Mi", "--pods", "9")
        self.assertEqual(rc, 0)
        self.assertIn("-> fits", out)
        rc, out, _ = self._main(nodes, {"items": []}, "--cpu", "751m", "--memory", "1Gi", "--pods", "1")
        self.assertEqual(rc, 1)
        self.assertIn("does not fit", out)

    def test_no_node_is_an_error(self) -> None:
        rc, _, err = self._main({"items": []}, {"items": []}, "--cpu", "1m", "--memory", "1Mi", "--pods", "1")
        self.assertEqual(rc, 2)
        self.assertIn("no schedulable Ready node", err)

    def test_bad_quantity_and_kubectl_failure(self) -> None:
        for bad in ("lots", "1.2.3", "."):
            rc, out, err = self._main({"items": []}, {"items": []}, "--cpu", bad, "--memory", "1Mi", "--pods", "1")
            self.assertEqual(rc, 2)
            self.assertIn(f"bad quantity {bad!r}", err)
            self.assertEqual(out, "")
        out, err = io.StringIO(), io.StringIO()
        boom = subprocess.CalledProcessError(1, ["kubectl"])
        with mock.patch.object(preflight, "kubectl_json", side_effect=boom), redirect_stdout(out), redirect_stderr(err):
            rc = preflight.main(["--context", "x", "--cpu", "1m", "--memory", "1Mi", "--pods", "1"])
        self.assertEqual(rc, 2)
        self.assertIn("preflight:", err.getvalue())

    def test_node_without_allocatable_is_a_clean_error(self) -> None:
        broken = node()
        del broken["status"]["allocatable"]
        rc, out, err = self._main({"items": [broken]}, {"items": []}, "--cpu", "1m", "--memory", "1Mi", "--pods", "1")
        self.assertEqual(rc, 2)
        self.assertIn("no complete status.allocatable", err)
        self.assertNotIn("Traceback", err)


class KubectlJsonTest(unittest.TestCase):
    def test_argv_and_parse(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout='{"items": []}', stderr="")
        with mock.patch.object(preflight.subprocess, "run", return_value=done) as run:
            got = preflight.kubectl_json("k8s01", "pods", "--all-namespaces", "--field-selector",
                                         "status.phase!=Succeeded,status.phase!=Failed")
        self.assertEqual(got, {"items": []})
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["kubectl", "--context", "k8s01", "get", "pods", "--all-namespaces",
                                "--field-selector", "status.phase!=Succeeded,status.phase!=Failed",
                                "-o", "json"])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_main_reads_non_terminated_pods(self) -> None:
        calls = []

        def fake(context: str, *args: str) -> dict:
            calls.append((context, args))
            return {"items": [node()]} if args[0] == "nodes" else {"items": []}

        with mock.patch.object(preflight, "kubectl_json", side_effect=fake), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            preflight.main(["--context", "k8s01", "--cpu", "1m", "--memory", "1Mi", "--pods", "1"])
        self.assertIn(("k8s01", ("pods", "--all-namespaces", "--field-selector",
                                 "status.phase!=Succeeded,status.phase!=Failed")), calls)


if __name__ == "__main__":
    unittest.main()
