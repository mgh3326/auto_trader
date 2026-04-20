# naver_finance.py 리팩토링 — 서브 파서 분리 및 코드 재배치

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `_parse_valuation_from_soups` 함수를 기능별 서브 파서 4개로 분리하고, 파일 전체를 논리적 섹션 순서로 재배치하여 가독성을 개선한다.

**Architecture:** `_parse_valuation_from_soups`의 로직을 `_parse_basic_info`, `_parse_financial_metrics` 두 서브 파서로 분리하고, `fetch_company_profile`에서 `_parse_industry_info`를, `fetch_sector_peers`에서 `_parse_peer_comparison`을 추출한다. 모든 변경은 `naver_finance.py` 단일 파일 내에서 수행하며, 기존 테스트를 회귀 검증 수단으로 활용한다.

**Tech Stack:** Python 3.13, BeautifulSoup4, pytest, pytest-asyncio

---

## 현재 파일 구조 분석

```
app/services/naver_finance.py (1,098줄)

Lines 1-46:     Imports, Constants
Lines 53-97:    _parse_naver_date()
Lines 105-135:  _fetch_html(), _decode_html_content(), _fetch_html_with_client()
Lines 138-151:  _extract_current_price_from_main_soup()
Lines 154-187:  _parse_news_soup()
Lines 190-208:  _parse_report_detail_soup()
Lines 211-262:  _collect_opinion_report_infos()
Lines 265-307:  _build_investment_opinions_from_company_list_soup()
Lines 310-397:  _parse_valuation_from_soups()           ← 분리 대상
Lines 405-420:  fetch_news()
Lines 428-515:  fetch_company_profile()                  ← _parse_industry_info 추출
Lines 523-604:  fetch_financials()
Lines 612-690:  fetch_investor_trends()
Lines 698-715:  _fetch_report_detail(), _fetch_report_detail_with_client()
Lines 718-732:  _fetch_current_price()
Lines 735-763:  fetch_investment_opinions()
Lines 766-837:  _fetch_kr_snapshot()
Lines 845-865:  fetch_valuation()
Lines 872-901:  NAVER_MOBILE_API, _parse_total_infos()
Lines 904-947:  _fetch_integration()
Lines 950-1045: fetch_sector_peers()                     ← _parse_peer_comparison 추출
Lines 1048-1098: _fetch_sector_stock_codes(), _fetch_sector_name()
```

## 목표 파일 구조

```
app/services/naver_finance.py

# ── Section 1: Imports & Constants ──────────────────────────
#   (변경 없음)

# ── Section 2: Utility Helpers ──────────────────────────────
#   _parse_naver_date()

# ── Section 3: HTTP Fetch Layer ─────────────────────────────
#   _fetch_html(), _decode_html_content(), _fetch_html_with_client()

# ── Section 4: Atomic Sub-Parsers (순수 파싱, I/O 없음) ────
#   _extract_current_price_from_main_soup()
#   _parse_basic_info()              ← NEW
#   _parse_financial_metrics()       ← NEW
#   _parse_industry_info()           ← NEW
#   _parse_peer_comparison()         ← NEW
#   _parse_news_soup()
#   _parse_report_detail_soup()
#   _collect_opinion_report_infos()
#   _parse_total_infos()

# ── Section 5: Composite Parsers ────────────────────────────
#   _build_investment_opinions_from_company_list_soup()
#   _parse_valuation_from_soups()    ← 리팩토링: sub-parser 호출

# ── Section 6: Internal Fetch Helpers ───────────────────────
#   _fetch_report_detail()
#   _fetch_report_detail_with_client()
#   _fetch_current_price()
#   _fetch_integration()
#   _fetch_sector_stock_codes()
#   _fetch_sector_name()
#   _fetch_kr_snapshot()

# ── Section 7: Public API (논리적 흐름 순서) ────────────────
#   fetch_company_profile()          ← 리팩토링: sub-parser 호출
#   fetch_valuation()
#   fetch_financials()
#   fetch_news()
#   fetch_investor_trends()
#   fetch_investment_opinions()
#   fetch_sector_peers()             ← 리팩토링: sub-parser 호출
```

---

