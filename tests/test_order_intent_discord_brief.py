import ast
import inspect

import pytest

from app.schemas.order_intent_preview import (
    IntentTriggerPreview,
    OrderIntentPreviewItem,
    OrderIntentPreviewResponse,
)
from app.services import order_intent_discord_brief as brief_module
from app.services.order_intent_discord_brief import (
    build_decision_desk_url,
    format_discord_brief,
)


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


def _item(**overrides) -> OrderIntentPreviewItem:
    base = {
        "decision_run_id": "decision-r1",
        "decision_item_id": "item-1",
        "symbol": "005930",
        "market": "KR",
        "side": "buy",
        "intent_type": "buy_candidate",
        "status": "watch_ready",
        "execution_mode": "requires_final_approval",
        "budget_krw": 100000.0,
        "quantity_pct": None,
        "trigger": IntentTriggerPreview(
            metric="price", operator="below", threshold=72000
        ),
        "warnings": [],
    }
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
