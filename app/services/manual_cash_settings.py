"""Operator-entered ``user_settings.manual_cash`` — validation and persistence (#671).

``manual_cash`` is the parking-balance term of the §177차 deployment cap
denominator (``app/services/deployment_cap.py``) and is folded into
``summary.total_orderable_krw`` by ``get_available_capital_impl``
(``app/mcp_server/tooling/portfolio_cash.py``). A wrong value therefore
oversizes a live advisory, so this module is deliberately strict:

* the value comes from operator input only — nothing here reads a broker,
  estimates, or prefills from a balance;
* every amount is a non-negative integer KRW ≤ ``MANUAL_CASH_MAX_KRW``
  (bool, float, NaN, Infinity, strings are rejected before arithmetic);
* a save that moves the total by more than 50% of the stored value — or away
  from an absent/zero/unreadable value — must carry an explicit confirmation,
  checked here on the server and not only in the UI;
* a save must name the ``updated_at`` it was edited against, so a form opened
  before someone else's update cannot silently overwrite it.

The stale rule is defined here once and ``portfolio_cash`` delegates to it, so
the settings screen shows exactly the rule the capital read applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_settings import UserSetting

MANUAL_CASH_KEY = "manual_cash"

# 100억 KRW. Per row and for the total. A parking balance above this is far
# more likely a typo (extra zeros) than a real balance; raising it is an
# explicit code change, not an input.
MANUAL_CASH_MAX_KRW = 10_000_000_000
MANUAL_CASH_MAX_ACCOUNTS = 20
MANUAL_CASH_NAME_MAX_LEN = 40

# "More than 50%" — a change of exactly half does not need confirmation.
MANUAL_CASH_JUMP_CONFIRM_RATIO = Decimal("0.5")

# The stale rule consumed by get_available_capital_impl: strictly older than
# this ⇒ stale_warning=true ⇒ excluded from total_orderable_krw and the
# deployment-cap parking term becomes 0 (``stale_treated_as_zero``).
MANUAL_CASH_STALE_AFTER = timedelta(days=3)

SOURCE_OPERATOR_CONFIRMED = "operator_confirmed"
ORIGIN_INVEST_SETTINGS_UI = "invest_settings_ui"


class ManualCashValidationError(ValueError):
    """The submitted breakdown is not a valid operator declaration."""


class ManualCashConflictError(Exception):
    """The stored value changed, or a large change was not confirmed."""

    def __init__(self, error: str, message: str, **context: Any) -> None:
        super().__init__(message)
        self.error = error
        self.context = context


@dataclass(frozen=True, slots=True)
class ManualCashAccount:
    name: str
    amount: int


def is_manual_cash_stale(updated_at_iso: str | None, *, now: datetime) -> bool:
    """True when the stored value is older than ``MANUAL_CASH_STALE_AFTER``.

    Missing or unparseable timestamps are stale (fail-closed). Naive
    timestamps are read as UTC.
    """

    if not updated_at_iso:
        return True
    try:
        updated_at = datetime.fromisoformat(updated_at_iso)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return updated_at < now - MANUAL_CASH_STALE_AFTER
    except (ValueError, TypeError):
        return True


def parse_stored_amount(value: Any) -> Decimal | None:
    """Read ``value["amount"]`` as a finite, non-negative Decimal, else None."""

    if not isinstance(value, dict):
        return None
    raw = value.get("amount")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def validate_krw_amount(raw: Any, *, field: str) -> int:
    """Accept only a plain ``int`` in ``[0, MANUAL_CASH_MAX_KRW]``."""

    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ManualCashValidationError(f"{field} must be an integer KRW amount")
    if raw < 0:
        raise ManualCashValidationError(f"{field} must not be negative")
    if raw > MANUAL_CASH_MAX_KRW:
        raise ManualCashValidationError(
            f"{field} must not exceed {MANUAL_CASH_MAX_KRW} KRW"
        )
    return raw


def validate_accounts(raw_accounts: Any) -> list[ManualCashAccount]:
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ManualCashValidationError("at least one parking account is required")
    if len(raw_accounts) > MANUAL_CASH_MAX_ACCOUNTS:
        raise ManualCashValidationError(
            f"at most {MANUAL_CASH_MAX_ACCOUNTS} parking accounts are allowed"
        )
    accounts: list[ManualCashAccount] = []
    for index, row in enumerate(raw_accounts):
        if not isinstance(row, dict):
            raise ManualCashValidationError(f"accounts[{index}] must be an object")
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ManualCashValidationError(f"accounts[{index}].name is required")
        name = name.strip()
        if len(name) > MANUAL_CASH_NAME_MAX_LEN:
            raise ManualCashValidationError(
                f"accounts[{index}].name must be at most "
                f"{MANUAL_CASH_NAME_MAX_LEN} characters"
            )
        amount = validate_krw_amount(
            row.get("amount"), field=f"accounts[{index}].amount"
        )
        accounts.append(ManualCashAccount(name=name, amount=amount))
    total = sum(account.amount for account in accounts)
    if total > MANUAL_CASH_MAX_KRW:
        raise ManualCashValidationError(
            f"total must not exceed {MANUAL_CASH_MAX_KRW} KRW"
        )
    return accounts


def change_ratio(current: Decimal | None, new_total: int) -> Decimal | None:
    """|new - current| / current, or None when there is no positive baseline."""

    if current is None or current <= 0:
        return None
    return abs(Decimal(new_total) - current) / current


def requires_large_change_confirmation(current: Decimal | None, new_total: int) -> bool:
    """>50% change, or any positive value over an absent/zero/unreadable one."""

    ratio = change_ratio(current, new_total)
    if ratio is None:
        return new_total > 0
    return ratio > MANUAL_CASH_JUMP_CONFIRM_RATIO


def _iso(value: datetime) -> str:
    return value.isoformat()


def serialize_manual_cash(row: UserSetting | None, *, now: datetime) -> dict[str, Any]:
    """Read model for the settings screen — same stale rule as the capital read."""

    stale_after = MANUAL_CASH_STALE_AFTER
    if row is None:
        return {
            "present": False,
            "amount": None,
            "amount_valid": False,
            "accounts": [],
            "source": None,
            "updated_at": None,
            "stale": True,
            "stale_at": None,
            "stale_after_hours": int(stale_after.total_seconds() // 3600),
        }
    value = row.value if isinstance(row.value, dict) else {}
    amount = parse_stored_amount(value)
    raw_accounts = value.get("accounts")
    accounts: list[dict[str, Any]] = []
    if isinstance(raw_accounts, list):
        for item in raw_accounts:
            if (
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and isinstance(item.get("amount"), int)
                and not isinstance(item.get("amount"), bool)
            ):
                accounts.append({"name": item["name"], "amount": item["amount"]})
    updated_at_iso = _iso(row.updated_at)
    source = value.get("source")
    return {
        "present": True,
        "amount": None if amount is None else int(amount),
        "amount_valid": amount is not None,
        "accounts": accounts,
        "source": source if isinstance(source, str) else None,
        "confirmed_at": value.get("confirmed_at"),
        "updated_at": updated_at_iso,
        "stale": is_manual_cash_stale(updated_at_iso, now=now),
        "stale_at": _iso(row.updated_at + stale_after),
        "stale_after_hours": int(stale_after.total_seconds() // 3600),
    }


async def load_manual_cash_row(
    db: AsyncSession, *, owner_user_id: int, for_update: bool = False
) -> UserSetting | None:
    stmt = select(UserSetting).where(
        UserSetting.user_id == owner_user_id,
        UserSetting.key == MANUAL_CASH_KEY,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


def _same_instant(expected_iso: str | None, row: UserSetting | None) -> bool:
    if row is None:
        return expected_iso is None
    if expected_iso is None:
        return False
    try:
        expected = datetime.fromisoformat(expected_iso)
    except ValueError:
        return False
    if expected.tzinfo is None:
        return False
    return expected == row.updated_at


async def save_manual_cash(
    db: AsyncSession,
    *,
    owner_user_id: int,
    actor_user_id: int,
    raw_accounts: Any,
    expected_updated_at: str | None,
    confirm_large_change: bool,
    now: datetime,
) -> dict[str, Any]:
    """Validate, guard, and upsert the operator's breakdown; return the read model.

    Raises ``ManualCashValidationError`` for malformed input and
    ``ManualCashConflictError`` for a stale form or an unconfirmed large change.
    Nothing is written in either case.
    """

    accounts = validate_accounts(raw_accounts)
    total = sum(account.amount for account in accounts)

    row = await load_manual_cash_row(db, owner_user_id=owner_user_id, for_update=True)
    if not _same_instant(expected_updated_at, row):
        # Serialize before rollback: rollback expires the loaded row.
        current = serialize_manual_cash(row, now=now)
        await db.rollback()
        raise ManualCashConflictError(
            "stale_form",
            "manual_cash changed since this form was loaded; reload before saving",
            current=current,
        )

    current_amount = parse_stored_amount(row.value) if row is not None else None
    if requires_large_change_confirmation(current_amount, total) and (
        confirm_large_change is not True
    ):
        await db.rollback()
        ratio = change_ratio(current_amount, total)
        raise ManualCashConflictError(
            "confirm_required",
            "a change of more than 50% requires explicit confirmation",
            current_amount=None if current_amount is None else int(current_amount),
            new_amount=total,
            change_ratio=None if ratio is None else format(ratio, "f"),
        )

    value = {
        "amount": total,
        "accounts": [
            {"name": account.name, "amount": account.amount} for account in accounts
        ],
        "source": SOURCE_OPERATOR_CONFIRMED,
        "origin": ORIGIN_INVEST_SETTINGS_UI,
        "confirmed_by_user_id": actor_user_id,
        "confirmed_at": _iso(now),
    }
    stmt = insert(UserSetting).values(
        user_id=owner_user_id, key=MANUAL_CASH_KEY, value=value
    )
    if row is None:
        # Nothing was there to lock, so a concurrent first write (another form
        # or the MCP set_user_setting path) may have landed since the SELECT.
        # Insert only if still absent; never overwrite what someone else wrote
        # against a baseline this form never saw.
        inserted = (
            await db.execute(
                stmt.on_conflict_do_nothing(
                    index_elements=["user_id", "key"]
                ).returning(UserSetting.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            await db.rollback()
            current = serialize_manual_cash(
                await load_manual_cash_row(db, owner_user_id=owner_user_id),
                now=now,
            )
            await db.rollback()
            raise ManualCashConflictError(
                "stale_form",
                "manual_cash changed since this form was loaded; reload before saving",
                current=current,
            )
    else:
        # The row is locked FOR UPDATE above; this only ever updates it.
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=["user_id", "key"],
                set_={"value": value, "updated_at": func.now()},
            )
        )
    await db.commit()

    saved = await load_manual_cash_row(db, owner_user_id=owner_user_id)
    if saved is not None:
        await db.refresh(saved)
    return serialize_manual_cash(saved, now=now)


__all__ = [
    "MANUAL_CASH_JUMP_CONFIRM_RATIO",
    "MANUAL_CASH_KEY",
    "MANUAL_CASH_MAX_ACCOUNTS",
    "MANUAL_CASH_MAX_KRW",
    "MANUAL_CASH_NAME_MAX_LEN",
    "MANUAL_CASH_STALE_AFTER",
    "ORIGIN_INVEST_SETTINGS_UI",
    "SOURCE_OPERATOR_CONFIRMED",
    "ManualCashAccount",
    "ManualCashConflictError",
    "ManualCashValidationError",
    "change_ratio",
    "is_manual_cash_stale",
    "load_manual_cash_row",
    "parse_stored_amount",
    "requires_large_change_confirmation",
    "save_manual_cash",
    "serialize_manual_cash",
    "validate_accounts",
    "validate_krw_amount",
]
