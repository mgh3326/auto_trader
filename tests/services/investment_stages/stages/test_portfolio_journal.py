import uuid
from types import SimpleNamespace

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)
from app.services.investment_stages.stages.portfolio_journal import (
    PortfolioJournalStage,
)


def _snap(kind, payload):
    return SimpleNamespace(
        snapshot_uuid=uuid.uuid4(), snapshot_kind=kind, payload_json=payload
    )


@pytest.mark.asyncio
async def test_portfolio_journal_unavailable_without_portfolio():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(), snapshots_by_kind={}, bundle_metadata={}
    )
    with pytest.raises(UnavailableStageError):
        await PortfolioJournalStage().run(ctx)


@pytest.mark.asyncio
async def test_portfolio_journal_emits_neutral_with_buying_power():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "portfolio": [
                _snap("portfolio", {"buying_power_krw": 200000, "nav_krw": 1000000})
            ],
            "journal": [
                _snap("journal", {"entries": [{"symbol": "035420", "thesis": "tech"}]})
            ],
        },
        bundle_metadata={},
    )
    payload = await PortfolioJournalStage().run(ctx)
    assert payload.verdict == StageVerdict.NEUTRAL
    assert "035420" in (payload.summary or "")
    assert len(payload.cited_snapshots) >= 1


@pytest.mark.asyncio
async def test_portfolio_journal_derives_totals_from_nested_kis_payload():
    """ROB-314 follow-up: the production portfolio collector emits a *nested*
    payload (``cash.krw``, ``buying_power.krw``, ``holdings[].value_krw``), not
    the legacy flat ``nav_krw`` / ``buying_power_krw`` keys. The stage must
    derive non-zero NAV / buying power from the nested shape instead of
    defaulting to 0 and reporting an empty portfolio for a real account."""
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "portfolio": [
                _snap(
                    "portfolio",
                    {
                        "count": 2,
                        "primary_source": "kis",
                        "cash": {"krw": 3_308_957.0, "usd": None},
                        "buying_power": {"krw": 3_292_494.5274, "usd": None},
                        "holdings": [
                            {"symbol": "035420", "value_krw": 4_000_000.0},
                            {"symbol": "035720", "value_krw": 2_000_000.0},
                        ],
                    },
                )
            ],
        },
        bundle_metadata={},
    )
    payload = await PortfolioJournalStage().run(ctx)

    # NAV = holdings value sum (6,000,000) + cash (3,308,957) = 9,308,957
    assert "NAV=9,308,957" in (payload.summary or "")
    assert "buying_power_krw=3,292,495" in (payload.summary or "")
    assert "NAV=0," not in (payload.summary or "")
    assert "buying_power_krw=0 " not in (payload.summary or "")
    # buying power ~35% of NAV → healthy → NEUTRAL, confidence 60
    assert payload.verdict == StageVerdict.NEUTRAL
    assert payload.confidence == 60
    assert any(c.snapshot_kind == "portfolio" for c in payload.cited_snapshots)
