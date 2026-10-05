"""#1250 — pure rule for the Q-46 kis_mock expired[inference] close (no DB)."""

from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal
from typing import Any

import pytest

from app.services import kis_mock_inference_expiry as rule
from app.services.order_proposals.kis_leftover_inference import (
    EXPIRED_INFERENCE_CAVEAT,
    is_expired_inference_reason,
)

pytestmark = pytest.mark.unit

KST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime(2026, 10, 5, 6, 30, tzinfo=KST)


def kst(*parts: int) -> datetime.datetime:
    return datetime.datetime(*parts, tzinfo=KST)


BOUNDS = (kst(2026, 7, 21, 9, 0), kst(2026, 7, 21, 15, 30))


def _row(**changes: Any) -> rule.MockLedgerRowFacts:
    order_no = changes.pop("order_no", "0000012345")
    order_time = changes.pop("order_time", "101500")
    values: dict[str, Any] = {
        "ledger_id": 63,
        "lifecycle_state": "accepted",
        "status": "accepted",
        "account_mode": "kis_mock",
        "broker": "kis",
        "instrument_type": "equity_kr",
        "currency": "KRW",
        "side": "buy",
        "symbol": "005930",
        "order_type": "limit",
        "quantity": Decimal("2"),
        "price": Decimal("234500"),
        "order_no": order_no,
        "order_time": order_time,
        "trade_date": kst(2026, 7, 21, 10, 15, 1),
        "response_code": "0",
        "raw_response": {
            "rt_cd": "0",
            "msg_cd": "40600000",
            "msg": "accepted",
            "odno": order_no,
            "ord_tmd": order_time,
        },
        "scalping_role": None,
        "correlation_id": "corr-63",
        "last_reconcile_detail": None,
        "holdings_baseline_qty": Decimal("0"),
        "strategy": "buy_review mirror",
    }
    values.update(changes)
    return rule.MockLedgerRowFacts(**values)


def _evidence(row: rule.MockLedgerRowFacts | None = None, **changes: Any):
    values: dict[str, Any] = {
        "ledger_id": 63,
        "row": _row() if row is None else row,
        "session_bounds": BOUNDS,
        "order_exec_fills": (),
        "symbol_exec_fills": (),
        "symbol_rows": (),
        "correlation_rows": (),
    }
    values.update(changes)
    return rule.RowEvidence(**values)


def _fill(**changes: Any) -> rule.ExecFillFacts:
    values: dict[str, Any] = {
        "ledger_id": 9001,
        "broker_order_id": "12345",
        "symbol": "005930",
        "filled_at": kst(2026, 7, 21, 10, 20),
        "source": "websocket",
        "quarantined": False,
    }
    values.update(changes)
    return rule.ExecFillFacts(**values)


def _sibling(**changes: Any) -> rule.MockSiblingFacts:
    values: dict[str, Any] = {
        "ledger_id": 501,
        "symbol": "005930",
        "correlation_id": "other",
        "lifecycle_state": "fill",
        "last_reconcile_detail": {"reason_code": "fill_detected"},
        "trade_date": kst(2026, 7, 22, 10, 0),
        "reconciled_at": None,
    }
    values.update(changes)
    return rule.MockSiblingFacts(**values)


# Each scenario breaks exactly one condition (the mutant test reuses these).
ISOLATED_BREAKS: dict[str, tuple[rule.RowEvidence, datetime.datetime]] = {
    "kis_mock_accepted_buy_row": (_evidence(_row(side="sell")), NOW),
    "kis_mock_row_open": (_evidence(_row(lifecycle_state="anomaly")), NOW),
    "day_order": (_evidence(_row(order_type="ioc")), NOW),
    "regular_session_accept": (
        _evidence(_row(order_time="084500", trade_date=kst(2026, 7, 21, 8, 45, 1))),
        NOW,
    ),
    "day_close_passed": (_evidence(), kst(2026, 7, 21, 19, 59)),
    "no_fill_recorded_for_order": (
        _evidence(order_exec_fills=(_fill(filled_at=kst(2026, 7, 21, 10, 0)),)),
        NOW,
    ),
    "holding_quantity_unchanged": (
        _evidence(_row(holdings_baseline_qty=None)),
        NOW,
    ),
}


