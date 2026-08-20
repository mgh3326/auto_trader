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
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.review import (
    KISMockOrderLedger,
    TradeRetrospective,
    TradeRetrospectiveAction,
)
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
    assert not session._hit(" live_order_ledger"), (
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
