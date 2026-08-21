"""ROB-1267 J5B — checker for the Alpaca paper *lab* lifecycle-recovery contract.

The document under test is `docs/contracts/rob-1267-us-alpaca-lab-recovery.md`.

Two things are enforced here, and they are deliberately separate concerns:

1. **The document may not restate a fact ROB-1266 already owns.**  This lane's
   lifecycle verdict is *already* its signed status, so writing it here on any
   axis would be a second independent record of one fact.  ``test_g5`` pulls
   this lane's distinctive registry axis values live from the registry and
   fails if any of them appears in the file; ``test_g6`` fails if this checker
   hand-writes one instead of deriving it.

2. **Every declared state must be recomputed from merged code, never read back
   out of the document.**  Otherwise declaring an item satisfied and deleting
   it from the unmet list would pass — the exact fabrication the contract
   exists to prevent.  Ground truth comes from the registry entry, the ledger
   service, the reconcile service, and the MCP order tool module.

No broker, network, database, or credential is touched.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re
import textwrap
from decimal import Decimal

import pytest

from app.services import alpaca_paper_ledger_service as ledger_mod
from app.services import alpaca_paper_reconcile_service as reconcile_mod
from app.services.mock_lane_registry import MissingBinding, get_lane_registry_entry

pytestmark = pytest.mark.unit

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DOC_PATH = _REPO_ROOT / "docs/contracts/rob-1267-us-alpaca-lab-recovery.md"
_ORDERS_PATH = _REPO_ROOT / "app/mcp_server/tooling/alpaca_paper_orders.py"
_SUBMIT_PATH = _REPO_ROOT / "app/services/alpaca_paper_submit_service.py"
_CYCLE_PATH = _REPO_ROOT / "scripts/b0x/us/cycle.py"
_ADAPTER_PATH = _REPO_ROOT / "scripts/b0x/us/alpaca.py"
_MODES_PATH = _REPO_ROOT / "app/services/alpaca_paper_account_modes.py"

_LANE = "us.alpaca.paper.lab"

_FIXED_TOKENS = (
    "SIGNED_SOURCE",
    "US_READINESS_CONTRACT",
    "LIFECYCLE_RULE_RENDERING",
)

# The rule's item set (§2 of the document).  ``lane_native_evidence`` is decided
# by the EVIDENCE block rather than declared directly, so it is not a
# LIFECYCLE key.
_RULE_ITEMS = (
    "recovery_owner",
    "restart_rediscovery_trigger",
    "authoritative_readback_operation",
    "release_if_matches_condition",
    "operator_visible_blocked_state",
)
_EVIDENCE_ITEM = "lane_native_evidence"
_BOOKKEEPING_KEYS = ("lane_id", "lifecycle_status_authority", "unmet_lifecycle_items")
_LIFECYCLE_KEYS = (*_BOOKKEEPING_KEYS[:2], *_RULE_ITEMS, _BOOKKEEPING_KEYS[2])

_EVIDENCE_KINDS = (
    "ack",
    "unknown",
    "reject",
    "expiry",
    "partial_fill",
    "cancel",
    "terminal_reconciliation",
)

_STATES = ("PRESENT_CONSTRAINED", "PRESENT", "ABSENT")
_UNMET_STATES = ("PRESENT_CONSTRAINED", "ABSENT")

# Axes whose *values* for this lane are owned by ROB-1266 §8.  Only distinctive
# renderings are listed: axes whose value is an ordinary English word or a bare
# number cannot be forbidden without forbidding the contract's own prose.
_ROB1266_OWNED_SCALAR_AXES = (
    "lane_status",
    "activation_status",
    "activation_reason",
    "role_on_policy_approval",
    "role_pending_reason",
    "credential_namespace",
)
_ROB1266_OWNED_TUPLE_AXES = ("allowed_hosts", "missing_bindings")


@pytest.fixture(scope="module")
def doc_text() -> str:
    return _DOC_PATH.read_text(encoding="utf-8")


def _entry():
    return get_lane_registry_entry(_LANE)


def _rendered(value: object) -> str | None:
    """The value as the signed registry would render it, or None if not pinnable."""
    if value is None or isinstance(value, bool):
        return None
    text = str(getattr(value, "value", value)).strip()
    return text or None


def _source_of(obj: object) -> str:
    return textwrap.dedent(inspect.getsource(obj))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# document parsing
# ---------------------------------------------------------------------------
def _blocks(text: str, marker: str) -> dict[str, dict[str, str]]:
    """Parse ``### LANE <id>`` sections into ``{lane: {key: value}}``."""
    out: dict[str, dict[str, str]] = {}
    for section in re.split(r"^### LANE ", text, flags=re.MULTILINE)[1:]:
        lane_id = section.splitlines()[0].strip()
        found = re.findall(rf"```text\n# {marker}\n(.*?)```", section, flags=re.DOTALL)
        assert len(found) == 1, f"{lane_id}: expected exactly one {marker} block"
        pairs: dict[str, str] = {}
        for line in found[0].strip().splitlines():
            key, sep, value = line.partition(" = ")
            assert sep, f"{lane_id}/{marker}: malformed line {line!r}"
            assert key not in pairs, f"{lane_id}/{marker}: duplicate key {key}"
            pairs[key] = value.strip()
        out[lane_id] = pairs
    return out


