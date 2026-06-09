# ROB-475 — 체결 자동 정산 (auto-booking) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 수동 `kis_live_reconcile_orders` 의존을 제거하기 위해, 검증된 reconcile 커널을 주기적으로 자동 실행하는 default-off scheduleless TaskIQ 태스크 + operator CLI + 도구/런북 명확화를 추가한다.

**Architecture:** 새 booking 로직 없음. `kis_live_reconcile_orders_impl` (`app/mcp_server/tooling/kis_live_ledger.py:522`)를 (1) paused TaskIQ 태스크와 (2) operator CLI가 호출한다. 기본 비활성 env 플래그(`KIS_LIVE_AUTO_RECONCILE_ENABLED`)로 게이트. cron 등록은 이 리포 밖(robin-prefect-automations). ROB-404 `kis_mock_reconciliation_tasks.py` 패턴과 동일.

**Tech Stack:** Python 3.13, TaskIQ (`app.core.taskiq_broker.broker`), pydantic-settings, pytest/pytest-asyncio.

**Migration:** 0.

---

### Task 1: Config 플래그 추가

**Files:**
- Modify: `app/core/config.py` (line ~492, `KIS_MOCK_RECONCILE_PERIODIC_ENABLED` 옆)
- Test: `tests/test_config_flags.py` (없으면 생성)

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_config_flags.py
from app.core.config import settings


def test_kis_live_auto_reconcile_flag_defaults_false():
    assert settings.KIS_LIVE_AUTO_RECONCILE_ENABLED is False
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_config_flags.py::test_kis_live_auto_reconcile_flag_defaults_false -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'KIS_LIVE_AUTO_RECONCILE_ENABLED'`

- [ ] **Step 3: 플래그 추가**

`app/core/config.py`의 `KIS_MOCK_RECONCILE_PERIODIC_ENABLED: bool = False` 바로 아래에:

```python
    # ROB-475 — paused periodic auto-reconcile for KIS live KR orders.
    # Default off; operator flips + adds cron in robin-prefect-automations.
    KIS_LIVE_AUTO_RECONCILE_ENABLED: bool = False
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_config_flags.py::test_kis_live_auto_reconcile_flag_defaults_false -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/core/config.py tests/test_config_flags.py
git commit -m "feat(ROB-475): add KIS_LIVE_AUTO_RECONCILE_ENABLED flag (default off)"
```

---

### Task 2: Paused TaskIQ 자동 reconcile 태스크

**Files:**
- Create: `app/tasks/kis_live_reconcile_tasks.py`
- Test: `tests/tasks/test_kis_live_reconcile_tasks.py`

`kis_mock_reconciliation_tasks.py`를 미러. `app/tasks/__init__.py`의 `TASKIQ_TASK_MODULES`에는 **추가하지 않는다** (paused 패턴 — operator가 후속으로 등록).

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/tasks/test_kis_live_reconcile_tasks.py
from unittest.mock import AsyncMock, patch

import pytest

from app.tasks import kis_live_reconcile_tasks as mod


@pytest.mark.asyncio
async def test_paused_when_flag_disabled():
    with patch.object(mod.settings, "KIS_LIVE_AUTO_RECONCILE_ENABLED", False), \
         patch.object(mod, "kis_live_reconcile_orders_impl", AsyncMock()) as kernel:
        result = await mod.kis_live_reconcile_periodic()
    assert result["status"] == "paused"
    kernel.assert_not_awaited()


@pytest.mark.asyncio
async def test_runs_kernel_when_enabled():
    fake = {"success": True, "counts": {"filled": 1}}
    with patch.object(mod.settings, "KIS_LIVE_AUTO_RECONCILE_ENABLED", True), \
         patch.object(mod, "kis_live_reconcile_orders_impl",
                      AsyncMock(return_value=fake)) as kernel:
        result = await mod.kis_live_reconcile_periodic()
    kernel.assert_awaited_once_with(dry_run=False)
    assert result == fake
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/tasks/test_kis_live_reconcile_tasks.py -v`
Expected: FAIL — `ModuleNotFoundError: app.tasks.kis_live_reconcile_tasks`

- [ ] **Step 3: 태스크 구현**

