# Watch Alerts Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Redis 기반 watch 조건(`price/rsi`, `above/below`)을 MCP에서 등록/조회/삭제하고, TaskIQ 주기 스캔으로 `crypto/kr/us`를 평가해 조건 충족 시 OpenClaw+Telegram 알림 후 watch를 삭제한다.

**Architecture:** `WatchAlertService`가 Redis 상태/검증을 담당하고, `WatchScanner`가 market open 판정(`exchange-calendars`) 및 조건 평가/배치 알림/삭제를 담당한다. 스케줄은 TaskIQ `@broker.task`로 `app/tasks`에만 선언하고, MCP는 `manage_watch_alerts`로 입력(`metric+operator`)을 내부 `condition_type`으로 변환해 서비스에 위임한다.

**Tech Stack:** Python 3.13+, TaskIQ, Redis(asyncio), exchange-calendars(XKRX/XNYS), FastMCP tooling, pytest + pytest-asyncio, uv, Ruff/Pyright.

---

참고 서브스킬: `@test-driven-development`, `@verification-before-completion`, `@requesting-code-review`

### Task 1: WatchAlertService 추가 (Redis CRUD + idempotent add)

**Files:**
- Create: `/Users/robin/PycharmProjects/auto_trader/app/services/watch_alerts.py`
- Create: `/Users/robin/PycharmProjects/auto_trader/tests/test_watch_alerts.py`

**Step 1: Write the failing test**

`tests/test_watch_alerts.py`에 Redis fake 기반 계약 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_add_watch_is_idempotent_and_preserves_created_at():
    service = WatchAlertService()
    fake_redis = _FakeRedisHash()
    service._get_redis = AsyncMock(return_value=fake_redis)  # type: ignore[method-assign]

    first = await service.add_watch("crypto", "btc", "price_below", 90000000)
    second = await service.add_watch("crypto", "BTC", "price_below", 90000000)

    assert first["created"] is True
    assert second["created"] is False
    assert second["already_exists"] is True

    all_rows = await service.list_watches("crypto")
    watch = all_rows["crypto"][0]
    assert watch["symbol"] == "BTC"
    assert watch["condition_type"] == "price_below"
    assert watch["metric"] == "price"
    assert watch["operator"] == "below"


@pytest.mark.asyncio
async def test_rsi_threshold_out_of_range_raises_value_error():
    service = WatchAlertService()
    with pytest.raises(ValueError, match="RSI threshold"):
        service.validate_watch_inputs(
            market="crypto",
            symbol="BTC",
            condition_type="rsi_below",
            threshold=101,
        )
```

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_watch_alerts.py -v
```

Expected: `ModuleNotFoundError` 또는 `AttributeError`로 FAIL.

**Step 3: Write minimal implementation**

`app/services/watch_alerts.py`에 아래 핵심 로직을 구현한다.

```python
class WatchAlertService:
    def validate_watch_inputs(self, market: str, symbol: str, condition_type: str, threshold: float) -> None:
        ...

    async def add_watch(self, market: str, symbol: str, condition_type: str, threshold: float) -> dict[str, object]:
        # HEXISTS -> already_exists이면 created_at 유지
        ...

    async def list_watches(self, market: str | None = None) -> dict[str, list[dict[str, object]]]:
        # condition_type에서 metric/operator 파생
        ...
```

