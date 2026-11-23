"""The guard read of the soak run loop (specs/soak-simulation task K-loop): a
thin adapter over K1's ``testing/k8s/soakplatform.py``, which owns the guard
logic — the mesh and platform guard (a)-(h) (``soakplatform.guard``), the
scoped sentinels (``read_sentinels``), the telemetry edges (``read_edges``),
the cluster-safe query builder (``sel``) and the K-D13 floors. This module turns
those verdicts into the loop's item states and adds what a 60 s read needs and
a one-shot snapshot does not:

  ok       nothing wrong;
  breach   the window is invalid (``platform: mesh`` for a guard (a)-(h) item,
           ``platform: guard`` otherwise) and the load is stopped;
  finding  report-only — a non-telemetry mesh edge, a copy sentinel (it gates
           the copy verdicts, judged by K-count), a shared sentinel in a
           passive window (K0e B1 scoping, owner ruling M13 2026-11-23);
  na       not applicable now;
  blind    the look-up failed. A read is blind when every look-up failed or a
           core look-up (the pod list or the guard itself) failed (review M3);
           a blind read stops the load (SR15 Orchestration).

Reads: ``entry_read`` is K1's entry gate (30 min lookback, K0e B1 + item (e))
without (h); ``wait_demo_ready`` polls (g) after the hook's roll, skipping
Terminating pods (review B1); ``wait_copy_counts`` is (h) as the increase since
the hook (M7); ``window_read`` runs K1's guard every 60 s over twice the
interval without (g) and (h) (K0e M2), compares every roll-list pod with the
previous read — presence, ``restartCount``, a running container (M8) — and
exempts the requested copy rolls inside their time bound (K0e M1, review L5).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import soakplatform as sp  # noqa: E402
import verify  # noqa: E402

ReadError = verify.QueryError
# soakplatform.guard item names that belong to the mesh guard (K-D18).
MESH_PREFIX = "guard ("
FLOOR_KEYS = ("mem_available_bytes", "idle_cpu_share", "rootfs_free_share")
# Items whose blindness makes the whole read blind (review M3).
CORE_ITEMS = ("pods", "guard")
# A K1 snapshot older than this is not used for the floors (review M14).
SNAPSHOT_MAX_AGE_S = 48 * 3600
COPY_ROLE = "soak-agent"


@dataclass
class GuardConfig:
    lookback_s: int = 1800    # (e) and the B1 entry gate, initial, unmeasured
    ready_s: int = 600        # (g), initial, unmeasured
    ready_margin_s: int = 600  # rollout time allowed on top of ready_s after the hook
    copy_count_s: int = 60    # (h), initial, unmeasured
    interval_s: int = 60
    langs: tuple[str, ...] = tuple(verify.LANGS)
    floors: dict[str, float] = field(default_factory=lambda: dict(sp.INITIAL_FLOORS))
    snapshot: str | None = None  # the K1 snapshot the floors came from

    @classmethod
    def from_snapshot(cls, path: Path, now: float) -> "GuardConfig":
        """Floors from a passing K1 ``snapshot.json`` no older than 48 h; any
        other file (missing, unreadable, FAIL, not K1, stale) leaves the
        initial floors. ``snapshot`` names the file used, or why none was."""
        cfg = cls()
        try:
            snap = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            cfg.snapshot = f"none ({path}: {exc.__class__.__name__})"
            return cfg
        taken = sp.parse_ts(snap.get("taken_at"))
        why = ("not a K1 snapshot" if snap.get("task") != "K1" else
               f"verdict {snap.get('verdict')}" if snap.get("verdict") != "PASS" else
               "no taken_at" if taken is None else
               f"older than {SNAPSHOT_MAX_AGE_S // 3600} h" if now - taken > SNAPSHOT_MAX_AGE_S else "")
        if why:
            cfg.snapshot = f"none ({path}: {why})"
            return cfg
        for key in FLOOR_KEYS:
            value = ((snap.get("floors") or {}).get(key) or {}).get("floor")
            if isinstance(value, (int, float)):
                cfg.floors[key] = max(float(value), sp.INITIAL_FLOORS[key])
        cfg.snapshot = str(path)
        return cfg


@dataclass
class Item:
    name: str
    state: str          # ok | breach | finding | na | blind
    detail: str = ""


@dataclass
class GuardRead:
    at: float
    items: list[Item]

    @property
    def blind(self) -> bool:
        if not self.items:
            return False
        return (all(i.state == "blind" for i in self.items)
                or any(i.state == "blind" and i.name in CORE_ITEMS for i in self.items))

    @property
    def breaches(self) -> list[Item]:
        return [i for i in self.items if i.state == "breach"]

    @property
    def findings(self) -> list[Item]:
        return [i for i in self.items if i.state == "finding"]

    def reason(self) -> str:
        first = self.breaches[0]
        kind = "mesh" if first.name.startswith(MESH_PREFIX) else "guard"
        return f"platform: {kind} ({first.name}: {first.detail})"

    def as_json(self) -> dict:
        return {"at": self.at, "blind": self.blind,
                "items": [{"name": i.name, "state": i.state, "detail": i.detail} for i in self.items]}


class QuietReport(sp.PlatformReport):
    """Collects ``soakplatform``'s checks as items instead of printing them."""

    def __init__(self) -> None:
        super().__init__()
        self.collected: list[Item] = []

    def check(self, ok: bool, name: str, detail: str) -> bool:
        self.items[name] = "PASS" if ok else "FAIL"
        self.collected.append(Item(name, "ok" if ok else "breach", detail))
        return ok

    def error(self, name: str, exc: Exception) -> bool:
        self.collected.append(Item(name, "blind", f"query error: {exc}"))
        return False

    def na(self, name: str, detail: str) -> None:
        self.collected.append(Item(name, "na", detail))

    @staticmethod
    def info(name: str, detail: str) -> None:
        return None


