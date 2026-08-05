from __future__ import annotations

from app.services.kis_mock_runner.correlation import kis_mock_runner_correlation_id


def test_correlation_id_is_deterministic_and_namespaced() -> None:
    values = {
        "tag": "foreground",
        "candidate_id": "kr-candidate-v1",
        "contract_hash": "abc123",
        "strategy_id": "survivor-v1",
        "decision_key": "2026-08-05:005930:buy",
    }
    correlation_id = kis_mock_runner_correlation_id(**values)
    assert correlation_id == kis_mock_runner_correlation_id(**values)
    assert correlation_id.startswith("kis-mock-runner:foreground:")
    assert len(correlation_id.rsplit(":", maxsplit=1)[-1]) == 16
