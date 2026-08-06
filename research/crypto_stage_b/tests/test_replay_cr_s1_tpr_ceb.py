from __future__ import annotations

import hashlib
import json

import pytest

from scripts import replay_cr_s1_tpr_ceb as replay


def test_replay_driver_is_bounded_to_exactly_four_tpr_ceb_pairs() -> None:
    assert set(replay._FROZEN_PAIR_SPECS) == {  # noqa: SLF001 - frozen CLI contract
        "tpr_upbit_krw",
        "tpr_binance_usdt_spot",
        "ceb_upbit_krw",
        "ceb_binance_usdt_spot",
    }
    assert {
        spec.strategy_id
        for spec in replay._FROZEN_PAIR_SPECS.values()  # noqa: SLF001
    } == {"CR-SPOT-TPR-01", "CR-SPOT-CEB-01"}
    assert {
        spec.output_filename
        for spec in replay._FROZEN_PAIR_SPECS.values()  # noqa: SLF001
    } == {
        "cr-spot-tpr-01__upbit_krw.json",
        "cr-spot-tpr-01__binance_usdt_spot.json",
        "cr-spot-ceb-01__upbit_krw.json",
        "cr-spot-ceb-01__binance_usdt_spot.json",
    }


def test_replay_record_writer_is_atomic_and_refuses_overwrite(tmp_path) -> None:
    target = tmp_path / "cr-spot-tpr-01__upbit_krw.json"
    payload = {"schema_version": "test", "value": [1, 2, 3]}

    digest = replay.write_json_once(target, payload)

    expected_bytes = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    assert digest == hashlib.sha256(expected_bytes).hexdigest()
    assert target.read_bytes() == expected_bytes
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        replay.write_json_once(target, {"schema_version": "replacement"})
    assert target.read_bytes() == expected_bytes


def test_replay_driver_and_engine_manifest_are_content_bound() -> None:
    driver_sha256 = replay._sha256_file(replay._DRIVER_PATH)  # noqa: SLF001
    manifest = replay._code_blob_manifest()  # noqa: SLF001

    assert len(driver_sha256) == 64
    assert all(len(digest) == 64 for digest in manifest.values())
    assert replay._code_blob_manifest_sha256(manifest) == replay._sha256_json(manifest)  # noqa: SLF001


def test_budget_literal_and_accounting_are_bound_to_the_driver() -> None:
    assert replay._BUDGET_LITERAL == (  # noqa: SLF001 - evidence literal contract
        "이 replay 는 기존 구현 오류의 correction 이며 새 trial 이 아니다 — D2 예산 미소모."
    )


def test_manifest_seals_exactly_the_four_bound_records(tmp_path) -> None:
    driver_sha256 = replay._sha256_file(replay._DRIVER_PATH)  # noqa: SLF001
    code_manifest_sha256 = replay._code_blob_manifest_sha256(  # noqa: SLF001
        replay._code_blob_manifest()  # noqa: SLF001
    )
    for spec in replay._FROZEN_PAIR_SPECS.values():  # noqa: SLF001
        payload = {
            "strategy_id": spec.strategy_id,
            "venue": spec.venue,
            "driver": {"blob_sha256": driver_sha256},
            "engine_code_manifest_sha256": code_manifest_sha256,
            "replay_classification": {
                "budget_literal": replay._BUDGET_LITERAL,  # noqa: SLF001
                "trial_count": 0,
                "correction_replay_count": 1,
            },
            "pair_record_stream_sha256": "a" * 64,
            "run_contract": {
                "contract_hash": "test-contract",
                "cost_literal": {
                    "round_trip_bp": 30,
                    "sensitivity_round_trip_bp": 70,
                },
            },
        }
        (tmp_path / spec.output_filename).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    manifest = replay.build_manifest(
        output_dir=tmp_path,
        expected_driver_sha256=driver_sha256,
        expected_code_manifest_sha256=code_manifest_sha256,
    )

    assert manifest["replay_classification"]["trial_count"] == 0
    assert manifest["replay_classification"]["correction_replay_count"] == 4
    assert manifest["scope"]["etr_pairs_touched"] == 0
    assert len(manifest["records"]) == 4
