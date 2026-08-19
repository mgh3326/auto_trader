"""Compact 1-message formatter (ROB-1297, §78 card compression).

Holiday → empty string (caller must not send).
Zero fills → exactly one line.
Otherwise a short multi-line card, never a long-form write-up.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.market_close_digest.types import DigestSnapshot, LedgerFill, Market
from app.telegram_contract import TELEGRAM_SEND_MESSAGE_TEXT_LIMIT, telegram_text_length

_MARKET_LABEL: dict[Market, str] = {
    "us": "US",
    "kr": "KR",
    "crypto": "crypto",
}
_MAX_FILLS_LISTED = 8


def format_digest_message(snapshot: DigestSnapshot) -> str:
    if snapshot.status == "skipped_holiday":
        return ""
    label = _MARKET_LABEL[snapshot.market]
    day = snapshot.session_date.isoformat()
    if snapshot.fill_count == 0:
        return f"{label} 마감 {day} · 체결 0건"

    lines = [
        f"{label} 마감 {day}",
        (
            f"체결 {snapshot.fill_count}건 · 매도 {snapshot.sell_count}"
            f" · 매수 {snapshot.buy_count}"
        ),
    ]
    sell_line = _join_fills(snapshot.sells, prefix="매도")
    if sell_line:
        lines.append(sell_line)
    buy_line = _join_fills(snapshot.buys, prefix="매수")
    if buy_line:
        lines.append(buy_line)
    lines.append(
        f"신규매수 {snapshot.buy_count} · 순매수 {_fmt_net(snapshot.net_notional)}"
    )
    lines.append(f"자동승인 {snapshot.auto_approve_count} · 카드 {snapshot.card_count}")
    if snapshot.oversell_blocked:
        symbols = ",".join(block.symbol for block in snapshot.oversell_blocked)
        lines.append(f"차단 오버셀 {len(snapshot.oversell_blocked)} ({symbols})")
    for flag in snapshot.flags:
        lines.append(f"개선: {flag}")
    message = "\n".join(lines)
    if telegram_text_length(message) > TELEGRAM_SEND_MESSAGE_TEXT_LIMIT:
        message = message[: TELEGRAM_SEND_MESSAGE_TEXT_LIMIT - 1] + "…"
    return message


def _join_fills(fills: tuple[LedgerFill, ...], *, prefix: str) -> str | None:
    if not fills:
        return None
    listed = fills[:_MAX_FILLS_LISTED]
    body = " · ".join(_fmt_fill(fill) for fill in listed)
    extra = len(fills) - len(listed)
    if extra > 0:
        body = f"{body} · +{extra}"
    return f"{prefix} {body}"


def _fmt_fill(fill: LedgerFill) -> str:
    parts = [fill.symbol]
    pnl = _fmt_pnl(fill.pnl, fill.pnl_currency)
    if pnl:
        parts.append(pnl)
    pct = _fmt_pct(fill.pnl_pct)
    if pct:
        parts.append(pct)
    return " ".join(parts)


def _fmt_pnl(pnl: Decimal | None, currency: str | None) -> str | None:
    if pnl is None:
        return None
    sign = "+" if pnl >= 0 else ""
    quantized = pnl.quantize(Decimal("0.01"))
    unit = "$" if (currency or "").upper() in {"USD", ""} else str(currency)
    if (currency or "").upper() == "KRW":
        return f"{sign}{int(pnl):,}원"
    return f"{sign}{quantized}{unit}"


def _fmt_pct(pct: Decimal | None) -> str | None:
    if pct is None:
        return None
    sign = "+" if pct >= 0 else ""
    return f"({sign}{pct.quantize(Decimal('0.1'))}%)"


def _fmt_net(net: Decimal) -> str:
    sign = "+" if net >= 0 else ""
    quantized = net.quantize(Decimal("0.01"))
    return f"{sign}{quantized}"
