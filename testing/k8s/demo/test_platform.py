"""Tests for ``verify.py platform`` (specs/soak-simulation K1, r6.2; soakplatform.py).

Standard library unittest; no cluster access. Prometheus, Tempo and kubectl answers
are built inline in their API shapes; the ztunnel log line and the PID 1 outputs
are shaped like the reads on k8s01 on 2026-11-23 (values made up).
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import soakplatform as sp  # noqa: E402
import verify  # noqa: E402

NOW = 1_795_000_000.0
DAY = 86400


def series(metric: dict, values: list[tuple[float, float]]) -> dict:
    return {"metric": metric, "values": [[t, str(v)] for t, v in values]}


def instant(metric: dict, value: float) -> dict:
    return {"metric": metric, "value": [NOW, str(value)]}


def flat(start: float, end: float, value: float, step: float = 60) -> list[tuple[float, float]]:
    t, out = start, []
    while t <= end:
        out.append((t, value))
        t += step
    return out


def k8s(**labels) -> dict:
    return {"cluster": "k8s01", **labels}


class QueryBuilding(unittest.TestCase):
    def test_cluster_label_always_first(self):
        q = sp.sel("up", {"job": "tempo"})
        self.assertEqual(q, 'up{cluster="k8s01", job="tempo"}')
        self.assertIn('cluster="k8s01"', sp.sel("up"))
        self.assertIn('job=~"a|b"', sp.sel("up", {"job": ("=~", "a|b")}))

    def test_cluster_cannot_be_overridden(self):
        with self.assertRaises(ValueError):
            sp.sel("up", {"cluster": "vps"})
        with self.assertRaises(ValueError):
            sp.sel("up", {"job": ("<>", "x")})

    def test_name_selector_carries_cluster(self):
        q = sp.name_sel("a|b", {"job": "j"})
        self.assertEqual(q, '{__name__=~"a|b", cluster="k8s01", job="j"}')

    def test_every_catalog_query_has_the_cluster(self):
        r = FakeReader()
        sp.read_sentinels(r, NOW, (NOW - 1800,), copy_idle=False)
        sp.read_edges(r, NOW, (NOW - 1800,))
        self.assertTrue(r.queries)
        for q in r.queries:
            self.assertIn('cluster="k8s01"', q, q)

    def test_vps_series_ignored(self):
        res = [instant(k8s(job="x"), 1), instant({"cluster": "vps", "job": "x"}, 1), instant({"job": "x"}, 1)]
        self.assertEqual(len(sp.cluster_only(res)), 1)

    def test_vps_noisy_series_never_enters_a_sentinel(self):
        r = FakeReader()
        start = NOW - 1800
        r.raw["otelcol_receiver_refused_spans"] = [
            series({"cluster": "vps", "__name__": "otelcol_receiver_refused_spans", "job": sp.AGENT_JOB},
                   [(start, 0), (NOW - 60, 5)]),
            series(k8s(__name__="otelcol_receiver_refused_spans", job=sp.AGENT_JOB), flat(start - 60, NOW, 0))]
        out = {v.id: v for v in sp.read_sentinels(r, NOW, (start,), copy_idle=False)[0]}
        self.assertEqual(out[f"{sp.AGENT_JOB}:otelcol_receiver_refused_spans"].verdict, "CLEAN")


class Credentials(unittest.TestCase):
    def test_object(self):
        obj = {
            "metadata": {"name": "x", "managedFields": [{"a": 1}],
                         "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{...}", "k": "v"}},
            "spec": {
                "env": [{"name": "API_TOKEN", "value": "abc"}, {"name": "LEVEL", "value": "info"},
                        {"name": "PW", "valueFrom": {"secretKeyRef": {"name": "s", "key": "password"}}}],
                "config": {"exporters": {"otlp": {"endpoint": "https://user:pw@host:4317",
                                                  "headers": {"Authorization": "Bearer abc.def"}}},
                           "extensions": {"basicauth": {"client_auth": {"password": "p", "username": "u"}}}},
                "note": "send with Bearer xyz123",
            },
        }
        out = sp.strip_credentials(obj)
        text = json.dumps(out)
        self.assertNotIn("managedFields", text)
        self.assertNotIn("last-applied", text)
        self.assertEqual(out["metadata"]["annotations"], {"k": "v"})
        self.assertEqual(out["spec"]["env"][0]["value"], sp.REDACTED)
        self.assertEqual(out["spec"]["env"][1]["value"], "info")
        self.assertEqual(out["spec"]["env"][2]["valueFrom"]["secretKeyRef"]["name"], "s")
        self.assertEqual(out["spec"]["config"]["exporters"]["otlp"]["endpoint"], f"https://{sp.REDACTED}@host:4317")
        self.assertEqual(out["spec"]["config"]["exporters"]["otlp"]["headers"]["Authorization"], sp.REDACTED)
        self.assertEqual(out["spec"]["config"]["extensions"]["basicauth"]["client_auth"]["password"], sp.REDACTED)
        self.assertEqual(out["spec"]["note"], f"send with Bearer {sp.REDACTED}")
        for secret in ("abc", "pw@", "abc.def", "xyz123", '"p"'):
            self.assertNotIn(secret, text)
        self.assertEqual(obj["spec"]["env"][0]["value"], "abc")  # input untouched

    def test_text(self):
        text = ("exporters:\n  otlp:\n    endpoint: http://u:secretpw@h:4317\n    headers:\n"
                "      authorization: Bearer s3cr3tv\n    tls:\n      insecure: true\n"
                "  basic:\n    token:\n      value: zzz\n      more: yyy\n    keep: 1\n")
        out = sp.strip_text(text)
        for secret in ("secretpw", "s3cr3tv", "zzz", "yyy"):
            self.assertNotIn(secret, out)
        self.assertIn("insecure: true", out)
        self.assertIn("keep: 1", out)
        self.assertEqual(sp.strip_text("k8s_attributes:\n  passthrough: true\n"), "k8s_attributes:\n  passthrough: true\n")
        self.assertIn(f"authorization: {sp.REDACTED}", out)
        self.assertTrue(out.endswith("\n"))

    def test_embedded_yaml_string(self):
        out = sp.strip_credentials({"data": {"config.yaml": "a: 1\napi_key: k1\n"}})
        self.assertEqual(out["data"]["config.yaml"], f"a: 1\napi_key: {sp.REDACTED}\n")


class Counters(unittest.TestCase):
    START = NOW - 3600

    def test_flat_is_clean(self):
        self.assertEqual(sp.raw_increase(flat(self.START - 60, NOW, 7), self.START), (0.0, None))

    def test_increase_and_reset(self):
        vals = [(self.START - 30, 1), (self.START + 60, 3), (self.START + 120, 2), (self.START + 180, 2)]
        inc, last = sp.raw_increase(vals, self.START)
        self.assertEqual(inc, 4.0)  # +2, then a reset that adds 2
        self.assertEqual(last, self.START + 120)

    def test_born_inside_adds_first_value(self):
        # Review H2: a series with no sample before the start was born inside the
        # window, however soon after the start it appears (the read reaches back
        # BORN_GRACE + one scrape, so an older series has a sample there).
        vals = [(self.START + 600, 3), (self.START + 660, 3)]
        self.assertEqual(sp.raw_increase(vals, self.START)[0], 3.0)
        self.assertEqual(sp.raw_increase([(self.START + 30, 3), (self.START + 90, 3)], self.START)[0], 3.0)
        self.assertEqual(sp.raw_increase([(self.START + 30, 0), (self.START + 90, 0)], self.START)[0], 0.0)
        # A series present before the start does not count its first value.
        self.assertEqual(sp.raw_increase([(self.START - 30, 3), (self.START + 90, 3)], self.START)[0], 0.0)

    def test_born_inside_a_short_window_read(self):
        """A 120 s window read: the series appears 40 s after the start and is counted."""
        r = FakeReader()
        start = NOW - 120
        name = "otelcol_exporter_send_failed_spans"
        r.siblings[sp.GATEWAY_JOB] = {"otelcol_exporter_sent_spans"}
        r.raw[name] = [series(k8s(__name__=name, job=sp.GATEWAY_JOB), [(start + 40, 2), (NOW, 2)]),
                       # an old, flat series: its samples before the start are inside the reach
                       series(k8s(__name__=name, job=sp.GATEWAY_JOB, exporter="b"),
                              [(start - sp.BORN_GRACE, 5), (start + 10, 5), (NOW, 5)])]
        out = {v.id: v for v in sp.read_sentinels(r, NOW, (start,), copy_idle=False)[0]}
        v = out[f"{sp.GATEWAY_JOB}:{name}"]
        self.assertEqual((v.verdict, v.increase), ("NOISY", 2.0))
        self.assertTrue(any(q.endswith(f"[{120 + sp.BORN_GRACE + sp.SCRAPE_S}s]") for q in r.queries))

    def test_samples_before_start(self):
        # An increase before the window is not counted; a series older than the window is not "born".
        vals = [(self.START - 600, 0), (self.START - 60, 5), (self.START, 5), (self.START + 60, 5)]
        self.assertEqual(sp.raw_increase(vals, self.START)[0], 0.0)
        sparse = [(self.START - 20000, 4), (self.START + 1800, 4)]
        self.assertEqual(sp.raw_increase(sparse, self.START), (0.0, None))
        across = [(self.START - 30, 1), (self.START + 30, 3)]
        self.assertEqual(sp.raw_increase(across, self.START)[0], 2.0)
        self.assertEqual(sp.raw_increase([(self.START - 30, 1)], self.START), (0.0, None))

    def test_hourly(self):
        vals = flat(NOW - 7200, NOW, 0)
        vals = [(t, 0 if t < NOW - 1800 else 4) for t, _ in vals]
        hours = sp.hourly(vals, NOW - 7200, NOW)
        self.assertEqual(hours, [0.0, 4.0])

    def test_verdicts(self):
        m = {"__name__": "x"}
        clean = sp.judge_counter("c", "shared", [series(m, flat(self.START - 60, NOW, 2))], True, self.START, NOW)
        self.assertEqual(clean.verdict, "CLEAN")
        noisy = sp.judge_counter("c", "shared", [series(m, [(self.START - 1, 0), (NOW, 2)])], False, self.START, NOW)
        self.assertEqual((noisy.verdict, noisy.increase), ("NOISY", 2.0))
        self.assertEqual(sp.judge_counter("c", "shared", [], True, self.START, NOW).verdict, "CLEAN")
        self.assertEqual(sp.judge_counter("c", "shared", [], False, self.START, NOW).verdict, "ABSENT")
        self.assertEqual(sp.judge_counter("c", "copy", [], False, self.START, NOW, idle_ok=True).verdict, "NA")

    def test_catalog(self):
        cat = sp.sentinel_catalog()
        self.assertEqual(len(cat), 3 * 3 * 5 + 2)
        ids = {c["id"] for c in cat}
        self.assertIn(f"{sp.COPY_JOB}:otelcol_processor_memory_limiter_refused_log_records", ids)
        for c in cat:
            if c["metric"].startswith("otelcol_exporter"):
                self.assertTrue(c["sibling"].startswith("otelcol_exporter_sent_"))
            self.assertFalse(c["metric"].startswith("otelcol_") and c["metric"].endswith("_total"))

    def test_read_sentinels_sibling_rule(self):
        r = FakeReader()
        r.siblings[sp.GATEWAY_JOB] = {"otelcol_receiver_accepted_spans"}
        out = {v.id: v for v in sp.read_sentinels(r, NOW, (NOW - 1800,), copy_idle=True)[0]}
        self.assertEqual(out[f"{sp.GATEWAY_JOB}:otelcol_receiver_refused_spans"].verdict, "CLEAN")
        self.assertEqual(out[f"{sp.GATEWAY_JOB}:otelcol_receiver_refused_log_records"].verdict, "ABSENT")
        self.assertEqual(out[f"{sp.COPY_JOB}:otelcol_receiver_refused_log_records"].verdict, "NA")


class Floors(unittest.TestCase):
    """Review H7 (rule in K-D13, dated 2026-11-23): initial floors are the lower
    bound, BELOW_INITIAL fails, a stricter floor only from a K-cal footprint."""

    def test_initial_floors_until_kcal(self):
        f = sp.derive_floors([8 * 2**30, 10 * 2**30], [0.5, 0.6], [0.4, 0.41])
        self.assertEqual({k: v["floor"] for k, v in f.items()}, sp.INITIAL_FLOORS)
        self.assertTrue(all(v["verdict"] == "PASS" for v in f.values()))
        self.assertIn("no K-cal footprint", f["mem_available_bytes"]["basis"])

    def test_below_initial(self):
        f = sp.derive_floors([1.5 * 2**30], [0.12], [0.06])
        self.assertTrue(all(v["verdict"] == "BELOW_INITIAL" for v in f.values()))
        self.assertEqual(f["mem_available_bytes"]["floor"], sp.INITIAL_FLOORS["mem_available_bytes"])

    def test_footprint_makes_a_stricter_floor_only(self):
        f = sp.derive_floors([10 * 2**30], [0.6], [0.4],
                             footprint={"mem_available_bytes": 2 * 2**30, "idle_cpu_share": 0.4})
        self.assertEqual(f["mem_available_bytes"]["floor"], 7 * 2**30)  # 10 - 2 x 1.5
        self.assertEqual(f["idle_cpu_share"]["floor"], 0.15)  # 0.6 - 0.6 = 0 is not stricter
        self.assertIn("not stricter", f["idle_cpu_share"]["basis"])

    def test_absent(self):
        self.assertEqual(sp.derive_floors([], [0.5], [0.5])["mem_available_bytes"]["verdict"], "ABSENT")

    def test_below_initial_fails_the_snapshot(self):
        r = healthy_reader()
        r.ranges[0] = (r"MemAvailable", [series(k8s(), flat(NOW - 3600, NOW, 1 * 2**30))])
        with tempfile.TemporaryDirectory() as tmp:
            code, text = run(r, Path(tmp))
        self.assertEqual(code, 1)
        self.assertIn("FAIL  floor mem_available_bytes", text)


class Pid1(unittest.TestCase):
    def test_apps(self):
        self.assertTrue(sp.parse_pid1("demo-api\n/usr/local/bin/demo-api \n", "go", "api")["ok"])
        self.assertTrue(sp.parse_pid1("dotnet\ndotnet Demo.Api.dll \n", "dotnet", "api")["ok"])
        self.assertTrue(sp.parse_pid1("python3.13\npython api.py\n", "python", "api")["ok"])
        self.assertTrue(sp.parse_pid1("node\nnode api/main.js\n", "node", "api")["ok"])

    def test_argv_basenames(self):
        res = sp.parse_pid1("demo-worker\n/usr/local/bin/demo-worker --flag=secret extra\n", "go", "worker")
        self.assertEqual(res["argv"], ["demo-worker", "--flag=secret"])

    def test_not_the_app(self):
        self.assertFalse(sp.parse_pid1("sh\nsh -c node api.js\n", "node", "api")["ok"])
        self.assertFalse(sp.parse_pid1("tini\ntini -- dotnet x\n", "dotnet", "api")["ok"])
        self.assertFalse(sp.parse_pid1("", "go", "api")["ok"])
        self.assertFalse(sp.parse_pid1("demo-api\n", "go", "backend")["ok"])
        self.assertFalse(sp.parse_pid1("java\n", "node", "api")["ok"])


class Ztunnel(unittest.TestCase):
    LINE = json.dumps({"level": "error", "time": "2026-11-23T15:15:31Z", "message": "connection complete",
                       "error": "io error: Connection refused (os error 111)", "src.namespace": "dev-otel-libs",
                       "src.workload": "demo-go-api", "dst.addr": "10.0.0.1:15008", "dst.namespace": "dev-otel-libs",
                       "dst.workload": "soak-agent", "src.identity": "spiffe://x"})

    def test_hits_and_misses(self):
        other = self.LINE.replace("dev-otel-libs", "argo")
        no_hbone = self.LINE.replace(":15008", ":4317")
        hits = sp.ztunnel_refused([self.LINE, other, no_hbone, "plain text"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["dst.workload"], "soak-agent")
        self.assertNotIn("src.identity", hits[0])
        text = "2026 ERROR connection refused 10.0.0.1:15008 ns monitoring"
        self.assertEqual(sp.ztunnel_refused([text]), [{"time": None, "text": "non-JSON line"}])


class Pods(unittest.TestCase):
    def pod(self, ready_at: float, started_at: float, ready: str = "True") -> dict:
        return {"metadata": {"name": "demo-go-api-1-a", "namespace": sp.APP_NS},
                "status": {"phase": "Running",
                           "conditions": [{"type": "Ready", "status": ready, "lastTransitionTime": sp.iso(ready_at)}],
                           "containerStatuses": [{"name": "api", "restartCount": 0,
                                                  "state": {"running": {"startedAt": sp.iso(started_at)}}}]}}

    def test_demo_ready(self):
        self.assertTrue(sp.demo_ready(self.pod(NOW - 900, NOW - 950), NOW, 600)[0])
        self.assertFalse(sp.demo_ready(self.pod(NOW - 300, NOW - 950), NOW, 600)[0])
        self.assertFalse(sp.demo_ready(self.pod(NOW - 900, NOW - 100), NOW, 600)[0])
        self.assertFalse(sp.demo_ready(self.pod(NOW - 900, NOW - 950, "False"), NOW, 600)[0])

    def test_yaml_block(self):
        text = ("server:\n  port: 1\noverrides:\n    defaults:\n        ingestion:\n            rate_limit_bytes: 3\n"
                "        global:\n            max_bytes_per_trace: 5\n    per_tenant_override_period: 10s\nother: 1\n")
        self.assertEqual(sp.yaml_block(text, ["overrides", "defaults"]),
                         {"ingestion.rate_limit_bytes": "3", "global.max_bytes_per_trace": "5"})
        self.assertEqual(sp.yaml_block(text, ["missing"]), {})
        self.assertEqual(sp.config_value("a:\n  max_stale: 30m # x\n", "max_stale"), "30m")


# ── an end-to-end run against fake cluster answers ──

def pod_obj(ns: str, name: str, started: float, annotated: bool = True, image: str = "img:1") -> dict:
    container = name.split("-")[2] if name.startswith("demo-") else "c"
    return {"metadata": {"name": name, "namespace": ns,
                         "annotations": {"ambient.istio.io/redirection": "enabled"} if annotated else {}},
            "spec": {"nodeName": "k8s01", "containers": [{"name": container,
                                                          "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}}}]},
            "status": {"phase": "Running", "startTime": sp.iso(started),
                       "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": sp.iso(started)}],
                       "containerStatuses": [{"name": container, "restartCount": 0, "image": image,
                                              "imageID": "sha256:" + "0" * 64, "ready": True,
                                              "state": {"running": {"startedAt": sp.iso(started)}}}]}}


def matches(q: str, metric: dict) -> bool:
    """Whether a series satisfies every matcher of a selector (except job and reporter)."""
    for key, op, val in re.findall(r'(\w+)(=~|!=|=)"([^"]*)"', q):
        if key in ("job", "reporter", "cluster"):
            continue
        got = metric.get(key, "")
        if (op == "=" and got != val) or (op == "=~" and not re.fullmatch(val, got)) or (op == "!=" and got == val):
            return False
    return True


def bounded(s: dict, at: float, rng: str) -> dict:
    """A range selector's answer: only the samples in [at - range, at] (review H2)."""
    width = float(rng.strip("[]s"))
    return {**s, "values": [v for v in s.get("values") or [] if at - width <= float(v[0]) <= at]}


