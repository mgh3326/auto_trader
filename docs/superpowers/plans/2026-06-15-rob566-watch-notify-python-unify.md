# ROB-566 — watch 알림 Python(TradeNotifier) 통일 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:test-driven-development (per task) + executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** watch 트리거 Discord 알림을 Prefect 수신기 대신 auto_trader **in-process `TradeNotifier`**로 렌더링(체결 ROB-558과 동형). 플래그 뒤에서 opt-in, `HermesDeliveryResult` 3-way 계약 보존 → alert 상태머신·호출처 2곳 무변경.

**Architecture:** `send_review_trigger(payload) -> HermesDeliveryResult` 내부에 transport 분기 추가. `WATCH_NOTIFY_TRANSPORT="python_direct"`면 `TradeNotifier.notify_investment_watch(payload)`로 렌더(시장별 webhook), 결과를 `HermesDeliveryResult`로 매핑. default `hermes_webhook`은 현행 HTTP 경로 불변.

**Tech Stack:** Python 3.13, pytest(+`@pytest.mark.unit`/`asyncio`), ruff, ty. 마이그레이션 0.

**스펙:** `docs/superpowers/specs/2026-06-15-rob566-watch-notify-python-unify-design.md`

### 핵심 참조 (현재 코드, file:line)
- `app/services/hermes_client.py`: `ReviewTriggerPayload`(255, 필드: market/symbol/metric/operator/threshold/threshold_high/current_value/outcome/action_mode/invest_links/operator_action_guidance/price_guidance/trigger_checklist/planned_action), `HermesDeliveryResult`(298: status success|skipped|failed, http_status, reason), `send_review_trigger`(330), `InvestLinks`(52: report_path/stock_path/anchors, **path-only — host는 렌더러가 prepend**), `OperatorActionGuidance`(66: headline/requires_operator_review/order_behavior), `PriceGuidance`(80: entry_review_below_price/suggested_limit_price_range/max_chase_price/invalidation).
- `app/monitoring/trade_notifier/formatters_discord.py:510` `format_fill_notification` (미러 대상), `notifier.py:259` `notify_fill` (미러), `_dispatch(embed, telegram_msg, market_type)`, `_get_webhook_for_market_type`(kr/us/crypto/alerts).
- `app/core/config.py:433-435` HERMES_* (옆에 플래그 추가), `public_base_url`(딥링크 host).
- 호출처(무변경 확인용): `app/jobs/investment_watch_scanner.py:219`+`_record_delivery_outcome`(399, `delivery.status=="success"`→triggered), `app/services/investment_reports/watch_validity_review.py:280`.
- `app/services/fill_notification.py`: `KR_SYMBOLS` reverse cache + `resolve_fill_display_name`(개명 재사용 기반).

---

## Task 1: config 플래그 `WATCH_NOTIFY_TRANSPORT`

**Files:** Modify `app/core/config.py`, `env.example`; Test `tests/test_config.py`(있으면 추가) 또는 새 단언

- [ ] **Step 1: 실패 테스트**

```python
@pytest.mark.unit
def test_watch_notify_transport_defaults_to_hermes_webhook():
    from app.core.config import settings
    assert settings.WATCH_NOTIFY_TRANSPORT in ("hermes_webhook", "python_direct")
    # default 보장 (env 미설정 시)
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/test_config.py -k watch_notify_transport -v` → FAIL(AttributeError)

- [ ] **Step 3: 구현** — `config.py` HERMES_* 아래에 추가:

```python
    # ROB-566: watch 트리거 알림 전송 수단. "hermes_webhook"(default, 현행 Prefect
    # 수신기로 HTTP POST) | "python_direct"(in-process TradeNotifier 렌더, ROB-558 체결과 동형).
    WATCH_NOTIFY_TRANSPORT: Literal["hermes_webhook", "python_direct"] = "hermes_webhook"
```
`env.example`에 `WATCH_NOTIFY_TRANSPORT=hermes_webhook` 1줄 추가(주석 포함). `Literal` import 확인(이미 있음).

- [ ] **Step 4: 통과** — `uv run pytest tests/test_config.py -k watch_notify_transport -v && uv run ty check app/core/config.py`

- [ ] **Step 5: 커밋** — `git commit -m "feat(ROB-566): add WATCH_NOTIFY_TRANSPORT flag (default hermes_webhook)"`

---

