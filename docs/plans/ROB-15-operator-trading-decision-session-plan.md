# ROB-15 — Operator Trading Decision Session Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans
> (or superpowers:subagent-driven-development) to execute this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**AOE_STATUS:** plan-ready
**AOE_ISSUE:** ROB-15
**AOE_ROLE:** planner-opus
**AOE_NEXT:** Codex implementer executes Tasks 1–10 in order, on this worktree
(`/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-15-operator-trading-decision-session`,
branch `feature/ROB-15-operator-trading-decision-session`).

- **Linear issue:** ROB-15 — Generate Trading Decision Session from operator request
- **Branch / worktree:** `feature/ROB-15-operator-trading-decision-session`
  (`/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-15-operator-trading-decision-session`)
- **Status:** Plan only. **No code begins until this plan is reviewed.**
- **Planner / reviewer:** Claude Opus
- **Implementer:** Codex (`codex --yolo`), scoped to this worktree.
- **Depends on:**
  - ROB-1 (#595) — Trading Decision Workspace schema + service helpers (`create_decision_session`, `add_decision_proposals`).
  - ROB-2 (#597) — `app/schemas/trading_decisions.py` literal types and request/response shapes.
  - ROB-9 (#601) — `tradingagents_research_service.run_tradingagents_research()` and `TradingAgentsNotConfigured`.
  - ROB-13 (#604) — production-DB smoke harness pattern (forbidden-prefix and forbidden-argv guards).
  - ROB-14 (#605, #606) — pure synthesis (`app/schemas/trading_decision_synthesis.py`,
    `app/services/trading_decision_synthesis.py`,
    `app/services/trading_decision_synthesis_persistence.py`) with
    `advisory_only=True` / `execution_allowed=False` invariants.

**Goal:** Add an operator-facing service + FastAPI entry point that turns a
structured operator request (market scope + candidates + advisory toggle) into a
persisted `TradingDecisionSession` (with `TradingDecisionProposal` rows) and
returns a session URL on `trader.robinco.dev`. **Never** places live or paper
orders, registers watch alerts, or creates order intents. TradingAgents stays
advisory-only when toggled on.

**Architecture:** One **pure URL helper**, one **operator-session service** that
composes only safe primitives, and **one new POST endpoint** added to the
existing `app/routers/trading_decisions.py`. The service exposes a thin seam
(operator-supplied `OperatorCandidate[]`) so candidate sourcing (screener /
portfolio decision / Hermes parser) can be wired in a later issue without
expanding ROB-15's blast radius. Persistence is delegated to existing helpers
only. Optional TradingAgents synthesis reuses the ROB-9 runner and the ROB-14
synthesis pipeline. Every persisted row carries the
`advisory_only=True` / `execution_allowed=False` invariant.

**Tech Stack:** Python 3.13, Pydantic v2, FastAPI, SQLAlchemy async, pytest
(`unit`, `integration`, subprocess-import safety tests).

---

## 1. Scope check

ROB-15 is **one** subsystem (operator-facing session creation). It does not:
- introduce a Hermes/Discord natural-language parser (that ticket consumes this
  endpoint),
- modify the screener, portfolio decision, KIS, or Upbit services,
- expose a UI page (the existing `/trading/decisions/...` SPA renders the
  session this endpoint creates),
- add scheduling/automation,
- modify ROB-9/13/14 modules.

The acceptance criteria are met by:

- a public Python service entry point operators can call directly,
- a FastAPI POST route the future Hermes adapter can call over HTTPS,
- unit tests covering: optional advisory toggle, missing-config handling,
  persistence shape, and absence of broker side effects,
- integration test covering router → service → DB persistence shape,
- subprocess-import / module-import safety test mirroring ROB-13/14.

## 2. In-scope vs Out-of-scope

| Area | In scope (this PR) | Deferred |
|---|---|---|
| `app/schemas/operator_decision_session.py` (new request/response shapes) | ✅ | — |
| `app/services/trading_decision_session_url.py` (pure URL helper) | ✅ | — |
| `app/services/operator_decision_session_service.py` (orchestrator) | ✅ | — |
| `POST /trading/api/decisions/from-operator-request` route | ✅ | — |
| Unit tests for service composition + advisory toggle | ✅ | — |
| Integration test for router → DB shape | ✅ | — |
| Module-import safety test (mirrors ROB-13/14) | ✅ | — |
| Forbidden-argv guard reused in tests | ✅ | — |
| Hermes natural-language parser (`파이리, …`) | ❌ | future ROB |
| Auto-generation of candidates from screener/portfolio decision/account state | ❌ | future ROB |
| TradingDecisionAction / Counterfactual / Outcome rows | ❌ — **forbidden** | — |
| Watch alert registration | ❌ — **forbidden** | — |
| Live, paper, or `dry_run=False` order placement | ❌ — **forbidden** | — |
| Modifying `tradingagents_research_service.py`, `trading_decision_service.py`, ROB-14 synthesis modules, or models | ❌ | — |
| Modifying TradingAgents fork | ❌ | upstream-only |
| Reading or echoing API keys / `.env` values / tokens / passwords | ❌ — **forbidden** | — |

## 3. Safety invariants this PR MUST enforce

1. The new service module imports **none** of:
   `app.services.kis*`, `app.services.upbit*`, `app.services.brokers`,
   `app.services.order_service`, `app.services.orders`, `app.services.watch_alerts*`,
   `app.services.paper_trading_service`, `app.services.openclaw_client`,
   `app.services.crypto_trade_cooldown_service`, `app.services.fill_notification`,
   `app.services.execution_event`, `app.services.redis_token_manager`,
   `app.services.kis_websocket*`, `app.tasks*`, `app.services.screener_service`
   (transitively pulls `_place_order_impl`),
   `app.mcp_server.tooling.order_execution`,
   `app.mcp_server.tooling.watch_alerts_registration`.
   Allowed dependencies: `app.services.trading_decision_service`,
   `app.services.trading_decision_synthesis*`, `app.schemas.*`,
   `app.services.tradingagents_research_service`, `app.core.config`,
   `app.models.trading_decision`, `app.models.trading`.
2. The new router additions in `app/routers/trading_decisions.py` MUST NOT
   add any imports that violate (1).
3. Every persisted row has `original_payload["advisory_only"] is True` and
   `original_payload["execution_allowed"] is False`. Every persisted session
   has `market_brief["advisory_only"] is True` and
   `market_brief["execution_allowed"] is False`.
4. The service NEVER calls:
   `place_order`, `register_watch_alert*`, `create_order_intent`,
   `_place_order_impl`, anything in `app.services.kis_trading_service`,
   anything in `app.services.orders`, anything that mutates broker state.
   Enforced by a unit test that mocks the persistence layer and asserts no
   forbidden symbol appears in `sys.modules` after the service runs.
5. `include_tradingagents=True` with **missing** TradingAgents config (any of
   `tradingagents_python`, `tradingagents_repo_path`, or runner file) → fail
   closed: HTTP 503 with `{"detail":"tradingagents_not_configured"}` and no DB
   write. The session is **not** created.
6. `include_tradingagents=False` (default) → skip advisory entirely;
   `original_payload["synthesis"]` is **absent**; persistence does NOT use
   `create_synthesized_decision_session` (which requires a synthesis block). A
   `applied_policies` array is still written with value `["no_advisory"]` for
   audit symmetry, inside `original_payload["operator_request"]`.
7. The TradingAgents subprocess is launched **once per candidate** (sequentially),
   reusing `tradingagents_research_service.run_tradingagents_research()`. No new
   subprocess invocation lives in ROB-15.
8. Operator request input is sanitized:
   - `market_scope ∈ {"kr", "us", "crypto"}`,
   - `instrument_type ∈ InstrumentType`,
   - `symbol` matches `^[A-Za-z0-9._/-]{1,32}$` (mirrors ROB-9 / ROB-13),
   - `candidates` non-empty, length ≤ 20,
   - `notes` ≤ 4000 chars,
   - `analysts` (if present) is a list of `^[a-z_]{1,32}$` tokens, default `["market"]`.
9. The service NEVER prints, logs, or persists raw env values / secrets. Any
   `original_payload` field that came from `os.environ` is forbidden. The only
   advisory metadata copied through is the allowlist already established by
   ROB-14 (`provider`, `model`, `base_url`, `decision_text`,
   `final_trade_decision_text`, `warnings`, `risk_flags`, `raw_state_keys`,
   `as_of_date`).
10. The router uses the existing `get_authenticated_user` dependency — no new
    bypass auth path. Hermes will authenticate as the operator user when wired
    in a follow-up issue.
11. The session URL helper is a **pure** function (no settings / env / I/O
    imports inside the helper module — `settings.public_base_url` is read by
    the caller and passed in, mirroring `order_intent_discord_brief`).

## 4. Design

### 4.1 Request / response shapes (new)

`app/schemas/operator_decision_session.py` (new):

```python
"""Operator-facing trading decision session request/response schemas.

These schemas model the inbound operator request (eventually proxied by the
Hermes/Discord adapter) and the response handing back the persisted session
URL. They contain no broker/order/watch affordances.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.trading_decisions import (
    InstrumentTypeLiteral,
    ProposalKindLiteral,
    SessionStatusLiteral,
    SideLiteral,
)

OperatorMarketScopeLiteral = Literal["kr", "us", "crypto"]


class OperatorCandidate(BaseModel):
    """One deterministic candidate the operator wants reflected in the session."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    instrument_type: InstrumentTypeLiteral
    side: SideLiteral = "none"
    confidence: int = Field(ge=0, le=100)
    proposal_kind: ProposalKindLiteral = "other"
    rationale: str = Field(default="", max_length=4000)
    quantity: Decimal | None = None
    quantity_pct: Decimal | None = Field(default=None, ge=0, le=100)
    amount: Decimal | None = Field(default=None, ge=0)
    price: Decimal | None = Field(default=None, ge=0)
    trigger_price: Decimal | None = Field(default=None, ge=0)
    threshold_pct: Decimal | None = Field(default=None, ge=0, le=100)
    currency: str | None = Field(default=None, max_length=8)

    @field_validator("symbol")
    @classmethod
    def _symbol_charset(cls, value: str) -> str:
        import re
        if not re.fullmatch(r"^[A-Za-z0-9._/-]{1,32}$", value):
            raise ValueError("symbol contains unsupported characters")
        return value


class OperatorDecisionRequest(BaseModel):
    """Inbound operator request payload.

    The future Hermes adapter constructs this from an operator's natural-language
    message (e.g. '파이리, KR 기준으로 …'). For ROB-15 the payload is structured.
    """

    model_config = ConfigDict(extra="forbid")

    market_scope: OperatorMarketScopeLiteral
    candidates: list[OperatorCandidate] = Field(min_length=1, max_length=20)
    include_tradingagents: bool = False
    analysts: list[str] | None = None
    strategy_name: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    source_profile: str = Field(
        default="operator_request",
        min_length=1,
        max_length=64,
    )
    generated_at: datetime | None = None

    @field_validator("analysts")
    @classmethod
    def _analyst_charset(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        import re
        for token in value:
            if not re.fullmatch(r"^[a-z_]{1,32}$", token):
                raise ValueError("analyst token contains unsupported characters")
        return value


class OperatorDecisionResponse(BaseModel):
    session_uuid: UUID
    session_url: str
    status: SessionStatusLiteral
    proposal_count: int
    advisory_used: bool
    advisory_skipped_reason: str | None = None
```

### 4.2 Pure URL helper

`app/services/trading_decision_session_url.py` (new):

```python
"""Pure URL helper for the Trading Decision Workspace SPA shell.

Contract:
- No DB / Redis / httpx / settings / env imports.
- Inputs in → string out. Deterministic for fixed inputs.
"""

from __future__ import annotations

from urllib.parse import quote
from uuid import UUID


def build_trading_decision_session_url(base_url: str, session_uuid: UUID) -> str:
    """Compose `<origin>/trading/decisions/<uuid>`.

    Strips trailing slashes from `base_url`. The UUID is path-quoted with no
    safe characters reserved.
    """
    base = base_url.rstrip("/")
    return f"{base}/trading/decisions/{quote(str(session_uuid), safe='')}"


def resolve_trading_decision_base_url(
    *, configured: str | None, request_base_url: str
) -> str:
    """Pick the configured public base URL when set, else fall back.

    Pure function — no settings/env access. The caller (router) supplies
    `configured` from `settings.public_base_url` and `request_base_url` from
    `request.base_url`. Whitespace-only or empty configured values are treated
    as unset so the request origin remains the fallback.
    """
    if configured is not None and configured.strip():
        return configured.strip()
    return request_base_url
```

### 4.3 Operator-session service

`app/services/operator_decision_session_service.py` (new). Orchestrates:

1. Validate the request (already done by Pydantic at the router boundary).
2. For each `OperatorCandidate`, build a `CandidateAnalysis` (ROB-14 schema).
3. If `include_tradingagents=True`:
   - Probe TradingAgents config eagerly; if missing → raise
     `TradingAgentsNotConfigured` (ROB-9 exception, re-exported from this
     module for router re-mapping to HTTP 503).
   - For each candidate, call
     `tradingagents_research_service.run_tradingagents_research(
       symbol, instrument_type, as_of_date=today, analysts=request.analysts
     )`.
   - Convert each runner result via
     `advisory_from_runner_result(result.model_dump(mode="json"))`.
   - Synthesize via
     `synthesize_candidate_with_advisory(candidate, advisory)`.
   - Persist via
     `create_synthesized_decision_session(...)` with
     `source_profile=request.source_profile + "+tradingagents"`,
     `market_scope=request.market_scope`,
     `strategy_name=request.strategy_name or "operator_tradingagents"`,
     `market_brief={"advisory_only": True, "execution_allowed": False, "operator_request": {...sanitized...}}`,
     `generated_at=request.generated_at or now(UTC)`,
     `notes=request.notes`.
4. If `include_tradingagents=False`:
   - Build raw `ProposalCreate` entries directly (no synthesis dict in payload):
     ```python
     {
         "symbol": c.symbol,
         "instrument_type": InstrumentType(c.instrument_type),
         "proposal_kind": ProposalKind(c.proposal_kind),
         "side": c.side,
         "original_quantity": c.quantity,
         "original_quantity_pct": c.quantity_pct,
         "original_amount": c.amount,
         "original_price": c.price,
         "original_trigger_price": c.trigger_price,
         "original_threshold_pct": c.threshold_pct,
         "original_currency": c.currency,
         "original_rationale": c.rationale,
         "original_payload": {
             "advisory_only": True,
             "execution_allowed": False,
             "operator_request": {
                 "source_profile": request.source_profile,
                 "applied_policies": ["no_advisory"],
                 "candidate": c.model_dump(mode="json"),
             },
         },
     }
     ```
   - Persist via raw `create_decision_session` + `add_decision_proposals`
     (NOT `create_synthesized_decision_session`, which requires a `synthesis`
     dict in `original_payload`).
5. Return `(session_obj, advisory_used, advisory_skipped_reason)`.

Public entry point:

```python
async def create_operator_decision_session(
    db: AsyncSession,
    *,
    user_id: int,
    request: OperatorDecisionRequest,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> OperatorSessionResult: ...
```

Where `OperatorSessionResult` is a small dataclass:

```python
@dataclass(frozen=True)
class OperatorSessionResult:
    session: TradingDecisionSession
    proposal_count: int
    advisory_used: bool
    advisory_skipped_reason: str | None
```

The service does NOT call `await db.commit()` — that remains the router's
responsibility, matching `trading_decision_service`.

Allowed imports (exhaustive):

```python
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading import InstrumentType
from app.models.trading_decision import (
    ProposalKind,
    TradingDecisionProposal,
    TradingDecisionSession,
)
from app.schemas.operator_decision_session import (
    OperatorCandidate,
    OperatorDecisionRequest,
)
from app.schemas.trading_decision_synthesis import (
    AdvisoryEvidence,
    CandidateAnalysis,
    advisory_from_runner_result,
)
from app.services import trading_decision_service
from app.services.trading_decision_synthesis import (
    synthesize_candidate_with_advisory,
)
from app.services.trading_decision_synthesis_persistence import (
    create_synthesized_decision_session,
)
from app.services.tradingagents_research_service import (
    TradingAgentsNotConfigured,
    TradingAgentsRunnerError,
    run_tradingagents_research,
)
```

The service module must NOT import any other `app.services.*` module beyond the
above set.

### 4.4 Router endpoint (additive)

Append to `app/routers/trading_decisions.py`:

```python
@router.post(
    "/api/decisions/from-operator-request",
    response_model=OperatorDecisionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_decision_from_operator_request(
    request: OperatorDecisionRequest,
    response: Response,
    fastapi_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> OperatorDecisionResponse:
    try:
        result = await operator_decision_session_service.create_operator_decision_session(
            db,
            user_id=current_user.id,
            request=request,
        )
    except TradingAgentsNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="tradingagents_not_configured",
        ) from exc
    except TradingAgentsRunnerError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="tradingagents_runner_failed",
        ) from exc

    await db.commit()

    base_url = resolve_trading_decision_base_url(
        configured=settings.public_base_url,
        request_base_url=str(fastapi_request.base_url),
    )
    session_url = build_trading_decision_session_url(
        base_url=base_url, session_uuid=result.session.session_uuid
    )
    response.headers["Location"] = (
        f"/trading/api/decisions/{result.session.session_uuid}"
    )

    return OperatorDecisionResponse(
        session_uuid=result.session.session_uuid,
        session_url=session_url,
        status=result.session.status,
        proposal_count=result.proposal_count,
        advisory_used=result.advisory_used,
        advisory_skipped_reason=result.advisory_skipped_reason,
    )
```

The new imports added to the router:

```python
from fastapi import Request
from app.core.config import settings
from app.schemas.operator_decision_session import (
    OperatorDecisionRequest,
    OperatorDecisionResponse,
)
from app.services import operator_decision_session_service
from app.services.tradingagents_research_service import (
    TradingAgentsNotConfigured,
    TradingAgentsRunnerError,
)
from app.services.trading_decision_session_url import (
    build_trading_decision_session_url,
    resolve_trading_decision_base_url,
)
```

These are all confirmed safe in §3.

## 5. File map

- **Create:**
  - `app/schemas/operator_decision_session.py`
  - `app/services/trading_decision_session_url.py`
  - `app/services/operator_decision_session_service.py`
  - `tests/test_trading_decision_session_url.py`
  - `tests/services/test_operator_decision_session_service.py`
  - `tests/services/test_operator_decision_session_service_safety.py`
  - `tests/test_operator_decision_session_router.py`
- **Modify:**
  - `app/routers/trading_decisions.py` — append POST `/api/decisions/from-operator-request` plus imports.
  - `tests/test_trading_decisions_router_safety.py` — extend `FORBIDDEN_PREFIXES` if needed (it already covers all forbidden paths; this is a no-op verification).
- **Untouched (do not edit):**
  - `app/models/trading.py`, `app/models/trading_decision.py`
  - `app/services/trading_decision_service.py`
  - `app/services/trading_decision_synthesis.py`
  - `app/services/trading_decision_synthesis_persistence.py`
  - `app/services/tradingagents_research_service.py`
  - `app/schemas/trading_decisions.py`, `app/schemas/trading_decision_synthesis.py`
  - `app/main.py` (router already registered)
  - All KIS / Upbit / orders / watch_alerts modules

No new Alembic migration. No new env-var keys. (Reuses existing
`PUBLIC_BASE_URL`, `TRADINGAGENTS_*`.)

## 6. Tasks

> **Convention:** every task ends with a commit step. Use the trailer
> `Co-Authored-By: Paperclip <noreply@paperclip.ing>` per CLAUDE.md.
> Use `uv run pytest -p no:cacheprovider` for all test runs.

### Task 1 — Pure URL helper

**Files:**
- Create: `app/services/trading_decision_session_url.py`
- Create: `tests/test_trading_decision_session_url.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/test_trading_decision_session_url.py
from __future__ import annotations

from uuid import UUID

import pytest


@pytest.mark.unit
def test_builds_url_from_base_and_uuid():
    from app.services.trading_decision_session_url import (
        build_trading_decision_session_url,
    )

    url = build_trading_decision_session_url(
        "https://trader.robinco.dev/", UUID("11111111-1111-1111-1111-111111111111")
    )
    assert url == (
        "https://trader.robinco.dev/trading/decisions/"
        "11111111-1111-1111-1111-111111111111"
    )


@pytest.mark.unit
def test_strips_trailing_slashes_and_quotes_uuid():
    from app.services.trading_decision_session_url import (
        build_trading_decision_session_url,
    )

    url = build_trading_decision_session_url(
        "https://trader.robinco.dev///",
        UUID("22222222-2222-2222-2222-222222222222"),
    )
    assert url == (
        "https://trader.robinco.dev/trading/decisions/"
        "22222222-2222-2222-2222-222222222222"
    )


@pytest.mark.unit
def test_resolve_uses_configured_when_present():
    from app.services.trading_decision_session_url import (
        resolve_trading_decision_base_url,
    )

    resolved = resolve_trading_decision_base_url(
        configured="https://trader.robinco.dev",
        request_base_url="http://localhost:8000/",
    )
    assert resolved == "https://trader.robinco.dev"


@pytest.mark.unit
def test_resolve_falls_back_when_configured_blank():
    from app.services.trading_decision_session_url import (
        resolve_trading_decision_base_url,
    )

    resolved = resolve_trading_decision_base_url(
        configured="   ",
        request_base_url="http://localhost:8000/",
    )
    assert resolved == "http://localhost:8000/"


@pytest.mark.unit
def test_resolve_strips_configured_whitespace():
    from app.services.trading_decision_session_url import (
        resolve_trading_decision_base_url,
    )

    resolved = resolve_trading_decision_base_url(
        configured="  https://trader.robinco.dev/  ",
        request_base_url="http://localhost:8000/",
    )
    assert resolved == "https://trader.robinco.dev/"
```

- [x] **Step 2: Run tests to verify they fail (module missing)**

```bash
uv run pytest tests/test_trading_decision_session_url.py -v
```

Expected: collection error or `ModuleNotFoundError: app.services.trading_decision_session_url`.

- [x] **Step 3: Write minimal implementation**

```python
# app/services/trading_decision_session_url.py
"""Pure URL helper for the Trading Decision Workspace SPA shell.

Contract:
- No DB / Redis / httpx / settings / env imports.
- Inputs in → string out. Deterministic for fixed inputs.
"""

from __future__ import annotations

from urllib.parse import quote
from uuid import UUID


def build_trading_decision_session_url(base_url: str, session_uuid: UUID) -> str:
    base = base_url.rstrip("/")
    return f"{base}/trading/decisions/{quote(str(session_uuid), safe='')}"


def resolve_trading_decision_base_url(
    *, configured: str | None, request_base_url: str
) -> str:
    if configured is not None and configured.strip():
        return configured.strip()
    return request_base_url
```

- [x] **Step 4: Run tests, verify pass**

```bash
uv run pytest tests/test_trading_decision_session_url.py -v
```

Expected: 5 passed.

- [x] **Step 5: Commit**

```bash
git add \
  app/services/trading_decision_session_url.py \
  tests/test_trading_decision_session_url.py
git commit -m "$(cat <<'EOF'
feat(rob-15): add Trading Decision Session URL helper

Pure helper that composes /trading/decisions/<uuid> URLs from the
request origin or settings.public_base_url. No DB / settings imports
inside the module.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 2 — Operator session schemas

**Files:**
- Create: `app/schemas/operator_decision_session.py`
- Create: `tests/test_operator_decision_session_schemas.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/test_operator_decision_session_schemas.py
from __future__ import annotations

from decimal import Decimal

import pytest


@pytest.mark.unit
def test_operator_candidate_minimum_fields():
    from app.schemas.operator_decision_session import OperatorCandidate

    cand = OperatorCandidate(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        confidence=70,
        proposal_kind="enter",
    )
    assert cand.symbol == "005930"
    assert cand.instrument_type == "equity_kr"
    assert cand.side == "buy"


@pytest.mark.unit
def test_operator_candidate_rejects_unsupported_symbol_chars():
    from app.schemas.operator_decision_session import OperatorCandidate

    with pytest.raises(ValueError):
        OperatorCandidate(
            symbol="bad symbol!",
            instrument_type="equity_kr",
            confidence=50,
        )


@pytest.mark.unit
def test_operator_request_default_advisory_off():
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    req = OperatorDecisionRequest(
        market_scope="kr",
        candidates=[
            OperatorCandidate(
                symbol="005930", instrument_type="equity_kr", confidence=50
            )
        ],
    )
    assert req.include_tradingagents is False
    assert req.source_profile == "operator_request"


@pytest.mark.unit
def test_operator_request_rejects_extra_fields():
    from app.schemas.operator_decision_session import OperatorDecisionRequest

    with pytest.raises(ValueError):
        OperatorDecisionRequest.model_validate(
            {
                "market_scope": "kr",
                "candidates": [
                    {
                        "symbol": "005930",
                        "instrument_type": "equity_kr",
                        "confidence": 50,
                    }
                ],
                "place_order": True,
            }
        )


@pytest.mark.unit
def test_operator_request_caps_candidates():
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    too_many = [
        OperatorCandidate(
            symbol=f"AAA{i:03d}", instrument_type="equity_us", confidence=50
        )
        for i in range(21)
    ]
    with pytest.raises(ValueError):
        OperatorDecisionRequest(market_scope="us", candidates=too_many)


@pytest.mark.unit
def test_operator_request_validates_analyst_token_charset():
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    cand = [
        OperatorCandidate(
            symbol="AAPL", instrument_type="equity_us", confidence=50
        )
    ]
    with pytest.raises(ValueError):
        OperatorDecisionRequest(
            market_scope="us",
            candidates=cand,
            analysts=["BAD-TOKEN"],
        )


@pytest.mark.unit
def test_operator_request_accepts_decimal_quantity():
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    cand = OperatorCandidate(
        symbol="BTC",
        instrument_type="crypto",
        confidence=40,
        side="buy",
        amount=Decimal("100000"),
    )
    OperatorDecisionRequest(market_scope="crypto", candidates=[cand])
```

- [x] **Step 2: Run tests, verify they fail**

```bash
uv run pytest tests/test_operator_decision_session_schemas.py -v
```

Expected: `ModuleNotFoundError`.

- [x] **Step 3: Write minimal implementation**

```python
# app/schemas/operator_decision_session.py
"""Operator-facing Trading Decision Session request/response schemas.

The future Hermes / Discord adapter constructs OperatorDecisionRequest from a
natural-language operator message and posts it to
POST /trading/api/decisions/from-operator-request. These schemas contain no
broker / order / watch affordances.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.trading_decisions import (
    InstrumentTypeLiteral,
    ProposalKindLiteral,
    SessionStatusLiteral,
    SideLiteral,
)

OperatorMarketScopeLiteral = Literal["kr", "us", "crypto"]

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._/-]{1,32}$")
_ANALYST_RE = re.compile(r"^[a-z_]{1,32}$")


class OperatorCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    instrument_type: InstrumentTypeLiteral
    side: SideLiteral = "none"
    confidence: int = Field(ge=0, le=100)
    proposal_kind: ProposalKindLiteral = "other"
    rationale: str = Field(default="", max_length=4000)
    quantity: Decimal | None = None
    quantity_pct: Decimal | None = Field(default=None, ge=0, le=100)
    amount: Decimal | None = Field(default=None, ge=0)
    price: Decimal | None = Field(default=None, ge=0)
    trigger_price: Decimal | None = Field(default=None, ge=0)
    threshold_pct: Decimal | None = Field(default=None, ge=0, le=100)
    currency: str | None = Field(default=None, max_length=8)

    @field_validator("symbol")
    @classmethod
    def _symbol_charset(cls, value: str) -> str:
        if not _SYMBOL_RE.fullmatch(value):
            raise ValueError("symbol contains unsupported characters")
        return value


class OperatorDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_scope: OperatorMarketScopeLiteral
    candidates: list[OperatorCandidate] = Field(min_length=1, max_length=20)
    include_tradingagents: bool = False
    analysts: list[str] | None = None
    strategy_name: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    source_profile: str = Field(
        default="operator_request",
        min_length=1,
        max_length=64,
    )
    generated_at: datetime | None = None

    @field_validator("analysts")
    @classmethod
    def _analyst_charset(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for token in value:
            if not _ANALYST_RE.fullmatch(token):
                raise ValueError("analyst token contains unsupported characters")
        return value


class OperatorDecisionResponse(BaseModel):
    session_uuid: UUID
    session_url: str
    status: SessionStatusLiteral
    proposal_count: int
    advisory_used: bool
    advisory_skipped_reason: str | None = None
```

- [x] **Step 4: Run tests, verify pass**

```bash
uv run pytest tests/test_operator_decision_session_schemas.py -v
```

Expected: 7 passed.

- [x] **Step 5: Commit**

```bash
git add \
  app/schemas/operator_decision_session.py \
  tests/test_operator_decision_session_schemas.py
git commit -m "$(cat <<'EOF'
feat(rob-15): add operator decision session request schemas

OperatorDecisionRequest / OperatorCandidate / OperatorDecisionResponse
model the inbound operator payload and outbound session URL response.
extra='forbid' rejects unknown fields; symbol and analyst tokens use the
ROB-9 charset.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 3 — Operator-session service: include_tradingagents=False path

**Files:**
- Create: `app/services/operator_decision_session_service.py`
- Create: `tests/services/test_operator_decision_session_service.py`

- [x] **Step 1: Write the failing test (no-advisory path persists raw proposal)**

```python
# tests/services/test_operator_decision_session_service.py
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_no_advisory_path_persists_via_raw_helpers(monkeypatch):
    import app.services.operator_decision_session_service as svc
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    fake_session = SimpleNamespace(
        id=42,
        session_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        status="open",
        market_brief={},
    )
    fake_proposals = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    create_session_mock = AsyncMock(return_value=fake_session)
    add_proposals_mock = AsyncMock(return_value=fake_proposals)
    forbidden_synth = AsyncMock(side_effect=AssertionError("must not run"))

    monkeypatch.setattr(svc.trading_decision_service, "create_decision_session", create_session_mock)
    monkeypatch.setattr(svc.trading_decision_service, "add_decision_proposals", add_proposals_mock)
    monkeypatch.setattr(svc, "create_synthesized_decision_session", forbidden_synth)
    monkeypatch.setattr(svc, "run_tradingagents_research", AsyncMock(side_effect=AssertionError("must not run")))

    req = OperatorDecisionRequest(
        market_scope="kr",
        candidates=[
            OperatorCandidate(
                symbol="005930",
                instrument_type="equity_kr",
                side="buy",
                confidence=70,
                proposal_kind="enter",
                rationale="test",
            ),
            OperatorCandidate(
                symbol="000660",
                instrument_type="equity_kr",
                side="none",
                confidence=40,
                proposal_kind="pullback_watch",
            ),
        ],
        include_tradingagents=False,
        notes="op session",
    )

    result = await svc.create_operator_decision_session(
        SimpleNamespace(), user_id=7, request=req
    )

    assert result.advisory_used is False
    assert result.advisory_skipped_reason == "include_tradingagents=False"
    assert result.proposal_count == 2

    create_session_mock.assert_awaited_once()
    create_kwargs = create_session_mock.await_args.kwargs
    assert create_kwargs["user_id"] == 7
    assert create_kwargs["source_profile"] == "operator_request"
    assert create_kwargs["market_scope"] == "kr"
    assert create_kwargs["market_brief"]["advisory_only"] is True
    assert create_kwargs["market_brief"]["execution_allowed"] is False
    assert "synthesis_meta" not in create_kwargs["market_brief"]

    add_proposals_mock.assert_awaited_once()
    proposals_arg = add_proposals_mock.await_args.kwargs["proposals"]
    assert len(proposals_arg) == 2
    for p in proposals_arg:
        payload = p["original_payload"]
        assert payload["advisory_only"] is True
        assert payload["execution_allowed"] is False
        assert "synthesis" not in payload
        assert payload["operator_request"]["applied_policies"] == ["no_advisory"]


@pytest.mark.asyncio
async def test_no_advisory_path_uses_now_callable(monkeypatch):
    import app.services.operator_decision_session_service as svc
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    fake_session = SimpleNamespace(
        id=1, session_uuid="zz", status="open", market_brief={}
    )
    monkeypatch.setattr(
        svc.trading_decision_service,
        "create_decision_session",
        AsyncMock(return_value=fake_session),
    )
    monkeypatch.setattr(
        svc.trading_decision_service,
        "add_decision_proposals",
        AsyncMock(return_value=[SimpleNamespace(id=1)]),
    )

    req = OperatorDecisionRequest(
        market_scope="us",
        candidates=[
            OperatorCandidate(
                symbol="AAPL", instrument_type="equity_us", confidence=50
            )
        ],
    )
    fixed = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await svc.create_operator_decision_session(
        SimpleNamespace(),
        user_id=1,
        request=req,
        now=lambda: fixed,
    )
    create_kwargs = svc.trading_decision_service.create_decision_session.await_args.kwargs
    assert create_kwargs["generated_at"] == fixed
```

- [x] **Step 2: Run tests, verify they fail**

```bash
uv run pytest tests/services/test_operator_decision_session_service.py -v
```

Expected: `ModuleNotFoundError: app.services.operator_decision_session_service`.

- [x] **Step 3: Write minimal implementation (no-advisory path only)**

```python
# app/services/operator_decision_session_service.py
"""Operator-driven Trading Decision Session orchestrator.

Composes only:
- app.services.trading_decision_service (session/proposal helpers)
- app.services.trading_decision_synthesis* (ROB-14)
- app.services.tradingagents_research_service (ROB-9 runner)

Forbidden: any broker/order/watch import. See
tests/services/test_operator_decision_session_service_safety.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading import InstrumentType
from app.models.trading_decision import (
    ProposalKind,
    TradingDecisionSession,
)
from app.schemas.operator_decision_session import (
    OperatorCandidate,
    OperatorDecisionRequest,
)
from app.schemas.trading_decision_synthesis import (
    AdvisoryEvidence,
    CandidateAnalysis,
    advisory_from_runner_result,
)
from app.services import trading_decision_service
from app.services.trading_decision_synthesis import (
    synthesize_candidate_with_advisory,
)
from app.services.trading_decision_synthesis_persistence import (
    create_synthesized_decision_session,
)
from app.services.tradingagents_research_service import (
    TradingAgentsNotConfigured,
    TradingAgentsRunnerError,
    run_tradingagents_research,
)


@dataclass(frozen=True)
class OperatorSessionResult:
    session: TradingDecisionSession
    proposal_count: int
    advisory_used: bool
    advisory_skipped_reason: str | None


def _build_no_advisory_proposal(
    candidate: OperatorCandidate,
    *,
    source_profile: str,
) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "instrument_type": InstrumentType(candidate.instrument_type),
        "proposal_kind": ProposalKind(candidate.proposal_kind),
        "side": candidate.side,
        "original_quantity": candidate.quantity,
        "original_quantity_pct": candidate.quantity_pct,
        "original_amount": candidate.amount,
        "original_price": candidate.price,
        "original_trigger_price": candidate.trigger_price,
        "original_threshold_pct": candidate.threshold_pct,
        "original_currency": candidate.currency,
        "original_rationale": candidate.rationale,
        "original_payload": {
            "advisory_only": True,
            "execution_allowed": False,
            "operator_request": {
                "source_profile": source_profile,
                "applied_policies": ["no_advisory"],
                "candidate": candidate.model_dump(mode="json"),
            },
        },
    }


async def create_operator_decision_session(
    db: AsyncSession,
    *,
    user_id: int,
    request: OperatorDecisionRequest,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> OperatorSessionResult:
    generated_at = request.generated_at or now()
    if request.include_tradingagents:
        return await _run_with_advisory(
            db, user_id=user_id, request=request, generated_at=generated_at
        )
    return await _run_without_advisory(
        db, user_id=user_id, request=request, generated_at=generated_at
    )


async def _run_without_advisory(
    db: AsyncSession,
    *,
    user_id: int,
    request: OperatorDecisionRequest,
    generated_at: datetime,
) -> OperatorSessionResult:
    session_obj = await trading_decision_service.create_decision_session(
        db,
        user_id=user_id,
        source_profile=request.source_profile,
        strategy_name=request.strategy_name,
        market_scope=request.market_scope,
        market_brief={
            "advisory_only": True,
            "execution_allowed": False,
            "operator_request": {
                "applied_policies": ["no_advisory"],
                "include_tradingagents": False,
            },
        },
        generated_at=generated_at,
        notes=request.notes,
    )
    proposals = await trading_decision_service.add_decision_proposals(
        db,
        session_id=session_obj.id,
        proposals=[
            _build_no_advisory_proposal(c, source_profile=request.source_profile)
            for c in request.candidates
        ],
    )
    return OperatorSessionResult(
        session=session_obj,
        proposal_count=len(proposals),
        advisory_used=False,
        advisory_skipped_reason="include_tradingagents=False",
    )


async def _run_with_advisory(
    db: AsyncSession,
    *,
    user_id: int,
    request: OperatorDecisionRequest,
    generated_at: datetime,
) -> OperatorSessionResult:
    raise NotImplementedError  # implemented in Task 4
```

- [x] **Step 4: Run tests, verify pass**

```bash
uv run pytest tests/services/test_operator_decision_session_service.py -v
```

Expected: 2 passed.

- [x] **Step 5: Commit**

```bash
git add \
  app/services/operator_decision_session_service.py \
  tests/services/test_operator_decision_session_service.py
git commit -m "$(cat <<'EOF'
feat(rob-15): add operator session orchestrator (no-advisory path)

create_operator_decision_session persists a TradingDecisionSession plus
proposals from operator-supplied candidates with
include_tradingagents=False. Every payload carries advisory_only=True
and execution_allowed=False; no broker imports are introduced.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 4 — Operator-session service: include_tradingagents=True path

**Files:**
- Modify: `app/services/operator_decision_session_service.py`
- Modify: `tests/services/test_operator_decision_session_service.py`

- [x] **Step 1: Add failing tests for advisory path**

Append to `tests/services/test_operator_decision_session_service.py`:

```python
@pytest.mark.asyncio
async def test_advisory_path_uses_synthesis_persistence(monkeypatch):
    import app.services.operator_decision_session_service as svc
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )
    from app.schemas.tradingagents_research import (
        TradingAgentsConfigSnapshot,
        TradingAgentsLLM,
        TradingAgentsRunnerResult,
        TradingAgentsWarnings,
    )
    from datetime import date

    fake_runner_result = TradingAgentsRunnerResult(
        status="ok",
        symbol="NVDA",
        as_of_date=date(2026, 4, 28),
        decision="Underweight",
        advisory_only=True,
        execution_allowed=False,
        analysts=["market"],
        llm=TradingAgentsLLM(
            provider="openai-compatible",
            model="gpt-5.5",
            base_url="http://127.0.0.1:8796/v1",
        ),
        config=TradingAgentsConfigSnapshot(
            max_debate_rounds=1,
            max_risk_discuss_rounds=1,
            max_recur_limit=30,
            output_language="English",
            checkpoint_enabled=False,
        ),
        warnings=TradingAgentsWarnings(),
        final_trade_decision="no execution",
        raw_state_keys=["k1", "k2"],
    )

    fake_session = SimpleNamespace(
        id=99,
        session_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        status="open",
    )
    fake_proposals = [SimpleNamespace(id=10)]
    runner_mock = AsyncMock(return_value=fake_runner_result)
    synth_persist_mock = AsyncMock(return_value=(fake_session, fake_proposals))
    raw_create_mock = AsyncMock(side_effect=AssertionError("must not run"))

    monkeypatch.setattr(svc, "run_tradingagents_research", runner_mock)
    monkeypatch.setattr(svc, "create_synthesized_decision_session", synth_persist_mock)
    monkeypatch.setattr(
        svc.trading_decision_service, "create_decision_session", raw_create_mock
    )

    req = OperatorDecisionRequest(
        market_scope="us",
        candidates=[
            OperatorCandidate(
                symbol="NVDA",
                instrument_type="equity_us",
                side="buy",
                confidence=70,
                proposal_kind="enter",
            )
        ],
        include_tradingagents=True,
        analysts=["market"],
        strategy_name="op_us",
    )

    result = await svc.create_operator_decision_session(
        SimpleNamespace(), user_id=7, request=req
    )

    assert result.advisory_used is True
    assert result.advisory_skipped_reason is None
    assert result.proposal_count == 1
    runner_mock.assert_awaited_once()
    synth_persist_mock.assert_awaited_once()
    persist_kwargs = synth_persist_mock.await_args.kwargs
    assert persist_kwargs["user_id"] == 7
    assert persist_kwargs["market_scope"] == "us"
    assert persist_kwargs["source_profile"] == "operator_request+tradingagents"
    assert persist_kwargs["strategy_name"] == "op_us"
    assert persist_kwargs["market_brief"]["advisory_only"] is True
    assert persist_kwargs["market_brief"]["execution_allowed"] is False
    assert persist_kwargs["market_brief"]["operator_request"][
        "include_tradingagents"
    ] is True
    synthesized = persist_kwargs["proposals"]
    assert len(synthesized) == 1
    assert synthesized[0].advisory.advisory_only is True
    assert synthesized[0].advisory.execution_allowed is False
    # Buy + Underweight downgrades to pullback_watch / none (ROB-14 policy)
    assert synthesized[0].final_proposal_kind == "pullback_watch"
    assert synthesized[0].final_side == "none"


@pytest.mark.asyncio
async def test_advisory_missing_config_raises(monkeypatch):
    import app.services.operator_decision_session_service as svc
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )
    from app.services.tradingagents_research_service import (
        TradingAgentsNotConfigured,
    )

    monkeypatch.setattr(
        svc,
        "run_tradingagents_research",
        AsyncMock(side_effect=TradingAgentsNotConfigured("missing")),
    )
    no_persistence = AsyncMock(side_effect=AssertionError("must not persist"))
    monkeypatch.setattr(svc, "create_synthesized_decision_session", no_persistence)
    monkeypatch.setattr(
        svc.trading_decision_service, "create_decision_session", no_persistence
    )

    req = OperatorDecisionRequest(
        market_scope="us",
        candidates=[
            OperatorCandidate(
                symbol="NVDA",
                instrument_type="equity_us",
                side="buy",
                confidence=70,
                proposal_kind="enter",
            )
        ],
        include_tradingagents=True,
    )

    with pytest.raises(TradingAgentsNotConfigured):
        await svc.create_operator_decision_session(
            SimpleNamespace(), user_id=1, request=req
        )


@pytest.mark.asyncio
async def test_advisory_runner_error_propagates(monkeypatch):
    import app.services.operator_decision_session_service as svc
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )
    from app.services.tradingagents_research_service import (
        TradingAgentsRunnerError,
    )

    monkeypatch.setattr(
        svc,
        "run_tradingagents_research",
        AsyncMock(side_effect=TradingAgentsRunnerError("crashed")),
    )
    monkeypatch.setattr(
        svc,
        "create_synthesized_decision_session",
        AsyncMock(side_effect=AssertionError("must not persist")),
    )

    req = OperatorDecisionRequest(
        market_scope="us",
        candidates=[
            OperatorCandidate(
                symbol="NVDA",
                instrument_type="equity_us",
                side="buy",
                confidence=70,
                proposal_kind="enter",
            )
        ],
        include_tradingagents=True,
    )

    with pytest.raises(TradingAgentsRunnerError):
        await svc.create_operator_decision_session(
            SimpleNamespace(), user_id=1, request=req
        )
```

- [x] **Step 2: Run tests, verify they fail**

```bash
uv run pytest tests/services/test_operator_decision_session_service.py -v -k advisory
```

Expected: 3 failures (NotImplementedError or assertion failures).

- [x] **Step 3: Implement `_run_with_advisory`**

Replace the `_run_with_advisory` body in `app/services/operator_decision_session_service.py` with the following:

```python
async def _run_with_advisory(
    db: AsyncSession,
    *,
    user_id: int,
    request: OperatorDecisionRequest,
    generated_at: datetime,
) -> OperatorSessionResult:
    synthesized_proposals = []
    as_of: date = generated_at.astimezone(UTC).date()
    for candidate in request.candidates:
        runner_result = await run_tradingagents_research(
            symbol=candidate.symbol,
            instrument_type=InstrumentType(candidate.instrument_type),
            as_of_date=as_of,
            analysts=request.analysts,
        )
        advisory = advisory_from_runner_result(
            runner_result.model_dump(mode="json")
        )
        cand_schema = CandidateAnalysis(
            symbol=candidate.symbol,
            instrument_type=candidate.instrument_type,
            side=candidate.side,
            confidence=candidate.confidence,
            proposal_kind=candidate.proposal_kind,
            rationale=candidate.rationale,
            quantity=candidate.quantity,
            quantity_pct=candidate.quantity_pct,
            amount=candidate.amount,
            price=candidate.price,
            trigger_price=candidate.trigger_price,
            threshold_pct=candidate.threshold_pct,
            currency=candidate.currency,
        )
        synthesized_proposals.append(
            synthesize_candidate_with_advisory(cand_schema, advisory)
        )

    session_obj, db_proposals = await create_synthesized_decision_session(
        db,
        user_id=user_id,
        proposals=synthesized_proposals,
        generated_at=generated_at,
        source_profile=f"{request.source_profile}+tradingagents",
        strategy_name=request.strategy_name,
        market_scope=request.market_scope,
        market_brief={
            "advisory_only": True,
            "execution_allowed": False,
            "operator_request": {
                "include_tradingagents": True,
                "analysts": list(request.analysts) if request.analysts else None,
            },
        },
        notes=request.notes,
    )
    return OperatorSessionResult(
        session=session_obj,
        proposal_count=len(db_proposals),
        advisory_used=True,
        advisory_skipped_reason=None,
    )
```

- [x] **Step 4: Run tests, verify pass**

```bash
uv run pytest tests/services/test_operator_decision_session_service.py -v
```

Expected: 5 passed (2 from Task 3 + 3 new).

- [x] **Step 5: Commit**

```bash
git add \
  app/services/operator_decision_session_service.py \
  tests/services/test_operator_decision_session_service.py
git commit -m "$(cat <<'EOF'
feat(rob-15): wire TradingAgents advisory path in operator session service

include_tradingagents=True runs the ROB-9 advisory runner per candidate,
synthesizes via ROB-14 policy, and persists through
create_synthesized_decision_session. Missing config or runner errors
propagate as TradingAgentsNotConfigured / TradingAgentsRunnerError;
nothing is persisted on failure.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 5 — Module-import safety test

**Files:**
- Create: `tests/services/test_operator_decision_session_service_safety.py`

- [x] **Step 1: Write the failing test**

```python
# tests/services/test_operator_decision_session_service_safety.py
"""Safety: operator orchestrator must not transitively import broker code."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_FORBIDDEN_PREFIXES = [
    "app.services.kis",
    "app.services.upbit",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.orders",
    "app.services.watch_alerts",
    "app.services.paper_trading_service",
    "app.services.openclaw_client",
    "app.services.crypto_trade_cooldown_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.redis_token_manager",
    "app.services.kis_websocket",
    "app.services.screener_service",
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.watch_alerts_registration",
    "app.tasks",
]


@pytest.mark.unit
def test_service_module_does_not_import_forbidden_prefixes_in_subprocess():
    """Importing the orchestrator module in a fresh process must not pull in
    any broker / order / watch / task module."""
    project_root = Path(__file__).resolve().parents[2]
    script = """
import importlib
import json
import sys

importlib.import_module(
    "app.services.operator_decision_session_service"
)
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = set(json.loads(result.stdout))
    violations = sorted(
        name
        for name in loaded
        for forbidden in _FORBIDDEN_PREFIXES
        if name == forbidden or name.startswith(f"{forbidden}.")
    )
    if violations:
        pytest.fail(f"forbidden modules transitively imported: {violations}")


@pytest.mark.unit
def test_schema_module_does_not_import_forbidden_prefixes_in_subprocess():
    project_root = Path(__file__).resolve().parents[2]
    script = """
import importlib
import json
import sys

importlib.import_module("app.schemas.operator_decision_session")
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = set(json.loads(result.stdout))
    violations = sorted(
        name
        for name in loaded
        for forbidden in _FORBIDDEN_PREFIXES
        if name == forbidden or name.startswith(f"{forbidden}.")
    )
    if violations:
        pytest.fail(f"forbidden modules transitively imported: {violations}")


@pytest.mark.unit
def test_url_helper_module_has_no_settings_or_db_imports_in_subprocess():
    project_root = Path(__file__).resolve().parents[2]
    script = """
import importlib
import json
import sys

importlib.import_module("app.services.trading_decision_session_url")
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = set(json.loads(result.stdout))
    forbidden = [
        "app.core.config",
        "app.core.db",
        "redis",
        "httpx",
        "sqlalchemy",
    ]
    violations = sorted(
        name
        for name in loaded
        for prefix in forbidden
        if name == prefix or name.startswith(f"{prefix}.")
    )
    if violations:
        pytest.fail(f"URL helper pulled in heavyweight imports: {violations}")
```

- [x] **Step 2: Run tests, verify they pass**

```bash
uv run pytest tests/services/test_operator_decision_session_service_safety.py -v
```

Expected: 3 passed. (If any fail, audit the orchestrator's imports against §3.1.)

- [x] **Step 3: Commit**

```bash
git add tests/services/test_operator_decision_session_service_safety.py
git commit -m "$(cat <<'EOF'
test(rob-15): subprocess-import safety for operator orchestrator

Mirrors ROB-13/14 forbidden-prefix guards. Asserts the orchestrator,
the request schemas, and the URL helper do not transitively import
broker / order / watch / task / heavyweight infra modules.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 6 — Router endpoint

**Files:**
- Modify: `app/routers/trading_decisions.py`

- [x] **Step 1: Add imports at the top of the router (after existing imports)**

```python
from fastapi import Request

from app.core.config import settings
from app.schemas.operator_decision_session import (
    OperatorDecisionRequest,
    OperatorDecisionResponse,
)
from app.services import operator_decision_session_service
from app.services.tradingagents_research_service import (
    TradingAgentsNotConfigured,
    TradingAgentsRunnerError,
)
from app.services.trading_decision_session_url import (
    build_trading_decision_session_url,
    resolve_trading_decision_base_url,
)
```

- [x] **Step 2: Append the new endpoint at the end of the router file**

```python
@router.post(
    "/api/decisions/from-operator-request",
    response_model=OperatorDecisionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_decision_from_operator_request(
    payload: OperatorDecisionRequest,
    response: Response,
    fastapi_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> OperatorDecisionResponse:
    """Operator entry point: persists a Trading Decision Session and returns
    its URL. Never places orders, registers watches, or creates order intents.
    """
    try:
        result = (
            await operator_decision_session_service.create_operator_decision_session(
                db,
                user_id=current_user.id,
                request=payload,
            )
        )
    except TradingAgentsNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="tradingagents_not_configured",
        ) from exc
    except TradingAgentsRunnerError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="tradingagents_runner_failed",
        ) from exc

    await db.commit()

    base_url = resolve_trading_decision_base_url(
        configured=settings.public_base_url,
        request_base_url=str(fastapi_request.base_url),
    )
    session_url = build_trading_decision_session_url(
        base_url=base_url,
        session_uuid=result.session.session_uuid,
    )
    response.headers["Location"] = (
        f"/trading/api/decisions/{result.session.session_uuid}"
    )

    return OperatorDecisionResponse(
        session_uuid=result.session.session_uuid,
        session_url=session_url,
        status=result.session.status,
        proposal_count=result.proposal_count,
        advisory_used=result.advisory_used,
        advisory_skipped_reason=result.advisory_skipped_reason,
    )
```

- [x] **Step 3: Verify the existing safety test still passes**

```bash
uv run pytest tests/test_trading_decisions_router_safety.py -v
```

Expected: 1 passed. (If this fails, an import in step 1 violates §3.1 — revert and audit.)

- [x] **Step 4: Commit**

```bash
git add app/routers/trading_decisions.py
git commit -m "$(cat <<'EOF'
feat(rob-15): expose POST /trading/api/decisions/from-operator-request

Operator endpoint that takes OperatorDecisionRequest, persists a
TradingDecisionSession (and proposals) via the new orchestrator, and
returns the trader.robinco.dev session URL. Maps
TradingAgentsNotConfigured to 503 and TradingAgentsRunnerError to 502;
never places orders or registers watches.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 7 — Router unit tests

**Files:**
- Create: `tests/test_operator_decision_session_router.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/test_operator_decision_session_router.py
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_test_client():
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(trading_decisions.router)
    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    return TestClient(app), app, fake_user


@pytest.mark.unit
def test_no_advisory_returns_201_with_session_url(monkeypatch):
    from app.routers import trading_decisions
    from app.services import operator_decision_session_service

    sess_uuid = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    fake_session = SimpleNamespace(
        id=1,
        session_uuid=sess_uuid,
        status="open",
    )
    fake_result = SimpleNamespace(
        session=fake_session,
        proposal_count=1,
        advisory_used=False,
        advisory_skipped_reason="include_tradingagents=False",
    )
    monkeypatch.setattr(
        operator_decision_session_service,
        "create_operator_decision_session",
        AsyncMock(return_value=fake_result),
    )

    # Patch settings.public_base_url and also AsyncSession.commit so router
    # commit doesn't blow up.
    from app.core.db import get_db

    class _FakeDB:
        async def commit(self):
            return None

    fake_db = _FakeDB()
    client, app, _ = _make_test_client()
    app.dependency_overrides[get_db] = lambda: fake_db

    monkeypatch.setattr(
        trading_decisions.settings, "public_base_url", "https://trader.robinco.dev"
    )

    response = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "kr",
            "candidates": [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "side": "buy",
                    "confidence": 70,
                    "proposal_kind": "enter",
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["session_uuid"] == str(sess_uuid)
    assert body["session_url"] == (
        f"https://trader.robinco.dev/trading/decisions/{sess_uuid}"
    )
    assert body["proposal_count"] == 1
    assert body["advisory_used"] is False
    assert body["advisory_skipped_reason"] == "include_tradingagents=False"
    assert response.headers["Location"] == (
        f"/trading/api/decisions/{sess_uuid}"
    )


@pytest.mark.unit
def test_unauthenticated_returns_401(monkeypatch):
    from fastapi import HTTPException

    from app.core.db import get_db
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(trading_decisions.router)
    app.dependency_overrides[get_authenticated_user] = lambda: (_ for _ in ()).throw(
        HTTPException(status_code=401, detail="auth required")
    )
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    client = TestClient(app)
    resp = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "kr",
            "candidates": [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "confidence": 50,
                }
            ],
        },
    )
    assert resp.status_code == 401


@pytest.mark.unit
def test_extra_fields_rejected_with_422():
    client, app, _ = _make_test_client()
    from app.core.db import get_db

    app.dependency_overrides[get_db] = lambda: SimpleNamespace()

    resp = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "kr",
            "candidates": [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "confidence": 50,
                }
            ],
            "place_order": True,  # forbidden by extra='forbid'
        },
    )
    assert resp.status_code == 422


