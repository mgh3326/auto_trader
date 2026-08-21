"""ROB-1311 W3 round-2 regression tests — the 4 verified blockers (B1-B4).

Source of truth: herdr-inbox/jobs/ROB-1311-verify-20260820-1458/final.md.

- B1: the analyze_stock_batch MCP tool description must not tell the agent
  that quick=True already returns a fresh price / that get_quote is normally
  unnecessary — quick's current_price is a stale DB daily close.
- B2: every quick field removed by PR #1915 relative to the pre-PR quick
  summary must be enumerated in the docs the hard invariant points at
  (CLAUDE.md, MCP README).
- B3: the batched decision_history read model must preserve five ROB-884 /
  ROB-711 invariants: real `overdue`, `open_actions_meta.count`,
  default-mode mock-counterfactual exclusion, `sql_is_learning_eligible()`
  exclusion, and overdue-first ordering / byte-budget enforcement.
- B4: one unresolvable symbol in a quick batch must not abort the whole
  batch; the rest of the batch still returns, plus duplicate/alias inputs
  must not make summary counts self-contradictory.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.models.review import (
    KISLiveOrderLedger,
    KISMockOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
    TradeRetrospective,
    TradeRetrospectiveAction,
)
from app.services.decision_history import _truncate, build_decision_context
from app.services.trade_journal.forecast_service import _normalize_symbol_for_filter

try:
    from app.mcp_server.tooling import analysis_quick
except ImportError:  # baseline-compatible: fail on assertions, not import
    analysis_quick = None  # type: ignore[assignment]

from app.mcp_server.tooling import analysis_tool_handlers as handlers

pytestmark = [
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
    pytest.mark.usefixtures("retrospective_action_control_lock"),
]


def _uniq_symbol() -> str:
    return "R2B" + uuid.uuid4().hex[:9].upper()


async def _add_retro(
    db: AsyncSession,
    *,
    symbol: str,
    correlation_id: str | None = None,
    account_mode: str = "kis_live",
    lesson: str | None = None,
    created_by_profile: str | None = None,
    evidence_snapshot: dict | None = None,
    created_at: datetime | None = None,
) -> TradeRetrospective:
    row = TradeRetrospective(
        symbol=symbol,
        instrument_type="equity_kr",
        account_mode=account_mode,
        market="kr",
        outcome="filled",
        side="sell",
        strategy_key="resistance_ladder",
        correlation_id=correlation_id or f"live:{uuid.uuid4()}",
        realized_pnl=Decimal("100"),
        realized_pnl_currency="KRW",
        pnl_pct=Decimal("1.0"),
        trigger_type="fill",
        lesson=lesson,
        created_by_profile=created_by_profile,
        evidence_snapshot=evidence_snapshot,
        created_at=created_at or datetime(2026, 7, 1, tzinfo=UTC),
    )
    db.add(row)
    await db.flush()
    return row


async def _add_action(
    db: AsyncSession,
    *,
    retrospective_id: int,
    position: int = 0,
    action: str = "Follow up on entry plan",
    status: str = "open",
    due_kst_date=None,
    updated_at: datetime | None = None,
    action_id: uuid.UUID | None = None,
) -> TradeRetrospectiveAction:
    row = TradeRetrospectiveAction(
        id=action_id or uuid.uuid4(),
        retrospective_id=retrospective_id,
        position=position,
        action=action,
        status=status,
        due_kst_date=due_kst_date,
        version=1,
        status_actor="test:rob-1311-r2",
        status_source="migration",
        updated_at=updated_at or datetime(2026, 7, 10, tzinfo=UTC),
    )
    db.add(row)
    await db.flush()
    return row


async def _make_report(db: AsyncSession, **overrides) -> InvestmentReport:
    payload = {
        "report_uuid": uuid.uuid4(),
        "idempotency_key": f"key-{uuid.uuid4()}",
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "test",
        "title": "t",
        "summary": "s",
        "status": "draft",
    }
    payload.update(overrides)
    row = InvestmentReport(**payload)
    db.add(row)
    await db.flush()
    return row


async def _add_item(
    db: AsyncSession, report_id: int, *, symbol: str, **overrides
) -> None:
    payload = {
        "report_id": report_id,
        "item_uuid": uuid.uuid4(),
        "idempotency_key": f"item-{uuid.uuid4()}",
        "item_kind": "action",
        "symbol": symbol,
        "intent": "buy_review",
        "rationale": "지지선 눌림 재진입",
        "evidence_snapshot": {},
        "created_at": datetime(2026, 6, 1, tzinfo=UTC),
    }
    payload.update(overrides)
    db.add(InvestmentReportItem(**payload))
    await db.flush()


def _add_mock_counterfactual_ledger(
    db: AsyncSession, *, symbol: str, correlation_id: str
):
    from app.models.trading import InstrumentType

    db.add(
        KISMockOrderLedger(
            trade_date=datetime(2026, 7, 6, tzinfo=UTC),
            symbol=symbol,
            instrument_type=InstrumentType.equity_kr,
            side="buy",
            order_type="limit",
            quantity=Decimal("5"),
            price=Decimal("1500"),
            amount=Decimal("7500"),
            fee=Decimal("0"),
            currency="KRW",
            order_no=f"MIRROR-{uuid.uuid4().hex[:8]}",
            account_mode="kis_mock",
            broker="kis",
            status="accepted",
            lifecycle_state="fill",
            last_reconcile_detail={"attributed_fill_qty": "5"},
            mirror_cohort="mock_counterfactual",
            mirror_source_bucket="place_original",
            correlation_id=correlation_id,
        )
    )


# ---------------------------------------------------------------------------
# B1 — MCP tool description must not claim quick already returns a fresh
# price / that get_quote is normally unnecessary.
# ---------------------------------------------------------------------------


def test_quick_tool_description_states_stale_price_and_get_quote_guidance():
    import inspect

    from app.mcp_server.tooling import analysis_registration

    source = inspect.getsource(analysis_registration)
    start = source.index('name="analyze_stock_batch"')
    end = source.index("async def analyze_stock_batch", start)
    description = source[start:end]

    assert "already returns" not in description.lower(), (
        "description must not claim quick=True already returns a fresh price"
    )
    assert "normally unnecessary" not in description.lower()
    assert "not a live quote" in description.lower()
    assert "get_quote" in description
    assert "stale" in description.lower()


# ---------------------------------------------------------------------------
# B2 — every field removed from the quick contract must be enumerated in
# CLAUDE.md and the MCP README (the hard invariant demands PR-body/doc
# enumeration, not silent removal).
# ---------------------------------------------------------------------------

_REMOVED_QUICK_FIELDS = (
    "nxt_tradable",
    "price_source",
    "session_state",
    "krx_prev_close",
    "change_pct",
    "venue",
    "quote_asof",
    "delayed",
    "price_data_state",
    "fresh_artifact_exists",
)


def test_claude_md_enumerates_removed_quick_fields():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    claude_md = (repo_root / "CLAUDE.md").read_text(encoding="utf-8")
    section_start = claude_md.index("analyze quick fast projection (ROB-1311)")
    section = claude_md[section_start : section_start + 4000]

    missing = [field for field in _REMOVED_QUICK_FIELDS if field not in section]
    assert not missing, (
        f"CLAUDE.md ROB-1311 section is missing removed fields: {missing}"
    )
    assert "get_quote" in section


def test_mcp_readme_enumerates_removed_quick_fields():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    readme = (repo_root / "app" / "mcp_server" / "README.md").read_text(
        encoding="utf-8"
    )
    section_start = readme.index("analyze_stock_batch(symbols")
    section = readme[section_start : section_start + 3000]

    missing = [field for field in _REMOVED_QUICK_FIELDS if field not in section]
    assert not missing, f"MCP README quick section is missing removed fields: {missing}"


# ---------------------------------------------------------------------------
# B3 — decision_history batch read model must preserve ROB-884/711 invariants
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quick_decision_history_overdue_is_computed_not_hardcoded_false(
    db_session: AsyncSession,
):
    assert analysis_quick is not None
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")
    retro = await _add_retro(db_session, symbol=sym)
    yesterday = now_kst().date() - timedelta(days=1)
    tomorrow = now_kst().date() + timedelta(days=1)
    await _add_action(
        db_session,
        retrospective_id=retro.id,
        position=0,
        action="Overdue action",
        due_kst_date=yesterday,
    )
    await _add_action(
        db_session,
        retrospective_id=retro.id,
        position=1,
        action="Not-yet-due action",
        due_kst_date=tomorrow,
    )
    await db_session.flush()

    ctx = await analysis_quick._load_decision_history_batch(
        db_session, [(raw, "equity_kr")]
    )
    actions = {a["action"]: a for a in ctx[sym.upper()]["open_actions"]}
    assert actions["Overdue action"]["overdue"] is True
    assert actions["Not-yet-due action"]["overdue"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quick_decision_history_open_actions_meta_count_matches_returned_items(
    db_session: AsyncSession,
):
    assert analysis_quick is not None
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")
    retro = await _add_retro(db_session, symbol=sym)
    for i in range(7):
        await _add_action(
            db_session,
            retrospective_id=retro.id,
            position=i,
            action=f"Action {i}",
            updated_at=datetime(2026, 7, 10, i % 12 + 1, tzinfo=UTC),
        )
    await db_session.flush()

    ctx = await analysis_quick._load_decision_history_batch(
        db_session, [(raw, "equity_kr")]
    )
    row = ctx[sym.upper()]
    assert row["open_actions_meta"]["count"] == len(row["open_actions"])
    assert row["open_actions_meta"]["count"] > 0
    assert row["open_actions_meta"]["truncated"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quick_decision_history_default_excludes_mock_counterfactual(
    db_session: AsyncSession,
):
    assert analysis_quick is not None
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")

    drop_corr = f"mirror-mock:{uuid.uuid4()}"
    _add_mock_counterfactual_ledger(db_session, symbol=sym, correlation_id=drop_corr)
    retro_drop = await _add_retro(
        db_session,
        symbol=sym,
        correlation_id=drop_corr,
        account_mode="kis_mock",
        lesson="MIRROR COUNTERFACTUAL LESSON",
    )
    await _add_action(
        db_session, retrospective_id=retro_drop.id, action="mirror action"
    )

    keep_corr = f"legacy-mock:{uuid.uuid4()}"
    retro_keep = await _add_retro(
        db_session,
        symbol=sym,
        correlation_id=keep_corr,
        account_mode="kis_mock",
        lesson="LEGACY MOCK LESSON",
    )
    await _add_action(
        db_session, retrospective_id=retro_keep.id, action="legacy mock action"
    )
    await db_session.flush()

    ctx = await analysis_quick._load_decision_history_batch(
        db_session, [(raw, "equity_kr")], account_mode=None
    )
    row = ctx[sym.upper()]
    lessons = row["prior_lessons"]
    actions = [a["action"] for a in row["open_actions"]]

    assert "MIRROR COUNTERFACTUAL LESSON" not in lessons
    assert "LEGACY MOCK LESSON" in lessons
    assert "mirror action" not in actions
    assert "legacy mock action" in actions


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quick_decision_history_excludes_intake_retrospectives(
    db_session: AsyncSession,
):
    assert analysis_quick is not None
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")

    await _add_retro(
        db_session,
        symbol=sym,
        lesson="INTAKE LESSON",
        evidence_snapshot={"retrospective_type": "intake"},
    )
    await _add_retro(db_session, symbol=sym, lesson="EXECUTION LESSON")
    await db_session.flush()

    ctx = await analysis_quick._load_decision_history_batch(
        db_session, [(raw, "equity_kr")]
    )
    lessons = ctx[sym.upper()]["prior_lessons"]
    assert "INTAKE LESSON" not in lessons
    assert "EXECUTION LESSON" in lessons


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quick_decision_history_ordering_overdue_first_then_due_asc(
    db_session: AsyncSession,
):
    assert analysis_quick is not None
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")
    retro = await _add_retro(db_session, symbol=sym)
    today = now_kst().date()
    # Not overdue, later due date -> should sort AFTER the overdue one.
    await _add_action(
        db_session,
        retrospective_id=retro.id,
        position=0,
        action="Due later",
        due_kst_date=today + timedelta(days=5),
    )
    # Overdue -> should sort FIRST regardless of insertion order.
    await _add_action(
        db_session,
        retrospective_id=retro.id,
        position=1,
        action="Overdue",
        due_kst_date=today - timedelta(days=3),
    )
    await db_session.flush()

    ctx = await analysis_quick._load_decision_history_batch(
        db_session, [(raw, "equity_kr")]
    )
    ordered = [a["action"] for a in ctx[sym.upper()]["open_actions"]]
    assert ordered[0] == "Overdue"
    assert ordered[-1] == "Due later"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quick_decision_history_open_actions_respect_byte_budget(
    db_session: AsyncSession,
):
    assert analysis_quick is not None
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")
    retro = await _add_retro(db_session, symbol=sym)
    for i in range(5):
        await _add_action(
            db_session,
            retrospective_id=retro.id,
            position=i,
            action="가" * 220,  # multi-byte-heavy, near the 220c text limit
            updated_at=datetime(2026, 7, 10, i % 12 + 1, tzinfo=UTC),
        )
    await db_session.flush()

    ctx = await analysis_quick._load_decision_history_batch(
        db_session, [(raw, "equity_kr")]
    )
    row = ctx[sym.upper()]
    payload_bytes = len(
        json.dumps(row["open_actions"], ensure_ascii=False).encode("utf-8")
    )
    assert payload_bytes <= 3072
    assert row["open_actions_meta"]["count"] == len(row["open_actions"])


# ---------------------------------------------------------------------------
# B4 — one unresolvable symbol must not abort the whole quick batch; and
# duplicate/alias inputs must not make summary counts self-contradictory.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quick_batch_partial_success_when_one_symbol_is_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_projection(symbols, *, market, **kwargs):
        return {
            str(symbol): {"symbol": str(symbol), "current_price": 1.0}
            for symbol in symbols
        }

    monkeypatch.setattr(handlers, "_load_quick_projection_batch", fake_projection)

    result = await handlers.analyze_stock_batch_impl(
        ["005930", "$$$bad$$$", "000660"], include_position=False
    )

    assert result["summary"]["total_symbols"] == 3
    assert result["summary"]["successful"] == 2
    assert result["summary"]["failed"] == 1
    assert "005930" in result["results"]
    assert "000660" in result["results"]
    assert "error" in result["results"]["$$$bad$$$"]
    assert (
        result["summary"]["successful"] + result["summary"]["failed"]
        == (result["summary"]["total_symbols"])
    )


@pytest.mark.asyncio
async def test_quick_batch_all_symbols_unresolvable_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fail_projection(*args, **kwargs):
        raise AssertionError("must not call the DB loader with zero valid symbols")

    monkeypatch.setattr(handlers, "_load_quick_projection_batch", fail_projection)

    result = await handlers.analyze_stock_batch_impl(
        ["$$$bad1$$$", "$$$bad2$$$"], include_position=False
    )

    assert result["summary"]["successful"] == 0
    assert result["summary"]["failed"] == 2
    assert result["summary"]["total_symbols"] == 2


@pytest.mark.asyncio
async def test_quick_batch_duplicate_symbols_summary_counts_are_self_consistent(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_projection(symbols, *, market, **kwargs):
        return {
            str(symbol): {"symbol": str(symbol), "current_price": 1.0}
            for symbol in symbols
        }

    monkeypatch.setattr(handlers, "_load_quick_projection_batch", fake_projection)

    result = await handlers.analyze_stock_batch_impl(
        ["005930", "A005930", "005930"], market="kr", include_position=False
    )

    summary = result["summary"]
    assert summary["successful"] + summary["failed"] == summary["total_symbols"]
    assert summary["total_symbols"] == len(result["results"])


# ---------------------------------------------------------------------------
# B5 — decision_history recent_fills must branch on account_mode like the
# canonical decision_history._recent_fills: kis_mock queries ONLY the mock
# ledger (mirror_cohort=mock_counterfactual, lifecycle_state=fill); non-mock
# queries the live ledgers. Currently analysis_quick queries the live ledgers
# unconditionally, regardless of account_mode.
# ---------------------------------------------------------------------------


class _SpySession:
    """Records which SQL statements executed; empty result for every query."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement: object, params: object | None = None):
        self.statements.append(str(statement))

        class _Result:
            def scalars(self) -> _Result:
                return self

            def all(self) -> list[object]:
                return []

        return _Result()

    def _hit(self, table: str) -> bool:
        return any(table in sql for sql in self.statements)

    def _hit_mock_fill_query(self) -> bool:
        # The default-mode retrospective visibility predicate also references
        # kis_mock_order_ledger (mock-counterfactual EXISTS subquery), so a
        # bare substring match on the table name is not selective enough.
        # The actual recent_fills query is the only statement that combines
        # this table with a lifecycle_state filter.
        return any(
            "kis_mock_order_ledger" in sql and "lifecycle_state" in sql
            for sql in self.statements
        )


