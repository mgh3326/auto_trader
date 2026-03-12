# Refactor Priority Program Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce maintenance cost across the validated refactor hotspots while preserving KIS automation payloads, MCP response contracts, and existing public handler entrypoints.

**Architecture:** Start with the KIS automation cluster because it combines duplicated runtime orchestration with duplicated test scaffolding and already has an internal result-contract seam in `app/services/kis_trading_contracts.py`. Follow with `analysis_recommend.py`, then continue the already-approved facade-first MCP screening refactor, and only then split the analyzer/prompt stack after adding characterization around retry, validation, and persistence behavior.

**Tech Stack:** Python 3.13+, TaskIQ jobs, FastMCP, pytest, pandas, Ruff, ty

---

### Task 1: Freeze KIS automation contracts and extract reusable test support

**Files:**
- Create: `tests/_kis_tasks_support.py`
- Modify: `tests/test_kis_tasks.py`
- Test: `tests/test_kis_tasks.py`

**Step 1: Write the failing tests**

Add focused regressions that lock down the shared scenario surface you will reuse during the refactor.

```python
from tests._kis_tasks_support import (
    build_domestic_holdings_scenario,
    build_overseas_holdings_scenario,
)


@pytest.mark.asyncio
async def test_domestic_automation_contract_preserves_step_order(monkeypatch):
    scenario = build_domestic_holdings_scenario()
    result = await kis_tasks.run_per_domestic_stock_automation(**scenario.call_kwargs)
    assert [step["step"] for step in result["results"][0]["steps"]] == [
        "analysis",
        "cancel_buy_orders",
        "buy",
        "cancel_sell_orders",
        "sell",
    ]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_kis_tasks.py -k "contract_preserves_step_order or _kis_tasks_support" -q`

Expected: FAIL because `tests/_kis_tasks_support.py` does not exist and the new shared scenario helpers are not wired.

**Step 3: Write minimal implementation**

Create `tests/_kis_tasks_support.py` with fixture factories that return repeated `DummyKIS`, manual-service, and notifier setups without changing production behavior.

```python
def build_domestic_holdings_scenario() -> AutomationScenario:
    return AutomationScenario(
        client_factory=DummyDomesticKIS,
        manual_service_factory=EmptyManualService,
        notifier_factory=RecordingNotifier,
        call_kwargs={"user_id": 1},
    )
```

Update `tests/test_kis_tasks.py` to consume the helpers for the new regressions only.

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_kis_tasks.py -k "contract_preserves_step_order or _kis_tasks_support" -q`

Expected: PASS

**Step 5: Commit**

```bash
git add tests/_kis_tasks_support.py tests/test_kis_tasks.py
git commit -m "test: add KIS automation scenario helpers"
```

---

### Task 2: Extract a shared KIS automation runner behind stable task entrypoints

**Files:**
- Create: `app/jobs/kis_automation_runner.py`
- Modify: `app/jobs/kis_trading.py`
- Modify: `tests/test_kis_tasks.py`
- Test: `tests/test_kis_tasks.py`

**Step 1: Write the failing test**

Add a seam test that proves the public job still delegates through a shared runner.

```python
from app.jobs import kis_automation_runner


@pytest.mark.asyncio
async def test_domestic_task_uses_shared_automation_runner(monkeypatch):
    called: dict[str, object] = {}

    async def fake_runner(*, market: str, **kwargs):
        called["market"] = market
        return {"status": "completed", "results": []}

    monkeypatch.setattr(kis_automation_runner, "run_market_automation", fake_runner)

    result = await kis_tasks.run_per_domestic_stock_automation(user_id=1)

    assert result["status"] == "completed"
    assert called == {"market": "domestic"}
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_kis_tasks.py -k "uses_shared_automation_runner" -q`

Expected: FAIL because `app/jobs/kis_automation_runner.py` and `run_market_automation(...)` do not exist yet.

**Step 3: Write minimal implementation**

Create `app/jobs/kis_automation_runner.py` with a `run_market_automation(...)` coordinator that accepts the common dependencies and calls market-specific hooks.

```python
async def run_market_automation(*, market: str, holdings_loader, analyzer, step_hooks):
    holdings = await holdings_loader()
    results: list[dict[str, Any]] = []
    for holding in holdings:
        steps = await step_hooks.run_all(holding=holding, analyzer=analyzer)
        results.append({"name": holding["name"], "steps": steps})
    return {"status": "completed", "results": results}
