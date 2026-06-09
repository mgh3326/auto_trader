# ROB-376 item 2 — intraday_update context tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hermes가 단일 pull로 (현재 번들 context + baseline 리포트 대비 델타)를 받아 `intraday_update_v1` 리포트를 compose할 수 있도록, 신규 read-only MCP 도구 `investment_report_prepare_intraday_context`와 `HermesContextPayload`의 델타 블록 필드를 추가한다.

**Architecture:** PR1의 `DeltaService.compute_delta`(델타) + 기존 `HermesContextExporter.export`(번들 context)를 결합하는 얇은 핸들러. `HermesContextPayload`에 optional `baseline_report_uuid`/`intraday_delta_block`(additive) 추가. 신호별 fail-open(델타 실패가 context를 죽이지 않음). gate=기존 `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED`. migration 0, no LLM, no broker/watch mutation.

**Tech Stack:** Python 3.13, Pydantic v2, FastMCP, pytest. 재사용: `HermesContextExporter`, `DeltaService`, `_disabled_check`/`_parse_bundle_uuid`.

**Spec:** `docs/superpowers/specs/2026-06-02-rob-376-item2-intraday-update-context-design.md`

---

## File Structure

- `app/schemas/hermes_composition.py` — `HermesContextPayload`에 `baseline_report_uuid`/`intraday_delta_block` optional 필드 추가.
- `app/mcp_server/tooling/investment_hermes_handlers.py` — `INTRADAY_UPDATE_REPORT_TYPE` 상수 + `investment_report_prepare_intraday_context_impl` + 등록 + `INVESTMENT_HERMES_TOOL_NAMES` + `__all__`.
- `tests/mcp_server/test_investment_hermes_tools.py` — tool-names set 단언 갱신 + 신규 도구 동작 테스트.
- (신규) `tests/test_hermes_context_payload_intraday.py` — 스키마 additive 단위.

---

## Task 1: 스키마 additive 필드 (TDD)

**Files:**
- Create: `tests/test_hermes_context_payload_intraday.py`
- Modify: `app/schemas/hermes_composition.py` (`HermesContextPayload`, 현 라인 88-101 필드 블록)

- [ ] **Step 1: 실패 테스트 작성**

새 파일 `tests/test_hermes_context_payload_intraday.py`:

```python
"""ROB-376 item 2 — HermesContextPayload intraday delta fields (additive)."""

from __future__ import annotations

import uuid

from app.schemas.hermes_composition import HermesContextPayload


def _minimal(**kw) -> HermesContextPayload:
    base = dict(
        snapshot_bundle_uuid=uuid.uuid4(),
        bundle_status="ready",
        market="us",
        policy_version="intraday_action_report_v1",
    )
    base.update(kw)
    return HermesContextPayload(**base)


def test_intraday_fields_default_none() -> None:
    payload = _minimal()
    assert payload.baseline_report_uuid is None
    assert payload.intraday_delta_block is None
    # context_version unchanged (additive, no version bump)
    assert payload.context_version == "hermes-context.v1"


def test_intraday_fields_roundtrip() -> None:
    base = uuid.uuid4()
    block = {"success": True, "levels_delta": {"summary": {"target_hit": 1}}}
    payload = _minimal(baseline_report_uuid=base, intraday_delta_block=block)
    dumped = payload.model_dump(mode="json")
    assert dumped["baseline_report_uuid"] == str(base)
    assert dumped["intraday_delta_block"] == block
```

- [ ] **Step 2: 실행 → 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-376 && uv run pytest tests/test_hermes_context_payload_intraday.py -p no:randomly -q`
Expected: FAIL — `ValidationError` (extra="forbid"라 `baseline_report_uuid`/`intraday_delta_block` 미정의 키 거부).

- [ ] **Step 3: 스키마 필드 추가**

`app/schemas/hermes_composition.py`의 `HermesContextPayload`에서 `constraints: HermesContextConstraints = Field(...)` 정의(현 라인 100-102) **다음 줄**에 추가:

```python
    # ROB-376 item 2 — intraday_update report continuity. Optional/additive
    # (no context_version bump). Populated only by the intraday context tool;
    # the base get_hermes_context path leaves both None.
    baseline_report_uuid: uuid.UUID | None = None
    intraday_delta_block: dict[str, Any] | None = None
```

(`uuid`, `Any` 이미 import 되어 있음.)

- [ ] **Step 4: 실행 → 통과 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-376 && uv run pytest tests/test_hermes_context_payload_intraday.py -p no:randomly -q`
Expected: PASS (2 tests).

