# ROB-169 KR News Investment Relevance & Toss-Style Ranking Quality Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic KR investment-relevance gate to `/invest/api/feed/news` so society/crime/no-symbol-only KR articles like `'광주 여고생 살해' 피의자 사이코패스 검사 결과 공개된다` are demoted to noise (and removed from the `kr` tab), while preserving market-wide KR signals (KOSPI/KOSDAQ/IPO/ETF/금리/환율/원자재/반도체/정책/산업영향) that legitimately have no `stock_symbol`/`relatedSymbols`. Issue-chip rendering for these noise rows must also be suppressed via `noiseReason`/`tags`.

**Architecture:** Mirror the ROB-155 read-layer pattern — pure function, no migrations, no ingestion changes. Add `app/services/kr_news_relevance_service.py::score_kr_news_article()` returning a `KrNewsRelevance` dataclass (`score`, `bucket`, `category`, `include_in_briefing`, `matched_terms`, `noise_reason`). Wire it into `feed_news_service.build_feed_news()` for `market_value == "kr"`, after symbol enrichment but before `relation` derivation, parallel to the existing crypto branch. Drop crypto-style "drop only when no relatedSymbols" rule on the `kr` tab. Suppress `issueId` for noise-flagged KR rows. Surface `noiseReason`/`category`/`tags`/`scope` to the frontend type but do NOT change rendering yet (no UX risk).

**Tech Stack:** Python 3.13 + FastAPI + SQLAlchemy 2 async, pydantic v2 (`extra="forbid"`), pytest 9 + pytest-asyncio. Frontend TypeScript (`frontend/invest/src/types/feedNews.ts`). All work in worktree `/Users/mgh3326/.hermes/hermes-agent/.worktrees/t_1706a804` on branch `feature/ROB-169-kr-news-relevance` (already created, baseline at `8ab37c3c`).

**Pre-flight (one-time before Task 1):**

```bash
cd /Users/mgh3326/.hermes/hermes-agent/.worktrees/t_1706a804
uv sync --all-groups
uv run pytest tests/test_feed_news_scope.py tests/test_feed_news_crypto_filter.py tests/test_invest_feed_news_router.py tests/test_news_entity_matcher.py -v
# Expected: PASS (baseline). If anything fails on main (8ab37c3c), STOP and report.
```

---

## File Structure

**New files (created):**
- `app/services/kr_news_relevance_service.py` — pure scorer, `KrNewsRelevance` dataclass, `score_kr_news_article()`, `user_facing_kr_category()`, KR alias-data constants.
- `tests/test_kr_news_relevance_service.py` — unit tests for the scorer.
- `tests/test_feed_news_kr_filter.py` — unit tests for the integration in `build_feed_news`.
- `tests/fixtures/kr_news_relevance/positive_market_wide.json` — fixture: KR positive examples (KOSPI/IPO/금리/환율/반도체 등) without `stock_symbol`.
- `tests/fixtures/kr_news_relevance/negative_society_crime.json` — fixture: KR society/crime/연예/스포츠/사고 examples that must be flagged as noise.
- `tests/fixtures/kr_news_relevance/borderline.json` — fixture: ambiguous policy/government articles that must stay despite no symbol (positive control).

**Modified files:**
- `app/services/news_entity_alias_data.py` — append `KR_BROAD_MARKET_TERMS`, `KR_INVEST_KEYWORDS`, `KR_NOISE_TERMS`, `KR_SOCIETY_TERMS`, `KR_CRIME_TERMS` constants. Append `KR_BIG_CAP_GROUP_SYMBOLS` (read-only demotion list mirror of `US_BIG_TECH_GROUP_SYMBOLS`).
- `app/services/invest_view_model/feed_news_service.py` — import `score_kr_news_article`, `user_facing_kr_category`. Inside the `for row in rows:` loop, branch on `market_value == "kr"` parallel to the existing `if market_value == "crypto":` block (lines 423–433). Suppress `issueId` for KR noise rows. After the loop, drop noise-flagged rows on `tab == "kr"` (mirror crypto filter at lines 459–460), guarded so positive market-wide rows are kept.
- `app/schemas/invest_feed_news.py` — extend `NewsScope` literal with `"kr_market_wide"` to allow the KR scope to be expressed without overloading the US-tagged `"market_wide"`. Defaults remain `"symbol_specific"`.
- `frontend/invest/src/types/feedNews.ts` — add additive `scope?`, `tags?`, `category?`, `noiseReason?` to `FeedNewsItem`. No render change in `NewsListItem.tsx` (out of scope).
- `tests/test_invest_feed_news_router.py` — add 3 regression tests (positive market-wide KR with no symbol stays, negative society/crime KR is dropped on `kr` tab, issue chip suppressed for noise).
- `docs/runbooks/kr-news-relevance.md` — short runbook (input/output, how to extend term lists, how to verify in prod).

**Out of scope (deliberately):**
- No DB migrations, no ingestion-time scoring, no Prefect/scheduler changes.
- No frontend rendering changes — only type widening so future UX work compiles.
- No tvscreener KR ingest changes (treated as supplemental high-confidence source, untouched).
- No broker/order/watch/order-intent/live/paper/KIS/Upbit mutation. No production DB writes.

---

## Task 1: Add KR alias-data constants

**Files:**
- Modify: `app/services/news_entity_alias_data.py:101` (append at end of file, after existing US block)

- [ ] **Step 1: Write the failing test**

`tests/test_news_entity_alias_data_kr.py` (new file):

```python
"""ROB-169: KR alias-data smoke tests for KR investment relevance constants."""

from __future__ import annotations

from app.services.news_entity_alias_data import (
    KR_BIG_CAP_GROUP_SYMBOLS,
    KR_BROAD_MARKET_TERMS,
    KR_CRIME_TERMS,
    KR_INVEST_KEYWORDS,
    KR_NOISE_TERMS,
    KR_SOCIETY_TERMS,
)


def test_kr_broad_market_terms_include_indices_and_macro():
    expected = {"코스피", "코스닥", "kospi", "kosdaq", "기준금리", "환율", "원달러", "ipo"}
    assert expected.issubset({t.lower() for t in KR_BROAD_MARKET_TERMS})


def test_kr_invest_keywords_cover_core_industries_and_policy():
    expected = {"반도체", "배터리", "etf", "공모주", "상장", "금융위", "한국은행"}
    assert expected.issubset({t.lower() for t in KR_INVEST_KEYWORDS})


def test_kr_society_terms_cover_crime_and_celebrity_noise():
    assert "살해" in KR_CRIME_TERMS
    assert "피의자" in KR_CRIME_TERMS
    assert "연예" in KR_SOCIETY_TERMS or "연예인" in KR_SOCIETY_TERMS
    assert "사이코패스" in KR_NOISE_TERMS or "사이코패스" in KR_CRIME_TERMS


def test_kr_big_cap_group_symbols_includes_top_kospi():
    assert {"005930", "000660"}.issubset(KR_BIG_CAP_GROUP_SYMBOLS)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_news_entity_alias_data_kr.py -v
```

Expected: FAIL with `ImportError: cannot import name 'KR_BROAD_MARKET_TERMS'`.

- [ ] **Step 3: Write minimal implementation**

Append to `app/services/news_entity_alias_data.py` (after line 100):

