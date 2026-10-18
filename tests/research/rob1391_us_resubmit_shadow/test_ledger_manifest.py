"""Append-only hash-chained ledger and T0 manifest seal."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from research.rob1391_us_resubmit_shadow import constants as K
from research.rob1391_us_resubmit_shadow.ledger import Ledger, LedgerIntegrityError
from research.rob1391_us_resubmit_shadow.manifest import (
    ManifestError,
    build_manifest,
    verify_against_runtime,
)
from tests.research.rob1391_us_resubmit_shadow.helpers import (
    MANDATORY_DEVIATIONS,
    POLICY,
    SEAL_AT,
    seal,
)

pytestmark = pytest.mark.unit
NOW = datetime(2025, 11, 20, tzinfo=UTC)


def test_manifest_contents_and_single_seal(tmp_path):
    ledger = seal(tmp_path)
    manifest, digest = ledger.read_manifest()
    assert manifest["experiment_id"] == K.EXPERIMENT_ID
    assert (
        manifest["constants"]["seed"] == 1383
        and manifest["constants"]["e0_usd"] == "10000"
    )
    assert manifest["market_data"] == {
        **manifest["market_data"],
        "host": "data.alpaca.markets",
        "feed": "sip",
    }
    assert (
        manifest["window"]["start_session"] == "2025-11-14"
        and len(manifest["window"]["sessions"]) == 20
    )
    assert (
        manifest["net_metrics_status"] == "SEALED"
        and manifest["fee_schedule"]["sha256"]
    )
    assert "fixture prereg" not in json.dumps(manifest)  # only the hash is sealed
    with pytest.raises(LedgerIntegrityError):
        ledger.write_manifest(manifest)
    assert ledger.load()[0].payload == {"manifest_sha256": digest}


def test_manifest_tamper_is_detected(tmp_path):
    ledger = seal(tmp_path)
    raw = ledger.manifest_path.read_text().replace(
        '"e0_usd":"10000"', '"e0_usd":"20000"'
    )
    ledger.manifest_path.write_text(raw)
    with pytest.raises(LedgerIntegrityError):
        ledger.read_manifest()


def test_event_tamper_and_chain_break_are_detected(tmp_path):
    ledger = seal(tmp_path)
    ledger.append(
        "latch",
        {"latch_type": "loss", "reason": "x", "session": None, "detail": None},
        now=NOW,
    )
    lines = ledger.events_path.read_text().splitlines()
    # deleting the latch line is not a way to resume
    ledger.events_path.write_text(lines[0] + "\n")
    with pytest.raises(LedgerIntegrityError, match="truncated"):
        ledger.load()
    ledger.events_path.write_text("\n".join(lines) + "\n")
    assert len(ledger.load()) == 2
    tampered = lines[1].replace('"loss"', '"policy_change"')
    ledger.events_path.write_text(lines[0] + "\n" + tampered + "\n")
    with pytest.raises(LedgerIntegrityError, match="hash mismatch"):
        ledger.load()
    ledger.events_path.write_text(lines[1] + "\n" + lines[0] + "\n")
    with pytest.raises(LedgerIntegrityError):
        ledger.load()


def test_append_refuses_before_seal(tmp_path):
    with pytest.raises(LedgerIntegrityError):
        Ledger(tmp_path / "x").append("register", {"session": "x"}, now=NOW)


def test_lock_refuses_concurrent_command(tmp_path):
    ledger = seal(tmp_path)
    with ledger.locked():
        with pytest.raises(LedgerIntegrityError):
            with ledger.locked():
                pass


def test_seal_refuses_policy_mismatch_and_missing_approval(tmp_path):
    prereg = tmp_path / "p.md"
    prereg.write_text("x")
    kwargs = {
        "seal_at": SEAL_AT,
        "approval_ref": "ok",
        "prereg_path": prereg,
        "code_sha": "abc",
        "policy_stamp": dict(POLICY),
        "deployed_policy": dict(POLICY),
        "feed": "sip",
        "fee_schedule_path": None,
        "approved_deviations": MANDATORY_DEVIATIONS,
        "deviation_ref": "fixture-ref",
    }
    assert build_manifest(**kwargs)["net_metrics_status"] == "PENDING_FEE_SCHEDULE"
    with pytest.raises(ManifestError):
        build_manifest(
            **{**kwargs, "deployed_policy": {"version": "other", "content_hash": "x"}}
        )
    with pytest.raises(ManifestError):
        build_manifest(**{**kwargs, "approval_ref": " "})
    with pytest.raises(ManifestError):
        build_manifest(**{**kwargs, "deviation_ref": None})
    optional = MANDATORY_DEVIATIONS | {"D3_budget_fill_notional_only"}
    assert build_manifest(**{**kwargs, "approved_deviations": optional})


def test_runtime_constant_or_calendar_drift_is_reported(tmp_path, monkeypatch):
    ledger = seal(tmp_path)
    manifest, _ = ledger.read_manifest()
    assert verify_against_runtime(manifest) == []
    monkeypatch.setattr(K, "MAX_PARENTS_PER_SESSION", 4)
    assert "constants_changed_since_seal" in verify_against_runtime(manifest)
    monkeypatch.undo()
    manifest["calendar"]["sessions"][5]["close_utc"] = "2025-01-01T00:00:00+00:00"
    assert "calendar_drift_since_seal" in verify_against_runtime(manifest)