@pytest.mark.unit
def test_tradingagents_not_configured_maps_to_503(monkeypatch):
    from app.core.db import get_db
    from app.services import operator_decision_session_service
    from app.services.tradingagents_research_service import (
        TradingAgentsNotConfigured,
    )

    monkeypatch.setattr(
        operator_decision_session_service,
        "create_operator_decision_session",
        AsyncMock(side_effect=TradingAgentsNotConfigured("missing")),
    )

    class _FakeDB:
        async def commit(self):
            return None

    client, app, _ = _make_test_client()
    app.dependency_overrides[get_db] = lambda: _FakeDB()

    resp = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "us",
            "candidates": [
                {
                    "symbol": "AAPL",
                    "instrument_type": "equity_us",
                    "confidence": 50,
                }
            ],
            "include_tradingagents": True,
        },
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "tradingagents_not_configured"


@pytest.mark.unit
def test_tradingagents_runner_error_maps_to_502(monkeypatch):
    from app.core.db import get_db
    from app.services import operator_decision_session_service
    from app.services.tradingagents_research_service import (
        TradingAgentsRunnerError,
    )

    monkeypatch.setattr(
        operator_decision_session_service,
        "create_operator_decision_session",
        AsyncMock(side_effect=TradingAgentsRunnerError("crash")),
    )

    class _FakeDB:
        async def commit(self):
            return None

    client, app, _ = _make_test_client()
    app.dependency_overrides[get_db] = lambda: _FakeDB()

    resp = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "us",
            "candidates": [
                {
                    "symbol": "AAPL",
                    "instrument_type": "equity_us",
                    "confidence": 50,
                }
            ],
            "include_tradingagents": True,
        },
    )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "tradingagents_runner_failed"


