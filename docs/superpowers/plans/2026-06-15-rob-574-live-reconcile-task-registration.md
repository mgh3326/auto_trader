# ROB-574 Live Reconcile Task Registration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register inert, scheduleless TaskIQ wrappers for KIS and Toss live order reconcile so accepted-only live orders can be auto-booked only after explicit operator cron registration and safety-gate env flips.

**Architecture:** No new booking kernel is introduced. The existing KIS `kis_live_reconcile_orders_impl` and Toss `toss_reconcile_orders_impl` remain the only mutation paths; TaskIQ wrappers only call them with `dry_run=False` after two broker-specific default-off gates pass. The repo ships tasks registered but scheduleless, while recurrence stays in `robin-prefect-automations` and production activation stays behind final review.

**Tech Stack:** Python 3.13, TaskIQ, Pydantic settings, pytest/pytest-asyncio, existing MCP reconcile kernels.

---

## File Structure

- Modify `app/core/config.py` to add Toss auto-reconcile gates beside existing KIS live gates.
- Modify `app/tasks/__init__.py` to add `kis_live_reconcile_tasks` to `TASKIQ_TASK_MODULES` and import/register new `toss_live_reconcile_tasks`.
- Modify `app/tasks/kis_live_reconcile_tasks.py` docstring only, replacing the obsolete “not added” note with the ROB-574 registered-but-scheduleless contract.
- Create `app/tasks/toss_live_reconcile_tasks.py` as the Toss twin of the KIS paused wrapper.
- Modify `tests/test_config_flags.py` to assert all KIS/Toss gates default false.
- Modify `tests/tasks/test_kis_live_reconcile_tasks.py` to assert KIS task module registration and no recurring schedule.
- Create `tests/tasks/test_toss_live_reconcile_tasks.py` to cover registration, paused gates, and kernel invocation.
- Modify `docs/runbooks/kis-live-order-reconcile.md` to clarify KIS is registered but carries no in-code schedule.
- Modify `docs/runbooks/toss-live-order-reconcile.md` to document the new Toss paused task, independent gates, and recommended external cadence.

## Task 1: Config Gates For Toss

**Files:**
- Modify: `app/core/config.py`
- Modify: `tests/test_config_flags.py`

- [ ] **Step 1: Write the failing config test**

Replace `tests/test_config_flags.py` with:

```python
from app.core.config import settings


def test_live_auto_reconcile_flags_default_false():
    assert settings.KIS_LIVE_AUTO_RECONCILE_ENABLED is False
    assert settings.KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED is False
    assert settings.TOSS_LIVE_AUTO_RECONCILE_ENABLED is False
    assert settings.TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED is False
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
uv run pytest tests/test_config_flags.py -q
```

Expected: FAIL with `AttributeError` for `TOSS_LIVE_AUTO_RECONCILE_ENABLED`.

- [ ] **Step 3: Add default-off Toss gates**

In `app/core/config.py`, immediately after the existing KIS live auto-reconcile settings, change the block to:

```python
    # ROB-475 / ROB-574 — paused periodic auto-reconcile for KIS live KR orders.
    # Default off; operator flips + adds recurrence outside this repo.
    # ROB-487 adds a second default-off gate: flipping only the legacy flag
    # is no longer enough — a deployment must carry the fail-closed reconcile
    # semantics AND pass the safety review before unattended booking runs.
    KIS_LIVE_AUTO_RECONCILE_ENABLED: bool = False
    KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED: bool = False

    # ROB-574 — paused periodic auto-reconcile for Toss live KR/US orders.
    # Default off and scheduleless in this repo. Recurrence belongs to the
    # operator automation layer; unattended booking requires both gates.
    TOSS_LIVE_AUTO_RECONCILE_ENABLED: bool = False
    TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED: bool = False
```

- [ ] **Step 4: Run the focused test to verify it passes**

Run:

```bash
uv run pytest tests/test_config_flags.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/test_config_flags.py
git commit -m "feat(ROB-574): add Toss live auto reconcile gates"
```

## Task 2: Register Existing KIS Live Task Without Schedule

**Files:**
- Modify: `app/tasks/__init__.py`
- Modify: `app/tasks/kis_live_reconcile_tasks.py`
- Modify: `tests/tasks/test_kis_live_reconcile_tasks.py`

- [ ] **Step 1: Write the failing registration test**

