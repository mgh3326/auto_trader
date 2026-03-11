# Issue #272 screen_stocks US Limit Expansion Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `screen_stocks`의 공개 계약을 `default=20, max=50`에서 `default=50, max=100`으로 올려서 US 스크리닝 풀이 좁아지는 문제를 해결하고, 기존 `category` 기반 US sector 필터 동작은 그대로 유지한다.

**Architecture:** 구현은 contract-first로 진행한다. 먼저 MCP/tool, FastAPI router, service 경계에서 새 기본값과 상한을 테스트로 고정하고, 그 다음 공개 진입점(`analysis_registration.py`, `analysis_tool_handlers.py`, `app/routers/screener.py`, `app/services/screener_service.py`, dashboard JS fallback, README)을 일관되게 맞춘다. `analysis_screen_core.py`의 screening 엔진은 이미 `limit=100`까지 흘릴 수 있으므로, 20/50 전제를 가진 경계 코드만 우선 수정하고, core 변경은 테스트가 실제 병목을 보여줄 때만 한다.

**Tech Stack:** Python 3.13+, FastMCP, FastAPI, yfinance/tvscreener, pytest, uv

---

## Verified Current State

- `app/mcp_server/tooling/analysis_tool_handlers.py`의 `screen_stocks_impl()`는 이미 `limit > 100`을 `100`으로 clamp한다.
- 하지만 MCP 등록부 `app/mcp_server/tooling/analysis_registration.py`는 여전히 `limit: int = 20`이다.
- 웹 경계 `app/routers/screener.py`는 `default=20, le=50`로 묶여 있다.
- `app/services/screener_service.py`도 `limit: int = 20`이며, `min_volume` 후처리용 overfetch가 `min(50, ...)`라서 100 요청을 끝까지 못 살린다.
- `app/mcp_server/README.md`는 아직 `limit: Maximum results 1-50 (default: 20)`라고 적혀 있다.
- US sector 필터는 새 파라미터가 필요한 상태가 아니다. 이미 `category`가 legacy US path에서 `sector` 조건으로 매핑되고, 관련 회귀 테스트(`test_us_category_with_max_rsi_falls_back_to_legacy_path`)도 존재한다.
- 따라서 이 이슈의 핵심 변경은 “새 `limit` 파라미터 추가”가 아니라 “기존 `limit/category` 계약을 50/100 기준으로 재정렬”이다.

### Task 1: MCP `screen_stocks` 기본값/상한 계약을 테스트로 먼저 고정

**Files:**
- Modify: `tests/test_mcp_screen_stocks_kr.py`
- Modify: `tests/test_mcp_screen_stocks_filters_and_rsi.py`
- Modify: `tests/_mcp_screen_stocks_support.py`
- Modify: `app/mcp_server/tooling/analysis_registration.py`
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py`

**Step 1: Write the failing MCP default-limit regression**

`tests/test_mcp_screen_stocks_kr.py`의 facade test에서 기본 호출이 `screen_stocks_unified(..., limit=50)`을 넘기는지 고정한다.

```python
@pytest.mark.asyncio
async def test_screen_stocks_tool_uses_analysis_screening_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = build_tools()
    called: dict[str, Any] = {}

    async def fake_screen(**kwargs: Any) -> dict[str, Any]:
        called.update(kwargs)
        return {
            "results": [],
            "total_count": 0,
            "returned_count": 0,
            "filters_applied": {"market": "kr"},
            "market": "kr",
            "timestamp": "2026-03-11T00:00:00Z",
            "meta": {"source": "screening-facade"},
        }

    monkeypatch.setattr(analysis_screening, "screen_stocks_unified", fake_screen)

    await tools["screen_stocks"](market="kr")

    assert called["limit"] == 50
```

**Step 2: Update the existing clamp tests to the new ceiling**

`tests/test_mcp_screen_stocks_filters_and_rsi.py`와 `tests/_mcp_screen_stocks_support.py`의 `test_limit_over_50_capped`를 `test_limit_over_100_capped`로 바꾸고, 기대치를 `<= 100`으로 올린다. `limit=0` 테스트는 더 이상 `"between 1 and 50"`를 찾지 말고 `"at least 1"` 또는 현재 실제 에러 텍스트를 찾도록 바꾼다.

```python
with pytest.raises(ValueError, match="at least 1"):
    await tools["screen_stocks"](..., limit=0)


result = await tools["screen_stocks"](..., limit=999)
assert result["returned_count"] <= 100
```

**Step 3: Run the focused MCP tests to verify RED**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks_kr.py tests/test_mcp_screen_stocks_filters_and_rsi.py -k "screen_stocks_tool_uses_analysis_screening_facade or limit_" -q`

Expected: FAIL because the registered MCP default is still `20`, and the legacy tests still encode the `50` ceiling.

**Step 4: Update the MCP public defaults only at the entrypoints**

