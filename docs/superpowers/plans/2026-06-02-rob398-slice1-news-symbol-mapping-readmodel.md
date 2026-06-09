# ROB-398 Slice 1 — 뉴스-종목 매핑 read-model + 별칭 확장 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 데이터(article.stock_symbol + news_article_related_symbols + 별칭 matcher) 위에 provenance·is_primary·모호성·freshness를 통합하는 read-only read-model을 만들고 `KR_ALIASES`를 큐레이티드 확장한다.

**Architecture:** 신규 패키지 `app/services/kr_news_symbol_mapping/`. 핵심 로직(provenance 통합·is_primary 파생·freshness)은 **순수 함수**(`resolver.py`/`freshness.py`)로 두어 DB 없이 단위테스트한다. `query_service.py`는 article-provider 의존성 주입으로 조립만 하며 DB는 기본 provider에만 둔다. migration/write/scheduler 없음.

**Tech Stack:** Python 3.13, `@dataclass(frozen=True)`, pytest. 기존 `news_entity_matcher.match_symbols_for_article`/`SymbolMatch`, `news_entity_alias_data.AliasEntry/KR_ALIASES` 재사용. 새 의존성 없음.

**참조 스펙:** `docs/superpowers/specs/2026-06-02-rob398-slice1-news-symbol-mapping-readmodel-design.md`

기존 시그니처(확인됨):
- `AliasEntry(symbol, market, canonical_name, aliases: tuple[str,...])` — `app/services/news_entity_alias_data.py`
- `SymbolMatch(symbol, market, canonical_name, matched_term, reason)`; `match_symbols_for_article(*, title, summary=None, keywords=None, market=None) -> list[SymbolMatch]` (symbol별 dedup, 정렬) — `app/services/news_entity_matcher.py`
- `NewsArticleRelatedSymbol(symbol, source, score, rank, matched_term, ...)`, `NewsArticle(stock_symbol, published_at, scraped_at, title, summary, keywords, market, ...)` — `app/models/news.py`

---

## File Structure

- Create `app/services/kr_news_symbol_mapping/__init__.py` — 공개 표면 re-export
- Create `app/services/kr_news_symbol_mapping/contract.py` — frozen dataclass(`MappedSymbol`, `CandidateRow`, `MappedArticle`, `Freshness`, `SymbolNewsMapping`, `ArticleView`) + 상수(`MAPPING_SOURCE_PRIORITY`, `NER_CONFIDENCE`, `CANDIDATE_DEFAULT_CONFIDENCE`, `FRESHNESS_TTL_HOURS`)
- Create `app/services/kr_news_symbol_mapping/resolver.py` — 순수 `resolve_article_symbols(...)`
- Create `app/services/kr_news_symbol_mapping/freshness.py` — 순수 `derive_freshness(...)`
- Create `app/services/kr_news_symbol_mapping/query_service.py` — DI 기반 `get_symbol_news_mapping(...)`
- Modify `app/services/news_entity_alias_data.py` — `KR_ALIASES` 큐레이티드 확장 + 복합별칭
- Create tests: `tests/test_kr_news_symbol_mapping_resolver.py`, `tests/test_kr_news_symbol_mapping_freshness.py`, `tests/test_kr_news_symbol_mapping_query.py`, `tests/test_kr_aliases_extension.py`

---

## Task 1: contract dataclasses + 상수 (`contract.py`)

**Files:**
- Create: `app/services/kr_news_symbol_mapping/__init__.py` (빈 docstring; Task 6에서 re-export)
- Create: `app/services/kr_news_symbol_mapping/contract.py`
- Test: `tests/test_kr_news_symbol_mapping_resolver.py` (Task 2에서 채움; Task 1은 contract import만 검증)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kr_news_symbol_mapping_resolver.py
import dataclasses

import pytest

from app.services.kr_news_symbol_mapping.contract import (
    CandidateRow,
    Freshness,
    MappedSymbol,
    MAPPING_SOURCE_PRIORITY,
    NER_CONFIDENCE,
)


