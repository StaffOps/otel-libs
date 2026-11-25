"""``verify.py platform``: the soak's platform snapshot and entry gate
(specs/soak-simulation task K1, r6.2 with the K0e corrections; design K-D12,
K-D13, K-D18).

One run, read-only on the cluster:
  - records the platform: the three Collector CRs and the ``soak-agent``
    ConfigMap (credentials stripped), Prometheus ``spec.otlp``, retention and
    the PVC capacity against the CR request, backend and mesh versions and
    digests, the namespace ambient label, the gateway ``span_metrics``
    settings, the Tempo and Loki limits, the node's allocatable and requests,
    the roll list (pod names and start times), the K8a runtime-metric names;
  - runs the mesh and platform guard (a)-(h) of SR15 and the sentinels over
    the gate lookback (30 min), so it serves as the entry gate (K0e B1);
  - reads the baseline (24 h) of every sentinel and guard counter, including
    ``istio_tcp_connections_failed_total`` on the two telemetry edges (B2);
  - reads 1 h of node MemAvailable, idle CPU and root filesystem free space
    and derives the K-D13 floors;
  - checks that PID 1 is the app in every demo container (one ``kubectl
    exec`` per container that reads ``/proc/1/comm`` and ``/proc/1/cmdline``,
    the named read-only exec of SR15 "Checker");
  - measures the Tempo resource-attribute set of the demo services and writes
    the selectors the later checks use.
Every query on a shared job carries ``cluster="k8s01"`` (K0e H5) and every
result is filtered to that cluster again. Output goes to the git-ignored
``testing/k8s/soak/runs/<run>/``; ``snapshot.json`` there is what later tasks
read. Exit 0 when every item passes, 1 otherwise.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "demo"))
sys.path.insert(0, str(HERE))
import verify  # noqa: E402
from preflight import node_free  # noqa: E402

CLUSTER = "k8s01"
APP_NS = "dev-otel-libs"
MESH_NAMESPACES = ("dev-otel-libs", "opentelemetry", "monitoring")
# Guard (c) counts a refused HBONE line only when one side is the run's services or the
# Collector path (owner ruling 2026-11-25): monitoring alone (Prometheus scraping another
# namespace's pod) is not the soak's path.
RUN_PATH_NAMESPACES = ("dev-otel-libs", "opentelemetry")
COPY_JOB = "dev-otel-libs/soak-agent"
AGENT_JOB = "otel-agent-collector-monitoring"
GATEWAY_JOB = "otel-gateway-collector-monitoring"
ZTUNNEL_JOB = "istio-system/ztunnel"
SIGNALS = ("spans", "metric_points", "log_records")
COLLECTOR_CRS = (("opentelemetry", "otel-agent"), ("opentelemetry", "otel-gateway"),
                 ("opentelemetry", "otel-logs"))
DEMO_SERVICE_RE = "demo-(go|python|node|dotnet)-(worker|api|backend)"
DEMO_SERVICES = tuple(f"demo-{lang}-{app}" for lang in verify.LANGS for app in verify.DEMO_APPS)
RUNS_DIR = HERE / "soak" / "runs"
# A run id: a valid label value (shared with testing/k8s/soak, which labels
# every soak-run object otel-libs/soak-run=<run>). Match with fullmatch.
RUN_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
IMAGE_DIGEST_RE = re.compile(r"[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+(:[A-Za-z0-9_.-]+)?@sha256:[0-9a-f]{64}$")

# Every raw read reaches back this far before its window's start (BORN_GRACE +
# one scrape), so a series that existed before the start has a sample there and a
# series with none was born inside the window (K-loop review H2).
BORN_GRACE = 120
# The ztunnel / Collector scrape interval: an initial value dated 2026-11-23,
# `unmeasured` (not read from the target: the reader has no cheap targets call;
# K1's next snapshot records it and replaces this).
SCRAPE_S = 30
# A series "born" in a window that has a sample in this hour before the start
# only had a scrape gap: reset-unknown, not an increase (re-review M1).
HISTORY_S = 3600
# Guard (a): a pod whose Running or deletion transition is younger than this may
# not be in ztunnel's scraped count yet (2 x the scrape interval, review H3).
PROXY_SETTLE_S = 2 * SCRAPE_S

# The roll list of SR15 "Windows" plus the mesh pods of guard (d):
# (namespace, pod-name regex, role). Each role must match >= 1 Running pod.
ROLL_LIST = (
    (APP_NS, r"soak-agent-[a-z0-9]+-[a-z0-9]+", "soak-agent"),
    ("opentelemetry", r"otel-agent-collector-[a-z0-9]+", "agent"),
    ("opentelemetry", r"otel-gateway-collector-[a-z0-9]+-[a-z0-9]+", "gateway"),
    ("monitoring", r"tempo-0", "tempo"),
    ("monitoring", r"loki-0", "loki"),
    ("monitoring", r"prometheus-kps-0", "prometheus"),
    ("monitoring", r"kube-state-metrics-[a-z0-9]+-[a-z0-9]+", "kube-state-metrics"),
    ("istio-system", r"ztunnel-[a-z0-9]+", "ztunnel"),
    ("istio-system", r"istio-cni-node-[a-z0-9]+", "istio-cni-node"),
    ("istio-system", r"istiod-[a-z0-9]+-[a-z0-9]+", "istiod"),
)
# Other pods whose versions are recorded (not in the roll list).
VERSION_ONLY = (
    ("opentelemetry", r"otel-logs-collector-[a-z0-9]+", "logs-collector"),
    ("opentelemetry", r"opentelemetry-operator-[a-z0-9]+-[a-z0-9]+", "otel-operator"),
)

# The run's two telemetry edges (guard (b), K0e B2): (name, matchers).
TELEMETRY_EDGES = (
    ("demo->soak-agent", {
        "source_workload_namespace": APP_NS, "source_workload": ("=~", DEMO_SERVICE_RE),
        "destination_workload_namespace": APP_NS, "destination_workload": "soak-agent"}),
    ("soak-agent->otel-agent-collector", {
        "source_workload_namespace": APP_NS, "source_workload": "soak-agent",
        "destination_workload_namespace": "opentelemetry",
        "destination_workload": "otel-agent-collector"}),
)
EDGE_LABELS = ("source_workload_namespace", "source_workload", "destination_workload_namespace",
               "destination_workload", "response_flags")

# The K8a runtime-metric names (SR15 "Passive window evidence and gate", r6.2):
# (metric, matchers, the jobs or namespace that must carry it).
RUNTIME_METRICS = (
    *((m, {"job": ("=~", f"{APP_NS}/demo-go-.*")}, 3)
      for m in ("go_goroutine_count", "go_memory_used_bytes")),
    *((m, {"job": ("=~", f"{APP_NS}/demo-dotnet-.*")}, 3)
      for m in ("dotnet_thread_pool_thread_count_total", "dotnet_thread_pool_queue_length_total",
                "dotnet_gc_last_collection_heap_size_bytes")),
    *((m, {"job": ("=~", f"{APP_NS}/demo-python-.*")}, 3)
      for m in ("process_open_file_descriptor_count", "process_runtime_cpython_thread_count",
                "process_memory_usage_bytes")),
    *((m, {"job": "kubelet", "metrics_path": "/metrics/cadvisor", "namespace": APP_NS}, 12)
      for m in ("container_memory_rss", "container_memory_working_set_bytes", "container_threads",
                "container_start_time_seconds")),
    ("kube_pod_container_status_restarts_total", {"job": "kube-state-metrics", "namespace": APP_NS}, 12),
    ("otelcol_process_uptime", {"job": GATEWAY_JOB}, 1),
)

# Initial K-D13 floors (2026-11-19, `unmeasured`) and the hard minimum a derived floor never goes below.
INITIAL_FLOORS = {"mem_available_bytes": 2 * 2**30, "idle_cpu_share": 0.15, "rootfs_free_share": 0.10}
HARD_FLOORS = {"mem_available_bytes": 1 * 2**30, "idle_cpu_share": 0.10, "rootfs_free_share": 0.05}

# PID 1 per language: the process name (/proc/1/comm) the app runs as.
PID1_COMM = {"go": None, "dotnet": re.compile(r"^dotnet$"), "python": re.compile(r"^python[0-9.]*$"),
             "node": re.compile(r"^node$")}
SHELLS = {"sh", "bash", "ash", "dash", "zsh", "tini", "dumb-init", "catatonit", "s6-svscan", "runsv", "init"}
PID1_SCRIPT = 'cat /proc/1/comm; tr "\\0" " " </proc/1/cmdline'

# ── query building (K0e H5) ──


def sel(metric: str, matchers: dict | None = None) -> str:
    """``metric{cluster="k8s01", ...}``. A value is a string (=) or (op, value)."""
    parts = [f"cluster={verify._quote(CLUSTER)}"]
    for key, value in (matchers or {}).items():
        if key == "cluster":
            raise ValueError("the cluster matcher is fixed to k8s01")
        op, val = value if isinstance(value, tuple) else ("=", value)
        if op not in ("=", "!=", "=~", "!~"):
            raise ValueError(f"bad matcher operator {op!r}")
        parts.append(f"{key}{op}{verify._quote(val)}")
    return f"{metric}{{{', '.join(parts)}}}"


def name_sel(name_re: str, matchers: dict | None = None) -> str:
    """``{__name__=~"<re>", cluster="k8s01", ...}``."""
    inner = sel("", {**(matchers or {})})[1:-1]
    return f'{{__name__=~{verify._quote(name_re)}, {inner}}}'


def cluster_only(result: list[dict]) -> list[dict]:
    """Drop every series of another cluster (the Prometheus also holds ``cluster="vps"``)."""
    return [s for s in result if (s.get("metric") or {}).get("cluster") == CLUSTER]


# ── credential stripping ──

SECRET_KEY_RE = re.compile(r"(?i)(password|passwd|passphrase|^pass$|secret|token|authorization|api[-_]?key|bearer|"
                           r"credential|private[-_]?key|client[-_]?key|cookie|signature)")
REFERENCE_KEYS = {"secretKeyRef", "secretRef", "secretName", "credentialsFile"}
USERINFO_RE = re.compile(r"(://)[^/@\s]+@")
AUTH_VALUE_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
DROP_ANNOTATIONS = ("kubectl.kubernetes.io/last-applied-configuration",)
REDACTED = "<redacted>"


def _strip_str(value: str) -> str:
    return AUTH_VALUE_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", USERINFO_RE.sub(rf"\1{REDACTED}@", value))


def strip_credentials(obj):
    """A copy of a Kubernetes object with credentials replaced by ``<redacted>``:
    secret-looking keys, env entries with a secret-looking name, URL userinfo,
    Bearer / Basic values; ``managedFields`` and the last-applied annotation dropped;
    an embedded YAML string is stripped line by line (``strip_text``)."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key == "managedFields":
                continue
            if key == "annotations" and isinstance(value, dict):
                value = {k: v for k, v in value.items() if k not in DROP_ANNOTATIONS}
            if key not in REFERENCE_KEYS and SECRET_KEY_RE.search(key):
                out[key] = REDACTED
                continue
            out[key] = strip_credentials(value)
        if isinstance(out.get("name"), str) and "value" in out and SECRET_KEY_RE.search(out["name"]):
            out["value"] = REDACTED  # an env entry {name: FOO_TOKEN, value: ...}
        return out
    if isinstance(obj, list):
        return [strip_credentials(v) for v in obj]
    if isinstance(obj, str):
        return strip_text(obj) if "\n" in obj else _strip_str(obj)
    return obj