## Task 1: 기존 테스트 통과 확인 (baseline)

**Files:**
- Read: `tests/test_naver_finance.py`

- [ ] **Step 1: 현재 테스트 실행**

Run: `uv run pytest tests/test_naver_finance.py -v`
Expected: 모든 테스트 PASS (baseline 확인)

- [ ] **Step 2: Commit baseline (선택)**

건너뛰어도 됨 — 현재 브랜치가 clean 상태이므로.

---

## Task 2: `_parse_basic_info` 서브 파서 추출

**Files:**
- Modify: `app/services/naver_finance.py` (Section 4에 함수 추가)
- Modify: `tests/test_naver_finance.py` (새 테스트 클래스 추가)

- [ ] **Step 1: 테스트 작성**

`tests/test_naver_finance.py`의 `TestParseNaverDate` 클래스 뒤에 추가:

```python
class TestParseBasicInfo:
    """Tests for _parse_basic_info sub-parser."""

    def test_extracts_name_and_price(self) -> None:
        soup = BeautifulSoup(SAMPLE_VALUATION_MAIN_HTML, "lxml")
        result = naver_finance._parse_basic_info(soup)
        assert result["name"] == "삼성전자"
        assert result["current_price"] == 75000

    def test_missing_name(self) -> None:
        soup = BeautifulSoup("<html></html>", "lxml")
        result = naver_finance._parse_basic_info(soup)
        assert result["name"] is None
        assert result["current_price"] is None

    def test_fallback_price_parsing(self) -> None:
        soup = BeautifulSoup(SAMPLE_VALUATION_MINIMAL_MAIN_HTML, "lxml")
        result = naver_finance._parse_basic_info(soup)
        assert result["name"] == "효성중공업"
        assert result["current_price"] == 450000
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_naver_finance.py::TestParseBasicInfo -v`
Expected: FAIL — `_parse_basic_info` 함수 없음

- [ ] **Step 3: 구현**

`app/services/naver_finance.py`에서 `_extract_current_price_from_main_soup` 함수 바로 뒤에 추가:

```python
def _parse_basic_info(main_soup: BeautifulSoup) -> dict[str, Any]:
    """Extract company name and current price from the main page soup."""
    info: dict[str, Any] = {"name": None, "current_price": None}
    name_elem = main_soup.select_one("div.wrap_company h2 a")
    if name_elem:
        info["name"] = name_elem.get_text(strip=True)
    info["current_price"] = _extract_current_price_from_main_soup(main_soup)
    return info
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_naver_finance.py::TestParseBasicInfo -v`
Expected: 3 PASSED

- [ ] **Step 5: 기존 테스트 회귀 확인**

