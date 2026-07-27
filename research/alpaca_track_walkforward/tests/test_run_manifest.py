"""Canonical run-manifest authority and synthetic input identity regressions."""

from __future__ import annotations

from datetime import UTC, datetime

import fold_schedule as fs
import pytest
import run_manifest as rm
import runner
import synthetic_fixture as sfx

_ANCHOR_MS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
_FOLD = fs.build_fold_schedule(_ANCHOR_MS)[0]
_NUM_DAYS = (_FOLD.oos_end_ms - _FOLD.train_start_ms) // rm.DAY_MS


def test_manifest_is_singleton_code_authority_not_caller_constructible():
    manifest = rm.canonical_run_manifest()
    assert manifest is rm.canonical_run_manifest()
    assert manifest.anchor_oos_start_ms == _ANCHOR_MS
    assert manifest.source_kind == "synthetic_fixture"
    assert len(manifest.manifest_hash) == 64
    with pytest.raises(TypeError, match="issued only"):
        rm.CanonicalRunManifest(
            run_id=manifest.run_id,
            source_kind=manifest.source_kind,
            anchor_oos_start_ms=manifest.anchor_oos_start_ms,
            symbols=manifest.symbols,
            daily_bars_hash_by_fold=manifest.daily_bars_hash_by_fold,
            universe_grid_hash_by_fold_family=(
                manifest.universe_grid_hash_by_fold_family
            ),
            minute_grid_hash_by_fold_family=manifest.minute_grid_hash_by_fold_family,
            manifest_hash=manifest.manifest_hash,
            _construction_token=object(),
        )
    with pytest.raises(TypeError):
        manifest.daily_bars_hash_by_fold["fold-0"] = "0" * 64


@pytest.mark.parametrize("family", ["AP-A1", "AP-A2"])
def test_fold0_fixture_recomputes_exact_pinned_input_lineage(family):
    manifest = rm.canonical_run_manifest()
    bars = sfx.build_bars_by_symbol(
        window_start_ms=_FOLD.train_start_ms,
        num_days=_NUM_DAYS,
        n_symbols=20,
    )
    universe_provider = sfx.make_universe_snapshot_provider(20)
    minute_provider = sfx.make_minute_bars_provider(
        window_start_ms=_FOLD.train_start_ms,
        n_symbols=20,
    )
    timestamps = runner._all_decision_timestamps(family=family, fold=_FOLD)
    universe_grid = {
        timestamp: universe_provider(timestamp).snapshot for timestamp in timestamps
    }
    minute_grid = {
        (symbol, timestamp): minute_provider(symbol, timestamp).bars
        for timestamp in timestamps
        for symbol in manifest.symbols
    }
    key = f"fold-0:{family}"

    assert (
        rm.canonical_daily_bars_hash(bars)
        == (manifest.daily_bars_hash_by_fold["fold-0"])
    )
    assert (
        rm.canonical_universe_grid_hash(universe_grid)
        == (manifest.universe_grid_hash_by_fold_family[key])
    )
    assert (
        rm.canonical_minute_grid_hash(minute_grid)
        == (manifest.minute_grid_hash_by_fold_family[key])
    )
