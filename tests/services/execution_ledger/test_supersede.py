"""ROB-1379 — pure order-identity, supersede and terminal-cover rules (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.execution_ledger import supersede as s

pytestmark = pytest.mark.unit

KR_ORDER_TIME = datetime(2099, 11, 12, 0, 30, tzinfo=UTC)  # 09:30 KST
KR_FILL_TIME = datetime(2099, 11, 12, 0, 30, 1, tzinfo=UTC)


def kr_daily_raw(**overrides: Any) -> dict[str, Any]:
    raw = {
        "ord_dt": "20991112",
        "odno": "0012345678",
        "pdno": "934220",
        "ord_qty": "10",
        "tot_ccld_qty": "10",
        "rmn_qty": "0",
        "rjct_qty": "0",
        "cncl_yn": "",
    }
    raw.update(overrides)
    return raw


def row(**overrides: Any) -> SimpleNamespace:
    data: dict[str, Any] = {
        "id": 1,
        "broker": "kis",
        "account_mode": "live",
        "venue": "krx",
        "instrument_type": "equity_kr",
        "symbol": "934220",
        "side": "buy",
        "broker_order_id": "0012345678",
        "currency": "KRW",
        "filled_at": KR_ORDER_TIME,
        "filled_qty": Decimal("10"),
        "source": "reconciler",
        "raw_payload_json": kr_daily_raw(),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def ws(**overrides: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "id": 2,
        "source": "websocket",
        "filled_at": KR_FILL_TIME,
        "broker_order_id": "12345678",  # leading zeros differ by writer
        "filled_qty": Decimal("4"),
        "raw_payload_json": {"tr": "H0STCNI0", "fields": []},
    }
    defaults.update(overrides)
    return row(**defaults)


# ------------------------------------------------------------------ identity


def test_identity_ignores_fill_seq_venue_time_and_leading_zeros() -> None:
    assert s.order_identity(row()) == s.order_identity(ws(venue="nxt"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("broker", "upbit"),
        ("account_mode", "mock"),
        ("instrument_type", "equity_us"),
        ("symbol", "934221"),
        ("side", "sell"),
        ("currency", "USD"),
        ("broker_order_id", "0012345679"),
        # KIS reuses order numbers across days.
        ("filled_at", datetime(2099, 11, 13, 0, 30, tzinfo=UTC)),
    ],
)
def test_identity_separates_every_order_field(field: str, value: Any) -> None:
    assert s.order_identity(row()) != s.order_identity(ws(**{field: value}))


def test_us_identity_normalizes_symbol_and_uses_the_us_trading_date() -> None:
    reconciler = row(
        instrument_type="equity_us",
        symbol="BRK/B",
        currency="USD",
        venue="NASD",
        filled_at=datetime(2099, 11, 12, 14, 30, tzinfo=UTC),  # 09:30 ET
    )
    # Same US trading date even though the KST date already rolled over.
    websocket = ws(
        instrument_type="equity_us",
        symbol="BRK.B",
        currency="USD",
        venue="krx",
        filled_at=datetime(2099, 11, 12, 20, 59, tzinfo=UTC),  # 15:59 ET
    )
    assert s.order_identity(reconciler) == s.order_identity(websocket)
    after_rollover = ws(
        instrument_type="equity_us",
        symbol="BRK.B",
        currency="USD",
        filled_at=datetime(2099, 11, 13, 1, 30, tzinfo=UTC),  # 20:30 ET
    )
    assert s.order_identity(reconciler) != s.order_identity(after_rollover)


def test_upbit_identity_carries_no_day() -> None:
    a = row(broker="upbit", instrument_type="crypto", symbol="BTC", broker_order_id="u")
    b = ws(
        broker="upbit",
        instrument_type="crypto",
        symbol="BTC",
        broker_order_id="u",
        filled_at=datetime(2099, 11, 20, tzinfo=UTC),
    )
    assert s.order_identity(a) == s.order_identity(b)


# ------------------------------------------------------------------ read side


def test_supersede_drops_only_covered_websocket_rows() -> None:
    covered = ws(id=2)
    other_order = ws(id=3, broker_order_id="0099999999")
    kept, superseded = s.supersede_provisional([row(), covered, other_order])
    assert [r.id for r in kept] == [1, 3]
    assert superseded == 1


def test_supersede_never_drops_authoritative_rows() -> None:
    manual = row(id=4, source="manual_import")
    kept, superseded = s.supersede_provisional([row(), manual])
    assert [r.id for r in kept] == [1, 4]
    assert superseded == 0


def test_supersede_uses_the_wider_authority_set() -> None:
    kept, superseded = s.supersede_provisional([ws()], authority=[row(), ws()])
    assert (kept, superseded) == ([], 1)


# ------------------------------------------------------------------ terminal


def test_kr_terminal_quantity() -> None:
    assert s.terminal_final_quantity(row()) == Decimal("10")


#: (reason, row overrides) — each row is refused by exactly one guard.
NOT_TERMINAL: list[tuple[str, dict[str, Any]]] = [
    ("websocket_row", {"source": "websocket"}),
    ("manual_import_row", {"source": "manual_import"}),
    ("no_raw_payload", {"raw_payload_json": None}),
    ("non_positive_booked", {"filled_qty": Decimal("0")}),
    ("still_live", {"raw_payload_json": kr_daily_raw(rmn_qty="3")}),
    ("remaining_missing", {"raw_payload_json": kr_daily_raw(rmn_qty=None)}),
    ("remaining_unreadable", {"raw_payload_json": kr_daily_raw(rmn_qty="x")}),
    ("remaining_bool", {"raw_payload_json": kr_daily_raw(rmn_qty=False)}),
    ("total_missing", {"raw_payload_json": kr_daily_raw(tot_ccld_qty=None)}),
    # A per-fill row is not the order aggregate.
    ("total_not_booked", {"raw_payload_json": kr_daily_raw(tot_ccld_qty="12")}),
    ("unsupported_broker", {"broker": "toss"}),
    ("unsupported_instrument", {"instrument_type": "crypto"}),
]


@pytest.mark.parametrize(
    "reason,overrides", NOT_TERMINAL, ids=[r for r, _ in NOT_TERMINAL]
)
def test_not_proven_terminal(reason: str, overrides: dict[str, Any]) -> None:
    assert s.terminal_final_quantity(row(**overrides)) is None


def test_us_terminal_quantity_uses_overseas_fields() -> None:
    us = row(
        instrument_type="equity_us",
        filled_qty=Decimal("2"),
        raw_payload_json={"nccs_qty": "0", "ft_ccld_qty": "2", "ft_ord_qty": "2"},
    )
    assert s.terminal_final_quantity(us) == Decimal("2")
    live = row(
        instrument_type="equity_us",
        filled_qty=Decimal("2"),
        raw_payload_json={"nccs_qty": "1", "ft_ccld_qty": "2"},
    )
    assert s.terminal_final_quantity(live) is None


@pytest.mark.parametrize(
    "state,expected",
    [("done", Decimal("0.0003")), ("cancel", Decimal("0.0003")), ("wait", None)],
)
def test_upbit_terminal_quantity(state: str, expected: Decimal | None) -> None:
    upbit = row(
        broker="upbit",
        instrument_type="crypto",
        filled_qty=Decimal("0.0001"),  # one trade of the order
        raw_payload_json={"state": state, "executed_volume": "0.0003"},
    )
    assert s.terminal_final_quantity(upbit) == expected


def test_terminal_cover_requires_identity_terminal_and_containment() -> None:
    reconciler = row()
    assert s.terminal_cover(ws(), [reconciler]) is reconciler
    assert s.terminal_cover(ws(filled_qty=Decimal("11")), [reconciler]) is None
    assert s.terminal_cover(ws(side="sell"), [reconciler]) is None
    live = row(raw_payload_json=kr_daily_raw(rmn_qty="1"))
    assert s.terminal_cover(ws(), [live]) is None
    # Only a websocket fill is ever covered.
    assert s.terminal_cover(row(source="reconciler", id=9), [reconciler]) is None


def _upbit_trade_row(row_id: int, qty: str) -> SimpleNamespace:
    """One reconciler row per Upbit trade; each carries the whole order detail."""
    return row(
        id=row_id,
        broker="upbit",
        instrument_type="crypto",
        symbol="BTC",
        broker_order_id="9d2c0f9e-order",
        filled_qty=Decimal(qty),
        raw_payload_json={"state": "done", "executed_volume": "3"},
    )


def _upbit_ws(qty: str) -> SimpleNamespace:
    return ws(
        broker="upbit",
        instrument_type="crypto",
        symbol="BTC",
        broker_order_id="9d2c0f9e-order",
        filled_qty=Decimal(qty),
    )


def test_terminal_is_not_complete_a_missing_trade_is_not_covered() -> None:
    """r1 B1: the order is done (3) but only the trade of 1 is on a row."""
    assert s.terminal_cover(_upbit_ws("2"), [_upbit_trade_row(1, "1")]) is None


def test_complete_trade_set_covers_and_answers_the_lowest_id() -> None:
    rows = [_upbit_trade_row(7, "2"), _upbit_trade_row(5, "1")]
    assert s.terminal_cover(_upbit_ws("2"), rows) is rows[1]


def test_over_booked_order_is_not_proof() -> None:
    """An aggregate row stored twice (drifting fill_seq) sums past the total."""
    assert s.terminal_cover(ws(), [row(id=7), row(id=5)]) is None


def test_manual_import_rows_neither_witness_nor_count() -> None:
    manual = row(id=3, source="manual_import", raw_payload_json=None)
    assert s.terminal_cover(ws(), [row(), manual]) is not None
    assert s.terminal_cover(ws(), [manual]) is None


@pytest.mark.parametrize("raw", ["0000000000", "000", "0"])
def test_all_zero_order_ids_share_one_identity(raw: str) -> None:
    assert s.normalize_order_id(raw) == "0"
    assert s.normalize_order_id("") == ""