Run: `uv run pytest tests/test_naver_finance.py -v`
Expected: 전체 PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/naver_finance.py tests/test_naver_finance.py
git commit -m "refactor(naver): extract _parse_basic_info sub-parser"
```

---

## Task 3: `_parse_financial_metrics` 서브 파서 추출

**Files:**
- Modify: `app/services/naver_finance.py` (Section 4에 함수 추가)
- Modify: `tests/test_naver_finance.py` (새 테스트 클래스 추가)

- [ ] **Step 1: 테스트 작성**

`tests/test_naver_finance.py`의 `TestParseBasicInfo` 클래스 뒤에 추가:

```python
class TestParseFinancialMetrics:
    """Tests for _parse_financial_metrics sub-parser."""

    def test_extracts_all_metrics(self) -> None:
        soup = BeautifulSoup(SAMPLE_VALUATION_MAIN_HTML, "lxml")
        result = naver_finance._parse_financial_metrics(soup)
        assert result["per"] == 12.5
        assert result["pbr"] == 1.2
        assert result["roe"] == 18.5
        assert result["roe_controlling"] == 17.2
        assert abs(result["dividend_yield"] - 0.02) < 0.001

    def test_skips_zero_per(self) -> None:
        html = '<html><body><em id="_per">0</em></body></html>'
        soup = BeautifulSoup(html, "lxml")
        result = naver_finance._parse_financial_metrics(soup)
        assert result["per"] is None

    def test_skips_na_per(self) -> None:
        soup = BeautifulSoup(SAMPLE_VALUATION_MINIMAL_MAIN_HTML, "lxml")
        result = naver_finance._parse_financial_metrics(soup)
        assert result["per"] is None
        assert result["pbr"] == 2.1
        assert result["roe"] is None
        assert result["dividend_yield"] is None

    def test_empty_html(self) -> None:
        soup = BeautifulSoup("<html></html>", "lxml")
        result = naver_finance._parse_financial_metrics(soup)
        assert result["per"] is None
        assert result["pbr"] is None
        assert result["roe"] is None
        assert result["roe_controlling"] is None
        assert result["dividend_yield"] is None
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_naver_finance.py::TestParseFinancialMetrics -v`
Expected: FAIL — `_parse_financial_metrics` 함수 없음

- [ ] **Step 3: 구현**

`app/services/naver_finance.py`에서 `_parse_basic_info` 함수 바로 뒤에 추가:

```python
def _parse_financial_metrics(main_soup: BeautifulSoup) -> dict[str, Any]:
    """Extract PER, PBR, ROE, and dividend yield from the main page soup."""
    metrics: dict[str, Any] = {
        "per": None,
        "pbr": None,
        "roe": None,
        "roe_controlling": None,
        "dividend_yield": None,
    }

    per_elem = main_soup.select_one("em#_per")
    if per_elem:
        per_val = _parse_korean_number(per_elem.get_text(strip=True))
        if per_val is not None and per_val != 0:
            metrics["per"] = per_val

    pbr_elem = main_soup.select_one("em#_pbr")
    if pbr_elem:
        pbr_val = _parse_korean_number(pbr_elem.get_text(strip=True))
        if pbr_val is not None and pbr_val != 0:
            metrics["pbr"] = pbr_val

    dvr_elem = main_soup.select_one("em#_dvr")
    if dvr_elem:
        dvr_val = _parse_korean_number(dvr_elem.get_text(strip=True))
        if dvr_val is not None:
            metrics["dividend_yield"] = dvr_val / 100

    for row in main_soup.select("tr"):
        th = row.select_one("th")
        if not th:
            continue
        th_text = th.get_text(strip=True)
        if not th_text.startswith("ROE"):
            continue
        tds = row.select("td")
        if not tds:
            continue
        roe_val = _parse_korean_number(tds[0].get_text(strip=True))
        if roe_val is None:
            continue
        if "ROE(%)" in th_text:
            metrics["roe"] = roe_val
        elif "지배" in th_text:
            metrics["roe_controlling"] = roe_val

    return metrics
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_naver_finance.py::TestParseFinancialMetrics -v`
Expected: 4 PASSED

- [ ] **Step 5: 기존 테스트 회귀 확인**

Run: `uv run pytest tests/test_naver_finance.py -v`
Expected: 전체 PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/naver_finance.py tests/test_naver_finance.py
git commit -m "refactor(naver): extract _parse_financial_metrics sub-parser"
```

---

## Task 4: `_parse_industry_info` 서브 파서 추출

**Files:**
- Modify: `app/services/naver_finance.py` (Section 4에 함수 추가)
- Modify: `tests/test_naver_finance.py` (새 테스트 클래스 추가)

- [ ] **Step 1: 테스트 작성**

`tests/test_naver_finance.py`의 `TestParseFinancialMetrics` 클래스 뒤에 추가:

```python
class TestParseIndustryInfo:
    """Tests for _parse_industry_info sub-parser."""

    def test_extracts_exchange_and_sector(self) -> None:
        soup = BeautifulSoup(SAMPLE_PROFILE_HTML, "lxml")
        result = naver_finance._parse_industry_info(soup)
        assert result["exchange"] == "KOSPI"
        assert result["sector"] == "전기전자"

    def test_kosdaq_exchange(self) -> None:
        html = '<html><body><div class="code">123456 코스닥</div></body></html>'
        soup = BeautifulSoup(html, "lxml")
        result = naver_finance._parse_industry_info(soup)
        assert result["exchange"] == "KOSDAQ"
        assert result["sector"] is None

    def test_empty_html(self) -> None:
        soup = BeautifulSoup("<html></html>", "lxml")
        result = naver_finance._parse_industry_info(soup)
        assert result["exchange"] is None
        assert result["sector"] is None
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_naver_finance.py::TestParseIndustryInfo -v`
Expected: FAIL — `_parse_industry_info` 함수 없음

