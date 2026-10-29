"""ADR-011: the deprecated ``otel_helper`` import re-exports ``staffops_otel_helper``.

The compatibility module ships in the same wheel during 0.3.x and is removed
in 1.0. It must warn once, expose the same objects (not copies: the helper
keeps one copy of its state) and resolve submodule imports to the same module
objects.
"""

import importlib
import logging
import subprocess
import sys
import types
import warnings
from pathlib import Path

import pytest

import staffops_otel_helper
import staffops_otel_helper.config
import staffops_otel_helper.ext.aws
import staffops_otel_helper.ext.redis
import staffops_otel_helper.ext.sql
import staffops_otel_helper.metrics


def _is_shim(name):
    return name == "otel_helper" or name.startswith("otel_helper.")


@pytest.fixture
def fresh_shim():
    """Drop the shim from sys.modules/sys.meta_path so each test imports it anew."""
    saved_modules = {k: v for k, v in sys.modules.items() if _is_shim(k)}
    for name in saved_modules:
        del sys.modules[name]
    saved_meta = list(sys.meta_path)
    sys.meta_path[:] = [f for f in saved_meta if type(f).__module__ != "otel_helper"]
    yield
    for name in [k for k in sys.modules if _is_shim(k)]:
        del sys.modules[name]
    sys.modules.update(saved_modules)
    sys.meta_path[:] = saved_meta


def _import_shim():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        import otel_helper  # noqa: PLC0415  (the import under test)
    return otel_helper, [w for w in caught if issubclass(w.category, DeprecationWarning)]


