import pytest

from app.services.live_place_provenance import publish_place_time_forecast


@pytest.mark.integration
@pytest.mark.asyncio
async def test_buy_with_target_publishes_at_or_above_forecast(monkeypatch):
    captured = {}

    async def fake_save_forecast(db, **kwargs):
        captured.update(kwargs)

        class _FC:
            forecast_id = "fc-123"

        return "created", _FC()

    monkeypatch.setattr(
        "app.services.live_place_provenance.save_forecast", fake_save_forecast
    )

    fid = await publish_place_time_forecast(
        correlation_id="live:kis_live:abc",
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        target_price=80000.0,
        min_hold_days=None,
        session_label="kis_live_place",
        created_by="auto_place_live",
    )
    assert fid == "fc-123"
    assert captured["forecast_target"] == {
        "kind": "price_target",
        "direction": "at_or_above",
        "target_price": 80000.0,
        "outcome_rule_version": "window-touch-v1-high-gte-low-lte",
    }
    assert captured["probability"] == 0.5
    assert captured["correlation_id"] == "live:kis_live:abc"
    # default horizon 10 calendar days when min_hold_days is None
    assert captured["horizon"] == "P10D"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sell_or_no_target_skips_forecast(monkeypatch):
    called = False

    async def fake_save_forecast(db, **kwargs):  # pragma: no cover
        nonlocal called
        called = True
        return "created", object()

    monkeypatch.setattr(
        "app.services.live_place_provenance.save_forecast", fake_save_forecast
    )

    assert (
        await publish_place_time_forecast(
            correlation_id="live:kis_live:abc",
            symbol="005930",
            instrument_type="equity_kr",
            side="sell",
            target_price=80000.0,
            min_hold_days=None,
            session_label="kis_live_place",
            created_by="auto_place_live",
        )
        is None
    )
    assert (
        await publish_place_time_forecast(
            correlation_id="live:kis_live:abc",
            symbol="005930",
            instrument_type="equity_kr",
            side="buy",
            target_price=None,
            min_hold_days=None,
            session_label="kis_live_place",
            created_by="auto_place_live",
        )
        is None
    )
    assert called is False
