"""k6 script generator (specs/soak-simulation SR15 "Load (r6.2)", task K-loop).

The script drives ``POST /orders`` on each language's demo api with an open
loop (``constant-arrival-rate``), a fixed scenario mix and a fixed mix of
inbound classes (none / sampled=01 / sampled=00, random trace ids).

Per cell — language × route × scenario × inbound class (× phase when the run
has ``phase=pre|post`` scenarios, K-shut) — it declares one no-op threshold per
outcome on the counter ``soak_requests``: ``expected`` (status = the scenario's
expected status), ``unexpected``, ``noanswer`` (status 0). A threshold makes k6
keep that sub-metric, so ``handleSummary`` carries one count per cell and
outcome. The business scenario is tagged ``scen``, not ``scenario`` (a k6
system tag, the k6 scenario's name).

Orphan reservoir (SR9): ``handleSummary`` only receives aggregated metrics and
VUs share no mutable state, so the summary cannot carry per-request ids on
k6 2.3.0. The script uses the named fallback (K0e M5): one bounded stdout line
``SOAKRES {json}`` per request of ``reservoir`` rounds of each language
scenario, a round being ``len(mix) x len(classes)`` consecutive iterations
(``exec.scenario.iterationInTest``, unique per scenario across its VUs), one
round every ``stride`` = max(1, floor(planned rounds / reservoir)) so the
logged rounds spread over the window (2026-11-26, round 2), so every cell of
every language logs and the total is bounded whatever
``maxVUs`` is (2026-11-26: the former gate ``exec.vu.idInTest <= 2`` is global
across scenarios, so the languages whose scenarios did not get VUs 1 and 2 —
Go and Node.js in K8b ``k8b-20261125`` — logged nothing) — saved by the trap's
``kubectl logs``. The summary is printed between
``SOAK_SUMMARY_BEGIN`` / ``SOAK_SUMMARY_END`` lines.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from render import LANGS

EXPECTED_STATUS = {"ok": 201, "business_error": 422, "db_error": 409, "unhandled": 500}
# The demo worker's cycle of 10 (demo DE-D7): ok 7, one of each error.
DEFAULT_MIX = {"ok": 7, "business_error": 1, "db_error": 1, "unhandled": 1}
CLASSES = ("none", "s01", "s00")
DEFAULT_CLASSES = {"none": 1, "s01": 1, "s00": 1}
OUTCOMES = ("expected", "unexpected", "noanswer")
ROUTE = "orders"
MAX_TIMEOUT_S = 15
SUMMARY_BEGIN = "SOAK_SUMMARY_BEGIN"
SUMMARY_END = "SOAK_SUMMARY_END"
RESERVOIR_PREFIX = "SOAKRES "


class PlanError(ValueError):
    """The load plan violates a SR15 "Load (r6.2)" rule."""


@dataclass(frozen=True)
class Phase:
    name: str          # "pre" or "post" (K-shut); tag phase=<name>
    start_s: int
    duration_s: int


@dataclass(frozen=True)
class K6Plan:
    langs: tuple[str, ...]
    rate: int                    # requests per second, per language
    duration_s: int              # ignored when phases are given
    timeout_s: int = 10          # request timeout, <= 15 s (initial, unmeasured)
    p99_s: float = 1.0           # expected p99 latency (from K-cal; initial guess)
    mix: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_MIX))
    classes: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_CLASSES))
    reservoir: int = 50          # first N rounds per language scenario (initial, unmeasured)
    phases: tuple[Phase, ...] = ()
    url: str = "http://demo-{lang}-api:8080/orders"
    pull_s: int = 300            # image pull allowance in the deadline
    summary_s: int = 60          # summary write allowance in the deadline

    def validate(self) -> None:
        if not self.langs or any(lang not in LANGS for lang in self.langs):
            raise PlanError(f"langs {self.langs!r}: a non-empty subset of {LANGS}")
        if len(set(self.langs)) != len(self.langs):
            raise PlanError("langs: duplicate language")
        if self.rate < 1:
            raise PlanError("rate must be at least 1 request/s per language")
        if not 1 <= self.timeout_s <= MAX_TIMEOUT_S:
            raise PlanError(f"request timeout {self.timeout_s}s: 1-{MAX_TIMEOUT_S} s (SR15 Load)")
        if self.p99_s <= 0:
            raise PlanError("p99 must be > 0")
        if self.reservoir < 0:
            raise PlanError("reservoir must be >= 0")
        for name, table, known in (("mix", self.mix, EXPECTED_STATUS), ("classes", self.classes, CLASSES)):
            if not table or any(k not in known or v < 0 for k, v in table.items()) \
                    or sum(table.values()) == 0:
                raise PlanError(f"{name} {table!r}: non-negative shares of {tuple(known)}, not all 0")
        if self.phases:
            names = [p.name for p in self.phases]
            if sorted(names) != ["post", "pre"]:
                raise PlanError("phases: exactly pre and post")
            if any(p.start_s < 0 or p.duration_s < 1 for p in self.phases):
                raise PlanError("phases: start >= 0 and duration >= 1")
        elif self.duration_s < 1:
            raise PlanError("duration must be at least 1 s")
        if "{lang}" not in self.url:
            raise PlanError("url must contain {lang}")

    # ── derived numbers ────────────────────────────────────────────────────
    @property
    def max_vus(self) -> int:
        """maxVUs >= rate × (request timeout + p99), per language scenario."""
        return max(1, math.ceil(self.rate * (self.timeout_s + self.p99_s)))

    @property
    def pre_allocated_vus(self) -> int:
        return min(self.max_vus, max(1, math.ceil(self.rate * self.p99_s)))

    @property
    def concurrent_max_vus(self) -> int:
        """The most VUs alive at once: every language's scenario of one phase."""
        return self.max_vus * len(self.langs)

    @property
    def memory_limit_mi(self) -> int:
        """The k6 container memory limit: 256 MiB base + 4 MiB per concurrent VU
        (initial, unmeasured; K-cal records k6's peak RSS and replaces it)."""
        return 256 + 4 * self.concurrent_max_vus

    @property
    def reservoir_lines_max(self) -> int:
        """reservoir rounds x one round (the expanded mix x classes) per language scenario."""
        rounds = sum(self.mix.values()) * sum(self.classes.values())
        return self.reservoir * rounds * len(self.langs) * max(1, len(self.phases))

    @property
    def graceful_stop_s(self) -> int:
        """In-flight iterations may finish: one request timeout plus 5 s."""
        return self.timeout_s + 5

    @property
    def total_duration_s(self) -> int:
        if self.phases:
            return max(p.start_s + p.duration_s for p in self.phases)
        return self.duration_s

    @property
    def deadline_s(self) -> int:
        """activeDeadlineSeconds >= image pull + duration + gracefulStop + summary."""
        return self.pull_s + self.total_duration_s + self.graceful_stop_s + self.summary_s

    def cells(self) -> list[dict[str, str]]:
        """Every (lang, route, scen, class[, phase]) with a non-zero share."""
        out = []
        phases = [p.name for p in self.phases] or [None]
        for phase in phases:
            for lang in self.langs:
                for scen, share in self.mix.items():
                    if not share:
                        continue
                    for cls, cshare in self.classes.items():
                        if not cshare:
                            continue
                        cell = {"lang": lang, "route": ROUTE, "scen": scen, "class": cls}
                        if phase:
                            cell["phase"] = phase
                        out.append(cell)
        return out


