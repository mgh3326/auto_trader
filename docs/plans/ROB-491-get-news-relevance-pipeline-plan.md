# ROB-491: get_news 관련성 파이프라인 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** get_news(KR)를 "수집→DB 저장→상태 표시" 구조로 개조하고, 외부 비동기 Job이 관련성 판정을 write-back하는 ingest 표면을 추가한다.

**Architecture:** 신규 `symbol_news_relevance` 테이블이 (기사, 종목) 관계의 수명(pending/confirmed/excluded)을 소유. get_news는 네이버 피드를 set-difference upsert하고 DB 기준으로 응답(excluded만 제외, pending은 상태 표시). 판정은 token-authed HTTP ingest로만 write-back — auto_trader 코드는 어떤 기사도 자동 제외하지 않는다. 기존 하드코딩 블랙리스트는 폐기, alias/키워드 신호는 비권위적 `hints`로 격하.

**Tech Stack:** SQLAlchemy async + alembic, FastAPI(AuthMiddleware token branch), pytest(`db_session` 픽스처), pg_insert on_conflict.

**Spec:** `docs/plans/ROB-491-get-news-relevance-pipeline-spec.md` (먼저 읽을 것)

**작업 위치:** PR1 = 이 worktree(`/Users/mgh3326/work/auto_trader.rob-491`, branch `rob-491`). 미커밋 1차 슬라이스(블랙리스트 필터)가 working tree에 있으며 Task 3~6이 이를 재작업한다 — stash/revert 하지 말 것. PR2 = PR1 머지 후 `origin/main` 기준 새 branch `rob-491-pr2`.

**참고할 기존 패턴 (구현 전 일독):**
- DB 세션: `app/core/db.py` — `AsyncSessionLocal` (MCP tooling 사용례: `app/mcp_server/tooling/kis_live_ledger.py:71`)
- upsert: `app/services/llm_news_service.py:312` — `pg_insert(...).on_conflict_do_nothing(index_elements=[NewsArticle.url])`
- token-auth: `app/middleware/auth.py:206` HERMES 분기 + `app/routers/investment_hermes_http.py` + `tests/routers/test_investment_hermes_http_auth.py`
- DB 테스트: `tests/conftest.py:454` `db_session` 픽스처 (create_all 기반, xdist advisory lock)

---

## PR1 — 수집·저장·상태 표시

### Task 1: ORM 모델 `SymbolNewsRelevance`

**Files:**
- Create: `app/models/symbol_news_relevance.py`
- Modify: `app/models/__init__.py` (alphabetical import 추가)
- Test: `tests/models/test_symbol_news_relevance_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/models/test_symbol_news_relevance_model.py
"""SymbolNewsRelevance table contract (ROB-491)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.news import NewsArticle
from app.models.symbol_news_relevance import SymbolNewsRelevance


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


async def _make_article(db, url: str) -> NewsArticle:
    now = _utcnow()
    article = NewsArticle(
        url=url,
        title="t",
        market="kr",
        feed_source="naver_item_news",
        scraped_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(article)
    await db.flush()
    return article


@pytest.mark.integration
@pytest.mark.asyncio
async def test_link_roundtrip_defaults_pending(db_session) -> None:
    article = await _make_article(db_session, "https://x/rob491-roundtrip")
    now = _utcnow()
    link = SymbolNewsRelevance(
        article_id=article.id,
        market="kr",
        symbol="035420",
        feed_source="naver_item_news",
        first_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add(link)
    await db_session.flush()
    await db_session.refresh(link)
    assert link.status == "pending"
    assert link.relationship is None
    assert link.judged_at is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_link_violates_unique(db_session) -> None:
    article = await _make_article(db_session, "https://x/rob491-dup")
    now = _utcnow()
    for _ in range(2):
        db_session.add(
            SymbolNewsRelevance(
                article_id=article.id,
                market="kr",
                symbol="035420",
                feed_source="naver_item_news",
                first_seen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.flush()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/models/test_symbol_news_relevance_model.py -v`
Expected: FAIL — `ModuleNotFoundError: app.models.symbol_news_relevance`

- [ ] **Step 3: Write the model**

```python
# app/models/symbol_news_relevance.py
"""Symbol↔news relevance lifecycle (ROB-491).

One row owns the full lifecycle of "this article appeared in this symbol's
feed": provenance (feed_source/first_seen_at), pending state, and the external
judgment written back via the token-authed ingest route. auto_trader code never
sets ``excluded`` on its own — only the ingest path transitions status.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SymbolNewsRelevance(Base):
    __tablename__ = "symbol_news_relevance"

    __table_args__ = (
        UniqueConstraint(
            "article_id",
            "market",
            "symbol",
            name="uq_symbol_news_relevance_article_market_symbol",
        ),
        CheckConstraint(
            "market IN ('kr', 'us', 'crypto')",
            name="ck_symbol_news_relevance_market",
        ),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'excluded')",
            name="ck_symbol_news_relevance_status",
        ),
        CheckConstraint(
            "relationship IS NULL OR relationship IN "
            "('direct', 'material_indirect', 'incidental', 'unrelated')",
            name="ck_symbol_news_relevance_relationship",
        ),
        CheckConstraint(
            "relevance IS NULL OR relevance IN ('high', 'medium', 'low')",
            name="ck_symbol_news_relevance_relevance",
        ),
        CheckConstraint(
            "price_relevance IS NULL OR price_relevance IN "
            "('catalyst', 'explainer', 'background', 'none')",
            name="ck_symbol_news_relevance_price_relevance",
        ),
        Index(
            "ix_symbol_news_relevance_market_symbol_status",
            "market",
            "symbol",
            "status",
        ),
        Index(
            "ix_symbol_news_relevance_status_first_seen",
            "status",
            "first_seen_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    article_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("news_articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    market: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    feed_source: Mapped[str] = mapped_column(String(40), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    relationship: Mapped[str | None] = mapped_column(String(20), nullable=True)
    relevance: Mapped[str | None] = mapped_column(String(10), nullable=True)
    price_relevance: Mapped[str | None] = mapped_column(String(20), nullable=True)
    score: Mapped[float | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    judged_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    judged_at: Mapped[datetime | None] = mapped_column(nullable=True)
    hints: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    def __repr__(self) -> str:
        return (
            "<SymbolNewsRelevance("
            f"article_id={self.article_id}, market='{self.market}', "
            f"symbol='{self.symbol}', status='{self.status}')>"
        )
```

`app/models/__init__.py`에 알파벳 순서 위치에 추가:

```python
from .symbol_news_relevance import SymbolNewsRelevance
```

