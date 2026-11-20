"""Trading-day retention cutoff for invest_screener_snapshots (hk 5752).

Keeps the newest ``keep`` distinct snapshot dates per market. Pure: the caller
supplies the dates, newest first, and performs the delete.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence


def retention_cutoff(
    trading_dates_desc: Sequence[dt.date], keep: int
) -> dt.date | None:
    """Return the oldest snapshot date that must stay, or None for a no-op.

    Rows with ``snapshot_date < cutoff`` are the ones to delete. When there are
    fewer than ``keep`` distinct dates nothing is pruned, so the result is None.
    When exactly ``keep`` dates exist the cutoff is the oldest kept date, which
    deletes nothing.
    """
    if keep < 1:
        raise ValueError("keep must be >= 1")
    if len(trading_dates_desc) < keep:
        return None
    return trading_dates_desc[keep - 1]


def projected_retention_cutoff(
    persisted_dates_desc: Sequence[dt.date],
    pending_dates: Iterable[dt.date],
    keep: int,
) -> dt.date | None:
    """Cutoff computed over persisted dates plus the dates a build is about to write.

    A dry-run cannot read the rows it has not written yet, so it must add the
    build's dates itself. Otherwise its cutoff differs from the one a commit
    computes after writing, and the preview under-reports what the commit deletes.
    """
    union = sorted(set(persisted_dates_desc) | set(pending_dates), reverse=True)
    return retention_cutoff(union, keep)
