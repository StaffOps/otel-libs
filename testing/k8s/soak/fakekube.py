"""A fake ``kubectl`` (and ``demo.sh``) for the soak loop's tests and dry run.

Never talks to a cluster. State is a JSON file (``FAKEKUBE_STATE``):
``{"objects": [...], "logs": {"job/<name>": "..."}, "ztunnel_log": "...",
"raw": {"<path prefix>": <JSON answer>}, "fail": {"<verb>": "<stderr>"}}``.
Every call is appended to ``FAKEKUBE_LOG`` as one JSON line
``{"argv": [...], "stdin": "...", "env": {...}}``.

Supported: ``get <kinds>[ <name>] [-l <selector>] [-A] -o json``,
``get --raw <url>``, ``apply ... -f -`` (a JSON object or List on stdin),
``delete <kinds> -l <selector>``, ``label namespace <ns> k=v --overwrite``,
``logs job/<name>`` and ``logs -l <selector>``, ``patch job <name> -p <json>``.
``fakekube.py demo-sh <args>`` stands for ``testing/k8s/demo/demo.sh``: it only
records the call (argv and the TAG / LANGS / RUN / DEMO_ENV_FILE env).
"""

from __future__ import annotations

import json
import os
import sys

KINDS = {
    "jobs": "Job", "job": "Job", "configmaps": "ConfigMap", "configmap": "ConfigMap",
    "services": "Service", "service": "Service", "pods": "Pod", "pod": "Pod",
    "networkpolicies": "NetworkPolicy", "deployments": "Deployment",
    "namespace": "Namespace", "namespaces": "Namespace",
}


def _load() -> dict:
    path = os.environ["FAKEKUBE_STATE"]
    with open(path) as fh:
        return json.load(fh)


