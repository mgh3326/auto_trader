import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.services import decision_history as dh
from app.services.trade_journal.forecast_service import _normalize_symbol_for_filter


def _digit_symbol() -> str:
    return ("9" + uuid.uuid4().hex[:9])[:10].upper()


async def _add_report_item(db: AsyncSession, symbol: str) -> None:
    rep = InvestmentReport(
        report_uuid=uuid.uuid4(),
        idempotency_key=f"rob713-r-{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="test",
        title="t",
        summary="s",
        status="draft",
    )
    db.add(rep)
    await db.flush()
    db.add(
        InvestmentReportItem(
            report_id=rep.id,
            item_uuid=uuid.uuid4(),
            idempotency_key=f"rob713-item-{uuid.uuid4()}",
            item_kind="action",
            symbol=symbol,
            intent="buy_review",
            rationale="seed for realized_r_by_tag",
            evidence_snapshot={},
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_realized_r_by_tag_present_and_bounded(db_session, monkeypatch):
    async def fake_scoreboard(db, *, market=None, **kw):
        return {
            "groups": [
                {
                    "tag": f"t{i}",
                    "n": 12,
                    "expectancy_r": 1.0,
                    "win_rate": 0.6,
                    "profit_factor": 2.0,
                    "avg_mae": -0.03,
                    "insufficient_sample": False,
                }
                for i in range(5)
            ],
            "overall": None,
            "as_of": "2026-07-05T00:00:00+00:00",
            "count": 60,
        }

    # build_trading_scoreboard is imported lazily inside _realized_r_by_tag
    # (keeps decision_history free of the broker import chain), so patch it at
    # its source module rather than on the decision_history namespace.
    monkeypatch.setattr(
        "app.services.trade_journal.aggregates.build_trading_scoreboard",
        fake_scoreboard,
    )

    sym = _digit_symbol()
    norm = _normalize_symbol_for_filter(sym, "equity_kr")
    await _add_report_item(db_session, norm)

    ctx = await dh.build_decision_context(db_session, symbol=sym, market="kr")

    assert ctx is not None
    assert "realized_r_by_tag" in ctx
    assert len(ctx["realized_r_by_tag"]) <= 3
    if ctx["realized_r_by_tag"]:
        first = next(iter(ctx["realized_r_by_tag"].values()))
        assert set(first) == {
            "n",
            "expectancy_r",
            "win_rate",
            "profit_factor",
            "avg_mae",
            "insufficient_sample",
        }