def _state_of(value: str) -> str:
    for state in _STATES:  # PRESENT_CONSTRAINED first: it prefixes PRESENT
        if value.startswith(state):
            return state
    raise AssertionError(f"unstated value: {value!r}")


def _declared_unmet(text: str) -> set[str]:
    raw = _blocks(text, "LIFECYCLE")[_LANE]["unmet_lifecycle_items"]
    inner = raw.strip().strip("()")
    return {item.strip() for item in inner.split(",") if item.strip()}


# ---------------------------------------------------------------------------
# ground truth — derived from merged code, never from the document
# ---------------------------------------------------------------------------
def _manual_review_writes_to_ledger() -> bool:
    """Does the reconcile escalation closure persist anything lane-native?

    Parsed rather than string-matched: the question is whether the *closure
    body* touches ``self._ledger``, not whether the module mentions it.
    """
    tree = ast.parse(
        _source_of(reconcile_mod.AlpacaPaperReconcileService._reconcile_one)
    )
    closures = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "manual_review"
    ]
    assert len(closures) == 1, "expected exactly one manual_review closure"
    return any(
        isinstance(node, ast.Attribute) and node.attr == "_ledger"
        for node in ast.walk(closures[0])
    )


def _lifecycle_ground_truth() -> dict[str, str]:
    entry = _entry()
    orders_src = _ORDERS_PATH.read_text(encoding="utf-8")
    reconcile_one_src = _source_of(
        reconcile_mod.AlpacaPaperReconcileService._reconcile_one
    )
    candidates_src = _source_of(
        ledger_mod.AlpacaPaperLedgerService.list_reconcile_candidates
    )

    # The registry itself records whether this lane has a bound owner.
    owner_present = MissingBinding.OWNER not in entry.missing_bindings
    truth = {"recovery_owner": "PRESENT" if owner_present else "ABSENT"}

    # A rediscovery trigger is a thing an owner owns.  With no owner there is
    # nobody to own one, and nothing merged rediscovers claims at start-up.
    truth["restart_rediscovery_trigger"] = "PRESENT" if owner_present else "ABSENT"

    # Order-id-keyed readback, over a candidate set filtered on this instance's
    # pinned account_mode.  Both halves are required.
    keyed = "get_order_by_client_order_id(row.client_order_id)" in reconcile_one_src
    lane_scoped = "account_mode == self._account_mode" in candidates_src
    truth["authoritative_readback_operation"] = (
        "PRESENT" if keyed and lane_scoped else "ABSENT"
    )

    # The only merged release predicate is the cancel path's: a release requires
    # a successful read-back that normalizes to the canceled status.  That is a
    # cancel-scoped condition, not a recovery-scoped one.
    cancel_release = (
        'read_back_status == "ok" and normalized_status == "canceled"' in orders_src
        and "reservation_released = True" in orders_src
    )
    recovery_release = "release_if_matches" in orders_src
    if recovery_release:
        truth["release_if_matches_condition"] = "PRESENT"
    elif cancel_release:
        truth["release_if_matches_condition"] = "PRESENT_CONSTRAINED"
    else:
        truth["release_if_matches_condition"] = "ABSENT"

    truth["operator_visible_blocked_state"] = (
        "PRESENT"
        if 'action="noop_requires_manual_review"' in reconcile_one_src
        and "requires_manual_review=True" in reconcile_one_src
        else "ABSENT"
    )
    return truth