@pytest.mark.unit
def test_mapped_symbol_is_frozen():
    m = MappedSymbol(
        symbol="005930",
        market="kr",
        mapping_source="naver_code",
        confidence=1.0,
        is_primary=True,
        matched_term=None,
    )
    assert m.confidence == 1.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.is_primary = False  # type: ignore[misc]


@pytest.mark.unit
def test_priority_order_and_ner_confidence_constants():
    assert MAPPING_SOURCE_PRIORITY["naver_code"] < MAPPING_SOURCE_PRIORITY["candidate"]
    assert MAPPING_SOURCE_PRIORITY["candidate"] < MAPPING_SOURCE_PRIORITY["ner"]
    assert 0.0 < NER_CONFIDENCE < 1.0


@pytest.mark.unit
def test_candidate_row_holds_score_rank():
    row = CandidateRow(symbol="000660", source="news_ingestor", score=0.8, rank=1, matched_term="하이닉스")
    assert row.symbol == "000660"
    assert row.score == 0.8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398 && uv run pytest tests/test_kr_news_symbol_mapping_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.kr_news_symbol_mapping.contract`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/kr_news_symbol_mapping/__init__.py
"""KR 뉴스-종목 매핑 read-model (ROB-398 Slice 1)."""
```

```python
# app/services/kr_news_symbol_mapping/contract.py
"""뉴스-종목 매핑 read-model 계약 — provenance-rich 통합 뷰 (ROB-398 Slice 1).

기존 데이터(article.stock_symbol / news_article_related_symbols / 별칭 matcher)
위의 read-only 뷰. write/migration 없음.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# mapping_source 우선순위 (작을수록 우선). naver_code = 확정, ner = 최약.
MAPPING_SOURCE_PRIORITY: dict[str, int] = {
    "naver_code": 0,
    "candidate": 1,
    "ner": 2,
}

NER_CONFIDENCE: float = 0.5
CANDIDATE_DEFAULT_CONFIDENCE: float = 0.7  # candidate row.score 부재 시
FRESHNESS_TTL_HOURS: int = 24


@dataclass(frozen=True)
class CandidateRow:
    """news_article_related_symbols 한 행의 read-only 뷰 (candidate source)."""

    symbol: str
    source: str
    score: float | None = None
    rank: int | None = None
    matched_term: str | None = None


@dataclass(frozen=True)
class MappedSymbol:
    symbol: str
    market: str
    mapping_source: str        # "naver_code" | "candidate" | "ner"
    confidence: float          # 0.0..1.0
    is_primary: bool
    matched_term: str | None


@dataclass(frozen=True)
class ArticleView:
    """resolver/query_service 입력용 기사 뷰 (DB ORM 비의존, 테스트 친화)."""

    market: str
    stock_symbol: str | None
    related_rows: tuple[CandidateRow, ...]
    title: str | None
    summary: str | None
    keywords: tuple[str, ...]
    as_of: datetime


@dataclass(frozen=True)
class MappedArticle:
    as_of: datetime
    title: str | None
    mapped_symbols: tuple[MappedSymbol, ...]


@dataclass(frozen=True)
class Freshness:
    overall: str               # "fresh" | "stale" | "unavailable"
    latest_as_of: datetime | None
    stale_reason: str | None


@dataclass(frozen=True)
class SymbolNewsMapping:
    symbol: str
    market: str
    articles: tuple[MappedArticle, ...]
    freshness: Freshness
```

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398 && uv run pytest tests/test_kr_news_symbol_mapping_resolver.py -v && uv run ruff check app/services/kr_news_symbol_mapping/`
Expected: PASS (3 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-398
git add app/services/kr_news_symbol_mapping/__init__.py app/services/kr_news_symbol_mapping/contract.py tests/test_kr_news_symbol_mapping_resolver.py
git commit -m "feat(ROB-398): 뉴스-종목 매핑 read-model 계약 dataclass + 상수

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: provenance 통합 + is_primary 파생 (`resolver.py`)

**Files:**
- Create: `app/services/kr_news_symbol_mapping/resolver.py`
- Test: `tests/test_kr_news_symbol_mapping_resolver.py` (Task 1 파일에 추가)

- [ ] **Step 1: Write the failing test (append)**

`tests/test_kr_news_symbol_mapping_resolver.py` 에 추가:

```python
from app.services.kr_news_symbol_mapping.resolver import resolve_article_symbols
from app.services.news_entity_matcher import SymbolMatch


