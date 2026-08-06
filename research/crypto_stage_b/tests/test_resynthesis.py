from __future__ import annotations

import hashlib

import pytest

from research.crypto_stage_b.resynthesis import (
    ArtifactSchemaError,
    apply_inconclusive_predicate,
    canonical_json_bytes,
    resynthesize_artifact_directory,
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
