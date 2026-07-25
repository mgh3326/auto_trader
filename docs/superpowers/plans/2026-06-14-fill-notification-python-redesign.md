# 체결(Fill) 알림 Python 재설계 Implementation Plan (ROB-558)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 체결(fill) 알림을 죽은 n8n 경로에서 Python `TradeNotifier`로 이전하고, 메시지(한글명·슬리피지·매도 실현손익·매수 포지션)와 죽은 `/portfolio`(410) 링크(→ `/invest/stocks`)를 고친다.

**Architecture:** 체결을 감지하는 `websocket_monitor.py`(launchd 상주)가 이미 `TradeNotifier`+Discord webhook을 보유하므로, 새 `notify_fill`을 통해 매수/매도 "주문 접수"와 동일한 시장 채널·포맷으로 직접 렌더링한다. 평단/실현손익은 브로커 직접 조회(KIS/Upbit, env 자격)로 best-effort·fail-open 보강한다. 체결→n8n(`OpenClawClient.send_fill_notification`) 경로는 제거하고, 전체 n8n 디커미션은 Phase 2(별도 이슈)로 남긴다.

**Tech Stack:** Python 3.13, FastAPI/asyncio, httpx, pytest(+`@pytest.mark.unit`), Discord webhook embeds, Telegram Bot API. 마이그레이션 없음.

**스펙:** `docs/superpowers/specs/2026-06-14-fill-notification-python-redesign-design.md`

---

## 파일 구조 (생성/수정)

| 파일 | 책임 | 조치 |
|---|---|---|
| `app/monitoring/trade_notifier/types.py` | 임베드 타입 | `DiscordEmbed`에 `url: NotRequired[str]` 추가 (클릭 가능한 제목 링크) |
| `app/core/portfolio_links.py` | 상세 링크 빌더 | `build_position_detail_url` 구현 → `/invest/stocks/{market}/{symbol}` (kis_trading·toss_notification 자동 수정) |
| `app/services/crypto_pending_order_alert_service.py` | 크립토 펜딩 알림 | 인라인 `/portfolio?market=` 죽은 링크 → `/invest/stocks/crypto/{symbol}` |
| `app/services/fill_notification.py` | 정규화+공유 헬퍼 | `FillEnrichment` 추가, `resolve_fill_display_name` 이동(openclaw→여기), `format_fill_money`/`format_fill_quantity` 공개, `is_fill_notifiable`(통화 인식 임계) 추가, `format_fill_message`/`build_position_detail_url` import 제거 |
| `app/monitoring/trade_notifier/formatters_discord.py` | 체결 임베드 | `format_fill_notification(order, *, display_name, detail_url, enrichment)` 추가 |
| `app/monitoring/trade_notifier/formatters_telegram.py` | 체결 텍스트 | `format_fill_notification_telegram(...)` 추가 |
| `app/monitoring/trade_notifier/notifier.py` | 디스패치 | `notify_fill(order, *, enrichment, detail_url)` 추가 |
| `app/services/fill_enrichment.py` (신규) | 평단/실현손익 조회 | `fetch_fill_enrichment(order) -> FillEnrichment | None` (KIS/Upbit, fail-open) |
| `websocket_monitor.py` | 배선 | `_send_fill_notification` → 임계+보강+`notify_fill`, openclaw 사용 제거 |
| `app/services/openclaw_client.py` | n8n 클라 | `send_fill_notification`·`_build_n8n_fill_payload`·`_resolve_fill_display_name`·fill import 제거 |
| `app/core/config.py`, `env.example` | 설정 | 무참조면 `N8N_FILL_WEBHOOK_URL` 제거 |

> **레이어 주의:** `formatters_discord/telegram`이 `app.services.fill_notification`의 `FillOrder`/`FillEnrichment`/포맷 헬퍼를 import한다(monitoring→services). 순환 없음(`fill_notification`은 notifier를 import하지 않음).

> **디스패치 의미:** `notify_fill`은 기존 `_dispatch`(Discord-우선, Telegram-fallback)를 사용한다. 즉 Discord 성공 시 Telegram은 보내지 않는다(다른 `notify_*`와 동일). 운영상 현재는 Telegram만 나가던 것이 → Discord로 전환된다.

---

## Task 1: `DiscordEmbed`에 `url` 필드 추가

**Files:**
- Modify: `app/monitoring/trade_notifier/types.py:15-19`
- Test: `tests/test_trade_notifier_types.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_trade_notifier_types.py`에 추가:

```python
@pytest.mark.unit
def test_discord_embed_accepts_optional_url():
    from app.monitoring.trade_notifier.types import DiscordEmbed

    embed: DiscordEmbed = {
        "title": "t",
        "description": "d",
        "color": 0x00FF00,
        "fields": [],
        "url": "https://example.com/invest/stocks/kr/005930",
    }
    assert embed["url"].endswith("/invest/stocks/kr/005930")
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_trade_notifier_types.py::test_discord_embed_accepts_optional_url -v`
Expected: FAIL (`url` is not a valid key / ty type error) — 또는 ty 검사에서 실패.

- [ ] **Step 3: 구현**

`types.py` 상단 import와 `DiscordEmbed` 수정:

```python
from typing import NotRequired, TypedDict


class DiscordEmbed(TypedDict):
    title: str
    description: str
    color: int
    fields: list[DiscordField]
    url: NotRequired[str]
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_trade_notifier_types.py -v && uv run ty check app/monitoring/trade_notifier/types.py`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/monitoring/trade_notifier/types.py tests/test_trade_notifier_types.py
git commit -m "feat(ROB-558): add optional url to DiscordEmbed for clickable title"
```

---

## Task 2: 상세 링크를 `/invest/stocks`로 교체 (죽은 410 링크 수정)

**Files:**
- Modify: `app/core/portfolio_links.py:27-41`
- Test: `tests/test_portfolio_links.py` (없으면 생성)

`build_position_detail_url`는 fill/kis_trading(토스 가격추천)/toss_notification에서 공유되므로 구현 교체 한 번으로 세 곳이 모두 살아있는 링크가 된다.

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_portfolio_links.py` 생성:

