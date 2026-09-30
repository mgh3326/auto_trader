"""#1175 — pure tests for the execution-ledger quarantine eligibility rules."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.execution_ledger import quarantine as q
from tests.services.execution_ledger._quarantine_fixtures import (
    fake_row,
    frame,
    frame_fields,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE = REPO_ROOT / "app/services/execution_ledger/quarantine.py"
CLI = REPO_ROOT / "scripts/quarantine_execution_ledger_rows.py"
REPOSITORY = REPO_ROOT / "app/services/execution_ledger/repository.py"


# ------------------------------------------------------------------ ids


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["58051"], (58051,)),
        (["58051,58052,58053,58064"], (58051, 58052, 58053, 58064)),
        (["58051", "58052"], (58051, 58052)),
        ([str(2**63 - 1)], (2**63 - 1,)),
    ],
)
def test_parse_ids_accepts_only_exact_decimal_ids(values, expected) -> None:
    assert q.parse_ids(values) == expected


@pytest.mark.parametrize(
    "token",
    [
        "",
        "58051-58064",
        "58051..58064",
        "5805*",
        "%",
        "58051 ",
        " 58051",
        "+58051",
        "-1",
        "0",
        "058051",
        "58_051",
        "５８０５１",  # full-width digits: int() would accept them
        "٥٨٠٥١",  # Arabic-Indic digits
        "0x1",
        "1e3",
        "58051;DROP",
        str(2**63),
        "1" * 20,
    ],
)
def test_parse_ids_refuses_ranges_patterns_and_look_alikes(token: str) -> None:
    with pytest.raises(q.QuarantineInputError):
        q.parse_ids([token])


def test_parse_ids_refuses_duplicates_empty_lists_and_oversized_batches() -> None:
    with pytest.raises(q.QuarantineInputError, match="repeats"):
        q.parse_ids(["58051,58051"])
    with pytest.raises(q.QuarantineInputError, match="repeats"):
        q.parse_ids(["58051", "58051"])
    with pytest.raises(q.QuarantineInputError):
        q.parse_ids([])
    with pytest.raises(q.QuarantineInputError):
        q.parse_ids(["1,"])
    with pytest.raises(q.QuarantineInputError, match="at most"):
        q.parse_ids([",".join(str(i) for i in range(1, q.MAX_IDS + 2))])


@pytest.mark.parametrize("value", [None, "", "   ", "a\nb", "a\x00b", "x​y"])
def test_reason_and_actor_are_required_single_line_text(value) -> None:
    with pytest.raises(q.QuarantineInputError):
        q.validate_text("reason", value, max_chars=q.MAX_REASON_CHARS)


def test_reason_is_bounded() -> None:
    with pytest.raises(q.QuarantineInputError, match="exceeds"):
        q.validate_text("reason", "x" * 501, max_chars=q.MAX_REASON_CHARS)
    assert q.validate_text("reason", "  hk 1172  ", max_chars=500) == "hk 1172"


# ------------------------------------------------------------- verdicts


def test_accept_notice_row_is_eligible_and_never_echoes_account_fields() -> None:
    verdict = q.evaluate_row(1, fake_row())
    assert verdict.verdict == "accept_notice"
    assert verdict.eligible
    assert verdict.detail["raw_cntg_yn"] == "1"
    assert verdict.detail["raw_fill_seq_matches"] is True
    rendered = repr(verdict.as_dict())
    assert "FAKECUST" not in rendered
    assert "ACCTFAKE01" not in rendered
    assert "FAKENAME" not in rendered


REFUSALS: dict[str, dict] = {
    "not_found": {},
    "already_quarantined": {"quarantined_at": datetime(2026, 10, 1, tzinfo=UTC)},
    "not_websocket": {"source": "reconciler"},
    "not_kis": {"broker": "upbit"},
    "not_live": {"account_mode": "mock"},
    "not_equity_kr": {"instrument_type": "equity_us"},
    "raw_payload_missing": {"raw_payload_json": None},
    "raw_payload_not_domestic_execution_notice": {
        "raw_payload_json": frame(tr="H0STCNI9")
    },
    "raw_payload_fields_malformed": {
        "raw_payload_json": {"tr": "H0STCNI0", "fields": frame_fields()[:13]}
    },
    "fill_notice_cntg_yn_2": {"cntg_yn": "2"},
    "cntg_yn_not_accept": {"cntg_yn": " "},
    "raw_order_no_mismatch": {
        "raw_payload_json": frame(order_no="0000099999"),
    },
    "raw_symbol_mismatch": {"raw_payload_json": frame(symbol="005930")},
}


def refusal_row(verdict: str):
    if verdict == "not_found":
        return None
    return fake_row(**REFUSALS[verdict])


def test_every_refusal_verdict_has_a_scenario() -> None:
    literal = set(q.Verdict.__args__)  # type: ignore[attr-defined]
    assert literal - {"accept_notice"} == set(REFUSALS)


@pytest.mark.parametrize("verdict", sorted(REFUSALS))
def test_each_ineligible_row_gets_its_own_refusal(verdict: str) -> None:
    result = q.evaluate_row(7, refusal_row(verdict))
    assert result.verdict == verdict
    assert not result.eligible


@pytest.mark.parametrize(
    "raw",
    [
        {},
        [],
        "H0STCNI0",
        {"tr": "H0STCNI0"},
        {"tr": "H0STCNI0", "fields": "a^b"},
        {"tr": "H0STCNI0", "fields": [*frame_fields()[:13], 1]},
        {"tr": "H0STCNI0", "fields": [None] * 23},
        {"tr": "H0GSCNI0", "fields": frame_fields()},
        {"tr": "h0stcni0", "fields": frame_fields()},
        {"TR": "H0STCNI0", "fields": frame_fields()},
    ],
)
def test_unreadable_or_foreign_frames_are_refused(raw) -> None:
    result = q.evaluate_row(7, fake_row(raw_payload_json=raw))
    assert not result.eligible


def test_cntg_yn_must_be_exactly_one() -> None:
    for value in ("2", "", "0", "11", "Y", "１"):
        result = q.evaluate_row(7, fake_row(cntg_yn=value))
        assert not result.eligible, value
    # surrounding whitespace in the fixed-width KIS field is tolerated
    assert q.evaluate_row(7, fake_row(cntg_yn=" 1 ")).eligible


def test_fill_seq_mismatch_is_reported_not_gated() -> None:
    row = fake_row()
    row.fill_seq = row.fill_seq ^ 1
    result = q.evaluate_row(7, row)
    assert result.eligible
    assert result.detail["raw_fill_seq_matches"] is False


def test_fillwire_fill_seq_matches_the_go_derivation() -> None:
    # Vectors computed once with fillwire's Go DeriveFillSeq (sha256 of the
    # "^"-joined fields, first 4 bytes big-endian, masked to 31 bits).
    assert q.fillwire_fill_seq(["a", "b", "c"]) == 1858998197
    assert q.fillwire_fill_seq(frame_fields()) == 2074241475


# -------------------------------------------------------------- batches


def _v(ledger_id: int, verdict: str) -> q.RowVerdict:
    return q.RowVerdict(ledger_id, verdict)  # type: ignore[arg-type]


def test_batch_decision() -> None:
    ids = (1, 2)
    ok = [_v(1, "accept_notice"), _v(2, "accept_notice")]
    assert q.decide(ids, ok) == "eligible"
    assert q.decide(ids, [_v(1, "accept_notice"), _v(2, "not_found")]) == "refused"
    assert (
        q.decide(ids, [_v(1, "accept_notice"), _v(2, "already_quarantined")])
        == "refused"
    )
    assert (
        q.decide(ids, [_v(1, "already_quarantined"), _v(2, "already_quarantined")])
        == "noop"
    )
    assert q.decide(ids, [_v(1, "accept_notice")]) == "refused"
    assert q.decide((), []) == "refused"


# ------------------------------------------------------------ no delete


def _calls_and_strings(path: Path) -> tuple[set[str], list[str]]:
    tree = ast.parse(path.read_text("utf-8"))
    calls: set[str] = set()
    strings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                calls.add(func.id)
            elif isinstance(func, ast.Attribute):
                calls.add(func.attr)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node.value)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    return calls | imported, strings


@pytest.mark.parametrize(
    "path", [SERVICE, CLI, REPOSITORY], ids=["service", "cli", "repository"]
)
def test_quarantine_surface_has_no_delete_or_raw_sql_path(path: Path) -> None:
    calls, strings = _calls_and_strings(path)
    assert "delete" not in calls
    assert "text" not in calls
    assert "execute_raw" not in calls
    for value in strings:
        upper = value.upper()
        assert "DELETE " not in upper, value
        assert "TRUNCATE" not in upper, value
        assert "UPDATE REVIEW" not in upper, value


def _chain_root(node: ast.AST) -> ast.AST:
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        node = node.func.value
    return node


def test_the_only_ledger_update_is_the_guarded_quarantine_set() -> None:
    tree = ast.parse(REPOSITORY.read_text("utf-8"))
    updates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "update"
    ]
    assert len(updates) == 1
    update_values = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "values"
        and isinstance(root := _chain_root(node.func.value), ast.Call)
        and isinstance(root.func, ast.Name)
        and root.func.id == "update"
    ]
    assert len(update_values) == 1
    assert {kw.arg for kw in update_values[0].keywords} == {
        "quarantined_at",
        "quarantine_reason",
        "quarantined_by",
    }
    # the service itself builds no SQL: every read/write goes via the repository
    service = SERVICE.read_text("utf-8")
    assert "import sqlalchemy" not in service
    assert "from sqlalchemy import" not in service
    assert ".execute(" not in service


def test_accept_notice_predicate_matches_the_eligibility_rule() -> None:
    from app.services.execution_ledger.accept_notice import is_accept_notice_frame

    assert is_accept_notice_frame(frame())
    assert is_accept_notice_frame(frame(cntg_yn=" 1 "))
    for raw in (
        None,
        {},
        frame(cntg_yn="2"),
        frame(cntg_yn=""),
        frame(tr="H0STCNI9"),
        {"tr": "H0STCNI0", "fields": frame_fields()[:13]},
        {"tr": "H0STCNI0", "fields": [None] * 23},
    ):
        assert not is_accept_notice_frame(raw), raw
