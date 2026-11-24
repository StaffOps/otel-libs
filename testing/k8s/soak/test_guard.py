"""Tests for the guard adapter (guard.py) over K1's soakplatform: item states,
the window read (scoped sentinels, dropped (g)/(h), copy-roll exemptions),
the floors, blind reads and (h) at window start. The fake Prometheus / kubectl
reader is K1's (testing/k8s/demo/test_platform.py), not a copy."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "demo"))

import guard  # noqa: E402
import test_platform as tp  # noqa: E402
from guard import sp  # noqa: E402

NOW = tp.NOW


def healthy() -> tp.FakeReader:
    r = tp.healthy_reader()
    r.instant.append((r"node_memory_MemAvailable_bytes", [tp.instant(tp.k8s(), 20 * 2**30)]))
    r.instant.append((r"node_cpu_seconds_total", [tp.instant(tp.k8s(instance="n"), 0.6)]))
    r.instant.append((r"node_filesystem_avail_bytes", [tp.instant(tp.k8s(), 0.5)]))
    return r


def soak_reader(r: tp.FakeReader, services=("demo-go-api",)) -> tp.FakeReader:
    r.instant.insert(0, (r"otel_libs_soak_spans_total",
                         [tp.instant(tp.k8s(service_name=s), 5) for s in services]))
    return r


class Blind(tp.FakeReader):
    def prom(self, q, at):
        raise sp.verify.QueryError("prometheus unreachable")

    def kget(self, args):
        raise sp.verify.QueryError("api unreachable")


def states(read: guard.GuardRead) -> dict[str, str]:
    return {i.name: i.state for i in read.items}


class EntryRead(unittest.TestCase):
    def test_healthy_platform_passes(self):
        read = guard.entry_read(healthy(), guard.GuardConfig(), NOW)
        self.assertEqual(read.breaches, [], json.dumps(read.as_json(), indent=1))
        self.assertFalse(read.blind)
        self.assertNotIn("guard (h) copy counts", states(read))
        self.assertIn("guard (a) ztunnel proxies", states(read))

    def test_noisy_shared_sentinel_blocks_the_gate(self):
        r = healthy()
        name = "otelcol_exporter_send_failed_spans"
        r.raw[name] = [tp.series(tp.k8s(__name__=name, job=sp.GATEWAY_JOB),
                                 [(NOW - 1800, 0), (NOW - 600, 3), (NOW, 3)])]
        read = guard.entry_read(r, guard.GuardConfig(), NOW)
        self.assertIn("gate sentinels clean", [i.name for i in read.breaches])
        self.assertTrue(read.reason().startswith("platform: guard (gate sentinels clean"))

    def test_mesh_breach_is_reason_mesh(self):
        r = healthy()
        r.pods.append(tp.pod_obj(sp.APP_NS, "extra-pod", NOW - 5 * tp.DAY))  # annotated, not proxied
        read = guard.entry_read(r, guard.GuardConfig(), NOW)
        self.assertTrue(read.reason().startswith("platform: mesh (guard (a)"), read.reason())

    def test_missing_roll_role(self):
        r = healthy()
        r.pods = [p for p in r.pods if not p["metadata"]["name"].startswith("istiod")]
        read = guard.entry_read(r, guard.GuardConfig(), NOW)
        self.assertIn("guard (d) roll list istiod present", [i.name for i in read.breaches])

    def test_entry_gate_reads_guard_a_twice(self):
        r = healthy()
        extra = tp.pod_obj(sp.APP_NS, "settled-unproxied", NOW - tp.DAY)
        r.pods.append(extra)
        slept = []
        persistent = guard.entry_read(r, guard.GuardConfig(), NOW, clock=lambda: NOW + 60, sleep=slept.append)
        self.assertEqual(slept, [60])
        self.assertIn(guard.A_ITEM, [i.name for i in persistent.breaches])

        def vanish(s):
            slept.append(s)
            r.pods.remove(extra)
        transient = guard.entry_read(r, guard.GuardConfig(), NOW, clock=lambda: NOW + 60, sleep=vanish)
        self.assertEqual(transient.breaches, [], transient.as_json())
        self.assertNotIn("guard (d) roll list vs previous read", states(transient))

    def test_blind(self):
        read = guard.entry_read(Blind(), guard.GuardConfig(), NOW)
        self.assertTrue(read.blind, read.as_json())

    def test_query_error_inside_the_guard_is_blind_item(self):
        r = healthy()

        def boom(q, at, _orig=r.prom):
            if "istio_tcp_connections_failed_total" in q:
                raise sp.verify.QueryError("timeout")
            return _orig(q, at)
        r.prom = boom
        read = guard.entry_read(r, guard.GuardConfig(), NOW)
        self.assertEqual(states(read)["guard"], "blind")
        self.assertTrue(read.blind)  # a core look-up failed (review M3)
        self.assertFalse(guard.GuardRead(NOW, [guard.Item("floor x", "blind"), guard.Item("y", "ok")]).blind)


class WindowRead(unittest.TestCase):
    def read(self, r, loaded=True, roll=None):
        return guard.window_read(r, guard.GuardConfig(), roll or guard.RollState(), NOW, loaded=loaded)

    def test_healthy_window_drops_g_and_h(self):
        read = self.read(soak_reader(healthy()))
        self.assertEqual(read.breaches, [], read.as_json())
        names = [i.name for i in read.items]
        self.assertFalse(any(n.startswith(("guard (g)", "guard (h)", "gate sentinels")) for n in names))
        self.assertIn("sentinels", names)

    def test_shared_rise_breaches_loaded_only_copy_rise_is_a_finding(self):
        for loaded, state in ((True, "breach"), (False, "finding")):
            r = soak_reader(healthy())
            name = "otelcol_receiver_refused_spans"
            r.raw[name] = [tp.series(tp.k8s(__name__=name, job=sp.AGENT_JOB), [(NOW - 100, 0), (NOW - 10, 2)])]
            r.raw["otelcol_exporter_enqueue_failed_spans"] = [tp.series(
                tp.k8s(__name__="otelcol_exporter_enqueue_failed_spans", job=sp.COPY_JOB),
                [(NOW - 100, 0), (NOW - 10, 1)])]
            st = states(self.read(r, loaded=loaded))
            self.assertEqual(st["sentinels shared"], state)
            self.assertEqual(st["sentinels copy"], "finding")

    def test_telemetry_edge_breaches_other_edge_is_finding(self):
        r = soak_reader(healthy())
        m = dict(source_workload_namespace=sp.APP_NS, source_workload="demo-go-api",
                 destination_workload_namespace=sp.APP_NS, destination_workload="soak-agent",
                 response_flags="CONNECT")
        other = dict(m, destination_workload="demo-postgres")
        r.raw["istio_tcp_connections_failed_total"] = [
            tp.series(tp.k8s(**m), [(NOW - 100, 0), (NOW - 5, 1)]),
            tp.series(tp.k8s(**other), [(NOW - 100, 0), (NOW - 5, 4)])]
        read = self.read(r)
        self.assertTrue(read.reason().startswith("platform: mesh (guard (b) edge demo->soak-agent"), read.reason())
        self.assertEqual(states(read)["guard (b) other edges"], "finding")

    def open_roll(self, n=1):
        return guard.RollState(exempt_copy_rolls=n, exempt_from=NOW - 600, exempt_until=NOW + 600)

    def test_copy_roll_exemptions(self):
        r = soak_reader(healthy())
        copy = [p for p in r.pods if p["metadata"]["name"].startswith("soak-agent")][0]
        copy["status"]["containerStatuses"][0]["state"]["running"]["startedAt"] = sp.iso(NOW - 30)
        roll = self.open_roll(1)
        first = self.read(r, roll=roll)
        self.assertEqual(states(first)["guard (d,e) roll list unchanged"], "ok")
        self.assertEqual(roll.exempt_copy_rolls, 0)
        again = self.read(r, roll=roll)  # the same start seen by the overlapping next read
        self.assertEqual(states(again)["guard (d,e) roll list unchanged"], "ok")
        copy["status"]["containerStatuses"][0]["state"]["running"]["startedAt"] = sp.iso(NOW - 5)
        self.assertEqual(states(self.read(r, roll=roll))["guard (d,e) roll list unchanged"], "breach")

    def test_exemption_outside_its_time_bound(self):
        r = soak_reader(healthy())
        copy = [p for p in r.pods if p["metadata"]["name"].startswith("soak-agent")][0]
        copy["status"]["containerStatuses"][0]["state"]["running"]["startedAt"] = sp.iso(NOW - 30)
        late = guard.RollState(exempt_copy_rolls=2, exempt_from=NOW - 7200, exempt_until=NOW - 3600)
        self.assertEqual(states(self.read(r, roll=late))["guard (d,e) roll list unchanged"], "breach")

    def test_no_exemption_for_other_pods(self):
        r = soak_reader(healthy())
        zt = [p for p in r.pods if p["metadata"]["name"].startswith("ztunnel")][0]
        zt["status"]["containerStatuses"][0]["state"]["running"]["startedAt"] = sp.iso(NOW - 30)
        read = self.read(r, roll=self.open_roll(2))
        self.assertEqual(states(read)["guard (d,e) roll list unchanged"], "breach")

    def test_copy_pod_absent_during_an_exempt_roll(self):
        r = soak_reader(healthy())
        r.pods = [p for p in r.pods if not p["metadata"]["name"].startswith("soak-agent")]
        self.assertEqual(states(self.read(r, roll=self.open_roll()))["guard (d) roll list soak-agent present"], "ok")
        self.assertEqual(states(self.read(r))["guard (d) roll list soak-agent present"], "breach")

    def test_restart_count_and_presence_between_reads(self):
        """Review M8: every pod of the previous read stays, none restarts."""
        r = soak_reader(healthy())
        roll = guard.RollState()
        self.assertNotIn("guard (d) roll list vs previous read", states(self.read(r, roll=roll)))
        self.assertEqual(states(self.read(r, roll=roll))["guard (d) roll list vs previous read"], "ok")
        tempo = [p for p in r.pods if p["metadata"]["name"] == "tempo-0"][0]
        tempo["status"]["containerStatuses"][0]["restartCount"] = 1
        read = self.read(r, roll=roll)
        self.assertEqual(states(read)["guard (d) roll list vs previous read"], "breach")
        self.assertIn("tempo-0/c restartCount 0 -> 1", [i.detail for i in read.items
                                                         if i.name == "guard (d) roll list vs previous read"][0])
        r.pods = [p for p in r.pods if not p["metadata"]["name"].startswith("istio-cni-node")]
        detail = [i.detail for i in self.read(r, roll=roll).items if i.name == "guard (d) roll list vs previous read"][0]
        self.assertIn("istio-cni-node", detail)
        self.assertIn("gone", detail)

    def test_copy_replaced_needs_an_open_exemption(self):
        r = soak_reader(healthy())
        for roll, want in ((guard.RollState(), "breach"), (self.open_roll(), "ok")):
            rr = soak_reader(healthy())
            self.read(rr, roll=roll)
            copy = [p for p in rr.pods if p["metadata"]["name"].startswith("soak-agent")][0]
            copy["metadata"]["name"] = "soak-agent-5f4d7fd6f8-newpd"
            self.assertEqual(states(self.read(rr, roll=roll))["guard (d) roll list vs previous read"], want)
        del r

    def test_guard_a_needs_two_consecutive_mismatches(self):
        """Re-review HIGH-1: a one-read mismatch passes, a persistent one breaches."""
        r = soak_reader(healthy())
        roll = guard.RollState()
        extra = tp.pod_obj(sp.APP_NS, "settled-unproxied", NOW - tp.DAY)
        r.pods.append(extra)
        self.assertEqual(states(self.read(r, roll=roll))[guard.A_ITEM], "finding")
        r.pods.remove(extra)  # transient: gone at the next read (counted as in transition)
        self.assertEqual(states(self.read(r, roll=roll))[guard.A_ITEM], "ok")
        r.pods.append(extra)
        self.assertEqual(states(self.read(r, roll=roll))[guard.A_ITEM], "finding")
        read = self.read(r, roll=roll)
        self.assertEqual(states(read)[guard.A_ITEM], "breach")  # persistent
        self.assertTrue(read.reason().startswith("platform: mesh (guard (a)"))

    def test_guard_a_counts_a_deleted_pod_of_the_previous_read(self):
        r = soak_reader(healthy())
        roll = guard.RollState()
        self.read(r, roll=roll)
        gone = [p for p in r.pods if p["metadata"]["name"].startswith("demo-go-api")][0]
        r.pods.remove(gone)  # deleted; ztunnel's scraped count still has it
        self.assertEqual(states(self.read(r, roll=roll))[guard.A_ITEM], "ok")

    def test_blind_window_read(self):
        self.assertTrue(self.read(Blind()).blind)

    def test_reads_cover_twice_the_interval(self):
        r = soak_reader(healthy())
        self.read(r)
        width = 120 + sp.BORN_GRACE + sp.SCRAPE_S  # review H2: the reach before the start
        self.assertTrue(any(q.endswith(f"[{width}s]") for q in r.queries), r.queries[:5])


class Retried(tp.FakeReader):
    """K1's fake reader whose first Prometheus look-up needed one transport retry."""

    def __init__(self, base: tp.FakeReader):
        self.__dict__.update(base.__dict__)
        self.retries, self.retry_error = 0, ""

    def prom(self, q, at):
        if not self.retries:
            self.retries, self.retry_error = 1, "Unable to connect to the server: connection reset by peer"
        return super().prom(q, at)


