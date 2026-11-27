"""The K-cal calibration driver (specs/soak-simulation task K-cal-d, design
K-D20; SR15 "Counting in bounds (r6.2)", "Windows and drain (r6.2)", "Load
(r6.2)", "Orchestration (r6.2)").

``soak.py cal`` runs it inside the same pre-flight, entry gate, hook, trap and
soak-run labels as ``soak.py run``; this module holds the plan, the pure
derivations and the phase logic, and talks to the cluster only through an
``ops`` object (``soak.CalOps`` on the cluster, a fake in the tests).

Phase A (k values). Per cell of the K8b plan (``k6script.K6Plan``'s default
scenario mix and inbound classes), one k6 run of N requests per language at a
low rate, twice, each followed by a quiesce; one quiesce before the first. The
four languages run in the same k6 run: their services are disjoint
(``demo-<lang>-{worker,api,backend}``, each api calling its own backend,
``testing/k8s/demo/demo.sh`` DEMO_API_URL / DEMO_BACKEND_ADDR), and both count
sources carry ``service_name``. The cells of one language cannot share a run:
neither source carries the cell. A repeat's delta is the copy's
``otel_libs_soak_fixed_spans_total`` per service over (previous quiesced
boundary, this one], read with K-count's series rules (``copy_counts``), and
the gateway's ``traces_span_metrics_calls_total`` per fixed family as the
cross-check (``gateway_families``). The repeats go to ``kcal-input.json`` in
the shape ``soakcount.calibrate`` takes; a repeat whose window or quiesce was
invalid, whose copy gate was not clean, or whose copy count was ambiguous
goes to its ``invalid`` list instead (so the cell has fewer than two repeats
and ``calibrate`` excludes it, by name). Then ``soak-cal.json``: calibrate's
output (what ``verify.py soak count --kcal`` reads) plus a ``driver`` section.

Worker timing: each non-Go worker's first ``demo.drive`` is read as the first
step of the copy's fixed count of its api or worker after the hook (an upper
bound: the drive precedes the step by up to one export interval and one copy
scrape); Go's first drive is at start + ``DEMO_INTERVAL_SECONDS``, outside the
calibration, so it is ``unmeasured`` with that reason. The worker interval of
the hook keeps every next drive after the calibration's end; a drive that
would fall inside it stops the driver.

Phase B (rate steps). One k6 run per step (default mix and classes), each
followed by a quiesce; a step is clean when its window and quiesce are valid
(every guard and sentinel, read as a loaded window) and the k6 summary has
``dropped_iterations`` = 0 (k6 omits the metric when it drops none). The
steps stop at the first unclean one. Loaded rate = the highest clean step
minus one step. Drain: per language the PRODUCER ``send demo:<lang>:orders``
and CONSUMER ``process demo:<lang>:orders`` gateway counts per step; while a
backlog is left at a boundary the worker was saturated and its drain rate is
the consumed count over the step's span; a worker that kept up at every step
has no measured drain rate (``unmeasured``, with the lower bound it reached).
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import k6script
import render
import soakcount

sp = soakcount.sp
verify = soakcount.verify

REPEATS = 2
DEFAULT_N = 30
DEFAULT_RATE = 2
DEFAULT_STEPS = (2, 5, 10, 20)
DEFAULT_STEP_S = 300
# The hook's worker interval during the calibration: 6 h, so no worker drive
# (Go: start + interval; the others: first drive + interval) lands inside it.
CAL_WORKER_INTERVAL_S = 21600
DRAIN_WAIT_S = 900
GUARD_INTERVAL_S = 60
GAP_FACTOR = 3
# k6 pod start with a cached image plus the summary write (initial, `unmeasured`).
K6_OVERHEAD_S = 30
# The (g) and (h) waits after the hook (guard.GuardConfig ready_s + margin, copy_count_s).
HOOK_WAIT_S = (600, 1260)
# The K8b plan's cells (k6script defaults); the tests may narrow these.
CAL_MIX = dict(k6script.DEFAULT_MIX)
CAL_CLASSES = dict(k6script.DEFAULT_CLASSES)
GO_FIRST_DRIVE = ("unmeasured: Go drives first at start + DEMO_INTERVAL_SECONDS "
                  "(go/demo/worker/main.go:97, :108-121), after the calibration")
DRIVE_SOURCE = ("first step of otel_libs_soak_fixed_spans_total for the api or the worker after the hook "
                "(upper bound: the drive precedes it by up to one export interval + one copy scrape)")
LOADED_RULE = "the highest clean step minus one step (SR15 Load / K-cal)"
# A step saturated the worker only when send - process (upper) exceeds this
# (code review M1: one late CONSUMER span or a lower/upper ambiguity is not a backlog).
SATURATION_MIN_MSGS = 10
SATURATION_SHARE = 0.05
K6_PEAK_RSS = ("unmeasured: the k6 summary carries no process-memory metric "
               "(testing/k8s/soak/fixtures/k6-2.3.0-run.log); the limit stays 256 MiB + 4 MiB per VU")
NO_SIBLING = "absent copy series, no sibling success"


class CalError(k6script.PlanError):
    """The calibration plan is not runnable."""


@dataclass(frozen=True)
class CalPlan:
    langs: tuple[str, ...]
    n: int = DEFAULT_N
    rate: int = DEFAULT_RATE
    steps: tuple[int, ...] = DEFAULT_STEPS
    step_s: int = DEFAULT_STEP_S
    quiesce_s: float = soakcount.QUIESCE_S
    worker_interval_s: int = CAL_WORKER_INTERVAL_S
    drain_wait_s: int = DRAIN_WAIT_S
    timeout_s: int = 10
    p99_s: float = 1.0
    interval_s: int = GUARD_INTERVAL_S
    mix: dict[str, int] = field(default_factory=lambda: dict(CAL_MIX))
    classes: dict[str, int] = field(default_factory=lambda: dict(CAL_CLASSES))

    def validate(self) -> None:
        if self.rate < 1 or self.n < 2 * self.rate or self.n % self.rate:
            raise CalError(f"--n {self.n} / --rate {self.rate}: N a multiple of the rate, at least 2 s of load")
        if len(self.steps) < 2 or any(s < 1 for s in self.steps) or list(self.steps) != sorted(set(self.steps)):
            raise CalError(f"--steps {self.steps}: at least two rising rates >= 1 (loaded = highest clean minus one)")
        if self.step_s < self.interval_s:
            raise CalError(f"--step-duration {self.step_s}: at least one guard interval ({self.interval_s} s)")
        if self.quiesce_s <= 0 or self.drain_wait_s < 0:
            raise CalError("--quiesce > 0 and --drain-wait >= 0")
        if not self.cells():
            raise CalError("the mix and classes leave no cell")
        end = self.estimate()["worst_s"]
        if self.worker_interval_s <= end + self.quiesce_s:
            raise CalError(f"--worker-interval {self.worker_interval_s}: must exceed the worst-case "
                           f"wall clock {end} s + the quiesce (a worker drive would land inside)")
        self.cell_plan(*self.cells()[0][1:]).validate()
        self.step_plan(self.steps[-1]).validate()

    def cells(self) -> list[tuple[str, str, str]]:
        """(index, scenario, class) of every cell of the plan with a non-zero share."""
        pairs = [(s, c) for s, sv in self.mix.items() if sv for c, cv in self.classes.items() if cv]
        return [(f"a{i:02d}", s, c) for i, (s, c) in enumerate(pairs)]

    def cell_plan(self, scen: str, cls: str) -> k6script.K6Plan:
        return k6script.K6Plan(langs=self.langs, rate=self.rate, duration_s=self.n // self.rate,
                               timeout_s=self.timeout_s, p99_s=self.p99_s, mix={scen: 1}, classes={cls: 1},
                               reservoir=0)

    def step_plan(self, rate: int) -> k6script.K6Plan:
        return k6script.K6Plan(langs=self.langs, rate=rate, duration_s=self.step_s, timeout_s=self.timeout_s,
                               p99_s=self.p99_s, mix=dict(self.mix), classes=dict(self.classes), reservoir=0)

    def _ticks(self, seconds: float) -> int:
        """A window ends at the first guard read at or after ``seconds``."""
        return max(1, math.ceil(seconds / self.interval_s)) * self.interval_s

    def estimate(self) -> dict:
        """Wall clock in seconds: the k6 window ends at the first guard read
        after the Job succeeded, a quiesce at the first read after its length."""
        q = self._ticks(self.quiesce_s)
        rep = self._ticks(K6_OVERHEAD_S + self.n / self.rate)
        step = self._ticks(K6_OVERHEAD_S + self.step_s)
        phase_a = q + len(self.cells()) * REPEATS * (rep + q)
        phase_b = len(self.steps) * (step + q)
        drain = self._ticks(self.drain_wait_s) if self.drain_wait_s else 0
        lo = HOOK_WAIT_S[0] + phase_a + phase_b
        # Worst case (code review L1): every k6 window runs to its Job's activeDeadlineSeconds.
        worst = (HOOK_WAIT_S[1] + q + len(self.cells()) * REPEATS * (self.window_max_s(self.cell_plan(
            *self.cells()[0][1:])) + q) + sum(self.window_max_s(self.step_plan(r)) + q for r in self.steps) + drain)
        return {"hook_wait_s": list(HOOK_WAIT_S), "phase_a_s": phase_a, "phase_b_s": phase_b,
                "drain_wait_max_s": drain, "total_s": [lo, HOOK_WAIT_S[1] + phase_a + phase_b + drain],
                "worst_s": worst, "k6_runs": len(self.cells()) * REPEATS + len(self.steps)}

    def window_max_s(self, plan: k6script.K6Plan) -> int:
        """The longest a k6 window can last: the Job's deadline, to the next guard read."""
        return self._ticks(plan.deadline_s)

    def memory_problem(self, mem_available: float, floor: float) -> str | None:
        """Code review L2: the top step's k6 memory limit must fit in the node's
        MemAvailable minus the K-D13 floor."""
        need = self.step_plan(self.steps[-1]).memory_limit_mi * 2**20
        if need > mem_available - floor:
            return (f"k6 memory limit at {self.steps[-1]} req/s ({need / 2**30:.2f} GiB) exceeds MemAvailable "
                    f"{mem_available / 2**30:.2f} GiB minus the floor {floor / 2**30:.2f} GiB")
        return None

    def window_names(self) -> list[str]:
        names = [f"cal-{i}-r{r}" for i, _, _ in self.cells() for r in range(1, REPEATS + 1)]
        return names + [f"cal-b{j}-{rate}" for j, rate in enumerate(self.steps)]

    def as_json(self) -> dict:
        out = asdict(self)
        out["langs"], out["steps"], out["repeats"] = list(self.langs), list(self.steps), REPEATS
        return out

    def describe(self, run: str) -> list[str]:
        est = self.estimate()
        cells = self.cells()
        lines = [f"cal --run {run}: dry run (no kubectl call, nothing written)",
                 f"languages: {' '.join(self.langs)} (one k6 run drives every language: services are disjoint)",
                 f"phase A: {len(cells)} cells x {REPEATS} repeats, N = {self.n} requests per language and repeat "
                 f"at {self.rate} req/s ({self.n // self.rate} s), quiesce {self.quiesce_s:g} s before the first "
                 "repeat and after each"]
        lines += [f"  {i}: scen={s} class={c} (k6 windows cal-{i}-r1, cal-{i}-r2)" for i, s, c in cells]
        lines += [f"phase B: steps {', '.join(map(str, self.steps))} req/s per language, {self.step_s} s each, "
                  f"default mix and classes, quiesce after each; stop at the first unclean step; "
                  f"loaded rate = {LOADED_RULE}; drain wait <= {self.drain_wait_s} s",
                  f"hook: demo re-applied through demo.sh DEMO_ENV_FILE (endpoint soak-otlp, canary header, "
                  f"new DEMO_RESTART_NONCE, */worker DEMO_INTERVAL_SECONDS={self.worker_interval_s}); "
                  "the trap restores the recorded demo",
                  f"objects (label otel-libs/soak-run={run}, namespace {render.NAMESPACE}): Service/soak-otlp; "
                  f"per k6 run ConfigMap + Job soak-k6-<window> ({est['k6_runs']} runs; each Job deleted after its "
                  "logs are saved, the ConfigMaps and any stopped Job left to the trap)",
                  "k6 windows: " + ", ".join(self.window_names()),
                  f"estimate: hook waits {est['hook_wait_s'][0] // 60}-{est['hook_wait_s'][1] // 60} min, "
                  f"phase A {est['phase_a_s'] / 60:.0f} min, phase B {est['phase_b_s'] / 60:.0f} min, drain wait "
                  f"<= {est['drain_wait_max_s'] / 60:.0f} min; total {est['total_s'][0] / 3600:.1f}-"
                  f"{est['total_s'][1] / 3600:.1f} h (worst case, every k6 Job to its deadline: "
                  f"{est['worst_s'] / 3600:.1f} h)",
                  f"outputs: runs/{run}/kcal-input.json, runs/{run}/soak-cal.json, raw/soak-k6-<window>.log, "
                  "guard.jsonl, windows.jsonl, trap.jsonl"]
        return lines