@pytest.mark.asyncio
async def test_quick_decision_history_kis_mock_recent_fills_queries_only_mock_ledger():
    assert analysis_quick is not None
    session = _SpySession()

    await analysis_quick._load_decision_history_batch(
        session, [("005930", "equity_kr")], account_mode="kis_mock"
    )

    assert session._hit_mock_fill_query(), (
        "kis_mock account_mode must query the mock ledger for recent_fills"
    )
    assert not session._hit("kis_live_order_ledger"), (
        "kis_mock account_mode must NOT query the live KIS ledger"
    )
    assert not session._hit("review.live_order_ledger"), (
        "kis_mock account_mode must NOT query the live order ledger"
    )
    assert not session._hit("toss_live_order_ledger"), (
        "kis_mock account_mode must NOT query the Toss live ledger"
    )


@pytest.mark.asyncio
async def test_quick_decision_history_non_mock_recent_fills_queries_live_ledgers():
    assert analysis_quick is not None
    session = _SpySession()

    await analysis_quick._load_decision_history_batch(
        session, [("005930", "equity_kr")], account_mode=None
    )

    assert session._hit("kis_live_order_ledger")
    assert session._hit("toss_live_order_ledger")
    assert not session._hit_mock_fill_query(), (
        "default/live account_mode must NOT query the mock ledger for recent_fills"
    )


