from __future__ import annotations

import hashlib

import pytest

from research.crypto_stage_b.resynthesis import (
    ArtifactSchemaError,
    apply_inconclusive_predicate,
    canonical_json_bytes,
    resynthesize_artifact_directory,
    resynthesize_artifact_files,
    resynthesize_pair_artifact,
    write_json_once,
)


def _year(
    full: float | None,
    ablation: float | None,
    incremental: float | None,
) -> dict[str, object]:
    return {
        "incremental": {
            "full_net_mean_return": full,
            "ablation_net_mean_return": ablation,
            "full_minus_ablation_net_mean_return": incremental,
        }
    }


def _artifact(
    records: list[dict[str, object]],
    *,
    verdict_base: str = "PASS",
    verdict_sensitivity: str = "PASS",
    fragile: bool = False,
) -> dict[str, object]:
    return {
        "strategy_id": "CR-SPOT-TEST-01",
        "venue": "upbit_krw",
        "contract_hash": "frozen-contract-hash",
        "verdict_base": verdict_base,
        "verdict_sensitivity": verdict_sensitivity,
        "fragile": {"state": "EVALUATED", "fragile": fragile},
        "harness_query": {"records": records},
    }


def _resynthesize(records: list[dict[str, object]], **kwargs: object):
    return resynthesize_pair_artifact(
        _artifact(records, **kwargs),
        source_artifact_filename="pair.json",
        source_artifact_sha256="a" * 64,
    ).to_dict()


def _replay_year(
    year: int,
    *,
    full: float | None,
    ablation: float | None,
    full_count: int = 1,
    ablation_count: int = 1,
) -> dict[str, object]:
    return {
        "full": {
            "calendar_year": year,
            "net_mean_return": full,
            "resolved_exit_count": full_count if full is not None else 0,
        },
        "ablation": {
            "calendar_year": year,
            "net_mean_return": ablation,
            "resolved_exit_count": ablation_count if ablation is not None else 0,
        },
        "incremental": {
            "full_net_mean_return": full,
            "ablation_net_mean_return": ablation,
            "full_minus_ablation_net_mean_return": (
                None if full is None or ablation is None else full - ablation
            ),
        },
    }


def _replay_artifact(
    records: list[dict[str, object]],
    *,
    full_net_mean_return: float,
    ablation_net_mean_return: float,
    full_sensitivity_net_mean_return: float | None = None,
    ablation_sensitivity_net_mean_return: float | None = None,
) -> dict[str, object]:
    return {
        "strategy_id": "CR-SPOT-TEST-01",
        "venue": "upbit_krw",
        "pair": {
            "strategy_id": "CR-SPOT-TEST-01",
            "venue": "upbit_krw",
            "contract_hash": "frozen-contract-hash",
            "full": {
                "net_mean_return": full_net_mean_return,
                "sensitivity_net_mean_return": (
                    full_net_mean_return
                    if full_sensitivity_net_mean_return is None
                    else full_sensitivity_net_mean_return
                ),
            },
            "ablation": {
                "net_mean_return": ablation_net_mean_return,
                "sensitivity_net_mean_return": (
                    ablation_net_mean_return
                    if ablation_sensitivity_net_mean_return is None
                    else ablation_sensitivity_net_mean_return
                ),
            },
        },
        "harness_query": {
            "strategy_id": "CR-SPOT-TEST-01",
            "venue": "upbit_krw",
            "contract_hash": "frozen-contract-hash",
            "records": records,
        },
    }


def test_one_contributing_year_is_inconclusive_not_a_new_sample_gate() -> None:
    result = _resynthesize([_year(0.1, 0.02, 0.08)])

    assert result["headline"] == {
        "verdict_base": "INCONCLUSIVE_INSUFFICIENT_JUDGEABLE_YEARS",
        "verdict_sensitivity": "INCONCLUSIVE_INSUFFICIENT_JUDGEABLE_YEARS",
        "judgeable_years": 1,
        "fragile": False,
    }


def test_judgeable_years_is_not_the_total_calendar_year_count() -> None:
    result = _resynthesize(
        [
            _year(None, 0.02, None),
            _year(0.1, 0.02, 0.08),
            _year(None, 0.02, None),
            _year(None, 0.02, None),
        ]
    )

    assert result["headline"]["judgeable_years"] == 1
    assert result["headline"]["verdict_base"] == (
        "INCONCLUSIVE_INSUFFICIENT_JUDGEABLE_YEARS"
    )


def test_empty_arm_year_never_counts_as_judgeable() -> None:
    result = _resynthesize([_year(None, 0.02, None), _year(None, 0.03, None)])

    assert result["headline"]["judgeable_years"] == 0
    assert result["headline"]["verdict_base"] == (
        "INCONCLUSIVE_INSUFFICIENT_JUDGEABLE_YEARS"
    )


def test_all_arms_empty_has_its_own_inconclusive_label() -> None:
    result = _resynthesize([_year(None, None, None), _year(None, None, None)])

    assert result["headline"]["verdict_base"] == "INCONCLUSIVE_EMPTY_ALL_ARMS"
    assert result["headline"]["verdict_sensitivity"] == "INCONCLUSIVE_EMPTY_ALL_ARMS"


def test_inconclusive_precedes_existing_falsified_but_run_invalid_wins() -> None:
    result = _resynthesize(
        [_year(0.1, 0.02, 0.08)],
        verdict_base="FALSIFIED_NO_INCREMENT_OVER_ABLATION",
        verdict_sensitivity="FALSIFIED_NONPOSITIVE_NET_RETURN",
    )

    assert result["headline"]["verdict_base"] == (
        "INCONCLUSIVE_INSUFFICIENT_JUDGEABLE_YEARS"
    )
    assert (
        apply_inconclusive_predicate(
            "RUN_INVALID_MISSING_EXIT", judgeable_years=0, all_arms_empty=True
        )
        == "RUN_INVALID_MISSING_EXIT"
    )


