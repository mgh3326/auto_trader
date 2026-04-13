# Paper Trading 다중 전략 — 잔여 작업 구현 플랜

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 미커밋 코드 정리, `compare_strategies`/`recommend_go_live` MCP 도구 구현, MCP 등록, 전체 검증을 완료한다.

**Architecture:** `paper_journal_bridge.py` 모듈에 `compare_strategies`와 `recommend_go_live`를 구현한다. 기존 구현에서 journal create/close는 `order_journal.py`를 재사용하는 방식으로 이미 완료되었으므로, bridge 모듈에는 분석/추천 함수만 포함한다. `paper_journal_registration.py`에서 MCP 도구로 등록한다.

**Tech Stack:** Python 3.13, SQLAlchemy (async), FastMCP, pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-04-13-paper-strategy-journal-integration-design.md`

**선행 작업 상태:**
- Tasks 1-5, 9 커밋 완료 (모델, 마이그레이션, journal account_type, MCP description, strategy 필터, order→journal 연동)
- Task 6 코드 존재하나 미커밋 (`paper_account_registration.py`)
- Tasks 7-8은 `order_journal.py` 재사용으로 대체 (별도 bridge 불필요)
- Tasks 10-12 미구현 (compare_strategies, recommend_go_live, MCP 등록)
- Task 13 미실행 (전체 테스트/lint)

---

## 파일 구조

| 파일 | 유형 | 책임 |
|------|------|------|
| `app/mcp_server/tooling/paper_account_registration.py` | 미커밋 변경 | strategy_name 파라미터 노출 (이미 코드 존재) |
| `app/mcp_server/tooling/paper_journal_bridge.py` | 신규 | compare_strategies, recommend_go_live 구현 |
| `app/mcp_server/tooling/paper_journal_registration.py` | 신규 | MCP 도구 등록 |
| `app/mcp_server/tooling/registry.py` | 수정 | paper_journal 등록 추가 |
| `tests/test_paper_journal_bridge.py` | 신규 | bridge 단위 테스트 |

---

## Task 1: 미커밋 코드 커밋 (paper_account_registration strategy_name)

**Files:**
- Commit: `app/mcp_server/tooling/paper_account_registration.py` (이미 변경됨)

- [ ] **Step 1: 변경 내용 확인**

Run: `git diff app/mcp_server/tooling/paper_account_registration.py`

확인할 내용:
- `create_paper_account`에 `strategy_name: str | None = None` 파라미터 추가
- `service.create_account()`에 `strategy_name=strategy_name` 전달
- `list_paper_accounts`에 `strategy_name: str | None = None` 파라미터 추가
- `service.list_accounts()`에 `strategy_name=strategy_name` 전달
- MCP description에 strategy_name 설명 추가

- [ ] **Step 2: 관련 테스트 통과 확인**

Run: `uv run pytest tests/test_paper_account_tools.py -v`
Expected: 모두 PASS

- [ ] **Step 3: 커밋**

```bash
git add app/mcp_server/tooling/paper_account_registration.py
git commit -m "feat(paper): expose strategy_name in create/list paper account MCP tools"
```

---

## Task 2: compare_strategies 테스트 작성

**Files:**
- Create: `tests/test_paper_journal_bridge.py`

- [ ] **Step 1: 테스트 파일 생성**

`tests/test_paper_journal_bridge.py` 생성:

```python
"""Paper Journal Bridge unit tests — compare_strategies, recommend_go_live."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.timezone import now_kst
from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType


def _make_closed_journal(
    *,
    symbol: str,
    account: str,
    strategy: str | None,
    account_type: str = "paper",
    entry_price: float,
    pnl_pct: float,
    journal_id: int,
) -> TradeJournal:
    j = TradeJournal(
        symbol=symbol,
        instrument_type=InstrumentType.equity_kr,
        thesis="Test thesis",
        entry_price=Decimal(str(entry_price)),
        account_type=account_type,
        account=account,
        strategy=strategy,
        status="closed",
        pnl_pct=Decimal(str(pnl_pct)),
    )
    j.id = journal_id
    j.created_at = now_kst()
    j.updated_at = now_kst()
    j.exit_date = now_kst()
    j.exit_price = Decimal(str(entry_price * (1 + pnl_pct / 100)))
    return j


def _mock_bridge_session(monkeypatch, execute_side_effects: list):
    """Set up mock session factory for paper_journal_bridge."""
    from app.mcp_server.tooling import paper_journal_bridge

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=execute_side_effects)

    cm = AsyncMock()
    cm.__aenter__.return_value = mock_session
    cm.__aexit__.return_value = None
    factory = MagicMock(return_value=cm)
    monkeypatch.setattr(paper_journal_bridge, "_session_factory", lambda: factory)
    return mock_session


def _scalars_result(items: list) -> MagicMock:
    """Build a mock SQLAlchemy result with .scalars().all()."""
    scalars = MagicMock()
    scalars.all.return_value = items
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


def _scalar_one_result(value) -> MagicMock:
    """Build a mock SQLAlchemy result with .scalar_one()."""
    result = MagicMock()
    result.scalar_one.return_value = value
    return result


class TestCompareStrategies:
    """compare_strategies 단위 테스트."""

    @pytest.mark.asyncio
    async def test_closed_only_aggregation(self, monkeypatch):
        """closed journal 기준으로만 집계. active는 제외."""
        from app.mcp_server.tooling import paper_journal_bridge

        closed_win = _make_closed_journal(
            symbol="005930", account="paper-m", strategy="momentum",
            entry_price=72000, pnl_pct=5.0, journal_id=1,
        )
        closed_loss = _make_closed_journal(
            symbol="AAPL", account="paper-m", strategy="momentum",
            entry_price=150, pnl_pct=-3.0, journal_id=2,
        )
        # active journal — must be excluded from aggregation
        active_journal = TradeJournal(
            symbol="TSLA",
            instrument_type=InstrumentType.equity_us,
            thesis="Test",
            account_type="paper",
            account="paper-m",
            strategy="momentum",
            status="active",
        )
        active_journal.id = 3
        active_journal.created_at = now_kst()
        active_journal.updated_at = now_kst()

        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.name = "paper-m"
        mock_account.strategy_name = "momentum"

        _mock_bridge_session(monkeypatch, [
            _scalars_result([mock_account]),                          # accounts
            _scalars_result([closed_win, closed_loss, active_journal]),  # paper journals
            _scalars_result([]),                                      # live journals
        ])

        result = await paper_journal_bridge.compare_strategies(days=30)

        assert result["success"] is True
        assert len(result["strategies"]) == 1
        s = result["strategies"][0]
        assert s["total_trades"] == 2  # active excluded
        assert s["win_count"] == 1
        assert s["loss_count"] == 1
        assert s["win_rate"] == 50.0
        assert s["total_return_pct"] == 2.0  # 5.0 + (-3.0)
        assert s["best_trade"]["symbol"] == "005930"
        assert s["worst_trade"]["symbol"] == "AAPL"

    @pytest.mark.asyncio
    async def test_strategy_name_filter(self, monkeypatch):
        """strategy_name 필터가 TradeJournal.strategy 기준으로 적용."""
        from app.mcp_server.tooling import paper_journal_bridge

        # Only momentum journals returned (filtered by query)
        momentum_j = _make_closed_journal(
            symbol="005930", account="paper-m", strategy="momentum",
            entry_price=72000, pnl_pct=5.0, journal_id=1,
        )
        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.name = "paper-m"
        mock_account.strategy_name = "momentum"

        _mock_bridge_session(monkeypatch, [
            _scalars_result([mock_account]),
            _scalars_result([momentum_j]),
            _scalars_result([]),
        ])

        result = await paper_journal_bridge.compare_strategies(
            days=30, strategy_name="momentum"
        )
        assert result["success"] is True
        assert len(result["strategies"]) == 1
        assert result["strategies"][0]["strategy_name"] == "momentum"

    @pytest.mark.asyncio
    async def test_include_live_comparison_false(self, monkeypatch):
        """include_live_comparison=False → live_vs_paper 빈 배열."""
        from app.mcp_server.tooling import paper_journal_bridge

        _mock_bridge_session(monkeypatch, [
            _scalars_result([]),  # accounts
            _scalars_result([]),  # paper journals
            # no live query issued
        ])

        result = await paper_journal_bridge.compare_strategies(
            days=30, include_live_comparison=False
        )
        assert result["success"] is True
        assert result["live_vs_paper"] == []

    @pytest.mark.asyncio
    async def test_live_vs_paper_same_symbol(self, monkeypatch):
        """같은 종목 live/paper closed journal → 비교 결과 생성."""
        from app.mcp_server.tooling import paper_journal_bridge

        paper_j = _make_closed_journal(
            symbol="005930", account="paper-m", strategy="momentum",
            account_type="paper", entry_price=72000, pnl_pct=5.0, journal_id=1,
        )
        live_j = _make_closed_journal(
            symbol="005930", account="kis-main", strategy=None,
            account_type="live", entry_price=71000, pnl_pct=3.0, journal_id=2,
        )

        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.name = "paper-m"
        mock_account.strategy_name = "momentum"

        _mock_bridge_session(monkeypatch, [
            _scalars_result([mock_account]),
            _scalars_result([paper_j]),
            _scalars_result([live_j]),
        ])

        result = await paper_journal_bridge.compare_strategies(
            days=30, include_live_comparison=True
        )
        assert result["success"] is True
        assert len(result["live_vs_paper"]) == 1
        comp = result["live_vs_paper"][0]
        assert comp["symbol"] == "005930"
        assert comp["paper_pnl_pct"] == 5.0
        assert comp["live_pnl_pct"] == 3.0
        assert comp["delta_pnl_pct"] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_live_only_symbol_not_in_comparison(self, monkeypatch):
        """live만 있고 paper 없는 종목은 live_vs_paper에 미포함."""
        from app.mcp_server.tooling import paper_journal_bridge

        live_j = _make_closed_journal(
            symbol="TSLA", account="kis-main", strategy=None,
            account_type="live", entry_price=200, pnl_pct=10.0, journal_id=1,
        )

        _mock_bridge_session(monkeypatch, [
            _scalars_result([]),
            _scalars_result([]),
            _scalars_result([live_j]),
        ])

        result = await paper_journal_bridge.compare_strategies(days=30)
        assert result["live_vs_paper"] == []

    @pytest.mark.asyncio
    async def test_empty_strategies_no_error(self, monkeypatch):
        """paper journal 없으면 빈 strategies 반환."""
        from app.mcp_server.tooling import paper_journal_bridge

        _mock_bridge_session(monkeypatch, [
            _scalars_result([]),
            _scalars_result([]),
            _scalars_result([]),
        ])

        result = await paper_journal_bridge.compare_strategies(days=30)
        assert result["success"] is True
        assert result["strategies"] == []
        assert result["live_vs_paper"] == []
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py::TestCompareStrategies -v`
Expected: FAIL — `paper_journal_bridge` module not found

---

## Task 3: compare_strategies 구현

**Files:**
- Create: `app/mcp_server/tooling/paper_journal_bridge.py`

- [ ] **Step 1: paper_journal_bridge.py 생성**

```python
"""Paper Journal Bridge — 전략 비교 및 실전 전환 추천.

