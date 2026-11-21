"""hk 5946 — 선물 모의 예약·원장·귀속 writer 의 PostgreSQL 증명.

CI 전용(``integration``): 로컬에서 돌리지 않는다. 행은 테스트마다 무작위 키로 만들고
``finally`` 에서 지운다 — 공유 스키마는 부트스트랩된 그대로 남는다.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.core.db import AsyncSessionLocal, engine
from app.core.timezone import KST
from app.models.kis_futures_mock import (
    KISFuturesMockOrderLedger,
    KISFuturesMockSignalLedger,
)
from app.models.review import OrderSendIntent
from app.services.kis_futures_mock import constants as C  # noqa: N812
from app.services.kis_futures_mock.attribution import (
    FuturesAttribution,
    FuturesSignalRecorder,
    SignalStrategyConflict,
    mark_signal_not_sent,
    mark_signal_outcome,
    record_signal,
)
from app.services.kis_futures_mock.ledger_service import (
    DuplicateOrderIntent,
    FuturesLedgerPayloadConflict,
    FuturesMockOrderLedgerService,
    FuturesPlaceRecorder,
    OrderFacts,
)
from app.services.kis_futures_mock.place import (
    FuturesPlaceDeps,
    PlaceRequest,
    place_futures_mock_order,
)
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _day() -> str:
    return f"{uuid.uuid4().int % 90000000 + 10000000}"  # 시험용 가짜 거래일 (충돌 방지)


def _facts(key: str, trading_day: str, **kw) -> OrderFacts:
    base = {
        "client_order_id": key,
        "contract_symbol": "MF200.2612",
        "broker_symbol": "A05612",
        "product_code": "MF200",
        "contract_month": "2026-12",
        "side": "buy",
        "position_effect": "open",
        "operation_kind": "place",
        "quantity": 1,
        "price": Decimal("400.00"),
        "amount": Decimal("20000000.00"),
        "trading_day": trading_day,
        "correlation_id": f"corr-{key}",
        "strategy": "operator-smoke",
    }
    base.update(kw)
    return OrderFacts(**base)


def _ack(order_no: str | None = None) -> dict:
    return {
        "status": "accepted",
        "broker_order_id": order_no or f"T{uuid.uuid4().hex[:10]}",
        "response_code": "0",
        "response_message": "ok",
        "raw_response": {"rt_cd": "0"},
    }


async def _cleanup(*, keys=(), days=(), correlations=()) -> None:
    async with AsyncSessionLocal() as session:
        for key in keys:
            await session.execute(
                delete(KISFuturesMockOrderLedger).where(
                    KISFuturesMockOrderLedger.client_order_id == key
                )
            )
            await session.execute(
                delete(OrderSendIntent).where(
                    OrderSendIntent.account_scope == C.RESERVATION_SCOPE,
                    OrderSendIntent.idempotency_key == key,
                )
            )
        for day in days:
            await session.execute(
                delete(KISFuturesMockOrderLedger).where(
                    KISFuturesMockOrderLedger.trading_day == day
                )
            )
        for cid in correlations:
            await session.execute(
                delete(KISFuturesMockSignalLedger).where(
                    KISFuturesMockSignalLedger.correlation_id == cid
                )
            )
        await session.commit()


# --- 예약 (order_send_intents, 자기 scope) ---------------------------------------
async def test_reservation_is_unique_per_key_and_releases_only_the_observed_row():
    key = f"kfm-{uuid.uuid4().hex[:16]}"
    recorder = FuturesPlaceRecorder(AsyncSessionLocal)
    try:
        first = await recorder.reserve(
            idempotency_key=key, symbol="MF200.2612", side="buy"
        )
        with pytest.raises(DuplicateOrderIntent):
            await recorder.reserve(idempotency_key=key, symbol="MF200.2612", side="buy")
        # 다른 키의 release 는 이 예약을 건드리지 못한다.
        stranger = first.__class__(first.row_id, "kfm-other", first.side)
        assert await recorder.release(stranger) == 0
        assert await recorder.release(first) == 1
        again = await recorder.reserve(
            idempotency_key=key, symbol="MF200.2612", side="buy"
        )
        assert again.row_id != first.row_id
    finally:
        await _cleanup(keys=[key])


async def test_reservation_scope_is_separate_from_the_stock_mock_scopes():
    key = f"kfm-{uuid.uuid4().hex[:16]}"
    recorder = FuturesPlaceRecorder(AsyncSessionLocal)
    try:
        async with AsyncSessionLocal() as session:
            session.add(OrderSendIntent(account_scope="kis_mock", idempotency_key=key))
            await session.commit()
        await recorder.reserve(idempotency_key=key, symbol="MF200.2612", side="buy")
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(OrderSendIntent).where(OrderSendIntent.idempotency_key == key)
            )
            await session.commit()


# --- 원장 writer ------------------------------------------------------------------
async def test_record_send_persists_replays_and_never_writes_fill_columns():
    key, day = f"kfm-{uuid.uuid4().hex[:16]}", _day()
    try:
        async with AsyncSessionLocal() as session:
            first = await FuturesMockOrderLedgerService(session).record_send(
                _facts(key, day), **_ack("0000012345")
            )
            first_id = first.id
            assert (
                first.status == "accepted" and first.account_mode == "kis_futures_mock"
            )
            assert first.filled_qty is None and first.realized_pnl_krw is None
            assert first.amount == Decimal("20000000.00") and first.currency == "KRW"
        async with AsyncSessionLocal() as other:
            again = await FuturesMockOrderLedgerService(other).record_send(
                _facts(key, day), **_ack("0000099999")
            )
            assert again.id == first_id and again.broker_order_id == "0000012345"
        async with AsyncSessionLocal() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(KISFuturesMockOrderLedger)
                    .where(KISFuturesMockOrderLedger.client_order_id == key)
                )
            ) == 1
    finally:
        await _cleanup(keys=[key])


async def test_same_key_with_other_content_is_a_conflict_not_an_overwrite():
    key, day = f"kfm-{uuid.uuid4().hex[:16]}", _day()
    try:
        async with AsyncSessionLocal() as session:
            await FuturesMockOrderLedgerService(session).record_send(
                _facts(key, day), **_ack()
            )
        async with AsyncSessionLocal() as session:
            with pytest.raises(FuturesLedgerPayloadConflict):
                await FuturesMockOrderLedgerService(session).record_send(
                    _facts(key, day, price=Decimal("400.02")), **_ack()
                )
    finally:
        await _cleanup(keys=[key])


async def test_uncertain_row_has_no_order_number_and_replays():
    key, day = f"kfm-{uuid.uuid4().hex[:16]}", _day()
    try:
        async with AsyncSessionLocal() as session:
            service = FuturesMockOrderLedgerService(session)
            first = await service.record_uncertain(_facts(key, day), reason="x")
            again = await service.record_uncertain(_facts(key, day), reason="x")
            assert first.status == "uncertain" and first.broker_order_id is None
            assert again.id == first.id
        async with AsyncSessionLocal() as session:
            assert (
                await FuturesMockOrderLedgerService(session).get_status(key)
                == "uncertain"
            )
    finally:
        await _cleanup(keys=[key])


async def test_accepted_without_an_order_number_is_refused_in_service_and_by_check():
    key, day = f"kfm-{uuid.uuid4().hex[:16]}", _day()
    try:
        async with AsyncSessionLocal() as session:
            with pytest.raises(ValueError):
                await FuturesMockOrderLedgerService(session).record_send(
                    _facts(key, day), **{**_ack(), "broker_order_id": None}
                )
        async with AsyncSessionLocal() as session:
            session.add(_orm_row(key, day, status="accepted", broker_order_id=None))
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await _cleanup(keys=[key])


def _orm_row(key: str, day: str, **overrides) -> KISFuturesMockOrderLedger:
    values = {
        "trade_date": datetime.now(KST),
        "trading_day": day,
        "contract_symbol": "MF200.2612",
        "broker_symbol": "A05612",
        "product_code": "MF200",
        "contract_month": "2026-12",
        "side": "buy",
        "order_type": "limit",
        "position_effect": "open",
        "operation_kind": "place",
        "quantity": Decimal(1),
        "price": Decimal("400.00"),
        "amount": Decimal("20000000"),
        "currency": "KRW",
        "client_order_id": key,
        "idempotency_key": key,
        "account_mode": "kis_futures_mock",
        "broker": "kis",
        "status": "pending",
    }
    values.update(overrides)
    return KISFuturesMockOrderLedger(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": Decimal(2)},  # 1계약 잠금
        {"amount": Decimal("50000001")},  # 명목 상한 잠금
        {"order_type": "market"},  # 지정가 전용
        {"product_code": "F200"},  # 닫힌 상품 집합
        {"account_mode": "kis_mock"},
        {"broker": "kiwoom"},
        {"currency": "USD"},
        {"status": "filled_maybe"},
        {"operation_kind": "modify"},
        {"operation_kind": "cancel", "original_order_id": None},  # 취소는 원주문 필수
        {"price": Decimal(0)},
        {"position_effect": "roll"},
        {"side": "short"},
    ],
)
async def test_database_refuses_rows_that_break_the_locked_invariants(overrides):
    key = f"kfm-{uuid.uuid4().hex[:16]}"
    try:
        async with AsyncSessionLocal() as session:
            session.add(_orm_row(key, _day(), **overrides))
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await _cleanup(keys=[key])


async def test_notional_boundary_row_is_accepted():
    key = f"kfm-{uuid.uuid4().hex[:16]}"
    try:
        async with AsyncSessionLocal() as session:
            session.add(_orm_row(key, _day(), amount=Decimal("50000000")))
            await session.commit()
    finally:
        await _cleanup(keys=[key])


async def test_broker_order_number_is_unique_per_day():
    keys = [f"kfm-{uuid.uuid4().hex[:16]}" for _ in range(2)]
    day = _day()
    try:
        async with AsyncSessionLocal() as session:
            session.add(_orm_row(keys[0], day, status="accepted", broker_order_id="N1"))
            await session.commit()
        async with AsyncSessionLocal() as session:
            session.add(_orm_row(keys[1], day, status="accepted", broker_order_id="N1"))
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await _cleanup(keys=keys, days=[day])


# --- 일일 손실 상태 ---------------------------------------------------------------
async def test_daily_loss_state_is_unverified_until_settlement_evidence_exists():
    day = _day()
    keys = [f"kfm-{uuid.uuid4().hex[:16]}" for _ in range(3)]
    try:
        async with AsyncSessionLocal() as session:
            state = await FuturesMockOrderLedgerService(session).daily_loss_state(
                trading_day=day
            )
        assert state.unverified is False and state.realized_loss_krw == Decimal(0)

        async with AsyncSessionLocal() as session:
            service = FuturesMockOrderLedgerService(session)
            await service.record_send(_facts(keys[0], day), **_ack())
        async with AsyncSessionLocal() as session:
            state = await FuturesMockOrderLedgerService(session).daily_loss_state(
                trading_day=day
            )
        assert state.unverified is True  # 체결 증거·정산이 없는 accepted

        # T5 가 쓰게 될 열을 시험에서만 직접 채운다.
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    select(KISFuturesMockOrderLedger).where(
                        KISFuturesMockOrderLedger.client_order_id == keys[0]
                    )
                )
            ).scalar_one()
            row.realized_pnl_krw = Decimal("-1500000")
            await session.commit()
        async with AsyncSessionLocal() as session:
            state = await FuturesMockOrderLedgerService(session).daily_loss_state(
                trading_day=day
            )
        assert state.unverified is False and state.realized_loss_krw == Decimal(
            "1500000"
        )

        # rejected(체결 없음)와 cancel 행은 unverified 를 만들지 않는다.
        async with AsyncSessionLocal() as session:
            service = FuturesMockOrderLedgerService(session)
            await service.record_send(
                _facts(keys[1], day, correlation_id=None),
                status="rejected",
                broker_order_id=None,
                response_code="E",
                response_message="no",
                raw_response=None,
            )
            await service.record_send(
                _facts(keys[2], day, operation_kind="cancel", original_order_id="O1"),
                **_ack(),
            )
        async with AsyncSessionLocal() as session:
            state = await FuturesMockOrderLedgerService(session).daily_loss_state(
                trading_day=day
            )
        assert state.unverified is False
    finally:
        await _cleanup(keys=keys, days=[day])


async def test_original_order_lookup_is_scoped_to_the_day_and_place_rows():
    key, day = f"kfm-{uuid.uuid4().hex[:16]}", _day()
    recorder = FuturesPlaceRecorder(AsyncSessionLocal)
    try:
        async with AsyncSessionLocal() as session:
            await FuturesMockOrderLedgerService(session).record_send(
                _facts(key, day), **_ack("0000077777")
            )
        found = await recorder.get_original_order(
            trading_day=day, broker_order_id="0000077777"
        )
        assert found is not None and found.client_order_id == key
        assert found.status == "accepted" and found.amount == Decimal("20000000.0000")
        assert (
            await recorder.get_original_order(
                trading_day=_day(), broker_order_id="0000077777"
            )
            is None
        )
    finally:
        await _cleanup(keys=[key], days=[day])


# --- 귀속 신호 --------------------------------------------------------------------
async def test_signal_is_idempotent_and_never_relabelled_to_another_strategy():
    cid = f"live:kis_futures_mock:{uuid.uuid4().hex[:16]}"
    attribution = FuturesAttribution(cid, "operator-smoke", "manual-cli")
    common = {
        "contract_symbol": "MF200.2612",
        "broker_symbol": "A05612",
        "side": "buy",
        "quantity": 1,
        "price": Decimal("400.00"),
        "kst_date": "2026-11-24",
    }
    try:
        async with AsyncSessionLocal() as session:
            first = await record_signal(session, attribution=attribution, **common)
        async with AsyncSessionLocal() as session:
            replay = await record_signal(session, attribution=attribution, **common)
        assert (
            first.created is True
            and replay.created is False
            and replay.row_id == first.row_id
        )
        async with AsyncSessionLocal() as session:
            with pytest.raises(SignalStrategyConflict):
                await record_signal(
                    session, attribution=FuturesAttribution(cid, "other", "x"), **common
                )
    finally:
        await _cleanup(correlations=[cid])


async def test_signal_check_refuses_blank_attribution_columns():
    for field in ("strategy", "signal_source", "correlation_id"):
        cid = f"live:kis_futures_mock:{uuid.uuid4().hex[:16]}"
        values = {
            "correlation_id": cid,
            "strategy": "s",
            "signal_source": "x",
            "account_mode": "kis_futures_mock",
            "contract_symbol": "MF200.2612",
            "broker_symbol": "A05612",
            "decision": "order",
            "kst_date": "2026-11-24",
        }
        values[field] = "   "
        try:
            async with AsyncSessionLocal() as session:
                session.add(KISFuturesMockSignalLedger(**values))
                with pytest.raises(IntegrityError):
                    await session.commit()
        finally:
            await _cleanup(correlations=[cid, "   "])


async def test_not_sent_mark_is_a_compare_and_set_on_recorded_only():
    cid = f"live:kis_futures_mock:{uuid.uuid4().hex[:16]}"
    attribution = FuturesAttribution(cid, "operator-smoke", "manual-cli")
    try:
        async with AsyncSessionLocal() as session:
            await record_signal(
                session,
                attribution=attribution,
                contract_symbol="MF200.2612",
                broker_symbol="A05612",
                side="buy",
                quantity=1,
                price=Decimal("400.00"),
                kst_date="2026-11-24",
            )
        async with AsyncSessionLocal() as session:
            assert (
                await mark_signal_outcome(
                    session, correlation_id=cid, outcome_state="submitted"
                )
                == 1
            )
        async with AsyncSessionLocal() as session:
            # 이미 submitted 인 행은 되돌리지 않는다 (중복 의도 거부가 닿을 수 있다).
            assert (
                await mark_signal_not_sent(
                    session, correlation_id=cid, reason="duplicate_intent"
                )
                == 0
            )
            state = await session.scalar(
                select(KISFuturesMockSignalLedger.outcome_state).where(
                    KISFuturesMockSignalLedger.correlation_id == cid
                )
            )
        assert state == "submitted"
        async with AsyncSessionLocal() as session:
            with pytest.raises(ValueError):
                await mark_signal_not_sent(
                    session, correlation_id=cid, reason="made_up"
                )
    finally:
        await _cleanup(correlations=[cid])


# --- 파이프라인 + 실제 DB: 귀속 → 예약 → 한 번 전송 → 원장 -------------------------
class _View:
    async def futures_master(self):
        return [
            {
                "product_type": "B",
                "short_code": "A05612",
                "standard_code": "x",
                "name": "미니F 202612",
                "atm": "",
                "strike": "0",
                "month_type": "1",
                "underlying_code": "2001",
                "underlying_name": "KOSPI200",
            }
        ]

    async def futures_balance(self, account):
        return {"positions": [], "summary": {}}

    async def close(self):
        return None


class _Client:
    is_mock_client = True
    base_url = C.MOCK_HOST

    def __init__(self):
        self.sends = 0

    async def place(self, **_):
        self.sends += 1
        return {"rt_cd": "0", "output": {"ODNO": f"D{uuid.uuid4().hex[:9]}"}}

    async def close(self):
        return None


async def test_pipeline_commits_signal_and_reservation_before_the_single_send(
    monkeypatch,
):
    monkeypatch.setenv(C.ENABLED_ENV, "true")
    client = _Client()
    now = datetime(2026, 11, 24, 10, 0, tzinfo=KST)
    deps = FuturesPlaceDeps(
        now=lambda: now,
        open_read_view=_View,
        new_order_client=lambda: client,
        account_no=lambda: "12345678",
        recorder=lambda: FuturesPlaceRecorder(AsyncSessionLocal),
        signal_recorder=lambda: FuturesSignalRecorder(AsyncSessionLocal),
    )
    rung = uuid.uuid4().hex[:8]  # 같은 날 같은 내용의 시험 간 충돌 방지
    request = PlaceRequest(
        "A05612", "buy", Decimal("400.00"), 1, strategy="operator-smoke", rung=rung
    )
    keys, cids = [], []
    try:
        first = await place_futures_mock_order(request, confirm=True, deps=deps)
        keys.append(first["client_order_id"])
        cids.append(first["correlation_id"])
        assert first["status"] == "accepted" and client.sends == 1

        async with AsyncSessionLocal() as session:
            signal = (
                await session.execute(
                    select(KISFuturesMockSignalLedger).where(
                        KISFuturesMockSignalLedger.correlation_id
                        == first["correlation_id"]
                    )
                )
            ).scalar_one()
            assert (
                signal.outcome_state == "submitted"
                and signal.strategy == "operator-smoke"
            )
            intent = await session.scalar(
                select(func.count())
                .select_from(OrderSendIntent)
                .where(
                    OrderSendIntent.account_scope == "kis_futures_mock",
                    OrderSendIntent.idempotency_key == first["client_order_id"],
                )
            )
            assert intent == 1
            row = (
                await session.execute(
                    select(KISFuturesMockOrderLedger).where(
                        KISFuturesMockOrderLedger.client_order_id
                        == first["client_order_id"]
                    )
                )
            ).scalar_one()
            assert row.status == "accepted" and row.filled_qty is None

        # 같은 claim 을 다시 보내려 하면 전송 없이 거부된다.
        second = await place_futures_mock_order(request, confirm=True, deps=deps)
        assert second["status"] == "refused" and second["error_code"] in {
            "duplicate_intent",
            "daily_loss_unverified",  # 첫 주문이 정산 전이라 진입이 먼저 막힐 수도 있다
            "max_open_positions_exceeded",
        }
        assert client.sends == 1
    finally:
        await _cleanup(keys=keys, correlations=cids)
        # trading_day 는 실제 KST 날짜가 아니라 시험의 고정 시각(20261124)이다.
        await _cleanup(days=["20261124"])