- [ ] **Step 3: 구현**

`app/services/naver_finance.py`에서 `_parse_financial_metrics` 함수 바로 뒤에 추가:

```python
def _parse_industry_info(soup: BeautifulSoup) -> dict[str, Any]:
    """Extract exchange type and sector from the main page soup."""
    info: dict[str, Any] = {"exchange": None, "sector": None}

    code_info = soup.select_one("div.code")
    if code_info:
        code_text = code_info.get_text(strip=True)
        if "코스피" in code_text:
            info["exchange"] = "KOSPI"
        elif "코스닥" in code_text:
            info["exchange"] = "KOSDAQ"

    sector_elem = soup.select_one("div.tab_con1 em a")
    if sector_elem:
        info["sector"] = sector_elem.get_text(strip=True)

    return info
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_naver_finance.py::TestParseIndustryInfo -v`
Expected: 3 PASSED

- [ ] **Step 5: 기존 테스트 회귀 확인**

Run: `uv run pytest tests/test_naver_finance.py -v`
Expected: 전체 PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/naver_finance.py tests/test_naver_finance.py
git commit -m "refactor(naver): extract _parse_industry_info sub-parser"
```

---

## Task 5: `_parse_peer_comparison` 서브 파서 추출

**Files:**
- Modify: `app/services/naver_finance.py` (Section 4에 함수 추가)
- Modify: `tests/test_naver_finance.py` (새 테스트 클래스 추가)

- [ ] **Step 1: 테스트 작성**

`tests/test_naver_finance.py`의 `TestParseIndustryInfo` 클래스 뒤에 추가:

```python
class TestParsePeerComparison:
    """Tests for _parse_peer_comparison sub-parser."""

    def test_builds_sorted_peer_list(self) -> None:
        raw = [
            {
                "symbol": "AAA",
                "name": "Small",
                "current_price": 1000,
                "change_pct": 1.0,
                "per": 10.0,
                "pbr": 1.0,
                "market_cap": 100,
            },
            {
                "symbol": "BBB",
                "name": "Big",
                "current_price": 5000,
                "change_pct": -0.5,
                "per": 15.0,
                "pbr": 2.0,
                "market_cap": 999,
            },
        ]
        result = naver_finance._parse_peer_comparison(raw, limit=5)
        assert len(result) == 2
        assert result[0]["symbol"] == "BBB"  # market_cap 999 first
        assert result[1]["symbol"] == "AAA"

    def test_none_entries_skipped(self) -> None:
        raw = [
            None,
            {
                "symbol": "CCC",
                "name": "Only",
                "current_price": 2000,
                "change_pct": 0.0,
                "per": 8.0,
                "pbr": 0.5,
                "market_cap": 50,
            },
            None,
        ]
        result = naver_finance._parse_peer_comparison(raw, limit=5)
        assert len(result) == 1
        assert result[0]["symbol"] == "CCC"

    def test_limit_applied(self) -> None:
        raw = [
            {
                "symbol": f"S{i}",
                "name": f"Stock{i}",
                "current_price": 1000 * i,
                "change_pct": 0.0,
                "per": 10.0,
                "pbr": 1.0,
                "market_cap": 100 * i,
            }
            for i in range(1, 6)
        ]
        result = naver_finance._parse_peer_comparison(raw, limit=3)
        assert len(result) == 3
        # Top 3 by market_cap: S5(500), S4(400), S3(300)
        assert [p["symbol"] for p in result] == ["S5", "S4", "S3"]

    def test_none_market_cap_sorted_last(self) -> None:
        raw = [
            {
                "symbol": "X",
                "name": "NoMcap",
                "current_price": 1000,
                "change_pct": 0.0,
                "per": None,
                "pbr": None,
                "market_cap": None,
            },
            {
                "symbol": "Y",
                "name": "HasMcap",
                "current_price": 2000,
                "change_pct": 0.0,
                "per": 5.0,
                "pbr": 1.0,
                "market_cap": 200,
            },
        ]
        result = naver_finance._parse_peer_comparison(raw, limit=5)
        assert result[0]["symbol"] == "Y"
        assert result[1]["symbol"] == "X"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_naver_finance.py::TestParsePeerComparison -v`
Expected: FAIL — `_parse_peer_comparison` 함수 없음

- [ ] **Step 3: 구현**

`app/services/naver_finance.py`에서 `_parse_industry_info` 함수 바로 뒤에 추가:

```python
def _parse_peer_comparison(
    peer_results: list[dict[str, Any] | None],
    limit: int,
) -> list[dict[str, Any]]:
    """Build a sorted peer list from raw integration fetch results.

    Filters out ``None`` entries, picks the display fields, sorts by
    market-cap descending (``None`` last), and trims to *limit*.
    """
    peers: list[dict[str, Any]] = []
    for pr in peer_results:
        if pr is None:
            continue
        peers.append(
            {
                "symbol": pr["symbol"],
                "name": pr["name"],
                "current_price": pr["current_price"],
                "change_pct": pr["change_pct"],
                "per": pr["per"],
                "pbr": pr["pbr"],
                "market_cap": pr["market_cap"],
            }
        )
    peers.sort(key=lambda x: x.get("market_cap") or 0, reverse=True)
    return peers[:limit]
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_naver_finance.py::TestParsePeerComparison -v`
Expected: 4 PASSED

- [ ] **Step 5: 기존 테스트 회귀 확인**

Run: `uv run pytest tests/test_naver_finance.py -v`
Expected: 전체 PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/naver_finance.py tests/test_naver_finance.py
git commit -m "refactor(naver): extract _parse_peer_comparison sub-parser"
```

