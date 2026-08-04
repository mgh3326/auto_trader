from __future__ import annotations

from app.services.kis_lean_execution import run_once


def test_once_is_fixed_no_order_and_observes_all_six_stages() -> None:
    events: list[dict[str, object]] = []

    result = run_once(
        {"symbol": "005930", "price": 70000},
        correlation_id="test-run",
        emit=events.append,
    )

    assert result.status == "shadow_complete"
    assert result.decision == "NO_ORDER"
    assert [event["stage"] for event in events] == [
        "decision",
        "order_intent",
        "kis_pre_submit",
        "fill",
        "position_reconcile",
        "discord",
    ]
    assert events[1]["output"] == {
        "kind": "no_order",
        "side": None,
        "quantity": None,
        "decision": "NO_ORDER",
    }
    assert events[2]["output"]["accepted_for_submission"] is False
    assert events[2]["output"]["blocked_reason"] == "account_ownership_unconfirmed"
    assert events[2]["output"]["concurrent_writer_action"] == "refuse_and_report"
    assert events[-1]["output"]["must_notify"] is True


def test_custom_strategy_is_a_calculation_seam_but_pre_submit_remains_shadow() -> None:
    class CandidateAdapter:
        def evaluate(self, snapshot: dict[str, object]) -> dict[str, object]:
            return {"decision": "NO_ORDER", "signal_emitted": False, "candidate": True}

    events: list[dict[str, object]] = []
    result = run_once(
        {"symbol": "005930"},
        correlation_id="adapter-test",
        emit=events.append,
        strategy=CandidateAdapter(),  # type: ignore[arg-type]
    )

    assert result.status == "shadow_complete"
    assert events[0]["output"]["candidate"] is True
    assert events[2]["output"]["accepted_for_submission"] is False


def test_strategy_failure_emits_discord_failure_event() -> None:
    class BrokenAdapter:
        def evaluate(self, snapshot: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("synthetic adapter failure")

    events: list[dict[str, object]] = []
    result = run_once(
        {"symbol": "005930"},
        correlation_id="failure-test",
        emit=events.append,
        strategy=BrokenAdapter(),  # type: ignore[arg-type]
    )

    assert result.status == "failed"
    assert events[-1]["stage"] == "discord"
    assert events[-1]["status"] == "failed"
    assert events[-1]["output"]["must_notify"] is True
