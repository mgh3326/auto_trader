"""Outer pick-log observer for the live ``screen_stocks*`` MCP tools (hk 5622).

The KR/US live sessions discover candidates through ``screen_stocks`` and
``screen_stocks_snapshot``, not through ``discover_buy_candidates_fanout``, so the
fanout observer in ``screener_pick_log`` never saw them. This module records what
those two tools *returned* into ``review.screener_pick_log``.

Contract (AGENTS.md hard rule 13):

* The tool implementations stay write-free. The observer is called only from the
  MCP registration layer (``analysis_registration``). It is never called from
  ``screen_stocks_impl`` itself, because ``buy_candidate_fanout`` and the web
  screener reuse that impl and must not write.
* Default-off (``SCREENER_PICK_LOG_ENABLED``), fail-open (a recorder error returns
  the tool output untouched), no scheduler.
* ``collection_version="screener-tools-v1"``. Past days are never backfilled.
* Prices are exact decimal text, never floats.

Row semantics (the table is shared with the fanout A-record):

* ``admission='admitted'`` — the row was returned to the caller.
* ``admission='not_admitted'`` — the tool itself knew the row and removed it
  (``halted_suspect``, ``exclude_symbols`` ...). ``admission_reason`` is a closed
  code. Rows beyond the returned page are *not* recorded: the session never saw them.
* ``rank`` — the tool's own rank for the first matched preset; the 1-based position
  for ``screen_stocks`` (its rows carry no rank).
* ``selection_seq`` — 1-based position in the returned list (offset included).
* ``source_status`` is ``ok`` for every recorded row; the snapshot's own freshness
  verdict (fresh/partial/stale/...) is kept in ``call_context.freshness`` because the
  column vocabulary has no equivalent.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.core.symbol import to_db_symbol
from app.services.screener_pick_log import (
    ScreenerPickRow,
    _as_int,
    _as_text,
    _default_write,
    _exact_or_float_text,
    _parse_fetched_at,
    env_gate_enabled,
)

logger = logging.getLogger(__name__)

COLLECTION_VERSION = "screener-tools-v1"
TOOL_SCREEN_STOCKS = "screen_stocks"
TOOL_SCREEN_STOCKS_SNAPSHOT = "screen_stocks_snapshot"
FANOUT_VERSION_PREFIX = "screener_tools"

ADMITTED = "admitted"
NOT_ADMITTED = "not_admitted"
REASON_RETURNED = "returned_to_session"
REASON_HALTED_SUSPECT = "halted_suspect"

_KST = ZoneInfo("Asia/Seoul")
_RECORDED_MARKETS = frozenset({"kr", "us"})
_KR_ALIASES = frozenset({"kr", "kospi", "kosdaq", "konex", "all"})
_SELF_PATH = Path(__file__).resolve()
_LABEL_PRICE = re.compile(r"^\$?([0-9][0-9,]*(?:\.[0-9]+)?)(?:원)?$")

WriteRows = Callable[[Sequence[ScreenerPickRow]], Awaitable[None]]


def tool_code_sha256() -> str:
    """Hash of this module — the code that decides what a row contains."""

    return hashlib.sha256(_SELF_PATH.read_bytes()).hexdigest()


def _json_safe(value: object) -> Any:
    """Detach and make JSONB-safe: no NaN/Inf, no numpy scalars, no odd types."""

    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_safe(item())
        except Exception:  # noqa: BLE001 — fall through to str()
            pass
    return str(value)


def _object(value: object) -> dict[str, Any]:
    safe = _json_safe(value)
    return safe if isinstance(safe, dict) else {}


def _normalize_market(raw: object) -> str | None:
    text = str(raw or "").strip().lower()
    if text in _KR_ALIASES:
        return "kr"
    if text in _RECORDED_MARKETS:
        return text
    return None


def _symbol_key(market: str, raw: object) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if market == "us":
        try:
            return to_db_symbol(text)
        except Exception:  # noqa: BLE001
            return text
    return text


def _label_price_text(label: object) -> str | None:
    """Exact decimal text out of a formatted ``priceLabel`` ("75,000원", "$12.30")."""

    if not isinstance(label, str):
        return None
    match = _LABEL_PRICE.match(label.strip())
    if match is None:
        return None
    return match.group(1).replace(",", "")


def _stamp(now: datetime | None) -> tuple[datetime, str]:
    recorded_at = now or datetime.now(UTC)
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=UTC)
    utc = recorded_at.astimezone(UTC)
    return utc, utc.astimezone(_KST).isoformat()


def _request_context(request: Mapping[str, Any]) -> dict[str, Any]:
    """The caller's non-default arguments, JSONB-safe."""

    return {k: _json_safe(v) for k, v in request.items() if v is not None}