---

## Task 6: `_parse_valuation_from_soups`를 서브 파서 호출로 리팩토링

**Files:**
- Modify: `app/services/naver_finance.py:310-397` — 함수 본문 교체

- [ ] **Step 1: 함수 본문 교체**

`_parse_valuation_from_soups` 함수를 다음으로 교체:

```python
def _parse_valuation_from_soups(
    code: str,
    main_soup: BeautifulSoup,
    sise_soup: BeautifulSoup,
) -> dict[str, Any]:
    basic = _parse_basic_info(main_soup)
    metrics = _parse_financial_metrics(main_soup)

    valuation: dict[str, Any] = {
        "symbol": code,
        "name": basic["name"],
        "current_price": basic["current_price"],
        **metrics,
        "high_52w": None,
        "low_52w": None,
        "current_position_52w": None,
    }

    for row in sise_soup.select("tr"):
        cells = row.select("th, td")
        for i, cell in enumerate(cells):
            label = cell.get_text(strip=True)
            if "52주 최고" in label or "52주최고" in label:
                if i + 1 < len(cells):
                    high_val = _parse_korean_number(cells[i + 1].get_text(strip=True))
                    if high_val:
                        valuation["high_52w"] = int(high_val)
            elif "52주 최저" in label or "52주최저" in label:
                if i + 1 < len(cells):
                    low_val = _parse_korean_number(cells[i + 1].get_text(strip=True))
                    if low_val:
                        valuation["low_52w"] = int(low_val)

    if (
        valuation["current_price"] is not None
        and valuation["high_52w"] is not None
        and valuation["low_52w"] is not None
    ):
        high = valuation["high_52w"]
        low = valuation["low_52w"]
        current = valuation["current_price"]
        if high > low:
            valuation["current_position_52w"] = round(
                (current - low) / (high - low), 2
            )

    return valuation
```

- [ ] **Step 2: 기존 Valuation 테스트 통과 확인**

Run: `uv run pytest tests/test_naver_finance.py::TestFetchValuation -v`
Expected: 전체 PASS (동작 불변)

- [ ] **Step 3: 전체 회귀 테스트**

Run: `uv run pytest tests/test_naver_finance.py -v`
Expected: 전체 PASS

- [ ] **Step 4: Commit**

```bash
git add app/services/naver_finance.py
git commit -m "refactor(naver): wire _parse_valuation_from_soups to sub-parsers"
```

---

## Task 7: `fetch_company_profile`을 `_parse_basic_info` + `_parse_industry_info` 호출로 리팩토링

