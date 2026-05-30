# ROB-373 — mock/live 리포트 분리 + 공통 evidence 재사용 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude Code schedule job이 US `/invest/reports`를 실행할 때, 공통 evidence(market/news/candidate/symbol)를 한 번 수집해 재사용하고, 최종 리포트를 `kis_live`(advisory_only)와 `kis_mock`(mock_preview)로 분리하며, mock preview 리포트 item을 read-only fail-closed preflight 하에 KIS mock preview 브리지로 연결한다(실주문 executor 제외).

**Architecture:** account-독립 snapshot kind를 insert 단일 chokepoint에서 `account_scope=NULL`로 정규화 → dedupe가 row-level cross-scope 공유. 전용 mock-report runner가 live 리포트 item을 projection하고 us_dual_paper KIS-mock 어댑터로 preview하여 ingestion service에 `(kis_mock, mock_preview)`로 직접 기록. snapshot-backed generator의 live-only 가드와 Hermes advisory_only 하드코딩은 불변. Operator CLI가 전체를 오케스트레이션(default-disabled). DB 마이그레이션 0건.

**Tech Stack:** Python 3.13, SQLAlchemy async ORM, Pydantic v2, pytest-asyncio, UV. 설계 spec: `docs/superpowers/specs/2026-05-30-rob373-mock-live-report-separation-design.md`.

---

## File Structure

| File | 책임 | 신규/수정 |
|------|------|-----------|
| `app/services/investment_snapshots/scope_policy.py` | account-독립 kind 분류 + scope 정규화 (단일 진실) | 신규 |
| `app/services/investment_snapshots/repository.py` | `insert_snapshot` chokepoint에서 정규화 적용 | 수정 (lines 96-116) |
| `app/services/investment_reports/mock_preview/__init__.py` | 패키지 | 신규 |
| `app/services/investment_reports/mock_preview/bridge.py` | report item → KIS-mock preview (fail-closed, submit off) | 신규 |
| `app/services/investment_reports/mock_preview/runner.py` | live item projection + mock_preview 리포트 ingest | 신규 |
| `scripts/invest_reports_us_schedule.py` | operator CLI 오케스트레이터 (default-disabled) | 신규 |
| `docs/runbooks/invest-reports-us-schedule.md` | runbook | 신규 |
| `tests/services/investment_snapshots/test_scope_policy.py` | Unit 1 분류/정규화 | 신규 |
| `tests/services/investment_snapshots/test_insert_snapshot_scope_normalization.py` | Unit 1 chokepoint | 신규 |
| `tests/services/investment_snapshots/test_cross_scope_reuse.py` | Unit 1 cross-scope 재사용 증명 (AC) | 신규 |
| `tests/services/investment_reports/mock_preview/test_bridge.py` | Unit 3 fail-closed | 신규 |
| `tests/services/investment_reports/mock_preview/test_runner.py` | Unit 2 projection + ingest | 신규 |
| `tests/services/investment_reports/mock_preview/test_no_mutation_imports.py` | Unit 5 safety guard | 신규 |
| `tests/scripts/test_invest_reports_us_schedule_cli.py` | Unit 4 CLI gate/dry-run | 신규 |

**Prerequisites (실행 전 1회):** working tree clean, branch `rob-373`, `git fetch --prune origin` 후 origin/main 기준. (이미 확인됨: clean, 0 behind.) **`git stash` 금지** (공유 worktree). 명령은 `uv run pytest ...` 형태로 실행.

---

## Task 1: Account-독립 snapshot scope 정책 (Unit 1 core)

**Files:**
- Create: `app/services/investment_snapshots/scope_policy.py`
- Test: `tests/services/investment_snapshots/test_scope_policy.py`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/services/investment_snapshots/test_scope_policy.py
import pytest

from app.services.investment_snapshots.scope_policy import (
    ACCOUNT_INDEPENDENT_SNAPSHOT_KINDS,
    is_account_independent,
    normalize_account_scope,
)


@pytest.mark.unit
def test_account_independent_kinds_are_exactly_the_shared_four() -> None:
    assert ACCOUNT_INDEPENDENT_SNAPSHOT_KINDS == frozenset(
        {"market", "news", "candidate_universe", "symbol"}
    )


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["market", "news", "candidate_universe", "symbol"])
def test_independent_kinds_normalize_scope_to_none(kind: str) -> None:
    assert is_account_independent(kind) is True
    assert normalize_account_scope(kind, "kis_live") is None
    assert normalize_account_scope(kind, "kis_mock") is None
    assert normalize_account_scope(kind, None) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "kind", ["portfolio", "journal", "watch_context", "pending_orders"]
)
def test_account_bound_kinds_preserve_scope(kind: str) -> None:
    assert is_account_independent(kind) is False
    assert normalize_account_scope(kind, "kis_live") == "kis_live"
    assert normalize_account_scope(kind, "kis_mock") == "kis_mock"
    assert normalize_account_scope(kind, None) is None
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/services/investment_snapshots/test_scope_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.investment_snapshots.scope_policy`

- [ ] **Step 3: 최소 구현**

```python
# app/services/investment_snapshots/scope_policy.py
"""ROB-373 — account-independence policy for investment snapshot kinds.

Single source of truth deciding which snapshot kinds are *account-independent*
(market-wide evidence shared across broker scopes) vs *account-bound* (portfolio,
journal, watch, pending orders — meaningful only within one broker account).

Account-independent kinds are normalized to ``account_scope=None`` at the write
chokepoint so the snapshot dedup key ``(canonical_payload_hash, snapshot_kind,
market, account_scope)`` collapses identical market/news/candidate/symbol payloads
into ONE row that both a ``kis_live`` and a ``kis_mock`` bundle can cite.
"""

from __future__ import annotations

# market-wide evidence: identical regardless of which account requested it.
ACCOUNT_INDEPENDENT_SNAPSHOT_KINDS: frozenset[str] = frozenset(
    {"market", "news", "candidate_universe", "symbol"}
)


def is_account_independent(snapshot_kind: str) -> bool:
    """True when the kind is market-wide evidence (no account binding)."""
    return snapshot_kind in ACCOUNT_INDEPENDENT_SNAPSHOT_KINDS


def normalize_account_scope(
    snapshot_kind: str, account_scope: str | None
) -> str | None:
    """Force ``None`` for account-independent kinds; pass through otherwise.

    Idempotent and total: account-bound kinds keep whatever scope (incl. None)
    they arrived with.
    """
    if is_account_independent(snapshot_kind):
        return None
    return account_scope
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/services/investment_snapshots/test_scope_policy.py -v`
Expected: PASS (모든 케이스)

- [ ] **Step 5: 커밋**

```bash
git add app/services/investment_snapshots/scope_policy.py tests/services/investment_snapshots/test_scope_policy.py
git commit -m "feat(ROB-373): account-independence policy for snapshot kinds (Unit 1 core)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: insert_snapshot chokepoint 정규화 적용 (Unit 1 wiring)

