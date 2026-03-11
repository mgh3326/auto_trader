# pyright: reportMissingImports=false
from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest  # type: ignore[reportMissingImports]


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


class _AlwaysFailClaimRedis(_FakeRedis):
    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        if nx:
            return False
        return await super().set(key, value, ex=ex, nx=nx)


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

    service = ScreenerService(redis_client=cast(Any, fake_redis))

    first = await service.list_screening(market="us", limit=20)
    second = await service.list_screening(market="us", limit=20)

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert mock_screen.await_count == 1


@pytest.mark.asyncio
async def test_list_screening_coerces_crypto_volume_sort_to_trade_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [],
            "total_count": 0,
            "returned_count": 0,
            "market": "crypto",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))
    result = await service.list_screening(
        market="crypto",
        sort_by="volume",
        sort_order="desc",
        limit=20,
    )

    assert result["cache_hit"] is False
    await_args = mock_screen.await_args
    assert await_args is not None
    call_kwargs = await_args.kwargs
    assert call_kwargs["market"] == "crypto"
    assert call_kwargs["sort_by"] == "trade_amount"
    assert call_kwargs["sort_order"] == "desc"


@pytest.mark.asyncio
async def test_list_screening_filters_us_by_min_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [
                {"code": "AAPL", "volume": 500},
                {"code": "MSFT", "volume": 1000},
                {"code": "NVDA", "volume": 2500},
                {"code": "TSLA", "volume": 1500},
            ],
            "total_count": 4,
            "returned_count": 4,
            "filters_applied": {"market": "us"},
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))
    result = await service.list_screening(market="us", min_volume=1000, limit=2)

    assert result["cache_hit"] is False
    assert [item["code"] for item in result["results"]] == ["MSFT", "NVDA"]
    assert result["returned_count"] == 2
    assert result["total_count"] == 3
    assert result["filters_applied"]["min_volume"] == 1000

    await_args = mock_screen.await_args
    assert await_args is not None
    call_kwargs = await_args.kwargs
    assert call_kwargs["limit"] == 6


@pytest.mark.asyncio
async def test_list_screening_filters_crypto_by_trade_amount_24h(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [
                {"code": "KRW-BTC", "trade_amount_24h": 900},
                {"code": "KRW-ETH", "trade_amount_24h": 1200},
                {"code": "KRW-XRP"},
                {"code": "KRW-SOL", "trade_amount_24h": 3000},
            ],
            "total_count": 4,
            "returned_count": 4,
            "filters_applied": {"market": "crypto"},
            "market": "crypto",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))
    result = await service.list_screening(market="crypto", min_volume=1000, limit=2)

    assert result["cache_hit"] is False
    assert [item["code"] for item in result["results"]] == ["KRW-ETH", "KRW-SOL"]
    assert result["returned_count"] == 2
    assert result["total_count"] == 2
    assert result["filters_applied"]["min_volume"] == 1000

    await_args = mock_screen.await_args
    assert await_args is not None
    call_kwargs = await_args.kwargs
    assert call_kwargs["limit"] == 6


@pytest.mark.asyncio
async def test_list_screening_uses_separate_cache_keys_for_min_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [{"code": "AAPL", "volume": 1000}],
            "total_count": 1,
            "returned_count": 1,
            "filters_applied": {"market": "us"},
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))

    first = await service.list_screening(market="us", min_volume=1000, limit=1)
    second = await service.list_screening(market="us", min_volume=2000, limit=1)
    third = await service.list_screening(market="us", min_volume=1000, limit=1)

    assert first["cache_hit"] is False
    assert second["cache_hit"] is False
    assert third["cache_hit"] is True
    assert mock_screen.await_count == 2


@pytest.mark.asyncio
async def test_list_screening_uses_separate_cache_keys_for_new_fundamentals_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [{"code": "AAPL", "sector": "Technology"}],
            "total_count": 1,
            "returned_count": 1,
            "filters_applied": {"market": "us"},
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))

    first = await cast(Any, service).list_screening(
        market="us",
        sector="Technology",
        min_analyst_buy=5,
        min_dividend=2.0,
        limit=1,
    )
    second = await cast(Any, service).list_screening(
        market="us",
        sector="Healthcare",
        min_analyst_buy=5,
        min_dividend=2.0,
        limit=1,
    )
    third = await cast(Any, service).list_screening(
        market="us",
        sector="Technology",
        min_analyst_buy=6,
        min_dividend=2.0,
        limit=1,
    )
    fourth = await cast(Any, service).list_screening(
        market="us",
        sector="Technology",
        min_analyst_buy=5,
        min_dividend=3.0,
        limit=1,
    )
    fifth = await cast(Any, service).list_screening(
        market="us",
        sector="Technology",
        min_analyst_buy=5,
        min_dividend=2.0,
        limit=1,
    )

    assert first["cache_hit"] is False
    assert second["cache_hit"] is False
    assert third["cache_hit"] is False
    assert fourth["cache_hit"] is False
    assert fifth["cache_hit"] is True
    assert mock_screen.await_count == 4


