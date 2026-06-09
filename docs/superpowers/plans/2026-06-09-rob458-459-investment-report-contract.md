# investment_report_create 계약 노출 + advisory 근거·체이닝 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `investment_report_create`를 1발에 발행 가능하게 만들고(계약 노출 + 일괄 검증 에러), CLAUDE_ADVISOR advisory draft 체이닝과 타입드 per-item 근거를 추가한다.

**Architecture:** 3개의 독립 PR 슬라이스. (S1) MCP 핸들러의 item 검증을 옆 도구(`generate_from_bundle`)에 이미 있는 일괄-에러 패턴으로 공용 헬퍼 추출해 양쪽이 공유 + 도구 description/CLAUDE.md에 계약 명시. (S2) advisory-draft 화이트리스트를 default∪config(fail-closed)로 확장. (S3) `ItemEvidencePayload`를 additive 추가, 기존 `evidence_snapshot` JSONB에 reserved key로 병합(마이그레이션 0, 읽기경로 무비용 round-trip).

**Tech Stack:** Python 3.13, FastMCP, Pydantic v2, SQLAlchemy async, pytest(asyncio). 스펙: `docs/superpowers/specs/2026-06-09-rob458-459-investment-report-contract-design.md`.

**PR/브랜치 순서:** S1(ROB-458) → S2(ROB-459 P3) → S3(ROB-459 P1). S1은 현재 워크트리(`auto_trader.rob-458`, branch `rob-458`)에서. S2/S3는 ROB-459이므로 S1 머지 후 `auto_trader.rob-459` 워크트리를 `origin/main`에서 새로 만들어 진행(CLAUDE.md worktree 규칙). 슬라이스 간 코드 충돌은 `CREATE_DESCRIPTION` 상수 한 곳뿐(각 슬라이스가 리터럴에 한 줄 추가).

**전 슬라이스 공통 제약:** 브로커/주문/감시/order-intent mutation 없음. 마이그레이션 0(권장안). 모두 additive 또는 에러-UX 개선.

---

## Slice 1 — ROB-458: 계약 노출 + 일괄 검증 에러 (PR 1)

**파일 맵**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` — 공용 헬퍼 `_validate_report_items` 추출, `generate_from_bundle`/`create` 양쪽 배선, `CREATE_DESCRIPTION` 상수 + 등록부 사용.
- Modify: `CLAUDE.md` — 투자 리포트 섹션에 item 계약 운영노트 신규 추가.
- Test: `tests/mcp_server/test_investment_report_create_handler.py` (신규), `tests/mcp_server/test_investment_report_generate_from_bundle_handler.py` (회귀).

---

### Task 1.1: 공용 검증 헬퍼 `_validate_report_items` 추출

**파일**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` (현재 inline 블록 `:694-759`)
- Test: `tests/mcp_server/test_investment_report_create_handler.py` (신규)

- [ ] **Step 1: 헬퍼 단위 테스트 작성 (실패 예상)**

신규 파일 `tests/mcp_server/test_investment_report_create_handler.py`:

```python
"""ROB-458 — investment_report_create 계약/일괄검증 핸들러 테스트.

검증 단계는 DB 세션을 열기 전에 단락(short-circuit)되므로 DB 픽스처 불필요.
"""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import investment_reports_handlers as h

pytestmark = pytest.mark.unit


def test_validate_report_items_collects_all_missing_fields_at_once():
    # client_item_key + intent + rationale 누락 — 세 위반이 한 응답에 모여야 한다.
    items = [{"item_kind": "action"}]
    validated, error = h._validate_report_items(items)
    assert validated == []
    assert error is not None
    assert error["error"] == "invalid_items"
    fields = {e["field"] for e in error["item_errors"][0]["errors"]}
    assert {"client_item_key", "intent", "rationale"} <= fields


def test_validate_report_items_reports_bad_enum_with_enum_block():
    items = [
        {
            "client_item_key": "k1",
            "item_kind": "action",
            "intent": "not_a_real_intent",
            "rationale": "r",
        }
    ]
    _validated, error = h._validate_report_items(items)
    assert error is not None
    assert "intent" in str(error["item_errors"][0]["errors"])
    assert error["enums"]["item_kind"] == ["action", "watch", "risk"]


def test_validate_report_items_non_dict_does_not_crash():
    _validated, error = h._validate_report_items(["not-a-dict"])
    assert error is not None
    assert error["item_errors"][0]["index"] == 0
    assert "object" in str(error["item_errors"][0]["errors"])


def test_validate_report_items_happy_path_returns_items_and_no_error():
    items = [
        {
            "client_item_key": "k1",
            "item_kind": "action",
            "intent": "buy_review",
            "rationale": "r",
        }
    ]
    validated, error = h._validate_report_items(items)
    assert error is None
    assert len(validated) == 1
    assert validated[0].client_item_key == "k1"


def test_validate_report_items_only_flags_the_bad_index():
    items = [
        {
            "client_item_key": "ok",
            "item_kind": "action",
            "intent": "buy_review",
            "rationale": "r",
        },
        {"item_kind": "action"},  # bad
    ]
    _validated, error = h._validate_report_items(items)
    assert error is not None
    assert [e["index"] for e in error["item_errors"]] == [1]
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/test_investment_report_create_handler.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_validate_report_items'`

- [ ] **Step 3: 헬퍼 구현 (기존 inline 블록을 추출)**

`investment_reports_handlers.py`에 모듈-레벨 함수 추가(예: `generate_from_bundle` impl 위 또는 파일 상단 헬퍼 영역). 본문은 현재 `:694-759` 블록을 그대로 이동하되 시그니처를 `(raw_items) -> (items, error|None)`로:

