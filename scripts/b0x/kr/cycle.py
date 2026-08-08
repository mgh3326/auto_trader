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
``kr_mock.submit_planned_order`` (contract v1.3 ③) — see
``scripts.b0x.kr.mock``'s module docstring for the one documented mismatch
(BUY-leg freshness re-check with no live feed). No CLI path exercises it
this PR: ``scripts/run_b0x_kr_cycle.py`` has no ``--confirm`` flag, so a
cycle only ever reaches ``confirm=True`` when a caller (a test, or a future
operator entrypoint) passes it explicitly. ``KrCycleOutcome.submitted``
always records exactly what happened, never a stamped ``real_orders=0``
constant. orch's relayed X-C lesson was explicit about not repeating that
pattern.

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
from typing import Any

from app.services.kis_mock_runner.session import is_krx_regular_session
from scripts.b0x.cycle import base_record, render_cycle_report
from scripts.b0x.derivation import DerivationResult, derive_orders
from scripts.b0x.envelope import assert_envelope_locked, load_envelope
from scripts.b0x.kill_switch import evaluate as evaluate_kill_switch
from scripts.b0x.kr import mock as kr_mock
from scripts.b0x.labels import header_labels
from scripts.b0x.ledger import (
    DEFAULT_OBSERVATION_DIR,
    ObservationLedger,
    load_json_state,
    writer_lock,
)
from scripts.b0x.state import B0XPosition, LaneAccountState
from scripts.b0x.table_source import (
    DEFAULT_TABLE_DIR,
    PolicyTable,
    TableUnavailable,
    load_policy_table,
)

MARKET = "kr"
LANE = kr_mock.LANE

#: KR's ``table_source.MAX_TABLE_AGE`` entry is 36h — contract §2-2 v1.1
#: (operator-confirmed 2026-08-08, sha256 ``97278b0e8b8000e2e663c936328686001
#: af5850087897270bc80a95ebf8f6b2e``), not a value this adapter chose. See
#: ``tests/scripts/b0x/test_kr_envelope_and_kill_switch.py`` for the
#: regression guard and ``docs/runbooks/b0x-kr-cycle.md`` §8 for the citation.
ZeroOrderReason = str

OUTSIDE_RTH_REASON: ZeroOrderReason = "outside_krx_regular_session"

