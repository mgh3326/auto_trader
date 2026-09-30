"""ROB-1052 — auto-approve digest batching and notices-destination helpers.

Message classes and where they go:

* Human approval cards (``pending_approval`` — manual, reconfirm, batch,
  loss-cut confirmation) keep the existing ``allowlist[0]`` destination.
* Auto-approve notices, fill notifications and expiry notices are "notices"
  and route to ``ORDER_PROPOSALS_TELEGRAM_NOTICES_*`` when configured; when
  it is not configured every class keeps its pre-split destination.

Round key for the auto-approve digest: ``dispatch_proposal`` is the single
emit site for auto-approve notices (its ``auto_submitted`` publication
branch).  ``open_auto_digest_round`` in ``dispatch.py`` installs an
``AutoDigestCollector`` for one batch of those calls -- today the
post-commit ``pending_dispatch`` loop of ``support_reserve_net_consume_impl``
and the row loop of ``apply_decision_table`` -- so all auto-approve notices
emitted inside one such batch flush as ONE digest message (plus overflow
chunks) at scope exit.  Proposals dispatched outside any round (a plain
``order_proposal_create``) are a 1-item "round" that emits immediately,
identical to the pre-digest code path.

Digest keyboard contract: every vetoable item contributes one ``vc`` button
bound to its OWN attempt/nonce (same envelope as a standalone auto-veto
card).  When an operator vetoes one item, the digest message is re-rendered
by ``render_auto_digest_veto_update`` so the tapped item shows the outcome
and every still-live sibling keeps a working button -- a single-message
edit that does not destroy un-consumed nonces.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from app.core.config import OrderProposalsNoticesDestination, settings
from app.core.portfolio_links import build_position_detail_url
from app.services.order_proposals.approval_message import (
    _escape_inline_code,
    _escape_markdown,
    _format_datetime,
    _format_symbol_label,
    build_callback_data,
)
from app.services.order_proposals.auto_approve import auto_veto_thesis_summary
from app.services.order_proposals.auto_veto import classify_veto_outcome
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    ApprovalDispatchState,
    DispatchBinding,
)
from app.telegram_contract import (
    TELEGRAM_SEND_MESSAGE_TEXT_LIMIT,
    split_telegram_text,
    telegram_text_length,
)

logger = logging.getLogger(__name__)

_AUTO_ACTION_HEADERS: dict[str, str] = {
    "cancel": "✅ 자동 취소됨",
    "replace": "✅ 자동 정정 접수됨",
}
_AUTO_RESULT_LABELS: dict[str, str] = {
    "submitted_acked": "주문 접수(체결 대기)",
    "submitted_resting": "주문 접수(미체결 대기)",
    "cancelled": "취소 완료",
}
_VETO_OUTCOME_LABELS: dict[str, str] = {
    "filled": "✅ 체결됨",
    "failed": "⚠️ 취소 실패",
    "unconfirmed": "🔍 취소 확인 중",
    "cancelled": "🛑 취소됨",
}


@dataclass(slots=True)
class AutoDigestItem:
    """One buffered auto-submitted notice inside a collection round."""

    proposal_id: uuid.UUID
    attempt_id: uuid.UUID | None
    vetoable: bool
    callback_data: str | None
    payload_chars: int
    symbol: str
    display_name: str | None
    market: str
    account_mode: str
    broker_account_id: str | None
    side: str
    action: str
    target_broker_order_id: str | None
    quantities: list[str]
    prices: list[str]
    result: str
    thesis_summary: str
    valid_until_text: str
    policy_version: str
    detail_url: str | None


@dataclass(slots=True)
class AutoDigestCollector:
    """Accumulates auto-approve notices for one dispatch round."""

    round_id: uuid.UUID = field(default_factory=uuid.uuid4)
    items: list[AutoDigestItem] = field(default_factory=list)
    # proposal_id -> the finalized approval_dispatch payload (filled by the
    # scope-exit flush) so callers that received "pending" inside the round
    # can reconcile their results after the block exits.
    outcomes: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)

    def add(self, item: AutoDigestItem) -> None:
        self.items.append(item)


@dataclass(frozen=True, slots=True)
class AutoDigestChunk:
    """One sendable digest message plus the items it carries."""

    text: str
    inline_keyboard: dict[str, Any] | None
    items: tuple[AutoDigestItem, ...]


_current_collector: ContextVar[AutoDigestCollector | None] = ContextVar(
    "order_proposals_auto_digest", default=None
)


def current_auto_digest() -> AutoDigestCollector | None:
    """Return the collector of the enclosing dispatch round, if any."""
    return _current_collector.get()


def activate_collector(
    collector: AutoDigestCollector,
) -> Token[AutoDigestCollector | None]:
    return _current_collector.set(collector)


def reset_collector(token: Token[AutoDigestCollector | None]) -> None:
    _current_collector.reset(token)


def notices_destination() -> OrderProposalsNoticesDestination:
    """Resolve the configured notices destination (chat id + thread id)."""
    return settings.order_proposals_telegram_notices_destination


def snapshot_auto_digest_item(
    *,
    group: Any,
    rungs: Sequence[Any],
    attempt_id: uuid.UUID | None,
    binding: DispatchBinding | None,
    veto_nonce: str | None,
    vetoable: bool,
    result: str,
    policy_version: str,
    display_name: str | None,
    payload_chars: int,
) -> AutoDigestItem:
    """Capture the render fields one auto-submitted proposal contributes."""
    ordered = sorted(rungs, key=lambda rung: rung.rung_index)
    callback_data = (
        build_callback_data(
            action="vc",
            proposal_id=group.proposal_id,
            nonce=veto_nonce,
            binding=binding,
        )
        if vetoable
        else None
    )
    return AutoDigestItem(
        proposal_id=group.proposal_id,
        attempt_id=attempt_id,
        vetoable=vetoable,
        callback_data=callback_data,
        payload_chars=payload_chars,
        symbol=str(group.symbol),
        display_name=display_name,
        market=str(getattr(group, "market", "") or ""),
        account_mode=str(getattr(group, "account_mode", "") or ""),
        broker_account_id=getattr(group, "broker_account_id", None),
        side=str(getattr(group, "side", "") or ""),
        action=str(getattr(group, "action", None) or "place"),
        target_broker_order_id=getattr(group, "target_broker_order_id", None),
        quantities=[f"#{rung.rung_index + 1} {rung.quantity}" for rung in ordered],
        prices=[f"#{rung.rung_index + 1} {rung.limit_price}" for rung in ordered],
        result=result,
        thesis_summary=auto_veto_thesis_summary(group) or "",
        valid_until_text=_format_datetime(
            getattr(group, "valid_until", None), approximate=False
        ),
        policy_version=policy_version,
        detail_url=build_position_detail_url(group.symbol, group.market),
    )


def _result_label(result: str) -> str:
    return _AUTO_RESULT_LABELS.get(result, result or "확인 불가")


def _render_item_block(item: AutoDigestItem, *, compact: bool = False) -> list[str]:
    header = _AUTO_ACTION_HEADERS.get(item.action, "✅ 자동 접수됨")
    lines = [
        f"{header} — {_format_symbol_label(item.symbol, display_name=item.display_name)}"
    ]
    account = item.account_mode
    if item.broker_account_id:
        account = (
            f"{account} · {item.broker_account_id}"
            if account
            else item.broker_account_id
        )
    if account:
        lines.append(f"- 계좌: `{_escape_inline_code(account)}`")
    lines.append(
        f"- 방향: `{_escape_inline_code(item.side)}`"
        f" · 수량: {', '.join(item.quantities)}"
        f" · 가격: {', '.join(item.prices)}"
    )
    if item.action in {"cancel", "replace"} and item.target_broker_order_id:
        lines.append(
            f"- 대상 주문: `{_escape_inline_code(item.target_broker_order_id)}`"
        )
    lines.append(f"- 결과: {_escape_markdown(_result_label(item.result))}")
    if not compact:
        lines.append(f"- 근거: {_escape_markdown(item.thesis_summary)}")
    lines.append(f"- 유효기간: {item.valid_until_text}")
    lines.append(f"- `auto:policy@{_escape_inline_code(item.policy_version)}`")
    if not compact and item.detail_url:
        lines.append(f"- /invest 상세: {item.detail_url}")
    return lines


def _digest_header(count: int, part: int, total: int) -> str:
    header = f"🤖 자동승인 {count}건"
    if total > 1:
        header = f"{header} ({part}/{total})"
    return header


def _veto_button(item: AutoDigestItem, callback_data: str) -> dict[str, Any]:
    return {"text": f"🛑 취소 {item.symbol}", "callback_data": callback_data}


def render_auto_digest_chunks(
    items: Sequence[AutoDigestItem],
) -> list[AutoDigestChunk]:
    """Group items into sendable digest messages under the UTF-16 limit."""
    if not items:
        return []
    # Every chunk's text is header + "\n\n" + blocks.join("\n\n"), so the
    # header block is charged its own trailing separator and the widest
    # possible "(part/total)" suffix: part and total each take up to
    # len(str(len(items))) digits, and len(str)-width "9" values are the
    # widest single-part headers renderable.
    widest = 10 ** len(str(len(items))) - 1
    header_budget = telegram_text_length(_digest_header(len(items), widest, widest))
    block_budget = TELEGRAM_SEND_MESSAGE_TEXT_LIMIT - header_budget - 2
    blocks: dict[uuid.UUID, str] = {}
    for item in items:
        block = "\n".join(_render_item_block(item))
        if telegram_text_length(block) > block_budget:
            # A lone block can outgrow a whole chunk: the thesis line is the
            # only unbounded field, drop it first; hard-truncate as the last
            # resort so a digest never fails with telegram_payload_too_long.
            block = "\n".join(
                line
                for line in _render_item_block(item)
                if not line.startswith("- 근거:")
            )
        if telegram_text_length(block) > block_budget:
            block = split_telegram_text(block, max_units=block_budget)[0]
        blocks[item.proposal_id] = block
    groups: list[list[AutoDigestItem]] = []
    current: list[AutoDigestItem] = []
    current_len = 0
    for item in items:
        additional = telegram_text_length(blocks[item.proposal_id]) + (
            2 if current else 0
        )
        if current and current_len + additional > block_budget:
            groups.append(current)
            current = []
            current_len = 0
            additional = telegram_text_length(blocks[item.proposal_id])
        current.append(item)
        current_len += additional
    if current:
        groups.append(current)
    total = len(groups)
    chunks: list[AutoDigestChunk] = []
    for part, group_items in enumerate(groups, start=1):
        text = (
            _digest_header(len(items), part, total)
            + "\n\n"
            + "\n\n".join(blocks[item.proposal_id] for item in group_items)
        )
        rows = [
            [_veto_button(item, item.callback_data)]
            for item in group_items
            if item.callback_data is not None
        ]
        chunks.append(
            AutoDigestChunk(
                text=text,
                inline_keyboard={"inline_keyboard": rows} if rows else None,
                items=tuple(group_items),
            )
        )
    return chunks


def _digest_item_from_group(
    group: Any,
    rungs: Sequence[Any],
    *,
    display_name: str | None = None,
) -> AutoDigestItem:
    """Re-derive render fields from the current durable row at veto time."""
    auto = (getattr(group, "source_asof", None) or {}).get("auto_approved", {}) or {}
    outcomes = auto.get("outcomes") or []
    ordered = sorted(rungs, key=lambda rung: rung.rung_index)
    return AutoDigestItem(
        proposal_id=group.proposal_id,
        attempt_id=getattr(group, "approval_dispatch_attempt_id", None),
        vetoable=False,
        callback_data=None,
        payload_chars=0,
        symbol=str(group.symbol),
        display_name=display_name,
        market=str(getattr(group, "market", "") or ""),
        account_mode=str(getattr(group, "account_mode", "") or ""),
        broker_account_id=getattr(group, "broker_account_id", None),
        side=str(getattr(group, "side", "") or ""),
        action=str(getattr(group, "action", None) or "place"),
        target_broker_order_id=getattr(group, "target_broker_order_id", None),
        quantities=[f"#{rung.rung_index + 1} {rung.quantity}" for rung in ordered],
        prices=[f"#{rung.rung_index + 1} {rung.limit_price}" for rung in ordered],
        result=str(outcomes[0]) if outcomes else "",
        thesis_summary=auto_veto_thesis_summary(group) or "",
        valid_until_text=_format_datetime(
            getattr(group, "valid_until", None), approximate=False
        ),
        policy_version=str(auto.get("policy_version") or ""),
        detail_url=build_position_detail_url(group.symbol, group.market),
    )


def _digest_member_live(group: Any) -> bool:
    """True while the member's own auto-veto nonce is still consumable."""
    return (
        str(getattr(group, "approval_dispatch_state", "") or "")
        == ApprovalDispatchState.SENT_CURRENT
        and str(getattr(group, "approval_dispatch_card_kind", "") or "")
        == ApprovalCardKind.AUTO_VETO
        and bool(getattr(group, "approval_nonce", None))
        and getattr(group, "approval_nonce_used_at", None) is None
        and getattr(group, "approval_dispatch_attempt_id", None) is not None
        and getattr(group, "approval_dispatch_membership_revision", None) is not None
        and bool(getattr(group, "approval_dispatch_membership_digest", None))
    )


