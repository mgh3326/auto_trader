"""hk 6503 — source-to-store orchestration for the KR trading flags.

Two sources, one store:

* ``kis-master`` — the KIS 종목정보 zip files (daily baseline). Parsed by
  ``kis_master.parse_kis_master_line``.
* ``naver-intraday`` — the Naver 관리·거래정지·경보 status list (intraday
  overlay). Parsed by ``naver_status.parse_naver_status_payload``.

Both write through ``store.upsert_flag_observations`` with the time of the
fetch as ``observed_at``. A dry run parses and counts, and writes nothing.
A source failure raises before any write, so a failed fetch never leaves a
partial snapshot behind.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.kr_symbol_universe_service import _download_mst_lines
from app.services.kr_trading_flags import vocabulary as v
from app.services.kr_trading_flags.kis_master import parse_kis_master_line
from app.services.kr_trading_flags.naver_status import (
    DEFAULT_PAGE_SIZE,
    fetch_naver_status_payload,
    parse_naver_status_payload,
)
from app.services.kr_trading_flags.store import upsert_flag_observations

_KOSPI_ZIP = "kospi_code.mst.zip"
_KOSDAQ_ZIP = "kosdaq_code.mst.zip"


#: A category where most KIS rows fail to map means the layout is wrong, not the
#: market. Refuse the write rather than store a wall of ``unmapped``.
MOSTLY_UNMAPPED_MIN_ROWS = 20
MOSTLY_UNMAPPED_SHARE = 0.5


class MostlyUnmappedError(RuntimeError):
    """A KIS category is mostly unmapped; nothing may be written."""


def refuse_if_mostly_unmapped(observations: list[v.FlagObservation]) -> None:
    by_category: dict[str, list[v.FlagObservation]] = {}
    for obs in observations:
        by_category.setdefault(obs.category, []).append(obs)
    for category, rows in sorted(by_category.items()):
        if len(rows) < MOSTLY_UNMAPPED_MIN_ROWS:
            continue
        unmapped = sum(1 for o in rows if o.state == v.STATE_UNMAPPED)
        if unmapped / len(rows) > MOSTLY_UNMAPPED_SHARE:
            raise MostlyUnmappedError(
                f"refusing KIS master write: {category} unmapped {unmapped}/{len(rows)}"
            )


def _summarize(
    observations: list[v.FlagObservation],
    *,
    source: str,
    skipped: int,
    dry_run: bool,
    write: dict[str, int] | None,
) -> dict[str, Any]:
    by_state: Counter[str] = Counter(f"{o.category}:{o.state}" for o in observations)
    return {
        "source": source,
        "dry_run": dry_run,
        "symbols": len({o.symbol for o in observations}),
        "observations": len(observations),
        "skipped_rows": skipped,
        "by_state": dict(sorted(by_state.items())),
        "write": write,
    }


async def _write(
    observations: list[v.FlagObservation], observed_at: datetime
) -> dict[str, int]:
    async with AsyncSessionLocal() as db:
        stats = await upsert_flag_observations(
            db, observations, observed_at=observed_at
        )
        await db.commit()
    return {
        "attempted": stats.attempted,
        "written": stats.written,
        "guarded": stats.guarded,
    }


async def sync_kis_master_flags(*, dry_run: bool, now: datetime) -> dict[str, Any]:
    kospi_lines = await _download_mst_lines(_KOSPI_ZIP)
    kosdaq_lines = await _download_mst_lines(_KOSDAQ_ZIP)

    observations: list[v.FlagObservation] = []
    skipped = 0
    for exchange, lines in (("KOSPI", kospi_lines), ("KOSDAQ", kosdaq_lines)):
        for line in lines:
            row = parse_kis_master_line(line, exchange=exchange)
            if row is None:
                skipped += 1
                continue
            observations.extend(row)

    refuse_if_mostly_unmapped(observations)
    write = None if dry_run else await _write(observations, now)
    return _summarize(
        observations,
        source=v.SOURCE_KIS_MASTER,
        skipped=skipped,
        dry_run=dry_run,
        write=write,
    )


async def sync_naver_intraday_flags(
    *,
    dry_run: bool,
    now: datetime,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    payload = await fetch_naver_status_payload(page_size)
    observations, skipped = parse_naver_status_payload(payload)
    write = None if dry_run else await _write(observations, now)
    return _summarize(
        observations,
        source=v.SOURCE_NAVER_INTRADAY,
        skipped=skipped,
        dry_run=dry_run,
        write=write,
    )
