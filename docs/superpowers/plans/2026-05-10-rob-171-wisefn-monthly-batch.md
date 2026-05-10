# ROB-171 — WiseFn KR Earnings Monthly Batch Ingestion (PoC) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Implementer model:** Claude Code Sonnet (per ROB-171 handoff requirement). Reviewer model: Claude Code Opus. If the worker profile cannot enforce these directly, log the actual runtime model used in the PR description.

**Goal:** Add a clean, fixture-tested KR earnings (`source=wisefn`, `category=earnings`, `market=kr`) ingestion path into the existing `market_events` foundation so /invest/calendar can later show forward-looking KR 실적 발표 예정. Ship behind `WISEFN_EARNINGS_ENABLED=false` until the upstream contract is confirmed.

**Architecture:** Reuse the existing `MarketEvent` / `MarketEventValue` / `MarketEventIngestionPartition` tables with no schema changes. Add a new source string `wisefn` to taxonomy, a `wisefn_helpers.py` fetch seam, a `normalize_wisefn_earnings_row` pure-function normalizer, and an `ingest_kr_earnings_wisefn_for_date` orchestrator that follows the dart/forexfactory dependency-injection pattern. CLI gains a `--month YYYY-MM` thin wrapper that expands to the existing per-day `from_date/to_date` loop — no new partition shape. All live fetches are gated behind a `WISEFN_EARNINGS_ENABLED` setting (default `False`); CI / tests stub the seam with inline dict fixtures.

**Tech Stack:** Python 3.13, FastAPI/SQLAlchemy 2.x async, PostgreSQL, pytest + pytest-asyncio, ruff. No new third-party dependencies.

---

## No-New-Table Confirmation

**No new main data table is required.** The repository contract — `app/services/market_events/repository.py::MarketEventsRepository.upsert_event_with_values` — already accepts arbitrary `source` strings and resolves idempotency via either `source_event_id` (partial unique index `ix_market_events_source_event_id_unique`) or the natural key `(source, category, market, symbol, event_date, fiscal_year, fiscal_quarter)`. The `market_event_ingestion_partitions` table is keyed on `(source, category, market, partition_date)` and likewise accepts new `source` values without DDL.

All values stored in `source` / `category` / `market` columns are validated at the application layer by `app/services/market_events/taxonomy.py` — there is no Postgres ENUM or check constraint. Adding `wisefn` is a one-line set membership change. The `MarketEvent` model has no `source` ENUM either (see `app/models/market_events.py:33-112`).

**No Alembic migration is needed for ROB-171.** If a follow-up adds a metric the `market_event_values` schema cannot represent (e.g. cumulative-period EPS), that becomes a separate ticket.

## Why `--month YYYY-MM` as a thin wrapper

Three options were considered:

1. **Pure `--from-date/--to-date`** (zero CLI change). Operators pass `--from-date 2026-05-01 --to-date 2026-05-31`. Lowest blast radius, but the brief explicitly asks for "monthly batch ingestion" semantics and that ergonomics is poor for forward-looking KR earnings (operators must know month boundaries).
2. **First-class `--month`** that *replaces* `--from-date/--to-date`. Larger blast radius — every existing call site must be updated, all CLI tests need to learn a new mode, and the per-day partition logic still requires expansion.
3. **Selected: `--month YYYY-MM` thin wrapper.** Argparse-mutually-exclusive with `--from-date/--to-date`. When `--month` is provided, the parser derives `from_date = first day of month` and `to_date = last day of month` and the rest of the pipeline (per-day `iter_partition_dates` loop, per-day partition rows, per-day fetch+upsert) is unchanged. The "monthly" semantics live entirely in argparse; the data model stays day-grained.

This satisfies the brief's "month mode or equivalent clean monthly partition" with the smallest possible delta to existing source paths (finnhub / dart / forexfactory). Existing CLI tests keep passing untouched.

## File Structure

### Created files

| Path | Responsibility |
| --- | --- |
| `app/services/market_events/wisefn_helpers.py` | Async fetch seam for WiseFn KR earnings calendar. Module-level `_fetch_calendar_payload` raises `NotImplementedError` until contract is confirmed; `fetch_wisefn_earnings_for_date(target_date)` returns `list[dict]` for the orchestrator. |
| `tests/services/test_market_events_wisefn_normalizers.py` | Inline-dict fixture tests for `normalize_wisefn_earnings_row` (in-period, no-symbol skip, scheduled vs. released, fiscal-period parsing). |
| `tests/services/test_market_events_wisefn_helpers.py` | Helper unit test that patches `_fetch_calendar_payload` with a fixture and asserts the returned row dicts shape. |
| `tests/services/test_market_events_wisefn_ingestion.py` | Integration test for `ingest_kr_earnings_wisefn_for_date` using injected `fetch_rows`. Mirrors `test_market_events_ingestion.py::test_ingest_us_earnings_for_date_succeeds`. |

### Modified files

| Path | What changes |
| --- | --- |
| `app/services/market_events/taxonomy.py` | Add `"wisefn"` to `SOURCES`. |
| `app/core/config.py` | Add `wisefn_earnings_enabled: bool = False` to `Settings`. |
| `app/services/market_events/normalizers.py` | Add `normalize_wisefn_earnings_row`. |
| `app/services/market_events/ingestion.py` | Add `ingest_kr_earnings_wisefn_for_date`. |
| `app/services/market_events/expected_sources.py` | Add `("wisefn", "earnings", "kr")` to `EXPECTED_SOURCES` and weekday gate. |
| `scripts/ingest_market_events.py` | Add `wisefn` to `--source` choices, register in `SUPPORTED`, add `--month` flag + `month_to_date_range` helper, add WISEFN_EARNINGS_ENABLED gate. |
| `tests/services/test_market_events_taxonomy.py` | Test `"wisefn" in SOURCES`. |
| `tests/services/test_market_events_expected_sources.py` | Test wisefn weekday inclusion / weekend exclusion. |
| `tests/test_market_events_cli.py` | Test `--month` parse, mutual exclusion with `--from-date/--to-date`, dispatch to wisefn entry, gate on disabled flag. |
| `docs/runbooks/market-events-ingestion.md` | New "WiseFn KR earnings (ROB-171)" section. |

**No DB migration. No router / schema / public API surface changes** — the existing `GET /trading/api/market-events/today,/range` endpoints already expose the new rows via the standard `source=` filter.

---

## Acceptance Checklist

A reviewer should be able to verify the following after the implementer is done:

- [ ] `pytest tests/services/test_market_events_taxonomy.py tests/services/test_market_events_normalizers.py tests/services/test_market_events_wisefn_normalizers.py tests/services/test_market_events_wisefn_helpers.py tests/services/test_market_events_wisefn_ingestion.py tests/services/test_market_events_expected_sources.py tests/test_market_events_cli.py -v` all green.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass with no new violations.
- [ ] `uv run python -m scripts.ingest_market_events --source wisefn --category earnings --market kr --month 2026-05 --dry-run` prints planned partitions for `2026-05-01..2026-05-31` and exits 0 without DB writes.
- [ ] Without `WISEFN_EARNINGS_ENABLED=true`, a non-dry-run wisefn invocation logs a clear `wisefn earnings ingestion disabled` warning and exits 0 (no DB writes, no live HTTP).
- [ ] With `WISEFN_EARNINGS_ENABLED=true` *and* the still-`NotImplementedError` upstream seam, a non-dry-run invocation marks each per-day partition `failed` with `last_error` containing `NotImplementedError` (no event rows written) — proving the upstream contract is the only thing missing for production.
- [ ] Re-running the same fixture-driven ingestion test (within `test_market_events_wisefn_ingestion.py`) twice does **not** create duplicate `market_events` rows, proving idempotency on `source_event_id`.
- [ ] `EXPECTED_SOURCES` includes `("wisefn", "earnings", "kr")` and `expected_sources_for_date(weekend)` excludes it.
- [ ] `docs/runbooks/market-events-ingestion.md` has a WiseFn section that documents: source string, the `WISEFN_EARNINGS_ENABLED` flag default-false posture, the `--month` ergonomics, the fixture-only test policy, and the explicit "live endpoint not yet wired" follow-up.
- [ ] Branch contains no DDL, no broker / order / watch / scheduling code changes, no new `app/routers/` files, no production scheduler activation.

