"""Parity fixture runner (ADR-008, specs/config-contract-v0.3/design.md D2).

Each case in ``parity/cases.json`` is one test id. Status ``pass`` must pass;
``xfail:<task>`` must fail on a behavior mismatch (strict: an unexpected pass
fails the suite, so the task that fixes the behavior flips the status);
``skip:<reason>`` is skipped. Fixture or adapter errors (unknown kind, expect
key outside the snapshot schema, unsupported code key, driver crash) fail hard
whatever the status. A key ``python@sdk>=X.Y`` overrides ``python`` when the
installed ``opentelemetry-sdk`` is at least X.Y (highest matching key wins).

Every case runs in its own interpreter (``parity_driver.py``): OTel globals are
set-once per process. Only the selected ids run (``-k`` works); non-export
cases run concurrently, then export cases one at a time (they share the fake
receiver's request log). Format and run instructions: ``parity/README.md``.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_DRIVER = _HERE / "parity_driver.py"
_CASES_PATH = Path(os.environ.get("PARITY_CASES") or _HERE.parents[1] / "parity" / "cases.json")
_CHILD_TIMEOUT_S = 120  # export cases may wait up to 75 s for a long-timeout export

# Snapshot schema per kind (mirrors go/parity_match_test.go).
_COMMON_EXPECT = {"error", "warning", "warning_count", "log", "diagnostics", "helper_log", "console"}
_CASE_KEYS = {"id", "kind", "requirement", "note", "env", "code", "input", "expect", "status"}
_REQUIRED_KEYS = {"id", "kind", "env", "code", "expect", "status"}  # same set as go/parity_match_test.go
_OVERRIDE_RE = re.compile(r"^python@sdk>=[\d.]+$")
_SCHEMA = {
    "resolve": {"resource", "metric_exporters", "metric_export_interval_ms", "log_level"},
    "export": {"received"},
    "sampler": {"sampled", "attributes"},
    "propagation": {"headers"},
    "http_server": {"server_spans", "requests_counted", "handler_child_spans"},
    "grpc_server": {"server_spans", "rpcs_counted", "call_ok"},
    "logging": set(),
    "metrics_scrape": {"scrape", "scrape_status"},
    "fork": {"instance_ids", "instance_ids_distinct"},
}
_INPUT_SCHEMA = {
    "sampler": {"scenario"},
    "http_server": {"requests", "traceparent", "child_span"},
    "grpc_server": {"method"},
}
_SIGNALS = {"traces", "metrics", "logs"}
_RECORD_FIELDS = {"proto", "tls", "port", "path", "raw_path", "encoding", "headers", "timeout_ms", "absent"}
_MATCHER_KEYS = {"prefix", "regex", "absent", "not_contains", "all", "lte", "gte"}
_STATUS_RE = re.compile(r"^(pass|xfail:[A-Za-z0-9.-]+|skip:[A-Za-z0-9._-]+)$")


def _load_cases() -> list[dict]:
    with open(_CASES_PATH, encoding="utf-8") as fh:
        return json.load(fh)["cases"]


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


def _python_status(case: dict) -> str:
    """``python``, overridden by the highest ``python@sdk>=X.Y`` key that matches."""
    sdk = _version_tuple(version("opentelemetry-sdk"))
    best: tuple[tuple[int, ...], str] | None = None
    for key, value in case["status"].items():
        m = re.fullmatch(r"python@sdk>=([\d.]+)", key)
        if m and sdk >= _version_tuple(m.group(1)) and (best is None or _version_tuple(m.group(1)) > best[0]):
            best = (_version_tuple(m.group(1)), value)
    return best[1] if best else case["status"]["python"]


def _check_matchers(path: str, v) -> list[str]:
    """Objects that contain a matcher op may contain only matcher ops."""
    errs = []
    if isinstance(v, dict):
        if any(k in _MATCHER_KEYS for k in v):
            errs += [f"{path}: {k!r} is not a matcher op" for k in v if k not in _MATCHER_KEYS]
            if "all" in v:
                if not isinstance(v["all"], list):
                    errs.append(f"{path}.all must be a list")
                else:
                    for i, sub in enumerate(v["all"]):
                        errs += _check_matchers(f"{path}.all[{i}]", sub)
            errs += [f"{path}.{op} must be a number" for op in ("lte", "gte")
                     if op in v and (isinstance(v[op], bool) or not isinstance(v[op], (int, float)))]
            return errs
        for k, x in v.items():
            errs += _check_matchers(f"{path}.{k}", x)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            errs += _check_matchers(f"{path}[{i}]", x)
    return errs


def validate_case(case: dict) -> list[str]:
    """Fixture-format errors (not behavior mismatches); type-checks every asserted shape."""
    errs = [f"missing key {k!r}" for k in sorted(_REQUIRED_KEYS - case.keys())]
    errs += [f"unknown top-level key {k!r}" for k in sorted(case.keys() - _CASE_KEYS)]
    kind = case.get("kind")
    if kind not in _SCHEMA:
        return errs + [f"unknown kind {kind!r}"]
    expect = case.get("expect")
    if not isinstance(expect, dict):
        return errs + ["expect must be an object"]
    for key in expect:
        if key not in _SCHEMA[kind] | _COMMON_EXPECT:
            errs.append(f"expect key {key!r} is not in the {kind} snapshot schema")
    inp = case.get("input") or {}
    for key, val in inp.items():
        if key not in _INPUT_SCHEMA.get(kind, set()):
            errs.append(f"input key {key!r} is not valid for kind {kind}")
        elif key == "requests" and not (isinstance(val, list) and all(isinstance(x, str) for x in val)):
            errs.append("input.requests must be a list of strings")
        elif key in ("method", "scenario", "traceparent") and not isinstance(val, str):
            errs.append(f"input.{key} must be a string")
        elif key == "child_span" and not isinstance(val, bool):
            errs.append("input.child_span must be a boolean")
    wc = expect.get("warning_count")
    if wc is not None and not (isinstance(wc, dict) and all(isinstance(n, (int, float)) and not isinstance(n, bool)
                                                         for n in wc.values())):
        errs.append("warning_count must map substrings to numbers")
    w = expect.get("warning")
    if w is not None and not (isinstance(w, str) or (isinstance(w, list) and all(isinstance(x, str) for x in w))):
        errs.append("warning must be a string or list of strings")
    rec = expect.get("received")
    if rec is not None:
        if not isinstance(rec, dict):
            errs.append("expect.received must be an object")
        else:
            for sig, want in rec.items():
                if sig not in _SIGNALS:
                    errs.append(f"received signal {sig!r} unknown")
                if not isinstance(want, dict):
                    errs.append(f"received.{sig} must be an object")
                    continue
                errs += [f"received.{sig} field {f!r} unknown" for f in want if f not in _RECORD_FIELDS]
    errs += _check_matchers("expect", expect)
    status = case.get("status") if isinstance(case.get("status"), dict) else {}
    for lang in ("go", "python", "dotnet", "nodejs"):
        if not _STATUS_RE.match(str(status.get(lang, ""))):
            errs.append(f"status.{lang} invalid")
    for key, val in status.items():
        if key not in ("go", "python", "dotnet", "nodejs") and not (_OVERRIDE_RE.match(key) and _STATUS_RE.match(str(val))):
            errs.append(f"status override {key!r}: {val!r} invalid")

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "regex" and isinstance(x, str):
                    try:
                        re.compile(x)
                    except re.error as exc:
                        errs.append(f"regex {x!r} does not compile: {exc}")
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(expect)
    return errs


# ── receiver / environment ────────────────────────────────────────────────

def _receiver() -> dict | None:
    host = os.environ.get("FAKEOTLP_HOST")
    if not host:
        return None
    return {"{receiver_ipv6}": os.environ.get("FAKEOTLP_HOST_IPV6", ""),
            "{receiver_tls}": os.environ.get("FAKEOTLP_HOST_TLS", ""),
            "{receiver_ca}": os.environ.get("FAKEOTLP_CA", ""),
            "{receiver}": host}


class _NeedsReceiver(Exception):
    """A placeholder the environment can't fill; the message names the variable."""


