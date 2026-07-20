from __future__ import annotations

import dataclasses
import hashlib
import importlib
import importlib.util
import inspect
import math
from decimal import Decimal

import pytest

S3_EXPECTED = (
    ("S3-00", 12, 0.35, 0.35, 1.25, 1.60, "baseline"),
    ("S3-01", 8, 0.35, 0.35, 1.25, 1.60, "OFAT"),
    ("S3-02", 10, 0.35, 0.35, 1.25, 1.60, "OFAT"),
    ("S3-03", 16, 0.35, 0.35, 1.25, 1.60, "OFAT"),
    ("S3-04", 20, 0.35, 0.35, 1.25, 1.60, "OFAT"),
    ("S3-05", 12, 0.20, 0.35, 1.25, 1.60, "OFAT"),
    ("S3-06", 12, 0.30, 0.35, 1.25, 1.60, "OFAT"),
    ("S3-07", 12, 0.50, 0.35, 1.25, 1.60, "OFAT"),
    ("S3-08", 12, 0.65, 0.35, 1.25, 1.60, "OFAT"),
    ("S3-09", 12, 0.35, 0.25, 1.25, 1.60, "OFAT"),
    ("S3-10", 12, 0.35, 0.30, 1.25, 1.60, "OFAT"),
    ("S3-11", 12, 0.35, 0.40, 1.25, 1.60, "OFAT"),
    ("S3-12", 12, 0.35, 0.45, 1.25, 1.60, "OFAT"),
    ("S3-13", 12, 0.35, 0.35, 1.00, 1.60, "OFAT"),
    ("S3-14", 12, 0.35, 0.35, 1.10, 1.60, "OFAT"),
    ("S3-15", 12, 0.35, 0.35, 1.40, 1.60, "OFAT"),
    ("S3-16", 12, 0.35, 0.35, 1.60, 1.60, "OFAT"),
    ("S3-17", 12, 0.35, 0.35, 1.25, 1.35, "OFAT"),
    ("S3-18", 12, 0.35, 0.35, 1.25, 1.45, "OFAT"),
    ("S3-19", 12, 0.35, 0.35, 1.25, 1.80, "OFAT"),
    ("S3-20", 12, 0.35, 0.35, 1.25, 2.00, "OFAT"),
    ("S3-21", 10, 0.30, 0.30, 1.25, 1.60, "interaction"),
    ("S3-22", 16, 0.50, 0.40, 1.25, 1.60, "interaction"),
    ("S3-23", 12, 0.50, 0.35, 1.40, 1.80, "interaction"),
)

S4_EXPECTED = (
    ("S4-00", 180, 1.80, 180, 1.25, 1.50, "baseline"),
    ("S4-01", 120, 1.80, 180, 1.25, 1.50, "OFAT"),
    ("S4-02", 150, 1.80, 180, 1.25, 1.50, "OFAT"),
    ("S4-03", 240, 1.80, 180, 1.25, 1.50, "OFAT"),
    ("S4-04", 300, 1.80, 180, 1.25, 1.50, "OFAT"),
    ("S4-05", 180, 1.40, 180, 1.25, 1.50, "OFAT"),
    ("S4-06", 180, 1.60, 180, 1.25, 1.50, "OFAT"),
    ("S4-07", 180, 2.00, 180, 1.25, 1.50, "OFAT"),
    ("S4-08", 180, 2.20, 180, 1.25, 1.50, "OFAT"),
    ("S4-09", 180, 1.80, 140, 1.25, 1.50, "OFAT"),
    ("S4-10", 180, 1.80, 160, 1.25, 1.50, "OFAT"),
    ("S4-11", 180, 1.80, 220, 1.25, 1.50, "OFAT"),
    ("S4-12", 180, 1.80, 260, 1.25, 1.50, "OFAT"),
    ("S4-13", 180, 1.80, 180, 1.00, 1.50, "OFAT"),
    ("S4-14", 180, 1.80, 180, 1.10, 1.50, "OFAT"),
    ("S4-15", 180, 1.80, 180, 1.40, 1.50, "OFAT"),
    ("S4-16", 180, 1.80, 180, 1.60, 1.50, "OFAT"),
    ("S4-17", 180, 1.80, 180, 1.25, 1.35, "OFAT"),
    ("S4-18", 180, 1.80, 180, 1.25, 1.65, "OFAT"),
    ("S4-19", 180, 1.80, 180, 1.25, 1.80, "OFAT"),
    ("S4-20", 180, 1.80, 180, 1.25, 2.00, "OFAT"),
    ("S4-21", 150, 1.60, 160, 1.25, 1.50, "interaction"),
    ("S4-22", 240, 2.00, 220, 1.25, 1.50, "interaction"),
    ("S4-23", 180, 2.00, 220, 1.40, 1.80, "interaction"),
)