---

## Working Conventions for the Implementer

- Worktree: `/Users/mgh3326/services/auto_trader/worktrees/rob-171-wisefn-calendar`. Do **not** edit `~/auto_trader/current` or `~/work/auto_trader` directly.
- Branch: `rob-171-wisefn-calendar` (already checked out).
- Commit cadence: one commit per task in this plan (frequent commits, no fixup squashes during implementation).
- Commit footer: include `Co-Authored-By: Paperclip <noreply@paperclip.ing>` to match existing repo convention.
- Never invoke any WiseFn / WiseReport URL during tests or CI — patch `_fetch_calendar_payload` with `AsyncMock` / `unittest.mock.patch.object`.
- Do not print secrets. The fixture rows below are synthetic — do not replace with real subscriber data.

---

## Tasks

### Task 1: Add `wisefn` to taxonomy

**Files:**
- Modify: `app/services/market_events/taxonomy.py:34-36`
- Modify: `tests/services/test_market_events_taxonomy.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/test_market_events_taxonomy.py`:

```python
@pytest.mark.unit
def test_sources_includes_wisefn():
    from app.services.market_events.taxonomy import SOURCES

    assert "wisefn" in SOURCES
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/services/test_market_events_taxonomy.py::test_sources_includes_wisefn -v
```

Expected: FAIL with `AssertionError: assert 'wisefn' in frozenset({...})`.

- [ ] **Step 3: Add `wisefn` to SOURCES**

In `app/services/market_events/taxonomy.py`, replace lines 34-36:

```python
SOURCES: frozenset[str] = frozenset(
    {"finnhub", "dart", "upbit", "bithumb", "binance", "token_unlocks", "forexfactory"}
)
```

with:

```python
SOURCES: frozenset[str] = frozenset(
    {
        "finnhub",
        "dart",
        "upbit",
        "bithumb",
        "binance",
        "token_unlocks",
        "forexfactory",
        "wisefn",
    }
)
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/services/test_market_events_taxonomy.py -v
```

Expected: all tests pass, including `test_sources_includes_wisefn`.

- [ ] **Step 5: Commit**

```bash
git add app/services/market_events/taxonomy.py tests/services/test_market_events_taxonomy.py
git commit -m "$(cat <<'EOF'
feat(rob-171): register wisefn source in market_events taxonomy

Adds the 'wisefn' source string used by the upcoming KR earnings monthly batch
ingestion path. No DB / schema changes — the source column is application-validated.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 2: Add `wisefn_earnings_enabled` setting

**Files:**
- Modify: `app/core/config.py:298-308`

- [ ] **Step 1: Inspect the existing settings block**

Read `app/core/config.py` lines 295-320 to confirm the `Settings` class structure (BaseSettings from pydantic).

- [ ] **Step 2: Add the new setting**

Insert after `finnhub_api_key: str | None = None` (around line 304):

```python
    # WiseFn KR earnings calendar (ROB-171)
    # Default False until the upstream contract is confirmed; CI never calls live.
    wisefn_earnings_enabled: bool = False
```

- [ ] **Step 3: Verify import-time stability**

```
uv run python -c "from app.core.config import settings; print('wisefn_earnings_enabled =', settings.wisefn_earnings_enabled)"
```

Expected: prints `wisefn_earnings_enabled = False`.

- [ ] **Step 4: Commit**

```bash
git add app/core/config.py
git commit -m "$(cat <<'EOF'
feat(rob-171): add WISEFN_EARNINGS_ENABLED settings flag (default False)

Gates the new wisefn KR earnings ingestion path. Operational writes are disabled
by default until the upstream contract is confirmed. Tests / CI never depend on
this flag because they inject fetch_rows directly into the orchestrator.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 3: Create the WiseFn fetch helper (NotImplementedError seam)

**Files:**
- Create: `app/services/market_events/wisefn_helpers.py`
- Create: `tests/services/test_market_events_wisefn_helpers.py`

- [ ] **Step 1: Write the failing helper test**

Create `tests/services/test_market_events_wisefn_helpers.py`:

```python
"""WiseFn per-day fetch helper tests (ROB-171, fixture-only)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

# Synthetic minimal payload mirroring the row shape we plan to consume.
SAMPLE_PAYLOAD = {
    "as_of_date": "2026-05-13",
    "items": [
        {
            "stock_code": "005930",
            "corp_name": "삼성전자",
            "release_date": "2026-05-13",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
            "release_type": "scheduled",
            "title": "삼성전자 2026년 1분기 실적발표 예정",
            "time_hint": "after_close",
        },
        {
            "stock_code": "000660",
            "corp_name": "SK하이닉스",
            "release_date": "2026-05-13",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
            "release_type": "scheduled",
            "title": "SK하이닉스 2026년 1분기 실적발표 예정",
            "time_hint": "before_open",
        },
        {
            "stock_code": "005380",
            "corp_name": "현대차",
            "release_date": "2026-05-14",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
            "release_type": "scheduled",
            "title": "현대차 2026년 1분기 실적발표 예정",
            "time_hint": "unknown",
        },
    ],
}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_wisefn_for_date_filters_to_target_day():
    from app.services.market_events import wisefn_helpers as wf

    with patch.object(
        wf,
        "_fetch_calendar_payload",
        AsyncMock(return_value=SAMPLE_PAYLOAD),
    ):
        rows = await wf.fetch_wisefn_earnings_for_date(date(2026, 5, 13))

    assert len(rows) == 2
    assert {r["stock_code"] for r in rows} == {"005930", "000660"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_wisefn_for_date_returns_empty_list_when_no_match():
    from app.services.market_events import wisefn_helpers as wf

    with patch.object(
        wf,
        "_fetch_calendar_payload",
        AsyncMock(return_value=SAMPLE_PAYLOAD),
    ):
        rows = await wf.fetch_wisefn_earnings_for_date(date(2026, 6, 1))

    assert rows == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_default_fetch_calendar_payload_raises_not_implemented():
    """The live fetch is intentionally disabled until upstream contract is confirmed."""
    from app.services.market_events import wisefn_helpers as wf

    with pytest.raises(NotImplementedError):
        await wf._fetch_calendar_payload(date(2026, 5, 13))
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/services/test_market_events_wisefn_helpers.py -v
```

Expected: ImportError / ModuleNotFoundError on `app.services.market_events.wisefn_helpers`.

- [ ] **Step 3: Implement the helper**

Create `app/services/market_events/wisefn_helpers.py`:

