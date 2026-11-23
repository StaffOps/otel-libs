"""Manifests of every soak-run object (specs/soak-simulation task K-loop).

Each renderer returns one Kubernetes object as a dict (applied as JSON, so no
YAML library is needed). Every object, and every pod template, carries only the
soak labels ``otel-libs/soak-run=<run>`` and ``otel-libs/soak-role=<role>``:
never the demo's ``app.kubernetes.io/component=demo`` / ``otel-libs/run``, so
``demo.sh delete`` cannot remove a soak object and the trap's selector
(``otel-libs/soak-run=<run>``) cannot remove a demo one (design K-D15).

Objects (SR15 r6.2 soak-run writes): the k6 ConfigMap and Job; the
``soak-otlp`` Service in its variants (copy, refused, hung, redirect); the
``testing/fakeotlp`` sink pods (hung, redirect) and their Services; the
telemetrygen positive-control Job; the SR6 fallback Service.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from soakplatform import RUN_RE  # noqa: E402  (one run-id rule for K1 and the loop)

RUN_LABEL = "otel-libs/soak-run"
ROLE_LABEL = "otel-libs/soak-role"
NAMESPACE = "dev-otel-libs"
LANGS = ("go", "python", "node", "dotnet")

# docker.io/grafana/k6 2.3.0, multi-arch index digest read 2026-11-23 with
# `docker buildx imagetools inspect` (pushed 2026-10-24; 2.4.0 of 2026-11-12 is
# under two weeks old).
K6_IMAGE = ("docker.io/grafana/k6:2.3.0"
            "@sha256:2dd4756ec6e14e62778916e1eb850108e4b499304c02df56760fa0bdb0fba0b8")
# The k6 image's own non-root user (uid of user `k6`).
K6_UID = 12345
# telemetrygen v0.165.0 (the copy runs contrib 0.165.0), index digest read
# 2026-11-23 (image created 2026-11-07).
TELEMETRYGEN_IMAGE = ("ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen:v0.165.0"
                      "@sha256:fbdd37913c76a2c69774b442e04bf980968c7c8df12877027d3d06b4851e81c9")
# testing/fakeotlp at ab24d24 (K-hang), built by testing/k8s/build-fakeotlp.sh and
# pushed by K1 on 2026-11-23; digest recorded in the K1 snapshot.
FAKEOTLP_IMAGE = ("harbor.staffops.space/development/otel-libs-fakeotlp:ab24d24"
                  "@sha256:08ac9e9f22efa2554a8f1470316e1f3ca4cfd8159c68ac643948db6c9fd00c41")
COPY_HOST = f"soak-agent.{NAMESPACE}.svc.cluster.local"
OTLP_PORTS = (("otlp-grpc", 4317), ("otlp-http", 4318))
FALLBACK_PORT = 9464  # Prometheus /metrics fallback of the helpers (ADR-007)

WINDOW_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?")
DIGEST_RE = re.compile(r"[a-z0-9.\-/:]+@sha256:[0-9a-f]{64}")
OTLP_VARIANTS = ("copy", "refused", "hung", "redirect")


class RenderError(ValueError):
    """A renderer was given a value that cannot make a valid object."""


def check_run(run: str) -> str:
    if not RUN_RE.fullmatch(run):
        raise RenderError(f"run id {run!r}: a label value, 1-63 of [a-z0-9-], starting and ending alphanumeric")
    return run


def labels(run: str, role: str) -> dict[str, str]:
    return {RUN_LABEL: check_run(run), ROLE_LABEL: role}


def _meta(name: str, run: str, role: str, **extra) -> dict:
    return {"name": name, "namespace": NAMESPACE, "labels": labels(run, role), **extra}


def _pod_security(uid: int) -> dict:
    return {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid,
            "seccompProfile": {"type": "RuntimeDefault"}}


def _container_security() -> dict:
    return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}}


def _pinned(image: str, what: str) -> str:
    if not DIGEST_RE.fullmatch(image):
        raise RenderError(f"{what} image {image!r} is not referenced by digest")
    return image


# ── k6 ──────────────────────────────────────────────────────────────────────

def k6_name(window: str) -> str:
    if not WINDOW_RE.fullmatch(window):
        raise RenderError(f"window {window!r}: 1-20 of [a-z0-9-]")
    return f"soak-k6-{window}"


def k6_configmap(run: str, window: str, script: str) -> dict:
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": _meta(k6_name(window), run, "k6"),
            "data": {"script.js": script}}


def k6_job(run: str, window: str, deadline_s: int, *, image: str = K6_IMAGE,
           cpu: str = "250m", memory: str = "256Mi", memory_limit: str = "1Gi") -> dict:
    """The k6 Job: one pod, never retried, bounded by ``activeDeadlineSeconds``.
    ``memory_limit``: the caller passes ``K6Plan.memory_limit_mi`` (from the VU count).

    No ``ttlSecondsAfterFinished``: the trap saves ``kubectl logs job/<name>``
    (summary and reservoir lines) before it deletes the Job.
    """
    if deadline_s < 1:
        raise RenderError("activeDeadlineSeconds must be at least 1")
    name = k6_name(window)
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": _meta(name, run, "k6"),
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline_s,
            "template": {
                "metadata": {"labels": labels(run, "k6")},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "terminationGracePeriodSeconds": 60,
                    "securityContext": _pod_security(K6_UID),
                    "containers": [{
                        "name": "k6",
                        "image": _pinned(image, "k6"),
                        # raw: console lines (the reservoir) are printed as-is.
                        "args": ["run", "--log-format=raw", "/scripts/script.js"],
                        "env": [{"name": "K6_NO_USAGE_REPORT", "value": "true"}],
                        "resources": {"requests": {"cpu": cpu, "memory": memory},
                                      "limits": {"memory": memory_limit}},
                        "securityContext": _container_security(),
                        "volumeMounts": [
                            {"name": "script", "mountPath": "/scripts", "readOnly": True},
                            {"name": "tmp", "mountPath": "/tmp"},
                        ],
                    }],
                    "volumes": [
                        {"name": "script", "configMap": {"name": name}},
                        {"name": "tmp", "emptyDir": {}},
                    ],
                },
            },
        },
    }


# ── soak-otlp and the fakeotlp sinks ────────────────────────────────────────

def _ports(pairs=OTLP_PORTS) -> list[dict]:
    return [{"name": n, "port": p, "targetPort": p, "protocol": "TCP"} for n, p in pairs]


def soak_otlp(run: str, variant: str) -> dict:
    """``soak-otlp``, the apps' one OTLP endpoint (design K-D14).

    copy     ExternalName to the GitOps copy ``soak-agent``;
    refused  a selector-less ClusterIP Service: no endpoints, so connects are
             refused (what ambient makes of it is ``unmeasured``);
    hung / redirect  ExternalName to the fakeotlp sink Service of that mode.
    A switch between ExternalName and ClusterIP is a delete and a create.
    """
    meta = _meta("soak-otlp", run, f"otlp-{variant}")
    if variant == "copy":
        spec = {"type": "ExternalName", "externalName": COPY_HOST}
    elif variant == "refused":
        spec = {"type": "ClusterIP", "ports": _ports()}
    elif variant in ("hung", "redirect"):
        spec = {"type": "ExternalName",
                "externalName": f"{sink_name(variant)}.{NAMESPACE}.svc.cluster.local"}
    else:
        raise RenderError(f"soak-otlp variant {variant!r}: one of {', '.join(OTLP_VARIANTS)}")
    return {"apiVersion": "v1", "kind": "Service", "metadata": meta, "spec": spec}


def sink_name(mode: str) -> str:
    if mode not in ("hung", "redirect"):
        raise RenderError(f"sink mode {mode!r}: hung or redirect")
    return f"soak-fakeotlp-{mode}"


def sink_pod(run: str, mode: str, image: str = FAKEOTLP_IMAGE, *, hang_max: int = 256) -> dict:
    """A ``testing/fakeotlp`` pod: ``--hang`` for *hung*, the default mode for
    *redirect* (its ``/parity-redirect-c/`` path answers 307 with the fixed
    token ``parity-redirect-token``). Both listeners take gRPC and HTTP; the TLS
    listener stays on loopback; the admin port serves ``GET /held``.
    """
    name = sink_name(mode)
    env = [
        {"name": "PLAIN", "value": ":4317,:4318"},
        {"name": "TLS", "value": "127.0.0.2:14317"},
        {"name": "ADMIN", "value": ":8888"},
        {"name": "OUT", "value": "/out"},
    ]
    if mode == "hung":
        env += [{"name": "HANG", "value": "true"}, {"name": "HANG_MAX", "value": str(hang_max)}]
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": _meta(name, run, f"sink-{mode}"),
        "spec": {
            "restartPolicy": "Always",
            "automountServiceAccountToken": False,
            "securityContext": _pod_security(65534),
            "containers": [{
                "name": "fakeotlp",
                "image": _pinned(image, "fakeotlp"),
                "env": env,
                "ports": [{"name": n, "containerPort": p} for n, p in OTLP_PORTS]
                         + [{"name": "admin", "containerPort": 8888}],
                "readinessProbe": {"tcpSocket": {"port": 4317}, "periodSeconds": 5},
                "resources": {"requests": {"cpu": "20m", "memory": "32Mi"},
                              "limits": {"memory": "128Mi"}},
                "securityContext": _container_security(),
                "volumeMounts": [{"name": "out", "mountPath": "/out"}],
            }],
            "volumes": [{"name": "out", "emptyDir": {}}],
        },
    }


def sink_service(run: str, mode: str) -> dict:
    name = sink_name(mode)
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": _meta(name, run, f"sink-{mode}"),
            "spec": {"type": "ClusterIP", "ports": _ports(),
                     "selector": labels(run, f"sink-{mode}")}}


# ── positive control and SR6 fallback ───────────────────────────────────────

CONTROL_SERVICE = "soak-control"


def control_instances(run: str) -> dict[str, str]:
    """The control's service.instance.id per container; "unset" for the one
    sent without it (the copy's count defaults the label to "unset")."""
    return {"rpc-system": f"{run}-ctl-a", "rpc-system-name": f"{run}-ctl-b",
            "no-instance": "unset"}


def control_job(run: str, *, image: str = TELEMETRYGEN_IMAGE,
                endpoint: str = "soak-otlp:4317") -> dict:
    """The fixed-family positive control (task K-copy): three telemetrygen
    containers, each one trace of one CLIENT span (``--child-spans 0``):
    one with ``rpc.system=grpc``, one with ``rpc.system.name=grpc`` (both with
    a ``service.instance.id``), and one with ``rpc.system=grpc`` and no
    ``service.instance.id``. Expected on the copy: ``otel_libs_soak_fixed_spans_total``
    +1 for each of the three ``service_instance_id`` values of
    ``control_instances`` and ``otel_libs_soak_spans_total{service_name="soak-control"}`` +3.
    That the root span is CLIENT is read from the delivery smoke (2 spans per
    trace, the server one always fixed); K-copy confirms it.
    """
    inst = control_instances(run)
    attrs = {"rpc-system": "rpc.system", "rpc-system-name": "rpc.system.name",
             "no-instance": "rpc.system"}
    containers = []
    for cname, key in attrs.items():
        args = ["traces", "--traces", "1", "--child-spans", "0", "--workers", "1",
                "--otlp-endpoint", endpoint, "--otlp-insecure",
                "--service", CONTROL_SERVICE,
                "--telemetry-attributes", f'{key}="grpc"',
                "--otlp-attributes", 'deployment.environment.name="dev"']
        if inst[cname] != "unset":
            args += ["--otlp-attributes", f'service.instance.id="{inst[cname]}"']
        containers.append({
            "name": cname, "image": _pinned(image, "telemetrygen"), "args": args,
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                          "limits": {"memory": "128Mi"}},
            "securityContext": _container_security(),
        })
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": _meta("soak-control", run, "control"),
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 300,
            "template": {
                "metadata": {"labels": labels(run, "control")},
                "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                         "securityContext": _pod_security(65532),
                         "containers": containers},
            },
        },
    }