S3_LABELS = (
    "baseline",
    "OFAT: 짧은 trend",
    "OFAT",
    "OFAT",
    "OFAT: 긴 trend",
    "OFAT: 얕은 pullback",
    "OFAT",
    "OFAT",
    "OFAT: 깊은 pullback",
    "OFAT: 낮은 효율",
    "OFAT",
    "OFAT",
    "OFAT: 높은 효율",
    "OFAT: 좁은 SL",
    "OFAT",
    "OFAT",
    "OFAT: 넓은 SL",
    "OFAT: 낮은 RR",
    "OFAT",
    "OFAT",
    "OFAT: 높은 RR",
    "interaction: 빠른 trend + 얕은 pullback",
    "interaction: 느린 trend + 깊은 pullback",
    "interaction: 깊은 pullback + 넓은 risk/return",
)

S4_LABELS = (
    "baseline",
    "OFAT: 짧은 beta window",
    "OFAT",
    "OFAT",
    "OFAT: 긴 beta window",
    "OFAT: 낮은 z",
    "OFAT",
    "OFAT",
    "OFAT: 높은 z",
    "OFAT: 작은 절대거리",
    "OFAT",
    "OFAT",
    "OFAT: 큰 절대거리",
    "OFAT: 좁은 SL",
    "OFAT",
    "OFAT",
    "OFAT: 넓은 SL",
    "OFAT: 낮은 RR",
    "OFAT",
    "OFAT",
    "OFAT: 높은 RR",
    "interaction: 빠른 적응 + 낮은 entry",
    "interaction: 안정성 우선 strict",
    "interaction: 큰 이탈 + tail 완충",
)


@pytest.fixture(scope="module")
def manifest():
    spec = importlib.util.find_spec("rob974_h3_manifest")
    assert spec is not None, "ROB-980 CP1 manifest behavior is not implemented"
    return importlib.import_module("rob974_h3_manifest")


def test_exact_48_row_roster_order_values_designs_and_labels(manifest):
    rows = manifest.FROZEN_H3_ROSTER
    assert type(rows) is tuple
    assert len(rows) == 48
    assert tuple(row.config_id for row in rows) == tuple(
        [f"S3-{index:02d}" for index in range(24)]
        + [f"S4-{index:02d}" for index in range(24)]
    )
    assert (
        tuple(
            (
                row.config_id,
                row.L,
                row.q_min,
                row.ER_min,
                row.k_SL,
                row.R_TP,
                row.design_type,
            )
            for row in rows[:24]
        )
        == S3_EXPECTED
    )
    assert (
        tuple(
            (
                row.config_id,
                row.W,
                row.z_entry,
                row.d_min_bp,
                row.k_SL,
                row.R_TP,
                row.design_type,
            )
            for row in rows[24:]
        )
        == S4_EXPECTED
    )
    assert tuple(row.authority_label for row in rows[:24]) == S3_LABELS
    assert tuple(row.authority_label for row in rows[24:]) == S4_LABELS
    assert manifest.FROZEN_S3_CONFIGS == rows[:24]
    assert manifest.FROZEN_S4_CONFIGS == rows[24:]


def test_hypothesis_bytes_are_exact_unmodified_line_plus_one_lf(manifest):
    expected = (
        (
            manifest.S3_HYPOTHESIS_UTF8,
            699,
            "f8893d558067e07f6248beb66fbc3f917af558634226484c6506c489e2752e01",
        ),
        (
            manifest.S4_HYPOTHESIS_UTF8,
            423,
            "c5152f9d207682a569c4df197e4e276cdf907d8eb6a2f68d3d9725b9bf229d28",
        ),
    )
    for raw, size, digest in expected:
        assert type(raw) is bytes
        assert len(raw) == size
        assert hashlib.sha256(raw).hexdigest() == digest
        assert raw.endswith(b"\n")
        assert not raw.endswith(b"\n\n")
        assert b"\r" not in raw
        assert all(not line.endswith(b" ") for line in raw.splitlines())
    assert all(
        row.hypothesis_utf8 is manifest.S3_HYPOTHESIS_UTF8
        for row in manifest.FROZEN_S3_CONFIGS
    )
    assert all(
        row.hypothesis_utf8 is manifest.S4_HYPOTHESIS_UTF8
        for row in manifest.FROZEN_S4_CONFIGS
    )


