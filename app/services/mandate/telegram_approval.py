"""Published-card authority for terms and activation-profile decisions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import TypeGuard

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.mandate import MandateApproval, MandateApprovalRequest, MandateTerms
from app.models.strategy_sleeves import StrategySleeveVersion
from app.services.mandate import store
from app.services.mandate.vocabulary import MandateRefusal

PREFIX = "mta:"
CARD_TTL_SECONDS = 900
_BODY = struct.Struct(">QIB8s")
_TAG_SIZE = 16
_TOKEN = re.compile(r"mta:[A-Za-z0-9_-]{50}")
_DECISIONS = ("denied", "approved")


def _tag(body: bytes, chat_id: int, message_id: int, content_sha256: str) -> bytes:
    key = hmac.digest(settings.SECRET_KEY.encode(), b"mandate-approval-v1", "sha256")
    binding = struct.pack(">qq", chat_id, message_id) + bytes.fromhex(content_sha256)
    return hmac.digest(key, body + binding, "sha256")[:_TAG_SIZE]


def card_markup(
    request: MandateApprovalRequest, chat_id: int, message_id: int, now: datetime
) -> dict:
    expires = int(now.timestamp()) + CARD_TTL_SECONDS
    nonce = secrets.token_bytes(8)
    buttons = []
    for decision, label in enumerate(("거절", "승인")):
        body = _BODY.pack(request.id, expires, decision, nonce)
        raw = body + _tag(body, chat_id, message_id, request.content_sha256)
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        buttons.append({"text": label, "callback_data": PREFIX + token})
    return {"inline_keyboard": [buttons]}


def _bounded_int(value: object, minimum: int, maximum: int) -> TypeGuard[int]:
    return type(value) is int and minimum <= value <= maximum


def is_mandate_update(update: dict) -> bool:
    query = update.get("callback_query")
    data = query.get("data") if type(query) is dict else None
    return type(data) is str and data.startswith(PREFIX)


async def handle_mandate_update(
    update: dict,
    *,
    now: datetime,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
) -> str:
    """Return only a closed outcome; authenticate before opening a transaction."""
    query = update["callback_query"]
    message = query.get("message")
    chat = message.get("chat") if type(message) is dict else None
    chat_id = chat.get("id") if type(chat) is dict else None
    message_id = message.get("message_id") if type(message) is dict else None
    if (
        not _bounded_int(chat_id, -(2**52 - 1), 2**52 - 1)
        or not chat_id
        or str(chat_id) not in settings.order_proposals_telegram_chat_allowlist
    ):
        return "chat_not_allowed"
    if not _bounded_int(message_id, 1, 2**31 - 1) or (
        "update_id" in update and not _bounded_int(update["update_id"], 1, 2**31 - 1)
    ):
        return "invalid_telegram_identifier"
    sender = query.get("from")
    principal = sender.get("id") if type(sender) is dict else None
    if not _bounded_int(principal, 1, 2**52 - 1):
        return "invalid_telegram_identifier"
    allowed = settings.MANDATE_APPROVER_TELEGRAM_USER_IDS.split(",")
    if str(principal) not in {item.strip() for item in allowed}:
        return "mandate_approver_not_allowed"

    data = query["data"]
    if not _TOKEN.fullmatch(data):
        return "malformed_callback_data"
    encoded = data[len(PREFIX) :]
    raw = base64.urlsafe_b64decode(encoded + "==")
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded:
        return "malformed_callback_data"
    body, supplied_tag = raw[:-_TAG_SIZE], raw[-_TAG_SIZE:]
    request_id, expires, decision, nonce = _BODY.unpack(body)
    if not 0 < request_id < 2**63 or decision >= len(_DECISIONS):
        return "malformed_callback_data"
    if now.timestamp() >= expires:
        return "mandate_card_expired"
    try:
        async with session_factory() as session:
            request = await session.get(MandateApprovalRequest, request_id)
            if request is None or not hmac.compare_digest(
                supplied_tag, _tag(body, chat_id, message_id, request.content_sha256)
            ):
                return "mandate_card_invalid"
            await store.record_approval(
                session,
                request_id=request_id,
                decision=_DECISIONS[decision],
                principal=str(principal),
                nonce_digest=hashlib.sha256(
                    struct.pack(">Q", request_id) + nonce
                ).hexdigest(),
                now=now,
            )
            await session.commit()
    except store.MandateError as exc:
        if exc.code == MandateRefusal.REQUEST_STALE:
            return "mandate_request_stale"
        return "mandate_decision_refused"
    except Exception:  # noqa: BLE001 - no payload or database detail crosses the boundary
        return "internal_error"
    return "mandate_decision_recorded"


async def card_text(session: AsyncSession, request: MandateApprovalRequest) -> str:
    """Render the immutable snapshot and reserve the profile review section for T9."""
    if (
        await session.scalar(
            select(MandateApproval.id).where(MandateApproval.request_id == request.id)
        )
        is not None
    ):
        raise ValueError("mandate_request_stale")
    version, terms = await store.current_terms(session, request.sleeve_id)
    if version is None or version.version != request.base_sleeve_version:
        raise ValueError("mandate_request_stale")
    lines = [
        f"위임장 승인 요청 #{request.id} · {request.subject_kind}",
        f"승인 근거 {request.approval_ref}",
        f"내용 SHA-256 {request.content_sha256}",
        json.dumps(request.content, ensure_ascii=False, sort_keys=True, indent=2),
    ]
    if request.subject_kind == "terms":
        previous = {}
        if terms is not None:
            previous = store.terms_content_from_rows(terms, version)
        else:
            result = await session.execute(
                select(MandateTerms, StrategySleeveVersion)
                .join(
                    StrategySleeveVersion,
                    MandateTerms.sleeve_version_id == StrategySleeveVersion.id,
                )
                .where(
                    StrategySleeveVersion.sleeve_id == request.sleeve_id,
                    StrategySleeveVersion.version < request.base_sleeve_version,
                    StrategySleeveVersion.state == "live",
                )
                .order_by(StrategySleeveVersion.version.desc())
                .limit(1)
            )
            if prior := result.first():
                previous = store.terms_content_from_rows(prior[0], prior[1])
        changed = {
            key: {"이전": previous.get(key), "요청": value}
            for key, value in request.content.items()
            if previous.get(key) != value
        }
        lines.extend(
            (
                "직전 live 조항과의 변경",
                json.dumps(changed, ensure_ascii=False, default=str),
            )
        )
    else:
        if terms is None or terms.id != request.content["terms_id"]:
            raise ValueError("mandate_request_stale")
        lines.extend(
            (
                "활성화 프로필 승인 화면",
                f"결속 조항 SHA-256 {terms.content_sha256}",
                "사전 주문 원금·하루 금액 밖 노출·미결 주문·취소의 한계",
                "지정가 상한·추가 매수 동결·cap·회차 상한·만료·조항 해시·D1~D4",
            )
        )
    return "\n".join(lines)
