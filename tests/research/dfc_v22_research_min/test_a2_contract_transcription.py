"""Transcription and network-zero guards for the A2 contract.

The upstream wording lives outside this repository, so nothing here can prove
the repo copy matches it byte-for-byte at test time.  What these tests *can*
pin is everything downstream of that copy: the contract document must quote the
frozen strings unchanged, the frozen literals must be the ones the document
claims, and the package must remain incapable of reaching the network.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from research.dfc_v22_research_min import contract as c
from research.dfc_v22_research_min import nw_verbatim

PACKAGE_DIR = Path(__file__).resolve().parents[3] / "research" / "dfc_v22_research_min"
DOC_PATH = PACKAGE_DIR / "contracts" / "DFC_V22_RESEARCH_MIN.md"

#: Anything that could fetch, list or download.  A2 measurement is a separate
#: job; this package is signed *before* data contact and must stay that way.
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "aiohttp",
        "boto3",
        "botocore",
        "ftplib",
        "http",
        "httpx",
        "requests",
        "s3fs",
        "socket",
        "urllib",
        "websocket",
        "websockets",
    }
)


def _package_sources() -> list[Path]:
    return sorted(PACKAGE_DIR.glob("*.py"))


@pytest.mark.unit
def test_contract_document_quotes_every_clause_verbatim() -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    for key, text in nw_verbatim.VERBATIM_CLAUSES.items():
        assert text in doc, f"{key} is paraphrased or missing in {DOC_PATH.name}"


@pytest.mark.unit
def test_canonical_source_is_pinned() -> None:
    assert nw_verbatim.CANONICAL_SOURCE_SHA256.startswith("df7aee908e50af42")
    assert len(nw_verbatim.CANONICAL_SOURCE_SHA256) == 64
    assert nw_verbatim.CANONICAL_SOURCE_LINE_COUNT == 137
    assert set(nw_verbatim.VERBATIM_CLAUSES) == {
        "NW-F2",
        "NW-F4",
        "NW-F5",
        "NW-F6",
        "OD-26",
    }
    assert "§26차" in nw_verbatim.BINDING_RECORD_AMENDMENT


@pytest.mark.unit
def test_arm_label_domain_is_the_shared_closed_set() -> None:
    """Pin the wire contract A2 shares with the v2.2 registration (PR #1825).

    The two registrations live on disjoint paths and cannot import each other,
    so the only thing that keeps them from drifting is that both pin this exact
    literal.  Changing it here without changing it there is the split this test
    exists to make loud.
    """
    assert c.ARM_LABELS == ("candidate", "control")
    assert (c.ARM_CANDIDATE, c.ARM_CONTROL) == c.ARM_LABELS
    assert all(isinstance(label, str) for label in c.ARM_LABELS)
    assert not any(isinstance(label, bool) for label in c.ARM_LABELS)
    doc = DOC_PATH.read_text(encoding="utf-8")
    for label in c.ARM_LABELS:
        assert f"`{label}`" in doc, f"arm label {label!r} is undocumented"
    assert "research_contracts/dfc_2c_4h_v22.py" in (
        (PACKAGE_DIR / "contract.py").read_text(encoding="utf-8")
    ), "the counterpart declaration must be named at the point of declaration"


@pytest.mark.unit
def test_frozen_literals_appear_in_the_document() -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    for literal in (
        c.CONTRACT_ID,
        c.CORPUS_ID,
        c.CORPUS_ROOT,
        "2021-02-02T00:00:00Z",
        "2021-05-02T00:00:00Z",
        "2023-08-04T00:00:00Z",
    ):
        assert literal in doc, f"{literal!r} is not stated in the contract document"


@pytest.mark.unit
def test_clause_ids_map_to_upstream_clauses() -> None:
    assert set(c.CLAUSE_SOURCES.values()) == set(nw_verbatim.VERBATIM_CLAUSES)
    doc = DOC_PATH.read_text(encoding="utf-8")
    for clause_id in c.CLAUSE_SOURCES:
        assert clause_id in doc, f"clause {clause_id} is undocumented"


#: MUTANT ③ target.  The substitute evidence (A2-C6) is a *different kind* of
#: evidence from the lifecycle authority that does not exist.  Any wording that
#: closes that distance turns a stated absence back into a quiet claim, so the
#: distance is pinned in text as well as in the literals.
FORBIDDEN_EQUIVALENCE_PHRASES = (
    "effectively equivalent",
    "equally authoritative",
    "as authoritative as",
    "authoritative substitute",
    "equivalent to a lifecycle record",
    "equivalent to the authoritative",
    "사실상 동등",
    "사실상 권위",
)

#: Compared against the document with whitespace collapsed, so re-wrapping a
#: paragraph does not fail a test about what the paragraph *says*.
REQUIRED_A2_C6_WORDING = (
    "There is no single authoritative public Binance source for contract "
    "lifecycle or eligibility.",
    "a different kind of evidence, not an approximation of the missing authority",
    "Nothing here claims the two are interchangeable",
)

REQUIRED_A2_C7_WORDING = (
    "Silent re-ranking is forbidden",
    "promoting the next-ranked one",
    "`NO_IMPACT`",
    "`RUN_INVALID_INPUT_EVIDENCE`",
)


@pytest.mark.unit
def test_substitute_evidence_is_never_called_equivalent_to_an_authority() -> None:
    haystacks = {DOC_PATH.name: DOC_PATH.read_text(encoding="utf-8")}
    for path in _package_sources():
        haystacks[path.name] = path.read_text(encoding="utf-8")

    offenders = [
        f"{name}: {phrase!r}"
        for name, text in haystacks.items()
        for phrase in FORBIDDEN_EQUIVALENCE_PHRASES
        if phrase in text.lower()
    ]
    assert not offenders, offenders


@pytest.mark.unit
def test_gap_closure_clauses_state_their_load_bearing_sentences() -> None:
    doc = " ".join(DOC_PATH.read_text(encoding="utf-8").split())
    for wording in REQUIRED_A2_C6_WORDING + REQUIRED_A2_C7_WORDING:
        assert wording in doc, f"missing from the contract document: {wording!r}"


@pytest.mark.unit
def test_no_authoritative_lifecycle_source_is_frozen_as_absent() -> None:
    assert c.LIFECYCLE_AUTHORITATIVE_PUBLIC_SOURCE is None
    assert set(c.LIFECYCLE_PROXY_KINDS) == {
        "exchange_info_onboard_date",
        "archive_month_range",
    }
    assert c.ELIGIBILITY_EVIDENCE_KIND not in c.LIFECYCLE_PROXY_KINDS
    assert c.GAP_EPOCH_VERDICTS == ("NO_IMPACT", "RUN_INVALID_INPUT_EVIDENCE")
    assert c.TERMINAL_CODE_PRIORITY[0] == "RUN_INVALID_INPUT_EVIDENCE"
    # Every proxy carries a stated limitation, not an empty placeholder.
    for name, limit in c.LIFECYCLE_PROXY_LIMITS.items():
        assert len(limit) > 80, f"{name} has no meaningful limitation text"


@pytest.mark.unit
def test_package_cannot_reach_the_network() -> None:
    offenders: list[str] = []
    for path in _package_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in FORBIDDEN_IMPORT_ROOTS:
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert not offenders, offenders


@pytest.mark.unit
def test_package_does_not_write_files() -> None:
    """No collection, no freeze: this package only judges objects handed to it."""
    offenders: list[str] = []
    for path in _package_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in {"write_text", "write_bytes", "open", "mkdir", "write_table"}:
                offenders.append(f"{path.name}:{node.lineno} calls {name}")
    assert not offenders, offenders