@pytest.mark.unit
def test_response_session_url_falls_back_to_request_origin_when_unconfigured(
    monkeypatch,
):
    from app.core.db import get_db
    from app.routers import trading_decisions
    from app.services import operator_decision_session_service

    sess_uuid = uuid4()
    fake_result = SimpleNamespace(
        session=SimpleNamespace(id=1, session_uuid=sess_uuid, status="open"),
        proposal_count=1,
        advisory_used=False,
        advisory_skipped_reason="include_tradingagents=False",
    )
    monkeypatch.setattr(
        operator_decision_session_service,
        "create_operator_decision_session",
        AsyncMock(return_value=fake_result),
    )

    class _FakeDB:
        async def commit(self):
            return None

    client, app, _ = _make_test_client()
    app.dependency_overrides[get_db] = lambda: _FakeDB()
    monkeypatch.setattr(trading_decisions.settings, "public_base_url", "")

    resp = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "kr",
            "candidates": [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "confidence": 50,
                }
            ],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    # request origin used by TestClient is http://testserver
    assert body["session_url"].startswith(
        "http://testserver/trading/decisions/"
    )
    assert body["session_url"].endswith(str(sess_uuid))
```

- [x] **Step 2: Run tests, verify they pass**

```bash
uv run pytest tests/test_operator_decision_session_router.py -v
```

Expected: 6 passed.

- [x] **Step 3: Commit**

```bash
git add tests/test_operator_decision_session_router.py
git commit -m "$(cat <<'EOF'
test(rob-15): cover operator session router contracts

