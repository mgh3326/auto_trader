"""Observation-only recorder for live fanout screener picks.

Lives *outside* ``buy_candidate_fanout``. That module's contract is
"performs no writes"; this wrapper records what the fanout already returned.

Fail-open: logging errors never propagate to the caller.
Default-off: ``SCREENER_PICK_LOG_ENABLED`` must be true.
No scheduler registration. Callers are existing fanout entrypoints only.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_TRUE = frozenset({"1", "true", "yes", "on"})
_KST = ZoneInfo("Asia/Seoul")
_FANOUT_PATH = (
    Path(__file__).resolve().parents[1]
    / "mcp_server"
    / "tooling"
    / "buy_candidate_fanout.py"
)
FANOUT_VERSION_PREFIX = "buy_candidate_fanout"


@dataclass(frozen=True, slots=True)
class ScreenerPickRow:
    """One source-ranked pick extracted from a fanout return."""

    call_id: uuid.UUID
    recorded_at: datetime
    recorded_at_kst: str
    market: str
    source: str
    family: str
    kind: str
    symbol: str
    rank: int | None
    decision_price_text: str | None
    source_sort_by: str | None
    source_sort_order: str | None
    source_limit: int | None
    source_preset: str | None
    fanout_version: str
    fanout_code_sha256: str
    source_params: dict[str, Any]


def env_gate_enabled() -> bool:
    """Call-time env gate. Default off. Independent of the Settings singleton."""

    return os.environ.get("SCREENER_PICK_LOG_ENABLED", "").strip().lower() in _TRUE


def exact_decimal_text(value: object) -> str | None:
    """Return a finite decimal as a canonical text string.

    Floats are refused. Pass a ``Decimal``, ``int``, or numeric string.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("bool is not an exact decimal price")
    if isinstance(value, float):
        raise TypeError(
            "float prices are forbidden; pass an exact decimal string or Decimal"
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return format(value, "f")
    if isinstance(value, int):
        return format(Decimal(value), "f")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except InvalidOperation as exc:
            raise TypeError(f"not an exact decimal string: {text!r}") from exc
        if not parsed.is_finite():
            return None
        return format(parsed, "f")
    raise TypeError(f"unsupported price type: {type(value).__name__}")


def fanout_code_sha256(path: Path | None = None) -> str:
    target = path or _FANOUT_PATH
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return str(value)
    return None


def _price_from_candidate(candidate: Mapping[str, Any]) -> str | None:
    funnel = candidate.get("funnel")
    funnel_data = funnel if isinstance(funnel, Mapping) else {}
    eligibility = funnel_data.get("base_eligibility")
    eligibility_data = eligibility if isinstance(eligibility, Mapping) else {}
    for raw in (
        eligibility_data.get("current_price"),
        candidate.get("current_price"),
        candidate.get("latest_close"),
    ):
        if raw is None:
            continue
        try:
            return exact_decimal_text(raw)
        except TypeError:
            # Fanout currently emits Python floats for current_price. Convert
            # via Decimal(str(...)) so the column stays text, never a float.
            if (
                isinstance(raw, float)
                and raw == raw
                and raw not in {float("inf"), float("-inf")}
            ):
                return format(Decimal(str(raw)), "f")
            continue
    return None


def extract_pick_rows(
    result: Mapping[str, Any],
    *,
    now: datetime | None = None,
    call_id: uuid.UUID | None = None,
    code_sha256: str | None = None,
) -> list[ScreenerPickRow]:
    """Pure projection of a fanout return into log rows. No I/O."""

    recorded_at = now or datetime.now(UTC)
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=UTC)
    recorded_at_utc = recorded_at.astimezone(UTC)
    recorded_at_kst = recorded_at_utc.astimezone(_KST).isoformat()
    resolved_call_id = call_id or uuid.uuid4()
    digest = code_sha256 or fanout_code_sha256()
    market = str(result.get("market") or "").strip()
    bounds = result.get("bounds") if isinstance(result.get("bounds"), Mapping) else {}
    default_limit = _as_int(bounds.get("top_n_per_source"))
    fanout_version = f"{FANOUT_VERSION_PREFIX}:top_n_per_source={default_limit}"

    sources_by_name: dict[str, Mapping[str, Any]] = {}
    for item in result.get("sources") or []:
        if isinstance(item, Mapping) and item.get("source"):
            sources_by_name[str(item["source"])] = item

    rows: list[ScreenerPickRow] = []
    seen: set[tuple[str, str]] = set()
    for candidate in result.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        symbol = str(candidate.get("symbol") or "").strip()
        if not symbol:
            continue
        price_text = _price_from_candidate(candidate)
        source_rows = candidate.get("source_rows")
        entries = source_rows if isinstance(source_rows, list) else []
        if not entries:
            matched = candidate.get("matched_sources")
            names = matched if isinstance(matched, list) else []
            entries = [{"source": name} for name in names]
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            source = str(entry.get("source") or "").strip()
            if not source:
                continue
            key = (source, symbol)
            if key in seen:
                continue
            seen.add(key)
            meta_source = sources_by_name.get(source, {})
            metadata = (
                meta_source.get("metadata")
                if isinstance(meta_source.get("metadata"), Mapping)
                else {}
            )
            request = (
                metadata.get("request")
                if isinstance(metadata.get("request"), Mapping)
                else {}
            )
            source_params = {
                "request": dict(request) if request else {},
                "preset": metadata.get("preset"),
                "kind": meta_source.get("kind") or entry.get("kind"),
            }
            rows.append(
                ScreenerPickRow(
                    call_id=resolved_call_id,
                    recorded_at=recorded_at_utc,
                    recorded_at_kst=recorded_at_kst,
                    market=market,
                    source=source,
                    family=str(
                        entry.get("family") or meta_source.get("family") or source
                    ),
                    kind=str(entry.get("kind") or meta_source.get("kind") or ""),
                    symbol=symbol,
                    rank=_as_int(entry.get("rank")),
                    decision_price_text=price_text,
                    source_sort_by=_as_text(request.get("sort_by")),
                    source_sort_order=_as_text(request.get("sort_order")),
                    source_limit=_as_int(request.get("limit")) or default_limit,
                    source_preset=_as_text(metadata.get("preset")),
                    fanout_version=fanout_version,
                    fanout_code_sha256=digest,
                    source_params=source_params,
                )
            )
    return rows


