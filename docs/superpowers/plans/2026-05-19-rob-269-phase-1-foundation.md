# ROB-269 Phase 1 — Snapshot Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the read/write foundation for ROB-269 snapshot bundles — 4 immutable tables under `review` schema, append-only repository, canonical hashing, freshness classifier — as a self-contained PR (PR 1 of 4). All new code is dead-code in prod until later phases wire it in. No live broker / order / external mutation.

**Architecture:** 4 SQLAlchemy models under `app/models/investment_snapshots.py` backed by one Alembic migration. Repository (`app/services/investment_snapshots/repository.py`) enforces append-only at the service layer (raises on UPDATE/DELETE). Pure-function utilities (`canonicalize.py`, `freshness.py`) are TDD-friendly. Existing domain snapshot tables (Decision 1 of pre-plan) are referenced via a `(source_table, source_id, source_uri)` triple stored on `investment_snapshots`. No generator/UI/scheduler wiring in this phase.

**Tech Stack:** Python 3.13, SQLAlchemy 2.x async, asyncpg, Alembic, Pydantic v2, pytest-asyncio, uv. Postgres `review` schema, JSONB columns, UUID primary keys for external refs.

**Pre-plan reference:** `docs/superpowers/plans/2026-05-19-rob-269-pre-plan.md` (5 decisions locked).

**Safety boundaries (binding, repeated in every relevant task):**
- No live broker/order/watch/live trading side effects. No KIS/Upbit/Alpaca mutation HTTP calls.
- Operational DB writes only via Alembic + service-layer repository — no `psql` direct INSERT/UPDATE/DELETE.
- No `git push`, no PR creation, no deploy.
- Commits stay local.
- If a task hits a judgement call the plan doesn't cover, leave a `# TODO(rob-269 reviewer):` note and stop — do not improvise schema or contracts.

---

## File Structure

**Create:**
- `alembic/versions/20260519_rob269_add_snapshot_foundation.py` — 4-table migration UP+DOWN
- `app/models/investment_snapshots.py` — 4 ORM classes
- `app/schemas/investment_snapshots.py` — Pydantic DTOs (Upsert/Read)
- `app/services/investment_snapshots/__init__.py`
- `app/services/investment_snapshots/repository.py` — append-only DAO
- `app/services/investment_snapshots/freshness.py` — policy → status classifier (pure)
- `app/services/action_report/__init__.py`
- `app/services/action_report/common/__init__.py`
- `app/services/action_report/common/canonicalize.py` — canonical payload hash (pure)
- `tests/services/investment_snapshots/__init__.py`
- `tests/services/investment_snapshots/test_repository.py`
- `tests/services/investment_snapshots/test_append_only.py`
- `tests/services/investment_snapshots/test_freshness.py`
- `tests/services/action_report/__init__.py`
- `tests/services/action_report/common/__init__.py`
- `tests/services/action_report/common/test_canonicalize.py`
- `tests/services/test_investment_snapshots_roundtrip.py`
- `scripts/snapshot_bundle_smoke.py` — local dry-run CLI

**Modify (optional, only if needed for model registration):**
- `app/models/__init__.py` — if the codebase auto-imports models there; if not, skip.

**Read but do not modify** (anchors for patterns):
- `app/models/investment_reports.py` — ROB-265, use as schema/CheckConstraint reference
- `alembic/versions/20260518_rob265_add_investment_reports.py` — migration style template
- `app/services/investment_reports/repository.py` — repository class pattern
- `app/services/crypto_insight_snapshots/repository.py` — async DAO + Pydantic upsert DTO
- `tests/services/test_crypto_insight_snapshots_repository.py` — async db_session test pattern

---

## Conventions you must follow

These are not optional — they're already baked into the repo and the next phases assume them:

1. **All new tables under `review` schema.** Match ROB-265.
2. **JSONB defaults** use `sa.text("'{}'::jsonb")` in Alembic and `server_default=text("'{}'::jsonb")` in models.
3. **UUID columns** use `postgresql.UUID(as_uuid=True)` (Alembic) and `PG_UUID(as_uuid=True)` (model).
4. **Idempotency key** is a UNIQUE TEXT column on every artifact table. Auto-build format: `f"{run_uuid}:{snapshot_kind}:{symbol or '_'}:{canonical_payload_hash[:12]}"`.
5. **Pydantic models** use `model_config = ConfigDict(extra="forbid")`.
6. **Repository** does NO business logic. Class-based, `__init__(self, session: AsyncSession)`, flushes when it has to but never commits.
7. **Tests** use the existing `db_session` async fixture from `tests/conftest.py` and `@pytest.mark.asyncio`.
8. **account_scope enum** matches `review.investment_reports` exactly: `{'kis_live', 'kis_mock', 'alpaca_paper', 'upbit_live'}` — nullable.
9. **No `# noqa`, no `# type: ignore`** without an inline reason comment.
10. **Commit messages** prefix `feat(rob-269):` and end with `Co-Authored-By: Paperclip <noreply@paperclip.ing>`. Commits stay local; **never `git push`**.

---

## Task 1 — Canonicalize pure function (TDD-first because everything else hashes through this)

**Files:**
- Create: `app/services/action_report/common/canonicalize.py`
- Test: `tests/services/action_report/common/test_canonicalize.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/action_report/common/test_canonicalize.py
import datetime as dt

import pytest

from app.services.action_report.common.canonicalize import canonical_payload_hash


def test_hash_is_stable_for_identical_payload():
    a = {"symbol": "035420", "price": 195000.0}
    b = {"price": 195000.0, "symbol": "035420"}
    assert canonical_payload_hash(a) == canonical_payload_hash(b)


def test_hash_strips_sub_second_timestamps_to_second_precision():
    base = dt.datetime(2026, 5, 19, 11, 11, 1, tzinfo=dt.UTC)
    near = dt.datetime(2026, 5, 19, 11, 11, 1, 999_999, tzinfo=dt.UTC)
    a = {"as_of": base.isoformat()}
    b = {"as_of": near.isoformat()}
    assert canonical_payload_hash(a) == canonical_payload_hash(b)


def test_hash_excludes_source_timestamps_block():
    base = {"data": {"price": 195000.0}}
    a = {**base, "source_timestamps": {"fetched_at": "2026-05-19T11:11:00Z"}}
    b = {**base, "source_timestamps": {"fetched_at": "2026-05-19T11:11:30Z"}}
    assert canonical_payload_hash(a) == canonical_payload_hash(b)


def test_hash_normalizes_float_to_nine_digit_precision():
    a = {"price": 1.123456789012}
    b = {"price": 1.123456789}
    assert canonical_payload_hash(a) == canonical_payload_hash(b)


def test_hash_differs_when_meaningful_field_differs():
    a = {"symbol": "035420", "price": 195000.0}
    b = {"symbol": "035420", "price": 195100.0}
    assert canonical_payload_hash(a) != canonical_payload_hash(b)


def test_hash_is_64_char_sha256_hex():
    h = canonical_payload_hash({"x": 1})
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_handles_nested_dicts_and_lists():
    a = {"items": [{"k": 1}, {"k": 2}]}
    b = {"items": [{"k": 1}, {"k": 2}]}  # same — list order preserved (not sorted)
    assert canonical_payload_hash(a) == canonical_payload_hash(b)


def test_hash_list_order_is_significant():
    a = {"items": [1, 2, 3]}
    b = {"items": [3, 2, 1]}
    assert canonical_payload_hash(a) != canonical_payload_hash(b)


def test_hash_treats_none_and_missing_as_different():
    # Conservative: explicit null is meaningful information vs absent key.
    a = {"symbol": "035420", "name": None}
    b = {"symbol": "035420"}
    assert canonical_payload_hash(a) != canonical_payload_hash(b)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/common/test_canonicalize.py -v`