class FakeReader:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.raw: dict[str, list[dict]] = {}        # metric name -> raw series for range selectors
        self.siblings: dict[str, set[str]] = {}     # job -> present sibling metric names
        self.instant: list[tuple[str, list[dict]]] = []
        self.ranges: list[tuple[str, list[dict]]] = []
        self.pods: list[dict] = []
        self.kube: dict[str, dict] = {}
        self.texts: dict[str, str] = {}
        self.tempo_ans: dict[str, dict] = {}
        self.pid1_out: dict[str, str] = {}
        self.log_lines: list[str] = []
        self.copy_series: list[dict] = []

    def prom(self, q: str, at: float) -> list[dict]:
        self.queries.append(q)
        m = re.match(r"^\{__name__=~\"([^\"]+)\", cluster=\"k8s01\", job=\"([^\"]+)\"\}(\[\d+s\])?$", q)
        if m:
            names = m.group(1).split("|")
            if m.group(3):
                return [bounded(s, at, m.group(3)) for n in names for s in self.raw.get(n, [])
                        if s["metric"].get("job") == m.group(2) or s["metric"].get("job") is None]
            return [instant(k8s(__name__=n, job=m.group(2)), 1) for n in sorted(self.siblings.get(m.group(2), set()))
                    if n in names]
        if q.startswith("istio_tcp_connections_failed_total{") and q.endswith("s]"):
            rng = q[q.rindex("["):]
            return [bounded(s, at, rng) for s in self.raw.get("istio_tcp_connections_failed_total", [])
                    if matches(q, s["metric"])]
        for pattern, ans in self.instant:
            if re.search(pattern, q):
                group = re.search(r"\bby \(([^)]*)\)", q)
                if group and "cluster" not in group.group(1):  # an aggregation drops the cluster label
                    return [{**s, "metric": {k: v for k, v in s["metric"].items() if k != "cluster"}} for s in ans]
                return ans
        return []

    def prom_range(self, q: str, start: float, end: float, step: int) -> list[dict]:
        self.queries.append(q)
        for pattern, ans in self.ranges:
            if re.search(pattern, q):
                return ans
        return []

    def prom_series(self, match: str, start: float, end: float) -> list[dict]:
        self.queries.append(match)
        return self.copy_series

    def prom_status(self, which: str) -> dict:
        return {"version": "3.16.0", "revision": "abc"}

    def tempo(self, path: str, params: dict) -> dict:
        return self.tempo_ans.get(path, {})

    def tempo_search(self, q: str, start: float, end: float) -> list[dict]:
        return [{"traceID": "1"}]

    def text(self, service: str, path: str) -> str:
        return self.texts.get(service, "")

    def kget(self, args: list[str]) -> dict:
        if args[0] == "pods":
            return {"items": self.pods}
        return self.kube[args[0] if args[0] not in ("opentelemetrycollectors.opentelemetry.io",) else args[-1]]

    def logs(self, namespace: str, pod: str, since_s: int) -> list[str]:
        return self.log_lines

    def pid1(self, namespace: str, pod: str, container: str) -> str:
        return self.pid1_out.get(pod, "sh\nsh\n")


