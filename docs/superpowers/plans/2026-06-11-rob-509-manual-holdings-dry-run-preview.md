# ROB-509 Manual Holdings Dry-Run Diff Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `update_manual_holdings(dry_run=True)` return actionable diff/count previews for remove and upsert actions, and include item identity in `market_section` validation warnings.

**Architecture:** Keep the public MCP tool wrapper unchanged and fix the behavior in `ScreenshotHoldingsService.resolve_and_update`, where diff and counts are already computed. Dry-run responses should include the same count keys as live responses, while dry-run diff actions use preview verbs (`would_add`, `would_update`, `would_remove`, `unchanged`) so callers can distinguish preview from mutation results.

**Tech Stack:** Python 3.13, FastMCP tool wrapper, SQLAlchemy async session mocks, pytest, pytest-asyncio, uv.

---

## File Structure

- Modify: `app/services/screenshot_holdings_service.py`
  - Add small helper methods for dry-run diff action naming and skipped-item identity.
  - Include symbol/stock name in `market_section` warnings.
  - Count dry-run add/update/remove/unchanged operations.
  - Return count fields and `diff` for both dry-run and live responses.
- Modify: `tests/test_screenshot_holdings_service_resolution.py`
  - Add focused dry-run remove preview test.
  - Add mixed dry-run add/update/remove/unchanged preview test.
  - Update existing dry-run response contract test from old "no diff/count" contract to new preview contract.
  - Assert missing `market_section` warnings include the item identity.
- Modify: `app/mcp_server/README.md`
  - Document dry-run count/diff behavior and preview action names.

## Task 1: Add Failing Service Tests

**Files:**
- Modify: `tests/test_screenshot_holdings_service_resolution.py`

- [ ] **Step 1: Add a dry-run remove preview test**

Add this test after `test_dry_run_response_contract` or near the existing dry-run tests:

```python
@pytest.mark.asyncio
async def test_dry_run_remove_returns_preview_diff_and_counts(
    service, mock_db, mock_broker_account, monkeypatch
):
    existing_ionq = MagicMock()
    existing_ionq.id = 101
    existing_ionq.ticker = "IONQ"
    existing_ionq.market_type = MagicMock()
    existing_ionq.market_type.value = "US"
    existing_ionq.quantity = Decimal("3")
    existing_ionq.avg_price = Decimal("42")

    existing_tsm = MagicMock()
    existing_tsm.id = 102
    existing_tsm.ticker = "TSM"
    existing_tsm.market_type = MagicMock()
    existing_tsm.market_type.value = "US"
    existing_tsm.quantity = Decimal("2")
    existing_tsm.avg_price = Decimal("180")

    _setup_mocks(
        monkeypatch,
        mock_db,
        mock_broker_account,
        existing_holdings=[existing_ionq, existing_tsm],
    )

    delete_calls = []

    async def mock_delete_holding(self, holding_id):
        delete_calls.append(holding_id)
        return True

    monkeypatch.setattr(
        "app.services.screenshot_holdings_service.ManualHoldingsService.delete_holding",
        mock_delete_holding,
    )

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {"symbol": "IONQ", "market_section": "us", "action": "remove"},
            {"symbol": "TSM", "market_section": "us", "action": "remove"},
        ],
        broker="toss",
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["parsed_count"] == 0
    assert result["holdings"] == []
    assert result["added_count"] == 0
    assert result["updated_count"] == 0
    assert result["removed_count"] == 2
    assert result["unchanged_count"] == 0
    assert result["diff"] == [
        {"action": "would_remove", "ticker": "IONQ", "market_type": "US"},
        {"action": "would_remove", "ticker": "TSM", "market_type": "US"},
    ]
    assert delete_calls == []
```

- [ ] **Step 2: Add a mixed dry-run preview test**

Add this test in the same file:

```python
@pytest.mark.asyncio
async def test_dry_run_mixed_upsert_remove_returns_preview_diff_and_counts(
    service, mock_db, mock_broker_account, monkeypatch
):
    existing_remove = MagicMock()
    existing_remove.id = 201
    existing_remove.ticker = "IONQ"
    existing_remove.market_type = MagicMock()
    existing_remove.market_type.value = "US"
    existing_remove.quantity = Decimal("3")
    existing_remove.avg_price = Decimal("42")

    existing_update = MagicMock()
    existing_update.id = 202
    existing_update.ticker = "AAPL"
    existing_update.market_type = MagicMock()
    existing_update.market_type.value = "US"
    existing_update.quantity = Decimal("1")
    existing_update.avg_price = Decimal("100")

    existing_unchanged = MagicMock()
    existing_unchanged.id = 203
    existing_unchanged.ticker = "NVDA"
    existing_unchanged.market_type = MagicMock()
    existing_unchanged.market_type.value = "US"
    existing_unchanged.quantity = Decimal("2")
    existing_unchanged.avg_price = Decimal("120")

    _setup_mocks(
        monkeypatch,
        mock_db,
        mock_broker_account,
        existing_holdings=[existing_remove, existing_update, existing_unchanged],
    )

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {"symbol": "IONQ", "market_section": "us", "action": "remove"},
            {
                "symbol": "AAPL",
                "quantity": 2,
                "avg_buy_price": 110,
                "market_section": "us",
            },
            {
                "symbol": "MSFT",
                "quantity": 4,
                "avg_buy_price": 300,
                "market_section": "us",
            },
            {
                "symbol": "NVDA",
                "quantity": 2,
                "avg_buy_price": 120,
                "market_section": "us",
            },
        ],
        broker="toss",
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["parsed_count"] == 3
    assert result["added_count"] == 1
    assert result["updated_count"] == 1
    assert result["removed_count"] == 1
    assert result["unchanged_count"] == 1
    assert result["diff"] == [
        {"action": "would_remove", "ticker": "IONQ", "market_type": "US"},
        {
            "action": "would_update",
            "ticker": "AAPL",
            "market_type": "US",
            "old_quantity": 1.0,
            "new_quantity": 2.0,
            "old_avg_price": 100.0,
            "new_avg_price": 110.0,
        },
        {
            "action": "would_add",
            "ticker": "MSFT",
            "market_type": "US",
            "quantity": 4.0,
            "avg_buy_price": 300.0,
        },
        {"action": "unchanged", "ticker": "NVDA", "market_type": "US"},
    ]
```

- [ ] **Step 3: Update the existing dry-run contract test**

In `test_dry_run_response_contract`, replace the old dry-run assertions:

```python
assert "added_count" not in result_dry
assert "updated_count" not in result_dry
assert "removed_count" not in result_dry
assert "unchanged_count" not in result_dry
assert "diff" not in result_dry
```

with:

```python
assert result_dry["added_count"] == 1
assert result_dry["updated_count"] == 0
assert result_dry["removed_count"] == 0
assert result_dry["unchanged_count"] == 0
assert result_dry["diff"] == [
    {
        "action": "would_add",
        "ticker": "KRW-ETH",
        "market_type": "CRYPTO",
        "quantity": 1.0,
        "avg_buy_price": 3000000.0,
    }
]
```

- [ ] **Step 4: Update the missing market-section warning test**

In `test_market_section_missing_skip_warning`, change the input payload to include a symbol:

```python
holdings_data=[
    {
        "symbol": "ETH",
        "stock_name": "이더리움",
        "quantity": 1,
        "market_section": "",
    }
],
```

Add this assertion after the existing `market_section` assertion:

```python
assert "ETH" in result["warnings"][0]
```

- [ ] **Step 5: Run the new/updated tests and verify they fail for the expected reason**

Run:

```bash
uv run --group test pytest \
  tests/test_screenshot_holdings_service_resolution.py::test_dry_run_response_contract \
  tests/test_screenshot_holdings_service_resolution.py::test_dry_run_remove_returns_preview_diff_and_counts \
  tests/test_screenshot_holdings_service_resolution.py::test_dry_run_mixed_upsert_remove_returns_preview_diff_and_counts \
  tests/test_screenshot_holdings_service_resolution.py::test_market_section_missing_skip_warning \
  -q
```

Expected: FAIL because current dry-run responses do not include `added_count`, `removed_count`, or `diff`, and the warning does not include `ETH`.

## Task 2: Implement Dry-Run Preview Diff and Counts

**Files:**
- Modify: `app/services/screenshot_holdings_service.py`

- [ ] **Step 1: Add helper methods near the existing static helpers**

Add these methods after `_validate_us_avg_price`:

```python
    @staticmethod
    def _preview_diff_action(action: str, dry_run: bool) -> str:
        if not dry_run:
            return action
        return {
            "added": "would_add",
            "updated": "would_update",
            "removed": "would_remove",
        }.get(action, action)

    @staticmethod
    def _holding_identity(
        *, symbol: str, stock_name: str, fallback: str = "<unknown>"
    ) -> str:
        return symbol or stock_name or fallback
```

- [ ] **Step 2: Update the response docstring**

In `resolve_and_update`, replace the existing comment that says count fields are only included when `dry_run=False` with:

```python
                # Count fields and diff are included for both dry_run and live runs.
                # dry_run diff actions use would_add/would_update/would_remove
                # plus unchanged. Live diff actions use added/updated/removed.
                "added_count": int,
                "updated_count": int,
                "removed_count": int,
                "unchanged_count": int,
                "diff": [...],
```

- [ ] **Step 3: Include item identity in market-section warnings**

Replace the warning block at the `market_section not in ("kr", "us", "crypto")` check with:

```python
            if market_section not in ("kr", "us", "crypto"):
                identity = self._holding_identity(
                    symbol=symbol, stock_name=stock_name
                )
                warnings.append(
                    f"Skipping holding {identity}: invalid or missing market_section "
                    f"'{market_section_raw}' (must be kr|us|crypto)"
                )
                continue
```

- [ ] **Step 4: Count and label explicit remove previews**

Replace the `if action == "remove":` body with this implementation:

```python
            if action == "remove":
                if symbol:
                    ticker = symbol
                else:
                    ticker, _, _ = await self._resolve_symbol(
                        stock_name, market_section, broker
                    )

                existing = old_map.get((ticker, market_type.value))
                if existing:
                    if not dry_run:
                        await manual_holdings_service.delete_holding(existing.id)
                    removed_count += 1
                    diff.append(
                        {
                            "action": self._preview_diff_action(
                                "removed", dry_run
                            ),
                            "ticker": ticker,
                            "market_type": market_type.value,
                        }
                    )
                else:
                    warnings.append(
                        f"Cannot remove: {symbol or stock_name} not found in holdings"
                    )
                continue
```

- [ ] **Step 5: Count and label non-positive quantity delete previews**

In the `if quantity <= 0:` block, replace the `existing` branch with:

```python
                if existing:
                    if not dry_run:
                        await manual_holdings_service.delete_holding(existing.id)
                    removed_count += 1
                    diff.append(
                        {
                            "action": self._preview_diff_action(
                                "removed", dry_run
                            ),
                            "ticker": ticker,
                            "market_type": market_type.value,
                        }
                    )
```

- [ ] **Step 6: Count and label live/dry-run updates**

In both update branches, replace literal `"updated"` action values with:

```python
"action": self._preview_diff_action("updated", dry_run),
```

Then ensure `updated_count += 1` runs for both dry-run and live update previews. The dry-run update branch should be:

```python
                    if (
                        abs(old_qty - quantity) > 0.0001
                        or abs(old_avg - avg_buy_price) > 0.01
                    ):
                        updated_count += 1
                        diff.append(
                            {
                                "action": self._preview_diff_action(
                                    "updated", dry_run
                                ),
                                "ticker": ticker,
                                "market_type": market_type.value,
                                "old_quantity": old_qty,
                                "new_quantity": quantity,
                                "old_avg_price": old_avg,
                                "new_avg_price": avg_buy_price,
                            }
                        )
                    else:
                        unchanged_count += 1
                        diff.append(
                            {
                                "action": "unchanged",
                                "ticker": ticker,
                                "market_type": market_type.value,
                            }
                        )
```

- [ ] **Step 7: Count and label live/dry-run additions**

In both add branches, replace literal `"added"` action values with:

```python
"action": self._preview_diff_action("added", dry_run),
```

Then ensure `added_count += 1` runs for both dry-run and live additions. The dry-run add branch should be:

```python
                else:
                    added_count += 1
                    diff.append(
                        {
                            "action": self._preview_diff_action("added", dry_run),
                            "ticker": ticker,
                            "market_type": market_type.value,
                            "quantity": quantity,
                            "avg_buy_price": avg_buy_price,
                        }
                    )
```

- [ ] **Step 8: Always include counts and diff in successful responses**

Replace the final `if not dry_run: result.update(...)` block with unconditional update:

```python
        result.update(
            {
                "added_count": added_count,
                "updated_count": updated_count,
                "removed_count": removed_count,
                "unchanged_count": unchanged_count,
                "diff": diff,
            }
        )
```

- [ ] **Step 9: Run the focused tests**

Run:

```bash
uv run --group test pytest \
  tests/test_screenshot_holdings_service_resolution.py::test_dry_run_response_contract \
  tests/test_screenshot_holdings_service_resolution.py::test_dry_run_remove_returns_preview_diff_and_counts \
  tests/test_screenshot_holdings_service_resolution.py::test_dry_run_mixed_upsert_remove_returns_preview_diff_and_counts \
  tests/test_screenshot_holdings_service_resolution.py::test_market_section_missing_skip_warning \
  -q
```

Expected: PASS.

## Task 3: Preserve Live Behavior with Regression Tests

**Files:**
- Modify: `tests/test_screenshot_holdings_service_resolution.py`

- [ ] **Step 1: Strengthen live remove expectations**

In `test_qty_zero_upsert_deletes_existing_holding`, keep the existing assertion:

```python
assert {"action": "removed", "ticker": "BITX", "market_type": "US"} in result[
    "diff"
]
```

Add this assertion after it:

```python
assert all(item["action"] != "would_remove" for item in result["diff"])
```

- [ ] **Step 2: Add explicit live update action regression**

Add this test near `test_qty_zero_upsert_deletes_existing_holding`:

```python
@pytest.mark.asyncio
async def test_live_update_keeps_mutation_diff_action(
    service, mock_db, mock_broker_account, monkeypatch
):
    existing = MagicMock()
    existing.id = 401
    existing.ticker = "AAPL"
    existing.market_type = MagicMock()
    existing.market_type.value = "US"
    existing.quantity = Decimal("1")
    existing.avg_price = Decimal("100")

    _setup_mocks(
        monkeypatch,
        mock_db,
        mock_broker_account,
        existing_holdings=[existing],
    )

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "symbol": "AAPL",
                "quantity": 2,
                "avg_buy_price": 110,
                "market_section": "us",
            }
        ],
        broker="toss",
        dry_run=False,
    )

    assert result["success"] is True
    assert result["updated_count"] == 1
    assert result["diff"] == [
        {
            "action": "updated",
            "ticker": "AAPL",
            "market_type": "US",
            "old_quantity": 1.0,
            "new_quantity": 2.0,
            "old_avg_price": 100.0,
            "new_avg_price": 110.0,
        }
    ]
```

- [ ] **Step 3: Run live regression tests**

Run:

```bash
uv run --group test pytest \
  tests/test_screenshot_holdings_service_resolution.py::test_qty_zero_upsert_deletes_existing_holding \
  tests/test_screenshot_holdings_service_resolution.py::test_live_update_keeps_mutation_diff_action \
  -q
```

