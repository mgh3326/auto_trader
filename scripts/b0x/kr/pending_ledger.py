"""KR 자기 미체결 출처 — ``review.kis_mock_order_ledger`` (계약 v1.6 ①).

Why this module exists, and why it is *not* a re-run of ``attributed_book``
---------------------------------------------------------------------------

Contract v1.5 ① made the §4 caps read from the broker each cycle. Two of the
three KR inputs read cleanly; the third — 자기(``b0xk``) 미체결 — cannot be read
from ``kis_mock`` **at all**:

* ``DomesticOrderClient.inquire_korea_orders`` (TR ``TTTC8036R``, 미체결 주문
  조회) raises outright for ``is_mock=True``.
* ``inquire_daily_order_domestic`` (daily-ccld) routes to a mock TR but ROB-341
  measured it returning ``rt_cd=0`` with **empty rows even after same-day mock
  order activity** — an empty answer proves nothing.

X-E1 therefore carried :data:`scripts.b0x.kr.mock.KR_PENDING_UNREADABLE` and
failed closed on every symbol, which is honest but leaves the lane deriving
zero orders forever. Contract **v1.6 ①** grants one narrow exception:

    ① kis_mock 한정 예외 — 브로커가 자기 미체결 조회 표면을 제공하지 않고
       + 제출 chokepoint 가 매번 강제 기록하는 원장에 한함.
       🔴 일반 원칙은 브로커 진실 유지. crypto·US 로 확대 금지.
    ② 용도 = 미체결 dedup / 캡 입력 **만**. 🔴 포지션 진실은 계속 브로커 조회.
    ③ 오류 방향 = 과잉 차단(체결·취소분이 pending 으로 보일 수 있음) — 안전한
       실패로 수용. 🔴 관대한 방향(누락)으로 기울면 위반.
    ④ ``PendingUnreadable`` 상태는 **유지**(원장 가용 시 해소).

The distinction that makes this legitimate, stated precisely because it is the
whole argument: ``attributed_book.json`` was rejected **not** because it was a
self-record but because it had *read paths only and no write path anywhere in
the repo* — every cycle loaded ``None``. This ledger is written by the
submission chokepoint itself, on the ``is_mock`` branch that precedes the POST:

* ``app/mcp_server/tooling/order_execution.py`` — ``_execute_and_record``
  opens with the kis_mock pre-submit attribution gate. Unattributed →
  ``MissingAttribution`` → the order never reaches the broker. Attributed →
  ``record_signal`` commits a ``review.kis_mock_signal_ledger`` row **before**
  the send, and a failure of *that* write also refuses the send
  (``error_code="signal_record_unavailable"``). The row's ``correlation_id`` /
  ``strategy`` / ``signal_source`` are NOT NULL with blank-rejecting CHECKs.
* The post-send ``review.kis_mock_order_ledger`` row (contract v1.6's named
  source) carries the same ``correlation_id``.

Both tables are read here, and the union is deliberate rather than redundant:
the *order* row can be lost (``LedgerWriteError`` → ``ledger_id=None`` →
``ledger_tracking_unavailable``), and a lost order row for an order that does
exist at the broker is exactly the 관대한 방향(누락) that v1.6 ③ forbids. The
pre-submit signal row cannot be lost without the send being refused, so it
closes that hole. Both are ``kis_mock``-scoped and written by the one chokepoint
v1.6 ① names — this is a wider *block*, not a wider *exception*.

What "pending" means here — a deliberate superset
--------------------------------------------------

🔴 This module does **not** inspect ``lifecycle_state`` and does not try to
decide whether a given order filled, was cancelled, or is still resting.
Inferring that would be the 관대한 방향: every such inference can release a
symbol that is in fact still working. So a symbol counts as pending when this
lane recorded *any* order-or-signal row for it inside the current KRX trading
day, whatever became of it. v1.6 ③ names that exact error direction
("체결·취소분이 pending 으로 보일 수 있음") and accepts it.

The single bound is the trading day, and it is structural rather than inferred:
KRX orders are day orders. ROB-671's classifier
(``app/services/brokers/kis/live_order_expiry.py`` — cited, never imported; the
KR AST guard forbids importing it) puts the *latest* possible expiry at 20:00
KST **on the accept day**, for every session × side combination including the
regular-session SELL that carries to NXT. An order accepted on KST day *D*
therefore cannot still be resting on KST day *D+1*. That is the same boundary
the §4 일일 신규 cap already uses, so the two agree by construction.

Failure is unreadable, never empty
-----------------------------------

Any failure to read — DB down, schema drift, anything — returns
:data:`scripts.b0x.kr.mock.KR_PENDING_UNREADABLE`'s sibling
:func:`ledger_unreadable`, i.e. the v1.5 ① ``PendingUnreadable`` state, which
:meth:`~scripts.b0x.broker_truth.BrokerTruth.resubmit_block` refuses every
symbol under. The tri-state is preserved exactly as X-E1 built it: 원장이
**가용할 때만** 해소된다. There is no code path in this module that turns a
failed read into ``()``.
"""

from __future__ import annotations

