# Upbit Cash Orderable Legacy Wrapper Removal Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `fetch_krw_balance` 호환 래퍼를 제거하고 Upbit KRW 조회 API를 `fetch_krw_cash_summary` + `fetch_krw_orderable_balance`로 단일화한다.

**Architecture:** 저장소 전역 참조를 먼저 검증한 뒤, 미사용 래퍼(`fetch_krw_balance`)를 삭제한다. `check_krw_balance_sufficient`와 MCP 현금 조회 경로는 이미 신규 API를 사용하므로 기능 로직은 유지된다.

**Tech Stack:** Python 3.13+, pytest/pytest-asyncio, uv, Ruff, Pyright.

---

참고 서브스킬: `@test-driven-development`, `@verification-before-completion`

### Task 1: 삭제 안전성 사전 검증

**Files:**
- Verify only

**Step 1: Search direct references**

Run:
```bash
rg -n "fetch_krw_balance\\(" /Users/robin/.codex/worktrees/8901/auto_trader -S
```

Expected: `app/services/upbit.py` 함수 정의 1건만 출력.

**Step 2: Search symbol-level references**

Run:
```bash
rg -n "fetch_krw_balance" /Users/robin/.codex/worktrees/8901/auto_trader/app /Users/robin/.codex/worktrees/8901/auto_trader/tests -S
```

Expected: 런타임/테스트 참조 없음(정의 제외).

**Step 3: Commit (optional)**

코드 변경 없음. 커밋 생략.

### Task 2: Legacy Wrapper 제거

**Files:**
- Modify: `/Users/robin/.codex/worktrees/8901/auto_trader/app/services/upbit.py`

**Step 1: Write the failing test (reference guard)**

런타임 참조가 생기지 않도록 grep 기반 가드 검증을 먼저 실행한다.

Run:
```bash
rg -n "fetch_krw_balance\\(" /Users/robin/.codex/worktrees/8901/auto_trader/app /Users/robin/.codex/worktrees/8901/auto_trader/tests -S
```

Expected: 정의 1건만 존재.

**Step 2: Remove wrapper implementation**

`app/services/upbit.py`에서 아래 함수를 삭제한다.

```python
async def fetch_krw_balance() -> float:
    return await fetch_krw_orderable_balance()
```

**Step 3: Run static reference check**

Run:
```bash
rg -n "fetch_krw_balance\\(" /Users/robin/.codex/worktrees/8901/auto_trader/app /Users/robin/.codex/worktrees/8901/auto_trader/tests -S
```

Expected: 결과 없음.

**Step 4: Commit**

```bash
git add /Users/robin/.codex/worktrees/8901/auto_trader/app/services/upbit.py
git commit -m "refactor: remove unused upbit fetch_krw_balance wrapper"
```

### Task 3: 테스트 계약 보강 (신규 API 고정)

**Files:**
- Modify: `/Users/robin/.codex/worktrees/8901/auto_trader/tests/test_services.py`
- Modify: `/Users/robin/.codex/worktrees/8901/auto_trader/tests/test_mcp_server_tools.py`

**Step 1: Add/keep summary tests**

`fetch_krw_cash_summary` 관련 2개 계약을 유지/점검:
- `balance = orderable + locked`
- KRW row 없음 시 `{"balance": 0.0, "orderable": 0.0}`

**Step 2: Add explicit orderable path test**

`fetch_krw_orderable_balance`가 summary 기반으로 동작함을 유지/점검:

```python
summary = {"balance": 700000.0, "orderable": 500000.0}
assert await upbit_service_module.fetch_krw_orderable_balance() == 500000.0
```

**Step 3: Add upbit filter success path test**

`account="upbit"` 성공 경로에서 아래 계약 고정:
- `accounts` 1건
- `balance` 총액
- `orderable` 가용 금액
- `summary.total_krw`가 `balance` 합산값

**Step 4: Commit**

```bash
git add /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_services.py /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_mcp_server_tools.py
git commit -m "test: lock upbit cash summary and orderable contracts"
```

### Task 4: 문서 정합성 확인

**Files:**
- Verify/Modify: `/Users/robin/.codex/worktrees/8901/auto_trader/app/mcp_server/README.md`

**Step 1: Verify contract text**

`get_cash_balance` 섹션에 Upbit 계약이 아래와 일치하는지 확인:
- `balance`: total KRW (`balance + locked`)
- `orderable`: orderable KRW (`balance`)

**Step 2: Update only if mismatch**

문구 불일치 시 최소 수정.

**Step 3: Commit (if changed)**

```bash
git add /Users/robin/.codex/worktrees/8901/auto_trader/app/mcp_server/README.md
git commit -m "docs: align upbit cash balance contract wording"
```

### Task 5: 품질 게이트

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

**Step 3: Test run (no-cov for focused verification)**

Run:
```bash
uv run pytest --no-cov /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_services.py::TestUpbitService -q
uv run pytest --no-cov /Users/robin/.codex/worktrees/8901/auto_trader/tests/test_mcp_server_tools.py -k "get_cash_balance" -q
```

Expected: PASS.

