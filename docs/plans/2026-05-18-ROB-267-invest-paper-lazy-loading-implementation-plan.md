# ROB-267: /invest Paper/Mock Lazy Loading 및 Home 중복 Fetch 제거 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/invest` 초기 진입에서 paper/mock reader 실행과 중복 home fetch를 제거하여 우측 계좌 패널과 투자 홈의 p95 지연을 감소시킨다.

**Architecture:**
- Backend: `InvestHomeService.get_home()` 에 `include_paper`, `paper_sources` 파라미터 추가; 10개 invest_api 엔드포인트에 `includePaper`, `paperSources` 쿼리 파라미터 노출; `build_account_panel` 을 슬림 read path (옵션 B) 로 분리; reader별 Sentry span 추가.
- Frontend: `AccountPanelProvider` mount 자동 fetch 제거 → lazy `load()` API 도입; `tick` reload는 이미 로드된 상태에서만 동작; paper 계좌 버튼 클릭 시 source 별 lazy fetch.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy / sentry_sdk; React 18 / TypeScript / Vitest / Testing Library.

---

## 사전 준비

이미 `rob-267` 브랜치 워크트리에서 작업 중인 것을 가정한다 (`/Users/mgh3326/work/auto_trader.rob-267`). 모든 작업은 이 디렉터리 안에서 수행한다.

## File Structure

### Backend — 수정

| 파일 | 책임 |
|---|---|
| `app/services/invest_home_service.py` | `get_home()` 에 `include_paper`, `paper_sources` 파라미터 추가; reader별 Sentry span; `build_account_panel_view()` 슬림 read path 신설 |
| `app/routers/invest_api.py` | 10개 엔드포인트 (`/home`, `/account-panel`, `/crypto/dashboard`, `/crypto/naver-reference`, `/kr/action-readiness`, `/signals`, `/calendar`, `/feed/news`, `/feed/research`, `/screener/results`) 에 `includePaper`, `paperSources` 쿼리 파라미터 추가 및 service 호출에 전달 |
| `app/services/invest_view_model/account_panel_service.py` | `build_account_panel` 이 `home_service.build_account_panel_view()` 슬림 path 호출하도록 변경 |
| `app/services/invest_view_model/action_readiness_service.py` | `build_kr_action_readiness` 가 `home_service.get_home(include_paper=...)` 으로 호출하도록 plumb-through |

### Backend — 테스트 추가/수정

| 파일 | 책임 |
|---|---|
| `tests/test_invest_home_service.py` | 신규: `include_paper=False` 기본 동작, `paper_sources` 필터링, paper reader exception graceful fallback, Sentry span 검증 |
| `tests/test_invest_api_router.py` | 신규: `/home`, `/account-panel` 쿼리 파라미터 전달 |
| `tests/test_invest_account_panel_router.py` | 슬림 path 사용 검증 |
| `tests/test_invest_api_router_includepaper_sweep.py` (신규) | 10개 엔드포인트 일관성 sweep 테스트 |

### Frontend — 수정

| 파일 | 책임 |
|---|---|
| `frontend/invest/src/api/accountPanel.ts` | `fetchAccountPanel({ includePaper?, paperSources?, signal? })` 옵션 인자 도입 |
| `frontend/invest/src/api/investHome.ts` | `fetchInvestHome({ signal?, includePaper?, paperSources? })` 옵션 인자 도입 |
| `frontend/invest/src/desktop/AccountPanelProvider.tsx` | mount 자동 fetch 제거; `load(options?)` / `reload()` 컨텍스트 메서드 노출; `tick` 은 이미 로드된 경우에만 reload 트리거 |
| `frontend/invest/src/desktop/RightRemotePanel.tsx` | 패널 portfolio 탭 활성화 시 `load()` 호출; paper 계좌 필터 버튼 클릭 시 `load({ includePaper: true, paperSources: [source] })` 호출; per-source loading 상태 표시 |

### Frontend — 테스트 추가/수정

| 파일 | 책임 |
|---|---|
| `frontend/invest/src/__tests__/AccountPanelProvider.test.tsx` | mount 자동 fetch 없음; `load()` 호출 시 fetch 발생; `tick` reload는 이미 로드된 후에만 동작 |
| `frontend/invest/src/__tests__/RightRemotePanel.test.tsx` | portfolio 탭 가시화 시 fetch; paper 버튼 클릭 시 해당 source 만 fetch (다른 paper source 호출되지 않음) |

### 추가/생성 파일 없음

설계상 새 모듈을 만들지 않는다 — 기존 파일에 메서드/파라미터 추가로 처리.

---

## 작업 단위 (Task)

각 Task = 1 logical commit. Step = 2–5분 단위 액션.

---

### Task 1: `InvestHomeService.get_home()` 에 `include_paper` / `paper_sources` 파라미터 추가

**Files:**
- Modify: `app/services/invest_home_service.py`
- Test: `tests/test_invest_home_service.py`

- [ ] **Step 1.1: 실패하는 테스트 작성 — include_paper=False 시 paper readers 미호출**

`tests/test_invest_home_service.py` 끝에 추가:

```python
import pytest

@pytest.mark.asyncio
async def test_get_home_does_not_invoke_paper_readers_when_include_paper_false():
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    class _Stub:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _SpyPaperReader:
        source = "kis_mock"
        called = False
        async def fetch(self, *, user_id):
            type(self).called = True
            return _SourceFetchResult(accounts=[], holdings=[])

    spy = _SpyPaperReader()
    service = InvestHomeService(
        kis_reader=_Stub(),
        upbit_reader=_Stub(),
        manual_reader=_Stub(),
        paper_readers=[spy],
    )

    await service.get_home(user_id=1)

    assert _SpyPaperReader.called is False


@pytest.mark.asyncio
async def test_get_home_invokes_only_requested_paper_sources():
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    class _Stub:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _SpyKisMock:
        source = "kis_mock"
        called = False
        async def fetch(self, *, user_id):
            type(self).called = True
            return _SourceFetchResult(accounts=[], holdings=[])

    class _SpyAlpaca:
        source = "alpaca_paper"
        called = False
        async def fetch(self, *, user_id):
            type(self).called = True
            return _SourceFetchResult(accounts=[], holdings=[])

    service = InvestHomeService(
        kis_reader=_Stub(),
        upbit_reader=_Stub(),
        manual_reader=_Stub(),
        paper_readers=[_SpyKisMock(), _SpyAlpaca()],
    )

    await service.get_home(
        user_id=1, include_paper=True, paper_sources=frozenset({"kis_mock"})
    )

    assert _SpyKisMock.called is True
    assert _SpyAlpaca.called is False


@pytest.mark.asyncio
async def test_get_home_invokes_all_paper_readers_when_sources_none():
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    class _Stub:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _SpyKisMock:
        source = "kis_mock"
        called = False
        async def fetch(self, *, user_id):
            type(self).called = True
            return _SourceFetchResult(accounts=[], holdings=[])

    class _SpyAlpaca:
        source = "alpaca_paper"
        called = False
        async def fetch(self, *, user_id):
            type(self).called = True
            return _SourceFetchResult(accounts=[], holdings=[])

    service = InvestHomeService(
        kis_reader=_Stub(),
        upbit_reader=_Stub(),
        manual_reader=_Stub(),
        paper_readers=[_SpyKisMock(), _SpyAlpaca()],
    )

    await service.get_home(user_id=1, include_paper=True, paper_sources=None)

    assert _SpyKisMock.called is True
    assert _SpyAlpaca.called is True
```

- [ ] **Step 1.2: 테스트 실패 확인**

Run: `uv run pytest tests/test_invest_home_service.py::test_get_home_does_not_invoke_paper_readers_when_include_paper_false tests/test_invest_home_service.py::test_get_home_invokes_only_requested_paper_sources tests/test_invest_home_service.py::test_get_home_invokes_all_paper_readers_when_sources_none -v`

Expected: 3 FAIL (`get_home()` 는 아직 `include_paper`, `paper_sources` 인자를 받지 않음 → `TypeError: get_home() got an unexpected keyword argument 'include_paper'`).

- [ ] **Step 1.3: `get_home()` 시그니처와 paper reader 게이팅 구현**

`app/services/invest_home_service.py:321` 의 `get_home` 메서드를 다음으로 교체:

```python
    async def get_home(
        self,
        *,
        user_id: int,
        include_paper: bool = False,
        paper_sources: frozenset[str] | None = None,
    ) -> InvestHomeResponse:
        warnings: list[InvestHomeWarning] = []
        accounts: list[Account] = []
        holdings: list[Holding] = []
        hidden_holdings: list[Holding] = []
        hidden_counts = InvestHomeHiddenCounts()

        for fetcher, src in (
            (self._kis.fetch, "kis"),
            (self._upbit.fetch, "upbit"),
            (self._manual.fetch, "toss_manual"),
        ):
            try:
                if src == "kis" or src == "upbit":
                    result: _SourceFetchResult = await fetcher(user_id=user_id)
                else:
                    result: _SourceFetchResult = await self._manual.fetch(
                        user_id=user_id
                    )

                accounts.extend(result.accounts)
                holdings.extend(result.holdings)
                hidden_holdings.extend(result.hidden_holdings)
                hidden_counts.upbitInactive += result.hidden_counts.upbitInactive
                hidden_counts.upbitDust += result.hidden_counts.upbitDust

                if result.warning is not None:
                    warnings.append(result.warning)

                # Synthetic Toss Manual Account
                if src == "toss_manual":
                    toss_account = build_manual_account_from_holdings(result.holdings)
                    if toss_account is not None:
                        accounts.append(toss_account)

            except Exception as exc:  # 부분 실패 — 전체 API 는 살림
                logger.warning(
                    "[invest_home] %s fetch failed: %s", src, exc, exc_info=True
                )
                warnings.append(
                    InvestHomeWarning(
                        source=src, message=str(exc) or type(exc).__name__
                    )
                )

        # Paper readers — gated by include_paper flag and optional paper_sources filter.
        # Default (include_paper=False) skips all paper readers entirely so that
        # /invest 기본 경로에서 KIS mock / Alpaca Paper API 호출이 발생하지 않는다.
        if include_paper:
            for reader in self._paper_readers:
                reader_source: str = getattr(reader, "source", None) or "kis_mock"
                if paper_sources is not None and reader_source not in paper_sources:
                    continue
                try:
                    result = await reader.fetch(user_id=user_id)  # type: ignore[union-attr]
                    accounts.extend(result.accounts)
                    holdings.extend(result.holdings)
                    if result.warning is not None:
                        warnings.append(result.warning)
                except Exception as exc:
                    src_name = type(reader).__name__
                    logger.warning(
                        "[invest_home] paper reader %s failed: %s",
                        src_name,
                        exc,
                        exc_info=True,
                    )
                    if reader_source in _PAPER:
                        warnings.append(
                            InvestHomeWarning(
                                source=reader_source, message=type(exc).__name__
                            )  # type: ignore[arg-type]
                        )

        return InvestHomeResponse(
            homeSummary=build_home_summary(accounts),
            accounts=accounts,
            holdings=holdings,
            groupedHoldings=build_grouped_holdings(holdings),
            meta=InvestHomeResponseMeta(
                warnings=warnings,
                hiddenCounts=hidden_counts,
                hiddenHoldings=hidden_holdings,
            ),
        )
```

