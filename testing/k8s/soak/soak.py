"""The soak run loop on the operator's laptop (specs/soak-simulation task K-loop;
SR15 "Orchestration (r6.2)", "Load (r6.2)", "Mesh and platform guard (r6.2)",
"Evidence (r6.2)"; design K-D14, K-D15, K-D17, K-D18).

Standard library only; run with ``python3 -I testing/k8s/soak/soak.py <mode>``.

Modes:
  preflight --run <id>   refuses while any object labelled otel-libs/soak-run
                         exists (any run id), an unlabelled object carries a
                         soak name, or the namespace label istio.io/dataplane-mode
                         is not ``ambient``; records the running demo (image tag,
                         replicas) and refuses when its tag is not the expected
                         one; then the entry gate (every guard and sentinel clean
                         over 30 min). Reads only.
  run --run <id> --window <name> --duration <s> [--rate <n>] [--langs ...]
                         [--hook] [--worker-interval <s>] [--quiesce <s>] [--allow-short-quiesce] [--tag <t>]
                         the pre-flight and the entry gate (both before any
                         write); ``soak-otlp`` (copy); with --hook the demo
                         re-applied through demo.sh's DEMO_ENV_FILE (endpoint
                         soak-otlp, a per-run canary header, a new
                         DEMO_RESTART_NONCE), then guard (g) polled until the
                         rolled pods are Ready and (h) as an increase since the
                         hook; with --rate the k6 ConfigMap and Job; a guard read
                         every 60 s under ``caffeinate`` (macOS) with a heartbeat
                         until the window ends (with k6: when its Job succeeds,
                         at most its activeDeadlineSeconds); then a guarded
                         quiesce (--quiesce, 120 s) with the demo as is;
                         workers.json (with --hook; the worker window checked
                         before k6 starts) and count-window.json (t1, t2). The
                         trap always runs.
  cal --run <id> [--langs ...] [--n N] [--rate R] [--steps a,b,c]
      [--step-duration S] [--quiesce Q] [--worker-interval I] [--dry-run]
                         K-cal (task K-cal-d, design K-D20): run's reads, hook
                         (always) and trap around cal.py's driver: phase A
                         (per K8b cell two k6 repeats of N requests, quiesced;
                         kcal-input.json, soak-cal.json), worker first drives,
                         phase B (rate steps -> loaded rate, drain rate).
                         --dry-run prints the plan; no kubectl call, nothing
                         written.
  cleanup --run <id> [--tag <t>]
                         the trap's steps for a run whose laptop was cut off.
  render --run <id> --what <object> [...]
                         prints one soak-run object as JSON (offline).
  dry-run --run <id>     against the built-in fake kubectl (fakekube.py): creates
                         one object of each kind, runs the trap, and checks it
                         deleted exactly those and nothing else (offline).

Window validity: a wall-clock gap > 3 x the guard interval since the last
heartbeat invalidates the window (``laptop: wall-clock gap``); a guard breach
invalidates it (``platform: mesh`` / ``platform: guard``) and suspends k6; a
blind read deletes k6, retried with backoff until it succeeds or the Job's
``activeDeadlineSeconds`` has passed; a failed k6 Job, or a k6 pod not running
after the pull allowance, invalidates it (``k6: ...``). Each stop saves
``kubectl logs job/<k6>`` first.

The trap (EXIT, INT, TERM, HUP), each step running whatever the earlier ones
did: (1) save ``kubectl logs job/<k6>`` of every k6 Job of the run to
``runs/<run>/raw/`` (errors to ``<job>.log.err``); (1b) only when the hook was
applied, save ``kubectl logs`` (stdout and stderr, since the hook) of every demo
container to ``raw/<pod>.<container>.log`` before the restore replaces the
pods, counting canary lines (count only, in ``trap.jsonl``); (2) delete only the objects
labelled ``otel-libs/soak-run=<run>`` (never by kind alone); (3) re-read the
namespace label and write ``ambient`` only if it is not (a guarded no-op while
K4 is deferred); (4) only when the hook was applied: ``demo.sh apply --all``
with ``DEMO_RESTORE=1``, the recorded TAG and LANGS and no DEMO_ENV_FILE, then
``demo.sh scale`` back to the recorded replica counts. ``trap.jsonl`` is always
written.

Raw output: ``testing/k8s/soak/runs/<run>/`` (git-ignored). Env: KUBE_CONTEXT
(``k8s01``; another context needs --allow-other-context), NAMESPACE
(``dev-otel-libs``, the only namespace written), SOAK_RUNS_DIR.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cal  # noqa: E402  (K-cal-d: the calibration driver)
import guard  # noqa: E402
import k6script  # noqa: E402
import render  # noqa: E402
import soakcount  # noqa: E402  (K-count: the FD exec sweep, testing/k8s/soakcount.py)
from render import NAMESPACE, ROLE_LABEL, RUN_LABEL  # noqa: E402

DNS1123_LABEL_RE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")

DEMO_SH = HERE.parent / "demo" / "demo.sh"
DEMO_TAG = HERE.parent / "demo" / "demo-tag.sh"
RUNS_DIR = HERE / "runs"
CONTEXT = "k8s01"
# Every kind a soak run writes, plus NetworkPolicy (written by r6.1 runs only):
# the pre-flight reads them all, the trap deletes them by the run's label only.
SOAK_KINDS = "jobs,configmaps,services,pods,networkpolicies"
# The kinds Kube.apply accepts (review M1).
APPLY_KINDS = frozenset({"Job", "ConfigMap", "Service", "Pod"})
# Names the renderers use: an object with one of them and no run label is foreign.
SOAK_NAME_RE = re.compile(r"soak-(otlp|control|k6-[a-z0-9-]+|fakeotlp-[a-z0-9-]+|fallback-[a-z0-9-]+)")
DEMO_SELECTOR = "app.kubernetes.io/part-of=otel-libs,app.kubernetes.io/component=demo,otel-libs/lang"
MESH_LABEL = "istio.io/dataplane-mode"
GUARD_INTERVAL_S = 60
GAP_FACTOR = 3
READ_TIMEOUT_S = 25        # one kubectl / service-proxy call (review M5)
READ_BUDGET = 0.8          # a guard read must end within this share of the interval
STOP_BACKOFF_S = (5, 10, 20, 40, 60)
K6_WAITING = ("ImagePullBackOff", "ErrImagePull", "InvalidImageName", "CreateContainerConfigError",
              "CreateContainerError", "CrashLoopBackOff")


class SoakError(Exception):
    """A step of the loop could not run (kubectl failed, bad input)."""


# ── child processes (review M6) ────────────────────────────────────────────

_children: set[subprocess.Popen] = set()


def _killpg(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def kill_children() -> None:
    """Ends every running kubectl / demo.sh child and its process group."""
    for proc in list(_children):
        _killpg(proc)


def run_child(cmd: list[str], *, stdin: str | None = None, env: dict | None = None,
              timeout: float) -> subprocess.CompletedProcess:
    """A child in its own session (so an interrupt can end its whole process
    group); TimeoutExpired propagates after the group is ended."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                            start_new_session=True)
    _children.add(proc)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except BaseException:
        _killpg(proc)
        proc.communicate()
        raise
    finally:
        _children.discard(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


# ── kubectl ────────────────────────────────────────────────────────────────

class Kube:
    """kubectl against one context and namespace, with a fixed set of verbs.

    Writes go only to ``namespace`` (soak-run objects of the run), plus the
    guarded namespace-label restore. No method deletes without a label
    selector. ``deadline`` (a ``time.monotonic`` value) caps every call while a
    guard read runs.
    """

    def __init__(self, context: str, namespace: str = NAMESPACE,
                 kubectl: list[str] | None = None, timeout: int = READ_TIMEOUT_S):
        self.context, self.namespace = context, namespace
        self.kubectl = kubectl or ["kubectl"]
        self.timeout = timeout
        self.deadline: float | None = None

    def _run(self, args: list[str], stdin: str | None = None, namespaced: bool = True,
             timeout: float | None = None) -> subprocess.CompletedProcess:
        cmd = [*self.kubectl, "--context", self.context]
        if namespaced:
            cmd += ["-n", self.namespace]
        limit = timeout or self.timeout
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left <= 0:
                return subprocess.CompletedProcess(cmd, -1, "", "the guard read's deadline passed")
            limit = min(limit, left)
        try:
            return run_child(cmd + args, stdin=stdin, timeout=limit)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, -1, "", f"kubectl timed out after {limit:.0f}s")

    def _ok(self, args: list[str], **kw) -> str:
        proc = self._run(args, **kw)
        if proc.returncode != 0:
            raise SoakError(f"kubectl {' '.join(args[:3])}: {proc.stderr.strip() or 'failed'}")
        return proc.stdout

    def get(self, args: list[str], namespaced: bool = True) -> dict:
        out = self._ok(["get", *args, "-o", "json"], namespaced=namespaced)
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise SoakError(f"kubectl get {' '.join(args)}: not JSON ({exc})") from exc

    def apply(self, run: str, objs: list[dict]) -> str:
        """Server-side apply of soak-run objects of ``run`` only (review M1)."""
        for obj in objs:
            meta = obj.get("metadata") or {}
            what = f"{obj.get('kind')}/{meta.get('name')}"
            if obj.get("kind") not in APPLY_KINDS:
                raise SoakError(f"refusing to apply {what}: kind not in {sorted(APPLY_KINDS)}")
            if meta.get("namespace") != self.namespace or (meta.get("labels") or {}).get(RUN_LABEL) != run:
                raise SoakError(f"refusing to apply {what}: not a soak-run object of run {run} in {self.namespace}")
        return self._ok(["apply", "--server-side", "--field-manager=otel-libs-soak", "-f", "-"],
                        stdin=json.dumps(render.as_list(objs)), timeout=60)

    def run_objects(self, run: str | None) -> list[str]:
        """``kind/name`` of every object with the run label (any run when None)."""
        sel = f"{RUN_LABEL}={run}" if run else RUN_LABEL
        items = self.get([SOAK_KINDS, "-l", sel]).get("items") or []
        return sorted(f"{o.get('kind')}/{(o.get('metadata') or {}).get('name')}" for o in items)

    def foreign_soak_names(self) -> list[str]:
        """Objects without the run label whose name is a soak name (review M1)."""
        items = self.get([SOAK_KINDS, "-l", f"!{RUN_LABEL}"]).get("items") or []
        return sorted(f"{o.get('kind')}/{o['metadata']['name']}" for o in items
                      if SOAK_NAME_RE.fullmatch((o.get("metadata") or {}).get("name", "")))

    def delete_run(self, run: str) -> str:
        render.check_run(run)
        return self._ok(["delete", SOAK_KINDS, "-l", f"{RUN_LABEL}={run}",
                         "--ignore-not-found", "--wait=true", "--timeout=180s"], timeout=240)

    def logs(self, target: str, container: str | None = None,
             since_time: str | None = None) -> tuple[int, str]:
        """``kubectl logs`` without ``--timestamps`` (review H1: a timestamp
        prefix breaks the summary markers and k6's long lines); one container
        and ``--since-time`` when given (the demo containers, trap step 1b)."""
        args = ["logs", target, f"--container={container}"] if container else ["logs", target, "--all-containers"]
        if since_time:
            args.append(f"--since-time={since_time}")
        proc = self._run(args, timeout=120)
        return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr

    def stop_job(self, run: str, name: str, how: str) -> None:
        """Stops a k6 Job: ``suspend`` (its pods go, the Job stays for the trap)
        or ``delete`` (by the run's k6 label and the Job's name)."""
        if how == "suspend":
            self._ok(["patch", "job", name, "--type=merge", "-p", '{"spec":{"suspend":true}}'])
        else:
            self._ok(["delete", "jobs", "-l", f"{RUN_LABEL}={render.check_run(run)},{ROLE_LABEL}=k6",
                      "--field-selector", f"metadata.name={name}", "--ignore-not-found"], timeout=60)

    def exec_fd(self, pod: str, container: str) -> str:
        """The one named exec of SR15 "Checker" (owner ruling K0e-2): the fixed,
        read-only FD listing of PID 1 (``ls -1 /proc/1/fd``) in a demo
        container; no other command."""
        if not guard.verify.POD_RE.fullmatch(pod) or not DNS1123_LABEL_RE.fullmatch(container):
            raise SoakError(f"refusing exec in {pod}/{container}: not a demo pod and container")
        # No shell (review H2): the listing comes back and is counted in Python;
        # a non-zero exit raises (SoakError), so a failed ls is never 0 FDs.
        return self._ok(["exec", pod, "-c", container, "--", *soakcount.FD_ARGV],
                        timeout=soakcount.FD_TIMEOUT_S)

    def fd_sweep(self, langs) -> "soakcount.FdSweep":
        """The K-count FD reader for ``Loop(fd_reader=...)``: every guard read."""
        return soakcount.FdSweep(lambda: self.get(["pods"]).get("items") or [], self.exec_fd, time.time, langs)

    def namespace_label(self) -> str | None:
        ns = self.get(["namespace", self.namespace], namespaced=False)
        return ((ns.get("metadata") or {}).get("labels") or {}).get(MESH_LABEL)

    def set_ambient(self) -> None:
        self._ok(["label", "namespace", self.namespace, f"{MESH_LABEL}=ambient", "--overwrite"],
                 namespaced=False)