At the top of `tests/tasks/test_kis_live_reconcile_tasks.py`, after the imports, add:

```python
def test_task_registered_without_recurring_schedule():
    import app.tasks as task_package

    assert mod in task_package.TASKIQ_TASK_MODULES
    labels = getattr(mod.kis_live_reconcile_periodic, "labels", {}) or {}
    assert labels.get("schedule") is None
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
uv run pytest tests/tasks/test_kis_live_reconcile_tasks.py -q
```

Expected: FAIL because `kis_live_reconcile_tasks` is not in `TASKIQ_TASK_MODULES`.

- [ ] **Step 3: Register the KIS task module**

In `app/tasks/__init__.py`, add `kis_live_reconcile_tasks` to `TASKIQ_TASK_MODULES` immediately before `kis_mock_reconciliation_tasks`:

```python
    journal_verdict_tasks,
    journal_counterfactual_tasks,
    kis_live_reconcile_tasks,
    kis_mock_reconciliation_tasks,
    watch_follow_up_tasks,
```

- [ ] **Step 4: Update the KIS task docstring**

Replace the docstring in `app/tasks/kis_live_reconcile_tasks.py` with:

```python
"""ROB-475 / ROB-574 — paused TaskIQ auto-reconcile for KIS live KR orders.

Registered with the worker so operators can kick or externally schedule it, but
it carries no in-code ``schedule=`` label. Recurrence is owned by
robin-prefect-automations plus env gate flips after safety review.

Reuses the proven kis_live_reconcile_orders_impl kernel. The accepted-only send
gate stays intact: no fills, journals, or realized PnL are booked unless broker
evidence is confirmed by the reconcile kernel.
"""
```

- [ ] **Step 5: Run the focused test to verify it passes**

Run:

```bash
uv run pytest tests/tasks/test_kis_live_reconcile_tasks.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/tasks/__init__.py app/tasks/kis_live_reconcile_tasks.py tests/tasks/test_kis_live_reconcile_tasks.py
git commit -m "feat(ROB-574): register KIS live reconcile task"
```

## Task 3: Add Toss Live Paused Task

**Files:**
- Create: `app/tasks/toss_live_reconcile_tasks.py`
- Modify: `app/tasks/__init__.py`
- Create: `tests/tasks/test_toss_live_reconcile_tasks.py`

- [ ] **Step 1: Write the failing Toss task tests**

Create `tests/tasks/test_toss_live_reconcile_tasks.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from app.tasks import toss_live_reconcile_tasks as mod


def test_task_registered_without_recurring_schedule():
    import app.tasks as task_package

    assert mod in task_package.TASKIQ_TASK_MODULES
    labels = getattr(mod.toss_live_reconcile_periodic, "labels", {}) or {}
    assert labels.get("schedule") is None


@pytest.mark.asyncio
async def test_paused_when_flag_disabled():
    with (
        patch.object(mod.settings, "TOSS_LIVE_AUTO_RECONCILE_ENABLED", False),
        patch.object(
            mod.settings, "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED", True
        ),
        patch.object(mod, "toss_reconcile_orders_impl", AsyncMock()) as kernel,
    ):
        result = await mod.toss_live_reconcile_periodic()
    assert result["status"] == "paused"
    assert "TOSS_LIVE_AUTO_RECONCILE_ENABLED" in result["message"]
    kernel.assert_not_awaited()


@pytest.mark.asyncio
async def test_paused_when_safety_review_flag_disabled():
    with (
        patch.object(mod.settings, "TOSS_LIVE_AUTO_RECONCILE_ENABLED", True),
        patch.object(
            mod.settings, "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED", False
        ),
        patch.object(mod, "toss_reconcile_orders_impl", AsyncMock()) as kernel,
    ):
        result = await mod.toss_live_reconcile_periodic()
    assert result["status"] == "paused"
    assert "SAFETY_REVIEW" in result["message"]
    kernel.assert_not_awaited()


@pytest.mark.asyncio
async def test_runs_kernel_when_enabled():
    fake = {"success": True, "counts": {"FILLED": 1}}
    with (
        patch.object(mod.settings, "TOSS_LIVE_AUTO_RECONCILE_ENABLED", True),
        patch.object(
            mod.settings, "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED", True
        ),
        patch.object(
            mod, "toss_reconcile_orders_impl", AsyncMock(return_value=fake)
        ) as kernel,
    ):
        result = await mod.toss_live_reconcile_periodic()
    kernel.assert_awaited_once_with(dry_run=False)
    assert result == fake
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
uv run pytest tests/tasks/test_toss_live_reconcile_tasks.py -q
```