_PLACEHOLDER_ENV = {"{receiver_ipv6}": "FAKEOTLP_HOST_IPV6", "{receiver_tls}": "FAKEOTLP_HOST_TLS"}


def _expand(value, recv):
    if isinstance(value, str):
        if "{receiver" not in value:
            return value
        if recv is None:
            raise _NeedsReceiver("FAKEOTLP_HOST")
        for placeholder, var in _PLACEHOLDER_ENV.items():
            if placeholder in value and not recv[placeholder]:
                raise _NeedsReceiver(var)
        for placeholder, real in recv.items():  # specific placeholders before "{receiver}"
            value = value.replace(placeholder, real)
        return value
    if isinstance(value, list):
        return [_expand(v, recv) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, recv) for k, v in value.items()}
    return value


def _dest_ips(recv) -> str:
    """Receiver addresses; records sent to other local addresses are ignored."""
    ips = set()
    for key in ("{receiver}", "{receiver_tls}", "{receiver_ipv6}"):
        host = recv[key].strip("[]")
        if host:
            ips |= {info[4][0] for info in socket.getaddrinfo(host, None)}
    return ",".join(sorted(ips))


def _child_env(case: dict, recv) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("OTEL_") or k in ("SERVICE_NAME", "ENVIRONMENT"))}
    env["OTEL_HELPER_METRICS_PORT"] = "0"  # never bind :9464 in tests; cases may override
    env.update(_expand(case.get("env") or {}, recv))
    if case["kind"] == "export":
        if recv is None:
            raise _NeedsReceiver("FAKEOTLP_HOST")
        env["FAKEOTLP_DEST_IPS"] = _dest_ips(recv)
    return env


