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


# --- ROB-366 B7: currency-aware (US/USD) portfolio --------------------------
def _us_snap(payload_extra: dict):
    base = {"market": "us"}
    base.update(payload_extra)
    return _snap("portfolio", base)


@pytest.mark.asyncio
async def test_portfolio_journal_us_surfaces_usd_buying_power():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "portfolio": [
                _us_snap(
                    {
                        "cash": {"krw": None, "usd": 10_000.0},
                        "buying_power": {"krw": None, "usd": 8_000.0},
                        "holdings": [
                            {
                                "symbol": "AAPL",
                                "currency": "USD",
                                "value_native": 20_000.0,
                                "value_krw": 27_000_000.0,
                            }
                        ],
                    }
                )
            ],
        },
        bundle_metadata={},
    )
    payload = await PortfolioJournalStage().run(ctx)
    # NAV(USD) = value_native 20,000 + cash.usd 10,000 = 30,000 (no value_krw)
    assert "NAV(USD)=30,000" in (payload.summary or "")
    assert "buying_power_usd=8,000" in (payload.summary or "")
    assert "buying_power_krw" not in (payload.summary or "")
    assert "27,000,000" not in (payload.summary or "")  # no KRW cross-currency leak
    assert payload.cited_snapshots[0].payload_path == "$.buying_power.usd"
    assert payload.verdict == StageVerdict.NEUTRAL
    assert payload.confidence == 60  # bp_ratio 8000/30000 ≈ 0.27 → healthy
    assert payload.risk_evidence == []


@pytest.mark.asyncio
async def test_portfolio_journal_us_buying_power_absent_is_unavailable_not_zero():
    # ROB-326: KIS overseas cash often unsupported (OPSQ0002) → usd may be None.
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "portfolio": [
                _us_snap(
                    {
                        "cash": {"krw": None, "usd": None},
                        "buying_power": {"krw": None, "usd": None},
                        "holdings": [
                            {
                                "symbol": "AAPL",
                                "currency": "USD",
                                "value_native": 20_000.0,
                                "value_krw": 27_000_000.0,
                            }
                        ],
                    }
                )
            ],
        },
        bundle_metadata={},
    )
    payload = await PortfolioJournalStage().run(ctx)
    assert "buying_power_usd=unavailable" in (payload.summary or "")
    assert "buying_power_usd" in payload.missing_data
    assert payload.risk_evidence == []  # absence is not a <5% NAV risk
    assert payload.confidence == 60  # not punished to 40 for absent data
    assert "buying_power_krw" not in (payload.summary or "")
    assert "27,000,000" not in (payload.summary or "")  # never falls back to value_krw
    assert "NAV(USD)=20,000" in (payload.summary or "")  # value_native only


@pytest.mark.asyncio
async def test_portfolio_journal_us_nav_unavailable_when_value_native_missing():
    # No cross-currency fallback: a holding without value_native makes NAV
    # honestly unavailable rather than summing the KRW-normalized figure.
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "portfolio": [
                _us_snap(
                    {
                        "cash": {"krw": None, "usd": 5_000.0},
                        "buying_power": {"krw": None, "usd": 8_000.0},
                        "holdings": [
                            {
                                "symbol": "AAPL",
                                "currency": "USD",
                                "value_native": None,
                                "value_krw": 27_000_000.0,
                            }
                        ],
                    }
                )
            ],
        },
        bundle_metadata={},
    )
    payload = await PortfolioJournalStage().run(ctx)
    assert "NAV(USD)=unavailable" in (payload.summary or "")
    assert "27,000,000" not in (payload.summary or "")
    assert "buying_power_usd=8,000," in (payload.summary or "")  # present, no ratio


@pytest.mark.asyncio
async def test_portfolio_journal_crypto_uses_krw():
    # Upbit (crypto) is KRW-denominated → KRW path, byte-identical labels.
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "portfolio": [
                _snap(
                    "portfolio",
                    {
                        "market": "crypto",
                        "buying_power_krw": 500_000,
                        "nav_krw": 1_000_000,
                    },
                )
            ],
        },
        bundle_metadata={},
    )
    payload = await PortfolioJournalStage().run(ctx)
    assert "buying_power_krw=500,000" in (payload.summary or "")
    assert payload.cited_snapshots[0].payload_path == "$.buying_power.krw"


@pytest.mark.asyncio
async def test_portfolio_journal_crypto_nested_upbit_payload_yields_live_nav():
    """ROB-369 E9 — the upbit_live collector now emits a *nested* payload
    (cash.krw, buying_power.krw, holdings[].value_krw). NAV must reflect the
    live Upbit eval instead of defaulting to 0 (the pre-fix bug that reported
    "NAV=0, buying_power_krw=0" for a real ~25.75M KRW account)."""
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "portfolio": [
                _snap(
                    "portfolio",
                    {
                        "market": "crypto",
                        "primary_source": "upbit",
                        "cash": {"krw": 365_342.0, "usd": None},
                        "buying_power": {"krw": 365_342.0, "usd": None},
                        "holdings": [
                            {"symbol": "BTC", "value_krw": 25_384_658.0},
                        ],
                    },
                )
            ],
        },
        bundle_metadata={},
    )
    payload = await PortfolioJournalStage().run(ctx)
    # NAV = holdings 25,384,658 + cash 365,342 = 25,750,000 (live eval, not 0).
    assert "NAV=25,750,000" in (payload.summary or "")
    assert "NAV=0," not in (payload.summary or "")
    assert "buying_power_krw=365,342" in (payload.summary or "")
    assert payload.cited_snapshots[0].payload_path == "$.buying_power.krw"
    # Low orderable vs a 25.75M book is now a TRUE signal (~1.4% < 5%),
    # not the pre-fix 0/0 artifact.
    assert payload.risk_evidence == ["buying_power < 5% NAV"]
    assert payload.confidence == 40


@pytest.mark.asyncio
async def test_portfolio_journal_surfaces_nav_scope_label_in_key_points():
    """ROB-392 — portfolio collector's nav_scope_label is surfaced in
    key_points so the report makes the NAV scope explicit. The byte-identical
    KR summary string is unchanged (label rides only in key_points)."""
    label = (
        "NAV는 KIS 실거래(매도가능) 보유 + 현금 기준 · "
        "ISA/Toss 참조분(reference_holdings)은 제외"
    )
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "portfolio": [
                _snap(
                    "portfolio",
                    {
                        "nav_krw": 1_000_000,
                        "buying_power_krw": 500_000,
                        "nav_scope_label": label,
                    },
                )
            ],
            "journal": [
                _snap("journal", {"entries": [{"symbol": "005930", "thesis": "tech"}]})
            ],
        },
        bundle_metadata={},
    )
    artifact = await PortfolioJournalStage().run(ctx)
    assert label in artifact.key_points
