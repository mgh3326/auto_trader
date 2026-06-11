# ROB-512 갭3 — KR/US 카테고리 정규화 섹터 + lazy fill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 스크리너 category를 KR/US 한글 업종으로 채운다 — 신규 `symbol_sectors` 정규화 테이블 + universe FK + MCP enrichment lazy fill.

**Architecture:** ① 신규 `symbol_sectors`(UNIQUE(market,source,source_key), name_kr/name_en 동시 보관) + `kr/us_symbol_universe.sector_id` FK (migration 1). ② 쓰기는 신규 `symbol_sectors_service` 2함수만. ③ lazy fill은 `enrich_snapshot_page`(이미 페이지 단위 per-symbol 외부호출 패턴 보유)에서 NULL 심볼만 KR=Naver upjong 링크 / US=yfinance info→정적 한글 매핑으로 채우고 응답 category 교체. ④ 로더 4곳은 기존 universe lookup을 LEFT JOIN으로 확장(DB-only).

**Tech Stack:** Python 3.13, SQLAlchemy async + Alembic(hand-written migration), BeautifulSoup(기존 `_fetch_html`), yfinance, pytest(`uv run pytest`, 실 PG `db_session` fixture).

**스펙:** `docs/plans/ROB-512-gap3-kr-sector-master-lazy-fill-spec.md` (확정 결정 6건 포함). 브랜치: `rob-512-gap3-kr-sector`.

---

## File Structure

| 파일 | 역할 | 변경 |
|---|---|---|
| `app/models/symbol_sectors.py` | SymbolSector ORM | **Create** |
| `app/models/kr_symbol_universe.py`, `app/models/us_symbol_universe.py` | sector_id/sector_updated_at | Modify |
| `app/models/__init__.py` | 모델 등록 | Modify |
| `alembic/versions/rob512_add_symbol_sectors.py` | migration | **Create** |
| `app/services/symbol_sectors_service.py` | get_or_create_sector / assign_symbol_sector | **Create** |
| `app/services/naver_finance/valuation.py:177-194` | `_parse_industry_info` upjong 수리 + sector_no | Modify |
| `app/services/us_sector_korean_map.py` | yfinance industry/sector → 한글 정적 매핑 | **Create** |
| `app/services/invest_view_model/screener_analysis_enrichment.py` | lazy fill 단계 | Modify |
| `app/services/invest_view_model/screener_service.py` | consec(:541-560)·flow(:761-779) lookup 확장 | Modify |
| `app/services/invest_view_model/double_buy_screener.py:169-181` | lookup 확장 | Modify |
| `app/services/invest_view_model/kr_fundamentals_tv_screener.py:529-537,641` | name_map 확장 + `_build_row` sector | Modify |
| 테스트 | 아래 각 Task 참조 | Create/Modify |

**기존 테스트 하니스 주의 (구현자 필독):**
- `tests/test_invest_view_model_screener_service.py`의 `_FakeSession`은 시퀀스 소진 시 빈 결과 반환(:144-148), autouse fixture(:57-100)가 `resolve_healthy_partition`을 가로채 결과 1개를 pop. **name lookup select에 컬럼을 추가해도 execute 횟수는 불변**이므로 기존 테스트는 영향 없음 — 단, 기존 fake row(`_name_row`)에 새 컬럼 속성이 없으므로 **로더 코드는 `getattr(row, "name_kr", None)` 방식의 안전 접근**을 써야 한다(속성 직접 접근 시 AttributeError → except가 name까지 삼켜 기존 테스트 깨짐).
- `tests/test_naver_finance.py:117-134`에 `_parse_industry_info` 기존 테스트 존재(구 셀렉터 fixture) — 구 셀렉터를 fallback으로 유지해 green 보존.
- db_session fixture는 공유 persistent test DB — TRUNCATE 금지, 합성 심볼(9-prefix 등)만 정리.

---

### Task 1: SymbolSector 모델 + universe 컬럼 + migration

**Files:**
- Create: `app/models/symbol_sectors.py`
- Modify: `app/models/kr_symbol_universe.py`, `app/models/us_symbol_universe.py`, `app/models/__init__.py`
- Create: `alembic/versions/rob512_add_symbol_sectors.py`
- Test: `tests/test_symbol_sectors_model.py`

- [ ] **Step 1: 실패하는 모델 테스트 작성**

`tests/test_symbol_sectors_model.py` 신규:

```python
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.symbol_sectors import SymbolSector

_TEST_SYMBOL = "915000"  # 9-prefix 합성 심볼 (공유 test DB 격리 관례)


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    async def _purge():
        await db_session.execute(
            sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol == _TEST_SYMBOL)
        )
        await db_session.execute(
            sa.delete(SymbolSector).where(SymbolSector.source_key == "999278")
        )
        await db_session.commit()

    await _purge()
    yield
    await _purge()


@pytest.mark.asyncio
async def test_symbol_sector_roundtrip_and_universe_fk(db_session):
    sector = SymbolSector(
        market="kr", source="naver_upjong", source_key="999278",
        name_kr="반도체와반도체장비", name_en=None,
    )
    db_session.add(sector)
    await db_session.flush()

    db_session.add(
        KRSymbolUniverse(
            symbol=_TEST_SYMBOL, name="테스트반도체", exchange="KOSPI",
            is_active=True, sector_id=sector.id,
            sector_updated_at=dt.datetime(2026, 6, 11, tzinfo=dt.UTC),
        )
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            sa.select(KRSymbolUniverse.symbol, SymbolSector.name_kr)
            .outerjoin(SymbolSector, KRSymbolUniverse.sector_id == SymbolSector.id)
            .where(KRSymbolUniverse.symbol == _TEST_SYMBOL)
        )
    ).one()
    assert row.name_kr == "반도체와반도체장비"


@pytest.mark.asyncio
async def test_symbol_sector_unique_market_source_key(db_session):
    db_session.add(
        SymbolSector(market="kr", source="naver_upjong", source_key="999278",
                     name_kr="반도체와반도체장비")
    )
    await db_session.flush()
    db_session.add(
        SymbolSector(market="kr", source="naver_upjong", source_key="999278",
                     name_kr="중복")
    )
    with pytest.raises(sa.exc.IntegrityError):
        await db_session.flush()
    await db_session.rollback()
```

- [ ] **Step 2: 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-512 && uv run pytest tests/test_symbol_sectors_model.py -v`
Expected: FAIL — `ModuleNotFoundError: app.models.symbol_sectors`

- [ ] **Step 3: 모델 작성**

`app/models/symbol_sectors.py` 신규 (`kr_symbol_universe.py`의 created/updated_at 패턴 미러):

```python
from __future__ import annotations

from datetime import datetime

from sqlalchemy import TIMESTAMP, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SymbolSector(Base):
    """ROB-512 갭3: 시장별 섹터 마스터 (KR=Naver upjong / US=yfinance industry).

    source_key가 안정 식별자 — KR은 Naver 업종번호("278")라 업종명 개명에도
    identity 유지, US는 yfinance industry 영문 원문. 표시 규칙은
    name_kr ?? name_en ?? "-" (US 한글 매핑 미스는 name_kr=NULL, fake 금지).
    """

    __tablename__ = "symbol_sectors"
    __table_args__ = (
        UniqueConstraint(
            "market", "source", "source_key",
            name="uq_symbol_sectors_market_source_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    source_key: Mapped[str] = mapped_column(String(100), nullable=False)
    name_kr: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name_en: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )
```

- [ ] **Step 4: universe 모델 2곳에 컬럼 추가**

`app/models/kr_symbol_universe.py` — `is_active` 줄 뒤에 (import에 `ForeignKey`, `Integer` 추가):

```python
    # ROB-512 갭3: 섹터 마스터 FK. NULL = 미워밍(lazy fill 대상). sync는 이
    # 컬럼을 건드리지 않는다(_apply_snapshot 필드 단위 갱신).
    sector_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("symbol_sectors.id"), nullable=True
    )
    sector_updated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
