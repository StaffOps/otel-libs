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
  demo --run <id>    the demo apps of specs/demo-environment (DE-D11): checks a-g per
                     language against testing/k8s/demo/expected.json over the window
                     [now - settle - W, now - settle] (--window 15m, --settle 2m, the
                     minimum). Verdicts PASS / FAIL / XFAIL / XPASS / NA / UNEXPECTED /
                     BLOCKED and a language x cell matrix; exit 1 on FAIL, XPASS,
                     UNEXPECTED or BLOCKED. --dry-run runs the read-only pre-flight
                     only (Prometheus flags and start time, exemplar storage, the run's
                     target_info series and pods).

smoke/examples: one PASS/FAIL line per check; exit 1 when any check fails. Every
mode exits 2 on usage errors.

Env: KUBE_CONTEXT (k8s01), NAMESPACE (dev-otel-libs, where the apps run),
     BACKENDS_NAMESPACE (monitoring), TEMPO (tempo:3200), LOKI (loki:3100),
     PROMETHEUS (kps-prometheus:9090).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

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


# kubectl stderr fragments of a failed connection to the API server: the request never
# got an answer, so it is retried (an HTTP error answer from a backend never is).
TRANSPORT_ERRORS = ("unable to connect to the server", "connection reset",
                    "tls handshake timeout", "i/o timeout")
# Pauses (seconds) before the 2nd and 3rd attempt of a request that failed in transport.
TRANSPORT_BACKOFF = (1, 3)
_sleep = time.sleep


class QueryError(Exception):
    """A backend query failed (kubectl error, non-JSON answer, API error status)."""


def _is_transport_error(stderr: str) -> bool:
    low = stderr.lower()
    # "Error from server (...)" is an HTTP answer (4xx/5xx), even when its body quotes a
    # backend's own transport error: never retried.
    if "error from server" in low:
        return False
    return any(marker in low for marker in TRANSPORT_ERRORS)


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
        for attempt in range(len(TRANSPORT_BACKOFF) + 1):
            try:
                proc = subprocess.run(
                    ["kubectl", "--context", self.context, "get", "--raw", url],
                    capture_output=True, text=True, timeout=60, check=False,
                )
                returncode, stderr = proc.returncode, proc.stderr
            except subprocess.TimeoutExpired:  # no answer within 60 s: transport too
                returncode, stderr = -1, "kubectl timed out after 60s (i/o timeout)"
            if returncode == 0 or not _is_transport_error(stderr):
                break
            if attempt < len(TRANSPORT_BACKOFF):
                _sleep(TRANSPORT_BACKOFF[attempt])
        if returncode != 0:
            raise QueryError(f"{service}{path}: {stderr.strip() or 'kubectl failed'}")
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
        return [(labels, line) for _ts, labels, line in self.loki_entries(logql, start, end, limit)]

    def loki_entries(self, logql: str, start: int, end: int, limit: int = 50) -> list[tuple[str, dict, str]]:
        """(timestamp ns, labels incl. structured metadata, line), newest first."""
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
                out.append((str(value[0]), merged, value[1]))
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

    def prom_query_range(self, promql: str, start: int, end: int, step: int) -> list[dict]:
        """Range query: the result matrix ({"metric", "values": [[t, "v"], ...]})."""
        data = self._get(self.prometheus, "/api/v1/query_range",
                         {"query": promql, "start": start, "end": end, "step": step})
        _check_status("prometheus", data)
        return (data.get("data") or {}).get("result") or []

    def prom_exemplars(self, promql: str, start: int, end: int) -> list[dict]:
        """/api/v1/query_exemplars: [{"seriesLabels", "exemplars": [{"labels", ...}]}]."""
        data = self._get(self.prometheus, "/api/v1/query_exemplars",
                         {"query": promql, "start": start, "end": end})
        _check_status("prometheus", data)
        return data.get("data") or []

    def prom_status(self, which: str) -> dict:
        """/api/v1/status/<which> (flags, runtimeinfo): the data object."""
        data = self._get(self.prometheus, f"/api/v1/status/{which}", {})
        _check_status("prometheus", data)
        return data.get("data") or {}

    def tempo_metrics(self, traceql: str, start: int, end: int, step: int) -> list[dict]:
        """TraceQL metrics query_range: the ``series`` list."""
        data = self._get(self.tempo, "/api/metrics/query_range",
                         {"q": traceql, "start": start, "end": end, "step": f"{step}s"})
        return data.get("series") or []

    def loki_query(self, logql: str, at: int) -> list[dict]:
        """Loki instant (metric) query: the result vector."""
        data = self._get(self.loki, "/loki/api/v1/query", {"query": logql, "time": at * 10**9})
        _check_status("loki", data)
        return (data.get("data") or {}).get("result") or []


