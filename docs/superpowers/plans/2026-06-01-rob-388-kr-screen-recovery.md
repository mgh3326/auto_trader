# ROB-388 KR `screen_stocks` 개장 경로 복구 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 개장 시점 `screen_stocks(market="kr")`의 두 결함(trade_amount false affordance, KRX session-expired raw 예외)을 좁게 수정해 KR 발굴 entrypoint를 복구한다.

**Architecture:** (1) KR 종목이 이미 보유한 `value`(ACC_TRDVAL=거래대금) 필드를 `trade_amount`로 노출하고 validation을 market-aware로 바꿔 KR `trade_amount` 정렬을 실제 지원한다(US는 actionable error). (2) KRX 재인증-후-LOGOUT를 전용 예외 `KRXSessionExpiredError`로 분류하고, KR 스크리닝 경계에서 catch해 raw 예외 대신 `data_state="unavailable"`/`retryable=true`/warning 구조화 신호를 반환한다.

**Tech Stack:** Python 3.13, pytest (`uv run pytest`), httpx (MockTransport), 기존 `app/mcp_server/tooling/screening/*` + `app/services/krx.py`.

**Spec:** `docs/superpowers/specs/2026-06-01-rob-388-kr-screen-recovery-design.md`

---

## File Structure

- **Modify** `app/services/krx.py` — `KRXSessionExpiredError` 예외 정의 + `fetch_data` 재인증-후-LOGOUT 분기에서 raise.
- **Modify** `app/mcp_server/tooling/screening/common.py` — `_validate_screen_filters` market-aware `trade_amount`; `_sort_and_limit` trade_amount fallback.
- **Modify** `app/mcp_server/tooling/screening/kr.py` — legacy `_screen_kr` 후보에 `trade_amount=value` 노출; `_normalize_kr_results`(tvscreener)에 `trade_amount=base.value`; `_screen_kr_with_fallback`에서 `KRXSessionExpiredError` catch → 구조화 응답.
- **Modify** `app/mcp_server/tooling/analysis_registration.py` — `screen_stocks` description에 trade_amount 시장 지원 범위 1줄.
- **Test** `tests/test_services_krx.py`, `tests/test_screening_common.py`, `tests/test_screening_kr.py` — 기존 파일에 추가.

> 실행 시 모든 명령은 worktree `/Users/mgh3326/work/auto_trader.rob-388`에서 `uv run` 으로 수행한다.

---

## Task 1: KRX session-expired 전용 예외 (`KRXSessionExpiredError`)

**Files:**
- Modify: `app/services/krx.py` (예외 정의 + `fetch_data` 라인 ~229-238)
- Test: `tests/test_services_krx.py`

- [ ] **Step 1: Write the failing test**

`tests/test_services_krx.py` 끝에 추가:

```python
class TestKRXSessionExpired:
    """fetch_data raises a typed, classifiable error after re-auth LOGOUT."""

    async def test_fetch_data_raises_typed_error_after_reauth_logout(self, monkeypatch):
        import httpx

        from app.services.krx import KRXSessionExpiredError, KRXSessionManager

        def handler(request: httpx.Request) -> httpx.Response:
            # Always reply 400 + LOGOUT body to force re-auth then failure.
            return httpx.Response(400, text="LOGOUT")

        manager = KRXSessionManager()
        manager._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        manager._authenticated = True  # skip real login in _ensure_session

        async def _noop_login() -> None:
            manager._authenticated = True

        monkeypatch.setattr(manager, "_login", _noop_login)

        with pytest.raises(KRXSessionExpiredError):
            await manager.fetch_data(bld="dummy/bld")

        await manager.close()

    def test_session_expired_error_is_httpx_status_error(self):
        import httpx

        from app.services.krx import KRXSessionExpiredError

        assert issubclass(KRXSessionExpiredError, httpx.HTTPStatusError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_services_krx.py::TestKRXSessionExpired -v`
Expected: FAIL with `ImportError: cannot import name 'KRXSessionExpiredError'`.

- [ ] **Step 3: Define the exception**

