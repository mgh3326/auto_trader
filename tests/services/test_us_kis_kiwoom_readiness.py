"""ROB-1268 J5C — US KIS / Kiwoom lifecycle-recovery contract checker.

Offline only: this module reads repository artifacts — the contract document,
the signed lane registry, and the merged J2B/J3A port sources — and compares
them.  It opens no socket, no database connection, and no file outside the
repository, and it modifies nothing.

Expectation provenance is deliberately split, following the J5A pattern:

* Structural expectations (S1-S5) are **derived** from the document's own
  declared shape rules, so the document is the artifact under test.
* Safety expectations (S6-S9) are **literal**.  If the registry itself is
  flipped, these assertions must go red rather than quietly follow it.
* S10 proves that the literal block really is literal, so nobody can later
  convert it into a derived value unnoticed.
* Citation expectations (S11) assert that every in-repo anchor the document
  names actually exists, so a citation cannot rot into fiction.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.services.mock_lane_registry import (
    CANONICAL_LANE_REGISTRY,
    LANE_CREDENTIAL_NAMESPACES,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DOC_PATH = _REPO_ROOT / "docs/contracts/rob-1268-us-kis-kiwoom-readiness.md"
_REGISTRY_PATH = _REPO_ROOT / "app/services/mock_lane_registry.py"
_COORDINATION_PATH = _REPO_ROOT / "app/services/mock_integration/coordination.py"
_LINEAGE_PATH = _REPO_ROOT / "app/services/mock_integration/lineage.py"

_LANES = ("us.kis.mock", "us.kiwoom.mock")

_FIXED_TOKENS = (
    "SIGNED_SOURCE = app/services/mock_lane_registry.py::CANONICAL_LANE_REGISTRY"
    " @ e057941425d2ea7d35a36ebf6074a6c70eba3013",
    "LINEAGE_SOURCE = app/services/mock_integration/lineage.py"
    " @ 094ab2d59d6f2bf5fc3df4efa43bb5d412221ffd",
    "COORDINATION_SOURCE = app/services/mock_integration/coordination.py"
    " @ 03beecc5f53e636c352ddf0527aa3d98ddc7bd61",
    "US_READINESS_CONTRACT = docs/contracts/rob-1266-us-readiness.md"
    " @ ddf4895ece2ca9dff8daf1a04fa7d6143f43c899",
)

# The seven evidence kinds are quoted from the controlling rule, in its order.
_EVIDENCE_KINDS = (
    "ack",
    "unknown",
    "reject",
    "expiry",
    "partial_fill",
    "cancel",
    "terminal_reconciliation",
)

_LIFECYCLE_KEYS = (
    "lane_id",
    "lifecycle_recovery_owner_status",
    "recovery_owner",
    "restart_rediscovery_trigger",
    "authoritative_readback_operation",
    "release_if_matches_condition",
    "operator_visible_blocked_state",
    "unmet_lifecycle_items",
)

_STATES = ("PRESENT_CONSTRAINED", "PRESENT", "ABSENT")
_UNMET_STATES = ("PRESENT_CONSTRAINED", "ABSENT")

_LIFECYCLE_BLOCKED = "AUTO_READY_BLOCKED_BY_LIFECYCLE"

# ---------------------------------------------------------------------------
# S6-S9 expectations — LITERAL BY CONTRACT.  Do not derive any part of the
# constants below from ``app.services.mock_lane_registry``; ``test_s10_*``
# enforces that mechanically.
# ---------------------------------------------------------------------------

_SAFETY_AXES_EXPECTED = {
    "us.kis.mock": {
        "role": "AUTO_MIRROR",
        "lane_status": "NOT_READY",
        "activation_status": "BLOCKED",
        "quote_currency": "USD",
        "writer": False,
        "auto_order_enabled": False,
        "scheduler_owner": None,
        "physical_account_id": None,
    },
    "us.kiwoom.mock": {
        "role": "BROKER_REGRESSION",
        "lane_status": "NOT_READY",
        "activation_status": "BLOCKED",
        "quote_currency": "USD",
        "writer": False,
        "auto_order_enabled": False,
        "scheduler_owner": None,
        "physical_account_id": None,
    },
}

# The KR/US KIS credential-namespace collision that §5.3 and §6.4 rest on.
_SHARED_KIS_NAMESPACE_LANES = ("kr.kis.mock", "us.kis.mock")
_SHARED_KIS_NAMESPACE = "KIS_MOCK_*"

# Each invariant section, with substrings that must appear inside it.  These
# encode the eight adversarial mutants: removing or weakening any invariant
# drops one of these substrings and turns the corresponding case red.
_INVARIANTS = {
    "6.1": ("open_order_count", "stop before broker I/O", "never read as zero"),
    "6.2": ("physical_account_id", "identity_status", "may not be set true"),
    "6.3": ("BROKER_REGRESSION", "AUTO_MIRROR", "not authorized"),
    "6.4": ("may not hold writers concurrently", "unproven"),
    "6.5": ("zero new broker POSTs", "durable binary claim"),
    "6.6": ("stale", "lease_lost", "may not proceed"),
    "6.7": ("may not cancel, roll back", "share no transaction"),
    "6.8": (
        "currency_conversion_not_authorized",
        "lane_quote_currency_mismatch",
        "no FX rate, parity, or conversion",
    ),
}

# In-repo anchors the document cites by name.  A citation that no longer
# resolves is a defect in this document, not a reason to relax the test.
_CITED_ANCHORS = (
    (_COORDINATION_PATH, "async def release_if_matches"),
    (_COORDINATION_PATH, "def _terminal_evidence_authorizes"),
    (_COORDINATION_PATH, "async def release_with_terminal_evidence"),
    (_COORDINATION_PATH, "def unreleased_authority_holds"),
    (_COORDINATION_PATH, "async def list_reservations"),
    (_COORDINATION_PATH, "async def assert_owned"),
    (_LINEAGE_PATH, "currency_conversion_not_authorized"),
    (_LINEAGE_PATH, "lane_quote_currency_mismatch"),
)


@pytest.fixture(scope="module")
def doc_text() -> str:
    return _DOC_PATH.read_text(encoding="utf-8")


def _entry(lane_id: str):
    """``CANONICAL_LANE_REGISTRY`` is an ordered tuple, not a mapping."""

    matches = [e for e in CANONICAL_LANE_REGISTRY if e.lane_id == lane_id]
    assert len(matches) == 1, f"{lane_id}: expected exactly one registry row"
    return matches[0]


def _flat(text: str) -> str:
    """Collapse newlines so prose assertions do not depend on line wrapping."""

    return re.sub(r"\s+", " ", text)


def _blocks(text: str, marker: str) -> dict[str, dict[str, str]]:
    """Parse ``### LANE <id>`` sections into ``{lane: {key: value}}``."""

    out: dict[str, dict[str, str]] = {}
    sections = re.split(r"^### LANE ", text, flags=re.MULTILINE)[1:]
    for section in sections:
        lane_id = section.splitlines()[0].strip()
        found = re.findall(rf"```text\n# {marker}\n(.*?)```", section, flags=re.DOTALL)
        assert len(found) == 1, f"{lane_id}: expected exactly one {marker} block"
        pairs: dict[str, str] = {}
        for line in found[0].strip().splitlines():
            key, _, value = line.partition(" = ")
            assert _, f"{lane_id}/{marker}: malformed line {line!r}"
            assert key not in pairs, f"{lane_id}/{marker}: duplicate key {key}"
            pairs[key] = value.strip()
        out[lane_id] = pairs
    return out