**Files:**
- Modify: `app/services/investment_snapshots/repository.py:96-116` (`insert_snapshot`)
- Test: `tests/services/investment_snapshots/test_insert_snapshot_scope_normalization.py`

배경: `insert_snapshot`은 dedup SELECT(line 98-105)에서 `payload.account_scope`를 쓰고, INSERT(line 110-116)에서 `payload.model_dump()`의 account_scope를 쓴다. 정규화 값을 **한 번 계산**해 두 곳 모두에 적용한다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/services/investment_snapshots/test_insert_snapshot_scope_normalization.py
import datetime as dt

import pytest

from app.schemas.investment_snapshots import SnapshotCreate
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository

_NOW = dt.datetime(2026, 5, 30, 9, 0, tzinfo=dt.timezone.utc)


async def _new_run(repo: InvestmentSnapshotsRepository):
    # Reuse whatever run-create helper the repo exposes; see existing tests in
    # tests/services/investment_snapshots/ for the run fixture pattern.
    return await repo.insert_run(  # type: ignore[attr-defined]
        purpose="report_generation",
        market="us",
        account_scope="kis_live",
        policy_version="intraday_action_report_v1",
        requested_by="claude_code",
    )


def _snap(run_uuid, kind: str, scope: str | None) -> SnapshotCreate:
    return SnapshotCreate(
        run_uuid=run_uuid,
        snapshot_kind=kind,  # type: ignore[arg-type]
        market="us",
        account_scope=scope,  # type: ignore[arg-type]
        source_kind="manual",
        payload_json={"k": kind, "fixed": "payload"},
        as_of=_NOW,
        freshness_status="fresh",
    )


@pytest.mark.asyncio
async def test_market_snapshot_scope_is_normalized_to_none(db_session) -> None:
    repo = InvestmentSnapshotsRepository(db_session)
    run = await _new_run(repo)
    row = await repo.insert_snapshot(_snap(run.run_uuid, "market", "kis_live"))
    assert row.account_scope is None


@pytest.mark.asyncio
async def test_portfolio_snapshot_scope_is_preserved(db_session) -> None:
    repo = InvestmentSnapshotsRepository(db_session)
    run = await _new_run(repo)
    row = await repo.insert_snapshot(_snap(run.run_uuid, "portfolio", "kis_live"))
    assert row.account_scope == "kis_live"


@pytest.mark.asyncio
async def test_same_market_payload_dedups_across_live_and_mock(db_session) -> None:
    """kis_live and kis_mock requests for identical market payload share ONE row."""
    repo = InvestmentSnapshotsRepository(db_session)
    run = await _new_run(repo)
    live = await repo.insert_snapshot(_snap(run.run_uuid, "market", "kis_live"))
    mock = await repo.insert_snapshot(_snap(run.run_uuid, "market", "kis_mock"))
    assert live.snapshot_uuid == mock.snapshot_uuid
    assert mock.account_scope is None
```

> **Note (executing agent):** `InvestmentSnapshotsRepository`의 정확한 클래스명과 run-create helper 시그니처는 `app/services/investment_snapshots/repository.py` 상단에서 확인하라. run 생성 helper명이 `insert_run`이 아니면 기존 `tests/services/investment_snapshots/` 픽스처 패턴(예: `test_bundle_ensure_service.py`)을 그대로 따른다. 이 차이는 테스트 헬퍼에만 영향을 주며 구현 로직과 무관하다.

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/services/investment_snapshots/test_insert_snapshot_scope_normalization.py -v`
Expected: FAIL — `test_market_snapshot_scope_is_normalized_to_none`에서 `account_scope == 'kis_live'` (정규화 미적용)

- [ ] **Step 3: 구현 — chokepoint 정규화**

`app/services/investment_snapshots/repository.py`의 `insert_snapshot`에 import와 정규화를 추가한다.

파일 상단 import 블록에 추가:

```python
from app.services.investment_snapshots.scope_policy import normalize_account_scope
```

`insert_snapshot` 내부에서 dedup SELECT 직전(현재 line 96 주석 위)에 한 줄 추가하고, dedup SELECT의 `payload.account_scope`(line 103)와 INSERT data를 정규화 값으로 교체한다. 최종 형태:

```python
        # 3. ROB-373 — normalize account-independent kinds to scope=None so the
        #    dedup key shares market/news/candidate/symbol rows across scopes.
        effective_account_scope = normalize_account_scope(
            payload.snapshot_kind, payload.account_scope
        )

        # 4. Dedup short-circuit — same canonical payload reuses the existing
        #    row across runs (intentional, see docstring above).
        existing = await self._session.scalar(
            sa.select(InvestmentSnapshot).where(
                InvestmentSnapshot.canonical_payload_hash == canonical_hash,
                InvestmentSnapshot.snapshot_kind == payload.snapshot_kind,
                InvestmentSnapshot.market == payload.market,
                InvestmentSnapshot.account_scope == effective_account_scope,
            )
        )
        if existing is not None:
            return existing

        # 5. Insert.
        data = payload.model_dump(exclude={"run_uuid"})
        data["account_scope"] = effective_account_scope
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
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/services/investment_snapshots/test_insert_snapshot_scope_normalization.py -v`
Expected: PASS (3개 모두)

- [ ] **Step 5: 회귀 확인 — 기존 snapshot/bundle 테스트**

Run: `uv run pytest tests/services/investment_snapshots/ -v`
Expected: PASS (기존 테스트 회귀 없음). 만약 account-독립 kind에 명시적 scope를 기대하던 기존 테스트가 깨지면, 그 테스트가 검증하던 의도가 "정규화 전 동작"인지 확인하고 NULL 기대로 갱신한다(설계 의도 반영).

- [ ] **Step 6: 커밋**

```bash
git add app/services/investment_snapshots/repository.py tests/services/investment_snapshots/test_insert_snapshot_scope_normalization.py
git commit -m "feat(ROB-373): normalize account-independent snapshot scope to NULL at insert chokepoint (Unit 1)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: Cross-scope 재사용 증명 통합 테스트 (Unit 1 / AC)

**Files:**
- Test: `tests/services/investment_snapshots/test_cross_scope_reuse.py`

AC "공통 evidence reuse가 계좌 비의존 데이터에만 적용되고 portfolio/journal/watch는 scope별로 분리됨"을 `SnapshotBundleEnsureService` 레벨에서 증명한다. 기존 `test_bundle_ensure_service.py`의 `_manual_snapshot` / `_all_required_manual_snapshots` 패턴을 재사용한다.

- [ ] **Step 1: 실패하는(혹은 미존재) 테스트 작성**

```python
# tests/services/investment_snapshots/test_cross_scope_reuse.py
import datetime as dt
import uuid