`app/mcp_server/tooling/analysis_registration.py`와 `app/mcp_server/tooling/analysis_tool_handlers.py`에서 `screen_stocks`/`screen_stocks_impl`의 기본 `limit`을 `50`으로 올린다. `screen_stocks_impl()`의 clamp 동작은 유지하되 ceiling만 `100`으로 둔다. 여기서는 `analysis_screen_core.py` 내부 fetch algorithm은 건드리지 않는다.

```python
async def screen_stocks(..., limit: int = 50) -> dict[str, Any]:
    return await screen_stocks_impl(..., limit=limit)


async def screen_stocks_impl(..., limit: int = 50) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if limit > 100:
        limit = 100
```

**Step 5: Re-run the focused MCP tests to verify GREEN**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks_kr.py tests/test_mcp_screen_stocks_filters_and_rsi.py -k "screen_stocks_tool_uses_analysis_screening_facade or limit_" -q`

Expected: PASS

**Step 6: Commit**

```bash
git add tests/test_mcp_screen_stocks_kr.py tests/test_mcp_screen_stocks_filters_and_rsi.py tests/_mcp_screen_stocks_support.py app/mcp_server/tooling/analysis_registration.py app/mcp_server/tooling/analysis_tool_handlers.py
git commit -m "feat: raise screen_stocks MCP limit defaults"
```

---

### Task 2: Screener service/API 경계를 50/100 계약에 맞추고 overfetch 병목을 제거

**Files:**
- Modify: `tests/test_screener_service.py`
- Modify: `tests/test_screener_router.py`
- Modify: `app/services/screener_service.py`
- Modify: `app/routers/screener.py`
- Modify: `app/templates/screener_dashboard.html`

**Step 1: Write the failing service test for 100-limit overfetch**

`tests/test_screener_service.py`에 `min_volume` 후처리 시 request limit이 커져도 overfetch가 `50`에 걸리지 않도록 고정하는 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_list_screening_min_volume_overfetch_caps_at_100(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [],
            "total_count": 0,
            "returned_count": 0,
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=fake_redis)
    await service.list_screening(market="us", min_volume=1000, limit=80)

    assert mock_screen.await_args.kwargs["limit"] == 100
```

**Step 2: Write the failing router default/max validation tests**

`tests/test_screener_router.py`에:
- `GET /api/screener/list`에서 `limit` 생략 시 service가 `limit=50`을 받는지
- `limit=101`이면 FastAPI validation으로 `422`가 나는지

를 고정하는 테스트를 추가한다.

```python
def test_list_endpoint_uses_default_limit_50(client):
    test_client, fake_service = client

    response = test_client.get("/api/screener/list", params={"market": "us"})

    assert response.status_code == 200
    assert fake_service.list_screening.await_args.kwargs["limit"] == 50


def test_list_endpoint_rejects_limit_over_100(client):
    test_client, _ = client

    response = test_client.get("/api/screener/list", params={"market": "us", "limit": 101})

    assert response.status_code == 422
```

**Step 3: Run the focused service/router tests to verify RED**

Run: `uv run pytest --no-cov tests/test_screener_service.py tests/test_screener_router.py -k "limit or overfetch" -q`

Expected: FAIL because the service still defaults to `20`, overfetch still caps at `50`, and the router schema still declares `le=50`.

**Step 4: Align the web/service boundary with the MCP contract**

다음 경계를 함께 수정한다.

- `app/services/screener_service.py`
  - `list_screening()` / `refresh_screening()` 기본 `limit=50`
  - `_calculate_overfetch_limit()`를 `min(100, max(request_limit * 3, request_limit))`로 조정
- `app/routers/screener.py`
  - `ScreenerFilterRequest.limit = Field(default=50, ge=1, le=100)`
  - `screener_list()` query `limit = Query(default=50, ge=1, le=100)`
- `app/templates/screener_dashboard.html`
  - JS fallback `Number(... || 50)`로 변경

```python
@staticmethod
def _calculate_overfetch_limit(request_limit: int) -> int:
    return min(100, max(request_limit * 3, request_limit))


class ScreenerFilterRequest(BaseModel):
    ...
    limit: int = Field(default=50, ge=1, le=100)


@router.get("/api/screener/list")
async def screener_list(..., limit: int = Query(default=50, ge=1, le=100), ...):
    ...
```

```javascript
const payload = {
    market: marketSelect.value,
    sort_by: sortBySelect.value,
    sort_order: document.getElementById("sort-order").value,
    limit: Number(document.getElementById("limit").value || 50),
};
```

**Step 5: Re-run the focused service/router tests to verify GREEN**

Run: `uv run pytest --no-cov tests/test_screener_service.py tests/test_screener_router.py -k "limit or overfetch" -q`

Expected: PASS

**Step 6: Commit**

```bash
git add tests/test_screener_service.py tests/test_screener_router.py app/services/screener_service.py app/routers/screener.py app/templates/screener_dashboard.html
git commit -m "feat: align screener web limits with screen_stocks"
```

---

### Task 3: README와 회귀 테스트로 새 공개 계약을 문서화하고 scope drift를 막기