추가 구현 포인트:
- key: `watch:alerts:{market}`
- field: `{SYMBOL}:{condition_type}:{canonical_threshold}`
- value: `{"created_at": now_kst().isoformat()}`
- `close()`에서 Redis client 정리

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_watch_alerts.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/services/watch_alerts.py /Users/robin/PycharmProjects/auto_trader/tests/test_watch_alerts.py
git commit -m "feat: add redis watch alert service with idempotent add"
```

### Task 2: WatchScanner 추가 (market open + price/rsi 평가 + 배치 알림)

**Files:**
- Create: `/Users/robin/PycharmProjects/auto_trader/app/jobs/watch_scanner.py`
- Create: `/Users/robin/PycharmProjects/auto_trader/tests/test_watch_scanner.py`

**Step 1: Write the failing test**

`tests/test_watch_scanner.py`에 다음 회귀 테스트를 먼저 작성한다.

```python
@pytest.mark.asyncio
async def test_scan_market_sends_single_batched_message_and_removes_only_triggered(monkeypatch):
    scanner = WatchScanner()
    scanner._watch_service = _FakeWatchService(
        rows=[
            {"symbol": "BTC", "condition_type": "price_below", "threshold": 100.0, "field": "BTC:price_below:100"},
            {"symbol": "ETH", "condition_type": "rsi_above", "threshold": 70.0, "field": "ETH:rsi_above:70"},
        ]
    )
    scanner._openclaw = _FakeOpenClawClient(success=True)

    monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)
    monkeypatch.setattr(scanner, "_get_price", AsyncMock(return_value=90.0))
    monkeypatch.setattr(scanner, "_get_rsi", AsyncMock(return_value=72.5))

    result = await scanner.scan_market("crypto")

    assert result["alerts_sent"] == 2
    assert len(scanner._openclaw.messages) == 1
    assert scanner._watch_service.removed_fields == [
        ("crypto", "BTC:price_below:100"),
        ("crypto", "ETH:rsi_above:70"),
    ]


@pytest.mark.asyncio
async def test_run_scans_all_markets_and_skips_closed_market(monkeypatch):
    scanner = WatchScanner()
    ...
```

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_watch_scanner.py -v
```

Expected: `ModuleNotFoundError` 또는 미구현 메서드로 FAIL.

**Step 3: Write minimal implementation**

`app/jobs/watch_scanner.py` 구현 포인트:

```python
class WatchScanner:
    async def scan_market(self, market: str) -> dict:
        if not self._is_market_open(market):
            return {"market": market, "skipped": True, "reason": "market_closed"}

        watches = await self._watch_service.get_watches_for_market(market)
        ...
        # market별 단일 배치 메시지 전송
        request_id = await self._openclaw.send_scan_alert(message)
        if request_id:
            for field in triggered_fields:
                await self._watch_service.trigger_and_remove(market, field)
```

필수 구현:
- `run()`에서 `("crypto", "kr", "us")` 순회
- `price_*`, `rsi_*` 모두 지원
- 장 시간 판정:
  - crypto: always true
  - kr: `exchange_calendars.get_calendar("XKRX")`
  - us: `exchange_calendars.get_calendar("XNYS")`

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_watch_scanner.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/jobs/watch_scanner.py /Users/robin/PycharmProjects/auto_trader/tests/test_watch_scanner.py
git commit -m "feat: add watch scanner for crypto kr us with market-hour gating"
```

### Task 3: OpenClaw 알림 메서드 확장 (`send_watch_alert`)

**Files:**
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/services/openclaw_client.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/tests/test_openclaw_client.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/jobs/watch_scanner.py`

**Step 1: Write the failing test**

`tests/test_openclaw_client.py`에 watch 전용 메서드 테스트를 추가한다.

```python
@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_watch_alert_success(mock_httpx_client_cls, monkeypatch):
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    ...

    result = await OpenClawClient().send_watch_alert("watch message")

    assert result is not None
    called_json = mock_cli.post.call_args.kwargs["json"]
    assert called_json["name"] == "auto-trader:watch"
    assert called_json["sessionKey"].startswith("auto-trader:watch:")
```

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_openclaw_client.py::test_send_watch_alert_success -v
```

Expected: `AttributeError: send_watch_alert`로 FAIL.

**Step 3: Write minimal implementation**

`openclaw_client.py`에서 공통 전송 메서드를 만들고 래퍼를 유지한다.

```python
async def _send_market_alert(self, message: str, category: str) -> str | None:
    ...

async def send_scan_alert(self, message: str) -> str | None:
    return await self._send_market_alert(message, category="scan")

async def send_watch_alert(self, message: str) -> str | None:
    return await self._send_market_alert(message, category="watch")
```

요구사항:
- OpenClaw 성공 시 Telegram 미러링 유지
- retry/backoff 정책은 기존과 동일
- `watch_scanner.py`에서 전송 호출을 `send_scan_alert()`에서 `send_watch_alert()`로 변경한다.

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_openclaw_client.py -k "scan_alert or watch_alert" -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/services/openclaw_client.py /Users/robin/PycharmProjects/auto_trader/app/jobs/watch_scanner.py /Users/robin/PycharmProjects/auto_trader/tests/test_openclaw_client.py
git commit -m "feat: add watch alert sender while keeping scan alert compatibility"
```