# ---------------------------------------------------------------------------
# Round-3 correction — independent verifier (herdr-inbox/jobs/
# ROB-1311-verify-r2-20260820-1530/final.md) found two remaining functional
# blockers with real DB rows at the R2 fixed point:
#
# B5: default/live recent_fills iterates KIS -> generic-live -> Toss and caps
#     at six DURING that source-ordered iteration, so it can return six older
#     KIS rows while dropping newer generic-live/Toss evidence. Canonical
#     `decision_history._recent_fills` collects ALL rows, sorts by trade_date
#     desc, THEN caps at six (MAX_FILLS). These populated-row tests pin
#     equality against the canonical helper across the two-source-plus-mock
#     interleave, and additionally lock the (already-correct) kis_mock
#     partial-fill parity so a regression there would also be caught.
#
# B3: the lesson payload used `str(lesson)[:219]` — no strip/blank-check, no
#     whitespace normalization, and a different truncation/ellipsis rule than
#     canonical `_truncate`. The adjacent prior-decision rationale path
#     returned the raw (untruncated) rationale. These tests pin real-DB
#     equality against `build_decision_context` for both payload shapes.
#
# SHOULD: a swallowed decision-history batch failure must emit a diagnostic
# (matching the established full-path `logger.debug("decision_history
# injection skipped: %s", exc)` fail-open observability) while the price
# projection stays fail-open (unaffected).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quick_recent_fills_globally_ordered_across_sources_before_cap(
    db_session: AsyncSession,
):
    assert analysis_quick is not None
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")

    # 6 older KIS fills — enough to fill the per-symbol cap on their own if
    # the buggy source-then-cap behavior is in play.
    for i in range(6):
        db_session.add(
            KISLiveOrderLedger(
                trade_date=datetime(2026, 6, 1 + i, tzinfo=UTC),
                symbol=sym,
                instrument_type="equity_kr",
                side="buy",
                order_type="limit",
                account_mode="kis_live",
                broker="kis",
                status="filled",
                lifecycle_state="filled",
                order_no=f"KIS-{raw}-{i}",
                quantity=Decimal("1"),
                filled_qty=Decimal("1"),
                avg_fill_price=Decimal("1000"),
            )
        )
    # Newer generic-live fill than every KIS row above.
    db_session.add(
        LiveOrderLedger(
            trade_date=datetime(2026, 6, 20, tzinfo=UTC),
            broker="alpaca",
            account_scope="alpaca_live",
            market="us",
            symbol=sym,
            side="buy",
            order_kind="market",
            status="filled",
            lifecycle_state="filled",
            order_no=f"LIVE-{raw}",
            quantity=Decimal("2"),
            filled_qty=Decimal("2"),
            avg_fill_price=Decimal("2000"),
        )
    )
    # Newest of all: a Toss fill.
    db_session.add(
        TossLiveOrderLedger(
            trade_date=datetime(2026, 6, 25, tzinfo=UTC),
            broker="toss",
            account_mode="toss_live",
            operation_kind="place",
            market="kr",
            symbol=sym,
            side="buy",
            order_type="limit",
            client_order_id=f"TOSS-{raw}",
            status="filled",
            quantity=Decimal("3"),
            filled_qty=Decimal("3"),
            avg_fill_price=Decimal("3000"),
        )
    )
    await db_session.flush()

    canonical_ctx = await build_decision_context(db_session, symbol=raw, market="kr")
    assert canonical_ctx is not None
    canonical_fills = canonical_ctx["recent_fills"]

    quick_ctx = await analysis_quick._load_decision_history_batch(
        db_session, [(raw, "equity_kr")]
    )
    quick_fills = quick_ctx[sym.upper()]["recent_fills"]

    assert quick_fills == canonical_fills, (
        "quick recent_fills must be globally date-ordered across all live "
        "ledger sources (not source-then-cap) to equal canonical output"
    )
    assert quick_fills[0]["source"] == "toss", (
        "the newest row (Toss) must lead recent_fills, not be dropped by a "
        "source-first six-item cap"
    )
    assert len(quick_fills) == 6


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quick_recent_fills_kis_mock_partial_fill_matches_canonical(
    db_session: AsyncSession,
):
    assert analysis_quick is not None
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")
    from app.models.trading import InstrumentType

    db_session.add(
        KISMockOrderLedger(
            trade_date=datetime(2026, 6, 15, tzinfo=UTC),
            symbol=sym,
            instrument_type=InstrumentType.equity_kr,
            side="buy",
            order_type="limit",
            quantity=Decimal("10"),
            price=Decimal("100.5"),
            amount=Decimal("1005"),
            fee=Decimal("0"),
            currency="KRW",
            order_no=f"MOCK-{raw}",
            account_mode="kis_mock",
            broker="kis",
            status="accepted",
            lifecycle_state="fill",
            last_reconcile_detail={"attributed_fill_qty": "2.5"},
            mirror_cohort="mock_counterfactual",
            mirror_source_bucket="place_original",
            correlation_id=f"mock:{uuid.uuid4()}",
        )
    )
    await db_session.flush()

    canonical_ctx = await build_decision_context(
        db_session, symbol=raw, market="kr", account_mode="kis_mock"
    )
    assert canonical_ctx is not None
    canonical_fills = canonical_ctx["recent_fills"]

    quick_ctx = await analysis_quick._load_decision_history_batch(
        db_session, [(raw, "equity_kr")], account_mode="kis_mock"
    )
    quick_fills = quick_ctx[sym.upper()]["recent_fills"]

    assert quick_fills == canonical_fills
    assert quick_fills[0]["status"] == "partial"
    assert quick_fills[0]["filled_qty"] == 2.5