async def _default_write(rows: Sequence[ScreenerPickRow]) -> None:
    from app.core.db import AsyncSessionLocal
    from app.models.screener_pick_log import ScreenerPickLog

    async with AsyncSessionLocal() as session:
        session.add_all(
            [
                ScreenerPickLog(
                    call_id=row.call_id,
                    recorded_at=row.recorded_at,
                    recorded_at_kst=row.recorded_at_kst,
                    market=row.market,
                    source=row.source,
                    family=row.family,
                    kind=row.kind,
                    symbol=row.symbol,
                    rank=row.rank,
                    decision_price_text=row.decision_price_text,
                    source_sort_by=row.source_sort_by,
                    source_sort_order=row.source_sort_order,
                    source_limit=row.source_limit,
                    source_preset=row.source_preset,
                    fanout_version=row.fanout_version,
                    fanout_code_sha256=row.fanout_code_sha256,
                    source_params=row.source_params,
                )
                for row in rows
            ]
        )
        await session.commit()


async def maybe_record_fanout_picks(
    result: Mapping[str, Any],
    *,
    enabled: bool | None = None,
    write_rows: Callable[[Sequence[ScreenerPickRow]], Awaitable[None]] | None = None,
    now: datetime | None = None,
    code_sha256: str | None = None,
) -> None:
    """Record fanout picks when the env gate is on. Never raises. Never mutates ``result``."""

    try:
        if enabled is None:
            enabled = env_gate_enabled()
        if not enabled:
            return
        if not isinstance(result, Mapping):
            return
        rows = extract_pick_rows(result, now=now, code_sha256=code_sha256)
        if not rows:
            return
        writer = write_rows or _default_write
        await writer(rows)
    except Exception:
        logger.warning(
            "screener pick log failed; fanout result is unchanged",
            exc_info=True,
        )


__all__ = [
    "FANOUT_VERSION_PREFIX",
    "ScreenerPickRow",
    "env_gate_enabled",
    "exact_decimal_text",
    "extract_pick_rows",
    "fanout_code_sha256",
    "maybe_record_fanout_picks",
]