class KubeReader(guard.sp.Reader):
    """K1's read-only reader (``soakplatform.Reader``) with the guard's calls
    made through this loop's ``Kube`` (so ``--kubectl`` and the read deadline
    apply to them): Prometheus instant and range queries through the API
    server's service proxy, ``kubectl get``, pod logs and exec. ``prom_series``,
    ``prom_status`` and the Tempo calls stay on ``verify.Backends`` (the guard
    does not use them). Env: BACKENDS_NAMESPACE, PROMETHEUS as verify.py.

    A call that fails in transport (``soakplatform.transport_failure``: reset,
    refused, timeout, TLS handshake, an API 5xx) is tried again with
    ``soakplatform.READ_RETRY_BACKOFF_S`` pauses while the pause fits before the
    guard read's deadline (entry reads have no deadline: the count bounds
    them); the retries are counted (``retries``,
    ``retry_error``) and recorded by the guard read in ``guard.jsonl``. An
    answer — an error status, an empty result — is never retried (correction
    2026-11-24 to SR15 Orchestration)."""

    def __init__(self, kube: Kube, sleep: Callable[[float], None] = time.sleep):
        b = guard.verify.Backends(
            context=kube.context,
            namespace=os.environ.get("BACKENDS_NAMESPACE", "monitoring"),
            tempo=os.environ.get("TEMPO", "tempo:3200"),
            loki=os.environ.get("LOKI", "loki:3100"),
            prometheus=os.environ.get("PROMETHEUS", "kps-prometheus:9090"),
        )
        super().__init__(b, kube.context, sleep=sleep)
        self.kube = kube

    def time_left(self) -> float | None:
        if self.kube.deadline is None:
            return None
        return self.kube.deadline - time.monotonic()

    def _read(self, args: list[str]) -> subprocess.CompletedProcess:
        return self.retrying(lambda: self.kube._run(args, namespaced=False))

    def _read_ok(self, args: list[str]) -> str:
        proc = self._read(args)
        if proc.returncode != 0:
            raise guard.ReadError(f"kubectl {' '.join(args[:3])}: {proc.stderr.strip() or 'failed'}")
        return proc.stdout

    def _prom_api(self, path: str, params: dict) -> list[dict]:
        url = (f"/api/v1/namespaces/{self.b.namespace}/services/{self.b.prometheus}/proxy{path}?"
               + urllib.parse.urlencode(params))
        out = self._read_ok(["get", "--raw", url])
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise guard.ReadError(f"get --raw {url}: not JSON ({exc})") from exc
        if data.get("status") != "success":
            raise guard.ReadError(f"prometheus: {data.get('error') or data.get('status')}")
        return (data.get("data") or {}).get("result") or []

    def prom(self, q: str, at: float) -> list[dict]:
        return self._prom_api("/api/v1/query", {"query": q, "time": int(at)})

    def prom_range(self, q: str, start: float, end: float, step: int) -> list[dict]:
        return self._prom_api("/api/v1/query_range", {"query": q, "start": int(start), "end": int(end),
                                                      "step": step})

    def kget(self, args: list[str]) -> dict:
        out = self._read_ok(["get", *args, "-o", "json"])
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise guard.ReadError(f"kubectl get {' '.join(args)}: not JSON ({exc})") from exc

    def _run(self, args: list[str]) -> str:
        proc = self._read(args)
        if proc.returncode != 0:
            raise guard.ReadError(f"kubectl {args[0]}: {proc.stderr.strip()[:200] or 'failed'}")
        return proc.stdout


# ── run state and evidence ─────────────────────────────────────────────────

def run_dir(run: str, base: Path | None = None) -> Path:
    """``runs/<run>/`` (git-ignored) with its ``raw/``; SOAK_RUNS_DIR overrides."""
    if base is None:
        base = Path(os.environ["SOAK_RUNS_DIR"]) if os.environ.get("SOAK_RUNS_DIR") else RUNS_DIR
    d = base / render.check_run(run)
    (d / "raw").mkdir(parents=True, exist_ok=True)
    return d


def load_state(d: Path) -> dict:
    p = d / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(d: Path, state: dict) -> None:
    write_evidence(d / "state.json", json.dumps(state, indent=1, sort_keys=True) + "\n")


def write_evidence(path: Path, text: str, mode: str = "w") -> list[str]:
    """Writes evidence after ``verify.scan_secret`` (review L3); a flagged text
    is not written (a marker line is, naming the patterns). Returns the hits."""
    hits = guard.verify.scan_secret(text)
    if hits:
        text = json.dumps({"not_written": "secret scan", "patterns": hits}) + "\n"
    with path.open(mode) as fh:
        fh.write(text)
    return hits


def append_jsonl(path: Path, record: dict) -> None:
    write_evidence(path, json.dumps(record, sort_keys=True) + "\n", mode="a")


def _redact_canary(text: str, canary: str) -> str:
    """``text`` without the canary (case-insensitive)."""
    return re.sub(re.escape(canary), "<canary>", text, flags=re.IGNORECASE) if canary else text