def _member_veto_callback(group: Any) -> str | None:
    """Rebuild the member's own vc envelope from its durable binding."""
    if not _digest_member_live(group):
        return None
    binding = DispatchBinding(
        attempt_id=group.approval_dispatch_attempt_id,
        card_kind=ApprovalCardKind.AUTO_VETO,
        membership_revision=group.approval_dispatch_membership_revision,
        membership_digest=group.approval_dispatch_membership_digest,
    )
    try:
        return build_callback_data(
            action="vc",
            proposal_id=group.proposal_id,
            nonce=group.approval_nonce,
            binding=binding,
        )
    except Exception:  # noqa: BLE001 - never break the veto feedback edit
        return None


def _member_handled_status(group: Any) -> str:
    """Summarize a consumed member from its recorded veto outcomes."""
    veto = (
        (getattr(group, "source_asof", None) or {}).get("auto_approved", {}) or {}
    ).get("veto") or {}
    kinds = {classify_veto_outcome(outcome) for outcome in veto.get("outcomes") or []}
    for kind in ("filled", "failed", "unconfirmed"):
        if kind in kinds:
            return _VETO_OUTCOME_LABELS[kind]
    if "cancelled" in kinds:
        return _VETO_OUTCOME_LABELS["cancelled"]
    return "➖ 처리됨"