```

`app/models/us_symbol_universe.py` — `is_common_stock` 줄 뒤에 동일 2컬럼(동일 코드, 동일 주석).

`app/models/__init__.py` — 알파벳 순서에 맞춰 추가:

```python
from .symbol_sectors import SymbolSector
```

(파일 하단 `__all__`이 있으면 `"SymbolSector"`도 추가.)

- [ ] **Step 5: migration 작성**

먼저 현재 head 확인: `uv run alembic heads` → 출력된 단일 head revision id를 아래 `down_revision`에 넣는다 (2-head면 먼저 해소 — ⚠️ 과거 PR들에서 반복된 함정).

`alembic/versions/rob512_add_symbol_sectors.py` 신규 (rob422 hand-written 컨벤션):

```python
"""add symbol_sectors + universe sector FK (ROB-512 gap3)

Revision ID: rob512_symbol_sectors
Revises: <alembic heads 출력값>
Create Date: 2026-06-11 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "rob512_symbol_sectors"
down_revision: str | None = "<alembic heads 출력값>"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "symbol_sectors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("source_key", sa.String(length=100), nullable=False),
        sa.Column("name_kr", sa.String(length=100), nullable=True),
        sa.Column("name_en", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "market", "source", "source_key",
            name="uq_symbol_sectors_market_source_key",
        ),
    )
    for table in ("kr_symbol_universe", "us_symbol_universe"):
        op.add_column(table, sa.Column("sector_id", sa.Integer(), nullable=True))
        op.add_column(
            table,
            sa.Column("sector_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )
        op.create_foreign_key(
            f"fk_{table}_sector_id", table, "symbol_sectors",
            ["sector_id"], ["id"],
        )


def downgrade() -> None:
    for table in ("kr_symbol_universe", "us_symbol_universe"):
        op.drop_constraint(f"fk_{table}_sector_id", table, type_="foreignkey")
        op.drop_column(table, "sector_updated_at")
        op.drop_column(table, "sector_id")
    op.drop_table("symbol_sectors")
```

- [ ] **Step 6: 로컬 DB에 적용 + 테스트 통과 확인**

Run: `uv run alembic upgrade head && uv run alembic heads`
Expected: 에러 없음, 단일 head `rob512_symbol_sectors`.

Run: `uv run pytest tests/test_symbol_sectors_model.py -v`
Expected: 2 PASS.

(참고: 테스트 DB가 별도라면 그 DB에도 upgrade 필요 — `db_session` fixture가 쓰는 DATABASE_URL 기준. ROB-407 교훈: CI는 create_all 경로라 migration 미적용이어도 테이블이 생긴다.)

- [ ] **Step 7: Commit**

```bash
git add app/models/symbol_sectors.py app/models/kr_symbol_universe.py app/models/us_symbol_universe.py app/models/__init__.py alembic/versions/rob512_add_symbol_sectors.py tests/test_symbol_sectors_model.py
git commit -m "feat(ROB-512): symbol_sectors 테이블 + universe sector FK (gap3)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 2: symbol_sectors_service (쓰기 전용 서비스)

**Files:**
- Create: `app/services/symbol_sectors_service.py`
- Test: `tests/test_symbol_sectors_service.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_symbol_sectors_service.py` 신규:

```python
from __future__ import annotations

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.symbol_sectors import SymbolSector
from app.services.symbol_sectors_service import (
    assign_symbol_sector,
    get_or_create_sector,
)

_TEST_SYMBOL = "916000"


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    async def _purge():
        await db_session.execute(
            sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol == _TEST_SYMBOL)
        )
        await db_session.execute(
            sa.delete(SymbolSector).where(SymbolSector.source_key.like("999%"))
        )
        await db_session.commit()

    await _purge()
    yield
    await _purge()


@pytest.mark.asyncio
async def test_get_or_create_is_idempotent_and_tracks_rename(db_session):
    sid1 = await get_or_create_sector(
        db_session, market="kr", source="naver_upjong",
        source_key="999278", name_kr="반도체와반도체장비",
    )
    sid2 = await get_or_create_sector(
        db_session, market="kr", source="naver_upjong",
        source_key="999278", name_kr="반도체와반도체장비",
    )
    assert sid1 == sid2  # 동일 키 → 같은 id

    # 개명 추적: 같은 키, 새 이름 → 같은 id, name_kr 갱신
    sid3 = await get_or_create_sector(
        db_session, market="kr", source="naver_upjong",
        source_key="999278", name_kr="반도체",
    )
    assert sid3 == sid1
    row = (
        await db_session.execute(
            sa.select(SymbolSector).where(SymbolSector.id == sid1)
        )
    ).scalar_one()
    assert row.name_kr == "반도체"


@pytest.mark.asyncio
async def test_get_or_create_rejects_unknown_market(db_session):
    with pytest.raises(ValueError):
        await get_or_create_sector(
            db_session, market="crypto", source="naver_upjong",
            source_key="9991", name_kr="x",
        )


@pytest.mark.asyncio
async def test_assign_updates_existing_symbol_and_ignores_missing(db_session):
    db_session.add(
        KRSymbolUniverse(
            symbol=_TEST_SYMBOL, name="테스트", exchange="KOSPI", is_active=True
        )
    )
    await db_session.flush()
    sid = await get_or_create_sector(
        db_session, market="kr", source="naver_upjong",
        source_key="999285", name_kr="방송과엔터테인먼트",
    )

    assert await assign_symbol_sector(
        db_session, market="kr", symbol=_TEST_SYMBOL, sector_id=sid
    ) is True
    row = (
        await db_session.execute(
            sa.select(KRSymbolUniverse).where(KRSymbolUniverse.symbol == _TEST_SYMBOL)
        )
    ).scalar_one()
    assert row.sector_id == sid
    assert row.sector_updated_at is not None

    # 미존재 심볼 → False, INSERT 없음 (universe 생성은 sync 책임)
    assert await assign_symbol_sector(
        db_session, market="kr", symbol="917999", sector_id=sid
    ) is False
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_symbol_sectors_service.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.symbol_sectors_service`

- [ ] **Step 3: 서비스 구현**

`app/services/symbol_sectors_service.py` 신규:

```python
"""ROB-512 갭3: symbol_sectors 쓰기 전용 서비스.

모든 섹터 쓰기는 이 모듈의 두 함수만 사용한다. universe 행 INSERT는 하지
않는다(행 생성은 시장별 sync의 책임). 동시 enrichment의 중복 생성 경합은
ON CONFLICT DO NOTHING으로 흡수한다.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.symbol_sectors import SymbolSector
from app.models.us_symbol_universe import USSymbolUniverse

logger = logging.getLogger(__name__)

_UNIVERSE_BY_MARKET = {"kr": KRSymbolUniverse, "us": USSymbolUniverse}


def _require_market(market: str) -> None:
    if market not in _UNIVERSE_BY_MARKET:
        raise ValueError(f"unsupported market for symbol sectors: {market!r}")


async def get_or_create_sector(
    db: AsyncSession,
    *,
    market: str,
    source: str,
    source_key: str,
    name_kr: str | None = None,
    name_en: str | None = None,
) -> int:
    """UNIQUE(market,source,source_key)로 get-or-create하고 id를 반환.

    기존 행의 이름이 새 값과 다르면 갱신한다(소스 측 개명 추적). None 인자는
    기존 값을 지우지 않는다.
    """
    _require_market(market)
    await db.execute(
        pg_insert(SymbolSector)
        .values(
            market=market, source=source, source_key=source_key,
            name_kr=name_kr, name_en=name_en,
        )
        .on_conflict_do_nothing(
            constraint="uq_symbol_sectors_market_source_key"
        )
    )
    row = (
        await db.execute(
            sa.select(SymbolSector).where(
                SymbolSector.market == market,
                SymbolSector.source == source,
                SymbolSector.source_key == source_key,
            )
        )
    ).scalar_one()
    changed = False
    if name_kr is not None and row.name_kr != name_kr:
        row.name_kr = name_kr
        changed = True
    if name_en is not None and row.name_en != name_en:
        row.name_en = name_en
        changed = True
    if changed:
        await db.flush()
    return row.id


