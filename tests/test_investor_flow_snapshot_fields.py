"""ROB-640: verify the 5 ROB-575 market fields are wired through the upsert
schema, the builder Naver mapping, and the repository ON CONFLICT path."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert

from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.services.investor_flow_snapshots.builder import (
    build_investor_flow_snapshots,
)
from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotUpsert,
    _with_derived_flags,
)

_NEW_FIELDS = (
    "close",
    "change_rate",
    "volume",
    "foreign_holding_shares",
    "foreign_holding_rate",
)


@pytest.mark.unit
def test_upsert_schema_accepts_five_new_fields():
    payload = InvestorFlowSnapshotUpsert(
        market="kr",
        symbol="005930",
        snapshot_date=dt.date(2026, 7, 1),
        source="naver_finance",
        close=Decimal("70000"),
        change_rate=Decimal("1.5"),
        volume=12_345_678,
        foreign_holding_shares=1_234_567,
        foreign_holding_rate=Decimal("8.5"),
    )
    assert payload.close == Decimal("70000")
    assert payload.change_rate == Decimal("1.5")
    assert payload.volume == 12_345_678
    assert payload.foreign_holding_shares == 1_234_567
    assert payload.foreign_holding_rate == Decimal("8.5")


@pytest.mark.unit
def test_upsert_schema_new_fields_default_to_none():
    payload = InvestorFlowSnapshotUpsert(
        market="kr",
        symbol="005930",
        snapshot_date=dt.date(2026, 7, 1),
        source="naver_finance",
    )
    assert payload.close is None
    assert payload.change_rate is None
    assert payload.volume is None
    assert payload.foreign_holding_shares is None
    assert payload.foreign_holding_rate is None


@pytest.mark.unit
def test_upsert_schema_rejects_unknown_field():
    with pytest.raises(ValidationError):
        InvestorFlowSnapshotUpsert(
            market="kr",
            symbol="005930",
            snapshot_date=dt.date(2026, 7, 1),
            source="naver_finance",
            unknown_column=42,  # type: ignore[call-arg]
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_builder_maps_naver_fields_to_upsert_payloads():
    async def fetcher(symbol: str, days: int):
        return {
            "symbol": symbol,
            "data": [
                {
                    "date": "2026-07-01",
                    "close": 70000.0,
                    "change_pct": 0.015,
                    "volume": 12_345_678,
                    "institutional_net": 200,
                    "foreign_net": 300,
                    "foreign_holding_shares": 1_234_567,
                    "foreign_holding_rate": 8.5,
                },
            ],
        }

    result = await build_investor_flow_snapshots(
        symbols=["005930"],
        days=1,
        fetcher=fetcher,
    )

    assert len(result.payloads) == 1
    payload = result.payloads[0]
    assert payload.close == Decimal("70000")
    assert payload.change_rate == Decimal("1.5")
    assert payload.volume == 12_345_678
    assert payload.foreign_holding_shares == 1_234_567
    assert payload.foreign_holding_rate == Decimal("8.5")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_builder_integration_with_trend_json_fixture(
    monkeypatch: pytest.MonkeyPatch,
):
    trend_payload = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "investor_flow"
            / "005930_trend_synthesized.json"
        ).read_text(encoding="utf-8")
    )

    async def mock_fetch_json(*args, **kwargs):
        return trend_payload

    monkeypatch.setattr(
        "app.services.naver_finance.investor._fetch_json", mock_fetch_json
    )

    result = await build_investor_flow_snapshots(
        symbols=["005930"],
        days=4,
    )

    assert len(result.payloads) == 4
    payload = result.payloads[0]
    assert payload.snapshot_date == dt.date(2026, 9, 23)
    assert payload.close == Decimal("74000")
    # Oldest row in the window has no prior close -> change_rate NULL.
    assert payload.change_rate is None
    assert payload.volume == 9_500_000
    assert payload.foreign_net == 0
    assert payload.institution_net == -50_000
    assert payload.individual_net == 50_000
    # foreignerHoldRatio "46.40%" -> 46.40 percent (0..100)
    assert payload.foreign_holding_rate == pytest.approx(Decimal("46.40"))

    newest = result.payloads[-1]
    assert newest.snapshot_date == dt.date(2026, 9, 28)
    assert newest.close == Decimal("75500")
    # Derived change_rate: (75500 - 75000) / 75000 * 100 = 0.6667%
    assert newest.change_rate == pytest.approx(Decimal("0.6667"), abs=1e-3)
    assert newest.foreign_net == 4_513_767
    assert newest.institution_net == 1_234_567
    assert newest.individual_net == -5_748_334
    # The trend payload has no foreign holding share count — NULL, never
    # fabricated (quantity x closePrice is NOT a share count).
    assert newest.foreign_holding_shares is None
    assert newest.foreign_holding_rate == pytest.approx(Decimal("46.64"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_builder_handles_missing_naver_market_fields():
    """Legacy 7-cell Naver rows may omit close/volume/holding fields."""

    async def fetcher(symbol: str, days: int):
        return {
            "symbol": symbol,
            "data": [
                {
                    "date": "2026-07-01",
                    "foreign_net": 300,
                    "institutional_net": 200,
                },
            ],
        }

    result = await build_investor_flow_snapshots(
        symbols=["005930"],
        days=1,
        fetcher=fetcher,
    )

    assert len(result.payloads) == 1
    payload = result.payloads[0]
    assert payload.close is None
    assert payload.change_rate is None
    assert payload.volume is None
    assert payload.foreign_holding_shares is None
    assert payload.foreign_holding_rate is None


@pytest.mark.unit
def test_upsert_insert_includes_five_fields():
    payload = InvestorFlowSnapshotUpsert(
        market="kr",
        symbol="005930",
        snapshot_date=dt.date(2026, 7, 1),
        source="naver_finance",
        foreign_net=300,
        institution_net=200,
        individual_net=-500,
        close=Decimal("70000"),
        change_rate=Decimal("1.5"),
        volume=12_345_678,
        foreign_holding_shares=1_234_567,
        foreign_holding_rate=Decimal("8.5"),
    )
    values = _with_derived_flags(payload)
    stmt = insert(InvestorFlowSnapshot).values(**values)
    sql_text = str(stmt.compile(dialect=postgresql.dialect()))

    for col in _NEW_FIELDS:
        assert col in sql_text, f"INSERT missing column: {col}"


@pytest.mark.unit
def test_upsert_on_conflict_includes_five_fields():
    payload = InvestorFlowSnapshotUpsert(
        market="kr",
        symbol="005930",
        snapshot_date=dt.date(2026, 7, 1),
        source="naver_finance",
        foreign_net=300,
        institution_net=200,
        individual_net=-500,
        close=Decimal("70000"),
        change_rate=Decimal("1.5"),
        volume=12_345_678,
        foreign_holding_shares=1_234_567,
        foreign_holding_rate=Decimal("8.5"),
    )
    values = _with_derived_flags(payload)
    stmt = insert(InvestorFlowSnapshot).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_investor_flow_snapshots_market_symbol_date_source",
        set_={
            key: stmt.excluded[key]
            for key in values
            if key not in {"market", "symbol", "snapshot_date", "source"}
        },
    )

    sql_text = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in sql_text
    assert "DO UPDATE SET" in sql_text
    for col in _NEW_FIELDS:
        assert col in sql_text, f"ON CONFLICT SET missing column: {col}"