def render_auto_digest_veto_update(
    members: Sequence[tuple[Any, Sequence[Any]]],
    *,
    vetoed_proposal_id: uuid.UUID,
    outcome_text: str,
    display_names: dict[uuid.UUID, str | None] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Rebuild a multi-member digest after one veto was consumed.

    The tapped member carries the outcome text; every sibling whose own
    nonce is still consumable keeps its ``vc`` button; already-handled
    siblings show their recorded outcome.  If the rebuild would overflow
    the UTF-16 limit the item blocks are compacted once, then the plain
    outcome text is returned so the veto feedback is never lost.
    """
    for compact in (False, True):
        blocks: list[str] = []
        rows: list[list[dict[str, Any]]] = []
        for group, rungs in members:
            item = _digest_item_from_group(
                group,
                rungs,
                display_name=((display_names or {}).get(group.proposal_id)),
            )
            block = _render_item_block(item, compact=compact)
            if group.proposal_id == vetoed_proposal_id:
                block.append(f"→ {outcome_text}")
            else:
                callback = _member_veto_callback(group)
                if callback is not None:
                    rows.append([_veto_button(item, callback)])
                else:
                    block.append(f"→ {_member_handled_status(group)}")
            blocks.append("\n".join(block))
        text = _digest_header(len(members), 1, 1) + "\n\n" + "\n\n".join(blocks)
        if telegram_text_length(text) <= TELEGRAM_SEND_MESSAGE_TEXT_LIMIT:
            return text, {"inline_keyboard": rows}
    return outcome_text, {"inline_keyboard": []}


async def send_order_proposal_expiry_notice(*, notifier: Any, text: str) -> None:
    """Best-effort copy of an expiry edit into the notices destination.

    The approval card itself is still edited in place at its original
    destination (callback bindings cannot move); this sends the identical
    operator-facing text as a standalone notice.  When the notices
    destination is not configured this is a no-op -- the pre-split path
    never emitted an extra message, so the fallback stays identical.
    """
    dest = notices_destination()
    if not dest.configured or dest.chat_id is None:
        return
    try:
        await notifier.send_approval_message(
            text,
            None,
            chat_id=dest.chat_id,
            **(
                {"message_thread_id": dest.message_thread_id}
                if dest.message_thread_id is not None
                else {}
            ),
        )
    except Exception:  # noqa: BLE001 - notices never break their caller
        logger.debug("order_proposals.expiry_notice_send_failed", exc_info=True)


__all__ = [
    "AutoDigestChunk",
    "AutoDigestCollector",
    "AutoDigestItem",
    "activate_collector",
    "current_auto_digest",
    "notices_destination",
    "render_auto_digest_chunks",
    "render_auto_digest_veto_update",
    "reset_collector",
    "send_order_proposal_expiry_notice",
    "snapshot_auto_digest_item",
]
