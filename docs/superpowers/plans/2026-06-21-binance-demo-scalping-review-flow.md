# Binance Demo 스캘핑 일별 리뷰+벤치마크 자동화 (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 매일 자동으로 데모 product별 스캘핑 리뷰 draft 롤업 + buy&hold 벤치마크를 생성해 `/invest/scalping`에 전략 net vs 패시브가 채워지게 한다. recurrence는 Prefect로 통일.

**Architecture:** 실제 로직은 prefect-free `app/jobs/binance_demo_scalping_review.py`(유닛테스트 가능), `app/flows/binance_demo_scalping_review_flow.py`는 thin Prefect 래퍼(정적 테스트만 — Prefect는 프로젝트 의존성이 아님). env-gate default-off. 배포 등록은 외부(robin-prefect-automations) deferred·paused. 읽기전용(주문 mutation 없음).

**Tech Stack:** Python 3.13, SQLAlchemy(async), Prefect(@flow/@task, 미설치=정적테스트), pytest-asyncio, Decimal.

## Global Constraints

- Python 3.13+. 변경은 worktree `feature/binance-demo-scalping-review-flow`에서. canonical repo는 main 고정.
- **Prefect는 미설치** — `app/flows/*` 모듈은 테스트에서 import 불가. 실제 로직은 `app/jobs/`(prefect-free)에 두고 flow는 thin 래퍼. flow 테스트는 정적(파일 내용) only (기존 `tests/test_invest_screener_snapshots_us_flow.py` 패턴).
- **default-off**: `settings.binance_demo_scalping_review_flow_enabled`(기본 False). 미설정 시 `{"status":"disabled"}` no-op.
- **읽기전용 경계**: 주문/브로커 mutation 모듈(executor/execution_client) import 금지. `ScalpingReviewService`/`benchmark_runner`/`DemoScalpingMarketData`만.
- **In-repo cron 금지**: `@broker.task(schedule=...)` 부착 금지(Prefect로 통일). 실제 recurrence는 외부 deployment(이 PR 아님).
- 마이그레이션 없음(`benchmark_return_bps` 컬럼은 Phase 1에서 추가됨).
- account_scope는 `"binance_demo"`(`SCALPING_REVIEW_ACCOUNT_SCOPE`) 고정, session_tag 기본 `""`.
- Decimal은 문자열 직렬화(`_num`).
- TDD: 실패 테스트 → 최소 구현 → 통과 → 커밋.
- 커밋 trailer:
  ```
  Co-Authored-By: Paperclip <noreply@paperclip.ing>
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
  ```

## File Structure

- `app/core/config.py` (수정) — `binance_demo_scalping_review_flow_enabled: bool = False` 추가.
- `app/jobs/binance_demo_scalping_review.py` (생성) — `run_demo_scalping_review_refresh(...)` 실제 로직(prefect-free, 유닛테스트 가능).
- `app/flows/binance_demo_scalping_review_flow.py` (생성) — thin `@task`+`@flow` 래퍼.
- `tests/jobs/test_binance_demo_scalping_review.py` (생성) — job 유닛테스트(게이트 off/on/격리).
- `tests/test_binance_demo_scalping_review_flow.py` (생성) — flow 정적 테스트.

---

### Task 1: config flag + job 로직 + job 유닛테스트

**Files:**
- Modify: `app/core/config.py` (Settings 클래스, `kis_mock_scalping_ws_confirm: bool = False` 근처)
- Create: `app/jobs/binance_demo_scalping_review.py`
- Test: `tests/jobs/test_binance_demo_scalping_review.py`

**Interfaces:**
- Consumes: `settings.binance_demo_scalping_review_flow_enabled`(이 태스크가 추가); `ScalpingReviewService(session).build_draft(*, review_date, product, now, session_tag="", account_scope=SCALPING_REVIEW_ACCOUNT_SCOPE) -> ScalpingDailyReview`(`.trade_count`,`.net_return_bps`,`.benchmark_return_bps`); `compute_and_store_daily_benchmark(*, session, market_data, review_date, product, now, session_tag="", account_scope=...) -> Decimal | None`; `DemoScalpingMarketData()`/`.aclose()`/`.fetch_klines(product, symbol, *, interval, limit)`; `AsyncSessionLocal`.
- Produces: `run_demo_scalping_review_refresh(*, review_date: dt.date | None = None, products: Sequence[str] = ("spot","usdm_futures"), now: dt.datetime | None = None, session: AsyncSession | None = None, market_data: Any | None = None) -> dict[str, Any]` — `{"status":"disabled"}` or `{"status":"ran","reviewDate":iso,"products":[{product,tradeCount,netReturnBps,benchmarkReturnBps}],"errors":[{product,error}]}`. Task 2가 호출.

