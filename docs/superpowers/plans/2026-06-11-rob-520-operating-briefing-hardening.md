# ROB-520 Operating Briefing Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden `get_operating_briefing` so optional sections fail open, advisory report lookup cannot be hidden behind newer smoke rows, and the declared response schema is actually used.

**Architecture:** Keep the public MCP response shape additive and backward-compatible. Add query-level report filters in the repository/query-service boundary, then have the operating briefing compose each optional section with explicit fallback payloads and per-section `staleness` metadata.

**Tech Stack:** Python 3.13, FastAPI-adjacent MCP tooling, SQLAlchemy async sessions, Pydantic v2 schemas, pytest/pytest-asyncio, Ruff/ty via `make lint`.

---

## Root Cause

ROB-517 added `app/mcp_server/tooling/operating_briefing.py`, but the branch initially lagged `origin/main`. The branch has been fast-forwarded to `e2f9ec74` so the ROB-517 files now exist locally.

Findings:

- `get_operating_briefing_impl` catches no exceptions around `_latest_report_summary`, `_recent_session_context`, or `list_active_watches_impl`. Any validation/database/runtime error in those optional sections aborts the whole briefing.
- `_latest_report_summary` calls `InvestmentReportQueryService.list_reports(limit=20)` and filters advisory profiles in memory. If 20 newer smoke/test rows exist, an older advisory row is silently missed.
- `OperatingBriefingResponse` exists in `app/schemas/investment_reports.py` but `get_operating_briefing_impl` returns a hand-built dict without validating it.

Scope:

- Required: fix the three named optional section fail-open gaps.
- Required: move advisory profile filtering to SQL-level query filters and exclude superseded advisory reports.
- Required: use `OperatingBriefingResponse` rather than deleting it.
- Out of scope for this pass: parallelizing sections, adding `has_more`, or changing `_kis_expected_expiry`.

## File Structure

- Modify `app/services/investment_reports/repository.py`: add additive SQL filters for `created_by_profile IN (...)` and `status NOT IN (...)`.
- Modify `app/services/investment_reports/query_service.py`: expose the same filters through `list_reports` and `latest_report`.
- Modify `app/mcp_server/tooling/operating_briefing.py`: use SQL-level advisory lookup, add per-section fail-open fallbacks, and validate output through `OperatingBriefingResponse`.
- Modify `app/mcp_server/README.md`: document degraded optional-section behavior.
- Modify `tests/test_investment_reports_repository.py`: cover new repository filters.
- Modify `tests/test_investment_reports_query_service.py`: cover query-service pass-through for new filters.
- Modify `tests/mcp_server/test_operating_briefing_tools.py`: cover advisory lookback, superseded exclusion, fail-open behavior, and schema-validated response shape.

---

### Task 1: Add SQL-Level Report Filters

**Files:**
- Modify: `app/services/investment_reports/repository.py:13-164`
- Modify: `app/services/investment_reports/query_service.py:10-129`
- Test: `tests/test_investment_reports_repository.py`
- Test: `tests/test_investment_reports_query_service.py`

- [ ] **Step 1: Write failing repository tests**

Append these tests after `test_list_reports_filters_by_market_and_status` in `tests/test_investment_reports_repository.py`:

```python
@pytest.mark.asyncio
async def test_list_reports_filters_by_created_by_profiles_and_excluded_statuses(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    await _insert_report(
        repo,
        title="smoke",
        created_by_profile="test",
        status="draft",
    )
    await _insert_report(
        repo,
        title="old-superseded-advisory",
        created_by_profile="CLAUDE_ADVISOR",
        status="superseded",
    )
    expected = await _insert_report(
        repo,
        title="current-advisory",
        created_by_profile="CLAUDE_ADVISOR",
        status="draft",
    )

    rows = await repo.list_reports(
        market="kr",
        account_scope="kis_mock",
        created_by_profiles={"HERMES_ADVISOR", "CLAUDE_ADVISOR"},
        exclude_statuses={"superseded"},
        limit=10,
    )

    assert [row.id for row in rows] == [expected.id]


@pytest.mark.asyncio
async def test_latest_report_honors_created_by_profiles_and_excluded_statuses(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    await _insert_report(
        repo,
        title="published-smoke",
        created_by_profile="test",
        status="published",
    )
    await _insert_report(
        repo,
        title="superseded-advisory",
        created_by_profile="CLAUDE_ADVISOR",
        status="superseded",
    )
    expected = await _insert_report(
        repo,
        title="draft-advisory",
        created_by_profile="CLAUDE_ADVISOR",
        status="draft",
    )

    latest = await repo.latest_report(
        market="kr",
        account_scope="kis_mock",
        created_by_profiles={"CLAUDE_ADVISOR"},
        exclude_statuses={"superseded"},
    )

    assert latest is not None
    assert latest.id == expected.id
```

- [ ] **Step 2: Run repository tests and verify they fail**

Run:

```bash
uv run pytest tests/test_investment_reports_repository.py::test_list_reports_filters_by_created_by_profiles_and_excluded_statuses tests/test_investment_reports_repository.py::test_latest_report_honors_created_by_profiles_and_excluded_statuses -q
```

Expected: both tests fail with `TypeError: ... unexpected keyword argument 'created_by_profiles'`.

- [ ] **Step 3: Implement repository filters**

Update `app/services/investment_reports/repository.py`.

Change the imports:

```python
from collections.abc import Collection
from datetime import datetime
from typing import Any
```

Update `list_reports`:

```python
    async def list_reports(
        self,
        *,
        market: str | None = None,
        market_session: str | None = None,
        account_scope: str | None = None,
        status: str | None = None,
        report_type: str | None = None,
        created_by_profiles: Collection[str] | None = None,
        exclude_statuses: Collection[str] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[InvestmentReport]:
        stmt = sa.select(InvestmentReport).order_by(
            InvestmentReport.created_at.desc(), InvestmentReport.id.desc()
        )
        stmt = self._apply_report_filters(
            stmt,
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            status=status,
            report_type=report_type,
            created_by_profiles=created_by_profiles,
            exclude_statuses=exclude_statuses,
        )
        if offset:
            stmt = stmt.offset(offset)
        stmt = stmt.limit(limit)
        result = await self._session.scalars(stmt)
        return list(result.all())
```

Update `latest_report`:

```python
    async def latest_report(
        self,
        *,
        market: str | None = None,
        market_session: str | None = None,
        account_scope: str | None = None,
        status: str | None = None,
        report_type: str | None = None,
        created_by_profiles: Collection[str] | None = None,
        exclude_statuses: Collection[str] | None = None,
    ) -> InvestmentReport | None:
        stmt = sa.select(InvestmentReport).order_by(
            InvestmentReport.created_at.desc(), InvestmentReport.id.desc()
        )
        stmt = self._apply_report_filters(
            stmt,
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            status=status,
            report_type=report_type,
            created_by_profiles=created_by_profiles,
            exclude_statuses=exclude_statuses,
        )
        return await self._session.scalar(stmt.limit(1))
```

Update `_apply_report_filters`:

```python
    @staticmethod
    def _apply_report_filters(
        stmt: sa.Select,
        *,
        market: str | None,
        market_session: str | None,
        account_scope: str | None,
        status: str | None,
        report_type: str | None,
        created_by_profiles: Collection[str] | None,
        exclude_statuses: Collection[str] | None,
    ) -> sa.Select:
        if market is not None:
            stmt = stmt.where(InvestmentReport.market == market)
        if market_session is not None:
            stmt = stmt.where(InvestmentReport.market_session == market_session)
        if account_scope is not None:
            stmt = stmt.where(InvestmentReport.account_scope == account_scope)
        if status is not None:
            stmt = stmt.where(InvestmentReport.status == status)
        if report_type is not None:
            stmt = stmt.where(InvestmentReport.report_type == report_type)
        if created_by_profiles:
            stmt = stmt.where(
                InvestmentReport.created_by_profile.in_(created_by_profiles)
            )
        if exclude_statuses:
            stmt = stmt.where(InvestmentReport.status.not_in(exclude_statuses))
        return stmt
```

