"""KIS 선물 모의 place/cancel 파이프라인 (hk 5946).

확정(``confirm=True``) 경로의 순서 — 앞 단계가 거부하면 뒤 단계는 일어나지 않는다:

1. 잠금 상수 확인 → 순수 형태·닫힌 상품(단축코드) 확인 → env 게이트 → 모의 호스트·계좌 확인
   → 귀속 확정(strategy 필수). **여기까지 네트워크·DB 0.**
2. 종목마스터로 계약 확정 → 귀속 신호 행 **커밋**(실패하면 전송 없음) → 세션·포지션·일일 손실·
   명목·방향 인식 가드 판정(`rules.decide`).
3. ``order_send_intents`` 예약 커밋(같은 키는 UNIQUE 위반 → 전송 없음) → 전송 직전 세션 재확인
   → POST **정확히 한 번**.
4. 응답 분류: 주문번호가 증명된 ACK 만 ``accepted``, 명시적 거절만 ``rejected``, 그 밖의 모든
   것(예외·주문번호 없는 ACK·판독 불가)은 ``uncertain`` — 예약을 유지하고 다시 보내지 않는다.
   ``accepted`` 는 체결이 아니다.

예약은 **송신하지 않았음이 증명된 경우에만** 풀린다(사전 거부). 미리보기(``confirm=False``)는
읽기만 하고 쓰기·예약·전송이 없다. 이 모듈은 MCP 도구·스케줄러에 연결되지 않는다.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.timezone import KST
from app.services.brokers.kis.futures_orders import FuturesPreDispatchError

from . import constants as C  # noqa: N812
from .attribution import (
    FuturesAttribution,
    MissingAttribution,
    resolve_attribution,
)
from .ledger_service import (
    DuplicateOrderIntent,
    FuturesPlaceRecorder,
    OrderFacts,
    OriginalOrder,
    SendReservation,
    build_cancel_client_order_id,
    build_client_order_id,
    kst_trading_day,
)
from .rules import (
    Contract,
    OrderRequest,
    Refusal,
    check_request_shape,
    decide,
    parse_positions,
    precheck_product,
    resolve_contract,
    session_refusal,
)

logger = logging.getLogger(__name__)

_SAFE_ORDER_NO = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")


@dataclass(frozen=True, slots=True)
class PlaceRequest:
    broker_symbol: str
    side: str
    price: Decimal
    quantity: int
    strategy: str | None = None
    signal_source: str | None = None
    correlation_id: str | None = None
    rung: int | str | None = None


@dataclass(frozen=True, slots=True)
class FuturesPlaceDeps:
    """호출자(CLI)와 테스트가 바꿀 수 있는 이음새. 잠금 상수는 여기에 없다."""

    now: Callable[[], datetime]
    open_read_view: Callable[[], Any]
    new_order_client: Callable[[], Any]
    account_no: Callable[[], str]
    recorder: Callable[[], FuturesPlaceRecorder]
    signal_recorder: Callable[[], Any]


def default_deps() -> FuturesPlaceDeps:
    from app.core.db import AsyncSessionLocal
    from app.services.kis_futures_read.view import open_futures_view

    from .attribution import FuturesSignalRecorder

    def new_order_client() -> Any:
        from app.services.brokers.kis.client import KISClient
        from app.services.brokers.kis.futures_orders import FuturesOrders

        return FuturesOrders(KISClient(is_mock=True))

    return FuturesPlaceDeps(
        now=lambda: datetime.now(KST),
        open_read_view=lambda: open_futures_view(is_mock=True),
        new_order_client=new_order_client,
        account_no=lambda: os.environ.get(C.ACCOUNT_ENV, ""),
        recorder=lambda: FuturesPlaceRecorder(AsyncSessionLocal),
        signal_recorder=lambda: FuturesSignalRecorder(AsyncSessionLocal),
    )


def _base(request: PlaceRequest, *, confirm: bool) -> dict[str, Any]:
    return {
        "account_mode": C.ACCOUNT_MODE,
        "broker_symbol": request.broker_symbol,
        "side": request.side,
        "price": format(request.price, "f")
        if isinstance(request.price, Decimal)
        else str(request.price),
        "quantity": request.quantity,
        "order_type": "limit",
        "dry_run": not confirm,
        "dispatch_started": False,
    }


def _refused(
    base: dict[str, Any], code: str, *, detail: str | None = None, human: bool = False
) -> dict[str, Any]:
    response = {
        **base,
        "success": False,
        "status": "refused",
        "error_code": code,
        "dispatch_started": False,
    }
    if detail is not None:
        response["detail"] = detail
    if human:
        response["requires_human_card"] = True
    return response


def _refusal(base: dict[str, Any], refusal: Refusal) -> dict[str, Any]:
    return _refused(
        base, refusal.code, detail=refusal.detail, human=refusal.requires_human_card
    )


def identity_error(order_client: Any, account: str) -> str | None:
    """모의 호스트·클라이언트·선물 계좌 형식 확인 — 예약·전송 전에 한 번 더 본다."""
    if not getattr(order_client, "is_mock_client", False):
        return "not_mock_client"
    if getattr(order_client, "base_url", "") != C.MOCK_HOST:
        return "not_mock_host"
    if len(account) != 8 or not account.isascii() or not account.isdigit():
        return "account_identity_unverified"
    return None


def classify_response(
    response: Any, *, expect_order_no: bool = True
) -> tuple[str, str | None]:
    """(``accepted``|``rejected``|``uncertain``, 주문번호). 증명된 것만 확정한다."""
    if not isinstance(response, dict):
        return "uncertain", None
    rt_cd = response.get("rt_cd")
    output = response.get("output")
    order_no = output.get("ODNO") if isinstance(output, dict) else None
    if rt_cd == "0":
        if isinstance(order_no, str) and _SAFE_ORDER_NO.fullmatch(order_no.strip()):
            return "accepted", order_no.strip()
        return "uncertain", None
    if isinstance(rt_cd, str) and rt_cd and rt_cd != "0":
        msg_cd = response.get("msg_cd")
        if isinstance(msg_cd, str) and msg_cd.strip():
            return "rejected", None
    return "uncertain", None


def _raw(response: Any) -> dict[str, Any] | None:
    """원장에 남기는 응답 — 계좌·토큰 같은 값은 필드 allowlist 로 걸러서 남긴다."""
    if not isinstance(response, dict):
        return None
    keep = {k: response.get(k) for k in ("rt_cd", "msg_cd", "msg1") if k in response}
    output = response.get("output")
    if isinstance(output, dict):
        keep["output"] = {
            k: output.get(k)
            for k in ("ODNO", "ORD_TMD", "KRX_FWDG_ORD_ORGNO")
            if k in output
        }
    return keep


async def _close(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001 - 종료 실패는 주문 결과를 바꾸지 않는다
        logger.warning("futures mock client close failed")


async def _not_sent(
    signals: Any,
    attribution: FuturesAttribution | None,
    *,
    created: bool,
    reason: str,
    detail: dict[str, Any],
) -> None:
    if signals is None or attribution is None or not created:
        return
    try:
        await signals.mark_not_sent(
            correlation_id=attribution.correlation_id,
            reason=reason,
            detail_patch=detail,
        )
    except Exception:  # noqa: BLE001 - 귀속은 이미 durable 이고 표시는 best-effort
        logger.warning("futures mock signal not-sent mark failed")


async def place_futures_mock_order(
    request: PlaceRequest,
    *,
    confirm: bool = False,
    deps: FuturesPlaceDeps | None = None,
) -> dict[str, Any]:
    """모의 선물 지정가 1건. ``confirm=False`` 는 미리보기(쓰기·예약·전송 없음)."""
    base = _base(request, confirm=confirm)
    try:
        C.assert_limits_locked()
    except C.LimitsNotLocked:
        return _refused(base, "limits_not_locked")

    product = precheck_product(request.broker_symbol)
    if isinstance(product, Refusal):
        return _refusal(base, product)
    order = OrderRequest(
        broker_symbol=request.broker_symbol,
        side=request.side,
        price=request.price,
        quantity=request.quantity,
    )
    shape = check_request_shape(order, product)
    if shape is not None:
        return _refusal(base, shape)

    deps = deps or default_deps()
    if confirm and os.environ.get(C.ENABLED_ENV) != "true":
        return _refused(base, "env_gate_disabled")
    order_client = deps.new_order_client() if confirm else None
    try:
        return await _place_flow(
            base, request, order, product, confirm, order_client, deps
        )
    finally:
        await _close(order_client)


async def _place_flow(
    base: dict[str, Any],
    request: PlaceRequest,
    order: OrderRequest,
    product: Any,
    confirm: bool,
    order_client: Any | None,
    deps: FuturesPlaceDeps,
) -> dict[str, Any]:
    attribution: FuturesAttribution | None = None
    if confirm:
        identity = identity_error(order_client, deps.account_no())
        if identity is not None:
            return _refused(base, identity)
        try:
            attribution = resolve_attribution(
                symbol=request.broker_symbol,
                side=request.side,
                price=request.price,
                quantity=request.quantity,
                strategy=request.strategy,
                signal_source=request.signal_source,
                correlation_id=request.correlation_id,
                kst_trade_day=deps.now().astimezone(KST).strftime("%Y-%m-%d"),
                rung=request.rung,
            )
        except MissingAttribution as exc:
            code = (
                "placeholder_strategy"
                if type(exc).__name__ == "InvalidStrategy"
                else "attribution_required"
            )
            return _refused(base, code, detail=",".join(exc.missing))
        base["correlation_id"] = attribution.correlation_id

    try:
        view = deps.open_read_view()
    except Exception as exc:  # noqa: BLE001 - 읽기 게이트(KIS_FUTURES_READ_ENABLED) 등
        return _refused(base, "read_gate_disabled", detail=type(exc).__name__)
    try:
        try:
            master = await view.futures_master()
        except Exception as exc:  # noqa: BLE001
            return _refused(base, "contract_unknown", detail=type(exc).__name__)
        contract = resolve_contract(master, request.broker_symbol, product)
        if isinstance(contract, Refusal):
            return _refusal(base, contract)
        base["contract_symbol"] = contract.contract_symbol

        now = deps.now()
        recorder = deps.recorder()
        signals: Any | None = None
        signal_created = False
        if confirm:
            assert attribution is not None
            signals = deps.signal_recorder()
            try:
                recorded = await signals.record(
                    attribution=attribution,
                    contract_symbol=contract.contract_symbol,
                    broker_symbol=request.broker_symbol,
                    side=request.side,
                    quantity=request.quantity,
                    price=request.price,
                    kst_date=now.astimezone(KST).strftime("%Y-%m-%d"),
                    detail={"product": product.code},
                )
            except Exception as exc:  # noqa: BLE001 - SignalStrategyConflict 포함
                # 귀속을 기록하지 못하면 주문은 나가지 않는다.
                logger.error(
                    "futures mock signal record failed, refusing to send: %s",
                    type(exc).__name__,
                )
                return _refused(
                    base, "signal_record_unavailable", detail=type(exc).__name__
                )
            signal_created = recorded.created

        async def refuse(refusal: Refusal) -> dict[str, Any]:
            await _not_sent(
                signals,
                attribution,
                created=signal_created,
                reason="rules_refused",
                detail={"status": "not_submitted", "error_code": refusal.code},
            )
            return _refusal(base, refusal)

        session = session_refusal(contract, now)
        if session is not None:
            return await refuse(session)
        try:
            balance = await view.futures_balance(deps.account_no())
        except Exception as exc:  # noqa: BLE001
            return await refuse(Refusal("positions_unverified", type(exc).__name__))
        positions = parse_positions(balance.get("positions"))
        if isinstance(positions, Refusal):
            return await refuse(positions)
        trading_day = kst_trading_day(now)
        try:
            daily_loss = await recorder.daily_loss_state(trading_day=trading_day)
        except Exception as exc:  # noqa: BLE001
            return await refuse(Refusal("daily_loss_unverified", type(exc).__name__))
        decision = decide(
            order,
            contract=contract,
            now=now,
            positions=positions,
            daily_loss=daily_loss,
        )
        if isinstance(decision, Refusal):
            return await refuse(decision)
    finally:
        await _close(view)

    base.update(
        position_effect=decision.effect,
        notional_krw=format(decision.notional_krw, "f"),
    )
    if not confirm:
        return {**base, "success": True, "status": "preview", "preview": True}

    assert order_client is not None and attribution is not None
    return await _send(
        base=base,
        request=request,
        contract=contract,
        effect=decision.effect,
        notional=decision.notional_krw,
        trading_day=trading_day,
        attribution=attribution,
        signals=signals,
        signal_created=signal_created,
        recorder=recorder,
        order_client=order_client,
        deps=deps,
    )


async def _send(
    *,
    base: dict[str, Any],
    request: PlaceRequest,
    contract: Contract,
    effect: str,
    notional: Decimal,
    trading_day: str,
    attribution: FuturesAttribution,
    signals: Any,
    signal_created: bool,
    recorder: FuturesPlaceRecorder,
    order_client: Any,
    deps: FuturesPlaceDeps,
) -> dict[str, Any]:
    client_order_id = build_client_order_id(
        contract_symbol=contract.contract_symbol,
        side=request.side,
        price=request.price,
        quantity=request.quantity,
        trading_day=trading_day,
        rung=request.rung,
    )
    base["client_order_id"] = client_order_id
    facts = OrderFacts(
        client_order_id=client_order_id,
        contract_symbol=contract.contract_symbol,
        broker_symbol=contract.broker_symbol,
        product_code=contract.product.code,
        contract_month=contract.contract_month,
        side=request.side,
        position_effect=effect,
        operation_kind="place",
        quantity=request.quantity,
        price=request.price,
        amount=notional,
        trading_day=trading_day,
        correlation_id=attribution.correlation_id,
        strategy=attribution.strategy,
    )

    try:
        reservation = await recorder.reserve(
            idempotency_key=client_order_id,
            symbol=contract.contract_symbol,
            side=request.side,
        )
    except DuplicateOrderIntent:
        status = None
        try:
            status = await recorder.existing_status(client_order_id)
        except Exception:  # noqa: BLE001
            logger.warning("futures mock duplicate-intent status read failed")
        await _not_sent(
            signals,
            attribution,
            created=signal_created,
            reason="duplicate_intent",
            detail={"status": "not_submitted", "client_order_id": client_order_id},
        )
        return {
            **base,
            "success": False,
            "status": "refused",
            "error_code": "duplicate_intent",
            "existing_ledger_status": status,
            "reconcile_required": status in (None, "uncertain"),
        }
    except Exception as exc:  # noqa: BLE001 - 가드를 증명하지 못하면 보내지 않는다
        await _not_sent(
            signals,
            attribution,
            created=signal_created,
            reason="send_intent_unavailable",
            detail={"status": "not_submitted", "error_class": type(exc).__name__},
        )
        return _refused(base, "send_intent_unavailable", detail=type(exc).__name__)

    async def release_and_refuse(code: str, detail: str | None) -> dict[str, Any]:
        released = await _release(recorder, reservation)
        await _not_sent(
            signals,
            attribution,
            created=signal_created,
            reason="pre_dispatch_error",
            detail={
                "status": "not_submitted",
                "error_code": code,
                "reservation_released": released,
            },
        )
        response = _refused(base, code, detail=detail)
        response["reservation_released"] = released
        return response

    # 예약 뒤·POST 전: 시간이 흘렀으므로 세션을 한 번 더 본다 (순수·무네트워크).
    late = session_refusal(contract, deps.now())
    if late is not None:
        return await release_and_refuse(late.code, late.detail)

    try:
        response = await order_client.place(
            account=deps.account_no(),
            symbol=contract.broker_symbol,
            side=request.side,
            quantity=request.quantity,
            price=request.price,
        )
    except FuturesPreDispatchError as exc:
        # 요청이 만들어지기 전에 멈췄음이 증명된다 — 같은 키로 다시 시도할 수 있다.
        return await release_and_refuse("pre_dispatch_error", exc.stage)
    except Exception as exc:  # noqa: BLE001 - 송신됐을 수 있다: 예약 유지, 재전송 없음
        return await _record_outcome(
            base,
            recorder,
            signals,
            attribution,
            facts,
            outcome="uncertain",
            response=None,
            order_no=None,
            reason=f"dispatch_unknown:{type(exc).__name__}",
        )
    outcome, order_no = classify_response(response)
    return await _record_outcome(
        base,
        recorder,
        signals,
        attribution,
        facts,
        outcome=outcome,
        response=response,
        order_no=order_no,
        reason=None,
    )


async def _release(
    recorder: FuturesPlaceRecorder, reservation: SendReservation
) -> bool:
    try:
        return bool(await recorder.release(reservation))
    except Exception:  # noqa: BLE001 - 풀지 못하면 예약이 남는다 (보수적)
        logger.warning("futures mock reservation release failed")
        return False


async def _record_outcome(
    base: dict[str, Any],
    recorder: FuturesPlaceRecorder,
    signals: Any,
    attribution: FuturesAttribution | None,
    facts: OrderFacts,
    *,
    outcome: str,
    response: Any,
    order_no: str | None,
    reason: str | None,
) -> dict[str, Any]:
    """브로커가 답한(또는 답을 알 수 없는) 뒤의 원장 기록. 이 함수는 재전송하지 않는다."""
    raw = _raw(response)
    code = response.get("msg_cd") if isinstance(response, dict) else None
    message = response.get("msg1") if isinstance(response, dict) else None
    response_dict: dict[str, Any] = {
        **base,
        "dispatch_started": True,
        "status": outcome,
        "success": outcome == "accepted",
    }
    if outcome == "accepted":
        response_dict["broker_order_id"] = order_no
        # accepted 는 체결이 아니다 — 체결은 T4 통보·T5 대사가 증명한다.
        response_dict["filled"] = False
    else:
        response_dict["retry_allowed"] = False
        if outcome == "uncertain":
            response_dict["reconcile_required"] = True
    try:
        if outcome == "uncertain":
            receipt = await recorder.record_uncertain(
                facts,
                broker_order_id=None,
                response_code=code if isinstance(code, str) else None,
                response_message=message if isinstance(message, str) else None,
                raw_response=raw,
                reason=reason or "acceptance_uncertain",
            )
        else:
            receipt = await recorder.record_send(
                facts,
                status=outcome,
                broker_order_id=order_no,
                response_code=code if isinstance(code, str) else None,
                response_message=message if isinstance(message, str) else None,
                raw_response=raw,
            )
        response_dict["ledger_id"] = receipt.ledger_id
    except Exception as exc:  # noqa: BLE001 - FuturesLedgerPayloadConflict 포함
        # 브로커는 이미 답했다: 예외를 올려 재시도를 부르지 않고, 원장 미기록을 드러낸다.
        logger.error(
            "futures mock ledger write failed after send: %s", type(exc).__name__
        )
        response_dict.update(
            ledger_recorded=False,
            reconcile_required=True,
            retry_allowed=False,
            ledger_error_class=type(exc).__name__,
        )
    if signals is not None and attribution is not None:
        try:
            await signals.mark_outcome(
                correlation_id=attribution.correlation_id,
                outcome_state="submitted" if outcome == "accepted" else "failed",
                detail_patch={
                    "outcome": outcome,
                    "client_order_id": facts.client_order_id,
                },
            )
        except Exception:  # noqa: BLE001 - 귀속은 이미 durable
            logger.warning("futures mock signal outcome mark failed")
    return response_dict


async def cancel_futures_mock_order(
    *,
    broker_order_id: str,
    confirm: bool = False,
    deps: FuturesPlaceDeps | None = None,
) -> dict[str, Any]:
    """오늘 이 레인이 보낸 place 주문 하나의 전량 취소 (원주문당 한 번).

    대상은 자기 원장의 당일 place 행이어야 하고(브로커 주문번호만으로 지정할 수 없다),
    ``accepted``/``pending``/``partial`` 상태여야 한다. ``uncertain`` 은 주문번호가 증명되지
    않았으므로 취소하지 않는다. 귀속 사슬 밖이다(취소는 위험을 줄인다) — 가드는 그대로다.
    """
    base: dict[str, Any] = {
        "account_mode": C.ACCOUNT_MODE,
        "operation": "cancel",
        "broker_order_id": broker_order_id,
        "dry_run": not confirm,
        "dispatch_started": False,
    }
    try:
        C.assert_limits_locked()
    except C.LimitsNotLocked:
        return _refused(base, "limits_not_locked")
    if not _SAFE_ORDER_NO.fullmatch(broker_order_id or ""):
        return _refused(base, "invalid_order_no")
    if confirm and os.environ.get(C.ENABLED_ENV) != "true":
        return _refused(base, "env_gate_disabled")
    deps = deps or default_deps()
    order_client = deps.new_order_client()
    try:
        if confirm:
            identity = identity_error(order_client, deps.account_no())
            if identity is not None:
                return _refused(base, identity)
        now = deps.now()
        trading_day = kst_trading_day(now)
        recorder = deps.recorder()
        try:
            original = await recorder.get_original_order(
                trading_day=trading_day, broker_order_id=broker_order_id
            )
        except Exception as exc:  # noqa: BLE001
            return _refused(base, "ledger_unavailable", detail=type(exc).__name__)
        if original is None:
            return _refused(base, "original_order_unknown")
        if original.status not in ("accepted", "pending", "partial"):
            return _refused(base, "original_not_cancellable", detail=original.status)
        base.update(contract_symbol=original.contract_symbol)
        contract = Contract(
            broker_symbol=original.broker_symbol,
            contract_symbol=original.contract_symbol,
            product=C.MINI_KOSPI200,
            contract_month=original.contract_month,
        )
        if original.product_code != C.MINI_KOSPI200.code:
            return _refused(base, "product_not_allowed")
        session = session_refusal(contract, now)
        if session is not None:
            return _refusal(base, session)
        if not confirm:
            return {**base, "success": True, "status": "preview", "preview": True}
        return await _send_cancel(
            base=base,
            original=original,
            contract=contract,
            trading_day=trading_day,
            recorder=recorder,
            order_client=order_client,
            deps=deps,
        )
    finally:
        await _close(order_client)


async def _send_cancel(
    *,
    base: dict[str, Any],
    original: OriginalOrder,
    contract: Contract,
    trading_day: str,
    recorder: FuturesPlaceRecorder,
    order_client: Any,
    deps: FuturesPlaceDeps,
) -> dict[str, Any]:
    client_order_id = build_cancel_client_order_id(
        original_client_order_id=original.client_order_id
    )
    base["client_order_id"] = client_order_id
    facts = OrderFacts(
        client_order_id=client_order_id,
        contract_symbol=original.contract_symbol,
        broker_symbol=original.broker_symbol,
        product_code=original.product_code,
        contract_month=original.contract_month,
        side=original.side,
        position_effect=original.position_effect,
        operation_kind="cancel",
        quantity=1,
        price=original.price,
        amount=original.amount,
        trading_day=trading_day,
        original_order_id=original.broker_order_id,
    )
    try:
        reservation = await recorder.reserve(
            idempotency_key=client_order_id,
            symbol=original.contract_symbol,
            side=original.side,
        )
    except DuplicateOrderIntent:
        return {
            **base,
            "success": False,
            "status": "refused",
            "error_code": "duplicate_intent",
            "reconcile_required": True,
        }
    except Exception as exc:  # noqa: BLE001
        return _refused(base, "send_intent_unavailable", detail=type(exc).__name__)

    late = session_refusal(contract, deps.now())
    if late is not None:
        released = await _release(recorder, reservation)
        return {**_refusal(base, late), "reservation_released": released}
    try:
        response = await order_client.cancel(
            account=deps.account_no(),
            original_order_no=original.broker_order_id,
        )
    except FuturesPreDispatchError as exc:
        released = await _release(recorder, reservation)
        return {
            **_refused(base, "pre_dispatch_error", detail=exc.stage),
            "reservation_released": released,
        }
    except Exception as exc:  # noqa: BLE001 - 송신됐을 수 있다
        return await _record_outcome(
            base,
            recorder,
            None,
            None,
            facts,
            outcome="uncertain",
            response=None,
            order_no=None,
            reason=f"dispatch_unknown:{type(exc).__name__}",
        )
    outcome, order_no = classify_response(response)
    return await _record_outcome(
        base,
        recorder,
        None,
        None,
        facts,
        outcome=outcome,
        response=response,
        order_no=order_no,
        reason=None,
    )


__all__ = (
    "FuturesPlaceDeps",
    "PlaceRequest",
    "cancel_futures_mock_order",
    "classify_response",
    "default_deps",
    "identity_error",
    "place_futures_mock_order",
)