def _ner(symbol, term="x"):
    return SymbolMatch(symbol=symbol, market="kr", canonical_name=symbol, matched_term=term, reason="alias_dict")


@pytest.mark.unit
def test_naver_code_is_confirmed_primary():
    out = resolve_article_symbols(
        market="kr", stock_symbol="005930", related_rows=(), ner_matches=()
    )
    assert len(out) == 1
    assert out[0].symbol == "005930"
    assert out[0].mapping_source == "naver_code"
    assert out[0].confidence == 1.0
    assert out[0].is_primary is True


@pytest.mark.unit
def test_candidate_source_uses_score_and_matched_term():
    out = resolve_article_symbols(
        market="kr",
        stock_symbol=None,
        related_rows=(CandidateRow(symbol="000660", source="news_ingestor", score=0.8, rank=1, matched_term="하이닉스"),),
        ner_matches=(),
    )
    assert len(out) == 1
    assert out[0].mapping_source == "candidate"
    assert out[0].confidence == 0.8
    assert out[0].is_primary is True  # 단일 후보


@pytest.mark.unit
def test_candidate_missing_score_uses_default_confidence():
    out = resolve_article_symbols(
        market="kr",
        stock_symbol=None,
        related_rows=(CandidateRow(symbol="000660", source="x", score=None, rank=2),),
        ner_matches=(),
    )
    assert out[0].confidence == 0.7  # CANDIDATE_DEFAULT_CONFIDENCE


@pytest.mark.unit
def test_ner_single_match_is_primary():
    out = resolve_article_symbols(
        market="kr", stock_symbol=None, related_rows=(), ner_matches=(_ner("035420", "네이버"),)
    )
    assert out[0].mapping_source == "ner"
    assert out[0].confidence == 0.5
    assert out[0].is_primary is True


@pytest.mark.unit
def test_ner_ambiguity_holds_back_is_primary():
    # 이름충돌(복합 별칭 "삼전닉스" → 005930 + 000660), 확정/후보 disambiguator 없음.
    out = resolve_article_symbols(
        market="kr",
        stock_symbol=None,
        related_rows=(),
        ner_matches=(_ner("005930", "삼전닉스"), _ner("000660", "삼전닉스")),
    )
    assert {m.symbol for m in out} == {"005930", "000660"}
    assert all(m.is_primary is False for m in out)  # 강제 단일 금지


@pytest.mark.unit
def test_higher_priority_source_wins_per_symbol():
    # 같은 symbol이 candidate + ner 둘 다 → candidate(우선)로 합쳐짐.
    out = resolve_article_symbols(
        market="kr",
        stock_symbol=None,
        related_rows=(CandidateRow(symbol="035420", source="x", score=0.9),),
        ner_matches=(_ner("035420", "네이버"),),
    )
    assert len(out) == 1
    assert out[0].mapping_source == "candidate"
    assert out[0].confidence == 0.9


