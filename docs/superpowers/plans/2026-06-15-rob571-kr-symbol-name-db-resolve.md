# ROB-571 — 체결/watch 알림 KR 종목명 DB 해석 + 제목 중복 제거 Implementation Plan

> REQUIRED SUB-SKILL: test-driven-development. 작은 변경(async 헬퍼 1 + notify 2 + 포맷터 dedup + 테스트). 마이그레이션 0.

**Goal:** 체결/watch Discord 알림에서 KR 종목명을 `kr_symbol_universe`로 해석(011200→HMM). 이름==심볼이면 제목 중복(`X (X)`) 제거.

**근본원인:** `resolve_symbol_display_name`(KR 분기 = 8개 하드코딩 `KR_SYMBOLS` 역맵) → 011200 미존재 → 코드 폴백.

**재사용:** `get_kr_names_by_symbols(symbols, db=None)` (app/services/kr_symbol_universe_service.py:484, async, db 옵션=자체세션, fail-open, ROB-557이 매도이력에 사용).

---

## Task 1: async `resolve_display_name_db`

**Files:** Modify `app/services/fill_notification.py`; Test `tests/test_fill_notification.py`

- [ ] **Step 1: 실패 테스트**
```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_display_name_db_kr_uses_universe(monkeypatch):
    from app.services import fill_notification as fn
    async def fake(symbols, db=None): return {"011200": "HMM"}
    monkeypatch.setattr("app.services.fill_notification.get_kr_names_by_symbols", fake, raising=False)
    assert await fn.resolve_display_name_db("kr", "011200") == "HMM"

@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_display_name_db_kr_failopen(monkeypatch):
    from app.services import fill_notification as fn
    async def boom(symbols, db=None): raise RuntimeError("db down")
    monkeypatch.setattr("app.services.fill_notification.get_kr_names_by_symbols", boom, raising=False)
    # fail-open → sync resolver(미존재면 심볼)
    assert await fn.resolve_display_name_db("kr", "011200") == "011200"

@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_display_name_db_us_and_crypto_passthrough():
    from app.services import fill_notification as fn
    assert await fn.resolve_display_name_db("us", "AAPL") == "AAPL"
    assert await fn.resolve_display_name_db("crypto", "KRW-BTC") == "BTC"
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/test_fill_notification.py -k resolve_display_name_db -v` → FAIL

- [ ] **Step 3: 구현** — `fill_notification.py`에 모듈 함수 import + 신규 async 함수:
```python
from app.services.kr_symbol_universe_service import get_kr_names_by_symbols
# (상단 import; 순환 주의 — kr_symbol_universe_service는 fill_notification을 import하지 않음)

async def resolve_display_name_db(market_type: str | None, symbol: str) -> str:
    """DB(kr_symbol_universe) 기반 KR 종목명 해석. 실패/타시장은 sync 폴백(US 심볼·crypto split)."""
    if market_type == "kr":
        try:
            names = await get_kr_names_by_symbols([symbol])
            name = names.get(symbol)
            if name:
                return name
        except Exception:
            logger.debug("DB display-name resolution failed: %s/%s", market_type, symbol, exc_info=True)
    return resolve_symbol_display_name(market_type, symbol)
```
> import 순환 위험 시 함수 내부 지연 import로 변경(monkeypatch 대상 경로는 `app.services.fill_notification.get_kr_names_by_symbols` 유지 위해 상단 import 권장).

- [ ] **Step 4: 통과** — `uv run pytest tests/test_fill_notification.py -k resolve_display_name_db -v`
- [ ] **Step 5: 커밋** — `feat(ROB-571): DB-backed KR display-name resolver (fail-open)`

---

## Task 2: notify_fill + notify_investment_watch → await DB resolver

**Files:** Modify `app/monitoring/trade_notifier/notifier.py`; Test `tests/test_trade_notifier.py`