- [ ] **Step 4: Run repository tests and verify they pass**

Run:

```bash
uv run pytest tests/test_investment_reports_repository.py::test_list_reports_filters_by_created_by_profiles_and_excluded_statuses tests/test_investment_reports_repository.py::test_latest_report_honors_created_by_profiles_and_excluded_statuses -q
```

Expected: both tests pass.

- [ ] **Step 5: Write failing query-service pass-through test**

Append this test after `test_list_reports_filters` in `tests/test_investment_reports_query_service.py`:

```python
@pytest.mark.asyncio
async def test_query_service_list_and_latest_reports_pass_profile_filters(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    await repo.insert_report(
        idempotency_key=f"query-filter-smoke:{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="test",
        title="smoke",
        summary="s",
        status="draft",
    )
    expected = await repo.insert_report(
        idempotency_key=f"query-filter-advisory:{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="CLAUDE_ADVISOR",
        title="advisory",
        summary="s",
        status="draft",
    )
    await repo.insert_report(
        idempotency_key=f"query-filter-superseded:{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="CLAUDE_ADVISOR",
        title="superseded",
        summary="s",
        status="superseded",
    )

    query = InvestmentReportQueryService(session)
    reports = await query.list_reports(
        market="kr",
        account_scope="kis_mock",
        created_by_profiles={"CLAUDE_ADVISOR"},
        exclude_statuses={"superseded"},
    )
    latest = await query.latest_report(
        market="kr",
        account_scope="kis_mock",
        created_by_profiles={"CLAUDE_ADVISOR"},
        exclude_statuses={"superseded"},
    )

    assert [row.id for row in reports] == [expected.id]
    assert latest is not None
    assert latest.id == expected.id
```

- [ ] **Step 6: Run query-service test and verify it fails**

Run:

```bash
uv run pytest tests/test_investment_reports_query_service.py::test_query_service_list_and_latest_reports_pass_profile_filters -q
```

Expected: fails with `TypeError: ... unexpected keyword argument 'created_by_profiles'`.

- [ ] **Step 7: Implement query-service pass-through**

Update `app/services/investment_reports/query_service.py`.

Change the imports:

```python
from collections.abc import Collection
import json
from datetime import datetime
```

Update `list_reports`:

```python
    async def list_reports(
        self,
        *,
        market: str | None = None,
        market_session: str | None = None,
        account_scope: str | None = None,
        status: str | None = None,
        report_type: str | None = None,
        created_by_profiles: Collection[str] | None = None,
        exclude_statuses: Collection[str] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[InvestmentReport]:
        return await self._repo.list_reports(
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            status=status,
            report_type=report_type,
            created_by_profiles=created_by_profiles,
            exclude_statuses=exclude_statuses,
            limit=limit,
            offset=offset,
        )
```

Update `latest_report`:

```python
    async def latest_report(
        self,
        *,
        market: str | None = None,
        market_session: str | None = None,
        account_scope: str | None = None,
        status: str | None = None,
        report_type: str | None = None,
        created_by_profiles: Collection[str] | None = None,
        exclude_statuses: Collection[str] | None = None,
    ) -> InvestmentReport | None:
        return await self._repo.latest_report(
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            status=status,
            report_type=report_type,
            created_by_profiles=created_by_profiles,
            exclude_statuses=exclude_statuses,
        )
```

- [ ] **Step 8: Run query-service test and verify it passes**

Run:

```bash
uv run pytest tests/test_investment_reports_query_service.py::test_query_service_list_and_latest_reports_pass_profile_filters -q
```

Expected: pass.

- [ ] **Step 9: Commit Task 1**

```bash
git add app/services/investment_reports/repository.py app/services/investment_reports/query_service.py tests/test_investment_reports_repository.py tests/test_investment_reports_query_service.py
git commit -m "fix(ROB-520): filter advisory reports in SQL"
```

