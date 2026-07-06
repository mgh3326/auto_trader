import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
CMD = REPO / ".claude" / "commands" / "fill-event-triage.md"
pytestmark = pytest.mark.unit


def test_command_exists():
    assert CMD.is_file()


def test_command_invokes_full_bootstrap_sequence():
    body = CMD.read_text(encoding="utf-8")
    for token in (
        "$ARGUMENTS",
        "get_operating_briefing",
        "get_cash_balance",
        "session_context_get_recent",
        "session_context_append",
    ):
        assert token in body, f"커맨드에 {token} 누락"


def test_command_covers_sell_and_redeploy():
    body = CMD.read_text(encoding="utf-8").lower()
    assert "sell" in body
    assert "redeploy" in body


def test_command_states_readonly_contract():
    body = CMD.read_text(encoding="utf-8").lower()
    assert ("read-only" in body) or ("주문" in body and "금지" in body)


def test_command_does_not_reference_denied_tools():
    """Slash command must not instruct the LLM to call tools denied by settings.readonly.json."""
    cmd_body = CMD.read_text(encoding="utf-8")
    settings = json.loads(
        (REPO / ".claude" / "settings.readonly.json").read_text(encoding="utf-8")
    )
    denied = set(settings.get("permissions", {}).get("deny", []))
    # Check every mcp__auto_trader_local__* tool name mentioned in the command
    mentioned_tools: set[str] = set(
        re.findall(r"mcp__auto_trader_local__(\w+)", cmd_body)
    )
    # Also check bare tool names that appear in the deny list
    for tool_suffix in re.findall(
        r"\b(buy_ladder_fill_preview|sell_ladder_fill_preview|place_order|cancel_order|modify_order|reconcile_orders)\b",
        cmd_body,
    ):
        mentioned_tools.add(tool_suffix)
    denied_suffixes = {
        d.replace("mcp__auto_trader_local__", "")
        for d in denied
        if d.startswith("mcp__auto_trader_local__")
    }
    contradictions = mentioned_tools & denied_suffixes
    assert not contradictions, (
        f"Slash command references tools that are denied in settings.readonly.json: {contradictions}. "
        f"Either remove the reference from the command or remove the tool from the deny list."
    )