def threshold_key(cell: dict[str, str], outcome: str) -> str:
    """``soak_requests{lang:go,route:orders,scen:ok,class:none[,phase:pre],outcome:expected}``."""
    order = ["lang", "route", "scen", "class", "phase"]
    tags = [f"{k}:{cell[k]}" for k in order if k in cell] + [f"outcome:{outcome}"]
    return "soak_requests{" + ",".join(tags) + "}"


def thresholds(plan: K6Plan) -> dict[str, list[str]]:
    return {threshold_key(c, o): ["count>=0"] for c in plan.cells() for o in OUTCOMES}


def _expand(table: dict[str, int], order) -> list[str]:
    return [k for k in order if k in table for _ in range(table[k])]


def reservoir_stride(plan: K6Plan, duration_s: int) -> int:
    """Rounds between two logged rounds, so the ``reservoir`` logged rounds spread
    over the planned window: max(1, floor(total rounds / reservoir)), total
    rounds = rate x duration / (len(mix) x len(classes))."""
    round_size = sum(plan.mix.values()) * sum(plan.classes.values())
    total_rounds = (plan.rate * duration_s) // round_size
    return max(1, total_rounds // plan.reservoir) if plan.reservoir else 1


def scenarios(plan: K6Plan) -> tuple[dict, dict]:
    """(k6 options.scenarios, scenario name -> {lang, phase, url})."""
    sc, meta = {}, {}
    phases = list(plan.phases) or [Phase("", 0, plan.duration_s)]
    for ph in phases:
        for lang in plan.langs:
            name = f"{ph.name}_{lang}" if ph.name else lang
            tags = {"lang": lang}
            if ph.name:
                tags["phase"] = ph.name
            sc[name] = {
                "executor": "constant-arrival-rate", "exec": "drive",
                "rate": plan.rate, "timeUnit": "1s", "duration": f"{ph.duration_s}s",
                "startTime": f"{ph.start_s}s",
                "preAllocatedVUs": plan.pre_allocated_vus, "maxVUs": plan.max_vus,
                "gracefulStop": f"{plan.graceful_stop_s}s", "tags": tags,
            }
            meta[name] = {"lang": lang, "phase": ph.name or None,
                          "url": plan.url.format(lang=lang), "stride": reservoir_stride(plan, ph.duration_s)}
    return sc, meta


_TEMPLATE = r"""// Generated by testing/k8s/soak/k6script.py (specs/soak-simulation K-loop). Do not edit.
import http from 'k6/http';
import exec from 'k6/execution';
import { Counter } from 'k6/metrics';

const CFG = __CFG__;
const requests = new Counter('soak_requests');
const CUSTOMERS = ['cust-a', 'cust-b', 'cust-c'];

export const options = __OPTIONS__;

function hex(n) {
  let s = '';
  while (s.length < n) s += Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, '0');
  s = s.slice(0, n);
  return /^0+$/.test(s) ? hex(n) : s;
}

export function drive() {
  const sc = CFG.scenarios[exec.scenario.name];
  const i = exec.scenario.iterationInTest;
  const scen = CFG.mix[i % CFG.mix.length];
  const cls = CFG.classes[Math.floor(i / CFG.mix.length) % CFG.classes.length];
  const n = i % 500;
  const body = {
    order_id: (scen === 'ok' ? 'ok-' : 'ord-') + n,
    customer: CUSTOMERS[i % CUSTOMERS.length],
    amount: scen === 'business_error' ? 5000 : 10 + (i % 90),
    scenario: scen,
  };
  const headers = { 'Content-Type': 'application/json' };
  let traceId = null;
  let parentId = null;
  if (cls !== 'none') {
    traceId = hex(32);
    parentId = hex(16);
    headers.traceparent = '00-' + traceId + '-' + parentId + '-' + (cls === 's01' ? '01' : '00');
  }
  const tags = { lang: sc.lang, route: CFG.route, scen: scen, class: cls };
  if (sc.phase) tags.phase = sc.phase;
  const res = http.post(sc.url, JSON.stringify(body), { headers: headers, timeout: CFG.timeout, tags: tags });
  const outcome = res.status === 0 ? 'noanswer' : (res.status === CFG.expected[scen] ? 'expected' : 'unexpected');
  requests.add(1, Object.assign({}, tags, { outcome: outcome }));
  // The SR9 reservoir: CFG.reservoir rounds of this scenario, one every sc.stride rounds.
  const round = Math.floor(i / (CFG.mix.length * CFG.classes.length));
  if (round % sc.stride === 0 && round / sc.stride < CFG.reservoir) {
    console.log(CFG.reservoirPrefix + JSON.stringify({
      lang: sc.lang, route: CFG.route, scen: scen, class: cls, phase: sc.phase,
      trace_id: traceId, parent_id: parentId, status: res.status, vu: exec.vu.idInTest,
    }));
  }
}

export function handleSummary(data) {
  return { stdout: CFG.summaryBegin + '\n' + JSON.stringify(data) + '\n' + CFG.summaryEnd + '\n' };
}
"""


def generate(plan: K6Plan) -> str:
    plan.validate()
    sc, meta = scenarios(plan)
    options = {
        "discardResponseBodies": True,
        "summaryTrendStats": ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"],
        "scenarios": sc,
        "thresholds": thresholds(plan),
    }
    cfg = {
        "scenarios": meta,
        "mix": _expand(plan.mix, EXPECTED_STATUS),
        "classes": _expand(plan.classes, CLASSES),
        "expected": EXPECTED_STATUS,
        "route": ROUTE,
        "timeout": f"{plan.timeout_s}s",
        "reservoir": plan.reservoir,
        "reservoirPrefix": RESERVOIR_PREFIX,
        "summaryBegin": SUMMARY_BEGIN,
        "summaryEnd": SUMMARY_END,
    }
    return (_TEMPLATE.replace("__CFG__", json.dumps(cfg, indent=2, sort_keys=True))
            .replace("__OPTIONS__", json.dumps(options, indent=2, sort_keys=True)))


def parse_logs(text: str) -> tuple[dict | None, list[dict]]:
    """(summary JSON or None, reservoir records) from a saved ``kubectl logs``."""
    summary, records, buf, inside = None, [], [], False
    for line in text.splitlines():
        if line == SUMMARY_BEGIN:
            inside, buf = True, []
        elif line == SUMMARY_END and inside:
            inside = False
            try:
                summary = json.loads("\n".join(buf))
            except json.JSONDecodeError:
                summary = None
        elif inside:
            buf.append(line)
        elif line.startswith(RESERVOIR_PREFIX):
            try:
                records.append(json.loads(line[len(RESERVOIR_PREFIX):]))
            except json.JSONDecodeError:
                continue
    return summary, records


def cell_counts(summary: dict) -> dict[str, int]:
    """Threshold key -> count, from ``handleSummary`` data (missing = absent)."""
    out = {}
    for key, metric in (summary.get("metrics") or {}).items():
        if key.startswith("soak_requests{"):
            values = metric.get("values") or {}
            out[key] = int(values.get("count", 0))
    return out
