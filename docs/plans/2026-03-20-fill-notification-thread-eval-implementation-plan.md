# Fill Notification Thread Evaluation Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 체결 알림 경로를 OpenClaw 고정 스레드/포럼 webhook 방식에서 `n8n -> #trading-alerts -> 새 스레드 -> OpenClaw wake` 방식으로 전환한다.

**Architecture:** `websocket_monitor.py`는 더 이상 마켓별 스레드 ID나 Discord forum webhook을 알지 않고, 정규화된 체결 payload를 `OpenClawClient.send_fill_notification()`을 통해 n8n webhook으로 전달한다. n8n이 Discord 제목 메시지 게시, 스레드 생성, 상세 메시지 게시, OpenClaw wake 호출을 담당하고, Python 쪽은 Telegram fallback과 최소 금액 필터만 유지한다.

**Tech Stack:** Python 3.13, FastAPI settings, httpx, tenacity, pytest, n8n webhook workflow, Discord REST API

**Design Notes:**
- 저장소 규칙상 Discord bot token / OpenClaw bearer token을 문서나 workflow JSON에 하드코딩하지 않는다. n8n 내 기존 credential 또는 환경변수 참조로 관리한다.
- 현재 `TradeNotifier`의 market별 Discord webhook은 체결 알림 외에도 주문/분석/실패 알림 경로에서 널리 사용된다. 따라서 이 플랜은 `필수 범위`와 `선택 정리 범위`를 분리한다.
- 추천 접근은 2단계다.
  - 1단계: fill 경로만 n8n으로 전환하고, 기존 notifier market webhook은 다른 알림 경로 때문에 일단 유지한다.
  - 2단계: 별도 배치에서 `TradeNotifier` market webhook 제거 및 모든 관련 알림의 라우팅 정책을 재설계한다.

---

## Scope Split

**Required in this change**
- `app/services/openclaw_client.py`의 fill 전송 경로를 n8n webhook POST로 교체
- `app/core/config.py`에 `N8N_FILL_WEBHOOK_URL` 추가
- `websocket_monitor.py`의 Upbit-only 최소 금액 필터 제거
- n8n `Fill Notification` workflow 생성 및 export
- `.env`, `.env.test`, 관련 테스트/문서 갱신

**Optional / high-risk cleanup**
- `discord_webhook_us|kr|crypto`와 `OPENCLAW_THREAD_*`를 설정/코드/테스트 전반에서 완전 제거
- `TradeNotifier`의 market별 Discord 라우팅 제거 또는 `discord_webhook_alerts` 단일 채널 기반으로 재설계

---

### Task 1: Failing tests for the new fill delivery contract

**Files:**
- Modify: `tests/test_openclaw_client.py`
- Modify: `tests/test_websocket_monitor.py`

**Step 1: Write failing tests for `OpenClawClient.send_fill_notification()`**

추가할 테스트:

```python
@pytest.mark.asyncio
async def test_send_fill_notification_skips_when_n8n_webhook_missing(...):
    monkeypatch.setattr(settings, "N8N_FILL_WEBHOOK_URL", "")
    result = await OpenClawClient().send_fill_notification(order)
    assert result.status == "skipped"
    assert result.reason == "n8n_webhook_not_configured"

@pytest.mark.asyncio
async def test_send_fill_notification_posts_fill_payload_to_n8n(...):
    monkeypatch.setattr(settings, "N8N_FILL_WEBHOOK_URL", "http://127.0.0.1:5678/webhook/fill-notification")
    result = await OpenClawClient().send_fill_notification(order, correlation_id="corr-123")
    assert result.status == "success"
    called_json = mock_cli.post.call_args.kwargs["json"]
    assert called_json["display_name"] == "한화에어로"
    assert called_json["market_type"] == "kr"
    assert called_json["correlation_id"] == "corr-123"

@pytest.mark.asyncio
@pytest.mark.parametrize("market_type,account,symbol,expected_name", [
    ("kr", "kis", "012450", "한화에어로"),
    ("us", "kis", "NVDA", "NVDA"),
    ("crypto", "upbit", "KRW-BTC", "BTC"),
])
async def test_send_fill_notification_resolves_display_name(...):
    ...

@pytest.mark.asyncio
@pytest.mark.parametrize("market_type,account", [
    ("kr", "kis"),
    ("us", "kis"),
    ("crypto", "upbit"),
])
async def test_send_fill_notification_skips_below_minimum_for_all_markets(...):
    assert result.status == "skipped"
    assert result.reason == "below_minimum_notify_amount"
```

유지해야 할 계약도 테스트로 명시:
- 4회 재시도
- 실패/성공 여부와 무관하게 `notify_openclaw_message()` finally fallback 실행