def write_private(path: Path, text: str) -> None:
    """A file only the operator can read (0600, also when it existed, review
    L2); its content is never printed and never scanned out (the canary)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def hook_env(nonce: str, canary: str, worker_interval_s: int | None = None) -> str:
    """The DEMO_ENV_FILE of a window: every app on ``soak-otlp`` with the canary
    header and a new restart nonce (demo DE13)."""
    lines = [
        "# soak run hook (testing/k8s/soak); written 0600, never printed",
        "*/* OTEL_EXPORTER_OTLP_ENDPOINT=http://soak-otlp:4317",
        f"*/* OTEL_EXPORTER_OTLP_HEADERS=x-soak-canary={canary}",
        f"*/* DEMO_RESTART_NONCE={nonce}",
    ]
    if worker_interval_s is not None:
        lines.append(f"*/worker DEMO_INTERVAL_SECONDS={worker_interval_s}")
    return "\n".join(lines) + "\n"


def guard_config(d: Path, now: float, langs: list[str] | tuple[str, ...] = render.LANGS,
                 log: Callable[[str], None] = print) -> guard.GuardConfig:
    """Floors from a passing K1 snapshot no older than 48 h (this run's, else the
    newest under ``runs/``), else the initial floors; logs which (review M14)."""
    snaps = [d / "snapshot.json"] + sorted((p for p in d.parent.glob("*/snapshot.json") if p.parent != d),
                                           key=lambda p: p.stat().st_mtime, reverse=True)
    reasons = []
    for snap in snaps:
        if not snap.exists():
            continue
        cfg = guard.GuardConfig.from_snapshot(snap, now)
        if cfg.snapshot == str(snap):
            cfg.langs = tuple(langs)
            log(f"guard: floors from K1 snapshot {snap}")
            return cfg
        reasons.append(cfg.snapshot)
    cfg = guard.GuardConfig(langs=tuple(langs))
    log("guard: initial K-D13 floors (no passing K1 snapshot of the last 48 h"
        + (f": {'; '.join(reasons)}" if reasons else "") + ")")
    return cfg


# ── the demo (review H5) ───────────────────────────────────────────────────

def image_tag(image: str) -> str:
    ref = image.split("@", 1)[0]
    return ref.rsplit(":", 1)[1] if ":" in ref.rsplit("/", 1)[-1] else ""


def demo_record(kube: Kube, langs: list[str]) -> tuple[dict, list[str]]:
    """The running demo apps of ``langs``: one image tag and the replicas per
    language, from the live Deployments; plus what prevents a faithful restore."""
    items = kube.get(["deployments", "-l", DEMO_SELECTOR]).get("items") or []
    tags, replicas, problems = set(), {}, []
    for lang in langs:
        mine = [o for o in items if (o["metadata"].get("labels") or {}).get("otel-libs/lang") == lang]
        if len(mine) != 3:
            problems.append(f"demo apps of {lang}: {len(mine)} of 3 Deployments applied")
            continue
        counts = {int((o.get("spec") or {}).get("replicas", 1)) for o in mine}
        if len(counts) != 1:
            problems.append(f"demo apps of {lang}: replicas differ {sorted(counts)}; set them equal first")
        replicas[lang] = max(counts)
        for o in mine:
            for c in ((o.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []:
                tags.add(image_tag(c.get("image", "")))
    if len(tags) > 1:
        problems.append(f"demo apps run several image tags {sorted(tags)}")
    return {"tag": next(iter(tags)) if len(tags) == 1 else None, "replicas": replicas}, problems


def demo_apply(demo_sh: list[str], context: str, namespace: str, tag: str, langs: list[str],
               env_file: Path | None = None, *, restore: bool = False) -> subprocess.CompletedProcess:
    """``demo.sh apply --all`` with an explicit TAG and LANGS; DEMO_ENV_FILE only
    when given; ``restore`` sets DEMO_RESTORE=1 (no capacity pre-flight: the
    apps already run). RUN is never passed (the backing services' run id is
    reused). TimeoutExpired propagates."""
    env = {k: v for k, v in os.environ.items() if k not in ("DEMO_ENV_FILE", "RUN", "TAG", "LANGS", "DEMO_RESTORE")}
    env.update(KUBE_CONTEXT=context, NAMESPACE=namespace, TAG=tag, LANGS=" ".join(langs))
    if env_file is not None:
        env["DEMO_ENV_FILE"] = str(env_file)
    if restore:
        env["DEMO_RESTORE"] = "1"
    return run_child([*demo_sh, "apply", "--all"], env=env, timeout=1800)


def demo_scale(demo_sh: list[str], context: str, namespace: str, lang: str, n: int, *,
               restore: bool) -> subprocess.CompletedProcess:
    """``demo.sh scale --lang <lang> <n>``: with ``restore`` (DEMO_RESTORE=1) only
    down to n; a scale-up runs demo.sh's normal capacity pre-flight."""
    env = {k: v for k, v in os.environ.items() if k not in ("DEMO_ENV_FILE", "RUN", "TAG", "LANGS", "DEMO_RESTORE")}
    env.update(KUBE_CONTEXT=context, NAMESPACE=namespace)
    if restore:
        env["DEMO_RESTORE"] = "1"
    return run_child([*demo_sh, "scale", "--lang", lang, str(n)], env=env, timeout=1800)


def demo_tag() -> str:
    """``demo-tag.sh``: the tag the demo images of HEAD carry (review L7: errors are SoakError)."""
    try:
        out = subprocess.run([str(DEMO_TAG)], capture_output=True, text=True, check=True, timeout=60,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SoakError(f"demo-tag.sh failed: {exc}") from exc
    tag = out.stdout.strip()
    if not tag:
        raise SoakError("demo-tag.sh printed no tag")
    return tag


# ── the trap ───────────────────────────────────────────────────────────────

@dataclass
class TrapResult:
    saved: list[str] = field(default_factory=list)
    log_bytes: dict[str, int] = field(default_factory=dict)
    demo_logs: dict[str, dict] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    label_written: bool = False
    demo_restored: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.left


def _step(res: TrapResult, what: str, fn: Callable[[], None]) -> None:
    """One trap step: any exception is recorded and the trap moves on (review H4)."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — the trap must reach every step
        res.errors.append(f"{what}: {exc.__class__.__name__}: {exc}")


def trap(kube: Kube, run: str, d: Path, *, state: dict, demo_sh: list[str],
         log: Callable[[str], None] = print) -> TrapResult:
    """The four trap steps; each runs even when an earlier one failed, and
    ``trap.jsonl`` is written in any case."""
    res = TrapResult()

    def save_logs():
        jobs = kube.get(["jobs", "-l", f"{RUN_LABEL}={run},{ROLE_LABEL}=k6"]).get("items") or []
        for job in jobs:
            name = job["metadata"]["name"]
            rc, text = kube.logs(f"job/{name}")
            if rc != 0:  # review M12: an error never overwrites a saved log
                write_evidence(d / "raw" / f"{name}.log.err", text)
                res.errors.append(f"logs job/{name}: {text.strip()[:200]}")
                continue
            path = d / "raw" / f"{name}.log"
            write_evidence(path, text)
            res.saved.append(str(path))
            res.log_bytes[name] = len(text.encode())
            if state.get("reservoir") and k6script.RESERVOIR_PREFIX not in text:
                res.errors.append(f"logs job/{name}: no {k6script.RESERVOIR_PREFIX.strip()} line "
                                  f"({len(text.encode())} bytes saved; rotated or not started?)")

    def save_demo_logs():
        """Step 1b: the demo containers' logs, before the restore replaces the pods
        (soak extras' canary source; counts only, never a line, in trap.jsonl)."""
        if not state.get("hook_applied"):
            return
        canary_file = d / "canary"
        canary = canary_file.read_text().strip().lower() if canary_file.exists() else ""
        since = state.get("hook_at")
        since_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(since))) if since else None
        for pod in kube.get(["pods"]).get("items") or []:
            name = (pod.get("metadata") or {}).get("name", "")
            if not guard.verify.POD_RE.fullmatch(name):
                continue
            for c in (pod.get("spec") or {}).get("containers") or []:
                cname = c.get("name", "")
                key = f"{name}/{cname}"
                rc, text = kube.logs(f"pod/{name}", cname, since_time)
                if rc != 0:
                    err = _redact_canary(" ".join(text.split()), canary)[:200]
                    res.demo_logs[key] = {"error": err}
                    res.errors.append(f"logs pod/{key}: {err}")
                    continue
                lines = text.splitlines()
                hits = sum(1 for ln in lines if canary and canary in ln.lower()) if canary else None
                path = d / "raw" / f"{name}.{cname}.log"
                # Review M4: a log with a canary line is never written, whatever the
                # secret scan says; every saved log is private (0600).
                marker = bool(hits)
                write_private(path, json.dumps({"not_written": "canary", "canary_lines": hits}) + "\n"
                              if marker else text)
                res.demo_logs[key] = {"path": path.name, "lines": len(lines), "bytes": len(text.encode()),
                                      "since": since_time, "secret_patterns": guard.verify.scan_secret(text),
                                      "canary_lines": hits, "marker": marker}

    def delete():
        before = kube.run_objects(run)
        kube.delete_run(run)
        res.left = kube.run_objects(run)
        res.deleted = sorted(set(before) - set(res.left))

    def label():
        if kube.namespace_label() != "ambient":
            kube.set_ambient()
            res.label_written = True

    def restore():
        if not state.get("hook_applied"):
            log("trap: the demo was not changed by this run (no hook): no restore")
            return
        tag, langs = state.get("tag"), state.get("langs") or list(render.LANGS)
        if not tag:
            raise SoakError(f"no image tag recorded; run `soak.py cleanup --run {run} --tag <tag>`")
        replicas = state.get("replicas") or {}
        # A language recorded at 0 is not re-applied (that would scale it up); it is
        # scaled back to 0 instead (re-review M2).
        apply_langs = [lang for lang in langs if replicas.get(lang, 1) != 0]
        if apply_langs:
            proc = demo_apply(demo_sh, kube.context, kube.namespace, tag, apply_langs, restore=True)
            if proc.returncode != 0:
                raise SoakError(f"demo.sh apply --all (TAG={tag}) exit {proc.returncode}: "
                                f"{proc.stderr.strip()[-300:]}")
        for lang in langs:
            n = replicas.get(lang, 1)
            if n != 1:
                proc = demo_scale(demo_sh, kube.context, kube.namespace, lang, n, restore=n == 0)
                if proc.returncode != 0:
                    raise SoakError(f"demo.sh scale --lang {lang} {n} exit {proc.returncode}")
        res.demo_restored = True

    try:
        _step(res, "k6 logs", save_logs)
        _step(res, "demo logs", save_demo_logs)
        _step(res, "delete", delete)
        _step(res, "namespace label", label)
        _step(res, "demo restore", restore)
    finally:
        try:
            append_jsonl(d / "trap.jsonl", {"at": time.time(), "saved": res.saved, "log_bytes": res.log_bytes,
                                             "demo_logs": res.demo_logs,
                                             "deleted": res.deleted, "left": res.left,
                                             "label_written": res.label_written,
                                             "demo_restored": res.demo_restored, "errors": res.errors})
        except OSError as exc:
            res.errors.append(f"trap.jsonl: {exc}")
    for line in (f"trap: saved {len(res.saved)} k6 log(s) {res.log_bytes or ''}".rstrip(),
                 f"trap: saved {len(res.demo_logs)} demo container log(s)" if res.demo_logs else "",
                 f"trap: deleted {len(res.deleted)} soak-run object(s): {', '.join(res.deleted) or '-'}",
                 f"trap: left {', '.join(res.left)}" if res.left else "trap: no soak-run object left",
                 "trap: namespace label set to ambient" if res.label_written
                 else "trap: namespace label already ambient (no write)",
                 "trap: demo restored to its recorded state" if res.demo_restored else "",
                 *(f"trap: ERROR {e}" for e in res.errors)):
        if line:
            log(line)
    return res


