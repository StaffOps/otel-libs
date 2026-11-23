"""Tests for the demo.sh env-override hook and `scale` (task DE13).

Design DE-D7 amendments 2026-11-19 and 2026-11-23: `DEMO_ENV_FILE` replaces
or adds app variables, never twice; only OTEL_* / DEMO_* names, without the
identity (`OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`) and the credential
wiring; `*_HEADERS` values masked in `render`; byte-identical manifests
without the file; `DEMO_RESTART_NONCE` changes the pod template; `scale
--lang|--all <n>` touches the demo apps only.

`fixtures/render-default.yaml` is `RUN=r1 TAG=abc1234 demo.sh render` of the
hook-less demo.sh (commit ec4c262). Regenerate it only for an intended change
of the default manifests, never to make this test pass.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from test_demo_apps import APPS, LANGS, SENTINEL, FakeCluster, documents, env_of, executable, run_demo

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "fixtures" / "render-default.yaml"
SECRET = "s3cr3t-canary-0001"


class HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for tool in ("kubectl", "docker", "openssl", "python3"):
            executable(self.dir / tool, SENTINEL)

    def tearDown(self):
        self.tmp.cleanup()

    def env_file(self, text: str) -> str:
        path = self.dir / "soak.env"
        path.write_text(text)
        return str(path)

    def render(self, text: str | None = None, **env):
        if text is not None:
            env["DEMO_ENV_FILE"] = self.env_file(text)
        return run_demo("render", env=env, path_prefix=str(self.dir))

    def assert_untouched(self):
        called = self.dir / "called"
        self.assertFalse(called.exists(), called.read_text() if called.exists() else "")


class Unchanged(HookCase):
    def test_render_without_the_file_is_byte_identical(self):
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, GOLDEN.read_text())

    def test_empty_name_and_comment_only_file_change_nothing(self):
        for env, text in (({"DEMO_ENV_FILE": ""}, None), ({}, "# soak run r1\n\n   \n  # indented\n")):
            with self.subTest(env=env, text=text):
                result = self.render(text, **env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, GOLDEN.read_text())
        self.assert_untouched()


class Overrides(HookCase):
    def test_replace_keeps_one_entry_in_place(self):
        result = self.render("go/api OTEL_METRIC_EXPORT_INTERVAL=60000\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = documents(result.stdout)
        api = docs[("Deployment", "demo-go-api")]
        self.assertEqual(env_of(api)["OTEL_METRIC_EXPORT_INTERVAL"], "60000")
        self.assertEqual(api.count("- name: OTEL_METRIC_EXPORT_INTERVAL"), 1)
        names = [line.split()[-1] for line in api.splitlines() if line.strip().startswith("- name: ")]
        default = [line.split()[-1] for line in documents(GOLDEN.read_text())[("Deployment", "demo-go-api")].splitlines()
                   if line.strip().startswith("- name: ")]
        self.assertEqual(names, default)  # same variables, same order
        for lang in LANGS:
            for app in APPS:
                if (lang, app) != ("go", "api"):
                    with self.subTest(lang=lang, app=app):
                        self.assertEqual(env_of(docs[("Deployment", f"demo-{lang}-{app}")])["OTEL_METRIC_EXPORT_INTERVAL"],
                                         "10000")

    def test_add_and_wildcards(self):
        result = self.render(
            "*/worker DEMO_INTERVAL_SECONDS=3600\n"
            "*/* OTEL_EXPORTER_OTLP_ENDPOINT=http://soak-otlp.dev-otel-libs.svc.cluster.local:4317\n"
            "python/* OTEL_EXPORTER_OTLP_TIMEOUT=\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = documents(result.stdout)
        for lang in LANGS:
            for app in APPS:
                doc = docs[("Deployment", f"demo-{lang}-{app}")]
                env = env_of(doc)
                with self.subTest(lang=lang, app=app):
                    self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"],
                                     "http://soak-otlp.dev-otel-libs.svc.cluster.local:4317")
                    self.assertEqual(doc.count("- name: OTEL_EXPORTER_OTLP_ENDPOINT"), 1)
                    self.assertEqual(env.get("DEMO_INTERVAL_SECONDS"), "3600" if app == "worker" else None)
                    self.assertEqual(env.get("OTEL_EXPORTER_OTLP_TIMEOUT"), "" if lang == "python" else None)
                    self.assertEqual(env["OTEL_SERVICE_NAME"], f"demo-{lang}-{app}")
        # The backing services are not apps: no override reaches them.
        self.assertNotIn("soak-otlp", docs[("Deployment", "demo-postgres")])

    def test_disjoint_selectors_may_set_one_name(self):
        result = self.render("go/api DEMO_X=1\npython/api DEMO_X=2\ngo/worker DEMO_X=3\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = documents(result.stdout)
        self.assertEqual(env_of(docs[("Deployment", "demo-go-api")])["DEMO_X"], "1")
        self.assertEqual(env_of(docs[("Deployment", "demo-python-api")])["DEMO_X"], "2")
        self.assertEqual(env_of(docs[("Deployment", "demo-go-worker")])["DEMO_X"], "3")

    def test_dotnet_backend_default_is_overridable(self):
        key = "OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION"
        result = self.render(f"dotnet/backend {key}=false\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = documents(result.stdout)[("Deployment", "demo-dotnet-backend")]
        self.assertEqual(env_of(doc)[key], "false")
        self.assertEqual(doc.count(f"- name: {key}"), 1)


class Rejected(HookCase):
    def assert_rejected(self, text: str, *needles: str):
        result = self.render(text)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("DEMO_ENV_FILE line", result.stderr)
        self.assertIn("nothing applied", result.stderr)
        for needle in needles:
            self.assertIn(needle, result.stderr)
        self.assertNotIn(SECRET, result.stderr)
        self.assert_untouched()

    def test_duplicates(self):
        for text, line in (("go/api DEMO_X=1\ngo/api DEMO_X=2\n", "line 2"),
                           ("*/* DEMO_X=1\n# c\ngo/api DEMO_X=2\n", "line 3"),
                           ("go/* DEMO_X=1\n*/api DEMO_X=2\n", "line 2"),
                           ("*/worker OTEL_EXPORTER_OTLP_ENDPOINT=a\n*/worker OTEL_EXPORTER_OTLP_ENDPOINT=b\n", "line 2")):
            with self.subTest(text=text):
                self.assert_rejected(text, line, "duplicate", "by line 1")

    def test_denied_names(self):
        for name in ("OTEL_SERVICE_NAME", "OTEL_RESOURCE_ATTRIBUTES", "DEMO_REDIS_PASSWORD_FILE",
                     "DEMO_PG_PASSWORD_FILE", "DEMO_PG_HOST", "DEMO_PG_USER", "DEMO_REDIS_ADDR",
                     "DEMO_OTHER_PASSWORD_FILE"):
            with self.subTest(name=name):
                self.assert_rejected(f"*/* {name}={SECRET}\n", "line 1", name, "may not be overridden")

    def test_names_outside_the_allow_list(self):
        for name in ("PATH", "HOME", "LD_PRELOAD", "otel_service_name", "OTELX", "DEMO", "Demo_X"):
            with self.subTest(name=name):
                self.assert_rejected(f"go/api {name}={SECRET}\n", "line 1", "not an OTEL_* or DEMO_* name")

    def test_malformed_lines(self):
        for text, needle in (("go/api\n", "not '<lang>/<app> NAME=VALUE'"),
                             ("go/api DEMO_X\n", "not '<lang>/<app> NAME=VALUE'"),
                             ("goapi DEMO_X=1\n", "not '<lang>/<app> NAME=VALUE'"),
                             ("go/api  =1\n", "not '<lang>/<app> NAME=VALUE'"),
                             ("DEMO_X=1\n", "not '<lang>/<app> NAME=VALUE'"),
                             ("rust/api DEMO_X=1\n", "unknown language 'rust'"),
                             ("go/db DEMO_X=1\n", "unknown app 'db'"),
                             (f'go/api DEMO_X={SECRET}"\n', "quote, a backslash or a control character"),
                             (f"go/api DEMO_X={SECRET}\\n\n", "quote, a backslash or a control character"),
                             (f"go/api DEMO_X={SECRET}\tx\n", "quote, a backslash or a control character"),
                             (f"go/api DEMO_X={SECRET}\r\n", "quote, a backslash or a control character")):
            with self.subTest(text=text):
                self.assert_rejected(text, "line 1", needle)

    def test_bad_line_after_good_ones(self):
        self.assert_rejected("# header\ngo/api DEMO_X=1\n\nbogus\n", "line 4")

    def test_missing_file(self):
        result = run_demo("render", env={"DEMO_ENV_FILE": str(self.dir / "absent.env")}, path_prefix=str(self.dir))
        self.assertEqual(result.returncode, 2)
        self.assertIn("is not a readable file", result.stderr)
        self.assertEqual(result.stdout, "")


class Masking(HookCase):
    def test_headers_masked_in_render(self):
        result = self.render(f"*/* OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer {SECRET}\n"
                             f"go/api OTEL_EXPORTER_OTLP_TRACES_HEADERS=x-canary={SECRET}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        docs = documents(result.stdout)
        for lang in LANGS:
            for app in APPS:
                with self.subTest(lang=lang, app=app):
                    self.assertEqual(env_of(docs[("Deployment", f"demo-{lang}-{app}")])["OTEL_EXPORTER_OTLP_HEADERS"],
                                     "<set>")
        self.assertEqual(env_of(docs[("Deployment", "demo-go-api")])["OTEL_EXPORTER_OTLP_TRACES_HEADERS"], "<set>")

    def test_other_values_are_shown(self):
        result = self.render("go/api DEMO_CANARY=visible-value\n")
        self.assertEqual(env_of(documents(result.stdout)[("Deployment", "demo-go-api")])["DEMO_CANARY"],
                         "visible-value")


class Nonce(HookCase):
    def test_nonce_changes_only_the_pod_template_env(self):
        a = self.render("*/* DEMO_RESTART_NONCE=a1\n")
        b = self.render("*/* DEMO_RESTART_NONCE=b2\n")
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertEqual(b.returncode, 0, b.stderr)
        docs_a, docs_b = documents(a.stdout), documents(b.stdout)
        self.assertEqual(docs_a.keys(), docs_b.keys())
        for key, doc in docs_a.items():
            with self.subTest(doc=key):
                if key[0] == "Deployment" and key[1] not in ("demo-postgres", "demo-redis"):
                    self.assertEqual(env_of(doc)["DEMO_RESTART_NONCE"], "a1")
                    self.assertEqual(env_of(docs_b[key])["DEMO_RESTART_NONCE"], "b2")
                    # The env list is inside spec.template: a new value is a new pod template.
                    template = doc.index("  template:")
                    self.assertGreater(doc.index("- name: DEMO_RESTART_NONCE"), template)
                    self.assertEqual(doc.replace('"a1"', '"b2"'), docs_b[key])
                else:
                    self.assertEqual(doc, docs_b[key])
        self.assertEqual(len(a.stdout.splitlines()), len(GOLDEN.read_text().splitlines()) + 2 * 12)


class ApplyWithHook(FakeCluster):
    def env_file(self, text: str) -> str:
        path = self.dir / "soak.env"
        path.write_text(text)
        return str(path)

    def test_apply_sends_real_values(self):
        result = self.apply("--lang", "go", DEMO_ENV_FILE=self.env_file(
            f"*/* OTEL_EXPORTER_OTLP_HEADERS=x-canary={SECRET}\n*/worker DEMO_INTERVAL_SECONDS=3600\n"
            "*/* DEMO_RESTART_NONCE=n1\n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        docs = documents((self.dir / "applied.yaml").read_text())
        worker = env_of(docs[("Deployment", "demo-go-worker")])
        self.assertEqual(worker["OTEL_EXPORTER_OTLP_HEADERS"], f"x-canary={SECRET}")
        self.assertEqual(worker["DEMO_INTERVAL_SECONDS"], "3600")
        self.assertEqual(worker["DEMO_RESTART_NONCE"], "n1")
        self.assertNotIn("DEMO_INTERVAL_SECONDS", env_of(docs[("Deployment", "demo-go-api")]))

    def test_bad_file_fails_before_any_call(self):
        result = self.apply("--lang", "go", DEMO_ENV_FILE=self.env_file("*/* OTEL_SERVICE_NAME=x\n"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.dir / "applied.yaml").exists())

    def test_bad_file_fails_backing_apply_too(self):
        result = self.apply(DEMO_ENV_FILE=self.env_file("bogus\n"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_delete_ignores_the_file(self):
        bad = self.env_file("bogus\n")
        self.assertEqual(self.demo("delete", "--lang", "node", DEMO_ENV_FILE=bad).returncode, 0)


def deploys(*langs: str) -> str:
    return "".join(f"deployment.apps/demo-{lang}-{app}\n" for lang in langs for app in APPS)


class Scale(FakeCluster):
    def scale(self, *args: str, **env):
        return self.demo("scale", *args, **env)

    def scale_calls(self) -> list[str]:
        return [c for c in self.calls() if " scale " in f" {c} "]

    def test_to_zero_skips_the_preflight_and_waits_for_pods(self):
        (self.dir / "app_deploys.txt").write_text(deploys("go"))
        result = self.scale("--lang", "go", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        (call,) = self.scale_calls()
        self.assertIn("--replicas=0", call)
        self.assertIn("app.kubernetes.io/component=demo,otel-libs/lang in (go)", call)
        self.assertFalse(any("get nodes" in c for c in self.calls()))  # no pre-flight
        self.assertTrue(any("wait --for=delete pods" in c and "otel-libs/lang in (go)" in c for c in self.calls()))
        self.assertIn("scaled to 0: go", result.stdout)

    def test_all_up_runs_the_preflight_and_never_touches_backing(self):
        (self.dir / "app_deploys.txt").write_text(deploys(*LANGS))
        result = self.scale("--all", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        # 2 replicas of 12 pods: 2 x 150m, 2 x 960Mi, 24 pods.
        self.assertIn("run 300m 1920Mi 24 pods", result.stdout)
        self.assertTrue(any("get nodes" in c for c in self.calls()))
        (call,) = self.scale_calls()
        self.assertIn("--replicas=2", call)
        self.assertIn("otel-libs/lang in (go,python,node,dotnet)", call)
        for c in self.calls():
            self.assertNotIn("demo-postgres", c)
            self.assertNotIn("demo-redis", c)
        self.assertFalse((self.dir / "applied.yaml").exists())  # scale renders nothing
        self.assertIn("apps rolled out: demo-go-worker", result.stdout)

    def test_all_follows_langs(self):
        (self.dir / "app_deploys.txt").write_text(deploys("node"))
        result = self.scale("--all", "1", LANGS="node")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("otel-libs/lang in (node)", self.scale_calls()[0])

    def test_up_that_does_not_fit_scales_nothing(self):
        (self.dir / "app_deploys.txt").write_text(deploys("dotnet"))
        self.write("nodes.json", {"items": [{"metadata": {"name": "k8s01"}, "spec": {}, "status": {
            "allocatable": {"cpu": "300m", "memory": "32Gi", "pods": "110"},
            "conditions": [{"type": "Ready", "status": "True"}]}}]})  # 60m + 250m reserve > 300m
        result = self.scale("--lang", "dotnet", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nothing scaled", result.stderr)
        self.assertEqual(self.scale_calls(), [])

    def test_missing_apps_refused(self):
        (self.dir / "app_deploys.txt").write_text("deployment.apps/demo-go-api\n")
        result = self.scale("--lang", "go", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("demo-go-worker is not applied", result.stderr)
        self.assertEqual(self.scale_calls(), [])

    def test_failed_scheduling_after_scale_up_scales_down(self):
        (self.dir / "app_deploys.txt").write_text(deploys("go"))
        self.write("events.json", {"items": [{
            "reason": "FailedScheduling", "message": "0/1 nodes are available: 1 Insufficient cpu.",
            "involvedObject": {"kind": "Pod", "namespace": "platform", "name": "p1"},
            "lastTimestamp": "2999-01-01T00:00:00Z"}]})
        self.write("pending.json", {"items": [{"metadata": {"namespace": "platform", "name": "p1"},
                                               "status": {"phase": "Pending"}}]})
        result = self.scale("--lang", "go", "1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual([c.split("--replicas=")[1] for c in self.scale_calls()], ["1", "0"])


class ScaleUsage(HookCase):
    def test_refusals_before_any_cluster_call(self):
        for args in (("scale",), ("scale", "--lang"), ("scale", "--lang", "rust", "1"), ("scale", "--lang", "go"),
                     ("scale", "--lang", "go", "-1"), ("scale", "--lang", "go", "x"), ("scale", "--lang", "go", "01"),
                     ("scale", "--lang", "go", "1000"), ("scale", "--lang", "go", "1", "2"), ("scale", "--all"),
                     ("scale", "--all", "1", "2"), ("scale", "--all", "1.5"), ("scale", "--bogus", "1"),
                     ("scale", "go", "1")):
            with self.subTest(args=args):
                result = run_demo(*args, path_prefix=str(self.dir))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assert_untouched()

    def test_other_target_refused(self):
        result = run_demo("scale", "--all", "0", env={"KUBE_CONTEXT": "prod"}, path_prefix=str(self.dir))
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing target", result.stderr)
        self.assert_untouched()


if __name__ == "__main__":
    unittest.main()
