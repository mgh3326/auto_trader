# Binance Demo 스캘핑 LLM 결정 주입 (Phase 3, D-PR1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** out-of-process LLM(MCP 세션)이 내린 결정을 `binance_demo_scalping_submit_decision` MCP 도구로 데모 스캘핑에 주입(monitored 라운드트립, `session_tag="llm"` + 근거 `signal_snapshot` 기록, dry_run+confirm).

**Architecture:** ① executor에 `session_tag`+`signal_snapshot` 배선(→ 이미 인자 보유한 `analytics.record`) ② MCP 도구가 기존 `DemoScalpingExecutor.execute_monitored`를 그대로 호출(모든 데모 안전경계 상속). LLM은 도구 호출자일 뿐 도구 내 LLM 호출 없음(런타임 경계 준수).

**Tech Stack:** Python 3.13, FastMCP, SQLAlchemy(async), DemoScalpingExecutor/futures_demo, pytest-asyncio, Decimal.

> **스펙 정제(§3.2 기록처):** 스펙은 결정/근거를 `strategy_events`에 기록한다 했으나, 추출 결과 `TradingDecisionStrategyEvent`는 제약된 source/event_type enum + `user_id`/`session_id`가 필요해 스캘프 결정에 부적합. 대신 **`scalp_trade_analytics.signal_snapshot`(JSONB)**에 기록 — 이미 컬럼과 `record()` 인자가 존재하고, LLM 근거가 그 트레이드의 분석 행에 co-locate되어 더 단순·적합. `session_tag`와 동일 배선으로 함께 전달.

## Global Constraints

- Python 3.13+. 변경은 worktree `feature/binance-demo-scalping-llm-decision`에서. canonical repo는 main 고정.
- **LLM 호출 없음**: 도구는 LLM이 제출한 결정을 결정론적으로 실행만(런타임 in-process LLM import 금지 경계 준수).
- **데모 전용 안전경계 상속**: `BinanceFuturesDemoExecutionClient.from_env()` + `DemoScalpingExecutor` 재사용 → demo-fapi only / 1x / 심볼 allowlist(XRP/DOGE/SOL) / notional cap 10 / Phase1 손실게이트. 새 주문 mutation 경로 없음.
- **이중 게이트**: dry_run 기본 True + `confirm=True` 필수(실 데모 주문). 도구 등록은 `settings.binance_demo_scalping_enabled`(default False) 게이트(kiwoom_mock 패턴).
- **무회귀**: executor `session_tag`/`signal_snapshot` 기본 `None` → 기존 호출자(스케줄러 규칙기반)는 NULL 유지.
- **ROB-285 audit**: 새 binance-참조 파일은 `tests/services/brokers/binance/test_audit_no_signed_endpoints.py` ALLOWED_LEGACY_FILES에 등재. 추가 후 audit 테스트로 검증해 새로 플래그되는 파일(핸들러 모듈, 필요시 registry.py)도 등재.
- **세션 검증 범위**: binance 관련 변경이므로 `tests/services/brokers/binance/` 전체 + `tests/mcp_server/`(또는 도구 테스트) 실행(Phase 2 교훈: audit 테스트는 한 단계 위 디렉토리).
- 마이그레이션 없음(session_tag/signal_snapshot 컬럼은 ROB-313 기존). D-PR2(리뷰 비교 surfacing)는 후속.
- 커밋 trailer:
  ```
  Co-Authored-By: Paperclip <noreply@paperclip.ing>
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
  ```

## File Structure

- `app/services/brokers/binance/demo_scalping_exec/executor.py` (수정) — `execute`/`execute_monitored`/`_finalize_analytics`/`_record_partial_analytics`에 `session_tag`+`signal_snapshot` 배선.
- `app/core/config.py` (수정) — `binance_demo_scalping_enabled: bool = False`.
- `app/mcp_server/tooling/binance_demo_scalping_handler.py` (생성) — 핸들러 + `register_binance_demo_scalping_tools(mcp)`.
- `app/mcp_server/tooling/registry.py` (수정) — DEFAULT 프로필에서 게이트 조건부 등록.
- `tests/services/brokers/binance/test_audit_no_signed_endpoints.py` (수정) — allowlist 등재.
- `docs/runbooks/binance-demo-scalping-llm-session.md` (생성) — 운영 런북.
- Test: `tests/services/brokers/binance/demo_scalping_exec/test_executor_session_tag.py` (생성), `tests/mcp_server/test_binance_demo_scalping_submit_decision.py` (생성).

