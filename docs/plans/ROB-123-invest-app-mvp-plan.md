# ROB-123 — `/invest/app` 토스식 모바일 투자 앱 MVP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/invest/app` 토스식 모바일 read-only 통합 홈 (Hero + 계좌 카드 + source 필터 + 보유 리스트) 을 신규 React 패키지 + read-only `/invest/api/home` 으로 출시한다. 기존 `/trading/decisions/*` 영향 0.

**Architecture:** `frontend/invest/` 신규 Vite + React 19 + react-router-dom 7 패키지(`base="/invest/app/"`). 백엔드는 SPA shell 라우터 `app/routers/invest_app_spa.py` (trading_decisions_spa 미러링) + read-only API 라우터 `app/routers/invest_api.py` + 합성 서비스 `app/services/invest_home_service.py` + Pydantic 스키마 `app/schemas/invest_home.py`. 합산 정책 `HOME_INCLUDED_SOURCES = {"kis","upbit","toss_manual"}` 은 백엔드에 하드코딩.

**Tech Stack:** FastAPI · Pydantic · React 19 · Vite 8 · react-router-dom 7 · vitest · @testing-library/react · pytest · ruff · uv.

**Spec:** `docs/superpowers/specs/2026-05-06-invest-app-mvp-design.md`.

**Safety (모든 task 공통):**
- 주문 제출/취소/정정/broker mutation/watch/order-intent mutation 금지
- scheduler/worker 변경 · DB migration/backfill/update/delete 금지
- realtime quote/websocket/chart 구현 금지
- `buyingPower` 는 display only — 클릭 핸들러 금지

---

## File Structure

### Created — Backend
| Path | Responsibility |
|---|---|
| `app/schemas/invest_home.py` | Pydantic models: `AccountKindLiteral`, `AccountSourceLiteral`, `MarketLiteral`, `AssetTypeLiteral`, `Account`, `Holding`, `GroupedHolding`, `HomeSummary`, `InvestHomeWarning`, `InvestHomeResponseMeta`, `InvestHomeResponse` |
| `app/services/invest_home_service.py` | `InvestHomeService` — KIS + Upbit + manual(toss) holdings 합성 + grouping + `HOME_INCLUDED_SOURCES` 적용 + 부분 실패 warnings |
| `app/routers/invest_api.py` | `APIRouter(prefix="/invest/api")`, `GET /home` → `InvestHomeResponse` |
| `app/routers/invest_app_spa.py` | `APIRouter(prefix="/invest/app")` SPA shell + assets + fallback (trading_decisions_spa 미러링) |
| `tests/test_invest_home_service.py` | grouping 규칙 / `includedInHome` 정책 / 부분 실패 warnings unit |
| `tests/test_invest_api_router.py` | `GET /invest/api/home` integration (TestClient) |
| `tests/test_invest_app_spa_router_safety.py` | SPA router import-safety |
| `tests/test_invest_api_router_safety.py` | API router + service import-safety |

### Modified — Backend
| Path | What |
|---|---|
| `app/main.py` | `from app.routers import (... invest_api, invest_app_spa)` + `app.include_router(...)` 두 줄 추가 |

### Created — Frontend
| Path | Responsibility |
|---|---|
| `frontend/invest/package.json` | `@auto-trader/invest`, scripts: dev/build/preview/typecheck/test |
| `frontend/invest/.gitignore` | `node_modules/`, `dist/`, `.vite/`, `*.local`, `*.tsbuildinfo` |
| `frontend/invest/.nvmrc` | `20` |
| `frontend/invest/index.html` | Root mount, viewport meta, dark BG |
| `frontend/invest/vite.config.ts` | `base: "/invest/app/"` + dev proxy |
| `frontend/invest/vitest.config.ts` | jsdom + setup |
| `frontend/invest/tsconfig.json` / `tsconfig.node.json` | trading-decision 미러링 |
| `frontend/invest/README.md` | 빌드/실행 안내 |
| `frontend/invest/src/main.tsx` | React entry |
| `frontend/invest/src/App.tsx` | `<RouterProvider />` |
| `frontend/invest/src/routes.tsx` | `/`, `/paper`, `/paper/:variant`, `*` |
| `frontend/invest/src/vite-env.d.ts` | vite/client types |
| `frontend/invest/src/styles.css` | dark theme tokens, layout primitives |
| `frontend/invest/src/types/invest.ts` | spec §"Frontend types" 그대로 |
| `frontend/invest/src/api/investHome.ts` | `fetchInvestHome()` (read-only fetch) |
| `frontend/invest/src/format/{currency,percent,number}.ts` | KRW/USD/% formatter, `null → "-"` fallback |
| `frontend/invest/src/hooks/useInvestHome.ts` | `useEffect` + `AbortController` 기반 fetch hook |
| `frontend/invest/src/components/AppShell.tsx` | mobile-width app shell, header |
| `frontend/invest/src/components/HeroCard.tsx` | `homeSummary` 표시 |
| `frontend/invest/src/components/AccountCardList.tsx` | KIS/Upbit/Toss 가로 스크롤. live badge 없음. KIS=KRW/USD 4칸, Upbit=KRW 2칸, Toss=차분 `수동` badge + `-` fallback |
| `frontend/invest/src/components/SourceFilterBar.tsx` | `all/kis/upbit/toss_manual` chip |
| `frontend/invest/src/components/HoldingRow.tsx` | `RawRow` + `GroupedRow` 두 컴포넌트 |
| `frontend/invest/src/components/BottomNav.tsx` | placeholder 4개, 클릭=`alert("준비 중")` |
| `frontend/invest/src/pages/HomePage.tsx` | `useInvestHome()` + 위 컴포넌트 조립 |
| `frontend/invest/src/pages/PaperPlaceholderPage.tsx` | "준비 중" placeholder, `:variant` param 표시 |
| `frontend/invest/src/test/setup.ts` | `@testing-library/jest-dom/vitest` + `cleanup` |
| `frontend/invest/src/__tests__/AccountCardList.test.tsx` | KIS/Upbit live badge 미표시, Toss 수동 badge 표시, cashBalances/buyingPower 렌더 |
| `frontend/invest/src/__tests__/HoldingRow.test.tsx` | GroupedRow `includedSources` chip, null→`-` fallback, buyingPower 클릭 핸들러 부재 |
| `frontend/invest/src/__tests__/HomePage.test.tsx` | activeSource 토글 → grouped vs raw 전환, meta.warnings 1줄 노출 |

---

### Task 1 — Branch & sanity baseline

**Files:**
- 없음 (working state 확인)

- [ ] **Step 1: 작업 브랜치 확인**

```bash
git rev-parse --abbrev-ref HEAD
```

Expected: `linear-mcp-rob-123` (이미 worktree 안. 다른 브랜치면 `git switch linear-mcp-rob-123`).

- [ ] **Step 2: working tree 깨끗한지 확인**

```bash
git status --short
```

Expected: spec 관련 commit 외에 변경 없음. 다른 변경이 있으면 commit 또는 stash 한 후 진행.

- [ ] **Step 3: 기존 회귀 baseline 캡처**

```bash
uv run --group test pytest tests/test_trading_decisions_spa_router_safety.py -q
cd frontend/trading-decision && npm ci --silent && npm run typecheck && npm test -- --run && cd -
```

Expected: 모두 PASS. `/trading/decisions/*` 가 출발점에서 깨지지 않은 것을 기록.

---

### Task 2 — Backend Pydantic schemas (`app/schemas/invest_home.py`)

**Files:**
- Create: `app/schemas/invest_home.py`
- Test: `tests/test_invest_home_service.py` (validate 단계에서 schema 사용)

- [ ] **Step 1: Create the schema module**

```python
# app/schemas/invest_home.py
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccountKindLiteral = Literal["live", "manual", "paper"]
AccountSourceLiteral = Literal[
    "kis", "upbit", "toss_manual",
    "pension_manual", "isa_manual",
    "kis_mock", "kiwoom_mock", "alpaca_paper", "db_simulated",
]
MarketLiteral = Literal["KR", "US", "CRYPTO"]
AssetTypeLiteral = Literal["equity", "etf", "crypto", "fund", "other"]
CurrencyLiteral = Literal["KRW", "USD"]


class CashAmounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    krw: float | None = None
    usd: float | None = None


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accountId: str
    displayName: str
    source: AccountSourceLiteral
    accountKind: AccountKindLiteral
    includedInHome: bool
    valueKrw: float
    costBasisKrw: float | None = None
    pnlKrw: float | None = None
    pnlRate: float | None = None
    cashBalances: CashAmounts = Field(default_factory=CashAmounts)
    buyingPower: CashAmounts = Field(default_factory=CashAmounts)


class Holding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    holdingId: str
    accountId: str
    source: AccountSourceLiteral
    accountKind: AccountKindLiteral
    symbol: str
    market: MarketLiteral
    assetType: AssetTypeLiteral
    displayName: str
    quantity: float
    averageCost: float | None = None
    costBasis: float | None = None
    currency: CurrencyLiteral
    valueNative: float | None = None
    valueKrw: float | None = None
    pnlKrw: float | None = None
    pnlRate: float | None = None


class GroupedSourceBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")
    holdingId: str
    accountId: str
    source: AccountSourceLiteral
    quantity: float
    averageCost: float | None = None
    costBasis: float | None = None
    valueNative: float | None = None
    valueKrw: float | None = None
    pnlKrw: float | None = None
    pnlRate: float | None = None


class GroupedHolding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groupId: str
    symbol: str
    market: MarketLiteral
    assetType: AssetTypeLiteral
    displayName: str
    currency: CurrencyLiteral
    totalQuantity: float
    averageCost: float | None = None
    costBasis: float | None = None
    valueNative: float | None = None
    valueKrw: float | None = None
    pnlKrw: float | None = None
    pnlRate: float | None = None
    includedSources: list[AccountSourceLiteral]
    sourceBreakdown: list[GroupedSourceBreakdown]


class HomeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    includedSources: list[AccountSourceLiteral]
    excludedSources: list[AccountSourceLiteral]
    totalValueKrw: float
    costBasisKrw: float | None = None
    pnlKrw: float | None = None
    pnlRate: float | None = None


class InvestHomeWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: AccountSourceLiteral
    message: str


class InvestHomeResponseMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warnings: list[InvestHomeWarning] = Field(default_factory=list)


class InvestHomeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    homeSummary: HomeSummary
    accounts: list[Account]
    holdings: list[Holding]
    groupedHoldings: list[GroupedHolding]
    meta: InvestHomeResponseMeta = Field(default_factory=InvestHomeResponseMeta)
```