def floors(r: sp.Reader, cfg: GuardConfig, at: float) -> list[Item]:
    """K-D13: the node's current free memory, idle CPU share and root fs share."""
    queries = {
        "mem_available_bytes": sp.sel("node_memory_MemAvailable_bytes", {"job": "node-exporter"}),
        "idle_cpu_share": f"avg by (cluster, instance) (rate("
                          f"{sp.sel('node_cpu_seconds_total', {'job': 'node-exporter', 'mode': 'idle'})}[5m]))",
        "rootfs_free_share": f"{sp.sel('node_filesystem_avail_bytes', {'job': 'node-exporter', 'mountpoint': '/'})}"
                             f" / {sp.sel('node_filesystem_size_bytes', {'job': 'node-exporter', 'mountpoint': '/'})}",
    }
    out = []
    for key, q in queries.items():
        name = f"floor {key}"
        try:
            res = sp.cluster_only(r.prom(q, at))
        except ReadError as exc:
            out.append(Item(name, "blind", str(exc)))
            continue
        if not res:
            out.append(Item(name, "blind", "no series"))
            continue
        value = min(float(s["value"][1]) for s in res)
        floor = cfg.floors[key]
        out.append(Item(name, "ok" if value >= floor else "breach", f"{value:.4g} (floor {floor:.4g})"))
    return out


# ── roll-list pods between reads (review M8, L5) ───────────────────────────

def roll_pods(pods: list[dict]) -> dict[str, dict[str, int]]:
    """``role ns/name`` -> {container: restartCount} of every Running roll-list pod."""
    out = {}
    for ns, rx, role in sp.ROLL_LIST:
        for p in sp.match_pods(pods, ns, rx):
            if (p.get("status") or {}).get("phase") != "Running" or sp.terminating(p):
                continue
            out[f"{role} {ns}/{p['metadata']['name']}"] = {
                c["name"]: int(c.get("restartCount") or 0) for c in sp.container_states(p)}
    return out


@dataclass
class RollState:
    """Roll-list pods of the previous read and the window's copy-roll
    exemptions (the two back-pressure flips, valid only inside
    [exempt_from, exempt_until])."""
    exempt_copy_rolls: int = 0
    exempt_from: float = 0.0
    exempt_until: float = 0.0
    exempted: set[str] = field(default_factory=set)
    previous: dict[str, dict[str, int]] | None = None
    proxied_prev: frozenset = frozenset()   # guard (a): last read's annotated Running pods
    a_mismatch_prev: bool = False           # guard (a): the last read already mismatched

    def exempt_open(self, at: float) -> bool:
        return self.exempt_copy_rolls > 0 and self.exempt_from <= at <= self.exempt_until


def _compare_roll(now: dict[str, dict[str, int]], roll: RollState, at: float) -> list[Item]:
    """Every pod of the previous read still present, none restarted; a copy
    pod change is exempt while an exemption is open."""
    items = []
    if roll.previous is not None:
        problems, copy_change = [], False
        for key, conts in roll.previous.items():
            cur = now.get(key)
            if cur is None:
                if key.startswith(COPY_ROLE + " "):
                    copy_change = True
                else:
                    problems.append(f"{key} gone")
                continue
            for c, n in cur.items():
                if n > conts.get(c, n):
                    if key.startswith(COPY_ROLE + " "):
                        copy_change = True
                    else:
                        problems.append(f"{key}/{c} restartCount {conts.get(c)} -> {n}")
        if copy_change and not roll.exempt_open(at):
            problems.append("soak-agent pod replaced or restarted")
        items.append(Item("guard (d) roll list vs previous read", "breach" if problems else "ok",
                          "; ".join(problems) or f"{len(now)} pods unchanged"))
    roll.previous = now
    return items