---

### Task 1: executor `session_tag` + `signal_snapshot` 배선

**Files:**
- Modify: `app/services/brokers/binance/demo_scalping_exec/executor.py`
- Test: `tests/services/brokers/binance/demo_scalping_exec/test_executor_session_tag.py` (생성)

**Interfaces:**
- Consumes: `ScalpTradeAnalyticsService.record(*, ..., session_tag: str | None = None, signal_snapshot: dict | None = None)` (이미 존재).
- Produces: `execute_monitored(*, ..., session_tag: str | None = None, signal_snapshot: dict[str, Any] | None = None)` 및 `execute(*, ..., session_tag=None, signal_snapshot=None)` — Task 2가 호출.

- [ ] **Step 1: 실패 테스트 작성**

`tests/services/brokers/binance/demo_scalping_exec/test_executor_session_tag.py` 생성. **`tests/services/brokers/binance/demo_scalping_exec/test_executor_analytics.py`의 30-146행(`_NOW`,`_REF`,`_limits`,`_intent`,`_Ref`,`_MD`,`_Sub`,`_OO`,`_Pos`,`_FakeFutures`)을 복사**한 뒤:

```python
from app.services.brokers.binance.demo_scalping_exec.analytics import (
    ScalpTradeAnalyticsService,
)


@pytest.mark.asyncio
async def test_session_tag_and_signal_snapshot_recorded(db_session) -> None:
    client = _FakeFutures(open_px="100", close_px="100.40")
    md = _MD([100.0, 100.4])
    ex = DemoScalpingExecutor(
        product="usdm_futures", client=client, session=db_session,
        reference=_Ref(), now=_NOW, limits=_limits("LLMTAGUSDT"),
        market_data=md, poll_delay_seconds=0.0,
    )
    snap = {"source": "llm", "rationale": "funding flip + oversold"}
    result = await ex.execute_monitored(
        _intent("LLMTAGUSDT"), confirm=True, max_poll_count=5,
        session_tag="llm", signal_snapshot=snap,
    )
    assert result.status == "reconciled"
    row = await ScalpTradeAnalyticsService(db_session).get_by_open_client_order_id(
        result.open_client_order_id
    )
    assert row is not None
    assert row.session_tag == "llm"
    assert row.signal_snapshot == snap


@pytest.mark.asyncio
async def test_session_tag_defaults_none_no_regression(db_session) -> None:
    client = _FakeFutures(open_px="100", close_px="100.40")
    md = _MD([100.0, 100.4])
    ex = DemoScalpingExecutor(
        product="usdm_futures", client=client, session=db_session,
        reference=_Ref(), now=_NOW, limits=_limits("NOTAGUSDT"),
        market_data=md, poll_delay_seconds=0.0,
    )
    result = await ex.execute_monitored(_intent("NOTAGUSDT"), confirm=True, max_poll_count=5)
    row = await ScalpTradeAnalyticsService(db_session).get_by_open_client_order_id(
        result.open_client_order_id
    )
    assert row is not None
    assert row.session_tag is None
    assert row.signal_snapshot is None
```

- [ ] **Step 2: 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.phase3-llm-decision && uv run pytest tests/services/brokers/binance/demo_scalping_exec/test_executor_session_tag.py -v`
Expected: FAIL — `execute_monitored() got an unexpected keyword argument 'session_tag'`.

- [ ] **Step 3: `execute_monitored` / `execute` 시그니처에 파라미터 추가**

`executor.py`의 `execute` 시그니처(현재 `async def execute(self, intent, *, confirm=False, market=None)`)를 교체:

```python
    async def execute(
        self,
        intent: OrderIntent,
        *,
        confirm: bool = False,
        market: MarketConditions | None = None,
        session_tag: str | None = None,
        signal_snapshot: dict[str, Any] | None = None,
    ) -> ExecutionResult:
```

`execute_monitored` 시그니처에 동일 2개 파라미터 추가(기존 파라미터 뒤, `max_runtime_s: float = 300.0,` 다음):

```python
        session_tag: str | None = None,
        signal_snapshot: dict[str, Any] | None = None,
```

- [ ] **Step 4: `_finalize_analytics` 호출 2곳에 전달**

`execute` 내 `_finalize_analytics` 호출(현재 `await self._finalize_analytics(intent, ref, qty, notional, instrument_id, result, telemetry)`)과 `execute_monitored` 내 동일 호출을 둘 다 교체:

```python
        await self._finalize_analytics(
            intent, ref, qty, notional, instrument_id, result, telemetry,
            session_tag=session_tag, signal_snapshot=signal_snapshot,
        )
```

- [ ] **Step 5: `_finalize_analytics` / `_record_partial_analytics` 시그니처 + record 호출에 전달**

`_finalize_analytics` 시그니처(현재 끝 `telemetry: _RunTelemetry | None = None,`)에 추가:

```python
        session_tag: str | None = None,
        signal_snapshot: dict[str, Any] | None = None,
```

`_finalize_analytics` 내 `self._record_partial_analytics(intent, qty, instrument_id, result, tele)` 호출을 교체:

```python
            await self._record_partial_analytics(
                intent, qty, instrument_id, result, tele,
                session_tag=session_tag, signal_snapshot=signal_snapshot,
            )
```

`_finalize_analytics` 내 `self.analytics.record(...)` 호출의 `now=self.now,` **직전에** 추가:

```python
                    session_tag=session_tag,
                    signal_snapshot=signal_snapshot,
```

`_record_partial_analytics` 시그니처(현재 끝 `tele: _RunTelemetry,`)에 추가:

```python
        session_tag: str | None = None,
        signal_snapshot: dict[str, Any] | None = None,
```

`_record_partial_analytics` 내 `self.analytics.record(...)` 호출의 `now=self.now,` **직전에** 추가:

```python
                session_tag=session_tag,
                signal_snapshot=signal_snapshot,
```

`Any`가 executor.py에 import돼 있는지 확인(파일 상단에 `from typing import Any` — 이미 `ExecutionResult.to_evidence_dict`가 `dict[str, Any]`를 쓰므로 존재).

- [ ] **Step 6: 통과 + 회귀**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_exec/test_executor_session_tag.py tests/services/brokers/binance/demo_scalping_exec/test_executor_analytics.py tests/services/brokers/binance/demo_scalping_exec/test_executor_realized_pnl.py -v`
Expected: PASS (신규 2 + 기존 analytics/realized_pnl 회귀 green — 기본 None이라 무변경).

- [ ] **Step 7: 커밋**

```bash
git add app/services/brokers/binance/demo_scalping_exec/executor.py tests/services/brokers/binance/demo_scalping_exec/test_executor_session_tag.py
git commit -F - <<'EOF'
feat: thread session_tag + signal_snapshot through demo scalping executor

execute/execute_monitored가 session_tag·signal_snapshot을 받아
_finalize_analytics/_record_partial_analytics → analytics.record로 전달
(record는 이미 인자 보유). 기본 None이라 무회귀. LLM 트레이드 태깅·근거기록 토대.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
EOF
```

---

### Task 2: `binance_demo_scalping_submit_decision` MCP 도구 + 등록 + 런북

**Files:**
- Modify: `app/core/config.py` (`binance_demo_scalping_enabled` flag)
- Create: `app/mcp_server/tooling/binance_demo_scalping_handler.py`
- Modify: `app/mcp_server/tooling/registry.py` (DEFAULT 프로필 조건부 등록)
- Modify: `tests/services/brokers/binance/test_audit_no_signed_endpoints.py` (allowlist)
- Create: `docs/runbooks/binance-demo-scalping-llm-session.md`
- Test: `tests/mcp_server/test_binance_demo_scalping_submit_decision.py` (생성)

**Interfaces:**
- Consumes: Task 1의 `execute_monitored(..., session_tag, signal_snapshot)`; `build_manual_intent`(scripts) 동등 로직; `ScalpingRiskLimits`; `BinanceFuturesDemoExecutionClient.from_env`/`DemoReferenceData`/`DemoScalpingMarketData`/`DemoScalpingExecutor`; `AsyncSessionLocal`; `settings.binance_demo_scalping_enabled`.
- Produces: MCP 도구 `binance_demo_scalping_submit_decision`.

