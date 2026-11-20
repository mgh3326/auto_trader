"""Pure retention cutoff for invest_screener_snapshots (hk 5752)."""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.invest_screener_snapshots.retention import retention_cutoff

NEWEST_FIRST = [dt.date(2026, 11, 20) - dt.timedelta(days=i) for i in range(7)]


def _kept(dates_newest_first, cutoff):
    """Dates that survive the delete ``snapshot_date < cutoff``."""
    if cutoff is None:
        return list(dates_newest_first)
    return [d for d in dates_newest_first if d >= cutoff]


def test_keeps_exactly_the_latest_five_trading_days() -> None:
    cutoff = retention_cutoff(NEWEST_FIRST, keep=5)

    assert _kept(NEWEST_FIRST, cutoff) == NEWEST_FIRST[:5]


def test_fewer_than_keep_is_a_no_op() -> None:
    four = NEWEST_FIRST[:4]

    assert retention_cutoff(four, keep=5) is None
    assert _kept(four, None) == four


def test_exactly_keep_dates_deletes_nothing() -> None:
    five = NEWEST_FIRST[:5]
    cutoff = retention_cutoff(five, keep=5)

    assert cutoff == five[-1]
    assert _kept(five, cutoff) == five


def test_no_rows_is_a_no_op() -> None:
    assert retention_cutoff([], keep=5) is None


def test_keep_must_be_positive() -> None:
    with pytest.raises(ValueError):
        retention_cutoff(NEWEST_FIRST, keep=0)


def test_cli_flag_defaults_off_and_rejects_zero() -> None:
    from scripts import build_invest_screener_snapshots as cli

    assert cli.parse_args(["--market", "kr"]).keep_trading_days is None
    assert (
        cli.parse_args(["--market", "kr", "--keep-trading-days", "5"]).keep_trading_days
        == 5
    )
    with pytest.raises(SystemExit):
        cli.parse_args(["--market", "kr", "--keep-trading-days", "0"])


def test_projected_cutoff_includes_pending_build_dates() -> None:
    """A dry-run must see the newer date the build is about to write (hk 5752 r1)."""
    from app.services.invest_screener_snapshots.retention import (
        projected_retention_cutoff,
    )

    persisted = NEWEST_FIRST[1:6]  # five stored dates, newest is 11-19
    pending = [NEWEST_FIRST[0]]  # the build writes a newer sixth date, 11-20

    cutoff = projected_retention_cutoff(persisted, pending, keep=5)

    # Union newest-first: 11-20, 11-19, 11-18, 11-17, 11-16, 11-15. Keep the newest
    # five, so the 5th newest (11-16) is the cutoff. Without pending, the cutoff
    # would be 11-15 and the preview would show nothing to delete.
    assert cutoff == NEWEST_FIRST[4]
    kept = sorted(set(persisted) | set(pending), reverse=True)
    assert _kept(kept, cutoff) == kept[:5]


def test_projected_cutoff_without_pending_matches_persisted_only() -> None:
    from app.services.invest_screener_snapshots.retention import (
        projected_retention_cutoff,
    )

    assert projected_retention_cutoff(NEWEST_FIRST[:5], [], keep=5) == NEWEST_FIRST[4]
    assert projected_retention_cutoff(NEWEST_FIRST[:3], [], keep=5) is None
