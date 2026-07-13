from pathlib import Path

from app.core.config import settings
from app.mcp_server.caller_identity_middleware import CALLER_AGENT_ID_HEADER
from app.models.trade_journal import TradeJournal

ROOT = Path(__file__).resolve().parent.parent
SOURCE_SUFFIXES = {".md", ".py", ".sh", ".tmpl"}
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


def test_dead_paperclip_dependencies_are_removed() -> None:
    for relative in ("app", "scripts"):
        for path in (ROOT / relative).rglob("*"):
            if path.is_file() and path.suffix in SOURCE_SUFFIXES:
                text = path.read_text(encoding="utf-8", errors="ignore")
                for token in DEAD_TOKENS:
                    assert token not in text, f"dead token {token!r} remains in {path}"
    assert not hasattr(settings, "paperclip_api_url")
    assert not hasattr(settings, "paperclip_api_key")


def test_remaining_paperclip_references_are_compatibility_surfaces() -> None:
    paths = set()
    for relative in ("app", "scripts"):
        for path in (ROOT / relative).rglob("*"):
            if (
                path.is_file()
                and path.suffix in SOURCE_SUFFIXES
                and "paperclip"
                in path.read_text(encoding="utf-8", errors="ignore").lower()
            ):
                paths.add(path.relative_to(ROOT).as_posix())
    assert paths <= ALLOWED_REFERENCE_FILES


def test_legacy_named_compatibility_contracts_remain() -> None:
    assert CALLER_AGENT_ID_HEADER == "x-paperclip-agent-id"
    assert "paperclip_issue_id" in TradeJournal.__table__.columns
    assert "ix_trade_journals_paperclip_issue_id" in {
        index.name for index in TradeJournal.__table__.indexes
    }