- [ ] **Step 1: 실패 테스트 작성**

`tests/mcp_server/test_binance_demo_scalping_submit_decision.py` 생성:

```python
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

import app.mcp_server.tooling.binance_demo_scalping_handler as mod


@pytest.mark.asyncio
async def test_dry_run_returns_plan_no_order() -> None:
    result = await mod.binance_demo_scalping_submit_decision(
        symbol="XRPUSDT", side="BUY", rationale="funding flip", dry_run=True
    )
    assert result["status"] == "planned"
    assert result["dry_run"] is True
    assert result["symbol"] == "XRPUSDT"
    assert result["side"] == "BUY"
    assert result["session_tag"] == "llm"
    assert "rationale" in result


@pytest.mark.asyncio
async def test_rejects_non_allowlisted_symbol() -> None:
    result = await mod.binance_demo_scalping_submit_decision(
        symbol="BTCUSDT", side="BUY", rationale="x", dry_run=True
    )
    assert result["status"] == "rejected"
    assert "symbol" in result["error"].lower()


@pytest.mark.asyncio
async def test_rejects_empty_rationale() -> None:
    result = await mod.binance_demo_scalping_submit_decision(
        symbol="XRPUSDT", side="BUY", rationale="  ", dry_run=True
    )
    assert result["status"] == "rejected"
    assert "rationale" in result["error"].lower()


@pytest.mark.asyncio
async def test_confirm_executes_monitored_with_llm_tag() -> None:
    fake_result = type(
        "R", (), {
            "status": "reconciled",
            "open_client_order_id": "rob307-x",
            "close_client_order_id": "rob307-y",
            "exit_reason": "take_profit",
            "to_evidence_dict": lambda self: {"status": "reconciled"},
        },
    )()
    captured: dict = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return fake_result

    with patch.object(mod, "_execute_confirmed_round_trip", AsyncMock(side_effect=fake_run)):
        result = await mod.binance_demo_scalping_submit_decision(
            symbol="SOLUSDT", side="SELL", rationale="OI surge fade",
            dry_run=False, confirm=True,
        )
    assert result["status"] == "reconciled"
    assert captured["session_tag"] == "llm"
    assert captured["signal_snapshot"]["rationale"] == "OI surge fade"
    assert captured["signal_snapshot"]["source"] == "llm"
    assert captured["symbol"] == "SOLUSDT"
    assert captured["side"] == "SELL"


@pytest.mark.asyncio
async def test_confirm_required_for_real_order() -> None:
    # dry_run False but confirm False → still a plan, no execution.
    result = await mod.binance_demo_scalping_submit_decision(
        symbol="XRPUSDT", side="BUY", rationale="x", dry_run=False, confirm=False
    )
    assert result["status"] == "planned"
    assert result["dry_run"] is True
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/test_binance_demo_scalping_submit_decision.py -v`
Expected: FAIL — `ModuleNotFoundError: app.mcp_server.tooling.binance_demo_scalping_handler`.

- [ ] **Step 3: config flag 추가**

`app/core/config.py`의 `binance_demo_scalping_review_flow_enabled: bool = False`(Phase 2에서 추가됨) **바로 다음**에:

```python
    # Phase 3 — gate for the LLM decision-injection MCP tool (default-off).
    binance_demo_scalping_enabled: bool = False
```

- [ ] **Step 4: 핸들러 모듈 구현**

`app/mcp_server/tooling/binance_demo_scalping_handler.py` 생성:

```python
"""Phase 3 — LLM decision-injection MCP tool for Binance Demo scalping.

The out-of-process LLM (an MCP session) reads market data + recent scalping
reviews, decides, and calls ``binance_demo_scalping_submit_decision`` with its
decision + rationale. The tool is deterministic: it executes the LLM's decision
via the existing ``DemoScalpingExecutor.execute_monitored`` (one round-trip),
tagging the trade ``session_tag="llm"`` and recording the rationale in
``signal_snapshot``. NO LLM call here — judgment belongs to the MCP caller
(runtime in-process LLM boundary). Demo-only; dry_run default + confirm gate.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any, Literal

from app.services.brokers.binance.demo_scalping.contract import ScalpingRiskLimits
from app.services.brokers.binance.demo_scalping.order_intent import OrderIntent

logger = logging.getLogger(__name__)

_ALLOWLIST = frozenset({"XRPUSDT", "DOGEUSDT", "SOLUSDT"})


def _build_intent(
    *, symbol: str, side: str, notional_usdt: Decimal, now: dt.datetime
) -> OrderIntent:
    now_ms = int(now.timestamp() * 1000)
    return OrderIntent(
        product="usdm_futures",
        symbol=symbol,
        side=side,
        order_type="MARKET",
        target_notional_usdt=notional_usdt,
        entry_reference_price=None,
        tp_price=None,
        sl_price=None,
        confidence=Decimal("0"),
        reason_codes=("llm_decision",),
        source_candle_close_time_ms=now_ms,
        evaluated_at_ms=now_ms,
    )


async def _execute_confirmed_round_trip(
    *,
    symbol: str,
    side: str,
    tp_bps: Decimal,
    sl_bps: Decimal,
    notional_usdt: Decimal,
    session_tag: str,
    signal_snapshot: dict[str, Any],
    now: dt.datetime,
) -> Any:
    """Construct the demo executor and run one monitored round-trip. Real Demo
    order — only reached on confirm=True. Mirrors scripts/binance_demo_scalping_execute."""
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.demo_scalping.market_data import (
        DemoScalpingMarketData,
    )
    from app.services.brokers.binance.demo_scalping_exec.executor import (
        DemoScalpingExecutor,
    )
    from app.services.brokers.binance.demo_scalping_exec.reference import (
        DemoReferenceData,
    )
    from app.services.brokers.binance.futures_demo.execution_client import (
        BinanceFuturesDemoExecutionClient,
    )

    limits = ScalpingRiskLimits()
    client = BinanceFuturesDemoExecutionClient.from_env()
    reference = DemoReferenceData()
    market_data = DemoScalpingMarketData()
    try:
        async with AsyncSessionLocal() as session:
            executor = DemoScalpingExecutor(
                product="usdm_futures",
                client=client,
                session=session,
                reference=reference,
                now=now,
                limits=limits,
                market_data=market_data,
            )
            intent = _build_intent(
                symbol=symbol, side=side, notional_usdt=notional_usdt, now=now
            )
            result = await executor.execute_monitored(
                intent,
                confirm=True,
                tp_bps=tp_bps,
                sl_bps=sl_bps,
                session_tag=session_tag,
                signal_snapshot=signal_snapshot,
            )
            await session.commit()
            return result
    finally:
        await reference.aclose()
        await market_data.aclose()
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()


async def binance_demo_scalping_submit_decision(
    symbol: str,
    side: Literal["BUY", "SELL"],
    rationale: str,
    tp_bps: float = 30.0,
    sl_bps: float = 20.0,
    notional_usdt: float = 10.0,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Submit an LLM-decided Binance Demo (USD-M futures) scalping round-trip.

    DEMO ONLY. ``dry_run`` (default) returns the plan with no order. A real Demo
    order requires ``dry_run=False`` AND ``confirm=True``. The trade is tagged
    ``session_tag="llm"`` and the rationale is recorded in ``signal_snapshot`` so
    the daily review can compare LLM vs the rule-based baseline. Symbols limited
    to XRPUSDT/DOGEUSDT/SOLUSDT; 1x; notional capped at 10 USDT by the executor."""
    sym = symbol.upper().strip()
    if sym not in _ALLOWLIST:
        return {
            "status": "rejected",
            "error": f"symbol {sym!r} not allowlisted (allowed: {sorted(_ALLOWLIST)})",
        }
    if side not in ("BUY", "SELL"):
        return {"status": "rejected", "error": f"side must be BUY|SELL, got {side!r}"}
    if not rationale or not rationale.strip():
        return {"status": "rejected", "error": "rationale must be a non-empty string"}

    notional = Decimal(str(notional_usdt))
    signal_snapshot = {
        "source": "llm",
        "rationale": rationale.strip(),
        "requested_side": side,
        "tp_bps": str(tp_bps),
        "sl_bps": str(sl_bps),
    }

    if dry_run or not confirm:
        return {
            "status": "planned",
            "dry_run": True,
            "symbol": sym,
            "side": side,
            "rationale": rationale.strip(),
            "session_tag": "llm",
            "notional_usdt": str(notional),
            "tp_bps": str(tp_bps),
            "sl_bps": str(sl_bps),
            "note": "set dry_run=false AND confirm=true to place the real Demo order",
        }

    now = dt.datetime.now(dt.UTC)
    try:
        result = await _execute_confirmed_round_trip(
            symbol=sym,
            side=side,
            tp_bps=Decimal(str(tp_bps)),
            sl_bps=Decimal(str(sl_bps)),
            notional_usdt=notional,
            session_tag="llm",
            signal_snapshot=signal_snapshot,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — surface broker/setup errors as data
        logger.exception("binance demo scalping submit_decision failed")
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    evidence = result.to_evidence_dict()
    return {
        "status": result.status,
        "dry_run": False,
        "symbol": sym,
        "side": side,
        "rationale": rationale.strip(),
        "session_tag": "llm",
        "open_client_order_id": result.open_client_order_id,
        "close_client_order_id": result.close_client_order_id,
        "exit_reason": result.exit_reason,
        "evidence": evidence,
    }


def register_binance_demo_scalping_tools(mcp: Any) -> None:
    mcp.tool(
        name="binance_demo_scalping_submit_decision",
        description=(
            "Submit an LLM-decided Binance Demo (USD-M futures) scalping "
            "round-trip. DEMO ONLY, dry_run default; real order needs "
            "dry_run=false + confirm=true. Tags the trade session_tag='llm' and "
            "records the rationale for LLM-vs-baseline comparison. Symbols: "
            "XRPUSDT/DOGEUSDT/SOLUSDT; 1x; <=10 USDT."
        ),
    )(binance_demo_scalping_submit_decision)
```

