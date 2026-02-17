# Upbit Cash Orderable Follow-up Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Upbit `get_cash_balance` 변경의 회귀 위험을 줄이기 위해 의미 혼동 방지와 테스트 커버리지를 보강한다.

**Architecture:** 기능 로직은 유지하고, `app/services/upbit.py`의 호환 래퍼 의미를 명확히 문서화한다. MCP 계층 경로(`account="upbit"`)와 호환 함수(`fetch_krw_balance`)에 대한 테스트를 추가해 계약을 고정한다.

**Tech Stack:** Python 3.13+, pytest/pytest-asyncio, uv, Ruff, Pyright.

---

참고 서브스킬: `@test-driven-development`, `@verification-before-completion`

### Task 1: `fetch_krw_balance` 의미 혼동 방지

**Files:**
- Modify: `/Users/robin/.codex/worktrees/8901/auto_trader/app/services/upbit.py`
- Test: `/Users/robin/.codex/worktrees/8901/auto_trader/tests/test_services.py`

**Step 1: Write the failing test**

`tests/test_services.py`에 호환 래퍼 동작 고정 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_fetch_krw_balance_is_backward_compatible_orderable(self, monkeypatch):
    monkeypatch.setattr(
        upbit_service_module,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 700000.0, "orderable": 500000.0}),
    )
    result = await upbit_service_module.fetch_krw_balance()
    assert result == 500000.0
```

**Step 2: Run test to verify it fails (if wrapper changed)**

Run:
```bash
uv run pytest --no-cov /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_services.py::TestUpbitService::test_fetch_krw_balance_is_backward_compatible_orderable -v
```

Expected: wrapper semantics mismatch 시 FAIL.

**Step 3: Write minimal implementation**

`app/services/upbit.py`의 `fetch_krw_balance()`에 다음 docstring을 추가한다.

```python
"""Backward-compatible alias returning orderable KRW only.

Prefer `fetch_krw_orderable_balance()` or `fetch_krw_cash_summary()` in new code.
"""
```

**Step 4: Run tests**

Run:
```bash
uv run pytest --no-cov /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_services.py::TestUpbitService -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/.codex/worktrees/8901/auto_trader/app/services/upbit.py /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_services.py
git commit -m "test: lock backward-compatible upbit krw balance semantics"
```

### Task 2: `account=\"upbit\"` 성공 경로 계약 고정

**Files:**
- Modify: `/Users/robin/.codex/worktrees/8901/auto_trader/tests/test_mcp_server_tools.py`

**Step 1: Write the failing test**

`account="upbit"` 성공 경로에서 `balance`/`orderable`/`formatted`를 검증하는 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_get_cash_balance_upbit_filter_success(monkeypatch):
    tools = build_tools()
    monkeypatch.setattr(
        upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 700000.0, "orderable": 500000.0}),
    )

    result = await tools["get_cash_balance"](account="upbit")
    assert len(result["accounts"]) == 1
    assert result["accounts"][0]["account"] == "upbit"
    assert result["accounts"][0]["balance"] == 700000.0
    assert result["accounts"][0]["orderable"] == 500000.0
    assert result["summary"]["total_krw"] == 700000.0
```

**Step 2: Run test**

Run:
```bash
uv run pytest --no-cov /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_mcp_server_tools.py::test_get_cash_balance_upbit_filter_success -v
```

Expected: PASS.

**Step 3: Run related regression**

Run:
```bash
uv run pytest --no-cov /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_mcp_server_tools.py -k "get_cash_balance" -q
```

Expected: PASS.

**Step 4: Commit**

```bash
git add /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_mcp_server_tools.py
git commit -m "test: add upbit account-filter cash balance success contract"
```

### Task 3: 품질 게이트

**Files:**
- Verify only

**Step 1: Lint**

Run:
```bash
uv run ruff check /Users/robin/.codex/worktrees/8901/auto_trader/app/services/upbit.py /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_services.py /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_mcp_server_tools.py
```

Expected: no errors.

**Step 2: Type check**

Run:
```bash
uv run pyright /Users/robin/.codex/worktrees/8901/auto_trader/app/services/upbit.py /Users/robin/.codex/worktrees/8901/auto_trader/app/mcp_server/tooling/portfolio_cash.py
```

Expected: no type errors.

**Step 3: Final test run**

Run:
```bash
uv run pytest --no-cov /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_services.py::TestUpbitService -q
uv run pytest --no-cov /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_mcp_server_tools.py -k "get_cash_balance" -q
```

Expected: PASS.

