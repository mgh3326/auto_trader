"""``kis_mock`` cycle orchestration — same skeleton as ``scripts.b0x.cycle``.

Contract §5, applied to the KR (kis_mock) column: *v1 = 수동 kickoff … 사이클
커맨드 1회 실행 = 표 확인 → 주문 파생 → 제출 → reconcile → 관측 기록*, KR/US 일
1회(장 전). The step order carries the same safety property the crypto lanes
established (``scripts/b0x/cycle.py`` module docstring): writer lock before
anything else, then the *cheapest* zero-order gate before the account read.

For KR there is a boundary condition with no crypto analogue and it is
checked first, before even the table: contract "세션 KRX RTH v1 — 정규장만.
NXT·시간외 금지". Reusing ``app.services.kis_mock_runner.session.
is_krx_regular_session`` (already the fail-closed XKRX-calendar gate the
existing kis_mock runner uses for new entries) rather than re-deriving a
09:00-15:30 KST window by hand — a hand-rolled version would silently miss
holidays.

    writer lock  →  RTH gate  →  table (or zero orders)  →  account state
                 →  kill switch  →  derive  →  plan  →  submit  →  record

``submit`` is wired to ``kr_mock.build_kis_mock_broker`` /
``kr_mock.submit_planned_order`` (contract v1.3 ③).  The manual CLI exposes
the otherwise default-disabled confirm path only with an explicit
``--confirm`` lever.  Before it can dispatch, that path must hold the
account-wide kis_mock writer lease and pass the NW-B4 fresh-truth preflight;
its one mock-host orderbook read then feeds the existing adapter pre-send
guard.  ``KrCycleOutcome.submitted`` always records exactly what happened,
never a stamped ``real_orders=0`` constant.

Contract v1.5 ① lands here as ``broker_state`` (below): the §4 caps read this
cycle's kis_mock account snapshot, and nothing is carried between cycles. The
KR-specific consequence was that 자기 미체결 is unreadable on this venue
(``kr_mock.KR_PENDING_UNREADABLE``) and therefore failed closed — the same
"시도조차 구조적으로 불가" posture the kill-trip cancellation note below already
takes, applied to a second KIS mock limitation with the same root.

Contract v1.6 ① supplies the missing input for that one field only, from the
submission ledger (``scripts.b0x.kr.pending_ledger``, wired through the
``pending_reader`` seam below). Everything else about the posture is unchanged:
the reader can only *resolve* the unreadable state, never bypass it — a failed
ledger read returns the same ``PendingUnreadable`` sentinel and every symbol is
refused again. 포지션 진실 stays with the broker holdings read (v1.6 ②): the
``positions`` this function builds come from ``fresh.positions`` and from
nothing else, whatever the ledger says.

§36차 2항 (2026-08-11) — 귀속 게이트가 flat 요구를 대체한다
------------------------------------------------------------

이 계좌에는 B0-X 가 만들지 않은 **legacy 보유**가 있다. 이전 confirm preflight 는
「보유가 하나라도 있으면 이상」이었고, 그래서 (a) 한 건도 제출할 수 없었으며
(b) 계약 v1.5 ③ 의 「매도는 자기 보유에서 파생」과 모순이었다(보유가 있어야
매도가 나오는데 보유가 있으면 막혔다). 운영자 결정은 flatten 기각 · 귀속 기반
공존이다: :mod:`scripts.b0x.kr.attribution` 이 자기 원장 fill 로 own/legacy 를
가르고, legacy 는 **읽히되 파생 입력에서 빠지며 매도 경로에 도달하지 못한다**.
무엇이 어디에 포함/제외되는지와 그 근거는 :func:`broker_state` 의 docstring 에
있다(이 판정은 §36차 2항 이 명시적으로 위임한 것이다).

Kill-trip cancellation asymmetry with the crypto sidecar, documented rather
than silently matched: the crypto lanes cancel outstanding B0-X orders on a
kill trip (``scripts.b0x.cycle._cancel_b0x_open_orders``) because their
venues expose an open-orders read. KIS mock's pending-order inquiry
(``DomesticOrderClient.inquire_korea_orders``, TR ``TTTC8036R``) explicitly
raises for ``is_mock=True`` — "모의투자에서 지원되지 않음" — so there is no
way to discover what, if anything, is still resting on a kill trip. The
kill-switch notice text is lane-specific for this reason (see
``KILL_CANCEL_UNSUPPORTED_NOTE`` below); it does not claim a cancellation
that cannot be structurally attempted.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from app.services.kis_mock_runner.session import is_krx_regular_session
from app.services.kis_mock_runner.singleton import (
    KISMockWriterLease,
    WriterSingletonContended,
    WriterSingletonUnavailable,
)
from scripts.b0x.broker_truth import (
    BrokerTruth,
    OwnPendingResubmitBlocked,
    PendingUnreadable,
)
from scripts.b0x.cycle import base_record, render_cycle_report
from scripts.b0x.derivation import DerivationResult, derive_orders
from scripts.b0x.envelope import Envelope, assert_envelope_locked, load_envelope
from scripts.b0x.kill_switch import evaluate as evaluate_kill_switch
from scripts.b0x.kr import attribution as kr_attribution
from scripts.b0x.kr import mock as kr_mock
from scripts.b0x.kr import pending_ledger as kr_pending_ledger
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
LANE = kr_mock.LANE

# These provenance blocks are intentionally local to KR.  The shared
# ``scripts.b0x.contract`` stamp is still v1.4 / the older account-map commit
# for the untouched US and crypto lanes; letting it leak into a KR artifact
# would make this v1.6 execution surface report the wrong governing facts.
# Do not update the shared stamp as part of this KR-only job.
KR_CONTRACT_VERSION: Final[str] = "v1.6"
KR_CONTRACT_FILE_SHA256_REFERENCE_ONLY: Final[str] = (
    "a3922894dcb91c2888daa2b33a9bfb9fab48a1c660ffc16deead09c530faea14"
)
KR_CONTRACT_CLAUSES: Final[dict[str, str]] = {
    "§8 v1.6": (
        "KR 자기 미체결은 kis_mock_order_ledger 조건부 예외이며, "
        "미체결 dedup/캡 입력에만 쓴다. 포지션 진실은 계속 브로커 조회다."
    ),
}
KR_ACCOUNT_MAP_COMMIT: Final[str] = "e93349e7ab9b1db414b1fba619e462cf84da1fa7"
KR_ACCOUNT_MAP_VALUES: Final[dict[str, str]] = {
    "account_lanes.kis_mock": "B0-X-KR",
    "exclusive_lane": "B0-X-KR",
    "active_ordering_strategy": "B0-X-adapter-single-writer",
    "surface": "kis_mock",
}
KR_STATUS_LABEL: Final[str] = (
    "OBSERVATION_DERIVATION_ONLY — KR cycle 지위는 유지된다. 이 수동 mock "
    "acceptance lever는 모의 자동매매 가동 선언이나 스케줄러가 아니다."
)

#: KR's ``table_source.MAX_TABLE_AGE`` entry is 36h — contract §2-2 v1.1
#: (operator-confirmed 2026-08-08, sha256 ``97278b0e8b8000e2e663c936328686001
#: af5850087897270bc80a95ebf8f6b2e``), not a value this adapter chose. See
#: ``tests/scripts/b0x/test_kr_envelope_and_kill_switch.py`` for the
#: regression guard and ``docs/runbooks/b0x-kr-cycle.md`` §8 for the citation.
ZeroOrderReason = str

OUTSIDE_RTH_REASON: ZeroOrderReason = "outside_krx_regular_session"

#: NW-B4: every account/ledger observation used to authorize a confirmed
#: dispatch must be taken in this bounded interval.  The value is a contract
#: requirement, not a tunable CLI parameter.
PREFLIGHT_MAX_AGE_SECONDS = 5 * 60

#: This job exposes a one-shot, manual acceptance lever only.  It is a
#: restrictive operational bound (not an envelope dial) and deliberately has
#: no CLI or environment override.
MANUAL_CONFIRM_SUBMISSION_LIMIT = 1

#: NW-B6: KIS mock cannot discover a cancellable order while ``is_mock=True``.
#: This literal distinguishes structural non-attempt from a failed attempt or
#: an invented successful cancellation.
KILL_TRIPPED_CANCEL_UNSUPPORTED = "KILL_TRIPPED_CANCEL_UNSUPPORTED"

#: Overrides ``KillSwitchDecision.operator_notice``'s default "잔여 주문 취소
#: 완료" clause — kis_mock has no pending-order inquiry to discover, let
#: alone cancel, what is still resting (see the module docstring). Stating
#: "취소 완료" here would be a false claim from the moment submission is
#: wired, not merely an untested one.
KILL_CANCEL_UNSUPPORTED_NOTE = (
    f"{KILL_TRIPPED_CANCEL_UNSUPPORTED}: 신규 주문 중단. 잔여 주문 취소는 구조적으로 시도 불가 — KIS 모의투자 "
    "미체결조회(TTTC8036R)가 is_mock=True 에 대해 지원되지 않아 "
    "(DomesticOrderClient.inquire_korea_orders) 취소 대상을 조회할 수단이 "
    "없음. 재개는 운영자 결정 (계약 §2-4)."
)


@dataclass
class KrCycleOutcome:
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


def _stamp_kr_contract_and_account_map(record: dict[str, Any]) -> None:
    """Replace only KR's inherited generic provenance with current facts."""

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
    record["cycle_status"] = "OBSERVATION_DERIVATION_ONLY"


