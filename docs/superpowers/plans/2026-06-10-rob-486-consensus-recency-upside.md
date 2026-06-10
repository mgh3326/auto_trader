# ROB-486 컨센서스 목표가 Recency 윈도우 + Upside-aware Recommendation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** KR 투자의견 컨센서스(avg/median/min/max 목표가, upside_pct, buy/hold/sell 카운트)를 행별 날짜 기반 recency 윈도우(기본 12개월)에서만 집계하고(stale/undated 행은 fail-closed 제외 + 메타데이터 보고), 컨센서스 평균 목표가가 현재가를 크게 밑돌면(upside ≤ -10%) count 기반 buy 추천을 차단/강등한다.

**Architecture:** 집계 코어는 `app/services/analyst_normalizer.py::build_consensus`를 date-aware로 바꿔 두 호출지(Naver 수집 경로 `investor.py` + 웹 패널 `stock_detail_research_consensus_service.py`)가 동일 필터를 타게 한다. 추천 스코어러는 `app/mcp_server/tooling/shared.py::build_recommendation_for_equity`에 upside 강등을 넣고, services→mcp_server import 금지 규칙에 따라 `app/services/symbol_analysis/derived.py::_score_action`에 동일 로직을 포트(RULE_VERSION 범프)한다. presence 플로어(`analysis_analyze.py`)는 truthy dict가 아니라 윈도우 생존 row 수 기준으로 강화한다.

**Tech Stack:** Python 3.13 + uv, pytest(`uv run pytest`), ruff/ty(`make lint`), 순수 함수 변경만 — DB/alembic migration 0, 브로커/주문 mutation 0.

---

## Verified Root Cause (2026-06-10 조사)

두 갈래 인과 사슬, 모두 코드+라이브 프로브로 확인됨 (ROB-485 통합 조사 verdict, confidence: high).

**(A) 목표가 오염 — 031330/005880 케이스:**
- KR `get_investment_opinions`는 **KIS TR이 아니라 Naver 리서치 스크랩**이다:
  `app/mcp_server/tooling/fundamentals/_valuation.py:96-97` (kr → `_fetch_investment_opinions_naver`) →
  `app/services/naver_finance/investor.py:301-329` (`finance.naver.com/research/company_list.naver`).
  레포 전체에서 `FHKST663300`/`HHKDB`/`ksdinfo`/`액면` 검색 0건. **이슈 본문의 "KIS TR(hts_goal_prc/stck_bsop_date)" 전제는 틀렸음(정정).**
- 각 의견 행에는 이미 ISO date가 있다: `investor.py:90` (`_parse_naver_date(cells[4]...)`), `investor.py:136` (opinion dict에 전파), `parser.py:33-77` ('YYYY-MM-DD'). **필터에 필요한 데이터는 이미 존재하며 단지 미사용.**
- `build_consensus` (`app/services/analyst_normalizer.py:140-144`)는 날짜 조건 없이 모든 `target_price`를 평균(avg `:161`, median `:162-169`, upside `:173-176`). 유일한 컷오프는 행 수 limit(`investor.py:98-99`, 기본 10/cap 30).
- 라이브 실측 (2026-06-10, 두 조사자 독립 재현, 수치 byte-identical):
  - **031330**: 의견 전체 2건 — `2019-12-27 한국기업데이터 Hold tp=None`, `2015-08-24 대신증권 Buy tp=2700` → avg 2,700 vs 현재가 ~15,360-15,580, upside ≈ **-82%**. KIS 무수정주가 시계열 검증으로 **기업행위 아님 — 순수 staleness** (2026-05 실제 ~2.2x 랠리).
  - **005880**: 첫 10행 중 8개 목표가 `[3000(2026-05-18 신한 Buy), 3600(2023), 4000(2022), 23000(2020), 31000×3(2018-2019), 35000(2018)]` → 합 161,600/8 = **avg 20,200**, median (23000+31000)/2 = **27,000**, 현재가 1,914 대비 upside **+964%**. KIS 무수정주가로 **10:1 액면분할 확증** (2020-10-08 18,100 → 10-12 1,725). 12개월 윈도우 시뮬레이션: avg=median=**3,000**, upside **+56.7%** (정상화).
- 윈도우 시뮬레이션: 3/6/12개월 모두 두 종목을 정상화; 12개월이 thin-coverage KR에서 안전한 기본값 (005880은 36개월에도 3행만 생존).

**(B) buy 모순 — 475150 케이스 (날짜 필터로 해결 안 됨):**
- `build_recommendation_for_equity` (`app/mcp_server/tooling/shared.py:554-601`)는 buy/sell **카운트 비율만** 스코어: buy_ratio>0.6 → +2 (`:573-574`), score≥2 → action='buy' (`:599-601`). **upside_pct/avg_target_price는 함수 어디서도 읽지 않음** (`:504-684` 전수 확인).
- 475150 실측: 8 buy vs 0 sell (전부 2025-04~2026-05 최근), avg target 32,625 vs 현재가 44,350 → upside **-26.44%**인데 action='buy' + "Analyst consensus bullish (8 buy vs 0 sell)".
- 동일 count-only 스코어가 `app/services/symbol_analysis/derived.py:25-59` `_score_action`에 의도적으로 복제돼 있음 (docstring `:7-8`, services→mcp_server import 금지). `ConsensusData.upside_pct`는 contract에 이미 존재 (`contract.py:61`)하나 미사용.
- presence 플로어는 존재만 검사: `analysis_analyze.py:430` `consensus_present = bool(...get("consensus"))` — **stale-only여도 truthy dict면 통과** (`floor.py:13-36`).

**조사로 정정된 이슈 본문 전제:**
1. KIS TR 소스 전제 → **틀림**, 실제는 Naver 리서치 스크랩 (위 참조).
2. "기업행위(액면) 보정/DART corporate-action 경로" 제안 → **레포 내 데이터 소스 없음** (market_events DART normalizer는 earnings/disclosure만 분류 `normalizers.py:107-128`; OpenDartReader 0.2.3 event()에 주식분할/병합 키 없음; KIS corporate-action TR 미래핑) → **명시적 Non-goal**.
3. 031330의 2,700이 기업행위 잔재라는 추정 → **틀림**, 순수 staleness.
4. (구현 함정) 웹 패널은 `_normalize_opinion`이 **date를 DROP**한 뒤 build_consensus를 재실행 (`stock_detail_research_consensus_service.py:259-273`) — investor.py에만 필터를 넣으면 패널은 오염 유지, build_consensus에만 넣으면 패널은 전 행 undated로 침묵 — **양쪽을 함께 고쳐야 함**.

## Non-goals (스코프 제외)

- 기업행위(액면분할/병합) 목표가 환산 및 DART corporate-action 수집 — 레포 내 데이터 소스 부재 (위 정정 참조).
- KR 소스를 KIS 종목투자의견 TR로 교체 — 래퍼 부재, Naver 행에 이미 날짜 존재. 후속 후보일 뿐.
- US(yfinance) 컨센서스 집계 변경 — 벤더(`analyst_price_targets`) 컨센서스를 그대로 사용, `opinion_window_months` 미적용 (도구 설명에 명시).
- DB migration — 코드-only, migration 0.

---

## Task 1: `build_consensus` date-aware 코어 + 양쪽 호출지(도구/웹 패널) 커버

**Files:**
- Modify: `app/services/analyst_normalizer.py` (`build_consensus` :108-178, 모듈 import :9)
- Modify: `app/services/invest_view_model/stock_detail_research_consensus_service.py` (`_normalize_opinion` :259-273)
- Test: `tests/test_analyst_normalizer.py` (TestBuildConsensus 전면 갱신 + 신규 TestBuildConsensusRecencyWindow)
- Test: `tests/test_stock_detail_research_consensus_service.py` (기존 날짜 fixture 상대화 + 신규 2 테스트)

### Steps

- [ ] **1.1 Write the failing tests (build_consensus 윈도우)** — `tests/test_analyst_normalizer.py` 상단 import를 다음으로 교체:

  현재 코드(발췌, :1-12):
  ```python
  """Unit tests for analyst rating normalizer."""

  from __future__ import annotations

  import pytest

  from app.services.analyst_normalizer import (
      build_consensus,
      is_strong_buy,
      normalize_rating_label,
      rating_to_bucket,
  )
  ```

  변경 후:
  ```python
  """Unit tests for analyst rating normalizer."""

  from __future__ import annotations

  from datetime import UTC, datetime, timedelta
  from typing import Any

  import pytest

  from app.services.analyst_normalizer import (
      build_consensus,
      is_strong_buy,
      normalize_rating_label,
      rating_to_bucket,
  )

  # ROB-486: 모든 build_consensus 테스트는 now 를 주입해 시한폭탄을 차단한다.
  _NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


  def _days_ago(days: int) -> str:
      return (_NOW.date() - timedelta(days=days)).isoformat()


  def _dated(op: dict[str, Any], days: int = 30) -> dict[str, Any]:
      return {**op, "date": _days_ago(days)}
  ```

  파일 끝에 신규 클래스 추가:
  ```python
  class TestBuildConsensusRecencyWindow:
      """ROB-486: 12개월 recency 윈도우 + fail-closed 메타데이터."""

      def test_stale_only_yields_null_targets_and_zero_counts(self) -> None:
          """031330 실측 모양 — 2015/2019 행만 존재 → 무필터 폴백 없이 전부 None/0."""
          opinions = [
              {"rating": "Hold", "target_price": None, "date": "2019-12-27"},
              {"rating": "Buy", "target_price": 2700, "date": "2015-08-24"},
          ]
          consensus = build_consensus(opinions, 15360, now=_NOW)

          assert consensus["avg_target_price"] is None
          assert consensus["median_target_price"] is None
          assert consensus["min_target_price"] is None
          assert consensus["max_target_price"] is None
          assert consensus["upside_pct"] is None
          assert consensus["buy_count"] == 0
          assert consensus["hold_count"] == 0
          assert consensus["sell_count"] == 0
          assert consensus["strong_buy_count"] == 0
          assert consensus["total_count"] == 0
          assert consensus["rows_total"] == 2
          assert consensus["rows_used"] == 0
          assert consensus["rows_excluded_stale"] == 2
          assert consensus["rows_excluded_undated"] == 0
          assert consensus["newest_opinion_date"] == "2019-12-27"
          assert consensus["window_months"] == 12

      def test_mixed_stale_and_recent_aggregates_recent_only(self) -> None:
          """005880 실측 모양 — 12개월 내 행(3,000 Buy + tp 없는 Hold)만 집계."""
          opinions = [
              {"rating": "Buy", "target_price": 3000, "date": "2026-05-18"},
              {"rating": "Hold", "target_price": None, "date": "2025-12-05"},
              {"rating": "Buy", "target_price": 3600, "date": "2023-08-11"},
              {"rating": "Buy", "target_price": 4000, "date": "2022-05-20"},
              {"rating": "Buy", "target_price": 23000, "date": "2020-06-12"},
              {"rating": "Buy", "target_price": 31000, "date": "2019-11-14"},
              {"rating": "Buy", "target_price": 31000, "date": "2019-08-14"},
              {"rating": "Buy", "target_price": 31000, "date": "2019-05-15"},
              {"rating": "Buy", "target_price": 35000, "date": "2018-10-29"},
          ]
          consensus = build_consensus(opinions, 1914, now=_NOW)

          # 무필터였다면 avg 20,200 / median 27,000 (버그 리포트 실측치)였을 입력.
          assert consensus["avg_target_price"] == 3000
          assert consensus["median_target_price"] == 3000
          assert consensus["upside_pct"] == pytest.approx(56.74, abs=0.01)
          assert consensus["buy_count"] == 1
          assert consensus["hold_count"] == 1
          assert consensus["total_count"] == 2
          assert consensus["rows_total"] == 9
          assert consensus["rows_used"] == 2
          assert consensus["rows_excluded_stale"] == 7
          assert consensus["rows_excluded_undated"] == 0
          assert consensus["newest_opinion_date"] == "2026-05-18"

      def test_undated_rows_excluded_and_counted(self) -> None:
          """date 부재/None/파싱불가 행은 windowed 집계에서 제외 + 메타데이터 카운트."""
          opinions = [
              {"rating": "Buy", "target_price": 100, "date": _days_ago(10)},
              {"rating": "Buy", "target_price": 999},  # date 키 자체 없음
              {"rating": "Buy", "target_price": 888, "date": None},
              {"rating": "Buy", "target_price": 777, "date": "not-a-date"},
          ]
          consensus = build_consensus(opinions, 90, now=_NOW)

          assert consensus["avg_target_price"] == 100
          assert consensus["buy_count"] == 1
          assert consensus["total_count"] == 1
          assert consensus["rows_total"] == 4
          assert consensus["rows_used"] == 1
          assert consensus["rows_excluded_undated"] == 3
          assert consensus["rows_excluded_stale"] == 0

      def test_window_boundary_inclusive(self) -> None:
          """cutoff 당일(now 기준 정확히 window_months 개월 전)은 생존, 하루 전은 stale."""
          opinions = [
              {"rating": "Buy", "target_price": 100, "date": "2025-06-10"},
              {"rating": "Buy", "target_price": 200, "date": "2025-06-09"},
          ]
          consensus = build_consensus(opinions, 90, now=_NOW)

          assert consensus["rows_used"] == 1
          assert consensus["avg_target_price"] == 100
          assert consensus["rows_excluded_stale"] == 1

      def test_window_months_override(self) -> None:
          opinions = [
              {"rating": "Buy", "target_price": 100, "date": "2025-06-09"},
          ]
          wide = build_consensus(opinions, 90, window_months=24, now=_NOW)
          narrow = build_consensus(opinions, 90, window_months=12, now=_NOW)

          assert wide["rows_used"] == 1
          assert wide["avg_target_price"] == 100
          assert wide["window_months"] == 24
          assert narrow["rows_used"] == 0
          assert narrow["avg_target_price"] is None

      def test_now_injection_controls_anchor(self) -> None:
          """동일 입력이라도 now 가 미래면 stale 로 떨어진다 (시한폭탄 방지 근거)."""
          opinions = [{"rating": "Buy", "target_price": 100, "date": "2026-05-18"}]

          current = build_consensus(opinions, 90, now=_NOW)
          future = build_consensus(opinions, 90, now=datetime(2027, 7, 1, tzinfo=UTC))

          assert current["rows_used"] == 1
          assert future["rows_used"] == 0
          assert future["avg_target_price"] is None

      def test_empty_opinions_metadata(self) -> None:
          consensus = build_consensus([], 100, now=_NOW)

          assert consensus["rows_total"] == 0
          assert consensus["rows_used"] == 0
          assert consensus["rows_excluded_stale"] == 0
          assert consensus["rows_excluded_undated"] == 0
          assert consensus["newest_opinion_date"] is None
          assert consensus["window_months"] == 12
  ```