def _save(state: dict) -> None:
    with open(os.environ["FAKEKUBE_STATE"], "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)


def _record(argv: list[str], stdin: str) -> None:
    env = {k: os.environ[k] for k in ("TAG", "LANGS", "RUN", "DEMO_ENV_FILE", "DEMO_RESTORE", "KUBE_CONTEXT", "NAMESPACE")
           if k in os.environ}
    with open(os.environ["FAKEKUBE_LOG"], "a") as fh:
        fh.write(json.dumps({"argv": argv, "stdin": stdin, "env": env}) + "\n")


def matches(labels: dict, selector: str) -> bool:
    """``k=v``, ``k!=v``, ``k`` and ``!k`` terms, comma-separated (all must hold)."""
    for term in filter(None, selector.split(",")):
        if "!=" in term:
            k, v = term.split("!=", 1)
            if labels.get(k) == v:
                return False
        elif "=" in term:
            k, v = term.split("=", 1)
            if labels.get(k) != v:
                return False
        elif term.startswith("!"):
            if term[1:] in labels:
                return False
        elif term not in labels:
            return False
    return True


def _opts(argv: list[str]) -> tuple[list[str], dict]:
    """Positional args and the options that take a value."""
    pos, opts, i = [], {}, 0
    takes = {"--context", "-n", "--namespace", "-l", "-o", "-p", "--type", "-f", "--since",
             "--tail", "--timeout", "--field-manager", "--max-log-requests", "--cascade",
             "--field-selector"}
    while i < len(argv):
        a = argv[i]
        if a in takes and i + 1 < len(argv):
            opts[a] = argv[i + 1]
            i += 2
            continue
        if a.startswith("--") and "=" in a:
            k, v = a.split("=", 1)
            opts[k] = v
        elif a.startswith("-"):
            opts[a] = True
        else:
            pos.append(a)
        i += 1
    return pos, opts


def _ns(opts: dict) -> str | None:
    if opts.get("-A"):
        return None
    return opts.get("-n") or opts.get("--namespace") or "default"


def _select(state: dict, kinds: list[str], ns: str | None, sel: str, name: str | None = None) -> list[dict]:
    out = []
    for obj in state["objects"]:
        meta = obj.get("metadata") or {}
        if obj.get("kind") not in kinds:
            continue
        if obj.get("kind") != "Namespace" and ns is not None and meta.get("namespace") != ns:
            continue
        if name is not None and meta.get("name") != name:
            continue
        if sel and not matches(meta.get("labels") or {}, sel):
            continue
        out.append(obj)
    return out


def _kinds(spec: str) -> list[str]:
    try:
        return [KINDS[k] for k in spec.split(",")]
    except KeyError as exc:
        print(f'error: the server doesn\'t have a resource type "{exc.args[0]}"', file=sys.stderr)
        sys.exit(1)


def main(argv: list[str]) -> int:
    stdin = "" if sys.stdin.isatty() else sys.stdin.read()
    _record(argv, stdin)
    if argv and argv[0] == "demo-sh":
        return 0
    state = _load()
    pos, opts = _opts(argv)
    verb = pos[0] if pos else ""
    fail = (state.get("fail") or {}).get(verb)
    if fail:
        print(fail, file=sys.stderr)
        return 1
    ns = _ns(opts)
    if verb == "get" and "--raw" in opts:
        url = opts["--raw"] if isinstance(opts["--raw"], str) else pos[1]
        for prefix, answer in (state.get("raw") or {}).items():
            if url.startswith(prefix):
                print(json.dumps(answer))
                return 0
        print(f"Error from server (NotFound): {url}", file=sys.stderr)
        return 1
    if verb == "get":
        kinds = _kinds(pos[1])
        name = pos[2] if len(pos) > 2 else None
        items = _select(state, kinds, ns, opts.get("-l", ""), name)
        if name is not None:
            if not items:
                print(f'Error from server (NotFound): {pos[1]} "{name}" not found', file=sys.stderr)
                return 1
            print(json.dumps(items[0]))
        else:
            print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
        return 0
    if verb == "apply":
        doc = json.loads(stdin)
        objs = doc["items"] if doc.get("kind") == "List" else [doc]
        for obj in objs:
            meta = obj["metadata"]
            state["objects"] = [o for o in state["objects"]
                                if not (o["kind"] == obj["kind"] and o["metadata"]["name"] == meta["name"]
                                        and o["metadata"].get("namespace") == meta.get("namespace"))]
            state["objects"].append(obj)
            print(f'{obj["kind"].lower()}/{meta["name"]} serverside-applied')
        _save(state)
        return 0
    if verb == "delete":
        kinds = _kinds(pos[1])
        sel = opts.get("-l")
        if not sel:
            print("error: the fake refuses a delete without a label selector", file=sys.stderr)
            return 1
        gone = _select(state, kinds, ns, sel)
        field = opts.get("--field-selector")
        if field:
            gone = [o for o in gone if f'metadata.name={o["metadata"]["name"]}' == field]
        state["objects"] = [o for o in state["objects"] if o not in gone]
        _save(state)
        for o in gone:
            print(f'{o["kind"].lower()} "{o["metadata"]["name"]}" deleted')
        return 0
    if verb == "label":
        _, kind, name, *pairs = pos
        for obj in _select(state, _kinds(kind), None, "", name):
            for pair in pairs:
                k, v = pair.split("=", 1)
                obj["metadata"].setdefault("labels", {})[k] = v
        _save(state)
        return 0
    if verb == "logs":
        if len(pos) > 1:
            text = (state.get("logs") or {}).get(pos[1])
            if text is None:
                print(f"error: {pos[1]} not found", file=sys.stderr)
                return 1
            sys.stdout.write(text)
            return 0
        sys.stdout.write(state.get("ztunnel_log", ""))
        return 0
    if verb == "patch":
        for obj in _select(state, _kinds(pos[1]), ns, "", pos[2]):
            obj.setdefault("spec", {}).update(json.loads(opts["-p"]).get("spec", {}))
        _save(state)
        return 0
    print(f"fakekube: unsupported call {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
