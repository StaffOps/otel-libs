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
                         [--hook] [--worker-interval <s>] [--tag <t>]
                         the pre-flight and the entry gate (both before any
                         write); ``soak-otlp`` (copy); with --hook the demo
                         re-applied through demo.sh's DEMO_ENV_FILE (endpoint
                         soak-otlp, a per-run canary header, a new
                         DEMO_RESTART_NONCE), then guard (g) polled until the
                         rolled pods are Ready and (h) as an increase since the
                         hook; with --rate the k6 ConfigMap and Job; a guard read
                         every 60 s under ``caffeinate`` (macOS) with a heartbeat
                         until the window ends. The trap always runs.
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
``runs/<run>/raw/`` (errors to ``<job>.log.err``); (2) delete only the objects
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

import guard  # noqa: E402
import k6script  # noqa: E402
import render  # noqa: E402
from render import NAMESPACE, ROLE_LABEL, RUN_LABEL  # noqa: E402

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

    def get_raw(self, url: str) -> dict:
        out = self._ok(["get", "--raw", url], namespaced=False)
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise SoakError(f"get --raw {url}: not JSON ({exc})") from exc

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

    def logs(self, target: str) -> tuple[int, str]:
        """``kubectl logs`` without ``--timestamps`` (review H1: a timestamp
        prefix breaks the summary markers and k6's long lines)."""
        proc = self._run(["logs", target, "--all-containers"], timeout=120)
        return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr

    def stop_job(self, run: str, name: str, how: str) -> None:
        """Stops a k6 Job: ``suspend`` (its pods go, the Job stays for the trap)
        or ``delete`` (by the run's k6 label and the Job's name)."""
        if how == "suspend":
            self._ok(["patch", "job", name, "--type=merge", "-p", '{"spec":{"suspend":true}}'])
        else:
            self._ok(["delete", "jobs", "-l", f"{RUN_LABEL}={render.check_run(run)},{ROLE_LABEL}=k6",
                      "--field-selector", f"metadata.name={name}", "--ignore-not-found"], timeout=60)

    def namespace_label(self) -> str | None:
        ns = self.get(["namespace", self.namespace], namespaced=False)
        return ((ns.get("metadata") or {}).get("labels") or {}).get(MESH_LABEL)

    def set_ambient(self) -> None:
        self._ok(["label", "namespace", self.namespace, f"{MESH_LABEL}=ambient", "--overwrite"],
                 namespaced=False)