- [ ] **1.2 Run tests to verify they fail**:
  ```bash
  cd /Users/mgh3326/work/auto_trader.rob-486
  uv run pytest tests/test_analyst_normalizer.py::TestBuildConsensusRecencyWindow -v
  ```
  기대: 전 테스트 ERROR/FAIL — `TypeError: build_consensus() got an unexpected keyword argument 'now'`.

- [ ] **1.3 Write the implementation** — `app/services/analyst_normalizer.py`:

  import 변경 — 현재 코드(:9):
  ```python
  from typing import Any, Literal
  ```
  변경 후:
  ```python
  import calendar
  from datetime import UTC, date, datetime
  from typing import Any, Literal
  ```

  `build_consensus`(현재 :108-178) 직전에 헬퍼 2개 추가, 함수 본문은 아래로 전면 교체:
  ```python
  def _months_before(anchor: date, months: int) -> date:
      """anchor 에서 months 개월 전 날짜 (말일 클램프, 외부 의존성 없음)."""
      total = anchor.year * 12 + (anchor.month - 1) - months
      year, month0 = divmod(total, 12)
      month = month0 + 1
      day = min(anchor.day, calendar.monthrange(year, month)[1])
      return date(year, month, day)


  def _parse_opinion_date(value: Any) -> date | None:
      """행별 ISO date(YYYY-MM-DD[…]) 파싱. 부재/파싱불가 → None (fail-closed)."""
      if not isinstance(value, str):
          return None
      raw = value.strip()[:10]
      if not raw:
          return None
      try:
          return date.fromisoformat(raw)
      except ValueError:
          return None


  def build_consensus(
      opinions: list[dict[str, Any]],
      current_price: int | float | None,
      *,
      window_months: int = 12,
      now: datetime | None = None,
  ) -> dict[str, Any]:
      """Build consensus statistics from analyst opinions within a recency window.

      ROB-486: 목표가 통계와 buy/hold/sell 카운트는 **window_months 이내 date 가
      있는 행(생존 집합)에서만** 집계한다. date 가 없거나 파싱 불가한 행은
      제외하고 메타데이터로만 카운트한다 (fail-closed — 조용한 혼입 금지).
      생존 행이 0이면 목표가 통계와 upside_pct 는 전부 None 이며 무필터 평균으로
      폴백하지 않는다.

      Args:
          opinions: List of individual opinions with rating_bucket, target_price,
              and per-row ISO ``date`` (YYYY-MM-DD)
          current_price: Current stock price
          window_months: Recency window in months (default 12)
          now: 집계 기준 시각 (테스트 주입용; 기본 현재 UTC)

      Returns:
          Dictionary with consensus statistics including:
          - buy_count, hold_count, sell_count, strong_buy_count: windowed counts
          - total_count: 생존(windowed) 행 수 (== rows_used)
          - avg_target_price, median_target_price, min_target_price, max_target_price
          - upside_pct: Upside percentage from current price (windowed avg 기준)
          - current_price: Current stock price
          - rows_total / rows_used / rows_excluded_stale / rows_excluded_undated /
            newest_opinion_date / window_months: 윈도우 메타데이터
      """
      anchor = (now or datetime.now(UTC)).date()
      cutoff = _months_before(anchor, window_months)

      surviving: list[dict[str, Any]] = []
      rows_excluded_stale = 0
      rows_excluded_undated = 0
      newest_opinion_date: date | None = None

      for op in opinions:
          parsed = _parse_opinion_date(op.get("date"))
          if parsed is None:
              rows_excluded_undated += 1
              continue
          if newest_opinion_date is None or parsed > newest_opinion_date:
              newest_opinion_date = parsed
          if parsed < cutoff:
              rows_excluded_stale += 1
              continue
          surviving.append(op)

      rating_counts: dict[str, int] = {"buy": 0, "hold": 0, "sell": 0}
      strong_buy_count = 0

      for op in surviving:
          rating_label = op.get("rating", op.get("rating_label", ""))
          normalized_label = normalize_rating_label(rating_label)
          rating_bucket = op.get("rating_bucket") or rating_to_bucket(normalized_label)

          if rating_bucket in rating_counts:
              rating_counts[rating_bucket] += 1

          if is_strong_buy(normalized_label):
              strong_buy_count += 1

      target_prices = [
          op["target_price"]
          for op in surviving
          if isinstance(op.get("target_price"), (int, float)) and op["target_price"] > 0
      ]

      consensus: dict[str, Any] = {
          "buy_count": rating_counts["buy"],
          "hold_count": rating_counts["hold"],
          "sell_count": rating_counts["sell"],
          "strong_buy_count": strong_buy_count,
          "total_count": len(surviving),
          "avg_target_price": None,
          "median_target_price": None,
          "min_target_price": None,
          "max_target_price": None,
          "upside_pct": None,
          "current_price": current_price,
          "rows_total": len(opinions),
          "rows_used": len(surviving),
          "rows_excluded_stale": rows_excluded_stale,
          "rows_excluded_undated": rows_excluded_undated,
          "newest_opinion_date": (
              newest_opinion_date.isoformat() if newest_opinion_date else None
          ),
          "window_months": window_months,
      }

      if target_prices:
          consensus["avg_target_price"] = int(sum(target_prices) / len(target_prices))
          sorted_prices = sorted(target_prices)
          n = len(sorted_prices)
          if n % 2 == 0:
              consensus["median_target_price"] = int(
                  (sorted_prices[n // 2 - 1] + sorted_prices[n // 2]) / 2
              )
          else:
              consensus["median_target_price"] = int(sorted_prices[n // 2])
          consensus["min_target_price"] = int(min(target_prices))
          consensus["max_target_price"] = int(max(target_prices))

          if current_price and isinstance(current_price, (int, float)):
              consensus["upside_pct"] = round(
                  (consensus["avg_target_price"] - current_price) / current_price * 100,
                  2,
              )

      return consensus
  ```

- [ ] **1.4 Run new tests to verify they pass**:
  ```bash
  uv run pytest tests/test_analyst_normalizer.py::TestBuildConsensusRecencyWindow -v
  ```
  기대: 7 passed. (이 시점에 기존 `TestBuildConsensus`는 fixture 무날짜로 RED — 다음 스텝에서 수정.)

- [ ] **1.5 기존 TestBuildConsensus fixture를 dated/now-주입으로 갱신** — `tests/test_analyst_normalizer.py`의 `class TestBuildConsensus` 전체(현재 :134-275)를 아래로 교체:
  ```python
  class TestBuildConsensus:
      """Tests for build_consensus (ROB-486: 모든 fixture 는 윈도우 내 date + now 주입)."""

      def test_buy_only_consensus(self) -> None:
          opinions = [
              _dated({"rating": "Buy", "target_price": 100}),
              _dated({"rating": "Strong Buy", "target_price": 110}),
              _dated({"rating": "Buy", "target_price": 95}),
          ]
          consensus = build_consensus(opinions, 90, now=_NOW)

          assert consensus["buy_count"] == 3
          assert consensus["hold_count"] == 0
          assert consensus["sell_count"] == 0
          assert consensus["strong_buy_count"] == 1
          assert consensus["total_count"] == 3
          assert consensus["avg_target_price"] == 101
          assert consensus["current_price"] == 90
          assert consensus["upside_pct"] == pytest.approx(12.22)
          assert consensus["rows_total"] == 3
          assert consensus["rows_used"] == 3

      def test_mixed_consensus(self) -> None:
          opinions = [
              _dated({"rating": "Buy", "target_price": 100}),
              _dated({"rating": "Hold", "target_price": 95}),
              _dated({"rating": "Sell", "target_price": 90}),
          ]
          consensus = build_consensus(opinions, 92, now=_NOW)

          assert consensus["buy_count"] == 1
          assert consensus["hold_count"] == 1
          assert consensus["sell_count"] == 1
          assert consensus["strong_buy_count"] == 0
          assert consensus["total_count"] == 3
          assert consensus["avg_target_price"] == 95
          assert consensus["median_target_price"] == 95
          assert consensus["min_target_price"] == 90
          assert consensus["max_target_price"] == 100

      def test_empty_opinions(self) -> None:
          consensus = build_consensus([], 100, now=_NOW)

          assert consensus["buy_count"] == 0
          assert consensus["hold_count"] == 0
          assert consensus["sell_count"] == 0
          assert consensus["strong_buy_count"] == 0
          assert consensus["total_count"] == 0
          assert consensus["avg_target_price"] is None
          assert consensus["median_target_price"] is None
          assert consensus["upside_pct"] is None

      def test_with_rating_bucket(self) -> None:
          opinions = [
              _dated({"rating_bucket": "buy", "target_price": 100}),
              _dated({"rating_bucket": "buy", "target_price": 110}),
              _dated({"rating_bucket": "hold", "target_price": None}),
          ]
          consensus = build_consensus(opinions, 95, now=_NOW)

          assert consensus["buy_count"] == 2
          assert consensus["hold_count"] == 1
          assert consensus["strong_buy_count"] == 0

      def test_target_price_statistics(self) -> None:
          opinions = [
              _dated({"rating": "Buy", "target_price": 90}),
              _dated({"rating": "Buy", "target_price": 100}),
              _dated({"rating": "Buy", "target_price": 110}),
              _dated({"rating": "Buy", "target_price": 120}),
          ]
          consensus = build_consensus(opinions, 95, now=_NOW)

          assert consensus["avg_target_price"] == 105
          assert consensus["median_target_price"] == 105
          assert consensus["min_target_price"] == 90
          assert consensus["max_target_price"] == 120

      def test_median_calculation_even(self) -> None:
          opinions = [
              _dated({"rating": "Buy", "target_price": 100}),
              _dated({"rating": "Buy", "target_price": 200}),
          ]
          consensus = build_consensus(opinions, 150, now=_NOW)

          assert consensus["median_target_price"] == 150

      def test_median_calculation_odd(self) -> None:
          opinions = [
              _dated({"rating": "Buy", "target_price": 100}),
              _dated({"rating": "Buy", "target_price": 200}),
              _dated({"rating": "Buy", "target_price": 300}),
          ]
          consensus = build_consensus(opinions, 150, now=_NOW)

          assert consensus["median_target_price"] == 200

      def test_upside_percentage_calculation(self) -> None:
          opinions = [_dated({"rating": "Buy", "target_price": 110})]
          consensus = build_consensus(opinions, 100, now=_NOW)

          assert consensus["upside_pct"] == pytest.approx(10.0)

      def test_upside_percentage_none_without_current_price(self) -> None:
          opinions = [_dated({"rating": "Buy", "target_price": 100})]
          consensus = build_consensus(opinions, None, now=_NOW)

          assert consensus["upside_pct"] is None

      def test_ignores_invalid_target_prices(self) -> None:
          opinions = [
              _dated({"rating": "Buy", "target_price": 100}),
              _dated({"rating": "Buy", "target_price": -50}),  # Invalid: negative
              _dated({"rating": "Buy", "target_price": None}),  # Invalid: None
              _dated({"rating": "Buy", "target_price": "abc"}),  # Invalid: string
          ]
          consensus = build_consensus(opinions, 90, now=_NOW)

          assert consensus["avg_target_price"] == 100
          assert consensus["min_target_price"] == 100
          assert consensus["max_target_price"] == 100

      def test_rating_label_bucket_fallback(self) -> None:
          """rating_bucket 미제공 시 rating_label 사용."""
          opinions = [
              _dated({"rating_label": "Strong Buy", "target_price": 100}),
              _dated({"rating_label": "Buy", "target_price": 95}),
          ]
          consensus = build_consensus(opinions, 90, now=_NOW)

          assert consensus["buy_count"] == 2
          assert consensus["strong_buy_count"] == 1

      def test_korean_ratings_in_consensus(self) -> None:
          opinions = [
              _dated({"rating": "매수", "target_price": 100}),
              _dated({"rating": "강력매수", "target_price": 110}),
              _dated({"rating": "중립", "target_price": 95}),
          ]
          consensus = build_consensus(opinions, 90, now=_NOW)

          assert consensus["buy_count"] == 2
          assert consensus["hold_count"] == 1
          assert consensus["strong_buy_count"] == 1
  ```
  ```bash
  uv run pytest tests/test_analyst_normalizer.py -v
  ```
  기대: 전체 passed.

