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
        "OD-26-JOB-B",
        "OD-31",
    }
    assert "§26차" in nw_verbatim.BINDING_RECORD_AMENDMENT
    assert "§31차" in nw_verbatim.BINDING_RECORD_AMENDMENT_C9


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

#: A2-C9 has two sentences that carry the whole clause: the list may not be
#: re-derived, and a gap outside it is a stop rather than an extension.  Both
#: are the kind of sentence a later edit would soften first.
REQUIRED_A2_C9_WORDING = (
    "The epochs are an enumeration, not a rule.",
    "It covers exactly the 49 enumerated epochs.",
    "a FREEZE that meets one is a fail-closed stop, not a case for this clause",
    "The enumeration cannot grow to fit what FREEZE finds.",
    # The amendment does not get to quietly retire the sentence it looks like
    # a counterexample to.  Both stay, and the reconciliation is written out.
    "an out-of-enumeration gap found during a FREEZE is a **scan failure**, "
    "escalated upstream, not a 50th row added on the spot",
    "They were **not** individually cross-checked against REST.",
    "Amended exactly once.",
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
    for wording in (
        REQUIRED_A2_C6_WORDING + REQUIRED_A2_C7_WORDING + REQUIRED_A2_C9_WORDING
    ):
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


# --- A2-C9 (OD-31): the amendment's own scope ------------------------------

#: Every literal §31차 explicitly ruled unchanged.  A2-C9 adds a clause; if any
#: value below moved, what shipped is a different contract wearing the same ID,
#: and this test is the thing that says so out loud.
FROZEN_ACROSS_THE_C9_AMENDMENT = {
    "CORPUS_ID": "dfc-2c-4h-v22-corpus-v1",
    "CORPUS_ROOT": "/Users/mgh3326/work/herdr-artifacts/dfc-2c-4h-v22-corpus-v1/",
    "UNIVERSE_LOOKBACK_CALENDAR_DAYS": 30,
    "UNIVERSE_RANKING_METRIC": "quote_volume",
    "UNIVERSE_TOP_N": 3,
    "UNIVERSE_TIE_BREAK": "canonical_symbol_ascending",
    "UNIVERSE_INSTRUMENT_CLASS": "usdm_perpetual",
    "OUTCOME_TAIL_BARS": 1,
    "OUTCOME_HORIZON_BARS": 1,
    "OUTCOME_UNIT": "absolute_log_return_bps",
    "IMPUTED_ROW_COUNT_MAX": 0,
    "ADMISSIBILITY_BASIS": "independent_recollection",
    "SAMPLE_SEED": 26,
    "SAMPLE_EPOCHS_PER_QUARTER": 12,
}


@pytest.mark.unit
def test_c9_amendment_moved_no_frozen_literal() -> None:
    """NW-F5 · NW-F6 · the universe rule · the sample protocol are untouched."""
    for name, expected in FROZEN_ACROSS_THE_C9_AMENDMENT.items():
        assert getattr(c, name) == expected, name

    # The judgment window was *not* narrowed around the deficit epochs — that
    # was §31차's option "B" and it was refused, because a shorter window hides
    # the problem instead of recording it.
    assert c.WARMUP_START.isoformat() == "2021-02-02T00:00:00+00:00"
    assert c.WARMUP_END.isoformat() == "2021-05-02T00:00:00+00:00"
    assert c.JUDGMENT_START.isoformat() == "2021-05-02T00:00:00+00:00"
    assert c.JUDGMENT_END.isoformat() == "2023-08-04T00:00:00+00:00"

    assert c.REQUIRED_SOURCE_KINDS == (
        "usdm_kline_4h",
        "premium_index_4h",
        "contract_lifecycle_eligibility",
    )
    # No new terminal code: A2-C9 reuses the one A2-C7 already raises.
    assert c.RANKING_INPUT_DEFICIT_VERDICT == c.RUN_INVALID_INPUT_EVIDENCE
    assert c.TERMINAL_CODE_PRIORITY == (
        "RUN_INVALID_INPUT_EVIDENCE",
        "RUN_INVALID_SCOPE_SEPARATION",
        "RUN_INVALID_CORPUS_LITERALS",
        "RUN_INVALID_FORBIDDEN_SOURCE",
        "RUN_INVALID_MANIFEST_SCHEMA",
        "RUN_INVALID_TABLE_SCHEMA",
        "RUN_INVALID_AUTHENTICITY_EVIDENCE",
        "RUN_INVALID_OUTCOME_EVIDENCE",
    )


@pytest.mark.unit
def test_c9_enumeration_is_a_literal_the_package_cannot_recompute() -> None:
    """MUTANT ③, structural half.

    The enumeration is a measurement result that has already been read, so the
    package must not be able to re-derive it — a re-derivation would be a
    decision procedure re-run after seeing its inputs. The only permitted
    computation over the frozen rows is projecting out their first column.
    """
    assert len(c.RANKING_INPUT_DEFICIT_ROWS) == 49
    assert len(c.RANKING_INPUT_DEFICIT_EPOCHS) == 49
    assert list(c.RANKING_INPUT_DEFICIT_EPOCHS) == sorted(
        c.RANKING_INPUT_DEFICIT_EPOCHS
    )
    assert len(set(c.RANKING_INPUT_DEFICIT_EPOCHS)) == 49
    assert c.RANKING_INPUT_DEFICIT_EPOCHS[0] == 1646193600000
    assert c.RANKING_INPUT_DEFICIT_EPOCHS[-1] == 1649260800000

    # §34차 2항 replaced the list once, 38 -> 49.  The 38 already
    # pre-registered by §31차 are carried across unchanged — asserted here
    # because "we only added rows" is exactly the claim an amendment makes
    # about itself, and the amendment is the moment it is cheapest to break.
    assert c.RANKING_INPUT_DEFICIT_ROWS[:38] == tuple(
        (epoch, "GALAUSDT", "LUNAUSDT")
        for epoch in range(1646193600000, 1646726400000 + 1, c.BAR_INTERVAL_MS)
    )
    assert c.RANKING_INPUT_DEFICIT_ROWS[38:] == tuple(
        (epoch, "LUNAUSDT", "GMTUSDT")
        for epoch in range(1649116800000, 1649260800000 + 1, c.BAR_INTERVAL_MS)
    )

    # Two contiguous 4h runs, one per source window — not one run, and not 49
    # scattered epochs.  Exactly one step in the list is larger than a bar.
    step = c.BAR_INTERVAL_MS
    steps = [
        b - a
        for a, b in zip(
            c.RANKING_INPUT_DEFICIT_EPOCHS,
            c.RANKING_INPUT_DEFICIT_EPOCHS[1:],
            strict=False,
        )
    ]
    assert steps.count(step) == len(steps) - 1
    assert steps[37] > step

    assert len(c.RANKING_INPUT_DEFICIT_ENUMERATION_SHA256) == 64
    assert c.RANKING_INPUT_DEFICIT_ENUMERATION_SHA256.startswith("1dcf41ff108d2a9b")
    assert c.RANKING_INPUT_DEFICIT_ENUMERATION_PATH.endswith("unified_flip_epochs.json")
    assert len(c.RANKING_INPUT_DEFICIT_SCAN_SHA256) == 64
    assert c.RANKING_INPUT_DEFICIT_SCAN_SHA256.startswith("f8cae492dddc5832")
    assert c.RANKING_INPUT_DEFICIT_SCAN_PATH.endswith("internal_gaps.json")
    assert c.RANKING_INPUT_DEFICIT_SCAN_RECORD_COUNT == 105

    source = (PACKAGE_DIR / "contract.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign | ast.Assign)
        for target in (
            [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        )
        if isinstance(target, ast.Name)
        and target.id.startswith("RANKING_INPUT_DEFICIT")
    ]
    assert len(assignments) == 9, [ast.unparse(a).split("=")[0] for a in assignments]
    for node in assignments:
        value = node.value
        if value is None:
            continue
        # Literal tuples/strings are fine.  The single generator is the epochs
        # projection, and it may only read column 0 of the frozen rows.
        for call in ast.walk(value):
            if isinstance(call, ast.Call):
                assert getattr(call.func, "id", None) == "tuple", (
                    f"unexpected call in a frozen enumeration literal: {ast.unparse(call)}"
                )


@pytest.mark.unit
def test_c9_validator_offers_no_substitution_path() -> None:
    """MUTANT ②, structural half: no repaired pool can come out of A2-C9."""
    from research.dfc_v22_research_min import validation as val

    tree = ast.parse(Path(val.__file__).read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    entry = functions["validate_ranking_input_deficit"]
    assert isinstance(entry.returns, ast.Constant) and entry.returns.value is None
    returned = [
        node
        for node in ast.walk(entry)
        if isinstance(node, ast.Return) and node.value is not None
    ]
    assert not returned, "the deficit validator hands something back to its caller"


#: The enumeration source lives outside the repository (operator artifacts), so
#: this check runs where that tree is present and skips where it is not — the
#: same posture as the canonical answer document, which is pinned by digest for
#: the same reason.  Where it *does* run it is the sharpest form of MUTANT ③:
#: the frozen rows are re-read from the file and compared row by row, so an
#: enumeration that moved on disk fails here even if every literal in this repo
#: is self-consistent.
ENUMERATION_FILE = Path(
    c.RANKING_INPUT_DEFICIT_ENUMERATION_PATH.replace("~", str(Path.home()), 1)
)


@pytest.mark.unit
@pytest.mark.skipif(
    not ENUMERATION_FILE.is_file(), reason="operator artifact tree not present"
)
def test_c9_frozen_rows_reproduce_the_pinned_enumeration_file() -> None:
    import hashlib
    import json

    raw = ENUMERATION_FILE.read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest() == c.RANKING_INPUT_DEFICIT_ENUMERATION_SHA256
    ), "the pinned enumeration file changed on disk"

    rows = json.loads(raw)
    assert len(rows) == len(c.RANKING_INPUT_DEFICIT_ROWS)
    head = list(c.RANKING_INPUT_DEFICIT_UNCHANGED_HEAD)
    for row, (epoch, as_archived, would_have_been) in zip(
        rows, c.RANKING_INPUT_DEFICIT_ROWS, strict=True
    ):
        assert row["epoch_open_time"] == epoch
        assert row["current_top3"] == [*head, as_archived]
        assert row["corrected_top3"] == [*head, would_have_been]
        # Membership actually moved — computed here rather than read from a
        # flag in the file, so a mislabelled record cannot pass by asserting
        # its own correctness.  An order-only change would leave the sets
        # equal, and A2-C9 is about who is in the pool, not their order.
        assert set(row["current_top3"]) != set(row["corrected_top3"])


#: The scan the enumeration was measured over is pinned by its own digest
#: (§33차).  Same posture as the enumeration file: checked where the operator
#: artifact tree exists.  This is the other half of MUTANT ③ — the list can be
#: restated perfectly while the scan beneath it moves, and then "exhaustive"
#: describes a scope that is gone.
SCAN_FILE = Path(c.RANKING_INPUT_DEFICIT_SCAN_PATH.replace("~", str(Path.home()), 1))


@pytest.mark.unit
@pytest.mark.skipif(
    not SCAN_FILE.is_file(), reason="operator artifact tree not present"
)
def test_c9_pinned_scan_file_still_matches_its_digest_and_record_count() -> None:
    import hashlib
    import json

    raw = SCAN_FILE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == c.RANKING_INPUT_DEFICIT_SCAN_SHA256, (
        "the gap scan the enumeration was measured over changed on disk"
    )
    assert len(json.loads(raw)) == c.RANKING_INPUT_DEFICIT_SCAN_RECORD_COUNT