- [ ] **Step 2: ruff check & format**

```bash
uv run ruff check app/schemas/invest_home.py
uv run ruff format --check app/schemas/invest_home.py
```

Expected: PASS. (포맷 안 맞으면 `uv run ruff format app/schemas/invest_home.py` 실행 후 재확인.)

- [ ] **Step 3: import-only sanity check**

```bash
uv run python -c "from app.schemas.invest_home import InvestHomeResponse; print(InvestHomeResponse.model_json_schema()['title'])"
```

Expected: `InvestHomeResponse`.

- [ ] **Step 4: Commit**

```bash
git add app/schemas/invest_home.py
git commit -m "feat(rob-123): add invest_home Pydantic schemas

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 3 — Backend `InvestHomeService` (TDD: grouping & policy)

**Files:**
- Create: `app/services/invest_home_service.py`
- Test: `tests/test_invest_home_service.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_invest_home_service.py
"""ROB-123 — InvestHomeService unit tests (read-only)."""
from __future__ import annotations

import pytest

from app.schemas.invest_home import Holding
from app.services.invest_home_service import (
    HOME_INCLUDED_SOURCES,
    build_grouped_holdings,
    build_home_summary,
    classify_account_kind,
)


def _h(**kw) -> Holding:
    base = dict(
        holdingId="x", accountId="a", source="kis", accountKind="live",
        symbol="AAA", market="KR", assetType="equity", displayName="AAA",
        quantity=1.0, averageCost=None, costBasis=None, currency="KRW",
        valueNative=None, valueKrw=None, pnlKrw=None, pnlRate=None,
    )
    base.update(kw)
    return Holding(**base)


@pytest.mark.unit
def test_home_included_sources_is_locked() -> None:
    assert HOME_INCLUDED_SOURCES == frozenset({"kis", "upbit", "toss_manual"})


@pytest.mark.unit
def test_classify_account_kind_maps_sources() -> None:
    assert classify_account_kind("kis") == "live"
    assert classify_account_kind("upbit") == "live"
    assert classify_account_kind("toss_manual") == "manual"
    assert classify_account_kind("pension_manual") == "manual"
    assert classify_account_kind("isa_manual") == "manual"
    assert classify_account_kind("kis_mock") == "paper"
    assert classify_account_kind("kiwoom_mock") == "paper"
    assert classify_account_kind("alpaca_paper") == "paper"
    assert classify_account_kind("db_simulated") == "paper"


@pytest.mark.unit
def test_grouped_merges_same_market_assettype_currency_symbol() -> None:
    h_kis = _h(holdingId="1", source="kis", accountId="a1",
               symbol="005930", market="KR", currency="KRW",
               quantity=30, averageCost=70000, costBasis=2_100_000,
               valueNative=2_148_000, valueKrw=2_148_000,
               pnlKrw=48_000, pnlRate=48_000 / 2_100_000)
    h_toss = _h(holdingId="2", source="toss_manual", accountId="a2",
                accountKind="manual",
                symbol="005930", market="KR", currency="KRW",
                quantity=20, averageCost=68_800, costBasis=1_376_000,
                valueNative=1_432_000, valueKrw=1_432_000,
                pnlKrw=56_000, pnlRate=56_000 / 1_376_000)
    grouped = build_grouped_holdings([h_kis, h_toss])
    assert len(grouped) == 1
    g = grouped[0]
    assert g.groupId == "KR:equity:KRW:005930"
    assert g.totalQuantity == 50
    assert g.costBasis == 2_100_000 + 1_376_000
    assert g.averageCost == pytest.approx((2_100_000 + 1_376_000) / 50)
    assert g.valueKrw == 2_148_000 + 1_432_000
    assert sorted(g.includedSources) == ["kis", "toss_manual"]
    assert {b.holdingId for b in g.sourceBreakdown} == {"1", "2"}


@pytest.mark.unit
def test_grouped_null_costbasis_propagates() -> None:
    a = _h(holdingId="1", source="kis", symbol="NVDA", market="US",
           currency="USD", quantity=2, averageCost=120, costBasis=240,
           valueNative=300, valueKrw=400_000)
    b = _h(holdingId="2", source="toss_manual", accountKind="manual",
           symbol="NVDA", market="US", currency="USD",
           quantity=5, averageCost=None, costBasis=None,
           valueNative=750, valueKrw=1_000_000)
    grouped = build_grouped_holdings([a, b])
    assert len(grouped) == 1
    g = grouped[0]
    assert g.totalQuantity == 7
    assert g.costBasis is None
    assert g.averageCost is None
    assert g.pnlKrw is None
    assert g.pnlRate is None
    assert g.valueKrw == 1_400_000


@pytest.mark.unit
def test_grouped_never_merges_crypto_with_equity() -> None:
    eq = _h(symbol="BTC", market="US", assetType="equity", currency="USD")
    cx = _h(holdingId="2", symbol="BTC", market="CRYPTO",
            assetType="crypto", currency="KRW", source="upbit")
    grouped = build_grouped_holdings([eq, cx])
    ids = sorted(g.groupId for g in grouped)
    assert ids == ["CRYPTO:crypto:KRW:BTC", "US:equity:USD:BTC"]


@pytest.mark.unit
def test_grouped_never_merges_different_currency() -> None:
    a = _h(symbol="AAA", currency="KRW")
    b = _h(holdingId="2", symbol="AAA", currency="USD", market="US")
    grouped = build_grouped_holdings([a, b])
    assert len(grouped) == 2


@pytest.mark.unit
def test_home_summary_uses_account_value_sum() -> None:
    from app.schemas.invest_home import Account, CashAmounts
    accounts = [
        Account(accountId="a1", displayName="KIS", source="kis",
                accountKind="live", includedInHome=True,
                valueKrw=10_000_000, costBasisKrw=9_000_000,
                pnlKrw=1_000_000, pnlRate=1 / 9,
                cashBalances=CashAmounts(), buyingPower=CashAmounts()),
        Account(accountId="a2", displayName="Toss", source="toss_manual",
                accountKind="manual", includedInHome=True,
                valueKrw=2_000_000, costBasisKrw=None,
                pnlKrw=None, pnlRate=None,
                cashBalances=CashAmounts(), buyingPower=CashAmounts()),
        Account(accountId="a3", displayName="Mock", source="kis_mock",
                accountKind="paper", includedInHome=False,
                valueKrw=999_999_999, costBasisKrw=None,
                pnlKrw=None, pnlRate=None,
                cashBalances=CashAmounts(), buyingPower=CashAmounts()),
    ]
    summary = build_home_summary(accounts)
    assert summary.totalValueKrw == 12_000_000
    assert summary.costBasisKrw is None  # 하나라도 null 이면 null
    assert summary.pnlKrw is None
    assert summary.pnlRate is None
    assert sorted(summary.includedSources) == ["kis", "toss_manual"]
    assert "kis_mock" in summary.excludedSources
