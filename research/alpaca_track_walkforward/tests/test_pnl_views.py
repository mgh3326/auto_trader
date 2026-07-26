"""ROB-1062 H4 (AC17-AC21) — 3-view PnL independence and no-double-fee
guarantees."""

from __future__ import annotations

import ast
from pathlib import Path

import pnl_views as pv
import pytest
import wf_seal_consumption as sc

_ROOT = Path(__file__).resolve().parents[1]


def _trade(*, entry_ref=100.0, entry_fill=100.5, exit_ref=110.0, exit_fill=109.45):
    return pv.TradeFill(
        entry_reference_close=entry_ref,
        entry_fill_price=entry_fill,
        exit_reference_close=exit_ref,
        exit_fill_price=exit_fill,
    )


def test_gross_uses_reference_closes_only():
    trade = _trade()
    expected = (110.0 - 100.0) / 100.0 * 10_000.0
    assert pv.gross_pnl_bp(trade) == pytest.approx(expected)


def test_actual_fill_uses_modeled_fill_prices_only_and_differs_from_gross():
    trade = _trade()
    expected = (109.45 - 100.5) / 100.5 * 10_000.0
    assert pv.actual_fill_pnl_bp(trade) == pytest.approx(expected)
    assert pv.actual_fill_pnl_bp(trade) != pv.gross_pnl_bp(trade)


def test_shadow_net_deducts_exactly_the_named_scenario_bp_once():
    trade = _trade()
    base = pv.actual_fill_pnl_bp(trade)
    scenarios = {"C50": 50, "C100": 100, "C120": 120, "C150": 150}
    for name, bp in scenarios.items():
        assert pv.shadow_net_pnl_bp(
            trade, scenario=name, cost_scenarios_bp=scenarios
        ) == pytest.approx(base - bp)


def test_shadow_net_rejects_unknown_scenario_name():
    trade = _trade()
    with pytest.raises(KeyError, match="unknown cost scenario"):
        pv.shadow_net_pnl_bp(trade, scenario="C999", cost_scenarios_bp={"C50": 50})


def test_scenarios_are_computed_independently_never_derived_from_each_other():
    """AC17 — deliberately non-monotonic, unevenly-spaced fake scenario
    table. Any implementation that derives one scenario from another
    (interpolates, assumes even spacing, assumes ordering) fails this."""
    trade = _trade()
    fake_scenarios = {"C50": 7, "C100": 999, "C120": 3, "C150": 12_345}
    base = pv.actual_fill_pnl_bp(trade)
    result = pv.three_view_pnl_bp(trade, cost_scenarios_bp=fake_scenarios)
    for name, bp in fake_scenarios.items():
        assert result.shadow_net_bp_by_scenario[name] == pytest.approx(base - bp)


def test_mutating_one_scenario_bp_moves_only_that_scenarios_output():
    trade = _trade()
    scenarios = {"C50": 50, "C100": 100, "C120": 120, "C150": 150}
    before = pv.three_view_pnl_bp(trade, cost_scenarios_bp=scenarios)
    mutated = dict(scenarios)
    mutated["C120"] = 121  # bump only C120
    after = pv.three_view_pnl_bp(trade, cost_scenarios_bp=mutated)
    assert (
        after.shadow_net_bp_by_scenario["C120"]
        != before.shadow_net_bp_by_scenario["C120"]
    )
    for name in ("C50", "C100", "C150"):
        assert (
            after.shadow_net_bp_by_scenario[name]
            == before.shadow_net_bp_by_scenario[name]
        )


def test_three_view_pnl_using_the_real_sealed_scenarios_matches_seal():
    trade = _trade()
    real_scenarios = sc.cost_scenarios_bp()
    result = pv.three_view_pnl_bp(trade, cost_scenarios_bp=real_scenarios)
    base = pv.actual_fill_pnl_bp(trade)
    assert result.shadow_net_bp_by_scenario["C120"] == pytest.approx(base - 120)
    assert result.gross_bp == pytest.approx(pv.gross_pnl_bp(trade))


def test_trade_fill_rejects_non_positive_prices():
    with pytest.raises(ValueError, match="must be positive"):
        pv.TradeFill(
            entry_reference_close=0.0,
            entry_fill_price=1.0,
            exit_reference_close=1.0,
            exit_fill_price=1.0,
        )


def test_module_never_imports_or_references_a_separate_paper_fee_bp_deduction():
    """AC19 (fee applied exactly once) enforced structurally: this module
    must never import wf_seal_consumption.paper_fee_bp — the cost scenario
    bp values already include the fee once; a second deduction would double
    -charge it."""
    text = (_ROOT / "pnl_views.py").read_text()
    tree = ast.parse(text)
    # Scan only executable code (calls/attribute access), not prose in
    # docstrings — the module docstring legitimately DISCUSSES why no
    # `paper_fee_bp` deduction exists, which would otherwise self-trip a
    # naive substring scan of the raw file text.
    referenced_names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "paper_fee_bp" not in referenced_names
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.add(node.module or "")
    assert "wf_seal_consumption" not in imported_names
