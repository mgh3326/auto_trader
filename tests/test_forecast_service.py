# tests/test_forecast_service.py
"""ROB-650 — forecast ledger save/resolve/aggregate integration tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast
from app.services.daily_candles.repository import DailyCandleRow
from app.services.trade_journal import forecast_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()


def _price_target(direction: str = "at_or_above", target_price: float = 130.0) -> dict:
    return {
        "kind": "price_target",
        "direction": direction,
        "target_price": target_price,
    }


def _candles(highs: list[float]) -> list[DailyCandleRow]:
    return [
        DailyCandleRow(
            time_utc=datetime(2026, 6, 2 + i, tzinfo=UTC),
            symbol="998877",
            partition="KRX",
            open=h - 5,
            high=h,
            low=h - 10,
            close=h - 2,
            adj_close=None,
            volume=1000.0,
            value=h * 1000.0,
            source="kis",
        )
        for i, h in enumerate(highs)
    ]


# --------------------------------------------------------------------------- #
# save validation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_save_rejects_probability_out_of_range(db_session: AsyncSession):
    with pytest.raises(svc.ForecastValidationError):
        await svc.save_forecast(
            db_session,
            created_by="claude",
            symbol="005930",
            instrument_type="equity_kr",
            forecast_target=_price_target(),
            probability=1.5,
            review_date="2026-07-15",
        )


@pytest.mark.asyncio
async def test_save_rejects_probability_outside_band(db_session: AsyncSession):
    with pytest.raises(svc.ForecastValidationError):
        await svc.save_forecast(
            db_session,
            created_by="claude",
            symbol="005930",
            instrument_type="equity_kr",
            forecast_target=_price_target(),
            probability=0.9,
            probability_range_low=0.5,
            probability_range_high=0.7,
            review_date="2026-07-15",
        )


@pytest.mark.asyncio
async def test_save_rejects_bad_price_target(db_session: AsyncSession):
    with pytest.raises(svc.ForecastValidationError):
        await svc.save_forecast(
            db_session,
            created_by="claude",
            symbol="005930",
            instrument_type="equity_kr",
            forecast_target={"kind": "price_target", "direction": "up"},
            probability=0.6,
            review_date="2026-07-15",
        )


@pytest.mark.asyncio
async def test_save_stamps_policy_version_and_normalizes_symbol(
    db_session: AsyncSession,
):
    action, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="brk-b",
        instrument_type="equity_us",
        forecast_target=_price_target(),
        probability=0.65,
        review_date="2026-07-15",
    )
    await db_session.commit()
    assert action == "created"
    assert row.symbol == "BRK.B"
    # ROB-659: default stamp now comes from the ROB-646 policy YAML, not the literal.
    assert row.policy_version == svc._default_policy_version()
    assert row.policy_version
    assert row.status == "open"


@pytest.mark.asyncio
async def test_save_idempotent_by_forecast_id_while_open(db_session: AsyncSession):
    a1, row1 = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target=_price_target(),
        probability=0.6,
        review_date="2026-07-15",
        contrary_evidence="v1",
    )
    await db_session.commit()
    fid = str(row1.forecast_id)
    a2, row2 = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target=_price_target(),
        probability=0.6,
        review_date="2026-07-15",
        forecast_id=fid,
        contrary_evidence="v2",
    )
    await db_session.commit()
    assert (a1, a2) == ("created", "updated")
    rows = (await db_session.execute(select(TradeForecast))).scalars().all()
    assert len(rows) == 1
    assert rows[0].contrary_evidence == "v2"


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resolve_manual_requires_evidence(db_session: AsyncSession):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "thesis_holds"},
        probability=0.6,
        review_date="2026-07-15",
    )
    await db_session.commit()
    with pytest.raises(svc.ForecastValidationError):
        await svc.resolve_forecast(
            db_session,
            forecast_id=str(row.forecast_id),
            persist=True,
            manual_outcome=True,
        )


@pytest.mark.asyncio
async def test_resolve_dry_run_does_not_persist(db_session: AsyncSession):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "thesis_holds"},
        probability=0.5,
        review_date="2026-07-15",
    )
    await db_session.commit()
    result = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=False,
        manual_outcome=True,
        manual_evidence=["closed above 130"],
    )
    assert result["status"] == "previewed"
    assert result["changed"] is False
    assert result["computed"]["brier_score"] == pytest.approx(0.25)
    await db_session.refresh(row)
    assert row.status == "open"
    assert row.brier_score is None


@pytest.mark.asyncio
async def test_resolve_manual_persists_and_scores(db_session: AsyncSession):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "thesis_holds"},
        probability=0.8,
        review_date="2026-07-15",
    )
    await db_session.commit()
    result = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
        manual_outcome=True,
        manual_observed_value=131.0,
        manual_evidence=["broke resistance"],
    )
    await db_session.commit()
    assert result["status"] == "resolved"
    assert result["changed"] is True
    await db_session.refresh(row)
    assert row.status == "closed"
    assert row.outcome is True
    assert row.resolution_source == "manual"
    # (0.8 - 1)^2 = 0.04
    assert float(row.brier_score) == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_resolve_idempotent_closed_not_rescored(db_session: AsyncSession):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "thesis_holds"},
        probability=0.8,
        review_date="2026-07-15",
    )
    await db_session.commit()
    await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
        manual_outcome=True,
        manual_evidence=["e"],
    )
    await db_session.commit()
    # Re-resolve with a contradictory outcome — must be ignored (idempotent).
    second = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
        manual_outcome=False,
        manual_evidence=["e2"],
    )
    await db_session.commit()
    assert second["status"] == "already_closed"
    assert second["changed"] is False
    await db_session.refresh(row)
    assert row.outcome is True
    assert float(row.brier_score) == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_save_cannot_modify_closed(db_session: AsyncSession):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "thesis_holds"},
        probability=0.8,
        review_date="2026-07-15",
    )
    await db_session.commit()
    await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
        manual_outcome=True,
        manual_evidence=["e"],
    )
    await db_session.commit()
    with pytest.raises(svc.ForecastValidationError):
        await svc.save_forecast(
            db_session,
            created_by="claude",
            symbol="005930",
            instrument_type="equity_kr",
            forecast_target={"kind": "thesis_holds"},
            probability=0.9,
            review_date="2026-07-15",
            forecast_id=str(row.forecast_id),
        )


@pytest.mark.asyncio
async def test_resolve_price_target_from_ohlcv(db_session: AsyncSession, monkeypatch):
    # The daily-candle store (kr_candles_1d) has no ORM model and is absent from
    # the create_all test DB, so patch the deterministic reader with canned bars
    # (the DB-read plumbing is covered by the repository unit test).
    async def _fake_read(*_a, **_k):
        return _candles([120.0, 131.0, 125.0])

    monkeypatch.setattr(svc, "_read_window_candles", _fake_read)

    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="998877",
        instrument_type="equity_kr",
        forecast_target=_price_target(direction="at_or_above", target_price=130.0),
        probability=0.7,
        forecast_start_date="2026-06-01",
        review_date="2026-06-05",
    )
    await db_session.commit()
    result = await svc.resolve_forecast(
        db_session, forecast_id=str(row.forecast_id), persist=True
    )
    await db_session.commit()
    assert result["status"] == "resolved"
    assert result["computed"]["resolution_source"] == "ohlcv_day"
    await db_session.refresh(row)
    assert row.status == "closed"
    assert row.outcome is True
    assert float(row.observed_value) == pytest.approx(131.0)
    # (0.7 - 1)^2 = 0.09
    assert float(row.brier_score) == pytest.approx(0.09)


@pytest.mark.asyncio
async def test_resolve_price_target_unresolved_no_data(
    db_session: AsyncSession, monkeypatch
):
    async def _empty_read(*_a, **_k):
        return []

    monkeypatch.setattr(svc, "_read_window_candles", _empty_read)

    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="997766",
        instrument_type="equity_kr",
        forecast_target=_price_target(),
        probability=0.6,
        forecast_start_date="2026-06-01",
        review_date="2026-06-05",
    )
    await db_session.commit()
    result = await svc.resolve_forecast(
        db_session, forecast_id=str(row.forecast_id), persist=True
    )
    assert result["status"] == "unresolved_no_data"
    assert result["changed"] is False
    await db_session.refresh(row)
    assert row.status == "open"


# --------------------------------------------------------------------------- #
# calibration aggregate
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_calibration_rows_defer_unused_jsonb(db_session: AsyncSession):
    """ROB-667: the calibration fetch must not hydrate the 3 unused JSONB
    columns (forecast_target / evidence_ids / resolution_detail)."""
    import sqlalchemy as sa

    async def _make(created_by: str, prob: float, outcome: bool) -> None:
        _, row = await svc.save_forecast(
            db_session,
            created_by=created_by,
            symbol="005930",
            instrument_type="equity_kr",
            forecast_target={"kind": "thesis_holds"},
            probability=prob,
            review_date="2026-07-15",
        )
        await db_session.commit()
        await svc.resolve_forecast(
            db_session,
            forecast_id=str(row.forecast_id),
            persist=True,
            manual_outcome=outcome,
            manual_evidence=["e"],
        )
        await db_session.commit()

    await _make("sessionA", 0.7, True)

    # Drop the identity map so the next query re-materializes with defer applied.
    db_session.expunge_all()

    filters = [
        TradeForecast.status == "closed",
        TradeForecast.brier_score.isnot(None),
    ]
    rows = await svc._fetch_calibration_rows(db_session, filters=filters)

    assert rows, "expected at least one closed+scored forecast"
    unloaded = sa.inspect(rows[0]).unloaded
    assert "forecast_target" in unloaded
    assert "evidence_ids" in unloaded
    assert "resolution_detail" in unloaded


@pytest.mark.asyncio
async def test_calibration_aggregate_groups_by_created_by(db_session: AsyncSession):
    async def _make(created_by: str, prob: float, outcome: bool) -> None:
        _, row = await svc.save_forecast(
            db_session,
            created_by=created_by,
            symbol="005930",
            instrument_type="equity_kr",
            forecast_target={"kind": "thesis_holds"},
            probability=prob,
            review_date="2026-07-15",
        )
        await db_session.commit()
        await svc.resolve_forecast(
            db_session,
            forecast_id=str(row.forecast_id),
            persist=True,
            manual_outcome=outcome,
            manual_evidence=["e"],
        )
        await db_session.commit()

    await _make("claude", 0.8, True)
    await _make("claude", 0.6, False)
    await _make("gpt", 0.9, True)

    agg = await svc.build_forecast_calibration_aggregate(
        db_session, group_by="created_by"
    )
    assert agg["group_by"] == "created_by"
    groups = {g["group"]: g for g in agg["groups"]}
    assert groups["claude"]["sample_size"] == 2
    assert groups["claude"]["hits"] == 1
    assert groups["gpt"]["sample_size"] == 1
    assert groups["gpt"]["hit_rate"] == pytest.approx(1.0)
    # claude avg brier = ((0.8-1)^2 + (0.6-0)^2)/2 = (0.04 + 0.36)/2 = 0.20
    assert groups["claude"]["avg_brier_score"] == pytest.approx(0.20)


# --------------------------------------------------------------------------- #
# ROB-659: symbol-filter normalization + policy_version default
# --------------------------------------------------------------------------- #
def test_normalize_symbol_for_filter_equity_dash_to_dot():
    # BRK-B (external form) must resolve to the stored BRK.B for filtering.
    assert svc._normalize_symbol_for_filter("brk-b") == "BRK.B"
    assert svc._normalize_symbol_for_filter("BRK/B") == "BRK.B"


def test_normalize_symbol_for_filter_preserves_crypto_pair():
    # Crypto pairs keep their market-separator dash (no dash->dot collapse).
    assert svc._normalize_symbol_for_filter("KRW-BTC") == "KRW-BTC"
    assert svc._normalize_symbol_for_filter("btc-eth") == "BTC-ETH"


def test_normalize_symbol_for_filter_uses_instrument_type_when_known():
    # With instrument_type it mirrors the write-side normalization exactly.
    assert svc._normalize_symbol_for_filter("brk-b", "equity_us") == "BRK.B"
    assert svc._normalize_symbol_for_filter("btc", "crypto") == "KRW-BTC"


def test_default_policy_version_uses_policy_yaml():
    # ROB-659: the default stamp comes from the ROB-646 policy YAML, not the
    # stale "forecast.v1" literal.
    assert svc._default_policy_version() == svc.policy_version_stamp()["version"]


@pytest.mark.asyncio
async def test_list_forecasts_symbol_filter_normalizes_equity_us(
    db_session: AsyncSession,
):
    # Stored as BRK.B; a query using the external BRK-B form must still match.
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="brk-b",
        instrument_type="equity_us",
        forecast_target=_price_target(),
        probability=0.6,
        review_date="2026-07-15",
    )
    await db_session.commit()
    assert row.symbol == "BRK.B"

    matched = await svc.list_forecasts(db_session, symbol="BRK-B")
    assert matched["summary"]["count"] == 1
    assert matched["entries"][0]["symbol"] == "BRK.B"


# --------------------------------------------------------------------------- #
# ROB-712: shared _resolve_candle_partition mapping
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resolve_candle_partition_kr_us_crypto(db_session: AsyncSession):
    from app.services.daily_candles.repository import MarketKey

    assert await svc._resolve_candle_partition(
        db_session, symbol="005930", instrument_type="equity_kr"
    ) == (MarketKey.KR, "KRX")
    # crypto MUST use the resolve-canonical partition so read/write are symmetric
    assert await svc._resolve_candle_partition(
        db_session, symbol="KRW-BTC", instrument_type="crypto"
    ) == (MarketKey.CRYPTO, "upbit_krw")
    assert await svc._resolve_candle_partition(
        db_session, symbol="X", instrument_type="bond"
    ) is None



@pytest.mark.asyncio
async def test_resolve_backfills_missing_candles_then_scores(

    db_session: AsyncSession, monkeypatch
):
    # ROB-712: when the resolution window has no candles, resolve_forecast must
    # call _backfill_daily_candles once, then re-read the window. The fake
    # backfill swaps _read_window_candles to return canned candles on the
    # second read (mirroring what a real fetch+persist would yield).
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target=_price_target(direction="at_or_above", target_price=100.0),
        probability=0.7,
        forecast_start_date="2026-06-01",
        review_date="2026-06-05",
    )
    await db_session.commit()

    real_read = svc._read_window_candles
    calls = {"n": 0}

    async def fake_backfill(*, symbol, market, partition, horizon_bars=200):
        calls["n"] += 1
        # Mirror the real-world effect: after the backfill a subsequent read
        # of the same window returns candles.
        async def seeded_read(*_a, **_k):
            return _candles([120.0, 131.0])

        monkeypatch.setattr(svc, "_read_window_candles", seeded_read)
        return 7

    monkeypatch.setattr(svc, "_backfill_daily_candles", fake_backfill)

    # First read returns empty so the backfill branch runs (the test DB lacks
    # the kr_candles_1d table so we cannot call the real reader here).
    async def empty_read(*_a, **_k):
        return []

    monkeypatch.setattr(svc, "_read_window_candles", empty_read)

    result = await svc.resolve_forecast(
        db_session, forecast_id=str(row.forecast_id), persist=False
    )
    await db_session.commit()

    assert calls["n"] == 1
    assert result["status"] == "previewed"
    assert result["computed"]["outcome"] is True
    assert result["computed"]["resolution_source"] == "ohlcv_day"
    # restore so the autouse cleanup fixture can run unaffected
    monkeypatch.setattr(svc, "_read_window_candles", real_read)


@pytest.mark.asyncio
async def test_resolve_backfill_failure_is_graceful(
    db_session: AsyncSession, monkeypatch
):
    # ROB-712: backfill returns 0 on failure (helper swallows exceptions) and
    # resolve must fall through to unresolved_no_data — never raise.
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target=_price_target(direction="at_or_above", target_price=100.0),
        probability=0.6,
        forecast_start_date="2026-06-01",
        review_date="2026-06-05",
    )
    await db_session.commit()

    async def empty_read(*_a, **_k):
        return []

    async def boom(*, symbol, market, partition, horizon_bars=200):
        return 0

    monkeypatch.setattr(svc, "_read_window_candles", empty_read)
    monkeypatch.setattr(svc, "_backfill_daily_candles", boom)

    result = await svc.resolve_forecast(
        db_session, forecast_id=str(row.forecast_id), persist=False
    )
    assert result["status"] == "unresolved_no_data"


@pytest.mark.asyncio
async def test_resolve_backfill_missing_false_skips_fetch(
    db_session: AsyncSession, monkeypatch
):
    # ROB-712: backfill_missing=False must skip the fetch+persist entirely.
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target=_price_target(direction="at_or_above", target_price=100.0),
        probability=0.6,
        forecast_start_date="2026-06-01",
        review_date="2026-06-05",
    )
    await db_session.commit()

    async def empty_read(*_a, **_k):
        return []

    called = {"n": 0}

    async def spy(*, symbol, market, partition, horizon_bars=200):
        called["n"] += 1
        return 0

    monkeypatch.setattr(svc, "_read_window_candles", empty_read)
    monkeypatch.setattr(svc, "_backfill_daily_candles", spy)

    result = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=False,
        backfill_missing=False,
    )
    assert called["n"] == 0
    assert result["status"] == "unresolved_no_data"
