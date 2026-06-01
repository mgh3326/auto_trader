# ROB-420 매도 라우팅 UX 함정 해소 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** get_holdings 계좌그룹에 `order_routable` 메타를 추가하고(toss/samsung=reference-only→False), kis_live/kis_mock 매도 실패 메시지를 "다른 브로커 서브계좌 보유라 이 채널로 매도 불가"로 명확화해 "보이는데 못 파는" UX 함정을 해소한다.

**Architecture:** 두 read-only 순수 헬퍼 추가 — `_account_order_routable`(source 기반 sellability) + `_no_holdings_sell_message`(market/mock 인지 메시지). get_holdings 그룹 dict에 additive 필드, 매도 실패 2개 지점이 새 메시지 사용. account_mode·order path·broker mutation 무변경.

**Tech Stack:** Python 3.13, pytest(asyncio + monkeypatch). 순수 함수 중심, DB/네트워크 없음.

---

## File Structure

- Modify: `app/mcp_server/tooling/portfolio_holdings.py` — `_account_order_routable` 헬퍼 + grouped_accounts에 `order_routable` (Unit 1)
- Modify: `app/mcp_server/tooling/order_validation.py` — `_no_holdings_sell_message` 헬퍼 + 매도 실패 2개 지점 (Unit 2)
- Test: `tests/test_mcp_holdings_account_mode_provenance.py` — Unit 1 (manual fixture + order_routable)
- Create: `tests/test_order_sell_routability_message.py` — Unit 2 (순수 메시지 헬퍼)

---

## Task 1: get_holdings `order_routable` 메타 (Unit 1)

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_holdings.py`
- Test: `tests/test_mcp_holdings_account_mode_provenance.py`

배경: `_get_holdings_impl`의 `grouped_accounts`(라인 1006-1024)는 계좌그룹에 broker/account_mode를 담지만, manual(toss/samsung) 그룹은 `_provenance_account_mode`가 `routing_mode`("kis_live")를 그대로 줘서 매도 가능처럼 오인됨. 매도가능 권위 신호 `order_routable`을 additive로 추가.

- [ ] **Step 1: Write the failing test**

`tests/test_mcp_holdings_account_mode_provenance.py`에 manual fixture + 2개 테스트 추가. 파일 상단엔 이미 `from app.mcp_server.tooling import portfolio_holdings`, `_upbit_position`/`_kis_position` 헬퍼, `DummyMCP` import가 있음.

```python
def _manual_position(symbol: str = "AAPL", broker: str = "toss") -> dict:
    return {
        "account": f"{broker}:기본 계좌",
        "account_name": "기본 계좌",
        "broker": broker,
        "source": "manual",
        "instrument_type": "equity_us",
        "market": "us",
        "symbol": symbol,
        "name": symbol,
        "quantity": 2.0,
        "avg_buy_price": 100.0,
        "current_price": None,
        "evaluation_amount": None,
        "profit_loss": None,
        "profit_rate": None,
    }


def test_account_order_routable_manual_is_false():
    assert portfolio_holdings._account_order_routable(source="manual") is False


def test_account_order_routable_brokered_sources_true():
    assert portfolio_holdings._account_order_routable(source="kis_api") is True
    assert portfolio_holdings._account_order_routable(source="upbit_api") is True


