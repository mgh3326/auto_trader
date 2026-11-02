"""Deterministic daily episode accounting; persistence and data collection live outside."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from research.strategy_evidence.decision_rules import (
    add_quantity,
    add_trigger,
    averaging_decision,
    next_open_day,
)

D = Decimal
HORIZON = {"crypto": 60, "us": 20}
RESERVE = {"crypto": D(3), "us": D("1.5")}
# Same conservative full round-trip cost per invested unit as the research replay.
# Imported lazily by the CLI from common.py; passed in so this engine is stdlib only.


@dataclass(frozen=True)
class Bar:
    day: date
    open: Decimal
    close: Decimal
    split: Decimal = D(0)


@dataclass(frozen=True)
class Signal:
    key: str
    symbol: str
    at: datetime
    unit: Decimal
    limit: Decimal | None = None


def entry_day(market: str, at: datetime, sessions: list[date]) -> date | None:
    """Research next_open_session convention, on the independent market calendar."""
    day = next_open_day(market, at)
    return next((s for s in sessions if s >= day), None)


def empty_arm() -> dict:
    return {"qty": "0", "invested": "0", "proceeds": "0", "fills": []}


def buy(arm: dict, day: date, price: Decimal, qty: Decimal, reason: str) -> dict:
    arm["qty"] = str(D(arm["qty"]) + qty)
    arm["invested"] = str(D(arm["invested"]) + price * qty)
    fill = {
        "day": str(day),
        "side": "buy",
        "price": str(price),
        "qty": str(qty),
        "reason": reason,
    }
    arm["fills"].append(fill)
    return fill


def sell(arm: dict, day: date, price: Decimal, reason: str) -> dict:
    qty = D(arm["qty"])
    arm["proceeds"] = str(D(arm["proceeds"]) + price * qty)
    arm["qty"] = "0"
    fill = {
        "day": str(day),
        "side": "sell",
        "price": str(price),
        "qty": str(qty),
        "reason": reason,
    }
    arm["fills"].append(fill)
    return fill


def pnl(arm: dict, mark: Decimal, cost: Decimal) -> Decimal:
    return D(arm["proceeds"]) + D(arm["qty"]) * mark - D(arm["invested"]) * (1 + cost)


def open_episode(
    market: str, signal: Signal, day: date, h_day: date, bar: Bar | None
) -> dict:
    episode = {
        "id": signal.key,
        "symbol": signal.symbol,
        "signal_at": signal.at.isoformat(),
        "entry_day": str(day),
        "h_day": str(h_day),
        "u": str(signal.unit),
        "E": str(signal.unit * RESERVE[market]),
        "entry_price": None,
        "unit_prices": [],
        "candidate": empty_arm(),
        "control": empty_arm(),
        "control_limit": str(signal.limit) if signal.limit is not None else None,
        "control_state": "unverified"
        if market == "us" and signal.limit is None
        else "known",
        "unverified": [],
        "h_result": None,
    }
    if bar is None:
        episode["unverified"].append(f"missing_entry_bar:{day}")
        return episode
    episode["entry_price"] = str(bar.open)
    episode["unit_prices"] = [str(bar.open)]
    buy(episode["candidate"], day, bar.open, signal.unit / bar.open, "entry")
    if market == "crypto":
        buy(episode["control"], day, bar.open, signal.unit / bar.open, "buy-and-hold")
    elif signal.limit is not None:
        # Frozen DAY limit, conservative open/close-only model. No intraday touch claim.
        price = (
            bar.open
            if bar.open <= signal.limit
            else signal.limit
            if bar.close <= signal.limit
            else None
        )
        if price is not None:
            buy(episode["control"], day, price, signal.unit / price, "deep-limit-day")
    return episode


def step_episode(
    market: str, episode: dict, bar: Bar | None, day: date, cost: Decimal
) -> list[dict]:
    actions = []
    candidate, control = episode["candidate"], episode["control"]
    active = D(candidate["qty"]) > 0 or day <= date.fromisoformat(episode["h_day"])
    if not active:
        return actions
    if bar is None:
        episode["unverified"].append(f"missing_bar:{day}")
        return actions
    if episode["unverified"]:
        return actions  # never guess the adds/exits that might have occurred in a gap
    if market == "us" and bar.split and str(day) != episode["entry_day"]:
        for arm in (candidate, control):
            arm["qty"] = str(D(arm["qty"]) * bar.split)
        episode["entry_price"] = str(D(episode["entry_price"]) / bar.split)
        actions.append(
            {
                "episode": episode["id"],
                "arm": "both",
                "day": str(day),
                "reason": "split",
                "ratio": str(bar.split),
            }
        )
    if D(candidate["qty"]) > 0:
        if market == "crypto":
            decision = averaging_decision(
                float(episode["entry_price"]),
                [float(p) for p in episode["unit_prices"]],
                float(bar.close),
            )
            for _ in range(decision.adds):
                episode["unit_prices"].append(str(bar.close))
                fill = buy(
                    candidate, day, bar.close, D(episode["u"]) / bar.close, "add"
                )
                actions.append({"episode": episode["id"], "arm": "candidate", **fill})
            if decision.exit_reason:
                fill = sell(candidate, day, bar.close, decision.exit_reason)
                actions.append({"episode": episode["id"], "arm": "candidate", **fill})
        elif len(candidate["fills"]) == 1 and add_trigger(
            float(bar.close / D(episode["entry_price"]) - 1)
        ):
            fill = buy(
                candidate, day, bar.close, add_quantity(D(candidate["qty"])), "add"
            )
            actions.append({"episode": episode["id"], "arm": "candidate", **fill})
    if str(day) == episode["h_day"]:
        cp = pnl(candidate, bar.close, cost)
        bp = (
            pnl(control, bar.close, cost)
            if episode["control_state"] == "known"
            else None
        )
        episode["h_result"] = {
            "state": "verified" if bp is not None else "unverified",
            "candidate_pnl": str(cp),
            "control_pnl": str(bp) if bp is not None else None,
            "D_i": str((cp - bp) / D(episode["E"])) if bp is not None else None,
        }
        if D(control["qty"]) > 0:
            fill = sell(control, day, bar.close, "H")
            actions.append({"episode": episode["id"], "arm": "control", **fill})
    # Costs are reported as a conservative reserve, not debited from E a second time.
    if D(candidate["invested"]) > D(episode["E"]) + D("1e-20"):
        raise ValueError("episode exceeded fixed E")
    return actions


def advance(
    market: str,
    asof: date,
    previous: dict | None,
    sessions: list[date],
    bars: dict[tuple[str, date], Bar],
    signals: list[Signal],
    cost: Decimal,
    input_state: str = "verified",
) -> dict:
    """Advance only new calendar sessions. D0+H never shifts for missing symbol bars."""
    episodes = deepcopy(previous["episodes"]) if previous else []
    seen = set(previous["seen"]) if previous else set()
    start = (
        date.fromisoformat(previous["asof"]) + timedelta(days=1) if previous else asof
    )
    actions, today_signals = [], []
    for day in (s for s in sessions if start <= s <= asof):
        for signal in sorted(signals, key=lambda s: (s.at, s.key)):
            if signal.key in seen or entry_day(market, signal.at, sessions) != day:
                continue
            seen.add(signal.key)
            held = next(
                (
                    e
                    for e in episodes
                    if e["symbol"] == signal.symbol
                    and (
                        D(e["candidate"]["qty"]) > 0
                        or (e["unverified"] and not e["candidate"]["fills"])
                    )
                ),
                None,
            )
            today_signals.append(
                {
                    "id": signal.key,
                    "symbol": signal.symbol,
                    "day": str(day),
                    "state": "joined_held_episode" if held else "new_episode",
                }
            )
            if held:
                continue
            i = sessions.index(day)
            h_day = sessions[i + HORIZON[market]]
            episode = open_episode(
                market, signal, day, h_day, bars.get((signal.symbol, day))
            )
            episodes.append(episode)
            for arm in ("candidate", "control"):
                actions.extend(
                    {"episode": signal.key, "arm": arm, **f}
                    for f in episode[arm]["fills"]
                )
        for episode in episodes:
            if episode["entry_day"] <= str(day):
                actions.extend(
                    step_episode(
                        market, episode, bars.get((episode["symbol"], day)), day, cost
                    )
                )
    rows = []
    for episode in episodes:
        bar = bars.get((episode["symbol"], asof))
        candidate = episode["candidate"]
        known = not episode["unverified"] and (
            bar is not None or D(candidate["qty"]) == 0
        )
        mark = bar.close if bar else D(0)
        basis = D(candidate["invested"]) if D(candidate["qty"]) else D(0)
        result = episode["h_result"]
        rows.append(
            {
                "id": episode["id"],
                "symbol": episode["symbol"],
                "E": episode["E"],
                "u": episode["u"],
                "state": "unverified"
                if not known
                else "open"
                if D(candidate["qty"])
                else "closed",
                "quantity": candidate["qty"],
                "cash": str(
                    D(episode["E"])
                    - D(candidate["invested"])
                    + D(candidate["proceeds"])
                ),
                "cost_reserve": str(D(candidate["invested"]) * cost),
                "control_cash": str(
                    D(episode["E"])
                    - D(episode["control"]["invested"])
                    + D(episode["control"]["proceeds"])
                ),
                "open_cost_over_E": str(basis / D(episode["E"])) if known else None,
                "open_value_over_E": str(D(candidate["qty"]) * mark / D(episode["E"]))
                if known
                else None,
                "pnl": str(pnl(candidate, mark, cost)) if known else None,
                "h_day": episode["h_day"],
                "h_result": result,
                "comparison_state": result["state"]
                if result
                else "unverified"
                if episode["unverified"]
                or episode["control_state"] != "known"
                or str(asof) >= episode["h_day"]
                else "pending_H",
                "unverified": episode["unverified"],
            }
        )
    return {
        "schema": "paper-forward/v1",
        "market": market,
        "asof": str(asof),
        "input_state": input_state,
        "signals": today_signals,
        "ledger_diff": actions,
        "summary": {
            "opened": sum(a.get("reason") == "entry" for a in actions),
            "added": len({a["episode"] for a in actions if a.get("reason") == "add"}),
            "closed": sum(
                a.get("arm") == "candidate" and a.get("side") == "sell" for a in actions
            ),
        },
        "episode_report": rows,
        "episodes": episodes,
        "seen": sorted(seen),
    }