def healthy_reader() -> FakeReader:
    r = FakeReader()
    old = NOW - 2 * DAY
    for lang in verify.LANGS:
        for app in verify.DEMO_APPS:
            name = f"demo-{lang}-{app}-1a2b3c-x9y8z"
            r.pods.append(pod_obj(sp.APP_NS, name, old))
            comm = {"go": f"demo-{app}", "dotnet": "dotnet", "python": "python", "node": "node"}[lang]
            r.pid1_out[name] = f"{comm}\n{comm} main\n"
    for ns, name in ((sp.APP_NS, "soak-agent-5f4d7fd6f8-mwbfn"), ("opentelemetry", "otel-agent-collector-2lx9n"),
                     ("opentelemetry", "otel-gateway-collector-7cc44695b6-7pj59"), ("monitoring", "tempo-0"),
                     ("monitoring", "loki-0"), ("monitoring", "prometheus-kps-0"),
                     ("monitoring", "kube-state-metrics-5c8655459f-tfv76"), ("istio-system", "ztunnel-qfmq4"),
                     ("istio-system", "istio-cni-node-abcde"), ("istio-system", "istiod-6d5f7b8c9-abcde")):
        r.pods.append(pod_obj(ns, name, old, annotated=ns != "istio-system"))
    annotated = sum(1 for p in r.pods if p["metadata"]["annotations"])
    cr = {"spec": {"mode": "deployment", "image": "contrib:0.165.0", "config": {"connectors": {
        "span_metrics": {"metrics_expiration": "5m", "metrics_flush_interval": "30s",
                         "dimensions": [{"name": "http.route"}]}}},
        "env": [{"name": "TOKEN", "value": "t0ps3cret"}]}, "status": {"version": "0.162.0"},
        "metadata": {"generation": 3}}
    r.kube.update({"otel-agent": cr, "otel-gateway": cr, "otel-logs": cr,
                   "configmap": {"data": {"config.yaml": "otel_libs.soak.fixed_spans:\n  max_stale: 30m\n"
                                                         "metric_expiration: 30m\n"
                                                         'x: attributes["rpc.system.name"] == "grpc"\n'
                                                         "password: hunter2\n"}},
                   "prometheus": {"spec": {"otlp": {"promoteResourceAttributes": ["service.name"],
                                                    "translationStrategy": "UnderscoreEscapingWithSuffixes"},
                                           "retention": "45d", "retentionSize": "50GB",
                                           "storage": {"volumeClaimTemplate": {"spec": {"resources": {
                                               "requests": {"storage": "60Gi"}}}}}}},
                   "pvc": {"spec": {"resources": {"requests": {"storage": "20Gi"}}},
                           "status": {"phase": "Bound", "capacity": {"storage": "20Gi"}}},
                   "nodes": {"items": [{"metadata": {"name": "k8s01"}, "spec": {},
                                        "status": {"allocatable": {"cpu": "16", "memory": "64Gi", "pods": "250"},
                                                   "conditions": [{"type": "Ready", "status": "True"}]}}]},
                   "namespaces": {"items": [{"metadata": {"name": n, "labels": {"istio.io/dataplane-mode": "ambient"}}}
                                            for n in sp.MESH_NAMESPACES]}})
    r.texts = {"tempo:3200": "overrides:\n    defaults:\n        ingestion:\n            rate_limit_bytes: 1\n",
               "loki:3100": "limits_config:\n  ingestion_rate_mb: 4\n"}
    r.tempo_ans = {"/api/v2/search/tags": {"scopes": [{"name": "resource", "tags": [
        "k8s.namespace.name", "service.instance.id", "service.name", "service.namespace"]}]},
        "/api/v2/search/tag/resource.k8s.namespace.name/values": {"tagValues": [{"value": "dev-otel-libs"}]},
        "/api/v2/search/tag/resource.service.namespace/values": {"tagValues": [{"value": "dev-otel-libs"}]}}
    for job in (sp.COPY_JOB, sp.AGENT_JOB, sp.GATEWAY_JOB, "tempo", "monitoring/loki"):
        r.siblings[job] = {c["sibling"] for c in sp.sentinel_catalog() if c["job"] == job}
    r.instant = [
        (r"workload_manager_active_proxy_count", [instant(k8s(job=sp.ZTUNNEL_JOB), annotated)]),
        (r'^up\{cluster="k8s01", job="istio-system/ztunnel"\}', [instant(k8s(job=sp.ZTUNNEL_JOB), 1)]),
        (r"^up\{", [instant(k8s(job=sp.COPY_JOB, endpoint="metrics"), 1), instant(k8s(job=sp.COPY_JOB, endpoint="count"), 1),
                    instant(k8s(job=sp.AGENT_JOB), 1), instant(k8s(job=sp.GATEWAY_JOB), 1),
                    instant({"cluster": "vps", "job": sp.AGENT_JOB}, 0)]),
        (r"^count by \(cluster, job, pod, k8s_pod_name\)", [instant(k8s(job=f"j{i}"), 1) for i in range(12)]),
        (r"otelcol_process_uptime", [instant(k8s(job=sp.GATEWAY_JOB), 1000)]),
        (r"prometheus_tsdb_lowest", [instant(k8s(), NOW - 9 * DAY)]),
        (r"prometheus_tsdb_storage", [instant(k8s(), 7e9)]),
    ]
    r.ranges = [
        (r"MemAvailable", [series(k8s(), flat(NOW - 3600, NOW, 20 * 2**30))]),
        (r"node_cpu_seconds_total", [series(k8s(), flat(NOW - 3600, NOW, 0.6))]),
        (r"node_filesystem_avail", [series(k8s(), flat(NOW - 3600, NOW, 0.5))]),
    ]
    return r