Paper 주문→journal 연동(create/close)은 order_journal.py가 담당한다.
이 모듈은 journal 데이터를 기반으로 한 분석/추천 기능만 책임진다.

집계 규칙 (고정):
- 모든 지표(win_rate, total_return_pct, avg_pnl_pct, best/worst_trade)는
  **closed** journal 기준으로만 계산 (realized performance).
- active journal은 집계에서 제외.
- total_return_pct는 closed journal들의 pnl_pct 합산 (realized 기준).
- 집계 단위는 paper account 기준. strategy_name은 필터/표시 역할.
- strategy_name 필터는 TradeJournal.strategy 기준 (PaperAccount.strategy_name 아님).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import desc, func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.models.paper_trading import PaperAccount
from app.models.trade_journal import JournalStatus, TradeJournal

logger = logging.getLogger(__name__)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


async def compare_strategies(
    days: int = 30,
    strategy_name: str | None = None,
    include_live_comparison: bool = True,
) -> dict[str, Any]:
    """Compare paper trading strategy performance over a given period.

    Shows per-account/per-strategy metrics such as win rate, realized return,
    and best/worst trade. All metrics are based on closed journals only.
    If include_live_comparison=True, also compares same-symbol live vs paper
    journal outcomes within the same period.
    """
    cutoff = now_kst() - timedelta(days=days)

    try:
        async with _session_factory()() as db:
            # 1. Paper accounts for metadata
            acct_stmt = select(PaperAccount).where(PaperAccount.is_active.is_(True))
            acct_result = await db.execute(acct_stmt)
            accounts = {a.name: a for a in acct_result.scalars().all()}

            # 2. Paper journals in period
            paper_filters: list = [
                TradeJournal.account_type == "paper",
                TradeJournal.created_at >= cutoff,
            ]
            if strategy_name is not None:
                paper_filters.append(TradeJournal.strategy == strategy_name)

            paper_stmt = (
                select(TradeJournal)
                .where(*paper_filters)
                .order_by(desc(TradeJournal.created_at))
            )
            paper_result = await db.execute(paper_stmt)
            paper_journals = list(paper_result.scalars().all())

            # 3. Aggregate by account (closed only)
            by_account: dict[str, list[TradeJournal]] = defaultdict(list)
            for j in paper_journals:
                if j.status == JournalStatus.closed and j.account:
                    by_account[j.account].append(j)

            strategies_out: list[dict[str, Any]] = []
            for account_name, journals in by_account.items():
                acct = accounts.get(account_name)
                total = len(journals)
                pnl_values = [
                    float(j.pnl_pct) for j in journals if j.pnl_pct is not None
                ]
                win_count = sum(1 for v in pnl_values if v > 0)
                loss_count = total - win_count
                total_return_pct = round(sum(pnl_values), 2) if pnl_values else 0.0
                avg_pnl_pct = (
                    round(total_return_pct / len(pnl_values), 2) if pnl_values else 0.0
                )
                win_rate = round(win_count / total * 100, 1) if total > 0 else 0.0

                best = max(journals, key=lambda j: float(j.pnl_pct or 0))
                worst = min(journals, key=lambda j: float(j.pnl_pct or 0))

                strategies_out.append({
                    "strategy_name": acct.strategy_name if acct else None,
                    "account_name": account_name,
                    "account_id": acct.id if acct else None,
                    "total_trades": total,
                    "win_count": win_count,
                    "loss_count": loss_count,
                    "win_rate": win_rate,
                    "total_return_pct": total_return_pct,
                    "avg_pnl_pct": avg_pnl_pct,
                    "best_trade": {
                        "symbol": best.symbol,
                        "pnl_pct": float(best.pnl_pct) if best.pnl_pct else 0.0,
                    },
                    "worst_trade": {
                        "symbol": worst.symbol,
                        "pnl_pct": float(worst.pnl_pct) if worst.pnl_pct else 0.0,
                    },
                })

            # 4. Live vs paper comparison (most recent closed per symbol)
            live_vs_paper: list[dict[str, Any]] = []
            if include_live_comparison:
                live_stmt = (
                    select(TradeJournal)
                    .where(
                        TradeJournal.account_type == "live",
                        TradeJournal.status == JournalStatus.closed,
                        TradeJournal.created_at >= cutoff,
                    )
                    .order_by(desc(TradeJournal.created_at))
                )
                live_result = await db.execute(live_stmt)
                live_journals = list(live_result.scalars().all())

                # Most recent closed per symbol
                live_by_symbol: dict[str, TradeJournal] = {}
                for j in live_journals:
                    if j.symbol not in live_by_symbol:
                        live_by_symbol[j.symbol] = j

                paper_closed = [
                    j for j in paper_journals if j.status == JournalStatus.closed
                ]
                paper_by_symbol: dict[str, TradeJournal] = {}
                for j in paper_closed:
                    if j.symbol not in paper_by_symbol:
                        paper_by_symbol[j.symbol] = j

                for sym in sorted(set(live_by_symbol) & set(paper_by_symbol)):
                    lj = live_by_symbol[sym]
                    pj = paper_by_symbol[sym]
                    l_pnl = float(lj.pnl_pct) if lj.pnl_pct is not None else 0.0
                    p_pnl = float(pj.pnl_pct) if pj.pnl_pct is not None else 0.0
                    live_vs_paper.append({
                        "symbol": sym,
                        "live_entry_price": (
                            float(lj.entry_price) if lj.entry_price else None
                        ),
                        "live_pnl_pct": l_pnl,
                        "paper_entry_price": (
                            float(pj.entry_price) if pj.entry_price else None
                        ),
                        "paper_pnl_pct": p_pnl,
                        "paper_strategy": pj.strategy,
                        "delta_pnl_pct": round(p_pnl - l_pnl, 4),
                    })

            return {
                "success": True,
                "period_days": days,
                "strategies": strategies_out,
                "live_vs_paper": live_vs_paper,
            }
    except Exception as exc:
        logger.exception("compare_strategies failed")
        return {"success": False, "error": f"compare_strategies failed: {exc}"}