```

Update `app/jobs/kis_trading.py` so the domestic and overseas task functions build dependencies and delegate to the runner while keeping their public signatures and payload shapes.

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_kis_tasks.py -k "uses_shared_automation_runner or domestic or overseas" -q`

Expected: PASS

**Step 5: Commit**

```bash
git add app/jobs/kis_automation_runner.py app/jobs/kis_trading.py tests/test_kis_tasks.py
git commit -m "refactor: add shared KIS automation runner"
```

---

### Task 3: Move market-specific KIS behavior into adapters and hook objects

**Files:**
- Create: `app/jobs/kis_market_adapters.py`
- Modify: `app/jobs/kis_automation_runner.py`
- Modify: `app/jobs/kis_trading.py`
- Modify: `tests/test_kis_tasks.py`
- Test: `tests/test_kis_tasks.py`

**Step 1: Write the failing test**

Add regressions that prove domestic and overseas differences are driven by adapter hooks, not duplicated loops.

```python
from app.jobs.kis_market_adapters import DomesticAutomationAdapter, OverseasAutomationAdapter


def test_domestic_adapter_refreshes_holdings_after_sell_cancel() -> None:
    adapter = DomesticAutomationAdapter()
    assert adapter.refresh_holdings_after_sell_cancel is True


def test_overseas_adapter_keeps_current_sell_cancel_behavior() -> None:
    adapter = OverseasAutomationAdapter()
    assert adapter.refresh_holdings_after_sell_cancel is False
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_kis_tasks.py -k "adapter_refreshes_holdings_after_sell_cancel" -q`

Expected: FAIL because `app/jobs/kis_market_adapters.py` does not exist.

**Step 3: Write minimal implementation**

Create adapters that expose only the divergent behavior and let the shared runner own the step order.

```python
@dataclass(frozen=True)
class DomesticAutomationAdapter:
    market: str = "domestic"
    refresh_holdings_after_sell_cancel: bool = True


@dataclass(frozen=True)
class OverseasAutomationAdapter:
    market: str = "overseas"
    refresh_holdings_after_sell_cancel: bool = False
```

Wire `app/jobs/kis_trading.py` to construct the correct adapter and pass it into `run_market_automation(...)`.

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_kis_tasks.py -k "adapter_refreshes_holdings_after_sell_cancel or ord_psbl_qty or pending_orders" -q`

Expected: PASS

**Step 5: Commit**

```bash
git add app/jobs/kis_market_adapters.py app/jobs/kis_automation_runner.py app/jobs/kis_trading.py tests/test_kis_tasks.py
git commit -m "refactor: extract KIS market adapters"
```

---

### Task 4: Collapse KIS task tests into a scenario matrix

**Files:**
- Modify: `tests/_kis_tasks_support.py`
- Modify: `tests/test_kis_tasks.py`
- Test: `tests/test_kis_tasks.py`

**Step 1: Write the failing test**

Replace one repeated notification branch with a parametrized matrix to prove the helper surface is sufficient.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "outcome", "notifier_method"),
    [
        ("domestic", "buy_success", "notify_buy_order"),
        ("domestic", "sell_success", "notify_sell_order"),
        ("overseas", "buy_failure", "notify_trade_failure"),
    ],
)
async def test_notification_matrix(monkeypatch, market, outcome, notifier_method):
    scenario = build_notification_scenario(market=market, outcome=outcome)
    result = await scenario.call()
    assert scenario.notifier.calls[-1]["method"] == notifier_method
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_kis_tasks.py -k "notification_matrix" -q`

