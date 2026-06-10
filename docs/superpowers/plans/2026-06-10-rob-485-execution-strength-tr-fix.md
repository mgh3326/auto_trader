# ROB-485 get_execution_strength KIS TR/필드 매핑 교체 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `get_execution_strength`가 장중에 전부 null을 반환하는 버그를 수정한다 — KIS TR을 FHKST01010100(inquire-price, 체결강도 필드 없음)에서 FHKST01010300(inquire-ccnl, `tday_rltv`)으로 교체하고, 장중 null을 `field_unavailable`로 정직하게 신호한다.

**Architecture:** broker 레이어(`DomesticMarketDataMixin.inquire_execution_strength`)가 FHKST01010300의 tick row LIST에서 `max(stck_cntg_hour)`로 최신 row를 골라 실 키(`tday_rltv`/`stck_cntg_hour`/`stck_prpr`) dict로 반환 → 순수 변환 레이어(`execution_strength.query_service`)가 파싱/trend 분류 → MCP impl(`_get_execution_strength_impl`)이 freshness 태깅 + 정직 신호를 담당한다. KIS REST에 per-side 체결량이 없으므로 buy/sell_volume은 필드 유지 + 항상 None.

**Tech Stack:** Python 3.13+/uv, pytest(+pytest-asyncio, AsyncMock), FastMCP, KIS REST (httpx 기반 `_request_with_token_retry`). Migration 0 (코드-only).

---

## Verified Root Cause (2026-06-10 조사)

장중(2026-06-10 정규장) 라이브 프로브 2회로 독립 검증된 root cause:

1. **TR/필드 불일치 (주원인)**: `inquire_execution_strength`(`app/services/brokers/kis/domestic_market_data.py:227-235`)는 `constants.DOMESTIC_PRICE_TR` = FHKST01010100(`/uapi/domestic-stock/v1/quotations/inquire-price`, `constants.py:15-16`)을 호출하고 `cttr`/`shnu_cntg_qty`/`seln_cntg_qty`/`stck_cntg_hour`/`stck_cntg_time`을 파싱(`domestic_market_data.py:236-245`)하지만, **이 TR의 실 응답(80-key output dict)에는 다섯 키 모두 부재** (012450/257720 두 종목에서 라이브 검증; `stck_prpr`/`acml_vol`/`stck_shrn_iscd`만 존재). 결과: `out.get(...)→None → _to_float(None)→None → _classify_trend(None)→None`(`app/services/execution_strength/query_service.py:25-31,34-42,53-60`) — 보고된 전부-null 페이로드와 정확히 일치.
2. **실 REST 체결강도 소스**: FHKST01010300(`/uapi/domestic-stock/v1/quotations/inquire-ccnl`, 주식현재가 체결)의 `output`은 **tick row 의 LIST**이며 row 키 전수는 `cntg_vol, prdy_ctrt, prdy_vrss, prdy_vrss_sign, stck_cntg_hour, stck_prpr, tday_rltv`. 라이브 실측: 012450 row0 = `{stck_cntg_hour:'094227', tday_rltv:'81.82', cntg_vol:'1', stck_prpr:'1031000', prdy_vrss:'15000', prdy_vrss_sign:'2', prdy_ctrt:'1.48'}` (독립 프로브 2: `80.89`@093713). **두 REST TR 모두 per-side 매수/매도 체결량 필드 없음** — 이슈 본문의 "체결량 기반 자체 산출 폴백" 제안은 실현 불가 (전제 정정).
3. **data_state="fresh" 동반**: `kr_market_data_state()`(`app/mcp_server/tooling/market_session.py:38-64`)는 XKRX 세션 시계 전용 분류기(ROB-464 by-design)로 페이로드 내용과 무관 — 분류기 자체는 무결. 장중 null을 "fresh"로 위장하는 갭은 도구 레벨에서만 메꾼다.
4. **facade 위임 갭 (이미 해소, 전제 정정)**: 조사 시점 commit 06c8b47e에서는 `KISClient`에 `inquire_execution_strength` delegate가 없어 AttributeError 에러 페이로드였으나, **PR #1215(squash f4f9b271)가 delegate + 회귀 테스트를 main에 머지 완료**. 이 워크트리(origin/main=26f7daee)는 f4f9b271을 포함하며 `client.py:150-153`에 delegate가 이미 존재 — **재추가 금지**.
5. **rate-limit 맵 미등록**: 프로브에서 `"Unmapped API rate limit ... using defaults (19/1.0s)"` 로그 확인. 맵은 `app/core/config.py:18-43` `DEFAULT_KIS_API_RATE_LIMITS`("TR_ID|/path" 키, `base.py:448` `api_key = f"{tr_id or 'unknown'}|{api_path}"`) — FHKST01010300 등록 필요.
6. **자기충족 테스트**: `tests/test_mcp_execution_strength.py:18-29`와 `tests/test_execution_strength_query_service.py:13` fixture가 FHKST01010100 output에 `cttr`가 있다고 가정한 가상의 dict를 mock — 실 TR 계약을 전혀 검증하지 못했음. 전부 실측 형태로 교체한다.

