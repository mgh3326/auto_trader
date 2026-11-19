"""Text of the morning batch cards (hk 3795 §5.1, ticket T3a).

Pure: standard library only. Plain text (no Markdown parse mode), Korean for the
operator, identifiers and hashes as they are. The text carries everything the
operator is approving, so the card is the record of what was shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.services.mandate.batch_rules import MARKET_ZONE
from app.telegram_contract import (
    TELEGRAM_SEND_MESSAGE_TEXT_LIMIT,
    split_telegram_text,
    telegram_text_length,
)

KIND_LABELS: dict[str, str] = {
    "new_plan": "신규 계획 승인",
    "reapproval": "위임장 변경 재승인",
}
#: Telegram counts UTF-16 code units; the margin keeps every message well inside it.
MAX_UNITS = TELEGRAM_SEND_MESSAGE_TEXT_LIMIT - 96
#: Budget of one context message of the thesis and conditions (the header comes on top).
STORY_CHUNK_UNITS = 3500


@dataclass(frozen=True)
class CardFacts:
    kind: str
    market: str
    basket_key: str
    symbol: str
    planned_amount: Decimal
    adopted_amount: Decimal
    sector_key: str
    thesis: str
    invalidation_conditions: tuple[str, ...]
    terms_id: int
    terms_sha256: str
    plan_sha256: str
    target_session: date
    approval_deadline_at: datetime
    expires_at: datetime
    card_no: int
    open_order_count: int = 0


@dataclass(frozen=True)
class SummaryFacts:
    market: str
    basket_key: str
    target_session: date
    approval_deadline_at: datetime
    expires_at: datetime
    #: (symbol, tranche index) of tranches armed for the day
    armed_tranches: tuple[tuple[str, int], ...] = ()
    member_count: int = 0


def _local(value: datetime, market: str) -> str:
    return value.astimezone(MARKET_ZONE[market]).strftime("%m-%d %H:%M")


def _amount(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != 0 else "0"


def _check(text: str) -> str:
    """Never truncate approvable text: an oversized message is a refusal, not a clip."""
    if telegram_text_length(text) > MAX_UNITS:
        raise ValueError("a mandate card message exceeds the Telegram text limit")
    return text


def _story_lines(facts: CardFacts) -> list[str]:
    lines = [f"논지: {facts.thesis}"]
    if facts.invalidation_conditions:
        lines.append("논지 훼손 조건:")
        lines += [f"- {c}" for c in facts.invalidation_conditions]
    return lines


def _card_lines(facts: CardFacts, *, story: list[str]) -> list[str]:
    new_amount = facts.planned_amount - facts.adopted_amount
    lines = [
        f"[위임장 아침 승인] {KIND_LABELS[facts.kind]}",
        f"바구니 {facts.basket_key} · {facts.symbol} ({facts.market})",
        f"계획 금액 {_amount(facts.planned_amount)} (편입액 {_amount(facts.adopted_amount)}, "
        f"신규 매수 {_amount(new_amount)})",
        f"업종 {facts.sector_key}",
        *story,
        f"조항 #{facts.terms_id} 해시 {facts.terms_sha256}",
        f"계획 해시 {facts.plan_sha256}",
        f"대상 거래일 {facts.target_session.isoformat()} · 카드 {facts.card_no}",
        f"승인 마감 {_local(facts.approval_deadline_at, facts.market)} "
        "(마감 전 응답은 대상 거래일부터, 마감 뒤 응답은 그 다음 거래일부터 효력)",
        f"배치 만료 {_local(facts.expires_at, facts.market)} (이후 응답 불가)",
    ]
    if facts.kind == "reapproval":
        lines.append(
            f"기각하면 이 계획은 폐기됩니다. 미결 제안·주문 {facts.open_order_count}건과 "
            "보유는 그대로 남습니다."
        )
    else:
        lines.append("기각하면 이 계획은 rejected 입니다.")
    return lines


def render_card(facts: CardFacts) -> str:
    """The whole card in one text: what is approved, under which clause, what a button does.

    Raises ``ValueError`` when it does not fit one Telegram message; use
    ``render_card_messages`` to publish a card of any size.
    """
    return _check("\n".join(_card_lines(facts, story=_story_lines(facts))))


def render_card_messages(facts: CardFacts) -> tuple[str, ...]:
    """The messages that publish one card; the last one is the actionable card.

    A card that fits one message is that message. Otherwise the thesis and the
    invalidation conditions go out first, whole and in order, in UTF-16-bounded context
    messages, and the actionable card (clause hash, plan hash, deadlines, what a
    rejection does) carries the buttons. No approvable text is dropped.
    """
    try:
        return (render_card(facts),)
    except ValueError:
        pass
    chunks = split_telegram_text(
        "\n".join(_story_lines(facts)), max_units=STORY_CHUNK_UNITS
    )
    context = tuple(
        _check(
            f"[위임장 아침 승인] {facts.symbol} 논지·훼손 조건 ({i}/{len(chunks)})\n{chunk}"
        )
        for i, chunk in enumerate(chunks, 1)
    )
    pointer = [f"논지와 훼손 조건은 위 메시지 {len(chunks)}개에 전문이 있습니다."]
    return (*context, _check("\n".join(_card_lines(facts, story=pointer))))


def render_summary(facts: SummaryFacts) -> str:
    """The batch overview, sent first; with no member it is the whole message."""
    lines = [
        "[위임장 아침 승인] 오늘 적용 기준",
        f"바구니 {facts.basket_key} ({facts.market}) · 대상 거래일 "
        f"{facts.target_session.isoformat()}",
        f"승인 마감 {_local(facts.approval_deadline_at, facts.market)} · "
        f"만료 {_local(facts.expires_at, facts.market)}",
    ]
    if facts.member_count:
        lines.append(f"승인 대상 카드 {facts.member_count}장")
    else:
        lines.append("승인할 변경이 없습니다.")
    if facts.armed_tranches:
        lines.append("오늘 armed 회차:")
        lines += [f"- {sym} {idx}차" for sym, idx in facts.armed_tranches]
    else:
        lines.append("오늘 armed 회차 없음")
    return _check("\n".join(lines))


def render_answered(facts: CardFacts, outcome: str) -> str:
    """The card text after an answer; the buttons are removed by the caller."""
    return _check(f"{render_card_messages(facts)[-1]}\n\n결과: {outcome}")