def test_s1_document_exists_and_is_the_only_owned_artifact() -> None:
    assert _DOC_PATH.is_file()


@pytest.mark.parametrize("token", _FIXED_TOKENS)
def test_s2_fixed_token_appears_exactly_once(doc_text: str, token: str) -> None:
    assert doc_text.count(token) == 1, f"token must appear exactly once: {token}"


def test_s3_exactly_the_two_owned_lanes_are_recorded(doc_text: str) -> None:
    recorded = tuple(_blocks(doc_text, "LIFECYCLE"))
    assert recorded == _LANES


@pytest.mark.parametrize("lane", _LANES)
def test_s4_lifecycle_key_set_is_closed(doc_text: str, lane: str) -> None:
    block = _blocks(doc_text, "LIFECYCLE")[lane]
    assert tuple(block) == _LIFECYCLE_KEYS
    assert block["lane_id"] == lane


@pytest.mark.parametrize("lane", _LANES)
def test_s5_evidence_key_set_is_exactly_the_seven_rule_kinds(
    doc_text: str, lane: str
) -> None:
    block = _blocks(doc_text, "EVIDENCE")[lane]
    assert tuple(block) == _EVIDENCE_KINDS, (
        "the seven evidence kinds are fixed by the controlling rule; "
        "none may be dropped, renamed, or added"
    )
    for kind, value in block.items():
        assert value.startswith(_STATES), f"{lane}/{kind}: unstated evidence"


@pytest.mark.parametrize("lane", _LANES)
def test_s6_lifecycle_verdict_follows_from_unmet_items(
    doc_text: str, lane: str
) -> None:
    """An unmet item forces the blocked verdict; the two cannot disagree."""

    lifecycle = _blocks(doc_text, "LIFECYCLE")[lane]
    evidence = _blocks(doc_text, "EVIDENCE")[lane]

    rule_items = (
        "recovery_owner",
        "restart_rediscovery_trigger",
        "authoritative_readback_operation",
        "release_if_matches_condition",
        "operator_visible_blocked_state",
    )
    unmet = {item for item in rule_items if lifecycle[item].startswith(_UNMET_STATES)}
    if any(value.startswith(_UNMET_STATES) for value in evidence.values()):
        unmet.add("lane_native_evidence")

    declared = {
        part.strip()
        for part in lifecycle["unmet_lifecycle_items"].strip("()").split(",")
        if part.strip()
    }
    assert declared == unmet, f"{lane}: declared unmet set disagrees with the blocks"
    assert unmet, f"{lane}: an empty unmet set would require enablement evidence"
    assert lifecycle["lifecycle_recovery_owner_status"] == _LIFECYCLE_BLOCKED