**플랜 실행 전제**: 워크트리 `/Users/mgh3326/work/auto_trader.rob-485`, 브랜치 `rob-485`(origin/main=26f7daee 기반, #1215 포함). 단일 PR, migration 0. `make lint`는 ruff가 `app/`+`tests/` 둘 다, ty는 `app/`를 검사한다. PR 직전 `git fetch --prune origin && git merge origin/main`으로 최신 main을 반영하고 관련 테스트를 재실행할 것.

---

## Task 1: query_service 입력 계약 교체 (tday_rltv/stck_cntg_hour + tick_time, buy/sell 항상 None)

**Files:**
- Modify: `app/services/execution_strength/query_service.py` (전면 재작성, 61줄)
- Test: `tests/test_execution_strength_query_service.py` (전면 교체)
- Test: `tests/test_mcp_execution_strength.py:1,59-105` (MCP 레벨 mock 형태만 선갱신 — 이 task에서 suite green 유지용)

### Steps

- [ ] **Write the failing tests (query_service 레벨)** — `tests/test_execution_strength_query_service.py` 전체를 다음으로 교체:

```python
"""ROB-485: 체결강도 computation from KIS FHKST01010300 (inquire-ccnl) tday_rltv."""

from __future__ import annotations

import pytest

from app.services.execution_strength.query_service import (
    compute_execution_strength,
)


def test_buy_dominant_when_tday_rltv_above_100():
    raw = {
        "tday_rltv": "135.5",
        "stck_cntg_hour": "100000",
        "stck_prpr": "80000",
        "acml_vol": None,
    }
    data = compute_execution_strength(
        raw, symbol="005930", as_of="2026-06-10T10:00:00+09:00"
    )
    assert data.symbol == "005930"
    assert data.execution_strength_pct == pytest.approx(135.5)
    assert data.tick_time == "100000"
    assert data.trend == "buy_dominant"
    # KIS REST 에 per-side 체결량 없음 — 항상 None (0 날조 금지).
    assert data.buy_volume is None
    assert data.sell_volume is None


def test_sell_dominant_with_live_probe_values():
    # 2026-06-10 09:42 KST 라이브 프로브 실측 row (012450, FHKST01010300).
    raw = {
        "stck_cntg_hour": "094227",
        "stck_prpr": "1031000",
        "prdy_vrss": "15000",
        "prdy_vrss_sign": "2",
        "cntg_vol": "1",
        "tday_rltv": "81.82",
        "prdy_ctrt": "1.48",
    }
    data = compute_execution_strength(raw, symbol="012450", as_of=None)
    assert data.execution_strength_pct == pytest.approx(81.82)
    assert data.trend == "sell_dominant"
    assert data.tick_time == "094227"


def test_neutral_at_exactly_100():
    data = compute_execution_strength(
        {"tday_rltv": "100", "stck_cntg_hour": "120000"}, symbol="x", as_of=None
    )
    assert data.trend == "neutral"


def test_missing_tday_rltv_returns_none_not_fabricated():
    data = compute_execution_strength({}, symbol="005930", as_of=None)
    assert data.execution_strength_pct is None
    assert data.trend is None
    assert data.tick_time is None
    assert data.buy_volume is None
    assert data.sell_volume is None


def test_legacy_fhkst01010100_dict_is_no_longer_assumed():
    # ROB-485 회귀 방지: 옛 FHKST01010100 가정 키(cttr/shnu/seln)는 더 이상
    # 파싱하지 않는다 (해당 TR 에 체결강도 필드 부재가 라이브 검증됨).
    raw = {"cttr": "135.5", "shnu_cntg_qty": "1200", "seln_cntg_qty": "800"}
    data = compute_execution_strength(raw, symbol="005930", as_of=None)
    assert data.execution_strength_pct is None
    assert data.buy_volume is None
    assert data.sell_volume is None
    assert data.trend is None


def test_blank_tick_time_normalizes_to_none():
    data = compute_execution_strength(
        {"tday_rltv": "99.0", "stck_cntg_hour": "  "}, symbol="x", as_of=None
    )
    assert data.tick_time is None
    assert data.trend == "sell_dominant"
```

- [ ] **Run test to verify it fails**:

```bash
cd /Users/mgh3326/work/auto_trader.rob-485
uv run pytest tests/test_execution_strength_query_service.py -q
```

기대 출력: 6개 중 다수 FAILED — `test_buy_dominant_when_tday_rltv_above_100`는 `assert None == 135.5 ± ...`(구 코드는 `cttr`만 읽음), `tick_time` 접근은 `AttributeError: 'ExecutionStrengthData' object has no attribute 'tick_time'`, `test_legacy_...`는 구 코드가 `cttr`를 파싱해 `assert 135.5 is None` 실패.

- [ ] **Update MCP-레벨 테스트 mock 형태** — `tests/test_mcp_execution_strength.py`에서 3곳 수정.

  (a) 모듈 docstring (line 1) — 현재 코드:

```python
"""ROB-462: get_execution_strength MCP tool + KIS broker fetch (KR equity)."""
```

  변경 후:

```python
"""ROB-462/ROB-485: get_execution_strength MCP tool + KIS FHKST01010300 fetch."""
```

  (b) `test_get_execution_strength_kr_returns_strength` (lines 59-85) — 현재 코드:

```python
@pytest.mark.asyncio
async def test_get_execution_strength_kr_returns_strength(monkeypatch):
    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            return {
                "symbol": code,
                "cttr": "135.5",
                "shnu_cntg_qty": "1200",
                "seln_cntg_qty": "800",
            }

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "fresh"
    )

    result = await market_data_quotes._get_execution_strength_impl("005930", "kr")

    assert result["symbol"] == "005930"
    assert result["execution_strength_pct"] == pytest.approx(135.5)
    assert result["trend"] == "buy_dominant"
    assert result["buy_volume"] == pytest.approx(1200.0)
    assert result["sell_volume"] == pytest.approx(800.0)
    assert result["data_state"] == "fresh"
    assert result["source"] == "kis"
    assert result["instrument_type"] == "equity_kr"
    assert result["as_of"]
```

  변경 후:

```python
@pytest.mark.asyncio
async def test_get_execution_strength_kr_returns_strength(monkeypatch):
    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            return {
                "symbol": code,
                "tday_rltv": "135.5",
                "stck_cntg_hour": "100000",
                "stck_prpr": "80000",
                "acml_vol": None,
            }

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "fresh"
    )

    result = await market_data_quotes._get_execution_strength_impl("005930", "kr")

    assert result["symbol"] == "005930"
    assert result["execution_strength_pct"] == pytest.approx(135.5)
    assert result["trend"] == "buy_dominant"
    # KIS REST 미제공 (WebSocket H0STCNT0 전용) — 항상 None, 0 날조 금지.
    assert result["buy_volume"] is None
    assert result["sell_volume"] is None
    assert result["data_state"] == "fresh"
    assert result["source"] == "kis"
    assert result["instrument_type"] == "equity_kr"
    assert result["as_of"]
```

  (c) `test_get_execution_strength_tags_premarket_data_state` (lines 88-104) — 현재 코드:

```python
@pytest.mark.asyncio
async def test_get_execution_strength_tags_premarket_data_state(monkeypatch):
    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            return {"cttr": "88.0"}
```

  변경 후 (mock 반환 dict 한 줄만 교체, 나머지 본문 동일):

```python
@pytest.mark.asyncio
async def test_get_execution_strength_tags_premarket_data_state(monkeypatch):
    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            return {"tday_rltv": "88.0"}
```

- [ ] **Run test to verify it fails**:

```bash
uv run pytest tests/test_mcp_execution_strength.py -q
```

기대 출력: `test_get_execution_strength_kr_returns_strength` FAILED (`assert result["execution_strength_pct"] == pytest.approx(135.5)` — 구 query_service가 `tday_rltv`를 못 읽어 None), `test_get_execution_strength_tags_premarket_data_state` FAILED (`trend` None). 나머지(broker/facade/등록 테스트)는 PASS.

- [ ] **Write minimal implementation** — `app/services/execution_strength/query_service.py` 전체를 다음으로 교체:

```python
"""ROB-462/ROB-485: KR 주식 체결강도 (execution strength) read model.

체결강도 = 매수체결량 / 매도체결량 × 100 (당일 누적). KIS REST 에서는
FHKST01010300 (주식현재가 체결, inquire-ccnl) tick row 의 ``tday_rltv`` 가
공식 체결강도다 — broker 레이어가 최신 row 를 골라 전달하고, 여기서는
파싱/분류만 한다 (재계산 금지). FHKST01010100 (주식현재가 시세) 에는
체결강도 필드가 없다 (2026-06-10 라이브 검증, ROB-485).

매수/매도 체결량 분리(buy_volume/sell_volume)는 KIS REST 미제공 — WebSocket
H0STCNT0 전용이므로 항상 None (cntg_vol/prdy_vrss_sign 으로 추정 조작 금지;
WS 소스 연동은 follow-up). Pure transform; broker fetch + freshness tagging
은 MCP tool 이 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExecutionStrengthData:
    symbol: str
    as_of: str | None
    tick_time: str | None
    execution_strength_pct: float | None
    buy_volume: float | None
    sell_volume: float | None
    trend: str | None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_tick_time(value: Any) -> str | None:
    """KIS ``stck_cntg_hour`` (HHMMSS KST) 문자열을 그대로 보존한다."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _classify_trend(strength: float | None) -> str | None:
    """체결강도 100 = 매수/매도 균형. >100 매수우위, <100 매도우위."""
    if strength is None:
        return None
    if strength > 100.0:
        return "buy_dominant"
    if strength < 100.0:
        return "sell_dominant"
    return "neutral"


def compute_execution_strength(
    raw: dict[str, Any], *, symbol: str, as_of: str | None
) -> ExecutionStrengthData:
    """Build the read model from a KIS FHKST01010300 tick row dict.

    ``raw`` 는 broker 가 고른 최신 tick row 의 실 키
    (``tday_rltv``/``stck_cntg_hour``/``stck_prpr``) + ``acml_vol`` 을 담은
    dict 다. ``tday_rltv`` 가 공식 체결강도 (당일 누적). 결측 필드는 None
    유지 — 0 으로 날조하지 않는다. buy/sell volume 은 KIS REST 미제공이라
    항상 None.
    """
    strength = _to_float(raw.get("tday_rltv"))
    return ExecutionStrengthData(
        symbol=symbol,
        as_of=as_of,
        tick_time=_to_tick_time(raw.get("stck_cntg_hour")),
        execution_strength_pct=strength,
        # KIS REST 에 per-side 체결량 없음 (WebSocket H0STCNT0 전용).
        buy_volume=None,
        sell_volume=None,
        trend=_classify_trend(strength),
    )
```

- [ ] **Run tests to verify they pass**:

```bash
uv run pytest tests/test_execution_strength_query_service.py tests/test_mcp_execution_strength.py -q
```

기대 출력: 전부 PASS (broker 레벨 `test_inquire_execution_strength_extracts_cttr`는 broker 코드 미변경이라 아직 PASS — Task 2에서 교체).

- [ ] **Commit**:

```bash
git add app/services/execution_strength/query_service.py tests/test_execution_strength_query_service.py tests/test_mcp_execution_strength.py
git commit -m "$(cat <<'EOF'
fix(ROB-485): query_service 체결강도 입력 계약을 FHKST01010300 실 키로 교체

tday_rltv → execution_strength_pct, stck_cntg_hour → tick_time(additive).
buy/sell_volume 은 KIS REST 미제공(WebSocket H0STCNT0 전용)으로 항상 None.
거짓 docstring(FHKST01010100 cttr 주장) 정정. trend 임계 유지.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 2: broker fetch 재작성 — FHKST01010300 LIST 처리 + max(stck_cntg_hour) row 선택

**Files:**
- Modify: `app/services/brokers/kis/constants.py:15-16` 직후 (DOMESTIC_CCNL_URL/TR 추가)
- Modify: `app/services/brokers/kis/domestic_market_data.py:47-50` 직후 (helper 추가) 및 `:217-245` (메서드 재작성)
- Test: `tests/test_mcp_execution_strength.py` (broker 레벨 테스트 교체 + facade mock 갱신)

**주의**: `KISClient` facade delegate(`client.py:150-153`)는 #1215로 이미 main에 있음 — **건드리지 않는다**.

### Steps

- [ ] **Write the failing tests** — `tests/test_mcp_execution_strength.py`에서 `test_inquire_execution_strength_extracts_cttr` (lines 12-38, 현재 코드는 cttr fixture 기반)를 **삭제**하고, 그 자리에 다음을 넣는다 (import 블록 바로 아래, 첫 테스트 위치):

```python
# 2026-06-10 09:42 KST 라이브 프로브 실측 row (012450). FHKST01010300 row 키
# 전수: cntg_vol, prdy_ctrt, prdy_vrss, prdy_vrss_sign, stck_cntg_hour,
# stck_prpr, tday_rltv — per-side 매수/매도 체결량 필드는 없다.
_CCNL_ROW_OLDER = {
    "stck_cntg_hour": "093713",
    "stck_prpr": "1031000",
    "prdy_vrss": "15000",
    "prdy_vrss_sign": "2",
    "cntg_vol": "2",
    "tday_rltv": "80.89",
    "prdy_ctrt": "1.48",
}
_CCNL_ROW_LATEST = {
    "stck_cntg_hour": "094227",
    "stck_prpr": "1031000",
    "prdy_vrss": "15000",
    "prdy_vrss_sign": "2",
    "cntg_vol": "1",
    "tday_rltv": "81.82",
    "prdy_ctrt": "1.48",
}


def _make_market_data_mixin(response):
    from app.services.brokers.kis.domestic_market_data import (
        DomesticMarketDataMixin,
    )

    md = DomesticMarketDataMixin.__new__(DomesticMarketDataMixin)
    md._kis_url = lambda path: path
    md._request_with_token_retry = AsyncMock(return_value=response)
    return md


@pytest.mark.asyncio
async def test_inquire_execution_strength_uses_ccnl_tr_and_selects_latest_row():
    from app.services.brokers.kis import constants

    # 의도적으로 오래된 row 를 먼저 둬서 index 0 비신뢰를 증명한다.
    md = _make_market_data_mixin({"output": [_CCNL_ROW_OLDER, _CCNL_ROW_LATEST]})

    raw = await md.inquire_execution_strength("012450")

    assert raw["symbol"] == "012450"
    assert raw["tday_rltv"] == "81.82"
    assert raw["stck_cntg_hour"] == "094227"
    assert raw["stck_prpr"] == "1031000"
    assert raw["acml_vol"] is None
    md._request_with_token_retry.assert_awaited_once_with(
        tr_id=constants.DOMESTIC_CCNL_TR,
        url=constants.DOMESTIC_CCNL_URL,
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "012450"},
        api_name="inquire_execution_strength",
    )