import pytest

from app.schemas.investment_snapshots import SnapshotCollectResult
from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository

_FIXED_NOW = dt.datetime(2026, 5, 30, 9, 0, tzinfo=dt.timezone.utc)


def _manual(kind: str, *, account_scope: str | None) -> SnapshotCollectResult:
    # Identical payload across both ensure calls so dedup can match.
    return SnapshotCollectResult(
        snapshot_kind=kind,  # type: ignore[arg-type]
        market="us",  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        source_kind="manual",
        payload_json={"k": kind, "stable": "evidence"},
        as_of=_FIXED_NOW,
        freshness_status="fresh",
    )


def _manual_for(scope: str) -> dict[str, list[SnapshotCollectResult]]:
    # account-independent kinds: identical payload; account-bound: scope-specific.
    return {
        "market": [_manual("market", account_scope=scope)],
        "news": [_manual("news", account_scope=scope)],
        "candidate_universe": [_manual("candidate_universe", account_scope=scope)],
        "portfolio": [_manual("portfolio", account_scope=scope)],
        "journal": [_manual("journal", account_scope=scope)],
        "watch_context": [_manual("watch_context", account_scope=scope)],
    }


async def _uuids_by_kind(repo, bundle_uuid) -> dict[str, uuid.UUID]:
    bundle = await repo.get_bundle_by_uuid(bundle_uuid)
    pairs = await repo.list_bundle_items_with_snapshots(bundle.id)
    return {snap.snapshot_kind: snap.snapshot_uuid for _item, snap in pairs}


@pytest.mark.asyncio
async def test_independent_evidence_shared_account_bound_separated(db_session) -> None:
    repo = InvestmentSnapshotsRepository(db_session)
    svc = SnapshotBundleEnsureService(db_session)
    purpose = f"rob373_reuse_{uuid.uuid4().hex[:8]}"

    live = await svc.ensure(
        EnsureBundleRequest(
            purpose=purpose, market="us", account_scope="kis_live",
            policy_version="intraday_action_report_v1", mode="ensure_fresh",
            manual_snapshots=_manual_for("kis_live"),
        )
    )
    mock = await svc.ensure(
        EnsureBundleRequest(
            purpose=purpose, market="us", account_scope="kis_mock",
            policy_version="intraday_action_report_v1", mode="ensure_fresh",
            manual_snapshots=_manual_for("kis_mock"),
        )
    )

    live_uuids = await _uuids_by_kind(repo, live.bundle_uuid)
    mock_uuids = await _uuids_by_kind(repo, mock.bundle_uuid)

    # Distinct bundles (scope is part of bundle identity).
    assert live.bundle_uuid != mock.bundle_uuid

    # Account-INDEPENDENT evidence is the SAME row in both bundles.
    for kind in ("market", "news", "candidate_universe"):
        assert live_uuids[kind] == mock_uuids[kind], f"{kind} should be shared"

    # Account-BOUND evidence is a DIFFERENT row per scope.
    for kind in ("portfolio", "journal", "watch_context"):
        assert live_uuids[kind] != mock_uuids[kind], f"{kind} should be separated"
```

> **Note (executing agent):** `SnapshotBundleEnsureService` 생성자 인자(`clock=` 등)와 `get_bundle_by_uuid`/`list_bundle_items_with_snapshots` 정확 시그니처는 `repository.py`/`snapshot_bundle.py`에서 확인(추출에서 두 메서드는 generator.py:561-565가 사용 중임을 확인). manual_snapshots 경로가 필수 kind 누락으로 partial 처리되면 `_manual_for`에 누락 kind를 추가한다.

- [ ] **Step 2: 테스트 실행 — 통과 확인 (Task 2가 정규화를 이미 제공)**

Run: `uv run pytest tests/services/investment_snapshots/test_cross_scope_reuse.py -v`
Expected: PASS. 만약 account-독립 kind가 공유되지 않으면(FAIL) Task 2 정규화가 ensure 경로에 반영됐는지 재확인(`insert_snapshot`이 유일 write 경로여야 함).

- [ ] **Step 3: read-path 무결성 확인 (코드 점검, 변경 없을 가능성 높음)**

`list_snapshots`에는 account_scope 필터 파라미터가 없고(추출 확인됨), bundle 조회는 `bundle.account_scope`로만 필터한다(snapshot.account_scope 아님). 따라서 account-독립 snapshot read는 NULL-scope로도 정상 조회된다. 점검:

Run: `rg -n "account_scope" app/services/investment_snapshots/repository.py app/services/action_report/`
검토: account-독립 kind(market/news/candidate/symbol)를 특정 account_scope로 직접 필터해 읽는 쿼리가 있으면 `account_scope IS NULL`(또는 kind-aware)로 갱신. 없으면 변경 없음(이 단계는 점검만).

- [ ] **Step 4: 커밋**

```bash
git add tests/services/investment_snapshots/test_cross_scope_reuse.py
git commit -m "test(ROB-373): prove cross-scope evidence reuse + account-bound separation (Unit 1 AC)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 4: Mock preview 브리지 (Unit 3) — fail-closed, submit off

**Files:**
- Create: `app/services/investment_reports/mock_preview/__init__.py` (빈 파일)
- Create: `app/services/investment_reports/mock_preview/bridge.py`
- Test: `tests/services/investment_reports/mock_preview/test_bridge.py`

브리지는 **순수 preview 생성기**다: projected order intent를 받아 us_dual_paper `build_packet`을 **KIS-mock 어댑터 단독**으로 호출하고 `kis_mock` BrokerPreviewResult를 JSON-able dict로 반환. Alpaca 미혼합, submit 항상 off. 어댑터 미설정 시 호출 전 fail-closed(`unsupported`, 키 이름만).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/services/investment_reports/mock_preview/__init__.py  (빈 파일 생성)
```

```python
# tests/services/investment_reports/mock_preview/test_bridge.py
import pytest

from app.services.investment_reports.mock_preview.bridge import (
    MockPreviewBridge,
    OrderParams,
    extract_order_params,
)


@pytest.mark.unit
def test_extract_order_params_happy_path() -> None:
    params = extract_order_params(
        symbol="AAPL",
        evidence_snapshot={"reference_price_usd": 200.0},
        max_action={"notional_usd": 50.0},
    )
    assert params == OrderParams(
        symbol="AAPL",
        quantity=pytest.approx(0.25),
        limit_price_usd=200.0,
        notional_cap_usd=50.0,
        reference_price_usd=200.0,
    )


@pytest.mark.unit
def test_extract_order_params_skips_when_no_price() -> None:
    assert extract_order_params(
        symbol="AAPL", evidence_snapshot={}, max_action={}
    ) is None


