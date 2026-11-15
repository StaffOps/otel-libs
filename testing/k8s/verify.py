"""Checks what the k8s smoke Jobs and example apps sent through the cluster's
OTel agent actually landed in Tempo, Loki and Prometheus.

Read-only: every query goes through the API server's service proxy
(``kubectl get --raw /api/v1/namespaces/<ns>/services/<svc>:<port>/proxy/...``),
so no port-forward and no credentials beyond the kube context are needed.
Standard library only; run it with ``python3 -I testing/k8s/verify.py``.

Modes:
  smoke --run <id>   for each language x {grpc, http}, service e2e-<lang>-k8s-<combo>:
                     a Tempo trace rooted at "e2e-smoke-span" for that run, its
                     resource carrying k8s.namespace.name and k8s.pod.name; a Loki
                     line "e2e-smoke-log" whose trace_id is that trace's; a
                     Prometheus series e2e_smoke_runs_total from that run's pod.
  examples           current traffic only (last --fresh minutes, default 10): per
                     language, non-failed requests on the Tempo service-graph edges
                     process -> api and api -> backend (Go: api -> backend only, its
                     examples have no process client), and one sampled Loki line of
                     the language's examples whose trace_id resolves in Tempo. Run it
                     for the languages currently deployed (--langs node,dotnet).

One PASS/FAIL line per check; exit 1 when any check fails, 2 on usage errors.

Env: KUBE_CONTEXT (k8s01), NAMESPACE (dev-otel-libs, where the apps run),
     BACKENDS_NAMESPACE (monitoring), TEMPO (tempo:3200), LOKI (loki:3100),
     PROMETHEUS (kps-prometheus:9090).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass

LANGS = ("go", "python", "node", "dotnet")
COMBOS = ("grpc", "http")
# Service-graph edges (client app, server app) every language's examples must show.
EDGES = {
    "go": (("api", "backend"),),
    "python": (("process", "api"), ("api", "backend")),
    "node": (("process", "api"), ("api", "backend")),
    "dotnet": (("process", "api"), ("api", "backend")),
}
# Trace ids from Loki tried against Tempo before the examples log check fails.
MAX_TRACE_LOOKUPS = 10


class QueryError(Exception):
    """A backend query failed (kubectl error, non-JSON answer, API error status)."""


@dataclass(frozen=True)
class Backends:
    context: str
    namespace: str
    tempo: str
    loki: str
    prometheus: str

    def _get(self, service: str, path: str, params: dict[str, str | int]) -> dict:
        url = f"/api/v1/namespaces/{self.namespace}/services/{service}/proxy{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        proc = subprocess.run(
            ["kubectl", "--context", self.context, "get", "--raw", url],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode != 0:
            raise QueryError(f"{service}{path}: {proc.stderr.strip() or 'kubectl failed'}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise QueryError(f"{service}{path}: not JSON ({exc})") from exc

    def tempo_search(self, traceql: str, start: int, end: int, limit: int = 20) -> list[dict]:
        data = self._get(self.tempo, "/api/search",
                         {"q": traceql, "start": start, "end": end, "limit": limit})
        return data.get("traces") or []

    def tempo_trace(self, trace_id: str) -> dict | None:
        """The trace in Tempo's JSON, or None when Tempo does not know it."""
        try:
            return self._get(self.tempo, f"/api/traces/{trace_id.zfill(32)}", {})
        except QueryError as exc:
            if "not find" in str(exc) or "NotFound" in str(exc) or "404" in str(exc):
                return None
            raise

    def loki_lines(self, logql: str, start: int, end: int, limit: int = 50) -> list[tuple[dict, str]]:
        """(labels incl. structured metadata, line) pairs, newest first."""
        data = self._get(self.loki, "/loki/api/v1/query_range", {
            "query": logql, "start": start * 10**9, "end": end * 10**9,
            "limit": limit, "direction": "backward",
        })
        _check_status("loki", data)
        out = []
        for stream in data.get("data", {}).get("result", []):
            labels = dict(stream.get("stream") or {})
            for value in stream.get("values", []):
                # With categorized labels a third element holds structured metadata.
                extra = value[2] if len(value) > 2 and isinstance(value[2], dict) else {}
                merged = {**labels, **(extra.get("structuredMetadata") or {})}
                out.append((merged, value[1]))
        return out

    def prom_query(self, promql: str, at: int) -> list[dict]:
        """Instant query: the result vector."""
        data = self._get(self.prometheus, "/api/v1/query", {"query": promql, "time": at})
        _check_status("prometheus", data)
        return (data.get("data") or {}).get("result") or []

    def prom_series(self, match: str, start: int, end: int) -> list[dict]:
        data = self._get(self.prometheus, "/api/v1/series",
                         {"match[]": match, "start": start, "end": end})
        _check_status("prometheus", data)
        return data.get("data") or []


def _check_status(name: str, data: dict) -> None:
    if data.get("status") != "success":
        raise QueryError(f"{name}: status {data.get('status')!r}: {data.get('error', '')}")


