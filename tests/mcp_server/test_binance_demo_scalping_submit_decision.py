from __future__ import annotations

import inspect
import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

import app.mcp_server.tooling.binance_demo_scalping_handler as mod
from app.services.brokers.binance.demo_scalping.contract import (
    MarketConditions,
    ReasonCode,
)
from app.services.brokers.binance.demo_scalping.market_data import (
    MarketConditionsUnavailable,
)

_FRESH = MarketConditions(
    spread_bps=Decimal("3"),
    data_age_seconds=4.0,
    spot_free_base_qty=Decimal("0"),
)


def _preflight_result(
    status: str, *, reason_codes=(), sized_qty=None, sized_notional=None
):
    return type(
        "R",
        (),
        {
            "status": status,
            "reason_codes": tuple(reason_codes),
            "sized_qty": sized_qty,
            "sized_notional_usdt": sized_notional,
        },
    )()


@pytest.mark.asyncio
async def test_dry_run_returns_plan_no_order() -> None:
    # ROB-841: dry-run now runs the SAME server-derived market/risk preflight,
    # but places no order and inserts no ledger row. Seam is mocked so the unit
    # test does no network / DB.
    allowed = _preflight_result(
        "dry_run", sized_qty=Decimal("7.3"), sized_notional=Decimal("10")
    )
    with patch.object(
        mod, "_dry_run_preflight", AsyncMock(return_value=(_FRESH, allowed))
    ):
        result = await mod.binance_demo_scalping_submit_decision(
            symbol="XRPUSDT", side="BUY", rationale="funding flip", dry_run=True
        )
    assert result["status"] == "planned"
    assert result["dry_run"] is True
    assert result["symbol"] == "XRPUSDT"
    assert result["side"] == "BUY"
    assert result["session_tag"] == "llm"
    assert "rationale" in result
    # Server-observed market snapshot echoed back for auditability.
    assert result["market_conditions"]["spread_bps"] == "3"
    assert result["market_conditions"]["data_age_seconds"] == 4.0


@pytest.mark.asyncio
async def test_dry_run_blocked_surfaces_existing_reason_codes() -> None:
    blocked = _preflight_result("blocked", reason_codes=(ReasonCode.SPREAD_TOO_WIDE,))
    with patch.object(
        mod, "_dry_run_preflight", AsyncMock(return_value=(_FRESH, blocked))
    ):
        result = await mod.binance_demo_scalping_submit_decision(
            symbol="XRPUSDT", side="BUY", rationale="x", dry_run=True
        )
    assert result["status"] == "blocked"
    assert ReasonCode.SPREAD_TOO_WIDE in result["reason_codes"]
    assert result["dry_run"] is True


@pytest.mark.asyncio
async def test_dry_run_market_unavailable_fails_closed() -> None:
    with patch.object(
        mod,
        "_dry_run_preflight",
        AsyncMock(side_effect=MarketConditionsUnavailable("provider_error: boom")),
    ):
        result = await mod.binance_demo_scalping_submit_decision(
            symbol="XRPUSDT", side="BUY", rationale="x", dry_run=True
        )
    assert result["status"] == "market_conditions_unavailable"
    assert result["dry_run"] is True
    assert "provider_error" in result["reason"]


@pytest.mark.asyncio
async def test_rejects_non_allowlisted_symbol() -> None:
    result = await mod.binance_demo_scalping_submit_decision(
        symbol="BTCUSDT", side="BUY", rationale="x", dry_run=True
    )
    assert result["status"] == "rejected"
    assert "symbol" in result["error"].lower()


@pytest.mark.asyncio
async def test_rejects_empty_rationale() -> None:
    result = await mod.binance_demo_scalping_submit_decision(
        symbol="XRPUSDT", side="BUY", rationale="  ", dry_run=True
    )
    assert result["status"] == "rejected"
    assert "rationale" in result["error"].lower()


@pytest.mark.asyncio
async def test_confirm_executes_monitored_with_llm_tag() -> None:
    fake_result = type(
        "R",
        (),
        {
            "status": "reconciled",
            "open_client_order_id": "rob307-x",
            "close_client_order_id": "rob307-y",
            "exit_reason": "take_profit",
            "to_evidence_dict": lambda self: {"status": "reconciled"},
        },
    )()
    captured: dict = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return fake_result

    with patch.object(
        mod, "_execute_confirmed_round_trip", AsyncMock(side_effect=fake_run)
    ):
        result = await mod.binance_demo_scalping_submit_decision(
            symbol="SOLUSDT",
            side="SELL",
            rationale="OI surge fade",
            dry_run=False,
            confirm=True,
        )
    assert result["status"] == "reconciled"
    assert result["dry_run"] is False
    assert captured["session_tag"] == "llm"
    assert captured["signal_snapshot"]["rationale"] == "OI surge fade"
    assert captured["signal_snapshot"]["source"] == "llm"
    assert captured["symbol"] == "SOLUSDT"
    assert captured["side"] == "SELL"


@pytest.mark.asyncio
async def test_confirm_market_unavailable_fails_closed_no_order() -> None:
    with patch.object(
        mod,
        "_execute_confirmed_round_trip",
        AsyncMock(side_effect=MarketConditionsUnavailable("empty_kline")),
    ):
        result = await mod.binance_demo_scalping_submit_decision(
            symbol="XRPUSDT",
            side="BUY",
            rationale="x",
            dry_run=False,
            confirm=True,
        )
    assert result["status"] == "market_conditions_unavailable"
    assert result["dry_run"] is False
    assert "empty_kline" in result["reason"]


