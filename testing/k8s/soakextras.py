"""``verify.py soak extras``: the K8b checks ``soak count`` does not cover
(specs/soak-simulation task K8b r6.2, design K-D21 draft).

Read-only on the cluster (``kubectl get`` / ``kubectl logs``, Prometheus, Loki
and Tempo reads through ``verify.Backends``). One JSON, ``soak-extras.json``,
under the run directory (or ``--out-dir``):

  graph     api -> backend service graph per language, raw samples of
            ``traces_service_graph_request_total`` over (t1, t_read], t_read the
            first connector flush after k6's end + the store TTL + 2 batch
            timeouts (capped at t2 + settle): the exact count against k6's
            answered cells x the calibrated backend server family k, and the
            failed share against k6's gRPC-failing share within 3 sigma.
  canary    the run's canary token in no Loki line, label or structured-metadata
            field over [t1, t2 + settle], no demo container log saved by the
            trap, no current Collector container log since t1, no span value of
            the fetched traces, not in the k6 log nor in soak-extras.json.
  orphans   SR9 on the k6 reservoir: s01 traces whole with only k6's parent
            missing, class none traces rooted, s00 traces absent; at least N per
            cell; one ``gateway.id`` per trace when the gateway sets it.
  parity    SR10: metric names and label keys of the demo jobs and span names
            and kinds, per app role, across the four languages; a difference
            passes only on an accepted entry of ``soak/sr10-gaps.json`` (or a
            runtime metric family); the gaps tables only suggest a candidate.
  logs      per-``severity_number`` Loki record counts per service (AG6, report-only).

The canary is read from ``runs/<run>/canary`` by this module only and is never
printed, logged, written or put in a query; every error text is redacted
before it is truncated. graph, orphans and parity are end-to-end checks, gated
by the shared agent and gateway sentinels over (t1, t2] (``soakcount.scoped_gates``).
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "soak"))
import k6script  # noqa: E402
import soakcount as sc  # noqa: E402
import soakplatform as sp  # noqa: E402
import verify  # noqa: E402

REPO = HERE.parent.parent
GAPS_DOC = REPO / "docs" / "site" / "library" / "overview.md"
GAPS_README = REPO / "README.md"
GAPS_HEADING = "known emitted-telemetry gaps"
SR10_GAPS = HERE / "soak" / "sr10-gaps.json"

SG_METRIC = "traces_service_graph_request_total"
BACKEND_FAMILY = "server:demo.v1.OrderService/PlaceOrder"
GRPC_FAILING = tuple(verify.GRPC_MESSAGES)          # business_error, db_error: a gRPC error status
SAMPLED_CLASSES = ("none", "s01")
UNSAMPLED_CLASS = "s00"
SIGMA_K = 3.0                                        # S-D5: within 3 sigma, sigma = sqrt(p(1-p)/n)
SHARE_MIN_N = 1000                                   # S-D5 "Sampling rates": n >= 1000 or insufficient-data
# The servicegraph connector evicts a series idle for 15 min, hard-coded (connector.go:815-849,
# collector-contrib v0.165.0); it has no metrics_expiration field. It flushes every 60 s (connector.go:59).
SG_EXPIRY_S = 900.0
SG_FLUSH_S = 60.0
SG_DEFAULT_TTL_S = 2.0                               # connector store.ttl default, when the CR sets none
DEFAULT_BATCH_S = 0.2                                # batch processor timeout default, when the CR sets none
SG_READ_MARGIN_S = 600
SG_EDGES_RE = "otelcol_connector_servicegraph_(expired|total)_edges(_total)?"
DEFAULT_SETTLE_S = verify.MIN_SETTLE
DEFAULT_SAMPLE = 50                                  # per reservoir cell (SR15 Load: the first 50 per cell)
SDK_BSP_DELAY_S = 5.0                                # OTEL_BSP_SCHEDULE_DELAY default 5000 ms (the demo apps set none)
# Tempo reads (platform owner, 2026-11-26, after tempo-0 was OOMKilled at 15:53:35Z by parallel reads):
DEFAULT_TEMPO_RATE = 10.0                            # reads per minute, strictly sequential
DEFAULT_TEMPO_MEM_STOP = 3 * 2**30                   # stop at a tempo-0 working set of 3 GiB (75 % of the 4 GiB limit, platform 085be8f)
TEMPO_BATCH = 10                                     # memory read before every batch of 10 reads
TEMPO_MAX_ERRORS = 3                                 # consecutive failed reads stop every further read
SEARCH_SLICES = 10
LOKI_LIMIT = 5000
LOKI_FILL = 0.5                                      # plan Loki slices at half the page limit
ENTRY_NAME = "POST /orders"
STATUS_SCEN = {"201": "ok", **{v: k for k, v in verify.HTTP_CODES.items()}}
GATEWAY_ID = "gateway.id"
COLLECTOR_PODS = (("opentelemetry", r"otel-(agent|gateway|logs)-collector-[a-z0-9-]+"),
                  (sp.APP_NS, r"soak-agent-[a-z0-9-]+"))
OTEL_LOGS_RE = r"otel-logs-collector-[a-z0-9-]+"
LOKI_NAMESPACES = (sp.APP_NS, "opentelemetry")
IGNORED_LABELS = {"__name__", "job", "instance", "cluster", "le"}
# SR10 "the language runtime metric families": the prefixes each language's runtime owns.
RUNTIME_FAMILIES = {"go": ("go_",), "python": ("process_", "system_", "cpython_"),
                    "dotnet": ("dotnet_",), "node": ("nodejs_", "v8js_")}
LANG_WORDS = {"go": re.compile(r"\bGo\b"), "python": re.compile(r"\bPython\b"),
              "node": re.compile(r"Node\.js|\bNode\b"), "dotnet": re.compile(r"\.NET\b")}
EVERY_LANG = re.compile(r"(?i)every language|all (four )?languages")
TOKEN_RE = re.compile(r"`([^`]+)`")
SET_ID_RE = re.compile(r'set\(\s*(resource\.)?attributes\[\s*"gateway\.id"\s*\]')
JUDGED_OK = ("PASS", "REPORT-ONLY", "N/A")
ROW_VERDICT = {"scanned": "PASS", "measured-empty": "REPORT-ONLY", "capped": "UNMEASURED",
               "incomplete": "UNMEASURED", "unmeasured": "UNMEASURED"}
LANGS_ALL = tuple(verify.LANGS)
TEMPO_MEM_Q = sp.sel("container_memory_working_set_bytes", {"namespace": "monitoring", "pod": "tempo-0",
                                                            "container": "tempo"})

_SECRET: list[str] = []     # the canary while main_extras runs: redact() removes it from any text


class InputError(ValueError):
    """Bad or unusable inputs: exit 2."""


def redact(text: object, limit: int | None = 200) -> str:
    """``text`` without the canary (case-insensitive), then truncated."""
    out = str(text)
    for s in _SECRET:
        out = re.sub(re.escape(s), "<canary>", out, flags=re.IGNORECASE)
    return out[:limit] if limit else out


class Sources:
    """Every read this module makes; tests replace it. ``r`` is the soak Reader."""

    def __init__(self, r: sp.Reader) -> None:
        self.r, self.b, self.context = r, r.b, r.context

    def prom(self, q: str, at: float) -> list[dict]:
        return self.r.prom(q, at)

    def series(self, match: str, start: float, end: float) -> list[dict]:
        return self.r.prom_series(match, start, end)

    def metadata(self) -> dict:
        data = self.b._get(self.b.prometheus, "/api/v1/metadata", {})
        verify._check_status("prometheus", data)
        return data.get("data") or {}

    def loki_entries(self, q: str, start: int, end: int, limit: int) -> list[tuple[str, dict, str]]:
        return self.b.loki_entries(q, start, end, limit=limit)

    def loki_query(self, q: str, at: int) -> list[dict]:
        return self.b.loki_query(q, at)

    def tempo_trace(self, trace_id: str) -> dict | None:
        return self.b.tempo_trace(trace_id)

    def tempo_search(self, q: str, start: int, end: int, limit: int) -> list[dict]:
        return self.b.tempo_search(q, start, end, limit)

    def tempo_metrics(self, q: str, start: int, end: int, step: int) -> list[dict]:
        return self.b.tempo_metrics(q, start, end, step)

    def kget(self, args: list[str]) -> dict:
        return self.r.kget(args)

    def logs(self, namespace: str, pod: str, container: str, since_s: int) -> str:
        return self.r._run(["logs", "-n", namespace, pod, "-c", container, f"--since={since_s}s"])


class TempoStopped(Exception):
    """The Tempo guard stopped fetching (memory at the stop line, unreadable, or 3 errors)."""


class TempoError(Exception):
    """One Tempo read failed (its text redacted); the run goes on."""


class TempoGuard:
    """Every Tempo read of this module, by the platform owner's rules (2026-11-26,
    after tempo-0 was OOMKilled by parallel trace-by-ID reads): strictly
    sequential, at most ``rate`` reads per minute; before every batch of
    ``batch`` reads, tempo-0's ``container_memory_working_set_bytes`` from
    Prometheus — at or above ``mem_stop`` (or unreadable) no further read; a failed
    read is that item's UNMEASURED and never aborts the run; ``max_errors``
    consecutive failures stop every further read."""

    def __init__(self, src: Sources, rate_per_min: float = DEFAULT_TEMPO_RATE, mem_stop: float = DEFAULT_TEMPO_MEM_STOP,
                 *, clock=time.monotonic, sleep=time.sleep, wall=time.time, batch: int = TEMPO_BATCH,
                 max_errors: int = TEMPO_MAX_ERRORS) -> None:
        if rate_per_min <= 0:
            raise InputError("--tempo-rate must be > 0 reads per minute")
        self.src, self.interval, self.mem_stop = src, 60.0 / rate_per_min, mem_stop
        self.rate_per_min = rate_per_min
        self.clock, self.sleep, self.wall = clock, sleep, wall
        self.batch, self.max_errors = batch, max_errors
        self.calls = self.errors = self.in_row = 0
        self.last: float | None = None
        self.stopped: str | None = None
        self.mem_max = 0.0
        self.mem_reads = 0

    def _memory(self) -> None:
        try:
            got = sp.cluster_only(self.src.prom(TEMPO_MEM_Q, self.wall()))
        except Exception as exc:  # noqa: BLE001 — an unreadable memory stops the reads
            self.stopped = f"tempo-0 memory unreadable ({redact(exc)}): Tempo reads stopped"
            raise TempoStopped(self.stopped) from None
        if not got:
            self.stopped = "tempo-0 memory unreadable (no container_memory_working_set_bytes): Tempo reads stopped"
            raise TempoStopped(self.stopped)
        mem = max(float(s["value"][1]) for s in got)
        self.mem_reads += 1
        self.mem_max = max(self.mem_max, mem)
        if mem >= self.mem_stop:
            self.stopped = (f"tempo-0 working set {mem / 2**30:.2f} GiB >= stop line "
                            f"{self.mem_stop / 2**30:.2f} GiB: Tempo reads stopped")
            raise TempoStopped(self.stopped)

    def call(self, fn, *args):
        if self.stopped:
            raise TempoStopped(self.stopped)
        if self.last is not None:
            wait = self.interval - (self.clock() - self.last)
            if wait > 0:
                self.sleep(wait)
        if self.calls % self.batch == 0:
            self._memory()
        self.calls += 1
        self.last = self.clock()
        try:
            out = fn(*args)
        except Exception as exc:  # noqa: BLE001 — a failed read never aborts the run
            self.errors += 1
            self.in_row += 1
            text = redact(exc)
            if self.in_row >= self.max_errors:
                self.stopped = f"{self.in_row} consecutive Tempo errors (last: {text}): Tempo reads stopped"
            raise TempoError(text) from None
        self.in_row = 0
        return out

    def trace(self, tid: str):
        return self.call(self.src.tempo_trace, tid)

    def search(self, q: str, start: int, end: int, limit: int):
        return self.call(self.src.tempo_search, q, start, end, limit)

    def metrics(self, q: str, start: int, end: int, step: int):
        return self.call(self.src.tempo_metrics, q, start, end, step)

    def stats(self) -> dict:
        return {"reads": self.calls, "errors": self.errors, "rate_per_min": self.rate_per_min,
                "mem_stop_gib": round(self.mem_stop / 2**30, 3), "mem_reads": self.mem_reads,
                "mem_max_gib": round(self.mem_max / 2**30, 3), "stopped": self.stopped,
                "rule": "sequential; memory read before every batch of "
                        f"{self.batch}; {self.max_errors} consecutive errors stop the reads"}


# ── inputs ──


def resolve_inputs(args, runs_dir: Path = sp.RUNS_DIR) -> dict:
    """{t1, t2, k6_log, k6_end, missing}: ``--t1``/``--t2``/``--k6-log``/``--k6-end``
    win over ``--count-window`` (its ``t1``, ``t2``, ``k6_end`` and ``k6_log``,
    relative to ``runs/<run>/``). A count window recorded ``valid: false`` is
    refused (InputError)."""
    t1, t2, k6, k6_end = args.t1, args.t2, args.k6_log, getattr(args, "k6_end", None)
    if args.count_window is not None:
        cw = json.loads(Path(args.count_window).read_text())
        if cw.get("valid") is False:
            raise InputError(f"count window not valid: {'; '.join(cw.get('reasons') or ['valid: false'])}")
        t1 = cw.get("t1") if t1 is None else t1
        t2 = cw.get("t2") if t2 is None else t2
        k6_end = cw.get("k6_end") if k6_end is None else k6_end
        if k6 is None and cw.get("k6_log"):
            k6 = runs_dir / args.run / cw["k6_log"]
    missing = [f for f, v in (("--t1", t1), ("--t2", t2), ("--k6-log", k6), ("--kcal", args.kcal)) if v is None]
    return {"t1": float(t1) if t1 is not None else 0.0, "t2": float(t2) if t2 is not None else 0.0,
            "k6_log": Path(k6) if k6 is not None else Path(), "missing": missing,
            "k6_end": float(k6_end) if k6_end is not None else None}


def read_canary(run: str, runs_dir: Path = sp.RUNS_DIR) -> str | None:
    """The run's canary token, or None. Never returned to a caller that prints."""
    p = runs_dir / run / "canary"
    if not p.exists():
        return None
    token = p.read_text().strip()
    return token or None