`app/services/krx.py`, `import httpx` 직후(모듈 상단, 클래스 정의 전)에 추가:

```python
class KRXSessionExpiredError(httpx.HTTPStatusError):
    """KRX session stayed logged out after a re-auth attempt.

    Subclasses ``httpx.HTTPStatusError`` so existing ``except httpx.HTTPStatusError``
    callers keep working, while new callers can classify this as a transient,
    retryable KRX-session failure (vs. a generic HTTP error).
    """
```

- [ ] **Step 4: Raise the typed error at the re-auth-LOGOUT branch**

`app/services/krx.py` `fetch_data` 내부, 기존 raise(라인 ~234)를 교체:

```python
                if response.status_code == 400 and "LOGOUT" in response.text:
                    logger.error(
                        "KRX 재인증 후에도 LOGOUT 응답 (login_code=%s)",
                        self._last_login_code or "unknown",
                    )
                    raise KRXSessionExpiredError(
                        "KRX session expired after re-auth",
                        request=response.request,
                        response=response,
                    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_services_krx.py::TestKRXSessionExpired -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add app/services/krx.py tests/test_services_krx.py
git commit -m "feat(ROB-388): classify KRX re-auth LOGOUT as KRXSessionExpiredError

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: KR `trade_amount` validation을 market-aware로

**Files:**
- Modify: `app/mcp_server/tooling/screening/common.py` (`_validate_screen_filters`, 라인 ~576-580)
- Test: `tests/test_screening_common.py`

- [ ] **Step 1: Write the failing test**

`tests/test_screening_common.py` 끝에 추가:

```python
class TestTradeAmountValidation:
    """trade_amount sorting is valid for KR/crypto, rejected for US with guidance."""

    @pytest.mark.parametrize("market", ["kr", "kospi", "kosdaq", "konex", "all"])
    def test_trade_amount_allowed_for_kr(self, market):
        from app.mcp_server.tooling.screening.common import _validate_screen_filters

        # Should not raise.
        _validate_screen_filters(
            market=market,
            asset_type="stock",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="trade_amount",
        )

    def test_trade_amount_rejected_for_us_with_actionable_message(self):
        from app.mcp_server.tooling.screening.common import _validate_screen_filters

        with pytest.raises(ValueError) as exc:
            _validate_screen_filters(
                market="us",
                asset_type="stock",
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="trade_amount",
            )
        message = str(exc.value)
        assert "volume" in message  # points the caller at a supported US sort key
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screening_common.py::TestTradeAmountValidation -v`
Expected: FAIL — KR cases raise `ValueError("'trade_amount' sorting is only supported for crypto market")`.

- [ ] **Step 3: Make the validation market-aware**

`app/mcp_server/tooling/screening/common.py`, `_validate_screen_filters`의 `else:` 분기(라인 ~576-580)를 교체:

```python
    else:
        kr_markets = {"kr", "kospi", "kosdaq", "konex", "all"}
        if sort_by == "trade_amount" and market not in kr_markets:
            raise ValueError(
                "'trade_amount' sorting is supported for KR and crypto markets; "
                "for US use 'volume', 'market_cap', or 'change_rate'."
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_screening_common.py::TestTradeAmountValidation -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/common.py tests/test_screening_common.py
git commit -m "feat(ROB-388): allow trade_amount sort for KR, actionable error for US

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `_sort_and_limit`이 `trade_amount`를 KR 거래대금으로 정렬

**Files:**
- Modify: `app/mcp_server/tooling/screening/common.py` (`_sort_and_limit`, 라인 ~675-704)
- Test: `tests/test_screening_common.py`

- [ ] **Step 1: Write the failing test**

`tests/test_screening_common.py` 끝에 추가:

```python
class TestSortByTradeAmount:
    """trade_amount sort falls back from trade_amount_24h (crypto) to trade_amount (KR)."""

    def test_kr_rows_sorted_by_trade_amount_field(self):
        from app.mcp_server.tooling.screening.common import _sort_and_limit

        rows = [
            {"symbol": "A", "trade_amount": 100.0},
            {"symbol": "C", "trade_amount": 300.0},
            {"symbol": "B", "trade_amount": 200.0},
        ]
        ordered = _sort_and_limit(rows, "trade_amount", "desc", 10)
        assert [r["symbol"] for r in ordered] == ["C", "B", "A"]

    def test_crypto_rows_sorted_by_trade_amount_24h(self):
        from app.mcp_server.tooling.screening.common import _sort_and_limit

        rows = [
            {"symbol": "A", "trade_amount_24h": 10.0},
            {"symbol": "B", "trade_amount_24h": 30.0},
        ]
        ordered = _sort_and_limit(rows, "trade_amount", "desc", 10)
        assert [r["symbol"] for r in ordered] == ["B", "A"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screening_common.py::TestSortByTradeAmount -v`
Expected: FAIL — `test_kr_rows_sorted_by_trade_amount_field` fails (current map only reads `trade_amount_24h`, so KR rows all sort as 0 and order is unchanged `A, C, B`).

- [ ] **Step 3: Add a fallback chain for trade_amount in `_sort_and_limit`**

`app/mcp_server/tooling/screening/common.py`, `_sort_and_limit`의 `sort_value` 함수를 교체(`field = sort_field_map.get(...)` 라인은 유지):

```python
    field = sort_field_map.get(sort_by, "volume")
    reverse = sort_order == "desc"

    def sort_value(item: dict[str, Any]) -> float:
        if sort_by == "trade_amount":
            # crypto rows expose trade_amount_24h; KR rows expose trade_amount.
            value = item.get("trade_amount_24h")
            if value is None:
                value = item.get("trade_amount")
        else:
            value = item.get(field)
        if field in {"rsi", "score"} and value is None:
            return -999.0 if reverse else 999.0
        return float(value or 0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_screening_common.py::TestSortByTradeAmount -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/common.py tests/test_screening_common.py
git commit -m "feat(ROB-388): sort trade_amount via trade_amount_24h->trade_amount fallback

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: KR 행이 `trade_amount`(=거래대금 value)를 노출

KR 종목 후보 dict가 정렬 시점에 `trade_amount` 키를 갖도록 두 경로(legacy + tvscreener)에 명시 노출한다.

**Files:**
- Modify: `app/mcp_server/tooling/screening/kr.py` (legacy `_screen_kr` 정규화 루프 ~124-145; `_normalize_kr_results` stock dict)
- Test: `tests/test_screening_kr.py`

- [ ] **Step 1: Write the failing test**

`tests/test_screening_kr.py` 끝에 추가 (legacy 경로를 KRX fetch를 fake로 대체해 검증):

```python
class TestKrTradeAmountExposed:
    """Legacy KR screening exposes trade_amount derived from KRX traded value."""

    async def test_screen_kr_sets_trade_amount_from_value(self, monkeypatch):
        from app.mcp_server.tooling.screening import kr as kr_mod

        async def fake_fetch_stock_all_cached(market: str):
            return [
                {"short_code": "000001", "code": "000001", "name": "AAA",
                 "value": 500.0, "market_cap": 10.0},
                {"short_code": "000002", "code": "000002", "name": "BBB",
                 "value": 100.0, "market_cap": 10.0},
            ]

        async def fake_fetch_etf_all_cached():
            return []

        async def fake_fetch_valuation_all_cached(market: str):
            return {}

        monkeypatch.setattr(kr_mod, "fetch_stock_all_cached", fake_fetch_stock_all_cached)
        monkeypatch.setattr(kr_mod, "fetch_etf_all_cached", fake_fetch_etf_all_cached)
        monkeypatch.setattr(
            kr_mod, "fetch_valuation_all_cached", fake_fetch_valuation_all_cached
        )

        response = await kr_mod._screen_kr(
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="trade_amount",
            sort_order="desc",
            limit=10,
        )
        results = response["results"]
        assert [r["symbol"] for r in results] == ["000001", "000002"]
        assert results[0]["trade_amount"] == 500.0
```

> 참고: `kr.py`가 실제로 import하는 심볼 이름(`fetch_stock_all_cached`, `fetch_etf_all_cached`, `fetch_valuation_all_cached`)을 monkeypatch한다. import 별칭이 다르면 `kr.py` 상단 import 블록(라인 33~)을 확인해 맞춘다. `symbol` 키는 `short_code`에서 파생된다(현재 정규화 동작과 일치).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screening_kr.py::TestKrTradeAmountExposed -v`
Expected: FAIL — `KeyError`/`None` on `results[0]["trade_amount"]` (현재 KR 행은 `trade_amount` 키를 만들지 않음).

- [ ] **Step 3: Expose trade_amount in the legacy `_screen_kr` normalization loop**

`app/mcp_server/tooling/screening/kr.py`, 후보 정규화 루프(라인 ~124-145, `for item in candidates:` 블록) 안 — `market_cap_krw` 보정 직후에 추가:

```python
        if item.get("trade_amount") is None and item.get("value") is not None:
            item["trade_amount"] = _to_optional_float(item.get("value"))
```

- [ ] **Step 4: Expose trade_amount in the tvscreener `_normalize_kr_results` stock dict**

`app/mcp_server/tooling/screening/kr.py`, `_normalize_kr_results`의 `stock` dict 구성부에 `"volume": ...` 항목 근처에 추가:

```python
            "trade_amount": _to_optional_float(base.get("value")),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_screening_kr.py::TestKrTradeAmountExposed -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/screening/kr.py tests/test_screening_kr.py
git commit -m "feat(ROB-388): expose KR trade_amount from KRX traded value (legacy + tvscreener)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: KRX session-expired를 KR 스크리닝 경계에서 구조화 신호로 변환

**Files:**
- Modify: `app/mcp_server/tooling/screening/kr.py` (`_screen_kr_with_fallback`, 라인 ~622-721; import에 `KRXSessionExpiredError` 추가)
- Test: `tests/test_screening_kr.py`

- [ ] **Step 1: Write the failing test**

`tests/test_screening_kr.py` 끝에 추가:

```python
class TestKrScreenSessionExpired:
    """KRXSessionExpiredError surfaces as a structured unavailable signal, not a raise."""

    async def test_session_expired_returns_unavailable_signal(self, monkeypatch):
        import httpx

        from app.mcp_server.tooling.screening import kr as kr_mod
        from app.services.krx import KRXSessionExpiredError

        async def fake_screen_kr(**kwargs):
            request = httpx.Request("POST", "https://example.invalid")
            response = httpx.Response(400, text="LOGOUT", request=request)
            raise KRXSessionExpiredError(
                "KRX session expired after re-auth",
                request=request,
                response=response,
            )

        # Force the legacy path and make it raise the typed error.
        monkeypatch.setattr(kr_mod, "_screen_kr", fake_screen_kr)
        monkeypatch.setattr(
            kr_mod, "_can_use_tvscreener_stock_path", lambda **kwargs: False
        )

        response = await kr_mod._screen_kr_with_fallback(
            market="kospi",
            asset_type="stock",
            category=None,
            sector=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            min_analyst_buy=None,
            max_rsi=None,
            sort_by="change_rate",
            sort_order="desc",
            limit=10,
        )

        assert response["results"] == []
        assert response["meta"]["data_state"] == "unavailable"
        assert response["meta"]["retryable"] is True
        assert response["meta"]["reason"] == "krx_session_expired"
        assert response.get("warnings")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screening_kr.py::TestKrScreenSessionExpired -v`
Expected: FAIL — `KRXSessionExpiredError` propagates out of `_screen_kr_with_fallback` (no catch yet).

- [ ] **Step 3: Import the typed error and add a structured-response helper**

`app/mcp_server/tooling/screening/kr.py` 상단 `from app.services.krx import (` 블록에 `KRXSessionExpiredError`를 추가한다. 그리고 `_screen_kr_with_fallback` 정의 바로 위에 헬퍼를 추가:

```python
def _krx_session_unavailable_response(
    market: str,
    sort_by: str,
    sort_order: str,
) -> dict[str, Any]:
    """Structured, retryable 'unavailable' response for an expired KRX session."""
    return _build_screen_response(
        [],
        0,
        {"market": market, "sort_by": sort_by, "sort_order": sort_order},
        market,
        warnings=[
            "KRX 세션이 만료되어 KR 스크리너를 일시적으로 사용할 수 없습니다. "
            "잠시 후 다시 시도하세요."
        ],
        meta_fields={
            "data_state": "unavailable",
            "retryable": True,
            "reason": "krx_session_expired",
        },
    )
```

> `_build_screen_response`가 `kr.py`에 이미 import되어 있는지 상단에서 확인한다(legacy `_screen_kr`가 사용 중이므로 import되어 있어야 함). 없으면 import에 추가한다.

- [ ] **Step 4: Wrap the fallback body to catch `KRXSessionExpiredError`**

`app/mcp_server/tooling/screening/kr.py`, `_screen_kr_with_fallback`의 본문(`capability_snapshot = ...`부터 끝의 `return await _screen_kr(...)`까지)을 `try/except`로 감싼다. 함수 마지막을 다음 형태로 변경:

```python
    try:
        # ... 기존 본문 전체 (capability_snapshot 계산, tvscreener 시도, 그리고
        #     마지막 legacy fallback `return await _screen_kr(...)`) ...
        return await _screen_kr(
            market=market,
            asset_type=asset_type,
            category=category,
            min_market_cap=min_market_cap,
            max_per=max_per,
            max_pbr=max_pbr,
            min_dividend_yield=min_dividend_yield,
            max_rsi=max_rsi,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            adv_krw_min=adv_krw_min,
            market_cap_min_krw=market_cap_min_krw,
            market_cap_max_krw=market_cap_max_krw,
            instrument_types=instrument_types,
            exclude_sectors=exclude_sectors,
            exclude_sector_keys=exclude_sector_keys,
        )
    except KRXSessionExpiredError:
        logger.warning(
            "KRX session expired during KR screening; returning unavailable signal"
        )
        return _krx_session_unavailable_response(market, sort_by, sort_order)
```

> 주의: 기존 본문에는 tvscreener 경로의 `except Exception:`가 있다. `KRXSessionExpiredError`가 tvscreener 경로에서 발생하면 그 `except Exception`이 먼저 삼켜 legacy로 폴백하고, legacy `_screen_kr`에서 다시 raise되어 바깥 `except KRXSessionExpiredError`가 잡는다 — 의도된 흐름이다. 들여쓰기만 `try:` 블록 안으로 한 단계 이동하고 로직은 보존한다.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_screening_kr.py::TestKrScreenSessionExpired -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/screening/kr.py tests/test_screening_kr.py
git commit -m "feat(ROB-388): surface KRX session expiry as structured unavailable signal

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: `screen_stocks` tool description에 trade_amount 지원 범위 명시

단일 `Literal` enum이 시장별 차등을 못 하므로 description으로 보완한다.

**Files:**
- Modify: `app/mcp_server/tooling/analysis_registration.py` (라인 ~192-197)

- [ ] **Step 1: Update the description string**

`app/mcp_server/tooling/analysis_registration.py`, `screen_stocks` 등록 `description`에 문장 추가:

```python
        description=(
            "Screen stocks across markets (KR/US/Crypto) with filters. "
            "KR supports kospi/kosdaq/konex/all, 30-day ADV via adv_krw_min "
            "(1B KRW conservative, 5B KRW aggressive), instrument_types, "
            "and exclude_sectors. "
            "sort_by='trade_amount' is supported for KR and crypto only; "
            "for US use 'volume', 'market_cap', or 'change_rate'."
        ),
```

- [ ] **Step 2: Verify the module imports cleanly**

Run: `uv run python -c "import app.mcp_server.tooling.analysis_registration"`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add app/mcp_server/tooling/analysis_registration.py
git commit -m "docs(ROB-388): note trade_amount sort is KR/crypto-only in screen_stocks desc

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: 전체 검증 + lint + 핸드오프

**Files:** 없음 (검증 전용)

- [ ] **Step 1: Run the focused test suites**

Run:
```bash
uv run pytest tests/test_services_krx.py tests/test_screening_common.py tests/test_screening_kr.py -v
```
Expected: 모두 PASS (신규 + 기존 회귀 없음).

- [ ] **Step 2: Run the broader screening + screen_stocks suites for regressions**

Run:
```bash
uv run pytest tests/test_screening_entrypoint.py tests/test_mcp_screen_stocks_kr.py tests/test_mcp_screen_stocks_crypto.py -v
```
Expected: 모두 PASS.

- [ ] **Step 3: Lint (CLAUDE.md 게이트: ruff check + format --check)**

Run:
```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
```
Expected: 둘 다 통과. (format 위반이면 `uv run ruff format app/ tests/` 후 재확인 및 커밋.)

- [ ] **Step 4: Push branch and open PR (base: main)**

Run:
```bash
git push -u origin rob-388
gh pr create --base main --title "fix(ROB-388): KR screen_stocks 개장 경로 복구 (trade_amount + KRX session)" --body "$(cat <<'EOF'
## 요약
ROB-388: 개장 시점 `screen_stocks(market="kr")`의 두 결함 복구.

1. **trade_amount false affordance** — KR 종목의 기존 `value`(ACC_TRDVAL=거래대금)로 KR `trade_amount` 정렬을 실제 지원. validation은 market-aware(US는 actionable error). tool description 보완.
2. **KRX session expired** — 재인증-후-LOGOUT를 `KRXSessionExpiredError`로 분류하고, KR 스크리닝 경계에서 catch → `data_state="unavailable"` / `retryable=true` / warning 구조화 신호 반환(raw 예외 전파 제거).

## 테스트
- `tests/test_services_krx.py` — 타입 예외 분류 (httpx MockTransport)
- `tests/test_screening_common.py` — validation market-aware + trade_amount 정렬
- `tests/test_screening_kr.py` — KR trade_amount 노출 + session-expired 구조화 응답

## 안전 경계
- read-only. broker/order/watch mutation 없음. 스키마 enum 추가/DB 마이그레이션 없음. recommend_stocks 재노출 없음.

## 잔여 (handoff)
- KRX 세션 prewarm / short-backoff auto-retry는 범위 밖 — live KRX 타이밍 의존이라 fake로 완전 검증 불가. 후속 operator-gated 검증 필요.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Expected: PR URL 출력. (출력된 URL을 확인 후에만 PR 번호 인용.)

- [ ] **Step 5: ROB-394 handoff 코멘트 작성**

ROB-394에 ROB-388 결과(PR 링크 + 검증 명령/결과 + 잔여 blocker: KRX prewarm/auto-retry live 검증)를 남기고, 다음 순서가 ROB-389임을 명시한다. (Linear `save_comment` 사용.)

---

## Self-Review

**Spec coverage:**
- 변경1 trade_amount KR 지원 → Task 2(validation) + Task 3(sort) + Task 4(노출) + Task 6(description). ✅
- 변경2 KRX session 구조화 → Task 1(예외) + Task 5(경계 catch). ✅
- 테스트 T1/T2/T3 → Task 2/Task 3/Task 4·5에 매핑. ✅
- 안전 경계(read-only, no migration, no recommend_stocks) → 코드 변경에 신규 mutation 없음, enum/마이그레이션 없음. ✅
- 비목표(US trade_amount 구현, prewarm live 검증) → Task 6 description + Task 7 handoff에 명시. ✅

**Placeholder scan:** 모든 step에 실제 코드/명령 포함. "적절한 처리" 류 없음. ✅

**Type consistency:** `KRXSessionExpiredError`(Task1 정의 → Task5 import/catch), `_krx_session_unavailable_response`(Task5 정의·사용), meta 키 `data_state`/`retryable`/`reason`(Task5 정의 ↔ 테스트 assert 일치), `trade_amount` 키(Task4 노출 ↔ Task3 정렬 ↔ 테스트). ✅