@pytest.mark.unit
def test_extract_order_params_skips_when_no_symbol() -> None:
    assert extract_order_params(
        symbol=None, evidence_snapshot={"reference_price_usd": 10.0}, max_action={}
    ) is None


@pytest.mark.asyncio
async def test_bridge_fail_closed_when_adapter_disabled() -> None:
    """No KIS_MOCK_* env -> adapter disabled -> 'unsupported', names only, no submit."""
    from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter

    bridge = MockPreviewBridge(adapter=KisMockUsAdapter(enabled=False))
    out = await bridge.preview(
        OrderParams(
            symbol="AAPL", quantity=0.25, limit_price_usd=200.0,
            notional_cap_usd=50.0, reference_price_usd=200.0,
        )
    )
    assert out["status"] == "unsupported"
    assert out["submit_enabled"] is False
    # names only — never values
    assert "KIS_MOCK_APP_KEY" in out.get("missing_env_keys", []) or out.get(
        "missing_env_keys"
    ) is not None


@pytest.mark.asyncio
async def test_bridge_previews_kis_mock_only_no_alpaca() -> None:
    """When enabled with a stub client, only kis_mock broker appears; submit off."""
    from app.schemas.us_dual_paper import (
        AccountStateSummary,
        BrokerPreviewRequest,
        BrokerPreviewResult,
        DualPaperBrokerStatus,
    )
    from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter

    class _StubAdapter(KisMockUsAdapter):
        def is_enabled(self) -> bool:  # bypass env gate
            return True

        async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult:
            return BrokerPreviewResult(
                account_scope="kis_mock",
                status=DualPaperBrokerStatus.PREVIEWED,
                quantity=req.quantity,
                limit_price_usd=req.limit_price_usd,
                notional_usd=req.quantity * req.limit_price_usd,
                account_state=AccountStateSummary(buying_power_usd=1000.0),
            )

    bridge = MockPreviewBridge(adapter=_StubAdapter(enabled=True))
    out = await bridge.preview(
        OrderParams(
            symbol="AAPL", quantity=0.25, limit_price_usd=200.0,
            notional_cap_usd=50.0, reference_price_usd=200.0,
        )
    )
    assert out["status"] == "previewed"
    assert out["account_scope"] == "kis_mock"
    assert out["submit_enabled"] is False
    assert "alpaca_paper" not in out  # no Alpaca evidence mixed in
```

> **Note (executing agent):** `AccountStateSummary` 필드명(`buying_power_usd`)은 `app/schemas/us_dual_paper.py`에서 확인. 다르면 stub을 맞춰라.

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/services/investment_reports/mock_preview/test_bridge.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.investment_reports.mock_preview.bridge`

- [ ] **Step 3: 브리지 구현**

```python
# app/services/investment_reports/mock_preview/__init__.py
```
(빈 파일)

```python
# app/services/investment_reports/mock_preview/bridge.py
"""ROB-373 — report item -> KIS mock preview bridge (read-only, submit OFF).

Translates a projected BUY intent into a us_dual_paper preview using the
``kis_mock`` adapter ONLY (Alpaca is never invoked here — KIS mock and Alpaca
Paper evidence must not mix). Fail-closed: if the adapter is not configured the
bridge returns ``status='unsupported'`` (env key NAMES only) without any network
call. No order is ever submitted: ``submit_enabled`` is always False.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.schemas.us_dual_paper import BrokerPreviewRequest
from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter

_DEFAULT_NOTIONAL_CAP_USD = 50.0


@dataclass(frozen=True)
class OrderParams:
    symbol: str
    quantity: float
    limit_price_usd: float
    notional_cap_usd: float
    reference_price_usd: float


def extract_order_params(
    *,
    symbol: str | None,
    evidence_snapshot: dict[str, Any],
    max_action: dict[str, Any],
) -> OrderParams | None:
    """Derive deterministic limit-order params from an advisory item.

    Returns None (skip — never fabricate) when symbol or a positive reference
    price is unavailable. notional cap comes from ``max_action`` or a default.
    """
    if not symbol:
        return None
    ref = evidence_snapshot.get("reference_price_usd")
    if ref is None:
        ref = evidence_snapshot.get("price") or evidence_snapshot.get("current_price")
    try:
        ref_price = float(ref) if ref is not None else 0.0
    except (TypeError, ValueError):
        return None
    if ref_price <= 0:
        return None

    cap_raw = max_action.get("notional_usd") or max_action.get("notional_cap_usd")
    try:
        cap = float(cap_raw) if cap_raw is not None else _DEFAULT_NOTIONAL_CAP_USD
    except (TypeError, ValueError):
        cap = _DEFAULT_NOTIONAL_CAP_USD

    limit_raw = evidence_snapshot.get("limit_price_usd")
    try:
        limit = float(limit_raw) if limit_raw is not None else ref_price
    except (TypeError, ValueError):
        limit = ref_price
    if limit <= 0:
        return None

    return OrderParams(
        symbol=symbol,
        quantity=cap / limit,
        limit_price_usd=limit,
        notional_cap_usd=cap,
        reference_price_usd=ref_price,
    )


class MockPreviewBridge:
    """Produces a kis_mock preview dict for embedding into a report item."""

    def __init__(self, *, adapter: KisMockUsAdapter | None = None) -> None:
        self._adapter = adapter if adapter is not None else KisMockUsAdapter()

    async def preview(self, params: OrderParams) -> dict[str, Any]:
        # Fail-closed BEFORE any network call: adapter not configured.
        if not self._adapter.is_enabled():
            return {
                "status": "unsupported",
                "account_scope": self._adapter.account_scope,
                "submit_enabled": False,
                "missing_env_keys": self._adapter.missing_env_keys(),
            }

        req = BrokerPreviewRequest(
            symbol=params.symbol,
            quantity=params.quantity,
            limit_price_usd=params.limit_price_usd,
            notional_cap_usd=params.notional_cap_usd,
            reference_price_usd=params.reference_price_usd,
        )
        result = await self._adapter.preview(req)
        payload = result.model_dump(mode="json")
        # DualPaperBrokerStatus serializes to its value (e.g. "previewed"/"blocked").
        payload["status"] = str(payload.get("status", "")).split(".")[-1].lower()
        payload["submit_enabled"] = False  # invariant: bridge never enables submit
        return payload
```

> **Note (executing agent):** `DualPaperBrokerStatus` enum의 `model_dump(mode="json")` 직렬화 형태를 확인하라 — Enum이 `.value`("previewed")로 직렬화되면 `status` 정규화 라인은 그대로 두어도 안전(소문자 last-segment). 테스트의 `status == "previewed"` / `"unsupported"`가 통과하도록 맞춘다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/services/investment_reports/mock_preview/test_bridge.py -v`
Expected: PASS (5개). 실패 시 status 직렬화 형태에 맞춰 정규화 라인 조정.

- [ ] **Step 5: 커밋**

```bash
git add app/services/investment_reports/mock_preview/__init__.py app/services/investment_reports/mock_preview/bridge.py tests/services/investment_reports/mock_preview/__init__.py tests/services/investment_reports/mock_preview/test_bridge.py
git commit -m "feat(ROB-373): KIS mock preview bridge — fail-closed, submit off, no Alpaca mix (Unit 3)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 5: Mock 리포트 runner (Unit 2) — live item projection + ingest