```python
import pytest

from app.core.portfolio_links import build_position_detail_url


@pytest.mark.unit
def test_kr_url_points_to_invest_stocks():
    url = build_position_detail_url("005930", "kr")
    assert url is not None
    assert url.endswith("/invest/stocks/kr/005930")
    assert "/portfolio/positions/" not in url


@pytest.mark.unit
def test_crypto_symbol_encoded():
    url = build_position_detail_url("KRW-BTC", "crypto")
    assert url is not None
    assert url.endswith("/invest/stocks/crypto/KRW-BTC")


@pytest.mark.unit
def test_unknown_market_returns_none():
    assert build_position_detail_url("005930", "bogus") is None
    assert build_position_detail_url("", "kr") is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_portfolio_links.py -v`
Expected: FAIL (`/portfolio/positions/` 잔존)

- [ ] **Step 3: 구현**

`app/core/portfolio_links.py` 수정:

```python
def build_position_detail_url(
    symbol: str | None, market_type: str | None
) -> str | None:
    """Build a URL to the symbol detail page in the /invest web UI.

    Example: https://mgh3326.duckdns.org/invest/stocks/kr/005930
    (구 /portfolio/positions/... 는 410 Gone — ROB-558에서 교체)
    """
    normalized_symbol = str(symbol or "").strip()
    normalized_market = normalize_position_market_type(market_type)

    if not normalized_symbol or normalized_market is None:
        return None

    encoded_symbol = quote(normalized_symbol, safe="-._~")
    return f"{settings.public_base_url.rstrip('/')}/invest/stocks/{normalized_market}/{encoded_symbol}"
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_portfolio_links.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/core/portfolio_links.py tests/test_portfolio_links.py
git commit -m "fix(ROB-558): point detail link to /invest/stocks (dead 410 /portfolio link)"
```

---

## Task 3: 크립토 펜딩 알림 인라인 죽은 링크 수정

**Files:**
- Modify: `app/services/crypto_pending_order_alert_service.py:168-172`
- Test: `tests/test_crypto_pending_order_alert_service.py` (해당 함수 테스트 추가; 파일 없으면 생성)

- [ ] **Step 1: 실패 테스트 작성**

```python
import pytest

from app.services.crypto_pending_order_alert_service import (
    CryptoPendingOrderAlertConfig,
    _position_url,
)


@pytest.mark.unit
def test_position_url_uses_invest_stocks():
    cfg = CryptoPendingOrderAlertConfig(trader_base_url="https://x.test")
    url = _position_url(cfg, "KRW-BTC")
    assert url == "https://x.test/invest/stocks/crypto/KRW-BTC"
    assert "/portfolio" not in url
```

> 참고: `CryptoPendingOrderAlertConfig` 생성자 필수 인자는 구현부에서 확인하여 테스트의 config 생성을 맞춘다(최소 `trader_base_url`). 다른 필수 필드가 있으면 더미값으로 채운다.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_crypto_pending_order_alert_service.py::test_position_url_uses_invest_stocks -v`
Expected: FAIL (`/portfolio?market=crypto&symbol=` 반환)

- [ ] **Step 3: 구현**

`_position_url` 수정:

```python
def _position_url(config: CryptoPendingOrderAlertConfig, symbol: str) -> str | None:
    base = config.trader_base_url.strip().rstrip("/")
    if not base:
        return None
    return f"{base}/invest/stocks/crypto/{symbol}"
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_crypto_pending_order_alert_service.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/services/crypto_pending_order_alert_service.py tests/test_crypto_pending_order_alert_service.py
git commit -m "fix(ROB-558): crypto pending alert link -> /invest/stocks/crypto"
```

---

## Task 4: `fill_notification.py` — 보강 모델·공유 헬퍼·통화 임계·표시명 이전

**Files:**
- Modify: `app/services/fill_notification.py`
- Test: `tests/test_fill_notification.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_fill_notification.py`에 추가:

```python
from app.services.fill_notification import (
    FillEnrichment,
    is_fill_notifiable,
    resolve_fill_display_name,
    format_fill_money,
    format_fill_quantity,
)


@pytest.mark.unit
class TestFillHelpers:
    def test_currency_aware_threshold(self):
        krw = FillOrder(symbol="005930", side="bid", filled_price=1000,
                        filled_qty=49, filled_amount=49_000, filled_at="t",
                        account="kis", market_type="kr", currency="KRW")
        assert is_fill_notifiable(krw) is False
        krw2 = FillOrder(symbol="005930", side="bid", filled_price=1000,
                         filled_qty=50, filled_amount=50_000, filled_at="t",
                         account="kis", market_type="kr", currency="KRW")
        assert is_fill_notifiable(krw2) is True
        usd = FillOrder(symbol="AAPL", side="bid", filled_price=10,
                        filled_qty=6, filled_amount=60, filled_at="t",
                        account="kis", market_type="us", currency="USD")
        assert is_fill_notifiable(usd) is True  # $60 >= $50 (구버전은 < 50000이라 스킵됐음)

    def test_resolve_display_name_crypto(self):
        order = FillOrder(symbol="KRW-BTC", side="bid", filled_price=1, filled_qty=1,
                          filled_amount=1, filled_at="t", account="upbit", market_type="crypto")
        assert resolve_fill_display_name(order) == "BTC"

    def test_money_and_qty_fmt(self):
        assert format_fill_money(68500, is_usd=False) == "68,500원"
        assert format_fill_money(12.5, is_usd=True) == "$12.50"
        assert format_fill_quantity(10.0) == "10"

    def test_enrichment_defaults(self):
        enr = FillEnrichment()
        assert enr.position_qty is None
        assert enr.is_approximate is True
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_fill_notification.py::TestFillHelpers -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 구현**

`app/services/fill_notification.py` 변경:

(a) 상단에서 죽은 링크 import 제거: `from app.core.portfolio_links import build_position_detail_url` 줄 삭제. `from app.core.kr_symbols import KR_SYMBOLS` 추가.

(b) `FillEnrichment` 데이터클래스 추가(`FillOrder` 아래):

```python
@dataclass
class FillEnrichment:
    """Best-effort 보강 데이터. 항상 근사치(~추정). 조회 실패 시 None 필드."""
    position_qty: float | None = None
    position_avg_price: float | None = None
    realized_pnl_amount: float | None = None
    realized_pnl_rate: float | None = None
    is_approximate: bool = True
```

