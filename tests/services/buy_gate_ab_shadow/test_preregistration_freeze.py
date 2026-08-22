"""§139차 ② — the §7-1 pre-registration is frozen and its blockers are honest.

A pre-registration nobody can verify is a post-registration. Two guards:

1. The verbatim declaration is hash-pinned, so an in-place edit to the
   hypothesis / window / sample target / promotion rule fails here.
2. The blocking start conditions in §7 are asserted against the code they
   describe. When the real work lands, these flip and the failure message
   says to update §7 and §9 — a reverse tripwire, on purpose: the document
   must not keep claiming "not collecting" after collection becomes possible.
"""

from __future__ import annotations

import hashlib
import pathlib
import re

import pytest

from app.services.buy_gate_ab_shadow.evaluate import ALLOWED_MARKETS, CANDIDATE_KEYS
from app.services.buy_gate_ab_shadow.spec import PRE_REGISTRATION

pytestmark = pytest.mark.unit

_DOC = (
    pathlib.Path(__file__).resolve().parents[3]
    / "docs"
    / "preregistrations"
    / "2026-08-22-support-strength-two-source-equivalence.md"
)


def _text() -> str:
    return _DOC.read_text(encoding="utf-8")


def _verbatim_block() -> str:
    match = re.search(r"```\n(.*?)\n```", _text(), re.S)
    assert match is not None, "the verbatim declaration block is missing"
    return match.group(1) + "\n"


def test_the_registered_declaration_is_hash_frozen() -> None:
    text = _text()
    claimed = re.search(r"`([0-9a-f]{64})`", text)
    assert claimed is not None, "the declaration's sha256 pin is missing"

    computed = hashlib.sha256(_verbatim_block().encode("utf-8")).hexdigest()

    assert computed == claimed.group(1), (
        "the frozen declaration changed. A pre-registration is never edited in "
        "place — add a new dated file that supersedes this one "
        "(docs/preregistrations/README.md)."
    )


def test_the_declaration_still_carries_its_load_bearing_terms() -> None:
    """Guards against a 'reformatting' that quietly drops a commitment."""

    block = _verbatim_block()

    assert "promote=false" in block
    assert "calibration_exclude=true" in block
    assert "source_count>=2" in block
    assert "2026-08-22 ~ 2026-09-19" in block
    assert "n>=40" in block
    # the promotion rule, all three clauses
    assert "D+20 중앙값 >= 0" in block
    assert "하위 4분위 평균 > -6%" in block
    assert "하나라도 미달이면 기각한다" in block
    # execution impact and the US precondition
    assert "집행 영향: 0" in block
    assert "US overhang" in block


def test_registered_status_says_collection_has_not_started() -> None:
    text = _text()

    assert "REGISTERED, NOT COLLECTING" in text
    assert "Collection is **not** started by registering this document" in text


# ---------------------------------------------------------------------------
# Blocking start conditions — asserted against the code they describe.
# ---------------------------------------------------------------------------


def test_b1_code_spec_is_not_yet_amended_to_this_declaration() -> None:
    """B1: the pinned ROB-1301 spec still declares the *other* experiment.

    When B1 is done, this test fails. That is the signal to update §7.1 and
    the §9 amendment log — not to delete the assertion.
    """

    assert PRE_REGISTRATION["variant_b"]["support_strength_min"] == "moderate"
    assert PRE_REGISTRATION["only_difference"] == "support_strength_min"
    assert PRE_REGISTRATION["markets"] == ["kr", "us"]
    assert PRE_REGISTRATION["windows_trading_days"] == [5, 20]
    assert "promotion_rule" not in PRE_REGISTRATION["scoring"]


def test_b2_evaluator_cannot_yet_accept_source_count_evidence() -> None:
    """B2: variant B needs source-count/family evidence the contract lacks.

    Critically, this fails *loudly* rather than silently: since §138차 ③ an
    unknown key is rejected, so a session cannot start sending
    support_source_count and quietly collect a meaningless cohort.
    """

    assert "support_source_count" not in CANDIDATE_KEYS
    assert not any("famil" in key for key in CANDIDATE_KEYS)

    from app.services.buy_gate_ab_shadow.evaluate import (
        CandidateEvidence,
        EvaluationError,
    )

    with pytest.raises(EvaluationError) as exc:
        CandidateEvidence.from_mapping(
            {
                "symbol": "005930",
                "market": "kr",
                "current_price": "70000",
                "support_strength": "moderate",
                "support_source_count": 2,
            }
        )
    assert "support_source_count" in str(exc.value)


def test_c1_crypto_arm_is_not_reachable_yet() -> None:
    """C1: the declaration covers crypto winner_pullback_add; the code does not."""

    assert "crypto" not in ALLOWED_MARKETS
    assert set(ALLOWED_MARKETS) == {"kr", "us"}


def test_doc_records_every_blocker_it_relies_on() -> None:
    text = _text()

    for blocker in ("B1", "B2", "B3", "B4", "U1", "C1", "C2"):
        assert f"| {blocker} |" in text, f"blocker {blocker} vanished from §7"
    # the start checklist must list them too
    for blocker in ("B1", "B2", "B3", "B4", "U1", "C1", "C2"):
        assert f"[ ] {blocker}" in text


def test_readme_declares_the_no_inplace_edit_rule() -> None:
    readme = (_DOC.parent / "README.md").read_text(encoding="utf-8")

    assert "never edited in place" in readme
    assert "2026-08-22-support-strength-two-source-equivalence.md" in readme
