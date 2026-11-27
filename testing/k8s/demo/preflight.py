"""Soak K-D2 requests-based pre-flight for the demo (specs/demo-environment DE-D12).

Reads, read-only, the schedulable Ready nodes' status.allocatable and the
requests of their non-terminated pods (init containers counted the way the
scheduler counts them: max of each regular init container vs the app
containers plus restartable sidecars; pod overhead added), then decides
whether the run's requests fit under allocatable - requested - reserve on at
least one node, for CPU, memory and pod slots.

Usage: python3 -I preflight.py --context k8s01 --cpu 150m --memory 320Mi --pods 2
       [--reserve-cpu 250m] [--reserve-memory 512Mi] [--reserve-pods 1]
Exit:  0 fits, 1 does not fit, 2 error. Prints numbers only.
Standard library only; it calls `kubectl get` (never writes).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation

_SUFFIX = {
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60,
    "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3"),
    "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18,
}
_QUANTITY = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)?$")


def quantity(text: str) -> Decimal:
    """Parse a Kubernetes resource quantity into base units (cores or bytes)."""
    match = _QUANTITY.match(str(text).strip())
    if not match:
        raise ValueError(f"bad quantity {text!r}")
    try:
        value = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ValueError(f"bad quantity {text!r}") from exc
    suffix = match.group(2)
    return value * Decimal(_SUFFIX[suffix]) if suffix else value


def _requests(container: dict) -> tuple[Decimal, Decimal]:
    req = (container.get("resources") or {}).get("requests") or {}
    return quantity(req.get("cpu", "0")), quantity(req.get("memory", "0"))


def pod_requests(spec: dict) -> tuple[Decimal, Decimal]:
    """Effective (cpu, memory) request of a pod spec, as the scheduler sums it."""
    app_cpu = app_mem = Decimal(0)
    for c in spec.get("containers") or []:
        cpu, mem = _requests(c)
        app_cpu, app_mem = app_cpu + cpu, app_mem + mem
    side_cpu = side_mem = init_cpu = init_mem = Decimal(0)
    for c in spec.get("initContainers") or []:
        cpu, mem = _requests(c)
        if c.get("restartPolicy") == "Always":  # sidecar: runs with the app
            side_cpu, side_mem = side_cpu + cpu, side_mem + mem
        else:
            init_cpu, init_mem = max(init_cpu, cpu + side_cpu), max(init_mem, mem + side_mem)
    overhead = spec.get("overhead") or {}
    cpu = max(app_cpu + side_cpu, init_cpu) + quantity(overhead.get("cpu", "0"))
    mem = max(app_mem + side_mem, init_mem) + quantity(overhead.get("memory", "0"))
    return cpu, mem


def kubectl_json(context: str, *args: str) -> dict:
    out = subprocess.run(
        ["kubectl", "--context", context, "get", *args, "-o", "json"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def node_free(nodes: dict, pods: dict) -> list[dict]:
    """Free cpu (cores), memory (bytes) and pod slots per schedulable Ready node."""
    used: dict[str, list] = {}
    for pod in pods.get("items", []):
        name = (pod.get("spec") or {}).get("nodeName")
        if not name:
            continue
        cpu, mem = pod_requests(pod["spec"])
        entry = used.setdefault(name, [Decimal(0), Decimal(0), 0])
        entry[0] += cpu
        entry[1] += mem
        entry[2] += 1
    result = []
    for node in nodes.get("items", []):
        if (node.get("spec") or {}).get("unschedulable"):
            continue
        conditions = (node.get("status") or {}).get("conditions") or []
        if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            continue
        alloc = (node.get("status") or {}).get("allocatable")
        name = (node.get("metadata") or {}).get("name", "?")
        if not alloc or not all(k in alloc for k in ("cpu", "memory", "pods")):
            raise ValueError(f"node {name} has no complete status.allocatable")
        cpu, mem, count = used.get(name, [Decimal(0), Decimal(0), 0])
        result.append({
            "node": name,
            "alloc_cpu": quantity(alloc["cpu"]), "req_cpu": cpu,
            "alloc_mem": quantity(alloc["memory"]), "req_mem": mem,
            "alloc_pods": int(alloc["pods"]), "pods": count,
        })
    return result


def fits(node: dict, cpu: Decimal, mem: Decimal, pods: int,
         reserve_cpu: Decimal, reserve_mem: Decimal, reserve_pods: int) -> bool:
    return (
        cpu <= node["alloc_cpu"] - node["req_cpu"] - reserve_cpu
        and mem <= node["alloc_mem"] - node["req_mem"] - reserve_mem
        and pods <= node["alloc_pods"] - node["pods"] - reserve_pods
    )


def _mi(value: Decimal) -> int:
    return int(value / 2**20)


def _m(value: Decimal) -> int:
    return int(value * 1000)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--context", required=True)
    parser.add_argument("--cpu", required=True)
    parser.add_argument("--memory", required=True)
    parser.add_argument("--pods", required=True, type=int)
    parser.add_argument("--reserve-cpu", default="250m")
    parser.add_argument("--reserve-memory", default="512Mi")
    parser.add_argument("--reserve-pods", default=1, type=int)
    args = parser.parse_args(argv)
    try:
        cpu, mem = quantity(args.cpu), quantity(args.memory)
        r_cpu, r_mem = quantity(args.reserve_cpu), quantity(args.reserve_memory)
        nodes = kubectl_json(args.context, "nodes")
        pods = kubectl_json(args.context, "pods", "--all-namespaces",
                            "--field-selector", "status.phase!=Succeeded,status.phase!=Failed")
    except (ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"preflight: {exc}", file=sys.stderr)
        return 2
    try:
        candidates = node_free(nodes, pods)
    except (ValueError, KeyError, TypeError) as exc:
        print(f"preflight: {exc}", file=sys.stderr)
        return 2
    if not candidates:
        print("preflight: no schedulable Ready node", file=sys.stderr)
        return 2
    ok = False
    for n in candidates:
        verdict = fits(n, cpu, mem, args.pods, r_cpu, r_mem, args.reserve_pods)
        ok = ok or verdict
        print(
            f"preflight: node={n['node']} cpu requested {_m(n['req_cpu'])}m/{_m(n['alloc_cpu'])}m "
            f"memory requested {_mi(n['req_mem'])}Mi/{_mi(n['alloc_mem'])}Mi "
            f"pods {n['pods']}/{n['alloc_pods']} | run {_m(cpu)}m {_mi(mem)}Mi {args.pods} pods "
            f"+ reserve {_m(r_cpu)}m {_mi(r_mem)}Mi {args.reserve_pods} pod -> "
            f"{'fits' if verdict else 'does not fit'}"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