@pytest.mark.asyncio
async def test_confirm_required_for_real_order() -> None:
    # dry_run False but confirm False → still a plan (dry-run preflight), no order.
    allowed = _preflight_result(
        "dry_run", sized_qty=Decimal("7.3"), sized_notional=Decimal("10")
    )
    with patch.object(
        mod, "_dry_run_preflight", AsyncMock(return_value=(_FRESH, allowed))
    ):
        result = await mod.binance_demo_scalping_submit_decision(
            symbol="XRPUSDT", side="BUY", rationale="x", dry_run=False, confirm=False
        )
    assert result["status"] == "planned"
    assert result["dry_run"] is True


def _confirmed_result():
    return type(
        "R",
        (),
        {
            "status": "reconciled",
            "open_client_order_id": "rob937-x",
            "close_client_order_id": "rob937-y",
            "exit_reason": "take_profit",
            "to_evidence_dict": lambda self: {"status": "reconciled"},
        },
    )()


_GATE_ENV = "BINANCE_DEMO_SCALPING_VALIDATED_GATE_PATH"


@pytest.mark.asyncio
async def test_confirm_response_carries_gate_audit_and_authorization_mode(
    monkeypatch,
) -> None:
    # ROB-937: the interactive LLM confirm path is INTENTIONALLY exempt from the
    # ROB-905 validated-signal gate. It must still surface the gate verdict
    # (audit only) plus an explicit authorization marker — the exemption is
    # documented, not silent. Default env => gate is unset => allowed=False.
    monkeypatch.delenv(_GATE_ENV, raising=False)
    captured: dict = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return _confirmed_result()

    with patch.object(
        mod, "_execute_confirmed_round_trip", AsyncMock(side_effect=fake_run)
    ):
        result = await mod.binance_demo_scalping_submit_decision(
            symbol="SOLUSDT",
            side="SELL",
            rationale="OI surge fade",
            dry_run=False,
            confirm=True,
        )
    # Execution is UNCHANGED: the round-trip still ran despite the gate denying.
    assert captured, "execute_confirmed_round_trip must run regardless of gate"
    assert result["status"] == "reconciled"
    assert result["authorization_mode"] == "operator_interactive_exception"
    assert result["validated_signal_gate"]["allowed"] is False
    assert result["validated_signal_gate"]["reason"] == "gate_path_unset"


@pytest.mark.asyncio
async def test_confirm_executes_even_when_gate_denies(monkeypatch) -> None:
    # Core execution-invariance proof: gate denied (unset path) must NOT block or
    # downgrade the real Demo round-trip on this human-authorized path.
    monkeypatch.delenv(_GATE_ENV, raising=False)
    captured: dict = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return _confirmed_result()

    with patch.object(
        mod, "_execute_confirmed_round_trip", AsyncMock(side_effect=fake_run)
    ):
        result = await mod.binance_demo_scalping_submit_decision(
            symbol="XRPUSDT",
            side="BUY",
            rationale="funding flip",
            dry_run=False,
            confirm=True,
        )
    assert captured["symbol"] == "XRPUSDT"
    assert captured["session_tag"] == "llm"
    assert result["dry_run"] is False
    assert result["status"] == "reconciled"
    assert result["validated_signal_gate"]["allowed"] is False


@pytest.mark.asyncio
async def test_confirm_gate_allowed_reflected_with_valid_artifact(
    monkeypatch, tmp_path
) -> None:
    gate_file = tmp_path / "gate.json"
    gate_file.write_text(
        json.dumps({"schema": "validated_signal_gate.v1", "verdict": "validated"}),
        encoding="utf-8",
    )
    monkeypatch.setenv(_GATE_ENV, str(gate_file))
    captured: dict = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return _confirmed_result()

    with patch.object(
        mod, "_execute_confirmed_round_trip", AsyncMock(side_effect=fake_run)
    ):
        result = await mod.binance_demo_scalping_submit_decision(
            symbol="DOGEUSDT",
            side="BUY",
            rationale="breakout",
            dry_run=False,
            confirm=True,
        )
    # Same execution path; only the audit verdict flips to allowed=True.
    assert captured, "round-trip still runs"
    assert result["authorization_mode"] == "operator_interactive_exception"
    assert result["validated_signal_gate"]["allowed"] is True
    assert result["validated_signal_gate"]["reason"] == "validated"


@pytest.mark.asyncio
async def test_dry_run_response_carries_gate_audit_field(monkeypatch) -> None:
    monkeypatch.delenv(_GATE_ENV, raising=False)
    allowed = _preflight_result(
        "dry_run", sized_qty=Decimal("7.3"), sized_notional=Decimal("10")
    )
    with patch.object(
        mod, "_dry_run_preflight", AsyncMock(return_value=(_FRESH, allowed))
    ):
        result = await mod.binance_demo_scalping_submit_decision(
            symbol="XRPUSDT", side="BUY", rationale="funding flip", dry_run=True
        )
    assert result["status"] == "planned"
    assert result["validated_signal_gate"]["allowed"] is False
    assert result["validated_signal_gate"]["reason"] == "gate_path_unset"
    # dry-run must be visibly distinct from the real-order authorization marker.
    assert result["authorization_mode"] != "operator_interactive_exception"


def test_public_contract_has_no_caller_controlled_market_fields() -> None:
    # AC5: spread / data-age must be server-observed only — the public tool
    # signature must never accept them as inputs.
    params = set(
        inspect.signature(mod.binance_demo_scalping_submit_decision).parameters
    )
    forbidden = {
        "spread",
        "spread_bps",
        "data_age",
        "data_age_seconds",
        "market",
        "market_conditions",
    }
    assert params.isdisjoint(forbidden), (
        f"caller-controlled market fields: {params & forbidden}"
    )
