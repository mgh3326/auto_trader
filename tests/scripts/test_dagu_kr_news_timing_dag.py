from __future__ import annotations

from pathlib import Path


def test_kr_news_timing_dag_loads_api_settings_file() -> None:
    dag = Path("deploy/dagu/at-kr-news-timing/at-kr-news-timing.yaml").read_text()

    assert "--env-file /root/at-secrets/.env.api" in dag
    assert "--env DATABASE_URL" not in dag