Authenticated 201 path returns session URL; extra fields produce 422;
TradingAgentsNotConfigured maps to 503; TradingAgentsRunnerError maps
to 502; URL helper falls back to request origin when public_base_url
is unset; unauthenticated requests get 401.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 8 — DB integration test (router → service → DB)

**Files:**
- Create: `tests/test_operator_decision_session_router_integration.py`

- [x] **Step 1: Write the failing test**

```python
# tests/test_operator_decision_session_router_integration.py
"""End-to-end DB integration test against a sqlite-backed AsyncSession.

Mirrors ROB-1 / ROB-14 integration patterns. Mocks the TradingAgents subprocess
runner so no external process is launched.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_advisory_persists_session_and_proposals_in_db(
    db_session, monkeypatch
):
    from app.core.db import get_db
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.models.trading_decision import (
        TradingDecisionProposal,
        TradingDecisionSession,
    )
    from sqlalchemy import select

    fake_user = SimpleNamespace(id=7)
    app = FastAPI()
    app.include_router(trading_decisions.router)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: db_session
    monkeypatch.setattr(
        trading_decisions.settings, "public_base_url", "https://trader.robinco.dev"
    )

    client = TestClient(app)

    resp = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "kr",
            "candidates": [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "side": "buy",
                    "confidence": 70,
                    "proposal_kind": "enter",
                    "rationale": "deterministic op",
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    rows = (await db_session.execute(select(TradingDecisionSession))).scalars().all()
    assert len(rows) == 1
    persisted = rows[0]
    assert persisted.user_id == 7
    assert persisted.market_scope == "kr"
    assert persisted.market_brief["advisory_only"] is True
    assert persisted.market_brief["execution_allowed"] is False

    proposals = (
        await db_session.execute(
            select(TradingDecisionProposal).where(
                TradingDecisionProposal.session_id == persisted.id
            )
        )
    ).scalars().all()
    assert len(proposals) == 1
    p = proposals[0]
    assert p.symbol == "005930"
    assert p.original_payload["advisory_only"] is True
    assert p.original_payload["execution_allowed"] is False
    assert "synthesis" not in p.original_payload
    assert (
        p.original_payload["operator_request"]["applied_policies"] == ["no_advisory"]
    )

    assert body["session_url"] == (
        f"https://trader.robinco.dev/trading/decisions/{persisted.session_uuid}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_advisory_path_persists_synthesis_block(db_session, monkeypatch):
    from app.core.db import get_db
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.models.trading_decision import (
        TradingDecisionProposal,
        TradingDecisionSession,
    )
    from app.schemas.tradingagents_research import (
        TradingAgentsConfigSnapshot,
        TradingAgentsLLM,
        TradingAgentsRunnerResult,
        TradingAgentsWarnings,
    )
    from app.services import operator_decision_session_service
    from sqlalchemy import select

    fake_user = SimpleNamespace(id=7)
    app = FastAPI()
    app.include_router(trading_decisions.router)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: db_session
    monkeypatch.setattr(
        trading_decisions.settings, "public_base_url", "https://trader.robinco.dev"
    )

    fake_runner_result = TradingAgentsRunnerResult(
        status="ok",
        symbol="NVDA",
        as_of_date=date(2026, 4, 28),
        decision="Underweight",
        advisory_only=True,
        execution_allowed=False,
        analysts=["market"],
        llm=TradingAgentsLLM(
            provider="openai-compatible",
            model="gpt-5.5",
            base_url="http://127.0.0.1:8796/v1",
        ),
        config=TradingAgentsConfigSnapshot(
            max_debate_rounds=1,
            max_risk_discuss_rounds=1,
            max_recur_limit=30,
            output_language="English",
            checkpoint_enabled=False,
        ),
        warnings=TradingAgentsWarnings(),
        final_trade_decision="no execution",
        raw_state_keys=["k1"],
    )
    monkeypatch.setattr(
        operator_decision_session_service,
        "run_tradingagents_research",
        AsyncMock(return_value=fake_runner_result),
    )

    client = TestClient(app)
    resp = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "us",
            "candidates": [
                {
                    "symbol": "NVDA",
                    "instrument_type": "equity_us",
                    "side": "buy",
                    "confidence": 70,
                    "proposal_kind": "enter",
                }
            ],
            "include_tradingagents": True,
            "analysts": ["market"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["advisory_used"] is True

    sessions = (
        await db_session.execute(select(TradingDecisionSession))
    ).scalars().all()
    assert len(sessions) == 1
    sess = sessions[0]
    assert sess.market_brief["advisory_only"] is True
    assert sess.market_brief["execution_allowed"] is False
    assert sess.market_brief["synthesis_meta"]["proposal_count"] == 1
    assert sess.source_profile == "operator_request+tradingagents"

    proposals = (
        await db_session.execute(
            select(TradingDecisionProposal).where(
                TradingDecisionProposal.session_id == sess.id
            )
        )
    ).scalars().all()
    assert len(proposals) == 1
    payload = proposals[0].original_payload
    assert payload["advisory_only"] is True
    assert payload["execution_allowed"] is False
    assert payload["synthesis"]["final_side"] == "none"
    assert payload["synthesis"]["final_proposal_kind"] == "pullback_watch"
```

