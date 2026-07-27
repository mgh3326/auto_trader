"""Operator CLI safety and audit-artifact tests."""

from __future__ import annotations

import json
import stat
from datetime import datetime

import pytest

from app.services.symbol_news_store import NewsRelevanceJudgmentSnapshot
from scripts import recover_news_relevance_contamination as recovery_script


def _snapshot() -> NewsRelevanceJudgmentSnapshot:
    return NewsRelevanceJudgmentSnapshot(
        id=10,
        article_id=20,
        market="kr",
        symbol="005930",
        status="excluded",
        relationship="unrelated",
        relevance="low",
        price_relevance="none",
        score=0.1,
        reason="복구 가능한 원문 판정",
        judged_by="hermes-news-relevance",
        judged_at=datetime(2026, 7, 26, 23, 7),
    )


@pytest.mark.unit
def test_cli_defaults_to_dry_run_and_execute_is_explicit() -> None:
    dry_run = recovery_script.parse_args([])
    execute = recovery_script.parse_args(["--execute"])

    assert dry_run.dry_run is True
    assert dry_run.execute is False
    assert execute.dry_run is False
    assert execute.execute is True


@pytest.mark.unit
def test_audit_artifact_preserves_judgment_and_is_exclusive_mode_0600(
    tmp_path,
) -> None:
    path = tmp_path / "audit.json"
    payload = recovery_script._audit_payload(
        rows=(_snapshot(),),
        execute_requested=False,
    )

    recovery_script._write_audit_artifact(path, payload)

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["expected_count"] == 96
    assert stored["selected_count"] == 1
    assert stored["rows"][0] == {
        "id": 10,
        "article_id": 20,
        "market": "kr",
        "symbol": "005930",
        "status": "excluded",
        "relationship": "unrelated",
        "relevance": "low",
        "price_relevance": "none",
        "score": 0.1,
        "reason": "복구 가능한 원문 판정",
        "judged_by": "hermes-news-relevance",
        "judged_at": "2026-07-26T23:07:00",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        recovery_script._write_audit_artifact(path, payload)