@pytest.mark.asyncio
async def test_list_screening_overfetches_when_post_screen_analyst_filtering_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [
                {"code": "AAPL", "analyst_buy": 12},
                {"code": "MSFT", "analyst_buy": 10},
                {"code": "NVDA", "analyst_buy": 8},
            ],
            "total_count": 3,
            "returned_count": 3,
            "filters_applied": {"market": "us"},
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))
    result = await cast(Any, service).list_screening(
        market="us", min_analyst_buy=10, limit=2
    )

    assert result["cache_hit"] is False
    await_args = mock_screen.await_args
    assert await_args is not None
    assert await_args.kwargs["market"] == "us"
    assert await_args.kwargs["min_analyst_buy"] == 10
    assert await_args.kwargs["limit"] == 6


@pytest.mark.asyncio
async def test_list_screening_rejects_negative_min_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(return_value={})
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))

    with pytest.raises(ValueError, match="min_volume must be >= 0"):
        await service.list_screening(market="us", min_volume=-1, limit=20)

    assert mock_screen.await_count == 0


@pytest.mark.asyncio
async def test_list_screening_min_volume_overfetch_caps_at_100(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [{"code": "AAPL", "volume": 1000}],
            "total_count": 1,
            "returned_count": 1,
            "filters_applied": {"market": "us"},
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=fake_redis)
    result = await service.list_screening(market="us", min_volume=1000, limit=80)

    assert result["cache_hit"] is False
    await_args = mock_screen.await_args
    assert await_args is not None
    call_kwargs = await_args.kwargs
    # With limit=80, overfetch should be min(100, max(80*3, 80)) = min(100, 240) = 100
    assert call_kwargs["limit"] == 100


@pytest.mark.asyncio
async def test_refresh_screening_invalidates_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        side_effect=[
            {
                "results": [{"code": "AAPL", "volume": 1200}],
                "total_count": 1,
                "returned_count": 1,
                "market": "us",
            },
            {
                "results": [{"code": "MSFT", "volume": 1500}],
                "total_count": 1,
                "returned_count": 1,
                "market": "us",
            },
        ]
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))

    first = await service.list_screening(market="us", min_volume=1000, limit=5)
    cached = await service.list_screening(market="us", min_volume=1000, limit=5)
    refreshed = await service.refresh_screening(market="us", min_volume=1000, limit=5)
    recached = await service.list_screening(market="us", min_volume=1000, limit=5)

    assert first["results"][0]["code"] == "AAPL"
    assert cached["cache_hit"] is True
    assert refreshed["results"][0]["code"] == "MSFT"
    assert refreshed["cache_hit"] is False
    assert recached["cache_hit"] is True
    assert mock_screen.await_count == 2


@pytest.mark.asyncio
async def test_request_report_reuses_inflight_job() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    openclaw = AsyncMock()
    openclaw.request_analysis = AsyncMock(return_value="job-1")

    service = ScreenerService(
        redis_client=cast(Any, fake_redis), openclaw_client=openclaw
    )

    first = await service.request_report(market="us", symbol="AAPL", name="Apple")
    second = await service.request_report(market="us", symbol="AAPL", name="Apple")

    assert first["job_id"] == "job-1"
    assert first["is_reused"] is False
    assert second["job_id"] == "job-1"
    assert second["is_reused"] is True
    openclaw.request_analysis.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_report_concurrent_same_symbol_single_dispatch() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()

    async def delayed_request_analysis(**kwargs: object) -> str:
        await asyncio.sleep(0.01)
        return str(kwargs["request_id"])

    openclaw = AsyncMock()
    openclaw.request_analysis = AsyncMock(side_effect=delayed_request_analysis)

    service = ScreenerService(
        redis_client=cast(Any, fake_redis), openclaw_client=openclaw
    )

    first, second = await asyncio.gather(
        service.request_report(market="us", symbol="AAPL", name="Apple"),
        service.request_report(market="us", symbol="AAPL", name="Apple"),
    )

    assert first["job_id"] == second["job_id"]
    assert {first["is_reused"], second["is_reused"]} == {False, True}
    openclaw.request_analysis.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_report_returns_failed_when_inflight_claim_is_unavailable() -> (
    None
):
    from app.services.screener_service import ScreenerService

    fake_redis = _AlwaysFailClaimRedis()
    service = ScreenerService(redis_client=cast(Any, fake_redis))

    result = await service.request_report(market="us", symbol="AAPL", name="Apple")

    assert result["status"] == "failed"
    assert result["is_reused"] is False
    assert result["error"] == "inflight_job_unavailable"

    status = await service.get_report_status(result["job_id"])
    assert status["status"] == "failed"
    assert status["error"] == "inflight_job_unavailable"


