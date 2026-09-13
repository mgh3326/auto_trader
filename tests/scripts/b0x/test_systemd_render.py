from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.b0x.systemd_render import (
    B0XUnitRenderError,
    B0XUnitRenderInputs,
    render_b0x_service_template,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "ops/ncp/systemd/job-kickoff-b0x-nudge-kr.service.in"


def _inputs(tmp_path: Path) -> B0XUnitRenderInputs:
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "fixture", str(worktree)], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.name", "Fixture"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "config",
            "user.email",
            "fixture@example.invalid",
        ],
        check=True,
    )
    (worktree / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-m", "fixture"],
        check=True,
    )
    env_file = tmp_path / "runtime.env"
    env_file.touch(mode=0o600)
    python = tmp_path / "python"
    python.write_text("fixture executable; never invoked\n", encoding="utf-8")
    python.chmod(0o700)
    return B0XUnitRenderInputs(
        auto_trader_worktree=worktree,
        env_file=env_file,
        python_executable=python,
        service_user="fixture-service",
        timeout_start_seconds=60,
    )


def test_public_template_renders_default_off_into_private_stage(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    receipt = render_b0x_service_template(TEMPLATE, staging_root=stage, inputs=inputs)

    rendered = Path(receipt["rendered_path"])
    contents = rendered.read_text(encoding="utf-8")
    assert receipt["installed"] == "false"
    assert receipt["enabled"] == "false"
    assert len(receipt["sha256"]) == 64
    assert len(receipt["auto_trader_head"]) == 40
    assert "@B0X_" not in contents and "@AUTO_TRADER_" not in contents
    assert "LANE_EVENT_KICKOFF_ENABLED=false" in contents
    assert "LANE_EVENT_KICKOFF_B0X_ENABLED=false" in contents
    assert "B0X_UNIT_TEMPLATE_RENDERED=false" in contents
    assert "--dry-run" in contents
    assert str(inputs.env_file) in contents
    assert str(inputs.python_executable) in contents


@pytest.mark.parametrize(
    "field,replacement",
    (
        ("auto_trader_worktree", Path("relative-checkout")),
        ("env_file", Path("relative.env")),
        ("python_executable", Path("relative-python")),
        ("service_user", "bad user"),
        ("timeout_start_seconds", 0),
    ),
)
def test_missing_relative_or_invalid_render_input_fails_before_write(
    tmp_path: Path, field: str, replacement: object
) -> None:
    inputs = replace(_inputs(tmp_path), **{field: replacement})
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(B0XUnitRenderError):
        render_b0x_service_template(TEMPLATE, staging_root=stage, inputs=inputs)
    assert list(stage.iterdir()) == []


def test_symlink_input_and_existing_output_fail_closed(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    linked_env = tmp_path / "linked.env"
    os.symlink(inputs.env_file, linked_env)
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(B0XUnitRenderError, match="symlink"):
        render_b0x_service_template(
            TEMPLATE,
            staging_root=stage,
            inputs=replace(inputs, env_file=linked_env),
        )

    first = render_b0x_service_template(TEMPLATE, staging_root=stage, inputs=inputs)
    with pytest.raises(B0XUnitRenderError, match="already exists"):
        render_b0x_service_template(TEMPLATE, staging_root=stage, inputs=inputs)
    assert Path(first["rendered_path"]).is_file()


@pytest.mark.parametrize(
    "old,new",
    (
        (
            "Environment=LANE_EVENT_KICKOFF_B0X_ENABLED=false",
            "Environment=LANE_EVENT_KICKOFF_B0X_ENABLED=true",
        ),
        (" --dry-run", ""),
    ),
)
def test_template_validator_rejects_leaked_layout_or_missing_guard(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    inputs = _inputs(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    mutated = tmp_path / "mutant.service.in"
    mutated.write_text(
        TEMPLATE.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )
    with pytest.raises(B0XUnitRenderError, match="default-off"):
        render_b0x_service_template(mutated, staging_root=stage, inputs=inputs)
    assert list(stage.iterdir()) == []


def test_fake_git_marker_and_unsafe_unit_value_fail_before_render(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    fake = tmp_path / "fake-checkout"
    fake.mkdir()
    (fake / ".git").mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(B0XUnitRenderError, match="usable Git worktree"):
        render_b0x_service_template(
            TEMPLATE,
            staging_root=stage,
            inputs=replace(inputs, auto_trader_worktree=fake),
        )

    unsafe = tmp_path / "unsafe path"
    unsafe.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "fixture", str(unsafe)], check=True)
    subprocess.run(
        ["git", "-C", str(unsafe), "config", "user.name", "Fixture"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(unsafe),
            "config",
            "user.email",
            "fixture@example.invalid",
        ],
        check=True,
    )
    (unsafe / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(unsafe), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(unsafe), "commit", "-q", "-m", "fixture"],
        check=True,
    )
    with pytest.raises(B0XUnitRenderError, match="unsafe systemd"):
        render_b0x_service_template(
            TEMPLATE,
            staging_root=stage,
            inputs=replace(inputs, auto_trader_worktree=unsafe),
        )
    assert list(stage.iterdir()) == []