```python

# ROB-169: KR investment relevance constants.
# Broad-market terms that indicate a market-wide investment story even without
# a specific stock_symbol/relatedSymbols. Keep tight and high-precision.
KR_BROAD_MARKET_TERMS: tuple[str, ...] = (
    "코스피",
    "코스닥",
    "kospi",
    "kosdaq",
    "코스피200",
    "kospi200",
    "코스닥150",
    "krx",
    "유가증권",
    "지수",
    "선물",
    "옵션",
    "etf",
    "etn",
    "리츠",
    "공모주",
    "ipo",
    "상장",
    "상폐",
    "유상증자",
    "무상증자",
    "배당",
    "배당락",
    "기준금리",
    "한국은행",
    "한은",
    "금융통화위원회",
    "금통위",
    "환율",
    "원달러",
    "원/달러",
    "달러원",
    "위안화",
    "엔화",
    "유가",
    "wti",
    "원유",
    "금값",
    "구리",
    "철광석",
    "리튬",
    "대출금리",
    "물가",
    "소비자물가",
    "cpi",
    "ppi",
    "gdp",
    "수출",
    "수입",
    "무역수지",
    "경상수지",
)

# Industry / policy / sector keywords that signal investment relevance even
# when the article does not name a specific listed company.
KR_INVEST_KEYWORDS: tuple[str, ...] = (
    "반도체",
    "메모리",
    "디램",
    "낸드",
    "파운드리",
    "hbm",
    "ai 반도체",
    "배터리",
    "이차전지",
    "전고체",
    "양극재",
    "음극재",
    "전기차",
    "수소차",
    "조선",
    "방산",
    "원전",
    "smr",
    "바이오",
    "제약",
    "신약",
    "임상",
    "건설",
    "부동산",
    "리츠",
    "상업용 부동산",
    "통신",
    "5g",
    "6g",
    "철강",
    "석유화학",
    "정유",
    "유통",
    "면세",
    "엔터",
    "콘텐츠",
    "ott",
    "게임",
    "플랫폼",
    "이커머스",
    "물류",
    "해운",
    "항공",
    "관세",
    "수출규제",
    "지원금",
    "보조금",
    "감세",
    "증세",
    "법인세",
    "금융위",
    "금감원",
    "공정위",
    "세제개편",
    "예산",
    "재정",
    "한미 정상회담",
    "한일 정상회담",
)

# Society/crime/celebrity/sports/accident noise terms — used to suppress KR
# rows that are neither symbol-specific nor market-wide.
KR_CRIME_TERMS: tuple[str, ...] = (
    "살해",
    "살인",
    "강도",
    "강간",
    "성폭행",
    "성추행",
    "납치",
    "감금",
    "유괴",
    "협박",
    "폭행",
    "폭언",
    "음주운전",
    "뺑소니",
    "마약",
    "필로폰",
    "도박",
    "사기",
    "보이스피싱",
    "스미싱",
    "스토킹",
    "피의자",
    "용의자",
    "구속",
    "체포",
    "기소",
    "재판",
    "선고",
    "징역",
    "벌금",
    "사이코패스",
    "성범죄",
    "아동학대",
    "가정폭력",
    "데이트폭력",
)

KR_SOCIETY_TERMS: tuple[str, ...] = (
    "연예",
    "연예인",
    "아이돌",
    "트로트",
    "스캔들",
    "열애",
    "결혼",
    "이혼",
    "재혼",
    "가요",
    "예능",
    "드라마",
    "스포츠",
    "야구",
    "축구",
    "농구",
    "배구",
    "골프",
    "프로야구",
    "kbo",
    "k리그",
    "올림픽",
    "월드컵",
    "아시안게임",
    "교통사고",
    "화재",
    "추락",
    "익사",
    "실종",
    "행방불명",
    "여고생",
    "여중생",
    "초등학생",
    "유치원",
    "어린이집",
    "학교폭력",
    "학폭",
    "층간소음",
    "주거침입",
    "고독사",
)

# Catch-all noise terms that are not strictly society/crime but still pure
# non-investment context. Keep tight to avoid silencing legitimate stories.
KR_NOISE_TERMS: tuple[str, ...] = (
    "날씨",
    "한파",
    "폭염",
    "장마",
    "태풍",
    "황사",
    "미세먼지",
    "운세",
    "복권",
    "로또",
    "맛집",
    "여행",
    "관광",
    "맛벌이",
    "건강검진",
    "다이어트",
    "헬스",
)

# Big-cap KR symbols whose incidental co-mention in market-wide rollup articles
# does not justify keeping the row in a symbol-specific bucket. Currently used
# only in the scope tag — symbol demotion is left to a future ROB.
KR_BIG_CAP_GROUP_SYMBOLS: frozenset[str] = frozenset(
    {"005930", "000660", "035420", "035720", "207940", "005380", "005490", "373220"}
)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_news_entity_alias_data_kr.py -v
```

Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/news_entity_alias_data.py tests/test_news_entity_alias_data_kr.py
git commit -m "feat(rob-169): add KR alias-data constants for investment relevance

KR broad-market, invest-keyword, crime/society/noise term lists and
KR_BIG_CAP_GROUP_SYMBOLS for the upcoming KR news relevance scorer."
```

---

## Task 2: KR news relevance scorer — empty-input contract

**Files:**
- Create: `app/services/kr_news_relevance_service.py`
- Test: `tests/test_kr_news_relevance_service.py`

- [ ] **Step 1: Write the failing test**

`tests/test_kr_news_relevance_service.py` (new file, first test only — more added in later tasks):

```python
"""ROB-169 — KR news investment relevance unit tests."""

from __future__ import annotations

from app.services.kr_news_relevance_service import (
    KrNewsRelevance,
    score_kr_news_article,
    user_facing_kr_category,
)


def test_empty_article_returns_low_relevance_with_no_matches():
    relevance = score_kr_news_article(
        {"title": "", "summary": "", "feed_source": "", "keywords": []}
    )

    assert isinstance(relevance, KrNewsRelevance)
    assert relevance.score == 0
    assert relevance.bucket == "low"
    assert relevance.category is None
    assert relevance.include_in_briefing is False
    assert relevance.matched_terms == []
    assert relevance.noise_reason == "low_kr_relevance"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_kr_news_relevance_service.py::test_empty_article_returns_low_relevance_with_no_matches -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.kr_news_relevance_service'`.

- [ ] **Step 3: Write minimal implementation**

`app/services/kr_news_relevance_service.py`:

```python
"""ROB-169 — KR news investment-relevance scorer.

Read-layer only. Mirrors the ROB-155 crypto/US shape: pure function over the
article view, no DB writes, no ingestion-time gating. The goal is to keep
market-wide KR investment context (KOSPI/IPO/금리/환율/반도체/정책 등) visible
even without a stock_symbol while suppressing pure society/crime/연예/스포츠
articles that have neither a stock_symbol nor a market-wide investment frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.news_entity_alias_data import (
    KR_BIG_CAP_GROUP_SYMBOLS,
    KR_BROAD_MARKET_TERMS,
    KR_CRIME_TERMS,
    KR_INVEST_KEYWORDS,
    KR_NOISE_TERMS,
    KR_SOCIETY_TERMS,
)

_INCLUDE_THRESHOLD = 35


@dataclass(frozen=True)
class KrNewsRelevance:
    score: int
    bucket: str
    category: str | None
    include_in_briefing: bool
    matched_terms: list[str]
    noise_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "bucket": self.bucket,
            "category": self.category,
            "include_in_briefing": self.include_in_briefing,
            "matched_terms": self.matched_terms,
            "noise_reason": self.noise_reason,
        }


_INTERNAL_TO_USER_CATEGORY: dict[str, str] = {
    "kr_macro": "kr_macro",
    "kr_index": "kr_index",
    "kr_industry": "kr_industry",
    "kr_policy": "kr_policy",
    "kr_listing": "kr_listing",
    "kr_symbol": "kr_symbol",
}


def _field(article: Any, name: str) -> Any:
    if isinstance(article, dict):
        return article.get(name)
    return getattr(article, name, None)


def _full_text(article: Any) -> tuple[str, str]:
    title = str(_field(article, "title") or "")
    summary = str(_field(article, "summary") or "")
    keywords = _field(article, "keywords") or []
    keyword_text = " ".join(str(k) for k in keywords if k)
    full = f"{title} {summary} {keyword_text}".lower()
    return title.lower(), full


def _bucket(score: int) -> str:
    if score >= 60:
        return "high"
    if score >= _INCLUDE_THRESHOLD:
        return "medium"
    return "low"


def _has_symbol_anchor(article: Any) -> bool:
    """The caller is expected to pass article rows with stock_symbol attribute or key."""
    return bool(_field(article, "stock_symbol"))


def score_kr_news_article(article: Any) -> KrNewsRelevance:
    """Score one KR-market article for investment relevance.

    Returns a KrNewsRelevance whose `include_in_briefing` is True only when the
    article is investment-relevant (either symbol-anchored, market-wide, or
    industry/policy framed) AND not dominated by society/crime/noise terms.
    """
    title_lower, full_text = _full_text(article)

    matched_terms: list[str] = []
    score = 0

    if _has_symbol_anchor(article):
        score += 30
        symbol = str(_field(article, "stock_symbol") or "")
        matched_terms.append(f"symbol:{symbol}")
        category = "kr_symbol"
    else:
        category = None

    broad_hits = [t for t in KR_BROAD_MARKET_TERMS if t.lower() in full_text]
    invest_hits = [t for t in KR_INVEST_KEYWORDS if t.lower() in full_text]
    crime_hits = [t for t in KR_CRIME_TERMS if t.lower() in full_text]
    society_hits = [t for t in KR_SOCIETY_TERMS if t.lower() in full_text]
    noise_hits = [t for t in KR_NOISE_TERMS if t.lower() in full_text]

    title_broad_hits = [t for t in KR_BROAD_MARKET_TERMS if t.lower() in title_lower]
    title_invest_hits = [t for t in KR_INVEST_KEYWORDS if t.lower() in title_lower]

    score += min(45, len(broad_hits) * 15)
    score += min(15, len(title_broad_hits) * 15)
    score += min(30, len(invest_hits) * 10)
    score += min(15, len(title_invest_hits) * 15)

    matched_terms.extend(broad_hits)
    matched_terms.extend(invest_hits)

    if broad_hits and not category:
        category = "kr_index" if any("코스" in t or "kospi" in t or "kosdaq" in t for t in broad_hits) else "kr_macro"
    if invest_hits and not category:
        category = "kr_industry"

    if crime_hits or society_hits or noise_hits:
        # Society/crime/sports/celebrity/weather override unless a strong
        # investment frame is also present.
        noise_strength = len(crime_hits) * 3 + len(society_hits) * 2 + len(noise_hits)
        invest_strength = (
            (30 if _has_symbol_anchor(article) else 0)
            + len(broad_hits) * 3
            + len(invest_hits) * 2
        )
        if noise_strength >= invest_strength:
            score = min(score, 10)
            matched_terms.extend(crime_hits + society_hits + noise_hits)
            primary_noise = "kr_crime" if crime_hits else "kr_society" if society_hits else "kr_noise"
            return KrNewsRelevance(
                score=score,
                bucket=_bucket(score),
                category=None,
                include_in_briefing=False,
                matched_terms=sorted(set(matched_terms)),
                noise_reason=primary_noise,
            )

    score = max(0, min(100, score))
    include = score >= _INCLUDE_THRESHOLD

    noise_reason: str | None = None
    if not include:
        if not (broad_hits or invest_hits or _has_symbol_anchor(article)):
            noise_reason = "kr_no_invest_signal"
        else:
            noise_reason = "low_kr_relevance"

    return KrNewsRelevance(
        score=score,
        bucket=_bucket(score),
        category=category if include else None,
        include_in_briefing=include,
        matched_terms=sorted(set(matched_terms)),
        noise_reason=noise_reason,
    )


def user_facing_kr_category(internal_category: str | None) -> str | None:
    """Map an internal scoring category to a user-facing category enum value."""
    if internal_category is None:
        return None
    return _INTERNAL_TO_USER_CATEGORY.get(internal_category, internal_category)


def _kr_big_cap_overlap(symbols: list[str]) -> set[str]:
    """Return the subset of provided symbols that are KR big-cap reference symbols.

    Currently unused by the scorer; reserved for future scope-based demotion if
    we extend KR scope classification analogous to ROB-155 US scope.
    """
    return {s for s in symbols if s in KR_BIG_CAP_GROUP_SYMBOLS}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_kr_news_relevance_service.py::test_empty_article_returns_low_relevance_with_no_matches -v
```

Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/kr_news_relevance_service.py tests/test_kr_news_relevance_service.py
git commit -m "feat(rob-169): KR news relevance scorer scaffold and empty-input contract

Pure-function scorer mirroring ROB-155 crypto shape. Returns KrNewsRelevance
with score, bucket, category, include_in_briefing, matched_terms, noise_reason.
Empty input → score=0, bucket=low, noise_reason=low_kr_relevance."
```

---

## Task 3: KR scorer — negative society/crime control

**Files:**
- Modify: `tests/test_kr_news_relevance_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_kr_news_relevance_service.py`:

```python
def test_society_crime_kr_article_no_symbol_is_dropped_with_kr_crime_reason():
    relevance = score_kr_news_article(
        {
            "title": "'광주 여고생 살해' 피의자 사이코패스 검사 결과 공개된다",
            "summary": "검찰은 살해 피의자에 대한 사이코패스 평가 결과를 곧 공개할 예정이다.",
            "feed_source": "browser_naver_mainnews",
            "keywords": ["사회", "범죄", "피의자"],
            "stock_symbol": None,
        }
    )

    assert relevance.include_in_briefing is False
    assert relevance.noise_reason == "kr_crime"
    assert relevance.score < 35
    assert any(t in relevance.matched_terms for t in ("살해", "피의자", "사이코패스"))


def test_celebrity_scandal_kr_article_is_dropped_as_kr_society():
    relevance = score_kr_news_article(
        {
            "title": "유명 아이돌 열애설 인정… 소속사 공식 입장",
            "summary": "스캔들로 번진 사생활 이슈에 팬들이 충격을 받았다.",
            "feed_source": "browser_naver_mainnews",
            "keywords": ["연예"],
            "stock_symbol": None,
        }
    )

    assert relevance.include_in_briefing is False
    assert relevance.noise_reason == "kr_society"