(c) 표시명 해석 이전(openclaw_client에서). 모듈 끝에 추가:

```python
_KR_SYMBOLS_REVERSE: dict[str, str] | None = None


def _get_kr_symbol_reverse() -> dict[str, str]:
    global _KR_SYMBOLS_REVERSE
    if _KR_SYMBOLS_REVERSE is None:
        _KR_SYMBOLS_REVERSE = {v: k for k, v in KR_SYMBOLS.items()}
    return _KR_SYMBOLS_REVERSE


def resolve_fill_display_name(order: FillOrder) -> str:
    """KR: KR_SYMBOLS 역매핑(미존재 시 코드). US: 심볼. Crypto: KRW-BTC->BTC."""
    if order.market_type == "kr":
        return _get_kr_symbol_reverse().get(order.symbol, order.symbol)
    if order.market_type == "crypto" and "-" in order.symbol:
        return order.symbol.split("-")[-1]
    return order.symbol
```

(d) 통화 인식 임계 추가:

```python
_MIN_NOTIFY_AMOUNT: dict[str, float] = {"KRW": 50_000.0, "USD": 50.0}


def is_fill_notifiable(order: FillOrder) -> bool:
    """통화별 최소 체결금액 이상이면 True (구버전 통화-무시 50,000 버그 수정)."""
    currency = (order.currency or "KRW").upper()
    threshold = _MIN_NOTIFY_AMOUNT.get(currency, 50_000.0)
    return order.filled_amount >= threshold
```

(e) 기존 `_format_krw`/`_format_usd`/`_format_money`/`_format_quantity`를 공개 별칭으로 노출(기존 함수는 유지, 공개 래퍼 추가):

```python
def format_fill_money(value: float, *, is_usd: bool) -> str:
    return _format_usd(value) if is_usd else _format_krw(value)


def format_fill_quantity(value: float) -> str:
    return _format_quantity(value)
```

(f) `format_fill_message` 함수 제거(신규 formatters로 대체). 이 함수만 `build_position_detail_url`을 쓰던 마지막 사용처다. 이를 import하는 테스트(`tests/test_fill_notification.py`의 `format_fill_message` 관련)와 `openclaw_client`의 import는 후속 Task에서 정리하되, **이 Task에서 `format_fill_message`를 삭제하면 openclaw_client import가 깨지므로 Task 10과 함께 삭제**한다. → 본 Task에서는 `format_fill_message`는 **그대로 두고** 위 (a)~(e)만 추가한다. (`format_fill_message` 내부의 `build_position_detail_url` 호출은 Task 10 제거 시 함께 사라진다. 단 (a)에서 import를 지우면 `format_fill_message`가 깨지므로, **(a)의 import 제거도 Task 10으로 미룬다.**)

> 정정: 본 Task는 **추가만** 한다((b)~(e)). import 제거·`format_fill_message` 삭제는 Task 10.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_fill_notification.py -v`
Expected: PASS (기존 테스트 포함 전부)

- [ ] **Step 5: 커밋**

```bash
git add app/services/fill_notification.py tests/test_fill_notification.py
git commit -m "feat(ROB-558): FillEnrichment, display-name, currency-aware threshold, public fmt helpers"
```

---

## Task 5: Discord 체결 임베드 포맷터

**Files:**
- Modify: `app/monitoring/trade_notifier/formatters_discord.py`
- Test: `tests/test_trade_notifier_formatters_discord.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_trade_notifier_formatters_discord.py`에 추가:

```python
from app.monitoring.trade_notifier.formatters_discord import format_fill_notification
from app.services.fill_notification import FillOrder, FillEnrichment


def _kr_buy(**kw):
    base = dict(symbol="005930", side="bid", filled_price=68500.0, filled_qty=10.0,
                filled_amount=685000.0, filled_at="2026-06-14T09:31:02", account="kis",
                order_price=68300.0, order_id="0001234567", market_type="kr", currency="KRW")
    base.update(kw)
    return FillOrder(**base)


@pytest.mark.unit
class TestFormatFillNotification:
    def test_buy_basic_with_link_and_slippage(self):
        embed = format_fill_notification(
            _kr_buy(), display_name="삼성전자",
            detail_url="https://x.test/invest/stocks/kr/005930", enrichment=None,
        )
        assert embed["title"] == "🟢 체결 · 삼성전자 (005930)"
        assert embed["color"] == 0x00FF00
        assert embed["url"] == "https://x.test/invest/stocks/kr/005930"
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["구분"] == "매수 체결"
        assert "68,500원" in fields["체결가"]
        assert "+0.29%" in fields["체결가"] or "+0.30%" in fields["체결가"]  # vs 68,300
        assert fields["수량"] == "10"
        assert fields["금액"] == "685,000원"

    def test_sell_shows_realized_pnl(self):
        order = _kr_buy(side="ask")
        enr = FillEnrichment(realized_pnl_amount=12000.0, realized_pnl_rate=1.8, is_approximate=True)
        embed = format_fill_notification(order, display_name="삼성전자", detail_url=None, enrichment=enr)
        assert embed["color"] == 0xFF0000
        assert "url" not in embed
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["구분"] == "매도 체결"
        assert "실현손익" in fields
        assert "+12,000원" in fields["실현손익"]
        assert "~추정" in fields["실현손익"]

    def test_buy_shows_position_when_enriched(self):
        enr = FillEnrichment(position_qty=30.0, position_avg_price=68100.0, is_approximate=True)
        embed = format_fill_notification(_kr_buy(), display_name="삼성전자", detail_url=None, enrichment=enr)
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert "보유" in fields
        assert "30" in fields["보유"] and "68,100원" in fields["보유"]

    def test_partial_label(self):
        embed = format_fill_notification(_kr_buy(fill_status="partial"), display_name="삼성전자",
                                         detail_url=None, enrichment=None)
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["구분"] == "매수 부분체결"

    def test_no_slippage_when_no_order_price(self):
        embed = format_fill_notification(_kr_buy(order_price=None), display_name="삼성전자",
                                         detail_url=None, enrichment=None)
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert "vs 주문가" not in fields["체결가"]
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_trade_notifier_formatters_discord.py::TestFormatFillNotification -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 구현**