```python
"""WiseFn KR earnings calendar fetch helper (ROB-171, fixture-only PoC).

This module exposes a single public coroutine, `fetch_wisefn_earnings_for_date`,
that returns a list of row dicts shaped for `normalize_wisefn_earnings_row`.

The actual upstream HTTP fetch is encapsulated in `_fetch_calendar_payload`,
which is intentionally a `NotImplementedError` seam: the upstream WiseFn /
WiseReport contract has not yet been confirmed, and tests / CI must never call
live. Tests inject fixture payloads via `unittest.mock.patch.object` against
`_fetch_calendar_payload`. Production runs are additionally gated behind
`settings.wisefn_earnings_enabled` in the CLI.

Expected row shape returned by `fetch_wisefn_earnings_for_date`:

    {
        "stock_code": "005930",          # KR 6-digit ticker
        "corp_name": "삼성전자",
        "release_date": "2026-05-13",    # ISO date string
        "fiscal_year": 2026,
        "fiscal_quarter": 1,
        "release_type": "scheduled",     # or "released"
        "title": "삼성전자 2026년 1분기 실적발표 예정",
        "time_hint": "after_close",      # before_open|after_close|during_market|unknown
    }
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


async def _fetch_calendar_payload(target_date: date) -> dict[str, Any]:
    """Fetch the upstream WiseFn calendar payload for `target_date`.

    Module-level seam — tests patch this with `unittest.mock.patch.object`.
    Default raises NotImplementedError; the live wiring is a follow-up that
    requires confirmed upstream contract + permission (see ROB-171 follow-ups
    in docs/runbooks/market-events-ingestion.md).
    """
    raise NotImplementedError(
        "ROB-171: WiseFn calendar endpoint is not wired yet. "
        "Set WISEFN_EARNINGS_ENABLED=false (default) or inject fetch_rows "
        "directly in tests."
    )


def _row_matches_date(row: dict[str, Any], target_date: date) -> bool:
    raw = row.get("release_date") or row.get("date")
    if not raw:
        return False
    try:
        return date.fromisoformat(str(raw)) == target_date
    except ValueError:
        return False


async def fetch_wisefn_earnings_for_date(target_date: date) -> list[dict[str, Any]]:
    """Return WiseFn earnings rows for one calendar day.

    The returned rows are passed through to
    `app.services.market_events.normalizers.normalize_wisefn_earnings_row`.
    """
    payload = await _fetch_calendar_payload(target_date)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        logger.warning(
            "wisefn payload missing 'items' list for %s; got keys=%s",
            target_date,
            list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
        )
        return []
    return [row for row in items if _row_matches_date(row, target_date)]
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/services/test_market_events_wisefn_helpers.py -v
```

Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/services/market_events/wisefn_helpers.py tests/services/test_market_events_wisefn_helpers.py
git commit -m "$(cat <<'EOF'
feat(rob-171): add WiseFn KR earnings fetch helper (NotImplementedError seam)

Introduces `fetch_wisefn_earnings_for_date(target_date)` that filters a payload
to the target day. The upstream HTTP fetch is encapsulated in
`_fetch_calendar_payload`, which is a NotImplementedError seam pending
confirmed upstream contract — tests inject fixtures via patch.object.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 4: Add `normalize_wisefn_earnings_row`

**Files:**
- Modify: `app/services/market_events/normalizers.py:288` (append at end)
- Create: `tests/services/test_market_events_wisefn_normalizers.py`

- [ ] **Step 1: Write the failing normalizer tests**

Create `tests/services/test_market_events_wisefn_normalizers.py`:

```python
"""WiseFn earnings normalizer tests (ROB-171)."""

from __future__ import annotations

from datetime import date

import pytest

WISEFN_ROW_SAMSUNG = {
    "stock_code": "005930",
    "corp_name": "삼성전자",
    "release_date": "2026-05-13",
    "fiscal_year": 2026,
    "fiscal_quarter": 1,
    "release_type": "scheduled",
    "title": "삼성전자 2026년 1분기 실적발표 예정",
    "time_hint": "after_close",
}

WISEFN_ROW_RELEASED = {
    **WISEFN_ROW_SAMSUNG,
    "release_type": "released",
    "title": "삼성전자 2026년 1분기 실적발표",
}


@pytest.mark.unit
def test_normalize_wisefn_basic_fields():
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    event, values = normalize_wisefn_earnings_row(WISEFN_ROW_SAMSUNG)

    assert event["category"] == "earnings"
    assert event["market"] == "kr"
    assert event["country"] == "KR"
    assert event["symbol"] == "005930"
    assert event["company_name"] == "삼성전자"
    assert event["event_date"] == date(2026, 5, 13)
    assert event["time_hint"] == "after_close"
    assert event["source"] == "wisefn"
    assert event["fiscal_year"] == 2026
    assert event["fiscal_quarter"] == 1
    assert event["status"] == "scheduled"
    assert event["source_timezone"] == "Asia/Seoul"
    assert values == []  # Forward-looking schedule has no eps/revenue numbers.


@pytest.mark.unit
def test_normalize_wisefn_released_status():
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    event, _ = normalize_wisefn_earnings_row(WISEFN_ROW_RELEASED)
    assert event["status"] == "released"


@pytest.mark.unit
def test_normalize_wisefn_uses_deterministic_source_event_id():
    """Re-normalizing the same row must yield the same source_event_id (idempotency)."""
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    e1, _ = normalize_wisefn_earnings_row(WISEFN_ROW_SAMSUNG)
    e2, _ = normalize_wisefn_earnings_row(dict(WISEFN_ROW_SAMSUNG))

    assert e1["source_event_id"] == e2["source_event_id"]
    assert e1["source_event_id"] == "wisefn::005930::2026-05-13::2026::1"


@pytest.mark.unit
def test_normalize_wisefn_unknown_time_hint_falls_back():
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    event, _ = normalize_wisefn_earnings_row(
        {**WISEFN_ROW_SAMSUNG, "time_hint": "garbage_value"}
    )
    assert event["time_hint"] == "unknown"


@pytest.mark.unit
def test_normalize_wisefn_missing_stock_code_raises():
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    with pytest.raises(ValueError):
        normalize_wisefn_earnings_row({**WISEFN_ROW_SAMSUNG, "stock_code": ""})


@pytest.mark.unit
def test_normalize_wisefn_missing_release_date_raises():
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    with pytest.raises(ValueError):
        normalize_wisefn_earnings_row({**WISEFN_ROW_SAMSUNG, "release_date": None})


@pytest.mark.unit
def test_normalize_wisefn_non_numeric_stock_code_raises():
    """KR tickers are 6-digit numeric; non-numeric is a row-shape error."""
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    with pytest.raises(ValueError):
        normalize_wisefn_earnings_row(
            {**WISEFN_ROW_SAMSUNG, "stock_code": "BAD-CODE"}
        )


@pytest.mark.unit
def test_normalize_wisefn_payload_is_jsonable():
    """raw_payload_json must be JSONB-safe (no datetime objects)."""
    import json

    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    event, _ = normalize_wisefn_earnings_row(WISEFN_ROW_SAMSUNG)
    json.dumps(event["raw_payload_json"])  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/services/test_market_events_wisefn_normalizers.py -v
```

Expected: ImportError on `normalize_wisefn_earnings_row`.

- [ ] **Step 3: Implement the normalizer**

Append to `app/services/market_events/normalizers.py` (after `normalize_forexfactory_event_row`):