**Files:**
- Modify: `app/services/naver_finance.py:428-515` — 인라인 파싱을 서브 파서 호출로 교체

- [ ] **Step 1: 함수 본문 교체**

`fetch_company_profile` 함수를 다음으로 교체:

```python
async def fetch_company_profile(code: str) -> dict[str, Any]:
    """Fetch company profile from Naver Finance.

    URL: finance.naver.com/item/main.naver?code={code}

    Args:
        code: 6-digit Korean stock code

    Returns:
        Company profile with name, sector, market_cap, exchange, etc.
    """
    url = f"{NAVER_FINANCE_ITEM}/main.naver"
    soup = await _fetch_html(url, params={"code": code})

    basic = _parse_basic_info(soup)
    industry = _parse_industry_info(soup)

    profile: dict[str, Any] = {
        "symbol": code,
        "name": basic["name"],
        "sector": industry["sector"],
        "industry": None,
        "market_cap": None,
        "shares_outstanding": None,
        "per": None,
        "pbr": None,
        "eps": None,
        "bps": None,
        "dividend_yield": None,
        "exchange": industry["exchange"],
        "website": None,
    }

    # Parse summary table with key metrics
    for table in soup.select("table.no_info, table.tb_type1"):
        for row in table.select("tr"):
            cells = row.select("th, td")
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True)
                value_elem = cells[1]

                em = value_elem.select_one("em")
                value = (
                    em.get_text(strip=True) if em else value_elem.get_text(strip=True)
                )

                if "시가총액" in label:
                    profile["market_cap"] = _parse_korean_number(value)
                elif "상장주식수" in label:
                    profile["shares_outstanding"] = _parse_korean_number(value)
                elif label == "PER":
                    profile["per"] = _parse_korean_number(value)
                elif label == "PBR":
                    profile["pbr"] = _parse_korean_number(value)
                elif label == "EPS":
                    profile["eps"] = _parse_korean_number(value)
                elif label == "BPS":
                    profile["bps"] = _parse_korean_number(value)
                elif "배당수익률" in label:
                    profile["dividend_yield"] = _parse_korean_number(value)

    # Try to get market cap from _market_sum element
    market_sum_elem = soup.select_one("em#_market_sum")
    if market_sum_elem and profile["market_cap"] is None:
        profile["market_cap"] = _parse_korean_number(
            market_sum_elem.get_text(strip=True)
        )

    # Filter out None values
    return {k: v for k, v in profile.items() if v is not None}
```

- [ ] **Step 2: CompanyProfile 테스트 통과 확인**

Run: `uv run pytest tests/test_naver_finance.py::TestFetchCompanyProfile -v`
Expected: 전체 PASS

- [ ] **Step 3: 전체 회귀 테스트**

Run: `uv run pytest tests/test_naver_finance.py -v`
Expected: 전체 PASS

- [ ] **Step 4: Commit**

```bash
git add app/services/naver_finance.py
git commit -m "refactor(naver): wire fetch_company_profile to sub-parsers"
```

---

## Task 8: `fetch_sector_peers`를 `_parse_peer_comparison` 호출로 리팩토링

**Files:**
- Modify: `app/services/naver_finance.py` — `fetch_sector_peers` 내 인라인 peer 빌드 로직을 서브 파서 호출로 교체

- [ ] **Step 1: 인라인 peer 빌드 블록 교체**

`fetch_sector_peers` 함수에서 `# ---- Build peer list ----` 주석 아래 블록(lines ~1013-1032)을 교체:

**Before (삭제 대상):**
```python
    # ---- Build peer list ----
    peers: list[dict[str, Any]] = []
    for pr in peer_results:
        if pr is None:
            continue
        peers.append(
            {
                "symbol": pr["symbol"],
                "name": pr["name"],
                "current_price": pr["current_price"],
                "change_pct": pr["change_pct"],
                "per": pr["per"],
                "pbr": pr["pbr"],
                "market_cap": pr["market_cap"],
            }
        )

    # Sort by market_cap desc (None last)
    peers.sort(key=lambda x: x.get("market_cap") or 0, reverse=True)
    peers = peers[:limit]
```