# ── execution ─────────────────────────────────────────────────────────────

def _run_case(case: dict) -> dict:
    recv = _receiver()
    try:
        env = _child_env(case, recv)
        prepared = dict(case, code=_expand(case.get("code") or {}, recv))
    except _NeedsReceiver as exc:
        return {"_needs": str(exc)}
    cov_data = os.environ.get("PARITY_COVERAGE_DATA")
    if cov_data:
        env["PARITY_COVERAGE_DATA"] = cov_data
    with tempfile.TemporaryDirectory() as tmp:
        case_file, out_file = Path(tmp, "case.json"), Path(tmp, "out.json")
        case_file.write_text(json.dumps(prepared), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(_DRIVER), str(case_file), str(out_file)],
            env=env, capture_output=True, text=True, timeout=_CHILD_TIMEOUT_S, check=False,
        )
        if not out_file.exists():
            return {"_crash": f"driver crashed (rc={proc.returncode}):\n{proc.stderr[-3000:]}"}
        snap = json.loads(out_file.read_text(encoding="utf-8"))
    console = proc.stdout + proc.stderr
    snap["console"] = console
    snap["log"] = "\n".join(filter(None, [snap.get("diagnostics", ""), console]))
    return snap


def _safe(case: dict) -> tuple[str, dict]:
    try:
        return case["id"], _run_case(case)
    except Exception as exc:  # noqa: BLE001 - reported as a hard failure by the test
        return case["id"], {"_crash": f"{type(exc).__name__}: {exc}"}


@pytest.fixture(scope="session")
def parity_results(request) -> dict[str, dict]:
    """Run the selected cases: non-export in parallel, then export one at a time."""
    selected = [item.callspec.params["case"] for item in request.session.items
                if getattr(item, "originalname", "") == "test_parity" and hasattr(item, "callspec")]
    todo = [c for c in selected if not _python_status(c).startswith("skip:") and not validate_case(c)]
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2) * 2)) as pool:
        for cid, res in pool.map(_safe, [c for c in todo if c["kind"] != "export"]):
            results[cid] = res
    for case in (c for c in todo if c["kind"] == "export"):  # after the pool drained
        cid, res = _safe(case)
        results[cid] = res
    return results


# ── matching (mirrors go/parity_match_test.go) ────────────────────────────

def _norm(v):
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    return v