```python


_WISEFN_TIME_HINT_ALLOWED = {"before_open", "after_close", "during_market", "unknown"}
_WISEFN_RELEASE_TYPE_TO_STATUS = {
    "scheduled": "scheduled",
    "released": "released",
    "revised": "revised",
    "cancelled": "cancelled",
    "tentative": "tentative",
}


def _wisefn_source_event_id(
    symbol: str,
    event_date: date,
    fiscal_year: Any,
    fiscal_quarter: Any,
) -> str:
    """Deterministic ID for idempotent upserts on (source, source_event_id)."""
    fy = "" if fiscal_year is None else str(fiscal_year)
    fq = "" if fiscal_quarter is None else str(fiscal_quarter)
    return f"wisefn::{symbol}::{event_date.isoformat()}::{fy}::{fq}"


def normalize_wisefn_earnings_row(
    row: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize one WiseFn KR earnings calendar row to a MarketEvent dict.

    Required fields: stock_code (6-digit numeric), release_date (ISO date),
    corp_name. Optional: fiscal_year, fiscal_quarter, release_type, title,
    time_hint.

    No metric values are produced — WiseFn rows describe the schedule, not
    realized eps/revenue. (Realized values are a follow-up that would join
    DART quarterly filings.)
    """
    stock_code = (row.get("stock_code") or "").strip()
    if not stock_code or not stock_code.isdigit():
        raise ValueError(
            f"wisefn row missing/invalid stock_code (must be numeric): {row.get('stock_code')!r}"
        )

    raw_date = row.get("release_date") or row.get("date")
    if not raw_date:
        raise ValueError("wisefn row missing release_date")
    try:
        event_date = date.fromisoformat(str(raw_date))
    except ValueError as exc:
        raise ValueError(f"wisefn row release_date not ISO: {raw_date!r}") from exc

    corp_name = (row.get("corp_name") or "").strip() or None
    title = (row.get("title") or "").strip() or None
    fiscal_year = row.get("fiscal_year")
    fiscal_quarter = row.get("fiscal_quarter")

    raw_hint = (row.get("time_hint") or "").strip().lower()
    time_hint = raw_hint if raw_hint in _WISEFN_TIME_HINT_ALLOWED else "unknown"

    raw_status = (row.get("release_type") or "").strip().lower()
    status = _WISEFN_RELEASE_TYPE_TO_STATUS.get(raw_status, "scheduled")

    source_event_id = _wisefn_source_event_id(
        stock_code, event_date, fiscal_year, fiscal_quarter
    )

    event = {
        "category": "earnings",
        "market": "kr",
        "country": "KR",
        "symbol": stock_code,
        "company_name": corp_name,
        "title": title,
        "event_date": event_date,
        "release_time_utc": None,
        "release_time_local": None,
        "source_timezone": "Asia/Seoul",
        "time_hint": time_hint,
        "importance": None,
        "status": status,
        "source": "wisefn",
        "source_event_id": source_event_id,
        "source_url": None,
        "fiscal_year": fiscal_year,
        "fiscal_quarter": fiscal_quarter,
        "raw_payload_json": _row_to_jsonable(row),
    }
    return event, []
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/services/test_market_events_wisefn_normalizers.py -v
```

Expected: 8 PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/services/market_events/normalizers.py tests/services/test_market_events_wisefn_normalizers.py
git commit -m "$(cat <<'EOF'
feat(rob-171): add normalize_wisefn_earnings_row pure-function normalizer

Maps a WiseFn KR earnings calendar row to a MarketEvent dict (no metric values
yet — WiseFn rows are schedules, not realized numbers). Idempotency is keyed
by a deterministic source_event_id of the form
"wisefn::{stock_code}::{event_date}::{fy}::{fq}".

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 5: Add `ingest_kr_earnings_wisefn_for_date` orchestrator

**Files:**
- Modify: `app/services/market_events/ingestion.py:178` (append after `ingest_kr_disclosures_for_date`, before `ingest_economic_events_for_date`)
- Create: `tests/services/test_market_events_wisefn_ingestion.py`

- [ ] **Step 1: Write the failing integration test**

Create `tests/services/test_market_events_wisefn_ingestion.py`:

```python
"""WiseFn KR earnings ingestion orchestrator tests (ROB-171)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture(autouse=True)
async def _clean_market_events(db_session):
    from app.models.market_events import (
        MarketEvent,
        MarketEventIngestionPartition,
        MarketEventValue,
    )

    await db_session.execute(delete(MarketEventValue))
    await db_session.execute(delete(MarketEvent))
    await db_session.execute(delete(MarketEventIngestionPartition))
    await db_session.commit()
    yield


WISEFN_ROW = {
    "stock_code": "005930",
    "corp_name": "삼성전자",
    "release_date": "2026-05-13",
    "fiscal_year": 2026,
    "fiscal_quarter": 1,
    "release_type": "scheduled",
    "title": "삼성전자 2026년 1분기 실적발표 예정",
    "time_hint": "after_close",
}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_wisefn_succeeds_with_injected_rows(db_session):
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition
    from app.services.market_events import ingestion

    fake = AsyncMock(return_value=[WISEFN_ROW])
    result = await ingestion.ingest_kr_earnings_wisefn_for_date(
        db_session, date(2026, 5, 13), fetch_rows=fake
    )
    await db_session.commit()

    assert result.status == "succeeded"
    assert result.event_count == 1
    fake.assert_awaited_once_with(date(2026, 5, 13))

    events = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].symbol == "005930"
    assert events[0].source == "wisefn"
    assert events[0].category == "earnings"
    assert events[0].market == "kr"
    assert events[0].source_event_id == "wisefn::005930::2026-05-13::2026::1"

    parts = (
        (await db_session.execute(select(MarketEventIngestionPartition))).scalars().all()
    )
    assert len(parts) == 1
    assert parts[0].source == "wisefn"
    assert parts[0].status == "succeeded"
    assert parts[0].event_count == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_wisefn_is_idempotent_on_repeat(db_session):
    from app.models.market_events import MarketEvent
    from app.services.market_events import ingestion

    fake = AsyncMock(return_value=[WISEFN_ROW])
    await ingestion.ingest_kr_earnings_wisefn_for_date(
        db_session, date(2026, 5, 13), fetch_rows=fake
    )
    await db_session.commit()
    await ingestion.ingest_kr_earnings_wisefn_for_date(
        db_session, date(2026, 5, 13), fetch_rows=fake
    )
    await db_session.commit()

    events = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(events) == 1, "repeat ingestion must upsert, not duplicate"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_wisefn_marks_failed_on_fetch_error(db_session):
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition
    from app.services.market_events import ingestion

    async def boom(_d):
        raise NotImplementedError("contract not wired")

    result = await ingestion.ingest_kr_earnings_wisefn_for_date(
        db_session, date(2026, 5, 13), fetch_rows=boom
    )
    await db_session.commit()

    assert result.status == "failed"
    assert "NotImplementedError" in (result.error or "") or "contract" in (
        result.error or ""
    )

    events = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert events == []
    parts = (
        (await db_session.execute(select(MarketEventIngestionPartition))).scalars().all()
    )
    assert parts[0].status == "failed"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_wisefn_default_fetch_uses_helper(db_session, monkeypatch):
    """When fetch_rows is None, the orchestrator wires fetch_wisefn_earnings_for_date."""
    from app.services.market_events import ingestion, wisefn_helpers

    captured = {}

    async def stub(target_date):
        captured["called"] = target_date
        return []

    monkeypatch.setattr(
        wisefn_helpers, "fetch_wisefn_earnings_for_date", stub
    )

    result = await ingestion.ingest_kr_earnings_wisefn_for_date(
        db_session, date(2026, 5, 13), fetch_rows=None
    )
    await db_session.commit()

    assert captured == {"called": date(2026, 5, 13)}
    assert result.status == "succeeded"
    assert result.event_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/services/test_market_events_wisefn_ingestion.py -v
```

Expected: AttributeError on `ingestion.ingest_kr_earnings_wisefn_for_date`.

- [ ] **Step 3: Implement the orchestrator**

Insert into `app/services/market_events/ingestion.py` after the existing `ingest_kr_disclosures_for_date` (around line 177, before `ingest_economic_events_for_date`):

```python


async def ingest_kr_earnings_wisefn_for_date(
    db: AsyncSession,
    target_date: date,
    fetch_rows: Callable[[date], Awaitable[list[dict[str, Any]]]] | None = None,
) -> IngestionRunResult:
    """Ingest WiseFn KR earnings rows for one day (ROB-171).

    `fetch_rows` is an optional injection point. Default uses
    `app.services.market_events.wisefn_helpers.fetch_wisefn_earnings_for_date`,
    which currently raises NotImplementedError until the upstream contract is
    confirmed. Tests inject a fixture-returning AsyncMock.
    """
    if fetch_rows is None:
        from app.services.market_events.wisefn_helpers import (
            fetch_wisefn_earnings_for_date as _default_fetch,
        )

        fetch_rows = _default_fetch

    source = "wisefn"
    category = "earnings"
    market = "kr"
    repo = MarketEventsRepository(db)
    partition = await repo.get_or_create_partition(
        source=source,
        category=category,
        market=market,
        partition_date=target_date,
    )
    await repo.mark_partition_running(partition)

    try:
        from app.services.market_events.normalizers import (
            normalize_wisefn_earnings_row,
        )

        rows = await fetch_rows(target_date)
        upserted = 0
        for row in rows:
            try:
                event_dict, value_dicts = normalize_wisefn_earnings_row(row)
            except ValueError as exc:
                logger.warning("skipping unparseable wisefn row: %s (%s)", row, exc)
                continue
            await repo.upsert_event_with_values(event_dict, value_dicts)
            upserted += 1

        await repo.mark_partition_succeeded(partition, event_count=upserted)
        return IngestionRunResult(
            source=source,
            category=category,
            market=market,
            partition_date=target_date,
            status="succeeded",
            event_count=upserted,
        )
    except Exception as exc:
        logger.exception("wisefn earnings ingestion failed for %s", target_date)
        return await _mark_failed_after_exception(
            db,
            source=source,
            category=category,
            market=market,
            partition_date=target_date,
            error=exc,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/services/test_market_events_wisefn_ingestion.py -v
```

Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/services/market_events/ingestion.py tests/services/test_market_events_wisefn_ingestion.py
git commit -m "$(cat <<'EOF'
feat(rob-171): add ingest_kr_earnings_wisefn_for_date orchestrator