**Files:**
- Create: `app/services/investment_reports/mock_preview/runner.py`
- Test: `tests/services/investment_reports/mock_preview/test_runner.py`

runner는 (1) 공유 evidence를 재사용하는 `kis_mock` 번들을 ensure, (2) live 리포트 item을 읽어 `IngestReportItem`으로 projection(`cited_snapshot_uuids` 보존 = provenance 재사용), (3) BUY action item은 브리지로 preview하여 `evidence_snapshot["mock_preview"]`에 부착, (4) `(kis_mock, mock_preview)` 리포트를 ingestion service로 직접 기록한다. live 리포트가 없거나 item이 비면 fail-closed(빈 리포트 success 위장 금지).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/services/investment_reports/mock_preview/test_runner.py
import pytest

from app.services.investment_reports.mock_preview.runner import (
    MockPreviewReportRunner,
    MockPreviewSourceMissing,
)


@pytest.mark.asyncio
async def test_runner_projects_live_items_into_mock_preview_report(
    db_session, seeded_live_report
) -> None:
    """seeded_live_report fixture: a published kis_live report with 1 BUY action
    item carrying cited_snapshot_uuids + evidence reference_price_usd."""
    runner = MockPreviewReportRunner(db_session)
    report, reused, count = await runner.run(
        live_report_uuid=seeded_live_report.report_uuid,
        market="us",
        market_session="regular",
        policy_version="intraday_action_report_v1",
        kst_date="2026-05-30",
        created_by_profile="schedule",
    )
    assert report.account_scope == "kis_mock"
    assert report.execution_mode == "mock_preview"
    assert report.status == "draft"
    assert count >= 1

    # provenance reuse: mock item cites the same snapshot uuids as the live item.
    from app.services.investment_reports.repository import InvestmentReportsRepository

    repo = InvestmentReportsRepository(db_session)
    mock_items = await repo.list_items_for_report(report.id)
    assert mock_items
    assert mock_items[0].cited_snapshot_uuids  # carried over, not dropped


@pytest.mark.asyncio
async def test_runner_fail_closed_when_live_report_missing(db_session) -> None:
    import uuid

    runner = MockPreviewReportRunner(db_session)
    with pytest.raises(MockPreviewSourceMissing):
        await runner.run(
            live_report_uuid=uuid.uuid4(),
            market="us",
            market_session="regular",
            policy_version="intraday_action_report_v1",
            kst_date="2026-05-30",
            created_by_profile="schedule",
        )
```

> **Note (executing agent):** `seeded_live_report` 픽스처는 이 테스트 파일에 직접 작성하라. 가장 단순한 경로: `InvestmentReportIngestionService(db_session).ingest(IngestReportRequest(account_scope="kis_live", execution_mode="advisory_only", market="us", market_session="regular", status="published", items=[IngestReportItem(client_item_key="seed1", item_kind="action", side="buy", intent="<intent literal>", rationale="seed", symbol="AAPL", evidence_snapshot={"reference_price_usd":200.0,"snapshot_uuid":"<a real snapshot uuid or omit>"}, max_action={"notional_usd":50.0}, cited_snapshot_uuids=[...])], report_type="snapshot_backed_advisory_v1", generator_version="v2-snapshot-backed", kst_date="2026-05-30", created_by_profile="seed", title="t", summary="s"))`. `intent`/`item_kind`의 정확한 literal 값은 `app/schemas/investment_reports.py`의 `ItemIntentLiteral`(line 45-)에서 확인. published 리포트의 freshness CHECK를 피하려면 `snapshot_freshness_summary`를 비우거나(legacy NULL 허용) status="draft"로 둔다.

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/services/investment_reports/mock_preview/test_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: ...mock_preview.runner`

- [ ] **Step 3: runner 구현**