class TransportRetries(unittest.TestCase):
    """Correction 2026-11-24: a retried look-up that answered is judged on its
    answer, and the retries are in guard.jsonl."""

    def test_retried_then_answered_is_ok_and_recorded(self):
        r = Retried(soak_reader(healthy()))
        read = guard.window_read(r, guard.GuardConfig(), guard.RollState(), NOW, loaded=True)
        self.assertFalse(read.blind)
        self.assertEqual(read.breaches, [], read.as_json())
        rec = read.as_json()
        self.assertEqual(rec["retries"], 1)
        self.assertIn("connection reset", rec["retry_error"])
        # A second read counts only its own retries.
        again = guard.window_read(r, guard.GuardConfig(), guard.RollState(), NOW, loaded=True)
        self.assertEqual(again.as_json()["retries"], 0)
        self.assertNotIn("retry_error", again.as_json())

    def test_entry_read_records_retries(self):
        read = guard.entry_read(Retried(healthy()), guard.GuardConfig(), NOW)
        self.assertEqual(read.retries, 1)

    def test_reader_without_counters_records_zero(self):
        self.assertEqual(guard.window_read(soak_reader(healthy()), guard.GuardConfig(), guard.RollState(),
                                           NOW, loaded=True).as_json()["retries"], 0)


