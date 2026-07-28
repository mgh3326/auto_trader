import json
from pathlib import Path

from app.services.posture_generator import PostureGeneratorInput
from app.services.posture_shadow_service import (
    append_shadow_jsonl,
    run_posture_shadow,
)
from app.services.trading_policy_service import (
    get_policy_for,
    load_trading_policy,
    policy_content_hash,
)
from scripts.run_posture_shadow import main

_REPLAY = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "posture"
    / "rob1106_20260727_replay.json"
)


def _replay() -> PostureGeneratorInput:
    return PostureGeneratorInput.model_validate_json(
        _REPLAY.read_text(encoding="utf-8")
    )


def test_shipped_policy_is_default_off_and_projection_shape_is_unchanged():
    policy = load_trading_policy()
    assert policy.posture.enabled is False
    assert policy.posture.mode == "shadow"

    existing_view = get_policy_for("kr", "sell")
    assert set(existing_view) == {
        "market",
        "lane",
        "version",
        "content_hash",
        "thresholds",
        "decision_rules",
        "market_rules",
        "crash_day",
        "user_stances",
    }
    assert "posture" not in existing_view


def test_default_off_returns_before_snapshot_load_and_record():
    policy = load_trading_policy()
    calls = {"load": 0, "record": 0}

    def loader() -> PostureGeneratorInput:
        calls["load"] += 1
        raise AssertionError("default-off must not load holdings or quotes")

    def recorder(_result) -> None:
        calls["record"] += 1
        raise AssertionError("default-off must not record an artifact")

    outcome = run_posture_shadow(
        posture_policy=policy.posture,
        policy_version=policy.version,
        policy_content_hash=policy_content_hash(),
        snapshot_loader=loader,
        recorder=recorder,
    )

    assert outcome.status == "disabled"
    assert outcome.reason == "posture.enabled=false"
    assert outcome.result is None
    assert calls == {"load": 0, "record": 0}


def test_default_off_cli_does_not_read_input_or_create_output(tmp_path: Path, capsys):
    missing_input = tmp_path / "does-not-exist.json"
    output = tmp_path / "must-not-exist.jsonl"

    assert main(["--input", str(missing_input), "--output", str(output)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "disabled"
    assert payload["reason"] == "posture.enabled=false"
    assert not output.exists()


def test_enabled_shadow_records_all_five_counts_and_unmapped_rows(tmp_path: Path):
    policy = load_trading_policy()
    enabled = policy.posture.model_copy(update={"enabled": True})
    snapshot = _replay()
    snapshot.policy_contexts = [
        row for row in snapshot.policy_contexts if row.holding_id != "toss-036460"
    ]
    output = tmp_path / "posture-coverage.jsonl"

    outcome = run_posture_shadow(
        posture_policy=enabled,
        policy_version=policy.version,
        policy_content_hash=policy_content_hash(),
        snapshot_loader=lambda: snapshot,
        recorder=lambda result: append_shadow_jsonl(output, result),
    )

    assert outcome.status == "recorded"
    assert outcome.result is not None
    recorded = json.loads(output.read_text(encoding="utf-8"))
    assert set(recorded["coverage"]["state_counts"]) == {
        "RESTING",
        "CONDITIONAL",
        "ARMED_DEFERRED",
        "DISARMED",
        "EXPIRED_REARMABLE",
    }
    assert recorded["coverage"]["unmapped_holding_count"] == 1
    assert recorded["unmapped_holdings"][0]["holding_id"] == "toss-036460"
    assert recorded["safety"] == {
        "orders_created": 0,
        "proposals_created": 0,
        "broker_mutations": 0,
    }