def test_structured_contract_seals_are_distinct_complete_and_source_honest(manifest):
    assert (
        manifest.RESEARCH_DOCUMENT_SHA256
        == "2f535196cf0f0a03292e8f4c1806794ffbf8282ba7b5c3f564a930763577a009"
    )
    s3 = manifest.S3_STRATEGY_CONTRACT
    s4 = manifest.S4_STRATEGY_CONTRACT
    assert (s3.key, s3.version) == ("rob974.s3.rpt-4h", "1")
    assert (s4.key, s4.version) == ("rob974.s4.brc-4h", "1")
    assert s3.source_research_sha256 == s4.source_research_sha256
    assert s3.source_research_sha256 == manifest.RESEARCH_DOCUMENT_SHA256
    assert (
        s3.contract_hash
        == "52118734b8fbbfe8c1fcec45641578e16b25a4bb22f625ff117cc40f648eb562"
    )
    assert (
        s4.contract_hash
        == "767fdeb136f7f78050890b98b6128e826ec49d0fdf2ed3ccbd0572cad4118180"
    )
    assert s3.contract_hash != s4.contract_hash
    assert s3.contract_hash != manifest.RESEARCH_DOCUMENT_SHA256
    assert s4.contract_hash != manifest.RESEARCH_DOCUMENT_SHA256
    assert all(len(item.contract_hash) == 64 for item in (s3, s4))
    manifest.validate_contract_seals(s3, s4)

    s3_payload = manifest.strategy_contract_payload("S3")
    s4_payload = manifest.strategy_contract_payload("S4")
    assert s3_payload["symbols"] == ["XRPUSDT", "DOGEUSDT", "SOLUSDT"]
    assert s4_payload["pairs"] == ["XRP-DOGE", "XRP-SOL", "DOGE-SOL"]
    assert s4_payload["posture"] == "historical_research_only"
    for payload in (s3_payload, s4_payload):
        assert payload["formulas"]
        assert payload["fixed_constants"]
        assert payload["parameter_domains"]
        assert payload["diagnostic_bins"]
        assert payload["mechanism_statements"]
        assert len(payload["configs"]) == 24


def test_manifest_validation_rejects_class_wide_tampering(manifest):
    rows = manifest.FROZEN_H3_ROSTER
    mutants = (
        rows[:-1],
        rows + (rows[-1],),
        rows[:1] + (rows[0],) + rows[2:],
        (rows[1], rows[0], *rows[2:]),
        (dataclasses.replace(rows[0], q_min=rows[5].q_min), *rows[1:]),
        (dataclasses.replace(rows[0], authority_label="OFAT"), *rows[1:]),
        (
            dataclasses.replace(
                rows[0], hypothesis_utf8=rows[0].hypothesis_utf8 + b"\n"
            ),
            *rows[1:],
        ),
        (*rows[:24], dataclasses.replace(rows[24], config_id="S3-00"), *rows[25:]),
    )
    for mutant in mutants:
        with pytest.raises(ValueError):
            manifest.validate_manifest(tuple(mutant))

    forged_source = dataclasses.replace(
        manifest.S3_STRATEGY_CONTRACT,
        contract_hash=manifest.RESEARCH_DOCUMENT_SHA256,
    )
    with pytest.raises(ValueError):
        manifest.validate_contract_seals(forged_source, manifest.S4_STRATEGY_CONTRACT)
    collided = dataclasses.replace(
        manifest.S4_STRATEGY_CONTRACT,
        contract_hash=manifest.S3_STRATEGY_CONTRACT.contract_hash,
    )
    with pytest.raises(ValueError):
        manifest.validate_contract_seals(manifest.S3_STRATEGY_CONTRACT, collided)


def test_exact_builtin_numeric_types_and_no_unit_or_fold_override(manifest):
    row = manifest.FROZEN_S3_CONFIGS[0]
    for bad in (True, 1, Decimal("1.25"), type("F", (float,), {})(1.25)):
        with pytest.raises(TypeError):
            dataclasses.replace(row, k_SL=bad)
    for bad in (True, 12.0, type("I", (int,), {})(12)):
        with pytest.raises(TypeError):
            dataclasses.replace(row, L=bad)
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            dataclasses.replace(row, q_min=bad)
    with pytest.raises(TypeError):
        dataclasses.replace(row, hypothesis_utf8=bytearray(row.hypothesis_utf8))
    assert tuple(inspect.signature(manifest.get_config).parameters) == ("config_id",)
    assert not {"symbol", "pair", "unit", "fold"} & {
        field.name for field in dataclasses.fields(row)
    }


def test_registered_membership_and_typed_hash_are_tamper_sensitive(manifest):
    canonical = manifest.FROZEN_S4_CONFIGS[0]
    manifest.assert_registered_config(canonical)
    with pytest.raises(ValueError):
        manifest.assert_registered_config(
            dataclasses.replace(canonical, z_entry=math.nextafter(1.80, math.inf))
        )
    payload = manifest.strategy_contract_payload("S4")
    changed = dict(payload)
    changed["fixed_constants"] = dict(payload["fixed_constants"])
    changed["fixed_constants"]["rho_min"] = math.nextafter(0.60, math.inf)
    assert (
        manifest.hash_contract_payload(changed)
        != manifest.S4_STRATEGY_CONTRACT.contract_hash
    )