# ── JSON output ────────────────────────────────────────────────────────────

def write_json(path: Path, data: dict) -> None:
    """Atomic: a temp file in the same directory, then ``os.replace``; refused
    on a secret-scan hit."""
    text = json.dumps(data, indent=1, sort_keys=True, default=str) + "\n"
    hits = verify.scan_secret(text)
    if hits:
        raise CalError(f"{path.name}: secret scan hit ({', '.join(hits)}), not written")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ── pure derivations ───────────────────────────────────────────────────────

def cell_id(lang: str, scen: str, cls: str) -> str:
    """The cell id of ``soakcount.parse_key`` (lang, route, scen, class)."""
    return f"lang:{lang},route:{k6script.ROUTE},scen:{scen},class:{cls}"


def lang_services(lang: str) -> list[str]:
    return [f"demo-{lang}-{app}" for app in verify.DEMO_APPS]


def first_increase(series: list[dict], since: float) -> dict[str, float]:
    """``service_name`` -> the first sample time after ``since`` where one of its
    series rose above its value at ``since`` (0 for a series born later), or
    came back lower but above 0 (a reset with new spans)."""
    out: dict[str, float] = {}
    for s in sp.cluster_only(series):
        svc = (s.get("metric") or {}).get("service_name", "")
        prev = 0.0
        for t, v in sorted((float(t), float(v)) for t, v in s.get("values") or []):
            if t <= since:
                prev = v
                continue
            if v > prev or 0 < v < prev:
                out[svc] = min(out.get(svc, t), t)
                break
            prev = v
    return out