def test_traffic_accident_kr_article_with_no_invest_signal_is_dropped():
    relevance = score_kr_news_article(
        {
            "title": "고속도로 추돌 사고로 3중 추돌… 1명 사망",
            "summary": "경찰은 음주운전 가능성도 조사 중이다.",
            "feed_source": "browser_naver_mainnews",
            "keywords": ["사고"],
            "stock_symbol": None,
        }
    )

    assert relevance.include_in_briefing is False
    assert relevance.noise_reason in ("kr_society", "kr_crime", "kr_no_invest_signal")
```

- [ ] **Step 2: Run tests to verify they fail or pass appropriately**

```bash
uv run pytest tests/test_kr_news_relevance_service.py -v
```

Expected: All three new tests PASS already if the Task-2 implementation is correct (society/crime hits cap score and set noise_reason). If any FAIL, the scorer's noise-strength heuristic needs tuning — adjust the multipliers in `noise_strength` (currently `3*crime + 2*society + 1*noise`) or the threshold in `score = min(score, 10)` block.

- [ ] **Step 3: If all passed, no implementation change needed. If any failed, tune Task 2 implementation**

The negative-test design is deliberate: it pins down both the threshold AND the noise_reason naming. If a test fails, prefer adjusting the multipliers over changing test expectations.

- [ ] **Step 4: Re-run full kr scorer test file**

```bash
uv run pytest tests/test_kr_news_relevance_service.py -v
```

Expected: 4 passed (1 from Task 2 + 3 from Task 3).

- [ ] **Step 5: Commit**

```bash
git add tests/test_kr_news_relevance_service.py
git commit -m "test(rob-169): negative KR society/crime/accident controls for relevance scorer"
```

---

## Task 4: KR scorer — positive market-wide & no-symbol controls

**Files:**
- Modify: `tests/test_kr_news_relevance_service.py`
- Create: `tests/fixtures/kr_news_relevance/positive_market_wide.json`
- Create: `tests/fixtures/kr_news_relevance/negative_society_crime.json`
- Create: `tests/fixtures/kr_news_relevance/borderline.json`

- [ ] **Step 1: Write the fixture files**

`tests/fixtures/kr_news_relevance/positive_market_wide.json`:

```json
[
  {
    "id": "kospi_close",
    "title": "코스피, 외국인 매수에 2,800선 회복",
    "summary": "코스피가 외국인 순매수와 반도체 강세에 힘입어 2,800선을 회복했다.",
    "feed_source": "browser_naver_mainnews",
    "keywords": ["증시", "코스피"],
    "stock_symbol": null,
    "expected_include": true,
    "expected_category": "kr_index"
  },
  {
    "id": "kosdaq_ipo",
    "title": "올해 코스닥 IPO 시장 회복… 공모주 청약 경쟁률 1000대 1",
    "summary": "공모주 시장이 살아나며 신규 상장 종목에 청약이 몰렸다.",
    "feed_source": "browser_naver_mainnews",
    "keywords": ["코스닥", "IPO", "공모주"],
    "stock_symbol": null,
    "expected_include": true,
    "expected_category": "kr_index"
  },
  {
    "id": "boK_rate",
    "title": "한국은행 기준금리 동결… 환율 변동성 우려",
    "summary": "금융통화위원회는 기준금리를 3.50%로 유지했다.",
    "feed_source": "browser_naver_mainnews",
    "keywords": ["한은", "기준금리"],
    "stock_symbol": null,
    "expected_include": true,
    "expected_category": "kr_macro"
  },
  {
    "id": "fx_macro",
    "title": "원달러 환율 1,400원 돌파… 수출주 영향",
    "summary": "달러원 환율이 1,400원을 돌파해 수출 기업의 환율 부담이 확대됐다.",
    "feed_source": "browser_naver_mainnews",
    "keywords": ["환율", "수출"],
    "stock_symbol": null,
    "expected_include": true,
    "expected_category": "kr_macro"
  },
  {
    "id": "semis_industry",
    "title": "HBM·AI 반도체 수요 폭증… 메모리 가격 반등",
    "summary": "AI 반도체와 HBM 수요로 디램, 낸드 가격이 반등했다.",
    "feed_source": "browser_naver_mainnews",
    "keywords": ["반도체", "HBM", "AI 반도체"],
    "stock_symbol": null,
    "expected_include": true,
    "expected_category": "kr_industry"
  },
  {
    "id": "policy_tax",
    "title": "정부, 대주주 양도세 기준 상향… 증시 영향 주목",
    "summary": "법인세·양도세 개편으로 증시 수급 변화가 예상된다.",
    "feed_source": "browser_naver_mainnews",
    "keywords": ["세제개편", "증시"],
    "stock_symbol": null,
    "expected_include": true,
    "expected_category": "kr_macro"
  }
]
```

`tests/fixtures/kr_news_relevance/negative_society_crime.json`:

```json
[
  {
    "id": "gwangju_murder",
    "title": "'광주 여고생 살해' 피의자 사이코패스 검사 결과 공개된다",
    "summary": "검찰은 살해 피의자에 대한 사이코패스 평가 결과를 곧 공개할 예정이다.",
    "feed_source": "browser_naver_mainnews",
    "keywords": ["사회", "범죄"],
    "stock_symbol": null,
    "expected_include": false,
    "expected_noise_reason": "kr_crime"
  },
  {
    "id": "celebrity_scandal",
    "title": "유명 아이돌 열애설 인정… 소속사 공식 입장",
    "summary": "스캔들로 번진 사생활 이슈에 팬들이 충격.",
    "feed_source": "browser_naver_mainnews",
    "keywords": ["연예"],
    "stock_symbol": null,
    "expected_include": false,
    "expected_noise_reason": "kr_society"
  },
  {
    "id": "traffic_accident",
    "title": "고속도로 추돌 사고로 3중 추돌… 1명 사망",
    "summary": "경찰은 음주운전 가능성도 조사 중이다.",
    "feed_source": "browser_naver_mainnews",
    "keywords": ["사고"],
    "stock_symbol": null,
    "expected_include": false
  },
  {
    "id": "weather",
    "title": "전국에 한파 경보… 미세먼지 농도 '나쁨'",
    "summary": "기상청은 전국에 한파 경보를 내렸다.",
    "feed_source": "browser_naver_mainnews",
    "keywords": ["날씨"],
    "stock_symbol": null,
    "expected_include": false
  },
  {
    "id": "kbo_sports",
    "title": "프로야구 KBO 한국시리즈 7차전 명승부",
    "summary": "야구팬이 환호한 명승부였다.",
    "feed_source": "browser_naver_mainnews",
    "keywords": ["스포츠", "야구"],
    "stock_symbol": null,
    "expected_include": false
  }
]
```

`tests/fixtures/kr_news_relevance/borderline.json`:

```json
[
  {
    "id": "samsung_no_symbol",
    "title": "삼성전자 4분기 실적 개선 기대… 반도체 업황 회복",
    "summary": "메모리 가격 반등으로 4분기 영업이익이 시장 예상치를 웃돌 것이라는 전망이 나왔다.",
    "feed_source": "browser_naver_mainnews",
    "keywords": ["반도체"],
    "stock_symbol": null,
    "expected_include": true,
    "_note": "stock_symbol is null but the alias_dict will pick up Samsung; even without that, KR_INVEST_KEYWORDS hits make this market-wide-ish."
  },
  {
    "id": "policy_overlap_crime",
    "title": "금융위, 보이스피싱 피해 보호 강화… 핀테크 업계 대응",
    "summary": "금융위원회가 핀테크 사업자에 보이스피싱 대응 의무를 강화한다.",
    "feed_source": "browser_naver_mainnews",
    "keywords": ["금융위", "핀테크"],
    "stock_symbol": null,
    "expected_include": true,
    "_note": "Crime term ('보이스피싱') overlaps with policy/industry frame; should keep because invest_strength > noise_strength via 금융위·핀테크."
  }
]
```

- [ ] **Step 2: Write the failing tests using fixtures**

Append to `tests/test_kr_news_relevance_service.py`:

```python
import json
import pathlib

