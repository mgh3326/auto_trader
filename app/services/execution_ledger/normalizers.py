"""Pure filled-order normalizers and execution-ledger adapters (ROB-211)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.timezone import KST, trade_day_kst
from app.schemas.execution_ledger import ExecutionLedgerUpsert

SENSITIVE_KEY_MARKERS = (
    "secret",
    "token",
    "authorization",
    "auth",
    "api_key",
    "apikey",
    "app_key",
    "app_secret",
)

# Upbit order states that represent at least a partial execution
UPBIT_FILL_STATES: frozenset[str] = frozenset({"done", "cancel"})
# PostgreSQL Integer columns are signed int32; hash-derived fill_seq values must
# stay inside that range or classify/upsert lookups fail before any commit.
MAX_SQL_INT32 = 2_147_483_647


def _stable_int32_hash(seed: str) -> int:
    return int(hashlib.sha256(str(seed).encode()).hexdigest()[:8], 16) & MAX_SQL_INT32


def _strip_crypto_prefix(symbol: str) -> str:
    upper = str(symbol or "").strip().upper()
    for prefix in ("KRW-", "USDT-"):
        if upper.startswith(prefix):
            return upper[len(prefix) :]
    return upper


def _to_decimal(value: object, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except Exception:
        return Decimal(default)


def _parse_kis_order_timestamp(ord_dt: object, ord_tmd: object) -> datetime:
    date_text = str(ord_dt or "").strip()
    time_text = str(ord_tmd or "000000").strip()
    if len(date_text) != 8 or not date_text.isdigit():
        raise ValueError("filled_at is empty and ord_dt is invalid")
    if len(time_text) < 6 or not time_text[:6].isdigit():
        raise ValueError("filled_at is empty and ord_tmd is invalid")
    try:
        return datetime.strptime(
            f"{date_text} {time_text[:6]}", "%Y%m%d %H%M%S"
        ).replace(tzinfo=KST)
    except ValueError as exc:
        raise ValueError(
            "filled_at is empty and KIS order timestamp is invalid"
        ) from exc


def _parse_filled_at(
    value: object,
    *,
    ord_dt: object | None = None,
    ord_tmd: object | None = None,
) -> datetime:
    text = str(value or "").strip()
    if not text:
        parsed = _parse_kis_order_timestamp(ord_dt, ord_tmd)
    else:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            if len(text) == 8 and text.isdigit():
                try:
                    parsed = datetime.strptime(text, "%Y%m%d").replace(tzinfo=KST)
                except ValueError as exc:
                    raise ValueError(f"invalid filled_at: {text!r}") from exc
            else:
                raise ValueError(f"invalid filled_at: {text!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    else:
        parsed = parsed.astimezone(KST)
    if ord_dt not in (None, "") and trade_day_kst(parsed) != str(ord_dt).strip():
        raise ValueError(
            "KST trade day mismatch: "
            f"filled_at={trade_day_kst(parsed)} ord_dt={str(ord_dt).strip()}"
        )
    return parsed


def _redact_sensitive_keys(payload: Any) -> Any:
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in SENSITIVE_KEY_MARKERS):
                out[key] = "[REDACTED]"
            else:
                out[key] = _redact_sensitive_keys(value)
        return out
    if isinstance(payload, list):
        return [_redact_sensitive_keys(v) for v in payload]
    if isinstance(payload, str) and (
        "Bearer " in payload or "KIS" in payload and len(payload) > 40
    ):
        return "[REDACTED]"
    return payload


def _upbit_trade_fill_seq(trade_uuid: str) -> int:
    """Stable fill_seq derived from Upbit trade uuid (SHA-256 truncated)."""
    return _stable_int32_hash(str(trade_uuid))


def _normalize_upbit_filled(order: dict[str, Any]) -> dict[str, Any] | None:
    """Return a single aggregate fill for one Upbit order, or None to skip.

    Accepts both 'done' and 'cancel' states; a cancelled order with executed
    volume represents a real (partial) fill that must be preserved.
    """
    if order.get("state") not in UPBIT_FILL_STATES:
        return None
    executed_vol = float(order.get("executed_volume") or 0)
    if executed_vol <= 0:
        return None
    price = float(order.get("price") or order.get("avg_price") or 0)
    if price <= 0 and order.get("trades"):
        trades = order.get("trades") or []
        total_funds = sum(float(t.get("funds") or 0) for t in trades)
        total_vol = sum(float(t.get("volume") or 0) for t in trades)
        price = total_funds / total_vol if total_vol else 0
    total = price * executed_vol
    raw_symbol = str(order.get("market", ""))
    side_raw = str(order.get("side", "")).lower()
    return {
        "symbol": _strip_crypto_prefix(raw_symbol),
        "raw_symbol": raw_symbol,
        "instrument_type": "crypto",
        "side": "buy" if side_raw == "bid" else "sell",
        "price": price,
        "quantity": executed_vol,
        "total_amount": total,
        "fee": float(order.get("paid_fee") or 0),
        "currency": "KRW",
        "account": "upbit",
        "order_id": str(order.get("uuid", "")),
        "filled_at": str(order.get("created_at", "")),
        "fill_seq": 0,
        "venue": "upbit_krw",
        "raw_payload_json": _redact_sensitive_keys(order),
    }


def normalize_upbit_order(order: dict[str, Any]) -> list[dict[str, Any]]:  # NOSONAR
    """Normalize one Upbit order into 0-N per-trade fill dicts.

    When the order detail includes individual trades, each trade becomes a
    separate fill row with a stable fill_seq derived from the trade uuid.
    When trades are absent (detail not fetched yet), a single aggregate fill
    with fill_seq=0 is returned so callers always get a list.
    """
    if order.get("state") not in UPBIT_FILL_STATES:
        return []
    executed_vol = float(order.get("executed_volume") or 0)
    if executed_vol <= 0:
        return []

    raw_symbol = str(order.get("market", ""))
    symbol = _strip_crypto_prefix(raw_symbol)
    side_raw = str(order.get("side", "")).lower()
    side = "buy" if side_raw == "bid" else "sell"
    order_id = str(order.get("uuid", ""))
    paid_fee = float(order.get("paid_fee") or 0)
    redacted = _redact_sensitive_keys(order)

    trades = [t for t in (order.get("trades") or []) if float(t.get("volume") or 0) > 0]

    if trades:
        fills: list[dict[str, Any]] = []
        for trade in trades:
            trade_vol = float(trade.get("volume") or 0)
            trade_funds = float(trade.get("funds") or 0)
            if trade_vol <= 0:
                continue
            trade_price = trade_funds / trade_vol if trade_vol else 0
            if trade_price <= 0:
                continue
            # Proportionally allocate order-level fee across trades by volume
            trade_fee = (trade_vol / executed_vol) * paid_fee if executed_vol else 0
            fills.append(
                {
                    "symbol": symbol,
                    "raw_symbol": raw_symbol,
                    "instrument_type": "crypto",
                    "side": side,
                    "price": trade_price,
                    "quantity": trade_vol,
                    "total_amount": trade_funds,
                    "fee": trade_fee,
                    "currency": "KRW",
                    "account": "upbit",
                    "order_id": order_id,
                    "filled_at": str(
                        trade.get("created_at") or order.get("created_at", "")
                    ),
                    "fill_seq": _upbit_trade_fill_seq(str(trade.get("uuid", ""))),
                    "venue": "upbit_krw",
                    "raw_payload_json": redacted,
                }
            )
        return fills

    # No trade detail: fall back to aggregate fill (fill_seq=0)
    price = float(order.get("price") or order.get("avg_price") or 0)
    if price <= 0:
        return []
    return [
        {
            "symbol": symbol,
            "raw_symbol": raw_symbol,
            "instrument_type": "crypto",
            "side": side,
            "price": price,
            "quantity": executed_vol,
            "total_amount": price * executed_vol,
            "fee": paid_fee,
            "currency": "KRW",
            "account": "upbit",
            "order_id": order_id,
            "filled_at": str(order.get("created_at", "")),
            "fill_seq": 0,
            "venue": "upbit_krw",
            "raw_payload_json": redacted,
        }
    ]


def _kis_datetime(ord_dt: str, ord_tmd: str) -> str:
    filled_at_str = ord_dt
    if len(ord_dt) == 8 and len(ord_tmd) >= 6:
        try:
            dt = datetime.strptime(f"{ord_dt} {ord_tmd[:6]}", "%Y%m%d %H%M%S")
            filled_at_str = dt.replace(tzinfo=KST).isoformat()
        except ValueError:
            pass
    return filled_at_str


def _domestic_fill_seq(order: dict[str, Any]) -> int:
    raw = order.get("ccld_seq") or order.get("ccld_seq_no") or order.get("odno_seq")
    if raw not in (None, ""):
        try:
            return int(raw)
        except ValueError:
            pass
    seed = "|".join(
        str(order.get(k, "")) for k in ("ord_dt", "ord_tmd", "ccld_tmd", "ccld_qty")
    )
    return _stable_int32_hash(seed)


def _overseas_fill_seq(order: dict[str, Any]) -> int:
    """Stable fill_seq for KIS overseas orders.

    Prefers ccld_seq / ccld_seq_no from the broker response.  When the field
    is absent or zero (common for non-US markets or single-fill orders), falls
    back to a SHA-1 hash of the order's key fields so that multiple fills for
    the same order_id do not collide on fill_seq=0.
    """
    raw = order.get("ccld_seq") or order.get("ccld_seq_no")
    if raw not in (None, ""):
        try:
            return int(raw)
        except ValueError:
            pass
    seed = "|".join(
        str(order.get(k, "")) for k in ("ord_dt", "ord_tmd", "ft_ccld_qty", "odno")
    )
    return _stable_int32_hash(seed)


def _normalize_kis_domestic_filled(order: dict[str, Any]) -> dict[str, Any] | None:
    qty = float(order.get("ccld_qty") or order.get("tot_ccld_qty") or 0)
    if qty <= 0:
        return None
    price = float(order.get("ccld_unpr") or order.get("avg_prvs") or 0)
    total = float(order.get("ccld_amt") or order.get("tot_ccld_amt") or price * qty)
    ord_dt = str(order.get("ord_dt", ""))
    ord_tmd = str(order.get("ord_tmd") or order.get("ccld_tmd") or "000000")
    symbol = str(order.get("pdno") or order.get("stck_code") or "").strip()
    side_code = str(order.get("sll_buy_dvsn_cd") or "")
    return {
        "symbol": symbol,
        "raw_symbol": symbol,
        "instrument_type": "equity_kr",
        "side": "sell" if side_code == "01" else "buy",
        "price": price,
        "quantity": qty,
        "total_amount": total,
        "fee": float(order.get("fee") or order.get("tot_fee") or 0),
        "currency": "KRW",
        "account": "kis",
        "order_id": str(order.get("ord_no") or order.get("odno") or ""),
        "filled_at": _kis_datetime(ord_dt, ord_tmd),
        "ord_dt": ord_dt,
        "ord_tmd": ord_tmd,
        "fill_seq": _domestic_fill_seq(order),
        "venue": "krx",
        "raw_payload_json": _redact_sensitive_keys(order),
    }


def _normalize_kis_overseas_filled(order: dict[str, Any]) -> dict[str, Any] | None:
    qty = float(order.get("ft_ccld_qty") or order.get("ccld_qty") or 0)
    if qty <= 0:
        return None
    price = float(order.get("ft_ccld_unpr3") or order.get("ccld_unpr") or 0)
    total = float(order.get("ft_ccld_amt3") or order.get("ccld_amt") or price * qty)
    ord_dt = str(order.get("ord_dt", ""))
    ord_tmd = str(order.get("ord_tmd") or "000000")
    symbol = str(order.get("pdno") or order.get("symb") or "").strip()
    return {
        "symbol": symbol,
        "raw_symbol": symbol,
        "instrument_type": "equity_us",
        "side": "sell" if str(order.get("sll_buy_dvsn_cd", "")) == "01" else "buy",
        "price": price,
        "quantity": qty,
        "total_amount": total,
        "fee": float(order.get("fee") or order.get("tot_fee") or 0),
        "currency": "USD",
        "account": "kis_overseas",
        "order_id": str(order.get("odno") or order.get("ord_no") or ""),
        "filled_at": _kis_datetime(ord_dt, ord_tmd),
        "ord_dt": ord_dt,
        "ord_tmd": ord_tmd,
        "fill_seq": _overseas_fill_seq(order),
        "venue": str(order.get("ovrs_excg_cd") or order.get("excg_cd") or "NASD"),
        "raw_payload_json": _redact_sensitive_keys(order),
    }


def to_execution_ledger_upsert(
    normalized: dict[str, Any],
    *,
    broker: str | None = None,
    account_mode: str = "live",
    source: str = "reconciler",
    correlation_id: str | None = None,
    source_run_id: uuid.UUID | None = None,
) -> ExecutionLedgerUpsert:
    broker_value = broker or (
        "upbit" if normalized.get("account") == "upbit" else "kis"
    )
    venue = str(
        normalized.get("venue") or ("upbit_krw" if broker_value == "upbit" else "krx")
    )
    return ExecutionLedgerUpsert(
        broker=broker_value,
        account_mode=account_mode,
        venue=venue,
        instrument_type=normalized["instrument_type"],
        symbol=normalized["symbol"],
        raw_symbol=normalized.get("raw_symbol") or normalized["symbol"],
        side=normalized["side"],
        broker_order_id=normalized["order_id"],
        fill_seq=int(normalized.get("fill_seq") or 0),
        filled_qty=_to_decimal(normalized["quantity"]),
        filled_price=_to_decimal(normalized["price"]),
        filled_notional=_to_decimal(normalized.get("total_amount")),
        fee_amount=_to_decimal(normalized.get("fee"))
        if normalized.get("fee") is not None
        else None,
        fee_currency=normalized.get("currency"),
        filled_at=_parse_filled_at(
            normalized.get("filled_at"),
            ord_dt=normalized.get("ord_dt"),
            ord_tmd=normalized.get("ord_tmd"),
        ),
        currency=normalized["currency"],
        correlation_id=correlation_id or normalized.get("correlation_id"),
        source=source,
        source_run_id=source_run_id,
        raw_payload_json=normalized.get("raw_payload_json"),
    )