Mirrors the dart/forexfactory pattern: claim partition → fetch rows
(injection point) → normalize → upsert → mark succeeded/failed. Default
fetch resolves to wisefn_helpers.fetch_wisefn_earnings_for_date, which
itself raises NotImplementedError until upstream contract is confirmed —
production ingestion is gated by WISEFN_EARNINGS_ENABLED.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 6: Register WiseFn in CLI + add `--month YYYY-MM` wrapper + enable gate

**Files:**
- Modify: `scripts/ingest_market_events.py`
- Modify: `tests/test_market_events_cli.py`

- [ ] **Step 1: Write the failing CLI tests**

Append to `tests/test_market_events_cli.py`:

```python
@pytest.mark.unit
def test_parse_args_month_expands_to_first_and_last_day():
    from scripts.ingest_market_events import parse_args

    ns = parse_args(
        ["--source", "wisefn", "--category", "earnings", "--market", "kr",
         "--month", "2026-05"]
    )
    assert ns.from_date == date(2026, 5, 1)
    assert ns.to_date == date(2026, 5, 31)


@pytest.mark.unit
def test_parse_args_month_february_leap_year_2024():
    from scripts.ingest_market_events import parse_args

    ns = parse_args(
        ["--source", "wisefn", "--category", "earnings", "--market", "kr",
         "--month", "2024-02"]
    )
    assert ns.from_date == date(2024, 2, 1)
    assert ns.to_date == date(2024, 2, 29)


@pytest.mark.unit
def test_parse_args_month_february_non_leap():
    from scripts.ingest_market_events import parse_args

    ns = parse_args(
        ["--source", "wisefn", "--category", "earnings", "--market", "kr",
         "--month", "2026-02"]
    )
    assert ns.to_date == date(2026, 2, 28)


@pytest.mark.unit
def test_parse_args_month_and_from_date_are_mutually_exclusive():
    from scripts.ingest_market_events import parse_args

    with pytest.raises(SystemExit):
        parse_args(
            ["--source", "wisefn", "--category", "earnings", "--market", "kr",
             "--month", "2026-05",
             "--from-date", "2026-05-01"]
        )


@pytest.mark.unit
def test_parse_args_requires_month_or_date_range():
    from scripts.ingest_market_events import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--source", "wisefn", "--category", "earnings", "--market", "kr"])


@pytest.mark.unit
def test_parse_args_accepts_wisefn_source():
    from scripts.ingest_market_events import parse_args

    ns = parse_args(
        ["--source", "wisefn", "--category", "earnings", "--market", "kr",
         "--from-date", "2026-05-01", "--to-date", "2026-05-01"]
    )
    assert ns.source == "wisefn"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_run_ingest_skips_wisefn_when_flag_disabled(db_session, monkeypatch, caplog):
    from app.core import config as config_mod
    from scripts import ingest_market_events as cli

    monkeypatch.setattr(config_mod.settings, "wisefn_earnings_enabled", False)

    fake = AsyncMock()
    monkeypatch.setitem(cli.SUPPORTED, ("wisefn", "earnings", "kr"), fake)

    rc = await cli.run_ingest(
        db=db_session,
        source="wisefn",
        category="earnings",
        market="kr",
        from_date=date(2026, 5, 1),
        to_date=date(2026, 5, 1),
        dry_run=False,
    )

    assert rc == 0
    fake.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_run_ingest_calls_wisefn_when_flag_enabled(db_session, monkeypatch):
    from app.core import config as config_mod
    from scripts import ingest_market_events as cli

    monkeypatch.setattr(config_mod.settings, "wisefn_earnings_enabled", True)

    fake = AsyncMock(
        return_value=type("R", (), {"status": "succeeded", "event_count": 0})()
    )
    monkeypatch.setitem(cli.SUPPORTED, ("wisefn", "earnings", "kr"), fake)

    rc = await cli.run_ingest(
        db=db_session,
        source="wisefn",
        category="earnings",
        market="kr",
        from_date=date(2026, 5, 1),
        to_date=date(2026, 5, 2),
        dry_run=False,
    )

    assert rc == 0
    assert fake.await_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_market_events_cli.py -v
```

Expected: failures on the new tests (e.g. unsupported `wisefn` source choice, missing `--month`).

- [ ] **Step 3: Update the CLI module**

Replace the entire `scripts/ingest_market_events.py` body with the version below (keeping the existing docstring intent and adding `--month` + wisefn dispatch + gate):

