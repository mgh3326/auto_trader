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