@contextmanager
def trapped(on_exit: Callable[[], None]):
    """Run ``on_exit`` on normal exit, an exception, SIGINT, SIGTERM or SIGHUP;
    a signal first ends the running children's process groups; during the trap
    further signals are ignored, so a second Ctrl-C cannot cut it."""
    def handler(signum, _frame):
        kill_children()
        raise SystemExit(128 + signum)

    sigs = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    old = {s: signal.signal(s, handler) for s in sigs}
    try:
        yield
    finally:
        for s in old:
            signal.signal(s, signal.SIG_IGN)
        try:
            on_exit()
        finally:
            for s, h in old.items():
                signal.signal(s, h)


# ── pre-flight ─────────────────────────────────────────────────────────────

def preflight(kube: Kube) -> list[str]:
    """Refusals (empty = may start): leftovers of any run, foreign objects with
    a soak name, a non-ambient label. Reads only."""
    problems = []
    left = kube.run_objects(None)
    if left:
        problems.append(f"soak-run objects exist ({', '.join(left)}); "
                        "run `soak.py cleanup --run <their run id>` first")
    foreign = kube.foreign_soak_names()
    if foreign:
        problems.append(f"objects with a soak name but no {RUN_LABEL} label ({', '.join(foreign)}); "
                        "remove or rename them first")
    label = kube.namespace_label()
    if label != "ambient":
        problems.append(f"namespace {kube.namespace} label {MESH_LABEL}={label!r}, not 'ambient'")
    return problems


def demo_preflight(kube: Kube, langs: list[str], expected_tag: str) -> tuple[dict, list[str]]:
    """The demo the trap will restore (review H5): refused when it is not
    complete or its running tag is not ``expected_tag``."""
    record, problems = demo_record(kube, langs)
    if record["tag"] and record["tag"] != expected_tag:
        problems.append(f"the demo runs image tag {record['tag']}, not {expected_tag} "
                        "(demo-tag.sh or --tag); re-apply it first")
    return record, problems


# ── the loop ───────────────────────────────────────────────────────────────

def start_caffeinate(platform: str = sys.platform, which=shutil.which,
                     popen=subprocess.Popen):
    """``caffeinate -dims -w <pid>`` on macOS (no sleep while the loop lives)."""
    if platform != "darwin" or not which("caffeinate"):
        return None
    return popen(["caffeinate", "-dims", "-w", str(os.getpid())])


