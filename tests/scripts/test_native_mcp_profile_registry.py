"""ROB-1258 fixed-profile MCP deploy-registry completeness guard."""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

from scripts.check_native_mcp_profile_registry import (
    LIFECYCLE_EXEMPT_LABELS,
    validate_registry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "deploy-native.sh"
SOURCE_PLISTS = REPO_ROOT / "ops" / "native" / "plists"


def _extract_array_values(body: str, name: str) -> list[str]:
    match = re.search(
        rf"^{re.escape(name)}=\(\n(.*?)\n\)", body, re.DOTALL | re.MULTILINE
    )
    assert match, f"{name}=(...) array not found"
    return re.findall(r'^\s*"([^"]+)"\s*$', match.group(1), re.MULTILINE)


def _write_plist(
    directory: Path,
    *,
    label: str,
    port: int | None,
    wrapper: Path | None = None,
) -> None:
    environment: dict[str, str] = {}
    if port is not None:
        environment["AUTO_TRADER_MCP_PORT"] = str(port)
    payload: dict[str, object] = {
        "Label": label,
        "ProgramArguments": [str(wrapper or Path("/bin/false"))],
        "EnvironmentVariables": environment,
    }
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{label}.plist").open("wb") as handle:
        plistlib.dump(payload, handle)


def test_repository_inventory_exactly_matches_both_deploy_registries() -> None:
    body = DEPLOY.read_text()
    result = validate_registry(
        source_plist_dir=SOURCE_PLISTS,
        single_active_labels=_extract_array_values(body, "SINGLE_ACTIVE_LABELS"),
        profile_port_entries=_extract_array_values(body, "MCP_PROFILE_PORTS"),
    )
    assert not result.errors, "\n".join(result.errors)


def test_new_fixed_profile_plist_fails_when_both_registries_omit_it(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    existing = "com.robinco.auto-trader.mcp-existing"
    added = "com.robinco.auto-trader.mcp-added-later"
    _write_plist(source, label=existing, port=9101)
    _write_plist(source, label=added, port=9102)

    result = validate_registry(
        source_plist_dir=source,
        single_active_labels=[existing],
        profile_port_entries=[f"{existing}:9101"],
    )

    assert (
        f"source fixed-profile missing from MCP_PROFILE_PORTS: {added}" in result.errors
    )
    assert (
        f"source fixed-profile missing from SINGLE_ACTIVE_LABELS: {added}"
        in result.errors
    )


def test_source_plist_port_mismatch_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    label = "com.robinco.auto-trader.mcp-profile"
    _write_plist(source, label=label, port=9101)

    result = validate_registry(
        source_plist_dir=source,
        single_active_labels=[label],
        profile_port_entries=[f"{label}:9102"],
    )

    assert any("source port mismatch" in error for error in result.errors)


def test_installed_out_of_band_profile_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    installed = tmp_path / "installed"
    known = "com.robinco.auto-trader.mcp-known"
    unknown = "com.robinco.auto-trader.mcp-manual"
    _write_plist(source, label=known, port=9101)
    _write_plist(installed, label=known, port=9101)
    _write_plist(installed, label=unknown, port=9102)

    result = validate_registry(
        source_plist_dir=source,
        installed_plist_dir=installed,
        single_active_labels=[known],
        profile_port_entries=[f"{known}:9101"],
    )

    assert f"installed fixed-profile has no source plist: {unknown}" in result.errors
    assert (
        f"installed fixed-profile missing from MCP_PROFILE_PORTS: {unknown}"
        in result.errors
    )


def test_installed_literal_wrapper_port_is_compared(tmp_path: Path) -> None:
    source = tmp_path / "source"
    installed = tmp_path / "installed"
    label = "com.robinco.auto-trader.mcp-paper-test"
    wrapper = tmp_path / "run-paper.sh"
    wrapper.write_text('#!/usr/bin/env bash\nexport MCP_PORT="9102"\n')
    _write_plist(source, label=label, port=9101)
    _write_plist(installed, label=label, port=None, wrapper=wrapper)

    result = validate_registry(
        source_plist_dir=source,
        installed_plist_dir=installed,
        single_active_labels=[label],
        profile_port_entries=[f"{label}:9101"],
    )

    assert any("installed port mismatch" in error for error in result.errors)


def test_blue_green_and_watchdog_plists_are_lifecycle_exempt(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for label in LIFECYCLE_EXEMPT_LABELS:
        _write_plist(source, label=label, port=None)

    result = validate_registry(
        source_plist_dir=source,
        single_active_labels=[],
        profile_port_entries=[],
    )

    assert not result.errors
    assert not result.source


def test_registry_preflight_runs_before_dependency_install_and_migrations() -> None:
    lines = DEPLOY.read_text().splitlines()
    call_index = lines.index("verify_mcp_profile_registry")
    install_index = next(
        i for i, line in enumerate(lines) if "uv sync --frozen" in line
    )
    migration_index = next(
        i for i, line in enumerate(lines) if "uv run alembic upgrade head" in line
    )
    sync_index = lines.index("sync_release_ops_to_base")

    assert call_index < install_index < migration_index < sync_index
