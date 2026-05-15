from __future__ import annotations

import sqlalchemy as sa

from app.models.invest_crypto_screener_snapshot import InvestCryptoScreenerSnapshot


def _constraint_sql(name: str) -> str:
    table = InvestCryptoScreenerSnapshot.__table__
    constraint = next(c for c in table.constraints if c.name == name)
    assert isinstance(constraint, sa.CheckConstraint)
    return str(constraint.sqltext)


def test_crypto_snapshot_table_constraints_are_crypto_specific() -> None:
    table = InvestCryptoScreenerSnapshot.__table__

    assert table.name == "invest_crypto_screener_snapshots"
    assert "uq_invest_crypto_screener_snapshots_symbol_date" in {
        c.name for c in table.constraints
    }
    assert "KRW-%" in _constraint_sql("ck_invest_crypto_screener_snapshots_symbol")
    assert "tvscreener_upbit" in _constraint_sql(
        "ck_invest_crypto_screener_snapshots_source"
    )


def test_crypto_snapshot_has_preset_metric_columns() -> None:
    columns = InvestCryptoScreenerSnapshot.__table__.columns

    for name in (
        "trade_amount_24h",
        "volume_24h",
        "volume_24h_usd",
        "market_cap",
        "rsi",
        "adx",
        "market_warning",
        "raw_payload",
    ):
        assert name in columns