```

- [ ] **Step 2: 테스트 통과 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py::TestCompareStrategies -v`
Expected: 6 tests PASS

- [ ] **Step 3: 커밋**

```bash
git add app/mcp_server/tooling/paper_journal_bridge.py tests/test_paper_journal_bridge.py
git commit -m "feat(paper): implement compare_strategies in paper_journal_bridge"
```

---

## Task 4: recommend_go_live 테스트 작성

**Files:**
- Modify: `tests/test_paper_journal_bridge.py`

- [ ] **Step 1: recommend_go_live 테스트 추가**

`tests/test_paper_journal_bridge.py`에 추가:

```python
def _make_simple_closed_journal(*, pnl_pct: float, journal_id: int) -> TradeJournal:
    j = TradeJournal(
        symbol=f"SYM{journal_id:03d}",
        instrument_type=InstrumentType.equity_kr,
        thesis="Test",
        entry_price=Decimal("10000"),
        account_type="paper",
        account="paper-test",
        status="closed",
        pnl_pct=Decimal(str(pnl_pct)),
    )
    j.id = journal_id
    j.created_at = now_kst()
    j.updated_at = now_kst()
    return j


class TestRecommendGoLive:
    """recommend_go_live 판정 테스트."""

    @pytest.mark.asyncio
    async def test_all_criteria_met_go_live(self, monkeypatch):
        """세 기준 모두 충족 → go_live."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_account = MagicMock()
        mock_account.name = "paper-test"
        mock_account.strategy_name = "momentum"

        # 25 trades: 16 wins (3.0%), 9 losses (-2.0%)
        # win_rate = 64%, total_return = 16*3 + 9*(-2) = 30.0
        journals = [
            _make_simple_closed_journal(pnl_pct=3.0, journal_id=i + 1)
            for i in range(16)
        ] + [
            _make_simple_closed_journal(pnl_pct=-2.0, journal_id=i + 17)
            for i in range(9)
        ]

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = mock_account
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [
            account_result,
            _scalars_result(journals),
            _scalar_one_result(2),  # active_positions count
        ])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test"
        )
        assert result["success"] is True
        assert result["recommendation"] == "go_live"
        assert result["all_passed"] is True
        assert result["criteria"]["min_trades"]["passed"] is True
        assert result["criteria"]["min_win_rate"]["passed"] is True
        assert result["criteria"]["min_return_pct"]["passed"] is True
        assert result["summary"]["total_trades"] == 25
        assert result["summary"]["active_positions"] == 2

    @pytest.mark.asyncio
    async def test_insufficient_trades_not_ready(self, monkeypatch):
        """거래 수 미달 → not_ready."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_account = MagicMock()
        mock_account.name = "paper-test"
        mock_account.strategy_name = "test"

        journals = [
            _make_simple_closed_journal(pnl_pct=5.0, journal_id=i + 1)
            for i in range(10)
        ]

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = mock_account
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [
            account_result,
            _scalars_result(journals),
            _scalar_one_result(0),
        ])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test"
        )
        assert result["recommendation"] == "not_ready"
        assert result["criteria"]["min_trades"]["passed"] is False
        assert result["criteria"]["min_trades"]["actual"] == 10
        assert result["criteria"]["min_trades"]["required"] == 20

    @pytest.mark.asyncio
    async def test_low_win_rate_not_ready(self, monkeypatch):
        """승률 미달 → not_ready."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_account = MagicMock()
        mock_account.name = "paper-test"
        mock_account.strategy_name = "test"

        # 20 trades, 5 wins → 25%
        journals = [
            _make_simple_closed_journal(pnl_pct=2.0, journal_id=i + 1)
            for i in range(5)
        ] + [
            _make_simple_closed_journal(pnl_pct=-1.0, journal_id=i + 6)
            for i in range(15)
        ]

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = mock_account
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [
            account_result,
            _scalars_result(journals),
            _scalar_one_result(0),
        ])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test"
        )
        assert result["recommendation"] == "not_ready"
        assert result["criteria"]["min_win_rate"]["passed"] is False

    @pytest.mark.asyncio
    async def test_negative_return_not_ready(self, monkeypatch):
        """수익률 미달 → not_ready."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_account = MagicMock()
        mock_account.name = "paper-test"
        mock_account.strategy_name = "test"

        # 20 trades, 11 wins at 1%, 9 losses at -3% → total = 11 - 27 = -16
        journals = [
            _make_simple_closed_journal(pnl_pct=1.0, journal_id=i + 1)
            for i in range(11)
        ] + [
            _make_simple_closed_journal(pnl_pct=-3.0, journal_id=i + 12)
            for i in range(9)
        ]

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = mock_account
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [
            account_result,
            _scalars_result(journals),
            _scalar_one_result(0),
        ])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test"
        )
        assert result["recommendation"] == "not_ready"
        assert result["criteria"]["min_return_pct"]["passed"] is False

    @pytest.mark.asyncio
    async def test_custom_thresholds(self, monkeypatch):
        """커스텀 기준값 override."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_account = MagicMock()
        mock_account.name = "paper-test"
        mock_account.strategy_name = "test"

        journals = [
            _make_simple_closed_journal(pnl_pct=1.0, journal_id=i + 1)
            for i in range(10)
        ]

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = mock_account
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [
            account_result,
            _scalars_result(journals),
            _scalar_one_result(0),
        ])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test",
            min_trades=5,
            min_win_rate=80.0,
            min_return_pct=5.0,
        )
        assert result["recommendation"] == "go_live"
        assert result["criteria"]["min_trades"]["required"] == 5

    @pytest.mark.asyncio
    async def test_account_not_found(self, monkeypatch):
        """존재하지 않는 account → 에러."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = None
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [account_result])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="nonexistent"
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_active_journals_excluded_from_metrics(self, monkeypatch):
        """active journal은 집계에서 제외 — closed만 계산."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_account = MagicMock()
        mock_account.name = "paper-test"
        mock_account.strategy_name = "test"

        # Query returns closed only (the function queries closed only)
        # but we verify the count is correct
        journals = [
            _make_simple_closed_journal(pnl_pct=5.0, journal_id=i + 1)
            for i in range(20)
        ]

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = mock_account
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [
            account_result,
            _scalars_result(journals),
            _scalar_one_result(5),  # 5 active positions
        ])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test"
        )
        assert result["summary"]["total_trades"] == 20
        assert result["summary"]["active_positions"] == 5
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py::TestRecommendGoLive -v`
Expected: FAIL — `recommend_go_live` not defined

