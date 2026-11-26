"""``verify.py soak identity`` (K3) and ``soak labels`` (K5): read-only checks
over ``soak.py shut``'s ``identity-input.json`` (specs/soak-simulation SR15
"Identity (K3)", "Labels (K5)", "Restart mechanics (r6.2)"; SR12; design
K-D3, K-D9, K-D22 draft).

Reads: Prometheus ``/api/v1/series`` of ``job="dev-otel-libs/<svc>"`` (the
app-pushed series carry ``instance`` = ``service.instance.id`` and the promoted
``k8s_pod_name``; the gateway's span-metrics series share the ``job`` and carry
no ``instance``, measured 2026-11-26, so they are counted apart and not
judged), one subquery per old instance for its last sample
(``max(max_over_time(timestamp({job, instance})[900s:5s]))``; never
``timestamp(last_over_time(...))``, which returns the evaluation time), the
copy's ``otel_libs_soak_fixed_spans_total`` label sets, and one Loki instant
count per service by ``service_instance_id``. No Tempo read.

identity  per roll and service: each new pod has exactly one instance, it is
          not an old one and not ``unset``; it is the copy's
          ``service_instance_id`` (= ``exported_instance``) and, when the
          service logged in the window, a Loki ``service_instance_id``; each
          old instance's last sample is <= its container's ``finishedAt`` +
          ``OTEL_METRIC_EXPORT_INTERVAL`` + 1 s (``finishedAt`` is whole
          seconds). The nonce roll is judged; the K-shut roll is REPORT-ONLY.
labels    per service and instance (app series only): ``job`` =
          ``<service_namespace>/<service_name>`` = ``dev-otel-libs/<svc>``,
          ``instance`` = ``target_info``'s ``service_instance_id``, every
          promoted attribute present on ``target_info`` on every series with
          the same value, no other resource label on a data series; the
          series count per metric before (old instances) and after (new
          instances) of each roll. A restart makes new series (``instance``
          changes); the old ones stay queryable for the lookback, with no
          staleness marker (OTLP push).
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import soakplatform as sp
import verify

ORDER = ("FAIL", "UNQUALIFIED", "UNMEASURED", "PASS")
LAST_SAMPLE_AFTER_S = 600       # the read reaches this far past finishedAt
LAST_SAMPLE_MIN_S = 900
LAST_SAMPLE_MAX_S = 86400
LAST_SAMPLE_STEPS = 2000        # subquery steps at most; the step stays <= the 5 min lookback
LOOKBACK_S = 300
SERIES_BEFORE_S = 900
TARGET_INFO = "target_info"
NOT_RESOURCE = {"__name__", "job", "instance", "service_instance_id"}
RESTART_NOTE = ("a restart or rebuild makes new series: every new process has a new service.instance.id, so the "
                "instance label changes and the app's series set is written again; the old series get no staleness "
                "marker (OTLP push) and stay queryable for the lookback window")


class Sources:
    """Every read; tests replace it. ``r`` is the soak Reader (soakplatform)."""

    def __init__(self, r: sp.Reader) -> None:
        self.r = r

    def series(self, match: str, start: float, end: float) -> list[dict]:
        return self.r.prom_series(match, start, end)

    def prom(self, q: str, at: float) -> list[dict]:
        return self.r.prom(q, at)

    def loki_query(self, q: str, at: int) -> list[dict]:
        return self.r.b.loki_query(q, at)


def worst(states) -> str:
    states = list(states) or ["PASS"]
    return min(states, key=ORDER.index)


def label_name(attr: str) -> str:
    """A resource attribute's Prometheus label (UnderscoreEscapingWithSuffixes)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", attr)


def job_of(svc: str) -> str:
    return f"{sp.APP_NS}/{svc}"


def app_series(src: Sources, svc: str, start: float, end: float, queries: list[str]) -> tuple[list[dict], int]:
    """(series with an ``instance`` label, count of the job's other series)."""
    match = sp.sel("", {"job": job_of(svc)})
    queries.append(f"series {match} [{sp.iso(start)}, {sp.iso(end)}]")
    got = [s for s in src.series(match, start, end) if s.get("cluster") == sp.CLUSTER]
    app = [s for s in got if s.get("instance")]
    return app, len(got) - len(app)


