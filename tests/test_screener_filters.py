"""ROB-439 MVP foundation: adjustable-filter schema over screener base snapshots.

Pure unit tests (no DB) for the shared filter core that will drive the UI chips,
the loader predicates, and the screen_stocks MCP tool: catalog integrity, AND
application (fail-closed), validation/clamping, override merge, pilot presets.
"""

from __future__ import annotations

import pytest

from app.services.invest_view_model.screener_filters import (
    SNAPSHOT_FILTER_FIELDS,
    ScreenerFilterCondition,
    ScreenerFilterError,
    apply_filter_conditions,
    consecutive_gainers_loader_thresholds,
    merge_filter_overrides,
    preset_starting_filters,
    snapshot_kind_for_preset,
    validate_conditions,
)

# --- catalog integrity --------------------------------------------------------


@pytest.mark.unit
def test_catalog_field_keys_match_definitions() -> None:
    for snapshot_kind, fields in SNAPSHOT_FILTER_FIELDS.items():
        assert fields, f"{snapshot_kind} has no filterable fields"
        for key, definition in fields.items():
            assert definition.field == key
            assert definition.operator in {"gte", "lte", "eq"}
            assert definition.value_type in {"int", "float", "percent"}


# --- apply (AND, fail-closed) -------------------------------------------------


@pytest.mark.unit
def test_apply_and_combines_conditions() -> None:
    rows = [
        {"symbol": "A", "consecutive_up_days": 6, "week_change_rate": 3.0},
        {"symbol": "B", "consecutive_up_days": 4, "week_change_rate": 9.0},
        {"symbol": "C", "consecutive_up_days": 7, "week_change_rate": -1.0},
    ]
    conds = [
        ScreenerFilterCondition("consecutive_up_days", "gte", 5),
        ScreenerFilterCondition("week_change_rate", "gte", 0.0),
    ]
    out = apply_filter_conditions(rows, conds)
    assert [r["symbol"] for r in out] == ["A"]  # B fails days, C fails rate


@pytest.mark.unit
def test_apply_lte_and_eq() -> None:
    rows = [{"per": 8.0}, {"per": 12.0}, {"per": 10.0}]
    assert (
        len(
            apply_filter_conditions(rows, [ScreenerFilterCondition("per", "lte", 10.0)])
        )
        == 2
    )
    assert (
        len(apply_filter_conditions(rows, [ScreenerFilterCondition("per", "eq", 10.0)]))
        == 1
    )


@pytest.mark.unit
def test_apply_fail_closed_on_missing_or_non_numeric() -> None:
    rows = [
        {"symbol": "ok", "roe": 20.0},
        {"symbol": "null", "roe": None},
        {"symbol": "missing"},
        {"symbol": "garbage", "roe": "n/a"},
    ]
    out = apply_filter_conditions(rows, [ScreenerFilterCondition("roe", "gte", 15.0)])
    assert [r["symbol"] for r in out] == ["ok"]  # NULL/missing/non-numeric excluded


@pytest.mark.unit
def test_apply_empty_conditions_returns_all() -> None:
    rows = [{"a": 1}, {"a": 2}]
    assert apply_filter_conditions(rows, []) == rows


# --- validate (fail-closed + clamp) -------------------------------------------


@pytest.mark.unit
def test_validate_rejects_unknown_snapshot_field_and_operator() -> None:
    with pytest.raises(ScreenerFilterError):
        validate_conditions([], snapshot_kind="does_not_exist")
    with pytest.raises(ScreenerFilterError):
        validate_conditions(
            [ScreenerFilterCondition("not_a_field", "gte", 1)],
            snapshot_kind="invest_screener_snapshots",
        )
    with pytest.raises(ScreenerFilterError):
        validate_conditions(
            [ScreenerFilterCondition("per", "between", 1)],  # type: ignore[arg-type]
            snapshot_kind="market_valuation_snapshots",
        )


@pytest.mark.unit
def test_validate_clamps_to_bounds() -> None:
    # consecutive_up_days bounds 2..20; 99 clamps to 20, 0 clamps to 2.
    high = validate_conditions(
        [ScreenerFilterCondition("consecutive_up_days", "gte", 99)],
        snapshot_kind="invest_screener_snapshots",
    )
    assert high[0].value == 20
    low = validate_conditions(
        [ScreenerFilterCondition("consecutive_up_days", "gte", 0)],
        snapshot_kind="invest_screener_snapshots",
    )
    assert low[0].value == 2


