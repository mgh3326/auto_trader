from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PINNED_DIGEST = "ba383d20d8aa8fb134ca475b1439329e97ac400f91ea957db0484deaa7df8854"


@pytest.mark.integration
def test_built_wheel_ships_small_research_contract_and_clean_imports(
    tmp_path: Path,
) -> None:
    output = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(output.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "research_contracts/canonical_hash.py" in names
    assert "research_contracts/evaluation_windows.py" in names
    assert "research_contracts/frozen_config.py" in names
    assert "research_contracts/honest_offline_gate.py" in names
    assert "research_contracts/jsonb_numbers.py" in names
    assert "research_contracts/trial_evidence.py" in names
    assert not any(name.startswith("research/nautilus_scalping/") for name in names)

    script = f"""
import sys
sys.path.insert(0, sys.argv[1])
from app.schemas.research_backtest import StrategyExperimentIdentity
from app.services import research_offline_gate_service
from app.services.research_canonical_hash import canonical_sha256
assert StrategyExperimentIdentity
assert research_offline_gate_service.finalize_offline_gate
assert canonical_sha256({{'b': 2, 'a': 1}}) == {PINNED_DIGEST!r}
"""
    subprocess.run(
        [sys.executable, "-I", "-c", script, str(wheel)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