- [ ] **1.6 Write the failing tests (웹 패널 date 보존)** — `tests/test_stock_detail_research_consensus_service.py`:

  (a) 기존 결합 테스트(:54-145)의 하드코딩 날짜 시한폭탄 제거 — 현재 코드(발췌, :61-75):
  ```python
              "opinions": [
                  {
                      "firm": "A증권",
                      "rating": "매수",
                      "target_price": 84000,
                      "date": "2026-05-13",
                  },
                  {
                      "firm": "B증권",
                      "rating": "중립",
                      "target_price": 72000,
                      "date": "2026-05-12",
                  },
              ],
  ```
  변경 후 (테스트 함수 내 `now = datetime.now(UTC)` 가 이미 :55에 있음):
  ```python
              "opinions": [
                  {
                      "firm": "A증권",
                      "rating": "매수",
                      "target_price": 84000,
                      "date": (now - timedelta(days=20)).date().isoformat(),
                  },
                  {
                      "firm": "B증권",
                      "rating": "중립",
                      "target_price": 72000,
                      "date": (now - timedelta(days=21)).date().isoformat(),
                  },
              ],
  ```

  (b) 파일 끝에 신규 테스트 2개 추가:
  ```python
  @pytest.mark.asyncio
  async def test_stock_detail_consensus_applies_recency_window_like_tool():
      """ROB-486: 패널이 행별 date 를 보존해 도구와 동일한 윈도우 집계를 탄다 (005880 모양)."""
      now = datetime.now(UTC)
      recent = (now - timedelta(days=23)).date().isoformat()
      stale = (now - timedelta(days=2050)).date().isoformat()

      async def opinions_provider(symbol, market, limit):
          return {
              "source": "naver",
              "current_price": 1914,
              "opinions": [
                  {
                      "firm": "신한투자증권",
                      "rating": "매수",
                      "target_price": 3000,
                      "date": recent,
                  },
                  {
                      "firm": "하나증권",
                      "rating": "매수",
                      "target_price": 23000,
                      "date": stale,
                  },
              ],
          }

      async def citations_provider(db, symbol, limit):
          return []

      async def readiness_provider(db, source, max_age_hours):
          return ResearchReportsReadinessResponse(
              source=source,
              is_ready=True,
              is_stale=False,
              latest_inserted_count=0,
              latest_skipped_count=0,
              latest_report_count=0,
              warnings=[],
              max_age_hours=max_age_hours,
          )

      response = await build_stock_detail_research_consensus(
          market="kr",
          symbol="005880",
          db=SimpleNamespace(),
          providers=StockDetailResearchConsensusProviders(
              resolver=_resolve_kr,
              opinions=opinions_provider,
              citations=citations_provider,
              readiness=readiness_provider,
          ),
      )

      assert response.consensus is not None
      assert response.consensus.totalCount == 1
      assert response.consensus.buyCount == 1
      assert response.consensus.avgTargetPrice == 3000
      assert response.consensus.upsidePct == pytest.approx(56.74, abs=0.01)


  @pytest.mark.asyncio
  async def test_stock_detail_consensus_stale_only_reports_missing():
      """ROB-486 (031330 모양): 윈도우 생존 row 0 → 패널 consensus 미노출 (폴백 금지)."""

      async def opinions_provider(symbol, market, limit):
          return {
              "source": "naver",
              "current_price": 15360,
              "opinions": [
                  {
                      "firm": "한국기업데이터",
                      "rating": "중립",
                      "target_price": None,
                      "date": "2019-12-27",
                  },
                  {
                      "firm": "대신증권",
                      "rating": "매수",
                      "target_price": 2700,
                      "date": "2015-08-24",
                  },
              ],
          }

      async def citations_provider(db, symbol, limit):
          return []

      async def readiness_provider(db, source, max_age_hours):
          return ResearchReportsReadinessResponse(
              source=source,
              is_ready=False,
              is_stale=False,
              latest_inserted_count=0,
              latest_skipped_count=0,
              latest_report_count=0,
              warnings=[],
              max_age_hours=max_age_hours,
          )

      response = await build_stock_detail_research_consensus(
          market="kr",
          symbol="031330",
          db=SimpleNamespace(),
          providers=StockDetailResearchConsensusProviders(
              resolver=_resolve_kr,
              opinions=opinions_provider,
              citations=citations_provider,
              readiness=readiness_provider,
          ),
      )

      assert response.consensus is None
      assert response.state == "missing"
      assert response.emptyReason == "no_analyst_consensus_or_research_reports"
  ```

- [ ] **1.7 Run panel tests to verify they fail**:
  ```bash
  uv run pytest tests/test_stock_detail_research_consensus_service.py -v
  ```
  기대: `test_stock_detail_consensus_applies_recency_window_like_tool` FAIL (`response.consensus is None` — `_normalize_opinion`이 date를 드롭해 전 행 undated 제외), 기존 결합 테스트도 동일 사유로 FAIL.

- [ ] **1.8 Write the implementation (date 보존)** — `app/services/invest_view_model/stock_detail_research_consensus_service.py`:

  현재 코드(:259-273):
  ```python
  def _normalize_opinion(row: Any) -> dict[str, Any]:
      if not isinstance(row, dict):
          return {"rating": None, "target_price": None}
      return {
          "rating": row.get("rating")
          or row.get("opinion")
          or row.get("investment_opinion")
          or row.get("recommendation"),
          "target_price": _to_float(
              row.get("target_price")
              or row.get("targetPrice")
              or row.get("target")
              or row.get("tp")
          ),
      }
  ```
  변경 후:
  ```python
  def _normalize_opinion(row: Any) -> dict[str, Any]:
      if not isinstance(row, dict):
          return {"rating": None, "target_price": None, "date": None}
      return {
          "rating": row.get("rating")
          or row.get("opinion")
          or row.get("investment_opinion")
          or row.get("recommendation"),
          "target_price": _to_float(
              row.get("target_price")
              or row.get("targetPrice")
              or row.get("target")
              or row.get("tp")
          ),
          # ROB-486: build_consensus 의 recency 윈도우가 패널에서도 동작하도록
          # 행별 date 를 보존한다 (드롭하면 모든 행이 undated 로 제외됨).
          "date": row.get("date"),
      }
  ```

- [ ] **1.9 Run tests to verify they pass**:
  ```bash
  uv run pytest tests/test_stock_detail_research_consensus_service.py tests/test_analyst_normalizer.py -v
  ```
  기대: 전체 passed.

- [ ] **1.10 Format + commit**:
  ```bash
  uv run ruff format app/services/analyst_normalizer.py app/services/invest_view_model/stock_detail_research_consensus_service.py tests/test_analyst_normalizer.py tests/test_stock_detail_research_consensus_service.py
  uv run ruff check app/services/analyst_normalizer.py app/services/invest_view_model/stock_detail_research_consensus_service.py tests/test_analyst_normalizer.py tests/test_stock_detail_research_consensus_service.py
  git add app/services/analyst_normalizer.py app/services/invest_view_model/stock_detail_research_consensus_service.py tests/test_analyst_normalizer.py tests/test_stock_detail_research_consensus_service.py
  git commit -m "$(cat <<'EOF'
  fix(ROB-486): build_consensus를 date-aware로 — 12개월 recency 윈도우 + fail-closed 메타데이터

  목표가 통계와 buy/hold/sell 카운트를 동일 생존 row 집합(윈도우 내 dated rows)
  에서만 집계. undated/stale row는 제외 + rows_excluded_* 메타데이터.
  생존 0이면 전부 None/0 (무필터 폴백 금지). 웹 패널 _normalize_opinion이
  date를 보존해 stock-detail 패널도 동일 필터를 탄다.

  Co-Authored-By: Paperclip <noreply@paperclip.ing>
  EOF
  )"
  ```

---

## Task 2: Naver 수집 경로 `window_months` 스레딩 + fixture 시한폭탄 제거

**Files:**
- Modify: `app/services/naver_finance/investor.py` (`_build_investment_opinions_from_company_list_soup` :106-148, `fetch_investment_opinions` :301-329)
- Modify: `app/mcp_server/tooling/fundamentals_sources_naver.py` (`_fetch_investment_opinions_naver` :102-108)
- Test: `tests/test_naver_finance.py` (fixture 상대 날짜화 :311-371, assertion :762, 신규 윈도우 테스트)

### Steps

