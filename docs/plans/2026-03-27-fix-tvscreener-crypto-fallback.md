# Fix: tvscreener CryptoScreener query None + legacy fallback 제거

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** tvscreener CryptoScreener의 `sort_by()` None 반환 문제를 수정하고, silent legacy fallback을 제거하여 에러를 명시적으로 전파

**Architecture:**
1. 기존 방어코드 (`query_crypto_screener`의 None-guard)를 기준으로 문제 위치를 fallback swallow로 확정
2. fallback 제거 후 에러 전파 테스트를 먼저 추가 (TDD)
3. `_screen_crypto_with_fallback()`의 try/except 제거
4. `_screen_crypto()` 및 `_enrich_crypto_indicators()` dead code 삭제
5. 삭제된 심볼에 의존하는 테스트 전환

**Tech Stack:** Python 3.13, tvscreener, pandas, pytest

---

## 검토 보정 포인트 (원본 계획 대비)

- **Part 1 (원인 진단) 불필요**: 이미 `query_crypto_screener()`에 `sort_by()`/`where()`가 `None` 반환 시 `TvScreenerError`를 던지는 방어코드 존재 (`app/services/tvscreener_service.py:868-874`)
- **삭제 순서 변경**: 테스트 고정 → fallback 제거 → dead code 제거 → 테스트 정리 (안전한 순서)
- **호출 경로 확인**: `screen_stocks` crypto는 `entrypoint.py`에서 `_screen_crypto_with_fallback()` 호출 (`app/mcp_server/tooling/screening/entrypoint.py:139`)
- **테스트 영향**: `_screen_crypto`, `_screen_crypto_with_fallback`, `_enrich_crypto_indicators`를 import/mocking하는 테스트 다수 존재

---

## Task 1: 기존 방어코드 존재 여부를 테스트 기준으로 확정

**Files:**
- Read: `tests/test_tvscreener_integration.py` (기존 테스트 존재 확인)

**Step 1: 기존 테스트 확인**

Run: `uv run pytest tests/test_tvscreener_integration.py -v -k "crypto" --collect-only`
Expected: `test_crypto_screener_raises_when_sort_by_returns_none` 또는 유사한 테스트 존재 확인

**Step 2: 테스트 실행**

Run: `uv run pytest tests/test_tvscreener_integration.py::test_crypto_screener_raises_when_sort_by_returns_none -q`
Expected: 1 passed. `query_crypto_screener`의 None-guard가 baseline으로 고정됨.

---

## Task 2: fallback 제거 후 기대 동작(에러 전파) 테스트 추가

**Files:**
- Create/Modify: `tests/test_tvscreener_crypto.py` (또는 `tests/test_screening_entrypoint.py`)

**Step 1: 테스트 추가 - tvscreener 실패 시 legacy로 가지 않고 예외 전파**

```python
@pytest.mark.asyncio
async def test_screen_crypto_fallback_removed_propagates_error():
    """tvscreener 실패 시 legacy fallback 없이 예외가 전파되어야 함."""
    from app.mcp_server.tooling.screening.crypto import _screen_crypto_with_fallback
    
    with patch(
        "app.mcp_server.tooling.screening.crypto._screen_crypto_via_tvscreener"
    ) as mock_tvscreener:
        mock_tvscreener.side_effect = TvScreenerError("sort_by returned None")
        
        with pytest.raises(TvScreenerError, match="sort_by returned None"):
            await _screen_crypto_with_fallback(
                market="crypto",
                asset_type=None,
                category=None,
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="trade_amount",
                sort_order="desc",
                limit=10,
            )
        
        # legacy _screen_crypto가 호출되지 않아야 함
        # (Task 3에서 fallback 제거 후 이 테스트가 pass해야 함)
```

**Step 2: 테스트 실행 (현재는 실패 예상)**

Run: `uv run pytest tests/test_tvscreener_crypto.py::test_screen_crypto_fallback_removed_propagates_error -q`
Expected: FAIL (현재는 fallback이 존재하여 예외가 전파되지 않음)

---

## Task 3: fallback wrapper의 try/except 제거

**Files:**
- Modify: `app/mcp_server/tooling/screening/crypto.py` L983-1026

**Step 1: `_screen_crypto_with_fallback()` 수정**

현재 코드:
```python
async def _screen_crypto_with_fallback(
    market: str,
    asset_type: str | None,
    category: str | None,
    min_market_cap: float | None,
    max_per: float | None,
    min_dividend_yield: float | None,
    max_rsi: float | None,
    sort_by: str,
    sort_order: str,
    limit: int,
) -> dict[str, Any]:
    """Screen crypto market with tvscreener fallback to legacy."""
    try:
        return await _screen_crypto_via_tvscreener(...)
    except Exception as exc:
        logger.debug(
            "tvscreener crypto screening failed, falling back to legacy: %s",
            exc,
        )
        # Fallback to legacy implementation
        return await _screen_crypto(...)
```