def run(r: FakeReader, out: Path, **extra) -> tuple[int, str]:
    args = argparse.Namespace(run="k1-test", baseline=DAY, lookback=1800, floor_window=3600, ready_settle=600,
                              expect_copy=False, fakeotlp_image="harbor.example/dev/otel-libs-fakeotlp:ab24d24@sha256:"
                              + "a" * 64, out_dir=out, skip_exec=False, context="k8s01")
    for k, v in extra.items():
        setattr(args, k, v)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = sp.main_platform(None, args, NOW, reader=r)
    return code, buf.getvalue()


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_platform_passes(self):
        code, text = run(healthy_reader(), self.out)
        self.assertEqual(code, 0, text)
        snap = json.loads((self.out / "snapshot.json").read_text())
        self.assertEqual(snap["verdict"], "PASS")
        self.assertEqual(snap["items"]["guard (h) copy counts"], "NA")
        self.assertEqual(snap["tempo"]["selectors"]["namespace"], '{resource.k8s.namespace.name="dev-otel-libs"}')
        self.assertEqual(len(snap["prometheus"]["findings"]), 1)
        self.assertTrue(snap["prometheus"]["findings"][0].startswith("PVC capacity 20Gi differs from the CR request 60Gi"))
        self.assertIn("not an outage risk", snap["prometheus"]["findings"][0])
        self.assertIn("ab24d24", snap["fakeotlp"]["image"])
        self.assertIs(snap["baseline"]["clean_over_whole_baseline"], True)
        self.assertEqual(snap["baseline"]["noisy"], {})
        written = "".join(p.read_text() for p in self.out.rglob("*") if p.is_file())
        self.assertNotIn("t0ps3cret", written)
        self.assertNotIn("hunter2", written)
        self.assertTrue((self.out / "crs" / "otel-gateway.json").exists())

    def test_noisy_counter_fails_the_gate_and_dates_the_baseline(self):
        r = healthy_reader()
        r.raw["otelcol_exporter_send_failed_metric_points"] = [series(
            k8s(__name__="otelcol_exporter_send_failed_metric_points", job=sp.GATEWAY_JOB),
            [(NOW - DAY, 0), (NOW - 600, 0), (NOW - 300, 9), (NOW, 9)])]
        code, text = run(r, self.out)
        self.assertEqual(code, 1)
        self.assertIn("FAIL  gate sentinels clean", text)
        snap = json.loads((self.out / "snapshot.json").read_text())
        noisy = snap["baseline"]["noisy"][f"{sp.GATEWAY_JOB}:otelcol_exporter_send_failed_metric_points"]
        self.assertEqual(noisy["increase"], 9)
        self.assertEqual(snap["baseline"]["clean_24h_window_at"], sp.iso(NOW - 300 + DAY))
        self.assertEqual(snap["baseline"]["settled_24h_window_at"], sp.iso(NOW - 300 + DAY))

    def test_old_noise_only_in_baseline(self):
        r = healthy_reader()
        r.raw["istio_tcp_connections_failed_total"] = [series(
            k8s(source_workload_namespace=sp.APP_NS, source_workload="soak-agent",
                destination_workload_namespace="opentelemetry", destination_workload="otel-agent-collector",
                response_flags="CONNECT"),
            [(NOW - DAY, 0), (NOW - 20000, 4), (NOW, 4)])]
        code, text = run(r, self.out)
        self.assertEqual(code, 0, text)
        snap = json.loads((self.out / "snapshot.json").read_text())
        self.assertEqual(snap["baseline"]["edges"]["edge soak-agent->otel-agent-collector"]["verdict"], "NOISY")
        self.assertEqual(snap["guard"]["b"]["edge soak-agent->otel-agent-collector"], "CLEAN")

    def test_absent_shared_counter_without_sibling_fails(self):
        r = healthy_reader()
        r.siblings[sp.AGENT_JOB] = set()
        code, text = run(r, self.out)
        self.assertEqual(code, 1)
        self.assertRegex(text, r"FAIL  gate sentinels clean: .*absent")

    def test_mesh_and_pid1_and_copy_failures(self):
        r = healthy_reader()
        r.instant[0] = (r"workload_manager_active_proxy_count", [instant(k8s(job=sp.ZTUNNEL_JOB), 3)])
        r.log_lines = [Ztunnel.LINE]
        first = next(p for p in r.pods if p["metadata"]["name"].startswith("demo-node-api"))
        r.pid1_out[first["metadata"]["name"]] = "sh\nsh -c node\n"
        code, text = run(r, self.out, expect_copy=True)
        self.assertEqual(code, 1)
        for line in ("FAIL  guard (a) ztunnel proxies", "FAIL  guard (c) ztunnel refused :15008",
                     "FAIL  pid1 node-api", "FAIL  guard (h) copy counts"):
            self.assertIn(line, text)

    def test_copy_counts_pass(self):
        r = healthy_reader()
        r.instant.insert(0, (r"^sum by \(cluster, service_name\)", [instant(k8s(service_name=s), 5) for s in sp.DEMO_SERVICES]))
        code, text = run(r, self.out, expect_copy=True)
        self.assertIn("PASS  guard (h) copy counts: 12/12", text)

    def test_young_roll_list_pod_and_unready_demo(self):
        r = healthy_reader()
        gw = next(p for p in r.pods if p["metadata"]["name"].startswith("otel-gateway"))
        gw["status"]["containerStatuses"][0]["state"]["running"]["startedAt"] = sp.iso(NOW - 60)
        demo = next(p for p in r.pods if p["metadata"]["name"].startswith("demo-go-api"))
        demo["status"]["conditions"][0]["status"] = "False"
        code, text = run(r, self.out)
        self.assertEqual(code, 1)
        self.assertIn("FAIL  guard (d,e) roll list unchanged: gateway started", text)
        self.assertIn("FAIL  guard (g) demo pods Ready", text)
        snap = json.loads((self.out / "snapshot.json").read_text())
        self.assertEqual(snap["baseline"]["settled_since"], sp.iso(NOW - 60))

    def test_copy_labels_measured(self):
        r = healthy_reader()
        r.copy_series = [{"cluster": "k8s01", "service_instance_id": "i1", "exported_instance": "i1"},
                         {"cluster": "k8s01", "service_instance_id": "unset"},
                         {"cluster": "vps", "service_instance_id": "i2"}]
        code, text = run(r, self.out)
        self.assertIn("PASS  copy exported_instance: 1/1", text)
        r.copy_series[0].pop("exported_instance")
        code, text = run(r, self.out)
        self.assertIn("FAIL  copy exported_instance", text)

    def test_usage_errors(self):
        self.assertEqual(run(healthy_reader(), self.out, run="Bad Run")[0], 2)
        self.assertEqual(run(healthy_reader(), self.out, fakeotlp_image="img:latest")[0], 2)
        self.assertEqual(run(healthy_reader(), self.out, lookback=0)[0], 2)

    def test_secret_scan_blocks_a_file(self):
        r = healthy_reader()
        r.texts["loki:3100"] = "limits_config:\n  note: " + "ab" * 24 + "\n"
        err = io.StringIO()
        old = sys.stderr
        sys.stderr = err
        try:
            run(r, self.out)
        finally:
            sys.stderr = old
        self.assertIn("not writing snapshot.json: secret scan matched ['hex48']", err.getvalue())
        self.assertFalse((self.out / "snapshot.json").exists())

    def test_verify_cli_registers_the_mode(self):
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(io.StringIO()):
            verify.main(["platform", "--help"])
        self.assertEqual(ctx.exception.code, 0)