Expected: FAIL because the shared matrix helpers are not implemented yet.

**Step 3: Write minimal implementation**

Extend `tests/_kis_tasks_support.py` with matrix builders and fold the duplicated inline stubs into reusable helpers.

```python
def build_notification_scenario(*, market: str, outcome: str) -> NotificationScenario:
    notifier = RecordingNotifier()
    return NotificationScenario(
        notifier=notifier,
        call=lambda: invoke_automation_case(market=market, outcome=outcome, notifier=notifier),
    )
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_kis_tasks.py -k "notification_matrix or telegram or pending_orders" -q`

Expected: PASS

**Step 5: Commit**

```bash
git add tests/_kis_tasks_support.py tests/test_kis_tasks.py
git commit -m "refactor: matrix KIS task scenarios"
```

---

### Task 5: Replace mutable recommend request state with an immutable context and shared allocator

**Files:**
- Create: `app/mcp_server/tooling/analysis_recommend_types.py`
- Modify: `app/mcp_server/tooling/analysis_recommend.py`
- Modify: `tests/test_mcp_recommend_flow.py`
- Modify: `tests/test_mcp_recommend_scoring.py`
- Test: `tests/test_mcp_recommend_flow.py`
- Test: `tests/test_mcp_recommend_scoring.py`

**Step 1: Write the failing tests**

Add one seam test for the context object and one for the unified allocator.

```python
from app.mcp_server.tooling.analysis_recommend_types import RecommendRequestContext


def test_prepare_recommend_request_returns_context_object() -> None:
    ctx = analysis_recommend._prepare_recommend_request(
        budget=1_000.0,
        market="kr",
        strategy="balanced",
        exclude_symbols=None,
        sectors=None,
        max_positions=3,
    )
    assert isinstance(ctx, RecommendRequestContext)


def test_allocator_supports_equal_and_weighted_modes() -> None:
    result_equal = analysis_recommend.allocate_budget(candidates=[], budget=100.0, strategy="equal")
    result_weighted = analysis_recommend.allocate_budget(candidates=[], budget=100.0, strategy="weighted")
    assert result_equal == result_weighted == ([], 100.0)
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_mcp_recommend_flow.py tests/test_mcp_recommend_scoring.py -k "context_object or weighted_modes" -q`

Expected: FAIL because `_prepare_recommend_request(...)` still returns a mutable dict and the allocator paths are still split.

**Step 3: Write minimal implementation**

Create a frozen context type and collapse duplicate allocation logic into one strategy-aware function.

```python
@dataclass(frozen=True)
class RecommendRequestContext:
    budget: float
    market: str
    strategy: str
    max_positions: int
    warnings: tuple[str, ...] = ()
```

```python
def allocate_budget(*, candidates, budget: float, strategy: str):
    if strategy == "equal":
        return _allocate_budget_common(candidates=candidates, budget=budget, weighted=False)
    return _allocate_budget_common(candidates=candidates, budget=budget, weighted=True)
```

Update `recommend_stocks_impl(...)` to pass the context explicitly through collectors and response building while preserving `warnings`, `diagnostics`, and `fallback_applied` keys.

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_mcp_recommend_flow.py tests/test_mcp_recommend_scoring.py -q`

Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_recommend_types.py app/mcp_server/tooling/analysis_recommend.py tests/test_mcp_recommend_flow.py tests/test_mcp_recommend_scoring.py
git commit -m "refactor: add immutable recommend context"
```

---

### Task 6: Continue the facade-first screening core extraction