def first_drives(series: list[dict], pods: list[dict], hook_t: float, langs) -> dict[str, dict]:
    """lang -> {pod, startedAt, restartCount, first_drive, first_drive_delay_s, source}."""
    state = soakcount.worker_state(pods, langs)
    steps = first_increase(series, hook_t)
    out = {}
    for lang in langs:
        w = dict(state.get(lang) or {"pod": None, "startedAt": None, "restartCount": None})
        if w["pod"] is None:
            w["note"] = "no Running worker pod"
        if lang == "go":
            w.update(first_drive=None, source=GO_FIRST_DRIVE)
        else:
            seen = [steps[s] for s in (f"demo-{lang}-api", f"demo-{lang}-worker") if s in steps]
            if seen:
                w.update(first_drive=min(seen), source=DRIVE_SOURCE)
                if w.get("startedAt") is not None:
                    w["first_drive_delay_s"] = round(min(seen) - float(w["startedAt"]), 3)
            else:
                w.update(first_drive=None, source="unmeasured: no step of the api's or worker's fixed count "
                                                  "since the hook")
        out[lang] = w
    return out


def drive_conflicts(workers: dict[str, dict], interval: float, end: float, quiesce: float) -> list[str]:
    """``soakcount.next_drive_ok`` over the recorded workers; a non-Go worker
    without an observed first drive uses its start (the earliest it can be)."""
    proxy, bad = {}, []
    for lang, w in workers.items():
        base = w.get("startedAt")
        if base is None:
            bad.append(f"{lang}: no worker start recorded")
            continue
        proxy[lang] = {"startedAt": base, "first_drive": w.get("first_drive") or base}
    return bad + soakcount.next_drive_ok(proxy, interval, end, quiesce)


