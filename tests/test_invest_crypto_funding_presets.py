"""ROB-443 Phase 1 PR2: crypto funding-rate presets (squeeze / overheated)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.models.invest_crypto_screener_snapshot import InvestCryptoScreenerSnapshot
from app.services.invest_crypto_screener_snapshots.repository import (
    InvestCryptoScreenerSnapshotsRepository,
)


def _row(
    symbol: str, funding: Decimal | None, *, vd: dt.date
) -> InvestCryptoScreenerSnapshot:
    return InvestCryptoScreenerSnapshot(
        symbol=symbol,
        snapshot_date=vd,
        latest_close=Decimal("100"),
        funding_rate=funding,
        market_warning=False,
        source="tvscreener_upbit",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_funding_squeeze_returns_negatives_most_negative_first(db_session):
    vd = dt.date(2099, 11, 1)
    await db_session.execute(
        InvestCryptoScreenerSnapshot.__table__.delete().where(
            InvestCryptoScreenerSnapshot.snapshot_date == vd
        )
    )
    db_session.add_all(
        [
            _row("KRW-AAA", Decimal("-0.0005"), vd=vd),
            _row("KRW-BBB", Decimal("-0.0020"), vd=vd),  # most negative
            _row("KRW-CCC", Decimal("0.0010"), vd=vd),  # positive → excluded
            _row("KRW-DDD", None, vd=vd),  # no perp → excluded (fail-closed)
        ]
    )
    await db_session.commit()
    repo = InvestCryptoScreenerSnapshotsRepository(db_session)

    rows = await repo.list_latest(preset_id="crypto_funding_squeeze", snapshot_date=vd)
    symbols = [r.symbol for r in rows]
    assert symbols == ["KRW-BBB", "KRW-AAA"]  # negatives only, most-negative first

    rows_over = await repo.list_latest(
        preset_id="crypto_funding_overheated", snapshot_date=vd
    )
    assert [r.symbol for r in rows_over] == ["KRW-CCC"]  # positives only

    await db_session.execute(
        InvestCryptoScreenerSnapshot.__table__.delete().where(
            InvestCryptoScreenerSnapshot.snapshot_date == vd
        )
    )
    await db_session.commit()


def test_funding_metric_label_signed_percent() -> None:
    from app.services.invest_view_model.screener_service import _metric_value_label

    neg, _ = _metric_value_label("crypto_funding_squeeze", {"funding_rate": -0.00025})
    pos, _ = _metric_value_label("crypto_funding_overheated", {"funding_rate": 0.0001})
    assert neg == "-0.0250%"
    assert pos == "+0.0100%"


def test_funding_presets_registered_for_crypto() -> None:
    from app.services.invest_view_model.screener_presets import preset_definitions

    ids = {p.id for p in preset_definitions("crypto")}
    assert "crypto_funding_squeeze" in ids
    assert "crypto_funding_overheated" in ids