def _row(
    *,
    call_id: uuid.UUID,
    recorded_at: datetime,
    recorded_at_kst: str,
    market: str,
    tool: str,
    source: str,
    family: str,
    kind: str,
    symbol: str,
    rank: int | None,
    price_text: str | None,
    sort_by: str | None,
    sort_order: str | None,
    limit: int | None,
    preset: str | None,
    admission: str,
    admission_reason: str,
    selection_seq: int | None,
    data_asof: str | None,
    fetched_at: datetime | None,
    raw_row: dict[str, Any] | None,
    source_params: dict[str, Any],
    call_context: dict[str, Any],
    code_sha256: str,
) -> ScreenerPickRow:
    return ScreenerPickRow(
        call_id=call_id,
        recorded_at=recorded_at,
        recorded_at_kst=recorded_at_kst,
        market=market,
        source=source,
        family=family,
        kind=kind,
        symbol=symbol,
        rank=rank,
        decision_price_text=price_text,
        source_sort_by=sort_by,
        source_sort_order=sort_order,
        source_limit=limit,
        source_preset=preset,
        fanout_version=(
            f"{FANOUT_VERSION_PREFIX}:{tool}:collection={COLLECTION_VERSION}"
        ),
        fanout_code_sha256=code_sha256,
        source_params=source_params,
        collection_version=COLLECTION_VERSION,
        admission=admission,
        admission_reason=admission_reason,
        selection_seq=selection_seq,
        source_status="ok",
        data_asof=data_asof,
        fetched_at=fetched_at,
        raw_row=raw_row,
        gate_features=None,
        call_context=call_context,
    )


def extract_screen_stocks_rows(
    result: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    call_id: uuid.UUID,
    now: datetime | None = None,
    code_sha256: str | None = None,
) -> list[ScreenerPickRow]:
    """Pure projection of a ``screen_stocks`` return into log rows. No I/O."""

    filters_applied = result.get("filters_applied")
    applied = filters_applied if isinstance(filters_applied, Mapping) else {}
    market = _normalize_market(applied.get("market") or request.get("market"))
    results = result.get("results")
    if market is None or not isinstance(results, list):
        return []

    recorded_at, recorded_at_kst = _stamp(now)
    digest = code_sha256 or tool_code_sha256()
    meta = result.get("meta") if isinstance(result.get("meta"), Mapping) else {}
    timestamp = result.get("timestamp")
    data_asof = _as_text(timestamp)
    fetched_at = _parse_fetched_at(timestamp)
    sort_by = _as_text(applied.get("sort_by") or request.get("sort_by"))
    sort_order = _as_text(applied.get("sort_order") or request.get("sort_order"))
    limit = _as_int(applied.get("limit")) or _as_int(request.get("limit"))
    preset = _as_text(request.get("strategy"))
    call_context = {
        "tool": TOOL_SCREEN_STOCKS,
        "request": _request_context(request),
        "provider_source": _as_text(meta.get("source")),
        "total_count": _as_int(result.get("total_count")),
        "returned_count": len(results),
        "price_basis": "tool_price_field",
    }
    source_params = {
        "request": _request_context(request),
        "filters_applied": _object(applied),
        "kind": "live_screen",
    }
    common = {
        "call_id": call_id,
        "recorded_at": recorded_at,
        "recorded_at_kst": recorded_at_kst,
        "market": market,
        "tool": TOOL_SCREEN_STOCKS,
        "source": TOOL_SCREEN_STOCKS,
        "family": "screen_stocks_live",
        "kind": "live_screen",
        "sort_by": sort_by,
        "sort_order": sort_order,
        "limit": limit,
        "preset": preset,
        "data_asof": data_asof,
        "fetched_at": fetched_at,
        "source_params": source_params,
        "call_context": call_context,
        "code_sha256": digest,
    }

    rows: list[ScreenerPickRow] = []
    seen: set[str] = set()
    for index, item in enumerate(results, start=1):
        if not isinstance(item, Mapping):
            continue
        symbol = _symbol_key(market, item.get("symbol") or item.get("code"))
        if symbol is None or symbol in seen:
            continue
        seen.add(symbol)
        rows.append(
            _row(
                **common,
                symbol=symbol,
                rank=index,
                price_text=_exact_or_float_text(item.get("price")),
                admission=ADMITTED,
                admission_reason=REASON_RETURNED,
                selection_seq=index,
                raw_row=_object(item),
            )
        )

    excluded = meta.get("halted_suspect_excluded")
    for item in excluded if isinstance(excluded, list) else []:
        if not isinstance(item, Mapping):
            continue
        symbol = _symbol_key(market, item.get("symbol"))
        if symbol is None or symbol in seen:
            continue
        seen.add(symbol)
        rows.append(
            _row(
                **common,
                symbol=symbol,
                rank=None,
                price_text=None,
                admission=NOT_ADMITTED,
                admission_reason=REASON_HALTED_SUSPECT,
                selection_seq=None,
                raw_row=_object(item),
            )
        )
    return rows


