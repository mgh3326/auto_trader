"""ROB-207 diagnose CLI structured output tests."""
from __future__ import annotations

import json
import pytest
import subprocess
import sys
from uuid import uuid4


@pytest.mark.integration
def test_diagnose_cli_prints_json_with_empty_source(db_session):
    src = f"empty_{uuid4()}"
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.diagnose_research_reports",
         "--source", src, "--max-age-hours", "24"],
        check=False, capture_output=True, text=True,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["source"] == src
    assert out["is_ready"] is False
    assert "research_reports_unavailable" in out["warnings"]