def _platform(r: sp.Reader, cfg: GuardConfig, at: float, lookback: int, *, expect_copy: bool,
              roll: RollState | None = None) -> tuple[list[Item], dict, list, list]:
    """``soakplatform.guard`` over ``lookback``; returns (items, its out dict,
    sentinel verdicts, report-only failing edges)."""
    rep = QuietReport()
    try:
        pods = r.kget(["pods", "-A"]).get("items") or []
    except ReadError as exc:
        return [Item("pods", "blind", str(exc))], {}, [], []
    entries = [sp.roll_entry(pods, ns, rx, role) for ns, rx, role in sp.ROLL_LIST]
    items = []
    for e in entries:
        if e["pods"]:
            continue
        exempt = roll is not None and e["role"] == COPY_ROLE and roll.exempt_open(at)
        items.append(Item(f"guard (d) roll list {e['role']} present", "ok" if exempt else "breach",
                          "no Running copy pod: a requested copy roll in progress" if exempt else "no Running pod"))
    if roll is not None:
        items += _compare_roll(roll_pods(pods), roll, at)
    start = at - lookback
    try:
        copy_idle = not expect_copy and not sp.cluster_only(r.prom(sp.sel(
            "otel_libs_soak_spans_total", {"job": sp.COPY_JOB, "service_name": ("=~", sp.DEMO_SERVICE_RE)}), at))
        sent = sp.read_sentinels(r, at, (start,), copy_idle=copy_idle)[0]
        edges, others = sp.read_edges(r, at, (start,))
        opts = sp.Options(run="soak", lookback=lookback, ready_settle=cfg.ready_s, expect_copy=expect_copy,
                          langs=cfg.langs, previous_proxied=roll.proxied_prev if roll else frozenset())
        out = sp.guard(r, rep, opts, at, pods, entries, sent, edges[0])
        if roll is not None:
            roll.proxied_prev = frozenset((out.get("a") or {}).get("running_keys") or ())
    except ReadError as exc:
        return items + rep.collected + [Item("guard", "blind", str(exc))], {}, [], []
    if others:
        items.append(Item("guard (b) other edges", "finding",
                          "; ".join(f"{e['source_workload_namespace']}/{e['source_workload']} -> "
                                    f"{e['destination_workload_namespace']}/{e['destination_workload']} "
                                    f"+{e['increase']:g}" for e in others[:5])))
    return items + rep.collected, out, sent, others


A_ITEM = "guard (a) ztunnel proxies"


def entry_read(r: sp.Reader, cfg: GuardConfig, at: float, *, expect_copy: bool = False,
               clock: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None) -> GuardRead:
    """Pre-flight and entry gate: K1's gate (every guard and sentinel counter
    clean over the 30 min lookback, (a)-(g) now) plus the floors. A breach does
    not start the window and points to the platform session. (h) is judged by
    ``wait_copy_counts`` on the run's services, after the hook. With ``sleep``,
    a guard (a) mismatch is read again one interval later (more than one
    scrape) and only a mismatch in both reads counts (re-review HIGH-1)."""
    roll = RollState()
    items, _, _, _ = _platform(r, cfg, at, cfg.lookback_s, expect_copy=expect_copy, roll=roll)
    if sleep is not None and any(i.name == A_ITEM and i.state == "breach" for i in items):
        sleep(cfg.interval_s)
        at = clock() if clock else at + cfg.interval_s
        items, _, _, _ = _platform(r, cfg, at, cfg.lookback_s, expect_copy=expect_copy, roll=roll)
    items = [i for i in items if not i.name.startswith(("guard (h)", "guard (d) roll list vs previous"))]
    return GuardRead(at, floors(r, cfg, at) + items)


def _scoped_sentinels(sent: list, loaded: bool) -> list[Item]:
    noisy = [v for v in sent if v.verdict in ("NOISY", "ABSENT")]
    shared = [v for v in noisy if v.scope == "shared"]
    copy = [v for v in noisy if v.scope != "shared"]
    out = []
    if shared:
        out.append(Item("sentinels shared", "breach" if loaded else "finding",
                        ", ".join(f"{v.id} {v.verdict} +{v.increase:g}" for v in shared)))
    if copy:
        out.append(Item("sentinels copy", "finding", ", ".join(f"{v.id} {v.verdict} +{v.increase:g}" for v in copy)))
    if not out:
        out.append(Item("sentinels", "ok", f"{len(sent)} clean"))
    return out