- [ ] **2.1 Write the failing tests** — `tests/test_naver_finance.py`:

  (a) import 변경 — 현재 코드(:5):
  ```python
  from datetime import date
  ```
  변경 후:
  ```python
  from datetime import date, timedelta
  ```

  (b) `SAMPLE_INVESTMENT_OPINIONS_HTML`(:311) 직전에 헬퍼 추가:
  ```python
  # ROB-486: 리스트 fixture 날짜를 상대값으로 생성해 recency 윈도우 시한폭탄을 막는다.
  _OPINION_LIST_DATE_RECENT_1 = date.today() - timedelta(days=30)
  _OPINION_LIST_DATE_RECENT_2 = date.today() - timedelta(days=45)
  _OPINION_LIST_DATE_STALE = date.today() - timedelta(days=400)


  def _naver_list_date(d: date) -> str:
      """Naver 리스트 페이지의 2자리 연도 형식 (예: '26.01.15')."""
      return d.strftime("%y.%m.%d")
  ```

  (c) `SAMPLE_INVESTMENT_OPINIONS_HTML`(:311-336)을 f-string으로 전환하고 날짜 셀 교체 (HTML에 중괄호 리터럴 없음 — f-string 안전):
  ```python
  SAMPLE_INVESTMENT_OPINIONS_HTML = f"""
  <html>
  <body>
  <table class="type_1">
      <tbody>
          <tr>
              <td><a href="/item/main.naver?code=005930">삼성전자</a></td>
              <td><a href="company_read.naver?nid=12345&page=1">반도체 업황 개선 전망</a></td>
              <td>삼성증권</td>
              <td><a href="https://example.com/report1.pdf"></a></td>
              <td class="date">{_naver_list_date(_OPINION_LIST_DATE_RECENT_1)}</td>
              <td>1234</td>
          </tr>
          <tr>
              <td><a href="/item/main.naver?code=005930">삼성전자</a></td>
              <td><a href="company_read.naver?nid=12346&page=1">실적 호조 지속</a></td>
              <td>미래에셋</td>
              <td><a href="https://example.com/report2.pdf"></a></td>
              <td class="date">{_naver_list_date(_OPINION_LIST_DATE_RECENT_2)}</td>
              <td>5678</td>
          </tr>
      </tbody>
  </table>
  </body>
  </html>
  """
  ```

  (d) `SAMPLE_INVESTMENT_OPINIONS_DUPLICATE_HTML`(:338-371)을 아래 전체로 교체 (f-string 전환 + 날짜 셀만 변경, 그 외 마크업 동일):
  ```python
  SAMPLE_INVESTMENT_OPINIONS_DUPLICATE_HTML = f"""
  <html>
  <body>
  <table class="type_1">
      <tbody>
          <tr>
              <td><a href="/item/main.naver?code=005930">삼성전자</a></td>
              <td><a href="company_read.naver?nid=12345&page=1">반도체 업황 개선 전망</a></td>
              <td>삼성증권</td>
              <td><a href="https://example.com/report1.pdf"></a></td>
              <td class="date">{_naver_list_date(_OPINION_LIST_DATE_RECENT_1)}</td>
              <td>1234</td>
          </tr>
          <tr>
              <td><a href="/item/main.naver?code=005930">삼성전자</a></td>
              <td><a href="company_read.naver?nid=12345&page=9">반도체 업황 개선 전망</a></td>
              <td>삼성증권</td>
              <td><a href="https://example.com/report1.pdf"></a></td>
              <td class="date">{_naver_list_date(_OPINION_LIST_DATE_RECENT_1)}</td>
              <td>9999</td>
          </tr>
          <tr>
              <td><a href="/item/main.naver?code=005930">삼성전자</a></td>
              <td><a href="company_read.naver?nid=12346&page=1">실적 호조 지속</a></td>
              <td>미래에셋</td>
              <td><a href="https://example.com/report2.pdf"></a></td>
              <td class="date">{_naver_list_date(_OPINION_LIST_DATE_RECENT_2)}</td>
              <td>5678</td>
          </tr>
      </tbody>
  </table>
  </body>
  </html>
  """
  ```

  (e) `TestFetchInvestmentOpinions::test_success`의 날짜 assertion — 현재 코드(:762):
  ```python
          assert op1["date"] == "2026-01-15"
  ```
  변경 후:
  ```python
          assert op1["date"] == _OPINION_LIST_DATE_RECENT_1.isoformat()
  ```

  (f) `SAMPLE_CURRENT_PRICE_HTML`(:427-439) 뒤에 신규 fixture 추가:
  ```python
  # ROB-486: 12개월 윈도우 테스트용 — 최근 1행 + 400일 전 1행 (005880 모양).
  SAMPLE_INVESTMENT_OPINIONS_MIXED_STALE_HTML = f"""
  <html>
  <body>
  <table class="type_1">
      <tbody>
          <tr>
              <td><a href="/item/main.naver?code=005880">대한해운</a></td>
              <td><a href="company_read.naver?nid=22345&page=1">실적 전망</a></td>
              <td>신한투자증권</td>
              <td><a href="https://example.com/r1.pdf"></a></td>
              <td class="date">{_naver_list_date(_OPINION_LIST_DATE_RECENT_1)}</td>
              <td>1234</td>
          </tr>
          <tr>
              <td><a href="/item/main.naver?code=005880">대한해운</a></td>
              <td><a href="company_read.naver?nid=22346&page=1">구 리포트</a></td>
              <td>하나증권</td>
              <td><a href="https://example.com/r2.pdf"></a></td>
              <td class="date">{_naver_list_date(_OPINION_LIST_DATE_STALE)}</td>
              <td>5678</td>
          </tr>
      </tbody>
  </table>
  </body>
  </html>
  """

  SAMPLE_DETAIL_HTML_TARGET_3000 = """
  <html><body>
  <div class="view_info_1">
      목표가 <em class="money"><strong>3,000</strong></em>
      <span class="division">|</span>
      투자의견 <em class="coment">매수</em>
  </div>
  </body></html>
  """

  SAMPLE_DETAIL_HTML_TARGET_23000 = """
  <html><body>
  <div class="view_info_1">
      목표가 <em class="money"><strong>23,000</strong></em>
      <span class="division">|</span>
      투자의견 <em class="coment">매수</em>
  </div>
  </body></html>
  """

  SAMPLE_CURRENT_PRICE_HTML_005880 = """
  <html><body>
  <p class="no_today">
      <span class="blind">현재가</span>
      <em><span class="blind">1,914</span></em>
  </p>
  </body></html>
  """
  ```

  (g) `TestFetchInvestmentOpinions` 클래스 끝(현재 `test_deduplicates_duplicate_nids_before_detail_fetch` 뒤, :896 부근)에 신규 테스트 2개 추가:
  ```python
      async def test_recency_window_excludes_stale_targets(
          self, monkeypatch: pytest.MonkeyPatch
      ) -> None:
          """ROB-486 (005880 모양): 12개월 밖 목표가는 집계 제외 + 메타데이터 보고."""

          async def mock_fetch_html(
              url: str, params: dict[str, Any] | None = None
          ) -> BeautifulSoup:
              if "company_list.naver" in url:
                  return BeautifulSoup(
                      SAMPLE_INVESTMENT_OPINIONS_MIXED_STALE_HTML, "lxml"
                  )
              elif "company_read.naver" in url:
                  nid = (params or {}).get("nid", "")
                  if nid == "22345":
                      return BeautifulSoup(SAMPLE_DETAIL_HTML_TARGET_3000, "lxml")
                  if nid == "22346":
                      return BeautifulSoup(SAMPLE_DETAIL_HTML_TARGET_23000, "lxml")
              elif "main.naver" in url:
                  return BeautifulSoup(SAMPLE_CURRENT_PRICE_HTML_005880, "lxml")
              return BeautifulSoup("<html></html>", "lxml")

          monkeypatch.setattr(naver_finance.investor, "_fetch_html", mock_fetch_html)

          result = await naver_finance.fetch_investment_opinions("005880", limit=10)

          # opinions 리스트에는 stale 행도 참고용으로 그대로 남는다.
          assert result["count"] == 2
          consensus = result["consensus"]
          assert consensus["avg_target_price"] == 3000
          assert consensus["median_target_price"] == 3000
          assert consensus["upside_pct"] == pytest.approx(56.74, abs=0.01)
          assert consensus["buy_count"] == 1
          assert consensus["total_count"] == 1
          assert consensus["rows_total"] == 2
          assert consensus["rows_used"] == 1
          assert consensus["rows_excluded_stale"] == 1
          assert consensus["rows_excluded_undated"] == 0
          assert consensus["window_months"] == 12
          assert (
              consensus["newest_opinion_date"]
              == _OPINION_LIST_DATE_RECENT_1.isoformat()
          )

      async def test_window_months_param_threads_to_consensus(
          self, monkeypatch: pytest.MonkeyPatch
      ) -> None:
          """ROB-486: window_months 파라미터가 build_consensus 까지 전달된다."""

          async def mock_fetch_html(
              url: str, params: dict[str, Any] | None = None
          ) -> BeautifulSoup:
              if "company_list.naver" in url:
                  return BeautifulSoup(
                      SAMPLE_INVESTMENT_OPINIONS_MIXED_STALE_HTML, "lxml"
                  )
              elif "company_read.naver" in url:
                  nid = (params or {}).get("nid", "")
                  if nid == "22345":
                      return BeautifulSoup(SAMPLE_DETAIL_HTML_TARGET_3000, "lxml")
                  if nid == "22346":
                      return BeautifulSoup(SAMPLE_DETAIL_HTML_TARGET_23000, "lxml")
              elif "main.naver" in url:
                  return BeautifulSoup(SAMPLE_CURRENT_PRICE_HTML_005880, "lxml")
              return BeautifulSoup("<html></html>", "lxml")

          monkeypatch.setattr(naver_finance.investor, "_fetch_html", mock_fetch_html)

          result = await naver_finance.fetch_investment_opinions(
              "005880", limit=10, window_months=24
          )

          consensus = result["consensus"]
          assert consensus["window_months"] == 24
          # 400일 전 행도 24개월 윈도우에는 생존 → (3000+23000)/2.
          assert consensus["rows_used"] == 2
          assert consensus["avg_target_price"] == 13000
  ```

- [ ] **2.2 Run tests to verify they fail**:
  ```bash
  uv run pytest tests/test_naver_finance.py -k "TestFetchInvestmentOpinions" -v
  ```
  기대: `test_window_months_param_threads_to_consensus` FAIL — `TypeError: fetch_investment_opinions() got an unexpected keyword argument 'window_months'`. (`test_recency_window_excludes_stale_targets`는 Task 1의 build_consensus 기본 12개월이 이미 동작하므로 PASS일 수 있음 — 정상.)

