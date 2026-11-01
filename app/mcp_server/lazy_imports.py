"""Defer heavy third-party imports in low-traffic MCP processes (ROB-1436).

Every MCP unit is its own Python process, and the tooling package imports
pandas, numpy, yfinance and the market-calendar libraries at module level. On
the low-traffic closed-world units (kiwoom, h3-*) that costs about 100 MB of
resident memory per process before any tool is called.

``install_lazy_imports`` puts a placeholder module in ``sys.modules`` for each
of those packages before the tooling package is imported. ``import pandas as
pd`` then binds the placeholder; the real package is imported on the first
attribute access (the first tool call that uses it) and every later access is
forwarded to it. The package that runs is the same one an eager import would
have run, so tool output does not change. DEFAULT and every other profile
stay eager.

Why not ``importlib.util.LazyLoader``: since Python 3.12 every ``import x``
statement reads ``x.__spec__`` to check for a partially initialised module,
and a LazyLoader module loads on that read, so the second importer forces the
load at boot. The placeholder answers ``__spec__`` from its own dict.

Trade-off: an import error in one of these packages surfaces on the first
tool call that touches it instead of at boot. The DEFAULT units run the same
image eagerly, so a broken dependency still fails a deploy health check.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import threading
import types
from typing import Any

from app.mcp_server.profiles import McpProfile

logger = logging.getLogger(__name__)

# Top-level packages only. A submodule import (``import pandas.api.types``)
# reads the placeholder's ``__path__``, which loads the real package first.
LAZY_MODULES: tuple[str, ...] = (
    "pandas",
    "numpy",
    "yfinance",
    "exchange_calendars",
    "finnhub",
)

# Low-traffic units (Sentry tools/call, 30 days: kiwoom 365, h3-crypto-paper
# 1,168, h3-us-paper 281, against 11,732 on DEFAULT).
LAZY_IMPORT_PROFILES: frozenset[McpProfile] = frozenset(
    {
        McpProfile.KIWOOM,
        McpProfile.KIWOOM_KR,
        McpProfile.H3_CRYPTO_PAPER,
        McpProfile.H3_US_PAPER,
    }
)

_LOAD_LOCK = threading.RLock()


class _DeferredModule(types.ModuleType):
    """Placeholder that imports the real module on first attribute access."""

    def __getattr__(self, attr: str) -> Any:
        # Only reached for names missing from the placeholder's own dict
        # (__name__, __spec__ and friends live there), so the import
        # machinery's __spec__ check never triggers a load.
        return getattr(_load(self), attr)

    def __dir__(self) -> list[str]:
        return dir(_load(self))


def _load(placeholder: _DeferredModule) -> types.ModuleType:
    name = placeholder.__name__
    with _LOAD_LOCK:
        real = placeholder.__dict__.get("_deferred_real")
        if real is not None:
            return real
        if sys.modules.get(name) is placeholder:
            del sys.modules[name]
        try:
            real = importlib.import_module(name)
        except BaseException:
            sys.modules.setdefault(name, placeholder)
            raise
        placeholder.__dict__["_deferred_real"] = real
        return real


def _install_one(name: str) -> bool:
    existing = sys.modules.get(name)
    if existing is not None:
        if not isinstance(existing, _DeferredModule):
            logger.warning("lazy import skipped, already imported: %s", name)
        return False
    spec = importlib.util.find_spec(name)
    if spec is None:
        return False
    placeholder = _DeferredModule(name)
    placeholder.__spec__ = spec
    sys.modules[name] = placeholder
    return True


def is_deferred(name: str) -> bool:
    """True while ``name`` is a placeholder whose real module has not loaded."""
    module = sys.modules.get(name)
    return isinstance(module, _DeferredModule) and (
        "_deferred_real" not in module.__dict__
    )


def install_lazy_imports(profile: McpProfile) -> tuple[str, ...]:
    """Defer LAZY_MODULES for a low-traffic profile; return what changed.

    Call before importing ``app.mcp_server.tooling``. A package that is
    already imported, or not installed, is left as it is.
    """
    if profile not in LAZY_IMPORT_PROFILES:
        return ()
    return tuple(name for name in LAZY_MODULES if _install_one(name))


__all__ = [
    "LAZY_IMPORT_PROFILES",
    "LAZY_MODULES",
    "install_lazy_imports",
    "is_deferred",
]