def shared_gates(src: Sources, t1: float, t2: float) -> dict:
    """The shared agent and gateway sentinels over (t1, t2], the shared queues at
    t2 and the gateway-restart detector, as ``soak count`` gates its cross-check."""
    sent = [v for v in sp.read_sentinels(src, t2, (t1,), copy_idle=False)[0] if v.scope != "copy"]
    pods = src.kget(["pods", "-n", "opentelemetry"]).get("items") or []
    upt = sp.cluster_only(src.prom(f"{sp.sel('otelcol_process_uptime', {'job': sp.GATEWAY_JOB})}[{int(t2 - t1)}s]", t2))
    extra = {"shared:agent queue t2": sc.queue_check(src, sp.AGENT_JOB, t2, zero=False),
             "shared:gateway queue t2": sc.queue_check(src, sp.GATEWAY_JOB, t2, zero=False),
             "shared:gateway restart": sc.gateway_restart(pods, upt, t1, t2)}
    return sc.scoped_gates(sent, extra)["shared"]


def gateway_cr(src: Sources) -> dict:
    """The gateway CR, or {} when unreadable."""
    try:
        return src.kget(["opentelemetrycollector", "otel-gateway", "-n", "opentelemetry"])
    except verify.QueryError:
        return {}


def collector_cr(src: Sources, name: str) -> dict:
    """A Collector CR in ``opentelemetry``, or {} when unreadable."""
    try:
        return src.kget(["opentelemetrycollector", name, "-n", "opentelemetry"])
    except verify.QueryError:
        return {}