- [x] **Step 2: Run tests, verify they pass**

```bash
uv run pytest tests/test_operator_decision_session_router_integration.py -v
```

Expected: 2 passed.

> **Fixture note for the implementer:** A `db_session` fixture already exists
> in `tests/conftest.py` (used by ROB-1/14 integration tests). If it does not,
> reuse the fixture pattern from
> `tests/test_trading_decisions_router.py` / `tests/services/test_trading_decision_synthesis.py`.
> Do NOT introduce a new in-memory DB harness — match the existing pattern.

- [x] **Step 3: Commit**

```bash
git add tests/test_operator_decision_session_router_integration.py
git commit -m "$(cat <<'EOF'
test(rob-15): integration coverage for router → service → DB

Asserts persisted session and proposals carry advisory_only=True and
execution_allowed=False, that no-advisory path omits the synthesis
block, and that include_tradingagents=True writes the ROB-14 synthesis
meta + per-proposal synthesis dict (with TradingAgents subprocess
mocked).

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 9 — Side-effect proof test (no broker calls)

**Files:**
- Modify: `tests/services/test_operator_decision_session_service_safety.py`

- [x] **Step 1: Append the side-effect-proof test**

```python
@pytest.mark.asyncio
async def test_orchestrator_invokes_only_allowlisted_helpers(monkeypatch):
    """Mock every allowed dependency and assert no other coroutine is awaited.

    This nails the contract: the orchestrator MUST NOT call broker / order /
    watch APIs even if a future refactor accidentally pulls them in. We do
    this by replacing every allowed callable with a tracking AsyncMock and
    sentinel-blocking every forbidden one we can name.
    """
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.services.operator_decision_session_service as svc
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    allowed_create = AsyncMock(
        return_value=SimpleNamespace(
            id=1, session_uuid="x", status="open", market_brief={}
        )
    )
    allowed_add = AsyncMock(return_value=[SimpleNamespace(id=1)])
    monkeypatch.setattr(
        svc.trading_decision_service, "create_decision_session", allowed_create
    )
    monkeypatch.setattr(
        svc.trading_decision_service, "add_decision_proposals", allowed_add
    )

    # Pin every forbidden name we can reach via sys.modules (defensive).
    forbidden_names = (
        "place_order",
        "_place_order_impl",
        "register_watch_alert",
        "register_watch_alert_tools",
        "create_order_intent",
        "submit_order",
    )
    for mod_name, mod in list(sys_modules_snapshot()).items():
        if not mod_name.startswith("app."):
            continue
        for symbol in forbidden_names:
            if hasattr(mod, symbol):
                monkeypatch.setattr(
                    mod,
                    symbol,
                    AsyncMock(side_effect=AssertionError(f"forbidden: {symbol}")),
                    raising=False,
                )

    req = OperatorDecisionRequest(
        market_scope="kr",
        candidates=[
            OperatorCandidate(
                symbol="005930",
                instrument_type="equity_kr",
                side="buy",
                confidence=50,
                proposal_kind="enter",
            )
        ],
    )
    await svc.create_operator_decision_session(
        SimpleNamespace(), user_id=1, request=req
    )

    # The two allowed coroutines must have been awaited; nothing else.
    allowed_create.assert_awaited_once()
    allowed_add.assert_awaited_once()


