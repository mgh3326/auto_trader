"""B-HOST uses the DEFAULT blue/green pair and a one-shot host witness only."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.scripts.test_deploy_ncp_pull_rollback import OLD, _run

MOUNT = "/etc/machine-id:/etc/machine-id:ro"
DEFAULT_COLORS = {"at-mcp-blue", "at-mcp-green"}


def _runs(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call and call[0] == "run" and "--name" in call]


def _name(call: list[str]) -> str:
    return call[call.index("--name") + 1]


@pytest.mark.parametrize("args", [(), ("--rollback",)])
@pytest.mark.parametrize("active_color", ["blue", "green"])
def test_only_default_mcp_color_gets_machine_id_on_promotion(
    tmp_path: Path, args: tuple[str, ...], active_color: str
) -> None:
    result, calls, _, _ = _run(tmp_path, args=args, active_mcp_color=active_color)
    assert result.returncode == 0, result.stderr
    runs = _runs(calls)
    mounted = {_name(call) for call in runs if MOUNT in call}
    assert mounted <= DEFAULT_COLORS
    assert len(mounted) == 1
    color = next(iter(mounted))
    call = next(call for call in runs if _name(call) == color)
    assert call.count(MOUNT) == 1
    assert "--pid=host" not in call


@pytest.mark.parametrize("active_color", ["blue", "green"])
def test_switch_and_failure_rollback_keeps_mount(
    tmp_path: Path, active_color: str
) -> None:
    result, calls, _, _ = _run(
        tmp_path,
        fail_name="at-mcp-account-read",
        active_mcp_color=active_color,
        both_mcp_colors_present=True,
    )
    assert result.returncode != 0
    runs = _runs(calls)
    mounted = {_name(call) for call in runs if MOUNT in call}
    target = f"at-mcp-{'green' if active_color == 'blue' else 'blue'}"
    assert mounted == {target}
    assert any(_name(call) == target and OLD in call for call in runs)
    assert sum(_name(call) == target and MOUNT in call for call in runs) == 2
    assert all(MOUNT not in call for call in runs if _name(call) not in DEFAULT_COLORS)


def test_host_witness_command_is_ephemeral_host_pid_only() -> None:
    wrapper = (
        Path(__file__).resolve().parents[2] / "scripts/nhplug-t14-host-witness.sh"
    ).read_text()
    assert "docker run --rm --pid=host --network none" in wrapper
    assert "-v /etc/machine-id:/etc/machine-id:ro" in wrapper
    assert "-p " not in wrapper and "--publish" not in wrapper
    assert "python -m scripts.nhplug_t14_host_witness" in wrapper
