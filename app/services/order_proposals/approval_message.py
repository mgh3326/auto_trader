"""Pure Telegram approval-message and callback-data builders."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from app.core.timezone import KST

_ALLOWED_ACTIONS = frozenset({"op", "dn", "lc", "vc", "ba"})
_CALLBACK_PATTERN = re.compile(
    r"^(?P<action>op|dn|lc|vc|ba):(?P<proposal_short>[0-9a-f]{8}):"
    r"(?P<nonce>[A-Za-z0-9_-]+)$"
)
_MAX_CALLBACK_BYTES = 64
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
        raise ValueError("action must be one of: op, dn, lc, vc, ba")
    if not isinstance(proposal_id, uuid.UUID):
        raise ValueError("proposal_id must be a UUID")
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", nonce):
        raise ValueError("nonce must be a non-empty URL-safe token")

    data = f"{action}:{str(proposal_id)[:8]}:{nonce}"
    if len(data.encode("utf-8")) > _MAX_CALLBACK_BYTES:
        raise ValueError("callback data must not exceed 64 bytes")
    return data


def build_batch_callback_data(*, batch_id: uuid.UUID, nonce: str) -> str:
    """Build compact callback data for a batch-only approval trigger."""
    return build_callback_data(action="ba", proposal_id=batch_id, nonce=nonce)


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


def build_action_diff(*, group: Any, rungs: Sequence[Any]) -> dict | None:
    action = getattr(group, "action", None) or "place"
    if action == "place" or len(rungs) != 1:
        return None

    source_asof = getattr(group, "source_asof", None)
    if not isinstance(source_asof, Mapping):
        return None
    before = source_asof.get("target_order_snapshot")
    if not isinstance(before, Mapping):
        return None

    if action == "replace":
        after = {
            "quantity": getattr(rungs[0], "quantity", None),
            "limit_price": getattr(rungs[0], "limit_price", None),
        }
    elif action == "cancel":
        after = {"quantity": "0", "limit_price": before.get("limit_price")}
    else:
        return None

    return {
        "before": {
            "quantity": before.get("remaining_quantity"),
            "limit_price": before.get("limit_price"),
        },
        "after": after,
    }


def build_buying_power_shortfall_text(detail: Mapping[str, Any]) -> str | None:
    if detail.get("reason") != "insufficient_buying_power":
        return None
    currency = _supported_currency(detail.get("currency"))
    if currency is None:
        return None
    values: list[Decimal] = []
    for key in ("available", "required", "shortfall"):
        try:
            value = Decimal(str(detail[key]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return None
        if not value.is_finite():
            return None
        values.append(value)
    available, required, shortfall = values
    return (
        f"매수가능 {_format_shortfall_money(available, currency=currency)} / "
        f"필요 {_format_shortfall_money(required, currency=currency)} → "
        f"부족 {_format_shortfall_money(shortfall, currency=currency)} "
        "— 입금 후 재승인"
    )


def build_batch_approval_message(
    *,
    batch: Any,
    proposals: Sequence[tuple[Any, Sequence[Any]]],
) -> tuple[str, dict]:
    """Render a pending manual-approval batch without exposing raw nonces."""
    if len(proposals) < 2:
        raise ValueError("batch summary requires at least two proposals")
    batch_id = getattr(batch, "batch_id", None)
    nonce = getattr(batch, "approval_nonce", None)
    if not isinstance(batch_id, uuid.UUID) or not nonce:
        raise ValueError("batch_id and approval_nonce are required")

    totals: dict[str, Decimal] = {}
    account_totals: dict[tuple[str, str], Decimal] = {}
    lines = [
        "*일괄 승인 대기*",
        f"- 제안: {len(proposals)}건",
        "",
        "*주문 목록*",
    ]
    for group, rungs in proposals:
        symbol = str(getattr(group, "symbol", None) or "미기재")
        side = str(getattr(group, "side", None) or "미기재")
        market = str(getattr(group, "market", None) or "")
        currency = _currency_for_market(market=market, symbol=symbol) or "기타"
        account_label = _batch_account_label(group)
        rung_parts: list[str] = []
        proposal_total = Decimal("0")
        has_notional = False
        for rung in sorted(rungs, key=lambda item: getattr(item, "rung_index", 0)):
            quantity = _safe_decimal(getattr(rung, "quantity", None))
            price = _safe_decimal(getattr(rung, "limit_price", None))
            explicit = _safe_decimal(getattr(rung, "notional", None))
            notional = (
                explicit
                if explicit is not None
                else (
                    quantity * price
                    if quantity is not None and price is not None
                    else None
                )
            )
            if notional is not None:
                proposal_total += notional
                has_notional = True
            rung_parts.append(
                f"#{int(getattr(rung, 'rung_index', 0)) + 1} "
                f"{_format_decimal(getattr(rung, 'quantity', None))} × "
                f"{_format_money(getattr(rung, 'limit_price', None), currency=currency, none_label='시장가')}"
            )
        lines.append(
            f"- `{_escape_inline_code(symbol)}` "
            f"`{_escape_inline_code(side)}` · "
            f"{_escape_markdown(account_label)} · " + "; ".join(rung_parts)
        )
        if has_notional:
            totals[currency] = totals.get(currency, Decimal("0")) + proposal_total
            account_key = (account_label, currency)
            account_totals[account_key] = (
                account_totals.get(account_key, Decimal("0")) + proposal_total
            )

    lines.extend(["", "*금액 요약*"])
    for currency, amount in sorted(totals.items()):
        lines.append(f"- 합계: {_format_money(amount, currency=currency)}")
    for (account_label, currency), amount in sorted(account_totals.items()):
        lines.append(
            f"- {_escape_markdown(account_label)}: "
            f"{_format_money(amount, currency=currency)}"
        )
    lines.extend(
        [
            "",
            f"- 승인 기한: {_format_datetime(getattr(batch, 'expires_at', None), approximate=False)}",
        ]
    )
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "전체 승인",
                    "callback_data": build_batch_callback_data(
                        batch_id=batch_id, nonce=str(nonce)
                    ),
                }
            ]
        ]
    }
    return "\n".join(lines), keyboard


def build_batch_result_message(
    *,
    proposals: Sequence[tuple[Any, Sequence[Any]]],
    results: Sequence[Mapping[str, Any]],
) -> str:
    """Render terminal batch results grouped by operator-relevant status."""
    symbols = {
        str(getattr(group, "proposal_id", "")): str(
            getattr(group, "symbol", None) or "미기재"
        )
        for group, _rungs in proposals
    }
    headings = (
        ("approved", "승인 완료"),
        ("needs_reconfirm", "재확인 필요"),
        ("skipped", "제외/건너뜀"),
        ("failed", "실패"),
    )
    grouped: dict[str, list[str]] = {status: [] for status, _label in headings}
    for result in results:
        status = str(result.get("status") or "failed")
        if status not in grouped:
            status = "failed"
        proposal_id = str(result.get("proposal_id") or "")
        symbol = symbols.get(proposal_id, proposal_id[:8] or "미기재")
        reason = " ".join(str(result.get("reason") or "").split())
        if len(reason) > 160:
            reason = reason[:159] + "…"
        suffix = f" — {_escape_markdown(reason)}" if reason else ""
        grouped[status].append(f"- `{_escape_inline_code(symbol)}`{suffix}")

    lines = ["*일괄 승인 결과*"]
    for status, label in headings:
        if grouped[status]:
            lines.extend(["", f"*{label}*", *grouped[status]])
    if len(lines) == 1:
        lines.extend(["", "- 처리 결과 없음"])
    return "\n".join(lines)


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
    action = str(getattr(group, "action", None) or "place")
    target_broker_order_id = getattr(group, "target_broker_order_id", None)
    currency = _currency_for_market(market=market, symbol=symbol)
    quantity_unit = "주" if market in {"equity_kr", "equity_us"} else ""
    sorted_rungs = sorted(rungs, key=lambda rung: getattr(rung, "rung_index", 0))
    explicit_reconfirm = diff is not None
    shortfall_text = (
        build_buying_power_shortfall_text(diff) if isinstance(diff, Mapping) else None
    )
    effective_diff = (
        None
        if shortfall_text is not None
        else diff
        if explicit_reconfirm
        else build_action_diff(group=group, rungs=rungs)
    )

    title = "*주문 제안 재확인*" if explicit_reconfirm else "*주문 제안 승인*"
    lines = [
        title,
        f"- 종목: `{_escape_inline_code(symbol)}`",
        "- 시장/방향/유형: "
        f"`{_escape_inline_code(f'{market} / {side} / {order_type}')}`",
    ]
    if action != "place":
        target = (
            f"`{_escape_inline_code(target_broker_order_id)}`"
            if target_broker_order_id
            else "미기재"
        )
        lines.extend(
            [
                f"- 작업: `{_escape_inline_code(action)}`",
                f"- 대상 주문 ID: {target}",
            ]
        )
    lines.extend(["", "*주문 단계*"])
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

    if getattr(group, "exit_intent", None) == "loss_cut":
        lines.extend(
            [
                "",
                "*손절 근거*",
                f"- 사유: {_escape_markdown(getattr(group, 'exit_reason', None))}",
                f"- 회고: #{getattr(group, 'retrospective_id', None)}",
            ]
        )

    time_lines = _build_time_lines(group)
    if time_lines:
        lines.extend(["", "*시간*", *time_lines])

    if cash_stress:
        cash_lines = _build_cash_lines(cash_stress, currency=currency)
        if cash_lines:
            lines.extend(["", "*현금 스트레스*", *cash_lines])

    if shortfall_text is not None:
        lines.extend(["", "*매수가능 금액 부족*", f"- {shortfall_text}"])

    if effective_diff is not None:
        before = (
            effective_diff.get("before")
            if isinstance(effective_diff, Mapping)
            else None
        )
        after = (
            effective_diff.get("after") if isinstance(effective_diff, Mapping) else None
        )
        diff_heading = "*재확인 변경사항*" if explicit_reconfirm else "*주문 변경사항*"
        lines.extend(
            [
                "",
                diff_heading,
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


def build_loss_cut_confirmation_message(
    *,
    group: Any,
    rungs: Sequence[Any],
    evidence: Mapping[str, Any],
) -> tuple[str, dict]:
    """Render the explicit second-step loss-cut confirmation prompt."""
    nonce = getattr(group, "approval_nonce", None)
    if not nonce:
        raise ValueError("group.approval_nonce is required")
    proposal_id = getattr(group, "proposal_id", None)
    market = str(getattr(group, "market", "") or "미기재")
    symbol = str(getattr(group, "symbol", "") or "미기재")
    currency = _currency_for_market(market=market, symbol=symbol)
    quantity_unit = "주" if market in {"equity_kr", "equity_us"} else ""
    evidence_by_rung = {
        int(item.get("rung_index", 0)): item
        for item in evidence.get("rungs", [])
        if isinstance(item, Mapping)
    }

    lines = [
        "*⚠️ 손절 확인*",
        f"- 종목: `{_escape_inline_code(symbol)}`",
        "",
        "*주문 및 손실 요약*",
    ]
    for rung in sorted(rungs, key=lambda value: getattr(value, "rung_index", 0)):
        index = int(getattr(rung, "rung_index", 0))
        item = evidence_by_rung.get(index, {})
        quantity = _format_decimal(getattr(rung, "quantity", None))
        limit_price = _format_money(
            getattr(rung, "limit_price", None), currency=currency, none_label="시장가"
        )
        current_price = _format_money(
            item.get("current_price"), currency=currency, none_label="조회 실패"
        )
        slip_band = _format_money(
            item.get("loss_cut_slip_band"), currency=currency, none_label="조회 실패"
        )
        loss_pct = str(item.get("loss_pct") or "조회 실패")
        if loss_pct != "조회 실패" and not loss_pct.endswith("%"):
            loss_pct = f"{loss_pct}%"
        lines.extend(
            [
                f"- #{index + 1}: {quantity}{quantity_unit} × {limit_price}",
                f"  현재가 {current_price} / 손실률 {loss_pct}",
                f"  허용 slip 밴드 하단 {slip_band}",
            ]
        )

    retrospective_id = evidence.get("retrospective_id")
    lesson = str(evidence.get("lesson_excerpt") or "미기재")
    lines.extend(
        [
            "",
            "*회고 근거*",
            f"- 회고: #{retrospective_id}",
            f"- 교훈: {_escape_markdown(lesson)}",
        ]
    )
    approval_note = str(getattr(group, "approval_issue_id", None) or "").strip()
    if approval_note:
        lines.append(f"- 승인 감사 메모: {_escape_markdown(approval_note)}")
    lines.extend(["", "이 손절 주문을 다시 확인해 주세요."])
    text = "\n".join(lines).replace(str(nonce), "[비공개]")
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "⚠️ 손절 확인",
                    "callback_data": build_callback_data(
                        action="lc", proposal_id=proposal_id, nonce=nonce
                    ),
                },
                {
                    "text": "❌ 거부",
                    "callback_data": build_callback_data(
                        action="dn", proposal_id=proposal_id, nonce=nonce
                    ),
                },
            ]
        ]
    }
    return text, keyboard


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
    requested_currency = _supported_currency(cash_stress.get("currency")) or currency

    for key, label in _CASH_LABELS:
        if key not in cash_stress or cash_stress[key] is None:
            continue
        value = cash_stress[key]
        if key.endswith("_pct"):
            numeric = _format_numeric(value)
            rendered = f"{numeric}%" if numeric is not None else None
        else:
            rendered = _format_money(
                value,
                currency=requested_currency,
                none_label="",
            )
        if not rendered:
            continue
        lines.append(f"- {label}: {rendered}")
    return lines


def _format_diff_side(value: object, *, currency: str | None) -> str:
    if not isinstance(value, Mapping):
        return "미기재"

    parts: list[str] = []
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
            rendered = formatter(value[key])
            if rendered != "미기재":
                parts.append(f"{label} {rendered}")
    return " / ".join(parts) if parts else "미기재"


def _currency_for_market(*, market: str, symbol: str) -> str | None:
    if market == "equity_kr" or "KRW" in symbol.upper():
        return "KRW"
    if market == "equity_us":
        return "USD"
    return None


def _batch_account_label(group: Any) -> str:
    account_mode = str(getattr(group, "account_mode", None) or "미기재")
    broker_account_id = str(getattr(group, "broker_account_id", None) or "")
    if not broker_account_id:
        return account_mode
    return f"{account_mode} ···{broker_account_id[-4:]}"


def _safe_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _supported_currency(value: object) -> str | None:
    normalized = str(value).upper() if isinstance(value, str) else None
    return normalized if normalized in {"KRW", "USD"} else None


def _format_money(
    value: object,
    *,
    currency: object | None,
    none_label: str = "미기재",
) -> str:
    amount = _format_numeric(value, grouping=True)
    if amount is None:
        return none_label
    normalized_currency = _supported_currency(currency)
    if normalized_currency == "KRW":
        return f"₩{amount}"
    if normalized_currency == "USD":
        return f"${amount}"
    if normalized_currency:
        return f"{amount} {_escape_markdown(normalized_currency)}"
    return amount


def _format_shortfall_money(value: Decimal, *, currency: str) -> str:
    if currency == "KRW":
        rounded = value.quantize(Decimal("1"), rounding=ROUND_CEILING)
        return f"{rounded:,.0f}원"
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return f"${rounded:,.2f}"


def _format_decimal(value: object, *, grouping: bool = False) -> str:
    return _format_numeric(value, grouping=grouping) or "미기재"


def _format_numeric(value: object, *, grouping: bool = False) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None

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


def _escape_markdown(value: object) -> str:
    text = str(value)
    for character in ("\\", "`", "*", "_", "[", "]"):
        text = text.replace(character, f"\\{character}")
    return text


def _escape_inline_code(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("`", "\\`")