@pytest.mark.unit
def test_naver_code_present_makes_other_symbols_non_primary():
    out = resolve_article_symbols(
        market="kr",
        stock_symbol="005930",
        related_rows=(),
        ner_matches=(_ner("000660", "하이닉스"),),
    )
    by_symbol = {m.symbol: m for m in out}
    assert by_symbol["005930"].is_primary is True
    assert by_symbol["000660"].is_primary is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398 && uv run pytest tests/test_kr_news_symbol_mapping_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: ...resolver`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/kr_news_symbol_mapping/resolver.py
"""provenance 통합 + is_primary 파생 (ROB-398 Slice 1, 순수 함수).

우선순위 naver_code(1.0) > candidate > ner. is_primary 는 확정 소스(naver_code)
또는 단일 후보일 때만 True; 그 외(복수 후보, 확정 없음)면 전부 False(모호성 보류).
"""

from __future__ import annotations

from collections.abc import Sequence

from app.services.kr_news_symbol_mapping.contract import (
    CANDIDATE_DEFAULT_CONFIDENCE,
    MAPPING_SOURCE_PRIORITY,
    NER_CONFIDENCE,
    CandidateRow,
    MappedSymbol,
)
from app.services.news_entity_matcher import SymbolMatch


def _candidate_confidence(score: float | None) -> float:
    if score is None:
        return CANDIDATE_DEFAULT_CONFIDENCE
    return max(0.0, min(1.0, float(score)))


def resolve_article_symbols(
    *,
    market: str,
    stock_symbol: str | None,
    related_rows: Sequence[CandidateRow],
    ner_matches: Sequence[SymbolMatch],
) -> list[MappedSymbol]:
    # symbol -> (priority, mapping_source, confidence, matched_term)
    best: dict[str, tuple[int, str, float, str | None]] = {}

    def _offer(symbol: str, source: str, confidence: float, matched_term: str | None) -> None:
        symbol = symbol.upper()
        priority = MAPPING_SOURCE_PRIORITY[source]
        existing = best.get(symbol)
        if existing is None or priority < existing[0]:
            best[symbol] = (priority, source, confidence, matched_term)

    if stock_symbol:
        _offer(stock_symbol, "naver_code", 1.0, None)
    for row in related_rows:
        _offer(row.symbol, "candidate", _candidate_confidence(row.score), row.matched_term)
    for match in ner_matches:
        _offer(match.symbol, "ner", NER_CONFIDENCE, match.matched_term)

    if not best:
        return []

    confirmed_symbol = stock_symbol.upper() if stock_symbol else None
    only_one = len(best) == 1

    out: list[MappedSymbol] = []
    for symbol in sorted(best):
        _priority, source, confidence, matched_term = best[symbol]
        if confirmed_symbol is not None:
            is_primary = symbol == confirmed_symbol
        else:
            is_primary = only_one
        out.append(
            MappedSymbol(
                symbol=symbol,
                market=market,
                mapping_source=source,
                confidence=confidence,
                is_primary=is_primary,
                matched_term=matched_term,
            )
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398 && uv run pytest tests/test_kr_news_symbol_mapping_resolver.py -v && uv run ruff check app/services/kr_news_symbol_mapping/resolver.py`
Expected: PASS (10 passed total in file); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-398
git add app/services/kr_news_symbol_mapping/resolver.py tests/test_kr_news_symbol_mapping_resolver.py
git commit -m "feat(ROB-398): provenance 통합 resolver + 모호시 is_primary 보류

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: freshness 파생 (`freshness.py`)

**Files:**
- Create: `app/services/kr_news_symbol_mapping/freshness.py`
- Test: `tests/test_kr_news_symbol_mapping_freshness.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kr_news_symbol_mapping_freshness.py
from datetime import datetime, timedelta, timezone

import pytest

from app.services.kr_news_symbol_mapping.freshness import derive_freshness

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)


@pytest.mark.unit
def test_no_articles_is_unavailable():
    f = derive_freshness([], now=NOW, ttl_hours=24)
    assert f.overall == "unavailable"
    assert f.latest_as_of is None
    assert f.stale_reason == "no_mapped_news"


@pytest.mark.unit
def test_recent_is_fresh():
    f = derive_freshness([NOW - timedelta(hours=2)], now=NOW, ttl_hours=24)
    assert f.overall == "fresh"
    assert f.latest_as_of == NOW - timedelta(hours=2)
    assert f.stale_reason is None


@pytest.mark.unit
def test_older_than_ttl_is_stale():
    f = derive_freshness(
        [NOW - timedelta(hours=30), NOW - timedelta(hours=48)], now=NOW, ttl_hours=24
    )
    assert f.overall == "stale"
    assert f.latest_as_of == NOW - timedelta(hours=30)  # 가장 신선한 것 기준
    assert f.stale_reason == "older_than_ttl"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398 && uv run pytest tests/test_kr_news_symbol_mapping_freshness.py -v`