def kubectl_json(context: str, args: list[str]) -> dict:
    """Read-only ``kubectl --context <ctx> get <args> -o json`` (shared with soak K1, K-D3).

    The verb is always ``get``: this wrapper cannot write to the cluster.
    """
    proc = subprocess.run(
        ["kubectl", "--context", context, "get", *args, "-o", "json"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise QueryError(f"kubectl get {' '.join(args)}: {proc.stderr.strip() or 'kubectl failed'}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise QueryError(f"kubectl get {' '.join(args)}: not JSON ({exc})") from exc


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


# ─── demo mode (specs/demo-environment DE-D10, DE-D11; task DE5) ─────────────────────

DEMO_APPS = ("worker", "api", "backend")
DEMO_SCOPE = "otel-libs-demo"
ERROR_SCENARIOS = ("business_error", "db_error", "unhandled")
BACKEND_SCENARIOS = ("ok", "business_error", "db_error")
# DE-D8 pinned error.type literals and the app whose demo span owns the failure.
PINNED_TYPES = {"business_error": "OrderRejectedError", "db_error": "OrderExistsError",
                "unhandled": "DemoUnhandledError"}
OWNER = {"business_error": ("backend", "demo.order.process"),
         "db_error": ("backend", "demo.order.process"),
         "unhandled": ("api", "demo.order.accept")}
GRPC_MESSAGES = {"business_error": "order rejected", "db_error": "order exists"}
HTTP_CODES = {"business_error": "422", "db_error": "409", "unhandled": "500"}
# DE-D9 explicit bucket boundaries of demo.order.duration (seconds).
DURATION_BOUNDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)
# DE-D7: one order every 5 s, a cycle of 10 = 7 ok + one of each error. Per 15 min
# ≈ 360 OTLP records per language (two INFO per ok order, the error and WARN records).
LOG_RECORDS_PER_SECOND = 360 / 900
OK_ORDERS_PER_SECOND = 7 / 50
MIN_SETTLE = 120  # DE-D11: the service_graph connector flushes every 60 s.
STARTUP_GRACE = 60  # DE-D11 r3.6: the window starts >= 60 s after the youngest app pod start.
RETRY_DELAY = 60  # DE-D11 r3.3: a late trace / span is retried once after 60 s.
MAX_EXEMPLAR_TRACES = 20
MAX_D_TRACES = 5
MAX_E_TRACES = 5
POLL_ROOT_SAMPLE = 50
TEMPO_PAGE_LIMIT = 500
LOKI_PAGE_LIMIT = 2000
MIN_SLICE = 60
STEP = 30
EXPECTED_JSON = Path(__file__).resolve().parent / "demo" / "expected.json"
# b cells: (cell, app, metric selector, extra label matchers).
EXEMPLAR_CELLS = {
    "b.http_server": ("api", "http_server_request_duration_seconds_bucket", ""),
    "b.rpc_server": ("backend", "rpc_server_call_duration_seconds_bucket", ""),
    "b.db_client": ("backend", "db_client_operation_duration_seconds_bucket", ""),
    "b.demo_order": ("backend", "demo_order_duration_seconds_bucket", ""),
}
# .NET records the gRPC server duration as an HTTP server duration (design "Metric map").
EXEMPLAR_OVERRIDES = {
    ("dotnet", "b.rpc_server"): ("backend", "http_server_request_duration_seconds_bucket",
                                 'http_route="/demo.v1.OrderService/PlaceOrder"'),
}
DEMO_CELLS = ("a", *EXEMPLAR_CELLS, "c", "d.demo", "d.grpc", "d.http", "d.db",
              "e.trace", "e.edges", "e.edge.db", "g.secret", "g.sentinel.db", "g.sentinel.app")
EXPECT_RE = re.compile(r"^(pass|xfail:[A-Za-z0-9][A-Za-z0-9-]*|n/a:[a-z0-9][a-z0-9-]*)$")
# DR12 hygiene patterns. The 48-hex token is anchored: no longer hex run around it.
HEX48_RE = re.compile(r"(^|[^0-9a-fA-F])[0-9a-fA-F]{48}([^0-9a-fA-F]|$)")
SECRET_RES = (("hex48", HEX48_RE), ("password=", re.compile(r"(?i)password=")),
              ("userinfo", re.compile(r"://[^/@]+:[^@]+@")))
POD_RE = re.compile(r"^demo-(go|python|node|dotnet)-(worker|api|backend)-[a-z0-9-]+$")
BACKING_PODS = "demo-postgres-.*|demo-redis-.*"
CONNECTION_ERROR_RE = r"[0-9]+"  # TraceQL regexes are anchored: a status code is digits only.

# Observed states of one cell, before the expectation is applied.
OBS_PASS, OBS_FAIL, OBS_NODATA = "pass", "fail", "nodata"
OBS_BLOCKED, OBS_UNEXPECTED, OBS_ERROR = "blocked", "unexpected", "error"
FAILING_VERDICTS = ("FAIL", "XPASS", "UNEXPECTED", "BLOCKED")


def svc(lang: str, app: str) -> str:
    return f"demo-{lang}-{app}"


def run_tokens(run: str) -> tuple[str, ...]:
    return (f"dup-{run}", f"rej-{run}", f"unh-{run}")


# ── ids ──

def norm_id(value: str, width: int) -> str:
    """Hex id of ``width`` characters from any encoding the backends use ("Id encodings").

    Tempo's trace JSON carries base64 ids, Tempo search hex without leading zeros,
    Loki and Prometheus exemplars 32/16 hex. A value with a character outside
    [0-9a-fA-F] (``=``, ``+``, ``/``, other letters) is base64; anything else is hex.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]+", text) and len(text) <= width:
        return text.lower().zfill(width)
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError):
        return ""  # invalid: the caller counts it as a miss
    if not raw or len(raw) * 2 > width:
        return ""
    return raw.hex().zfill(width)


def norm_trace(value: str) -> str:
    return norm_id(value, 32)


def norm_span(value: str) -> str:
    return norm_id(value, 16)


def severity_number(labels: dict) -> int:
    try:
        return int(str(labels.get("severity_number", "0")).strip() or "0")
    except ValueError:
        return 0


def sampled(labels: dict) -> bool:
    """W3C sampled flag (``flags & 1``); records without flags count as sampled."""
    flags = str(labels.get("flags", "")).strip()
    if not flags:
        return True
    try:
        return bool(int(flags, 10) & 1)
    except ValueError:
        return True


# ── Tempo trace model ──

_KINDS = {0: "unspecified", 1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}
_STATUSES = {0: "unset", 1: "ok", 2: "error"}


def _attr_value(value: dict) -> str:
    for key in ("stringValue", "intValue", "boolValue", "doubleValue"):
        if key in value:
            v = value[key]
            return ("true" if v else "false") if isinstance(v, bool) else str(v)
    return json.dumps(value, sort_keys=True)


def _attrs(items: list[dict] | None) -> dict[str, str]:
    return {kv["key"]: _attr_value(kv.get("value") or {}) for kv in items or [] if "key" in kv}


def _enum(value: object, prefix: str, table: dict[int, str]) -> str:
    if isinstance(value, int):
        return table.get(value, "unspecified")
    text = str(value or "").lower()
    return text[len(prefix):] if text.startswith(prefix) else (text or table[0])


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str
    name: str
    kind: str
    service: str
    scope: str
    status: str
    message: str
    attrs: dict[str, str]
    events: list[tuple[str, dict[str, str]]]
    links: list[tuple[str, str]]
    start_ns: int = 0
    resource: dict[str, str] = field(default_factory=dict)

    def is_db_client(self) -> bool:
        return self.kind == "client" and bool(self.attrs.get("db.system.name") or self.attrs.get("db.system"))

    def is_postgres(self) -> bool:
        return self.kind == "client" and "postgresql" in (
            self.attrs.get("db.system.name"), self.attrs.get("db.system"))

    def values(self):
        """(field label, value) for every scanned value; labels never carry the value."""
        yield "name", self.name
        yield "status", self.message
        for key, value in self.attrs.items():
            yield f"attr {key}", value
        for name, attrs in self.events:
            for key, value in attrs.items():
                yield f"event {name}.{key}", value
        for key, value in self.resource.items():
            yield f"resource {key}", value

    def detail_keys(self) -> list[str]:
        """exception.message / exception.stacktrace / grpc.error_message on the span or its events."""
        bad = {"exception.message", "exception.stacktrace", "grpc.error_message"}
        found = [f"attr {k}" for k in self.attrs if k in bad]
        found += [f"event {n}.{k}" for n, a in self.events for k in a if k in bad]
        return found


def parse_trace(data: dict) -> list[Span]:
    """Spans of a Tempo /api/traces/<id> answer (``batches`` or ``resourceSpans``)."""
    out = []
    for batch in data.get("batches") or data.get("resourceSpans") or []:
        res = _attrs((batch.get("resource") or {}).get("attributes"))
        groups = batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []
        for group in groups:
            scope = (group.get("scope") or group.get("instrumentationLibrary") or {}).get("name", "")
            for s in group.get("spans") or []:
                status = s.get("status") or {}
                out.append(Span(
                    trace_id=norm_trace(s.get("traceId", "")),
                    span_id=norm_span(s.get("spanId", "")),
                    parent_id=norm_span(s.get("parentSpanId", "")),
                    name=s.get("name", ""),
                    kind=_enum(s.get("kind", 0), "span_kind_", _KINDS),
                    service=res.get("service.name", ""),
                    scope=scope,
                    status=_enum(status.get("code", 0), "status_code_", _STATUSES),
                    message=status.get("message", ""),
                    attrs=_attrs(s.get("attributes")),
                    events=[(e.get("name", ""), _attrs(e.get("attributes"))) for e in s.get("events") or []],
                    links=[(norm_trace(link.get("traceId", "")), norm_span(link.get("spanId", "")))
                           for link in s.get("links") or []],
                    start_ns=int(s.get("startTimeUnixNano") or 0),
                    resource=res,
                ))
    return out


def descends(span: Span, ancestor: Span, by_id: dict[str, Span]) -> bool:
    """True when ``ancestor`` is on ``span``'s parent chain (framework spans in between allowed)."""
    seen = set()
    cur = span
    while cur.parent_id and cur.parent_id not in seen:
        if cur.parent_id == ancestor.span_id:
            return True
        seen.add(cur.parent_id)
        nxt = by_id.get(cur.parent_id)
        if nxt is None:
            return False
        cur = nxt
    return False


class TraceCache:
    """Tempo traces by normalized id; a miss (trace or span) is retried once after 60 s."""

    def __init__(self, b: Backends, sleep=time.sleep, retry_delay: int = RETRY_DELAY) -> None:
        self.b = b
        self.sleep = sleep
        self.retry_delay = retry_delay
        self._traces: dict[str, list[Span]] = {}

    def get(self, trace_id: str, refresh: bool = False) -> list[Span] | None:
        tid = norm_trace(trace_id)
        if not refresh and tid in self._traces:
            return self._traces[tid]
        data = self.b.tempo_trace(tid)
        if data is None:
            return None
        spans = parse_trace(data)
        self._traces[tid] = spans
        return spans

    def resolve(self, pairs: list[tuple[str, str]]) -> tuple[int, list[tuple[str, str]], int]:
        """(resolved count, final misses, retried count) for (trace id, span id) pairs.

        A pair resolves when the trace exists and, if a span id is given, that span is
        in it. Misses of the first pass are fetched again once after ``retry_delay``.
        """
        def ok(tid: str, sid: str, refresh: bool) -> bool:
            spans = self.get(tid, refresh=refresh)
            return spans is not None and (not sid or any(s.span_id == norm_span(sid) for s in spans))

        misses = [(t, s) for t, s in pairs if not ok(t, s, False)]
        retried = len(misses)
        if misses:
            self.sleep(self.retry_delay)
            misses = [(t, s) for t, s in misses if not ok(t, s, True)]
        return len(pairs) - len(misses), misses, retried


# ── expectations and verdicts ──

def load_expected(path: Path = EXPECTED_JSON) -> dict[str, dict[str, dict[str, str]]]:
    """{lang: {cell: {"expect", "ref"}}} from expected.json, validated strictly."""
    data = json.loads(path.read_text(encoding="utf-8"))
    langs = data.get("expected")
    if not isinstance(langs, dict):
        raise ValueError(f"{path}: missing object 'expected'")
    if "cells" in data and list(data["cells"]) != list(DEMO_CELLS):
        raise ValueError(f"{path}: 'cells' {data['cells']} != the checker's cells {list(DEMO_CELLS)}")
    out: dict[str, dict[str, dict[str, str]]] = {}
    for lang in LANGS:
        cells = langs.get(lang)
        if not isinstance(cells, dict):
            raise ValueError(f"{path}: no cells for language {lang!r}")
        extra = sorted(set(cells) - set(DEMO_CELLS))
        missing = [c for c in DEMO_CELLS if c not in cells]
        if extra or missing:
            raise ValueError(f"{path}: {lang}: unknown cells {extra}, missing cells {missing}")
        out[lang] = {}
        for cell, value in cells.items():
            entry = {"expect": value} if isinstance(value, str) else dict(value)
            if not EXPECT_RE.match(str(entry.get("expect", ""))):
                raise ValueError(f"{path}: {lang} {cell}: bad expectation {entry.get('expect')!r} "
                                 "(pass, xfail:<task>, n/a:<reason>)")
            also = entry.get("also_xfail", [])
            if also and (not entry["expect"].startswith("xfail:") or not isinstance(also, list)
                         or not all(isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", t)
                                    for t in also)):
                raise ValueError(f"{path}: {lang} {cell}: 'also_xfail' must be a list of task ids "
                                 "on an xfail cell")
            reason = entry.get("reason", [])
            if entry["expect"].startswith("xfail:"):
                if not (isinstance(reason, list) and reason and all(isinstance(r, str) and r for r in reason)):
                    raise ValueError(f"{path}: {lang} {cell}: an xfail cell needs 'reason': a non-empty "
                                     "list of problem substrings (DE-D11 r3.7)")
            elif reason:
                raise ValueError(f"{path}: {lang} {cell}: 'reason' is only for xfail cells")
            requires = entry.get("requires_series", False)
            if requires and not entry["expect"].startswith("n/a:"):
                raise ValueError(f"{path}: {lang} {cell}: 'requires_series' is only for n/a cells")
            out[lang][cell] = {"expect": entry["expect"], "ref": str(entry.get("ref", "")),
                               "also": [str(t) for t in also], "reason": [str(r) for r in reason],
                               "requires_series": bool(requires)}
    unknown = sorted(set(langs) - set(LANGS))
    if unknown:
        raise ValueError(f"{path}: unknown languages {unknown}")
    return out


def judge(expect: str, state: str) -> str:
    """Verdict for an observed state against the expected cell (strict xfail and n/a, DR9)."""
    if state == OBS_ERROR:
        return "FAIL"
    if state in (OBS_BLOCKED, OBS_UNEXPECTED):
        return state.upper()
    if expect.startswith("n/a:"):
        return "NA" if state == OBS_NODATA else "UNEXPECTED"
    if expect.startswith("xfail:"):
        return {OBS_FAIL: "XFAIL", OBS_PASS: "XPASS"}.get(state, "FAIL")
    return "PASS" if state == OBS_PASS else "FAIL"


@dataclass
class Obs:
    state: str
    detail: str
    # Problems behind a "fail" state; an xfail cell is XFAIL only when every problem
    # matches one of its reasons (DE-D11 r3.7). Empty = the detail is the one problem.
    problems: list[str] = field(default_factory=list)
    # b cells: whether the metric's series exist for the run (None = not looked up).
    series: bool | None = None


def unmatched(obs: Obs, reasons: list[str]) -> list[str]:
    """Problems of a failing cell that no expected reason explains."""
    problems = obs.problems or [obs.detail]
    return [p for p in problems if not any(r in p for r in reasons)]


class DemoReport:
    def __init__(self, expected: dict[str, dict[str, dict[str, str]]], out=None) -> None:
        self.expected = expected
        self.out = out or sys.stdout
        self.cells: dict[tuple[str, str], str] = {}
        self.preflight_blocked = False
        self.blocked_lines = 0

    def _print(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def info(self, text: str) -> None:
        self._print(f"INFO        {text}")

    def cell(self, lang: str, cell: str, obs: Obs) -> str:
        entry = self.expected[lang][cell]
        verdict = judge(entry["expect"], obs.state)
        extra = ""
        if verdict == "XFAIL":
            other = unmatched(obs, entry.get("reason", []))
            if other:  # the cell fails for a reason its task does not own
                verdict = "FAIL"
                extra = f"; problems outside the xfail reasons: {other[:4]}"
        if verdict == "NA" and entry.get("requires_series") and obs.series is False:
            verdict = "FAIL"
            extra = "; the n/a cell requires the metric series to exist and they do not"
        self.cells[(lang, cell)] = verdict
        also = "".join(f"+{t}" for t in entry.get("also", []))
        note = "" if entry["expect"] == "pass" else f" [expected {entry['expect']}{also}]"
        if entry["ref"] and verdict not in ("PASS",):
            note += f" (ref: {entry['ref']})"
        self._print(f"{verdict:<11} {lang} {cell}: {obs.detail}{extra}{note}")
        return verdict

    def blocked(self, text: str) -> None:
        self.preflight_blocked = True
        self.blocked_lines += 1
        self._print(f"BLOCKED     {text}")

    def failed(self) -> bool:
        return self.preflight_blocked or any(v in FAILING_VERDICTS for v in self.cells.values())

    def matrix(self, langs: list[str]) -> None:
        width = max(len(c) for c in DEMO_CELLS)
        self._print("")
        self._print(f"{'cell':<{width}}  " + "  ".join(f"{lang:<10}" for lang in langs))
        for cell in DEMO_CELLS:
            row = [self.cells.get((lang, cell), "-") for lang in langs]
            self._print(f"{cell:<{width}}  " + "  ".join(f"{v:<10}" for v in row))
        counts: dict[str, int] = {}
        for v in self.cells.values():
            counts[v] = counts.get(v, 0) + 1
        summary = ", ".join(f"{k} {counts[k]}" for k in sorted(counts))
        # Pre-flight BLOCKED lines are not cells but fail the run: the tally names them.
        pre = f"; pre-flight BLOCKED {self.blocked_lines}" if self.blocked_lines else ""
        self._print(f"verify demo: {summary or 'no cells'}{pre}; "
                    f"{'FAIL' if self.failed() else 'OK'}")


# ── run context and pre-flight ──

@dataclass
class DemoRun:
    run: str
    langs: list[str]
    namespace: str
    window_start: int
    window_end: int
    now: int
    exemplar_storage: bool | None = None
    flags_error: str = ""
    prom_start: float | None = None
    # (lang, app) -> {(job, instance)} from target_info{otel_libs_run=<run>}.
    instances: dict[tuple[str, str], set[tuple[str, str]]] = field(default_factory=dict)
    target_error: str = ""
    # lang -> [(pod name, status.startTime)] of the pods labelled otel-libs/run=<run>;
    # the earliest startTime starts the g scan (DE-D11 g).
    pods: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    pods_error: str = ""
    # Latest start of each pod (startTime or a container's running.startedAt): the
    # youngest one gates the window (startup race, DE-D11 r3.6).
    latest_starts: list[int] = field(default_factory=list)

    @property
    def window(self) -> int:
        return self.window_end - self.window_start


def _ts(text: str) -> int | None:
    """RFC 3339 timestamp (Kubernetes, Prometheus) to Unix seconds."""
    from datetime import datetime
    if not text:
        return None
    # Python 3.10's fromisoformat needs "+00:00" and 3 or 6 fraction digits.
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text.replace("Z", "+00:00"))
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def demo_preflight(b: Backends, context: str, run: DemoRun, rep: DemoReport,
                   kubectl=None) -> None:
    """Read-only pre-flight (DE-D10): flags, start time, exemplar buffer, target_info, pods."""
    kubectl = kubectl or kubectl_json
    try:
        flags = b.prom_status("flags")
        features = str(flags.get("enable-feature", ""))
        run.exemplar_storage = "exemplar-storage" in [f.strip() for f in features.split(",")]
        rep.info(f"preflight prometheus enable-feature={features or '(none)'}")
        if not run.exemplar_storage:
            rep.blocked("preflight: exemplar-storage absent from the Prometheus flags; check b "
                        "cannot run (DE-D10 reversal: ask the platform owner)")
    except QueryError as exc:
        run.flags_error = str(exc)
        rep.blocked(f"preflight: Prometheus flags unreadable: {exc}")
    try:
        start = _ts(str(b.prom_status("runtimeinfo").get("startTime", "")))
        run.prom_start = start
        if start is not None and start > run.window_start:
            rep.info(f"preflight Prometheus started at {start} inside the window "
                     f"[{run.window_start}, {run.window_end}]: exemplars before it were lost "
                     "(in-memory storage)")
        else:
            rep.info(f"preflight Prometheus start time {start} (before the window)")
    except QueryError as exc:
        rep.info(f"preflight Prometheus runtimeinfo unreadable: {exc}")
    try:
        vec = b.prom_query("max(prometheus_tsdb_exemplar_exemplars_in_storage)", run.now)
        stored = int(float(vec[0]["value"][1])) if vec else None
        rep.info(f"preflight exemplars in storage {stored} of maxSize 100000"
                 + (" (full: old exemplars are evicted)" if stored and stored >= 100000 else ""))
    except QueryError as exc:
        rep.info(f"preflight exemplar storage gauge unreadable: {exc}")
    try:
        promql = f"last_over_time(target_info{{otel_libs_run={_quote(run.run)}}}[{run.window}s])"
        for series in b.prom_query(promql, run.window_end):
            m = series.get("metric") or {}
            name = m.get("service_name", "")
            for lang in run.langs:
                for app in DEMO_APPS:
                    if name == svc(lang, app):
                        run.instances.setdefault((lang, app), set()).add(
                            (m.get("job", ""), m.get("instance", "")))
        found = sorted(f"{lang}/{app}={len(v)}" for (lang, app), v in run.instances.items())
        rep.info(f"preflight target_info{{otel_libs_run={run.run!r}}} instances: "
                 + (" ".join(found) or "none"))
    except QueryError as exc:
        run.target_error = str(exc)
        rep.info(f"preflight target_info unreadable: {exc}")
    try:
        items = kubectl(context, ["pods", "-n", run.namespace, "-l", f"otel-libs/run={run.run}"])
        for pod in items.get("items") or []:
            name = (pod.get("metadata") or {}).get("name", "")
            m = POD_RE.match(name)
            if not m:
                continue
            first, latest = pod_times(pod)
            if latest is not None:
                run.latest_starts.append(latest)
            run.pods.setdefault(m.group(1), []).append((name, first or run.window_start))
        rep.info("preflight pods labelled otel-libs/run=" + run.run + ": "
                 + (" ".join(f"{lang}={len(p)}" for lang, p in sorted(run.pods.items())) or "none"))
    except QueryError as exc:
        run.pods_error = str(exc)
        rep.info(f"preflight pods unreadable: {exc}")
        return
    if run.latest_starts and run.window_start < max(run.latest_starts) + STARTUP_GRACE:
        rep.blocked(f"preflight: the window starts at {run.window_start}, less than {STARTUP_GRACE}s "
                    f"after the youngest app pod start {max(run.latest_starts)} (startup race, "
                    "DE-D11 r3.6); wait or shorten --window")


def pod_times(pod: dict) -> tuple[int | None, int | None]:
    """(status.startTime, latest of startTime and the containers' running.startedAt)."""
    status = pod.get("status") or {}
    first = _ts(status.get("startTime", ""))
    times = [first]
    for c in status.get("containerStatuses") or []:
        times.append(_ts(((c.get("state") or {}).get("running") or {}).get("startedAt", "")))
    known = [t for t in times if t is not None]
    return first, (max(known) if known else None)


def pod_start(pod: dict) -> int | None:
    """Latest start of a pod (startTime or a container restart): the grace check's input."""
    return pod_times(pod)[1]


def _instance_matcher(pairs: set[tuple[str, str]]) -> str:
    jobs = sorted({j for j, _ in pairs})
    insts = sorted({i for _, i in pairs})
    job = f"job={_quote(jobs[0])}" if len(jobs) == 1 else f"job=~{_quote('|'.join(map(re.escape, jobs)))}"
    return f"{job},instance=~{_quote('|'.join(map(re.escape, insts)))}"


def _result(detail: str, problems: list[str]) -> Obs:
    """A pass, or a fail whose problems are kept for the xfail-reason match."""
    if not problems:
        return Obs(OBS_PASS, detail)
    return Obs(OBS_FAIL, detail + "; " + "; ".join(problems), problems=list(problems))


def under_demo(span: Span, spans: list[Span]) -> bool:
    """True for a demo span or a span below a demo span of the same service (e.g. the .NET
    helper's DB record span under demo.order.process); framework spans of the next service
    down (the api's SERVER span under the worker's demo.drive) do not count."""
    if span.scope == DEMO_SCOPE:
        return True
    by_id = {s.span_id: s for s in spans}
    return any(s.scope == DEMO_SCOPE and s.service == span.service and descends(span, s, by_id)
               for s in spans)


# ── check a: logs -> traces (DR5) ──

def check_a(b: Backends, run: DemoRun, lang: str, cache: TraceCache, max_traces: int = 200) -> Obs:
    sel = (f'{{service_name=~{_quote(f"demo-{lang}-(worker|api|backend)")}, service_instance_id!=""}}'
           f' | otel_libs_run={_quote(run.run)} | trace_id!=""')
    try:
        vec = b.loki_query(f"sum by (service_name)(count_over_time({sel} [{run.window}s]))",
                           run.window_end)
    except QueryError as exc:
        return Obs(OBS_ERROR, f"query error: {exc}")
    counts = {(v.get("metric") or {}).get("service_name", ""): float(v["value"][1]) for v in vec}
    total = sum(counts.values())
    if total == 0:
        return Obs(OBS_NODATA, f"no OTLP record with a trace_id for run {run.run} in the window")
    minimum = LOG_RECORDS_PER_SECOND * run.window / 2
    per_svc = " ".join(f"{app}={counts.get(svc(lang, app), 0):.0f}" for app in DEMO_APPS)
    problems: list[str] = []
    # Sample (trace id, span id) pairs across the whole window by time slices.
    records: dict[tuple[str, str], str] = {}  # (trace id, span id) -> service
    unsampled = invalid = 0
    slices = 10
    per_slice = max(1, max_traces // slices)
    step = max(1, run.window // slices)
    try:
        for i in range(slices):
            lo = run.window_start + i * step
            hi = run.window_end if i == slices - 1 else lo + step
            for labels, _line in b.loki_lines(sel, lo, hi, limit=per_slice * 4):
                if not sampled(labels):
                    unsampled += 1
                    continue
                tid = norm_trace(labels.get("trace_id", ""))
                if not tid:
                    invalid += 1
                    continue
                key = (tid, norm_span(labels.get("span_id", "")))
                if key not in records and len(records) < max_traces:
                    records[key] = labels.get("service_name", "")
    except QueryError as exc:
        return Obs(OBS_ERROR, f"query error: {exc}")
    no_span = [k for k in records if not k[1]]
    try:
        resolved, misses, retried = cache.resolve([k for k in records if k[1]])
    except QueryError as exc:
        return Obs(OBS_ERROR, f"query error: {exc}")
    # r3.5: only records on a demo span (or below one) count; framework records are ignored.
    demo: dict[str, int] = {}
    for (tid, sid), service in records.items():
        if not sid or (tid, sid) in misses:
            continue
        spans = cache.get(tid) or []
        span = next((s for s in spans if s.span_id == sid), None)
        if span is not None and under_demo(span, spans):
            demo[service] = demo.get(service, 0) + 1
    n_demo = sum(demo.values())
    share = n_demo / resolved if resolved else 0.0
    estimated = total * share
    if invalid:
        problems.append(f"{invalid} sampled records carry an invalid trace_id")
    if no_span:
        problems.append(f"{len(no_span)} sampled records carry a trace_id but no span_id")
    if misses:
        problems.append(f"{len(misses)} of {len(records)} sampled records do not resolve "
                        f"(trace or span_id missing in Tempo after one retry)")
    if not records:
        problems.append("no sampled record to resolve")
    for app in DEMO_APPS:
        if not demo.get(svc(lang, app)):
            problems.append(f"{app}: no sampled record on a demo span")
    if estimated < minimum:
        problems.append(f"records on demo spans ≈ {estimated:.0f} (total {total:.0f} x share "
                        f"{share:.2f}) < minimum {minimum:.0f} (half the cycle's records)")
    detail = (f"records {per_svc} total {total:.0f}; sampled {len(records)} (trace, span) "
              f"({unsampled} unsampled skipped), {resolved} resolve, {n_demo} on or below a demo "
              f"span (≈ {estimated:.0f}, min {minimum:.0f}), retried {retried}")
    return _result(detail, problems)


# ── check b: exemplars (DR6, DE-D10) ──

def check_b(b: Backends, run: DemoRun, lang: str, cell: str, cache: TraceCache) -> Obs:
    if run.flags_error:
        return Obs(OBS_BLOCKED, f"Prometheus flags unreadable: {run.flags_error}")
    if run.exemplar_storage is False:
        return Obs(OBS_BLOCKED, "exemplar-storage absent from the Prometheus flags")
    if run.target_error:
        return Obs(OBS_ERROR, f"target_info unreadable: {run.target_error}")
    app, metric, extra = EXEMPLAR_OVERRIDES.get((lang, cell), EXEMPLAR_CELLS[cell])
    pairs = run.instances.get((lang, app), set())
    if not pairs:
        return Obs(OBS_NODATA, f"no target_info instance of {svc(lang, app)} for run {run.run}",
                   series=False)
    matchers = _instance_matcher(pairs) + (f",{extra}" if extra else "")
    try:
        data = b.prom_exemplars(f"{metric}{{{matchers}}}", run.window_start, run.window_end)
    except QueryError as exc:
        return Obs(OBS_ERROR, f"query error: {exc}")
    insts = {i for _, i in pairs}
    by_trace: dict[str, set[str]] = {}
    count = no_span = invalid = 0
    for series in data:
        if (series.get("seriesLabels") or {}).get("instance") not in insts:
            continue
        for ex in series.get("exemplars") or []:
            labels = ex.get("labels") or {}
            raw = labels.get("trace_id", "") or labels.get("traceID", "")
            if not raw:
                continue
            count += 1
            tid = norm_trace(raw)
            sid = norm_span(labels.get("span_id", "") or labels.get("spanID", ""))
            if not tid:
                invalid += 1
            elif not sid:
                no_span += 1
            else:
                by_trace.setdefault(tid, set()).add(sid)
    if count == 0:
        try:
            exists = bool(b.prom_series(f"{metric}{{{matchers}}}", run.window_start, run.window_end))
        except QueryError as exc:
            return Obs(OBS_ERROR, f"query error: {exc}")
        return Obs(OBS_NODATA, f"0 exemplars on {metric} of {svc(lang, app)} in the window "
                               f"(series {'present' if exists else 'absent'})", series=exists)
    tried = sorted(by_trace)[:MAX_EXEMPLAR_TRACES]
    pairs_to_check = [(t, s) for t in tried for s in sorted(by_trace[t])]
    try:
        _, misses, retried = cache.resolve(pairs_to_check)
    except QueryError as exc:
        return Obs(OBS_ERROR, f"query error: {exc}")
    note = (" (exemplar span may be the parent of the DB span until A43 is decided)"
            if cell == "b.db_client" else "")
    problems = []
    if no_span:
        problems.append(f"{no_span} exemplars without span_id")
    if invalid:
        problems.append(f"{invalid} exemplars with an invalid trace_id")
    if misses:
        problems.append(f"{len(misses)} (trace, span) pairs do not resolve after one retry")
    detail = (f"{count} exemplars on {metric}{note}; {len(tried)} trace ids tried, "
              f"{len(pairs_to_check) - len(misses)}/{len(pairs_to_check)} (trace, span) resolve, "
              f"retried {retried}")
    obs = _result(detail, problems)
    obs.series = True
    return obs


# ── check c: custom metrics (DR7) ──

def series_increase(values: list, window_start: int) -> float:
    """Counter increase over query_range points, resets handled; a series born inside
    the window counts its first sample (``increase()`` would miss it, DE-D11 c)."""
    pts = [(float(t), float(v)) for t, v in values]
    if not pts:
        return 0.0
    inc = pts[0][1] if pts[0][0] > window_start else 0.0
    prev = pts[0][1]
    for _, v in pts[1:]:
        inc += v - prev if v >= prev else v
        prev = v
    return inc


def check_c(b: Backends, run: DemoRun, lang: str) -> Obs:
    if run.target_error:
        return Obs(OBS_ERROR, f"target_info unreadable: {run.target_error}")
    pairs = run.instances.get((lang, "backend"), set())
    if not pairs:
        return Obs(OBS_NODATA, f"no target_info instance of {svc(lang, 'backend')} for run {run.run}")
    matchers = _instance_matcher(pairs)
    problems: list[str] = []
    increases: dict[str, dict[str, float]] = {}
    try:
        for metric in ("demo_orders_processed_total", "demo_order_duration_seconds_count"):
            per = increases.setdefault(metric, {})
            for series in b.prom_query_range(f"{metric}{{{matchers}}}", run.window_start,
                                             run.window_end, STEP):
                scen = (series.get("metric") or {}).get("demo_scenario", "")
                per[scen] = per.get(scen, 0.0) + series_increase(series.get("values") or [], run.window_start)
        names = b.prom_series(f'{{__name__=~"demo_orders_processed.*|demo_order_duration.*",{matchers}}}',
                              run.window_start, run.window_end)
    except QueryError as exc:
        return Obs(OBS_ERROR, f"query error: {exc}")
    if not names and not any(increases.values()):
        return Obs(OBS_NODATA, f"no demo metric series of {svc(lang, 'backend')} for run {run.run}")
    unhandled = [m for m, per in increases.items() if "unhandled" in per]
    unhandled += sorted({s["__name__"] for s in names if s.get("demo_scenario") == "unhandled"} - set(unhandled))
    if unhandled:
        return Obs(OBS_UNEXPECTED, f"backend series with demo_scenario=\"unhandled\": {sorted(set(unhandled))}")
    for metric, per in increases.items():
        for scen in BACKEND_SCENARIOS:
            if per.get(scen, 0.0) <= 0:
                problems.append(f"{metric}{{demo_scenario={scen}}} increase 0")
    counter = {s["__name__"] for s in names if s.get("__name__", "").startswith("demo_orders_processed")}
    hist = [s for s in names if s.get("__name__", "").startswith("demo_order_duration")]
    if counter != {"demo_orders_processed_total"}:
        problems.append(f"counter series {sorted(counter)} (want exactly demo_orders_processed_total)")
    hist_names = {s["__name__"] for s in hist}
    want = {f"demo_order_duration_seconds_{x}" for x in ("bucket", "sum", "count")}
    if not want <= hist_names:
        problems.append(f"histogram series {sorted(hist_names)} (want {sorted(want)})")
    les = {s.get("le", "") for s in hist if s["__name__"].endswith("_bucket")}
    if "+Inf" not in les:
        problems.append('no le="+Inf" bucket')
    try:
        bounds = sorted(float(le) for le in les if le and le != "+Inf")
    except ValueError:
        bounds = []
        problems.append(f"non-numeric le values {sorted(les)}")
    if bounds and bounds != sorted(DURATION_BOUNDS):
        problems.append(f"le bounds {bounds} != DE-D9 {list(DURATION_BOUNDS)}")
    for s in names:
        if s.get("__name__", "").startswith(("demo_orders_processed", "demo_order_duration")):
            if not {"demo_scenario", "demo_outcome"} <= set(s):
                problems.append(f"{s['__name__']} without demo_scenario/demo_outcome")
                break
            if ("error_type" in s) != (s.get("demo_outcome") == "error"):
                problems.append(f"{s['__name__']}{{demo_outcome={s.get('demo_outcome')}}} "
                                f"error_type {'present' if 'error_type' in s else 'absent'}")
                break
    # The metric error_type equals the owning span's error.type (DE-D9, DE-D8 pinned literals).
    wrong = sorted({(s.get("__name__", ""), s.get("demo_scenario", ""), s.get("error_type", ""))
                    for s in names
                    if s.get("__name__", "").startswith(("demo_orders_processed", "demo_order_duration"))
                    and s.get("demo_scenario") in PINNED_TYPES and s.get("demo_outcome") == "error"
                    and s.get("error_type") != PINNED_TYPES[s["demo_scenario"]]})
    for metric, scen, value in wrong[:3]:
        problems.append(f"{metric}{{demo_scenario={scen}}} error_type {value!r} "
                        f"(want {PINNED_TYPES[scen]})")
    shown = " ".join(f"{scen}={increases['demo_orders_processed_total'].get(scen, 0):.0f}"
                     for scen in BACKEND_SCENARIOS)
    return _result(f"processed increase {shown}; {len(names)} series", problems)


# ── check d: error routes per span set (DR3, DE-D11 d) ──

def span_set_d(span: Span, lang: str) -> str | None:
    """The d cell a span belongs to, or None (spans outside every d set)."""
    if span.scope == DEMO_SCOPE:
        return "d.demo"
    if "OrderService/PlaceOrder" in span.name and span.kind in ("client", "server") \
            and span.service in (svc(lang, "api"), svc(lang, "backend")):
        return "d.grpc"
    if span.service == svc(lang, "api") and span.kind in ("server", "internal"):
        return "d.http"
    if span.service == svc(lang, "worker") and span.kind == "client" and "http.request.method" in span.attrs:
        return "d.http"
    if span.is_postgres():
        return "d.db"
    return None


def _loki_at_span(b: Backends, run: DemoRun, service: str, trace_id: str, span_id: str) -> list[dict]:
    q = (f'{{service_name={_quote(service)}, service_instance_id!=""}}'
         f" | trace_id={_quote(norm_trace(trace_id))} | span_id={_quote(norm_span(span_id))}")
    return [labels for labels, _ in b.loki_lines(q, run.window_start - 300, run.now, limit=50)]


def _loki_in_trace(b: Backends, run: DemoRun, lang: str, trace_id: str) -> list[dict]:
    """Every record of the language's demo services (worker, api, backend) in one trace."""
    q = (f'{{service_name=~{_quote(f"demo-{lang}-(worker|api|backend)")}, service_instance_id!=""}}'
         f" | trace_id={_quote(norm_trace(trace_id))}")
    return [labels for labels, _ in b.loki_lines(q, run.window_start - 300, run.now, limit=200)]


def _demo_span(spans: list[Span], lang: str, app: str, name: str) -> Span | None:
    return next((s for s in spans if s.service == svc(lang, app) and s.name == name
                 and s.scope == DEMO_SCOPE), None)


def _d_demo(b: Backends, run: DemoRun, lang: str, scen: str, spans: list[Span]) -> list[str]:
    """Owning span, propagating demo spans and the trace's ERROR-record budget (DE-D8, A45).

    One failure gives exactly one ERROR record in the trace, and it sits on the owning span.
    Counted: records on a demo span or below one in the same service, and records whose
    span id is missing or not in the trace. Not counted: the helper record of a failed
    PostgreSQL span (d.db's) and records on framework spans outside every demo span (the
    .NET Kestrel record on the api SERVER span for ``unhandled``: A37 debt, design r3.5
    "Framework logs on the unhandled path"). Propagating demo
    spans (api ``demo.order.accept`` on 422/409, worker ``demo.drive``) carry status ERROR
    without description, ``error.type`` = the HTTP status code, no event, no record.
    """
    app, name = OWNER[scen]
    s = _demo_span(spans, lang, app, name)
    if s is None:
        return [f"{scen}: owning span {svc(lang, app)} {name} missing"]
    out = []
    if s.status != "error":
        out.append(f"{scen}: {name} status {s.status}")
    if s.message:
        out.append(f"{scen}: {name} has a status description (want empty)")
    if s.attrs.get("error.type") != PINNED_TYPES[scen]:
        out.append(f"{scen}: {name} error.type {s.attrs.get('error.type')!r}")
    # A45 (contract design D15, DE-D8 note of 2026-11-17): no exception event on the
    # owning span in any language; the detail is in the one correlated record.
    excs = [a for n, a in s.events if n == "exception"]
    if excs:
        out.append(f"{scen}: {name} exception events {[sorted(a) for a in excs]} (want none)")
    by_id = {norm_span(x.span_id): x for x in spans}

    def counted(r: dict) -> bool:
        sid = norm_span(r.get("span_id", ""))
        at = by_id.get(sid)
        if at is None:
            return True
        return not at.is_postgres() and under_demo(at, spans)

    records = [r for r in _loki_in_trace(b, run, lang, s.trace_id)
               if severity_number(r) >= 17 and counted(r)]
    at_owner = [r for r in records if norm_span(r.get("span_id", "")) == norm_span(s.span_id)]
    elsewhere = sorted({f"{r.get('service_name', '?')}/{norm_span(r.get('span_id', '')) or 'no span_id'}"
                        for r in records if r not in at_owner})
    if len(records) != 1 or len(at_owner) != 1:
        out.append(f"{scen}: {len(records)} ERROR records on or below demo spans, "
                   f"{len(at_owner)} at the owning span (want 1, at the owning span)"
                   + (f"; others at {elsewhere}" if elsewhere else ""))
    if at_owner:
        r = at_owner[0]
        if not r.get("exception_stacktrace") or not r.get("exception_type"):
            out.append(f"{scen}: ERROR record without exception_stacktrace/exception_type")
        elif r.get("exception_type") != PINNED_TYPES[scen]:
            out.append(f"{scen}: {lang} record exception_type {r.get('exception_type')!r}")
    propagating = [("worker", "demo.drive")]
    if app != "api":
        propagating.append(("api", "demo.order.accept"))
    for papp, pname in propagating:
        p = _demo_span(spans, lang, papp, pname)
        if p is None:
            out.append(f"{scen}: propagating span {svc(lang, papp)} {pname} missing")
            continue
        if p.status != "error" or p.message:
            out.append(f"{scen}: {pname} status {p.status} description "
                       f"{'set' if p.message else 'empty'} (want error, empty)")
        if p.attrs.get("error.type") != HTTP_CODES[scen]:
            out.append(f"{scen}: {pname} error.type {p.attrs.get('error.type')!r} (want {HTTP_CODES[scen]})")
        if any(n == "exception" for n, _ in p.events):
            out.append(f"{scen}: {pname} has an exception event (want none)")
        n = sum(1 for r in records if norm_span(r.get("span_id", "")) == norm_span(p.span_id))
        if n:
            out.append(f"{scen}: {n} ERROR records at the propagating span {pname} (want 0)")
    return out


def _d_grpc(lang: str, scen: str, spans: list[Span]) -> list[str]:
    client = [s for s in spans if s.service == svc(lang, "api") and s.kind == "client"
              and "OrderService/PlaceOrder" in s.name]
    server = [s for s in spans if s.service == svc(lang, "backend") and s.kind == "server"
              and "OrderService/PlaceOrder" in s.name]
    out = []
    if not client:
        out.append(f"{scen}: gRPC client span missing")
    for s in client:
        if s.status != "error":
            out.append(f"{scen}: client status {s.status}")
        if s.message not in ("", GRPC_MESSAGES[scen]):
            out.append(f"{scen}: client description is not the constant gRPC message")
    if not server:
        out.append(f"{scen}: gRPC server span missing")
    for s in server:
        if s.status == "error":
            out.append(f"{scen}: server status error (want unset)")
    for s in client + server:
        keys = s.detail_keys()
        if keys:
            out.append(f"{scen}: {s.kind} span carries {sorted(set(keys))}")
    return out


def _d_http(lang: str, scen: str, spans: list[Span]) -> list[str]:
    by_id = {s.span_id: s for s in spans}
    servers = [s for s in spans if s.service == svc(lang, "api") and s.kind == "server"]
    out = []
    if not servers:
        return [f"{scen}: api SERVER span missing"]
    server = servers[0]
    framework = [s for s in spans if s.service == svc(lang, "api") and s.kind == "internal"
                 and s.scope != DEMO_SCOPE and descends(s, server, by_id)]
    if scen == "unhandled":
        if server.status != "error":
            out.append(f"{scen}: api SERVER status {server.status}")
        if server.attrs.get("error.type") != HTTP_CODES[scen]:
            out.append(f"{scen}: api SERVER error.type {server.attrs.get('error.type')!r} (want 500)")
    else:
        if server.status == "error":
            out.append(f"{scen}: api SERVER status error (want unset on 4xx)")
        clients = [s for s in spans if s.service == svc(lang, "worker") and s.kind == "client"
                   and "http.request.method" in s.attrs]
        if not clients:
            out.append(f"{scen}: worker HTTP CLIENT span missing")
        for s in clients:
            if s.status != "error" or s.attrs.get("error.type") != HTTP_CODES[scen]:
                out.append(f"{scen}: worker HTTP CLIENT status {s.status} error.type "
                           f"{s.attrs.get('error.type')!r} (want error {HTTP_CODES[scen]})")
    for s in [server, *framework]:
        keys = [k for k in s.detail_keys() if "grpc" not in k]
        if keys:
            out.append(f"{scen}: {s.kind} span {s.name!r} carries {sorted(set(keys))}")
    return out


def _d_db(b: Backends, run: DemoRun, lang: str, spans: list[Span]) -> list[str]:
    errs = [s for s in spans if s.is_postgres() and s.service == svc(lang, "backend") and s.status == "error"]
    if not errs:
        return ["db_error: no PostgreSQL CLIENT span with status error"]
    out = []
    for s in errs:
        if s.message:
            out.append(f"db_error: {s.name!r} has a status description (contract A25: empty)")
        if s.attrs.get("db.response.status_code") != "23505":
            out.append(f"db_error: {s.name!r} db.response.status_code "
                       f"{s.attrs.get('db.response.status_code')!r}")
        excs = [a for n, a in s.events if n == "exception"]
        if len(excs) != 1 or set(excs[0]) != {"exception.type"}:
            out.append(f"db_error: {s.name!r} exception events {[sorted(a) for a in excs]}")
        records = [r for r in _loki_at_span(b, run, svc(lang, "backend"), s.trace_id, s.span_id)
                   if severity_number(r) >= 17]
        if not records:
            out.append(f"db_error: no correlated ERROR record at {s.name!r} (ADR-014)")
    return out


def check_d(b: Backends, run: DemoRun, lang: str, cache: TraceCache, rep: DemoReport) -> dict[str, Obs]:
    cells = ("d.demo", "d.grpc", "d.http", "d.db")
    problems: dict[str, list[str]] = {c: [] for c in cells}
    inspected: dict[str, int] = {}
    tokens = run_tokens(run.run)
    for scen in ERROR_SCENARIOS:
        q = (f"{{resource.service.name={_quote(svc(lang, 'worker'))} && "
             f"resource.otel_libs.run={_quote(run.run)} && span.demo.scenario={_quote(scen)}}}")
        try:
            found = b.tempo_search(q, run.window_start, run.window_end, limit=20)
            traces = []
            for t in found[:MAX_D_TRACES]:
                spans = cache.get(t.get("traceID", ""))
                if spans:
                    traces.append(spans)
        except QueryError as exc:
            return {c: Obs(OBS_ERROR, f"query error: {exc}") for c in cells}
        inspected[scen] = len(traces)
        for spans in traces:
            try:
                problems["d.demo"] += _d_demo(b, run, lang, scen, spans)
                if scen in GRPC_MESSAGES:
                    problems["d.grpc"] += _d_grpc(lang, scen, spans)
                problems["d.http"] += _d_http(lang, scen, spans)
                if scen == "db_error":
                    problems["d.db"] += _d_db(b, run, lang, spans)
            except QueryError as exc:
                return {c: Obs(OBS_ERROR, f"query error: {exc}") for c in cells}
            for s in spans:
                target = span_set_d(s, lang)
                if target is None:
                    continue
                hits = sorted({label for label, value in s.values() if any(t in value for t in tokens)})
                if hits:  # labels only: never the span name or value (DE-D11 g, DR12)
                    problems[target].append(f"{scen}: run token on {s.kind} span {s.span_id} in {hits}")
    try:
        q = (f"{{resource.service.name={_quote(svc(lang, 'worker'))} && "
             f"resource.otel_libs.run={_quote(run.run)} && kind=client && "
             f"span.http.request.method != nil && span.error.type != nil && "
             f"span.error.type !~ {_quote(CONNECTION_ERROR_RE)}}}")
        conn = b.tempo_search(q, run.window_start, run.window_end, limit=1000)
        n = sum(((t.get("spanSet") or {}).get("matched") or 1) for t in conn)
        rep.info(f"{lang} d: {n} worker -> api HTTP CLIENT span(s) with a connection-error "
                 "error.type (never FAIL; analysed in DE6, DE-D11 r3.3)")
    except QueryError as exc:
        rep.info(f"{lang} d: connection-error count unreadable: {exc}")
    scen_of = {"d.demo": ERROR_SCENARIOS, "d.grpc": tuple(GRPC_MESSAGES),
               "d.http": ERROR_SCENARIOS, "d.db": ("db_error",)}
    out = {}
    for cell in cells:
        missing = [s for s in scen_of[cell] if not inspected.get(s)]
        seen = " ".join(f"{s}={inspected.get(s, 0)}" for s in scen_of[cell])
        if missing:
            out[cell] = Obs(OBS_NODATA, f"traces {seen}; no trace for {missing}")
            continue
        uniq = list(dict.fromkeys(problems[cell]))
        detail = f"traces {seen}" + ("; " + "; ".join(uniq[:6]) + (f" (+{len(uniq) - 6})" if len(uniq) > 6 else "")
                                     if uniq else "")
        out[cell] = Obs(OBS_FAIL if uniq else OBS_PASS, detail, problems=uniq)
    return out


# ── check e: trace structure and service graph (DR8) ──

def match_ok_trace(spans: list[Span], lang: str) -> list[str]:
    """Items of design "Expected trace" missing from one `ok` trace (empty = match)."""
    by_id = {s.span_id: s for s in spans}
    found: dict[int, Span] = {}

    def pick(n: int, parent: int | None, pred, direct: bool = False) -> None:
        anc = found.get(parent) if parent is not None else None
        if parent is not None and anc is None:
            return
        for s in spans:
            if not pred(s):
                continue
            if anc is not None and not (s.parent_id == anc.span_id if direct else descends(s, anc, by_id)):
                continue
            found[n] = s
            return

    redis = lambda s, names: (s.kind == "client" and s.attrs.get("db.system.name") == "redis"  # noqa: E731
                              and s.name.upper() in names)
    dest = f"demo:{lang}:orders"
    pick(1, None, lambda s: s.service == svc(lang, "worker") and s.kind == "internal"
         and s.name == "demo.drive" and not s.parent_id and s.scope == DEMO_SCOPE
         and s.attrs.get("demo.scenario") == "ok")
    pick(2, 1, lambda s: s.service == svc(lang, "worker") and s.kind == "client"
         and re.fullmatch(r"(HTTP )?POST", s.name) is not None and "http.request.method" in s.attrs)
    pick(3, 2, lambda s: s.service == svc(lang, "api") and s.kind == "server"
         and s.name == "POST /orders" and s.attrs.get("http.route") == "/orders")
    pick(4, 3, lambda s: s.service == svc(lang, "api") and s.kind == "internal"
         and s.name == "demo.order.accept" and s.scope == DEMO_SCOPE)
    pick(5, 4, lambda s: s.service == svc(lang, "api") and redis(s, ("GET", "SET", "SETEX")))
    pick(6, 4, lambda s: s.service == svc(lang, "api") and s.kind == "client"
         and "OrderService/PlaceOrder" in s.name)
    pick(7, 6, lambda s: s.service == svc(lang, "backend") and s.kind == "server"
         and "OrderService/PlaceOrder" in s.name)
    pick(8, 7, lambda s: s.service == svc(lang, "backend") and s.kind == "internal"
         and s.name == "demo.order.process" and s.scope == DEMO_SCOPE
         and "demo.scenario" in s.attrs and "order.id" in s.attrs)
    pick(9, 8, lambda s: s.service == svc(lang, "backend") and s.is_postgres())
    pick(10, 4, lambda s: s.service == svc(lang, "api") and s.kind == "producer"
         and s.name == f"send {dest}" and s.attrs.get("messaging.system") == "redis"
         and s.attrs.get("messaging.operation.type") == "send"
         and s.attrs.get("messaging.destination.name") == dest)
    pick(11, 10, lambda s: s.service == svc(lang, "api") and redis(s, ("XADD",)))
    prod = found.get(10)
    if prod is not None:
        pick(12, 10, lambda s: s.service == svc(lang, "worker") and s.kind == "consumer"
             and s.name == f"process {dest}" and s.attrs.get("messaging.operation.type") == "process"
             and s.attrs.get("messaging.consumer.group.name") == "demo-workers"
             and (prod.trace_id, prod.span_id) in s.links, direct=True)
    pick(13, 12, lambda s: s.service == svc(lang, "worker") and redis(s, ("XACK",)))
    names = {1: "demo.drive root", 2: "worker HTTP CLIENT", 3: "api SERVER POST /orders",
             4: "demo.order.accept", 5: "api Redis GET/SET", 6: "api gRPC CLIENT",
             7: "backend gRPC SERVER", 8: "demo.order.process", 9: "backend PostgreSQL CLIENT",
             10: "PRODUCER send", 11: "Redis XADD", 12: "CONSUMER (parent = link = PRODUCER)",
             13: "Redis XACK"}
    return [f"{n} {names[n]}" for n in names if n not in found]


def check_e_trace(b: Backends, run: DemoRun, lang: str, cache: TraceCache) -> Obs:
    q = (f"{{resource.service.name={_quote(svc(lang, 'worker'))} && "
         f'resource.otel_libs.run={_quote(run.run)} && name="demo.drive" && span.demo.scenario="ok"}}')
    try:
        found = b.tempo_search(q, run.window_start, run.window_end, limit=20)
    except QueryError as exc:
        return Obs(OBS_ERROR, f"query error: {exc}")
    found = [t for t in found if int(t.get("startTimeUnixNano") or 0) < run.window_end * 10**9]
    if not found:
        return Obs(OBS_NODATA, "no ok trace started before the window end")
    best: list[str] | None = None
    for t in found[:MAX_E_TRACES]:
        try:
            spans = cache.get(t.get("traceID", ""))
        except QueryError as exc:
            return Obs(OBS_ERROR, f"query error: {exc}")
        if not spans:
            continue
        missing = match_ok_trace(spans, lang)
        if not missing:
            return Obs(OBS_PASS, f"ok trace {norm_trace(t['traceID'])} matches the 13 expected items")
        if best is None or len(missing) < len(best):
            best = missing
    return Obs(OBS_FAIL, f"none of {min(len(found), MAX_E_TRACES)} ok traces matches; closest misses "
                         f"{best if best is not None else 'every trace (not fetchable)'}",
               problems=[f"missing {m}" for m in best] if best else ["no ok trace fetchable"])


def check_e_edges(b: Backends, run: DemoRun, lang: str, rep: DemoReport) -> dict[str, Obs]:
    promql = (f"sum by (client, server, connection_type, failed)(increase("
              f'traces_service_graph_request_total{{client=~"demo-{lang}-(worker|api|backend)"}}'
              f"[{run.window}s]))")
    try:
        vec = b.prom_query(promql, run.window_end)
    except QueryError as exc:
        err = Obs(OBS_ERROR, f"query error: {exc}")
        return {"e.edges": err, "e.edge.db": err}
    edges: dict[tuple[str, str, str], dict[str, float]] = {}
    for r in vec:
        m = r.get("metric") or {}
        key = (m.get("client", ""), m.get("server", ""), m.get("connection_type", ""))
        edges.setdefault(key, {})[m.get("failed", "")] = float(r["value"][1])

    def total(client: str, server: str, ctype: str) -> tuple[float, float]:
        per = edges.get((client, server, ctype), {})
        return sum(per.values()), per.get("true", 0.0)

    problems = []
    parts = []
    for client, server in (("worker", "api"), ("api", "backend")):
        t, f = total(svc(lang, client), svc(lang, server), "")
        parts.append(f"{client}->{server} {t:.1f} (failed share {f / t:.2f})" if t else f"{client}->{server} 0")
        if t <= 0:
            problems.append(f"{client} -> {server} edge (no connection_type) has no requests")
    expected_ok = OK_ORDERS_PER_SECOND * run.window
    msg, _ = total(svc(lang, "api"), svc(lang, "worker"), "messaging_system")
    parts.append(f"api->worker messaging {msg:.1f} (min {0.8 * expected_ok:.1f})")
    if msg < 0.8 * expected_ok:
        problems.append(f"messaging edge {msg:.1f} < 0.8 x {expected_ok:.1f} expected ok orders")
    try:
        q = (f"{{resource.service.name={_quote(svc(lang, 'worker'))} && resource.otel_libs.run="
             f"{_quote(run.run)} && kind=consumer && link:spanID != nil}}")
        linked = len(b.tempo_search(q, run.window_start, run.window_end, limit=1000))
        ratio = f"{msg / linked:.2f}" if linked else "n/a"
        parts.append(f"TraceQL linked CONSUMER traces {linked} (messaging edge / linked = {ratio})")
        if linked == 0:
            problems.append("no CONSUMER span with a link in Tempo")
    except QueryError as exc:
        problems.append(f"link cross-check query error: {exc}")
    # Edge series carry no run label: they are scoped by service name only.
    parts.append("edges scoped by service name, not by run")
    edges_obs = _result("; ".join(parts), problems)
    backend = svc(lang, "backend")
    db = {k: v for k, v in edges.items() if k[0] == backend}
    good = [(k[1], sum(v.values())) for k, v in db.items() if k[2] == "database" and sum(v.values()) > 0]
    seen = ", ".join(f"{k[1]}[{k[2] or 'none'}]={sum(v.values()):.1f}" for k, v in sorted(db.items()))
    if good:
        db_obs = Obs(OBS_PASS, f"backend -> {good[0][0]} connection_type=database {good[0][1]:.1f}")
    elif db:
        db_obs = Obs(OBS_FAIL, f"no connection_type=database edge from the backend; edges {seen}",
                     problems=[f"no connection_type=database edge; edges {seen}"])
    else:
        db_obs = Obs(OBS_NODATA, "no service-graph edge from the backend")
    api = svc(lang, "api")
    redis = [f"{k[1]}[{k[2] or 'none'}]={sum(v.values()):.1f}" for k, v in sorted(edges.items())
             if k[0] == api and not k[1].startswith("demo-")]
    rep.info(f"{lang} e: api -> Redis node edge(s) recorded: {', '.join(redis) or 'none'}")
    return {"e.edges": edges_obs, "e.edge.db": db_obs}


def servicegraph_dropped(b: Backends, run: DemoRun, rep: DemoReport) -> None:
    try:
        vec = b.prom_query(f"sum(increase(otelcol_connector_servicegraph_dropped_spans_total[{run.window}s]))"
                           f" or sum(increase(otelcol_connector_servicegraph_dropped_spans[{run.window}s]))",
                           run.window_end)
        value = f"{float(vec[0]['value'][1]):.0f}" if vec else "unmeasured (no data from the gateway)"
        rep.info(f"e: service_graph connector dropped spans in the window {value} (reported, not gated)")
    except QueryError as exc:
        rep.info(f"e: service_graph dropped-span count unreadable: {exc}")


# ── check g: hygiene (DR12) ──

def tempo_page(b: Backends, q: str, start: int, end: int,
               limit: int = TEMPO_PAGE_LIMIT, min_slice: int = MIN_SLICE) -> tuple[list[dict], bool]:
    """Every trace of a search, splitting the interval while a slice hits ``limit``.

    Returns (traces, capped): capped is True when a slice of ``min_slice`` seconds still
    returned ``limit`` traces (no silent cap: the caller fails).
    """
    traces = b.tempo_search(q, start, end, limit)
    if len(traces) < limit:
        return traces, False
    if end - start <= min_slice:
        return traces, True
    mid = (start + end) // 2
    left, cl = tempo_page(b, q, start, mid, limit, min_slice)
    right, cr = tempo_page(b, q, mid, end, limit, min_slice)
    seen: dict[str, dict] = {}
    for t in left + right:
        seen.setdefault(norm_trace(t.get("traceID", "")), t)
    return list(seen.values()), cl or cr


def loki_page(b: Backends, q: str, start: int, end: int,
              limit: int = LOKI_PAGE_LIMIT, min_slice: int = MIN_SLICE) -> tuple[list[tuple[dict, str]], bool]:
    """Every record of a query as (labels, line), halving slices while one hits ``limit``.

    Both halves share ``mid`` (no gap between them); records are de-duplicated on
    (timestamp, line, labels).
    """
    entries, capped = _loki_page(b, q, start, end, limit, min_slice)
    seen: dict[tuple, tuple[dict, str]] = {}
    for ts, labels, line in entries:
        seen.setdefault((ts, line, tuple(sorted(labels.items()))), (labels, line))
    return list(seen.values()), capped


def _loki_page(b: Backends, q: str, start: int, end: int, limit: int,
               min_slice: int) -> tuple[list[tuple[str, dict, str]], bool]:
    entries = b.loki_entries(q, start, end, limit=limit)
    if len(entries) < limit:
        return entries, False
    if end - start <= min_slice:
        return entries, True
    mid = (start + end) // 2
    left, cl = _loki_page(b, q, start, mid, limit, min_slice)
    right, cr = _loki_page(b, q, mid, end, limit, min_slice)
    return left + right, cl or cr


def paging_crosscheck(b: Backends, base: str, listed: list[dict], start: int, end: int) -> tuple[str, str]:
    """(detail, problem) comparing the listed root traces of one aligned minute with a
    TraceQL ``count_over_time()`` of root spans there; a mismatch is a problem."""
    a = (end - 60) // 60 * 60
    if a < start:
        return "paging cross-check skipped (scan shorter than a minute)", ""
    try:
        series = b.tempo_metrics(f"{{{base} && nestedSetParent<0}} | count_over_time()", a, a + 60, 60)
    except QueryError as exc:
        return f"paging cross-check unmeasured ({exc})", ""
    samples = [smp for ser in series for smp in ser.get("samples") or []]
    at = [smp for smp in samples if int(float(smp.get("timestampMs", 0))) == a * 1000]
    counted = int(float((at or samples or [{"value": 0}])[0].get("value", 0)))
    roots = sum(1 for t in listed if a * 10**9 <= int(t.get("startTimeUnixNano") or 0) < (a + 60) * 10**9)
    detail = f"paging cross-check minute {a}: listed {roots}, count_over_time {counted}"
    return detail, ("" if roots == counted else f"Tempo paging lists {roots} root traces in minute {a} "
                                                f"but count_over_time counts {counted}")


def scan_secret(value: str) -> list[str]:
    return [name for name, rx in SECRET_RES if rx.search(value)]


def _is_poll_root(t: dict) -> bool:
    return "XREADGROUP" in str(t.get("rootTraceName", "")).upper()


def check_g(b: Backends, run: DemoRun, lang: str, cache: TraceCache,
            loki_hits: dict[str, list[str]]) -> dict[str, Obs]:
    cells = ("g.secret", "g.sentinel.db", "g.sentinel.app")
    pods = run.pods.get(lang, [])
    if run.pods_error:
        err = Obs(OBS_ERROR, f"pods unreadable: {run.pods_error}")
        return {c: err for c in cells}
    if not pods:
        nod = Obs(OBS_NODATA, f"no pod labelled otel-libs/run={run.run} for {lang}")
        return {c: nod for c in cells}
    start = min(t for _, t in pods)  # earliest status.startTime (M7)
    last_start = max(run.latest_starts or [t for _, t in pods])  # youngest start, restarts included
    base = f"resource.otel_libs.run={_quote(run.run)} && resource.service.name=~{_quote(f'demo-{lang}-.*')}"
    tokens = run_tokens(run.run)
    token_re = "|".join(t.split("-", 1)[0] for t in tokens)
    try:
        listed, capped = tempo_page(b, f"{{{base}}}", start, run.window_end)
        conn, ccap = tempo_page(b, f'{{{base} && (name=~".*connect.*" || name=~"(?i)(auth|hello|startup)")}}',
                                start, min(last_start + 300, run.window_end))
        sentinel_counts = {}
        extra: list[dict] = []
        for label, field_q in (("status", "statusMessage"), ("event", "event.exception.message"),
                               ("db.query.text", "span.db.query.text")):
            q = f'{{{base} && {field_q}=~{_quote(f".*({token_re})-{re.escape(run.run)}.*")}}}'
            found = b.tempo_search(q, start, run.window_end, limit=1000)
            sentinel_counts[label] = len(found)
            extra += found
    except QueryError as exc:
        err = Obs(OBS_ERROR, f"query error: {exc}")
        return {c: err for c in cells}
    cross_detail, cross_problem = paging_crosscheck(b, base, listed, start, run.window_end)
    polls = [t for t in listed if _is_poll_root(t)]
    others = [t for t in listed if not _is_poll_root(t)]
    stride = max(1, len(polls) // POLL_ROOT_SAMPLE)
    sample = polls[::stride][:POLL_ROOT_SAMPLE]
    ids = list(dict.fromkeys(norm_trace(t.get("traceID", "")) for t in others + sample + conn + extra))
    ids = [i for i in ids if i]
    hits = {c: [] for c in cells}
    counted = {c: 0 for c in cells}
    unresolved = 0
    for tid in ids:
        try:
            spans = cache.get(tid)
        except QueryError as exc:
            err = Obs(OBS_ERROR, f"query error: {exc}")
            return {c: err for c in cells}
        if spans is None:
            unresolved += 1
            continue
        for s in spans:
            if not s.service.startswith(f"demo-{lang}-"):
                continue
            target = "g.sentinel.db" if s.is_db_client() else "g.sentinel.app"
            counted["g.secret"] += 1
            counted[target] += 1
            for label, value in s.values():
                # Labels only: span kind, span id and field label, never the name or value.
                for kind in scan_secret(value):
                    hits["g.secret"].append(f"{kind} in {s.kind} span {s.span_id} {label}")
                if any(t in value for t in tokens):
                    hits[target].append(f"run token in {s.kind} span {s.span_id} {label}")
    secret_loki = loki_hits.get(lang, []) + loki_hits.get("*", [])
    hits["g.secret"] += secret_loki
    common = (f"{len(ids)} traces scanned ({len(sample)} of {len(polls)} poll roots sampled, "
              f"{len(conn)} connection-time roots), server-side token counts "
              + " ".join(f"{k}={v}" for k, v in sentinel_counts.items()) + f"; {cross_detail}")
    out = {}
    for c in cells:
        problems = []
        if capped or ccap:
            problems.append(f"a {MIN_SLICE}s slice still hit the {TEMPO_PAGE_LIMIT}-trace limit")
        if cross_problem:
            problems.append(cross_problem)
        if unresolved and c == "g.secret":
            problems.append(f"{unresolved} listed traces not fetchable")
        uniq = list(dict.fromkeys(hits[c]))
        detail = f"{counted[c]} spans, {len(hits[c])} hit(s); {common}"
        if c == "g.secret":
            detail += f"; Loki hits {len(secret_loki)}"
        if counted[c] == 0 and not problems and not uniq:
            out[c] = Obs(OBS_NODATA, detail)
            continue
        # Each distinct hit is its own problem, so the xfail reasons must explain every one.
        shown = problems + uniq[:5] + ([f"(+{len(uniq) - 5} more hits)"] if len(uniq) > 5 else [])
        obs = _result(detail, problems + uniq)
        if obs.state == OBS_FAIL:
            obs.detail = detail + "; " + "; ".join(shown)
        out[c] = obs
    return out


def loki_hygiene(b: Backends, run: DemoRun) -> tuple[dict[str, list[str]], str]:
    """48-hex / password= / userinfo hits over the run pods' and backing pods' Loki records,
    scanned client-side over the line and every label / structured-metadata field.

    Returns ({lang or "*" (shared backing pods): [hit labels]}, error text).
    """
    names = [n for pods in run.pods.values() for n, _ in pods if POD_RE.match(n)]
    if not names:
        return {}, ""
    start = min(t for pods in run.pods.values() for _, t in pods)
    q = (f'{{k8s_namespace_name={_quote(run.namespace)}, '
         f'k8s_pod_name=~{_quote("|".join(names + [BACKING_PODS]))}}}')
    try:
        lines, capped = loki_page(b, q, start, run.window_end)
    except QueryError as exc:
        return {}, str(exc)
    out: dict[str, list[str]] = {}
    for labels, line in lines:
        pod = labels.get("k8s_pod_name", "")
        m = POD_RE.match(pod)
        key = m.group(1) if m else "*"
        for kind in scan_secret(line):
            out.setdefault(key, []).append(f"{kind} in a Loki line of pod {pod}")
        for name, value in labels.items():
            for kind in scan_secret(str(value)):
                out.setdefault(key, []).append(f"{kind} in Loki field {name} of pod {pod}")
    if capped:
        out.setdefault("*", []).append(f"Loki slice of {MIN_SLICE}s still hit the {LOKI_PAGE_LIMIT}-record limit")
    return out, ""


# ── driver ──

def verify_demo(b: Backends, context: str, run: DemoRun, rep: DemoReport, *,
                cache: TraceCache | None = None, kubectl=None,
                max_log_traces: int = 200, dry_run: bool = False) -> None:
    demo_preflight(b, context, run, rep, kubectl=kubectl)
    if dry_run:
        return
    cache = cache or TraceCache(b)
    servicegraph_dropped(b, run, rep)
    loki_hits, loki_error = loki_hygiene(b, run)
    if loki_error:
        loki_hits = {"*": [f"Loki hygiene query error: {loki_error}"]}
    for lang in run.langs:
        rep.cell(lang, "a", check_a(b, run, lang, cache, max_log_traces))
        for cell in EXEMPLAR_CELLS:
            rep.cell(lang, cell, check_b(b, run, lang, cell, cache))
        rep.cell(lang, "c", check_c(b, run, lang))
        for cell, obs in check_d(b, run, lang, cache, rep).items():
            rep.cell(lang, cell, obs)
        rep.cell(lang, "e.trace", check_e_trace(b, run, lang, cache))
        for cell, obs in check_e_edges(b, run, lang, rep).items():
            rep.cell(lang, cell, obs)
        for cell, obs in check_g(b, run, lang, cache, loki_hits).items():
            rep.cell(lang, cell, obs)


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
                        help="namespace the smoke Jobs / example apps / demo run in")
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
    demo = sub.add_parser("demo", help="check one demo run (specs/demo-environment DE-D11)")
    demo.add_argument("--run", required=True, help="the demo run id (resource attribute otel_libs.run)")
    demo.add_argument("--window", type=_duration, default=_duration("15m"),
                      help="window W (default 15m)")
    demo.add_argument("--settle", type=_duration, default=_duration("2m"),
                      help=f"settle delay, at least {MIN_SETTLE}s (default 2m)")
    demo.add_argument("--expected", type=Path, default=EXPECTED_JSON,
                      help="expected cells (default testing/k8s/demo/expected.json)")
    demo.add_argument("--max-log-traces", type=int, default=200,
                      help="check a: trace ids sampled from Loki per language (default 200)")
    demo.add_argument("--dry-run", action="store_true",
                      help="read-only pre-flight only (flags, start time, target_info, pods)")
    args = parser.parse_args(argv)

    b = Backends(
        context=args.context,
        namespace=os.environ.get("BACKENDS_NAMESPACE", "monitoring"),
        tempo=os.environ.get("TEMPO", "tempo:3200"),
        loki=os.environ.get("LOKI", "loki:3100"),
        prometheus=os.environ.get("PROMETHEUS", "kps-prometheus:9090"),
    )
    end = int(time.time())
    if args.mode == "demo":
        return main_demo(b, args, end)
    start = end - args.since
    rep = Report()
    if args.mode == "smoke":
        verify_smoke(b, args.namespace, args.run, args.langs, start, end, rep)
    else:
        verify_examples(b, args.langs, args.fresh, end, rep)
    print(f"verify {args.mode}: {rep.total - rep.failed}/{rep.total} checks passed", flush=True)
    return 1 if rep.failed else 0


def main_demo(b: Backends, args: argparse.Namespace, now: int) -> int:
    if args.settle < MIN_SETTLE:
        print(f"verify demo: --settle must be at least {MIN_SETTLE}s (DE-D11)", file=sys.stderr)
        return 2
    if args.window <= 0:
        print("verify demo: --window must be positive", file=sys.stderr)
        return 2
    try:
        expected = load_expected(args.expected)
    except (OSError, ValueError) as exc:
        print(f"verify demo: {exc}", file=sys.stderr)
        return 2
    window_end = now - args.settle
    run = DemoRun(run=args.run, langs=args.langs, namespace=args.namespace,
                  window_start=window_end - args.window, window_end=window_end, now=now)
    rep = DemoReport(expected)
    rep.info(f"run {args.run}, languages {' '.join(args.langs)}, window "
             f"[{run.window_start}, {run.window_end}] ({args.window}s, settle {args.settle}s)")
    verify_demo(b, args.context, run, rep, max_log_traces=args.max_log_traces, dry_run=args.dry_run)
    if args.dry_run:
        print(f"verify demo --dry-run: {'BLOCKED' if rep.preflight_blocked else 'OK'}", flush=True)
        return 1 if rep.preflight_blocked else 0
    rep.matrix(args.langs)
    return 1 if rep.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