---

## Task 5: recommend_go_live 구현

**Files:**
- Modify: `app/mcp_server/tooling/paper_journal_bridge.py`

- [ ] **Step 1: recommend_go_live 함수 추가**

`app/mcp_server/tooling/paper_journal_bridge.py`에 추가:

```python
async def recommend_go_live(
    account_name: str,
    min_trades: int = 20,
    min_win_rate: float = 50.0,
    min_return_pct: float = 0.0,
) -> dict[str, Any]:
    """Evaluate whether a paper trading account meets go-live criteria.

    All metrics are based on **closed** journals only (realized performance).
    Active positions are shown in summary for reference but excluded from judgment.
    """
    try:
        async with _session_factory()() as db:
            # 1. Account lookup
            acct_stmt = select(PaperAccount).where(
                PaperAccount.name == account_name
            )
            acct_result = await db.execute(acct_stmt)
            account = acct_result.scalars().one_or_none()
            if account is None:
                return {
                    "success": False,
                    "error": f"Paper account '{account_name}' not found",
                }

            # 2. Closed journals
            closed_stmt = (
                select(TradeJournal)
                .where(
                    TradeJournal.account_type == "paper",
                    TradeJournal.account == account_name,
                    TradeJournal.status == JournalStatus.closed,
                )
                .order_by(desc(TradeJournal.created_at))
            )
            closed_result = await db.execute(closed_stmt)
            closed_journals = list(closed_result.scalars().all())

            # 3. Active positions count (reference only)
            active_stmt = (
                select(sa_func.count())
                .select_from(TradeJournal)
                .where(
                    TradeJournal.account_type == "paper",
                    TradeJournal.account == account_name,
                    TradeJournal.status == JournalStatus.active,
                )
            )
            active_result = await db.execute(active_stmt)
            active_positions = active_result.scalar_one()

            # 4. Calculate metrics
            total_trades = len(closed_journals)
            pnl_values = [
                float(j.pnl_pct)
                for j in closed_journals
                if j.pnl_pct is not None
            ]
            win_count = sum(1 for v in pnl_values if v > 0)
            loss_count = total_trades - win_count
            total_return_pct = round(sum(pnl_values), 2) if pnl_values else 0.0
            win_rate = (
                round(win_count / total_trades * 100, 1) if total_trades > 0 else 0.0
            )
            avg_pnl_pct = (
                round(total_return_pct / len(pnl_values), 2) if pnl_values else 0.0
            )

            best_trade = None
            worst_trade = None
            if closed_journals:
                best = max(closed_journals, key=lambda j: float(j.pnl_pct or 0))
                worst = min(closed_journals, key=lambda j: float(j.pnl_pct or 0))
                best_trade = {
                    "symbol": best.symbol,
                    "pnl_pct": float(best.pnl_pct) if best.pnl_pct else 0.0,
                }
                worst_trade = {
                    "symbol": worst.symbol,
                    "pnl_pct": float(worst.pnl_pct) if worst.pnl_pct else 0.0,
                }

            # 5. Criteria check
            trades_passed = total_trades >= min_trades
            wr_passed = win_rate >= min_win_rate
            return_passed = total_return_pct >= min_return_pct
            all_passed = trades_passed and wr_passed and return_passed

            return {
                "success": True,
                "account_name": account_name,
                "strategy_name": account.strategy_name,
                "recommendation": "go_live" if all_passed else "not_ready",
                "criteria": {
                    "min_trades": {
                        "required": min_trades,
                        "actual": total_trades,
                        "passed": trades_passed,
                    },
                    "min_win_rate": {
                        "required": min_win_rate,
                        "actual": win_rate,
                        "passed": wr_passed,
                    },
                    "min_return_pct": {
                        "required": min_return_pct,
                        "actual": total_return_pct,
                        "passed": return_passed,
                    },
                },
                "all_passed": all_passed,
                "summary": {
                    "total_trades": total_trades,
                    "win_count": win_count,
                    "loss_count": loss_count,
                    "win_rate": win_rate,
                    "total_return_pct": total_return_pct,
                    "avg_pnl_pct": avg_pnl_pct,
                    "best_trade": best_trade,
                    "worst_trade": worst_trade,
                    "active_positions": active_positions,
                },
            }
    except Exception as exc:
        logger.exception("recommend_go_live failed")
        return {"success": False, "error": f"recommend_go_live failed: {exc}"}
```

