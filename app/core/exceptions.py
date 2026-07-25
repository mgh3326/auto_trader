"""Shared exception helpers."""

from __future__ import annotations


def describe_exception(exc: BaseException) -> str:
    """Return a non-empty, concrete reason string for an exception.

    httpx timeout exceptions (ReadTimeout / ConnectTimeout / PoolTimeout, ...) are
    frequently constructed with no message, so ``str(exc)`` yields ``""``. Surfacing
    that empty string as a user-facing ``error`` makes timeouts undiagnosable
    (ROB-600). When the message is empty/whitespace, fall back to the exception class
    name so e.g. ``ReadTimeout`` is shown instead of ``""``.

    Consolidates the ``str(exc) or exc.__class__.__name__`` idiom scattered across the
    codebase.
    """
    return str(exc).strip() or type(exc).__name__
