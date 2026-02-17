# DCA Runtime Removal Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove DCA runtime functionality end-to-end (`create_dca_plan`, `get_dca_status`, DCA service/model/monitor hooks) while keeping existing DB tables untouched.

**Architecture:** The change is contract-first: remove DCA tools from MCP registration, then delete DCA-only runtime modules and dependencies. Monitoring and event publishing are simplified to execution-only behavior. Database schema is intentionally left unchanged in this implementation; migration-chain cleanup is tracked as a follow-up plan.

**Tech Stack:** Python 3.13+, FastMCP tooling, SQLAlchemy ORM, pytest/pytest-asyncio, uv, Ruff, Pyright.

---

참고 서브스킬: `@test-driven-development`, `@verification-before-completion`

### Task 1: MCP Public Contract에서 DCA Tool 제거

**Files:**
- Modify: `/Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/__init__.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/portfolio_holdings.py`

**Step 1: Write the failing test**

`tests/test_mcp_tool_registration.py`에 DCA 툴 부재 계약 테스트를 추가한다.

```python
def test_removed_dca_tools_are_not_registered() -> None:
    mcp = DummyMCP()
    register_all_tools(mcp)

    assert "create_dca_plan" not in mcp.tools
    assert "get_dca_status" not in mcp.tools
```

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py::test_removed_dca_tools_are_not_registered -v
```

Expected: FAIL (`create_dca_plan`/`get_dca_status` currently still registered).

**Step 3: Write minimal implementation**

`app/mcp_server/__init__.py`와 `app/mcp_server/tooling/portfolio_holdings.py`에서 DCA 툴명을 제거한다.

```python
# app/mcp_server/__init__.py
AVAILABLE_TOOL_NAMES = [
    # ...
    "simulate_avg_cost",
    "update_manual_holdings",
    # "create_dca_plan",
    # "get_dca_status",
    "analyze_stock",
]
```

```python
# app/mcp_server/tooling/portfolio_holdings.py
PORTFOLIO_TOOL_NAMES: set[str] = {
    "get_holdings",
    "get_position",
    "get_cash_balance",
    "simulate_avg_cost",
    "update_manual_holdings",
}
```

그리고 `@mcp.tool(name="create_dca_plan")`, `@mcp.tool(name="get_dca_status")` 블록을 삭제한다.

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py /Users/robin/PycharmProjects/auto_trader/app/mcp_server/__init__.py /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/portfolio_holdings.py
git commit -m "refactor: remove dca tools from mcp public contract"
```

### Task 2: DCA MCP Runtime Module 삭제 및 Import 정리

**Files:**
- Delete: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/portfolio_dca_core.py`
- Delete: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/portfolio_dca_status.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py`

**Step 1: Write the failing test**

`tests/test_mcp_server_tools.py`에 MCP 도구 사전에서 DCA 툴 키가 없는지 검사하는 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_dca_tools_removed_from_build_tools():
    tools = build_tools()
    assert "create_dca_plan" not in tools
    assert "get_dca_status" not in tools
```

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py::test_dca_tools_removed_from_build_tools -v
```

Expected: FAIL (현재 `build_tools()` 결과에 DCA tool 존재).

**Step 3: Write minimal implementation**

- DCA 전용 모듈 2개를 삭제한다.
- `portfolio_holdings.py`에서 아래 import를 제거한다.

```python
from app.mcp_server.tooling.portfolio_dca_core import (
    create_dca_plan_impl,
    simulate_avg_cost_impl,
)
from app.mcp_server.tooling.portfolio_dca_status import (
    get_dca_status_impl as _get_dca_status_impl,
)
from app.services.dca_service import DcaService
from app.mcp_server.tooling.shared import MCP_DCA_USER_ID as _MCP_DCA_USER_ID
```

변경 후:

```python
from app.mcp_server.tooling.portfolio_dca_core import simulate_avg_cost_impl
```