def repeat_records(table: dict[str, dict[str, int]], copy: dict, fams: dict, langs, scen: str, cls: str,
                   meta: dict) -> list[tuple[dict, list[str]]]:
    """One ``calibrate`` repeat per language of a k6 run, with the problems that
    keep it out of the repeats: an ambiguous copy count; an absent copy series
    with no sibling success (design K-D12: absent is ambiguous, recorded 0 only
    when another service of the language counted in the same repeat). A gateway
    family whose lower and upper counts differ is left out of ``gateway`` (named
    in ``gateway_ambiguous``): ``calibrate`` judges the lower value."""
    out = []
    for lang in langs:
        cid = cell_id(lang, scen, cls)
        rec = {"cell": cid, "counts": dict(table.get(cid) or {"expected": 0, "unexpected": 0, "noanswer": 0}),
               "copy": {}, "gateway": {}, "unset": [], **meta}
        problems, absent = [], []
        for svc in lang_services(lang):
            c = copy.get(svc)
            if c is None:  # no copy series at all since expiry + the window: no fixed span (review L3)
                rec["copy"][svc] = 0.0
                absent.append(svc)
                continue
            rec["copy"][svc] = float(c.count)
            if c.count_hi != c.count:
                problems.append(f"{svc}: ambiguous copy count {c.count:g}..{c.count_hi:g}")
            if c.unset:
                rec["unset"].append(svc)
            if svc in fams:
                amb = sorted(f for f, v in fams[svc].items() if soakcount.upper(v) != float(v))
                rec["gateway"][svc] = {f: float(v) for f, v in fams[svc].items() if f not in amb}
                if amb:
                    rec.setdefault("gateway_ambiguous", {})[svc] = amb
        if absent:
            rec["absent"] = absent
            if not any(v > 0 for v in rec["copy"].values()):
                problems.append(f"{', '.join(absent)}: {NO_SIBLING}")
        out.append((rec, problems))
    return out


