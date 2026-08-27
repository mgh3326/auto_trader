"""``kiwoom_mock`` cycle orchestration — §39차 한시 KR venue.

Same skeleton as :mod:`scripts.b0x.kr.cycle`, with three deliberate divergences
that all come from the venue being *stronger*, not weaker:

    writer lock → RTH gate → table (or zero orders) → account truth
                → 귀속 → kill switch → derive → plan → mode-specific submit

1. **자기 미체결 = 브로커 직접 조회** (``kt00007``). The kis lane's contract
   v1.6 ① ledger exception is *not* used and must not be — it is scoped to a
   venue with no pending surface. See :mod:`scripts.b0x.kr.kiwoom`.
2. **Two mutation modes are deliberately separate.** ``ACCEPTANCE_ONLY``
   remains one submit → cancel → reconcile round trip. ``ORDERING`` submits
   every eligible envelope-derived DAY order and deliberately leaves it at the
   broker; it is never the default and it never inherits the acceptance cancel.
3. **ORDERING has a writer lease and a broker-truth mutation boundary.** The
   account-keyed lease is checked before every submit/cancel, and one fresh
   ``kt00007`` answer derives both pending and same-day foreign activity. A
   read failure, foreign trace, or lost lease closes that boundary before a
   mutation can leave the process.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

from app.services.kis_mock_runner.session import is_krx_regular_session
from app.services.mock_lane_registry import LaneRegistryEntry
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
from scripts.b0x.kr import kiwoom_ordering as ordering_support
from scripts.b0x.kr.kiwoom_coordination import (
    KIWOOM_COORDINATION_OWNER_ENTRY_REQUIRED,
    KiwoomCoordinationOwnerRejected,
    assert_kiwoom_coordination_owner,
)
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
KR_CONTRACT_VERSION: Final[str] = "v1.8"
KR_CONTRACT_FILE_SHA256_REFERENCE_ONLY: Final[str] = (
    "7d2729bc4197dd167d40e3e881b64f30a778b1b1a7158acf81fd0c7d38d008c0"
)
KR_CONTRACT_CLAUSES: Final[dict[str, str]] = {
    "§8 v1.8 (v1.5 ① KR amendment)": (
        "같은 사이클 = policy table hash + 제출 전 broker account state hash + "
        "locked envelope hash + lane으로 계산한 결정적 cycle_id. 동일 cycle_id·동일 "
        "symbol·BUY 다단만 묶음 직전 공용 재제출 게이트를 1회 검사한다. 후속 다리 "
        "직전 lease + kt00007 foreign trace 재검사는 유지한다. 다음 cycle의 기존 자기 "
        "미체결 차단, §4/§50 180만 cap, 일일 신규 3종목, SELL, 손실가드, crypto/US와 "
        "공용 게이트는 불변. 부분 실패는 accepted DAY 주문을 보상 취소하지 않고 "
        "보존하며 추가 mutation 중단 + partial_failure + exit 2로 기록한다."
    ),
    "§8 v1.6": (
        "KR 자기 미체결의 kis_mock_order_ledger 예외는 **브로커 표면 부재** 한정. "
        "kiwoom_mock 은 kt00007 미체결조회를 지원하므로 이 예외를 쓰지 않는다 "
        "(§39차 2항)."
    ),
    "§8 v1.7": (
        "「스케줄러 등록 없음」 개정 — **스케줄러 (Prefect)는 시각만 소유한다**: "
        "표 빌드 실행(KR 07:45·US 22:00)과 orch 기상 nudge(사이클 슬롯)에 한정. "
        "전략 판단·주문 파생·dispatch·워커 실행은 불변(orch/워커 소유, "
        "harvest-before-dispatch 유지). 근거 = 수동 원샷 장전 누락 4회 실측. "
        "실행 표면·envelope·승격 절차 무변경."
    ),
    "§39차 ①②③④⑤": (
        "별도 모듈 · 브로커 직접 미체결 · legacy 불가침 귀속 게이트 · "
        "§4 KR 열 수치 불변 + KRX RTH only · ACCEPTANCE_ONLY 제출→조회→취소→"
        "reconcile 보존 + ORDERING DAY lifecycle/readback/lease."
    ),
}
KR_ACCOUNT_MAP_COMMIT: Final[str] = "cbd8f86ba96b4235984e139a9e82e4a7620b5bf8"
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
type CycleMode = Literal["acceptance", "ordering"]

ACCEPTANCE_MODE: Final[CycleMode] = "acceptance"
ORDERING_MODE: Final[CycleMode] = "ordering"

PREVIEW_STATUS: Final[str] = "OBSERVATION_DERIVATION_ONLY"
ACCEPTANCE_ONLY_STATUS: Final[str] = "ACCEPTANCE_ONLY"
ORDERING_STATUS: Final[str] = "ORDERING"

PREVIEW_STATUS_LABEL: Final[str] = (
    "OBSERVATION_DERIVATION_ONLY — kiwoom_mock cycle 지위는 유지된다. 이 수동 mock "
    "acceptance lever는 모의 자동매매 가동 선언이나 스케줄러가 아니다."
)

#: Backward-compatible name for the preview-only label. Mutation paths select
#: their own literal below; no order-capable artifact may inherit this label.
KR_STATUS_LABEL: Final[str] = PREVIEW_STATUS_LABEL

ACCEPTANCE_ONLY_STATUS_LABEL: Final[str] = (
    "ACCEPTANCE_ONLY — 기존 1건 submit → cancel → reconcile 검증 경로. "
    "기본값은 preview 이며, 이 경로는 DAY 주문 잔존을 만들지 않는다."
)

ORDERING_STATUS_LABEL: Final[str] = (
    "ORDERING — kiwoom_mock 별도 DAY 주문 lifecycle 모드. 정책표 결정 파생만 §4 "
    "cap 안에서 제출하며, ACCEPTANCE_ONLY 왕복 취소를 상속하지 않는다. 각 mutation "
    "직전 account writer lease + kt00007 pending/당일 foreign trace를 fresh 재조회한다. "
    "일손실 입력 unreadable·foreign trace·read failure·lease loss는 주문 0; 매도는 "
    "자기 귀속 broker fill 수량까지만; kill은 자기 미체결 전부 취소 시도 후 재조회로 "
    "확인한다. KRX RTH only."
)

SAME_CYCLE_BUY_BATCH_POLICY: Final[str] = (
    "SAME_CYCLE_BUY_BATCH — 하나의 결정적 cycle_id에서 파생된 동일 symbol BUY "
    "다단만 한 묶음이다. 묶음 직전 broker truth로 공용 재제출 게이트를 정확히 "
    "1회 검사하고, 각 후속 다리 직전에는 writer lease + kt00007 foreign trace를 "
    "다시 검사한다. 다른 cycle_id·symbol·SELL은 이 경계를 사용할 수 없다."
)

PARTIAL_BATCH_RETAIN_POLICY: Final[str] = (
    "PARTIAL_BATCH_RETAIN — 묶음 도중 실패하면 이미 broker-acknowledged 된 DAY "
    "주문은 취소하지 않고 그대로 보존한다. 보상 취소는 별도 broker mutation이며 "
    "원실패보다 더 위험할 수 있다. 추가 제출은 즉시 중단하고 exit 2 + 부분 실패 "
    "라벨과 accepted/remaining order key를 산출물에 기록한다."
)

PARTIAL_BATCH_FAILURE_LABEL: Final[str] = (
    "PARTIAL_BATCH_FAILURE — 같은 사이클 BUY 묶음 일부만 broker acknowledgement를 "
    "받았다. 성공이 아니다. 이미 접수된 DAY 주문은 보상 취소하지 않았고 추가 "
    "mutation은 중단됐다."
)

ORDERING_REQUIREMENTS: Final[dict[str, str]] = {
    "table_only": "policy_table deterministic derivation within locked §4 envelope",
    "day": "default TIF is DAY; successful acknowledgement is never auto-cancelled",
    "lifecycle": "journal immediately after broker acknowledgement plus broker readback",
    "sell": "sell quantity is bounded by fresh attributed broker fills",
    "mutation_boundary": "fresh pending + same-day foreign trace before every mutation",
    "buy_batch": SAME_CYCLE_BUY_BATCH_POLICY,
    "partial_batch": PARTIAL_BATCH_RETAIN_POLICY,
    "lease": "account-keyed writer lease checked before every mutation",
    "pnl": "unreadable realized P&L input yields zero new orders",
    "kill": "kill cancels every own pending order and re-reads broker confirmation",
}

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

#: The legacy acceptance lever stays intentionally bounded.  ORDERING has its
#: own submission path and never consults this value.
ACCEPTANCE_SUBMISSION_LIMIT = 1

#: 🔴 There is no flag that skips the cancel on ACCEPTANCE_ONLY. Its record
#: makes the retained DAY-order behavior unavailable by accident.
ROUND_TRIP_MANDATORY_NOTE: Final[str] = (
    "ROUND_TRIP_MANDATORY — ACCEPTANCE_ONLY 경로는 제출 후 반드시 취소를 시도하고 "
    "브로커 재조회로 확인한다. 취소를 건너뛰는 플래그는 존재하지 않으며, 취소 "
    "미확인은 실패(RoundTripIncomplete)로 기록되고 exit code 2 로 나간다."
)

#: Kill-trip cancellation is *supported* here — the kis lane's
#: ``KILL_TRIPPED_CANCEL_UNSUPPORTED`` literal must never appear on this lane.
KILL_CANCEL_SUPPORTED_NOTE: Final[str] = (
    "신규 주문 중단. kiwoom ORDERING 은 kt00007로 자기 미체결을 재조회하고 각 "
    "잔량에 kt10003 취소를 시도한 뒤 broker 재조회로 남은 수량을 확인한다. 취소/"
    "재조회 실패는 성공으로 기록하지 않으며 재개는 운영자 결정 (계약 §2-4)."
)

DAY_ORDER_RETAINED_NOTE: Final[str] = (
    "DAY_ORDER_RETAINED — ORDERING 은 envelope 파생 주문을 DAY로 제출하고 "
    "성공 직후 자동 취소하지 않는다. broker_ack은 접수 증거이고 filled를 뜻하지 "
    "않으며, 후속 broker readback이 partial/fill/remaining을 보존한다."
)

REALIZED_PNL_UNAVAILABLE_REASON: Final[str] = "realized_pnl_unavailable"
COORDINATION_GRANT_UNAVAILABLE_REASON: Final[str] = "coordination_grant_unavailable"
_LIFECYCLE_BLOCK_REASONS: Final[frozenset[str]] = frozenset(
    {
        REALIZED_PNL_UNAVAILABLE_REASON,
        COORDINATION_GRANT_UNAVAILABLE_REASON,
        "unknown_pending_reconcile",
    }
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


def _render_kiwoom_cycle_report(
    record: dict[str, Any], *, labels: tuple[str, ...]
) -> str:
    """Add KR batch lifecycle state to the shared deterministic report."""

    base = render_cycle_report(record, labels=labels)
    batches = record.get("same_cycle_buy_batches") or []
    if not batches:
        return base
    lines = [
        base.rstrip(),
        "",
        "## Same-cycle BUY batches",
        "",
        "| cycle_id | symbol | status | planned | accepted keys | remaining keys | compensating cancel | failure |",
        "|---|---|---|---:|---|---|---|---|",
    ]
    for batch in batches:
        failure = batch.get("failure") or {}
        lines.append(
            f"| {batch.get('cycle_id', '-')} | {batch.get('symbol', '-')} | "
            f"`{batch.get('status', '-')}` | {batch.get('planned_count', 0)} | "
            f"{', '.join(batch.get('accepted_order_keys') or []) or '-'} | "
            f"{', '.join(batch.get('remaining_order_keys') or []) or '-'} | "
            f"{str(bool(batch.get('compensating_cancel_attempted'))).lower()} | "
            f"{failure.get('reason', '-')}:{failure.get('detail', '-')} |"
        )
    lines += ["", f"retention policy: {PARTIAL_BATCH_RETAIN_POLICY}", ""]
    return "\n".join(lines)


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
    realized_pnl_today: Decimal = Decimal("0"),
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
        realized_pnl_today=realized_pnl_today,
        nav=fresh.cash + scoped.attributed_evaluation,
    )


def _persist_outcome(
    *,
    outcome: KiwoomCycleOutcome,
    ledger: ObservationLedger,
    record: dict[str, Any],
    labels: tuple[str, ...],
) -> KiwoomCycleOutcome:
    artifact_labels = (
        (*labels, PARTIAL_BATCH_FAILURE_LABEL)
        if record.get("batch_submission_status") == "partial_failure"
        else labels
    )
    record["labels"] = list(artifact_labels)
    ledger.record_cycle(record)
    outcome.record = record
    outcome.artifact_path = ledger.write_artifact(
        name=f"{outcome.at.strftime('%Y%m%dT%H%M%SZ')}-cycle.md",
        content=_render_kiwoom_cycle_report(record, labels=artifact_labels),
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
    if reason in _LIFECYCLE_BLOCK_REASONS:
        record["lane_lifecycle_status"] = ordering_support.KIWOOM_LIFECYCLE_STATUS
        record["realized_pnl_numeric_substitute"] = None
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
    foreign_trace_readable = foreign_unreadable is None
    foreign_trace_count = None if foreign_unreadable is not None else foreign.count
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
        "kr_b1_inactive_gate": {
            "required": True,
            "source": "kt00007 same-day rows not authored by b0xkw journal",
            "foreign_trace_readable": foreign_trace_readable,
            "foreign_trace_count": foreign_trace_count,
            "passed": foreign_trace_readable and foreign_trace_count == 0,
            "fail_closed": (
                "foreign trace read failure is not clean; any unreadable answer "
                "or non-zero foreign trace produces zero orders"
            ),
            "residual_toctou": "preflight_once_before_submission",
        },
        "writer_lease": {
            "acquired": False,
            "surface": "ACCEPTANCE_ONLY",
            "note": (
                "이 retained acceptance lever는 ORDERING의 account-keyed lease를 "
                "획득하지 않는다. ORDERING은 별도 host-local fcntl lease와 매 "
                "mutation kt00007 foreign-trace boundary를 사용한다; 어느 쪽도 "
                "KR-B1 비활성 확인이라는 운영 조치를 대체하지 않는다."
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
        "kr_b1_inactive_gate": {
            "required": True,
            "source": "kt00007 same-day rows not authored by b0xkw journal",
            "foreign_trace_readable": False,
            "foreign_trace_count": None,
            "passed": False,
            "fail_closed": "account truth unavailable before KR-B1 inactive check",
            "residual_toctou": "preflight_once_before_submission",
        },
        "writer_lease": {
            "acquired": False,
            "surface": "ACCEPTANCE_ONLY",
            "note": "the retained acceptance lever does not claim ORDERING's "
            "account-keyed writer lease",
        },
        "passed": False,
        "reasons": ["account_truth_unavailable"],
        "error_type": type(error).__name__,
    }


async def _run_prepared_cycle(  # noqa: PLR0915 — retained acceptance path
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
    """Preserved ``ACCEPTANCE_ONLY`` submit → cancel → reconcile lever.

    This deliberately has no DAY-retention branch.  ORDERING is implemented in
    :func:`_run_ordering_cycle` below so changing its status literal cannot turn
    this historical acceptance workflow into a production-ordering workflow.
    """

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

    try:
        own_order_ids = journal.own_order_ids()
        journal_readable = True
    except Exception:  # noqa: BLE001 — unreadable evidence is not empty evidence
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
        record["planned"] = []
        record["blocked"] = []
        record["submitted"] = []
        record["round_trip"] = []
        return _persist_outcome(
            outcome=outcome, ledger=ledger, record=record, labels=labels
        )

    held = {pos.symbol: pos.quantity for pos in state.positions}
    planned, blocked = kiwoom_lane.plan_orders(
        derivation.orders,
        cycle_id=derivation.cycle_id,
        envelope=envelope,
        held_quantities=held,
    )
    record["planned"] = [order.to_json() for order in planned]
    record["blocked"] = [order.to_json() for order in blocked]
    if not confirm:
        record["submission_skipped"] = "confirm=False — preview only"
        record["submitted"] = []
        record["round_trip"] = []
        record["day_orders"] = []
        return _persist_outcome(
            outcome=outcome, ledger=ledger, record=record, labels=labels
        )

    record["round_trip_policy"] = ROUND_TRIP_MANDATORY_NOTE
    round_trips: list[dict[str, Any]] = []
    incomplete: kiwoom_lane.RoundTripIncomplete | None = None
    for index, order in enumerate(planned):
        if index >= ACCEPTANCE_SUBMISSION_LIMIT:
            record["submission_stopped"] = (
                f"acceptance_submission_limit={ACCEPTANCE_SUBMISSION_LIMIT}"
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
        if order.side == "sell":
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
            round_trips.append(trip.canonical())
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

    record["round_trip"] = round_trips
    record["day_orders"] = []
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


@dataclass(frozen=True, slots=True)
class MutationBoundary:
    """One coherent, immediately-pre-mutation broker observation.

    ``kt00007`` is queried exactly once for the boundary.  Both the account
    pending set and foreign same-day trace are derived from that same answer,
    preventing an accidental split-brain preflight where those facts came from
    two different broker moments.
    """

    at: dt.datetime
    pending: kiwoom_lane.BrokerPending | PendingUnreadable
    foreign: ForeignSameDayOrders | PendingUnreadable
    lease_held: bool
    blocking_reason: str | None = None
    detail: str | None = None

    @property
    def clean(self) -> bool:
        return (
            self.blocking_reason is None
            and not isinstance(self.pending, PendingUnreadable)
            and not isinstance(self.foreign, PendingUnreadable)
            and self.foreign.count == 0
        )

    def canonical(self, *, action: str) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "action": action,
            "lease_held": self.lease_held,
            "pending": self.pending.canonical(),
            "foreign_same_day_orders": self.foreign.canonical(),
            "clean": self.clean,
            "blocking_reason": self.blocking_reason,
            "detail": self.detail,
        }


def _foreign_same_day_orders_from_rows(
    *, rows: list[dict[str, Any]], own_order_ids: frozenset[str]
) -> ForeignSameDayOrders:
    """Pure foreign-trace projection from the same rows used for pending."""

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


async def _read_mutation_boundary(
    *,
    account: kiwoom_lane.ReadOnlyKiwoomMockAccount,
    journal: kiwoom_attr.OwnOrderJournal,
    lease: ordering_support.WriterLease,
    at: dt.datetime,
) -> MutationBoundary:
    """Fresh lease + one same-day ``kt00007`` read; any failure closes it."""

    try:
        lease.assert_held()
    except Exception as exc:  # noqa: BLE001 — lost authority cannot be retried open
        unreadable = kiwoom_lane.pending_unreadable(
            f"writer_lease:{type(exc).__name__}"
        )
        return MutationBoundary(
            at=at,
            pending=unreadable,
            foreign=unreadable,
            lease_held=False,
            blocking_reason="writer_lease_lost",
            detail=type(exc).__name__,
        )

    try:
        own_order_ids = journal.own_order_ids()
    except Exception as exc:  # noqa: BLE001 — corrupt ownership is not empty ownership
        unreadable = kiwoom_lane.pending_unreadable(
            f"own_order_journal:{type(exc).__name__}"
        )
        return MutationBoundary(
            at=at,
            pending=unreadable,
            foreign=unreadable,
            lease_held=True,
            blocking_reason="own_order_journal_unreadable",
            detail=type(exc).__name__,
        )

    try:
        rows = await account.read_order_detail(
            order_date=kiwoom_attr.kst_order_date(at)
        )
    except Exception as exc:  # noqa: BLE001 — failed foreign/pending read is closed
        unreadable = kiwoom_lane.pending_unreadable(f"kt00007:{type(exc).__name__}")
        return MutationBoundary(
            at=at,
            pending=unreadable,
            foreign=unreadable,
            lease_held=True,
            blocking_reason="mutation_boundary_read_unavailable",
            detail=type(exc).__name__,
        )

    pending = kiwoom_lane.broker_pending_from_detail_rows(
        rows=rows, own_order_ids=own_order_ids
    )
    foreign = _foreign_same_day_orders_from_rows(rows=rows, own_order_ids=own_order_ids)
    return MutationBoundary(
        at=at,
        pending=pending,
        foreign=foreign,
        lease_held=True,
        blocking_reason=("foreign_same_day_orders_present" if foreign.count else None),
        detail=(None if not foreign.count else f"foreign_order_count={foreign.count}"),
    )


def _append_ordering_event(
    *,
    journal: ordering_support.OrderingEventJournal,
    record: dict[str, Any],
    event: dict[str, Any],
) -> None:
    """Append lifecycle evidence before adding its non-authoritative summary."""

    journal.append(event)
    record.setdefault("fidelity_events", []).append(event)


def _finish_ordering_stopped(
    *,
    outcome: KiwoomCycleOutcome,
    ledger: ObservationLedger,
    record: dict[str, Any],
    labels: tuple[str, ...],
    reason: str,
    detail: str,
) -> KiwoomCycleOutcome:
    """Stop further mutations without erasing already broker-acknowledged work."""

    record["submission_stopped"] = reason
    record["no_further_mutations"] = True
    record.setdefault("ordering_failures", []).append(
        {"reason": reason, "detail": detail}
    )
    if record.get("submitted"):
        outcome.exit_code = 2
        return _persist_outcome(
            outcome=outcome, ledger=ledger, record=record, labels=labels
        )
    return _finish_zero_order(
        outcome=outcome,
        ledger=ledger,
        record=record,
        labels=labels,
        reason=reason,
        detail=detail,
    )


def _record_ordering_boundary(
    *,
    boundary: MutationBoundary,
    action: str,
    fidelity: ordering_support.OrderingEventJournal,
    record: dict[str, Any],
) -> None:
    evidence = boundary.canonical(action=action)
    record.setdefault("mutation_boundaries", []).append(evidence)
    _append_ordering_event(
        journal=fidelity,
        record=record,
        event={"at": boundary.at.isoformat(), "event": "mutation_boundary", **evidence},
    )


def _ordering_submission_groups(
    planned: list[kiwoom_lane.PlannedOrder],
) -> tuple[tuple[kiwoom_lane.PlannedOrder, ...], ...]:
    """Keep order sequence while grouping only contiguous same-symbol BUY legs."""

    groups: list[tuple[kiwoom_lane.PlannedOrder, ...]] = []
    index = 0
    while index < len(planned):
        first = planned[index]
        if first.side != "buy":
            groups.append((first,))
            index += 1
            continue
        end = index + 1
        while (
            end < len(planned)
            and planned[end].side == "buy"
            and planned[end].symbol == first.symbol
        ):
            end += 1
        groups.append(tuple(planned[index:end]))
        index = end
    return tuple(groups)


def _append_order_intent(
    *,
    order: kiwoom_lane.PlannedOrder,
    table_prices: dict[str, str],
    fidelity: ordering_support.OrderingEventJournal,
    record: dict[str, Any],
) -> None:
    intent = {
        "at": dt.datetime.now(dt.UTC).isoformat(),
        "event": "table_price_to_intended_limit",
        "cycle_id": order.cycle_id,
        "order_key": order.order_key,
        "correlation_id": order.client_order_id,
        "symbol": order.symbol,
        "side": order.side,
        "table_price": table_prices.get(order.order_key),
        "intended_limit": order.price,
        "quantity": order.quantity,
        "time_in_force": "DAY",
    }
    _append_ordering_event(journal=fidelity, record=record, event=intent)


async def _record_day_order_lifecycle(
    *,
    account: kiwoom_lane.ReadOnlyKiwoomMockAccount,
    order: kiwoom_lane.PlannedOrder,
    day_order: kiwoom_lane.DayOrderResult,
    cycle_now: dt.datetime,
    table_prices: dict[str, str],
    day_orders: list[dict[str, Any]],
    fidelity: ordering_support.OrderingEventJournal,
    record: dict[str, Any],
) -> tuple[str, str] | None:
    """Persist acknowledgement first, then require exact broker readback."""

    assert day_order.order_no is not None
    ack = {
        **day_order.canonical(),
        "cycle_id": order.cycle_id,
        "order_key": order.order_key,
        "table_price": table_prices.get(order.order_key),
    }
    day_orders.append(ack)
    _append_ordering_event(
        journal=fidelity,
        record=record,
        event={
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "event": "broker_ack",
            "cycle_id": order.cycle_id,
            "order_key": order.order_key,
            "broker_ack": ack,
        },
    )
    try:
        readback = await kiwoom_lane.read_order_readback(
            account,
            planned=order,
            order_no=day_order.order_no,
            order_date=kiwoom_attr.kst_order_date(cycle_now),
            at=dt.datetime.now(dt.UTC),
        )
    except (
        kiwoom_lane.BrokerOrderReadbackUnavailable,
        kiwoom_lane.BrokerEchoMismatch,
    ) as exc:
        ack["readback_failure"] = str(exc)
        return "broker_readback_unavailable", type(exc).__name__

    readback_evidence = readback.canonical()
    ack["broker_readback"] = readback_evidence
    _append_ordering_event(
        journal=fidelity,
        record=record,
        event={
            "at": readback.at.isoformat(),
            "event": "broker_readback_reconcile",
            "cycle_id": order.cycle_id,
            "order_key": order.order_key,
            "broker_readback": readback_evidence,
        },
    )
    return None


def _mark_buy_batch_failure(
    *,
    batch: dict[str, Any],
    record: dict[str, Any],
    reason: str,
    detail: str,
) -> str:
    accepted = list(batch["accepted_order_keys"])
    planned = list(batch["order_keys"])
    if 0 < len(accepted) < len(planned):
        status = "partial_failure"
    elif accepted:
        status = "incomplete_after_all_acknowledged"
    else:
        status = "failed_before_acknowledgement"
    batch.update(
        {
            "status": status,
            "accepted_count": len(accepted),
            "remaining_order_keys": [key for key in planned if key not in accepted],
            "failure": {"reason": reason, "detail": detail},
            "compensating_cancel_attempted": False,
            "retention_policy": PARTIAL_BATCH_RETAIN_POLICY,
        }
    )
    record["batch_submission_status"] = status
    return status


async def _submit_same_cycle_buy_batch(  # noqa: PLR0913
    *,
    account: kiwoom_lane.ReadOnlyKiwoomMockAccount,
    batch_orders: tuple[kiwoom_lane.PlannedOrder, ...],
    cycle_id: str,
    scoped: kr_attribution.ScopedPositions,
    halted: set[str],
    journal: kiwoom_attr.OwnOrderJournal,
    lease: ordering_support.WriterLease,
    coordination: ordering_support.KiwoomCoordinationAdapter | None,
    fidelity: ordering_support.OrderingEventJournal,
    record: dict[str, Any],
    day_orders: list[dict[str, Any]],
    table_prices: dict[str, str],
    cycle_now: dt.datetime,
) -> tuple[str, str, bool] | None:
    """Gate once, then submit one exact same-cycle/symbol BUY group in order."""

    batch = {
        "cycle_id": cycle_id,
        "symbol": batch_orders[0].symbol,
        "side": "buy",
        "order_keys": [order.order_key for order in batch_orders],
        "planned_count": len(batch_orders),
        "accepted_order_keys": [],
        "accepted_order_nos": [],
        "gate_checks": 0,
        "mutation_boundary_checks": 0,
        "status": "pending",
        "retention_policy": PARTIAL_BATCH_RETAIN_POLICY,
    }
    record.setdefault("same_cycle_buy_batches", []).append(batch)
    for order in batch_orders:
        _append_order_intent(
            order=order,
            table_prices=table_prices,
            fidelity=fidelity,
            record=record,
        )

    if batch["symbol"] in halted:
        record.setdefault("submission_blocked", []).append(
            {"symbol": batch["symbol"], "reason": "halted_suspect_symbol"}
        )
        batch["status"] = "blocked_halted_suspect"
        return None

    boundary = await _read_mutation_boundary(
        account=account,
        journal=journal,
        lease=lease,
        at=dt.datetime.now(dt.UTC),
    )
    batch["mutation_boundary_checks"] += 1
    _record_ordering_boundary(
        boundary=boundary,
        action=f"batch_gate:{cycle_id}:{batch['symbol']}",
        fidelity=fidelity,
        record=record,
    )
    if not boundary.clean:
        reason = "mutation_boundary_not_clean"
        detail = boundary.blocking_reason or "unknown_boundary_failure"
        _mark_buy_batch_failure(
            batch=batch, record=record, reason=reason, detail=detail
        )
        return reason, detail, False
    if coordination is None:
        reason = COORDINATION_GRANT_UNAVAILABLE_REASON
        detail = ordering_support.LOCAL_FLOCK_CANNOT_AUTHORIZE_SEND
        _mark_buy_batch_failure(
            batch=batch, record=record, reason=reason, detail=detail
        )
        return reason, detail, False

    current_truth = kiwoom_lane.broker_truth_from(
        position_symbols=scoped.cap_position_symbols,
        pending=boundary.pending,
    )
    batch["gate_checks"] = 1
    batch["gate_checked_at"] = boundary.at.isoformat()
    try:
        authorization = kiwoom_lane.authorize_same_cycle_buy_batch(
            cycle_id=cycle_id,
            planned=batch_orders,
            broker_truth=current_truth,
        )
    except OwnPendingResubmitBlocked as exc:
        batch["status"] = "blocked_by_preexisting_pending"
        batch["failure"] = {
            "reason": "pending_recheck_blocked",
            "detail": str(exc),
        }
        record.setdefault("submission_dedup_blocked", []).append(
            {
                "symbol": batch["symbol"],
                "cycle_id": cycle_id,
                "reason": "pending_recheck_blocked",
                "detail": str(exc),
            }
        )
        return None
    except kiwoom_lane.SameCycleBuyBatchViolation as exc:
        reason = "same_cycle_batch_invalid"
        detail = str(exc)
        _mark_buy_batch_failure(
            batch=batch, record=record, reason=reason, detail=detail
        )
        return reason, detail, True

    for index, order in enumerate(batch_orders):
        if index:
            boundary = await _read_mutation_boundary(
                account=account,
                journal=journal,
                lease=lease,
                at=dt.datetime.now(dt.UTC),
            )
            batch["mutation_boundary_checks"] += 1
            _record_ordering_boundary(
                boundary=boundary,
                action=f"batch_continue:{cycle_id}:{order.order_key}",
                fidelity=fidelity,
                record=record,
            )
            if not boundary.clean:
                reason = "mutation_boundary_not_clean"
                detail = boundary.blocking_reason or "unknown_boundary_failure"
                _mark_buy_batch_failure(
                    batch=batch, record=record, reason=reason, detail=detail
                )
                return reason, detail, False
        try:
            authorization.claim(order)
            coordinated = await coordination.submit_coordinated(
                account,
                planned=order,
                record_order_no=_journal_writer(journal),
                policy_version=coordination.policy_binding.policy_version,
                policy_version_hash=coordination.policy_binding.policy_version_hash,
                now=dt.datetime.now(dt.UTC),
            )
        except Exception as exc:  # noqa: BLE001 — every batch failure is material
            reason = "day_order_submission_unverified"
            detail = type(exc).__name__
            record.setdefault("day_order_failures", []).append(str(exc))
            _mark_buy_batch_failure(
                batch=batch, record=record, reason=reason, detail=detail
            )
            return reason, detail, True

        order_no = coordinated.evidence.broker_order_id
        assert order_no is not None
        batch["accepted_order_keys"].append(order.order_key)
        batch["accepted_order_nos"].append(order_no)
        day_order = kiwoom_lane.DayOrderResult(
            correlation_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            price=order.price,
            quantity=order.quantity,
            submitted=True,
            order_no=order_no,
        )
        lifecycle_failure = await _record_day_order_lifecycle(
            account=account,
            order=order,
            day_order=day_order,
            cycle_now=cycle_now,
            table_prices=table_prices,
            day_orders=day_orders,
            fidelity=fidelity,
            record=record,
        )
        if lifecycle_failure is not None:
            reason, detail = lifecycle_failure
            _mark_buy_batch_failure(
                batch=batch, record=record, reason=reason, detail=detail
            )
            return reason, detail, True

    batch.update(
        {
            "status": "complete",
            "accepted_count": len(batch["accepted_order_keys"]),
            "remaining_order_keys": [],
            "compensating_cancel_attempted": False,
        }
    )
    record["batch_submission_status"] = "complete"
    return None


async def _cancel_own_pending_on_kill(  # noqa: PLR0915 — linear safety sequence
    *,
    account: kiwoom_lane.ReadOnlyKiwoomMockAccount,
    journal: kiwoom_attr.OwnOrderJournal,
    fidelity: ordering_support.OrderingEventJournal,
    lease: ordering_support.WriterLease,
    coordination: ordering_support.KiwoomCoordinationAdapter | None,
    now: dt.datetime,
    outcome: KiwoomCycleOutcome,
    ledger: ObservationLedger,
    record: dict[str, Any],
    labels: tuple[str, ...],
) -> KiwoomCycleOutcome:
    """Cancel every currently-own resting order and prove the result by re-read."""

    initial = await _read_mutation_boundary(
        account=account, journal=journal, lease=lease, at=dt.datetime.now(dt.UTC)
    )
    _record_ordering_boundary(
        boundary=initial,
        action="kill_initial_cancel_inventory",
        fidelity=fidelity,
        record=record,
    )
    if not initial.clean:
        outcome.exit_code = 2
        return _finish_ordering_stopped(
            outcome=outcome,
            ledger=ledger,
            record=record,
            labels=labels,
            reason="kill_cancel_boundary_not_clean",
            detail=initial.blocking_reason or "unknown_boundary_failure",
        )
    if coordination is None:
        outcome.exit_code = 2
        return _finish_ordering_stopped(
            outcome=outcome,
            ledger=ledger,
            record=record,
            labels=labels,
            reason=COORDINATION_GRANT_UNAVAILABLE_REASON,
            detail=ordering_support.LOCAL_FLOCK_CANNOT_AUTHORIZE_SEND,
        )
    assert isinstance(initial.pending, kiwoom_lane.BrokerPending)
    record["kill_cancellation"] = {
        "required": True,
        "initial_own_resting_count": len(initial.pending.own_orders),
        "attempts": [],
        "confirmed": False,
    }

    for initial_order in initial.pending.own_orders:
        boundary = await _read_mutation_boundary(
            account=account, journal=journal, lease=lease, at=dt.datetime.now(dt.UTC)
        )
        _record_ordering_boundary(
            boundary=boundary,
            action=f"kill_cancel:{initial_order.order_id}",
            fidelity=fidelity,
            record=record,
        )
        if not boundary.clean:
            outcome.exit_code = 2
            return _finish_ordering_stopped(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="kill_cancel_boundary_not_clean",
                detail=boundary.blocking_reason or "unknown_boundary_failure",
            )
        assert isinstance(boundary.pending, kiwoom_lane.BrokerPending)
        current = next(
            (
                item
                for item in boundary.pending.own_orders
                if item.order_id == initial_order.order_id
            ),
            None,
        )
        if current is None:
            continue
        try:
            source = next(
                item for item in journal.read_all() if item.order_no == current.order_id
            )
        except Exception as exc:  # noqa: BLE001 — cannot prove cancel ownership
            outcome.exit_code = 2
            return _finish_ordering_stopped(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="kill_cancel_ownership_unreadable",
                detail=type(exc).__name__,
            )
        attempt: dict[str, Any] = {
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "original_order_no": current.order_id,
            "symbol": current.symbol,
            "remaining_quantity": current.remaining_quantity,
            "cancel_attempted": True,
        }
        if current.remaining_quantity is None:
            outcome.exit_code = 2
            return _finish_ordering_stopped(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="kill_cancel_remainder_unknown",
                detail=f"order_no={current.order_id}",
            )
        try:
            planned = kiwoom_lane.PlannedOrder(
                cycle_id=str(record.get("cycle_id") or "kill-cancel"),
                order_key=f"cancel:{current.order_id}",
                client_order_id=source.correlation_id,
                symbol=current.symbol,
                side=source.side,
                leg="cancel",
                price=source.price,
                quantity=int(current.remaining_quantity),
                notional=Decimal(source.price * int(current.remaining_quantity)),
            )
            coordinated = await coordination.cancel_attributed(
                account,
                planned=planned,
                native_order_id=current.order_id,
                known_remainder=Decimal(int(current.remaining_quantity)),
                policy_version=coordination.policy_binding.policy_version,
                policy_version_hash=coordination.policy_binding.policy_version_hash,
            )
            response = {
                "return_code": 0,
                "ord_no": coordinated.evidence.broker_order_id,
            }
        except Exception as exc:  # noqa: BLE001 — response failure is not cancellation
            attempt["cancel_response"] = {"error_type": type(exc).__name__}
            record["kill_cancellation"]["attempts"].append(attempt)
            _append_ordering_event(
                journal=fidelity,
                record=record,
                event={"at": attempt["at"], "event": "cancel_failure", **attempt},
            )
            outcome.exit_code = 2
            return _finish_ordering_stopped(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="kill_cancel_failed",
                detail=type(exc).__name__,
            )
        attempt["cancel_response"] = dict(response)
        raw_cancel_no = str(response.get("ord_no") or "").strip()
        if raw_cancel_no.isdigit() and raw_cancel_no != current.order_id:
            journal.append(
                kiwoom_attr.OwnOrderRecord(
                    at=attempt["at"],
                    order_no=raw_cancel_no,
                    correlation_id=f"{source.correlation_id}:cancel:{raw_cancel_no}",
                    symbol=current.symbol,
                    side="cancel",
                    price=current.ordered_price,
                    quantity=current.remaining_quantity,
                    order_date=kiwoom_attr.kst_order_date(now),
                )
            )
            attempt["cancel_order_no"] = raw_cancel_no
        record["kill_cancellation"]["attempts"].append(attempt)
        _append_ordering_event(
            journal=fidelity,
            record=record,
            event={"at": attempt["at"], "event": "cancel_ack", **attempt},
        )

        reconciled = await _read_mutation_boundary(
            account=account,
            journal=journal,
            lease=lease,
            at=dt.datetime.now(dt.UTC),
        )
        _record_ordering_boundary(
            boundary=reconciled,
            action=f"kill_cancel_reconcile:{current.order_id}",
            fidelity=fidelity,
            record=record,
        )
        if not reconciled.clean:
            outcome.exit_code = 2
            return _finish_ordering_stopped(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="kill_cancel_reconcile_unreadable",
                detail=reconciled.blocking_reason or "unknown_boundary_failure",
            )
        assert isinstance(reconciled.pending, kiwoom_lane.BrokerPending)
        if any(
            item.order_id == current.order_id for item in reconciled.pending.own_orders
        ):
            outcome.exit_code = 2
            return _finish_ordering_stopped(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="kill_cancel_not_confirmed",
                detail=f"order_no={current.order_id}",
            )

    final = await _read_mutation_boundary(
        account=account, journal=journal, lease=lease, at=dt.datetime.now(dt.UTC)
    )
    _record_ordering_boundary(
        boundary=final,
        action="kill_final_reconcile",
        fidelity=fidelity,
        record=record,
    )
    if not final.clean or not isinstance(final.pending, kiwoom_lane.BrokerPending):
        outcome.exit_code = 2
        return _finish_ordering_stopped(
            outcome=outcome,
            ledger=ledger,
            record=record,
            labels=labels,
            reason="kill_final_reconcile_unreadable",
            detail=final.blocking_reason or "pending_unreadable",
        )
    if final.pending.own_orders:
        outcome.exit_code = 2
        return _finish_ordering_stopped(
            outcome=outcome,
            ledger=ledger,
            record=record,
            labels=labels,
            reason="kill_own_pending_remains",
            detail=f"remaining_own_orders={len(final.pending.own_orders)}",
        )
    record["kill_cancellation"]["confirmed"] = True
    record["planned"] = []
    record["blocked"] = []
    record["submitted"] = []
    return _persist_outcome(
        outcome=outcome, ledger=ledger, record=record, labels=labels
    )


async def _run_ordering_cycle(  # noqa: PLR0915 — explicit safety sequence
    *,
    now: dt.datetime,
    table: PolicyTable,
    envelope: Envelope,
    labels: tuple[str, ...],
    outcome: KiwoomCycleOutcome,
    ledger: ObservationLedger,
    record: dict[str, Any],
    account: kiwoom_lane.ReadOnlyKiwoomMockAccount | None,
    journal: kiwoom_attr.OwnOrderJournal,
    lease: ordering_support.WriterLease,
    coordination: ordering_support.KiwoomCoordinationAdapter | None,
    realized_pnl_reader: Callable[..., kiwoom_attr.RealizedPnlInput],
) -> KiwoomCycleOutcome:
    """Independent ORDERING mode; it cannot fall through to acceptance cancel."""

    if account is None:
        account = kiwoom_lane.ReadOnlyKiwoomMockAccount()
    fidelity = ordering_support.OrderingEventJournal.for_lane(
        root=ledger.root, lane=LANE
    )
    record["fidelity_artifact"] = {
        "path": str(fidelity.path),
        "append_only": True,
        "required_path": [
            "table_price",
            "intended_limit",
            "broker_ack",
            "partial_or_fill_vwap",
            "cancel_or_reconcile",
        ],
    }
    try:
        prior_events = fidelity.read_all()
    except Exception as exc:  # noqa: BLE001 — corrupted lifecycle is no lifecycle
        return _finish_zero_order(
            outcome=outcome,
            ledger=ledger,
            record=record,
            labels=labels,
            reason="ordering_fidelity_journal_unreadable",
            detail=type(exc).__name__,
        )
    record["fidelity_artifact"]["prior_event_count"] = len(prior_events)
    record["writer_lease"] = lease.canonical()
    try:
        fresh = await kiwoom_lane.read_fresh_truth(account)
    except Exception as exc:  # noqa: BLE001
        return _finish_zero_order(
            outcome=outcome,
            ledger=ledger,
            record=record,
            labels=labels,
            reason="ordering_account_truth_unavailable",
            detail=type(exc).__name__,
        )
    attribution = await kiwoom_attr.read_own_attribution(
        journal=journal, read_order_detail=account.read_order_detail
    )
    scoped = scoped_positions(fresh=fresh, attribution=attribution)
    boundary = await _read_mutation_boundary(
        account=account, journal=journal, lease=lease, at=dt.datetime.now(dt.UTC)
    )
    _record_ordering_boundary(
        boundary=boundary,
        action="ordering_preflight",
        fidelity=fidelity,
        record=record,
    )
    record["ordering_preflight"] = {
        "cash_present": bool(fresh.cash > 0),
        "attribution_readable": scoped.unreadable is None,
        "attribution": scoped.canonical(),
        "mutation_boundary": boundary.canonical(action="ordering_preflight"),
        "passed": bool(fresh.cash > 0 and scoped.unreadable is None and boundary.clean),
    }
    record["fresh_truth"] = fresh.status_only(boundary.pending)
    record["attribution"] = {
        **scoped.canonical(),
        "source": kiwoom_attr.ATTRIBUTION_SOURCE,
        "nav_basis": KR_ATTRIBUTED_NAV_BASIS,
    }
    record["own_pending_source"] = kiwoom_lane.OWN_PENDING_SOURCE
    record["own_pending_basis"] = kiwoom_lane.OWN_PENDING_BASIS
    if fresh.cash <= 0:
        return _finish_zero_order(
            outcome=outcome,
            ledger=ledger,
            record=record,
            labels=labels,
            reason="ordering_preflight_not_clean",
            detail="cash_not_positive",
        )
    if scoped.unreadable is not None:
        return _finish_zero_order(
            outcome=outcome,
            ledger=ledger,
            record=record,
            labels=labels,
            reason="ordering_preflight_not_clean",
            detail="attribution_unreadable",
        )
    if not boundary.clean:
        return _finish_zero_order(
            outcome=outcome,
            ledger=ledger,
            record=record,
            labels=labels,
            reason="ordering_preflight_not_clean",
            detail=boundary.blocking_reason or "mutation_boundary_not_clean",
        )

    pnl_input = realized_pnl_reader(journal=journal, now=now)
    record["realized_pnl_input"] = pnl_input.canonical()
    record["realized_pnl_source"] = pnl_input.source
    if not pnl_input.readable:
        return _finish_zero_order(
            outcome=outcome,
            ledger=ledger,
            record=record,
            labels=labels,
            reason=REALIZED_PNL_UNAVAILABLE_REASON,
            detail=pnl_input.reason or "realized_pnl_input_unreadable",
        )
    assert pnl_input.value is not None
    state = broker_state(
        fresh=fresh,
        pending=boundary.pending,
        attribution=attribution,
        realized_pnl_today=pnl_input.value,
    )
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
            "day_order_policy": DAY_ORDER_RETAINED_NOTE,
            "round_trip": [],
        }
    )
    if decision.tripped:
        notice = decision.operator_notice(
            lane=LANE, remaining_orders_note=KILL_CANCEL_SUPPORTED_NOTE
        )
        if notice:
            ledger.record_notice(at=now, text=notice, lane=LANE)
            record["operator_notice"] = notice
        return await _cancel_own_pending_on_kill(
            account=account,
            journal=journal,
            fidelity=fidelity,
            lease=lease,
            coordination=coordination,
            now=now,
            outcome=outcome,
            ledger=ledger,
            record=record,
            labels=labels,
        )

    held = {pos.symbol: pos.quantity for pos in state.positions}
    planned, blocked = kiwoom_lane.plan_orders(
        derivation.orders,
        cycle_id=derivation.cycle_id,
        envelope=envelope,
        held_quantities=held,
    )
    record["planned"] = [order.to_json() for order in planned]
    record["blocked"] = [order.to_json() for order in blocked]
    record["halted_suspect"] = {
        "source": "policy_table.universe.halted_suspect (ROB-1236)",
        "symbols": list(halted_suspect_symbols(table)),
    }
    table_prices = {
        order.order_key: format(order.table_price, "f") for order in derivation.orders
    }
    day_orders: list[dict[str, Any]] = []
    record["submitted"] = day_orders
    # Keep the acknowledgement collection visible even if a later readback
    # fails.  A broker-accepted order must not disappear from the summary just
    # because its lifecycle could not yet be proven complete.
    record["day_orders"] = day_orders
    record["same_cycle_buy_batch_policy"] = SAME_CYCLE_BUY_BATCH_POLICY
    record["partial_batch_retention_policy"] = PARTIAL_BATCH_RETAIN_POLICY
    record["same_cycle_buy_batches"] = []
    halted = set(halted_suspect_symbols(table))
    for submission_group in _ordering_submission_groups(planned):
        if len(submission_group) > 1:
            stop = await _submit_same_cycle_buy_batch(
                account=account,
                batch_orders=submission_group,
                cycle_id=derivation.cycle_id,
                scoped=scoped,
                halted=halted,
                journal=journal,
                lease=lease,
                coordination=coordination,
                fidelity=fidelity,
                record=record,
                day_orders=day_orders,
                table_prices=table_prices,
                cycle_now=now,
            )
            if stop is not None:
                reason, detail, force_exit_two = stop
                if force_exit_two:
                    outcome.exit_code = 2
                return _finish_ordering_stopped(
                    outcome=outcome,
                    ledger=ledger,
                    record=record,
                    labels=labels,
                    reason=reason,
                    detail=detail,
                )
            continue

        order = submission_group[0]
        _append_order_intent(
            order=order,
            table_prices=table_prices,
            fidelity=fidelity,
            record=record,
        )
        if order.symbol in halted:
            record.setdefault("submission_blocked", []).append(
                {"symbol": order.symbol, "reason": "halted_suspect_symbol"}
            )
            continue
        sell_scope = scoped
        if order.side == "sell":
            try:
                sell_fresh = await kiwoom_lane.read_fresh_truth(account)
                sell_attribution = await kiwoom_attr.read_own_attribution(
                    journal=journal, read_order_detail=account.read_order_detail
                )
            except Exception as exc:  # noqa: BLE001 — failed fresh truth is closed
                return _finish_ordering_stopped(
                    outcome=outcome,
                    ledger=ledger,
                    record=record,
                    labels=labels,
                    reason="fresh_sell_attribution_unavailable",
                    detail=type(exc).__name__,
                )
            sell_scope = scoped_positions(
                fresh=sell_fresh, attribution=sell_attribution
            )
            if sell_scope.unreadable is not None:
                return _finish_ordering_stopped(
                    outcome=outcome,
                    ledger=ledger,
                    record=record,
                    labels=labels,
                    reason="fresh_sell_attribution_unavailable",
                    detail="attribution_unreadable",
                )
            try:
                kr_attribution.assert_sell_is_own(
                    sell_scope,
                    symbol=order.symbol,
                    quantity=Decimal(order.quantity),
                    lane=LANE,
                )
            except kr_attribution.LegacyPositionSellBlocked as exc:
                record.setdefault("submission_blocked", []).append(
                    {
                        "symbol": order.symbol,
                        "correlation_id": order.client_order_id,
                        "reason": "own_fill_sell_gate_blocked",
                        "detail": str(exc),
                    }
                )
                continue
            except Exception as exc:  # noqa: BLE001 — unknown sell proof is closed
                return _finish_ordering_stopped(
                    outcome=outcome,
                    ledger=ledger,
                    record=record,
                    labels=labels,
                    reason="fresh_sell_attribution_unavailable",
                    detail=type(exc).__name__,
                )
        boundary = await _read_mutation_boundary(
            account=account,
            journal=journal,
            lease=lease,
            at=dt.datetime.now(dt.UTC),
        )
        _record_ordering_boundary(
            boundary=boundary,
            action=f"submit:{order.order_key}",
            fidelity=fidelity,
            record=record,
        )
        if not boundary.clean:
            return _finish_ordering_stopped(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="mutation_boundary_not_clean",
                detail=boundary.blocking_reason or "unknown_boundary_failure",
            )
        current_truth = kiwoom_lane.broker_truth_from(
            position_symbols=sell_scope.cap_position_symbols,
            pending=boundary.pending,
        )
        if coordination is None:
            return _finish_ordering_stopped(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason=COORDINATION_GRANT_UNAVAILABLE_REASON,
                detail=ordering_support.LOCAL_FLOCK_CANNOT_AUTHORIZE_SEND,
            )
        try:
            kiwoom_lane.assert_resubmit_allowed(
                current_truth, symbol=order.symbol, lane=LANE
            )
            coordinated = await coordination.submit_coordinated(
                account,
                planned=order,
                record_order_no=_journal_writer(journal),
                policy_version=coordination.policy_binding.policy_version,
                policy_version_hash=coordination.policy_binding.policy_version_hash,
                now=dt.datetime.now(dt.UTC),
            )
            order_no = coordinated.evidence.broker_order_id
            if order_no is None:
                raise RuntimeError("coordinated submit returned no broker_order_id")
            day_order = kiwoom_lane.DayOrderResult(
                correlation_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                price=order.price,
                quantity=order.quantity,
                submitted=True,
                order_no=order_no,
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
            continue
        except (
            kiwoom_lane.BrokerEchoMismatch,
            kiwoom_lane.KiwoomBrokerRejected,
        ) as exc:
            outcome.exit_code = 2
            record.setdefault("day_order_failures", []).append(str(exc))
            return _finish_ordering_stopped(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="day_order_submission_unverified",
                detail=type(exc).__name__,
            )
        lifecycle_failure = await _record_day_order_lifecycle(
            account=account,
            order=order,
            day_order=day_order,
            cycle_now=now,
            table_prices=table_prices,
            day_orders=day_orders,
            fidelity=fidelity,
            record=record,
        )
        if lifecycle_failure is not None:
            reason, detail = lifecycle_failure
            outcome.exit_code = 2
            return _finish_ordering_stopped(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason=reason,
                detail=detail,
            )

    record["fidelity_artifact"]["event_count_after_cycle"] = len(fidelity.read_all())
    return _persist_outcome(
        outcome=outcome, ledger=ledger, record=record, labels=labels
    )


def _journal_writer(journal: kiwoom_attr.OwnOrderJournal) -> Any:
    """Return the append callback both submission modes invoke.

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