@pytest.mark.asyncio
async def test_request_report_does_not_downgrade_completed_status() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    openclaw = AsyncMock()
    openclaw.request_analysis = AsyncMock(return_value="job-race")

    service = ScreenerService(
        redis_client=cast(Any, fake_redis), openclaw_client=openclaw
    )
    fake_redis.store["screener:report:status:job-race"] = "completed"

    result = await service.request_report(market="us", symbol="AAPL", name="Apple")

    assert result["job_id"] == "job-race"
    assert result["status"] == "completed"
    assert fake_redis.store["screener:report:status:job-race"] == "completed"


@pytest.mark.asyncio
async def test_get_report_status_marks_running_when_inflight_exists() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    openclaw = AsyncMock()
    openclaw.request_analysis = AsyncMock(return_value="job-running")

    service = ScreenerService(
        redis_client=cast(Any, fake_redis), openclaw_client=openclaw
    )

    queued = await service.request_report(market="us", symbol="AAPL", name="Apple")
    status = await service.get_report_status(queued["job_id"])

    assert queued["status"] == "queued"
    assert status["status"] == "running"
    assert fake_redis.store["screener:report:status:job-running"] == "running"


@pytest.mark.asyncio
async def test_get_report_status_unknown_job_returns_not_found_failed() -> None:
    from app.services.screener_service import ScreenerService

    service = ScreenerService(redis_client=cast(Any, _FakeRedis()))

    result = await service.get_report_status("missing-job")

    assert result == {
        "job_id": "missing-job",
        "status": "failed",
        "error": "job_not_found",
        "not_found": True,
    }


@pytest.mark.asyncio
async def test_callback_completes_job_and_reuses_report() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    openclaw = AsyncMock()
    openclaw.request_analysis = AsyncMock(return_value="job-2")

    service = ScreenerService(
        redis_client=cast(Any, fake_redis), openclaw_client=openclaw
    )

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

    service = ScreenerService(
        redis_client=cast(Any, fake_redis), openclaw_client=openclaw
    )

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
    service = ScreenerService(redis_client=cast(Any, fake_redis))

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
async def test_callback_payload_mismatch_marks_failed_and_clears_inflight() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    openclaw = AsyncMock()
    openclaw.request_analysis = AsyncMock(return_value="job-mismatch")
    service = ScreenerService(
        redis_client=cast(Any, fake_redis), openclaw_client=openclaw
    )

    queued = await service.request_report(market="us", symbol="AAPL", name="Apple")
    assert queued["status"] == "queued"

    callback_result = await service.process_callback(
        {
            "request_id": "job-mismatch",
            "symbol": "MSFT",
            "name": "Microsoft",
            "instrument_type": "equity_us",
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

    assert callback_result["status"] == "failed"
    assert "callback_payload_mismatch" in callback_result["error"]
    assert "screener:report:inflight:us:AAPL" not in fake_redis.store
    assert "screener:report:result:us:AAPL" not in fake_redis.store

    status = await service.get_report_status("job-mismatch")
    assert status["status"] == "failed"
    assert "callback_payload_mismatch" in status["error"]


@pytest.mark.asyncio
async def test_place_order_confirm_maps_to_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_place = AsyncMock(return_value={"success": True})
    monkeypatch.setattr("app.services.screener_service._place_order_impl", mock_place)

    service = ScreenerService(redis_client=cast(Any, fake_redis))

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