Expected: `ModuleNotFoundError: No module named 'app.services.action_report.common.canonicalize'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/action_report/common/canonicalize.py
"""Canonical payload hashing for snapshot dedup (ROB-269 Phase 1).

Normalization rules (locked in pre-plan Decision 3):
1. Keys lexicographically sorted at every level.
2. Top-level ``source_timestamps`` block is excluded.
3. ISO-8601 timestamp strings are truncated to second precision.
4. Floats are formatted to 9-digit fixed precision.
5. List order is preserved (not sorted) — order is meaningful for ordered series.
6. None values are kept (explicit null != absent key).

Returns a 64-char SHA-256 hex digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

_ISO_SUBSECOND_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.\d+(.*)$"
)


def _truncate_iso_subsecond(value: str) -> str:
    m = _ISO_SUBSECOND_RE.match(value)
    if m is None:
        return value
    return f"{m.group(1)}{m.group(2)}"


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _normalize(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, float):
        return f"{value:.9f}"
    if isinstance(value, str):
        return _truncate_iso_subsecond(value)
    return value


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return SHA-256 hex of canonicalized payload (see module docstring)."""
    stripped = {k: payload[k] for k in payload if k != "source_timestamps"}
    normalized = _normalize(stripped)
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
```

Also create `app/services/action_report/__init__.py` and `app/services/action_report/common/__init__.py` as empty files, plus the corresponding test `__init__.py` files.

- [ ] **Step 4: Run test to verify passes**

Run: `uv run pytest tests/services/action_report/common/test_canonicalize.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/ tests/services/action_report/
git commit -m "$(cat <<'EOF'
feat(rob-269): canonical_payload_hash for snapshot dedup

Phase 1 of ROB-269 — pure-function canonicalizer used by both the
repository (idempotency_key build) and the dedup UNIQUE index. See
docs/superpowers/plans/2026-05-19-rob-269-pre-plan.md Decision 3.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 2 — Freshness classifier (pure)

**Files:**
- Create: `app/services/investment_snapshots/__init__.py`
- Create: `app/services/investment_snapshots/freshness.py`
- Test: `tests/services/investment_snapshots/__init__.py`
- Test: `tests/services/investment_snapshots/test_freshness.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/investment_snapshots/test_freshness.py
import datetime as dt

import pytest

from app.services.investment_snapshots.freshness import (
    FreshnessPolicy,
    classify_freshness,
)


def _policy(soft: int, hard: int) -> FreshnessPolicy:
    return FreshnessPolicy(
        soft_ttl=dt.timedelta(seconds=soft),
        hard_ttl=dt.timedelta(seconds=hard),
    )


def test_classify_fresh_within_soft():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    as_of = now - dt.timedelta(seconds=30)
    assert classify_freshness(as_of=as_of, now=now, policy=_policy(60, 300)) == "fresh"


def test_classify_soft_stale_past_soft_within_hard():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    as_of = now - dt.timedelta(seconds=120)
    assert classify_freshness(as_of=as_of, now=now, policy=_policy(60, 300)) == "soft_stale"


def test_classify_hard_stale_past_hard():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    as_of = now - dt.timedelta(seconds=900)
    assert classify_freshness(as_of=as_of, now=now, policy=_policy(60, 300)) == "hard_stale"


def test_classify_unavailable_when_as_of_is_none():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    assert classify_freshness(as_of=None, now=now, policy=_policy(60, 300)) == "unavailable"


def test_classify_rejects_naive_datetime():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    naive = dt.datetime(2026, 5, 19, 11, 10, 30)  # no tzinfo
    with pytest.raises(ValueError, match="tz-aware"):
        classify_freshness(as_of=naive, now=now, policy=_policy(60, 300))


def test_classify_rejects_future_as_of_more_than_skew():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    future = now + dt.timedelta(seconds=120)
    with pytest.raises(ValueError, match="future"):
        classify_freshness(as_of=future, now=now, policy=_policy(60, 300))


def test_classify_tolerates_small_clock_skew_into_future():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    slight_future = now + dt.timedelta(seconds=2)
    assert classify_freshness(as_of=slight_future, now=now, policy=_policy(60, 300)) == "fresh"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_snapshots/test_freshness.py -v`
Expected: `ModuleNotFoundError` for `app.services.investment_snapshots.freshness`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/investment_snapshots/freshness.py
"""Freshness classifier for snapshot artifacts (ROB-269 Phase 1).

Pre-plan Decision 3: policy_snapshot_json is frozen per-run. Each snapshot
kind carries its own (soft_ttl, hard_ttl); this module is the deterministic
mapping from (as_of, now, policy) → status. Generator + DB CHECK consume
the result (Decision 4 three-layer stale gate).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

FreshnessStatus = Literal["fresh", "soft_stale", "hard_stale", "partial", "unavailable"]

# Clock skew tolerance — collectors and DB can disagree by a few seconds.
_CLOCK_SKEW = dt.timedelta(seconds=5)


@dataclass(frozen=True)
class FreshnessPolicy:
    soft_ttl: dt.timedelta
    hard_ttl: dt.timedelta


def classify_freshness(
    *,
    as_of: dt.datetime | None,
    now: dt.datetime,
    policy: FreshnessPolicy,
) -> FreshnessStatus:
    if as_of is None:
        return "unavailable"
    if as_of.tzinfo is None or now.tzinfo is None:
        raise ValueError("classify_freshness requires tz-aware datetimes")
    if as_of > now + _CLOCK_SKEW:
        raise ValueError(f"as_of {as_of.isoformat()} is in the future of now {now.isoformat()}")
    age = max(now - as_of, dt.timedelta(0))
    if age <= policy.soft_ttl:
        return "fresh"
    if age <= policy.hard_ttl:
        return "soft_stale"
    return "hard_stale"
```

- [ ] **Step 4: Run test to verify passes**