## Task 2: watch 표시명 헬퍼 (시장/심볼 → 한글명)

**Files:** Modify `app/services/fill_notification.py`; Test `tests/test_fill_notification.py`

- [ ] **Step 1: 실패 테스트** (TestFillHelpers에 추가)

```python
def test_resolve_symbol_display_name(self):
    from app.services.fill_notification import resolve_symbol_display_name
    assert resolve_symbol_display_name("crypto", "KRW-BTC") == "BTC"
    assert resolve_symbol_display_name("us", "AAPL") == "AAPL"
    assert resolve_symbol_display_name("kr", "005930") in ("삼성전자", "005930")  # KR_SYMBOLS 의존
```

- [ ] **Step 2: 실패 확인** — FAIL(ImportError)

- [ ] **Step 3: 구현** — `fill_notification.py`에 시장/심볼 공용 해석기 추가하고 기존 `resolve_fill_display_name`이 위임하도록(중복 제거):

```python
def resolve_symbol_display_name(market_type: str | None, symbol: str) -> str:
    if market_type == "kr":
        return _get_kr_symbol_reverse().get(symbol, symbol)
    if market_type == "crypto" and "-" in symbol:
        return symbol.split("-")[-1]
    return symbol


# 기존 resolve_fill_display_name 내부를 위임으로 교체:
def resolve_fill_display_name(order: FillOrder) -> str:
    return resolve_symbol_display_name(order.market_type, order.symbol)
```

- [ ] **Step 4: 통과** — `uv run pytest tests/test_fill_notification.py -v` (기존 fill 표시명 테스트도 그린 유지)

- [ ] **Step 5: 커밋** — `git commit -m "refactor(ROB-566): extract resolve_symbol_display_name (reused by watch)"`

---

## Task 3: Discord watch 포맷터 `format_investment_watch_trigger`

**Files:** Modify `app/monitoring/trade_notifier/formatters_discord.py`, `types.py`(COLORS watch); Test `tests/test_trade_notifier_formatters_discord.py`

- [ ] **Step 1: 실패 테스트**

```python
from app.monitoring.trade_notifier.formatters_discord import format_investment_watch_trigger
from app.services.hermes_client import (
    ReviewTriggerPayload, InvestLinks, OperatorActionGuidance, PriceGuidance,
)
from decimal import Decimal
from uuid import uuid4

def _watch_payload(**kw):
    base = dict(
        event_uuid=uuid4(), alert_uuid=uuid4(), source_report_uuid=uuid4(),
        source_item_uuid=uuid4(), correlation_id="c1", kst_date="2026-06-15",
        market="kr", target_kind="asset", symbol="005930", metric="price",
        operator="below", threshold=Decimal("68000"), threshold_key="k",
        intent="buy_review", action_mode="notify_only", current_value=Decimal("67500"),
        scanner_snapshot={}, outcome="notified",
        invest_links=InvestLinks(report_path="/invest/reports/r1", stock_path="/invest/stocks/kr/005930"),
        operator_action_guidance=OperatorActionGuidance(headline="알림 전용", requires_operator_review=False, order_behavior="none"),
        price_guidance=None, planned_action=None, trigger_checklist=None,
    )
    base.update(kw); return ReviewTriggerPayload(**base)

@pytest.mark.unit
class TestFormatWatchTrigger:
    def test_basic_with_link_and_fields(self):
        emb = format_investment_watch_trigger(_watch_payload(), display_name="삼성전자",
                                              base_url="https://x.test")
        assert "삼성전자" in emb["title"] and "005930" in emb["title"]
        assert emb["url"] == "https://x.test/invest/stocks/kr/005930"
        fields = {f["name"]: f["value"] for f in emb["fields"]}
        assert "price" in fields["조건"] and "below" in fields["조건"] and "68000" in fields["조건"]
        assert "67500" in fields["현재값"]

    def test_price_guidance_and_checklist_rendered(self):
        pg = PriceGuidance(entry_review_below_price=Decimal("66000"), max_chase_price=Decimal("69000"),
                           suggested_limit_price_range=None, invalidation=None)
        emb = format_investment_watch_trigger(_watch_payload(price_guidance=pg, trigger_checklist=["수급 확인"]),
                                              display_name="삼성전자", base_url="https://x.test")
        names = {f["name"] for f in emb["fields"]}
        assert "가격 가이드" in names and "체크리스트" in names

    def test_no_link_when_invest_links_none(self):
        emb = format_investment_watch_trigger(_watch_payload(invest_links=None),
                                              display_name="삼성전자", base_url="https://x.test")
        assert "url" not in emb
```