```

- [ ] **Step 2: Run tests, verify FAIL**

```bash
uv run --group test pytest tests/test_invest_home_service.py -q
```

Expected: ImportError 또는 collection error (`build_grouped_holdings` 등 미정의).

- [ ] **Step 3: Implement service helpers**

```python
# app/services/invest_home_service.py
"""ROB-123 — read-only InvestHomeService.

이 모듈은 KIS / Upbit / manual(toss) holdings 를 read-only 로 합성한다.
mutation 경로(submit/cancel/modify/place_order/watch/order-intent/scheduler/worker)
모듈 import / 호출 금지. DB write/backfill/update/delete 금지.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from app.schemas.invest_home import (
    Account,
    AccountKindLiteral,
    AccountSourceLiteral,
    GroupedHolding,
    GroupedSourceBreakdown,
    Holding,
    HomeSummary,
    InvestHomeResponse,
    InvestHomeResponseMeta,
    InvestHomeWarning,
)

logger = logging.getLogger(__name__)

HOME_INCLUDED_SOURCES: frozenset[str] = frozenset({"kis", "upbit", "toss_manual"})

_PAPER: frozenset[str] = frozenset({"kis_mock", "kiwoom_mock", "alpaca_paper", "db_simulated"})
_MANUAL: frozenset[str] = frozenset({"toss_manual", "pension_manual", "isa_manual"})


def classify_account_kind(source: str) -> AccountKindLiteral:
    if source in _PAPER:
        return "paper"
    if source in _MANUAL:
        return "manual"
    return "live"  # kis, upbit


def _normalize_symbol(s: str) -> str:
    return s.strip().upper()


def _group_id(h: Holding) -> str:
    return f"{h.market}:{h.assetType}:{h.currency}:{_normalize_symbol(h.symbol)}"


def build_grouped_holdings(holdings: Iterable[Holding]) -> list[GroupedHolding]:
    buckets: dict[str, list[Holding]] = {}
    for h in holdings:
        buckets.setdefault(_group_id(h), []).append(h)

    out: list[GroupedHolding] = []
    for gid, items in buckets.items():
        first = items[0]
        total_qty = sum(h.quantity for h in items)
        cost_vals = [h.costBasis for h in items]
        avg_cost: float | None = None
        cost_basis: float | None = None
        if all(v is not None for v in cost_vals) and total_qty > 0:
            cost_basis = sum(v for v in cost_vals if v is not None)
            avg_cost = cost_basis / total_qty

        native_vals = [h.valueNative for h in items]
        value_native: float | None = (
            sum(v for v in native_vals if v is not None)
            if all(v is not None for v in native_vals)
            else None
        )
        krw_vals = [h.valueKrw for h in items]
        value_krw: float | None = (
            sum(v for v in krw_vals if v is not None)
            if all(v is not None for v in krw_vals)
            else None
        )
        pnl_krw: float | None = None
        pnl_rate: float | None = None
        if value_krw is not None and cost_basis is not None and cost_basis > 0:
            pnl_krw = value_krw - cost_basis
            pnl_rate = pnl_krw / cost_basis

        out.append(
            GroupedHolding(
                groupId=gid,
                symbol=_normalize_symbol(first.symbol),
                market=first.market,
                assetType=first.assetType,
                displayName=first.displayName,
                currency=first.currency,
                totalQuantity=total_qty,
                averageCost=avg_cost,
                costBasis=cost_basis,
                valueNative=value_native,
                valueKrw=value_krw,
                pnlKrw=pnl_krw,
                pnlRate=pnl_rate,
                includedSources=sorted({h.source for h in items}),
                sourceBreakdown=[
                    GroupedSourceBreakdown(
                        holdingId=h.holdingId,
                        accountId=h.accountId,
                        source=h.source,
                        quantity=h.quantity,
                        averageCost=h.averageCost,
                        costBasis=h.costBasis,
                        valueNative=h.valueNative,
                        valueKrw=h.valueKrw,
                        pnlKrw=h.pnlKrw,
                        pnlRate=h.pnlRate,
                    )
                    for h in items
                ],
            )
        )
    return out


def build_home_summary(accounts: Iterable[Account]) -> HomeSummary:
    included = [a for a in accounts if a.includedInHome]
    excluded = [a for a in accounts if not a.includedInHome]
    total = sum(a.valueKrw for a in included)
    cost_vals = [a.costBasisKrw for a in included]
    cost_basis: float | None = (
        sum(v for v in cost_vals if v is not None)
        if cost_vals and all(v is not None for v in cost_vals)
        else None
    )
    pnl_krw: float | None = None
    pnl_rate: float | None = None
    if cost_basis is not None and cost_basis > 0:
        pnl_krw = total - cost_basis
        pnl_rate = pnl_krw / cost_basis
    return HomeSummary(
        includedSources=sorted({a.source for a in included}),
        excludedSources=sorted({a.source for a in excluded}),
        totalValueKrw=total,
        costBasisKrw=cost_basis,
        pnlKrw=pnl_krw,
        pnlRate=pnl_rate,
    )


@dataclass(frozen=True)
class _SourceFetchResult:
    accounts: list[Account]
    holdings: list[Holding]
    warning: InvestHomeWarning | None = None


class InvestHomeService:
    """Read-only 합성 서비스. mutation 경로 호출 금지."""

    def __init__(self, *, kis_reader, upbit_reader, manual_reader) -> None:
        self._kis = kis_reader
        self._upbit = upbit_reader
        self._manual = manual_reader

    async def get_home(self, *, user_id: int) -> InvestHomeResponse:
        warnings: list[InvestHomeWarning] = []
        accounts: list[Account] = []
        holdings: list[Holding] = []
        for fetcher, src in (
            (self._kis.fetch, "kis"),
            (self._upbit.fetch, "upbit"),
            (self._manual.fetch, "toss_manual"),
        ):
            try:
                result: _SourceFetchResult = await fetcher(user_id=user_id)
                accounts.extend(result.accounts)
                holdings.extend(result.holdings)
                if result.warning is not None:
                    warnings.append(result.warning)
            except Exception as exc:  # 부분 실패 — 전체 API 는 살림
                logger.warning("[invest_home] %s fetch failed: %s", src, exc, exc_info=True)
                warnings.append(InvestHomeWarning(source=src, message=str(exc) or type(exc).__name__))
        return InvestHomeResponse(
            homeSummary=build_home_summary(accounts),
            accounts=accounts,
            holdings=holdings,
            groupedHoldings=build_grouped_holdings(holdings),
            meta=InvestHomeResponseMeta(warnings=warnings),
        )
```

- [ ] **Step 4: Run tests, verify PASS**

```bash
uv run --group test pytest tests/test_invest_home_service.py -q
```

Expected: 모든 unit 테스트 PASS.

- [ ] **Step 5: ruff & commit**

```bash
uv run ruff check app/services/invest_home_service.py tests/test_invest_home_service.py
uv run ruff format --check app/services/invest_home_service.py tests/test_invest_home_service.py
git add app/services/invest_home_service.py tests/test_invest_home_service.py
git commit -m "feat(rob-123): add InvestHomeService with grouping rules and home policy

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

> **Note for the implementer:** `kis_reader`, `upbit_reader`, `manual_reader` adapter 클래스들은 Task 4 에서 정의. 그들이 read-only `KISClient`/`Upbit` 클라이언트 + `ManualHoldingsService` 의 read-only 메서드만 호출하고, broker mutation 모듈은 import 하지 않는다. Adapter 신규 생성 시 인접 파일은 `app/services/invest_home_readers.py` 로 생성한다.

---

### Task 4 — Backend read-only readers + `invest_api` 라우터

**Files:**
- Create: `app/services/invest_home_readers.py`
- Create: `app/routers/invest_api.py`
- Test: `tests/test_invest_api_router.py`

- [ ] **Step 1: Write failing integration test**

```python
# tests/test_invest_api_router.py
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.invest_api import router as invest_api_router
from app.routers.invest_api import get_invest_home_service
from app.routers.dependencies import get_authenticated_user
from app.schemas.invest_home import (
    Account, CashAmounts, Holding, InvestHomeResponse,
    InvestHomeResponseMeta, InvestHomeWarning, HomeSummary,
)
from app.services.invest_home_service import build_grouped_holdings, build_home_summary


class _StubService:
    async def get_home(self, *, user_id: int) -> InvestHomeResponse:
        accounts = [
            Account(accountId="a1", displayName="KIS", source="kis",
                    accountKind="live", includedInHome=True,
                    valueKrw=10_000_000, costBasisKrw=9_000_000,
                    pnlKrw=1_000_000, pnlRate=1/9,
                    cashBalances=CashAmounts(krw=100, usd=1),
                    buyingPower=CashAmounts(krw=100, usd=1)),
            Account(accountId="a2", displayName="Mock", source="kis_mock",
                    accountKind="paper", includedInHome=False,
                    valueKrw=99, costBasisKrw=None, pnlKrw=None, pnlRate=None,
                    cashBalances=CashAmounts(), buyingPower=CashAmounts()),
        ]
        holdings = [
            Holding(holdingId="h1", accountId="a1", source="kis", accountKind="live",
                    symbol="005930", market="KR", assetType="equity",
                    displayName="삼성전자", quantity=10, averageCost=70000, costBasis=700_000,
                    currency="KRW", valueNative=720_000, valueKrw=720_000,
                    pnlKrw=20_000, pnlRate=20_000/700_000),
        ]
        return InvestHomeResponse(
            homeSummary=build_home_summary(accounts),
            accounts=accounts,
            holdings=holdings,
            groupedHoldings=build_grouped_holdings(holdings),
            meta=InvestHomeResponseMeta(warnings=[
                InvestHomeWarning(source="upbit", message="cache only"),
            ]),
        )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type("U", (), {"id": 1})()
    app.dependency_overrides[get_invest_home_service] = lambda: _StubService()
    return TestClient(app)


@pytest.mark.unit
def test_get_home_returns_200_with_schema(client: TestClient) -> None:
    r = client.get("/invest/api/home")
    assert r.status_code == 200
    body = r.json()
    assert body["homeSummary"]["totalValueKrw"] == 10_000_000  # mock 제외
    assert "kis_mock" in body["homeSummary"]["excludedSources"]
    assert any(a["source"] == "kis_mock" and a["includedInHome"] is False
               for a in body["accounts"])
    assert body["groupedHoldings"][0]["groupId"] == "KR:equity:KRW:005930"
    assert body["meta"]["warnings"][0]["source"] == "upbit"
```

- [ ] **Step 2: Run test, verify FAIL**

```bash
uv run --group test pytest tests/test_invest_api_router.py -q
```

Expected: ImportError (`app.routers.invest_api` not found).

- [ ] **Step 3: Implement readers (read-only adapters)**

```python
# app/services/invest_home_readers.py
"""ROB-123 — read-only adapters used by InvestHomeService.

각 reader 는 한 source 의 read-only 데이터만 가져온다.
broker mutation / order / watch / scheduler / worker 경로는 import / 호출 금지.
DB write / backfill 금지 — read-only 조회만 사용.
"""
from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_home import InvestHomeWarning
from app.services.invest_home_service import _SourceFetchResult

logger = logging.getLogger(__name__)


class HomeReader(Protocol):
    async def fetch(self, *, user_id: int) -> _SourceFetchResult: ...


class KISHomeReader:
    """KIS 실계좌 read-only reader. 잔고/평단/현금/매수가능만."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        # 구현 시 기존 read-only 메서드를 사용하라:
        #   - app.services.kis_holdings_service.KISHoldingsService.get_user_holdings
        #   - app.services.kis_holdings_service 의 잔고/매수가능 read-only 메서드
        # 이 reader 는 mutation (place_order / submit / cancel / modify) 모듈 import 금지.
        raise NotImplementedError("Wire KIS read-only adapter in implementation step.")