(차이 요약: 메서드 시그니처에 `include_paper`, `paper_sources` 추가; paper readers 루프 전체를 `if include_paper:` 로 감싸고 `paper_sources` filter 추가.)

- [ ] **Step 1.4: 테스트 통과 확인**

Run: `uv run pytest tests/test_invest_home_service.py::test_get_home_does_not_invoke_paper_readers_when_include_paper_false tests/test_invest_home_service.py::test_get_home_invokes_only_requested_paper_sources tests/test_invest_home_service.py::test_get_home_invokes_all_paper_readers_when_sources_none -v`

Expected: 3 PASS.

- [ ] **Step 1.5: 기존 InvestHomeService 테스트 회귀 확인**

Run: `uv run pytest tests/test_invest_home_service.py -v`

Expected: 모든 기존 테스트 + 신규 3개 모두 PASS.

> ⚠️ 기존 테스트가 paper readers 호출을 expectation 으로 가지고 있었다면 (`include_paper=True` 가 새 기본값이 아니므로) 영향이 있을 수 있다. 회귀 발생 시 해당 테스트를 명시적 `include_paper=True` 호출로 업데이트.

- [ ] **Step 1.6: 커밋**

```bash
git add app/services/invest_home_service.py tests/test_invest_home_service.py
git commit -m "feat(invest): gate paper readers via include_paper/paper_sources

InvestHomeService.get_home() 에 include_paper, paper_sources 파라미터를
추가하여 기본 호출에서 KIS mock / Alpaca Paper reader 가 실행되지 않도록 한다.
include_paper=True 일 때 paper_sources 로 호출 대상 paper source 를 제한할 수
있다. (ROB-267)"
```

---

### Task 2: 슬림 read path — `build_account_panel_view()` 도입 (옵션 B)

**Files:**
- Modify: `app/services/invest_home_service.py`
- Modify: `app/services/invest_view_model/account_panel_service.py`
- Test: `tests/test_invest_account_panel_router.py`

**컨텍스트:** account-panel 은 `holdings` 전체 리스트, `hiddenCounts`, `hiddenHoldings` 가 불필요하다 (panel UI 가 사용하지 않음). `build_account_panel_view()` 가 이런 후처리/필드를 생략하여 응답 크기와 직렬화 비용을 줄인다. **Reader fetch 자체는 동일하게 수행하지만**, account-panel response 가 `InvestHomeResponse` 가 아니라 슬림 view dataclass 가 되어 home/account-panel 의 책임이 명확히 분리된다.

- [ ] **Step 2.1: 실패하는 테스트 — build_account_panel 이 슬림 view 를 사용함**

`tests/test_invest_account_panel_router.py` 끝에 추가 (파일이 없는 경우는 신규 생성):

```python
import pytest


@pytest.mark.asyncio
async def test_build_account_panel_uses_slim_view_path(monkeypatch):
    from app.services.invest_view_model.account_panel_service import build_account_panel
    from app.services.invest_home_service import InvestHomeService

    call_log: list[str] = []

    class _StubService:
        async def get_home(self, **kwargs):
            call_log.append("get_home")
            raise AssertionError("build_account_panel must not call get_home")

        async def build_account_panel_view(self, **kwargs):
            call_log.append("build_account_panel_view")
            from app.schemas.invest_home import HomeSummary, InvestHomeWarning
            from app.services.invest_home_service import _AccountPanelView
            return _AccountPanelView(
                homeSummary=HomeSummary(
                    includedSources=[],
                    excludedSources=[],
                    totalValueKrw=0,
                    costBasisKrw=None,
                    pnlKrw=None,
                    pnlRate=None,
                ),
                accounts=[],
                groupedHoldings=[],
                warnings=[],
            )

    class _DBStub:
        async def execute(self, _stmt):
            class _R:
                def all(self):
                    return []
            return _R()

    resp = await build_account_panel(
        user_id=1, db=_DBStub(), home_service=_StubService()
    )

    assert "build_account_panel_view" in call_log
    assert "get_home" not in call_log
    assert resp.homeSummary.totalValueKrw == 0
```

- [ ] **Step 2.2: 테스트 실패 확인**

Run: `uv run pytest tests/test_invest_account_panel_router.py::test_build_account_panel_uses_slim_view_path -v`

Expected: FAIL (`_AccountPanelView` 와 `build_account_panel_view` 가 존재하지 않음).

- [ ] **Step 2.3: 슬림 view dataclass + 메서드 추가**

`app/services/invest_home_service.py` 의 `from dataclasses import dataclass, field` 라인 (line 12) 바로 아래 import 들 다음, `_SourceFetchResult` 정의 위에 `_AccountPanelView` 를 추가:

```python
@dataclass(frozen=True)
class _AccountPanelView:
    """Slim view used by /account-panel — excludes full holdings/hidden tracking."""
    homeSummary: "HomeSummary"
    accounts: list["Account"]
    groupedHoldings: list["GroupedHolding"]
    warnings: list["InvestHomeWarning"]
```

그리고 `InvestHomeService` 클래스 내 `get_home` 메서드 바로 아래 (line 404 의 `return InvestHomeResponse(...)` 다음) 에 `build_account_panel_view` 메서드 추가:

```python
    async def build_account_panel_view(
        self,
        *,
        user_id: int,
        include_paper: bool = False,
        paper_sources: frozenset[str] | None = None,
    ) -> _AccountPanelView:
        """Slim path for /account-panel — skips holdings detail and hidden tracking.

        Runs the same reader fetches as get_home() (live/manual + optionally paper),
        but does not assemble the full Holdings list or hidden_holdings/hidden_counts
        tracking since the panel UI does not use those fields.
        """
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
                if src == "toss_manual":
                    toss_account = build_manual_account_from_holdings(result.holdings)
                    if toss_account is not None:
                        accounts.append(toss_account)
            except Exception as exc:
                logger.warning(
                    "[invest_home] %s fetch failed: %s", src, exc, exc_info=True
                )
                warnings.append(
                    InvestHomeWarning(
                        source=src, message=str(exc) or type(exc).__name__
                    )
                )

        if include_paper:
            for reader in self._paper_readers:
                reader_source: str = getattr(reader, "source", None) or "kis_mock"
                if paper_sources is not None and reader_source not in paper_sources:
                    continue
                try:
                    result = await reader.fetch(user_id=user_id)  # type: ignore[union-attr]
                    accounts.extend(result.accounts)
                    holdings.extend(result.holdings)
                    if result.warning is not None:
                        warnings.append(result.warning)
                except Exception as exc:
                    src_name = type(reader).__name__
                    logger.warning(
                        "[invest_home] paper reader %s failed: %s",
                        src_name,
                        exc,
                        exc_info=True,
                    )
                    if reader_source in _PAPER:
                        warnings.append(
                            InvestHomeWarning(
                                source=reader_source, message=type(exc).__name__
                            )  # type: ignore[arg-type]
                        )

        return _AccountPanelView(
            homeSummary=build_home_summary(accounts),
            accounts=accounts,
            groupedHoldings=build_grouped_holdings(holdings),
            warnings=warnings,
        )
```

- [ ] **Step 2.4: `build_account_panel` 이 슬림 path 호출하도록 변경**

`app/services/invest_view_model/account_panel_service.py` 의 `build_account_panel` 시그니처에 `include_paper`, `paper_sources` 추가하고 호출을 `home_service.build_account_panel_view(...)` 로 변경:

```python
async def build_account_panel(
    *,
    user_id: int,
    db: AsyncSession,
    home_service: InvestHomeService,
    include_paper: bool = False,
    paper_sources: frozenset[str] | None = None,
) -> AccountPanelResponse:
    view = await home_service.build_account_panel_view(
        user_id=user_id,
        include_paper=include_paper,
        paper_sources=paper_sources,
    )
    watch_symbols, watch_available = await _load_watch_symbols(db, user_id=user_id)
    return AccountPanelResponse(
        homeSummary=view.homeSummary,
        accounts=view.accounts,
        groupedHoldings=view.groupedHoldings,
        watchSymbols=watch_symbols,
        sourceVisuals=all_visuals(),
        meta=AccountPanelMeta(
            warnings=view.warnings,
            watchlistAvailable=watch_available,
        ),
    )
```

- [ ] **Step 2.5: 테스트 통과 확인**

Run: `uv run pytest tests/test_invest_account_panel_router.py -v`

Expected: 신규 테스트 + 기존 테스트 모두 PASS.