**Files:**
- Modify: `app/mcp_server/README.md`
- Modify: `tests/test_mcp_screen_stocks_tvscreener_contract.py`

**Step 1: Add/adjust a regression test that encodes the scope decision**

이미 있는 US category 회귀 테스트를 유지하면서, 기본 limit이 올라가도 `category="Technology"` 같은 sector filter는 기존 legacy path로 계속 흐른다는 점을 한 번 더 명시적으로 고정한다. 필요하면 같은 test block에 “새 sector 파라미터를 추가하지 않는다”는 의미의 assertion comment를 덧붙인다.

```python
@pytest.mark.asyncio
async def test_us_category_with_max_rsi_falls_back_to_legacy_path(self, monkeypatch):
    ...
    result = await tools["screen_stocks"](
        market="us",
        category="Technology",
        max_rsi=35,
        limit=50,
    )

    assert result["filters_applied"]["category"] == "Technology"
```

**Step 2: Update the living MCP contract doc**

`app/mcp_server/README.md`의 `screen_stocks` spec에서 limit 설명을 실제 계약으로 맞춘다.

```markdown
- `limit`: Maximum results 1-100 (default: 50)
```

같은 섹션에 이미 있는 `category: sector for US` 설명은 유지하고, 새 파라미터를 추가했다는 식의 문구는 넣지 않는다.

**Step 3: Run the doc-adjacent regression tests**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks_tvscreener_contract.py -k "us_category_with_max_rsi_falls_back_to_legacy_path" -q`

Expected: PASS

**Step 4: Run a grep sanity check for live contract drift**

Run: `rg -n "screen_stocks|1-50|default: 20|ge=1, le=50|\\|\\| 20" app/mcp_server/README.md app/mcp_server/tooling/analysis_registration.py app/mcp_server/tooling/analysis_tool_handlers.py app/services/screener_service.py app/routers/screener.py app/templates/screener_dashboard.html`

Expected:
- no remaining `screen_stocks`-related `1-50`, `default: 20`, `le=50`, `|| 20`
- unrelated/historical docs outside this live contract surface may remain untouched

**Step 5: Commit**

```bash
git add app/mcp_server/README.md tests/test_mcp_screen_stocks_tvscreener_contract.py
git commit -m "docs: update screen_stocks limit contract"
```

---

### Task 4: Final regression sweep for the widened limit contract

**Files:**
- Verify only; no new files expected

**Step 1: Run the combined targeted regression suite**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks_kr.py tests/test_mcp_screen_stocks_filters_and_rsi.py tests/test_mcp_screen_stocks_tvscreener_contract.py tests/test_screener_service.py tests/test_screener_router.py -k "limit or overfetch or category_with_max_rsi or screen_stocks_tool_uses_analysis_screening_facade" -q`

Expected: PASS

**Step 2: Run one broader screen_stocks contract pass**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks_*.py -q`

Expected: PASS, or at worst only unrelated pre-existing failures. If new failures appear, fix them before claiming completion.

**Step 3: Manual contract spot-check**

If local runtime is available, sanity-check one direct call path:

```python
result = await tools["screen_stocks"](market="us")
assert result["filters_applied"]["market"] == "us"
```

And one larger explicit limit:

```python
result = await tools["screen_stocks"](market="us", limit=100)
assert result["returned_count"] <= 100
```

**Step 4: Commit the final verification state**

```bash
git add app/mcp_server/README.md app/mcp_server/tooling/analysis_registration.py app/mcp_server/tooling/analysis_tool_handlers.py app/services/screener_service.py app/routers/screener.py app/templates/screener_dashboard.html tests/test_mcp_screen_stocks_kr.py tests/test_mcp_screen_stocks_filters_and_rsi.py tests/test_mcp_screen_stocks_tvscreener_contract.py tests/test_screener_service.py tests/test_screener_router.py tests/_mcp_screen_stocks_support.py
git commit -m "feat: expand screen_stocks limit contract to 100"
```

## Out of Scope / Do Not Change

- `screen_stocks`에 새로운 `sector` 파라미터를 추가하지 않는다. 기존 `category`가 US sector 역할을 이미 수행한다.
- `analysis_screen_core.py` 내부 candidate ranking/refetch algorithm을 광범위하게 재설계하지 않는다. 테스트가 실제 20/50 병목을 보여줄 때만 최소 수정한다.
- 과거 검증 보고서(`docs/VERIFICATION_SUMMARY.md`, `docs/manual_endpoint_verification.md` 등) 같은 historical artifact는 retroactive rewrite하지 않는다.

Plan complete and saved to `docs/plans/2026-03-11-issue-272-screen-stocks-us-limit-implementation-plan.md`. Two execution options:

**1. Subagent-Driven (this session)** - I dispatch fresh subagent per task, review between tasks, fast iteration

**2. Parallel Session (separate)** - Open new session with `superpowers:executing-plans`, batch execution with checkpoints

**Which approach?**