def fallback_service(run: str, lang: str) -> dict:
    """SR6 "no endpoint → /metrics": a Service on one language's api port 9464,
    read through the API server's service proxy."""
    if lang not in LANGS:
        raise RenderError(f"language {lang!r}: one of {', '.join(LANGS)}")
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": _meta(f"soak-fallback-{lang}", run, "fallback"),
            "spec": {"type": "ClusterIP",
                     "selector": {"app.kubernetes.io/name": f"demo-{lang}-api"},
                     "ports": [{"name": "metrics", "port": FALLBACK_PORT,
                                "targetPort": FALLBACK_PORT, "protocol": "TCP"}]}}


def as_list(objs: list[dict]) -> dict:
    return {"apiVersion": "v1", "kind": "List", "items": objs}


def every_kind(run: str, fakeotlp_image: str = FAKEOTLP_IMAGE, script: str = "", deadline_s: int = 1,
               window: str = "dry") -> list[dict]:
    """One object of each renderer (the K-loop dry run): k6 ConfigMap + Job,
    ``soak-otlp`` (copy), both sink pods and Services, the control Job and one
    fallback Service."""
    return [
        k6_configmap(run, window, script),
        k6_job(run, window, deadline_s),
        soak_otlp(run, "copy"),
        sink_pod(run, "hung", fakeotlp_image),
        sink_service(run, "hung"),
        sink_pod(run, "redirect", fakeotlp_image),
        sink_service(run, "redirect"),
        control_job(run),
        fallback_service(run, "go"),
    ]
