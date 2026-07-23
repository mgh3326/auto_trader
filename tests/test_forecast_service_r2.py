"""ROB-1038 R2 security regressions for forecast resolution semantics."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast
from app.services.daily_candles.repository import DailyCandleRow
from app.services.trade_journal import forecast_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]

_TOUCH_RULE = "window-touch-v1-high-gte-low-lte"
_TERMINAL_RULE = "terminal-close-v1-up-gte-down-lt"
_EVIDENCE_HASH = "a" * 64


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()


def _touch_target(*, versioned: bool = True) -> dict:
    target = {
        "kind": "price_target",
        "direction": "at_or_above",
        "target_price": 130.0,
    }
    if versioned:
        target["outcome_rule_version"] = _TOUCH_RULE
    return target


def _terminal_target() -> dict:
    return {
        "kind": "terminal_close",
        "direction": "up",
        "target_price": 130.0,
        "outcome_rule_version": _TERMINAL_RULE,
        "price_adjustment_policy": "explicit-factor-v1",
        "target_to_close_factor": 1.0,
        "adjustment_provenance": {
            "contract_version": "corporate-action-adjustment-v1",
            "authority_type": "licensed_data_vendor",
            "authority_id": "KIS",
            "actor_principal": "service:forecast-review",
            "authentication_method": "service_identity",
            "symbol": "SMCI",
            "action_type": "none",
            "action_ratio": 1.0,
            "effective_date": "2026-06-05",
            "verified_through_date": "2026-06-05",
            "source": "KIS corporate-action feed",
            "source_ref": "artifact://corporate-actions/SMCI/2026-06-05",
            "source_sha256": _EVIDENCE_HASH,
            "source_price_basis": "provider_adjusted",
            "evidence_ref": "artifact://corporate-actions/SMCI/2026-06-05",
        },
    }


async def _insert_legacy_touch(
    db_session: AsyncSession,
    *,
    review_date: date = date(2020, 1, 1),
) -> TradeForecast:
    row = TradeForecast(
        created_by="legacy-writer",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_touch_target(versioned=False),
        probability=0.6,
        review_date=review_date,
        status="open",
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_new_touch_requires_explicit_rule_version(db_session: AsyncSession):
    with pytest.raises(svc.ForecastValidationError, match="outcome_rule_version"):
        await svc.save_forecast(
            db_session,
            created_by="new-writer",
            symbol="SMCI",
            instrument_type="equity_us",
            forecast_target=_touch_target(versioned=False),
            probability=0.6,
            review_date="2026-06-05",
        )


@pytest.mark.asyncio
async def test_legacy_versionless_touch_single_id_quarantines_before_candle_read(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    row = await _insert_legacy_touch(db_session)

    async def forbidden_read(*_args, **_kwargs):
        raise AssertionError("legacy quarantine must happen before candle lookup")

    monkeypatch.setattr(svc, "_read_window_candles", forbidden_read)
    result = await svc.resolve_forecast(
        db_session,
        forecast_id=row.forecast_id,
        persist=False,
        backfill_missing=False,
    )

    assert result["status"] == "quarantined_legacy_price_target"
    assert result["changed"] is False
    assert result["resolution_evidence"]["target_kind"] == "price_target"
    assert result["resolution_evidence"]["outcome_rule_version"] is None


@pytest.mark.asyncio
async def test_quarantine_does_not_consume_general_due_limit(db_session: AsyncSession):
    legacy = await _insert_legacy_touch(db_session)
    _, eligible = await svc.save_forecast(
        db_session,
        created_by="typed-writer",
        symbol="AAPL",
        instrument_type="equity_us",
        forecast_target=_touch_target(),
        probability=0.6,
        review_date="2020-01-02",
    )
    await db_session.commit()

    due = await svc.list_due_forecasts(
        db_session, now=datetime(2026, 6, 8, tzinfo=UTC), limit=1
    )
    quarantined = await svc.list_due_quarantined_forecasts(
        db_session, now=datetime(2026, 6, 8, tzinfo=UTC), limit=1
    )

    assert [row.forecast_id for row in due] == [eligible.forecast_id]
    assert [row.forecast_id for row in quarantined] == [legacy.forecast_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        {"kind": "thesis_holds"},
        _touch_target(),
    ],
)
async def test_terminal_same_id_cannot_change_kind(
    db_session: AsyncSession, replacement: dict
):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=0.48,
        probability_range_low=0.4,
        probability_range_high=0.6,
        review_date="2026-06-05",
        session_label="origin-session",
        model_label="origin-model",
        evidence_ids=["artifact:origin"],
    )
    await db_session.commit()

    with pytest.raises(svc.ForecastValidationError, match="immutable"):
        await svc.save_forecast(
            db_session,
            forecast_id=row.forecast_id,
            created_by="claude",
            symbol="SMCI",
            instrument_type="equity_us",
            forecast_target=replacement,
            probability=1.0,
            review_date="2026-06-05",
        )


@pytest.mark.asyncio
async def test_terminal_exact_replay_preserves_omitted_optional_provenance(
    db_session: AsyncSession,
):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=0.48,
        review_date="2026-06-05",
        session_label="origin-session",
        model_label="origin-model",
        evidence_ids=["artifact:origin"],
        contrary_evidence="origin cutoff",
    )
    await db_session.commit()

    action, replayed = await svc.save_forecast(
        db_session,
        forecast_id=row.forecast_id,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=0.48,
        review_date="2026-06-05",
    )
    await db_session.commit()

    assert action == "unchanged"
    assert replayed.session_label == "origin-session"
    assert replayed.model_label == "origin-model"
    assert replayed.evidence_ids == ["artifact:origin"]
    assert replayed.contrary_evidence == "origin cutoff"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        {"kind": "thesis_holds"},
        _touch_target(),
    ],
)
async def test_terminal_original_semantics_blocks_direct_mutation_manual_bypass(
    db_session: AsyncSession, replacement: dict
):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=0.48,
        review_date="2026-06-05",
    )
    await db_session.commit()

    row.forecast_target = replacement
    await db_session.commit()

    result = await svc.resolve_forecast(
        db_session,
        forecast_id=row.forecast_id,
        persist=True,
        manual_outcome=True,
        manual_evidence=["post-review rewrite"],
        backfill_missing=False,
    )

    assert result["status"] == "quarantined_claim_integrity"
    assert result["changed"] is False
    await db_session.refresh(row)
    assert row.status == "open"
    assert row.brier_score is None


def test_terminal_classifier_rejects_candle_without_finality_provenance():
    candle = DailyCandleRow(
        time_utc=datetime(2026, 6, 5, tzinfo=UTC),
        symbol="SMCI",
        partition="NASD",
        open=129.0,
        high=135.0,
        low=125.0,
        close=129.0,
        adj_close=None,
        volume=1000.0,
        value=129000.0,
        source="kis",
    )

    with pytest.raises(
        svc.TerminalCloseDataError, match="final regular-session provenance"
    ):
        svc.classify_terminal_close_outcome(
            [candle],
            review_date=date(2026, 6, 5),
            direction="up",
            target_price=130.0,
        )


@pytest.mark.asyncio
async def test_resolve_side_malformed_terminal_target_is_row_local_fail_closed(
    db_session: AsyncSession,
):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_target(),
        probability=0.48,
        review_date="2026-06-05",
    )
    await db_session.commit()

    row.forecast_target = {
        "kind": "terminal_close",
        "direction": "up",
        "outcome_rule_version": "unknown-version",
    }
    await db_session.commit()

    result = await svc.resolve_forecast(
        db_session,
        forecast_id=row.forecast_id,
        persist=False,
        backfill_missing=False,
    )

    assert result["status"] == "quarantined_invalid_target"
    assert result["changed"] is False
    assert "outcome_rule_version" in result["reason"]