class AfterTheHook(unittest.TestCase):
    """Review B1 and M7: (g) polled after the roll, (h) as an increase."""

    def test_wait_demo_ready_skips_terminating_and_waits(self):
        r = healthy()
        t = [NOW]
        new = [p for p in r.pods if p["metadata"]["name"].startswith("demo-go-api")][0]
        old = json.loads(json.dumps(new))
        old["metadata"]["name"] = "demo-go-api-0ld0ld-zzzzz"
        old["metadata"]["deletionTimestamp"] = sp.iso(NOW - 1)
        r.pods.append(old)  # the Terminating pod of the old ReplicaSet
        new["status"]["conditions"][0]["lastTransitionTime"] = sp.iso(NOW - 5)  # just restarted
        new["status"]["containerStatuses"][0]["state"]["running"]["startedAt"] = sp.iso(NOW - 5)
        cfg = guard.GuardConfig()

        def sleep(s):
            t[0] += s
        item = guard.wait_demo_ready(r, cfg, lambda: t[0], sleep)
        self.assertEqual(item.state, "ok", item.detail)
        self.assertGreaterEqual(t[0] - NOW, 595)

    def test_wait_demo_ready_gives_up(self):
        r = healthy()
        r.pods = [p for p in r.pods if not p["metadata"]["name"].startswith("demo-node-api")]
        t = [NOW]
        cfg = guard.GuardConfig(ready_s=60, ready_margin_s=60)
        item = guard.wait_demo_ready(r, cfg, lambda: t[0], lambda s: t.__setitem__(0, t[0] + s))
        self.assertEqual(item.state, "breach")
        self.assertIn("11 of 12", item.detail)
        advance = lambda s: t.__setitem__(0, t[0] + s)  # noqa: E731
        cfg_sub = guard.GuardConfig(langs=("go",))
        self.assertEqual(guard.wait_demo_ready(r, cfg_sub, lambda: t[0], advance).state, "ok")
        self.assertEqual(guard.wait_demo_ready(Blind(), cfg, lambda: t[0], advance).state, "blind")

    def test_copy_counts_are_an_increase_since_the_hook(self):
        r = soak_reader(healthy(), services=("demo-go-api",))
        since = guard.copy_totals(r, NOW)
        self.assertEqual(since, {"demo-go-api": 5.0})
        self.assertEqual(guard.copy_counts(r, ["demo-go-api"], NOW, since).state, "breach")
        self.assertEqual(guard.copy_counts(r, ["demo-go-api"], NOW, {"demo-go-api": 4}).state, "ok")
        self.assertEqual(guard.copy_counts(r, ["demo-go-api"], NOW).state, "ok")


