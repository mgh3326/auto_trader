"""ROB-1268 J5C — US KIS / Kiwoom lifecycle-recovery contract checker.

Offline only: this module reads repository artifacts — the contract document,
the signed lane registry, and the merged J2B/J3A port sources — and compares
them.  It opens no socket, no database connection, and no file outside the
repository, and it modifies nothing.

**Every expectation here is derived, never hand-written.**  That is the
correction this module exists to hold:

* ROB-1266 §8 owns the signed registry rendering for these two lanes, and the
  registry owns the values themselves.  This module keeps **no** literal copy of
  a lane's `role`, `lane_status`, `activation_status`, `quote_currency`,
  `identity_status`, or `credential_namespace`.  ``test_g5_*`` goes further and
  proves the *document* keeps no copy either, using values pulled live from the
  registry as the forbidden set — so a third independent record cannot appear
  without turning this suite red.
* The lifecycle verdict in §4 of the document is checked against **ground truth
  derived from merged code**, not against the document's own neighbouring
  fields.  Declaring an item ``PRESENT`` while the registry records its binding
  as missing is therefore a failure, and cannot be laundered by also editing
  ``unmet_lifecycle_items``.  ``test_g7_*``/``test_g8_*`` enforce that.

The literal-pin duty belongs to J5A's checker
(``tests/services/mock_integration/test_rob1266_us_readiness_contract.py``) and
to ``assert_registry_startup`` in the registry itself, which rejects an unsafe
row at import time.  Duplicating it here would be the very second copy this
contract forbids.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.services.brokers.capabilities import BROKER_CAPABILITIES, Broker, Market
from app.services.mock_lane_registry import (
    CANONICAL_LANE_REGISTRY,
    LANE_ALLOWED_HOSTS,
    LANE_CREDENTIAL_NAMESPACES,
    MissingBinding,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DOC_PATH = _REPO_ROOT / "docs/contracts/rob-1268-us-kis-kiwoom-readiness.md"
_REGISTRY_PATH = _REPO_ROOT / "app/services/mock_lane_registry.py"
_COORDINATION_PATH = _REPO_ROOT / "app/services/mock_integration/coordination.py"
_LINEAGE_PATH = _REPO_ROOT / "app/services/mock_integration/lineage.py"
_KIS_OVERSEAS_PATH = _REPO_ROOT / "app/services/brokers/kis/overseas_orders.py"
_MODELS_DIR = _REPO_ROOT / "app/models"

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

# The five rule items carried on the LIFECYCLE block.  The sixth rule item —
# lane-native evidence — lives in the EVIDENCE block and joins the unmet set
# under the name below.
_RULE_ITEMS = (
    "recovery_owner",
    "restart_rediscovery_trigger",
    "authoritative_readback_operation",
    "release_if_matches_condition",
    "operator_visible_blocked_state",
)
_EVIDENCE_ITEM = "lane_native_evidence"

_STATES = ("PRESENT_CONSTRAINED", "PRESENT", "ABSENT")
_UNMET_STATES = ("PRESENT_CONSTRAINED", "ABSENT")
_LIFECYCLE_BLOCKED = "AUTO_READY_BLOCKED_BY_LIFECYCLE"

# Axes ROB-1266 §8 renders.  Their values are pulled from the registry at run
# time; none of them is written down here.
_ROB1266_OWNED_AXES = (
    "role",
    "lane_status",
    "activation_status",
    "quote_currency",
    "identity_status",
    "credential_namespace",
)
# Axes whose value is a pure identity token, so any bare occurrence in the
# document is a restatement regardless of surrounding prose.
_IDENTITY_AXES = ("role", "lane_status", "credential_namespace")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


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


def _rendered(value: object) -> str | None:
    """The registry value as it would appear if someone restated it."""

    if value is None:
        return None
    inner = getattr(value, "value", value)
    return str(inner)


def _axis_value(lane_id: str, axis: str) -> str | None:
    if axis == "credential_namespace":
        return _rendered(LANE_CREDENTIAL_NAMESPACES[lane_id])
    return _rendered(getattr(_entry(lane_id), axis))


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


def _state_of(value: str) -> str:
    for state in _STATES:  # PRESENT_CONSTRAINED first: it prefixes PRESENT
        if value.startswith(state):
            return state
    raise AssertionError(f"unstated value: {value!r}")


# ---------------------------------------------------------------------------
# ground truth — derived from merged code, never from the document
# ---------------------------------------------------------------------------


def _readback_ground_truth(lane_id: str) -> str:
    """Is an authoritative broker readback usable by this lane, unaided?"""

    if lane_id == "us.kiwoom.mock":
        # The readback endpoints exist, but the capability registry does not
        # declare this broker for US equity at all.
        declared = Market.US_EQUITY in BROKER_CAPABILITIES[Broker.KIWOOM].markets
        return "PRESENT" if declared else "PRESENT_CONSTRAINED"
    if lane_id == "us.kis.mock":
        # The daily-order inquiry has a mock TR, but open-order truth — the
        # pending-orders inquiry — refuses the mock lane outright.
        refuses_mock = "available in mock mode" in _flat(
            _KIS_OVERSEAS_PATH.read_text(encoding="utf-8")
        )
        return "PRESENT_CONSTRAINED" if refuses_mock else "PRESENT"
    raise AssertionError(f"unowned lane: {lane_id}")


def _evidence_ground_truth(lane_id: str) -> str:
    """Can a lane-native evidence table attribute a row to *this* lane?"""

    models = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(_MODELS_DIR.glob("*.py"))
    )
    if lane_id == "us.kiwoom.mock":
        tablenames = re.findall(r'__tablename__\s*=\s*"([^"]+)"', models)
        has_ledger = any("kiwoom" in name and "ledger" in name for name in tablenames)
        return "PRESENT" if has_ledger else "ABSENT"
    if lane_id == "us.kis.mock":
        # The sole KIS mock ledger is CHECK-pinned to one account_mode and
        # carries no market/venue/lane discriminator, so a row cannot be
        # attributed to the US lane rather than the KR one.
        block = models.split('__tablename__ = "kis_mock_order_ledger"', 1)
        assert len(block) == 2, "kis_mock_order_ledger model not found"
        body = block[1].split("__tablename__", 1)[0]
        discriminated = bool(
            re.search(r"^\s+(lane|lane_id|market|venue)\s*:", body, flags=re.MULTILINE)
        )
        return "PRESENT" if discriminated else "ABSENT"
    raise AssertionError(f"unowned lane: {lane_id}")


def _lifecycle_ground_truth(lane_id: str) -> dict[str, str]:
    """The state each rule item must be declared with, derived from code."""

    entry = _entry(lane_id)
    coordination = _COORDINATION_PATH.read_text(encoding="utf-8")

    # The registry itself records whether this lane has a bound owner.
    owner_present = MissingBinding.OWNER not in entry.missing_bindings
    truth = {"recovery_owner": "PRESENT" if owner_present else "ABSENT"}

    # A rediscovery trigger is a thing an owner owns.  With no owner there is
    # nobody to own one, and J3A ships no recovery API to stand in.
    truth["restart_rediscovery_trigger"] = "PRESENT" if owner_present else "ABSENT"

    truth["authoritative_readback_operation"] = _readback_ground_truth(lane_id)

    release_anchors = (
        "async def release_if_matches",
        "def _terminal_evidence_authorizes",
        "async def release_with_terminal_evidence",
    )
    truth["release_if_matches_condition"] = (
        "PRESENT" if all(a in coordination for a in release_anchors) else "ABSENT"
    )
    truth["operator_visible_blocked_state"] = (
        "PRESENT" if "def unreleased_authority_holds" in coordination else "ABSENT"
    )
    return truth


def _unmet_ground_truth(lane_id: str) -> set[str]:
    unmet = {
        item
        for item, state in _lifecycle_ground_truth(lane_id).items()
        if state in _UNMET_STATES
    }
    if _evidence_ground_truth(lane_id) in _UNMET_STATES:
        unmet.add(_EVIDENCE_ITEM)
    return unmet


# ---------------------------------------------------------------------------
# G1-G4 — document shape
# ---------------------------------------------------------------------------


def test_g1_document_exists() -> None:
    assert _DOC_PATH.is_file()


@pytest.mark.parametrize("token", _FIXED_TOKENS)
def test_g2_fixed_token_appears_exactly_once(doc_text: str, token: str) -> None:
    assert doc_text.count(token) == 1, f"token must appear exactly once: {token}"


def test_g3_exactly_the_two_owned_lanes_are_recorded(doc_text: str) -> None:
    assert tuple(_blocks(doc_text, "LIFECYCLE")) == _LANES


@pytest.mark.parametrize("lane", _LANES)
def test_g4_block_key_sets_are_closed(doc_text: str, lane: str) -> None:
    lifecycle = _blocks(doc_text, "LIFECYCLE")[lane]
    assert tuple(lifecycle) == _LIFECYCLE_KEYS
    assert lifecycle["lane_id"] == lane

    evidence = _blocks(doc_text, "EVIDENCE")[lane]
    assert tuple(evidence) == _EVIDENCE_KINDS, (
        "the seven evidence kinds are fixed by the controlling rule; "
        "none may be dropped, renamed, or added"
    )
    for kind, value in evidence.items():
        assert _state_of(value), f"{lane}/{kind}: unstated evidence"


# ---------------------------------------------------------------------------
# G5-G6 — no second rendering of ROB-1266 / registry facts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", _LANES)
@pytest.mark.parametrize("axis", _ROB1266_OWNED_AXES)
def test_g5_document_does_not_restate_a_registry_axis(
    doc_text: str, lane: str, axis: str
) -> None:
    """The forbidden strings are pulled from the registry, not written here.

    ROB-1266 §8 renders these axes for these lanes.  A second rendering here
    would make a third independent record of a signed fact, which the contract
    forbids; cite the path and section instead.
    """

    value = _axis_value(lane, axis)
    if value is None:
        pytest.skip(f"{lane}.{axis} is absent in the registry; nothing to restate")

    flat = _flat(doc_text)
    assert not re.search(rf"{re.escape(axis)}\s*=\s*{re.escape(value)}", flat), (
        f"{lane}: document restates {axis} = {value}; cite ROB-1266 §8 instead"
    )
    if axis in _IDENTITY_AXES:
        assert not re.search(rf"(?<![\w.]){re.escape(value)}(?![\w.])", flat), (
            f"{lane}: document contains the bare registry value {value!r} for "
            f"{axis}; that value is owned by ROB-1266 §8"
        )


def test_g6_checker_keeps_no_literal_copy_of_a_registry_value() -> None:
    """Guards G5: this module may not hand-write what it claims to derive."""

    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    forbidden = {
        value
        for lane in _LANES
        for axis in _ROB1266_OWNED_AXES
        if (value := _axis_value(lane, axis)) is not None
    }
    for value in forbidden:
        assert f'"{value}"' not in source and f"'{value}'" not in source, (
            f"{value!r} is written literally in this checker; derive it from "
            "app.services.mock_lane_registry instead"
        )


def test_g7_kr_and_us_kis_share_one_namespace_and_host() -> None:
    """The relation §5.3 records — asserted, never transcribed."""

    assert (
        LANE_CREDENTIAL_NAMESPACES["kr.kis.mock"]
        == LANE_CREDENTIAL_NAMESPACES["us.kis.mock"]
    ), (
        "if the KR/US KIS namespaces are ever separated, §5.3 and §6.4 must be "
        "rewritten rather than left asserting a collision that no longer exists"
    )
    assert LANE_ALLOWED_HOSTS["kr.kis.mock"] == LANE_ALLOWED_HOSTS["us.kis.mock"]
    assert "declared collision" in _flat(_DOC_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# G8-G10 — the lifecycle verdict must match derived ground truth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", _LANES)
@pytest.mark.parametrize("item", _RULE_ITEMS)
def test_g8_declared_item_state_matches_ground_truth(
    doc_text: str, lane: str, item: str
) -> None:
    """A declaration is checked against merged code, not against its neighbours.

    This is what stops a fabricated ``PRESENT``: the registry records whether
    the binding exists, so claiming otherwise fails here even if
    ``unmet_lifecycle_items`` is edited to agree with the claim.
    """

    declared = _state_of(_blocks(doc_text, "LIFECYCLE")[lane][item])
    expected = _lifecycle_ground_truth(lane)[item]
    assert declared == expected, (
        f"{lane}/{item}: document declares {declared} but merged code shows "
        f"{expected}; fix the document rather than the expectation"
    )


@pytest.mark.parametrize("lane", _LANES)
def test_g9_declared_evidence_matches_ground_truth(doc_text: str, lane: str) -> None:
    expected = _evidence_ground_truth(lane)
    for kind, value in _blocks(doc_text, "EVIDENCE")[lane].items():
        assert _state_of(value) == expected, (
            f"{lane}/{kind}: document declares {_state_of(value)} but no "
            f"lane-attributable evidence table exists ({expected})"
        )


@pytest.mark.parametrize("lane", _LANES)
def test_g10_unmet_set_and_verdict_follow_ground_truth(
    doc_text: str, lane: str
) -> None:
    lifecycle = _blocks(doc_text, "LIFECYCLE")[lane]
    declared = {
        part.strip()
        for part in lifecycle["unmet_lifecycle_items"].strip("()").split(",")
        if part.strip()
    }
    expected = _unmet_ground_truth(lane)
    assert declared == expected, (
        f"{lane}: declared unmet set {sorted(declared)} disagrees with ground "
        f"truth {sorted(expected)}"
    )
    assert expected, f"{lane}: an empty unmet set would require enablement evidence"
    assert lifecycle["lifecycle_recovery_owner_status"] == _LIFECYCLE_BLOCKED


@pytest.mark.parametrize("lane", _LANES)
def test_g11_verdict_is_carried_off_lane_status(doc_text: str, lane: str) -> None:
    """The verdict must not be written into ``lane_status``.

    The signed allowlist admits a single status for these lanes, so putting the
    lifecycle verdict there would violate the registry.  The admitted value is
    read from the registry rather than repeated.
    """

    assert "lane_status" not in _blocks(doc_text, "LIFECYCLE")[lane]
    assert _entry(lane).lane_status.value != _LIFECYCLE_BLOCKED


# ---------------------------------------------------------------------------
# G12-G14 — invariants, citations, scope
# ---------------------------------------------------------------------------

_INVARIANTS = {
    "6.1": ("open_order_count", "stop before broker I/O", "never read as zero"),
    "6.2": ("may not be set true", "Masked physical"),
    "6.3": ("promoting the lane to an automatic mirroring role", "not authorized"),
    "6.4": ("may not hold writers concurrently", "unproven"),
    "6.5": ("zero new broker POSTs", "durable binary claim"),
    "6.6": ("stale", "lease_lost", "may not proceed"),
    "6.7": ("may not cancel, roll back", "share no transaction"),
    "6.8": (
        "currency_conversion_not_authorized",
        "lane_quote_currency_mismatch",
        "no FX rate, parity, or conversion may be applied",
    ),
}

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


@pytest.mark.parametrize("section", sorted(_INVARIANTS))
def test_g12_pre_submit_invariant_is_present(doc_text: str, section: str) -> None:
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
def test_g13_cited_anchor_still_exists(path: pathlib.Path, anchor: str) -> None:
    assert anchor in path.read_text(encoding="utf-8"), (
        f"{path.name} no longer contains {anchor!r}; the citation must be "
        "repaired rather than the assertion relaxed"
    )


def test_g14_document_cites_rob1266_and_touches_no_production_module(
    doc_text: str,
) -> None:
    for owned_marker in ("# REGISTRY", "# DERIVED", "CAP_STATUS"):
        assert owned_marker not in doc_text, (
            f"{owned_marker!r} belongs to docs/contracts/rob-1266-us-readiness.md"
        )
    assert "docs/contracts/rob-1266-us-readiness.md" in doc_text
    for path in (_REGISTRY_PATH, _COORDINATION_PATH, _LINEAGE_PATH):
        assert path.is_file()
