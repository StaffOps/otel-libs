"""Soak K-D2 FailedScheduling watch for the demo (specs/demo-environment DE-D12).

A demo run must never push a platform pod out of the node. This reads,
read-only, the FailedScheduling events of every namespace and reports a hit
when an event:
  - is about a Pod outside the demo namespace,
  - is newer than the run start (--since, RFC 3339 UTC),
  - says "Too many pods", "Insufficient cpu" or "Insufficient memory",
  - and its pod still exists and is Pending.
demo.sh scales the run's app Deployments to 0 on a hit.

Usage: python3 -I schedwatch.py --context k8s01 --namespace dev-otel-libs --since 2026-11-16T18:00:00Z
Exit:  0 no hit, 10 hit(s), 2 error (the watch could not look: kubectl failed,
       timed out after 60 s, or returned something unreadable). Prints namespace/name and the matched
       phrase only (never the event message). Standard library only; it calls
       `kubectl get` (never writes).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone

PHRASES = ("Too many pods", "Insufficient cpu", "Insufficient memory")
EXIT_HIT = 10
EXIT_BLIND = 2
KUBECTL_TIMEOUT_S = 60


def parse_time(text: str | None) -> datetime | None:
    """Parse a Kubernetes RFC 3339 timestamp (Z or offset, optional fraction)."""
    if not text:
        return None
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def event_time(event: dict) -> datetime | None:
    """The newest timestamp an event carries (core/v1 or events.k8s.io shapes)."""
    series = event.get("series") or {}
    candidates = [
        event.get("lastTimestamp"),
        series.get("lastObservedTime"),
        event.get("eventTime"),
        event.get("firstTimestamp"),
        (event.get("metadata") or {}).get("creationTimestamp"),
    ]
    times = [t for t in (parse_time(c) for c in candidates) if t is not None]
    return max(times) if times else None


def hits(events: dict, pending: dict, namespace: str, since: datetime) -> list[tuple[str, str]]:
    """(namespace/name, phrase) for every event that matches the K-D2 scope."""
    still_pending = {
        f"{(p.get('metadata') or {}).get('namespace')}/{(p.get('metadata') or {}).get('name')}"
        for p in pending.get("items", [])
        if (p.get("status") or {}).get("phase") == "Pending"
    }
    found: dict[str, str] = {}
    for event in events.get("items", []):
        if event.get("reason") != "FailedScheduling":
            continue
        obj = event.get("involvedObject") or event.get("regarding") or {}
        if obj.get("kind") != "Pod":
            continue
        obj_ns = obj.get("namespace") or (event.get("metadata") or {}).get("namespace")
        if not obj_ns or obj_ns == namespace:
            continue
        when = event_time(event)
        if when is None or when < since:
            continue
        message = event.get("message") or event.get("note") or ""
        phrase = next((p for p in PHRASES if p in message), None)
        if phrase is None:
            continue
        key = f"{obj_ns}/{obj.get('name')}"
        if key in still_pending:
            found.setdefault(key, phrase)
    return sorted(found.items())


def kubectl_json(context: str, *args: str) -> dict:
    out = subprocess.run(
        ["kubectl", "--context", context, "get", *args, "-o", "json"],
        check=True, capture_output=True, text=True, timeout=KUBECTL_TIMEOUT_S,
    )
    return json.loads(out.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--since", required=True)
    args = parser.parse_args(argv)
    since = parse_time(args.since)
    if since is None:
        print(f"schedwatch: bad --since {args.since!r}", file=sys.stderr)
        return EXIT_BLIND
    try:
        events = kubectl_json(args.context, "events", "--all-namespaces",
                              "--field-selector", "reason=FailedScheduling")
        pending = kubectl_json(args.context, "pods", "--all-namespaces",
                               "--field-selector", "status.phase=Pending")
        found = hits(events, pending, args.namespace, since)
    except Exception as exc:  # noqa: BLE001 — any failure means the watch is blind
        # The type name only: kubectl's stderr or an event could carry anything.
        print(f"schedwatch: {type(exc).__name__}", file=sys.stderr)
        return EXIT_BLIND
    for key, phrase in found:
        print(f"schedwatch: FailedScheduling ({phrase}) for {key}")
    return EXIT_HIT if found else 0


if __name__ == "__main__":
    sys.exit(main())