- [ ] **Step 1: 실패 테스트 작성**

`tests/jobs/test_binance_demo_scalping_review.py` 생성. 상단에 **`tests/services/scalping_reviews/test_service.py`의 22-67행(`_DATE`,`_NOW`,`_instrument`,`_analytics`)을 복사**하고, **`tests/services/brokers/binance/demo_scalping_exec/test_benchmark_runner.py`의 `_FakeMD` 클래스를 복사**(또는 아래 정의를 사용)한 뒤:

```python
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.jobs.binance_demo_scalping_review import run_demo_scalping_review_refresh
from app.models.crypto_instruments import CryptoInstrument
from app.models.scalp_trade_analytics import ScalpTradeAnalytics
from app.services.brokers.binance.demo_scalping.signal import Candle
from app.services.scalping_reviews.service import ScalpingReviewService

# (여기에 test_service.py 22-67행의 _DATE/_NOW/_instrument/_analytics 복사)


class _FakeMD:
    def __init__(self, prices: dict[str, tuple[float, float]]) -> None:
        self._prices = prices

    async def fetch_klines(self, product, symbol, *, interval="1m", limit=50):
        o, c = self._prices[symbol]
        return [
            Candle(
                open_time_ms=0,
                open=Decimal(str(o)),
                high=Decimal(str(max(o, c))),
                low=Decimal(str(min(o, c))),
                close=Decimal(str(c)),
                close_time_ms=0,
            )
        ]

    async def aclose(self) -> None:  # pragma: no cover - injected, not owned
        return None


@pytest.mark.asyncio
async def test_disabled_gate_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(settings, "binance_demo_scalping_review_flow_enabled", False)
    result = await run_demo_scalping_review_refresh(
        review_date=_DATE, products=("usdm_futures",), now=_NOW
    )
    assert result == {"status": "disabled"}


@pytest.mark.asyncio
async def test_enabled_builds_review_and_benchmark(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "binance_demo_scalping_review_flow_enabled", True)
    iid = await _instrument(db_session, "XRPUSDT")
    await _analytics(
        db_session, iid, tag="w", symbol="XRPUSDT",
        entry_price=Decimal("100"), exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"), net_pnl_usdt=Decimal("0.9"),
        gross_pnl_usdt=Decimal("1.0"), exit_reason="take_profit",
    )
    md = _FakeMD({"XRPUSDT": (100.0, 101.0)})  # +100 bps
    result = await run_demo_scalping_review_refresh(
        session=db_session, market_data=md, review_date=_DATE,
        products=("usdm_futures",), now=_NOW,
    )
    assert result["status"] == "ran"
    assert result["reviewDate"] == _DATE.isoformat()
    assert result["errors"] == []
    [summary] = result["products"]
    assert summary == {
        "product": "usdm_futures",
        "tradeCount": 1,
        "netReturnBps": "90.0000",
        "benchmarkReturnBps": "100",
    }
    review = await ScalpingReviewService(db_session)._get_by_key(
        _DATE, "usdm_futures", "binance_demo", ""
    )
    assert review is not None and review.benchmark_return_bps == Decimal("100")


@pytest.mark.asyncio
async def test_product_failure_is_isolated(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "binance_demo_scalping_review_flow_enabled", True)
    iid = await _instrument(db_session, "XRPUSDT")
    await _analytics(
        db_session, iid, tag="w", symbol="XRPUSDT",
        entry_price=Decimal("100"), exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"), net_pnl_usdt=Decimal("0.9"),
        gross_pnl_usdt=Decimal("1.0"), exit_reason="take_profit",
    )
    md = _FakeMD({"XRPUSDT": (100.0, 101.0)})
    orig = ScalpingReviewService.build_draft

    async def flaky(self, *, review_date, product, now, **kw):
        if product == "spot":
            raise RuntimeError("boom")
        return await orig(self, review_date=review_date, product=product, now=now, **kw)

    monkeypatch.setattr(ScalpingReviewService, "build_draft", flaky)
    result = await run_demo_scalping_review_refresh(
        session=db_session, market_data=md, review_date=_DATE,
        products=("spot", "usdm_futures"), now=_NOW,
    )
    assert result["status"] == "ran"
    assert [s["product"] for s in result["products"]] == ["usdm_futures"]
    assert [e["product"] for e in result["errors"]] == ["spot"]
```