```python
#!/usr/bin/env python3
"""Per-day market events ingestion CLI (ROB-128, ROB-132, ROB-171).

Examples:
    # US earnings, explicit range
    python -m scripts.ingest_market_events \\
        --source finnhub --category earnings --market us \\
        --from-date 2026-05-07 --to-date 2026-05-14

    # KR DART disclosures, single day
    python -m scripts.ingest_market_events \\
        --source dart --category disclosure --market kr \\
        --from-date 2026-05-07 --to-date 2026-05-07

    # KR earnings via WiseFn, whole month (ROB-171)
    python -m scripts.ingest_market_events \\
        --source wisefn --category earnings --market kr \\
        --month 2026-05 --dry-run

`--month YYYY-MM` is a thin wrapper that expands to
`--from-date <first day of month> --to-date <last day of month>` and is
mutually exclusive with `--from-date/--to-date`. The pipeline still loops
per-day partitions internally — the `MarketEventIngestionPartition` shape is
unchanged.

Operational gates:
* `wisefn` is only invoked when `settings.wisefn_earnings_enabled` is True.
  Otherwise the CLI logs a warning and exits 0 without DB writes.

Recommended rolling window for later Prefect schedule:
    today - 7 days through today + 60 days
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import logging
import re
from collections.abc import Iterator
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cli import setup_logging_and_sentry
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.monitoring.sentry import capture_exception
from app.services.market_events.ingestion import (
    ingest_economic_events_for_date,
    ingest_kr_disclosures_for_date,
    ingest_kr_earnings_wisefn_for_date,
    ingest_us_earnings_for_date,
)

logger = logging.getLogger(__name__)


SUPPORTED = {
    ("finnhub", "earnings", "us"): ingest_us_earnings_for_date,
    ("dart", "disclosure", "kr"): ingest_kr_disclosures_for_date,
    ("forexfactory", "economic", "global"): ingest_economic_events_for_date,
    ("wisefn", "earnings", "kr"): ingest_kr_earnings_wisefn_for_date,
}


_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def iter_partition_dates(from_date: date, to_date: date) -> Iterator[date]:
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    cur = from_date
    while cur <= to_date:
        yield cur
        cur += timedelta(days=1)


def month_to_date_range(month: str) -> tuple[date, date]:
    """Expand 'YYYY-MM' to (first_day, last_day) inclusive."""
    m = _MONTH_RE.match(month)
    if not m:
        raise ValueError(f"--month must be YYYY-MM, got {month!r}")
    year = int(m.group(1))
    mo = int(m.group(2))
    if not 1 <= mo <= 12:
        raise ValueError(f"--month month component out of range: {month!r}")
    last_day = calendar.monthrange(year, mo)[1]
    return date(year, mo, 1), date(year, mo, last_day)


def _parse_iso(value: str) -> date:
    return date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Per-day market events ingestion CLI (ROB-128 / 132 / 171)."
    )
    parser.add_argument(
        "--source",
        default="finnhub",
        choices=["finnhub", "dart", "forexfactory", "wisefn"],
    )
    parser.add_argument(
        "--category",
        default="earnings",
        choices=["earnings", "disclosure", "economic"],
    )
    parser.add_argument(
        "--market",
        default="us",
        choices=["us", "kr", "global"],
    )

    range_group = parser.add_mutually_exclusive_group(required=True)
    range_group.add_argument(
        "--month",
        type=str,
        default=None,
        help="Whole-month batch as YYYY-MM. Expands to first..last day of month.",
    )
    range_group.add_argument(
        "--from-date",
        type=_parse_iso,
        dest="from_date",
        help="ISO start date (inclusive). Requires --to-date.",
    )

    parser.add_argument(
        "--to-date",
        type=_parse_iso,
        dest="to_date",
        help="ISO end date (inclusive). Required when --from-date is used.",
    )
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    ns = parser.parse_args(argv)

    if ns.month is not None:
        if ns.to_date is not None:
            parser.error("--month is mutually exclusive with --to-date")
        try:
            ns.from_date, ns.to_date = month_to_date_range(ns.month)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        if ns.to_date is None:
            parser.error("--to-date is required when --from-date is used")

    key = (ns.source, ns.category, ns.market)
    if key not in SUPPORTED:
        parser.error(
            f"unsupported source/category/market combination: {key}. "
            f"supported: {sorted(SUPPORTED.keys())}"
        )
    return ns


def _is_source_enabled(source: str, category: str, market: str) -> tuple[bool, str | None]:
    """Return (enabled, reason_when_disabled) for a configured source."""
    if (source, category, market) == ("wisefn", "earnings", "kr"):
        if not settings.wisefn_earnings_enabled:
            return False, (
                "wisefn earnings ingestion disabled "
                "(set WISEFN_EARNINGS_ENABLED=true to enable)"
            )
    return True, None


async def run_ingest(
    *,
    db: AsyncSession,
    source: str,
    category: str,
    market: str,
    from_date: date,
    to_date: date,
    dry_run: bool,
) -> int:
    enabled, reason = _is_source_enabled(source, category, market)
    if not enabled and not dry_run:
        logger.warning("%s; skipping run for %s..%s", reason, from_date, to_date)
        return 0

    fn = SUPPORTED[(source, category, market)]
    succeeded = 0
    failed = 0
    for d in iter_partition_dates(from_date, to_date):
        if dry_run:
            logger.info(
                "[DRY-RUN] would ingest %s/%s/%s for %s", source, category, market, d
            )
            succeeded += 1
            continue
        result = await fn(db, d)
        await db.commit()
        if result.status == "succeeded":
            succeeded += 1
            logger.info(
                "ingested %s events for %s/%s/%s on %s",
                result.event_count,
                source,
                category,
                market,
                d,
            )
        else:
            failed += 1
            logger.error(
                "ingest failed for %s/%s/%s on %s: %s",
                source,
                category,
                market,
                d,
                result.error,
            )
    summary = {
        "source": source,
        "category": category,
        "market": market,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "dry_run": dry_run,
        "succeeded": succeeded,
        "failed": failed,
    }
    import json as _json

    print(_json.dumps(summary))
    logger.info("ingest complete: %s", summary)
    return 0 if failed == 0 else 2


async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="market-events-ingest")
    ns = parse_args(argv)

    try:
        async with AsyncSessionLocal() as db:
            return await run_ingest(
                db=db,
                source=ns.source,
                category=ns.category,
                market=ns.market,
                from_date=ns.from_date,
                to_date=ns.to_date,
                dry_run=ns.dry_run,
            )
    except Exception as exc:
        capture_exception(exc, process="ingest_market_events")
        logger.error("ingest_market_events crashed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 4: Run all CLI tests to verify they pass**

```
uv run pytest tests/test_market_events_cli.py -v
```

Expected: all pre-existing tests still pass plus the 8 new ones.

- [ ] **Step 5: Manual dry-run sanity check**

```
uv run python -m scripts.ingest_market_events --source wisefn --category earnings --market kr --month 2026-05 --dry-run
```

Expected: 31 lines of `[DRY-RUN] would ingest wisefn/earnings/kr for 2026-05-DD`, then a JSON summary with `"succeeded": 31, "failed": 0`, and exit code 0. (`echo $?` → 0.)

- [ ] **Step 6: Commit**

```bash
git add scripts/ingest_market_events.py tests/test_market_events_cli.py
git commit -m "$(cat <<'EOF'
feat(rob-171): wisefn CLI dispatch + --month wrapper + WISEFN_EARNINGS_ENABLED gate

Adds 'wisefn' to the --source choices and dispatch table, introduces a
--month YYYY-MM thin wrapper (mutually exclusive with --from-date/--to-date)
that expands to first..last day of the month, and gates non-dry-run wisefn
runs behind settings.wisefn_earnings_enabled. The per-day partition shape is
unchanged — monthly semantics live entirely in argparse.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 7: Register WiseFn in expected_sources / freshness

**Files:**
- Modify: `app/services/market_events/expected_sources.py`
- Modify: `tests/services/test_market_events_expected_sources.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_market_events_expected_sources.py`:

```python
@pytest.mark.unit
def test_expected_sources_includes_wisefn_constant():
    assert ("wisefn", "earnings", "kr") in EXPECTED_SOURCES


@pytest.mark.unit
def test_expected_sources_includes_wisefn_on_kr_weekday():
    triples = expected_sources_for_date(date(2026, 5, 11))  # Monday
    assert ("wisefn", "earnings", "kr") in triples


@pytest.mark.unit
def test_expected_sources_drops_wisefn_on_kr_weekend():
    triples = expected_sources_for_date(date(2026, 5, 10))  # Sunday
    assert ("wisefn", "earnings", "kr") not in triples
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/services/test_market_events_expected_sources.py -v
```

Expected: 3 failures on the new tests.

- [ ] **Step 3: Add WiseFn to the registry and weekday gate**

Update `app/services/market_events/expected_sources.py`:

Replace lines 26-32 (the EXPECTED_SOURCES constant):

```python
EXPECTED_SOURCES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("finnhub", "earnings", "us"),
        ("dart", "disclosure", "kr"),
        ("forexfactory", "economic", "global"),
    }
)
```

with:

```python
EXPECTED_SOURCES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("finnhub", "earnings", "us"),
        ("dart", "disclosure", "kr"),
        ("forexfactory", "economic", "global"),
        ("wisefn", "earnings", "kr"),
    }
)
```

Replace lines 35-47 (the `expected_sources_for_date` function body):

```python
def expected_sources_for_date(target_date: date) -> frozenset[tuple[str, str, str]]:
    """Return the subset of EXPECTED_SOURCES expected to have non-empty data on `target_date`.

    Saturday = 5, Sunday = 6 in `date.weekday()`.
    """
    weekday = target_date.weekday()
    is_weekend = weekday >= 5

    triples: set[tuple[str, str, str]] = {("forexfactory", "economic", "global")}
    if not is_weekend:
        triples.add(("finnhub", "earnings", "us"))
        triples.add(("dart", "disclosure", "kr"))
    return frozenset(triples)
```

with:

```python
def expected_sources_for_date(target_date: date) -> frozenset[tuple[str, str, str]]:
    """Return the subset of EXPECTED_SOURCES expected to have non-empty data on `target_date`.

    Saturday = 5, Sunday = 6 in `date.weekday()`.
    """
    weekday = target_date.weekday()
    is_weekend = weekday >= 5

    triples: set[tuple[str, str, str]] = {("forexfactory", "economic", "global")}
    if not is_weekend:
        triples.add(("finnhub", "earnings", "us"))
        triples.add(("dart", "disclosure", "kr"))
        triples.add(("wisefn", "earnings", "kr"))
    return frozenset(triples)
```

- [ ] **Step 4: Update the module docstring**

Append to the module docstring (after the ForexFactory bullet, lines 17-18):

```python
* WiseFn KR earnings (ROB-171) is a forward-looking schedule source; we expect
  it on KR weekdays only, matching DART. The default fetcher raises
  NotImplementedError until the upstream contract is confirmed, so freshness
  for `(wisefn, earnings, kr)` will surface "expected but failed" until the
  helper is wired and `WISEFN_EARNINGS_ENABLED=true` is set.
```

- [ ] **Step 5: Run tests to verify they pass**

```
uv run pytest tests/services/test_market_events_expected_sources.py -v
```

Expected: all PASSED (existing 4 + new 3).

- [ ] **Step 6: Commit**

```bash
git add app/services/market_events/expected_sources.py tests/services/test_market_events_expected_sources.py
git commit -m "$(cat <<'EOF'
feat(rob-171): include wisefn KR earnings in expected_sources freshness registry

Adds (wisefn, earnings, kr) to EXPECTED_SOURCES with the same weekday gate
applied to DART. While the upstream contract is not wired, freshness will
surface this as 'expected but failed' — exactly the diagnostic surface ROB-167
introduced.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 8: Runbook update

**Files:**
- Modify: `docs/runbooks/market-events-ingestion.md`

- [ ] **Step 1: Append a new section after the ForexFactory section**

Insert at the end of `docs/runbooks/market-events-ingestion.md` (after the existing "Open follow-ups specific to economic events" section):

```markdown

## KR earnings (WiseFn, ROB-171)

WiseFn / WiseReport publishes a forward-looking KR 실적 발표 예정 schedule. We
ingest it as `(source=wisefn, category=earnings, market=kr)` and store rows in
the existing `market_events` table (no new tables, no DDL).

> **Posture:** PoC. Ships behind `WISEFN_EARNINGS_ENABLED=false` until the
> upstream contract is confirmed. CI / tests **never** call live; the fetch
> seam (`_fetch_calendar_payload`) raises `NotImplementedError` by default and
> tests inject fixture rows via `unittest.mock.patch.object` /
> `fetch_rows=AsyncMock(...)`.

### CLI

```bash
# Whole-month dry run (no DB writes, no live HTTP)
uv run python -m scripts.ingest_market_events \
  --source wisefn --category earnings --market kr \
  --month 2026-05 --dry-run

# Equivalent explicit range (still works)
uv run python -m scripts.ingest_market_events \
  --source wisefn --category earnings --market kr \
  --from-date 2026-05-01 --to-date 2026-05-31 --dry-run
```

`--month YYYY-MM` is a thin wrapper that expands to first..last day of the
month and is mutually exclusive with `--from-date/--to-date`. The per-day
`MarketEventIngestionPartition` shape is unchanged — monthly semantics live
entirely in the CLI.

When `WISEFN_EARNINGS_ENABLED` is unset / false, non-dry-run runs log a
warning and exit 0 without touching the DB.

### Idempotency

`source_event_id` is a deterministic string of the form

```
wisefn::{stock_code}::{event_date_iso}::{fiscal_year}::{fiscal_quarter}
```

so repeated ingestion of the same scheduled release upserts on
`(source, category, market, source_event_id)` (partial unique index).
Re-running a month is safe — you should see partition rows flip
`pending → running → succeeded` and event rows update in place.

### Values

WiseFn rows describe the **schedule**, not realized eps/revenue. The
normalizer therefore returns an empty `value_dicts` list. Joining DART
quarterly filings to populate realized values is a follow-up.

### Env vars

| Var | Purpose | Default |
| --- | --- | --- |
| `WISEFN_EARNINGS_ENABLED` | Gate non-dry-run wisefn invocations | `false` |

No API key is consumed yet — the upstream client is not wired.

### Tests

```bash
uv run pytest tests/services/test_market_events_wisefn_normalizers.py -v
uv run pytest tests/services/test_market_events_wisefn_helpers.py -v
uv run pytest tests/services/test_market_events_wisefn_ingestion.py -v
uv run pytest tests/test_market_events_cli.py -v
```

### Follow-ups specific to ROB-171

1. **Upstream contract**: confirm WiseFn / WiseReport endpoint, auth posture,
   per-row schema, and ToS / scraping permissions. Replace the
   `_fetch_calendar_payload` `NotImplementedError` with a real `httpx.AsyncClient`
   call in `app/services/market_events/wisefn_helpers.py`. Pin the upstream
   schema in a docstring so future drift is caught by `normalize_wisefn_earnings_row`.
2. **Realized eps/revenue join**: once a quarterly DART filing arrives, link
   it to the `wisefn` schedule row (probably via `(symbol, fiscal_year, fiscal_quarter)`)
   so the realized numbers populate `market_event_values`. Currently the schedule
   row is informational only.
3. **Prefect deployment**: a monthly Prefect flow that calls
   `scripts.ingest_market_events.run_ingest` for the next two months at a low
   weekly cadence is the natural cadence (the schedule rarely changes mid-month).
4. **UI surface**: `/invest/calendar` already consumes
   `GET /trading/api/market-events/range`. Once `WISEFN_EARNINGS_ENABLED=true`
   in production, KR earnings will appear automatically — no UI change needed.
```

- [ ] **Step 2: Verify rendering**

```
uv run python -c "import pathlib; print(pathlib.Path('docs/runbooks/market-events-ingestion.md').read_text().count('WiseFn'))"
```

Expected: prints a positive integer (≥ 5).

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/market-events-ingestion.md
git commit -m "$(cat <<'EOF'
docs(rob-171): document wisefn KR earnings ingestion path + --month CLI

Adds a 'KR earnings (WiseFn, ROB-171)' section covering CLI usage, idempotency
strategy, value-row policy (empty for forward-looking schedules), env vars,
tests, and ROB-171-specific follow-ups (upstream contract, realized-value
join, Prefect cadence, UI surface).

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 9: Final repo-wide verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full market_events test surface**

```
uv run pytest tests/services/test_market_events_models.py tests/services/test_market_events_taxonomy.py tests/services/test_market_events_normalizers.py tests/services/test_market_events_wisefn_normalizers.py tests/services/test_market_events_wisefn_helpers.py tests/services/test_market_events_wisefn_ingestion.py tests/services/test_market_events_repository.py tests/services/test_market_events_ingestion.py tests/services/test_market_events_query_service.py tests/services/test_market_events_expected_sources.py tests/services/test_market_events_freshness_service.py tests/services/test_market_events_schemas.py tests/test_market_events_router.py tests/test_market_events_cli.py -v
```

Expected: all green. Specifically watch for:
- `test_sources_includes_wisefn` PASS
- `test_normalize_wisefn_*` PASS (8 cases)
- `test_fetch_wisefn_*` PASS (3 cases)
- `test_ingest_wisefn_*` PASS (4 cases)
- `test_expected_sources_includes_wisefn_*` PASS (3 cases)
- `test_parse_args_month_*` PASS (3 cases)
- `test_run_ingest_skips_wisefn_when_flag_disabled` / `..._calls_wisefn_when_flag_enabled` PASS

- [ ] **Step 2: Run lint + format check**

```
uv run ruff check app/services/market_events/ scripts/ingest_market_events.py tests/services/test_market_events_wisefn_normalizers.py tests/services/test_market_events_wisefn_helpers.py tests/services/test_market_events_wisefn_ingestion.py
uv run ruff format --check app/services/market_events/ scripts/ingest_market_events.py tests/services/test_market_events_wisefn_normalizers.py tests/services/test_market_events_wisefn_helpers.py tests/services/test_market_events_wisefn_ingestion.py
```

Expected: no violations. If `ruff format --check` fails, run `uv run ruff format <files>` then re-verify.

- [ ] **Step 3: Confirm no broker / order / watch / scheduling files were modified**

```
git diff --name-only main...HEAD
```

Expected output (and **only** these files):

```
app/core/config.py
app/services/market_events/expected_sources.py
app/services/market_events/ingestion.py
app/services/market_events/normalizers.py
app/services/market_events/taxonomy.py
app/services/market_events/wisefn_helpers.py
docs/runbooks/market-events-ingestion.md
docs/superpowers/plans/2026-05-10-rob-171-wisefn-monthly-batch.md
scripts/ingest_market_events.py
tests/services/test_market_events_expected_sources.py
tests/services/test_market_events_taxonomy.py
tests/services/test_market_events_wisefn_helpers.py
tests/services/test_market_events_wisefn_ingestion.py
tests/services/test_market_events_wisefn_normalizers.py
tests/test_market_events_cli.py
```

If anything else appears (e.g. `app/routers/`, `alembic/versions/`, `app/services/kis_*`, `app/services/upbit_*`, `app/services/alpaca_*`), revert it before opening the PR.

- [ ] **Step 4: Live-call audit**

```
uv run grep -n -e 'httpx.AsyncClient' -e 'requests.get' -e 'aiohttp' app/services/market_events/wisefn_helpers.py
```

Expected: no matches (the helper has only the NotImplementedError seam — no live HTTP code).

- [ ] **Step 5: Push branch and open PR**

```
git push -u origin rob-171-wisefn-calendar
gh pr create --base main --title "feat(rob-171): wisefn KR earnings monthly batch (PoC, gated)" --body "$(cat <<'EOF'
## Summary