변경 후:
```python
async def _screen_crypto_with_fallback(
    market: str,
    asset_type: str | None,
    category: str | None,
    min_market_cap: float | None,
    max_per: float | None,
    min_dividend_yield: float | None,
    max_rsi: float | None,
    sort_by: str,
    sort_order: str,
    limit: int,
) -> dict[str, Any]:
    """Screen crypto market using tvscreener (fallback removed)."""
    # Silent fallback 제거 - 에러를 명시적으로 전파
    return await _screen_crypto_via_tvscreener(
        market=market,
        asset_type=asset_type,
        category=category,
        min_market_cap=min_market_cap,
        max_per=max_per,
        min_dividend_yield=min_dividend_yield,
        max_rsi=max_rsi,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
    )
```

**Step 2: 테스트 재실행 (Task 2 테스트가 pass해야 함)**

Run: `uv run pytest tests/test_tvscreener_crypto.py::test_screen_crypto_fallback_removed_propagates_error -q`
Expected: PASS

---

## Task 4: 라우팅 명칭 정리 (wrapper 유지 vs 직접 호출)

**Files:**
- Read: `app/mcp_server/tooling/screening/entrypoint.py` L139

**Step 1: 현재 호출 방식 확인**

```python
# 현재 호출 예상
from app.mcp_server.tooling.screening.crypto import _screen_crypto_with_fallback
...
result = await _screen_crypto_with_fallback(...)
```

**Step 2: 결정 - 옵션 선택**

- **옵션 A**: `_screen_crypto_with_fallback` 이름 유지 (호환성) - 권장
- **옵션 B**: `_screen_crypto_via_tvscreener` 직접 호출로 변경

옵션 A 선택 시: 별도 변경 없음 (Task 3에서 이미 정리됨)

**Step 3: 검증**

Run: `uv run pytest tests/test_mcp_screen_stocks_crypto.py -k "default_restores_public_contract" -q`
Expected: 성공 경로 동작 동일, 실패 경로만 명시적으로 드러남

---

## Task 5: `_screen_crypto` 참조 0개 확인

**Files:**
- Search: 전체 워크스페이스

**Step 1: grep으로 참조 확인**

Run: `grep -n "_screen_crypto\(" app/mcp_server/tooling/screening/crypto.py`
Expected:
- 정의부 (L462)
- fallback에서의 호출 (L1015) - Task 3에서 제거됨

**Step 2: 다른 파일에서 참조 확인**

Run: `grep -rn "_screen_crypto" app/ --include="*.py" | grep -v "__pycache__"`
Expected: `crypto.py` 외 참조 없음 확인

---

## Task 6: legacy `_screen_crypto` 삭제

**Files:**
- Modify: `app/mcp_server/tooling/screening/crypto.py` L462-678 (approx)

**Step 1: 함수 삭제**

`async def _screen_crypto(...)` 함수 블록 전체 삭제.

삭제 범위:
- L462: `async def _screen_crypto(` 시작
- L678: 함수 마지막 (return 문 또는 마지막 줄)

**Step 2: import 검증 테스트 확인**

Run: `uv run pytest tests/test_screening_crypto.py -q`
Expected: `_screen_crypto` import 테스트가 깨짐 (Task 9에서 갱신)

---

## Task 7: `_enrich_crypto_indicators` 참조 0개 확인

**Files:**
- Search: 전체 워크스페이스

**Step 1: grep으로 참조 확인**

Run: `grep -rn "_enrich_crypto_indicators\(" app/ --include="*.py" | grep -v "__pycache__"`
Expected:
- 정의부 (`crypto.py` L186)
- `_screen_crypto` 낸의 호출 (L610) - Task 6에서 제거됨

**Step 2: 생산 코드 참조 0개 확인**

테스트 파일을 제외하고 생산 코드에서의 참조가 0개인지 확인.

---

## Task 8: dead code `_enrich_crypto_indicators` 삭제 + 관련 import 정리

**Files:**
- Modify: `app/mcp_server/tooling/screening/crypto.py` L186-460 (approx)

**Step 1: 함수 삭제**

`async def _enrich_crypto_indicators(...)` 함수 및 관련 헬퍼 삭제.

삭제 범위:
- L186: `async def _enrich_crypto_indicators(` 시작
- L460: 이전 함수 마지막

**Step 2: 사용하지 않는 import 정리**

삭제 후 사용되지 않는 import 확인 및 정리.

**Step 3: 테스트 실행**

Run: `uv run pytest tests/test_tvscreener_crypto.py -q`
Expected: 삭제된 함수를 직접 테스트하던 케이스는 실패 (Task 9에서 전환)

---

## Task 9: 삭제된 심볼에 의존하는 테스트 전환

**Files:**
- Modify: `tests/test_screening_crypto.py`
- Modify: `tests/test_tvscreener_crypto.py`
- Modify: `tests/test_mcp_screen_stocks_filters_and_rsi.py` (필요 시)
- Modify: `tests/test_mcp_screen_stocks_crypto.py`

**Step 1: `_screen_crypto` import 테스트 제거/대체**

`tests/test_screening_crypto.py`에서 `_screen_crypto` import 테스트 제거.

**Step 2: `_enrich_crypto_indicators` 테스트 전환**

