"""Argumentless read-only smoke check for Alpaca paper MCP tools (ROB-71).

Calls every tool in ALPACA_PAPER_READONLY_TOOL_NAMES once, prints counts/status
only (never raw payloads), and exits non-zero if any call fails.
"""

from __future__ import annotations

import asyncio
import sys

from app.mcp_server.tooling.alpaca_paper import (
    ALPACA_PAPER_READONLY_TOOL_NAMES,
    alpaca_paper_get_account,
    alpaca_paper_get_cash,
    alpaca_paper_get_order,
    alpaca_paper_list_assets,
    alpaca_paper_list_fills,
    alpaca_paper_list_orders,
    alpaca_paper_list_positions,
)
from app.mcp_server.tooling.alpaca_paper_ledger_read import (
    alpaca_paper_ledger_get,
    alpaca_paper_ledger_list_recent,
)


async def run_smoke() -> int:
    """Run all read-only Alpaca paper checks; return 0 if all OK, 1 otherwise."""
    results: list[tuple[str, bool, str]] = []

    async def _probe(name: str, coro, note_fn) -> None:  # type: ignore[type-arg]
        try:
            payload = await coro
            results.append((name, True, note_fn(payload)))
        except Exception as exc:  # noqa: BLE001
            results.append((name, False, f"ERROR: {type(exc).__name__}: {exc}"))

    await _probe(
        "alpaca_paper_get_account",
        alpaca_paper_get_account(),
        lambda p: f"status={p['account'].get('status', '?')}",
    )

    await _probe(
        "alpaca_paper_get_cash",
        alpaca_paper_get_cash(),
        lambda p: f"cash_set={p['cash'].get('cash') is not None}",
    )

    await _probe(
        "alpaca_paper_list_positions",
        alpaca_paper_list_positions(),
        lambda p: f"count={p['count']}",
    )

    # list_orders also seeds the order_id for the get_order probe below
    orders_payload: dict | None = None  # type: ignore[type-arg]
    try:
        orders_payload = await alpaca_paper_list_orders(status="open", limit=1)
        results.append(
            ("alpaca_paper_list_orders", True, f"count={orders_payload['count']}")
        )
    except Exception as exc:  # noqa: BLE001
        results.append(
            (
                "alpaca_paper_list_orders",
                False,
                f"ERROR: {type(exc).__name__}: {exc}",
            )
        )

    # get_order: derive id from list_orders; skip as OK when no orders exist
    order_id: str | None = None
    if orders_payload and orders_payload.get("orders"):
        order_id = orders_payload["orders"][0].get("id")

    if order_id:
        await _probe(
            "alpaca_paper_get_order",
            alpaca_paper_get_order(order_id),
            lambda p: f"status={p['order'].get('status', '?')}",
        )
    else:
        results.append(
            ("alpaca_paper_get_order", True, "skipped: no orders to inspect")
        )

    await _probe(
        "alpaca_paper_list_assets",
        alpaca_paper_list_assets(status="active", asset_class="us_equity"),
        lambda p: f"count={p['count']}",
    )

    await _probe(
        "alpaca_paper_list_fills",
        alpaca_paper_list_fills(limit=5),
        lambda p: f"count={p['count']}",
    )

    # Ledger read tools: list recent rows, then inspect one client_order_id when present.
    ledger_payload: dict | None = None  # type: ignore[type-arg]
    try:
        ledger_payload = await alpaca_paper_ledger_list_recent(limit=1)
        results.append(
            (
                "alpaca_paper_ledger_list_recent",
                True,
                f"count={ledger_payload['count']}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        results.append(
            (
                "alpaca_paper_ledger_list_recent",
                False,
                f"ERROR: {type(exc).__name__}: {exc}",
            )
        )

    client_order_id: str | None = None
    if ledger_payload and ledger_payload.get("items"):
        client_order_id = ledger_payload["items"][0].get("client_order_id")

    if not client_order_id:
        client_order_id = "alpaca-paper-smoke-missing-client-order-id"

    await _probe(
        "alpaca_paper_ledger_get",
        alpaca_paper_ledger_get(client_order_id),
        lambda p: f"found={p.get('found', False)}",
    )

    # Confirm every expected tool was exercised
    expected_count = len(ALPACA_PAPER_READONLY_TOOL_NAMES)
    exercised = {name for name, _, _ in results}
    missing = ALPACA_PAPER_READONLY_TOOL_NAMES - exercised
    if missing:
        results.append(("_inventory", False, f"missing tools: {sorted(missing)}"))

    all_ok = True
    ok_tool_count = 0
    for name, ok, note in results:
        tag = "OK" if ok else "FAIL"
        print(f"  [{tag}] {name}: {note}")
        if name in ALPACA_PAPER_READONLY_TOOL_NAMES and ok:
            ok_tool_count += 1
        if not ok:
            all_ok = False

    classification = "PASS" if all_ok and ok_tool_count == expected_count else "PARTIAL"
    print(f"summary: {classification} tools_ok={ok_tool_count}/{expected_count}")
    return 0 if classification == "PASS" else 1


def main() -> None:
    sys.exit(asyncio.run(run_smoke()))


if __name__ == "__main__":
    main()