async def assign_symbol_sector(
    db: AsyncSession, *, market: str, symbol: str, sector_id: int
) -> bool:
    """universe 행의 sector_id/sector_updated_at만 갱신. 미존재 심볼은 False."""
    _require_market(market)
    model = _UNIVERSE_BY_MARKET[market]
    result = await db.execute(
        sa.update(model)
        .where(model.symbol == symbol)
        .values(sector_id=sector_id, sector_updated_at=datetime.now(UTC))
    )
    await db.flush()
    return bool(result.rowcount)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_symbol_sectors_service.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/symbol_sectors_service.py tests/test_symbol_sectors_service.py
git commit -m "feat(ROB-512): symbol_sectors_service — get_or_create/assign 쓰기 전용 (gap3)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 3: sync 보존 회귀 테스트 (KR + US)

**Files:**
- Test: `tests/test_symbol_universe_sector_preservation.py`

코드 변경 없음 — 두 sync의 `_apply_snapshot`이 필드 단위 갱신이라 sector_id를 보존한다는 사실(스펙 §1 검증 사항)을 회귀 가드로 고정한다.

- [ ] **Step 1: 테스트 작성**

`tests/test_symbol_universe_sector_preservation.py` 신규:

```python
"""ROB-512 갭3: universe sync가 lazy-fill된 sector_id를 지우지 않음을 고정.

kr/us _apply_snapshot 둘 다 name/exchange(/nxt_eligible/name_kr/name_en)/is_active만
필드 단위로 갱신한다 — 통째 upsert로 바뀌면 이 테스트가 깨져서 알린다.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.symbol_sectors import SymbolSector
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.kr_symbol_universe_service import (
    _apply_snapshot as kr_apply_snapshot,
)
from app.services.kr_symbol_universe_service import _UniverseRow as KRRow
from app.services.us_symbol_universe_service import (
    _apply_snapshot as us_apply_snapshot,
)
from app.services.us_symbol_universe_service import _UniverseRow as USRow

_KR_SYMBOL = "918000"
_US_SYMBOL = "ZZROBTST"


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    async def _purge():
        await db_session.execute(
            sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol == _KR_SYMBOL)
        )
        await db_session.execute(
            sa.delete(USSymbolUniverse).where(USSymbolUniverse.symbol == _US_SYMBOL)
        )
        await db_session.execute(
            sa.delete(SymbolSector).where(SymbolSector.source_key.like("999%"))
        )
        await db_session.commit()

    await _purge()
    yield
    await _purge()


async def _make_sector(db_session, market: str) -> int:
    sector = SymbolSector(
        market=market, source="naver_upjong" if market == "kr" else "yfinance_industry",
        source_key="999300", name_kr="테스트업종", name_en="TestIndustry",
    )
    db_session.add(sector)
    await db_session.flush()
    return sector.id


@pytest.mark.asyncio
async def test_kr_sync_preserves_sector_id(db_session):
    sid = await _make_sector(db_session, "kr")
    db_session.add(
        KRSymbolUniverse(
            symbol=_KR_SYMBOL, name="옛이름", exchange="KOSPI",
            is_active=True, sector_id=sid,
        )
    )
    await db_session.flush()

    # 이름이 바뀐 snapshot으로 sync (해당 심볼만 포함하면 다른 행은 비활성화
    # 되지만, _clean이 우리 행만 만들었고 공유 DB의 타 행 비활성화는 같은
    # 세션 내 flush 후 rollback 가능하도록 commit하지 않는다)
    await kr_apply_snapshot(
        db_session,
        {_KR_SYMBOL: KRRow(symbol=_KR_SYMBOL, name="새이름", exchange="KOSPI",
                           nxt_eligible=False)},
    )
    row = (
        await db_session.execute(
            sa.select(KRSymbolUniverse).where(KRSymbolUniverse.symbol == _KR_SYMBOL)
        )
    ).scalar_one()
    assert row.name == "새이름"
    assert row.sector_id == sid  # 보존!
    await db_session.rollback()  # 타 행 deactivation 원복


@pytest.mark.asyncio
async def test_us_sync_preserves_sector_id(db_session):
    sid = await _make_sector(db_session, "us")
    db_session.add(
        USSymbolUniverse(
            symbol=_US_SYMBOL, exchange="NASDAQ", name_kr="옛", name_en="Old",
            is_active=True, sector_id=sid,
        )
    )
    await db_session.flush()

    await us_apply_snapshot(
        db_session,
        {_US_SYMBOL: USRow(symbol=_US_SYMBOL, exchange="NASDAQ",
                           name_kr="새", name_en="New")},
    )
    row = (
        await db_session.execute(
            sa.select(USSymbolUniverse).where(USSymbolUniverse.symbol == _US_SYMBOL)
        )
    ).scalar_one()
    assert row.name_en == "New"
    assert row.sector_id == sid  # 보존!
    await db_session.rollback()
```

⚠️ `_apply_snapshot`은 snapshot에 없는 기존 행을 `is_active=False`로 바꾼다 — 공유 test DB 오염 방지를 위해 **commit하지 않고 검증 후 rollback**한다(위 코드 그대로). rollback이 검증을 깨지 않도록 단언을 rollback 앞에 둔다.

- [ ] **Step 2: 통과 확인**

Run: `uv run pytest tests/test_symbol_universe_sector_preservation.py -v`
Expected: 2 PASS (이미 보존되는 동작의 가드 — 즉시 green이 정상).

- [ ] **Step 3: Commit**

```bash
git add tests/test_symbol_universe_sector_preservation.py
git commit -m "test(ROB-512): universe sync가 sector_id를 보존함을 회귀 가드로 고정 (gap3)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 4: KR 파서 수리 — upjong 셀렉터 + 업종번호

**Files:**
- Modify: `app/services/naver_finance/valuation.py:177-194` (`_parse_industry_info`)
- Test: `tests/test_naver_finance.py` (기존 `_parse_industry_info` 테스트 클래스에 추가)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_naver_finance.py`의 `_parse_industry_info` 테스트 클래스(:117 부근)에 추가:

```python
    def test_parses_sector_from_upjong_link_with_number(self):
        """ROB-512: 현행 페이지의 동종업종비교 upjong 링크에서 한글 업종명과
        안정 식별자(업종번호 no=)를 추출한다. 구 셀렉터(div.tab_con1 em a)는
        현행 페이지에서 죽어 있다(2026-06-11 라이브 확인)."""
        html = (
            '<div class="section trade_compare"><h4 class="h_sub sub_tit7">'
            "<span>동종업종비교</span><em>(업종명 : "
            '<a href="/sise/sise_group_detail.naver?type=upjong&amp;no=278">'
            "반도체와반도체장비</a><span class="bar">｜</span>)</em></h4></div>'
        )
        soup = BeautifulSoup(html, "html.parser")
        result = naver_finance._parse_industry_info(soup)
        assert result["sector"] == "반도체와반도체장비"
        assert result["sector_no"] == "278"

    def test_sector_no_none_when_no_upjong_link(self):
        soup = BeautifulSoup("<div>업종 정보 없음</div>", "html.parser")
        result = naver_finance._parse_industry_info(soup)
        assert result["sector"] is None
        assert result["sector_no"] is None
```

