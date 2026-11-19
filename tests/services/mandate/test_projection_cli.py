"""hk 3795 T4b: the projection CLI ``scripts/mandate_projection.py`` without a database.

Preview versus commit (through injected fakes), per-plan transactions, the closed
holdings input, the static boundary (no order, broker or ledger write) and the
runbook's commands against the real argparse. The database round trips live in
``test_projection_db.py`` and run in CI.
"""

from __future__ import annotations

import ast
import asyncio
import json
import shlex
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.mandate import projection as proj
from app.services.mandate import store as ms
from app.services.mandate import vocabulary as v
from app.services.mandate.projection_rules import (
    Hold,
    PlannedEvent,
    ProjectionHold,
)
from scripts import mandate as base_cli
from scripts import mandate_projection as cli

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/mandate_projection.py"
RUNBOOK = ROOT / "docs/runbooks/mandate-projection.md"
DB = ["--database-url-env", "MANDATE_TEST_DB"]
NOW = datetime(2026, 11, 27, 6, 0, tzinfo=UTC)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _parse(*argv: str):
    return cli._parser().parse_args([*DB, *argv])


def _report(plan_id: int, events: tuple[str, ...] = (), **over) -> proj.PlanReport:
    base = {
        "plan_id": plan_id,
        "symbol": "005930",
        "authority": "approved",
        "cleanup": "none",
        "planned": tuple(PlannedEvent(e, {}, {}) for e in events),
        "holds": (),
    }
    base.update(over)
    return proj.PlanReport(**base)


def _run(monkeypatch, argv: list[str], reports: dict[int, object]):
    session = FakeSession()
    calls: list[tuple[int, bool]] = []

    async def fake_project(_session, plan_id, **kwargs):
        calls.append((plan_id, kwargs["write"]))
        outcome = reports[plan_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(cli.proj, "project_plan", fake_project)
    code, payload = asyncio.run(
        cli.run(_parse(*argv), session_factory=lambda: session, now=NOW)
    )
    return code, payload, session, calls


# ------------------------------------------------------------ preview and commit
def test_a_run_without_commit_is_a_preview_that_never_writes_or_commits(monkeypatch):
    code, payload, session, calls = _run(
        monkeypatch,
        ["--plan-id", "1"],
        {1: _report(1, ("tranche_unfilled",))},
    )
    assert code == 0
    assert payload["mode"] == "preview"
    assert calls == [(1, False)]
    assert session.commits == 0
    assert payload["plans"][0]["written"] is False
    assert [e["event"] for e in payload["plans"][0]["events"]] == ["tranche_unfilled"]


def test_commit_commits_each_plan_that_has_events_in_its_own_transaction(monkeypatch):
    code, payload, session, calls = _run(
        monkeypatch,
        ["--plan-id", "1", "--plan-id", "2", "--commit"],
        {
            1: _report(1, ("tranche_filled", "first_tranche_settled")),
            2: _report(2, ("tranche_unfilled",)),
        },
    )
    assert code == 0
    assert calls == [(1, True), (2, True)]
    assert session.commits == 2
    assert [p["written"] for p in payload["plans"]] == [True, True]


def test_commit_does_not_commit_a_plan_with_nothing_to_write(monkeypatch):
    code, payload, session, _ = _run(
        monkeypatch,
        ["--plan-id", "1", "--commit"],
        {1: _report(1, holds=(Hold(ProjectionHold.RUNG_OPEN, None, 1),))},
    )
    assert code == 0
    assert session.commits == 0
    assert payload["plans"][0]["holds"][0]["code"] == "rung_open"
    assert payload["plans"][0]["written"] is False


def test_a_store_refusal_rolls_that_plan_back_and_the_others_still_run(monkeypatch):
    refusal = ms.MandateError(v.MandateRefusal.INVALID_TRANSITION, "x")
    code, payload, session, calls = _run(
        monkeypatch,
        ["--plan-id", "1", "--plan-id", "2", "--commit"],
        {1: refusal, 2: _report(2, ("tranche_unfilled",))},
    )
    assert code == 1
    assert payload["failed"] is True
    assert [c[0] for c in calls] == [1, 2]
    assert session.commits == 1  # only plan 2
    assert "mandate_invalid_transition" in payload["plans"][0]["error"]
    assert payload["plans"][1]["written"] is True


def test_an_append_error_inside_a_plan_is_rolled_back_not_committed(monkeypatch):
    code, payload, session, _ = _run(
        monkeypatch,
        ["--plan-id", "1", "--commit"],
        {1: _report(1, ("tranche_filled",), error="boom", appended_seqs=(5,))},
    )
    assert code == 1
    assert session.commits == 0
    assert payload["plans"][0]["written"] is False
    assert payload["plans"][0]["error"] == "boom"


def test_the_same_plan_id_is_projected_once(monkeypatch):
    _, _, _, calls = _run(
        monkeypatch,
        ["--plan-id", "3", "--plan-id", "3"],
        {3: _report(3)},
    )
    assert calls == [(3, False)]


def test_a_sleeve_key_selects_its_unreleased_plans(monkeypatch):
    async def fake_sleeve(_session, key):
        return SimpleNamespace(id=9) if key == "mock-kr" else None

    async def fake_ids(_session, *, sleeve_id=None):
        assert sleeve_id == 9
        return [4, 7]

    monkeypatch.setattr(cli.sleeve_store, "get_sleeve_by_key", fake_sleeve)
    monkeypatch.setattr(cli.proj, "unreleased_plan_ids", fake_ids)
    _, payload, _, calls = _run(
        monkeypatch, ["--sleeve-key", "mock-kr"], {4: _report(4), 7: _report(7)}
    )
    assert [c[0] for c in calls] == [4, 7]
    assert [p["plan_id"] for p in payload["plans"]] == [4, 7]


def test_an_unknown_sleeve_key_is_refused(monkeypatch):
    async def fake_sleeve(_session, _key):
        return None

    monkeypatch.setattr(cli.sleeve_store, "get_sleeve_by_key", fake_sleeve)
    with pytest.raises(base_cli.MandateCliError):
        _run(monkeypatch, ["--sleeve-key", "nope"], {})


def test_the_events_carry_exact_decimal_strings_not_floats(monkeypatch):
    event = PlannedEvent(
        "tranche_filled",
        {
            "tranche_index": 1,
            "fill_amount": Decimal("588600.000000000000"),
            "fill_quantity": Decimal("6"),
            "first_fill_amount": Decimal("888600"),
            "first_fill_quantity": Decimal("9"),
        },
        {"trade_date": "2026-11-24"},
    )
    _, payload, _, _ = _run(
        monkeypatch,
        ["--plan-id", "1"],
        {1: _report(1, planned=(event,))},
    )
    got = payload["plans"][0]["events"][0]
    assert got["fill_amount"] == "588600.000000000000"
    assert got["first_fill_amount"] == "888600"
    json.dumps(payload)  # serialisable as is


# ------------------------------------------------------------ the parser
def test_a_target_is_required_and_the_two_targets_exclude_each_other():
    with pytest.raises(SystemExit):
        _parse()
    with pytest.raises(SystemExit):
        _parse("--plan-id", "1", "--sleeve-key", "k")


def test_the_database_must_be_named():
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["--plan-id", "1"])