class UpbitHomeReader:
    """Upbit read-only reader. 잔고/평단/원화 매수가능만."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        # 구현 시 read-only Upbit accounts API 만 호출.
        # app.services.upbit_websocket / order / mutation 경로 import 금지.
        raise NotImplementedError("Wire Upbit read-only adapter in implementation step.")


class ManualHomeReader:
    """manual_holdings (Toss 등) read-only reader."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        # 구현 시:
        #   app.services.manual_holdings_service.ManualHoldingsService.get_holdings_by_user
        # 만 사용. broker_account.broker_type == "toss" 인 행만 toss_manual 로 매핑.
        # cost basis 가 없으면 averageCost / costBasis / pnl* 모두 None 으로 둔다.
        raise NotImplementedError("Wire manual read-only adapter in implementation step.")
```

> **Note:** `_SourceFetchResult` 는 Task 3 에서 `dataclass(frozen=True)` 로 정의된 것을 그대로 import. 위 readers 는 Task 4 에서는 인터페이스만 잡고, 실제 KIS/Upbit/manual 잔고 매핑은 같은 Task 안 step 5 에서 채운다 — 이때 mutation 모듈 import 금지를 강제.

- [ ] **Step 4: Implement `invest_api` router**

```python
# app/routers/invest_api.py
"""ROB-123 — read-only `/invest/api`.

이 라우터는 `InvestHomeService` 만 의존하고 broker / KIS / Upbit 클라이언트를 직접
import 하지 않는다. order / watch / scheduler / mutation 경로 import 금지.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.routers.dependencies import get_authenticated_user
from app.schemas.invest_home import InvestHomeResponse
from app.services.invest_home_readers import (
    KISHomeReader, ManualHomeReader, UpbitHomeReader,
)
from app.services.invest_home_service import InvestHomeService

router = APIRouter(prefix="/invest/api", tags=["invest"])


def get_invest_home_service(db: AsyncSession = Depends(get_db)) -> InvestHomeService:
    return InvestHomeService(
        kis_reader=KISHomeReader(db),
        upbit_reader=UpbitHomeReader(db),
        manual_reader=ManualHomeReader(db),
    )


@router.get("/home", response_model=InvestHomeResponse)
async def get_home(
    user=Depends(get_authenticated_user),
    service: InvestHomeService = Depends(get_invest_home_service),
) -> InvestHomeResponse:
    return await service.get_home(user_id=user.id)
```

- [ ] **Step 5: Wire real read-only data inside readers**

각 reader 의 `fetch` 안에서 (오직 read-only 메서드만):

- `KISHomeReader.fetch`: 기존 `app.services.kis_holdings_service.KISHoldingsService` 의 read-only 조회 메서드만 호출. 응답을 `Account(source="kis", accountKind="live", includedInHome=True, ...)` + `Holding(source="kis", accountKind="live", ...)` 리스트로 매핑. 부분 실패 시 `_SourceFetchResult(accounts=[], holdings=[], warning=InvestHomeWarning(source="kis", message=...))` 리턴.
- `UpbitHomeReader.fetch`: 기존 read-only Upbit accounts API (예: `/v1/accounts`) 만 호출. KRW 현금/매수가능 + 코인 holdings (`assetType="crypto"`, `market="CRYPTO"`, `currency="KRW"`) 매핑. mutation 또는 websocket 모듈 import 금지.
- `ManualHomeReader.fetch`: `ManualHoldingsService.get_holdings_by_user(user_id=...)` 만 호출. `broker_account.broker_type == "toss"` 인 행만 `source="toss_manual", accountKind="manual"` 로 매핑. `costBasis = quantity * avg_price`, `quantity*avg_price` 가 0/null 이면 None 으로. `cashBalances` / `buyingPower` 는 `CashAmounts()` (전부 null).

각 reader 가 라우터/서비스에서 분리되어 있으므로, 이 step 에서 실제 데이터 매핑을 채운다. `NotImplementedError` 를 정상 매핑 코드로 교체. mutation 모듈 (`app.services.kis_trading_service`, `app.services.brokers`, `app.services.order_service`, `app.tasks.*`, `app.services.upbit_websocket`, `app.services.execution_event` 등) 은 import 하지 않는다.

- [ ] **Step 6: Run integration test, verify PASS**

```bash
uv run --group test pytest tests/test_invest_api_router.py tests/test_invest_home_service.py -q
```

Expected: 모두 PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/invest_home_readers.py app/routers/invest_api.py tests/test_invest_api_router.py
git commit -m "feat(rob-123): add /invest/api/home read-only router and adapters

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 5 — Backend SPA shell (`app/routers/invest_app_spa.py`)

**Files:**
- Create: `app/routers/invest_app_spa.py`

- [ ] **Step 1: Implement (mirror trading_decisions_spa.py with new prefix/dist)**

```python
# app/routers/invest_app_spa.py
"""SPA shell router for /invest/app (ROB-123).

Serves the prebuilt React + Vite bundle from frontend/invest/dist/.
This module MUST NOT import any broker, watch, Redis, KIS, Upbit, or
task-queue module. See tests/test_invest_app_spa_router_safety.py.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invest/app", tags=["invest-app-spa"])

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "frontend" / "invest" / "dist"
INDEX_FILE = DIST_DIR / "index.html"
ASSETS_DIR = DIST_DIR / "assets"

_BUILD_MISSING_HTML = """\
<!doctype html>
<html><head><meta charset="utf-8"><title>/invest/app · build missing</title></head>
<body style="font:16px/1.6 ui-sans-serif,system-ui;max-width:680px;margin:4rem auto;padding:0 1rem;">
<h1>/invest/app · build missing</h1>
<p>The React bundle has not been built yet. Run:</p>
<pre><code>cd frontend/invest &amp;&amp; npm ci &amp;&amp; npm run build</code></pre>
<p>or, from the repo root: <code>make frontend-install &amp;&amp; make frontend-build</code>.</p>
</body></html>
"""


def _no_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@router.get("/assets/{asset_path:path}", include_in_schema=False)
async def serve_asset(asset_path: str) -> FileResponse:
    candidate = (ASSETS_DIR / asset_path).resolve()
    try:
        candidate.relative_to(ASSETS_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
    if not candidate.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return FileResponse(candidate)


@router.get("/", include_in_schema=False)
async def spa_index() -> Response:
    return _serve_index()


@router.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str) -> Response:
    return _serve_index()


def _serve_index() -> Response:
    if not INDEX_FILE.is_file():
        logger.warning("SPA build missing at %s; returning 503 build-missing page", INDEX_FILE)
        return _no_cache(HTMLResponse(content=_BUILD_MISSING_HTML,
                                      status_code=status.HTTP_503_SERVICE_UNAVAILABLE))
    return _no_cache(FileResponse(INDEX_FILE, media_type="text/html"))
```

- [ ] **Step 2: ruff check + import sanity**

```bash
uv run ruff check app/routers/invest_app_spa.py
uv run ruff format --check app/routers/invest_app_spa.py
uv run python -c "from app.routers.invest_app_spa import router; print(router.prefix)"
```

Expected: PASS, prefix `/invest/app`.

- [ ] **Step 3: Commit**

```bash
git add app/routers/invest_app_spa.py
git commit -m "feat(rob-123): add /invest/app SPA shell router

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 6 — Wire routers in `app/main.py`

**Files:**
- Modify: `app/main.py` (import block + include_router)

- [ ] **Step 1: Add imports (alphabetical placement near `trading_decisions_spa`)**

`app/main.py` 의 라우터 import 블록(현재 `trading_decisions_spa,` 줄 근처) 에 두 라인 추가:

```python
    invest_api,
    invest_app_spa,
```

위치는 알파벳 순서로 `health` 와 `kospi200` 사이가 자연스럽다 — 정확한 위치는 기존 import 블록을 보고 alphabetical 로 배치.

- [ ] **Step 2: Add `include_router` calls**

기존 `app.include_router(trading_decisions_spa.router)` 다음에 두 줄 추가:

```python
    app.include_router(invest_api.router)
    app.include_router(invest_app_spa.router)
```

순서 주의: `invest_api` 가 `invest_app_spa` 보다 먼저 등록되어야 `/invest/app/{full_path:path}` catch-all 이 `/invest/api/home` 을 가로채지 않는다.

- [ ] **Step 3: Boot smoke test**

```bash
uv run python -c "from app.main import create_app; app = create_app(); paths = [r.path for r in app.routes]; assert '/invest/api/home' in paths; assert '/invest/app/' in paths; print('routes ok')"
```

Expected: `routes ok`.

- [ ] **Step 4: Commit**

```bash
git add app/main.py
git commit -m "feat(rob-123): wire invest_api and invest_app_spa routers

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 7 — Backend import-safety pytests

**Files:**
- Create: `tests/test_invest_app_spa_router_safety.py`
- Create: `tests/test_invest_api_router_safety.py`

- [ ] **Step 1: SPA router safety test (mirror `trading_decisions_spa` pattern)**

```python
# tests/test_invest_app_spa_router_safety.py
"""Safety: invest_app_spa.py must not import broker/watch/redis/kis/upbit/task-queue."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FORBIDDEN_PREFIXES = [
    "app.services.kis",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.upbit",
    "app.services.upbit_websocket",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.redis_token_manager",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.tasks",
]


@pytest.mark.unit
def test_invest_app_spa_does_not_import_execution_paths() -> None:
    project_root = Path(__file__).resolve().parent.parent
    script = """