(클래스 내 기존 테스트의 import/도우미 스타일을 그대로 따른다 — `BeautifulSoup`, `naver_finance` alias는 파일 상단에 이미 있음.)

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_naver_finance.py -v -k "upjong or sector_no"`
Expected: 2 FAIL — `KeyError: 'sector_no'` 또는 sector None.

- [ ] **Step 3: 파서 수정**

`app/services/naver_finance/valuation.py:177-194`의 `_parse_industry_info` 전체를 교체 (`re`는 이미 import됨):

```python
def _parse_industry_info(soup: BeautifulSoup) -> dict[str, Any]:
    """Extract exchange type and sector from the main page soup.

    ROB-512: 한글 업종은 동종업종비교 헤더의 upjong 링크에서 추출한다 —
    링크 href의 ``no=`` 쿼리값이 Naver 업종번호(안정 식별자)다. 과거 셀렉터
    ``div.tab_con1 em a``는 현행 페이지에서 매칭되지 않아(2026-06-11 라이브
    확인, 전 종목 None) legacy fallback으로만 유지한다.
    """
    info: dict[str, Any] = {"exchange": None, "sector": None, "sector_no": None}

    code_info = soup.select_one("div.code")
    if code_info:
        code_text = code_info.get_text(strip=True)
        if "코스피" in code_text:
            info["exchange"] = "KOSPI"
        elif "코스닥" in code_text:
            info["exchange"] = "KOSDAQ"

    sector_elem = soup.select_one('a[href*="type=upjong"]')
    if sector_elem is not None:
        info["sector"] = sector_elem.get_text(strip=True) or None
        match = re.search(r"[?&]no=(\d+)", sector_elem.get("href") or "")
        if match:
            info["sector_no"] = match.group(1)
        return info

    # legacy fallback (구 페이지 구조 / 기존 fixture 호환)
    legacy_elem = soup.select_one("div.tab_con1 em a")
    if legacy_elem is not None:
        info["sector"] = legacy_elem.get_text(strip=True) or None
    return info
```

- [ ] **Step 4: 통과 확인 (파일 전체 — 기존 fixture 회귀 포함)**

Run: `uv run pytest tests/test_naver_finance.py -v`
Expected: 전부 PASS (구 셀렉터 fixture 테스트는 legacy fallback으로 green 유지).

- [ ] **Step 5: Commit**

```bash
git add app/services/naver_finance/valuation.py tests/test_naver_finance.py
git commit -m "fix(ROB-512): _parse_industry_info upjong 링크 셀렉터 수리 + 업종번호 추출 (gap3)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 5: US industry → 한글 정적 매핑 모듈

**Files:**
- Create: `app/services/us_sector_korean_map.py`
- Test: `tests/test_us_sector_korean_map.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_us_sector_korean_map.py` 신규:

```python
from __future__ import annotations

import pytest

from app.services.us_sector_korean_map import korean_sector_label


@pytest.mark.unit
def test_maps_known_industry_to_korean():
    assert korean_sector_label("Semiconductors") == "반도체"
    assert korean_sector_label("Banks - Regional") == "지방은행"


@pytest.mark.unit
def test_maps_known_sector_to_korean():
    assert korean_sector_label("Technology") == "기술"
    assert korean_sector_label("Healthcare") == "헬스케어"


@pytest.mark.unit
def test_unknown_returns_none_not_fake():
    # 매핑 미스는 None — 호출자가 영문 원문을 표시(fake 한글 금지)
    assert korean_sector_label("Quantum Flux Capacitors") is None
    assert korean_sector_label("") is None
    assert korean_sector_label(None) is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_us_sector_korean_map.py -v`
Expected: FAIL — ModuleNotFoundError.

- [ ] **Step 3: 매핑 모듈 작성**

`app/services/us_sector_korean_map.py` 신규 — yfinance `info["sector"]` 11종 전체 + 빈출 `info["industry"]` 정적 매핑. **미스는 None 반환(영문 fallback은 호출자 책임) — 이 사전이 전수가 아니어도 결함이 아니다**:

```python
"""ROB-512 갭3: yfinance sector/industry 영문 → 한글 표시명 정적 매핑.

표시용 베스트에포트 사전이다. 미스는 None — 호출자가 영문 원문을 그대로
표시한다(fake 한글 금지). 항목 추가는 자유(additive)다.
"""

from __future__ import annotations

# yfinance info["sector"] — 11종 전체
_SECTOR_KR: dict[str, str] = {
    "Technology": "기술",
    "Healthcare": "헬스케어",
    "Financial Services": "금융",
    "Consumer Cyclical": "임의소비재",
    "Consumer Defensive": "필수소비재",
    "Industrials": "산업재",
    "Energy": "에너지",
    "Utilities": "유틸리티",
    "Real Estate": "부동산",
    "Basic Materials": "소재",
    "Communication Services": "커뮤니케이션서비스",
}

# yfinance info["industry"] — 빈출 항목 (전수 아님; 미스는 영문 표시)
_INDUSTRY_KR: dict[str, str] = {
    "Semiconductors": "반도체",
    "Semiconductor Equipment & Materials": "반도체장비·소재",
    "Software - Application": "응용소프트웨어",
    "Software - Infrastructure": "인프라소프트웨어",
    "Consumer Electronics": "가전·전자기기",
    "Computer Hardware": "컴퓨터하드웨어",
    "Communication Equipment": "통신장비",
    "Information Technology Services": "IT서비스",
    "Electronic Components": "전자부품",
    "Internet Content & Information": "인터넷콘텐츠·정보",
    "Internet Retail": "인터넷쇼핑",
    "Entertainment": "엔터테인먼트",
    "Telecom Services": "통신서비스",
    "Advertising Agencies": "광고",
    "Electronic Gaming & Multimedia": "게임·멀티미디어",
    "Drug Manufacturers - General": "제약(대형)",
    "Drug Manufacturers - Specialty & Generic": "제약(스페셜티·제네릭)",
    "Biotechnology": "바이오테크",
    "Medical Devices": "의료기기",
    "Medical Instruments & Supplies": "의료기구·소모품",
    "Diagnostics & Research": "진단·연구",
    "Medical Care Facilities": "의료서비스시설",
    "Healthcare Plans": "건강보험",
    "Banks - Diversified": "대형은행",
    "Banks - Regional": "지방은행",
    "Capital Markets": "자본시장",
    "Asset Management": "자산운용",
    "Insurance - Diversified": "종합보험",
    "Insurance - Life": "생명보험",
    "Insurance - Property & Casualty": "손해보험",
    "Credit Services": "신용서비스",
    "Financial Data & Stock Exchanges": "금융데이터·거래소",
    "Auto Manufacturers": "자동차",
    "Auto Parts": "자동차부품",
    "Restaurants": "외식",
    "Apparel Retail": "의류소매",
    "Footwear & Accessories": "신발·액세서리",
    "Travel Services": "여행서비스",
    "Resorts & Casinos": "리조트·카지노",
    "Lodging": "호텔",
    "Specialty Retail": "전문소매",
    "Home Improvement Retail": "홈임프루브먼트소매",
    "Discount Stores": "할인점",
    "Grocery Stores": "식료품점",
    "Household & Personal Products": "생활용품",
    "Beverages - Non-Alcoholic": "음료(비주류)",
    "Beverages - Brewers": "맥주",
    "Beverages - Wineries & Distilleries": "주류(와인·증류)",
    "Confectioners": "제과",
    "Packaged Foods": "포장식품",
    "Tobacco": "담배",
    "Aerospace & Defense": "항공우주·방산",
    "Railroads": "철도",
    "Airlines": "항공사",
    "Farm & Heavy Construction Machinery": "농기계·중장비",
    "Specialty Industrial Machinery": "산업기계",
    "Electrical Equipment & Parts": "전기장비·부품",
    "Building Products & Equipment": "건축자재·설비",
    "Engineering & Construction": "엔지니어링·건설",
    "Waste Management": "폐기물처리",
    "Integrated Freight & Logistics": "종합물류",
    "Trucking": "트럭운송",
    "Marine Shipping": "해운",
    "Oil & Gas Integrated": "종합석유가스",
    "Oil & Gas E&P": "석유가스탐사·생산",
    "Oil & Gas Midstream": "석유가스미드스트림",
    "Oil & Gas Refining & Marketing": "정유·판매",
    "Oil & Gas Equipment & Services": "유전장비·서비스",
    "Utilities - Regulated Electric": "전력(규제)",
    "Utilities - Regulated Gas": "가스(규제)",
    "Utilities - Renewable": "신재생유틸리티",
    "REIT - Diversified": "리츠(복합)",
    "REIT - Residential": "리츠(주거)",
    "REIT - Retail": "리츠(리테일)",
    "REIT - Industrial": "리츠(산업)",
    "REIT - Specialty": "리츠(특수)",
    "REIT - Healthcare Facilities": "리츠(헬스케어)",
    "Real Estate Services": "부동산서비스",
    "Gold": "금",
    "Copper": "구리",
    "Steel": "철강",
    "Aluminum": "알루미늄",
    "Chemicals": "화학",
    "Specialty Chemicals": "정밀화학",
    "Agricultural Inputs": "농자재",
    "Solar": "태양광",
}


def korean_sector_label(value: str | None) -> str | None:
    """industry/sector 영문 원문 → 한글 표시명. 미스/빈 값은 None."""
    if not value:
        return None
    return _INDUSTRY_KR.get(value) or _SECTOR_KR.get(value)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_us_sector_korean_map.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/us_sector_korean_map.py tests/test_us_sector_korean_map.py
git commit -m "feat(ROB-512): US yfinance industry/sector 한글 정적 매핑 (gap3)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 6: enrichment lazy fill — `enrich_snapshot_page`에 sector 단계

**Files:**
- Modify: `app/services/invest_view_model/screener_analysis_enrichment.py`
- Test: `tests/services/test_screener_analysis_enrichment.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/services/test_screener_analysis_enrichment.py` 끝에 추가 (이 파일은 이미 `db_session`·`monkeypatch` 기반 async 테스트 보유; `enrich_snapshot_page` import 존재):

```python
# ---------------------------------------------------------------------------
# ROB-512 갭3: sector lazy fill
# ---------------------------------------------------------------------------