def test_commit_is_off_by_default():
    assert _parse("--plan-id", "1").commit is False
    assert _parse("--plan-id", "1", "--commit").commit is True


@pytest.mark.parametrize("hours", ["0", "-1", "169", "x"])
def test_the_holdings_age_limit_is_bounded(hours: str):
    with pytest.raises(SystemExit):
        _parse("--plan-id", "1", "--max-holdings-age-hours", hours)


# ------------------------------------------------------------ holdings input
def _holdings_file(tmp_path: Path, body) -> str:
    path = tmp_path / "holdings.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def _reading(**over):
    base = {
        "account_ref": "acct-1",
        "symbol": "005930",
        "quantity": "0",
        "checked_at": "2026-11-27T05:00:00+00:00",
    }
    base.update(over)
    return base


def test_no_holdings_file_means_no_readings():
    assert cli.read_holdings(None) == ()


def test_a_closed_holdings_file_is_read_into_typed_readings(tmp_path):
    path = _holdings_file(tmp_path, {"readings": [_reading(quantity="12.5")]})
    (item,) = cli.read_holdings(path)
    assert item.account_ref == "acct-1"
    assert item.quantity == Decimal("12.5")
    assert item.checked_at.tzinfo is not None


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"readings": "x"},
        {"readings": [], "extra": 1},
        {"readings": [_reading(extra="x")]},
        {"readings": [{k: v for k, v in _reading().items() if k != "symbol"}]},
        {"readings": [_reading(checked_at="2026-11-27T05:00:00")]},  # no zone
        {"readings": [_reading(checked_at="yesterday")]},
        {"readings": [_reading(quantity="NaN")]},
        {"readings": [_reading(quantity="lots")]},
        {"readings": [_reading(account_ref=" ")]},
        {"readings": [_reading(symbol=5)]},
    ],
)
def test_a_malformed_holdings_file_is_refused(tmp_path, body):
    with pytest.raises(base_cli.MandateCliError):
        cli.read_holdings(_holdings_file(tmp_path, body))


