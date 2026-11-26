from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    path = ROOT / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_check_is_added_not_validated_then_validated():
    expand = _load("20261126_hk6663_us_mover_sources")
    validate = _load("20261126_hk6663_us_mover_sources_val")
    assert expand.down_revision == "20261125_hk6582_dart_capital"
    assert all(
        source in expand.NEW_SOURCES
        for source in (
            "kis_overseas_price_fluct",
            "kis_overseas_volume_surge",
            "kis_overseas_new_highlow",
            "kis_overseas_volume_power",
            "toss_rankings",
            "benzinga_market_movers",
        )
    )
    assert "NOT VALID" in __import__("inspect").getsource(expand.upgrade)
    assert "VALIDATE CONSTRAINT" in __import__("inspect").getsource(validate.upgrade)


def test_downgrade_refuses_rows_and_surface_runs_for_new_sources():
    import inspect

    expand = _load("20261126_hk6663_us_mover_sources")
    source = inspect.getsource(expand.downgrade)
    assert "LOCK TABLE invest_momentum_event_runs" in source
    assert "source IN" in source
    assert "split_part(surface, ':', 2)" in source
    assert "raise RuntimeError" in source