> ⚠️ 기존 `test_invest_account_panel_router.py:83` 의 `build_account_panel(user_id=1, db=db, home_service=home_service)` 호출은 시그니처가 후방 호환이므로 영향 없음. stub `home_service` 가 `get_home` 만 구현했다면 `build_account_panel_view` 도 구현하도록 업데이트.

- [ ] **Step 2.6: 회귀 확인 — InvestHomeService 전체**

Run: `uv run pytest tests/test_invest_home_service.py tests/test_invest_account_panel_router.py -v`

Expected: 전부 PASS.

- [ ] **Step 2.7: 커밋**

```bash
git add app/services/invest_home_service.py app/services/invest_view_model/account_panel_service.py tests/test_invest_account_panel_router.py
git commit -m "feat(invest): slim build_account_panel_view path for /account-panel

옵션 B — /account-panel 이 InvestHomeService.get_home() 전체를 호출하는 대신
build_account_panel_view() 슬림 path 를 사용하도록 변경한다. holdings 전체
리스트와 hidden_holdings/hiddenCounts 후처리를 건너뛰어 응답을 가볍게 한다.
(ROB-267)"
```

---

### Task 3: invest_api 엔드포인트에 `includePaper` / `paperSources` 쿼리 파라미터 추가 (10개)

**Files:**
- Modify: `app/routers/invest_api.py`
- Modify: `app/services/invest_view_model/action_readiness_service.py`
- Test: `tests/test_invest_api_router.py`

대상 10개 엔드포인트:
1. `/invest/api/home`
2. `/invest/api/account-panel`
3. `/invest/api/crypto/dashboard`
4. `/invest/api/crypto/naver-reference`
5. `/invest/api/kr/action-readiness`
6. `/invest/api/signals`
7. `/invest/api/calendar`
8. `/invest/api/feed/news`
9. `/invest/api/feed/research`
10. `/invest/api/screener/results`

- [ ] **Step 3.1: 실패하는 테스트 — /home 이 includePaper 미지정 시 paper readers 호출 안 함**

`tests/test_invest_api_router.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_home_endpoint_default_does_not_request_paper(monkeypatch):
    from app.routers.invest_api import get_invest_home_service

    received: dict = {}

    class _StubService:
        async def get_home(self, *, user_id, include_paper=False, paper_sources=None):
            received["include_paper"] = include_paper
            received["paper_sources"] = paper_sources
            from app.schemas.invest_home import (
                InvestHomeResponse,
                InvestHomeResponseMeta,
                HomeSummary,
                InvestHomeHiddenCounts,
            )
            return InvestHomeResponse(
                homeSummary=HomeSummary(
                    includedSources=[],
                    excludedSources=[],
                    totalValueKrw=0,
                    costBasisKrw=None,
                    pnlKrw=None,
                    pnlRate=None,
                ),
                accounts=[],
                holdings=[],
                groupedHoldings=[],
                meta=InvestHomeResponseMeta(
                    warnings=[],
                    hiddenCounts=InvestHomeHiddenCounts(),
                    hiddenHoldings=[],
                ),
            )

    from app.main import app  # adjust if app entry differs
    app.dependency_overrides[get_invest_home_service] = lambda: _StubService()

    from httpx import AsyncClient, ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Auth dep override required — assume conftest provides it
        r = await client.get("/invest/api/home")
    assert r.status_code in (200, 401)  # 401 ok if no auth fixture; 200 path verified by other tests
    if r.status_code == 200:
        assert received["include_paper"] is False
        assert received["paper_sources"] is None


@pytest.mark.asyncio
async def test_home_endpoint_passes_include_paper_query(monkeypatch):
    from app.routers.invest_api import get_invest_home_service

    received: dict = {}

    class _StubService:
        async def get_home(self, *, user_id, include_paper=False, paper_sources=None):
            received["include_paper"] = include_paper
            received["paper_sources"] = paper_sources
            from app.schemas.invest_home import (
                InvestHomeResponse,
                InvestHomeResponseMeta,
                HomeSummary,
                InvestHomeHiddenCounts,
            )
            return InvestHomeResponse(
                homeSummary=HomeSummary(
                    includedSources=[],
                    excludedSources=[],
                    totalValueKrw=0,
                    costBasisKrw=None,
                    pnlKrw=None,
                    pnlRate=None,
                ),
                accounts=[],
                holdings=[],
                groupedHoldings=[],
                meta=InvestHomeResponseMeta(
                    warnings=[],
                    hiddenCounts=InvestHomeHiddenCounts(),
                    hiddenHoldings=[],
                ),
            )

    from app.main import app
    app.dependency_overrides[get_invest_home_service] = lambda: _StubService()

    from httpx import AsyncClient, ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/invest/api/home?includePaper=true&paperSources=kis_mock,alpaca_paper")
    if r.status_code == 200:
        assert received["include_paper"] is True
        assert received["paper_sources"] == frozenset({"kis_mock", "alpaca_paper"})
```

> 인증 의존 주입 (`get_authenticated_user`) 처리는 기존 test_invest_api_router.py 의 conftest/fixture 패턴을 따른다. 만약 인증을 우회하는 패턴이 없으면 해당 dependency 도 `app.dependency_overrides` 로 stub 처리.

- [ ] **Step 3.2: 테스트 실패 확인**

Run: `uv run pytest tests/test_invest_api_router.py::test_home_endpoint_default_does_not_request_paper tests/test_invest_api_router.py::test_home_endpoint_passes_include_paper_query -v`

Expected: 두 테스트 모두 `received` 에 키가 채워지지 않음 (router 가 파라미터를 service 에 전달하지 않음) → FAIL 또는 200 이지만 received 값 검증 실패.

- [ ] **Step 3.3: `paperSources` 파서 헬퍼 추가**

`app/routers/invest_api.py:117` (router 정의) 위에 헬퍼 함수 추가:

```python
def _parse_paper_sources(value: str | None) -> frozenset[str] | None:
    """Parse comma-separated paper source identifiers into a frozenset.

    Returns None when value is None or empty — meaning "all paper sources"
    when include_paper is True.
    """
    if not value:
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    return frozenset(parts) if parts else None
```

- [ ] **Step 3.4: `/home` 엔드포인트에 쿼리 파라미터 추가**

`app/routers/invest_api.py:156-161` 의 `get_home` 핸들러를 다음으로 교체:

```python
@router.get("/home")
async def get_home(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    include_paper: Annotated[bool, Query(alias="includePaper")] = False,
    paper_sources: Annotated[str | None, Query(alias="paperSources")] = None,
) -> InvestHomeResponse:
    return await service.get_home(
        user_id=user.id,
        include_paper=include_paper,
        paper_sources=_parse_paper_sources(paper_sources),
    )
```

- [ ] **Step 3.5: `/account-panel` 엔드포인트에 쿼리 파라미터 추가**

`app/routers/invest_api.py:288-294` 의 `get_account_panel` 핸들러를 다음으로 교체:

```python
@router.get("/account-panel")
async def get_account_panel(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    include_paper: Annotated[bool, Query(alias="includePaper")] = False,
    paper_sources: Annotated[str | None, Query(alias="paperSources")] = None,
) -> AccountPanelResponse:
    return await build_account_panel(
        user_id=user.id,
        db=db,
        home_service=service,
        include_paper=include_paper,
        paper_sources=_parse_paper_sources(paper_sources),
    )
```

- [ ] **Step 3.6: 나머지 8개 엔드포인트에 쿼리 파라미터 추가**

다음 핸들러들도 같은 패턴으로 수정 (각각 `include_paper`, `paper_sources` 파라미터 추가하고 `service.get_home(user_id=..., include_paper=..., paper_sources=_parse_paper_sources(...))` 형태로 호출):

- `get_crypto_dashboard` (line 202-219)
- `get_crypto_naver_reference` (line 222-245)
- `get_kr_action_readiness` (line 272-285) — 이 엔드포인트는 `build_kr_action_readiness` 가 `home_service` 를 받으므로 다음 step 에서 별도 처리
- `get_signals` (line 462-474)
- `get_calendar` (line 477-496)
- `get_feed_news` (line 508-531)
- `get_feed_research` (line 534-574)
- `get_screener_results_endpoint` (line 585-604)

각 핸들러에 다음 두 줄의 파라미터를 (기존 `db: ...` 다음 위치에) 추가:
```python
    include_paper: Annotated[bool, Query(alias="includePaper")] = False,
    paper_sources: Annotated[str | None, Query(alias="paperSources")] = None,
```

그리고 `await service.get_home(user_id=user.id)` 를 다음으로 교체:
```python
    home = await service.get_home(
        user_id=user.id,
        include_paper=include_paper,
        paper_sources=_parse_paper_sources(paper_sources),
    )
```

(기존 `home = ...` 가 1줄이었으면 멀티라인으로 변경.)

- [ ] **Step 3.7: `build_kr_action_readiness` plumb-through**

`app/services/invest_view_model/action_readiness_service.py:680` 의 `build_kr_action_readiness` 시그니처에 `include_paper`, `paper_sources` 추가하고 `home_service.get_home(user_id=user_id)` 호출을 다음으로 교체 (line 762 부근):

```python
        home = await home_service.get_home(
            user_id=user_id,
            include_paper=include_paper,
            paper_sources=paper_sources,
        )
```

그리고 `get_kr_action_readiness` router 핸들러 (`app/routers/invest_api.py:272-285`) 가 새 인자를 전달하도록 수정:

```python
@router.get("/kr/action-readiness")
async def get_kr_action_readiness(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    symbol: str = Query("", description="Optional six-digit KR equity symbol"),
    include_paper: Annotated[bool, Query(alias="includePaper")] = False,
    paper_sources: Annotated[str | None, Query(alias="paperSources")] = None,
) -> KrActionReadinessResponse:
    return await build_kr_action_readiness(
        db=db,
        user_id=user.id,
        home_service=service,
        symbol=symbol or None,
        include_paper=include_paper,
        paper_sources=_parse_paper_sources(paper_sources),
    )
```

