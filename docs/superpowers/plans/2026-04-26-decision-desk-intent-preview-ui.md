# Decision Desk Intent Preview UI + Discord Brief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Decision Desk UI panel that builds an Order Intent Preview from a persisted run and lets the operator copy a server-generated Discord-ready markdown brief. UI/operator handoff only — no orders, no watch alerts, no Redis writes, no Discord webhooks.

**Architecture:** A new pure formatter module (`order_intent_discord_brief.py`) generates the markdown server-side and is plumbed through the existing `OrderIntentPreviewService` and `/intent-preview` endpoint via an additive `discord_brief: str | None` field on the response. The Decision Desk template adds a snapshot-mode-only panel that posts to the existing endpoint and copies `response.discord_brief` to the clipboard.

**Tech Stack:** FastAPI, Pydantic v2, pytest + pytest-asyncio, Bootstrap-styled Jinja2 template with vanilla JS (no JS test infra).

**Spec:** `docs/superpowers/specs/2026-04-26-decision-desk-intent-preview-ui-design.md`

---

## File Structure

**Modified:**
- `app/schemas/order_intent_preview.py` — additive `discord_brief: str | None = None`
- `app/services/order_intent_preview_service.py` — kw-only `decision_desk_url=None`, formatter call
- `app/routers/portfolio.py` — `request: Request` param, `decision_desk_url` build
- `app/templates/portfolio_decision_desk.html` — preview panel markup + JS handlers
- `tests/test_order_intent_preview_service.py` — 2 cases for `discord_brief`
- `tests/test_order_intent_preview_router.py` — 1 case asserting brief contains run path

**Created:**
- `app/services/order_intent_discord_brief.py` — pure formatter + URL helper
- `tests/test_order_intent_discord_brief.py` — formatter tests + AST forbidden-import guard

**Per-file responsibility:**
- `order_intent_discord_brief.py` — pure stateless functions; no DB / Redis / httpx / settings imports.
- `order_intent_preview_service.py` — preview build (existing) + a single formatter call. Markdown assembly does not live here.
- `portfolio.py` (router) — composes the URL and passes it down.
- `portfolio_decision_desk.html` — renders preview UI; copies `response.discord_brief` verbatim, never reassembles it.

---

## Task 1: Add additive `discord_brief` field to response schema