**Files:**
- Modify: `app/mcp_server/tooling/analysis_screen_core.py`
- Modify: `app/mcp_server/tooling/analysis_screening.py`
- Create: `app/mcp_server/tooling/analysis_screen_types.py`
- Modify: `tests/test_mcp_screen_stocks_filters_and_rsi.py`
- Modify: `tests/test_mcp_screen_stocks_tvscreener_contract.py`
- Test: `tests/test_mcp_screen_stocks_filters_and_rsi.py`
- Test: `tests/test_mcp_screen_stocks_tvscreener_contract.py`

**Step 1: Write the failing tests**

Add seam tests that prove request normalization and response shaping can be imported from the stable facade while public payloads stay unchanged.

```python
from app.mcp_server.tooling import analysis_screening


def test_analysis_screening_reexports_screen_contract_helpers() -> None:
    assert callable(analysis_screening.normalize_screen_request)
    assert callable(analysis_screening.build_screen_response)
```

```python
@pytest.mark.asyncio
async def test_screen_response_preserves_filters_applied_keys(monkeypatch):
    result = await tools["screen_stocks"](market="us", limit=5)
    assert "filters_applied" in result
    assert "sort_order" in result["filters_applied"]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks_filters_and_rsi.py tests/test_mcp_screen_stocks_tvscreener_contract.py -k "reexports_screen_contract_helpers or preserves_filters_applied_keys" -q`

Expected: FAIL because the helper ownership is still centered in `analysis_screen_core.py`.

**Step 3: Write minimal implementation**

Extract pure types and re-export the stable helpers through `analysis_screening.py` without changing `screen_stocks_unified(...)`.

```python
class ScreenFilters(TypedDict, total=False):
    market: str
    asset_type: str | None
    sort_by: str
    sort_order: str
```

```python
normalize_screen_request = analysis_screen_core.normalize_screen_request
build_screen_response = analysis_screen_core._build_screen_response
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks_filters_and_rsi.py tests/test_mcp_screen_stocks_tvscreener_contract.py -q`

Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_screen_types.py app/mcp_server/tooling/analysis_screen_core.py app/mcp_server/tooling/analysis_screening.py tests/test_mcp_screen_stocks_filters_and_rsi.py tests/test_mcp_screen_stocks_tvscreener_contract.py
git commit -m "refactor: continue facade-first screen helper extraction"
```

---

### Task 7: Add analyzer and prompt characterization coverage

**Files:**
- Create: `tests/test_analysis_prompt.py`
- Create: `tests/test_analysis_analyzer.py`
- Modify: `tests/test_analysis.py`
- Test: `tests/test_analysis_prompt.py`
- Test: `tests/test_analysis_analyzer.py`

**Step 1: Write the failing tests**

Add one prompt snapshot-style regression and one analyzer orchestration regression.

```python
def test_build_prompt_snapshot_kr_equity(sample_kr_df, snapshot):
    prompt = build_prompt(sample_kr_df, "005930", "Samsung", "KRW", "shares")
    snapshot.assert_match(prompt, "build_prompt_kr_equity.txt")