@dataclass
class WindowResult:
    name: str
    started: bool
    valid: bool
    reason: str = ""
    reads: int = 0
    start: float | None = None
    end: float | None = None
    findings: list[str] = field(default_factory=list)
    # Why the loop ended: "load done" (the k6 Job succeeded), "duration" (the
    # window's time ran out first) or "invalid" (a breach, gap, blind read or k6 failure).
    ended: str = ""

    def as_json(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Load:
    """The k6 Job of a window: its name, the pull allowance and its deadline."""
    job: str
    pull_s: int
    deadline_s: int


class Loop:
    def __init__(self, run: str, kube: Kube, reader: "guard.sp.Reader", cfg: guard.GuardConfig, d: Path, *,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 interval: int = GUARD_INTERVAL_S, fd_reader: Callable[[], dict] | None = None,
                 log: Callable[[str], None] = print):
        self.run, self.kube, self.reader, self.cfg, self.d = run, kube, reader, cfg, d
        self.clock, self.sleep, self.interval = clock, sleep, interval
        self.fd_reader, self.log = fd_reader, log
        self.cfg.interval_s = interval
        self.last_beat: float | None = None

    def heartbeat(self) -> str | None:
        """Writes the heartbeat; returns the gap reason when the wall clock
        jumped more than 3 x the interval (laptop asleep or the loop stalled)."""
        now = self.clock()
        reason = None
        if self.last_beat is not None:
            gap = now - self.last_beat
            if gap > GAP_FACTOR * self.interval or gap < 0:
                reason = (f"laptop: wall-clock gap {gap:.0f}s since the last heartbeat "
                          f"(> {GAP_FACTOR} x {self.interval}s)")
        self.last_beat = now
        (self.d / "heartbeat").write_text(f"{now:.3f}\n")
        return reason

    def stop_load(self, load: Load | None, how: str, started: float | None = None) -> str:
        """Saves the k6 logs, then suspends (breach, gap) or deletes (blind) the
        Job. A delete is retried with backoff until it succeeds or the Job's
        ``activeDeadlineSeconds`` has passed (review M4)."""
        if load is None:
            return "no load to stop"
        rc, text = self.kube.logs(f"job/{load.job}")
        write_evidence(self.d / "raw" / (f"{load.job}.stop.log" if rc == 0 else f"{load.job}.stop.log.err"), text)
        until = (started or self.clock()) + load.deadline_s
        attempt = 0
        while True:
            try:
                self.kube.stop_job(self.run, load.job, how)
                return f"k6 Job {load.job} {'suspended' if how == 'suspend' else 'deleted'}"
            except SoakError as exc:
                if how != "delete" or self.clock() >= until:
                    return f"k6 stop failed ({exc}); activeDeadlineSeconds bounds the load"
            self.sleep(STOP_BACKOFF_S[min(attempt, len(STOP_BACKOFF_S) - 1)])
            attempt += 1

    def load_state(self, load: Load, started: float) -> str | None:
        """None while k6 runs; "done" when it succeeded; else the invalid reason
        (a failed Job, or no running k6 pod after the pull allowance; review H6)."""
        try:
            job = self.kube.get(["jobs", load.job])
        except SoakError:
            return None
        status = job.get("status") or {}
        if status.get("succeeded"):
            return "done"
        if status.get("failed"):
            conds = [c for c in status.get("conditions") or [] if c.get("type") == "Failed"]
            why = (conds[0].get("reason") or "") + (": " + conds[0].get("message", "") if conds else "")
            return f"k6: Job failed ({why.strip() or 'no reason given'})"
        if self.clock() - started < load.pull_s:
            return None
        try:
            pods = self.kube.get(["pods", "-l", f"job-name={load.job}"]).get("items") or []
        except SoakError:
            return None
        for p in pods:
            for c in (p.get("status") or {}).get("containerStatuses") or []:
                st = c.get("state") or {}
                if "running" in st:
                    return None
                # k6 exited 0 and the Job controller has not set ``succeeded`` yet
                # (K-cal run kcal-20261125b, step 2: a 300 s step ended right at the
                # 300 s pull allowance); the next read sees the Job done. The pod's
                # restartPolicy is Never (render.k6_job), so the exit is final.
                if (st.get("terminated") or {}).get("exitCode") == 0:
                    return None
        reasons = sorted({((c.get("state") or {}).get("waiting") or {}).get("reason", "not running")
                          for p in pods for c in (p.get("status") or {}).get("containerStatuses") or []})
        return f"k6: pod not running {load.pull_s}s after the start ({', '.join(reasons) or 'no pod'})"

    def _read(self, roll: guard.RollState, loaded: bool) -> guard.GuardRead:
        self.kube.deadline = time.monotonic() + READ_BUDGET * self.interval
        try:
            return guard.window_read(self.reader, self.cfg, roll, self.clock(), loaded=loaded)
        finally:
            self.kube.deadline = None

    def window(self, name: str, duration_s: int, *, loaded: bool = False, gate: bool = True,
               start_load: Callable[[], Load | None] | None = None,
               services: list[str] | None = None, since: dict[str, float] | None = None,
               exempt_copy_rolls: int = 0, exempt_window: tuple[float, float] = (0.0, 0.0)) -> WindowResult:
        res = WindowResult(name, started=False, valid=False)
        if gate:
            read = guard.entry_read(self.reader, self.cfg, self.clock(), clock=self.clock, sleep=self.sleep)
            append_jsonl(self.d / "guard.jsonl", {"window": name, "phase": "entry", **read.as_json()})
            if read.blind or read.breaches:
                why = "blind read" if read.blind else read.reason()
                res.reason = f"entry gate: {why}; not started — ask the platform session"
                return self._finish(res)
            if services:
                h = guard.wait_copy_counts(self.reader, self.cfg, services, self.clock, self.sleep, since)
                if h.state != "ok":
                    res.reason = f"entry gate: {h.name}: {h.detail}; not started"
                    return self._finish(res)
        roll = guard.RollState(exempt_copy_rolls, *exempt_window)
        try:
            load = start_load() if start_load else None
        except Exception as exc:  # noqa: BLE001 — review L6: the window record is written
            res.reason = f"k6 start failed: {exc.__class__.__name__}: {exc}"
            return self._finish(res)
        res.started, res.valid, res.start = True, True, self.clock()
        self.last_beat = None
        while True:
            iteration_start = self.clock()
            gap = self.heartbeat()
            if gap:
                res.valid, res.reason = False, gap + "; " + self.stop_load(load, "suspend", res.start)
                res.ended = "invalid"
                break
            read = self._read(roll, loaded)
            record = {"window": name, "phase": "window", **read.as_json()}
            if self.fd_reader is not None:
                record["fd"] = self.fd_reader()
            append_jsonl(self.d / "guard.jsonl", record)
            res.reads += 1
            res.findings += [f"{i.name}: {i.detail}" for i in read.findings]
            if read.blind:
                res.valid, res.reason = False, "guard: blind read; " + self.stop_load(load, "delete", res.start)
                res.ended = "invalid"
                break
            if read.breaches:
                res.valid, res.reason = False, read.reason() + "; " + self.stop_load(load, "suspend", res.start)
                res.ended = "invalid"
                break
            if load:
                st = self.load_state(load, res.start)
                if st == "done":
                    res.ended = "load done"
                    break
                if st:
                    res.valid, res.reason, res.ended = False, st, "invalid"
                    break
            if self.clock() - res.start >= duration_s:
                res.ended = "duration"
                break
            # A fixed cadence (K-count re-review HIGH-2): the read's own time is
            # taken off the sleep, so a read starts every interval, not every
            # interval + read time; a read longer than the interval sleeps 0.
            self.sleep(max(0.0, self.interval - (self.clock() - iteration_start)))
        return self._finish(res)

    def _finish(self, res: WindowResult) -> WindowResult:
        res.end = self.clock()
        append_jsonl(self.d / "windows.jsonl", res.as_json())
        self.log(f"window {res.name}: {'started' if res.started else 'NOT started'}, "
                 f"{'valid' if res.valid else 'INVALID'}{': ' + res.reason if res.reason else ''}")
        return res


# ── positive control (task K-copy) ─────────────────────────────────────────

# The control Job's own deadline (render.control_job) plus a margin; the count
# wait (batch + one 15 s scrape, several times over) and the settle re-read
# (two scrapes): initial values, 2026-11-23.
CONTROL_JOB_S = 360
CONTROL_COUNT_S = 120
CONTROL_SETTLE_S = 30
CONTROL_STEP_S = 5


def control_counts(r: "guard.sp.Reader", at: float) -> dict:
    """The copy's cumulative control counts now: ``spans`` (all spans of
    ``soak-control``) and ``fixed`` by ``service_instance_id``."""
    m = {"job": guard.sp.COPY_JOB, "service_name": render.CONTROL_SERVICE}
    spans = guard.sp.cluster_only(r.prom(f"sum by (cluster) ({guard.sp.sel('otel_libs_soak_spans_total', m)})", at))
    fixed = guard.sp.cluster_only(r.prom(
        f"sum by (cluster, service_instance_id) ({guard.sp.sel(soakcount.FIXED_METRIC, m)})", at))
    return {"spans": sum(float(s["value"][1]) for s in spans),
            "fixed": {(s.get("metric") or {}).get("service_instance_id", ""): float(s["value"][1]) for s in fixed}}


def control_delta(before: dict, after: dict) -> tuple[dict | None, str]:
    """``after - before`` (an absent series counts 0: the copy holds no series
    of a new instance id); a decrease is a copy reset, and the delta is unknown."""
    fixed = {}
    for iid in set(before["fixed"]) | set(after["fixed"]):
        a, b = after["fixed"].get(iid, 0.0), before["fixed"].get(iid, 0.0)
        if a < b:
            return None, f"fixed[{iid}] went down {b:g} -> {a:g} (copy reset)"
        if a != b:
            fixed[iid] = a - b
    if after["spans"] < before["spans"]:
        return None, f"spans went down {before['spans']:g} -> {after['spans']:g} (copy reset)"
    return {"spans": after["spans"] - before["spans"], "fixed": fixed}, ""


def control_job_state(kube: Kube) -> str | None:
    """None while the control runs; "done"; or why it failed."""
    try:
        status = kube.get(["jobs", render.CONTROL_SERVICE]).get("status") or {}
    except SoakError:
        return None
    if status.get("succeeded"):
        return "done"
    if status.get("failed"):
        conds = [c for c in status.get("conditions") or [] if c.get("type") == "Failed"]
        return "control Job failed (" + ((conds[0].get("reason") or "no reason") if conds else "no reason") + ")"
    return None


def positive_control(kube: Kube, r: "guard.sp.Reader", run: str, d: Path, *,
                     clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                     log: Callable[[str], None] = print) -> dict:
    """Task K-copy: the telemetrygen control through ``soak-otlp`` (copy). Reads
    the copy's control series, applies ``soak-control``, waits for the Job,
    polls until the counts arrive, re-reads after two scrapes and judges
    exact equality (``render.control_expected``). Writes ``control.json`` and one
    ``windows.jsonl`` record; returns the record (``verdict`` PASS / FAIL).
    The Job stays for the trap."""
    expected = render.control_expected(run)
    rec: dict = {"window": "control", "expected": expected}
    try:
        before = control_counts(r, clock())
    except guard.ReadError as exc:
        raise SoakError(f"control counts before the Job: {exc}") from exc
    if before["fixed"].get(soakcount.UNSET):
        # Measured 2026-11-23 (runs kcopy-20261123a/b): two control streams without
        # service.instance.id differ only in k8s.pod.ip (a target_info attribute),
        # so the copy's Prometheus exporter shows one series for both and the new
        # one never counts. A live earlier one makes the "unset" case unjudgeable.
        raise SoakError(f"an earlier control's {soakcount.UNSET} series is still live on the copy "
                        f"({before['fixed'][soakcount.UNSET]:g}); streams without service.instance.id "
                        f"collide at its Prometheus exporter: retry {soakcount.COPY_EXPIRY_S // 60} min "
                        "after the last control")
    rec["before"], rec["applied_at"] = before, clock()
    kube.apply(run, [render.control_job(run)])
    deadline = clock() + CONTROL_JOB_S
    while (st := control_job_state(kube)) is None and clock() < deadline:
        sleep(CONTROL_STEP_S)
    rec["job"] = st or f"control Job not finished after {CONTROL_JOB_S}s"
    rc, text = kube.logs(f"job/{render.CONTROL_SERVICE}")
    write_evidence(d / "raw" / ("soak-control.log" if rc == 0 else "soak-control.log.err"), text)
    delta, why = None, rec["job"] if st != "done" else ""
    if st == "done":
        deadline = clock() + CONTROL_COUNT_S
        while True:
            try:
                delta, why = control_delta(before, control_counts(r, clock()))
            except guard.ReadError as exc:
                delta, why = None, f"blind read: {exc}"
            if delta is not None and delta["spans"] >= expected["spans"] and \
                    all(delta["fixed"].get(k, 0) >= v for k, v in expected["fixed"].items()):
                break
            if clock() >= deadline:
                break
            sleep(CONTROL_STEP_S)
        rec["first"] = delta
        sleep(CONTROL_SETTLE_S)  # a late increment after the first match would show here
        try:
            delta, why = control_delta(before, control_counts(r, clock()))
        except guard.ReadError as exc:
            delta, why = None, f"blind read: {exc}"
    rec["delta"], rec["read_at"] = delta, clock()
    if delta is not None and not why:
        if delta != expected:
            why = f"counted {delta}, expected {expected}"
    rec["verdict"], rec["reason"] = ("PASS", "") if delta == expected and not why else ("FAIL", why)
    write_evidence(d / "control.json", json.dumps(rec, indent=1, sort_keys=True) + "\n")
    append_jsonl(d / "windows.jsonl", rec)
    log(f"control: {rec['verdict']}" + (f": {rec['reason']}" if rec["reason"] else
                                         f" (spans +{delta['spans']:g}, fixed {delta['fixed']})"))
    return rec


# ── CLI ────────────────────────────────────────────────────────────────────

def _langs(text: str) -> list[str]:
    langs = [x for x in text.replace(",", " ").split() if x]
    bad = [x for x in langs if x not in render.LANGS]
    if bad or not langs:
        raise argparse.ArgumentTypeError(f"languages: a subset of {' '.join(render.LANGS)}")
    return langs


def k6_objects(run: str, window: str, plan: k6script.K6Plan) -> list[dict]:
    return [render.k6_configmap(run, window, k6script.generate(plan)),
            render.k6_job(run, window, plan.deadline_s, memory_limit=f"{plan.memory_limit_mi}Mi")]


def build_object(args: argparse.Namespace) -> list[dict]:
    w = args.what
    if w in ("k6", "k6-configmap", "k6-job"):
        plan = k6script.K6Plan(langs=tuple(args.langs), rate=args.rate, duration_s=args.duration,
                               timeout_s=args.timeout, p99_s=args.p99)
        objs = k6_objects(args.run, args.window, plan)
        return objs if w == "k6" else [objs[0] if w == "k6-configmap" else objs[1]]
    if w == "soak-otlp":
        return [render.soak_otlp(args.run, args.variant)]
    if w in ("sink-hung", "sink-redirect"):
        mode = w.split("-", 1)[1]
        return [render.sink_pod(args.run, mode, args.fakeotlp_image), render.sink_service(args.run, mode)]
    if w == "control":
        return [render.control_job(args.run)]
    if w == "fallback":
        return [render.fallback_service(args.run, args.langs[0])]
    raise SoakError(f"unknown object {w!r}")


def _seed_state(run: str, tag: str = "dryrun0") -> dict:
    """Fake cluster for the dry run: the demo, the GitOps copy, the namespace and
    a soak-labelled object of this run in another namespace, which the trap must
    all leave alone."""
    def obj(kind, name, labels, ns=NAMESPACE, **extra):
        return {"apiVersion": "v1", "kind": kind,
                "metadata": {"name": name, "namespace": ns, "labels": labels}, **extra}
    demo = {"app.kubernetes.io/part-of": "otel-libs", "app.kubernetes.io/component": "demo",
            "otel-libs/run": run}
    copy = {"app.kubernetes.io/name": "soak-agent", "app.kubernetes.io/part-of": "otel-libs"}
    objects = [obj("Namespace", NAMESPACE, {MESH_LABEL: "ambient"}, ns=None)]
    for lang in render.LANGS:
        for app in ("worker", "api", "backend"):
            spec = {"replicas": 1, "template": {"spec": {"containers": [
                {"name": app, "image": f"harbor.staffops.space/development/otel-libs-demo-{lang}-{app}:{tag}"}]}}}
            objects.append(obj("Deployment", f"demo-{lang}-{app}", {**demo, "otel-libs/lang": lang}, spec=spec))
        for app in ("api", "backend"):
            objects.append(obj("Service", f"demo-{lang}-{app}", {**demo, "otel-libs/lang": lang}))
    for name in ("demo-postgres", "demo-redis"):
        objects += [obj("Deployment", name, demo), obj("Service", name, demo)]
    objects += [obj("Deployment", "soak-agent", copy), obj("Service", "soak-agent", copy),
                obj("ConfigMap", "soak-agent", copy),
                obj("Service", "soak-otlp", {RUN_LABEL: run, ROLE_LABEL: "otlp-copy"}, ns="monitoring")]
    return {"objects": objects, "logs": {}}


DRY_FAKEOTLP = render.FAKEOTLP_IMAGE


def dry_run(run: str, d: Path, log: Callable[[str], None] = print) -> int:
    """Creates one soak-run object of each kind on the fake kubectl, runs the
    trap, and checks it deleted exactly those (and nothing else)."""
    with tempfile.TemporaryDirectory() as tmp:
        state_path, log_path = Path(tmp) / "state.json", Path(tmp) / "calls.jsonl"
        state_path.write_text(json.dumps(_seed_state(run)))
        log_path.write_text("")
        saved_env = {k: os.environ.get(k) for k in ("FAKEKUBE_STATE", "FAKEKUBE_LOG")}
        os.environ["FAKEKUBE_STATE"], os.environ["FAKEKUBE_LOG"] = str(state_path), str(log_path)
        try:
            return _dry_run(run, d, state_path, log_path, log)
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _dry_run(run: str, d: Path, state_path: Path, log_path: Path, log: Callable[[str], None]) -> int:
    """The dry run body, on the fake kubectl whose files are given."""
    fake = [sys.executable, "-I", str(HERE / "fakekube.py")]
    kube = Kube(CONTEXT, kubectl=fake)
    seeded = {(o["kind"], o["metadata"].get("namespace"), o["metadata"]["name"])
              for o in json.loads(state_path.read_text())["objects"]}
    problems = preflight(kube)
    record, demo_problems = demo_preflight(kube, list(render.LANGS), "dryrun0")
    if problems or demo_problems:
        log("dry-run: pre-flight refused: " + "; ".join(problems + demo_problems))
        return 1
    plan = k6script.K6Plan(langs=render.LANGS, rate=5, duration_s=60)
    objs = render.every_kind(run, DRY_FAKEOTLP, k6script.generate(plan), plan.deadline_s)
    kube.apply(run, objs)
    created = sorted(f"{o['kind']}/{o['metadata']['name']}" for o in objs)
    log(f"dry-run: created {len(created)} object(s): {', '.join(created)}")
    log(f"dry-run: kinds {', '.join(sorted({o['kind'] for o in objs}))}")
    st = json.loads(state_path.read_text())
    st["logs"] = {f"job/{render.k6_name('dry')}": "SOAKRES {}\nSOAK_SUMMARY_BEGIN\n{}\nSOAK_SUMMARY_END\n"}
    state_path.write_text(json.dumps(st))
    state = {"tag": record["tag"], "replicas": record["replicas"], "langs": list(render.LANGS),
             "hook_applied": True, "reservoir": True}
    res = trap(kube, run, d, state=state, demo_sh=[*fake, "demo-sh"], log=log)
    remaining = {(o["kind"], o["metadata"].get("namespace"), o["metadata"]["name"])
                 for o in json.loads(state_path.read_text())["objects"]}
    calls = [json.loads(x) for x in log_path.read_text().splitlines()]
    other_ns = ("Service", "monitoring", "soak-otlp") in remaining
    ok = (res.deleted == created and not res.left and remaining == seeded and res.demo_restored
          and not res.label_written and not res.errors)
    demo_calls = [c for c in calls if c["argv"][:1] == ["demo-sh"]]
    log(f"dry-run: seeded objects untouched: {remaining == seeded} ({len(seeded)}, including a soak-labelled "
        f"Service in namespace monitoring: {other_ns}); demo.sh calls: {[c['argv'] for c in demo_calls]} "
        f"env {[c['env'] for c in demo_calls]}")
    log(f"dry-run: {'PASS' if ok else 'FAIL'} — the trap deleted exactly the {len(created)} created objects")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="soak.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="mode", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--run", required=True)
    common.add_argument("--langs", type=_langs, default=list(render.LANGS))
    common.add_argument("--kubectl", default=os.environ.get("SOAK_KUBECTL", "kubectl"))
    common.add_argument("--tag")
    common.add_argument("--allow-other-context", action="store_true",
                        help=f"accept a KUBE_CONTEXT other than {CONTEXT}")
    sub.add_parser("preflight", parents=[common])
    sub.add_parser("cleanup", parents=[common])
    sub.add_parser("dry-run", parents=[common])
    rp = sub.add_parser("render", parents=[common])
    rp.add_argument("--what", required=True)
    rp.add_argument("--variant", default="copy", choices=render.OTLP_VARIANTS)
    for sp in (rp, sub.add_parser("run", parents=[common])):
        sp.add_argument("--window", default="loaded")
        sp.add_argument("--rate", type=int, default=0)
        sp.add_argument("--duration", type=int, default=7200)
        sp.add_argument("--timeout", type=int, default=10)
        sp.add_argument("--p99", type=float, default=1.0)
        sp.add_argument("--fakeotlp-image", default=render.FAKEOTLP_IMAGE)
    sub.choices["run"].add_argument("--hook", action="store_true")
    sub.choices["run"].add_argument("--worker-interval", type=int)
    sub.choices["run"].add_argument("--quiesce", type=float, default=soakcount.QUIESCE_S,
                                    help="seconds after a loaded window's k6 end, demo kept as is, "
                                         "before the trap (0 = none)")
    sub.choices["run"].add_argument("--allow-short-quiesce", action="store_true",
                                    help=f"accept a --quiesce below {soakcount.QUIESCE_S:g}s")
    sub.choices["run"].add_argument("--control", action="store_true",
                                    help="K-copy: the telemetrygen positive control first, before the hook")
    cp = sub.add_parser("cal", parents=[common], help="K-cal: the calibration driver (design K-D20)")
    cp.add_argument("--n", type=int, default=cal.DEFAULT_N, help="requests per language and repeat")
    cp.add_argument("--rate", type=int, default=cal.DEFAULT_RATE, help="phase A rate, req/s per language")
    cp.add_argument("--steps", type=_steps, default=list(cal.DEFAULT_STEPS), help="phase B rates, e.g. 2,5,10,20")
    cp.add_argument("--step-duration", type=int, default=cal.DEFAULT_STEP_S, help="seconds per step")
    cp.add_argument("--quiesce", type=float, default=soakcount.QUIESCE_S, help="seconds after each k6 run")
    cp.add_argument("--worker-interval", type=int, default=cal.CAL_WORKER_INTERVAL_S,
                    help="the hook's */worker DEMO_INTERVAL_SECONDS")
    cp.add_argument("--drain-wait", type=int, default=cal.DRAIN_WAIT_S, help="max seconds for a backlog to drain")
    cp.add_argument("--timeout", type=int, default=10)
    cp.add_argument("--p99", type=float, default=1.0)
    cp.add_argument("--control", action="store_true", help="K-copy: the positive control first")
    cp.add_argument("--dry-run", action="store_true", help="print the plan; no kubectl call, nothing written")
    args = p.parse_args(argv)
    try:
        render.check_run(args.run)
    except render.RenderError as exc:
        p.error(str(exc))
    context = os.environ.get("KUBE_CONTEXT", CONTEXT)
    if context != CONTEXT and not args.allow_other_context:
        p.error(f"KUBE_CONTEXT {context!r} is not {CONTEXT}; pass --allow-other-context to target it")
    namespace = os.environ.get("NAMESPACE", NAMESPACE)
    if namespace != NAMESPACE:
        p.error(f"the soak writes only to {NAMESPACE}")
    try:
        if args.mode == "render":
            if args.what in ("k6", "k6-configmap", "k6-job") and args.rate < 1:
                p.error("--rate is required for the k6 objects")
            print(json.dumps(render.as_list(build_object(args)), indent=1, sort_keys=True))
            return 0
        if args.mode == "cal" and args.dry_run:
            print("\n".join(cal_plan(args).describe(args.run)))
            return 0
        d = run_dir(args.run)
        if args.mode == "dry-run":
            return dry_run(args.run, d)
        kube = Kube(context, namespace, kubectl=[args.kubectl])
        if args.mode == "preflight":
            problems = preflight(kube)
            _, demo_problems = demo_preflight(kube, args.langs, args.tag or demo_tag())
            problems += demo_problems
            if not problems:
                gate = guard.entry_read(KubeReader(kube), guard_config(d, time.time(), args.langs), time.time(),
                                        clock=time.time, sleep=time.sleep)
                append_jsonl(d / "guard.jsonl", {"phase": "preflight", **gate.as_json()})
                for i in gate.items:
                    print(f"{i.state.upper():8} {i.name}: {i.detail}")
                if gate.blind or gate.breaches:
                    problems.append("entry gate: " + ("blind read" if gate.blind else gate.reason())
                                    + " — ask the platform session")
            for prob in problems:
                print(f"preflight: REFUSED {prob}")
            return 1 if problems else 0
        if args.mode == "cleanup":
            state = load_state(d)
            if args.tag:  # an explicit tag restores the demo (replicas as recorded, else 1)
                state = {**state, "tag": args.tag, "hook_applied": True,
                         "langs": state.get("langs") or args.langs}
            res = trap(kube, args.run, d, state=state, demo_sh=[str(DEMO_SH)])
            return 0 if res.ok else 1
        if args.mode == "cal":
            return cal_mode(args, kube, d)
        return run_mode(args, kube, d)
    except (SoakError, render.RenderError, k6script.PlanError) as exc:
        print(f"soak: {exc}", file=sys.stderr)
        return 1