`formatters_discord.py` 상단 import 추가:

```python
from app.services.fill_notification import (
    FillEnrichment,
    FillOrder,
    format_fill_money,
    format_fill_quantity,
)
```

함수 추가:

```python
def format_fill_notification(
    order: FillOrder,
    *,
    display_name: str,
    detail_url: str | None = None,
    enrichment: FillEnrichment | None = None,
) -> DiscordEmbed:
    is_sell = order.side == "ask"
    is_partial = order.fill_status == "partial"
    side_emoji = "🔴" if is_sell else ("🟢" if order.side == "bid" else "⚪")
    side_text = "매도" if is_sell else ("매수" if order.side == "bid" else "미확인")
    fill_label = "부분체결" if is_partial else "체결"
    is_usd = order.currency == "USD"

    price_str = format_fill_money(order.filled_price, is_usd=is_usd)
    if order.order_price:
        diff_pct = (order.filled_price - order.order_price) / order.order_price * 100
        price_str += f" ({diff_pct:+.2f}% vs 주문가)"

    fields: list[DiscordField] = [
        {"name": "구분", "value": f"{side_text} {fill_label}", "inline": True},
        {"name": "체결가", "value": price_str, "inline": True},
        {"name": "수량", "value": format_fill_quantity(order.filled_qty), "inline": True},
        {"name": "금액", "value": format_fill_money(order.filled_amount, is_usd=is_usd), "inline": True},
    ]

    if enrichment is not None:
        approx = " ~추정" if enrichment.is_approximate else ""
        if is_sell and enrichment.realized_pnl_amount is not None:
            sign = "+" if enrichment.realized_pnl_amount >= 0 else ""
            rate = (
                f" ({enrichment.realized_pnl_rate:+.2f}%)"
                if enrichment.realized_pnl_rate is not None
                else ""
            )
            fields.append({
                "name": "실현손익",
                "value": f"{sign}{format_fill_money(enrichment.realized_pnl_amount, is_usd=is_usd)}{rate}{approx}",
                "inline": True,
            })
        elif (not is_sell and enrichment.position_qty is not None
              and enrichment.position_avg_price is not None):
            fields.append({
                "name": "보유",
                "value": f"{format_fill_quantity(enrichment.position_qty)} · 평단 "
                         f"{format_fill_money(enrichment.position_avg_price, is_usd=is_usd)}{approx}",
                "inline": True,
            })

    account_val = order.account
    if order.order_id:
        account_val += f" · 주문 {order.order_id[:8]}…"
    fields.append({"name": "계좌", "value": account_val, "inline": False})

    embed: DiscordEmbed = {
        "title": f"{side_emoji} {fill_label} · {display_name} ({order.symbol})",
        "description": f"🕒 {format_datetime()}",
        "color": COLORS["sell"] if is_sell else COLORS["buy"],
        "fields": fields,
    }
    if detail_url:
        embed["url"] = detail_url
    return embed
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_trade_notifier_formatters_discord.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/monitoring/trade_notifier/formatters_discord.py tests/test_trade_notifier_formatters_discord.py
git commit -m "feat(ROB-558): Discord fill embed formatter (slippage, pnl, position, link)"
```

---

## Task 6: Telegram 체결 포맷터

**Files:**
- Modify: `app/monitoring/trade_notifier/formatters_telegram.py`
- Test: `tests/test_trade_notifier_formatters_telegram.py`

- [ ] **Step 1: 실패 테스트 작성**

```python
from app.monitoring.trade_notifier.formatters_telegram import format_fill_notification_telegram
from app.services.fill_notification import FillOrder, FillEnrichment


def _kr_sell():
    return FillOrder(symbol="005930", side="ask", filled_price=68500.0, filled_qty=10.0,
                     filled_amount=685000.0, filled_at="2026-06-14T09:31:02", account="kis",
                     order_price=68300.0, order_id="0001234567", market_type="kr", currency="KRW")


@pytest.mark.unit
class TestFormatFillTelegram:
    def test_sell_with_pnl_and_link(self):
        enr = FillEnrichment(realized_pnl_amount=12000.0, realized_pnl_rate=1.8)
        msg = format_fill_notification_telegram(
            _kr_sell(), display_name="삼성전자",
            detail_url="https://x.test/invest/stocks/kr/005930", enrichment=enr,
        )
        assert "매도 체결" in msg
        assert "삼성전자" in msg and "005930" in msg
        assert "+12,000원" in msg and "~추정" in msg
        assert "[종목 상세 보기](https://x.test/invest/stocks/kr/005930)" in msg

    def test_no_link_when_none(self):
        msg = format_fill_notification_telegram(_kr_sell(), display_name="삼성전자",
                                                detail_url=None, enrichment=None)
        assert "종목 상세 보기" not in msg
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_trade_notifier_formatters_telegram.py::TestFormatFillTelegram -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 구현**

`formatters_telegram.py` 상단 import 추가:

```python
from app.services.fill_notification import (
    FillEnrichment,
    FillOrder,
    format_fill_money,
    format_fill_quantity,
)
```

함수 추가:

```python
def format_fill_notification_telegram(
    order: FillOrder,
    *,
    display_name: str,
    detail_url: str | None = None,
    enrichment: FillEnrichment | None = None,
) -> str:
    """Telegram(legacy Markdown) 체결 메시지."""
    is_sell = order.side == "ask"
    is_partial = order.fill_status == "partial"
    side_emoji = "🔴" if is_sell else ("🟢" if order.side == "bid" else "⚪")
    side_text = "매도" if is_sell else ("매수" if order.side == "bid" else "미확인")
    fill_label = "부분체결" if is_partial else "체결"
    is_usd = order.currency == "USD"

    price_str = format_fill_money(order.filled_price, is_usd=is_usd)
    if order.order_price:
        diff_pct = (order.filled_price - order.order_price) / order.order_price * 100
        price_str += f" ({diff_pct:+.2f}%)"

    lines = [
        f"*{side_emoji} {fill_label} · {display_name} \\({order.symbol}\\)*",
        "",
        f"*구분:* {side_text} {fill_label}",
        f"*체결가:* {price_str}",
        f"*수량:* {format_fill_quantity(order.filled_qty)}",
        f"*금액:* {format_fill_money(order.filled_amount, is_usd=is_usd)}",
    ]

    if enrichment is not None:
        approx = " ~추정" if enrichment.is_approximate else ""
        if is_sell and enrichment.realized_pnl_amount is not None:
            sign = "+" if enrichment.realized_pnl_amount >= 0 else ""
            rate = (
                f" ({enrichment.realized_pnl_rate:+.2f}%)"
                if enrichment.realized_pnl_rate is not None else ""
            )
            lines.append(
                f"*실현손익:* {sign}{format_fill_money(enrichment.realized_pnl_amount, is_usd=is_usd)}{rate}{approx}"
            )
        elif (not is_sell and enrichment.position_qty is not None
              and enrichment.position_avg_price is not None):
            lines.append(
                f"*보유:* {format_fill_quantity(enrichment.position_qty)} · 평단 "
                f"{format_fill_money(enrichment.position_avg_price, is_usd=is_usd)}{approx}"
            )

    account_val = order.account
    if order.order_id:
        account_val += f" · 주문 {order.order_id[:8]}…"
    lines.append(f"*계좌:* {account_val}")
    lines.append(f"🕒 {format_datetime()}")
    if detail_url:
        lines.append(f"[종목 상세 보기]({detail_url})")

    return "\n".join(lines)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_trade_notifier_formatters_telegram.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/monitoring/trade_notifier/formatters_telegram.py tests/test_trade_notifier_formatters_telegram.py