```python
def _validate_report_items(
    raw_items: list[dict[str, Any]] | None,
) -> tuple[list[IngestReportItem], dict[str, Any] | None]:
    """Validate report items with per-item, per-field errors (ROB-458).

    Returns ``(validated_items, error_payload)``. ``error_payload`` is None on
    success; otherwise a structured MCP error dict naming EVERY offending item
    index / client_item_key / field so the caller fixes all violations in one
    round-trip. Shared by investment_report_create and
    investment_report_generate_from_bundle so the two cannot drift.
    """
    validated_items: list[IngestReportItem] = []
    item_errors: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items or []):
        if not isinstance(raw, dict):
            item_errors.append(
                {
                    "index": index,
                    "client_item_key": None,
                    "errors": [
                        {
                            "field": "",
                            "message": (
                                f"item must be an object, got {type(raw).__name__}"
                            ),
                        }
                    ],
                }
            )
            continue
        try:
            validated_items.append(IngestReportItem.model_validate(raw))
        except ValidationError as exc:
            item_errors.append(
                {
                    "index": index,
                    "client_item_key": raw.get("client_item_key"),
                    "errors": [
                        {
                            "field": ".".join(str(p) for p in err["loc"]),
                            "message": err["msg"],
                        }
                        for err in exc.errors()
                    ],
                }
            )
    if item_errors:
        return [], {
            "success": False,
            "error": "invalid_items",
            "item_errors": item_errors,
            "required_fields": [
                "client_item_key",
                "item_kind",
                "intent",
                "rationale",
            ],
            "enums": {
                "item_kind": ["action", "watch", "risk"],
                "intent": [
                    "buy_review",
                    "sell_review",
                    "risk_review",
                    "trend_recovery_review",
                    "rebalance_review",
                ],
                "target_kind": ["asset", "index", "fx"],
                "side": ["buy", "sell"],
            },
            "notes": (
                "watch items require watch_condition + valid_until unless "
                "operation is 'review'; decision_bucket must be one of the "
                "DECISION_BUCKETS vocabulary. target_kind is a SEPARATE optional "
                "field (asset|index|fx, default 'asset') — it is NOT item_kind."
            ),
        }
    return validated_items, None
```

(`IngestReportItem`, `ValidationError`, `Any`는 이미 이 모듈에 import 되어 있음 — line 283/716에서 사용 중.)

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/mcp_server/test_investment_report_create_handler.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: `generate_from_bundle`를 헬퍼로 치환**

`investment_report_generate_from_bundle_impl` 내 현재 item 검증 블록(`:694-759`)을 다음으로 교체:

```python
    validated_items, item_error = _validate_report_items(items)
    if item_error is not None:
        return item_error
```

이후 라인(`overwrite_existing` 가드 `:764-`, payload 구성에서 `"items": validated_items`)은 그대로 유지.

- [ ] **Step 6: 회귀 확인 (generate 기존 테스트 byte-동형)**

Run: `uv run pytest tests/mcp_server/test_investment_report_generate_from_bundle_handler.py -v`
Expected: PASS. (유일한 동작 변화는 에러 응답의 `notes` 문자열에 target_kind 명료화 한 문장 추가 — 어떤 테스트도 `notes` 본문을 단언하지 않으므로 그린 유지.)

- [ ] **Step 7: 커밋**

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py tests/mcp_server/test_investment_report_create_handler.py
git commit -m "refactor(ROB-458): item 검증을 공용 _validate_report_items 헬퍼로 추출

generate_from_bundle의 일괄 item_errors 패턴을 모듈 헬퍼로 추출해
create/generate 양쪽이 공유(drift 방지). 동작 동일, notes에 target_kind
명료화 추가.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 1.2: `investment_report_create_impl` 배선 + `CREATE_DESCRIPTION`