---

### Task 2: Fix Latest Advisory Report Lookup

**Files:**
- Modify: `app/mcp_server/tooling/operating_briefing.py:14-185`
- Test: `tests/mcp_server/test_operating_briefing_tools.py`

- [ ] **Step 1: Add report helper for operating briefing tests**

In `tests/mcp_server/test_operating_briefing_tools.py`, add this import:

```python
from app.services.investment_reports.repository import InvestmentReportsRepository
```

Add this helper after `FakeMCP`:

```python
async def _insert_briefing_report(
    session: AsyncSession,
    *,
    title: str,
    created_by_profile: str,
    status: str = "draft",
):
    repo = InvestmentReportsRepository(session)
    return await repo.insert_report(
        idempotency_key=f"rob520:briefing-report:{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile=created_by_profile,
        title=title,
        summary="s",
        status=status,
        report_metadata={},
    )
```

- [ ] **Step 2: Write failing lookback regression tests**

Append these tests after `test_latest_report_summary_skips_non_advisory_newer_report`:

```python
@pytest.mark.asyncio
async def test_latest_report_summary_finds_advisory_beyond_twenty_smoke_reports(
    session: AsyncSession,
) -> None:
    from app.mcp_server.tooling import operating_briefing as ob

    advisory = await _insert_briefing_report(
        session,
        title="older advisory",
        created_by_profile="CLAUDE_ADVISOR",
    )
    for idx in range(25):
        await _insert_briefing_report(
            session,
            title=f"newer smoke {idx}",
            created_by_profile="test",
        )
    await session.commit()

    summary = await ob._latest_report_summary(
        session,
        market="kr",
        account_scope="kis_mock",
    )

    assert summary is not None
    assert summary["report_uuid"] == str(advisory.report_uuid)
    assert summary["title"] == "older advisory"


@pytest.mark.asyncio
async def test_latest_report_summary_excludes_superseded_advisory(
    session: AsyncSession,
) -> None:
    from app.mcp_server.tooling import operating_briefing as ob

    current = await _insert_briefing_report(
        session,
        title="current advisory",
        created_by_profile="CLAUDE_ADVISOR",
    )
    await _insert_briefing_report(
        session,
        title="superseded advisory",
        created_by_profile="CLAUDE_ADVISOR",
        status="superseded",
    )
    await session.commit()

    summary = await ob._latest_report_summary(
        session,
        market="kr",
        account_scope="kis_mock",
    )

    assert summary is not None
    assert summary["report_uuid"] == str(current.report_uuid)
    assert summary["title"] == "current advisory"
```

- [ ] **Step 3: Run lookback tests and verify they fail**

Run:

```bash
uv run pytest tests/mcp_server/test_operating_briefing_tools.py::test_latest_report_summary_finds_advisory_beyond_twenty_smoke_reports tests/mcp_server/test_operating_briefing_tools.py::test_latest_report_summary_excludes_superseded_advisory -q
```

Expected before implementation:

- First test fails because `_latest_report_summary` returns `None`.
- Second test fails because the newer `superseded advisory` is selected.

- [ ] **Step 4: Implement query-level advisory lookup**

Update `app/mcp_server/tooling/operating_briefing.py`.

Replace `_latest_report_summary` lines 139-153 with:

```python
    service = InvestmentReportQueryService(db)
    report = await service.latest_report(
        market=market,
        account_scope=account_scope,
        created_by_profiles=_advisory_draft_profiles(),
        exclude_statuses={"superseded"},
    )
```

The rest of `_latest_report_summary` stays the same:

```python
    if report is None:
        return None
    bundle = await service.get_bundle(report.report_uuid)
```

- [ ] **Step 5: Run lookback tests and verify they pass**

Run:

```bash
uv run pytest tests/mcp_server/test_operating_briefing_tools.py::test_latest_report_summary_finds_advisory_beyond_twenty_smoke_reports tests/mcp_server/test_operating_briefing_tools.py::test_latest_report_summary_excludes_superseded_advisory -q
```

