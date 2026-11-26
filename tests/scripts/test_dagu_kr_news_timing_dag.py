from __future__ import annotations

from pathlib import Path


def test_kr_news_timing_dag_uses_database_environment_variable() -> None:
    dag = Path("deploy/dagu/at-kr-news-timing/at-kr-news-timing.yaml").read_text()

    assert "--env DATABASE_URL" in dag
    assert "--env-file" not in dag
