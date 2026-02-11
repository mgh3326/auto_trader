# DCA Testing and Implementation Plan

## Current Status

### Completed Tasks ✅
1. ✅ Clean up duplicate `TestGetDcaStatus` class - renamed second one to `TestCreateDcaPlan`
2. ✅ Add `@pytest.mark.asyncio` decorator to `TestCreateDcaPlan` class
3. ✅ Add missing imports (`DcaPlan`, `DcaPlanStep`, `DcaPlanStatus`, `DcaStepStatus`, `DcaService`, `MagicMock`)
4. ✅ Validate migration enum/default/index/FK match with models
5. ✅ Fix migration downgrade - add enum type cleanup for Postgres

### Pending Tasks ⏳
6. ⏳ Implement test: create_dca_plan(dry_run=True) saves to DB and returns plan_id
7. ⏳ Implement test: create_dca_plan(dry_run=False/execute_steps) updates ordered/order_id on success
8. ⏳ Implement test: get_dca_status(plan_id) returns single plan details with progress
9. ⏳ Implement test: get_dca_status(symbol, status) filter combinations work correctly
10. ⏳ Add existing create_dca_plan regression tests (strategy/validation)

## Implementation Analysis

### create_dca_plan Function (from tools.py:6272)

**Signature:**
```python
async def create_dca_plan(
    symbol: str,
    total_amount: float,
    splits: int = 3,
    strategy: str = "support",
    dry_run: bool = True,
    market: str | None = None,
    execute_steps: list[int] | None = None,
) -> dict[str, Any]
```

**Behavior:**
1. **Always** saves plan to DB (line 6366): `plan = await dca_service.create_plan(...)`
2. **Always** returns `plan_id` (line 6404): `plan_id = plan.id`
3. **Only executes orders** when `should_execute = not dry_run or (execute_steps is not None)` (line 6467)
4. Returns execution results when orders are executed (line 6495)

**Response Structure (dry_run=True):**
```python
{
    "success": True,
    "symbol": "KRW-BTC",
    "dry_run": True,
    "plans": [
        {"step": 1, "price": 100000, "amount": 33333, "quantity": 0.333, ...},
        ...
    ],
    "summary": {...},
    "plan_id": 123  # ← Always returned
}
```

**Response Structure (dry_run=False with execution):**
```python
{
    "success": True,
    "symbol": "KRW-BTC",
    "dry_run": False,
    "plans": [...],
    "summary": {...},
    "plan_id": 123,
    "execution_results": [  # ← Only when orders executed
        {"step": 1, "success": True, "order_id": "uuid-1", ...},
        {"step": 2, "success": True, "order_id": "uuid-2", ...},
    ],
    "executed_steps": [1, 2]  # ← Steps that were executed
}
```

### get_dca_status Function (from tools.py:6716)

**Signature:**
```python
async def get_dca_status(
    plan_id: int | None = None,
    symbol: str | None = None,
    status: str = "active",
    limit: int = 10,
) -> dict[str, Any]
```

**Priority Order:**
1. `plan_id` - Query specific plan (highest)
2. `symbol + status` - Query by filter
3. `status` only - Query by status

**Response Structure:**
```python
{
    "success": True,
    "plans": [
        {
            "plan_id": 123,
            "symbol": "KRW-BTC",
            "market": "crypto",
            "status": "active",
            "splits": 3,
            "total_amount": 100000,
            "strategy": "support",
            "created_at": "2024-01-01T00:00:00Z",
            "progress": {
                "total_steps": 3,
                "pending": 1,
                "ordered": 1,
                "filled": 1,
                "cancelled": 0,
                "skipped": 0,
                "completion_pct": 33.33,
            },
            "steps": [
                {"step_number": 1, "status": "filled", "price": 100000, ...},
                {"step_number": 2, "status": "ordered", "price": 95000, "order_id": "uuid-1", ...},
                {"step_number": 3, "status": "pending", "price": 90000, ...},
            ]
        }
    ]
}
```