#: Contract v1.5 ①: like the crypto sidecar, the KR lane has no realized-P&L
#: source now that the never-written state file is gone — fills are not
#: reconciled into a P&L ledger here. Recorded explicitly so a reader does not
#: mistake a structural absence for a measured zero: with no P&L input the
#: −2.5% NAV kill cannot fire.
KR_REALIZED_PNL_UNAVAILABLE = (
    "realized_pnl_today has no source on this lane — B0-X fills are not "
    "reconciled into a P&L ledger, so the −2.5% NAV kill cannot fire. "
    "Not a measured zero."
)


#: 🔴 귀속 리더를 배선하지 않은 호출자가 받는 상태. 「자기 것이 없다」가 아니라
#: 「자기 것이 무엇인지 확인하지 않았다」이며, 두 방향 모두 닫힌다(자기 포지션 0
#: + §4 상한 입력 = 계좌 전체). ``KR_PENDING_UNREADABLE`` 이 미배선 호출자에게
#: 하는 일과 같은 자세다.
ATTRIBUTION_NOT_WIRED: Final[kr_attribution.AttributionUnreadable] = (
    kr_attribution.AttributionUnreadable(
        reason="kis_mock_own_fill_attribution_not_wired",
        detail=(
            "호출자가 자기 원장 귀속 리더를 배선하지 않았다 — 어떤 보유가 자기 "
            "것인지 확인되지 않았으므로 legacy 로 취급한다(매도/물타기 파생 0) "
            "+ §4 상한은 계좌 전체 기준. 「미배선」을 「귀속 없음」으로도 "
            "「legacy 없음」으로도 읽지 않는다 (§36차 2항)"
        ),
    )
)