def test_baseline_row_is_eligible_with_the_1112_deadline() -> None:
    decision = rule.classify_row(_evidence(), now=NOW)
    assert decision.verdict == "eligible", decision.failed_conditions
    assert decision.accept_at == kst(2026, 7, 21, 10, 15)
    # ROB-671 conservative regular-session buy expiry: 20:00 KST NXT close.
    assert decision.deadline == kst(2026, 7, 21, 20, 0)
    assert decision.deadline_reason == "regular_buy_conservative_20_00"


@pytest.mark.parametrize("condition", sorted(ISOLATED_BREAKS))
def test_each_isolated_break_fails_only_its_condition(condition: str) -> None:
    evidence, now = ISOLATED_BREAKS[condition]
    decision = rule.classify_row(evidence, now=now)
    assert decision.verdict == "refused"
    assert decision.failed_conditions == (condition,)


def test_conditions_cover_the_1112_rule_minus_waived_strategy() -> None:
    names = [name for name, _ in rule.CONDITIONS]
    assert names == sorted(ISOLATED_BREAKS, key=names.index)
    assert set(names) == set(ISOLATED_BREAKS)
    assert rule.WAIVED_CONDITIONS == ("strategy_match", "reconcile_coverage")


def test_strategy_text_is_never_read_by_any_condition() -> None:
    for strategy in (None, "", "b0xk", "x" * 4000, "deep_limit_support_pullback"):
        decision = rule.classify_row(_evidence(_row(strategy=strategy)), now=NOW)
        assert decision.verdict == "eligible"


@pytest.mark.parametrize(
    "change",
    [
        {"account_mode": "kis_live"},
        {"account_mode": "live"},
        {"broker": "toss"},
        {"instrument_type": "equity_us"},
        {"currency": "USD"},
        {"status": "unknown"},
        {"status": "rejected"},
        {"scalping_role": "entry"},
        {"symbol": "AAPL"},
        {"order_no": None},
        {"order_no": "12A45"},
        {"response_code": None},
        {"response_code": "1"},
        {"raw_response": None},
        {"raw_response": {"rt_cd": "1", "odno": "0000012345", "ord_tmd": "101500"}},
        {"raw_response": {"rt_cd": "0", "odno": "999", "ord_tmd": "101500"}},
        {"raw_response": {"rt_cd": "0", "odno": "0000012345", "ord_tmd": "101501"}},
        {"raw_response": {"rt_cd": "0", "odno": "0000012345"}},
    ],
)
def test_not_a_kis_mock_accepted_buy_row_is_refused(change: dict[str, Any]) -> None:
    decision = rule.classify_row(_evidence(_row(**change)), now=NOW)
    assert decision.verdict == "refused"
    assert "kis_mock_accepted_buy_row" in decision.failed_conditions


@pytest.mark.parametrize(
    "state",
    ["expired", "cancelled", "reconciled", "stale", "failed", "fill", "anomaly"],
)
def test_terminal_or_non_open_row_is_refused(state: str) -> None:
    decision = rule.classify_row(_evidence(_row(lifecycle_state=state)), now=NOW)
    assert decision.verdict == "refused"
    assert "kis_mock_row_open" in decision.failed_conditions


@pytest.mark.parametrize(
    "change",
    [
        {"order_type": "market"},
        {"price": Decimal("0")},
        {"quantity": Decimal("0")},
        {"quantity": Decimal("1.5")},
        {"quantity": None},
        {"price": None},
    ],
)
def test_non_day_terms_are_refused(change: dict[str, Any]) -> None:
    decision = rule.classify_row(_evidence(_row(**change)), now=NOW)
    assert "day_order" in decision.failed_conditions


def test_market_zero_price_is_a_day_order() -> None:
    decision = rule.classify_row(
        _evidence(_row(order_type="market", price=Decimal("0"))), now=NOW
    )
    assert decision.verdict == "eligible"


@pytest.mark.parametrize(
    "change",
    [
        {"trade_date": kst(2026, 7, 21, 15, 45)},  # recorded after the close
        {"trade_date": kst(2026, 7, 20, 10, 15)},  # recorded on another day
        {"trade_date": None},
    ],
)
def test_recorded_send_instant_outside_session_is_refused(
    change: dict[str, Any],
) -> None:
    decision = rule.classify_row(_evidence(_row(**change)), now=NOW)
    assert "regular_session_accept" in decision.failed_conditions