git commit -m "feat(ROB-558): Telegram fill formatter (pnl, position, markdown link)"
```

---

## Task 7: `TradeNotifier.notify_fill`

**Files:**
- Modify: `app/monitoring/trade_notifier/notifier.py`
- Test: `tests/test_trade_notifier.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_trade_notifier.py`에 추가:

```python
from app.services.fill_notification import FillOrder, FillEnrichment


def _fill():
    return FillOrder(symbol="005930", side="bid", filled_price=68500.0, filled_qty=10.0,
                     filled_amount=685000.0, filled_at="t", account="kis",
                     order_price=68300.0, order_id="0001234567", market_type="kr", currency="KRW")


@pytest.mark.unit
async def test_notify_fill_routes_to_kr_webhook(trade_notifier):
    trade_notifier.configure(
        bot_token="t", chat_ids=["1"], enabled=True,
        discord_webhook_kr="https://discord.com/api/webhooks/kr",
    )
    with patch.object(trade_notifier, "_send_to_discord_embed_single",
                      new=AsyncMock(return_value=True)) as mock_discord, \
         patch.object(trade_notifier, "_send_to_telegram",
                      new=AsyncMock(return_value=True)) as mock_tg:
        ok = await trade_notifier.notify_fill(
            _fill(), enrichment=FillEnrichment(position_qty=30.0, position_avg_price=68100.0),
            detail_url="https://x.test/invest/stocks/kr/005930",
        )
    assert ok is True
    # Discord 성공 → Telegram 미발송(_dispatch fallback)
    mock_discord.assert_awaited_once()
    mock_tg.assert_not_awaited()
    embed_arg = mock_discord.await_args.args[0]
    assert embed_arg["title"] == "🟢 체결 · 삼성전자 (005930)"
    assert embed_arg["url"] == "https://x.test/invest/stocks/kr/005930"


@pytest.mark.unit
async def test_notify_fill_telegram_fallback_when_discord_fails(trade_notifier):
    trade_notifier.configure(bot_token="t", chat_ids=["1"], enabled=True,
                             discord_webhook_kr="https://discord.com/api/webhooks/kr")
    with patch.object(trade_notifier, "_send_to_discord_embed_single",
                      new=AsyncMock(return_value=False)), \
         patch.object(trade_notifier, "_send_to_telegram",
                      new=AsyncMock(return_value=True)) as mock_tg:
        ok = await trade_notifier.notify_fill(_fill(), enrichment=None, detail_url=None)
    assert ok is True
    mock_tg.assert_awaited_once()
```

> `pytest.ini`/`pyproject`에 asyncio mode가 auto가 아니면 각 async 테스트에 `@pytest.mark.asyncio`를 추가한다(기존 async 테스트 컨벤션 확인).

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_trade_notifier.py -k notify_fill -v`
Expected: FAIL (no attribute `notify_fill`)

- [ ] **Step 3: 구현**

`notifier.py` import 추가:

```python
from app.services.fill_notification import FillEnrichment, FillOrder
```

`notify_buy_order` 옆에 추가:

```python
    async def notify_fill(
        self,
        order: FillOrder,
        *,
        enrichment: FillEnrichment | None = None,
        detail_url: str | None = None,
    ) -> bool:
        """체결(fill) 알림. Discord 우선, Telegram fallback."""
        from .formatters_telegram import format_fill_notification_telegram
        from app.services.fill_notification import resolve_fill_display_name

        display_name = resolve_fill_display_name(order)
        embed = fmt_discord.format_fill_notification(
            order, display_name=display_name, detail_url=detail_url, enrichment=enrichment,
        )
        telegram_msg = format_fill_notification_telegram(
            order, display_name=display_name, detail_url=detail_url, enrichment=enrichment,
        )
        return await self._dispatch(embed, telegram_msg, order.market_type)
```

> 모듈 상단 `from . import formatters_telegram as fmt_telegram`가 이미 있으므로 `fmt_telegram.format_fill_notification_telegram`을 써도 된다(로컬 import 대신). 일관성 위해 `fmt_telegram.` 사용 권장. `resolve_fill_display_name`은 상단 import로 올린다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_trade_notifier.py -k notify_fill -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/monitoring/trade_notifier/notifier.py tests/test_trade_notifier.py
git commit -m "feat(ROB-558): TradeNotifier.notify_fill (discord-first, telegram-fallback)"
```

---

## Task 8: `fill_enrichment.py` — 브로커 직접 보강 (fail-open)

**Files:**
- Create: `app/services/fill_enrichment.py`
- Test: `tests/test_fill_enrichment.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_fill_enrichment.py` 생성:

```python
from unittest.mock import AsyncMock, patch

