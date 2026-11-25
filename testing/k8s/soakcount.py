"""``verify.py soak``: the soak's count and passive verdicts (specs/soak-simulation
task K-count, r6.2 with the K0e corrections; design S-D5, K-D12, K-D19).

Read-only on the cluster. Actions (each writes ``soak-<action>.json`` under the
git-ignored ``testing/k8s/soak/runs/<run>/``; ``report`` turns them into the
committed ``specs/soak-simulation/reports/<run>.md``):

  passive  K8a: per demo container, the S-D5 slope rule (judged span and its
           second half; the 1.10 ratio report-only) on RSS, working set,
           ``container_threads``, Go goroutines, the .NET thread pool, the FDs of
           the ``exec`` sweep (``guard.jsonl``, all four languages); restarts 0;
           Python ``process_open_file_descriptor_count`` as a cross-check;
           Node.js runtime state ``n/a`` (AG12). ``--gate`` is the coverage gate
           (span metrics flowing per demo job, runtime coverage >= 95 %).
  count    K8b: the fixed-family count per service from the copy's
           ``otel_libs_soak_fixed_spans_total`` (primary, copy sentinels) and the
           gateway ``traces_span_metrics_calls_total`` per family (cross-check,
           shared sentinels), against the k6 cells x the K-cal k; the drain check.
  shut     K-shut: the old ``service_instance_id``'s fixed count in its bound,
           exit within grace.
  cal      K-cal: per-cell k from repeats (integer, stable), the rate cap.
  report   the summary, after the secret scan (canary, fakeotlp token).

Every shared-job query carries ``cluster="k8s01"`` (``soakplatform.sel``) and
every result is filtered to that cluster again (K0e H5).
"""

from __future__ import annotations

import json
import math
import re
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "soak"))
import k6script  # noqa: E402  (the loop's k6 summary parser, testing/k8s/soak)
import soakplatform as sp  # noqa: E402
import verify  # noqa: E402