def test_unknown_session_bounds_refuse() -> None:
    decision = rule.classify_row(_evidence(session_bounds=None), now=NOW)
    assert "regular_session_accept" in decision.failed_conditions
    assert "day_close_passed" in decision.failed_conditions


def test_deadline_is_strict() -> None:
    assert (
        "day_close_passed"
        in rule.classify_row(_evidence(), now=kst(2026, 7, 21, 20, 0)).failed_conditions
    )
    assert (
        rule.classify_row(_evidence(), now=kst(2026, 7, 21, 20, 0, 1)).verdict
        == "eligible"
    )


@pytest.mark.parametrize(
    "evidence_change",
    [
        {"order_exec_fills": (_fill(),)},
        {"order_exec_fills": (_fill(broker_order_id="0000012345"),)},
        {"order_exec_fills": (_fill(quarantined=True),)},
        {"order_exec_fills": (_fill(source="reconciler"),)},
        {"order_exec_fills": None},
        {"correlation_rows": None},
        {"correlation_rows": (_sibling(correlation_id="corr-63"),)},
        {
            "correlation_rows": (
                _sibling(
                    lifecycle_state="anomaly",
                    last_reconcile_detail={"reason_code": "attribution_unconfirmed"},
                ),
            )
        },
    ],
)
def test_any_recorded_fill_for_the_order_refuses(
    evidence_change: dict[str, Any],
) -> None:
    decision = rule.classify_row(_evidence(**evidence_change), now=NOW)
    assert "no_fill_recorded_for_order" in decision.failed_conditions


@pytest.mark.parametrize(
    "detail",
    [
        {"reason_code": "fill_detected"},
        {"reason_code": "partial_fill_detected", "attributed_fill_qty": "1"},
        {"reason_code": "position_reconciled"},
        {"reason_code": "holdings_mismatch"},
        {"reason_code": "attribution_unconfirmed"},
        {"reason_code": "pending_unconfirmed", "attributed_fill_qty": "1"},
        {"reason_code": "pending_unconfirmed", "attributed_fill_qty": "garbage"},
        {"reason_code": "pending_unconfirmed", "attributed_fill_qty": None},
        ["not", "a", "mapping"],
    ],
)
def test_own_detail_with_fill_evidence_refuses(detail: Any) -> None:
    decision = rule.classify_row(_evidence(_row(last_reconcile_detail=detail)), now=NOW)
    assert "no_fill_recorded_for_order" in decision.failed_conditions


@pytest.mark.parametrize(
    "detail",
    [
        None,
        {"reason_code": "pending_unconfirmed", "attributed_fill_qty": "0"},
        {"reason_code": "baseline_missing"},
        {"reason_code": "holdings_snapshot_missing"},
    ],
)
def test_own_detail_without_fill_evidence_is_not_a_fill(detail: Any) -> None:
    decision = rule.classify_row(_evidence(_row(last_reconcile_detail=detail)), now=NOW)
    assert decision.verdict == "eligible"


@pytest.mark.parametrize(
    "evidence_change",
    [
        {"symbol_exec_fills": (_fill(broker_order_id="777"),)},
        {"symbol_exec_fills": (_fill(broker_order_id="777", quarantined=True),)},
        {
            "symbol_exec_fills": (
                _fill(broker_order_id="777", filled_at=kst(2026, 7, 21, 10, 15)),
            )
        },
        {"symbol_exec_fills": None},
        {"symbol_rows": None},
        {"symbol_rows": (_sibling(),)},
        # placed before accept but reconciled after: may have moved the holding
        {
            "symbol_rows": (
                _sibling(
                    trade_date=kst(2026, 7, 20, 10, 0),
                    reconciled_at=kst(2026, 7, 22, 9, 0),
                ),
            )
        },
        # placed before accept, never reconciled: unknown
        {"symbol_rows": (_sibling(trade_date=kst(2026, 7, 20, 10, 0)),)},
        {
            "symbol_rows": (
                _sibling(
                    lifecycle_state="pending",
                    last_reconcile_detail={"reason_code": "holdings_mismatch"},
                ),
            )
        },
    ],
)
def test_symbol_holding_moved_or_unknown_refuses(
    evidence_change: dict[str, Any],
) -> None:
    decision = rule.classify_row(_evidence(**evidence_change), now=NOW)
    assert "holding_quantity_unchanged" in decision.failed_conditions