import datetime as dt
from typing import Final, Protocol

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.review import KISMockOrderLedger, KISMockSignalLedger
from scripts.b0x.broker_truth import PendingUnreadable

#: KST (UTC+9). Fixed offset — Korea observes no DST, and this module must not
#: depend on the host's local zone.
KST: Final[dt.timezone] = dt.timezone(dt.timedelta(hours=9))

#: ``account_mode`` both ledgers pin with a CHECK constraint.
ACCOUNT_MODE: Final[str] = "kis_mock"

#: ``PendingUnreadable.reason`` for a ledger that could not be read. Distinct
#: from ``kis_mock_pending_inquiry_unsupported`` (the broker-surface fact) so an
#: observation record shows *which* source failed, not merely that one did.
LEDGER_UNREADABLE_REASON: Final[str] = "kis_mock_ledger_pending_unreadable"


class PendingReader(Protocol):
    """The seam ``run_kr_cycle`` reads 자기 미체결 through."""

    async def __call__(
        self, *, now: dt.datetime, correlation_prefix: str
    ) -> tuple[str, ...] | PendingUnreadable: ...


def ledger_unreadable(cause: str) -> PendingUnreadable:
    """The v1.6 ④ fallback — the ledger could not answer, so nothing is known.

    ``cause`` must be an exception *type* name, never its message: a DB error
    message can carry a DSN with credentials, and this value is written into a
    durable observation record.
    """

    return PendingUnreadable(
        reason=LEDGER_UNREADABLE_REASON,
        detail=(
            f"review.kis_mock_order_ledger 조회 실패({cause}) — 브로커 미체결 "
            "표면은 이미 막혀 있다(TTTC8036R 는 is_mock=True 에서 raise, "
            "daily-ccld 는 당일 주문이 있어도 빈 행 가능, ROB-341 실측)이라 "
            "폴백이 없다. 「조회 불가」를 「미체결 없음」으로 취급하지 않는다 "
            "(계약 v1.6 ③④)"
        ),
    )


def kst_trading_day_start(now: dt.datetime) -> dt.datetime:
    """Midnight KST of the trading day containing ``now`` (tz-aware, KST).

    Naive input is read as UTC, matching ``app.core.timezone.trade_day_kst``'s
    own rule for legacy timestamps, so a host-local zone can never move the
    boundary.
    """

    moment = now.replace(tzinfo=dt.UTC) if now.tzinfo is None else now
    return moment.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)


def kst_trading_day_label(now: dt.datetime) -> str:
    """``YYYY-MM-DD`` in KST — the spelling ``kis_mock_signal_ledger.kst_date``
    stores (``record_signal``: ``now_kst().strftime("%Y-%m-%d")``)."""

    return kst_trading_day_start(now).strftime("%Y-%m-%d")


async def read_own_pending(
    *, now: dt.datetime, correlation_prefix: str
) -> tuple[str, ...] | PendingUnreadable:
    """자기 미체결 심볼 — contract v1.6 ①②, read-only.

    Returns the deliberate superset described in the module docstring, or
    :func:`ledger_unreadable` if either query fails for any reason. An empty
    tuple means *the ledgers answered and named nothing* — a real, readable
    "이 레인이 오늘 아무 주문도 넣지 않았다".

    🔴 Nothing here reads or returns positions. Position truth stays with the
    broker holdings read (``FreshTruth.non_dust_position_symbols``) per v1.6 ②.
    """

    since = kst_trading_day_start(now)
    kst_date = kst_trading_day_label(now)
    prefix = f"{correlation_prefix}%"

    try:
        async with AsyncSessionLocal() as db:
            # The contract's named source. No lifecycle_state predicate: see
            # the module docstring on why filtering by state is the forbidden
            # 관대한 방향.
            order_symbols = (
                await db.execute(
                    select(KISMockOrderLedger.symbol).where(
                        KISMockOrderLedger.account_mode == ACCOUNT_MODE,
                        KISMockOrderLedger.correlation_id.like(prefix),
                        KISMockOrderLedger.trade_date >= since,
                    )
                )
            ).scalars()
            # The pre-submit chokepoint row, which cannot be lost without the
            # send itself being refused — this is what closes the lost-native-
            # write hole the order table alone would leave open.
            signal_symbols = (
                await db.execute(
                    select(KISMockSignalLedger.symbol).where(
                        KISMockSignalLedger.account_mode == ACCOUNT_MODE,
                        KISMockSignalLedger.correlation_id.like(prefix),
                        KISMockSignalLedger.kst_date == kst_date,
                    )
                )
            ).scalars()
            symbols = {
                str(symbol).strip()
                for symbol in [*order_symbols, *signal_symbols]
                if str(symbol).strip()
            }
    except Exception as exc:  # noqa: BLE001 — any failure is "unreadable"
        return ledger_unreadable(type(exc).__name__)

    return tuple(sorted(symbols))


__all__ = [
    "ACCOUNT_MODE",
    "KST",
    "LEDGER_UNREADABLE_REASON",
    "PendingReader",
    "kst_trading_day_label",
    "kst_trading_day_start",
    "ledger_unreadable",
    "read_own_pending",
]