def _cycle_status(*, confirm: bool, mode: CycleMode) -> tuple[str, str]:
    """Return the exact artifact literal for the behavior that actually ran."""

    if not confirm:
        return PREVIEW_STATUS, PREVIEW_STATUS_LABEL
    if mode == ACCEPTANCE_MODE:
        return ACCEPTANCE_ONLY_STATUS, ACCEPTANCE_ONLY_STATUS_LABEL
    return ORDERING_STATUS, ORDERING_STATUS_LABEL


def _stamp_contract_and_account_map(
    record: dict[str, Any], *, cycle_status: str, status_label: str
) -> None:
    record["contract"] = {
        "path": "~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md",
        "version": KR_CONTRACT_VERSION,
        "clauses": dict(KR_CONTRACT_CLAUSES),
        "file_sha256_reference_only": KR_CONTRACT_FILE_SHA256_REFERENCE_ONLY,
    }
    record["account_map"] = {
        "repo": "auto_trader-operator",
        "commit": KR_ACCOUNT_MAP_COMMIT,
        "canonical_surface": "operator_contract.yaml",
        "reference_surface": "mock/CLAUDE.md",
        "gate_values": dict(KR_ACCOUNT_MAP_VALUES),
    }
    record["cycle_status"] = cycle_status
    record["cycle_status_label"] = status_label
    if cycle_status == ORDERING_STATUS:
        record["ordering_requirements"] = dict(ORDERING_REQUIREMENTS)