def test_symbol_history_that_ended_before_accept_does_not_block() -> None:
    evidence = _evidence(
        symbol_exec_fills=(
            _fill(broker_order_id="777", filled_at=kst(2026, 7, 1, 10, 0)),
        ),
        symbol_rows=(
            _sibling(
                trade_date=kst(2026, 7, 1, 10, 0),
                reconciled_at=kst(2026, 7, 2, 9, 0),
                lifecycle_state="reconciled",
            ),
            _sibling(
                ledger_id=502, lifecycle_state="cancelled", last_reconcile_detail=None
            ),
        ),
    )
    assert rule.classify_row(evidence, now=NOW).verdict == "eligible"


def test_missing_row_is_refused() -> None:
    decision = rule.classify_row(
        rule.RowEvidence(80, None, None, None, None, None, None), now=NOW
    )
    assert decision.verdict == "refused"
    assert decision.failed_conditions[0] == "row_missing"


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError):
        rule.classify_row(_evidence(), now=datetime.datetime(2026, 10, 5))


# ------------------------------------------------------------------ input


@pytest.mark.parametrize(
    "values",
    [
        ["80,66,64,63"],
        ["63", "64", "66", "80"],
        ["66,80", "63,64"],
    ],
)
def test_parse_ids_accepts_exactly_the_allowlist(values: list[str]) -> None:
    assert set(rule.parse_ids(values)) == {63, 64, 66, 80}


@pytest.mark.parametrize(
    "values",
    [
        ["80,66,64,63,81"],
        ["80,66,64,62"],
        ["80,66,64"],
        ["80"],
        ["80,66,64,63,63"],
        ["80,66,64,063"],
        ["80,66,64,+63"],
        ["80,66,64, 63"],
        ["80,66,64,6٣"],
        ["63-80"],
        ["80,66,,64,63"],
        [""],
        [],
    ],
)
def test_parse_ids_refuses_anything_else(values: list[str]) -> None:
    with pytest.raises(rule.InferenceInputError):
        rule.parse_ids(values)


@pytest.mark.parametrize(
    "value", ["Q-46 ", "q-46", "hk:task/706 Q-46", "Q46", "", None, "Q-47"]
)
def test_decision_ref_must_be_exactly_q46(value: Any) -> None:
    with pytest.raises(rule.InferenceInputError):
        rule.validate_decision_ref(value)
    assert rule.validate_decision_ref("Q-46") == "Q-46"


@pytest.mark.parametrize("value", [None, "", "   ", "a\nb", "a​b", "x" * 501])
def test_reason_text_is_bounded_and_single_line(value: Any) -> None:
    with pytest.raises(rule.InferenceInputError):
        rule.validate_text("reason", value, max_chars=rule.MAX_REASON_CHARS)


# ------------------------------------------------------------- batch + marker


def _decision(verdict: str, ledger_id: int) -> rule.RowDecision:
    return rule.RowDecision(ledger_id, verdict, (), None, None, None, "accepted", {})  # type: ignore[arg-type]


IDS = (80, 66, 64, 63)


def test_batch_all_eligible_is_eligible() -> None:
    decisions = tuple(_decision("eligible", i) for i in IDS)
    assert rule.decide_batch(IDS, decisions) == "eligible"


def test_batch_one_refused_refuses_everything() -> None:
    decisions = tuple(_decision("refused" if i == 64 else "eligible", i) for i in IDS)
    assert rule.decide_batch(IDS, decisions) == "refused"


def test_batch_all_closed_is_noop_but_mixed_closed_is_refused() -> None:
    assert (
        rule.decide_batch(IDS, tuple(_decision("already_closed", i) for i in IDS))
        == "noop"
    )
    mixed = tuple(
        _decision("already_closed" if i == 80 else "eligible", i) for i in IDS
    )
    assert rule.decide_batch(IDS, mixed) == "refused"