class KubeReader(guard.sp.Reader):
    """K1's read-only reader (``soakplatform.Reader``) with every call made
    through this loop's ``Kube`` (so ``--kubectl`` and the read deadline apply
    to the guard reads too): Prometheus through the API server's service proxy,
    ``kubectl get``, pod logs. Env: BACKENDS_NAMESPACE, PROMETHEUS as verify.py."""

    def __init__(self, kube: Kube):
        b = guard.verify.Backends(
            context=kube.context,
            namespace=os.environ.get("BACKENDS_NAMESPACE", "monitoring"),
            tempo=os.environ.get("TEMPO", "tempo:3200"),
            loki=os.environ.get("LOKI", "loki:3100"),
            prometheus=os.environ.get("PROMETHEUS", "kps-prometheus:9090"),
        )
        super().__init__(b, kube.context)
        self.kube = kube

    def _prom_api(self, path: str, params: dict) -> list[dict]:
        url = (f"/api/v1/namespaces/{self.b.namespace}/services/{self.b.prometheus}/proxy{path}?"
               + urllib.parse.urlencode(params))
        try:
            data = self.kube.get_raw(url)
        except SoakError as exc:
            raise guard.ReadError(str(exc)) from exc
        if data.get("status") != "success":
            raise guard.ReadError(f"prometheus: {data.get('error') or data.get('status')}")
        return (data.get("data") or {}).get("result") or []

    def prom(self, q: str, at: float) -> list[dict]:
        return self._prom_api("/api/v1/query", {"query": q, "time": int(at)})

    def prom_range(self, q: str, start: float, end: float, step: int) -> list[dict]:
        return self._prom_api("/api/v1/query_range", {"query": q, "start": int(start), "end": int(end),
                                                      "step": step})

    def kget(self, args: list[str]) -> dict:
        try:
            return self.kube.get(args, namespaced=False)
        except SoakError as exc:
            raise guard.ReadError(str(exc)) from exc

    def _run(self, args: list[str]) -> str:
        proc = self.kube._run(args, namespaced=False)
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
        _step(res, "delete", delete)
        _step(res, "namespace label", label)
        _step(res, "demo restore", restore)
    finally:
        try:
            append_jsonl(d / "trap.jsonl", {"at": time.time(), "saved": res.saved, "log_bytes": res.log_bytes,
                                             "deleted": res.deleted, "left": res.left,
                                             "label_written": res.label_written,
                                             "demo_restored": res.demo_restored, "errors": res.errors})
        except OSError as exc:
            res.errors.append(f"trap.jsonl: {exc}")
    for line in (f"trap: saved {len(res.saved)} k6 log(s) {res.log_bytes or ''}".rstrip(),
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
                if "running" in (c.get("state") or {}):
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
            gap = self.heartbeat()
            if gap:
                res.valid, res.reason = False, gap + "; " + self.stop_load(load, "suspend", res.start)
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
                break
            if read.breaches:
                res.valid, res.reason = False, read.reason() + "; " + self.stop_load(load, "suspend", res.start)
                break
            if load:
                st = self.load_state(load, res.start)
                if st == "done":
                    break
                if st:
                    res.valid, res.reason = False, st
                    break
            if self.clock() - res.start >= duration_s:
                break
            self.sleep(self.interval)
        return self._finish(res)

    def _finish(self, res: WindowResult) -> WindowResult:
        res.end = self.clock()
        append_jsonl(self.d / "windows.jsonl", res.as_json())
        self.log(f"window {res.name}: {'started' if res.started else 'NOT started'}, "
                 f"{'valid' if res.valid else 'INVALID'}{': ' + res.reason if res.reason else ''}")
        return res


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
        return run_mode(args, kube, d)
    except (SoakError, render.RenderError, k6script.PlanError) as exc:
        print(f"soak: {exc}", file=sys.stderr)
        return 1


def run_mode(args: argparse.Namespace, kube: Kube, d: Path) -> int:
    """Reads first (pre-flight, demo record, entry gate: review M9), then the
    writes inside the trap."""
    problems = preflight(kube)
    expected = args.tag or demo_tag()
    record, demo_problems = demo_preflight(kube, args.langs, expected)
    problems += demo_problems
    if problems:
        for prob in problems:
            print(f"preflight: REFUSED {prob}")
        return 1
    reader = KubeReader(kube)
    cfg = guard_config(d, time.time(), args.langs)
    loop = Loop(args.run, kube, reader, cfg, d)
    gate = guard.entry_read(reader, cfg, time.time(), clock=time.time, sleep=time.sleep)
    append_jsonl(d / "guard.jsonl", {"window": args.window, "phase": "entry", **gate.as_json()})
    if gate.blind or gate.breaches:
        print("preflight: REFUSED entry gate: " + ("blind read" if gate.blind else gate.reason())
              + " — ask the platform session (nothing written)")
        return 1
    plan = None
    if args.rate:
        plan = k6script.K6Plan(langs=tuple(args.langs), rate=args.rate, duration_s=args.duration,
                               timeout_s=args.timeout, p99_s=args.p99)
        plan.validate()
    state = {"run": args.run, "tag": record["tag"], "replicas": record["replicas"], "langs": args.langs,
             "context": kube.context, "namespace": kube.namespace, "window": args.window,
             "started": time.time(), "hook_applied": False, "reservoir": bool(plan and plan.reservoir)}
    save_state(d, state)
    caff = start_caffeinate()

    def on_exit():
        trap(kube, args.run, d, state=load_state(d), demo_sh=[str(DEMO_SH)])
        if caff is not None:
            caff.terminate()

    res = None
    with trapped(on_exit):
        kube.apply(args.run, [render.soak_otlp(args.run, "copy")])
        services, since = [], None
        if args.hook:
            try:
                since = guard.copy_totals(reader, time.time())
            except guard.ReadError as exc:
                raise SoakError(f"copy totals before the hook: {exc}") from exc
            env_path = d / "soak.env"
            canary = secrets.token_hex(24)
            write_private(d / "canary", canary + "\n")
            write_private(env_path, hook_env(secrets.token_hex(4), canary, args.worker_interval))
            state["hook_applied"] = True  # before the apply: an interrupt mid-apply still restores
            save_state(d, state)
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
        start_load = None
        if plan is not None:
            def start_load():
                kube.apply(args.run, k6_objects(args.run, args.window, plan))
                return Load(render.k6_name(args.window), plan.pull_s, plan.deadline_s)
        res = loop.window(args.window, args.duration, loaded=plan is not None, gate=False, start_load=start_load)
    return 0 if res is not None and res.valid else 1


if __name__ == "__main__":
    sys.exit(main())