```python
# app/services/investment_reports/mock_preview/runner.py
"""ROB-373 — mock_preview report runner (Unit 2).

Projects a kis_live advisory report's items into a kis_mock / mock_preview report,
reusing account-independent evidence (via the shared NULL-scope snapshot rows and
carried-over cited_snapshot_uuids) and attaching a KIS-mock preview to each BUY
action item. Writes through InvestmentReportIngestionService ONLY — the
snapshot-backed generator's live-only guard is never touched.
"""

from __future__ import annotations

from uuid import UUID

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.schemas.investment_reports import (
    IngestReportItem,
    IngestReportRequest,
    TargetRefPayload,
    WatchConditionPayload,
)
from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)
from app.services.investment_reports.ingestion import InvestmentReportIngestionService
from app.services.investment_reports.mock_preview.bridge import (
    MockPreviewBridge,
    extract_order_params,
)
from app.services.investment_reports.repository import InvestmentReportsRepository

_MOCK_GENERATOR_VERSION = "v2-mock-preview"


class MockPreviewSourceMissing(Exception):
    """Raised when the live source report is absent or empty (fail-closed)."""


class MockPreviewReportRunner:
    def __init__(
        self,
        session,
        *,
        bridge: MockPreviewBridge | None = None,
        ensure_service: SnapshotBundleEnsureService | None = None,
    ) -> None:
        self._session = session
        self._reports_repo = InvestmentReportsRepository(session)
        self._ingestion = InvestmentReportIngestionService(session)
        self._bridge = bridge if bridge is not None else MockPreviewBridge()
        self._ensure = (
            ensure_service
            if ensure_service is not None
            else SnapshotBundleEnsureService(session)
        )

    async def run(
        self,
        *,
        live_report_uuid: UUID,
        market: str,
        market_session: str | None,
        policy_version: str,
        kst_date: str,
        created_by_profile: str,
        user_id: int | None = None,
    ) -> tuple[InvestmentReport, bool, int]:
        live = await self._reports_repo.get_report_by_uuid(live_report_uuid)
        if live is None:
            raise MockPreviewSourceMissing(
                f"live report not found: {live_report_uuid}"
            )
        live_items = await self._reports_repo.list_items_for_report(live.id)
        if not live_items:
            raise MockPreviewSourceMissing(
                f"live report has no items: {live_report_uuid}"
            )

        # Ensure a kis_mock bundle: account-independent evidence dedups to the
        # shared NULL-scope rows; only account-bound (portfolio/...) is collected
        # fresh for kis_mock. Best-effort — partial coverage is acceptable.
        ensure_resp = await self._ensure.ensure(
            EnsureBundleRequest(
                purpose="mock_preview_report",
                market=market,  # type: ignore[arg-type]
                account_scope="kis_mock",
                policy_version=policy_version,
                mode="ensure_fresh",
                requested_by="claude_code",
                user_id=user_id,
            )
        )

        projected: list[IngestReportItem] = []
        for idx, item in enumerate(live_items):
            projected.append(await self._project(item, idx))

        request = IngestReportRequest(
            report_type=live.report_type,
            market=market,  # type: ignore[arg-type]
            market_session=market_session,  # type: ignore[arg-type]
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile=created_by_profile,
            title=f"[MOCK PREVIEW] {live.title}",
            summary=live.summary,
            risk_summary=live.risk_summary,
            thesis_text=live.thesis_text,
            no_action_note=live.no_action_note,
            status="draft",
            metadata={"mock_preview_of_report_uuid": str(live.report_uuid)},
            items=projected,
            generator_version=_MOCK_GENERATOR_VERSION,
            kst_date=kst_date,
            snapshot_bundle_uuid=ensure_resp.bundle_uuid,
            snapshot_policy_version=policy_version,
        )
        return await self._ingestion.ingest_with_outcome(request)

    async def _project(
        self, item: InvestmentReportItem, idx: int
    ) -> IngestReportItem:
        evidence = dict(item.evidence_snapshot or {})
        max_action = dict(item.max_action or {})

        # BUY action items get a KIS-mock preview embedded into evidence.
        if item.item_kind == "action" and item.side == "buy":
            params = extract_order_params(
                symbol=item.symbol, evidence_snapshot=evidence, max_action=max_action
            )
            if params is None:
                evidence["mock_preview"] = {
                    "status": "skipped",
                    "reason": "insufficient_order_params",
                    "submit_enabled": False,
                }
            else:
                evidence["mock_preview"] = await self._bridge.preview(params)

        watch_condition = (
            WatchConditionPayload.model_validate(item.watch_condition)
            if item.watch_condition
            else None
        )
        target_ref = (
            TargetRefPayload.model_validate(item.target_ref)
            if item.target_ref
            else None
        )

        return IngestReportItem(
            client_item_key=f"mockpv:{idx}:{item.item_kind}:{item.symbol or 'na'}",
            item_kind=item.item_kind,  # type: ignore[arg-type]
            operation=item.operation,  # type: ignore[arg-type]
            symbol=item.symbol,
            side=item.side,  # type: ignore[arg-type]
            intent=item.intent,  # type: ignore[arg-type]
            target_kind=item.target_kind,  # type: ignore[arg-type]
            priority=item.priority,
            confidence=item.confidence,
            rationale=item.rationale,
            evidence_snapshot=evidence,
            watch_condition=watch_condition,
            trigger_checklist=list(item.trigger_checklist or []),
            max_action=max_action,
            valid_until=item.valid_until,
            metadata=dict(item.item_metadata or {}),
            target_ref=target_ref,
            current_state=item.current_state,
            proposed_state=item.proposed_state,
            diff=item.diff,
            apply_policy="requires_user_approval",
            decision_bucket=item.decision_bucket,
            cited_snapshot_uuids=list(item.cited_snapshot_uuids or []),
        )
```

> **Note (executing agent):** ORM 속성명(`item_metadata`, `evidence_snapshot`, `max_action`, `trigger_checklist`, `current_state`, `proposed_state`, `diff`, `cited_snapshot_uuids`)은 `app/models/investment_reports.py:108-168`에서 확인된 값이다. `InvestmentReportsRepository`/`InvestmentReportIngestionService`/`SnapshotBundleEnsureService` 생성자 인자가 다르면 맞춘다. watch item에 `valid_until`이 필수인 경우 live item이 이미 만족하므로 보존된 값으로 통과한다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/services/investment_reports/mock_preview/test_runner.py -v`
Expected: PASS (2개). projection 검증 실패 시 IngestReportItem 필수 필드(`intent`,`rationale`,`client_item_key`)와 operation별 validator 규칙을 ORM 값으로 충족하는지 확인.

- [ ] **Step 5: 커밋**

```bash
git add app/services/investment_reports/mock_preview/runner.py tests/services/investment_reports/mock_preview/test_runner.py
git commit -m "feat(ROB-373): mock_preview report runner — project live items, embed preview, ingest (Unit 2)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 6: Operator CLI 오케스트레이터 (Unit 4)

**Files:**
- Create: `scripts/invest_reports_us_schedule.py`
- Test: `tests/scripts/test_invest_reports_us_schedule_cli.py`

default-disabled CLI. `--dry-run`(secret/네트워크 없이 계획만 출력), `--run`(live advisory 생성 → mock_preview runner). gate는 argparse 이후·side-effect 이전. 누락 env는 **이름만** 보고. live 생성은 기존 `investment_report_generate_from_bundle_impl`을 재사용(중복 구현 금지).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/scripts/test_invest_reports_us_schedule_cli.py
import pytest

from scripts.invest_reports_us_schedule import main


@pytest.mark.unit
def test_disabled_is_noop_exit_zero(monkeypatch, capsys) -> None:
    monkeypatch.delenv("INVEST_REPORTS_US_SCHEDULE_ENABLED", raising=False)
    rc = main(["--run"])
    assert rc == 0
    assert "disabled" in capsys.readouterr().out.lower()