def test_batch_with_wrong_ids_is_refused() -> None:
    decisions = tuple(_decision("eligible", i) for i in (80, 66, 64, 81))
    assert rule.decide_batch((80, 66, 64, 81), decisions) == "refused"
    assert rule.decide_batch(IDS, decisions[:3]) == "refused"


def test_closed_detail_carries_the_1112_marker_caveat_and_decision_ref() -> None:
    decision = rule.classify_row(_evidence(), now=NOW)
    detail = rule.closed_detail(
        decision,
        decision_ref="Q-46",
        reason="r",
        actor="a",
        batch_id="b",
        closed_at=NOW,
    )
    assert is_expired_inference_reason(detail["reason_code"])
    assert detail["expiry_caveat"] == EXPIRED_INFERENCE_CAVEAT == "no_broker_original"
    assert detail["expiry_basis"] == "inference"
    assert detail["operator_decision_ref"] == "Q-46"
    assert detail["waived_conditions"] == ["strategy_match", "reconcile_coverage"]
    closed = dataclasses.replace(
        _row(), lifecycle_state="expired", last_reconcile_detail=detail
    )
    assert rule.is_closed_by_this_rule(closed)
    # The closed row with its audit row is already_closed, never re-evaluated.
    again = rule.classify_row(_evidence(closed, audit_recorded=True), now=NOW)
    assert again.verdict == "already_closed"
    # Marker without the audit row is inconsistent: refused, not noop.
    assert rule.classify_row(_evidence(closed), now=NOW).verdict == "refused"


def test_q46_tool_expiry_is_not_mistaken_for_this_rule() -> None:
    q46 = _row(
        lifecycle_state="expired",
        last_reconcile_detail={
            "reason_code": "operator_legacy_day_expired",
            "operator_decision_ref": "Q-46",
        },
    )
    assert not rule.is_closed_by_this_rule(q46)
    assert rule.classify_row(_evidence(q46, audit_recorded=True), now=NOW).verdict == (
        "refused"
    )


# ------------------------------------------------------- r2: exact int ids


@pytest.mark.parametrize(
    "ids",
    [
        (80, 66, 64, 63.0),
        (80.0, 66.0, 64.0, 63.0),
        (True, 66, 64, 80),
        ("80", 66, 64, 63),
        (80, 66, 64),
        (80, 66, 64, 63, 63),
        (80, 66, 64, 81),
    ],
)
def test_exact_ids_refuses_anything_but_the_four_builtin_ints(ids) -> None:
    with pytest.raises(rule.InferenceInputError):
        rule.exact_ids(ids)
    assert rule.decide_batch(ids, tuple(_decision("eligible", 63) for _ in ids)) == (
        "refused"
    )


def test_exact_ids_refuses_numpy_ints() -> None:
    np = pytest.importorskip("numpy")
    with pytest.raises(rule.InferenceInputError):
        rule.exact_ids(tuple(np.int64(x) for x in (80, 66, 64, 63)))


def test_exact_ids_accepts_the_four_in_any_order() -> None:
    assert rule.exact_ids([63, 80, 66, 64]) == (63, 80, 66, 64)


def test_validate_closed_detail_accepts_only_the_rule_marker() -> None:
    decision = rule.classify_row(_evidence(), now=NOW)
    good = rule.closed_detail(
        decision,
        decision_ref="Q-46",
        reason="r",
        actor="a",
        batch_id="b",
        closed_at=NOW,
    )
    rule.validate_closed_detail(good, 63)
    for bad in (
        {},
        None,
        {**good, "reason_code": "operator_legacy_day_expired"},
        {**good, "inference_rule": "other"},
        {**good, "rule_version": "v0"},
        {**good, "operator_decision_ref": "hk:task/706 Q-46"},
        {**good, "expiry_basis": "broker"},
        {**good, "expiry_caveat": None},
        {**good, "waived_conditions": ["strategy_match"]},
        {**good, "batch_id": ""},
        {k: v for k, v in good.items() if k != "batch_id"},
    ):
        with pytest.raises(rule.InferenceInputError):
            rule.validate_closed_detail(bad, 63)
