"""The one real underwater lot: Upbit KRW-XRP.

The lot facts below are a *literal input*, transcribed from a read-only
holdings snapshot taken on 2026-08-21 outside this package.  Keeping them as
constants is what lets the study stay offline — nothing here calls a broker.

    quantity        2758.02569914
    avg_buy_price   2,118.736 KRW
    current_price   1,738 KRW
    profit_rate     -17.97%

The average cost therefore sits +21.90% above the market, between the
pre-registered +20% and +30% grid points.

🔴 Scope limit: crypto-corpus-v1's exploration tree ends 2024-12-31 and
2025-01-01 onward is a sealed holdout.  The live 2026 lot cannot be replayed
against its own forward data.  What this case study answers is narrower and
stated as such: *given KRW-XRP's own historical spike events, what would the
three options have produced for a holder whose cost sat +21.90% above the
event price?*
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .report import Arm, load_observations, summarise
from .simulate import cost_basis_view, option_values
from .spec import BASES, HORIZONS

XRP_SYMBOL = "KRW-XRP"
XRP_QUANTITY = 2758.02569914
XRP_AVG_BUY_PRICE = 2118.73604672
XRP_SNAPSHOT_PRICE = 1738.0
XRP_SNAPSHOT_DATE = "2026-08-21"
XRP_COST_PREMIUM = XRP_AVG_BUY_PRICE / XRP_SNAPSHOT_PRICE - 1.0


def case_study(
    observations: list[dict[str, Any]],
    *,
    symbol: str = XRP_SYMBOL,
    cost_premium: float = XRP_COST_PREMIUM,
) -> dict[str, Any]:
    subset = [o for o in observations if o["symbol"] == symbol]
    events = [o for o in subset if o["kind"] == "event"]

    per_event: list[dict[str, Any]] = []
    for observation in events:
        row: dict[str, Any] = {
            "session": observation["session"],
            "ret_24h": observation["ret_24h"],
            "rsi": observation["rsi"],
            "resistance_count": observation["resistance_count"],
            "named_resistance_count": observation["named_resistance_count"],
            "rebuy_price_strong": observation["rebuy_price"],
            "rebuy_price_moderate_plus": observation["rebuy_price_moderate_plus"],
        }
        for horizon in HORIZONS:
            block = observation["forward"][f"event_close:{horizon}"]
            rebuy = observation["rebuy_price_moderate_plus"]
            if rebuy is not None and rebuy >= block["p0"]:
                rebuy = None
            values = option_values(
                p0=block["p0"],
                pt=block["exit_price"],
                rebuy_price=rebuy,
                window_low=block["window_low"],
            )
            view = cost_basis_view(values, p0=block["p0"], cost_premium=cost_premium)
            row[f"d{horizon}"] = {
                "p0": block["p0"],
                "exit_price": block["exit_price"],
                "hold_return": values.hold / block["p0"] - 1.0,
                "trim_minus_hold": (values.trim - values.hold) / block["p0"],
                "rebid_minus_hold": (
                    None
                    if values.rebid is None
                    else (values.rebid - values.hold) / block["p0"]
                ),
                "rebuy_filled": values.rebuy_filled,
                "hold_still_underwater": bool(view["hold_still_underwater"]),
                "trim_still_underwater": bool(view["trim_still_underwater"]),
            }
        per_event.append(row)

    aggregate = {
        f"{basis}:{horizon}": summarise(
            subset, Arm("named", "moderate_plus", basis, horizon, "event")
        )
        for basis in BASES
        for horizon in HORIZONS
    }
    return {
        "symbol": symbol,
        "lot": {
            "snapshot_date": XRP_SNAPSHOT_DATE,
            "quantity": XRP_QUANTITY,
            "avg_buy_price": XRP_AVG_BUY_PRICE,
            "snapshot_price": XRP_SNAPSHOT_PRICE,
            "cost_premium": cost_premium,
            "trim_10pct_quantity": XRP_QUANTITY * 0.10,
            "trim_10pct_proceeds_at_snapshot": XRP_QUANTITY * 0.10 * XRP_SNAPSHOT_PRICE,
            "realised_loss_on_that_trim": XRP_QUANTITY
            * 0.10
            * (XRP_SNAPSHOT_PRICE - XRP_AVG_BUY_PRICE),
        },
        "events": per_event,
        "aggregate": aggregate,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--symbol", default=XRP_SYMBOL)
    args = parser.parse_args(argv)

    payload = case_study(load_observations(args.observations), symbol=args.symbol)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps({"symbol": payload["symbol"], "events": len(payload["events"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