def sys_modules_snapshot():
    import sys

    return dict(sys.modules)
```

- [x] **Step 2: Run tests, verify they pass**

```bash
uv run pytest tests/services/test_operator_decision_session_service_safety.py -v
```

Expected: 4 passed.

- [x] **Step 3: Commit**

```bash
git add tests/services/test_operator_decision_session_service_safety.py
git commit -m "$(cat <<'EOF'
test(rob-15): assert orchestrator never awaits forbidden broker calls

Replaces every reachable place_order / register_watch_alert /
create_order_intent / submit_order with an asserting AsyncMock and runs
the no-advisory orchestrator end-to-end. Only the two allowlisted
trading_decision_service helpers are awaited.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 10 — Lint, typecheck, full test sweep, summary

**Files:**
- None (verification only)

- [x] **Step 1: Lint and format**

```bash
make lint
make format
```

Expected: zero ruff / ty errors. Fix any introduced violations.

- [x] **Step 2: Run the full test suite (excluding live)**

```bash
uv run pytest tests/ -v -m "not live" --maxfail=5
```

Expected: all green.

- [x] **Step 3: Run safety/forbidden tests in isolation**

```bash
uv run pytest \
  tests/test_trading_decisions_router_safety.py \
  tests/services/test_trading_decision_synthesis_safety.py \
  tests/services/test_operator_decision_session_service_safety.py \
  -v
```