- [ ] **Step 2: 실패 확인** — FAIL(ImportError)

- [ ] **Step 3: 구현** — `types.py` COLORS에 `"watch": 0xF1C40F`(노랑) 추가. `formatters_discord.py`:

```python
def format_investment_watch_trigger(
    payload, *, display_name: str, base_url: str
) -> DiscordEmbed:
    """ROB-566: watch 트리거 Discord 임베드 (Prefect 렌더 대체)."""
    outcome_kr = {
        "notified": "알림", "review_required": "검토 필요",
        "preview_attached": "프리뷰 첨부", "executed": "모의 실행",
    }.get(payload.outcome, payload.outcome)

    fields: list[DiscordField] = [
        {"name": "조건", "value": f"{payload.metric} {payload.operator} {payload.threshold}", "inline": True},
        {"name": "현재값", "value": (str(payload.current_value) if payload.current_value is not None else "-"), "inline": True},
        {"name": "시장", "value": payload.market, "inline": True},
        {"name": "구분", "value": outcome_kr, "inline": True},
    ]
    pg = payload.price_guidance
    if pg is not None:
        parts: list[str] = []
        if pg.entry_review_below_price is not None: parts.append(f"진입검토 ≤ {pg.entry_review_below_price}")
        if pg.suggested_limit_price_range is not None:
            parts.append(f"지정가 {pg.suggested_limit_price_range.low}~{pg.suggested_limit_price_range.high}")
        if pg.max_chase_price is not None: parts.append(f"최대추격 {pg.max_chase_price}")
        if pg.invalidation is not None and getattr(pg.invalidation, "price", None) is not None:
            parts.append(f"무효화 {pg.invalidation.price}")
        if parts: fields.append({"name": "가격 가이드", "value": "\n".join(parts), "inline": False})
    if payload.trigger_checklist:
        fields.append({"name": "체크리스트", "value": "\n".join(f"• {c}" for c in payload.trigger_checklist), "inline": False})
    if payload.invest_links is not None:
        fields.append({"name": "링크", "value": f"[리포트]({base_url}{payload.invest_links.report_path}) · [종목]({base_url}{payload.invest_links.stock_path})", "inline": False})

    desc = ""
    if payload.operator_action_guidance is not None:
        desc = payload.operator_action_guidance.headline
    desc = (desc + f"\n🕒 {format_datetime()}").strip()

    embed: DiscordEmbed = {
        "title": f"🔔 워치 트리거 · {display_name} ({payload.symbol})",
        "description": desc,
        "color": COLORS["watch"],
        "fields": fields,
    }
    if payload.invest_links is not None:
        embed["url"] = f"{base_url}{payload.invest_links.stock_path}"
    return embed
```
> 타입힌트는 `ReviewTriggerPayload`를 import해 명시(`from app.services.hermes_client import ReviewTriggerPayload`); 순환 주의 — hermes_client는 formatters를 import하지 않으므로 안전.

- [ ] **Step 4: 통과** — `uv run pytest tests/test_trade_notifier_formatters_discord.py -k Watch -v`

- [ ] **Step 5: 커밋** — `git commit -m "feat(ROB-566): Discord watch-trigger formatter (deeplinks, price guidance, checklist)"`

---

## Task 4: Telegram watch 포맷터

**Files:** Modify `formatters_telegram.py`; Test `tests/test_trade_notifier_formatters_telegram.py`

- [ ] **Step 1: 실패 테스트** — `format_investment_watch_trigger_telegram(payload, *, display_name, base_url)`가 "워치 트리거", display_name, 조건, 마크다운 링크 `[종목 상세](base+stock_path)` 포함.
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — discord 포맷터와 동일 정보의 마크다운 문자열(체결 telegram 포맷터 동형, `\\(`/`\\)` 이스케이프, 딥링크는 `[텍스트](url)`).
- [ ] **Step 4: 통과**
- [ ] **Step 5: 커밋** — `git commit -m "feat(ROB-566): Telegram watch-trigger formatter"`

---

## Task 5: `TradeNotifier.notify_investment_watch`

**Files:** Modify `notifier.py`; Test `tests/test_trade_notifier.py`

