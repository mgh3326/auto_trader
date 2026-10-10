"""#1189: every MCP server names its MCP_PROFILE; a blank one refuses to start.

The entrypoint (app/mcp_server/main.py) resolves its profile through
``require_mcp_profile``: a missing, empty or whitespace-only value raises
instead of falling back to the full DEFAULT surface (tests/test_mcp_server_main.py
drives the real guard through the entrypoint). This file pins the guard itself
and the inventory of every in-repo server definition, each of which must set
an explicit profile so the refusal breaks none of them.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from app.mcp_server.profiles import (
    McpProfile,
    McpProfileRequiredError,
    require_mcp_profile,
    resolve_mcp_profile,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "scripts" / "deploy-ncp-pull.sh"

BLANK_VALUES = [None, "", " ", "   ", "\t", "\n", " \t\r\n "]


# --- the guard ---------------------------------------------------------------


@pytest.mark.parametrize("value", BLANK_VALUES)
def test_blank_missing_or_whitespace_profile_is_refused(value: str | None) -> None:
    with pytest.raises(McpProfileRequiredError, match="MCP_PROFILE is required"):
        require_mcp_profile(value)


def test_refusal_is_a_value_error_naming_the_explicit_default() -> None:
    with pytest.raises(ValueError, match="MCP_PROFILE=default"):
        require_mcp_profile("")


@pytest.mark.parametrize("profile", list(McpProfile))
def test_every_explicit_profile_resolves(profile: McpProfile) -> None:
    assert require_mcp_profile(profile.value) is profile
    assert require_mcp_profile(f"  {profile.value}\n") is profile


def test_unknown_profile_is_still_refused() -> None:
    with pytest.raises(ValueError, match="Unknown MCP_PROFILE"):
        require_mcp_profile("bogus")


def test_lenient_resolver_is_unchanged_for_non_server_callers() -> None:
    # trade_retrospective_tools labels its actor through resolve_mcp_profile
    # inside an already-started server; its documented contract stays.
    assert resolve_mcp_profile(None) is McpProfile.DEFAULT
    assert resolve_mcp_profile("  ") is McpProfile.DEFAULT


# --- inventory of in-repo server definitions ---------------------------------

# Every tracked file outside tests/docs that launches app.mcp_server.main.
# A new launcher must be added here with its explicit-profile check.
REVIEWED_LAUNCHERS = {
    "docker-compose.prod.yml",
    "scripts/deploy-ncp-pull.sh",
    "scripts/mcp_server.sh",
    "scripts/mock_session_mcp.py",
    "scripts/shadow_replay_mcp.json",
}
# Mentions the module name in a docstring only; it launches nothing.
NON_LAUNCHERS = {"app/mcp_server/tool_call_log_middleware.py"}


def _tracked_files_mentioning_entrypoint() -> set[str]:
    listed = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    found: set[str] = set()
    for rel in listed:
        if rel.startswith(("tests/", "docs/", "blog/")) or rel.endswith(".md"):
            continue
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "app.mcp_server.main" in text:
            found.add(rel)
    return found


def test_launcher_inventory_is_complete() -> None:
    assert _tracked_files_mentioning_entrypoint() == REVIEWED_LAUNCHERS | NON_LAUNCHERS


def test_compose_mcp_services_each_set_an_explicit_profile() -> None:
    compose = yaml.safe_load((REPO / "docker-compose.prod.yml").read_text())
    launched: dict[str, str] = {}
    for name, service in compose["services"].items():
        if "app.mcp_server.main" not in json.dumps(service.get("command", "")):
            continue
        value = str((service.get("environment") or {}).get("MCP_PROFILE", ""))
        launched[name] = value
    assert launched == {
        "mcp": "default",
        "mcp-analysis-readonly": "analysis_readonly",
        "mcp-account-read": "account_read",
        "mcp-tradingcodex-execution": "tradingcodex_execution",
    }
    for value in launched.values():
        assert "$" not in value  # a literal, never an interpolated fallback
        require_mcp_profile(value)


def test_deploy_script_passes_an_explicit_profile_to_every_unit() -> None:
    source = DEPLOY.read_text()
    calls = re.findall(r"\brun_mcp (?!\(\))[^;|>]*", source)
    assert len(calls) == 4  # deploy color, deploy unit, restore color, restore unit
    for call in calls:
        assert (" default MCP_AUTH_TOKEN " in call) != (
            ' "${MCP_PROFILES[$i]}" "${MCP_TOKENS[$i]}" ' in call
        ), call
    profiles = re.search(r"^declare -a MCP_PROFILES=\((.*)\)$", source, re.M)
    names = re.search(r"^declare -a MCP_NAMES=\((.*)\)$", source, re.M)
    assert profiles and names
    assert len(profiles.group(1).split()) == len(names.group(1).split())
    for value in profiles.group(1).split():
        assert value.strip("'\"").strip(), "blank profile in MCP_PROFILES"
        require_mcp_profile(value)


def test_shadow_replay_stdio_config_sets_its_profile() -> None:
    config = json.loads((REPO / "scripts" / "shadow_replay_mcp.json").read_text())
    (server,) = config["mcpServers"].values()
    assert server["env"]["MCP_PROFILE"] == "shadow-replay"


def test_mock_session_launcher_sets_the_approved_profile() -> None:
    source = (REPO / "scripts" / "mock_session_mcp.py").read_text()
    assert 'child_env["MCP_PROFILE"] = approved_profile' in source


def test_wrapper_supplies_no_profile_fallback() -> None:
    # The wrapper passes the caller's MCP_PROFILE through untouched; it must
    # not reintroduce a blank-to-default fallback of its own.
    lines = (REPO / "scripts" / "mcp_server.sh").read_text().splitlines()
    code = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    assert "MCP_PROFILE" not in code