**Step 2: Write failing monitor tests for moved filter responsibility**

기존 Upbit-only 필터 테스트를 대체:

```python
@pytest.mark.asyncio
async def test_send_fill_notification_always_routes_to_client(...):
    monitor.openclaw_client.send_fill_notification = AsyncMock(...)
    await monitor._send_fill_notification(low_amount_order)
    monitor.openclaw_client.send_fill_notification.assert_awaited_once()
```

기존 `test_send_fill_notification_skips_upbit_below_minimum`와
`test_send_fill_notification_does_not_filter_kis_low_amount`는 삭제 또는 통합.

**Step 3: Run targeted tests to confirm failure**

Run:

```bash
uv run pytest tests/test_openclaw_client.py -k "fill_notification" -xvs
uv run pytest tests/test_websocket_monitor.py -k "send_fill_notification" -xvs
```

Expected:
- 기존 OpenClaw thread / disabled 전제 테스트 다수 실패
- monitor minimum filter 테스트 실패

**Step 4: Commit the failing tests**

```bash
git add tests/test_openclaw_client.py tests/test_websocket_monitor.py
git commit -m "test: capture n8n fill delivery contract"
```

---

### Task 2: Add the new setting and replace fill delivery implementation

**Files:**
- Modify: `app/core/config.py`
- Modify: `app/services/openclaw_client.py`
- Modify: `app/core/kr_symbols.py` (read-only reference)

**Step 1: Add the new settings surface**

`app/core/config.py`:

```python
N8N_FILL_WEBHOOK_URL: str = ""
```

같은 task에서 `OPENCLAW_THREAD_KR|US|CRYPTO`는 제거한다. 단, `discord_webhook_us|kr|crypto`는 Task 6 전까지 유지할지 여부를 여기서 결정하지 말고 별도 task에서 처리한다.

**Step 2: Replace fill delivery in `OpenClawClient`**

구현 목표:

```python
async def send_fill_notification(self, order: FillOrderLike, *, correlation_id: str | None = None) -> FillNotificationDeliveryResult:
    normalized_order = coerce_fill_order(order)
    if not settings.N8N_FILL_WEBHOOK_URL.strip():
        return FillNotificationDeliveryResult(status="skipped", reason="n8n_webhook_not_configured")
    if normalized_order.filled_amount < 50_000:
        return FillNotificationDeliveryResult(status="skipped", reason="below_minimum_notify_amount")
    payload = _build_n8n_fill_payload(normalized_order, correlation_id=correlation_id)
    async for attempt in _build_openclaw_retrying():
        ...
        await cli.post(settings.N8N_FILL_WEBHOOK_URL, json=payload, headers={"Content-Type": "application/json"})
```

세부 구현:
- `_resolve_fill_analysis_thread_id`, `_build_fill_agent_message`, `OPENCLAW_FILL_AGENT_*`, `OPENCLAW_FILL_MARKET_LABELS`, `_resolve_fill_agent_market` 제거
- 새 helper 추가:
  - `_resolve_fill_display_name(order: FillOrder) -> str`
  - `_build_n8n_fill_payload(order: FillOrder, *, correlation_id: str | None) -> dict[str, Any]`
- KR display name:
  - `KR_SYMBOLS` 역매핑으로 조회
  - 미존재 시 심볼 코드 그대로 사용
- US display name:
  - 심볼 그대로
- Crypto display name:
  - `KRW-BTC -> BTC`
  - `USDT-BTC -> BTC`
  - 구분자 없으면 원문 사용

**Step 3: Preserve retry/logging/fallback behavior**

유지해야 하는 것:
- 4 attempts, exponential backoff `1 -> 2 -> 4`
- `request_id = str(uuid4())`
- finally 블록에서:

```python
await self._forward_to_telegram(
    format_fill_message(normalized_order),
    alert_type="fill",
    correlation_id=correlation_id,
    market_type=normalized_order.market_type,
)
```

로그는 `OpenClaw fill notification ...` 대신 실제 행위를 반영하도록 수정:
- `N8N fill notification send start`
- `N8N fill notification sent`
- `N8N fill notification failed after retries`

**Step 4: Run targeted tests and make them pass**

Run:

```bash
uv run pytest tests/test_openclaw_client.py -k "fill_notification" -xvs
```

Expected: PASS

**Step 5: Commit**

```bash
git add app/core/config.py app/services/openclaw_client.py tests/test_openclaw_client.py
git commit -m "feat: route fill notifications through n8n webhook"
```

---

### Task 3: Remove monitor-side amount filtering and keep result logging

**Files:**
- Modify: `websocket_monitor.py`
- Modify: `tests/test_websocket_monitor.py`

**Step 1: Remove local minimum filter**