_SECTOR_TEST_KR_SYMBOL = "919100"


@pytest_asyncio.fixture
async def _sector_clean(db_session):
    import sqlalchemy as sa

    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.models.symbol_sectors import SymbolSector

    async def _purge():
        await db_session.execute(
            sa.delete(KRSymbolUniverse).where(
                KRSymbolUniverse.symbol == _SECTOR_TEST_KR_SYMBOL
            )
        )
        await db_session.execute(
            sa.delete(SymbolSector).where(SymbolSector.source_key.like("999%"))
        )
        await db_session.commit()

    await _purge()
    yield
    await _purge()


@pytest.mark.asyncio
async def test_sector_lazy_fill_kr_persists_and_replaces_category(
    db_session, session_factory, _sector_clean
):
    """NULL sector 심볼 → fake fetch로 (업종번호, 한글명) 획득 → persist →
    응답 category '-'가 한글로 교체. 두 번째 호출은 fetch 0 (DB hit)."""
    import sqlalchemy as sa

    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.services.invest_view_model.screener_analysis_enrichment import (
        enrich_snapshot_page,
    )

    db_session.add(
        KRSymbolUniverse(
            symbol=_SECTOR_TEST_KR_SYMBOL, name="테스트", exchange="KOSPI",
            is_active=True,
        )
    )
    await db_session.commit()

    calls: list[str] = []

    async def fake_fetch_kr(code: str):
        calls.append(code)
        return "999278", "반도체와반도체장비"

    rows = [{"symbol": _SECTOR_TEST_KR_SYMBOL, "market": "kr", "category": "-"}]

    async def no_opinions(**kwargs):
        return {"error": "analyst_consensus_unavailable"}

    out1 = await enrich_snapshot_page(
        rows=rows, market="kr", session_factory=session_factory,
        opinion_provider=no_opinions, fetch_kr_sector=fake_fetch_kr,
    )
    assert calls == [_SECTOR_TEST_KR_SYMBOL]
    assert out1["results"][0]["category"] == "반도체와반도체장비"
    assert out1["summary"]["sectorResolved"] == 1

    # persist 확인 + 2회차 fetch 0
    row = (
        await db_session.execute(
            sa.select(KRSymbolUniverse).where(
                KRSymbolUniverse.symbol == _SECTOR_TEST_KR_SYMBOL
            )
        )
    ).scalar_one()
    await db_session.refresh(row)
    assert row.sector_id is not None

    out2 = await enrich_snapshot_page(
        rows=rows, market="kr", session_factory=session_factory,
        opinion_provider=no_opinions, fetch_kr_sector=fake_fetch_kr,
    )
    assert calls == [_SECTOR_TEST_KR_SYMBOL]  # 추가 fetch 없음
    assert out2["results"][0]["category"] == "반도체와반도체장비"


@pytest.mark.asyncio
async def test_sector_lazy_fill_fails_open(db_session, session_factory, _sector_clean):
    """fetch 실패 → category 유지('-'), 워닝 없이도 결과 자체는 정상."""
    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.services.invest_view_model.screener_analysis_enrichment import (
        enrich_snapshot_page,
    )

    db_session.add(
        KRSymbolUniverse(
            symbol=_SECTOR_TEST_KR_SYMBOL, name="테스트", exchange="KOSPI",
            is_active=True,
        )
    )
    await db_session.commit()

    async def boom(code: str):
        raise RuntimeError("naver down")

    async def no_opinions(**kwargs):
        return {"error": "analyst_consensus_unavailable"}

    out = await enrich_snapshot_page(
        rows=[{"symbol": _SECTOR_TEST_KR_SYMBOL, "market": "kr", "category": "-"}],
        market="kr", session_factory=session_factory,
        opinion_provider=no_opinions, fetch_kr_sector=boom,
    )
    assert out["results"][0]["category"] == "-"
    assert out["summary"]["sectorResolved"] == 0
```

주의: 이 테스트 파일에 `session_factory` fixture가 없으면 파일 상단 fixture들을 확인해 동등한 `async_sessionmaker`를 쓰는 기존 fixture명(예: `db_session`이 쓰는 factory)을 재사용하거나, 파일 conftest의 factory fixture를 사용한다 — `enrich_snapshot_page`의 기존 테스트(`test_enrich_snapshot_page_adds_consensus_rsi_and_summary`)가 무엇을 넘기는지 그대로 따른다.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_screener_analysis_enrichment.py -v -k "sector_lazy"`
Expected: FAIL — `enrich_snapshot_page() got an unexpected keyword argument 'fetch_kr_sector'`

- [ ] **Step 3: 구현 — fetch 헬퍼 + lazy fill + 본문 통합**

`app/services/invest_view_model/screener_analysis_enrichment.py`에 추가/수정.

(a) 파일 상단 import 추가:

```python
from app.core.symbol import to_yahoo_symbol
from app.services.us_sector_korean_map import korean_sector_label
```

(b) `_rsi_by_symbol` 아래에 헬퍼들 추가:

```python
_SECTOR_FETCH_TIMEOUT = 4.5
_SECTOR_FETCH_CONCURRENCY = 4


async def _fetch_kr_sector(code: str) -> tuple[str | None, str | None]:
    """Naver 종목 메인 페이지에서 (업종번호, 한글 업종명)을 추출."""
    from app.services.naver_finance.valuation import (
        _fetch_html,
        _parse_industry_info,
    )

    soup = await _fetch_html(
        "https://finance.naver.com/item/main.naver", params={"code": code}
    )
    info = _parse_industry_info(soup)
    return info.get("sector_no"), info.get("sector")


async def _fetch_us_sector(symbol: str) -> tuple[str | None, str | None]:
    """yfinance info에서 (industry, sector) 영문 원문을 추출 (DB 심볼 입력)."""
    import yfinance as yf

    def _sync() -> tuple[str | None, str | None]:
        info = yf.Ticker(to_yahoo_symbol(symbol)).info or {}
        return info.get("industry") or None, info.get("sector") or None

    return await asyncio.to_thread(_sync)


async def _sector_labels_for_page(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    market: str,
    symbols: list[str],
    fetch_kr_sector: Callable[..., Any],
    fetch_us_sector: Callable[..., Any],
) -> dict[str, str]:
    """워밍된 섹터는 DB에서, NULL은 lazy fetch→persist 후 표시 라벨을 반환.

    실패는 전부 삼키고 해당 심볼만 빠진 dict를 반환한다(fail-open) —
    스크리너 응답 자체는 영향받지 않는다.
    """
    if not symbols or market not in {"kr", "us"}:
        return {}

    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.models.symbol_sectors import SymbolSector
    from app.models.us_symbol_universe import USSymbolUniverse
    from app.services.symbol_sectors_service import (
        assign_symbol_sector,
        get_or_create_sector,
    )

    universe = KRSymbolUniverse if market == "kr" else USSymbolUniverse
    labels: dict[str, str] = {}
    missing: list[str] = []

    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(
                    universe.symbol, SymbolSector.name_kr, SymbolSector.name_en
                )
                .outerjoin(SymbolSector, universe.sector_id == SymbolSector.id)
                .where(universe.symbol.in_(symbols))
            )
        ).all()
        known = {row.symbol for row in rows}
        for row in rows:
            label = row.name_kr or row.name_en
            if label:
                labels[row.symbol] = label
            else:
                missing.append(row.symbol)
        # universe에 없는 심볼은 fetch 대상에서 제외(assign이 어차피 불가)
        missing = [s for s in missing if s in known]

    if not missing:
        return labels

    sem = asyncio.Semaphore(_SECTOR_FETCH_CONCURRENCY)
    fetched: dict[str, tuple[str, str | None, str | None]] = {}
    # 값: (source_key, name_kr, name_en)

    async def _one(symbol: str) -> None:
        async with sem:
            try:
                if market == "kr":
                    no, name = await asyncio.wait_for(
                        fetch_kr_sector(symbol), timeout=_SECTOR_FETCH_TIMEOUT
                    )
                    if no and name:
                        fetched[symbol] = (str(no), name, None)
                else:
                    industry, sector = await asyncio.wait_for(
                        fetch_us_sector(symbol), timeout=_SECTOR_FETCH_TIMEOUT
                    )
                    raw = industry or sector
                    if raw:
                        fetched[symbol] = (raw, korean_sector_label(raw), raw)
            except Exception:  # noqa: BLE001 — per-symbol fail-open
                return

    await asyncio.gather(*[_one(s) for s in missing])
    if not fetched:
        return labels

    source = "naver_upjong" if market == "kr" else "yfinance_industry"
    try:
        async with session_factory() as db:
            for symbol, (source_key, name_kr, name_en) in fetched.items():
                sector_id = await get_or_create_sector(
                    db, market=market, source=source, source_key=source_key,
                    name_kr=name_kr, name_en=name_en,
                )
                await assign_symbol_sector(
                    db, market=market, symbol=symbol, sector_id=sector_id
                )
                label = name_kr or name_en
                if label:
                    labels[symbol] = label
            await db.commit()
    except Exception:  # noqa: BLE001 — persist 실패도 fail-open (라벨만 미반영)
        return labels
    return labels
```

(`Callable` import는 파일 상단 `collections.abc`에 이미 있음 — 없으면 추가. `sa`는 import되어 있음.)

(c) `enrich_snapshot_page` 시그니처에 injectable fetcher 추가 + 본문 통합:

```python
async def enrich_snapshot_page(
    *,
    rows: list[dict[str, Any]],
    market: str,
    session_factory: async_sessionmaker[AsyncSession],
    opinion_provider: OpinionProvider = handle_get_investment_opinions,
    fetch_kr_sector: Callable[..., Any] = _fetch_kr_sector,
    fetch_us_sector: Callable[..., Any] = _fetch_us_sector,
) -> dict[str, Any]:
```

summary 초기화에 `"sectorResolved": 0` 추가. RSI 블록 뒤에:

```python
    sector_labels: dict[str, str] = {}
    try:
        sector_labels = await _sector_labels_for_page(
            session_factory=session_factory, market=market, symbols=symbols,
            fetch_kr_sector=fetch_kr_sector, fetch_us_sector=fetch_us_sector,
        )
    except Exception:  # noqa: BLE001
        summary["warnings"].append("sector_enrichment_unavailable")
```

row 루프에서 `enriched.append({**row, ...})` 직전에:

```python
        sector_label = sector_labels.get(symbol)
        if sector_label is not None:
            summary["sectorResolved"] += 1
```

그리고 append dict에 category 교체 추가:

```python
        enriched.append(
            {
                **row,
                **(
                    {"category": sector_label}
                    if sector_label and (row.get("category") or "-") == "-"
                    else {}
                ),
                "analystLabel": build_analyst_label(consensus, warnings=warnings),
                "analysisContext": context.model_dump(mode="json"),
            }
        )
```

- [ ] **Step 4: 통과 확인 (신규 + 기존 회귀)**

Run: `uv run pytest tests/services/test_screener_analysis_enrichment.py -v`
Expected: 전부 PASS — 기존 enrich 테스트들은 fetcher 기본값이 실 함수지만 **universe에 없는 합성 심볼은 fetch 대상에서 제외**되므로 실 HTTP 없이 green이어야 한다. 깨지면 missing 필터(`s in known`)부터 의심.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/screener_analysis_enrichment.py tests/services/test_screener_analysis_enrichment.py
git commit -m "feat(ROB-512): enrich_snapshot_page sector lazy fill — KR Naver/US yfinance, fail-open (gap3)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 7: 로더 배선 — 4곳 universe lookup을 sector join으로 확장

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py` (consec :541-560, flow :761-779)
- Modify: `app/services/invest_view_model/double_buy_screener.py:169-181`
- Modify: `app/services/invest_view_model/kr_fundamentals_tv_screener.py` (:529-537 name_map, :410+ `_build_row`, :641 호출)
- Test: `tests/test_invest_view_model_screener_service.py`, `tests/test_invest_view_model_double_buy_screener.py`, `tests/test_fundamentals_screener.py`(fundamentals 로더 기존 테스트 파일 — `_build_row` 직접 테스트가 있으면 그 파일)

공통 패턴 — 기존 name lookup select를 outerjoin으로 확장하고 **fake row 호환을 위해 getattr 안전 접근**:

```python
# BEFORE (각 로더 공통 형태)
sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(...)
# AFTER
sa.select(
    KRSymbolUniverse.symbol,
    KRSymbolUniverse.name,
    SymbolSector.name_kr.label("sector_name_kr"),
    SymbolSector.name_en.label("sector_name_en"),
).outerjoin(
    SymbolSector, KRSymbolUniverse.sector_id == SymbolSector.id
).where(...)  # where 절은 기존 그대로
```

맵 구성 (name_map 만들던 곳 바로 옆):

```python
sector_map = {
    row.symbol: label
    for row in name_result.all()
    if (
        label := (
            getattr(row, "sector_name_kr", None)
            or getattr(row, "sector_name_en", None)
        )
    )
}
```

⚠️ `name_result.all()`은 1회만 호출 가능 — 기존 코드가 이미 `.all()`을 변수에 담지 않았다면 `rows = name_result.all()`로 받아 name/sector 두 맵을 같은 리스트에서 만든다.

row dict에는 `"sector": sector_map.get(snap.symbol)` (또는 double_buy는 `sector_map.get(sym)`) 추가 — 포맷터(`row.get("sector") or row.get("category") or "-"`)가 자동 인식.

- [ ] **Step 1: 실패하는 테스트 — flow 로더**

`tests/test_invest_view_model_screener_service.py`의 ROB-512 섹션에 추가 (PR1 테스트들 뒤). `_name_row` 옆에 헬퍼 추가:

```python
def _name_sector_row(
    symbol: str, name: str, sector_kr: str | None = None, sector_en: str | None = None
) -> Any:
    return type(
        "NameSectorRow",
        (),
        {
            "symbol": symbol,
            "name": name,
            "sector_name_kr": sector_kr,
            "sector_name_en": sector_en,
        },
    )()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_investor_flow_rows_carry_master_sector() -> None:
    """ROB-512 갭3: name lookup join으로 sector가 row에 실려 category가
    한글 업종으로 렌더된다."""
    from app.services.invest_view_model.screener_service import (
        _load_investor_flow_discovery_from_snapshots,
    )

    snapshot_date = date(2026, 5, 15)
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[snapshot_date]),
            _FakeExecuteResult(
                scalar_rows=[
                    _FakeInvestorFlowSnapshot(
                        symbol="005930", snapshot_date=snapshot_date
                    )
                ]
            ),
            _FakeExecuteResult(
                rows=[_name_sector_row("005930", "삼성전자", "반도체와반도체장비")]
            ),
        ]
    )

    load_result = await _load_investor_flow_discovery_from_snapshots(
        session, market="kr", limit=20
    )
    assert load_result is not None
    assert load_result.rows[0]["sector"] == "반도체와반도체장비"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py -v -k "carry_master_sector"`
Expected: FAIL — `KeyError: 'sector'`.

- [ ] **Step 3: flow 로더 구현**

`screener_service.py` flow 로더(:761-779)의 name lookup을 공통 패턴으로 확장 (파일 내 lookup 직전에 `from app.models.symbol_sectors import SymbolSector` 지역 import — 이 함수의 기존 지역 import 스타일 유지):

```python
        try:
            name_result = await session.execute(
                sa.select(
                    KRSymbolUniverse.symbol,
                    KRSymbolUniverse.name,
                    SymbolSector.name_kr.label("sector_name_kr"),
                    SymbolSector.name_en.label("sector_name_en"),
                )
                .outerjoin(
                    SymbolSector, KRSymbolUniverse.sector_id == SymbolSector.id
                )
                .where(
                    KRSymbolUniverse.symbol.in_(candidate_symbols),
                    KRSymbolUniverse.is_active.is_(True),
                )
            )
            _name_rows = name_result.all()
            symbol_names = {row.symbol: row.name for row in _name_rows}
            sector_map = {
                row.symbol: label
                for row in _name_rows
                if (
                    label := (
                        getattr(row, "sector_name_kr", None)
                        or getattr(row, "sector_name_en", None)
                    )
                )
            }