def _start(args: argparse.Namespace, kube: Kube, d: Path):
    """Reads first (pre-flight, demo record, entry gate: review M9). Returns
    (record, reader, cfg, loop), or None after printing the refusal."""
    problems = preflight(kube)
    expected = args.tag or demo_tag()
    record, demo_problems = demo_preflight(kube, args.langs, expected)
    problems += demo_problems
    if problems:
        for prob in problems:
            print(f"preflight: REFUSED {prob}")
        return None
    reader = KubeReader(kube)
    cfg = guard_config(d, time.time(), args.langs)
    loop = Loop(args.run, kube, reader, cfg, d, fd_reader=kube.fd_sweep(args.langs).read)
    gate = guard.entry_read(reader, cfg, time.time(), clock=time.time, sleep=time.sleep)
    append_jsonl(d / "guard.jsonl", {"window": args.window, "phase": "entry", **gate.as_json()})
    if gate.blind or gate.breaches:
        print("preflight: REFUSED entry gate: " + ("blind read" if gate.blind else gate.reason())
              + " — ask the platform session (nothing written)")
        return None
    return record, reader, cfg, loop


def _state(args: argparse.Namespace, kube: Kube, record: dict, *, reservoir: bool) -> dict:
    return {"run": args.run, "tag": record["tag"], "replicas": record["replicas"], "langs": args.langs,
            "context": kube.context, "namespace": kube.namespace, "window": args.window,
            "started": time.time(), "hook_applied": False, "reservoir": reservoir}