`websocket_monitor.py`:
- `MIN_FILL_NOTIFY_AMOUNT = 50_000` 삭제
- `_send_fill_notification()` 앞단의 Upbit-specific early return 삭제
- monitor는 모든 fill을 client로 넘기고, skip/fail/success는 `FillNotificationDeliveryResult` 기반으로만 로그 남김

**Step 2: Keep external behavior stable**

유지:
- `fills_forwarded`는 `result.status == "success"`일 때만 증가
- skipped/failed logging 포맷 유지

**Step 3: Run monitor tests**

Run:

```bash
uv run pytest tests/test_websocket_monitor.py -k "send_fill_notification or fill processed" -xvs
```

Expected: PASS

**Step 4: Commit**

```bash
git add websocket_monitor.py tests/test_websocket_monitor.py
git commit -m "refactor: move fill minimum filtering into client"
```

---

### Task 4: Update environment and test fixtures for the new fill path

**Files:**
- Modify: `.env`
- Modify: `.env.test`
- Modify: `tests/conftest.py`

**Step 1: Update environment contracts**

`.env`:

```dotenv
N8N_FILL_WEBHOOK_URL=http://127.0.0.1:5678/webhook/fill-notification
```

삭제 후보:

```dotenv
OPENCLAW_THREAD_KR=
OPENCLAW_THREAD_US=
OPENCLAW_THREAD_CRYPTO=
```

`.env.test`에도 빈 값 추가:

```dotenv
N8N_FILL_WEBHOOK_URL=
```

**Step 2: Update shared test settings bootstrap**

`tests/conftest.py`에 기본값 추가:

```python
"N8N_FILL_WEBHOOK_URL": "",
```

필요하면 obsolete thread settings 기본값 제거.

**Step 3: Run config-adjacent tests**

Run:

```bash
uv run pytest tests/test_openclaw_client.py tests/test_websocket_monitor.py tests/test_taskiq_broker.py -xvs
```

Expected: PASS or only expected failures from Task 6 optional scope

**Step 4: Commit**

```bash
git add .env .env.test tests/conftest.py
git commit -m "chore: add n8n fill webhook setting"
```

---

### Task 5: Create and export the n8n `Fill Notification` workflow

**Files:**
- Create: `n8n/workflows/fill-notification.json`
- Modify: `n8n/README.md`
- Optional notes: `docs/plans/2026-03-20-fill-notification-thread-eval-implementation-plan.md`

**Step 1: Build the workflow in n8n UI**

노드 순서:

```text
Webhook -> Build Message -> Send Title -> Create Thread -> Send Data -> Wake OpenClaw
```

주의:
- Webhook path: `fill-notification`
- Response mode: immediate 200
- Discord token / OpenClaw bearer token은 n8n credential 또는 env variable expression 사용
- workflow JSON에 literal secret이 export되지 않는지 확인

**Step 2: Implement the Build Message code node**

핵심 출력 필드:

```javascript
return [{
  json: {
    title,
    threadName,
    detail,
    wakeText,
    market,
    displayName,
    sideText,
    priceStr,
    qtyStr,
  }
}];
```

추가 검증:
- `filled_at`이 KST 기준 `HH:MM`로 제목/스레드명에 반영되는지
- `USD` / `KRW` 포맷이 각각 `$1,234.56`, `1,234원` 형태로 나오는지

**Step 3: Export and track the workflow**

Run:

```bash
docker exec auto_trader_n8n_prod n8n export:workflow \
  --all --separate --output=/home/node/.n8n/workflows/
cp n8n/data/workflows/*.json n8n/workflows/
```

필요하면 exported filename을 `n8n/workflows/fill-notification.json`으로 정리한다.

**Step 4: Document operator steps**

`n8n/README.md`에 추가:
- fill notification workflow purpose
- import/export path
- localhost webhook smoke test command

**Step 5: Commit**

```bash
git add n8n/workflows/ n8n/README.md
git commit -m "feat: add n8n fill notification workflow"
```

---

### Task 6: Optional cleanup — remove market-specific Discord webhooks from notifier

**Files:**
- Modify: `app/main.py`
- Modify: `app/core/taskiq_broker.py`
- Modify: `websocket_monitor.py`
- Modify: `app/monitoring/trade_notifier.py`
- Modify: `tests/test_trade_notifier.py`
- Modify: `tests/test_taskiq_broker.py`
- Modify: `tests/test_websocket_monitor.py`
- Possible fallout: `app/jobs/analyze.py`, `app/jobs/kis_trading.py`, `app/jobs/kis_market_adapters.py`

**Step 1: Decide routing policy before deleting fields**

선택지 둘 중 하나를 명시적으로 고른다:
- `Option A`: 주문/분석/실패 알림도 모두 `discord_webhook_alerts` 단일 채널로 보낸다
- `Option B`: Discord는 alerts 전용만 남기고, 나머지는 Telegram-only fallback으로 돌린다