## Test Implementation Plan

### Test 6: create_dca_plan(dry_run=True) Saves to DB

**Test Pattern:**
```python
@pytest.mark.asyncio
async def test_create_dca_plan_dry_run_saves_to_db(self, monkeypatch):
    """Test that create_dca_plan(dry_run=True) saves plan to DB and returns plan_id."""
    tools = build_tools()

    # Mock DcaService.create_plan to verify it's called and returns plan_id
    create_plan_called = []

    async def mock_create_plan(*args, **kwargs):
        create_plan_called.append((args, kwargs))
        # Return a mock plan with id
        from app.models.dca_plan import DcaPlan
        return DcaPlan(
            id=123,  # ← Should be returned in response
            user_id=1,
            symbol="KRW-BTC",
            market="crypto",
            status=DcaPlanStatus.ACTIVE,
            splits=3,
            total_amount=100000,
            strategy="support",
        )

    monkeypatch.setattr(DcaService, "create_plan", mock_create_plan)

    # Mock support/resistance and indicators (always needed)
    # [mock setup...]

    result = await tools["create_dca_plan"](
        symbol="KRW-BTC",
        total_amount=100000,
        splits=3,
        strategy="support",
        dry_run=True,
    )

    # Verify
    assert result["success"] is True
    assert result["dry_run"] is True
    assert "plan_id" in result  # ← Verify plan_id is returned
    assert result["plan_id"] == 123
    assert len(create_plan_called) == 1  # ← Verify DB save happened
    assert "execution_results" not in result  # dry_run has no execution
```

### Test 7: create_dca_plan(dry_run=False/execute_steps) Updates ordered/order_id

**Test Pattern:**
```python
@pytest.mark.asyncio
async def test_create_dca_plan_dry_run_false_updates_ordered(self, monkeypatch):
    """Test that create_dca_plan(dry_run=False) updates ordered/order_id on success."""
    tools = build_tools()

    # Mock DcaService.create_plan
    async def mock_create_plan(*args, **kwargs):
        from app.models.dca_plan import DcaPlan, DcaPlanStep
        return DcaPlan(
            id=123,
            # ... other fields ...
            steps=[],
        )

    # Mock _place_order_impl to track calls and return order_id
    order_calls = []

    async def mock_place_order(*args, **kwargs):
        order_calls.append({"args": args, "kwargs": kwargs})
        return {"success": True, "dry_run": False, "order_id": f"uuid-{len(order_calls)}"}

    monkeypatch.setattr(DcaService, "create_plan", mock_create_plan)
    monkeypatch.setattr(mcp_tools, "_place_order_impl", mock_place_order)

    # [mock support/resistance and indicators...]

    result = await tools["create_dca_plan"](
        symbol="KRW-BTC",
        total_amount=100000,
        splits=3,
        strategy="support",
        dry_run=False,  # ← This triggers execution
    )

    # Verify
    assert result["success"] is True
    assert result["dry_run"] is False
    assert "plan_id" in result
    assert "execution_results" in result  # ← Only when orders executed
    assert len(result["execution_results"]) == 3
    # Verify each result has order_id for successful orders
    for i, exec_result in enumerate(result["execution_results"], 1):
        assert exec_result["step"] == i
        assert exec_result["success"] is True
        assert "order_id" in exec_result  # ← Verify order_id is reflected
        assert exec_result["order_id"] == f"uuid-{i}"
```

### Test 8: get_dca_status(plan_id) Returns Single Plan Details