def _freshness_context(freshness: object) -> dict[str, Any]:
    if not isinstance(freshness, Mapping):
        return {}
    primary = freshness.get("primary")
    primary_map = primary if isinstance(primary, Mapping) else {}
    return {
        "dataState": freshness.get("dataState"),
        "overallState": freshness.get("overallState"),
        "fetchedAt": freshness.get("fetchedAt"),
        "primary": {
            key: primary_map.get(key)
            for key in (
                "kind",
                "snapshotDate",
                "computedAt",
                "dataState",
                "source",
                "degradationReason",
            )
        },
    }


def extract_snapshot_rows(
    payload: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    dropped_rows: Sequence[tuple[Mapping[str, Any], str]],
    freshness_by_preset: Mapping[str, Any],
    preset_ids: Sequence[str],
    call_id: uuid.UUID,
    now: datetime | None = None,
    code_sha256: str | None = None,
) -> list[ScreenerPickRow]:
    """Pure projection of a ``screen_stocks_snapshot`` return into log rows.

    One row per (matched preset, symbol): the source is ``screener_snapshot:<preset>``
    so the table's (call, source, symbol) key stays unique for a multi-preset sweep.
    """

    results = payload.get("results")
    if not isinstance(results, list):
        return []
    market = _normalize_market(request.get("market"))
    if market is None:
        return []

    recorded_at, recorded_at_kst = _stamp(now)
    digest = code_sha256 or tool_code_sha256()
    pagination = (
        payload.get("pagination")
        if isinstance(payload.get("pagination"), Mapping)
        else {}
    )
    offset = _as_int(pagination.get("offset")) or 0
    limit = _as_int(pagination.get("limit")) or _as_int(request.get("limit"))
    freshness_context = {
        preset: _freshness_context(freshness_by_preset.get(preset))
        for preset in preset_ids
    }
    call_context = {
        "tool": TOOL_SCREEN_STOCKS_SNAPSHOT,
        "request": _request_context(request),
        "presets": list(preset_ids),
        "pagination": _object(pagination),
        "freshness": freshness_context,
        "price_basis": "price_label_parsed",
    }
    sort_by = _as_text(request.get("sort"))

    def sources_for(item: Mapping[str, Any]) -> list[str]:
        matched = item.get("matchedPresets")
        names = [str(p) for p in matched] if isinstance(matched, list) else []
        if not names and len(preset_ids) == 1:
            names = [preset_ids[0]]
        return names

    def build(
        item: Mapping[str, Any],
        *,
        preset: str,
        first: bool,
        symbol: str,
        admission: str,
        reason: str,
        seq: int | None,
    ) -> ScreenerPickRow:
        freshness = freshness_by_preset.get(preset)
        primary = (
            freshness.get("primary")
            if isinstance(freshness, Mapping)
            and isinstance(freshness.get("primary"), Mapping)
            else {}
        )
        data_asof = _as_text(primary.get("snapshotDate")) or _as_text(
            primary.get("computedAt")
        )
        fetched_at = _parse_fetched_at(
            freshness.get("fetchedAt") if isinstance(freshness, Mapping) else None
        )
        return _row(
            call_id=call_id,
            recorded_at=recorded_at,
            recorded_at_kst=recorded_at_kst,
            market=market,
            tool=TOOL_SCREEN_STOCKS_SNAPSHOT,
            source=f"{TOOL_SCREEN_STOCKS_SNAPSHOT}:{preset}",
            family="screener_snapshot",
            kind="snapshot_preset",
            symbol=symbol,
            # The merged row keeps only the first matched preset's rank.
            rank=_as_int(item.get("rank")) if first else None,
            price_text=_label_price_text(item.get("priceLabel")),
            sort_by=sort_by,
            sort_order=None,
            limit=limit,
            preset=preset,
            admission=admission,
            admission_reason=reason,
            selection_seq=seq,
            data_asof=data_asof,
            fetched_at=fetched_at,
            raw_row=_object(item),
            source_params={
                "request": _request_context(request),
                "preset": preset,
                "kind": "snapshot_preset",
            },
            call_context=call_context,
            code_sha256=digest,
        )

    rows: list[ScreenerPickRow] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(results):
        if not isinstance(item, Mapping):
            continue
        symbol = _symbol_key(market, item.get("symbol"))
        if symbol is None:
            continue
        for position, preset in enumerate(sources_for(item)):
            if (preset, symbol) in seen:
                continue
            seen.add((preset, symbol))
            rows.append(
                build(
                    item,
                    preset=preset,
                    first=position == 0,
                    symbol=symbol,
                    admission=ADMITTED,
                    reason=REASON_RETURNED,
                    seq=offset + index + 1,
                )
            )
    for item, reason in dropped_rows:
        if not isinstance(item, Mapping):
            continue
        symbol = _symbol_key(market, item.get("symbol"))
        if symbol is None:
            continue
        for position, preset in enumerate(sources_for(item)):
            if (preset, symbol) in seen:
                continue
            seen.add((preset, symbol))
            rows.append(
                build(
                    item,
                    preset=preset,
                    first=position == 0,
                    symbol=symbol,
                    admission=NOT_ADMITTED,
                    reason=reason,
                    seq=None,
                )
            )
    return rows