#: 🔴 §36차 2항 이 판정을 위임한 항목 중 하나 — legacy 평가금액의 NAV 포함 여부.
#: 기록에 남겨 두는 이유: pct_of_nav kill 의 절대 임계는 NAV 에 비례하므로, 이
#: 선택은 「어느 방향으로 틀릴 것인가」의 선택이다.
KR_ATTRIBUTED_NAV_BASIS: Final[str] = (
    "nav = cash + 자기 귀속 평가금액(지분 비례). legacy 평가금액은 제외 — §4 "
    "daily_loss_kill 이 pct_of_nav 이므로 legacy 를 포함하면 kill 이 발화하는 "
    "절대 손실액이 커진다(임계가 넓어진다). 귀속 불가면 자기 평가금액 0 → "
    "nav = cash 로 더 좁아진다. 어느 경우에도 넓어지지 않는다."
)


def broker_state(
    *,
    fresh: kr_mock.FreshTruth,
    own_pending: tuple[str, ...] | PendingUnreadable | None = None,
    attribution: (
        kr_attribution.OwnFillAttribution | kr_attribution.AttributionUnreadable | None
    ) = None,
) -> LaneAccountState:
    """kis_mock account state, derived entirely from this cycle's broker read.

    Contract v1.5 ①/③, **as amended by §36차 2항 (귀속 기반 공존)**. Positions
    are no longer "every mock holding": the account carries legacy holdings
    B0-X did not create, and those belong to an observation-only coexisting
    lane. ``attribution`` (자기 원장 fill 귀속) is what separates the two —
    see :mod:`scripts.b0x.kr.attribution` for the evidence rules and the
    deliberately asymmetric error direction. Omitting it is fail-closed
    (:data:`ATTRIBUTION_NOT_WIRED`), never "everything is mine".

    ``average_price`` for an attributed position is the weighted average of
    **B0-X's own fills**, not the broker's 매입평균가: on a symbol shared with
    a legacy holding the broker's average blends someone else's cost in.

    🔴 What legacy holdings are excluded from, and why (§36차 2항 delegates
    this judgement, so it is stated here rather than left implicit):

    * **매도/물타기 파생** — excluded. This is the literal instruction, and
      the whole reason the gate exists: a legacy symbol must never become a
      sell or an averaging candidate.
    * **§4 동시포지션 / 일일신규 상한 입력** — excluded (``cap_position_
      symbols`` = attributed only). This diverges from the shared
      ``broker_truth`` docstring's "account-wide" reading and from the US
      lane's comment that foreign sellable positions consume a slot, and the
      divergence is deliberate: with 11 legacy holdings against a cap of 10,
      an account-wide count means this lane derives **zero** new entries
      forever, which is exactly the coexistence the operator's (b) decision
      replaced flatten with. The §4 *numbers* are untouched; what changed is
      whose positions they count. 🔴 The cost, stated plainly: the account can
      now carry legacy + up to 10 B0-X positions, so account-level exposure is
      wider than the cap alone suggests.
    * **NAV (kill 임계 기준)** — excluded, pro-rated by attributed share.
      The §4 kill is ``pct_of_nav``, so a *larger* NAV means a *larger*
      absolute loss is tolerated before it fires. Including legacy market
      value would therefore widen the threshold; excluding it can only
      narrow it. Where a judgement was free, it was made in the direction
      that never widens the kill (unreadable attribution → attributed
      evaluation 0 → NAV = cash, the tightest defensible basis).
    * **계좌 진실 · 오염 판정 · preflight 기록** — *included*. Legacy holdings
      are read, counted and named in the artifact (``positions.legacy_
      symbols``); the CONTAMINATED judgement stays exactly what it was
      (non-``b0xk`` correlation *traces*, i.e. another **writer**), because a
      sanctioned coexisting holding is not another writer.

    🔴 ``invested_notional`` is the **cost basis**, not cumulative deployment,
    because a single broker snapshot carries no cumulative figure — and a
    partial sell lowers a cost basis. Since ``B0XPosition.invested_notional``
    feeds the §4 per-symbol total cap, which bounds deployment precisely so it
    does *not* shrink on a sell, this lane declares
    ``cumulative_deployment_readable=False`` and derivation refuses additions
    rather than sizing them against a figure it knows is understated.

    ``entry_count`` is reported as ``0``: how many separate entries built a
    holding is not in the snapshot, and derivation does not read it. Inventing
    a plausible number would put a fabricated value into the hashed state.

    🔴 ``own_pending`` (contract v1.6 ①) reaches **only**
    ``BrokerTruth.own_pending``. It is not read when building ``positions``
    below, and it is not merged into ``broker_truth.position_symbols`` — v1.6
    ② keeps 포지션 진실 on the broker. ``None`` (the default) leaves the lane
    on ``KR_PENDING_UNREADABLE``.

    🔴 v1.6 ② still holds under the attribution gate: the broker holdings read
    remains the only source of *whether* a position exists and how large it
    is. The ledger answers only *whose* it is, and can never conjure a
    position the broker did not report (``scope_positions`` iterates the
    broker's rows and caps every attributed quantity by the held quantity).
    """

    scoped = scoped_positions(fresh=fresh, attribution=attribution)
    return LaneAccountState(
        lane=LANE,
        quote_currency=kr_mock.QUOTE_CURRENCY,
        cash=fresh.cash,
        broker_truth=BrokerTruth(
            position_symbols=scoped.cap_position_symbols,
            own_pending=(
                kr_mock.KR_PENDING_UNREADABLE if own_pending is None else own_pending
            ),
        ),
        positions=scoped.own_positions,
        cumulative_deployment_readable=False,
        realized_pnl_today=Decimal("0"),
        # 🔴 NAV = cash + 자기 귀속 평가금액. legacy 평가금액을 넣으면 pct_of_nav
        # kill 의 절대 임계가 커진다 — 넓히지 않는 방향을 고른다(위 docstring).
        nav=fresh.cash + scoped.attributed_evaluation,
    )