def window_read(r: sp.Reader, cfg: GuardConfig, roll: RollState, at: float, *, loaded: bool) -> GuardRead:
    """The read inside a window, every ``interval_s``."""
    items, out, sent, _ = _platform(r, cfg, at, 2 * cfg.interval_s, expect_copy=False, roll=roll)
    kept = []
    for item in items:
        if item.name.startswith(("guard (g)", "guard (h)", "gate sentinels")):
            continue
        if item.name == "guard (d,e) roll list unchanged" and item.state == "breach":
            item = _exempt(item, out.get("d") or [], roll, at)
        if item.name == A_ITEM:
            first = item.state == "breach" and not roll.a_mismatch_prev
            roll.a_mismatch_prev = item.state == "breach"
            if first:  # one read can race the scrape: the next read (60 s later) decides
                item = Item(item.name, "finding", item.detail + "; first mismatch, the next read decides")
        kept.append(item)
    if sent:
        kept += _scoped_sentinels(sent, loaded)
    return GuardRead(at, floors(r, cfg, at) + kept)


def _exempt(item: Item, young: list[str], roll: RollState, at: float) -> Item:
    """A copy roll ("soak-agent started <t>" or a copy container not running
    during the roll) is exempt while an exemption is open; the same start seen
    again in the overlapping next read is not counted twice."""
    if not young or any(not y.startswith(COPY_ROLE + " ") for y in young):
        return item
    if not roll.exempt_open(at) and not all(y in roll.exempted for y in young):
        return item
    new = [y for y in young if y not in roll.exempted and " started " in y]
    if len(new) > roll.exempt_copy_rolls:
        return item
    roll.exempt_copy_rolls -= len(new)
    roll.exempted.update(new)
    return Item(item.name, "ok", f"exempt copy roll ({'; '.join(young)}); "
                                 f"{roll.exempt_copy_rolls} exemption(s) left")


# ── window start: (g) after the hook's roll, (h) as an increase ────────────

def wait_demo_ready(r: sp.Reader, cfg: GuardConfig, clock: Callable[[], float],
                    sleep: Callable[[float], None], step_s: float = 30) -> Item:
    """(g) after the hook rolled the demo (review B1): poll until every
    non-Terminating demo pod of the run's languages is Ready for ``ready_s``,
    at most ``ready_s + ready_margin_s``."""
    name = "guard (g) demo pods Ready after the hook"
    deadline = clock() + cfg.ready_s + cfg.ready_margin_s
    want = len(sp.demo_services(cfg.langs))
    while True:
        try:
            pods = r.kget(["pods", "-A"]).get("items") or []
            demo, bad = sp.demo_pods_check(pods, cfg.langs, clock(), cfg.ready_s)
            if len(demo) != want:
                bad = bad + [f"{len(demo)} of {want} demo pods"]
            item = Item(name, "breach" if bad else "ok", "; ".join(bad) or f"{want} pods Ready for >= {cfg.ready_s}s")
        except ReadError as exc:
            item = Item(name, "blind", str(exc))
        if item.state == "ok" or clock() >= deadline:
            return item
        sleep(step_s)


def copy_totals(r: sp.Reader, at: float) -> dict[str, float]:
    """``service_name`` -> the copy's cumulative span count now."""
    q = f"sum by (cluster, service_name) ({sp.sel('otel_libs_soak_spans_total', {'job': sp.COPY_JOB})})"
    return {(s.get("metric") or {}).get("service_name"): float(s["value"][1])
            for s in sp.cluster_only(r.prom(q, at))}


def copy_counts(r: sp.Reader, services: list[str], at: float, since: dict[str, float] | None = None) -> Item:
    """(h) one read: every ``service_name`` counted more spans than at the hook
    (``since``, the totals read just before it; review M7)."""
    name = "guard (h) copy counts"
    try:
        now = copy_totals(r, at)
    except ReadError as exc:
        return Item(name, "blind", str(exc))
    base = since or {}
    missing = [s for s in services if now.get(s, 0.0) <= base.get(s, 0.0)]
    return Item(name, "breach" if missing else "ok",
                ("no increase since the hook: " + ", ".join(missing)) if missing
                else f"{len(services)} services counted since the hook")


def wait_copy_counts(r: sp.Reader, cfg: GuardConfig, services: list[str], clock: Callable[[], float],
                     sleep: Callable[[float], None], since: dict[str, float] | None = None,
                     step_s: float = 10) -> Item:
    """(h) at a window's start only (K0e M2): poll until every service counts,
    at most ``copy_count_s``."""
    deadline = clock() + cfg.copy_count_s
    while True:
        item = copy_counts(r, services, clock(), since)
        if item.state == "ok" or clock() >= deadline:
            return item
        sleep(step_s)
