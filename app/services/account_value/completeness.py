"""Append-only operator confirmations of external-flow completeness."""

from datetime import timedelta

from sqlalchemy import (
    TIMESTAMP,
    Column,
    Date,
    MetaData,
    Table,
    Text,
    insert,
    select,
    text,
)

from app.services.account_value.identity import valid_account

confirmations = Table(
    "account_flow_confirmations",
    MetaData(),
    Column("account", Text),
    Column("start_day", Date),
    Column("end_day", Date),
    Column("confirmed_at", TIMESTAMP(timezone=True)),
    Column("confirmed_by", Text),
    Column("reason", Text),
    schema="review",
)


def covered(start, end, records, flows):
    """Every date must have a confirmation no older than its newest flow."""
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        dates = [r.confirmed_at for r in records if r.start_day <= day <= r.end_day]
        if not dates:
            return False
        latest = max(dates)
        if any(f.day == day and f.created_at > latest for f in flows):
            return False
    return True


async def acquire_account_lock(session, account: str) -> None:
    """Hold the per-account writer lock until the transaction ends.

    Flow rows and confirmations commit on independent sessions, so a
    confirmation could otherwise be written while a flow insert for the same
    account is still in flight and become visible first. Taking the same
    advisory lock on both writers makes the confirmation wait for the flow's
    commit, restoring the ordering ``covered()`` assumes.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:account))"),
        {"account": account},
    )


async def load_confirmations(session, start, end):
    result = await session.execute(
        select(confirmations).where(
            confirmations.c.start_day <= end, confirmations.c.end_day >= start
        )
    )
    return result.all()


async def record_confirmation(
    session, account, start, end, confirmed_by, reason, *, commit=False
):
    if not valid_account(account) or account.endswith(":identity_unknown"):
        raise ValueError("invalid account")
    if end < start or not confirmed_by.strip() or not reason.strip():
        raise ValueError("invalid confirmation")
    if not commit:
        return "preview"
    await acquire_account_lock(session, account)
    await session.execute(
        insert(confirmations).values(
            account=account,
            start_day=start,
            end_day=end,
            confirmed_by=confirmed_by,
            reason=reason,
        )
    )
    await session.commit()
    return "inserted"
