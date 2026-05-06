"""Tests for ROB-85 Alpaca Paper fill/reconcile smoke."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from scripts.smoke import alpaca_paper_fill_reconcile_smoke as smoke


@dataclass
class FakeLedgerRow:
    client_order_id: str
    lifecycle_state: str = "previewed"
    reconcile_status: str | None = None


class FakeLedger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.row = FakeLedgerRow(client_order_id="")

    async def record_preview(self, **kwargs: Any) -> FakeLedgerRow:
        self.calls.append(("record_preview", kwargs))
        self.row.client_order_id = kwargs["client_order_id"]
        self.row.lifecycle_state = kwargs.get("lifecycle_state", "previewed")
        return self.row

    async def record_submit(
        self,
        client_order_id: str,
        order: dict[str, Any],
        raw_response: dict[str, Any] | None = None,
    ) -> FakeLedgerRow:
        self.calls.append(
            (
                "record_submit",
                {
                    "client_order_id": client_order_id,
                    "order": order,
                    "raw_response": raw_response,
                },
            )
        )
        self.row.client_order_id = client_order_id
        self.row.lifecycle_state = str(order.get("status") or "submitted")
        return self.row

    async def record_status(
        self,
        client_order_id: str,
        order: dict[str, Any],
        raw_response: dict[str, Any] | None = None,
    ) -> FakeLedgerRow:
        self.calls.append(
            (
                "record_status",
                {
                    "client_order_id": client_order_id,
                    "order": order,
                    "raw_response": raw_response,
                },
            )
        )
        self.row.lifecycle_state = str(order.get("status") or "status")
        return self.row

    async def record_position_snapshot(
        self,
        client_order_id: str,
        position: dict[str, Any] | None,
        raw_response: dict[str, Any] | None = None,
    ) -> FakeLedgerRow:
        self.calls.append(
            (
                "record_position_snapshot",
                {
                    "client_order_id": client_order_id,
                    "position": position,
                    "raw_response": raw_response,
                },
            )
        )
        return self.row

    async def record_reconcile(
        self,
        client_order_id: str,
        reconcile_status: str,
        notes: str | None = None,
        error_summary: str | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> FakeLedgerRow:
        self.calls.append(
            (
                "record_reconcile",
                {
                    "client_order_id": client_order_id,
                    "reconcile_status": reconcile_status,
                    "notes": notes,
                    "error_summary": error_summary,
                    "raw_response": raw_response,
                },
            )
        )
        self.row.reconcile_status = reconcile_status
        return self.row


def _payload(**overrides: Any) -> smoke.SmokePayload:
    params = {
        "signal_symbol": "KRW-BTC",
        "notional": Decimal("10"),
        "limit_price_usd": Decimal("65000"),
        "client_order_id": "rob85-fill-test",
        "now": datetime(2026, 5, 3, tzinfo=UTC),
    }
    params.update(overrides)
    return smoke.build_smoke_payload(**params)


def test_build_smoke_payload_separates_signal_and_execution() -> None:
    payload = _payload()

    assert payload.signal_symbol == "KRW-BTC"
    assert payload.signal_venue == "upbit"
    assert payload.execution_symbol == "BTC/USD"
    assert payload.execution_venue == "alpaca_paper"
    assert payload.order_request["asset_class"] == "crypto"
    assert payload.order_request["side"] == "buy"
    assert payload.order_request["type"] == "limit"
    assert payload.provenance.signal_symbol == "KRW-BTC"
    assert payload.provenance.execution_asset_class == "crypto"


def test_build_smoke_payload_rejects_unsupported_signal_symbol() -> None:
    with pytest.raises(smoke.SmokeStopError, match="unsupported crypto signal symbol"):
        _payload(signal_symbol="KRW-XRP")


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("notional", Decimal("10.01"), "notional exceeds"),
        ("limit_price_usd", None, "limit_price_usd is required"),
        ("time_in_force", "day", "time_in_force"),
        ("client_order_id", "x" * 49, "client_order_id"),
    ],
)
def test_build_smoke_payload_rejects_policy_violations(
    field: str, value: Any, match: str
) -> None:
    with pytest.raises(smoke.SmokeStopError, match=match):
        _payload(**{field: value})


@pytest.mark.parametrize(
    "snapshot",
    [
        smoke.PreflightSnapshot("active", Decimal("9.99"), 0, 0, 0, 0, 0),
        smoke.PreflightSnapshot("active", Decimal("100"), 1, 1, 0, 0, 0),
        smoke.PreflightSnapshot("active", Decimal("100"), 0, 0, 1, 1, 0),
        smoke.PreflightSnapshot("inactive", Decimal("100"), 0, 0, 0, 0, 0),
    ],
)
def test_validate_preflight_fails_closed(snapshot: smoke.PreflightSnapshot) -> None:
    with pytest.raises(smoke.SmokeStopError):
        smoke.validate_preflight(snapshot, _payload())


def test_validate_preflight_allows_clear_attribution() -> None:
    snapshot = smoke.PreflightSnapshot("active", Decimal("100"), 0, 0, 0, 0, 0)
    smoke.validate_preflight(snapshot, _payload())


@pytest.mark.asyncio
async def test_collect_preflight_snapshot_filters_execution_symbol() -> None:
    payload = _payload()

    async def account() -> dict[str, Any]:
        return {"account": {"status": "ACTIVE", "account_id": "raw-account-id"}}

    async def cash() -> dict[str, Any]:
        return {"cash": {"buying_power": "100"}}

    async def orders(**_: Any) -> dict[str, Any]:
        return {"orders": [{"symbol": "ETH/USD"}], "count": 1}

    async def positions() -> dict[str, Any]:
        return {"positions": [{"symbol": "SOL/USD", "qty": "1"}], "count": 1}

    async def fills(**_: Any) -> dict[str, Any]:
        return {"fills": [{"order_id": "other"}], "count": 1}

    snapshot = await smoke.collect_preflight_snapshot(
        payload,
        get_account_fn=account,
        get_cash_fn=cash,
        list_orders_fn=orders,
        list_positions_fn=positions,
        list_fills_fn=fills,
    )

    assert snapshot.account_status == "active"
    assert snapshot.buying_power == pytest.approx(Decimal("100"))
    assert snapshot.open_order_count == 1
    assert snapshot.execution_symbol_open_order_count == 0
    assert snapshot.position_count == 1
    assert snapshot.execution_symbol_position_count == 0
    assert snapshot.recent_fill_count == 1


@pytest.mark.asyncio
async def test_collect_preflight_snapshot_matches_slashless_crypto_symbol() -> None:
    payload = _payload()

    async def account() -> dict[str, Any]:
        return {"account": {"status": "ACTIVE"}}

    async def cash() -> dict[str, Any]:
        return {"cash": {"buying_power": "100"}}

    async def orders(**_: Any) -> dict[str, Any]:
        return {"orders": [{"symbol": "BTCUSD"}], "count": 1}

    async def positions() -> dict[str, Any]:
        return {"positions": [{"symbol": "BTCUSD", "qty": "0.001"}], "count": 1}

    async def fills(**_: Any) -> dict[str, Any]:
        return {"fills": [], "count": 0}

    snapshot = await smoke.collect_preflight_snapshot(
        payload,
        get_account_fn=account,
        get_cash_fn=cash,
        list_orders_fn=orders,
        list_positions_fn=positions,
        list_fills_fn=fills,
    )

    assert snapshot.execution_symbol_open_order_count == 1
    assert snapshot.execution_symbol_position_count == 1


@pytest.mark.asyncio
async def test_preview_confirm_false_validation_records_ledger_preview() -> None:
    payload = _payload()
    ledger = FakeLedger()
    preview_calls: list[dict[str, Any]] = []
    submit_calls: list[dict[str, Any]] = []

    async def preview(**kwargs: Any) -> dict[str, Any]:
        preview_calls.append(kwargs)
        return {
            "success": True,
            "preview": True,
            "submitted": False,
            "would_exceed_buying_power": False,
        }

    async def submit(**kwargs: Any) -> dict[str, Any]:
        submit_calls.append(kwargs)
        return {
            "success": True,
            "submitted": False,
            "blocked_reason": "confirmation_required",
            "client_order_id": payload.client_order_id,
        }

    result = await smoke.validate_preview_and_confirm_false(
        payload,
        preview_fn=preview,
        submit_fn=submit,
        ledger=ledger,  # type: ignore[arg-type]
    )

    assert result.preview["preview"] is True
    assert submit_calls == [{**payload.order_request, "confirm": False}]
    assert preview_calls == [payload.order_request]
    assert [name for name, _ in ledger.calls] == ["record_preview"]
    preview_kwargs = ledger.calls[0][1]
    assert preview_kwargs["client_order_id"] == payload.client_order_id
    assert preview_kwargs["execution_symbol"] == "BTC/USD"
    assert preview_kwargs["execution_venue"] == "alpaca_paper"
    assert preview_kwargs["validation_summary"]["preview_success"] is True


@pytest.mark.asyncio
async def test_preview_confirm_false_rejects_unexpected_submit() -> None:
    payload = _payload()

    async def preview(**_: Any) -> dict[str, Any]:
        return {"success": True, "preview": True, "submitted": False}

    async def submit(**_: Any) -> dict[str, Any]:
        return {"success": True, "submitted": True}

    with pytest.raises(smoke.SmokeStopError, match="unexpectedly submitted"):
        await smoke.validate_preview_and_confirm_false(
            payload, preview_fn=preview, submit_fn=submit
        )


async def _noop_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_execute_and_reconcile_filled_position_matched() -> None:
    payload = _payload()
    ledger = FakeLedger()

    async def submit(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["confirm"] is True
        return {
            "submitted": True,
            "client_order_id": payload.client_order_id,
            "order": {
                "id": "paper-order-1",
                "client_order_id": payload.client_order_id,
                "status": "accepted",
                "symbol": "BTC/USD",
            },
        }

    async def get_order(order_id: str) -> dict[str, Any]:
        assert order_id == "paper-order-1"
        return {
            "order": {
                "id": order_id,
                "client_order_id": payload.client_order_id,
                "status": "filled",
                "filled_qty": "0.001",
                "filled_avg_price": "65000",
            }
        }

    fill_calls: list[dict[str, Any]] = []

    async def fills(**kwargs: Any) -> dict[str, Any]:
        fill_calls.append(kwargs)
        return {"fills": [{"order_id": "paper-order-1", "qty": "0.001"}]}

    async def positions() -> dict[str, Any]:
        return {
            "positions": [
                {"symbol": "ETH/USD", "qty": "99"},
                {"symbol": "BTCUSD", "qty": "0.001"},
            ]
        }

    result = await smoke.execute_and_reconcile(
        payload,
        ledger=ledger,  # type: ignore[arg-type]
        submit_fn=submit,
        get_order_fn=get_order,
        list_fills_fn=fills,
        list_positions_fn=positions,
        sleep_fn=_noop_sleep,
    )

    assert result.reconcile_status == "filled_position_matched"
    assert result.fill_count == 1
    assert result.position_present is True
    assert [name for name, _ in ledger.calls] == [
        "record_submit",
        "record_status",
        "record_position_snapshot",
        "record_reconcile",
    ]
    assert fill_calls[0]["limit"] == 100
    position_call = next(
        payload for name, payload in ledger.calls if name == "record_position_snapshot"
    )
    assert position_call["position"] == pytest.approx(
        {"symbol": "BTCUSD", "qty": "0.001"}
    )
    assert position_call["raw_response"] == {
        "position": {"symbol": "BTCUSD", "qty": "0.001"},
        "execution_symbol": "BTC/USD",
    }


@pytest.mark.asyncio
async def test_execute_and_reconcile_partial_fill_position_matched() -> None:
    payload = _payload()
    ledger = FakeLedger()

    async def submit(**_: Any) -> dict[str, Any]:
        return {
            "submitted": True,
            "client_order_id": payload.client_order_id,
            "order": {"id": "paper-order-2", "status": "accepted"},
        }

    async def get_order(_: str) -> dict[str, Any]:
        return {"order": {"id": "paper-order-2", "status": "partially_filled"}}

    async def fills(**_: Any) -> dict[str, Any]:
        return {"fills": [{"order_id": "paper-order-2"}]}

    async def positions() -> dict[str, Any]:
        return {"positions": [{"symbol": "BTC/USD", "qty": "0.0005"}]}

    result = await smoke.execute_and_reconcile(
        payload,
        ledger=ledger,  # type: ignore[arg-type]
        submit_fn=submit,
        get_order_fn=get_order,
        list_fills_fn=fills,
        list_positions_fn=positions,
        sleep_fn=_noop_sleep,
    )

    assert result.reconcile_status == "partial_fill_position_matched"


@pytest.mark.asyncio
async def test_execute_and_reconcile_open_after_poll_timeout_no_cancel() -> None:
    payload = _payload()
    ledger = FakeLedger()
    get_order_calls = 0

    async def submit(**_: Any) -> dict[str, Any]:
        return {
            "submitted": True,
            "client_order_id": payload.client_order_id,
            "order": {"id": "paper-order-open", "status": "accepted"},
        }

    async def get_order(_: str) -> dict[str, Any]:
        nonlocal get_order_calls
        get_order_calls += 1
        return {"order": {"id": "paper-order-open", "status": "accepted"}}

    async def fills(**_: Any) -> dict[str, Any]:
        return {"fills": []}

    async def positions() -> dict[str, Any]:
        return {"positions": []}

    result = await smoke.execute_and_reconcile(
        payload,
        ledger=ledger,  # type: ignore[arg-type]
        submit_fn=submit,
        get_order_fn=get_order,
        list_fills_fn=fills,
        list_positions_fn=positions,
        poll_attempts=2,
        sleep_fn=_noop_sleep,
    )

    assert get_order_calls == 2
    assert result.reconcile_status == "open_after_poll_timeout"
    assert "record_cancel" not in [name for name, _ in ledger.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "expired"])
async def test_execute_and_reconcile_unexpected_terminal_states(status: str) -> None:
    payload = _payload()
    ledger = FakeLedger()

    async def submit(**_: Any) -> dict[str, Any]:
        return {
            "submitted": True,
            "client_order_id": payload.client_order_id,
            "order": {"id": "paper-order-bad", "status": "accepted"},
        }

    async def get_order(_: str) -> dict[str, Any]:
        return {"order": {"id": "paper-order-bad", "status": status}}

    async def fills(**_: Any) -> dict[str, Any]:
        return {"fills": []}

    async def positions() -> dict[str, Any]:
        return {"positions": []}

    result = await smoke.execute_and_reconcile(
        payload,
        ledger=ledger,  # type: ignore[arg-type]
        submit_fn=submit,
        get_order_fn=get_order,
        list_fills_fn=fills,
        list_positions_fn=positions,
        sleep_fn=_noop_sleep,
    )

    assert result.reconcile_status == "unexpected_state"


def test_report_omits_raw_account_identifier_and_states_boundaries() -> None:
    payload = _payload()
    preflight = smoke.PreflightSnapshot("active", Decimal("100"), 0, 0, 0, 0, 0)
    report = smoke.build_report(payload=payload, preflight=preflight)

    assert "raw-account-id" not in report
    assert "account_id" not in report
    assert "secrets=not printed" in report
    assert "sell_close=out_of_scope" in report
    assert "no live/generic/KIS/Upbit" in report
