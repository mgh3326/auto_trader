# ROB-829 Telegram Answer-First Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram 주문 승인 버튼의 callback query를 주문 처리 전에 `"처리 중"`으로 선응답하고, 주문 처리 뒤 최종 메시지 edit에 성공 또는 실패 사유를 표시한다.

**Architecture:** `handle_callback_update`가 callback query 메타데이터를 추출한 직후 기존 `_safe_answer`를 한 번 호출해 Telegram 스피너를 먼저 해제한다. 승인 해시, rung 전이, nonce/lease, 주문 제출, 원장 기록, commit-before-notify 흐름은 그대로 두고, 완료 후 `_safe_edit_message`가 렌더링하는 결과 요약에 주문 실패의 `detail["error"]`만 포함한다.

**Tech Stack:** Python 3.13, FastAPI service layer, pytest/pytest-asyncio, Ruff, ty

## Global Constraints

- 실주문 실행 흐름과 `approval_hash`, rung, 원장 기록, nonce/lease/멱등 가드는 변경하지 않는다.
- 텔레그램 응답 순서와 결과 메시지 렌더링만 변경한다.
- 데이터베이스 모델과 Alembic revision은 변경하지 않는다 (`migration-0`).
- `answerCallbackQuery`의 중립 텍스트는 정확히 `처리 중`이다.
- PR 생성까지만 수행하고 머지하지 않는다.

## Current-Flow Evidence

- `app/services/order_proposals/telegram_callback.py:303-305`에서 `revalidate_fn`이 실제 재검증·주문 처리를 수행한다.
- `app/services/order_proposals/telegram_callback.py:368-377`에서 결과 요약 생성, DB commit, `edit_message`, `answer_callback` 순으로 실행되어 callback answer가 마지막이다.
- `app/services/order_proposals/telegram_callback.py:335-360`의 재확인 경로도 edit/send 뒤 callback answer를 호출한다.
- `app/services/order_proposals/telegram_callback.py:261-305`의 nonce 소비, commit lease, 승인 기록, rung 전이, 주문 호출은 실주문 안전 경계이므로 수정 대상이 아니다.
- `app/services/order_proposals/revalidation.py:337-345`는 명시적 주문 거절을 `RungOutcome(result="error", detail={"error": ...})`로 반환한다.
- `app/monitoring/trade_notifier/notifier.py:90-92`는 Telegram 전송에 쓰는 공유 `httpx.AsyncClient`를 `timeout=10.0`으로 생성한다. `app/monitoring/trade_notifier/transports.py:93-99`와 `123-129`의 answer/edit 호출은 이 client를 사용하므로 API 콜 타임아웃이 이미 존재한다.

## Files

- Modify: `tests/services/order_proposals/test_telegram_callback.py` — answer/order/edit 호출 순서와 실패 사유 표시 회귀 테스트.
- Modify: `app/services/order_proposals/telegram_callback.py` — callback 선응답 위치와 실패 결과 요약.
- No migration files.

### Task 1: Write the callback-order and failure-detail regression tests

**Interfaces:**

- Consumes: `handle_callback_update(update, now=..., service_factory=..., notifier=..., revalidate_fn=...)`.
- Proves: notifier answer event occurs before `revalidate_fn`; final edit occurs after it; failed `RungOutcome.detail["error"]` is visible in the edited text.

- [x] **Step 1: Add an event-order test**

Add a notifier spy whose `answer_callback` and `edit_message` append `"answer"` and `"edit"` to a shared list. Have `fake_revalidate` append `"order"`, return a successful `RungOutcome`, invoke `handle_callback_update`, and assert:

```python
assert events == ["answer", "db", "order", "edit"]
assert notifier.answered == [("cbq-1", "처리 중")]
```

- [x] **Step 2: Add an order-failure edit test**

Return the same shape emitted by `revalidation._classify_submit` for a broker rejection:

```python
return [RungOutcome(0, "error", {"error": "broker_rejected"})]
```

Assert the handler remains handled as an approval result and the final edited text includes both the error label and the exact reason:

```python
assert result["reason"] == "approved"
assert "오류" in notifier.edited[0][2]
assert "broker\\_rejected" in notifier.edited[0][2]
```