(파일 끝 `__all__` 리스트가 있다면 거기에도 `"SymbolNewsRelevance"` 추가.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/models/test_symbol_news_relevance_model.py -v`
Expected: 2 PASS (db_session 픽스처의 create_all이 신규 테이블 생성)

- [ ] **Step 5: Commit**

```bash
git add app/models/symbol_news_relevance.py app/models/__init__.py tests/models/test_symbol_news_relevance_model.py
git commit -m "feat(ROB-491): SymbolNewsRelevance 모델 — 기사·종목 관련성 수명 테이블"
```

### Task 2: Alembic 마이그레이션

**Files:**
- Create: `alembic/versions/20260610_rob491_symbol_news_relevance.py`

- [ ] **Step 1: 현재 head 확인**

Run: `uv run alembic heads`
Expected: `c07e44daf745 (head)` — 단일 head. **다르면 출력된 head를 아래 `down_revision`에 사용** (main이 전진했을 수 있음).

- [ ] **Step 2: 마이그레이션 작성**

```python
# alembic/versions/20260610_rob491_symbol_news_relevance.py
"""add symbol_news_relevance (ROB-491)

Revision ID: 20260610_rob491
Revises: c07e44daf745
Create Date: 2026-06-10 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260610_rob491"
down_revision: str | None = "c07e44daf745"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "symbol_news_relevance",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("market", sa.String(length=20), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("feed_source", sa.String(length=40), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("relationship", sa.String(length=20), nullable=True),
        sa.Column("relevance", sa.String(length=10), nullable=True),
        sa.Column("price_relevance", sa.String(length=20), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("judged_by", sa.String(length=100), nullable=True),
        sa.Column("judged_at", sa.DateTime(), nullable=True),
        sa.Column("hints", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["article_id"], ["news_articles.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "article_id",
            "market",
            "symbol",
            name="uq_symbol_news_relevance_article_market_symbol",
        ),
        sa.CheckConstraint(
            "market IN ('kr', 'us', 'crypto')",
            name="ck_symbol_news_relevance_market",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'excluded')",
            name="ck_symbol_news_relevance_status",
        ),
        sa.CheckConstraint(
            "relationship IS NULL OR relationship IN "
            "('direct', 'material_indirect', 'incidental', 'unrelated')",
            name="ck_symbol_news_relevance_relationship",
        ),
        sa.CheckConstraint(
            "relevance IS NULL OR relevance IN ('high', 'medium', 'low')",
            name="ck_symbol_news_relevance_relevance",
        ),
        sa.CheckConstraint(
            "price_relevance IS NULL OR price_relevance IN "
            "('catalyst', 'explainer', 'background', 'none')",
            name="ck_symbol_news_relevance_price_relevance",
        ),
    )
    op.create_index(
        "ix_symbol_news_relevance_article_id",
        "symbol_news_relevance",
        ["article_id"],
    )
    op.create_index(
        "ix_symbol_news_relevance_market_symbol_status",
        "symbol_news_relevance",
        ["market", "symbol", "status"],
    )
    op.create_index(
        "ix_symbol_news_relevance_status_first_seen",
        "symbol_news_relevance",
        ["status", "first_seen_at"],
    )


def downgrade() -> None:
    op.drop_table("symbol_news_relevance")
```

- [ ] **Step 3: 검증 — 단일 head + SQL 컴파일**

Run: `uv run alembic heads && uv run alembic upgrade head --sql | grep -c "CREATE TABLE symbol_news_relevance"`
Expected: head가 `20260610_rob491` 하나, grep 결과 `1`

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/20260610_rob491_symbol_news_relevance.py
git commit -m "feat(ROB-491): symbol_news_relevance alembic migration"
```

### Task 3: `symbol_news_relevance.py` → hints 빌더로 재작업 (블랙리스트 폐기)

**Files:**
- Rewrite: `app/services/symbol_news_relevance.py` (전체 교체)
- Test: `tests/services/test_symbol_news_relevance_hints.py` (신규)

기존 `classify_symbol_news_relevance`/`SymbolNewsRelevanceDecision`/`_KR_TITLE_NOISE_TERMS`는 삭제된다. 호출처는 `app/services/symbol_news_service.py` 하나뿐(Task 4~5에서 함께 교체)이므로 이 Task 직후에는 service 쪽이 일시적으로 깨진다 — Task 3과 Task 4~5를 같은 세션에서 연달아 수행하고, Task 3 커밋은 hints 모듈+테스트만 포함한다 (service는 Task 5 커밋에서 정리).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_symbol_news_relevance_hints.py
"""Deterministic relevance hints builder (ROB-491 — non-authoritative)."""

from __future__ import annotations

import pytest

from app.services.symbol_news_relevance import build_relevance_hints


@pytest.mark.unit
def test_alias_match_recorded_as_hint() -> None:
    hints = build_relevance_hints(
        symbol="035420",
        market="kr",
        title="네이버 D2SF, AI 보안 스타트업에 신규 투자",
    )
    assert hints is not None
    assert "네이버" in hints["alias_match"]


@pytest.mark.unit
def test_symbol_code_in_text_counts_as_alias_match() -> None:
    hints = build_relevance_hints(
        symbol="035420", market="kr", title="035420 거래량 급증"
    )
    assert hints is not None
    assert "035420" in hints["alias_match"]


@pytest.mark.unit
def test_no_signals_returns_none() -> None:
    assert (
        build_relevance_hints(
            symbol="035420",
            market="kr",
            title="판다 아이바오, 셋째 출산",
        )
        is None
    )


@pytest.mark.unit
def test_invest_keywords_recorded() -> None:
    hints = build_relevance_hints(
        symbol="035420",
        market="kr",
        title="젠슨 황이 만나고 간 대기업들, 'AI 보안' 스타트업에 투자",
    )
    assert hints is not None
    assert hints.get("invest_keywords")


@pytest.mark.unit
def test_blacklist_api_is_gone() -> None:
    """하드코딩 노이즈 분기 폐기 — 사례 후행적 분기 재도입 방지 가드."""
    import app.services.symbol_news_relevance as mod

    assert not hasattr(mod, "_KR_TITLE_NOISE_TERMS")
    assert not hasattr(mod, "classify_symbol_news_relevance")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_symbol_news_relevance_hints.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_relevance_hints'`

- [ ] **Step 3: 모듈 전체 교체**

```python
# app/services/symbol_news_relevance.py
"""Deterministic relevance *hints* for symbol news (ROB-491).

Non-authoritative signals only. auto_trader never excludes an article based on
these — the external judgment job reads them as context; only the token-authed
ingest route transitions an article's status.
"""

from __future__ import annotations

from typing import Any

from app.services.news_entity_alias_data import (
    KR_ALIASES,
    KR_BROAD_MARKET_TERMS,
    KR_INVEST_KEYWORDS,
)

_KR_SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    entry.symbol: entry.aliases for entry in KR_ALIASES
}


def _matched_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    lowered = text.lower()
    return [term for term in terms if term.lower() in lowered]


def _target_terms(symbol: str, market: str) -> tuple[str, ...]:
    if market == "kr":
        return (*_KR_SYMBOL_ALIASES.get(symbol, ()), symbol)
    return (symbol,)


def build_relevance_hints(
    *,
    symbol: str,
    market: str,
    title: str,
    summary: str | None = None,
) -> dict[str, Any] | None:
    """Deterministic signals for one article, or None when nothing matched."""
    text = " ".join(part for part in (title, summary) if part)
    hints: dict[str, Any] = {}
    if alias_match := _matched_terms(text, _target_terms(symbol, market)):
        hints["alias_match"] = alias_match
    if market == "kr":
        if invest := _matched_terms(text, KR_INVEST_KEYWORDS):
            hints["invest_keywords"] = invest
        if market_terms := _matched_terms(text, KR_BROAD_MARKET_TERMS):
            hints["market_terms"] = market_terms
    return hints or None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_symbol_news_relevance_hints.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/symbol_news_relevance.py tests/services/test_symbol_news_relevance_hints.py
git commit -m "refactor(ROB-491): 관련성 분류기 → 비권위적 hints 빌더 (블랙리스트 폐기)"
```

### Task 4: 저장 서비스 `symbol_news_store.py`

**Files:**
- Create: `app/services/symbol_news_store.py`
- Test: `tests/services/test_symbol_news_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_symbol_news_store.py
"""symbol_news_store persistence seam (ROB-491 PR1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models.news import NewsArticle
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services import symbol_news_store
from app.services.symbol_news_store import FeedArticleInput


def _item(url: str, title: str, published: datetime | None = None) -> FeedArticleInput:
    return FeedArticleInput(
        url=url,
        title=title,
        source="매일경제",
        published_at=published or datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_is_idempotent_set_difference(db_session) -> None:
    items = [
        _item("https://x/rob491-a1", "네이버 D2SF 투자"),
        _item("https://x/rob491-a2", "판다 아이바오 출산"),
    ]
    await symbol_news_store.upsert_kr_feed_articles(db_session, "035420", items)
    # 같은 윈도우 재호출(중복) + 신규 1건 — 순서 무관, 멱등
    await symbol_news_store.upsert_kr_feed_articles(
        db_session,
        "035420",
        [_item("https://x/rob491-a3", "젠슨황 AI 보안 투자"), *items],
    )

    urls = (
        (
            await db_session.execute(
                select(NewsArticle.url).where(
                    NewsArticle.url.like("https://x/rob491-a%")
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(urls) == [
        "https://x/rob491-a1",
        "https://x/rob491-a2",
        "https://x/rob491-a3",
    ]
    links = (
        (
            await db_session.execute(
                select(SymbolNewsRelevance).where(
                    SymbolNewsRelevance.symbol == "035420",
                    SymbolNewsRelevance.market == "kr",
                )
            )
        )
        .scalars()
        .all()
    )
    by_status = {link.status for link in links}
    assert by_status == {"pending"}
    assert len(links) >= 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_attaches_hints_for_alias_match(db_session) -> None:
    await symbol_news_store.upsert_kr_feed_articles(
        db_session, "035420", [_item("https://x/rob491-h1", "네이버 신사업 공개")]
    )
    link = (
        await db_session.execute(
            select(SymbolNewsRelevance)
            .join(NewsArticle, NewsArticle.id == SymbolNewsRelevance.article_id)
            .where(NewsArticle.url == "https://x/rob491-h1")
        )
    ).scalar_one()
    assert link.hints is not None
    assert "네이버" in link.hints["alias_match"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_load_excludes_only_excluded_and_counts(db_session) -> None:
    items = [
        _item("https://x/rob491-l1", "네이버 호실적", datetime(2026, 6, 10, 9, tzinfo=UTC)),
        _item("https://x/rob491-l2", "판다 출산", datetime(2026, 6, 10, 8, tzinfo=UTC)),
        _item("https://x/rob491-l3", "시황 브리핑", datetime(2026, 6, 10, 7, tzinfo=UTC)),
    ]
    await symbol_news_store.upsert_kr_feed_articles(db_session, "999991", items)
    # l2를 excluded로 직접 마킹 (PR2 전이라 store 내부 상태를 수동 구성)
    link = (
        await db_session.execute(
            select(SymbolNewsRelevance)
            .join(NewsArticle, NewsArticle.id == SymbolNewsRelevance.article_id)
            .where(NewsArticle.url == "https://x/rob491-l2")
        )
    ).scalar_one()
    link.status = "excluded"
    await db_session.flush()

    stored, excluded_count = await symbol_news_store.load_symbol_news(
        db_session, "999991", "kr", limit=10
    )
    titles = [row.title for row in stored]
    assert titles == ["네이버 호실적", "시황 브리핑"]  # published_at desc, excluded 제외
    assert excluded_count == 1
    assert stored[0].relevance["status"] == "pending"
    assert stored[0].relevance["hints"] is not None


@pytest.mark.unit
def test_derive_status_rules() -> None:
    assert symbol_news_store.derive_status("unrelated", "high") == "excluded"
    assert symbol_news_store.derive_status("direct", "low") == "excluded"
    assert symbol_news_store.derive_status("direct", "high") == "confirmed"
    assert symbol_news_store.derive_status("incidental", "medium") == "confirmed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_symbol_news_store.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.symbol_news_store`

- [ ] **Step 3: Write the store**

```python
# app/services/symbol_news_store.py
"""Persistence seam for the symbol-news relevance lifecycle (ROB-491).

All DB writes for the get_news cache go through here: ① article/link upsert at
fetch time (set-difference by unique url — feed order is never trusted), and
② judgment apply via the token-authed ingest route (PR2). No MCP imports, no
LLM, no broker/order surface. Callers own session lifecycle and commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import NewsArticle
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services.symbol_news_relevance import build_relevance_hints

logger = logging.getLogger(__name__)

KR_FEED_SOURCE = "naver_item_news"


@dataclass(frozen=True)
class FeedArticleInput:
    url: str
    title: str
    source: str | None
    published_at: datetime | None


@dataclass(frozen=True)
class StoredSymbolNews:
    article_id: int
    url: str
    title: str
    source: str | None
    published_at: datetime | None
    relevance: dict[str, Any]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def derive_status(relationship: str, relevance: str) -> str:
    """Server-owned status rule — the judgment job never writes status itself."""
    if relationship == "unrelated" or relevance == "low":
        return "excluded"
    return "confirmed"


def _relevance_block(link: SymbolNewsRelevance) -> dict[str, Any]:
    return {
        "status": link.status,
        "relationship": link.relationship,
        "relevance": link.relevance,
        "price_relevance": link.price_relevance,
        "score": link.score,
        "reason": link.reason,
        "judged_by": link.judged_by,
        "judged_at": link.judged_at.isoformat() if link.judged_at else None,
        "hints": link.hints,
    }


async def upsert_kr_feed_articles(
    db: AsyncSession,
    symbol: str,
    items: list[FeedArticleInput],
    *,
    feed_source: str = KR_FEED_SOURCE,
) -> None:
    """Set-difference upsert: new urls insert, known urls no-op (idempotent)."""
    if not items:
        return
    now = _utcnow()
    article_values = [
        {
            "url": item.url,
            "title": item.title[:500],
            "source": item.source,
            "market": "kr",
            "feed_source": feed_source,
            "article_published_at": item.published_at,
            "is_analyzed": False,
            "scraped_at": now,
            "created_at": now,
            "updated_at": now,
        }
        for item in items
    ]
    await db.execute(
        pg_insert(NewsArticle)
        .values(article_values)
        .on_conflict_do_nothing(index_elements=[NewsArticle.url])
    )
    urls = [item.url for item in items]
    id_rows = await db.execute(
        select(NewsArticle.id, NewsArticle.url).where(NewsArticle.url.in_(urls))
    )
    url_to_id = {url: article_id for article_id, url in id_rows.all()}

    link_values = []
    for item in items:
        article_id = url_to_id.get(item.url)
        if article_id is None:  # insert race lost and url missing — skip, next call heals
            continue
        link_values.append(
            {
                "article_id": article_id,
                "market": "kr",
                "symbol": symbol,
                "feed_source": feed_source,
                "first_seen_at": now,
                "status": "pending",
                "hints": build_relevance_hints(
                    symbol=symbol, market="kr", title=item.title
                ),
                "created_at": now,
                "updated_at": now,
            }
        )
    if link_values:
        await db.execute(
            pg_insert(SymbolNewsRelevance)
            .values(link_values)
            .on_conflict_do_nothing(
                index_elements=[
                    SymbolNewsRelevance.article_id,
                    SymbolNewsRelevance.market,
                    SymbolNewsRelevance.symbol,
                ]
            )
        )
    await db.commit()


async def load_symbol_news(
    db: AsyncSession,
    symbol: str,
    market: str,
    limit: int,
) -> tuple[list[StoredSymbolNews], int]:
    """Canonical read: non-excluded rows newest-first + excluded count."""
    rows = await db.execute(
        select(NewsArticle, SymbolNewsRelevance)
        .join(
            SymbolNewsRelevance,
            SymbolNewsRelevance.article_id == NewsArticle.id,
        )
        .where(
            SymbolNewsRelevance.market == market,
            SymbolNewsRelevance.symbol == symbol,
            SymbolNewsRelevance.status != "excluded",
        )
        .order_by(
            NewsArticle.article_published_at.desc().nullslast(),
            NewsArticle.id.desc(),
        )
        .limit(limit)
    )
    stored = [
        StoredSymbolNews(
            article_id=article.id,
            url=article.url,
            title=article.title,
            source=article.source,
            published_at=article.article_published_at,
            relevance=_relevance_block(link),
        )
        for article, link in rows.all()
    ]
    excluded_count = (
        await db.execute(
            select(func.count())
            .select_from(SymbolNewsRelevance)
            .where(
                SymbolNewsRelevance.market == market,
                SymbolNewsRelevance.symbol == symbol,
                SymbolNewsRelevance.status == "excluded",
            )
        )
    ).scalar_one()
    return stored, int(excluded_count)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_symbol_news_store.py -v`
Expected: 4 PASS

주의: `upsert_kr_feed_articles`가 `db.commit()`을 호출하므로 db_session 픽스처가 rollback 격리라면 테스트 간 잔존 데이터가 생길 수 있다 — 테스트 URL에 `rob491-` 프리픽스를 쓴 이유. 실패 시 픽스처의 트랜잭션 정책을 확인하고, 필요하면 commit을 호출자(서비스 레이어)로 옮기고 store는 flush만 수행하도록 조정한 뒤 테스트도 함께 갱신할 것 (둘 다 허용되는 설계 — 단 Task 5의 서비스 코드와 일관되게).

- [ ] **Step 5: Commit**

```bash
git add app/services/symbol_news_store.py tests/services/test_symbol_news_store.py
git commit -m "feat(ROB-491): symbol_news_store — 기사/링크 set-difference upsert + canonical load"
```

### Task 5: `symbol_news_service` KR 경로 재배선

**Files:**
- Modify: `app/services/symbol_news_service.py`
- Rewrite: `tests/services/test_symbol_news_service.py` (KR 테스트 교체, US/공통 유지)

- [ ] **Step 1: KR 테스트 교체 (failing)**

`tests/services/test_symbol_news_service.py`에서 `test_kr_filters_duplicate_and_low_relevance_naver_symbol_news`를 삭제하고 아래로 교체. 나머지 테스트(US finnhub/empty/error/unsupported + `test_kr_returns_normalized_articles_with_external_id`)는 유지하되, KR 테스트들은 store를 monkeypatch한다 (DB 불요 — store 자체는 Task 4에서 검증됨).

```python
# tests/services/test_symbol_news_service.py 에 추가/교체할 KR 테스트들
from unittest.mock import AsyncMock, MagicMock


def _stored(article_id: int, url: str, title: str, status: str = "pending"):
    from app.services.symbol_news_store import StoredSymbolNews

    return StoredSymbolNews(
        article_id=article_id,
        url=url,
        title=title,
        source="매일경제",
        published_at=datetime(2026, 6, 10, 9, 0),
        relevance={
            "status": status,
            "relationship": None,
            "relevance": None,
            "price_relevance": None,
            "score": None,
            "reason": None,
            "judged_by": None,
            "judged_at": None,
            "hints": None,
        },
    )


def _patch_store(monkeypatch, *, stored, excluded_count=0):
    upsert = AsyncMock()
    load = AsyncMock(return_value=(stored, excluded_count))
    monkeypatch.setattr(symbol_news_service.symbol_news_store,
                        "upsert_kr_feed_articles", upsert)
    monkeypatch.setattr(symbol_news_service.symbol_news_store,
                        "load_symbol_news", load)
    # AsyncSessionLocal() 컨텍스트를 가짜 세션으로 대체
    fake_session = MagicMock()
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_session)
    fake_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        symbol_news_service, "AsyncSessionLocal", MagicMock(return_value=fake_cm)
    )
    return upsert, load


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_persists_then_serves_db_state(monkeypatch) -> None:
    raw = [
        {
            "title": "네이버 D2SF 투자",
            "url": "https://finance.naver.com/item/news_read.naver?article_id=1&office_id=009",
            "source": "매일경제",
            "datetime": "2026-06-10",
        }
    ]
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=raw)
    )
    upsert, _ = _patch_store(
        monkeypatch,
        stored=[_stored(1, raw[0]["url"], raw[0]["title"])],
        excluded_count=3,
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr", limit=10)

    assert result.status == "ok"
    assert result.excluded_count == 3
    assert result.degraded is False
    upsert.assert_awaited_once()
    art = result.articles[0]
    assert art.provider_metadata["relevance"]["status"] == "pending"
    # 현재 fetch 윈도우에 있던 기사는 원본 source_item 보존
    assert art.provider_metadata["source_item"] == raw[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_db_row_outside_window_gets_reconstructed_source_item(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=[])
    )
    _patch_store(
        monkeypatch, stored=[_stored(7, "https://x/old-article", "지난주 네이버 기사")]
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr", limit=10)

    item = result.articles[0].provider_metadata["source_item"]
    assert item["title"] == "지난주 네이버 기사"
    assert item["url"] == "https://x/old-article"
    assert "datetime" in item and "source" in item


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_fetch_failure_serves_db_cache_degraded(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service.naver_finance,
        "fetch_news",
        AsyncMock(side_effect=RuntimeError("naver down")),
    )
    _patch_store(monkeypatch, stored=[_stored(1, "https://x/cached", "캐시 기사")])

    result = await symbol_news_service.fetch_symbol_news("035420", "kr", limit=10)

    assert result.status == "ok"
    assert result.degraded is True
    assert result.fetch_error == "RuntimeError"
    assert result.articles[0].title == "캐시 기사"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_db_failure_degrades_to_on_demand_pending(monkeypatch) -> None:
    raw = [
        {
            "title": "네이버 호실적",
            "url": "https://finance.naver.com/item/news_read.naver?article_id=9&office_id=001",
            "source": "한국경제",
            "datetime": "2026-06-10",
        }
    ]
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=raw)
    )
    monkeypatch.setattr(
        symbol_news_service,
        "AsyncSessionLocal",
        MagicMock(side_effect=RuntimeError("db down")),
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr", limit=10)

    assert result.status == "ok"  # 도구는 DB 때문에 죽지 않는다
    assert result.articles[0].provider_metadata["relevance"]["status"] == "pending"
    assert result.excluded_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_both_fetch_and_db_down_is_error(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service.naver_finance,
        "fetch_news",
        AsyncMock(side_effect=RuntimeError("naver down")),
    )
    monkeypatch.setattr(
        symbol_news_service,
        "AsyncSessionLocal",
        MagicMock(side_effect=RuntimeError("db down")),
    )
    result = await symbol_news_service.fetch_symbol_news("035420", "kr")
    assert result.status == "error"
```

기존 `test_kr_returns_normalized_articles_with_external_id`는 store patch(`_patch_store(monkeypatch, stored=[...])`)를 추가해 유지한다 — load 반환값을 raw[0] 기반 `_stored(...)`로 채우고 `provider_metadata["source_item"] == raw[0]` 단언은 유지.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_symbol_news_service.py -v`
Expected: 신규 KR 테스트 FAIL (`AttributeError: ... no attribute 'symbol_news_store'` 또는 `excluded_count`)

- [ ] **Step 3: 서비스 재배선**

`app/services/symbol_news_service.py` 변경:

```python
# import 교체 (classify_symbol_news_relevance 제거)
from app.core.db import AsyncSessionLocal
from app.services import naver_finance, symbol_news_store
from app.services.finnhub_news import fetch_news_finnhub
from app.services.symbol_news_store import FeedArticleInput, StoredSymbolNews
```

```python
# SymbolNewsFetchResult에 필드 추가 (기본값으로 기존 호출처 호환)
@dataclass(frozen=True)
class SymbolNewsFetchResult:
    symbol: str
    market: str
    provider: str
    status: str  # ok | empty | unavailable | error
    requested_limit: int
    returned_count: int
    articles: list[SymbolNewsArticle]
    error_code: str | None = None
    excluded_count: int = 0
    degraded: bool = False
    fetch_error: str | None = None
```

```python
_PENDING_RELEVANCE: dict[str, Any] = {
    "status": "pending",
    "relationship": None,
    "relevance": None,
    "price_relevance": None,
    "score": None,
    "reason": None,
    "judged_by": None,
    "judged_at": None,
    "hints": None,
}


async def _fetch_naver(
    symbol: str, limit: int, fetched_at: datetime
) -> list[SymbolNewsArticle]:
    """Pure normalize: URL dedupe only — no filtering, no relevance verdicts."""
    items = await naver_finance.fetch_news(symbol, limit=limit)
    out: list[SymbolNewsArticle] = []
    seen_urls: set[str] = set()
    for raw in items:
        url = (raw.get("url") or "").strip()
        title = (raw.get("title") or "").strip()
        if not url or not title:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        out.append(
            SymbolNewsArticle(
                provider="naver",
                market="kr",
                symbol=symbol,
                external_article_id=_naver_external_id(url),
                title=title,
                source_name=raw.get("source") or None,
                canonical_url=url,
                summary=None,
                published_at=_parse_dt(raw.get("datetime")),
                fetched_at=fetched_at,
                related_symbols=[],
                provider_metadata={"source_item": raw},
            )
        )
    return out


def _stored_to_article(
    row: StoredSymbolNews,
    symbol: str,
    fetched_at: datetime,
    raw_by_url: dict[str, Any],
) -> SymbolNewsArticle:
    source_item = raw_by_url.get(row.url) or {
        "title": row.title,
        "url": row.url,
        "source": row.source or "",
        "datetime": row.published_at.isoformat() if row.published_at else None,
    }
    return SymbolNewsArticle(
        provider="naver",
        market="kr",
        symbol=symbol,
        external_article_id=_naver_external_id(row.url),
        title=row.title,
        source_name=row.source,
        canonical_url=row.url,
        summary=None,
        published_at=row.published_at,
        fetched_at=fetched_at,
        related_symbols=[],
        provider_metadata={"source_item": source_item, "relevance": row.relevance},
    )


async def _kr_persist_and_load(
    symbol: str,
    fetched: list[SymbolNewsArticle],
    limit: int,
    fetched_at: datetime,
) -> tuple[list[SymbolNewsArticle], int] | None:
    """Persist this window then serve canonical DB state. None → DB unavailable."""
    try:
        async with AsyncSessionLocal() as db:
            if fetched:
                await symbol_news_store.upsert_kr_feed_articles(
                    db,
                    symbol,
                    [
                        FeedArticleInput(
                            url=a.canonical_url,
                            title=a.title,
                            source=a.source_name,
                            published_at=a.published_at,
                        )
                        for a in fetched
                    ],
                )
            stored, excluded_count = await symbol_news_store.load_symbol_news(
                db, symbol, "kr", limit
            )
    except Exception as exc:  # noqa: BLE001 — cache layer must not kill the tool
        logger.warning(
            "symbol_news_service: store unavailable, degrading: symbol=%s err=%s",
            symbol,
            exc,
        )
        return None
    raw_by_url = {
        a.canonical_url: a.provider_metadata.get("source_item") for a in fetched
    }
    articles = [
        _stored_to_article(row, symbol, fetched_at, raw_by_url) for row in stored
    ]
    return articles, excluded_count
```

`fetch_symbol_news`의 KR 분기 교체:

```python
    if market == "kr":
        fetched: list[SymbolNewsArticle] | None
        fetch_error: str | None = None
        try:
            fetched = await asyncio.wait_for(
                _fetch_naver(symbol, limit, fetched_at), timeout=timeout_s
            )
        except Exception as exc:  # noqa: BLE001 — fall back to DB cache
            logger.warning(
                "symbol_news_service: naver fetch failed: symbol=%s err=%s",
                symbol,
                exc,
            )
            fetched = None
            fetch_error = type(exc).__name__

        persisted = await _kr_persist_and_load(
            symbol, fetched or [], limit, fetched_at
        )
        if persisted is not None:
            articles, excluded_count = persisted
            if fetched is None and not articles:
                return SymbolNewsFetchResult(
                    symbol, market, provider, "error", limit, 0, [],
                    fetch_error or "naver_fetch_failed",
                )
            status = "ok" if articles else "empty"
            return SymbolNewsFetchResult(
                symbol,
                market,
                provider,
                status,
                limit,
                len(articles),
                articles,
                None,
                excluded_count=excluded_count,
                degraded=fetched is None,
                fetch_error=fetch_error,
            )
        # DB 불가 — 기존 on-demand 동작으로 degrade (전부 pending 표시)
        if fetched is None:
            return SymbolNewsFetchResult(
                symbol, market, provider, "error", limit, 0, [],
                fetch_error or "naver_fetch_failed",
            )
        articles = [
            replace(
                a,
                provider_metadata={
                    **a.provider_metadata,
                    "relevance": {
                        **_PENDING_RELEVANCE,
                        "hints": symbol_news_store_hints(symbol, a.title),
                    },
                },
            )
            for a in fetched
        ]
        status = "ok" if articles else "empty"
        return SymbolNewsFetchResult(
            symbol, market, provider, status, limit, len(articles), articles, None
        )
```

여기서 `symbol_news_store_hints`는 모듈 상단의 작은 헬퍼:

```python
def symbol_news_store_hints(symbol: str, title: str) -> dict[str, Any] | None:
    from app.services.symbol_news_relevance import build_relevance_hints

    return build_relevance_hints(symbol=symbol, market="kr", title=title)
```

기존 `try/except` 전체 래핑 구조(US/crypto·unsupported 분기 포함)는 유지하되 KR 분기만 위 코드로 분리한다 — KR은 자체 예외 처리를 가지므로 바깥 `except`에 도달하지 않는다.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/test_symbol_news_service.py tests/services/test_symbol_news_relevance_hints.py tests/services/test_symbol_news_store.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/symbol_news_service.py tests/services/test_symbol_news_service.py
git commit -m "feat(ROB-491): get_news KR 경로 — 수집→DB upsert→canonical 상태 서빙 (fail-open 2경로)"
```

### Task 6: MCP envelope — `relevance` 블록 + 메타

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals/_news.py:59-72`
- Modify: `tests/mcp_server/tooling/test_get_news_envelope.py`

- [ ] **Step 1: envelope 테스트 교체 (failing)**

`test_get_news_kr_exposes_symbol_relevance_metadata`를 삭제하고 교체:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_kr_exposes_relevance_block_and_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relevance = {
        "status": "confirmed",
        "relationship": "direct",
        "relevance": "high",
        "price_relevance": "catalyst",
        "score": 0.9,
        "reason": "본문이 NAVER 실적을 직접 다룸",
        "judged_by": "hermes",
        "judged_at": "2026-06-10T10:00:00+00:00",
        "hints": {"alias_match": ["네이버"]},
    }
    art = replace(
        _naver_article(),
        provider_metadata={
            **_naver_article().provider_metadata,
            "relevance": relevance,
        },
    )
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(
            return_value=SymbolNewsFetchResult(
                "005930", "kr", "naver", "ok", 10, 1, [art], excluded_count=4
            )
        ),
    )

    out = await _news.handle_get_news("005930", market="kr", limit=10)

    assert out["news"][0]["relevance"] == relevance
    assert out["excluded_count"] == 4
    assert "degraded" not in out
    assert "relevance" not in art.provider_metadata["source_item"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_kr_degraded_meta_surfaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(
            return_value=SymbolNewsFetchResult(
                "005930", "kr", "naver", "ok", 10, 1, [_naver_article()],
                degraded=True, fetch_error="RuntimeError",
            )
        ),
    )
    out = await _news.handle_get_news("005930", market="kr", limit=10)
    assert out["degraded"] is True
    assert out["fetch_error"] == "RuntimeError"
```

`test_get_news_kr_envelope_unchanged`는 expected dict에 `"excluded_count": 0` 추가. `test_get_news_us_envelope_keys_preserved`는 무변경(US item에 `relevance` 키 없음 단언 추가):

```python
    assert "relevance" not in out["news"][0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/mcp_server/tooling/test_get_news_envelope.py -v`
Expected: 신규/수정 테스트 FAIL (`KeyError: 'excluded_count'` 등)

- [ ] **Step 3: 핸들러 수정**

`handle_get_news`의 news 빌드 + return 교체:

```python
    news = []
    for article in result.articles:
        source_item = article.provider_metadata.get("source_item", {})
        item = dict(source_item) if isinstance(source_item, dict) else {}
        if relevance := article.provider_metadata.get("relevance"):
            item["relevance"] = relevance
        news.append(item)
    payload: dict[str, Any] = {
        "symbol": symbol,
        "market": normalized_market,
        "source": result.provider,
        "count": len(news),
        "excluded_count": result.excluded_count,
        "news": news,
    }
    if result.degraded:
        payload["degraded"] = True
        payload["fetch_error"] = result.fetch_error
    return payload
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mcp_server/tooling/test_get_news_envelope.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/fundamentals/_news.py tests/mcp_server/tooling/test_get_news_envelope.py
git commit -m "feat(ROB-491): get_news envelope — relevance 블록 + excluded_count/degraded 메타"
```

### Task 7: README 갱신 + PR1 게이트

**Files:**
- Modify: `app/mcp_server/README.md` (working tree에 있는 get_news 섹션 재작성)

- [ ] **Step 1: README의 get_news 항목을 새 계약으로 교체**

기존 working-tree 추가분(“filters obvious low-relevance title noise…” 단락)을 삭제하고 교체:

```markdown
- `get_news(symbol, market=None, limit=10)`
  - Fetch symbol-level recent news for decision diagnostics (`kr`: Naver Finance, `us`/`crypto`: Finnhub)
  - KR: fetched articles are persisted (`news_articles` + `symbol_news_relevance`) and the response is served from DB state. Each item carries a `relevance` block (`status`: `pending`/`confirmed`; judged fields + non-authoritative `hints`). `excluded` articles (judged unrelated/low by the external judgment job) are omitted; `excluded_count` reports how many. No deterministic blacklist — auto_trader never excludes on its own.
  - `degraded: true` + `fetch_error` appear when Naver fetch failed and the response was served from DB cache only.
  - `pending` means "not yet judged" — treat as unverified recall, not confirmed evidence.
  - Returns: `symbol`, `market`, `source`, `count`, `excluded_count`, `news`
```

- [ ] **Step 2: 전체 게이트 (lint는 app/ + tests/ 둘 다 — CI 동일)**

Run:
```bash
uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/
uv run ty check app/
uv run pytest tests/services/test_symbol_news_service.py tests/services/test_symbol_news_store.py tests/services/test_symbol_news_relevance_hints.py tests/mcp_server/tooling/test_get_news_envelope.py tests/models/test_symbol_news_relevance_model.py -v
uv run pytest tests/ -m "not integration and not slow" -q
```
Expected: 전부 green. (풀 스위트에서 shared-DB 오염성 실패가 나오면 단독 재실행으로 회귀 여부 판별 — ROB-434 교훈.)

- [ ] **Step 3: Commit + PR1 생성**

```bash
git add app/mcp_server/README.md
git commit -m "docs(ROB-491): get_news README — DB-backed relevance 계약"
git push -u origin rob-491
gh pr create --base main --title "feat(ROB-491): get_news KR — 수집·DB저장·관련성 상태 표시 (PR1)" --body "<spec/plan 링크 + 요약 + 안전경계(migration 1, 브로커 mutation 0, LLM 호출 0)>"
```

PR body에 명시: operator 게이트 = 배포 후 `alembic upgrade head` 수동 실행.

---

## PR2 — 판정 ingest 표면 (PR1 머지 후)

**시작 절차:** canonical repo에서 `git fetch --prune origin` 후 이 worktree에서 `git switch -c rob-491-pr2 origin/main` (PR1 머지 반영 확인: `git log origin/main --oneline -5`에 PR1 squash 커밋 존재).

### Task 8: 설정 + AuthMiddleware 토큰 분기

**Files:**
- Modify: `app/core/config.py` (HERMES_INGEST_TOKEN 근처, ~482행)
- Modify: `app/middleware/auth.py` (HERMES 분기 바로 아래)
- Test: `tests/routers/test_news_relevance_auth.py`

- [ ] **Step 1: Write the failing auth tests**

`tests/routers/test_investment_hermes_http_auth.py`의 구조를 그대로 따른다 (해당 파일을 먼저 읽을 것):

```python
# tests/routers/test_news_relevance_auth.py
"""news-relevance ingest token gate (ROB-491 PR2) — 403/401/200 contract."""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.middleware.auth import AuthMiddleware
from app.routers.news_relevance import router as news_relevance_router

_PATH = "/trading/api/news-relevance/ingest/bulk"
_BODY = {
    "judgments": [
        {
            "article_id": 1,
            "market": "kr",
            "symbol": "035420",
            "relationship": "direct",
            "relevance": "high",
            "price_relevance": "catalyst",
            "reason": "직접 관련",
            "judged_by": "hermes",
        }
    ]
}


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(news_relevance_router)
    app.add_middleware(AuthMiddleware)
    return app


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unconfigured_token_returns_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "NEWS_RELEVANCE_INGEST_TOKEN", "", raising=False)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(_PATH, json=_BODY)
    assert resp.status_code == 403
    assert "not configured" in cast(str, resp.json()["detail"]).lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wrong_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "NEWS_RELEVANCE_INGEST_TOKEN", "secret", raising=False
    )
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(
            _PATH, json=_BODY, headers={"X-News-Relevance-Ingest-Token": "nope"}
        )
    assert resp.status_code == 401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_get_also_token_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "NEWS_RELEVANCE_INGEST_TOKEN", "secret", raising=False
    )
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.get("/trading/api/news-relevance/pending?market=kr")
    assert resp.status_code == 401
```

(이 시점에는 `app.routers.news_relevance`가 없어 import 에러 — Task 10에서 라우터가 생기면 전체 green이 된다. Task 8에서는 미들웨어 분기만 먼저 만들고, 이 테스트 파일은 작성만 해두고 Task 10 Step에서 실행한다.)

- [ ] **Step 2: 설정 추가** (`app/core/config.py`, HERMES 항목 바로 아래)

```python
    NEWS_RELEVANCE_INGEST_TOKEN: str = ""
    NEWS_RELEVANCE_INGEST_TOKEN_HEADER: str = "X-News-Relevance-Ingest-Token"
```

- [ ] **Step 3: 미들웨어 분기 추가** (`app/middleware/auth.py`)

클래스 상수에 추가:

```python
    NEWS_RELEVANCE_PATH_PREFIX = "/trading/api/news-relevance/"
```

HERMES 분기 바로 아래에 같은 shape로 추가 (hmac.compare_digest 사용 — 기존 import 재사용):

```python
        # ROB-491 — external judgment job surface (pending read + judgment
        # ingest). Same prefix-token shape as the Hermes branch; both GET and
        # POST require the token because the payloads expose article batches.
        if path.startswith(self.NEWS_RELEVANCE_PATH_PREFIX):
            expected_token = settings.NEWS_RELEVANCE_INGEST_TOKEN
            if not expected_token:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "News relevance ingest token not configured"},
                )
            header_name = settings.NEWS_RELEVANCE_INGEST_TOKEN_HEADER.strip()
            if not header_name:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "News relevance ingest token header not configured"},
                )
            supplied_token = request.headers.get(header_name, "")
            if not hmac.compare_digest(supplied_token, expected_token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid news relevance ingest token"},
                )
            return None
```

주의: 이 분기를 삽입할 위치는 HERMES 분기와 동일한 함수 내 — 삽입 전 해당 함수 전체를 읽고 기존 분기들과 같은 패턴(이른 return)을 유지할 것.

- [ ] **Step 4: Commit**

```bash
git add app/core/config.py app/middleware/auth.py tests/routers/test_news_relevance_auth.py
git commit -m "feat(ROB-491): news-relevance ingest 토큰 게이트 (403/401, default-off)"
```

### Task 9: store 판정 함수 — `list_pending` + `apply_judgment`

**Files:**
- Modify: `app/services/symbol_news_store.py`
- Modify: `tests/services/test_symbol_news_store.py`

- [ ] **Step 1: Write the failing tests** (test_symbol_news_store.py에 추가)

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_judgment_transitions_and_is_idempotent(db_session) -> None:
    await symbol_news_store.upsert_kr_feed_articles(
        db_session, "777771", [_item("https://x/rob491-j1", "네이버 신규 투자")]
    )
    link = (
        await db_session.execute(
            select(SymbolNewsRelevance)
            .join(NewsArticle, NewsArticle.id == SymbolNewsRelevance.article_id)
            .where(NewsArticle.url == "https://x/rob491-j1")
        )
    ).scalar_one()

    status = await symbol_news_store.apply_judgment(
        db_session,
        article_id=link.article_id,
        market="kr",
        symbol="777771",
        relationship="direct",
        relevance="high",
        price_relevance="catalyst",
        score=0.9,
        reason="직접 관련",
        judged_by="hermes",
    )
    assert status == "confirmed"

    # 재판정(overwrite) — unrelated → excluded
    status2 = await symbol_news_store.apply_judgment(
        db_session,
        article_id=link.article_id,
        market="kr",
        symbol="777771",
        relationship="unrelated",
        relevance="low",
        price_relevance="none",
        score=0.2,
        reason="재검토 결과 무관",
        judged_by="hermes",
    )
    assert status2 == "excluded"
    await db_session.refresh(link)
    assert link.status == "excluded"
    assert link.judged_at is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_judgment_missing_link_returns_none(db_session) -> None:
    status = await symbol_news_store.apply_judgment(
        db_session,
        article_id=999999999,
        market="kr",
        symbol="000000",
        relationship="direct",
        relevance="high",
        price_relevance="none",
        score=None,
        reason="x",
        judged_by="hermes",
    )
    assert status is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_pending_returns_article_fields_and_hints(db_session) -> None:
    await symbol_news_store.upsert_kr_feed_articles(
        db_session, "888881", [_item("https://x/rob491-p1", "네이버 D2SF 펀딩")]
    )
    rows = await symbol_news_store.list_pending(db_session, "kr", limit=50, symbol="888881")
    assert rows
    row = rows[0]
    assert row["url"] == "https://x/rob491-p1"
    assert row["title"] == "네이버 D2SF 펀딩"
    assert row["hints"] is not None
    assert isinstance(row["article_id"], int)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_symbol_news_store.py -v -k "judgment or pending"`
Expected: FAIL — `AttributeError: ... 'apply_judgment'`

- [ ] **Step 3: store에 함수 추가**

```python
async def list_pending(
    db: AsyncSession,
    market: str,
    limit: int,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Pending links oldest-first with the article fields a judge needs."""
    conditions = [
        SymbolNewsRelevance.market == market,
        SymbolNewsRelevance.status == "pending",
    ]
    if symbol:
        conditions.append(SymbolNewsRelevance.symbol == symbol)
    stmt = (
        select(NewsArticle, SymbolNewsRelevance)
        .join(SymbolNewsRelevance, SymbolNewsRelevance.article_id == NewsArticle.id)
        .where(*conditions)
        .order_by(SymbolNewsRelevance.first_seen_at.asc(), SymbolNewsRelevance.id.asc())
        .limit(limit)
    )
    rows = await db.execute(stmt)
    return [
        {
            "article_id": article.id,
            "market": link.market,
            "symbol": link.symbol,
            "url": article.url,
            "title": article.title,
            "source": article.source,
            "published_at": (
                article.article_published_at.isoformat()
                if article.article_published_at
                else None
            ),
            "first_seen_at": link.first_seen_at.isoformat(),
            "hints": link.hints,
        }
        for article, link in rows.all()
    ]


async def apply_judgment(
    db: AsyncSession,
    *,
    article_id: int,
    market: str,
    symbol: str,
    relationship: str,
    relevance: str,
    price_relevance: str,
    score: float | None,
    reason: str,
    judged_by: str,
) -> str | None:
    """Idempotent judgment write-back. Returns new status, None if link missing.

    Status is derived server-side (``derive_status``) — the job never sets it.
    """
    link = (
        await db.execute(
            select(SymbolNewsRelevance).where(
                SymbolNewsRelevance.article_id == article_id,
                SymbolNewsRelevance.market == market,
                SymbolNewsRelevance.symbol == symbol,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        return None
    now = _utcnow()
    link.relationship = relationship
    link.relevance = relevance
    link.price_relevance = price_relevance
    link.score = score
    link.reason = reason
    link.judged_by = judged_by
    link.judged_at = now
    link.updated_at = now
    link.status = derive_status(relationship, relevance)
    await db.flush()
    return link.status
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/test_symbol_news_store.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/symbol_news_store.py tests/services/test_symbol_news_store.py
git commit -m "feat(ROB-491): store 판정 표면 — list_pending + apply_judgment (status 서버 파생)"
```

### Task 10: 스키마 + 라우터 + 등록

**Files:**
- Create: `app/schemas/news_relevance.py`
- Create: `app/routers/news_relevance.py`
- Modify: `app/main.py` (`investment_hermes_http` include 근처에 추가)
- Test: `tests/routers/test_news_relevance_auth.py` (Task 8에서 작성됨) + `tests/routers/test_news_relevance_ingest.py`

- [ ] **Step 1: Write the failing functional test**

```python
# tests/routers/test_news_relevance_ingest.py
"""news-relevance pending/ingest functional contract (ROB-491 PR2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.db import get_db
from app.middleware.auth import AuthMiddleware
from app.routers.news_relevance import router as news_relevance_router
from app.services import symbol_news_store
from app.services.symbol_news_store import FeedArticleInput

_HEADERS = {"X-News-Relevance-Ingest-Token": "secret"}


def _build_app(db_session) -> FastAPI:
    app = FastAPI()
    app.include_router(news_relevance_router)
    app.add_middleware(AuthMiddleware)

    async def _db_override() -> AsyncIterator[object]:
        yield db_session

    app.dependency_overrides[get_db] = _db_override
    return app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pending_then_ingest_roundtrip(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "NEWS_RELEVANCE_INGEST_TOKEN", "secret", raising=False
    )
    await symbol_news_store.upsert_kr_feed_articles(
        db_session,
        "666661",
        [
            FeedArticleInput(
                url="https://x/rob491-r1",
                title="네이버 급락 원인",
                source="매일경제",
                published_at=datetime(2026, 6, 10, 9, tzinfo=UTC),
            )
        ],
    )
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        pending = await client.get(
            "/trading/api/news-relevance/pending?market=kr&symbol=666661",
            headers=_HEADERS,
        )
        assert pending.status_code == 200
        rows = pending.json()["pending"]
        assert rows and rows[0]["url"] == "https://x/rob491-r1"
        article_id = rows[0]["article_id"]

        resp = await client.post(
            "/trading/api/news-relevance/ingest/bulk",
            headers=_HEADERS,
            json={
                "judgments": [
                    {
                        "article_id": article_id,
                        "market": "kr",
                        "symbol": "666661",
                        "relationship": "direct",
                        "relevance": "high",
                        "price_relevance": "catalyst",
                        "score": 0.92,
                        "reason": "급락 원인 직접 보도",
                        "judged_by": "hermes",
                    },
                    {
                        "article_id": 999999999,
                        "market": "kr",
                        "symbol": "666661",
                        "relationship": "unrelated",
                        "relevance": "low",
                        "price_relevance": "none",
                        "reason": "무관",
                        "judged_by": "hermes",
                    },
                ]
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] == [
        {"article_id": article_id, "market": "kr", "symbol": "666661", "status": "confirmed"}
    ]
    assert body["errors"] == [
        {"index": 1, "article_id": 999999999, "error": "link_not_found"}
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_invalid_enum_is_422(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "NEWS_RELEVANCE_INGEST_TOKEN", "secret", raising=False
    )
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(
            "/trading/api/news-relevance/ingest/bulk",
            headers=_HEADERS,
            json={
                "judgments": [
                    {
                        "article_id": 1,
                        "market": "kr",
                        "symbol": "x",
                        "relationship": "kinda_related",
                        "relevance": "high",
                        "price_relevance": "none",
                        "reason": "r",
                        "judged_by": "hermes",
                    }
                ]
            },
        )
    assert resp.status_code == 422  # pydantic Literal — loc에 item index 포함
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/routers/test_news_relevance_ingest.py tests/routers/test_news_relevance_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: app.routers.news_relevance`

- [ ] **Step 3: 스키마 작성**

```python
# app/schemas/news_relevance.py
"""Request/response contracts for the news-relevance judgment surface (ROB-491)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class NewsRelevanceJudgment(BaseModel):
    article_id: int
    market: Literal["kr", "us", "crypto"]
    symbol: str = Field(min_length=1, max_length=40)
    relationship: Literal["direct", "material_indirect", "incidental", "unrelated"]
    relevance: Literal["high", "medium", "low"]
    price_relevance: Literal["catalyst", "explainer", "background", "none"]
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=2000)
    judged_by: str = Field(min_length=1, max_length=100)


class NewsRelevanceIngestRequest(BaseModel):
    judgments: list[NewsRelevanceJudgment] = Field(min_length=1, max_length=200)
```

- [ ] **Step 4: 라우터 작성**

```python
# app/routers/news_relevance.py
"""News-relevance judgment surface (ROB-491 PR2).

Token-authed via AuthMiddleware ``NEWS_RELEVANCE_PATH_PREFIX`` branch
(default-off: token unset → 403). Pending read + idempotent judgment ingest.
Status is derived server-side; no broker/order surface.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.schemas.news_relevance import NewsRelevanceIngestRequest
from app.services import symbol_news_store

router = APIRouter(prefix="/trading/api/news-relevance", tags=["news-relevance"])


@router.get("/pending")
async def get_pending(
    market: str = Query(default="kr"),
    limit: int = Query(default=50, ge=1, le=200),
    symbol: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    pending = await symbol_news_store.list_pending(db, market, limit, symbol=symbol)
    return {"market": market, "count": len(pending), "pending": pending}


@router.post("/ingest/bulk")
async def ingest_bulk(
    request: NewsRelevanceIngestRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    applied: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, judgment in enumerate(request.judgments):
        status = await symbol_news_store.apply_judgment(
            db,
            article_id=judgment.article_id,
            market=judgment.market,
            symbol=judgment.symbol,
            relationship=judgment.relationship,
            relevance=judgment.relevance,
            price_relevance=judgment.price_relevance,
            score=judgment.score,
            reason=judgment.reason,
            judged_by=judgment.judged_by,
        )
        if status is None:
            errors.append(
                {
                    "index": index,
                    "article_id": judgment.article_id,
                    "error": "link_not_found",
                }
            )
        else:
            applied.append(
                {
                    "article_id": judgment.article_id,
                    "market": judgment.market,
                    "symbol": judgment.symbol,
                    "status": status,
                }
            )
    await db.commit()
    return {"applied": applied, "errors": errors}
```

`app/main.py` — `app.include_router(investment_hermes_http.router)` 근처에 추가 (import 포함):

```python
from app.routers import news_relevance
app.include_router(news_relevance.router)
```

(main.py의 기존 import 스타일을 따를 것 — 상단 일괄 import면 거기에 추가.)

- [ ] **Step 5: Run all PR2 tests**

Run: `uv run pytest tests/routers/test_news_relevance_auth.py tests/routers/test_news_relevance_ingest.py tests/services/test_symbol_news_store.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add app/schemas/news_relevance.py app/routers/news_relevance.py app/main.py tests/routers/test_news_relevance_ingest.py
git commit -m "feat(ROB-491): news-relevance pending/ingest 라우터 — 항목별 결과 + 멱등 판정"
```

### Task 11: 런북 + CLAUDE.md

**Files:**
- Create: `docs/runbooks/news-relevance-judgment.md`
- Modify: `CLAUDE.md` (다른 feature 섹션들과 같은 위치에 간단 섹션 추가)

- [ ] **Step 1: 런북 작성** — 다음 내용 포함 (기존 runbook 형식 예: `docs/runbooks/live-order-reconcile.md` 참고):

```markdown
# News Relevance Judgment Job (ROB-491)

## 개요
get_news(KR)가 수집·저장한 기사의 종목 관련성 판정을 외부 LLM Job이 수행해
write-back하는 절차. auto_trader는 판정하지 않는다 (스케줄러 연결 없음).

## 활성화 (default-off)
- `NEWS_RELEVANCE_INGEST_TOKEN` 설정 (미설정 시 모든 호출 403)
- 헤더: `X-News-Relevance-Ingest-Token`

## Job 절차 (Hermes류 세션 / operator 수동)
1. `GET /trading/api/news-relevance/pending?market=kr&limit=50`
2. 각 항목 판정 — 기준:
   - relationship: direct(종목 직접) / material_indirect(밸류체인·투자처 등
     실질 연관) / incidental(스치는 언급) / unrelated(무관)
   - relevance: high/medium/low — 투자 판단 유용성
   - price_relevance: catalyst(가격 변동 원인) / explainer(변동 해설) /
     background / none
   - hints는 참고만 (alias_match 있으면 direct 가능성 높음, 보장 아님)
3. `POST /trading/api/news-relevance/ingest/bulk` — judgments[] 배치 (≤200)
4. 응답 errors[] 의 link_not_found는 재수집 대상 아님 (기사 삭제 등) — 무시 가능
5. 멱등: 같은 (article_id, market, symbol) 재판정은 overwrite

## 검증
- 판정 후 `get_news(symbol)` 호출 → excluded_count 증가 + 해당 기사 미노출 확인
- status 파생 규칙: unrelated 또는 low → excluded, 그 외 → confirmed (서버 소유)
```

- [ ] **Step 2: CLAUDE.md 섹션 추가** (다른 §들과 같은 형식, "### investment_report_create item 계약" 섹션 뒤):

```markdown
### get_news 관련성 파이프라인 (ROB-491)

KR get_news는 네이버 피드를 `news_articles` + `symbol_news_relevance`에 set-difference
upsert하고 DB 상태로 응답한다 (excluded만 제외, pending은 상태 표시). 관련성 판정은
외부 Job이 token-authed ingest로만 write-back — **auto_trader 코드는 어떤 기사도
자동 제외하지 않는다** (하드코딩 노이즈 블랙리스트 금지).

- **모델**: `app/models/symbol_news_relevance.SymbolNewsRelevance`
- **저장 서비스**: `app/services/symbol_news_store.py` — 모든 쓰기는 이 모듈 경유
- **라우터**: `app/routers/news_relevance.py` — GET pending / POST ingest/bulk
  (`NEWS_RELEVANCE_INGEST_TOKEN`, default-off)
- **런북**: `docs/runbooks/news-relevance-judgment.md`
- **스케줄러 연결 없음**: Job은 레포 밖(Hermes류 세션/operator)
```

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/news-relevance-judgment.md CLAUDE.md
git commit -m "docs(ROB-491): 판정 Job 런북 + CLAUDE.md 섹션"
```

### Task 12: PR2 게이트 + PR 생성

- [ ] **Step 1: 전체 게이트**

Run:
```bash
uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/
uv run ty check app/
uv run pytest tests/ -m "not integration and not slow" -q
uv run pytest tests/services/test_symbol_news_store.py tests/routers/test_news_relevance_auth.py tests/routers/test_news_relevance_ingest.py -v
```
Expected: 전부 green

- [ ] **Step 2: Push + PR 생성**

```bash
git push -u origin rob-491-pr2
gh pr create --base main --title "feat(ROB-491): news-relevance 판정 ingest 표면 (PR2)" --body "<요약 + 안전경계(token default-off, migration 0, 브로커 mutation 0) + 런북 링크 + operator 활성화 절차>"
```

- [ ] **Step 3: Linear 코멘트** — ROB-491에 PR1/PR2 링크 + operator 체크리스트(① 배포 후 `alembic upgrade head`, ② `NEWS_RELEVANCE_INGEST_TOKEN` 설정, ③ 판정 Job 1회 수동 실행 후 get_news로 excluded 확인) 게시.

---

## 리스크 / 주의

- **Task 4 commit 정책**: store가 `db.commit()`을 직접 호출한다. `db_session` 픽스처가 외부 트랜잭션 rollback 격리를 쓰면 충돌할 수 있음 — 그 경우 store는 `flush`만 하고 commit을 서비스(`_kr_persist_and_load`)와 라우터(`ingest_bulk`)로 옮기는 대안이 plan 본문에 명시돼 있다. 어느 쪽이든 한 가지로 통일.
- **풀 스위트 shared-DB 오염**: 로컬 test_db는 truncate되지 않는다(ROB-460 교훈). store 테스트는 고유 URL 프리픽스(`rob491-`)와 가짜 심볼(999991 등)로 충돌을 피했지만, 반복 실행 시 잔존 행으로 count 단언이 깨지면 단언을 부분집합 비교로 완화할 것.
- **alembic 2-head**: PR1 머지 전 main이 전진해 head가 바뀌면 `down_revision` 재지정(treadmill — 과거 다수 PR에서 발생). 머지 직전 `uv run alembic heads` 재확인.
- **lint는 app/ + tests/ 둘 다** — CI 동일 (ROB-423 교훈).
- **get_news 소비자 영향**: `snapshot_backed/collectors/news.py`가 `result.articles`를 소비한다 — KR 경로의 articles 의미는 "DB canonical (excluded 제외)"로 바뀌지만 dataclass shape은 불변(추가 필드는 기본값). Task 7 풀 스위트에서 해당 collector 테스트가 깨지면 envelope이 아니라 기대값 stale 여부를 먼저 의심할 것.
```
