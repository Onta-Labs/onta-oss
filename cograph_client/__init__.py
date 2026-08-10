"""Deprecated import path for the Infona OSS client.

Prefer::

    import infona_client

This package is a thin compatibility alias. Prefer the ``infona_client``
import path in all new code. A meta-path finder (installed by
``infona_client``) maps ``cograph_client.*`` onto ``infona_client.*``.
"""
from __future__ import annotations

import sys
import warnings

warnings.warn(
    "The 'cograph_client' import path is deprecated; use 'infona_client' instead.",
    DeprecationWarning,
    stacklevel=2,
)

import infona_client as _real  # noqa: E402
from infona_client._legacy_alias import install as _install  # noqa: E402

_install()
sys.modules[__name__] = _real
