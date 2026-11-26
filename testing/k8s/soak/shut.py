"""The K-shut + K3 + K5 driver (specs/soak-simulation, r6.2 task "K-shut + K3 +
K5"; SR15 "Shutdown (r6.2)", "Restart mechanics (r6.2)", "Identity (K3)",
"Labels (K5)"; SR7, SR12; design K-D22 draft).

``soak.py shut`` runs it inside the same pre-flight, entry gate, hook, trap and
guarded loop as ``soak.py run``. This module holds the plan, the pod watch,
the pure derivations and the deletion step; it talks to the cluster only
through an ``ops`` object (``soak.ShutOps`` on the cluster, a fake in the
tests).

One k6 Job runs two scenarios per language, ``phase=pre`` (0..pre) and
``phase=post`` (pre..pre+post). The loop's ``tick`` (``Shutter.tick``) reads
the k6 container's ``state.running.startedAt``, and at ``startedAt`` + pre +
delta reads the copy's fixed count per api over (t1, now] (the old
``service_instance_id`` is the one with an increase), then deletes each api
pod by name. The hook's own apply is the nonce roll (every hook file carries
a new ``DEMO_RESTART_NONCE``): its old pods are the demo pods before the hook,
its new pods the hooked ones. A watch of the demo pods records every pod's
``finishedAt`` and exit code; the pod objects are parsed in memory and never
written (the hooked pods' env carries the canary header).

Outputs, under ``runs/<run>/``: ``shut-input.json`` (``verify.py soak shut
--input``, the schema of ``soakcount.action_shut``), ``identity-input.json``
(``verify.py soak identity`` and ``soak labels``, ``testing/k8s/soakident.py``),
``shut-window.json`` (t1, t2, validity, the verify commands) and
``pods.jsonl`` (the watch's pod states).
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import k6script
import render
import soakcount

sp = soakcount.sp
verify = soakcount.verify

DEFAULT_PRE_S = 300
DEFAULT_POST_S = 300
DEFAULT_DELTA_S = 30
K6_POLL_S = 5                 # how often the tick reads the k6 pod before it starts
# The old instance's copy series must still be live when `verify.py soak shut`
# reads it: copy expiry (30 min) minus a margin for the read after t2.
COPY_SAFE_S = soakcount.COPY_EXPIRY_S - 300
FINISH_RESOLUTION_S = 1       # finishedAt and startedAt are whole seconds (RFC 3339)
POD_NAME_RE = re.compile(r"demo-(go|python|node|dotnet)-(worker|api|backend)-[a-z0-9]+-[a-z0-9]+")
WINDOW = "shut"


class ShutError(k6script.PlanError):
    """The K-shut plan or its preconditions do not hold."""


@dataclass(frozen=True)
class ShutPlan:
    langs: tuple[str, ...]
    rate: int
    pre_s: int = DEFAULT_PRE_S
    post_s: int = DEFAULT_POST_S
    delta_s: int = DEFAULT_DELTA_S
    quiesce_s: float = soakcount.QUIESCE_S
    worker_interval_s: int = int(soakcount.DEFAULT_WORKER_INTERVAL_S)
    timeout_s: int = 10
    p99_s: float = 1.0
    guard_interval_s: int = 60

    def k6(self) -> k6script.K6Plan:
        return k6script.K6Plan(langs=tuple(self.langs), rate=self.rate, duration_s=self.pre_s + self.post_s,
                               timeout_s=self.timeout_s, p99_s=self.p99_s,
                               phases=(k6script.Phase("pre", 0, self.pre_s),
                                       k6script.Phase("post", self.pre_s, self.post_s)))

    @property
    def tail_s(self) -> float:
        """From the planned deletion to t2 at the latest: the rest of post, the
        graceful stop and summary, one loop interval plus a read, the quiesce."""
        k = self.k6()
        return (self.post_s - self.delta_s + k.graceful_stop_s + k.summary_s + 1.8 * self.guard_interval_s
                + self.quiesce_s)

    def validate(self) -> None:
        k = self.k6()
        k.validate()
        if self.pre_s < 60 or self.post_s < 60:
            raise ShutError("--pre and --post: at least 60 s each")
        if self.delta_s < k.graceful_stop_s + 5:
            raise ShutError(f"--delta {self.delta_s}: at least the pre scenario's graceful stop + 5 s "
                            f"({k.graceful_stop_s + 5} s), so the pre requests have ended")
        if self.delta_s > self.post_s - 60:
            raise ShutError(f"--delta {self.delta_s}: at most --post - 60 s, so the new pod serves post traffic")
        if self.worker_interval_s < 1:
            raise ShutError("--worker-interval is required (10800 for K-shut, as K8b)")
        if self.tail_s > COPY_SAFE_S:
            raise ShutError(f"deletion to t2 is up to {self.tail_s:.0f} s, beyond {COPY_SAFE_S} s: the old "
                            "instance's copy series could expire before `verify.py soak shut` reads it")

    def as_json(self) -> dict:
        return {**asdict(self), "langs": list(self.langs), "k6_deadline_s": self.k6().deadline_s,
                "tail_s": self.tail_s}


# ── pods ───────────────────────────────────────────────────────────────────

def _state_times(st: dict) -> dict:
    run, term = st.get("running") or {}, st.get("terminated") or {}
    out = {"started_at": sp.parse_ts(run.get("startedAt") or term.get("startedAt"))}
    if term:
        out.update(finished_at=sp.parse_ts(term.get("finishedAt")), exit_code=term.get("exitCode"),
                   reason=term.get("reason"))
    return out


def pod_summary(obj: dict) -> dict | None:
    """The fields K-shut keeps of a demo pod (never its spec's env: the hooked
    pods carry the canary header). None for a pod that is not a demo app pod."""
    meta = obj.get("metadata") or {}
    m = POD_NAME_RE.fullmatch(meta.get("name", ""))
    if not m:
        return None
    status = obj.get("status") or {}
    containers = {}
    for c in status.get("containerStatuses") or []:
        rec = {"restart_count": int(c.get("restartCount") or 0), **_state_times(c.get("state") or {})}
        last = _state_times(c.get("lastState") or {})
        if last.get("finished_at") is not None:
            rec["last_terminated"] = last
        containers[c.get("name", "")] = rec
    grace = (obj.get("spec") or {}).get("terminationGracePeriodSeconds")
    return {"pod": meta["name"], "uid": meta.get("uid"), "lang": m.group(1), "app": m.group(2),
            "service": f"demo-{m.group(1)}-{m.group(2)}", "phase": status.get("phase"),
            "deletion": sp.parse_ts(meta.get("deletionTimestamp")), "grace_s": grace, "containers": containers}


def live(summary: dict) -> bool:
    return summary.get("phase") == "Running" and summary.get("deletion") is None


def demo_pods(pods: list[dict], langs) -> list[dict]:
    """Running, not terminating demo app pods of ``langs``."""
    out = []
    for p in pods:
        s = pod_summary(p)
        if s and s["lang"] in langs and live(s):
            out.append(s)
    return sorted(out, key=lambda s: s["pod"])


def deployment_grace(deps: list[dict]) -> dict[str, float | None]:
    """``app.kubernetes.io/name`` -> the pod template's terminationGracePeriodSeconds."""
    out = {}
    for dep in deps:
        name = ((dep.get("metadata") or {}).get("labels") or {}).get("app.kubernetes.io/name") or \
            (dep.get("metadata") or {}).get("name", "")
        g = ((((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("terminationGracePeriodSeconds"))
        out[name] = float(g) if g is not None else None
    return out


class PodWatch:
    """``kubectl get pods --watch --output-watch-events -o json`` of the demo pods,
    parsed in memory on a thread. Keeps, per pod, the latest state and the first
    terminated state seen per container (a later event never erases it), and
    whether a DELETED event came. Restarted when the API server ends the watch;
    each restart is a recorded gap."""

    def __init__(self, command: list[str], spawn: Callable[[list[str]], object], *,
                 clock: Callable[[], float] = time.time, restart_pause_s: float = 1.0, max_restarts: int = 100):
        self.command, self.spawn, self.clock = command, spawn, clock
        self.restart_pause_s, self.max_restarts = restart_pause_s, max_restarts
        self.pods: dict[str, dict] = {}
        self.events = 0
        self.restarts = 0
        self.gaps: list[float] = []
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc = None
        self._thread: threading.Thread | None = None

    # parsing (also used directly by the tests)
    def handle(self, event: dict) -> None:
        obj = event.get("object") if isinstance(event, dict) else None
        s = pod_summary(obj) if isinstance(obj, dict) else None
        if s is None:
            return
        with self._lock:
            self.events += 1
            cur = self.pods.setdefault(s["pod"], {"first_seen": self.clock(), "terminated": {}})
            cur.update({k: v for k, v in s.items() if k != "containers"})
            cur["containers"] = s["containers"]
            for name, c in s["containers"].items():
                if c.get("finished_at") is not None and name not in cur["terminated"]:
                    cur["terminated"][name] = {k: c.get(k) for k in ("started_at", "finished_at", "exit_code", "reason")}
            if event.get("type") == "DELETED":
                cur["deleted_seen"] = self.clock()

    def feed(self, buf: str) -> str:
        """Every complete JSON document of ``buf`` handled; returns the rest."""
        dec = json.JSONDecoder()
        while True:
            stripped = buf.lstrip()
            if not stripped:
                return ""
            try:
                doc, end = dec.raw_decode(stripped)
            except json.JSONDecodeError:
                return stripped
            self.handle(doc)
            buf = stripped[end:]

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                proc = self.spawn(self.command)
            except Exception as exc:  # noqa: BLE001 — recorded; the window goes on
                self.errors.append(f"watch start: {exc.__class__.__name__}: {exc}")
                return
            self._proc = proc
            if self._stop.is_set():   # stop() ran while this child was being spawned
                self._end(proc)
                return
            buf = ""
            for line in proc.stdout:
                buf = self.feed(buf + line)
                if self._stop.is_set():
                    break
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            if self._stop.is_set():
                return
            if self.restarts >= self.max_restarts:
                self.errors.append(f"watch ended {self.restarts + 1} times; not restarted")
                return
            self.restarts += 1
            self.gaps.append(self.clock())
            self._stop.wait(self.restart_pause_s)

    def start(self) -> "PodWatch":
        self._thread = threading.Thread(target=self._run, name="soak-pod-watch", daemon=True)
        self._thread.start()
        return self

    @staticmethod
    def _end(proc) -> None:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001 — already gone
            pass

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            self._end(self._proc)
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._proc is not None:   # a child spawned between the first terminate and the join
            self._end(self._proc)

    def terminated(self, pod: str, container: str) -> dict | None:
        with self._lock:
            return dict((self.pods.get(pod) or {}).get("terminated", {}).get(container) or {}) or None

    def snapshot(self) -> dict[str, dict]:
        """A copy of every pod's record (name -> latest state, terminated, deletion)."""
        with self._lock:
            return json.loads(json.dumps(self.pods, default=str))

    def record(self) -> dict:
        with self._lock:
            return {"events": self.events, "restarts": self.restarts, "gaps": list(self.gaps),
                    "errors": list(self.errors), "pods": len(self.pods)}

    def lines(self) -> str:
        with self._lock:
            return "".join(json.dumps({"pod": k, **v}, sort_keys=True, default=str) + "\n"
                           for k, v in sorted(self.pods.items()))


# ── derivations ────────────────────────────────────────────────────────────

def old_instance(series: list[dict], api: str, t1: float, t2: float) -> tuple[str | None, str]:
    """The api's ``service_instance_id`` with a fixed-count increase in (t1, t2]
    (the copy's series rules); None with the reason when there is none or
    there are several (design K-D22 draft, gap 10)."""
    c = soakcount.copy_counts([s for s in series if (s.get("metric") or {}).get("service_name") == api], t1, t2).get(api)
    if c is None:
        return None, "no copy series for the api in (t1, deletion]"
    grown = sorted(i for i, n in c.instances.items() if n > 0)
    if soakcount.UNSET in grown:
        return None, f"an {soakcount.UNSET} copy series grew (D4)"
    if len(grown) != 1:
        return None, (f"{len(grown)} service_instance_id values grew in (t1, deletion]: {grown}" if grown
                      else "no service_instance_id grew in (t1, deletion]")
    return grown[0], "copy otel_libs_soak_fixed_spans_total increase in (t1, deletion]"


def post_issued_bound(rate: int, post_start: float | None, finished_at: float | None,
                      post_total: int | None) -> tuple[int | None, str]:
    """Post requests issued before ``finishedAt``, an upper bound: ceil(rate x
    (finishedAt + 1 s - post_start)) + 1, at most the language's post requests.
    ``post_start`` is a lower bound on the post phase's real start, on the
    cluster clock (``Shutter.post_start_lo``), so the estimate errs high. None
    without the times: then every post answer bounds it (``shut_verdict``)."""
    if post_start is None or finished_at is None:
        return None, "the post start or the old container's finishedAt unknown: every post answer bounds it"
    span = finished_at + FINISH_RESOLUTION_S - post_start
    n = 0 if span <= 0 else math.ceil(rate * span) + 1
    if post_total is not None:
        n = min(n, post_total)
    return n, (f"rate {rate}/s x {max(span, 0):.1f} s after the post start (+1)"
               + (f", capped at the {post_total} post requests" if post_total is not None else ""))


def post_totals(cells: dict[str, dict[str, int]] | None) -> dict[str, int]:
    """Per language: every post request (answered or not)."""
    out: dict[str, int] = {}
    for cell, n in (cells or {}).items():
        tags = soakcount.cell_tags(cell)
        if tags.get("phase") == "post":
            out[tags["lang"]] = out.get(tags["lang"], 0) + n["expected"] + n["unexpected"] + n["noanswer"]
    return out


# ── k6 times, deleted-pod logs, the edge exemption ────────────────────────

_TS_RE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z ")


def line_time(line: str) -> float | None:
    """The leading RFC 3339 timestamp of a ``kubectl logs --timestamps`` line
    (nanoseconds cut to microseconds)."""
    m = _TS_RE.match(line)
    if not m:
        return None
    frac = (m.group(2) or "")[:6]
    t = sp.parse_ts(m.group(1) + "Z")
    return None if t is None else t + (float("0." + frac) if frac else 0.0)


def phase_times(text: str) -> dict[str, float | None]:
    """The first reservoir line's timestamp per phase (cluster clock). k6 logs a
    reservoir line after the response, so it is the phase start + one request
    latency (<= the request timeout)."""
    out: dict[str, float | None] = {"pre": None, "post": None}
    for line in text.splitlines():
        t = line_time(line)
        if t is None:
            continue
        rest = line[_TS_RE.match(line).end():]
        if not rest.startswith(k6script.RESERVOIR_PREFIX):
            continue
        try:
            ph = json.loads(rest[len(k6script.RESERVOIR_PREFIX):]).get("phase")
        except json.JSONDecodeError:
            continue
        if ph in out and out[ph] is None:
            out[ph] = t
    return out


def increments(values: list, start: float) -> list[tuple[float, float]]:
    """(sample time, step) of every increase of one counter series over
    [start, ...], by the rules of ``soakplatform.raw_increase`` (a lower value is
    a reset and adds its value; a series born in the range adds its first)."""
    pts = [(float(t), float(v)) for t, v in values]
    before = [p for p in pts if p[0] < start][-1:]
    pts = before + [p for p in pts if p[0] >= start]
    out = []
    if pts and not before and pts[0][1] > 0:
        out.append(pts[0])
    for (_, prev), (t, v) in zip(pts, pts[1:]):
        step = v - prev if v >= prev else v
        if step > 0:
            out.append((t, step))
    return out


def merge_logs(snapshot: list[str], follow: list[str]) -> list[str]:
    """The snapshot, then the follow's lines after the snapshot's last timestamp
    (a line at that same timestamp is kept unless the snapshot has it)."""
    last = max((t for t in map(line_time, snapshot) if t is not None), default=None)
    if last is None:
        return list(snapshot) + list(follow)
    have = set(snapshot)
    tail = []
    for ln in follow:
        t = line_time(ln)
        if t is None or t > last or (t == last and ln not in have):
            tail.append(ln)
    return list(snapshot) + tail


class LogFollow:
    """``kubectl logs -f --timestamps`` of a deleted api pod's container, from
    just before its deletion until the container exits, bounded at grace + 15 s
    (design K-D22, review round 1 HIGH-1). Then, when the pod is still there
    (Terminating), one non-follow read as a cross-check of the line count."""

    def __init__(self, command: list[str], spawn: Callable[[list[str]], object], bound_s: float,
                 read_once: Callable[[], tuple[int, str]]):
        self.command, self.spawn, self.bound_s, self.read_once = command, spawn, bound_s, read_once
        self.lines: list[str] = []
        self.error: str | None = None
        self.bounded = False
        self.rc: int | None = None
        self.cross: dict | None = None
        self._thread: threading.Thread | None = None
        self.running = threading.Event()   # the first line came, or the child ended

    def wait_started(self, timeout: float) -> bool:
        """Until the follower has emitted its first line (or ended), at most ``timeout``."""
        return self.running.wait(timeout)

    def _kill(self, proc) -> None:
        self.bounded = True
        PodWatch._end(proc)

    def _run(self) -> None:
        try:
            proc = self.spawn(self.command)
        except Exception as exc:  # noqa: BLE001 — recorded: the source is UNMEASURED
            self.error = f"kubectl logs -f: {exc.__class__.__name__}: {exc}"
            self.running.set()
            return
        timer = threading.Timer(self.bound_s, self._kill, (proc,))
        timer.daemon = True
        timer.start()
        try:
            for line in proc.stdout:
                self.lines.append(line)
                self.running.set()
            self.running.set()
            try:
                self.rc = proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.rc = None
        finally:
            timer.cancel()
        if self.rc not in (0, None) and not self.bounded and not self.lines:
            self.error = f"kubectl logs -f exit {self.rc}"
        try:
            rc, text = self.read_once()
            self.cross = {"lines": len(text.splitlines())} if rc == 0 else {"error": "pod gone (not read)"}
        except Exception as exc:  # noqa: BLE001
            self.cross = {"error": f"{exc.__class__.__name__}"}

    def start(self) -> "LogFollow":
        self._thread = threading.Thread(target=self._run, name="soak-log-follow", daemon=True)
        self._thread.start()
        return self

    def join(self, timeout: float) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return False


EDGE_NAME = "guard (b) edge demo->soak-agent"
EXEMPT_CAP = 3                  # increments of the failed-connection counter, over the whole window
REPLACEMENT_S = 30              # the replacement pod's first seconds after startedAt
FOLLOW_START_S = 2              # wait for the follower's first line before the delete


class EdgeExempt:
    """The one guard exemption of the shut path (review round 1 HIGH-2): on the
    demo -> soak-agent edge only, increases of ``istio_tcp_connections_failed_total``
    whose ``source_workload`` is the api of a language whose pod was deleted, with
    the sample in [deleted_at, finishedAt + one ztunnel scrape] or in the
    replacement pod's first 30 s after startedAt + one scrape (the counter's
    sample time is its scrape time), at most 3 increments in all, each recorded
    with its ``response_flags``. Every other edge (soak-agent -> otel-agent-
    collector included) stays strict. Never used by ``run`` or ``cal``."""

    def __init__(self, sh: "Shutter", cap: int = EXEMPT_CAP):
        self.sh, self.cap = sh, cap
        self.used = 0.0
        self.seen: set[tuple] = set()
        self.records: list[dict] = []
        self.flags_seen: set[str] = set()

    def windows(self) -> dict[str, list[tuple[float, float, str, bool]]]:
        """source_workload -> [(from, to, why, CONNECT failures?)] on the cluster clock."""
        sh, out = self.sh, {}
        skew = sh.skew or 0.0
        for lang, rec in sh.deletions.items():
            if rec.get("deleted_at") is None:
                continue
            api = f"demo-{lang}-api"
            pod = sh.api[lang]
            fin = (sh._terminated(pod) or {}).get("finished_at")
            grace = float(pod.get("grace_s") or 30)
            lo = rec["deleted_at"] + skew - FINISH_RESOLUTION_S
            hi = (fin if fin is not None else rec["deleted_at"] + skew + grace) + sp.SCRAPE_S + FINISH_RESOLUTION_S
            out.setdefault(api, []).append((lo, hi, "deletion to finishedAt + one scrape", False))
            for st in sh.replacement_starts(lang):
                out[api].append((st, st + REPLACEMENT_S + sp.SCRAPE_S + FINISH_RESOLUTION_S,
                                 "replacement pod's first 30 s + one scrape", True))
        return out

    EDGE_ID = "edge demo->soak-agent"
    CONNECT = "CONNECT"     # ztunnel's response_flags for a failed connect

    def filter(self, read, reader, at: float, lookback: float):
        """``read`` with the demo -> soak-agent breach turned into a finding only
        when (review round 2) the guard's own verdict for that edge over the
        guard's own range (``guard.window_lookback``) is NOISY, every increment
        in that range is exempt, and the exempted steps equal the guard's
        counted increase. UNKNOWN (a scrape gap) and ABSENT stay breaches. In
        the deletion window only non-CONNECT failures are exempt (the dying
        pod's open connections); in the replacement window only CONNECT ones
        (the new pod connecting before it is in the mesh). New exemptions are
        committed (and counted against the cap) only when the read is
        downgraded."""
        items = [i for i in read.items if i.name == EDGE_NAME and i.state == "breach"]
        if not items:
            return read
        item = items[0]
        start = at - lookback
        q = (f"{sp.sel('istio_tcp_connections_failed_total', {'job': sp.ZTUNNEL_JOB, 'reporter': 'source', **sp.TELEMETRY_EDGES[0][1]})}"
             f"[{sp.lookback_range(at, start)}s]")
        try:
            verdicts, _ = sp.read_edges(reader, at, (start,))
            v = next(x for x in verdicts[0] if x.id == self.EDGE_ID)
            series = sp.cluster_only(reader.prom(q, at))
        except Exception as exc:  # noqa: BLE001 — not read: the breach stays
            item.detail += f"; shut exemption not read ({exc.__class__.__name__})"
            return read
        if v.verdict != "NOISY":
            item.detail += f"; shut exemption: guard verdict {v.verdict}, not exemptible"
            return read
        wins = self.windows()
        strict = exempt = 0.0
        new: list[tuple[tuple, dict]] = []
        used = self.used
        for s in series:
            m = s.get("metric") or {}
            flags = m.get("response_flags", "")
            for t, step in increments(s.get("values") or [], start):
                self.flags_seen.add(flags)
                key = (tuple(sorted(m.items())), t)
                if key in self.seen:
                    exempt += step
                    continue
                why = next((w for lo, hi, w, connect in wins.get(m.get("source_workload", ""), [])
                            if lo <= t <= hi and (flags == self.CONNECT) == connect), None)
                if why is None or used + step > self.cap:
                    strict += step
                    continue
                used += step
                exempt += step
                new.append((key, {"at": t, "at_iso": sp.iso(t), "step": step,
                                  "source_workload": m.get("source_workload"),
                                  "destination_workload": m.get("destination_workload"),
                                  "response_flags": flags, "window": why, "read_at": at}))
        if strict == 0 and exempt > 0 and abs(exempt - float(v.increase)) < 1e-9:
            self.used = used
            for key, rec in new:
                self.seen.add(key)
                self.records.append(rec)
            item.state = "finding"
            item.detail = (f"shut exemption: {exempt:g} of the guard's {v.increase:g} exempt, {len(self.records)} "
                           f"increment(s) of {self.used:g} so far (cap {self.cap}); was: {item.detail}")
        else:
            item.detail += (f"; shut exemption: exempt {exempt:g}, outside {strict:g}, guard counted "
                            f"{v.increase:g}: not downgraded")
        return read

    def for_api(self, api: str) -> list[dict]:
        return [r for r in self.records if r.get("source_workload") == api]


# ── the driver ─────────────────────────────────────────────────────────────

class Shutter:
    """K-shut's state across the window: the pods of the nonce roll, the api
    pods to delete, the tick, and the inputs written at the end."""

    def __init__(self, ops, plan: ShutPlan, d: Path, *, kcal: Path, watch: PodWatch | None = None,
                 log: Callable[[str], None] = print, interval_s: float = 60.0,
                 log_spawn: Callable[[list[str]], object] | None = None):
        self.ops, self.plan, self.d, self.kcal, self.watch, self.log = ops, plan, d, Path(kcal), watch, log
        self.interval_s, self.log_spawn = interval_s, log_spawn
        self.job = render.k6_name(WINDOW)
        self.t1: float | None = None
        self.apply_at: tuple[float, float] | None = None
        self.k6_started: float | None = None
        self.skew: float | None = None          # cluster clock - laptop clock (s)
        self.skew_error_s: float | None = None
        self.planned: float | None = None
        self.phase: dict = {}
        self.post_start_lo: float | None = None
        self.done = False
        self.hook_at: float | None = None
        self.nonce_old: list[dict] = []
        self.nonce_new: list[dict] = []
        self.api: dict[str, dict] = {}
        self.grace_dep: dict[str, float | None] = {}
        self.promoted: dict = {"list": None, "source": "not read"}
        self.deletions: dict[str, dict] = {}
        self.follows: dict[str, LogFollow] = {}
        self.snapshots: dict[str, dict] = {}
        self.deleted_logs: dict[str, dict] = {}
        self.problems: list[str] = []
        self.exempt = EdgeExempt(self)

    def before_hook(self) -> None:
        """The nonce roll's old pods and the promoted list (reads)."""
        self.nonce_old = demo_pods(self.ops.pods(), self.plan.langs)
        self.promoted = self.ops.promoted()

    def after_hook(self, hook_at: float) -> None:
        """The nonce roll's new pods and the api pods to delete. Refused when a
        language has not exactly one api pod running."""
        self.hook_at = hook_at
        self.nonce_new = demo_pods(self.ops.pods(), self.plan.langs)
        self.grace_dep = deployment_grace(self.ops.deployments())
        bad = []
        for lang in self.plan.langs:
            pods = [p for p in self.nonce_new if p["service"] == f"demo-{lang}-api"]
            if len(pods) != 1:
                bad.append(f"{lang}: {len(pods)} api pods running, not 1")
            else:
                self.api[lang] = pods[0]
        if bad:
            raise ShutError("api pods after the hook: " + "; ".join(bad))

    def start_window(self, t1: float, applied: tuple[float, float] | None = None) -> None:
        """t1, and the laptop times just before and after the k6 apply (the skew)."""
        self.t1, self.apply_at = t1, applied

    @property
    def skew_limit_s(self) -> float:
        return self.plan.delta_s - self.plan.k6().graceful_stop_s - 5

    def _measure_skew(self) -> None:
        """skew = the Job's creationTimestamp (cluster) - the apply's midpoint
        (laptop); error = half the apply's duration + 1 s (whole seconds)."""
        created = None
        try:
            created = self.ops.k6_job_created(self.job)
        except Exception as exc:  # noqa: BLE001
            self.log(f"shut: k6 Job creationTimestamp unreadable ({exc.__class__.__name__})")
        if created is None or self.apply_at is None:
            self.problems.append("clock skew unmeasured (no Job creationTimestamp or apply time)")
            return
        a, b = self.apply_at
        self.skew = round(created - (a + b) / 2, 3)
        self.skew_error_s = round((b - a) / 2 + FINISH_RESOLUTION_S, 3)
        if abs(self.skew) > self.skew_limit_s:
            self.problems.append(f"clock skew {self.skew:+.1f} s beyond +/-{self.skew_limit_s:g} s "
                                 "(delta - graceful stop - 5)")

    def tick(self, now: float) -> float | None:
        """Called by the loop between guard reads; returns its next wake time."""
        if self.done or self.t1 is None:
            return None
        if self.k6_started is None:
            try:
                self.k6_started = self.ops.k6_started(self.job)
            except Exception as exc:  # noqa: BLE001 — the next tick reads again
                self.log(f"shut: k6 start unreadable ({exc.__class__.__name__}: {exc}); retrying")
            if self.k6_started is None:
                return now + K6_POLL_S
            self._measure_skew()
            # The k6 container's startedAt is on the cluster clock; the loop on the laptop's.
            self.planned = self.k6_started - (self.skew or 0.0) + self.plan.pre_s + self.plan.delta_s
            self.log(f"shut: k6 started {sp.iso(self.k6_started)} (cluster), skew {self.skew}; api pods deleted at "
                     f"{sp.iso(self.planned)} (laptop)")
        if now < self.planned:
            return self.planned
        self.delete_all(now)
        return None

    def _phase_check(self, now: float) -> None:
        """The real post start from the k6 log's timestamps (review round 1 M4)."""
        text = None
        try:
            text = self.ops.k6_log_timestamps(self.job)
        except Exception as exc:  # noqa: BLE001
            self.log(f"shut: k6 log unreadable ({exc.__class__.__name__})")
        times = phase_times(text) if text else {"pre": None, "post": None}
        timeout = self.plan.timeout_s
        lows = [self.k6_started + self.plan.pre_s]
        if times["post"] is not None:
            lows.append(times["post"] - timeout)
        if times["pre"] is not None:
            lows.append(times["pre"] - timeout + self.plan.pre_s)
        self.post_start_lo = max(lows)
        deletion = now + (self.skew or 0.0)
        self.phase = {"first_pre_line": times["pre"], "first_post_line": times["post"],
                      "post_start_lo": self.post_start_lo, "deletion_cluster": round(deletion, 3),
                      "source": "kubectl logs --timestamps of the k6 Job (first SOAKRES line per phase)"}
        if times["post"] is None:
            self.problems.append("no phase=post line in the k6 log before the deletion: the delete may have "
                                 "landed before the post phase (k6 init longer than delta?)")
        elif times["post"] > deletion:
            self.problems.append(f"the delete landed {times['post'] - deletion:.1f} s before the first post "
                                 "answer: before the post phase")
        else:
            self.phase["deleted_after_post_s"] = round(deletion - self.post_start_lo, 3)
            self.phase["delta_difference_s"] = round(deletion - self.post_start_lo - self.plan.delta_s, 3)

    def delete_all(self, now: float) -> None:
        self.done = True
        self._phase_check(now)
        try:
            series = self.ops.copy_series(self.t1, now)
            read_error = None
        except Exception as exc:  # noqa: BLE001 — the deletion still runs; the old ids are unknown
            series, read_error = [], f"copy read failed: {exc.__class__.__name__}: {exc}"
        since = sp.iso((self.hook_at or self.t1) - 60)
        self.log_since = since
        if self.log_spawn is not None:
            # Review round 2 M2: a snapshot first, then the follower, which must be
            # running before the delete; a fast exit cannot beat both.
            for lang in self.plan.langs:
                pod = self.api[lang]
                try:
                    rc, text = self.ops.read_logs(pod["pod"], pod["app"], since, timestamps=True)
                    self.snapshots[lang] = {"lines": text.splitlines(keepends=True)} if rc == 0 else \
                        {"error": f"snapshot: kubectl logs exit {rc}"}
                except Exception as exc:  # noqa: BLE001
                    self.snapshots[lang] = {"error": f"snapshot: {exc.__class__.__name__}"}
                bound = float(pod.get("grace_s") or 30) + 15
                self.follows[lang] = LogFollow(self.ops.follow_command(pod["pod"], pod["app"], since),
                                               self.log_spawn, bound,
                                               lambda p=pod: self.ops.read_logs(p["pod"], p["app"], since)).start()
            for lang, f in self.follows.items():
                self.snapshots[lang]["follow_running_before_delete"] = f.wait_started(FOLLOW_START_S)
        for lang in self.plan.langs:
            pod = self.api[lang]
            api = f"demo-{lang}-api"
            iid, source = (None, read_error) if read_error else old_instance(series, api, self.t1, now)
            rec = {"pod": pod["pod"], "uid": pod.get("uid"), "old_instance": iid, "old_instance_source": source,
                   "planned_at": self.planned, "grace_s": pod.get("grace_s"),
                   "grace_deployment_s": self.grace_dep.get(api)}
            rec["deleted_at"] = self.ops.now()
            try:
                self.ops.delete(lang, pod["pod"])
                rec["delete_returned_at"] = self.ops.now()
            except Exception as exc:  # noqa: BLE001 — recorded; that api is not judged
                rec["deleted_at"] = None
                rec["error"] = f"{exc.__class__.__name__}: {exc}"
            if rec["deleted_at"] is not None and self.planned is not None:
                rec["delete_delay_s"] = round(rec["deleted_at"] - self.planned, 3)
                if rec["delete_delay_s"] > self.interval_s:
                    self.problems.append(f"{api}: deleted {rec['delete_delay_s']:.0f} s late (> one loop interval)")
                if self.post_start_lo is not None and \
                        rec["deleted_at"] + (self.skew or 0.0) - self.post_start_lo > self.plan.post_s - 60:
                    self.problems.append(f"{api}: deleted later than post - 60 s after the post start")
            self.deletions[lang] = rec
            self.log(f"shut: {pod['pod']} " + (f"deleted at {sp.iso(rec['deleted_at'])}" if rec["deleted_at"]
                                                else f"NOT deleted ({rec['error']})"))

    def replacement_starts(self, lang: str) -> list[float]:
        """startedAt of the api pods the watch saw after the deletion (not the deleted one)."""
        if self.watch is None or lang not in self.api:
            return []
        out = []
        rec = self.deletions.get(lang) or {}
        for name, p in self.watch.snapshot().items():
            if p.get("service") == f"demo-{lang}-api" and name != self.api[lang]["pod"]:
                st = (p.get("containers", {}).get("api") or {}).get("started_at")
                if st is not None and rec.get("deleted_at") and st >= rec["deleted_at"] + (self.skew or 0) - 5:
                    out.append(st)
        return out

    def save_deleted_logs(self, canary: str, writer: Callable[[Path, str], None]) -> dict:
        """Each followed log through trap step 1b's path: a log with a canary line
        is replaced by a marker, every file 0600, counts only in the record."""
        canary = canary.lower()
        for lang, f in sorted(self.follows.items()):
            pod = self.api[lang]
            key = f"{pod['pod']}/{pod['app']}"
            done = f.join(f.bound_s + 20)
            snap = self.snapshots.get(lang) or {"error": "no snapshot"}
            follow_error = f.error or (None if done else "follower still running after its bound")
            if follow_error and snap.get("error"):
                self.deleted_logs[key] = {"error": f"{follow_error}; {snap['error']}"}
                continue
            lines = merge_logs(snap.get("lines") or [], [] if follow_error else f.lines)
            text = "".join(lines)
            hits = sum(1 for ln in lines if canary and canary in ln.lower()) if canary else None
            path = self.d / "raw" / f"{pod['pod']}.{pod['app']}.deleted.log"
            marker = bool(hits)
            writer(path, json.dumps({"not_written": "canary", "canary_lines": hits}) + "\n" if marker else text)
            rec = {"path": path.name, "lines": len(lines), "bytes": len(text.encode()), "canary_lines": hits,
                   "snapshot_lines": len(snap.get("lines") or []), "follow_lines": len(f.lines),
                   "snapshot_error": snap.get("error"), "follow_error": follow_error,
                   "follow_running_before_delete": snap.get("follow_running_before_delete"),
                   "marker": marker, "bounded": f.bounded, "since": getattr(self, "log_since", None), "cross_check": f.cross,
                   "secret_patterns": soakcount.verify.scan_secret(text)}
            if follow_error:
                rec["finding"] = f"follow failed ({follow_error}): the snapshot before the delete only"
            elif f.cross and f.cross.get("lines") is not None and f.cross["lines"] > len(lines):
                rec["finding"] = f"the non-follow read has {f.cross['lines']} lines, the saved log {len(lines)}"
            self.deleted_logs[key] = rec
        return self.deleted_logs

    # ── the inputs ──
    def _terminated(self, pod: dict) -> dict:
        t = self.watch.terminated(pod["pod"], pod["app"]) if self.watch is not None else None
        return t or {}

    def _deletion(self, pod: dict) -> float | None:
        if self.watch is None:
            return None
        return (self.watch.snapshot().get(pod["pod"]) or {}).get("deletion")

    def shut_input(self, t1: float, t2: float, cells: dict | None, new_api: dict[str, list[dict]],
                   reasons: list[str] | None = None) -> dict:
        totals = post_totals(cells)
        apis = {}
        for lang in self.plan.langs:
            api = f"demo-{lang}-api"
            rec = dict(self.deletions.get(lang) or {"pod": (self.api.get(lang) or {}).get("pod"),
                                                    "error": "not deleted: the window ended before the deletion"})
            term = self._terminated(self.api[lang]) if lang in self.api else {}
            rec.update(finished_at=term.get("finished_at"), exit_code=term.get("exit_code"),
                       exit_reason=term.get("reason"))
            rec["finished_at_iso"] = sp.iso(rec["finished_at"])
            rec["post_before_finish"], rec["post_basis"] = post_issued_bound(
                self.plan.rate, self.post_start_lo, rec["finished_at"], totals.get(lang))
            rec["new_pods"] = [p["pod"] for p in new_api.get(lang, [])]
            rec["edge_exemptions"] = self.exempt.for_api(api)
            findings = []
            code = rec.get("exit_code")
            if code not in (None, 0, soakcount.SIGKILL_EXIT):
                findings.append(f"exit code {code} (not 0; a finding next to the count verdict, not a FAIL: gap 9)")
            if rec.get("deleted_at") is not None and rec["finished_at"] is None:
                findings.append("finishedAt not seen by the pod watch: exit within grace UNQUALIFIED")
            rec["findings"] = findings
            if rec.get("error"):
                rec["unqualified"] = rec["error"]
            elif rec.get("old_instance") is None:
                rec["unqualified"] = f"old service_instance_id unknown ({rec.get('old_instance_source')})"
            rec["old_instance"] = rec.get("old_instance") or ""
            if rec.get("deleted_at") is not None and self.skew is not None:
                # exit_verdict compares with finishedAt (cluster clock): deleted_at on that clock.
                rec["deleted_at_laptop"] = rec["deleted_at"]
                rec["deleted_at"] = round(rec["deleted_at"] + self.skew, 3)
            apis[api] = rec
        return {"window": WINDOW, "t1": t1, "t2": t2, "t1_iso": sp.iso(t1), "t2_iso": sp.iso(t2),
                "k6_log": f"raw/{self.job}.log", "kcal": os.path.relpath(self.kcal.resolve(), self.d.resolve()),
                "k6_started_at": self.k6_started, "planned_delete_at": self.planned, "plan": self.plan.as_json(),
                "clock": {"skew_s": self.skew, "skew_error_s": self.skew_error_s, "skew_limit_s": self.skew_limit_s,
                          "note": "skew = cluster - laptop; deleted_at is on the cluster clock (laptop + skew)"},
                "phase": self.phase, "apis": apis, "valid": not reasons, "reasons": list(reasons or []),
                "note": "paths relative to this file's directory; valid: false is refused by verify.py soak shut"}

    def identity_input(self, t1: float, t2: float, new_api: dict[str, list[dict]]) -> dict:
        def with_exit(pods: list[dict]) -> list[dict]:
            out = []
            for p in pods:
                term = self._terminated(p)
                out.append({"pod": p["pod"], "uid": p.get("uid"), "started_at": (p["containers"].get(p["app"]) or {}).get(
                    "started_at"), "finished_at": term.get("finished_at"), "exit_code": term.get("exit_code"),
                    "deletion": self._deletion(p)})
                if out[-1]["deletion"] is None:   # the K-shut pod: deleted_at on the cluster clock
                    rec = next((r for lang, r in self.deletions.items() if self.api.get(lang, {}).get("pod") == p["pod"]),
                               None)
                    if rec and rec.get("deleted_at") is not None and self.skew is not None:
                        out[-1]["deletion"] = round(rec["deleted_at"] + self.skew, 3)
                        out[-1]["deletion_source"] = "deleted_at + skew (no deletionTimestamp from the watch)"
            return out

        nonce = {}
        for p in self.nonce_old + self.nonce_new:
            nonce.setdefault(p["service"], {"old": [], "new": []})
        for p in self.nonce_old:
            nonce[p["service"]]["old"].append(p)
        for p in self.nonce_new:
            nonce[p["service"]]["new"].append(p)
        shut = {f"demo-{lang}-api": {"old": [self.api[lang]], "new": new_api.get(lang, [])}
                for lang in self.plan.langs if lang in self.deletions and self.deletions[lang].get("deleted_at")}
        deleted = [r["deleted_at"] + (self.skew or 0.0) for r in self.deletions.values() if r.get("deleted_at")]
        return {"run_window": WINDOW, "langs": list(self.plan.langs), "hook_at": self.hook_at, "t1": t1, "t2": t2,
                "export_interval_s": soakcount.EXPORT_INTERVAL_S, "finish_resolution_s": FINISH_RESOLUTION_S,
                "promoted": self.promoted, "skew_s": self.skew,
                "rolls": {
                    "nonce": {"at": self.hook_at, "judged": True,
                              "what": "the hook's apply (new DEMO_RESTART_NONCE); it also moves the export from the "
                                      "shared agent (old pods) to the copy (new pods), so it is not a pure restart",
                              "services": {s: {"old": with_exit(v["old"]), "new": with_exit(v["new"])}
                                           for s, v in sorted(nonce.items())}},
                    "shut": {"at": min(deleted) if deleted else None, "judged": False,
                             "what": "K-shut: each api pod deleted by name (report-only for K3)",
                             "services": {s: {"old": with_exit(v["old"]), "new": with_exit(v["new"])}
                                          for s, v in sorted(shut.items())}}},
                "watch": self.watch.record() if self.watch is not None else None}

    def new_api_pods(self) -> dict[str, list[dict]]:
        """The replacement api pods (any api pod not the deleted one)."""
        pods = demo_pods(self.ops.pods(), self.plan.langs)
        return {lang: [p for p in pods if p["service"] == f"demo-{lang}-api" and p["pod"] != self.api[lang]["pod"]]
                for lang in self.plan.langs if lang in self.api}


def commands(run: str, inputs_valid: bool) -> list[str]:
    """The read-only verify commands for the written inputs."""
    if not inputs_valid:
        return []
    base = f"python3 -I testing/k8s/verify.py soak {{}} --run {run} --input testing/k8s/soak/runs/{run}/{{}}"
    return [base.format("shut", "shut-input.json"), base.format("identity", "identity-input.json"),
            base.format("labels", "identity-input.json")]
