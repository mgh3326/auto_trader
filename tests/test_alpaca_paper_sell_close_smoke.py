"""Tests for ROB-86 guarded Alpaca Paper sell/reduce/close smoke."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from scripts.smoke import alpaca_paper_sell_close_smoke as smoke


@dataclass
class FakeSourceRow:
    client_order_id: str = "source-buy-1"
    execution_symbol: str = "BTC/USD"
    execution_venue: str | None = "alpaca_paper_crypto"
    side: str = "buy"
    lifecycle_state: str = "filled"
    reconcile_status: str = "filled_position_matched"
    filled_qty: Decimal = Decimal("0.001")
    requested_qty: Decimal = Decimal("0.001")
    signal_symbol: str | None = "KRW-BTC"
    signal_venue: str | None = "upbit"
    execution_asset_class: str | None = "crypto"


@dataclass
class FakeLedgerRow:
    client_order_id: str = ""
    reconcile_status: str | None = None


class FakeLedger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.row = FakeLedgerRow()

    async def record_preview(self, **kwargs: Any) -> FakeLedgerRow:
        self.calls.append(("record_preview", kwargs))
        self.row.client_order_id = kwargs["client_order_id"]
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


async def _source_lookup(_: str) -> list[FakeSourceRow]:
    return [FakeSourceRow()]


def _preflight(**overrides: Any) -> smoke.SellClosePreflightSnapshot:
    params = {
        "account_status": "active",
        "open_order_count": 0,
        "conflicting_open_sell_order_count": 0,
        "matching_position_count": 1,
        "matching_position_qty": Decimal("0.001"),
        "recent_fill_count": 0,
        "close_intent": "reduce",
    }
    params.update(overrides)
    return smoke.SellClosePreflightSnapshot(**params)


@pytest.mark.asyncio
async def test_build_sell_close_payload_requires_exact_source_buy_and_bounded_sell() -> None:
    payload = await smoke.build_sell_close_payload(
        source_client_order_id="source-buy-1",
        symbol="BTC/USD",
        qty=Decimal("0.0005"),
        limit_price_usd=Decimal("50000"),
        client_order_id="rob86-sell-test",
        source_lookup_fn=_source_lookup,
        now=datetime(2026, 5, 3, tzinfo=UTC),
    )

    assert payload.execution_symbol == "BTC/USD"
    assert payload.execution_venue == "alpaca_paper_crypto"
    assert payload.close_intent == "reduce"
    assert payload.order_request == {
        "symbol": "BTC/USD",
        "side": "sell",
        "type": "limit",
        "qty": Decimal("0.0005"),
        "time_in_force": "gtc",
        "limit_price": Decimal("50000"),
        "client_order_id": "rob86-sell-test",
        "asset_class": "crypto",
    }


@pytest.mark.asyncio
async def test_build_sell_close_payload_fails_closed_on_source_and_size_mismatch() -> None:
    async def bad_source(_: str) -> list[FakeSourceRow]:
        return [FakeSourceRow(side="sell")]

    with pytest.raises(smoke.SellCloseStopError, match="source ledger row must be a buy"):
        await smoke.build_sell_close_payload(
            source_client_order_id="source-buy-1",
            symbol="BTC/USD",
            qty=Decimal("0.0005"),
            limit_price_usd=Decimal("50000"),
            source_lookup_fn=bad_source,
        )

    with pytest.raises(smoke.SellCloseStopError, match="estimated notional"):
        await smoke.build_sell_close_payload(
            source_client_order_id="source-buy-1",
            symbol="BTC/USD",
            qty=Decimal("0.002"),
            limit_price_usd=Decimal("50000"),
            source_lookup_fn=_source_lookup,
        )


@pytest.mark.parametrize(
    "snapshot",
    [
        _preflight(account_status="inactive"),
        _preflight(conflicting_open_sell_order_count=1),
        _preflight(matching_position_count=0),
        _preflight(matching_position_qty=Decimal("0.0001")),
    ],
)
def test_validate_sell_close_preflight_fails_closed(
    snapshot: smoke.SellClosePreflightSnapshot,
) -> None:
    source = FakeSourceRow()
    payload = smoke.SellClosePayload(
        source_client_order_id=source.client_order_id,
        signal_symbol=source.signal_symbol,
        signal_venue=source.signal_venue,
        execution_symbol=source.execution_symbol,
        execution_venue="alpaca_paper_crypto",
        asset_class="crypto",
        qty=Decimal("0.0005"),
        limit_price_usd=Decimal("50000"),
        time_in_force="gtc",
        client_order_id="rob86-sell-test",
        order_request={},
        source=smoke.SourceLedgerSnapshot(
            client_order_id=source.client_order_id,
            execution_symbol=source.execution_symbol,
            execution_venue=source.execution_venue,
            side=source.side,
            lifecycle_state=source.lifecycle_state,
            reconcile_status=source.reconcile_status,
            qty=source.filled_qty,
        ),
        close_intent="reduce",
        provenance=smoke.ApprovalProvenance(
            signal_symbol=source.signal_symbol,
            signal_venue=source.signal_venue,
            execution_asset_class="crypto",
            workflow_stage="rob86_guarded_sell_close",
            purpose="paper_sell_close_smoke",
        ),
    )

    with pytest.raises(smoke.SellCloseStopError):
        smoke.validate_sell_close_preflight(snapshot, payload)


@pytest.mark.asyncio
async def test_validate_preview_and_confirm_false_never_submits() -> None:
    payload = await smoke.build_sell_close_payload(
        source_client_order_id="source-buy-1",
        symbol="BTC/USD",
        qty=Decimal("0.0005"),
        limit_price_usd=Decimal("50000"),
        client_order_id="rob86-sell-test",
        source_lookup_fn=_source_lookup,
    )
    ledger = FakeLedger()
    calls: list[tuple[str, dict[str, Any]]] = []

    async def preview(**kwargs: Any) -> dict[str, Any]:
        calls.append(("preview", kwargs))
        return {"success": True, "preview": True, "submitted": False}

    async def submit(**kwargs: Any) -> dict[str, Any]:
        calls.append(("submit", kwargs))
        return {
            "success": True,
            "submitted": False,
            "blocked_reason": "confirmation_required",
            "client_order_id": kwargs["client_order_id"],
        }

    result = await smoke.validate_sell_close_preview_and_confirm_false(
        payload,
        preview_fn=preview,
        submit_fn=submit,
        ledger=ledger,  # type: ignore[arg-type]
    )

    assert result.confirm_false["submitted"] is False
    assert calls == [
        ("preview", payload.order_request),
        ("submit", payload.order_request | {"confirm": False}),
    ]
    assert ledger.calls[0][0] == "record_preview"
    assert ledger.calls[0][1]["side"] == "sell"


@pytest.mark.asyncio
async def test_execute_sell_close_submits_exactly_one_order_and_reconciles() -> None:
    payload = await smoke.build_sell_close_payload(
        source_client_order_id="source-buy-1",
        symbol="BTC/USD",
        qty=Decimal("0.0005"),
        limit_price_usd=Decimal("50000"),
        client_order_id="rob86-sell-test",
        source_lookup_fn=_source_lookup,
    )
    ledger = FakeLedger()
    submit_calls: list[dict[str, Any]] = []

    async def submit(**kwargs: Any) -> dict[str, Any]:
        submit_calls.append(kwargs)
        return {
            "submitted": True,
            "client_order_id": kwargs["client_order_id"],
            "order": {
                "id": "paper-sell-1",
                "client_order_id": kwargs["client_order_id"],
                "symbol": kwargs["symbol"],
                "side": "sell",
                "type": "limit",
                "status": "accepted",
            },
        }

    async def get_order(_: str) -> dict[str, Any]:
        return {
            "order": {
                "id": "paper-sell-1",
                "client_order_id": payload.client_order_id,
                "symbol": payload.execution_symbol,
                "status": "filled",
            }
        }

    async def fills(**_: Any) -> dict[str, Any]:
        return {"fills": [{"order_id": "paper-sell-1"}]}

    async def positions() -> dict[str, Any]:
        return {"positions": [{"symbol": "BTC/USD", "qty": "0.0005"}]}

    result = await smoke.execute_sell_close_and_reconcile(
        payload,
        ledger=ledger,  # type: ignore[arg-type]
        submit_fn=submit,
        get_order_fn=get_order,
        list_fills_fn=fills,
        list_positions_fn=positions,
        sleep_fn=lambda _: smoke.asyncio.sleep(0),
    )

    assert len(submit_calls) == 1
    assert submit_calls[0] == payload.order_request | {"confirm": True}
    assert result.reconcile_status == "reduced_position_matched"
    assert [call[0] for call in ledger.calls] == [
        "record_submit",
        "record_status",
        "record_position_snapshot",
        "record_reconcile",
    ]