def by_instance(series: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in series:
        e = out.setdefault(s["instance"], {"pods": set(), "series": [], "target_info": None})
        if s.get("k8s_pod_name"):
            e["pods"].add(s["k8s_pod_name"])
        if s.get("__name__") == TARGET_INFO:
            e["target_info"] = s
        else:
            e["series"].append(s)
    return out


def pod_instances(inst: dict[str, dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for iid, e in inst.items():
        for pod in e["pods"]:
            out.setdefault(pod, set()).add(iid)
    return out


def last_sample(src: Sources, svc: str, iid: str, finished_at: float, now: float,
                queries: list[str], started_at: float | None = None) -> float | None:
    """The instance's last sample time. ``timestamp()`` returns each selected
    sample's own time, so the max over a subquery is the last sample as long as
    the step is at most the 5 min lookback; the range runs from the pod's start
    (at most 24 h back, at least 15 min) to finishedAt + 10 min, so an instance
    that went quiet early is still read (review round 1 M6)."""
    inner = sp.sel("", {"job": job_of(svc), "instance": iid})
    at = min(now, finished_at + LAST_SAMPLE_AFTER_S)
    first = started_at if started_at is not None else at - LAST_SAMPLE_MIN_S
    rng = int(min(LAST_SAMPLE_MAX_S, max(LAST_SAMPLE_MIN_S, at - first + LOOKBACK_S)))
    step = int(min(LOOKBACK_S - 60, max(5, math.ceil(rng / LAST_SAMPLE_STEPS))))
    q = f"max(max_over_time(timestamp({inner})[{rng}s:{step}s]))"
    queries.append(f"{q} @ {sp.iso(at)}")
    res = src.prom(q, at)
    return float(res[0]["value"][1]) if res else None


def copy_ids(src: Sources, svc: str, start: float, end: float, queries: list[str]) -> dict:
    match = sp.sel("otel_libs_soak_fixed_spans_total", {"job": sp.COPY_JOB, "service_name": svc})
    queries.append(f"series {match} [{sp.iso(start)}, {sp.iso(end)}]")
    got = [s for s in src.series(match, start, end) if s.get("cluster") == sp.CLUSTER]
    ids = {s.get("service_instance_id", "") for s in got}
    bad = sorted({f"{s.get('service_instance_id')} != exported_instance {s.get('exported_instance')}"
                  for s in got if s.get("service_instance_id") not in (None, "unset")
                  and s.get("exported_instance") != s.get("service_instance_id")})
    return {"series": len(got), "ids": ids, "mismatch": bad}


def loki_ids(src: Sources, svc: str, start: float, end: float, queries: list[str]) -> dict[str, int]:
    w = max(1, int(end - start))
    q = f"sum by (service_instance_id) (count_over_time({{service_name={verify._quote(svc)}}}[{w}s]))"
    queries.append(f"loki {q} @ {sp.iso(end)}")
    out = {}
    for r in src.loki_query(q, int(end)):
        out[(r.get("metric") or {}).get("service_instance_id", "")] = int(float(r["value"][1]))
    return out


def judge_identity(svc: str, roll: dict, inst: dict[str, dict], copy: dict | None, loki: dict[str, int] | None,
                   last: dict[str, float | None], *, interval: float, resolution: float,
                   known: set[str] | None = None, tempo: dict[str, str] | None = None) -> dict:
    """K3 for one service and roll; ``checks`` lists every judged part with its
    state; ``report_only`` the final-flush, Loki extra-id and Tempo parts."""
    pods = pod_instances(inst)
    checks: list[tuple[str, str, str]] = []
    report: list[dict] = []
    old_ids: set[str] = set()
    for p in roll.get("old") or []:
        ids = pods.get(p["pod"], set())
        if len(ids) != 1:
            checks.append((f"old {p['pod']}", "UNMEASURED", f"{len(ids)} instances in Prometheus {sorted(ids)}"))
        old_ids |= ids
    new_ids = []
    for p in roll.get("new") or []:
        ids = pods.get(p["pod"], set())
        if not ids:
            checks.append((f"new {p['pod']}", "UNMEASURED", "no app series in Prometheus"))
            continue
        if len(ids) > 1:
            checks.append((f"new {p['pod']}", "FAIL", f"{len(ids)} instances {sorted(ids)} for one process"))
            continue
        iid = next(iter(ids))
        new_ids.append(iid)
        if iid in old_ids:
            checks.append((f"new {p['pod']}", "FAIL", f"instance {iid} is an old one"))
        elif iid in ("", "unset"):
            checks.append((f"new {p['pod']}", "FAIL", f"instance {iid!r} (D4)"))
        else:
            checks.append((f"new {p['pod']}", "PASS", f"new instance {iid}"))
        if copy is None:
            pass
        elif not copy["series"]:
            checks.append((f"copy {iid}", "UNMEASURED", "no copy series for the service in the window"))
        else:
            ok = iid in copy["ids"] and not copy["mismatch"]
            checks.append((f"copy {iid}", "PASS" if ok else "FAIL",
                           "service_instance_id = exported_instance = instance" if ok else
                           ("; ".join(copy["mismatch"]) or f"not among the copy's ids {sorted(copy['ids'])}")))
        if loki is None:
            checks.append((f"loki {iid}", "UNMEASURED", "Loki unreadable"))
        elif not loki:
            checks.append((f"loki {iid}", "UNMEASURED", "the service logged nothing in the window"))
        else:
            ok = iid in loki
            checks.append((f"loki {iid}", "PASS" if ok else "FAIL",
                           f"{loki.get(iid, 0)} records" if ok else f"not among the Loki ids {sorted(loki)}"))
    if not (roll.get("new") or []):
        checks.append(("new pods", "UNMEASURED", "no new pod recorded"))
    for p in roll.get("old") or []:
        for iid in sorted(pods.get(p["pod"], set())):
            fin = p.get("finished_at")
            got = last.get(iid)
            if fin is None:
                checks.append((f"last sample {iid}", "UNQUALIFIED", f"{p['pod']}: finishedAt not recorded"))
            elif got is None:
                checks.append((f"last sample {iid}", "UNMEASURED", "no sample found"))
            else:
                limit = fin + interval + resolution
                checks.append((f"last sample {iid}", "PASS" if got <= limit else "FAIL",
                               f"{sp.iso(got)} vs finishedAt {sp.iso(fin)} + {interval:g} s + {resolution:g} s "
                               f"({got - fin:+.1f} s)"))
            # SR7's signal: a sample at or after the deletion is the shutdown's final flush.
            dele = p.get("deletion")
            if got is not None and dele is not None:
                report.append({"check": f"final flush {iid}", "seen": got >= dele,
                               "detail": f"last sample {sp.iso(got)} vs deletion {sp.iso(dele)} ({got - dele:+.1f} s)"})
    if loki and known is not None:
        extra = sorted(set(loki) - set(known) - {""})
        if extra:
            report.append({"check": "loki extra ids", "seen": True,
                           "detail": f"Loki service_instance_id values not of a recorded pod: {extra}"})
    for iid, state in sorted((tempo or {}).items()):
        report.append({"check": f"tempo {iid}", "seen": state == "found", "detail": state})
    return {"old_instances": sorted(old_ids), "new_instances": new_ids,
            "checks": [{"check": c, "state": s, "detail": d} for c, s, d in checks],
            "report_only": report, "verdict": worst(s for _, s, _ in checks)}


def judge_labels(svc: str, iid: str, e: dict, promoted: list[str] | None) -> dict:
    """K5 for one service and instance (app series only)."""
    out = {"instance": iid, "pods": sorted(e["pods"]), "series": len(e["series"]),
           "by_metric": {}}
    for s in e["series"]:
        out["by_metric"][s["__name__"]] = out["by_metric"].get(s["__name__"], 0) + 1
    ti = e["target_info"]
    if promoted is None:
        out.update(verdict="UNMEASURED", reason="no promoted list (prometheus/kps and K1 unreadable)")
        return out
    if ti is None:
        out.update(verdict="UNMEASURED", reason="no target_info for the instance")
        return out
    want = {label_name(a) for a in promoted}
    present = sorted(want & set(ti))
    resource_only = sorted(set(ti) - want - NOT_RESOURCE)
    problems = []
    job = ti.get("job")
    if job != job_of(svc):
        problems.append(f"job {job!r}, not {job_of(svc)!r}")
    if ti.get("service_namespace") is not None and job != f"{ti['service_namespace']}/{ti.get('service_name')}":
        problems.append(f"job {job!r} is not service_namespace/service_name")
    if ti.get("service_instance_id") != iid:
        problems.append(f"instance {iid} but target_info service_instance_id {ti.get('service_instance_id')!r}")
    missing, mismatch, leaked = {}, {}, {}
    leaked_in: dict[str, set[str]] = {}
    for s in e["series"]:
        name = s["__name__"]
        if s.get("job") != job:
            problems.append(f"{name}: job {s.get('job')!r}")
        for lbl in present:
            if lbl not in s:
                missing[lbl] = missing.get(lbl, 0) + 1
            elif s[lbl] != ti[lbl]:
                mismatch[lbl] = mismatch.get(lbl, 0) + 1
        for lbl in resource_only:
            if lbl in s:
                leaked[lbl] = leaked.get(lbl, 0) + 1
                leaked_in.setdefault(lbl, set()).add(name)
    problems += [f"promoted {k} missing on {n} series" for k, n in sorted(missing.items())]
    problems += [f"promoted {k} differs from target_info on {n} series" for k, n in sorted(mismatch.items())]
    problems += [f"resource label {k} (not promoted) on {n} series: {', '.join(sorted(leaked_in[k])[:5])}"
                 + (f" (+{len(leaked_in[k]) - 5} more)" if len(leaked_in[k]) > 5 else "")
                 for k, n in sorted(leaked.items())]
    out.update(job=job, promoted_present=present, resource_only=resource_only,
               leaked_metrics={k: sorted(v) for k, v in sorted(leaked_in.items())},
               promoted_absent=sorted(want - set(ti)), problems=sorted(set(problems)),
               verdict="FAIL" if problems else "PASS" if e["series"] else "UNMEASURED")
    if not e["series"] and not problems:
        out["reason"] = "target_info only, no data series"
    return out


def _services(inp: dict) -> list[str]:
    names = set()
    for roll in (inp.get("rolls") or {}).values():
        names |= set((roll.get("services") or {}))
    return sorted(names)


def _window(inp: dict) -> tuple[float, float]:
    start = float(inp.get("hook_at") or inp["t1"]) - SERIES_BEFORE_S
    return start, float(inp["t2"])


def tempo_ids(tempo, svc: str, ids: list[str], start: float, end: float, queries: list[str]) -> dict[str, str]:
    """Report-only: a trace of the instance in Tempo, one search each, through
    ``soakextras.TempoGuard`` (sequential, rate-capped, memory stop)."""
    import soakextras as sx
    out = {}
    for iid in ids:
        q = (f"{{ resource.service.name = {verify._quote(svc)} && "
             f"resource.service.instance.id = {verify._quote(iid)} }}")
        queries.append(f"tempo {q} [{sp.iso(start)}, {sp.iso(end)}]")
        try:
            got = tempo.search(q, int(start), int(end), 1)
        except (sx.TempoStopped, sx.TempoError) as exc:
            out[iid] = f"unmeasured ({sx.redact(exc)})"
            continue
        out[iid] = "found" if got else "not found"
    return out


def action_identity(src: Sources, inp: dict, now: float, tempo=None) -> dict:
    start, end = _window(inp)
    hook = float(inp.get("hook_at") or inp["t1"])
    interval, res = float(inp.get("export_interval_s", 10)), float(inp.get("finish_resolution_s", 1))
    queries: list[str] = []
    rolls: dict[str, dict] = {}
    findings: list[str] = []
    for svc in _services(inp):
        app, other = app_series(src, svc, start, end, queries)
        inst = by_instance(app)
        pods_map = pod_instances(inst)
        known = {i for roll in (inp.get("rolls") or {}).values()
                 for side in ("old", "new") for p in ((roll.get("services") or {}).get(svc) or {}).get(side) or []
                 for i in pods_map.get(p["pod"], set())}
        copy = copy_ids(src, svc, hook, end, queries)
        try:
            loki = loki_ids(src, svc, hook, end, queries)
        except verify.QueryError as exc:
            loki = None
            findings.append(f"{svc}: Loki unreadable ({str(exc)[:200]})")
        for name, roll in (inp.get("rolls") or {}).items():
            r = (roll.get("services") or {}).get(svc)
            if r is None:
                continue
            last = {}
            for p in r.get("old") or []:
                if p.get("finished_at") is None:
                    continue
                for iid in sorted(pod_instances(inst).get(p["pod"], set())):
                    last[iid] = last_sample(src, svc, iid, float(p["finished_at"]), now, queries,
                                            p.get("started_at"))
            new_ids = sorted({i for p in r.get("new") or [] for i in pod_instances(inst).get(p["pod"], set())})
            tem = tempo_ids(tempo, svc, new_ids, hook, end, queries) if tempo is not None else None
            v = judge_identity(svc, r, inst, copy, loki, last, interval=interval, resolution=res,
                               known=known, tempo=tem)
            if not roll.get("judged", True):
                v = {**v, "result": v["verdict"], "verdict": "REPORT-ONLY"}
            rolls.setdefault(name, {})[svc] = v
        if "unset" in copy["ids"]:
            findings.append(f"{svc}: an unset service_instance_id copy series in the window (D4)")
    return {"window": f"[{sp.iso(start)}, {sp.iso(end)}]", "rolls": rolls, "findings": findings,
            "queries": queries, "bound": f"finishedAt + {interval:g} s (OTEL_METRIC_EXPORT_INTERVAL) + {res:g} s",
            "tempo": tempo.stats() if tempo is not None else "not read (--no-identity-tempo)"}


def action_labels(src: Sources, inp: dict) -> dict:
    start, end = _window(inp)
    promoted = (inp.get("promoted") or {}).get("list")
    queries: list[str] = []
    services: dict[str, dict] = {}
    for svc in _services(inp):
        app, other = app_series(src, svc, start, end, queries)
        inst = by_instance(app)
        pods = pod_instances(inst)
        rolls = {}
        for name, roll in (inp.get("rolls") or {}).items():
            r = (roll.get("services") or {}).get(svc)
            if r is None:
                continue
            before = sorted({i for p in r.get("old") or [] for i in pods.get(p["pod"], set())})
            after = sorted({i for p in r.get("new") or [] for i in pods.get(p["pod"], set())})
            rolls[name] = {"before": {i: len(inst[i]["series"]) for i in before},
                           "after": {i: len(inst[i]["series"]) for i in after}}
        judged = {iid: judge_labels(svc, iid, e, promoted) for iid, e in sorted(inst.items())}
        services[svc] = {"instances": judged, "rolls": rolls, "other_series": other,
                         "other_note": "series of this job without an instance label (the gateway's span metrics): "
                                       "counted, not judged",
                         "verdict": worst(v["verdict"] for v in judged.values()) if judged else "UNMEASURED"}
    return {"window": f"[{sp.iso(start)}, {sp.iso(end)}]", "services": services,
            "promoted": inp.get("promoted"), "note": RESTART_NOTE, "queries": queries, "findings": []}


def rows(name: str, data: dict) -> list[tuple[str, str, str]]:
    out = []
    if name == "identity":
        for roll, svcs in sorted((data.get("rolls") or {}).items()):
            for svc, v in sorted(svcs.items()):
                bad = [c for c in v["checks"] if c["state"] != "PASS"]
                detail = "; ".join(f"{c['check']}: {c['state']} {c['detail']}" for c in bad) or \
                    f"old {v['old_instances']} -> new {v['new_instances']}"
                ro = [f"{c['check']}: {'seen' if c['seen'] else 'NOT seen'}" for c in v.get("report_only") or []]
                out.append((f"{roll} {svc}", v["verdict"] + (f" ({v['result']})" if v.get("result") else ""),
                            detail + (f" | report-only: {'; '.join(ro)}" if ro else "")))
    elif name == "labels":
        for svc, v in sorted((data.get("services") or {}).items()):
            counts = "; ".join(f"{roll}: before {sum(r['before'].values())} series ({len(r['before'])} inst), "
                               f"after {sum(r['after'].values())} ({len(r['after'])} inst)"
                               for roll, r in sorted(v["rolls"].items()))
            probs = sorted({p for i in v["instances"].values() for p in i.get("problems") or []} |
                           {i["reason"] for i in v["instances"].values() if i.get("reason")})
            out.append((svc, v["verdict"], "; ".join(probs[:5]) + (" | " if probs else "") + counts
                        + f"; other series {v['other_series']}"))
    return out


def main_ident(r, args, d: Path, now: float, src: Sources | None = None) -> tuple[dict, bool]:
    """(data, ok) for ``identity`` or ``labels``."""
    import soakcount
    inp = soakcount._load_json(args.input)
    src = src or Sources(r)
    if args.action == "identity":
        tempo = None
        if not getattr(args, "no_identity_tempo", False):
            import soakextras as sx
            tempo = sx.TempoGuard(sx.Sources(r), getattr(args, "tempo_rate", sx.DEFAULT_TEMPO_RATE),
                                  getattr(args, "tempo_mem_stop", sx.DEFAULT_TEMPO_MEM_STOP / 2**30) * 2**30)
        data = action_identity(src, inp, now, tempo)
        judged = [v for roll in data["rolls"].values() for v in roll.values() if v["verdict"] != "REPORT-ONLY"]
        ok = bool(judged) and all(v["verdict"] == "PASS" for v in judged)
    else:
        data = action_labels(src, inp)
        ok = bool(data["services"]) and all(v["verdict"] == "PASS" for v in data["services"].values())
    return data, ok
