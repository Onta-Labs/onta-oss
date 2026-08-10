"""Register ``cograph_client`` as a deprecated alias of ``infona_client``.

Imported from ``infona_client.__init__`` so the meta-path finder is installed
as soon as the real package is loaded.

Legacy callers that only ever import ``cograph_client.*`` get the real
``infona_client.*`` module objects (identity-preserving). Prefer migrating
imports to ``infona_client``.
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
import warnings
from types import ModuleType

_REAL = "infona_client"
_ALIAS = "cograph_client"
_warned = False


def _warn_once() -> None:
    global _warned
    if _warned:
        return
    _warned = True
    warnings.warn(
        "The 'cograph_client' import path is deprecated; use 'infona_client' instead.",
        DeprecationWarning,
        stacklevel=3,
    )


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, real: ModuleType) -> None:
        self._real = real

    def create_module(self, spec) -> ModuleType:  # noqa: ANN001
        return self._real

    def exec_module(self, module: ModuleType) -> None:  # noqa: ARG002
        return

    def load_module(self, fullname: str) -> ModuleType:  # noqa: ARG002
        return self._real


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):  # noqa: ANN001, ARG002
        if fullname != _ALIAS and not fullname.startswith(_ALIAS + "."):
            return None
        _warn_once()
        real_name = _REAL + fullname[len(_ALIAS) :]
        # Already published under the alias? Reuse.
        existing = sys.modules.get(fullname)
        if existing is not None and getattr(existing, "__name__", "").startswith(_REAL):
            return importlib.util.spec_from_loader(
                fullname,
                _AliasLoader(existing),
                origin=getattr(existing, "__file__", None),
                is_package=hasattr(existing, "__path__"),
            )
        real = importlib.import_module(real_name)
        sys.modules[fullname] = real
        return importlib.util.spec_from_loader(
            fullname,
            _AliasLoader(real),
            origin=getattr(real, "__file__", None),
            is_package=hasattr(real, "__path__"),
        )


def install() -> None:
    """Idempotently install the ``cograph_client`` → ``infona_client`` alias finder."""
    if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _AliasFinder())
    real = sys.modules.get(_REAL)
    if real is not None:
        sys.modules.setdefault(_ALIAS, real)
