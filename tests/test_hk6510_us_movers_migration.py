"""hk 6510 — US movers storage migration (static here, DB round trip in CI).

The migration rides on existing tables, so the static checks are the ones that pin
its shape: the chain, the expand/validate split, the closed vocabularies it
enforces and the downgrade refusal while ``us`` rows exist.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EXPAND = REPO / "alembic" / "versions" / "20261125_hk6510_us_movers.py"
VALIDATE = REPO / "alembic" / "versions" / "20261125_hk6510_us_movers_val.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_revision_chain_and_length() -> None:
    expand = _load(EXPAND, "hk6510_chain")
    validate = _load(VALIDATE, "hk6510_chain_val")
    assert expand.revision == "20261125_hk6510_us_movers"
    assert expand.down_revision == "20261125_hk6508_fnguide"
    assert validate.revision == "20261125_hk6510_us_movers_val"
    assert validate.down_revision == expand.revision
    assert len(expand.revision) <= 32 and len(validate.revision) <= 32


@pytest.mark.unit
def test_checks_are_added_not_valid_then_validated() -> None:
    source = EXPAND.read_text()
    assert source.count('NOT VALID"') == 2
    validate = VALIDATE.read_text()
    assert "VALIDATE CONSTRAINT" in validate
    for name in (
        "ck_invest_momentum_event_snapshots_market",
        "ck_invest_momentum_event_snapshots_source",
    ):
        assert name in validate


@pytest.mark.unit
def test_vocabularies_match_the_code() -> None:
    from app.models.invest_momentum_event_snapshot import RUN_ERROR_REASONS

    source = EXPAND.read_text()
    # hk 6510 owns the source vocabulary that existed at that revision. Later
    # additive source changes belong to their own migration and static test.
    for value in (
        "alpaca_screener",
        "kis_overseas_ranking",
        "naver_stock",
    ):
        assert f"'{value}'" in source
    # hk 6510 introduced source_unavailable; it is checked separately below.
    for reason in RUN_ERROR_REASONS:
        if reason == "source_unavailable":
            continue
        assert f"'{reason}'" in source


@pytest.mark.unit
def test_downgrade_refuses_while_us_rows_or_new_reasons_exist() -> None:
    source = EXPAND.read_text()
    downgrade = source.split("def downgrade()", 1)[1]
    # snapshot rows would violate the restored kr-only CHECK ...
    assert "market = 'us'" in downgrade
    # ... and so would a run row carrying the widened reason.
    assert "source_unavailable" in downgrade
    assert downgrade.count("RAISE EXCEPTION") == 2
    migration = _load(EXPAND, "hk6510_runs_guard")
    assert migration.RUNS_TABLE == "invest_momentum_event_runs"
    assert "{RUNS_TABLE}" in downgrade


@pytest.mark.unit
def test_upgrade_only_touches_the_momentum_tables() -> None:
    expand = _load(EXPAND, "hk6510_scope")
    captured: list[str] = []

    class _Op:
        def __getattr__(self, name):
            def _record(*args, **kwargs):
                captured.append(f"{name} {args}")
                return None

            return _record

    op_attribute = "op"
    setattr(expand, op_attribute, _Op())
    expand.upgrade()
    joined = "\n".join(captured)
    for column in ("asset_type", "first_received_at", "field_reasons"):
        assert column in joined
    assert "invest_momentum_event_snapshots" in joined
    assert "invest_momentum_event_runs" in joined
    for other in ("naver_stock ", "kr_candles", "us_candles", "research."):
        assert other not in joined


@pytest.mark.unit
def test_database_round_trip_is_covered_by_the_ci_suite() -> None:
    """The DB assertions live with the writer, test_us_intraday_db.py."""
    db_test = REPO / "tests/services/invest_momentum_events/test_us_intraday_db.py"
    assert db_test.exists()
    text = db_test.read_text()
    assert '"us"' in text and "market" in text
    assert "IntegrityError" in text