**Files:**
- Modify: `app/schemas/order_intent_preview.py:69-74`
- Modify: `tests/test_order_intent_preview_service.py` (append one schema test next to the existing ValidationError tests at the bottom)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_order_intent_preview_service.py`:

```python
@pytest.mark.unit
def test_response_includes_optional_discord_brief_field() -> None:
    from app.schemas.order_intent_preview import OrderIntentPreviewResponse

    response = OrderIntentPreviewResponse(decision_run_id="r")
    assert response.discord_brief is None

    response.discord_brief = "## Order Intent Preview Ready\n"
    dumped = response.model_dump()
    assert dumped["discord_brief"] == "## Order Intent Preview Ready\n"
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_order_intent_preview_service.py::test_response_includes_optional_discord_brief_field -v
```

Expected: FAIL — `AttributeError: 'OrderIntentPreviewResponse' object has no attribute 'discord_brief'` or Pydantic raising on the assignment.

- [ ] **Step 3: Add the field**

In `app/schemas/order_intent_preview.py`, replace the `OrderIntentPreviewResponse` class with:

```python
class OrderIntentPreviewResponse(BaseModel):
    success: bool = True
    decision_run_id: str
    mode: Literal["preview_only"] = "preview_only"
    intents: list[OrderIntentPreviewItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    discord_brief: str | None = None
```

- [ ] **Step 4: Run the new test and the existing schema/service tests**

```
uv run pytest tests/test_order_intent_preview_service.py tests/test_order_intent_preview_router.py -q
```

Expected: all PASS (the new field is additive and existing assertions don't reference it).

- [ ] **Step 5: Commit**

```
git add app/schemas/order_intent_preview.py tests/test_order_intent_preview_service.py
git commit -m "feat(intent-preview): add optional discord_brief field to response

Additive Pydantic field, default None. No behavior change for existing
callers. Will be populated server-side by the Discord brief formatter
in a follow-up commit.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: Create formatter module with URL helper (TDD)

**Files:**
- Create: `app/services/order_intent_discord_brief.py`
- Create: `tests/test_order_intent_discord_brief.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_order_intent_discord_brief.py`:

```python
import pytest

from app.services.order_intent_discord_brief import build_decision_desk_url


@pytest.mark.unit
def test_build_decision_desk_url_strips_trailing_slash() -> None:
    url = build_decision_desk_url("https://trader.robinco.dev/", "decision-r1")
    assert url == "https://trader.robinco.dev/portfolio/decision?run_id=decision-r1"


@pytest.mark.unit
def test_build_decision_desk_url_local_origin() -> None:
    url = build_decision_desk_url("http://localhost:8000", "decision-r1")
    assert url == "http://localhost:8000/portfolio/decision?run_id=decision-r1"


@pytest.mark.unit
def test_build_decision_desk_url_percent_encodes_run_id() -> None:
    url = build_decision_desk_url(
        "https://trader.robinco.dev/", "decision-abc/with slash"
    )
    assert url == (
        "https://trader.robinco.dev/portfolio/decision"
        "?run_id=decision-abc%2Fwith%20slash"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_order_intent_discord_brief.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.order_intent_discord_brief'`.

- [ ] **Step 3: Create the formatter module with the URL helper**

Create `app/services/order_intent_discord_brief.py`:

```python
"""Pure formatter for Decision Desk → Discord handoff brief.

Contract:
- No DB / Redis / httpx / settings / env imports.
- No I/O, no logging side effects, no global state.
- Inputs in → string out. Deterministic for fixed inputs.
"""

from __future__ import annotations

from urllib.parse import quote


def build_decision_desk_url(base_url: str, run_id: str) -> str:
    """Compose `<origin>/portfolio/decision?run_id=<quoted-id>`.

    Pure string operation. Strips trailing slashes from the origin and
    percent-encodes the run id with no safe characters reserved.
    """
    base = base_url.rstrip("/")
    return f"{base}/portfolio/decision?run_id={quote(run_id, safe='')}"
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_order_intent_discord_brief.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```
git add app/services/order_intent_discord_brief.py tests/test_order_intent_discord_brief.py
git commit -m "feat(intent-preview): add Decision Desk URL helper for Discord brief

Pure stdlib-only helper that composes the Decision Desk URL from a
request base_url and run_id. Strips trailing slashes and percent-encodes
the run id. First entry in the new pure formatter module.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: Lock module purity with AST-based forbidden-import guard

**Files:**
- Modify: `tests/test_order_intent_discord_brief.py` (add one test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_order_intent_discord_brief.py`:

```python
import ast
import inspect

from app.services import order_intent_discord_brief as brief_module


@pytest.mark.unit
def test_module_does_not_import_forbidden_modules() -> None:
    """AST-level guard so the module stays import-side-effect free.

    Substring checks would catch forbidden tokens in docstrings; an AST
    walk only inspects actual `import` and `from ... import ...` nodes.
    """
    source = inspect.getsource(brief_module)
    tree = ast.parse(source)

    forbidden_prefixes = (
        "sqlalchemy",
        "redis",
        "httpx",
        "app.core.config",
        "app.tasks",
        "app.services.kis",
        "app.services.upbit",
        "app.services.redis_token_manager",
    )

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported.append(node.module)

    for name in imported:
        for prefix in forbidden_prefixes:
            assert not name.startswith(prefix), (
                f"forbidden import '{name}' in order_intent_discord_brief.py"
            )
```

- [ ] **Step 2: Run test to verify it passes (current module imports only stdlib)**

```
uv run pytest tests/test_order_intent_discord_brief.py::test_module_does_not_import_forbidden_modules -v
```

Expected: PASS — current module imports only `urllib.parse` and `__future__`.

- [ ] **Step 3: Sanity-check the guard fails when expected**

Temporarily prepend `import redis  # noqa` to `app/services/order_intent_discord_brief.py`, then re-run the same test. Expected: FAIL — `forbidden import 'redis'`. Remove the temporary line and re-run; expected PASS again. (Do **not** commit the temporary line.)

- [ ] **Step 4: Commit**

```
git add tests/test_order_intent_discord_brief.py
git commit -m "test(intent-preview): AST guard against side-effecting imports in formatter

Walks ast.Import / ast.ImportFrom nodes and rejects any module whose
name starts with sqlalchemy / redis / httpx / app.core.config / app.tasks /
app.services.kis / app.services.upbit / app.services.redis_token_manager.
Substring-level check would have false-positives on docstring text; the
AST check leaves prose alone.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 4: `format_discord_brief` — header, counts, safety, empty intents (TDD)

**Files:**
- Modify: `tests/test_order_intent_discord_brief.py` (add fixtures + tests)
- Modify: `app/services/order_intent_discord_brief.py` (add formatter)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_order_intent_discord_brief.py`:

```python
from app.schemas.order_intent_preview import (
    IntentTriggerPreview,
    OrderIntentPreviewItem,
    OrderIntentPreviewResponse,
)
from app.services.order_intent_discord_brief import format_discord_brief


def _item(**overrides) -> OrderIntentPreviewItem:
    base = dict(
        decision_run_id="decision-r1",
        decision_item_id="item-1",
        symbol="005930",
        market="KR",
        side="buy",
        intent_type="buy_candidate",
        status="watch_ready",
        execution_mode="requires_final_approval",
        budget_krw=100000.0,
        quantity_pct=None,
        trigger=IntentTriggerPreview(
            metric="price", operator="below", threshold=72000
        ),
        warnings=[],
    )
    base.update(overrides)
    return OrderIntentPreviewItem(**base)


def _response(intents: list[OrderIntentPreviewItem]) -> OrderIntentPreviewResponse:
    return OrderIntentPreviewResponse(decision_run_id="decision-r1", intents=intents)


_DEFAULT_URL = "https://trader.robinco.dev/portfolio/decision?run_id=decision-r1"


@pytest.mark.unit
def test_format_brief_header_lines() -> None:
    out = format_discord_brief(
        preview=_response([]),
        decision_desk_url=_DEFAULT_URL,
        execution_mode="requires_final_approval",
    )
    assert "## Order Intent Preview Ready" in out
    assert f"Decision Desk: {_DEFAULT_URL}" in out
    assert "Run ID: `decision-r1`" in out
    assert "Mode: `preview_only`" in out
    assert "Execution mode: `requires_final_approval`" in out


@pytest.mark.unit
@pytest.mark.parametrize(
    "needle",
    [
        "This is preview-only.",
        "No orders were placed.",
        "No watch alerts were registered.",
        "Final approval is still required before any execution.",
    ],
)
def test_format_brief_safety_text_locked(needle: str) -> None:
    out = format_discord_brief(
        preview=_response([]),
        decision_desk_url=_DEFAULT_URL,
        execution_mode="requires_final_approval",
    )
    assert needle in out


@pytest.mark.unit
def test_format_brief_counts_by_side_and_status() -> None:
    intents = [
        _item(side="buy", intent_type="buy_candidate", status="watch_ready"),
        _item(
            decision_item_id="item-2",
            side="sell",
            intent_type="trim_candidate",
            status="manual_review_required",
            budget_krw=None,
            quantity_pct=30.0,
            trigger=None,
        ),
        _item(
            decision_item_id="item-3",
            side="sell",
            intent_type="sell_watch",
            status="execution_candidate",
            budget_krw=None,
            quantity_pct=100.0,
            trigger=IntentTriggerPreview(
                metric="price", operator="above", threshold=80000
            ),
        ),
    ]
    out = format_discord_brief(
        preview=_response(intents),
        decision_desk_url=_DEFAULT_URL,
        execution_mode="requires_final_approval",
    )
    assert "- Total intents: 3" in out
    assert "- Buy: 1" in out
    assert "- Sell: 2" in out
    assert "- Manual review required: 1" in out
    assert "- Execution candidates: 1" in out
    assert "- Watch ready: 1" in out


@pytest.mark.unit
def test_format_brief_empty_intents_renders_no_intents_marker() -> None:
    out = format_discord_brief(
        preview=_response([]),
        decision_desk_url=_DEFAULT_URL,
        execution_mode="requires_final_approval",
    )
    assert "- Total intents: 0" in out
    assert "(no intents)" in out
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_order_intent_discord_brief.py -v
```

Expected: 4 new tests FAIL — `ImportError: cannot import name 'format_discord_brief'`. URL/AST tests still PASS.

- [ ] **Step 3: Implement `format_discord_brief` (header / summary / safety / empty marker, with a stub for top intents lines)**

Replace the contents of `app/services/order_intent_discord_brief.py` with:

```python
"""Pure formatter for Decision Desk → Discord handoff brief.

Contract:
- No DB / Redis / httpx / settings / env imports.
- No I/O, no logging side effects, no global state.
- Inputs in → string out. Deterministic for fixed inputs.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from app.schemas.order_intent_preview import (
    OrderIntentPreviewItem,
    OrderIntentPreviewResponse,
)

ExecutionMode = Literal["requires_final_approval", "paper_only", "dry_run_only"]
_TOP_INTENTS_DEFAULT_LIMIT = 10
_SAFETY_LINES = (
    "- This is preview-only.",
    "- No orders were placed.",
    "- No watch alerts were registered.",
    "- Final approval is still required before any execution.",
)


def build_decision_desk_url(base_url: str, run_id: str) -> str:
    """Compose `<origin>/portfolio/decision?run_id=<quoted-id>`.

    Pure string operation. Strips trailing slashes from the origin and
    percent-encodes the run id with no safe characters reserved.
    """
    base = base_url.rstrip("/")
    return f"{base}/portfolio/decision?run_id={quote(run_id, safe='')}"


def format_discord_brief(
    *,
    preview: OrderIntentPreviewResponse,
    decision_desk_url: str,
    execution_mode: ExecutionMode,
    top_intents_limit: int = _TOP_INTENTS_DEFAULT_LIMIT,
) -> str:
    """Render a deterministic Discord-ready markdown brief."""
    intents = list(preview.intents)
    counts = _counts(intents)

    lines: list[str] = []
    lines.append("## Order Intent Preview Ready")
    lines.append("")
    lines.append(f"Decision Desk: {decision_desk_url}")
    lines.append(f"Run ID: `{preview.decision_run_id}`")
    lines.append("Mode: `preview_only`")
    lines.append(f"Execution mode: `{execution_mode}`")
    lines.append("")
    lines.append("Summary:")
    lines.append(f"- Total intents: {len(intents)}")
    lines.append(f"- Buy: {counts['buy']}")
    lines.append(f"- Sell: {counts['sell']}")
    lines.append(f"- Manual review required: {counts['manual_review_required']}")
    lines.append(f"- Execution candidates: {counts['execution_candidate']}")
    lines.append(f"- Watch ready: {counts['watch_ready']}")
    lines.append("")
    lines.append("Top intents:")
    lines.extend(_top_intent_lines(intents, top_intents_limit))
    lines.append("")
    lines.append("Safety:")
    lines.extend(_SAFETY_LINES)
    return "\n".join(lines) + "\n"


def _counts(intents: list[OrderIntentPreviewItem]) -> dict[str, int]:
    return {
        "buy": sum(1 for i in intents if i.side == "buy"),
        "sell": sum(1 for i in intents if i.side == "sell"),
        "manual_review_required": sum(
            1 for i in intents if i.status == "manual_review_required"
        ),
        "execution_candidate": sum(
            1 for i in intents if i.status == "execution_candidate"
        ),
        "watch_ready": sum(1 for i in intents if i.status == "watch_ready"),
    }


def _top_intent_lines(
    intents: list[OrderIntentPreviewItem], limit: int
) -> list[str]:
    if not intents:
        return ["(no intents)"]
    # Filled in by Task 5.
    return ["(no intents)"]
```

- [ ] **Step 4: Run tests to verify the four new tests pass**

```
uv run pytest tests/test_order_intent_discord_brief.py -v
```

Expected: header / safety (4 parametrized) / counts / empty all PASS. URL/AST tests still PASS.

- [ ] **Step 5: Commit**

```
git add app/services/order_intent_discord_brief.py tests/test_order_intent_discord_brief.py
git commit -m "feat(intent-preview): scaffold Discord brief formatter (header / counts / safety)

Adds format_discord_brief with header, summary counts, locked safety
footer, and an empty-intents path. Top-intent line rendering is a stub
returning '(no intents)' and will be filled in next.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 5: `format_discord_brief` — top-intent lines + truncation (TDD)

**Files:**
- Modify: `tests/test_order_intent_discord_brief.py` (add tests)
- Modify: `app/services/order_intent_discord_brief.py` (replace `_top_intent_lines` stub)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_order_intent_discord_brief.py`:

```python
@pytest.mark.unit
def test_top_intent_line_buy_with_trigger_and_budget() -> None:
    out = format_discord_brief(
        preview=_response([_item()]),
        decision_desk_url=_DEFAULT_URL,
        execution_mode="requires_final_approval",
    )
    assert (
        "1. `005930` KR buy buy_candidate — watch_ready "
        "— price below 72000 — budget ₩100,000"
    ) in out


@pytest.mark.unit
def test_top_intent_line_sell_manual_review_with_qty() -> None:
    item = _item(
        symbol="KRW-BTC",
        market="CRYPTO",
        side="sell",
        intent_type="trim_candidate",
        status="manual_review_required",
        budget_krw=None,
        quantity_pct=30.0,
        trigger=None,
    )
    out = format_discord_brief(
        preview=_response([item]),
        decision_desk_url=_DEFAULT_URL,
        execution_mode="paper_only",
    )
    assert (
        "1. `KRW-BTC` CRYPTO sell trim_candidate — manual_review_required — qty 30%"
    ) in out


@pytest.mark.unit
def test_top_intents_truncated_at_default_limit_with_more_marker() -> None:
    items = [
        _item(decision_item_id=f"item-{i}", symbol=f"SYM{i:02d}") for i in range(13)
    ]
    out = format_discord_brief(
        preview=_response(items),
        decision_desk_url=_DEFAULT_URL,
        execution_mode="requires_final_approval",
    )
    assert "10. `SYM09`" in out
    assert "11. " not in out
    assert "… and 3 more" in out
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_order_intent_discord_brief.py -v
```

Expected: 3 new tests FAIL — current stub always emits `(no intents)`.

- [ ] **Step 3: Implement `_top_intent_lines`**

In `app/services/order_intent_discord_brief.py`, replace `_top_intent_lines` with:

```python
def _top_intent_lines(
    intents: list[OrderIntentPreviewItem], limit: int
) -> list[str]:
    if not intents:
        return ["(no intents)"]

    visible = intents[:limit]
    overflow = len(intents) - len(visible)
    lines = [_format_top_line(idx, intent) for idx, intent in enumerate(visible, 1)]
    if overflow > 0:
        lines.append(f"… and {overflow} more")
    return lines


def _format_top_line(idx: int, intent: OrderIntentPreviewItem) -> str:
    head = (
        f"{idx}. `{intent.symbol}` {intent.market} "
        f"{intent.side} {intent.intent_type} — {intent.status}"
    )

    trigger_part = ""
    if intent.trigger is not None and intent.trigger.threshold is not None:
        trigger_part = (
            f" — price {intent.trigger.operator} "
            f"{intent.trigger.threshold:g}"
        )

    size_part = ""
    if intent.side == "buy" and intent.budget_krw is not None:
        size_part = f" — budget ₩{int(intent.budget_krw):,}"
    elif intent.side == "sell" and intent.quantity_pct is not None:
        size_part = f" — qty {intent.quantity_pct:g}%"

    return head + trigger_part + size_part
```

- [ ] **Step 4: Run all formatter tests to verify they pass**

```
uv run pytest tests/test_order_intent_discord_brief.py -v
```

Expected: every test in the file PASSES (URL helper, AST guard, header, safety×4, counts, empty, buy line, sell manual-review line, truncation).

- [ ] **Step 5: Commit**

```
git add app/services/order_intent_discord_brief.py tests/test_order_intent_discord_brief.py
git commit -m "feat(intent-preview): render top-intent lines and truncation marker

Per-line format is '{idx}. \`{symbol}\` {market} {side} {intent_type} —
{status}' with optional ' — price {operator} {threshold:g}' and either
' — budget ₩{krw:,}' (buy) or ' — qty {pct:g}%' (sell). Truncates at 10
items and appends '… and N more' (Unicode ellipsis).

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 6: Wire formatter into `OrderIntentPreviewService` (TDD)

**Files:**
- Modify: `tests/test_order_intent_preview_service.py` (append two cases)
- Modify: `app/services/order_intent_preview_service.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_order_intent_preview_service.py`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_preview_omits_discord_brief_when_url_is_none() -> None:
    service = _service(_payload_with_items([_item()]))

    response = await service.build_preview(
        user_id=7,
        run_id="decision-test-run",
        request=OrderIntentPreviewRequest(),
    )

    assert response.discord_brief is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_preview_fills_discord_brief_when_url_provided() -> None:
    service = _service(_payload_with_items([_item()]))

    response = await service.build_preview(
        user_id=7,
        run_id="decision-test-run",
        request=OrderIntentPreviewRequest(),
        decision_desk_url=(
            "https://trader.robinco.dev/portfolio/decision?run_id=decision-test-run"
        ),
    )

    assert response.discord_brief is not None
    assert (
        "https://trader.robinco.dev/portfolio/decision?run_id=decision-test-run"
        in response.discord_brief
    )
    assert "Mode: `preview_only`" in response.discord_brief
    assert "Run ID: `decision-test-run`" in response.discord_brief
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_order_intent_preview_service.py::test_build_preview_omits_discord_brief_when_url_is_none tests/test_order_intent_preview_service.py::test_build_preview_fills_discord_brief_when_url_provided -v
```

Expected: first FAILS on `AttributeError`/`response.discord_brief` was never set (still `None` is fine — but the *fills* test asserts non-None and will FAIL on `TypeError: build_preview() got an unexpected keyword argument 'decision_desk_url'`).

- [ ] **Step 3: Add the kw-only param and the formatter call**

Edit `app/services/order_intent_preview_service.py`:

1. Add the import next to the existing schema import:

```python
from app.services.order_intent_discord_brief import format_discord_brief
```

2. Replace the `build_preview` method signature and body's tail:

```python
    async def build_preview(
        self,
        *,
        user_id: int,
        run_id: str,
        request: OrderIntentPreviewRequest,
        decision_desk_url: str | None = None,
    ) -> OrderIntentPreviewResponse:
        payload = await self._decision_service.get_decision_run(
            user_id=user_id,
            run_id=run_id,
        )
        intents: list[OrderIntentPreviewItem] = []
        warnings: list[str] = []

        selection_map = self._selections_by_id(request.selections)

        for group in payload.get("symbol_groups", []):
            for item in group.get("items", []):
                item_id = item.get("id")
                selection = selection_map.get(item_id) if item_id else None
                intent = self._build_intent_for_item(
                    run_id=run_id,
                    group=group,
                    item=item,
                    request=request,
                    selection=selection,
                )
                if intent is not None:
                    intents.append(intent)

        response = OrderIntentPreviewResponse(
            decision_run_id=run_id,
            intents=intents,
            warnings=warnings,
        )
        if decision_desk_url is not None:
            response.discord_brief = format_discord_brief(
                preview=response,
                decision_desk_url=decision_desk_url,
                execution_mode=request.execution_mode,
            )
        return response
```

(Only the construction-of-response and trailing block changed; the loop above is identical to the existing implementation.)

- [ ] **Step 4: Run the service test suite**

```
uv run pytest tests/test_order_intent_preview_service.py -q
```

Expected: all PASS, including the two new ones and the existing forbidden-symbol guard (`format_discord_brief` is allowed; `redis`/`place_order`/etc. are not introduced).

- [ ] **Step 5: Commit**

```
git add app/services/order_intent_preview_service.py tests/test_order_intent_preview_service.py
git commit -m "feat(intent-preview): plumb Discord brief formatter into preview service

OrderIntentPreviewService.build_preview now accepts an optional
decision_desk_url kwarg. When supplied, the response is enriched with a
discord_brief string produced by the pure formatter. When omitted (the
default for direct callers), the response remains identical to before.
Markdown assembly stays in the formatter module — the service only calls
into it.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 7: Wire URL building into the `/intent-preview` router (TDD)

**Files:**
- Modify: `tests/test_order_intent_preview_router.py` (replace the existing stub fake_preview to defer to a real service-side mock so we can assert the brief is present)
- Modify: `app/routers/portfolio.py` (preview endpoint only)

- [ ] **Step 1: Write the failing test**

The existing `_make_client()` returns a fixed empty-intents response, which would mask the brief. Add a parallel client builder that uses the *real* `OrderIntentPreviewService` over a mocked `PortfolioDecisionService`, so the brief gets populated end-to-end.

Append to `tests/test_order_intent_preview_router.py`:

```python
from app.services.order_intent_preview_service import OrderIntentPreviewService


def _make_client_with_real_preview_service():
    app = FastAPI()
    app.include_router(portfolio.router)

    fake_decision_service = AsyncMock()
    fake_decision_service.get_decision_run = AsyncMock(
        return_value={
            "success": True,
            "decision_run": {
                "id": "decision-r1",
                "generated_at": "2026-04-20T10:00:00+00:00",
                "mode": "analysis_only",
                "persisted": True,
                "source": "portfolio_decision_service_v1",
            },
            "filters": {"market": "ALL", "account_keys": [], "q": None},
            "summary": {
                "symbols": 0,
                "decision_items": 0,
                "actionable_items": 0,
                "manual_review_items": 0,
                "auto_candidate_items": 0,
                "missing_context_items": 0,
                "by_action": {},
                "by_market": {},
            },
            "facets": {"accounts": []},
            "symbol_groups": [],
            "warnings": [],
        }
    )

    real_preview_service = OrderIntentPreviewService(
        decision_service=fake_decision_service
    )

    app.dependency_overrides[portfolio.get_authenticated_user] = lambda: (
        SimpleNamespace(id=7)
    )
    app.dependency_overrides[portfolio.get_order_intent_preview_service] = (
        lambda: real_preview_service
    )
    return TestClient(app)


@pytest.mark.unit
def test_preview_endpoint_response_includes_discord_brief_with_run_path() -> None:
    client = _make_client_with_real_preview_service()

    response = client.post(
        "/portfolio/api/decision-runs/decision-r1/intent-preview",
        json={
            "budget": {"default_buy_budget_krw": 100000},
            "selections": [],
            "execution_mode": "requires_final_approval",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "preview_only"
    assert "discord_brief" in body
    assert body["discord_brief"] is not None
    # Path/query substring is asserted (not full origin) — TestClient base
    # URL is environment-dependent.
    assert "/portfolio/decision?run_id=decision-r1" in body["discord_brief"]
    assert "Mode: `preview_only`" in body["discord_brief"]
    assert "This is preview-only." in body["discord_brief"]
```

- [ ] **Step 2: Run the new test to verify it fails**

```
uv run pytest tests/test_order_intent_preview_router.py::test_preview_endpoint_response_includes_discord_brief_with_run_path -v
```

Expected: FAIL — `body["discord_brief"]` is `None` because the router doesn't yet pass `decision_desk_url`.

- [ ] **Step 3: Update the preview endpoint**

Edit `app/routers/portfolio.py`:

1. Add the import next to existing service imports:

```python
from app.services.order_intent_discord_brief import build_decision_desk_url
```

(`Request` is already imported — verify with `grep "^from fastapi" app/routers/portfolio.py` and only add it if missing.)

2. Replace the preview endpoint with:

```python
@router.post(
    "/api/decision-runs/{run_id}/intent-preview",
    responses={
        404: {"description": "Decision run not found"},
        500: {"description": "Failed to build order intent preview"},
    },
)
async def preview_order_intents_for_decision_run(
    run_id: str,
    payload: OrderIntentPreviewRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    preview_service: Annotated[
        OrderIntentPreviewService, Depends(get_order_intent_preview_service)
    ],
) -> OrderIntentPreviewResponse:
    # TODO(follow-up): respect PUBLIC_BASE_URL / X-Forwarded-* origin once
    # the public Decision Desk URL diverges from request.base_url under
    # proxies. For now request.base_url works for direct + standard setups.
    decision_desk_url = build_decision_desk_url(str(request.base_url), run_id)
    try:
        return await preview_service.build_preview(
            user_id=current_user.id,
            run_id=run_id,
            request=payload,
            decision_desk_url=decision_desk_url,
        )
    except PortfolioDecisionRunNotFoundError as e:
        raise HTTPException(
            status_code=404,
            detail=DECISION_RUN_NOT_FOUND_DETAIL,
        ) from e
    except Exception as e:
        logger.error("Error building intent preview: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=INTENT_PREVIEW_ERROR_DETAIL,
        ) from e
```

- [ ] **Step 4: Run router and decision-run test suites**

```
uv run pytest tests/test_order_intent_preview_router.py tests/test_portfolio_decision_router.py tests/test_portfolio_decision_service.py tests/test_portfolio_decision_run_model.py -q
```

Expected: all PASS — the new test, the existing 200/404 cases, and the surrounding decision-run tests.

- [ ] **Step 5: Commit**

```
git add app/routers/portfolio.py tests/test_order_intent_preview_router.py
git commit -m "feat(intent-preview): pass Decision Desk URL from router to preview service

The /intent-preview endpoint now composes a Decision Desk URL from
request.base_url + run_id and forwards it to the service so the response
carries discord_brief. 422/404/500 behavior is unchanged. A TODO is left
for a follow-up to honor PUBLIC_BASE_URL / X-Forwarded-* under proxies.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 8: Add Decision Desk preview panel HTML

**Files:**
- Modify: `app/templates/portfolio_decision_desk.html` (insert one `<section>` between `#summary-section` and the `#filter-form` card)

- [ ] **Step 1: Insert the panel markup**

In `app/templates/portfolio_decision_desk.html`, find the closing `</div>` of the summary section (the line right after the spinner card on roughly line 40, before the `<!-- Filters -->` comment) and insert this block immediately after it (before `<!-- Filters -->`):

```html
    <!-- Order Intent Preview (snapshot mode only) -->
    <section id="intent-preview-section" class="card border-0 shadow-sm mb-4 d-none" aria-labelledby="intent-preview-title">
        <div class="card-body">
            <div class="d-flex justify-content-between align-items-center mb-3">
                <h2 id="intent-preview-title" class="h5 mb-0">Order Intent Preview</h2>
                <span class="badge bg-info-subtle text-info-emphasis">preview_only</span>
            </div>

            <div class="alert alert-warning small mb-3" role="note">
                Preview only — no order, watch alert, Redis watch key, broker task, or Paperclip action is created.
            </div>

            <div class="row g-3 align-items-end mb-3">
                <div class="col-md-4">
                    <label for="intent-default-buy-budget" class="form-label small text-uppercase fw-bold text-muted">
                        Default buy budget (KRW)
                    </label>
                    <input id="intent-default-buy-budget" type="number" min="0" step="1000" class="form-control" placeholder="100000">
                </div>
                <div class="col-md-4">
                    <label for="intent-execution-mode" class="form-label small text-uppercase fw-bold text-muted">
                        Execution mode
                    </label>
                    <select id="intent-execution-mode" class="form-select">
                        <option value="requires_final_approval" selected>requires_final_approval</option>
                        <option value="paper_only">paper_only</option>
                        <option value="dry_run_only">dry_run_only</option>
                    </select>
                </div>
                <div class="col-md-2">
                    <button id="build-intent-preview-btn" type="button" class="btn btn-primary w-100">
                        Build Intent Preview
                    </button>
                </div>
                <div class="col-md-2">
                    <button id="copy-intent-discord-brief-btn" type="button" class="btn btn-outline-secondary w-100" disabled>
                        <i class="bi bi-clipboard"></i> Copy Discord Brief
                    </button>
                </div>
            </div>

            <div id="intent-preview-status" class="small text-muted mb-2" aria-live="polite"></div>

            <div id="intent-preview-result" class="d-none">
                <div class="row g-2 mb-3" id="intent-preview-counts"></div>
                <div class="table-responsive">
                    <table class="table table-sm align-middle mb-0">
                        <thead>
                            <tr>
                                <th>#</th>
                                <th>Symbol</th>
                                <th>Market</th>
                                <th>Side</th>
                                <th>Type</th>
                                <th>Status</th>
                                <th>Trigger</th>
                                <th>Size</th>
                                <th>Warnings</th>
                            </tr>
                        </thead>
                        <tbody id="intent-preview-rows"></tbody>
                    </table>
                </div>
                <div id="intent-preview-truncation" class="small text-muted mt-2 d-none"></div>
            </div>
        </div>
    </section>
```

- [ ] **Step 2: Run the template shell test**

```
uv run pytest tests/test_portfolio_decision_router.py::test_portfolio_decision_page_renders_html tests/test_portfolio_decision_router.py::test_portfolio_decision_page_with_run_id_renders_html_shell -v
```

Expected: PASS — page still renders 200 and contains `id="portfolio-decision-desk-page"`.

- [ ] **Step 3: Commit**

```
git add app/templates/portfolio_decision_desk.html
git commit -m "feat(intent-preview): add Decision Desk preview panel markup

Adds a snapshot-mode-only Order Intent Preview section between the
summary cards and the filter form. Includes a default-buy-budget input,
execution-mode select, Build/Copy buttons, a result table, and the
preview-only safety banner. JS handlers come next; the section stays
hidden by default via .d-none.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 9: Wire up Decision Desk preview panel JS

**Files:**
- Modify: `app/templates/portfolio_decision_desk.html` (append handlers at the end of the existing `DOMContentLoaded` callback, before its closing `});`)

- [ ] **Step 1: Add the handlers and renderers**

Inside the existing `document.addEventListener('DOMContentLoaded', function() { ... })` block in `app/templates/portfolio_decision_desk.html`, immediately above the line `// Initial fetch` (which contains `fetchSlate({updateUrl: false});`), append:

```js
        const intentPreviewSection = document.getElementById('intent-preview-section');
        const intentPreviewBudgetInput = document.getElementById('intent-default-buy-budget');
        const intentPreviewModeSelect = document.getElementById('intent-execution-mode');
        const buildIntentPreviewBtn = document.getElementById('build-intent-preview-btn');
        const copyIntentBriefBtn = document.getElementById('copy-intent-discord-brief-btn');
        const intentPreviewStatus = document.getElementById('intent-preview-status');
        const intentPreviewResult = document.getElementById('intent-preview-result');
        const intentPreviewCounts = document.getElementById('intent-preview-counts');
        const intentPreviewRows = document.getElementById('intent-preview-rows');
        const intentPreviewTruncation = document.getElementById('intent-preview-truncation');
        const INTENT_PREVIEW_ROW_LIMIT = 10;

        let lastIntentBrief = null;

        if (isSnapshotMode) {
            intentPreviewSection.classList.remove('d-none');
            buildIntentPreviewBtn.addEventListener('click', buildIntentPreview);
            copyIntentBriefBtn.addEventListener('click', copyIntentBrief);
        }

        async function buildIntentPreview() {
            if (!snapshotRunId) return;
            setPreviewStatus('Building preview…', 'text-muted');
            intentPreviewResult.classList.add('d-none');
            buildIntentPreviewBtn.disabled = true;
            copyIntentBriefBtn.disabled = true;
            lastIntentBrief = null;

            const raw = intentPreviewBudgetInput.value.trim();
            const parsed = raw === '' ? null : Number(raw);
            const defaultBuyBudgetKrw = parsed !== null && Number.isFinite(parsed) ? parsed : null;

            const body = {
                budget: { default_buy_budget_krw: defaultBuyBudgetKrw },
                selections: [],
                execution_mode: intentPreviewModeSelect.value,
            };

            try {
                const response = await fetch(
                    `/portfolio/api/decision-runs/${encodeURIComponent(snapshotRunId)}/intent-preview`,
                    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
                );
                const data = await response.json().catch(() => ({}));
                if (!response.ok) {
                    setPreviewStatus(formatPreviewError(response.status, data), 'text-danger');
                    return;
                }
                renderIntentPreview(data);
                if (data.discord_brief) {
                    lastIntentBrief = data.discord_brief;
                    copyIntentBriefBtn.disabled = false;
                }
                setPreviewStatus(`Preview built (${data.intents.length} intents).`, 'text-success');
            } catch (err) {
                setPreviewStatus(`Error: ${err.message}`, 'text-danger');
            } finally {
                buildIntentPreviewBtn.disabled = false;
            }
        }

        async function copyIntentBrief() {
            if (!lastIntentBrief) return;
            try {
                await navigator.clipboard.writeText(lastIntentBrief);
                const original = copyIntentBriefBtn.innerHTML;
                copyIntentBriefBtn.innerHTML = '<i class="bi bi-check2"></i> Copied';
                setTimeout(() => { copyIntentBriefBtn.innerHTML = original; }, 1500);
            } catch (err) {
                setPreviewStatus(`Clipboard error: ${err.message}`, 'text-danger');
            }
        }

        function setPreviewStatus(message, toneClass) {
            intentPreviewStatus.textContent = message;
            intentPreviewStatus.className = `small ${toneClass} mb-2`;
        }

        function formatPreviewError(status, data) {
            if (status === 422 && data && Array.isArray(data.detail)) {
                const msgs = data.detail.map(d => d && d.msg).filter(Boolean);
                if (msgs.length > 0) return msgs.join('; ');
                return 'Invalid input.';
            }
            if (data && typeof data.detail === 'string') return data.detail;
            return `Request failed (${status}).`;
        }

        function renderIntentPreview(data) {
            intentPreviewResult.classList.remove('d-none');

            const total = data.intents.length;
            const counts = {
                buy: 0, sell: 0,
                manual_review_required: 0, execution_candidate: 0,
                watch_ready: 0, invalid: 0,
            };
            data.intents.forEach(i => {
                if (i.side === 'buy') counts.buy += 1;
                if (i.side === 'sell') counts.sell += 1;
                if (i.status in counts) counts[i.status] += 1;
            });

            intentPreviewCounts.replaceChildren();
            appendCountChip('Total', total);
            appendCountChip('Buy', counts.buy);
            appendCountChip('Sell', counts.sell);
            appendCountChip('Manual review', counts.manual_review_required);
            appendCountChip('Execution candidate', counts.execution_candidate);
            appendCountChip('Watch ready', counts.watch_ready);
            appendCountChip('Invalid', counts.invalid);

            intentPreviewRows.replaceChildren();
            if (data.intents.length === 0) {
                const tr = document.createElement('tr');
                const td = document.createElement('td');
                td.colSpan = 9;
                td.className = 'text-muted text-center';
                td.textContent = '(no intents)';
                tr.appendChild(td);
                intentPreviewRows.appendChild(tr);
            } else {
                data.intents.slice(0, INTENT_PREVIEW_ROW_LIMIT).forEach((intent, idx) => {
                    intentPreviewRows.appendChild(renderIntentRow(idx + 1, intent));
                });
            }

            if (data.intents.length > INTENT_PREVIEW_ROW_LIMIT) {
                intentPreviewTruncation.textContent =
                    `Showing ${INTENT_PREVIEW_ROW_LIMIT} of ${data.intents.length} intents — see full list in Discord brief.`;
                intentPreviewTruncation.classList.remove('d-none');
            } else {
                intentPreviewTruncation.classList.add('d-none');
            }
        }

        function appendCountChip(label, value) {
            const col = document.createElement('div');
            col.className = 'col-auto';
            const span = document.createElement('span');
            span.className = 'badge bg-light text-dark border';
            span.textContent = `${label}: ${value}`;
            col.appendChild(span);
            intentPreviewCounts.appendChild(col);
        }

        function renderIntentRow(idx, intent) {
            const tr = document.createElement('tr');
            const triggerText = intent.trigger
                ? `${intent.trigger.metric} ${intent.trigger.operator} ${intent.trigger.threshold}`
                : '';
            let sizeText = '';
            if (intent.side === 'buy' && intent.budget_krw != null) {
                sizeText = `₩${Number(intent.budget_krw).toLocaleString()}`;
            } else if (intent.side === 'sell' && intent.quantity_pct != null) {
                sizeText = `${intent.quantity_pct}%`;
            }
            const cells = [
                String(idx),
                intent.symbol,
                intent.market,
                intent.side,
                intent.intent_type,
                intent.status,
                triggerText,
                sizeText,
                (intent.warnings || []).join(', '),
            ];
            cells.forEach(text => {
                const td = document.createElement('td');
                td.textContent = text;
                tr.appendChild(td);
            });
            return tr;
        }
```

(Functions are declared with `function`, so hoisting allows the `if (isSnapshotMode)` block to reference them before their declarations.)

- [ ] **Step 2: Run the template shell test**

```
uv run pytest tests/test_portfolio_decision_router.py -q
```

Expected: PASS — JS additions don't change rendered HTML markers.

- [ ] **Step 3: Manual browser verification (golden path)**

Start the dev server in a side terminal:

```
make dev
```

Then in the browser, while logged in:

1. Open `/portfolio/decision?run_id=<persisted-run-id>` (use any persisted run id from a recent share-link). The Order Intent Preview section appears between the summary cards and the filter form.
2. Click `Build Intent Preview`. Verify:
   - Status line shows `Preview built (N intents).`
   - Counts chips render (Total / Buy / Sell / Manual review / Execution candidate / Watch ready / Invalid).
   - The first up-to-10 intents render in the table.
   - If the run has more than 10 intents, the truncation hint appears.
3. Click `Copy Discord Brief`. Paste into a scratch buffer. Verify the text contains:
   - The Decision Desk URL with `?run_id=<the-run-id>`.
   - `Run ID: \`<the-run-id>\``.
   - `Mode: \`preview_only\``.
   - All four safety lines: `This is preview-only.`, `No orders were placed.`, `No watch alerts were registered.`, `Final approval is still required before any execution.`.
4. Open `/portfolio/decision` (no `run_id`). Verify the preview section stays hidden.
5. Confirm no side effects via Redis:

```
docker compose exec redis redis-cli --scan --pattern 'watch_alerts:*' | wc -l
docker compose exec redis redis-cli --scan --pattern 'model_rate_limit:*' | wc -l
```

Run those two commands once before clicking Build Intent Preview and once after. Both counts must be unchanged.

- [ ] **Step 4: Manual browser verification (negative paths)**

1. Type a negative number into `Default buy budget (KRW)` and click Build Intent Preview. Status line turns red and surfaces the FastAPI 422 message; result and Copy stay disabled.
2. Manually edit the URL to `?run_id=does-not-exist` and click Build Intent Preview. Status line shows `Decision run not found.`.

- [ ] **Step 5: Commit**

```
git add app/templates/portfolio_decision_desk.html
git commit -m "feat(intent-preview): wire Decision Desk preview panel JS

Build Intent Preview posts to the existing /intent-preview endpoint with
the default buy budget and execution mode, renders counts + first-10
intents, and enables Copy Discord Brief once the response carries
discord_brief. The brief is copied verbatim — never reassembled on the
client. All cell values go through textContent (no innerHTML for data).

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 10: Final verification sweep

**Files:**
- None (verification + optional ruff format autofix)

- [ ] **Step 1: Run ruff format check and lint**

```
uv run ruff format --check app/ tests/
uv run ruff check app/ tests/
```

Expected: both clean. If ruff format reports a diff, run `uv run ruff format app/ tests/` and amend the most recent task's commit only if the diff is purely cosmetic (otherwise commit separately as `style: ruff format` and continue).

- [ ] **Step 2: Run the full backend test sweep listed in the brief**

```
uv run pytest tests/test_order_intent_discord_brief.py -q
uv run pytest tests/test_order_intent_preview_service.py tests/test_order_intent_preview_router.py -q
uv run pytest tests/test_portfolio_decision_router.py tests/test_portfolio_decision_service.py tests/test_portfolio_decision_run_model.py -q
```

Expected: all green.

- [ ] **Step 3: Confirm safety-guard regression assertions**

```
uv run pytest tests/test_order_intent_discord_brief.py::test_module_does_not_import_forbidden_modules tests/test_order_intent_preview_service.py::test_preview_service_does_not_import_order_or_redis_modules -v
```

Expected: both PASS — the AST guard on the new formatter and the existing substring guard on the preview service.

- [ ] **Step 4: Inspect the diff for forbidden symbols**

```
git diff main -- app/ tests/ app/templates/ | grep -nE 'place_order|manage_watch_alerts|broker|paperclip|webhook|httpx\.post|requests\.post' || echo 'clean'
```

Expected: prints `clean`. Any hit must be reviewed before opening the PR.

- [ ] **Step 5: Push the branch and open the PR**

```
git push -u origin HEAD
gh pr create --base main --title "feat(intent-preview): add decision desk preview panel and discord brief" --body "$(cat <<'EOF'
## Summary
- Adds a snapshot-mode-only Order Intent Preview panel to the Decision Desk page that posts to the already-deployed `/intent-preview` endpoint.
- Generates a deterministic Discord-ready markdown brief server-side via a new pure formatter and returns it on the existing response as an additive `discord_brief: str | None` field.
- UI/operator handoff only — no orders, no watch alerts, no Redis writes (beyond existing session/auth), no Paperclip writes, no Discord webhook send.

## Test plan
- [x] `uv run ruff format --check app/ tests/`
- [x] `uv run ruff check app/ tests/`
- [x] `uv run pytest tests/test_order_intent_discord_brief.py -q`
- [x] `uv run pytest tests/test_order_intent_preview_service.py tests/test_order_intent_preview_router.py -q`
- [x] `uv run pytest tests/test_portfolio_decision_router.py tests/test_portfolio_decision_service.py tests/test_portfolio_decision_run_model.py -q`
- [x] Manual: Build Intent Preview + Copy Discord Brief on a persisted run; verified safety text in pasted brief; Redis `watch_alerts:*` / `model_rate_limit:*` counts unchanged.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review notes

**Spec coverage** — every numbered section of the spec maps to at least one task:
- Spec §4.1 (schema additive) → Task 1.
- Spec §4.2 (formatter module + URL helper) → Tasks 2–3.
- Spec §4.3 (markdown layout) → Task 4.
- Spec §4.4 (top intents lines + truncation + empty marker) → Task 5.
- Spec §4.5 (service wiring) → Task 6.
- Spec §4.6 (router wiring + TODO) → Task 7.
- Spec §4.7 (contract diff) — verified by Task 1 + Task 6 + Task 7 tests.
- Spec §5 (template UI) → Tasks 8–9.
- Spec §6 (test plan) → Tasks 1, 4–7 (auto), 9 (manual).
- Spec §7 (safety constraints) → Task 3 AST guard + existing service substring guard preserved + Task 10 step 4 diff scan.
- Spec §9 (acceptance criteria) → Task 9 manual checklist + Task 10 PR description.

**Type/name consistency** — `format_discord_brief`, `build_decision_desk_url`, `decision_desk_url`, `discord_brief` are spelled identically across every task.

**Placeholder scan** — every code-changing step shows complete code; commit messages are concrete; commands have expected outputs; manual steps list specific UI interactions and Redis commands.