def test_an_unreadable_holdings_file_is_refused(tmp_path):
    with pytest.raises(base_cli.MandateCliError):
        cli.read_holdings(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(base_cli.MandateCliError):
        cli.read_holdings(str(bad))


def test_a_holdings_error_stops_the_run_before_any_database_is_opened(tmp_path):
    args = _parse("--plan-id", "1", "--holdings", str(tmp_path / "missing.json"))
    opened: list[int] = []
    with pytest.raises(base_cli.MandateCliError):
        asyncio.run(
            cli.run(args, session_factory=lambda: opened.append(1) or FakeSession())
        )
    assert opened == []


# ------------------------------------------------------------ static boundary
def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


FORBIDDEN_IMPORTS = (
    "app.services.brokers",
    "app.mcp_server",
    "app.services.order_proposals.dispatch",
    "app.services.order_proposals.revalidation",
    "app.services.order_proposals.broker_gateway",
    "app.services.order_proposals.auto_approve",
    "app.services.order_proposals.service",
    "app.services.order_proposals.telegram_callback",
    "app.services.mechanical_loss_cut",
    "app.services.execution_intent_gateway",
    "app.tasks",
    "app.models.execution_ledger",
)
PROJECTION_FILES = (
    SCRIPT,
    ROOT / "app/services/mandate/projection.py",
    ROOT / "app/services/mandate/projection_rules.py",
)


@pytest.mark.parametrize("path", PROJECTION_FILES, ids=lambda p: p.name)
def test_the_projection_imports_no_order_broker_or_ledger_module(path: Path):
    offenders = [n for n in _imports(path) if n.startswith(FORBIDDEN_IMPORTS)]
    assert offenders == []


@pytest.mark.parametrize("path", PROJECTION_FILES, ids=lambda p: p.name)
def test_the_projection_writes_no_row_of_its_own(path: Path):
    """Hard rule 5: ledger and order rows are read, never written. Every event goes
    through the T1 store (``append_plan_event``)."""
    tree = ast.parse(path.read_text())
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                called.add(func.attr)
            elif isinstance(func, ast.Name):
                called.add(func.id)
    assert called.isdisjoint({"add", "add_all", "insert", "update", "delete", "merge"})
    text = path.read_text().lower()
    for table in (
        "kis_live_order_ledger",
        "toss_live_order_ledger",
        "alpaca_paper_order_ledger",
        "execution_ledger",
    ):
        assert f"insert into review.{table}" not in text


def test_the_events_are_written_only_through_the_t1_store():
    text = (ROOT / "app/services/mandate/projection.py").read_text()
    assert "ms.append_plan_event(" in text


def test_the_projection_actor_is_the_system_actor_the_design_names():
    from app.services.mandate.projection_rules import ACTOR

    assert ACTOR == "system:mandate_projection"
    assert ACTOR.startswith("system:")  # kept_outside_basket is refused for it


# ------------------------------------------------------------ runbook vs argparse
def _runbook_commands() -> list[list[str]]:
    commands: list[list[str]] = []
    in_block = False
    for line in RUNBOOK.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("```"):
            in_block = line.strip() == "```bash"
            continue
        if in_block and "scripts.mandate_projection" in line:
            tokens = shlex.split(line.split("#", 1)[0].strip())
            start = tokens.index("scripts.mandate_projection") + 1
            commands.append(tokens[start:])
    return commands


def test_the_runbook_has_commands():
    assert len(_runbook_commands()) >= 3


def test_every_runbook_command_parses_against_the_real_argparse():
    for tokens in _runbook_commands():
        cli._parser().parse_args(tokens)


def test_every_flag_is_named_in_the_runbook():
    text = RUNBOOK.read_text(encoding="utf-8")
    for action in cli._parser()._actions:
        for option in action.option_strings:
            if option in ("-h", "--help"):
                continue
            assert option in text, option


def test_the_runbook_preview_commands_never_commit():
    """A preview example in the runbook must not carry --commit by accident."""
    commands = _runbook_commands()
    previews = [c for c in commands if "--commit" not in c]
    commits = [c for c in commands if "--commit" in c]
    assert previews and commits