#: Overrides ``KillSwitchDecision.operator_notice``'s default "잔여 주문 취소
#: 완료" clause — kis_mock has no pending-order inquiry to discover, let
#: alone cancel, what is still resting (see the module docstring). Stating
#: "취소 완료" here would be a false claim from the moment submission is
#: wired, not merely an untested one.
KILL_CANCEL_UNSUPPORTED_NOTE = (
    "신규 주문 중단. 잔여 주문 취소는 구조적으로 시도 불가 — KIS 모의투자 "
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


def attributed_state(
    stored: dict[str, Any] | None,
    *,
    fresh: kr_mock.FreshTruth,
    now: dt.datetime,
) -> LaneAccountState:
    """B0-X-attributed book, cross-checked against a fresh cash/NAV read.

    Mirrors the crypto sidecar's ``_sidecar_state``: positions are the
    locally persisted B0-X-owned book (their average_price/invested_notional
    are B0-X's own cost basis, not derivable from a single venue snapshot),
    while cash and NAV are always the fresh read so a kill decision is never
    made against a stale balance.
    """

    stored = stored or {}
    today = now.astimezone(dt.UTC).date().isoformat()
    same_day = stored.get("utc_day") == today
    positions = tuple(
        B0XPosition(
            symbol=symbol,
            quantity=Decimal(row["quantity"]),
            average_price=Decimal(row["average_price"]),
            invested_notional=Decimal(row["invested_notional"]),
            entry_count=int(row["entry_count"]),
        )
        for symbol, row in sorted((stored.get("positions") or {}).items())
    )
    return LaneAccountState(
        lane=LANE,
        quote_currency=kr_mock.QUOTE_CURRENCY,
        cash=fresh.cash,
        positions=positions,
        new_entry_symbols_today=(
            tuple(sorted(stored.get("new_entry_symbols_today") or []))
            if same_day
            else ()
        ),
        realized_pnl_today=(
            Decimal(stored.get("realized_pnl_today", "0")) if same_day else Decimal("0")
        ),
        nav=fresh.nav,
    )


async def run_kr_cycle(
    *,
    now: dt.datetime,
    table_dir: Path = DEFAULT_TABLE_DIR,
    out_dir: Path = DEFAULT_OBSERVATION_DIR,
    confirm: bool = False,
    client: Any | None = None,
    broker: Any | None = None,
) -> KrCycleOutcome:
    """One kis_mock cycle. ``broker`` mirrors the existing ``client`` DI seam
    (tests inject a fake broker; production leaves it ``None`` and this
    function builds ``kr_mock.build_kis_mock_broker()`` lazily, only when
    there is something to submit). See ``scripts.b0x.kr.mock`` module
    docstring for what submission wiring does and does not cover.
    """

    envelope = load_envelope(MARKET)
    assert_envelope_locked(envelope)
    labels = header_labels()
    outcome = KrCycleOutcome(lane=LANE, at=now)

    with writer_lock(lane=LANE, root=Path(out_dir).expanduser()):
        ledger = ObservationLedger(lane=LANE, root=Path(out_dir).expanduser())
        ledger.ensure()
        state_path = ledger.lane_dir / "attributed_book.json"

        record = base_record(
            market=MARKET, lane=LANE, now=now, envelope=envelope, labels=labels
        )
        record["confirm"] = confirm

        # --- RTH gate: cheapest check, before any table/account I/O ---
        in_session = is_krx_regular_session(now)
        record["krx_regular_session"] = in_session
        if not in_session:
            record["zero_order_reason"] = OUTSIDE_RTH_REASON
            record["zero_order_detail"] = (
                f"now={now.isoformat()} is outside the XKRX regular session "
                "(contract: KRX RTH v1 — 정규장만, NXT/시간외 금지)"
            )
            record["orders"] = []
            record["skipped"] = []
            record["submitted"] = []
            outcome.zero_order_reason = OUTSIDE_RTH_REASON
            ledger.record_cycle(record)
            outcome.record = record
            outcome.artifact_path = ledger.write_artifact(
                name=f"{now.strftime('%Y%m%dT%H%M%SZ')}-cycle.md",
                content=render_cycle_report(record, labels=labels),
            )
            return outcome

        # --- table gate ---
        table, unavailable = _table_or_reason(now=now, table_dir=Path(table_dir))
        if table is None:
            assert unavailable is not None
            record["zero_order_reason"] = unavailable.reason
            record["zero_order_detail"] = unavailable.detail
            record["orders"] = []
            record["skipped"] = []
            record["submitted"] = []
            outcome.zero_order_reason = unavailable.reason
            ledger.record_cycle(record)
            outcome.record = record
            outcome.artifact_path = ledger.write_artifact(
                name=f"{now.strftime('%Y%m%dT%H%M%SZ')}-cycle.md",
                content=render_cycle_report(record, labels=labels),
            )
            return outcome

        outcome.table_hash = table.policy_table_hash
        outcome.table_generated_at = table.generated_at.isoformat()
        outcome.table_age_seconds = int(table.age.total_seconds())
        record["policy_table_hash"] = table.policy_table_hash
        record["policy_table_path"] = str(table.path)
        record["policy_table_generated_at"] = table.generated_at.isoformat()
        record["policy_table_age_seconds"] = int(table.age.total_seconds())

        # --- account state (read-only) ---
        owns_client = client is None
        if owns_client:
            client = kr_mock.ReadOnlyKISMockDomesticClient()
        fresh = await kr_mock.read_fresh_truth(client)
        record["fresh_truth"] = fresh.status_only()

        state = attributed_state(load_json_state(state_path), fresh=fresh, now=now)
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
            record["cancellation_unsupported"] = (
                "kis_mock has no pending-order inquiry for is_mock=True "
                "(DomesticOrderClient.inquire_korea_orders raises); cancel "
                "was not attempted, not attempted-and-failed"
            )
        else:
            held = {pos.symbol: pos.quantity for pos in state.positions}
            planned, blocked = kr_mock.plan_orders(
                derivation.orders, envelope=envelope, held_quantities=held
            )
            record["planned"] = [order.to_json() for order in planned]
            record["blocked"] = [order.to_json() for order in blocked]

            submitted: list[dict[str, Any]] = []
            if not confirm:
                # Preview only, like the crypto sidecar's confirm=False: zero
                # dispatch attempts, planned orders are still fully recorded
                # above. Does NOT construct a broker or call submit_planned_
                # order — a dry preview must not depend on submission wiring
                # at all.
                record["submission_skipped"] = "confirm=False — preview only"
            else:
                # contract v1.3 ③: KisMockBroker (ROB-321/341), reused as-is.
                # See scripts.b0x.kr.mock module docstring for the one
                # documented mismatch (BUY-leg freshness re-check with no
                # live feed) and why it fails closed rather than fabricates
                # a quote.
                active_broker = (
                    broker if broker is not None else (kr_mock.build_kis_mock_broker())
                )
                for order in planned:
                    result = await kr_mock.submit_planned_order(
                        active_broker, planned=order, confirm=confirm
                    )
                    submitted.append(result)
            record["submitted"] = submitted

        ledger.record_cycle(record)
        outcome.record = record
        outcome.artifact_path = ledger.write_artifact(
            name=f"{now.strftime('%Y%m%dT%H%M%SZ')}-cycle.md",
            content=render_cycle_report(record, labels=labels),
        )
        if owns_client:
            close = getattr(client, "close", None)
            if callable(close):
                await close()
        return outcome


__all__ = [
    "MARKET",
    "LANE",
    "OUTSIDE_RTH_REASON",
    "KrCycleOutcome",
    "attributed_state",
    "run_kr_cycle",
]