```python
# app/tasks/kis_live_reconcile_tasks.py
"""ROB-475 — paused taskiq periodic auto-reconcile for KIS live KR orders.

NO schedule: starts paused. An operator adds the cron in
robin-prefect-automations + flips KIS_LIVE_AUTO_RECONCILE_ENABLED in a
follow-up. Reuses the proven kis_live_reconcile_orders_impl kernel (accepted-
only send gate stays intact — ROB-395). NOT added to TASKIQ_TASK_MODULES.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.mcp_server.tooling.kis_live_ledger import kis_live_reconcile_orders_impl

logger = logging.getLogger(__name__)


@broker.task(task_name="kis_live.reconcile_periodic")  # no schedule → paused
async def kis_live_reconcile_periodic() -> dict:
    if not settings.KIS_LIVE_AUTO_RECONCILE_ENABLED:
        return {
            "status": "paused",
            "message": "KIS_LIVE_AUTO_RECONCILE_ENABLED is False",
        }
    return await kis_live_reconcile_orders_impl(dry_run=False)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/tasks/test_kis_live_reconcile_tasks.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: 커밋**

```bash
git add app/tasks/kis_live_reconcile_tasks.py tests/tasks/test_kis_live_reconcile_tasks.py
git commit -m "feat(ROB-475): paused periodic kis_live auto-reconcile task"
```

---

### Task 3: Operator CLI

**Files:**
- Create: `scripts/kis_live_auto_reconcile.py`
- Test: `tests/scripts/test_kis_live_auto_reconcile_cli.py`

CLI는 taskiq 없이 커널을 직접 호출한다(온디맨드/cron 연결용). dry-run 기본값.

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/scripts/test_kis_live_auto_reconcile_cli.py
from unittest.mock import AsyncMock, patch

import pytest

import scripts.kis_live_auto_reconcile as cli


@pytest.mark.asyncio
async def test_cli_default_dry_run_true():
    with patch.object(cli, "kis_live_reconcile_orders_impl",
                      AsyncMock(return_value={"success": True, "counts": {}})) as k:
        rc = await cli._run(dry_run=True)
    k.assert_awaited_once_with(dry_run=True)
    assert rc == 0


@pytest.mark.asyncio
async def test_cli_apply_passes_dry_run_false():
    with patch.object(cli, "kis_live_reconcile_orders_impl",
                      AsyncMock(return_value={"success": True, "counts": {}})) as k:
        rc = await cli._run(dry_run=False)
    k.assert_awaited_once_with(dry_run=False)
    assert rc == 0
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/scripts/test_kis_live_auto_reconcile_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.kis_live_auto_reconcile`

- [ ] **Step 3: CLI 구현**

```python
#!/usr/bin/env python3
"""ROB-475 — operator CLI for KIS live auto-reconcile.

Runs the same kernel as the paused taskiq task (kis_live.reconcile_periodic).
Use on-demand or wire to a cron. dry_run defaults to True (preview verdicts);
pass --apply to book fills/journals. Reuses the ROB-395 evidence-gated kernel —
no new mutation path. Prints the counts/summary; never prints secrets.

Exit codes:
    0  - success
    1  - kernel reported success=False

Usage:
    uv run python -m scripts.kis_live_auto_reconcile            # dry-run
    uv run python -m scripts.kis_live_auto_reconcile --apply    # book fills
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.mcp_server.tooling.kis_live_ledger import kis_live_reconcile_orders_impl


async def _run(*, dry_run: bool) -> int:
    result = await kis_live_reconcile_orders_impl(dry_run=dry_run)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0 if result.get("success") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="KIS live auto-reconcile (ROB-475)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="book fills/journals (dry_run=False). Default is dry-run preview.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(dry_run=not args.apply))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/scripts/test_kis_live_auto_reconcile_cli.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: 커밋**

```bash
git add scripts/kis_live_auto_reconcile.py tests/scripts/test_kis_live_auto_reconcile_cli.py
git commit -m "feat(ROB-475): operator CLI for kis_live auto-reconcile (dry-run default)"
```

---

### Task 4: 도구 설명 명확화 (제안 #2)

**Files:**
- Modify: `app/mcp_server/tooling/orders_kis_variants.py` (place_order desc ~378-397; reconcile desc ~554-560)
- Test: `tests/mcp_server/test_kis_live_tool_descriptions.py`

목표 문구: "reconcile는 로컬 부기 레이어이며, 실계좌 진실은 `get_holdings`/`get_available_capital`."

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/mcp_server/test_kis_live_tool_descriptions.py
from app.mcp_server.tooling import orders_kis_variants as mod


def _tool_desc(name: str) -> str:
    # descriptions live as string literals in the module source.
    import inspect
    return inspect.getsource(mod)


def test_place_order_desc_mentions_account_truth():
    src = _tool_desc("kis_live_place_order")
    assert "get_holdings" in src
    assert "get_available_capital" in src


def test_reconcile_desc_mentions_local_bookkeeping():
    src = _tool_desc("kis_live_reconcile_orders")
    assert "local bookkeeping" in src
```