> 참고: `netReturnBps == "90.0000"` — net_pnl 0.9 / entry_notional 100 × 10000 = 90 bps, `Numeric(12,4)`라 `"90.0000"`. `benchmarkReturnBps == "100"` — 단일 심볼 (101/100-1)×10000.

- [ ] **Step 2: 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.phase2-review-flow && uv run pytest tests/jobs/test_binance_demo_scalping_review.py -v`
Expected: FAIL — `ModuleNotFoundError: app.jobs.binance_demo_scalping_review` (및 `settings`에 flag 속성 없음 → AttributeError on monkeypatch).

- [ ] **Step 3: config flag 추가**

`app/core/config.py`의 `kis_mock_scalping_ws_confirm: bool = False`(약 230행) **바로 다음**에 추가:

```python
    # Phase 2 — daily demo scalping review + buy&hold benchmark flow (default-off).
    binance_demo_scalping_review_flow_enabled: bool = False
```

- [ ] **Step 4: job 모듈 구현**

`app/jobs/binance_demo_scalping_review.py` 생성:

```python
"""Phase 2 — daily demo scalping review + buy&hold benchmark refresh (job logic).

Prefect-free so it is unit-testable (the @flow/@task wrapper lives in
``app/flows/binance_demo_scalping_review_flow.py``). Default-OFF behind
``settings.binance_demo_scalping_review_flow_enabled``. Read-only w.r.t.
brokers/orders: rolls that day's ``scalp_trade_analytics`` into the daily
review draft (``build_draft``) and computes the notional-weighted daily
buy&hold benchmark (``compute_and_store_daily_benchmark``). Per-product
failures are isolated via a SAVEPOINT so one product cannot poison another.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.brokers.binance.demo_scalping.market_data import (
    DemoScalpingMarketData,
)
from app.services.brokers.binance.demo_scalping_exec.benchmark_runner import (
    compute_and_store_daily_benchmark,
)
from app.services.scalping_reviews.service import ScalpingReviewService

logger = logging.getLogger(__name__)

_DEFAULT_PRODUCTS: tuple[str, ...] = ("spot", "usdm_futures")


def _num(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


async def _refresh_with_session(
    session: AsyncSession,
    market_data: Any,
    review_date: dt.date,
    products: Sequence[str],
    now: dt.datetime,
) -> dict[str, Any]:
    service = ScalpingReviewService(session)
    summaries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for product in products:
        try:
            async with session.begin_nested():  # SAVEPOINT: isolate per product
                review = await service.build_draft(
                    review_date=review_date, product=product, now=now
                )
                benchmark = await compute_and_store_daily_benchmark(
                    session=session,
                    market_data=market_data,
                    review_date=review_date,
                    product=product,
                    now=now,
                )
                summaries.append(
                    {
                        "product": product,
                        "tradeCount": review.trade_count,
                        "netReturnBps": _num(review.net_return_bps),
                        "benchmarkReturnBps": _num(benchmark),
                    }
                )
        except Exception as exc:  # noqa: BLE001 — isolate; savepoint rolled back
            logger.exception(
                "demo scalping review refresh failed for product=%s", product
            )
            errors.append(
                {"product": product, "error": f"{type(exc).__name__}: {exc}"}
            )
    return {
        "status": "ran",
        "reviewDate": review_date.isoformat(),
        "products": summaries,
        "errors": errors,
    }


async def run_demo_scalping_review_refresh(
    *,
    review_date: dt.date | None = None,
    products: Sequence[str] = _DEFAULT_PRODUCTS,
    now: dt.datetime | None = None,
    session: AsyncSession | None = None,
    market_data: Any | None = None,
) -> dict[str, Any]:
    """Build the daily review draft + buy&hold benchmark per demo product.

    No-op (``{"status": "disabled"}``) unless the env flag is set. When
    ``session``/``market_data`` are injected (tests) they are used as-is and
    NOT committed/closed by this function; otherwise they are created and
    committed/closed here."""
    if not settings.binance_demo_scalping_review_flow_enabled:
        return {"status": "disabled"}

    now = now or dt.datetime.now(dt.UTC)
    review_date = review_date or now.astimezone(dt.UTC).date()

    owns_md = market_data is None
    md = market_data or DemoScalpingMarketData()
    try:
        if session is not None:
            return await _refresh_with_session(
                session, md, review_date, products, now
            )
        async with AsyncSessionLocal() as own_session:
            result = await _refresh_with_session(
                own_session, md, review_date, products, now
            )
            await own_session.commit()
            return result
    finally:
        if owns_md:
            await md.aclose()
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/jobs/test_binance_demo_scalping_review.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.phase2-review-flow
git add app/core/config.py app/jobs/binance_demo_scalping_review.py tests/jobs/test_binance_demo_scalping_review.py
git commit -F - <<'EOF'
feat: daily demo scalping review + benchmark job (Phase 2)

prefect-free run_demo_scalping_review_refresh: product별 build_draft 롤업 +
buy&hold 벤치마크(benchmark_runner). env-gate default-off, product별 SAVEPOINT
격리, 주입형 session/market_data로 유닛테스트 가능. 읽기전용(주문 mutation 없음).

Co-Authored-By: Paperclip <noreply@paperclip.ing>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
EOF
```

---

### Task 2: thin Prefect flow 래퍼 + 정적 테스트

**Files:**
- Create: `app/flows/binance_demo_scalping_review_flow.py`
- Test: `tests/test_binance_demo_scalping_review_flow.py`

**Interfaces:**
- Consumes: `run_demo_scalping_review_refresh()`(Task 1).
- Produces: `binance_demo_scalping_review_flow` (Prefect `@flow`), `binance_demo_scalping_review_task` (`@task`).

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_binance_demo_scalping_review_flow.py` 생성 (Prefect 미설치이므로 정적 검증 — `tests/test_invest_screener_snapshots_us_flow.py` 패턴):

```python
"""Static checks for the Phase 2 demo scalping review Prefect flow.