import pytest

_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "kr_news_relevance"


def _load_cases(name: str) -> list[dict]:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _load_cases("positive_market_wide.json"), ids=lambda c: c["id"])
def test_positive_market_wide_kr_articles_are_included(case):
    relevance = score_kr_news_article(case)

    assert relevance.include_in_briefing is True, (
        f"{case['id']!r} expected included; got noise_reason={relevance.noise_reason!r}, "
        f"score={relevance.score}, matched={relevance.matched_terms}"
    )
    if "expected_category" in case:
        assert relevance.category == case["expected_category"]


@pytest.mark.parametrize("case", _load_cases("negative_society_crime.json"), ids=lambda c: c["id"])
def test_negative_society_crime_kr_articles_are_excluded(case):
    relevance = score_kr_news_article(case)

    assert relevance.include_in_briefing is False, (
        f"{case['id']!r} expected excluded; got score={relevance.score}, matched={relevance.matched_terms}"
    )
    if "expected_noise_reason" in case:
        assert relevance.noise_reason == case["expected_noise_reason"]


@pytest.mark.parametrize("case", _load_cases("borderline.json"), ids=lambda c: c["id"])
def test_borderline_kr_articles_lean_to_expected_include(case):
    relevance = score_kr_news_article(case)
    assert relevance.include_in_briefing is case["expected_include"], (
        f"{case['id']!r} expected include={case['expected_include']}; "
        f"got include={relevance.include_in_briefing}, noise_reason={relevance.noise_reason!r}, "
        f"score={relevance.score}"
    )
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest tests/test_kr_news_relevance_service.py -v
```

Expected: All parameterized cases PASS. Borderline `policy_overlap_crime` requires `금융위` to be in `KR_INVEST_KEYWORDS` (it is) AND `보이스피싱`/`피싱` not to dominate — verify the noise_strength formula. If a fixture case fails, tune the term lists in Task 1 file (`KR_INVEST_KEYWORDS`, `KR_CRIME_TERMS`) or the multipliers in `score_kr_news_article`. Do not relax the test expectations.

- [ ] **Step 4: Commit**

```bash
git add tests/test_kr_news_relevance_service.py tests/fixtures/kr_news_relevance/
git commit -m "test(rob-169): positive market-wide and borderline KR relevance fixtures

