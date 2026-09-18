from __future__ import annotations

from typing import Any

import pytest

from app.services.fill_event_handoff.broker_risk import (
    CATEGORY_CANCEL_FAILED,
    CATEGORY_DUPLICATE_ORDER,
    CATEGORY_LIMIT_EXCEEDED,
    CATEGORY_ORDER_STATE_UNKNOWN,
    BrokerRiskConfig,
    BrokerRiskDetector,
    render_risk_push_text,
)


def _fill() -> dict[str, Any]:
    return {
        "ledger_id": 10,
        "broker": "upbit",
        "account_mode": "live",
        "venue": "upbit",
        "market": "crypto",
        "symbol": "BTC",
        "side": "buy",
        "filled_qty": "1",
        "filled_price": "100",
        "filled_notional": "100",
        "currency": "KRW",
        "broker_order_id": "order-1",
        "fill_seq": 1,
    }


class _Evidence:
    def __init__(self) -> None:
        self.fills: list[dict[str, Any]] = []
        self.rungs: list[dict[str, Any]] = []
        self.cancels: list[dict[str, Any]] = []

    async def list_fills_for_order(self, **_kwargs: object) -> list[dict[str, Any]]:
        return self.fills

    async def list_rungs_for_broker_order(
        self, _broker_order_id: str
    ) -> list[dict[str, Any]]:
        return self.rungs

    async def list_cancel_proposals_for_target(
        self, _target_broker_order_id: str
    ) -> list[dict[str, Any]]:
        return self.cancels


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_order_has_both_ledger_rows_as_evidence() -> None:
    source = _Evidence()
    source.fills = [
        _fill(),
        dict(_fill(), ledger_id=11, fill_seq=2),
    ]

    risks = await BrokerRiskDetector().detect(_fill(), source=source)
    duplicate = next(
        risk for risk in risks if risk.category == CATEGORY_DUPLICATE_ORDER
    )

    assert duplicate.evidence["ledger_ids"] == [10, 11]
    assert duplicate.evidence["fill_seqs"] == [1, 2]
    assert duplicate.evidence["duplicate_fill_seqs"] == [2]
    assert "live:upbit:order-1" in duplicate.dedupe_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_state_unknown_has_rung_and_proposal_evidence() -> None:
    source = _Evidence()
    source.rungs = [
        {
            "rung_id": 31,
            "proposal_pk": 9,
            "state": "unverified",
            "void_reason": "broker_result_ambiguous",
            "correlation_id": "corr-9",
        }
    ]

    risks = await BrokerRiskDetector().detect(_fill(), source=source)
    unknown = next(
        risk for risk in risks if risk.category == CATEGORY_ORDER_STATE_UNKNOWN
    )

    assert unknown.evidence == {
        "ledger_id": 10,
        "broker_order_id": "order-1",
        "rung_ids": [31],
        "proposal_pks": [9],
        "void_reasons": ["broker_result_ambiguous"],
        "correlation_ids": ["corr-9"],
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_failed_has_dispatch_evidence() -> None:
    source = _Evidence()
    source.cancels = [
        {
            "proposal_row_id": 41,
            "proposal_id": "00000000-0000-0000-0000-000000000041",
            "lifecycle_state": "proposed",
            "approval_dispatch_state": "partial_failed",
            "approval_dispatch_failure_code": "telegram_timeout",
            "target_broker_order_id": "order-1",
        }
    ]

    risks = await BrokerRiskDetector().detect(_fill(), source=source)
    failed = next(risk for risk in risks if risk.category == CATEGORY_CANCEL_FAILED)

    assert failed.evidence["proposal_row_ids"] == [41]
    assert failed.evidence["approval_dispatch_states"] == ["partial_failed"]
    assert failed.evidence["approval_dispatch_failure_codes"] == ["telegram_timeout"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_limit_exceeded_is_observational_and_evidenced() -> None:
    source = _Evidence()
    detector = BrokerRiskDetector(BrokerRiskConfig(notional_caps={"KRW": 99}))

    risks = await detector.detect(_fill(), source=source)
    exceeded = next(risk for risk in risks if risk.category == CATEGORY_LIMIT_EXCEEDED)

    assert exceeded.evidence["ledger_id"] == 10
    assert exceeded.evidence["filled_notional"] == "100"
    assert exceeded.evidence["observation_cap"] == "99"
    assert "evidence:" in render_risk_push_text(exceeded)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_normal_fill_has_no_immediate_push_eligible_risk() -> None:
    assert await BrokerRiskDetector().detect(_fill(), source=_Evidence()) == []
