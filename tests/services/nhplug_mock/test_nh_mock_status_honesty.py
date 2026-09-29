# ruff: noqa: F811
# Imported pytest fixtures intentionally share names with test parameters.
"""#942 answer-envelope honesty for nh_mock reconcile and order detail.

Rules under test:
- RECONCILE: status is reconciled only when every targeted row is resolved.
  Any unresolved row gives partial (some resolved) or uncertain (none
  resolved), with success false. The dry run predicts the same words.
- DETAIL: an incomplete all-scope listing is never success=true.
- REVIEW: an anomaly or manual-review row is never resolved. With no row
  unresolved it gives needs_review, success false, and names the row.

Expected values come from the fake NH setup and an independent ledger read,
never from the code under test. Each rule has an assertion-RED mutant compiled
from ``operations.py`` on disk.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.nhplug_mock import operations
from tests.services.nhplug_mock.test_dispatch_state_machine import (  # noqa: F401
    nhplug_engine,
)
from tests.services.nhplug_mock.test_nh_mock_operations import (  # noqa: F401
    UNKNOWN_LISTINGS,
    FakeNH,
    install,
    key,
    ledger_rows,
    ops_engine,
    order_row,
    stage2_env,
)

pytestmark = pytest.mark.integration

OPERATIONS_SOURCE = Path(operations.__file__)

# Mutant sources: each restores the #849 expression the rule replaced.
RECONCILE_RULE = (
    '        return "partial" if resolved else "uncertain"\n',
    '        return "reconciled"\n',
)
# The needs-review branch removed: an anomaly row reads as reconciled again.
REVIEW_RULE = (
    '    if needs_review:\n        return "needs_review"\n',
    "",
)
# Review rows counted as resolved: an anomaly beside an unresolved row reads
# as partial progress instead of none.
REVIEW_RESOLVED_RULE = (
    "        targeted - set(unresolved_ids) - set(unverified) - set(needs_review)\n",
    "        targeted - set(unresolved_ids) - set(unverified)\n",
)
DETAIL_RULE = (
    '        "success": listing.complete\n'
    '        and (broker["broker_view"] == "listed" or bool(rows)),\n',
    '        "success": broker["broker_view"] == "listed" or bool(rows),\n',
)


def _compile_mutant(name: str, old: str, new: str) -> types.ModuleType:
    source = OPERATIONS_SOURCE.read_text("utf-8")
    assert source.count(old) == 1, name
    module = types.ModuleType(name)
    module.__file__ = str(OPERATIONS_SOURCE)
    sys.modules[name] = module
    exec(  # noqa: S102
        compile(source.replace(old, new), str(OPERATIONS_SOURCE), "exec"),
        module.__dict__,
    )
    return module


@pytest.fixture
def reconcile_mutant() -> Iterator[types.ModuleType]:
    name = "nh_mock_operations_mutant_reconcile_rule"
    try:
        yield _compile_mutant(name, *RECONCILE_RULE)
    finally:
        sys.modules.pop(name, None)


@pytest.fixture
def review_mutant() -> Iterator[types.ModuleType]:
    name = "nh_mock_operations_mutant_review_rule"
    try:
        yield _compile_mutant(name, *REVIEW_RULE)
    finally:
        sys.modules.pop(name, None)


@pytest.fixture
def review_resolved_mutant() -> Iterator[types.ModuleType]:
    name = "nh_mock_operations_mutant_review_resolved_rule"
    try:
        yield _compile_mutant(name, *REVIEW_RESOLVED_RULE)
    finally:
        sys.modules.pop(name, None)


@pytest.fixture
def detail_mutant() -> Iterator[types.ModuleType]:
    name = "nh_mock_operations_mutant_detail_rule"
    try:
        yield _compile_mutant(name, *DETAIL_RULE)
    finally:
        sys.modules.pop(name, None)


async def _place(
    module: Any, suffix: str, n: int, symbol: str = "005930"
) -> dict[str, Any]:
    return await module.place_order(
        symbol=symbol,
        side="buy",
        quantity=1,
        price=50000,
        order_type="limit",
        idempotency_key=key(suffix, n),
        dry_run=False,
        confirm=True,
    )


async def _dry(module: Any) -> dict[str, Any]:
    return await module.reconcile_orders(dry_run=True)


async def _confirmed(module: Any) -> dict[str, Any]:
    return await module.reconcile_orders(dry_run=False, confirm=True)


# Scenario name -> (numbers the broker lists, expected envelope words).
# Row 0 places 005930 with number 1000801; row 1 places 000660 with 1000802
# (a different symbol, so the first reservation does not block it).
SCENARIOS: dict[str, tuple[tuple[int, ...], int, str, str]] = {
    # name: (listed numbers, rows placed, dry-run would_be_status, confirmed status)
    "all_resolved": ((1000801, 1000802), 2, "verification_pending", "reconciled"),
    "some_resolved": ((1000801,), 2, "partial", "partial"),
    "none_resolved": ((), 2, "uncertain", "uncertain"),
    "single_unlisted": ((), 1, "uncertain", "uncertain"),
}


async def _scenario(
    module: Any,
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    suffix: str,
    listed: tuple[int, ...],
    placed: int,
) -> FakeNH:
    fake = install(
        monkeypatch,
        engine,
        suffix,
        module=module,
        order_numbers=["1000801", "1000802"],
    )
    for n, symbol in list(enumerate(("005930", "000660"), start=1))[:placed]:
        answer = await _place(module, suffix, n, symbol)
        assert answer["status"] == "uncertain", answer
    symbols = {1000801: "005930", 1000802: "000660"}
    for number in listed:
        fake.rows[number] = order_row(number, symbol=symbols[number])
    return fake


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_reconcile_status_names_every_unresolved_row(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    listed, placed, would_be, expected = SCENARIOS[name]
    suffix = "st" + name.replace("_", "")[:12]
    fake = await _scenario(operations, monkeypatch, ops_engine, suffix, listed, placed)
    before = await ledger_rows(ops_engine, fake.account_no)
    ids = [r["id"] for r in before]
    listed_ids = [
        ids[i]
        for i, number in enumerate((1000801, 1000802)[:placed])
        if number in listed
    ]
    unlisted_ids = [i for i in ids if i not in listed_ids]

    plan = await _dry(operations)
    assert plan["status"] == "dry_run"
    assert plan["ledger_writes"] == 0
    assert plan["would_be_status"] == would_be
    assert plan["would_be_unresolved_row_ids"] == unlisted_ids
    assert plan["verification_pending_row_ids"] == listed_ids
    assert await ledger_rows(ops_engine, fake.account_no) == before

    result = await _confirmed(operations)
    after = await ledger_rows(ops_engine, fake.account_no)
    still_uncertain = [r["id"] for r in after if r["state"] == "uncertain"]
    assert still_uncertain == unlisted_ids  # independent ledger read
    assert result["status"] == expected
    assert result["success"] is (expected == "reconciled")
    assert result["unresolved_row_ids"] == still_uncertain
    assert result["resolved_row_ids"] == listed_ids
    assert result["incomplete_scopes"] == []
    assert result["unverified_row_ids"] == []
    if still_uncertain:
        assert result["status"] != "reconciled"
    # Dry-run and commit agree: the same word, or pending became reconciled.
    assert (plan["would_be_status"], result["status"]) in {
        ("verification_pending", "reconciled"),
        ("partial", "partial"),
        ("uncertain", "uncertain"),
    }
    # Sends only happened at placement.
    assert len(fake.orders) == placed


@pytest.mark.asyncio
async def test_reconcile_with_nothing_to_settle_is_reconciled(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "stnothing")
    plan = await _dry(operations)
    assert plan["would_be_status"] == "reconciled"
    assert plan["would_be_unresolved_row_ids"] == []
    assert plan["verification_pending_row_ids"] == []
    result = await _confirmed(operations)
    assert result["status"] == "reconciled"
    assert result["success"] is True
    assert result["resolved_row_ids"] == []
    assert fake.orders == []


@pytest.mark.asyncio
async def test_partial_after_a_bound_row_reconciles_and_a_new_row_stays_uncertain(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bound row re-verified plus a new unlisted row is partial, not reconciled."""

    fake = install(
        monkeypatch,
        ops_engine,
        "stbound",
        order_numbers=["1000811", "1000812"],
    )
    await _place(operations, "stbound", 1)
    fake.rows[1000811] = order_row(1000811)
    assert (await _confirmed(operations))["status"] == "reconciled"
    await _place(operations, "stbound", 2, "000660")
    rows = await ledger_rows(ops_engine, fake.account_no)
    assert [r["state"] for r in rows] == ["open", "uncertain"]

    plan = await _dry(operations)
    assert plan["would_be_status"] == "partial"
    assert plan["would_be_unresolved_row_ids"] == [rows[1]["id"]]
    assert plan["verification_pending_row_ids"] == [rows[0]["id"]]

    result = await _confirmed(operations)
    assert result["status"] == "partial"
    assert result["success"] is False
    assert result["resolved_row_ids"] == [rows[0]["id"]]
    assert result["unresolved_row_ids"] == [rows[1]["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ("all", "open", "filled"))
async def test_dry_run_and_commit_both_say_unknown_on_an_incomplete_scope(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    suffix = "stinc" + scope
    fake = await _scenario(operations, monkeypatch, ops_engine, suffix, (1000801,), 1)
    fake.listing_override = {scope: UNKNOWN_LISTINGS["gateway_error"]}
    plan = await _dry(operations)
    assert plan["would_be_status"] == "unknown"
    assert plan["incomplete_scopes"] == [scope]
    result = await _confirmed(operations)
    assert result["status"] == "unknown"
    assert result["success"] is False
    assert result["incomplete_scopes"] == [scope]


# ---------------------------------------------------------------------------
# Order detail
# ---------------------------------------------------------------------------


async def _detail_with_incomplete_listing(
    module: Any, monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine, suffix: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    fake = install(
        monkeypatch, engine, suffix, module=module, order_numbers=["1000821"]
    )
    await _place(module, suffix, 1)
    fake.listing_override = {"all": UNKNOWN_LISTINGS["gateway_error"]}
    detail = await module.get_order_detail(order_id="1000821")
    return detail, await ledger_rows(engine, fake.account_no)


def _assert_detail_invariant(detail: dict[str, Any]) -> None:
    assert detail["success"] is False
    assert detail["status"] == "unknown"
    assert detail["reason"] == "order_listing_incomplete"


@pytest.mark.asyncio
async def test_detail_on_incomplete_listing_is_never_success(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    detail, rows = await _detail_with_incomplete_listing(
        operations, monkeypatch, ops_engine, "dtinc"
    )
    # The ledger does reference the number: rows alone must not make success.
    assert [r["ack_evidence_order_id"] for r in rows] == ["1000821"]
    assert [r["id"] for r in rows] == [
        r["ledger_row_id"] for r in detail["ledger_rows"]
    ]
    _assert_detail_invariant(detail)
    assert detail["broker_view"] == "unknown"
    assert detail["listing_reason"]


@pytest.mark.asyncio
async def test_detail_on_complete_listing_keeps_849_behavior(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "dtcomp", order_numbers=["1000831"])
    await _place(operations, "dtcomp", 1)
    # Complete listing without the number, ledger rows reference it: unchanged.
    unlisted = await operations.get_order_detail(order_id="1000831")
    assert unlisted["status"] == "not_listed"
    assert unlisted["success"] is True
    assert "reason" not in unlisted
    # Complete listing without the number and no ledger rows: unchanged.
    stranger = await operations.get_order_detail(order_id="1000839")
    assert stranger["status"] == "not_listed"
    assert stranger["success"] is False
    # Complete listing showing the number.
    fake.rows[1000831] = order_row(1000831)
    listed = await operations.get_order_detail(order_id="1000831")
    assert listed["status"] == "listed"
    assert listed["success"] is True


# ---------------------------------------------------------------------------
# Assertion-RED mutants
# ---------------------------------------------------------------------------


def _assert_reconcile_invariant(
    plan: dict[str, Any], result: dict[str, Any], after: list[dict[str, Any]]
) -> None:
    uncertain = [r["id"] for r in after if r["state"] == "uncertain"]
    assert uncertain, "scenario must leave an unresolved row"
    assert result["success"] is False
    assert result["status"] in {"partial", "uncertain"}
    assert plan["would_be_status"] in {"partial", "uncertain"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ("some_resolved", "none_resolved"))
async def test_reconcile_rule_mutant_is_assertion_red(
    ops_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    reconcile_mutant: types.ModuleType,
    name: str,
) -> None:
    listed, placed, _, _ = SCENARIOS[name]
    for module, tag in ((operations, "c"), (reconcile_mutant, "m")):
        suffix = "rm" + tag + name.replace("_", "")[:10]
        fake = await _scenario(module, monkeypatch, ops_engine, suffix, listed, placed)
        plan = await _dry(module)
        result = await _confirmed(module)
        after = await ledger_rows(ops_engine, fake.account_no)
        if module is operations:
            _assert_reconcile_invariant(plan, result, after)
        else:
            with pytest.raises(AssertionError):
                _assert_reconcile_invariant(plan, result, after)
            # The mutant is the #849 answer: reconciled over an unresolved row.
            assert result["status"] == "reconciled"
            assert result["unresolved_row_ids"]


@pytest.mark.asyncio
async def test_detail_rule_mutant_is_assertion_red(
    ops_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    detail_mutant: types.ModuleType,
) -> None:
    detail, _ = await _detail_with_incomplete_listing(
        operations, monkeypatch, ops_engine, "dmctl"
    )
    _assert_detail_invariant(detail)
    detail, _ = await _detail_with_incomplete_listing(
        detail_mutant, monkeypatch, ops_engine, "dmmut"
    )
    with pytest.raises(AssertionError):
        _assert_detail_invariant(detail)
    assert detail["success"] is True  # the #849 answer


# ---------------------------------------------------------------------------
# Needs review: anomaly and manual-review rows are never resolved
# ---------------------------------------------------------------------------


async def _anomaly_scenario(
    module: Any,
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    suffix: str,
    *,
    second: str | None,
) -> FakeNH:
    """Row 0 (005930) gets 1000841, which the broker lists under 000660.

    second: None (no other row), "listed" (000660 row whose 1000842 is listed
    with matching attributes), or "unlisted" (000660 row, 1000842 absent).
    """

    fake = install(
        monkeypatch,
        engine,
        suffix,
        module=module,
        order_numbers=["1000841", "1000842"],
    )
    await _place(module, suffix, 1, "005930")
    fake.rows[1000841] = order_row(1000841, symbol="000660")
    if second is not None:
        await _place(module, suffix, 2, "000660")
        if second == "listed":
            fake.rows[1000842] = order_row(1000842, symbol="000660")
    return fake


def _assert_review_invariant(
    result: dict[str, Any], after: list[dict[str, Any]]
) -> None:
    review = [
        r["id"] for r in after if r["state"] == "anomaly" or r["requires_manual_review"]
    ]
    assert review, "scenario must leave a needs-review row"
    assert result["success"] is False
    assert result["status"] != "reconciled"
    assert result["needs_review_row_ids"] == review
    assert not set(review) & set(result["resolved_row_ids"])


# name: (second row, dry would_be_status, confirmed status)
REVIEW_SCENARIOS: dict[str, tuple[str | None, str, str]] = {
    "anomaly_alone": (None, "needs_review", "needs_review"),
    "anomaly_and_resolved": ("listed", "needs_review", "needs_review"),
    "anomaly_and_unresolved": ("unlisted", "uncertain", "uncertain"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(REVIEW_SCENARIOS))
async def test_anomaly_row_is_never_resolved(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    second, would_be, expected = REVIEW_SCENARIOS[name]
    suffix = "nr" + name.replace("_", "")[:12]
    fake = await _anomaly_scenario(
        operations, monkeypatch, ops_engine, suffix, second=second
    )
    before = await ledger_rows(ops_engine, fake.account_no)
    first_id = before[0]["id"]
    second_id = before[1]["id"] if second is not None else None

    plan = await _dry(operations)
    assert plan["would_be_status"] == would_be
    assert plan["would_be_needs_review_row_ids"] == [first_id]
    assert plan["would_be_unresolved_row_ids"] == (
        [second_id] if second == "unlisted" else []
    )
    assert plan["verification_pending_row_ids"] == (
        [second_id] if second == "listed" else []
    )
    assert await ledger_rows(ops_engine, fake.account_no) == before

    result = await _confirmed(operations)
    after = await ledger_rows(ops_engine, fake.account_no)
    # Independent read: T9b recorded the attribute mismatch as anomaly.
    assert (after[0]["state"], after[0]["requires_manual_review"]) == ("anomaly", True)
    assert after[0]["manual_review_reason"] == "own_number_attribute_mismatch"
    _assert_review_invariant(result, after)
    assert result["status"] == expected
    assert result["resolved_row_ids"] == ([second_id] if second == "listed" else [])
    assert result["unresolved_row_ids"] == ([second_id] if second == "unlisted" else [])
    assert len(fake.orders) == len(before)

    # The anomaly is terminal: a later reconcile still says it needs review.
    again = await _confirmed(operations)
    assert again["status"] == expected
    assert again["needs_review_row_ids"] == [first_id]
    assert (await _dry(operations))["would_be_status"] == expected


async def _review_run(
    module: Any,
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    suffix: str,
    second: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    fake = await _anomaly_scenario(module, monkeypatch, engine, suffix, second=second)
    result = await _confirmed(module)
    return result, await ledger_rows(engine, fake.account_no)


@pytest.mark.asyncio
async def test_review_rule_mutant_is_assertion_red(
    ops_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    review_mutant: types.ModuleType,
) -> None:
    result, after = await _review_run(
        operations, monkeypatch, ops_engine, "rvctlalone", None
    )
    _assert_review_invariant(result, after)
    result, after = await _review_run(
        review_mutant, monkeypatch, ops_engine, "rvmutalone", None
    )
    with pytest.raises(AssertionError):
        _assert_review_invariant(result, after)
    # The mutant is the round-1 answer: reconciled over an anomaly row.
    assert (result["status"], result["success"]) == ("reconciled", True)


@pytest.mark.asyncio
async def test_review_resolved_mutant_is_assertion_red(
    ops_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    review_resolved_mutant: types.ModuleType,
) -> None:
    result, after = await _review_run(
        operations, monkeypatch, ops_engine, "rvctlunres", "unlisted"
    )
    _assert_review_invariant(result, after)
    assert result["status"] == "uncertain"
    result, after = await _review_run(
        review_resolved_mutant, monkeypatch, ops_engine, "rvmutunres", "unlisted"
    )
    with pytest.raises(AssertionError):
        _assert_review_invariant(result, after)
    # The mutant counts the anomaly row as progress.
    assert result["status"] == "partial"