@pytest.mark.asyncio
async def test_quick_prior_lesson_whitespace_and_truncation_match_canonical(
    db_session: AsyncSession,
):
    assert analysis_quick is not None
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")
    await _add_retro(db_session, symbol=sym, lesson="  Alpha\n\tBeta  ")
    await db_session.flush()

    canonical_ctx = await build_decision_context(db_session, symbol=raw, market="kr")
    assert canonical_ctx is not None
    canonical_lessons = canonical_ctx["prior_lessons"]

    quick_ctx = await analysis_quick._load_decision_history_batch(
        db_session, [(raw, "equity_kr")]
    )
    quick_lessons = quick_ctx[sym.upper()]["prior_lessons"]

    assert quick_lessons == canonical_lessons
    assert quick_lessons == ["Alpha Beta"], (
        "lesson text must be whitespace-normalized like canonical `_truncate`, "
        "not a raw str(...)[:219] slice"
    )


@pytest.mark.asyncio
async def test_quick_prior_decision_rationale_truncated_like_canonical(
    db_session: AsyncSession,
):
    assert analysis_quick is not None
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")
    report = await _make_report(db_session)
    long_rationale = "근거 " * 100  # well past the canonical 220-char limit
    await _add_item(
        db_session,
        report.id,
        symbol=sym,
        rationale=long_rationale,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await db_session.flush()

    canonical_ctx = await build_decision_context(db_session, symbol=raw, market="kr")
    assert canonical_ctx is not None
    canonical_rationale = canonical_ctx["prior_decisions"][0]["rationale"]

    quick_ctx = await analysis_quick._load_decision_history_batch(
        db_session, [(raw, "equity_kr")]
    )
    quick_rationale = quick_ctx[sym.upper()]["prior_decisions"][0]["rationale"]

    assert quick_rationale == canonical_rationale
    assert quick_rationale == _truncate(long_rationale)
    assert quick_rationale != long_rationale, "rationale must actually be truncated"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quick_decision_history_batch_failure_logs_diagnostic_and_stays_fail_open(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    assert analysis_quick is not None

    async def boom(*args, **kwargs):
        raise RuntimeError("synthetic decision-history batch failure")

    monkeypatch.setattr(analysis_quick, "_load_decision_history_batch", boom)

    with caplog.at_level(
        logging.WARNING, logger="app.mcp_server.tooling.analysis_quick"
    ):
        result = await analysis_quick.load_quick_projection_batch(
            [(_uniq_symbol(), "equity_kr")]
        )

    row = next(iter(result.values()))
    assert "decision_history" not in row, "must stay fail-open on batch failure"
    assert row["data_state"] == "missing"

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, (
        "a swallowed decision-history batch failure must emit a diagnostic, "
        "matching the established full-path fail-open observability"
    )
    assert any("decision" in r.getMessage().lower() for r in warnings)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quick_decision_history_batch_failure_log_excludes_exception_payload(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """The diagnostic must never format the raw exception into the log.

    A DB/programming exception's message, args, or repr can carry values the
    caller never intended to expose (e.g. a connection string, a row value,
    or — in the worst case — a credential-shaped string an upstream library
    embedded in an error). The established full-path pattern
    (`logger.debug("decision_history injection skipped: %s", exc)`) formats
    the raw exception; the quick path must NOT copy that here — only a fixed
    message and/or the exception CLASS NAME are safe to log.
    """
    assert analysis_quick is not None

    fake_secret = "FAKE_SECRET_TOKEN_rob1311_r3_sentinel_9f3c2a"

    async def boom(*args, **kwargs):
        raise RuntimeError(f"db error near {fake_secret}")

    monkeypatch.setattr(analysis_quick, "_load_decision_history_batch", boom)

    with caplog.at_level(
        logging.WARNING, logger="app.mcp_server.tooling.analysis_quick"
    ):
        result = await analysis_quick.load_quick_projection_batch(
            [(_uniq_symbol(), "equity_kr")]
        )

    row = next(iter(result.values()))
    assert "decision_history" not in row, "must stay fail-open on batch failure"
    assert row["data_state"] == "missing"

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a swallowed decision-history batch failure must emit a diagnostic"

    for record in warnings:
        assert fake_secret not in record.getMessage(), (
            "the formatted log message must not contain the raw exception text"
        )
        assert fake_secret not in str(record.args), (
            "the raw exception must not be passed as a lazy-format arg either"
        )
        assert fake_secret not in (record.exc_text or ""), (
            "no traceback/exc_info carrying the raw exception may be attached"
        )
        if record.exc_info:
            import traceback

            formatted = "".join(traceback.format_exception(*record.exc_info))
            assert fake_secret not in formatted, (
                "exc_info must not be attached with the raw exception traceback"
            )


# ---------------------------------------------------------------------------
# Additional coverage — two edge cases the R3 populated-row tests above did
# not pin: lesson truncation actually crossing the 220-char boundary with the
# canonical ellipsis marker, and a prior-decision row whose SMOKE marker
# lives only on `status` (not `rationale`). Runtime behavior for both is
# already correct as of `02bd221cf` (canonical `_truncate`/`_is_smoke` reuse);
# these are coverage-only additions, not a defect RED/GREEN pair.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quick_prior_lesson_over_limit_truncated_with_ellipsis_like_canonical(
    db_session: AsyncSession,
):
    assert analysis_quick is not None
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")
    long_lesson = "레슨 " * 100  # well past the canonical 220-char limit
    await _add_retro(db_session, symbol=sym, lesson=long_lesson)
    await db_session.flush()

    canonical_ctx = await build_decision_context(db_session, symbol=raw, market="kr")
    assert canonical_ctx is not None
    canonical_lessons = canonical_ctx["prior_lessons"]

    quick_ctx = await analysis_quick._load_decision_history_batch(
        db_session, [(raw, "equity_kr")]
    )
    quick_lessons = quick_ctx[sym.upper()]["prior_lessons"]

    assert quick_lessons == canonical_lessons
    assert quick_lessons == [_truncate(long_lesson)]
    assert quick_lessons[0].endswith("…"), (
        "an over-limit lesson must carry the canonical ellipsis truncation marker"
    )
    assert len(quick_lessons[0]) == 220


class _FakePriorRowSession:
    """Fake AsyncSession returning one fabricated (never persisted) row for
    the prior-decisions query and empty results for every other query
    `_load_decision_history_batch` issues.

    `investment_report_items.status` is CHECK-constrained to
    ``proposed|approved|denied|deferred|activated|expired`` — none of which
    can ever carry a "smoke" marker, so a real committed row can never
    exercise canonical `_is_smoke(rationale, status)`'s status-branch for
    this table. This fake exercises the real `_load_decision_history_batch`
    code path (attribute reads + `_is_smoke` call) against a fabricated row
    instead of a real DB insert.
    """

    def __init__(self, prior_rows: list[Any]) -> None:
        self._prior_rows = prior_rows

    async def execute(self, statement: object, params: object | None = None):
        text = str(statement)

        class _Result:
            def __init__(self, rows: list[Any]) -> None:
                self._rows = rows

            def scalars(self) -> _Result:
                return self

            def all(self) -> list[Any]:
                return self._rows

        if "investment_report_items" in text:
            return _Result(self._prior_rows)
        return _Result([])


@pytest.mark.asyncio
async def test_quick_prior_decision_status_smoke_marker_excludes_row():
    assert analysis_quick is not None
    from types import SimpleNamespace

    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")

    smoke_row = SimpleNamespace(
        symbol=sym,
        rationale="ordinary rationale, no marker here",
        status="smoke_test_status",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        intent="buy_review",
        side="buy",
        decision_bucket="new_buy_candidate",
        confidence=Decimal("70"),
    )
    session = _FakePriorRowSession([smoke_row])

    ctx = await analysis_quick._load_decision_history_batch(
        session, [(raw, "equity_kr")]
    )

    assert sym.upper() not in ctx or not ctx[sym.upper()]["prior_decisions"], (
        "a prior-decision row whose SMOKE marker lives only on `status` "
        "(not `rationale`) must still be excluded, matching canonical "
        "`_is_smoke(rationale, status)`"
    )


# ---------------------------------------------------------------------------
# R6 — remaining active consumer contracts that still describe the pre-fix
# quick semantics (live price / consensus / position from a quick call, a
# `mode=quick` or `max_symbols` argument that was never a real parameter, or
# missing quick=False annotations for upside/deep-confirm/position guidance).
# ---------------------------------------------------------------------------


def test_get_quote_description_does_not_claim_quick_batch_has_fresh_price():
    import inspect

    from app.mcp_server.tooling import market_data_quotes

    source = inspect.getsource(market_data_quotes)
    start = source.index('name="get_quote"')
    end = source.index("async def get_quote", start)
    description = source[start:end]

    assert "already includes" not in description.lower(), (
        "get_quote description must not claim a planned analyze_stock_batch "
        "call already includes a fresh price — quick=True (the default) is "
        "a DB-only stale projection with no live price fetch"
    )
    if "analyze_stock_batch" in description:
        assert "quick=false" in description.lower(), (
            "any redundancy claim against analyze_stock_batch must be "
            "scoped to quick=False (the only path that fetches a live "
            "price internally)"
        )


def test_route_request_buy_lane_uses_real_quick_parameter_name():
    from app.mcp_server.tooling.route_request_lanes import LANE_SEQUENCES

    buy_step = next(
        step for step in LANE_SEQUENCES["buy"] if step["tool"] == "analyze_stock_batch"
    )
    purpose = buy_step["purpose"]

    assert "mode=quick" not in purpose, (
        "analyze_stock_batch has no `mode` parameter — the real parameter "
        "is `quick` (bool, default True)"
    )
    assert "max_symbols" not in purpose, (
        "analyze_stock_batch has no `max_symbols` parameter"
    )
    assert "consensus" not in purpose.lower(), (
        "quick=True no longer returns consensus — removed by PR #1915"
    )
    assert "per-account position" not in purpose.lower(), (
        "analyze_stock_batch never attaches a position field regardless of "
        "quick (include_position is always forced False internally) — "
        "use get_holdings"
    )


def test_route_request_sell_and_discovery_lanes_annotate_quick_false_for_upside_and_deep_confirm():
    from app.mcp_server.tooling.route_request_lanes import LANE_SEQUENCES

    sell_step = next(
        step for step in LANE_SEQUENCES["sell"] if step["tool"] == "analyze_stock_batch"
    )
    assert "quick=false" in sell_step["purpose"].lower(), (
        "the sell lane's analyze_stock_batch step confirms 'upside', which "
        "requires the full quick=False path — this must be explicit so a "
        "caller does not rely on the quick=True default and get nothing"
    )

    discovery_step = next(
        step
        for step in LANE_SEQUENCES["discovery"]
        if step["tool"] == "analyze_stock_batch"
    )
    assert "quick=false" in discovery_step["purpose"].lower(), (
        "the discovery lane's 'deep confirm' analyze_stock_batch step must "
        "explicitly say quick=False — quick=True is the default and would "
        "silently skip the deep confirmation this step exists to do"
    )


def test_playbook_buy_lane_uses_real_quick_parameter_name():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    playbook = (
        repo_root / "docs" / "playbooks" / "trading-decision-playbook.md"
    ).read_text(encoding="utf-8")

    assert "mode=quick" not in playbook, (
        "trading-decision-playbook.md prose must use the real "
        "analyze_stock_batch parameter `quick`, not `mode`"
    )
    assert "mode: quick" not in playbook, (
        "trading-decision-playbook.md machine-readable args must use the "
        "real analyze_stock_batch parameter `quick`, not `mode`"
    )

    tool_start = playbook.index("tool: analyze_stock_batch\n        args:")
    args_line_start = playbook.index("args:", tool_start)
    args_line_end = playbook.index("\n", args_line_start)
    args_line = playbook[args_line_start:args_line_end]
    assert "max_symbols" not in args_line, (
        "analyze_stock_batch has no `max_symbols` parameter — the "
        "machine-readable buy-lane args block must not invent one"
    )


def test_playbook_buy_prose_position_guidance_matches_get_holdings_contract():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    playbook = (
        repo_root / "docs" / "playbooks" / "trading-decision-playbook.md"
    ).read_text(encoding="utf-8")

    section_start = playbook.index("## 1) Buy pipeline")
    section = playbook[section_start : section_start + 1500]

    assert "position require `quick=false`" not in section.lower(), (
        "analyze_stock_batch never attaches a `position` field for any "
        "quick value (include_position is always forced False internally) "
        "— the playbook must not imply quick=False returns it"
    )
    assert "get_holdings" in section, (
        "the buy-pipeline prose must point position lookups at get_holdings, "
        "the only tool that returns per-account position"
    )


def test_playbook_sell_lane_upside_step_annotates_quick_false():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    playbook = (
        repo_root / "docs" / "playbooks" / "trading-decision-playbook.md"
    ).read_text(encoding="utf-8")

    section_start = playbook.index("## 2) Sell (profit-taking) pipeline")
    section = playbook[section_start : section_start + 600]

    assert "quick=false" in section.lower(), (
        "the sell pipeline's analyze_stock_batch step (confirm distance to "
        "resistance, RSI, upside) must explicitly say quick=False — upside "
        "is not part of the quick=True (default) allowlist"
    )


def test_mcp_readme_screener_snapshot_section_does_not_claim_live_holdings_or_enrichment():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    readme = (repo_root / "app" / "mcp_server" / "README.md").read_text(
        encoding="utf-8"
    )
    section_start = readme.index("screen_stocks_snapshot(preset=None")
    next_tool_start = readme.index("get_top_stocks(market=", section_start)
    section = readme[section_start:next_tool_start]

    assert "kis-live portfolio" not in section.lower(), (
        "screen_stocks_snapshot is DB-only (ROB-1309) and never calls "
        "live KIS holdings — that claim belongs to screen_stocks_enrich"
    )
    assert "returned rows include `analysiscontext`" not in section.lower(), (
        "screen_stocks_snapshot never returns analysisContext (consensus, "
        "RSI) inline anymore — that requires the separate "
        "screen_stocks_enrich tool"
    )
    assert "zero external http" in section.lower(), (
        "the README must state the DB-only / zero-external-HTTP contract "
        "for screen_stocks_snapshot (ROB-1309)"
    )
    assert "screen_stocks_enrich" in readme, (
        "the README must document the screen_stocks_enrich tool that now "
        "owns live KIS holdings / analyst-consensus / sector enrichment"
    )


# ---------------------------------------------------------------------------
# R7 — independent-audit corrections: positive contracts (not just absence
# of stale text), structural YAML parsing for machine-readable playbook
# args, heading-bounded prose extraction, README internal-consistency
# (analyze_stock_batch section must not contradict the snapshot/enrich
# split), W2 portfolio/sellable README contract preservation, and the
# include_position no-effect-for-any-quick-value contract on both active
# description surfaces.
# ---------------------------------------------------------------------------


def _playbook_text() -> str:
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    return (
        repo_root / "docs" / "playbooks" / "trading-decision-playbook.md"
    ).read_text(encoding="utf-8")


def _readme_text() -> str:
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    return (repo_root / "app" / "mcp_server" / "README.md").read_text(encoding="utf-8")


def _heading_section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    end = text.index(next_heading, start)
    return text[start:end]


_YAML_BLOCK_RE_R7 = re.compile(r"```yaml\n(.*?)```", re.DOTALL)


def _lane_yaml_block(playbook: str, marker_comment: str) -> dict:
    for block in _YAML_BLOCK_RE_R7.findall(playbook):
        if marker_comment in block:
            parsed = yaml.safe_load(block)
            assert isinstance(parsed, dict)
            return parsed
    raise AssertionError(f"no yaml block found containing {marker_comment!r}")


def _find_tool_step(node, tool_name: str) -> dict | None:
    if isinstance(node, dict):
        if node.get("tool") == tool_name:
            return node
        for value in node.values():
            found = _find_tool_step(value, tool_name)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_tool_step(item, tool_name)
            if found is not None:
                return found
    return None


def test_get_quote_description_positive_contract_states_live_price_source():
    import inspect

    from app.mcp_server.tooling import market_data_quotes

    source = inspect.getsource(market_data_quotes)
    start = source.index('name="get_quote"')
    end = source.index("async def get_quote", start)
    description = source[start:end].lower()

    assert "quick=true" in description, (
        "get_quote description must positively state that "
        "analyze_stock_batch's quick=True path is the one that never "
        "fetches a live price"
    )
    assert "db-only" in description, (
        "get_quote description must positively state quick=True is DB-only"
    )
    assert "always call get_quote" in description, (
        "get_quote description must positively state that get_quote itself "
        "is the live-price source"
    )
    assert "always call get_quote for that" in description, (
        "get_quote description must literally state that get_quote is the "
        "thing to call for a live price"
    )


def test_route_request_buy_purpose_positive_contract():
    from app.mcp_server.tooling.route_request_lanes import LANE_SEQUENCES

    buy_step = next(
        step for step in LANE_SEQUENCES["buy"] if step["tool"] == "analyze_stock_batch"
    )
    purpose = buy_step["purpose"].lower()

    assert "quick=true" in purpose
    assert "db-only" in purpose
    assert "get_quote" in purpose
    assert "get_holdings" in purpose, (
        "position lookups must be routed to get_holdings, the only tool "
        "that returns per-account position"
    )


def test_route_request_sell_purpose_requires_upside_and_quick_false_together():
    from app.mcp_server.tooling.route_request_lanes import LANE_SEQUENCES

    sell_step = next(
        step for step in LANE_SEQUENCES["sell"] if step["tool"] == "analyze_stock_batch"
    )
    purpose = sell_step["purpose"].lower()

    assert "upside" in purpose, (
        "must retain the positive claim that this step confirms upside"
    )
    assert "quick=false" in purpose, (
        "must explicitly require quick=False for that upside claim to be true"
    )


def test_route_request_discovery_purpose_requires_deep_confirm_and_quick_false_together():
    from app.mcp_server.tooling.route_request_lanes import LANE_SEQUENCES

    discovery_step = next(
        step
        for step in LANE_SEQUENCES["discovery"]
        if step["tool"] == "analyze_stock_batch"
    )
    purpose = discovery_step["purpose"].lower()

    assert "deep confirm" in purpose
    assert "quick=false" in purpose


def test_playbook_buy_lane_analyze_step_args_are_exactly_quick_true_structural():
    playbook = _playbook_text()
    parsed = _lane_yaml_block(playbook, "playbook-machine-readable: buy lane")
    step = _find_tool_step(parsed, "analyze_stock_batch")
    assert step is not None, "buy lane must have an analyze_stock_batch step"
    assert step.get("args") == {"quick": True}, (
        f"buy lane analyze_stock_batch args must be exactly "
        f"{{'quick': True}} — no mode/max_symbols/unknown keys, got "
        f"{step.get('args')!r}"
    )


def test_playbook_sell_lane_analyze_step_args_are_exactly_quick_false_structural():
    playbook = _playbook_text()
    parsed = _lane_yaml_block(playbook, "playbook-machine-readable: sell lane")
    step = _find_tool_step(parsed, "analyze_stock_batch")
    assert step is not None, "sell lane must have an analyze_stock_batch step"
    assert step.get("args") == {"quick": False}, (
        "sell lane's analyze_stock_batch step confirms 'upside', which "
        "requires the full analysis path — this must be a real `args: "
        f"{{quick: false}}` key, not just a comment; got {step.get('args')!r}"
    )


def test_playbook_discovery_lane_analyze_step_args_are_exactly_quick_false_structural():
    playbook = _playbook_text()
    parsed = _lane_yaml_block(playbook, "playbook-machine-readable: discovery lane")
    step = _find_tool_step(parsed, "analyze_stock_batch")
    assert step is not None, "discovery lane must have an analyze_stock_batch step"
    assert step.get("args") == {"quick": False}, (
        "discovery lane's 'deep confirm' analyze_stock_batch step must be a "
        "real `args: {quick: false}` key, not just a trailing comment; got "
        f"{step.get('args')!r}"
    )


def test_playbook_buy_prose_position_guidance_heading_bounded():
    playbook = _playbook_text()
    section = _heading_section(
        playbook, "## 1) Buy pipeline", "## 2) Sell (profit-taking) pipeline"
    )

    assert "position require `quick=false`" not in section.lower(), (
        "analyze_stock_batch never attaches a `position` field for any "
        "quick value (include_position is always forced False internally) "
        "— the playbook must not imply quick=False returns it"
    )
    assert "get_holdings" in section, (
        "the buy-pipeline prose must point position lookups at get_holdings"
    )


def test_playbook_sell_lane_upside_step_quick_false_heading_bounded():
    playbook = _playbook_text()
    section = _heading_section(
        playbook,
        "## 2) Sell (profit-taking) pipeline",
        "## 3) New-idea discovery pipeline",
    )

    assert "upside" in section.lower()
    assert "quick=false" in section.lower(), (
        "the sell pipeline's analyze_stock_batch step (confirm distance to "
        "resistance, RSI, upside) must explicitly say quick=False — upside "
        "is not part of the quick=True (default) allowlist"
    )


def test_mcp_readme_analyze_batch_section_does_not_contradict_snapshot_enrich_split():
    readme = _readme_text()

    assert "rows now expose consensus and rsi context directly" not in readme.lower(), (
        "screen_stocks_snapshot is DB-only (ROB-1309) and never returns "
        "consensus/RSI inline — the analyze_stock_batch section must not "
        "claim snapshot rows expose that context directly"
    )

    section = _heading_section(
        readme,
        "`analyze_stock_batch(symbols",
        "### Snapshot-backed report generation",
    )
    lowered = section.lower()

    assert "on that page's symbols" not in lowered, (
        "screen_stocks_enrich has no `symbols` parameter — it reruns the "
        "same preset/filter/pagination query, it does not take a page's "
        "symbol list as input; the README must not describe a call shape "
        "the tool doesn't support"
    )
    assert "screen_stocks_enrich" in section, (
        "the follow-up-after-snapshot guidance must point at "
        "screen_stocks_enrich, not imply the DB-only snapshot already "
        "carries consensus/RSI"
    )
    assert "same preset" in lowered, (
        "must describe the executable contract: screen_stocks_enrich takes "
        "the same preset/filter/pagination inputs as screen_stocks_snapshot"
    )
    assert "pagination" in lowered


def test_mcp_readme_screen_stocks_enrich_section_positively_owns_live_holdings_and_consensus():
    readme = _readme_text()
    section = _heading_section(
        readme,
        "- `screen_stocks_enrich(preset=None",
        "- `get_top_stocks(market=",
    )
    lowered = section.lower()
    assert "live kis holdings" in lowered
    assert "consensus" in lowered


def test_mcp_readme_snapshot_section_states_zero_http_heading_bounded():
    readme = _readme_text()
    section = _heading_section(
        readme,
        "- `screen_stocks_snapshot(preset=None",
        "- `screen_stocks_enrich(preset=None",
    )
    assert "zero external http" in section.lower()
    assert "kis-live portfolio" not in section.lower()


def test_mcp_readme_preserves_w2_portfolio_sellable_contract():
    readme = _readme_text()

    for phrase in (
        "Redis distributed singleflight (ROB-1310)",
        "portfolio_snapshot_unavailable",
        "need_sellable=false` paths skip Toss sellable reads entirely",
        'sellable_quantity_source="toss_broker_preflight"',
    ):
        assert phrase in readme, (
            f"W2's portfolio/sellable README contract must be preserved "
            f"verbatim — missing: {phrase!r}"
        )


def test_analysis_registration_include_position_has_no_effect_for_any_quick_value():
    import inspect

    from app.mcp_server.tooling import analysis_registration

    source = inspect.getsource(analysis_registration)
    start = source.index('name="analyze_stock_batch"')
    end = source.index("async def analyze_stock_batch", start)
    description = source[start:end].lower()

    assert "ignored by quick" not in description, (
        "must not imply quick=False attaches position — analyze_stock_batch "
        "never attaches a position field for any quick value"
    )
    assert "any quick value" in description, (
        "must positively state the no-effect claim covers ANY quick value, "
        "not just quick=True"
    )
    assert "never attaches a position field" in description, (
        "must positively state analyze_stock_batch never attaches a "
        "position field at all"
    )
    assert "get_holdings" in description, "must point position lookups at get_holdings"


def test_mcp_readme_include_position_note_has_no_effect_for_any_quick_value():
    readme = _readme_text()
    section = _heading_section(
        readme,
        "- `analyze_stock_batch(symbols",
        "### Snapshot-backed report generation",
    )
    lowered = section.lower()
    normalized = re.sub(r"[`\s]+", " ", lowered)

    assert "ignored in quick mode" not in lowered, (
        "must not imply quick=False attaches position — analyze_stock_batch "
        "never attaches a position field for any quick value"
    )
    assert "any quick value" in normalized, (
        "must positively state the no-effect claim covers ANY quick value, "
        "not just quick=True"
    )
    assert "never attaches a position field" in normalized, (
        "must positively state analyze_stock_batch never attaches a "
        "position field at all"
    )
    assert "get_holdings" in section
