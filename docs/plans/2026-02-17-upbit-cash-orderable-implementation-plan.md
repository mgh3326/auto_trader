# Upbit Cash Orderable Contract Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** MCP `get_cash_balance`에서 Upbit 계좌도 KIS처럼 `balance`(총 KRW)와 `orderable`(실주문가능 KRW)을 함께 반환한다.

**Architecture:** Upbit KRW 계산 로직은 `app/services/upbit.py` 서비스 경계에 집중하고, MCP 계층(`portfolio_cash`)은 요약 결과만 소비한다. 기존 `fetch_krw_balance()` 의미 혼란은 명시 함수(`fetch_krw_orderable_balance`)와 요약 함수(`fetch_krw_cash_summary`)를 추가해 단계적으로 해소한다.

**Tech Stack:** Python 3.13+, FastAPI/MCP tooling, pytest + pytest-asyncio, uv, Ruff/Pyright.

---

참고 서브스킬: `@test-driven-development`, `@verification-before-completion`

### Task 1: Upbit KRW Summary API 추가 (서비스 경계)

**Files:**
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/services/upbit.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/tests/test_services.py`

**Step 1: Write the failing test**

`tests/test_services.py`의 `TestUpbitService`에 아래 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_fetch_krw_cash_summary_includes_locked(self, monkeypatch):
    monkeypatch.setattr(
        upbit_service_module,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {"currency": "KRW", "balance": "500000.0", "locked": "200000.0"}
            ]
        ),
    )

    summary = await upbit_service_module.fetch_krw_cash_summary()

    assert summary["balance"] == 700000.0
    assert summary["orderable"] == 500000.0


@pytest.mark.asyncio
async def test_fetch_krw_orderable_balance_reads_summary(self, monkeypatch):
    monkeypatch.setattr(
        upbit_service_module,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 700000.0, "orderable": 500000.0}),
    )

    result = await upbit_service_module.fetch_krw_orderable_balance()
    assert result == 500000.0
```

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_services.py::TestUpbitService::test_fetch_krw_cash_summary_includes_locked -v
```

Expected: `AttributeError` 또는 `NameError` (`fetch_krw_cash_summary` 미구현).

**Step 3: Write minimal implementation**

`app/services/upbit.py`에 함수 3개를 추가/정리한다.

```python
async def fetch_krw_cash_summary() -> dict[str, float]:
    accounts = await fetch_my_coins()
    for account in accounts:
        if account.get("currency") == "KRW":
            orderable = float(account.get("balance", 0) or 0)
            locked = float(account.get("locked", 0) or 0)
            return {"balance": orderable + locked, "orderable": orderable}
    return {"balance": 0.0, "orderable": 0.0}


async def fetch_krw_orderable_balance() -> float:
    summary = await fetch_krw_cash_summary()
    return float(summary["orderable"])


async def fetch_krw_balance() -> float:
    """Backward-compatible alias for orderable KRW.

    New code should use fetch_krw_orderable_balance() or fetch_krw_cash_summary().
    """
    return await fetch_krw_orderable_balance()
```

그리고 `check_krw_balance_sufficient()`는 `fetch_krw_orderable_balance()`를 호출하도록 변경한다.

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_services.py::TestUpbitService::test_fetch_krw_cash_summary_includes_locked -v
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_services.py::TestUpbitService::test_fetch_krw_orderable_balance_reads_summary -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/services/upbit.py /Users/robin/PycharmProjects/auto_trader/tests/test_services.py
git commit -m "feat: add upbit krw cash summary and explicit orderable accessor"
```

### Task 2: MCP get_cash_balance에서 Upbit `balance/orderable` 계약 반영

**Files:**
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/portfolio_cash.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py`

**Step 1: Write the failing test**

`tests/test_mcp_server_tools.py`에서 Upbit 모킹을 `fetch_krw_cash_summary` 기준으로 바꾸고, `orderable` 검증을 추가한다.

```python
monkeypatch.setattr(
    upbit_service,
    "fetch_krw_cash_summary",
    AsyncMock(return_value={"balance": 700000.0, "orderable": 500000.0}),
)

