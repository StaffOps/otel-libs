"""Tests for the demo.sh app manifests and `apply --lang/--all` (task DE4).

`render` runs offline. The apply tests run demo.sh against a fake `kubectl`
on PATH (a shell script that answers the read calls and records the writes),
so no cluster is touched. Standard library unittest; needs bash.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO = HERE / "demo.sh"
LANGS = ("go", "python", "node", "dotnet")
APPS = ("worker", "api", "backend")
BUDGET = {"go": ("5m", "32Mi", "64Mi"), "python": ("15m", "96Mi", "192Mi"),
          "node": ("10m", "64Mi", "192Mi"), "dotnet": ("20m", "128Mi", "256Mi")}  # DE7
UID = {"go": "65534", "python": "65532", "node": "1000", "dotnet": "1654"}
HEX48 = re.compile(r"(?<![0-9a-f])[0-9a-f]{48}(?![0-9a-f])")


def run_demo(*args: str, env: dict | None = None, path_prefix: str | None = None) -> subprocess.CompletedProcess:
    """demo.sh with a clean environment; an env value of None removes the variable."""
    strip = ("RUN", "LANGS", "TAG", "REGISTRY", "DEMO_", "KUBE_CONTEXT", "NAMESPACE")
    full = {k: v for k, v in os.environ.items() if not k.startswith(strip)}
    full.update({"RUN": "r1", "TAG": "abc1234"})
    if path_prefix:
        full["PATH"] = f"{path_prefix}{os.pathsep}{full.get('PATH', '')}"
    for key, value in (env or {}).items():
        if value is None:
            full.pop(key, None)
        else:
            full[key] = value
    return subprocess.run(["bash", str(DEMO), *args], capture_output=True, text=True, env=full, timeout=120)


def executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def documents(text: str) -> dict[tuple[str, str], str]:
    """(kind, name) -> document text, for the rendered multi-document YAML."""
    docs = {}
    for doc in text.split("\n---\n"):
        kind = re.search(r"^kind: (\S+)$", doc, re.M)
        name = re.search(r"^  name: (\S+)$", doc, re.M)
        if kind and name:
            docs[(kind.group(1), name.group(1))] = doc
    return docs


def env_of(doc: str) -> dict[str, str]:
    return dict(re.findall(r'- name: (\S+)\n\s+value: "([^"]*)"', doc))


class Render(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_demo("render")
        cls.docs = documents(cls.result.stdout)

    def test_render_succeeds_offline(self):
        self.assertEqual(self.result.returncode, 0, self.result.stderr)

    def test_twelve_deployments_and_eight_services(self):
        for lang in LANGS:
            for app in APPS:
                self.assertIn(("Deployment", f"demo-{lang}-{app}"), self.docs)
            self.assertIn(("Service", f"demo-{lang}-api"), self.docs)
            self.assertIn(("Service", f"demo-{lang}-backend"), self.docs)
            self.assertNotIn(("Service", f"demo-{lang}-worker"), self.docs)

    def test_de_d2_env(self):
        for lang in LANGS:
            for app in APPS:
                env = env_of(self.docs[("Deployment", f"demo-{lang}-{app}")])
                with self.subTest(lang=lang, app=app):
                    self.assertEqual(env["OTEL_SERVICE_NAME"], f"demo-{lang}-{app}")
                    self.assertEqual(env["OTEL_RESOURCE_ATTRIBUTES"],
                                     "deployment.environment.name=dev,otel_libs.run=r1")
                    self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"],
                                     "http://otel-agent-collector.opentelemetry.svc.cluster.local:4317")
                    self.assertEqual(env["OTEL_EXPORTER_OTLP_PROTOCOL"], "grpc")
                    self.assertEqual(env["OTEL_METRIC_EXPORT_INTERVAL"], "10000")
                    grpc_key = "OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION"
                    if (lang, app) == ("dotnet", "backend"):
                        self.assertEqual(env[grpc_key], "true")
                    else:
                        self.assertNotIn(grpc_key, env)

    def test_wiring(self):
        for lang in LANGS:
            worker = env_of(self.docs[("Deployment", f"demo-{lang}-worker")])
            api = env_of(self.docs[("Deployment", f"demo-{lang}-api")])
            backend = env_of(self.docs[("Deployment", f"demo-{lang}-backend")])
            self.assertEqual(worker["DEMO_API_URL"], f"http://demo-{lang}-api:8080")
            self.assertEqual(api["DEMO_BACKEND_ADDR"], f"demo-{lang}-backend:50051")
            self.assertEqual(api["DEMO_HTTP_PORT"], "8080")
            self.assertEqual(backend["DEMO_GRPC_PORT"], "50051")
            for e in (worker, api):
                self.assertEqual(e["DEMO_REDIS_ADDR"], "demo-redis:6379")
                self.assertEqual(e["DEMO_REDIS_PASSWORD_FILE"], "/run/secrets/demo-redis/password")
                self.assertNotIn("DEMO_PG_PASSWORD_FILE", e)
            self.assertEqual(backend["DEMO_PG_HOST"], "demo-postgres")
            self.assertEqual(backend["DEMO_PG_DB"], f"demo_{lang}")
            self.assertEqual(backend["DEMO_PG_USER"], f"demo_{lang}")
            self.assertEqual(backend["DEMO_PG_PASSWORD_FILE"], "/run/secrets/demo-postgres/password")
            self.assertNotIn("DEMO_REDIS_PASSWORD_FILE", backend)

    def test_passwords_are_files_with_least_privilege(self):
        for lang in LANGS:
            for app in APPS:
                doc = self.docs[("Deployment", f"demo-{lang}-{app}")]
                with self.subTest(lang=lang, app=app):
                    self.assertNotIn("secretKeyRef", doc)
                    self.assertNotIn("envFrom", doc)
                    self.assertIn("defaultMode: 0440", doc)
                    if app == "backend":
                        self.assertIn(f"- {{key: demo_{lang}, path: password}}", doc)
                        self.assertNotIn("demo-redis-auth", doc)
                    else:
                        self.assertIn("secretName: demo-redis-auth", doc)
                        self.assertNotIn("demo-postgres-auth", doc)
                        self.assertNotIn("redis.conf", doc)

    def test_labels_hardening_budget(self):
        for lang in LANGS:
            cpu, mem, limit = BUDGET[lang]
            for app in APPS:
                doc = self.docs[("Deployment", f"demo-{lang}-{app}")]
                with self.subTest(lang=lang, app=app):
                    self.assertEqual(doc.count("app.kubernetes.io/component: demo"), 2)
                    self.assertEqual(doc.count(f"otel-libs/lang: {lang}"), 2)
                    self.assertEqual(doc.count('otel-libs/run: "r1"'), 2)
                    self.assertIn(f"image: harbor.staffops.space/development/otel-libs-demo-{lang}-{app}:abc1234", doc)
                    self.assertIn(f"requests: {{cpu: {cpu}, memory: {mem}}}", doc)
                    self.assertIn(f"limits: {{memory: {limit}}}", doc)
                    self.assertNotRegex(doc, r"limits: \{[^}]*cpu")
                    for line in ("automountServiceAccountToken: false", "runAsNonRoot: true",
                                 f"runAsUser: {UID[lang]}", f"fsGroup: {UID[lang]}",
                                 "type: RuntimeDefault", "allowPrivilegeEscalation: false",
                                 "readOnlyRootFilesystem: true", 'drop: ["ALL"]'):
                        self.assertIn(line, doc)

    def test_probes(self):
        for lang in LANGS:
            api = self.docs[("Deployment", f"demo-{lang}-api")]
            backend = self.docs[("Deployment", f"demo-{lang}-backend")]
            worker = self.docs[("Deployment", f"demo-{lang}-worker")]
            self.assertEqual(api.count("httpGet: {path: /health, port: 8080}"), 2)
            self.assertIn("livenessProbe:\n            tcpSocket: {port: 8080}", api)
            self.assertEqual(backend.count("grpc: {port: 50051}"), 2)
            self.assertIn("livenessProbe:\n            tcpSocket: {port: 50051}", backend)
            self.assertNotIn("Probe", worker)

    def test_service_app_protocols(self):
        self.assertIn("appProtocol: tcp", self.docs[("Service", "demo-postgres")])
        self.assertIn("appProtocol: tcp", self.docs[("Service", "demo-redis")])
        for lang in LANGS:
            self.assertIn("appProtocol: http", self.docs[("Service", f"demo-{lang}-api")])
            self.assertIn("appProtocol: grpc", self.docs[("Service", f"demo-{lang}-backend")])

    def test_no_secret_value(self):
        self.assertIsNone(HEX48.search(self.result.stdout))
        self.assertIn("<generated>", self.result.stdout)

    def test_langs_subset(self):
        result = run_demo("render", env={"LANGS": "go"})
        docs = documents(result.stdout)
        self.assertIn(("Deployment", "demo-go-api"), docs)
        self.assertNotIn(("Deployment", "demo-python-api"), docs)
        self.assertIn(("Deployment", "demo-postgres"), docs)


SENTINEL = """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$(dirname "$0")/called"
exit 97
"""


class Usage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for tool in ("kubectl", "docker", "openssl"):
            executable(self.dir / tool, SENTINEL)

    def tearDown(self):
        self.tmp.cleanup()

    def assert_untouched(self):
        self.assertFalse((self.dir / "called").exists(),
                         (self.dir / "called").read_text() if (self.dir / "called").exists() else "")

    def test_refusals_before_any_cluster_call(self):
        for args, env in [(("render",), {"LANGS": "go rust"}), (("render",), {"LANGS": " "}),
                          (("render",), {"TAG": "bad tag"}), (("render",), {"TAG": "latest"}),
                          (("render",), {"DEMO_WATCH_SECONDS": "x"}),
                          (("apply", "--lang", "rust"), {}), (("apply", "--bogus"), {}),
                          (("apply", "--lang"), {}), (("delete",), {}), (("delete", "--lang", "rust"), {}),
                          (("bogus",), {})]:
            with self.subTest(args=args, env=env):
                result = run_demo(*args, env=env, path_prefix=str(self.dir))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assert_untouched()

    def test_other_target_refused(self):
        for env in ({"KUBE_CONTEXT": "prod"}, {"NAMESPACE": "default"}):
            with self.subTest(env=env):
                result = run_demo("apply", "--all", env=env, path_prefix=str(self.dir))
                self.assertEqual(result.returncode, 2)
                self.assertIn("refusing target", result.stderr)
                self.assert_untouched()

    def test_render_calls_nothing(self):
        result = run_demo("render", path_prefix=str(self.dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_untouched()

    def test_default_tag_without_git_is_refused(self):
        executable(self.dir / "git", "#!/usr/bin/env bash\nexit 128\n")
        result = run_demo("render", env={"TAG": None}, path_prefix=str(self.dir))
        self.assertEqual(result.returncode, 2)
        self.assertIn("set TAG explicitly", result.stderr)
        self.assertNotIn("latest", result.stdout)


FAKE_KUBECTL = r"""#!/usr/bin/env bash
# Fake kubectl for test_demo_apps.py: answers reads from state files in
# $FAKE_DIR, records every call, refuses selectors it does not expect.
d="$FAKE_DIR"
printf '%s\n' "$*" >> "$d/calls.log"
args=" $* "
case "$args" in
  *" get namespace "*) echo namespace/dev-otel-libs ;;
  *" get deployments -l app.kubernetes.io/part-of=otel-libs,app.kubernetes.io/component=demo,!otel-libs/lang "*)
    cat "$d/backing.txt" ;;
  *" get deployments -l app.kubernetes.io/part-of=otel-libs,app.kubernetes.io/component=demo,otel-libs/lang in ("*" jsonpath="*)
    cat "$d/app_state.txt" 2>/dev/null ;;  # DEMO_RESTORE=1 checks: "<name> <replicas> <image>"
  *" get deployments -l app.kubernetes.io/part-of=otel-libs,app.kubernetes.io/component=demo,otel-libs/lang in ("*)
    cat "$d/app_deploys.txt" 2>/dev/null ;;  # demo.sh scale (DE13)
  *" get secret "*) echo secret/a ;;
  *" get pods -l app.kubernetes.io/name=demo-postgres "*) printf '0 ' ;;
  *" exec "*) echo 4 ;;
  *" get nodes "*) cat "$d/nodes.json" ;;
  *" get pods --all-namespaces --field-selector status.phase!=Succeeded"*)
    # Once the backing services exist, their requests count on the node.
    if [ -e "$d/created.yaml" ]; then
      echo '{"items": [{"spec": {"nodeName": "k8s01", "containers": [{"resources": {"requests": {"cpu": "45m", "memory": "96Mi"}}}]}}]}'
    else
      echo '{"items": []}'
    fi ;;
  *" get events "*) [ ! -e "$d/events_fail" ] || exit 1; cat "$d/events.json" ;;
  *" get pods --all-namespaces --field-selector status.phase=Pending"*) cat "$d/pending.json" ;;
  *" apply "*) cat > "$d/applied.yaml" ;;
  *" create "*) cat > "$d/created.yaml" ;;
  *" rollout status "*) exit 0 ;;
  *" scale "*) echo scaled ;;
  *" delete "*) echo deleted ;;
  *" get pods -l app.kubernetes.io/part-of=otel-libs,app.kubernetes.io/component=demo,otel-libs/lang="*" -o name "*)
    cat "$d/lang_pods.txt" 2>/dev/null ;;
  *" wait --for=delete pods "*) echo waited ;;
  *" get pods -l "*) echo "NAME READY" ;;
  *) echo "fake kubectl: unexpected: $*" >&2; exit 9 ;;