def scoped_positions(
    *,
    fresh: kr_mock.FreshTruth,
    attribution: (
        kr_attribution.OwnFillAttribution | kr_attribution.AttributionUnreadable | None
    ),
) -> kr_attribution.ScopedPositions:
    """One place where 자기 보유 / legacy 보유 is decided for this lane."""

    return kr_attribution.scope_positions(
        positions=fresh.positions,
        attribution=ATTRIBUTION_NOT_WIRED if attribution is None else attribution,
        account_wide_non_dust=fresh.non_dust_position_symbols(),
        min_trade_unit=kr_mock.KRX_MIN_TRADE_UNIT_SHARES,
    )


def _persist_outcome(
    *,
    outcome: KrCycleOutcome,
    ledger: ObservationLedger,
    record: dict[str, Any],
    labels: tuple[str, ...],
) -> KrCycleOutcome:
    """Append one immutable cycle record and render its matching artifact."""

    ledger.record_cycle(record)
    outcome.record = record
    outcome.artifact_path = ledger.write_artifact(
        name=f"{outcome.at.strftime('%Y%m%dT%H%M%SZ')}-cycle.md",
        content=render_cycle_report(record, labels=labels),
    )
    return outcome


def _finish_zero_order(
    *,
    outcome: KrCycleOutcome,
    ledger: ObservationLedger,
    record: dict[str, Any],
    labels: tuple[str, ...],
    reason: str,
    detail: str,
) -> KrCycleOutcome:
    """Record a fail-closed, no-dispatch terminal outcome."""

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