def saturated(send: float, process_hi: float) -> bool:
    """A backlog, not noise: send - process (upper) > max(10, 5 % of send)."""
    return send - process_hi > max(SATURATION_MIN_MSGS, SATURATION_SHARE * send)


def k8b_rate(loaded: int | None, drain: dict[str, float], mix: dict[str, int]) -> tuple[int | None, dict]:
    """Code review M4: per language floor(drain / ok share), and the K8b rate =
    min(loaded, every language's cap) (SR15 "Windows and drain")."""
    share = mix.get("ok", 0) / sum(mix.values())
    per = {lang: (math.floor(d / share) if share else None) for lang, d in drain.items()}
    caps = [v for v in per.values() if v is not None]
    if loaded is None:
        return None, per
    return min([loaded] + caps), per


def dropped_iterations(summary: dict) -> int:
    """k6's ``dropped_iterations`` count (absent when k6 dropped none)."""
    metric = (summary.get("metrics") or {}).get("dropped_iterations") or {}
    return int((metric.get("values") or {}).get("count", 0))


def loaded_rate(steps: list[dict]) -> int | None:
    """The highest clean step of the leading clean run, minus one step; None
    when fewer than two steps were clean."""
    clean = 0
    for s in steps:
        if not s["clean"]:
            break
        clean += 1
    return steps[clean - 2]["rate"] if clean >= 2 else None


def drain_summary(steps: list[dict], langs) -> dict[str, dict]:
    """lang -> measured drain (the lowest saturated estimate) or unmeasured."""
    out = {}
    for lang in langs:
        sat = [s["drain"][lang]["drain_msg_per_s"] for s in steps
               if (s.get("drain") or {}).get(lang, {}).get("drain_msg_per_s") is not None]
        if sat:
            out[lang] = {"state": "measured", "msg_per_s": round(min(sat), 3),
                         "basis": "consumed / step span while a backlog was left at the boundary"}
        else:
            reached = [s["drain"][lang]["send"] / s["duration_s"] for s in steps
                       if (s.get("drain") or {}).get(lang) and s.get("duration_s")]
            out[lang] = {"state": "unmeasured",
                         "reason": "the worker kept up at every step (no backlog at a boundary)"
                         if reached else "no drain read",
                         "at_least_msg_per_s": round(max(reached), 3) if reached else None}
    return out


# ── the driver ─────────────────────────────────────────────────────────────