Expected: both tests pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add app/mcp_server/tooling/operating_briefing.py tests/mcp_server/test_operating_briefing_tools.py
git commit -m "fix(ROB-520): find advisory briefing reports past smoke rows"
```

---

### Task 3: Make Optional Briefing Sections Fail Open

**Files:**
- Modify: `app/mcp_server/tooling/operating_briefing.py:14-286`
- Test: `tests/mcp_server/test_operating_briefing_tools.py`

- [ ] **Step 1: Write failing fail-open tests**

Append this test after `test_get_operating_briefing_composes_all_sections`:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("section_name", "patch_name"),
    [
        ("latest_report", "_latest_report_summary"),
        ("session_context", "_recent_session_context"),
        ("active_watches", "list_active_watches_impl"),
    ],
)
async def test_get_operating_briefing_fail_opens_optional_sections(
    monkeypatch: pytest.MonkeyPatch,
    section_name: str,
    patch_name: str,
) -> None:
    from app.mcp_server.tooling import operating_briefing as ob

    async def fake_holdings(**kwargs):
        return {
            "filters": {"market": kwargs["market"]},
            "total_accounts": 1,
            "total_positions": 0,
            "summary": {},
            "accounts": [],
            "errors": [],
        }

    class EmptyPendingSnapshot:
        orders: list[dict] = []
        as_of = "2026-06-11T01:00:00+00:00"
        freshness_status = "fresh"
        unavailable_reason = None
        account_scope = "kis_live"

    async def fake_pending(db, *, market, account_scope):
        return EmptyPendingSnapshot()

    async def ok_latest_report(db, *, market, account_scope):
        return {
            "report_uuid": "11111111-1111-1111-1111-111111111111",
            "title": "latest plan",
            "status": "draft",
            "created_at": "2026-06-11T00:00:00+00:00",
            "items": {"total": 0, "by_status": {}, "top": []},
        }

    async def ok_session_context(db, *, market, account_scope, limit):
        return {"count": 1, "entries": [{"title": "handoff"}]}

    async def ok_active_watches(**kwargs):
        return {
            "success": True,
            "count": 1,
            "as_of": "2026-06-11T01:00:00+00:00",
            "filters": kwargs,
            "active_watches": [{"symbol": "005930"}],
        }

    async def boom(*args, **kwargs):
        raise RuntimeError("section boom")

    monkeypatch.setattr(ob, "_get_holdings_impl", fake_holdings)
    monkeypatch.setattr(ob, "collect_pending_orders_snapshot", fake_pending)
    monkeypatch.setattr(ob, "_latest_report_summary", ok_latest_report)
    monkeypatch.setattr(ob, "_recent_session_context", ok_session_context)
    monkeypatch.setattr(ob, "list_active_watches_impl", ok_active_watches)
    monkeypatch.setattr(ob, patch_name, boom)

    result = await ob.get_operating_briefing_impl(
        market="kr",
        account_scope="kis_live",
    )

    assert result["success"] is True
    assert result["staleness"][section_name]["freshness_status"] == "unavailable"
    assert result["staleness"][section_name]["unavailable_reason"].startswith(
        f"{section_name}_failed:RuntimeError:section boom"
    )
    if section_name == "latest_report":
        assert result["latest_report"] is None
    elif section_name == "session_context":
        assert result["session_context"] == {
            "count": 0,
            "entries": [],
            "unavailable_reason": "session_context_failed:RuntimeError:section boom",
        }
    else:
        assert result["active_watches"] == {
            "count": 0,
            "watches": [],
            "unavailable_reason": "active_watches_failed:RuntimeError:section boom",
        }
```

- [ ] **Step 2: Run fail-open tests and verify they fail**

Run:

```bash
uv run pytest tests/mcp_server/test_operating_briefing_tools.py::test_get_operating_briefing_fail_opens_optional_sections -q
```

Expected: all parameter cases fail by raising `RuntimeError: section boom`.

- [ ] **Step 3: Use `OperatingBriefingResponse` in the implementation**