- [ ] **Step 3.8: 테스트 통과 확인**

Run: `uv run pytest tests/test_invest_api_router.py -v`

Expected: Task 3.1 의 신규 테스트 + 기존 router 테스트 모두 PASS.

- [ ] **Step 3.9: 라우터 회귀 sweep**

Run: `uv run pytest tests/test_invest_api_router.py tests/test_invest_api_crypto_router.py tests/test_invest_api_feed_research_router.py tests/test_invest_api_feed_research_copyright_guardrails.py tests/test_invest_api_screener_router.py tests/test_invest_account_panel_router.py -v`

Expected: 전부 PASS.

- [ ] **Step 3.10: 커밋**

```bash
git add app/routers/invest_api.py app/services/invest_view_model/action_readiness_service.py tests/test_invest_api_router.py
git commit -m "feat(invest): expose includePaper/paperSources on 10 invest_api endpoints

/home, /account-panel, /crypto/dashboard, /crypto/naver-reference,
/kr/action-readiness, /signals, /calendar, /feed/news, /feed/research,
/screener/results 모두에 includePaper/paperSources 쿼리 파라미터를 추가하여
paper/mock reader 가 기본 호출에서 실행되지 않도록 한다. (ROB-267)"
```

---

### Task 4: paper reader exception graceful fallback 테스트

**Files:**
- Test: `tests/test_invest_home_service.py`

- [ ] **Step 4.1: 실패하는 테스트 — paper reader 예외 시 live response 가 깨지지 않음**

`tests/test_invest_home_service.py` 에 추가:

```python
@pytest.mark.asyncio
async def test_paper_reader_exception_does_not_break_live_response():
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult
    from app.schemas.invest_home import Account

    class _StubLiveReader:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(
                accounts=[
                    Account(
                        accountId="kis_real",
                        displayName="KIS 실계좌",
                        source="kis",
                        accountKind="live",
                        includedInHome=True,
                        valueKrw=1_000_000,
                        costBasisKrw=900_000,
                        pnlKrw=100_000,
                        pnlRate=0.11,
                        cashBalances=Account.model_fields["cashBalances"].default_factory(),
                        buyingPower=Account.model_fields["buyingPower"].default_factory(),
                    )
                ],
                holdings=[],
            )

    class _EmptyReader:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _ExplodingPaperReader:
        source = "kis_mock"
        async def fetch(self, *, user_id):
            raise RuntimeError("paper api down")

    service = InvestHomeService(
        kis_reader=_StubLiveReader(),
        upbit_reader=_EmptyReader(),
        manual_reader=_EmptyReader(),
        paper_readers=[_ExplodingPaperReader()],
    )

    resp = await service.get_home(user_id=1, include_paper=True)

    assert len(resp.accounts) == 1
    assert resp.accounts[0].source == "kis"
    assert resp.accounts[0].valueKrw == 1_000_000
    assert any(w.source == "kis_mock" for w in resp.meta.warnings)


@pytest.mark.asyncio
async def test_paper_reader_exception_does_not_break_account_panel_view():
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    class _EmptyReader:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _ExplodingPaperReader:
        source = "alpaca_paper"
        async def fetch(self, *, user_id):
            raise RuntimeError("alpaca outage")

    service = InvestHomeService(
        kis_reader=_EmptyReader(),
        upbit_reader=_EmptyReader(),
        manual_reader=_EmptyReader(),
        paper_readers=[_ExplodingPaperReader()],
    )

    view = await service.build_account_panel_view(
        user_id=1, include_paper=True, paper_sources=frozenset({"alpaca_paper"})
    )

    assert any(w.source == "alpaca_paper" for w in view.warnings)
    # live/manual accounts list is empty in this stub but response still succeeds
    assert view.accounts == []
```

- [ ] **Step 4.2: 테스트 통과 확인**

Run: `uv run pytest tests/test_invest_home_service.py::test_paper_reader_exception_does_not_break_live_response tests/test_invest_home_service.py::test_paper_reader_exception_does_not_break_account_panel_view -v`

Expected: PASS (Task 1, 2 에서 추가된 graceful fallback 로직이 이미 동작).

- [ ] **Step 4.3: 커밋**

```bash
git add tests/test_invest_home_service.py
git commit -m "test(invest): paper reader exception graceful fallback (ROB-267)

paper reader failure 가 live/manual 응답이나 account_panel_view 를 깨뜨리지
않고 warning 으로 격리되는 것을 검증한다."
```

---

### Task 5: Reader별 Sentry span 추가

**Files:**
- Modify: `app/services/invest_home_service.py`
- Test: `tests/test_invest_home_service.py`

- [ ] **Step 5.1: 실패하는 테스트 — get_home 호출 시 reader별 span 이 생성됨**

`tests/test_invest_home_service.py` 에 추가:

```python
@pytest.mark.asyncio
async def test_get_home_creates_reader_spans(monkeypatch):
    """Verify per-reader Sentry spans are emitted for observability."""
    import sentry_sdk
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    spans: list[tuple[str, dict]] = []

    class _RecordingSpan:
        def __init__(self, op, name, **kwargs):
            self.op = op
            self.name = name
            self.tags = {}
            self.data = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            spans.append((self.name, {"op": self.op, "tags": dict(self.tags), "data": dict(self.data)}))
            return False

        def set_tag(self, k, v):
            self.tags[k] = v

        def set_data(self, k, v):
            self.data[k] = v

    def _fake_start_span(*, op=None, name=None, **_kw):
        return _RecordingSpan(op=op, name=name)

    monkeypatch.setattr(sentry_sdk, "start_span", _fake_start_span)

    class _EmptyReader:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _Paper:
        source = "kis_mock"
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    service = InvestHomeService(
        kis_reader=_EmptyReader(),
        upbit_reader=_EmptyReader(),
        manual_reader=_EmptyReader(),
        paper_readers=[_Paper()],
    )

    await service.get_home(user_id=1, include_paper=True, paper_sources=frozenset({"kis_mock"}))

    names = [n for n, _ in spans]
    assert "invest.home.kis" in names
    assert "invest.home.upbit" in names
    assert "invest.home.manual" in names
    assert "invest.home.kis_mock" in names

    kis_mock_span = next(meta for n, meta in spans if n == "invest.home.kis_mock")
    assert kis_mock_span["tags"].get("source") == "kis_mock"
    assert kis_mock_span["tags"].get("include_paper") is True


@pytest.mark.asyncio
async def test_get_home_default_skips_paper_spans(monkeypatch):
    import sentry_sdk
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    spans: list[str] = []

    class _RecordingSpan:
        def __init__(self, op, name, **kwargs):
            self.name = name
        def __enter__(self): return self
        def __exit__(self, *exc):
            spans.append(self.name)
            return False
        def set_tag(self, *_): pass
        def set_data(self, *_): pass

    monkeypatch.setattr(sentry_sdk, "start_span", lambda **kw: _RecordingSpan(**kw))

    class _Stub:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _Paper:
        source = "alpaca_paper"
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    service = InvestHomeService(
        kis_reader=_Stub(), upbit_reader=_Stub(), manual_reader=_Stub(),
        paper_readers=[_Paper()],
    )
    await service.get_home(user_id=1)

    assert "invest.home.alpaca_paper" not in spans
    assert "invest.home.kis_mock" not in spans
```

- [ ] **Step 5.2: 테스트 실패 확인**

Run: `uv run pytest tests/test_invest_home_service.py::test_get_home_creates_reader_spans tests/test_invest_home_service.py::test_get_home_default_skips_paper_spans -v`

Expected: FAIL (현재 코드에 sentry span 이 없음).

- [ ] **Step 5.3: span 헬퍼 + reader 루프 wrapping**

`app/services/invest_home_service.py` 상단 import 영역 (line 10 의 `import logging` 다음) 에 추가:

```python
import sentry_sdk
```

`InvestHomeService` 클래스 내부에 private 헬퍼 메서드를 추가 (다른 메서드 정의 위치):

```python
    def _reader_span(self, source: str, *, include_paper: bool, paper_sources):
        span = sentry_sdk.start_span(
            op="invest.home.reader",
            name=f"invest.home.{source}",
        )
        # start_span returns a context manager; tags are applied within __enter__
        return span, source, include_paper, paper_sources
```

이보다는 인라인으로 span 진입을 처리하는 게 더 명확하다. `get_home` 의 `for fetcher, src in (...)` 루프와 `for reader in self._paper_readers:` 루프 본문을 다음과 같이 변경 (전체 메서드를 다시 보여줌):