import pytest

from app.services.fill_enrichment import fetch_fill_enrichment
from app.services.fill_notification import FillOrder


def _kr(side="ask"):
    return FillOrder(symbol="005930", side=side, filled_price=68500.0, filled_qty=10.0,
                     filled_amount=685000.0, filled_at="t", account="kis",
                     market_type="kr", currency="KRW")


@pytest.mark.unit
async def test_kr_sell_realized_pnl(monkeypatch):
    async def fake_holding(client, ticker, market):
        return {"quantity": 50, "avg_price": 68000.0, "current_price": 68500.0}

    monkeypatch.setattr("app.services.fill_enrichment.get_kis_holding_for_ticker", fake_holding)
    monkeypatch.setattr("app.services.fill_enrichment.KISClient", lambda: object())
    enr = await fetch_fill_enrichment(_kr(side="ask"))
    assert enr is not None
    # (68500-68000)*10 = 5000
    assert enr.realized_pnl_amount == pytest.approx(5000.0)
    assert enr.realized_pnl_rate == pytest.approx((68500/68000 - 1) * 100)


@pytest.mark.unit
async def test_kr_buy_position(monkeypatch):
    async def fake_holding(client, ticker, market):
        return {"quantity": 30, "avg_price": 68100.0, "current_price": 68500.0}

    monkeypatch.setattr("app.services.fill_enrichment.get_kis_holding_for_ticker", fake_holding)
    monkeypatch.setattr("app.services.fill_enrichment.KISClient", lambda: object())
    enr = await fetch_fill_enrichment(_kr(side="bid"))
    assert enr is not None
    assert enr.position_qty == pytest.approx(30.0)
    assert enr.position_avg_price == pytest.approx(68100.0)
    assert enr.realized_pnl_amount is None


@pytest.mark.unit
async def test_fail_open_returns_none(monkeypatch):
    async def boom(client, ticker, market):
        raise RuntimeError("broker down")

    monkeypatch.setattr("app.services.fill_enrichment.get_kis_holding_for_ticker", boom)
    monkeypatch.setattr("app.services.fill_enrichment.KISClient", lambda: object())
    assert await fetch_fill_enrichment(_kr()) is None


@pytest.mark.unit
async def test_no_position_returns_none(monkeypatch):
    async def empty(client, ticker, market):
        return {"quantity": 0, "avg_price": 0.0, "current_price": 0.0}

    monkeypatch.setattr("app.services.fill_enrichment.get_kis_holding_for_ticker", empty)
    monkeypatch.setattr("app.services.fill_enrichment.KISClient", lambda: object())
    assert await fetch_fill_enrichment(_kr()) is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_fill_enrichment.py -v`
Expected: FAIL (module missing)

- [ ] **Step 3: 구현**

`app/services/fill_enrichment.py` 생성:

```python
"""Best-effort, fail-open enrichment for fill notifications.

브로커 직접 조회(env 자격)로 체결 시점 평단/포지션/실현손익 근사치를 얻는다.
어떤 예외도 알림을 막지 않는다(항상 None 반환으로 graceful).
"""

from __future__ import annotations

import logging

from app.models.manual_holdings import MarketType
from app.services.brokers.kis.client import KISClient
from app.services.kis_holdings_service import get_kis_holding_for_ticker
from app.services.fill_notification import FillEnrichment, FillOrder

logger = logging.getLogger(__name__)


async def fetch_fill_enrichment(order: FillOrder) -> FillEnrichment | None:
    try:
        if order.market_type in ("kr", "us"):
            return await _fetch_kis(order)
        if order.market_type == "crypto":
            return await _fetch_upbit(order)
    except Exception:
        logger.warning(
            "fill enrichment failed (fail-open): symbol=%s market=%s",
            order.symbol, order.market_type, exc_info=True,
        )
    return None


def _build(order: FillOrder, *, qty: float, avg: float) -> FillEnrichment | None:
    if qty <= 0 or avg <= 0:
        return None
    enr = FillEnrichment(position_qty=qty, position_avg_price=avg, is_approximate=True)
    if order.side == "ask":  # 매도 → 실현손익 근사치
        enr.realized_pnl_amount = (order.filled_price - avg) * order.filled_qty
        enr.realized_pnl_rate = (order.filled_price / avg - 1) * 100
    return enr


async def _fetch_kis(order: FillOrder) -> FillEnrichment | None:
    market = MarketType.KR if order.market_type == "kr" else MarketType.US
    holding = await get_kis_holding_for_ticker(KISClient(), order.symbol, market)
    return _build(order, qty=float(holding.get("quantity") or 0),
                  avg=float(holding.get("avg_price") or 0))


async def _fetch_upbit(order: FillOrder) -> FillEnrichment | None:
    from app.services.brokers.upbit.client import (
        fetch_my_coins,
        parse_upbit_account_row,
    )

    currency = order.symbol.split("-")[-1] if "-" in order.symbol else order.symbol
    accounts = await fetch_my_coins()
    for row in accounts:
        if str(row.get("currency", "")).upper() == currency.upper():
            parsed = parse_upbit_account_row(row)
            return _build(order, qty=float(parsed["total_quantity"]),
                          avg=float(parsed["avg_buy_price"]))
    return None
```

> 구현 중 확인: `app/services/brokers/upbit/client.py`에 `fetch_my_coins`(또는 `/v1/accounts`를 반환하는 동등 함수)와 `parse_upbit_account_row`가 존재한다(스펙에서 확인됨). 이름이 다르면 실제 공개 함수명으로 맞춘다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_fill_enrichment.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/services/fill_enrichment.py tests/test_fill_enrichment.py
git commit -m "feat(ROB-558): broker-direct fill enrichment (KIS/Upbit, fail-open)"
```

---

## Task 9: `websocket_monitor` 배선 — notify_fill + 임계 + 보강, openclaw 제거

**Files:**
- Modify: `websocket_monitor.py` (imports, `__init__`, `_send_fill_notification`)
- Test: `tests/test_websocket_monitor.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_websocket_monitor.py`에 추가(기존 테스트 패턴 확인 후):