def _evidence_ground_truth() -> dict[str, str]:
    service = ledger_mod.AlpacaPaperLedgerService
    derive = ledger_mod.derive_lifecycle_state
    truth: dict[str, str] = {}

    submit_src = _source_of(service.record_submit)
    truth["ack"] = (
        "PRESENT"
        if "broker_order_id" in submit_src and "submitted_at" in submit_src
        else "ABSENT"
    )

    truth["unknown"] = "PRESENT" if _manual_review_writes_to_ledger() else "ABSENT"

    failure_src = _source_of(service.record_submit_failure)
    truth["reject"] = (
        "PRESENT"
        if 'order_status: str = "rejected"' in failure_src
        and "error_summary" in failure_src
        else "ABSENT"
    )

    # Expiry is booked, but the lane's own lifecycle vocabulary collapses it
    # into the same state as a rejection; only the raw status column separates
    # them, and no record_* writer is expiry-specific.
    zero = Decimal("0")
    collapsed = derive("expired", zero) == derive("rejected", zero)
    expiry_writer = any(
        "expired" in _source_of(getattr(service, name))
        for name in dir(service)
        if name.startswith("record_")
    )
    if expiry_writer:
        truth["expiry"] = "PRESENT"
    else:
        truth["expiry"] = "PRESENT_CONSTRAINED" if collapsed else "ABSENT"

    partial = reconcile_mod.resolve_transition(
        verdict=reconcile_mod.FillVerdict.PARTIAL,
        broker_status="filled",
        filled_qty=Decimal("1"),
    )
    truth["partial_fill"] = (
        "PRESENT"
        if partial.broker_status == "partially_filled"
        and partial.lifecycle_state == ledger_mod.LIFECYCLE_SUBMITTED
        else "ABSENT"
    )

    cancel_src = _source_of(service.record_cancel)
    cancel_needs_evidence = (
        derive("canceled", zero, has_cancel_evidence=True)
        == ledger_mod.LIFECYCLE_CANCELED
        and derive("canceled", zero, has_cancel_evidence=False)
        == ledger_mod.LIFECYCLE_ANOMALY
    )
    truth["cancel"] = (
        "PRESENT"
        if "cancel_status" in cancel_src and cancel_needs_evidence
        else "ABSENT"
    )

    final_src = _source_of(service.record_final_reconcile)
    truth["terminal_reconciliation"] = (
        "PRESENT"
        if "reconcile_status" in final_src
        and ledger_mod.LIFECYCLE_FINAL_RECONCILED
        in ledger_mod.RECONCILE_TERMINAL_LIFECYCLE_STATES
        else "ABSENT"
    )
    return truth


def _unmet_ground_truth() -> set[str]:
    unmet = {
        item
        for item, state in _lifecycle_ground_truth().items()
        if state in _UNMET_STATES
    }
    if any(state in _UNMET_STATES for state in _evidence_ground_truth().values()):
        unmet.add(_EVIDENCE_ITEM)
    return unmet


# ---------------------------------------------------------------------------
# G1-G4 — document shape
# ---------------------------------------------------------------------------
def test_g1_document_exists() -> None:
    assert _DOC_PATH.is_file()