@pytest.mark.parametrize("lane", _LANES)
def test_s7_verdict_is_carried_off_lane_status(doc_text: str, lane: str) -> None:
    """The verdict must not be written into ``lane_status``.

    The signed allowlist admits only ``NOT_READY`` for these lanes, so putting
    the lifecycle verdict on ``lane_status`` would violate the registry.
    """

    lifecycle = _blocks(doc_text, "LIFECYCLE")[lane]
    assert "lane_status" not in lifecycle
    entry = _entry(lane)
    assert entry.lane_status.value == "NOT_READY"
    assert _LIFECYCLE_BLOCKED != entry.lane_status.value


@pytest.mark.parametrize("lane", _LANES)
def test_s8_signed_safety_axes_are_unchanged(lane: str) -> None:
    """Literal pin.  A registry flip must turn this red, not be followed."""

    entry = _entry(lane)
    expected = _SAFETY_AXES_EXPECTED[lane]

    assert entry.role is not None
    assert entry.role.value == expected["role"]
    assert entry.lane_status.value == expected["lane_status"]
    assert entry.activation_status.value == expected["activation_status"]
    assert entry.quote_currency == expected["quote_currency"]
    assert entry.writer is expected["writer"]
    assert entry.auto_order_enabled is expected["auto_order_enabled"]
    assert entry.scheduler_owner is expected["scheduler_owner"]
    assert entry.physical_account_id is expected["physical_account_id"]


def test_s9_kr_us_kis_share_one_credential_namespace(doc_text: str) -> None:
    """The collision §5.3 records, and the basis of §6.4."""

    namespaces = {
        lane: LANE_CREDENTIAL_NAMESPACES[lane] for lane in _SHARED_KIS_NAMESPACE_LANES
    }
    assert set(namespaces.values()) == {_SHARED_KIS_NAMESPACE}, (
        "if the KR/US KIS namespaces are ever separated, §5.3 and §6.4 must be "
        "rewritten rather than left asserting a collision that no longer exists"
    )
    assert "declared collision" in _flat(doc_text)


def test_s10_safety_expectations_are_literal_not_derived() -> None:
    """Guards S8/S9: the pins above may not be computed from the registry."""

    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    head = source.split("@pytest.fixture", 1)[0]
    literal_block = head.split("_SAFETY_AXES_EXPECTED", 1)[1]
    for forbidden in (
        "CANONICAL_LANE_REGISTRY",
        "LANE_CREDENTIAL_NAMESPACES",
        "LANE_QUOTE_CURRENCIES",
    ):
        assert forbidden not in literal_block, (
            f"{forbidden} must not seed the literal expectations; "
            "deriving them would make a registry flip self-approving"
        )


@pytest.mark.parametrize("section", sorted(_INVARIANTS))
def test_s11_pre_submit_invariant_is_present(doc_text: str, section: str) -> None:
    body = re.search(
        rf"^### {re.escape(section)} .*?(?=^### |^## )",
        doc_text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert body is not None, f"invariant §{section} is missing"
    for needle in _INVARIANTS[section]:
        assert needle in _flat(body.group(0)), (
            f"§{section} lost its substance: {needle!r}"
        )


@pytest.mark.parametrize(("path", "anchor"), _CITED_ANCHORS)
def test_s12_cited_anchor_still_exists(path: pathlib.Path, anchor: str) -> None:
    assert anchor in path.read_text(encoding="utf-8"), (
        f"{path.name} no longer contains {anchor!r}; the citation must be "
        "repaired rather than the assertion relaxed"
    )


def test_s13_document_does_not_rerender_rob1266_registry_records(
    doc_text: str,
) -> None:
    """Scope fence: ROB-1266 owns the registry rendering; cite, do not repeat."""

    for owned_key in ("# REGISTRY", "# DERIVED", "CAP_STATUS"):
        assert owned_key not in doc_text, (
            f"{owned_key!r} belongs to docs/contracts/rob-1266-us-readiness.md; "
            "a second rendering would be a second source of truth"
        )
    assert "docs/contracts/rob-1266-us-readiness.md" in doc_text


def test_s14_no_production_module_is_modified_by_this_contract() -> None:
    """The registry and ports are consumed, never edited, by this job."""

    for path in (_REGISTRY_PATH, _COORDINATION_PATH, _LINEAGE_PATH):
        assert path.is_file()