def _match_value(path: str, want, got, has: bool) -> list[str]:
    if not isinstance(want, dict):
        if not has:
            return [f"{path}: want {want!r}, got <absent>"]
        return [] if _norm(want) == _norm(got) else [f"{path}: want {want!r}, got {got!r}"]
    if not any(k in _MATCHER_KEYS for k in want):
        gd = got if isinstance(got, dict) else {}
        return [m for k in sorted(want) for m in _match_value(f"{path}.{k}", want[k], gd.get(k), k in gd)]
    out = []
    text = "" if got is None else str(got)
    for op in sorted(want):
        arg = want[op]
        if op == "absent":
            if arg is True and has:
                out.append(f"{path}: want absent, got {got!r}")
            if arg is False and not has:
                out.append(f"{path}: want present")
        elif op == "prefix" and (not has or not text.startswith(arg)):
            out.append(f"{path}: want prefix {arg!r}, got {got!r}")
        elif op == "regex":
            try:
                if not has or not re.search(arg, text):
                    out.append(f"{path}: want /{arg}/, got {text[:300]!r}")
            except re.error as exc:
                out.append(f"{path}: bad regex {arg!r}: {exc}")
        elif op == "not_contains" and has and arg in text:
            out.append(f"{path}: must not contain {arg!r}")
        elif op in ("lte", "gte"):
            num = isinstance(got, (int, float)) and not isinstance(got, bool)
            if not has or not num or (op == "lte" and got > arg) or (op == "gte" and got < arg):
                out.append(f"{path}: want {op} {arg}, got {got!r}")
        elif op == "all":
            for sub in arg:
                out += _match_value(path, sub, got, has)
    return out


def _match_received(want: dict, received) -> list[str]:
    received = received or {}
    out = []
    for sig in sorted(want):
        w = want[sig]
        recs = received.get(sig) or []
        if w.get("absent") is True:
            if recs:
                out.append(f"received.{sig}: want none, got {recs}")
            continue
        if not recs:
            out.append(f"received.{sig}: nothing received (all: {received})")
            continue
        for i, rec in enumerate(recs):
            for field in sorted(w):
                out += _match_value(f"received.{sig}[{i}].{field}", w[field], rec.get(field), field in rec)
    return out


def match_expect(expect: dict, snap: dict) -> list[str]:
    out = []
    if "error" not in expect and "error" in snap:
        out.append(f"unexpected error: {snap['error']}")
    for key in sorted(expect):
        want = expect[key]
        if key == "error":
            if isinstance(want, str):
                if "error" not in snap or want not in str(snap["error"]):
                    out.append(f"error: want substring {want!r}, got {snap.get('error')!r}")
            else:
                out += _match_value("error", want, snap.get("error"), "error" in snap)
        elif key == "warning":
            log = snap.get("log", "")
            for s in [want] if isinstance(want, str) else want:
                if s not in log:
                    out.append(f"warning: no diagnostic contains {s!r} (log: {log[:300]!r})")
        elif key == "warning_count":
            lines = (snap.get("diagnostics") or "").split("\n")
            for sub, n in sorted(want.items()):
                got = sum(1 for line in lines if sub in line)
                if got != n:
                    out.append(f"warning_count[{sub!r}]: want {n}, got {got}")
        elif key == "received":
            out += _match_received(want, snap.get("received"))
        else:
            out += _match_value(key, want, snap.get(key), key in snap)
    return out


# ── tests ─────────────────────────────────────────────────────────────────

_CASES = _load_cases()


def _param(case: dict):
    status = _python_status(case)
    marks = []
    if status.startswith("skip:"):
        marks.append(pytest.mark.skip(reason=status))
    elif status.startswith("xfail:"):
        # raises=AssertionError: only a behavior mismatch satisfies the xfail;
        # fixture/adapter errors use pytest.fail and still fail the build.
        marks.append(pytest.mark.xfail(strict=True, raises=AssertionError, reason=status))
    return pytest.param(case, id=case["id"], marks=marks)


@pytest.mark.parametrize("case", [_param(c) for c in _CASES])
def test_parity(case: dict, parity_results: dict) -> None:
    errs = validate_case(case)
    if errs:
        pytest.fail("fixture error (fails regardless of status): " + "; ".join(errs))
    snap = parity_results[case["id"]]
    if "_crash" in snap:
        pytest.fail(snap["_crash"])
    if "_needs" in snap:
        if os.environ.get("PARITY_REQUIRE_FAKEOTLP"):
            pytest.fail(f"PARITY_REQUIRE_FAKEOTLP is set but {snap['_needs']} is not: export cases must run")
        pytest.skip(f"case needs the fake OTLP receiver ({snap['_needs']} unset; see testing/README.md)")
    if "adapter_error" in snap:
        pytest.fail(f"adapter error (fails regardless of status): {snap['adapter_error']}")
    mismatches = match_expect(case["expect"], snap)
    assert not mismatches, "\n  ".join([f"parity case {case['id']} failed:"] + mismatches)