- [ ] **Step 5: registry.py 조건부 등록**

`app/mcp_server/tooling/registry.py`의 `register_all_tools` 내 DEFAULT 프로필 블록에서 `if settings.kiwoom_mock_enabled:` 등록 줄 **다음에** 추가:

```python
        if settings.binance_demo_scalping_enabled:
            from app.mcp_server.tooling.binance_demo_scalping_handler import (
                register_binance_demo_scalping_tools,
            )

            register_binance_demo_scalping_tools(mcp)
```

(import을 함수 내부 lazy로 두어 게이트 off 시 모듈 로드 비용 0.)

- [ ] **Step 6: ROB-285 audit allowlist 등재**

`tests/services/brokers/binance/test_audit_no_signed_endpoints.py`의 ALLOWED_LEGACY_FILES에서 Phase 2 블록(`"app/core/config.py",` 등) **다음에** 추가:

```python
        # Phase 3 — LLM decision-injection MCP tool. Deterministic executor of an
        # LLM-submitted decision (no signed HTTP itself; reuses futures_demo via
        # execute_monitored). References "Binance" via imports + tool name only.
        "app/mcp_server/tooling/binance_demo_scalping_handler.py",
```

- [ ] **Step 7: 런북 작성**

`docs/runbooks/binance-demo-scalping-llm-session.md` 생성:

```markdown
# Binance Demo 스캘핑 — 매일 LLM 결정 세션 (Phase 3, 반자동)

매일 사람이 MCP 세션을 트리거해 LLM이 데모 스캘핑 결정을 주입한다.

## 전제
- `BINANCE_DEMO_SCALPING_ENABLED=true` (MCP 도구 등록 게이트) + futures demo 자격증명.
- 데모 전용. 실 주문은 `confirm=true`에서만.

## 절차 (MCP 세션)
1. **시장 읽기** — `get_crypto_*`(funding/OI/캔들) 및 최근 스캘핑 리뷰/벤치마크(`/invest/scalping`)와
   과거 결정·결과(이전 `signal_snapshot`/리뷰)를 검토한다.
2. **결정** — symbol(XRPUSDT/DOGEUSDT/SOLUSDT 중)·side·근거(rationale)를 정한다.
3. **dry-run 확인** — `binance_demo_scalping_submit_decision(symbol, side, rationale, dry_run=true)`로 계획 확인.
4. **주입** — `...(dry_run=false, confirm=true)`로 실 데모 라운드트립 실행. 결과(status/realized_pnl)를 기록.
5. **회고** — 다음 세션에서 직전 결정의 결과(net vs buy&hold, LLM vs 규칙 baseline)를 보고 전략을 조정한다.

## 안전
- 1x · notional<=10 USDT · 손실예산 게이트(Phase 1) · demo-fapi only — executor가 강제.
- 같은 날 결과는 `session_tag="llm"`로 분리 집계(규칙 baseline과 비교; surfacing은 D-PR2).
```