def _confirm_preflight_record(
    *,
    started_at: dt.datetime,
    completed_at: dt.datetime,
    account_identity: dict[str, str],
    fresh: kr_mock.FreshTruth,
    scoped: kr_attribution.ScopedPositions,
    own_pending: tuple[str, ...] | PendingUnreadable,
    foreign_traces: kr_pending_ledger.ForeignLedgerTraces | PendingUnreadable,
) -> dict[str, Any]:
    """NW-B4 account truth, rendered without balances or account numbers.

    KIS mock's native pending-order query is structurally unavailable.  The
    v1.6-approved same-day signal/order-ledger union is therefore named as a
    **ledger shadow** rather than misreported as a native open-order response.
    A non-B0-X trace is contamination; an unreadable trace source is also a
    failure because exclusive truth was not established.

    🔴 §36차 2항 — the flat-account requirement is **gone**, replaced by the
    attribution gate. Until 2026-08-11 this record failed on
    ``unexpected_positions`` whenever the account carried *any* non-dust
    holding, which on this account (11 legacy holdings B0-X never created)
    meant the confirm path could never dispatch, and which also contradicted
    contract v1.5 ③ (매도는 자기 보유에서 파생되는데 보유가 있으면 막혔다).
    Legacy holdings are now recorded as a named, coexisting fact instead of an
    anomaly; what still fails closed is being unable to *tell them apart*
    (``attribution_unreadable``), because that is the state in which a sell
    could reach someone else's shares.
    """

    elapsed_seconds = (completed_at - started_at).total_seconds()
    own_unreadable = own_pending if isinstance(own_pending, PendingUnreadable) else None
    foreign_unreadable = (
        foreign_traces if isinstance(foreign_traces, PendingUnreadable) else None
    )
    own_symbols = () if own_unreadable is not None else own_pending
    foreign = None if foreign_unreadable is not None else foreign_traces
    position_symbols = fresh.non_dust_position_symbols()
    reasons: list[str] = []

    if elapsed_seconds > PREFLIGHT_MAX_AGE_SECONDS:
        reasons.append("preflight_exceeded_5_minutes")
    if fresh.cash <= 0:
        reasons.append("cash_not_positive")
    if scoped.unreadable is not None:
        # 🔴 「보유가 있다」가 아니라 「누구 것인지 모른다」가 실패 사유다.
        reasons.append("attribution_unreadable")
    if own_unreadable is not None:
        reasons.append("ledger_pending_unreadable")
    elif own_symbols:
        reasons.append("ledger_pending_present")
    if foreign_unreadable is not None:
        reasons.append("foreign_trace_unreadable")
    elif foreign is not None and foreign.trace_count:
        reasons.append("CONTAMINATED_foreign_correlation_trace")

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
            # 🔴 계좌 전체(위)와 귀속 분리(아래)를 같은 블록에 나란히 둔다 —
            # legacy 를 조용히 지우지 않는다 (§36차 2항 「무시」 ≠ 「삭제」).
            "own_attributed_symbols": [pos.symbol for pos in scoped.own_positions],
            "own_attributed_count": len(scoped.own_positions),
            "legacy_symbols": list(scoped.legacy_symbols),
            "legacy_count": len(scoped.legacy_symbols),
        },
        "attribution": (
            {
                "source": kr_attribution.ATTRIBUTION_SOURCE,
                "readable": False,
                "unreadable": scoped.unreadable.canonical(),
                "cap_basis": scoped.cap_basis,
            }
            if scoped.unreadable is not None
            else {
                "source": kr_attribution.ATTRIBUTION_SOURCE,
                "readable": True,
                "cap_basis": scoped.cap_basis,
            }
        ),
        "open_orders": {
            "native_broker": {
                "available": False,
                "reason": "TTTC8036R is unsupported for is_mock=True",
            },
            "ledger_shadow": {
                "own_pending_symbols": list(own_symbols),
                "foreign_traces": (
                    foreign_unreadable.canonical()
                    if foreign_unreadable is not None
                    else foreign.canonical()
                    if foreign is not None
                    else None
                ),
            },
        },
        "ledger_pending": (
            own_unreadable.canonical()
            if own_unreadable is not None
            else {"symbols": list(own_symbols), "count": len(own_symbols)}
        ),
        "writer_lease": {"acquired": True, "surface": "b0x_adapter"},
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
    """Record an unreadable account snapshot without exposing exception text."""

    elapsed_seconds = (completed_at - started_at).total_seconds()
    return {
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 3),
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
        "open_orders": {
            "native_broker": {
                "available": False,
                "reason": "account truth unavailable before pending check",
            },
            "ledger_shadow": None,
        },
        "ledger_pending": None,
        "writer_lease": {"acquired": True, "surface": "b0x_adapter"},
        "passed": False,
        "reasons": ["account_truth_unavailable"],
        "error_type": type(error).__name__,
    }