- [ ] **Step 1: 실패 테스트** — `notify_fill`(KR order, market_type="kr", symbol="011200")가 `format_fill_notification`에 `display_name="HMM"`을 넘기는지 (resolve_display_name_db를 monkeypatch로 "HMM" 반환). watch도 동일.
```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_fill_uses_db_display_name(trade_notifier, monkeypatch):
    monkeypatch.setattr("app.services.fill_notification.resolve_display_name_db",
                        AsyncMock(return_value="HMM"))
    trade_notifier.configure(bot_token="t", chat_ids=["1"], enabled=True,
                             discord_webhook_kr="https://discord.com/api/webhooks/kr")
    order = FillOrder(symbol="011200", side="ask", filled_price=21600.0, filled_qty=12.0,
                      filled_amount=259200.0, filled_at="t", account="kis",
                      market_type="kr", currency="KRW")
    with patch.object(trade_notifier, "_send_to_discord_embed_single", new=AsyncMock(return_value=True)) as md:
        await trade_notifier.notify_fill(order)
    assert "HMM" in md.await_args.args[0]["title"]
```

- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** —
  - `notify_fill`: `display_name = resolve_fill_display_name(order)` →
    ```python
    from app.services.fill_notification import resolve_display_name_db
    display_name = await resolve_display_name_db(order.market_type, order.symbol)
    ```
  - `notify_investment_watch`: `display_name = resolve_symbol_display_name(payload.market, payload.symbol)` →
    ```python
    from app.services.fill_notification import resolve_display_name_db
    display_name = await resolve_display_name_db(payload.market, payload.symbol)
    ```
- [ ] **Step 4: 통과** — `uv run pytest tests/test_trade_notifier.py -k "notify_fill or investment_watch" -v`
- [ ] **Step 5: 커밋** — `feat(ROB-571): resolve fill/watch display name via DB universe`

---

## Task 3: 제목 중복 제거 (name==symbol → 심볼만)

**Files:** Modify `formatters_discord.py`, `formatters_telegram.py`; Test the two formatter test files

- [ ] **Step 1: 실패 테스트** — `format_fill_notification(order(symbol="011200"), display_name="011200")` 제목 == `"🔴 체결 · 011200"` (중복 `(011200)` 없음); `display_name="HMM"`이면 `"🔴 체결 · HMM (011200)"`. watch + telegram 동형.

- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — 작은 헬퍼 + 제목 라인 교체:
  - `formatters_discord.py` 상단:
    ```python
    def _title_label(display_name: str, symbol: str) -> str:
        return symbol if display_name == symbol else f"{display_name} ({symbol})"
    ```
    fill 제목(585): `f"{side_emoji} {fill_label} · {_title_label(display_name, order.symbol)}"`.
    watch 제목(665): `f"🔔 워치 트리거 · {_title_label(display_name, payload.symbol)}"`.
  - `formatters_telegram.py` 상단(이스케이프 버전):
    ```python
    def _title_label_tg(display_name: str, symbol: str) -> str:
        return symbol if display_name == symbol else f"{display_name} \\({symbol}\\)"
    ```
    fill(359): `f"*{side_emoji} {fill_label} · {_title_label_tg(display_name, order.symbol)}*"`.
    watch(412): `f"*🔔 워치 트리거 · {_title_label_tg(display_name, payload.symbol)}*"`.
- [ ] **Step 4: 통과** — 두 포맷터 테스트 그린
- [ ] **Step 5: 커밋** — `feat(ROB-571): collapse redundant 'name (symbol)' title when equal`

---

## Task 4: 검증
- [ ] `uv run ruff format app/ tests/ && uv run ruff check app/ tests/ && uv run ty check app/`
- [ ] `uv run pytest tests/ --collect-only -q` → 0 에러
- [ ] `uv run pytest tests/test_fill_notification.py tests/test_trade_notifier.py tests/test_trade_notifier_formatters_discord.py tests/test_trade_notifier_formatters_telegram.py -q` → green
- [ ] 커밋 + PR

## 비목표
KR_SYMBOLS 제거(normalize_kr_symbol 유지), US 회사명/crypto 한글명 전환, watch 외 알림.