Run: `uv run pytest tests/services/investment_snapshots/test_freshness.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_snapshots/ tests/services/investment_snapshots/
git commit -m "$(cat <<'EOF'
feat(rob-269): freshness classifier (fresh/soft_stale/hard_stale/unavailable)

Phase 1 of ROB-269 — pure (as_of, now, policy) -> status mapping.
Decision 4 three-layer stale gate consumes this. Decision 3 policy_snapshot
freeze relies on (soft_ttl, hard_ttl) per snapshot_kind.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 3 — Alembic migration for 4 tables

**Files:**
- Create: `alembic/versions/20260519_rob269_add_snapshot_foundation.py`

This task does not have a unit test — verification is via `uv run alembic upgrade head` cleanly applying and `uv run alembic downgrade -1` cleanly reverting. The integration test in Task 7 will exercise the schema end-to-end.

- [ ] **Step 1: Confirm current alembic head**

Run: `uv run alembic current`
Capture the revision id printed. The migration file's `down_revision` must point at this. Expected: `20260518_rob265` (from ROB-265). If different, use whatever is printed.

- [ ] **Step 2: Create the migration file**

```python
# alembic/versions/20260519_rob269_add_snapshot_foundation.py
"""rob-269 phase 1: snapshot foundation (runs/snapshots/bundles/items)

Revision ID: 20260519_rob269_p1
Revises: 20260518_rob265
Create Date: 2026-05-19

Adds 4 immutable tables under ``review`` schema. All additive. Append-only
invariant is enforced at the service layer (Task 4) — no DB trigger in v1.

See: docs/superpowers/plans/2026-05-19-rob-269-pre-plan.md (Decisions 1, 3).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260519_rob269_p1"
down_revision: str | None = "20260518_rob265"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb_default(literal: str) -> sa.sql.elements.TextClause:
    return sa.text(f"'{literal}'::jsonb")


_ACCOUNT_SCOPE_CHECK = (
    "account_scope IS NULL OR account_scope IN "
    "('kis_live','kis_mock','alpaca_paper','upbit_live')"
)
_MARKET_CHECK = "market IN ('kr','us','crypto')"


def upgrade() -> None:
    # ----------------------------------------------------------------
    # review.investment_snapshot_runs
    # ----------------------------------------------------------------
    op.create_table(
        "investment_snapshot_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("run_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column(
            "policy_snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("refresh_reason", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.UniqueConstraint("run_uuid", name="uq_investment_snapshot_runs_run_uuid"),
        sa.CheckConstraint(
            "purpose IN ('report_generation','scheduled_refresh',"
            "'manual_refresh','reviewer_requested')",
            name="ck_investment_snapshot_runs_purpose",
        ),
        sa.CheckConstraint(_MARKET_CHECK, name="ck_investment_snapshot_runs_market"),
        sa.CheckConstraint(
            _ACCOUNT_SCOPE_CHECK,
            name="ck_investment_snapshot_runs_account_scope",
        ),
        sa.CheckConstraint(
            "status IN ('running','completed','partial','failed')",
            name="ck_investment_snapshot_runs_status",
        ),
        sa.CheckConstraint(
            "requested_by IN ('hermes','user','scheduler','claude_code','reviewer')",
            name="ck_investment_snapshot_runs_requested_by",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshot_runs_purpose_market_started",
        "investment_snapshot_runs",
        ["purpose", "market", sa.text("started_at DESC")],
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshot_runs_status_started",
        "investment_snapshot_runs",
        ["status", sa.text("started_at DESC")],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_snapshots
    # ----------------------------------------------------------------
    op.create_table(
        "investment_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("snapshot_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey("review.investment_snapshot_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_kind", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("source_table", sa.Text(), nullable=True),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "source_timestamps_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "coverage_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "errors_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("as_of", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "collected_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("freshness_status", sa.Text(), nullable=False),
        sa.Column("canonical_payload_hash", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("snapshot_uuid", name="uq_investment_snapshots_snapshot_uuid"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_investment_snapshots_idempotency_key"
        ),
        sa.UniqueConstraint(
            "canonical_payload_hash",
            "snapshot_kind",
            "market",
            "account_scope",
            name="uq_investment_snapshots_canonical_dedup",
        ),
        sa.CheckConstraint(
            "snapshot_kind IN ('portfolio','market','news','symbol',"
            "'candidate_universe','browser_probe','invest_page',"
            "'journal','watch_context','naver_remote_debug',"
            "'toss_remote_debug','llm_input_frozen')",
            name="ck_investment_snapshots_snapshot_kind",
        ),
        sa.CheckConstraint(_MARKET_CHECK, name="ck_investment_snapshots_market"),
        sa.CheckConstraint(
            _ACCOUNT_SCOPE_CHECK,
            name="ck_investment_snapshots_account_scope",
        ),
        sa.CheckConstraint(
            "source_kind IN ('kis_mcp','auto_trader_mcp','invest_api',"
            "'naver_remote_debug','toss_remote_debug','combined',"
            "'news_ingestor','manual','domain_ref')",
            name="ck_investment_snapshots_source_kind",
        ),
        sa.CheckConstraint(
            "freshness_status IN ('fresh','soft_stale','hard_stale',"
            "'partial','unavailable')",
            name="ck_investment_snapshots_freshness_status",
        ),
        sa.CheckConstraint(
            "(source_table IS NULL AND source_id IS NULL AND source_uri IS NULL) "
            "OR (source_table IS NOT NULL AND source_id IS NOT NULL "
            "AND source_uri IS NOT NULL)",
            name="ck_investment_snapshots_source_ref_triple",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshots_kind_market_symbol_as_of",
        "investment_snapshots",
        ["snapshot_kind", "market", "symbol", sa.text("as_of DESC")],
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshots_source_uri",
        "investment_snapshots",
        ["source_uri"],
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshots_run_id",
        "investment_snapshots",
        ["run_id"],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_snapshot_bundles
    # ----------------------------------------------------------------
    op.create_table(
        "investment_snapshot_bundles",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("bundle_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column(
            "policy_snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("as_of", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "coverage_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "freshness_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "bundle_uuid", name="uq_investment_snapshot_bundles_bundle_uuid"
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_investment_snapshot_bundles_idempotency_key",
        ),
        sa.CheckConstraint(_MARKET_CHECK, name="ck_investment_snapshot_bundles_market"),
        sa.CheckConstraint(
            _ACCOUNT_SCOPE_CHECK,
            name="ck_investment_snapshot_bundles_account_scope",
        ),
        sa.CheckConstraint(
            "status IN ('complete','partial','stale_fallback','failed')",
            name="ck_investment_snapshot_bundles_status",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshot_bundles_purpose_market_account_asof",
        "investment_snapshot_bundles",
        ["purpose", "market", "account_scope", sa.text("as_of DESC")],
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshot_bundles_status_created",
        "investment_snapshot_bundles",
        ["status", sa.text("created_at DESC")],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_snapshot_bundle_items
    # ----------------------------------------------------------------
    op.create_table(
        "investment_snapshot_bundle_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column(
            "bundle_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "review.investment_snapshot_bundles.id", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "review.investment_snapshots.id", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "bundle_id",
            "snapshot_id",
            name="uq_investment_snapshot_bundle_items_bundle_snapshot",
        ),
        sa.CheckConstraint(
            "role IN ('required','optional','fallback','conflict_evidence')",
            name="ck_investment_snapshot_bundle_items_role",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshot_bundle_items_snapshot",
        "investment_snapshot_bundle_items",
        ["snapshot_id"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_table("investment_snapshot_bundle_items", schema="review")
    op.drop_table("investment_snapshot_bundles", schema="review")
    op.drop_table("investment_snapshots", schema="review")
    op.drop_table("investment_snapshot_runs", schema="review")
```

- [ ] **Step 3: Verify migration applies clean**

Run: `uv run alembic upgrade head`
Expected: completes without error, prints `Running upgrade 20260518_rob265 -> 20260519_rob269_p1`.

Sanity-check the tables exist:

```bash
docker compose exec postgres psql -U postgres -d auto_trader -c "\dt review.investment_snapshot*"
```

Expected: 4 rows (`investment_snapshot_bundle_items`, `investment_snapshot_bundles`, `investment_snapshot_runs`, `investment_snapshots`).

- [ ] **Step 4: Verify downgrade is clean**

Run: `uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: downgrade then upgrade both succeed; psql check still shows 4 tables after.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/20260519_rob269_add_snapshot_foundation.py
git commit -m "$(cat <<'EOF'
feat(rob-269): alembic migration for snapshot foundation

Phase 1 of ROB-269 — adds review.investment_snapshot_{runs,snapshots,
bundles,bundle_items}. All additive, no data backfill, CASCADE on run
deletion, RESTRICT on snapshot deletion when a bundle references it.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 4 — SQLAlchemy models for 4 tables

**Files:**
- Create: `app/models/investment_snapshots.py`

This task has no dedicated unit test — models are exercised by Task 5 (schemas) and Task 6 (repository tests). If the codebase's `app/models/__init__.py` explicitly imports every model module, add the import there; otherwise leave it.

- [ ] **Step 1: Write the model file**

```python
# app/models/investment_snapshots.py
"""ROB-269 Phase 1 — Snapshot foundation ORM (immutable artifacts).

