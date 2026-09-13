"""Read-only token seam for a future context consumer; no issuer ownership."""

from __future__ import annotations

from typing import Protocol

__all__ = [
    "CachedTokenUnavailable",
    "ReadOnlyCachedTokenProvider",
    "read_cached_token",
]


class ReadOnlyCachedTokenProvider(Protocol):
    """The non-owner contract intentionally has no issue or refresh method."""

    async def get_cached_access_token(self) -> str | None: ...


class CachedTokenUnavailable(RuntimeError):
    """A non-owner fails closed rather than asking any issuer to refresh."""


async def read_cached_token(provider: ReadOnlyCachedTokenProvider) -> str:
    """Read a usable cached token; never issue, refresh, or force reissue."""
    token = await provider.get_cached_access_token()
    if not isinstance(token, str) or not token.strip():
        raise CachedTokenUnavailable("read-only cached Toss token is unavailable")
    return token
