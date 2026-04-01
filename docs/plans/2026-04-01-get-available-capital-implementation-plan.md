# Get Available Capital Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a generic `user_settings` persistence layer plus MCP tools to store user settings and calculate consolidated available capital across KIS, Upbit, and manual cash.

**Architecture:** Add a new `user_settings` table and ORM model keyed by `(user_id, key)` with JSONB values so `manual_cash` and later risk/profile settings can share the same storage path. Keep settings CRUD in a dedicated MCP tooling module and registration file, and keep capital aggregation in the portfolio group by extending `app/mcp_server/tooling/portfolio_cash.py` to compose existing cash-balance logic, exchange-rate conversion, and the new settings helper. Tests should stay focused: unit-test tool behavior by patching session factories and broker/exchange-rate dependencies, then add one lightweight integration test for the new table shape if a migrated database is available.

**Tech Stack:** Python 3.13, SQLAlchemy async ORM, PostgreSQL JSONB, Alembic, FastMCP, pytest

**Implementation assumptions:**
- `manual_cash` is stored as a JSON object and the first supported shape is `{"amount": <number>}`; extra JSON fields are preserved untouched.
- `account="toss"` is treated as the current manual-cash path, because Toss cash is not API-backed yet. The tool should document that this is a temporary alias for manually managed cash.
- When `manual_cash` is absent, malformed, or lacks a numeric `amount`, the tool should not fail; it should treat the manual amount as `0` and add a warning/error entry only if the payload is malformed.
- `stale_warning` is `True` when `manual_cash.updated_at < now_kst() - 3 days`.

---

### Task 1: Add failing tests for `user_settings` MCP tooling

**Files:**
- Create: `tests/test_mcp_user_settings_tools.py`
- Read: `tests/test_mcp_trade_profile_tools.py`
- Read: `tests/_mcp_tooling_support.py`
- Read: `app/mcp_server/tooling/trade_profile_tools.py`

**Step 1: Write the failing test**

Add tool-level tests for:

```python
async def test_get_user_setting_returns_none_for_missing_key() -> None: ...
async def test_set_user_setting_upserts_and_serializes_updated_at() -> None: ...
async def test_get_user_setting_returns_json_value() -> None: ...
def test_user_settings_tool_names_are_registered() -> None: ...
```

Use the same `_build_session_cm` / patched `_session_factory()` pattern used by `tests/test_mcp_trade_profile_tools.py`. The upsert test should assert that the handler returns:

```python
{
    "key": "manual_cash",
    "value": {"amount": 15000000},
    "updated_at": "2026-04-01T08:00:00+09:00",
}
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_user_settings_tools.py -q`
Expected: FAIL because `app/mcp_server/tooling/user_settings_tools.py` and `user_settings_registration.py` do not exist yet.

### Task 2: Add the `user_settings` model and Alembic migration

**Files:**
- Create: `app/models/user_settings.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/<new_revision>_add_user_settings_table.py`
- Read: `app/models/trade_profile.py`
- Read: `alembic/versions/a69eac660fba_add_symbol_trade_settings_table.py`

**Step 1: Write minimal implementation**

Create `app/models/user_settings.py` with a single ORM model:

```python
class UserSetting(Base):
    __tablename__ = "user_settings"
    __table_args__ = (
        Index("ix_user_settings_user_id", "user_id"),
        Index("uq_user_settings_user_key", "user_id", "key", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[Any] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
```

Export `UserSetting` from `app/models/__init__.py`.

Create an Alembic revision that matches the requested schema exactly:

```sql
CREATE TABLE user_settings (
  id BIGSERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(user_id, key)
);
CREATE INDEX ix_user_settings_user_id ON user_settings(user_id);
```

Prefer explicit SQL in the migration to avoid JSONB/default drift between autogenerate and the requested DDL.

**Step 2: Run migration smoke check**

Run: `uv run alembic upgrade head`
Expected: PASS and the new `user_settings` table exists.