@contextmanager
def _session(kube: Kube, run: str, d: Path, state: dict):
    """The state saved, ``caffeinate``, and the trap around the writes."""
    save_state(d, state)
    caff = start_caffeinate()

    def on_exit():
        trap(kube, run, d, state=load_state(d), demo_sh=[str(DEMO_SH)])
        if caff is not None:
            caff.terminate()

    with trapped(on_exit):
        yield


def _setup(args: argparse.Namespace, kube: Kube, reader: "KubeReader", cfg: guard.GuardConfig, d: Path,
           state: dict, record: dict) -> float | None:
    """Inside the trap: ``soak-otlp``, the positive control, the hook and its
    waits, the entry gate again. Returns the time just before the hook's
    apply (None without ``--hook``)."""
    kube.apply(args.run, [render.soak_otlp(args.run, "copy")])
    if args.control:
        ctl = positive_control(kube, reader, args.run, d)
        if ctl["verdict"] != "PASS":
            raise SoakError(f"positive control: {ctl['reason']}")
    if not args.hook:
        return None
    try:
        since = guard.copy_totals(reader, time.time())
    except guard.ReadError as exc:
        raise SoakError(f"copy totals before the hook: {exc}") from exc
    env_path = d / "soak.env"
    canary = secrets.token_hex(24)
    write_private(d / "canary", canary + "\n")
    write_private(env_path, hook_env(secrets.token_hex(4), canary, args.worker_interval))
    state["hook_applied"] = True  # before the apply: an interrupt mid-apply still restores
    state["hook_at"] = time.time()  # the trap's demo logs start here (step 1b)
    save_state(d, state)
    hook_t = time.time()
    proc = demo_apply([str(DEMO_SH)], kube.context, kube.namespace, record["tag"], args.langs, env_path)
    if proc.returncode != 0:
        raise SoakError(f"demo.sh apply with the hook: exit {proc.returncode}")
    ready = guard.wait_demo_ready(reader, cfg, time.time, time.sleep)
    if ready.state != "ok":
        raise SoakError(f"{ready.name}: {ready.detail}")
    services = [f"demo-{lang}-{app}" for lang in args.langs for app in ("worker", "api", "backend")]
    h = guard.wait_copy_counts(reader, cfg, services, time.time, time.sleep, since)
    if h.state != "ok":
        raise SoakError(f"{h.name}: {h.detail}")
    # Re-review HIGH-2: the first gate is minutes old after the waits; read it
    # again right before the window. A breach refuses (the trap restores).
    gate = guard.entry_read(reader, cfg, time.time(), clock=time.time, sleep=time.sleep)
    append_jsonl(d / "guard.jsonl", {"window": args.window, "phase": "entry-after-hook", **gate.as_json()})
    if gate.blind or gate.breaches:
        raise SoakError("entry gate after the hook: " + ("blind read" if gate.blind else gate.reason())
                        + " — ask the platform session")
    return hook_t


# The quiesce follows the k6 window at once; a longer pause than one guard
# interval plus this slack between the two is a laptop stall.
QUIESCE_GAP_SLACK_S = 30


def worker_record(ops: "CalOps", hook_t: float, langs, *, start: bool) -> dict:
    """The workers of the window (defect 2 of K8b run k8b-20261125). At the
    window start: ``cal.first_drives`` over the copy series since the hook
    (pod, ``startedAt``, ``restartCount``, the first ``demo.drive`` — Go's is
    ``unmeasured``, its t1 rule uses ``startedAt``); at t2: the Running
    workers' pod, ``startedAt`` and ``restartCount``, so ``verify.py soak
    count`` checks for a restart or a replacement against t2 and not against
    the pods the trap's restore made. A failed read is recorded as ``error``,
    never as workers."""
    at = ops.now()
    try:
        if start:
            workers = cal.first_drives(ops.copy_series(hook_t, at), ops.pods(), hook_t, langs)
        else:
            workers = soakcount.worker_state(ops.pods(), langs)
    except Exception as exc:  # noqa: BLE001 — the caller decides (refused at the start, recorded at t2)
        return {"at": at, "error": f"{exc.__class__.__name__}: {exc}"}
    return {"at": at, "workers": workers}


def worker_start_problems(rec: dict, langs, t1: float, t2: float, *, quiesce: float,
                          interval: float) -> list[str]:
    """What would make ``verify.py soak count`` reject the worker window, read
    at minute 0 against the planned t2: the start record failed, a language
    has no ``startedAt`` (Go) or first ``demo.drive`` (the others), t1 is
    before the t1 rule, or a next drive falls before t2 + quiesce (the same
    rule as ``count``)."""
    if "workers" not in rec:
        return [f"workers at the window start: {rec.get('error', 'not recorded')}"]
    workers = rec["workers"]
    live = {lang: w for lang, w in workers.items() if w.get("pod")}
    probs = soakcount.window_problems(workers, live, t1, t2, quiesce=quiesce, interval=interval, langs=langs,
                                      when="at the window start")
    return [x if x.startswith(f"{lang}:") else f"{lang}: {x}" for lang, ps in probs.items() for x in ps]


def job_completion(kube: Kube, name: str) -> float | None:
    """The k6 Job's ``status.completionTime`` (None when unreadable)."""
    try:
        job = kube.get(["jobs", name])
    except SoakError:
        return None
    return guard.sp.parse_ts((job.get("status") or {}).get("completionTime"))


def count_inputs(args: argparse.Namespace, res: WindowResult, q: WindowResult | None, *, hooked: bool,
                 k6_completion: float | None, interval: float) -> dict:
    """``count-window.json``: the inputs of ``verify.py soak count`` for this
    window and why ``count`` could not qualify it (``reasons``; then
    ``count_args`` is null). t1 = the window start, right after the k6 apply;
    t2 = max(the Job's ``completionTime``, the loop's window end) + the
    quiesce."""
    reasons = []
    if not hooked:
        reasons.append("no --hook: no workers.json, count cannot check the worker window")
    if not res.valid:
        reasons.append(res.reason or "window invalid")
    elif res.ended != "load done":
        reasons.append(f"k6 not done at window end (ended: {res.ended or 'unknown'})")
    if q is not None and not q.valid:
        reasons.append(q.reason or "quiesce invalid")
    quiesce = args.quiesce if q is not None else 0.0
    # Round 2 MEDIUM-C: t2 stays on the laptop clock (the loop's window end);
    # completionTime is the cluster's clock, recorded with the skew only.
    t2 = res.end + quiesce
    if q is not None and q.valid and q.end is not None and q.end < t2:
        reasons.append(f"quiesce ended at {guard.sp.iso(q.end)}, before t2 {guard.sp.iso(t2)}")
    k6_log = f"raw/{render.k6_name(args.window)}.log"
    argv = None
    if not reasons:
        argv = ["--t1", f"{res.start:.3f}", "--t2", f"{t2:.3f}", "--k6-log", k6_log, "--quiesce", f"{quiesce:g}",
                "--workers", "workers.json", "--worker-interval", f"{interval:g}"]
    return {"window": args.window, "t1": res.start, "t2": t2, "t1_iso": guard.sp.iso(res.start),
            "t2_iso": guard.sp.iso(t2), "k6_end": res.end, "k6_end_source": "loop window end (laptop clock)",
            "k6_completion_time": k6_completion,
            "k6_completion_skew_s": None if k6_completion is None else round(res.end - k6_completion, 3),
            "window_end": res.end, "window_ended": res.ended, "quiesce_s": quiesce,
            "short_quiesce": quiesce < soakcount.QUIESCE_S,
            "quiesce_window": q.name if q is not None else None, "quiesce_end": q.end if q is not None else None,
            "valid": not reasons, "reasons": reasons, "k6_log": k6_log,
            "workers": "workers.json" if hooked else None, "worker_interval_s": interval,
            "count_args": argv, "note": "paths relative to runs/<run>/; add --kcal <soak-cal.json>"}


