import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
CMD = REPO / ".claude" / "commands" / "crypto-alert-triage.md"


def test_command_exists():
    assert CMD.is_file()


def test_command_invokes_full_bootstrap_sequence():
    body = CMD.read_text(encoding="utf-8")
    for token in (
        "$ARGUMENTS",
        "get_operating_briefing",
        "investment_report_get",
        "session_context_get_recent",
        "session_context_append",
    ):
        assert token in body, f"커맨드에 {token} 누락"


def test_command_states_readonly_contract():
    body = CMD.read_text(encoding="utf-8").lower()
    assert "dry_run" in body
    assert ("read-only" in body) or ("주문" in body and "금지" in body)


def test_command_requires_telegram_safe_triage_sections():
    body = CMD.read_text(encoding="utf-8")
    for token in (
        "알림 요약",
        "제안 verdict",
        "결정 필요",
        "operator 세션에서: `session_context 최근 제안 승인 검토`",
    ):
        assert token in body, f"커맨드에 {token} 누락"