result = await tools["get_cash_balance"](account="upbit")
upbit = result["accounts"][0]
assert upbit["balance"] == 700000.0
assert upbit["orderable"] == 500000.0
assert upbit["formatted"] == "700,000 KRW"
```

추가로 전체 계좌 조회 테스트(`test_get_cash_balance_all_accounts`)에서도 Upbit `orderable` 필드 존재를 검증한다.

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py::test_get_cash_balance_all_accounts -v
```

Expected: Upbit `orderable` missing 또는 모킹 대상 함수 불일치로 FAIL.

**Step 3: Write minimal implementation**

`app/mcp_server/tooling/portfolio_cash.py` Upbit 분기를 다음처럼 변경한다.

```python
summary = await upbit_service.fetch_krw_cash_summary()
krw_balance = float(summary.get("balance", 0.0))
krw_orderable = float(summary.get("orderable", 0.0))
accounts.append(
    {
        "account": "upbit",
        "account_name": "기본 계좌",
        "broker": "upbit",
        "currency": "KRW",
        "balance": krw_balance,
        "orderable": krw_orderable,
        "formatted": f"{int(krw_balance):,} KRW",
    }
)
```

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py::test_get_cash_balance_all_accounts -v
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py::test_get_cash_balance_with_account_filter -v
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py::test_get_cash_balance_partial_failure -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/portfolio_cash.py /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py
git commit -m "feat: include upbit orderable in mcp cash balance response"
```

### Task 3: 계약 문서 정합성 및 회귀 검증

**Files:**
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/README.md`
- Test: `/Users/robin/PycharmProjects/auto_trader/tests/test_services.py`
- Test: `/Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py`

**Step 1: Write the failing test**

KRW row 누락 시 요약이 0으로 반환되는 회귀 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_fetch_krw_cash_summary_returns_zero_without_krw(self, monkeypatch):
    monkeypatch.setattr(
        upbit_service_module,
        "fetch_my_coins",
        AsyncMock(return_value=[{"currency": "BTC", "balance": "0.1", "locked": "0"}]),
    )
    summary = await upbit_service_module.fetch_krw_cash_summary()
    assert summary == {"balance": 0.0, "orderable": 0.0}
```

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_services.py::TestUpbitService::test_fetch_krw_cash_summary_returns_zero_without_krw -v
```

Expected: 함수/리턴 계약 미반영 상태면 FAIL.

**Step 3: Write minimal implementation**

Task 1 구현에 0.0 fallback이 이미 포함되어 있으면 추가 구현 없이 테스트만 정리한다.  
동시에 `app/mcp_server/README.md`의 `get_cash_balance` 설명을 아래 의미로 보강한다.
- Upbit `balance`: 총 KRW(가용+locked)
- Upbit `orderable`: 실주문가능 KRW

**Step 4: Run tests to verify they pass**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_services.py::TestUpbitService -v
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py -k "get_cash_balance" -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/mcp_server/README.md /Users/robin/PycharmProjects/auto_trader/tests/test_services.py /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py
git commit -m "docs: document upbit cash balance and orderable semantics"
```

### Task 4: 품질 게이트 (Lint/Type/Regression)

**Files:**
- Test only (no code changes required unless failures occur)

**Step 1: Run focused lint**

Run:
```bash
uv run ruff check /Users/robin/PycharmProjects/auto_trader/app/services/upbit.py /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/portfolio_cash.py /Users/robin/PycharmProjects/auto_trader/tests/test_services.py /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py
```

Expected: no errors.

**Step 2: Run focused type check**

Run:
```bash
uv run pyright /Users/robin/PycharmProjects/auto_trader/app/services/upbit.py /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/portfolio_cash.py
```

Expected: no type errors in touched files.

**Step 3: Run regression tests**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_services.py::TestUpbitService -v
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py -k "get_cash_balance" -v
```

Expected: PASS.

**Step 4: Commit (if any final fixups were needed)**

```bash
git add /Users/robin/PycharmProjects/auto_trader
git commit -m "chore: finalize upbit cash orderable contract verification"
```

(수정이 없다면 커밋 생략)
