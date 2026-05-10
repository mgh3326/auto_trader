"""ROB-170 follow-up — full-universe backfill flag and is_active alignment."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_parse_args_all_flag_defaults_false():
    from scripts.build_invest_screener_snapshots import parse_args

    args = parse_args(["--market", "kr"])
    assert args.all is False
    assert args.batch_size == 200
    assert args.dry_run is True


@pytest.mark.unit
def test_parse_args_all_overrides_limit():
    from scripts.build_invest_screener_snapshots import parse_args

    args = parse_args(["--market", "kr", "--all"])
    assert args.all is True


@pytest.mark.unit
def test_parse_args_all_with_symbol_rejected():
    from scripts.build_invest_screener_snapshots import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--market", "kr", "--all", "--symbol", "005930"])


@pytest.mark.unit
def test_parse_args_all_with_explicit_limit_rejected():
    from scripts.build_invest_screener_snapshots import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--market", "kr", "--all", "--limit", "100"])


@pytest.mark.asyncio
async def test_us_resolver_filters_active(monkeypatch, db_session):
    """US universe iteration must filter is_active=True (alignment with KR + coverage)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.us_symbol_universe import USSymbolUniverse
    from scripts.build_invest_screener_snapshots import _resolve_symbols

    # Upsert test-only symbols idempotently so re-runs don't fail on duplicate PK
    for sym, exch, name, active in [
        ("TSTACTV", "NASDAQ", "TestActive", True),
        ("TSTOBSO", "NYSE", "TestObsolete", False),
    ]:
        stmt = (
            pg_insert(USSymbolUniverse)
            .values(symbol=sym, exchange=exch, name_en=name, is_active=active)
            .on_conflict_do_update(
                index_elements=["symbol"],
                set_={"exchange": exch, "name_en": name, "is_active": active},
            )
        )
        await db_session.execute(stmt)
    await db_session.commit()

    class _AsyncCtx:
        def __init__(self, session):
            self._session = session

        async def __aenter__(self):
            return self._session

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "scripts.build_invest_screener_snapshots.AsyncSessionLocal",
        lambda: _AsyncCtx(db_session),
    )
    out = await _resolve_symbols(market="us", override=[], limit=10)
    assert "TSTACTV" in out
    assert "TSTOBSO" not in out