async def _write_and_tag(
    result: Mapping[str, Any],
    rows: list[ScreenerPickRow],
    call_id: uuid.UUID,
    write_rows: WriteRows | None,
) -> dict[str, Any] | None:
    if not rows:
        return None
    await (write_rows or _default_write)(rows)
    return {**result, "pick_log_call_id": str(call_id)}


async def record_screen_stocks_picks(
    result: Any,
    *,
    request: Mapping[str, Any],
    enabled: bool | None = None,
    write_rows: WriteRows | None = None,
    now: datetime | None = None,
    code_sha256: str | None = None,
) -> Any:
    """Record a ``screen_stocks`` return; return it unchanged unless rows were written.

    On success the result gains the optional top-level ``pick_log_call_id``. Gate off,
    nothing to record, or any recorder failure returns ``result`` as-is (fail-open).
    """

    try:
        if enabled is None:
            enabled = env_gate_enabled()
        if not enabled or not isinstance(result, Mapping):
            return result
        call_id = uuid.uuid4()
        rows = extract_screen_stocks_rows(
            result,
            request=request,
            call_id=call_id,
            now=now,
            code_sha256=code_sha256,
        )
        tagged = await _write_and_tag(result, rows, call_id, write_rows)
        return result if tagged is None else tagged
    except Exception:
        logger.warning(
            "screener tool pick log failed; screen_stocks result is unchanged",
            exc_info=True,
        )
        return result


async def record_snapshot_picks(
    payload: Any,
    *,
    request: Mapping[str, Any],
    observation: Mapping[str, Any],
    enabled: bool | None = None,
    write_rows: WriteRows | None = None,
    now: datetime | None = None,
    code_sha256: str | None = None,
) -> Any:
    """Record a ``screen_stocks_snapshot`` return; same contract as above.

    ``observation`` is the sink ``screen_stocks_snapshot_impl`` filled in
    (``dropped_rows`` / ``freshness_by_preset`` / ``preset_ids``). It is empty when
    the call short-circuited with an error, in which case nothing is recorded.
    """

    try:
        if enabled is None:
            enabled = env_gate_enabled()
        if not enabled or not isinstance(payload, Mapping) or not observation:
            return payload
        call_id = uuid.uuid4()
        rows = extract_snapshot_rows(
            payload,
            request=request,
            dropped_rows=copy.copy(list(observation.get("dropped_rows") or [])),
            freshness_by_preset=observation.get("freshness_by_preset") or {},
            preset_ids=list(observation.get("preset_ids") or []),
            call_id=call_id,
            now=now,
            code_sha256=code_sha256,
        )
        tagged = await _write_and_tag(payload, rows, call_id, write_rows)
        return payload if tagged is None else tagged
    except Exception:
        logger.warning(
            "screener tool pick log failed; snapshot result is unchanged",
            exc_info=True,
        )
        return payload


__all__ = [
    "COLLECTION_VERSION",
    "extract_screen_stocks_rows",
    "extract_snapshot_rows",
    "record_screen_stocks_picks",
    "record_snapshot_picks",
    "tool_code_sha256",
]