async def _run_prepared_cycle(
    *,
    now: dt.datetime,
    table: PolicyTable,
    envelope: Envelope,
    labels: tuple[str, ...],
    outcome: KrCycleOutcome,
    ledger: ObservationLedger,
    record: dict[str, Any],
    confirm: bool,
    client: Any | None,
    broker: Any | None,
    pending_reader: kr_pending_ledger.PendingReader | None,
    foreign_trace_reader: kr_pending_ledger.ForeignTraceReader | None,
    attribution_reader: kr_attribution.AttributionReader | None,
    account_identity: dict[str, str] | None,
) -> KrCycleOutcome:
    """Account truth → derive → plan → bounded confirm dispatch.

    The caller holds the B0-X artifact lock and, on the confirm path, the
    account-wide kis_mock writer lease.  Keeping the two locks separate is
    deliberate: the local artifact lock protects B0-X's own records while the
    durable lease checks every catalogued kis_mock mutation surface.
    """

    owns_client = client is None
    if owns_client:
        client = kr_mock.ReadOnlyKISMockDomesticClient()

    try:
        preflight_started_at = dt.datetime.now(dt.UTC) if confirm else None
        try:
            fresh = await kr_mock.read_fresh_truth(client)
        except Exception as exc:
            if not confirm:
                raise
            assert preflight_started_at is not None
            assert account_identity is not None
            completed_at = dt.datetime.now(dt.UTC)
            record["preflight"] = _preflight_truth_unavailable_record(
                started_at=preflight_started_at,
                completed_at=completed_at,
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

        reader = pending_reader or kr_pending_ledger.read_own_pending
        pending_now = preflight_started_at if preflight_started_at is not None else now
        try:
            own_pending = await reader(
                now=pending_now, correlation_prefix=f"{kr_mock.CLIENT_ORDER_ID_PREFIX}-"
            )
        except Exception as exc:
            if not confirm:
                raise
            own_pending = kr_pending_ledger.ledger_unreadable(type(exc).__name__)
        record["fresh_truth"] = fresh.status_only(own_pending)
        record["own_pending_source"] = kr_mock.OWN_PENDING_SOURCE

        # §36차 2항 — 자기 원장 귀속.  Read inside the same NW-B4 window as the
        # account snapshot: the two are compared against each other, so a stale
        # attribution against a fresh holdings read is not a usable answer.
        attribution_read = attribution_reader or kr_attribution.read_own_attribution
        try:
            attribution = await attribution_read(
                correlation_prefix=f"{kr_mock.CLIENT_ORDER_ID_PREFIX}-"
            )
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 「알 수 없음」
            attribution = kr_attribution.attribution_unreadable(type(exc).__name__)
        scoped = scoped_positions(fresh=fresh, attribution=attribution)
        record["attribution"] = {
            **scoped.canonical(),
            "source": kr_attribution.ATTRIBUTION_SOURCE,
            "nav_basis": KR_ATTRIBUTED_NAV_BASIS,
        }

        if confirm:
            foreign_reader = (
                foreign_trace_reader or kr_pending_ledger.read_foreign_traces
            )
            try:
                foreign_traces = await foreign_reader(
                    now=pending_now,
                    correlation_prefix=f"{kr_mock.CLIENT_ORDER_ID_PREFIX}-",
                )
            except Exception as exc:
                foreign_traces = kr_pending_ledger.ledger_unreadable(type(exc).__name__)
            assert preflight_started_at is not None
            assert account_identity is not None
            preflight = _confirm_preflight_record(
                started_at=preflight_started_at,
                completed_at=dt.datetime.now(dt.UTC),
                account_identity=account_identity,
                fresh=fresh,
                scoped=scoped,
                own_pending=own_pending,
                foreign_traces=foreign_traces,
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

        state = broker_state(
            fresh=fresh, own_pending=own_pending, attribution=attribution
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
            }
        )

        if decision.tripped:
            notice = decision.operator_notice(
                lane=LANE, remaining_orders_note=KILL_CANCEL_UNSUPPORTED_NOTE
            )
            if notice:
                ledger.record_notice(at=now, text=notice, lane=LANE)
                record["operator_notice"] = notice
            record["planned"] = []
            record["blocked"] = []
            record["submitted"] = []
            record["cancelled"] = []
            record["cancel_status"] = KILL_TRIPPED_CANCEL_UNSUPPORTED
            record["cancel_attempted"] = False
            record["cancel_confirmed"] = False
            record["cancellation_unsupported"] = KILL_CANCEL_UNSUPPORTED_NOTE
        else:
            held = {pos.symbol: pos.quantity for pos in state.positions}
            planned, blocked = kr_mock.plan_orders(
                derivation.orders, envelope=envelope, held_quantities=held
            )
            record["planned"] = [order.to_json() for order in planned]
            record["blocked"] = [order.to_json() for order in blocked]

            submitted: list[dict[str, Any]] = []
            fill_confirmation: list[dict[str, Any]] = []
            if not confirm:
                record["submission_skipped"] = "confirm=False — preview only"
            else:
                active_broker = (
                    broker if broker is not None else kr_mock.build_kis_mock_broker()
                )
                for index, order in enumerate(planned):
                    if index >= MANUAL_CONFIRM_SUBMISSION_LIMIT:
                        record["submission_stopped"] = (
                            "acceptance_submission_limit="
                            f"{MANUAL_CONFIRM_SUBMISSION_LIMIT}"
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

                    # 🔴 §36차 2항, at the mutation boundary: a SELL may only
                    # ever reach shares this lane's own fills paid for. The
                    # derivation already refused legacy symbols (they are not
                    # in ``state.positions`` at all), and this second line is
                    # deliberately redundant — a one-line regression there
                    # would otherwise become a sale of someone else's shares,
                    # the one irreversible mistake available on this lane.
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
                            record["submission_stopped"] = (
                                "legacy_position_sell_blocked"
                            )
                            break

                    # v1.6's dedup is re-read immediately before every real
                    # dispatch.  The artifact's first snapshot is evidence;
                    # this re-read is the mutation boundary.
                    try:
                        current_pending = await reader(
                            now=submitted_at,
                            correlation_prefix=f"{kr_mock.CLIENT_ORDER_ID_PREFIX}-",
                        )
                    except Exception as exc:
                        current_pending = kr_pending_ledger.ledger_unreadable(
                            type(exc).__name__
                        )
                    current_truth = broker_state(
                        fresh=fresh,
                        own_pending=current_pending,
                        attribution=attribution,
                    ).broker_truth
                    try:
                        result = await kr_mock.submit_planned_order(
                            active_broker,
                            planned=order,
                            confirm=True,
                            broker_truth=current_truth,
                        )
                    except OwnPendingResubmitBlocked:
                        record.setdefault("submission_dedup_blocked", []).append(
                            {
                                "symbol": order.symbol,
                                "correlation_id": order.client_order_id,
                                "reason": "v1_6_pending_recheck_blocked",
                            }
                        )
                        record["submission_stopped"] = "v1_6_dedup_blocked"
                        break
                    submitted.append(result)

                    # Prove the mandatory pre-submit trace is visible after a
                    # successful dispatch. The signal ledger is authoritative
                    # for this check even if the post-send order-ledger insert
                    # is unavailable, so it does not depend on ``ledger_id``.
                    if result.get("success"):
                        post_pending = await reader(
                            now=dt.datetime.now(dt.UTC),
                            correlation_prefix=f"{kr_mock.CLIENT_ORDER_ID_PREFIX}-",
                        )
                        observed = (
                            not isinstance(post_pending, PendingUnreadable)
                            and order.symbol in post_pending
                        )
                        record.setdefault("post_submit_dedup", []).append(
                            {
                                "symbol": order.symbol,
                                "correlation_id": order.client_order_id,
                                "observed": observed,
                            }
                        )
                        if not observed:
                            record["submission_stopped"] = "post_submit_dedup_unproven"
                            break

                    if result.get("success") and isinstance(
                        active_broker, kr_mock.KisMockBroker
                    ):
                        try:
                            fill = await active_broker.confirm_fill(result)
                        except Exception as exc:  # noqa: BLE001 — observation only
                            fill_confirmation.append(
                                {
                                    "symbol": order.symbol,
                                    "correlation_id": order.client_order_id,
                                    "confirmed": False,
                                    "error_type": type(exc).__name__,
                                }
                            )
                        else:
                            fill_confirmation.append(
                                {
                                    "symbol": order.symbol,
                                    "correlation_id": order.client_order_id,
                                    "confirmed": fill is not None,
                                    "quantity": (
                                        None if fill is None else str(fill.quantity)
                                    ),
                                    "price": None if fill is None else str(fill.price),
                                }
                            )

            record["submitted"] = submitted
            if fill_confirmation:
                record["fill_confirmation"] = fill_confirmation

        return _persist_outcome(
            outcome=outcome, ledger=ledger, record=record, labels=labels
        )
    finally:
        if owns_client:
            close = getattr(client, "close", None)
            if callable(close):
                await close()


async def run_kr_cycle(
    *,
    now: dt.datetime,
    table_dir: Path = DEFAULT_TABLE_DIR,
    out_dir: Path = DEFAULT_OBSERVATION_DIR,
    confirm: bool = False,
    client: Any | None = None,
    broker: Any | None = None,
    pending_reader: kr_pending_ledger.PendingReader | None = None,
    foreign_trace_reader: kr_pending_ledger.ForeignTraceReader | None = None,
    attribution_reader: kr_attribution.AttributionReader | None = None,
) -> KrCycleOutcome:
    """One manual kis_mock B0-X cycle.

    ``confirm=False`` remains the ordinary observation/derivation path.  A
    confirm call is a separate, default-disabled manual surface: it requires
    the B0-X env gate, the per-call ``confirm=True`` argument, the
    account-wide kis_mock writer lease, and a fresh clean NW-B4 preflight
    before it reaches the existing ``KisMockBroker`` adapter.

    ``pending_reader`` is the v1.6 own-pending seam.  ``foreign_trace_reader``
    is intentionally separate: it never becomes position/cap truth and is
    only the exclusive-account contamination check for a confirm preflight.
    """

    envelope = load_envelope(MARKET)
    assert_envelope_locked(envelope)
    # Keep the KR lane wired to the scope helper even while the initial scope
    # remains sidecar-only. A future, explicit scope expansion must change the
    # rendered KR artifact rather than disappear because this call was absent.
    labels = header_labels(
        lane=LANE,
        extra=(*account_history_labels(LANE), KR_STATUS_LABEL),
    )
    outcome = KrCycleOutcome(lane=LANE, at=now)

    with writer_lock(lane=LANE, root=Path(out_dir).expanduser()):
        ledger = ObservationLedger(lane=LANE, root=Path(out_dir).expanduser())
        ledger.ensure()

        record = base_record(
            market=MARKET, lane=LANE, now=now, envelope=envelope, labels=labels
        )
        _stamp_kr_contract_and_account_map(record)
        record["confirm"] = confirm
        record["realized_pnl_source"] = KR_REALIZED_PNL_UNAVAILABLE

        # --- RTH gate: cheapest check, before any table/account I/O ---
        in_session = is_krx_regular_session(now)
        record["krx_regular_session"] = in_session
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

        # --- table gate ---
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
                client=client,
                broker=broker,
                pending_reader=pending_reader,
                foreign_trace_reader=foreign_trace_reader,
                attribution_reader=attribution_reader,
                account_identity=None,
            )

        try:
            kr_mock.assert_kr_lane_enabled()
            # Validate the configured account identity before any broker/DB
            # truth read.  Only its one-way fingerprint enters the artifact.
            account_identity = kr_mock.account_identity_summary()
        except kr_mock.KrLaneDisabled as exc:
            return _finish_zero_order(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="confirm_gate_not_armed",
                detail=str(exc),
            )

        try:
            async with KISMockWriterLease(writer_surface="b0x_adapter"):
                return await _run_prepared_cycle(
                    now=now,
                    table=table,
                    envelope=envelope,
                    labels=labels,
                    outcome=outcome,
                    ledger=ledger,
                    record=record,
                    confirm=True,
                    client=client,
                    broker=broker,
                    pending_reader=pending_reader,
                    foreign_trace_reader=foreign_trace_reader,
                    attribution_reader=attribution_reader,
                    account_identity=account_identity,
                )
        except WriterSingletonContended as exc:
            return _finish_zero_order(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="preflight_writer_contended",
                detail=str(exc),
            )
        except WriterSingletonUnavailable as exc:
            return _finish_zero_order(
                outcome=outcome,
                ledger=ledger,
                record=record,
                labels=labels,
                reason="preflight_writer_unavailable",
                detail=str(exc),
            )


__all__ = [
    "MARKET",
    "LANE",
    "OUTSIDE_RTH_REASON",
    "PREFLIGHT_MAX_AGE_SECONDS",
    "MANUAL_CONFIRM_SUBMISSION_LIMIT",
    "KILL_TRIPPED_CANCEL_UNSUPPORTED",
    "KR_CONTRACT_VERSION",
    "KR_ACCOUNT_MAP_COMMIT",
    "KR_STATUS_LABEL",
    "KR_REALIZED_PNL_UNAVAILABLE",
    "KR_ATTRIBUTED_NAV_BASIS",
    "ATTRIBUTION_NOT_WIRED",
    "scoped_positions",
    "KrCycleOutcome",
    "broker_state",
    "run_kr_cycle",
]