- [ ] **Step 5: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-376
git add app/schemas/hermes_composition.py tests/test_hermes_context_payload_intraday.py
git commit -m "feat(ROB-376): HermesContextPayload baseline_report_uuid + intraday_delta_block (additive)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: intraday context 도구 (TDD)

**Files:**
- Modify: `app/mcp_server/tooling/investment_hermes_handlers.py`
- Test: `tests/mcp_server/test_investment_hermes_tools.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/mcp_server/test_investment_hermes_tools.py`의 tool-names 단언(현 라인 78)에 신규 도구 추가:

```python
    assert INVESTMENT_HERMES_TOOL_NAMES == {
        "investment_report_prepare_bundle",
        "investment_report_get_hermes_context",
        "investment_report_create_from_hermes_composition",
        "investment_stage_artifacts_ingest_from_hermes",
        "investment_report_prepare_intraday_context",
    }
```

그리고 파일 하단에 동작 테스트 추가 (import 블록에 핸들러 추가):

```python
@pytest.mark.asyncio
async def test_prepare_intraday_context_disabled(monkeypatch) -> None:
    from app.mcp_server.tooling import investment_hermes_handlers as h

    monkeypatch.setattr(h.settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", False)
    out = await h.investment_report_prepare_intraday_context_impl(
        snapshot_bundle_uuid=str(uuid.uuid4()),
        baseline_report_uuid=str(uuid.uuid4()),
    )
    assert out["success"] is False
    assert out["error"] == "snapshot_backed_report_generator_disabled"


def _fake_payload() -> object:
    from app.schemas.hermes_composition import HermesContextPayload

    return HermesContextPayload(
        snapshot_bundle_uuid=uuid.uuid4(),
        bundle_status="ready",
        market="us",
        policy_version="intraday_action_report_v1",
    )


@pytest.mark.asyncio
async def test_prepare_intraday_context_attaches_delta(monkeypatch) -> None:
    from app.mcp_server.tooling import investment_hermes_handlers as h

    monkeypatch.setattr(h.settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True)
    base_uuid = uuid.uuid4()

    class _FakeExporter:
        def __init__(self, db): ...
        async def export(self, *, snapshot_bundle_uuid):
            return _fake_payload()

    class _FakeDelta:
        def __init__(self, db): ...
        async def compute_delta(self, report_uuid, **kw):
            return {"success": True, "baseline_report_uuid": str(report_uuid),
                    "levels_delta": {"summary": {"target_hit": 1}}}

    monkeypatch.setattr(h, "HermesContextExporter", _FakeExporter)
    monkeypatch.setattr(h, "DeltaService", _FakeDelta)

    out = await h.investment_report_prepare_intraday_context_impl(
        snapshot_bundle_uuid=str(uuid.uuid4()),
        baseline_report_uuid=str(base_uuid),
    )
    assert out["success"] is True
    assert out["report_type_hint"] == "intraday_update_v1"
    assert out["baseline_report_uuid"] == str(base_uuid)
    assert out["intraday_delta_block"]["success"] is True
    assert out["intraday_delta_block"]["levels_delta"]["summary"]["target_hit"] == 1


@pytest.mark.asyncio
async def test_prepare_intraday_context_failopen_bad_baseline(monkeypatch) -> None:
    from app.mcp_server.tooling import investment_hermes_handlers as h

    monkeypatch.setattr(h.settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True)

    class _FakeExporter:
        def __init__(self, db): ...
        async def export(self, *, snapshot_bundle_uuid):
            return _fake_payload()

    class _FakeDelta:
        def __init__(self, db): ...
        async def compute_delta(self, report_uuid, **kw):
            return {"success": False, "error": "baseline_not_found"}

    monkeypatch.setattr(h, "HermesContextExporter", _FakeExporter)
    monkeypatch.setattr(h, "DeltaService", _FakeDelta)

    out = await h.investment_report_prepare_intraday_context_impl(
        snapshot_bundle_uuid=str(uuid.uuid4()),
        baseline_report_uuid=str(uuid.uuid4()),
    )
    # fail-open: context still success, delta block carries the reason
    assert out["success"] is True
    assert out["intraday_delta_block"]["success"] is False
    assert out["intraday_delta_block"]["error"] == "baseline_not_found"


@pytest.mark.asyncio
async def test_prepare_intraday_context_failopen_delta_raises(monkeypatch) -> None:
    from app.mcp_server.tooling import investment_hermes_handlers as h

    monkeypatch.setattr(h.settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True)

    class _FakeExporter:
        def __init__(self, db): ...
        async def export(self, *, snapshot_bundle_uuid):
            return _fake_payload()

    class _FakeDelta:
        def __init__(self, db): ...
        async def compute_delta(self, report_uuid, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(h, "HermesContextExporter", _FakeExporter)
    monkeypatch.setattr(h, "DeltaService", _FakeDelta)

    out = await h.investment_report_prepare_intraday_context_impl(
        snapshot_bundle_uuid=str(uuid.uuid4()),
        baseline_report_uuid=str(uuid.uuid4()),
    )
    assert out["success"] is True
    assert "unavailable" in out["intraday_delta_block"]


@pytest.mark.asyncio
async def test_prepare_intraday_context_invalid_baseline_uuid(monkeypatch) -> None:
    from app.mcp_server.tooling import investment_hermes_handlers as h

    monkeypatch.setattr(h.settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True)

    class _FakeExporter:
        def __init__(self, db): ...
        async def export(self, *, snapshot_bundle_uuid):
            return _fake_payload()

    monkeypatch.setattr(h, "HermesContextExporter", _FakeExporter)

    out = await h.investment_report_prepare_intraday_context_impl(
        snapshot_bundle_uuid=str(uuid.uuid4()),
        baseline_report_uuid="not-a-uuid",
    )
    assert out["success"] is True
    assert out["intraday_delta_block"]["error"] == "invalid_report_uuid"
```