Update the schema imports in `app/mcp_server/tooling/operating_briefing.py`:

```python
from app.schemas.investment_reports import (
    ActiveWatchesListResponse,
    InvestmentWatchAlertResponse,
    OperatingBriefingResponse,
)
```

- [ ] **Step 4: Add section failure helper**

Add this helper after `_top_movers`:

```python
def _section_unavailable_reason(section: str, exc: Exception) -> str:
    return f"{section}_failed:{type(exc).__name__}:{exc}"
```

- [ ] **Step 5: Implement fail-open composition**

Replace `get_operating_briefing_impl` with:

```python
async def get_operating_briefing_impl(
    market: str,
    account_scope: str | None = None,
    session_context_limit: int = 10,
    include_current_price: bool = True,
) -> dict[str, Any]:
    as_of = now_kst()
    effective_scope = _default_account_scope(market, account_scope)
    holdings = await _get_holdings_impl(
        **_holdings_kwargs(market, effective_scope, include_current_price)
    )
    async with AsyncSessionLocal() as db:
        pending = await collect_pending_orders_snapshot(
            db,
            market=market,
            account_scope=effective_scope,
        )
        try:
            latest_report = await _latest_report_summary(
                db,
                market=market,
                account_scope=effective_scope,
            )
            latest_report_staleness = {
                "freshness_status": "db_read" if latest_report else "not_found",
            }
        except Exception as exc:  # noqa: BLE001
            reason = _section_unavailable_reason("latest_report", exc)
            latest_report = None
            latest_report_staleness = {
                "freshness_status": "unavailable",
                "unavailable_reason": reason,
            }

        try:
            session_context = await _recent_session_context(
                db,
                market=market,
                account_scope=effective_scope,
                limit=session_context_limit,
            )
            session_context_staleness = {
                "freshness_status": "db_read",
            }
        except Exception as exc:  # noqa: BLE001
            reason = _section_unavailable_reason("session_context", exc)
            session_context = {
                "count": 0,
                "entries": [],
                "unavailable_reason": reason,
            }
            session_context_staleness = {
                "freshness_status": "unavailable",
                "unavailable_reason": reason,
            }

    try:
        active_watches = await list_active_watches_impl(market=market)
        active_watches_staleness = {
            "as_of": active_watches.get("as_of"),
            "freshness_status": "db_read",
        }
    except Exception as exc:  # noqa: BLE001
        reason = _section_unavailable_reason("active_watches", exc)
        active_watches = {
            "count": 0,
            "active_watches": [],
            "unavailable_reason": reason,
        }
        active_watches_staleness = {
            "as_of": None,
            "freshness_status": "unavailable",
            "unavailable_reason": reason,
        }

    active_watches_unavailable_reason = active_watches.get("unavailable_reason")
    response = {
        "success": True,
        "market": market,
        "account_scope": effective_scope,
        "as_of": as_of.isoformat(),
        "staleness": {
            "holdings": {
                "as_of": as_of.isoformat(),
                "freshness_status": "live_or_best_effort",
                "errors": holdings.get("errors") or [],
            },
            "pending_orders": {
                "as_of": pending.as_of,
                "freshness_status": pending.freshness_status,
                "unavailable_reason": pending.unavailable_reason,
            },
            "active_watches": active_watches_staleness,
            "latest_report": latest_report_staleness,
            "session_context": session_context_staleness,
        },
        "holdings": {
            "filters": holdings.get("filters"),
            "total_accounts": holdings.get("total_accounts"),
            "total_positions": holdings.get("total_positions"),
            "summary": holdings.get("summary"),
            "top_movers": _top_movers(holdings),
            "errors": holdings.get("errors") or [],
        },
        "pending_orders": {
            "count": len(pending.orders or []),
            "orders": pending.orders,
            "unavailable_reason": pending.unavailable_reason,
        },
        "active_watches": {
            "count": active_watches.get("count", 0),
            "watches": active_watches.get("active_watches", []),
            **(
                {"unavailable_reason": active_watches_unavailable_reason}
                if active_watches_unavailable_reason
                else {}
            ),
        },
        "latest_report": latest_report,
        "session_context": session_context,
    }
    return OperatingBriefingResponse.model_validate(response).model_dump(mode="json")
```