현재 코드상 `notify_buy_order`, `notify_sell_order`, `notify_cancel_orders`, `notify_analysis_complete`, `notify_trade_failure`, Toss recommendation helpers가 모두 market별 webhook에 의존한다. 이 결정을 먼저 하지 않으면 단순 삭제는 회귀를 만든다.

**Step 2: Rewrite notifier configuration surface**

예시:

```python
trade_notifier.configure(
    bot_token=bot_token,
    chat_ids=chat_ids,
    enabled=True,
    discord_webhook_alerts=settings.discord_webhook_alerts,
)
```

그리고 `TradeNotifier.__init__`, `configure`, `test_connection`, `_get_webhook_for_market_type` 관련 테스트를 정책에 맞게 정리한다.

**Step 3: Rewrite notifier methods or explicitly scope them out**

예시:

```python
webhook_url = self._discord_webhook_alerts
if webhook_url:
    ...
```

또는 Telegram fallback only로 단순화.

**Step 4: Run notifier/job tests**

Run:

```bash
uv run pytest tests/test_trade_notifier.py tests/test_taskiq_broker.py tests/test_websocket_monitor.py -xvs
```

필요시 추가:

```bash
uv run pytest tests/test_kis_tasks.py -k "notify_buy_order or notify_sell_order" -xvs
```

**Step 5: Commit**

```bash
git add app/main.py app/core/taskiq_broker.py websocket_monitor.py app/monitoring/trade_notifier.py tests/test_trade_notifier.py tests/test_taskiq_broker.py tests/test_websocket_monitor.py
git commit -m "refactor: consolidate discord notifier configuration"
```

---

### Task 7: End-to-end verification

**Files:**
- No code changes required

**Step 1: Run targeted automated tests**

Run:

```bash
uv run pytest \
  tests/test_openclaw_client.py \
  tests/test_websocket_monitor.py \
  tests/test_taskiq_broker.py \
  -xvs
```

Optional if Task 6 executed:

```bash
uv run pytest tests/test_trade_notifier.py -xvs
```

**Step 2: Run lint/type checks on touched files**

Run:

```bash
uv run ruff check app/core/config.py app/services/openclaw_client.py websocket_monitor.py app/monitoring/trade_notifier.py tests/test_openclaw_client.py tests/test_websocket_monitor.py tests/test_taskiq_broker.py tests/test_trade_notifier.py
uv run ty check app/services/openclaw_client.py websocket_monitor.py app/monitoring/trade_notifier.py
```

**Step 3: Smoke-test the n8n webhook**

Run:

```bash
curl -X POST http://127.0.0.1:5678/webhook/fill-notification \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "012450",
    "display_name": "한화에어로",
    "side": "bid",
    "filled_price": 1095000,
    "filled_qty": 1,
    "filled_amount": 1095000,
    "filled_at": "2026-03-20T11:17:00+09:00",
    "account": "kis",
    "market_type": "kr",
    "fill_status": "filled",
    "currency": "KRW"
  }'
```

Expected:
- `#trading-alerts` 제목 메시지 1건
- 해당 메시지에 새 스레드 생성
- 스레드 상세 메시지 전송
- OpenClaw wake 후 평가 메시지 생성

**Step 4: Runtime smoke test**

Run:

```bash
python websocket_monitor.py --mode both
```

실계좌/실체결 없이 확인 가능한 것:
- notifier/config bootstrap 예외 없음
- n8n webhook URL 설정 인식

**Step 5: Final commit**

```bash
git status
git add app/ tests/ n8n/ .env .env.test docs/plans/
git commit -m "feat: migrate fill notifications to n8n thread workflow"
```

---

## Risks to watch during implementation

- `OPENCLAW_ENABLED`는 scan/watch/analysis 경로에 여전히 사용된다. fill 전송 경로가 이 플래그에 종속되지 않도록 분리해야 한다.
- `TradeNotifier` market webhook 제거는 fill migration보다 훨씬 blast radius가 크다. 같은 PR에 넣으면 `app/jobs/*` 경로 회귀 가능성이 높다.
- n8n export JSON에 credential 값이 평문으로 들어가면 보안 사고다. export 전에 credential reference만 남는지 확인해야 한다.
- `KR_SYMBOLS` 역매핑은 런타임마다 dict comprehension을 새로 만들면 불필요한 비용이 생긴다. module-level cached reverse map helper를 고려한다.
- `filled_at`은 timezone-aware ISO 문자열과 naive 문자열이 섞일 수 있다. Build Message code node에서 `Invalid Date`가 나오지 않도록 샘플 payload를 3시장 모두 검증한다.
