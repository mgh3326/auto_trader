"""The CLI refuses an artifact root inside the repository (no network, no I/O)."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.crypto_trend.run import refuse_in_repository


@pytest.mark.unit
@pytest.mark.parametrize("root", [Path("."), Path("research/crypto_trend/data")])
def test_refuses_roots_inside_the_checkout(root: Path) -> None:
    checkout = Path(__file__).resolve().parents[3]
    candidate = checkout / root
    with pytest.raises(ValueError, match="outside the repository"):
        refuse_in_repository(candidate)


@pytest.mark.unit
def test_accepts_an_artifact_root_outside_the_checkout(tmp_path: Path) -> None:
    refuse_in_repository(tmp_path / "rob-3796")  # must not raise
