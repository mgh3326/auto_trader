"""ROB-970 R1/R2 permits one exact post-ROB-970 production-source repair window.

ROB-970's own re-pin tip (``cc462d94``, which already includes its
authorized S2 timestamp root fix + original Q2/Q3 diagnostic-evidence
boundary) is the comparison base -- ROB-970 R1, and now R2 (two passes: the
initial audit response, then a further pre-finalization stop-gate) on top
of it, supersede that single-change-window check because the Critical-1
fail-closed redaction hardening, Q1=A cap=32 bounded diagnostic evidence,
the Q2=C-modified replay-divergence observation, and (R2) the frame-aware
truncation/canonical-byte replay/app-service-boundary/real-observer-effect-0
hardening -- plus the stop-gate's genuine model_construct/TOCTOU close, the
shared ``research_contracts.diagnostic_evidence_policy`` cap authority
(a brand-new file under a frozen path), the hostile-frame-filename/
boundary-truncation fixes, and the full persistence-chain test (all
Fable/audit-approved) -- are intentionally lineage-changing now
(``rob944_walkforward.py`` and friends are themselves part of the hashed
frozen-campaign source provenance). Test files are reviewable support
changes; every production add/modify/delete/rename/copy/type-change remains
in scope. ``_FULL_CAMPAIGN_HASH`` did NOT move again in the stop-gate pass
(none of its touched files this round feed the envelope's hash inputs
differently than before).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path, PurePosixPath

from rob944_frozen_campaign import build_production_frozen_campaign_envelope

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ROB960_MERGE_HEAD = "cc462d949c60d74f5edcfc2d3005b3e4033446ce"
_COST_MODEL_PATH = "research/nautilus_scalping/rob940_cost_model.py"
_COST_MODEL_SHA256 = "a5db97037a4fa3acd3712bbe82ec8d69eea4d3545d926de30d752398a3ea5366"
_FULL_CAMPAIGN_HASH = "605d5e30b57656d9e43ff55134baaa093883ce1f1aa6ffc4c467f0105d49f152"
_AUTHORIZED_PRODUCTION_CHANGES = [
    ("A", ("research_contracts/diagnostic_evidence_policy.py",)),
    ("M", ("app/schemas/research_campaign_bridge.py",)),
    ("M", ("app/services/research_campaign_bridge.py",)),
    ("M", ("research/nautilus_scalping/rob944_diagnostic_evidence.py",)),
    ("M", ("research/nautilus_scalping/rob944_walkforward.py",)),
    ("M", ("research/nautilus_scalping/rob945_accounting_seal.py",)),
    ("M", ("research/nautilus_scalping/rob945_h6_summary_contract.py",)),
    ("M", ("research/nautilus_scalping/run_rob944_campaign.py",)),
    # ROB-978 authorized additive modules. These are intentionally explicit:
    # future rob974_* production files still require their own guard re-pin.
    ("A", ("research/nautilus_scalping/rob974_features.py",)),
    ("A", ("research/nautilus_scalping/rob974_lineage.py",)),
    ("A", ("research/nautilus_scalping/rob974_smoke.py",)),
]
_FROZEN_PATHS = (
    "research/nautilus_scalping",
    "app/services/rob944_campaign_controller.py",
    "app/schemas/research_campaign_bridge.py",
    "app/services/research_campaign_bridge.py",
    "app/services/research_db_write_guard.py",
    "research_contracts",
)
_TEST_SUPPORT_PATHS = frozenset({"research/nautilus_scalping/conftest.py"})


def _parse_name_status_z(raw: str) -> list[tuple[str, tuple[str, ...]]]:
    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()

    changes: list[tuple[str, tuple[str, ...]]] = []
    cursor = 0
    while cursor < len(fields):
        status = fields[cursor]
        cursor += 1
        path_count = 2 if status[:1] in {"R", "C"} else 1
        paths = tuple(fields[cursor : cursor + path_count])
        if len(paths) != path_count:
            raise AssertionError(f"malformed git --name-status -z output: {fields!r}")
        cursor += path_count
        changes.append((status, paths))
    return changes


def _changes_since_rob960() -> list[tuple[str, tuple[str, ...]]]:
    diff = subprocess.run(
        (
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            _ROB960_MERGE_HEAD,
            "--",
            *_FROZEN_PATHS,
        ),
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    untracked = subprocess.run(
        (
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            *_FROZEN_PATHS,
        ),
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    changes = _parse_name_status_z(diff.stdout)
    changes.extend(("?", (path,)) for path in untracked.stdout.split("\0") if path)
    return sorted(changes)


def _is_test_path(path: str) -> bool:
    return "tests" in PurePosixPath(path).parts or path in _TEST_SUPPORT_PATHS


def test_rob970_r1_repair_is_the_only_post_rob970_production_delta():
    production_changes = [
        change
        for change in _changes_since_rob960()
        if not all(_is_test_path(path) for path in change[1])
    ]
    assert production_changes == sorted(_AUTHORIZED_PRODUCTION_CHANGES)


def test_rob962_cost_model_bytes_and_derived_lineage_are_exactly_refrozen():
    cost_model_bytes = (_REPO_ROOT / _COST_MODEL_PATH).read_bytes()
    assert hashlib.sha256(cost_model_bytes).hexdigest() == _COST_MODEL_SHA256
    assert (
        build_production_frozen_campaign_envelope().full_campaign_hash()
        == _FULL_CAMPAIGN_HASH
    )