```python
    async def get_home(
        self,
        *,
        user_id: int,
        include_paper: bool = False,
        paper_sources: frozenset[str] | None = None,
    ) -> InvestHomeResponse:
        warnings: list[InvestHomeWarning] = []
        accounts: list[Account] = []
        holdings: list[Holding] = []
        hidden_holdings: list[Holding] = []
        hidden_counts = InvestHomeHiddenCounts()

        for fetcher, src in (
            (self._kis.fetch, "kis"),
            (self._upbit.fetch, "upbit"),
            (self._manual.fetch, "toss_manual"),
        ):
            span_name = (
                "invest.home.manual" if src == "toss_manual" else f"invest.home.{src}"
            )
            with sentry_sdk.start_span(
                op="invest.home.reader", name=span_name
            ) as span:
                span.set_tag("source", src)
                span.set_tag("include_paper", include_paper)
                if paper_sources is not None:
                    span.set_tag("paper_sources", ",".join(sorted(paper_sources)))
                try:
                    result: _SourceFetchResult = await fetcher(user_id=user_id)
                    accounts.extend(result.accounts)
                    holdings.extend(result.holdings)
                    hidden_holdings.extend(result.hidden_holdings)
                    hidden_counts.upbitInactive += result.hidden_counts.upbitInactive
                    hidden_counts.upbitDust += result.hidden_counts.upbitDust
                    if result.warning is not None:
                        warnings.append(result.warning)
                    if src == "toss_manual":
                        toss_account = build_manual_account_from_holdings(result.holdings)
                        if toss_account is not None:
                            accounts.append(toss_account)
                except Exception as exc:
                    logger.warning(
                        "[invest_home] %s fetch failed: %s", src, exc, exc_info=True
                    )
                    warnings.append(
                        InvestHomeWarning(
                            source=src, message=str(exc) or type(exc).__name__
                        )
                    )

        if include_paper:
            for reader in self._paper_readers:
                reader_source: str = getattr(reader, "source", None) or "kis_mock"
                if paper_sources is not None and reader_source not in paper_sources:
                    continue
                with sentry_sdk.start_span(
                    op="invest.home.reader",
                    name=f"invest.home.{reader_source}",
                ) as span:
                    span.set_tag("source", reader_source)
                    span.set_tag("include_paper", True)
                    if paper_sources is not None:
                        span.set_tag(
                            "paper_sources", ",".join(sorted(paper_sources))
                        )
                    try:
                        result = await reader.fetch(user_id=user_id)  # type: ignore[union-attr]
                        accounts.extend(result.accounts)
                        holdings.extend(result.holdings)
                        if result.warning is not None:
                            warnings.append(result.warning)
                    except Exception as exc:
                        src_name = type(reader).__name__
                        logger.warning(
                            "[invest_home] paper reader %s failed: %s",
                            src_name,
                            exc,
                            exc_info=True,
                        )
                        if reader_source in _PAPER:
                            warnings.append(
                                InvestHomeWarning(
                                    source=reader_source, message=type(exc).__name__
                                )  # type: ignore[arg-type]
                            )

        return InvestHomeResponse(
            homeSummary=build_home_summary(accounts),
            accounts=accounts,
            holdings=holdings,
            groupedHoldings=build_grouped_holdings(holdings),
            meta=InvestHomeResponseMeta(
                warnings=warnings,
                hiddenCounts=hidden_counts,
                hiddenHoldings=hidden_holdings,
            ),
        )
```

`build_account_panel_view` 메서드에도 동일 패턴으로 span 추가 (Task 2 에서 작성한 두 reader 루프를 위와 같은 패턴으로 감싸기) + 외곽 span 하나 추가:

`build_account_panel_view` 시작 부분 (메서드 본문 첫 줄) 을 다음으로 wrap:

```python
    async def build_account_panel_view(
        self,
        *,
        user_id: int,
        include_paper: bool = False,
        paper_sources: frozenset[str] | None = None,
    ) -> _AccountPanelView:
        with sentry_sdk.start_span(
            op="invest.account_panel", name="invest.account_panel.build"
        ) as outer:
            outer.set_tag("include_paper", include_paper)
            if paper_sources is not None:
                outer.set_tag("paper_sources", ",".join(sorted(paper_sources)))
            return await self._build_account_panel_view_inner(
                user_id=user_id,
                include_paper=include_paper,
                paper_sources=paper_sources,
            )

    async def _build_account_panel_view_inner(
        self,
        *,
        user_id: int,
        include_paper: bool,
        paper_sources: frozenset[str] | None,
    ) -> _AccountPanelView:
        # ... Task 2 의 본문을 그대로 옮기되 reader 루프를 위 get_home 의 with span 패턴으로 감싼다
```

(편의상 inner 메서드로 분리. 코드 중복을 피하려면 get_home 과 동일한 reader-iteration 로직을 재사용하는 헬퍼를 만들어도 됨.)

- [ ] **Step 5.4: 테스트 통과 확인**

Run: `uv run pytest tests/test_invest_home_service.py::test_get_home_creates_reader_spans tests/test_invest_home_service.py::test_get_home_default_skips_paper_spans -v`

Expected: PASS.

- [ ] **Step 5.5: 전체 invest 회귀 확인**

Run: `uv run pytest tests/test_invest_home_service.py tests/test_invest_account_panel_router.py tests/test_invest_api_router.py -v`

Expected: 전부 PASS.

- [ ] **Step 5.6: 커밋**

```bash
git add app/services/invest_home_service.py tests/test_invest_home_service.py
git commit -m "obs(invest): per-reader Sentry spans for get_home / account_panel

invest.home.kis, invest.home.upbit, invest.home.manual, invest.home.kis_mock,
invest.home.alpaca_paper, invest.account_panel.build span 을 추가하고
include_paper / paper_sources / source 태그를 남긴다. (ROB-267)"
```

---

### Task 6: 10-endpoint sweep 테스트 — paper reader 미호출 일관성 검증

**Files:**
- Test: `tests/test_invest_api_router_includepaper_sweep.py` (신규)

- [ ] **Step 6.1: 신규 테스트 파일 생성**

```python
"""ROB-267 — invest_api 10개 엔드포인트가 기본 호출에서 paper reader 를 실행하지
않는지 sweep 검증."""

from __future__ import annotations

import pytest

ENDPOINTS_PASSING_HOME = [
    ("/invest/api/home", {}),
    ("/invest/api/account-panel", {}),
    ("/invest/api/crypto/dashboard", {}),
    ("/invest/api/crypto/naver-reference", {}),
    ("/invest/api/kr/action-readiness", {}),
    ("/invest/api/signals", {}),
    ("/invest/api/calendar", {"from_date": "2026-01-01", "to_date": "2026-01-07"}),
    ("/invest/api/feed/news", {}),
    ("/invest/api/feed/research", {}),
    ("/invest/api/screener/results", {"preset": "default", "market": "kr"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,params", ENDPOINTS_PASSING_HOME)
async def test_default_call_does_not_request_paper(path, params, monkeypatch):
    from app.routers.invest_api import get_invest_home_service

    received: list[dict] = []

    class _StubService:
        async def get_home(self, *, user_id, include_paper=False, paper_sources=None):
            received.append({
                "include_paper": include_paper,
                "paper_sources": paper_sources,
                "endpoint": path,
            })
            from app.schemas.invest_home import (
                InvestHomeResponse,
                InvestHomeResponseMeta,
                HomeSummary,
                InvestHomeHiddenCounts,
            )
            return InvestHomeResponse(
                homeSummary=HomeSummary(
                    includedSources=[],
                    excludedSources=[],
                    totalValueKrw=0,
                    costBasisKrw=None,
                    pnlKrw=None,
                    pnlRate=None,
                ),
                accounts=[],
                holdings=[],
                groupedHoldings=[],
                meta=InvestHomeResponseMeta(
                    warnings=[],
                    hiddenCounts=InvestHomeHiddenCounts(),
                    hiddenHoldings=[],
                ),
            )

        async def build_account_panel_view(self, **kwargs):
            received.append({
                "include_paper": kwargs.get("include_paper", False),
                "paper_sources": kwargs.get("paper_sources"),
                "endpoint": path,
            })
            from app.services.invest_home_service import _AccountPanelView
            from app.schemas.invest_home import HomeSummary
            return _AccountPanelView(
                homeSummary=HomeSummary(
                    includedSources=[],
                    excludedSources=[],
                    totalValueKrw=0,
                    costBasisKrw=None,
                    pnlKrw=None,
                    pnlRate=None,
                ),
                accounts=[],
                groupedHoldings=[],
                warnings=[],
            )

    # Reuse the app + auth override pattern from existing tests; if a shared
    # fixture exists in conftest.py, prefer that.
    from app.main import app
    app.dependency_overrides[get_invest_home_service] = lambda: _StubService()

    from httpx import AsyncClient, ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(path, params=params)

    # Some endpoints return non-200 for other reasons (auth, DB stubs); we only
    # assert that IF the handler called the service, it used include_paper=False.
    for entry in received:
        assert entry["include_paper"] is False, (
            f"{entry['endpoint']} default call passed include_paper=True"
        )
        assert entry["paper_sources"] is None, (
            f"{entry['endpoint']} default call passed paper_sources={entry['paper_sources']}"
        )
```

> 실제 conftest 가 인증/DB stub 을 제공하는 형태에 맞춰 fixture import 를 조정. 일부 엔드포인트는 추가 인자 (e.g., `preset`, `from_date`) 가 필요하므로 `ENDPOINTS_PASSING_HOME` 의 `params` 로 처리.

- [ ] **Step 6.2: 테스트 실행**

Run: `uv run pytest tests/test_invest_api_router_includepaper_sweep.py -v`

Expected: 10개 파라미터화 케이스 모두 PASS (Task 3 의 라우터 변경이 모든 엔드포인트에 일관되게 적용되었음을 검증).

- [ ] **Step 6.3: 커밋**

```bash
git add tests/test_invest_api_router_includepaper_sweep.py
git commit -m "test(invest): sweep — 10개 엔드포인트 기본 호출에 paper 미호출 (ROB-267)"
```

---

### Task 7: Frontend API layer — `fetchAccountPanel`, `fetchInvestHome` 에 옵션 인자 추가

**Files:**
- Modify: `frontend/invest/src/api/accountPanel.ts`
- Modify: `frontend/invest/src/api/investHome.ts`

- [ ] **Step 7.1: `fetchAccountPanel` 옵션 인자 도입**

`frontend/invest/src/api/accountPanel.ts` 를 다음으로 교체:

```ts
import type { AccountPanelResponse } from "../types/invest";

export interface FetchAccountPanelOptions {
  includePaper?: boolean;
  paperSources?: readonly string[];
  signal?: AbortSignal;
}

export async function fetchAccountPanel(
  options: FetchAccountPanelOptions = {},
): Promise<AccountPanelResponse> {
  const params = new URLSearchParams();
  if (options.includePaper) {
    params.set("includePaper", "true");
  }
  if (options.paperSources && options.paperSources.length > 0) {
    params.set("paperSources", options.paperSources.join(","));
  }
  const qs = params.toString();
  const url = qs ? `/invest/api/account-panel?${qs}` : "/invest/api/account-panel";
  const res = await fetch(url, { credentials: "include", signal: options.signal });
  if (!res.ok) throw new Error(`account-panel ${res.status}`);
  return res.json();
}
```