(파일 상단 import에 `investment_report_prepare_intraday_context_impl`을 `from app.mcp_server.tooling.investment_hermes_handlers import (...)` 묶음에 추가. `uuid`/`pytest`는 기존 테스트에서 이미 import.)

- [ ] **Step 2: 실행 → 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-376 && uv run pytest tests/mcp_server/test_investment_hermes_tools.py -p no:randomly -q`
Expected: FAIL — `ImportError`(핸들러 미정의) + names-set 단언 불일치.

- [ ] **Step 3: 상수 + 핸들러 구현**

`app/mcp_server/tooling/investment_hermes_handlers.py`에서 `DeltaService` import를 상단에 추가 (현 import 블록):

```python
from app.services.investment_reports.delta_service import DeltaService
```

(`HermesContextExporter`는 이미 import. `settings`, `AsyncSessionLocal`, `uuid`, `Any`도 이미 import.)

`INVESTMENT_HERMES_TOOL_NAMES` set(현 라인 68-73)에 추가:

```python
    "investment_report_prepare_intraday_context",
```

상수 추가 (`_DISABLED_PAYLOAD` 부근, 모듈 상단):

```python
INTRADAY_UPDATE_REPORT_TYPE = "intraday_update_v1"
```

`investment_report_get_hermes_context_impl` 함수 정의 **다음**에 신규 핸들러 추가:

```python
# ---------------------------------------------------------------------------
# investment_report_prepare_intraday_context (ROB-376 item 2)
# ---------------------------------------------------------------------------
async def investment_report_prepare_intraday_context_impl(
    snapshot_bundle_uuid: str,
    baseline_report_uuid: str,
    near_pct: float = 1.0,
    account_type: str = "live",
) -> dict[str, Any]:
    """Assemble an intraday_update Hermes context: the bundle's deterministic
    context + an ``intraday_delta_block`` (report-vs-now/prior delta) keyed to
    ``baseline_report_uuid``. Read-only; no in-process LLM; no broker / order /
    watch / order-intent mutation. Fail-open: a delta failure leaves the rest
    of the context intact. Gated by SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED.
    """
    disabled = _disabled_check()
    if disabled is not None:
        return disabled

    parsed_bundle = _parse_bundle_uuid(snapshot_bundle_uuid)
    if isinstance(parsed_bundle, dict):
        return parsed_bundle

    from app.core.timezone import now_kst

    base_uuid: uuid.UUID | None = None
    async with AsyncSessionLocal() as db:
        exporter = HermesContextExporter(db)
        try:
            payload = await exporter.export(snapshot_bundle_uuid=parsed_bundle)
        except HermesContextExportError as exc:
            return {
                "success": False,
                "error": "snapshot_bundle_not_found",
                "snapshot_bundle_uuid": snapshot_bundle_uuid,
                "detail": str(exc),
            }

        # Delta is fail-open: errors ride inside intraday_delta_block, never
        # flip the context's success.
        try:
            base_uuid = uuid.UUID(baseline_report_uuid)
        except (ValueError, AttributeError, TypeError):
            delta_block: dict[str, Any] = {
                "success": False,
                "error": "invalid_report_uuid",
            }
        else:
            try:
                delta_block = await DeltaService(db).compute_delta(
                    base_uuid,
                    near_pct=near_pct,
                    account_type=account_type,
                    computed_at_kst=now_kst().isoformat(),
                )
            except Exception as exc:  # noqa: BLE001 — fail-open
                logger.exception("intraday delta computation failed")
                delta_block = {"unavailable": str(exc) or exc.__class__.__name__}

    # Echo baseline only when the delta actually succeeded against it.
    payload.baseline_report_uuid = base_uuid if delta_block.get("success") else None
    payload.intraday_delta_block = delta_block
    return {
        "success": True,
        "report_type_hint": INTRADAY_UPDATE_REPORT_TYPE,
        **payload.model_dump(mode="json"),
    }