- [ ] **Step 8: 통과 + audit + 회귀**

Run:
```bash
uv run pytest tests/mcp_server/test_binance_demo_scalping_submit_decision.py tests/services/brokers/binance/test_audit_no_signed_endpoints.py -v
```
Expected: PASS (도구 5 + audit). audit가 registry.py를 새로 플래그하면 그 파일도 ALLOWED_LEGACY_FILES에 동일 형식으로 등재 후 재실행.

- [ ] **Step 9: 커밋**

```bash
git add app/core/config.py app/mcp_server/tooling/binance_demo_scalping_handler.py app/mcp_server/tooling/registry.py tests/services/brokers/binance/test_audit_no_signed_endpoints.py tests/mcp_server/test_binance_demo_scalping_submit_decision.py docs/runbooks/binance-demo-scalping-llm-session.md
git commit -F - <<'EOF'
feat: binance_demo_scalping_submit_decision MCP tool (Phase 3 D-PR1)

out-of-process LLM의 결정을 데모 스캘핑에 주입하는 결정론적 MCP 도구.
execute_monitored 재사용(session_tag="llm" + signal_snapshot 근거기록),
dry_run 기본+confirm 게이트, allowlist/데모 안전경계 상속. settings 게이트로
DEFAULT 프로필 조건부 등록. ROB-285 allowlist 등재. 운영 런북 포함.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
EOF
```

---

### 최종 검증

- [ ] **Step: binance 패키지 전체 + MCP + lint/type**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.phase3-llm-decision
uv run pytest tests/services/brokers/binance/ tests/mcp_server/test_binance_demo_scalping_submit_decision.py -q -p no:cacheprovider
uv run ruff format app/services/brokers/binance/demo_scalping_exec/executor.py app/mcp_server/tooling/binance_demo_scalping_handler.py app/mcp_server/tooling/registry.py app/core/config.py tests/services/brokers/binance/demo_scalping_exec/test_executor_session_tag.py tests/mcp_server/test_binance_demo_scalping_submit_decision.py
uv run ruff check app/mcp_server/tooling/binance_demo_scalping_handler.py app/services/brokers/binance/demo_scalping_exec/executor.py app/mcp_server/tooling/registry.py app/core/config.py
uv run ty check app/
```
Expected: 신규 테스트 + binance 패키지(audit 포함) 회귀 PASS, ruff/ty clean.

---

## Self-Review (spec 대비)

- **§2.1-1 session_tag 배선** → Task 1(+signal_snapshot 동반). ✓
- **§2.1-2 결정 주입 MCP 도구** → Task 2. ✓ (dry_run/confirm/allowlist/게이트/실행)
- **§2.1-3 런북** → Task 2 Step 7. ✓
- **§3.2 기록처** → 스펙의 strategy_events를 `signal_snapshot`으로 정제(상단 노트; 추출로 strategy_events 부적합 확인). ✓
- **§5 경계** → LLM 호출 없음(결정론적), 데모 전용(from_env/executor 재사용), 이중게이트, ROB-285 allowlist, 무회귀(기본 None). ✓
- **§6 에러** → 게이트/allowlist/빈 rationale 거부, dry_run 계획만, execute 예외 → error dict, 거래는 executor가 anomaly 처리. ✓
- **§7 테스트** → session_tag/snapshot 기록+무회귀(Task1); dry_run/confirm/게이트/allowlist/기록(Task2). ✓
- **§8 위험** → measurability(session_tag 분리), dry_run 기본, ROB-285. ✓
- **범위밖** → D-PR2(리뷰 비교 surfacing)·Hermes 자율·자동 피드백 미포함. ✓
- 마이그레이션 없음(session_tag/signal_snapshot 기존 컬럼). ✓