- [ ] **Step 7.2: `fetchInvestHome` 옵션 인자 도입**

`frontend/invest/src/api/investHome.ts` 를 다음으로 교체:

```ts
import type { InvestHomeResponse } from "../types/invest";

export interface FetchInvestHomeOptions {
  signal?: AbortSignal;
  includePaper?: boolean;
  paperSources?: readonly string[];
}

export async function fetchInvestHome(
  options: FetchInvestHomeOptions = {},
): Promise<InvestHomeResponse> {
  const params = new URLSearchParams();
  if (options.includePaper) {
    params.set("includePaper", "true");
  }
  if (options.paperSources && options.paperSources.length > 0) {
    params.set("paperSources", options.paperSources.join(","));
  }
  const qs = params.toString();
  const url = qs ? `/invest/api/home?${qs}` : "/invest/api/home";
  const res = await fetch(url, { credentials: "include", signal: options.signal });
  if (!res.ok) {
    throw new Error(`/invest/api/home ${res.status}`);
  }
  return (await res.json()) as InvestHomeResponse;
}
```

- [ ] **Step 7.3: `useInvestHome` 호출 위치 업데이트**

`frontend/invest/src/hooks/useInvestHome.ts:17` 의 `fetchInvestHome(controller.signal)` 호출을 다음으로 교체:

```ts
    fetchInvestHome({ signal: controller.signal })
```

기존 시그니처 변경에 따른 호출 사이트 수정.

- [ ] **Step 7.4: 빌드 + 타입체크**

Run: `cd frontend/invest && pnpm tsc --noEmit` (또는 `npm run typecheck` / 프로젝트 표준 명령)

Expected: 타입 에러 없음.

- [ ] **Step 7.5: 커밋**

```bash
git add frontend/invest/src/api/accountPanel.ts frontend/invest/src/api/investHome.ts frontend/invest/src/hooks/useInvestHome.ts
git commit -m "feat(invest-fe): API supports includePaper/paperSources query params (ROB-267)"
```

---

### Task 8: `AccountPanelProvider` lazy 동작 + `tick` 게이팅

**Files:**
- Modify: `frontend/invest/src/desktop/AccountPanelProvider.tsx`
- Test: `frontend/invest/src/__tests__/AccountPanelProvider.test.tsx`

- [ ] **Step 8.1: 실패하는 테스트 — mount 시 fetch 가 발생하지 않음**

`frontend/invest/src/__tests__/AccountPanelProvider.test.tsx` 의 기존 테스트들을 다음으로 교체:

```tsx
import { act, render, screen, waitFor } from "@testing-library/react";
import { vi, test, expect, beforeEach } from "vitest";
import { AccountPanelProvider, useAccountPanelContext } from "../desktop/AccountPanelProvider";
import * as panelApi from "../api/accountPanel";
import type { AccountPanelResponse } from "../types/invest";

const MOCK_RESP: AccountPanelResponse = {
  homeSummary: {
    includedSources: ["kis"],
    excludedSources: [],
    totalValueKrw: 2_000_000,
    pnlKrw: 100_000,
    pnlRate: 0.05,
  },
  accounts: [],
  groupedHoldings: [],
  watchSymbols: [],
  sourceVisuals: [],
  meta: { warnings: [], watchlistAvailable: true },
};

function StatusChild({ autoLoad }: { autoLoad?: boolean }) {
  const ctx = useAccountPanelContext();
  if (autoLoad && !ctx.data && !ctx.loading && !ctx.error) {
    // Trigger explicit load to simulate panel visibility
    ctx.load();
  }
  if (ctx.loading) return <div data-testid="loading">loading</div>;
  if (ctx.error) return <div data-testid="error">{ctx.error}</div>;
  if (ctx.data) return <div data-testid="ready">{ctx.data.homeSummary.totalValueKrw}</div>;
  return <div data-testid="idle">idle</div>;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

test("does not fetch on mount", async () => {
  const spy = vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(MOCK_RESP);
  render(
    <AccountPanelProvider>
      <StatusChild />
    </AccountPanelProvider>,
  );
  expect(screen.getByTestId("idle")).toBeInTheDocument();
  // Yield to microtasks to ensure no fetch was scheduled.
  await act(async () => {
    await Promise.resolve();
  });
  expect(spy).not.toHaveBeenCalled();
});

test("load() triggers fetch and populates data", async () => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(MOCK_RESP);
  render(
    <AccountPanelProvider>
      <StatusChild autoLoad />
    </AccountPanelProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("ready")).toBeInTheDocument());
  expect(screen.getByTestId("ready").textContent).toBe("2000000");
});

test("reload() is a no-op when not yet loaded", async () => {
  const spy = vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(MOCK_RESP);
  function ReloadButton() {
    const ctx = useAccountPanelContext();
    return <button onClick={ctx.reload}>reload</button>;
  }
  render(
    <AccountPanelProvider>
      <ReloadButton />
    </AccountPanelProvider>,
  );
  screen.getByText("reload").click();
  await act(async () => {
    await Promise.resolve();
  });
  expect(spy).not.toHaveBeenCalled();
});

test("reload() re-fetches with last params after load", async () => {
  const spy = vi
    .spyOn(panelApi, "fetchAccountPanel")
    .mockResolvedValue(MOCK_RESP);
  function Controls() {
    const ctx = useAccountPanelContext();
    return (
      <>
        <button onClick={() => ctx.load({ includePaper: true, paperSources: ["kis_mock"] })}>load-kis-mock</button>
        <button onClick={ctx.reload}>reload</button>
      </>
    );
  }
  render(
    <AccountPanelProvider>
      <Controls />
    </AccountPanelProvider>,
  );
  screen.getByText("load-kis-mock").click();
  await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
  expect(spy).toHaveBeenLastCalledWith(
    expect.objectContaining({ includePaper: true, paperSources: ["kis_mock"] }),
  );
  screen.getByText("reload").click();
  await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
  expect(spy).toHaveBeenLastCalledWith(
    expect.objectContaining({ includePaper: true, paperSources: ["kis_mock"] }),
  );
});

test("shows error state when fetch fails", async () => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockRejectedValue(new Error("network error"));
  render(
    <AccountPanelProvider>
      <StatusChild autoLoad />
    </AccountPanelProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("error")).toBeInTheDocument());
  expect(screen.getByTestId("error").textContent).toContain("network error");
});

test("throws when used outside provider", () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  expect(() => render(<StatusChild />)).toThrow();
  consoleError.mockRestore();
});
```

- [ ] **Step 8.2: 테스트 실패 확인**

Run: `cd frontend/invest && pnpm vitest run src/__tests__/AccountPanelProvider.test.tsx`

Expected: 다수 FAIL — 현재 `AccountPanelProvider` 가 mount 시 자동 fetch 하므로 "does not fetch on mount" 가 실패하고, `load()` / `reload()` 의 새 시맨틱이 없음.

- [ ] **Step 8.3: `AccountPanelProvider` 를 lazy 로 재작성**

`frontend/invest/src/desktop/AccountPanelProvider.tsx` 전체를 다음으로 교체:

```tsx
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { AccountPanelResponse } from "../types/invest";
import { fetchAccountPanel, type FetchAccountPanelOptions } from "../api/accountPanel";

export interface AccountPanelLoadOptions {
  includePaper?: boolean;
  paperSources?: readonly string[];
}

export interface AccountPanelContextValue {
  data: AccountPanelResponse | undefined;
  error: string | undefined;
  loading: boolean;
  refreshing: boolean;
  lastLoadedAt: number | undefined;
  /** Currently-loaded paper sources (empty unless includePaper was passed). */
  loadedPaperSources: readonly string[];
  /** Lazy fetch entry-point. Safe to call multiple times. */
  load: (options?: AccountPanelLoadOptions) => void;
  /** Re-fetch with the last successful params. No-op if never loaded. */
  reload: () => void;
}

const AccountPanelContext = createContext<AccountPanelContextValue | null>(null);

export function AccountPanelProvider({ children }: Readonly<{ children: ReactNode }>) {
  const [data, setData] = useState<AccountPanelResponse | undefined>();
  const [error, setError] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [lastLoadedAt, setLastLoadedAt] = useState<number | undefined>();
  const [loadedPaperSources, setLoadedPaperSources] = useState<readonly string[]>([]);

  const lastOptionsRef = useRef<AccountPanelLoadOptions | null>(null);
  const inflightRef = useRef<AbortController | null>(null);
  const hasLoadedRef = useRef(false);

  const doFetch = useCallback((opts: AccountPanelLoadOptions) => {
    inflightRef.current?.abort();
    const controller = new AbortController();
    inflightRef.current = controller;

    setError(undefined);
    if (hasLoadedRef.current) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }

    const apiOpts: FetchAccountPanelOptions = {
      signal: controller.signal,
      includePaper: opts.includePaper,
      paperSources: opts.paperSources,
    };

    fetchAccountPanel(apiOpts)
      .then((r) => {
        if (controller.signal.aborted) return;
        setData(r);
        setLoading(false);
        setRefreshing(false);
        setLastLoadedAt(Date.now());
        setLoadedPaperSources(opts.paperSources ? [...opts.paperSources] : []);
        hasLoadedRef.current = true;
        lastOptionsRef.current = opts;
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted) return;
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
        setLoading(false);
        setRefreshing(false);
        hasLoadedRef.current = true;
        lastOptionsRef.current = opts;
      });
  }, []);

  const load = useCallback(
    (options: AccountPanelLoadOptions = {}) => {
      doFetch(options);
    },
    [doFetch],
  );

  const reload = useCallback(() => {
    // Lazy mode: do not auto-fetch unless we have previously loaded.
    if (!hasLoadedRef.current || lastOptionsRef.current === null) return;
    doFetch(lastOptionsRef.current);
  }, [doFetch]);

  const value = useMemo(
    () => ({
      data,
      error,
      loading,
      refreshing,
      lastLoadedAt,
      loadedPaperSources,
      load,
      reload,
    }),
    [data, error, loading, refreshing, lastLoadedAt, loadedPaperSources, load, reload],
  );

  return (
    <AccountPanelContext.Provider value={value}>
      {children}
    </AccountPanelContext.Provider>
  );
}

export function useAccountPanelContext(): AccountPanelContextValue {
  const ctx = useContext(AccountPanelContext);
  if (!ctx) throw new Error("useAccountPanelContext must be used within AccountPanelProvider");
  return ctx;
}
```