```

(`logger`는 hermes_handlers 모듈에 이미 정의되어 있음 — 없으면 `import logging; logger = logging.getLogger(__name__)` 추가.)

- [ ] **Step 4: 등록 + __all__**

`register_investment_hermes_tools`의 마지막 `mcp.tool(...)(investment_stage_artifacts_ingest_from_hermes_impl)` 다음에 추가:

```python
    mcp.tool(
        name="investment_report_prepare_intraday_context",
        description=(
            "ROB-376 — assemble an intraday_update Hermes context: the bundle's "
            "deterministic context plus an intraday_delta_block (report-vs-now / "
            "report-vs-prior delta) keyed to baseline_report_uuid, for Hermes to "
            "compose an intraday_update_v1 report. Read-only, fail-open on the "
            "delta, no in-process LLM, no broker/order/watch mutation. Gated by "
            "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED."
        ),
    )(investment_report_prepare_intraday_context_impl)
```

`__all__`에 `"investment_report_prepare_intraday_context_impl"` 추가.

- [ ] **Step 5: 실행 → 통과 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-376 && uv run pytest tests/mcp_server/test_investment_hermes_tools.py tests/test_hermes_context_payload_intraday.py -p no:randomly -q`
Expected: PASS (기존 hermes + 신규 5 + 스키마 2).

- [ ] **Step 6: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-376
git add app/mcp_server/tooling/investment_hermes_handlers.py tests/mcp_server/test_investment_hermes_tools.py
git commit -m "feat(ROB-376): investment_report_prepare_intraday_context tool (delta-backed intraday context)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: 전체 검증 + lint

**Files:** 없음 (품질 게이트만)

- [ ] **Step 1: 관련 + 회귀 테스트**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-376
uv run pytest tests/mcp_server/test_investment_hermes_tools.py tests/test_hermes_context_payload_intraday.py tests/mcp_server/test_investment_report_delta_tool.py tests/test_investment_reports_mcp.py tests/test_us_candles_sync.py::test_revision_graph_has_single_final_head -p no:randomly -q
```
Expected: 전부 PASS (신규 + hermes/delta 회귀 + 단일 alembic head).

- [ ] **Step 2: lint (CI 게이트와 동일)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-376 && uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/`
Expected: 둘 다 통과. (format 실패 시 `uv run ruff format app/ tests/` 후 amend.)

- [ ] **Step 3: 타입 체크 + no-mutation 확인**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-376
uv run ty check app/mcp_server/tooling/investment_hermes_handlers.py app/schemas/hermes_composition.py
grep -nE "commit\(|insert_|update_alert|activate_watch|decide_item|\.add\(" app/mcp_server/tooling/investment_hermes_handlers.py | grep -i intraday || echo "no mutation in intraday handler"
```
Expected: 본 변경 신규 타입 에러 없음; intraday 핸들러에 mutation 없음.

- [ ] **Step 4: 정리 커밋 (필요 시)**

```bash
cd /Users/mgh3326/work/auto_trader.rob-376
git add -A && git commit -m "chore(ROB-376): lint/format item 2

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Post-implementation (PR 외 수동 단계)

- PR 전 사전-머지 full-CI 게이트: `ruff check app/ tests/` + GitHub Test 워크플로우 green 확인 후 머지. ⚠️ alembic 단일-head 사전 확인(최근 병렬 마이그레이션 머지로 main이 일시 two-head가 되는 사례 반복 — `uv run alembic heads`로 점검, 필요 시 `git merge origin/main` 후 재실행).
- Linear ROB-376 댓글: item 2 완료 → PR1(델타 도구)+본 PR(intraday context 결합)로 **auto_trader 측 ROB-376 종료**; no broker/scheduler/LLM 경계 + 테스트/CI evidence. 실 Hermes intraday 합성 라운드트립은 operator-gated 후속(별도). ROB-376 Done 처리 여부는 operator 판단(레포 밖 합성 미검증).

## 비범위 (재확인)

- 실제 Hermes intraday 합성/푸시(레포 밖, operator-gated). report_type은 자유 문자열 규약(`intraday_update_v1`)일 뿐 enum/CHECK/migration 없음.
- 급변주/뉴스 신규 신호(델타는 PR1의 levels/holdings_pnl/index 3신호 유지).