- [ ] **2.3 Write the implementation** — `app/services/naver_finance/investor.py`:

  (a) 현재 코드(:106-113, 시그니처):
  ```python
  async def _build_investment_opinions_from_company_list_soup(
      code: str,
      company_list_soup: BeautifulSoup,
      limit: int,
      *,
      current_price: int | None,
      detail_fetcher: Callable[[str], Awaitable[dict[str, Any] | None]],
  ) -> dict[str, Any]:
  ```
  변경 후:
  ```python
  async def _build_investment_opinions_from_company_list_soup(
      code: str,
      company_list_soup: BeautifulSoup,
      limit: int,
      *,
      current_price: int | None,
      detail_fetcher: Callable[[str], Awaitable[dict[str, Any] | None]],
      window_months: int = 12,
  ) -> dict[str, Any]:
  ```

  (b) 현재 코드(:146-148, 함수 끝):
  ```python
      opinions["count"] = len(opinions["opinions"])
      opinions["consensus"] = build_consensus(opinions["opinions"], current_price)
      return opinions
  ```
  변경 후:
  ```python
      opinions["count"] = len(opinions["opinions"])
      opinions["consensus"] = build_consensus(
          opinions["opinions"], current_price, window_months=window_months
      )
      return opinions
  ```

  (c) `fetch_investment_opinions` — 현재 코드(:301-329):
  ```python
  async def fetch_investment_opinions(code: str, limit: int = 10) -> dict[str, Any]:
      """Fetch securities firm investment opinions and target prices.

      URL: finance.naver.com/research/company_list.naver
      Individual reports: finance.naver.com/research/company_read.naver?nid={nid}

      Args:
          code: 6-digit Korean stock code
          limit: Maximum number of opinions to return

      Returns:
          Investment opinions with normalized ratings and consensus statistics:
          - symbol: Stock code
          - count: Number of opinions
          - opinions: List of individual opinions with normalized ratings
          - consensus: Aggregated statistics (buy/hold/sell counts, target prices, upside_pct)
      """
      url = f"{NAVER_FINANCE_BASE}/research/company_list.naver"
      company_list_soup = await _fetch_html(
          url, params={"searchType": "itemCode", "itemCode": code}
      )
      current_price = await _fetch_current_price(code)
      return await _build_investment_opinions_from_company_list_soup(
          code,
          company_list_soup,
          limit,
          current_price=current_price,
          detail_fetcher=_fetch_report_detail,
      )
  ```
  변경 후:
  ```python
  async def fetch_investment_opinions(
      code: str, limit: int = 10, *, window_months: int = 12
  ) -> dict[str, Any]:
      """Fetch securities firm investment opinions and target prices.

      URL: finance.naver.com/research/company_list.naver
      Individual reports: finance.naver.com/research/company_read.naver?nid={nid}

      Args:
          code: 6-digit Korean stock code
          limit: Maximum number of opinions to return
          window_months: ROB-486 컨센서스 recency 윈도우(개월). 목표가 통계와
              buy/hold/sell 카운트는 이 윈도우 내 date 가 있는 행에서만 집계되고,
              opinions 리스트 자체는 윈도우 밖 행도 포함한다.

      Returns:
          Investment opinions with normalized ratings and consensus statistics:
          - symbol: Stock code
          - count: Number of opinions
          - opinions: List of individual opinions with normalized ratings
          - consensus: Windowed aggregated statistics (buy/hold/sell counts,
            target prices, upside_pct + rows_total/rows_used/rows_excluded_stale/
            rows_excluded_undated/newest_opinion_date/window_months)
      """
      url = f"{NAVER_FINANCE_BASE}/research/company_list.naver"
      company_list_soup = await _fetch_html(
          url, params={"searchType": "itemCode", "itemCode": code}
      )
      current_price = await _fetch_current_price(code)
      return await _build_investment_opinions_from_company_list_soup(
          code,
          company_list_soup,
          limit,
          current_price=current_price,
          detail_fetcher=_fetch_report_detail,
          window_months=window_months,
      )
  ```

  (d) `app/mcp_server/tooling/fundamentals_sources_naver.py` — 현재 코드(:102-108):
  ```python
  async def _fetch_investment_opinions_naver(symbol: str, limit: int) -> dict[str, Any]:
      opinions = await naver_finance.fetch_investment_opinions(symbol, limit=limit)
      return {
          "instrument_type": "equity_kr",
          "source": "naver",
          **opinions,
      }
  ```
  변경 후:
  ```python
  async def _fetch_investment_opinions_naver(
      symbol: str, limit: int, window_months: int = 12
  ) -> dict[str, Any]:
      opinions = await naver_finance.fetch_investment_opinions(
          symbol, limit=limit, window_months=window_months
      )
      return {
          "instrument_type": "equity_kr",
          "source": "naver",
          **opinions,
      }
  ```
  (참고: `_fetch_screen_enrichment_kr`의 `_fetch_investment_opinions_naver(symbol, 10)` 호출(:115)과 `_fetch_kr_snapshot` 경로는 기본값 12를 그대로 사용 — 변경 불필요.)

- [ ] **2.4 Run tests to verify they pass**:
  ```bash
  uv run pytest tests/test_naver_finance.py -v
  ```
  기대: 전체 passed (TestFetchKrSnapshot 포함 — fixture가 최근 날짜라 윈도우 생존).

- [ ] **2.5 Format + commit**:
  ```bash
  uv run ruff format app/services/naver_finance/investor.py app/mcp_server/tooling/fundamentals_sources_naver.py tests/test_naver_finance.py
  uv run ruff check app/services/naver_finance/investor.py app/mcp_server/tooling/fundamentals_sources_naver.py tests/test_naver_finance.py
  git add app/services/naver_finance/investor.py app/mcp_server/tooling/fundamentals_sources_naver.py tests/test_naver_finance.py
  git commit -m "$(cat <<'EOF'
  fix(ROB-486): Naver 의견 수집 경로 window_months 스레딩 + 테스트 fixture 시한폭탄 제거

  fetch_investment_opinions/_build_investment_opinions_from_company_list_soup/
  _fetch_investment_opinions_naver에 window_months(기본 12) 키워드 추가.
  하드코딩 '26.01.15' fixture를 today 기준 상대 날짜로 전환.

  Co-Authored-By: Paperclip <noreply@paperclip.ing>
  EOF
  )"
  ```

---