Four tables under ``review`` schema:
* ``investment_snapshot_runs``   — one collection run.
* ``investment_snapshots``       — immutable artifact row.
* ``investment_snapshot_bundles``— a reusable report data bundle.
* ``investment_snapshot_bundle_items`` — bundle ↔ snapshot link with role.

Append-only invariant is enforced at the service layer
(``app.services.investment_snapshots.repository``). Direct ``UPDATE/DELETE``
is forbidden once services land.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text

from app.models.base import Base


_ACCOUNT_SCOPE_CHECK = (
    "account_scope IS NULL OR account_scope IN "
    "('kis_live','kis_mock','alpaca_paper','upbit_live')"
)
_MARKET_CHECK = "market IN ('kr','us','crypto')"


# ---------------------------------------------------------------------------
# review.investment_snapshot_runs
# ---------------------------------------------------------------------------
class InvestmentSnapshotRun(Base):
    __tablename__ = "investment_snapshot_runs"
    __table_args__ = (
        UniqueConstraint("run_uuid", name="uq_investment_snapshot_runs_run_uuid"),
        CheckConstraint(
            "purpose IN ('report_generation','scheduled_refresh',"
            "'manual_refresh','reviewer_requested')",
            name="ck_investment_snapshot_runs_purpose",
        ),
        CheckConstraint(_MARKET_CHECK, name="ck_investment_snapshot_runs_market"),
        CheckConstraint(
            _ACCOUNT_SCOPE_CHECK,
            name="ck_investment_snapshot_runs_account_scope",
        ),
        CheckConstraint(
            "status IN ('running','completed','partial','failed')",
            name="ck_investment_snapshot_runs_status",
        ),
        CheckConstraint(
            "requested_by IN ('hermes','user','scheduler','claude_code','reviewer')",
            name="ck_investment_snapshot_runs_requested_by",
        ),
        Index(
            "ix_investment_snapshot_runs_purpose_market_started",
            "purpose",
            "market",
            text("started_at DESC"),
        ),
        Index(
            "ix_investment_snapshot_runs_status_started",
            "status",
            text("started_at DESC"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    account_scope: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'running'")
    )
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    policy_snapshot_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    refresh_reason: Mapped[str | None] = mapped_column(Text)
    error_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    run_metadata: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )


# ---------------------------------------------------------------------------
# review.investment_snapshots
# ---------------------------------------------------------------------------
class InvestmentSnapshot(Base):
    __tablename__ = "investment_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_uuid", name="uq_investment_snapshots_snapshot_uuid"
        ),
        UniqueConstraint(
            "idempotency_key", name="uq_investment_snapshots_idempotency_key"
        ),
        UniqueConstraint(
            "canonical_payload_hash",
            "snapshot_kind",
            "market",
            "account_scope",
            name="uq_investment_snapshots_canonical_dedup",
        ),
        CheckConstraint(
            "snapshot_kind IN ('portfolio','market','news','symbol',"
            "'candidate_universe','browser_probe','invest_page',"
            "'journal','watch_context','naver_remote_debug',"
            "'toss_remote_debug','llm_input_frozen')",
            name="ck_investment_snapshots_snapshot_kind",
        ),
        CheckConstraint(_MARKET_CHECK, name="ck_investment_snapshots_market"),
        CheckConstraint(
            _ACCOUNT_SCOPE_CHECK,
            name="ck_investment_snapshots_account_scope",
        ),
        CheckConstraint(
            "source_kind IN ('kis_mcp','auto_trader_mcp','invest_api',"
            "'naver_remote_debug','toss_remote_debug','combined',"
            "'news_ingestor','manual','domain_ref')",
            name="ck_investment_snapshots_source_kind",
        ),
        CheckConstraint(
            "freshness_status IN ('fresh','soft_stale','hard_stale',"
            "'partial','unavailable')",
            name="ck_investment_snapshots_freshness_status",
        ),
        CheckConstraint(
            "(source_table IS NULL AND source_id IS NULL AND source_uri IS NULL) "
            "OR (source_table IS NOT NULL AND source_id IS NOT NULL "
            "AND source_uri IS NOT NULL)",
            name="ck_investment_snapshots_source_ref_triple",
        ),
        Index(
            "ix_investment_snapshots_kind_market_symbol_as_of",
            "snapshot_kind",
            "market",
            "symbol",
            text("as_of DESC"),
        ),
        Index("ix_investment_snapshots_source_uri", "source_uri"),
        Index("ix_investment_snapshots_run_id", "run_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    run_id: Mapped[int] = mapped_column(
        ForeignKey("review.investment_snapshot_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_kind: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    account_scope: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(Text)

    source_table: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    source_uri: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)

    payload_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    source_timestamps_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    coverage_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    errors_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    as_of: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    valid_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    freshness_status: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# review.investment_snapshot_bundles
# ---------------------------------------------------------------------------
class InvestmentSnapshotBundle(Base):
    __tablename__ = "investment_snapshot_bundles"
    __table_args__ = (
        UniqueConstraint(
            "bundle_uuid", name="uq_investment_snapshot_bundles_bundle_uuid"
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_investment_snapshot_bundles_idempotency_key",
        ),
        CheckConstraint(_MARKET_CHECK, name="ck_investment_snapshot_bundles_market"),
        CheckConstraint(
            _ACCOUNT_SCOPE_CHECK,
            name="ck_investment_snapshot_bundles_account_scope",
        ),
        CheckConstraint(
            "status IN ('complete','partial','stale_fallback','failed')",
            name="ck_investment_snapshot_bundles_status",
        ),
        Index(
            "ix_investment_snapshot_bundles_purpose_market_account_asof",
            "purpose",
            "market",
            "account_scope",
            text("as_of DESC"),
        ),
        Index(
            "ix_investment_snapshot_bundles_status_created",
            "status",
            text("created_at DESC"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bundle_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    account_scope: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    policy_snapshot_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    as_of: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    coverage_summary: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    freshness_summary: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# review.investment_snapshot_bundle_items
# ---------------------------------------------------------------------------
class InvestmentSnapshotBundleItem(Base):
    __tablename__ = "investment_snapshot_bundle_items"
    __table_args__ = (
        UniqueConstraint(
            "bundle_id",
            "snapshot_id",
            name="uq_investment_snapshot_bundle_items_bundle_snapshot",
        ),
        CheckConstraint(
            "role IN ('required','optional','fallback','conflict_evidence')",
            name="ck_investment_snapshot_bundle_items_role",
        ),
        Index(
            "ix_investment_snapshot_bundle_items_snapshot",
            "snapshot_id",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bundle_id: Mapped[int] = mapped_column(
        ForeignKey(
            "review.investment_snapshot_bundles.id", ondelete="CASCADE"
        ),
        nullable=False,
    )
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey(
            "review.investment_snapshots.id", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 2: Smoke-check model import**

Run: `uv run python -c "from app.models.investment_snapshots import InvestmentSnapshotRun, InvestmentSnapshot, InvestmentSnapshotBundle, InvestmentSnapshotBundleItem; print('OK')"`
Expected: `OK`.

- [ ] **Step 3: Verify no Alembic drift**

Run: `uv run alembic check`
Expected: no autogenerate diffs. If diffs appear, the model and migration are out of sync — reconcile by editing the **model** to match the migration (the migration is the canonical source for column defaults/constraints).

- [ ] **Step 4: If `app/models/__init__.py` explicitly imports models, add this module**

```bash
grep -n "investment_reports" app/models/__init__.py || echo "(no explicit imports — skip)"
```

If present, mirror that import for `investment_snapshots`. If not (i.e., models are auto-discovered), leave alone.

- [ ] **Step 5: Commit**

```bash
git add app/models/investment_snapshots.py
# also app/models/__init__.py if modified
git commit -m "$(cat <<'EOF'
feat(rob-269): SQLAlchemy models for snapshot foundation

Phase 1 of ROB-269 — 4 ORM classes mirroring the alembic migration.
Append-only invariant lives at the service layer (Task 6).

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 5 — Pydantic schemas

**Files:**
- Create: `app/schemas/investment_snapshots.py`

- [ ] **Step 1: Write the schema file**

```python
# app/schemas/investment_snapshots.py
"""ROB-269 Phase 1 — Pydantic DTOs for snapshot foundation."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SnapshotPurpose = Literal[
    "report_generation", "scheduled_refresh", "manual_refresh", "reviewer_requested"
]
SnapshotMarket = Literal["kr", "us", "crypto"]
SnapshotAccountScope = Literal["kis_live", "kis_mock", "alpaca_paper", "upbit_live"]
SnapshotRunStatus = Literal["running", "completed", "partial", "failed"]
SnapshotRequestedBy = Literal["hermes", "user", "scheduler", "claude_code", "reviewer"]
SnapshotKind = Literal[
    "portfolio",
    "market",
    "news",
    "symbol",
    "candidate_universe",
    "browser_probe",
    "invest_page",
    "journal",
    "watch_context",
    "naver_remote_debug",
    "toss_remote_debug",
    "llm_input_frozen",
]
SourceKind = Literal[
    "kis_mcp",
    "auto_trader_mcp",
    "invest_api",
    "naver_remote_debug",
    "toss_remote_debug",
    "combined",
    "news_ingestor",
    "manual",
    "domain_ref",
]
FreshnessStatus = Literal[
    "fresh", "soft_stale", "hard_stale", "partial", "unavailable"
]
BundleStatus = Literal["complete", "partial", "stale_fallback", "failed"]
BundleItemRole = Literal["required", "optional", "fallback", "conflict_evidence"]


class SnapshotRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: SnapshotPurpose
    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None = None
    requested_by: SnapshotRequestedBy
    policy_version: str
    policy_snapshot_json: dict[str, Any] = Field(default_factory=dict)
    refresh_reason: str | None = None
    run_metadata: dict[str, Any] = Field(default_factory=dict)


class SnapshotCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_uuid: uuid.UUID
    snapshot_kind: SnapshotKind
    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None = None
    symbol: str | None = None
    source_table: str | None = None
    source_id: int | None = None
    source_uri: str | None = None
    source_kind: SourceKind
    payload_json: dict[str, Any] = Field(default_factory=dict)
    source_timestamps_json: dict[str, Any] = Field(default_factory=dict)
    coverage_json: dict[str, Any] = Field(default_factory=dict)
    errors_json: dict[str, Any] = Field(default_factory=dict)
    as_of: dt.datetime
    valid_until: dt.datetime | None = None
    freshness_status: FreshnessStatus

    @model_validator(mode="after")
    def _source_ref_triple_consistent(self) -> "SnapshotCreate":
        triple = (self.source_table, self.source_id, self.source_uri)
        nulls = sum(1 for v in triple if v is None)
        if nulls not in (0, 3):
            raise ValueError(
                "source_table / source_id / source_uri must all be set or all None"
            )
        return self

    @model_validator(mode="after")
    def _domain_ref_requires_source_triple(self) -> "SnapshotCreate":
        if self.source_kind == "domain_ref" and self.source_table is None:
            raise ValueError(
                "source_kind='domain_ref' requires the source_table/source_id/source_uri triple"
            )
        return self


class BundleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str
    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None = None
    policy_version: str
    policy_snapshot_json: dict[str, Any] = Field(default_factory=dict)
    as_of: dt.datetime
    status: BundleStatus
    coverage_summary: dict[str, Any] = Field(default_factory=dict)
    freshness_summary: dict[str, Any] = Field(default_factory=dict)


class BundleItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_uuid: uuid.UUID
    role: BundleItemRole
```

- [ ] **Step 2: Smoke-check import**

Run: `uv run python -c "from app.schemas.investment_snapshots import SnapshotRunCreate, SnapshotCreate, BundleCreate, BundleItemCreate; print('OK')"`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add app/schemas/investment_snapshots.py
git commit -m "$(cat <<'EOF'
feat(rob-269): Pydantic DTOs for snapshot foundation

Phase 1 of ROB-269 — Create DTOs with source_ref triple consistency
validator and domain_ref source_kind constraint.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 6 — Repository (append-only DAO)

**Files:**
- Create: `app/services/investment_snapshots/repository.py`
- Test: `tests/services/investment_snapshots/test_repository.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/investment_snapshots/test_repository.py
import datetime as dt
import uuid

import pytest

from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.action_report.common.canonicalize import canonical_payload_hash
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)


def _run_payload() -> SnapshotRunCreate:
    return SnapshotRunCreate(
        purpose="report_generation",
        market="kr",
        account_scope="kis_live",
        requested_by="user",
        policy_version="intraday_action_report_v1",
        policy_snapshot_json={"portfolio": {"soft_ttl": 60, "hard_ttl": 300}},
        run_metadata={"local_smoke": True},
    )


def _snapshot_payload(run_uuid: uuid.UUID, *, price: float = 195000.0) -> SnapshotCreate:
    payload = {"symbol": "035420", "price": price}
    return SnapshotCreate(
        run_uuid=run_uuid,
        snapshot_kind="symbol",
        market="kr",
        account_scope="kis_live",
        symbol="035420",
        source_kind="kis_mcp",
        payload_json=payload,
        as_of=_now(),
        freshness_status="fresh",
    )


@pytest.mark.asyncio
async def test_insert_run_returns_persisted_row(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(_run_payload())
    await db_session.commit()
    assert run.id > 0
    assert run.purpose == "report_generation"
    assert run.run_uuid is not None


@pytest.mark.asyncio
async def test_insert_snapshot_computes_canonical_hash_and_idempotency_key(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(_run_payload())
    snap = await repo.insert_snapshot(_snapshot_payload(run.run_uuid))
    await db_session.commit()

    expected_hash = canonical_payload_hash({"symbol": "035420", "price": 195000.0})
    assert snap.canonical_payload_hash == expected_hash
    assert snap.idempotency_key.startswith(f"{run.run_uuid}:symbol:035420:")
    assert snap.idempotency_key.endswith(expected_hash[:12])


@pytest.mark.asyncio
async def test_insert_snapshot_dedupes_identical_payload(db_session):
    """Same canonical payload → same row reused, second call returns existing."""
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(_run_payload())
    a = await repo.insert_snapshot(_snapshot_payload(run.run_uuid))
    b = await repo.insert_snapshot(_snapshot_payload(run.run_uuid))
    await db_session.commit()
    assert a.id == b.id


@pytest.mark.asyncio
async def test_insert_bundle_and_link_items(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(_run_payload())
    snap = await repo.insert_snapshot(_snapshot_payload(run.run_uuid))
    bundle = await repo.insert_bundle(
        BundleCreate(
            purpose="kr_action_report",
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_now(),
            status="complete",
            coverage_summary={"required": {"symbol": "fresh"}},
            freshness_summary={"symbol": {"as_of": _now().isoformat(), "status": "fresh"}},
        )
    )
    item = await repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snap.snapshot_uuid, role="required"),
    )
    await db_session.commit()
    assert item.bundle_id == bundle.id
    assert item.snapshot_id == snap.id
    assert item.role == "required"


@pytest.mark.asyncio
async def test_get_run_by_uuid_and_get_snapshot_by_uuid(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(_run_payload())
    snap = await repo.insert_snapshot(_snapshot_payload(run.run_uuid))
    await db_session.commit()

    assert (await repo.get_run_by_uuid(run.run_uuid)).id == run.id
    assert (await repo.get_snapshot_by_uuid(snap.snapshot_uuid)).id == snap.id
    assert await repo.get_run_by_uuid(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_source_ref_domain_ref_round_trip(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(_run_payload())
    snap = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="candidate_universe",
            market="kr",
            source_kind="domain_ref",
            source_table="invest_screener_snapshots",
            source_id=42,
            source_uri="invest_screener_snapshots:42",
            payload_json={"ref_only": True},
            as_of=_now(),
            freshness_status="fresh",
        )
    )
    await db_session.commit()
    assert snap.source_table == "invest_screener_snapshots"
    assert snap.source_uri == "invest_screener_snapshots:42"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_snapshots/test_repository.py -v`
Expected: ImportError for `InvestmentSnapshotsRepository`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/investment_snapshots/repository.py
"""ROB-269 Phase 1 — DAO over investment_snapshot_* tables.

Append-only invariant: ``insert_*`` and ``link_*`` are the only writes.
``UPDATE`` and ``DELETE`` are intentionally absent. A separate test
(``test_append_only.py``) verifies this is enforced.

Hash + idempotency_key composition lives here (not in the schema) so the
dedup UNIQUE constraint can rely on a deterministic input.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_snapshots import (
    InvestmentSnapshot,
    InvestmentSnapshotBundle,
    InvestmentSnapshotBundleItem,
    InvestmentSnapshotRun,
)
from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.action_report.common.canonicalize import canonical_payload_hash


class InvestmentSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    async def insert_run(self, payload: SnapshotRunCreate) -> InvestmentSnapshotRun:
        data: dict[str, Any] = payload.model_dump()
        data["run_metadata"] = data.pop("run_metadata")
        row = InvestmentSnapshotRun(**data)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_run_by_uuid(
        self, run_uuid: uuid.UUID
    ) -> InvestmentSnapshotRun | None:
        return await self._session.scalar(
            sa.select(InvestmentSnapshotRun).where(
                InvestmentSnapshotRun.run_uuid == run_uuid
            )
        )

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------
    async def insert_snapshot(
        self, payload: SnapshotCreate
    ) -> InvestmentSnapshot:
        # 1. Resolve run.
        run = await self.get_run_by_uuid(payload.run_uuid)
        if run is None:
            raise ValueError(f"run not found: {payload.run_uuid}")

        # 2. Compute canonical hash + idempotency key.
        canonical_hash = canonical_payload_hash(payload.payload_json)
        symbol_component = payload.symbol or "_"
        idempotency_key = (
            f"{run.run_uuid}:{payload.snapshot_kind}:"
            f"{symbol_component}:{canonical_hash[:12]}"
        )

        # 3. Dedup short-circuit — same canonical payload reuses the existing row.
        existing = await self._session.scalar(
            sa.select(InvestmentSnapshot).where(
                InvestmentSnapshot.canonical_payload_hash == canonical_hash,
                InvestmentSnapshot.snapshot_kind == payload.snapshot_kind,
                InvestmentSnapshot.market == payload.market,
                InvestmentSnapshot.account_scope == payload.account_scope,
            )
        )
        if existing is not None:
            return existing

        # 4. Insert.
        data = payload.model_dump(exclude={"run_uuid"})
        row = InvestmentSnapshot(
            run_id=run.id,
            canonical_payload_hash=canonical_hash,
            idempotency_key=idempotency_key,
            **data,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_snapshot_by_uuid(
        self, snapshot_uuid: uuid.UUID
    ) -> InvestmentSnapshot | None:
        return await self._session.scalar(
            sa.select(InvestmentSnapshot).where(
                InvestmentSnapshot.snapshot_uuid == snapshot_uuid
            )
        )

    # ------------------------------------------------------------------
    # Bundles
    # ------------------------------------------------------------------
    async def insert_bundle(
        self, payload: BundleCreate
    ) -> InvestmentSnapshotBundle:
        # Bundle idempotency_key default: deterministic over identity tuple.
        idempotency_key = (
            f"bundle:{payload.purpose}:{payload.market}:"
            f"{payload.account_scope or '_'}:{payload.policy_version}:"
            f"{payload.as_of.isoformat()}"
        )
        existing = await self._session.scalar(
            sa.select(InvestmentSnapshotBundle).where(
                InvestmentSnapshotBundle.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing
        data = payload.model_dump()
        row = InvestmentSnapshotBundle(idempotency_key=idempotency_key, **data)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def link_bundle_item(
        self, *, bundle_uuid: uuid.UUID, item: BundleItemCreate
    ) -> InvestmentSnapshotBundleItem:
        bundle = await self._session.scalar(
            sa.select(InvestmentSnapshotBundle).where(
                InvestmentSnapshotBundle.bundle_uuid == bundle_uuid
            )
        )
        if bundle is None:
            raise ValueError(f"bundle not found: {bundle_uuid}")
        snapshot = await self.get_snapshot_by_uuid(item.snapshot_uuid)
        if snapshot is None:
            raise ValueError(f"snapshot not found: {item.snapshot_uuid}")
        # Reuse if same (bundle, snapshot) already linked.
        existing = await self._session.scalar(
            sa.select(InvestmentSnapshotBundleItem).where(
                InvestmentSnapshotBundleItem.bundle_id == bundle.id,
                InvestmentSnapshotBundleItem.snapshot_id == snapshot.id,
            )
        )
        if existing is not None:
            return existing
        row = InvestmentSnapshotBundleItem(
            bundle_id=bundle.id, snapshot_id=snapshot.id, role=item.role
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row
```

Also create `app/services/investment_snapshots/__init__.py` (empty), `tests/services/investment_snapshots/__init__.py` (empty).

- [ ] **Step 4: Run test to verify passes**

Run: `uv run pytest tests/services/investment_snapshots/test_repository.py -v`
Expected: 6 passed. If `db_session` fixture errors with "schema review does not exist," ensure `uv run alembic upgrade head` ran for the test DB (see `tests/conftest.py` for the fixture wiring).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_snapshots/repository.py app/services/investment_snapshots/__init__.py tests/services/investment_snapshots/
git commit -m "$(cat <<'EOF'
feat(rob-269): InvestmentSnapshotsRepository (append-only DAO)

Phase 1 of ROB-269 — insert/get for runs/snapshots/bundles/items.
Canonical hash + idempotency_key composition happens here so the dedup
UNIQUE constraint sees deterministic input. No UPDATE/DELETE methods.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 7 — Append-only invariant test

**Files:**
- Test: `tests/services/investment_snapshots/test_append_only.py`

This task asserts the repository surface itself is append-only — no `update_*` / `delete_*` method names. It's a static guard so future PRs can't quietly add mutation.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/investment_snapshots/test_append_only.py
import inspect

from app.services.investment_snapshots import repository as repo_mod


_FORBIDDEN_PREFIXES = ("update_", "delete_", "remove_", "mutate_", "patch_")


def test_repository_surface_has_no_mutation_methods():
    cls = repo_mod.InvestmentSnapshotsRepository
    public_methods = [
        name
        for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    forbidden = [
        m for m in public_methods if m.startswith(_FORBIDDEN_PREFIXES)
    ]
    assert forbidden == [], (
        f"Append-only invariant violated: {forbidden}. "
        "Snapshot artifacts must be immutable; status transitions live on "
        "the run row only and require a separate write path with reviewer "
        "sign-off."
    )


def test_repository_surface_only_inserts_and_links_and_reads():
    cls = repo_mod.InvestmentSnapshotsRepository
    public_methods = sorted(
        name
        for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    )
    # Lock the surface — adding a new method requires this test to be updated
    # which forces reviewer awareness.
    assert public_methods == [
        "get_run_by_uuid",
        "get_snapshot_by_uuid",
        "insert_bundle",
        "insert_run",
        "insert_snapshot",
        "link_bundle_item",
    ]
```

- [ ] **Step 2: Run test to verify it passes** (the implementation from Task 6 already satisfies this)

Run: `uv run pytest tests/services/investment_snapshots/test_append_only.py -v`
Expected: 2 passed. If failing, audit the repository in Task 6 — the public surface drifted from the locked allow-list.

- [ ] **Step 3: Commit**

```bash
git add tests/services/investment_snapshots/test_append_only.py
git commit -m "$(cat <<'EOF'
test(rob-269): pin append-only surface of snapshots repository

Phase 1 of ROB-269 — guard test fails if any future PR adds update_/delete_
methods or otherwise changes the public surface without updating this test.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 8 — Round-trip integration test

**Files:**
- Test: `tests/services/test_investment_snapshots_roundtrip.py`

End-to-end: insert run → insert 3 snapshots (kinds: portfolio, market, candidate_universe with source_ref) → insert bundle → link all 3 items → read back via bundle. Exercises CASCADE/RESTRICT FK + UNIQUE indexes + JSONB persistence.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_investment_snapshots_roundtrip.py
import datetime as dt

import pytest
import sqlalchemy as sa

from app.models.investment_snapshots import (
    InvestmentSnapshot,
    InvestmentSnapshotBundleItem,
)
from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_full_bundle_round_trip(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="kr",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
            policy_snapshot_json={
                "portfolio": {"soft_ttl": 60, "hard_ttl": 300},
                "market": {"soft_ttl": 180, "hard_ttl": 600},
                "candidate_universe": {"soft_ttl": 900, "hard_ttl": 3600},
            },
        )
    )

    portfolio = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="portfolio",
            market="kr",
            account_scope="kis_live",
            source_kind="kis_mcp",
            payload_json={"cash_krw": 1_000_000, "holdings": []},
            as_of=_now(),
            freshness_status="fresh",
        )
    )
    market = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="market",
            market="kr",
            source_kind="domain_ref",
            source_table="market_quote_snapshots",
            source_id=1,
            source_uri="market_quote_snapshots:1",
            payload_json={"kospi": 2710.0},
            as_of=_now(),
            freshness_status="fresh",
        )
    )
    candidate = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="candidate_universe",
            market="kr",
            source_kind="domain_ref",
            source_table="invest_screener_snapshots",
            source_id=42,
            source_uri="invest_screener_snapshots:42",
            payload_json={"top_n": [{"symbol": "035420"}]},
            as_of=_now(),
            freshness_status="fresh",
        )
    )

    bundle = await repo.insert_bundle(
        BundleCreate(
            purpose="kr_action_report",
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_now(),
            status="complete",
            coverage_summary={
                "required": {"portfolio": "fresh", "market": "fresh"},
                "optional": {"candidate_universe": "fresh"},
            },
            freshness_summary={
                "portfolio": {"status": "fresh"},
                "market": {"status": "fresh"},
                "candidate_universe": {"status": "fresh"},
            },
        )
    )

    for snap, role in [
        (portfolio, "required"),
        (market, "required"),
        (candidate, "optional"),
    ]:
        await repo.link_bundle_item(
            bundle_uuid=bundle.bundle_uuid,
            item=BundleItemCreate(snapshot_uuid=snap.snapshot_uuid, role=role),
        )
    await db_session.commit()

    # Read back: 3 items linked to this bundle.
    rows = (
        await db_session.execute(
            sa.select(InvestmentSnapshotBundleItem).where(
                InvestmentSnapshotBundleItem.bundle_id == bundle.id
            )
        )
    ).scalars().all()
    assert len(rows) == 3
    assert {r.role for r in rows} == {"required", "optional"}

    # source_ref triple persisted on domain_ref snapshots.
    market_row = await db_session.scalar(
        sa.select(InvestmentSnapshot).where(InvestmentSnapshot.id == market.id)
    )
    assert market_row.source_uri == "market_quote_snapshots:1"
```

- [ ] **Step 2: Run test to verify passes**

Run: `uv run pytest tests/services/test_investment_snapshots_roundtrip.py -v`
Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_investment_snapshots_roundtrip.py
git commit -m "$(cat <<'EOF'
test(rob-269): full bundle round-trip integration test

Phase 1 of ROB-269 — exercises run -> 3 snapshots (incl. 2 domain_ref) ->
bundle -> 3 linked items. Validates CASCADE/RESTRICT FK, JSONB persistence,
source_ref triple round trip.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 9 — Dry-run smoke CLI

**Files:**
- Create: `scripts/snapshot_bundle_smoke.py`

A local-only CLI that exercises the foundation end-to-end against the dev DB. Does NOT call live KIS/Upbit/Alpaca — all payloads are static fixtures.

- [ ] **Step 1: Write the CLI**

```python
# scripts/snapshot_bundle_smoke.py
"""ROB-269 Phase 1 dry-run smoke.

Local-only. Inserts a fake snapshot run + 2 snapshots + 1 bundle + 2 links
against the dev DB, prints the resulting UUIDs, then exits. Always uses
``requested_by='user'`` and ``policy_version='intraday_action_report_v1_smoke'``.

Safety: no broker/order/network mutation. All payloads are static. Run only
against a non-production DB.

Usage:
    uv run python -m scripts.snapshot_bundle_smoke --dry-run
    uv run python -m scripts.snapshot_bundle_smoke --commit  # actually commits the tx
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

from app.db.session import async_session_factory  # adjust import if different
from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC).replace(microsecond=0)


async def _run(commit: bool) -> int:
    async with async_session_factory() as session:
        repo = InvestmentSnapshotsRepository(session)
        run = await repo.insert_run(
            SnapshotRunCreate(
                purpose="manual_refresh",
                market="kr",
                account_scope="kis_live",
                requested_by="user",
                policy_version="intraday_action_report_v1_smoke",
                policy_snapshot_json={"smoke": True},
                refresh_reason="rob-269 phase 1 local smoke",
            )
        )
        snap = await repo.insert_snapshot(
            SnapshotCreate(
                run_uuid=run.run_uuid,
                snapshot_kind="portfolio",
                market="kr",
                account_scope="kis_live",
                source_kind="manual",
                payload_json={"cash_krw": 0, "holdings": []},
                as_of=_now(),
                freshness_status="fresh",
            )
        )
        bundle = await repo.insert_bundle(
            BundleCreate(
                purpose="kr_action_report_smoke",
                market="kr",
                account_scope="kis_live",
                policy_version="intraday_action_report_v1_smoke",
                as_of=_now(),
                status="complete",
            )
        )
        await repo.link_bundle_item(
            bundle_uuid=bundle.bundle_uuid,
            item=BundleItemCreate(
                snapshot_uuid=snap.snapshot_uuid, role="required"
            ),
        )

        print(f"run_uuid     = {run.run_uuid}")
        print(f"snapshot_uuid= {snap.snapshot_uuid}")
        print(f"bundle_uuid  = {bundle.bundle_uuid}")

        if commit:
            await session.commit()
            print("committed.")
        else:
            await session.rollback()
            print("rolled back (use --commit to persist).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Rollback the tx.")
    group.add_argument("--commit", action="store_true", help="Commit the tx.")
    args = parser.parse_args()
    return asyncio.run(_run(commit=args.commit))


if __name__ == "__main__":
    sys.exit(main())
```

> **Note for implementer:** If `app/db/session.async_session_factory` does not exist, grep for the project's actual async-session factory (`app/core/db.py` or `app/database.py` are likely candidates) and adjust the import. Leave a `# TODO(rob-269 reviewer):` if the factory contract is unclear.

- [ ] **Step 2: Smoke-run dry-run**

Run: `uv run python -m scripts.snapshot_bundle_smoke --dry-run`
Expected: prints three UUIDs and `rolled back (use --commit to persist).`. Verifies the entire foundation path works against the actual dev DB without persisting.

- [ ] **Step 3: Commit**

```bash
git add scripts/snapshot_bundle_smoke.py
git commit -m "$(cat <<'EOF'
feat(rob-269): local dry-run smoke for snapshot foundation

Phase 1 of ROB-269 — scripts/snapshot_bundle_smoke.py exercises run ->
snapshot -> bundle -> link end-to-end against the dev DB. --dry-run
rolls back; --commit persists. No broker/network mutation.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Final verification

- [ ] **Run the full Phase 1 test slice**

```bash
uv run pytest \
  tests/services/action_report/common/ \
  tests/services/investment_snapshots/ \
  tests/services/test_investment_snapshots_roundtrip.py \
  -v
```

Expected: all green (25 tests roughly — 9 canonicalize + 7 freshness + 6 repository + 2 append_only + 1 roundtrip).

- [ ] **Run lint + type check on changed files**

```bash
uv run ruff check app/models/investment_snapshots.py app/schemas/investment_snapshots.py app/services/investment_snapshots/ app/services/action_report/ scripts/snapshot_bundle_smoke.py tests/services/investment_snapshots/ tests/services/action_report/ tests/services/test_investment_snapshots_roundtrip.py
uv run ruff format --check app/models/investment_snapshots.py app/schemas/investment_snapshots.py app/services/investment_snapshots/ app/services/action_report/ scripts/snapshot_bundle_smoke.py
```

Expected: no errors. If `ruff format --check` complains, run `ruff format <path>` and amend (but **never `--amend`** an already-pushed commit; in Phase 1 nothing is pushed so a fresh commit is preferred per CLAUDE.md).

- [ ] **Verify migration up/down is clean one more time**

```bash
uv run alembic downgrade -1
uv run alembic upgrade head
uv run alembic check
```

Expected: all three succeed without diffs.

- [ ] **Confirm no live calls were introduced**

```bash
grep -rn "httpx\.\|aiohttp\.\|requests\." app/services/investment_snapshots/ app/services/action_report/ scripts/snapshot_bundle_smoke.py || echo "clean"
```

Expected: `clean`.

- [ ] **Confirm append-only public surface**

```bash
uv run pytest tests/services/investment_snapshots/test_append_only.py -v
```

Expected: 2 passed.

---

## Handoff report for Claude reviewer

After all tasks pass, leave a final commit-free summary as a comment block at the end of the most recent commit message, or as plain output:

- Branch name: `rob-269` (already current)
- Commits added by Phase 1 (`git log --oneline ${PHASE1_BASE_SHA}..HEAD`): expected 9 commits, one per task plus any fixup commits explicitly noted.
- Migration filename: `20260519_rob269_add_snapshot_foundation.py`
- Tests run + result: paste the final `pytest` summary line.
- Lint/format/check: paste any non-clean output.
- Local commits made? List SHAs. **No `git push` was attempted** — confirm.
- Open `# TODO(rob-269 reviewer):` notes left in the code: list file + line.
- Decisions deferred to Phase 2+: anything the plan said "do not improvise" that you hit.
- Known risk: any spot where the smoke CLI's import (`async_session_factory`) didn't match the codebase and a workaround was used.

After handoff, **stop**. Do not start Phase 2. Reviewer (Claude) will look at the diff and either approve or request changes.

---

## Post-merge notes (reviewer pass, 2026-05-19)

Two follow-up commits applied after the implementer pass (`5016e257`, plus a docs commit):

1. **`chore(rob-269): ruff cleanup on phase 1 surface`** — 14 cosmetic violations the implementer pass missed (W293/I001/F401/F541/F841). No behaviour change, 25 tests still pass.

2. **Dedup semantic clarification** — see pre-plan §3b-1. The UNIQUE `(canonical_payload_hash, snapshot_kind, market, account_scope)` does *not* include `run_id`, so cross-run dedup returns the **first writer's** row (including its `idempotency_key` and `run_id`). Run-membership for the current caller lives in `investment_snapshot_bundle_items`, not on the snapshot row. The repository docstring on `insert_snapshot` now documents this; the implementer test pass had to randomize fixture symbols to work around a persistent-DB collision, which surfaced the semantic. Phase 2 callers must not assert `snapshot.run_id == my_run.id` as an invariant.

No other follow-ups required. Phase 1 is ready for PR.
