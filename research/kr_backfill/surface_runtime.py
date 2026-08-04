"""Fail-closed runtime bindings for individually owned backfill surfaces."""

from __future__ import annotations

from typing import Any

SURFACE_SOURCES = {
    "kiwoom_mock": "kiwoom",
    "kiwoom_live": "kiwoom",
    "kis_mock": "kis",
    "kis_live": "kis",
}


class SurfaceRuntimeError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        self.retry_disposition = "STOP_NO_RETRY"
        super().__init__(reason_code)


class ReuseOnlyTokenManager:
    """Allow KIS live Redis token reads, but never issuance or cache mutation."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    async def get_token(self, *args: Any, **kwargs: Any) -> str | None:
        return await self._delegate.get_token(*args, **kwargs)

    async def refresh_token_with_lock(self, _token_fetcher: Any) -> str:
        raise SurfaceRuntimeError("KIS_LIVE_TOKEN_CACHE_MISS_STOP")

    async def clear_token(self) -> None:
        raise SurfaceRuntimeError("KIS_LIVE_TOKEN_REJECTED_STOP")


def source_for_surface(surface_id: str) -> str:
    try:
        return SURFACE_SOURCES[surface_id]
    except KeyError as exc:
        raise SurfaceRuntimeError(f"UNKNOWN_SURFACE:{surface_id}") from exc


async def build_kis_surface_client(surface_id: str) -> Any:
    """Build one KIS market-data client with surface-specific token policy."""
    if surface_id not in {"kis_mock", "kis_live"}:
        raise SurfaceRuntimeError(f"NOT_A_KIS_SURFACE:{surface_id}")

    from app.services.brokers.kis.client import KISClient

    client = KISClient(is_mock=surface_id == "kis_mock")
    if surface_id == "kis_live":
        from app.services.redis_token_manager import redis_token_manager

        token = await redis_token_manager.get_token(force_redis_check=True)
        if not token:
            raise SurfaceRuntimeError("KIS_LIVE_TOKEN_CACHE_MISS_STOP")
        client._settings.kis_access_token = token
        client._token_manager = ReuseOnlyTokenManager(redis_token_manager)
    return client


def is_kis_live_immediate_stop(exc: BaseException) -> bool:
    """KIS live 401/429/auth failures stop this pipe without retrying here."""
    reason = str(getattr(exc, "reason_code", ""))
    message = f"{type(exc).__name__}: {exc}".lower()
    return bool(
        reason.startswith("KIS_LIVE_TOKEN_")
        or " 401" in message
        or "401 " in message
        or " 429" in message
        or "429 " in message
        or "ratelimitexceeded" in message
        or "rate limit retries exhausted" in message
    )