### Task 4: TaskIQ 스케줄 등록

**Files:**
- Create: `/Users/robin/PycharmProjects/auto_trader/app/tasks/watch_scan_tasks.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/tasks/__init__.py`
- Create: `/Users/robin/PycharmProjects/auto_trader/tests/test_watch_scan_tasks.py`

**Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_run_watch_scan_task_uses_scanner_and_closes(monkeypatch):
    from app.tasks.watch_scan_tasks import run_watch_scan_task

    scanner = _FakeScanner(result={"crypto": {"alerts_sent": 1}})
    monkeypatch.setattr("app.tasks.watch_scan_tasks.WatchScanner", lambda: scanner)

    result = await run_watch_scan_task()

    assert result == {"crypto": {"alerts_sent": 1}}
    assert scanner.closed is True
```

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_watch_scan_tasks.py -v
```

Expected: `ModuleNotFoundError`로 FAIL.

**Step 3: Write minimal implementation**

`app/tasks/watch_scan_tasks.py`:

```python
@broker.task(
    task_name="scan.watch_alerts",
    schedule=[{"cron": "*/5 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_watch_scan_task() -> dict:
    scanner = WatchScanner()
    try:
        return await scanner.run()
    finally:
        await scanner.close()
```

`app/tasks/__init__.py`에 `watch_scan_tasks` import와 `TASKIQ_TASK_MODULES` 반영.

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_watch_scan_tasks.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/tasks/watch_scan_tasks.py /Users/robin/PycharmProjects/auto_trader/app/tasks/__init__.py /Users/robin/PycharmProjects/auto_trader/tests/test_watch_scan_tasks.py
git commit -m "feat: schedule watch scanner task every 5 minutes"
```

### Task 5: MCP `manage_watch_alerts` 도구 추가

**Files:**
- Create: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/watch_alerts_registration.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/registry.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/__init__.py`
- Create: `/Users/robin/PycharmProjects/auto_trader/tests/test_mcp_watch_alerts.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py`

**Step 1: Write the failing test**

`tests/test_mcp_watch_alerts.py`에 MCP 계약 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_manage_watch_alerts_add_maps_metric_operator(monkeypatch):
    tools = build_tools()

    fake_service = _FakeWatchAlertService()
    monkeypatch.setattr(watch_alerts_registration, "WatchAlertService", lambda: fake_service)

    result = await tools["manage_watch_alerts"](
        action="add",
        market="crypto",
        symbol="btc",
        metric="price",
        operator="below",
        threshold=90000000,
    )

    assert result["success"] is True
    assert fake_service.add_calls[0][2] == "price_below"
```

그리고 `tests/test_mcp_tool_registration.py`에 신규 tool name 반영 assertion을 추가한다.

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_watch_alerts.py -v
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py -v
```

Expected: tool 미등록/미구현으로 FAIL.

**Step 3: Write minimal implementation**

`watch_alerts_registration.py` 구현 핵심:

```python
WATCH_ALERT_TOOL_NAMES = {"manage_watch_alerts"}

@mcp.tool(name="manage_watch_alerts", description="Manage watch alerts")
async def manage_watch_alerts(...):
    condition_type = f"{metric}_{operator}"
    ...
```

반영 포인트:
- `action=list`은 `market optional`
- `add/remove`는 `market,symbol,metric,operator,threshold` 필수 검증
- unknown action/invalid metric/operator는 에러 반환
- registry에서 `register_watch_alert_tools(mcp)` 호출
- `AVAILABLE_TOOL_NAMES`에 `manage_watch_alerts` 추가

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_watch_alerts.py -v
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/watch_alerts_registration.py /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/registry.py /Users/robin/PycharmProjects/auto_trader/app/mcp_server/__init__.py /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_watch_alerts.py /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py
git commit -m "feat: add mcp manage_watch_alerts tool and registry wiring"
```

### Task 6: 스캐너 데이터 소스 연결 (crypto/kr/us price+rsi)

**Files:**
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/jobs/watch_scanner.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/tests/test_watch_scanner.py`

**Step 1: Write the failing test**