@pytest.mark.asyncio
async def test_inquire_execution_strength_empty_list_returns_all_none():
    # 개장 직전/거래정지 등 빈 tick 리스트 → all-None graceful, 절대 raise 금지.
    md = _make_market_data_mixin({"output": []})

    raw = await md.inquire_execution_strength("012450")

    assert raw == {
        "symbol": "012450",
        "tday_rltv": None,
        "stck_cntg_hour": None,
        "stck_prpr": None,
        "acml_vol": None,
    }


@pytest.mark.asyncio
async def test_inquire_execution_strength_dict_output_returns_all_none():
    # 옛 FHKST01010100 식 단일 dict output 형태는 더 이상 가정하지 않는다.
    md = _make_market_data_mixin(
        {"output": {"cttr": "120.3", "stck_prpr": "80000"}}
    )

    raw = await md.inquire_execution_strength("005930")

    assert raw["tday_rltv"] is None
    assert raw["stck_cntg_hour"] is None


@pytest.mark.asyncio
async def test_inquire_execution_strength_row_missing_tday_rltv():
    row = {"stck_cntg_hour": "094227", "stck_prpr": "1031000", "cntg_vol": "1"}
    md = _make_market_data_mixin({"output": [row]})

    raw = await md.inquire_execution_strength("012450")

    assert raw["tday_rltv"] is None
    assert raw["stck_cntg_hour"] == "094227"