def _coordination_error_code(error: BaseException) -> str:
    """Return only a closed/report-safe coordination error code."""

    code = getattr(error, "code", None)
    if type(code) is str and code.strip():
        return code
    reason = getattr(error, "reason", None)
    if type(reason) is str and reason.strip():
        return reason
    return type(error).__name__


def _resolve_coordination_owner(
    *,
    coordination_factory: Callable[[], object] | None,
    expected_entry: LaneRegistryEntry | None,
) -> tuple[ordering_support.KiwoomCoordinationAdapter | None, dict[str, Any]]:
    """Resolve and pin the nominated owner without silently downgrading it."""

    base = {
        "present": False,
        "recovery_owner": None,
        "authorizes_send": False,
        "local_flock_authorizes_send": False,
        "legacy_offline": False,
        "recovery_contract": dict(ordering_support.KIWOOM_LANE_RECOVERY_CONTRACT),
        "identity_guard": {"status": "not_configured"},
    }
    if coordination_factory is None:
        return None, base

    if expected_entry is None:
        base["identity_guard"] = {
            "status": "rejected",
            "code": KIWOOM_COORDINATION_OWNER_ENTRY_REQUIRED,
            "owner_type": None,
        }
        return None, base

    try:
        candidate = coordination_factory()
    except Exception as exc:  # noqa: BLE001 — owner construction is fail-closed
        base["identity_guard"] = {
            "status": "rejected",
            "code": _coordination_error_code(exc),
            "owner_type": None,
        }
        base["factory_error_type"] = type(exc).__name__
        return None, base

    if candidate is None:
        base["identity_guard"] = {
            "status": "rejected",
            "code": "coordination_factory_returned_none",
            "owner_type": "NoneType",
        }
        return None, base

    try:
        owner = assert_kiwoom_coordination_owner(
            candidate,
            expected_lane_id=ordering_support.KIWOOM_CANONICAL_LANE_ID,
            expected_entry=expected_entry,
        )
    except KiwoomCoordinationOwnerRejected as exc:
        base["identity_guard"] = {
            "status": "rejected",
            "code": exc.code,
            "owner_type": type(candidate).__name__,
        }
        return None, base
    except Exception as exc:  # noqa: BLE001 — malformed owner is fail-closed
        base["identity_guard"] = {
            "status": "rejected",
            "code": _coordination_error_code(exc),
            "owner_type": type(candidate).__name__,
        }
        return None, base

    base.update(
        {
            "present": True,
            "recovery_owner": owner.recovery_owner,
            # G1/G2 records owner presence only.  A grant-only adapter never
            # opens send; a future non-canary adapter may expose this field to
            # the later bounded-send stage.
            "authorizes_send": (
                not owner.grant_only
                and getattr(owner.ports, "legacy_offline", False) is not True
            ),
            "legacy_offline": getattr(owner.ports, "legacy_offline", False),
            "identity_guard": {
                "status": "accepted",
                "owner_type": type(owner).__name__,
                "lane_id": owner.ports.entry.lane_id,
                "account_identity_source": "LaneRegistryEntry.physical_account_id",
            },
        }
    )
    return owner, base