- [ ] **Step 2: 전체 bridge 테스트 통과 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py -v`
Expected: 모두 PASS (TestCompareStrategies 6건 + TestRecommendGoLive 7건)

- [ ] **Step 3: 커밋**

```bash
git add app/mcp_server/tooling/paper_journal_bridge.py tests/test_paper_journal_bridge.py
git commit -m "feat(paper): implement recommend_go_live in paper_journal_bridge"
```

---

## Task 6: MCP 등록 — compare_strategies, recommend_go_live

**Files:**
- Create: `app/mcp_server/tooling/paper_journal_registration.py`
- Modify: `app/mcp_server/tooling/registry.py`
- Modify: `tests/test_paper_journal_bridge.py`

- [ ] **Step 1: registration 테스트 작성**

`tests/test_paper_journal_bridge.py`에 추가:

```python
class TestPaperJournalRegistration:
    """MCP 도구 등록 확인."""

    def test_tool_names_defined(self):
        from app.mcp_server.tooling.paper_journal_registration import (
            PAPER_JOURNAL_TOOL_NAMES,
        )

        assert "compare_strategies" in PAPER_JOURNAL_TOOL_NAMES
        assert "recommend_go_live" in PAPER_JOURNAL_TOOL_NAMES

    def test_register_does_not_raise(self):
        from app.mcp_server.tooling.paper_journal_registration import (
            register_paper_journal_tools,
        )

        mock_mcp = MagicMock()
        mock_mcp.tool.return_value = lambda fn: fn
        register_paper_journal_tools(mock_mcp)
        assert mock_mcp.tool.call_count == 2
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py::TestPaperJournalRegistration -v`
Expected: FAIL — module not found

- [ ] **Step 3: paper_journal_registration.py 생성**

`app/mcp_server/tooling/paper_journal_registration.py` 생성:

```python
"""Paper Journal MCP tool registration — compare_strategies, recommend_go_live."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.paper_journal_bridge import (
    compare_strategies,
    recommend_go_live,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

PAPER_JOURNAL_TOOL_NAMES: set[str] = {
    "compare_strategies",
    "recommend_go_live",
}


def register_paper_journal_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="compare_strategies",
        description=(
            "Compare paper trading strategy performance over a given period. "
            "Shows per-account/per-strategy metrics such as win rate, realized return, "
            "and best/worst trade. All metrics are based on closed journals only. "
            "If include_live_comparison=True, also compares same-symbol live vs paper "
            "journal outcomes within the same period."
        ),
    )(compare_strategies)

    _ = mcp.tool(
        name="recommend_go_live",
        description=(
            "Evaluate whether a paper trading account meets criteria for live trading. "
            "Checks total trades, win rate, and realized return against thresholds "
            "(default: 20 trades, 50% win rate, positive return). "
            "All metrics are based on closed journals only."
        ),
    )(recommend_go_live)


__all__ = ["PAPER_JOURNAL_TOOL_NAMES", "register_paper_journal_tools"]
```

- [ ] **Step 4: registry.py에 등록 추가**

`app/mcp_server/tooling/registry.py` 수정:

import 추가 (paper_account_registration import 아래):
```python
from app.mcp_server.tooling.paper_journal_registration import (
    register_paper_journal_tools,
)
```

`register_all_tools` 함수 마지막에 추가:
```python
    register_paper_journal_tools(mcp)
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `uv run pytest tests/test_paper_journal_bridge.py::TestPaperJournalRegistration -v`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add app/mcp_server/tooling/paper_journal_registration.py app/mcp_server/tooling/registry.py tests/test_paper_journal_bridge.py
git commit -m "feat(paper): register compare_strategies and recommend_go_live MCP tools"
```

---

## Task 7: 전체 테스트 스위트 + lint

- [ ] **Step 1: 신규 + 기존 관련 테스트 전체 실행**

Run: `uv run pytest tests/test_paper_journal_bridge.py tests/test_mcp_trade_journal.py tests/test_paper_trading_service.py tests/test_paper_account_tools.py tests/test_paper_order_handler.py tests/test_trade_journal_model.py -v`
Expected: 모두 PASS (기존 106건 + 신규 ~15건)

- [ ] **Step 2: lint 실행**

Run: `make lint`
Expected: 오류 없음

- [ ] **Step 3: format 실행**

Run: `make format`

- [ ] **Step 4: lint 재확인**

Run: `make lint`
Expected: 오류 없음

- [ ] **Step 5: 포맷 변경사항 있으면 커밋**

```bash
git add -A
git commit -m "style: fix formatting"
```

(변경사항 없으면 생략)