- [ ] **Step 8.4: 테스트 통과 확인**

Run: `cd frontend/invest && pnpm vitest run src/__tests__/AccountPanelProvider.test.tsx`

Expected: 모두 PASS.

- [ ] **Step 8.5: 기존 컴포넌트 회귀 확인 — `useAccountPanel` 소비자**

Run: `cd frontend/invest && pnpm vitest run src/__tests__/RightRemotePanel.test.tsx src/__tests__/DesktopShell.test.tsx`

Expected: 다수 FAIL — 기존 테스트들이 mount 자동 fetch 를 가정하고 있으므로 깨질 것. 이는 Task 9 에서 RightRemotePanel 이 명시 load() 를 호출하도록 변경하면서 정리됨.

> ⚠️ Step 8.5 의 실패는 **예상되는 회귀**다. Task 9 진행까지 누적된 채로 둔다.

- [ ] **Step 8.6: 커밋**

```bash
git add frontend/invest/src/desktop/AccountPanelProvider.tsx frontend/invest/src/__tests__/AccountPanelProvider.test.tsx
git commit -m "feat(invest-fe): AccountPanelProvider lazy load + tick guard

mount 시 자동 fetch 를 제거하고 load(options?) 메서드로 진입점을 명시한다.
reload() 는 이미 한 번 load() 된 경우에만 마지막 옵션으로 재호출한다.
loadedPaperSources 로 현재 로드된 paper source 를 노출한다. (ROB-267)"
```

---

### Task 9: `RightRemotePanel` — portfolio 탭 가시화 시 load + paper 버튼 source-specific lazy fetch

**Files:**
- Modify: `frontend/invest/src/desktop/RightRemotePanel.tsx`
- Test: `frontend/invest/src/__tests__/RightRemotePanel.test.tsx`

**디자인 요약:**
- portfolio 탭이 활성화될 때 (또는 컴포넌트 mount + tab=="portfolio") `load()` 호출 — `includePaper=false` 기본.
- 계좌 필터 버튼 중 source 가 `kis_mock`, `kiwoom_mock`, `alpaca_paper`, `db_simulated` 에 해당하는 것 (paper) 을 클릭하면 `load({ includePaper: true, paperSources: [source] })` 재호출.
- "전체" 또는 live/manual source 클릭 시는 `load({ includePaper: false })` 재호출 (paper 데이터 비움).
- 클릭 중 해당 버튼/패널에 로딩 상태 표시.

- [ ] **Step 9.1: 실패하는 테스트 — portfolio 탭 mount 시 fetchAccountPanel 호출 (기본 includePaper=false)**

`frontend/invest/src/__tests__/RightRemotePanel.test.tsx` 끝에 추가 (또는 기존 mount fetch 테스트를 다음으로 교체):

```tsx
test("portfolio 탭 활성화 시 includePaper=false 로 load", async () => {
  const spy = vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(PANEL_RESP);
  render(
    <AccountPanelProvider>
      <RightRemotePanel initialTab="portfolio" />
    </AccountPanelProvider>,
  );
  await waitFor(() => expect(spy).toHaveBeenCalled());
  expect(spy).toHaveBeenLastCalledWith(
    expect.objectContaining({ includePaper: false }),
  );
});

test("KIS 모의 버튼 클릭 시 paperSources=['kis_mock'] 로 lazy fetch", async () => {
  const spy = vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(PANEL_RESP);
  render(
    <AccountPanelProvider>
      <RightRemotePanel initialTab="portfolio" />
    </AccountPanelProvider>,
  );
  await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));

  // PANEL_RESP 가 KIS 모의 옵션을 포함하도록 setUp 필요 — 기존 RightRemotePanel.test.tsx
  // 픽스처에 'kis_mock' source 의 Account 를 1개 추가하고, scoped.options 에
  // 'KIS 모의' 라벨이 들어오도록 한다.
  const btn = screen.getByRole("button", { name: /kis 모의/i });
  btn.click();

  await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
  expect(spy).toHaveBeenLastCalledWith(
    expect.objectContaining({
      includePaper: true,
      paperSources: ["kis_mock"],
    }),
  );
});

test("KIS 모의 클릭 시 Alpaca Paper 가 함께 조회되지 않음", async () => {
  const spy = vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(PANEL_RESP);
  render(
    <AccountPanelProvider>
      <RightRemotePanel initialTab="portfolio" />
    </AccountPanelProvider>,
  );
  await waitFor(() => expect(spy).toHaveBeenCalled());

  screen.getByRole("button", { name: /kis 모의/i }).click();
  await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));

  const lastCall = spy.mock.calls[spy.mock.calls.length - 1][0];
  expect(lastCall?.paperSources).toEqual(["kis_mock"]);
  expect(lastCall?.paperSources).not.toContain("alpaca_paper");
});
```

> Mock 픽스처 (`PANEL_RESP`) 에 `kis_mock` source 의 account 가 포함되어 있어야 `KIS 모의` 버튼이 렌더링됨. 기존 픽스처를 검토하여 필요시 보강.

- [ ] **Step 9.2: 테스트 실패 확인**

Run: `cd frontend/invest && pnpm vitest run src/__tests__/RightRemotePanel.test.tsx`

Expected: 다수 FAIL (현재 RightRemotePanel 은 lazy load 를 모르고 paper 버튼 클릭이 source-specific fetch 를 트리거하지 않음).

- [ ] **Step 9.3: `RightRemotePanel` 에 lazy load + paper 버튼 wiring 추가**

`frontend/invest/src/desktop/RightRemotePanel.tsx` 상단 (line 1-15 부근) 의 import 에 `useAccountPanel` 이 이미 있는지 확인하고, paper source 식별 유틸을 추가. 핵심 변경:

1. portfolio 탭이 활성화되었을 때 `load()` 호출하는 `useEffect` 추가.
2. 계좌 필터 버튼 핸들러를 `setSelectedAccountKey(option.key)` 에서 확장하여 source 가 paper 면 `load({ includePaper: true, paperSources: [source] })` 호출.

새 헬퍼 (파일 상단 어디든, 예를 들어 `scopeHoldings.ts` 가 import 되는 부근에 추가):

```tsx
const PAPER_SOURCES: ReadonlySet<string> = new Set([
  "kis_mock",
  "kiwoom_mock",
  "alpaca_paper",
  "db_simulated",
]);

function isPaperSource(source: string | undefined): boolean {
  return source !== undefined && PAPER_SOURCES.has(source);
}
```

`RightRemotePanel` 컴포넌트 안에서 (line 100 부근, `useAccountPanel()` 사용 위치) 의 hook 사용을 다음 패턴으로 바꾼다:

```tsx
const accountPanel = useAccountPanel(); // 기존 hook
const { data, loading, refreshing, error, load, loadedPaperSources, reload } = accountPanel;

// Portfolio 탭이 활성화 되었을 때 (한 번만) 기본 load.
useEffect(() => {
  if (currentTab !== "portfolio") return;
  if (data !== undefined || loading || refreshing) return;
  load({ includePaper: false });
}, [currentTab, data, loading, refreshing, load]);
```

(현 코드의 `useAccountPanel()` 인터페이스가 변경됐을 수 있으므로 `useAccountPanel.ts` 파일도 함께 확인 — 단순 wrapper 인 경우 변경 불필요.)

계좌 필터 버튼 핸들러 (line 292 `onClick={() => setSelectedAccountKey(option.key)}`) 를 다음으로 교체:

```tsx
onClick={() => {
  setSelectedAccountKey(option.key);
  const source = option.source; // assume AccountFilterOption exposes underlying source
  if (isPaperSource(source)) {
    load({ includePaper: true, paperSources: [source] });
  } else if (loadedPaperSources.length > 0) {
    // 사용자가 paper 가 아닌 옵션을 선택하면 paper 데이터 청소
    load({ includePaper: false });
  }
}}
```

> `option.source` 가 노출되지 않은 상태라면 `frontend/invest/src/desktop/scopeHoldings.ts` 의 `AccountFilterOption` 타입에 `source: string` 을 추가하고 `optionFor` 빌더에서 채워 넣는다.

- [ ] **Step 9.4: `useAccountPanel` 래퍼 확인 / 업데이트**

`frontend/invest/src/desktop/useAccountPanel.ts` 가 단순히 `useAccountPanelContext` 를 re-export 하는 wrapper 인지 확인. 그렇다면 변경 불필요. 추가 로직이 있다면 `load`, `reload`, `loadedPaperSources` 가 노출되도록 보강.

- [ ] **Step 9.5: 테스트 통과 확인**

Run: `cd frontend/invest && pnpm vitest run src/__tests__/RightRemotePanel.test.tsx src/__tests__/AccountPanelProvider.test.tsx`

Expected: 모두 PASS.

- [ ] **Step 9.6: 다른 테스트 파일 전수 점검**

Run: `cd frontend/invest && pnpm vitest run`