@pytest.mark.skipif(not os.environ.get("PARITY_RELEASE"), reason="set PARITY_RELEASE=1 for the release gate")
def test_no_task_skips_at_release() -> None:
    """A release build fails while any case is still skipped pending a task ("skip:A…")."""
    pending = [f"{c['id']}: {k}={v}" for c in _CASES for k, v in c["status"].items() if str(v).startswith("skip:A")]
    assert not pending


def test_fixture_is_well_formed() -> None:
    ids = [c["id"] for c in _CASES]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    problems = {c["id"]: validate_case(c) for c in _CASES}
    assert not {k: v for k, v in problems.items() if v}


class TestHarness:
    """The matcher and schema are test infrastructure; pin their semantics."""

    def test_operators(self):
        snap = {"a": "unknown_service:python", "n": 3, "log": "x OTEL_FOO y", "diagnostics": "OTEL_FOO\nOTEL_FOO"}
        assert match_expect({"a": {"prefix": "unknown_service"}, "n": 3, "warning": "OTEL_FOO"}, snap) == []
        assert match_expect({"b": {"absent": True}}, snap) == []
        assert match_expect({"a": {"all": [{"regex": "python$"}, {"not_contains": "go"}]}}, snap) == []
        assert match_expect({"warning_count": {"OTEL_FOO": 2}}, snap) == []
        assert match_expect({"warning_count": {"OTEL_FOO": 1}}, snap)
        assert match_expect({"a": {"not_contains": "python"}}, snap)
        assert match_expect({"a": {"regex": "("}}, snap)  # bad regex → mismatch, not a crash
        assert match_expect({"n": 4}, snap)

    def test_unexpected_error_fails_unless_expected(self):
        assert match_expect({}, {"error": "boom"}) == ["unexpected error: boom"]
        assert match_expect({"error": "boo"}, {"error": "boom"}) == []
        assert match_expect({"error": {"absent": True}}, {"error": "boom"})

    def test_received(self):
        snap = {"received": {"traces": [{"port": 4318, "path": "/v1/traces"}], "metrics": []}}
        assert match_expect({"received": {"traces": {"port": 4318}, "metrics": {"absent": True}}}, snap) == []
        assert match_expect({"received": {"metrics": {}}}, snap)
        assert match_expect({"received": {"traces": {"port": 443}}}, snap)

    def test_schema_rejects_fixture_typos(self):
        base = {"id": "x", "env": {}, "code": {},
                "status": {"go": "pass", "python": "pass", "dotnet": "xfail:phase-c", "nodejs": "skip:phase-b"}}
        assert validate_case({**base, "kind": "bogus", "expect": {}})
        assert validate_case({**base, "kind": "resolve", "expect": {"resorce": {}}})
        assert validate_case({**base, "kind": "export", "expect": {"received": {"trace": {}}}})
        assert not validate_case({**base, "kind": "resolve", "expect": {"resource": {}}})
        assert validate_case({**base, "kind": "resolve", "expect": {"log": {"regex": "x", "contians": "y"}}})
        assert validate_case({**base, "kind": "export", "expect": {"received": "nope"}})
        assert validate_case({**base, "kind": "resolve", "expect": {"warning_count": {"x": "1"}}})
        assert validate_case({**base, "kind": "resolve", "expect": {}, "stauts": {}})
        assert validate_case({**base, "kind": "resolve", "expect": {},
                              "status": {**base["status"], "python@sdk>=x": "pass"}})
        assert validate_case({k: v for k, v in base.items() if k != "env"} | {"kind": "resolve", "expect": {}})
        assert match_expect({"t": {"lte": 500, "gte": 1}}, {"t": 499}) == []
        assert match_expect({"t": {"lte": 500}}, {"t": 9999})

    def test_highest_sdk_override_wins(self, monkeypatch):
        monkeypatch.setattr(sys.modules[__name__], "version", lambda _: "1.46.0")
        case = {"status": {"python": "xfail:A3", "python@sdk>=1.45": "pass", "python@sdk>=1.46": "xfail:A4"}}
        assert _python_status(case) == "xfail:A4"
