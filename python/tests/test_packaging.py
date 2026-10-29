"""P17: typed package, public OpenTelemetry APIs only."""

import ast
from importlib import resources
from pathlib import Path

import staffops_otel_helper

_PKG_DIR = Path(staffops_otel_helper.__file__).parent


def test_ships_py_typed_marker():
    """P17: the package (and the otel_helper compatibility module) ships py.typed."""
    assert resources.files("staffops_otel_helper").joinpath("py.typed").is_file()
    # ADR-011 compatibility module (removed in 1.0), shipped next to it in the wheel.
    assert (_PKG_DIR.parent / "otel_helper" / "py.typed").is_file()


def test_no_opentelemetry_internal_imports():
    """P17: no import from an opentelemetry.*._internal module."""
    offenders = []
    for path in sorted(_PKG_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            for module in modules:
                parts = module.split(".")
                if parts[0] == "opentelemetry" and "_internal" in parts:
                    offenders.append(f"{path.relative_to(_PKG_DIR)}:{node.lineno} {module}")
    assert offenders == []