class Floors(unittest.TestCase):
    def test_breach_blind_and_no_series(self):
        r = healthy()
        cfg = guard.GuardConfig()
        self.assertEqual({i.state for i in guard.floors(r, cfg, NOW)}, {"ok"})
        cfg.floors["mem_available_bytes"] = 64 * 2**30
        self.assertEqual(guard.floors(r, cfg, NOW)[0].state, "breach")
        self.assertEqual({i.state for i in guard.floors(Blind(), cfg, NOW)}, {"blind"})
        empty = tp.FakeReader()
        self.assertEqual(guard.floors(empty, cfg, NOW)[0].detail, "no series")

    def test_floors_from_a_passing_recent_k1_snapshot_only(self):
        """Review M14 and H7: K1, PASS, <= 48 h; never below the initial floors."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "snapshot.json"
            good = {"task": "K1", "verdict": "PASS", "taken_at": sp.iso(NOW - 3600),
                    "floors": {"mem_available_bytes": {"floor": 5e9}, "idle_cpu_share": {"floor": None},
                               "rootfs_free_share": {"floor": 0.01}}}
            p.write_text(json.dumps(good))
            cfg = guard.GuardConfig.from_snapshot(p, NOW)
            self.assertEqual(cfg.snapshot, str(p))
            self.assertEqual(cfg.floors["mem_available_bytes"], 5e9)
            self.assertEqual(cfg.floors["idle_cpu_share"], sp.INITIAL_FLOORS["idle_cpu_share"])
            self.assertEqual(cfg.floors["rootfs_free_share"], sp.INITIAL_FLOORS["rootfs_free_share"])
            for bad, why in (({**good, "verdict": "FAIL"}, "verdict FAIL"), ({**good, "task": "K9"}, "not a K1"),
                             ({**good, "taken_at": sp.iso(NOW - 49 * 3600)}, "older than 48 h"),
                             ({**good, "taken_at": None}, "no taken_at")):
                p.write_text(json.dumps(bad))
                cfg = guard.GuardConfig.from_snapshot(p, NOW)
                self.assertEqual(cfg.floors, sp.INITIAL_FLOORS)
                self.assertIn(why, cfg.snapshot)
            self.assertIn("none", guard.GuardConfig.from_snapshot(Path(tmp) / "missing", NOW).snapshot)


class CopyCounts(unittest.TestCase):
    def test_wait_then_report(self):
        t = [NOW]

        def sleep(s):
            t[0] += s
        r = soak_reader(healthy(), services=("demo-go-api", "soak-control"))
        ok = guard.wait_copy_counts(r, guard.GuardConfig(), ["demo-go-api"], lambda: t[0], sleep)
        self.assertEqual(ok.state, "ok")
        late = guard.wait_copy_counts(r, guard.GuardConfig(), ["demo-go-api", "demo-go-worker"], lambda: t[0], sleep)
        self.assertEqual(late.state, "breach")
        self.assertIn("demo-go-worker", late.detail)
        self.assertGreaterEqual(t[0] - NOW, 60)
        self.assertEqual(guard.copy_counts(Blind(), ["x"], NOW).state, "blind")


class ReadObject(unittest.TestCase):
    def test_reason_and_json(self):
        read = guard.GuardRead(NOW, [guard.Item("floor x", "breach", "d"), guard.Item("y", "finding", "f")])
        self.assertEqual(read.reason(), "platform: guard (floor x: d)")
        self.assertEqual(len(read.findings), 1)
        self.assertFalse(read.as_json()["blind"])
        self.assertFalse(guard.GuardRead(NOW, []).blind)

    def test_quiet_report_never_prints(self):
        rep = guard.QuietReport()
        rep.check(True, "a", "d")
        rep.na("b", "d")
        rep.info("c", "d")
        rep.error("e", ValueError("x"))
        self.assertEqual([i.state for i in rep.collected], ["ok", "na", "blind"])


if __name__ == "__main__":
    unittest.main()