Pin down the contract for KOSPI/IPO/금리/환율/반도체/정책 KR articles without
stock_symbol staying in the feed, while society/crime/사고/날씨/스포츠 are
suppressed, including a policy×crime overlap (보이스피싱) borderline case."
```

---

## Task 5: Wire KR scorer into `build_feed_news`

**Files:**
- Modify: `app/services/invest_view_model/feed_news_service.py:23-40` (imports)
- Modify: `app/services/invest_view_model/feed_news_service.py:399-454` (per-article loop)
- Modify: `app/services/invest_view_model/feed_news_service.py:456-461` (post-loop crypto filter; extend with KR)

- [ ] **Step 1: Write the failing test**

Create `tests/test_feed_news_kr_filter.py`:

```python
"""ROB-169 — KR investment relevance integration tests for build_feed_news."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.news_issues import (
    IssueSignals,
    MarketIssue,
    MarketIssueArticle,
)
from app.services.invest_view_model.relation_resolver import RelationResolver

_NOW = datetime(2026, 5, 10, tzinfo=UTC)


def _kr_article(*, id: int, title: str, summary: str = "", keywords: list[str] | None = None,
                symbol: str | None = None, name: str | None = None) -> MagicMock:
    a = MagicMock()
    a.id = id
    a.market = "kr"
    a.title = title
    a.source = "Naver"
    a.feed_source = "browser_naver_mainnews"
    a.article_published_at = _NOW
    a.stock_symbol = symbol
    a.stock_name = name
    a.summary = summary
    a.keywords = keywords or []
    a.url = f"https://example.com/kr/{id}"
    return a


def _empty_related_result() -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    return result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_society_crime_article_dropped_on_kr_tab(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _kr_article(
            id=301,
            title="'광주 여고생 살해' 피의자 사이코패스 검사 결과 공개된다",
            summary="검찰은 사이코패스 평가 결과를 공개할 예정이다.",
            keywords=["사회"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result, _empty_related_result()])
    monkeypatch.setattr(svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[])))

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="kr", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_market_wide_kospi_article_kept_with_no_symbol(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _kr_article(
            id=302,
            title="코스피, 외국인 매수에 2,800선 회복",
            summary="코스피가 외국인 순매수와 반도체 강세에 힘입어 2,800선을 회복했다.",
            keywords=["증시", "코스피"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result, _empty_related_result()])
    monkeypatch.setattr(svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[])))

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="kr", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == [302]
    item = resp.items[0]
    assert item.relatedSymbols == []
    assert item.category == "kr_index"
    assert item.noiseReason is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_society_article_suppresses_issue_chip(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    article = _kr_article(
        id=303,
        title="유명 아이돌 열애설 인정… 소속사 공식 입장",
        summary="스캔들로 번진 사생활 이슈에 팬들이 충격.",
        keywords=["연예"],
    )
    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [article]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result, _empty_related_result()])

    issue = MarketIssue(
        id="iss-noise",
        market="kr",
        rank=1,
        issue_title="연예 이슈",
        subtitle=None,
        direction="neutral",
        source_count=1,
        article_count=1,
        updated_at=_NOW,
        articles=[
            MarketIssueArticle(
                id=303,
                title=article.title,
                url=article.url,
                source="Naver",
                feed_source="browser_naver_mainnews",
                published_at=_NOW,
            )
        ],
        signals=IssueSignals(
            recency_score=0.5, source_diversity_score=0.5, mention_score=0.5
        ),
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[issue]))
    )

    # On the "top" tab the row is NOT dropped (only the kr tab applies the
    # filter), but the issueId must be suppressed because the row is noise.
    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="top", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == [303]
    assert resp.items[0].issueId is None
    assert resp.items[0].noiseReason == "kr_society"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_top_tab_does_not_drop_kr_society_rows(monkeypatch) -> None:
    """The kr-tab filter only fires on tab=='kr'; other tabs keep the row but
    still flag noiseReason so the frontend can choose to render or hide it.
    """
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _kr_article(
            id=304,
            title="유명 아이돌 열애설 인정… 소속사 공식 입장",
            summary="연예 가십.",
            keywords=["연예"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result, _empty_related_result()])
    monkeypatch.setattr(svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[])))

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="top", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == [304]
    assert resp.items[0].noiseReason == "kr_society"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_feed_news_kr_filter.py -v
```

Expected: FAIL — `noiseReason` is None and items are not dropped (KR scorer not yet wired in).

- [ ] **Step 3: Wire KR scorer into `build_feed_news`**

Edit `app/services/invest_view_model/feed_news_service.py`.

(a) Update imports near line 23:

```python
from app.services.crypto_news_relevance_service import (
    score_crypto_news_article,
    user_facing_category,
)
from app.services.kr_news_relevance_service import (
    score_kr_news_article,
    user_facing_kr_category,
)
```

(b) Inside the `for row in rows:` loop, immediately after the existing crypto block (around line 433), add the KR branch. Replace lines 420-434 with:

```python
        # ROB-155: apply crypto relevance scoring for crypto articles.
        item_category: str | None = None
        item_noise_reason: str | None = None
        if market_value == "crypto":
            relevance = score_crypto_news_article(row)
            item_category = user_facing_category(relevance.category)
            item_noise_reason = relevance.noise_reason
            # Demote relatedSymbols for low-relevance crypto articles.
            if not relevance.include_in_briefing:
                item_category = item_category or "low_relevance"
                if "crypto_low_relevance" not in scope_tags:
                    scope_tags.append("crypto_low_relevance")
                if related:
                    related = []

        # ROB-169: apply KR investment-relevance scoring for KR articles.
        if market_value == "kr":
            kr_relevance = score_kr_news_article(row)
            kr_user_category = user_facing_kr_category(kr_relevance.category)
            if kr_relevance.include_in_briefing:
                # Keep symbol-anchored category if alias_dict already filled
                # related; otherwise advertise the kr category for the row.
                if kr_user_category and not item_category:
                    item_category = kr_user_category
            else:
                item_noise_reason = kr_relevance.noise_reason
                if "kr_low_relevance" not in scope_tags:
                    scope_tags.append("kr_low_relevance")
```

(c) Suppress `issueId` when KR noise is set. Replace the `FeedNewsItem(...)` construction (around line 437) with:

```python
        relation = _relation_from_related_symbols(related)
        suppress_issue = bool(item_noise_reason) and market_value == "kr"
        items.append(
            FeedNewsItem(
                id=row.id,
                title=row.title,
                publisher=row.source,
                feedSource=row.feed_source,
                publishedAt=row.article_published_at,
                market=market_typed,
                relatedSymbols=related,
                issueId=None if suppress_issue else issue_id_for_article.get(row.id),
                summarySnippet=_summary_snippet_for_row(row, analysis_summary),
                relation=relation,
                url=row.url,
                scope=cast(NewsScope, item_scope),
                tags=scope_tags,
                category=item_category,
                noiseReason=item_noise_reason,
            )
        )
```

(d) Extend the post-loop tab filter (around line 459-460) so the `kr` tab drops noise-flagged rows:

```python
    # ROB-155 / ROB-169: drop very-low-relevance rows on tab-scoped feeds. We
    # never drop on broader tabs (top/latest/holdings/watchlist) — frontends
    # can choose to render with reduced styling using `noiseReason`.
    if tab == "crypto":
        items = [i for i in items if not (i.noiseReason and not i.relatedSymbols)]
    elif tab == "kr":
        items = [
            i
            for i in items
            if not (
                i.noiseReason
                and i.noiseReason.startswith("kr_")
                and not i.relatedSymbols
            )
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_feed_news_kr_filter.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Run regression on existing feed news tests**

```bash
uv run pytest tests/test_invest_feed_news_router.py tests/test_feed_news_scope.py tests/test_feed_news_crypto_filter.py tests/test_kr_news_relevance_service.py -v
```

Expected: All previously-passing tests still PASS. If a US/crypto test fails, the KR branch wrongly fired for non-KR articles — verify the `if market_value == "kr":` guard.

- [ ] **Step 6: Commit**

```bash
git add app/services/invest_view_model/feed_news_service.py tests/test_feed_news_kr_filter.py
git commit -m "feat(rob-169): wire KR investment-relevance gate into /invest/api/feed/news

KR rows that match society/crime/연예/스포츠/사고/날씨 with no investment frame
get noiseReason=kr_crime|kr_society|kr_no_invest_signal and tags+=kr_low_relevance.
Drop such rows on tab=kr; keep on broader tabs but suppress issueId so the chip
does not surface noise. Positive market-wide rows (KOSPI/IPO/금리/환율/반도체/
정책) without stock_symbol stay with category=kr_index|kr_macro|kr_industry."
```

---

## Task 6: Schema scope literal extension

**Files:**
- Modify: `app/schemas/invest_feed_news.py:18`
- Modify: `tests/test_feed_news_scope.py` (add positive coverage)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_feed_news_scope.py`:

```python
def test_feed_news_item_accepts_kr_market_wide_scope():
    item = FeedNewsItem(
        id=42,
        title="코스피 회복",
        market="kr",
        url="https://example.com/kr/42",
        scope="kr_market_wide",
    )
    assert item.scope == "kr_market_wide"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_feed_news_scope.py::test_feed_news_item_accepts_kr_market_wide_scope -v
```

Expected: FAIL — `pydantic.ValidationError` because `"kr_market_wide"` is not in the `NewsScope` literal.

- [ ] **Step 3: Extend the literal**

Edit `app/schemas/invest_feed_news.py:18`:

```python
# ROB-155: article scope — market_wide means broad macro/index/sector article;
# symbol_specific means article thesis anchors on one or more specific symbols;
# mixed means both a broad frame and a clearly anchored specific symbol.
# ROB-169: kr_market_wide is the KR analogue of market_wide for KOSPI/KOSDAQ/
# 금리/환율/반도체/정책 articles that lack a stock_symbol but are investment-
# relevant.
NewsScope = Literal["market_wide", "symbol_specific", "mixed", "kr_market_wide"]
```

- [ ] **Step 4: Run test**

```bash
uv run pytest tests/test_feed_news_scope.py -v
```

Expected: All PASS (5 + 1 new).

- [ ] **Step 5: Optionally set scope=kr_market_wide for KR no-symbol positive rows**

In `app/services/invest_view_model/feed_news_service.py` Task-5 `if market_value == "kr":` branch, after `if kr_relevance.include_in_briefing:`, also set:

```python
                if kr_relevance.include_in_briefing and not related:
                    item_scope = cast(NewsScope, "kr_market_wide")
```

(Note: `item_scope` is the loop-local var that comes back from `_related_symbols_for_article`. It must be reassigned BEFORE the `FeedNewsItem(...)` constructor — verify by reading `feed_news_service.py:406-453`.)

Add a regression test in `tests/test_feed_news_kr_filter.py`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_no_symbol_market_wide_row_advertises_kr_market_wide_scope(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _kr_article(
            id=305,
            title="원달러 환율 1,400원 돌파… 수출주 영향",
            summary="달러원 환율이 1,400원을 돌파했다.",
            keywords=["환율"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result, _empty_related_result()])
    monkeypatch.setattr(svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[])))

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="kr", limit=30, cursor=None
    )

    assert resp.items[0].scope == "kr_market_wide"
    assert resp.items[0].relatedSymbols == []
```

- [ ] **Step 6: Run all related tests**

```bash
uv run pytest tests/test_feed_news_scope.py tests/test_feed_news_kr_filter.py tests/test_invest_feed_news_router.py -v
```

Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add app/schemas/invest_feed_news.py app/services/invest_view_model/feed_news_service.py tests/test_feed_news_scope.py tests/test_feed_news_kr_filter.py
git commit -m "feat(rob-169): kr_market_wide scope literal for no-symbol KR investment rows

Additive scope value, mirrored from ROB-155 market_wide. Set on KR rows that
pass the relevance gate but have no relatedSymbols (KOSPI/IPO/금리/환율/etc.).
Default remains symbol_specific so existing clients are unaffected."
```

---

## Task 7: Frontend type widening (no render change)

**Files:**
- Modify: `frontend/invest/src/types/feedNews.ts:24-36`
- Test: `frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx` (only run, no new test required — additive optional fields cannot break existing tests)

- [ ] **Step 1: Run the existing frontend tests as the baseline**

```bash
cd frontend/invest && npm install && npm test -- --run
```

Expected: PASS (baseline). If anything fails on the un-modified branch, STOP and report — that is a pre-existing issue, not a ROB-169 regression.

- [ ] **Step 2: Write the failing TypeScript expectation**

Edit `frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx` if it has type assertions; else add a tiny smoke type-check at the top of the test file:

```typescript
import type { FeedNewsItem } from "../types/feedNews";

const _smoke: FeedNewsItem = {
  id: 1,
  title: "x",
  market: "kr",
  url: "https://example.com",
  relation: "none",
  relatedSymbols: [],
  scope: "kr_market_wide",
  tags: ["kr_low_relevance"],
  category: "kr_index",
  noiseReason: "kr_society",
};
void _smoke;
```

- [ ] **Step 3: Run TypeScript check to verify it fails**

```bash
cd frontend/invest && npm run type-check
```

(or `npx tsc --noEmit` if the script is named differently — check `frontend/invest/package.json`)

Expected: FAIL — `Object literal may only specify known properties, and 'scope' does not exist in type 'FeedNewsItem'.`.

- [ ] **Step 4: Extend the FeedNewsItem type**

Edit `frontend/invest/src/types/feedNews.ts:24-36`:

```typescript
export type FeedNewsScope = "market_wide" | "symbol_specific" | "mixed" | "kr_market_wide";

export interface FeedNewsItem {
  id: number;
  title: string;
  publisher?: string | null;
  feedSource?: string | null;
  publishedAt?: string | null;
  market: "kr" | "us" | "crypto";
  relatedSymbols: FeedRelatedSymbol[];
  issueId?: string | null;
  summarySnippet?: string | null;
  relation: RelationKind;
  url: string;
  // ROB-155 / ROB-169 — additive read-layer classification.
  scope?: FeedNewsScope;
  tags?: string[];
  category?: string | null;
  noiseReason?: string | null;
}
```

- [ ] **Step 5: Run frontend type check + tests**

```bash
cd frontend/invest && npm run type-check && npm test -- --run
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/invest/src/types/feedNews.ts frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx
git commit -m "feat(rob-169): widen FeedNewsItem TS type with scope/tags/category/noiseReason

Additive optional fields. NewsListItem render is intentionally unchanged in
this PR; future work can fade noiseReason rows or label kr_market_wide chips."
```

---

## Task 8: Runbook + KR no-symbol regression test in router file

**Files:**
- Create: `docs/runbooks/kr-news-relevance.md`
- Modify: `tests/test_invest_feed_news_router.py` (append one regression test)

- [ ] **Step 1: Write the runbook**

`docs/runbooks/kr-news-relevance.md`:

```markdown
# KR News Investment Relevance Gate (ROB-169)

## What this does

`/invest/api/feed/news` applies a deterministic KR investment-relevance scorer
to every `market="kr"` row. The scorer is read-layer only: ingestion is
unchanged.

## Components

- Scorer: `app/services/kr_news_relevance_service.py::score_kr_news_article`
- Term lists: `app/services/news_entity_alias_data.py`
  - `KR_BROAD_MARKET_TERMS` — KOSPI/KOSDAQ/금리/환율/원자재/CPI/GDP/IPO 등
  - `KR_INVEST_KEYWORDS` — 반도체/배터리/조선/방산/원전/바이오/금융위 등
  - `KR_CRIME_TERMS` — 살해/피의자/사이코패스/마약/사기/보이스피싱 등
  - `KR_SOCIETY_TERMS` — 연예/아이돌/스포츠/사고/날씨/여고생 등
  - `KR_NOISE_TERMS` — 한파/미세먼지/맛집/운세/로또 등
- Wiring: `app/services/invest_view_model/feed_news_service.py::build_feed_news`
- Schema fields: `app/schemas/invest_feed_news.py::FeedNewsItem.{scope,tags,category,noiseReason}`

## How to extend term lists

1. Add the new term to the appropriate constant in `news_entity_alias_data.py`.
2. Add a fixture row in `tests/fixtures/kr_news_relevance/` covering it.
3. Run `uv run pytest tests/test_kr_news_relevance_service.py -v`.

## How to verify in production

Read-only smoke (no auth changes, no mutation):

```bash
# Replace COOKIE with the operator session cookie.
curl -s -b "session=$COOKIE" "https://prod.host/invest/api/feed/news?tab=kr&limit=50" | \
  jq '.items[] | {id, title, market, noiseReason, category, scope, hasIssue: (.issueId!=null), hasSymbols: (.relatedSymbols|length>0)}'
```

Expected:
- `tab=kr` response has zero rows with `noiseReason: "kr_crime"` or `"kr_society"`.
- KOSPI/IPO/금리/환율/반도체/정책 rows present even when `relatedSymbols` is empty.
- `noiseReason` set on demoted rows (visible on broader tabs like `top`/`latest`),
  with `issueId: null` for those rows.

## Rollback

This feature is a pure-function, additive read-layer change with no DB
migration. Revert the wiring commit (`git revert <hash-of-task-5>`) to disable.
The schema additions in `app/schemas/invest_feed_news.py` (Task 6) and the
frontend type widening (Task 7) are safely additive and may be left in place
during a partial revert.

## Known limitations

- Heuristic term lists; tune them via fixtures, not threshold changes.
- KR scope-based symbol demotion (analogous to ROB-155 US scope) is intentionally
  out of scope here. `KR_BIG_CAP_GROUP_SYMBOLS` is reserved for that future ROB.
- tvscreener KR rows are scored the same way; if their richer metadata should
  bypass the gate, that should be its own ticket (suggested follow-up).
```

- [ ] **Step 2: Append a regression test in the router file**

Append to `tests/test_invest_feed_news_router.py`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_kr_society_crime_dropped_on_kr_tab(monkeypatch) -> None:
    """ROB-169 regression: known-bad production row must not appear on tab=kr."""
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=999,
            market="kr",
            symbol=None,
            title="'광주 여고생 살해' 피의자 사이코패스 검사 결과 공개된다",
            summary="검찰은 사이코패스 평가 결과를 공개할 예정이다.",
            keywords=["사회"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="kr", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == []
```

- [ ] **Step 3: Run all news-related tests**

```bash
uv run pytest tests/test_invest_feed_news_router.py tests/test_feed_news_kr_filter.py tests/test_kr_news_relevance_service.py tests/test_feed_news_scope.py tests/test_feed_news_crypto_filter.py tests/test_news_entity_matcher.py tests/test_news_entity_alias_data_kr.py -v
```

Expected: All PASS.

- [ ] **Step 4: Run the full unit-test suite**

```bash
make test-unit
```

Expected: PASS (no regressions).

- [ ] **Step 5: Commit**

```bash
git add docs/runbooks/kr-news-relevance.md tests/test_invest_feed_news_router.py
git commit -m "docs(rob-169): runbook for KR news relevance gate + canonical regression test

Pin down the production class — the '광주 여고생 살해' row — as a regression
test against tab=kr so a future term-list change can never silently re-include
it."
```

---

## Task 9: Lint, typecheck, and full verification

- [ ] **Step 1: Lint and format**

```bash
make lint
make format
```

Expected: PASS. If `make format` modifies files, re-stage and amend the relevant Task commit OR create a fixup commit.

- [ ] **Step 2: Typecheck**

```bash
make typecheck
```

Expected: PASS.

- [ ] **Step 3: Full test suite**

```bash
make test
```

Expected: PASS.

- [ ] **Step 4: Frontend test + typecheck**

```bash
cd frontend/invest && npm run type-check && npm test -- --run
```

Expected: PASS.

- [ ] **Step 5: Push branch and open draft PR**

```bash
git push -u origin feature/ROB-169-kr-news-relevance
gh pr create --draft --title "ROB-169: KR news relevance & ranking quality gate" --body "$(cat <<'EOF'
## Summary
- Adds deterministic KR investment-relevance scorer at `app/services/kr_news_relevance_service.py`.
- Wires it into `/invest/api/feed/news` so KR rows lacking a stock_symbol AND lacking a market-wide investment frame (society/crime/연예/스포츠/사고/날씨) are flagged with `noiseReason` and dropped on `tab=kr`.
- Preserves market-wide KR signals (KOSPI/IPO/금리/환율/반도체/정책) without `stock_symbol` via `category=kr_index|kr_macro|kr_industry` and new `scope=kr_market_wide`.
- Suppresses `issueId` on KR noise rows so the issue chip does not surface noise.
- Read-layer only — no DB migrations, no ingestion changes, no broker/order/watch mutation.

## Test plan
- [ ] `uv run pytest tests/test_kr_news_relevance_service.py tests/test_feed_news_kr_filter.py tests/test_invest_feed_news_router.py tests/test_feed_news_scope.py tests/test_feed_news_crypto_filter.py tests/test_news_entity_alias_data_kr.py -v`
- [ ] `make lint && make typecheck && make test`
- [ ] `cd frontend/invest && npm run type-check && npm test -- --run`
- [ ] Production smoke per `docs/runbooks/kr-news-relevance.md`: confirm no `noiseReason: kr_crime|kr_society` on `tab=kr`, and KOSPI/IPO/금리 rows without symbols stay.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

(Do NOT mark the PR ready-for-review until production smoke per runbook passes.)

- [ ] **Step 6: Production smoke (post-merge or pre-merge against staging)**

Authenticated read-only call (no mutation):

```bash
curl -s -b "session=$OPERATOR_COOKIE" "https://staging.host/invest/api/feed/news?tab=kr&limit=100" | \
  jq '[.items[] | select(.noiseReason != null) | {id,title,noiseReason}]'
# Expected: empty array (because tab=kr drops them).

curl -s -b "session=$OPERATOR_COOKIE" "https://staging.host/invest/api/feed/news?tab=top&limit=100" | \
  jq '[.items[] | select(.market=="kr" and .noiseReason != null) | {id,title,noiseReason,issueId}]'
# Expected: any society/crime row carries noiseReason and issueId=null.

curl -s -b "session=$OPERATOR_COOKIE" "https://staging.host/invest/api/feed/news?tab=kr&limit=100" | \
  jq '[.items[] | select((.relatedSymbols|length)==0) | {id,title,scope,category,noiseReason}]'
# Expected: KOSPI/IPO/금리/환율/반도체/정책 rows; scope=kr_market_wide; noiseReason=null.
```

Document smoke output in the PR thread before flipping to ready-for-review.

---

## Self-Review Checklist

**Spec coverage:**

- [x] KR investment relevance classifier/gate — Tasks 1, 2, 3, 4 (scorer + term lists + fixtures).
- [x] latest/top ranking behavior — Task 5 (broader tabs keep rows with noiseReason; tab=kr drops them; ranking remains the existing `article_published_at DESC, id DESC` keyset; no in-place reordering needed because the existing ranking is acceptable once noise is suppressed).
- [x] issue-chip suppression for non-investment rows — Task 5 step 3 (`suppress_issue` branch sets `issueId=None` for KR noise rows on every tab).
- [x] positive no-symbol market-wide controls — Tasks 4 (positive_market_wide.json) + 6 (`kr_market_wide` scope).
- [x] negative society/crime controls — Tasks 3, 4 (negative_society_crime.json) + 8 (canonical regression).
- [x] minimal safe frontend changes — Task 7 (type widening only; no render change).
- [x] verification commands and production smoke — Task 9 (`make lint/typecheck/test`, frontend type-check + test, runbook smoke).
- [x] do NOT implement code beyond small read-only inspection — N/A: this plan is the deliverable; the planner does not run Tasks 1-9.

**Placeholder scan:** No "TBD"/"TODO"/"add appropriate"/"similar to Task N"/"fill in" — every step has either exact code, exact command + expected output, or explicit "no change required" reasoning.

**Type consistency:**
- `KrNewsRelevance` — defined in Task 2, referenced in Tasks 3, 4, 5.
- `score_kr_news_article` / `user_facing_kr_category` — defined in Task 2, imported in Task 5.
- `KR_BROAD_MARKET_TERMS`, `KR_INVEST_KEYWORDS`, `KR_CRIME_TERMS`, `KR_SOCIETY_TERMS`, `KR_NOISE_TERMS`, `KR_BIG_CAP_GROUP_SYMBOLS` — defined in Task 1, used in Tasks 2, 3, 4, 5, 8.
- `NewsScope` literal extended in Task 6 with `"kr_market_wide"`; the frontend `FeedNewsScope` (Task 7) carries the same value set.
- `noiseReason` strings: `"kr_crime"`, `"kr_society"`, `"kr_no_invest_signal"`, `"low_kr_relevance"` — used consistently across scorer (Task 2), tests (Tasks 3, 4, 5, 8), runbook (Task 8). The `kr_low_relevance` tag is used in scope_tags (Task 5).

**Risk / blockers (deliverable section):**
- KR society/celebrity term overlap with rare KR business stories (e.g., 연예 사업, 스포츠 마케팅) — borderline fixture pins down the policy×crime overlap (`보이스피싱` vs `금융위`); future term-list refinements should add fixtures, not relax tests.
- tvscreener KR rows (`feed_source` prefix `http_tvscreener_news_kr`) flow through the same scorer. The fixture set deliberately uses `browser_naver_mainnews`; if QA finds tvscreener-KR rows being demoted incorrectly, add a fixture under `tests/fixtures/kr_news_relevance/tvscreener_kr.json` and tune.
- The plan does NOT touch ingestion, persistence, or scheduler/Prefect cadence (per the Kanban safety boundaries). Production smoke is read-only.

---

## Execution Handoff

Plan complete. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks; fast iteration if the term lists need tuning.
2. **Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batched with checkpoints.

Choose at the K2 implementer ticket; this planner ticket ends with the plan written and committed.
