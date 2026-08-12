"""``kiwoom_mock`` cycle orchestration — §39차 한시 KR venue.

Same skeleton as :mod:`scripts.b0x.kr.cycle`, with three deliberate divergences
that all come from the venue being *stronger*, not weaker:

    writer lock → RTH gate → table (or zero orders) → account truth
                → 귀속 → kill switch → derive → plan → submit → cancel → reconcile

1. **자기 미체결 = 브로커 직접 조회** (``kt00007``). The kis lane's contract
   v1.6 ① ledger exception is *not* used and must not be — it is scoped to a
   venue with no pending surface. See :mod:`scripts.b0x.kr.kiwoom`.
2. **취소가 실제로 가능하다.** The kis lane records
   ``KILL_TRIPPED_CANCEL_UNSUPPORTED`` because ``TTTC8036R`` cannot even
   discover what is resting. Kiwoom can, so this lane cancels — and the confirm
   path's round trip is *mandatory*, not conditional (see
   :data:`ROUND_TRIP_MANDATORY_NOTE`).
3. **No account-wide durable lease exists for this account.** The kis lane
   holds ``KISMockWriterLease``; there is no kiwoom equivalent in this repo and
   this module does not invent one. What it does instead is check the account's
   own **same-day order rows** for activity B0-X did not author
   (:func:`foreign_same_day_orders`) — broker evidence, which is strictly
   better than the kis lane's ledger shadow, and which is also the empirical
   form of the "KR-B1 이 지금 주문을 내고 있으면 착수하지 마라" gate. It is a
   *detection*, not a lock: exclusivity itself remains an operator action
   (KR-B1 비활성 확인).

§39차 ③ — legacy 불가침
------------------------

``kiwoom_mock`` 의 operator lane 은 KR-B1 이고 B0-X 는 한시 공존이다. The
account may carry holdings B0-X never created; they are read, named, and
excluded from every derivation input. The gate is #1835's, with a kiwoom-native
evidence source (:mod:`scripts.b0x.kr.kiwoom_attribution`). NAV follows the
same precedent: ``cash + 자기 귀속 평가금액`` only — the direction that can never
widen the ``pct_of_nav`` kill threshold.

§39차 ④ — envelope · session
------------------------------

The §4 KR column is reused **byte-identical** (``load_envelope("kr")`` →
``KR_MOCK_ENVELOPE``): 30만 · 신규×5 · 동시 10 · 일 3 · −2.5% NAV. No new
envelope constant exists for this lane, precisely so no number can drift.
Session is KRX RTH only; the underlying order client rejects ``NXT``/``SOR``
before any network call and this lane never offers the choice.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from app.services.kis_mock_runner.session import is_krx_regular_session
from scripts.b0x.broker_truth import (
    OwnPendingResubmitBlocked,
    PendingUnreadable,
)
from scripts.b0x.cycle import base_record, render_cycle_report
from scripts.b0x.derivation import DerivationResult, derive_orders
from scripts.b0x.envelope import Envelope, assert_envelope_locked, load_envelope
from scripts.b0x.kill_switch import evaluate as evaluate_kill_switch
from scripts.b0x.kr import attribution as kr_attribution
from scripts.b0x.kr import kiwoom as kiwoom_lane
from scripts.b0x.kr import kiwoom_attribution as kiwoom_attr
from scripts.b0x.labels import account_history_labels, header_labels
from scripts.b0x.ledger import (
    DEFAULT_OBSERVATION_DIR,
    ObservationLedger,
    writer_lock,
)
from scripts.b0x.state import LaneAccountState
from scripts.b0x.table_source import (
    DEFAULT_TABLE_DIR,
    PolicyTable,
    TableUnavailable,
    load_policy_table,
)

MARKET = "kr"
LANE = kiwoom_lane.LANE

# Provenance is KR-lane-local on purpose (same reasoning as
# ``scripts.b0x.kr.cycle``): the shared ``scripts.b0x.contract`` stamp still
# describes the untouched US/crypto lanes.
KR_CONTRACT_VERSION: Final[str] = "v1.6"
KR_CONTRACT_CLAUSES: Final[dict[str, str]] = {
    "§8 v1.6": (
        "KR 자기 미체결의 kis_mock_order_ledger 예외는 **브로커 표면 부재** 한정. "
        "kiwoom_mock 은 kt00007 미체결조회를 지원하므로 이 예외를 쓰지 않는다 "
        "(§39차 2항)."
    ),
    "§39차 ①②③④⑤": (
        "별도 모듈 · 브로커 직접 미체결 · legacy 불가침 귀속 게이트 · "
        "§4 KR 열 수치 불변 + KRX RTH only · 제출→조회→취소→reconcile 완전 왕복."
    ),
}
KR_ACCOUNT_MAP_COMMIT: Final[str] = "09c3fc16fcacbc47060e8aa1aa3e8beed0a74028"
KR_ACCOUNT_MAP_VALUES: Final[dict[str, str]] = {
    "account_lanes.kiwoom_mock": "KR-B1",
    "b0x_adapter_orders_20260808.surfaces": "∋ kiwoom_mock (§39차 한시, KRX RTH only)",
    "b0x_adapter_orders_20260808.writer": "b0x_adapter_single",
    "coexistence": (
        "B0-X 는 공존 배정이다 — KR-B1 주문 발행과 동시 사용 금지, KR-B1 재가동 시 "
        "재결정, kis_mock 복구 시 복귀"
    ),
    "surface": "kiwoom_mock",
}
KR_STATUS_LABEL: Final[str] = (
    "OBSERVATION_DERIVATION_ONLY — kiwoom_mock cycle 지위는 유지된다. 이 수동 mock "
    "acceptance lever는 모의 자동매매 가동 선언이나 스케줄러가 아니다."
)

#: This account is not B0-X's alone, and the artifact must say so every time.
COEXISTENCE_LABEL: Final[str] = (
    "COEXISTING_ACCOUNT_LANE — kiwoom_mock 의 operator lane 은 KR-B1 이고 B0-X 는 "
    "§39차 한시 공존 배정이다. 이 계좌의 보유·체결 이력 전부를 B0-X 산출로 읽으면 "
    "안 된다: 자기 저널(b0xkw)에 귀속되지 않은 보유는 legacy 로 분리 기록되며 "
    "매도/물타기 파생에 들어가지 않는다."
)

ZeroOrderReason = str

OUTSIDE_RTH_REASON: ZeroOrderReason = "outside_krx_regular_session"

#: NW-B4: every observation authorizing a confirmed dispatch is taken inside
#: this bounded interval. A contract requirement, not a CLI parameter.
PREFLIGHT_MAX_AGE_SECONDS = 5 * 60

#: One-shot manual acceptance lever. Restrictive operational bound with no CLI
#: or environment override.
MANUAL_CONFIRM_SUBMISSION_LIMIT = 1

#: 🔴 There is no flag that skips the cancel. Recorded on every confirm cycle so
#: a reader cannot mistake a resting order for an intended position.
ROUND_TRIP_MANDATORY_NOTE: Final[str] = (
    "ROUND_TRIP_MANDATORY — confirm 경로는 제출 후 반드시 취소를 시도하고 "
    "브로커 재조회로 확인한다. 취소를 건너뛰는 플래그는 존재하지 않으며, 취소 "
    "미확인은 실패(RoundTripIncomplete)로 기록되고 exit code 2 로 나간다."
)

#: Kill-trip cancellation is *supported* here — the kis lane's
#: ``KILL_TRIPPED_CANCEL_UNSUPPORTED`` literal must never appear on this lane.
KILL_CANCEL_SUPPORTED_NOTE: Final[str] = (
    "신규 주문 중단. 🔴 kiwoom 은 kt00007 미체결조회와 kt10003 취소를 지원하므로 "
    "kis_mock 의 「구조적 시도 불가」가 여기서는 성립하지 않는다. 다만 이 사이클은 "
    "kill 발화로 파생 자체가 0 이라 취소 대상이 생기지 않았다 — 이 레인은 confirm "
    "경로에서 자기 주문을 항상 같은 사이클 안에서 회수한다(ROUND_TRIP_MANDATORY). "
    "재개는 운영자 결정 (계약 §2-4)."
)

#: Realized P&L has no source on this lane either — stated so a reader does not
#: read a structural absence as a measured zero.
KR_REALIZED_PNL_UNAVAILABLE: Final[str] = (
    "realized_pnl_today has no source on this lane — B0-X fills are not "
    "reconciled into a P&L ledger, so the −2.5% NAV kill cannot fire. "
    "Not a measured zero."
)

KR_ATTRIBUTED_NAV_BASIS: Final[str] = (
    "nav = cash + 자기 귀속 평가금액(지분 비례). legacy 평가금액은 제외 — §4 "
    "daily_loss_kill 이 pct_of_nav 이므로 legacy 를 포함하면 kill 이 발화하는 "
    "절대 손실액이 커진다(임계가 넓어진다). 귀속 불가면 자기 평가금액 0 → "
    "nav = cash 로 더 좁아진다. 어느 경우에도 넓어지지 않는다 (#1835 선례 동일 방향)."
)

#: 🔴 The caller that did not wire the attribution reader. Not "nothing is
#: mine" — "nobody checked whose it is". Both directions close.
ATTRIBUTION_NOT_WIRED: Final[kr_attribution.AttributionUnreadable] = (
    kr_attribution.AttributionUnreadable(
        reason="kiwoom_mock_own_fill_attribution_not_wired",
        detail=(
            "호출자가 자기 귀속 리더를 배선하지 않았다 — 어떤 보유가 자기 것인지 "
            "확인되지 않았으므로 legacy 로 취급한다(매도/물타기 파생 0) + §4 상한 "
            "입력은 계좌 전체. 「미배선」을 「귀속 없음」으로도 「legacy 없음」으로도 "
            "읽지 않는다 (§39차 ③)"
        ),
    )
)


@dataclass
class KiwoomCycleOutcome:
    lane: str
    at: dt.datetime
    zero_order_reason: str | None = None
    table_hash: str | None = None
    table_generated_at: str | None = None
    table_age_seconds: int | None = None
    derivation: DerivationResult | None = None
    record: dict[str, Any] = field(default_factory=dict)
    artifact_path: Path | None = None
    exit_code: int = 0

    @property
    def order_count(self) -> int:
        return 0 if self.derivation is None else len(self.derivation.orders)


def _table_or_reason(
    *, now: dt.datetime, table_dir: Path
) -> tuple[PolicyTable | None, TableUnavailable | None]:
    result = load_policy_table(market=MARKET, now=now, table_dir=table_dir)
    if isinstance(result, TableUnavailable):
        return None, result
    return result, None


def halted_suspect_symbols(table: PolicyTable) -> tuple[str, ...]:
    """Symbols the KR table builder excluded as ``halted_suspect`` (ROB-1236).

    The builder already drops these rows, so this is a second, independent
    read of the same evidence used as a submission-boundary assertion. A
    halted-suspect symbol reaching a real order would mean the table's own
    exclusion silently regressed.
    """

    universe = table.payload.get("universe")
    if not isinstance(universe, dict):
        return ()
    raw = universe.get("halted_suspect")
    if not isinstance(raw, list):
        return ()
    symbols: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            symbols.append(entry.strip())
        elif isinstance(entry, dict):
            symbols.append(str(entry.get("symbol") or "").strip())
    return tuple(sorted({symbol for symbol in symbols if symbol}))


def scoped_positions(
    *,
    fresh: kiwoom_lane.FreshTruth,
    attribution: (
        kr_attribution.OwnFillAttribution | kr_attribution.AttributionUnreadable | None
    ),
) -> kr_attribution.ScopedPositions:
    """One place where 자기 보유 / legacy 보유 is decided for this lane."""

    return kr_attribution.scope_positions(
        positions=fresh.positions,
        attribution=ATTRIBUTION_NOT_WIRED if attribution is None else attribution,
        account_wide_non_dust=fresh.non_dust_position_symbols(),
        min_trade_unit=kiwoom_lane.KRX_MIN_TRADE_UNIT_SHARES,
    )


def broker_state(
    *,
    fresh: kiwoom_lane.FreshTruth,
    pending: kiwoom_lane.BrokerPending | PendingUnreadable,
    attribution: (
        kr_attribution.OwnFillAttribution | kr_attribution.AttributionUnreadable | None
    ) = None,
) -> LaneAccountState:
    """kiwoom_mock account state, derived entirely from this cycle's reads.

    🔴 ``positions`` carries **only** attributed holdings; legacy holdings are
    excluded from derivation, from the §4 cap inputs and from NAV, for the
    reasons #1835 set out and this lane inherits verbatim (see
    :data:`KR_ATTRIBUTED_NAV_BASIS` and the module docstring).

    🔴 ``broker_truth.own_pending`` is broker-derived in every branch —
    ``kt00007``'s answer or the tri-state sentinel when that read failed. There
    is no ledger path here.
    """

    scoped = scoped_positions(fresh=fresh, attribution=attribution)
    return LaneAccountState(
        lane=LANE,
        quote_currency=kiwoom_lane.QUOTE_CURRENCY,
        cash=fresh.cash,
        broker_truth=kiwoom_lane.broker_truth_from(
            position_symbols=scoped.cap_position_symbols, pending=pending
        ),
        positions=scoped.own_positions,
        # Same as the kis lane: a broker snapshot carries cost basis, not
        # cumulative deployment, so derivation refuses additions rather than
        # sizing them against a figure that shrinks on a partial sell.
        cumulative_deployment_readable=False,
        realized_pnl_today=Decimal("0"),
        nav=fresh.cash + scoped.attributed_evaluation,
    )


def _persist_outcome(
    *,
    outcome: KiwoomCycleOutcome,
    ledger: ObservationLedger,
    record: dict[str, Any],
    labels: tuple[str, ...],
) -> KiwoomCycleOutcome:
    ledger.record_cycle(record)
    outcome.record = record
    outcome.artifact_path = ledger.write_artifact(
        name=f"{outcome.at.strftime('%Y%m%dT%H%M%SZ')}-cycle.md",
        content=render_cycle_report(record, labels=labels),
    )
    return outcome


def _finish_zero_order(
    *,
    outcome: KiwoomCycleOutcome,
    ledger: ObservationLedger,
    record: dict[str, Any],
    labels: tuple[str, ...],
    reason: str,
    detail: str,
) -> KiwoomCycleOutcome:
    record["zero_order_reason"] = reason
    record["zero_order_detail"] = detail
    record["orders"] = []
    record["skipped"] = []
    record["planned"] = []
    record["blocked"] = []
    record["submitted"] = []
    outcome.zero_order_reason = reason
    return _persist_outcome(
        outcome=outcome, ledger=ledger, record=record, labels=labels
    )


@dataclass(frozen=True, slots=True)
class ForeignSameDayOrders:
    """Same-day order rows on this account that B0-X did not author.

    🔴 The empirical form of "KR-B1 이 지금 주문을 내고 있으면 착수하지 마라".
    Anything here fails the confirm preflight: the account map grants B0-X a
    *coexisting* seat, not a shared writer seat, and same-day foreign order
    activity is the observable signature of a second writer.
    """

    order_ids: tuple[str, ...]
    symbols: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.order_ids)

    def canonical(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "order_ids": list(self.order_ids),
            "symbols": list(self.symbols),
        }


async def foreign_same_day_orders(
    account: kiwoom_lane.ReadOnlyKiwoomMockAccount,
    *,
    own_order_ids: frozenset[str],
    order_date: str,
) -> ForeignSameDayOrders | PendingUnreadable:
    """kt00007 for today → rows whose ``ord_no`` is not in our journal."""

    try:
        rows = await account.read_order_detail(order_date=order_date)
    except Exception as exc:  # noqa: BLE001 — unreadable ≠ clean
        return kiwoom_lane.pending_unreadable(f"kt00007:{type(exc).__name__}")
    foreign_ids: list[str] = []
    foreign_symbols: set[str] = set()
    for row in rows:
        order_id = str(row.get("order_id") or "").strip()
        if not order_id or order_id in own_order_ids:
            continue
        foreign_ids.append(order_id)
        symbol = str(row.get("symbol") or "").strip()
        if symbol:
            foreign_symbols.add(symbol)
    return ForeignSameDayOrders(
        order_ids=tuple(sorted(foreign_ids)), symbols=tuple(sorted(foreign_symbols))
    )


def _confirm_preflight_record(
    *,
    started_at: dt.datetime,
    completed_at: dt.datetime,
    account_identity: dict[str, str],
    fresh: kiwoom_lane.FreshTruth,
    scoped: kr_attribution.ScopedPositions,
    pending: kiwoom_lane.BrokerPending | PendingUnreadable,
    foreign: ForeignSameDayOrders | PendingUnreadable,
) -> dict[str, Any]:
    """NW-B4 account truth, rendered without balances or account numbers.

    🔴 Unlike the kis lane, the open-order block here is a **native broker
    answer**, not a ledger shadow. That is the §39차 ② point and the record must
    show it, because a future reader comparing the two lanes' artifacts has to
    be able to tell which evidence each one actually had.
    """

    elapsed_seconds = (completed_at - started_at).total_seconds()
    pending_unreadable = pending if isinstance(pending, PendingUnreadable) else None
    foreign_unreadable = foreign if isinstance(foreign, PendingUnreadable) else None
    position_symbols = fresh.non_dust_position_symbols()
    reasons: list[str] = []

    if elapsed_seconds > PREFLIGHT_MAX_AGE_SECONDS:
        reasons.append("preflight_exceeded_5_minutes")
    if fresh.cash <= 0:
        reasons.append("cash_not_positive")
    if scoped.unreadable is not None:
        # 🔴 「보유가 있다」가 아니라 「누구 것인지 모른다」가 실패 사유다.
        reasons.append("attribution_unreadable")
    if pending_unreadable is not None:
        reasons.append("broker_pending_unreadable")
    elif pending.account_symbols:  # type: ignore[union-attr]
        # A resting order on this account — ours or KR-B1's — means the
        # account is not in the state this acceptance lever assumes.
        reasons.append("account_has_resting_orders")
    if foreign_unreadable is not None:
        reasons.append("foreign_same_day_trace_unreadable")
    elif foreign.count:  # type: ignore[union-attr]
        reasons.append("CONTAMINATED_foreign_same_day_orders_kr_b1_active_suspect")

    return {
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "max_age_seconds": PREFLIGHT_MAX_AGE_SECONDS,
        "account": account_identity,
        "cash": {"present": bool(fresh.cash > 0)},
        "positions": {
            "non_dust_symbols": list(position_symbols),
            "count": len(position_symbols),
            "own_attributed_symbols": [pos.symbol for pos in scoped.own_positions],
            "own_attributed_count": len(scoped.own_positions),
            # 🔴 legacy 는 조용히 지우지 않는다 — 「무시」 ≠ 「삭제」.
            "legacy_symbols": list(scoped.legacy_symbols),
            "legacy_count": len(scoped.legacy_symbols),
        },
        "attribution": {
            "source": kiwoom_attr.ATTRIBUTION_SOURCE,
            "readable": scoped.unreadable is None,
            "cap_basis": scoped.cap_basis,
            "unreadable": (
                None if scoped.unreadable is None else scoped.unreadable.canonical()
            ),
        },
        "open_orders": {
            "native_broker": {
                "available": True,
                "api": "kt00007 계좌별주문체결내역상세요청 (ord_remnq>0)",
                "note": (
                    "🔴 kis_mock 과 달리 브로커가 직접 답한다 — 계약 v1.6 ① 원장 "
                    "예외 미사용 (§39차 2항). 게이트는 kt00007 이다: kt00009 는 "
                    "미체결이 있어도 빈 배열을 반환함이 2026-08-12 실측되어 "
                    "진단(order_status_diagnostic)으로만 기록한다"
                ),
                "answer": (
                    pending_unreadable.canonical()
                    if pending_unreadable is not None
                    else pending.canonical()  # type: ignore[union-attr]
                ),
            },
            "ledger_shadow": None,
        },
        "foreign_same_day_orders": (
            foreign_unreadable.canonical()
            if foreign_unreadable is not None
            else foreign.canonical()  # type: ignore[union-attr]
        ),
        "writer_lease": {
            "acquired": False,
            "surface": "b0x_adapter",
            "note": (
                "이 계좌에는 kis_mock 의 KISMockWriterLease 같은 durable lease 가 "
                "없다. 대신 (a) B0-X 프로세스 간 flock writer_lock, (b) 브로커 "
                "당일 주문 흔적 기반 외부 writer 탐지를 쓴다. 계좌 배타성 자체는 "
                "운영 조치(KR-B1 비활성 확인)로만 확보된다 — lease 가 있다고 "
                "주장하지 않는다."
            ),
        },
        "passed": not reasons,
        "reasons": reasons,
    }


def _preflight_truth_unavailable_record(
    *,
    started_at: dt.datetime,
    completed_at: dt.datetime,
    account_identity: dict[str, str],
    error: Exception,
) -> dict[str, Any]:
    return {
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "elapsed_seconds": round((completed_at - started_at).total_seconds(), 3),
        "max_age_seconds": PREFLIGHT_MAX_AGE_SECONDS,
        "account": account_identity,
        "cash": {"present": None},
        "positions": {
            "non_dust_symbols": None,
            "count": None,
            "own_attributed_symbols": None,
            "own_attributed_count": None,
            "legacy_symbols": None,
            "legacy_count": None,
        },
        "attribution": None,
        "open_orders": {"native_broker": None, "ledger_shadow": None},
        "foreign_same_day_orders": None,
        "writer_lease": {"acquired": False, "surface": "b0x_adapter"},
        "passed": False,
        "reasons": ["account_truth_unavailable"],
        "error_type": type(error).__name__,
    }


async def _run_prepared_cycle(  # noqa: PLR0915 — linear cycle script, kept flat
    *,
    now: dt.datetime,
    table: PolicyTable,
    envelope: Envelope,
    labels: tuple[str, ...],
    outcome: KiwoomCycleOutcome,
    ledger: ObservationLedger,
    record: dict[str, Any],
    confirm: bool,
    account: kiwoom_lane.ReadOnlyKiwoomMockAccount | None,
    journal: kiwoom_attr.OwnOrderJournal,
    account_identity: dict[str, str] | None,
) -> KiwoomCycleOutcome:
    """Account truth → 귀속 → derive → plan → bounded confirm round trip."""

    if account is None:
        account = kiwoom_lane.ReadOnlyKiwoomMockAccount()

    preflight_started_at = dt.datetime.now(dt.UTC) if confirm else None
    try:
        fresh = await kiwoom_lane.read_fresh_truth(account)
    except Exception as exc:
        if not confirm:
            raise
        assert preflight_started_at is not None
        assert account_identity is not None
        record["preflight"] = _preflight_truth_unavailable_record(
            started_at=preflight_started_at,
            completed_at=dt.datetime.now(dt.UTC),
            account_identity=account_identity,
            error=exc,
        )
        return _finish_zero_order(
            outcome=outcome,
            ledger=ledger,
            record=record,
            labels=labels,
            reason="preflight_not_clean",
            detail="account_truth_unavailable",
        )

    # 🔴 Journal read failure is not "we authored nothing" — it propagates as
    # AttributionUnreadable below and as an empty own-id set here, which only
    # widens the pending superset (more blocking, never less).
    try:
        own_order_ids = journal.own_order_ids()
        journal_readable = True
    except Exception:  # noqa: BLE001
        own_order_ids = frozenset()
        journal_readable = False
    record["own_order_journal"] = {
        "path": str(journal.path),
        "readable": journal_readable,
        "recorded_order_count": len(own_order_ids),
    }

    pending = await kiwoom_lane.read_broker_pending(
        account, own_order_ids=own_order_ids
    )
    # 🔴 kt00009 is recorded, never gating — 2026-08-12 measured it answering
    # ``return_code=0`` with an empty array while B0-X orders were live. Keeping
    # the count in the artifact is what makes that claim checkable, and what
    # will show it changing if the mock is ever fixed.
    record["order_status_diagnostic"] = await account.read_order_status_diagnostic()
    record["fresh_truth"] = fresh.status_only(pending)
    record["own_pending_source"] = kiwoom_lane.OWN_PENDING_SOURCE
    record["own_pending_basis"] = kiwoom_lane.OWN_PENDING_BASIS

    attribution = await kiwoom_attr.read_own_attribution(
        journal=journal, read_order_detail=account.read_order_detail
    )
    scoped = scoped_positions(fresh=fresh, attribution=attribution)
    record["attribution"] = {
        **scoped.canonical(),
        "source": kiwoom_attr.ATTRIBUTION_SOURCE,
        "nav_basis": KR_ATTRIBUTED_NAV_BASIS,
    }

    halted = halted_suspect_symbols(table)
    record["halted_suspect"] = {
        "source": "policy_table.universe.halted_suspect (ROB-1236)",
        "symbols": list(halted),
        "count": len(halted),
    }

    if confirm:
        assert preflight_started_at is not None
        assert account_identity is not None
        foreign = await foreign_same_day_orders(
            account,
            own_order_ids=own_order_ids,
            order_date=kiwoom_attr.kst_order_date(preflight_started_at),
        )
        preflight = _confirm_preflight_record(
            started_at=preflight_started_at,
            completed_at=dt.datetime.now(dt.UTC),
            account_identity=account_identity,
            fresh=fresh,
            scoped=scoped,
            pending=pending,
            foreign=foreign,
        )
        record["preflight"] = preflight
        if not preflight["passed"]:
            return _finish_zero_order(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="preflight_not_clean",
                detail=", ".join(preflight["reasons"]),
            )

    state = broker_state(fresh=fresh, pending=pending, attribution=attribution)
    record["broker_truth"] = state.broker_truth.canonical()
    decision = evaluate_kill_switch(state=state, envelope=envelope)
    derivation = derive_orders(
        table=table,
        state=state,
        envelope=envelope,
        kill_switch=decision,
        lane_universe=None,
        apply_envelope=True,
    )
    outcome.derivation = derivation
    record.update(
        {
            "cycle_id": derivation.cycle_id,
            "account_state_hash": derivation.account_state_hash,
            "derivation_hash": derivation.derivation_hash(),
            "orders": [order.canonical() for order in derivation.orders],
            "skipped": [skip.canonical() for skip in derivation.skipped],
            "kill_switch": decision.canonical(),
        }
    )

    if decision.tripped:
        notice = decision.operator_notice(
            lane=LANE, remaining_orders_note=KILL_CANCEL_SUPPORTED_NOTE
        )
        if notice:
            ledger.record_notice(at=now, text=notice, lane=LANE)
            record["operator_notice"] = notice
        record["planned"] = []
        record["blocked"] = []
        record["submitted"] = []
        record["round_trip"] = []
        return _persist_outcome(
            outcome=outcome, ledger=ledger, record=record, labels=labels
        )

    held = {pos.symbol: pos.quantity for pos in state.positions}
    planned, blocked = kiwoom_lane.plan_orders(
        derivation.orders, envelope=envelope, held_quantities=held
    )
    record["planned"] = [order.to_json() for order in planned]
    record["blocked"] = [order.to_json() for order in blocked]

    round_trips: list[dict[str, Any]] = []
    if not confirm:
        record["submission_skipped"] = "confirm=False — preview only"
        record["submitted"] = []
        record["round_trip"] = []
        return _persist_outcome(
            outcome=outcome, ledger=ledger, record=record, labels=labels
        )

    record["round_trip_policy"] = ROUND_TRIP_MANDATORY_NOTE
    incomplete: kiwoom_lane.RoundTripIncomplete | None = None

    for index, order in enumerate(planned):
        if index >= MANUAL_CONFIRM_SUBMISSION_LIMIT:
            record["submission_stopped"] = (
                f"acceptance_submission_limit={MANUAL_CONFIRM_SUBMISSION_LIMIT}"
            )
            break
        submitted_at = dt.datetime.now(dt.UTC)
        if (
            preflight_started_at is None
            or (submitted_at - preflight_started_at).total_seconds()
            > PREFLIGHT_MAX_AGE_SECONDS
        ):
            record["submission_stopped"] = "preflight_expired"
            break

        # 🔴 §39차 ③ at the mutation boundary. A SELL may only ever reach shares
        # this lane's own fills paid for. Derivation already refused legacy
        # symbols (they are not in ``state.positions`` at all); this second line
        # is deliberately redundant, because a one-line regression there becomes
        # a sale of KR-B1's shares.
        if order.side == "sell":
            try:
                kr_attribution.assert_sell_is_own(
                    scoped,
                    symbol=order.symbol,
                    quantity=Decimal(order.quantity),
                    lane=LANE,
                )
            except kr_attribution.LegacyPositionSellBlocked as exc:
                record.setdefault("submission_blocked", []).append(
                    {
                        "symbol": order.symbol,
                        "correlation_id": order.client_order_id,
                        "reason": "legacy_position_sell_blocked",
                        "detail": str(exc),
                    }
                )
                record["submission_stopped"] = "legacy_position_sell_blocked"
                break
            # 🔴 The acceptance lever is buy-only. Even an attributed sell is
            # refused here rather than silently executed, because a sell that
            # fills cannot be taken back by the mandatory cancel.
            record.setdefault("submission_blocked", []).append(
                {
                    "symbol": order.symbol,
                    "correlation_id": order.client_order_id,
                    "reason": "sell_leg_not_wired_on_acceptance_lever",
                }
            )
            record["submission_stopped"] = "sell_leg_not_wired"
            break

        if order.symbol in halted:
            record.setdefault("submission_blocked", []).append(
                {
                    "symbol": order.symbol,
                    "correlation_id": order.client_order_id,
                    "reason": "halted_suspect_symbol",
                }
            )
            record["submission_stopped"] = "halted_suspect_symbol"
            break

        # Re-read the broker's resting set immediately before the dispatch: the
        # preflight snapshot is evidence, this is the mutation boundary.
        current_pending = await kiwoom_lane.read_broker_pending(
            account, own_order_ids=own_order_ids
        )
        current_truth = kiwoom_lane.broker_truth_from(
            position_symbols=scoped.cap_position_symbols, pending=current_pending
        )
        try:
            trip = await kiwoom_lane.submit_and_cancel(
                account,
                planned=order,
                broker_truth=current_truth,
                record_order_no=_journal_writer(journal),
                now=submitted_at,
            )
        except OwnPendingResubmitBlocked as exc:
            record.setdefault("submission_dedup_blocked", []).append(
                {
                    "symbol": order.symbol,
                    "correlation_id": order.client_order_id,
                    "reason": "pending_recheck_blocked",
                    "detail": str(exc),
                }
            )
            record["submission_stopped"] = "dedup_blocked"
            break
        except kiwoom_lane.RoundTripIncomplete as exc:
            incomplete = exc
            record["submission_stopped"] = "round_trip_incomplete"
            record.setdefault("round_trip_failures", []).append(str(exc))
            break
        round_trips.append(trip.canonical())

    record["round_trip"] = round_trips
    record["submitted"] = [
        {
            "correlation_id": trip["correlation_id"],
            "symbol": trip["symbol"],
            "order_no": trip["order_no"],
            "submitted": trip["submitted"],
        }
        for trip in round_trips
    ]
    if incomplete is not None:
        outcome.exit_code = 2
    return _persist_outcome(
        outcome=outcome, ledger=ledger, record=record, labels=labels
    )


def _journal_writer(journal: kiwoom_attr.OwnOrderJournal) -> Any:
    """Return the append callback :func:`kiwoom.submit_and_cancel` invokes.

    Kept as a tiny closure so the lane module owns *when* the journal is
    written (immediately after the broker returns an order number) while this
    module owns *where* it is written.
    """

    def _record(
        *, order_no: str, planned: kiwoom_lane.PlannedOrder, at: dt.datetime
    ) -> None:
        journal.append(
            kiwoom_attr.OwnOrderRecord(
                at=at.isoformat(),
                order_no=order_no,
                correlation_id=planned.client_order_id,
                symbol=planned.symbol,
                side=planned.side,
                price=planned.price,
                quantity=planned.quantity,
                order_date=kiwoom_attr.kst_order_date(at),
            )
        )

    return _record


def _stamp_contract_and_account_map(record: dict[str, Any]) -> None:
    record["contract"] = {
        "path": "~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md",
        "version": KR_CONTRACT_VERSION,
        "clauses": dict(KR_CONTRACT_CLAUSES),
    }
    record["account_map"] = {
        "repo": "auto_trader-operator",
        "commit": KR_ACCOUNT_MAP_COMMIT,
        "canonical_surface": "operator_contract.yaml",
        "reference_surface": "mock/CLAUDE.md",
        "gate_values": dict(KR_ACCOUNT_MAP_VALUES),
    }
    record["cycle_status"] = "OBSERVATION_DERIVATION_ONLY"


async def run_kiwoom_cycle(
    *,
    now: dt.datetime,
    table_dir: Path = DEFAULT_TABLE_DIR,
    out_dir: Path = DEFAULT_OBSERVATION_DIR,
    confirm: bool = False,
    account: kiwoom_lane.ReadOnlyKiwoomMockAccount | None = None,
    journal: kiwoom_attr.OwnOrderJournal | None = None,
) -> KiwoomCycleOutcome:
    """One manual kiwoom_mock B0-X cycle.

    ``confirm=False`` is the ordinary observation/derivation path. A confirm
    call is a separate, default-disabled manual surface requiring the B0-X env
    gate, the per-call ``confirm=True`` argument and a clean NW-B4 preflight
    inside KRX RTH — and it always completes the mandatory cancel round trip.
    """

    envelope = load_envelope(MARKET)
    assert_envelope_locked(envelope)
    kiwoom_lane.assert_correlation_prefixes_disjoint()
    labels = header_labels(
        lane=LANE,
        extra=(*account_history_labels(LANE), COEXISTENCE_LABEL, KR_STATUS_LABEL),
    )
    outcome = KiwoomCycleOutcome(lane=LANE, at=now)
    root = Path(out_dir).expanduser()
    lane_journal = (
        kiwoom_attr.OwnOrderJournal.for_lane(root=root, lane=LANE)
        if journal is None
        else journal
    )

    with writer_lock(lane=LANE, root=root):
        ledger = ObservationLedger(lane=LANE, root=root)
        ledger.ensure()

        record = base_record(
            market=MARKET, lane=LANE, now=now, envelope=envelope, labels=labels
        )
        _stamp_contract_and_account_map(record)
        record["confirm"] = confirm
        record["realized_pnl_source"] = KR_REALIZED_PNL_UNAVAILABLE

        # --- RTH gate: cheapest check, before any table/account I/O ---
        in_session = is_krx_regular_session(now)
        record["krx_regular_session"] = in_session
        record["session_policy"] = "KRX RTH only — NXT/SOR 주문 불가 (§39차 ④)"
        if not in_session:
            return _finish_zero_order(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason=OUTSIDE_RTH_REASON,
                detail=(
                    f"now={now.isoformat()} is outside the XKRX regular session "
                    "(contract: KRX RTH v1 — 정규장만, NXT/시간외 금지)"
                ),
            )

        table, unavailable = _table_or_reason(now=now, table_dir=Path(table_dir))
        if table is None:
            assert unavailable is not None
            return _finish_zero_order(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason=unavailable.reason,
                detail=unavailable.detail,
            )

        outcome.table_hash = table.policy_table_hash
        outcome.table_generated_at = table.generated_at.isoformat()
        outcome.table_age_seconds = int(table.age.total_seconds())
        record["policy_table_hash"] = table.policy_table_hash
        record["policy_table_path"] = str(table.path)
        record["policy_table_generated_at"] = table.generated_at.isoformat()
        record["policy_table_age_seconds"] = int(table.age.total_seconds())

        if not confirm:
            return await _run_prepared_cycle(
                now=now,
                table=table,
                envelope=envelope,
                labels=labels,
                outcome=outcome,
                ledger=ledger,
                record=record,
                confirm=False,
                account=account,
                journal=lane_journal,
                account_identity=None,
            )

        try:
            kiwoom_lane.assert_kiwoom_lane_enabled()
            account_identity = kiwoom_lane.account_identity_summary()
        except kiwoom_lane.KiwoomLaneDisabled as exc:
            return _finish_zero_order(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="confirm_gate_not_armed",
                detail=str(exc),
            )

        return await _run_prepared_cycle(
            now=now,
            table=table,
            envelope=envelope,
            labels=labels,
            outcome=outcome,
            ledger=ledger,
            record=record,
            confirm=True,
            account=account,
            journal=lane_journal,
            account_identity=account_identity,
        )


__all__ = [
    "MARKET",
    "LANE",
    "OUTSIDE_RTH_REASON",
    "PREFLIGHT_MAX_AGE_SECONDS",
    "MANUAL_CONFIRM_SUBMISSION_LIMIT",
    "ROUND_TRIP_MANDATORY_NOTE",
    "KILL_CANCEL_SUPPORTED_NOTE",
    "KR_CONTRACT_VERSION",
    "KR_ACCOUNT_MAP_COMMIT",
    "KR_ACCOUNT_MAP_VALUES",
    "KR_STATUS_LABEL",
    "COEXISTENCE_LABEL",
    "KR_REALIZED_PNL_UNAVAILABLE",
    "KR_ATTRIBUTED_NAV_BASIS",
    "ATTRIBUTION_NOT_WIRED",
    "ForeignSameDayOrders",
    "KiwoomCycleOutcome",
    "foreign_same_day_orders",
    "broker_state",
    "halted_suspect_symbols",
    "run_kiwoom_cycle",
    "scoped_positions",
]
