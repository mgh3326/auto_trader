"""Minimal, hand-built ``policy_table.v1`` payloads for the B0-X tests.

Built through the real ``scripts.policy_table.core.schema`` hashing helpers so
a fixture table passes the same integrity check a generated one does — a
fixture that skipped the hash would test a door B0-X does not actually use.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from scripts.policy_table.core.schema import (
    canonical_json_bytes,
    compute_policy_table_hash,
)


def make_row(
    *,
    symbol: str,
    previous_close: str,
    buy_l1: str | None,
    buy_l2: str | None = None,
    sell_r1: str | None = None,
    sell_r2: str | None = None,
    insufficient_history: bool = False,
) -> dict[str, Any]:
    if insufficient_history:
        return {
            "symbol": symbol,
            "held": False,
            "insufficient_history": True,
            "bars_available": 3,
            "bars_required": 120,
            "note": "not enough bars",
        }
    return {
        "symbol": symbol,
        "held": False,
        "insufficient_history": False,
        "bars_used": 200,
        "previous_close": previous_close,
        "rsi": "50.0",
        "A_buy_side": {
            "buy_l1": (
                {"price": buy_l1, "basis": "t_minus_1_close_x_0.97_tick_aligned"}
                if buy_l1
                else None
            ),
            "buy_l2": (
                {"price": buy_l2, "basis": "support_cluster"} if buy_l2 else None
            ),
            "sizing_band": {"new_entry_notional_krw": "10000"},
        },
        "B_sell_side": {
            "label": "SELL_SIDE_MODEL_MISMATCH",
            "sell_r1": sell_r1,
            "sell_r2": sell_r2,
            "loss_guard_floor": None,
        },
        "C_diagnostics": {},
        "D_context": {"alt_breadth_rank": 1},
    }


def make_payload(
    *,
    rows: list[dict[str, Any]],
    generated_at: dt.datetime,
    market: str = "crypto",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "policy_table.v1",
        "market": market,
        "generated_at": generated_at.isoformat(),
        "trust_labels": ["a", "b", "c"],
        "config": {
            "quote_currency": "KRW",
            "candle_period": "4h",
            "averaging_k_levels": ["0.05", "0.10"],
            "loss_guard_multiplier": "1.01",
        },
        "universe": {
            "holdings": [],
            "watch": [],
            "top_n": [row["symbol"] for row in rows],
            "total_symbols": len(rows),
            "symbols_with_insufficient_history": [],
        },
        "market_context": {},
        "rows": rows,
        "sizing": {
            "new_entry_notional_krw": "10000",
            "orderable_krw": "1000000",
            "min_krw_balance_floor": "0",
            "headroom_krw": "1000000",
        },
    }
    payload["stamps"] = {
        "policy_table_hash": compute_policy_table_hash(payload),
        "auto_trader_head": "0" * 40,
        "indicator_code_commit": "0" * 40,
        "engine_module_sha256": {},
        "input_as_of": payload["generated_at"],
    }
    return payload


def write_table(
    directory: Path, payload: dict[str, Any], *, market: str = "crypto"
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamped = directory / f"20260808T000000Z-{market}.json"
    stamped.write_bytes(canonical_json_bytes(payload))
    latest = directory / f"latest-{market}.json"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(stamped.name)
    return latest


def write_stale_marker(directory: Path, *, market: str = "crypto") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / f"latest-{market}.STALE"
    marker.write_text(json.dumps({"error": "builder failed"}) + "\n")
    return marker