esac
"""

FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_DIR/docker.log"
[ "$1 $2 $3" = "buildx imagetools inspect" ] || exit 9
grep -qxF "$4" "$FAKE_DIR/images.txt"
"""

# openssl rand -hex 24 without an openssl binary (the slim test image has none).
FAKE_OPENSSL = r"""#!/usr/bin/env bash
[ "$*" = "rand -hex 24" ] || exit 9
od -An -N24 -tx1 /dev/urandom | tr -d ' \n'; echo
"""

FAKE_GIT = r"""#!/usr/bin/env bash
case " $* " in
  *" rev-parse --show-toplevel "*) echo /fake-root ;;
  *" log -1 --format=%h "*) echo f00d123 ;;
  *) exit 9 ;;
esac
"""


def node(cpu: str, mem: str) -> dict:
    return {"items": [{"metadata": {"name": "k8s01"}, "spec": {},
                       "status": {"allocatable": {"cpu": cpu, "memory": mem, "pods": "110"},
                                  "conditions": [{"type": "Ready", "status": "True"}]}}]}


class FakeCluster(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        executable(self.dir / "kubectl", FAKE_KUBECTL)
        executable(self.dir / "docker", FAKE_DOCKER)
        executable(self.dir / "git", FAKE_GIT)
        executable(self.dir / "openssl", FAKE_OPENSSL)
        self.write("nodes.json", node("8", "32Gi"))
        self.write("events.json", {"items": []})
        self.write("pending.json", {"items": []})
        (self.dir / "backing.txt").write_text("demo-postgres=r1\ndemo-redis=r1\n")
        (self.dir / "images.txt").write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, data: dict) -> None:
        (self.dir / name).write_text(json.dumps(data))

    def demo(self, *args: str, **env) -> subprocess.CompletedProcess:
        full = {"FAKE_DIR": str(self.dir), "DEMO_WATCH_SECONDS": "0", **env}
        return run_demo(*args, env=full, path_prefix=str(self.dir))

    def apply(self, *args: str, **env) -> subprocess.CompletedProcess:
        return self.demo("apply", *args, **env)

    def calls(self) -> list[str]:
        path = self.dir / "calls.log"
        return path.read_text().splitlines() if path.exists() else []