Expected: FAIL — `ModuleNotFoundError: ...freshness`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/kr_news_symbol_mapping/freshness.py
"""뉴스-종목 매핑 freshness 파생 (ROB-398 Slice 1, 순수 함수).

가장 신선한 매핑 기사 as_of 가 TTL 내면 fresh, 초과면 stale, 0건이면
unavailable. reason 을 동봉해 숨김 없이 노출한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from app.services.kr_news_symbol_mapping.contract import (
    FRESHNESS_TTL_HOURS,
    Freshness,
)


def derive_freshness(
    as_ofs: Sequence[datetime],
    *,
    now: datetime,
    ttl_hours: int = FRESHNESS_TTL_HOURS,
) -> Freshness:
    if not as_ofs:
        return Freshness(overall="unavailable", latest_as_of=None, stale_reason="no_mapped_news")
    latest = max(as_ofs)
    if now - latest <= timedelta(hours=ttl_hours):
        return Freshness(overall="fresh", latest_as_of=latest, stale_reason=None)
    return Freshness(overall="stale", latest_as_of=latest, stale_reason="older_than_ttl")
```

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398 && uv run pytest tests/test_kr_news_symbol_mapping_freshness.py -v && uv run ruff check app/services/kr_news_symbol_mapping/freshness.py`
Expected: PASS (3 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-398
git add app/services/kr_news_symbol_mapping/freshness.py tests/test_kr_news_symbol_mapping_freshness.py
git commit -m "feat(ROB-398): 뉴스 매핑 freshness 파생(fresh/stale/unavailable + reason)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 별칭 큐레이티드 확장 + 복합별칭 (`news_entity_alias_data.py`)

**Files:**
- Modify: `app/services/news_entity_alias_data.py` (`KR_ALIASES` 튜플)
- Test: `tests/test_kr_aliases_extension.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kr_aliases_extension.py
import pytest

from app.services.news_entity_matcher import match_symbols


def _symbols(text):
    return {m.symbol for m in match_symbols(text, market="kr")}


@pytest.mark.unit
def test_new_curated_aliases_match():
    assert "000270" in _symbols("기아 신차 출시")          # 기아
    assert "006400" in _symbols("삼성SDI 배터리 수주")       # 삼성SDI
    assert "068270" in _symbols("셀트리온 바이오시밀러 허가")  # 셀트리온


@pytest.mark.unit
def test_compound_alias_maps_to_multiple_symbols_ambiguous():
    # "삼전닉스" → 삼성전자(005930) + SK하이닉스(000660) 동시 매칭(이름충돌 시연).
    syms = _symbols("오늘 삼전닉스 강세")
    assert "005930" in syms
    assert "000660" in syms


@pytest.mark.unit
def test_no_false_positive_on_unrelated_text():
    # 무관 텍스트는 매칭되지 않아야 한다(precision 회귀).
    assert _symbols("점심 삼겹살 맛집 추천") == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398 && uv run pytest tests/test_kr_aliases_extension.py -v`
Expected: FAIL — 신규 별칭/복합별칭 미존재로 `000270`/`006400`/`068270`/`삼전닉스` 매칭 실패.

- [ ] **Step 3: Modify `KR_ALIASES`**

`app/services/news_entity_alias_data.py` 의 `KR_ALIASES` 튜플에서:

1. 기존 `005930`(삼성전자)·`000660`(SK하이닉스) 엔트리의 `aliases` 에 복합별칭 `"삼전닉스"` 를 **양쪽 모두** 추가:

```python
    AliasEntry("005930", "kr", "삼성전자", ("삼성전자", "삼전", "삼전닉스", "Samsung Electronics")),
    AliasEntry(
        "000660", "kr", "SK하이닉스", ("SK하이닉스", "하이닉스", "닉스", "삼전닉스", "SK Hynix")
    ),
```

2. 튜플 끝(`373220` 다음)에 큐레이티드 신규 엔트리 추가(고신호·precision 우선, 모호한 짧은 토큰 회피):

```python
    AliasEntry("000270", "kr", "기아", ("기아", "기아차", "Kia")),
    AliasEntry("006400", "kr", "삼성SDI", ("삼성SDI", "삼성에스디아이")),
    AliasEntry("068270", "kr", "셀트리온", ("셀트리온",)),
    AliasEntry("066570", "kr", "LG전자", ("LG전자", "엘지전자", "LG Electronics")),
    AliasEntry("105560", "kr", "KB금융", ("KB금융", "KB금융지주")),
```

(주의: `"기아"`는 2글자지만 KR 한글 substring 매칭이라 "기아차/기아자동차" 등에서 안전. 짧은 영문 토큰은 추가 금지 — word-boundary라도 오탐 위험.)

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398 && uv run pytest tests/test_kr_aliases_extension.py -v && uv run ruff check app/services/news_entity_alias_data.py tests/test_kr_aliases_extension.py`
Expected: PASS (3 passed); ruff clean.

- [ ] **Step 5: 기존 별칭/엔티티 테스트 회귀 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398 && uv run pytest tests/ -k "alias or entity_match or news_entity" -q`
Expected: PASS (기존 매처 테스트 무회귀). 실패 시 신규 별칭이 기존 기대값과 충돌하는지 확인 후 조정.

- [ ] **Step 6: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-398
git add app/services/news_entity_alias_data.py tests/test_kr_aliases_extension.py
git commit -m "feat(ROB-398): KR_ALIASES 큐레이티드 확장 + 복합별칭(삼전닉스) 모호성 시연

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: query_service 조립 (DI, `query_service.py`)

**Files:**
- Create: `app/services/kr_news_symbol_mapping/query_service.py`
- Test: `tests/test_kr_news_symbol_mapping_query.py`

`get_symbol_news_mapping` 은 article-provider(콜러블)로 `ArticleView` 시퀀스를 받아 per-article resolve → target symbol 포함 기사만 모아 freshness 파생. 기본 provider는 DB(후속에서 실 연결); 본 슬라이스는 DI로 테스트하고 기본 provider는 미연결 시 빈 결과를 honest 반환.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kr_news_symbol_mapping_query.py
from datetime import datetime, timedelta, timezone

import pytest

from app.services.kr_news_symbol_mapping.contract import ArticleView, CandidateRow
from app.services.kr_news_symbol_mapping.query_service import get_symbol_news_mapping

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_assembles_mapping_for_target_symbol_with_freshness():
    articles = [
        ArticleView(
            market="kr",
            stock_symbol="005930",  # naver_code 확정
            related_rows=(),
            title="삼성전자 신규 투자",
            summary=None,
            keywords=(),
            as_of=NOW - timedelta(hours=1),
        ),
        ArticleView(
            market="kr",
            stock_symbol=None,
            related_rows=(),
            title="네이버 실적 발표",  # NER로 035420 매핑 → target과 무관
            summary=None,
            keywords=(),
            as_of=NOW - timedelta(hours=2),
        ),
    ]

    async def provider(symbol, market, hours, limit):
        return articles

    result = await get_symbol_news_mapping(
        "005930", market="kr", hours=24, limit=20, now=NOW, article_provider=provider
    )

    assert result.symbol == "005930"
    # target symbol(005930)을 매핑한 기사만 포함
    assert len(result.articles) == 1
    primary = result.articles[0].mapped_symbols[0]
    assert primary.symbol == "005930"
    assert primary.mapping_source == "naver_code"
    assert primary.is_primary is True
    assert result.freshness.overall == "fresh"


@pytest.mark.asyncio
async def test_no_matching_articles_is_unavailable():
    async def provider(symbol, market, hours, limit):
        return []

    result = await get_symbol_news_mapping(
        "005930", market="kr", now=NOW, article_provider=provider
    )
    assert result.articles == ()
    assert result.freshness.overall == "unavailable"


@pytest.mark.asyncio
async def test_candidate_row_article_maps_target():
    articles = [
        ArticleView(
            market="kr",
            stock_symbol=None,
            related_rows=(CandidateRow(symbol="000660", source="news_ingestor", score=0.8, rank=1),),
            title="반도체 업황",
            summary=None,
            keywords=(),
            as_of=NOW - timedelta(hours=3),
        )
    ]

    async def provider(symbol, market, hours, limit):
        return articles

    result = await get_symbol_news_mapping(
        "000660", market="kr", now=NOW, article_provider=provider
    )
    assert len(result.articles) == 1
    m = result.articles[0].mapped_symbols[0]
    assert m.mapping_source == "candidate"
    assert m.confidence == 0.8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398 && uv run pytest tests/test_kr_news_symbol_mapping_query.py -v`
Expected: FAIL — `ModuleNotFoundError: ...query_service`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/kr_news_symbol_mapping/query_service.py
"""뉴스-종목 매핑 read-model query_service (ROB-398 Slice 1).

