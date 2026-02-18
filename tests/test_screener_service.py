from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        if nx and key in self.store:
            return False
        _ = ex
        self.store[key] = value
        return True

    async def setex(self, key: str, ttl: int, value: str):
        _ = ttl
        self.store[key] = value
        return True

    async def delete(self, key: str):
        self.store.pop(key, None)
        return 1


@pytest.mark.asyncio
async def test_list_screening_uses_5m_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [{"code": "AAPL", "name": "Apple"}],
            "total_count": 1,
            "returned_count": 1,
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=fake_redis)

    first = await service.list_screening(market="us", limit=20)
    second = await service.list_screening(market="us", limit=20)

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert mock_screen.await_count == 1


@pytest.mark.asyncio
async def test_refresh_screening_invalidates_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        side_effect=[
            {
                "results": [{"code": "AAPL"}],
                "total_count": 1,
                "returned_count": 1,
                "market": "us",
            },
            {
                "results": [{"code": "MSFT"}],
                "total_count": 1,
                "returned_count": 1,
                "market": "us",
            },
        ]
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=fake_redis)

    first = await service.list_screening(market="us", limit=20)
    refreshed = await service.refresh_screening(market="us", limit=20)

    assert first["results"][0]["code"] == "AAPL"
    assert refreshed["results"][0]["code"] == "MSFT"
    assert refreshed["cache_hit"] is False
    assert mock_screen.await_count == 2


@pytest.mark.asyncio
async def test_request_report_reuses_inflight_job() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    openclaw = AsyncMock()
    openclaw.request_analysis = AsyncMock(return_value="job-1")

    service = ScreenerService(redis_client=fake_redis, openclaw_client=openclaw)

    first = await service.request_report(market="us", symbol="AAPL", name="Apple")
    second = await service.request_report(market="us", symbol="AAPL", name="Apple")

    assert first["job_id"] == "job-1"
    assert first["is_reused"] is False
    assert second["job_id"] == "job-1"
    assert second["is_reused"] is True
    openclaw.request_analysis.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_completes_job_and_reuses_report() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    openclaw = AsyncMock()
    openclaw.request_analysis = AsyncMock(return_value="job-2")

    service = ScreenerService(redis_client=fake_redis, openclaw_client=openclaw)

    queued = await service.request_report(market="us", symbol="AAPL", name="Apple")
    assert queued["status"] == "queued"

    callback_payload = {
        "request_id": "job-2",
        "symbol": "AAPL",
        "name": "Apple",
        "instrument_type": "equity_us",
        "decision": "hold",
        "confidence": 60,
        "reasons": ["range"],
        "price_analysis": {
            "appropriate_buy_range": {"min": 100, "max": 110},
            "appropriate_sell_range": {"min": 120, "max": 130},
            "buy_hope_range": {"min": 95, "max": 98},
            "sell_target_range": {"min": 150, "max": 160},
        },
        "detailed_text": "done",
    }
    callback_result = await service.process_callback(callback_payload)
    status = await service.get_report_status("job-2")
    reused = await service.request_report(market="us", symbol="AAPL", name="Apple")

    assert callback_result["status"] == "ok"
    assert status["status"] == "completed"
    assert status["report"]["decision"] == "hold"
    assert reused["status"] == "completed"
    assert reused["is_reused"] is True
    openclaw.request_analysis.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_report_marks_failed_when_openclaw_request_fails() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    openclaw = AsyncMock()
    openclaw.request_analysis = AsyncMock(side_effect=RuntimeError("openclaw down"))

    service = ScreenerService(redis_client=fake_redis, openclaw_client=openclaw)

    result = await service.request_report(market="us", symbol="AAPL", name="Apple")

    assert result["status"] == "failed"
    assert result["is_reused"] is False
    assert "openclaw down" in result["error"]

    status = await service.get_report_status(result["job_id"])
    assert status["status"] == "failed"
    assert "openclaw down" in status["error"]


@pytest.mark.asyncio
async def test_callback_with_unknown_instrument_type_marks_failed() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    service = ScreenerService(redis_client=fake_redis)

    result = await service.process_callback(
        {
            "request_id": "job-unknown-type",
            "symbol": "AAPL",
            "name": "Apple",
            "instrument_type": "equity_jp",
            "decision": "hold",
            "confidence": 55,
            "reasons": ["r1"],
            "price_analysis": {
                "appropriate_buy_range": {"min": 100, "max": 110},
                "appropriate_sell_range": {"min": 120, "max": 130},
                "buy_hope_range": {"min": 95, "max": 98},
                "sell_target_range": {"min": 150, "max": 160},
            },
            "detailed_text": "report",
        }
    )

    assert result["status"] == "failed"
    assert "instrument_type must be one of" in result["error"]

    status = await service.get_report_status("job-unknown-type")
    assert status["status"] == "failed"
    assert "instrument_type must be one of" in status["error"]


@pytest.mark.asyncio
async def test_place_order_confirm_maps_to_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_place = AsyncMock(return_value={"success": True})
    monkeypatch.setattr("app.services.screener_service._place_order_impl", mock_place)

    service = ScreenerService(redis_client=fake_redis)

    await service.place_order(
        market="us",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=1,
        price=100,
        confirm=False,
    )
    await service.place_order(
        market="us",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=1,
        price=100,
        confirm=True,
    )

    first_kwargs = mock_place.await_args_list[0].kwargs
    second_kwargs = mock_place.await_args_list[1].kwargs
    assert first_kwargs["dry_run"] is True
    assert second_kwargs["dry_run"] is False
    assert first_kwargs["market"] == "us"
    assert second_kwargs["market"] == "us"
