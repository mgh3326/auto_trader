"""Tests that build_screener_results emits a user-facing warning for US missing/stale snapshots.

ROB-204: Ensures the view-model layer surfaces a Korean user-facing string when the
US screener snapshot state is missing or stale, so the React UI can show it to users.
"""

from __future__ import annotations

import pytest

from app.services.invest_view_model.screener_service import (
    _US_SCREENER_DATA_NOT_READY_WARNING,
    build_screener_results,
)


class _Resolver:
    def relation(self, market: str, symbol: str) -> str:
        return "none"


class _EmptyScreener:
    async def list_screening(self, **kwargs) -> dict:
        return {
            "results": [],
            "warnings": [],
            "timestamp": "2026-05-12T00:00:00+00:00",
            "cache_hit": False,
        }


@pytest.mark.asyncio
async def test_us_consecutive_gainers_missing_emits_user_facing_warning() -> None:
    response = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=_EmptyScreener(),
        resolver=_Resolver(),
        market="us",
        session=None,
    )
    assert response.freshness.dataState == "missing"
    assert any("미국 스크리너 데이터 준비중" in w for w in response.warnings), (
        f"Expected US warning in {response.warnings!r}"
    )


@pytest.mark.asyncio
async def test_kr_consecutive_gainers_missing_does_not_emit_us_warning() -> None:
    response = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=_EmptyScreener(),
        resolver=_Resolver(),
        market="kr",
        session=None,
    )
    assert not any("미국 스크리너" in w for w in response.warnings), (
        f"Unexpected US warning in KR response: {response.warnings!r}"
    )


@pytest.mark.asyncio
async def test_us_missing_warning_constant_is_the_expected_string() -> None:
    assert "미국 스크리너 데이터 준비중" in _US_SCREENER_DATA_NOT_READY_WARNING
    assert "일부 결과만 표시됩니다" in _US_SCREENER_DATA_NOT_READY_WARNING