```

```python
@pytest.mark.asyncio
async def test_analyzer_calls_prompt_model_and_repository(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("app.analysis.analyzer.build_prompt", lambda *a, **k: calls.append("prompt") or "PROMPT")
    monkeypatch.setattr("app.analysis.analyzer.Analyzer._generate_with_smart_retry", AsyncMock(return_value=("TEXT", "gemini")))
    monkeypatch.setattr("app.analysis.analyzer.Analyzer._save_to_db", AsyncMock(side_effect=lambda *a, **k: calls.append("save")))

    analyzer = Analyzer(api_key="test")
    await analyzer.analyze_and_save(sample_df, "005930", "Samsung")

    assert calls == ["prompt", "save"]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_analysis_prompt.py tests/test_analysis_analyzer.py -q`

Expected: FAIL because the new test modules and snapshots do not exist.

**Step 3: Write minimal implementation**

Create the new tests and snapshot fixtures without changing production code yet.

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_analysis_prompt.py tests/test_analysis_analyzer.py -q`

Expected: PASS

**Step 5: Commit**

```bash
git add tests/test_analysis_prompt.py tests/test_analysis_analyzer.py tests/test_analysis.py
git commit -m "test: add analysis characterization coverage"
```

---

### Task 8: Split analyzer and prompt into composer, executor, validator, and repository

**Files:**
- Create: `app/analysis/prompt_builder.py`
- Create: `app/analysis/model_executor.py`
- Create: `app/analysis/response_validator.py`
- Create: `app/analysis/analysis_repository.py`
- Modify: `app/analysis/analyzer.py`
- Modify: `app/analysis/prompt.py`
- Modify: `tests/test_analysis_prompt.py`
- Modify: `tests/test_analysis_analyzer.py`
- Test: `tests/test_analysis_prompt.py`
- Test: `tests/test_analysis_analyzer.py`

**Step 1: Write the failing tests**

Add seam tests that prove the analyzer delegates to the new collaborators.

```python
from app.analysis import prompt_builder, model_executor, analysis_repository


@pytest.mark.asyncio
async def test_analyzer_uses_collaborators(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(prompt_builder.PromptBuilder, "build_text_prompt", lambda *a, **k: called.append("prompt") or "PROMPT")
    monkeypatch.setattr(model_executor.ModelExecutor, "execute", AsyncMock(return_value=("TEXT", "gemini")))
    monkeypatch.setattr(analysis_repository.AnalysisRepository, "save_text_analysis", AsyncMock(side_effect=lambda *a, **k: called.append("save")))

    analyzer = Analyzer(api_key="test")
    await analyzer.analyze_and_save(sample_df, "005930", "Samsung")

    assert called == ["prompt", "save"]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_analysis_prompt.py tests/test_analysis_analyzer.py -k "uses_collaborators" -q`

Expected: FAIL because the collaborator modules do not exist and `Analyzer` still owns the whole pipeline.

**Step 3: Write minimal implementation**

Create the new collaborators and make `Analyzer` orchestrate them.

```python
class PromptBuilder:
    def build_text_prompt(self, df, symbol, name, **kwargs) -> str:
        return build_prompt(df, symbol, name, **kwargs)
```

```python
class ModelExecutor:
    async def execute(self, prompt: str, *, use_json: bool):
        return await self._generate_with_retry(prompt=prompt, use_json=use_json)
```

```python
class AnalysisRepository:
    async def save_text_analysis(self, prompt: str, result: str, metadata: dict[str, Any]) -> None:
        ...
```

Refactor `Analyzer` so it wires these collaborators together without changing the public `analyze_and_save(...)` behavior.

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_analysis_prompt.py tests/test_analysis_analyzer.py tests/test_analysis.py -q`

Expected: PASS

**Step 5: Commit**

```bash
git add app/analysis/prompt_builder.py app/analysis/model_executor.py app/analysis/response_validator.py app/analysis/analysis_repository.py app/analysis/analyzer.py app/analysis/prompt.py tests/test_analysis_prompt.py tests/test_analysis_analyzer.py
git commit -m "refactor: split analysis pipeline collaborators"
```

---

### Final verification

**Step 1: Run targeted regression suites after each wave**

Run:

```bash
uv run pytest --no-cov tests/test_kis_tasks.py -q
uv run pytest --no-cov tests/test_mcp_recommend_flow.py tests/test_mcp_recommend_scoring.py -q
uv run pytest --no-cov tests/test_mcp_screen_stocks_*.py tests/test_mcp_recommend_*.py -q
uv run pytest --no-cov tests/test_analysis.py tests/test_analysis_prompt.py tests/test_analysis_analyzer.py -q
```

Expected: PASS

**Step 2: Run lint and type checks**

Run:

```bash
make lint
uv run ty check app tests
```

Expected: PASS

**Step 3: Commit the final cleanup if needed**

```bash
git add app tests docs/plans
git commit -m "refactor: complete prioritized refactor program"
```