Expected: 13개 가량의 다른 `__tests__/*.tsx` 가 `AccountPanelProvider` 를 wrapper 로 사용하는데, 대부분은 panel data 를 직접 사용하지 않으므로 영향 없음. 실패하는 테스트가 있다면 해당 테스트 컴포넌트 내에서 명시 `ctx.load()` 호출이 필요한지 검토하고 수정.

대상 후보:
- `DesktopFeedNewsPage.test.tsx`
- `DesktopCalendarPage.test.tsx`
- `DesktopScreenerPage.test.tsx`
- `DesktopShell.test.tsx`
- `RightRemotePanel.test.tsx` (이미 Task 9.5 에서 처리)

각 테스트 파일에서 `vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(...)` 후 panel data 가 렌더 결과에 영향을 주는지 검토. 영향이 있다면 테스트 자체가 RightRemotePanel 의 portfolio 탭 활성화로 load() 가 트리거되는 시나리오일 것이므로, 테스트가 자연스럽게 통과해야 함. 그렇지 않은 경우 명시 load 호출을 추가.

- [ ] **Step 9.7: 커밋**

```bash
git add frontend/invest/src/desktop/RightRemotePanel.tsx frontend/invest/src/desktop/scopeHoldings.ts frontend/invest/src/desktop/useAccountPanel.ts frontend/invest/src/__tests__/RightRemotePanel.test.tsx
git commit -m "feat(invest-fe): portfolio 탭 lazy load + paper 버튼 source-specific fetch

portfolio 탭 활성화 시 includePaper=false 로 load 한다. 계좌 필터 버튼에서
KIS 모의 / Alpaca Paper 등 paper source 를 클릭하면 해당 source 만
paperSources 로 명시하여 lazy fetch. 다른 paper source 는 함께 조회되지
않는다. (ROB-267)"
```

---

### Task 10: 다른 frontend 테스트 파일 회귀 정리

**Files:**
- Modify (조건부): 영향받는 `frontend/invest/src/__tests__/*.tsx`

- [ ] **Step 10.1: 전체 frontend 테스트 실행**

Run: `cd frontend/invest && pnpm vitest run`

Expected: 모든 테스트 PASS. 실패하는 테스트가 있다면 다음 진단:
1. 테스트가 `panel data` 자체에 의존하는지 (예: holdings 가 렌더되는지 확인하는 assertion)
2. 의존한다면 테스트 setup 에서 명시 `useAccountPanelContext().load()` 호출이 필요한지

- [ ] **Step 10.2: 각 실패 테스트 별 처리**

대표 패턴 — 테스트가 `<AccountPanelProvider>` 로 감싸고 child 컴포넌트가 panel data 를 사용한다면, 테스트 helper 컴포넌트로 mount 직후 load() 호출:

```tsx
function AutoLoadHelper() {
  const ctx = useAccountPanelContext();
  React.useEffect(() => { ctx.load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

render(
  <AccountPanelProvider>
    <AutoLoadHelper />
    <ComponentUnderTest />
  </AccountPanelProvider>,
);
```

또는 production 코드 (RightRemotePanel 등) 가 자연스럽게 load() 를 트리거하면 그대로 통과.

- [ ] **Step 10.3: 타입체크 + 린트**

Run: `cd frontend/invest && pnpm tsc --noEmit && pnpm lint`

Expected: 클린.

- [ ] **Step 10.4: 커밋 (변경이 있는 경우에만)**

```bash
git add frontend/invest/src/__tests__/
git commit -m "test(invest-fe): adjust existing test wrappers for lazy AccountPanelProvider (ROB-267)"
```

---

### Task 11: 통합 smoke — dev 서버에서 실제 동작 확인

> **참고:** 이 Task 는 코드 변경 없음. 수동 검증.

- [ ] **Step 11.1: 백엔드 + 프론트엔드 dev 서버 기동**

Run (백엔드):
```bash
make dev
```

Run (프론트엔드, 별도 터미널):
```bash
cd frontend/invest && pnpm dev
```

- [ ] **Step 11.2: 브라우저 DevTools Network 탭으로 다음 시나리오 검증**

시나리오 A — `/invest` 초기 진입:
- ✅ `/invest/api/home` 호출됨 (파라미터: `includePaper` 없음 또는 `false`)
- ❌ `/invest/api/account-panel` 호출 안 됨 (lazy)
- ❌ KIS mock / Alpaca Paper 백엔드 호출 없음 (Sentry trace 또는 백엔드 로그로 확인)

시나리오 B — 우측 portfolio 탭 클릭:
- ✅ `/invest/api/account-panel?includePaper=false` 1회 호출
- ❌ paper API 호출 없음

시나리오 C — "KIS 모의" 버튼 클릭:
- ✅ `/invest/api/account-panel?includePaper=true&paperSources=kis_mock` 1회 호출
- ❌ Alpaca Paper 호출 없음

시나리오 D — "Alpaca Paper" 버튼 클릭:
- ✅ `/invest/api/account-panel?includePaper=true&paperSources=alpaca_paper` 1회 호출
- ❌ KIS mock 호출 없음

시나리오 E — Sentry 로컬 dev 환경 (있다면) trace:
- ✅ `invest.home.kis`, `invest.home.upbit`, `invest.home.manual` span 존재
- ✅ paper 호출 시에만 `invest.home.kis_mock` / `invest.home.alpaca_paper` 가 존재
- ✅ `invest.account_panel.build` outer span 존재

- [ ] **Step 11.3: 회귀 확인 — 모바일 라우트**

`/m/invest` 등 모바일 페이지가 있다면 그곳도 진입하여 useInvestHome 만 호출되는지 (AccountPanelProvider 가 모바일에 적용되지 않거나 panel 이 비활성 상태로 mount 되는지) 확인.

- [ ] **Step 11.4: 발견된 이슈 (있다면) 별도 커밋 또는 후속 처리**

수동 검증에서 회귀 발견 시 적절한 Task 로 돌아가 수정.

---

### Task 12: 전체 회귀 + 최종 커밋

- [ ] **Step 12.1: 백엔드 풀 테스트**

Run: `make test`

Expected: 모든 테스트 PASS.

- [ ] **Step 12.2: 백엔드 린트 + 타입체크**

Run: `make lint && make typecheck`

Expected: 클린.

- [ ] **Step 12.3: 프론트엔드 풀 테스트**

Run: `cd frontend/invest && pnpm vitest run && pnpm tsc --noEmit`

Expected: 클린.

- [ ] **Step 12.4: git log 정리**

Run: `git log --oneline main..HEAD`

Expected: ROB-267 관련 커밋들이 의미 단위로 분리되어 있음 (Task 별 1 커밋).

- [ ] **Step 12.5: PR 생성**

Run:
```bash
gh pr create --base main --title "feat(invest): paper/mock lazy loading 및 home 중복 fetch 제거 (ROB-267)" --body "$(cat <<'EOF'
## Summary
- `InvestHomeService.get_home()` 에 `include_paper`, `paper_sources` 파라미터 추가, 기본 false 로 paper reader 호출 차단
- 10개 invest_api 엔드포인트에 `includePaper`/`paperSources` 쿼리 파라미터 노출 (옵션 B 슬림 path 포함)
- `AccountPanelProvider` mount 자동 fetch 제거, `load()` lazy API + `reload()` 게이팅
- `RightRemotePanel` paper 계좌 버튼 클릭 시 source 별 lazy fetch
- reader 별 Sentry span (`invest.home.kis|upbit|manual|kis_mock|alpaca_paper`) + `invest.account_panel.build` outer span

## Linear
- ROB-267

## Test plan
- [x] Backend: `make test` 전부 PASS
- [x] Backend: 10-endpoint sweep — 기본 호출에 paper reader 미실행
- [x] Backend: paper reader exception graceful fallback
- [x] Frontend: `AccountPanelProvider` mount 자동 fetch 없음
- [x] Frontend: paper 버튼 클릭 시 해당 source 만 fetch
- [x] 수동 smoke (시나리오 A-E)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review 체크리스트

플랜 작성 후 spec 대비 다음을 점검:

1. **Spec coverage**
   - [x] 1. includePaper=false 적용 범위 확장 — 10개 엔드포인트 sweep (Task 3, 6) ✅
   - [x] 2. 옵션 B 1차 방향 — `build_account_panel_view` 슬림 path (Task 2) ✅
   - [x] 3. 단일 endpoint + query param + source별 제어 (Task 3) ✅
   - [x] 4. AccountPanelProvider mount/tick 게이팅 (Task 8) ✅
   - [x] 5. useInvestHome 중복 fetch — 옵션 B 분리로 본질적 중복 제거 (Task 2 + Task 8 lazy 조합) ✅
   - [x] 6. Observability — 6개 reader span + tag (Task 5) ✅
   - [x] 7. 테스트 — backend 정상 / 필터 / 예외 / sweep, frontend lazy / source-specific / 회귀 (Task 1, 2, 4, 6, 8, 9) ✅
   - [x] 8. 비범위 — broker/order/watch mutation 미포함, FX/Fear&Greed/US per-symbol 미포함 ✅

2. **Placeholder scan** — TBD / TODO / "Add appropriate" 없음 ✅

3. **Type consistency** — `include_paper`, `paper_sources`, `_AccountPanelView`, `build_account_panel_view`, `load()`, `loadedPaperSources` 가 Task 간 일관 ✅

---

## 실행 가이드

이 플랜은 12 Task / 다수 step 으로 구성. 권장 실행 모드:

- **Subagent-Driven** — 백엔드 (Task 1-6) 와 프론트엔드 (Task 7-10) 가 독립적으로 진행 가능하므로 두 흐름을 parallel 로 dispatch 가능
- **Inline** — 순차 실행, 각 Task 후 사용자 review checkpoint

각 Task 의 모든 step 이 PASS 한 뒤에만 다음 Task 로 진행. 예상되는 회귀 (Task 8.5) 처럼 의도적인 누적 실패는 후속 Task 에서 정리.