Prefect is not a project dependency, so the flow module is validated by file
content (decorators / names / delegation / deferred deployment), not import.
The real logic is unit-tested in tests/jobs/test_binance_demo_scalping_review.py.
"""

from __future__ import annotations

from pathlib import Path

_FLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "flows"
    / "binance_demo_scalping_review_flow.py"
)


def test_flow_file_exists() -> None:
    assert _FLOW_PATH.exists(), f"Flow file not found at {_FLOW_PATH}"


def test_flow_declares_prefect_flow_and_task() -> None:
    text = _FLOW_PATH.read_text()
    assert "@flow" in text, "Missing @flow decorator"
    assert "@task" in text, "Missing @task decorator"
    assert "binance_demo_scalping_review" in text, "Missing flow name"


def test_flow_delegates_to_job() -> None:
    text = _FLOW_PATH.read_text()
    assert "run_demo_scalping_review_refresh" in text, (
        "Flow must delegate to the prefect-free job helper"
    )


def test_flow_does_not_attach_in_repo_schedule() -> None:
    text = _FLOW_PATH.read_text()
    assert "@broker.task" not in text and "schedule=" not in text, (
        "Recurrence is Prefect-only; no in-repo TaskIQ schedule"
    )


def test_flow_not_registered_via_deployment_yaml() -> None:
    project_root = _FLOW_PATH.parents[1]
    import pytest

    yaml_files = list(project_root.glob("**/*.yaml")) + list(
        project_root.glob("**/*.yml")
    )
    for yf in yaml_files:
        if ".venv" in str(yf) or ".git" in str(yf):
            continue
        if "binance_demo_scalping_review" in yf.read_text():
            pytest.fail(
                f"Deployment YAML references the flow at {yf}; "
                "registration is deferred (external robin-prefect-automations)."
            )
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_binance_demo_scalping_review_flow.py -v`
Expected: FAIL — `test_flow_file_exists` (파일 없음).

- [ ] **Step 3: flow 래퍼 구현**

`app/flows/binance_demo_scalping_review_flow.py` 생성:

```python
"""Phase 2 — Prefect wrapper for the daily demo scalping review + benchmark.