article-provider(DI)로 ArticleView 들을 받아 per-article provenance 를 resolve 하고
target symbol 을 매핑한 기사만 모아 freshness 와 함께 반환. read-only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone

from app.services.kr_news_symbol_mapping.contract import (
    FRESHNESS_TTL_HOURS,
    ArticleView,
    MappedArticle,
    SymbolNewsMapping,
)
from app.services.kr_news_symbol_mapping.freshness import derive_freshness
from app.services.kr_news_symbol_mapping.resolver import resolve_article_symbols
from app.services.news_entity_matcher import match_symbols_for_article

ArticleProvider = Callable[[str, str, int, int], Awaitable[Sequence[ArticleView]]]


async def _empty_provider(symbol: str, market: str, hours: int, limit: int) -> list[ArticleView]:
    # 기본 provider: DB 연결은 후속 슬라이스. 미연결 시 honest 빈 결과.
    return []


async def get_symbol_news_mapping(
    symbol: str,
    *,
    market: str = "kr",
    hours: int = 24,
    limit: int = 20,
    now: datetime | None = None,
    ttl_hours: int = FRESHNESS_TTL_HOURS,
    article_provider: ArticleProvider | None = None,
) -> SymbolNewsMapping:
    now = now or datetime.now(timezone.utc)
    provider = article_provider or _empty_provider
    target = symbol.upper()

    raw_articles = await provider(symbol, market, hours, limit)

    mapped_articles: list[MappedArticle] = []
    as_ofs: list[datetime] = []
    for av in raw_articles:
        ner_matches = match_symbols_for_article(
            title=av.title, summary=av.summary, keywords=av.keywords, market=market
        )
        mapped = resolve_article_symbols(
            market=market,
            stock_symbol=av.stock_symbol,
            related_rows=av.related_rows,
            ner_matches=ner_matches,
        )
        if not any(m.symbol == target for m in mapped):
            continue
        # target 매핑을 앞으로 정렬(소비자 편의), 결정적 순서 유지.
        ordered = tuple(
            sorted(mapped, key=lambda m: (m.symbol != target, m.symbol))
        )
        mapped_articles.append(
            MappedArticle(as_of=av.as_of, title=av.title, mapped_symbols=ordered)
        )
        as_ofs.append(av.as_of)

    freshness = derive_freshness(as_ofs, now=now, ttl_hours=ttl_hours)
    return SymbolNewsMapping(
        symbol=target,
        market=market,
        articles=tuple(mapped_articles),
        freshness=freshness,
    )