```python
from unittest.mock import AsyncMock, patch

import pytest

from app.services.fill_notification import FillOrder


def _fill(amount=685000.0, currency="KRW", market="kr"):
    return FillOrder(symbol="005930", side="bid", filled_price=68500.0, filled_qty=10.0,
                     filled_amount=amount, filled_at="t", account="kis",
                     market_type=market, currency=currency)


@pytest.mark.unit
async def test_send_fill_notification_calls_notify_fill(monkeypatch):
    from websocket_monitor import UnifiedWebSocketMonitor  # 실제 클래스명 확인

    monitor = UnifiedWebSocketMonitor(mode="kis")
    fake_notifier = AsyncMock()
    fake_notifier.notify_fill = AsyncMock(return_value=True)
    monkeypatch.setattr("websocket_monitor.get_trade_notifier", lambda: fake_notifier)
    monkeypatch.setattr("websocket_monitor.fetch_fill_enrichment",
                        AsyncMock(return_value=None))

    await monitor._send_fill_notification(_fill())
    fake_notifier.notify_fill.assert_awaited_once()


@pytest.mark.unit
async def test_send_fill_notification_skips_below_threshold(monkeypatch):
    from websocket_monitor import UnifiedWebSocketMonitor

    monitor = UnifiedWebSocketMonitor(mode="kis")
    fake_notifier = AsyncMock()
    fake_notifier.notify_fill = AsyncMock(return_value=True)
    monkeypatch.setattr("websocket_monitor.get_trade_notifier", lambda: fake_notifier)
    monkeypatch.setattr("websocket_monitor.fetch_fill_enrichment",
                        AsyncMock(return_value=None))

    await monitor._send_fill_notification(_fill(amount=10_000.0))  # < 50,000 KRW
    fake_notifier.notify_fill.assert_not_awaited()


@pytest.mark.unit
async def test_enrichment_failure_does_not_block(monkeypatch):
    from websocket_monitor import UnifiedWebSocketMonitor

    monitor = UnifiedWebSocketMonitor(mode="kis")
    fake_notifier = AsyncMock()
    fake_notifier.notify_fill = AsyncMock(return_value=True)
    monkeypatch.setattr("websocket_monitor.get_trade_notifier", lambda: fake_notifier)
    monkeypatch.setattr("websocket_monitor.fetch_fill_enrichment",
                        AsyncMock(side_effect=RuntimeError("boom")))

    await monitor._send_fill_notification(_fill())  # 예외가 새지 않아야 함
    fake_notifier.notify_fill.assert_awaited_once()
    # enrichment=None으로 호출됨
    assert monitor._send_fill_notification  # smoke
```

> 실제 클래스명/생성자 시그니처는 `websocket_monitor.py`에서 확인하여 맞춘다(`UnifiedWebSocketMonitor(mode=...)` 가정). asyncio 마커도 컨벤션에 맞춘다.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_websocket_monitor.py -k fill -v`
Expected: FAIL

- [ ] **Step 3: 구현**

(a) `websocket_monitor.py` import 정리: `from app.services.openclaw_client import OpenClawClient` 제거. 추가:

```python
from app.monitoring.trade_notifier import get_trade_notifier  # (이미 존재)
from app.services.fill_enrichment import fetch_fill_enrichment
from app.services.fill_notification import (
    FillOrder,
    is_fill_notifiable,
    # (normalize_* 등 기존 import 유지)
)
```

(b) `__init__`에서 `self.openclaw_client = OpenClawClient()` 줄 제거(다른 사용처 없음 — grep로 확인).

(c) `_send_fill_notification` 전체 교체:

```python
    async def _send_fill_notification(
        self, order: FillOrder, *, correlation_id: str | None = None
    ) -> None:
        """체결 알림: 통화 임계 → best-effort 보강 → TradeNotifier (fire-and-forget)."""
        if not is_fill_notifiable(order):
            logger.info(
                "Fill notification skipped: below threshold symbol=%s amount=%s currency=%s",
                order.symbol, order.filled_amount, order.currency,
            )
            return

        enrichment = None
        try:
            enrichment = await fetch_fill_enrichment(order)
        except Exception:
            logger.warning("Fill enrichment error (fail-open): symbol=%s",
                           order.symbol, exc_info=True)

        from app.core.portfolio_links import build_position_detail_url
        detail_url = build_position_detail_url(order.symbol, order.market_type)

        logger.info(
            "Fill notification send start: correlation_id=%s symbol=%s account=%s amount=%s",
            correlation_id, order.symbol, order.account, order.filled_amount,
        )
        try:
            ok = await get_trade_notifier().notify_fill(
                order, enrichment=enrichment, detail_url=detail_url,
            )
            if ok:
                self.fills_forwarded += 1
                self.last_openclaw_success_at = datetime.now(UTC).isoformat()
                logger.info("Fill notification sent: correlation_id=%s symbol=%s result=success",
                            correlation_id, order.symbol)
            else:
                logger.warning("Fill notification not delivered: correlation_id=%s symbol=%s",
                               correlation_id, order.symbol)
        except Exception as e:
            logger.error("Fill notification error: correlation_id=%s symbol=%s error=%s",
                         correlation_id, order.symbol, e, exc_info=True)
```

> `fetch_fill_enrichment` 내부가 이미 fail-open(None 반환)이지만, 배선부에서도 한 겹 더 감싸 절대 알림 경로가 죽지 않게 한다. `last_openclaw_success_at`/`fills_forwarded` 카운터 이름은 heartbeat(런타임 모니터)가 읽으므로 유지한다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_websocket_monitor.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add websocket_monitor.py tests/test_websocket_monitor.py
git commit -m "feat(ROB-558): wire fill notifications to TradeNotifier (threshold+enrichment, drop n8n)"
```

---

## Task 10: openclaw 체결 경로 + 죽은 헬퍼 제거

**Files:**
- Modify: `app/services/openclaw_client.py` (remove fill bits)
- Modify: `app/services/fill_notification.py` (remove `format_fill_message` + dead import)
- Test: `tests/test_openclaw_client.py`, `tests/test_fill_notification.py`

- [ ] **Step 1: 제거 대상 확정 (grep)**