## Task 3: MCP 도구 `opinion_window_months` 파라미터 + 클램프 + 설명

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals/_valuation.py` (`handle_get_investment_opinions` :71-107)
- Modify: `app/mcp_server/tooling/fundamentals_handlers.py` (`get_investment_opinions` 등록 :190-202)
- Test: `tests/test_mcp_fundamentals_tools.py` (mock 시그니처 :2822, :2870 + 신규 클램프 테스트)

### Steps

- [ ] **3.1 Write the failing test** — `tests/test_mcp_fundamentals_tools.py`의 `get_investment_opinions` 테스트 클래스(:2822 mock이 속한 클래스) 안에 추가:
  ```python
      async def test_kr_opinion_window_months_clamped_and_forwarded(self, monkeypatch):
          """ROB-486: opinion_window_months 가 1~60으로 클램프되어 KR fetcher로 전달."""
          tools = build_tools()
          captured: list[int] = []

          async def mock_fetch(code, limit, window_months=12):
              captured.append(window_months)
              return {
                  "instrument_type": "equity_kr",
                  "source": "naver",
                  "symbol": code,
                  "count": 0,
                  "opinions": [],
                  "consensus": None,
              }

          _patch_runtime_attr(
              monkeypatch, "_fetch_investment_opinions_naver", mock_fetch
          )

          await tools["get_investment_opinions"]("005930", market="kr")
          await tools["get_investment_opinions"](
              "005930", market="kr", opinion_window_months=120
          )
          await tools["get_investment_opinions"](
              "005930", market="kr", opinion_window_months=0
          )

          assert captured == [12, 60, 1]
  ```

- [ ] **3.2 Run test to verify it fails**:
  ```bash
  uv run pytest tests/test_mcp_fundamentals_tools.py -k "opinion_window_months_clamped" -v
  ```
  기대: FAIL — `TypeError: get_investment_opinions() got an unexpected keyword argument 'opinion_window_months'`.

- [ ] **3.3 Write the implementation**:

  (a) `app/mcp_server/tooling/fundamentals/_valuation.py` — 현재 코드(:71-98 발췌):
  ```python
  async def handle_get_investment_opinions(
      symbol: str | int,
      limit: int = 10,
      market: str | None = None,
  ) -> dict[str, Any]:
  ```
  ```python
      normalized_market = normalize_equity_market(str(market))
      capped_limit = min(max(limit, 1), 30)

      try:
          if normalized_market == "kr":
              return await _fetch_investment_opinions_naver(symbol, capped_limit)
          return await _fetch_investment_opinions_yfinance(symbol, capped_limit)
  ```
  변경 후:
  ```python
  async def handle_get_investment_opinions(
      symbol: str | int,
      limit: int = 10,
      market: str | None = None,
      opinion_window_months: int = 12,
  ) -> dict[str, Any]:
  ```
  ```python
      normalized_market = normalize_equity_market(str(market))
      capped_limit = min(max(limit, 1), 30)
      # ROB-486: KR 컨센서스 recency 윈도우 (1~60개월 클램프). US(yfinance)는
      # 벤더 컨센서스를 그대로 쓰므로 적용되지 않는다.
      capped_window = min(max(opinion_window_months, 1), 60)

      try:
          if normalized_market == "kr":
              return await _fetch_investment_opinions_naver(
                  symbol, capped_limit, window_months=capped_window
              )
          return await _fetch_investment_opinions_yfinance(symbol, capped_limit)
  ```
  (except 블록은 무변경.)

  (b) `app/mcp_server/tooling/fundamentals_handlers.py` — 현재 코드(:190-202):
  ```python
      @mcp.tool(
          name="get_investment_opinions",
          description=(
              "Get securities firm investment opinions and target prices for a US or "
              "Korean stock. Returns analyst ratings, price targets, and upside potential."
          ),
      )
      async def get_investment_opinions(
          symbol: str | int,
          limit: int = 10,
          market: str | None = None,
      ) -> dict[str, Any]:
          return await handle_get_investment_opinions(symbol, limit, market)
  ```
  변경 후:
  ```python
      @mcp.tool(
          name="get_investment_opinions",
          description=(
              "Get securities firm investment opinions and target prices for a US or "
              "Korean stock. Returns analyst ratings, price targets, and upside "
              "potential. KR consensus (buy/hold/sell counts and avg/median/min/max "
              "target, upside_pct) is aggregated ONLY over opinions dated within "
              "opinion_window_months (default 12, clamped 1-60); older or undated "
              "rows are excluded from the aggregate and reported via "
              "rows_excluded_stale / rows_excluded_undated. If no opinion survives "
              "the window, target stats and upside_pct are null and counts are 0 — "
              "there is NO fallback to stale averages (check rows_total, rows_used, "
              "newest_opinion_date, window_months metadata). The opinions list still "
              "includes older rows for reference. US consensus comes from the vendor "
              "(yfinance) and ignores opinion_window_months."
          ),
      )
      async def get_investment_opinions(
          symbol: str | int,
          limit: int = 10,
          market: str | None = None,
          opinion_window_months: int = 12,
      ) -> dict[str, Any]:
          return await handle_get_investment_opinions(
              symbol, limit, market, opinion_window_months
          )
  ```

  (c) 기존 KR mock 시그니처 갱신 — `tests/test_mcp_fundamentals_tools.py`. 현재 코드(:2822, :2870 두 곳 동일):
  ```python
          async def mock_fetch(code, limit):
  ```
  변경 후 (두 곳 모두):
  ```python
          async def mock_fetch(code, limit, window_months=12):
  ```

- [ ] **3.4 Run tests to verify they pass**:
  ```bash
  uv run pytest tests/test_mcp_fundamentals_tools.py -k "investment_opinions or opinion_window" -v
  ```
  기대: 전체 passed.

- [ ] **3.5 Format + commit**:
  ```bash
  uv run ruff format app/mcp_server/tooling/fundamentals/_valuation.py app/mcp_server/tooling/fundamentals_handlers.py tests/test_mcp_fundamentals_tools.py
  uv run ruff check app/mcp_server/tooling/fundamentals/_valuation.py app/mcp_server/tooling/fundamentals_handlers.py tests/test_mcp_fundamentals_tools.py
  git add app/mcp_server/tooling/fundamentals/_valuation.py app/mcp_server/tooling/fundamentals_handlers.py tests/test_mcp_fundamentals_tools.py
  git commit -m "$(cat <<'EOF'
  fix(ROB-486): get_investment_opinions에 opinion_window_months 파라미터(1~60 클램프) + 윈도우/메타데이터 문서화

  Co-Authored-By: Paperclip <noreply@paperclip.ing>
  EOF
  )"
  ```

---

## Task 4: `consensus_present` 플로어 강화 — rows_used/total 기반

**Files:**
- Modify: `app/mcp_server/tooling/analysis_analyze.py` (`_apply_recommendation` :419-457 + 헬퍼 신설)
- Test: `tests/test_analyze_stock_floor.py`

### Steps

- [ ] **4.1 Write the failing tests** — `tests/test_analyze_stock_floor.py` 끝에 추가:
  ```python
  @pytest.mark.unit
  def test_floor_holds_when_consensus_stale_only():
      """ROB-486 (031330): 윈도우 생존 row 0인 컨센서스는 presence 로 치지 않는다."""
      analysis = {
          "quote": {"price": 15360.0},
          "indicators": {"rsi": {"14": 25.0}},
          "support_resistance": {"supports": [{"price": 14000.0}]},
          "opinions": {
              "consensus": {
                  "buy_count": 0,
                  "hold_count": 0,
                  "sell_count": 0,
                  "strong_buy_count": 0,
                  "total_count": 0,
                  "avg_target_price": None,
                  "median_target_price": None,
                  "min_target_price": None,
                  "max_target_price": None,
                  "upside_pct": None,
                  "current_price": 15360,
                  "rows_total": 2,
                  "rows_used": 0,
                  "rows_excluded_stale": 2,
                  "rows_excluded_undated": 0,
                  "newest_opinion_date": "2019-12-27",
                  "window_months": 12,
              }
          },
          "valuation": {},
      }
      _apply_recommendation(analysis, "equity_kr")
      rec = analysis["recommendation"]
      assert rec["action"] == "hold"
      assert rec["confidence"] == "low"
      assert "consensus" in rec["insufficient_inputs"]


  @pytest.mark.unit
  def test_floor_passes_when_windowed_rows_used_positive():
      """rows_used>0 이면 presence 인정."""
      analysis = {
          "quote": {"price": 1000.0},
          "indicators": {"rsi": {"14": 25.0}},
          "support_resistance": {"supports": [{"price": 950.0}]},
          "opinions": {
              "consensus": {
                  "buy_count": 8,
                  "sell_count": 1,
                  "strong_buy_count": 5,
                  "total_count": 10,
                  "rows_used": 10,
              }
          },
          "valuation": {},
      }
      _apply_recommendation(analysis, "equity_kr")
      rec = analysis["recommendation"]
      assert rec["insufficient_inputs"] == []
      assert rec["action"] == "buy"


  @pytest.mark.unit
  def test_floor_holds_when_us_consensus_counts_all_none():
      """US(yfinance) rows_used 없음 + total_count None → presence 불인정 (fail-closed)."""
      analysis = {
          "quote": {"price": 150.0},
          "indicators": {"rsi": {"14": 25.0}},
          "support_resistance": {"supports": []},
          "opinions": {
              "consensus": {
                  "buy_count": None,
                  "hold_count": None,
                  "sell_count": None,
                  "strong_buy_count": None,
                  "total_count": None,
                  "avg_target_price": 195.5,
                  "upside_pct": 8.0,
                  "current_price": 150.0,
              }
          },
          "valuation": {},
      }
      _apply_recommendation(analysis, "equity_us")
      rec = analysis["recommendation"]
      assert rec["action"] == "hold"
      assert "consensus" in rec["insufficient_inputs"]
  ```

- [ ] **4.2 Run tests to verify they fail**:
  ```bash
  uv run pytest tests/test_analyze_stock_floor.py -v
  ```
  기대: 신규 3개 중 `test_floor_holds_when_consensus_stale_only`와 `test_floor_holds_when_us_consensus_counts_all_none` FAIL (truthy dict가 presence 통과 → insufficient_inputs에 consensus 없음). 기존 3개 + `test_floor_passes_when_windowed_rows_used_positive` PASS.

- [ ] **4.3 Write the implementation** — `app/mcp_server/tooling/analysis_analyze.py`:

  (a) `_apply_recommendation`(:419) 바로 위에 헬퍼 추가:
  ```python
  def _consensus_rows_present(consensus: Any) -> bool:
      """ROB-486: stale-only 컨센서스가 presence 플로어를 통과하지 못하게 한다.

      KR(naver) windowed consensus 는 rows_used(윈도우 생존 row 수) 기준,
      US(yfinance) consensus 는 rows_used 가 없으므로 total_count 기준.
      둘 다 없거나 0 이면 consensus 부재로 본다 (fail-closed).
      """
      if not isinstance(consensus, dict) or not consensus:
          return False
      rows_used = consensus.get("rows_used")
      if rows_used is not None:
          try:
              return int(rows_used) > 0
          except (TypeError, ValueError):
              return False
      total = consensus.get("total_count")
      if total is None:
          return False
      try:
          return int(total) > 0
      except (TypeError, ValueError):
          return False
  ```

  (b) 현재 코드(:430):
  ```python
      consensus_present = bool((analysis.get("opinions") or {}).get("consensus"))
  ```
  변경 후:
  ```python
      consensus_present = _consensus_rows_present(
          (analysis.get("opinions") or {}).get("consensus")
      )
  ```

- [ ] **4.4 Run tests to verify they pass**:
  ```bash
  uv run pytest tests/test_analyze_stock_floor.py tests/test_symbol_analysis_floor.py -v
  ```
  기대: 전체 passed.

- [ ] **4.5 Format + commit**:
  ```bash
  uv run ruff format app/mcp_server/tooling/analysis_analyze.py tests/test_analyze_stock_floor.py
  uv run ruff check app/mcp_server/tooling/analysis_analyze.py tests/test_analyze_stock_floor.py
  git add app/mcp_server/tooling/analysis_analyze.py tests/test_analyze_stock_floor.py
  git commit -m "$(cat <<'EOF'
  fix(ROB-486): consensus_present 플로어를 rows_used/total_count 기반으로 강화

  stale-only(윈도우 생존 0) 또는 카운트 전무(US 벤더 미제공) 컨센서스는
  truthy dict여도 presence로 인정하지 않는다 (fail-closed hold).

  Co-Authored-By: Paperclip <noreply@paperclip.ing>
  EOF
  )"
  ```

---

## Task 5: upside-aware `build_recommendation_for_equity` (475150 케이스)

**Files:**
- Modify: `app/mcp_server/tooling/shared.py` (상수/헬퍼 :467 뒤, 본문 :532-607, `__all__` :791-792)
- Test: `tests/test_mcp_fundamentals_tools.py` (기존 추천 테스트 클래스 :164-258 부근에 추가)

### Steps

- [ ] **5.1 Write the failing tests** — `tests/test_mcp_fundamentals_tools.py`의 추천 테스트들(`test_recommendation_generation_skips_unavailable_consensus_counts` 등이 속한 클래스, :178-258) 뒤에 추가:
  ```python
      async def test_recommendation_blocks_buy_when_consensus_target_exceeded(self):
          """ROB-486 (475150 실측): 8 buy/0 sell이어도 upside -26.44% → buy 금지."""
          mock_analysis = {
              "symbol": "475150",
              "market_type": "equity_kr",
              "source": "kis",
              "quote": {"price": 44350},
              "support_resistance": {"supports": [], "resistances": []},
              "opinions": {
                  "consensus": {
                      "buy_count": 8,
                      "hold_count": 0,
                      "sell_count": 0,
                      "strong_buy_count": 0,
                      "total_count": 8,
                      "avg_target_price": 32625,
                      "upside_pct": -26.44,
                      "current_price": 44350,
                  }
              },
          }

          rec = shared.build_recommendation_for_equity(mock_analysis, "equity_kr")

          assert rec is not None
          assert rec["action"] == "hold"
          assert "target_exceeded" in rec["reasoning"]
          assert "Consensus target below current price" in rec["reasoning"]
          assert "Analyst consensus bullish" not in rec["reasoning"]

      async def test_recommendation_demotes_rsi_buy_when_consensus_target_exceeded(
          self,
      ):
          """ROB-486: RSI 단독 +2로 buy가 나와도 음수 upside 컨센서스면 hold 강등."""
          mock_analysis = {
              "quote": {"price": 44350},
              "indicators": {"indicators": {"rsi": {"14": 25.0}}},
              "support_resistance": {"supports": [], "resistances": []},
              "opinions": {
                  "consensus": {
                      "buy_count": 1,
                      "hold_count": 3,
                      "sell_count": 0,
                      "strong_buy_count": 0,
                      "total_count": 4,
                      "avg_target_price": 32625,
                      "upside_pct": -26.44,
                      "current_price": 44350,
                  }
              },
          }

          rec = shared.build_recommendation_for_equity(mock_analysis, "equity_kr")

          assert rec is not None
          assert rec["action"] == "hold"
          assert rec["confidence"] == "low"
          assert "target_exceeded" in rec["reasoning"]

      async def test_recommendation_allows_buy_with_mildly_negative_upside(self):
          """ROB-486: 임계(-10%)보다 완만한 -5% upside는 기존 count 가산 유지."""
          mock_analysis = {
              "quote": {"price": 1000},
              "support_resistance": {"supports": [], "resistances": []},
              "opinions": {
                  "consensus": {
                      "buy_count": 8,
                      "hold_count": 0,
                      "sell_count": 0,
                      "strong_buy_count": 0,
                      "total_count": 8,
                      "avg_target_price": 950,
                      "upside_pct": -5.0,
                      "current_price": 1000,
                  }
              },
          }

          rec = shared.build_recommendation_for_equity(mock_analysis, "equity_kr")

          assert rec is not None
          assert rec["action"] == "buy"
          assert "Analyst consensus bullish" in rec["reasoning"]
          assert "target_exceeded" not in rec["reasoning"]

      async def test_recommendation_demotes_at_exact_threshold(self):
          """ROB-486: upside == -10.0 (임계 동치)도 강등 대상 (<=)."""
          mock_analysis = {
              "quote": {"price": 1000},
              "support_resistance": {"supports": [], "resistances": []},
              "opinions": {
                  "consensus": {
                      "buy_count": 8,
                      "hold_count": 0,
                      "sell_count": 0,
                      "strong_buy_count": 0,
                      "total_count": 8,
                      "avg_target_price": 900,
                      "upside_pct": -10.0,
                      "current_price": 1000,
                  }
              },
          }

          rec = shared.build_recommendation_for_equity(mock_analysis, "equity_kr")

          assert rec is not None
          assert rec["action"] == "hold"
          assert "target_exceeded" in rec["reasoning"]
  ```

- [ ] **5.2 Run tests to verify they fail**:
  ```bash
  uv run pytest tests/test_mcp_fundamentals_tools.py -k "target_exceeded or mildly_negative or exact_threshold" -v
  ```
  기대: `blocks_buy`/`demotes_rsi`/`exact_threshold` FAIL (action == "buy"), `allows_buy...` PASS.

- [ ] **5.3 Write the implementation** — `app/mcp_server/tooling/shared.py`:

  (a) `_to_optional_consensus_count`(:455-467) 바로 뒤에 추가:
  ```python
  # ROB-486: 컨센서스 평균 목표가의 upside 가 이 값(%) 이하면 — 즉 현재가가 평균
  # 목표가를 ~10% 이상 초과(target_exceeded) — count 기반 buy 가산을 차단하고
  # 최종 buy 를 hold 로 강등한다.
  CONSENSUS_NEGATIVE_UPSIDE_DEMOTION_PCT = -10.0


  def _to_optional_consensus_upside(value: Any) -> float | None:
      """consensus.upside_pct 방어 파싱 — 숫자만 (bool/NaN/문자 제외)."""
      if isinstance(value, bool) or value is None:
          return None
      if isinstance(value, (int, float)):
          try:
              if pd.isna(value):
                  return None
          except Exception:
              pass
          return float(value)
      return None
  ```

  (b) `build_recommendation_for_equity` 본문 — 현재 코드(:532-534):
  ```python
      reasoning_parts: list[str] = []
      score = 0
      max_score = 0
  ```
  변경 후:
  ```python
      reasoning_parts: list[str] = []
      score = 0
      max_score = 0

      # ROB-486: 컨센서스 평균 목표가가 현재가 대비 임계 이하(음수 upside)면
      # count 기반 buy 가산 차단 + 최종 buy 강등에 사용.
      consensus_target_exceeded = False
      consensus_demotion_reason: str | None = None
  ```

  (c) 현재 코드(:554-597, consensus 블록):
  ```python
      if consensus:
          buy_count = _to_optional_consensus_count(consensus.get("buy_count"))
          sell_count = _to_optional_consensus_count(consensus.get("sell_count"))
          strong_buy_count = _to_optional_consensus_count(
              consensus.get("strong_buy_count")
          )
          total = _to_optional_consensus_count(consensus.get("total_count"))

          if (
              total is not None
              and total > 0
              and buy_count is not None
              and sell_count is not None
              and strong_buy_count is not None
          ):
              max_score += 2
              buy_ratio = buy_count / total
              sell_ratio = sell_count / total

              if buy_ratio > 0.6:
                  score += 2
                  if strong_buy_count > 0 and strong_buy_count >= buy_count / 2:
                      reasoning_parts.append(
                          f"Analyst consensus strong bullish ({buy_count} buy, {strong_buy_count} strong buy vs {sell_count} sell)"
                      )
                  else:
                      reasoning_parts.append(
                          f"Analyst consensus bullish ({buy_count} buy vs {sell_count} sell)"
                      )
              elif buy_ratio > 0.4:
                  score += 1
                  reasoning_parts.append(
                      f"Analyst consensus moderate ({buy_count} buy vs {sell_count} sell)"
                  )
              elif sell_ratio > 0.6:
                  score -= 2
                  reasoning_parts.append(
                      f"Analyst consensus bearish ({sell_count} sell vs {buy_count} buy)"
                  )
              elif sell_ratio > 0.4:
                  score -= 1
                  reasoning_parts.append(
                      f"Analyst consensus cautious ({sell_count} sell vs {buy_count} buy)"
                  )
  ```
  변경 후:
  ```python
      if consensus:
          buy_count = _to_optional_consensus_count(consensus.get("buy_count"))
          sell_count = _to_optional_consensus_count(consensus.get("sell_count"))
          strong_buy_count = _to_optional_consensus_count(
              consensus.get("strong_buy_count")
          )
          total = _to_optional_consensus_count(consensus.get("total_count"))

          upside_pct = _to_optional_consensus_upside(consensus.get("upside_pct"))
          if (
              upside_pct is not None
              and upside_pct <= CONSENSUS_NEGATIVE_UPSIDE_DEMOTION_PCT
          ):
              consensus_target_exceeded = True
              consensus_demotion_reason = (
                  "Consensus target below current price "
                  f"(upside {upside_pct:.1f}%) — target_exceeded"
              )

          if (
              total is not None
              and total > 0
              and buy_count is not None
              and sell_count is not None
              and strong_buy_count is not None
          ):
              max_score += 2
              buy_ratio = buy_count / total
              sell_ratio = sell_count / total

              if buy_ratio > 0.6:
                  if consensus_target_exceeded:
                      # ROB-486 (a): 목표가 초과 상태에서는 +2 가산도, bullish
                      # 문구도 내지 않는다.
                      if consensus_demotion_reason is not None:
                          reasoning_parts.append(consensus_demotion_reason)
                  else:
                      score += 2
                      if strong_buy_count > 0 and strong_buy_count >= buy_count / 2:
                          reasoning_parts.append(
                              f"Analyst consensus strong bullish ({buy_count} buy, {strong_buy_count} strong buy vs {sell_count} sell)"
                          )
                      else:
                          reasoning_parts.append(
                              f"Analyst consensus bullish ({buy_count} buy vs {sell_count} sell)"
                          )
              elif buy_ratio > 0.4:
                  score += 1
                  reasoning_parts.append(
                      f"Analyst consensus moderate ({buy_count} buy vs {sell_count} sell)"
                  )
              elif sell_ratio > 0.6:
                  score -= 2
                  reasoning_parts.append(
                      f"Analyst consensus bearish ({sell_count} sell vs {buy_count} buy)"
                  )
              elif sell_ratio > 0.4:
                  score -= 1
                  reasoning_parts.append(
                      f"Analyst consensus cautious ({sell_count} sell vs {buy_count} buy)"
                  )
  ```

  (d) 현재 코드(:599-607, action 매핑):
  ```python
      if score >= 2:
          recommendation["action"] = "buy"
          recommendation["confidence"] = "high" if score >= 3 else "medium"
      elif score <= -2:
          recommendation["action"] = "sell"
          recommendation["confidence"] = "high" if score <= -3 else "medium"
      else:
          recommendation["action"] = "hold"
          recommendation["confidence"] = "low"
  ```
  변경 후:
  ```python
      if score >= 2:
          recommendation["action"] = "buy"
          recommendation["confidence"] = "high" if score >= 3 else "medium"
      elif score <= -2:
          recommendation["action"] = "sell"
          recommendation["confidence"] = "high" if score <= -3 else "medium"
      else:
          recommendation["action"] = "hold"
          recommendation["confidence"] = "low"

      # ROB-486 (c): 다른 경로(RSI 단독 +2, moderate +1 조합 등)로 buy 가 산출돼도
      # 컨센서스 목표가 초과 상태면 hold 로 강등한다.
      if consensus_target_exceeded and recommendation["action"] == "buy":
          recommendation["action"] = "hold"
          recommendation["confidence"] = "low"
          if (
              consensus_demotion_reason is not None
              and consensus_demotion_reason not in reasoning_parts
          ):
              reasoning_parts.append(consensus_demotion_reason)
  ```

  (e) `__all__`(:791-792) — 현재 코드:
  ```python
      # Recommendation builder
      "build_recommendation_for_equity",
  ```
  변경 후:
  ```python
      # Recommendation builder
      "CONSENSUS_NEGATIVE_UPSIDE_DEMOTION_PCT",
      "build_recommendation_for_equity",
  ```

- [ ] **5.4 Run tests to verify they pass**:
  ```bash
  uv run pytest tests/test_mcp_fundamentals_tools.py -k "recommendation or target_exceeded or mildly_negative" -v
  ```
  기대: 전체 passed (기존 count-only 추천 테스트는 upside 미포함 fixture라 무영향).

- [ ] **5.5 Format + commit**:
  ```bash
  uv run ruff format app/mcp_server/tooling/shared.py tests/test_mcp_fundamentals_tools.py
  uv run ruff check app/mcp_server/tooling/shared.py tests/test_mcp_fundamentals_tools.py
  git add app/mcp_server/tooling/shared.py tests/test_mcp_fundamentals_tools.py
  git commit -m "$(cat <<'EOF'
  fix(ROB-486): build_recommendation_for_equity에 upside-aware 강등 (target_exceeded)

  upside_pct <= -10%면 (a) buy-count +2 가산 차단, (b) 'Consensus target below
  current price (upside N%) — target_exceeded' reason, (c) 최종 buy → hold 강등.

  Co-Authored-By: Paperclip <noreply@paperclip.ing>
  EOF
  )"
  ```

---

## Task 6: `derived.py` 동일 로직 포트 + RULE_VERSION 범프

**Files:**
- Modify: `app/services/symbol_analysis/derived.py` (RULE_VERSION :22, `_score_action` :25-59, `derive_recommendation` :130-136)
- Test: `tests/test_symbol_analysis_derived.py`

### Steps

- [ ] **6.1 Write the failing tests** — `tests/test_symbol_analysis_derived.py` 끝에 추가 (파일 상단 `_block`/`ConsensusData` 헬퍼 재사용):
  ```python
  def _negative_upside_consensus():
      # 475150 실측 모양: 8 buy / 0 sell, avg target 32,625 vs current 44,350 → -26.44%
      return ConsensusData(
          buy=8,
          hold=0,
          sell=0,
          strong_buy=0,
          total=8,
          target_avg=32625.0,
          upside_pct=-26.44,
      )


  @pytest.mark.unit
  def test_rule_version_bumped_for_upside_demotion():
      # ROB-486: 스코어링 규칙 변경 → contract-versioned RULE_VERSION 범프.
      assert RULE_VERSION == "symbol_analysis.derived.v2"


  @pytest.mark.unit
  def test_negative_upside_consensus_blocks_count_buy():
      d = derive_recommendation(
          price=_block(PriceData(44350.0)),
          technicals=_block(TechnicalData(rsi14=50.0, supports=(43000.0,))),
          consensus=_block(_negative_upside_consensus()),
      )
      assert d.action == "hold"
      assert d.confidence == "low"
      assert d.insufficient_inputs == ()


  @pytest.mark.unit
  def test_negative_upside_demotes_rsi_driven_buy():
      d = derive_recommendation(
          price=_block(PriceData(44350.0)),
          technicals=_block(TechnicalData(rsi14=25.0, supports=(43000.0,))),
          consensus=_block(_negative_upside_consensus()),
      )
      assert d.action == "hold"
      assert d.confidence == "low"


  @pytest.mark.unit
  def test_mildly_negative_upside_keeps_count_buy():
      cons = ConsensusData(
          buy=8, hold=0, sell=0, strong_buy=0, total=8, upside_pct=-5.0
      )
      d = derive_recommendation(
          price=_block(PriceData(1000.0)),
          technicals=_block(TechnicalData(rsi14=50.0, supports=(950.0,))),
          consensus=_block(cons),
      )
      assert d.action == "buy"
  ```

- [ ] **6.2 Run tests to verify they fail**:
  ```bash
  uv run pytest tests/test_symbol_analysis_derived.py -v
  ```
  기대: `test_rule_version_bumped...`, `test_negative_upside_consensus_blocks_count_buy`, `test_negative_upside_demotes_rsi_driven_buy` FAIL. 기존 테스트 + `test_mildly_negative...` PASS.

- [ ] **6.3 Write the implementation** — `app/services/symbol_analysis/derived.py`:

  (a) 현재 코드(:22):
  ```python
  RULE_VERSION = "symbol_analysis.derived.v1"
  ```
  변경 후:
  ```python
  RULE_VERSION = "symbol_analysis.derived.v2"

  # ROB-486: shared.py::build_recommendation_for_equity 와 동일 임계값 포팅
  # (services → mcp_server import 금지 — 복제). 컨센서스 평균 목표가 upside 가
  # 이 값(%) 이하면 count 기반 buy 가산을 차단하고 최종 buy 를 hold 로 강등.
  CONSENSUS_NEGATIVE_UPSIDE_DEMOTION_PCT = -10.0


  def _consensus_target_exceeded(consensus: ConsensusData | None) -> bool:
      """평균 목표가가 현재가 대비 임계 이상 낮은가 (upside_pct <= -10)."""
      if consensus is None or consensus.upside_pct is None:
          return False
      return consensus.upside_pct <= CONSENSUS_NEGATIVE_UPSIDE_DEMOTION_PCT
  ```

  (b) `_score_action` — 현재 코드(:25-59) 중 docstring과 buy_ratio 분기를 변경:
  ```python
  def _score_action(
      rsi14: float | None, consensus: ConsensusData | None
  ) -> tuple[int, int]:
      """(score, max_score). shared.build_recommendation_for_equity 와 동일 임계값."""
  ```
  ```python
          if buy_ratio > 0.6:
              score += 2
  ```
  변경 후:
  ```python
  def _score_action(
      rsi14: float | None, consensus: ConsensusData | None
  ) -> tuple[int, int]:
      """(score, max_score). shared.build_recommendation_for_equity 와 동일 임계값.

      ROB-486: buy_ratio > 0.6 의 +2 는 컨센서스 목표가 초과(upside <= -10%)면
      가산하지 않는다.
      """
  ```
  ```python
          if buy_ratio > 0.6:
              if not _consensus_target_exceeded(consensus):
                  score += 2
  ```
  (나머지 분기 `elif buy_ratio > 0.4:` 이하 무변경.)

  (c) `derive_recommendation` — 현재 코드(:130-136):
  ```python
      score, _ = _score_action(tech.rsi14, cons)
      if score >= 2:
          action, confidence = "buy", ("high" if score >= 3 else "medium")
      elif score <= -2:
          action, confidence = "sell", ("high" if score <= -3 else "medium")
      else:
          action, confidence = "hold", "low"
  ```
  변경 후:
  ```python
      score, _ = _score_action(tech.rsi14, cons)
      if score >= 2:
          action, confidence = "buy", ("high" if score >= 3 else "medium")
      elif score <= -2:
          action, confidence = "sell", ("high" if score <= -3 else "medium")
      else:
          action, confidence = "hold", "low"

      # ROB-486: RSI 단독 +2 등 다른 경로로 buy 가 나와도 목표가 초과면 강등.
      if action == "buy" and _consensus_target_exceeded(cons):
          action, confidence = "hold", "low"
  ```

- [ ] **6.4 Run tests to verify they pass**:
  ```bash
  uv run pytest tests/test_symbol_analysis_derived.py tests/test_symbol_analysis_contract.py tests/test_symbol_analysis_floor.py -v
  ```
  기대: 전체 passed. (`tests/test_symbol_analysis_contract.py:50`의 `rule_version="symbol_analysis.derived.v1"`은 자체 DerivedBlock 생성 리터럴이라 무영향.)

- [ ] **6.5 Format + commit**:
  ```bash
  uv run ruff format app/services/symbol_analysis/derived.py tests/test_symbol_analysis_derived.py
  uv run ruff check app/services/symbol_analysis/derived.py tests/test_symbol_analysis_derived.py
  git add app/services/symbol_analysis/derived.py tests/test_symbol_analysis_derived.py
  git commit -m "$(cat <<'EOF'
  fix(ROB-486): symbol_analysis derived 스코어러에 upside 강등 포트 + RULE_VERSION v2

  services→mcp_server import 금지 규칙에 따라 shared.py 로직을 복제 포팅.
  ConsensusData.upside_pct(contract.py:61, 기존 미사용)를 _score_action에서 소비.

  Co-Authored-By: Paperclip <noreply@paperclip.ing>
  EOF
  )"
  ```

---

## Task 7: Blast-radius 회귀 고정 (스크리너/배치 요약/패널 스키마)

**Files:**
- Create: `tests/test_rob486_consensus_blast_radius.py`

확인 대상 (코드 변경 없음 — 회귀 테스트로 고정):
- `_build_screen_enrichment_payload`의 `_coerce_optional_number`(`fundamentals_sources_common.py:54-61, 81-84`)는 None-safe — stale-only 컨센서스(전부 None/0)에서 `avg_target`/`upside_pct`=None, `analyst_*`=0.
- `analyze_stock_batch` 요약(`analysis_tool_handlers.py:504-505`)은 consensus dict를 그대로 통과 — 신규 메타데이터 키는 additive.
- `StockDetailAnalystConsensus`(`app/schemas/invest_stock_detail_research_consensus.py:35-49`)는 목표가 필드 전부 `| None` 허용.

### Steps

- [ ] **7.1 Write the tests** — `tests/test_rob486_consensus_blast_radius.py` 신규 생성:
  ```python
  """ROB-486 blast-radius 회귀 고정 — windowed consensus(None 목표가 + 메타데이터)가
  다운스트림 소비자(스크리너 enrichment / 배치 요약 / 패널 스키마)를 깨지 않는다."""

  from __future__ import annotations

  import pytest

  from app.mcp_server.tooling.analysis_tool_handlers import _summarize_analysis_result
  from app.mcp_server.tooling.fundamentals_sources_common import (
      _build_screen_enrichment_payload,
  )
  from app.schemas.invest_stock_detail_research_consensus import (
      StockDetailAnalystConsensus,
  )

  # 031330 실측 모양의 stale-only windowed consensus.
  _STALE_ONLY_WINDOWED_CONSENSUS = {
      "buy_count": 0,
      "hold_count": 0,
      "sell_count": 0,
      "strong_buy_count": 0,
      "total_count": 0,
      "avg_target_price": None,
      "median_target_price": None,
      "min_target_price": None,
      "max_target_price": None,
      "upside_pct": None,
      "current_price": 15360,
      "rows_total": 2,
      "rows_used": 0,
      "rows_excluded_stale": 2,
      "rows_excluded_undated": 0,
      "newest_opinion_date": "2019-12-27",
      "window_months": 12,
  }


  @pytest.mark.unit
  def test_screen_enrichment_payload_none_safe_with_stale_only_consensus():
      payload = _build_screen_enrichment_payload(
          sector="반도체", consensus=_STALE_ONLY_WINDOWED_CONSENSUS
      )

      assert payload["analyst_buy"] == 0
      assert payload["analyst_hold"] == 0
      assert payload["analyst_sell"] == 0
      assert payload["avg_target"] is None
      assert payload["upside_pct"] is None


  @pytest.mark.unit
  def test_batch_summary_passes_windowed_consensus_through():
      analysis = {
          "market_type": "equity_kr",
          "source": "naver",
          "quote": {"price": 15360},
          "opinions": {"consensus": _STALE_ONLY_WINDOWED_CONSENSUS},
          "recommendation": {"action": "hold", "confidence": "low"},
      }

      summary = _summarize_analysis_result("031330", analysis)

      assert summary["consensus"] == _STALE_ONLY_WINDOWED_CONSENSUS
      assert summary["consensus"]["rows_used"] == 0
      assert summary["recommendation"]["action"] == "hold"


  @pytest.mark.unit
  def test_stock_detail_consensus_schema_accepts_null_targets():
      model = StockDetailAnalystConsensus(
          source="naver",
          buyCount=0,
          holdCount=0,
          sellCount=0,
          strongBuyCount=0,
          totalCount=0,
          avgTargetPrice=None,
          medianTargetPrice=None,
          minTargetPrice=None,
          maxTargetPrice=None,
          upsidePct=None,
          currentPrice=15360.0,
      )
      assert model.avgTargetPrice is None
      assert model.upsidePct is None
  ```

- [ ] **7.2 Run tests to verify they pass** (회귀 고정 — 기존 코드가 이미 None-safe인지 증명):
  ```bash
  uv run pytest tests/test_rob486_consensus_blast_radius.py -v
  ```
  기대: 3 passed. FAIL 시 해당 소비자가 None-unsafe라는 뜻 — 그 소비자를 고치고 재실행 (예상되지는 않음; `_coerce_optional_number`/스키마는 코드 리딩으로 None-safe 확인됨).

- [ ] **7.3 스크리너 mock 경로 무회귀 확인**:
  ```bash
  uv run pytest tests/test_mcp_fundamentals_tools.py -k "screen_enrichment" -v
  uv run pytest tests/ -k "screen_stocks" -q -m "not integration and not live" 2>&1 | tail -5
  ```
  기대: 전부 passed (screen enrichment 경로의 `_fetch_investment_opinions_naver(symbol, 10)` 2-인자 호출은 무변경).

- [ ] **7.4 Format + commit**:
  ```bash
  uv run ruff format tests/test_rob486_consensus_blast_radius.py
  uv run ruff check tests/test_rob486_consensus_blast_radius.py
  git add tests/test_rob486_consensus_blast_radius.py
  git commit -m "$(cat <<'EOF'
  fix(ROB-486): windowed consensus blast-radius 회귀 고정 (스크리너/배치요약/패널 스키마 None-safe)

  Co-Authored-By: Paperclip <noreply@paperclip.ing>
  EOF
  )"
  ```

---

## Task 8: 풀 게이트 + PR 생성 + operator 검증 절차

**Files:** 코드 변경 없음.

### Steps

- [ ] **8.1 풀 lint 게이트** (CI는 app/ + tests/ 둘 다 검사):
  ```bash
  cd /Users/mgh3326/work/auto_trader.rob-486
  make lint
  ```
  기대: ruff check/format-check + ty 전부 clean. 실패 시 `make format` 후 잔여 위반 수동 수정.

- [ ] **8.2 관련 테스트 일괄 실행**:
  ```bash
  uv run pytest tests/test_analyst_normalizer.py tests/test_naver_finance.py \
    tests/test_stock_detail_research_consensus_service.py \
    tests/test_mcp_fundamentals_tools.py tests/test_analyze_stock_floor.py \
    tests/test_symbol_analysis_derived.py tests/test_symbol_analysis_contract.py \
    tests/test_symbol_analysis_floor.py tests/test_fundamentals_sources_naver.py \
    tests/test_rob486_consensus_blast_radius.py -v
  ```
  기대: 전부 passed.

- [ ] **8.3 전체 스위트** (analyze 플로우/스크리너 등 간접 소비자 회귀 확인):
  ```bash
  uv run pytest tests/ -m "not live" -q
  ```
  기대: 전부 passed. 만약 analyze 플로우 테스트가 강화된 `consensus_present`(rows_used/total 기반) 때문에 hold 플로어로 떨어져 실패하면, 해당 테스트의 consensus fixture에 실제 카운트(`total_count`>0)를 채우는 방향으로 갱신 (fake 통과 금지 — 새 의미가 정직한 동작).

- [ ] **8.4 최신 main 반영 후 재검증** (PR 직전 — upstream 드리프트 조기 발견):
  ```bash
  git fetch --prune origin
  git merge origin/main   # 충돌 시 해소 후 커밋
  uv run pytest tests/test_analyst_normalizer.py tests/test_mcp_fundamentals_tools.py -q
  ```

- [ ] **8.5 Push + PR 생성** (base `main`):
  ```bash
  git push -u origin rob-486
  gh pr create --base main \
    --title "fix(ROB-486): 컨센서스 목표가 recency 윈도우(12mo) + upside-aware buy 강등" \
    --body "$(cat <<'EOF'
  ## Summary
  - **소스 정정**: KR 투자의견은 KIS TR이 아니라 Naver 리서치 스크랩 (이슈 본문 전제 정정). 기업행위(액면) 보정/DART corporate-action은 레포 내 데이터 소스가 없어 명시적 Non-goal.
  - `build_consensus`를 date-aware로: 목표가 통계 + buy/hold/sell 카운트를 **동일한 윈도우 생존 row 집합**(기본 12개월, 행별 ISO date)에서만 집계. undated/stale row는 fail-closed 제외 + `rows_total/rows_used/rows_excluded_stale/rows_excluded_undated/newest_opinion_date/window_months` 메타데이터. 생존 0이면 전부 None/0 — 무필터 폴백 금지 (031330: avg 2,700/-82% → 정직한 None).
  - 웹 패널(`_normalize_opinion`)이 date를 보존해 stock-detail 패널도 동일 필터 적용 (도구/패널 일치).
  - `get_investment_opinions`에 `opinion_window_months`(기본 12, 1~60 클램프) 추가 + 도구 설명에 윈도우/메타데이터 문서화. US(yfinance)는 벤더 컨센서스 유지로 미적용.
  - `consensus_present` 플로어를 rows_used/total_count 기반으로 강화 — stale-only 컨센서스가 플로어를 통과 못함.
  - **upside-aware 강등** (475150 — 날짜 필터로는 못 고침): `upside_pct <= -10%`면 buy-count +2 차단 + `target_exceeded` reason + 최종 buy→hold 강등. `derived.py::_score_action`에 동일 포트 (RULE_VERSION v1→v2, services→mcp_server import 금지 유지).
  - 테스트 fixture 시한폭탄 제거 (날짜 상대화/now 주입).

  ## Live 실측 근거 (2026-06-10)
  - 031330: 의견 2건(2015/2019)만 존재 → avg 2,700 vs 15,360 (-82%) → 윈도우 후 None.
  - 005880: 8개 목표가 중 6개가 10:1 액면분할(2020-10-12 KIS 무수정주가 18,100→1,725) 이전 스케일 → avg 20,200/+964% → 윈도우 후 3,000/+56.7%.
  - 475150: 전부 최근 의견 8 buy/0 sell, avg 32,625 vs 44,350 (-26.44%)인데 'buy' → 강등.

  ## Migration
  없음 (코드-only).

  ## Test plan
  - [ ] `make lint` clean
  - [ ] `uv run pytest tests/ -m "not live"` green
  - [ ] operator 읽기 전용 라이브 스모크 (아래)

  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  EOF
  )"
  ```

- [ ] **8.6 Operator 검증 절차 (읽기 전용 라이브 스모크 — 머지 후 Linear 코멘트로 증거 게시)**:
  1. MCP `get_investment_opinions(symbol="031330", market="kr")` → `consensus.avg_target_price == null`, `rows_used == 0`, `rows_excluded_stale >= 1`, `newest_opinion_date == "2019-12-27"` 확인 (더 이상 -82% upside 없음).
  2. MCP `get_investment_opinions(symbol="005880", market="kr")` → `avg_target_price ≈ 3000` (20,200 아님), `rows_excluded_stale >= 5`, `window_months == 12` 확인.
  3. MCP `get_investment_opinions(symbol="005880", market="kr", opinion_window_months=60)` → 더 많은 행 생존 + `window_months == 60` 확인 (파라미터 동작).
  4. MCP `analyze_stock(symbol="475150", market="kr")` → `recommendation.action != "buy"`이고 reasoning에 `target_exceeded` 포함 확인 (단, RSI 등 시장 상황에 따라 upside가 -10% 위로 회복됐다면 해당 시점 실측 upside로 판단).
  5. `/invest` 주식 상세(005880) 리서치 컨센서스 패널 → 도구와 동일한 windowed 값 노출 확인.
  6. US 무회귀: `get_investment_opinions(symbol="AAPL", market="us")` → 기존 형태 그대로 (벤더 컨센서스, 윈도우 메타데이터 없음).
  7. 모든 스모크는 read-only (시세/스크랩 조회만, 주문/감시 mutation 없음).

- [ ] **8.7 Linear 갱신**: ROB-486에 PR 링크 + 전제 정정(KIS TR → Naver 스크랩, corporate-action Non-goal) 코멘트. operator 스모크 완료 전까지 Done 전환 금지.
