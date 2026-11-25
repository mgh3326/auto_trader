"""hk 6573: static checks of the Benzinga Pro capture Dagu copy (not registered)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

DAG = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "dagu"
    / "at-benzinga-pro"
    / "at-benzinga-pro-capture.yaml"
)


def test_runs_on_mbp_with_capture_script_and_no_inline_secret() -> None:
    text = DAG.read_text()
    doc = yaml.safe_load(text)
    assert doc["worker_selector"] == {"host": "mbp"}
    assert doc["max_active_runs"] == 1
    assert "저장소가 등록하지 않는다" in text.splitlines()[0]
    (step,) = doc["steps"]
    assert "-m scripts.benzinga_pro_capture" in step["script"]
    assert step["timeout_sec"] < 1800
    for word in ("apikey", "TOKEN=", "Bearer"):
        assert word not in step["script"]
    assert all(s.startswith("CRON_TZ=America/New_York ") for s in doc["schedule"])
