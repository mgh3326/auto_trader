"""Build investor_flow_snapshots payloads from KR investor-flow sources.

The builder is intentionally persistence-free so operator dry-runs can produce
approval-packet evidence without touching the database.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.investor_flow_snapshots.repository import InvestorFlowSnapshotUpsert
from app.services.naver_finance import fetch_investor_trends

InvestorTrendFetcher = Callable[
    [str, int], Awaitable[Mapping[str, Any]] | Mapping[str, Any]
]


@dataclass(frozen=True)
class InvestorFlowBuildResult:
    payloads: list[InvestorFlowSnapshotUpsert]
    warnings: tuple[str, ...] = ()


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _parse_snapshot_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _derive_individual(
    foreign_net: int | None, institution_net: int | None
) -> int | None:
    if foreign_net is None or institution_net is None:
        return None
    return -(foreign_net + institution_net)


def _streak_for(
    rows: Sequence[InvestorFlowSnapshotUpsert], start: int, attr: str
) -> tuple[int | None, int | None]:
    value = getattr(rows[start], attr)
    if value is None or value == 0:
        return None, None
    sign = 1 if value > 0 else -1
    streak = 0
    for row in rows[start:]:
        candidate = getattr(row, attr)
        if candidate is None or candidate == 0:
            break
        candidate_sign = 1 if candidate > 0 else -1
        if candidate_sign != sign:
            break
        streak += 1
    if sign > 0:
        return streak, None
    return None, streak


def _apply_streaks(
    payloads: list[InvestorFlowSnapshotUpsert],
) -> list[InvestorFlowSnapshotUpsert]:
    by_symbol: dict[str, list[InvestorFlowSnapshotUpsert]] = defaultdict(list)
    for payload in payloads:
        by_symbol[payload.symbol].append(payload)

    updated: list[InvestorFlowSnapshotUpsert] = []
    by_key: dict[tuple[str, dt.date, str], InvestorFlowSnapshotUpsert] = {}
    for symbol, rows in by_symbol.items():
        sorted_rows = sorted(rows, key=lambda p: p.snapshot_date, reverse=True)
        for idx, row in enumerate(sorted_rows):
            foreign_buy, foreign_sell = _streak_for(sorted_rows, idx, "foreign_net")
            institution_buy, institution_sell = _streak_for(
                sorted_rows, idx, "institution_net"
            )
            individual_buy, individual_sell = _streak_for(
                sorted_rows, idx, "individual_net"
            )
            by_key[(symbol, row.snapshot_date, row.source)] = row.model_copy(
                update={
                    "foreign_consecutive_buy_days": foreign_buy,
                    "foreign_consecutive_sell_days": foreign_sell,
                    "institution_consecutive_buy_days": institution_buy,
                    "institution_consecutive_sell_days": institution_sell,
                    "individual_consecutive_buy_days": individual_buy,
                    "individual_consecutive_sell_days": individual_sell,
                }
            )
    for payload in payloads:
        updated.append(by_key[(payload.symbol, payload.snapshot_date, payload.source)])
    return updated


def _rank_values(
    values: list[tuple[int, InvestorFlowSnapshotUpsert]], *, reverse: bool
) -> dict[tuple[str, dt.date, str], int]:
    sorted_values = sorted(values, key=lambda item: item[0], reverse=reverse)
    ranks: dict[tuple[str, dt.date, str], int] = {}
    for idx, (_, payload) in enumerate(sorted_values, start=1):
        ranks[(payload.symbol, payload.snapshot_date, payload.source)] = idx
    return ranks


def _apply_ranks(
    payloads: list[InvestorFlowSnapshotUpsert],
) -> list[InvestorFlowSnapshotUpsert]:
    by_date: dict[dt.date, list[InvestorFlowSnapshotUpsert]] = defaultdict(list)
    for payload in payloads:
        by_date[payload.snapshot_date].append(payload)

    rank_updates: dict[tuple[str, dt.date, str], dict[str, int]] = defaultdict(dict)
    for snapshot_date, rows in by_date.items():
        del snapshot_date
        foreign_buys = [
            (p.foreign_net, p)
            for p in rows
            if p.foreign_net is not None and p.foreign_net > 0
        ]
        foreign_sells = [
            (p.foreign_net, p)
            for p in rows
            if p.foreign_net is not None and p.foreign_net < 0
        ]
        institution_buys = [
            (p.institution_net, p)
            for p in rows
            if p.institution_net is not None and p.institution_net > 0
        ]
        institution_sells = [
            (p.institution_net, p)
            for p in rows
            if p.institution_net is not None and p.institution_net < 0
        ]
        for key, rank in _rank_values(foreign_buys, reverse=True).items():
            rank_updates[key]["foreign_net_buy_rank"] = rank
        for key, rank in _rank_values(foreign_sells, reverse=False).items():
            rank_updates[key]["foreign_net_sell_rank"] = rank
        for key, rank in _rank_values(institution_buys, reverse=True).items():
            rank_updates[key]["institution_net_buy_rank"] = rank
        for key, rank in _rank_values(institution_sells, reverse=False).items():
            rank_updates[key]["institution_net_sell_rank"] = rank

    return [
        payload.model_copy(
            update=rank_updates.get(
                (payload.symbol, payload.snapshot_date, payload.source), {}
            )
        )
        for payload in payloads
    ]


async def _call_fetcher(
    fetcher: InvestorTrendFetcher, symbol: str, days: int
) -> Mapping[str, Any]:
    result = fetcher(symbol, days)
    if inspect.isawaitable(result):
        return await result
    return result


async def build_investor_flow_snapshots(
    *,
    symbols: Sequence[str],
    days: int = 20,
    today: dt.date | None = None,
    collected_at: dt.datetime | None = None,
    fetcher: InvestorTrendFetcher = fetch_investor_trends,
    concurrency: int = 4,
) -> InvestorFlowBuildResult:
    """Fetch and convert KR investor trends into repository upsert payloads."""
    del today  # Kept for deterministic call-site symmetry; Naver rows carry dates.
    collected_at = collected_at or dt.datetime.now(dt.UTC)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    warnings: list[str] = []
    payloads: list[InvestorFlowSnapshotUpsert] = []

    async def build_symbol(
        symbol: str,
    ) -> tuple[list[InvestorFlowSnapshotUpsert], tuple[str, ...]]:
        normalized = _normalize_symbol(symbol)
        async with semaphore:
            try:
                result = await _call_fetcher(fetcher, normalized, days)
            except (
                Exception
            ) as exc:  # pragma: no cover - defensive external-source boundary
                return [], (f"{normalized}: fetch failed: {exc.__class__.__name__}",)
        rows = result.get("data") if isinstance(result, Mapping) else None
        if not rows:
            return [], (f"{normalized}: no investor-flow rows returned",)
        built: list[InvestorFlowSnapshotUpsert] = []
        local_warnings: list[str] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                local_warnings.append(f"{normalized}: row {index} is not an object")
                continue
            snapshot_date = _parse_snapshot_date(row.get("date"))
            if snapshot_date is None:
                local_warnings.append(f"{normalized}: row {index} has invalid date")
                continue
            foreign_net = _int_or_none(row.get("foreign_net"))
            institution_net = _int_or_none(row.get("institutional_net"))
            individual_net = _int_or_none(row.get("individual_net"))
            if individual_net is None:
                individual_net = _derive_individual(foreign_net, institution_net)
            close = row.get("close")
            change_rate = row.get("change_pct")
            if change_rate is not None:
                # Naver fetcher returns change_pct as a fraction (e.g., 0.015 for 1.5%).
                # Multiply by 100 to store as a percent (e.g., 1.5).
                change_rate = change_rate * 100
            volume = _int_or_none(row.get("volume"))
            foreign_holding_shares = _int_or_none(row.get("foreign_holding_shares"))
            foreign_holding_rate = row.get("foreign_holding_rate")
            built.append(
                InvestorFlowSnapshotUpsert(
                    market="kr",
                    symbol=normalized,
                    snapshot_date=snapshot_date,
                    foreign_net=foreign_net,
                    institution_net=institution_net,
                    individual_net=individual_net,
                    close=close,
                    change_rate=change_rate,
                    volume=volume,
                    foreign_holding_shares=foreign_holding_shares,
                    foreign_holding_rate=foreign_holding_rate,
                    source="naver_finance",
                    collected_at=collected_at,
                )
            )
        if not built:
            local_warnings.append(f"{normalized}: no valid investor-flow rows built")
        return built, tuple(local_warnings)

    results = await asyncio.gather(*(build_symbol(symbol) for symbol in symbols))
    for built, local_warnings in results:
        payloads.extend(built)
        warnings.extend(local_warnings)

    payloads = _apply_ranks(_apply_streaks(payloads))
    payloads.sort(key=lambda p: (p.symbol, p.snapshot_date), reverse=False)
    return InvestorFlowBuildResult(payloads=payloads, warnings=tuple(warnings))
