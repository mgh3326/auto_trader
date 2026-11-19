"""Canonical content and hash of an approved clause or activation profile (§3.1, §3.5).

An approval request stores the canonical JSON of what the operator approves and
its SHA-256. The clause or profile row built from it must hash to the same value
when recomputed *from the database columns*, so every value is canonicalized
before hashing: decimals without trailing zeros (``30`` == ``30.00``),
timestamps as UTC microsecond strings, sets sorted, dates as ISO strings.

Pure: standard library only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any


def canonical_decimal(value: Any) -> str:
    """``30``, ``30.00`` and ``Decimal('3E+1')`` all give ``"30"``; zero is ``"0"``."""
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("a clause amount must be finite")
    if number == 0:
        return "0"
    text = format(number.normalize(), "f")
    return text


def canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("a clause timestamp needs a time zone")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_date(value: date) -> str:
    return value.isoformat()


def canonical_json(content: Mapping[str, Any]) -> str:
    return json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def content_sha256(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def _decimals(values: Iterable[Any]) -> list[str]:
    return [canonical_decimal(x) for x in values]


def terms_content(
    *,
    sleeve_id: int,
    terms_version: str,
    basket_key: str,
    account_ref: str,
    universe_rule: str,
    allowed_symbols: Sequence[str],
    entry_max_premium_bps: Any,
    per_order_cap: Any,
    daily_cap: Any,
    sector_cap_pct: Any,
    basket_drawdown_freeze_pct: Any,
    account_drawdown_freeze_pct: Any,
    tranche_rule: Mapping[str, Any],
    loss_sell_mode: str,
    expires_at: datetime,
    sleeve_state: str,
    budget_cap: Any,
    loss_limit: Any,
    prereg_metric: str | None,
    prereg_min_effect_pct: Any,
    prereg_horizon_sessions: int | None,
    prereg_max_duration_days: int | None,
) -> dict[str, Any]:
    """The content an operator approves: the clause columns and the sleeve values.

    Row identifiers (``sleeve_version_id``, ``approval_id``) are not part of it:
    they do not exist when the request is made.
    """
    return {
        "sleeve_id": int(sleeve_id),
        "terms_version": terms_version,
        "basket_key": basket_key,
        "account_ref": account_ref,
        "universe_rule": universe_rule,
        "allowed_symbols": sorted(set(allowed_symbols)),
        "entry_max_premium_bps": canonical_decimal(entry_max_premium_bps),
        "per_order_cap": canonical_decimal(per_order_cap),
        "daily_cap": canonical_decimal(daily_cap),
        "sector_cap_pct": canonical_decimal(sector_cap_pct),
        "basket_drawdown_freeze_pct": canonical_decimal(basket_drawdown_freeze_pct),
        "account_drawdown_freeze_pct": canonical_decimal(account_drawdown_freeze_pct),
        "tranche_rule": {
            "weights": _decimals(tranche_rule["weights"]),
            "triggers_pct_from_first": _decimals(
                tranche_rule["triggers_pct_from_first"]
            ),
            "min_gap_sessions": int(tranche_rule["min_gap_sessions"]),
        },
        "loss_sell_mode": loss_sell_mode,
        "expires_at": canonical_timestamp(expires_at),
        "sleeve": {
            "state": sleeve_state,
            "budget_cap": canonical_decimal(budget_cap),
            "loss_limit": canonical_decimal(loss_limit),
            "prereg_metric": prereg_metric,
            "prereg_min_effect_pct": (
                None
                if prereg_min_effect_pct is None
                else canonical_decimal(prereg_min_effect_pct)
            ),
            "prereg_horizon_sessions": prereg_horizon_sessions,
            "prereg_max_duration_days": prereg_max_duration_days,
        },
    }


def profile_content(
    *,
    sleeve_id: int,
    profile_version: int,
    terms_id: int,
    max_tranche_index: int,
    effective_session: date,
    expires_at: datetime,
) -> dict[str, Any]:
    return {
        "sleeve_id": int(sleeve_id),
        "profile_version": int(profile_version),
        "terms_id": int(terms_id),
        "max_tranche_index": int(max_tranche_index),
        "effective_session": canonical_date(effective_session),
        "expires_at": canonical_timestamp(expires_at),
    }