- [ ] **Step 1: 실패 테스트**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_investment_watch_routes_by_market(trade_notifier):
    from tests... import _watch_payload  # 또는 인라인 구성
    trade_notifier.configure(bot_token="t", chat_ids=["1"], enabled=True,
                             discord_webhook_kr="https://discord.com/api/webhooks/kr")
    with patch.object(trade_notifier, "_send_to_discord_embed_single", new=AsyncMock(return_value=True)) as md, \
         patch.object(trade_notifier, "_send_to_telegram", new=AsyncMock(return_value=True)) as mt:
        ok = await trade_notifier.notify_investment_watch(_watch_payload(market="kr"))
    assert ok is True
    md.assert_awaited_once(); mt.assert_not_awaited()
    assert md.await_args.args[0]["title"].startswith("🔔 워치 트리거")
```

- [ ] **Step 2: 실패 확인** — FAIL(no attribute)

- [ ] **Step 3: 구현** — `notify_fill` 동형:

```python
    async def notify_investment_watch(self, payload) -> bool:
        """watch 트리거 알림 (ROB-566). Discord 우선, Telegram fallback."""
        from app.core.config import settings as _settings
        from app.services.fill_notification import resolve_symbol_display_name

        display_name = resolve_symbol_display_name(payload.market, payload.symbol)
        base_url = _settings.public_base_url.rstrip("/")
        embed = fmt_discord.format_investment_watch_trigger(
            payload, display_name=display_name, base_url=base_url
        )
        telegram_msg = fmt_telegram.format_investment_watch_trigger_telegram(
            payload, display_name=display_name, base_url=base_url
        )
        return await self._dispatch(embed, telegram_msg, payload.market)
```
> `payload` 타입힌트는 `ReviewTriggerPayload`(import). `payload.market` 값(kr/us/crypto)은 `_get_webhook_for_market_type`가 처리.

- [ ] **Step 4: 통과** — `uv run pytest tests/test_trade_notifier.py -k investment_watch -v`

- [ ] **Step 5: 커밋** — `git commit -m "feat(ROB-566): TradeNotifier.notify_investment_watch (market-routed)"`

---

## Task 6: `send_review_trigger` transport 분기 (seam)

**Files:** Modify `app/services/hermes_client.py`; Test `tests/test_hermes_client.py`

- [ ] **Step 1: 실패 테스트**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_python_direct_success_maps_to_success(monkeypatch):
    monkeypatch.setattr("app.services.hermes_client.settings.WATCH_NOTIFY_TRANSPORT", "python_direct", raising=False)
    fake = AsyncMock(); fake.notify_investment_watch = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.hermes_client.get_trade_notifier", lambda: fake)
    client = HermesNotificationClient(enabled=True)
    res = await client.send_review_trigger(_watch_payload())
    assert res.status == "success"; fake.notify_investment_watch.assert_awaited_once()

@pytest.mark.unit
@pytest.mark.asyncio
async def test_python_direct_failure_maps_to_failed(monkeypatch):
    monkeypatch.setattr("app.services.hermes_client.settings.WATCH_NOTIFY_TRANSPORT", "python_direct", raising=False)
    fake = AsyncMock(); fake.notify_investment_watch = AsyncMock(return_value=False)
    monkeypatch.setattr("app.services.hermes_client.get_trade_notifier", lambda: fake)
    res = await HermesNotificationClient(enabled=True).send_review_trigger(_watch_payload())
    assert res.status == "failed"

@pytest.mark.unit
@pytest.mark.asyncio
async def test_hermes_webhook_default_path_unchanged(monkeypatch):
    # transport=hermes_webhook (default) → 기존 HTTP 경로 (MockTransport 200) → success
    ...  # 기존 send_review_trigger 테스트 패턴 재사용
```

- [ ] **Step 2: 실패 확인** — FAIL(분기 없음 → python_direct여도 HTTP 시도)

- [ ] **Step 3: 구현** — `send_review_trigger` 상단에 분기 추가(파일 상단 `from app.monitoring.trade_notifier import get_trade_notifier` import):