Expected: all green.

- [ ] **Step 4: Smoke the new endpoint manually (optional, only if dev DB is up)**

```bash
# In one shell, with a logged-in dev session cookie set in $COOKIE:
curl -i -X POST http://localhost:8000/trading/api/decisions/from-operator-request \
  -H 'Content-Type: application/json' \
  -H "Cookie: $COOKIE" \
  -d '{
    "market_scope": "kr",
    "candidates": [
      {"symbol":"005930","instrument_type":"equity_kr","side":"buy","confidence":70,"proposal_kind":"enter"}
    ]
  }'
```

Expected: HTTP 201, JSON body with `session_url` ending in
`/trading/decisions/<uuid>`, `advisory_used:false`,
`advisory_skipped_reason:"include_tradingagents=False"`.

- [x] **Step 5: Final summary commit (only if tasks 1–9 already committed)**

If lint/format introduced any whitespace fixes, commit them:

```bash
git status
git diff
git add -A
git commit -m "$(cat <<'EOF'
chore(rob-15): final lint / format pass for operator session

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

Then push and open the PR (base: `main`):

```bash
git push -u origin feature/ROB-15-operator-trading-decision-session
gh pr create --base main --title "feat(rob-15): operator-driven Trading Decision Session" --body "$(cat <<'EOF'
## Summary
- Adds POST /trading/api/decisions/from-operator-request that turns an operator-supplied candidate slate into a persisted TradingDecisionSession plus proposals and returns a trader.robinco.dev session URL.
- include_tradingagents=true reuses the ROB-9 advisory runner and the ROB-14 synthesis policy. Missing TradingAgents config → HTTP 503 (no DB write).
- include_tradingagents=false (default) persists raw operator candidates without an advisory block.
- Safety: subprocess-import test + side-effect proof test prove the orchestrator does not transitively import or await any broker / order / watch / task code. Every persisted row carries advisory_only=True and execution_allowed=False.