def check_quiesce(args: argparse.Namespace) -> None:
    """``--quiesce``: finite, >= 0, and not below ``soakcount.QUIESCE_S``
    without ``--allow-short-quiesce`` (a shorter one also loosens count's t1
    rule, which takes the same value)."""
    if not math.isfinite(args.quiesce) or args.quiesce < 0:
        raise SoakError(f"--quiesce {args.quiesce}: a finite number of seconds, 0 or more")
    if args.quiesce < soakcount.QUIESCE_S and not args.allow_short_quiesce:
        raise SoakError(f"--quiesce {args.quiesce:g} is below {soakcount.QUIESCE_S:g}s (soakcount.QUIESCE_S); "
                        "pass --allow-short-quiesce to accept it")
    # Round 2 MEDIUM-B: without --worker-interval the hooked workers keep the
    # app's own DEMO_INTERVAL_SECONDS, which neither the minute-0 check nor
    # count's --worker-interval can know.
    if args.hook and args.rate and not args.worker_interval:
        raise SoakError("--hook --rate needs --worker-interval (the workers' DEMO_INTERVAL_SECONDS; "
                        "10800 for K8b): without it the workers keep the app default")


def run_mode(args: argparse.Namespace, kube: Kube, d: Path) -> int:
    """Reads first (pre-flight, demo record, entry gate: review M9), then the
    writes inside the trap. A loaded window runs until the k6 Job succeeds (at
    most its ``activeDeadlineSeconds``, as K-cal's k6 windows); then a quiesce
    (``--quiesce``, ``soakcount.QUIESCE_S`` by default) with the demo as the
    window left it: a guarded window read as loaded, as K-cal's quiet windows,
    so the spans in flight land before t2 and the trap's restore after it
    (defect 1 of K8b run k8b-20261125). With ``--hook`` the worker window is
    checked at minute 0 against the planned t2; a window ``count`` could not
    qualify is refused before k6 starts (the trap runs). An INT or TERM still
    runs the trap at once."""
    check_quiesce(args)
    started = _start(args, kube, d)
    if started is None:
        return 1
    record, reader, cfg, loop = started
    plan = None
    if args.rate:
        plan = k6script.K6Plan(langs=tuple(args.langs), rate=args.rate, duration_s=args.duration,
                               timeout_s=args.timeout, p99_s=args.p99)
        plan.validate()
    interval = float(args.worker_interval or soakcount.DEFAULT_WORKER_INTERVAL_S)
    state = _state(args, kube, record, reservoir=bool(plan and plan.reservoir))
    res = q = inputs = None
    with _session(kube, args.run, d, state):
        hook_t = _setup(args, kube, reader, cfg, d, state, record)
        ops = CalOps(args.run, kube, loop, reader, d)
        workers = None
        if hook_t is not None:
            workers = {"start": worker_record(ops, hook_t, args.langs, start=True)}
            cal.write_json(d / "workers.json", workers)
            if plan is not None:
                now = ops.now()
                # Round 2 MEDIUM-A: the loop ends at the first read after the
                # deadline (up to one interval later, plus that read's budget).
                t2_plan = now + plan.deadline_s + loop.interval + READ_BUDGET * loop.interval + args.quiesce
                bad = worker_start_problems(workers["start"], args.langs, now, t2_plan,
                                            quiesce=args.quiesce, interval=interval)
                if bad:
                    raise SoakError("worker window: " + "; ".join(bad) + " (count could not qualify it; not started)")
        start_load = None
        if plan is not None:
            def start_load():
                kube.apply(args.run, k6_objects(args.run, args.window, plan))
                return Load(render.k6_name(args.window), plan.pull_s, plan.deadline_s)
        duration = plan.deadline_s if plan is not None else args.duration
        res = loop.window(args.window, duration, loaded=plan is not None, gate=False, start_load=start_load)
        if plan is not None and res.started:
            done = res.valid and res.ended == "load done"
            if done and args.quiesce > 0:
                q = loop.window(f"{args.window}-q", int(math.ceil(args.quiesce)), loaded=True, gate=False)
                limit = loop.interval + QUIESCE_GAP_SLACK_S
                if q.valid and q.start is not None and res.end is not None and q.start - res.end > limit:
                    q.valid, q.reason = False, (f"laptop: {q.start - res.end:.0f}s between the window and its "
                                                f"quiesce (> {limit}s)")
            completion = job_completion(kube, render.k6_name(args.window)) if done else None
            if workers is not None:
                workers["t2"] = worker_record(ops, hook_t, args.langs, start=False)
                cal.write_json(d / "workers.json", workers)
            inputs = count_inputs(args, res, q, hooked=workers is not None, k6_completion=completion,
                                  interval=interval)
            cal.write_json(d / "count-window.json", inputs)
            loop.log(f"count window {args.window}: t1 {inputs['t1_iso']}, t2 {inputs['t2_iso']}"
                     f" ({'valid' if inputs['valid'] else 'INVALID: ' + '; '.join(inputs['reasons'])});"
                     f" count-window.json")
    ok = res is not None and res.valid and (q is None or q.valid) and (inputs is None or inputs["valid"])
    return 0 if ok else 1


# ── calibration (task K-cal-d, design K-D20) ───────────────────────────────

class CalOps:
    """The calibration driver's cluster side: k6 and quiesce windows through
    the loop (guard reads every interval, read as loaded windows), the count
    reads through K-count's readers. Writes only soak-run objects of the run."""

    def __init__(self, run: str, kube: Kube, loop: Loop, reader: "KubeReader", d: Path):
        self.run, self.kube, self.loop, self.reader, self.d = run, kube, loop, reader, d

    def now(self) -> float:
        return self.loop.clock()

    def k6(self, window: str, plan: k6script.K6Plan):
        name = render.k6_name(window)

        def start():
            self.kube.apply(self.run, k6_objects(self.run, window, plan))
            return Load(name, plan.pull_s, plan.deadline_s)

        res = self.loop.window(window, plan.deadline_s, loaded=True, gate=False, start_load=start)
        if not res.started:
            return res, None
        rc, text = self.kube.logs(f"job/{name}")
        write_evidence(self.d / "raw" / (f"{name}.log" if rc == 0 else f"{name}.log.err"), text)
        summary = k6script.parse_logs(text)[0] if rc == 0 else None
        if res.valid:  # a stopped (suspended) Job stays for the trap
            try:
                self.kube.stop_job(self.run, name, "delete")
            except SoakError as exc:
                res.findings.append(f"k6 Job {name} not deleted ({exc}); the trap removes it")
        return res, summary

    def quiet(self, window: str, seconds: float):
        return self.loop.window(window, int(math.ceil(seconds)), loaded=True, gate=False)

    def copy_series(self, t1: float, t2: float) -> list[dict]:
        return soakcount.raw_read(self.reader, soakcount.FIXED_METRIC,
                                  {"job": guard.sp.COPY_JOB, "service_name": ("=~", guard.sp.DEMO_SERVICE_RE)},
                                  t1, t2, soakcount.COPY_EXPIRY_S)[1]

    def gateway_series(self, t1: float, t2: float) -> list[dict]:
        return soakcount.raw_read(self.reader, soakcount.SPAN_METRIC,
                                  {"service_name": ("=~", guard.sp.DEMO_SERVICE_RE)},
                                  t1, t2, soakcount.GATEWAY_EXPIRY_S)[1]

    def copy_gate(self, t1: float, t2: float) -> dict:
        extra, sent = soakcount._copy_gate(self.reader, t1, t2)
        return soakcount.scoped_gates(sent, extra)["copy"]

    def pods(self) -> list[dict]:
        return self.reader.kget(["pods", "-n", guard.sp.APP_NS]).get("items") or []


def cal_plan(args: argparse.Namespace) -> cal.CalPlan:
    plan = cal.CalPlan(langs=tuple(args.langs), n=args.n, rate=args.rate, steps=tuple(args.steps),
                       step_s=args.step_duration, quiesce_s=args.quiesce, worker_interval_s=args.worker_interval,
                       drain_wait_s=args.drain_wait, timeout_s=args.timeout, p99_s=args.p99)
    plan.validate()
    return plan


def k6_memory_check(reader, cfg: guard.GuardConfig, plan: cal.CalPlan, at: float) -> str | None:
    """Code review L2, a read: the top step's k6 memory limit against the node's
    MemAvailable (the K-D13 floor query) minus the floor; a blind read refuses."""
    q = guard.sp.sel("node_memory_MemAvailable_bytes", {"job": "node-exporter"})
    try:
        res = guard.sp.cluster_only(reader.prom(q, at))
    except (guard.ReadError, guard.verify.QueryError) as exc:
        return f"k6 memory check: MemAvailable unreadable ({exc})"
    if not res:
        return "k6 memory check: no MemAvailable series"
    return plan.memory_problem(min(float(x["value"][1]) for x in res), cfg.floors["mem_available_bytes"])


def cal_mode(args: argparse.Namespace, kube: Kube, d: Path) -> int:
    """``run``'s reads, hook and trap around the calibration driver (the hook is
    always applied: the copy only sees the hooked demo)."""
    plan = cal_plan(args)
    args.hook, args.window = True, "cal"
    started = _start(args, kube, d)
    if started is None:
        return 1
    record, reader, cfg, loop = started
    problem = k6_memory_check(reader, cfg, plan, time.time())
    if problem:
        print(f"preflight: REFUSED {problem} (nothing written)")
        return 1
    rc = 1
    state = _state(args, kube, record, reservoir=False)
    with _session(kube, args.run, d, state):
        hook_t = _setup(args, kube, reader, cfg, d, state, record)
        rc = cal.Calibrator(CalOps(args.run, kube, loop, reader, d), plan, d).run(hook_t)
    return rc


def _steps(text: str) -> list[int]:
    try:
        return [int(x) for x in text.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--steps: comma-separated integers, e.g. 2,5,10,20") from exc


if __name__ == "__main__":
    sys.exit(main())