**After (교체):**
```python
    peers = _parse_peer_comparison(peer_results, limit)
```

- [ ] **Step 2: 전체 회귀 테스트**

Run: `uv run pytest tests/test_naver_finance.py -v`
Expected: 전체 PASS

- [ ] **Step 3: Commit**

```bash
git add app/services/naver_finance.py
git commit -m "refactor(naver): wire fetch_sector_peers to _parse_peer_comparison"
```

---

## Task 9: 파일 전체 섹션 재배치

**Files:**
- Modify: `app/services/naver_finance.py` — 함수 순서를 목표 구조에 맞게 재배치

이 태스크는 함수 본문을 변경하지 않고, 함수 정의의 **위치**만 이동합니다.

- [ ] **Step 1: 섹션 배너 주석 + 함수 순서 재배치**

파일 전체를 목표 구조(이 플랜 상단의 "목표 파일 구조")에 맞게 재배치합니다. 각 섹션에 배너 주석을 추가합니다:

```python
# ---------------------------------------------------------------------------
# Section 4: Atomic Sub-Parsers
# ---------------------------------------------------------------------------
```

이동이 필요한 함수들:

| 함수 | 현재 위치 | 목표 Section |
|------|-----------|-------------|
| `NAVER_MOBILE_API` (상수) | Section 7 부근 (line 872) | Section 1 (상수 영역) |
| `_parse_total_infos` | Section 7 부근 (line 875) | Section 4 (Atomic Sub-Parsers) |
| `_fetch_report_detail` | Section 7 직전 (line 698) | Section 6 (Internal Fetch Helpers) |
| `_fetch_report_detail_with_client` | Section 7 직전 (line 707) | Section 6 |
| `_fetch_current_price` | Section 7 직전 (line 718) | Section 6 |
| `_fetch_integration` | Section 7 부근 (line 904) | Section 6 |
| `_fetch_sector_stock_codes` | 파일 끝 (line 1048) | Section 6 |
| `_fetch_sector_name` | 파일 끝 (line 1078) | Section 6 |
| `_fetch_kr_snapshot` | Section 7 사이 (line 766) | Section 6 |
| `fetch_company_profile` | 2번째 public (line 428) | Section 7 첫 번째 |
| `fetch_valuation` | 6번째 public (line 845) | Section 7 두 번째 |
| `fetch_news` | 1번째 public (line 405) | Section 7 네 번째 |

**Public API 최종 순서:**
1. `fetch_company_profile` — 종목 기본 정보
2. `fetch_valuation` — 밸류에이션 지표
3. `fetch_financials` — 재무제표
4. `fetch_news` — 뉴스
5. `fetch_investor_trends` — 투자자 동향
6. `fetch_investment_opinions` — 증권사 의견
7. `fetch_sector_peers` — 동종업계 비교

- [ ] **Step 2: 전체 회귀 테스트**

Run: `uv run pytest tests/test_naver_finance.py -v`
Expected: 전체 PASS

- [ ] **Step 3: lint 확인**

Run: `uv run ruff check app/services/naver_finance.py`
Expected: 에러 없음

- [ ] **Step 4: Commit**

```bash
git add app/services/naver_finance.py
git commit -m "refactor(naver): reorganize file sections in logical flow order"
```

---

## Task 10: 최종 검증

**Files:**
- Read: `app/services/naver_finance.py` (전체 구조 확인)

- [ ] **Step 1: 전체 naver 테스트 실행**

Run: `uv run pytest tests/ -k "naver" -v`
Expected: 전체 PASS

- [ ] **Step 2: lint + format 확인**

Run: `uv run ruff check app/services/naver_finance.py && uv run ruff format --check app/services/naver_finance.py`
Expected: 에러 없음

- [ ] **Step 3: 새 서브 파서 4개 존재 확인**

Run: `grep -n "^def _parse_basic_info\|^def _parse_financial_metrics\|^def _parse_industry_info\|^def _parse_peer_comparison" app/services/naver_finance.py`
Expected: 4개 함수 모두 출력

- [ ] **Step 4: 섹션 배너 확인**

Run: `grep -n "^# .* Section" app/services/naver_finance.py`
Expected: Section 1~7 배너가 순서대로 출력