_YAML_KEY_RE = re.compile(r"^(\s*-?\s*)([\"']?)([^:\"'#]+)\2(\s*:\s*)(.*)$")


def strip_text(text: str) -> str:
    """YAML-like text with the value of every secret-looking key redacted (also a
    block under such a key) and URL userinfo / Bearer values masked."""
    out: list[str] = []
    block_indent: int | None = None
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None:
            if line.strip() and indent <= block_indent:
                block_indent = None
            else:
                if line.strip():
                    out.append(" " * indent + REDACTED)
                continue
        m = _YAML_KEY_RE.match(line)
        if m and SECRET_KEY_RE.search(m.group(3)) and m.group(3).strip() not in REFERENCE_KEYS:
            if m.group(5).strip():
                out.append(f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(2)}{m.group(4)}{REDACTED}")
            else:
                out.append(line)
                block_indent = indent
            continue
        out.append(_strip_flow(_strip_str(line)))
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


_FLOW_PAIR_RE = re.compile(r"""([{,]\s*)(["']?)([^:"',{}]+)\2(\s*:\s*)("[^"]*"|'[^']*'|[^,}]*)""")


def _strip_flow(line: str) -> str:
    """Secret-looking keys inside a YAML flow map (``{user: u, password: p}``)."""
    if "{" not in line:
        return line

    def sub(m: re.Match) -> str:
        key = m.group(3).strip()
        if SECRET_KEY_RE.search(key) and key not in REFERENCE_KEYS and m.group(5).strip():
            return f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(2)}{m.group(4)}{REDACTED}"
        return m.group(0)
    return _FLOW_PAIR_RE.sub(sub, line)


# ── counters over raw samples ──


def born_in_window(values: list, start: float) -> bool:
    """No sample before ``start`` and a first value above 0."""
    pts = [(float(t), float(v)) for t, v in values]
    return bool(pts) and not any(t < start for t, _ in pts) and pts[0][1] > 0


def raw_increase(values: list, start: float, count_born: bool = True) -> tuple[float, float | None]:
    """(increase, time of the last sample that increased) over [start, ...] from raw
    samples of one counter series: positive differences summed (from the last sample
    before ``start`` when the read has one), a lower value is a reset and adds its
    value, and a series with no sample before ``start`` was born in the window and
    adds its first value. The caller's range must reach ``lookback_range`` before
    ``start`` (BORN_GRACE + one scrape), so an older series has a sample there."""
    allpts = [(float(t), float(v)) for t, v in values]
    before = [p for p in allpts if p[0] < start][-1:]
    pts = before + [p for p in allpts if p[0] >= start]
    if not pts or (before and len(pts) == 1):
        return 0.0, None
    inc, last = 0.0, None
    if not before and pts[0][1] > 0 and count_born:
        inc, last = pts[0][1], pts[0][0]
    prev = pts[0][1]
    for t, v in pts[1:]:
        step = v - prev if v >= prev else v
        if step > 0:
            inc, last = inc + step, t
        prev = v
    return inc, last


def lookback_range(now: float, start: float) -> int:
    """The raw range (s) of a read judged from ``start``: the window plus the margin."""
    return int(now - start) + BORN_GRACE + SCRAPE_S


def hourly(values: list, start: float, end: float) -> list[float]:
    """The increase per whole hour of [start, end] (the last bucket may be shorter)."""
    out = []
    t = start
    pts = [(float(a), float(b)) for a, b in values]
    while t < end:
        bucket_end = min(t + 3600, end)
        # Include the last sample before the bucket so a step across the edge counts once.
        before = [p for p in pts if p[0] < t][-1:]
        inside = [p for p in pts if t <= p[0] < bucket_end]
        series = before + inside
        inc = 0.0
        for (_, a), (_, b) in zip(series, series[1:]):
            inc += b - a if b >= a else b
        if not before and inside and t == start:
            inc += inside[0][1]
        out.append(inc)
        t = bucket_end
    return out


@dataclass
class CounterVerdict:
    id: str
    scope: str            # copy | shared | guard
    verdict: str          # CLEAN | NOISY | ABSENT | NA
    increase: float = 0.0
    last_increase_at: float | None = None
    series: int = 0
    detail: str = ""
    hourly: list[float] = field(default_factory=list)


def judge_counter(cid: str, scope: str, error_series: list[dict], sibling_present: bool,
                  start: float, end: float, *, idle_ok: bool = False, with_hourly: bool = False,
                  history=None) -> CounterVerdict:
    """CLEAN / NOISY over [start, end]; an absent error counter is 0 only when a
    sibling success metric of the same job is present in the same read (SR15
    "Sentinels (r6.2)"), else ABSENT — or NA when ``idle_ok`` (the copy before the
    apps point at it). A series that looks born in the window but for which
    ``history(metric)`` finds a sample in the hour before the start only had a
    scrape gap: its first value is not counted and the verdict is UNKNOWN
    (reset-unknown; re-review M1) unless a later increase makes it NOISY."""
    if not error_series:
        if sibling_present:
            return CounterVerdict(cid, scope, "CLEAN", detail="absent, sibling present")
        return CounterVerdict(cid, scope, "NA" if idle_ok else "ABSENT",
                              detail="absent and no sibling success metric" + (" (idle)" if idle_ok else ""))
    total, last = 0.0, None
    hours: list[float] = []
    gaps = 0
    for s in error_series:
        vals = s.get("values") or []
        gap = born_in_window(vals, start) and history is not None and history(s.get("metric") or {})
        gaps += gap
        inc, at = raw_increase(vals, start, count_born=not gap)
        total += inc
        if at is not None and (last is None or at > last):
            last = at
        if with_hourly:
            h = hourly(s.get("values") or [], start, end)
            hours = [a + b for a, b in zip(hours, h)] if hours else h
    verdict = "NOISY" if total > 0 else "UNKNOWN" if gaps else "CLEAN"
    return CounterVerdict(cid, scope, verdict, total, last, len(error_series), hourly=hours,
                          detail=f"{gaps} series with a scrape gap at the start (reset unknown)" if gaps else "")


def history_lookup(r, start: float):
    """``history(metric)``: whether that exact series has a sample in the
    ``HISTORY_S`` before ``start`` (one instant query per candidate)."""
    def history(metric: dict) -> bool:
        labels = {k: v for k, v in metric.items() if k not in ("__name__", "cluster")}
        q = f"last_over_time({sel(metric.get('__name__', ''), labels)}[{HISTORY_S}s])"
        return bool(cluster_only(r.prom(q, start - 1)))
    return history