Expected: PASS.

## Task 4: Update MCP Documentation

**Files:**
- Modify: `app/mcp_server/README.md`

- [ ] **Step 1: Update dry-run validation rule text**

Replace:

```markdown
- **dry_run behavior**: When `dry_run=True`, no DB mutations occur; only preview data and warnings are returned.
```

with:

```markdown
- **dry_run behavior**: When `dry_run=True`, no DB mutations occur. The response still includes `added_count`, `updated_count`, `removed_count`, `unchanged_count`, and `diff` so callers can validate the planned changes before execution. Dry-run diff actions are `would_add`, `would_update`, `would_remove`, and `unchanged`; live execution actions remain `added`, `updated`, and `removed`.
```

- [ ] **Step 2: Add a dry-run response example after the existing response format**

Add this block after the live response example:

````markdown
Dry-run remove preview example:
```json
{
  "success": true,
  "dry_run": true,
  "message": "Preview only (set dry_run=False to update DB)",
  "broker": "toss",
  "account_name": "기본 계좌",
  "parsed_count": 0,
  "holdings": [],
  "warnings": [],
  "added_count": 0,
  "updated_count": 0,
  "removed_count": 2,
  "unchanged_count": 0,
  "diff": [
    {"action": "would_remove", "ticker": "IONQ", "market_type": "US"},
    {"action": "would_remove", "ticker": "TSM", "market_type": "US"}
  ]
}
```
````

- [ ] **Step 3: Check Markdown rendering boundaries**

Run:

```bash
rg -n "Dry-run remove preview example|would_remove|added_count" app/mcp_server/README.md
```

Expected: command exits with code 0 and prints the dry-run preview section lines.

## Task 5: Full Focused Verification

**Files:**
- No code changes.

- [ ] **Step 1: Run the complete screenshot holdings service resolution suite**

Run:

```bash
uv run --group test pytest tests/test_screenshot_holdings_service_resolution.py -q
```

Expected: PASS.

- [ ] **Step 2: Run MCP wrapper tests that cover `update_manual_holdings`**

Run:

```bash
uv run --group test pytest tests/test_screenshot_holdings.py -q
```

Expected: PASS.

- [ ] **Step 3: Run lint on touched files**

Run:

```bash
uv run --group dev ruff check \
  app/services/screenshot_holdings_service.py \
  tests/test_screenshot_holdings_service_resolution.py
```

Expected: PASS.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git diff -- \
  app/services/screenshot_holdings_service.py \
  tests/test_screenshot_holdings_service_resolution.py \
  app/mcp_server/README.md
```

Expected:
- Dry-run success responses always include `added_count`, `updated_count`, `removed_count`, `unchanged_count`, and `diff`.
- Dry-run diff actions use `would_add`, `would_update`, `would_remove`, or `unchanged`.
- Live diff actions remain `added`, `updated`, or `removed`.
- Missing/invalid `market_section` warnings include the input symbol or stock name.

- [ ] **Step 5: Commit**

Run:

```bash
git add \
  app/services/screenshot_holdings_service.py \
  tests/test_screenshot_holdings_service_resolution.py \
  app/mcp_server/README.md
git commit -m "fix: preview manual holding removals in dry run"
```

Expected: commit succeeds after tests pass.

## Self-Review

- Spec coverage: ROB-509 acceptance maps to Task 1 and Task 2 for remove dry-run `diff`, Task 1 mixed dry-run test for remove/upsert coverage, Task 2 warning identity, and Task 4 MCP docs.
- Placeholder scan: No TBD/TODO/fill-in steps remain; code snippets and commands are concrete.
- Type consistency: Helper methods are static methods on `ScreenshotHoldingsService`; tests use existing `MagicMock`, `Decimal`, and `_setup_mocks` imports already present in `tests/test_screenshot_holdings_service_resolution.py`.
