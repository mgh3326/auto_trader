"""CI guard tests confirming Alpaca package is not imported by routers, MCP, or profiles (ROB-57)."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ALPACA_IMPORT_PATTERNS = (
    "app.services.brokers.alpaca",
    "from app.services.brokers.alpaca",
    "import app.services.brokers.alpaca",
)
ALPACA_PKG_PATH = REPO_ROOT / "app" / "services" / "brokers" / "alpaca"


def _source_imports_alpaca(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return any(pattern in text for pattern in ALPACA_IMPORT_PATTERNS)


def _collect_python_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return list(directory.rglob("*.py"))


@pytest.mark.unit
def test_no_router_imports_alpaca_paper():
    """No file under app/routers/ imports app.services.brokers.alpaca."""
    routers_dir = REPO_ROOT / "app" / "routers"
    offenders = [
        p for p in _collect_python_files(routers_dir) if _source_imports_alpaca(p)
    ]
    assert not offenders, (
        f"These router files import the Alpaca package (not allowed in this issue): "
        f"{[str(p) for p in offenders]}"
    )


@pytest.mark.unit
def test_only_explicit_readonly_mcp_tool_imports_alpaca_paper():
    """Only the ROB-69 read-only MCP tooling module may import Alpaca paper service."""
    mcp_dir = REPO_ROOT / "app" / "mcp_server"
    allowed = {mcp_dir / "tooling" / "alpaca_paper.py"}
    offenders = [
        p
        for p in _collect_python_files(mcp_dir)
        if _source_imports_alpaca(p) and p not in allowed
    ]
    assert not offenders, (
        f"Only app/mcp_server/tooling/alpaca_paper.py may import Alpaca package: "
        f"{[str(p) for p in offenders]}"
    )


@pytest.mark.unit
def test_no_hermes_profile_imports_alpaca_paper():
    """No trade-profile or orchestrator registration file imports app.services.brokers.alpaca."""
    # Hermes profiles manifest as trade_profile_registration.py and related modules.
    # Check all registration/strategies/profile files in MCP tooling.
    tooling_dir = REPO_ROOT / "app" / "mcp_server" / "tooling"
    profile_files = [
        p
        for p in _collect_python_files(tooling_dir)
        if any(
            keyword in p.name
            for keyword in ("registration", "profile", "strategies", "registry")
        )
    ]
    offenders = [p for p in profile_files if _source_imports_alpaca(p)]
    assert not offenders, (
        f"These profile/registration files import the Alpaca package: "
        f"{[str(p) for p in offenders]}"
    )


@pytest.mark.unit
def test_no_alpaca_live_settings_field():
    """Settings model must have no field name starting with 'alpaca_live_' (invariant I4)."""
    from app.core.config import Settings

    live_fields = [
        name for name in Settings.model_fields if name.startswith("alpaca_live_")
    ]
    assert not live_fields, (
        f"Settings has unexpected alpaca_live_* field(s): {live_fields}. "
        "Live endpoint support is explicitly out of scope for ROB-57."
    )