def sentinel_catalog() -> list[dict]:
    """Every sentinel counter of SR15 "Sentinels (r6.2)" with its sibling."""
    out = []
    for job, scope in ((COPY_JOB, "copy"), (AGENT_JOB, "shared"), (GATEWAY_JOB, "shared")):
        for sig in SIGNALS:
            for err, sib in ((f"otelcol_receiver_refused_{sig}", f"otelcol_receiver_accepted_{sig}"),
                             (f"otelcol_receiver_failed_{sig}", f"otelcol_receiver_accepted_{sig}"),
                             (f"otelcol_processor_memory_limiter_refused_{sig}",
                              f"otelcol_processor_memory_limiter_accepted_{sig}"),
                             (f"otelcol_exporter_send_failed_{sig}", f"otelcol_exporter_sent_{sig}"),
                             (f"otelcol_exporter_enqueue_failed_{sig}", f"otelcol_exporter_sent_{sig}")):
                out.append({"id": f"{job}:{err}", "scope": scope, "job": job, "metric": err, "sibling": sib})
    out.append({"id": "tempo:tempo_discarded_spans_total", "scope": "shared", "job": "tempo",
                "metric": "tempo_discarded_spans_total", "sibling": "tempo_distributor_spans_received_total"})
    out.append({"id": "monitoring/loki:loki_discarded_samples_total", "scope": "shared", "job": "monitoring/loki",
                "metric": "loki_discarded_samples_total", "sibling": "loki_distributor_lines_received_total"})
    return out


# ── floors (K-D13, r6.2) ──


def _values(series: list[dict]) -> list[float]:
    return [float(v) for s in series for _, v in (s.get("values") or [])]


def derive_floors(mem: list[float], idle: list[float], fs_free: list[float],
                  footprint: dict | None = None, margin: float = 1.5) -> dict:
    """K-D13 floors from the 1 h reading (rule dated 2026-11-23 after the K-loop
    review H7, for owner review in K-D13): the initial floors (2 GiB, 15 %, 10 %)
    are the lower bound; a reading below them is ``BELOW_INITIAL``, a FAIL. A
    stricter floor comes only from a run footprint K-cal measures (``footprint``,
    per key, in the key's unit): observed_min - footprint x margin when that is
    above the initial floor. Until K-cal, the floor is the initial one."""
    out = {}
    for key, vals in (("mem_available_bytes", mem), ("idle_cpu_share", idle), ("rootfs_free_share", fs_free)):
        initial = INITIAL_FLOORS[key]
        if not vals:
            out[key] = {"floor": None, "verdict": "ABSENT", "initial": initial}
            continue
        low = min(vals)
        floor, basis = initial, "initial (no K-cal footprint)"
        fp = (footprint or {}).get(key)
        if fp is not None:
            candidate = low - fp * margin
            if candidate > initial:
                floor, basis = candidate, f"observed_min - footprint {fp:.4g} x {margin:g}"
            else:
                basis = f"initial (observed_min - footprint x {margin:g} = {candidate:.4g} is not stricter)"
        out[key] = {"observed_min": low, "observed_max": max(vals), "samples": len(vals),
                    "initial": initial, "floor": floor, "basis": basis,
                    "verdict": "PASS" if low >= initial else "BELOW_INITIAL"}
    return out


# ── PID 1 ──


def parse_pid1(stdout: str, lang: str, app: str) -> dict:
    """PID 1 of a demo container from the exec read (comm, then cmdline)."""
    lines = stdout.splitlines()
    comm = lines[0].strip() if lines else ""
    argv = (lines[1].split() if len(lines) > 1 else [])[:2]
    argv = [a.rsplit("/", 1)[-1] for a in argv]  # basenames only: no paths, no flags beyond argv[1]
    if not comm:
        return {"ok": False, "comm": "", "argv": argv, "detail": "no /proc/1/comm read"}
    if comm in SHELLS:
        return {"ok": False, "comm": comm, "argv": argv, "detail": f"PID 1 is {comm}, not the app"}
    want = PID1_COMM[lang] or re.compile(rf"^demo-{app}$")
    ok = bool(want.match(comm))
    return {"ok": ok, "comm": comm, "argv": argv,
            "detail": "PID 1 is the app" if ok else f"PID 1 is {comm}, expected {want.pattern}"}


# ── ztunnel log lines (guard (c)) ──


def _roll_listed(rec: dict) -> bool:
    """Either side is a roll-list pod (the path's own Tempo, Loki, Prometheus,
    kube-state-metrics), by namespace and pod-name regex (guard (c) review M1)."""
    for side in ("src", "dst"):
        ns, wl = rec.get(f"{side}.namespace"), rec.get(f"{side}.workload") or ""
        if any(ns == rns and re.fullmatch(rx, wl) for rns, rx, _role in ROLL_LIST):
            return True
    return False


def ztunnel_refused(lines: list[str], namespaces: tuple[str, ...] = RUN_PATH_NAMESPACES) -> list[dict]:
    """Lines saying ``Connection refused`` to a ``:15008`` address whose source or
    destination is in one of the namespaces (default: the run's services and the
    Collector path, ``RUN_PATH_NAMESPACES``). JSON access-log lines are read by field; other lines by text.
    Returns sanitized hits (time, src/dst namespace and workload only)."""
    hits = []
    for line in lines:
        if "connection refused" not in line.lower() or ":15008" not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            rec = None
        if isinstance(rec, dict):
            nss = {rec.get("src.namespace"), rec.get("dst.namespace")}
            if not nss & set(namespaces) and not _roll_listed(rec):
                continue
            hits.append({k: rec.get(k) for k in ("time", "src.namespace", "src.workload",
                                                 "dst.namespace", "dst.workload")})
        elif any(re.search(rf"(?<![\w-]){re.escape(ns)}(?![\w-])", line) for ns in namespaces):
            hits.append({"time": None, "text": "non-JSON line"})
    return hits


# ── pods ──