- [x] **Step 3: Run the two tests and verify RED**

Run:

```bash
uv run pytest \
  tests/services/order_proposals/test_telegram_callback.py::test_approve_answers_before_order_processing_and_final_edit \
  tests/services/order_proposals/test_telegram_callback.py::test_order_failure_final_edit_includes_reason -q
```

Expected: the order test fails because current events are `db, order, edit, answer`; the failure-detail test fails because `_build_result_summary` currently renders only `오류`.

### Task 2: Move callback answer ahead of heavy work and render failure detail

**Interfaces:**

- Consumes: parsed Telegram callback query ID and existing `_safe_answer`/`_safe_edit_message` wrappers.
- Produces: one early `answer_callback(callback_query_id, "처리 중")`; unchanged order-processing return contracts; error summary line `- #N: 오류 — <reason>` when a reason exists.

- [x] **Step 1: Add the single early answer**

In `handle_callback_update`, immediately after extracting callback metadata and before allowlist parsing, proposal lookup, DB mutation, or `revalidate_fn`, call:

```python
await _safe_answer(active_notifier, callback_query_id, "처리 중")
```

Remove later answer calls for the same callback so Telegram receives exactly one answer. Do not change any service/DB/order statements.

- [x] **Step 2: Include explicit order failure reasons in the final summary**

In `_build_result_summary`, preserve all existing result labels and append `detail["error"]` only when it is present:

```python
reason = (outcome.detail or {}).get("error")
suffix = f" — {_escape_markdown(reason)}" if reason else ""
lines.append(f"- #{outcome.rung_index + 1}: {label}{suffix}")
```

- [x] **Step 3: Run the focused tests and verify GREEN**

Run the two-test command from Task 1. Expected: 2 passed.

- [x] **Step 4: Run the complete callback service suite**

Run:

```bash
uv run pytest tests/services/order_proposals/test_telegram_callback.py -q
```

Expected: all tests pass. Update existing assertions that expected late branch-specific answers so they instead require the single neutral early answer; do not weaken order/guard assertions.

### Task 3: Verify transport coverage, quality gates, and scope

- [x] **Step 1: Run Telegram transport tests**

```bash
uv run pytest tests/monitoring/test_trade_notifier_reply_markup.py tests/routers/test_telegram_callback_route.py -q
```

Expected: all tests pass and existing answer/edit transport contracts remain unchanged.

- [x] **Step 2: Run lint/type quality gate**

```bash
make lint
```

Expected: Ruff and ty exit 0.

- [x] **Step 3: Audit the diff against the live-order invariants**

```bash
git diff --check
git diff -- app/services/order_proposals/telegram_callback.py tests/services/order_proposals/test_telegram_callback.py docs/plans/ROB-829-telegram-answer-first.md
git status --short
```

Expected: only the plan, callback notification ordering/result rendering, and callback tests changed; no migration, order execution, approval hash, rung, ledger, or idempotency implementation changed.

### Task 4: Create the unmerged PR

- [x] **Step 1: Commit the scoped change**

```bash
git add docs/plans/ROB-829-telegram-answer-first.md \
  app/services/order_proposals/telegram_callback.py \
  tests/services/order_proposals/test_telegram_callback.py
git commit -m "perf(ROB-829): answer Telegram callbacks before orders"
```

- [x] **Step 2: Push the feature branch and create a PR without merging**

Use the repository ship workflow to push `rob-829` and open a PR against `main`. The PR body must state `migration-0`, list the RED/GREEN evidence and `make lint` result, and explicitly note that the order/guard/ledger/idempotency flow is unchanged.

## Self-Review

- Requirement coverage: early answer → Tasks 1–2; post-order edit and failure reason → Tasks 1–2; timeout confirmation → Current-Flow Evidence and Task 3; migration-0 → Global Constraints and Task 3; related suites/lint → Task 3; PR-only/no merge → Task 4.
- Scope: one service module, one service test module, one plan document; no independent subsystem or refactor.
- Placeholders: none.
- Type/interface consistency: `RungOutcome.detail` is an optional dict at the existing service boundary; notifier method signatures are unchanged.
