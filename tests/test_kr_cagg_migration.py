from __future__ import annotations

from importlib import util as importlib_util
from pathlib import Path


def _load_migration_module(file_name: str):
    migration_path = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / file_name
    )
    spec = importlib_util.spec_from_file_location(
        file_name,
        migration_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load KR CAGG migration module")
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cagg_sql_policy_recreated(monkeypatch):
    migration = _load_migration_module(
        "c9e4f5b8a2d1_rebuild_kr_hour_cagg_with_exchange_priority.py"
    )
    statements: list[str] = []

    def _record(statement):
        statements.append(str(statement))

    monkeypatch.setattr(migration.op, "execute", _record)

    migration.upgrade()

    assert any(
        "remove_continuous_aggregate_policy" in stmt and "market_candles_1h_kr" in stmt
        for stmt in statements
    )
    assert any(
        "DROP MATERIALIZED VIEW IF EXISTS market_candles_1h_kr" in stmt
        for stmt in statements
    )

    create_cagg_sql = next(
        stmt
        for stmt in statements
        if "CREATE MATERIALIZED VIEW market_candles_1h_kr" in stmt
    )
    assert "FROM market_candles_1m_kr" in create_cagg_sql
    assert "exchange IN ('KRX', 'NXT')" in create_cagg_sql
    assert "WHEN exchange = 'NXT' THEN INTERVAL '1 millisecond'" in create_cagg_sql
    assert "WHEN exchange = 'KRX' THEN INTERVAL '1 millisecond'" in create_cagg_sql

    assert any(
        "add_continuous_aggregate_policy" in stmt
        and "start_offset => INTERVAL '8 days'" in stmt
        and "end_offset => INTERVAL '1 minute'" in stmt
        and "schedule_interval => INTERVAL '5 minutes'" in stmt
        for stmt in statements
    )

    fail_fast_sql = next(stmt for stmt in statements if "RAISE EXCEPTION" in stmt)
    assert "upper(coalesce(route, '')) NOT IN ('J', 'NX', 'NXT')" in fail_fast_sql
    assert "unsupported route (allowed: J,NX,NXT)" in fail_fast_sql

    route_remap_sql = next(
        stmt for stmt in statements if "INSERT INTO market_candles_1m_kr" in stmt
    )
    assert "upper(coalesce(route, '')) = 'J' THEN 'KRX'" in route_remap_sql
    assert "IN ('NX', 'NXT')" in route_remap_sql
    assert "upper(coalesce(route, '')) IN ('J', 'NX', 'NXT')" in route_remap_sql
    assert "ELSE 'KRX'" not in route_remap_sql
    assert "'UN', 'NXT'" not in route_remap_sql


def test_v2_migration_creates_bigint_tables_and_policy(monkeypatch):
    migration = _load_migration_module(
        "d2f4a8c1b9e3_add_kr_quarantine_and_bigint_v2.py"
    )
    statements: list[str] = []

    def _record(statement):
        statements.append(str(statement))

    monkeypatch.setattr(migration.op, "execute", _record)

    migration.upgrade()

    assert any(
        "CREATE TABLE IF NOT EXISTS market_candles_ingest_quarantine" in stmt
        for stmt in statements
    )
    assert any(
        "CREATE TABLE IF NOT EXISTS market_candles_1m_kr_v2" in stmt
        for stmt in statements
    )
    assert any("ROUND(open)::BIGINT" in stmt for stmt in statements)
    assert any(
        "CREATE MATERIALIZED VIEW IF NOT EXISTS market_candles_1h_kr_v2" in stmt
        for stmt in statements
    )
    assert any(
        "add_continuous_aggregate_policy" in stmt and "market_candles_1h_kr_v2" in stmt
        for stmt in statements
    )