**파일**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` (`:283` 검증, `:849-857` 등록부)
- Test: `tests/mcp_server/test_investment_report_create_handler.py`

- [ ] **Step 1: create 핸들러/description 테스트 추가 (실패 예상)**

`tests/mcp_server/test_investment_report_create_handler.py`에 append:

```python
def _kwargs(**overrides):
    base = {
        "report_type": "snapshot_backed_advisory_v1",
        "market": "us",
        "summary": "s",
        "created_by_profile": "claude_code",
        "title": "t",
        "kst_date": "2026-06-09",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_create_invalid_item_returns_structured_error_without_db():
    # client_item_key 누락 — DB 세션을 열기 전에 구조화 에러로 단락되어야 한다.
    res = await h.investment_report_create_impl(
        **_kwargs(
            items=[{"item_kind": "action", "intent": "buy_review", "rationale": "r"}]
        )
    )
    assert res["success"] is False
    assert res["error"] == "invalid_items"
    assert res["item_errors"][0]["index"] == 0
    assert "client_item_key" in str(res["item_errors"][0]["errors"])


@pytest.mark.asyncio
async def test_create_invalid_enum_names_the_field():
    res = await h.investment_report_create_impl(
        **_kwargs(
            items=[
                {
                    "client_item_key": "k1",
                    "item_kind": "action",
                    "intent": "not_a_real_intent",
                    "rationale": "r",
                }
            ]
        )
    )
    assert res["success"] is False
    assert res["error"] == "invalid_items"
    assert "intent" in str(res["item_errors"][0]["errors"])


def test_create_tool_description_documents_item_contract():
    desc = h.CREATE_DESCRIPTION
    assert "client_item_key" in desc
    assert "item_kind" in desc
    assert "action|watch|risk" in desc
    # item_kind vs target_kind 혼동 방지 문구가 반드시 노출되어야 한다(ROB-458 핵심).
    assert "target_kind" in desc
    assert "NOT item_kind" in desc
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/test_investment_report_create_handler.py -k "create_" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'CREATE_DESCRIPTION'` 및 invalid 경로가 아직 uncaught ValidationError.

- [ ] **Step 3: `CREATE_DESCRIPTION` 상수 추가**

`GENERATE_FROM_BUNDLE_DESCRIPTION` 정의 근처에 모듈-레벨 상수 추가:

```python
CREATE_DESCRIPTION = (
    "Persist one ROB-265 investment_report bundle (report + items). "
    "Idempotent on (report_type, market, market_session, account_scope, "
    "execution_mode, kst_date, generator_version). No broker / order "
    "submission is performed. "
    "items[] each require: client_item_key, item_kind (action|watch|risk), "
    "intent (buy_review|sell_review|risk_review|trend_recovery_review|"
    "rebalance_review), rationale. "
    "watch items also require watch_condition + valid_until unless "
    "operation='review'. "
    "target_kind (asset|index|fx, default 'asset') is a SEPARATE optional field "
    "— it is NOT item_kind. "
    "decision_bucket (optional) must be one of: new_buy_candidate, open_action, "
    "completed_or_existing, deferred_no_action, risk_watch."
)
```

- [ ] **Step 4: `investment_report_create_impl` 배선**

`:264-287` 구간을 수정 — payload 구성 전에 헬퍼로 검증하고, item 에러면 즉시 반환:

```python
async def investment_report_create_impl(
    report_type: str,
    market: str,
    summary: str,
    created_by_profile: str,
    title: str,
    kst_date: str,
    items: list[dict[str, Any]] | None = None,
    market_session: str | None = None,
    account_scope: str | None = None,
    execution_mode: str = "advisory_only",
    risk_summary: str | None = None,
    thesis_text: str | None = None,
    no_action_note: str | None = None,
    market_snapshot: dict[str, Any] | None = None,
    portfolio_snapshot: dict[str, Any] | None = None,
    previous_report_uuid: str | None = None,
    status: str = "draft",
    metadata: dict[str, Any] | None = None,
    valid_until: str | None = None,
    published_at: str | None = None,
    generator_version: str = "v1",
) -> dict:
    # ROB-458 — validate items with per-item, all-at-once errors BEFORE opening
    # a DB session, so a malformed call never gets a partial write or a raw
    # ValidationError. Mirrors investment_report_generate_from_bundle.
    validated_items, item_error = _validate_report_items(items)
    if item_error is not None:
        return item_error

    payload: dict[str, Any] = {
        "report_type": report_type,
        "market": market,
        "market_session": market_session,
        "account_scope": account_scope,
        "execution_mode": execution_mode,
        "created_by_profile": created_by_profile,
        "title": title,
        "summary": summary,
        "risk_summary": risk_summary,
        "thesis_text": thesis_text,
        "no_action_note": no_action_note,
        "market_snapshot": market_snapshot or {},
        "portfolio_snapshot": portfolio_snapshot or {},
        "previous_report_uuid": previous_report_uuid,
        "status": status,
        "metadata": metadata or {},
        "valid_until": valid_until,
        "published_at": published_at,
        "items": validated_items,
        "generator_version": generator_version,
        "kst_date": kst_date,
    }
    request = IngestReportRequest.model_validate(payload)
    # ... (이후 idempotency 프로브 / ingest / 응답 구성은 기존 :289-315 그대로) ...
```

- [ ] **Step 5: 등록부에서 `CREATE_DESCRIPTION` 사용**

`:849-857`의 inline description을 상수로 교체:

```python
    mcp.tool(
        name="investment_report_create",
        description=CREATE_DESCRIPTION,
    )(investment_report_create_impl)
```

- [ ] **Step 6: 통과 확인**

Run: `uv run pytest tests/mcp_server/test_investment_report_create_handler.py -v`
Expected: PASS (전체)

- [ ] **Step 7: 커밋**

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py tests/mcp_server/test_investment_report_create_handler.py
git commit -m "fix(ROB-458): investment_report_create 일괄 검증 에러 + 계약 description

items 검증을 _validate_report_items로 단락 — 첫 실패 throw 대신 모든 위반을
한 응답에. CREATE_DESCRIPTION에 필수 필드/enum/watch 규칙 및 item_kind vs
target_kind 구분 명시.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 1.3: CLAUDE.md 운영노트 추가 (전제 정정 반영)

**파일**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 투자 리포트 섹션에 item 계약 노트 추가**

`CLAUDE.md`의 Hermes Report Generation(ROB-287) 섹션 바로 아래에 신규 블록 추가:

```markdown
### investment_report_create item 계약 (ROB-458)

`investment_report_create`의 `items[]` 각 항목 필수/선택 필드:

- **필수**: `client_item_key`(비어있지 않은 str), `item_kind ∈ {action, watch, risk}`,
  `intent ∈ {buy_review, sell_review, risk_review, trend_recovery_review, rebalance_review}`,
  `rationale`(자유 텍스트 근거).
- **watch 규칙**: `item_kind="watch"`이고 `operation ∈ {None, create, modify}`이면
  `watch_condition` + `valid_until` 필수(`operation="review"`면 면제).
- **선택**: `target_kind ∈ {asset, index, fx}`(기본 `asset`) — **`item_kind`와 별개**이며
  watch 스캐너의 asset/index/fx dispatch용. (자산종류이지 항목 종류가 아님.)
  `decision_bucket ∈ {new_buy_candidate, open_action, completed_or_existing,
  deferred_no_action, risk_watch}`, `side ∈ {buy, sell}`, `symbol`, `confidence`,
  `evidence_snapshot`(비정형 dict) 등.

잘못된 item은 단일 응답으로 모든 위반을 반환한다
(`{success:false, error:"invalid_items", item_errors:[...], required_fields, enums, notes}`).
```

- [ ] **Step 2: 커밋**

```bash
git add CLAUDE.md
git commit -m "docs(ROB-458): investment_report_create item 계약 운영노트 추가

전제 정정: 기존에 틀린 target_kind 노트가 있었던 게 아니라 계약 노트 자체가
부재했음 — item_kind vs target_kind 구분 포함해 신규 추가.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 3: 슬라이스 전체 검증**

Run: `uv run pytest tests/mcp_server/test_investment_report_create_handler.py tests/mcp_server/test_investment_report_generate_from_bundle_handler.py -v`
Run: `uv run ruff check app/mcp_server/tooling/investment_reports_handlers.py tests/mcp_server/test_investment_report_create_handler.py`
Expected: 전체 PASS, ruff clean. → PR 1 생성 준비 완료.

---

## Slice 2 — ROB-459 P3: advisory-draft 체이닝 (PR 2)

**전제:** S1 머지 후 `auto_trader.rob-459` 워크트리를 `origin/main`에서 새로 생성하여 진행.

**파일 맵**
- Modify: `app/core/config.py` — `INVESTMENT_ADVISORY_DRAFT_PROFILES` 설정 + comma-sep 검증.
- Modify: `app/services/investment_reports/query_service.py` — default∪config 화이트리스트.
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` — context_get description 갱신.
- Test: `tests/test_advisory_draft_profiles_config.py`(신규), `tests/test_investment_reports_query_prior_drafts.py`(확장).

---

### Task 2.1: config 설정 + comma-sep 검증

**파일**
- Modify: `app/core/config.py` (`PUBLIC_API_PATHS` 검증 `:609-627` 패턴 미러)
- Test: `tests/test_advisory_draft_profiles_config.py` (신규)

- [ ] **Step 1: config 파싱 테스트 작성 (실패 예상)**

신규 `tests/test_advisory_draft_profiles_config.py`:

```python
"""ROB-459 P3 — INVESTMENT_ADVISORY_DRAFT_PROFILES 파싱."""

from __future__ import annotations

import pytest

from app.core.config import Settings

pytestmark = pytest.mark.unit


def test_advisory_profiles_default_empty():
    s = Settings()
    assert s.INVESTMENT_ADVISORY_DRAFT_PROFILES == []


def test_advisory_profiles_parses_comma_separated():
    s = Settings(INVESTMENT_ADVISORY_DRAFT_PROFILES="OPERATOR_ADVISOR, FOO_ADVISOR")
    assert s.INVESTMENT_ADVISORY_DRAFT_PROFILES == ["OPERATOR_ADVISOR", "FOO_ADVISOR"]


def test_advisory_profiles_parses_json_list():
    s = Settings(INVESTMENT_ADVISORY_DRAFT_PROFILES='["A_ADVISOR", "B_ADVISOR"]')
    assert s.INVESTMENT_ADVISORY_DRAFT_PROFILES == ["A_ADVISOR", "B_ADVISOR"]


def test_advisory_profiles_blank_is_empty():
    s = Settings(INVESTMENT_ADVISORY_DRAFT_PROFILES="")
    assert s.INVESTMENT_ADVISORY_DRAFT_PROFILES == []
```

(주의: `Settings()` 직접 인스턴스화가 다른 필수 env를 요구하면, 기존 config 테스트의 픽스처/기본 env 패턴을 따른다 — `tests/` 내 `Settings(` 직접 생성 선례를 grep해서 동일 방식 사용.)

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_advisory_draft_profiles_config.py -v`
Expected: FAIL — `INVESTMENT_ADVISORY_DRAFT_PROFILES` 속성 없음.

- [ ] **Step 3: 설정 필드 + 검증 추가**

`app/core/config.py` Settings 클래스에 필드 추가(다른 `INVESTMENT_*` 플래그 근처, 예: `INVESTMENT_SNAPSHOTS_MCP_ENABLED` 인접):

```python
    # ROB-459 P3 — context_get(draft_policy="advisory_only")에서 baseline으로
    # admit할 advisory 프로필을 운영자가 확장(default와 UNION). 빈 값이면 기본
    # {HERMES_ADVISOR, CLAUDE_ADVISOR}만. 스모크/테스트 프로필은 명시하지 않는 한 제외.
    INVESTMENT_ADVISORY_DRAFT_PROFILES: list[str] = []
```

그리고 `validate_public_api_paths`(`:609-627`) 바로 아래에 동일 패턴 검증 추가:

```python
    @field_validator("INVESTMENT_ADVISORY_DRAFT_PROFILES", mode="before")
    @classmethod
    def _parse_advisory_draft_profiles(cls, v: list[str] | str) -> list[str]:
        """Parse comma-separated or JSON-list env into a clean profile list."""
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return []
            if value.startswith("["):
                parsed = json.loads(value)
                if not isinstance(parsed, list) or not all(
                    isinstance(p, str) for p in parsed
                ):
                    raise ValueError(
                        "INVESTMENT_ADVISORY_DRAFT_PROFILES JSON value must be a "
                        "string list"
                    )
                return [p.strip() for p in parsed if p.strip()]
            return [p.strip() for p in value.split(",") if p.strip()]
        return v or []
```

(`json`은 `validate_public_api_paths`가 이미 사용 중 — 별도 import 불필요.)

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_advisory_draft_profiles_config.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: 커밋**

```bash
git add app/core/config.py tests/test_advisory_draft_profiles_config.py
git commit -m "feat(ROB-459): INVESTMENT_ADVISORY_DRAFT_PROFILES 설정(comma-sep) 추가

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2.2: query_service 런타임 화이트리스트(default∪config)

**파일**
- Modify: `app/services/investment_reports/query_service.py` (`:43`, `:54-56`)
- Test: `tests/test_investment_reports_query_prior_drafts.py` (확장)

- [ ] **Step 1: 동작 테스트 확장 (실패 예상)**

`tests/test_investment_reports_query_prior_drafts.py`에 append:

```python
@pytest.mark.asyncio
async def test_prior_reports_advisory_only_admits_claude_advisor(
    session: AsyncSession,
) -> None:
    """ROB-459 P3 — CLAUDE_ADVISOR draft도 advisory_only에서 admit, 스모크는 제외."""
    repo = InvestmentReportsRepository(session)
    await _make_report(repo, key="pub:1", status="published", title="real-1")
    await _make_report(
        repo, key="draft:smoke", status="draft", title="smoke-1", created_by_profile="t"
    )
    await _make_report(
        repo,
        key="draft:claude",
        status="draft",
        title="claude-adv-1",
        created_by_profile="CLAUDE_ADVISOR",
    )
    svc = InvestmentReportQueryService(session)
    ctx = await svc.previous_report_context(
        market="us",
        account_scope="kis_live",
        report_type="snapshot_backed_advisory_v1",
        n_prior=4,
        draft_policy="advisory_only",
    )
    titles = {r.title for r in ctx["prior_reports"]}
    assert "claude-adv-1" in titles
    assert "smoke-1" not in titles


@pytest.mark.asyncio
async def test_prior_reports_advisory_only_honors_config_profiles(
    session: AsyncSession, monkeypatch
) -> None:
    """운영자 설정 프로필이 default와 UNION으로 admit된다."""
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "INVESTMENT_ADVISORY_DRAFT_PROFILES", ["OPERATOR_ADVISOR"],
        raising=False,
    )
    repo = InvestmentReportsRepository(session)
    await _make_report(repo, key="pub:1", status="published", title="real-1")
    await _make_report(
        repo,
        key="draft:op",
        status="draft",
        title="op-adv-1",
        created_by_profile="OPERATOR_ADVISOR",
    )
    # 기본값도 여전히 admit되어야 한다(UNION).
    await _make_report(
        repo,
        key="draft:hermes",
        status="draft",
        title="hermes-adv-1",
        created_by_profile="HERMES_ADVISOR",
    )
    svc = InvestmentReportQueryService(session)
    ctx = await svc.previous_report_context(
        market="us",
        account_scope="kis_live",
        report_type="snapshot_backed_advisory_v1",
        n_prior=4,
        draft_policy="advisory_only",
    )
    titles = {r.title for r in ctx["prior_reports"]}
    assert {"op-adv-1", "hermes-adv-1"} <= titles
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_investment_reports_query_prior_drafts.py -v`
Expected: FAIL — CLAUDE_ADVISOR/OPERATOR_ADVISOR가 아직 제외됨.

- [ ] **Step 3: 다른 참조 확인**

Run: `git grep -n "_ADVISORY_DRAFT_PROFILES"`
Expected: `query_service.py`의 정의/사용만 (다른 모듈 참조 없음 확인 — 있으면 함께 갱신).

- [ ] **Step 4: 런타임 화이트리스트 구현**

`query_service.py:43`의 상수를 default로 rename하고 합성 함수 추가:

```python
# ROB-459 P3 — default advisory profiles. CLAUDE_ADVISOR added so Claude-authored
# advisory drafts chain as baseline; operators extend via config (UNION only).
_DEFAULT_ADVISORY_DRAFT_PROFILES: frozenset[str] = frozenset(
    {"HERMES_ADVISOR", "CLAUDE_ADVISOR"}
)


def _advisory_draft_profiles() -> frozenset[str]:
    """Runtime advisory whitelist: built-in defaults UNION operator config.

    Union-only / fail-closed: operators may ADD genuine advisory profiles via
    INVESTMENT_ADVISORY_DRAFT_PROFILES but cannot drop a default or admit every
    draft (there is still no 'all' policy). Smoke/test profiles ('t', 'test', …)
    stay excluded unless explicitly listed.
    """
    from app.core.config import settings

    extra = frozenset(settings.INVESTMENT_ADVISORY_DRAFT_PROFILES or [])
    return _DEFAULT_ADVISORY_DRAFT_PROFILES | extra
```

그리고 `_is_advisory_draft`(`:54-56`)가 합성 함수를 쓰도록:

```python
def _is_advisory_draft(report: InvestmentReport) -> bool:
    """True when a draft report is a genuine advisory baseline (not smoke)."""
    return report.created_by_profile in _advisory_draft_profiles()
```

기존 주석 블록(`:37-42`)에 CLAUDE_ADVISOR 포함 사유를 한 줄 보강.

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/test_investment_reports_query_prior_drafts.py -v`
Expected: PASS (기존 3 + 신규 2). 특히 기존 `test_prior_reports_advisory_only_includes_advisory_excludes_smoke`(HERMES admit, 't' 제외)도 그대로 그린.

- [ ] **Step 6: 커밋**

```bash
git add app/services/investment_reports/query_service.py tests/test_investment_reports_query_prior_drafts.py
git commit -m "feat(ROB-459): advisory-draft 체이닝에 CLAUDE_ADVISOR + config UNION

_ADVISORY_DRAFT_PROFILES를 default(HERMES_ADVISOR, CLAUDE_ADVISOR) ∪
INVESTMENT_ADVISORY_DRAFT_PROFILES로 합성. fail-closed 유지(스모크 제외, no 'all').

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2.3: context_get description 갱신

**파일**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` (`:893-905`)
- Test: `tests/mcp_server/test_investment_report_create_handler.py` 또는 신규 description 테스트

- [ ] **Step 1: description 테스트 작성 (실패 예상)**

`tests/mcp_server/test_investment_report_context_get_description.py` (신규):

```python
"""ROB-459 P3 — context_get description이 확장된 advisory 집합을 반영."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import investment_reports_handlers as h

pytestmark = pytest.mark.unit


def test_context_get_description_mentions_claude_advisor():
    # 등록부 description 문자열을 직접 검사. 등록부가 인라인이면 상수로 승격 후 검사.
    desc = h.CONTEXT_GET_DESCRIPTION
    assert "CLAUDE_ADVISOR" in desc
    assert "advisory_only" in desc
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/test_investment_report_context_get_description.py -v`
Expected: FAIL — `CONTEXT_GET_DESCRIPTION` 없음.

- [ ] **Step 3: description을 상수로 승격 + 문구 갱신**

`:893-905`의 인라인 description을 모듈 상수로 추출하고 advisory 집합 문구 갱신:

```python
CONTEXT_GET_DESCRIPTION = (
    "Return previous-report context for the next-report generator: "
    "prior_reports, unresolved_deferred_items, active_watches, "
    "triggered_events, recent_decisions. n_prior clamped to 1..10. "
    "draft_policy (optional, default 'exclude'): 'exclude' drops all draft "
    "reports; 'advisory_only' admits genuine advisory drafts "
    "(created_by_profile in HERMES_ADVISOR / CLAUDE_ADVISOR, plus any profiles "
    "configured via INVESTMENT_ADVISORY_DRAFT_PROFILES) as prior context while "
    "still excluding smoke/test drafts. advisory reports persist as draft, so "
    "use 'advisory_only' to chain the next delta report off the latest advisory "
    "baseline. (Unknown values fall back to 'exclude'.)"
)
```

등록부:

```python
    mcp.tool(
        name="investment_report_context_get",
        description=CONTEXT_GET_DESCRIPTION,
    )(investment_report_context_get_impl)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/mcp_server/test_investment_report_context_get_description.py -v`
Expected: PASS

- [ ] **Step 5: 커밋 + 슬라이스 검증**

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py tests/mcp_server/test_investment_report_context_get_description.py
git commit -m "docs(ROB-459): context_get description에 CLAUDE_ADVISOR/config 반영

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

Run: `uv run pytest tests/test_advisory_draft_profiles_config.py tests/test_investment_reports_query_prior_drafts.py tests/mcp_server/test_investment_report_context_get_description.py -v && uv run ruff check app/core/config.py app/services/investment_reports/query_service.py`
Expected: 전체 PASS, ruff clean. → PR 2 준비 완료.

---

## Slice 3 — ROB-459 P1: 타입드 per-item evidence (PR 3)

**전제:** S2와 같은 `auto_trader.rob-459` 워크트리에서 S2 머지 후 새 브랜치(`origin/main` 기준), 또는 S2 PR과 stacked. 충돌 없음(서로 다른 파일).

**파일 맵**
- Modify: `app/schemas/investment_reports.py` — `ItemEvidenceFreshnessLiteral`, `ItemEvidencePayload`, `IngestReportItem`에 `evidence`/`freshness` 추가.
- Modify: `app/services/investment_reports/ingestion.py` (`_insert_item` `:285-343`) — evidence_snapshot 병합.
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` — `CREATE_DESCRIPTION`에 evidence 한 줄.
- Test: `tests/test_investment_report_item_evidence.py` (신규).

---

### Task 3.1: `ItemEvidencePayload` 스키마 추가

**파일**
- Modify: `app/schemas/investment_reports.py` (`:44-` Literal 영역, `:217-256` IngestReportItem)
- Test: `tests/test_investment_report_item_evidence.py` (신규)

- [ ] **Step 1: 스키마 테스트 작성 (실패 예상)**

신규 `tests/test_investment_report_item_evidence.py`:

```python
"""ROB-459 P1 — 타입드 per-item evidence 스키마/영속화."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.schemas.investment_reports import IngestReportItem, ItemEvidencePayload

pytestmark = pytest.mark.unit


def test_evidence_payload_requires_source():
    with pytest.raises(Exception):
        ItemEvidencePayload(metric="buy_ratings", value=10)  # source 누락


def test_item_accepts_structured_evidence():
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="action",
        intent="buy_review",
        rationale="컨센 10buy",
        evidence=[
            {"source": "consensus", "metric": "buy_ratings", "value": 10},
            {
                "source": "foreign_flow",
                "metric": "net",
                "value": "1.2e9",
                "as_of": "2026-06-09",
                "freshness": "fresh",
            },
        ],
        freshness="fresh",
    )
    assert len(item.evidence) == 2
    assert item.evidence[0].source == "consensus"
    assert item.freshness == "fresh"


def test_item_evidence_defaults_empty():
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="action",
        intent="buy_review",
        rationale="r",
    )
    assert item.evidence == []
    assert item.freshness is None


def test_evidence_value_decimal_and_str_round_trip_json():
    payload = ItemEvidencePayload(source="s", value=Decimal("10.5"))
    dumped = payload.model_dump(mode="json")
    assert dumped["value"] == "10.5"  # Decimal → JSON string
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_investment_report_item_evidence.py -v`
Expected: FAIL — `ItemEvidencePayload` import 불가 / `evidence` 필드 없음.

- [ ] **Step 3: 스키마 구현**

`app/schemas/investment_reports.py` Literal 영역(`:56` 근처)에 추가:

```python
ItemEvidenceFreshnessLiteral = Literal["fresh", "soft_stale", "stale", "unknown"]
```

`IngestReportItem` 클래스 정의 위(예: `WatchConditionPayload` 등 다른 submodel 인접)에 추가:

```python
class ItemEvidencePayload(BaseModel):
    """ROB-459 P1 — 하나의 구조적 evidence 행(컨센/수급/종토 등 소스 연결).

    자유 텍스트 rationale을 대체하지 않고 보강한다. ``source`` 외 전 필드 선택.
    영속화는 마이그레이션 없이 ``evidence_snapshot['structured_evidence']``에
    병합되어 InvestmentReportItemResponse.evidence_snapshot으로 round-trip한다.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    metric: str | None = None
    value: Decimal | str | None = None
    as_of: datetime | str | None = None
    freshness: ItemEvidenceFreshnessLiteral | None = None
```

`IngestReportItem`의 `evidence_snapshot`(`:236`) 바로 아래에 additive 필드 추가:

```python
    # ROB-459 P1 — 타입드 구조 근거(선택). evidence_snapshot(비정형)을 보강.
    evidence: list[ItemEvidencePayload] = Field(default_factory=list)
    freshness: ItemEvidenceFreshnessLiteral | None = None
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_investment_report_item_evidence.py -v`
Expected: PASS (4 tests). (만약 smart-union이 `Decimal("10.5")`를 다르게 직렬화하면 `value: Decimal | str` 순서/단언을 실제 동작에 맞춰 조정 — Pydantic v2 mode="json"의 Decimal→str이 기본.)

- [ ] **Step 5: 커밋**

```bash
git add app/schemas/investment_reports.py tests/test_investment_report_item_evidence.py
git commit -m "feat(ROB-459): IngestReportItem에 타입드 evidence/freshness 추가(additive)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3.2: `_insert_item` evidence_snapshot 병합(no-migration round-trip)

**파일**
- Modify: `app/services/investment_reports/ingestion.py` (`:285-324`)
- Test: `tests/test_investment_report_item_evidence.py` (DB round-trip 추가)

- [ ] **Step 1: round-trip 테스트 추가 (실패 예상)**

`tests/test_investment_report_item_evidence.py`에 append:

```python
@pytest.mark.asyncio
async def test_structured_evidence_round_trips_through_ingestion(session) -> None:
    """create→저장→read에서 structured_evidence/item_freshness가 노출된다."""
    from app.schemas.investment_reports import IngestReportItem, IngestReportRequest
    from app.services.investment_reports.ingestion import (
        InvestmentReportIngestionService,
    )
    from app.services.investment_reports.repository import InvestmentReportsRepository

    repo = InvestmentReportsRepository(session)
    svc = InvestmentReportIngestionService(session, repository=repo)
    report = await svc.ingest(
        IngestReportRequest(
            report_type="advisory_lite_v1",
            market="kr",
            created_by_profile="CLAUDE_ADVISOR",
            title="t",
            summary="s",
            kst_date="2026-06-09",
            status="draft",
            items=[
                IngestReportItem(
                    client_item_key="k1",
                    item_kind="action",
                    intent="buy_review",
                    rationale="컨센 10buy / 외국인 순매수",
                    evidence=[
                        {"source": "consensus", "metric": "buy_ratings", "value": 10},
                    ],
                    freshness="fresh",
                )
            ],
        )
    )
    await session.flush()
    items = await repo.list_items_for_report(report.id)
    assert len(items) == 1
    snap = items[0].evidence_snapshot
    assert snap["structured_evidence"][0]["source"] == "consensus"
    assert snap["structured_evidence"][0]["value"] in (10, "10")
    assert snap["item_freshness"] == "fresh"


@pytest.mark.asyncio
async def test_no_evidence_leaves_snapshot_keys_absent(session) -> None:
    """evidence 미지정 시 reserved key를 추가하지 않는다(기존 동작 무변화)."""
    from app.schemas.investment_reports import IngestReportItem, IngestReportRequest
    from app.services.investment_reports.ingestion import (
        InvestmentReportIngestionService,
    )
    from app.services.investment_reports.repository import InvestmentReportsRepository

    repo = InvestmentReportsRepository(session)
    svc = InvestmentReportIngestionService(session, repository=repo)
    report = await svc.ingest(
        IngestReportRequest(
            report_type="advisory_lite_v1",
            market="kr",
            created_by_profile="CLAUDE_ADVISOR",
            title="t",
            summary="s",
            kst_date="2026-06-09",
            status="draft",
            items=[
                IngestReportItem(
                    client_item_key="k1",
                    item_kind="action",
                    intent="buy_review",
                    rationale="r",
                )
            ],
        )
    )
    await session.flush()
    items = await repo.list_items_for_report(report.id)
    assert "structured_evidence" not in (items[0].evidence_snapshot or {})
    assert "item_freshness" not in (items[0].evidence_snapshot or {})
```

(주의: `IngestReportRequest`의 idempotency CHECK / published freshness CHECK를 피하려고 `status="draft"`로 둔다 — `docs/superpowers/plans/2026-05-30-rob373-...md` 픽스처 주석 참조. xdist DB 충돌 회피가 필요하면 기존 DB 테스트의 cleanup-lock 픽스처 관례를 따른다.)

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_investment_report_item_evidence.py -k "round_trips or snapshot_keys_absent" -v`
Expected: FAIL — `structured_evidence` 키 부재(아직 병합 안 함).

- [ ] **Step 3: `_insert_item` 병합 구현**

`app/services/investment_reports/ingestion.py`의 `_insert_item`에서, `self._repo.insert_item(...)` 호출(`:313`) **직전**에 병합 dict 구성:

```python
        # ROB-459 P1 — 타입드 evidence를 기존 evidence_snapshot JSONB에 reserved
        # key로 병합(마이그레이션 0). InvestmentReportItemResponse.evidence_snapshot
        # 으로 그대로 round-trip한다. evidence 미지정 시 키를 추가하지 않음.
        evidence_payload = dict(item_req.evidence_snapshot or {})
        if item_req.evidence:
            evidence_payload["structured_evidence"] = [
                e.model_dump(mode="json") for e in item_req.evidence
            ]
        if item_req.freshness is not None:
            evidence_payload["item_freshness"] = item_req.freshness
```

그리고 `:324`의 인자를 교체:

```python
            evidence_snapshot=evidence_payload,
```

(insert/overwrite 두 경로 모두 `_insert_item`을 통과 — 한 곳만 수정하면 양쪽 적용.)

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_investment_report_item_evidence.py -v`
Expected: PASS (전체)

- [ ] **Step 5: 커밋**

```bash
git add app/services/investment_reports/ingestion.py tests/test_investment_report_item_evidence.py
git commit -m "feat(ROB-459): structured evidence를 evidence_snapshot에 병합(no migration)

_insert_item이 item_req.evidence/freshness를 evidence_snapshot.structured_evidence
/item_freshness로 병합 — 읽기경로 무비용 round-trip, 마이그레이션 0.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3.3: `CREATE_DESCRIPTION`에 evidence 노출

**파일**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` (`CREATE_DESCRIPTION`)
- Test: `tests/mcp_server/test_investment_report_create_handler.py`

- [ ] **Step 1: description 테스트 추가 (실패 예상)**

`tests/mcp_server/test_investment_report_create_handler.py`에 append:

```python
def test_create_description_documents_structured_evidence():
    desc = h.CREATE_DESCRIPTION
    assert "evidence" in desc
    assert "source" in desc
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/test_investment_report_create_handler.py -k structured_evidence -v`
Expected: FAIL.

- [ ] **Step 3: `CREATE_DESCRIPTION`에 한 줄 추가**

`CREATE_DESCRIPTION` 리터럴 끝에 evidence 문장 추가:

```python
    "decision_bucket (optional) must be one of: new_buy_candidate, open_action, "
    "completed_or_existing, deferred_no_action, risk_watch. "
    "Optional structured evidence per item: evidence=[{source, metric, value, "
    "as_of, freshness}] (source required) and item-level freshness "
    "(fresh|soft_stale|stale|unknown), persisted alongside evidence_snapshot."
)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/mcp_server/test_investment_report_create_handler.py -v`
Expected: PASS

- [ ] **Step 5: 커밋 + 슬라이스 검증**

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py tests/mcp_server/test_investment_report_create_handler.py
git commit -m "docs(ROB-459): CREATE_DESCRIPTION에 structured evidence 노출

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

Run: `uv run pytest tests/test_investment_report_item_evidence.py tests/mcp_server/test_investment_report_create_handler.py -v && uv run ruff check app/schemas/investment_reports.py app/services/investment_reports/ingestion.py`
Expected: 전체 PASS, ruff clean. → PR 3 준비 완료.

---

## Self-Review (스펙 대비)

**1. Spec coverage**
- 스펙 §3(S1) → Task 1.1(헬퍼)/1.2(배선+description)/1.3(CLAUDE.md). ✅ 일괄 에러, 계약 노출, 전제 정정 노트 모두 커버.
- 스펙 §4(S2 P3) → Task 2.1(config)/2.2(화이트리스트 default∪config, fail-closed)/2.3(description). ✅
- 스펙 §5(S3 P1) → Task 3.1(스키마)/3.2(병합 round-trip)/3.3(description). ✅ no-migration 경로.
- 스펙 §6 제약(mutation 0, migration 0) → 전 태스크 additive/에러-UX. ✅
- 스펙 §7 deferred(P2/P4) → 계획에서 제외(의도적). ✅

**2. Placeholder scan** — 모든 코드 스텝에 실제 코드/명령/기대출력 포함. "TBD/TODO/적절히" 없음. ✅

**3. Type consistency**
- `_validate_report_items(raw_items) -> (list[IngestReportItem], dict|None)` — Task 1.1 정의, 1.2(create)/1.1-Step5(generate)에서 동일 시그니처 사용. ✅
- `CREATE_DESCRIPTION` — 1.2 도입, 3.3에서 리터럴 확장(동일 상수명). ✅
- `_advisory_draft_profiles()` / `_DEFAULT_ADVISORY_DRAFT_PROFILES` — 2.2에서 정의·사용 일관. ✅
- `ItemEvidencePayload` / `evidence` / `freshness` / `structured_evidence` / `item_freshness` — 3.1 정의, 3.2 영속화에서 동일 키. ✅
- `CONTEXT_GET_DESCRIPTION` — 2.3 신규 상수, 테스트와 등록부에서 동일명. ✅

**열린 실행-시 확인사항(차단 아님):** (a) `Settings()` 직접 생성이 필수 env를 요구하면 기존 config 테스트 관례 따르기; (b) S3 DB 테스트의 xdist cleanup-lock 픽스처 필요 여부는 기존 investment_reports DB 테스트 관례 따르기; (c) `value` smart-union 직렬화는 실제 동작에 맞춰 단언 미세조정.

---

## Execution Handoff

(상위 세션에서 안내)