def _quote(value: str) -> str:
    """A double-quoted TraceQL/LogQL/PromQL string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _norm_trace_id(trace_id: str) -> str:
    # Tempo drops leading zeros from trace ids in search results; Loki keeps 32 hex.
    return trace_id.lower().lstrip("0")


def _resource_attrs(trace: dict) -> list[dict[str, str]]:
    """Every resource's attributes in a Tempo trace, string values only."""
    out = []
    for batch in trace.get("batches") or trace.get("resourceSpans") or []:
        attrs = {}
        for kv in (batch.get("resource") or {}).get("attributes", []):
            value = kv.get("value") or {}
            if "stringValue" in value:
                attrs[kv["key"]] = value["stringValue"]
        out.append(attrs)
    return out


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.total = 0

    def check(self, ok: bool, name: str, detail: str) -> bool:
        self.total += 1
        if not ok:
            self.failed += 1
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)
        return ok

    def error(self, name: str, exc: Exception) -> bool:
        return self.check(False, name, f"query error: {exc}")


def verify_smoke(b: Backends, app_ns: str, run: str, langs: list[str],
                 start: int, end: int, rep: Report) -> None:
    for lang in langs:
        for combo in COMBOS:
            svc = f"e2e-{lang}-k8s-{combo}"
            trace_id = _smoke_trace(b, app_ns, svc, run, start, end, rep)
            _smoke_log(b, svc, run, trace_id, start, end, rep)
            _smoke_metric(b, svc, lang, combo, run, start, end, rep)


def _smoke_trace(b: Backends, app_ns: str, svc: str, run: str,
                 start: int, end: int, rep: Report) -> str | None:
    name = f"{svc} trace"
    q = (f"{{resource.service.name={_quote(svc)} && resource.otel_libs.run={_quote(run)}"
         f' && name="e2e-smoke-span"}}')
    try:
        traces = [t for t in b.tempo_search(q, start, end)
                  if t.get("rootTraceName") == "e2e-smoke-span"]
    except QueryError as exc:
        rep.error(name, exc)
        rep.error(f"{svc} k8s resource", exc)
        return None
    if not rep.check(bool(traces), name,
                     f"{len(traces)} trace(s) rooted at e2e-smoke-span"
                     + (f", {traces[0]['traceID']}" if traces else f" for run {run}")):
        rep.check(False, f"{svc} k8s resource", "no trace to inspect")
        return None
    trace_id = traces[0]["traceID"]
    try:
        trace = b.tempo_trace(trace_id)
    except QueryError as exc:
        rep.error(f"{svc} k8s resource", exc)
        return trace_id
    resources = [r for r in _resource_attrs(trace or {}) if r.get("service.name") == svc]
    ok = any(r.get("k8s.namespace.name") == app_ns and r.get("k8s.pod.name") for r in resources)
    pod = next((r.get("k8s.pod.name") for r in resources if r.get("k8s.pod.name")), None)
    rep.check(ok, f"{svc} k8s resource",
              f"k8s.namespace.name={resources[0].get('k8s.namespace.name') if resources else None}"
              f" k8s.pod.name={pod}")
    return trace_id


def _smoke_log(b: Backends, svc: str, run: str, trace_id: str | None,
               start: int, end: int, rep: Report) -> None:
    name = f"{svc} log"
    q = f'{{service_name={_quote(svc)}}} |= "e2e-smoke-log" | otel_libs_run={_quote(run)}'
    try:
        lines = [labels for labels, line in b.loki_lines(q, start, end) if "e2e-smoke-log" in line]
    except QueryError as exc:
        rep.error(name, exc)
        return
    if not lines:
        rep.check(False, name, f"no e2e-smoke-log line for run {run}")
        return
    ids = {labels.get("trace_id", "") for labels in lines}
    if trace_id is None:
        rep.check(False, name, f"{len(lines)} line(s), trace_id {sorted(ids)}; no Tempo trace to match")
        return
    ok = _norm_trace_id(trace_id) in {_norm_trace_id(i) for i in ids if i}
    rep.check(ok, name, f"{len(lines)} line(s), trace_id {'matches' if ok else 'differs from'} "
                        f"the Tempo trace ({sorted(ids)})")


def _smoke_metric(b: Backends, svc: str, lang: str, combo: str, run: str,
                  start: int, end: int, rep: Report) -> None:
    name = f"{svc} metric"
    # Resource attributes other than k8s ones are not labels, so the run is matched
    # through the Job's pod name: smoke-<lang>-<combo>-<run>-<suffix>.
    pod_re = f"smoke-{lang}-{combo}-{re.escape(run)}-.*"
    match = f"e2e_smoke_runs_total{{service_name={_quote(svc)},k8s_pod_name=~{_quote(pod_re)}}}"
    try:
        series = b.prom_series(match, start, end)
    except QueryError as exc:
        rep.error(name, exc)
        return
    pods = sorted({s.get("k8s_pod_name", "") for s in series})
    rep.check(bool(series), name, f"{len(series)} e2e_smoke_runs_total series, pod(s) {pods}")


