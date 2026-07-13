# ROB-865 Paperclip Residue Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove dead Paperclip integrations while preserving the canonical caller header and legacy trade-journal schema/API compatibility.

**Architecture:** Encode the A-versus-B inventory boundary as a source-level regression test, then make the smallest deletions and documentation edits needed to satisfy it. Runtime data flow is unchanged except that the dead CIO CLI network input and unused settings disappear.

**Tech Stack:** Python 3.13, pytest, Pydantic Settings, SQLAlchemy, Bash template, Ruff, ty

## Global Constraints

- Remove inventory A only.
- Never delete or rename `x-paperclip-agent-id`; it remains the canonical MCP caller identity header.
- Keep `trade_journal.paperclip_issue_id`, its index, MCP parameters, and behavior; change comments/documentation only.
- Do not modify archived `docs/plans/**` files.
- Use TDD: observe the new regression test fail before changing production code.

---

### Task 1: Lock the inventory boundary and remove dead dependencies

**Files:**
- Create: `tests/test_paperclip_residue_inventory.py`
- Modify: `app/core/config.py`
- Modify: `scripts/cio_quality_gate.py`
- Modify: `app/mcp_server/caller_identity_middleware.py`
- Modify: `app/mcp_server/main.py`
- Modify: `app/mcp_server/tooling/trade_journal_tools.py`
- Modify: `app/mcp_server/tooling/trade_journal_registration.py`
- Modify: `app/models/trade_journal.py`
- Modify: `app/schemas/trade_retrospective.py`
- Modify: `app/mcp_server/README.md`
- Modify: `scripts/templates/mcp_call.sh.tmpl`
- Local-only modify: `.env.prod`

**Interfaces:**
- Consumes: ROB-864's Telegram two-step loss-cut authorization on `origin/main`.
- Produces: unchanged `CALLER_AGENT_ID_HEADER == "x-paperclip-agent-id"`; unchanged `TradeJournal.paperclip_issue_id` column and `ix_trade_journals_paperclip_issue_id` index; CIO CLI inputs limited to file/stdin.

- [ ] **Step 1: Write the failing inventory regression test**

```python
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
                and "paperclip" in path.read_text(
                encoding="utf-8", errors="ignore"
                ).lower()
            ):
                paths.add(path.relative_to(ROOT).as_posix())
    assert paths <= ALLOWED_REFERENCE_FILES


def test_legacy_named_compatibility_contracts_remain() -> None:
    assert CALLER_AGENT_ID_HEADER == "x-paperclip-agent-id"
    assert "paperclip_issue_id" in TradeJournal.__table__.columns
    assert "ix_trade_journals_paperclip_issue_id" in {
        index.name for index in TradeJournal.__table__.indexes
    }
```

- [ ] **Step 2: Run the new test and verify RED**

Run: `uv run pytest --no-cov -q tests/test_paperclip_residue_inventory.py`

Expected: FAIL because the settings, CIO loader/option, and dead tokens still exist.

- [ ] **Step 3: Implement the minimal cleanup**

Delete the two unused settings, the CIO Paperclip loader/CLI branch/imports/export, and the two matching lines from the local `.env.prod`. Preserve the header constant and shell header emission. Preserve the trade-journal column/index/arguments and update their nearby comments and current MCP documentation to say `external issue key (legacy Paperclip name; current Linear ROB key)`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest --no-cov -q tests/test_paperclip_residue_inventory.py tests/test_mcp_call_template.py tests/test_mcp_caller_identity_middleware.py tests/test_trade_journal_model.py tests/test_mcp_trade_journal.py tests/test_mcp_execution_tools.py tests/mcp_server/tooling/test_toss_live_ledger.py`

Expected: all selected tests pass; only pre-existing warnings may remain.

- [ ] **Step 5: Verify acceptance and quality gates**

Run: `git grep -in paperclip -- app scripts ':!docs/plans/**'`

Expected: every result is either the canonical legacy caller header/template or the retained trade-journal external-issue compatibility surface, with an explanatory comment.

Run: `make lint`

Expected: Ruff checks, Ruff formatting checks, and ty checks all exit 0.

- [ ] **Step 6: Commit**

```bash
git add app/core/config.py scripts/cio_quality_gate.py \
  app/mcp_server/caller_identity_middleware.py app/mcp_server/main.py \
  app/mcp_server/tooling/trade_journal_tools.py \
  app/mcp_server/tooling/trade_journal_registration.py \
  app/models/trade_journal.py app/schemas/trade_retrospective.py \
  app/mcp_server/README.md scripts/templates/mcp_call.sh.tmpl \
  tests/test_paperclip_residue_inventory.py
git commit -m "chore(ROB-865): remove dead Paperclip integrations"
```