# --- merge overrides (adjust / add) -------------------------------------------


@pytest.mark.unit
def test_merge_adjusts_same_field_and_appends_new() -> None:
    base = preset_starting_filters("consecutive_gainers")
    # adjust the streak threshold + ADD a max_per condition (different snapshot
    # field, but merge is field/op keyed — adding is the "필터 추가" path).
    overrides = [
        ScreenerFilterCondition("consecutive_up_days", "gte", 10),  # adjust
        ScreenerFilterCondition("week_change_rate", "gte", 5.0),  # adjust
    ]
    merged = merge_filter_overrides(base, overrides)
    by_field = {(c.field, c.operator): c.value for c in merged}
    assert by_field[("consecutive_up_days", "gte")] == 10
    assert by_field[("week_change_rate", "gte")] == 5.0
    assert len(merged) == 2  # both adjusted, none appended

    added = merge_filter_overrides(
        base, [ScreenerFilterCondition("volume", "gte", 100000)]
    )
    assert len(added) == 3  # original 2 + 1 appended
    assert ("volume", "gte") in {(c.field, c.operator) for c in added}


# --- pilot presets ------------------------------------------------------------


@pytest.mark.unit
def test_pilot_presets_starting_sets() -> None:
    assert (
        snapshot_kind_for_preset("consecutive_gainers") == "invest_screener_snapshots"
    )
    assert snapshot_kind_for_preset("high_yield_value") == "market_valuation_snapshots"
    assert snapshot_kind_for_preset("not_a_preset") is None

    cg = {
        (c.field, c.operator, c.value)
        for c in preset_starting_filters("consecutive_gainers")
    }
    assert ("consecutive_up_days", "gte", 5) in cg
    assert ("week_change_rate", "gte", 0.0) in cg

    hyv = {
        (c.field, c.operator, c.value)
        for c in preset_starting_filters("high_yield_value")
    }
    assert ("roe", "gte", 15.0) in hyv
    assert ("per", "lte", 10.0) in hyv

    assert preset_starting_filters("not_a_preset") == []


@pytest.mark.unit
def test_pilot_starting_filters_validate_against_their_snapshot() -> None:
    # starting filters must be valid against the preset's own base snapshot.
    for preset in ("consecutive_gainers", "high_yield_value"):
        kind = snapshot_kind_for_preset(preset)
        assert kind is not None
        validated = validate_conditions(
            preset_starting_filters(preset), snapshot_kind=kind
        )
        assert len(validated) == len(preset_starting_filters(preset))


# --- consecutive_gainers loader threshold derivation --------------------------


@pytest.mark.unit
def test_consecutive_gainers_loader_thresholds_default_and_adjust() -> None:
    # default starting set → loader gets the preset defaults (5 / 0.0).
    base = preset_starting_filters("consecutive_gainers")
    assert consecutive_gainers_loader_thresholds(base) == {
        "min_consecutive_up_days": 5,
        "min_week_change_rate": 0.0,
    }
    # loosen days to 3 + tighten week rate to 5% (the "조정 over snapshot" path).
    adjusted = merge_filter_overrides(
        base,
        [
            ScreenerFilterCondition("consecutive_up_days", "gte", 3),
            ScreenerFilterCondition("week_change_rate", "gte", 5.0),
        ],
    )
    assert consecutive_gainers_loader_thresholds(adjusted) == {
        "min_consecutive_up_days": 3,
        "min_week_change_rate": 5.0,
    }


@pytest.mark.unit
def test_consecutive_gainers_loader_thresholds_ignores_non_where_conditions() -> None:
    # only gte on the loader's own SQL columns map to WHERE kwargs; a volume add
    # (no LOOSEN semantics here) and an lte are not loader thresholds.
    conds = [
        ScreenerFilterCondition("consecutive_up_days", "gte", 7),
        ScreenerFilterCondition("volume", "gte", 100000),  # not a CG WHERE column
        ScreenerFilterCondition("week_change_rate", "lte", 50.0),  # not gte
    ]
    assert consecutive_gainers_loader_thresholds(conds) == {
        "min_consecutive_up_days": 7,
    }