```

  그리고 facade 테스트 `test_kis_facade_delegates_execution_strength` (lines 41-56)의 mock 형태만 갱신 — 현재 코드:

```python
    client._market_data.inquire_execution_strength = AsyncMock(
        return_value={"cttr": "120.3"}
    )

    raw = await client.inquire_execution_strength("005930", market="J")

    assert raw == {"cttr": "120.3"}
```

  변경 후 (#1215 회귀 테스트는 유지, mock 형태만 실 키로):

```python
    client._market_data.inquire_execution_strength = AsyncMock(
        return_value={"tday_rltv": "81.82"}
    )

    raw = await client.inquire_execution_strength("005930", market="J")

    assert raw == {"tday_rltv": "81.82"}
```

- [ ] **Run test to verify it fails**:

```bash
uv run pytest tests/test_mcp_execution_strength.py -q
```

기대 출력: 신규 broker 테스트 4개 FAILED/ERROR. 실패 모드: KIS 호출을 await하는 테스트는 구 구현의 `(js.get("output") or {}).get(...)`이 **assert 도달 전에** `AttributeError: 'list' object has no attribute 'get'`로 먼저 죽는다 (`constants.DOMESTIC_CCNL_TR` 비교 assert까지 가지 않음); 상수만 직접 참조하는 테스트는 `AttributeError: module ... has no attribute 'DOMESTIC_CCNL_TR'`; empty-list 테스트는 키 불일치 assert 실패. facade/MCP 테스트는 PASS.

- [ ] **Write minimal implementation (1/2: constants)** — `app/services/brokers/kis/constants.py` lines 15-16 직후에 추가. 현재 코드:

```python
DOMESTIC_PRICE_URL = "/uapi/domestic-stock/v1/quotations/inquire-price"
DOMESTIC_PRICE_TR = "FHKST01010100"
```

  변경 후:

```python
DOMESTIC_PRICE_URL = "/uapi/domestic-stock/v1/quotations/inquire-price"
DOMESTIC_PRICE_TR = "FHKST01010100"

# ROB-485: 주식현재가 체결 — 최근 체결 tick rows (tday_rltv = 당일 체결강도)
DOMESTIC_CCNL_URL = "/uapi/domestic-stock/v1/quotations/inquire-ccnl"
DOMESTIC_CCNL_TR = "FHKST01010300"
```

- [ ] **Write minimal implementation (2/2: broker)** — `app/services/brokers/kis/domestic_market_data.py`.

  (a) module-level helper 추가 — `normalize_daily_chart_lookback` (lines 47-50)과 `class DomesticMarketDataMixin` (line 53) 사이에 삽입:

```python
def _select_latest_ccnl_row(rows: list[Any]) -> dict[str, Any] | None:
    """FHKST01010300 tick rows 중 최신 체결 row 선택 (ROB-485).

    KIS 응답이 최신-우선 정렬로 관측되었지만 (2026-06-10 라이브 프로브)
    문서 보장이 없으므로 index 0 을 신뢰하지 않고 ``stck_cntg_hour``
    (HHMMSS zero-padded → 사전순 == 시간순) 최대값으로 고른다.
    """
    candidates: list[dict[str, Any]] = [
        row for row in rows if isinstance(row, dict)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: str(row.get("stck_cntg_hour") or ""))
