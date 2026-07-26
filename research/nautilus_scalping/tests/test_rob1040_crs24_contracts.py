from __future__ import annotations

import hashlib
from pathlib import Path

import rob1040_crs24_contracts as contract

from research_contracts.canonical_hash import canonical_sha256


def test_exact_universe_config_slots_and_permanent_closed_slot() -> None:
    assert contract.UNIVERSE == ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
    assert tuple(
        (
            item.config_id,
            item.formation_hours,
            item.formation_return_count,
            item.posture,
        )
        for item in contract.ACTIVE_CONFIGS
    ) == (
        ("CRS-A0", 168, 42, "primary"),
        ("CRS-A1", 72, 18, "sensitivity"),
        ("CRS-A2", 336, 84, "sensitivity"),
    )
    assert tuple(item.identifier for item in contract.CONFIG_SLOTS) == (
        "CRS-A0",
        "CRS-A1",
        "CRS-A2",
        "CLOSED_UNUSED",
    )
    closed = contract.CONFIG_SLOTS[3]
    assert closed.slot == 3
    assert closed.formation_hours is None
    assert closed.formation_return_count is None
    assert closed.posture == "permanently_closed"


def test_canonical_preregistration_bytes_are_exactly_sealed() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "contracts"
        / "rob1040_crs24_corr1_preregistration.md"
    )
    raw = path.read_bytes()
    assert len(raw) == contract.PREREGISTRATION_SIZE == 6_883
    assert hashlib.sha256(raw).hexdigest() == contract.PREREGISTRATION_SHA256
    assert contract.canonical_preregistration_bytes(raw) is raw
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    assert b"\r" not in raw
    assert all(not line.endswith(b" ") for line in raw.splitlines())


def test_filter_fold_and_contract_hashes_seal_their_payloads() -> None:
    assert contract.FILTER_MANIFEST_SHA256 == canonical_sha256(
        contract.filter_manifest_payload()
    )
    assert contract.FOLD_SCHEDULE_SHA256 == canonical_sha256(
        contract.fold_schedule_payload()
    )
    assert contract.CONTRACT_SHA256 == canonical_sha256(contract.contract_payload())
    contract.validate_contract()


def test_static_filter_fixture_and_sizing_posture_are_frozen() -> None:
    assert tuple(
        (item.symbol, str(item.quantity_step)) for item in contract.SYMBOL_FILTERS
    ) == (
        ("XRPUSDT", "0.1"),
        ("DOGEUSDT", "1"),
        ("SOLUSDT", "0.01"),
    )
    payload = contract.filter_manifest_payload()
    assert payload["authority"] == "rob1040_local_static_fixture"
    assert payload["sizing"] == {
        "target_reference_notional_usdt": "8",
        "accepted_reference_notional_usdt": ["6", "10"],
        "quantity_rounding": "floor_to_step",
        "leverage": 1,
        "position_mode": "one_way",
    }


def test_frozen_numeric_contract_has_no_override_surface() -> None:
    assert contract.VOLATILITY_RETURN_COUNT == 42
    assert contract.VOLATILITY_FLOOR == 1e-8
    assert contract.PIT_LOOKBACK_MS == 60 * contract.DAY_MS
    assert contract.PIT_MIN_OBSERVATIONS == 100
    assert contract.DISPERSION_QUANTILE == 0.50
    assert contract.COMMON_MAGNITUDE_QUANTILE == 0.75
    assert contract.ARBITRATION_TOLERANCE == 1e-12
    assert contract.ENTRY_DELAY_MS == 60_000
    assert contract.HOLD_MS == 24 * 60 * 60 * 1000