def verify_examples(b: Backends, langs: list[str], fresh_min: int, end: int, rep: Report) -> None:
    """Current traffic only: edges and log lines from the last ``fresh_min`` minutes."""
    for lang in langs:
        for client, server in EDGES[lang]:
            c, s = f"example-{lang}-{client}", f"example-{lang}-{server}"
            name = f"{lang} edge {client} -> {server}"
            promql = (f"increase(traces_service_graph_request_total{{client={_quote(c)},"
                      f"server={_quote(s)},failed=\"false\"}}[{fresh_min}m]) > 0")
            try:
                result = b.prom_query(promql, end)
            except QueryError as exc:
                rep.error(name, exc)
                continue
            total = sum(float(r["value"][1]) for r in result)
            rep.check(bool(result), name,
                      f"{total:.0f} non-failed requests {c} -> {s} in the last {fresh_min}m")
        _examples_log(b, lang, end - fresh_min * 60, end, rep)


def _examples_log(b: Backends, lang: str, start: int, end: int, rep: Report) -> None:
    name = f"{lang} log -> trace"
    q = f'{{service_name=~{_quote(f"example-{lang}-.*")}}} | trace_id!=""'
    try:
        lines = b.loki_lines(q, start, end, limit=200)
    except QueryError as exc:
        rep.error(name, exc)
        return
    seen: list[tuple[str, str]] = []
    unsampled = 0
    for labels, _line in lines:
        # A record logged under a span that was not recorded (e.g. a health request
        # the helper filters) keeps its trace_id but the sampled flag (bit 0) is
        # clear: that trace is not in Tempo by design, so skip it.
        flags = labels.get("flags", "")
        if flags.isdigit() and not int(flags) & 1:
            unsampled += 1
            continue
        pair = (labels.get("service_name", "?"), labels.get("trace_id", ""))
        if pair[1] and pair not in seen:
            seen.append(pair)
    for svc, trace_id in seen[:MAX_TRACE_LOOKUPS]:
        try:
            if b.tempo_trace(trace_id) is not None:
                rep.check(True, name, f"{svc} line with trace_id {trace_id} found in Tempo")
                return
        except QueryError as exc:
            rep.error(name, exc)
            return
    window = f"in the last {(end - start) // 60}m"
    if seen:
        detail = (f"{window}: {len(lines)} line(s) with a trace_id ({unsampled} unsampled); none of "
                  f"{len(seen[:MAX_TRACE_LOOKUPS])} sampled trace ids tried resolves in Tempo")
    else:
        detail = f"{window}: no sampled example log line carries a trace_id ({len(lines)} line(s), {unsampled} unsampled)"
    rep.check(False, name, detail)


def _duration(text: str) -> int:
    m = re.fullmatch(r"(\d+)([smhd]?)", text)
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration {text!r} (e.g. 90m, 6h, 3600)")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _langs(text: str) -> list[str]:
    langs = text.replace(",", " ").split()
    bad = [lang for lang in langs if lang not in LANGS]
    if bad or not langs:
        raise argparse.ArgumentTypeError(f"unknown language(s) {bad}; known: {' '.join(LANGS)}")
    return langs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--context", default=os.environ.get("KUBE_CONTEXT", "k8s01"))
    parser.add_argument("--namespace", default=os.environ.get("NAMESPACE", "dev-otel-libs"),
                        help="namespace the smoke Jobs / example apps run in")
    parser.add_argument("--since", type=_duration, default=_duration("6h"),
                        help="smoke: look-back window (default 6h)")
    parser.add_argument("--fresh", type=int, default=10, metavar="MINUTES",
                        help="examples: edges and log lines must be newer than this (default 10)")
    parser.add_argument("--langs", type=_langs, default=list(LANGS),
                        help="languages to check (default: all four)")
    sub = parser.add_subparsers(dest="mode", required=True)
    smoke = sub.add_parser("smoke", help="check one smoke run")
    smoke.add_argument("--run", required=True, help="the run id passed to smoke-jobs.sh (RUN)")
    sub.add_parser("examples", help="check the example apps")
    args = parser.parse_args(argv)

    b = Backends(
        context=args.context,
        namespace=os.environ.get("BACKENDS_NAMESPACE", "monitoring"),
        tempo=os.environ.get("TEMPO", "tempo:3200"),
        loki=os.environ.get("LOKI", "loki:3100"),
        prometheus=os.environ.get("PROMETHEUS", "kps-prometheus:9090"),
    )
    end = int(time.time())
    start = end - args.since
    rep = Report()
    if args.mode == "smoke":
        verify_smoke(b, args.namespace, args.run, args.langs, start, end, rep)
    else:
        verify_examples(b, args.langs, args.fresh, end, rep)
    print(f"verify {args.mode}: {rep.total - rep.failed}/{rep.total} checks passed", flush=True)
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
