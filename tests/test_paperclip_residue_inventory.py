import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

from app.core.config import settings
from app.mcp_server.caller_identity_middleware import CALLER_AGENT_ID_HEADER
from app.models.trade_journal import TradeJournal

ROOT = Path(__file__).resolve().parent.parent
DEAD_TOKENS = {
    "paperclip_api_url",
    "paperclip_api_key",
    "PAPERCLIP_API_URL",
    "PAPERCLIP_API_KEY",
    "load_from_paperclip",
    "--paperclip-issue",
}
ALLOWED_REFERENCE_FILES = {
    "app/mcp_server/README.md",
    "app/mcp_server/caller_identity_middleware.py",
    "app/mcp_server/main.py",
    "app/mcp_server/tooling/trade_journal_registration.py",
    "app/mcp_server/tooling/trade_journal_tools.py",
    "app/models/trade_journal.py",
    "app/schemas/trade_retrospective.py",
    "scripts/templates/mcp_call.sh.tmpl",
}
COMPATIBILITY_MARKERS = {
    "x-paperclip-agent-id",
    "paperclip_agent_id",
    "paperclip_issue_id",
    "legacy paperclip",
    "paperclip-named",
}


def _tracked_texts() -> Iterator[tuple[Path, str]]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "app", "scripts"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    for raw_path in tracked.split(b"\0"):
        if not raw_path:
            continue
        path = ROOT / os.fsdecode(raw_path)
        content = path.read_bytes()
        if b"\0" not in content:
            yield path, content.decode("utf-8", errors="ignore")


def test_dead_paperclip_dependencies_are_removed() -> None:
    for path, text in _tracked_texts():
        for token in DEAD_TOKENS:
            assert token not in text, f"dead token {token!r} remains in {path}"
    assert not hasattr(settings, "paperclip_api_url")
    assert not hasattr(settings, "paperclip_api_key")


def test_remaining_paperclip_references_are_compatibility_surfaces() -> None:
    for path, text in _tracked_texts():
        paperclip_lines = [
            (line_number, line)
            for line_number, line in enumerate(text.splitlines(), start=1)
            if "paperclip" in line.lower()
        ]
        if not paperclip_lines:
            continue

        relative_path = path.relative_to(ROOT).as_posix()
        assert relative_path in ALLOWED_REFERENCE_FILES
        for line_number, line in paperclip_lines:
            normalized_line = line.lower()
            assert any(marker in normalized_line for marker in COMPATIBILITY_MARKERS), (
                f"unmarked Paperclip reference in {relative_path}:{line_number}: {line}"
            )


def test_legacy_named_compatibility_contracts_remain() -> None:
    assert CALLER_AGENT_ID_HEADER == "x-paperclip-agent-id"
    assert "paperclip_issue_id" in TradeJournal.__table__.columns
    assert "ix_trade_journals_paperclip_issue_id" in {
        index.name for index in TradeJournal.__table__.indexes
    }