`tests/test_tvscreener_crypto.py`에서 `_enrich_crypto_indicators` 직접 테스트는 `_screen_crypto_via_tvscreener` 계약 테스트로 교체.

**Step 3: mocking 대상 변경**

다음 테스트 파일에서 `_enrich_crypto_indicators` mocking을 `_screen_crypto_via_tvscreener` 관 mocking으로 변경:
- `tests/test_mcp_screen_stocks_filters_and_rsi.py`
- `tests/test_mcp_screen_stocks_crypto.py`
- `tests/_mcp_screen_stocks_support.py`

**Step 4: 테스트 실행**

Run: `uv run pytest tests/test_screening_crypto.py tests/test_tvscreener_crypto.py tests/test_mcp_screen_stocks_filters_and_rsi.py -q`
Expected: 삭제된 API 의존 제거, 테스트가 실제 계약만 검증

---

## Task 10: voting signal None 회귀 방지 테스트 고정

**Files:**
- Modify: `tests/test_mcp_screen_stocks_crypto.py`

**Step 1: 성공 케이스에서 voting signal None 방지 assertion 추가**

```python
async def test_screen_crypto_success_returns_voting_signals():
    """성공 시 voting signal이 전부 None이 아님을 보장."""
    result = await screen_stocks(market="crypto", limit=5)
    
    for item in result["results"]:
        # voting 시그널이 None이면 안 됨 (silent failure 방지)
        assert item.get("bull_votes") is not None, f"{item['symbol']}: bull_votes is None"
        assert item.get("bear_votes") is not None, f"{item['symbol']}: bear_votes is None"
        assert item.get("buy_signal") is not None, f"{item['symbol']}: buy_signal is None"
        assert item.get("sell_signal") is not None, f"{item['symbol']}: sell_signal is None"
```

**Step 2: 테스트 실행**

Run: `uv run pytest tests/test_mcp_screen_stocks_crypto.py -k "voting" -q`
Expected: silent failure가 재발하면 즉시 red

---

## Task 11: 문서/주석 정리 (fallback 표현 제거)

**Files:**
- Modify: `app/mcp_server/tooling/screening/crypto.py` 모듈 docstring
- Modify: 필요 시 `app/mcp_server/README.md`의 crypto screening 설명

**Step 1: docstring에서 "fallback" 표현 제거**

**Step 2: tvscreener 실패 시 동작(에러 전파) 명시**

**Step 3: grep으로 잔여 표현 확인**

Run: `grep "fallback" app/mcp_server/tooling/screening/crypto.py app/mcp_server/README.md`
Expected: 코드/문서 계약 일치

---

## Task 12: 최종 검증 게이트

**Files:**
- All modified files

**Step 1: LSP diagnostics**

Run: diagnostics on modified files
Expected: error 0

**Step 2: 핵심 회귀 테스트**

```bash
uv run pytest tests/test_tvscreener_integration.py tests/test_tvscreener_crypto.py tests/test_mcp_screen_stocks_crypto.py tests/test_screening_crypto.py -q
```
Expected: All passed

**Step 3: 린트 체크**

```bash
uv run ruff check app/mcp_server/tooling/screening/crypto.py app/services/tvscreener_service.py
```
Expected: Clean

**Step 4: 타입 체크 (선택)**

```bash
uv run ty app/mcp_server/tooling/screening/crypto.py app/services/tvscreener_service.py
```

---

## Task 13: 커밋

**Step 1: 변경사항 스테이징**

```bash
git add -A
```

**Step 2: 커밋**

```bash
git commit -m "fix: resolve tvscreener CryptoScreener None query and remove silent legacy fallback

- Remove silent legacy fallback from _screen_crypto_with_fallback()
  - tvscreener 실패 시 에러를 명시적으로 전파
- Delete _screen_crypto() dead code
- Delete _enrich_crypto_indicators() dead code
- Update tests to use tvscreener path only
- Add voting signal None regression test
- Add error propagation test for removed fallback

Fixes silent failure where voting signals were all None due to fallback"
```

---

## 검증 체크리스트

- [x] Task 1: 기존 방어코드 baseline 확인
- [x] Task 2: 에러 전파 테스트 추가 (red 상태)
- [x] Task 3: fallback 제거 (Task 2 green)
- [x] Task 4: 라우팅 정리
- [x] Task 5: `_screen_crypto` 참조 0개 확인
- [x] Task 6: `_screen_crypto` 삭제
- [x] Task 7: `_enrich_crypto_indicators` 참조 0개 확인
- [x] Task 8: `_enrich_crypto_indicators` 삭제
- [x] Task 9: 테스트 전환
- [x] Task 10: voting signal 회귀 테스트
- [x] Task 11: 문서 정리
- [x] Task 12: 최종 검증
- [ ] Task 13: 커밋

---

## 주의사항

1. **삭제 순서**: 테스트 고정 → fallback 제거 → dead code 제거 → 테스트 정리
2. **검증 단계**: 각 삭제 전에 참조 0개 확인 (grep)
3. **테스트 의존성**: 많은 테스트가 삭제 대상 함수를 mock - Task 9에서 일괄 전환
4. **voting signal**: Task 10의 회귀 테스트로 silent failure 방지