```

(`sector_map: dict[str, str] = {}`를 try 블록 위에서 초기화.) row dict에 `"sector": sector_map.get(snap.symbol),` 추가.

- [ ] **Step 4: flow 테스트 통과 + 파일 회귀 확인**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py -v`
Expected: 전부 PASS (기존 `_name_row` fake는 getattr 기본값 None으로 무해).

- [ ] **Step 5: consec 로더 동일 적용 + 테스트**

`screener_service.py` consec 로더(:541-560)에 Step 3과 동일 패턴 적용(이쪽은 `if market == "kr"` 가드 내부). row dict(:622-650)에 `"sector": sector_map.get(snap.symbol),` 추가 (KR 외 market이면 sector_map이 빈 dict이라 None — 무해).

**US**: consec 로더의 KR name lookup 블록 뒤에 US 블록 추가:

```python
    if market == "us" and candidate_snaps:
        from app.models.symbol_sectors import SymbolSector
        from app.models.us_symbol_universe import USSymbolUniverse

        try:
            us_rows = (
                await session.execute(
                    sa.select(
                        USSymbolUniverse.symbol,
                        SymbolSector.name_kr.label("sector_name_kr"),
                        SymbolSector.name_en.label("sector_name_en"),
                    )
                    .outerjoin(
                        SymbolSector, USSymbolUniverse.sector_id == SymbolSector.id
                    )
                    .where(
                        USSymbolUniverse.symbol.in_(
                            [snap.symbol for snap in candidate_snaps]
                        )
                    )
                )
            ).all()
            sector_map = {
                row.symbol: label
                for row in us_rows
                if (
                    label := (
                        getattr(row, "sector_name_kr", None)
                        or getattr(row, "sector_name_en", None)
                    )
                )
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "consecutive_gainers: us sector lookup failed: %s", exc, exc_info=True
            )
```

⚠️ 이 US 블록은 execute 1회를 추가한다 — consec 로더의 `_FakeSession` 기반 기존 테스트 중 market="us" 케이스가 있으면 시퀀스 소진으로 빈 결과(fail-open)라 green 유지되지만, **반드시 try/except로 감싼다**(위 코드 그대로).

consec 테스트 추가 (같은 파일, flow 테스트와 동일 스타일 — consec 로더 기존 테스트의 `_FakeSession` 시퀀스를 복사해 name row만 `_name_sector_row`로 교체하고 `rows[0]["sector"]` 단언; 기존 consec 테스트 중 가장 단순한 것 하나를 본뜬다):

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_consecutive_gainers_rows_carry_master_sector() -> None:
    from app.services.invest_view_model.screener_service import (
        _load_consecutive_gainers_from_snapshots,
    )

    snapshot_date = date(2026, 5, 11)
    session = _FakeSession(
        [
            _FakeExecuteResult(scalar_rows=[snapshot_date]),  # resolve (fixture pop)
            _FakeExecuteResult(
                scalar_rows=[_FakeSnapshot(symbol="005930", snapshot_date=snapshot_date)]
            ),
            _FakeExecuteResult(
                rows=[_name_sector_row("005930", "삼성전자", "반도체와반도체장비")]
            ),
        ]
    )
    load_result = await _load_consecutive_gainers_from_snapshots(
        session, market="kr", limit=20
    )
    assert load_result is not None
    assert load_result.rows[0]["sector"] == "반도체와반도체장비"
```

Run: `uv run pytest tests/test_invest_view_model_screener_service.py -v`
Expected: 전부 PASS.

- [ ] **Step 6: double_buy 로더 + 테스트**

`tests/test_invest_view_model_double_buy_screener.py`의 `test_returns_rows_filtered_by_double_buy_and_positive_change_rate`에 단언 추가 (실DB 테스트 — 시드에 sector 추가):

시드 블록(KRSymbolUniverse 911000 추가하는 곳)을 sector 연결로 확장:

```python
    from app.models.symbol_sectors import SymbolSector

    sector = SymbolSector(
        market="kr", source="naver_upjong", source_key="999991",
        name_kr="반도체와반도체장비",
    )
    db_session.add(sector)
    await db_session.flush()
    # 기존 KRSymbolUniverse(symbol="911000", ...) 생성 인자에 sector_id=sector.id 추가