REPO = HERE.parent.parent
REPORTS_DIR = REPO / "specs" / "soak-simulation" / "reports"
FIXED_METRIC = "otel_libs_soak_fixed_spans_total"
SPAN_METRIC = "traces_span_metrics_calls_total"
# Series expiry per source (SR15 "Counting in bounds", K0e H2 / H4; live 2026-11-23).
COPY_EXPIRY_S = 1800
GATEWAY_EXPIRY_S = 300
UNSET = "unset"
FAKEOTLP_TOKEN = "parity-redirect-token"
# Fixed span families (requirements Glossary): kinds, demo-scope names, RPC.
FIXED_KINDS = {"SPAN_KIND_SERVER": "server", "SPAN_KIND_CONSUMER": "consumer", "SPAN_KIND_PRODUCER": "producer"}
DEMO_SCOPE = ("demo.order.accept", "demo.order.process")
RPC_NAME_RE = re.compile(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+/[A-Za-z0-9_]+")
SERVICE_RE = re.compile(r"demo-(go|python|node|dotnet)-(worker|api|backend)")
# S-D5 (2026-11-23 recalibration): slope <= 1 % of the median per hour.
SLOPE_SHARE = 0.01
RATIO_LIMIT = 1.10
# Integer gauges (threads, goroutines, FDs, the .NET thread-pool count and queue):
# owner ruling 2026-11-23 (design S-D5 note, `unmeasured`): the limit per judged
# span or half is max(1 %/h x median, Q / span_h), Q = 2 units, when every
# sample of the span is an integer; the queue length is fitted on 15 min medians.
INTEGER_Q = 2.0
MEDIAN_BIN_S = 900
MEDIAN_BIN_RULES = ("threadpool_queue",)
COVERAGE_MIN = 0.95      # coverage gate (initial, `unmeasured`, K0e M6)
EXPORT_INTERVAL_S = 10   # the demo apps' OTEL_METRIC_EXPORT_INTERVAL (10000, testing/k8s/demo/demo.sh)
# The FD read, without a shell (review H2): the lines of ``ls -1 /proc/1/fd``
# are counted here; a non-zero exit is an error, never 0.
FD_ARGV = ("ls", "-1", "/proc/1/fd")
FD_TIMEOUT_S = 10
# The kubelet cAdvisor scrape interval: 242 samples per hour read on k8s01
# 2026-11-23 (`count_over_time(container_memory_rss{...}[1h])`), so 15 s.
CADVISOR_SCRAPE_S = 15
CADVISOR = {"job": "kubelet", "metrics_path": "/metrics/cadvisor", "namespace": sp.APP_NS,
            "container": ("!~", "|POD")}
# K8a judged metrics: (rule id, metric, matchers, languages, key labels).
PASSIVE_METRICS = (
    ("rss", "container_memory_rss", CADVISOR, None, ("pod", "container")),
    ("working_set", "container_memory_working_set_bytes", CADVISOR, None, ("pod", "container")),
    ("threads", "container_threads", CADVISOR, None, ("pod", "container")),
    ("goroutines", "go_goroutine_count", {"job": ("=~", f"{sp.APP_NS}/demo-go-.*")}, ("go",),
     ("k8s_pod_name", "k8s_container_name")),
    ("threadpool_threads", "dotnet_thread_pool_thread_count_total", {"job": ("=~", f"{sp.APP_NS}/demo-dotnet-.*")},
     ("dotnet",), ("k8s_pod_name", "k8s_container_name")),
    ("threadpool_queue", "dotnet_thread_pool_queue_length_total", {"job": ("=~", f"{sp.APP_NS}/demo-dotnet-.*")},
     ("dotnet",), ("k8s_pod_name", "k8s_container_name")),
)
CROSSCHECK = ("python_fds", "process_open_file_descriptor_count", {"job": ("=~", f"{sp.APP_NS}/demo-python-.*")},
              ("python",), ("k8s_pod_name", "k8s_container_name"))
RUNTIME_RULES = {"go": ("goroutines",), "dotnet": ("threadpool_threads", "threadpool_queue"),
                 "python": ("python_fds",), "node": ()}


# ── series rules (SR15 "Counting in bounds", K0e H4) ──


# The scrape interval per source, measured on k8s01 2026-11-23 (re-review HIGH-1):
# ``count_over_time(up{job="dev-otel-libs/soak-agent", endpoint="count"}[1h])``
# = 240 (15 s); ``count_over_time(traces_span_metrics_calls_total{...}[1h])`` =
# 120 (30 s). A read measures its own spacing (``measured_scrape``) and uses it
# when it has enough samples; these are the dated fallbacks.
COPY_SCRAPE_S = 15
GATEWAY_SCRAPE_S = 30
# The quiesce after the last traffic before a count boundary (Glossary "Quiesced
# checkpoint": SDK batch delay + export timeout + agent batch timeout, plus the
# copy and gateway scrapes); initial value, `unmeasured` (K-cal, K8b).
QUIESCE_S = 120.0


class Counted(float):
    """A count with an upper value: the float is the lower count, ``hi`` the
    upper one (they differ only for ambiguous reappearances, re-review HIGH-1)."""

    def __new__(cls, lo: float, hi: float | None = None):
        obj = super().__new__(cls, lo)
        obj.hi = float(lo if hi is None else hi)
        return obj


def upper(x: float) -> float:
    return getattr(x, "hi", float(x))


def measured_scrape(series: list[dict], default: float, min_steps: int = 10) -> float:
    """The median spacing of consecutive samples over every series of a read,
    or ``default`` with fewer than ``min_steps`` spacings."""
    steps = []
    for s in series:
        ts = sorted(float(t) for t, _ in s.get("values") or [])
        steps += [b - a for a, b in zip(ts, ts[1:]) if b > a]
    return statistics.median(steps) if len(steps) >= min_steps else float(default)


def count_series(values: list, t1: float, t2: float, expiry: float,
                 scrape: float) -> tuple[float, float, list[str]]:
    """(lower, upper, events): the count of one cumulative series over (t1, t2].

    Baseline: the last sample at or before t1 (the read reaches ``expiry`` +
    margin before t1). No baseline: the series was born in the window, or came
    back after it expired, and adds its first value. Expiry is decided by the
    time since the value last *changed* (review H1): the exporters keep a flat
    value until the expiry, then the series vanishes and returns about one
    scrape later.
      - sample gap > 2 x ``scrape`` and unchanged >= expiry - scrape: expired, adds v;
      - gap <= 2 x scrape, unchanged >= expiry - scrape, v >= last: ambiguous
        (re-review HIGH-1) — within a scrape a real update just before the
        expiry and an expiry followed by a fresh value look the same: the lower
        count adds v - last, the upper adds v, and the change clock restarts;
      - otherwise v - last, or v when v < last (a reset)."""
    pts = sorted((float(t), float(v)) for t, v in values)
    before = [p for p in pts if p[0] <= t1]
    inside = [p for p in pts if t1 < p[0] <= t2]
    events: list[str] = []
    if not inside:
        return 0.0, 0.0, events
    lo = hi = 0.0
    if before:
        prev_t, prev = before[-1]
        changed_at = prev_t
        for t, v in reversed(before[:-1]):  # the start of the flat run ending at the baseline
            if v != prev:
                break
            changed_at = t
    else:
        events.append("first sample inside the window: adds its value")
        lo = hi = inside[0][1]
        prev_t, prev = inside[0]
        changed_at = prev_t
        inside = inside[1:]
    for t, v in inside:
        gap = t - prev_t
        aged = t - changed_at >= expiry - scrape
        # A flat sample (v == prev) is never an expiry or an ambiguity: an idle
        # series keeps exposing its last value until the expiry, and a scrape
        # gap on a flat series adds nothing (K-count final review).
        if aged and gap > 2 * scrape and v != prev:
            events.append(f"gap {gap:.0f}s, unchanged for {t - changed_at:.0f}s >= expiry: expired, adds v")
            lo, hi, changed_at = lo + v, hi + v, t
        elif aged and v > prev:
            events.append(f"ambiguous: expiry or update (gap {gap:.0f}s, unchanged for {t - changed_at:.0f}s)")
            lo, hi, changed_at = lo + (v - prev), hi + v, t
        elif v < prev:
            events.append(f"lower value after {gap:.0f}s: reset")
            lo, hi, changed_at = lo + v, hi + v, t
        else:
            lo, hi = lo + (v - prev), hi + (v - prev)
            if v != prev:
                changed_at = t
        prev_t, prev = t, v
    return lo, hi, events


def raw_read(r: sp.Reader, metric: str, matchers: dict, t1: float, t2: float, expiry: float) -> tuple[str, list[dict]]:
    """(query, k8s01 series) of a raw range read at t2 reaching expiry + margin before t1."""
    span = int(t2 - t1) + int(expiry) + sp.BORN_GRACE + sp.SCRAPE_S
    q = f"{sp.sel(metric, matchers)}[{span}s]"
    return q, sp.cluster_only(r.prom(q, t2))


def fixed_family(kind: str, name: str) -> str | None:
    """The fixed family of a gateway span-metrics series, or None (report-only)."""
    if kind in FIXED_KINDS:
        return f"{FIXED_KINDS[kind]}:{name}"
    if name in DEMO_SCOPE:
        return f"demo:{name}"
    if kind == "SPAN_KIND_CLIENT" and RPC_NAME_RE.fullmatch(name):
        return f"rpc-client:{name}"
    return None


@dataclass
class CopyCount:
    service: str
    count: float = 0.0
    instances: dict[str, float] = field(default_factory=dict)
    series: int = 0
    events: list[str] = field(default_factory=list)
    count_hi: float = 0.0         # the upper count (ambiguous reappearances, re-review HIGH-1)
    instances_hi: dict[str, float] = field(default_factory=dict)
    scrape_s: float = COPY_SCRAPE_S
    unset: bool = False           # an unset series with samples in (t1, t2] (D4; invalidates)
    unset_outside: bool = False   # an unset series seen only outside the window (a finding)


def copy_counts(series: list[dict], t1: float, t2: float, expiry: float = COPY_EXPIRY_S) -> dict[str, CopyCount]:
    """Per demo ``service_name``: the fixed count summed over ``pod``, ``instance``,
    ``exported_instance``, per ``service_instance_id`` too; ``unset`` flagged (D4)."""
    out: dict[str, CopyCount] = {}
    series = sp.cluster_only(series)  # defense in depth: never another cluster (K0e H5)
    scrape = measured_scrape(series, COPY_SCRAPE_S)
    for s in series:
        m = s.get("metric") or {}
        svc = m.get("service_name", "")
        if not SERVICE_RE.fullmatch(svc):
            continue
        c = out.setdefault(svc, CopyCount(svc))
        inc, inc_hi, ev = count_series(s.get("values") or [], t1, t2, expiry, scrape)
        iid = m.get("service_instance_id", UNSET)
        c.count += inc
        c.count_hi += inc_hi
        c.scrape_s = scrape
        c.series += 1
        c.instances[iid] = c.instances.get(iid, 0.0) + inc
        c.instances_hi[iid] = c.instances_hi.get(iid, 0.0) + inc_hi
        c.events += [f"{m.get('pod', '?')}/{iid}: {e}" for e in ev]
        if iid == UNSET:
            in_window = any(t1 < float(t) <= t2 for t, _ in s.get("values") or [])
            c.unset = c.unset or in_window
            c.unset_outside = c.unset_outside or not in_window
    return out


def gateway_families(series: list[dict], t1: float, t2: float,
                     expiry: float = GATEWAY_EXPIRY_S) -> dict[str, dict[str, float]]:
    """``service_name`` -> family -> ``Counted`` (lower, ``.hi`` upper), summed
    by (service_name, span_kind, span_name) over every other label
    (``service_version`` included)."""
    acc: dict[str, dict[str, list[float]]] = {}
    series = sp.cluster_only(series)
    scrape = measured_scrape(series, GATEWAY_SCRAPE_S)
    for s in series:
        m = s.get("metric") or {}
        svc = m.get("service_name", "")
        fam = fixed_family(m.get("span_kind", ""), m.get("span_name", ""))
        if fam is None or not SERVICE_RE.fullmatch(svc):
            continue
        lo, hi, _ = count_series(s.get("values") or [], t1, t2, expiry, scrape)
        cur = acc.setdefault(svc, {}).setdefault(fam, [0.0, 0.0])
        cur[0] += lo
        cur[1] += hi
    return {svc: {fam: Counted(lo, hi) for fam, (lo, hi) in fams.items()} for svc, fams in acc.items()}


# ── k6 cells and expected bounds ──


def parse_key(key: str) -> tuple[str, str] | None:
    """``soak_requests{lang:go,...,outcome:x}`` -> (cell id without the outcome, outcome)."""
    m = re.fullmatch(r"soak_requests\{(.*)\}", key)
    if not m:
        return None
    tags = dict(t.split(":", 1) for t in m.group(1).split(",") if ":" in t)
    outcome = tags.pop("outcome", None)
    if outcome is None:
        return None
    order = ["lang", "route", "scen", "class", "phase"]
    return ",".join(f"{k}:{tags[k]}" for k in order if k in tags), outcome


def cell_table(counts: dict[str, int]) -> dict[str, dict[str, int]]:
    """cell id -> {expected, unexpected, noanswer} from ``k6script.cell_counts``."""
    out: dict[str, dict[str, int]] = {}
    for key, n in counts.items():
        p = parse_key(key)
        if p:
            out.setdefault(p[0], {"expected": 0, "unexpected": 0, "noanswer": 0})[p[1]] = int(n)
    return out


def cell_tags(cell: str) -> dict[str, str]:
    return dict(t.split(":", 1) for t in cell.split(","))


def base_cell(cell: str) -> str:
    """The cell without its K-shut ``phase`` tag (K-cal calibrates without phases)."""
    return ",".join(t for t in cell.split(",") if not t.startswith("phase:"))


def service_lang(service: str) -> str:
    m = SERVICE_RE.fullmatch(service)
    return m.group(1) if m else ""


@dataclass
class Bound:
    lo: float
    hi: float
    missing: list[str] = field(default_factory=list)   # cells with traffic and no k
    unexpected: int = 0
    answered: int = 0
    noanswer: int = 0
    kmax: int = 0      # the largest k over the cells with traffic (K-shut upper part, review M4)


def bounds(cells: dict[str, dict[str, int]], kmap: dict[str, int], lang: str, *,
           phase: str | None = None) -> Bound:
    """[Σ answered x k, Σ (answered + no answer) x k] over the cells of ``lang``
    (answered = expected + unexpected status; an unexpected status is counted
    and named, its k is the cell's)."""
    b = Bound(0.0, 0.0)
    for cell, n in cells.items():
        tags = cell_tags(cell)
        if tags.get("lang") != lang or (phase is not None and tags.get("phase") != phase):
            continue
        answered = n["expected"] + n["unexpected"]
        if answered + n["noanswer"] == 0:
            continue
        k = kmap.get(base_cell(cell))
        if k is None:
            b.missing.append(cell)
            continue
        b.lo += answered * k
        b.hi += (answered + n["noanswer"]) * k
        b.unexpected += n["unexpected"]
        b.answered += answered
        b.noanswer += n["noanswer"]
        b.kmax = max(b.kmax, k)
    return b


def judge_bound(counted: float, b: Bound, counted_hi: float | None = None) -> str:
    """No vacuous PASS (review M3): a language with no answered request is
    UNQUALIFIED, not PASS. ``counted`` is the lower count and ``counted_hi``
    the upper one (``upper(counted)`` by default): LOSS when even the upper count
    is below the bound, DUPLICATES when even the lower one is above it."""
    hi = upper(counted) if counted_hi is None else counted_hi
    if b.missing or b.answered == 0:
        return "UNQUALIFIED"
    if hi < b.lo:
        return "LOSS"
    if float(counted) > b.hi:
        return "DUPLICATES"
    return "PASS"


# ── sentinels scoped by verdict (SR15 "Sentinels (r6.2)", K0e B1) ──


def queue_check(r: sp.Reader, job: str, at: float, *, zero: bool) -> tuple[bool, str]:
    """The copy's queue reads 0 (``zero``), or a shared queue <= 10 % of capacity."""
    size = sp.cluster_only(r.prom(sp.sel("otelcol_exporter_queue_size", {"job": job}), at))
    cap = sp.cluster_only(r.prom(sp.sel("otelcol_exporter_queue_capacity", {"job": job}), at))
    if not size:
        return False, f"{job}: no otelcol_exporter_queue_size at {sp.iso(at)}"
    capacity = {_key(s["metric"]): float(s["value"][1]) for s in cap}
    bad = []
    for s in size:
        v = float(s["value"][1])
        c = capacity.get(_key(s["metric"]))
        if zero and v != 0:
            bad.append(f"{s['metric'].get('exporter', '?')} size {v:g}")
        elif not zero and (c is None or v > 0.10 * c):
            bad.append(f"{s['metric'].get('exporter', '?')} size {v:g} of {c}")
    return not bad, "; ".join(bad) or f"{job} queue ok at {sp.iso(at)}"


def _key(m: dict) -> tuple:
    return tuple(sorted((k, v) for k, v in m.items() if k != "__name__"))


def gateway_restart(pods: list[dict], uptime: list[dict], t1: float, t2: float) -> tuple[bool, str]:
    """The gateway-restart detector: every gateway container ``startedAt`` <= t1
    (a restart sets a new one, a rollout a new pod) and ``otelcol_process_uptime``
    grew without a drop over [t1, t2]."""
    entry = sp.roll_entry(pods, "opentelemetry", r"otel-gateway-collector-[a-z0-9]+-[a-z0-9]+", "gateway")
    if not entry["pods"]:
        return False, "no Running gateway pod"
    for p in entry["pods"]:
        for c in p["containers"]:
            started = sp.parse_ts(c.get("startedAt"))
            if started is None or started > t1:
                return False, f"{p['name']}/{c['name']} started {c.get('startedAt')} after t1"
    for s in uptime:
        vals = [float(v) for t, v in s.get("values") or [] if t1 <= float(t) <= t2]
        if not vals:
            return False, "no otelcol_process_uptime sample in [t1, t2]"
        if any(b < a for a, b in zip(vals, vals[1:])):
            return False, "otelcol_process_uptime dropped"
    if not uptime:
        return False, "no otelcol_process_uptime series"
    return True, f"{len(entry['pods'])} gateway pod(s) unchanged since t1"


def scoped_gates(verdicts: list, extra: dict[str, tuple[bool, str]]) -> dict[str, dict]:
    """{copy, shared}: clean flag and reasons from the ``read_sentinels`` verdicts
    (CounterVerdict) and the queue and restart checks (scope -> (ok, detail))."""
    out = {"copy": {"clean": True, "reasons": []}, "shared": {"clean": True, "reasons": []}}
    for v in verdicts:
        if v.verdict not in ("CLEAN",):
            out[v.scope if v.scope in out else "shared"]["clean"] = False
            out[v.scope if v.scope in out else "shared"]["reasons"].append(f"{v.id}: {v.verdict} {v.increase:g}")
    for name, (ok, detail) in extra.items():
        scope = name.split(":", 1)[0]
        if not ok:
            out[scope]["clean"] = False
            out[scope]["reasons"].append(f"{name}: {detail}")
    return out


# ── windows and drain (SR15 "Windows and drain (r6.2)", K0e H6) ──


def window_t1(workers: dict[str, dict], quiesce: float) -> tuple[float, dict[str, float]]:
    """t1 per language and overall (the latest): Go from the worker's
    ``startedAt`` + quiesce (its first drive is at start + interval), the others
    from their observed first ``demo.drive`` + quiesce."""
    per = {}
    for lang, w in workers.items():
        base = w.get("startedAt") if lang == "go" else w.get("first_drive")
        if base is None:
            raise ValueError(f"{lang}: no {'startedAt' if lang == 'go' else 'first demo.drive'} for t1")
        per[lang] = float(base) + quiesce
    return max(per.values()), per


def next_drive_ok(workers: dict[str, dict], interval: float, t2: float, quiesce: float = 0.0) -> list[str]:
    """Languages whose next worker drive falls before t2 + quiesce (the window's
    spans are still arriving until then; review M2)."""
    bad = []
    end = t2 + quiesce
    for lang, w in workers.items():
        nxt = (float(w["startedAt"]) + interval if lang == "go" else float(w["first_drive"]) + interval)
        if nxt <= end:
            bad.append(f"{lang}: next demo.drive at {sp.iso(nxt)} <= t2 + quiesce {sp.iso(end)}")
    return bad


def worker_state(pods: list[dict], langs) -> dict[str, dict]:
    """lang -> {pod, startedAt, restartCount} of the Running demo worker (kubectl JSON)."""
    out = {}
    for p in pods:
        meta = p.get("metadata") or {}
        m = verify.POD_RE.fullmatch(meta.get("name", ""))
        if not m or m.group(2) != "worker" or m.group(1) not in langs or sp.terminating(p):
            continue
        cs = sp.container_states(p)
        if cs:
            out[m.group(1)] = {"pod": meta["name"], "startedAt": sp.parse_ts(cs[0].get("startedAt")),
                               "restartCount": cs[0].get("restartCount", 0)}
    return out


def window_problems(workers: dict[str, dict], live: dict[str, dict], t1: float, t2: float, *,
                    quiesce: float, interval: float, langs) -> dict[str, list[str]]:
    """lang -> what makes its window unusable (review M2): t1 before the t1 rule,
    the next drive before t2 + quiesce, a worker that restarted or was replaced
    since the inputs were recorded."""
    out: dict[str, list[str]] = {lang: [] for lang in langs}
    for lang in langs:
        w = workers.get(lang)
        if w is None:
            out[lang].append("no worker input (startedAt / first demo.drive)")
            continue
        try:
            _, per = window_t1({lang: w}, quiesce)
        except ValueError as exc:
            out[lang].append(str(exc))
            continue
        if t1 < per[lang]:
            out[lang].append(f"t1 {sp.iso(t1)} before the t1 rule {sp.iso(per[lang])}")
        out[lang] += next_drive_ok({lang: w}, interval, t2, quiesce)
        lw = live.get(lang)
        if lw is None:
            out[lang].append("no Running worker pod now")
        elif (w.get("restartCount") is not None and lw["restartCount"] != w["restartCount"]) or \
                (w.get("startedAt") is not None and lw["startedAt"] is not None
                 and abs(lw["startedAt"] - float(w["startedAt"])) > 1):
            out[lang].append(f"worker {lw['pod']} restarted or replaced since the inputs")
    return out


def drain_check(fams: dict[str, dict[str, float]], langs, cells: dict[str, dict[str, int]] | None = None) -> dict[str, dict]:
    """Per language: PRODUCER ``send demo:<lang>:orders`` = CONSUMER ``process
    demo:<lang>:orders``. With no send and no process the verdict is NA only when
    no ``ok`` request was answered; otherwise FAIL (no vacuous pass, review M3)."""
    out = {}
    for lang in langs:
        send = sum(float(f.get(f"producer:send demo:{lang}:orders", 0.0)) for f in fams.values())
        proc = sum(float(f.get(f"consumer:process demo:{lang}:orders", 0.0)) for f in fams.values())
        send_hi = sum(upper(f.get(f"producer:send demo:{lang}:orders", 0.0)) for f in fams.values())
        proc_hi = sum(upper(f.get(f"consumer:process demo:{lang}:orders", 0.0)) for f in fams.values())
        ok_answered = sum(n["expected"] + n["unexpected"] for c, n in (cells or {}).items()
                          if cell_tags(c).get("lang") == lang and cell_tags(c).get("scen") == "ok")
        # With ambiguous reappearances each side is an interval: equal when they overlap.
        if send_hi < proc or proc_hi < send:
            verdict = "FAIL"
        elif send_hi > 0:
            verdict = "PASS"
        else:
            verdict = "FAIL" if ok_answered else "NA"
        out[lang] = {"send": send, "process": proc, "ok_answered": ok_answered, "verdict": verdict}
        if send_hi != send or proc_hi != proc:
            out[lang].update(send_hi=send_hi, process_hi=proc_hi, ambiguous=True)
    return out


# ── K8b: the count verdicts ──


def count_verdicts(copy: dict[str, CopyCount], fams: dict[str, dict[str, float]], cells: dict[str, dict[str, int]],
                   kcal: dict, gates: dict, services, window: dict[str, list[str]] | None = None) -> dict:
    """Per service: the copy verdict (primary) and the gateway cross-check per
    family; a language whose window is unusable (``window``) is UNQUALIFIED."""
    out = {}
    for svc in services:
        lang = service_lang(svc)
        cal = (kcal.get("services") or {}).get(svc) or {}
        c = copy.get(svc, CopyCount(svc))
        entry = {"counted": c.count, "instances": c.instances, "series": c.series, "events": c.events[:20],
                 "scrape_s": c.scrape_s}
        if c.count_hi != c.count:
            entry.update(counted_hi=c.count_hi, instances_hi=c.instances_hi,
                         ambiguous="expiry or update: the count is an interval")
        if not cal.get("qualified"):
            entry.update(verdict="UNQUALIFIED", reason=f"K-cal: {cal.get('cause') or 'not calibrated'}")
        else:
            b = bounds(cells, cal.get("k") or {}, lang)
            entry.update(lo=b.lo, hi=b.hi, unexpected=b.unexpected, verdict=judge_bound(c.count, b, c.count_hi))
            if b.missing:
                entry["reason"] = f"no K-cal k for cells {b.missing}"
            elif b.answered == 0:
                entry["reason"] = f"no answered request for {lang}"
        if c.unset_outside and not c.unset:
            entry["finding"] = "service_instance_id=\"unset\" series outside the window (D4)"
        problems = (window or {}).get(lang) or []
        if c.unset:
            entry.update(verdict="UNQUALIFIED", finding="service_instance_id=\"unset\" series (D4)",
                         reason="a demo series without service.instance.id invalidates the per-instance verdict")
        elif problems:
            entry.update(verdict="UNQUALIFIED", reason="window: " + "; ".join(problems))
        elif not gates["copy"]["clean"] and entry["verdict"] != "UNQUALIFIED":
            entry.update(verdict="UNQUALIFIED", reason="copy sentinels: " + "; ".join(gates["copy"]["reasons"]))
        cross = {}
        for fam, kfam in ((kcal.get("families") or {}).get(svc) or {}).items():
            got = (fams.get(svc) or {}).get(fam, 0.0)
            if not gates["shared"]["clean"] or problems:
                cross[fam] = {"counted": float(got), "verdict": "UNQUALIFIED",
                              "reason": "window: " + "; ".join(problems) if problems else "shared sentinels not clean"}
                continue
            b = bounds(cells, kfam, lang)
            cross[fam] = {"counted": float(got), "lo": b.lo, "hi": b.hi, "verdict": judge_bound(got, b)}
            if upper(got) != float(got):
                cross[fam]["counted_hi"] = upper(got)
        entry["gateway"] = cross
        out[svc] = entry
    return out


# ── K-shut (SR15 "Shutdown (r6.2)", K0e H3) ──

SIGKILL_EXIT = 137


def exit_verdict(deleted_at: float | None, finished_at: float | None, grace_s: float | None,
                 exit_code: int | None) -> dict:
    """Exit within grace (review H3): both times and the grace are required; a
    SIGKILL exit (137) or an exit at grace - 1 s or later is a FAIL."""
    if deleted_at is None or finished_at is None or grace_s is None:
        return {"verdict": "UNQUALIFIED", "reason": "deleted_at, finished_at and the grace are required"}
    took = finished_at - deleted_at
    ok = 0 <= took < grace_s - 1 and exit_code != SIGKILL_EXIT
    return {"seconds": took, "grace_s": grace_s, "exit_code": exit_code, "verdict": "PASS" if ok else "FAIL"}


def shut_verdict(old_iid: str, counted: float, cells: dict[str, dict[str, int]], kmap: dict[str, int] | None,
                 lang: str, *, post_before_finish: dict[str, int] | int | None = None,
                 deleted_at: float | None = None, finished_at: float | None = None, grace_s: float | None = None,
                 exit_code: int | None = None, copy_gate: dict | None = None) -> dict:
    """The old process's fixed count in [pre answered x k, (pre answered + post
    answered before ``finishedAt`` + no answer of both phases) x k], and the exit
    within grace, folded into one verdict (a failed exit is FAIL).

    ``post_before_finish``: per post cell, the answers before ``finishedAt``
    (exact); a single number: that many answers x the largest post k (named);
    absent: every post answer (exact per cell, named) (review M4)."""
    out: dict = {"service_instance_id": old_iid, "counted": float(counted),
                 "exit": exit_verdict(deleted_at, finished_at, grace_s, exit_code)}
    if upper(counted) != float(counted):
        out["counted_hi"] = upper(counted)
    count_v = None
    if old_iid == UNSET:
        count_v, out["reason"] = "UNQUALIFIED", "old service_instance_id is unset (D4)"
    elif copy_gate is not None and not copy_gate.get("clean"):
        count_v, out["reason"] = "UNQUALIFIED", "copy sentinels: " + "; ".join(copy_gate.get("reasons") or [])
    elif not kmap:
        count_v, out["reason"] = "REPORT-ONLY", "K-cal did not qualify this api's copy total"
    else:
        pre = bounds(cells, kmap, lang, phase="pre")
        post = bounds(cells, kmap, lang, phase="post")
        if pre.missing or post.missing:
            count_v, out["reason"] = "UNQUALIFIED", f"no K-cal k for {pre.missing + post.missing}"
        elif pre.answered == 0:
            count_v, out["reason"] = "UNQUALIFIED", "no pre answer"
        else:
            if isinstance(post_before_finish, dict):
                post_part = sum(int(n) * kmap.get(base_cell(c), post.kmax) for c, n in post_before_finish.items())
                out["post_basis"] = "per-cell post answers before finishedAt"
            elif post_before_finish is not None:
                post_part = int(post_before_finish) * post.kmax
                out["post_basis"] = f"{int(post_before_finish)} post answers x the largest post k {post.kmax}"
            else:
                post_part = post.lo
                out["post_basis"] = "post answers before finishedAt unknown: every post answer bounds it"
            lo, hi = pre.lo, pre.lo + post_part + (pre.hi - pre.lo) + (post.hi - post.lo)
            out.update(lo=lo, hi=hi)
            count_v = "LOSS" if upper(counted) < lo else "DUPLICATES" if float(counted) > hi else "PASS"
    out["count_verdict"] = count_v
    ev = out["exit"]["verdict"]
    out["verdict"] = ("FAIL" if ev == "FAIL" else count_v if count_v in ("LOSS", "DUPLICATES") else
                      "UNQUALIFIED" if ev == "UNQUALIFIED" or count_v == "UNQUALIFIED" else
                      count_v)
    return out


# ── K-cal (SR15 "Counting in bounds", "Windows and drain") ──


def _integral(x: float) -> bool:
    return abs(x - round(x)) < 1e-9


def calibrate(repeats: list[dict], *, mix: dict[str, int] | None = None, rate: float | None = None,
              drain: dict[str, float] | None = None, invalid: list[dict] | None = None,
              planned: list[str] | None = None) -> dict:
    """Per (service, cell): k = copy delta / N over two repeats of N requests of
    one cell (N = the k6 answered count, every request answered as expected);
    integral and equal across the repeats, else the cell, and so the service, is
    excluded and the cause named. Same per gateway family (cross-check). With
    ``rate`` and ``drain`` (msg/s per language) the rate cap: rate x the share of
    requests that enqueue (``ok``) <= the drain rate.

    A repeat: {"cell": id, "counts": {expected, unexpected, noanswer},
    "copy": {service: delta}, "gateway": {service: {family: delta}},
    "unset": [services]}.

    ``invalid`` (K-cal-d): repeats the driver kept out (an invalid window, an
    unclean copy gate, an ambiguous count), each with its ``reasons``: their
    cell is excluded, the reasons named. ``planned``: the plan's cell ids; a
    planned cell with no repeat is excluded ("0 repeat(s)")."""
    by_cell: dict[str, list[dict]] = {}
    for rep in repeats:
        by_cell.setdefault(rep["cell"], []).append(rep)
    bad_cells: dict[str, list[str]] = {}
    for rec in invalid or []:
        by_cell.setdefault(rec["cell"], [])
        bad_cells.setdefault(rec["cell"], []).extend(r for r in rec.get("reasons") or ["invalid"]
                                                     if r not in bad_cells.get(rec["cell"], []))
    for cell in planned or []:
        by_cell.setdefault(cell, [])
    services: dict[str, dict] = {}
    families: dict[str, dict[str, dict[str, int]]] = {}
    for cell, reps in by_cell.items():
        lang = cell_tags(cell).get("lang")
        svcs = sorted({s for r in reps for s in list(r.get("copy") or {}) + list(r.get("gateway") or {})}
                      | {f"demo-{lang}-{a}" for a in verify.DEMO_APPS})
        for svc in svcs:
            st = services.setdefault(svc, {"k": {}, "qualified": True, "causes": []})
            ks, cause = [], None
            if bad_cells.get(cell):
                cause = f"{cell}: invalid repeat(s): {'; '.join(bad_cells[cell])}"
            elif len(reps) < 2:
                cause = f"{cell}: {len(reps)} repeat(s), need 2"
            for r in reps if not bad_cells.get(cell) else []:
                n = r["counts"]
                if n.get("noanswer") or n.get("unexpected"):
                    cause = f"{cell}: repeat with {n.get('noanswer', 0)} no answer / {n.get('unexpected', 0)} unexpected"
                    break
                if svc in (r.get("unset") or []):
                    cause = f"{cell}: service_instance_id=\"unset\" series (D4)"
                    break
                N = n["expected"]
                if N <= 0:
                    cause = f"{cell}: N = 0"
                    break
                if svc not in (r.get("copy") or {}):  # review L3: absent is not k = 0
                    cause = f"{cell}: no copy delta for {svc} in a repeat (record 0 explicitly)"
                    break
                ks.append(float(r["copy"][svc]) / N)
            if cause is None:
                if not all(_integral(k) for k in ks):
                    cause = f"{cell}: copy delta / N = {ks} not integral" + _family_cause(reps, svc)
                elif len({round(k) for k in ks}) > 1:
                    cause = f"{cell}: k differs across repeats {[round(k) for k in ks]}" + _family_cause(reps, svc)
            if cause:
                st["qualified"] = False
                st["causes"].append(cause)
            else:
                st["k"][cell] = round(ks[0])
            for fam in sorted({f for r in reps for f in ((r.get("gateway") or {}).get(svc) or {})}):
                fks = [((r.get("gateway") or {}).get(svc) or {}).get(fam, 0.0) / max(r["counts"]["expected"], 1)
                       for r in reps]
                if not bad_cells.get(cell) and len(fks) >= 2 and all(_integral(k) for k in fks) \
                        and len({round(k) for k in fks}) == 1:
                    families.setdefault(svc, {}).setdefault(fam, {})[cell] = round(fks[0])
                else:
                    families.setdefault(svc, {}).setdefault(f"excluded:{fam}", {})[cell] = -1
    out_services = {}
    for svc, st in services.items():
        out_services[svc] = {"qualified": st["qualified"], "k": st["k"],
                             "cause": "; ".join(st["causes"]) if st["causes"] else None}
    fam_out = {svc: {f: v for f, v in fs.items() if not f.startswith("excluded:")} for svc, fs in families.items()}
    excluded = {svc: sorted(f[len("excluded:"):] for f in fs if f.startswith("excluded:"))
                for svc, fs in families.items() if any(f.startswith("excluded:") for f in fs)}
    # A family ambiguous (lower != upper) in any repeat is excluded and named, even
    # when the driver left it out of every repeat's ``gateway`` (K-cal-d re-review M3).
    for rep in repeats:
        for svc, fams in (rep.get("gateway_ambiguous") or {}).items():
            for fam in fams:
                (fam_out.get(svc) or {}).pop(fam, None)
                if fam not in excluded.setdefault(svc, []):
                    excluded[svc] = sorted(excluded[svc] + [fam])
    result = {"services": out_services, "families": fam_out, "excluded_families": excluded}
    if rate is not None and drain:
        m = mix or {"ok": 7, "business_error": 1, "db_error": 1, "unhandled": 1}
        share = m.get("ok", 0) / sum(m.values())
        result["rate_cap"] = {lang: {"enqueue_rate": rate * share, "drain_rate": d,
                                     "verdict": "PASS" if rate * share <= d else "FAIL"}
                              for lang, d in drain.items()}
    return result


def _family_cause(reps: list[dict], svc: str) -> str:
    """Names the gateway families whose per-request count is not integral or
    differs: the likely cause of a non-integral copy total."""
    bad = set()
    fams = {f for r in reps for f in ((r.get("gateway") or {}).get(svc) or {})}
    for fam in fams:
        fks = [((r.get("gateway") or {}).get(svc) or {}).get(fam, 0.0) / max(r["counts"]["expected"], 1) for r in reps]
        if not all(_integral(k) for k in fks) or len({round(k) for k in fks}) > 1:
            bad.add(fam)
    return f" (cause: gateway families {sorted(bad)} vary per request)" if bad else \
        " (cause: no gateway family varies — a span outside the fixed families, or the copy, is suspect)"


# ── K8a: the S-D5 slope rule (2026-11-23 recalibration, K0e-1) ──


def slope_per_hour(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope, units per hour."""
    if len(points) < 2:
        return None
    n = len(points)
    mt = sum(t for t, _ in points) / n
    mv = sum(v for _, v in points) / n
    den = sum((t - mt) ** 2 for t, _ in points)
    if den == 0:
        return None
    return sum((t - mt) * (v - mv) for t, v in points) / den * 3600


def _bins(pts: list[tuple[float, float]], start: float, width: float) -> list[tuple[float, float]]:
    """(bin middle, median) per ``width`` seconds from ``start``."""
    by: dict[int, list[float]] = {}
    for t, v in pts:
        by.setdefault(int((t - start) // width), []).append(v)
    return [(start + (i + 0.5) * width, statistics.median(vs)) for i, vs in sorted(by.items())]


def judge_slope(points: list[tuple[float, float]], start: float, end: float, *,
                median_bins: bool = False) -> dict:
    """PASS when the slope over [start, end] and over its second half are both
    within their limit: 1 % of the span's median per hour, or for a span of
    integers at least INTEGER_Q units over that span or half (owner ruling
    2026-11-23); with ``median_bins`` the fit uses 15 min medians. The ratio of
    the last 15 min median to the first 15 min median is reported only."""
    pts = sorted((float(t), float(v)) for t, v in points if start <= float(t) <= end)
    if len(pts) < 4:
        return {"verdict": "NODATA", "samples": len(pts)}
    med = statistics.median(v for _, v in pts)
    integer = all(v.is_integer() for _, v in pts)
    span_h = (end - start) / 3600
    mid = start + (end - start) / 2

    def limit(hours: float) -> float:
        base = SLOPE_SHARE * abs(med)
        return max(base, INTEGER_Q / hours) if integer and hours > 0 else base

    fit = _bins(pts, start, MEDIAN_BIN_S) if median_bins else pts
    full = slope_per_hour(fit)
    half = slope_per_hour([p for p in fit if p[0] >= mid])
    first = [v for t, v in pts if t < start + 900]
    last = [v for t, v in pts if t > end - 900]
    ratio = (statistics.median(last) / statistics.median(first)
             if first and last and statistics.median(first) else None)
    lim, lim_half = limit(span_h), limit(span_h / 2)
    ok = full is not None and half is not None and full <= lim and half <= lim_half
    return {"verdict": "PASS" if ok else "FAIL", "samples": len(pts), "median": med, "integer": integer,
            "median_bins": median_bins, "limit_per_h": lim, "second_half_limit_per_h": lim_half,
            "slope_per_h": full, "second_half_slope_per_h": half, "ratio_report_only": ratio,
            "ratio_over_1_10": ratio is not None and ratio > RATIO_LIMIT}


def container_id(lang: str, app: str) -> str:
    return f"{lang}-{app}"


def _demo_key(pod: str) -> str | None:
    m = verify.POD_RE.match(pod or "")
    return container_id(m.group(1), m.group(2)) if m else None


def passive_series(r: sp.Reader, start: float, end: float, step: int = 60) -> tuple[dict, list[str]]:
    """rule -> container id -> list of (pod, container, points); and the queries."""
    out: dict[str, dict[str, list]] = {}
    queries = []
    for rule, metric, matchers, _langs, keys in (*PASSIVE_METRICS, CROSSCHECK):
        q = sp.sel(metric, matchers)
        queries.append(f"{q} (range {sp.iso(start)}..{sp.iso(end)}, step {step}s)")
        for s in sp.cluster_only(r.prom_range(q, start, end, step)):
            m = s.get("metric") or {}
            cid = _demo_key(m.get(keys[0], ""))
            if cid is None:
                continue
            pts = [(float(t), float(v)) for t, v in s.get("values") or []]
            out.setdefault(rule, {}).setdefault(cid, []).append((m.get(keys[0]), m.get(keys[1]), pts))
    return out, queries


def fd_points(guard_jsonl: Path) -> dict[str, list[tuple[str, float, float]]]:
    """container id -> (pod, at, FDs) from the ``fd`` records of the loop's
    ``guard.jsonl`` (the pod is kept for the several-pods rule, review M5)."""
    out: dict[str, list] = {}
    if not guard_jsonl.exists():
        return out
    for line in guard_jsonl.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        fd = rec.get("fd") or {}
        at = fd.get("at")
        for cid, c in (fd.get("containers") or {}).items():
            if at is not None and isinstance(c.get("fds"), int):
                out.setdefault(cid, []).append((c.get("pod", ""), float(at), float(c["fds"])))
    return out


def fd_reads(guard_jsonl: Path) -> list[float]:
    """The time of every guard read that ran the FD sweep (an ``fd`` record with
    ``at``), successful or not (re-review HIGH-2)."""
    out = []
    if not guard_jsonl.exists():
        return out
    for line in guard_jsonl.read_text().splitlines():
        try:
            at = (json.loads(line).get("fd") or {}).get("at")
        except (json.JSONDecodeError, AttributeError):
            continue
        if at is not None:
            out.append(float(at))
    return out


PARTIAL_BIN_MIN_S = 600   # a trailing part of an hour this long or longer is its own coverage bin


def coverage_bins(start: float, end: float) -> tuple[int, float, list[float]]:
    """(whole hours from ``start``, the remainder in s, the seconds each coverage
    bin covers) for [start, end]. A remainder of at least ``PARTIAL_BIN_MIN_S``
    (or a span under 1 h) is its own last bin; a shorter one is folded into the
    last whole hour (K8a review 2026-11-25 LOW-1: a 10 h 30 min span left its
    last 30 min out)."""
    span = max(0.0, end - start)
    full = int(span // 3600)
    rem = span - full * 3600
    if rem >= PARTIAL_BIN_MIN_S or full == 0:
        bins = [3600.0] * full + [rem]
    else:
        bins = [3600.0] * full
        bins[-1] += rem
    return full, rem, bins


def fd_shares(points: list, reads: list[float], start: float, end: float) -> dict:
    """FD coverage per hour: successful FD samples of the container / the guard
    reads that ran in that hour (re-review HIGH-2: the loop's period is not a
    fixed 60 s). An hour with no read is not judged here — missing reads are the
    wall-clock heartbeat rule's (a gap invalidates the window)."""
    hours = len(coverage_bins(start, end)[2])
    ran = [0] * hours
    ok = [0] * hours
    for t in reads:
        if start <= t < end:
            ran[min(int((t - start) // 3600), hours - 1)] += 1
    for _pod, t, _v in points:
        if start <= t < end:
            ok[min(int((t - start) // 3600), hours - 1)] += 1
    shares = [round(o / n, 4) if n else None for o, n in zip(ok, ran)]
    total = round(sum(ok) / sum(ran), 4) if sum(ran) else None
    low = [i for i, x in enumerate(shares) if x is not None and x < COVERAGE_MIN]
    return {"hours": shares, "total": total, "reads": ran, "low_hours": low,
            "ok": total is not None and not low and total >= COVERAGE_MIN}


def restarts(r: sp.Reader, start: float, end: float) -> tuple[dict[str, float], str]:
    """container id -> restarts over [start, end] (kube-state-metrics). Series
    are merged per pod first (a kube-state-metrics restart starts new series of
    the same pod, which are not restarts); a demo pod first seen after the start
    replaced one and counts as a restart (review M5)."""
    q = sp.sel("kube_pod_container_status_restarts_total", {"job": "kube-state-metrics", "namespace": sp.APP_NS})
    span = int(end - start) + sp.BORN_GRACE + sp.SCRAPE_S
    pods: dict[tuple[str, str], dict[float, float]] = {}
    for s in sp.cluster_only(r.prom(f"{q}[{span}s]", end)):
        m = s.get("metric") or {}
        cid = _demo_key(m.get("pod", ""))
        if cid is None:
            continue
        merged = pods.setdefault((cid, m.get("pod", "")), {})
        for t, v in s.get("values") or []:
            merged[float(t)] = max(merged.get(float(t), 0.0), float(v))
    out: dict[str, float] = {}
    for (cid, _pod), merged in pods.items():
        pts = sorted(merged.items())
        before = [v for t, v in pts if t <= start][-1:]
        inside = [v for t, v in pts if start < t <= end]
        inc = 0.0
        prev = before[0] if before else (inside[0] if inside else 0.0)
        if not before and inside:
            inc += 1 + inside[0]
        for v in inside[(0 if before else 1):]:
            inc += v - prev if v >= prev else v
            prev = v
        out[cid] = out.get(cid, 0.0) + inc
    return out, f"{q}[{span}s]"


def rule_interval(rule: str, export_interval: float = EXPORT_INTERVAL_S) -> float:
    """The expected seconds between samples of a judged rule's series."""
    if rule in ("rss", "working_set", "threads"):
        return CADVISOR_SCRAPE_S
    return export_interval


def hourly_shares(counts: list[tuple[float, float]], start: float, end: float, interval: float,
                  tail: float | None = None) -> dict:
    """Per bin of [start, end] (``coverage_bins``): samples / (bin seconds /
    interval), and the total. ``counts`` are the whole-hour range points (hour
    end, samples in that hour), never routed past the last whole hour; ``tail``
    is the sample count of the remainder after the last whole hour (an instant
    read at ``end``), added to the last bin."""
    full, _rem, bins = coverage_bins(start, end)
    per = [0.0] * len(bins)
    for t, n in counts:
        i = int(math.ceil((float(t) - start) / 3600 - 1e-6)) - 1
        if 0 <= i < full:
            per[i] += float(n)
    if tail:
        per[-1] += float(tail)
    wants = [b / interval for b in bins]
    shares = [round(n / w, 4) if w else 0.0 for n, w in zip(per, wants)]
    total = round(sum(per) / sum(wants), 4) if sum(wants) else 0.0
    low = [i for i, x in enumerate(shares) if x < COVERAGE_MIN]
    return {"hours": shares, "total": total, "ok": not low and total >= COVERAGE_MIN, "low_hours": low}


def passive_coverage(r: sp.Reader, start: float, end: float, fds: dict, langs,
                     export_interval: float = EXPORT_INTERVAL_S,
                     reads: list[float] | None = None) -> tuple[dict, list[str]]:
    """Coverage inside the judged span (review H4): cid -> rule -> hourly shares,
    from ``count_over_time(<series>[1h])`` per hour (Prometheus), an instant
    ``count_over_time(<series>[<rem>s])`` at ``end`` for the remainder after the
    last whole hour, and the guard-read count per hour (FDs)."""
    out: dict[str, dict] = {}
    queries = []
    full, rem, _bins = coverage_bins(start, end)
    for rule, metric, matchers, rlangs, keys in PASSIVE_METRICS:
        if rlangs is not None and not any(lang in rlangs for lang in langs):
            continue
        q = f"count_over_time({sp.sel(metric, matchers)}[3600s])"
        per: dict[str, list] = {}
        tails: dict[str, float] = {}
        if full:      # a span under 1 h has no whole hour (start + 3600 > end)
            queries.append(f"{q} (range {sp.iso(start + 3600)}..{sp.iso(start + full * 3600)}, step 3600s)")
            for srs in sp.cluster_only(r.prom_range(q, start + 3600, start + full * 3600, 3600)):
                cid = _demo_key((srs.get("metric") or {}).get(keys[0], ""))
                if cid is not None:
                    per.setdefault(cid, []).extend((float(t), float(v)) for t, v in srs.get("values") or [])
        if rem >= 1:
            q_rem = f"count_over_time({sp.sel(metric, matchers)}[{int(rem)}s])"
            queries.append(f"{q_rem} (instant {sp.iso(end)})")
            for srs in sp.cluster_only(r.prom(q_rem, end)):
                cid = _demo_key((srs.get("metric") or {}).get(keys[0], ""))
                val = srs.get("value")
                if cid is not None and val:
                    tails[cid] = tails.get(cid, 0.0) + float(val[1])
        for lang in langs:
            if rlangs is not None and lang not in rlangs:
                continue
            for app in verify.DEMO_APPS:
                cid = container_id(lang, app)
                out.setdefault(cid, {})[rule] = hourly_shares(per.get(cid, []), start, end,
                                                              rule_interval(rule, export_interval),
                                                              tail=tails.get(cid))
    for lang in langs:
        for app in verify.DEMO_APPS:
            cid = container_id(lang, app)
            out.setdefault(cid, {})["fds"] = fd_shares(fds.get(cid) or [], reads or [], start, end)
    return out, queries


def passive_verdicts(series: dict, fds: dict, restart: dict, judged: tuple[float, float], langs,
                     cov: dict | None = None) -> dict:
    """container id -> rule -> verdict (S-D5), restarts 0, FDs from the sweep.
    A judged series below the coverage minimum in any hour or in total makes the
    container INVALID (``platform: telemetry gap``), unless it restarted (FAIL)."""
    start, end = judged
    out: dict[str, dict] = {}
    for lang in langs:
        for app in verify.DEMO_APPS:
            cid = container_id(lang, app)
            rules: dict[str, dict] = {}
            for rule, _m, _x, rlangs, _k in (*PASSIVE_METRICS, CROSSCHECK):
                if rlangs is not None and lang not in rlangs:
                    continue
                found = (series.get(rule) or {}).get(cid) or []
                if not found:
                    rules[rule] = {"verdict": "NODATA"}
                    continue
                pods = sorted({p for p, _c, _pts in found})
                pts = [pt for _p, _c, ps in found for pt in ps]
                v = judge_slope(pts, start, end, median_bins=rule in MEDIAN_BIN_RULES)
                if len(pods) > 1:
                    v.update(verdict="FAIL", reason=f"several pods in the judged span {pods}")
                if rule == "python_fds":
                    v["report_only"] = True
                rules[rule] = v
            fdp = fds.get(cid) or []
            rules["fds"] = judge_slope([(t, v) for _p, t, v in fdp], start, end)
            fd_pods = sorted({p for p, t, _v in fdp if start <= t <= end})
            if len(fd_pods) > 1:
                rules["fds"].update(verdict="FAIL", reason=f"several pods in the judged span {fd_pods}")
            rules["restarts"] = ({"verdict": "PASS" if restart[cid] == 0 else "FAIL", "increase": restart[cid]}
                                 if cid in restart else {"verdict": "NODATA"})
            if lang == "node":
                rules["runtime"] = {"verdict": "n/a", "reason": "no Node.js runtime metrics (AG12)"}
            judged_rules = [k for k, v in rules.items() if not v.get("report_only") and k != "runtime"]
            gaps = []
            for rule, c in ((cov or {}).get(cid) or {}).items():
                if rule in rules:      # always recorded (K8a review 2026-11-25 MEDIUM-2)
                    rules[rule]["coverage"] = c
                if rule in judged_rules and not c["ok"]:
                    gaps.append(f"{rule} total {c['total']}, hours below {COVERAGE_MIN}: {c['low_hours']}")
            if rules["restarts"]["verdict"] == "FAIL":
                verdict = "FAIL"
            elif gaps:
                verdict = "INVALID"
            else:
                verdict = ("FAIL" if any(rules[k]["verdict"] == "FAIL" for k in judged_rules) else
                           "NODATA" if any(rules[k]["verdict"] == "NODATA" for k in judged_rules) else "PASS")
            out[cid] = {"verdict": verdict, "rules": rules}
            if gaps:
                out[cid]["reason"] = "platform: telemetry gap (" + "; ".join(gaps) + ")"
    return out


def coverage(r: sp.Reader, at: float, lookback: int, langs, interval: int = EXPORT_INTERVAL_S) -> dict:
    """The K8a coverage gate over [at - lookback, at]: span metrics grow for every
    demo job, and >= 95 % of the expected export intervals carry a sample of each
    judged runtime series per process (Node.js: none, AG12)."""
    out: dict = {"span_metrics": {}, "runtime": {}, "queries": []}
    start = at - lookback
    q = (f"sum by (cluster, service_name) (increase("
         f"{sp.sel(SPAN_METRIC, {'service_name': ('=~', sp.DEMO_SERVICE_RE)})}[{lookback}s]))")
    out["queries"].append(q)
    grown = {(s.get("metric") or {}).get("service_name"): float(s["value"][1])
             for s in sp.cluster_only(r.prom(q, at))}
    for svc in sp.demo_services(langs):
        out["span_metrics"][svc] = "PASS" if grown.get(svc, 0) > 0 else "FAIL"
    want = lookback / interval
    for rule, metric, matchers, rlangs, keys in (*PASSIVE_METRICS[3:], CROSSCHECK):
        if not any(lang in rlangs for lang in langs):
            continue
        qq = f"count_over_time({sp.sel(metric, matchers)}[{lookback}s])"
        out["queries"].append(qq)
        for s in sp.cluster_only(r.prom(qq, at)):
            m = s.get("metric") or {}
            cid = _demo_key(m.get(keys[0], ""))
            if cid is None:
                continue
            share = round(float(s["value"][1]) / want, 4)
            key = f"{cid}:{rule}"
            if key in out["runtime"]:  # several series of one container: the lowest (review L5)
                share = min(share, out["runtime"][key]["share"])
            out["runtime"][key] = {"share": share, "verdict": "PASS" if share >= COVERAGE_MIN else "FAIL"}
        for lang in langs:
            if lang in rlangs:
                for app in verify.DEMO_APPS:
                    out["runtime"].setdefault(f"{container_id(lang, app)}:{rule}", {"share": 0.0, "verdict": "FAIL"})
    for lang in langs:
        if not RUNTIME_RULES.get(lang):
            out["runtime"][f"{lang}:runtime"] = {"verdict": "n/a", "reason": "AG12"}
    vals = list(out["span_metrics"].values()) + [v["verdict"] for v in out["runtime"].values()]
    out["verdict"] = "PASS" if all(v in ("PASS", "n/a") for v in vals) else "FAIL"
    out["window"] = [sp.iso(start), sp.iso(at)]
    return out


# ── the FD sweep (owner ruling K0e-2; the named exec of SR15 "Checker") ──


def parse_fd_listing(stdout: str) -> tuple[int | None, str | None]:
    """(count, error) from ``ls -1 /proc/1/fd``: one FD number per line. A
    non-numeric line is an error; 0 FDs is implausible (PID 1 holds at least
    its standard streams) and an error too (review H2)."""
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    bad = [ln for ln in lines if not ln.isdigit()]
    if bad:
        return None, f"not an FD listing: {bad[0][:40]!r}"
    if not lines:
        return None, "0 FDs listed: implausible"
    return len(lines), None


class FdSweep:
    """``ls -1 /proc/1/fd`` (no shell) in every Running, non-Terminating demo
    container, in parallel, at every guard read (K-loop's ``fd_reader`` hook).
    ``list_pods`` returns the namespace's pods (``kubectl get pods`` JSON items);
    ``exec_read`` runs FD_ARGV in one container, returns stdout and raises on a
    non-zero exit."""

    def __init__(self, list_pods, exec_read, clock, langs=tuple(verify.LANGS), workers: int = 12):
        self.list_pods, self.exec_read, self.clock = list_pods, exec_read, clock
        self.langs, self.workers = tuple(langs), workers

    def targets(self) -> list[tuple[str, str, str]]:
        out = []
        for p in self.list_pods():
            meta = p.get("metadata") or {}
            m = verify.POD_RE.match(meta.get("name", ""))
            if not m or m.group(1) not in self.langs or sp.terminating(p):
                continue
            if (p.get("status") or {}).get("phase") != "Running":
                continue
            container = ((p.get("spec") or {}).get("containers") or [{}])[0].get("name", m.group(2))
            out.append((container_id(m.group(1), m.group(2)), meta["name"], container))
        return sorted(out)

    def _one(self, target: tuple[str, str, str]) -> tuple[str, dict]:
        cid, pod, container = target
        try:
            n, err = parse_fd_listing(self.exec_read(pod, container))
            if err:
                return cid, {"pod": pod, "container": container, "error": err}
            return cid, {"pod": pod, "container": container, "fds": n}
        except Exception as exc:  # noqa: BLE001 — a failed exec is recorded, never fatal
            return cid, {"pod": pod, "container": container, "error": f"{exc.__class__.__name__}: {str(exc)[:120]}"}

    def read(self) -> dict:
        at = self.clock()
        try:
            targets = self.targets()
        except Exception as exc:  # noqa: BLE001
            return {"at": at, "error": f"pod list: {str(exc)[:120]}", "containers": {}}
        with ThreadPoolExecutor(max_workers=max(1, min(self.workers, len(targets) or 1))) as ex:
            res = dict(ex.map(self._one, targets))
        missing = [container_id(lang, app) for lang in self.langs for app in verify.DEMO_APPS
                   if container_id(lang, app) not in res]
        return {"at": at, "containers": res, "missing": missing}


# ── the report (SR15 "Evidence (r6.2)") ──


class ScanError(ValueError):
    """The report carries a secret pattern, the canary or the fakeotlp token."""


def scan_report(text: str, canary: str | None) -> list[str]:
    hits = verify.scan_secret(text)
    if sp.AUTH_VALUE_RE.search(text):  # review L8
        hits.append("auth value")
    if sp.USERINFO_RE.search(text):
        hits.append("url userinfo")
    if canary and canary in text:
        hits.append("canary")
    if FAKEOTLP_TOKEN in text:
        hits.append("fakeotlp token")
    return hits


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _cnt(v: dict) -> str:
    """A count, or ``lo..hi (ambiguous)`` when an ambiguous reappearance widened it."""
    if v.get("counted_hi") is not None and v["counted_hi"] != v["counted"]:
        return f"{_fmt(v['counted'])}..{_fmt(v['counted_hi'])} (ambiguous: expiry or update)"
    return _fmt(v["counted"])


def render_report(run: str, meta: dict, sections: dict[str, dict]) -> str:
    """Counts only: verdicts, values, exact queries, digests, the commit and tag."""
    lines = [f"# Soak run `{run}`", "",
             f"- Commit: `{meta.get('commit', 'unknown')}`; tag: `{meta.get('tag', 'none')}`",
             f"- Generated: {meta.get('generated', '')} by `verify.py soak report`", ""]
    for d in meta.get("digests") or []:
        lines.append(f"- Image: `{d}`")
    if meta.get("digests"):
        lines.append("")
    for name in ("passive", "gate", "count", "shut", "cal"):
        sec = sections.get(name)
        if not sec:
            continue
        lines += [f"## {name}", ""]
        if sec.get("window"):
            lines += [f"Window: {sec['window']}", ""]
        rows = sec.get("rows") or []
        if rows:
            lines += ["| Item | Verdict | Values |", "|---|---|---|"]
            for item, verdict, values in rows:
                lines.append(f"| {item} | {verdict} | {values} |")
            lines.append("")
        for f in sec.get("findings") or []:
            lines.append(f"- Finding: {f}")
        if sec.get("findings"):
            lines.append("")
        if sec.get("queries"):
            lines += ["Queries:", ""]
            lines += [f"- `{q}`" for q in sec["queries"]]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def rows_for(name: str, data: dict) -> dict:
    """The report section of one action's JSON."""
    rows, findings = [], list(data.get("findings") or [])
    if name == "passive":
        for cid, c in sorted((data.get("containers") or {}).items()):
            for rule, v in sorted(c["rules"].items()):
                vals = ", ".join(f"{k} {_fmt(v[k])}" for k in ("samples", "median", "slope_per_h",
                                                                "second_half_slope_per_h", "limit_per_h",
                                                                "ratio_report_only", "increase") if v.get(k) is not None)
                rows.append((f"{cid} {rule}", v["verdict"], vals or v.get("reason", "")))
    elif name == "gate":
        for svc, v in sorted((data.get("span_metrics") or {}).items()):
            rows.append((f"span metrics {svc}", v, ""))
        for key, v in sorted((data.get("runtime") or {}).items()):
            rows.append((f"runtime {key}", v["verdict"], f"share {v.get('share', v.get('reason', ''))}"))
    elif name == "count":
        for svc, v in sorted((data.get("services") or {}).items()):
            rows.append((f"{svc} copy", v["verdict"],
                         f"counted {_cnt(v)} in [{_fmt(v.get('lo', '-'))}, {_fmt(v.get('hi', '-'))}]"
                         + (f"; unexpected {v['unexpected']}" if v.get("unexpected") is not None else "")
                         + (f"; {v['reason']}" if v.get("reason") else "")))
            if v.get("finding"):
                findings.append(f"{svc}: {v['finding']}")
            for fam, g in sorted((v.get("gateway") or {}).items()):
                rows.append((f"{svc} gateway {fam}", g["verdict"],
                             f"counted {_cnt(g)} in [{_fmt(g.get('lo', '-'))}, {_fmt(g.get('hi', '-'))}]"))
        for lang, v in sorted((data.get("drain") or {}).items()):
            rows.append((f"drain {lang}", v["verdict"], f"send {_fmt(v['send'])}, process {_fmt(v['process'])}"))
    elif name == "shut":
        for api, v in sorted((data.get("apis") or {}).items()):
            rows.append((f"{api} {v['service_instance_id']}", v["verdict"],
                         f"counted {_cnt(v)} in [{_fmt(v.get('lo', '-'))}, {_fmt(v.get('hi', '-'))}]"
                         + f"; count {v.get('count_verdict')}"
                         + (f"; exit {v['exit']['verdict']} {_fmt(v['exit'].get('seconds', '-'))}s"
                            f" (grace {_fmt(v['exit'].get('grace_s', '-'))}s, code {v['exit'].get('exit_code')})"
                            if v.get("exit") else "")
                         + (f"; {v['reason']}" if v.get("reason") else "")))
    elif name == "cal":
        for svc, v in sorted((data.get("services") or {}).items()):
            rows.append((f"{svc}", "QUALIFIED" if v["qualified"] else "EXCLUDED",
                         v.get("cause") or ", ".join(f"{c} k={k}" for c, k in sorted(v["k"].items()))))
        for lang, v in sorted((data.get("rate_cap") or {}).items()):
            rows.append((f"rate cap {lang}", v["verdict"],
                         f"enqueue {_fmt(v['enqueue_rate'])}/s vs drain {_fmt(v['drain_rate'])}/s"))
    return {"rows": rows, "findings": findings, "queries": data.get("queries") or [], "window": data.get("window")}


def write_report(path: Path, text: str, canary: str | None) -> None:
    hits = scan_report(text, canary)
    if hits:
        raise ScanError(f"report not written: secret scan hit {sorted(set(hits))}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ── the CLI ──


def _write_json(d: Path, name: str, data: dict) -> Path:
    text = json.dumps(data, indent=1, sort_keys=True, default=str) + "\n"
    if verify.scan_secret(text):
        raise ScanError(f"{name}: secret scan hit, not written")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"soak-{name}.json"
    p.write_text(text)
    return p


def _load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def main_soak(b: verify.Backends, args, now: float, reader: sp.Reader | None = None) -> int:
    r = reader or sp.Reader(b, args.context)
    d = args.out_dir or (sp.RUNS_DIR / args.run)
    langs = tuple(args.langs)
    missing = {"passive": ("start",), "count": ("t1", "t2", "k6_log", "kcal"),
               "shut": ("input",), "cal": ("input",)}.get(args.action, ())
    absent = [f"--{m.replace('_', '-')}" for m in missing if getattr(args, m, None) is None]
    if absent:  # review L6
        print(f"verify soak {args.action}: required {' '.join(absent)}", file=sys.stderr)
        return 2
    try:
        if args.action == "passive":
            data = action_passive(r, args, langs, d)
            ok = all(c["verdict"] == "PASS" for c in data["containers"].values())
        elif args.action == "gate":
            data = coverage(r, now, args.lookback, langs, args.export_interval)
            ok = data["verdict"] == "PASS"
        elif args.action == "count":
            data = action_count(r, args, langs)
            ok = all(v["verdict"] == "PASS" for v in data["services"].values()) and \
                all(v["verdict"] != "FAIL" for v in data["drain"].values())
        elif args.action == "shut":
            data = action_shut(r, args)
            ok = all(v["verdict"] in ("PASS", "REPORT-ONLY") for v in data["apis"].values())
        elif args.action == "cal":
            inp = _load_json(args.input)
            data = calibrate(inp["repeats"], mix=inp.get("mix"), rate=args.rate, drain=inp.get("drain"),
                             invalid=inp.get("invalid"), planned=inp.get("planned"))
            ok = all(v["qualified"] for v in data["services"].values())
        else:
            return action_report(args, d)
    except (verify.QueryError, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"verify soak {args.action}: ERROR {exc}", file=sys.stderr)
        return 1
    path = _write_json(d, args.action, data)
    for item, verdict, values in rows_for(args.action, data)["rows"]:
        print(f"{verdict:<12} {item}: {values}", flush=True)
    print(f"verify soak {args.action}: {'PASS' if ok else 'FAIL'} ({path})", flush=True)
    return 0 if ok else 1


def action_passive(r: sp.Reader, args, langs, d: Path) -> dict:
    start = float(args.start)
    judged = (start + args.warmup, start + args.warmup + args.judged)
    series, queries = passive_series(r, judged[0], judged[1])
    rest, rq = restarts(r, start, judged[1])
    fds = fd_points(d / "guard.jsonl")
    cov, cq = passive_coverage(r, judged[0], judged[1], fds, langs, args.export_interval,
                               fd_reads(d / "guard.jsonl"))
    out = {"window": f"warm-up {sp.iso(start)}..{sp.iso(judged[0])}, judged {sp.iso(judged[0])}..{sp.iso(judged[1])}",
           "containers": passive_verdicts(series, fds, rest, judged, langs, cov), "queries": queries + cq + [rq],
           "fd_source": "guard.jsonl exec sweep (ls -1 /proc/1/fd, lines counted)"}
    return out


def _read_cells(k6_log: Path) -> dict[str, dict[str, int]]:
    summary, _ = k6script.parse_logs(Path(k6_log).read_text())
    if summary is None:
        raise ValueError("no k6 summary between the markers")
    return cell_table(k6script.cell_counts(summary))


def _copy_gate(r: sp.Reader, t1: float, t2: float) -> tuple[dict, list]:
    """The copy's own gate over [t1, t2]: its sentinels, its queue 0 at t1 and t2."""
    sent = sp.read_sentinels(r, t2, (t1,), copy_idle=False)[0]
    extra = {"copy:queue t1": queue_check(r, sp.COPY_JOB, t1, zero=True),
             "copy:queue t2": queue_check(r, sp.COPY_JOB, t2, zero=True)}
    return extra, sent


def action_count(r: sp.Reader, args, langs) -> dict:
    t1, t2 = float(args.t1), float(args.t2)
    cells = _read_cells(args.k6_log)
    kcal = _load_json(args.kcal)
    workers = _load_json(args.workers) if args.workers else {}
    services = sp.demo_services(langs)
    qc, copy_series = raw_read(r, FIXED_METRIC, {"job": sp.COPY_JOB, "service_name": ("=~", sp.DEMO_SERVICE_RE)},
                               t1, t2, COPY_EXPIRY_S)
    qg, gw_series = raw_read(r, SPAN_METRIC, {"service_name": ("=~", sp.DEMO_SERVICE_RE)}, t1, t2, GATEWAY_EXPIRY_S)
    copy = copy_counts(copy_series, t1, t2)
    fams = gateway_families(gw_series, t1, t2)
    extra, sent = _copy_gate(r, t1, t2)
    # The gateway pods are read now, not at t2 (review L4): a gateway restart after
    # t2 sets a startedAt after t1 and makes the cross-check UNQUALIFIED — the
    # conservative side; run `count` soon after t2.
    pods = verify.kubectl_json(r.context, ["pods", "-n", "opentelemetry"]).get("items") or []
    demo = verify.kubectl_json(r.context, ["pods", "-n", sp.APP_NS]).get("items") or []
    upt = sp.cluster_only(r.prom(f"{sp.sel('otelcol_process_uptime', {'job': sp.GATEWAY_JOB})}[{int(t2 - t1)}s]", t2))
    extra.update({
        "shared:agent queue t2": queue_check(r, sp.AGENT_JOB, t2, zero=False),
        "shared:gateway queue t2": queue_check(r, sp.GATEWAY_JOB, t2, zero=False),
        "shared:gateway restart": gateway_restart(pods, upt, t1, t2),
    })
    gates = scoped_gates(sent, extra)
    window = window_problems(workers, worker_state(demo, langs), t1, t2, quiesce=args.quiesce,
                             interval=args.worker_interval, langs=langs)
    verdicts = count_verdicts(copy, fams, cells, kcal, gates, services, window)
    drain = drain_check(fams, langs, cells)
    for lang, v in drain.items():
        if not gates["shared"]["clean"] or window.get(lang):
            drain[lang] = {**v, "verdict": "UNQUALIFIED"}
    findings = [f"{s}: unset service_instance_id in the window" for s, c in copy.items() if c.unset]
    findings += [f"{s}: unset service_instance_id outside the window" for s, c in copy.items()
                 if c.unset_outside and not c.unset]
    findings += [f"window {lang}: {'; '.join(p)}" for lang, p in window.items() if p]
    return {"window": f"({sp.iso(t1)}, {sp.iso(t2)}]", "services": verdicts, "drain": drain, "gates": gates,
            "worker_window": window, "copy_source": FIXED_METRIC, "gateway_source": SPAN_METRIC,
            "findings": findings, "queries": [qc, qg]}


def api_grace(r: sp.Reader, api: str) -> float | None:
    """``terminationGracePeriodSeconds`` of the api's Deployment pod template (review H3)."""
    try:
        dep = verify.kubectl_json(r.context, ["deployment", api, "-n", sp.APP_NS])
    except verify.QueryError:
        return None
    g = ((((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("terminationGracePeriodSeconds"))
    return float(g) if g is not None else None


def action_shut(r: sp.Reader, args) -> dict:
    inp = _load_json(args.input)
    t1, t2 = float(inp["t1"]), float(inp["t2"])
    cells = _read_cells(inp["k6_log"])
    kcal = _load_json(inp["kcal"])
    q, series = raw_read(r, FIXED_METRIC, {"job": sp.COPY_JOB, "service_name": ("=~", sp.DEMO_SERVICE_RE)},
                         t1, t2, COPY_EXPIRY_S)
    copy = copy_counts(series, t1, t2)
    extra, sent = _copy_gate(r, t1, t2)
    gate = scoped_gates([v for v in sent if v.scope == "copy"], extra)["copy"]
    apis = {}
    for api, a in inp["apis"].items():
        cal = (kcal.get("services") or {}).get(api) or {}
        c = copy.get(api) or CopyCount(api)
        counted = Counted(c.instances.get(a["old_instance"], 0.0), c.instances_hi.get(a["old_instance"], 0.0))
        apis[api] = shut_verdict(a["old_instance"], counted, cells, cal.get("k") if cal.get("qualified") else None,
                                 service_lang(api), post_before_finish=a.get("post_before_finish"),
                                 deleted_at=a.get("deleted_at"), finished_at=a.get("finished_at"),
                                 grace_s=api_grace(r, api), exit_code=a.get("exit_code"), copy_gate=gate)
    return {"window": f"({sp.iso(t1)}, {sp.iso(t2)}]", "apis": apis, "copy_gate": gate, "queries": [q]}


def action_report(args, d: Path) -> int:
    sections = {}
    for name in ("passive", "gate", "count", "shut", "cal"):
        p = d / f"soak-{name}.json"
        if p.exists():
            sections[name] = rows_for(name, _load_json(p))
    snap = d / "snapshot.json"
    digests = []
    if snap.exists():
        digests = sorted({str(v) for v in _walk(_load_json(snap)) if isinstance(v, str) and sp.IMAGE_DIGEST_RE.match(v)})
    canary_file = d / "canary"
    canary = canary_file.read_text().strip() if canary_file.exists() else None
    meta = {"commit": sp._git_head(), "tag": args.tag or f"soak/{args.run}", "digests": digests,
            "generated": sp.iso(time.time())}
    text = render_report(args.run, meta, sections)
    path = args.report or (REPORTS_DIR / f"{args.run}.md")
    try:
        write_report(path, text, canary)
    except ScanError as exc:
        print(f"verify soak report: {exc}", file=sys.stderr)
        return 1
    print(f"verify soak report: written {path} ({len(sections)} sections)", flush=True)
    return 0


def _walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


def add_parser(sub) -> None:
    """The ``soak`` sub-command of verify.py."""
    p = sub.add_parser("soak", help="soak K-count: passive, count, shut, cal and report verdicts (read-only)")
    p.add_argument("action", choices=("passive", "gate", "count", "shut", "cal", "report"))
    p.add_argument("--run", required=True, help="soak run id (directory under testing/k8s/soak/runs/)")
    p.add_argument("--out-dir", type=Path, default=None, help="default testing/k8s/soak/runs/<run>/")
    p.add_argument("--start", type=float, help="passive: window start (epoch s), required")
    p.add_argument("--warmup", type=verify._duration, default=verify._duration("12h"))
    p.add_argument("--judged", type=verify._duration, default=verify._duration("18h"))
    p.add_argument("--lookback", type=verify._duration, default=verify._duration("30m"), help="gate lookback")
    p.add_argument("--export-interval", type=int, default=EXPORT_INTERVAL_S,
                   help=f"gate: the apps' metric export interval in s (default {EXPORT_INTERVAL_S}, demo.sh)")
    p.add_argument("--t1", type=float, help="count: window start (epoch s)")
    p.add_argument("--t2", type=float, help="count: window end (epoch s)")
    p.add_argument("--k6-log", type=Path, help="count: the saved kubectl logs of the k6 Job")
    p.add_argument("--kcal", type=Path, help="count: soak-cal.json")
    p.add_argument("--input", type=Path, help="shut / cal: input JSON")
    p.add_argument("--workers", type=Path, default=None,
                   help="count: JSON {lang: {startedAt, first_drive, restartCount}} recorded at the window start")
    p.add_argument("--quiesce", type=float, default=QUIESCE_S, help="count: the quiesce after the last traffic (s)")
    p.add_argument("--worker-interval", type=float, default=10800.0,
                   help="count: the workers' DEMO_INTERVAL_SECONDS in the window (default 10800)")
    p.add_argument("--rate", type=float, default=None, help="cal: loaded rate per language (req/s)")
    p.add_argument("--tag", default=None, help="report: the soaked commit's tag (default soak/<run>)")
    p.add_argument("--report", type=Path, default=None, help="report: default specs/soak-simulation/reports/<run>.md")