async def run_kiwoom_cycle(
    *,
    now: dt.datetime,
    table_dir: Path = DEFAULT_TABLE_DIR,
    out_dir: Path = DEFAULT_OBSERVATION_DIR,
    confirm: bool = False,
    ordering: bool = False,
    account: kiwoom_lane.ReadOnlyKiwoomMockAccount | None = None,
    journal: kiwoom_attr.OwnOrderJournal | None = None,
    lease_factory: Callable[[Path, str, str], ordering_support.WriterLease]
    | None = None,
    coordination_factory: (Callable[[], object] | None) = None,
    coordination_entry: LaneRegistryEntry | None = None,
    realized_pnl_reader: Callable[..., kiwoom_attr.RealizedPnlInput] | None = None,
) -> KiwoomCycleOutcome:
    """One manual kiwoom_mock B0-X cycle.

    ``confirm=False`` is the ordinary observation/derivation path. A confirmed
    call is default-disabled and requires the B0-X env gate plus a clean NW-B4
    preflight inside KRX RTH. With ``ordering=False`` (the safe default) it
    remains the one-order ``ACCEPTANCE_ONLY`` cancel round trip. The caller
    must additionally set ``ordering=True`` to select the independent DAY
    lifecycle path; a status string alone can never select that path.
    """

    envelope = load_envelope(MARKET)
    assert_envelope_locked(envelope)
    kiwoom_lane.assert_correlation_prefixes_disjoint()
    mode: CycleMode = ORDERING_MODE if ordering else ACCEPTANCE_MODE
    cycle_status, status_label = _cycle_status(confirm=confirm, mode=mode)
    labels = header_labels(
        lane=LANE,
        extra=(*account_history_labels(LANE), COEXISTENCE_LABEL, status_label),
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
        _stamp_contract_and_account_map(
            record, cycle_status=cycle_status, status_label=status_label
        )
        record["confirm"] = confirm
        record["ordering"] = ordering
        record["execution_mode"] = "preview" if not confirm else mode
        record["realized_pnl_source"] = (
            "not_evaluated_outside_ordering"
            if mode == ACCEPTANCE_MODE
            else "pending_ordering_preflight"
        )

        coordination, coordination_record = _resolve_coordination_owner(
            coordination_factory=coordination_factory,
            expected_entry=coordination_entry,
        )
        record["coordination"] = coordination_record

        # A configured owner factory is an explicit production dependency.  A
        # missing/rejected owner, or a grant-only canary, must therefore stop
        # every confirmed mutation mode before account/broker work.  Preview is
        # still allowed to render the diagnostic record and planned derivation.
        if (
            confirm
            and coordination_factory is not None
            and (coordination is None or coordination.grant_only)
        ):
            return _finish_zero_order(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason=COORDINATION_GRANT_UNAVAILABLE_REASON,
                detail=(
                    "coordination owner is absent or grant-only; "
                    "G1 does not authorize send"
                ),
            )

        if ordering and not confirm:
            return _finish_zero_order(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="ordering_requires_confirm",
                detail=(
                    "ORDERING is not armed by its mode flag alone; "
                    "the per-call confirm=True gate is required"
                ),
            )

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

        if mode == ACCEPTANCE_MODE:
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

        make_lease = (
            ordering_support.AccountWriterLease
            if lease_factory is None
            else lease_factory
        )
        lease = make_lease(root, LANE, account_identity["fingerprint"])
        try:
            lease.acquire()
        except Exception as exc:  # noqa: BLE001 — no writer authority, no order
            record["writer_lease"] = {
                "acquired": False,
                "authority": "account_keyed_ordering_lease",
                "error_type": type(exc).__name__,
            }
            return _finish_zero_order(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="writer_lease_unavailable",
                detail=type(exc).__name__,
            )
        mutation_coordination = (
            None
            if coordination is not None and coordination.grant_only
            else coordination
        )
        try:
            return await _run_ordering_cycle(
                now=now,
                table=table,
                envelope=envelope,
                labels=labels,
                outcome=outcome,
                ledger=ledger,
                record=record,
                account=account,
                journal=lane_journal,
                lease=lease,
                coordination=mutation_coordination,
                realized_pnl_reader=(
                    kiwoom_attr.realized_pnl_input_today
                    if realized_pnl_reader is None
                    else realized_pnl_reader
                ),
            )
        finally:
            lease.release()


__all__ = [
    "MARKET",
    "LANE",
    "CycleMode",
    "ACCEPTANCE_MODE",
    "ORDERING_MODE",
    "PREVIEW_STATUS",
    "ACCEPTANCE_ONLY_STATUS",
    "ORDERING_STATUS",
    "OUTSIDE_RTH_REASON",
    "PREFLIGHT_MAX_AGE_SECONDS",
    "ACCEPTANCE_SUBMISSION_LIMIT",
    "ROUND_TRIP_MANDATORY_NOTE",
    "KILL_CANCEL_SUPPORTED_NOTE",
    "DAY_ORDER_RETAINED_NOTE",
    "KR_CONTRACT_VERSION",
    "KR_ACCOUNT_MAP_COMMIT",
    "KR_ACCOUNT_MAP_VALUES",
    "KR_STATUS_LABEL",
    "PREVIEW_STATUS_LABEL",
    "ACCEPTANCE_ONLY_STATUS_LABEL",
    "ORDERING_STATUS_LABEL",
    "ORDERING_REQUIREMENTS",
    "COEXISTENCE_LABEL",
    "REALIZED_PNL_UNAVAILABLE_REASON",
    "KR_ATTRIBUTED_NAV_BASIS",
    "ATTRIBUTION_NOT_WIRED",
    "ForeignSameDayOrders",
    "MutationBoundary",
    "KiwoomCycleOutcome",
    "foreign_same_day_orders",
    "broker_state",
    "halted_suspect_symbols",
    "run_kiwoom_cycle",
    "scoped_positions",
]