Expected: FAIL with `ImportError` or module import failure because `toss_live_reconcile_tasks` does not exist.

- [ ] **Step 3: Create the Toss task wrapper**

Create `app/tasks/toss_live_reconcile_tasks.py`:

```python
"""ROB-574 — paused TaskIQ auto-reconcile for Toss live KR/US orders.

Registered with the worker so operators can kick or externally schedule it, but
it carries no in-code ``schedule=`` label. Recurrence is owned by
robin-prefect-automations plus env gate flips after safety review.

Reuses the proven toss_reconcile_orders_impl kernel. Send-time Toss order rows
remain accepted-only; fills, journals, and realized PnL are booked only from
confirmed single-order broker evidence.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.mcp_server.tooling.toss_live_ledger import toss_reconcile_orders_impl

logger = logging.getLogger(__name__)


@broker.task(task_name="toss_live.reconcile_periodic")  # no schedule -> paused
async def toss_live_reconcile_periodic() -> dict:
    if not settings.TOSS_LIVE_AUTO_RECONCILE_ENABLED:
        return {
            "status": "paused",
            "message": "TOSS_LIVE_AUTO_RECONCILE_ENABLED is False",
        }
    if not settings.TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED:
        return {
            "status": "paused",
            "message": "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED is False",
        }
    return await toss_reconcile_orders_impl(dry_run=False)
```

- [ ] **Step 4: Register the Toss module**

In `app/tasks/__init__.py`, add `toss_live_reconcile_tasks` to both the import list and `TASKIQ_TASK_MODULES`.

Import list:

```python
    research_run_refresh_tasks,
    toss_live_reconcile_tasks,
    toss_warnings_sync_tasks,
    upbit_symbol_universe_tasks,
```

Task module tuple:

```python
    us_candles_tasks,
    us_symbol_universe_tasks,
    toss_live_reconcile_tasks,
    toss_warnings_sync_tasks,
)
```

- [ ] **Step 5: Run the focused test to verify it passes**

Run:

```bash
uv run pytest tests/tasks/test_toss_live_reconcile_tasks.py -q
```

Expected: PASS.

- [ ] **Step 6: Run KIS and config tests again**

Run:

```bash
uv run pytest tests/test_config_flags.py tests/tasks/test_kis_live_reconcile_tasks.py tests/tasks/test_toss_live_reconcile_tasks.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/tasks/__init__.py app/tasks/toss_live_reconcile_tasks.py tests/tasks/test_toss_live_reconcile_tasks.py
git commit -m "feat(ROB-574): add Toss live reconcile task"
```

## Task 4: Runbook Updates

**Files:**
- Modify: `docs/runbooks/kis-live-order-reconcile.md`
- Modify: `docs/runbooks/toss-live-order-reconcile.md`

- [ ] **Step 1: Update KIS runbook wording**

In `docs/runbooks/kis-live-order-reconcile.md`, replace the `Paused TaskIQ 태스크` bullet in the Auto-reconcile section with:

```markdown
- **Paused TaskIQ 태스크**: `kis_live.reconcile_periodic` — worker에 등록되지만
  코드 내 `schedule=`은 없다. 외부 recurrence는 robin-prefect-automations에서
  등록한다. 활성화에는 **(ROB-487/ROB-574) 2개 플래그가 모두** 필요:
  `KIS_LIVE_AUTO_RECONCILE_ENABLED=true` **그리고**
  `KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED=true`. 하나라도 미설정 시
  `{"status":"paused"}`로 inert.
```

- [ ] **Step 2: Add Toss auto-reconcile section**

In `docs/runbooks/toss-live-order-reconcile.md`, after the manual single-order examples and before `## Status Semantics`, add:

```markdown
## Auto-reconcile (ROB-574)

수동 `toss_reconcile_orders(dry_run=False)` 반복을 피하려면 주기 자동 정산을
활성화한다. TaskIQ wrapper는 기존 증거-게이트 커널만 호출하며 새 booking 로직은
없다.

- **Paused TaskIQ 태스크**: `toss_live.reconcile_periodic` — worker에 등록되지만
  코드 내 `schedule=`은 없다. 외부 recurrence는 robin-prefect-automations에서
  등록한다.
- **Activation gates**: `TOSS_LIVE_AUTO_RECONCILE_ENABLED=true` **그리고**
  `TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED=true`가 모두 필요하다. 하나라도
  미설정 시 `{"status":"paused"}`로 inert.
- **권장 external cadence**: 장중 수 분 간격 reconcile + 장마감 후 sweep.
  정확한 cron은 운영 자동화 레포에서 관리한다.
- **Safety**: 배포만으로 자동 booking은 시작되지 않는다. cron 등록과 env flip은
  high-risk final review 이후 별도 operator 후속으로 진행한다.
```

- [ ] **Step 3: Verify docs mention no in-code schedule**

Run:

```bash
rg -n "kis_live.reconcile_periodic|toss_live.reconcile_periodic|schedule=|robin-prefect-automations|SAFETY_REVIEW" docs/runbooks/kis-live-order-reconcile.md docs/runbooks/toss-live-order-reconcile.md
```

Expected: output includes both task names, both safety review gates, and both references to external recurrence.

- [ ] **Step 4: Commit**

```bash
git add docs/runbooks/kis-live-order-reconcile.md docs/runbooks/toss-live-order-reconcile.md
git commit -m "docs(ROB-574): document live reconcile activation gates"
```

## Task 5: Final Verification And Hold

**Files:**
- No new file edits unless a verification failure exposes a bug.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
uv run pytest tests/test_config_flags.py tests/tasks/test_kis_live_reconcile_tasks.py tests/tasks/test_toss_live_reconcile_tasks.py -q
```

Expected: PASS.

- [ ] **Step 2: Run lint on changed Python files**

Run:

```bash
uv run ruff check app/core/config.py app/tasks/__init__.py app/tasks/kis_live_reconcile_tasks.py app/tasks/toss_live_reconcile_tasks.py tests/test_config_flags.py tests/tasks/test_kis_live_reconcile_tasks.py tests/tasks/test_toss_live_reconcile_tasks.py
```

Expected: PASS.

- [ ] **Step 3: Verify no recurring schedule labels were introduced**

Run:

```bash
uv run python - <<'PY'
from app.tasks import kis_live_reconcile_tasks, toss_live_reconcile_tasks

for task in (
    kis_live_reconcile_tasks.kis_live_reconcile_periodic,
    toss_live_reconcile_tasks.toss_live_reconcile_periodic,
):
    labels = getattr(task, "labels", {}) or {}
    assert labels.get("schedule") is None, (task.task_name, labels)
    print(task.task_name, "scheduleless")
PY
```

Expected:

```text
kis_live.reconcile_periodic scheduleless
toss_live.reconcile_periodic scheduleless
```

- [ ] **Step 4: Confirm git diff matches the approved scope**

Run:

```bash
git diff --stat HEAD~4..HEAD
git diff -- app/tasks app/core/config.py tests/test_config_flags.py tests/tasks docs/runbooks
```

Expected: only config gates, task registration/wrappers, tests, and runbooks changed. No cron labels, no broker kernel changes, no migrations, no live env defaults changed.

- [ ] **Step 5: Add Linear/PR hold note**

Use this note in the PR description or Linear comment:

```markdown
ROB-574 implementation is intentionally inert: KIS and Toss live reconcile TaskIQ wrappers are registered but carry no in-code schedule, and both brokers require independent default-off ENABLED + SAFETY_REVIEW gates before unattended booking can run. No cron registration or env flip is included. Applying `high_risk_change` + `hold_for_final_review`; no deploy activation or live recurring reconcile until Opus/CTO final review clears the operator automation plan.
```

## Self-Review

- Spec coverage: The plan covers KIS registration, Toss scheduleless wrapper, independent gates, docs, verification, and final-review hold. It excludes in-code cron and production env activation by design.
- Placeholder scan: No placeholder steps remain.
- Type consistency: The task names are `kis_live.reconcile_periodic` and `toss_live.reconcile_periodic`; the settings names are `KIS_LIVE_AUTO_RECONCILE_*` and `TOSS_LIVE_AUTO_RECONCILE_*`; both wrappers call their existing kernels with `dry_run=False`.
- Risk boundary: This is still a live-order booking boundary. The PR must carry `high_risk_change`, `candidate_for_opus`, and `hold_for_final_review`.