def parse_ts(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def container_states(pod: dict) -> list[dict]:
    out = []
    for cs in (pod.get("status") or {}).get("containerStatuses") or []:
        running = (cs.get("state") or {}).get("running") or {}
        out.append({"name": cs.get("name"), "restartCount": cs.get("restartCount", 0),
                    "startedAt": running.get("startedAt"), "image": cs.get("image"),
                    "imageID": cs.get("imageID"), "ready": cs.get("ready", False)})
    return out


def match_pods(pods: list[dict], namespace: str, name_re: str) -> list[dict]:
    rx = re.compile(rf"^{name_re}$")
    return [p for p in pods if (p.get("metadata") or {}).get("namespace") == namespace
            and rx.match((p.get("metadata") or {}).get("name", ""))]


def roll_entry(pods: list[dict], namespace: str, name_re: str, role: str) -> dict:
    found = [p for p in match_pods(pods, namespace, name_re) if (p.get("status") or {}).get("phase") == "Running"]
    return {"role": role, "namespace": namespace,
            "pods": [{"name": p["metadata"]["name"], "startTime": (p.get("status") or {}).get("startTime"),
                      "containers": container_states(p)} for p in found]}


def youngest_start(entry: dict) -> float | None:
    times = [parse_ts(c.get("startedAt")) for p in entry["pods"] for c in p["containers"]]
    times = [t for t in times if t is not None]
    return max(times) if times else None


def terminating(pod: dict) -> bool:
    return bool((pod.get("metadata") or {}).get("deletionTimestamp"))


def _pod_key(p: dict) -> str:
    meta = p.get("metadata") or {}
    return f"{meta.get('namespace')}/{meta.get('name')}"


def _annotated(p: dict) -> bool:
    return ((p.get("metadata") or {}).get("annotations") or {}).get("ambient.istio.io/redirection") == "enabled"


def proxy_counts(pods: list[dict], now: float, previous: set[str] | None = None) -> tuple[int, int, set[str]]:
    """Guard (a): (settled, in transition, annotated Running keys). In
    transition (re-review HIGH-1): an annotated Running pod that is deleting,
    has a container not running yet (a slow image pull), or whose youngest
    ``state.running.startedAt`` or Ready ``lastTransitionTime`` is within
    PROXY_SETTLE_S; an annotated pod that left Running (Succeeded / Failed:
    its newest ``state.terminated.finishedAt``) within PROXY_SETTLE_S; and a
    pod of ``previous`` (the last read's annotated Running pods) that is gone."""
    settled = young = 0
    running: set[str] = set()
    present = {_pod_key(p) for p in pods}
    for p in pods:
        if not _annotated(p):
            continue
        status = p.get("status") or {}
        states = [c.get("state") or {} for c in status.get("containerStatuses") or []]
        if status.get("phase") != "Running":
            ended = [parse_ts((st.get("terminated") or {}).get("finishedAt")) for st in states]
            ended = [t for t in ended if t is not None]
            if ended and now - max(ended) < PROXY_SETTLE_S:
                young += 1
            continue
        running.add(_pod_key(p))
        times = [parse_ts((st.get("running") or {}).get("startedAt")) for st in states]
        ready = [c for c in status.get("conditions") or [] if c.get("type") == "Ready"]
        times.append(parse_ts(ready[0].get("lastTransitionTime")) if ready else None)
        not_running = not states or any("running" not in st for st in states)
        known = [t for t in times if t is not None]
        if terminating(p) or not_running or not known or now - max(known) < PROXY_SETTLE_S:
            young += 1
        else:
            settled += 1
    young += len((previous or set()) - present)
    return settled, young, running


def demo_services(langs) -> tuple[str, ...]:
    return tuple(f"demo-{lang}-{app}" for lang in langs for app in verify.DEMO_APPS)


def demo_pods_check(pods: list[dict], langs, now: float, settle: float) -> tuple[list[dict], list[str]]:
    """Guard (g): the non-Terminating demo pods of ``langs`` and what is wrong with them."""
    demo = []
    for p in pods:
        meta = p.get("metadata") or {}
        m = verify.POD_RE.match(meta.get("name", ""))
        if meta.get("namespace") == APP_NS and m and m.group(1) in langs and not terminating(p):
            demo.append(p)
    bad = []
    for p in demo:
        ok, why = demo_ready(p, now, settle)
        if not ok:
            bad.append(f"{p['metadata']['name']}: {why}")
    return demo, bad


def demo_ready(pod: dict, now: float, settle: float) -> tuple[bool, str]:
    """Guard (g): Ready since >= settle and no container (re)started within it."""
    conds = (pod.get("status") or {}).get("conditions") or []
    ready = [c for c in conds if c.get("type") == "Ready"]
    if not ready or ready[0].get("status") != "True":
        return False, "not Ready"
    since = parse_ts(ready[0].get("lastTransitionTime"))
    if since is None or now - since < settle:
        return False, f"Ready for less than {int(settle)}s"
    for c in container_states(pod):
        started = parse_ts(c["startedAt"])
        if started is None or now - started < settle:
            return False, f"container {c['name']} started within {int(settle)}s"
    return True, f"Ready since {iso(since)}"


# ── small text helpers ──


def yaml_block(text: str, path: list[str]) -> dict[str, str]:
    """Scalar leaves (dotted keys) under ``path`` of an indented YAML dump; enough for
    the Tempo ``/status/config`` and Loki ``/config`` answers. Not a YAML parser."""
    lines = text.splitlines()
    depth, indent, i = 0, -1, 0
    while depth < len(path) and i < len(lines):
        line = lines[i]
        ind = len(line) - len(line.lstrip(" "))
        if line.strip() == f"{path[depth]}:" and ind > indent:
            depth, indent = depth + 1, ind
        elif line.strip() and ind <= indent and depth:
            return {}
        i += 1
    if depth < len(path):
        return {}
    out: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for line in lines[i:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        ind = len(line) - len(line.lstrip(" "))
        if ind <= indent:
            break
        m = re.match(r"^\s*([A-Za-z0-9_.-]+):\s*(.*)$", line)
        if not m:
            continue
        while stack and stack[-1][0] >= ind:
            stack.pop()
        key, value = m.group(1), m.group(2).strip()
        if value == "":
            stack.append((ind, key))
            continue
        out[".".join([k for _, k in stack] + [key])] = value
    return out


def config_value(text: str, key: str) -> str | None:
    """The first ``key: value`` in YAML text (e.g. ``max_stale``)."""
    m = re.search(rf"^\s*{re.escape(key)}:\s*([^\s#]+)", text, re.M)
    return m.group(1) if m else None


def _num(d: Decimal) -> float:
    return float(d)


# ── the reader (cluster I/O, read-only) ──

# A read the API server never answered (connection reset or refused, a timeout, a
# TLS handshake failure) or answered with a 5xx of its own or of its service proxy
# is a transport failure: retried inside one read (soak K-loop correction
# 2026-11-24). Any other answer, an empty result included, is never retried.
TRANSPORT_MARKERS = ("unable to connect to the server", "connection reset", "connection refused",
                     "tls handshake", "i/o timeout", "unexpected eof", "kubectl timed out")
# A permanent TLS failure behind "unable to connect": never retried.
PERMANENT_MARKERS = ("x509", "certificate")
# kubectl's "Error from server (<reason>)" for an HTTP 5xx (of the API server, its
# service proxy or the backend behind it): 500 and any other 5xx without its own
# reason (InternalError), 503 (ServiceUnavailable), 504 (Timeout).
SERVER_5XX_RE = re.compile(r"error from server \((internalerror|serviceunavailable|timeout)\)")
# Pauses (seconds) before the 2nd and 3rd attempt of a read that failed in transport.
READ_RETRY_BACKOFF_S = (1, 3)
# A retry is made only when this much time is left after its pause.
READ_RETRY_MIN_CALL_S = 5
TIMEOUT_MARKER = "kubectl timed out"


def transport_failure(stderr: str) -> bool:
    """True when kubectl's ``stderr`` is a transport failure (see TRANSPORT_MARKERS)."""
    low = stderr.lower()
    if "error from server" in low:  # an HTTP answer: only a 5xx is transport
        return SERVER_5XX_RE.search(low) is not None
    if any(marker in low for marker in PERMANENT_MARKERS):
        return False
    return any(marker in low for marker in TRANSPORT_MARKERS)


class Reader:
    """Every cluster read the mode makes; tests replace it.

    ``retries`` counts the attempts repeated after a transport failure and
    ``retry_error`` keeps the last such failure, so a caller can record them."""

    def __init__(self, b: verify.Backends, context: str, sleep=time.sleep) -> None:
        self.b, self.context = b, context
        self.sleep = sleep
        self.retries = 0
        self.retry_error = ""

    def time_left(self) -> float | None:
        """Seconds left for the current read; None when unbounded."""
        return None

    def retrying(self, call) -> subprocess.CompletedProcess:
        """``call()`` (a CompletedProcess) again after a transport failure, with
        READ_RETRY_BACKOFF_S pauses, while the pause plus READ_RETRY_MIN_CALL_S
        fits in ``time_left()``. A timed-out call is retried only inside a bounded
        read (an unbounded one would wait up to three call timeouts). The last
        answer is returned whatever it is: the caller decides on failure.

        Retries are counted whenever they happen; a caller that records them
        (the soak guard reads) takes the difference over its own read."""
        for pause in (*READ_RETRY_BACKOFF_S, None):
            proc = call()
            err = proc.stderr or ""
            if proc.returncode == 0 or pause is None or not transport_failure(err):
                return proc
            left = self.time_left()
            if left is None and TIMEOUT_MARKER in err.lower():
                return proc
            if left is not None and left <= pause + READ_RETRY_MIN_CALL_S:
                return proc
            self.retries += 1
            self.retry_error = " ".join(strip_text(err).split())[:200]
            self.sleep(pause)
        raise AssertionError("unreachable")  # pragma: no cover

    def prom(self, q: str, at: float) -> list[dict]:
        return self.b.prom_query(q, int(at))

    def prom_range(self, q: str, start: float, end: float, step: int) -> list[dict]:
        return self.b.prom_query_range(q, int(start), int(end), step)

    def prom_series(self, match: str, start: float, end: float) -> list[dict]:
        return self.b.prom_series(match, int(start), int(end))

    def prom_status(self, which: str) -> dict:
        return self.b.prom_status(which)

    def tempo(self, path: str, params: dict) -> dict:
        return self.b._get(self.b.tempo, path, params)

    def tempo_search(self, q: str, start: float, end: float) -> list[dict]:
        return self.b.tempo_search(q, int(start), int(end), limit=1)

    def text(self, service: str, path: str) -> str:
        url = f"/api/v1/namespaces/{self.b.namespace}/services/{service}/proxy{path}"
        return self._run(["get", "--raw", url])

    def kget(self, args: list[str]) -> dict:
        """Read-only ``kubectl get <args> -o json`` (the verb is always ``get``)."""
        proc = self.retrying(lambda: self._proc(["get", *args, "-o", "json"], 60))
        if proc.returncode != 0:
            raise verify.QueryError(f"kubectl get {' '.join(args)}: {proc.stderr.strip() or 'kubectl failed'}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise verify.QueryError(f"kubectl get {' '.join(args)}: not JSON ({exc})") from exc

    def logs(self, namespace: str, pod: str, since_s: int) -> list[str]:
        return self._run(["logs", "-n", namespace, pod, f"--since={since_s}s"]).splitlines()

    def pid1(self, namespace: str, pod: str, container: str) -> str:
        return self._run(["exec", "-n", namespace, pod, "-c", container, "--", "sh", "-c", PID1_SCRIPT])

    def _proc(self, args: list[str], timeout: int) -> subprocess.CompletedProcess:
        cmd = ["kubectl", "--context", self.context, *args]
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:  # no answer in time: transport too
            return subprocess.CompletedProcess(cmd, -1, "", f"kubectl timed out after {timeout}s")

    def _run(self, args: list[str]) -> str:
        proc = self.retrying(lambda: self._proc(args, 120))
        if proc.returncode != 0:
            raise verify.QueryError(f"kubectl {args[0]}: {proc.stderr.strip()[:200] or 'failed'}")
        return proc.stdout


# ── the report ──


class PlatformReport(verify.Report):
    def __init__(self) -> None:
        super().__init__()
        self.items: dict[str, str] = {}

    def check(self, ok: bool, name: str, detail: str) -> bool:
        self.items[name] = "PASS" if ok else "FAIL"
        return super().check(ok, name, detail)

    def na(self, name: str, detail: str) -> None:
        self.items[name] = "NA"
        print(f"NA    {name}: {detail}", flush=True)

    @staticmethod
    def info(name: str, detail: str) -> None:
        print(f"INFO  {name}: {detail}", flush=True)


@dataclass
class Options:
    run: str
    baseline: int = 24 * 3600
    lookback: int = 30 * 60
    floor_window: int = 3600
    ready_settle: int = 600
    expect_copy: bool = False
    fakeotlp: str | None = None
    out_dir: Path | None = None
    skip_exec: bool = False
    langs: tuple[str, ...] = tuple(verify.LANGS)
    previous_proxied: frozenset = frozenset()  # guard (a): last read's annotated Running pods
    skip_a: bool = False  # the soak does not read guard (a) (owner ruling 2026-11-24)


# ── the mode ──


def _safe(rep: PlatformReport, name: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (verify.QueryError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        rep.error(name, exc)
        return None


def snapshot_collectors(r: Reader, rep: PlatformReport, raw: dict) -> dict:
    out = {}
    for ns, name in COLLECTOR_CRS:
        cr = r.kget(["opentelemetrycollectors.opentelemetry.io", "-n", ns, name])
        stripped = strip_credentials(cr)
        raw[f"crs/{name}.json"] = stripped
        spec, status = cr.get("spec") or {}, cr.get("status") or {}
        out[name] = {"mode": spec.get("mode"), "image": spec.get("image"), "version": status.get("version"),
                     "generation": (cr.get("metadata") or {}).get("generation")}
        rep.check(bool(spec.get("config")), f"collector-cr {ns}/{name}",
                  f"mode {spec.get('mode')}, operator version {status.get('version')}, image {spec.get('image')}")
        if name == "otel-gateway":
            sm = ((spec.get("config") or {}).get("connectors") or {}).get("span_metrics") or {}
            sg = ((spec.get("config") or {}).get("connectors") or {}).get("service_graph") or {}
            out["gateway_span_metrics"] = {
                "metrics_expiration": sm.get("metrics_expiration"),
                "metrics_flush_interval": sm.get("metrics_flush_interval"),
                "aggregation_temporality": sm.get("aggregation_temporality"),
                "dimensions": [d.get("name") for d in sm.get("dimensions") or []],
                "resource_metrics_key_attributes": sm.get("resource_metrics_key_attributes"),
                "service_graph_store": sg.get("store"),
            }
            rep.check(bool(sm), "gateway span_metrics",
                      f"expiration {sm.get('metrics_expiration')}, flush {sm.get('metrics_flush_interval')}, "
                      f"{len(sm.get('dimensions') or [])} dimensions")
    cm = r.kget(["configmap", "-n", APP_NS, "soak-agent"])
    data = (cm.get("data") or {})
    text = "\n".join(data.values())
    raw["crs/soak-agent-configmap.yaml"] = strip_text(text)
    out["soak_agent"] = {"max_stale": config_value(text, "max_stale"),
                         "metric_expiration": config_value(text, "metric_expiration"),
                         "fixed_spans": "otel_libs.soak.fixed_spans" in text,
                         "rpc_system_name_clause": 'attributes["rpc.system.name"] == "grpc"' in text}
    rep.check(bool(data) and out["soak_agent"]["fixed_spans"], "soak-agent configmap",
              f"max_stale {out['soak_agent']['max_stale']}, metric_expiration "
              f"{out['soak_agent']['metric_expiration']}, fixed_spans {out['soak_agent']['fixed_spans']}, "
              f"rpc.system.name clause {out['soak_agent']['rpc_system_name_clause']}")
    return out


def snapshot_prometheus(r: Reader, rep: PlatformReport, now: float) -> dict:
    cr = r.kget(["prometheus", "-n", "monitoring", "kps"])
    spec = cr.get("spec") or {}
    otlp = spec.get("otlp") or {}
    req = (((((spec.get("storage") or {}).get("volumeClaimTemplate") or {}).get("spec") or {})
            .get("resources") or {}).get("requests") or {}).get("storage")
    pvc = r.kget(["pvc", "-n", "monitoring", "prometheus-kps-db-prometheus-kps-0"])
    pvc_req = (((pvc.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage")
    cap = ((pvc.get("status") or {}).get("capacity") or {}).get("storage")
    out = {"promoteResourceAttributes": otlp.get("promoteResourceAttributes"),
           "translationStrategy": otlp.get("translationStrategy"),
           "keepIdentifyingResourceAttributes": otlp.get("keepIdentifyingResourceAttributes"),
           "retention": spec.get("retention"), "retentionSize": spec.get("retentionSize"),
           "version": spec.get("version"), "cr_storage_request": req,
           "pvc": {"phase": (pvc.get("status") or {}).get("phase"), "request": pvc_req, "capacity": cap}}
    rep.check(bool(otlp.get("promoteResourceAttributes")), "prometheus spec.otlp",
              f"{len(otlp.get('promoteResourceAttributes') or [])} promoted, {otlp.get('translationStrategy')}")
    findings = []
    if req and cap and req != cap:
        findings.append(f"PVC capacity {cap} differs from the CR request {req} (tracked, not an outage risk: "
                        "local-path does not enforce PVC capacity; the TSDB lives on the node root fs, "
                        "bounded by retentionSize; confirmed 2026-11-23)")
    out["findings"] = findings
    rep.check(out["pvc"]["phase"] == "Bound", "prometheus pvc",
              f"bound {cap} (PVC request {pvc_req}, CR request {req}); retention {spec.get('retention')}, "
              f"retentionSize {spec.get('retentionSize')}" + (f"; finding: {findings[0]}" if findings else ""))
    low = cluster_only(r.prom(sel("prometheus_tsdb_lowest_timestamp_seconds", {"job": "kps-prometheus"}), now))
    blocks = cluster_only(r.prom(sel("prometheus_tsdb_storage_blocks_bytes", {"job": "kps-prometheus"}), now))
    out["lowest_timestamp"] = iso(float(low[0]["value"][1])) if low else None
    out["tsdb_blocks_bytes"] = float(blocks[0]["value"][1]) if blocks else None
    try:
        out["buildinfo"] = {k: v for k, v in r.prom_status("buildinfo").items() if k in ("version", "revision")}
    except verify.QueryError:
        out["buildinfo"] = None
    return out


def snapshot_versions(r: Reader, pods: list[dict], rep: PlatformReport) -> tuple[dict, list[dict]]:
    versions, roll = {}, []
    for ns, name_re, role in ROLL_LIST:
        entry = roll_entry(pods, ns, name_re, role)
        roll.append(entry)
        imgs = sorted({(c["image"], c["imageID"]) for p in entry["pods"] for c in p["containers"]})
        versions[role] = [{"image": i, "imageID": d} for i, d in imgs]
        rep.check(bool(entry["pods"]), f"roll-list {role}",
                  ", ".join(f"{p['name']} start {p['startTime']}" for p in entry["pods"]) or "no Running pod")
    for ns, name_re, role in VERSION_ONLY:
        entry = roll_entry(pods, ns, name_re, role)
        versions[role] = sorted({(c["image"], c["imageID"]) for p in entry["pods"] for c in p["containers"]})
        versions[role] = [{"image": i, "imageID": d} for i, d in versions[role]]
    return versions, roll


def snapshot_node(r: Reader, pods: dict, rep: PlatformReport) -> list[dict]:
    nodes = r.kget(["nodes"])
    free = node_free(nodes, pods)
    out = []
    for n in free:
        out.append({"node": n["node"], "alloc_cpu": _num(n["alloc_cpu"]), "req_cpu": _num(n["req_cpu"]),
                    "alloc_mem": _num(n["alloc_mem"]), "req_mem": _num(n["req_mem"]),
                    "alloc_pods": n["alloc_pods"], "pods": n["pods"]})
        rep.check(True, f"node {n['node']}",
                  f"cpu {n['req_cpu']:.2f}/{n['alloc_cpu']:.2f}, memory "
                  f"{int(n['req_mem']) // 2**20}/{int(n['alloc_mem']) // 2**20} Mi, pods {n['pods']}/{n['alloc_pods']}")
    if not free:
        rep.check(False, "node", "no schedulable Ready node")
    return out


def snapshot_limits(r: Reader, rep: PlatformReport) -> dict:
    out = {}
    try:
        t = r.text("tempo:3200", "/status/config")
        out["tempo"] = {k: v for k, v in yaml_block(t, ["overrides", "defaults"]).items()}
        out["tempo"].update({f"compaction.{k}": v for k, v in yaml_block(t, ["backend_worker", "compaction"]).items()
                             if k == "block_retention"})
        rep.check(bool(out["tempo"]), "tempo limits", ", ".join(
            f"{k}={v}" for k, v in out["tempo"].items()
            if k.split(".")[-1] in ("rate_limit_bytes", "burst_size_bytes", "max_traces_per_user",
                                    "max_bytes_per_trace", "block_retention")))
    except verify.QueryError as exc:
        rep.error("tempo limits", exc)
    try:
        lt = r.text("loki:3100", "/config")
        out["loki"] = yaml_block(lt, ["limits_config"])
        keep = ("ingestion_rate_mb", "ingestion_burst_size_mb", "per_stream_rate_limit",
                "per_stream_rate_limit_burst", "max_global_streams_per_user", "max_line_size",
                "retention_period", "reject_old_samples_max_age")
        rep.check(bool(out["loki"]), "loki limits",
                  ", ".join(f"{k}={out['loki'][k]}" for k in keep if k in out["loki"]))
    except verify.QueryError as exc:
        rep.error("loki limits", exc)
    return {k: {kk: strip_credentials(vv) for kk, vv in v.items()} for k, v in out.items()}


def snapshot_runtime_names(r: Reader, rep: PlatformReport, now: float) -> dict:
    out = {}
    for metric, matchers, want in RUNTIME_METRICS:
        res = cluster_only(r.prom(f"count by (cluster, job, pod, k8s_pod_name) ({sel(metric, matchers)})", now))
        out[metric] = len(res)
        rep.check(len(res) >= want, f"runtime metric {metric}", f"{len(res)} series groups (want >= {want})")
    return out


def snapshot_copy_labels(r: Reader, rep: PlatformReport, now: float) -> dict:
    series = [s for s in r.prom_series(sel("otel_libs_soak_fixed_spans_total", {"job": COPY_JOB}),
                                       now - 2 * 86400, now) if s.get("cluster") == CLUSTER]
    names = sorted({k for s in series for k in s})
    with_id = [s for s in series if s.get("service_instance_id") not in (None, "unset")]
    has_exp = [s for s in with_id if "exported_instance" in s]
    out = {"series_2d": len(series), "label_names": names,
           "series_with_instance_id": len(with_id), "with_exported_instance": len(has_exp),
           "exported_instance_equals_service_instance_id":
               all(s["exported_instance"] == s["service_instance_id"] for s in has_exp)}
    if not with_id:
        out["exported_instance"] = "unmeasured"
        rep.na("copy exported_instance", f"no copy series with a service_instance_id in 2 days ({len(series)} series)")
    else:
        out["exported_instance"] = "present" if len(has_exp) == len(with_id) else "missing"
        rep.check(len(has_exp) == len(with_id) and out["exported_instance_equals_service_instance_id"],
                  "copy exported_instance",
                  f"{len(has_exp)}/{len(with_id)} series with a service_instance_id carry exported_instance "
                  f"(= service_instance_id: {out['exported_instance_equals_service_instance_id']}); "
                  f"series with service_instance_id=\"unset\" carry none")
    return out


def tempo_attributes(r: Reader, rep: PlatformReport, now: float) -> dict:
    start = now - 3600
    base = f'{{resource.service.name=~{verify._quote(DEMO_SERVICE_RE)}}}'
    tags = r.tempo("/api/v2/search/tags", {"scope": "resource", "q": base, "start": int(start), "end": int(now)})
    names = sorted({t for sc in tags.get("scopes") or [] for t in sc.get("tags") or []})
    values = {}
    for tag in ("service.namespace", "k8s.namespace.name", "deployment.environment.name", "cluster",
                "k8s.cluster.name", "telemetry.sdk.language"):
        if tag not in names:
            continue
        ans = r.tempo(f"/api/v2/search/tag/resource.{tag}/values",
                      {"q": base, "start": int(start), "end": int(now)})
        values[tag] = sorted(v.get("value") for v in ans.get("tagValues") or [])
    selectors = {"demo_services": base}
    for key, tag in (("namespace", "k8s.namespace.name"), ("service_namespace", "service.namespace"),
                     ("environment", "deployment.environment.name"), ("cluster", "k8s.cluster.name")):
        if len(values.get(tag, [])) == 1:
            selectors[key] = f"{{resource.{tag}={verify._quote(values[tag][0])}}}"
    checked = {}
    for key, q in selectors.items():
        found = r.tempo_search(q, start, now)
        checked[key] = len(found)
        rep.check(bool(found), f"tempo selector {key}", f"{q} -> {'traces' if found else 'nothing'} in 1 h")
    rep.check("service.instance.id" in names, "tempo resource attributes",
              f"{len(names)} tags: {', '.join(names)}")
    return {"tags": names, "values": values, "selectors": selectors, "selector_hits": checked}


def check_pid1(r: Reader, rep: PlatformReport, pods: list[dict]) -> dict:
    out = {}
    for pod in pods:
        name = pod["metadata"]["name"]
        m = verify.POD_RE.match(name)
        if not m or (pod.get("status") or {}).get("phase") != "Running":
            continue
        lang, app = m.group(1), m.group(2)
        container = ((pod.get("spec") or {}).get("containers") or [{}])[0].get("name", app)
        try:
            res = parse_pid1(r.pid1(APP_NS, name, container), lang, app)
        except (verify.QueryError, subprocess.TimeoutExpired) as exc:
            res = {"ok": False, "comm": "", "argv": [], "detail": f"exec read failed: {str(exc)[:120]}"}
        out[f"{lang}-{app}"] = {"pod": name, "container": container, **res}
        rep.check(res["ok"], f"pid1 {lang}-{app}", f"{res['detail']} (comm {res['comm']!r}, argv {res['argv']})")
    for lang in verify.LANGS:
        for app in verify.DEMO_APPS:
            if f"{lang}-{app}" not in out:
                rep.check(False, f"pid1 {lang}-{app}", "no Running demo pod")
    return out


def read_sentinels(r: Reader, now: float, starts: tuple[float, ...], *,
                   copy_idle: bool) -> list[list[CounterVerdict]]:
    """Every sentinel judged over [start, now] for each start, from one raw read per
    job over the longest window (so a series older than a short window is never
    taken for one born inside it)."""
    cat = sentinel_catalog()
    out: list[list[CounterVerdict]] = [[] for _ in starts]
    span = lookback_range(now, min(starts))
    for job in sorted({c["job"] for c in cat}):
        entries = [c for c in cat if c["job"] == job]
        err_re = "|".join(sorted({c["metric"] for c in entries}))
        sib_re = "|".join(sorted({c["sibling"] for c in entries}))
        raw = cluster_only(r.prom(f"{name_sel(err_re, {'job': job})}[{span}s]", now))
        sibs = {s["metric"].get("__name__") for s in cluster_only(r.prom(name_sel(sib_re, {"job": job}), now))}
        for c in entries:
            series = [s for s in raw if s["metric"].get("__name__") == c["metric"]]
            for i, start in enumerate(starts):
                out[i].append(judge_counter(c["id"], c["scope"], series, c["sibling"] in sibs, start, now,
                                            idle_ok=copy_idle and c["scope"] == "copy", with_hourly=i == 0,
                                            history=history_lookup(r, start)))
    return out


def read_edges(r: Reader, now: float, starts: tuple[float, ...]) -> tuple[list[list[CounterVerdict]], list[dict]]:
    """Guard (b) on the two telemetry edges for each start, and every other failing edge
    that touches the three namespaces over the longest window (report-only, B2)."""
    span = lookback_range(now, min(starts))
    zt_up = cluster_only(r.prom(sel("up", {"job": ZTUNNEL_JOB}), now))
    zt_ok = any(float(s["value"][1]) == 1 for s in zt_up)
    verdicts: list[list[CounterVerdict]] = [[] for _ in starts]
    for name, matchers in TELEMETRY_EDGES:
        q = f"{sel('istio_tcp_connections_failed_total', {'job': ZTUNNEL_JOB, 'reporter': 'source', **matchers})}[{span}s]"
        series = cluster_only(r.prom(q, now))
        for i, start in enumerate(starts):
            verdicts[i].append(judge_counter(f"edge {name}", "guard", series, zt_ok, start, now, with_hourly=i == 0,
                                             history=history_lookup(r, start)))
    others: dict[tuple, dict] = {}
    ns_re = "|".join(MESH_NAMESPACES)
    for side in ("source_workload_namespace", "destination_workload_namespace"):
        q = f"{sel('istio_tcp_connections_failed_total', {'job': ZTUNNEL_JOB, 'reporter': 'source', side: ('=~', ns_re)})}[{span}s]"
        for s in cluster_only(r.prom(q, now)):
            key = tuple(s["metric"].get(k, "") for k in EDGE_LABELS)
            if key in others:
                continue
            if any(all(_matches(s["metric"], k, v) for k, v in m.items()) for _, m in TELEMETRY_EDGES):
                continue
            inc, last = raw_increase(s.get("values") or [], min(starts))
            if inc > 0:
                others[key] = {**dict(zip(EDGE_LABELS, key)), "increase": inc, "last_increase_at": iso(last)}
    return verdicts, sorted(others.values(), key=lambda e: -e["increase"])


def _matches(metric: dict, key: str, value) -> bool:
    op, val = value if isinstance(value, tuple) else ("=", value)
    got = metric.get(key, "")
    return bool(re.fullmatch(val, got)) if op == "=~" else got == val


def guard(r: Reader, rep: PlatformReport, opts: Options, now: float, pods: list[dict],
          roll: list[dict], sentinels_gate: list[CounterVerdict], edges_gate: list[CounterVerdict]) -> dict:
    """The mesh and platform guard (a)-(h) of SR15 at ``now`` with the gate lookback."""
    out = {}
    # (a) proxied workloads vs annotated Running pods (one node). A pod that started
    # running or began deleting within PROXY_SETTLE_S may or may not be in the
    # scraped count yet (review H3): the count must lie in [settled, settled + young].
    if not opts.skip_a:
        settled, young_a, running_a = proxy_counts(pods, now, opts.previous_proxied)
        prox = cluster_only(r.prom(sel("workload_manager_active_proxy_count", {"job": ZTUNNEL_JOB}), now))
        count = sum(float(s["value"][1]) for s in prox) if prox else None
        out["a"] = {"active_proxy_count": count, "annotated_running": settled, "in_transition": young_a,
                    "running_keys": sorted(running_a)}
        rep.check(count is not None and settled <= int(count) <= settled + young_a, "guard (a) ztunnel proxies",
                  f"workload_manager_active_proxy_count {count} vs {settled} annotated Running pods"
                  + (f" (+ up to {young_a} in transition)" if young_a else ""))
    # (b) the two telemetry edges over the lookback.
    for v in edges_gate:
        if v.verdict == "UNKNOWN":  # a scrape gap: neither clean nor an increase (re-review M1)
            rep.error(f"guard (b) {v.id}", verify.QueryError(v.detail))
            continue
        rep.check(v.verdict == "CLEAN", f"guard (b) {v.id}",
                  f"{v.verdict}: increase {v.increase:g} over {opts.lookback}s ({v.detail or f'{v.series} series'})")
    out["b"] = {v.id: v.verdict for v in edges_gate}
    # (c) ztunnel log lines.
    hits = []
    zt = [p for e in roll if e["role"] == "ztunnel" for p in e["pods"]]
    for p in zt:
        try:
            hits += ztunnel_refused(r.logs("istio-system", p["name"], opts.lookback))
        except (verify.QueryError, subprocess.TimeoutExpired) as exc:
            rep.error(f"guard (c) logs {p['name']}", exc)
    out["c"] = {"refused_15008": len(hits), "samples": hits[:5]}
    rep.check(bool(zt) and not hits, "guard (c) ztunnel refused :15008",
              f"{len(hits)} lines in {opts.lookback}s across {len(zt)} ztunnel pods")
    # (d)+(e) roll list unchanged over the lookback.
    young = []
    for e in roll:
        t = youngest_start(e)
        if t is not None and now - t < opts.lookback:
            young.append(f"{e['role']} started {iso(t)}")
        for p in e["pods"]:  # a waiting / crash-looping container (review M8)
            for c in p["containers"]:
                if not c.get("startedAt"):
                    young.append(f"{e['role']} {p['name']}/{c['name']} not running")
    out["d"] = young
    rep.check(not young, "guard (d,e) roll list unchanged",
              "; ".join(young) or f"no container start in the last {opts.lookback}s")
    # (e) sentinels over the lookback (B1 entry gate).
    noisy = [v for v in sentinels_gate if v.verdict == "NOISY"]
    absent = [v for v in sentinels_gate if v.verdict == "ABSENT"]
    na = [v for v in sentinels_gate if v.verdict == "NA"]
    unknown = [v for v in sentinels_gate if v.verdict == "UNKNOWN"]
    out["e_sentinels"] = {"noisy": [v.id for v in noisy], "absent": [v.id for v in absent], "na": [v.id for v in na],
                          "unknown": [v.id for v in unknown]}
    for v in unknown:
        rep.error(f"sentinel {v.id}", verify.QueryError(v.detail))
    rep.check(not noisy and not absent, "gate sentinels clean",
              f"{len(sentinels_gate) - len(noisy) - len(absent) - len(na) - len(unknown)} clean, {len(noisy)} noisy "
              f"{[v.id for v in noisy]}, {len(absent)} absent {[v.id for v in absent]}, "
              f"{len(na)} n/a (idle copy)")
    # (f) scrape targets.
    up = cluster_only(r.prom(sel("up", {"job": ("=~", f"{COPY_JOB}|{AGENT_JOB}|{GATEWAY_JOB}")}), now))
    want = {(COPY_JOB, "metrics"), (COPY_JOB, "count"), (AGENT_JOB, None), (GATEWAY_JOB, None)}
    seen = {}
    for s in up:
        m = s["metric"]
        key = (m.get("job"), m.get("endpoint") if m.get("job") == COPY_JOB else None)
        seen[key] = seen.get(key, True) and float(s["value"][1]) == 1
    missing = sorted(f"{j}{'/' + e if e else ''}" for j, e in want if not seen.get((j, e)))
    out["f"] = {"missing_or_down": missing}
    rep.check(not missing, "guard (f) up == 1", "; ".join(missing) or "soak-agent metrics + count, agent, gateway")
    # (g) demo pods of the run's languages; Terminating pods are skipped (B1).
    services = demo_services(opts.langs)
    demo, bad = demo_pods_check(pods, opts.langs, now, opts.ready_settle)
    out["g"] = {"pods": len(demo), "bad": bad}
    rep.check(len(demo) == len(services) and not bad, "guard (g) demo pods Ready",
              "; ".join(bad) or f"{len(demo)} pods Ready for >= {opts.ready_settle}s")
    # (h) copy count per demo service.
    res = cluster_only(r.prom(f"sum by (cluster, service_name) ({sel('otel_libs_soak_spans_total', {'job': COPY_JOB, 'service_name': ('=~', DEMO_SERVICE_RE)})})", now))
    counted = {s["metric"].get("service_name"): float(s["value"][1]) for s in res}
    missing_h = [s for s in services if counted.get(s, 0) <= 0]
    out["h"] = {"counted": counted, "missing": missing_h}
    if not counted and not opts.expect_copy:
        rep.na("guard (h) copy counts", "no demo service exports to the copy (apps not pointed at soak-otlp)")
    else:
        rep.check(not missing_h, "guard (h) copy counts", f"{len(services) - len(missing_h)}/"
                  f"{len(services)} services counted; missing {missing_h}")
    return out


def read_floors(r: Reader, rep: PlatformReport, now: float, window: int) -> dict:
    start = now - window
    mem = cluster_only(r.prom_range(sel("node_memory_MemAvailable_bytes", {"job": "node-exporter"}), start, now, 60))
    idle = cluster_only(r.prom_range(
        f"avg by (cluster, instance) (rate({sel('node_cpu_seconds_total', {'job': 'node-exporter', 'mode': 'idle'})}[5m]))",
        start, now, 60))
    fs = cluster_only(r.prom_range(
        f"{sel('node_filesystem_avail_bytes', {'job': 'node-exporter', 'mountpoint': '/'})} / "
        f"{sel('node_filesystem_size_bytes', {'job': 'node-exporter', 'mountpoint': '/'})}",
        start, now, 60))
    floors = derive_floors(_values(mem), _values(idle), _values(fs))
    for key, f in floors.items():
        if f["floor"] is None:
            rep.check(False, f"floor {key}", "no samples in the reading")
            continue
        rep.check(f["verdict"] == "PASS", f"floor {key}",
                  f"observed min {f['observed_min']:.4g} over {f['samples']} samples "
                  f"(initial {f['initial']:.4g}: {f['verdict']}) -> floor {f['floor']:.4g}, {f['basis']}")
    floors["window"] = {"start": iso(start), "end": iso(now), "step": 60}
    return floors


def summarize_baseline(verdicts: list[CounterVerdict], start: float, now: float,
                       roll: list[dict] | None = None) -> dict:
    """The baseline verdict, when the counters were last noisy, and when a clean 24 h
    window (counters clean and no roll-list container started) will be available."""
    noisy = [v for v in verdicts if v.verdict == "NOISY"]
    last = max((v.last_increase_at for v in noisy if v.last_increase_at), default=None)
    clean_since = last if last is not None else start
    starts = [t for t in (youngest_start(e) for e in roll or []) if t is not None]
    settled = max([clean_since, *starts])
    return {"start": iso(start), "end": iso(now),
            "clean_over_whole_baseline": not noisy and not any(v.verdict == "ABSENT" for v in verdicts),
            "noisy": {v.id: {"increase": v.increase, "last_increase_at": iso(v.last_increase_at),
                             "hours_nonzero": [i for i, h in enumerate(v.hourly) if h > 0]} for v in noisy},
            "absent": [v.id for v in verdicts if v.verdict == "ABSENT"],
            "na": [v.id for v in verdicts if v.verdict == "NA"],
            "clean_since": iso(clean_since),
            "clean_24h_window_at": iso(clean_since + 24 * 3600),
            "settled_since": iso(settled),
            "settled_24h_window_at": iso(settled + 24 * 3600)}


def main_platform(b: verify.Backends, args, now: float, reader: Reader | None = None) -> int:
    opts = Options(run=args.run, baseline=args.baseline, lookback=args.lookback, floor_window=args.floor_window,
                   ready_settle=args.ready_settle, expect_copy=args.expect_copy, fakeotlp=args.fakeotlp_image,
                   out_dir=args.out_dir, skip_exec=args.skip_exec)
    if not RUN_RE.fullmatch(opts.run):
        print(f"verify platform: --run {opts.run!r} must match {RUN_RE.pattern}", file=sys.stderr)
        return 2
    if opts.fakeotlp and not IMAGE_DIGEST_RE.fullmatch(opts.fakeotlp):
        print("verify platform: --fakeotlp-image must be <registry>/<repo>[:tag]@sha256:<64 hex>", file=sys.stderr)
        return 2
    if opts.lookback <= 0 or opts.baseline < opts.lookback or opts.floor_window <= 0:
        print("verify platform: need 0 < --lookback <= --baseline and --floor-window > 0", file=sys.stderr)
        return 2
    r = reader or Reader(b, args.context)
    rep = PlatformReport()
    out_dir = opts.out_dir or RUNS_DIR / opts.run
    raw: dict[str, object] = {}
    snap: dict = {"schema": 1, "task": "K1", "run": opts.run, "taken_at": iso(now), "context": args.context,
                  "cluster": CLUSTER, "git_head": _git_head(),
                  "parameters": {"baseline_s": opts.baseline, "lookback_s": opts.lookback,
                                 "floor_window_s": opts.floor_window, "ready_settle_s": opts.ready_settle}}
    rep.info("platform", f"run {opts.run}, context {args.context}, cluster {CLUSTER}, at {iso(now)}")

    pods_obj = _safe(rep, "pods", r.kget, ["pods", "-A"]) or {"items": []}
    pods = pods_obj.get("items") or []
    snap["collectors"] = _safe(rep, "collectors", snapshot_collectors, r, rep, raw)
    snap["prometheus"] = _safe(rep, "prometheus", snapshot_prometheus, r, rep, now)
    vr = _safe(rep, "versions", snapshot_versions, r, pods, rep) or ({}, [])
    snap["versions"], snap["roll_list"] = vr
    snap["node"] = _safe(rep, "node", snapshot_node, r, pods_obj, rep)
    nss = _safe(rep, "namespaces", r.kget, ["namespaces", *MESH_NAMESPACES]) or {"items": []}
    labels = {(n.get("metadata") or {}).get("name"): ((n.get("metadata") or {}).get("labels") or {}).get(
        "istio.io/dataplane-mode") for n in nss.get("items") or []}
    snap["namespace_ambient"] = labels
    rep.check(labels.get(APP_NS) == "ambient", f"namespace {APP_NS} ambient",
              ", ".join(f"{k}={v}" for k, v in sorted(labels.items())))
    snap["limits"] = _safe(rep, "limits", snapshot_limits, r, rep)
    snap["runtime_metrics"] = _safe(rep, "runtime metrics", snapshot_runtime_names, r, rep, now)
    snap["copy_labels"] = _safe(rep, "copy labels", snapshot_copy_labels, r, rep, now)
    snap["tempo"] = _safe(rep, "tempo attributes", tempo_attributes, r, rep, now)
    if opts.skip_exec:
        rep.na("pid1", "--skip-exec")
    else:
        snap["pid1"] = check_pid1(r, rep, [p for p in pods if (p.get("metadata") or {}).get("namespace") == APP_NS])
    gw = [p for e in snap["roll_list"] if e["role"] == "gateway" for p in e["pods"]]
    snap["gateway_pod"] = gw[0] if gw else None
    up = _safe(rep, "gateway uptime", r.prom, sel("otelcol_process_uptime", {"job": GATEWAY_JOB}), now)
    snap["gateway_uptime_s"] = float(cluster_only(up)[0]["value"][1]) if up and cluster_only(up) else None
    snap["fakeotlp"] = {"image": opts.fakeotlp}
    if opts.fakeotlp:
        rep.check(True, "fakeotlp image", opts.fakeotlp)
    else:
        rep.na("fakeotlp image", "not given (--fakeotlp-image)")

    # Copy idle = no demo service in the copy's counts: its sentinels without a sibling are NA, not ABSENT.
    copy_idle = not opts.expect_copy and not cluster_only(r.prom(
        sel("otel_libs_soak_spans_total", {"job": COPY_JOB, "service_name": ("=~", DEMO_SERVICE_RE)}), now))
    base_start, gate_start = now - opts.baseline, now - opts.lookback
    sentinels, gate_sent = _safe(rep, "sentinels", read_sentinels, r, now, (base_start, gate_start),
                                 copy_idle=copy_idle) or ([], [])
    (edges, gate_edges), others = _safe(rep, "edges", read_edges, r, now, (base_start, gate_start)) or (([], []), [])
    snap["guard"] = _safe(rep, "guard", guard, r, rep, opts, now, pods, snap["roll_list"], gate_sent, gate_edges)
    snap["baseline"] = summarize_baseline(sentinels + edges, base_start, now, snap["roll_list"])
    snap["baseline"]["edges"] = {v.id: {"verdict": v.verdict, "increase": v.increase,
                                        "last_increase_at": iso(v.last_increase_at), "detail": v.detail}
                                 for v in edges}
    snap["baseline"]["other_edges_failing"] = len(others)
    raw["baseline.json"] = {"counters": [v.__dict__ for v in sentinels + edges], "other_edges": others}
    raw["edges-report-only.json"] = others
    for v in sentinels + edges:
        if v.verdict == "NOISY":
            rep.info(f"baseline {v.id}", f"increase {v.increase:g}, last at {iso(v.last_increase_at)}, "
                     f"hours with increase (0 = oldest) {[i for i, h in enumerate(v.hourly) if h > 0]}")
    bl = snap["baseline"]
    rep.info("baseline", f"{len(bl['noisy'])} noisy, {len(bl['absent'])} absent, {len(bl['na'])} n/a over "
             f"[{bl['start']}, {bl['end']}]; counters clean since {bl['clean_since']}, platform settled since "
             f"{bl['settled_since']} (youngest roll-list start); a clean 24 h baseline is readable from "
             f"{bl['settled_24h_window_at']}; {len(others)} report-only failing edges in the three namespaces")
    snap["floors"] = _safe(rep, "floors", read_floors, r, rep, now, opts.floor_window)
    snap["items"] = rep.items
    snap["verdict"] = "PASS" if not rep.failed else "FAIL"
    _write(out_dir, snap, raw)
    print(f"verify platform: {rep.total - rep.failed}/{rep.total} checks passed; snapshot "
          f"{out_dir / 'snapshot.json'}", flush=True)
    return 1 if rep.failed else 0


def _git_head() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def _write(out_dir: Path, snap: dict, raw: dict) -> None:
    """Writes the run files; refuses one that the secret scan flags (never prints the match)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {"snapshot.json": snap, **raw}
    for rel, content in files.items():
        text = content if isinstance(content, str) else json.dumps(content, indent=1, sort_keys=True, default=str)
        hits = verify.scan_secret(text)
        if hits:
            print(f"verify platform: not writing {rel}: secret scan matched {hits}", file=sys.stderr)
            continue
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + ("" if text.endswith("\n") else "\n"))


def add_parser(sub) -> None:
    """The ``platform`` sub-command of verify.py."""
    p = sub.add_parser("platform", help="soak K1: platform snapshot, entry gate and baselines (read-only)")
    p.add_argument("--run", required=True, help="soak run id (directory under testing/k8s/soak/runs/)")
    p.add_argument("--baseline", type=verify._duration, default=verify._duration("24h"),
                   help="baseline of every sentinel and guard counter (default 24h)")
    p.add_argument("--lookback", type=verify._duration, default=verify._duration("30m"),
                   help="entry-gate lookback (default 30m)")
    p.add_argument("--floor-window", type=verify._duration, default=verify._duration("1h"),
                   help="node floor reading (default 1h)")
    p.add_argument("--ready-settle", type=verify._duration, default=verify._duration("10m"),
                   help="guard (g): demo pods Ready for at least this long (default 10m)")
    p.add_argument("--expect-copy", action="store_true",
                   help="the apps export to the copy: guard (h) and the copy sentinels are required")
    p.add_argument("--fakeotlp-image", default=None, help="the pushed fakeotlp image, <ref>@sha256:<digest>")
    p.add_argument("--out-dir", type=Path, default=None, help="default testing/k8s/soak/runs/<run>/")
    p.add_argument("--skip-exec", action="store_true", help="skip the PID 1 exec read")


if __name__ == "__main__":  # pragma: no cover
    print("run it as: python3 -I testing/k8s/verify.py platform --run <id>", file=sys.stderr)
    sys.exit(2)
