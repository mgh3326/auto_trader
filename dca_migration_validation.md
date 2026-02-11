# DCA Migration Validation Report

## Comparison: Migration vs Models

### dca_plans table

| Field | Migration | Model | Match | Issue |
|-------|-----------|-------|-------|-------|
| id | BigInteger, PK | BigInteger, PK | ✅ | |
| user_id | BigInteger FK CASCADE | BigInteger FK CASCADE | ✅ | |
| symbol | Text, nullable=False | Text, nullable=False, index=True | ✅ | Model has extra index |
| market | String(50), nullable=False | String(50), nullable=False | ✅ | |
| total_amount | Numeric(18, 2), nullable=False | Numeric(18, 2), nullable=False | ✅ | |
| splits | BigInteger, nullable=False | BigInteger, nullable=False | ✅ | |
| strategy | String(50), nullable=False | String(50), nullable=False | ✅ | |
| status | Enum, default="active" | Enum, default=DcaPlanStatus.ACTIVE | ✅ | |
| created_at | TIMESTAMP, default=now() | TIMESTAMP, default=func.now() | ✅ | |
| updated_at | TIMESTAMP, default=now(), onupdate=now() | TIMESTAMP, default=func.now(), onupdate=func.now() | ✅ | |
| completed_at | TIMESTAMP, nullable=True | TIMESTAMP, nullable=True | ✅ | |
| rsi_14 | Numeric(5, 2), nullable=True | Numeric(5, 2), nullable=True | ✅ | |

**Indexes:**
- `pk_dca_plans` on id: Migration ✅, Model: implicit (PK)
- `fk_dca_plans_user_id_users` FK CASCADE: Migration ✅, Model: implicit (FK)
- `ix_dca_plans_user_status` on (user_id, status): Migration ✅, Model ✅
- `ix_dca_plans_symbol` on symbol: Migration ✅, Model ✅

### dca_plan_steps table

| Field | Migration | Model | Match | Issue |
|-------|-----------|-------|-------|-------|
| id | BigInteger, PK | BigInteger, PK | ✅ | |
| plan_id | BigInteger FK CASCADE | BigInteger FK CASCADE, index=True | ✅ | Model has extra index |
| step_number | BigInteger, nullable=False | BigInteger, nullable=False | ✅ | |
| target_price | Numeric(18, 8), nullable=False | Numeric(18, 8), nullable=False | ✅ | |
| target_amount | Numeric(18, 2), nullable=False | Numeric(18, 2), nullable=False | ✅ | |
| target_quantity | Numeric(18, 8), nullable=False | Numeric(18, 8), nullable=False | ✅ | |
| status | Enum, default="pending" | Enum, default=DcaStepStatus.PENDING | ✅ | |
| filled_price | Numeric(18, 8), nullable=True | Numeric(18, 8), nullable=True | ✅ | |
| filled_quantity | Numeric(18, 8), nullable=True | Numeric(18, 8), nullable=True | ✅ | |
| filled_amount | Numeric(18, 2), nullable=True | Numeric(18, 2), nullable=True | ✅ | |
| order_id | Text, nullable=True | Text, nullable=True, index=True | ✅ | Model has extra index |
| ordered_at | TIMESTAMP, nullable=True | TIMESTAMP, nullable=True | ✅ | |
| filled_at | TIMESTAMP, nullable=True | TIMESTAMP, nullable=True | ✅ | |
| level_source | Text, nullable=True | Text, nullable=True | ✅ | |

**Indexes:**
- `pk_dca_plan_steps` on id: Migration ✅, Model: implicit (PK)
- `fk_dca_plan_steps_plan_id_dca_plans` FK CASCADE: Migration ✅, Model: implicit (FK)
- `ix_dca_plan_steps_plan_id` on plan_id: Migration ✅, Model ✅
- `ix_dca_plan_steps_order_id` on order_id: Migration ✅, Model ✅
- `uq_dca_plan_step` on (plan_id, step_number): Migration ✅, Model ✅

## Issues Found

### 1. Enum Type Creation
**Migration:** Creates enum types with `sa.Enum(..., name="dca_plan_status")`
**Model:** Uses `create_type=False` in `Enum(..., create_type=False)`

**Impact:** The model expects enum types to already exist (or be managed separately). The migration creates them inline.

**Status:** ⚠️ POTENTIAL CONFLICT - If `create_type=False` is intentional, the migration should not create enums inline.

### 2. Indexes in Model `__table_args__`
The model defines indexes in `__table_args__` but the migration creates them explicitly with `op.create_index()`. This is standard and acceptable.

### 3. Downgrade - Missing Enum Cleanup

**Current downgrade:**
```python
def downgrade() -> None:
    # Drop indexes
    op.drop_index(...)
    # Drop tables
    op.drop_table("dca_plan_steps")
    op.drop_table("dca_plans")
```

**Issue:** Does not drop enum types (`dca_plan_status`, `dca_step_status`).

**Postgres Impact:** When dropping tables, Postgres will keep the enum types. If the migration is re-run:
1. First upgrade: Creates enum types OK
2. Downgrade: Drops tables only (enums remain)
3. Second upgrade: Tries to create enums that already exist → **CONFLICT**

**Fix:** Add enum type drops in downgrade:
```python
def downgrade() -> None:
    # Drop indexes first
    op.drop_index(...)
    # Drop tables
    op.drop_table("dca_plan_steps")
    op.drop_table("dca_plans")
    # Drop enum types (Postgres specific)
    op.execute('DROP TYPE IF EXISTS dca_step_status')
    op.execute('DROP TYPE IF EXISTS dca_plan_status')
```

## Recommendations

1. **Fix Downgrade**: Add enum type cleanup to prevent Postgres conflicts on re-migration.
2. **Model Enum**: Consider whether `create_type=False` is intentional. If enums should be created by migration, remove `create_type=False` from models.
3. **Test Migration**: Run `alembic upgrade head` → `alembic downgrade base` → `alembic upgrade head` to verify no conflicts.
