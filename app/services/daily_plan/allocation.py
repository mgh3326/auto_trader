"""Plan-only comparison of candidate ranking inputs and budget allocation.

Pure: the stored input bundle, the candidate rows effective at one publication
and per (account_ref, currency) cash and parking facts go in; a
closed-vocabulary JSON block comes out. Money is exact ``Decimal`` and leaves
as decimal strings. Nothing here reads a database or a broker, and a recorded
parking sale is an assumption that nothing executes.

Every unproven value is ``unknown`` with a reason. It never becomes zero or
"no bad news".
"""

import re
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, Inexact, localcontext
from fractions import Fraction
from typing import Any

from app.core.symbol import to_db_symbol
from app.services.daily_plan.bundle import decimal_string
from app.services.daily_plan.vocabulary import (
    ALLOCATION_SCHEMA_VERSION,
    BUDGET_POLICIES,
    INPUT_UNKNOWN_REASONS,
    LIST_INPUTS,
    NEWS_DIRECTIONS,
    PARKING_PRECONDITION,
    RANKING_A_INPUTS,
    RANKING_B_ADDED_INPUTS,
)
from app.services.mechanical_loss_cut.rules import is_parking_instrument

CURRENCY = "KRW"
_RANKINGS = {
    "A": RANKING_A_INPUTS,
    "B": RANKING_A_INPUTS + RANKING_B_ADDED_INPUTS,
}
_LOWER_IS_BETTER = frozenset({"dist_to_support_pct", "rsi_14", "pbr"})
_BUY_ACTIONS = frozenset({"new", "add", "watch"})
_DECIMAL = re.compile(r"-?[0-9]{1,38}(?:\.[0-9]{1,38})?")
# Wide enough for quantity x price x rate of bounded inputs without rounding.
_MONEY_PRECISION = 400
_SCORE_STEP = Decimal("0.000000000001")
_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class _Window:
    cutoff_at: datetime
    stale_before: datetime


@dataclass(frozen=True, slots=True)
class _Parking:
    symbol: str
    units: Decimal
    price: Decimal
    sell_cost_bps: Decimal


@dataclass(slots=True)
class _Account:
    cash: Decimal | None = None
    cash_reason: str | None = "input_missing"
    parking: list[_Parking] = field(default_factory=list)
    unusable: list[dict[str, str | None]] = field(default_factory=list)