class Calibrator:
    """Phases A and B over ``ops``: ``now()``, ``k6(window, plan) -> (WindowResult,
    summary | None)``, ``quiet(window, seconds) -> WindowResult``,
    ``copy_series(t1, t2)``, ``gateway_series(t1, t2)``, ``copy_gate(t1, t2) ->
    {clean, reasons}``, ``pods()``."""

    def __init__(self, ops, plan: CalPlan, d: Path, log: Callable[[str], None] = print):
        self.ops, self.plan, self.d, self.log = ops, plan, d, log
        self.langs = plan.langs
        self.last_end: float | None = None
        self.repeats: list[dict] = []
        self.invalid: list[dict] = []
        self.driver: dict = {"plan": plan.as_json(), "estimate": plan.estimate(), "phase_a": {"cells": []},
                             "steps": [], "loaded_rule": LOADED_RULE}

    # windows ---------------------------------------------------------------
    def _gap(self, res) -> str | None:
        """A gap between two windows longer than the heartbeat rule allows."""
        prev, self.last_end = self.last_end, res.end
        if prev is not None and res.start is not None and res.start - prev > GAP_FACTOR * self.plan.interval_s:
            return f"laptop: {res.start - prev:.0f}s between windows (> {GAP_FACTOR} x {self.plan.interval_s}s)"
        return None

    def _drive_check(self, window_max_s: float) -> str | None:
        """Before each window (code review L1): now + the window's worst case +
        the quiesce must stay before every worker's next drive."""
        workers = self.driver.get("workers")
        if not workers:
            return None
        end = self.ops.now() + window_max_s
        bad = drive_conflicts(workers, self.plan.worker_interval_s, end, self.plan.quiesce_s)
        return ("worker drive inside the next window: " + "; ".join(bad)) if bad else None

    def _k6(self, window: str, plan: k6script.K6Plan) -> tuple[object, dict | None, str | None]:
        conflict = self._drive_check(self.plan.window_max_s(plan))
        if conflict:
            now = self.ops.now()
            return SimpleNamespace(start=now, end=now, valid=False, reason=conflict), None, conflict
        res, summary = self.ops.k6(window, plan)
        why = None if res.valid else (res.reason or "invalid")
        gap = self._gap(res)
        if why is None and gap:
            why = gap
        if why is None and summary is None:
            why = "k6: no summary between the markers"
        return res, summary, why

    def _quiet(self, window: str) -> tuple[object, str | None]:
        res = self.ops.quiet(window, self.plan.quiesce_s)
        why = None if res.valid else (res.reason or "invalid")
        gap = self._gap(res)
        return res, why or gap

    def _inputs(self) -> dict:
        return {"repeats": self.repeats, "invalid": self.invalid, "mix": dict(self.plan.mix),
                "planned": self.planned()}

    def planned(self) -> list[str]:
        return [cell_id(lang, s, c) for _, s, c in self.plan.cells() for lang in self.langs]

    def _write_inputs(self, drain: dict | None = None) -> None:
        inp = self._inputs()
        if drain:
            inp["drain"] = drain
        write_json(self.d / "kcal-input.json", inp)

    # phase A ---------------------------------------------------------------
    def phase_a(self) -> str | None:
        """Returns the abort reason, or None."""
        q, why = self._quiet("cal-q0")
        if why:
            return f"quiesce before the first repeat: {why}"
        boundary = q.end
        for idx, scen, cls in self.plan.cells():
            cell = {"index": idx, "scen": scen, "class": cls, "repeats": []}
            self.driver["phase_a"]["cells"].append(cell)
            reps, abort = [], None
            first = boundary
            for rep in range(1, REPEATS + 1):
                window = f"cal-{idx}-r{rep}"
                res, summary, why = self._k6(window, self.plan.cell_plan(scen, cls))
                end = res.end
                if why is None:  # an invalid k6 window stops the phase: no quiesce after it
                    qres, qwhy = self._quiet(f"cal-{idx}-q{rep}")
                    why, end = (f"quiesce: {qwhy}" if qwhy else None), qres.end
                table = soakcount.cell_table(k6script.cell_counts(summary)) if summary else {}
                reps.append({"window": window, "t1": boundary, "t2": end, "table": table, "why": why})
                cell["repeats"].append({"window": window, "t1": sp.iso(boundary), "t2": sp.iso(end),
                                        "invalid": why})
                boundary = end
                if why:
                    abort = f"{window}: {why}"
                    break
            self._count(idx, scen, cls, reps, first, boundary, cell)
            self._write_inputs()
            if abort:
                return abort
        return None

    def _count(self, idx: str, scen: str, cls: str, reps: list[dict], t_first: float, t_last: float,
               cell: dict) -> None:
        try:
            copy_series = self.ops.copy_series(t_first, t_last)
            gw_series = self.ops.gateway_series(t_first, t_last)
            gate = self.ops.copy_gate(t_first, t_last)
        except Exception as exc:  # noqa: BLE001 — any read failure invalidates the cell, named
            gate, copy_series, gw_series = {"clean": False, "reasons": [f"count read failed: {exc}"]}, [], []
        cell["copy_gate"] = gate
        for r in reps:
            copy = soakcount.copy_counts(copy_series, r["t1"], r["t2"])
            fams = soakcount.gateway_families(gw_series, r["t1"], r["t2"])
            meta = {"window": r["window"], "t1": r["t1"], "t2": r["t2"]}
            for rec, problems in repeat_records(r["table"], copy, fams, self.langs, scen, cls, meta):
                reasons = ([r["why"]] if r["why"] else []) + problems
                if not gate.get("clean"):
                    reasons.append("copy gate: " + "; ".join(gate.get("reasons") or ["not clean"]))
                if reasons:
                    self.invalid.append({**rec, "reasons": reasons})
                else:
                    self.repeats.append(rec)

    # workers ---------------------------------------------------------------
    def workers(self, hook_t: float) -> dict[str, dict]:
        now = self.ops.now()
        return first_drives(self.ops.copy_series(hook_t, now), self.ops.pods(), hook_t, self.langs)

    # phase B ---------------------------------------------------------------
    def phase_b(self, boundary: float) -> None:
        backlog = {lang: 0.0 for lang in self.langs}
        for j, rate in enumerate(self.plan.steps):
            window = f"cal-b{j}-{rate}"
            res, summary, why = self._k6(window, self.plan.step_plan(rate))
            end = res.end
            if why is None:
                qres, qwhy = self._quiet(f"cal-b{j}-q")
                why, end = (f"quiesce: {qwhy}" if qwhy else None), qres.end
            dropped = dropped_iterations(summary) if summary else None
            step = {"rate": rate, "window": window, "t1": sp.iso(boundary), "t2": sp.iso(end),
                    "duration_s": self.plan.step_s, "dropped_iterations": dropped, "noanswer": None,
                    "invalid": why}
            if summary:
                table = soakcount.cell_table(k6script.cell_counts(summary))
                step["answers"] = {lang: {o: sum(n[o] for c, n in table.items()
                                                 if soakcount.cell_tags(c).get("lang") == lang)
                                          for o in k6script.OUTCOMES} for lang in self.langs}
                step["noanswer"] = sum(a["noanswer"] for a in step["answers"].values())
            step["drain"] = self._drain(boundary, end, res.start, backlog)
            step["clean"] = step["invalid"] is None and dropped == 0
            self.driver["steps"].append(step)
            boundary = end
            if not step["clean"]:
                break
        self.driver["drain_wait"] = self._drain_wait(boundary, backlog)

    def _drain(self, t1: float, t2: float, started: float | None, backlog: dict[str, float]) -> dict | None:
        try:
            fams = soakcount.gateway_families(self.ops.gateway_series(t1, t2), t1, t2)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"gateway read failed: {exc}"}
        dc = soakcount.drain_check(fams, self.langs)
        out = {}
        span = t2 - (started if started is not None else t1)
        for lang in self.langs:
            send, proc = dc[lang]["send"], dc[lang]["process"]
            proc_hi = dc[lang].get("process_hi", proc)
            backlog[lang] += send - proc
            sat = saturated(send, proc_hi)
            rec = {"send": send, "process": proc, "process_hi": proc_hi, "backlog": backlog[lang],
                   "saturated": sat, "drain_msg_per_s": None}
            if sat and span > 0:
                rec["drain_msg_per_s"] = round(proc / span, 3)
            out[lang] = rec
        return out

    def _drain_wait(self, boundary: float, backlog: dict[str, float]) -> dict:
        waited, reads = 0.0, []
        left = lambda: any(v > SATURATION_MIN_MSGS for v in backlog.values())  # noqa: E731
        while left() and waited < self.plan.drain_wait_s:
            qres, why = self._quiet(f"cal-d{int(waited)}")
            if why:
                return {"waited_s": waited, "backlog": dict(backlog), "stopped": why, "reads": reads}
            reads.append({"t1": sp.iso(boundary), "t2": sp.iso(qres.end),
                          "read": self._drain(boundary, qres.end, None, backlog)})
            waited += qres.end - boundary
            boundary = qres.end
        return {"waited_s": waited, "backlog": dict(backlog), "drained": not left(), "reads": reads}

    # the whole run ---------------------------------------------------------
    def _write_cal(self, rate: int | None = None, drain: dict | None = None) -> dict:
        cal = soakcount.calibrate(self.repeats, mix=dict(self.plan.mix), rate=rate, drain=drain,
                                  invalid=self.invalid, planned=self.planned())
        out = {**cal, "driver": self.driver}
        write_json(self.d / "soak-cal.json", out)
        return out

    def run(self, hook_t: float) -> int:
        workers = self.workers(hook_t)
        self.driver["workers"] = workers
        self.driver["k6_peak_rss"] = K6_PEAK_RSS
        end = self.ops.now() + self.plan.estimate()["worst_s"]
        conflicts = drive_conflicts(workers, self.plan.worker_interval_s, end, self.plan.quiesce_s)
        if conflicts:
            self.driver["aborted"] = "worker drive inside the calibration: " + "; ".join(conflicts)
            self._write_inputs()
            self._write_cal()
            self.log(f"cal: ABORTED {self.driver['aborted']}")
            return 1
        abort = self.phase_a()
        self.driver["phase_a"]["aborted"] = abort
        cal = self._write_cal()
        for svc, v in sorted(cal["services"].items()):
            self.log(f"cal: {'QUALIFIED' if v['qualified'] else 'EXCLUDED':<10} {svc}"
                     + ("" if v["qualified"] else f": {v['cause']}"))
        if abort:
            self.log(f"cal: phase A ABORTED {abort}; phase B not run")
            return 1
        self.phase_b(self.last_end)
        loaded = loaded_rate(self.driver["steps"])
        self.driver["loaded_rate"] = loaded
        drain = drain_summary(self.driver["steps"], self.langs)
        self.driver["drain"] = drain
        measured = {lang: v["msg_per_s"] for lang, v in drain.items() if v["state"] == "measured"}
        self._write_inputs(measured)
        rate, per = k8b_rate(loaded, measured, dict(self.plan.mix))
        self.driver.update(k8b_rate=rate, k8b_rate_per_lang=per)
        cal = self._write_cal(loaded, measured)
        for s in self.driver["steps"]:
            self.log(f"cal: step {s['rate']} req/s: {'clean' if s['clean'] else 'NOT clean'}"
                     f" (dropped_iterations {s['dropped_iterations']}, noanswer {s['noanswer']}"
                     f"{', ' + s['invalid'] if s['invalid'] else ''})")
        for lang, v in drain.items():
            self.log(f"cal: drain {lang}: " + (f"{v['msg_per_s']} msg/s" if v["state"] == "measured"
                                               else f"unmeasured ({v['reason']}; >= {v['at_least_msg_per_s']})"))
        self.log(f"cal: loaded rate {loaded if loaded is not None else 'none (fewer than two clean steps)'}"
                 + (f"; rate cap {json.dumps(cal['rate_cap'], sort_keys=True)}" if cal.get("rate_cap") else "")
                 + f"; K8b rate {rate} (per language {json.dumps(per, sort_keys=True)})")
        failed = sorted(lang for lang, v in (cal.get("rate_cap") or {}).items() if v["verdict"] == "FAIL")
        if failed:
            self.log(f"cal: FAIL rate cap for {', '.join(failed)}: loaded rate x ok share above the drain rate; "
                     f"run K8b at {rate} req/s or lower DEMO_POLL_SECONDS (SR15 Windows and drain)")
            return 2
        return 0 if loaded is not None else 1