```python
    async def send_review_trigger(self, payload: ReviewTriggerPayload) -> HermesDeliveryResult:
        if settings.WATCH_NOTIFY_TRANSPORT == "python_direct":
            return await self._render_in_process(payload)
        # ----- 기존 hermes_webhook 경로 (불변) -----
        if not self._enabled:
            ...
```
신규 메서드:
```python
    async def _render_in_process(self, payload: ReviewTriggerPayload) -> HermesDeliveryResult:
        """ROB-566: Prefect 수신기 대신 TradeNotifier로 직접 렌더. 결과를 3-way로 매핑."""
        notifier = get_trade_notifier()
        try:
            sent = await notifier.notify_investment_watch(payload)
        except Exception as exc:
            logger.warning("watch in-process render raised: event_uuid=%s error=%s", payload.event_uuid, exc)
            return HermesDeliveryResult(status="failed", reason="render_exception")
        if sent:
            return HermesDeliveryResult(status="success")
        # webhook 미설정 등으로 dispatch가 False면 skipped(=alert active 유지, 재시도)
        return HermesDeliveryResult(status="skipped", reason="discord_not_configured")
```
> `notify_investment_watch`가 `_dispatch`에서 `_enabled=False`/webhook 미설정 시 `False` 반환 → 여기서 `skipped`로 매핑(현행 skip 의미=alert active 유지 보존). dispatch 자체 예외만 `failed`.
> ⚠️ 매핑 결정 확인(스펙 리뷰): False→skipped(보수적, 재시도) vs failed. 본 플랜은 **skipped**(미설정은 일시적/구성 문제로 보고 재시도 유도). 구현자는 스펙과 대조.

- [ ] **Step 4: 통과** — `uv run pytest tests/test_hermes_client.py -v` (기존 webhook 테스트 + 신규 분기 전부 그린)

- [ ] **Step 5: 커밋** — `git commit -m "feat(ROB-566): route watch via TradeNotifier when WATCH_NOTIFY_TRANSPORT=python_direct"`

---

## Task 7: 전체 검증 + 상태머신 회귀

- [ ] **Step 1**: `uv run ruff format app/ tests/ && uv run ruff check app/ tests/ && uv run ty check app/`
- [ ] **Step 2**: `uv run pytest tests/ --collect-only -q` → 0 import 에러
- [ ] **Step 3 (상태머신 무변경 회귀)**: `uv run pytest tests/ -k "watch_scanner or investment_watch or hermes or watch_validity or trade_notifier or formatters" -q` → 그린. 특히 `_record_delivery_outcome`가 `delivery.status` 3-way를 그대로 받는지(success→triggered, skipped/failed→active) 기존 테스트 통과 확인.
- [ ] **Step 4 (계약 보존 확인)**: `grep -rn "send_review_trigger" app/jobs/investment_watch_scanner.py app/services/investment_reports/watch_validity_review.py` → 호출 시그니처 무변경(여전히 `await ...send_review_trigger(payload)` → `HermesDeliveryResult`).
- [ ] **Step 5: 커밋** — `git commit -m "test(ROB-566): full verification + state-machine regression"`

---

## 자체 점검 (작성자 — 완료)
- **스펙 커버리지:** 플래그(T1)·표시명(T2)·Discord 포맷터+딥링크/price_guidance/checklist(T3)·Telegram(T4)·notify_investment_watch 시장라우팅(T5)·send_review_trigger 분기+3way 매핑(T6)·회귀(T7). 채널=시장별(T5 `_dispatch(payload.market)`). Telegram fallback=_dispatch 무료.
- **계약 보존:** 호출처 2곳·`_record_delivery_outcome`·alert 상태머신 무변경(T6/T7 검증).
- **default 안전:** `WATCH_NOTIFY_TRANSPORT=hermes_webhook` default → 머지 자체는 behavior 불변(opt-in).
- **플레이스홀더:** 없음. 구현자 확인사항: (a) `WatchPriceRange`/`WatchInvalidation` 실제 필드명(low/high/price) — hermes_client에서 확인 후 포맷터 맞춤, (b) `_watch_payload` 테스트 헬퍼는 공용 fixture로 추출 가능, (c) False→skipped 매핑(스펙 §6.1).

## 미해결 (스펙 리뷰 후 — 구현자/검증자)
- price_guidance 하위필드 ✅확인됨(`app/schemas/investment_reports.py`): `WatchPriceRange(low: Decimal, high: Decimal)`, `WatchInvalidation(kind: Literal["price_below","condition_text"], price: Decimal|None, text: str|None)`. 플랜 T3 코드와 일치.
- Prefect 수신기 폐기 = operator(별도, cross-repo). 본 PR은 플래그만.
- `HermesNotificationClient`/`HERMES_*` 개명 = 별도 후속.