```

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398 && uv run pytest tests/test_kr_news_symbol_mapping_query.py -v && uv run ruff check app/services/kr_news_symbol_mapping/query_service.py`
Expected: PASS (3 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-398
git add app/services/kr_news_symbol_mapping/query_service.py tests/test_kr_news_symbol_mapping_query.py
git commit -m "feat(ROB-398): 뉴스-종목 매핑 query_service(DI provider + freshness 조립)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: 패키지 공개 표면 + 전체 검증

**Files:**
- Modify: `app/services/kr_news_symbol_mapping/__init__.py`

- [ ] **Step 1: __init__ re-export**

```python
# app/services/kr_news_symbol_mapping/__init__.py
"""KR 뉴스-종목 매핑 read-model (ROB-398 Slice 1)."""

from app.services.kr_news_symbol_mapping.contract import (
    ArticleView,
    CandidateRow,
    Freshness,
    MappedArticle,
    MappedSymbol,
    SymbolNewsMapping,
)
from app.services.kr_news_symbol_mapping.freshness import derive_freshness
from app.services.kr_news_symbol_mapping.query_service import get_symbol_news_mapping
from app.services.kr_news_symbol_mapping.resolver import resolve_article_symbols

__all__ = [
    "ArticleView",
    "CandidateRow",
    "Freshness",
    "MappedArticle",
    "MappedSymbol",
    "SymbolNewsMapping",
    "derive_freshness",
    "get_symbol_news_mapping",
    "resolve_article_symbols",
]
```

- [ ] **Step 2: 전체 모듈 테스트 + lint + format + import-contract**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-398
uv run pytest tests/test_kr_news_symbol_mapping_resolver.py tests/test_kr_news_symbol_mapping_freshness.py tests/test_kr_news_symbol_mapping_query.py tests/test_kr_aliases_extension.py -v
uv run ruff check app/services/kr_news_symbol_mapping/ app/services/news_entity_alias_data.py tests/test_kr_news_symbol_mapping_*.py tests/test_kr_aliases_extension.py
uv run ruff format --check app/services/kr_news_symbol_mapping/ tests/test_kr_news_symbol_mapping_*.py tests/test_kr_aliases_extension.py
uv run pytest tests/test_import_contracts.py -q
```
Expected: 전부 PASS; ruff clean. (kr_news_symbol_mapping 은 services 내부 + news_entity_matcher 만 import → import-contract 위반 없음.)

- [ ] **Step 3: 별칭 변경 인접 회귀 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-398 && uv run pytest tests/ -k "alias or entity_match or news_entity or news" -q`
Expected: PASS. (KR_ALIASES 확장이 기존 뉴스/매처 테스트를 깨지 않음 확인.)

- [ ] **Step 4: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-398
git add app/services/kr_news_symbol_mapping/__init__.py
git commit -m "feat(ROB-398): kr_news_symbol_mapping 패키지 공개 표면 re-export

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review (작성자 체크)

**Spec coverage:**
- §3 아키텍처(패키지 + 순수 resolver/freshness + DI query_service) → Task 1/2/3/5 ✅
- §3 mapping_source 3소스(naver_code/candidate/ner) + confidence → Task 2 ✅
- §4 우선순위 + is_primary 파생 + 모호성 보류 → Task 2 (`test_ner_ambiguity_holds_back_is_primary`) ✅
- §5 freshness(fresh/stale/unavailable + reason) → Task 3 ✅
- §6 별칭 큐레이티드 확장 + 복합별칭 + false-positive 회귀 → Task 4 ✅
- §7 테스트 6종 → Task 2/3/4/5 전반 ✅
- §2 migration 0 / read-only / write 없음 → 어떤 Task도 모델/마이그레이션/ingest write 미추가 ✅

**Placeholder scan:** 모든 step 에 실제 코드/명령/기대 출력. placeholder 없음.

**Type consistency:** `MappedSymbol`/`CandidateRow`/`ArticleView`/`MappedArticle`/`Freshness`/`SymbolNewsMapping`(Task1) → `resolve_article_symbols`(Task2)/`derive_freshness`(Task3)/`get_symbol_news_mapping`(Task5)에서 일관 사용. `MAPPING_SOURCE_PRIORITY`/`NER_CONFIDENCE`/`CANDIDATE_DEFAULT_CONFIDENCE`/`FRESHNESS_TTL_HOURS` 상수 일관. `SymbolMatch`(기존, symbol/market/canonical_name/matched_term/reason) 필드 정확.

**검증 시 주의:**
- Task 4 의 신규 별칭이 기존 매처 테스트(`tests/` 의 alias/entity 테스트)와 충돌하면 Step 5에서 표면화 → 조정.
- `match_symbols_for_article` 의 keywords 인자는 Iterable; `ArticleView.keywords`는 tuple 로 전달(정상).