**Step 3: Add a lightweight integration test**

Create `tests/models/test_user_settings.py` with:

```python
async def test_user_settings_unique_per_user_and_key() -> None: ...
async def test_user_settings_cascades_on_user_delete() -> None: ...
```

Follow the guarded integration-test pattern from `tests/models/test_trade_profile.py` so the test skips cleanly when DB/migrations are unavailable.

**Step 4: Run the model test**

Run: `uv run pytest tests/models/test_user_settings.py -q`
Expected: PASS on a migrated local database, or SKIP when the DB is not available.

### Task 3: Implement `set_user_setting` / `get_user_setting` handlers and registration

**Files:**
- Create: `app/mcp_server/tooling/user_settings_tools.py`
- Create: `app/mcp_server/tooling/user_settings_registration.py`
- Modify: `app/mcp_server/tooling/registry.py`
- Read: `app/mcp_server/tooling/trade_profile_registration.py`
- Test: `tests/test_mcp_user_settings_tools.py`

**Step 1: Write minimal implementation**

Create `user_settings_tools.py` with:

```python
def _session_factory() -> async_sessionmaker[AsyncSession]: ...
def _serialize_setting(row: UserSetting) -> dict[str, Any]: ...
async def _get_setting_row(key: str) -> UserSetting | None: ...
async def get_user_setting(key: str) -> Any | None: ...
async def set_user_setting(key: str, value: Any) -> dict[str, Any]: ...
async def get_manual_cash_setting() -> dict[str, Any] | None: ...
```

Implementation details:
- Use `MCP_USER_ID` / default user `1`.
- Validate that `key` is non-empty after trimming.
- Use PostgreSQL `insert(...).on_conflict_do_update(...)` for upsert.
- Set `updated_at=func.now()` on conflict update.
- Return only the requested contract for `get_user_setting`: the JSON value or `None`.
- Keep `get_manual_cash_setting()` as a reusable helper for `get_available_capital`.

Create `user_settings_registration.py` with:

```python
USER_SETTINGS_TOOL_NAMES = {"get_user_setting", "set_user_setting"}
```

Register both tools with stable descriptions, then import/register the module from `app/mcp_server/tooling/registry.py`.

**Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_user_settings_tools.py -q`
Expected: PASS

### Task 4: Add failing tests for `get_available_capital`

**Files:**
- Create: `tests/test_mcp_available_capital.py`
- Read: `tests/test_mcp_portfolio_tools.py`
- Read: `app/mcp_server/tooling/portfolio_cash.py`
- Read: `app/services/exchange_rate_service.py`

**Step 1: Write the failing test**

Add focused tests for:

```python
async def test_get_available_capital_aggregates_accounts_and_manual_cash(monkeypatch): ...
async def test_get_available_capital_excludes_manual_when_flag_disabled(monkeypatch): ...
async def test_get_available_capital_handles_missing_manual_cash(monkeypatch): ...
async def test_get_available_capital_marks_stale_manual_cash(monkeypatch): ...
async def test_get_available_capital_toss_filter_uses_manual_cash_path(monkeypatch): ...
```

Patch:
- `portfolio_cash.get_cash_balance_impl`
- `portfolio_cash.get_usd_krw_rate`
- `portfolio_cash.get_manual_cash_setting`
- `portfolio_cash.now_kst`

Assert the response contract includes:

```python
{
    "accounts": [...],
    "manual_cash": {...} | None,
    "summary": {
        "total_orderable_krw": ...,
        "exchange_rate_usd_krw": ...,
        "as_of": "...",
    },
    "errors": [],
}
```

The aggregate test should confirm USD `orderable` is converted with `krw_equivalent = orderable * rate`, not `balance * rate`.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_available_capital.py -q`
Expected: FAIL because `get_available_capital` is not registered and `get_available_capital_impl` does not exist yet.