@pytest.mark.unit
def test_help_works_without_env(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


@pytest.mark.unit
def test_dry_run_prints_plan_without_side_effects(monkeypatch, capsys) -> None:
    monkeypatch.setenv("INVEST_REPORTS_US_SCHEDULE_ENABLED", "true")
    rc = main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "prepare_bundle" in out
    assert "kis_live" in out and "advisory_only" in out
    assert "kis_mock" in out and "mock_preview" in out
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/scripts/test_invest_reports_us_schedule_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.invest_reports_us_schedule`

- [ ] **Step 3: CLI 구현**

```python
# scripts/invest_reports_us_schedule.py
"""ROB-373 — Claude Code schedule entrypoint for US /invest/reports.

Default-disabled operator runner. Collects common evidence once, then produces a
kis_live advisory report and a kis_mock mock_preview report (the latter via the
ROB-373 mock runner). NO live order execution, NO market orders, NO shorting.
Only KIS_MOCK_* / KIS live creds present in the schedule environment are used;
``.env.prod.native`` is never sourced wholesale.

Modes (mutually exclusive):
  --dry-run : print the planned sequence; no HTTP, no DB, no secrets required.
  --run     : execute live advisory generation + mock_preview runner.
  (no flag) : enabled-no-action guidance.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

logger = logging.getLogger("invest_reports_us_schedule")

_ENABLE_ENV = "INVEST_REPORTS_US_SCHEDULE_ENABLED"
_MARKET = "us"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROB-373 US /invest/reports schedule runner. Default-disabled "
            f"(set {_ENABLE_ENV}=true to opt in). Produces a kis_live advisory "
            "report and a kis_mock mock_preview report from shared evidence. "
            "No live order execution / market orders / shorting."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Print the planned sequence without side effects (no creds needed).",
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help="Execute live advisory generation + mock_preview runner.",
    )
    parser.add_argument("--market-session", default="regular")
    parser.add_argument("--kst-date", default=None, help="KST date YYYY-MM-DD.")
    parser.add_argument(
        "--policy-version", default="intraday_action_report_v1"
    )
    parser.add_argument("--created-by-profile", default="schedule")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


_PLAN_LINES = (
    "1. prepare_bundle(market=us)  -> shared NULL-scope evidence collected once",
    "2. generate live report       -> account_scope=kis_live, execution_mode=advisory_only",
    "3. mock_preview runner         -> account_scope=kis_mock, execution_mode=mock_preview",
    "4. mock preview bridge         -> KIS-mock read-only preflight, submit OFF (no Alpaca)",
)


def _print_plan() -> None:
    print("ROB-373 US schedule plan (dry-run, no side effects):")
    for line in _PLAN_LINES:
        print("  " + line)


async def _run(args: argparse.Namespace) -> int:
    # Lazy imports: keep --help / --dry-run / disabled path free of Settings+DB.
    from app.core.db import AsyncSessionLocal
    from app.mcp_server.tooling.investment_reports_handlers import (
        investment_report_generate_from_bundle_impl,
    )
    from app.services.investment_reports.mock_preview.runner import (
        MockPreviewReportRunner,
    )

    kst_date = args.kst_date
    if kst_date is None:
        logger.error("--kst-date is required for --run")
        return 2

    live_result = await investment_report_generate_from_bundle_impl(
        market=_MARKET,
        account_scope="kis_live",
        market_session=args.market_session,
        kst_date=kst_date,
        created_by_profile=args.created_by_profile,
        policy_version=args.policy_version,
    )
    if not live_result.get("success"):
        logger.error("live generation failed: %s", live_result.get("error"))
        return 3
    live_uuid = live_result["report_uuid"]
    logger.info("live advisory report: %s", live_uuid)

    import uuid as _uuid

    async with AsyncSessionLocal() as session:
        runner = MockPreviewReportRunner(session)
        report, reused, count = await runner.run(
            live_report_uuid=_uuid.UUID(str(live_uuid)),
            market=_MARKET,
            market_session=args.market_session,
            policy_version=args.policy_version,
            kst_date=kst_date,
            created_by_profile=args.created_by_profile,
        )
        await session.commit()
    logger.info(
        "mock_preview report: %s (reused=%s, items=%s)",
        report.report_uuid,
        reused,
        count,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    # Gate AFTER argparse (so --help works) but BEFORE any side effect.
    if not _truthy(os.environ.get(_ENABLE_ENV)):
        print(f"{_MARKET} schedule disabled — set {_ENABLE_ENV}=true to opt in")
        return 0

    if args.dry_run:
        _print_plan()
        return 0
    if args.run:
        return asyncio.run(_run(args))

    print(
        "enabled but no action requested. Pass --dry-run to print the plan or "
        "--run to generate live advisory + mock_preview reports."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
```

> **Note (executing agent):** `AsyncSessionLocal`의 정확 import 경로(`app/core/db.py` 또는 유사)와 `investment_report_generate_from_bundle_impl`의 정확 kwargs는 `app/mcp_server/tooling/investment_reports_handlers.py:449-695`에서 확인해 맞춰라(특히 필수 title/summary/items 합성 플래그 — auto_emit/auto_compose가 필요하면 전달). `--run` 경로는 라이브 creds가 필요하므로 단위 테스트는 `--dry-run`/disabled/--help만 커버한다(위 테스트가 그러함).

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/scripts/test_invest_reports_us_schedule_cli.py -v`
Expected: PASS (3개)

- [ ] **Step 5: 커밋**

```bash
git add scripts/invest_reports_us_schedule.py tests/scripts/test_invest_reports_us_schedule_cli.py
git commit -m "feat(ROB-373): default-disabled operator CLI orchestrating live advisory + mock_preview (Unit 4)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 7: Safety guard 테스트 (Unit 5) — mutation import 0, generator 가드 불변

**Files:**
- Test: `tests/services/investment_reports/mock_preview/test_no_mutation_imports.py`

mock_preview 패키지가 broker/order/watch/order-intent mutation surface를 import하지 않고, snapshot-backed generator의 live-only 가드가 여전히 kis_mock을 거부함을 증명한다. 기존 `tests/services/brokers/binance/demo_scalping/test_no_mutation_imports.py`의 AST 스캐너 패턴을 따른다.

- [ ] **Step 1: 테스트 작성**

```python
# tests/services/investment_reports/mock_preview/test_no_mutation_imports.py
import ast
import pathlib

import pytest

_PKG = pathlib.Path("app/services/investment_reports/mock_preview")

_BANNED_PREFIXES = (
    "app.services.order_service",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.kis_websocket",
    "app.services.upbit_websocket",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.alpaca_paper_ledger_service",
    "app.services.brokers.kis.mock_scalping_exec",
    "app.tasks",
)


def _imports_in_file(py: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    tree = ast.parse(py.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(module.startswith(p) for p in _BANNED_PREFIXES):
                offenders.append(f"{py}: from {module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.startswith(p) for p in _BANNED_PREFIXES):
                    offenders.append(f"{py}: import {alias.name}")
    return offenders


@pytest.mark.unit
def test_mock_preview_pkg_has_no_mutation_imports() -> None:
    offenders: list[str] = []
    for py in _PKG.rglob("*.py"):
        offenders.extend(_imports_in_file(py))
    assert offenders == [], f"mutation imports found: {offenders}"


@pytest.mark.unit
def test_bridge_never_enables_submit() -> None:
    """Static guarantee: the source asserts submit_enabled=False, never True."""
    src = (_PKG / "bridge.py").read_text()
    assert "submit_enabled\"] = False" in src or "submit_enabled'] = False" in src
    assert "submit_enabled=True" not in src


@pytest.mark.asyncio
async def test_generator_guard_still_rejects_kis_mock(db_session, monkeypatch) -> None:
    """ROB-373 must NOT relax the snapshot-backed generator's live-only guard."""
    monkeypatch.setenv("SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", "true")
    from app.mcp_server.tooling.investment_reports_handlers import (
        investment_report_generate_from_bundle_impl,
    )

    result = await investment_report_generate_from_bundle_impl(
        market="us",
        account_scope="kis_mock",
        market_session="regular",
        kst_date="2026-05-30",
        created_by_profile="schedule",
    )
    assert result["success"] is False
    assert result["error"] == "unsupported_account_scope"
```

> **Note (executing agent):** `_BANNED_PREFIXES`는 기존 report-path safety 테스트(`tests/test_no_mutation_imports.py`)의 금지 목록과 정합하도록 확인/보강하라. `investment_report_generate_from_bundle_impl`의 필수 kwargs가 더 있으면 채워 호출하되, 가드가 kwargs 검증보다 먼저 동작하면 최소 인자로 충분하다.

- [ ] **Step 2: 테스트 통과 확인**

Run: `uv run pytest tests/services/investment_reports/mock_preview/test_no_mutation_imports.py -v`
Expected: PASS (3개). `test_bridge_never_enables_submit`의 문자열 매칭이 실패하면 bridge.py의 실제 대입 표현과 일치하도록 assert를 맞춘다.

- [ ] **Step 3: 커밋**

```bash
git add tests/services/investment_reports/mock_preview/test_no_mutation_imports.py
git commit -m "test(ROB-373): mock_preview safety guard — no mutation imports, submit off, generator guard intact (Unit 5)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 8: Runbook (Unit 6)

**Files:**
- Create: `docs/runbooks/invest-reports-us-schedule.md`

- [ ] **Step 1: runbook 작성**

```markdown
# Runbook — ROB-373 US /invest/reports schedule (mock/live 분리 + 공통 evidence 재사용)

## 개요
Claude Code schedule job이 US 리포트를 자동 실행한다. 공통 evidence(market/news/
candidate/symbol)는 `account_scope=NULL` snapshot으로 한 번 수집해 재사용하고, 최종
리포트는 `kis_live`(advisory_only)와 `kis_mock`(mock_preview)로 분리한다. mock preview
리포트 item은 read-only fail-closed preflight 하에 KIS-mock preview 브리지로 연결된다.
**실주문 executor는 범위 밖(ROB-364/368 live smoke 검증 후 별도 follow-up).**

## 엔트리포인트
`uv run python -m scripts.invest_reports_us_schedule [--dry-run | --run] --kst-date YYYY-MM-DD`

- 기본: default-disabled. `INVEST_REPORTS_US_SCHEDULE_ENABLED=true` 필요.
- `--dry-run`: secret/네트워크 없이 실행 계획만 출력.
- `--run`: live advisory 생성 → mock_preview runner.

## 실행 순서
1. prepare_bundle(market=us): 공통 NULL-scope evidence 1회 수집.
2. live advisory report: account_scope=kis_live / execution_mode=advisory_only.
3. mock_preview runner: account_scope=kis_mock / execution_mode=mock_preview
   (공유 evidence 재사용 + live item projection + cited_snapshot_uuids 보존).
4. mock preview 브리지: KIS-mock 단독 read-only preflight, submit OFF.

## 안전 경계
- KIS live 주문 자동 실행 금지 / market order 금지 / shorting 금지.
- Alpaca Paper 증거와 KIS mock US 증거 혼합 금지(브리지는 KIS-mock 어댑터 단독).
- report 생성 경로 broker/order/watch/order-intent mutation 금지(AST guard 테스트).
- preflight 실패·buying power 부족 시 item BLOCKED, 실주문 미진입.
- `.env.prod.native` 전체 source 금지 — `KIS_MOCK_*`만 선택 주입.
- 로그에 계정 식별자/비밀값 노출 금지(누락 env는 이름만 보고).

## 환경 변수
- `INVEST_REPORTS_US_SCHEDULE_ENABLED` (gate, default off)
- `KIS_MOCK_ENABLED`, `KIS_MOCK_APP_KEY`, `KIS_MOCK_APP_SECRET`, `KIS_MOCK_ACCOUNT_NO`
  (mock 번들/브리지용)
- `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` (live 생성 게이트)

## 범위 밖
production scheduler 등록/unpause, prod DB backfill, prod env/secret 변경.
KIS mock US BUY/SELL executor/bridge(별도 이슈).
```

- [ ] **Step 2: 커밋**

```bash
git add docs/runbooks/invest-reports-us-schedule.md
git commit -m "docs(ROB-373): runbook for US schedule mock/live report separation (Unit 6)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 9: 전체 검증 (full suite + lint)

- [ ] **Step 1: 신규/인접 테스트 전체 실행**

Run:
```bash
uv run pytest tests/services/investment_snapshots/ tests/services/investment_reports/ tests/scripts/test_invest_reports_us_schedule_cli.py -v
```
Expected: PASS (회귀 없음).

- [ ] **Step 2: lint + import guard (pre-merge gate)**

Run:
```bash
uv run ruff check app/ tests/ scripts/
uv run pytest tests/test_no_mutation_imports.py tests/test_invest_api_router_safety.py -v
```
Expected: clean / PASS. (브랜치 보호가 lint/test를 게이트하지 않으므로 머지 전 직접 green 확인 — full Test workflow 포함.)

- [ ] **Step 3: 최종 커밋 (필요 시) + PR 준비**

`gh pr create --base main` 전에 Test workflow green을 확인. `gh pr merge --auto` 사용 시 red main 착지 위험 있으니 full CI green 확인 후 머지.

---

## Self-Review (작성자 체크 — 구현 시작 전 반영 완료)

**Spec coverage:** spec §3 Unit 1→Task 1-3, Unit 2→Task 5, Unit 3→Task 4, Unit 4→Task 6, Unit 5→Task 7, Unit 6→Task 8. AC(리포트 키 분리/provenance/generator 가드 불변/fail-closed/테스트/runbook) 모두 매핑됨.

**Placeholder scan:** 모든 코드 step에 실제 코드 포함. "Note (executing agent)"는 placeholder가 아니라 파일에서 확인할 정확 시그니처 지점을 지정(ORM 속성/생성자 인자 등)하는 지시로, 추출 워크플로로 확인된 사실에 근거. DB 마이그레이션 0건.

**Type consistency:** `OrderParams`/`extract_order_params`/`MockPreviewBridge.preview`(Task 4) ↔ runner 사용(Task 5) 일치. `normalize_account_scope`(Task 1) ↔ repository 사용(Task 2) 일치. `IngestReportItem` 필드명은 추출된 스키마와 일치. `report_key`는 기존 7-field 분리를 그대로 사용(코드 변경 없음 — 이미 account_scope/execution_mode 포함).

**알려진 위험(executing 단계에서 해소):** (a) `seeded_live_report` 픽스처의 `intent`/`item_kind` literal 정확값 확인 필요; (b) `investment_report_generate_from_bundle_impl`의 필수 kwargs(item 합성 플래그) — live 경로는 단위 테스트 미커버, dry-run/disabled만 검증; (c) `DualPaperBrokerStatus` 직렬화 형태에 따른 status 정규화 라인 미세조정.