Importable only; NO deployment registered here. Recurrence lives in
robin-prefect-automations (paused-by-default). Default-OFF via
``settings.binance_demo_scalping_review_flow_enabled`` (enforced in the job).
All logic is in app/jobs/binance_demo_scalping_review.py (prefect-free).
"""

from __future__ import annotations

from typing import Any

from prefect import flow, task

from app.jobs.binance_demo_scalping_review import run_demo_scalping_review_refresh


@task(name="binance_demo_scalping_review")
async def binance_demo_scalping_review_task() -> dict[str, Any]:
    return await run_demo_scalping_review_refresh()


@flow(name="binance_demo_scalping_review")
async def binance_demo_scalping_review_flow() -> dict[str, Any]:
    """Daily review + buy&hold benchmark; deployment registration deferred."""
    return await binance_demo_scalping_review_task()
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_binance_demo_scalping_review_flow.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: 커밋**

```bash
git add app/flows/binance_demo_scalping_review_flow.py tests/test_binance_demo_scalping_review_flow.py
git commit -F - <<'EOF'
feat: Prefect flow wrapper for daily demo scalping review (Phase 2)

thin @task/@flow가 prefect-free job(run_demo_scalping_review_refresh)에 위임.
배포 등록은 외부(robin-prefect-automations) deferred·paused. in-repo TaskIQ
schedule 미부착(Prefect 통일). Prefect 미설치라 정적 테스트.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
EOF
```

---

### 최종 검증

- [ ] **Step: 관련 스코프 회귀 + lint/type**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.phase2-review-flow
uv run pytest tests/jobs/test_binance_demo_scalping_review.py tests/test_binance_demo_scalping_review_flow.py tests/services/scalping_reviews/ tests/services/brokers/binance/demo_scalping_exec/ -q
uv run ruff format app/jobs/binance_demo_scalping_review.py app/flows/binance_demo_scalping_review_flow.py app/core/config.py tests/jobs/test_binance_demo_scalping_review.py tests/test_binance_demo_scalping_review_flow.py
uv run ruff check app/jobs/binance_demo_scalping_review.py app/flows/binance_demo_scalping_review_flow.py app/core/config.py tests/jobs/test_binance_demo_scalping_review.py tests/test_binance_demo_scalping_review_flow.py
uv run ty check app/
```
Expected: 신규 8 tests + 관련 스코프 회귀 전부 PASS, ruff/ty clean.

---

## Self-Review (spec 대비)

- **§3.1 job 로직(build_draft + benchmark per product)** → Task 1. ✓ (스펙은 flow 파일에 로직, but Prefect 미설치라 prefect-free job 모듈로 분리 — 정제, 더 testable)
- **§3.2 config flag** → Task 1 Step 3. ✓
- **§3.1 flow 래퍼** → Task 2. ✓
- **§5 경계(읽기전용, 라우터 무관, default-off)** → job/flow가 executor/execution_client 미import; gate; 정적 테스트가 `@broker.task`/`schedule=` 금지 확인. ✓
- **§6 에러처리(게이트 off no-op / product 격리 / md 실패 best-effort)** → gate test, 격리 test(savepoint), benchmark_runner 자체 best-effort. ✓
- **§7 테스트** → gate off / gate on per-product / 격리 3종 + flow 정적 5종. ✓
- **§8 위험(중복스케줄 금지)** → Global Constraints + flow 정적 테스트가 `@broker.task`/`schedule=` 부재 단언. ✓
- **§2 범위밖(배포 등록 외부)** → flow docstring + 정적 테스트가 deployment yaml 부재 단언; 플랜은 배포 등록 안 함. ✓
- 마이그레이션 없음(Phase 1 컬럼 재사용) ✓.