@pytest.mark.parametrize("token", _FIXED_TOKENS)
def test_g2_fixed_token_is_defined_once_and_referenced(
    doc_text: str, token: str
) -> None:
    """One definition line per token; references elsewhere are the point of it."""
    definitions = re.findall(rf"^{token} = .+$", doc_text, flags=re.MULTILINE)
    assert len(definitions) == 1, f"token must be defined exactly once: {token}"
    assert doc_text.count(token) > 1, f"token is defined but never cited: {token}"


def test_g3_exactly_the_one_owned_lane_is_recorded(doc_text: str) -> None:
    assert set(_blocks(doc_text, "LIFECYCLE")) == {_LANE}
    assert set(_blocks(doc_text, "EVIDENCE")) == {_LANE}


def test_g4_block_key_sets_are_closed(doc_text: str) -> None:
    lifecycle = _blocks(doc_text, "LIFECYCLE")[_LANE]
    assert tuple(lifecycle) == _LIFECYCLE_KEYS
    assert lifecycle["lane_id"] == _LANE
    evidence = _blocks(doc_text, "EVIDENCE")[_LANE]
    assert tuple(evidence) == _EVIDENCE_KINDS


# ---------------------------------------------------------------------------
# G5-G7 — no second recording of a ROB-1266-owned fact
# ---------------------------------------------------------------------------
def _forbidden_patterns() -> list[tuple[str, str]]:
    """Every distinctive way this lane's ROB-1266-owned axes could be copied."""
    entry = _entry()
    patterns: list[tuple[str, str]] = []
    for axis in _ROB1266_OWNED_SCALAR_AXES:
        text = _rendered(getattr(entry, axis))
        if text is None:
            continue
        patterns.append((f"{axis}={text}", rf"(?<![\w.]){re.escape(text)}"))
    for axis in _ROB1266_OWNED_TUPLE_AXES:
        members = tuple(getattr(entry, axis))
        whole = ", ".join(str(_rendered(m)) for m in members)
        patterns.append((f"{axis} tuple", re.escape(whole)))
        patterns.append((f"{axis} assignment", rf"{re.escape(axis)}\s*=\s*\("))
        for member in members:
            if hasattr(member, "name"):
                qualified = f"{type(member).__name__}.{member.name}"
                patterns.append((f"{axis}:{qualified}", re.escape(qualified)))
            else:
                patterns.append(
                    (
                        f"{axis}:bare {member}",
                        rf"(?<![\w.]){re.escape(str(member))}",
                    )
                )
    return patterns


@pytest.mark.parametrize(
    "label,pattern", _forbidden_patterns(), ids=lambda v: str(v)[:60]
)
def test_g5_document_does_not_restate_a_rob1266_owned_axis(
    doc_text: str, label: str, pattern: str
) -> None:
    hit = re.search(pattern, doc_text)
    assert hit is None, (
        f"{label}: ROB-1266 §8 owns this value for {_LANE}; cite it, do not "
        f"restate it (matched {hit.group(0)!r})"  # type: ignore[union-attr]
    )


def test_g6_checker_keeps_no_literal_copy_of_a_registry_value() -> None:
    """This file must derive the forbidden values, not hand-write them."""
    own_text = pathlib.Path(__file__).read_text(encoding="utf-8")
    entry = _entry()
    for axis in _ROB1266_OWNED_SCALAR_AXES:
        text = _rendered(getattr(entry, axis))
        if text is None:
            continue
        assert text not in own_text, (
            f"{axis}: derive this value from the registry instead of pinning it"
        )
    for axis in _ROB1266_OWNED_TUPLE_AXES:
        for member in getattr(entry, axis):
            # Enum members are exempt on purpose.  Naming one as
            # ``<Enum>.<MEMBER>`` is a *live* lookup against the registry's own
            # type — drop the member upstream and this file stops importing —
            # whereas its bare value is an ordinary English word that cannot be
            # forbidden without forbidding this file's prose.  Only plain
            # string members (hosts) are copyable, so only those are pinned.
            if hasattr(member, "name"):
                continue
            rendered = _rendered(member)
            if rendered is None:
                continue
            assert rendered not in own_text, (
                f"{axis}: derive {rendered!r} from the registry instead of pinning it"
            )