또는 `simulate_avg_cost_impl`를 별도 모듈로 이동한 뒤 해당 경로만 import한다.

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py::test_dca_tools_removed_from_build_tools -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/portfolio_holdings.py /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py
git add /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/portfolio_dca_core.py /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/portfolio_dca_status.py
git commit -m "refactor: delete dca mcp runtime modules"
```

### Task 3: DCA 모델/서비스 제거

**Files:**
- Delete: `/Users/robin/PycharmProjects/auto_trader/app/services/dca_service.py`
- Delete: `/Users/robin/PycharmProjects/auto_trader/app/models/dca_plan.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/models/__init__.py`
- Delete: `/Users/robin/PycharmProjects/auto_trader/tests/test_dca_service.py`

**Step 1: Write the failing test**

`tests/test_mcp_server_tools.py` 최상단 DCA import 제거 전에 아래 가드를 추가한다.

```python
def test_models_package_no_longer_exports_dca() -> None:
    import app.models as models

    assert not hasattr(models, "DcaPlan")
    assert not hasattr(models, "DcaPlanStep")
```

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py::test_models_package_no_longer_exports_dca -v
```

Expected: FAIL (`app.models` currently exports DCA symbols).

**Step 3: Write minimal implementation**

`app/models/__init__.py`에서 DCA import/export 제거:

```python
# remove
from .dca_plan import (
    DcaPlan,
    DcaPlanStatus,
    DcaPlanStep,
    DcaStepStatus,
)

# __all__ 에서도 DCA 항목 제거
```

그리고 DCA service/model 파일 및 `tests/test_dca_service.py`를 삭제한다.

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py::test_models_package_no_longer_exports_dca -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/models/__init__.py /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py
git add /Users/robin/PycharmProjects/auto_trader/app/services/dca_service.py /Users/robin/PycharmProjects/auto_trader/app/models/dca_plan.py /Users/robin/PycharmProjects/auto_trader/tests/test_dca_service.py
git commit -m "refactor: remove dca model and service layer"
```

### Task 4: KIS WebSocket Monitor에서 DCA 연동 제거

**Files:**
- Modify: `/Users/robin/PycharmProjects/auto_trader/kis_websocket_monitor.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/tests/test_kis_websocket_monitor.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/tests/test_execution_event.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/services/execution_event.py`

**Step 1: Write the failing test**

`tests/test_kis_websocket_monitor.py`에 DCA 비의존 계약 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_on_execution_with_order_id_publishes_without_dca_lookup():
    monitor = KISWebSocketMonitor()
    mock_publish = AsyncMock()
    with patch("kis_websocket_monitor.publish_execution_event", mock_publish):
        event = {"type": "execution", "market": "kr", "order_id": "ORDER-123"}
        await monitor._on_execution(event)

    mock_publish.assert_awaited_once_with(event)
    assert "dca_next_step" not in event
```

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_kis_websocket_monitor.py::test_on_execution_with_order_id_publishes_without_dca_lookup -v
```

Expected: FAIL (현재 monitor가 DCA lookup 경로를 유지).

**Step 3: Write minimal implementation**

`kis_websocket_monitor.py`에서 DCA 관련 필드/메서드 제거:

```python
class KISWebSocketMonitor:
    def __init__(self):
        self.websocket_client: KISExecutionWebSocket | None = None
        self._db_engine: AsyncEngine | None = None
        self.is_running = False

    async def _on_execution(self, event: dict[str, Any]):
        await self._publish_execution_event(event)
```

`app/services/execution_event.py` docstring의 옵션 필드 설명에서 `dca_next_step` 문구를 삭제한다.

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_kis_websocket_monitor.py -v
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_execution_event.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/kis_websocket_monitor.py /Users/robin/PycharmProjects/auto_trader/app/services/execution_event.py /Users/robin/PycharmProjects/auto_trader/tests/test_kis_websocket_monitor.py /Users/robin/PycharmProjects/auto_trader/tests/test_execution_event.py
git commit -m "refactor: remove dca hooks from kis websocket monitor"
```