### Task 5: Implement `get_available_capital` in the portfolio tool group

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_cash.py`
- Modify: `app/mcp_server/tooling/portfolio_holdings.py`
- Modify: `app/mcp_server/README.md`
- Test: `tests/test_mcp_available_capital.py`
- Read: `app/core/timezone.py`

**Step 1: Write minimal implementation**

Add `get_available_capital_impl()` to `app/mcp_server/tooling/portfolio_cash.py`:

```python
async def get_available_capital_impl(
    account: str | None = None,
    include_manual: bool = True,
) -> dict[str, Any]:
    ...
```

Implementation outline:
- Reuse `normalize_account_filter()` to interpret `account`.
- Call `get_cash_balance_impl(account=...)` for broker-backed accounts, except the temporary `toss` alias path.
- Fetch `manual_cash` via `get_manual_cash_setting()` only when `include_manual=True`.
- Read `manual_cash["amount"]` as numeric KRW, defaulting to `0.0` if missing.
- Use `get_usd_krw_rate()` only when at least one USD account is present.
- Add `krw_equivalent` to USD accounts using `orderable * rate`.
- Sum KRW `orderable`, USD KRW-equivalent, and manual cash into `summary.total_orderable_krw`.
- Set `summary.as_of = now_kst().isoformat()`.
- Emit `manual_cash.stale_warning` when older than 3 days.
- Preserve/append partial failures to the `errors` list rather than crashing in multi-account mode.

Register the new tool in `app/mcp_server/tooling/portfolio_holdings.py`:

```python
@mcp.tool(
    name="get_available_capital",
    description=(
        "Query orderable capital across KIS, Upbit, and manual cash. "
        "Converts USD orderable cash to KRW and can optionally exclude manual cash."
    ),
)
async def get_available_capital(
    account: str | None = None,
    include_manual: bool = True,
) -> dict[str, Any]:
    return await _get_available_capital_impl(account=account, include_manual=include_manual)
```

Also update:
- `PORTFOLIO_TOOL_NAMES` to include `"get_available_capital"`
- imports at the top of `portfolio_holdings.py`
- `app/mcp_server/README.md` with new specs for `set_user_setting`, `get_user_setting`, and `get_available_capital`

**Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_available_capital.py -q`
Expected: PASS

### Task 6: Verify the full slice and prepare the commit

**Files:**
- Verify only

**Step 1: Run targeted MCP tests**

Run: `uv run pytest tests/test_mcp_user_settings_tools.py tests/test_mcp_available_capital.py tests/test_mcp_portfolio_tools.py -q`
Expected: PASS

**Step 2: Run the integration model test**

Run: `uv run pytest tests/models/test_user_settings.py -q`
Expected: PASS or SKIP if DB/migrations are unavailable.

**Step 3: Run lint/type checks for touched files**

Run: `make lint`
Expected: PASS

Run: `uv run ty check app/mcp_server/tooling app/models -q`
Expected: PASS

**Step 4: Run a migration sanity check on a clean DB if available**

Run: `uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: PASS without leftover enum/default drift.

**Step 5: Commit**

```bash
git add app/models/user_settings.py app/models/__init__.py app/mcp_server/tooling/user_settings_tools.py app/mcp_server/tooling/user_settings_registration.py app/mcp_server/tooling/registry.py app/mcp_server/tooling/portfolio_cash.py app/mcp_server/tooling/portfolio_holdings.py app/mcp_server/README.md alembic/versions/<new_revision>_add_user_settings_table.py tests/test_mcp_user_settings_tools.py tests/test_mcp_available_capital.py tests/models/test_user_settings.py docs/plans/2026-04-01-get-available-capital-implementation-plan.md
git commit -m "feat: add get_available_capital with user_settings support"
```

Alternative acceptable commit split:
- `feat: add user_settings table and model`
- `feat: add set/get_user_setting MCP tools`
- `feat: add get_available_capital MCP tool`