def test_headline_discloses_fragile_without_using_it_as_a_gate() -> None:
    records = [_year(0.1, 0.02, 0.08), _year(0.03, 0.01, 0.02)]

    fragile = _resynthesize(records, fragile=True)
    stable = _resynthesize(records, fragile=False)

    assert fragile["headline"]["fragile"] is True
    assert stable["headline"]["fragile"] is False
    assert (
        fragile["headline"]["verdict_base"]
        == stable["headline"]["verdict_base"]
        == "PASS"
    )


def test_directory_resynthesis_checks_all_source_hashes_before_parsing(
    tmp_path,
) -> None:
    manifest = tmp_path / "manifest.json"
    pair = tmp_path / "pair.json"
    manifest.write_bytes(canonical_json_bytes({"kind": "manifest"}))
    pair.write_bytes(canonical_json_bytes(_artifact([_year(0.1, 0.02, 0.08)])))
    expected = {
        "manifest.json": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "pair.json": hashlib.sha256(pair.read_bytes()).hexdigest(),
    }

    payload = resynthesize_artifact_directory(tmp_path, expected_sha256=expected)

    assert payload["resynthesis_only"] is True
    assert payload["records"][0]["headline"]["judgeable_years"] == 1
    pair.write_text("{}", encoding="utf-8")
    with pytest.raises(ArtifactSchemaError, match="SHA-256 mismatch"):
        resynthesize_artifact_directory(tmp_path, expected_sha256=expected)


def test_writer_is_atomic_and_refuses_overwrite(tmp_path) -> None:
    target = tmp_path / "result.json"
    payload = {"resynthesis_only": True}

    digest = write_json_once(target, payload)

    assert digest == hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_json_once(target, payload)


def test_replay_record_reconstructs_existing_verdict_and_fragile_evidence() -> None:
    artifact = _replay_artifact(
        [
            _replay_year(2020, full=0.10, ablation=0.02),
            _replay_year(2021, full=-0.01, ablation=0.02),
        ],
        full_net_mean_return=0.045,
        ablation_net_mean_return=0.02,
    )

    result = resynthesize_pair_artifact(
        artifact,
        source_artifact_filename="replay.json",
        source_artifact_sha256="b" * 64,
    ).to_dict()

    assert result["source_verdict_base"] == "PASS"
    assert result["headline"] == {
        "verdict_base": "PASS",
        "verdict_sensitivity": "PASS",
        "judgeable_years": 2,
        "fragile": True,
    }
    assert result["fragile_evidence"] == {
        "state": "EVALUATED",
        "fragile": True,
        "highest_year_net_mean_return": 0.10,
        "highest_years": [2020],
        "remaining_net_mean_returns": {"2020": -0.01},
    }


@pytest.mark.parametrize(
    ("full", "ablation", "expected_verdict"),
    [
        (-0.01, -0.02, "FALSIFIED_NONPOSITIVE_NET_RETURN"),
        (0.01, 0.02, "FALSIFIED_NO_INCREMENT_OVER_ABLATION"),
    ],
)
def test_replay_record_reconstructs_frozen_falsification_precedence(
    full: float, ablation: float, expected_verdict: str
) -> None:
    artifact = _replay_artifact(
        [
            _replay_year(2020, full=full, ablation=ablation),
            _replay_year(2021, full=full, ablation=ablation),
        ],
        full_net_mean_return=full,
        ablation_net_mean_return=ablation,
    )

    result = resynthesize_pair_artifact(
        artifact,
        source_artifact_filename="replay.json",
        source_artifact_sha256="b" * 64,
    ).to_dict()

    assert result["source_verdict_base"] == expected_verdict
    assert result["headline"]["verdict_base"] == expected_verdict


def test_file_resynthesis_hash_binds_separate_pair_and_manifest_sources(
    tmp_path,
) -> None:
    pair = tmp_path / "pair.json"
    correction_manifest = tmp_path / "correction-manifest.json"
    etr_manifest = tmp_path / "etr-manifest.json"
    pair.write_bytes(
        canonical_json_bytes(
            _replay_artifact(
                [
                    _replay_year(2020, full=0.02, ablation=0.01),
                    _replay_year(2021, full=0.04, ablation=0.01),
                ],
                full_net_mean_return=0.03,
                ablation_net_mean_return=0.01,
            )
        )
    )
    correction_manifest.write_bytes(canonical_json_bytes({"kind": "correction"}))
    etr_manifest.write_bytes(canonical_json_bytes({"kind": "etr"}))

    payload = resynthesize_artifact_files(
        pair_sources={"pair.json": pair},
        expected_pair_sha256={
            "pair.json": hashlib.sha256(pair.read_bytes()).hexdigest()
        },
        manifest_sources={
            "correction_manifest": correction_manifest,
            "etr_manifest": etr_manifest,
        },
        expected_manifest_sha256={
            "correction_manifest": hashlib.sha256(
                correction_manifest.read_bytes()
            ).hexdigest(),
            "etr_manifest": hashlib.sha256(etr_manifest.read_bytes()).hexdigest(),
        },
    )

    assert payload["resynthesis_only"] is True
    assert payload["source_artifact_sha256"] == {
        "pair.json": hashlib.sha256(pair.read_bytes()).hexdigest()
    }
    assert [item["label"] for item in payload["source_manifests"]] == [
        "correction_manifest",
        "etr_manifest",
    ]