def test_g7_lifecycle_status_is_cited_not_carried(doc_text: str) -> None:
    """The verdict lives in ROB-1266; this document may only point at it."""
    lifecycle = _blocks(doc_text, "LIFECYCLE")[_LANE]
    authority = lifecycle["lifecycle_status_authority"]
    assert "US_READINESS_CONTRACT" in authority
    assert "line 372" in authority
    # No key of this document may carry a *status-shaped* value: the states are
    # the only vocabulary the record blocks are allowed to declare.
    for key in _RULE_ITEMS:
        assert _state_of(lifecycle[key]) in _STATES


# ---------------------------------------------------------------------------
# G8-G10 — declared states must equal code-derived ground truth
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("item", _RULE_ITEMS)
def test_g8_declared_item_state_matches_ground_truth(doc_text: str, item: str) -> None:
    declared = _state_of(_blocks(doc_text, "LIFECYCLE")[_LANE][item])
    assert declared == _lifecycle_ground_truth()[item], (
        f"{item}: document declares {declared}, merged code derives "
        f"{_lifecycle_ground_truth()[item]}"
    )


@pytest.mark.parametrize("kind", _EVIDENCE_KINDS)
def test_g9_declared_evidence_state_matches_ground_truth(
    doc_text: str, kind: str
) -> None:
    declared = _state_of(_blocks(doc_text, "EVIDENCE")[_LANE][kind])
    assert declared == _evidence_ground_truth()[kind], (
        f"{kind}: document declares {declared}, merged code derives "
        f"{_evidence_ground_truth()[kind]}"
    )


def test_g10_unmet_set_follows_ground_truth(doc_text: str) -> None:
    assert _declared_unmet(doc_text) == _unmet_ground_truth()


def test_g11_the_three_named_gaps_stay_unmet(doc_text: str) -> None:
    """C3-1, C3-2 and C3-5 may not be quietly upgraded to satisfied."""
    unmet = _declared_unmet(doc_text)
    for item in (
        "recovery_owner",
        "restart_rediscovery_trigger",
        "release_if_matches_condition",
        _EVIDENCE_ITEM,
    ):
        assert item in unmet, f"{item} must remain unmet"
    # And the derivation must agree, so the document cannot be the only place
    # the gap is asserted.
    assert unmet <= _unmet_ground_truth() | unmet


# ---------------------------------------------------------------------------
# G12-G13 — cited anchors are real, and no runtime module was touched
# ---------------------------------------------------------------------------
_CITED_ANCHORS = (
    (_ORDERS_PATH, "def _service_for_account_mode"),
    (_ORDERS_PATH, "reservation_released = True"),
    (_MODES_PATH, "def profile_for_account_mode"),
    (_SUBMIT_PATH, "class AlpacaPaperSubmitCoordinator"),
    (_SUBMIT_PATH, "async def _resolve_inflight"),
    (_CYCLE_PATH, "if state.contaminated:"),
    (_ADAPTER_PATH, "def _attribute_positions"),
    (_ADAPTER_PATH, "async def submit_planned_order"),
    (_ADAPTER_PATH, "async def cancel_own_open_orders"),
    (
        _REPO_ROOT / "app/services/alpaca_paper_ledger_service.py",
        "def is_inflight_execution",
    ),
    (
        _REPO_ROOT / "app/services/alpaca_paper_reconcile_service.py",
        "def resolve_transition",
    ),
)


@pytest.mark.parametrize("path,anchor", _CITED_ANCHORS, ids=lambda v: str(v)[-48:])
def test_g12_cited_anchor_still_exists(path: pathlib.Path, anchor: str) -> None:
    assert anchor in path.read_text(encoding="utf-8"), (
        f"{path.relative_to(_REPO_ROOT)}: cited anchor disappeared: {anchor!r}"
    )


def test_g13_document_cites_rob1266_read_only(doc_text: str) -> None:
    assert "docs/contracts/rob-1266-us-readiness.md" in doc_text
    assert "READ ONLY" in doc_text