```

  (b) `inquire_execution_strength` 재작성 — 현재 코드 (lines 217-245):

```python
    async def inquire_execution_strength(
        self, code: str, market: str = "J"
    ) -> dict[str, Any]:
        """ROB-462: KIS FHKST01010100 (주식현재가 시세) 체결강도 raw 필드.

        ``cttr`` 가 체결강도(매수/매도 × 100). 매수/매도 체결량(shnu/seln)·현재가·
        누적거래량·체결시각을 raw 문자열 그대로 반환하고, 파싱/분류는
        execution_strength.query_service 가 담당한다. 정확한 필드명은 라이브
        스모크로 확정한다(없는 필드는 None 처리, 0 날조 금지).
        """
        js = await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_PRICE_TR,
            url=self._kis_url(constants.DOMESTIC_PRICE_URL),
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": code.zfill(6),
            },
            api_name="inquire_execution_strength",
        )
        out = js.get("output") or {}
        return {
            "symbol": out.get("stck_shrn_iscd") or code,
            "cttr": out.get("cttr"),
            "shnu_cntg_qty": out.get("shnu_cntg_qty"),
            "seln_cntg_qty": out.get("seln_cntg_qty"),
            "last_price": out.get("stck_prpr"),
            "acml_vol": out.get("acml_vol"),
            "time": out.get("stck_cntg_hour") or out.get("stck_cntg_time"),
        }
```

  변경 후:

```python
    async def inquire_execution_strength(
        self, code: str, market: str = "J"
    ) -> dict[str, Any]:
        """ROB-485: KIS FHKST01010300 (주식현재가 체결) 체결강도 raw 필드.

        ``output`` 은 최근 체결 tick row 의 **리스트**다. ``tday_rltv`` 가
        당일 (누적) 체결강도. 최신 row 는 ``stck_cntg_hour`` 최대값으로
        고르고, 빈 리스트(개장 직전/거래정지 등)는 all-None 으로 graceful
        처리한다 — 절대 raise 하지 않는다. 파싱/분류는
        execution_strength.query_service 담당 (결측 시 None, 0 날조 금지).

        매수/매도 체결량 분리 필드는 KIS REST 에 없다 (FHKST01010100 80-key
        및 FHKST01010300 row 키 전수 라이브 검증, 2026-06-10) — WebSocket
        H0STCNT0 전용이며 WS 소스 연동은 follow-up.
        """
        js = await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_CCNL_TR,
            url=self._kis_url(constants.DOMESTIC_CCNL_URL),
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": code.zfill(6),
            },
            api_name="inquire_execution_strength",
        )
        output = js.get("output")
        rows = output if isinstance(output, list) else []
        row = _select_latest_ccnl_row(rows) or {}
        return {
            "symbol": code,
            "tday_rltv": row.get("tday_rltv"),
            "stck_cntg_hour": row.get("stck_cntg_hour"),
            "stck_prpr": row.get("stck_prpr"),
            # FHKST01010300 row 에는 acml_vol 없음 (per-tick cntg_vol 만)
            # — 결측은 None 유지, 합산 날조 금지.
            "acml_vol": None,
        }
```

- [ ] **Run tests to verify they pass**:

```bash
uv run pytest tests/test_mcp_execution_strength.py tests/test_execution_strength_query_service.py -q
```

기대 출력: 전부 PASS.

- [ ] **Commit**:

```bash
git add app/services/brokers/kis/constants.py app/services/brokers/kis/domestic_market_data.py tests/test_mcp_execution_strength.py
git commit -m "$(cat <<'EOF'
fix(ROB-485): inquire_execution_strength 를 FHKST01010300(inquire-ccnl)로 재작성

FHKST01010100 에는 체결강도 필드가 없음 (2026-06-10 라이브 검증). output 은
tick row LIST — max(stck_cntg_hour) 로 최신 row 선택 (index 0 비신뢰), 빈
리스트는 all-None graceful (raise 금지). 자기충족 cttr fixture 를 실측 row
형태로 교체. facade delegate(#1215)는 main 기존 그대로 (mock 형태만 갱신).

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 3: MCP impl — tick_time 노출 + 장중 null 정직 신호(field_unavailable) + 거짓 문서 정정

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py:30` (import), `:1220-1267` (impl + 로컬 상수), `:1306-1316` (도구 설명)
- Test: `tests/test_mcp_execution_strength.py` (신규 테스트 3개 추가 — 기존 테스트는 무변경)

**주의**: `market_session.py`의 `kr_market_data_state`는 **절대 수정 금지** (ROB-464 quote/index/top_stocks 공유 분류기). 신호 대체는 이 도구 페이로드에서만 로컬 처리.

### Steps

- [ ] **Write the failing tests** — `tests/test_mcp_execution_strength.py` 파일 끝의 `test_execution_strength_tool_registered` 바로 앞에 추가:

```python
@pytest.mark.asyncio
async def test_get_execution_strength_surfaces_tick_time(monkeypatch):
    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            return {
                "symbol": code,
                "tday_rltv": "81.82",
                "stck_cntg_hour": "094227",
                "stck_prpr": "1031000",
                "acml_vol": None,
            }

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "fresh"
    )

    result = await market_data_quotes._get_execution_strength_impl("012450", "kr")

    assert result["tick_time"] == "094227"
    assert result["execution_strength_pct"] == pytest.approx(81.82)
    assert result["trend"] == "sell_dominant"
    assert result["data_state"] == "fresh"


@pytest.mark.asyncio
async def test_get_execution_strength_field_unavailable_during_fresh(monkeypatch):
    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            # 빈 tick 리스트 → broker all-None graceful 형태.
            return {
                "symbol": code,
                "tday_rltv": None,
                "stck_cntg_hour": None,
                "stck_prpr": None,
                "acml_vol": None,
            }

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "fresh"
    )

    result = await market_data_quotes._get_execution_strength_impl("005930", "kr")

    assert result["execution_strength_pct"] is None
    assert result["trend"] is None
    # 장중인데 전부 null 을 "fresh" 로 위장하지 않는다 (ROB-485 정직 신호).
    assert result["data_state"] == "field_unavailable"


