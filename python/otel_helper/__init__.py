"""Deprecated compatibility module: ``otel_helper`` is now ``staffops_otel_helper``.

ADR-011 renamed the Python package (distribution ``staffops-otel-helper``,
import ``staffops_otel_helper``). This module keeps the old import working
during 0.3.x and is removed in 1.0:

- ``import otel_helper`` re-exports the public API of ``staffops_otel_helper``,
  emits one ``DeprecationWarning`` naming the new import and logs the same
  notice once on the ``staffops_otel_helper.compat`` logger (design D8);
- ``otel_helper.<sub>`` (``otel_helper.ext.sql``, ``otel_helper.metrics``, ...)
  resolves to the **same module object** as ``staffops_otel_helper.<sub>``, so
  the helper keeps one copy of its state whichever name the caller uses.

Helper logger names are not aliased: diagnostics are emitted on the
``staffops_otel_helper.*`` loggers only.
"""

from __future__ import annotations

import importlib as _importlib
import importlib.machinery as _machinery
import logging as _logging
import os as _os
import sys as _sys
import warnings as _warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType
    from typing import Any, Sequence

_OLD = __name__
_NEW = "staffops_otel_helper"
_MESSAGE = (
    f"'{_OLD}' is deprecated and will be removed in 1.0: install 'staffops-otel-helper' "
    f"and use 'import {_NEW}' (helper loggers are now '{_NEW}.*')."
)


class _AliasLoader:
    """Loads ``otel_helper.<sub>`` as the ``staffops_otel_helper.<sub>`` module object.

    The real module is imported in :meth:`create_module`, not in
    ``find_spec``: Python 3.12+ calls ``find_spec`` while holding the global
    import lock, and importing there deadlocks against a thread that is still
    initializing ``staffops_otel_helper`` (review M1). ``create_module`` runs
    under the alias's own module lock only.
    """

    def __init__(self, target: str) -> None:
        self._target = target
        self._spec: _machinery.ModuleSpec | None = None

    def create_module(self, spec: _machinery.ModuleSpec) -> ModuleType:
        # Wait until the top-level package is fully initialized first: a
        # submodule import skips the wait when its parent is already in
        # sys.modules, and racing another thread's package __init__ through
        # its own submodule imports raises _DeadlockError (review M1).
        _importlib.import_module(_NEW)
        try:
            module = _importlib.import_module(self._target)
        except ModuleNotFoundError as err:
            if err.name == self._target:
                raise ModuleNotFoundError(f"No module named '{spec.name}'", name=spec.name) from None
            raise
        self._spec = module.__spec__
        return module

    def exec_module(self, module: ModuleType) -> None:
        """Nothing to run: the target is already executed.

        ``module_from_spec`` overwrote its ``__spec__`` with the alias spec;
        put the original back so ``staffops_otel_helper.<sub>`` stays intact.
        """
        module.__spec__ = self._spec

    def get_code(self, fullname: str) -> None:
        """``python -m otel_helper.<sub>`` / runpy: there is no code to run here."""
        raise ImportError(
            f"'{fullname}' is an alias of '{self._target}' and cannot be run; "
            f"use 'python -m {self._target}'",
            name=fullname,
        )


class _AliasFinder:
    """Maps ``otel_helper.<sub>`` imports onto ``staffops_otel_helper.<sub>``.

    Never imports (see :class:`_AliasLoader`); a name with no
    ``staffops_otel_helper`` counterpart fails at load time with
    ``No module named 'otel_helper.<sub>'``.
    """

    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None, target: ModuleType | None = None,
    ) -> _machinery.ModuleSpec | None:
        if not fullname.startswith(_OLD + "."):
            return None
        return _machinery.ModuleSpec(fullname, _AliasLoader(_NEW + fullname[len(_OLD):]))


def _install_finder() -> None:
    # Before importing the new package (review M1): a thread importing
    # otel_helper.<sub> while this module is still initializing must find the alias.
    if not any(type(f).__qualname__ == _AliasFinder.__qualname__ and type(f).__module__ == _OLD
               for f in _sys.meta_path):
        _sys.meta_path.insert(0, _AliasFinder())


def _warn() -> None:
    """One DeprecationWarning attributed to the code that imported this module.

    ``stacklevel`` cannot name that frame reliably: the importer may sit behind
    frozen ``importlib._bootstrap`` frames and ``importlib.import_module``, and
    ``skip_file_prefixes`` (3.12+) does not skip the module body. So walk the
    stack and use ``warn_explicit`` with the importer's file, line and module —
    the default ``__main__`` filter then shows it for scripts.
    """
    importlib_dir = _os.path.dirname(_importlib.__file__) + _os.sep
    frame = _sys._getframe(1)
    while frame is not None and (frame.f_code.co_filename.startswith(("<frozen importlib", importlib_dir))
                                 or frame.f_code.co_filename == __file__):
        frame = frame.f_back
    if frame is None:  # pragma: no cover - always an importer frame in practice
        _warnings.warn(_MESSAGE, DeprecationWarning, stacklevel=2)
        return
    module_globals = frame.f_globals
    _warnings.warn_explicit(
        _MESSAGE, DeprecationWarning, frame.f_code.co_filename, frame.f_lineno,
        module=module_globals.get("__name__", "<unknown>"),
        registry=module_globals.setdefault("__warningregistry__", {}),
    )


_warn()
_install_finder()

_real = _importlib.import_module(_NEW)

from staffops_otel_helper import *  # noqa: E402,F401,F403  (re-export; __all__ below)
from staffops_otel_helper import _diag  # noqa: E402

# DeprecationWarning is hidden unless the import is in __main__ (e.g. an app
# module loaded by uvicorn); the helper logger makes the notice visible (D8).
_diag.warn_once_per_process(_logging.getLogger(f"{_NEW}.compat"), "import:otel_helper", _MESSAGE)
del _diag

__all__ = list(_real.__all__)


def __getattr__(name: str) -> Any:
    """Resolve any other attribute (a submodule such as ``metrics``) on the new package."""
    try:
        return getattr(_real, name)
    except AttributeError:
        pass
    try:
        return _importlib.import_module(f"{_OLD}.{name}")
    except ModuleNotFoundError as err:
        if err.name != f"{_OLD}.{name}":
            raise
        raise AttributeError(f"module '{_OLD}' has no attribute '{name}'") from None


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_real)))