import importlib, json, sys
importlib.import_module("app.routers.invest_app_spa")
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run([sys.executable, "-c", script], cwd=project_root,
                            env=env, check=True, capture_output=True, text=True)
    loaded = set(json.loads(result.stdout))
    violations = sorted(m for m in loaded for f in FORBIDDEN_PREFIXES
                        if m == f or m.startswith(f"{f}."))
    if violations:
        pytest.fail(f"Forbidden execution-path imports: {violations}")
```

- [ ] **Step 2: API router + service safety test (mutation-only)**

```python
# tests/test_invest_api_router_safety.py
"""Safety: invest_api router and invest_home_service must not import mutation paths.

Read-only KIS/Upbit/manual *holdings* services are allowed; only mutation modules are forbidden.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FORBIDDEN_MUTATION_MODULES = [
    "app.services.brokers",
    "app.services.order_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.upbit_websocket",
    "app.services.alpaca_paper_ledger_service",
    "app.services.weekend_crypto_paper_cycle_runner",
    "app.tasks",
]

ROUTER_FORBIDDEN_DIRECT = [
    "app.services.kis",
    "app.services.upbit",
]


def _loaded(module: str, project_root: Path) -> set[str]:
    script = f"import importlib, json, sys; importlib.import_module({module!r}); print(json.dumps(sorted(sys.modules)))"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run([sys.executable, "-c", script], cwd=project_root,
                            env=env, check=True, capture_output=True, text=True)
    return set(json.loads(result.stdout))


def _violations(loaded: set[str], forbidden: list[str]) -> list[str]:
    return sorted(m for m in loaded for f in forbidden if m == f or m.startswith(f"{f}."))


@pytest.mark.unit
def test_invest_api_router_no_mutation_imports() -> None:
    root = Path(__file__).resolve().parent.parent
    loaded = _loaded("app.routers.invest_api", root)
    v = _violations(loaded, FORBIDDEN_MUTATION_MODULES + ROUTER_FORBIDDEN_DIRECT)
    if v:
        pytest.fail(f"Forbidden imports in invest_api: {v}")


@pytest.mark.unit
def test_invest_home_service_no_mutation_imports() -> None:
    root = Path(__file__).resolve().parent.parent
    loaded = _loaded("app.services.invest_home_service", root)
    v = _violations(loaded, FORBIDDEN_MUTATION_MODULES)
    if v:
        pytest.fail(f"Forbidden imports in invest_home_service: {v}")
```

- [ ] **Step 3: Run safety tests, verify PASS**

```bash
uv run --group test pytest tests/test_invest_app_spa_router_safety.py tests/test_invest_api_router_safety.py -q
```

Expected: 모두 PASS. FAIL 시 reader 들이 mutation 모듈을 끌어왔는지 확인하고 import 를 좁힌다 (예: `app.services.kis_holdings_service` 만 사용).

- [ ] **Step 4: Commit**

```bash
git add tests/test_invest_app_spa_router_safety.py tests/test_invest_api_router_safety.py
git commit -m "test(rob-123): add import-safety pytests for invest router/service

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 8 — Frontend package scaffold (`frontend/invest/`)

**Files:**
- Create: `frontend/invest/package.json`, `.gitignore`, `.nvmrc`, `index.html`, `vite.config.ts`, `vitest.config.ts`, `tsconfig.json`, `tsconfig.node.json`, `README.md`

- [ ] **Step 1: Create `package.json`**

```json
{
  "name": "@auto-trader/invest",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "engines": { "node": ">=20.19.0" },
  "scripts": {
    "dev": "vite",
    "build": "tsc -p tsconfig.json --noEmit && tsc -p tsconfig.node.json --noEmit && vite build",
    "preview": "vite preview",
    "typecheck": "tsc -p tsconfig.json --noEmit && tsc -p tsconfig.node.json --noEmit",
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "dependencies": {
    "react": "^19.2.5",
    "react-dom": "^19.2.5",
    "react-router-dom": "^7.14.2"
  },
  "devDependencies": {
    "@testing-library/jest-dom": "^6.9.1",
    "@testing-library/react": "^16.3.2",
    "@testing-library/user-event": "^14.6.1",
    "@types/node": "^25.6.0",
    "@types/react": "^19.2.14",
    "@types/react-dom": "^19.2.3",
    "@vitejs/plugin-react": "^6.0.1",
    "jsdom": "^29.1.0",
    "typescript": "^6.0.3",
    "vite": "^8.0.10",
    "vitest": "^4.1.5"
  }
}
```

- [ ] **Step 2: Create remaining boilerplate files**

```
# frontend/invest/.gitignore
node_modules/
dist/
.vite/
*.local
*.tsbuildinfo
```

```
# frontend/invest/.nvmrc
20
```

```html
<!-- frontend/invest/index.html -->
<!doctype html>
<html lang="ko">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <meta name="theme-color" content="#0F1115" />
    <title>내 투자 · auto_trader</title>
  </head>
  <body style="background:#0F1115;color:#E6E8EB;margin:0;">
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

```ts
// frontend/invest/vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/invest/app/",
  build: { outDir: "dist", assetsDir: "assets", sourcemap: true, emptyOutDir: true },
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      "/invest/api": { target: "http://localhost:8000", changeOrigin: false },
      "/portfolio/api": { target: "http://localhost:8000", changeOrigin: false },
      "/trading/api": { target: "http://localhost:8000", changeOrigin: false },
      "/api": { target: "http://localhost:8000", changeOrigin: false },
      "/auth": { target: "http://localhost:8000", changeOrigin: false },
    },
  },
});
```

```ts
// frontend/invest/vitest.config.ts
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
    include: ["src/__tests__/**/*.test.{ts,tsx}"],
  },
});
```

```json
// frontend/invest/tsconfig.json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "noImplicitOverride": true,
    "noFallthroughCasesInSwitch": true,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "isolatedModules": true,
    "verbatimModuleSyntax": true,
    "useDefineForClassFields": true,
    "forceConsistentCasingInFileNames": true,
    "resolveJsonModule": true,
    "types": ["vite/client", "vitest/globals", "node"]
  },
  "include": ["src", "src/__tests__"]
}
```

```json
// frontend/invest/tsconfig.node.json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2023"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "skipLibCheck": true,
    "strict": true,
    "allowSyntheticDefaultImports": true
  },
  "include": ["vite.config.ts", "vitest.config.ts"]
}
```

```markdown
<!-- frontend/invest/README.md -->
# @auto-trader/invest

`/invest/app` — 토스식 모바일 read-only 통합 홈 (ROB-123).

## Develop

```bash
cd frontend/invest
nvm use
npm ci
npm run dev   # http://localhost:5174 (Vite). 백엔드 :8000 필요.
```

## Build

```bash
npm run typecheck && npm test && npm run build
```

빌드 산출물은 `frontend/invest/dist/` → `app/routers/invest_app_spa.py` 가 서빙.
```

- [ ] **Step 3: Install + smoke**

```bash
cd frontend/invest && npm ci && npm run typecheck
```

Expected: 의존성 설치 완료, typecheck PASS (src 가 비어 있어도 통과).

- [ ] **Step 4: Commit**

```bash
git add frontend/invest/package.json frontend/invest/package-lock.json frontend/invest/.gitignore frontend/invest/.nvmrc frontend/invest/index.html frontend/invest/vite.config.ts frontend/invest/vitest.config.ts frontend/invest/tsconfig.json frontend/invest/tsconfig.node.json frontend/invest/README.md
git commit -m "feat(rob-123): scaffold frontend/invest Vite+React package

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 9 — Frontend types, API client, format helpers

**Files:**
- Create: `frontend/invest/src/types/invest.ts`
- Create: `frontend/invest/src/api/investHome.ts`
- Create: `frontend/invest/src/format/{currency,percent,number}.ts`
- Create: `frontend/invest/src/test/setup.ts`
- Create: `frontend/invest/src/vite-env.d.ts`
- Create: `frontend/invest/src/styles.css`

- [ ] **Step 1: types**

`frontend/invest/src/types/invest.ts` — spec §"Frontend types" 그대로 그대로 복사 (위 spec 파일과 100% 일치 필요).

- [ ] **Step 2: api client**

```ts
// frontend/invest/src/api/investHome.ts
import type { InvestHomeResponse } from "../types/invest";

export async function fetchInvestHome(signal?: AbortSignal): Promise<InvestHomeResponse> {
  const res = await fetch("/invest/api/home", { credentials: "include", signal });
  if (!res.ok) {
    throw new Error(`/invest/api/home ${res.status}`);
  }
  return (await res.json()) as InvestHomeResponse;
}
```

- [ ] **Step 3: format helpers (`null → "-"` fallback)**

```ts
// frontend/invest/src/format/number.ts
export function formatNumber(v: number | null | undefined, opts?: Intl.NumberFormatOptions): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return new Intl.NumberFormat("ko-KR", opts).format(v);
}
```

```ts
// frontend/invest/src/format/currency.ts
import { formatNumber } from "./number";

export function formatKrw(v: number | null | undefined): string {
  if (v === null || v === undefined) return "-";
  return `₩${formatNumber(v, { maximumFractionDigits: 0 })}`;
}

export function formatUsd(v: number | null | undefined): string {
  if (v === null || v === undefined) return "-";
  return `$${formatNumber(v, { maximumFractionDigits: 2 })}`;
}
```

```ts
// frontend/invest/src/format/percent.ts
export function formatPercent(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || Number.isNaN(rate)) return "-";
  const pct = rate * 100;
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(1)}%`;
}
```

- [ ] **Step 4: test setup + vite-env + styles.css**

```ts
// frontend/invest/src/test/setup.ts
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
afterEach(() => cleanup());
```

```ts
// frontend/invest/src/vite-env.d.ts
/// <reference types="vite/client" />
```

```css
/* frontend/invest/src/styles.css */
:root {
  --bg: #0F1115; --surface: #181B22; --surface-2: #1f232b;
  --text: #E6E8EB; --muted: #8A8F98;
  --gain: #FF5C5C; --loss: #3B82F6; --warn: #f6c177;
  --pill-toss: #2a1f2c; --pill-toss-fg: #d6a3ff;
  --pill-kis: #1f2a3a; --pill-kis-fg: #7eb6ff;
  --pill-up: #2a2218; --pill-up-fg: #f6c177;
  --pill-mix: #1f2a25; --pill-mix-fg: #86d2a3;
}
* { box-sizing: border-box; }
html, body, #root { margin: 0; padding: 0; background: var(--bg); color: var(--text); font-family: ui-sans-serif, system-ui, -apple-system, "Apple SD Gothic Neo", sans-serif; }
.app-shell { max-width: 420px; margin: 0 auto; min-height: 100vh; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
.gain-pos { color: var(--gain); font-weight: 600; }
.gain-neg { color: var(--loss); font-weight: 600; }
.fallback { color: var(--muted); }
.subtle { color: var(--muted); font-size: 12px; }
```

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/types frontend/invest/src/api frontend/invest/src/format frontend/invest/src/test frontend/invest/src/vite-env.d.ts frontend/invest/src/styles.css
git commit -m "feat(rob-123): add invest types, fetch client, formatters

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 10 — Frontend components (TDD)

**Files:**
- Create: `frontend/invest/src/components/{AppShell,HeroCard,AccountCardList,SourceFilterBar,HoldingRow,BottomNav}.tsx`
- Create: `frontend/invest/src/__tests__/AccountCardList.test.tsx`
- Create: `frontend/invest/src/__tests__/HoldingRow.test.tsx`

> 이 task 는 step 이 많다. 한 컴포넌트씩 TDD: test → 실패 → 구현 → 통과.

- [ ] **Step 1: `AccountCardList` test (live badge 미표시 + cashBalances/buyingPower 표시)**

```tsx
// frontend/invest/src/__tests__/AccountCardList.test.tsx
import { render, screen } from "@testing-library/react";
import { AccountCardList } from "../components/AccountCardList";
import type { Account } from "../types/invest";

const acct = (overrides: Partial<Account> = {}): Account => ({
  accountId: "a1", displayName: "KIS 실계좌", source: "kis",
  accountKind: "live", includedInHome: true,
  valueKrw: 31_420_000, costBasisKrw: 30_478_000,
  pnlKrw: 942_000, pnlRate: 0.031,
  cashBalances: { krw: 92_408, usd: 49.25 },
  buyingPower: { krw: 92_408, usd: 49.25 },
  ...overrides,
});

test("KIS card does not render a live badge", () => {
  render(<AccountCardList accounts={[acct()]} />);
  expect(screen.queryByText(/^live$/i)).toBeNull();
});

test("Upbit card does not render a live badge and shows KRW cash + buying power", () => {
  render(<AccountCardList accounts={[acct({
    accountId: "u1", source: "upbit", displayName: "Upbit",
    cashBalances: { krw: 412_000 }, buyingPower: { krw: 412_000 },
  })]} />);
  expect(screen.queryByText(/^live$/i)).toBeNull();
  expect(screen.getByText(/원화 현금/)).toBeInTheDocument();
  expect(screen.getByText(/원화 매수/)).toBeInTheDocument();
});

test("Toss manual card shows quiet 수동 badge and falls back to '-' when empty", () => {
  render(<AccountCardList accounts={[acct({
    accountId: "t1", source: "toss_manual", accountKind: "manual", displayName: "Toss",
    costBasisKrw: null, pnlKrw: null, pnlRate: null,
    cashBalances: {}, buyingPower: {},
  })]} />);
  expect(screen.getByText("수동")).toBeInTheDocument();
  expect(screen.getAllByText("-").length).toBeGreaterThan(0);
});

test("buyingPower rendering does not attach onClick handlers", () => {
  const { container } = render(<AccountCardList accounts={[acct()]} />);
  const buyingPowerLabel = screen.getByText(/원화 매수/);
  // buyingPower 행과 그 자식 어디에도 button 또는 onClick 없음
  const cell = buyingPowerLabel.closest('[data-testid="account-card"]')!;
  expect(cell.querySelector("button, [role='button']")).toBeNull();
  expect(cell.querySelectorAll("[onclick]")).toHaveLength(0);
});
```

- [ ] **Step 2: `HoldingRow` test (GroupedRow includedSources chip + null fallback)**

```tsx
// frontend/invest/src/__tests__/HoldingRow.test.tsx
import { render, screen } from "@testing-library/react";
import { GroupedRow, RawRow } from "../components/HoldingRow";
import type { GroupedHolding, Holding } from "../types/invest";

const grouped: GroupedHolding = {
  groupId: "KR:equity:KRW:005930",
  symbol: "005930", market: "KR", assetType: "equity",
  displayName: "삼성전자", currency: "KRW",
  totalQuantity: 50, averageCost: 70_400, costBasis: 3_520_000,
  valueNative: 3_580_000, valueKrw: 3_580_000,
  pnlKrw: 60_000, pnlRate: 60_000 / 3_520_000,
  includedSources: ["kis", "toss_manual"],
  sourceBreakdown: [],
};

test("GroupedRow shows includedSources chip 'KIS · Toss 수동'", () => {
  render(<GroupedRow row={grouped} />);
  expect(screen.getByText(/KIS/)).toBeInTheDocument();
  expect(screen.getByText(/Toss/)).toBeInTheDocument();
});

test("GroupedRow renders '-' when averageCost is null", () => {
  render(<GroupedRow row={{ ...grouped, averageCost: null, pnlRate: null, costBasis: null, pnlKrw: null }} />);
  expect(screen.getAllByText("-").length).toBeGreaterThan(0);
});

test("RawRow renders single source pill", () => {
  const raw: Holding = {
    holdingId: "h1", accountId: "a1", source: "toss_manual",
    accountKind: "manual", symbol: "TSLA", market: "US",
    assetType: "equity", displayName: "Tesla", quantity: 4,
    averageCost: 234, costBasis: 936, currency: "USD",
    valueNative: 924, valueKrw: 1_244_000, pnlKrw: -16_000, pnlRate: -16_000 / 1_260_000,
  };
  render(<RawRow row={raw} />);
  expect(screen.getByText(/Toss/)).toBeInTheDocument();
});
```

- [ ] **Step 3: Run tests, verify FAIL**

```bash
cd frontend/invest && npm test -- --run
```

Expected: 컴포넌트가 없어 모듈 resolve 실패.

- [ ] **Step 4: Implement components**

```tsx
// frontend/invest/src/components/AppShell.tsx
import type { ReactNode } from "react";

export function AppShell({ children }: { children: ReactNode }) {
  return <div className="app-shell">{children}</div>;
}
```

```tsx
// frontend/invest/src/components/HeroCard.tsx
import type { HomeSummary } from "../types/invest";
import { formatKrw } from "../format/currency";
import { formatPercent } from "../format/percent";

export function HeroCard({ summary }: { summary: HomeSummary }) {
  const gainCls = (summary.pnlRate ?? 0) >= 0 ? "gain-pos" : "gain-neg";
  return (
    <div data-testid="hero-card" style={{ background: "var(--surface)", borderRadius: 14, padding: 16, textAlign: "center" }}>
      <div className="subtle">내 투자 ({summary.includedSources.join(" · ").toUpperCase()})</div>
      <div style={{ fontSize: 30, fontWeight: 700, marginTop: 4 }}>{formatKrw(summary.totalValueKrw)}</div>
      <div className={gainCls} style={{ fontSize: 13, marginTop: 2 }}>
        {summary.pnlKrw === null ? "-" : formatKrw(summary.pnlKrw)} · {formatPercent(summary.pnlRate)}
      </div>
      <div className="subtle" style={{ marginTop: 4 }}>
        원금 {summary.costBasisKrw === null ? "정보 부족" : formatKrw(summary.costBasisKrw)} 기준
      </div>
    </div>
  );
}
```

```tsx
// frontend/invest/src/components/AccountCardList.tsx
import type { Account } from "../types/invest";
import { formatKrw, formatUsd } from "../format/currency";
import { formatPercent } from "../format/percent";

function gainClass(rate: number | null): string {
  if (rate === null) return "fallback";
  return rate >= 0 ? "gain-pos" : "gain-neg";
}

function AccountCard({ a }: { a: Account }) {
  const isToss = a.source === "toss_manual";
  return (
    <div data-testid="account-card" style={{
      minWidth: 220, background: "var(--surface)", borderRadius: 14, padding: 12, flex: "0 0 auto",
    }}>
      <div style={{ fontWeight: 600, fontSize: 12 }}>
        {a.displayName}
        {isToss && (
          <span style={{
            marginLeft: 6, padding: "1px 6px", borderRadius: 5,
            background: "#1c1e24", color: "var(--muted)", fontSize: 9,
          }}>수동</span>
        )}
      </div>
      <div style={{ fontWeight: 700, fontSize: 18, marginTop: 4 }}>{formatKrw(a.valueKrw)}</div>
      <div className={gainClass(a.pnlRate)} style={{ fontSize: 11 }}>
        {a.pnlKrw === null ? "-" : formatKrw(a.pnlKrw)} · {formatPercent(a.pnlRate)}
        {a.costBasisKrw === null && <span className="subtle"> · 원금 정보 부족</span>}
      </div>
      <div style={{
        marginTop: 10, paddingTop: 8, borderTop: "1px solid var(--surface-2)",
        display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 10px",
      }}>
        {a.source === "kis" && (
          <>
            <Cell k="원화 현금" v={formatKrw(a.cashBalances.krw ?? null)} />
            <Cell k="달러 현금" v={formatUsd(a.cashBalances.usd ?? null)} />
            <Cell k="원화 매수" v={formatKrw(a.buyingPower.krw ?? null)} />
            <Cell k="달러 매수" v={formatUsd(a.buyingPower.usd ?? null)} />
          </>
        )}
        {a.source === "upbit" && (
          <>
            <Cell k="원화 현금" v={formatKrw(a.cashBalances.krw ?? null)} />
            <Cell k="원화 매수" v={formatKrw(a.buyingPower.krw ?? null)} />
          </>
        )}
        {isToss && (
          <>
            <Cell k="원화 현금" v={a.cashBalances.krw === undefined ? "-" : formatKrw(a.cashBalances.krw)} />
            <Cell k="원화 매수" v={a.buyingPower.krw === undefined ? "-" : formatKrw(a.buyingPower.krw)} />
          </>
        )}
      </div>
    </div>
  );
}

function Cell({ k, v }: { k: string; v: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between" }}>
      <span style={{ color: "var(--muted)", fontSize: 10 }}>{k}</span>
      <span style={{ fontSize: 11, textAlign: "right" }}>{v}</span>
    </div>
  );
}

export function AccountCardList({ accounts }: { accounts: Account[] }) {
  return (
    <div>
      <div className="subtle" style={{ padding: "0 4px 4px" }}>계좌</div>
      <div style={{ display: "flex", gap: 8, overflowX: "auto" }}>
        {accounts.map((a) => <AccountCard key={a.accountId} a={a} />)}
      </div>
    </div>
  );
}
```

```tsx
// frontend/invest/src/components/SourceFilterBar.tsx
import type { AccountSource } from "../types/invest";

export type ActiveSource = AccountSource | "all";

const LABELS: Record<ActiveSource, string> = {
  all: "전체", kis: "KIS", upbit: "Upbit", toss_manual: "Toss 수동",
  pension_manual: "퇴직연금", isa_manual: "ISA",
  kis_mock: "KIS 모의", kiwoom_mock: "키움 모의",
  alpaca_paper: "Alpaca", db_simulated: "DB 시뮬",
};

export function SourceFilterBar({
  sources, active, onChange,
}: {
  sources: ActiveSource[];
  active: ActiveSource;
  onChange: (s: ActiveSource) => void;
}) {
  return (
    <div style={{ display: "flex", gap: 6, padding: "0 4px", flexWrap: "wrap" }}>
      {sources.map((s) => {
        const on = s === active;
        return (
          <button key={s} type="button" onClick={() => onChange(s)} style={{
            padding: "4px 10px", borderRadius: 999,
            background: on ? "var(--text)" : "var(--surface)",
            color: on ? "var(--bg)" : "var(--text)",
            border: "none", fontSize: 11, cursor: "pointer",
          }}>{LABELS[s]}</button>
        );
      })}
    </div>
  );
}
```

```tsx
// frontend/invest/src/components/HoldingRow.tsx
import type { AccountSource, GroupedHolding, Holding, Market } from "../types/invest";
import { formatKrw, formatUsd } from "../format/currency";
import { formatNumber } from "../format/number";
import { formatPercent } from "../format/percent";

const SRC_LABEL: Record<AccountSource, string> = {
  kis: "KIS", upbit: "Upbit", toss_manual: "Toss 수동",
  pension_manual: "퇴직연금", isa_manual: "ISA",
  kis_mock: "KIS 모의", kiwoom_mock: "키움 모의",
  alpaca_paper: "Alpaca", db_simulated: "DB 시뮬",
};

function valueFmt(currency: string, v: number | null): string {
  if (v === null) return "-";
  return currency === "USD" ? formatUsd(v) : formatKrw(v);
}

function gainClass(rate: number | null): string {
  if (rate === null) return "fallback";
  return rate >= 0 ? "gain-pos" : "gain-neg";
}

export function GroupedRow({ row }: { row: GroupedHolding }) {
  const sources = row.includedSources.map((s) => SRC_LABEL[s]).join(" · ");
  return (
    <div data-testid="grouped-row" style={rowStyle}>
      <div>
        <div style={{ fontWeight: 600, fontSize: 12 }}>
          {row.displayName} <SourceChip text={sources} />
        </div>
        <div className="subtle" style={{ fontSize: 10 }}>
          {row.symbol} · 합산 {formatNumber(row.totalQuantity)}{unitFor(row.market)} · 평단 {row.averageCost === null ? "-" : valueFmt(row.currency, row.averageCost)}
        </div>
      </div>
      <div style={{ textAlign: "right", fontSize: 11 }}>
        <div>{valueFmt(row.currency, row.valueNative ?? row.valueKrw)}</div>
        <div className={gainClass(row.pnlRate)}>{formatPercent(row.pnlRate)}</div>
      </div>
    </div>
  );
}

export function RawRow({ row }: { row: Holding }) {
  return (
    <div data-testid="raw-row" style={rowStyle}>
      <div>
        <div style={{ fontWeight: 600, fontSize: 12 }}>
          {row.displayName} <SourceChip text={SRC_LABEL[row.source]} />
        </div>
        <div className="subtle" style={{ fontSize: 10 }}>
          {row.symbol} · {formatNumber(row.quantity)}{unitFor(row.market)} · 평단 {row.averageCost === null ? "-" : valueFmt(row.currency, row.averageCost)}
        </div>
      </div>
      <div style={{ textAlign: "right", fontSize: 11 }}>
        <div>{valueFmt(row.currency, row.valueNative ?? row.valueKrw)}</div>
        <div className={gainClass(row.pnlRate)}>{formatPercent(row.pnlRate)}</div>
      </div>
    </div>
  );
}

const rowStyle: React.CSSProperties = {
  display: "flex", justifyContent: "space-between", alignItems: "center",
  padding: "8px 4px", borderBottom: "1px solid var(--surface-2)",
};

function SourceChip({ text }: { text: string }) {
  return (
    <span style={{
      display: "inline-block", padding: "1px 6px", marginLeft: 4,
      background: "var(--pill-mix)", color: "var(--pill-mix-fg)",
      borderRadius: 6, fontSize: 9, verticalAlign: "middle",
    }}>{text}</span>
  );
}

function unitFor(market: Market): string {
  if (market === "CRYPTO") return "";
  return market === "KR" ? "주" : "주";
}
```

```tsx
// frontend/invest/src/components/BottomNav.tsx
const TABS = ["증권", "관심", "발견", "피드"];

export function BottomNav() {
  return (
    <div style={{
      display: "flex", justifyContent: "space-around",
      paddingTop: 8, borderTop: "1px solid var(--surface-2)",
      color: "var(--muted)", fontSize: 10,
      position: "sticky", bottom: 0, background: "var(--bg)",
    }}>
      {TABS.map((label, i) => (
        <button
          key={label}
          type="button"
          onClick={() => alert("준비 중")}
          style={{ background: "none", border: "none", color: i === 0 ? "var(--text)" : "var(--muted)", cursor: "pointer", padding: 8, fontSize: 10 }}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 5: Run tests, verify PASS**

```bash
cd frontend/invest && npm test -- --run
```

Expected: 모든 컴포넌트 unit 테스트 PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/invest/src/components frontend/invest/src/__tests__/AccountCardList.test.tsx frontend/invest/src/__tests__/HoldingRow.test.tsx
git commit -m "feat(rob-123): add HomePage components with TDD coverage

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 11 — Frontend pages, routes, app entry

**Files:**
- Create: `frontend/invest/src/hooks/useInvestHome.ts`
- Create: `frontend/invest/src/pages/HomePage.tsx`
- Create: `frontend/invest/src/pages/PaperPlaceholderPage.tsx`
- Create: `frontend/invest/src/routes.tsx`
- Create: `frontend/invest/src/App.tsx`
- Create: `frontend/invest/src/main.tsx`
- Create: `frontend/invest/src/__tests__/HomePage.test.tsx`

- [ ] **Step 1: Write failing HomePage test (filter switch + warnings)**

```tsx
// frontend/invest/src/__tests__/HomePage.test.tsx
import { render, screen, fireEvent } from "@testing-library/react";
import { HomePage } from "../pages/HomePage";
import type { InvestHomeResponse } from "../types/invest";

const data: InvestHomeResponse = {
  homeSummary: {
    includedSources: ["kis", "toss_manual"], excludedSources: ["kis_mock"],
    totalValueKrw: 12_000_000, costBasisKrw: 9_000_000,
    pnlKrw: 3_000_000, pnlRate: 1 / 3,
  },
  accounts: [],
  holdings: [{
    holdingId: "h1", accountId: "a1", source: "toss_manual", accountKind: "manual",
    symbol: "TSLA", market: "US", assetType: "equity", displayName: "Tesla",
    quantity: 4, averageCost: 234, costBasis: 936, currency: "USD",
    valueNative: 924, valueKrw: 1_244_000, pnlKrw: -16_000, pnlRate: -0.012,
  }],
  groupedHoldings: [{
    groupId: "US:equity:USD:TSLA", symbol: "TSLA", market: "US", assetType: "equity",
    displayName: "Tesla", currency: "USD", totalQuantity: 4,
    averageCost: 234, costBasis: 936, valueNative: 924, valueKrw: 1_244_000,
    pnlKrw: -16_000, pnlRate: -0.012,
    includedSources: ["toss_manual"], sourceBreakdown: [],
  }],
  meta: { warnings: [{ source: "upbit", message: "cache only" }] },
};

test("renders meta.warnings as a single line", () => {
  render(<HomePage state={{ status: "ready", data }} reload={() => {}} />);
  expect(screen.getByText(/cache only/)).toBeInTheDocument();
});

test("activeSource toggles between groupedHoldings and raw holdings", () => {
  render(<HomePage state={{ status: "ready", data }} reload={() => {}} />);
  expect(screen.getByTestId("grouped-row")).toBeInTheDocument();
  fireEvent.click(screen.getByText("Toss 수동"));
  expect(screen.queryByTestId("grouped-row")).toBeNull();
  expect(screen.getByTestId("raw-row")).toBeInTheDocument();
});
```

- [ ] **Step 2: Implement hook + pages + routes + entry**

```ts
// frontend/invest/src/hooks/useInvestHome.ts
import { useEffect, useState } from "react";
import { fetchInvestHome } from "../api/investHome";
import type { InvestHomeResponse } from "../types/invest";

export type InvestHomeState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: InvestHomeResponse };

export function useInvestHome() {
  const [state, setState] = useState<InvestHomeState>({ status: "loading" });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchInvestHome(controller.signal)
      .then((data) => setState({ status: "ready", data }))
      .catch((e) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: e?.message ?? "failed" });
      });
    return () => controller.abort();
  }, [tick]);
  return { state, reload: () => setTick((t) => t + 1) };
}
```

```tsx
// frontend/invest/src/pages/HomePage.tsx
import { useState } from "react";
import { AppShell } from "../components/AppShell";
import { HeroCard } from "../components/HeroCard";
import { AccountCardList } from "../components/AccountCardList";
import { SourceFilterBar, type ActiveSource } from "../components/SourceFilterBar";
import { GroupedRow, RawRow } from "../components/HoldingRow";
import { BottomNav } from "../components/BottomNav";
import { useInvestHome, type InvestHomeState } from "../hooks/useInvestHome";

const FILTER_SOURCES: ActiveSource[] = ["all", "kis", "upbit", "toss_manual"];

export function HomePage(props?: { state?: InvestHomeState; reload?: () => void }) {
  const live = useInvestHome();
  // Hooks must run in the same order on every render — declare before any early return.
  const [active, setActive] = useState<ActiveSource>("all");

  const state = props?.state ?? live.state;
  const reload = props?.reload ?? live.reload;

  if (state.status === "loading") {
    return <AppShell><div className="subtle">불러오는 중…</div></AppShell>;
  }
  if (state.status === "error") {
    return (
      <AppShell>
        <div>잠시 후 다시 시도해 주세요.</div>
        <button type="button" onClick={reload}>재시도</button>
        <div className="subtle">{state.message}</div>
      </AppShell>
    );
  }

  const { data } = state;
  const warnings = data.meta?.warnings ?? [];

  return (
    <AppShell>
      <HeroCard summary={data.homeSummary} />
      <AccountCardList accounts={data.accounts} />
      <SourceFilterBar sources={FILTER_SOURCES} active={active} onChange={setActive} />
      {warnings.length > 0 && (
        <div role="alert" style={{
          padding: 8, color: "var(--warn)", fontSize: 10,
          background: "rgba(246,193,119,0.08)",
          border: "1px solid rgba(246,193,119,0.27)", borderRadius: 10,
        }}>
          {warnings.map((w) => `⚠ ${w.source}: ${w.message}`).join(" · ")}
        </div>
      )}
      <div style={{ flex: 1, overflowY: "auto" }}>
        {active === "all"
          ? data.groupedHoldings.map((g) => <GroupedRow key={g.groupId} row={g} />)
          : data.holdings.filter((h) => h.source === active).map((h) => <RawRow key={h.holdingId} row={h} />)}
      </div>
      <div style={{
        padding: 8, color: "var(--muted)", fontSize: 10,
        border: "1px dashed var(--surface-2)", borderRadius: 10,
      }}>
        합산 제외: 퇴직연금 · ISA · 모의투자 (별도 화면)
      </div>
      <BottomNav />
    </AppShell>
  );
}
```

```tsx
// frontend/invest/src/pages/PaperPlaceholderPage.tsx
import { useParams } from "react-router-dom";
import { AppShell } from "../components/AppShell";

export function PaperPlaceholderPage() {
  const { variant } = useParams();
  return (
    <AppShell>
      <h2>모의투자 ({variant ?? "전체"})</h2>
      <div className="subtle">이번 MVP 에서는 준비 중입니다.</div>
    </AppShell>
  );
}
```

```tsx
// frontend/invest/src/routes.tsx
import { createBrowserRouter, Navigate } from "react-router-dom";
import { HomePage } from "./pages/HomePage";
import { PaperPlaceholderPage } from "./pages/PaperPlaceholderPage";

export const router = createBrowserRouter([
  { path: "/invest/app/", element: <HomePage /> },
  { path: "/invest/app/paper", element: <PaperPlaceholderPage /> },
  { path: "/invest/app/paper/:variant", element: <PaperPlaceholderPage /> },
  { path: "/invest/app/*", element: <Navigate to="/invest/app/" replace /> },
]);
```

```tsx
// frontend/invest/src/App.tsx
import { RouterProvider } from "react-router-dom";
import { router } from "./routes";

export default function App() {
  return <RouterProvider router={router} />;
}
```

```tsx
// frontend/invest/src/main.tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("#root element not found");
}
createRoot(container).render(<StrictMode><App /></StrictMode>);
```

- [ ] **Step 3: Run tests + typecheck + build**

```bash
cd frontend/invest && npm run typecheck && npm test -- --run && npm run build
```

Expected: 모두 PASS. `dist/index.html` 생성.

- [ ] **Step 4: Commit**

```bash
git add frontend/invest/src/hooks frontend/invest/src/pages frontend/invest/src/routes.tsx frontend/invest/src/App.tsx frontend/invest/src/main.tsx frontend/invest/src/__tests__/HomePage.test.tsx
git commit -m "feat(rob-123): add HomePage, paper placeholder, routes

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 12 — Verification sweep + `/trading/decisions` 회귀 가드

**Files:**
- 없음 (verification only)

- [ ] **Step 1: Backend full verification**

```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run --group test pytest tests/test_invest_home_service.py tests/test_invest_api_router.py tests/test_invest_app_spa_router_safety.py tests/test_invest_api_router_safety.py -q
```

Expected: 모두 PASS. ruff 위반/실패 시 `uv run ruff format app/ tests/` 후 재확인.

- [ ] **Step 2: `/trading/decisions/*` 회귀 없음 확인**

```bash
uv run --group test pytest tests/test_trading_decisions_spa_router_safety.py -q
cd frontend/trading-decision && npm run typecheck && npm test -- --run && npm run build && cd -
```

Expected: 모두 PASS. (Task 1 baseline 과 동일.)

- [ ] **Step 3: Frontend invest 최종 검증**

```bash
cd frontend/invest && npm run typecheck && npm test -- --run && npm run build && cd -
git diff --check
```

Expected: typecheck PASS · 모든 테스트 PASS · `frontend/invest/dist/index.html` 생성 · whitespace 위반 없음.

- [ ] **Step 4: Boot smoke**

```bash
uv run python -c "from app.main import create_app; app = create_app(); paths={r.path for r in app.routes}; assert '/invest/api/home' in paths; assert '/invest/app/' in paths; print('routes ok')"
```

Expected: `routes ok`.

- [ ] **Step 5: 수동 확인 (옵션)**

```bash
uv run uvicorn app.main:app --port 8000 &  # 별도 터미널
# 브라우저: http://localhost:8000/invest/app/   → HomePage
# 브라우저: http://localhost:8000/invest/api/home → 200 + JSON (인증된 세션)
# 브라우저: http://localhost:8000/trading/decisions/  → 변함 없이 작동
```

- [ ] **Step 6: 마지막 cleanup commit (있다면)**

```bash
git status --short
# 변경 사항 없으면 skip. 있으면 적절히 commit.
```

---

## Verification Commands (요약)

**Frontend:**
```bash
cd frontend/invest
npm run typecheck
npm test -- --run
npm run build
```

**Backend:**
```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run --group test pytest \
  tests/test_invest_home_service.py \
  tests/test_invest_api_router.py \
  tests/test_invest_app_spa_router_safety.py \
  tests/test_invest_api_router_safety.py \
  tests/test_trading_decisions_spa_router_safety.py \
  -q
git diff --check
```

---

## Handoff Checklist

- [ ] Branch name: `linear-mcp-rob-123`
- [ ] PR URL: (생성 후 기록)
- [ ] `/invest/app` 로컬 확인: `uv run uvicorn app.main:app --port 8000` → `http://localhost:8000/invest/app/`
- [ ] 재사용한 API: `/invest/api/home` 신규(이번 PR), 기존 read-only 서비스 재사용 (`KISHoldingsService`, `ManualHoldingsService`, Upbit accounts read-only)
- [ ] 신규 API contract: `GET /invest/api/home → InvestHomeResponse` (spec §"API contract" 참고)
- [ ] 실행한 검증 명령: 위 Verification Commands 모두
- [ ] 남은 디자인/데이터 gap: `cashBalances/buyingPower` 의 USD 매수가능은 KIS 실계좌의 환전 정책 + 주문가능 잔고 read-only 응답 매핑이 후속 issue 에서 보강될 수 있음 (현재 MVP 는 fallback `-` 허용)
- [ ] `/invest/web` 을 위한 예약 구조: `frontend/invest/` 가 모바일 패키지. 향후 `/invest/web` 은 sibling 패키지(`frontend/invest-web/`) 또는 같은 패키지 안 별도 entry 로 추가 가능. 백엔드 `app/routers/invest_api.py` 는 둘 다 공유.