- [ ] **Step 6: Run fail-open tests and verify they pass**

Run:

```bash
uv run pytest tests/mcp_server/test_operating_briefing_tools.py::test_get_operating_briefing_fail_opens_optional_sections -q
```

Expected: pass.

- [ ] **Step 7: Run existing operating briefing tests**

Run:

```bash
uv run pytest tests/mcp_server/test_operating_briefing_tools.py -q
```

Expected: pass.

- [ ] **Step 8: Commit Task 3**

```bash
git add app/mcp_server/tooling/operating_briefing.py tests/mcp_server/test_operating_briefing_tools.py
git commit -m "fix(ROB-520): fail open optional briefing sections"
```

---

### Task 4: Document Degraded Section Behavior

**Files:**
- Modify: `app/mcp_server/README.md:697-715`

- [ ] **Step 1: Update README response semantics**

In `app/mcp_server/README.md`, under `### get_operating_briefing`, replace the final staleness sentence with:

```markdown
- `staleness`: per-section `as_of`, freshness, and unavailable reason where available. If an optional DB-backed section (`active_watches`, `latest_report`, or `session_context`) raises, the tool still returns `success=true`; that section is returned as an empty or null fallback and `staleness.<section>.freshness_status` is `unavailable` with `unavailable_reason`.
```

- [ ] **Step 2: Commit Task 4**

```bash
git add app/mcp_server/README.md
git commit -m "docs(ROB-520): document operating briefing degraded sections"
```

---

### Task 5: Final Verification

**Files:**
- Verify all modified files.

- [ ] **Step 1: Run targeted tests**

```bash
uv run pytest tests/mcp_server/test_operating_briefing_tools.py tests/test_investment_reports_repository.py tests/test_investment_reports_query_service.py -q
```

Expected: pass.

- [ ] **Step 2: Run adjacent MCP investment report tests**

```bash
uv run pytest tests/test_investment_reports_mcp.py -q
```

Expected: pass.

- [ ] **Step 3: Run lint/type gate**

```bash
make lint
```

Expected: pass.

- [ ] **Step 4: Review diff**

```bash
git diff --stat origin/main...HEAD
git diff -- app/mcp_server/tooling/operating_briefing.py app/services/investment_reports/repository.py app/services/investment_reports/query_service.py app/mcp_server/README.md tests/mcp_server/test_operating_briefing_tools.py tests/test_investment_reports_repository.py tests/test_investment_reports_query_service.py
```

Expected:

- No public MCP field removals or renames.
- Only additive query-service/repository filters.
- Fail-open behavior limited to `latest_report`, `session_context`, and `active_watches`.
- `OperatingBriefingResponse` is imported and used.

- [ ] **Step 5: Update Linear**

Add a Linear comment to ROB-520:

```markdown
Implemented ROB-520 hardening:
- `get_operating_briefing` now fail-opens `latest_report`, `session_context`, and `active_watches` with `staleness.<section>.unavailable_reason`.
- Advisory report lookup moved to SQL-level profile filtering and excludes `superseded`.
- `OperatingBriefingResponse` now validates the composed response.

Verification:
- `uv run pytest tests/mcp_server/test_operating_briefing_tools.py tests/test_investment_reports_repository.py tests/test_investment_reports_query_service.py -q`
- `uv run pytest tests/test_investment_reports_mcp.py -q`
- `make lint`
```

- [ ] **Step 6: Final commit if Task 5 changed only metadata**

If only Linear was updated, no commit is needed. If verification required docs/test adjustments, commit them:

```bash
git add app/mcp_server/README.md tests/mcp_server/test_operating_briefing_tools.py tests/test_investment_reports_repository.py tests/test_investment_reports_query_service.py
git commit -m "chore(ROB-520): tighten operating briefing verification"
```