def build_comparison(
    *,
    bundle: Mapping[str, Any] | None,
    bundle_sha256: str | None,
    candidates: Mapping[str, Mapping[str, Any]],
    account_facts: Any,
    max_adopt: int,
    cutoff_at: datetime,
    stale_before: datetime,
    evaluated_at: datetime,
) -> dict[str, Any]:
    """Compare rankings A/B and budget policies W/N over the same candidates."""
    window = _Window(cutoff_at, stale_before)
    evidence = _candidate_evidence(bundle)
    with localcontext(prec=_MONEY_PRECISION) as context:
        context.traps[Inexact] = True
        accounts = _accounts(account_facts, window)
        views: dict[str, dict[str, Any]] = {}
        known: dict[str, dict[str, Decimal]] = {}
        intents: dict[str, tuple[str, Decimal]] = {}
        for symbol in sorted(candidates):
            row = candidates[symbol]
            facts = evidence.get(symbol, {})
            inputs, known[symbol] = _inputs(facts.get("ranking_inputs"), window)
            reason, account_ref, amount = _buy_intent(row, accounts, evaluated_at)
            if reason is None:
                intents[symbol] = (account_ref, amount)
            views[symbol] = {
                "symbol": symbol,
                "inputs": inputs,
                "ineligible_reason": reason,
                "account_ref": account_ref,
                "amount": None if amount is None else decimal_string(amount),
                "entry": _entry(row, facts, window),
            }

    budget = {}
    for ranking, names in _RANKINGS.items():
        ranked = _rank(known, names)
        for symbol, view in views.items():
            view[ranking] = ranked[symbol]
        order = sorted(
            intents,
            key=lambda symbol: (
                ranked[symbol]["rank"] is None,
                ranked[symbol]["rank"] or 0,
                symbol,
            ),
        )
        with localcontext(prec=_MONEY_PRECISION) as context:
            context.traps[Inexact] = True
            budget[ranking] = {
                policy: _allocate(
                    policy,
                    order=order,
                    ranked=ranked,
                    intents=intents,
                    views=views,
                    accounts=accounts,
                    max_adopt=max_adopt,
                )
                for policy in BUDGET_POLICIES
            }

    return {
        "schema_version": ALLOCATION_SCHEMA_VERSION,
        "mode": "plan_only",
        "input_bundle_sha256": bundle_sha256,
        "cutoff_at": cutoff_at.isoformat(),
        "stale_before": stale_before.isoformat(),
        "evaluated_at": evaluated_at.isoformat(),
        "caps": {"max_adopt": max_adopt},
        "tie_break": "symbol_ascending",
        "ranking_inputs": {name: list(items) for name, items in _RANKINGS.items()},
        "candidates": list(views.values()),
        "account_facts": [
            _account_view(key, account) for key, account in sorted(accounts.items())
        ],
        "budget": budget,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _decimal(value: Any) -> Decimal | None:
    if type(value) is not str or not _DECIMAL.fullmatch(value):
        return None
    return Decimal(value)


def _positive(value: Any) -> Decimal | None:
    number = _decimal(value)
    return number if number is not None and number > 0 else None


def _time(value: Any) -> datetime | None:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _time_reason(record: Mapping[str, Any], window: _Window) -> str | None:
    """Both times must be proven, at or before the cutoff and not stale."""
    event = _time(record.get("event_at"))
    observed = _time(record.get("first_observed_at"))
    if any(at is not None and at > window.cutoff_at for at in (event, observed)):
        return "observed_after_cutoff"
    if event is None or observed is None:
        return "observed_time_unproven"
    if event < window.stale_before:
        return "input_stale"
    return None


def _unavailable_reason(value: Any) -> str:
    if value == "price_stale":
        return "input_stale"
    return value if value in INPUT_UNKNOWN_REASONS else "input_missing"


def _candidate_evidence(
    bundle: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    return {
        row["symbol"]: row
        for row in _mapping(bundle).get("rows", [])
        if row.get("row_kind") == "candidate" and row.get("market") == "kr"
    }


def _unknown(reason: str) -> dict[str, Any]:
    return {"state": "unknown", "reason": reason, "value": None}


def _present(value: Decimal) -> dict[str, Any]:
    return {"state": "present", "reason": None, "value": decimal_string(value)}


def _inputs(
    raw: Any, window: _Window
) -> tuple[dict[str, dict[str, Any]], dict[str, Decimal]]:
    views = {}
    known = {}
    for name in _RANKINGS["B"]:
        record = _mapping(raw).get(name)
        view = (
            _items_input(record, window)
            if name in LIST_INPUTS
            else _scalar_input(name, record, window)
        )
        views[name] = view
        if view["state"] == "present":
            known[name] = Decimal(view["value"])
    return views, known


def _in_range(name: str, value: Decimal) -> bool:
    if name == "rsi_14":
        return 0 <= value <= 100
    if name == "daily_turnover":
        return value >= 0
    if name == "pbr":
        return value > 0
    return True


def _scalar_input(name: str, record: Any, window: _Window) -> dict[str, Any]:
    if record is None:
        return _unknown("input_missing")
    if not isinstance(record, Mapping):
        return _unknown("input_invalid")
    if "unavailable" in record:
        return _unknown(_unavailable_reason(record["unavailable"]))
    if record.get("value") is None:
        return _unknown("input_missing")
    reason = _time_reason(record, window)
    if reason:
        return _unknown(reason)
    value = _decimal(record["value"])
    if value is None or not _in_range(name, value):
        return _unknown("input_invalid")
    return _present(value)


def _items_input(record: Any, window: _Window) -> dict[str, Any]:
    """Net direction (-1, 0, 1) of the items provably known inside the window.

    Items after the cutoff or older than the window are left out and counted. An
    item whose time or direction is unknown could be the bad news, so it makes
    the whole input unknown.
    """
    excluded: Counter[str] = Counter()
    directions: list[str] = []

    def result(view: dict[str, Any]) -> dict[str, Any]:
        return {
            **view,
            "used_items": len(directions),
            "excluded": dict(sorted(excluded.items())),
        }

    if record is None:
        return result(_unknown("input_missing"))
    if not isinstance(record, Mapping):
        return result(_unknown("input_invalid"))
    if "unavailable" in record:
        return result(_unknown(_unavailable_reason(record["unavailable"])))
    items = record.get("items")
    if not isinstance(items, list):
        return result(_unknown("input_invalid"))
    for item in items:
        if not isinstance(item, Mapping):
            excluded["input_invalid"] += 1
            continue
        reason = _time_reason(item, window)
        if reason is None and item.get("direction") not in NEWS_DIRECTIONS:
            reason = "direction_unclassified"
        if reason:
            excluded[reason] += 1
        else:
            directions.append(item["direction"])
    for reason in ("input_invalid", "observed_time_unproven", "direction_unclassified"):
        if excluded[reason]:
            return result(_unknown(reason))
    if not directions:
        reason = next(
            (
                code
                for code in ("observed_after_cutoff", "input_stale")
                if excluded[code]
            ),
            "input_missing",
        )
        return result(_unknown(reason))
    net = directions.count("positive") - directions.count("negative")
    return result(_present(Decimal((net > 0) - (net < 0))))


def _rank(
    known: Mapping[str, Mapping[str, Decimal]], names: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    """Equal-weight mean of the scores of the inputs each candidate has.

    Numbers score as percentiles among the candidates (design §4.2 rule 2) and
    need two observations. News and disclosure directions score on their own
    scale, so one proven item counts. A missing input leaves the mean; it is
    never scored as zero or neutral.
    """
    scores: dict[str, list[tuple[str, Fraction]]] = {symbol: [] for symbol in known}
    for name in names:
        sign = -1 if name in _LOWER_IS_BETTER else 1
        observed = {
            symbol: Fraction(values[name]) * sign
            for symbol, values in known.items()
            if name in values
        }
        if name in LIST_INPUTS:
            for symbol, value in observed.items():
                scores[symbol].append((name, (value + 1) / 2))
            continue
        if len(observed) < 2:
            continue
        ordered = sorted(observed.values())
        for symbol, value in observed.items():
            worse = bisect_left(ordered, value)
            tied = bisect_right(ordered, value) - worse
            score = Fraction(2 * worse + tied - 1, 2 * (len(ordered) - 1))
            scores[symbol].append((name, score))
    composite = {
        symbol: sum(score for _, score in parts) / len(parts)
        for symbol, parts in scores.items()
        if parts
    }
    ascending = sorted(composite.values())
    result = {}
    for symbol, parts in scores.items():
        value = composite.get(symbol)
        if value is None:
            result[symbol] = {
                "rank": None,
                "score": None,
                "tied": False,
                "inputs_used": [],
            }
            continue
        above = len(ascending) - bisect_right(ascending, value)
        result[symbol] = {
            "rank": above + 1,
            "score": _score_string(value),
            "tied": bisect_right(ascending, value) - bisect_left(ascending, value) > 1,
            "inputs_used": [name for name, _ in parts],
        }
    return result


def _score_string(value: Fraction) -> str:
    """Display form only; ordering always uses the exact fraction."""
    with localcontext(prec=50):
        ratio = Decimal(value.numerator) / Decimal(value.denominator)
        return decimal_string(ratio.quantize(_SCORE_STEP, rounding=ROUND_HALF_EVEN))


def _accounts(raw: Any, window: _Window) -> dict[tuple[str, str], _Account]:
    accounts: dict[tuple[str, str], _Account] = {}
    for entry in raw if isinstance(raw, list) else []:
        entry = _mapping(entry)
        ref, currency = entry.get("account_ref"), entry.get("currency")
        if type(ref) is not str or not ref or type(currency) is not str:
            continue
        if (ref, currency) in accounts:
            # Two statements for one budget: neither is trusted.
            accounts[ref, currency] = _Account(cash_reason="input_invalid")
            continue
        account = accounts[ref, currency] = _Account()
        cash = _mapping(entry.get("cash"))
        if "unavailable" in cash:
            account.cash_reason = _unavailable_reason(cash["unavailable"])
        elif cash.get("value") is not None:
            account.cash_reason = _time_reason(cash, window)
            value = _decimal(cash["value"])
            if account.cash_reason is None:
                if value is None or value < 0:
                    account.cash_reason = "input_invalid"
                else:
                    account.cash = value
        _add_parking(account, entry.get("parking"), currency, window)
    return accounts


def _add_parking(account: _Account, raw: Any, currency: str, window: _Window) -> None:
    holdings = [_mapping(item) for item in raw] if isinstance(raw, list) else []
    symbols = [_parking_symbol(item.get("symbol")) for item in holdings]
    for item, symbol in zip(holdings, symbols, strict=True):
        quantity = _decimal(item.get("quantity"))
        price = _positive(item.get("price"))
        cost = _decimal(item.get("sell_cost_bps"))
        if currency != CURRENCY or not is_parking_instrument(symbol, "equity_kr"):
            reason = "not_parking_instrument"
        elif symbols.count(symbol) > 1:
            reason = "duplicate_parking_symbol"
        elif "unavailable" in item:
            reason = _unavailable_reason(item["unavailable"])
        elif any(
            item.get(name) is None for name in ("quantity", "price", "sell_cost_bps")
        ):
            reason = "input_missing"
        else:
            reason = _time_reason(item, window)
            if reason is None and (
                quantity is None
                or quantity < 0
                or price is None
                or cost is None
                or not 0 <= cost < 10000
            ):
                reason = "input_invalid"
        if reason:
            account.unusable.append({"symbol": symbol, "reason": reason})
            continue
        account.parking.append(_Parking(symbol, quantity // 1, price, cost))
    account.parking.sort(key=lambda holding: holding.symbol)
    account.unusable.sort(key=lambda item: (item["symbol"] or "", item["reason"]))


def _parking_symbol(value: Any) -> str | None:
    return to_db_symbol(value.strip().upper()) if type(value) is str else None


def _account_view(key: tuple[str, str], account: _Account) -> dict[str, Any]:
    return {
        "account_ref": key[0],
        "currency": key[1],
        "cash": None if account.cash is None else decimal_string(account.cash),
        "cash_reason": account.cash_reason,
        "parking": [
            {
                "symbol": holding.symbol,
                "whole_quantity": decimal_string(holding.units),
                "price": decimal_string(holding.price),
                "sell_cost_bps": decimal_string(holding.sell_cost_bps),
            }
            for holding in account.parking
        ],
        "parking_unusable": account.unusable,
    }


def _buy_intent(
    row: Mapping[str, Any],
    accounts: Mapping[tuple[str, str], _Account],
    evaluated_at: datetime,
) -> tuple[str | None, str | None, Decimal | None]:
    """Why this row can never draw budget, or the account and amount it asks."""
    account_ref = row.get("would_be_account_ref")
    amount = _requested_amount(row)
    until = _time(row.get("valid_until"))
    if until is None:
        reason = "valid_until_unknown"
    elif until <= evaluated_at:
        reason = "expired"
    elif row.get("disposition") == "dropped":
        reason = "disposition_dropped"
    elif row.get("action") not in _BUY_ACTIONS:
        reason = "no_buy_intent"
    elif row.get("not_executable_reason"):
        reason = "not_executable"
    elif not account_ref:
        reason = "account_unassigned"
    elif (account_ref, CURRENCY) not in accounts:
        reason = "account_facts_missing"
    elif amount is None:
        reason = "amount_unknown"
    else:
        reason = None
    return reason, account_ref, amount


def _requested_amount(row: Mapping[str, Any]) -> Decimal | None:
    """Order size first (quantity or notional), else the row's target total."""
    if row.get("would_be_quantity") is not None:
        quantity = _positive(row["would_be_quantity"])
        price = _positive(row.get("would_be_limit_price") or row.get("max_buy_price"))
        return None if quantity is None or price is None else quantity * price
    if row.get("would_be_notional") is not None:
        return _positive(row["would_be_notional"])
    return _positive(row.get("planned_amount"))


def _entry(
    row: Mapping[str, Any], facts: Mapping[str, Any], window: _Window
) -> dict[str, Any]:
    """Whether the row's price conditions hold at the proven reference price."""
    source = _mapping(_mapping(facts.get("sources")).get("price"))
    price = _positive(facts.get("last_close"))
    if "unavailable" in source:
        reason = _unavailable_reason(source["unavailable"])
    elif not source or facts.get("last_close") is None:
        reason = "input_missing"
    else:
        reason = _time_reason(source, window)
        if reason is None and price is None:
            reason = "input_invalid"
    if reason:
        price = None

    checks: list[bool | None] = []
    trigger = _mapping(row.get("trigger"))
    if trigger:
        level = _positive(trigger.get("value"))
        kind = trigger.get("kind")
        if price is None or level is None:
            checks.append(None)
        elif kind == "price_at_or_below":
            checks.append(price <= level)
        elif kind == "price_at_or_above":
            checks.append(price >= level)
        else:
            checks.append(None)
    if row.get("max_buy_price") is not None:
        ceiling = _positive(row["max_buy_price"])
        checks.append(None if price is None or ceiling is None else price <= ceiling)
    if False in checks:
        state = "not_met"
    elif not checks or None in checks:
        state = "unknown"
    else:
        state = "met"
    return {
        "state": state,
        "reference_price": None if price is None else decimal_string(price),
        "reference_price_reason": reason,
    }


def _allocate(
    policy: str,
    *,
    order: list[str],
    ranked: Mapping[str, Mapping[str, Any]],
    intents: Mapping[str, tuple[str, Decimal]],
    views: Mapping[str, Mapping[str, Any]],
    accounts: Mapping[tuple[str, str], _Account],
    max_adopt: int,
) -> dict[str, Any]:
    """Walk one ranking and draw cash first, then assumed parking proceeds.

    W holds the money (and a max_adopt slot) for a higher-ranked row whose entry
    condition is not met; N passes both to the next row. Parking units are whole
    and rounded down, so a shortfall smaller than one unit stays unfunded.
    """
    cash = {key: account.cash for key, account in accounts.items()}
    units = {
        (key, holding.symbol): holding.units
        for key, account in accounts.items()
        for holding in account.parking
    }
    slots = 0
    allocations = []
    for symbol in order:
        account_ref, amount = intents[symbol]
        key = (account_ref, CURRENCY)
        entry_state = views[symbol]["entry"]["state"]
        waiting = None if entry_state == "met" else f"entry_{entry_state}"
        outcome = {
            "symbol": symbol,
            "rank": ranked[symbol]["rank"],
            "account_ref": account_ref,
            "currency": CURRENCY,
            "requested": decimal_string(amount),
            "status": "not_funded",
            "reason": None,
            "allocated": "0",
            "cash_used": "0",
            "parking_proceeds": "0",
            "would_sell_parking": [],
            "precondition": None,
        }
        allocations.append(outcome)
        if ranked[symbol]["rank"] is None:
            outcome["reason"] = "unranked"
            continue
        if waiting and policy == "N":
            outcome.update(status="skipped_waiting", reason=waiting)
            continue
        if slots >= max_adopt:
            outcome["reason"] = "max_adopt_reached"
            continue
        if cash[key] is None:
            outcome["reason"] = "cash_unknown"
            continue

        cash_used = min(amount, cash[key])
        shortfall = amount - cash_used
        proceeds = _ZERO
        sales = []
        for holding in accounts[key].parking:
            fee_per_unit = holding.price * holding.sell_cost_bps / 10000
            net_per_unit = holding.price - fee_per_unit
            quantity = min(
                units[key, holding.symbol], (shortfall - proceeds) // net_per_unit
            )
            if quantity <= 0:
                continue
            units[key, holding.symbol] -= quantity
            proceeds += quantity * net_per_unit
            sales.append(
                {
                    "symbol": holding.symbol,
                    "quantity": decimal_string(quantity),
                    "est_gross": decimal_string(quantity * holding.price),
                    "est_cost": decimal_string(quantity * fee_per_unit),
                    "est_proceeds": decimal_string(quantity * net_per_unit),
                    "reason": "cash_short",
                }
            )
        allocated = cash_used + proceeds
        if allocated == 0:
            outcome["reason"] = "budget_exhausted"
            continue
        cash[key] -= cash_used
        slots += 1
        if waiting:
            status = "reserved_waiting"
        else:
            status = "funded" if allocated == amount else "partially_funded"
        outcome.update(
            status=status,
            reason=waiting,
            allocated=decimal_string(allocated),
            cash_used=decimal_string(cash_used),
            parking_proceeds=decimal_string(proceeds),
            would_sell_parking=sales,
            precondition=PARKING_PRECONDITION if sales else None,
        )
    return {
        "allocations": allocations,
        "accounts": [_account_totals(key, allocations) for key in sorted(accounts)],
    }


def _account_totals(
    key: tuple[str, str], allocations: list[dict[str, Any]]
) -> dict[str, Any]:
    drawn = [
        item for item in allocations if (item["account_ref"], item["currency"]) == key
    ]
    return {
        "account_ref": key[0],
        "currency": key[1],
        **{
            name: decimal_string(sum((Decimal(item[name]) for item in drawn), _ZERO))
            for name in ("allocated", "cash_used", "parking_proceeds")
        },
    }
