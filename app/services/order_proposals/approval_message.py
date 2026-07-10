"""Pure Telegram approval-message and callback-data builders."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.timezone import KST

_ALLOWED_ACTIONS = frozenset({"op", "dn"})
_CALLBACK_PATTERN = re.compile(
    r"^(?P<action>op|dn):(?P<proposal_short>[0-9a-f]{8}):"
    r"(?P<nonce>[A-Za-z0-9_-]+)$"
)
_MAX_CALLBACK_BYTES = 64
_SENSITIVE_KEY_PARTS = ("hash", "nonce", "digest")
_CASH_LABELS = (
    ("available_cash", "가용현금"),
    ("required_cash", "필요현금"),
    ("remaining_cash", "잔여현금"),
    ("buffer_cash", "현금 버퍼"),
    ("utilization_pct", "사용률"),
)


def build_callback_data(
    *,
    action: str,
    proposal_id: uuid.UUID,
    nonce: str,
) -> str:
    """Build compact Telegram callback data for an approval action."""
    if action not in _ALLOWED_ACTIONS:
        raise ValueError("action must be one of: op, dn")
    if not isinstance(proposal_id, uuid.UUID):
        raise ValueError("proposal_id must be a UUID")
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", nonce):
        raise ValueError("nonce must be a non-empty URL-safe token")

    data = f"{action}:{str(proposal_id)[:8]}:{nonce}"
    if len(data.encode("utf-8")) > _MAX_CALLBACK_BYTES:
        raise ValueError("callback data must not exceed 64 bytes")
    return data


def parse_callback_data(data: str) -> tuple[str, str, str]:
    """Parse and validate compact Telegram approval callback data."""
    if not isinstance(data, str) or len(data.encode("utf-8")) > _MAX_CALLBACK_BYTES:
        raise ValueError("malformed callback data")
    match = _CALLBACK_PATTERN.fullmatch(data)
    if match is None:
        raise ValueError("malformed callback data")
    return (
        match.group("action"),
        match.group("proposal_short"),
        match.group("nonce"),
    )


def build_approval_message(
    *,
    group: Any,
    rungs: Sequence[Any],
    cash_stress: dict | None = None,
    diff: dict | None = None,
) -> tuple[str, dict]:
    """Render a proposal and its Telegram inline keyboard without raw digests."""
    nonce = getattr(group, "approval_nonce", None)
    if not nonce:
        raise ValueError("group.approval_nonce is required")

    proposal_id = getattr(group, "proposal_id", None)
    approve_data = build_callback_data(
        action="op",
        proposal_id=proposal_id,
        nonce=nonce,
    )
    deny_data = build_callback_data(
        action="dn",
        proposal_id=proposal_id,
        nonce=nonce,
    )

    market = str(getattr(group, "market", "") or "미기재")
    symbol = str(getattr(group, "symbol", "") or "미기재")
    side = str(getattr(group, "side", "") or "미기재")
    order_type = str(getattr(group, "order_type", "") or "미기재")
    currency = _currency_for_market(market=market, symbol=symbol)
    quantity_unit = "주" if market in {"equity_kr", "equity_us"} else ""
    sorted_rungs = sorted(rungs, key=lambda rung: getattr(rung, "rung_index", 0))

    title = "*주문 제안 재확인*" if diff else "*주문 제안 승인*"
    lines = [
        title,
        f"- 종목: `{_escape_markdown(symbol)}`",
        f"- 시장/방향/유형: `{market} / {side} / {order_type}`",
        "",
        "*주문 단계*",
    ]
    if sorted_rungs:
        for rung in sorted_rungs:
            display_index = int(getattr(rung, "rung_index", 0)) + 1
            quantity = _format_decimal(getattr(rung, "quantity", None))
            price = _format_money(
                getattr(rung, "limit_price", None),
                currency=currency,
                none_label="시장가",
            )
            lines.append(f"- #{display_index}: {quantity}{quantity_unit} × {price}")
    else:
        lines.append("- 없음")

    thesis = getattr(group, "thesis", None) or "미기재"
    strategy = getattr(group, "strategy", None) or "미기재"
    lines.extend(
        [
            "",
            "*근거*",
            f"- 투자 논지: {_escape_markdown(thesis)}",
            f"- 전략: {_escape_markdown(strategy)}",
        ]
    )

    time_lines = _build_time_lines(group)
    if time_lines:
        lines.extend(["", "*시간*", *time_lines])

    if cash_stress:
        cash_lines = _build_cash_lines(cash_stress, currency=currency)
        if cash_lines:
            lines.extend(["", "*현금 스트레스*", *cash_lines])

    if diff:
        before = diff.get("before") if isinstance(diff, Mapping) else None
        after = diff.get("after") if isinstance(diff, Mapping) else None
        lines.extend(
            [
                "",
                "*재확인 변경사항*",
                f"- 변경 전: {_format_diff_side(before, currency=currency)}",
                f"- 변경 후: {_format_diff_side(after, currency=currency)}",
            ]
        )

    text = "\n".join(lines)
    secrets = [
        getattr(group, "payload_hash", None),
        getattr(group, "approval_hash", None),
        nonce,
        *(getattr(rung, "approval_hash_digest", None) for rung in sorted_rungs),
    ]
    for secret in secrets:
        if secret:
            text = text.replace(_escape_markdown(secret), "[비공개]")
            text = text.replace(str(secret), "[비공개]")

    inline_keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ 승인", "callback_data": approve_data},
                {"text": "❌ 거부", "callback_data": deny_data},
            ]
        ]
    }
    return text, inline_keyboard


def _build_time_lines(group: Any) -> list[str]:
    source_asof = getattr(group, "source_asof", None)
    resting_deadline = (
        source_asof.get("resting_deadline")
        if isinstance(source_asof, Mapping)
        else None
    )
    fields = (
        ("유효기간", getattr(group, "valid_until", None), True),
        ("검증시각", getattr(group, "validated_at", None), False),
        ("제출 임대", getattr(group, "commit_lease_until", None), True),
        ("주문 유지기한", resting_deadline, True),
    )
    return [
        f"- {label}: {_format_datetime(value, approximate=approximate)}"
        for label, value, approximate in fields
        if _coerce_datetime(value) is not None
    ]


def _build_cash_lines(cash_stress: Mapping, *, currency: str | None) -> list[str]:
    lines: list[str] = []
    rendered_keys: set[str] = set()
    requested_currency = cash_stress.get("currency") or currency

    for key, label in _CASH_LABELS:
        if key not in cash_stress or cash_stress[key] is None:
            continue
        rendered_keys.add(key)
        value = cash_stress[key]
        rendered = (
            f"{_format_decimal(value)}%"
            if key.endswith("_pct")
            else _format_money(value, currency=requested_currency)
        )
        lines.append(f"- {label}: {rendered}")

    for raw_key in sorted(cash_stress, key=str):
        key = str(raw_key)
        if (
            key in rendered_keys
            or key == "currency"
            or _is_sensitive_key(key)
            or cash_stress[raw_key] is None
        ):
            continue
        lines.append(
            f"- {_escape_markdown(key)}: {_format_generic(cash_stress[raw_key])}"
        )
    return lines


def _format_diff_side(value: object, *, currency: str | None) -> str:
    if not isinstance(value, Mapping):
        return "미기재"

    parts: list[str] = []
    used_keys: set[object] = set()
    for keys, label, formatter in (
        (("quantity", "qty", "normalized_quantity"), "수량", _format_decimal),
        (
            ("limit_price", "price", "normalized_price"),
            "가격",
            lambda item: _format_money(item, currency=currency),
        ),
    ):
        key = next((candidate for candidate in keys if candidate in value), None)
        if key is not None:
            used_keys.add(key)
            parts.append(f"{label} {formatter(value[key])}")

    for raw_key in sorted(value, key=str):
        key = str(raw_key)
        if raw_key in used_keys or _is_sensitive_key(key) or value[raw_key] is None:
            continue
        parts.append(f"{_escape_markdown(key)} {_format_generic(value[raw_key])}")
    return " / ".join(parts) if parts else "미기재"


def _currency_for_market(*, market: str, symbol: str) -> str | None:
    if market == "equity_kr" or "KRW" in symbol.upper():
        return "KRW"
    if market == "equity_us":
        return "USD"
    return None


def _format_money(
    value: object,
    *,
    currency: object | None,
    none_label: str = "미기재",
) -> str:
    if value is None:
        return none_label
    amount = _format_decimal(value, grouping=True)
    normalized_currency = str(currency).upper() if currency else None
    if normalized_currency == "KRW":
        return f"₩{amount}"
    if normalized_currency == "USD":
        return f"${amount}"
    if normalized_currency:
        return f"{amount} {_escape_markdown(normalized_currency)}"
    return amount


def _format_decimal(value: object, *, grouping: bool = False) -> str:
    if value is None:
        return "미기재"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return _escape_markdown(value)
    if not number.is_finite():
        return _escape_markdown(value)

    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"-0", ""}:
        text = "0"
    if not grouping:
        return text

    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    integer, separator, fractional = text.partition(".")
    grouped = f"{int(integer or '0'):,}"
    if separator:
        grouped = f"{grouped}.{fractional}"
    return f"{sign}{grouped}"


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def _format_datetime(value: object, *, approximate: bool) -> str:
    parsed = _coerce_datetime(value)
    if parsed is None:
        return "미기재"
    prefix = "~" if approximate else ""
    return f"{prefix}{parsed:%H:%M} KST ({parsed:%Y-%m-%d})"


def _format_generic(value: object) -> str:
    if isinstance(value, datetime):
        return _format_datetime(value, approximate=False)
    if isinstance(value, (Decimal, int, float)) and not isinstance(value, bool):
        return _format_decimal(value)
    if isinstance(value, (Mapping, list, tuple)):
        return _escape_markdown(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        )
    return _escape_markdown(value)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold()
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _escape_markdown(value: object) -> str:
    text = str(value)
    for character in ("\\", "`", "*", "_", "[", "]"):
        text = text.replace(character, f"\\{character}")
    return text