Adds the `wisefn` (KR 실적 발표 예정) source to the existing `market_events`
ingestion foundation. No new tables, no DDL, no broker / order / watch
mutations, no production scheduler activation.

- Source string `wisefn` registered in taxonomy + expected_sources.
- New normalizer `normalize_wisefn_earnings_row` (deterministic source_event_id).
- New orchestrator `ingest_kr_earnings_wisefn_for_date` (DI-friendly fetch_rows).
- New helper `app/services/market_events/wisefn_helpers.py` with a
  `NotImplementedError` seam — live upstream wiring is a follow-up.
- CLI gains `--month YYYY-MM` (thin wrapper to first..last day) and a
  `WISEFN_EARNINGS_ENABLED` gate; non-dry-run wisefn runs are no-ops by default.
- Runbook section + freshness/expected-source coverage.

## Test plan

- [ ] `uv run pytest tests/services/test_market_events_wisefn_normalizers.py tests/services/test_market_events_wisefn_helpers.py tests/services/test_market_events_wisefn_ingestion.py tests/services/test_market_events_expected_sources.py tests/services/test_market_events_taxonomy.py tests/test_market_events_cli.py -v`
- [ ] `uv run python -m scripts.ingest_market_events --source wisefn --category earnings --market kr --month 2026-05 --dry-run` exits 0
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean
- [ ] No live WiseFn / WiseReport HTTP calls were made; tests inject fixtures via `patch.object` and `fetch_rows=AsyncMock(...)`

## Safety boundaries honored

- Worktree-only changes (no edits in `current/` or `~/work/auto_trader`).
- No broker / order / watch / order-intent / live-trading mutations.
- No production scheduler activation; no recurring source collection.
- No production DB updates / backfills.
- No secrets printed.
- WiseFn live calls are gated behind `WISEFN_EARNINGS_ENABLED=false` (default).
- Implementer model used at runtime: <FILL IN — Claude Code Sonnet expected>.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Stop and wait for review. Do **not** merge. Do **not** flip `WISEFN_EARNINGS_ENABLED=true` anywhere.

---

## Risks / Unknowns

1. **Upstream contract**: the WiseFn / WiseReport row shape used in fixtures (`stock_code`, `release_date`, `fiscal_year`, `fiscal_quarter`, `release_type`, `time_hint`) is *speculative*. When the live contract is confirmed, the implementer of the follow-up ticket should re-validate `normalize_wisefn_earnings_row` against a real captured payload and adjust required-vs-optional handling. The tests intentionally pin the speculative contract so the failure mode is loud, not silent.
2. **Symbol format**: KR symbols in this repo are 6-digit strings (e.g. `005930`). The normalizer enforces `stock_code.isdigit()`. If WiseFn ever returns suffixed codes (e.g. `005930.KS`), reject in the normalizer and let the partition mark itself failed — do not silently strip suffixes.
3. **`fiscal_year` / `fiscal_quarter` natural-key vs source_event_id**: the `MarketEvent` partial unique index on `(source, category, market, source_event_id) WHERE source_event_id IS NOT NULL` is the upsert anchor. Because we always emit a deterministic `source_event_id`, the `fiscal_year` / `fiscal_quarter` natural-key path is *not* used for wisefn — both work, but only one is the live key. Keep it that way.
4. **Status taxonomy for "expected_but_failed"**: ROB-167's freshness service surfaces partitions with `status="failed"` differently from "missing." Until `_fetch_calendar_payload` is wired, every wisefn partition that runs (with the flag flipped on) will be `failed` — confirm with the freshness consumer that this is the intended diagnostic, not an alerting noise floor.
5. **Calendar dependence on `release_time_local`**: the brief mentions /invest/calendar Toss-style display. Today the wisefn normalizer leaves `release_time_local` and `release_time_utc` both `None` — the `time_hint` (`before_open` / `after_close`) is the only ordering signal. If the UI needs precise wall-clock ordering, capture it in a follow-up after the upstream contract is known.

## Self-Review Notes

- Spec coverage:
  - "monthly batch ingestion into existing calendar tables" ✅ Tasks 1–8 reuse `market_events` only.
  - "add wisereport/wisefn source for category=earnings, market=kr" ✅ Tasks 1, 4, 5, 7.
  - "monthly fetch/parse helper" ✅ Tasks 3, 4.
  - "fixture normalizer" ✅ Task 4 (inline-dict fixtures matching the dart/finnhub pattern).
  - "CLI month mode" ✅ Task 6 (`--month YYYY-MM` wrapper).
  - "idempotent upserts" ✅ Tasks 4, 5 (deterministic `source_event_id` + repeat-ingest test).
  - "freshness/expected-source docs" ✅ Tasks 7, 8.
  - "Keep WiseReport/WiseFn operational writes disabled by default behind a setting such as WISEFN_EARNINGS_ENABLED=false" ✅ Tasks 2, 6.
  - "Never call WiseReport/WiseFn live from CI/tests; use fixtures." ✅ Tasks 3, 4, 5 (NotImplementedError default + fixture injection).
- Placeholder scan: no TBD / TODO / "implement later" / "similar to Task N" / un-coded "add validation" steps. All code blocks are runnable verbatim.
- Type consistency:
  - Source string is `"wisefn"` everywhere (taxonomy, expected_sources, CLI SUPPORTED, ingestor return value, normalizer event dict, source_event_id prefix).
  - Setting name is `wisefn_earnings_enabled` everywhere; env var is `WISEFN_EARNINGS_ENABLED`.
  - Normalizer name is `normalize_wisefn_earnings_row` everywhere (test imports, ingestor import, normalizers.py public symbol).
  - Helper module is `app.services.market_events.wisefn_helpers` everywhere.
  - Orchestrator name is `ingest_kr_earnings_wisefn_for_date` everywhere (CLI SUPPORTED, ingestion.py public symbol, test imports).
  - `_fetch_calendar_payload` (with leading underscore) is the patched seam; `fetch_wisefn_earnings_for_date` is the public coroutine.
  - `month_to_date_range` returns `tuple[date, date]` — used only inside `parse_args`.