```

단언 추가: `assert target["sector"] == "반도체와반도체장비"`.
픽스처 `_purge`에 `await db_session.execute(sa.delete(SymbolSector).where(SymbolSector.source_key == "999991"))` 추가 (KRSymbolUniverse delete **앞**이 아닌 **뒤**에 — FK 참조 행 먼저 삭제).

구현: `double_buy_screener.py:169-181` name lookup을 공통 패턴으로 확장(파일 상단에 이미 모델 import들이 있으니 `from app.models.symbol_sectors import SymbolSector` 추가), `sector_map` 구성, row dict에 `"sector": sector_map.get(sym),` 추가.

Run: `uv run pytest tests/test_invest_view_model_double_buy_screener.py -v`
Expected: 전부 PASS.

- [ ] **Step 7: fundamentals 로더 + 테스트**

구현 (`kr_fundamentals_tv_screener.py`):
1. name_map 쿼리(:529-537)를 공통 패턴으로 확장(`from app.models.symbol_sectors import SymbolSector` 파일 상단 import) → `sector_map` 추가.
2. `_build_row` 시그니처에 `master_sector: str | None = None` 키워드 추가, row dict의 `"category": snap.industry or snap.sector` **위에** `"sector": master_sector,` 추가 (포맷터가 sector 우선 → 한글 우선·영문 category fallback이 자동).
3. 호출부(:641)에 `master_sector=sector_map.get(sym),` 전달.

테스트 — `_build_row`를 직접 단언 (fundamentals 기존 테스트 파일에서 `_build_row`를 import하는 파일을 찾아 추가; 없으면 `tests/test_fundamentals_screener.py`에):

```python
@pytest.mark.unit
def test_build_row_prefers_master_sector_over_english_category():
    """ROB-512 갭3: master 한글 sector가 있으면 row['sector']로 실려 포맷터의
    sector-우선 규칙에 따라 한글이 표시되고, 없으면 기존 영문 category fallback."""
    from app.services.invest_view_model.kr_fundamentals_tv_screener import _build_row

    snap = _make_snap()  # 파일 내 기존 스냅샷 팩토리 헬퍼 사용 (industry 영문 세팅)
    row = _build_row(
        snap, name="테스트", state="fresh",
        partition_date=dt.date(2026, 6, 10),
        master_sector="반도체와반도체장비",
    )
    assert row["sector"] == "반도체와반도체장비"
    assert row["category"]  # 영문 fallback 보존

    row_no_master = _build_row(
        snap, name="테스트", state="fresh", partition_date=dt.date(2026, 6, 10)
    )
    assert row_no_master["sector"] is None
```

(`_make_snap` 같은 팩토리가 그 파일에 없으면 기존 `_build_row` 호출 테스트의 스냅샷 구성 방식을 그대로 복사한다.)

Run: `uv run pytest tests/test_fundamentals_screener.py tests/test_invest_view_model_kr_fundamentals* -v 2>/dev/null || uv run pytest tests/ -v -k "build_row"`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/services/invest_view_model/ tests/
git commit -m "feat(ROB-512): 로더 4곳 universe lookup에 sector join — category 한글 표시 (gap3)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 8: 풀 게이트 + CLAUDE.md + PR

**Files:**
- Modify: `CLAUDE.md` (데이터 구조 섹션)

- [ ] **Step 1: CLAUDE.md에 섹션 추가**

`CLAUDE.md`의 "KR/US 심볼 유니버스" 항목 뒤에:

```markdown
### Symbol Sectors (ROB-512 갭3)

`symbol_sectors` — KR/US 섹터 마스터 (KR=Naver upjong 번호 키 한글명 / US=yfinance industry 영문 키 + 정적 한글 매핑). universe 테이블의 `sector_id` FK로 연결, 표시 규칙 `name_kr ?? name_en ?? "-"`.

- **모델**: `app/models/symbol_sectors.SymbolSector`
- **서비스**: `app/services/symbol_sectors_service` — 모든 쓰기는 `get_or_create_sector`/`assign_symbol_sector` 경유
- **Lazy fill**: `screener_analysis_enrichment.enrich_snapshot_page` — NULL 심볼만 페이지 단위 fetch(sem 4, fail-open), 일괄 백필 크롤 없음
- **주의**: universe sync는 sector_id를 보존(필드 단위 갱신) — 통째 upsert로 바꾸지 말 것
```

- [ ] **Step 2: 포맷/린트/타입**

Run:
```bash
uv run ruff format app/ tests/
uv run ruff check app/ tests/
make lint
```
Expected: clean (교훈: CI lint는 app/+tests/ 둘 다, ty는 `app/` 전체).

- [ ] **Step 3: 관련 스위트 일괄**

Run:
```bash
uv run pytest tests/test_symbol_sectors_model.py tests/test_symbol_sectors_service.py \
  tests/test_symbol_universe_sector_preservation.py tests/test_naver_finance.py \
  tests/test_us_sector_korean_map.py tests/services/test_screener_analysis_enrichment.py \
  tests/test_invest_view_model_screener_service.py tests/test_invest_view_model_double_buy_screener.py \
  tests/test_fundamentals_screener.py tests/test_partition_health_loader_wiring.py \
  tests/test_screener_snapshot_tool.py tests/test_kr_symbol_universe_sync.py \
  tests/test_us_symbol_universe* -v 2>&1 | tail -20
```
(마지막 두 패턴은 존재하는 파일명으로 조정 — `ls tests/ | grep -i "symbol_universe"`로 확인.)
Expected: 전부 PASS. 공유 DB run-ordering 실패가 보이면 해당 파일 단독 재실행으로 회귀 여부 분리.

- [ ] **Step 4: push + PR 생성**

```bash
git push -u origin rob-512-gap3-kr-sector
gh pr create --base main \
  --title "feat(ROB-512): KR/US 스크리너 카테고리 — symbol_sectors 정규화 + lazy fill (갭3)" \
  --body "$(cat <<'EOF'
## Summary
- 신규 `symbol_sectors` 테이블(UNIQUE(market,source,source_key), name_kr/name_en) + `kr/us_symbol_universe.sector_id` FK — alembic `rob512_symbol_sectors`
- KR: Naver 종목 메인 upjong 링크(업종번호=안정 키, 한글명) — 죽어있던 `_parse_industry_info` 셀렉터 수리 포함
- US: yfinance industry/sector + 정적 한글 매핑(미스는 영문 원문, fake 금지)
- Lazy fill: `enrich_snapshot_page`에서 NULL 심볼만 페이지 단위 fetch→persist→category 즉시 교체 (sem 4, fail-open, 심볼당 평생 1회) — 일괄 백필 크롤 없음
- 로더 4곳(consec/flow/double_buy/fundamentals) universe lookup을 sector LEFT JOIN으로 확장 (read-time DB-only)
- universe sync의 sector_id 보존을 회귀 테스트로 고정 (KR/US)

## Out of scope
갭4 flow lag(operator 트랙), 일괄 워밍 스크립트, stale 자동 갱신, 섹터 피어 MCP 도구 (모두 follow-up)

## Operator follow-up
`alembic upgrade head`만. 이후 MCP 사용으로 자연 워밍업 — 웹 프론트는 워밍된 심볼만 표시(스펙 §8).

## Spec / Plan
docs/plans/ROB-512-gap3-kr-sector-master-lazy-fill-spec.md / docs/plans/ROB-512-gap3-sector-implementation-plan.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: CI green 확인**

Run: `gh pr checks --watch`
Expected: 필수 체크 전부 green. ⚠️ alembic 2-head: main이 머지 사이에 전진해 새 migration이 들어왔으면 `uv run alembic heads`가 2개 — merge-heads revision으로 해소 후 재push.

---

## Self-Review

- **Spec coverage:** §1 모델/migration→Task 1, sync 보존→Task 3, §2 파서→Task 4, §3 서비스→Task 2, §4 lazy fill(KR/US)→Task 6, US 매핑→Task 5, §5 로더 4곳→Task 7, §6 안전경계→각 Task의 fail-open/서비스 경유 구현, §7 테스트 표→Task 1-7 각 테스트, §8 운영→Task 8 PR body. 누락 없음.
- **Placeholder scan:** migration `down_revision`은 환경 의존값이라 확인 명령(`uv run alembic heads`)+치환 지시로 처리. Task 7 fundamentals `_make_snap`은 "기존 테스트의 구성 방식 복사" 지시로 한정(파일별 팩토리명이 달라 하드코딩이 더 위험). 그 외 TBD/TODO 없음.
- **Type consistency:** `get_or_create_sector(db, *, market, source, source_key, name_kr, name_en) -> int` / `assign_symbol_sector(db, *, market, symbol, sector_id) -> bool` 시그니처가 Task 2 정의·Task 6 호출·테스트에서 일치. fetch 헬퍼 반환 `(source_key계열, name계열)` tuple 일치(KR=(no,name_kr), US=(industry,sector)). `sector_name_kr/_en` 라벨명이 로더 4곳·`_name_sector_row` fake에서 일치. `sectorResolved` summary 키 일치.