class Apply(FakeCluster):
    def test_all_fits_and_applies_with_the_backing_run(self):
        result = self.apply("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        # 3 pods per language: 4 x 3 = 12; Σ cpu 3*(5+15+10+20); Σ memory 3*(32+96+64+128).
        self.assertIn("run 150m 960Mi 12 pods", result.stdout)
        applied = (self.dir / "applied.yaml").read_text()
        self.assertEqual(applied.count("kind: Deployment"), 12)
        self.assertEqual(applied.count("kind: Service"), 8)
        self.assertNotIn("name: demo-postgres", applied)
        # The apps take .spec.replicas back from a `scale` (kubectl field manager).
        self.assertTrue(any("--server-side --force-conflicts --field-manager=otel-libs-demo" in c for c in self.calls()))
        self.assertFalse(any(" create " in f" {c} " for c in self.calls()))  # backing reused, no Secret written
        self.assertIn("run=r1 langs=go python node dotnet", result.stdout)
        self.assertFalse((self.dir / "docker.log").exists())  # explicit TAG: no image look-up

    def test_one_language(self):
        result = self.apply("--lang", "dotnet")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("run 60m 384Mi 3 pods", result.stdout)
        applied = (self.dir / "applied.yaml").read_text()
        self.assertEqual(applied.count("kind: Deployment"), 3)
        self.assertIn("demo-dotnet-backend", applied)
        self.assertNotIn("demo-go-", applied)

    def test_all_does_not_fit_says_rotate(self):
        self.write("nodes.json", node("350m", "32Gi"))  # 150m + 250m reserve > 350m
        result = self.apply("--all")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Rotate one language per window", result.stderr)
        self.assertNotIn("backing services were created", result.stderr)
        self.assertFalse((self.dir / "applied.yaml").exists())

    def test_one_language_does_not_fit(self):
        self.write("nodes.json", node("250m", "32Gi"))  # 15m + 250m reserve > 250m
        result = self.apply("--lang", "go")
        self.assertEqual(result.returncode, 1)
        self.assertIn("pre-flight failed for go", result.stderr)
        self.assertFalse((self.dir / "applied.yaml").exists())

    def test_backing_created_but_apps_do_not_fit(self):
        # No backing services: apply creates them (45m fit in 59m), then the
        # apps (15m) do not fit in the 14m left — the message says what was created.
        (self.dir / "backing.txt").write_text("")
        self.write("nodes.json", node("309m", "32Gi"))  # 59m free, then 14m
        result = self.apply("--lang", "go")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue((self.dir / "created.yaml").exists())
        self.assertIn("backing services were created by this apply", result.stderr)
        self.assertIn("apps were not applied", result.stderr)
        self.assertNotIn("demo-go-", (self.dir / "applied.yaml").read_text())

    def test_failed_scheduling_scales_the_apps_down(self):
        self.write("events.json", {"items": [{
            "reason": "FailedScheduling", "message": "0/1 nodes are available: 1 Insufficient cpu.",
            "involvedObject": {"kind": "Pod", "namespace": "platform", "name": "p1"},
            "lastTimestamp": "2999-01-01T00:00:00Z"}]})
        self.write("pending.json", {"items": [{"metadata": {"namespace": "platform", "name": "p1"},
                                               "status": {"phase": "Pending"}}]})
        result = self.apply("--lang", "go")
        self.assertEqual(result.returncode, 1)
        scale = [c for c in self.calls() if " scale " in f" {c} "]
        self.assertEqual(len(scale), 1)
        self.assertIn("--replicas=0", scale[0])
        self.assertIn("otel-libs/run=r1,otel-libs/lang in (go)", scale[0])

    def test_blind_watch_fails_without_scaling(self):
        (self.dir / "events_fail").write_text("")
        result = self.apply("--lang", "go")
        self.assertEqual(result.returncode, 1)
        self.assertIn("blind for all 1 look-ups", result.stderr)
        self.assertFalse(any(" scale " in f" {c} " for c in self.calls()))

    def test_explicit_run_mismatch_refused(self):
        result = self.apply("--lang", "go", RUN="other")
        self.assertEqual(result.returncode, 1)
        self.assertIn("differs from the running backing services", result.stderr)

    def test_partial_backing_state_refused(self):
        (self.dir / "backing.txt").write_text("demo-postgres=r1\n")
        result = self.apply("--lang", "go")
        self.assertEqual(result.returncode, 1)
        self.assertIn("partial demo state", result.stderr)
        self.assertFalse((self.dir / "applied.yaml").exists())
        self.assertFalse((self.dir / "created.yaml").exists())

    def test_backing_lookup_excludes_apps(self):
        self.apply("--lang", "go")
        lookups = [c for c in self.calls() if "get deployments" in c]
        self.assertEqual(len(lookups), 1)
        self.assertIn(",!otel-libs/lang", lookups[0])


class DefaultTag(FakeCluster):
    def test_missing_images_refused_before_anything(self):
        result = self.apply("--lang", "go", TAG=None)
        self.assertEqual(result.returncode, 1)
        self.assertIn("image(s) not found", result.stderr)
        self.assertIn("otel-libs-demo-go-worker:f00d123", result.stderr)
        self.assertEqual(self.calls(), [])  # not even the backing services

    def test_present_images_use_the_derived_tag(self):
        (self.dir / "images.txt").write_text("".join(
            f"harbor.staffops.space/development/otel-libs-demo-go-{a}:f00d123\n" for a in APPS))
        result = self.apply("--lang", "go", TAG=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len((self.dir / "docker.log").read_text().splitlines()), 3)
        self.assertIn("otel-libs-demo-go-api:f00d123", (self.dir / "applied.yaml").read_text())


class Delete(FakeCluster):
    def test_delete_lang_scopes_to_the_language_and_waits_for_pods(self):
        (self.dir / "lang_pods.txt").write_text("pod/demo-go-api-x\n")
        result = self.demo("delete", "--lang", "go")
        self.assertEqual(result.returncode, 0, result.stderr)
        deletes = [c for c in self.calls() if c.startswith("--context k8s01 -n dev-otel-libs delete")]
        self.assertEqual(len(deletes), 1)
        self.assertIn("deployments,services -l", deletes[0])
        self.assertIn("app.kubernetes.io/component=demo,otel-libs/lang=go", deletes[0])
        self.assertNotIn("secrets", deletes[0])
        self.assertTrue(any("wait --for=delete pods" in c and "otel-libs/lang=go" in c for c in self.calls()))

    def test_delete_lang_without_pods_does_not_wait(self):
        result = self.demo("delete", "--lang", "node")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(" wait " in f" {c} " for c in self.calls()))

    def test_delete_all_includes_secrets_and_no_lang_filter(self):
        result = self.demo("delete", "--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        deletes = [c for c in self.calls() if " delete " in f" {c} "]
        self.assertEqual(len(deletes), 1)
        self.assertIn("deployments,services,secrets -l", deletes[0])
        self.assertNotIn("otel-libs/lang", deletes[0])


if __name__ == "__main__":
    unittest.main()