**Test Pattern:**
```python
@pytest.mark.asyncio
async def test_get_dca_status_by_plan_id_returns_details(self, monkeypatch):
    """Test that get_dca_status(plan_id) returns single plan with progress."""
    tools = build_tools()

    # Mock DcaService.get_plan
    from app.models.dca_plan import DcaPlan, DcaPlanStep
    plan = DcaPlan(
        id=123,
        user_id=1,
        symbol="KRW-BTC",
        market="crypto",
        status=DcaPlanStatus.ACTIVE,
        splits=3,
        total_amount=100000,
        strategy="support",
        steps=[
            DcaPlanStep(id=1, step_number=1, status=DcaStepStatus.FILLED, ...),
            DcaPlanStep(id=2, step_number=2, status=DcaStepStatus.ORDERED, ...),
            DcaPlanStep(id=3, step_number=3, status=DcaStepStatus.PENDING, ...),
        ],
    )

    async def mock_get_plan(plan_id, user_id):
        if plan_id == 123:
            return plan
        return None

    monkeypatch.setattr(DcaService, "get_plan", mock_get_plan)

    result = await tools["get_dca_status"](plan_id=123)

    # Verify
    assert result["success"] is True
    assert len(result["plans"]) == 1  # ← Single plan
    plan_data = result["plans"][0]
    assert plan_data["plan_id"] == 123
    assert plan_data["symbol"] == "KRW-BTC"
    assert plan_data["status"] == "active"
    # ← Verify progress is calculated
    assert "progress" in plan_data
    assert plan_data["progress"]["total_steps"] == 3
    assert plan_data["progress"]["filled"] == 1
    assert plan_data["progress"]["ordered"] == 1
    assert plan_data["progress"]["pending"] == 1
    assert plan_data["progress"]["completion_pct"] == 33.33
```

### Test 9: get_dca_status(symbol, status) Filter Combinations

**Test Patterns:**
```python
@pytest.mark.asyncio
async def test_get_dca_status_filter_symbol_and_status(self, monkeypatch):
    """Test get_dca_status(symbol=..., status=...) filter works."""
    tools = build_tools()

    # Mock DcaService.get_plans_by_status
    plan1 = DcaPlan(id=1, symbol="KRW-BTC", status=DcaPlanStatus.ACTIVE, steps=[])
    plan2 = DcaPlan(id=2, symbol="KRW-BTC", status=DcaPlanStatus.COMPLETED, steps=[])

    async def mock_get_plans(user_id, status, symbol, limit):
        plans = []
        if status is None or status == "active":
            plans.extend([plan1])
        if status is None or status == "completed":
            plans.extend([plan2])
        return plans

    monkeypatch.setattr(DcaService, "get_plans_by_status", mock_get_plans)

    result = await tools["get_dca_status"](symbol="KRW-BTC", status="active")

    assert result["success"] is True
    assert len(result["plans"]) == 1
    assert result["plans"][0]["symbol"] == "KRW-BTC"
    assert result["plans"][0]["status"] == "active"
```

## Test Mocking Pattern Guidelines

### AsyncSessionLocal Mocking

**Current Issue:** Tests use `lambda: mock_db()` to patch `AsyncSessionLocal`, but this doesn't properly simulate the async context manager protocol.

**Correct Pattern:** Patch `DcaService` methods directly instead of mocking `AsyncSessionLocal`. This is simpler and more reliable:

```python
# ❌ Wrong
async def mock_db():
    db = AsyncMock()
    # ... setup db ...
    return db
monkeypatch.setattr(mcp_tools, "AsyncSessionLocal", lambda: mock_db())

# ✅ Correct - patch service methods directly
async def mock_get_plan(plan_id, user_id):
    return mock_plan_object

monkeypatch.setattr(DcaService, "get_plan", mock_get_plan)
```

### Removing Duplicate Imports

Tests currently import `DcaService`, `DcaPlan`, etc. inside test methods (lines 671-672). Since these are now imported at the top of the file, these local imports can be removed.

## Remaining Work

1. Remove duplicate imports from `TestGetDcaStatus` test methods
2. Implement Tests 6-10 with proper mocking patterns
3. Verify all tests pass with `pytest tests/test_mcp_server_tools.py::TestGetDcaStatus -v`
4. Verify all tests pass with `pytest tests/test_mcp_server_tools.py::TestCreateDcaPlan -v`
