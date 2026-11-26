"""hk 6395: static checks of the news-shadow Dagu copy (not registered)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2] / "deploy" / "dagu" / "at-news-shadow"


def test_dag_schedule_entrypoint_and_pattern() -> None:
    doc = yaml.safe_load((ROOT / "at-news-shadow.yaml").read_text())
    assert doc["schedule"] == ["CRON_TZ=Asia/Seoul */3 7-23 * * *"]
    assert doc["max_active_runs"] == 1
    assert doc["worker_selector"] == {"host": "pi"}
    # The gate must reach the container: DAG-level `env:` only sets the host
    # step shell, so the flag goes on `docker run -e` instead.
    assert "env" not in doc
    (step,) = doc["steps"]
    script = step["script"]
    assert "-m scripts.benzinga_news_shadow poll" in script
    assert "--symbols NVDA,AMD,QQQ,SMH,DELL,URBN" in script
    assert "--saveticker --saveticker-pages 3" in script
    assert "-e BENZINGA_NEWS_SHADOW_ENABLED=true" in script
    # `--rm` discards container paths: output must land on a host mount.
    assert "-v /root/at-run/benzinga-shadow:/out" in script
    assert "--out-dir /out" in script
    assert '"$(cat /root/at-run/deployed-digest)"' in script
    assert "--env-file /root/at-secrets/.env.api" in script
    # The Benzinga key lives in its own job env file (decision at-benzinga-trial-front-use).
    assert "--env-file /root/at-secrets/.env.benzinga" in script
    assert "--rm --init" in script
    assert "BENZINGA_API_KEY" not in script  # key comes from the job env file
    assert step["timeout_sec"] < 1800  # shorter than the 30 minute cadence
    text = (ROOT / "at-news-shadow.yaml").read_text()
    assert "import" not in text and "psql" not in text  # no DB write


def test_readme_says_copy_not_registered() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "등록되지 않는 복사본" in readme
    assert (
        "저장소는 Dagu 에 복사하거나 등록하지 않는다" in readme
        or "등록하지 않는다" in readme
    )
    assert "at-news-shadow" in readme
    assert "-v /root/at-run/benzinga-shadow:/out" in readme
    assert {p.name for p in ROOT.glob("*.yaml")} == {"at-news-shadow.yaml"}


def test_dag_header_says_not_registered() -> None:
    assert (
        "저장소가 등록하지 않는다"
        in (ROOT / "at-news-shadow.yaml").read_text().splitlines()[0]
    )