class ReviewFixes(unittest.TestCase):
    """K-loop review (2026-11-23): H3, B1/M10, M8, L1, L4."""

    def test_range_bounds_drop_old_samples(self):
        s = series(k8s(), [(NOW - 500, 1), (NOW - 100, 2), (NOW, 3)])
        self.assertEqual([v[0] for v in bounded(s, NOW, "[200s]")["values"]], [NOW - 100, NOW])

    def test_proxy_counts_tolerate_transitions(self):
        old, young = NOW - DAY, NOW - 10
        pods = [pod_obj(sp.APP_NS, "a", old), pod_obj(sp.APP_NS, "b", young), pod_obj(sp.APP_NS, "c", old),
                pod_obj(sp.APP_NS, "d", old, annotated=False)]
        pods[2]["metadata"]["deletionTimestamp"] = sp.iso(NOW - 5)
        settled, transition, running = sp.proxy_counts(pods, NOW)
        self.assertEqual((settled, transition), (1, 2))
        self.assertEqual(running, {f"{sp.APP_NS}/a", f"{sp.APP_NS}/b", f"{sp.APP_NS}/c"})

    def test_proxy_counts_slow_pull_succeeded_and_gone(self):
        """Re-review HIGH-1: start time from the containers / Ready, not the pod."""
        pull = pod_obj(sp.APP_NS, "k6-pull", NOW - 400)  # pod started 400 s ago ...
        pull["status"]["containerStatuses"][0]["state"] = {"running": {"startedAt": sp.iso(NOW - 20)}}  # ... ran 20 s
        waiting = pod_obj(sp.APP_NS, "k6-waiting", NOW - 400)
        waiting["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "ContainerCreating"}}
        ready_late = pod_obj(sp.APP_NS, "ready-late", NOW - DAY)
        ready_late["status"]["conditions"][0]["lastTransitionTime"] = sp.iso(NOW - 15)
        done = pod_obj(sp.APP_NS, "job-done", NOW - DAY)
        done["status"]["phase"] = "Succeeded"
        done["status"]["containerStatuses"][0]["state"] = {"terminated": {"finishedAt": sp.iso(NOW - 30)}}
        done_long_ago = json.loads(json.dumps(done))
        done_long_ago["metadata"]["name"] = "job-old"
        done_long_ago["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"] = sp.iso(NOW - 3600)
        settled_pod = pod_obj(sp.APP_NS, "steady", NOW - DAY)
        pods = [pull, waiting, ready_late, done, done_long_ago, settled_pod]
        settled, transition, running = sp.proxy_counts(pods, NOW, previous={f"{sp.APP_NS}/deleted-pod",
                                                                            f"{sp.APP_NS}/steady"})
        self.assertEqual(settled, 1)
        self.assertEqual(transition, 5)  # pull, waiting, ready-late, job-done, deleted-pod
        self.assertNotIn(f"{sp.APP_NS}/job-done", running)

    def test_scrape_gap_is_reset_unknown_not_an_increase(self):
        """Re-review M1: an old series with a 150 s gap over the start."""
        r = FakeReader()
        start = NOW - 120
        name = "otelcol_exporter_send_failed_spans"
        r.siblings[sp.GATEWAY_JOB] = {"otelcol_exporter_sent_spans"}
        old = series(k8s(__name__=name, job=sp.GATEWAY_JOB), [(start - 1000, 7), (start - 200, 7), (start + 30, 7),
                                                              (NOW, 7)])
        r.raw[name] = [old]
        r.instant.insert(0, (r"^last_over_time\(otelcol_exporter_send_failed_spans\{",
                             [instant(k8s(__name__=name, job=sp.GATEWAY_JOB), 7)]))
        v = {x.id: x for x in sp.read_sentinels(r, NOW, (start,), copy_idle=False)[0]}[f"{sp.GATEWAY_JOB}:{name}"]
        self.assertEqual((v.verdict, v.increase), ("UNKNOWN", 0.0))
        self.assertIn("reset unknown", v.detail)
        self.assertTrue(any(q.startswith("last_over_time(") and "[3600s]" in q for q in r.queries))
        # The same shape without history is born, and counts.
        r.instant.pop(0)
        v = {x.id: x for x in sp.read_sentinels(r, NOW, (start,), copy_idle=False)[0]}[f"{sp.GATEWAY_JOB}:{name}"]
        self.assertEqual((v.verdict, v.increase), ("NOISY", 7.0))
        # A later increase of a gapped series is still NOISY.
        r.raw[name] = [series(old["metric"], [(start - 200, 7), (start + 30, 7), (NOW, 9)])]
        r.instant.insert(0, (r"^last_over_time\(", [instant(k8s(), 7)]))
        v = {x.id: x for x in sp.read_sentinels(r, NOW, (start,), copy_idle=False)[0]}[f"{sp.GATEWAY_JOB}:{name}"]
        self.assertEqual((v.verdict, v.increase), ("NOISY", 2.0))

    def test_unknown_sentinel_fails_the_gate_as_an_error(self):
        rep = sp.PlatformReport()
        r = healthy_reader()
        unknown = sp.CounterVerdict("x", "shared", "UNKNOWN", detail="gap")
        edge = sp.CounterVerdict("edge e", "guard", "UNKNOWN", detail="gap")
        pods = r.pods
        roll = [sp.roll_entry(pods, ns, rx, role) for ns, rx, role in sp.ROLL_LIST]
        with redirect_stdout(io.StringIO()) as buf:
            sp.guard(r, rep, sp.Options(run="k1-test"), NOW, pods, roll, [unknown], [edge])
        self.assertEqual(rep.items["sentinel x"], "FAIL")
        self.assertEqual(rep.items["guard (b) edge e"], "FAIL")
        self.assertIn("query error: gap", buf.getvalue())

    def guard_items(self, r, **opts):
        rep = sp.PlatformReport()
        o = sp.Options(run="k1-test", **opts)
        pods = r.pods
        roll = [sp.roll_entry(pods, ns, rx, role) for ns, rx, role in sp.ROLL_LIST]
        with redirect_stdout(io.StringIO()):
            out = sp.guard(r, rep, o, NOW, pods, roll, [], [])
        return rep.items, out

    def test_guard_a_race_is_tolerated(self):
        r = healthy_reader()
        r.pods.append(pod_obj(sp.APP_NS, "new-pod", NOW - 5))  # annotated, not scraped yet
        items, out = self.guard_items(r)
        self.assertEqual(items["guard (a) ztunnel proxies"], "PASS")
        self.assertEqual(out["a"]["in_transition"], 1)
        r.pods.append(pod_obj(sp.APP_NS, "settled-unproxied", NOW - DAY))
        self.assertEqual(self.guard_items(r)[0]["guard (a) ztunnel proxies"], "FAIL")

    def test_guard_g_skips_terminating_and_follows_langs(self):
        r = healthy_reader()
        old = [p for p in r.pods if p["metadata"]["name"].startswith("demo-go-api")][0]
        old["metadata"]["deletionTimestamp"] = sp.iso(NOW - 5)
        old["status"]["conditions"][0]["status"] = "False"
        fresh = pod_obj(sp.APP_NS, "demo-go-api-9z9z9z-aaaaa", NOW - DAY)
        r.pods.append(fresh)
        items, _ = self.guard_items(r)
        self.assertEqual(items["guard (g) demo pods Ready"], "PASS")
        fresh["status"]["conditions"][0]["lastTransitionTime"] = sp.iso(NOW - 30)  # just restarted
        self.assertEqual(self.guard_items(r)[0]["guard (g) demo pods Ready"], "FAIL")
        r2 = healthy_reader()
        r2.pods = [p for p in r2.pods if not p["metadata"]["name"].startswith("demo-dotnet")]
        self.assertEqual(self.guard_items(r2)[0]["guard (g) demo pods Ready"], "FAIL")
        self.assertEqual(self.guard_items(r2, langs=("go", "python", "node"))[0]["guard (g) demo pods Ready"], "PASS")

    def test_guard_d_waiting_container(self):
        r = healthy_reader()
        zt = [p for p in r.pods if p["metadata"]["name"].startswith("ztunnel")][0]
        zt["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "CrashLoopBackOff"}}
        items, out = self.guard_items(r)
        self.assertEqual(items["guard (d,e) roll list unchanged"], "FAIL")
        self.assertTrue(any("not running" in y for y in out["d"]))

    def test_run_re_fullmatch(self):
        self.assertTrue(sp.RUN_RE.fullmatch("k1-20261123"))
        for bad in ("k1-", "-k1", "K1", "a" * 64, "k1\n"):
            self.assertIsNone(sp.RUN_RE.fullmatch(bad), bad)

    def test_strip_text_flow_maps(self):
        text = 'auth: {user: u, password: hunter2, "api_key": "k-1"}\nplain: {a: 1}\n'
        out = sp.strip_text(text)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("k-1", out)
        self.assertIn("user: u", out)
        self.assertIn("plain: {a: 1}", out)


if __name__ == "__main__":
    unittest.main()