## Test plan
- [ ] uv run pytest tests/services/test_operator_decision_session_service.py -v
- [ ] uv run pytest tests/services/test_operator_decision_session_service_safety.py -v
- [ ] uv run pytest tests/test_operator_decision_session_router.py -v
- [ ] uv run pytest tests/test_operator_decision_session_router_integration.py -v
- [ ] uv run pytest tests/test_trading_decision_session_url.py -v
- [ ] uv run pytest tests/test_operator_decision_session_schemas.py -v
- [ ] uv run pytest tests/test_trading_decisions_router_safety.py -v
- [ ] make lint && make format

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## 7. Acceptance / done-when checklist (planner self-review)

- [x] One operator-facing service entry point covering KR / US / CRYPTO.
- [x] Thin/testable seam: caller supplies `OperatorCandidate[]`; account/market
      auto-gathering deferred to follow-up issue (documented §2).
- [x] Optional `include_tradingagents=True/False` toggle (default `False`).
- [x] Persists `TradingDecisionSession` + `TradingDecisionProposal` rows only.
- [x] Returns `session_uuid` + `session_url` resolving to
      `https://trader.robinco.dev/trading/decisions/<uuid>` (or the request
      origin as a development fallback).
- [x] No live orders, no `dry_run=False` calls, no watch registration, no
      order-intent creation. Enforced by §3.1 import safety test, §6.9
      side-effect proof test, and §6.5 forbidden-prefix subprocess scan.
- [x] TradingAgents stays advisory-only: subprocess output flows only through
      ROB-14 synthesis, which already pins `advisory_only=True` /
      `execution_allowed=False`.
- [x] Missing TradingAgents config + `include_tradingagents=True` fails closed
      (HTTP 503, no DB write). Default-off path skips advisory cleanly.
- [x] Tests prove the safety contract — see §6.5, §6.7, §6.8, §6.9.
- [x] No new env vars, no new migrations, no Hermes parser changes.
- [x] Plan does not edit implementation code (planner constraint per task brief).

---

## AOE markers (for handoff)

- **AOE_STATUS:** implemented
- **AOE_ISSUE:** ROB-15
- **AOE_ROLE:** codex-implementer
- **AOE_NEXT:** Ready for review. Implementation was committed as one ROB-15
  changeset per the current handoff instead of the original per-task commit
  cadence. Verification run:
  - `uv run python -m pytest tests/services/test_operator_decision_session_service.py tests/routers/test_trading_decisions_operator_request.py tests/services/test_operator_decision_session_safety.py -q` — 15 passed.
  - `uv run ruff check app/schemas/operator_decision_session.py app/services/trading_decision_session_url.py app/services/operator_decision_session_service.py app/routers/trading_decisions.py tests/services/test_operator_decision_session_service.py tests/routers/test_trading_decisions_operator_request.py tests/services/test_operator_decision_session_safety.py` — passed.
  - `uv run ruff format --check app/schemas/operator_decision_session.py app/services/trading_decision_session_url.py app/services/operator_decision_session_service.py app/routers/trading_decisions.py tests/services/test_operator_decision_session_service.py tests/routers/test_trading_decisions_operator_request.py tests/services/test_operator_decision_session_safety.py` — passed.
  - `make lint` — passed.
  - `uv run pytest tests/test_trading_decisions_router_safety.py tests/services/test_trading_decision_synthesis_safety.py tests/services/test_operator_decision_session_safety.py -v` — 8 passed.
  - `uv run pytest tests/ -v -m "not live" --maxfail=5` — 4225 passed,
    42 skipped, 3 deselected, 2 unrelated failures
    (`tests/test_mcp_call_template.py::test_mcp_call_template_exits_after_first_sse_data_line`
    due `coproc: command not found`; `tests/test_mcp_ohlcv_tools.py::test_get_ohlcv_korean_equity`
    due cached/runtime KR rows instead of the dummy one-row client).

Original handoff:
  1. Read this plan top-to-bottom.
  2. Confirm worktree is `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-15-operator-trading-decision-session` on branch `feature/ROB-15-operator-trading-decision-session`.
  3. Execute Tasks 1–10 in order, committing after each task with the trailer
     `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.
  4. Do NOT modify any module under `app/services/kis*`, `app/services/upbit*`,
     `app/services/orders*`, `app/services/watch_alerts*`,
     `app/mcp_server/tooling/order_execution.py`,
     `app/mcp_server/tooling/watch_alerts_registration.py`, or `app/tasks/*`.
  5. After Task 10, push the branch and open a PR with base `main`. Hand back
     control with `AOE_STATUS=ready-for-review`.