@pytest.mark.asyncio
async def test_get_execution_strength_null_outside_session_keeps_session_state(
    monkeypatch,
):
    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            return {"tday_rltv": None, "stck_cntg_hour": None}

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    for state in ("premarket_unavailable", "market_closed"):
        monkeypatch.setattr(
            market_data_quotes,
            "kr_market_data_state",
            lambda *a, _state=state, **k: _state,
        )
        result = await market_data_quotes._get_execution_strength_impl(
            "005930", "kr"
        )
        # field_unavailable 은 fresh 일 때만 — 세션 외 상태는 그대로 보존.
        assert result["data_state"] == state
```

- [ ] **Run test to verify it fails**:

```bash
uv run pytest tests/test_mcp_execution_strength.py -q
```

기대 출력: `test_get_execution_strength_surfaces_tick_time` FAILED (`KeyError: 'tick_time'` — 페이로드에 키 없음), `test_get_execution_strength_field_unavailable_during_fresh` FAILED (`assert 'fresh' == 'field_unavailable'`). `..._keeps_session_state`는 PASS (현 구현도 보존). 나머지 PASS.

- [ ] **Write minimal implementation** — `app/mcp_server/tooling/market_data_quotes.py` 3곳 수정.

  (a) import (line 30) — 현재 코드:

```python
from app.mcp_server.tooling.market_session import kr_market_data_state
```

  변경 후:

```python
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    kr_market_data_state,
)
```

  (b) impl 교체 — 현재 코드 (lines 1220-1267):

```python
async def _get_execution_strength_impl(
    symbol: str | int, market: str = "kr"
) -> dict[str, Any]:
    """ROB-462: KR 주식 체결강도 (execution strength) snapshot from KIS.

    체결강도 = 매수체결량 / 매도체결량 × 100 (KIS FHKST01010100 ``cttr``).
    KR equity only — crypto is served by get_crypto_order_flow.
    """
    requested = str(market or "kr").strip().lower() or "kr"
    if requested not in ("kr", "kospi", "kosdaq"):
        return _error_payload(
            source="validation",
            message=(
                "get_execution_strength supports KR equity only "
                "(crypto: use get_crypto_order_flow)."
            ),
            symbol=str(symbol),
        )

    normalized = _normalize_symbol_input(symbol, "kr")
    if not normalized:
        raise ValueError("symbol is required")
    _, normalized = _resolve_market_type(normalized, "kr")

    try:
        raw = await KISClient().inquire_execution_strength(normalized)
    except Exception as exc:
        return _error_payload_from_exception(
            source="kis",
            exc=exc,
            symbol=normalized,
            instrument_type="equity_kr",
        )

    data = compute_execution_strength(
        raw, symbol=normalized, as_of=now_kst().isoformat()
    )
    return {
        "symbol": data.symbol,
        "as_of": data.as_of,
        "execution_strength_pct": data.execution_strength_pct,
        "buy_volume": data.buy_volume,
        "sell_volume": data.sell_volume,
        "trend": data.trend,
        "data_state": kr_market_data_state(),
        "source": "kis",
        "instrument_type": "equity_kr",
    }
```

  변경 후 (함수 정의 바로 위에 로컬 상수 포함):

```python
# ROB-485: 장중(fresh)인데 KIS 가 체결강도 필드를 안 준 경우의 정직 신호.
# market_session.kr_market_data_state (ROB-464 공유 분류기)는 수정하지 않고
# 이 도구 페이로드에서만 로컬로 대체한다.
DATA_STATE_FIELD_UNAVAILABLE = "field_unavailable"


async def _get_execution_strength_impl(
    symbol: str | int, market: str = "kr"
) -> dict[str, Any]:
    """ROB-462/ROB-485: KR 주식 체결강도 (execution strength) snapshot.

    체결강도 = 매수체결량 / 매도체결량 × 100 (당일 누적). 소스는 KIS
    FHKST01010300 (주식현재가 체결, inquire-ccnl) tick row 의 ``tday_rltv``
    — FHKST01010100 에는 체결강도 필드가 없다 (2026-06-10 라이브 검증).
    buy_volume/sell_volume 은 KIS REST 미제공 (WebSocket H0STCNT0 전용,
    WS 소스 follow-up) — 항상 null. ``as_of`` 는 조회 시각(now_kst),
    ``tick_time`` 은 broker 최신 체결 시각 (HHMMSS KST). KR equity only —
    crypto 는 get_crypto_order_flow.
    """
    requested = str(market or "kr").strip().lower() or "kr"
    if requested not in ("kr", "kospi", "kosdaq"):
        return _error_payload(
            source="validation",
            message=(
                "get_execution_strength supports KR equity only "
                "(crypto: use get_crypto_order_flow)."
            ),
            symbol=str(symbol),
        )

    normalized = _normalize_symbol_input(symbol, "kr")
    if not normalized:
        raise ValueError("symbol is required")
    _, normalized = _resolve_market_type(normalized, "kr")

    try:
        raw = await KISClient().inquire_execution_strength(normalized)
    except Exception as exc:
        return _error_payload_from_exception(
            source="kis",
            exc=exc,
            symbol=normalized,
            instrument_type="equity_kr",
        )

    data = compute_execution_strength(
        raw, symbol=normalized, as_of=now_kst().isoformat()
    )
    data_state = kr_market_data_state()
    if data.execution_strength_pct is None and data_state == DATA_STATE_FRESH:
        # 장중인데 필드가 비어 있으면 "fresh + 전부 null" 로 위장하지 않는다.
        data_state = DATA_STATE_FIELD_UNAVAILABLE
    return {
        "symbol": data.symbol,
        "as_of": data.as_of,
        "tick_time": data.tick_time,
        "execution_strength_pct": data.execution_strength_pct,
        "buy_volume": data.buy_volume,
        "sell_volume": data.sell_volume,
        "trend": data.trend,
        "data_state": data_state,
        "source": "kis",
        "instrument_type": "equity_kr",
    }
