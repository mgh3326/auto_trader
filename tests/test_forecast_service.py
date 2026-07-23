# tests/test_forecast_service.py
"""ROB-650 — forecast ledger save/resolve/aggregate integration tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast
from app.services.daily_candles.provenance import with_equity_provenance
from app.services.daily_candles.repository import DailyCandleRow
from app.services.trade_journal import forecast_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]

_SERVICE_ACTOR = svc.AuthenticatedForecastActor(
    principal="service:forecast-test",
    authentication_method="service_identity",
)
_RAW_SAVE_FORECAST = svc.save_forecast


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession,
    investment_reports_cleanup_lock: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    async def authenticated_save(*args, **kwargs):
        kwargs.setdefault("authenticated_actor", _SERVICE_ACTOR)
        return await _RAW_SAVE_FORECAST(*args, **kwargs)

    monkeypatch.setattr(svc, "save_forecast", authenticated_save)
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()


def _price_target(direction: str = "at_or_above", target_price: float = 130.0) -> dict:
    return {
        "kind": "price_target",
        "direction": direction,
        "target_price": target_price,
        "outcome_rule_version": "window-touch-v1-high-gte-low-lte",
    }


_TERMINAL_RULE_VERSION = "terminal-close-v1-up-gte-down-lt"


def _terminal_close_target(
    direction: str = "up",
    target_price: float = 130.0,
    *,
    review_date: str = "2026-06-05",
    adjustment_policy: str = "explicit-factor-v1",
    factor: float = 1.0,
) -> dict:
    target = {
        "kind": "terminal_close",
        "direction": direction,
        "target_price": target_price,
        "outcome_rule_version": _TERMINAL_RULE_VERSION,
        "price_adjustment_policy": adjustment_policy,
    }
    if adjustment_policy == "explicit-factor-v1":
        if factor < 1.0:
            action_type = "split"
        elif factor > 1.0:
            action_type = "reverse_split"
        else:
            action_type = "none"
        target.update(
            {
                "target_to_close_factor": factor,
                "adjustment_provenance": {
                    "contract_version": "corporate-action-adjustment-v1",
                    "authority_type": "licensed_data_vendor",
                    "authority_id": "KIS",
                    "actor_principal": "service:forecast-test",
                    "authentication_method": "service_identity",
                    "symbol": "SMCI",
                    "action_type": action_type,
                    "action_ratio": 1.0 / factor,
                    "effective_date": review_date,
                    "source": "test corporate-action ledger",
                    "source_ref": "test://corporate-actions/SMCI/2026-06-05",
                    "source_sha256": "a" * 64,
                    "source_price_basis": "provider_adjusted",
                    "verified_through_date": review_date,
                },
            }
        )
    return target


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


def _terminal_candle(
    day: int,
    *,
    high: float,
    low: float,
    close: float,
    hour: int = 0,
    source: str = "kis",
) -> DailyCandleRow:
    row = DailyCandleRow(
        time_utc=datetime(2026, 6, day, hour, tzinfo=UTC),
        symbol="SMCI",
        partition="NASD",
        open=close,
        high=high,
        low=low,
        close=close,
        adj_close=close - 1,
        volume=1000.0,
        value=close * 1000.0,
        source=source,
    )
    row = with_equity_provenance(
        row,
        final_through_date=date(2026, 6, 5),
    )
    return replace(
        row,
        ingested_at=datetime(2026, 6, day, 23, tzinfo=UTC),
    )


def _resolution_cas(preview: dict) -> dict:
    contract = preview["resolution_contract"]
    return {
        "expected_target_version": contract["target_version"],
        "expected_claim_hash": contract["immutable_claim_hash"],
        "expected_resolution_fingerprint": contract["resolution_fingerprint"],
    }


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
@pytest.mark.parametrize(
    ("target", "error_fragment"),
    [
        (
            {
                **_terminal_close_target(),
                "direction": "at_or_below",
            },
            "terminal_close.direction",
        ),
        (
            {
                **_terminal_close_target(),
                "outcome_rule_version": "terminal-close-v0",
            },
            "outcome_rule_version",
        ),
        (
            {
                **_terminal_close_target(),
                "adjustment_provenance": {
                    **_terminal_close_target()["adjustment_provenance"],
                    "verified_through_date": "2026-06-04",
                },
            },
            "verified_through_date",
        ),
    ],
)
async def test_save_rejects_bad_terminal_close_target(
    db_session: AsyncSession, target: dict, error_fragment: str
):
    with pytest.raises(svc.ForecastValidationError, match=error_fragment):
        await svc.save_forecast(
            db_session,
            created_by="claude",
            symbol="SMCI",
            instrument_type="equity_us",
            forecast_target=target,
            probability=0.6,
            review_date="2026-06-05",
        )


@pytest.mark.asyncio
async def test_save_rejects_terminal_close_for_non_session_instrument(
    db_session: AsyncSession,
):
    with pytest.raises(svc.ForecastValidationError, match="equity_kr or equity_us"):
        await svc.save_forecast(
            db_session,
            created_by="claude",
            symbol="BTC",
            instrument_type="crypto",
            forecast_target=_terminal_close_target(),
            probability=0.6,
            review_date="2026-06-05",
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


@pytest.mark.asyncio
async def test_terminal_close_preregistration_can_add_typed_adjustment_evidence(
    db_session: AsyncSession,
):
    first_action, preregistered = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_close_target(
            adjustment_policy="unverified_fail_closed"
        ),
        probability=0.6,
        review_date="2026-06-05",
    )
    await db_session.commit()

    second_action, updated = await svc.save_forecast(
        db_session,
        forecast_id=str(preregistered.forecast_id),
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_close_target(),
        probability=0.6,
        review_date="2026-06-05",
        expected_target_version=preregistered.target_version,
    )
    await db_session.commit()

    assert (first_action, second_action) == ("created", "updated")
    assert updated.forecast_id == preregistered.forecast_id
    assert updated.forecast_target["price_adjustment_policy"] == "explicit-factor-v1"
    assert updated.forecast_target["target_to_close_factor"] == pytest.approx(1.0)


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
    preview = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=False,
        backfill_missing=False,
    )
    result = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
        backfill_missing=False,
        **_resolution_cas(preview),
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
async def test_resolve_terminal_close_up_ignores_high_and_records_provenance(
    db_session: AsyncSession, monkeypatch
):
    async def _fake_read(*_a, **_k):
        return [
            _terminal_candle(4, high=150.0, low=120.0, close=140.0),
            _terminal_candle(5, high=145.0, low=110.0, close=129.0),
        ]

    monkeypatch.setattr(svc, "_read_window_candles", _fake_read)

    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_close_target(direction="up", target_price=130.0),
        probability=0.7,
        forecast_start_date="2026-06-01",
        review_date="2026-06-05",
    )
    await db_session.commit()

    preview = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=False,
        backfill_missing=False,
        now=datetime(2026, 6, 8, 12, tzinfo=UTC),
    )

    assert preview["status"] == "previewed"
    assert preview["computed"]["outcome"] is False
    assert preview["computed"]["observed_value"] == pytest.approx(129.0)
    assert preview["computed"]["resolution_source"] == "ohlcv_day_terminal_close"
    detail = preview["computed"]["resolution_detail"]
    assert detail["target_kind"] == "terminal_close"
    assert detail["outcome_rule_version"] == _TERMINAL_RULE_VERSION
    assert detail["comparison_operator"] == ">="
    assert detail["source_date"] == "2026-06-05"
    assert detail["source_price"] == pytest.approx(129.0)
    assert detail["source_price_field"] == "close"
    assert detail["source_price_basis"] == "provider_adjusted"
    assert detail["regular_session_only"] is True
    assert detail["adj_close_used"] is False
    assert detail["price_adjustment_policy"] == "explicit-factor-v1"
    assert detail["target_to_close_factor"] == pytest.approx(1.0)
    assert detail["effective_target_price"] == pytest.approx(130.0)
    assert detail["adjustment_provenance"]["source_ref"].startswith("test://")

    committed = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
        backfill_missing=False,
        now=datetime(2026, 6, 8, 12, tzinfo=UTC),
        **_resolution_cas(preview),
    )
    await db_session.commit()

    assert committed["status"] == "resolved"
    await db_session.refresh(row)
    assert row.outcome is False
    assert float(row.observed_value) == pytest.approx(129.0)
    assert row.resolution_detail == detail


@pytest.mark.asyncio
async def test_resolve_terminal_close_down_ignores_low(
    db_session: AsyncSession, monkeypatch
):
    async def _fake_read(*_a, **_k):
        return [
            _terminal_candle(4, high=115.0, low=80.0, close=90.0),
            _terminal_candle(5, high=125.0, low=90.0, close=120.0),
        ]

    monkeypatch.setattr(svc, "_read_window_candles", _fake_read)

    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_close_target(direction="down", target_price=100.0),
        probability=0.6,
        forecast_start_date="2026-06-01",
        review_date="2026-06-05",
    )
    await db_session.commit()

    result = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=False,
        backfill_missing=False,
        now=datetime(2026, 6, 8, 12, tzinfo=UTC),
    )

    assert result["computed"]["outcome"] is False
    assert result["computed"]["observed_value"] == pytest.approx(120.0)
    assert result["computed"]["resolution_detail"]["comparison_operator"] == "<"


@pytest.mark.asyncio
@pytest.mark.parametrize(("direction", "expected"), [("up", True), ("down", False)])
async def test_resolve_terminal_close_equality_is_up_only(
    db_session: AsyncSession, monkeypatch, direction: str, expected: bool
):
    async def _fake_read(*_a, **_k):
        return [_terminal_candle(5, high=101.0, low=99.0, close=100.0)]

    monkeypatch.setattr(svc, "_read_window_candles", _fake_read)

    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_close_target(direction=direction, target_price=100.0),
        probability=0.5,
        review_date="2026-06-05",
    )
    await db_session.commit()

    result = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=False,
        backfill_missing=False,
        now=datetime(2026, 6, 8, 12, tzinfo=UTC),
    )

    assert result["computed"]["outcome"] is expected


@pytest.mark.asyncio
async def test_resolve_terminal_close_applies_explicit_adjustment_factor(
    db_session: AsyncSession, monkeypatch
):
    async def _fake_read(*_a, **_k):
        return [_terminal_candle(5, high=105.0, low=95.0, close=100.0)]

    monkeypatch.setattr(svc, "_read_window_candles", _fake_read)

    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_close_target(
            direction="up",
            target_price=200.0,
            factor=0.5,
        ),
        probability=0.5,
        review_date="2026-06-05",
    )
    await db_session.commit()

    result = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=False,
        backfill_missing=False,
        now=datetime(2026, 6, 8, 12, tzinfo=UTC),
    )

    detail = result["computed"]["resolution_detail"]
    assert result["computed"]["outcome"] is True
    assert detail["original_target_price"] == pytest.approx(200.0)
    assert detail["target_to_close_factor"] == pytest.approx(0.5)
    assert detail["effective_target_price"] == pytest.approx(100.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candles", "expected_status"),
    [
        ([], "unresolved_no_review_candle"),
        (
            [_terminal_candle(4, high=101.0, low=99.0, close=100.0)],
            "unresolved_stale_data",
        ),
        (
            [
                _terminal_candle(5, high=101.0, low=99.0, close=100.0),
                _terminal_candle(5, high=102.0, low=98.0, close=101.0, hour=12),
            ],
            "unresolved_ambiguous_review_candle",
        ),
        (
            [
                _terminal_candle(
                    5,
                    high=101.0,
                    low=99.0,
                    close=100.0,
                    source="yahoo_extended",
                )
            ],
            "unresolved_untrusted_source",
        ),
    ],
)
async def test_resolve_terminal_close_bad_data_fails_closed(
    db_session: AsyncSession,
    monkeypatch,
    candles: list[DailyCandleRow],
    expected_status: str,
):
    async def _fake_read(*_a, **_k):
        return candles

    monkeypatch.setattr(svc, "_read_window_candles", _fake_read)

    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_close_target(),
        probability=0.6,
        review_date="2026-06-05",
    )
    await db_session.commit()

    result = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
        backfill_missing=False,
        now=datetime(2026, 6, 8, 12, tzinfo=UTC),
    )

    assert result["status"] == expected_status
    assert result["changed"] is False
    assert result["resolution_evidence"]["target_kind"] == "terminal_close"
    assert result["resolution_evidence"]["outcome_rule_version"] == (
        _TERMINAL_RULE_VERSION
    )
    await db_session.refresh(row)
    assert row.status == "open"
    assert row.outcome is None
    assert row.resolution_detail is None


@pytest.mark.asyncio
async def test_resolve_terminal_close_unverified_adjustment_fails_closed(
    db_session: AsyncSession, monkeypatch
):
    async def _unexpected_read(*_a, **_k):
        raise AssertionError("unverified terminal target must not read candles")

    monkeypatch.setattr(svc, "_read_window_candles", _unexpected_read)

    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_close_target(
            adjustment_policy="unverified_fail_closed"
        ),
        probability=0.6,
        review_date="2026-06-05",
    )
    await db_session.commit()

    result = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
        backfill_missing=False,
        now=datetime(2026, 6, 8, 12, tzinfo=UTC),
    )

    assert result["status"] == "requires_adjustment_evidence"
    assert result["changed"] is False
    assert result["resolution_evidence"]["price_adjustment_policy"] == (
        "unverified_fail_closed"
    )
    await db_session.refresh(row)
    assert row.status == "open"


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


@pytest.mark.asyncio
async def test_no_claim_placeholder_auto_closes_without_score_and_leaves_due_queue(
    db_session: AsyncSession,
):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "no_resolvable_forecast"},
        probability=0.0,
        review_date="2020-01-01",
    )
    await db_session.commit()

    result = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
    )
    await db_session.commit()

    assert result["status"] == "closed_no_claim"
    assert result["changed"] is True
    assert result["computed"] is None
    await db_session.refresh(row)
    assert row.status == "closed_no_claim"
    assert row.outcome is None
    assert row.observed_value is None
    assert row.brier_score is None
    assert row.resolved_at is not None
    assert await svc.list_due_forecasts(db_session) == []
    assert (await svc.build_forecast_calibration_aggregate(db_session))["groups"] == []


@pytest.mark.asyncio
async def test_non_placeholder_due_forecast_still_requires_manual_resolution(
    db_session: AsyncSession,
):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "thesis_holds"},
        probability=0.7,
        review_date="2020-01-01",
    )
    await db_session.commit()

    result = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
    )

    assert result["status"] == "requires_manual"
    assert result["changed"] is False
    await db_session.refresh(row)
    assert row.status == "open"
    assert row.brier_score is None
    assert [due.forecast_id for due in await svc.list_due_forecasts(db_session)] == [
        row.forecast_id
    ]


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
    assert (
        await svc._resolve_candle_partition(
            db_session, symbol="X", instrument_type="bond"
        )
        is None
    )


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