market별 데이터 소스 호출을 검증하는 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_get_price_and_rsi_use_market_specific_sources(monkeypatch):
    scanner = WatchScanner()

    # kr
    mock_kis = AsyncMock()
    mock_kis.inquire_price.return_value = pd.DataFrame([
        {"close": 55000.0}
    ])
    mock_kis.inquire_daily_itemchartprice.return_value = _make_ohlcv([1, 2, 3, 4, 5] * 20)
    monkeypatch.setattr(watch_scanner_module, "KISClient", lambda: mock_kis)

    # us
    monkeypatch.setattr(watch_scanner_module.yahoo_service, "fetch_price", AsyncMock(return_value=pd.DataFrame([{"close": 190.0}])))
    monkeypatch.setattr(watch_scanner_module.yahoo_service, "fetch_ohlcv", AsyncMock(return_value=_make_ohlcv([1, 2, 3, 4, 5] * 20)))

    assert await scanner._get_price("005930", "kr") == 55000.0
    assert await scanner._get_price("AMZN", "us") == 190.0
```

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_watch_scanner.py::test_get_price_and_rsi_use_market_specific_sources -v
```

Expected: 미구현/소스 매핑 누락으로 FAIL.

**Step 3: Write minimal implementation**

`watch_scanner.py` 구현 업데이트:

```python
async def _get_price(self, symbol: str, market: str) -> float | None:
    if market == "crypto": ...
    if market == "kr": ...  # KIS inquire_price
    if market == "us": ...  # yahoo fetch_price

async def _get_rsi(self, symbol: str, market: str) -> float | None:
    if market == "crypto": ...
    if market == "kr": ...  # KIS daily chart + _calculate_rsi
    if market == "us": ...  # yahoo ohlcv + _calculate_rsi
```

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_watch_scanner.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/jobs/watch_scanner.py /Users/robin/PycharmProjects/auto_trader/tests/test_watch_scanner.py
git commit -m "feat: support kr us price and rsi evaluation in watch scanner"
```

### Task 7: 문서 업데이트 + 최종 검증

**Files:**
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/README.md`
- Modify: `/Users/robin/PycharmProjects/auto_trader/docs/plans/2026-02-17-watch-alerts-design.md` (필요 시 only if drift)

**Step 1: Write/update docs**

`app/mcp_server/README.md`에 `manage_watch_alerts` 사용법을 추가한다.

```markdown
- manage_watch_alerts(action, market, symbol, metric, operator, threshold)
  - action: add/remove/list
  - metric: price|rsi
  - operator: above|below
```

**Step 2: Run focused lint**

Run:
```bash
uv run ruff check /Users/robin/PycharmProjects/auto_trader/app/services/watch_alerts.py /Users/robin/PycharmProjects/auto_trader/app/jobs/watch_scanner.py /Users/robin/PycharmProjects/auto_trader/app/tasks/watch_scan_tasks.py /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/watch_alerts_registration.py /Users/robin/PycharmProjects/auto_trader/app/services/openclaw_client.py /Users/robin/PycharmProjects/auto_trader/tests/test_watch_alerts.py /Users/robin/PycharmProjects/auto_trader/tests/test_watch_scanner.py /Users/robin/PycharmProjects/auto_trader/tests/test_watch_scan_tasks.py /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_watch_alerts.py
```

Expected: no errors.

**Step 3: Run focused type check**

Run:
```bash
uv run pyright /Users/robin/PycharmProjects/auto_trader/app/services/watch_alerts.py /Users/robin/PycharmProjects/auto_trader/app/jobs/watch_scanner.py /Users/robin/PycharmProjects/auto_trader/app/tasks/watch_scan_tasks.py /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/watch_alerts_registration.py /Users/robin/PycharmProjects/auto_trader/app/services/openclaw_client.py
```

Expected: no type errors in touched files.

**Step 4: Run focused regression tests**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_watch_alerts.py /Users/robin/PycharmProjects/auto_trader/tests/test_watch_scanner.py /Users/robin/PycharmProjects/auto_trader/tests/test_watch_scan_tasks.py /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_watch_alerts.py /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py /Users/robin/PycharmProjects/auto_trader/tests/test_openclaw_client.py -k "watch_alert or scan_alert or registration" -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/mcp_server/README.md /Users/robin/PycharmProjects/auto_trader
# 필요 파일만 선별 add 권장
git commit -m "docs: document watch alert tool and finalize verification"
```