```

  (c) 도구 설명 — 현재 코드 (lines 1306-1316):

```python
    @mcp.tool(
        name="get_execution_strength",
        description=(
            "Get KR equity 체결강도 (execution strength = 매수체결량/매도체결량 × 100) "
            "from KIS. >100 buy-dominant, <100 sell-dominant. Returns "
            "execution_strength_pct, buy_volume/sell_volume (null when KIS omits "
            "them — never a fabricated 0), trend, and data_state (premarket/closed "
            "sessions are tagged stale). KR equity only — for crypto taker order "
            "flow use get_crypto_order_flow."
        ),
    )
```

  변경 후:

```python
    @mcp.tool(
        name="get_execution_strength",
        description=(
            "Get KR equity 체결강도 (execution strength = 매수체결량/매도체결량 "
            "× 100, 당일 누적) from KIS FHKST01010300 (주식현재가 체결) tick "
            "rows. >100 buy-dominant, <100 sell-dominant. Returns "
            "execution_strength_pct, tick_time (latest tick HHMMSS KST), trend, "
            "and data_state (premarket/closed sessions tagged stale; "
            "'field_unavailable' when KIS omits the field during a live "
            "session). buy_volume/sell_volume are always null — per-side "
            "contracted volume is not provided by KIS REST (WebSocket H0STCNT0 "
            "only; WS-sourced follow-up). KR equity only — for crypto taker "
            "order flow use get_crypto_order_flow."
        ),
    )
```

- [ ] **Run tests to verify they pass**:

```bash
uv run pytest tests/test_mcp_execution_strength.py tests/test_execution_strength_query_service.py -q
```

기대 출력: 전부 PASS.

- [ ] **Commit**:

```bash
git add app/mcp_server/tooling/market_data_quotes.py tests/test_mcp_execution_strength.py
git commit -m "$(cat <<'EOF'
fix(ROB-485): tick_time 노출 + 장중 null 정직 신호(field_unavailable)

execution_strength_pct 가 None 인데 kr_market_data_state()==fresh 면
data_state 를 field_unavailable 로 로컬 대체 (market_session 공유 분류기
무변경 — ROB-464 소비자 보존). additive tick_time(HHMMSS KST) 노출,
as_of=now_kst() 유지. 거짓 docstring/도구 설명(FHKST01010100 cttr) 정정.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 4: KIS rate-limit 맵에 FHKST01010300|inquire-ccnl 등록

**Files:**
- Modify: `app/core/config.py:18-43` (`DEFAULT_KIS_API_RATE_LIMITS`)
- Test: `tests/test_config.py:13-38` (`EXPECTED_KIS_API_RATE_LIMITS`)

배경: 프로브 로그 `"[kis] Unmapped API rate limit for FHKST01010300|/uapi/domestic-stock/v1/quotations/inquire-ccnl, using defaults (19/1.0s)"`. 키 형식은 `base.py:448`의 `f"{tr_id}|{api_path}"`. 다른 시세 TR(FHKST03010100 등)과 동일하게 20/1.0 으로 등록한다.

### Steps

- [ ] **Write the failing test** — `tests/test_config.py`의 `EXPECTED_KIS_API_RATE_LIMITS` (lines 13-38)에서 FHKST03010230 항목 (lines 22-25) 바로 뒤에 추가. 현재 코드:

```python
    "FHKST03010230|/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice": {
        "rate": 20,
        "period": 1.0,
    },
    "TTTC8434R|/uapi/domestic-stock/v1/trading/inquire-balance": {
```

  변경 후:

```python
    "FHKST03010230|/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice": {
        "rate": 20,
        "period": 1.0,
    },
    "FHKST01010300|/uapi/domestic-stock/v1/quotations/inquire-ccnl": {
        "rate": 20,
        "period": 1.0,
    },
    "TTTC8434R|/uapi/domestic-stock/v1/trading/inquire-balance": {
```

- [ ] **Run test to verify it fails**:

```bash
uv run pytest tests/test_config.py -q -k "rate_limit"
```

기대 출력: **1개 FAILED** — `test_api_rate_limit_defaults_include_builtins` (`assert cfg.kis_api_rate_limits == EXPECTED_KIS_API_RATE_LIMITS` — 신규 키 부재). 나머지 full-map 동등성 테스트 2개(`test_empty_object_env_override_does_not_erase_builtins` 등)는 이름에 "rate_limit"이 없어 `-k`에서 제외되며, green 스텝의 파일 전체 실행에서 함께 검증된다.

- [ ] **Write minimal implementation** — `app/core/config.py`의 `DEFAULT_KIS_API_RATE_LIMITS` (lines 18-43)에서 동일 위치에 추가. 현재 코드:

```python
    "FHKST03010230|/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice": {
        "rate": 20,
        "period": 1.0,
    },
    "TTTC8434R|/uapi/domestic-stock/v1/trading/inquire-balance": {
```

  변경 후:

```python
    "FHKST03010230|/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice": {
        "rate": 20,
        "period": 1.0,
    },
    # ROB-485: get_execution_strength (주식현재가 체결, tick rows)
    "FHKST01010300|/uapi/domestic-stock/v1/quotations/inquire-ccnl": {
        "rate": 20,
        "period": 1.0,
    },
    "TTTC8434R|/uapi/domestic-stock/v1/trading/inquire-balance": {
```

- [ ] **Run test to verify it passes**:

```bash
uv run pytest tests/test_config.py -q
```

기대 출력: 전부 PASS.

- [ ] **Commit**:

```bash
git add app/core/config.py tests/test_config.py
git commit -m "$(cat <<'EOF'
fix(ROB-485): KIS rate-limit 맵에 FHKST01010300|inquire-ccnl 등록 (20/1.0s)

라이브 프로브에서 "Unmapped API rate limit ... defaults (19/1.0s)" 경고
확인 — 다른 시세 TR 과 동일하게 명시 등록.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 5: 풀 게이트 + PR 생성 + operator 읽기 전용 라이브 스모크 (이슈 close 게이트)

**Files:** 코드 변경 없음 (검증/PR/운영 절차만)

### Steps

- [ ] **풀 게이트 실행**:

```bash
cd /Users/mgh3326/work/auto_trader.rob-485
uv run ruff format app/ tests/        # 포맷 드리프트 선정리 (변경 있으면 amend)
make lint                             # ruff check app/ tests/ + format --check + ty check app/
uv run pytest tests/test_execution_strength_query_service.py \
  tests/test_mcp_execution_strength.py tests/test_config.py \
  tests/test_services_kis_market_data.py -v