Run:
```bash
grep -rn "send_fill_notification\|_build_n8n_fill_payload\|format_fill_message\|_resolve_fill_display_name" app/ websocket_monitor.py tests/
```
Expected: 사용처가 `openclaw_client.py`(정의)·`tests/test_openclaw_client.py`·`tests/test_fill_notification.py`에 한정(`websocket_monitor`는 Task 9에서 제거됨). 다른 프로덕션 사용처가 있으면 멈추고 재평가.

- [ ] **Step 2: 테스트 업데이트(실패→정리)**

`tests/test_openclaw_client.py`에서 `send_fill_notification`/`_build_n8n_fill_payload`/`_resolve_fill_display_name` 관련 테스트(6종)를 삭제한다. `tests/test_fill_notification.py`에서 `format_fill_message` import와 그 테스트(`TestFormatFillMessage` 등)를 삭제한다.

Run: `uv run pytest tests/test_openclaw_client.py tests/test_fill_notification.py -v`
Expected: 삭제 전이면 import 깨짐(다음 스텝에서 코드 제거).

- [ ] **Step 3: 코드 제거**

`app/services/openclaw_client.py`:
- `send_fill_notification` 메서드 삭제.
- 모듈 함수 `_build_n8n_fill_payload`, `_resolve_fill_display_name`, `_get_kr_symbol_reverse`, 모듈 변수 `_KR_SYMBOLS_REVERSE` 삭제(표시명은 fill_notification으로 이전됨, Task 4).
- 사용하지 않게 된 import 제거: `from app.core.kr_symbols import KR_SYMBOLS`, `from app.core.portfolio_links import build_position_detail_url`, `from app.services.fill_notification import (FillOrder, FillOrderLike, coerce_fill_order, format_fill_message)` — 단 `FillOrderLike`/`coerce_fill_order` 등이 다른 곳에서 쓰이면 남긴다(grep 확인).
- `FillNotificationDeliveryResult` 데이터클래스가 fill 전용이면 함께 삭제(다른 참조 없을 때).

`app/services/fill_notification.py`:
- `format_fill_message` 함수 삭제.
- 상단 `from app.core.portfolio_links import build_position_detail_url` import 삭제(이제 무참조).

- [ ] **Step 4: 통과 확인**

Run:
```bash
uv run pytest tests/test_openclaw_client.py tests/test_fill_notification.py -v
grep -rn "send_fill_notification\|format_fill_message\|_build_n8n_fill_payload" app/ websocket_monitor.py
```
Expected: 테스트 PASS, grep 0 매치(정의·호출 모두 제거).

- [ ] **Step 5: 커밋**

```bash
git add app/services/openclaw_client.py app/services/fill_notification.py tests/test_openclaw_client.py tests/test_fill_notification.py
git commit -m "refactor(ROB-558): remove dead n8n fill path (send_fill_notification, format_fill_message)"
```

---

## Task 11: 설정 정리 + 전체 검증

**Files:**
- Modify: `app/core/config.py`, `env.example` (조건부)
- Test: 전체 스위트

- [ ] **Step 1: `N8N_FILL_WEBHOOK_URL` 무참조 확인**

Run: `grep -rn "N8N_FILL_WEBHOOK_URL" app/ websocket_monitor.py tests/`
Expected: 정의(`config.py`)·`env.example`만 남음. 그 외 0이면 제거 가능.

- [ ] **Step 2: 제거(무참조일 때만)**

`app/core/config.py`의 `N8N_FILL_WEBHOOK_URL: str = ""` 줄과 `env.example`의 해당 줄 삭제. (나머지 N8N_/OPENCLAW_ 키는 Phase 2까지 보존.)

- [ ] **Step 3: 죽은 링크 잔존 가드**

Run: `grep -rn "/portfolio/positions/\|portfolio?market=" app/ websocket_monitor.py`
Expected: 0 매치(전부 `/invest/stocks`로 이전됨). 매치가 있으면 수정.

- [ ] **Step 4: 린트/타입/전체 테스트**

Run:
```bash
uv run ruff format app/ tests/ websocket_monitor.py
uv run ruff check app/ tests/ websocket_monitor.py
uv run ty check app/
uv run pytest tests/ -m "not integration and not slow" -q
```
Expected: 포맷 클린, 린트 0, ty 0, 테스트 그린. 실패 시 해당 Task로 돌아가 수정.

- [ ] **Step 5: 커밋**

```bash
git add -A
git commit -m "chore(ROB-558): drop unused N8N_FILL_WEBHOOK_URL + final verification"
```

---

## 자체 점검 (작성자 체크리스트 — 작성 완료)

- **스펙 커버리지:** ① 한글명 통일=Task 4/5/6 ② 매도 실현손익=Task 5/6/8 ③ 매수 포지션=Task 5/6/8 ④ 슬리피지=Task 5/6 / 링크 수정=Task 2/3 / Python 렌더=Task 5/6/7/9 / 통화 임계=Task 4/9 / n8n 체결 제거=Task 9/10/11. ✅ 누락 없음.
- **타입 일관성:** `FillOrder`/`FillEnrichment`(fill_notification 정의) → formatters/notifier/enrichment에서 동일 사용. `notify_fill(order,*,enrichment,detail_url)` 시그니처 Task 7/9 일치. `format_fill_money(value,*,is_usd)`/`format_fill_quantity(value)` 일치.
- **플레이스홀더:** 없음(모든 스텝에 실제 코드/명령). 단 실행자는 (a) 실제 클래스명 `UnifiedWebSocketMonitor`/생성자, (b) upbit 공개 함수명 `fetch_my_coins`, (c) `CryptoPendingOrderAlertConfig` 필수 인자, (d) asyncio 마커 컨벤션을 코드에서 확인 후 맞춘다(스펙에 근거 있음).

## 미해결(구현 중 확인)
- USD 임계 $50의 적정성(운영 후 조정 가능 — config화는 후속).
- crypto 평단/실현손익의 정밀도(Upbit avg_buy_price 기반 근사). 정밀 FIFO는 링크 페이지가 권위.
- `get_kis_holding_for_ticker`가 매 체결마다 전체 보유를 조회 — 빈도 낮아 허용. 부담되면 캐시는 후속.