class TestDeprecationWarning:
    def test_warns_once_naming_the_new_import(self, fresh_shim):
        _, caught = _import_shim()
        assert len(caught) == 1
        msg = str(caught[0].message)
        assert "import staffops_otel_helper" in msg
        assert "staffops-otel-helper" in msg and "1.0" in msg
        assert "staffops_otel_helper.*" in msg  # logger names moved too
        assert caught[0].filename == __file__  # attributed to the importer, not importlib

    def test_cached_and_submodule_imports_do_not_warn_again(self, fresh_shim):
        _import_shim()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            import otel_helper  # noqa: F401,PLC0415
            import otel_helper.ext.sql  # noqa: F401,PLC0415
            from otel_helper.metrics import metrics_app  # noqa: F401,PLC0415
        assert [w for w in caught if issubclass(w.category, DeprecationWarning)] == []

    def test_new_import_does_not_warn(self):
        # "-W always", not "error": OTel SDK 1.42 on Python 3.10 emits its own
        # DeprecationWarning (importlib.metadata SelectableGroups) on import.
        proc = subprocess.run(
            [sys.executable, "-W", "always::DeprecationWarning", "-c", "import staffops_otel_helper"],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert "is deprecated and will be removed in 1.0" not in proc.stderr

    def test_shown_under_default_filters_for_main(self):
        proc = subprocess.run([sys.executable, "-c", "import otel_helper"],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stderr.count("DeprecationWarning") == 1
        assert "<string>:1: DeprecationWarning: 'otel_helper' is deprecated" in proc.stderr


class TestReExports:
    def test_public_api_is_the_same_objects(self, fresh_shim):
        shim, _ = _import_shim()
        assert shim.__all__ == staffops_otel_helper.__all__
        for name in staffops_otel_helper.__all__:
            assert getattr(shim, name) is getattr(staffops_otel_helper, name), name

    @pytest.mark.parametrize("sub", ["ext.aws", "ext.redis", "ext.sql", "metrics", "config"])
    def test_submodule_import_is_the_same_module(self, fresh_shim, sub):
        _import_shim()
        old = importlib.import_module(f"otel_helper.{sub}")
        new = importlib.import_module(f"staffops_otel_helper.{sub}")
        assert old is new
        assert new.__spec__.name == f"staffops_otel_helper.{sub}"  # spec not clobbered

    def test_from_imports(self, fresh_shim):
        _import_shim()
        from otel_helper import TelemetryOptions, setup_telemetry  # noqa: PLC0415
        from otel_helper.ext.sql import instrument_sql  # noqa: PLC0415
        from otel_helper.metrics import metrics_app  # noqa: PLC0415
        assert TelemetryOptions is staffops_otel_helper.TelemetryOptions
        assert setup_telemetry is staffops_otel_helper.setup_telemetry
        assert instrument_sql is staffops_otel_helper.ext.sql.instrument_sql
        assert metrics_app is staffops_otel_helper.metrics_app

    def test_attribute_falls_back_to_submodule_import(self, fresh_shim, monkeypatch):
        shim, _ = _import_shim()
        assert shim.tracing is staffops_otel_helper.tracing  # attribute of the new package
        monkeypatch.delattr(staffops_otel_helper, "metrics")
        assert shim.metrics is sys.modules["staffops_otel_helper.metrics"]  # via import
        assert "setup_telemetry" in dir(shim) and "tracing" in dir(shim)

    def test_unknown_names_fail_like_a_normal_package(self, fresh_shim):
        shim, _ = _import_shim()
        with pytest.raises(ModuleNotFoundError) as err:
            import otel_helper.nope  # noqa: F401,PLC0415
        assert err.value.name == "otel_helper.nope"
        with pytest.raises(AttributeError, match="module 'otel_helper' has no attribute 'nope'"):
            shim.nope  # noqa: B018

    def test_missing_transitive_dependency_is_not_hidden(self, fresh_shim, monkeypatch):
        shim, _ = _import_shim()

        def broken(name):
            if name == "staffops_otel_helper":
                return staffops_otel_helper
            raise ModuleNotFoundError("No module named 'some_dep'", name="some_dep")

        monkeypatch.setattr(shim, "_importlib", types.SimpleNamespace(import_module=broken))
        finder = next(f for f in sys.meta_path if type(f).__module__ == "otel_helper")
        spec = finder.find_spec("otel_helper.ext.sql")
        with pytest.raises(ModuleNotFoundError) as err:
            spec.loader.create_module(spec)
        assert err.value.name == "some_dep"
        with pytest.raises(ModuleNotFoundError) as err:
            shim.not_an_attribute
        assert err.value.name == "some_dep"

    def test_finder_ignores_other_packages(self, fresh_shim):
        _import_shim()
        finder = next(f for f in sys.meta_path if type(f).__module__ == "otel_helper")
        assert finder.find_spec("otel_helperx") is None
        assert finder.find_spec("json") is None

    def test_reimport_does_not_stack_finders(self, fresh_shim):
        _import_shim()
        del sys.modules["otel_helper"]
        _import_shim()
        assert sum(type(f).__module__ == "otel_helper" for f in sys.meta_path) == 1


class TestReviewFixes:
    """Code-review follow-ups (2026-10-29): M1 race, M2 stubs, L1/L2/L6."""

    def test_concurrent_first_import_of_submodules(self, tmp_path):
        # M1: thread A runs `import otel_helper` while the real package import is
        # slow; threads importing otel_helper.<sub> meanwhile must not fail.
        script = tmp_path / "race.py"
        script.write_text(
            "import sys, threading, time, warnings\n"
            "warnings.simplefilter('ignore')\n"
            "import importlib.abc, importlib.machinery\n"
            "class Slow(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'staffops_otel_helper':\n"
            "            time.sleep(0.5)\n"
            "        return None\n"
            "sys.meta_path.insert(0, Slow())\n"
            "errors = []\n"
            "def first():\n"
            "    import otel_helper  # noqa: F401\n"
            "def sub(name):\n"
            "    try:\n"
            "        __import__(name)\n"
            "        assert sys.modules[name] is sys.modules['staffops_' + name], name\n"
            "    except BaseException as e:\n"
            "        errors.append(f'{name}: {e!r}')\n"
            "t0 = threading.Thread(target=first); t0.start(); time.sleep(0.1)\n"
            "subs = ['otel_helper.ext.sql', 'otel_helper.metrics', 'otel_helper.config',\n"
            "        'otel_helper.ext.redis', 'otel_helper.tracing', 'otel_helper.ext.aws']\n"
            "ts = [threading.Thread(target=sub, args=(n,)) for n in subs * 3]\n"
            "[t.start() for t in ts]; [t.join() for t in ts]; t0.join()\n"
            "print('FAILURES', len(errors)); print('\\n'.join(errors))\n"
        )
        # A deadlock (Python 3.12+ calls find_spec under the global import lock)
        # shows up intermittently, so run it several times; faulthandler dumps
        # the stacks instead of hanging the suite.
        for _ in range(5):
            proc = subprocess.run([sys.executable, "-X", "faulthandler", "-c",
                                   "import faulthandler; faulthandler.dump_traceback_later(20, exit=True); "
                                   f"exec(open({str(script)!r}).read())"],
                                  capture_output=True, text=True, timeout=60)
            assert proc.returncode == 0, proc.stderr
            assert "FAILURES 0" in proc.stdout, proc.stdout + proc.stderr

    @pytest.mark.parametrize("sub", ["config", "metrics", "tracing", "instrumentation", "setup",
                                     "ext/__init__", "ext/aws", "ext/redis", "ext/sql"])
    def test_type_stubs_point_at_the_new_module(self, sub):
        # M2: one stub per submodule users import, star-importing the same module.
        stub = Path(staffops_otel_helper.__file__).parent.parent / "otel_helper" / f"{sub}.pyi"
        target = "staffops_otel_helper." + sub.replace("/__init__", "").replace("/", ".")
        assert f"from {target} import *" in stub.read_text(encoding="utf-8")
        __import__(target)  # the stub's target exists

    def test_logs_the_notice_once_on_the_helper_logger(self, fresh_shim, caplog):
        # L6: visible even when the DeprecationWarning is filtered (non-__main__ importers).
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            _import_shim()
            del sys.modules["otel_helper"]
            _import_shim()
        notices = [r for r in caplog.records if r.name == "staffops_otel_helper.compat"]
        assert len(notices) == 1
        assert "import staffops_otel_helper" in notices[0].getMessage()

    def test_import_module_is_attributed_to_the_caller(self, fresh_shim):
        # L1: on 3.12+ importlib.import_module() frames are skipped too.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            importlib.import_module("otel_helper")
        dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(dep) == 1
        if sys.version_info >= (3, 12):
            assert dep[0].filename == __file__

    def test_running_an_alias_with_dash_m_fails_clearly(self, fresh_shim):
        # L2: runpy asks the loader for code; the alias has none.
        _import_shim()
        finder = next(f for f in sys.meta_path if type(f).__module__ == "otel_helper")
        loader = finder.find_spec("otel_helper.metrics").loader
        with pytest.raises(ImportError, match="python -m staffops_otel_helper.metrics"):
            loader.get_code("otel_helper.metrics")

    def test_shim_does_not_export_its_own_imports(self, fresh_shim):
        # L3: the shim's helpers are private names.
        shim, _ = _import_shim()
        assert not {"sys", "warnings", "importlib", "os", "logging"} & set(vars(shim))