def duration_s(text, default: float) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", str(text or ""))
    if not m:
        return default
    return float(m.group(1)) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def _walk_keys(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path + (str(k),), v
            yield from _walk_keys(v, path + (str(k),))
    elif isinstance(obj, list):
        for v in obj:
            yield path, v
            yield from _walk_keys(v, path)


def gateway_sets_id(cr: dict) -> tuple[bool, str]:
    """(sets, evidence): does a gateway processor write ``gateway.id``? Only a real
    set action counts: an ``attributes`` / ``resource`` action with key
    ``gateway.id`` (insert, upsert, update) or an OTTL ``set(…attributes["gateway.id"]…)``."""
    cfg = (cr.get("spec") or {}).get("config") or {}
    for name, proc in (cfg.get("processors") or {}).items():
        for path, v in _walk_keys(proc):
            if isinstance(v, dict) and v.get("key") == GATEWAY_ID and v.get("action") in ("insert", "upsert", "update"):
                return True, f"processor {name} {v['action']}s {GATEWAY_ID}"
            if isinstance(v, str) and SET_ID_RE.search(v):
                return True, f"processor {name} sets {GATEWAY_ID} (OTTL)"
    if not cfg:
        return False, "gateway CR unreadable"
    replicas = (cr.get("spec") or {}).get("replicas")
    exp = sorted((cfg.get("exporters") or {}).keys())
    return False, (f"N/A, AG2: the gateway writes no {GATEWAY_ID} (replicas {replicas}, exporters {exp}, "
                   f"no load-balancing tier); SR9 gateway.id routing is accepted gap AG2")


def flush_after(cr: dict, k6_end: float, agent_cr: dict | None = None) -> tuple[float, str]:
    """k6's end + the SDK BatchSpanProcessor delay + the agent batch timeout + the
    connector's store TTL + 2 x the gateway batch timeout: the first connector
    flush after it holds every edge of the load."""
    cfg = (cr.get("spec") or {}).get("config") or {}
    acfg = ((agent_cr or {}).get("spec") or {}).get("config") or {}
    ttl = duration_s(((cfg.get("connectors") or {}).get("service_graph") or {}).get("store", {}).get("ttl"),
                     SG_DEFAULT_TTL_S)
    batch = duration_s(((cfg.get("processors") or {}).get("batch") or {}).get("timeout"), DEFAULT_BATCH_S)
    abatch = duration_s(((acfg.get("processors") or {}).get("batch") or {}).get("timeout"), DEFAULT_BATCH_S)
    return (k6_end + SDK_BSP_DELAY_S + abatch + ttl + 2 * batch,
            f"k6 end + SDK BatchSpanProcessor delay {SDK_BSP_DELAY_S:g}s + agent batch.timeout {abatch:g}s"
            f" + store.ttl {ttl:g}s + 2 x gateway batch.timeout {batch:g}s")


# ── check 1: the api -> backend service graph ──


def backend_k(kcal: dict, lang: str) -> tuple[dict[str, int] | None, str]:
    """(cell -> k of the backend server family, mode): ``calibrated``; ``nominal``
    (1 per backend scenario in a sampled class) only when K-cal recorded
    ``qualified: false``; ``missing`` (None) when the service or the family is
    absent from the calibration file."""
    svc = verify.svc(lang, "backend")
    cal = (kcal.get("services") or {}).get(svc)
    fam = ((kcal.get("families") or {}).get(svc) or {}).get(BACKEND_FAMILY)
    if cal is None:
        return None, f"missing: {svc} not in the calibration file"
    if cal.get("qualified") is True:
        if not fam:
            return None, f"missing: no {BACKEND_FAMILY} family for {svc} in the calibration file"
        return dict(fam), "calibrated"
    if cal.get("qualified") is False:
        nominal = {}
        for scen in k6script.EXPECTED_STATUS:
            for cls in k6script.CLASSES:
                k = 1 if scen in verify.BACKEND_SCENARIOS and cls in SAMPLED_CLASSES else 0
                nominal[f"lang:{lang},route:{k6script.ROUTE},scen:{scen},class:{cls}"] = k
        return nominal, f"nominal: K-cal did not qualify {svc} (1 per backend scenario in a sampled class)"
    return None, f"missing: {svc} has no qualified flag in the calibration file"


def _edge(series: list[dict], client: str, server: str) -> list[dict]:
    return [s for s in sp.cluster_only(series) if (s.get("metric") or {}).get("client") == client
            and (s.get("metric") or {}).get("server") == server and not (s.get("metric") or {}).get("connection_type")]


def read_time(series: list[dict], after: float, cap: float) -> tuple[float | None, str]:
    """The first sample time (a connector flush: the series are pushed over OTLP)
    after ``after`` and at most ``cap``; None when there is none."""
    ts = sorted({float(t) for s in series for t, _ in s.get("values") or []})
    later = [t for t in ts if after < t <= cap]
    if later:
        return later[0], "first connector flush after the load's last edges"
    return None, "no connector flush after the load's last edges before the cap (t2 + settle)"


def edge_counts(series: list[dict], t1: float, t2: float, expiry: float = SG_EXPIRY_S) -> dict:
    """{total, failed}: (lower, upper) raw-sample counts of one edge over (t1, t2]."""
    scrape = sc.measured_scrape(series, sc.GATEWAY_SCRAPE_S)
    acc = {"total": [0.0, 0.0], "failed": [0.0, 0.0]}
    events: list[str] = []
    for s in series:
        m = s.get("metric") or {}
        lo, hi, ev = sc.count_series(s.get("values") or [], t1, t2, expiry, scrape)
        acc["total"][0] += lo
        acc["total"][1] += hi
        if m.get("failed") == "true":
            acc["failed"][0] += lo
            acc["failed"][1] += hi
        events += [f"failed={m.get('failed')}: {e}" for e in ev]
    return {"total": sc.Counted(*acc["total"]), "failed": sc.Counted(*acc["failed"]), "series": len(series),
            "events": events[:20], "scrape_s": scrape}


def judge_share(failed: float, total: float, p_exp: float) -> dict:
    """The failed share against the expected share within SIGMA_K sigma (binomial)."""
    if total <= 0:
        return {"verdict": "UNQUALIFIED", "reason": "no request on the edge"}
    p = failed / total
    sigma = math.sqrt(p_exp * (1 - p_exp) / total)
    ok = abs(p - p_exp) <= SIGMA_K * sigma + 1e-12
    return {"observed": round(p, 6), "expected": round(p_exp, 6), "sigma": round(sigma, 6), "n": total,
            "verdict": "PASS" if ok else "FAIL"}


def _worst(*verdicts: str) -> str:
    order = ("FAIL", "UNMEASURED", "UNQUALIFIED", "PASS")
    vs = [v for v in verdicts if v]
    return min(vs, key=lambda v: order.index(v) if v in order else len(order)) if vs else "PASS"


def graph_reads(series: list[dict], langs, after: float, cap: float) -> dict:
    """lang -> (read time or None, note), from the api -> backend edge's samples."""
    return {lang: read_time(_edge(series, verify.svc(lang, "api"), verify.svc(lang, "backend")), after, cap)
            for lang in langs}


def graph_verdicts(series: list[dict], cells: dict, kcal: dict, langs, t1: float, reads: dict,
                   gate: dict) -> dict:
    """Per language: the failed share (judged) and the exact counts (report-only,
    r6.2 "Counting in bounds" judges only the fixed span families). The count's
    lower end is the exact expected count; the upper end allows the
    demo-<lang>-worker -> api edge growth over the read (the restored demo's own
    traffic after the trap: a worker request makes at most one backend call)."""
    out = {}
    for lang in langs:
        t_read, read_note = reads[lang]
        kmap, mode = backend_k(kcal, lang)
        if t_read is None:
            out[lang] = {"verdict": "UNMEASURED", "reasons": [read_note], "read_note": read_note, "k_mode": mode,
                         "total": {"counted": None, "verdict": "UNMEASURED"}, "failed": {"counted": None},
                         "share": {"verdict": "UNMEASURED"}, "worker_api_growth": {"total": None}}
            continue
        edge = _edge(series, verify.svc(lang, "api"), verify.svc(lang, "backend"))
        worker = _edge(series, verify.svc(lang, "worker"), verify.svc(lang, "api"))
        got = edge_counts(edge, t1, t_read)
        bg = edge_counts(worker, t1, t_read)
        reasons: list[str] = []
        entry = {"series": got["series"], "events": got["events"], "read_at": t_read, "read_at_iso": sp.iso(t_read),
                 "read_note": read_note, "k_mode": mode,
                 "worker_api_growth": {"total": float(bg["total"]), "failed": float(bg["failed"])}}
        tot, fl, allow = float(got["total"]), float(got["failed"]), float(bg["total"])
        if kmap is None:
            entry.update(total={"counted": tot, "verdict": "UNQUALIFIED", "report_only": True},
                         failed={"counted": fl, "report_only": True},
                         share={"verdict": "UNQUALIFIED", "reason": mode}, verdict="UNQUALIFIED", reasons=[mode])
            out[lang] = entry
            continue
        fail_map = {c: (k if sc.cell_tags(c).get("scen") in GRPC_FAILING else 0) for c, k in kmap.items()}
        total_b, fail_b = sc.bounds(cells, kmap, lang), sc.bounds(cells, fail_map, lang)
        total_b.hi += allow
        fail_b.hi += allow
        exact = sc.judge_bound(tot, total_b)
        entry["total"] = {"counted": tot, "lo": total_b.lo, "hi": total_b.hi, "verdict": exact, "report_only": True,
                          "upper_allowance": f"+{allow:g} worker->api growth"}
        entry["failed"] = {"counted": fl, "lo": fail_b.lo, "hi": fail_b.hi, "verdict": sc.judge_bound(fl, fail_b),
                           "report_only": True}
        if total_b.missing:
            share = {"verdict": "UNQUALIFIED", "reason": f"no k for cells {total_b.missing}"}
        elif exact == "LOSS":
            share = {"verdict": "UNQUALIFIED", "reason": "the edge total is LOSS: the share is not judged"}
        elif tot < SHARE_MIN_N:
            share = {"verdict": "UNQUALIFIED", "reason": f"n {tot:g} < {SHARE_MIN_N} (S-D5 floor)"}
        elif total_b.lo <= 0:
            share = {"verdict": "UNQUALIFIED", "reason": f"no answered backend request for {lang}"}
        else:
            share = judge_share(fl, tot, fail_b.lo / total_b.lo)
        entry["share"] = share
        verdict = share["verdict"]
        if share.get("reason"):
            reasons.append(f"share: {share['reason']}")
        if not gate["clean"]:
            reasons.append("shared sentinels: " + "; ".join(gate["reasons"]))
            entry["measured"], verdict = verdict, "UNQUALIFIED"
        if mode.startswith("nominal"):
            reasons.append(f"report-only, wide band (worker->api allowance {allow:g}); {mode}")
            entry["measured"] = entry.get("measured", verdict)
            verdict = "REPORT-ONLY"
        entry.update(verdict=verdict, reasons=reasons)
        out[lang] = entry
    return out


def read_graph(src: Sources, t1: float, cap: float) -> tuple[str, list[dict]]:
    span = int(cap - t1) + SG_READ_MARGIN_S
    q = (f"{sp.sel(SG_METRIC, {'client': ('=~', 'demo-(go|python|node|dotnet)-(api|worker)')})}"
         f"[{span}s]")
    return q, sp.cluster_only(src.prom(q, cap))


def edge_store(src: Sources, t1: float, t2: float) -> tuple[str, dict]:
    """Report-only (SR15): the gateway connector's expired and total edge counters
    over (t1, t2], all edges (not attributable to one edge)."""
    span = int(t2 - t1) + SG_READ_MARGIN_S
    q = f"{sp.name_sel(SG_EDGES_RE, {'job': sp.GATEWAY_JOB})}[{span}s]"
    series = sp.cluster_only(src.prom(q, t2))
    scrape = sc.measured_scrape(series, sc.GATEWAY_SCRAPE_S)
    out: dict[str, float] = {}
    for s in series:
        name = (s.get("metric") or {}).get("__name__", "")
        lo, _, _ = sc.count_series(s.get("values") or [], t1, t2, math.inf, scrape)
        out[name] = out.get(name, 0.0) + lo
    return q, out


# ── check 2: the secret canary ──


def contains(text: str, canary: str) -> bool:
    return canary.lower() in str(text).lower()


def scan_entries(entries, canary: str) -> dict:
    """Counts only: lines scanned, canary hits by place (field names, never values),
    hygiene pattern hits (``verify.scan_secret``) by pattern."""
    out = {"lines": 0, "hits": [], "hygiene": Counter()}
    for labels, line in entries:
        out["lines"] += 1
        who = labels.get("service_name") or labels.get("k8s_pod_name") or "?"
        if contains(line, canary):
            out["hits"].append(f"line of {who}")
        for name, value in labels.items():
            if contains(value, canary):
                out["hits"].append(f"field {name} of {who}")
        for kind in verify.scan_secret(line):
            out["hygiene"][kind] += 1
    return out


def loki_counts(src: Sources, selector: str, start: int, end: int, by: str = "service_name") -> list[tuple[str, int]]:
    """(label value, record count) per group of ``selector`` over [start, end]."""
    q = f"sum by ({by}) (count_over_time({selector}[{end - start}s]))"
    return [((r.get("metric") or {}).get(by, ""), int(float(r["value"][1]))) for r in src.loki_query(q, end)]


def loki_slices(start: int, end: int, count: int, limit: int = LOKI_LIMIT) -> list[tuple[int, int]]:
    n = max(1, math.ceil(count / (limit * LOKI_FILL)))
    step = max(1, math.ceil((end - start) / n))
    return [(a, min(a + step, end)) for a in range(start, end, step)]


def loki_records(src: Sources, q: str, start: int, end: int, count: int) -> tuple[list[tuple[dict, str]], bool]:
    """Every record of ``q`` in [start, end] as (labels, line), slices planned from
    ``count`` and halved while one still hits the limit; de-duplicated on
    (timestamp, line, labels)."""
    class _B:  # verify._loki_page reads ``loki_entries`` only
        def loki_entries(self, qq, a, e, limit=50):
            return src.loki_entries(qq, a, e, limit)
    seen: dict[tuple, tuple[dict, str]] = {}
    capped = False
    for a, e in loki_slices(start, end, count):
        got, cap = verify._loki_page(_B(), q, a, e, LOKI_LIMIT, verify.MIN_SLICE)
        capped = capped or cap
        for ts, labels, line in got:
            seen.setdefault((ts, line, tuple(sorted(labels.items()))), (labels, line))
    return list(seen.values()), capped


def _scan_selector(src: Sources, canary: str, name: str, selector: str, count: int, start: int, end: int) -> dict:
    try:
        recs, capped = loki_records(src, selector, start, end, count)
    except verify.QueryError as exc:
        return {"source": name, "status": "unmeasured", "reason": f"query error: {redact(exc)}", "lines": 0,
                "hits": []}
    res = scan_entries(recs, canary)
    status = "capped" if capped else ("incomplete" if res["lines"] < count else "scanned")
    row = {"source": name, "lines": res["lines"], "expected_lines": count, "hits": res["hits"],
           "hygiene": dict(res["hygiene"]), "status": status}
    if status == "incomplete":
        row["reason"] = f"{res['lines']} record(s) read of {count} counted"
    return row


def canary_loki(src: Sources, canary: str, start: int, end: int) -> list[dict]:
    rows = []
    for ns in LOKI_NAMESPACES:
        base = f"k8s_namespace_name={verify._quote(ns)}"
        try:
            groups = loki_counts(src, f"{{{base}}}", start, end)
        except verify.QueryError as exc:
            rows.append({"source": f"loki {ns}", "status": "unmeasured", "reason": f"count: {redact(exc)}",
                         "lines": 0, "hits": []})
            continue
        if not groups:
            rows.append({"source": f"loki {ns}", "lines": 0, "hits": [], "status": "unmeasured",
                         "reason": "no stream in the window"})
        for svc, count in sorted(groups):
            rows.append(_scan_selector(src, canary, f"loki {ns}/{svc or '(no service_name)'}",
                                       f"{{{base}, service_name={verify._quote(svc)}}}", count, start, end))
    # M6: demo-service records without a namespace label (a k8s_attributes miss).
    sel = f'{{service_name=~{verify._quote(sp.DEMO_SERVICE_RE)}, k8s_namespace_name=""}}'
    try:
        groups = loki_counts(src, sel, start, end)
    except verify.QueryError as exc:
        rows.append({"source": "loki (no namespace)/demo", "status": "unmeasured",
                     "reason": f"count: {redact(exc)}", "lines": 0, "hits": []})
        return rows
    if not groups:
        rows.append({"source": "loki (no namespace)/demo", "lines": 0, "hits": [], "status": "scanned",
                     "reason": "no demo record without k8s_namespace_name"})
    for svc, count in sorted(groups):
        rows.append(_scan_selector(src, canary, f"loki (no namespace)/{svc}",
                                   f'{{service_name={verify._quote(svc)}, k8s_namespace_name=""}}', count, start, end))
    return rows


def container_states(pod: dict) -> list[tuple[str, float | None, int]]:
    return [(c.get("name", ""), sp.parse_ts(((c.get("state") or {}).get("running") or {}).get("startedAt")),
             int(c.get("restartCount") or 0)) for c in (pod.get("status") or {}).get("containerStatuses") or []]


def canary_collectors(src: Sources, canary: str, t1: float, now: float) -> tuple[list[dict], dict]:
    """The current Collector containers' logs since t1 when they ran since t1;
    and {otel_logs_stable: bool, why} for the demo file_log fallback."""
    rows = []
    since = int(math.ceil(now - t1)) + 1
    stable = {"ok": True, "why": []}
    for ns, rx in COLLECTOR_PODS:
        try:
            pods = [p for p in src.kget(["pods", "-n", ns]).get("items") or []
                    if re.fullmatch(rx, (p.get("metadata") or {}).get("name", ""))]
        except verify.QueryError as exc:
            rows.append({"source": f"container logs {ns} /{rx}/", "status": "unmeasured",
                         "reason": f"pod list: {redact(exc)}", "lines": 0, "hits": []})
            if ns == "opentelemetry":
                stable = {"ok": False, "why": ["pod list unreadable"]}
            continue
        if not pods:
            rows.append({"source": f"container logs {ns} /{rx}/", "status": "unmeasured",
                         "reason": "no such pod now", "lines": 0, "hits": []})
        for p in pods:
            name = p["metadata"]["name"]
            for cname, started, restarts in container_states(p):
                src_name = f"container log {ns}/{name}/{cname}"
                if re.fullmatch(OTEL_LOGS_RE, name) and (started is None or started > t1):
                    stable["ok"] = False
                    stable["why"].append(f"{name} started {sp.iso(started)} after t1")
                if started is None or started > t1:
                    why = (f"container started {sp.iso(started)} after t1 (restarts {restarts})" if started
                           else "container not running")
                    rows.append({"source": src_name, "status": "unmeasured", "reason": why, "lines": 0, "hits": []})
                    continue
                try:
                    text = src.logs(ns, name, cname, since)
                except verify.QueryError as exc:
                    rows.append({"source": src_name, "status": "unmeasured", "reason": redact(exc),
                                 "lines": 0, "hits": []})
                    continue
                lines = text.splitlines()
                hits = sum(1 for ln in lines if contains(ln, canary))
                rows.append({"source": src_name, "status": "scanned", "lines": len(lines),
                             "hits": [f"{hits} line(s)"] if hits else [],
                             "note": "the current log file since t1 (kubelet rotation is not detectable here)"})
    return rows, stable


def trap_demo_logs(run_dir: Path) -> dict | None:
    """The trap's step-1b record (``demo_logs`` of the last trap.jsonl line that has one)."""
    p = run_dir / "trap.jsonl"
    if not p.exists():
        return None
    found = None
    for line in p.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("demo_logs"):
            found = rec["demo_logs"]
    return found


def run_hooked(run_dir: Path) -> bool | None:
    """``hook_applied`` from the run's state.json (None when unreadable)."""
    p = run_dir / "state.json"
    try:
        return bool(json.loads(p.read_text()).get("hook_applied"))
    except (OSError, json.JSONDecodeError):
        return None


def shut_deleted_logs(run_dir: Path) -> dict | None:
    """``deleted_logs`` of ``soak.py shut``'s shut-window.json: the logs of the
    api pods K-shut deleted, followed until their exit (the trap cannot read
    them). None for a run that is not a shut run."""
    p = run_dir / "shut-window.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text()).get("deleted_logs") or {}
    except (OSError, json.JSONDecodeError):
        return {"shut-window.json": {"error": "unreadable"}}


def _saved_log_rows(run_dir: Path, rec: dict, canary: str, label: str) -> list[dict]:
    rows = []
    for key, r in sorted(rec.items()):
        name = label.format(key=key)
        if r.get("error"):
            rows.append({"source": name, "status": "unmeasured", "reason": redact(r["error"]), "lines": 0,
                         "hits": []})
            continue
        path = run_dir / "raw" / r.get("path", "")
        text = path.read_text() if path.is_file() else ""
        hits = (r.get("canary_lines") or 0) + sum(1 for ln in text.splitlines() if contains(ln, canary))
        status = "scanned" if path.is_file() and r.get("canary_lines") is not None else "unmeasured"
        row = {"source": name, "status": status, "lines": r.get("lines", 0),
               "hits": [f"{hits} line(s)"] if hits else []}
        if r.get("marker"):
            row["note"] = "saved as the marker instead of the text (canary lines); count from the record"
        if status == "unmeasured":
            row["reason"] = "no saved file or no canary count"
        rows.append(row)
    return rows


def canary_demo(run_dir: Path, canary: str, src: Sources, t1: float, t2: float, otel_logs: dict) -> list[dict]:
    """The demo container logs of the trap (``canary_demo_trap``) plus, for a
    K-shut run, the deleted api pods' followed logs, each its own source."""
    rows = canary_demo_trap(run_dir, canary, src, t1, t2, otel_logs)
    deleted = shut_deleted_logs(run_dir)
    if deleted is not None:
        if not deleted:
            rows.append({"source": "deleted api pod logs (shut)", "status": "unmeasured", "lines": 0, "hits": [],
                         "reason": "shut-window.json records no deleted pod log"})
        rows += _saved_log_rows(run_dir, deleted, canary, "deleted api pod log {key} (shut)")
    return rows


def canary_demo_trap(run_dir: Path, canary: str, src: Sources, t1: float, t2: float, otel_logs: dict) -> list[dict]:
    """The window's demo container logs: the files the trap saved (step 1b). A
    hooked run without that record is UNMEASURED. Only a run not hooked (or with
    no state) may fall back to "measured empty via otel-logs file_log"
    (REPORT-ONLY), when file_log demonstrably collected the namespace in the
    window and otel-logs did not restart in it."""
    rec = trap_demo_logs(run_dir)
    if rec:
        return _saved_log_rows(run_dir, rec, canary, "demo container log {key} (trap)")
    name = "demo container logs (window pods, replaced by the trap)"
    if run_hooked(run_dir):
        return [{"source": name, "status": "unmeasured", "lines": 0, "hits": [],
                 "reason": "the hook was applied but trap.jsonl has no demo_logs record (run before trap step 1b)"}]
    try:
        filelog = loki_counts(src, f'{{k8s_namespace_name={verify._quote(sp.APP_NS)}}} | service_instance_id = ""',
                              int(t1), int(t2))
    except verify.QueryError as exc:
        return [{"source": name, "status": "unmeasured", "reason": f"file_log count: {redact(exc)}", "lines": 0,
                 "hits": []}]
    demo_filelog = sum(n for svc, n in filelog if sc.SERVICE_RE.fullmatch(svc))
    other_filelog = sum(n for svc, n in filelog if svc and not sc.SERVICE_RE.fullmatch(svc) and svc != "k8s-events")
    if other_filelog > 0 and otel_logs["ok"]:
        why = (f"no saved file; otel-logs file_log collected {sp.APP_NS} in the window "
               f"({other_filelog} record(s) of other containers, otel-logs did not restart since t1) and holds "
               + ("no demo stdout record" if demo_filelog == 0 else f"{demo_filelog} demo stdout record(s), scanned in the Loki rows"))
        return [{"source": name, "status": "measured-empty" if demo_filelog == 0 else "scanned", "lines": demo_filelog,
                 "hits": [], "reason": why,
                 "other_file_log_records": other_filelog}]
    why = ("no saved file and the file_log fallback is not proven: "
           + ("; ".join(otel_logs["why"]) if not otel_logs["ok"] else "no file_log record of another container"))
    return [{"source": name, "status": "unmeasured", "reason": why, "lines": 0, "hits": []}]


def canary_file(path: Path, canary: str, name: str) -> dict:
    if not Path(path).is_file():
        return {"source": name, "status": "unmeasured", "reason": "file missing", "lines": 0, "hits": []}
    lines = Path(path).read_text().splitlines()
    hits = sum(1 for ln in lines if contains(ln, canary))
    return {"source": name, "status": "scanned", "lines": len(lines), "hits": [f"{hits} line(s)"] if hits else []}


def canary_spans(traces: dict[str, list], canary: str) -> dict:
    hits = []
    spans = 0
    for spans_ in traces.values():
        for s in spans_:
            spans += 1
            for label, value in s.values():
                if contains(value, canary):
                    hits.append(f"{s.service} {label}")
    return {"source": "span values of the traces fetched for the orphan check", "traces": len(traces),
            "spans": spans, "lines": spans, "hits": hits, "status": "scanned" if traces else "unmeasured",
            **({} if traces else {"reason": "no trace fetched (see orphans)"})}


def canary_verdict(rows: list[dict]) -> str:
    """FAIL on any hit; PASS only when every source is PASS (a REPORT-ONLY
    fallback leaves the check UNMEASURED)."""
    if any(r["hits"] for r in rows):
        return "FAIL"
    if any(ROW_VERDICT.get(r["status"], "UNMEASURED") != "PASS" for r in rows):
        return "UNMEASURED"
    return "PASS"


# ── check 3: SR9 orphans on the reservoir ──


def reservoir(records: list[dict], sample: int) -> dict[str, list[dict]]:
    """cell (lang|scen|class) -> the first ``sample`` answered records."""
    out: dict[str, list[dict]] = {}
    for r in records:
        if not r.get("status"):
            continue
        cell = f"{r.get('lang')}|{r.get('scen')}|{r.get('class')}"
        lst = out.setdefault(cell, [])
        if len(lst) < sample:
            lst.append(r)
    return out


def entry_span(spans: list, lang: str):
    api = verify.svc(lang, "api")
    cands = [s for s in spans if s.service == api and s.kind == "server" and s.name == ENTRY_NAME]
    return min(cands, key=lambda s: s.start_ns) if cands else None


def judge_trace(spans: list, lang: str, cls: str, k6_parent: str | None, gw_sets: bool) -> dict:
    """{orphans, problems, gateway}: parents missing from the trace beyond the one
    allowed (s01: k6's parent on the entry span; none: zero, the entry a root);
    with ``gw_sets``, one ``gateway.id`` on every span."""
    ids = {s.span_id for s in spans}
    missing = [s for s in spans if s.parent_id and s.parent_id not in ids]
    entry = entry_span(spans, lang)
    res = {"spans": len(spans), "orphans": len(missing), "problems": []}
    if entry is None:
        res["problems"].append("no entry span (api SERVER POST /orders)")
    elif cls == "none":
        if entry.parent_id:
            res["problems"].append("entry span has a parent (class none)")
    else:
        if entry in missing:
            res["orphans"] -= 1
            if k6_parent is not None and entry.parent_id != verify.norm_span(k6_parent):
                res["orphans"] += 1
                res["problems"].append("entry parent differs from k6's parent_id")
        elif not entry.parent_id:
            res["problems"].append("entry span is a root (class s01)")
    gids = {v for s in spans for k, v in list(s.attrs.items()) + list(s.resource.items()) if k == GATEWAY_ID}
    without = sum(1 for s in spans if GATEWAY_ID not in s.attrs and GATEWAY_ID not in s.resource)
    res["gateway_split"] = len(gids) > 1 or (gw_sets and without > 0) or (bool(gids) and without > 0)
    return res


def classify(spans: list, lang: str) -> tuple[str | None, str | None]:
    """(class, scenario) of a trace found by search, from its entry span."""
    entry = entry_span(spans, lang)
    if entry is None:
        return None, None
    ids = {s.span_id for s in spans}
    cls = "none" if not entry.parent_id else ("s01" if entry.parent_id not in ids else None)
    return cls, STATUS_SCEN.get(entry.attrs.get("http.response.status_code", ""))


def fetch_all(guard: TempoGuard, ids: list[str]) -> dict[str, list | None | str]:
    """Sequential trace-by-ID reads through the guard: spans, None (not in Tempo),
    "error" (that read failed) or "stopped" (the guard stopped the reads)."""
    out: dict[str, list | None | str] = {}
    for tid in ids:
        try:
            data = guard.trace(tid)
        except TempoStopped:
            out[tid] = "stopped"
            continue
        except TempoError:
            out[tid] = "error"
            continue
        out[tid] = verify.parse_trace(data) if data is not None else None
    return out


def tempo_holds_window(guard: TempoGuard, t1: float, t2: float, probe_ids: list[str]) -> tuple[bool, str]:
    q = '{resource.service.name=~"demo-(go|python|node|dotnet)-api"}'
    try:
        found = guard.search(q, int(t1), int(t2), 1)
    except (TempoError, TempoStopped) as exc:
        return False, f"Tempo search: {exc}"
    if found:
        return True, "Tempo search finds demo api traces in the window"
    got = fetch_all(guard, probe_ids)
    if any(isinstance(v, list) for v in got.values()):
        return True, "Tempo search empty, by-ID fetch finds window traces"
    if guard.stopped:
        return False, guard.stopped
    return False, (f"Tempo holds no trace of the window: search over the window returned 0 and "
                   f"{len(probe_ids)} reservoir trace id(s) were not found")


def search_traces(guard: TempoGuard, lang: str, t1: float, t2: float, n: int) -> list[str]:
    """Up to ``n`` trace ids of api entry spans, spread over SEARCH_SLICES slices
    (one guarded search each); a failed slice is skipped."""
    q = (f"{{resource.service.name={verify._quote(verify.svc(lang, 'api'))} && kind=server && "
         f"name={verify._quote(ENTRY_NAME)}}}")
    per = max(1, math.ceil(n / SEARCH_SLICES))
    step = max(1, int((t2 - t1) // SEARCH_SLICES))
    ids: list[str] = []
    for a in range(int(t1), int(t2), step):
        try:
            found = guard.search(q, a, min(a + step, int(t2)), per)
        except TempoError:
            continue
        for t in found:
            tid = verify.norm_trace(t.get("traceID", ""))
            if tid and tid not in ids:
                ids.append(tid)
    return ids[:n]


def _new_cell(records: int = 0, source: str = "reservoir") -> dict:
    return {"records": records, "fetched": 0, "missing": 0, "errors": 0, "stopped": 0, "orphans": 0, "absent": 0,
            "present": 0, "gateway_split": 0, "problems": [], "source": source}


def _judge_into(c: dict, spans: list, lang: str, cls: str, parent: str | None, gw_sets: bool) -> None:
    j = judge_trace(spans, lang, cls, parent, gw_sets)
    c["fetched"] += 1
    c["orphans"] += j["orphans"]
    c["problems"] += j["problems"][:3]
    c["gateway_split"] += int(j["gateway_split"])


def cell_verdict(cell: str, c: dict, n: int) -> tuple[str, list[str]]:
    """FAIL (orphans, a missing s01 trace, an s00 trace present, a split or a
    problem), UNMEASURED (a failed or stopped read), UNQUALIFIED (fewer than N checked)."""
    cls = cell.split("|")[2].split(" ")[0]
    probs = []
    if c["orphans"]:
        probs.append(f"{c['orphans']} orphan(s)")
    if c["missing"]:
        probs.append(f"{c['missing']} sampled trace(s) missing from Tempo")
    if c["present"]:
        probs.append(f"{c['present']} unsampled trace(s) present in Tempo")
    if c["gateway_split"]:
        probs.append(f"{c['gateway_split']} trace(s) without one {GATEWAY_ID} on every span")
    if c["problems"]:
        probs.append(str(sorted(set(c["problems"]))))
    if probs:
        return "FAIL", probs
    if c["errors"] or c.get("stopped"):
        return "UNMEASURED", [f"{c['errors']} failed and {c.get('stopped', 0)} stopped Tempo read(s)"]
    checked = c["absent"] if cls == UNSAMPLED_CLASS else c["fetched"]
    if checked < n:
        return "UNQUALIFIED", [f"{checked} checked < N {n}"]
    return "PASS", []


def mix_size(cells: dict, lang: str) -> int:
    """The plan's mix length (Σ weights) from k6's class-none answered counts per
    scenario (7/1/1/1 → 10); the default mix when the cells say nothing."""
    counts = [n["expected"] + n["unexpected"] for c, n in cells.items()
              if sc.cell_tags(c).get("lang") == lang and sc.cell_tags(c).get("class") == "none"]
    counts = [c for c in counts if c > 0]
    if not counts:
        return sum(k6script.DEFAULT_MIX.values())
    return sum(round(c / min(counts)) for c in counts)


def orphan_check(guard: TempoGuard, records: list[dict], langs, t1: float, t2: float, sample: int,
                 gate: dict, gw: tuple[bool, str], cells: dict | None = None) -> tuple[dict, dict[str, list]]:
    """Per language: per cell, fetched / missing / orphan counts against N =
    ``sample``; s00 absent; the traces fetched (for the canary scan). Every
    Tempo read goes through ``guard``."""
    res_cells = reservoir(records, sample)
    probe = [r["trace_id"] for c, rs in sorted(res_cells.items()) if c.endswith("|s01") for r in rs
             if r.get("trace_id")][:5]
    held, why = tempo_holds_window(guard, t1, t2, probe)
    traces: dict[str, list] = {}
    out = {}
    for lang in langs:
        lc = {c: rs for c, rs in res_cells.items() if c.startswith(f"{lang}|")}
        entry = {"gateway_id": gw[1], "n": sample, "cells": {}, "reservoir_records": sum(len(v) for v in lc.values())}
        if not lc:
            entry["finding"] = "no reservoir record for this language"
        if not held:
            entry.update(verdict="UNMEASURED", reason=why)
            out[lang] = entry
            continue
        got = fetch_all(guard, [r["trace_id"] for rs in lc.values() for r in rs if r.get("trace_id")])
        for cell, rs in sorted(lc.items()):
            cls = cell.split("|")[2]
            c = entry["cells"].setdefault(cell, _new_cell(len(rs)))
            for r in rs:
                if not r.get("trace_id"):
                    continue
                spans = got.get(r["trace_id"])
                if spans in ("error", "stopped"):
                    c["errors" if spans == "error" else "stopped"] += 1
                elif cls == UNSAMPLED_CLASS:
                    c["present" if spans else "absent"] += 1
                elif spans is None:
                    c["missing"] += 1
                else:
                    traces[r["trace_id"]] = spans
                    _judge_into(c, spans, lang, cls, r.get("parent_id"), gw[0])
            if cls == "none":
                c["source"] = "reservoir (no trace id) + Tempo search"
        has_s01 = any(c.endswith("|s01") for c in lc)
        n = sample * mix_size(cells or {}, lang) * (1 if has_s01 else 2)
        entry["search_size"] = n
        try:
            ids = search_traces(guard, lang, t1, t2, n)
        except TempoStopped as exc:
            ids, entry["search_error"] = [], str(exc)
        found = fetch_all(guard, [i for i in ids if i not in traces])
        for tid, spans in found.items():
            if not isinstance(spans, list):
                continue
            cls, scen = classify(spans, lang)
            if cls is None or (cls == "s01" and has_s01):
                continue
            key = f"{lang}|{scen or '?'}|{cls}" + ("" if cls == "none" else " (search, parent unverified)")
            c = entry["cells"].setdefault(key, _new_cell(0, "Tempo search"))
            if c["fetched"] >= sample:
                continue
            traces[tid] = spans
            _judge_into(c, spans, lang, cls, None, gw[0])
        entry["searched"] = len(ids)
        problems, verdicts = [], []
        for cell, c in sorted(entry["cells"].items()):
            v, why_c = cell_verdict(cell, c, sample)
            c["verdict"] = v
            verdicts.append(v)
            if why_c:
                c["why"] = why_c
                problems.append(f"{cell}: {v} {'; '.join(why_c)}")
        entry.update(fetched=sum(c["fetched"] for c in entry["cells"].values()),
                     missing=sum(c["missing"] for c in entry["cells"].values()),
                     errors=sum(c["errors"] for c in entry["cells"].values()),
                     stopped=sum(c["stopped"] for c in entry["cells"].values()),
                     orphans=sum(c["orphans"] for c in entry["cells"].values()), problems=problems)
        verdict = _worst(*verdicts) if verdicts else "UNMEASURED"
        if guard.stopped and verdict != "FAIL":
            verdict, entry["reason"] = "UNMEASURED", guard.stopped
        if not lc and verdict in ("PASS", "UNQUALIFIED"):
            verdict, entry["reason"] = "UNMEASURED", "no reservoir: s01 parents and s00 absence not checked"
        if not verdicts:
            entry.setdefault("reason", "no trace fetched")
        if not gate["clean"] and verdict != "UNMEASURED":
            entry["measured"], verdict = verdict, "UNQUALIFIED"
            entry["reason"] = "shared sentinels: " + "; ".join(gate["reasons"])
        entry["verdict"] = verdict
        out[lang] = entry
    return out, traces


# ── check 4: SR10 parity ──


def norm_span_name(name: str, lang: str) -> str:
    return name.replace(f"demo:{lang}:", "demo:<lang>:")


def gap_rows(paths=(GAPS_DOC, GAPS_README)) -> list[dict]:
    """The known-gaps rows: {area, text, tokens, langs} from each file's
    "Known emitted-telemetry gaps" section (table rows, or the paragraph)."""
    rows = []
    for path in paths:
        text = Path(path).read_text()
        inside = False
        level = 0
        for line in text.splitlines():
            m = re.match(r"^(#+)\s+(.*)$", line)
            if m:
                if inside and len(m.group(1)) <= level:
                    inside = False
                if m.group(2).strip().lower() == GAPS_HEADING:
                    inside, level = True, len(m.group(1))
                continue
            if not inside or not line.strip():
                continue
            if line.startswith("|") and not re.match(r"^\|\s*-", line) and "| Area |" not in line:
                cols = [c.strip() for c in line.strip().strip("|").split("|")]
                rows.append({"area": cols[0][:80], "text": " | ".join(cols), "file": Path(path).name})
            elif not line.startswith("|"):
                rows.append({"area": "(paragraph)", "text": line, "file": Path(path).name})
    for r in rows:
        r["tokens"] = TOKEN_RE.findall(r["text"])
        r["langs"] = set(LANGS_ALL) if EVERY_LANG.search(r["text"]) else {
            lang for lang, rx in LANG_WORDS.items() if rx.search(r["text"])}
    return rows


def _prom_form(token: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", token.strip().rstrip("*")).strip("_").lower()


def token_matches(token: str, item: str) -> bool:
    """A backticked gaps token names the item: equal after the Prometheus
    translation (dots to underscores), or a prefix of it at a ``_`` boundary."""
    t = _prom_form(token)
    i = _prom_form(item)
    return bool(t) and (i == t or i.startswith(t + "_"))


def candidate_row(item: str, have: list[str], missing: list[str], rows: list[dict], *,
                  context: str = "") -> str | None:
    """A gaps row that may describe the difference (a hint printed next to a
    FAIL, never a pass): it names the item in backticks and a language of either
    side (both on a tie); a label key also needs a token of its instrument family."""
    side = set(have if len(have) < len(missing) else missing if len(missing) < len(have) else have + missing)
    for r in rows:
        if not (r["langs"] & side and any(token_matches(t, item) for t in r["tokens"])):
            continue
        if context and not any(_prom_form(t).startswith(context) for t in r["tokens"]):
            continue
        return f"{r['file']}: {r['area']}"
    return None


def attr_context(metric: str) -> str:
    """The instrument family of a label-key difference (the metric's first two
    name segments, ``http_server``); none for the span-derived metrics."""
    if metric.startswith("traces_span_metrics_"):
        return ""
    return "_".join(metric.split("_")[:2])


def runtime_family(item: str, have: list[str]) -> str | None:
    if have and all(any(item.startswith(p) for p in RUNTIME_FAMILIES[lang]) for lang in have):
        return f"runtime metric family ({', '.join(have)})"
    return None


def load_allowlist(path: Path = SR10_GAPS) -> list[dict]:
    data = json.loads(Path(path).read_text())
    entries = data.get("entries") if isinstance(data, dict) else data
    for e in entries:
        for k in ("kind", "role", "item", "reason", "gaps_ref", "status"):
            if k not in e:
                raise InputError(f"sr10-gaps.json entry without {k}: {e}")
    return entries


def allowed(item: dict, entries: list[dict], accept_proposed: bool = False) -> dict | None:
    """The entry that covers the difference exactly, or None: same kind, role
    (or ``*``), item (exact, or a declared prefix when ``match`` is ``prefix``),
    attribute for a label key, and ``langs_missing`` covering every missing
    language; ``accepted`` entries only (``proposed`` too in a preview)."""
    for e in entries:
        if e["status"] != "accepted" and not (accept_proposed and e["status"] == "proposed"):
            continue
        if e["kind"] != item["kind"] or e["role"] not in ("*", item["role"]):
            continue
        name = item.get("metric", item["item"])
        if e.get("match") == "prefix":
            if not name.startswith(e["item"]):
                continue
        elif name != e["item"]:
            continue
        if item["kind"] == "metric attr" and e.get("attribute") != item["attribute"]:
            continue
        if item["kind"] == "span" and e.get("span_kind") not in (None, item.get("span_kind")):
            continue
        if not set(item["missing"]) <= set(e.get("langs_missing") or []):
            continue
        return e
    return None


def recorded_unmeasured(item: dict, entries: list[dict], run: str | None) -> dict | None:
    """The ``unmeasured`` allowlist entry that covers the difference exactly
    (same match rule as ``allowed``) and names ``run`` in its ``runs``, or
    None: an owner-ruled UNMEASURED item of that run only."""
    um = [e for e in entries if e.get("status") == "unmeasured" and run and run in (e.get("runs") or [])]
    hit = allowed(item, [dict(e, status="accepted") for e in um])
    return next((e for e in um if dict(e, status="accepted") == hit), None) if hit else None


def rejudge(saved: dict, entries: list[dict], accept_proposed: bool = False, run: str | None = None) -> dict:
    """Re-judges the SR10 items of a saved ``soak-extras.json`` against an
    allowlist, offline: runtime-family items and items UNMEASURED by the
    traffic rule keep their status; every other item is judged again
    (``unmeasured`` entry → UNMEASURED, an ``accepted`` entry → documented,
    else FAIL). No backend read."""
    items, counts = [], {"documented": 0, "UNMEASURED": 0, "FAIL": 0}
    for old in (saved.get("parity") or {}).get("items") or []:
        it = {k: v for k, v in old.items() if k not in ("status", "row", "reason", "candidate")}
        runtime = old.get("status") == "documented" and not str(old.get("row") or "").startswith("sr10-gaps.json")
        traffic = old.get("status") == "UNMEASURED" and str(old.get("reason") or "").startswith("no traffic")
        if runtime or traffic:
            it.update(status=old["status"], row=old.get("row"), reason=old.get("reason"))
        elif (um := recorded_unmeasured(it, entries, run)):
            it.update(status="UNMEASURED", row=None, reason=f"{um['reason']} ({um.get('ruling', 'no ruling')})")
        elif (e := allowed(it, entries, accept_proposed)):
            it.update(status="documented", row=f"sr10-gaps.json ({e['status']}): {e['gaps_ref']}")
        else:
            it.update(status="FAIL", row=None)
        counts[it["status"]] += 1
        items.append(it)
    verdict = "FAIL" if counts["FAIL"] else ("UNMEASURED" if counts["UNMEASURED"] else "PASS")
    if accept_proposed and verdict == "PASS":
        verdict = "PREVIEW"
    return {"verdict": verdict, "counts": counts, "items": items, "source_t1": saved.get("t1"),
            "source_t2": saved.get("t2"), "accept_proposed": accept_proposed, "run": run,
            "note": "offline re-judge of the saved SR10 items against the current allowlist (no backend read)"}


def main_rejudge(args) -> int:
    """``verify.py soak extras --rejudge <soak-extras.json>``: prints the counts
    and writes the result to ``--output`` (never overwriting)."""
    try:
        saved = json.loads(Path(args.rejudge).read_text())
        gaps = json.loads(Path(args.sr10_gaps or SR10_GAPS).read_text())
    except (OSError, ValueError) as exc:
        print(f"verify soak extras --rejudge: {redact(exc)}", file=sys.stderr)
        return 2
    out = rejudge(saved, gaps.get("entries") or [], bool(getattr(args, "sr10_accept_proposed", False)),
                  getattr(args, "run", None))
    c = out["counts"]
    print(f"verify soak extras --rejudge: items {sum(c.values())}: documented {c['documented']}, "
          f"UNMEASURED {c['UNMEASURED']}, FAIL {c['FAIL']} -> {out['verdict']}", flush=True)
    if getattr(args, "output", None):
        try:
            sc._write_json(Path(args.output).parent, "extras-rejudge", out, args.output)
        except FileExistsError:
            print(f"verify soak extras --rejudge: --output {args.output} exists; not overwritten", file=sys.stderr)
            return 1
    return 0 if out["verdict"] == "PASS" else 1


def metric_sets(series: list[dict]) -> dict[str, dict[str, dict[str, set]]]:
    """role -> lang -> metric name -> label keys, from /api/v1/series label sets."""
    out: dict[str, dict[str, dict[str, set]]] = {}
    for s in series:
        m = re.fullmatch(rf"{re.escape(sp.APP_NS)}/demo-(go|python|node|dotnet)-(api|backend|worker)", s.get("job", ""))
        if not m or s.get("cluster") != sp.CLUSTER:
            continue
        keys = {k for k in s if k not in IGNORED_LABELS}
        out.setdefault(m.group(2), {}).setdefault(m.group(1), {}).setdefault(s["__name__"], set()).update(keys)
    return out


def span_sets(series: list[dict], traces: dict[str, list]) -> dict[str, dict[str, set]]:
    """role -> lang -> {(normalized span name, kind)}, from TraceQL metrics series
    (labels ``resource.service.name``, ``name``, ``kind``) or the fetched traces."""
    out: dict[str, dict[str, set]] = {}

    def add(service: str, name: str, kind: str) -> None:
        m = sc.SERVICE_RE.fullmatch(service)
        if m:
            out.setdefault(m.group(2), {}).setdefault(m.group(1), set()).add(
                (norm_span_name(name, m.group(1)), kind.lower().replace("span_kind_", "")))
    for s in series:
        labels = {}
        for kv in s.get("labels") or []:
            v = kv.get("value") or {}
            labels[kv.get("key")] = next(iter(v.values()), "") if isinstance(v, dict) else v
        add(str(labels.get("resource.service.name", "")), str(labels.get("name", "")), str(labels.get("kind", "")))
    for spans in traces.values():
        for s in spans:
            add(s.service, s.name, s.kind)
    return out


def counter_of(metric: str) -> str | None:
    """The counter that says whether ``metric`` had traffic: itself (``_total``,
    ``_count``), the ``_count`` of a ``_sum``; None for a gauge."""
    if metric.endswith(("_total", "_count")):
        return metric
    if metric.endswith("_sum"):
        return metric[:-4] + "_count"
    return None


def metric_traffic(src: Sources, t1: float, t2: float) -> tuple[dict, str]:
    """(role, lang, counter name) -> True when a series of that counter grew over
    (t1, t2] in that job (instant values at t1 and t2: a series new at t2, or a
    reset, counts as traffic)."""
    q = sp.name_sel(".+(_count|_total)", {"job": ("=~", f"{sp.APP_NS}/{sp.DEMO_SERVICE_RE}")})
    at = {}
    for when, ts in (("t1", t1), ("t2", t2)):
        for s in sp.cluster_only(src.prom(q, ts)):
            m = s.get("metric") or {}
            at.setdefault(when, {})[tuple(sorted(m.items()))] = float(s["value"][1])
    out: dict[tuple, bool] = {}
    for key, v2 in (at.get("t2") or {}).items():
        m = dict(key)
        jm = re.fullmatch(rf"{re.escape(sp.APP_NS)}/demo-(go|python|node|dotnet)-(api|backend|worker)", m.get("job", ""))
        if not jm:
            continue
        v1 = (at.get("t1") or {}).get(key)
        grew = v2 > 0 if v1 is None else (v2 != v1)
        k = (jm.group(2), jm.group(1), m.get("__name__", ""))
        out[k] = out.get(k, False) or grew
    return out, q


def parity_items(metrics: dict, spans: dict, rows: list[dict], langs, entries: list[dict],
                 accept_proposed: bool = False, traffic: dict | None = None, run: str | None = None) -> list[dict]:
    """Every difference: documented (runtime family, or an allowlist entry),
    FAIL (with a candidate gaps row) or UNMEASURED (a label-key difference where
    a language had no traffic for the metric in the window)."""
    items = []

    def judge(it: dict, ctx: str = "", hint_item: str | None = None) -> None:
        rt = runtime_family(it["item"], it["have"]) if it["kind"] == "metric" else None
        um = None if rt else recorded_unmeasured(it, entries, run)
        e = None if rt or um else allowed(it, entries, accept_proposed)
        if um:
            it.update(status="UNMEASURED", row=None, reason=f"{um['reason']} ({um.get('ruling', 'no ruling')})")
        elif rt:
            it.update(status="documented", row=rt)
        elif e:
            it.update(status="documented", row=f"sr10-gaps.json ({e['status']}): {e['gaps_ref']}")
        else:
            it.update(status="FAIL", row=None,
                      candidate=candidate_row(hint_item or it["item"], it["have"], it["missing"], rows, context=ctx))
        items.append(it)
    for role, per in sorted(metrics.items()):
        names = sorted(set().union(*[set(v) for v in per.values()]))
        for n in names:
            have = [lg for lg in langs if n in per.get(lg, {})]
            if len(have) < len(langs):
                judge({"kind": "metric", "role": role, "item": n, "have": have,
                       "missing": [lg for lg in langs if lg not in have]})
            if len(have) < 2:
                continue
            counter = counter_of(n)
            idle = [lg for lg in have if traffic is not None and counter and not traffic.get((role, lg, counter))]
            for k in sorted(set().union(*[per[lg][n] for lg in have])):
                hk = [lg for lg in have if k in per[lg][n]]
                if len(hk) == len(have):
                    continue
                it = {"kind": "metric attr", "role": role, "item": f"{n} {k}", "metric": n, "attribute": k,
                      "have": hk, "missing": [lg for lg in have if lg not in hk]}
                if idle:
                    it.update(status="UNMEASURED", row=None,
                              reason=f"no traffic for {counter} in the window for {', '.join(idle)}: "
                                     "label keys not compared")
                    items.append(it)
                    continue
                judge(it, ctx=attr_context(n), hint_item=k)
    for role, per in sorted(spans.items()):
        for name, kind in sorted(set().union(*per.values())):
            have = [lg for lg in langs if (name, kind) in per.get(lg, set())]
            if len(have) < len(langs):
                judge({"kind": "span", "role": role, "item": name, "span_kind": kind, "have": have,
                       "missing": [lg for lg in langs if lg not in have]}, hint_item=name)
    return items


def parity_check(src: Sources, guard: TempoGuard, langs, t1: float, t2: float, traces: dict[str, list], gate: dict,
                 rows: list[dict], entries: list[dict], accept_proposed: bool = False,
                 allowlist: Path = SR10_GAPS, run: str | None = None) -> tuple[dict, list[str]]:
    match = sp.sel("", {"job": ("=~", f"{sp.APP_NS}/{sp.DEMO_SERVICE_RE}"), "__name__": ("!~", ".+_bucket")})
    queries = [f"series {match}"]
    series = src.series(match, t1, t2)
    md = src.metadata()
    metrics = metric_sets(series)
    traffic, tq_prom = metric_traffic(src, t1, t2)
    queries.append(f"{tq_prom} at t1 and t2")
    names = sorted({n for per in metrics.values() for v in per.values() for n in v})
    units = {n: sorted({e.get("unit", "") for e in md.get(n) or []}) for n in names if md.get(n)}
    tq = ('{resource.service.name=~"demo-(go|python|node|dotnet)-(api|backend|worker)"} | count_over_time() '
          'by (resource.service.name, name, kind)')
    queries.append(f"tempo metrics {tq}")
    span_note = ""
    try:
        tser = guard.metrics(tq, int(t1), int(t2), max(1, int(t2 - t1)))
    except (TempoError, TempoStopped) as exc:
        tser, span_note = [], f"TraceQL metrics: {exc}"
    spans = span_sets(tser, traces)
    if not spans:
        span_note = span_note or "no span of the window in Tempo (TraceQL metrics and fetched traces empty)"
    items = parity_items(metrics, spans, rows, langs, entries, accept_proposed, traffic, run)
    fails = [i for i in items if i["status"] == "FAIL"]
    unmeasured = [i for i in items if i["status"] == "UNMEASURED"]
    if fails:
        verdict = "FAIL"
    elif accept_proposed:
        verdict = "PREVIEW"
    elif not metrics or not spans or unmeasured:
        verdict = "UNMEASURED"
    else:
        verdict = "PASS"
    try:
        shown = str(Path(allowlist).resolve().relative_to(REPO))
    except ValueError:
        shown = str(allowlist)
    out = {"verdict": verdict, "items": items, "metric_jobs": sum(len(v) for v in metrics.values()),
           "series": len(series), "spans_source": "TraceQL metrics + fetched traces", "spans_note": span_note,
           "documented": sum(1 for i in items if i["status"] == "documented"), "fail": len(fails),
           "unmeasured": len(unmeasured), "allowlist": shown, "accept_proposed": accept_proposed,
           "units_report_only": units,
           "units_note": ("units are compared by the metric name's unit suffix only (the OTLP -> Prometheus "
                          "translation); /api/v1/metadata is global per name, not per job, so its units are "
                          "recorded report-only"),
           "traffic_rule": ("label keys of a role and metric are compared only when every language that has the "
                            "metric had traffic for it in the window (its counter grew over (t1, t2]); else the "
                            "item is UNMEASURED"),
           "normalization": ("per app role; span names demo:go:, demo:python:, demo:node:, demo:dotnet: -> "
                             "demo:<lang>:; metric names and label keys as stored")}
    if accept_proposed:
        out["preview"] = ("--sr10-accept-proposed: proposed allowlist entries pass; the verdict is PREVIEW, never "
                          "PASS, and extras never exits 0")
    if not metrics:
        out["reason"] = "no demo series in the window"
    elif not spans:
        out["reason"] = f"spans unmeasured: {span_note}"
    elif unmeasured and not fails:
        out["reason"] = f"{len(unmeasured)} label-key comparison(s) without traffic"
    if not gate["clean"] and verdict != "UNMEASURED":
        out.update(measured=verdict, verdict="UNQUALIFIED", reason="shared sentinels: " + "; ".join(gate["reasons"]))
    return out, queries


# ── check 5: per-severity Loki counts (AG6, report-only) ──


def severity_counts(src: Sources, t1: float, t2: float, kcal: dict) -> tuple[dict, str]:
    w = int(t2 - math.ceil(t1))
    q = (f'sum by (service_name, severity_number) (count_over_time({{k8s_namespace_name={verify._quote(sp.APP_NS)}, '
         f'service_name=~{verify._quote(sp.DEMO_SERVICE_RE)}}} | service_instance_id != "" [{w}s]))')
    out: dict[str, dict] = {}
    for r in src.loki_query(q, int(t2)):
        m = r.get("metric") or {}
        out.setdefault(m.get("service_name", ""), {})[m.get("severity_number", "")] = int(float(r["value"][1]))
    exp = kcal.get("log_severity") or kcal.get("severity")
    return {"verdict": "REPORT-ONLY", "services": out,
            "expected": exp if exp else "none: soak-cal.json carries no per-severity expectation"}, q


# ── driver ──


def summary_rows(data: dict) -> list[tuple[str, str, str]]:
    """(item, verdict, values) per check and language, as ``soak count`` prints."""
    rows = []
    for lang, v in sorted(data["graph"].items()):
        t, f, s = v["total"], v["failed"], v["share"]
        if t.get("counted") is None:
            rows.append((f"graph {lang} api->backend", v["verdict"], "; ".join(v.get("reasons") or [])))
            continue
        rows.append((f"graph {lang} api->backend", v["verdict"],
                     f"read at {v.get('read_at_iso')}; share {s.get('observed', '-')} vs {s.get('expected', '-')}"
                     f" +- {SIGMA_K:g} sigma {s.get('sigma', '-')} ({s['verdict']}); report-only counts: total"
                     f" {sc._fmt(t['counted'])} in [{sc._fmt(t.get('lo', '-'))}, {sc._fmt(t.get('hi', '-'))}]"
                     f" ({t['verdict']}), failed {sc._fmt(f['counted'])} in [{sc._fmt(f.get('lo', '-'))},"
                     f" {sc._fmt(f.get('hi', '-'))}] ({f.get('verdict', '-')}), upper ends include the worker->api"
                     f" growth {sc._fmt(v['worker_api_growth']['total'])}"
                     + (f"; {'; '.join(v['reasons'])}" if v.get("reasons") else "")))
    for name, n in sorted((data.get("graph_edges_report_only") or {}).items()):
        rows.append((f"graph {name}", "REPORT-ONLY", f"increase {sc._fmt(n)} over the window (gateway, all edges)"))
    c = data["canary"]
    for r in c["sources"]:
        rows.append((f"canary {r['source']}", "FAIL" if r["hits"] else ROW_VERDICT.get(r["status"], "UNMEASURED"),
                     f"{r['status']}, lines {r['lines']}" + (f"; hits {len(r['hits'])}" if r["hits"] else "")
                     + (f"; {r['reason']}" if r.get("reason") else "")))
    rows.append(("canary (overall)", c["verdict"], f"{len(c['sources'])} source(s)"))
    tg = data.get("tempo") or {}
    rows.append(("tempo reads", "REPORT-ONLY",
                 f"{tg.get('reads', 0)} read(s), {tg.get('errors', 0)} error(s), memory max {tg.get('mem_max_gib')} GiB"
                 f" (stop {tg.get('mem_stop_gib')} GiB), rate {tg.get('rate_per_min')}/min"
                 + (f"; {tg['stopped']}" if tg.get("stopped") else "")))
    for lang, v in sorted(data["orphans"].items()):
        rows.append((f"orphans {lang}", v["verdict"],
                     f"N {v.get('n')}; fetched {v.get('fetched', 0)}, missing {v.get('missing', 0)}, "
                     f"errors {v.get('errors', 0)}, stopped {v.get('stopped', 0)}, orphans {v.get('orphans', 0)}"
                     + (f"; {v['reason']}" if v.get("reason") else "")
                     + (f"; {'; '.join(v['problems'][:3])}" if v.get("problems") else "")))
    p = data["parity"]
    rows.append(("parity (SR10)", p["verdict"], f"items {len(p['items'])}: documented {p['documented']}, "
                 f"FAIL {p['fail']}, UNMEASURED {p.get('unmeasured', 0)}"
                 + (f"; {p['reason']}" if p.get("reason") else "")
                 + (f"; {p['preview']}" if p.get("preview") else "")))
    for i in p["items"]:
        if i["status"] in ("FAIL", "UNMEASURED"):
            rows.append((f"parity {i['role']} {i['kind']} {i['item']}", i["status"],
                         f"have {','.join(i['have']) or '-'}; missing {','.join(i['missing']) or '-'}"
                         + (f"; {i['reason']}" if i.get("reason") else "")
                         + (f"; heuristic candidate gaps row: {i['candidate']}" if i.get("candidate") else "")))
    for svc, sev in sorted(data["logs"]["services"].items()):
        rows.append((f"logs {svc}", "REPORT-ONLY", ", ".join(f"sev {k}: {n}" for k, n in sorted(sev.items()))))
    return rows


def action_extras(src: Sources, args, langs, canary: str | None, now: float, inputs: dict,
                  runs_dir: Path = sp.RUNS_DIR, guard: TempoGuard | None = None) -> dict:
    t1, t2, k6_log = inputs["t1"], inputs["t2"], inputs["k6_log"]
    cells = sc._read_cells(k6_log)
    _, records = k6script.parse_logs(Path(k6_log).read_text())
    kcal = sc._load_json(args.kcal)
    allowlist = Path(getattr(args, "sr10_gaps", None) or SR10_GAPS)
    entries = load_allowlist(allowlist)
    guard = guard or TempoGuard(src, args.tempo_rate, args.tempo_mem_stop * 2**30)
    gate = shared_gates(src, t1, t2)
    cr = gateway_cr(src)
    agent = collector_cr(src, "otel-agent")
    gw = gateway_sets_id(cr)
    k6_end = inputs["k6_end"] if inputs["k6_end"] is not None else t2
    after, after_note = flush_after(cr, k6_end, agent)
    cap = t2 + args.settle
    qg, sg_series = read_graph(src, t1, cap)
    reads = graph_reads(sg_series, langs, after, cap)
    t_max = max([t for t, _ in reads.values() if t is not None], default=t2)
    graph_gate = shared_gates(src, t1, t_max) if t_max > t2 else gate
    graph = graph_verdicts(sg_series, cells, kcal, langs, t1, reads, graph_gate)
    qe, edges = edge_store(src, t1, t2)
    orphans, traces = orphan_check(guard, records, langs, t1, t2, args.orphan_sample, gate, gw, cells)
    parity, pq = parity_check(src, guard, langs, t1, t2, traces, gate, gap_rows(), entries,
                              getattr(args, "sr10_accept_proposed", False), allowlist, args.run)
    logs, lq = severity_counts(src, t1, t2, kcal)
    if canary is None:
        canary_out = {"verdict": "UNMEASURED", "sources": [{"source": f"runs/{args.run}/canary", "status": "unmeasured",
                                                             "reason": "no canary file", "lines": 0, "hits": []}]}
    else:
        loki_rows = canary_loki(src, canary, int(t1), int(math.ceil(t2 + args.settle)))
        coll, stable = canary_collectors(src, canary, t1, now)
        srcs = loki_rows + canary_demo(runs_dir / args.run, canary, src, t1, t2, stable) + coll
        srcs.append(canary_spans(traces, canary))
        srcs.append(canary_file(k6_log, canary, f"k6 log {Path(k6_log).name}"))
        canary_out = {"verdict": canary_verdict(srcs), "sources": srcs,
                      "window": f"[{sp.iso(t1)}, {sp.iso(t2 + args.settle)}] (settle {args.settle:g}s)"}
    findings = [f"orphans {lang}: {v['finding']}" for lang, v in orphans.items() if v.get("finding")]
    if any(v["verdict"] == "UNMEASURED" for v in orphans.values()):
        reasons = sorted({v.get("reason", "") for v in orphans.values() if v["verdict"] == "UNMEASURED"})
        findings.append("orphans unmeasured: " + "; ".join(reasons))
    return {"window": f"({sp.iso(t1)}, {sp.iso(t2)}]", "t1": t1, "t2": t2, "k6_end": k6_end,
            "k6_end_source": "input" if inputs["k6_end"] is not None else "t2 (no --k6-end, no count-window k6_end)",
            "gates": {"shared": gate, "graph": {**graph_gate, "window": f"({sp.iso(t1)}, {sp.iso(t_max)}]"}},
            "graph": graph,
            "graph_read": {"after": after, "after_iso": sp.iso(after), "rule": after_note, "cap": cap,
                           "cap_iso": sp.iso(cap), "flush_s": SG_FLUSH_S,
                           "expiry": f"{SG_EXPIRY_S:g}s idle eviction, hard-coded (servicegraph connector.go:815-849, "
                                     f"v0.165.0; no metrics_expiration field)"},
            "graph_edges_report_only": edges, "canary": canary_out, "orphans": orphans,
            "orphan_sample": args.orphan_sample, "gateway_id": gw[1], "parity": parity, "logs": logs,
            "tempo": guard.stats(), "findings": findings, "queries": [qg, qe, *pq, lq]}


def overall_ok(data: dict) -> bool:
    verdicts = [v["verdict"] for v in data["graph"].values()] + [v["verdict"] for v in data["orphans"].values()]
    verdicts += [data["canary"]["verdict"], data["parity"]["verdict"]]
    return all(v in JUDGED_OK for v in verdicts)


def _dump(data: dict) -> str:
    return json.dumps(data, indent=1, sort_keys=True, default=lambda o: sorted(o) if isinstance(o, set) else str(o))


def main_extras(r, args, d: Path, langs, now: float, src: Sources | None = None,
                runs_dir: Path = sp.RUNS_DIR) -> int:
    _SECRET.clear()
    try:
        canary = read_canary(args.run, runs_dir)
        if canary:
            _SECRET.append(canary)
        inputs = resolve_inputs(args, runs_dir)
        if inputs["missing"]:
            print(f"verify soak extras: required {' '.join(inputs['missing'])} (or --count-window)", file=sys.stderr)
            return 2
        src = src or Sources(r)
        data = action_extras(src, args, tuple(langs), canary, now, inputs, runs_dir)
        text = _dump(data)
        if canary and contains(text, canary):
            raise sc.ScanError("soak-extras.json carries the canary: not written")
        if canary:
            data["canary"]["sources"].append({"source": "soak-extras.json (before writing)", "status": "scanned",
                                              "lines": len(text.splitlines()), "hits": []})
        data["rows"] = summary_rows(data)
        text = _dump(data)
        if canary and contains(text, canary):
            raise sc.ScanError("soak-extras.json carries the canary: not written")
        out = sc._write_json(d, "extras", json.loads(text), getattr(args, "output", None))
        if canary and contains(Path(out).read_text(), canary):
            raise sc.ScanError("soak-extras.json as written carries the canary")
    except InputError as exc:
        print(f"verify soak extras: {redact(exc)}", file=sys.stderr)
        return 2
    except FileExistsError:
        print(f"verify soak extras: --output {redact(args.output)} exists; not overwritten", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — every error text is redacted before it is printed
        print(f"verify soak extras: ERROR {exc.__class__.__name__}: {redact(exc, 400)}", file=sys.stderr)
        return 1
    finally:
        _SECRET.clear()
    for item, verdict, values in data["rows"]:
        print(f"{verdict:<12} {item}: {values}", flush=True)
    ok = overall_ok(data)
    print(f"verify soak extras: {'PASS' if ok else 'FAIL'} ({out})", flush=True)
    return 0 if ok else 1


def add_arguments(p) -> None:
    p.add_argument("--count-window", type=Path, default=None,
                   help="extras: the run's count-window.json (t1, t2, k6_end and k6_log; flags win; "
                        "valid: false is refused)")
    p.add_argument("--k6-end", type=float, default=None,
                   help="extras: k6's end (epoch s; default count-window k6_end, else t2): the graph read "
                        "follows the first connector flush after it")
    p.add_argument("--settle", type=float, default=float(DEFAULT_SETTLE_S),
                   help=f"extras: canary Loki scan and graph read cap at t2 + settle (default {DEFAULT_SETTLE_S}s)")
    p.add_argument("--orphan-sample", type=int, default=DEFAULT_SAMPLE,
                   help=f"extras: N, traces checked per cell (default {DEFAULT_SAMPLE})")
    p.add_argument("--tempo-rate", type=float, default=DEFAULT_TEMPO_RATE,
                   help=f"extras: Tempo reads per minute, sequential (default {DEFAULT_TEMPO_RATE:g})")
    p.add_argument("--tempo-mem-stop", type=float, default=DEFAULT_TEMPO_MEM_STOP / 2**30,
                   help="extras: stop Tempo reads when tempo-0's working set reaches this many GiB "
                        f"(default {DEFAULT_TEMPO_MEM_STOP / 2**30:g})")
    p.add_argument("--sr10-gaps", type=Path, default=None,
                   help="extras: the SR10 allowlist (default testing/k8s/soak/sr10-gaps.json)")
    p.add_argument("--rejudge", type=Path, default=None,
                   help="extras: re-judge the SR10 items of this saved soak-extras.json against the allowlist, "
                        "offline (no backend read); prints the counts; writes --output when given")
    p.add_argument("--sr10-accept-proposed", action="store_true",
                   help="extras: preview: proposed sr10-gaps.json entries pass; the verdict is PREVIEW and the "
                        "exit is never 0 (needs-row entries never pass)")