> 참고: 도구 설명은 데코레이터 인자 문자열이므로 모듈 소스 문자열 검사로 단언한다(런타임 도구 객체 접근이 어려우면 이 방식이 안정적). 만약 이 리포에 도구 설명을 런타임으로 조회하는 헬퍼가 이미 있으면 그것을 사용한다.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/test_kis_live_tool_descriptions.py -v`
Expected: FAIL — 문자열 부재로 assert 실패

- [ ] **Step 3: 설명 문자열 수정**

`kis_live_place_order` 설명 끝(account_mode 문구 앞)에 추가:

```
                "Fills are NOT recorded at send time; run "
                "kis_live_reconcile_orders (or enable the operator-gated "
                "kis_live.reconcile_periodic task, ROB-475) to book "
                "fill/journal/realized_pnl. reconcile is the LOCAL bookkeeping "
                "layer; the live-account truth is get_holdings / "
                "get_available_capital. "
```

`kis_live_reconcile_orders` 설명 끝에 추가:

```
                "This is the LOCAL bookkeeping layer (trade/journal/"
                "realized_pnl); the live-account truth is get_holdings / "
                "get_available_capital. An operator-gated periodic auto-"
                "reconcile task exists (kis_live.reconcile_periodic, ROB-475). "
```

(기존 설명 문자열 연결 스타일을 그대로 따른다.)

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/mcp_server/test_kis_live_tool_descriptions.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/orders_kis_variants.py tests/mcp_server/test_kis_live_tool_descriptions.py
git commit -m "docs(ROB-475): clarify reconcile=local bookkeeping vs account truth in tool descriptions"
```

---

### Task 5: 런북 업데이트

**Files:**
- Modify: `docs/runbooks/kis-live-order-reconcile.md`

- [ ] **Step 1: 자동 정산 섹션 추가**

파일 끝에 섹션 추가:

```markdown
## Auto-reconcile (ROB-475)

수동 `kis_live_reconcile_orders(dry_run=False)` 반복을 피하려면 주기 자동 정산을
활성화한다. 둘 다 동일한 증거-게이트 커널을 호출하며 새 mutation 경로는 없다.

- **CLI (온디맨드/cron)**: `uv run python -m scripts.kis_live_auto_reconcile`
  (dry-run 기본), 실제 booking은 `--apply`.
- **Paused TaskIQ 태스크**: `kis_live.reconcile_periodic` — 기본 비활성.
  활성화: `KIS_LIVE_AUTO_RECONCILE_ENABLED=true` + cron 등록(robin-prefect-
  automations). 플래그 미설정 시 `{"status":"paused"}`로 inert.

> **reconcile은 로컬 부기 레이어**(trade/journal/realized_pnl)다. 실계좌 진실은
> `get_holdings` / `get_available_capital`. reconcile 미실행은 실계좌에 영향을
> 주지 않으며, 로컬 리포트/성과추적만 비게 된다.
```

- [ ] **Step 2: 커밋**

```bash
git add docs/runbooks/kis-live-order-reconcile.md
git commit -m "docs(ROB-475): runbook auto-reconcile activation section"
```

---

### Task 6: 전체 검증

- [ ] **Step 1: 관련 테스트 + lint**

Run:
```bash
uv run pytest tests/test_config_flags.py tests/tasks/test_kis_live_reconcile_tasks.py \
  tests/scripts/test_kis_live_auto_reconcile_cli.py \
  tests/mcp_server/test_kis_live_tool_descriptions.py -v
uv run ruff check app/ scripts/ tests/ && uv run ruff format --check app/ scripts/ tests/
```
Expected: 모든 테스트 PASS, ruff clean (CI는 app/ + tests/ 둘 다 검사 — 메모리 교훈).

- [ ] **Step 2: 회귀 — reconcile 커널 무변경 확인**

Run: `uv run pytest tests/ -v -k "kis_live_ledger or kis_live_reconcile" -m "not integration"`
Expected: 기존 reconcile 테스트 전부 PASS (커널 시그니처/동작 무변경).

---

## Self-Review 체크
- 스펙 컴포넌트 1(커널 재사용)=Task 2/3에서 호출, 2(태스크)=Task 2, 3(CLI)=Task 3, 4(도구/런북)=Task 4/5, 5(config)=Task 1. 전부 커버.
- placeholder 없음. 모든 step에 실제 코드/명령.
- 타입 일관성: 태스크/CLI 모두 `kis_live_reconcile_orders_impl(dry_run=...)` 단일 시그니처 호출.
- Migration 0, 새 mutation 경로 0, default-off inert.
