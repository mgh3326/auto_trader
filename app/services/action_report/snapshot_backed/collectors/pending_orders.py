"""ROB-274 — pending_orders snapshot collector (read-only).

One collector kind with internal market/account adapters:

* ``market=kr``, ``account_scope=kis_live`` → KIS domestic open orders via
  :meth:`KISClient.inquire_korea_orders`.
* ``market=us``, ``account_scope=kis_live`` → KIS overseas open orders via
  :meth:`KISClient.inquire_overseas_orders` (iterated over NASD/NYSE/AMEX).
* ``market=crypto``, ``account_scope=upbit_live`` → Upbit open orders via
  the Upbit broker module's ``fetch_open_orders``.

The collector never calls broker submit/cancel/modify methods. Missing
broker clients or fetch failures produce an ``unavailable`` result with
``errors_json.reason`` and do NOT raise — downstream classifier maps
this to ``action/review`` with ``확인 불가`` rationale.

Output schema (each ``pending_orders[i]``)::

    {
        "target_ref": {"type": "broker_order", "broker": "kis"|"upbit",
                       "id": str, "raw": dict},
        "symbol": str | None,
        "side": "buy" | "sell" | "unknown",
        "price": str | None,
        "quantity": str | None,
        "remaining_quantity": str | None,
        "placed_at": str | None,      # ISO 8601 (KIS rows in KST; Upbit in UTC)
        "stale": bool,                # crypto-only; KR/US always False
        "market": "kr" | "us" | "crypto",
    }
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any, Protocol

from app.services.action_report.common.staleness import (
    is_crypto_pending_order_stale,
)
from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.brokers.kis.live_order_expiry import kr_day_order_expiry
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)

_KIS_US_EXCHANGES: tuple[str, ...] = ("NASD", "NYSE", "AMEX")

# KIS returns ord_dt/ord_tmd in Korea Standard Time (UTC+9) for both domestic
# and overseas endpoints. Stamping the parsed datetime as KST keeps the
# wall-clock semantics correct; downstream comparisons across tz-aware
# datetimes still work because Python normalizes across zones.
_KST = dt.timezone(dt.timedelta(hours=9), name="KST")


class _KISClientProtocol(Protocol):
    async def inquire_korea_orders(
        self, is_mock: bool = False
    ) -> list[dict[str, Any]]: ...

    async def inquire_overseas_orders(
        self,
        exchange_code: str = "NASD",
        is_mock: bool = False,
    ) -> list[dict[str, Any]]: ...


class _UpbitClientProtocol(Protocol):
    async def fetch_open_orders(
        self, market: str | None = None
    ) -> list[dict[str, Any]]: ...


class PendingOrdersSnapshotCollector:
    """Read-only collector for KR/US/crypto pending broker orders."""

    snapshot_kind: str = "pending_orders"

    def __init__(
        self,
        *,
        kis_client: _KISClientProtocol | None,
        upbit_client: _UpbitClientProtocol | None,
    ) -> None:
        self._kis = kis_client
        self._upbit = upbit_client

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        market = request.market
        if market == "kr":
            return [await self._collect_kis_kr(now=now, request=request)]
        if market == "us":
            return [await self._collect_kis_us(now=now, request=request)]
        if market == "crypto":
            return [await self._collect_upbit(now=now, request=request)]
        return [
            unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market=market,
                account_scope=request.account_scope,
                origin="auto_trader_db",
                reason=f"unsupported_market:{market}",
                as_of=now,
            )
        ]

    async def _collect_kis_kr(
        self, *, now: dt.datetime, request: CollectorRequest
    ) -> SnapshotCollectResult:
        if self._kis is None:
            return unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market="kr",
                account_scope=request.account_scope,
                origin="kis_api",
                reason="kis_client_unavailable",
                as_of=now,
            )
        try:
            raw = await self._kis.inquire_korea_orders(is_mock=False)
        except Exception as exc:  # noqa: BLE001 — collector must fail open
            return unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market="kr",
                account_scope=request.account_scope,
                origin="kis_api",
                reason=f"kis_fetch_failed:{type(exc).__name__}:{exc}",
                as_of=now,
            )
        normalized = [_normalize_kis_order(row, market="kr") for row in raw or []]
        return build_result(
            snapshot_kind=self.snapshot_kind,
            market="kr",
            account_scope=request.account_scope,
            payload={"pending_orders": normalized, "count": len(normalized)},
            origin="kis_api",
            as_of=now,
            coverage={"count": len(normalized)},
        )

    async def _collect_kis_us(
        self, *, now: dt.datetime, request: CollectorRequest
    ) -> SnapshotCollectResult:
        if self._kis is None:
            return unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market="us",
                account_scope=request.account_scope,
                origin="kis_api",
                reason="kis_client_unavailable",
                as_of=now,
            )
        all_rows: list[dict[str, Any]] = []
        exchange_errors: dict[str, str] = {}
        # KIS returns the same `odno` under each exchange query when the
        # order's exchange field doesn't filter to one of NASD/NYSE/AMEX
        # explicitly. Dedupe by order id to keep the snapshot tidy.
        seen_ids: set[str] = set()
        for exchange_code in _KIS_US_EXCHANGES:
            try:
                rows = await self._kis.inquire_overseas_orders(
                    exchange_code=exchange_code, is_mock=False
                )
            except Exception as exc:  # noqa: BLE001 — collector must fail open
                exchange_errors[exchange_code] = f"{type(exc).__name__}:{exc}"
                continue
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                order_id = str(row.get("odno") or row.get("ord_no") or "")
                if order_id and order_id in seen_ids:
                    continue
                if order_id:
                    seen_ids.add(order_id)
                all_rows.append(row)
        if not all_rows and exchange_errors:
            reason = "kis_fetch_failed:" + ";".join(
                f"{code}:{msg}" for code, msg in exchange_errors.items()
            )
            return unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market="us",
                account_scope=request.account_scope,
                origin="kis_api",
                reason=reason,
                as_of=now,
            )
        normalized = [_normalize_kis_order(row, market="us") for row in all_rows]
        return build_result(
            snapshot_kind=self.snapshot_kind,
            market="us",
            account_scope=request.account_scope,
            payload={"pending_orders": normalized, "count": len(normalized)},
            origin="kis_api",
            as_of=now,
            coverage={"count": len(normalized), "exchange_errors": exchange_errors},
        )

    async def _collect_upbit(
        self, *, now: dt.datetime, request: CollectorRequest
    ) -> SnapshotCollectResult:
        if self._upbit is None:
            return unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market="crypto",
                account_scope=request.account_scope,
                origin="upbit_mcp",
                reason="upbit_client_unavailable",
                as_of=now,
            )
        try:
            raw = await self._upbit.fetch_open_orders()
        except Exception as exc:  # noqa: BLE001 — collector must fail open
            return unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market="crypto",
                account_scope=request.account_scope,
                origin="upbit_mcp",
                reason=f"upbit_fetch_failed:{type(exc).__name__}:{exc}",
                as_of=now,
            )
        normalized = [_normalize_upbit_order(row, now=now) for row in raw or []]
        return build_result(
            snapshot_kind=self.snapshot_kind,
            market="crypto",
            account_scope=request.account_scope,
            payload={"pending_orders": normalized, "count": len(normalized)},
            origin="upbit_mcp",
            as_of=now,
            coverage={"count": len(normalized)},
        )


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

# KIS uses `01`=sell, `02`=buy. Some callers may pass already-mapped values.
_KIS_SIDE_BUY = {"02", "buy", "b", "매수"}
_KIS_SIDE_SELL = {"01", "sell", "s", "매도"}


def _kis_expected_expiry(
    placed_at: dt.datetime | None, *, market: str, side: str
) -> tuple[str | None, str | None]:
    """(ISO expiry, categorical reason) for a KR pending order via the shared helper.

    ROB-671: delegates to the single stdlib-only computer so the collector and
    the send-path (kis_live_ledger) agree. US/crypto have no NXT session → None.
    The downgrade flag is left off here (the collector is a read surface, not a
    live TTL decision); by conservative default the value stays 20:00 KST.
    """
    if market != "kr" or placed_at is None:
        return None, None
    return kr_day_order_expiry(accepted_at=placed_at, side=side)


def _normalize_kis_order(row: dict[str, Any], *, market: str) -> dict[str, Any]:
    order_id = _first_str(row, ("ord_no", "odno", "order_id"))
    symbol = _first_str(row, ("pdno", "symbol", "ticker"))
    price = _first_str(
        row,
        ("ord_unpr", "ft_ord_unpr3", "ord_unpr3", "price"),
    )
    quantity = _first_str(row, ("ord_qty", "ft_ord_qty", "quantity", "qty"))
    remaining_raw = _first_str(
        row,
        ("nccs_qty", "rmn_qty", "remaining_qty", "remaining_quantity"),
    )
    # KR domestic ``inquire_korea_orders`` returns rows that are already
    # cancellable (pending), so remaining defaults to ord_qty.
    if remaining_raw is None:
        remaining_raw = quantity
    placed_at = _kis_placed_at(row)
    side = _normalize_kis_side(row)
    expiry_iso, expiry_reason = _kis_expected_expiry(
        placed_at, market=market, side=side
    )
    return {
        "target_ref": {
            "type": "broker_order",
            "broker": "kis",
            "id": order_id or "",
            "raw": dict(row),
        },
        "symbol": symbol,
        "side": side,
        "price": price,
        "quantity": quantity,
        "remaining_quantity": remaining_raw,
        "placed_at": placed_at.isoformat() if placed_at is not None else None,
        "expected_expiry": expiry_iso,
        "expiry_reason": expiry_reason,
        # KR/US use session expiry handled by the broker; classifier handles
        # session-based gating, so the collector never flags stale here.
        "stale": False,
        "market": market,
    }


def _normalize_kis_side(row: dict[str, Any]) -> str:
    raw = (
        str(
            row.get("sll_buy_dvsn_cd")
            or row.get("sll_buy_dvsn_cd_name")
            or row.get("side")
            or ""
        )
        .strip()
        .lower()
    )
    if raw in _KIS_SIDE_BUY:
        return "buy"
    if raw in _KIS_SIDE_SELL:
        return "sell"
    return "unknown"


def _kis_placed_at(row: Mapping[str, Any]) -> dt.datetime | None:
    explicit = _coerce_datetime(row.get("placed_at"))
    if explicit is not None:
        return explicit
    ord_dt = row.get("ord_dt")
    ord_tmd = row.get("ord_tmd")
    if ord_dt and ord_tmd:
        combined = f"{ord_dt}{ord_tmd}"
        try:
            # KIS returns these wall-clock fields in KST (UTC+9) for both
            # domestic and overseas endpoints — stamping as KST keeps the
            # timestamp factually correct.
            return dt.datetime.strptime(combined, "%Y%m%d%H%M%S").replace(tzinfo=_KST)
        except ValueError:
            return None
    return None


def _normalize_upbit_order(row: dict[str, Any], *, now: dt.datetime) -> dict[str, Any]:
    placed_at_raw = row.get("created_at") or row.get("placed_at")
    placed_at = _coerce_datetime(placed_at_raw)
    stale = bool(placed_at and is_crypto_pending_order_stale(placed_at, now=now))
    side_raw = str(row.get("side") or "").strip().lower()
    if side_raw == "bid":
        side = "buy"
    elif side_raw == "ask":
        side = "sell"
    else:
        side = side_raw or "unknown"
    return {
        "target_ref": {
            "type": "broker_order",
            "broker": "upbit",
            "id": str(row.get("uuid") or ""),
            "raw": dict(row),
        },
        "symbol": row.get("market"),
        "side": side,
        "price": _stringify_optional(row.get("price")),
        "quantity": _stringify_optional(row.get("volume")),
        "remaining_quantity": _stringify_optional(row.get("remaining_volume")),
        "placed_at": placed_at.isoformat() if placed_at is not None else None,
        "expected_expiry": None,
        "expiry_reason": None,
        "stale": stale,
        "market": "crypto",
    }


def _first_str(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _stringify_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None
