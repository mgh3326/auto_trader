"""W5 — nothing in this repository turns the durable path on.

Adversarial review R24. Three gates default false in ``Settings``, but a
deployment surface can set an environment variable to ``true`` without any
Python default changing. So the negative is asserted where it actually lives:
across the tracked compose files, ops launchers and env templates.

The mutation test below is the point -- a guard that has never been shown a
file with the gate enabled is a guard that might be looking in the wrong
place.
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

_REPO = pathlib.Path(__file__).resolve().parents[4]

#: Everything a deployment could read a gate from.
_DEPLOYMENT_GLOBS = (
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "env.example",
    "Makefile",
    "ops/**/*.yml",
    "ops/**/*.yaml",
    "ops/**/*.sh",
    "ops/**/*.env",
    "ops/**/*.service",
    "scripts/*.sh",
    ".github/workflows/*.yml",
)

W5_GATES = (
    "ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED",
    "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
    "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
)

#: `GATE=true`, `GATE: true`, `GATE="true"`, `- GATE=1`, and so on.
_ENABLED = re.compile(
    r"(?P<gate>ORDER_PROPOSALS_TELEGRAM_CALLBACK_[A-Z_]*ENABLED)"
    r"\s*[:=]\s*[\"']?(?P<value>true|1|yes|on)\b",
    re.IGNORECASE,
)


def _deployment_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for pattern in _DEPLOYMENT_GLOBS:
        files.extend(
            path
            for path in _REPO.glob(pattern)
            if path.is_file() and "__pycache__" not in path.parts
        )
    return sorted(set(files))


def find_enabled_gates(text: str) -> list[str]:
    """Every W5 gate this text switches on."""
    return [match.group("gate") for match in _ENABLED.finditer(text)]


def test_the_guard_has_something_to_look_at() -> None:
    """Anti-vacuity: an empty file list would make the negative meaningless."""
    files = _deployment_files()
    assert len(files) >= 5, [str(path) for path in files]
    names = {path.name for path in files}
    assert "env.example" in names
    assert any(name.startswith("docker-compose") for name in names)


def test_no_deployment_surface_enables_a_w5_gate() -> None:
    """R24 — the actual no-auto-activation assertion."""
    offenders: list[str] = []
    for path in _deployment_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - binary in ops/
            continue
        for gate in find_enabled_gates(text):
            offenders.append(f"{path.relative_to(_REPO)}: {gate}")
    assert not offenders, offenders


def test_env_example_ships_every_gate_explicitly_false() -> None:
    """Present and false beats absent: an operator can see what exists."""
    text = (_REPO / "env.example").read_text(encoding="utf-8")
    for gate in W5_GATES:
        assert re.search(rf"^{gate}=false$", text, re.MULTILINE), gate


@pytest.mark.parametrize("gate", W5_GATES)
@pytest.mark.parametrize(
    "line",
    [
        "{gate}=true",
        "{gate}: true",
        '{gate}="true"',
        "      - {gate}=1",
        "{gate}=on",
    ],
)
def test_the_guard_would_catch_an_enabled_gate(gate: str, line: str) -> None:
    """Mutation: put the gate in a deployment file and the guard must fire.

    Asserted against the matcher directly rather than by writing into a
    tracked file, so the test cannot leave a deployment surface armed if it
    fails part-way.
    """
    mutated = f"services:\n  api:\n    environment:\n      {line.format(gate=gate)}\n"
    assert find_enabled_gates(mutated) == [gate], mutated


@pytest.mark.parametrize("gate", W5_GATES)
def test_the_guard_does_not_fire_on_a_disabled_gate(gate: str) -> None:
    for line in (f"{gate}=false", f"{gate}: false", f'{gate}=""', f"# {gate}=true"):
        # A commented-out enable is still an enable as far as this guard is
        # concerned -- deliberately, because uncommenting is one keystroke.
        expected = [gate] if line.startswith("#") else []
        assert find_enabled_gates(line) == expected, line