@pytest.mark.asyncio
async def test_get_holdings_impl_marks_manual_group_not_routable(monkeypatch):
    async def fake_collect(**_kwargs):
        return (
            [_kis_position("005930"), _manual_position("AAPL", broker="toss")],
            [],
            None,
            None,
        )

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    result = await portfolio_holdings._get_holdings_impl(
        include_current_price=False,
        routing_account_mode="kis_live",
    )

    by_account = {a["account"]: a for a in result["accounts"]}
    # KIS subaccount is sellable via the order channel; toss is reference-only.
    assert by_account["kis"]["order_routable"] is True
    toss_group = next(
        a for k, a in by_account.items() if a["broker"] == "toss"
    )
    assert toss_group["order_routable"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-420 && uv run pytest tests/test_mcp_holdings_account_mode_provenance.py -k "order_routable or not_routable" -v`
Expected: FAIL — `_account_order_routable` AttributeError / `order_routable` 키 없음(KeyError).

- [ ] **Step 3: Write minimal implementation**

`app/mcp_server/tooling/portfolio_holdings.py`:

(a) `_provenance_account_mode` 정의(라인 138 근처) 바로 뒤에 순수 헬퍼 추가:

```python
def _account_order_routable(*, source: str | None) -> bool:
    """Whether an account group's holdings are routable by an automated order tool.

    Manual holdings (toss/samsung/수동 입력, ``source="manual"``) are reference-only
    and cannot be sold via kis_live/kis_mock (or any) order tool. Everything else
    (``kis_api`` / ``upbit_api`` / paper sources) sells via its own channel. This is
    the authoritative sellability signal; ``account_mode`` stays a provenance label
    (ROB-357) and is intentionally left unchanged.
    """
    return source != "manual"
```

(b) `grouped_accounts` setdefault dict(라인 1010-1024)에 `order_routable` 추가:

```python
        grouped = grouped_accounts.setdefault(
            account_id,
            {
                "account": account_id,
                "broker": position["broker"],
                "account_name": position["account_name"],
                # ROB-357 — per-account provenance label so an Upbit group
                # never inherits the KIS routing default.
                "account_mode": _provenance_account_mode(
                    broker=position.get("broker"),
                    source=position.get("source"),
                    routing_mode=routing_account_mode,
                ),
                # ROB-420 — authoritative sellability: manual (toss/samsung)
                # holdings are reference-only and not routable by order tools.
                "order_routable": _account_order_routable(
                    source=position.get("source")
                ),
                "positions": [],
            },
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-420 && uv run pytest tests/test_mcp_holdings_account_mode_provenance.py -v`
Expected: PASS — 신규 3 + 기존 ROB-357 전부 green.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-420
git add app/mcp_server/tooling/portfolio_holdings.py tests/test_mcp_holdings_account_mode_provenance.py
git commit -m "feat(ROB-420): get_holdings 계좌그룹에 order_routable 메타(manual=reference-only)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 매도 실패 메시지 명확화 헬퍼 (Unit 2 — 순수 함수)

**Files:**
- Modify: `app/mcp_server/tooling/order_validation.py`
- Create: `tests/test_order_sell_routability_message.py`

배경: 매도 실패가 KIS 서브계좌 미보유일 때 `"No holdings found"`만 반환 → "안 가짐"과 "다른 브로커 서브계좌 보유라 이 채널 불가"를 구분 못함. user_id가 order path에 없으므로 특정 broker명 대신 제너릭·시장인지 메시지로 명확화.

- [ ] **Step 1: Write the failing test**

`tests/test_order_sell_routability_message.py` 신규 생성:

```python
"""ROB-420 — sell-failure message disambiguates KIS-subaccount scoping."""

from __future__ import annotations

from app.mcp_server.tooling.order_validation import _no_holdings_sell_message


def test_equity_kr_message_names_kis_subaccount_and_routable_hint():
    msg = _no_holdings_sell_message("005930", "equity_kr", is_mock=False)
    assert "kis_live" in msg
    assert "reference-only" in msg
    assert "order_routable" in msg


def test_equity_us_mock_message_uses_kis_mock_channel():
    msg = _no_holdings_sell_message("AAPL", "equity_us", is_mock=True)
    assert "kis_mock" in msg
    assert "toss/samsung" in msg


def test_crypto_message_is_upbit_not_kis():
    msg = _no_holdings_sell_message("KRW-BTC", "crypto", is_mock=False)
    assert "Upbit" in msg
    assert "kis_live" not in msg
    assert "kis_mock" not in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-420 && uv run pytest tests/test_order_sell_routability_message.py -v`
Expected: FAIL — `ImportError: cannot import name '_no_holdings_sell_message'`.

- [ ] **Step 3: Write minimal implementation**

`app/mcp_server/tooling/order_validation.py` — `_get_holdings_for_order`(라인 316) 정의 바로 앞(또는 근처 모듈 스코프)에 순수 헬퍼 추가:

```python
def _no_holdings_sell_message(symbol: str, market_type: str, is_mock: bool) -> str:
    """Disambiguate a sell-side holdings miss (ROB-420).

    For equities the order tools route only to the KIS subaccount, so a miss may
    mean the symbol is held in another (reference-only) broker subaccount rather
    than not held at all. Crypto routes to Upbit, so keep an Upbit-specific note.
    """
    if market_type == "crypto":
        return f"No holdings found for {symbol} on Upbit"
    channel = "kis_mock" if is_mock else "kis_live"
    return (
        f"No sellable holdings for {symbol} in the KIS subaccount that "
        f"{channel} routes to. Holdings in other broker subaccounts "
        f"(e.g. toss/samsung) are reference-only and cannot be sold via this "
        f"channel — check get_holdings 'order_routable'/'account_mode'."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-420 && uv run pytest tests/test_order_sell_routability_message.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-420
git add app/mcp_server/tooling/order_validation.py tests/test_order_sell_routability_message.py
git commit -m "feat(ROB-420): 매도 실패 메시지 KIS-서브계좌 사유 명확화 헬퍼

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: 매도 실패 2개 지점에 헬퍼 배선

**Files:**
- Modify: `app/mcp_server/tooling/order_validation.py`

`_preview_sell`(라인 557)와 `_validate_sell_side`(라인 731)의 평평한 메시지를 헬퍼로 교체. 두 함수 모두 `symbol`/`market_type`/`is_mock` in scope.

- [ ] **Step 1: Write the failing test**

`tests/test_order_sell_routability_message.py`에 배선 검증 추가. `_preview_sell`/`_validate_sell_side`가 `_get_holdings_for_order`를 호출하므로, 그것을 None 반환으로 monkeypatch해 메시지를 확인:

```python
import pytest

from app.mcp_server.tooling import order_validation


@pytest.mark.asyncio
async def test_preview_sell_uses_routability_message(monkeypatch):
    async def fake_holdings(*_a, **_k):
        return None

    monkeypatch.setattr(order_validation, "_get_holdings_for_order", fake_holdings)
    result = await order_validation._preview_sell(
        symbol="AAPL",
        order_type="limit",
        quantity=1.0,
        price=100.0,
        current_price=100.0,
        market_type="equity_us",
        is_mock=False,
    )
    assert "reference-only" in result["error"]
    assert "kis_live" in result["error"]


@pytest.mark.asyncio
async def test_validate_sell_side_uses_routability_message(monkeypatch):
    async def fake_holdings(*_a, **_k):
        return None

    captured: dict[str, str] = {}

    def order_error(msg: str) -> dict[str, str]:
        captured["msg"] = msg
        return {"error": msg}

    monkeypatch.setattr(order_validation, "_get_holdings_for_order", fake_holdings)
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="AAPL",
        normalized_symbol="AAPL",
        market_type="equity_us",
        quantity=1.0,
        order_type="limit",
        price=100.0,
        current_price=100.0,
        order_error_fn=order_error,
        is_mock=True,
    )
    assert err is not None
    assert "kis_mock" in captured["msg"]
    assert "reference-only" in captured["msg"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-420 && uv run pytest tests/test_order_sell_routability_message.py -k "uses_routability_message" -v`
Expected: FAIL — 현재 메시지는 `"No holdings found"`라 `reference-only`/`kis_live` 미포함.

- [ ] **Step 3: Write minimal implementation**

`app/mcp_server/tooling/order_validation.py`:

(a) `_preview_sell` 라인 557 교체:

```python
    holdings = await _get_holdings_for_order(symbol, market_type, is_mock=is_mock)
    if not holdings:
        result["error"] = _no_holdings_sell_message(symbol, market_type, is_mock)
        return result
```

(b) `_validate_sell_side` 라인 731 교체:

```python
    if not holdings:
        return 0.0, 0.0, order_error_fn(
            _no_holdings_sell_message(symbol, market_type, is_mock)
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-420 && uv run pytest tests/test_order_sell_routability_message.py -v`
Expected: PASS (신규 전건).

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-420
git add app/mcp_server/tooling/order_validation.py tests/test_order_sell_routability_message.py
git commit -m "feat(ROB-420): 매도 실패 2개 지점에 routability 메시지 배선

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 회귀 + lint 검증

**Files:**
- (없음 — 검증만)

- [ ] **Step 1: 매도 메시지 기존 단언 회귀 확인**

기존 테스트가 `"No holdings found"` 문자열을 단언하면 새 메시지로 깨질 수 있음. 먼저 탐색:

Run: `cd /Users/mgh3326/work/auto_trader.rob-420 && grep -rn "No holdings found" tests/`
조치: 매치되는 테스트가 있으면 해당 단언을 새 메시지(`"No sellable holdings"` / crypto `"on Upbit"`)에 맞게 갱신하고 그 파일을 함께 커밋. (없으면 스킵.)

- [ ] **Step 2: 관련 회귀 스위트**

Run: `cd /Users/mgh3326/work/auto_trader.rob-420 && uv run pytest tests/test_mcp_holdings_account_mode_provenance.py tests/test_order_sell_routability_message.py tests/test_kis_mock_scalping_sell_guard.py tests/test_kis_mock_routing.py -q`
Expected: PASS — provenance/sell-guard/routing 회귀 green.

- [ ] **Step 3: Lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-420 && uv run ruff check app/mcp_server/tooling/portfolio_holdings.py app/mcp_server/tooling/order_validation.py tests/test_order_sell_routability_message.py && uv run ruff format --check app/mcp_server/tooling/portfolio_holdings.py app/mcp_server/tooling/order_validation.py tests/test_order_sell_routability_message.py`
Expected: All checks passed / already formatted.

- [ ] **Step 4: Mutation import guard (read-only invariant)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-420 && uv run pytest -k "import_guard or mutation_guard" -q 2>&1 | tail -5`
Expected: green (order_validation/portfolio_holdings에 broker order-mutation 미도입). guard 없으면 스킵.

- [ ] **Step 5: 변경 없으면 커밋 불필요. Step 1에서 테스트 갱신했다면 이미 커밋됨.**

---

## Self-Review 결과

**Spec 커버리지:**
- Unit 1 (order_routable 메타) → Task 1 ✅
- Unit 2 (매도 실패 메시지 헬퍼) → Task 2 ✅
- 2개 지점 배선 → Task 3 ✅
- ROB-357 회귀 무변경 + lint + 기존 "No holdings found" 단언 갱신 → Task 4 ✅

**Placeholder 스캔:** 없음 — 모든 코드 step에 실제 코드 포함.

**Type 일관성:** `_account_order_routable(*, source: str | None) -> bool`, `_no_holdings_sell_message(symbol, market_type, is_mock) -> str` 시그니처 Task 1/2 정의와 Task 3 호출부 일치. `order_routable` 키명 Task 1(구현)·테스트 일관.

**안전 경계 재확인:** read-only/UX, account_mode 무변경(additive only), broker/order/watch/order-intent mutation 없음, migration 0. place_order user_id 스레딩 미수행(Non-goal). Toss reference를 sellable로 병합하지 않음(order_routable=False로 명시 분리).