### Task 5: DCA 전용 Indicator Helper 및 테스트 정리

**Files:**
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/market_data_indicators.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py`

**Step 1: Write the failing test**

`tests/test_mcp_server_tools.py`에 helper 부재 계약 테스트를 추가한다.

```python
def test_compute_dca_price_levels_helper_removed():
    assert not hasattr(market_data_indicators, "_compute_dca_price_levels")
```

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py::test_compute_dca_price_levels_helper_removed -v
```

Expected: FAIL (helper currently exists).

**Step 3: Write minimal implementation**

`market_data_indicators.py`에서 `_compute_dca_price_levels` 함수와 `__all__` 항목 제거.

```python
# __all__ cleanup
__all__ = [
    # ...
    "_compute_rsi_weights",
    # "_compute_dca_price_levels",
    "_normalize_number",
]
```

그리고 기존 `TestComputeDcaPriceLevels`, `TestCreateDcaPlan*`, `TestGetDcaStatus` 블록을 삭제하고 남은 테스트 import를 정리한다.

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/market_data_indicators.py /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py
git commit -m "refactor: remove dca indicator helper and tests"
```

### Task 6: 문서/정적검증/잔여참조 검증

**Files:**
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/README.md`
- Modify: `/Users/robin/PycharmProjects/auto_trader/docs/plans/2026-02-17-dca-removal-design.md` (필요 시 후속 migration 메모 보강)
- Create: `/Users/robin/PycharmProjects/auto_trader/docs/plans/2026-02-17-dca-migration-rebaseline-followup-plan.md` (선택)

**Step 1: Write the failing test**

문서 계약 테스트를 추가한다 (`tests/test_mcp_tool_registration.py` 또는 새 테스트 파일).

```python
def test_available_tool_names_exclude_removed_dca_tools():
    assert "create_dca_plan" not in AVAILABLE_TOOL_NAMES
    assert "get_dca_status" not in AVAILABLE_TOOL_NAMES
```

**Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py::test_available_tool_names_exclude_removed_dca_tools -v
```

Expected: FAIL if list cleanup 누락.

**Step 3: Write minimal implementation**

- `app/mcp_server/README.md`에서 DCA 툴 목록/설명 삭제
- 필요 시 후속 문서에 migration rebaseline 실행 계획을 명시:

```markdown
신규 DB에서 DCA 테이블 생성 차단은 migration chain 재정리(rebaseline)로 별도 진행한다.
기존 DB와 revision 정합성을 위해 현재 리비전 파일 직접 삭제는 금지한다.
```

**Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py -v
uv run ruff check /Users/robin/PycharmProjects/auto_trader/app /Users/robin/PycharmProjects/auto_trader/tests
uv run pyright /Users/robin/PycharmProjects/auto_trader/app
```

Expected: PASS / no diagnostics.

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/app/mcp_server/README.md /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py
git add /Users/robin/PycharmProjects/auto_trader/docs/plans/2026-02-17-dca-removal-design.md /Users/robin/PycharmProjects/auto_trader/docs/plans/2026-02-17-dca-migration-rebaseline-followup-plan.md
git commit -m "docs: sync mcp contract after dca runtime removal"
```

### Final Verification Gate (must run before merge)

Run:
```bash
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py -q
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py -q
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_kis_websocket_monitor.py -q
uv run pytest /Users/robin/PycharmProjects/auto_trader/tests/test_execution_event.py -q
uv run ruff check /Users/robin/PycharmProjects/auto_trader/app /Users/robin/PycharmProjects/auto_trader/tests
uv run pyright /Users/robin/PycharmProjects/auto_trader/app
rg -n "create_dca_plan|get_dca_status|DcaService|from app.models.dca_plan|dca_next_step" /Users/robin/PycharmProjects/auto_trader/app /Users/robin/PycharmProjects/auto_trader/tests
```

Expected:
- 테스트/정적분석 통과
- `rg` 결과 0건 (문서 제외)

