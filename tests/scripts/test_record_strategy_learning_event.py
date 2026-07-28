from __future__ import annotations

import json

import pytest

from scripts.record_strategy_learning_event import main

pytestmark = pytest.mark.unit


def _valid_payload(**overrides) -> dict:
    payload = {
        "experiment_id": None,
        "stage": "paper",
        "verdict": "iterate",
        "failure_class": "operational",
        "reason_codes": ["non_canonical_contract_drift"],
        "evidence_refs": ["artifact:" + "a1" * 32],
        "failure_fingerprint": {
            "market": "us_equity",
            "horizon": "directional-lab-us",
        },
        "learning_payload": {
            "tested_claim": "test claim",
            "observed": "test observation",
            "falsified_claims": [],
            "preserved_claims": [],
            "next_question": "test question",
            "allowed_change_axis": "operator_contract_enforcement",
            "prohibited_changes": [],
            "stop_rule": "test stop rule",
            "schema_version": "1",
        },
        "idempotency_key": "test-key-001",
        "actor_id": "test-operator",
        "actor_role": "operator",
    }
    payload.update(overrides)
    return payload


def test_dry_run_prints_canonical_preview_without_commit(capsys) -> None:
    payload = _valid_payload()
    rc = main(["--payload-json", json.dumps(payload)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert len(out["request_hash"]) == 64
    assert len(out["memory_event_id"]) == 64
    assert out["request"]["failure_class"] == "operational"


def test_dry_run_is_default_and_deterministic(capsys) -> None:
    payload = _valid_payload()
    main(["--payload-json", json.dumps(payload)])
    first = json.loads(capsys.readouterr().out)
    main(["--payload-json", json.dumps(payload)])
    second = json.loads(capsys.readouterr().out)
    assert first["memory_event_id"] == second["memory_event_id"]
    assert first["request_hash"] == second["request_hash"]


def test_invalid_failure_class_is_rejected() -> None:
    payload = _valid_payload(failure_class="NON_CANONICAL_CONTRACT_DRIFT")
    with pytest.raises(SystemExit, match="payload rejected by typed contract"):
        main(["--payload-json", json.dumps(payload)])


def test_invalid_stage_is_rejected() -> None:
    payload = _valid_payload(stage="not_a_real_stage")
    with pytest.raises(SystemExit, match="payload rejected by typed contract"):
        main(["--payload-json", json.dumps(payload)])


def test_invalid_verdict_is_rejected() -> None:
    payload = _valid_payload(verdict="not_a_real_verdict")
    with pytest.raises(SystemExit, match="payload rejected by typed contract"):
        main(["--payload-json", json.dumps(payload)])


def test_empty_reason_codes_rejected() -> None:
    payload = _valid_payload(reason_codes=[])
    with pytest.raises(SystemExit, match="payload rejected by typed contract"):
        main(["--payload-json", json.dumps(payload)])


def test_inline_evidence_ref_rejected() -> None:
    payload = _valid_payload(evidence_refs=["not-a-sha256-and-has-inline-data"])
    with pytest.raises(SystemExit, match="payload rejected by typed contract"):
        main(["--payload-json", json.dumps(payload)])


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(SystemExit, match="invalid JSON payload"):
        main(["--payload-json", "{not valid json"])


def test_non_object_json_is_rejected() -> None:
    with pytest.raises(SystemExit, match="payload must be a JSON object"):
        main(["--payload-json", "[1, 2, 3]"])


def test_payload_file_input(tmp_path, capsys) -> None:
    payload = _valid_payload()
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    rc = main(["--payload-file", str(payload_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"


def test_payload_json_and_payload_file_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        main(["--payload-json", "{}", "--payload-file", "x.json"])
