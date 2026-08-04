from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parents[2] / "research" / "kr_backfill"


@pytest.fixture
def runtime_module():
    sys.path.insert(0, str(MODULE_DIR))
    try:
        yield importlib.import_module("surface_runtime")
    finally:
        sys.modules.pop("surface_runtime", None)
        sys.path.remove(str(MODULE_DIR))


@pytest.mark.asyncio
async def test_kis_live_token_wrapper_never_refreshes_or_clears(runtime_module) -> None:
    class Delegate:
        async def get_token(self, **_kwargs: object) -> str:
            return "cached-token"

    wrapper = runtime_module.ReuseOnlyTokenManager(Delegate())

    assert await wrapper.get_token(force_redis_check=True) == "cached-token"
    with pytest.raises(
        runtime_module.SurfaceRuntimeError,
        match="KIS_LIVE_TOKEN_CACHE_MISS_STOP",
    ):
        await wrapper.refresh_token_with_lock(object())
    with pytest.raises(
        runtime_module.SurfaceRuntimeError,
        match="KIS_LIVE_TOKEN_REJECTED_STOP",
    ):
        await wrapper.clear_token()


@pytest.mark.parametrize(
    ("surface", "source"),
    [
        ("kiwoom_mock", "kiwoom"),
        ("kiwoom_live", "kiwoom"),
        ("kis_mock", "kis"),
        ("kis_live", "kis"),
    ],
)
def test_surface_source_is_explicit(runtime_module, surface: str, source: str) -> None:
    assert runtime_module.source_for_surface(surface) == source


@pytest.mark.parametrize(
    "message",
    [
        "HTTPStatusError: 401 Unauthorized",
        "HTTPStatusError: 429 Too Many Requests",
        "RateLimitExceededError: rate limit retries exhausted",
    ],
)
def test_kis_live_failure_classifier_is_fail_closed(
    runtime_module, message: str
) -> None:
    assert runtime_module.is_kis_live_immediate_stop(RuntimeError(message))
