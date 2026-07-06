import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
CMD = REPO / ".claude" / "commands" / "fill-event-triage.md"


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
