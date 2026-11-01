"""Contract v1 types, fill models, registry rejection (Acceptance 3), printer."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.execution_ledger import ExecutionLedger
from research.eval_contract.adapters import kr_d3
from research.eval_contract.contract import (
    BRACKET_CONSERVATIVE,
    BRACKET_OPTIMISTIC,
    ContractError,
    CostModel,
    Decision,
    DecisionAction,
    EvaluationReport,
    FillModelResult,
    FillRow,
    Intent,
    Market,
    MissingRegistryReference,
    OrderSpec,
    Outcome,
    Provenance,
    RegistryRef,
    Side,
    sha256_canonical,
)
from research.eval_contract.fill_models import (
    ConservativeThrough,
    EmpiricalFillModel,
    EmpiricalModelNotFitted,
    FillContext,
    FillInputMissing,
    FillObservation,
    OptimisticTouch,
    VolumeShare,
)
from research.eval_contract.outcomes import buy_markouts
from research.eval_contract.report import render_report
from tests.research.eval_contract.helpers import REGISTRY, make_run_input, ticks

T0 = datetime(2026, 11, 2, 0, 30, tzinfo=UTC)
SHA = "cd" * 32


def _fill_row(**overrides: object) -> FillRow:
    base: dict[str, object] = {
        "venue": "KRX",
        "instrument_type": "equity_kr",
        "symbol": "005930",
        "raw_symbol": "005930",
        "side": Side.BUY,
        "broker_order_id": "o1",
        "filled_qty": Decimal(3),
        "filled_price": Decimal(9600),
        "filled_notional": Decimal(28800),
        "fee_amount": Decimal("61.92"),
        "fee_currency": "KRW",
        "filled_at": T0,
        "currency": "KRW",
        "correlation_id": "d1",
        "source": "simulated:optimistic_touch",
    }
    base.update(overrides)
    return FillRow(**base)  # type: ignore[arg-type]


def _ok(model_id: str) -> FillModelResult:
    return FillModelResult(
        fill_model_id=model_id,
        params_sha256=SHA,
        status="ok",
        capital=Decimal(100),
        terminal_value=Decimal(101),
        cash_baseline=Decimal(100),
    )


def _provenance(registry: RegistryRef | None) -> Provenance:
    return Provenance(
        code_ref="abc123",
        data_manifest_sha256=SHA,
        config_sha256=SHA,
        registry=registry,
    )


# ------------------------------------------------------------- ledger shape


def test_fill_row_columns_are_execution_ledger_columns() -> None:
    ledger_columns = set(ExecutionLedger.__table__.columns.keys())
    row_columns = {f.name for f in dataclasses.fields(FillRow)}
    assert row_columns <= ledger_columns
    # Every NOT NULL ledger column without a server default is present.
    required = {
        c.name
        for c in ExecutionLedger.__table__.columns
        if not c.nullable
        and c.server_default is None
        and not c.primary_key
        and c.name not in {"created_at", "updated_at"}
    }
    assert required <= row_columns
    assert set(_fill_row().as_ledger_row()) == row_columns


def test_fill_row_is_marked_simulated_and_consistent() -> None:
    row = _fill_row()
    assert row.account_mode == "backtest" and row.broker == "simulator"
    with pytest.raises(ContractError, match="simulated"):
        _fill_row(account_mode="live")
    with pytest.raises(ContractError, match="simulated"):
        _fill_row(source="reconciler")
    with pytest.raises(ContractError, match="filled_notional"):
        _fill_row(filled_notional=Decimal(28801))
    with pytest.raises(ContractError, match="timezone"):
        _fill_row(filled_at=datetime(2026, 11, 2))  # noqa: DTZ001


# ------------------------------------------------------- pipeline invariants


def test_decision_rejects_look_ahead() -> None:
    with pytest.raises(ContractError, match="look-ahead"):
        Decision(
            decision_id="d",
            hypothesis_id="H",
            market=Market.KR,
            symbol="005930",
            action=DecisionAction.ENTER,
            decided_at=T0,
            data_cutoff=T0 + timedelta(seconds=1),
        )


def test_outcome_horizons_and_censoring() -> None:
    assert Outcome(order_id="o", horizon=20, censored=True).net_vs_cash is None
    with pytest.raises(ContractError, match="horizon"):
        Outcome(order_id="o", horizon=10, censored=True)
    with pytest.raises(ContractError, match="censored"):
        Outcome(order_id="o", horizon=1, censored=True, gross_return=Decimal(0))
    with pytest.raises(ContractError, match="every value"):
        Outcome(order_id="o", horizon=1, censored=False, gross_return=Decimal(0))


def test_canonical_hash_is_stable_and_refuses_floats() -> None:
    a = sha256_canonical({"b": Decimal("1.10"), "a": [T0, Side.BUY]})
    b = sha256_canonical({"a": [T0, Side.BUY], "b": Decimal("1.1")})
    assert a == b and len(a) == 64
    with pytest.raises(ContractError, match="float"):
        sha256_canonical({"x": 1.1})


def test_net_vs_cash_is_primary_and_fill_rate_is_none_without_orders() -> None:
    result = _ok(BRACKET_OPTIMISTIC)
    assert result.net_vs_cash == Decimal("0.01")
    assert result.fill_rate is None


def test_unavailable_result_carries_no_numbers() -> None:
    with pytest.raises(ContractError, match="no numbers"):
        FillModelResult(
            fill_model_id="volume_share",
            params_sha256=SHA,
            status="unavailable",
            unavailable_reason="x",
            capital=Decimal(1),
        )


# ------------------------------------------------- Acceptance 3: registry


def test_provenance_without_registry_is_rejected() -> None:
    with pytest.raises(MissingRegistryReference):
        _provenance(None)


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"registry": None},
        {"registry": {"hypothesis_id": "H", "k": 1}},
        {"registry": {"hypothesis_id": "H", "registry_hash": "", "k": 1}},
    ],
)
def test_serialised_report_without_registry_hash_is_rejected(
    header: dict[str, object],
) -> None:
    with pytest.raises(MissingRegistryReference):
        EvaluationReport.check_registry_header(header)


def test_serialised_header_with_registry_is_accepted() -> None:
    ref = EvaluationReport.check_registry_header(
        {"registry": {"hypothesis_id": "H", "registry_hash": SHA, "k": 3}}
    )
    assert ref == RegistryRef("H", SHA, 3)


@pytest.mark.parametrize(
    ("registry_hash", "k"),
    [("short", 1), ("CD" * 32, 1), (SHA, 0), (SHA, True)],
)
def test_registry_ref_shape(registry_hash: str, k: int) -> None:
    with pytest.raises(ContractError):
        RegistryRef("H", registry_hash, k)


def test_adapter_refuses_a_missing_registry_before_running() -> None:
    with pytest.raises(MissingRegistryReference):
        kr_d3.evaluate(make_run_input(), ticks(), registry=None, code_ref="t")


def test_printer_refuses_a_report_whose_registry_was_stripped() -> None:
    report = kr_d3.evaluate(make_run_input(), ticks(), registry=REGISTRY, code_ref="t")
    tampered = object.__new__(Provenance)
    for f in dataclasses.fields(Provenance):
        object.__setattr__(tampered, f.name, getattr(report.provenance, f.name))
    object.__setattr__(tampered, "registry", None)
    object.__setattr__(report, "provenance", tampered)
    with pytest.raises(MissingRegistryReference):
        render_report(report)


def test_report_requires_both_bracket_ends() -> None:
    provenance = _provenance(RegistryRef("H", SHA, 1))
    with pytest.raises(ContractError, match=BRACKET_CONSERVATIVE):
        EvaluationReport("H", Market.KR, provenance, (_ok(BRACKET_OPTIMISTIC),))
    report = EvaluationReport(
        "H", Market.KR, provenance, (_ok(BRACKET_OPTIMISTIC), _ok(BRACKET_CONSERVATIVE))
    )
    assert report.result(BRACKET_CONSERVATIVE).net_vs_cash == Decimal("0.01")


# ------------------------------------------------------------- fill models


def _ctx(**overrides: object) -> FillContext:
    base: dict[str, object] = {
        "side": Side.BUY,
        "limit": Decimal(100),
        "quantity": Decimal(10),
        "tick": Decimal(1),
        "optimistic_extreme": Decimal(100),
        "conservative_extreme": Decimal(100),
        "first_price": Decimal(103),
        "optimistic_window_volume": Decimal(1000),
    }
    base.update(overrides)
    return FillContext(**base)  # type: ignore[arg-type]


def test_bracket_touch_versus_through() -> None:
    assert OptimisticTouch().fill(_ctx()).price == Decimal(100)
    assert ConservativeThrough().fill(_ctx()).any_fill is False
    assert ConservativeThrough().fill(_ctx(conservative_extreme=Decimal(99))).price == (
        Decimal(100)
    )
    # Conservative never reads the optimistic window.
    assert (
        ConservativeThrough()
        .fill(_ctx(optimistic_extreme=Decimal(1), conservative_extreme=None))
        .any_fill
        is False
    )


def test_sell_side_mirrors_buy_side() -> None:
    sell = _ctx(
        side=Side.SELL,
        optimistic_extreme=Decimal(100),
        conservative_extreme=Decimal(100),
        first_price=Decimal(97),
    )
    assert OptimisticTouch().fill(sell).price == Decimal(100)
    assert ConservativeThrough().fill(sell).any_fill is False
    assert ConservativeThrough().fill(
        _ctx(
            side=Side.SELL, first_price=Decimal(101), conservative_extreme=Decimal(100)
        )
    ).price == (Decimal(101))


def test_volume_share_caps_by_participation_and_needs_volume() -> None:
    partial = VolumeShare(Decimal("0.1")).fill(
        _ctx(optimistic_window_volume=Decimal(45))
    )
    assert partial.filled_qty == Decimal(4) and partial.reason == "partial_capacity"
    full = VolumeShare(Decimal("0.1")).fill(_ctx())
    assert full.filled_qty == Decimal(10)
    assert VolumeShare().fill(_ctx(optimistic_extreme=Decimal(101))).any_fill is False
    assert VolumeShare().fill(_ctx(optimistic_window_volume=Decimal(5))).reason == (
        "no_capacity"
    )
    with pytest.raises(FillInputMissing):
        VolumeShare().fill(_ctx(optimistic_window_volume=None))


def test_empirical_is_a_hook_and_never_claims_a_fit_from_two_fills() -> None:
    model = EmpiricalFillModel()
    two_fills = [FillObservation(_ctx(), Decimal(10)) for _ in range(2)]
    status = model.fit(two_fills)
    assert status.status == "insufficient_sample"
    assert (status.n_observations, status.n_filled) == (2, 2)
    assert model.fit(two_fills * 20).status == "fit_not_implemented"
    assert model.params()["fitted"] is False
    with pytest.raises(EmpiricalModelNotFitted):
        model.fill(_ctx())


# ----------------------------------------------------------------- printer


def test_printer_order_is_primary_then_markouts_then_diagnostic() -> None:
    report = kr_d3.evaluate(
        make_run_input(last_low=Decimal(9450)), ticks(), registry=REGISTRY, code_ref="t"
    )
    text = render_report(report)
    primary = text.index("## Primary — net return vs cash at equal capital")
    markouts = text.index("## Markouts R_h")
    diagnostic = text.index("## Diagnostic — fill rate (not a performance metric)")
    assert primary < markouts < diagnostic
    assert f"hash={REGISTRY.registry_hash} k={REGISTRY.k}" in text
    assert "bracket: optimistic end" in text and "bracket: conservative end" in text
    assert "Bracket (conservative .. optimistic):" in text
    for h in (1, 5, 20):
        assert f"| {BRACKET_OPTIMISTIC} | {h} |" in text
    assert "unavailable: FillInputMissing" in text
    assert f"| {BRACKET_CONSERVATIVE} | 2 | 1 | 50.0% |" in text
    assert text == render_report(report)


# ------------------------------------------------ review round 1 regressions


def test_markout_cash_leg_and_sell_tax() -> None:
    costs = CostModel(
        currency="KRW",
        buy_fee_rate=Decimal("0.001"),
        sell_fee_rate=Decimal("0.002"),
        sell_tax_rate=Decimal("0.003"),
    )
    closes = [Decimal(100), Decimal(101), None, None, None, Decimal(110)]
    out = {
        o.horizon: o
        for o in buy_markouts(
            order_id="o",
            entry_price=Decimal(100),
            entry_index=0,
            closes=closes,
            costs=costs,
            cash_rate_per_session=Decimal("0.01"),
        )
    }
    net1 = Decimal(101) * Decimal("0.995") / (Decimal(100) * Decimal("1.001")) - 1
    assert out[1].net_return == net1
    assert out[1].cash_return == Decimal("0.01")
    assert out[1].net_vs_cash == net1 - Decimal("0.01")
    net5 = Decimal(110) * Decimal("0.995") / (Decimal(100) * Decimal("1.001")) - 1
    assert out[5].cash_return == Decimal("1.01") ** 5 - 1
    assert out[5].net_vs_cash == net5 - (Decimal("1.01") ** 5 - 1)
    assert out[20].censored is True
    with pytest.raises(ValueError, match="close"):
        buy_markouts(
            order_id="o",
            entry_price=Decimal(100),
            entry_index=2,
            closes=closes,
            costs=costs,
        )


def test_net_vs_cash_uses_the_cash_baseline_not_capital() -> None:
    result = dataclasses.replace(
        _ok(BRACKET_OPTIMISTIC), cash_baseline=Decimal("100.5")
    )
    assert result.net_vs_cash == Decimal("0.005")


def _chain(**changes: object) -> FillModelResult:
    """A minimal valid decision -> intent -> order -> fill -> outcome chain."""

    decision = Decision(
        decision_id="d1",
        hypothesis_id="H",
        market=Market.KR,
        symbol="005930",
        action=DecisionAction.ENTER,
        decided_at=T0,
        data_cutoff=T0 - timedelta(hours=17),
    )
    intent = Intent(
        intent_id="d1",
        decision_id="d1",
        side=Side.BUY,
        max_notional=Decimal(28800),
        valid_until=changes.pop("valid_until", T0 + timedelta(hours=7)),  # type: ignore[arg-type]
    )
    order = OrderSpec(
        order_id="o1",
        intent_id="d1",
        market=Market.KR,
        symbol="005930",
        side=Side.BUY,
        limit_price=Decimal(9600),
        quantity=Decimal(3),
        submitted_at=changes.pop("submitted_at", T0),  # type: ignore[arg-type]
        expires_at=changes.pop("expires_at", T0 + timedelta(hours=7)),  # type: ignore[arg-type]
    )
    fill = _fill_row(filled_at=changes.pop("filled_at", T0 + timedelta(hours=7)))
    outcome = Outcome(
        order_id=str(changes.pop("outcome_order", "o1")), horizon=1, censored=True
    )
    assert not changes
    return dataclasses.replace(
        _ok(BRACKET_OPTIMISTIC),
        decisions=(decision,),
        intents=(intent,),
        orders=(order,),
        fills=(fill,),
        outcomes=(outcome,),
    )


def test_valid_chain_is_accepted() -> None:
    assert _chain().fill_rate == Decimal(1)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"filled_at": T0 - timedelta(minutes=30)}, "outside its order"),
        ({"filled_at": T0 + timedelta(hours=8)}, "outside its order"),
        ({"submitted_at": T0 - timedelta(minutes=1)}, "before its decision"),
        ({"expires_at": T0 + timedelta(days=30)}, "outlives its intent"),
        ({"valid_until": T0}, "expires before its decision"),
        ({"outcome_order": "o2"}, "unfilled order"),
    ],
)
def test_chain_rejects_time_travel(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ContractError, match=message):
        _chain(**changes)


def test_printer_refuses_an_invalid_registry_built_around_its_constructor() -> None:
    report = kr_d3.evaluate(make_run_input(), ticks(), registry=REGISTRY, code_ref="t")
    bogus = object.__new__(RegistryRef)
    object.__setattr__(bogus, "hypothesis_id", REGISTRY.hypothesis_id)
    object.__setattr__(bogus, "registry_hash", "")
    object.__setattr__(bogus, "k", 0)
    object.__setattr__(report.provenance, "registry", bogus)
    with pytest.raises(MissingRegistryReference, match="invalid registry"):
        render_report(report)


def test_printer_refuses_a_foreign_registry_or_a_missing_bracket_end() -> None:
    report = kr_d3.evaluate(make_run_input(), ticks(), registry=REGISTRY, code_ref="t")
    object.__setattr__(report.provenance, "registry", RegistryRef("H-other", SHA, 2))
    with pytest.raises(ContractError, match="registry entry"):
        render_report(report)
    report = kr_d3.evaluate(make_run_input(), ticks(), registry=REGISTRY, code_ref="t")
    object.__setattr__(
        report,
        "results",
        tuple(r for r in report.results if r.fill_model_id != BRACKET_CONSERVATIVE),
    )
    with pytest.raises(ContractError, match="bracket end"):
        render_report(report)


def test_conservative_gap_needs_a_first_price_through_the_limit() -> None:
    at_limit = _ctx(first_price=Decimal(100), conservative_extreme=Decimal(100))
    assert ConservativeThrough().fill(at_limit).any_fill is False
    assert OptimisticTouch().fill(at_limit).price == Decimal(100)
    through = _ctx(first_price=Decimal(99), conservative_extreme=Decimal(99))
    assert ConservativeThrough().fill(through).price == Decimal(99)


def test_volume_share_keeps_the_gap_price_on_a_partial_fill() -> None:
    out = VolumeShare(Decimal("0.1")).fill(
        _ctx(first_price=Decimal(97), optimistic_window_volume=Decimal(45))
    )
    assert (out.filled_qty, out.price) == (Decimal(4), Decimal(97))


def test_canonical_hash_rejects_non_string_keys() -> None:
    with pytest.raises(ContractError, match="keys"):
        sha256_canonical({1: "a", "1": "b"})


def test_simulated_venue_matches_the_kis_kr_ledger_normalizer() -> None:
    report = kr_d3.evaluate(make_run_input(), ticks(), registry=REGISTRY, code_ref="t")
    assert {f.venue for f in report.result(BRACKET_OPTIMISTIC).fills} == {"krx"}


# ------------------------------------------------ review round 2 (mergewake)


def test_partial_fills_may_not_sum_past_the_order_quantity() -> None:
    chain = _chain()
    first = _fill_row(
        filled_qty=Decimal(2),
        filled_notional=Decimal(19200),
        filled_at=T0 + timedelta(hours=1),
    )
    second = dataclasses.replace(first, fill_seq=1, filled_at=T0 + timedelta(hours=2))
    with pytest.raises(ContractError, match="exceed their order quantity"):
        dataclasses.replace(chain, fills=(first, second))
    third = _fill_row(
        filled_qty=Decimal(1),
        filled_notional=Decimal(9600),
        fill_seq=1,
        filled_at=T0 + timedelta(hours=2),
    )
    assert dataclasses.replace(chain, fills=(first, third)).fill_rate == Decimal(1)


@pytest.mark.parametrize(
    "registry",
    [
        {"hypothesis_id": "H", "registry_hash": SHA},
        {"hypothesis_id": "H", "registry_hash": "not-a-sha", "k": 1},
        {"registry_hash": SHA, "k": 1},
    ],
)
def test_malformed_registry_header_is_a_registry_failure(
    registry: dict[str, object],
) -> None:
    with pytest.raises(MissingRegistryReference, match="invalid registry header"):
        EvaluationReport.check_registry_header({"registry": registry})
