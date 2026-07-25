# ROB-664 — analysis_artifacts + session_context read-only 웹 노출 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `review.analysis_artifacts`와 `review.operator_session_context`를 `/invest/insights`에서 볼 수 있게 하는 read-only GET 라우터 2개 + 프런트 패널 2개 (migration 0).

**Architecture:** ROB-663 forecast dashboard 패턴을 그대로 미러. 백엔드는 기존 서비스 read 메서드(`AnalysisArtifactService.list_artifacts`/`get`, `SessionContextService.get_recent`)와 기존 스키마(`AnalysisArtifactListResponse`/`AnalysisArtifactGetResponse`/`SessionContextRecentResponse`)를 재사용하는 thin 라우터. 유일한 데이터 변경은 `list_artifacts`에 `readiness_label` 필터를 additive로 추가. 프런트는 `DesktopInsightsPage`에 패널 2개 배선.

**Tech Stack:** FastAPI, SQLAlchemy async, Pydantic v2, pytest; React + TypeScript + Vite + vitest (frontend/invest).

## Global Constraints

- **read-only**: 브로커/주문/감시/order-intent mutation 도달 없음. 서비스 레이어 경유만.
- **migration 0**: 신규 DB 마이그레이션·컬럼 없음.
- 라우터 prefix 규칙: `/trading/api/invest/<name>`.
- 라우터 모든 endpoint는 `Depends(get_authenticated_user)` + `Depends(get_db)` (`app.routers.dependencies` / `app.core.db`).
- 잘못된 enum 쿼리 파라미터 → **HTTP 422** (ROB-663 `_validate_*` 패턴, manual HTTPException).
- 목록(list) 응답은 **payload 미포함** (`AnalysisArtifactMeta`), 상세(get)만 payload 포함 (ROB-504 교훈).
- 커밋 trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- 프런트 TS 타입은 Pydantic 스키마 **snake_case 필드 그대로** 미러. 쿼리 키는 camelCase → snake_case 변환.
- fetch는 `credentials: "include"`.

---

### Task 1: `list_artifacts`에 `readiness_label` 필터 추가 (서비스 + 스키마 + 통합 테스트)

**Files:**
- Modify: `app/services/analysis_artifact.py:150-196` (`list_artifacts`)
- Modify: `app/schemas/analysis_artifact.py:107-119` (`AnalysisArtifactListRequest`)
- Test: `tests/test_analysis_artifact_web_read.py` (Create)

**Interfaces:**
- Produces: `AnalysisArtifactService.list_artifacts(..., readiness_label: AnalysisArtifactReadinessLiteral | None = None)` — 기존 시그니처 끝에 additive 키워드. `None`이면 기존 동작 불변.
- Produces: `AnalysisArtifactListRequest.readiness_label: AnalysisArtifactReadinessLiteral | None = None` (필터 echo용).

- [ ] **Step 1: 통합 테스트 작성 (readiness_label 필터)**

`tests/test_analysis_artifact_web_read.py` 생성. `tests/test_forecast_web_read.py` 패턴(integration mark + cleanup fixture + `db_session`) 미러:

```python
from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.analysis_artifact import AnalysisArtifact
from app.services.analysis_artifact import AnalysisArtifactService

pytestmark = [pytest.mark.integration]


@pytest.fixture(autouse=True)
async def _cleanup(db_session: AsyncSession):
    await db_session.execute(sa.delete(AnalysisArtifact))
    await db_session.commit()
    yield
    await db_session.execute(sa.delete(AnalysisArtifact))
    await db_session.commit()


async def _add(db_session: AsyncSession, **kw) -> AnalysisArtifact:
    now = now_kst()
    defaults = dict(
        market="kr",
        kind="screening_ranking",
        title="t",
        symbols=[],
        payload={},
        as_of=now,
        valid_until=now + timedelta(days=1),
        created_by="claude",
        version=1,
    )
    defaults.update(kw)
    row = AnalysisArtifact(**defaults)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_list_filters_by_readiness_label(db_session: AsyncSession):
    await _add(db_session, title="ready", readiness_label="ready_for_order_review")
    await _add(db_session, title="blocked", readiness_label="blocked")
    svc = AnalysisArtifactService(db_session)

    rows = await svc.list_artifacts(readiness_label="ready_for_order_review")

    assert [r.title for r in rows] == ["ready"]


@pytest.mark.asyncio
async def test_list_readiness_none_returns_all(db_session: AsyncSession):
    await _add(db_session, title="a", readiness_label="ready_for_order_review")
    await _add(db_session, title="b", readiness_label=None)
    svc = AnalysisArtifactService(db_session)

    rows = await svc.list_artifacts()

    assert {r.title for r in rows} == {"a", "b"}
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_analysis_artifact_web_read.py -v`
Expected: FAIL — `list_artifacts() got an unexpected keyword argument 'readiness_label'`

- [ ] **Step 3: 서비스에 필터 추가**

`app/services/analysis_artifact.py`의 `list_artifacts` 시그니처에 `account_scope` 다음 줄로 추가:

```python
        account_scope: str | None = None,
        readiness_label: AnalysisArtifactReadinessLiteral | None = None,
    ) -> list[AnalysisArtifact]:
```

그리고 `account_scope` where 절 다음에 추가:

```python
        if account_scope is not None:
            stmt = stmt.where(AnalysisArtifact.account_scope == account_scope)
        if readiness_label is not None:
            stmt = stmt.where(AnalysisArtifact.readiness_label == readiness_label)
```

파일 상단 import에 `AnalysisArtifactReadinessLiteral`가 있는지 확인 — 없으면 기존 `from app.schemas.analysis_artifact import (...)` 블록에 추가 (이미 `AnalysisArtifactKindLiteral`를 import 중).

- [ ] **Step 4: 스키마 필터 필드 추가**

`app/schemas/analysis_artifact.py`의 `AnalysisArtifactListRequest`에 `account_scope` 다음 줄로 추가:

```python
    account_scope: str | None = None
    readiness_label: AnalysisArtifactReadinessLiteral | None = None
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `uv run pytest tests/test_analysis_artifact_web_read.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: 커밋**

```bash
git add app/services/analysis_artifact.py app/schemas/analysis_artifact.py tests/test_analysis_artifact_web_read.py
git commit -m "feat(ROB-664): add readiness_label filter to list_artifacts

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 아티팩트 GET 라우터 (`invest_artifacts.py`) + 단위 테스트 + 등록

**Files:**
- Create: `app/routers/invest_artifacts.py`
- Modify: `app/main.py:37` (import 그룹), `app/main.py:193-201` (include 클러스터)
- Test: `tests/routers/test_invest_artifacts_router.py` (Create)

**Interfaces:**
- Consumes: `AnalysisArtifactService(db).list_artifacts(...)` / `.get(artifact_id)` (Task 1 확장 포함).
- Produces: 라우터 `router` (prefix `/trading/api/invest/artifacts`), endpoints `GET /`, `GET /{artifact_id}`.

- [ ] **Step 1: 라우터 단위 테스트 작성**

`tests/routers/test_invest_artifacts_router.py` 생성. 라우터가 서비스를 per-request 인스턴스화하므로 **클래스 메서드를 monkeypatch**한다:

```python
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _meta_row(**kw):
    now = datetime(2026, 7, 3, tzinfo=timezone.utc)
    base = dict(
        id=1,
        artifact_uuid=uuid4(),
        market="kr",
        kind="screening_ranking",
        title="t",
        symbols=[],
        as_of=now,
        valid_until=None,
        session_label=None,
        correlation_id=None,
        account_scope=None,
        content_hash=None,
        version=1,
        readiness_label=None,
        payload_size_bytes=2,
        is_stale=False,
        created_by="claude",
        created_at=now,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _make_client(monkeypatch, *, rows=None, one=None):
    from app.core.db import get_db
    from app.routers import invest_artifacts
    from app.routers.dependencies import get_authenticated_user
    from app.services.analysis_artifact import AnalysisArtifactService

    calls: dict = {}

    async def _fake_list(self, **kwargs):
        calls["list"] = kwargs
        return rows if rows is not None else []

    async def _fake_get(self, artifact_id):
        calls["get"] = artifact_id
        return one

    monkeypatch.setattr(AnalysisArtifactService, "list_artifacts", _fake_list)
    monkeypatch.setattr(AnalysisArtifactService, "get", _fake_get)

    app = FastAPI()
    app.include_router(invest_artifacts.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: "db"
    return TestClient(app), calls


@pytest.mark.unit
def test_list_defaults(monkeypatch):
    client, calls = _make_client(monkeypatch, rows=[_meta_row(title="a")])
    r = client.get("/trading/api/invest/artifacts/")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["artifacts"][0]["title"] == "a"
    assert "payload" not in body["artifacts"][0]  # list = metadata only
    assert calls["list"]["include_stale"] is False


@pytest.mark.unit
def test_list_forwards_filters(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get(
        "/trading/api/invest/artifacts/"
        "?market=us&kind=briefing&readiness_label=blocked&symbol=AAPL"
        "&include_stale=true&limit=5"
    )
    assert r.status_code == 200
    assert calls["list"]["market"] == "us"
    assert calls["list"]["kind"] == "briefing"
    assert calls["list"]["readiness_label"] == "blocked"
    assert calls["list"]["symbol"] == "AAPL"
    assert calls["list"]["include_stale"] is True
    assert calls["list"]["limit"] == 5


@pytest.mark.unit
def test_list_rejects_invalid_kind(monkeypatch):
    client, _ = _make_client(monkeypatch)
    assert client.get("/trading/api/invest/artifacts/?kind=bogus").status_code == 422


@pytest.mark.unit
def test_list_rejects_invalid_readiness(monkeypatch):
    client, _ = _make_client(monkeypatch)
    assert (
        client.get("/trading/api/invest/artifacts/?readiness_label=bogus").status_code
        == 422
    )


@pytest.mark.unit
def test_get_detail_includes_payload(monkeypatch):
    row = _meta_row(id=9)
    row.payload = {"k": "v"}
    client, calls = _make_client(monkeypatch, one=row)
    r = client.get("/trading/api/invest/artifacts/9")
    assert r.status_code == 200
    body = r.json()
    assert body["artifact"]["payload"] == {"k": "v"}
    assert calls["get"] == "9"


@pytest.mark.unit
def test_get_detail_404(monkeypatch):
    client, _ = _make_client(monkeypatch, one=None)
    assert client.get("/trading/api/invest/artifacts/9").status_code == 404
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/routers/test_invest_artifacts_router.py -v`
Expected: FAIL — `ModuleNotFoundError: app.routers.invest_artifacts`

- [ ] **Step 3: 라우터 구현**

`app/routers/invest_artifacts.py` 생성:

```python
"""FastAPI router for /invest analysis-artifact read surface (ROB-664).

Read-only exposure of ``review.analysis_artifacts`` (ROB-637/648): list with
market/kind/readiness_label/symbol filters + is_stale badge, and per-artifact
detail with payload. Writes (analysis_artifact_save) stay MCP-only; no
broker/order/watch mutation is reachable from here. List responses omit the
payload (ROB-504 lesson) — payload loads only on the detail endpoint.
"""

from __future__ import annotations

from typing import Annotated, get_args

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.analysis_artifact import (
    AnalysisArtifactGetResponse,
    AnalysisArtifactKindLiteral,
    AnalysisArtifactListRequest,
    AnalysisArtifactListResponse,
    AnalysisArtifactMeta,
    AnalysisArtifactRead,
    AnalysisArtifactReadinessLiteral,
)
from app.schemas.investment_reports import MarketLiteral
from app.services.analysis_artifact import AnalysisArtifactService

router = APIRouter(
    prefix="/trading/api/invest/artifacts",
    tags=["invest-artifacts"],
)

_VALID_MARKETS = frozenset(get_args(MarketLiteral))
_VALID_KINDS = frozenset(get_args(AnalysisArtifactKindLiteral))
_VALID_READINESS = frozenset(get_args(AnalysisArtifactReadinessLiteral))


def _validate(name: str, value: str | None, allowed: frozenset[str]) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(status_code=422, detail=f"invalid {name}: {value}")


@router.get("/")
async def list_artifacts(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    readiness_label: Annotated[str | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    include_stale: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AnalysisArtifactListResponse:
    _validate("market", market, _VALID_MARKETS)
    _validate("kind", kind, _VALID_KINDS)
    _validate("readiness_label", readiness_label, _VALID_READINESS)
    svc = AnalysisArtifactService(db)
    rows = await svc.list_artifacts(
        market=market,
        kind=kind,
        readiness_label=readiness_label,
        symbol=symbol,
        include_stale=include_stale,
        limit=limit,
    )
    filters = AnalysisArtifactListRequest(
        market=market,
        kind=kind,
        readiness_label=readiness_label,
        symbol=symbol,
        include_stale=include_stale,
        limit=limit,
    )
    metas = [AnalysisArtifactMeta.model_validate(r) for r in rows]
    return AnalysisArtifactListResponse(
        count=len(metas), filters=filters, artifacts=metas
    )


@router.get("/{artifact_id}")
async def get_artifact(
    artifact_id: str,
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnalysisArtifactGetResponse:
    svc = AnalysisArtifactService(db)
    row = await svc.get(artifact_id)
    if row is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return AnalysisArtifactGetResponse(artifact=AnalysisArtifactRead.model_validate(row))
```

주의: `symbol` 정규화는 서비스가 이미 처리하므로 라우터는 raw로 전달. `AnalysisArtifactListRequest`의 `_strip_symbol` validator가 filters echo용 정규화를 수행.

- [ ] **Step 4: main.py에 라우터 등록**

`app/main.py:37` 근처 `from app.routers import (...)` 그룹에 `invest_artifacts,` 추가 (알파벳/기존 순서 유지, `invest_forecasts` 인접). 그리고 L193-201 invest include 클러스터에 추가:

```python
    app.include_router(invest_artifacts.router)
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `uv run pytest tests/routers/test_invest_artifacts_router.py -v`
Expected: PASS (6 passed)

- [ ] **Step 6: 라우터가 full app에 등록됐는지 확인**

Run: `uv run python -c "from app.main import create_app; app = create_app(); print([r.path for r in app.routes if '/artifacts' in r.path])"`
(만약 `create_app` 팩토리가 없으면 `from app.main import app`로 조회)
Expected: `/trading/api/invest/artifacts/` 와 `/trading/api/invest/artifacts/{artifact_id}` 출력

- [ ] **Step 7: 커밋**

```bash
git add app/routers/invest_artifacts.py app/main.py tests/routers/test_invest_artifacts_router.py
git commit -m "feat(ROB-664): analysis-artifact read-only GET router

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 세션 컨텍스트 GET 라우터 (`invest_session_context.py`) + 단위 테스트 + 등록

**Files:**
- Create: `app/routers/invest_session_context.py`
- Modify: `app/main.py:37` (import), `app/main.py:193-201` (include)
- Test: `tests/routers/test_invest_session_context_router.py` (Create)

**Interfaces:**
- Consumes: `SessionContextService(db).get_recent(...)`.
- Produces: 라우터 `router` (prefix `/trading/api/invest/session-context`), endpoint `GET /recent`.

- [ ] **Step 1: 라우터 단위 테스트 작성**

`tests/routers/test_invest_session_context_router.py` 생성:

```python
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _entry(**kw):
    now = datetime(2026, 7, 3, tzinfo=timezone.utc)
    base = dict(
        entry_uuid=uuid4(),
        kst_date=date(2026, 7, 3),
        market="kr",
        account_scope=None,
        entry_type="handoff_note",
        title="t",
        body="b",
        refs={},
        created_by="claude",
        session_label=None,
        created_at=now,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _make_client(monkeypatch, *, rows=None):
    from app.core.db import get_db
    from app.routers import invest_session_context
    from app.routers.dependencies import get_authenticated_user
    from app.services.session_context import SessionContextService

    calls: dict = {}

    async def _fake_recent(self, **kwargs):
        calls["recent"] = kwargs
        return rows if rows is not None else []

    monkeypatch.setattr(SessionContextService, "get_recent", _fake_recent)

    app = FastAPI()
    app.include_router(invest_session_context.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: "db"
    return TestClient(app), calls


@pytest.mark.unit
def test_recent_defaults(monkeypatch):
    client, calls = _make_client(monkeypatch, rows=[_entry(title="a")])
    r = client.get("/trading/api/invest/session-context/recent")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["entries"][0]["title"] == "a"
    assert calls["recent"]["limit"] == 20


@pytest.mark.unit
def test_recent_forwards_filters(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get(
        "/trading/api/invest/session-context/recent"
        "?market=us&account_scope=kis_live&entry_type=decision"
        "&kst_date_from=2026-07-01&limit=5"
    )
    assert r.status_code == 200
    assert calls["recent"]["market"] == "us"
    assert calls["recent"]["account_scope"] == "kis_live"
    assert calls["recent"]["entry_type"] == "decision"
    assert str(calls["recent"]["kst_date_from"]) == "2026-07-01"
    assert calls["recent"]["limit"] == 5


@pytest.mark.unit
def test_recent_rejects_invalid_entry_type(monkeypatch):
    client, _ = _make_client(monkeypatch)
    assert (
        client.get(
            "/trading/api/invest/session-context/recent?entry_type=bogus"
        ).status_code
        == 422
    )


@pytest.mark.unit
def test_recent_rejects_invalid_account_scope(monkeypatch):
    client, _ = _make_client(monkeypatch)
    assert (
        client.get(
            "/trading/api/invest/session-context/recent?account_scope=bogus"
        ).status_code
        == 422
    )
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/routers/test_invest_session_context_router.py -v`
Expected: FAIL — `ModuleNotFoundError: app.routers.invest_session_context`

- [ ] **Step 3: 라우터 구현**

`app/routers/invest_session_context.py` 생성:

```python
"""FastAPI router for /invest operator session-context read surface (ROB-664).

Read-only exposure of ``review.operator_session_context`` (ROB-516): the
append-only operator handoff log (entry_type: plan/decision/handoff_note/...),
newest first, with market/account_scope/entry_type/kst_date_from filters.
append_entries stays MCP-only; no mutation is reachable from here.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, get_args

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.investment_reports import AccountScopeLiteral, MarketLiteral
from app.schemas.session_context import (
    SessionContextEntryTypeLiteral,
    SessionContextRecentRequest,
    SessionContextRecentResponse,
    SessionContextResponse,
)
from app.services.session_context import SessionContextService

router = APIRouter(
    prefix="/trading/api/invest/session-context",
    tags=["invest-session-context"],
)

_VALID_MARKETS = frozenset(get_args(MarketLiteral))
_VALID_ACCOUNT_SCOPES = frozenset(get_args(AccountScopeLiteral))
_VALID_ENTRY_TYPES = frozenset(get_args(SessionContextEntryTypeLiteral))


def _validate(name: str, value: str | None, allowed: frozenset[str]) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(status_code=422, detail=f"invalid {name}: {value}")


@router.get("/recent")
async def list_recent_session_context(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Annotated[str | None, Query()] = None,
    account_scope: Annotated[str | None, Query()] = None,
    entry_type: Annotated[str | None, Query()] = None,
    kst_date_from: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SessionContextRecentResponse:
    _validate("market", market, _VALID_MARKETS)
    _validate("account_scope", account_scope, _VALID_ACCOUNT_SCOPES)
    _validate("entry_type", entry_type, _VALID_ENTRY_TYPES)
    svc = SessionContextService(db)
    rows = await svc.get_recent(
        market=market,
        account_scope=account_scope,
        entry_type=entry_type,
        kst_date_from=kst_date_from,
        limit=limit,
    )
    filters = SessionContextRecentRequest(
        market=market,
        account_scope=account_scope,
        entry_type=entry_type,
        kst_date_from=kst_date_from,
        limit=limit,
    )
    entries = [SessionContextResponse.model_validate(r) for r in rows]
    return SessionContextRecentResponse(
        count=len(entries), filters=filters, entries=entries
    )
```

- [ ] **Step 4: main.py에 라우터 등록**

`app/main.py:37` import 그룹에 `invest_session_context,` 추가, L193-201 클러스터에:

```python
    app.include_router(invest_session_context.router)
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `uv run pytest tests/routers/test_invest_session_context_router.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: 커밋**

```bash
git add app/routers/invest_session_context.py app/main.py tests/routers/test_invest_session_context_router.py
git commit -m "feat(ROB-664): operator session-context read-only GET router

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 세션 컨텍스트 통합 테스트 (서비스 read 스모크)

**Files:**
- Test: `tests/test_session_context_web_read.py` (Create)

**Interfaces:**
- Consumes: `SessionContextService.get_recent`, `OperatorSessionContext` ORM.

- [ ] **Step 1: 통합 테스트 작성**

`tests/test_session_context_web_read.py` 생성:

```python
from __future__ import annotations

from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session_context import OperatorSessionContext
from app.services.session_context import SessionContextService

pytestmark = [pytest.mark.integration]


@pytest.fixture(autouse=True)
async def _cleanup(db_session: AsyncSession):
    await db_session.execute(sa.delete(OperatorSessionContext))
    await db_session.commit()
    yield
    await db_session.execute(sa.delete(OperatorSessionContext))
    await db_session.commit()


async def _add(db_session: AsyncSession, **kw) -> OperatorSessionContext:
    base = dict(
        kst_date=date(2026, 7, 3),
        market="kr",
        entry_type="handoff_note",
        title="t",
        body="b",
        refs={},
        created_by="claude",
    )
    base.update(kw)
    row = OperatorSessionContext(**base)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_get_recent_newest_first_and_entry_type_filter(db_session: AsyncSession):
    await _add(db_session, title="old", entry_type="plan")
    await _add(db_session, title="new", entry_type="decision")
    svc = SessionContextService(db_session)

    all_rows = await svc.get_recent()
    assert [r.title for r in all_rows] == ["new", "old"]  # created_at DESC

    decisions = await svc.get_recent(entry_type="decision")
    assert [r.title for r in decisions] == ["new"]


@pytest.mark.asyncio
async def test_get_recent_kst_date_from(db_session: AsyncSession):
    await _add(db_session, title="jun", kst_date=date(2026, 6, 30))
    await _add(db_session, title="jul", kst_date=date(2026, 7, 2))
    svc = SessionContextService(db_session)

    rows = await svc.get_recent(kst_date_from=date(2026, 7, 1))
    assert [r.title for r in rows] == ["jul"]
```

- [ ] **Step 2: 테스트 통과 확인**

Run: `uv run pytest tests/test_session_context_web_read.py -v`
Expected: PASS (2 passed) — 서비스 read는 이미 존재하므로 신규 구현 없이 통과.

- [ ] **Step 3: 커밋**

```bash
git add tests/test_session_context_web_read.py
git commit -m "test(ROB-664): session-context web-read integration smoke

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 프런트 타입 + API 클라이언트

**Files:**
- Create: `frontend/invest/src/types/analysisArtifacts.ts`
- Create: `frontend/invest/src/types/sessionContext.ts`
- Create: `frontend/invest/src/api/analysisArtifacts.ts`
- Create: `frontend/invest/src/api/sessionContext.ts`

**Interfaces:**
- Produces: `fetchArtifacts(params)`, `fetchArtifactDetail(id)`, `fetchRecentSessionContext(params)` + 타입.

- [ ] **Step 1: 타입 정의**

`frontend/invest/src/types/analysisArtifacts.ts`:

```typescript
// Mirrors app/schemas/analysis_artifact.py (snake_case).
export type ArtifactKind =
  | "screening_ranking"
  | "profit_taking_verdicts"
  | "support_resistance_map"
  | "flow_assessment"
  | "candidate_pool"
  | "session_summary"
  | "briefing";

export type ArtifactReadiness =
  | "screen_grade"
  | "not_decision_ready"
  | "ready_for_order_review"
  | "blocked";

export interface ArtifactMeta {
  id: number;
  artifact_uuid: string;
  market: "kr" | "us" | "crypto";
  kind: ArtifactKind;
  title: string;
  symbols: string[];
  as_of: string;
  valid_until: string | null;
  session_label: string | null;
  correlation_id: string | null;
  account_scope: string | null;
  content_hash: string | null;
  version: number;
  readiness_label: ArtifactReadiness | null;
  payload_size_bytes: number;
  is_stale: boolean;
  created_by: "claude" | "operator" | "system";
  created_at: string;
}

export interface ArtifactRead extends ArtifactMeta {
  payload: Record<string, unknown>;
}

export interface ArtifactListResponse {
  success: true;
  count: number;
  artifacts: ArtifactMeta[];
}

export interface ArtifactGetResponse {
  success: true;
  artifact: ArtifactRead;
}
```

`frontend/invest/src/types/sessionContext.ts`:

```typescript
// Mirrors app/schemas/session_context.py (snake_case).
export type SessionEntryType =
  | "plan"
  | "decision"
  | "deferred"
  | "rejected_candidate"
  | "constraint"
  | "open_question"
  | "next_action"
  | "handoff_note";

export interface SessionContextEntry {
  entry_uuid: string;
  kst_date: string;
  market: "kr" | "us" | "crypto";
  account_scope: string | null;
  entry_type: SessionEntryType;
  title: string;
  body: string;
  refs: Record<string, unknown>;
  created_by: "claude" | "operator" | "system";
  session_label: string | null;
  created_at: string;
}

export interface SessionContextRecentResponse {
  success: true;
  count: number;
  entries: SessionContextEntry[];
}
```

- [ ] **Step 2: API 클라이언트 작성**

`frontend/invest/src/api/analysisArtifacts.ts` — `frontend/invest/src/api/forecasts.ts` 패턴 미러:

```typescript
import type {
  ArtifactGetResponse,
  ArtifactKind,
  ArtifactListResponse,
  ArtifactReadiness,
} from "../types/analysisArtifacts";

const BASE = "/trading/api/invest/artifacts";

export async function fetchArtifacts(params: {
  market?: string;
  kind?: ArtifactKind;
  readinessLabel?: ArtifactReadiness;
  symbol?: string;
  includeStale?: boolean;
  limit?: number;
}): Promise<ArtifactListResponse> {
  const q = new URLSearchParams();
  if (params.market) q.set("market", params.market);
  if (params.kind) q.set("kind", params.kind);
  if (params.readinessLabel) q.set("readiness_label", params.readinessLabel);
  if (params.symbol) q.set("symbol", params.symbol);
  if (params.includeStale) q.set("include_stale", "true");
  if (params.limit) q.set("limit", String(params.limit));
  const res = await fetch(`${BASE}/?${q.toString()}`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(`fetchArtifacts failed: ${res.status}`);
  return res.json();
}

export async function fetchArtifactDetail(
  id: number | string,
): Promise<ArtifactGetResponse> {
  const res = await fetch(`${BASE}/${id}`, { credentials: "include" });
  if (!res.ok) throw new Error(`fetchArtifactDetail failed: ${res.status}`);
  return res.json();
}
```

`frontend/invest/src/api/sessionContext.ts`:

```typescript
import type {
  SessionContextRecentResponse,
  SessionEntryType,
} from "../types/sessionContext";

const BASE = "/trading/api/invest/session-context";

export async function fetchRecentSessionContext(params: {
  market?: string;
  accountScope?: string;
  entryType?: SessionEntryType;
  kstDateFrom?: string;
  limit?: number;
}): Promise<SessionContextRecentResponse> {
  const q = new URLSearchParams();
  if (params.market) q.set("market", params.market);
  if (params.accountScope) q.set("account_scope", params.accountScope);
  if (params.entryType) q.set("entry_type", params.entryType);
  if (params.kstDateFrom) q.set("kst_date_from", params.kstDateFrom);
  if (params.limit) q.set("limit", String(params.limit));
  const res = await fetch(`${BASE}/recent?${q.toString()}`, {
    credentials: "include",
  });
  if (!res.ok)
    throw new Error(`fetchRecentSessionContext failed: ${res.status}`);
  return res.json();
}
```

- [ ] **Step 3: 타입체크 확인**

Run: `cd frontend/invest && npx tsc --noEmit`
Expected: 신규 파일 관련 에러 없음 (기존 pre-existing 에러는 무시).

- [ ] **Step 4: 커밋**

```bash
git add frontend/invest/src/types/analysisArtifacts.ts frontend/invest/src/types/sessionContext.ts frontend/invest/src/api/analysisArtifacts.ts frontend/invest/src/api/sessionContext.ts
git commit -m "feat(ROB-664): frontend types + api clients for artifacts/session-context

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 아티팩트 패널 컴포넌트 + 테스트

**Files:**
- Create: `frontend/invest/src/components/insights/AnalysisArtifactPanel.tsx`
- Test: `frontend/invest/src/__tests__/AnalysisArtifactPanel.test.tsx`

**Interfaces:**
- Consumes: `fetchArtifacts`, `fetchArtifactDetail`, `ArtifactMeta`, `ArtifactRead`.
- Produces: named export `AnalysisArtifactPanel`. Root `data-testid="analysis-artifact-panel"`.

**Reference:** `frontend/invest/src/components/insights/ForecastCalibrationPanel.tsx`의 구조를 따른다 — `LoadState<T>` discriminated union, `useEffect` + `cancelled` guard, inline-style `th`/`td`, `Card`/`Pill` from `../../ds`, Korean copy.

- [ ] **Step 1: 컴포넌트 테스트 작성**

`frontend/invest/src/__tests__/AnalysisArtifactPanel.test.tsx`:

```typescript
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AnalysisArtifactPanel } from "../components/insights/AnalysisArtifactPanel";

const listBody = {
  success: true,
  count: 1,
  artifacts: [
    {
      id: 3,
      artifact_uuid: "u-3",
      market: "kr",
      kind: "screening_ranking",
      title: "KR 스크리닝",
      symbols: ["005930"],
      as_of: "2026-07-03T00:00:00+00:00",
      valid_until: null,
      session_label: null,
      correlation_id: null,
      account_scope: null,
      content_hash: "abc123def456",
      version: 2,
      readiness_label: "ready_for_order_review",
      payload_size_bytes: 128,
      is_stale: true,
      created_by: "claude",
      created_at: "2026-07-03T00:00:00+00:00",
    },
  ],
};

afterEach(() => vi.unstubAllGlobals());

describe("AnalysisArtifactPanel", () => {
  it("renders artifacts with stale badge and version", async () => {
    const fetchMock = vi.fn(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () => listBody,
    }));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter>
        <AnalysisArtifactPanel />
      </MemoryRouter>,
    );

    await waitFor(() => screen.getByText("KR 스크리닝"));
    expect(screen.getByTestId("analysis-artifact-panel")).toBeTruthy();
    expect(screen.getByText(/stale/i)).toBeTruthy();
    expect(screen.getByText(/v2/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd frontend/invest && npx vitest run src/__tests__/AnalysisArtifactPanel.test.tsx`
Expected: FAIL — 모듈 없음.

- [ ] **Step 3: 컴포넌트 구현**

`frontend/invest/src/components/insights/AnalysisArtifactPanel.tsx`:

```tsx
import { useEffect, useState } from "react";
import { fetchArtifactDetail, fetchArtifacts } from "../../api/analysisArtifacts";
import { Card, Pill } from "../../ds";
import type { ArtifactMeta, ArtifactRead } from "../../types/analysisArtifacts";

type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; error: string };

const th: React.CSSProperties = {
  textAlign: "left",
  padding: "6px 8px",
  fontSize: 12,
  opacity: 0.7,
  whiteSpace: "nowrap",
};
const td: React.CSSProperties = { padding: "6px 8px", fontSize: 13 };

function fmt(ts: string): string {
  return ts.replace("T", " ").slice(0, 16);
}

export function AnalysisArtifactPanel() {
  const [state, setState] = useState<LoadState<ArtifactMeta[]>>({
    status: "loading",
  });
  const [detail, setDetail] = useState<ArtifactRead | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchArtifacts({ includeStale: true, limit: 20 })
      .then((res) => {
        if (!cancelled) setState({ status: "ready", data: res.artifacts });
      })
      .catch((e) => {
        if (!cancelled) setState({ status: "error", error: String(e) });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function openDetail(id: number) {
    setDetail(null);
    const res = await fetchArtifactDetail(id);
    setDetail(res.artifact);
  }

  return (
    <Card data-testid="analysis-artifact-panel">
      <h3 style={{ margin: "0 0 8px" }}>분석 아티팩트</h3>
      {state.status === "loading" && <div>불러오는 중…</div>}
      {state.status === "error" && (
        <div style={{ color: "var(--danger)" }}>에러: {state.error}</div>
      )}
      {state.status === "ready" && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", width: "100%" }}>
            <thead>
              <tr>
                <th style={th}>종류</th>
                <th style={th}>제목</th>
                <th style={th}>시장</th>
                <th style={th}>as_of</th>
                <th style={th}>상태</th>
                <th style={th}>ver</th>
                <th style={th}>payload</th>
              </tr>
            </thead>
            <tbody>
              {state.data.map((a) => (
                <tr key={a.id}>
                  <td style={td}>
                    <Pill tone="paper" size="sm">
                      {a.kind}
                    </Pill>
                  </td>
                  <td style={td}>
                    <button
                      onClick={() => openDetail(a.id)}
                      style={{
                        background: "none",
                        border: "none",
                        color: "var(--link, #4a9)",
                        cursor: "pointer",
                        padding: 0,
                      }}
                    >
                      {a.title}
                    </button>
                  </td>
                  <td style={td}>{a.market}</td>
                  <td style={td}>{fmt(a.as_of)}</td>
                  <td style={td}>
                    {a.is_stale ? (
                      <Pill tone="loss" size="sm">
                        stale
                      </Pill>
                    ) : (
                      <Pill tone="gain" size="sm">
                        fresh
                      </Pill>
                    )}
                    {a.readiness_label && (
                      <Pill tone="paper" size="sm">
                        {a.readiness_label}
                      </Pill>
                    )}
                  </td>
                  <td style={td}>v{a.version}</td>
                  <td style={td}>{a.payload_size_bytes}B</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {detail && (
        <div style={{ marginTop: 12 }}>
          <h4 style={{ margin: "0 0 4px" }}>
            payload — {detail.title}{" "}
            {detail.content_hash && (
              <span style={{ opacity: 0.6, fontSize: 12 }}>
                #{detail.content_hash.slice(0, 12)}
              </span>
            )}
          </h4>
          <pre
            style={{
              maxHeight: 320,
              overflow: "auto",
              background: "var(--surface-2, #1113)",
              padding: 8,
              fontSize: 12,
              borderRadius: 4,
            }}
          >
            {JSON.stringify(detail.payload, null, 2)}
          </pre>
        </div>
      )}
    </Card>
  );
}
```

주의: `Card`가 `data-testid`를 DOM으로 전달하는지 확인. 만약 전달 안 하면 `<div data-testid=...>`로 감싸거나 `Card`의 wrapper prop 사용. `Pill` tone 값(`paper`/`gain`/`loss`)은 `ForecastCalibrationPanel.tsx`에서 검증된 값 재사용.

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd frontend/invest && npx vitest run src/__tests__/AnalysisArtifactPanel.test.tsx`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add frontend/invest/src/components/insights/AnalysisArtifactPanel.tsx frontend/invest/src/__tests__/AnalysisArtifactPanel.test.tsx
git commit -m "feat(ROB-664): AnalysisArtifactPanel with stale badge + payload viewer

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 세션 컨텍스트 타임라인 패널 + 테스트

**Files:**
- Create: `frontend/invest/src/components/insights/SessionContextTimelinePanel.tsx`
- Test: `frontend/invest/src/__tests__/SessionContextTimelinePanel.test.tsx`

**Interfaces:**
- Consumes: `fetchRecentSessionContext`, `SessionContextEntry`.
- Produces: named export `SessionContextTimelinePanel`. Root `data-testid="session-context-timeline-panel"`.

- [ ] **Step 1: 컴포넌트 테스트 작성**

`frontend/invest/src/__tests__/SessionContextTimelinePanel.test.tsx`:

```typescript
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionContextTimelinePanel } from "../components/insights/SessionContextTimelinePanel";

const body = {
  success: true,
  count: 1,
  entries: [
    {
      entry_uuid: "e-1",
      kst_date: "2026-07-03",
      market: "kr",
      account_scope: null,
      entry_type: "handoff_note",
      title: "다음 세션 인계",
      body: "삼성전자 매수 래더 절반 남음",
      refs: {},
      created_by: "claude",
      session_label: null,
      created_at: "2026-07-03T09:00:00+00:00",
    },
  ],
};

afterEach(() => vi.unstubAllGlobals());

describe("SessionContextTimelinePanel", () => {
  it("renders recent handoff entries with entry_type chip", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => body })),
    );

    render(<SessionContextTimelinePanel />);

    await waitFor(() => screen.getByText("다음 세션 인계"));
    expect(screen.getByTestId("session-context-timeline-panel")).toBeTruthy();
    expect(screen.getByText("handoff_note")).toBeTruthy();
  });
});
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd frontend/invest && npx vitest run src/__tests__/SessionContextTimelinePanel.test.tsx`
Expected: FAIL — 모듈 없음.

- [ ] **Step 3: 컴포넌트 구현**

`frontend/invest/src/components/insights/SessionContextTimelinePanel.tsx`:

```tsx
import { useEffect, useState } from "react";
import { fetchRecentSessionContext } from "../../api/sessionContext";
import { Card, Pill } from "../../ds";
import type { SessionContextEntry } from "../../types/sessionContext";

type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; error: string };

function fmt(ts: string): string {
  return ts.replace("T", " ").slice(0, 16);
}

export function SessionContextTimelinePanel() {
  const [state, setState] = useState<LoadState<SessionContextEntry[]>>({
    status: "loading",
  });

  useEffect(() => {
    let cancelled = false;
    fetchRecentSessionContext({ limit: 15 })
      .then((res) => {
        if (!cancelled) setState({ status: "ready", data: res.entries });
      })
      .catch((e) => {
        if (!cancelled) setState({ status: "error", error: String(e) });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Card data-testid="session-context-timeline-panel">
      <h3 style={{ margin: "0 0 8px" }}>최근 핸드오프</h3>
      {state.status === "loading" && <div>불러오는 중…</div>}
      {state.status === "error" && (
        <div style={{ color: "var(--danger)" }}>에러: {state.error}</div>
      )}
      {state.status === "ready" && state.data.length === 0 && (
        <div style={{ opacity: 0.6 }}>최근 세션 컨텍스트 없음</div>
      )}
      {state.status === "ready" && (
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {state.data.map((e) => (
            <li
              key={e.entry_uuid}
              style={{
                padding: "8px 0",
                borderBottom: "1px solid var(--hairline, #8882)",
              }}
            >
              <div
                style={{ display: "flex", gap: 6, alignItems: "center" }}
              >
                <Pill tone="paper" size="sm">
                  {e.entry_type}
                </Pill>
                <Pill tone="paper" size="sm">
                  {e.market}
                </Pill>
                <span style={{ opacity: 0.6, fontSize: 12 }}>
                  {e.kst_date} · {fmt(e.created_at)}
                </span>
              </div>
              <div style={{ fontWeight: 600, marginTop: 4 }}>{e.title}</div>
              <div style={{ fontSize: 13, opacity: 0.85, whiteSpace: "pre-wrap" }}>
                {e.body}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd frontend/invest && npx vitest run src/__tests__/SessionContextTimelinePanel.test.tsx`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add frontend/invest/src/components/insights/SessionContextTimelinePanel.tsx frontend/invest/src/__tests__/SessionContextTimelinePanel.test.tsx
git commit -m "feat(ROB-664): SessionContextTimelinePanel (최근 핸드오프 card)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: `DesktopInsightsPage`에 두 패널 배선

**Files:**
- Modify: `frontend/invest/src/pages/desktop/DesktopInsightsPage.tsx`
- Test: `frontend/invest/src/__tests__/DesktopInsightsPage.test.tsx` (Modify — 기존 존재)

**Interfaces:**
- Consumes: `AnalysisArtifactPanel`, `SessionContextTimelinePanel`.

- [ ] **Step 1: 페이지 배선**

`DesktopInsightsPage.tsx` 상단 import에 추가 (기존 `ForecastCalibrationPanel` import 근처):

```tsx
import { AnalysisArtifactPanel } from "../../components/insights/AnalysisArtifactPanel";
import { SessionContextTimelinePanel } from "../../components/insights/SessionContextTimelinePanel";
```

`<ForecastCalibrationPanel />` 렌더 위치(center grid) 바로 다음에 두 패널 추가:

```tsx
        <ForecastCalibrationPanel />
        <AnalysisArtifactPanel />
        <SessionContextTimelinePanel />
```

(정확한 위치는 파일의 center `<DesktopShell>` 그리드 내부 — `ForecastCalibrationPanel`이 렌더되는 곳과 동일 부모.)

- [ ] **Step 2: 페이지 테스트 갱신**

`DesktopInsightsPage.test.tsx`의 fetch mock이 URL 라우팅 방식이면, 신규 두 엔드포인트(`/artifacts`, `/session-context`)에 대해 `{ok:true, json: {success:true, count:0, artifacts:[], entries:[]}}` 형태 응답을 반환하도록 추가. 예:

```typescript
    if (url.includes("/session-context")) {
      return { ok: true, status: 200, json: async () => ({ success: true, count: 0, entries: [] }) };
    }
    if (url.includes("/artifacts")) {
      return { ok: true, status: 200, json: async () => ({ success: true, count: 0, artifacts: [] }) };
    }
```

기존 테스트가 특정 패널만 assert하면, 신규 패널이 렌더되는지 최소 `getByTestId("analysis-artifact-panel")` / `getByTestId("session-context-timeline-panel")` assert를 추가 (선택).

- [ ] **Step 3: 페이지 테스트 통과 확인**

Run: `cd frontend/invest && npx vitest run src/__tests__/DesktopInsightsPage.test.tsx`
Expected: PASS

- [ ] **Step 4: 커밋**

```bash
git add frontend/invest/src/pages/desktop/DesktopInsightsPage.tsx frontend/invest/src/__tests__/DesktopInsightsPage.test.tsx
git commit -m "feat(ROB-664): wire artifact + session-context panels into /insights

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: 전체 검증 + 정리

**Files:** (없음 — 검증 전용)

- [ ] **Step 1: 백엔드 신규/영향 테스트 전체**

Run:
```bash
uv run pytest tests/routers/test_invest_artifacts_router.py tests/routers/test_invest_session_context_router.py tests/test_analysis_artifact_web_read.py tests/test_session_context_web_read.py -v
```
Expected: 전부 PASS (integration은 로컬 DB 필요; 없으면 unit만 실행하고 통합은 CI에 위임 — 그 경우 로그로 명시).

- [ ] **Step 2: lint + typecheck (백엔드)**

Run: `make lint` (또는 `uv run ruff check app/routers/invest_artifacts.py app/routers/invest_session_context.py app/services/analysis_artifact.py && uv run ty check`)
Expected: green (신규 파일 관련 에러 0).

- [ ] **Step 3: 프런트 신규 테스트 + 타입체크**

Run:
```bash
cd frontend/invest && npx vitest run src/__tests__/AnalysisArtifactPanel.test.tsx src/__tests__/SessionContextTimelinePanel.test.tsx src/__tests__/DesktopInsightsPage.test.tsx && npx tsc --noEmit
```
Expected: 신규 3 테스트 PASS, 타입 에러 없음. (프런트 full-suite의 pre-existing 5 fail — calendar/coverage — 은 ROB-663 메모 기준 무관하게 실패하는 stale set이므로 무시하되, 내 변경으로 **새로** 깨진 게 없는지 `git stash` 대조로 확인.)

- [ ] **Step 4: 라우터 full-app 등록 재확인**

Run: `uv run python -c "from app.main import app; print([r.path for r in app.routes if '/invest/artifacts' in r.path or '/invest/session-context' in r.path])"`
Expected: 3개 경로 출력.

- [ ] **Step 5: PR 생성**

`superpowers:finishing-a-development-branch` 스킬로 마무리 (base `main`, migration 0, read-only). PR 본문에 안전 경계(read-only/migration 0/service-layer only) 명시 + Linear ROB-664 링크.

---

## Self-Review

**Spec coverage:**
- 아티팩트 뷰어(market/kind/readiness_label/symbol 필터 + is_stale 배지 + version/content_hash + payload JSON 뷰어) → Task 1(readiness 필터)+Task 2(라우터)+Task 6(패널). ✅
- 대용량 payload 목록 제외/상세만 로드 → Task 2 (`AnalysisArtifactMeta` list + `get` detail) + Task 6 (openDetail). ✅
- 세션 컨텍스트 타임라인(kst_date/market별 최근, entry_type 칩, "최근 핸드오프" 카드) → Task 3(라우터)+Task 4(통합)+Task 7(패널). ✅
- read-only/migration 0/service-layer only → 전 태스크 준수 (mutation 없음). ✅
- /insights 배치, 단일 PR → Task 8, Task 9. ✅

**Placeholder scan:** 모든 코드 스텝에 실제 코드 포함. "정확한 위치는 파일의 center grid"(Task 8)는 실행자가 grep으로 확인해야 하는 배선 지점 — 코드 자체는 완전 제공. ✅

**Type consistency:** 서비스 `list_artifacts(readiness_label=...)`(Task 1) ↔ 라우터 호출(Task 2) 일치. `AnalysisArtifactMeta`/`AnalysisArtifactRead`/`AnalysisArtifactListResponse`/`AnalysisArtifactGetResponse`/`SessionContextRecentResponse`/`SessionContextResponse` 스키마명 기존 파일과 일치(재사용). TS `ArtifactMeta`/`ArtifactRead` ↔ Pydantic 필드 대응. 컴포넌트 export명(`AnalysisArtifactPanel`/`SessionContextTimelinePanel`) ↔ import(Task 8) 일치. ✅

**주의(실행 중 확인 필요):**
- `Card`가 `data-testid` prop을 DOM에 전달하는지 — 미전달 시 wrapper `<div>` 필요 (Task 6/7 주석).
- `app/main.py`의 앱 노출 형태(`app` 인스턴스 vs `create_app()` 팩토리) — Task 2 Step 6 / Task 9 Step 4에서 실제 형태에 맞춤.
- `AccountScopeLiteral`/`MarketLiteral`의 정확한 import 경로는 `app.schemas.investment_reports` (스키마 파일에서 확인됨).