```

기대: lint clean, 관련 테스트 전부 PASS (`test_services_kis_market_data.py`는 동일 mixin 파일의 `inquire_price` 무회귀 확인용). 시간이 허용되면 전체 스위트:

```bash
make test    # uv run pytest tests/ -v -m "not live" — CI 와 동일 게이트
```

  ruff format 이 파일을 고쳤다면 직전 커밋에 포함:

```bash
git add -u && git commit --amend --no-edit
```

- [ ] **PR 생성** (base: main; 브랜치 rob-485 그대로 사용 — facade delegate #1215는 이미 main에 있으므로 이 PR 에 재등장하지 않아야 함을 diff 에서 확인):

```bash
git push -u origin rob-485
gh pr create --base main \
  --title "fix(ROB-485): get_execution_strength 장중 전부 null — KIS TR 교체 (FHKST01010100→FHKST01010300)" \
  --body "$(cat <<'EOF'
## Summary
- 장중 전부-null 수정: FHKST01010100(inquire-price)에는 체결강도 필드가 없음(2026-06-10 라이브 검증, 80-key output에 cttr/shnu_cntg_qty/seln_cntg_qty 부재) → FHKST01010300(inquire-ccnl) tick rows의 `tday_rltv`로 교체
- output LIST 처리: `max(stck_cntg_hour)`로 최신 row 선택(index 0 비신뢰), 빈 리스트는 all-None graceful(절대 raise 금지)
- additive `tick_time`(HHMMSS KST) 노출, `as_of`(now_kst) 유지
- buy_volume/sell_volume: KIS REST 미제공(WebSocket H0STCNT0 전용)으로 **필드 유지 + 항상 null** — cntg_vol/prdy_vrss_sign 추정 조작 금지, WS 소스는 follow-up
- 정직 신호: 장중(fresh)인데 체결강도 null이면 `data_state="field_unavailable"` (market_session 공유 분류기 무변경 — ROB-464 소비자 보존)
- KIS rate-limit 맵에 `FHKST01010300|/uapi/domestic-stock/v1/quotations/inquire-ccnl` 등록 (unmapped 19/1.0s 경고 제거)
- 자기충족 테스트(FHKST01010100+cttr fixture) 전부 실측 형태로 교체; #1215 facade 위임 회귀 테스트는 mock 형태만 갱신

## Semantics
- `tday_rltv`는 KIS 당일 **누적** 체결강도(매수체결량/매도체결량×100). 라이브 실측 81.82(<100) → sell_dominant 일관. trend 임계 유지(>100 buy_dominant, <100 sell_dominant, ==100 neutral).

## Migration
- 없음 (코드-only)

## Test plan
- [ ] make lint (ruff app/+tests/ + ty)
- [ ] uv run pytest tests/test_execution_strength_query_service.py tests/test_mcp_execution_strength.py tests/test_config.py tests/test_services_kis_market_data.py -v
- [ ] operator 읽기 전용 라이브 스모크 (아래) — **이슈 close 게이트**

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Operator 읽기 전용 라이브 스모크 절차 (이슈 close 게이트)** — 머지 후 KRX 정규장(09:00–15:30 KST) 중 실행. 시세 조회 TR만 호출 (주문/감시 mutation 없음):

```bash
cd /Users/mgh3326/work/auto_trader
git pull --ff-only
uv run python - <<'EOF'
import asyncio
import json


async def main() -> None:
    from app.mcp_server.tooling.market_data_quotes import (
        _get_execution_strength_impl,
    )
    from app.services.brokers.kis.client import KISClient

    raw = await KISClient().inquire_execution_strength("012450")
    print("broker raw:", json.dumps(raw, ensure_ascii=False))
    payload = await _get_execution_strength_impl("012450", "kr")
    print("tool payload:", json.dumps(payload, ensure_ascii=False))


asyncio.run(main())
EOF
```

  합격 기준 (전부 충족 시 ROB-485 close):
  1. `execution_strength_pct`가 그럴듯한 float (대략 10–500 범위, HTS 체결강도와 자릿수 일치) — null 아님.
  2. `tick_time`이 세션 내 HHMMSS (예: "094227" 형태)이고 `as_of`와 같은 거래일로 정합.
  3. `trend`가 임계와 일관 (예: 81.82 → "sell_dominant").
  4. `buy_volume`/`sell_volume`은 null (설계대로), `data_state`는 "fresh".
  5. 로그에 `Unmapped API rate limit ... FHKST01010300` 경고 **없음**.
  6. (선택) 거래정지/저유동 종목으로 빈 tick 케이스 → `data_state:"field_unavailable"` + 에러 없이 all-null 확인.

- [ ] **배포 반영 메모**: 배포된 MCP 서버는 릴리즈 빌드 재시작 후에야 신규 코드/도구 설명을 서빙한다 — operator가 배포 후 위 스모크를 MCP `get_execution_strength` 호출로도 1회 재확인.

---

## Follow-up (이 PR 스코프 밖, 선택)

- `docs/runbooks/rob449-452-mcp-activation.md`(lines 15, 168, 383, 397)에 "get_execution_strength DEFERRED (FHPST01060000)" 역사적 노트가 남아 있음 — 도구가 이미 출시됐으므로 stale. 이 PR에 같이 정리하거나 별도 docs 커밋으로 처리.
- per-side 체결량(buy/sell volume)이 정말 필요해지면: REST로는 불가(라이브 검증). WebSocket H0STCNT0(실시간 체결가)의 누적 매수/매도 체결량 필드 기반 별도 스펙 + 라이브 프로브 필요. FHPST01060000(당일시간대별체결)은 미프로브 상태 — 검토 후보.
